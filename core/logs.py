#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaDesk 结构化日志（logs）：JSONL 落盘 + 内存环形缓冲。

设计目标
--------
1. **结构化**：每条日志就是一行 JSON（JSONL / ndjson），字段固定为
   `ts`（毫秒时间戳）、`time`（可读时间）、`level`、`event`，其余业务字段由调用方以
   `log(level, event, **fields)` 传入，落盘后可直接 `grep` / `jq` / 喂给离线分析；
2. **可落盘**：以追加方式写日志文件，进程崩溃也不会丢已 flush 的内容（每行写完即 flush）；
3. **可即时查询**：内存里维护固定容量的环形缓冲（默认 500 条），服务端接口 / 界面
   可以直接拉最近日志，无需读磁盘；
4. **零依赖**：只用标准库（json / os / threading / collections / time）。

使用方式
--------
::

    from core.logs import get_logger

    lg = get_logger("data/strategy.log")       # 默认容量 500、默认级别 info
    lg.log("info", "run.created", run_id="s123", code="600519")   # 落盘 + 入环形缓冲
    lg.ring(20, level="warn")                  # 取最近 20 条「warn 及以上」

模块级同名快捷函数（走全局默认 logger）::

    from core import logs
    logs.log("error", "quote.failed", code="AAPL", err="timeout")
    logs.ring(10)
"""

import collections
import json
import os
import threading
import time

#: 环形缓冲默认容量（条）
DEFAULT_CAPACITY = 500

#: 认可的日志级别（从低到高）
LEVELS = ("debug", "info", "warn", "error")

#: 级别严重度：用于 ring(level=...) 的「不低于」过滤
_LEVEL_RANK = {"debug": 10, "info": 20, "warn": 30, "warning": 30,
               "error": 40, "err": 40, "critical": 50}

#: 级别别名归一
_LEVEL_ALIASES = {"warning": "warn", "err": "error", "critical": "error", "fatal": "error"}

#: 记录里的保留字段（业务 fields 不得覆盖）
RESERVED_KEYS = ("ts", "time", "level", "event")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def now_ms():
    """当前毫秒时间戳"""
    return int(time.time() * 1000)


def norm_level(level):
    """级别归一：'WARNING' → 'warn'、'err' → 'error'；未知级别按 info 处理"""
    lv = str(level or "info").strip().lower()
    lv = _LEVEL_ALIASES.get(lv, lv)
    return lv if lv in LEVELS else "info"


def level_rank(level):
    """级别的严重度数值（未知级别按 info=20 处理）"""
    return _LEVEL_RANK.get(str(level or "").strip().lower(), 20)


def _dumps(obj):
    """JSON 序列化：中文不转义、不可序列化的对象降级为字符串（日志不能因为脏字段写不进去）"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _iso(ts_ms=None):
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS.mmm'（可读展示用）"""
    ts = (ts_ms if ts_ms is not None else now_ms()) / 1000.0
    head = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return "%s.%03d" % (head, int(round((ts - int(ts)) * 1000)) % 1000)


# --------------------------------------------------------------------------- #
# 环形缓冲
# --------------------------------------------------------------------------- #

class RingBuffer:
    """固定容量的环形缓冲：写满后新记录覆盖最旧的记录（内存占用恒定）。

    内部顺序：旧 → 新；对外查询时再反转，最新的排在前面。
    """

    def __init__(self, capacity=DEFAULT_CAPACITY):
        self._capacity = max(1, int(capacity))
        self._buf = collections.deque(maxlen=self._capacity)
        self._lock = threading.Lock()

    @property
    def capacity(self):
        return self._capacity

    def append(self, rec):
        """写入一条记录（返回该记录）"""
        with self._lock:
            self._buf.append(rec)
        return rec

    def snapshot(self):
        """当前缓冲快照（旧 → 新）"""
        with self._lock:
            return list(self._buf)

    def clear(self):
        """清空缓冲"""
        with self._lock:
            self._buf.clear()

    def __len__(self):
        with self._lock:
            return len(self._buf)


# --------------------------------------------------------------------------- #
# Logger
# --------------------------------------------------------------------------- #

