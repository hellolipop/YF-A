#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaDesk 持久化层（storage）：只用标准库 sqlite3 的策略跟踪存储。

模块定位
--------
`engine.py` 原本把全部任务写进 `data/strategy_runs.json`（整份文件重写 + 8 秒节流）。
任务一多，整文件重写的成本与并发风险都会放大，于是抽出这一层：
把「任务 / 逐笔交易 / 权益曲线 / 信号日志」拆成各自的表，用 SQLite 增量落库，
同时保留 JSON 文本列承载 `engine.py` 里的嵌套结构（params、monthly、pending、revisions…），
做到「结构可查询、细节不丢失」。

关键约定
--------
1. 仅依赖标准库 sqlite3（辅以 json / os / threading / time），零第三方依赖；
2. 建库即开启 WAL（读写不互相阻塞）与 foreign_keys（子表随任务级联删除）；
3. 表：runs / trades / equity / signals / logs / meta；
4. schema 版本存于 meta 表的 `schema_version` 键；`migrate()` 是结构升级的唯一入口，
   `migrate_from_json()` 负责把旧的 strategy_runs.json 整体搬进来；
5. trades 表固定包含 phase / signal_price / fill_price / fee / slippage：
   phase 区分「回溯 backfill / 实时 live」，signal_price 是信号触发价，
   fill_price 是含滑点的实际成交价，fee / slippage 用于交易成本归因；
6. run 的嵌套字段一律 JSON 序列化后存进 `runs.payload`；常用检索字段
   （market/code/name/strategy/status/created_at…）另外提升为独立列，列表页可直接排序过滤；
7. 线程安全：每个线程各持有一个连接（thread-local），写操作再用进程内互斥锁串行化，
   配合 WAL + busy_timeout 应对多线程并发读写。

顺序约定（很重要）
------------------
· `trades` / `equity` 按「时间从旧到新」追加，`list_*` 默认按写入顺序（旧 → 新）返回；
· `signals` 沿用 `engine.py` 的习惯「最新的在最前面」，因此 `list_signals()` 默认新 → 旧；
· `migrate_from_json()` 导入时会自动把信号列表反转成时间升序，保证与后续实时追加的顺序口径一致。
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 当前 schema 版本（每次改表结构都要 +1，并在 MIGRATIONS 里补一条升级步骤）
SCHEMA_VERSION = 2

#: schema 版本在 meta 表中的键名
META_SCHEMA_VERSION = "schema_version"

#: 默认库文件名（放 data/ 目录下）
DEFAULT_DB_NAME = "strategy.db"

#: 支持顺序号（seq）的表，SQL 表名白名单，避免拼接注入
_SEQ_TABLES = ("trades", "equity", "signals")


def now_ms():
    """当前时间戳（毫秒，与前端 / engine 的时间口径一致）"""
    return int(time.time() * 1000)


def _iso(ts_ms=None):
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'（仅用于可读展示）"""
    ts = (ts_ms if ts_ms is not None else now_ms()) / 1000.0
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _s(v):
    """转字符串；None 保持 None（避免把「无值」写成 'None'）"""
    return None if v is None else str(v)


def _f(v, default=None):
    """宽松转 float，失败返回 default"""
    if v is None or v == "" or isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=None):
    """宽松转 int，失败返回 default"""
    if v is None or v == "" or isinstance(v, bool):
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _b(v):
    """布尔字段落库为 0/1；None 保持 None"""
    return None if v is None else int(bool(v))


#: 日志级别别名（与 logs.py 的口径保持一致）
_LEVEL_ALIASES = {"warning": "warn", "err": "error", "critical": "error", "fatal": "error"}


def _alias_level(level):
    """日志级别归一：warning→warn、err/critical→error"""
    lv = str(level or "").strip().lower()
    return _LEVEL_ALIASES.get(lv, lv)


