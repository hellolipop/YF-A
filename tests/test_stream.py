# -*- coding: utf-8 -*-
"""core.stream（实时推送中枢）的单元测试（仅标准库 unittest，可直接 python 运行）。

覆盖范围（12 个测试类 ↔ 需求的 13 组断言）
------------------------------------------
  1. TestParseParams        参数归一：市场 / 标的列表 / 间隔夹取 / 研判附加参数 / 脏输入；
  2. TestChannelLifecycle   通道复用、订阅者计数、归还回收、刷新线程自行退出；
  3. TestEventDispatch      事件名与 payload 契约（quotes / error / 顺序 / id / 心跳 / 真线程）；
  4. TestBackpressure       慢客户端丢最旧、dropped 计数、发布不阻塞、订阅者互不影响；
  5. TestReplay             有限重放：cursor 语义、MAX_REPLAY 上限、非法 cursor 不抛异常；
  6. TestDiffAdvice         研判差分的六类触发条件、阈值以下不触发、标的消失 / 可用性变化；
  7. TestAdvisorChannel     snapshot → pulse → change 状态机、单次上游故障不打死通道；
  8. TestUpstreamThrottle   全局最小间隔限流（stats.throttled / stats.upstream 自洽）；
  9. TestTradeAndHook       trade 广播五类事件、**没有订阅者也要走 publish_hook**；
 10. TestStatusContract     status() 字段契约、available() 与 subscribe 的失败语义、close()；
 11. TestConcurrency        3 个发布线程 + 1 个订阅抖动线程，检查不崩且计数自洽；
 12. TestKnownGaps          已发现但**不允许修改被测模块**的缺陷锚点（expectedFailure）。

为什么全部离线、无网络
--------------------
``StreamHub`` 的两个上游（``fetch_quotes`` / ``recommend``）是**依赖注入**的，本文件一律
注入 ``FakeQuotes`` / ``FakeAdvisor``：它们只记录调用参数并返回确定性数据，绝不联网。
`/api/stream` 的 SSE 端点也尚未接入，因此这里测的是纯内存的推送语义。

为什么用假时钟而不是 time.sleep
-------------------------------
tick 是否到期（``Channel.due``）、限流要等多久（``_upstream_call``）都只取决于
``hub.clock()`` 的返回值。若用默认的 ``time.time``，「第二轮 tick」就必须先真睡一个
interval（quotes 至少 1 秒、advisor 至少 10 秒），既慢又不稳定。因此除下面三处不得不
依赖真实时间的场景，全部用 ``FakeClock`` 推进时间：

  · 刷新线程自身的懒启动 / 自行退出（TestChannelLifecycle，短轮询等待，≤ 0.6 秒）；
  · SSE 心跳（``Subscriber.get(timeout=...)`` 在空队列上返回 None，0.05 秒级）；
  · 限流确实会「等」（TestUpstreamThrottle 里 0.2 / 0.15 秒的真实等待，用来区分
    「只计数不等待」的假实现）。

其余 tick 全部由测试手动调用 ``hub._tick_once()`` 驱动：这是唯一能让「第几次 tick 推了
什么」可复现的办法（后台线程按真实时间抢跑会让断言变成抽奖）。为避免后台线程同时抢跑，
需要手动驱动的用例用 ``no_refresh_thread()`` 临时屏蔽线程懒启动；线程本身的行为由
TestChannelLifecycle 与 TestEventDispatch 里的「真线程」用例覆盖。

运行方式（tests/ 下无 __init__.py，直接跑文件最稳）：
    python3 tests/test_stream.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

# 让 tests/ 目录之外的包（core）可被导入，兼容任意工作目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import stream as S  # noqa: E402  被测模块

KIND_QUOTES = S.KIND_QUOTES
KIND_ADVISOR = S.KIND_ADVISOR
KIND_TRADE = S.KIND_TRADE


# --------------------------------------------------------------------------- #
# 测试替身（确定性 + 记录调用，绝不联网）
# --------------------------------------------------------------------------- #
class FakeClock(object):
    """可控时钟：``hub.clock()`` 的全部读数都来自它。

    ``StreamHub`` 里所有「时间」都经由 clock，因此只要换掉 clock，tick 到期判断与
    限流窗口就完全由测试决定，不必真睡。
    """

    def __init__(self, t=1000.0):
        self.now = float(t)

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += float(dt)
        return self.now


class FakeQuotes(object):
    """假行情抓取器：记录 (market, codes) 调用参数，返回确定性报价。

    ``omit_name=True``（默认）刻意不返回 name，用于验证「上游没给名字时回退到
    parse_params 里的用户输入名 / 代码本身」这条兜底路径；
    ``fail`` 里的代码不返回行，用于触发 ``failed`` / ``degraded``；
    ``bad_rows`` 用来往响应里掺脏行（None / 缺 code / 非 dict），验证上游脏数据只被跳过。
    """

    def __init__(self, omit_name=True, bad_rows=(), fail=(), price=10.0):
        self.calls = []
        self.omit_name = omit_name
        self.bad_rows = tuple(bad_rows)
        self.fail = set(fail)
        self.price = float(price)

    def __call__(self, market, codes):
        self.calls.append((market, list(codes)))
        rows = list(self.bad_rows)
        for i, code in enumerate(codes):
            if code in self.fail:
                continue
            row = {"code": code, "price": self.price + i,
                   "changePct": round(0.5 * (i + 1), 2), "change": 0.1 * (i + 1),
                   "volume": 1000 + i, "amount": 1.0e6, "updated": 1700000000000,
                   "open": self.price, "high": self.price + 1, "low": self.price - 1,
                   "prevClose": self.price - 0.5, "source": "fake"}
            if not self.omit_name:
                row["name"] = "行情名" + code
            rows.append(row)
        return rows


_UNSET = object()


class FakeAdvisor(object):
    """假研判器：返回确定性结果，可切换成「抛异常」或「返回非 dict」验证故障隔离。

    每次调用都返回**新的** dict 与新的 rows 副本：通道差分必须基于「值」而不是
    「对象身份」，否则每次 tick 都会因为新对象被误判成「有变化」。
    """

    def __init__(self, rows=(), portfolio=None, error=None, result=_UNSET):
        self.rows = [dict(r) for r in rows]
        self.portfolio = dict(portfolio if portfolio is not None
                              else {"cash": 1000.0, "invested": 2000.0, "positions": 1})
        self.error = error
        self.result = result
        self.calls = []

    def __call__(self, symbols, **kwargs):
        self.calls.append(([dict(s) for s in symbols], dict(kwargs)))
        if self.error:
            raise RuntimeError(self.error)
        if self.result is not _UNSET:
            return self.result
        rows = [dict(r) for r in self.rows]
        return {"ok": True, "market": kwargs.get("market"), "horizon": kwargs.get("horizon"),
                "capital": kwargs.get("capital"), "model": "fake-advisor",
                "updated": 1700000000000, "disclaimer": "合成数据，不构成投资建议",
                "rows": rows, "count": len(rows),
                "analyzed": sum(1 for r in rows if r.get("ok")),
                "portfolio": dict(self.portfolio)}


def advice_row(code="600519", name="贵州茅台", action="buy", score=60.0, ok=True,
               upProb=0.5, expected=2.0, weight=0.25, plan=None, market="cn"):
    """一行研判结果（只保留 diff_advice 关心的字段 + 前端渲染要用的字段）。

    数值刻意选 0.25 / 0.5 / 2.0 这类「二进制友好」的值：阈值比较都是浮点减法，
    基准值选得干净，才能确定「摆动 4.9 不触发、5.0 触发」这类边界真的在测阈值。
    """
    plan = dict(plan if plan is not None
                else {"entry": 10.0, "stop": 9.0, "target1": 12.0, "target2": 13.0})
    return {"code": code, "name": name, "market": market, "action": action,
            "actionText": {"buy": "建议买入", "hold": "持有观察", "sell": "建议回避"}.get(action, action),
            "score": score, "ok": ok, "confidence": 0.7, "price": 10.0, "changePct": 1.0,
            "kelly": {"weight": weight, "amount": 10000.0, "shares": 1000},
            "plan": plan, "forecast": {"upProb": upProb, "expectedReturn": expected}}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
@contextmanager
def no_refresh_thread(hub):
    """临时屏蔽刷新线程的懒启动：让 tick 完全由测试驱动（假时钟 + 手动 _tick_once）。

    为什么需要：``subscribe()`` 会启动后台线程，它按**真实时间**在任意时刻抢跑并调用
    ``_tick_once()``，使「第几次 tick 推了什么」「上游被调用几次」这类断言不确定。
    ``_tick_once`` 是 ``_loop`` 里唯一做业务判断的地方（``_reap`` 只负责回收），
    因此手动调用它与线程自然到期在行为上等价，只是时间点由 FakeClock 说了算。
    线程自身的行为另有「真线程」用例覆盖。
    """
    with mock.patch.object(hub, "_ensure_thread", lambda: None):
        yield hub


def tick(hub, clock=None, seconds=None):
    """（可选推进假时钟后）手动跑一轮刷新。

    等价于刷新线程到期后的那一次 ``_tick_once()``：到期判断用的就是 ``hub.clock()``。
    """
    if clock is not None and seconds is not None:
        clock.advance(seconds)
    hub._tick_once()


def take_events(sub):
    """取出订阅者队列里的全部事件并清空队列（返回快照）。

    为什么直接读 ``queue`` 而不是用 ``get()``：``get()`` 在空队列上会按 0.05 秒轮询
    直到超时，而这里要的是「此刻队列里有什么」。``get()`` 的顺序与心跳语义另有用例覆盖。
    """
    out = list(sub.queue)
    sub.queue.clear()
    return out


def names(events):
    return [e["event"] for e in events]


def wait_until(pred, timeout=0.6, step=0.02):
    """轮询等待条件成立（只在验证「刷新线程自身行为」时使用）。

    线程退出发生在**下一轮** ``_reap()``（最长等一个 TICK_STEP = 0.25 秒），
    所以这里必须等而不是直接断言；命中即返回，正常情况 0.25 秒内成立。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


