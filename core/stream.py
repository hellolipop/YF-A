# -*- coding: utf-8 -*-
"""AlphaDesk · 实时推送中枢（core/stream.py）

为什么是 SSE 而不是 WebSocket
-----------------------------
1. 本项目坚持零第三方依赖：WebSocket 要手写握手、掩码、帧解析与分片，出错面积大；
   SSE 只是一个长连接的 ``text/event-stream`` 响应，浏览器 ``EventSource`` 原生支持；
2. 推送方向是单向的（服务端 → 浏览器），反向操作（下单、改配置、改备注）继续走普通
   POST，职责与语义都更清晰，也便于用 curl 直接测试；
3. ``EventSource`` 自带断线重连与 ``Last-Event-ID``，配合服务端的**有限重放**（每个通道
   保留最近若干条事件），短暂抖动不会丢事件。

三条通道
--------
============  ==========  ==============================================================
``quotes``    默认 3 秒   行情快照。上游是批量报价接口，**多客户端订阅同一批标的只发一次
                          上游请求**（这正是 hub 存在的意义，否则每个浏览器都变成一台
                          独立爬虫，公开行情接口会被打爆）
``advisor``   默认 30 秒  AI 研判。周期性重新研判，但**只在实质性变化时推**（档位变了 /
                          评分摆动 ≥5 / 计划价位变了 / 预测概率摆动 ≥10% / 仓位摆动 ≥2%）；
                          没有变化时推 ``pulse`` 说明「已检查、无变化」—— 让用户知道系统
                          在干活，而不是靠静默伪装成「一切正常」
``trade``     事件驱动   委托、成交、账户快照、配置变化
============  ==========  ==============================================================

两个工程约束（都在下面的实现里被显式处理）
------------------------------------------
**背压**：每个订阅者一条有界队列（默认 64）。客户端读得慢时丢**最旧**的事件并累加
``dropped``（在 ``/api/stream/status`` 可见）。绝不因为一个慢客户端阻塞发布线程 ——
一旦发布被阻塞，所有通道都会退化成串行，推送也就名存实亡。

**上游限流**：全局最小请求间隔（默认 1.2 秒）由锁保护，所有通道与客户端共享。公开
行情接口不该被高频打；宁可让推送慢一点，也不要让工具变成压力源。

线程模型
--------
只有一个后台刷新线程（懒启动）：它按照每个通道自己的 ``interval`` 判断是否到期并执行
``tick()``。单线程带来两个好处：上游请求天然串行（限流实现简单且准确），以及不会因为
通道/客户端数量增长而线程爆炸。没有任何订阅者时，通道被回收、线程自行退出；下一次订阅
再启动。所有共享状态都用 ``RLock`` 保护。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

__all__ = [
    "StreamHub", "Subscriber", "Channel", "diff_advice",
    "KIND_QUOTES", "KIND_ADVISOR", "KIND_TRADE",
    "DIFF_SCORE", "DIFF_PROB", "DIFF_WEIGHT", "DIFF_RETURN",
    "MAX_QUEUE", "MAX_REPLAY", "MIN_UPSTREAM_GAP",
]

KIND_QUOTES = "quotes"
KIND_ADVISOR = "advisor"
KIND_TRADE = "trade"
KINDS = (KIND_QUOTES, KIND_ADVISOR, KIND_TRADE)

#: 订阅者队列长度：超出后丢最旧事件（背压保护）
MAX_QUEUE = 64
#: 每个通道保留的可重放事件条数（配合 EventSource 的 Last-Event-ID）
MAX_REPLAY = 32
#: 上游请求的全局最小间隔（秒）
MIN_UPSTREAM_GAP = 1.2
#: 刷新线程的轮询步长（秒）——只影响到期判断精度，与上游频率无关
TICK_STEP = 0.25

#: 研判「实质性变化」的阈值。低于阈值的变化不推：否则每 30 秒都会因为小数抖动
#: 产生一堆噪声推送，用户会把整条通道静音，反而错过真正的信号
DIFF_SCORE = 5.0      # 综合评分
DIFF_PROB = 0.10      # 条件分布上涨概率
DIFF_WEIGHT = 0.02    # 凯利权重
DIFF_RETURN = 1.5     # 窗口内期望收益（百分数）

#: 各通道的间隔边界
INTERVAL_RANGE = {
    KIND_QUOTES: (1, 60, 3),
    KIND_ADVISOR: (10, 600, 30),
}
#: 单通道最多订阅多少只标的（与 AI 选股接口的上限保持一致口径）
MAX_SYMBOLS = 60

#: ``ready`` 帧里给前端的通道说明（说明「你会收到什么、多久收一次」）
READY_NOTE = {
    KIND_QUOTES: "行情通道已建立：按 interval 推送 quote 事件；无对应标的会出现在 failed 里。",
    KIND_ADVISOR: "研判通道已建立：先推一次 snapshot（完整结果），之后只在实质性变化时推 "
                  "change，无变化时推 pulse。",
    KIND_TRADE: "交易通道已建立：委托、成交、账户、配置变化都会以事件推送（事件驱动，无定时）。",
}


def _num(v):
    """宽松转 float；None / bool / 非有限值（NaN、±inf）/ 无法解析一律返回 None。

    必须显式挡 NaN 与 inf：它们不是解析错误，但 `x or default` 里 NaN 是**真值**，
    默认值会被旁路掉，最终把 `{"capital": NaN}` 这种非法 JSON 推进链路
    （浏览器 JSON.parse 直接报错）。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        try:
            x = float(str(v).strip())
        except (TypeError, ValueError):
            return None
    return x if math.isfinite(x) else None