class Logger:
    """结构化日志器：JSONL 追加落盘 + 内存环形缓冲（默认 500 条）。

    :param path:     日志文件路径；None 表示只写内存缓冲（不落盘）；
                     若传入目录路径，则自动使用目录下的 ``app.jsonl``。
    :param capacity: 环形缓冲容量（条），默认 500
    :param level:    最低记录级别，低于该级别的日志直接丢弃（默认 info）
    :param sink:     可选的额外落点（callable，接收记录 dict）；
                     可直接传 ``store.append_log`` 把日志同时写进 SQLite logs 表
    :param echo:     是否同步打印到 stdout（调试用，默认关闭）
    """

    def __init__(self, path=None, capacity=DEFAULT_CAPACITY, level="info",
                 sink=None, echo=False):
        self.path = self._resolve_path(path)
        self.level = norm_level(level)
        self.buffer = RingBuffer(capacity)
        self._sink = sink
        self._echo = bool(echo)
        self._lock = threading.Lock()
        self._fh = None
        self.last_error = None      # 落盘失败时记录原因（日志系统自身不抛异常）
        self.count = 0              # 实际记录（落盘/入缓冲）过的条数
        self.dropped = 0            # 因级别不足被丢弃的条数

    @staticmethod
    def _resolve_path(path):
        """路径归一：目录 → 目录下 app.jsonl；文件 → 原样"""
        if not path:
            return None
        p = os.path.abspath(str(path))
        if os.path.isdir(p) or str(path).endswith(os.sep):
            return os.path.join(p, "app.jsonl")
        return p

    # ------------------------------------------------------------ 写入 --
    def log(self, level, event, **fields):
        """写一条日志，返回记录 dict（即使因级别被丢弃也会返回，方便调用方复用）。

        记录结构：``{"ts", "time", "level", "event", **fields}``。
        `level` / `event` 由位置参数决定；fields 里的 `ts` / `time` 会被真实值覆盖，
        避免业务字段污染记录的框架字段。
        """
        lv = norm_level(level)
        if level_rank(lv) < level_rank(self.level):
            self.dropped += 1
            return {"ts": now_ms(), "time": "", "level": lv, "event": str(event), **fields}

        ts = now_ms()
        rec = dict(fields)
        rec.update({"ts": ts, "time": _iso(ts), "level": lv, "event": str(event)})

        self.buffer.append(rec)          # 内存缓冲（接口/界面即时查询）
        self.count += 1
        self._write_line(rec)            # JSONL 追加落盘
        self._to_sink(rec)               # 可选二级落点（如 SQLite logs 表）
        if self._echo:
            print("[%s] %s %s" % (rec["level"], rec["event"], _dumps(
                {k: v for k, v in rec.items() if k not in RESERVED_KEYS})))
        return rec

    def debug(self, event, **fields):
        return self.log("debug", event, **fields)

    def info(self, event, **fields):
        return self.log("info", event, **fields)

    def warn(self, event, **fields):
        return self.log("warn", event, **fields)

    def error(self, event, **fields):
        return self.log("error", event, **fields)

    # ------------------------------------------------------------ 查询 --
    def ring(self, limit=None, level=None):
        """读取环形缓冲，最新的在最前面。

        :param limit: 最多返回多少条；None 表示缓冲内全部（上限即容量）
        :param level: 最低级别（含），如 'warn' 只返回 warn / error
        """
        recs = self.buffer.snapshot()                 # 旧 → 新
        if level:
            floor = level_rank(level)
            recs = [r for r in recs if level_rank(r.get("level")) >= floor]
        if limit:
            n = int(limit)
            recs = recs[-n:] if n > 0 else []
        recs.reverse()                                # 新 → 旧
        return recs

    def tail(self, limit=20, level=None):
        """ring 的语义化别名（看最近若干条）"""
        return self.ring(limit, level)

    def clear(self):
        """清空内存缓冲（不影响已落盘文件）"""
        self.buffer.clear()

    # ------------------------------------------------------------ 落盘 --
    def _write_line(self, rec):
        """JSONL 追加落盘：写一行 flush 一行，异常只记录不抛出"""
        if not self.path:
            return
        try:
            with self._lock:
                if self._fh is None:
                    parent = os.path.dirname(self.path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    self._fh = open(self.path, "a", encoding="utf-8")
                self._fh.write(_dumps(rec) + "\n")
                self._fh.flush()
        except OSError as exc:      # 磁盘满 / 权限问题：日志系统自身不能拖垮业务
            self.last_error = str(exc)

    def _to_sink(self, rec):
        """写入二级落点（异常同样不抛出）"""
        if self._sink is None:
            return
        try:
            fields = {k: v for k, v in rec.items() if k not in RESERVED_KEYS}
            self._sink(rec.get("level"), rec.get("event"), **fields)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)

    def flush(self):
        """把文件句柄缓冲刷到磁盘"""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError as exc:
                    self.last_error = str(exc)

    def close(self):
        """关闭文件句柄"""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# --------------------------------------------------------------------------- #
# 读回 JSONL（离线排查 / 测试）
# --------------------------------------------------------------------------- #

def read_jsonl(path, limit=None, level=None):
    """读回 JSONL 日志文件，最新的在最前面。

    与 ``Logger.ring`` 保持同样的过滤口径（level 为最低级别，含）；
    解析失败的行会被跳过（例如写入过程中被中断的最后一行）。
    """
    recs = []
    if not path or not os.path.exists(path):
        return recs
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                recs.append(rec)
    if level:
        floor = level_rank(level)
        recs = [r for r in recs if level_rank(r.get("level")) >= floor]
    if limit:
        n = int(limit)
        recs = recs[-n:] if n > 0 else []
    recs.reverse()
    return recs


# --------------------------------------------------------------------------- #
# 全局默认 logger（模块级快捷函数）
# --------------------------------------------------------------------------- #

_DEFAULT = {"logger": None, "lock": threading.Lock()}


def get_logger(path=None, capacity=DEFAULT_CAPACITY, level="info", sink=None,
               echo=False, default=None):
    """取全局默认 logger；首次调用时按参数创建（其后调用会直接复用）。

    :param default: True 时无视已存在的默认 logger，强制按本次参数重建
    """
    with _DEFAULT["lock"]:
        lg = _DEFAULT["logger"]
        if lg is None or default:
            lg = Logger(path, capacity=capacity, level=level, sink=sink, echo=echo)
            _DEFAULT["logger"] = lg
        return lg


def configure(path=None, capacity=DEFAULT_CAPACITY, level="info", sink=None, echo=False):
    """重新配置全局默认 logger（服务启动时调用一次即可），返回新 logger"""
    return get_logger(path, capacity=capacity, level=level, sink=sink, echo=echo, default=True)


def log(level, event, **fields):
    """模块级写入（走全局默认 logger）"""
    return get_logger().log(level, event, **fields)


def ring(limit=None, level=None):
    """模块级查询（走全局默认 logger 的环形缓冲，最新在前）"""
    return get_logger().ring(limit, level)


def sqlite_sink(store):
    """把 ``Store.append_log`` 适配成 Logger 的 sink，实现「JSONL + SQLite」双写::

        logger = Logger("data/app.jsonl", sink=sqlite_sink(store))
    """
    if store is None:
        return None

    def _sink(level, event, **fields):
        return store.append_log(level, event, **fields)

    return _sink