# --------------------------------------------------------------------------- #
# 1. 查询参数归一
# --------------------------------------------------------------------------- #
class TestParseParams(unittest.TestCase):
    """查询参数归一：任何用户输入都要变成「可用的通道参数」，非法值回退默认。"""

    def test_market_is_normalized_to_cn_or_us(self):
        """market 只有 cn / us 两种取值：以 us 开头（不分大小写）算美股，其余回落 A 股。

        为什么这样断言：通道键里带 market。若 'US'/'us'/'USA' 被当成三种市场，
        同一批标的会被拆成多条通道、上游请求成倍增加 —— 那正是 hub 要消灭的浪费。
        """
        for given, expect in (("cn", "cn"), ("CN", "cn"), ("", "cn"), (None, "cn"),
                              ("us", "us"), ("US", "us"), ("Us", "us"), ("usa", "us"),
                              ("hk", "cn"), ("jp", "cn"), ("600519", "cn")):
            got = S.parse_params(KIND_QUOTES, {"market": given})["market"]
            self.assertEqual(got, expect, "market=%r" % (given,))

    def test_symbols_accept_comma_space_and_semicolon(self):
        """三种分隔符都要认：用户从表格 / 聊天窗口粘出来的分隔符是不受控的。"""
        for raw_text in ("600519,000001", "600519 000001", "600519;000001",
                         "600519, 000001", "600519;;000001", "  600519,000001  "):
            got = S.parse_params(KIND_QUOTES, {"symbols": raw_text})["symbols"]
            self.assertEqual(got, ["600519", "000001"], "symbols=%r" % (raw_text,))

    def test_symbol_names_are_display_only(self):
        """``600519:贵州茅台`` 的冒号后半段只做展示；通道参数里的 symbols 只留代码。

        为什么这样断言：通道键由 symbols 拼成。若把名字也拼进去，「600519」与
        「600519:贵州茅台」会变成两条通道，同一次上游请求白翻一倍。
        """
        p = S.parse_params(KIND_QUOTES, {"symbols": "600519:贵州茅台,000001:平安银行"})
        self.assertEqual(p["symbols"], ["600519", "000001"])
        self.assertEqual(p["names"]["600519"], "贵州茅台")
        self.assertEqual(p["names"]["000001"], "平安银行")
        # 没写名字 / 冒号后为空：用代码兜底，前端不用再判空
        self.assertEqual(S.parse_params(KIND_QUOTES, {"symbols": "600519"})["names"]["600519"],
                         "600519")
        self.assertEqual(S.parse_params(KIND_QUOTES, {"symbols": "600519:"})["names"]["600519"],
                         "600519")
        # 只有冒号（没有代码）的条目整条跳过：空代码没法查，也不该把名字当成代码去查
        self.assertEqual(S.parse_params(KIND_QUOTES, {"symbols": ":600519"})["symbols"], [])
        self.assertEqual(S.parse_params(KIND_QUOTES, {"symbols": ":600519,000001"})["symbols"],
                         ["000001"])

    def test_symbols_are_upper_cased_and_deduplicated(self):
        """大小写归一后去重：否则 'aapl' 与 'AAPL' 会被当成两只标的重复请求上游。"""
        p = S.parse_params(KIND_QUOTES, {"symbols": "aapl,AAPL,Aapl,600519,600519"})
        self.assertEqual(p["symbols"], ["AAPL", "600519"])
        # 名称取「第一次出现」的那份，保持稳定（前端按订阅顺序渲染，不能乱跳）
        self.assertEqual(p["names"]["AAPL"], "AAPL")

    def test_symbols_truncated_at_max_symbols_keeping_order(self):
        """超过 MAX_SYMBOLS 截断而不是报错：上游批量接口有上限，超了只会整批失败。"""
        codes = ["%06d" % (600000 + i) for i in range(S.MAX_SYMBOLS + 10)]
        p = S.parse_params(KIND_QUOTES, {"symbols": ",".join(codes)})
        self.assertEqual(len(p["symbols"]), S.MAX_SYMBOLS)
        self.assertEqual(p["symbols"], codes[:S.MAX_SYMBOLS], "截断必须保序（保留最前面的）")
        self.assertEqual(sorted(p["names"]), sorted(codes[:S.MAX_SYMBOLS]))

    def test_symbols_accept_sequence_and_codes_alias(self):
        """list/tuple 直接传入也要能解析；``codes`` 是 ``symbols`` 的同义参数名。"""
        self.assertEqual(S.parse_params(KIND_QUOTES, {"symbols": ["600519", "000001"]})["symbols"],
                         ["600519", "000001"])
        self.assertEqual(S.parse_params(KIND_QUOTES, {"symbols": ("600519", "000001")})["symbols"],
                         ["600519", "000001"])
        self.assertEqual(S.parse_params(KIND_QUOTES, {"codes": "600519,000001"})["symbols"],
                         ["600519", "000001"])

    def test_quotes_interval_is_clamped(self):
        """quotes：1..60 秒、默认 3 秒。太快打爆公开接口，太慢就不是「实时」了。"""
        self.assertEqual(S.INTERVAL_RANGE[KIND_QUOTES], (1, 60, 3))
        for given, expect in (("1", 1), (1, 1), ("60", 60), ("0", 1), ("-5", 1),
                              ("61", 60), ("999", 60), ("3.9", 3), ("x", 3),
                              (None, 3), ("", 3), (True, 3)):
            got = S.parse_params(KIND_QUOTES, {"interval": given})["interval"]
            self.assertEqual(got, expect, "interval=%r" % (given,))

    def test_advisor_interval_is_clamped(self):
        """advisor：10..600 秒、默认 30 秒。研判很贵，10 秒以内属于压榨上游。"""
        self.assertEqual(S.INTERVAL_RANGE[KIND_ADVISOR], (10, 600, 30))
        for given, expect in (("10", 10), (10, 10), ("600", 600), ("5", 10), ("9999", 600),
                              ("x", 30), (None, 30), ("-1", 10)):
            got = S.parse_params(KIND_ADVISOR, {"interval": given})["interval"]
            self.assertEqual(got, expect, "interval=%r" % (given,))

    def test_advisor_extra_params_fall_back_to_defaults(self):
        """horizon/capital/kellyFraction/maxWeight 缺失时回退默认，类型必须是数值。

        为什么这样断言：这四项直接透传给 core.advisor.recommend，也参与通道键。
        默认值必须唯一且稳定，否则「同一批标的」会因为参数差异被拆成多条通道。
        """
        p = S.parse_params(KIND_ADVISOR, {"symbols": "600519"})
        self.assertEqual((p["horizon"], p["capital"], p["kellyFraction"], p["maxWeight"]),
                         (20, 100000.0, 0.5, 0.25))
        # 显式给值要采纳（query 里全是字符串，必须认字符串数字）
        p2 = S.parse_params(KIND_ADVISOR, {"symbols": "600519", "horizon": "5",
                                           "capital": "50000", "kellyFraction": "0.3",
                                           "maxWeight": "0.4"})
        self.assertEqual((p2["horizon"], p2["capital"], p2["kellyFraction"], p2["maxWeight"]),
                         (5, 50000.0, 0.3, 0.4))
        # horizon 有上下界 1..250
        self.assertEqual(S.parse_params(KIND_ADVISOR, {"horizon": "0"})["horizon"], 1)
        self.assertEqual(S.parse_params(KIND_ADVISOR, {"horizon": "999"})["horizon"], 250)
        # 0 对这三项视为「没给」：0 本金 / 0 仓位没有意义，与 core.advisor 的 DEFAULT_* 口径一致
        p3 = S.parse_params(KIND_ADVISOR, {"capital": 0, "kellyFraction": 0, "maxWeight": 0})
        self.assertEqual((p3["capital"], p3["kellyFraction"], p3["maxWeight"]),
                         (100000.0, 0.5, 0.25))

    def test_quotes_params_carry_no_advisor_extras(self):
        """quotes 不该混进研判参数：通道键里没有它们，带上只会制造「看起来不同」的键。"""
        p = S.parse_params(KIND_QUOTES, {"horizon": "5", "capital": "1", "maxWeight": "0.1"})
        for key in ("horizon", "capital", "kellyFraction", "maxWeight"):
            self.assertNotIn(key, p)
        self.assertEqual(sorted(p), ["interval", "market", "names", "symbols"])

    def test_dirty_values_fall_back_without_raising(self):
        """脏输入必须「回退默认」而不是抛异常：query 完全由用户控制，抛异常等于 500。

        这里把 None / 数字 / 布尔 / list / 嵌套 dict / 超长字符串 / 只剩分隔符都过一遍，
        只要求两点：不抛异常 + 返回值仍是能进通道键、能过 json.dumps 的合法结构。
        """
        dirty = [None, 0, 1, 3.14, True, False, [], {}, ["600519"], [None, 600519],
                 "6" * 10000, ",".join(["6"] * 5000), "600519:", ":600519",
                 ";;;, ,", "  ", {"600519": "茅台"}, object()]
        for raw in dirty:
            with self.subTest(symbols=repr(raw)[:40]):
                params = S.parse_params(KIND_QUOTES, {"symbols": raw, "interval": raw,
                                                      "market": raw, "codes": raw})
                self.assertIsInstance(params["market"], str)
                self.assertIsInstance(params["symbols"], list)
                self.assertLessEqual(len(params["symbols"]), S.MAX_SYMBOLS)
                self.assertIsInstance(params["interval"], int)
                self.assertTrue(1 <= params["interval"] <= 60)
                json.dumps(params)  # 通道参数会出现在 /api/stream/status 里

    def test_dirty_container_is_only_tolerated_for_none(self):
        """raw 为 None / 空 dict 时按「没给参数」处理（``raw = raw or {}``）。

        注意：非 dict 容器（字符串 / list / 数字）会抛 AttributeError，见 TestKnownGaps 的第 5 条
        （hub.subscribe 自己用 isinstance 兜住了，所以不影响推送链路）。
        """
        for raw in (None, {}, {"symbols": None}, {"symbols": "", "interval": ""}):
            with self.subTest(raw=repr(raw)):
                p = S.parse_params(KIND_QUOTES, raw)
                self.assertEqual(p["market"], "cn")
                self.assertEqual(p["symbols"], [])
                self.assertEqual(p["interval"], 3)

    def test_fullwidth_punctuation_is_not_split(self):
        """现状（见回复的「观察」）：全角逗号/分号不会被拆开，整串会变成一个「代码」。

        对中文用户来说从聊天记录里复制粘贴全角标点是常见操作；这里如实钉住当前行为，
        以免有人误以为已经做了全角归一 —— 上游查不到这些代码时只会进 failed 列表。
        """
        p = S.parse_params(KIND_QUOTES, {"symbols": "600519，000001"})
        self.assertEqual(len(p["symbols"]), 1)
        self.assertIn("600519", p["symbols"][0])   # 代码里混进了全角逗号