def _int(v, dflt, lo, hi):
    x = _num(v)
    if x is None:
        x = dflt
    try:
        iv = int(x)
    except (TypeError, ValueError, OverflowError):
        iv = int(dflt)
    return max(lo, min(hi, iv))


class Subscriber:
    """一个订阅者：有界队列 + 丢弃计数。

    队列满了丢**最旧**的：推送场景下「最新的行情」永远比「三秒前的行情」有价值，
    所以宁可丢头也不丢尾。
    """

    __slots__ = ("channel", "queue", "dropped", "created", "seq", "closed", "_lock",
                 "_size", "ready")

    def __init__(self, channel, size=MAX_QUEUE):
        self.channel = channel
        self.queue = deque()
        self.dropped = 0
        self.created = time.time()
        self.seq = 0
        self.closed = False
        self.ready = None
        self._lock = threading.Lock()
        self._size = max(1, int(size))

    def put(self, event):
        """投递一条事件；队列满则丢最旧并累加 dropped"""
        with self._lock:
            if self.closed:
                return False
            if len(self.queue) >= self._size:
                self.queue.popleft()
                self.dropped += 1
            self.queue.append(event)
        return True

    def get(self, timeout=None):
        """取一条事件；超时返回 None（供 SSE 处理器做心跳）"""
        deadline = time.time() + timeout if timeout else None
        while True:
            with self._lock:
                if self.queue:
                    return self.queue.popleft()
                if self.closed:
                    return None
            if deadline is not None and time.time() >= deadline:
                return None
            time.sleep(0.05)