def _dumps(obj):
    """JSON 序列化：中文不转义、不可序列化的对象降级为字符串"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(text, default=None):
    """JSON 反序列化：脏数据不抛异常，返回 default"""
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 行 → 字典（列表查询的对外结构）
# --------------------------------------------------------------------------- #

#: 规范列 → 驼峰别名（legacy 视图用，与 engine.py / core.metrics 的字段口径对齐）
_LEGACY_TRADE_KEYS = (
    ("inDate", "in_date"), ("inPrice", "in_price"),
    ("outDate", "out_date"), ("outPrice", "out_price"),
    ("pnlPct", "pnl_pct"),
    # v1 接口曾用驼峰暴露信号价与成交价，前端与实现滑点展示依赖这两个别名
    ("signalPrice", "signal_price"), ("fillPrice", "fill_price"),
)

def _trade_view(row, legacy=False):
    """trades 行 → dict（规范字段 + extra 里未映射的原始字段）

    legacy=True 时额外补上 engine / metrics 习惯的驼峰别名
    （inDate / inPrice / outDate / outPrice / pnlPct），
    这样查询结果可以直接喂给 ``core.metrics.summarize(trades=...)``。
    """
    out = {
        "id": row["id"], "run_id": row["run_id"], "seq": row["seq"],
        "side": row["side"], "phase": row["phase"],
        "signal_price": row["signal_price"], "fill_price": row["fill_price"],
        "in_date": row["in_date"], "in_price": row["in_price"],
        "out_date": row["out_date"], "out_price": row["out_price"],
        "qty": row["qty"], "fee": row["fee"], "slippage": row["slippage"],
        "pnl": row["pnl"], "pnl_pct": row["pnl_pct"], "bars": row["bars"],
        "reason": row["reason"],
    }
    extra = _loads(row["extra"], {}) or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            out.setdefault(k, v)
    if legacy:
        for alias, col in _LEGACY_TRADE_KEYS:
            out.setdefault(alias, out.get(col))
    return out


def _equity_view(row):
    """equity 行 → dict"""
    out = {
        "id": row["id"], "run_id": row["run_id"], "seq": row["seq"],
        "t": row["t"], "v": row["v"], "close": row["close"],
        "position": bool(row["position"]) if row["position"] is not None else None,
    }
    extra = _loads(row["extra"], {}) or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            out.setdefault(k, v)
    return out


def _signal_view(row):
    """signals 行 → dict"""
    out = {
        "id": row["id"], "run_id": row["run_id"], "seq": row["seq"],
        "t": row["t"], "side": row["side"], "price": row["price"], "phase": row["phase"],
        "had_position": bool(row["had_position"]) if row["had_position"] is not None else None,
        "skipped": bool(row["skipped"]) if row["skipped"] is not None else None,
        "note": row["note"],
    }
    extra = _loads(row["extra"], {}) or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            out.setdefault(k, v)
    return out


# --------------------------------------------------------------------------- #
# 入参 → 列（字段别名映射）
# --------------------------------------------------------------------------- #

#: run 的检索列（其余字段进 payload JSON）
_RUN_COLUMNS = ("id", "market", "code", "name", "strategy", "strategy_name",
                "status", "mode", "period", "created_at", "created_date",
                "updated_at", "payload")

_RUN_INSERT = """
INSERT INTO runs (id, market, code, name, strategy, strategy_name, status, mode,
                  period, created_at, created_date, updated_at, payload)
VALUES (:id, :market, :code, :name, :strategy, :strategy_name, :status, :mode,
        :period, :created_at, :created_date, :updated_at, :payload)
ON CONFLICT(id) DO UPDATE SET
    market        = excluded.market,
    code          = excluded.code,
    name          = excluded.name,
    strategy      = excluded.strategy,
    strategy_name = excluded.strategy_name,
    status        = excluded.status,
    mode          = excluded.mode,
    period        = excluded.period,
    -- 任务创建时间只在首次写入时确定，后续更新不覆盖
    created_at    = COALESCE(runs.created_at, excluded.created_at),
    created_date  = COALESCE(runs.created_date, excluded.created_date),
    updated_at    = excluded.updated_at,
    payload       = excluded.payload