# --------------------------------------------------------------------------- #
# 2. 通道复用、回收与线程生命周期
# --------------------------------------------------------------------------- #
class TestChannelLifecycle(unittest.TestCase):
    """通道复用与回收：hub 存在的意义就是「同一批标的只打一次上游」。"""

    def setUp(self):
        self.feed = FakeQuotes()
        # min_upstream_gap=0：本组关心的是通道与线程，不想为限流真等
        self.hub = S.StreamHub(fetch_quotes=self.feed, min_upstream_gap=0.0)
        self.addCleanup(self.hub.close)

    def test_same_params_share_one_channel(self):
        """同一组参数的两次 subscribe 必须落到同一个 Channel。

        为什么这样断言：这是整个 hub 的核心承诺 —— 10 个浏览器标签页订阅同一批标的，
        上游只被请求一次。一旦通道键不稳定（例如字符串 '5' 与数字 5 被当成两种），
        承诺立刻失效，而失效是静默的（只是请求变多），靠人工看不出来。
        """
        p1 = S.parse_params(KIND_QUOTES, {"symbols": "600519,000001", "interval": "5"})
        p2 = S.parse_params(KIND_QUOTES, {"symbols": "600519,000001", "interval": 5})
        sub1, r1 = self.hub.subscribe(KIND_QUOTES, p1)
        sub2, r2 = self.hub.subscribe(KIND_QUOTES, p2)
        self.assertEqual((r1, r2), ([], []))
        self.assertIs(sub1.channel, sub2.channel)
        self.assertEqual(len(self.hub.channels), 1)
        self.assertEqual(len(sub1.channel.subs), 2)
        self.assertEqual(self.hub.status()["subscribers"], 2)
        self.assertEqual(sub1.channel.interval, 5)
        self.assertEqual(sub1.channel.params["symbols"], ["600519", "000001"])
        # 第三个订阅者走同一批参数，仍然是同一条通道
        sub3, _ = self.hub.subscribe(KIND_QUOTES, dict(p1))
        self.assertIs(sub3.channel, sub1.channel)
        self.assertEqual(len(self.hub.channels), 1)

    def test_different_params_create_different_channels(self):
        """参数只要影响上游请求（间隔 / 标的集 / 市场），就必须是新通道。"""
        variants = [{"symbols": "600519", "interval": "5"},
                    {"symbols": "600519", "interval": "5"},          # 同参 → 复用
                    {"symbols": "600519", "interval": "6"},          # 间隔不同
                    {"symbols": "600519,000001", "interval": "5"},   # 标的集不同
                    {"symbols": "600519", "interval": "5", "market": "us"}]  # 市场不同
        subs = [self.hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES, v))[0]
                for v in variants]
        self.assertIs(subs[0].channel, subs[1].channel)
        # 5 次订阅 → 4 条通道（前两个变体同参），订阅者总数 5
        self.assertEqual(len(self.hub.channels), 4)
        self.assertEqual(self.hub.status()["subscribers"], 5)
        self.assertEqual(subs[2].channel.interval, 6)
        self.assertEqual(subs[4].channel.params["market"], "us")

    def test_unsubscribe_recycles_channel_and_stops_thread(self):
        """订阅者归零 → 通道被回收 → 没有通道时刷新线程自行退出（不常驻空转）。

        为什么必须等：``unsubscribe`` 立刻把通道从 ``hub.channels`` 摘掉，但线程退出发生在
        **下一轮** ``_reap()``（最长等一个 TICK_STEP = 0.25 秒），所以这里轮询等待。
        另外还要证明线程真的停了：再跨过一个 TICK_STEP，上游调用次数不能继续增长。
        """
        p = S.parse_params(KIND_QUOTES, {"symbols": "600519", "interval": "1"})
        sub1, _ = self.hub.subscribe(KIND_QUOTES, p)
        sub2, _ = self.hub.subscribe(KIND_QUOTES, p)
        self.assertTrue(wait_until(lambda: self.hub.status()["running"]), "订阅后线程应已启动")
        self.assertEqual(len(self.hub.channels), 1)
        self.assertIs(sub1.channel, sub2.channel)

        self.hub.unsubscribe(sub1)
        self.assertEqual(len(self.hub.channels), 1, "还有一个订阅者时通道不能被回收")
        self.assertTrue(sub1.closed, "归还后的订阅者要标记 closed，避免继续入队造成内存滞留")
        self.assertFalse(sub2.closed)
        self.assertEqual(self.hub.status()["channels"][0]["subscribers"], 1)

        self.hub.unsubscribe(sub2)
        self.assertEqual(self.hub.channels, {})
        self.assertEqual(self.hub.status()["subscribers"], 0)
        self.assertTrue(wait_until(lambda: self.hub.status()["running"] is False),
                        "没有通道后刷新线程必须自行退出")
        self.assertIsNone(self.hub._thread)
        called = len(self.feed.calls)
        # 唯一一次「跨过一个 TICK_STEP」的等待（0.28 秒 > 0.25），证明线程不再 tick：
        # 否则每条空转的通道都会周期性打上游，工具会变成公开接口的压力源。
        time.sleep(0.28)
        self.assertEqual(len(self.feed.calls), called, "线程退出后不该再有上游请求")

    def test_unsubscribed_client_stops_receiving(self):
        """归还的订阅者必须真的收不到事件（否则慢客户端的队列会一直胀着）。"""
        p = S.parse_params(KIND_QUOTES, {"symbols": "600519", "interval": "10"})
        with no_refresh_thread(self.hub):
            sub_a, _ = self.hub.subscribe(KIND_QUOTES, p)
            sub_b, _ = self.hub.subscribe(KIND_QUOTES, p)
            self.assertEqual(self.hub.publish(KIND_QUOTES, "quotes", {"n": 1}), 2)
            self.hub.unsubscribe(sub_a)
            self.assertEqual(self.hub.publish(KIND_QUOTES, "quotes", {"n": 2}), 1)
        self.assertEqual([e["data"]["n"] for e in take_events(sub_a)], [1])
        self.assertEqual([e["data"]["n"] for e in take_events(sub_b)], [1, 2])
        self.assertEqual(sub_a.dropped, 0)
        # 归还后再归还 / 归还 None / 归还「别人的」订阅者都不能抛异常
        self.hub.unsubscribe(sub_a)
        self.hub.unsubscribe(None)
        self.hub.unsubscribe(object())

    def test_subscriber_get_returns_none_when_closed(self):
        """已关闭且空的队列，get() 必须立刻返回 None（SSE 处理器靠它收尾，不能死等）。"""
        p = S.parse_params(KIND_QUOTES, {"symbols": "600519", "interval": "10"})
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, p)
            self.hub.unsubscribe(sub)
        t0 = time.time()
        self.assertIsNone(sub.get(timeout=0.2))
        self.assertLess(time.time() - t0, 0.05, "关闭的订阅者不该走轮询等待")


# --------------------------------------------------------------------------- #
# 3. 事件分发（事件名 + payload 契约）
# --------------------------------------------------------------------------- #
class TestEventDispatch(unittest.TestCase):
    """事件名与 payload 形状：这就是前端 EventSource 的输入，字段少了就是白屏。"""

    def setUp(self):
        self.clock = FakeClock()
        self.feed = FakeQuotes()
        self.hub = S.StreamHub(fetch_quotes=self.feed, min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(self.hub.close)
        self.params = S.parse_params(KIND_QUOTES, {"symbols": "600519:贵州茅台,000001",
                                                   "interval": "3"})

    def test_quotes_event_payload_contract(self):
        """quotes 事件必须带 rows/failed/degraded/elapsedMs：前端靠这四个字段渲染整表。

        为什么逐个字段断言：degraded 决定页面是否显示「部分标的取数失败」，
        failed 决定是哪几行，elapsedMs 用于展示上游耗时（排障时第一眼就看它）。
        少任何一个，前端不会报错，只会静默显示成「一切正常」。
        """
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            tick(self.hub)
        evs = take_events(sub)
        self.assertEqual(names(evs), ["quotes"])
        data = evs[0]["data"]
        self.assertEqual(sorted(data), ["degraded", "elapsedMs", "failed", "interval",
                                        "market", "rows", "source", "ts"])
        self.assertEqual(data["market"], "cn")
        self.assertEqual(data["interval"], 3)
        self.assertFalse(data["degraded"])
        self.assertEqual(data["failed"], [])
        self.assertIsInstance(data["elapsedMs"], int)
        self.assertGreaterEqual(data["elapsedMs"], 0)
        self.assertEqual(data["source"], "fake")
        self.assertIsInstance(data["ts"], int)
        self.assertGreater(data["ts"], 0)
        # 订阅顺序 = rows 顺序（前端表格不能因为上游返回顺序而跳动）
        self.assertEqual([r["code"] for r in data["rows"]], ["600519", "000001"])
        # 上游没给 name 时回退到 parse_params 里的用户输入名 / 代码本身
        self.assertEqual([r["name"] for r in data["rows"]], ["贵州茅台", "000001"])
        self.assertEqual(data["rows"][0]["price"], 10.0)
        self.assertEqual(data["rows"][1]["price"], 11.0)
        self.assertEqual(self.feed.calls, [("cn", ["600519", "000001"])])
        self.assertEqual(self.hub.stats["upstream"], 1)

    def test_upstream_name_wins_over_query_name(self):
        """上游给了 name 就用上游的（行情源的名称更权威），用户输入名只作兜底。"""
        feed = FakeQuotes(omit_name=False)
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_QUOTES, self.params)
            tick(hub)
        self.assertEqual([r["name"] for r in take_events(sub)[0]["data"]["rows"]],
                         ["行情名600519", "行情名000001"])

    def test_degraded_flag_when_some_symbols_are_missing(self):
        """部分标的取不到 → 进 failed、degraded=True，但**其余行照常推送**。

        为什么这样断言：一次批量请求里个别标的不存在是常态（退市 / 代码写错）。
        如果整批作废，用户会以为是「行情坏了」；正确行为是标出缺口、其余照常。
        """
        feed = FakeQuotes(fail=("000001",))
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_QUOTES, self.params)
            tick(hub)
        data = take_events(sub)[0]["data"]
        self.assertEqual(data["failed"], ["000001"])
        self.assertTrue(data["degraded"])
        self.assertEqual([r["code"] for r in data["rows"]], ["600519"])

    def test_junk_rows_from_upstream_are_skipped(self):
        """上游响应里的脏行（None / 非 dict / 缺 code / 空 code）只被跳过，不能打挂推送。

        为什么这样断言：报价接口偶尔会返回 null 或结构变形的行；一旦这里抛异常，
        ``_tick_once`` 会把整轮变成 error 事件，用户看到的是「行情坏了」而不是「一行脏数据」。
        """
        feed = FakeQuotes(bad_rows=(None, "600519", 0, {"price": 1.0}, {"code": ""},
                                    {"code": "600519", "price": 99.0}))
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_QUOTES, self.params)
            tick(hub)
        evs = take_events(sub)
        self.assertEqual(names(evs), ["quotes"])
        self.assertEqual(len(evs[0]["data"]["rows"]), 2)

    def test_upstream_code_case_is_normalized(self):
        """上游把代码写成小写（``sh600519`` / ``aapl``）时仍要能匹配上订阅的代码。

        为什么这样断言：匹配失败会静默变成「failed」，用户看到的是「取不到行情」，
        而实际上数据拿到了、只是大小写不一致。
        """
        feed = lambda market, codes: [{"code": "sh600519", "price": 7.0, "source": "fake"}]
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_QUOTES, S.parse_params(
                KIND_QUOTES, {"symbols": "SH600519", "interval": "3"}))
            tick(hub)
        data = take_events(sub)[0]["data"]
        self.assertEqual(data["failed"], [])
        self.assertEqual(data["rows"][0]["price"], 7.0)

    def test_error_event_when_no_symbol_is_subscribed(self):
        """没订阅任何标的时不推空表，而是推 error 并说明原因（前端才能提示「请选标的」）。"""
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, S.parse_params(
                KIND_QUOTES, {"symbols": "", "interval": "3"}))
            tick(self.hub)
        evs = take_events(sub)
        self.assertEqual(names(evs), ["error"])
        self.assertEqual(evs[0]["data"]["message"], "未订阅任何标的")
        self.assertIsInstance(evs[0]["data"]["ts"], int)
        self.assertEqual(self.feed.calls, [], "没有标的时不该打上游")

    def test_event_ids_are_numeric_strings_and_delivered_in_order(self):
        """事件 id 是「字符串数字」且只增不减：EventSource 的 Last-Event-ID 依赖它。

        为什么断言 id 递增而不是「非空」：重放的判断是 ``int(id) > last_id``，
        一旦 id 变成 UUID / 时间戳字符串，重放会静默地一条都补不回来。
        这里用 ``sub.get()`` 逐个取，顺便验证 SSE 处理器拿到的是同一批结构。
        """
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            for i in range(1, 6):
                self.hub.publish(KIND_QUOTES, "quotes", {"n": i})
            got = [sub.get(timeout=0.2) for _ in range(5)]
            self.assertIsNone(sub.get(timeout=0.05), "队列取空后应超时返回 None")
        self.assertEqual([e["id"] for e in got], ["1", "2", "3", "4", "5"])
        self.assertTrue(all(isinstance(e["id"], str) and e["id"].isdigit() for e in got))
        self.assertEqual([e["data"]["n"] for e in got], [1, 2, 3, 4, 5])
        for e in got:
            self.assertEqual(sorted(e), ["data", "event", "id", "ts"])
            self.assertEqual(e["event"], "quotes")

    def test_get_times_out_on_empty_queue_for_heartbeat(self):
        """空队列上 get(timeout=...) 必须超时返回 None：SSE 处理器靠它发心跳注释行。

        为什么断言「不抛异常且返回 None」：如果 get 在超时时抛异常，或者在没有事件时
        死等，长连接就会被浏览器判死或永久占住一个线程。
        """
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            t0 = time.time()
            self.assertIsNone(sub.get(timeout=0.05))
            waited = time.time() - t0
            self.assertGreaterEqual(waited, 0.05)
            self.assertLess(waited, 0.3)
            # 超时一次之后仍然可用：来事件就能取到
            self.hub.publish(KIND_QUOTES, "quotes", {"n": 1})
            self.assertEqual(sub.get(timeout=0.2)["data"]["n"], 1)

    def test_live_refresh_thread_pushes_quotes(self):
        """真线程（默认 clock=time.time）端到端验证：懒启动 → 到期 → 上游 → 推送。

        为什么还要这条：上面所有用例都屏蔽了后台线程（手动 tick），
        但「线程真的会自己跑」这件事没人验证过 —— 这条用例用轮询等待（≤ 0.6 秒，
        正常情况下一个 TICK_STEP = 0.25 秒内完成）把这条链路补上。
        """
        feed = FakeQuotes()
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=0.0)
        self.addCleanup(hub.close)
        sub, _ = hub.subscribe(KIND_QUOTES, S.parse_params(
            KIND_QUOTES, {"symbols": "600519", "interval": "3"}))
        self.assertTrue(wait_until(lambda: len(sub.queue) >= 1), "刷新线程应自行推来行情")
        evs = take_events(sub)
        self.assertEqual(names(evs), ["quotes"])
        self.assertEqual([r["code"] for r in evs[0]["data"]["rows"]], ["600519"])
        self.assertTrue(hub.status()["running"])
        self.assertEqual(len(feed.calls), 1)