class Channel:
    """一个推送主题。

    通道键由「类型 + 订阅参数」决定，因此**同一批标的的多个客户端共享同一个通道**，
    上游请求只发一次。参数变化会产生新通道，旧通道在订阅者归零后被回收。
    """

    def __init__(self, key, kind, params, interval):
        self.key = key
        self.kind = kind
        self.params = params
        self.interval = interval
        self.subs = []
        self.seq = 0
        self.ring = deque(maxlen=MAX_REPLAY)
        self.last_tick = 0.0
        self.last_ok = None
        self.last_error = None
        self.ticks = 0
        self.upstream = 0
        #: 本轮 tick 是否已经通过 _fail 记录过错误（否则 _tick_once 会把它清成 None）
        self.error_this_tick = False
        self.state = {}          # 通道内部状态（如上一次研判结果，用于差分）
        self._lock = threading.RLock()

    def due(self, now):
        return (now - self.last_tick) >= self.interval

    def next_event_id(self):
        with self._lock:
            self.seq += 1
            return "%d" % self.seq

    def add_sub(self, sub):
        with self._lock:
            self.subs.append(sub)

    def remove_sub(self, sub):
        with self._lock:
            if sub in self.subs:
                self.subs.remove(sub)
            sub.closed = True
        return len(self.subs)

    def empty(self):
        with self._lock:
            return not self.subs

    def publish(self, event, data, replay=True):
        """向所有订阅者投递；返回送达数。异常被吞掉：单个订阅者的问题不影响其他人。"""
        with self._lock:
            eid = self.next_event_id()
            item = {"id": eid, "event": event, "data": data, "ts": int(time.time() * 1000)}
            subs = list(self.subs)
            if replay:
                self.ring.append(item)
        sent = 0
        for sub in subs:
            try:
                if sub.put(item):
                    sent += 1
            except Exception:  # noqa: BLE001
                continue
        return sent

    def replay_since(self, last_id):
        """取回 id 大于 last_id 的缓存事件（客户端重连补发）。

        游标来自客户端可控的 ``Last-Event-ID``，因此**必须把非法值（非数字、负数）
        当作「无有效游标」返回空列表**：否则一个 ``-1`` 就能让每次重连都补发整段
        历史（最多 MAX_REPLAY 条），把重放变成常态噪声。
        """
        text = str(last_id).strip() if last_id is not None else ""
        if not text.isdigit():
            return []
        try:
            lid = int(text)
        except (TypeError, ValueError):
            return []
        with self._lock:
            return [it for it in list(self.ring)
                    if it["id"].isdigit() and int(it["id"]) > lid]

    def status(self):
        with self._lock:
            return {
                "key": self.key, "kind": self.kind, "interval": self.interval,
                "subscribers": len(self.subs), "ticks": self.ticks,
                "upstream": self.upstream, "lastTick": int(self.last_tick * 1000) or None,
                "lastError": self.last_error,
                "dropped": sum(s.dropped for s in self.subs),
                "symbols": self.params.get("symbols") or [],
            }


def _channel_key(kind, params):
    """通道键：把影响上游请求的参数拼进去，让同参订阅共享通道"""
    if kind == KIND_QUOTES:
        return "%s|%s|%s|%s" % (kind, params.get("market"),
                                ",".join(params.get("symbols") or []), params.get("interval"))
    if kind == KIND_ADVISOR:
        return "%s|%s|%s|%s|%s|%s|%s|%s" % (
            kind, params.get("market"), ",".join(params.get("symbols") or []),
            params.get("horizon"), params.get("capital"),
            params.get("kellyFraction"), params.get("maxWeight"), params.get("interval"))
    return "%s|%s" % (kind, params.get("market") or "all")