"""

#: 交易字段别名：把 engine / 前端 / 新命名的写法统一收敛到列名
_TRADE_ALIASES = {
    "side": ("side", "action", "direction"),
    "phase": ("phase", "stage"),
    "signal_price": ("signal_price", "signalPrice"),
    "fill_price": ("fill_price", "fillPrice"),
    "in_date": ("in_date", "inDate"),
    "in_price": ("in_price", "inPrice"),
    "out_date": ("out_date", "outDate"),
    "out_price": ("out_price", "outPrice"),
    "qty": ("qty", "quantity", "shares"),
    "fee": ("fee", "feeAmount"),
    "slippage": ("slippage",),
    "pnl": ("pnl", "profit"),
    "pnl_pct": ("pnl_pct", "pnlPct", "pnlPercent"),
    "bars": ("bars", "holdBars"),
    "reason": ("reason", "exitReason"),
}

_EQUITY_ALIASES = {
    "t": ("t", "time", "date"),
    "v": ("v", "value", "equity"),
    "close": ("close", "price"),
    "position": ("position", "hasPosition", "holding"),
}

_SIGNAL_ALIASES = {
    "t": ("t", "time", "date"),
    "side": ("side", "action"),
    "price": ("price", "signal_price", "signalPrice"),
    "phase": ("phase", "stage"),
    "had_position": ("had_position", "hadPosition"),
    "skipped": ("skipped",),
    "note": ("note", "reason"),
}


def _pick(src, aliases, default=None):
    """按别名顺序取第一个命中的值；同时把这些键从 src 里摘掉（返回剩余部分）"""
    for key in aliases:
        if key in src:
            return src.pop(key), src
    return default, src


def _norm_trade(trade):
    """交易 dict → trades 表的列值（未识别的字段照旧塞进 extra，保证信息不丢）

    · fill_price 取值顺序：显式 fill_price/fillPrice → 卖出时取 out_price → 买入时取 in_price；
    · signal_price 只认显式字段：它表达「信号触发价」，与成交价是两回事，不做猜测；
    · fee / slippage 缺省按 0 处理（成本归因不允许 NULL）；
      两个字段都是**原样透传**、不做单位换算：engine 里的 fee / slippage 是费率（0.0003 / 0.001），
      而 core.metrics.summarize 汇总 cost_total 时需要的是金额，调用方按自己的口径写入即可。
    """
    if not isinstance(trade, dict):
        raise ValueError("trade 必须是 dict")
    src = dict(trade)
    src.pop("id", None)
    src.pop("run_id", None)
    src.pop("seq", None)

    vals = {}
    for col, aliases in _TRADE_ALIASES.items():
        vals[col], src = _pick(src, aliases)

    side = vals["side"]
    if side is None:
        # 有平仓信息即卖出（engine 的逐笔交易是「回合制」记录）
        side = "sell" if (vals["out_date"] or vals["out_price"] is not None) else "buy"
    fill = vals["fill_price"]
    if fill is None:
        fill = vals["out_price"] if side == "sell" and vals["out_price"] is not None else vals["in_price"]

    return {
        "side": _s(side),
        "phase": _s(vals["phase"]),
        "signal_price": _f(vals["signal_price"]),
        "fill_price": _f(fill),
        "in_date": _s(vals["in_date"]),
        "in_price": _f(vals["in_price"]),
        "out_date": _s(vals["out_date"]),
        "out_price": _f(vals["out_price"]),
        "qty": _f(vals["qty"]),
        "fee": _f(vals["fee"], 0.0),
        "slippage": _f(vals["slippage"], 0.0),
        "pnl": _f(vals["pnl"]),
        "pnl_pct": _f(vals["pnl_pct"]),
        "bars": _i(vals["bars"]),
        "reason": _s(vals["reason"]),
        "extra": _dumps(src) if src else None,
    }


def _norm_equity(point):
    """权益点 dict → equity 表的列值"""
    if not isinstance(point, dict):
        raise ValueError("point 必须是 dict")
    src = dict(point)
    src.pop("id", None)
    src.pop("run_id", None)
    src.pop("seq", None)
    vals = {}
    for col, aliases in _EQUITY_ALIASES.items():
        vals[col], src = _pick(src, aliases)
    return {
        "t": _s(vals["t"]),
        "v": _f(vals["v"]),
        "close": _f(vals["close"]),
        "position": _b(vals["position"]),
        "extra": _dumps(src) if src else None,
    }


def _norm_signal(sig):
    """信号 dict → signals 表的列值"""
    if not isinstance(sig, dict):
        raise ValueError("sig 必须是 dict")
    src = dict(sig)
    src.pop("id", None)
    src.pop("run_id", None)
    src.pop("seq", None)
    vals = {}
    for col, aliases in _SIGNAL_ALIASES.items():
        vals[col], src = _pick(src, aliases)
    return {
        "t": _s(vals["t"]),
        "side": _s(vals["side"]),
        "price": _f(vals["price"]),
        "phase": _s(vals["phase"]),
        "had_position": _b(vals["had_position"]),
        "skipped": _b(vals["skipped"]),
        "note": _s(vals["note"]),
        "extra": _dumps(src) if src else None,
    }


# --------------------------------------------------------------------------- #
# 建表 DDL（按版本拆分，便于逐步迁移）
# --------------------------------------------------------------------------- #

#: v1：基础六表
_DDL_V1 = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        k          TEXT PRIMARY KEY,
        v          TEXT,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id            TEXT PRIMARY KEY,
        market        TEXT,
        code          TEXT,
        name          TEXT,
        strategy      TEXT,
        strategy_name TEXT,
        status        TEXT,
        mode          TEXT,
        period        TEXT,
        created_at    INTEGER,
        created_date  TEXT,
        updated_at    INTEGER,
        payload       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_runs_code ON runs (market, code)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status)",
    """
    CREATE TABLE IF NOT EXISTS trades (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        seq          INTEGER NOT NULL DEFAULT 0,
        side         TEXT,
        phase        TEXT,
        signal_price REAL,
        fill_price   REAL,
        in_date      TEXT,
        in_price     REAL,
        out_date     TEXT,
        out_price    REAL,
        qty          REAL,
        pnl          REAL,
        pnl_pct      REAL,
        bars         INTEGER,
        reason       TEXT,
        extra        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_run ON trades (run_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_trades_phase ON trades (run_id, phase)",
    """
    CREATE TABLE IF NOT EXISTS equity (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id   TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        seq      INTEGER NOT NULL DEFAULT 0,
        t        TEXT,
        v        REAL,
        close    REAL,
        position INTEGER,
        extra    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_equity_run ON equity (run_id, seq)",
    """
    CREATE TABLE IF NOT EXISTS signals (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        seq          INTEGER NOT NULL DEFAULT 0,
        t            TEXT,
        side         TEXT,
        price        REAL,
        phase        TEXT,
        had_position INTEGER,
        skipped      INTEGER,
        note         TEXT,
        extra        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_run ON signals (run_id, seq)",
    """
    CREATE TABLE IF NOT EXISTS logs (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      INTEGER NOT NULL,
        time    TEXT,
        level   TEXT NOT NULL,
        event   TEXT NOT NULL,
        run_id  TEXT,
        fields  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logs_level ON logs (level)",
    "CREATE INDEX IF NOT EXISTS idx_logs_run ON logs (run_id)",
)


def _apply_v1(conn):
    """v1 结构：六张基础表 + 索引"""
    for stmt in _DDL_V1:
        conn.execute(stmt)


def _apply_v2(conn):
    """v2 结构：trades 增加 fee / slippage 两个成本列（幂等：列已存在则跳过）"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    if "fee" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN fee REAL DEFAULT 0")
    if "slippage" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN slippage REAL DEFAULT 0")


#: 迁移步骤表：version 为目标版本，fn 接收连接（在事务里执行）
MIGRATIONS = (
    {"version": 1, "desc": "初始结构：runs / trades / equity / signals / logs / meta", "fn": _apply_v1},
    {"version": 2, "desc": "trades 增加 fee / slippage 交易成本列", "fn": _apply_v2},
)

#: 建表语句按版本索引：init_schema(version=N) 可直接建出历史版本结构（迁移演练 / 测试用）
_SCHEMA_BY_VERSION = {m["version"]: m["fn"] for m in MIGRATIONS}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

class Store:
    """策略跟踪数据的 SQLite 存储。

    典型用法::

        store = Store("data/strategy.db")        # 建/开库，自动建表 + 升级 schema
        store.upsert_run(run)                    # dict 原样存（嵌套字段走 JSON）
        store.append_trade(run["id"], trade)     # 逐笔追加
        store.list_equity(run["id"], limit=200)  # 取最近 200 个权益点

    说明：`:memory:` 内存库只在单线程下可用（每个线程一个连接）；需要多线程请用文件库。
    """

    def __init__(self, path=None, *, init=True, timeout=5.0, wal=True):
        self.path = str(path) if path else os.path.join("data", DEFAULT_DB_NAME)
        self.timeout = float(timeout)
        self.wal = bool(wal)
        self._lock = threading.RLock()        # 写操作串行化
        self._local = threading.local()       # 线程本地连接
        self._conns = []                      # 已创建的连接（close 时统一关闭）
        self._conns_lock = threading.Lock()
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        if init:
            self.init_schema()

    # ---------------------------------------------------------------- 连接 --
    def _conn(self):
        """取当前线程的连接（没有就建一个，并统一打开 WAL / foreign_keys）"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=self.timeout,
                               isolation_level=None,          # 关闭隐式事务，事务由 _tx 显式控制
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self.wal:
            conn.execute("PRAGMA journal_mode=WAL")          # 读写不互相阻塞
        conn.execute("PRAGMA foreign_keys=ON")               # 外键（连接级开关）
        conn.execute("PRAGMA busy_timeout=%d" % int(self.timeout * 1000))
        conn.execute("PRAGMA synchronous=NORMAL")
        self._local.conn = conn
        self._local.depth = 0
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    @contextmanager
    def _tx(self):
        """写事务：进程内互斥 + 显式 BEGIN/COMMIT，支持同线程嵌套（内层复用外层事务）"""
        with self._lock:
            conn = self._conn()
            depth = getattr(self._local, "depth", 0)
            inner = depth > 0
            if not inner:
                conn.execute("BEGIN IMMEDIATE")
            self._local.depth = depth + 1
            try:
                yield conn
            except BaseException:
                self._local.depth -= 1
                if not inner:
                    conn.execute("ROLLBACK")
                raise
            else:
                self._local.depth -= 1
                if not inner:
                    conn.execute("COMMIT")

    def pragma(self, name, value=None):
        """读取/设置 PRAGMA（测试与运维自检用）。

        注意：设置走自动提交（不带事务），因为 SQLite 在事务内会忽略 foreign_keys 的变更。
        """
        if value is None:
            row = self._conn().execute("PRAGMA %s" % name).fetchone()
            return row[0] if row is not None else None
        with self._lock:
            self._conn().execute("PRAGMA %s=%s" % (name, value))
        return value

    def journal_mode(self):
        """当前日志模式（期望 'wal'）"""
        return str(self.pragma("journal_mode") or "").lower()

    def foreign_keys_enabled(self):
        """当前连接是否开启外键约束"""
        return bool(self.pragma("foreign_keys"))

    def close(self):
        """关闭所有连接（含其他线程创建的连接）"""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if getattr(self._local, "conn", None) is not None:
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------- schema --
    def init_schema(self, version=None):
        """建表并写入 schema 版本。

        version=None（默认）：建出「当前最新」结构；库中若没有版本记录则写为最新版本。
        version=N（显式传入）：建出第 N 版历史结构并把版本写成 N，仅用于迁移演练与测试。
        """
        explicit = version is not None
        ver = int(version if explicit else SCHEMA_VERSION)
        if ver not in _SCHEMA_BY_VERSION:
            raise ValueError("未知 schema 版本：%s" % ver)
        with self._tx() as conn:
            # 逐级建表：新建库时等价于把全部迁移步骤按顺序走一遍
            for step in MIGRATIONS:
                if step["version"] <= ver:
                    step["fn"](conn)
        if explicit or self.meta_get(META_SCHEMA_VERSION) is None:
            self.meta_set(META_SCHEMA_VERSION, ver)
        return ver

    def schema_version(self):
        """读取当前 schema 版本（无记录返回 0，代表空库）"""
        return int(self.meta_get(META_SCHEMA_VERSION, 0) or 0)

    def migrate(self, target=None):
        """结构迁移入口：把库从当前版本逐级升到 target（默认最新版）。

        返回 ``{"from": 旧版本, "to": 新版本, "applied": [版本号…]}``；
        已是最新版时 applied 为空，可安全重复调用。
        """
        target = int(target if target is not None else SCHEMA_VERSION)
        known = sorted(_SCHEMA_BY_VERSION)
        if target not in _SCHEMA_BY_VERSION or target > known[-1]:
            raise ValueError("未知 schema 版本：%s" % target)
        cur = self.schema_version()
        applied = []
        for step in MIGRATIONS:
            ver = step["version"]
            if ver <= cur or ver > target:
                continue
            with self._tx() as conn:
                step["fn"](conn)
                conn.execute(
                    "INSERT INTO meta (k, v, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
                    (META_SCHEMA_VERSION, _dumps(ver), now_ms()),
                )
            applied.append(ver)
        if applied:
            self.meta_set("schema_migrated_at", now_ms())
        return {"from": cur, "to": applied[-1] if applied else cur, "applied": applied}

    # -------------------------------------------------------------- 元数据 --
    def meta_get(self, k, default=None):
        """读 meta（值以 JSON 保存，按原类型返回；缺失或脏数据返回 default）"""
        row = self._conn().execute("SELECT v FROM meta WHERE k = ?", (str(k),)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except (TypeError, ValueError):
            return row["v"] if row["v"] is not None else default

    def meta_set(self, k, v):
        """写 meta（任意可 JSON 序列化的值）"""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta (k, v, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
                (str(k), _dumps(v), now_ms()),
            )
        return v

    def meta_all(self):
        """读取全部 meta（dict）"""
        rows = self._conn().execute("SELECT k, v FROM meta").fetchall()
        out = {}
        for row in rows:
            try:
                out[row["k"]] = json.loads(row["v"])
            except (TypeError, ValueError):
                out[row["k"]] = row["v"]
        return out

    # ---------------------------------------------------------------- runs --
    def upsert_run(self, run):
        """写入/更新一个任务（按 id upsert）。

        嵌套字段（params、monthly、pending、revisions、benchmarkStart…）整体 JSON 序列化进
        ``runs.payload``；market/code/name/strategy/status 等常用字段另存独立列。
        返回任务 id。
        """
        if not isinstance(run, dict):
            raise ValueError("run 必须是 dict")
        rid = _s(run.get("id"))
        if not rid:
            raise ValueError("run 缺少 id")
        payload = dict(run)
        payload["id"] = rid
        row = {
            "id": rid,
            "market": _s(run.get("market")),
            "code": _s(run.get("code")),
            "name": _s(run.get("name")),
            "strategy": _s(run.get("strategy")),
            "strategy_name": _s(run.get("strategyName", run.get("strategy_name"))),
            "status": _s(run.get("status")),
            "mode": _s(run.get("mode")),
            "period": _s(run.get("period")),
            "created_at": _i(run.get("createdAt", run.get("created_at"))),
            "created_date": _s(run.get("createdDate", run.get("created_date"))),
            "updated_at": now_ms(),
            "payload": _dumps(payload),
        }
        with self._tx() as conn:
            conn.execute(_RUN_INSERT, row)
        return rid

    def get_run(self, rid):
        """按 id 取回任务（原样还原，含嵌套字段；不存在返回 None）"""
        row = self._conn().execute("SELECT payload FROM runs WHERE id = ?", (_s(rid),)).fetchone()
        if row is None:
            return None
        data = _loads(row["payload"], None)
        if not isinstance(data, dict):
            return {"id": _s(rid)}
        return data

    def list_runs(self, limit=None, status=None, market=None):
        """任务列表（按创建时间倒序，新任务在前）；返回完整的 run dict 列表"""
        sql = "SELECT payload, id FROM runs"
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(str(status))
        if market:
            where.append("market = ?")
            params.append(str(market))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(created_at, 0) DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn().execute(sql, params).fetchall()
        out = []
        for row in rows:
            data = _loads(row["payload"], None)
            out.append(data if isinstance(data, dict) else {"id": row["id"]})
        return out

    def get_run_column(self, rid, name):
        """读取任务的检索列（market/code/status… 免去解析 JSON）"""
        if name not in _RUN_COLUMNS:
            raise ValueError("不支持的列：%s" % name)
        row = self._conn().execute(
            "SELECT %s AS v FROM runs WHERE id = ?" % name, (_s(rid),)
        ).fetchone()
        return row["v"] if row is not None else None

    def delete_run(self, rid):
        """删除任务（trades / equity / signals 靠外键级联删除）；返回是否删到了记录"""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM runs WHERE id = ?", (_s(rid),))
        return cur.rowcount > 0

    def _ensure_run(self, conn, run_id):
        """写子表前校验任务存在，给出比外键报错更清晰的提示"""
        rid = _s(run_id)
        row = conn.execute("SELECT 1 FROM runs WHERE id = ?", (rid,)).fetchone()
        if row is None:
            raise ValueError("任务不存在：%s" % rid)
        return rid

    def _next_seq(self, conn, table, run_id):
        """取某任务在子表里的下一个顺序号（从 0 开始递增）"""
        if table not in _SEQ_TABLES:
            raise ValueError("不支持的子表：%s" % table)
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM %s WHERE run_id = ?" % table,
            (run_id,),
        ).fetchone()
        return int(row["n"])

    # -------------------------------------------------------------- trades --
    _TRADE_INSERT = """
    INSERT INTO trades (run_id, seq, side, phase, signal_price, fill_price,
                        in_date, in_price, out_date, out_price, qty,
                        fee, slippage, pnl, pnl_pct, bars, reason, extra)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def append_trade(self, run_id, trade):
        """追加一笔交易，返回自增主键 id。

        交易 dict 允许使用 engine / 前端习惯的驼峰命名（inPrice、outDate…），
        也支持规范命名（signal_price、fill_price、fee、slippage…）。
        """
        vals = _norm_trade(trade)
        with self._tx() as conn:
            rid = self._ensure_run(conn, run_id)
            seq = self._next_seq(conn, "trades", rid)
            cur = conn.execute(self._TRADE_INSERT, (
                rid, seq, vals["side"], vals["phase"], vals["signal_price"], vals["fill_price"],
                vals["in_date"], vals["in_price"], vals["out_date"], vals["out_price"], vals["qty"],
                vals["fee"], vals["slippage"], vals["pnl"], vals["pnl_pct"], vals["bars"],
                vals["reason"], vals["extra"],
            ))
            return cur.lastrowid

    def list_trades(self, run_id, limit=None, phase=None, legacy=False):
        """交易列表（按写入顺序：旧 → 新）。

        limit 表示「取最近 N 笔」；phase 可过滤 backfill / live；
        legacy=True 时额外带上驼峰别名（inDate / outDate / pnlPct…），
        与 `engine.py` 的逐笔交易、`core.metrics.summarize` 的入参口径一致。
        """
        sql = "SELECT * FROM trades WHERE run_id = ?"
        params = [_s(run_id)]
        if phase:
            sql += " AND phase = ?"
            params.append(str(phase))
        if limit:
            # 先按倒序取最近 N 条，再翻回升序返回
            sql += " ORDER BY seq DESC LIMIT ?"
            params.append(int(limit))
            rows = self._conn().execute(sql, params).fetchall()
            rows = list(reversed(rows))
        else:
            sql += " ORDER BY seq ASC"
            rows = self._conn().execute(sql, params).fetchall()
        return [_trade_view(r, legacy=legacy) for r in rows]

    def replace_trades(self, run_id, trades):
        """整体替换某任务的交易明细（迁移 / 重置场景），返回写入条数"""
        items = list(trades or [])
        with self._tx() as conn:
            rid = self._ensure_run(conn, run_id)
            conn.execute("DELETE FROM trades WHERE run_id = ?", (rid,))
            for i, trade in enumerate(items):
                vals = _norm_trade(trade)
                conn.execute(self._TRADE_INSERT, (
                    rid, i, vals["side"], vals["phase"], vals["signal_price"], vals["fill_price"],
                    vals["in_date"], vals["in_price"], vals["out_date"], vals["out_price"], vals["qty"],
                    vals["fee"], vals["slippage"], vals["pnl"], vals["pnl_pct"], vals["bars"],
                    vals["reason"], vals["extra"],
                ))
        return len(items)

    # -------------------------------------------------------------- equity --
    _EQUITY_INSERT = """
    INSERT INTO equity (run_id, seq, t, v, close, position, extra)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    def append_equity(self, run_id, point):
        """追加一个权益快照点（{'t': 'YYYY-MM-DD', 'v': 权益, 'close': 收盘价, 'position': bool}）"""
        vals = _norm_equity(point)
        with self._tx() as conn:
            rid = self._ensure_run(conn, run_id)
            seq = self._next_seq(conn, "equity", rid)
            cur = conn.execute(self._EQUITY_INSERT, (
                rid, seq, vals["t"], vals["v"], vals["close"], vals["position"], vals["extra"],
            ))
            return cur.lastrowid

    def list_equity(self, run_id, limit=None):
        """权益曲线（按时间升序；limit 表示只取最近 N 个点，仍以升序返回）"""
        sql = "SELECT * FROM equity WHERE run_id = ?"
        params = [_s(run_id)]
        if limit:
            sql += " ORDER BY seq DESC LIMIT ?"
            params.append(int(limit))
            rows = list(reversed(self._conn().execute(sql, params).fetchall()))
        else:
            sql += " ORDER BY seq ASC"
            rows = self._conn().execute(sql, params).fetchall()
        return [_equity_view(r) for r in rows]

    def replace_equity(self, run_id, points):
        """整体替换权益曲线，返回写入条数"""
        items = list(points or [])
        with self._tx() as conn:
            rid = self._ensure_run(conn, run_id)
            conn.execute("DELETE FROM equity WHERE run_id = ?", (rid,))
            for i, point in enumerate(items):
                vals = _norm_equity(point)
                conn.execute(self._EQUITY_INSERT, (
                    rid, i, vals["t"], vals["v"], vals["close"], vals["position"], vals["extra"],
                ))
        return len(items)

    # ------------------------------------------------------------- signals --
    _SIGNAL_INSERT = """
    INSERT INTO signals (run_id, seq, t, side, price, phase, had_position, skipped, note, extra)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def append_signal(self, run_id, sig):
        """追加一条信号记录（phase 区分 backfill / live；skipped 标记因资金不足被跳过的买入）"""
        vals = _norm_signal(sig)
        with self._tx() as conn:
            rid = self._ensure_run(conn, run_id)
            seq = self._next_seq(conn, "signals", rid)
            cur = conn.execute(self._SIGNAL_INSERT, (
                rid, seq, vals["t"], vals["side"], vals["price"], vals["phase"],
                vals["had_position"], vals["skipped"], vals["note"], vals["extra"],
            ))
            return cur.lastrowid

    def list_signals(self, run_id, limit=None, phase=None):
        """信号列表（最新的在前，与 engine / 前端展示口径一致）；limit 表示取最近 N 条"""
        sql = "SELECT * FROM signals WHERE run_id = ?"
        params = [_s(run_id)]
        if phase:
            sql += " AND phase = ?"
            params.append(str(phase))
        sql += " ORDER BY seq DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn().execute(sql, params).fetchall()
        return [_signal_view(r) for r in rows]

    def replace_signals(self, run_id, signals, *, reverse=False):
        """整体替换信号列表（reverse=True 时先把「新 → 旧」的入参翻成时间升序再落库）"""
        items = list(signals or [])
        if reverse:
            items = list(reversed(items))
        with self._tx() as conn:
            rid = self._ensure_run(conn, run_id)
            conn.execute("DELETE FROM signals WHERE run_id = ?", (rid,))
            for i, sig in enumerate(items):
                vals = _norm_signal(sig)
                conn.execute(self._SIGNAL_INSERT, (
                    rid, i, vals["t"], vals["side"], vals["price"], vals["phase"],
                    vals["had_position"], vals["skipped"], vals["note"], vals["extra"],
                ))
        return len(items)

    # ---------------------------------------------------------------- logs --
    def append_log(self, level, event, run_id=None, **fields):
        """写一条结构化日志到 logs 表；返回自增 id（可直接作为 logs.Logger 的 sink）"""
        ts = now_ms()
        extra = dict(fields)
        if run_id is None:
            run_id = extra.pop("run_id", None)
        extra.pop("ts", None)
        extra.pop("time", None)
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO logs (ts, time, level, event, run_id, fields) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, _iso(ts), str(level or "info").lower(), str(event or ""),
                 _s(run_id), _dumps(extra) if extra else None),
            )
            return cur.lastrowid

    def list_logs(self, limit=100, level=None, min_level=None, run_id=None):
        """日志查询（时间倒序，最新在前）。

        level：精确匹配；min_level：按严重度「不低于」过滤（debug < info < warn < error）。
        """
        sql = "SELECT * FROM logs"
        where, params = [], []
        if level:
            where.append("level = ?")
            params.append(_alias_level(level))
        if min_level:
            order = ("debug", "info", "warn", "error")
            lv = _alias_level(min_level)
            if lv in order:
                keep = order[order.index(lv):]
                where.append("level IN (%s)" % ", ".join("?" * len(keep)))
                params.extend(keep)
        if run_id:
            where.append("run_id = ?")
            params.append(_s(run_id))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn().execute(sql, params).fetchall()
        out = []
        for row in rows:
            rec = {
                "id": row["id"], "ts": row["ts"], "time": row["time"],
                "level": row["level"], "event": row["event"], "run_id": row["run_id"],
            }
            fields = _loads(row["fields"], {}) or {}
            if isinstance(fields, dict):
                for k, v in fields.items():
                    rec.setdefault(k, v)
            out.append(rec)
        return out

    # ------------------------------------------------------------ 计数辅助 --
    def counts(self):
        """各表行数（自检 / 测试用）"""
        out = {}
        for table in ("runs", "trades", "equity", "signals", "logs", "meta"):
            row = self._conn().execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()
            out[table] = int(row["n"])
        return out

    # ----------------------------------------------------------- JSON 迁移 --
    def migrate_from_json(self, path, *, replace=True):
        """把旧的 ``data/strategy_runs.json`` 整体导入 SQLite。

        兼容三种文件结构：``{"runs": [...]}``（engine.py 的落盘格式）、直接是 run 列表、
        或者是单个 run 对象（含 id 字段）。
        对每个任务：写 runs 行 → 交易/权益按原顺序写入 → 信号反转成时间升序后写入。
        replace=True（默认）时子表先清空再写，重复导入不会出现重复明细。

        返回 ``{"source", "runs", "trades", "equity", "signals", "skipped"}``。
        """
        src = str(path)
        with open(src, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            runs = data.get("runs")
            if runs is None and data.get("id") is not None:
                runs = [data]                      # 单个 run 对象
            runs = runs or []
            meta = data
        elif isinstance(data, list):
            runs = data
            meta = {}
        else:
            raise ValueError("无法识别的 JSON 结构：%s" % src)

        stat = {"source": src, "runs": 0, "trades": 0, "equity": 0, "signals": 0, "skipped": 0}
        for run in runs:
            if not isinstance(run, dict) or not _s(run.get("id")):
                stat["skipped"] += 1
                continue
            payload = dict(run)          # 只把「非明细」字段写进 runs，明细走各自的表
            trades = payload.pop("trades", None) or []
            equity = payload.pop("equity", None) or []
            signals = payload.pop("signals", None) or []
            self.upsert_run(payload)
            rid = payload["id"]
            if replace:
                stat["trades"] += self.replace_trades(rid, trades)
                stat["equity"] += self.replace_equity(rid, equity)
                stat["signals"] += self.replace_signals(rid, signals, reverse=True)
            else:
                for t in trades:
                    self.append_trade(rid, t)
                for p in equity:
                    self.append_equity(rid, p)
                for s in reversed(signals):
                    self.append_signal(rid, s)
                stat["trades"] += len(trades)
                stat["equity"] += len(equity)
                stat["signals"] += len(signals)
            stat["runs"] += 1

        # 顺带记录导入进度与原始文件版本
        self.meta_set("migrated_from", src)
        self.meta_set("migrated_at", now_ms())
        if meta.get("version") is not None:
            self.meta_set("migrated_source_version", meta.get("version"))
        if meta.get("savedAt") is not None:
            self.meta_set("migrated_source_saved_at", meta.get("savedAt"))
        return stat