# --------------------------------------------------------------------------- #
# 4. 背压
# --------------------------------------------------------------------------- #
class TestBackpressure(unittest.TestCase):
    """慢客户端不能拖死发布：丢最旧 + dropped 计数 + 发布方永不阻塞。"""

    def setUp(self):
        self.clock = FakeClock()
        self.hub = S.StreamHub(fetch_quotes=FakeQuotes(), queue_size=4,
                              min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(self.hub.close)
        self.params = S.parse_params(KIND_QUOTES, {"symbols": "600519", "interval": "10"})

    def test_queue_keeps_newest_and_counts_dropped(self):
        """队列满时丢**最旧**的：推送场景下「最新行情」永远比「三秒前的行情」有价值。

        queue_size=4（hub 的下限就是 4）连发 10 条 → 队列只剩最新 4 条、dropped=6，
        且 publish 的返回值仍是「送达订阅者数」（发生丢弃不等于发送失败）。
        """
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            for i in range(1, 11):
                self.assertEqual(self.hub.publish(KIND_QUOTES, "quotes", {"n": i}), 1)
            self.assertEqual(len(sub.queue), 4)
            self.assertEqual(sub.dropped, 6)
            evs = take_events(sub)
        self.assertEqual([e["id"] for e in evs], ["7", "8", "9", "10"])
        self.assertEqual([e["data"]["n"] for e in evs], [7, 8, 9, 10])
        # 通道状态里的 dropped 是所有订阅者之和：运维靠它发现「有人在慢慢读」
        self.assertEqual(self.hub.status()["channels"][0]["dropped"], 6)

    def test_publish_never_blocks(self):
        """发布总耗时必须可忽略：一旦被慢客户端阻塞，所有通道都会退化成串行。

        为什么用「实测耗时 < 0.2 秒」而不是直接断言实现：这条断言的语义就是
        「背压不能让发布方付钱」——丢事件是对的，停下来等是错的。
        """
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            t0 = time.time()
            for i in range(50):
                self.hub.publish(KIND_QUOTES, "quotes", {"n": i, "pad": "x" * 200})
            elapsed = time.time() - t0
        self.assertLess(elapsed, 0.2, "50 次发布耗时可忽略，实测 %.3fs" % elapsed)
        self.assertEqual((len(sub.queue), sub.dropped), (4, 46))

    def test_subscribers_do_not_interfere(self):
        """一个慢订阅者的丢弃不影响另一个：dropped 与队列都是「每人一份」。"""
        with no_refresh_thread(self.hub):
            slow, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            fast, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            for i in range(1, 11):
                self.hub.publish(KIND_QUOTES, "quotes", {"n": i})
            self.assertEqual((len(slow.queue), slow.dropped), (4, 6))
            self.assertEqual((len(fast.queue), fast.dropped), (4, 6))
            # fast 是「边发边取」的客户端：每轮先取空再收下一条，因此不再发生丢弃
            for i in range(11, 21):
                take_events(fast)
                self.hub.publish(KIND_QUOTES, "quotes", {"n": i})
                self.assertEqual(fast.dropped, 6, "及时读取的订阅者不该再增加 dropped")
            tail = take_events(fast)
            self.assertEqual([e["data"]["n"] for e in take_events(slow)], [17, 18, 19, 20])
            self.assertEqual(slow.dropped, 16)
            status = self.hub.status()["channels"][0]
        self.assertEqual([e["data"]["n"] for e in tail], [20])
        self.assertEqual(status["dropped"], slow.dropped + fast.dropped)
        self.assertEqual(status["subscribers"], 2)

    def test_queue_size_has_a_floor_of_four(self):
        """hub 把 queue_size 抬到 ≥ 4（Subscriber 自己的下限是 1）：钉住这个下限。

        为什么要钉：误配成 1 会让每个客户端「永远只拿到最新一条」，
        而队列一旦不设下限（0 / 负数）就会在 ``put`` 里对空 deque 做 popleft 直接崩。
        """
        hub = S.StreamHub(queue_size=1)
        self.addCleanup(hub.close)
        self.assertEqual(hub.queue_size, 4)
        self.assertEqual(hub.status()["queueSize"], 4)
        self.assertEqual(S.Subscriber(None, 0)._size, 1)

    def test_subscriber_put_after_close_is_rejected(self):
        """关闭后的订阅者 put 返回 False 且不入队：这是「归还后不再收事件」的底座。"""
        sub = S.Subscriber(None, 2)
        self.assertTrue(sub.put({"id": "1"}))
        sub.closed = True
        self.assertFalse(sub.put({"id": "2"}))
        self.assertEqual([e["id"] for e in sub.queue], ["1"])
        self.assertEqual(sub.dropped, 0)


# --------------------------------------------------------------------------- #
# 5. 有限重放
# --------------------------------------------------------------------------- #
class TestReplay(unittest.TestCase):
    """有限重放：EventSource 断线重连时用 Last-Event-ID 补发，但必须有界。

    「有界」是关键：无界缓存会在服务端把每条通道的历史无限攒着（内存泄漏），
    有界意味着**重连太久就会缺口**，这是刻意的取舍，测试要把这个取舍钉住。
    """

    def setUp(self):
        self.clock = FakeClock()
        self.hub = S.StreamHub(fetch_quotes=FakeQuotes(), min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(self.hub.close)
        self.params = S.parse_params(KIND_QUOTES, {"symbols": "600519", "interval": "10"})

    def _publish(self, count, start=1):
        for i in range(start, start + count):
            self.hub.publish(KIND_QUOTES, "quotes", {"n": i})

    def test_cursor_returns_only_newer_events(self):
        """cursor 语义：只返回 id **大于** last_event_id 的缓存事件（等于的不重发）。"""
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            self._publish(5)
            _, replay = self.hub.subscribe(KIND_QUOTES, self.params, last_event_id="3")
            _, replay_eq = self.hub.subscribe(KIND_QUOTES, self.params, last_event_id="5")
        self.assertEqual([e["id"] for e in replay], ["4", "5"])
        self.assertEqual([e["data"]["n"] for e in replay], [4, 5])
        self.assertEqual(replay_eq, [], "cursor 已经是最新时不该补发任何东西")
        self.assertEqual(len(sub.queue), 5)

    def test_replay_is_capped_at_max_replay(self):
        """缓存上限是 MAX_REPLAY：更早的事件被丢弃，重放最多补回这么多条。"""
        self.assertEqual(S.MAX_REPLAY, 32)
        with no_refresh_thread(self.hub):
            self.hub.subscribe(KIND_QUOTES, self.params)
            self._publish(S.MAX_REPLAY + 8)          # id 1..40
            _, replay = self.hub.subscribe(KIND_QUOTES, self.params, last_event_id="1")
        self.assertEqual(len(replay), S.MAX_REPLAY)
        # 最旧的 8 条（id 1..8）已经被丢弃，因此第一条能补回来的是 id 9
        self.assertEqual([e["id"] for e in replay], [str(i) for i in range(9, 41)])
        self.assertEqual(replay[0]["data"]["n"], 9)
        self.assertEqual(replay[-1]["data"]["n"], 40)

    def test_replay_is_not_queued_for_the_new_subscriber(self):
        """重放的事件是**返回值**，不进新订阅者的队列。

        为什么必须这样：SSE 处理器要先按顺序把重放的帧写出去，再开始消费队列；
        如果重放同时入队，就会出现「补发的旧行情盖住刚取的新行情」。
        """
        with no_refresh_thread(self.hub):
            self.hub.subscribe(KIND_QUOTES, self.params)
            self._publish(3)
            sub2, replay = self.hub.subscribe(KIND_QUOTES, self.params, last_event_id="1")
        self.assertEqual(len(replay), 2)
        self.assertEqual(list(sub2.queue), [])
        self.assertEqual(sub2.dropped, 0)

    def test_illegal_cursor_returns_empty_without_raising(self):
        """非法 cursor（非数字 / 空串 / 带小数 / 混合字符 / None）一律返回空列表。

        为什么不能抛异常：cursor 来自 HTTP 头 ``Last-Event-ID``，完全由客户端控制，
        抛异常等于「随便造个头就能让对方重连失败」。
        """
        with no_refresh_thread(self.hub):
            self.hub.subscribe(KIND_QUOTES, self.params)
            self._publish(3)
            for cursor in ("abc", "", "   ", "3.5", "12abc", "0x10", None,
                           "1e3", "--2", "+", "\t"):
                with self.subTest(cursor=repr(cursor)):
                    _, replay = self.hub.subscribe(KIND_QUOTES, self.params,
                                                   last_event_id=cursor)
                    self.assertEqual(replay, [], "cursor=%r" % (cursor,))

    def test_whitespace_cursor_is_tolerated(self):
        """带空白的合法 cursor 要能识别（HTTP 头里首尾空白很常见）。"""
        with no_refresh_thread(self.hub):
            self.hub.subscribe(KIND_QUOTES, self.params)
            self._publish(3)
            _, replay = self.hub.subscribe(KIND_QUOTES, self.params, last_event_id=" 1 ")
        self.assertEqual([e["id"] for e in replay], ["2", "3"])

    def test_replay_since_is_directly_bound_safe(self):
        """直接调用 Channel.replay_since 传脏值也不能抛（SSE 处理器可能自己算 cursor）。"""
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, self.params)
            self._publish(2)
        for cursor in (None, "", "x", [], {}, object(), float("nan")):
            with self.subTest(cursor=repr(cursor)):
                self.assertEqual(sub.channel.replay_since(cursor), [])


# --------------------------------------------------------------------------- #
# 6. 研判差分
# --------------------------------------------------------------------------- #
class TestDiffAdvice(unittest.TestCase):
    """diff_advice：只推「会影响决策」的变化，噪声推送会把用户逼到静音整条通道。

    阈值（DIFF_SCORE=5 / DIFF_PROB=0.10 / DIFF_WEIGHT=0.02 / DIFF_RETURN=1.5）都是
    浮点减法比较，所以下面用「二进制友好」的基准值，让 4.9 / 0.0625 / 0.015625 这类
    「阈值以下」真的是阈值以下，而不是浮点误差的巧合。
    """

    def test_thresholds_match_module_contract(self):
        """先把阈值口径钉住：改阈值必须同步改这里的说明和前端文案。"""
        self.assertEqual(S.DIFF_SCORE, 5.0)
        self.assertEqual(S.DIFF_PROB, 0.10)
        self.assertEqual(S.DIFF_WEIGHT, 0.02)
        self.assertEqual(S.DIFF_RETURN, 1.5)

    def _one_change(self, **over):
        """把基准行改一个字段，返回 (changes, reasons)。"""
        prev = [advice_row()]
        new = [advice_row(**over)]
        return S.diff_advice(prev, new)

    def test_action_change_triggers(self):
        """档位变化是最强信号（建议买入 → 持有观察），必须触发且文案带前后档位。"""
        changes = self._one_change(action="hold")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["reasons"], ["档位 buy → hold"])
        self.assertEqual(changes[0]["prevAction"], "buy")
        self.assertEqual(changes[0]["action"], "hold")
        self.assertEqual(changes[0]["code"], "600519")
        self.assertEqual(changes[0]["name"], "贵州茅台")

    def test_score_swing_triggers_at_threshold(self):
        """评分摆动 ≥ 5 触发，文案必须带两个具体数字（用户要看到「从多少到多少」）。"""
        changes = self._one_change(score=65.0)          # 恰好 5.0，整数基准保证比较精确
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["reasons"], ["评分 60.0 → 65.0"])

    def test_prob_swing_triggers(self):
        """上涨概率摆动 ≥ 0.10 触发，文案按百分数展示。"""
        changes = self._one_change(upProb=0.65)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["reasons"], ["上涨概率 50% → 65%"])

    def test_expected_return_swing_triggers(self):
        """窗口内期望收益摆动 ≥ 1.5 个百分点触发。"""
        changes = self._one_change(expected=4.0)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["reasons"], ["期望收益 2.00% → 4.00%"])

    def test_kelly_weight_swing_triggers(self):
        """凯利仓位摆动 ≥ 0.02 触发（0.25 → 0.31 是实盘里必须提醒的加仓）。"""
        changes = self._one_change(weight=0.31)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["reasons"], ["凯利权重 25.0% → 31.0%"])

    def test_each_plan_price_triggers(self):
        """交易计划的四个价位任一变化都要触发，且文案指出是哪一档。"""
        base = {"entry": 10.0, "stop": 9.0, "target1": 12.0, "target2": 13.0}
        for key, label in (("entry", "入场"), ("stop", "止损"),
                           ("target1", "目标1"), ("target2", "目标2")):
            with self.subTest(level=key):
                plan = dict(base)
                plan[key] = base[key] + 0.5
                changes = self._one_change(plan=plan)
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]["reasons"],
                                 ["%s %.4f → %.4f" % (label, base[key], base[key] + 0.5)])
                self.assertEqual(changes[0]["plan"][key], base[key] + 0.5)

    def test_price_itself_is_not_a_change(self):
        """价格 / 涨跌幅变化**不算**研判变化：那是行情通道的职责。

        为什么这样断言：若把价格算进来，advisor 每次 tick 都会因为股价波动触发 change，
        30 秒一条「研判变了」的推送会把真正的档位变化淹没。
        """
        prev = [advice_row()]
        new = [advice_row()]
        new[0]["price"] = 11.0
        new[0]["changePct"] = 9.9
        self.assertEqual(S.diff_advice(prev, new), [])

    def test_swings_below_threshold_do_not_trigger(self):
        """阈值以下一律静默：4.9 / 0.0625 / 1.4 / 0.015625 都不该产生推送。"""
        cases = [{"score": 64.9}, {"upProb": 0.5625}, {"expected": 3.4}, {"weight": 0.265625},
                 {"score": 55.1}, {"upProb": 0.4375}, {"expected": 0.6}, {"weight": 0.234375}]
        for over in cases:
            with self.subTest(**over):
                self.assertEqual(self._one_change(**over), [], "%r 不该触发" % (over,))

    def test_new_and_removed_symbols_are_reported(self):
        """标的消失（上游剔除 / 退市）必须报变化，否则用户会继续对着旧表操作。"""
        prev = [advice_row(code="600519"), advice_row(code="000001", name="平安银行")]
        new = [advice_row(code="600519"), advice_row(code="300750", name="宁德时代")]
        changes = S.diff_advice(prev, new)
        self.assertEqual(len(changes), 2)
        by_code = {c["code"]: c for c in changes}
        self.assertEqual(by_code["300750"]["reasons"], ["新增标的"])
        self.assertEqual(by_code["000001"]["reasons"], ["标的已从订阅中移除"])
        self.assertEqual(by_code["000001"]["prevAction"], "buy")
        self.assertIsNone(by_code["000001"]["action"])

    def test_data_availability_flip_is_reported(self):
        """ok 翻转要报「数据可用性变化」：取不到数据的行还在表里，但结论不可信。"""
        self.assertEqual(S.diff_advice([advice_row(ok=True)], [advice_row(ok=False)])[0]["reasons"],
                         ["数据可用性变化：正常 → 异常"])
        self.assertEqual(S.diff_advice([advice_row(ok=False)], [advice_row(ok=True)])[0]["reasons"],
                         ["数据可用性变化：异常 → 正常"])

    def test_identical_inputs_return_empty(self):
        """两次完全相同的研判 → 空列表（这正是 pulse 的触发条件）。"""
        rows = [advice_row(), advice_row(code="000001", name="平安银行", action="hold", score=44.0)]
        before = json.loads(json.dumps(rows))
        self.assertEqual(S.diff_advice(rows, json.loads(json.dumps(rows))), [])
        # 差分是纯函数：不能就地改写入参（入参就是通道状态，被改坏下次比对就全错）
        self.assertEqual(rows, before)
        self.assertEqual(S.diff_advice([], []), [])
        self.assertEqual(S.diff_advice(None, None), [])

    def test_multiple_reasons_are_accumulated(self):
        """同一标的多个维度同时变化时，原因要全部列出（用户一次看全，而不是分三条推）。"""
        changes = self._one_change(action="sell", score=20.0,
                                   plan={"entry": 10.0, "stop": 8.0,
                                         "target1": 12.0, "target2": 13.0})
        self.assertEqual(len(changes), 1)
        reasons = changes[0]["reasons"]
        self.assertEqual(reasons[0], "档位 buy → sell")
        self.assertIn("评分 60.0 → 20.0", reasons)
        self.assertIn("止损 9.0000 → 8.0000", reasons)

    def test_numeric_strings_are_compared_as_numbers(self):
        """上游把数字序列化成字符串时不能再漏判（``score="65"`` 与 ``65.0`` 等价）。"""
        prev = [advice_row(score=60.0)]
        new = [advice_row(score="65")]
        new[0]["forecast"]["upProb"] = "0.65"
        self.assertEqual(S.diff_advice(prev, new)[0]["reasons"], ["评分 60.0 → 65.0", "上涨概率 50% → 65%"])

    def test_dirty_rows_never_raise(self):
        """脏输入（None / 非 dict 行 / 缺字段 / 行容器是字符串或字典）一律不抛异常。

        为什么这条最重要：diff_advice 的入参一侧来自上游、一侧来自通道内部状态，
        任何一侧结构变形抛异常都会让整条 advisor 通道变成 error 推送。
        注意非 dict 的**行容器**（数字）会抛 TypeError —— 但它在 hub 内部不可达
        （``_tick_advisor`` 已经保证两侧都是 list），因此这里只覆盖可达的形态。
        """
        good = [advice_row()]
        dirty_rows = [None, [], ["x"], [None], [0], [{}], [{"code": None}], [5.0],
                      [advice_row(plan=None, upProb=None)]]
        for prev_rows in dirty_rows:
            for new_rows in dirty_rows:
                with self.subTest(prev=repr(prev_rows)[:20], new=repr(new_rows)[:20]):
                    self.assertIsInstance(S.diff_advice(prev_rows, new_rows), list)
        for container in (None, "abc", {"code": "600519"}):
            with self.subTest(container=repr(container)):
                self.assertIsInstance(S.diff_advice(container, container), list)
        # 缺 plan / forecast / kelly 的行只是「没有可比字段」，不该触发也不该抛
        stripped = [{"code": "600519", "name": "贵州茅台", "action": "buy", "score": 60.0}]
        self.assertEqual(S.diff_advice(stripped, [dict(stripped[0])]), [])
        self.assertEqual(len(S.diff_advice(stripped, [])), 1, "整批消失要报一条移除")
        self.assertEqual(S.diff_advice(good, None)[0]["reasons"], ["标的已从订阅中移除"])