def parse_params(kind, raw):
    """把查询参数整理成通道参数（非法值一律回退默认，不抛异常）。

    ``raw`` 是已扁平化的 query dict（值为字符串）。symbols 支持逗号 / 空格 / 分号分隔，
    也支持 ``600519:贵州茅台`` 这种带名称的写法（名称只用于展示，不参与上游请求）。
    """
    raw = raw if isinstance(raw, dict) else {}
    market = str(raw.get("market") or "cn").strip().lower()
    market = "us" if market.startswith("us") else "cn"
    symbols = []
    seen = set()
    text = raw.get("symbols") or raw.get("codes") or ""
    if isinstance(text, (list, tuple)):
        text = ",".join(str(x) for x in text)
    for piece in str(text).replace(";", ",").replace(" ", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        code, _, name = piece.partition(":")
        code = code.strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        symbols.append({"code": code, "name": name.strip() or code})
        if len(symbols) >= MAX_SYMBOLS:
            break
    if kind == KIND_TRADE:
        # trade 是事件驱动的，没有节拍：这里如实报 0，而不是硬塞一个默认 30 秒，
        # 否则前端会在「事件驱动」的通道上显示「每 30 秒推送一次」，属于误导
        interval = 0
    else:
        lo, hi, dflt = INTERVAL_RANGE.get(kind, (5, 600, 30))
        interval = _int(raw.get("interval"), dflt, lo, hi)
    params = {"market": market, "symbols": [s["code"] for s in symbols],
              "names": {s["code"]: s["name"] for s in symbols},
              "interval": interval}
    if kind == KIND_ADVISOR:
        params.update({
            "horizon": _int(raw.get("horizon"), 20, 1, 250),
            "capital": _num(raw.get("capital")) or 100000.0,
            "kellyFraction": _num(raw.get("kellyFraction")) or 0.5,
            "maxWeight": _num(raw.get("maxWeight")) or 0.25,
        })
    return params


def diff_advice(prev_rows, new_rows):
    """比较两次研判，返回「实质性变化」列表与原因。

    只关心会影响决策的字段：档位、评分、凯利仓位、交易计划价位、预测概率与期望。
    **价格本身不算变化**（那是行情通道的职责），但会带在变化项里供前端就地更新。
    """
    old = {str(r.get("code")): r for r in (prev_rows or []) if isinstance(r, dict)}
    changes = []
    for row in new_rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code"))
        prev = old.get(code)
        reasons = []
        if prev is None:
            reasons.append("新增标的")
        else:
            if prev.get("action") != row.get("action"):
                reasons.append("档位 %s → %s" % (prev.get("action") or "—",
                                                row.get("action") or "—"))
            ps, ns = _num(prev.get("score")), _num(row.get("score"))
            if ps is not None and ns is not None and abs(ns - ps) >= DIFF_SCORE:
                reasons.append("评分 %.1f → %.1f" % (ps, ns))
            if bool(prev.get("ok")) != bool(row.get("ok")):
                reasons.append("数据可用性变化：%s → %s"
                               % ("正常" if prev.get("ok") else "异常",
                                  "正常" if row.get("ok") else "异常"))
            pp = _num(((prev.get("forecast") or {}).get("upProb")))
            np_ = _num(((row.get("forecast") or {}).get("upProb")))
            if pp is not None and np_ is not None and abs(np_ - pp) >= DIFF_PROB:
                reasons.append("上涨概率 %.0f%% → %.0f%%" % (pp * 100, np_ * 100))
            pr = _num(((prev.get("forecast") or {}).get("expectedReturn")))
            nr = _num(((row.get("forecast") or {}).get("expectedReturn")))
            if pr is not None and nr is not None and abs(nr - pr) >= DIFF_RETURN:
                reasons.append("期望收益 %.2f%% → %.2f%%" % (pr, nr))
            pw = _num(((prev.get("kelly") or {}).get("weight")))
            nw = _num(((row.get("kelly") or {}).get("weight")))
            if pw is not None and nw is not None and abs(nw - pw) >= DIFF_WEIGHT:
                reasons.append("凯利权重 %.1f%% → %.1f%%" % (pw * 100, nw * 100))
            p_plan = prev.get("plan") or {}
            n_plan = row.get("plan") or {}
            for key, label in (("entry", "入场"), ("stop", "止损"),
                               ("target1", "目标1"), ("target2", "目标2")):
                a, b = _num(p_plan.get(key)), _num(n_plan.get(key))
                if a is not None and b is not None and a != b:
                    reasons.append("%s %.4f → %.4f" % (label, a, b))
        if not reasons:
            continue
        kelly = row.get("kelly") or {}
        plan = row.get("plan") or {}
        forecast = row.get("forecast") or {}
        changes.append({
            "code": code, "name": row.get("name"), "market": row.get("market"),
            "action": row.get("action"), "actionText": row.get("actionText"),
            "prevAction": (prev or {}).get("action"),
            "score": row.get("score"), "confidence": row.get("confidence"),
            "price": row.get("price"), "changePct": row.get("changePct"),
            "kelly": {"weight": kelly.get("weight"), "amount": kelly.get("amount"),
                      "shares": kelly.get("shares")},
            "plan": {"entry": plan.get("entry"), "stop": plan.get("stop"),
                     "target1": plan.get("target1"), "target2": plan.get("target2")},
            "forecast": {"expectedReturn": forecast.get("expectedReturn"),
                         "upProb": forecast.get("upProb")},
            "reasons": reasons,
        })
    # 标的从列表里消失（例如上游取不到数据被剔除）也算变化
    new_codes = {str(r.get("code")) for r in (new_rows or []) if isinstance(r, dict)}
    for code, prev in old.items():
        if code not in new_codes:
            changes.append({"code": code, "name": prev.get("name"),
                            "market": prev.get("market"), "action": None,
                            "prevAction": prev.get("action"), "reasons": ["标的已从订阅中移除"]})
    return changes


class StreamHub:
    """推送中枢：通道管理 + 单刷新线程 + 上游限流。

    参数
    ----
    fetch_quotes : callable(market, codes) -> list[dict]
        批量报价（由 server 注入，避免本模块依赖任何网络实现）。
    recommend : callable(symbols, **params) -> dict | None
        AI 研判（同样由 server 注入）。为 None 时 advisor 通道不可用。
    publish_hook : callable(kind, event, data) -> None
        可选，事件发布后的旁路钩子（如把成交事件同时转发到 webhook）。
    """

    def __init__(self, fetch_quotes=None, recommend=None, publish_hook=None,
                 queue_size=MAX_QUEUE, min_upstream_gap=MIN_UPSTREAM_GAP,
                 clock=time.time):
        self.fetch_quotes = fetch_quotes
        self.recommend = recommend
        self.publish_hook = publish_hook
        self.queue_size = max(4, int(queue_size))
        self.min_upstream_gap = max(0.0, float(min_upstream_gap))
        self.clock = clock
        self.channels = {}
        self._lock = threading.RLock()
        self._up_lock = threading.Lock()
        self._last_upstream = 0.0
        self._thread = None
        self._stop = threading.Event()
        self.closed = False
        self.stats = {"upstream": 0, "published": 0, "throttled": 0,
                      "startedAt": int(time.time() * 1000)}

    # ------------------------------------------------------------ 订阅管理 --
    def available(self, kind):
        """通道类型当前是否可用（缺注入的抓取器时如实返回 False，而不是等运行时报错）"""
        if kind == KIND_QUOTES:
            return callable(self.fetch_quotes)
        if kind == KIND_ADVISOR:
            return callable(self.recommend)
        return kind == KIND_TRADE

    def _ready_payload(self, ch, replay_n=0):
        """订阅受理的确认帧（只发给这个订阅者，不进重放环）"""
        return {
            "channel": ch.key, "kind": ch.kind,
            "market": ch.params.get("market"), "symbols": ch.params.get("symbols") or [],
            "interval": ch.interval, "replayed": replay_n,
            "ts": int(time.time() * 1000),
            "note": READY_NOTE.get(ch.kind),
        }

    def subscribe(self, kind, params, last_event_id=None):
        """订阅一个通道：返回 (subscriber, replay_events)。

        同一批参数复用同一通道，因此多个浏览器标签页共享一次上游请求。
        订阅成功会**立刻**往该订阅者的队列里放一帧 ``ready``：前端要靠它把状态从
        「连接中」切到「已连接」，以及知道服务端实际生效的参数（interval 会被夹取）。
        ``ready`` 只发给这个订阅者、不进重放环 —— 它是「你这次订阅被受理了」，
        不是通道上的公共事件，重放给后来者没有意义。
        """
        if kind not in KINDS:
            raise ValueError("未知的推送通道：%s" % kind)
        if not self.available(kind):
            raise RuntimeError("通道不可用：%s（缺少注入的数据源）" % kind)
        params = params if isinstance(params, dict) else {}
        key = _channel_key(kind, params)
        now = self.clock()
        with self._lock:
            if self.closed:
                raise RuntimeError("推送中枢已关闭")
            ch = self.channels.get(key)
            if ch is None:
                interval = _int(params.get("interval"),
                                INTERVAL_RANGE.get(kind, (5, 600, 30))[2], 1, 3600)
                ch = Channel(key, kind, dict(params), interval)
                # 新通道立刻到期，避免首次推送白等一个 interval
                ch.last_tick = now - interval
                self.channels[key] = ch
            sub = Subscriber(ch, self.queue_size)
            ch.add_sub(sub)
            replay = ch.replay_since(last_event_id) if last_event_id else []
            # ready 帧挂在订阅者身上、**不进队列**：它是「你这次订阅被受理了」的
            # 连接级确认（含服务端实际生效的 interval），由 SSE 传输层写为第一帧。
            # 放进队列会污染「队列里全是通道事件」这个不变量，也会让重连时的补发
            # 逻辑与 ready 混在一起。
            sub.ready = self._ready_payload(ch, len(replay))
            self._ensure_thread()
        return sub, replay

    def unsubscribe(self, sub):
        if sub is None:
            return
        ch = getattr(sub, "channel", None)
        if ch is None:
            return
        with self._lock:
            left = ch.remove_sub(sub)
            if left <= 0 and self.channels.get(ch.key) is ch:
                del self.channels[ch.key]

    def _ensure_thread(self):
        """懒启动刷新线程（调用方需持有 _lock）"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="stream-hub", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- 发布 --
    def publish_trade(self, event, data):
        """向所有 trade 通道广播一条事件（下单/成交/账户/配置）"""
        sent = 0
        with self._lock:
            targets = [ch for ch in self.channels.values() if ch.kind == KIND_TRADE]
        for ch in targets:
            sent += ch.publish(event, data)
        if sent:
            self.stats["published"] += sent
        # 钩子**不看有没有浏览器订阅者**：外发对接（webhook / 外部撮合系统）与
        # 「是否有人开着页面」无关，没有订阅者也要照发
        if self.publish_hook is not None:
            try:
                self.publish_hook(KIND_TRADE, event, data)
            except Exception:  # noqa: BLE001
                pass
        return sent

    def publish(self, kind, event, data, channel_key=None):
        """向指定类型的全部通道（或指定通道）广播，供测试与外部驱动使用"""
        sent = 0
        with self._lock:
            targets = [ch for ch in self.channels.values()
                       if ch.kind == kind and (channel_key is None or ch.key == channel_key)]
        for ch in targets:
            sent += ch.publish(event, data)
        self.stats["published"] += sent
        return sent

    # ------------------------------------------------------------ 上游限流 --
    def _upstream_call(self, fn, *args, **kwargs):
        """全局最小间隔保护下的上游调用。

        注意：这里**先抢锁再等待**，会短暂阻塞刷新线程 —— 这是刻意的：上游限流是
        全局的，宁可让推送慢一点，也不要让多个通道并发打同一个公开接口。
        锁的持有时间只有「间隔差值 + 一次请求」，不会长期占用（请求本身有超时）。
        """
        with self._up_lock:
            gap = self.clock() - self._last_upstream
            if gap < self.min_upstream_gap:
                wait = self.min_upstream_gap - gap
                self.stats["throttled"] += 1
                if self._stop.wait(wait):
                    raise RuntimeError("推送中枢已关闭")
            try:
                return fn(*args, **kwargs)
            finally:
                self._last_upstream = self.clock()
                self.stats["upstream"] += 1

    def _fail(self, ch, message):
        """记下通道错误并推 error 事件。

        两者必须同时做：``lastError`` 的唯一用途就是回答「这条通道为什么没数据」，
        而「未订阅任何标的」「研判返回无效结果」这类**不抛异常**的失败路径曾经只推
        事件不写字段，于是 /api/stream/status 上显示一条「健康」通道 —— 最需要它的
        场景反而看不到原因。抛异常的路径由 _tick_once 统一写。
        """
        ch.last_error = str(message)[:200]
        ch.error_this_tick = True   # 让 _tick_once 的 else 分支不要把它清掉
        return self._publish(ch, "error", {"message": str(message)[:200],
                                          "ts": int(time.time() * 1000)})

    def _publish(self, ch, event, data):
        """通道内发布 + 全局计数（status 里的 published 要能反映真实推送量）"""
        sent = ch.publish(event, data)
        self.stats["published"] += sent
        return sent

    # ------------------------------------------------------------ 刷新线程 --
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick_once()
                self._reap()
            except Exception:  # noqa: BLE001  刷新线程绝不能因为单次异常退出
                pass
            self._stop.wait(TICK_STEP)

    def _tick_once(self):
        now = self.clock()
        with self._lock:
            if self.closed:
                return
            due = [ch for ch in self.channels.values() if ch.due(now)]
        for ch in due:
            if ch.empty() and ch.kind != KIND_TRADE:
                continue
            ch.last_tick = now
            ch.ticks += 1
            ch.error_this_tick = False
            try:
                if ch.kind == KIND_QUOTES:
                    self._tick_quotes(ch)
                elif ch.kind == KIND_ADVISOR:
                    self._tick_advisor(ch)
            except Exception as exc:  # noqa: BLE001
                ch.last_error = str(exc)[:200]
                self._publish(ch, "error", {"message": str(exc)[:200],
                                            "ts": int(time.time() * 1000)})
            else:
                # 只有本轮**都没出过错**才清空：_fail() 不抛异常，若这里无条件清空，
                # 「未订阅标的」「研判返回无效」这类失败反而在 /api/stream/status 上
                # 显示成健康通道（实测踩过）
                if not ch.error_this_tick:
                    ch.last_error = None
                ch.last_ok = int(time.time() * 1000)

    def _reap(self):
        """回收没有订阅者的通道；全部回收后线程自行退出（不常驻空转）"""
        with self._lock:
            dead = [k for k, ch in self.channels.items() if ch.empty()]
            for k in dead:
                del self.channels[k]
            if not self.channels:
                self._stop.set()
                self._thread = None

    def _tick_quotes(self, ch):
        codes = list(ch.params.get("symbols") or [])
        market = ch.params.get("market") or "cn"
        if not codes:
            self._fail(ch, "未订阅任何标的")
            return
        t0 = self.clock()
        rows = self._upstream_call(self.fetch_quotes, market, codes) or []
        by_code = {}
        for r in rows:
            if isinstance(r, dict) and r.get("code"):
                by_code[str(r["code"]).upper()] = r
        out, failed = [], []
        for code in codes:
            r = by_code.get(code)
            if not r:
                failed.append(code)
                continue
            out.append({
                "code": code, "name": r.get("name") or (ch.params.get("names") or {}).get(code, code),
                "market": market, "price": r.get("price"), "changePct": r.get("changePct"),
                "change": r.get("change"), "volume": r.get("volume"),
                "amount": r.get("amount"), "updated": r.get("updated"),
                "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
                "prevClose": r.get("prevClose"), "source": r.get("source"),
            })
        self._publish(ch, "quotes", {
            "ts": int(time.time() * 1000), "market": market, "interval": ch.interval,
            "rows": out, "failed": failed,
            "elapsedMs": int((self.clock() - t0) * 1000),
            "source": (out[0].get("source") if out else None),
            "degraded": bool(failed),
        })

    def _tick_advisor(self, ch):
        params = ch.params
        symbols = [{"code": c, "market": params.get("market"), "name": (params.get("names") or {}).get(c, c)}
                   for c in (params.get("symbols") or [])]
        if not symbols:
            self._fail(ch, "未订阅任何标的")
            return
        t0 = self.clock()
        res = self._upstream_call(
            self.recommend, symbols,
            market=params.get("market") or "cn",
            horizon=params.get("horizon"), capital=params.get("capital"),
            kelly_fraction=params.get("kellyFraction"), max_weight=params.get("maxWeight"))
        if not isinstance(res, dict):
            self._fail(ch, "研判未返回有效结果")
            return
        rows = [r for r in (res.get("rows") or []) if isinstance(r, dict)]
        prev = ch.state.get("rows")
        changes = diff_advice(prev, rows)
        ch.state["rows"] = rows
        ch.state["at"] = int(time.time() * 1000)
        elapsed = int((self.clock() - t0) * 1000)
        payload = {
            "ts": int(time.time() * 1000), "elapsedMs": elapsed,
            "checked": len(rows), "market": res.get("market"),
            "analyzed": res.get("analyzed"),
        }
        if prev is None:
            # 首次（或重连后）推完整快照，前端可据此直接渲染整张表
            self._publish(ch, "snapshot", dict(payload, result=res))
        elif changes:
            self._publish(ch, "change", dict(payload, changes=changes,
                                             portfolio=res.get("portfolio"),
                                             changed=len(changes)))
        else:
            self._publish(ch, "pulse", dict(payload, changed=0))

    # ---------------------------------------------------------------- 状态 --
    def status(self):
        with self._lock:
            chans = [ch.status() for ch in self.channels.values()]
            alive = bool(self._thread is not None and self._thread.is_alive())
        return {
            "ok": True,
            "running": alive,
            "subscribers": sum(c["subscribers"] for c in chans),
            "channels": sorted(chans, key=lambda c: c["key"]),
            "stats": dict(self.stats),
            "minUpstreamGap": self.min_upstream_gap,
            "queueSize": self.queue_size,
            "note": ("三条通道：quotes（行情，默认 3 秒）| advisor（研判变化，默认 30 秒，"
                     "仅实质性变化时推）| trade（委托与成交，事件驱动）。"
                     "同一批参数的多个客户端共享一次上游请求；慢客户端丢最旧事件并计入 dropped。"),
        }

    def close(self):
        """关闭中枢：停线程、关闭所有订阅者（进程退出时调用）"""
        with self._lock:
            self.closed = True
            self._stop.set()
            subs = [s for ch in self.channels.values() for s in ch.subs]
            self.channels = {}
        for sub in subs:
            sub.closed = True