# --------------------------------------------------------------------------- #
# 7. advisor 通道状态机
# --------------------------------------------------------------------------- #
class TestAdvisorChannel(unittest.TestCase):
    """advisor：首次 snapshot → 无变化 pulse → 有变化 change；单次上游故障不能打死通道。"""

    def setUp(self):
        self.clock = FakeClock()
        self.advisor = FakeAdvisor(rows=[advice_row()])
        self.hub = S.StreamHub(recommend=self.advisor, min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(self.hub.close)
        self.params = S.parse_params(KIND_ADVISOR, {"symbols": "600519", "interval": "10"})

    def _subscribe(self):
        sub, replay = self.hub.subscribe(KIND_ADVISOR, self.params)
        self.assertEqual(replay, [])
        return sub

    def _next_tick(self):
        """推进一个 interval（10 秒）后手动跑一轮：等价于线程到期后的那次 _tick_once。"""
        tick(self.hub, self.clock, 10.0)

    @staticmethod
    def _channel_status(hub):
        return hub.status()["channels"][0]

    def test_first_tick_pushes_full_snapshot(self):
        """首次 tick 推 snapshot 且带**完整 result**：前端据此一次渲染整张表。

        为什么要求整份 result（而不是只给 rows）：详情面板、组合仓位、免责声明、
        模型名都在 result 里；缺了它们前端就得再发一次 /api/advisor/recommend，
        推送通道省下的那点带宽又还回去了。
        """
        with no_refresh_thread(self.hub):
            sub = self._subscribe()
            tick(self.hub)
            evs = take_events(sub)
        self.assertEqual(names(evs), ["snapshot"])
        data = evs[0]["data"]
        self.assertEqual(sorted(data), ["analyzed", "checked", "elapsedMs", "market",
                                        "result", "ts"])
        self.assertEqual(data["checked"], 1)
        self.assertEqual(data["analyzed"], 1)
        self.assertEqual(data["market"], "cn")
        self.assertIsInstance(data["elapsedMs"], int)
        result = data["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], self.advisor.rows)
        self.assertEqual(result["model"], "fake-advisor")
        self.assertEqual(result["disclaimer"], "合成数据，不构成投资建议")
        self.assertEqual(result["portfolio"]["positions"], 1)
        self.assertEqual(len(self.advisor.calls), 1)
        self.assertEqual(self.hub.stats["upstream"], 1)

    def test_second_tick_without_changes_pushes_pulse(self):
        """没有实质性变化时推 pulse（changed == 0）而不是安静：让用户知道系统在干活。

        为什么强调这点：静默会被误读成「推送挂了」。pulse 里带 checked/elapsedMs，
        前端可以显示「3 秒前已检查 12 只，无新变化」。
        """
        with no_refresh_thread(self.hub):
            sub = self._subscribe()
            tick(self.hub)                       # 第 1 次：snapshot
            take_events(sub)
            self._next_tick()                    # 第 2 次：同样的结果
            evs = take_events(sub)
        self.assertEqual(names(evs), ["pulse"])
        data = evs[0]["data"]
        self.assertEqual(data["changed"], 0)
        self.assertNotIn("changes", data, "pulse 不该带 changes（前端不该重排表格）")
        self.assertEqual(data["checked"], 1)
        self.assertEqual(len(self.advisor.calls), 2, "没变化也要重新研判，pulse 才有意义")
        self.assertEqual(self.hub.stats["upstream"], 2)

    def test_changed_tick_pushes_change_with_reasons(self):
        """有实质性变化时推 change，changed 必须等于 changes 长度（前端靠它决定要不要重排）。"""
        with no_refresh_thread(self.hub):
            sub = self._subscribe()
            tick(self.hub)
            take_events(sub)
            self.advisor.rows = [advice_row(action="hold", score=70.0)]
            self._next_tick()
            evs = take_events(sub)
            self.assertEqual(self._next_tick_pulse_when_stable(sub), ["pulse"])
        self.assertEqual(names(evs), ["change"])
        data = evs[0]["data"]
        self.assertEqual(data["changed"], len(data["changes"]))
        self.assertEqual(data["changed"], 1)
        change = data["changes"][0]
        self.assertEqual(change["code"], "600519")
        self.assertTrue(change["reasons"], "change 的每一项都必须带原因，否则用户不知道为什么推")
        self.assertIn("档位 buy → hold", change["reasons"])
        self.assertIn("评分 60.0 → 70.0", change["reasons"])
        self.assertEqual(data["portfolio"]["cash"], 1000.0)

    def _next_tick_pulse_when_stable(self, sub):
        """变化推送之后，同一份结果再来一次应回到 pulse（状态已被更新）。"""
        self._next_tick()
        return names(take_events(sub))

    def test_upstream_exception_pushes_error_and_channel_survives(self):
        """上游抛异常只影响当轮：推 error 事件，通道与订阅者都还活着，下一轮照常推。

        为什么这是最关键的一条：公开数据接口偶发 5xx / 超时是常态。若一次异常就让通道
        死掉（或让刷新线程退出），用户会一直盯着不再更新的表，而服务端已经悄悄停了 ——
        这是「单次上游故障不能打死通道」的回归测试。
        """
        with no_refresh_thread(self.hub):
            sub = self._subscribe()
            tick(self.hub)
            take_events(sub)

            self.advisor.error = "上游 502 Bad Gateway"
            self._next_tick()
            errs = take_events(sub)
            self.assertEqual(names(errs), ["error"])
            self.assertIn("上游 502 Bad Gateway", errs[0]["data"]["message"])
            self.assertIn("上游 502 Bad Gateway", self._channel_status(self.hub)["lastError"])
            self.assertFalse(sub.closed)
            self.assertEqual(self._channel_status(self.hub)["subscribers"], 1)
            self.assertIn(next(iter(self.hub.channels)), self.hub.channels)

            self.advisor.error = None
            self._next_tick()
            self.assertEqual(names(take_events(sub)), ["pulse"], "故障恢复后应照常推送")
            self.assertIsNone(self._channel_status(self.hub)["lastError"], "恢复后要清掉 lastError")
            self.assertEqual(self._channel_status(self.hub)["ticks"], 3)
        # 连续多轮故障也不能打死通道
        for _ in range(3):
            self.advisor.error = "连接超时"
            self._next_tick()
            self.assertEqual(names(take_events(sub)), ["error"])
        self.advisor.error = None
        self._next_tick()
        self.assertEqual(names(take_events(sub)), ["pulse"])

    def test_non_dict_result_pushes_error_and_channel_survives(self):
        """上游返回非 dict（None / list / 字符串）时推 error 而不是崩，且通道存活。"""
        for bad in (None, [], "oops", 0):
            with self.subTest(result=repr(bad)):
                clock = FakeClock()
                advisor = FakeAdvisor(rows=[advice_row()], result=bad)
                hub = S.StreamHub(recommend=advisor, min_upstream_gap=0.0, clock=clock)
                self.addCleanup(hub.close)
                with no_refresh_thread(hub):
                    sub, _ = hub.subscribe(KIND_ADVISOR, self.params)
                    tick(hub)
                    evs = take_events(sub)
                    self.assertEqual(names(evs), ["error"])
                    self.assertEqual(evs[0]["data"]["message"], "研判未返回有效结果")
                    self.assertEqual(self._channel_status(hub)["subscribers"], 1,
                                     "订阅者还在（通道没被打死）")
                    # 通道仍然存活：把上游修好，下一轮能正常推送
                    advisor.result = _UNSET
                    tick(hub, clock, 10.0)
                    self.assertEqual(names(take_events(sub)), ["snapshot"])

    def test_upstream_arguments_match_injection_contract(self):
        """透传契约：symbols 是 [{code, market, name}]，参数用 snake_case 关键字。

        为什么这样断言：core.advisor.recommend 的签名就是这一套（见 tests/test_advisor.py
        的 TestDataInjection）。写错一个关键字名不会报错 —— 会因为 **params 落进默认值，
        于是「用户设的 horizon 5」被静默当成 20，结论完全不同。
        """
        params = S.parse_params(KIND_ADVISOR, {"symbols": "600519:贵州茅台", "market": "us",
                                              "interval": "30", "horizon": "5",
                                              "capital": "200000", "kellyFraction": "0.3",
                                              "maxWeight": "0.2"})
        with no_refresh_thread(self.hub):
            self.hub.subscribe(KIND_ADVISOR, params)
            tick(self.hub)
        symbols, kwargs = self.advisor.calls[0]
        self.assertEqual(symbols, [{"code": "600519", "market": "us", "name": "贵州茅台"}])
        self.assertEqual(kwargs, {"market": "us", "horizon": 5, "capital": 200000.0,
                                 "kelly_fraction": 0.3, "max_weight": 0.2})

    def test_empty_symbols_pushes_error_without_calling_upstream(self):
        """没订阅标的时不打上游：省一次昂贵请求，同时给前端一个明确原因。"""
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_ADVISOR, S.parse_params(
                KIND_ADVISOR, {"symbols": "", "interval": "10"}))
            tick(self.hub)
        evs = take_events(sub)
        self.assertEqual(names(evs), ["error"])
        self.assertEqual(evs[0]["data"]["message"], "未订阅任何标的")
        self.assertEqual(self.advisor.calls, [])


# --------------------------------------------------------------------------- #
# 8. 上游限流
# --------------------------------------------------------------------------- #
class TestUpstreamThrottle(unittest.TestCase):
    """全局最小间隔：宁可推送慢一点，也不要让工具变成公开接口的压力源。"""

    def test_second_tick_inside_gap_waits_and_is_counted(self):
        """两次 tick 的间隔（1 秒）小于 min_upstream_gap（1.2 秒）时，第二次必须先等。

        为什么这样断言：限流只有两个可见证据 —— stats["throttled"]（发生过等待）
        与 stats["upstream"]（真实调用次数），后者必须和 FakeQuotes 记录的次数**完全一致**，
        否则 /api/stream/status 上的「上游请求数」就是在骗人。
        这里真实等待 0.2 秒（1.2 - 1.0）：本文件为限流付出的真等待只有两处（这里与
        test_gap_is_global_across_channels 的 0.15 秒），其余用例一律 gap=0。
        """
        clock = FakeClock()
        feed = FakeQuotes()
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=S.MIN_UPSTREAM_GAP, clock=clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_QUOTES, S.parse_params(
                KIND_QUOTES, {"symbols": "600519", "interval": "1"}))
            tick(hub)                                   # 第一次：_last_upstream 还是 0.0，不等
            self.assertEqual(hub.stats["throttled"], 0)
            clock.advance(1.0)                          # 恰好一个 interval，小于 1.2 秒窗口
            t0 = time.time()
            tick(hub)
            waited = time.time() - t0
            evs = take_events(sub)
        self.assertGreaterEqual(hub.stats["throttled"], 1)
        self.assertGreaterEqual(waited, 0.15, "限流等待必须真的发生，实测 %.3fs" % waited)
        self.assertEqual(hub.stats["upstream"], len(feed.calls))
        self.assertEqual(hub.stats["upstream"], 2)
        self.assertEqual([c[1] for c in feed.calls], [["600519"], ["600519"]])
        self.assertEqual(names(evs), ["quotes", "quotes"], "被限流不影响事件照常推")
        self.assertEqual(len(evs), 2)

    def test_gap_is_global_across_channels(self):
        """限流是**全局**的而不是每通道一份：否则通道一多，并发依旧能打爆上游。

        两条通道在同一瞬间到期 → 第二次上游调用必然落在窗口内 → throttled ≥ 1。
        这里刻意把窗口设成 0.15 秒（而不是默认 1.2 秒）：同一瞬间的两次调用 gap≈0，
        必然触发等待，没必要为了证明「限流真的会等」真睡 1.2 秒。
        """
        clock = FakeClock()
        feed = FakeQuotes()
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=0.15, clock=clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES, {"symbols": "600519",
                                                                    "interval": "1"}))
            hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES, {"symbols": "000001",
                                                                    "interval": "1"}))
            tick(hub)                                   # 两条通道同时到期
        self.assertEqual(hub.stats["throttled"], 1)
        self.assertEqual(hub.stats["upstream"], 2)
        self.assertEqual(hub.stats["upstream"], len(feed.calls))
        self.assertEqual(sorted(c[1][0] for c in feed.calls), ["000001", "600519"])

    def test_zero_gap_disables_waiting(self):
        """min_upstream_gap=0 是离线 / 测试的逃生门：不再等待，但仍如实计数。"""
        clock = FakeClock()
        feed = FakeQuotes()
        hub = S.StreamHub(fetch_quotes=feed, min_upstream_gap=0.0, clock=clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES, {"symbols": "600519",
                                                                    "interval": "1"}))
            tick(hub)
            clock.advance(1.0)
            t0 = time.time()
            tick(hub)
            waited = time.time() - t0
        self.assertLess(waited, 0.1, "gap=0 时不该有任何等待，实测 %.3fs" % waited)
        self.assertEqual(hub.stats["throttled"], 0)
        self.assertEqual(hub.stats["upstream"], 2)
        self.assertEqual(hub.stats["upstream"], len(feed.calls))

    def test_default_gap_and_queue_constants(self):
        """默认常量口径（1.2 秒 / 64 条 / 32 条）：它们是运维与前端文档引用的事实来源。"""
        self.assertEqual(S.MIN_UPSTREAM_GAP, 1.2)
        self.assertEqual(S.MAX_QUEUE, 64)
        self.assertEqual(S.MAX_REPLAY, 32)
        hub = S.StreamHub()
        self.addCleanup(hub.close)
        self.assertEqual(hub.min_upstream_gap, S.MIN_UPSTREAM_GAP)
        self.assertEqual(hub.queue_size, S.MAX_QUEUE)


# --------------------------------------------------------------------------- #
# 9. trade 通道与 publish_hook
# --------------------------------------------------------------------------- #
class TestTradeAndHook(unittest.TestCase):
    """trade 通道与 publish_hook：外发对接与「有没有人开着页面」无关。"""

    def test_trade_events_are_broadcast(self):
        """order / fill / account / config / note 五类事件都要广播到订阅者。

        为什么逐个事件名断言：前端 ``EventSource`` 是按事件名注册回调的，
        漏一个就意味着「成交推送明明发了，页面纹丝不动」。
        """
        hook = []
        hub = S.StreamHub(publish_hook=lambda kind, event, data: hook.append((kind, event, data)))
        self.addCleanup(hub.close)
        payloads = [("order", {"id": "T1", "code": "600519", "side": "buy"}),
                    ("fill", {"id": "T1", "price": 10.0, "qty": 100}),
                    ("account", {"cash": 1000.0, "equity": 2000.0}),
                    ("config", {"interval": 3}),
                    ("note", {"text": "换手率偏高"})]
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_TRADE, S.parse_params(KIND_TRADE, {}))
            for event, data in payloads:
                self.assertEqual(hub.publish_trade(event, data), 1)
            evs = take_events(sub)
        self.assertEqual(names(evs), [p[0] for p in payloads])
        self.assertEqual([e["data"] for e in evs], [p[1] for p in payloads])
        self.assertEqual([e["id"] for e in evs], ["1", "2", "3", "4", "5"])
        self.assertEqual([c[1] for c in hook], [p[0] for p in payloads])
        self.assertTrue(all(c[0] == KIND_TRADE for c in hook))
        self.assertEqual(hub.stats["published"], 5)

    def test_hook_fires_without_any_subscriber(self):
        """**没有任何浏览器订阅者**时，publish_hook 也必须被调用。

        这是刻意设计：把委托 / 成交同时外发到 webhook 或外部撮合系统，
        与「此刻有没有人开着页面」完全没有关系。没有任何订阅者也要照发 ——
        否则「收盘后自动跑批下的单没有外发」这种问题会极难排查（页面不开就丢消息）。
        """
        calls = []
        hub = S.StreamHub(publish_hook=lambda kind, event, data: calls.append((kind, event, data)))
        self.addCleanup(hub.close)
        self.assertEqual(hub.channels, {}, "前提：一条通道都没有")
        self.assertEqual(hub.publish_trade("order", {"id": "T1"}), 0, "送达数为 0，但钩子要跑")
        self.assertEqual(calls, [(KIND_TRADE, "order", {"id": "T1"})])
        # 通道存在但订阅者刚刚归还（通道已被回收）时，同样要发
        p = S.parse_params(KIND_TRADE, {})
        sub, _ = hub.subscribe(KIND_TRADE, p)
        hub.unsubscribe(sub)
        self.assertEqual(hub.publish_trade("fill", {"id": "T2"}), 0)
        self.assertEqual([c[1] for c in calls], ["order", "fill"])

    def test_hook_exception_does_not_break_publishing(self):
        """钩子抛异常必须被吞掉：webhook 挂在外部网络上，不能让推送跟着一起挂。"""
        def boom(kind, event, data):
            raise RuntimeError("webhook 挂了")
        hub = S.StreamHub(publish_hook=boom)
        self.addCleanup(hub.close)
        self.assertEqual(hub.publish_trade("order", {"id": "T1"}), 0)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_TRADE, S.parse_params(KIND_TRADE, {}))
            self.assertEqual(hub.publish_trade("fill", {"id": "T1"}), 1, "订阅者照常收到")
            self.assertEqual(names(take_events(sub)), ["fill"])
            # 钩子抛异常不能改变返回值语义：有订阅者时仍然是真实送达数
            self.assertEqual(hub.publish_trade("note", {"text": "风控拒单"}), 1)
            self.assertEqual(names(take_events(sub)), ["note"])

    def test_low_level_publish_is_for_external_drivers(self):
        """锁定当前语义：钩子只挂在 publish_trade 上，低层 publish 不触发钩子。

        为什么钉住：``hub.publish`` 是给测试 / 外部驱动用的低层入口，
        若将来把钩子上移到 publish 层，webhook 会开始收到一整批「本不该外发」的合成事件，
        这条断言会失败并提醒同步契约（详见回复里的「观察」）。
        """
        calls = []
        hub = S.StreamHub(publish_hook=lambda k, e, d: calls.append((k, e, d)))
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_TRADE, S.parse_params(KIND_TRADE, {}))
            # 指定通道键广播（外部驱动想模拟一次成交时用得到）
            key = next(iter(hub.channels))
            self.assertEqual(hub.publish(KIND_TRADE, "fill", {"id": "T1"}, channel_key=key), 1)
            self.assertEqual(hub.publish(KIND_TRADE, "fill", {"id": "T2"}, channel_key="nope"), 0)
            self.assertEqual(names(take_events(sub)), ["fill"])
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- #
# 10. status() / available() / close()
# --------------------------------------------------------------------------- #
class TestStatusContract(unittest.TestCase):
    """status() 是 /api/stream/status 的响应体，字段就是运维的全部视野。"""

    def test_status_field_contract(self):
        """顶层与通道级的字段一个都不能少；并锁定「dropped 是所有订阅者之和」这类口径。"""
        hub = S.StreamHub(fetch_quotes=FakeQuotes(), queue_size=8, min_upstream_gap=0.5)
        self.addCleanup(hub.close)
        sub, _ = hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES, {"symbols": "600519"}))
        status = hub.status()
        for key in ("ok", "running", "subscribers", "channels", "stats", "minUpstreamGap",
                    "queueSize", "note"):
            self.assertIn(key, status)
        self.assertTrue(status["ok"])
        self.assertEqual(status["queueSize"], 8)
        self.assertEqual(status["minUpstreamGap"], 0.5)
        self.assertEqual(status["subscribers"], 1)
        self.assertIsInstance(status["note"], str)
        self.assertIn("quotes", status["note"])
        self.assertIn("advisor", status["note"])
        self.assertIn("trade", status["note"])
        self.assertEqual(sorted(status["stats"]),
                         ["published", "startedAt", "throttled", "upstream"])
        self.assertIsInstance(status["stats"]["startedAt"], int)

        channel = status["channels"][0]
        for key in ("key", "kind", "interval", "subscribers", "ticks", "upstream",
                    "lastError", "dropped", "symbols"):
            self.assertIn(key, channel)
        self.assertEqual(channel["kind"], KIND_QUOTES)
        self.assertEqual(channel["interval"], 3)
        self.assertEqual(channel["subscribers"], 1)
        self.assertEqual(channel["symbols"], ["600519"])
        self.assertEqual(channel["dropped"], 0)
        self.assertIsNone(channel["lastError"])
        self.assertIn("quotes", channel["key"])
        json.dumps(status)  # 响应体必须能直接序列化

        self.assertEqual(hub.status()["subscribers"],
                         sum(c["subscribers"] for c in hub.status()["channels"]))

    def test_channels_are_sorted_by_key(self):
        """通道列表按 key 排序：状态页每次刷新都换顺序会让人以为通道在抖动。"""
        hub = S.StreamHub(fetch_quotes=FakeQuotes(), min_upstream_gap=0.0)
        self.addCleanup(hub.close)
        for symbols in ("600519,000001", "600519", "000001"):
            hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES, {"symbols": symbols}))
        keys = [c["key"] for c in hub.status()["channels"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), 3)

    def test_available_matrix(self):
        """available() 如实汇报：缺哪个注入哪个通道不可用；trade 不需要上游，恒可用。

        为什么强调「如实」：可用性决定服务端要不要暴露该通道的 SSE 端点。
        若它乐观返回 True，用户连上后只会看到一条永远没有数据的连接。
        """
        cases = ((S.StreamHub(), (False, False, True)),
                 (S.StreamHub(fetch_quotes=FakeQuotes()), (True, False, True)),
                 (S.StreamHub(recommend=FakeAdvisor()), (False, True, True)),
                 (S.StreamHub(fetch_quotes=FakeQuotes(), recommend=FakeAdvisor()),
                  (True, True, True)))
        for hub, expect in cases:
            self.addCleanup(hub.close)
            got = tuple(hub.available(k) for k in (KIND_QUOTES, KIND_ADVISOR, KIND_TRADE))
            self.assertEqual(got, expect)
        hub = S.StreamHub()
        self.addCleanup(hub.close)
        self.assertFalse(hub.available("bogus"), "未知通道名按不可用处理")
        # 不可调用（例如误把 URL 字符串当成抓取器）等于没注入
        self.assertFalse(S.StreamHub(fetch_quotes="http://example.com").available(KIND_QUOTES))
        self.assertFalse(S.StreamHub(recommend=object()).available(KIND_ADVISOR))

    def test_subscribe_shouts_when_unavailable(self):
        """不可用时 subscribe 必须**显式报错**（RuntimeError），不能返回一个永远收不到事件的订阅者。

        为什么要求显式失败：静默失败的推送通道是运维噩梦 —— 日志干净、连接 200、
        数据永远不来，排查要从 HTTP 层一路查到上游注入。
        """
        hub = S.StreamHub()
        self.addCleanup(hub.close)
        for kind in (KIND_QUOTES, KIND_ADVISOR):
            with self.subTest(kind=kind):
                with self.assertRaises(RuntimeError) as ctx:
                    hub.subscribe(kind, S.parse_params(kind, {"symbols": "600519"}))
                self.assertIn(kind, str(ctx.exception))
        self.assertEqual(hub.channels, {}, "失败的订阅不能留下半条通道")
        # trade 不需要任何注入
        sub, replay = hub.subscribe(KIND_TRADE, S.parse_params(KIND_TRADE, {}))
        self.assertEqual(replay, [])
        self.assertEqual(sub.channel.kind, KIND_TRADE)
        self.assertEqual(len(hub.channels), 1)

    def test_unknown_kind_is_rejected(self):
        """未知通道名抛 ValueError（与「缺数据源」的 RuntimeError 分工明确）。"""
        hub = S.StreamHub(fetch_quotes=FakeQuotes())
        self.addCleanup(hub.close)
        with self.assertRaises(ValueError):
            hub.subscribe("bogus", {})
        with self.assertRaises(ValueError):
            hub.subscribe(None, {})

    def test_subscribe_tolerates_dirty_params(self):
        """params 不是 dict 时不能把 subscribe 打挂（hub 用 isinstance 兜底成空参数）。"""
        hub = S.StreamHub(fetch_quotes=FakeQuotes())
        self.addCleanup(hub.close)
        for params in ("600519", 5, None, ["600519"], object()):
            with self.subTest(params=repr(params)[:20]):
                sub, replay = hub.subscribe(KIND_QUOTES, params)
                self.assertEqual(replay, [])
                self.assertEqual(sub.channel.params, {})
                self.assertEqual(hub.status()["channels"][0]["symbols"], [])

    def test_close_stops_thread_and_closes_subscribers(self):
        """close()：订阅抛异常、订阅者全部 closed、线程停掉；重复调用不抛异常。

        为什么要求幂等：close 会在进程退出、异常清理、测试 tearDown 等多条路径上被调用，
        抛异常会把真正的退出原因盖掉。
        """
        hub = S.StreamHub(fetch_quotes=FakeQuotes(), min_upstream_gap=0.0)
        self.addCleanup(hub.close)
        quotes_sub, _ = hub.subscribe(KIND_QUOTES, S.parse_params(
            KIND_QUOTES, {"symbols": "600519", "interval": "1"}))
        trade_sub, _ = hub.subscribe(KIND_TRADE, S.parse_params(KIND_TRADE, {}))
        hub.close()
        self.assertTrue(hub.closed)
        self.assertTrue(quotes_sub.closed)
        self.assertTrue(trade_sub.closed)
        self.assertEqual(hub.channels, {})
        with self.assertRaises(RuntimeError):
            hub.subscribe(KIND_TRADE, S.parse_params(KIND_TRADE, {}))
        self.assertTrue(wait_until(lambda: hub.status()["running"] is False),
                        "关闭后刷新线程必须停下来")
        hub.close()
        hub.close()
        hub.unsubscribe(quotes_sub)          # 关闭后归还订阅者也不能抛
        self.assertEqual(hub.publish_trade("order", {"id": "T1"}), 0)
        self.assertEqual(hub.publish(KIND_QUOTES, "quotes", {"n": 1}), 0)
        status = hub.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["subscribers"], 0)
        self.assertEqual(status["channels"], [])


# --------------------------------------------------------------------------- #
# 11. 并发安全（轻量）
# --------------------------------------------------------------------------- #
class TestConcurrency(unittest.TestCase):
    """并发安全：发布线程与订阅抖动线程共用同一把 RLock，且后台刷新线程同时在跑。

    为什么用 trade 通道做载体：trade 是事件驱动的，``_tick_once`` 不会为它产生任何事件
    （只累加 ticks），所以后台线程在跑也不会往队列里塞东西 ——
    这样「dropped + 队列长度 == 发布次数」才是一个可断言的等式。
    """

    ITERATIONS = 200
    PUBLISHERS = 3

    def test_concurrent_publish_and_subscribe_churn(self):
        hub = S.StreamHub(queue_size=8, min_upstream_gap=0.0)
        self.addCleanup(hub.close)
        params = S.parse_params(KIND_TRADE, {})
        other = S.parse_params(KIND_TRADE, {"market": "us"})
        observer, _ = hub.subscribe(KIND_TRADE, params)
        errors = []
        counts = [0] * self.PUBLISHERS
        stop = threading.Event()

        def publisher(idx):
            for i in range(self.ITERATIONS):
                try:
                    hub.publish(KIND_TRADE, "tick", {"i": i, "t": idx})
                    counts[idx] += 1      # 每个线程写自己的槽位，避免自增竞争
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def churn():
            while not stop.is_set():
                try:
                    s, _ = hub.subscribe(KIND_TRADE, params)
                    hub.unsubscribe(s)
                    s2, _ = hub.subscribe(KIND_TRADE, other)
                    hub.unsubscribe(s2)   # 这条通道可能刚被 _reap 回收，归还必须容忍
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        publishers = [threading.Thread(target=publisher, args=(i,))
                      for i in range(self.PUBLISHERS)]
        churner = threading.Thread(target=churn)
        churner.start()
        for thread in publishers:
            thread.start()
        for thread in publishers:
            thread.join()
        stop.set()
        churner.join()

        self.assertEqual(errors, [], "并发发布 / 订阅抖动不能抛异常")
        self.assertEqual(counts, [self.ITERATIONS] * self.PUBLISHERS)
        total = sum(counts)
        # 自洽等式：观察者要么收到、要么被丢弃，一条都不能凭空多出来
        self.assertEqual(observer.dropped + len(observer.queue), total)
        self.assertEqual(observer.channel.seq, total, "通道序号 == 实际落盘的事件数")
        self.assertLessEqual(len(observer.queue), hub.queue_size)
        ids = [e["id"] for e in observer.queue]
        self.assertEqual(len(set(ids)), len(ids), "同一条事件不能被重复入队")
        self.assertTrue(all(i.isdigit() for i in ids))
        self.assertTrue(all(1 <= int(i) <= total for i in ids))
        for event in observer.queue:
            self.assertEqual(sorted(event), ["data", "event", "id", "ts"], "结构不能被并发改坏")
        self.assertLessEqual(len(hub.channels), 2)
        self.assertIn(observer.channel.key, hub.channels)
        self.assertGreater(observer.dropped, 0, "队列只有 8 条、发了 600 条，必然发生丢弃")


# --------------------------------------------------------------------------- #
# 12. 已发现缺陷的锚点（不允许修改 core/stream.py，故用 expectedFailure）
# --------------------------------------------------------------------------- #
class TestFixedDefects(unittest.TestCase):
    """本文件编写过程中实测到、**已修复**的五处缺陷的回归测试。

    为什么保留它们：五条都是「曾经错误、现在正确」的行为，写成普通断言后一旦有人
    把修好的逻辑改回去，套件立刻变红 —— 比在代码里留一句注释更难被绕过。

    五处依次是：① subscribe 不产出 ready 帧（前端等不到「已连接」）；
    ② 负数 Last-Event-ID 触发全量重放；③ NaN / inf 参数旁路默认值并把非法 JSON 推进链路；
    ④ 只推 error 事件却不写 last_error，/api/stream/status 上显示成「健康」通道；
    ⑤ parse_params 收到非 dict 容器直接 AttributeError。
    """

    def setUp(self):
        self.clock = FakeClock()
        self.feed = FakeQuotes()
        self.hub = S.StreamHub(fetch_quotes=self.feed, recommend=FakeAdvisor(rows=[advice_row()]),
                               min_upstream_gap=0.0, clock=self.clock)
        self.addCleanup(self.hub.close)

    def test_ready_frame_is_attached_to_subscriber(self):
        """回归①：订阅必须产出 ready 帧（挂在订阅者上，由 SSE 传输层写成第一帧）。

        为什么 ready **不进队列**：它是「你这次订阅被受理了」的连接级确认（含服务端
        实际生效的 interval），不是通道上的公共事件。放进队列会污染「队列里全是通道
        事件」这个不变量，也会让重连补发逻辑与 ready 混在一起；由传输层写首帧语义更清。
        对前端的可见结果是等价的：连上就先收到 ready，状态从「连接中」切到「已连接」。
        """
        with no_refresh_thread(self.hub):
            sub, _ = self.hub.subscribe(KIND_QUOTES, S.parse_params(
                KIND_QUOTES, {"symbols": "600519", "interval": "3"}))
            evs = take_events(sub)
        self.assertIsInstance(sub.ready, dict, "订阅后必须带有 ready 帧")
        for key in ("channel", "kind", "market", "symbols", "interval", "ts", "note"):
            self.assertIn(key, sub.ready, "ready 帧缺少字段 %s" % key)
        self.assertEqual(sub.ready["kind"], KIND_QUOTES)
        self.assertEqual(sub.ready["symbols"], ["600519"])
        self.assertEqual(sub.ready["interval"], 3)
        self.assertTrue(sub.ready["note"])
        self.assertNotIn("ready", names(evs),
                         "ready 不应进入通道队列（队列只放通道事件）")

    def test_negative_last_event_id_should_mean_no_replay(self):
        """缺陷 2：负数 cursor 被当成合法值 → 触发**全量重放**，而不是返回空列表。

        现象：``last_event_id=-1`` 时返回整段缓存（因为所有 id 都 > -1）。
        复现：本用例 —— 传 ``-3`` 期望空列表，实际拿回 3 条。
        影响：cursor 来自客户端可控的 ``Last-Event-ID`` 头（也可能是前端把游标算错成负数），
        一个负数就能让服务端每次重连都补发最多 MAX_REPLAY 条历史事件；
        与 subscribe 里 ``if last_event_id`` 的非空判断叠加后，语义变成
        「0/None/空串 = 不重放，负数 = 全部重放」，与「非法值回退默认」的承诺相悖。
        建议修法：在 ``StreamHub.subscribe``/``Channel.replay_since`` 里把
        ``lid < 0`` 与解析失败一起归为「无有效游标」返回 ``[]``（正则 ``^\\d+$`` 更稳）。
        """
        with no_refresh_thread(self.hub):
            self.hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES,
                                                          {"symbols": "600519"}))
            for i in range(1, 4):
                self.hub.publish(KIND_QUOTES, "quotes", {"n": i})
            _, replay = self.hub.subscribe(KIND_QUOTES, S.parse_params(KIND_QUOTES,
                                                                      {"symbols": "600519"}),
                                           last_event_id="-3")
        self.assertEqual(replay, [], "非法 cursor（负数）应视为无游标")

    def test_nan_and_inf_params_should_fall_back_to_defaults(self):
        """缺陷 3：``capital=nan`` / ``maxWeight=inf`` 这类非有限数没有回退默认。

        现象：``_num()`` 只拦了解析异常，``float('nan')`` / ``float('inf')`` 会原样返回；
        而 ``x or default`` 里 NaN 是**真值**，于是默认值不会被用上。
        复现：``parse_params('advisor', {'capital': 'nan'})['capital']`` → nan（期望 100000.0）。
        影响：① 与 docstring「非法值一律回退默认」的承诺不符；
        ② 这些值会进通道键（键里出现 'nan'/'inf'）并透传给 core.advisor.recommend；
        ③ 非有限数在 JSON 里只能写成 ``NaN``/``Infinity``，浏览器 ``JSON.parse`` 会直接报错
        （core/advisor.py 自己用 allow_nan=False 的严格口径，这里却把 NaN 放进了链路）。
        建议修法：``_num`` 里改成
        ``x = float(...); return x if math.isfinite(x) else None``。
        对 diff_advice 无副作用：它本来就要求两侧都非 None 才比较，NaN 现状是「永不触发」，
        变成 None 后同样「永不触发」。
        """
        for given in ("nan", "inf", "-inf", float("nan"), float("inf")):
            with self.subTest(given=given):
                params = S.parse_params(KIND_ADVISOR, {"capital": given,
                                                       "kellyFraction": given,
                                                       "maxWeight": given})
                self.assertEqual(params["capital"], 100000.0, "capital=%r" % (given,))
                self.assertEqual(params["kellyFraction"], 0.5, "kellyFraction=%r" % (given,))
                self.assertEqual(params["maxWeight"], 0.25, "maxWeight=%r" % (given,))

    def test_error_events_are_visible_in_channel_status(self):
        """缺陷 4：推了 error 事件但通道 ``lastError`` 仍是 None（状态页显示「健康」）。

        现象：``_tick_quotes`` / ``_tick_advisor`` 里「未订阅任何标的」「研判未返回有效结果」
        这两条路径只 ``_publish('error', ...)`` 后返回，没有抛异常，
        于是 ``_tick_once`` 走 else 分支把 ``last_error`` 清成 None。
        复现：本用例 —— 上游返回 None，error 事件确实进了队列（前半段断言通过），
        但 ``status().channels[0].lastError`` 为 None。
        影响：lastError 的唯一用途就是回答「这条通道为什么没数据」，
        而最需要它的两条路径恰恰不写它 —— 运维在 /api/stream/status 上看不到任何异常。
        建议修法：这两处改成 ``ch.last_error = "研判未返回有效结果"; self._publish(...)``
        后返回；或者在 ``_tick_once`` 里用「本轮是否推过 error」统一维护 last_error。
        """
        advisor = FakeAdvisor(rows=[advice_row()], result=None)
        clock = FakeClock()
        hub = S.StreamHub(recommend=advisor, min_upstream_gap=0.0, clock=clock)
        self.addCleanup(hub.close)
        with no_refresh_thread(hub):
            sub, _ = hub.subscribe(KIND_ADVISOR, S.parse_params(
                KIND_ADVISOR, {"symbols": "600519", "interval": "10"}))
            tick(hub)
            # 1) error 事件确实推出去了（这一段是现状就成立的）
            self.assertEqual(names(take_events(sub)), ["error"])
            # 2) 通道状态里也应该能看到原因 —— 现状是 None，所以该断言失败
            self.assertIsNotNone(hub.status()["channels"][0]["lastError"])

    def test_parse_params_tolerates_non_dict_container(self):
        """缺陷 5（低危）：``parse_params`` 收到非 dict 容器时抛 AttributeError。

        现象：``raw = raw or {}`` 只挡住了 None/空值，字符串 / list / 数字会直接 ``.get``。
        复现：``parse_params('quotes', '600519')`` → AttributeError: 'str' object has no attribute 'get'。
        影响：docstring 承诺「非法值一律回退默认，不抛异常」，而 hub 自己的
        ``subscribe`` 是用 ``isinstance(params, dict)`` 宽容兜底的 —— 同一模块两套宽严标准，
        将来若有调用方（脚本 / CLI / 别的 handler）直接透传 query 容器就会 500。
        建议修法：``raw = raw if isinstance(raw, dict) else {}``（一行，与 subscribe 一致）。
        """
        for raw in ("600519", ["600519"], 5):
            with self.subTest(raw=repr(raw)):
                params = S.parse_params(KIND_QUOTES, raw)
                self.assertEqual(params["symbols"], [])
                self.assertEqual(params["market"], "cn")


if __name__ == "__main__":
    unittest.main(verbosity=2)

