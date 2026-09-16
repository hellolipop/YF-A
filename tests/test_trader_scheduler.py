#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时调度器（core/trader.py 的 SESSIONS_UTC / in_session / next_session_open /
TradeScheduler）的单元测试。

只依赖标准库 unittest，可直接 `python3 tests/test_trader_scheduler.py` 运行，
也能被 `python3 -m unittest discover -s tests -p "test_*.py"` 收集。

覆盖范围（A~E 五组，与需求的断言点一一对应）
------------------------------------------
A. ``TestSession*``            交易时段判定（UTC 口径）：A股四个时刻 + 四个边界（左闭右开）、
                               周末全天休市、美股并集窗口、脏 market 不抛异常、
                               ``next_session_open`` 必须「未来且真的开市」、周末也能算出
                               下周一的开市时间、判定只与传入 ts 有关（换本机时区结果不变）。
B. ``TestGate*`` / ``TestSkip*`` / ``TestInterval*`` / ``TestForce*`` /
   ``TestIgnoreMarketHours*``  三层闸门与跳过原因、``lastSkip`` 必写、闸门有序、
                               同一原因只计一次（以及跑过一轮后重新计数）、间隔限流（quiet）、
                               ``force`` 的语义（绕得开调度开关 / 间隔，但绕不过 enabled、
                               也绕不过 dryrun）、``ignoreMarketHours`` 开关。
C. ``TestDryrun*`` / ``TestPaper*`` / ``TestNoActionable*`` / ``TestRecommend*`` /
   ``TestSingleFlight``        计划与成交：dryrun 不动钱（前后指纹比对）、paper+autoExecute
                               成交并与手算口径对账、paper 不自动成交只出计划、
                               「无可执行档位」≠ 错误、研判失败记错误且不失效、单次不重叠。
D. ``TestStatus*`` / ``TestLifecycle*`` / ``TestCounters*`` / ``TestOnResult*`` /
   ``TestLogHook*`` / ``TestJsonSafety``
                               状态字段契约、线程启停幂等、计数器自洽、回调（含回调抛异常）、
                               日志钩子（执行 / 跳过 / 异常 / 启停）、返回值可 JSON 序列化。
E. ``TestConfigKeys``          三个新配置键（scheduler / autoExecute / ignoreMarketHours）的
                               默认值、字符串真值、非法回退、未知键丢弃。
Z. ``TestKnownGaps``           六个 ``unittest.expectedFailure`` 缺陷锚点（详见该类注释）：
                               ① force 没有绕过交易时段；② ``log`` 钩子被 ``callable`` 守卫
                               挡死（项目自带的 Logger 收不到任何调度日志）；③ 构造参数
                               ``market`` 被忽略；④ 构造参数 ``fetch_quotes`` 从未被调用；
                               ⑤ ``in_session``(秒) 与 ``next_session_open``(毫秒) 单位不一致
                               导致组合调用抛 ValueError；⑥ 锁竞争跳过绕过了跳过去重。

数量与耗时：61 个用例，单跑约 0.4 秒（线程用例合计只睡 0.25 秒）。

为什么全部离线、无网络、无长等待
--------------------------------
``TradeScheduler`` 把「研判」做成了依赖注入（``recommend_fn``），因此本文件一律注入
``FakeRecommend``：它只记录调用参数、返回确定性数据，绝不联网、绝不随机。时间也全部来自
注入的 ``FakeClock``（默认停在**周三 01:40 UTC = 北京时间 09:40**，A股上午开市），
绝大多数用例直接调 ``tick_once(now=…)`` / ``tick_once()`` 驱动，既不依赖真实时间，
也不依赖调度线程抢跑（后台线程按真实时间抢跑会让断言变成抽奖）。

只有两类用例不得不碰真实线程：
  · ``TestLifecycle``：验证 start/stop 幂等，以及「去重在线程里也生效」。仅用一次
    ``time.sleep(0.25)``（< 0.3 秒），理由写在该用例的 docstring 里 —— 而且它等待的
    是「至少跑了一轮」，断言「同一原因 skips == 1」在一轮和五轮下都成立，因此不 flaky；
  · ``TestSingleFlight``：用「慢研判 + Event 握手」制造重叠，靠 Event 而不是 sleep 同步。

单位约定（很容易踩坑，所以集中写在这里）
--------------------------------------
· ``in_session(market, ts)`` 的 ``ts`` 是 **秒**（与 ``time.time()`` 同单位）；
· ``next_session_open`` 的返回值，以及 ``tick_once`` / ``status()`` 里的 ``ts`` / ``nextOpen`` /
  ``nextRunAt`` 都是 **毫秒**（与 ``storage.now_ms()`` 同单位）。
本文件里毫秒与秒的换算一律显式 ``* 1000`` / ``/ 1000``，不靠记忆（这个不对称本身
也在 TestKnownGaps 里被记录为缺陷锚点）。
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager

# 兼容「在 stock-terminal/ 下跑」与「在仓库根目录下跑」
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import logs as L           # noqa: E402  项目自带的日志器（server 注入的就是它）
from core import trader as T          # noqa: E402  被测模块
from core.storage import Store        # noqa: E402

# --------------------------------------------------------------------------- #
# 常量与时间锚点（全部用 aware UTC 构造，因此与运行机器的时区无关）
# --------------------------------------------------------------------------- #
FEE = T.DEFAULT_FEE                 # 0.0003
SLIP = T.DEFAULT_SLIPPAGE           # 0.001
CAP = T.DEFAULT_CONFIG["capital"]   # 100000.0
LOT = 100                           # A 股一手

#: 三个新配置键（本轮的增量）
NEW_KEYS = ("scheduler", "autoExecute", "ignoreMarketHours")

WED = (2026, 9, 16)     # 周三（今天）
SAT = (2026, 9, 19)     # 周六（需求指定）
SUN = (2026, 9, 20)     # 周日（需求指定）
MON = (2026, 9, 21)     # 下周一

T_CN_OPEN = None        # 占位，真正的值在 utc() 定义之后赋值（见下）
T_CN_LUNCH = None
T_CN_PM = None
T_CN_CLOSE = None
T_US_OPEN = None


def utc(y, mo, d, h, mi, s=0):
    """UTC 时间戳（**秒**）。

    用 aware datetime 而不是 ``time.mktime``：后者按本机时区解释，会让「A股 01:30 UTC 开市」
    这条断言在非 UTC 机器上直接错掉 —— 而本文件要验证的恰恰是「判定与机器时区无关」。
    """
    return int(datetime.datetime(y, mo, d, h, mi, s,
                                 tzinfo=datetime.timezone.utc).timestamp())


T_CN_OPEN = utc(*WED, 1, 40)     # 01:40 UTC = 北京 09:40（A股上午开市）
T_CN_LUNCH = utc(*WED, 4, 0)     # 04:00 UTC = 北京 12:00（午休，休市）
T_CN_PM = utc(*WED, 6, 0)        # 06:00 UTC = 北京 14:00（下午开市）
T_CN_CLOSE = utc(*WED, 8, 0)     # 08:00 UTC = 北京 16:00（已收盘）
T_US_OPEN = utc(*WED, 14, 0)     # 14:00 UTC（美股并集窗口内，开市）


# --------------------------------------------------------------------------- #
# 手算口径（故意在测试里重写一遍，而不是抄实现算出来的数字）
# --------------------------------------------------------------------------- #
def buy_math(qty, price, fee=FEE, slip=SLIP):
    """买入手算：含滑点成交价 = 报价×(1+滑点)，费用 = 成交额×费率。

    为什么重写：如果直接把实现算出来的金额抄进断言，公式一旦被改错（滑点方向、费率乘错边），
    断言会跟着一起错，等于没测。这里与 core/advisor._round_trip / trader._fill_buy 同源。
    """
    notional = qty * price * (1.0 + slip)
    fee_amt = notional * fee
    return {"price": price * (1.0 + slip), "notional": notional, "fee": fee_amt,
            "cost": notional + fee_amt, "avg": (notional + fee_amt) / qty}


# --------------------------------------------------------------------------- #
# 测试替身（确定性 / 记录调用 / 绝不联网）
# --------------------------------------------------------------------------- #
class FakeClock(object):
    """可控时钟：调度器里的「现在」全部来自它。

    调度器的三个时间相关判断（interval 是否到期、是否在交易时段、status 里的 inSession /
    nextOpen）都只读 ``self.clock()``，因此换掉 clock 之后就不必真睡 60 秒。
    """

    def __init__(self, t=T_CN_OPEN):
        self.now = float(t)

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += float(dt)
        return self.now


class FakeLog(object):
    """假日志钩子：记录 ``(level, event, fields)``，接口与 ``core.logs.Logger`` 一致。

    为什么用假 logger 而不是真实 Logger：断言要的是「写了哪一级、什么事件、带什么字段」，
    而不是日志文本；core.logs.Logger 的接口就是 ``info(event, **fields)`` / ``error(event, **fields)``。

    注意本类**刻意不可调用**（没有 ``__call__``）—— 与项目自带的 Logger 保持一致：
    ``TradeScheduler._log`` 里有一道 ``callable(self.log)`` 守卫，而 ``core.logs.Logger``
    实例不是可调用对象，这个不一致本身就是缺陷（见 TestKnownGaps）。
    """

    def __init__(self):
        self.calls = []

    def _add(self, level, event, fields):
        self.calls.append((level, event, dict(fields)))

    def info(self, event, **fields):
        self._add("info", event, fields)

    def error(self, event, **fields):
        self._add("error", event, fields)

    def levels(self, event=None):
        return [c[0] for c in self.calls if event is None or c[1] == event]

    def find(self, event):
        return [c for c in self.calls if c[1] == event]

    def count(self, event):
        return len(self.find(event))


class CallableFakeLog(FakeLog):
    """**可调用**的假日志（额外实现 ``__call__``）。

    这不是推荐用法，而是 ``_log`` 里 ``callable(self.log)`` 守卫所要求的形态：
    用它可以把「日志调用点本身是通的」与「守卫把项目自带 Logger 挡在门外」两件事分开验证 ——
    否则一旦没有日志，无法判断是没接线、还是线接错了。
    """

    def __call__(self, *args, **kwargs):
        self.calls.append(("call", "direct", {"args": list(args)}))


def advice(rows, analyzed=None):
    """构造一份 recommend 返回值（core.advisor.recommend 契约的最小集）。"""
    rows = [dict(r) for r in rows]
    return {"ok": True, "market": "cn", "horizon": T.SCAN_HORIZON, "capital": CAP,
            "requested": len(rows), "count": len(rows),
            "analyzed": len(rows) if analyzed is None else analyzed,
            "rows": rows, "reviewId": "ar-test-0001"}


class FakeRecommend(object):
    """假的研判函数：记录调用参数、返回确定性结论（可切换成「抛异常 / 返回脏值」）。

    为什么必须注入假研判：真实 ``core.advisor.recommend`` 需要 K 线抓取器（会联网），
    而调度器只关心「拿到结论之后怎么判断」——把它抽掉，测试才能既离线又确定。
    """

    def __init__(self, rows=None, analyzed=None, exc=None, raw=None):
        self.rows = list(rows or [])
        self.analyzed = analyzed
        #: 抛出的异常（None 表示正常返回）
        self.exc = exc
        #: 整体替换返回值（用于「返回非 dict / 空 dict」这类脏输入）
        self.raw = raw
        self.calls = []

    def __call__(self, symbols, **kwargs):
        self.calls.append({"symbols": list(symbols), "kwargs": dict(kwargs)})
        if self.exc is not None:
            raise self.exc
        if self.raw is not None:
            return self.raw
        return advice(self.rows, self.analyzed)


class FakeQuotes(object):
    """假报价抓取器：只记录调用，绝不联网（用于断言构造参数有没有真的接上）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, market, codes):
        self.calls.append((market, list(codes or [])))
        return {c: 1.0 for c in (codes or [])}


class FlakyStore(object):
    """包装真 Store，让**某一层**调用抛异常（验证「单次异常不拖垮调度」）。"""

    def __init__(self, store, boom_on):
        self._store = store
        self._boom = boom_on

    def __getattr__(self, name):
        if name == self._boom:
            def _boom(*args, **kwargs):
                raise RuntimeError("注入的存储故障：%s" % name)
            return _boom
        return getattr(self._store, name)


# --------------------------------------------------------------------------- #
# 研判结论行（字段与 core.advisor.recommend 的 rows 契约一致）
# --------------------------------------------------------------------------- #
def buy_row(code="600519", price=100.0, amount=10000.0, weight=0.1, confidence=0.6, **over):
    """一行「买入」结论。

    ``kelly.amount`` 给 10000（而不是只给权重）：plan_orders 优先用研判给出的**可执行金额**，
    于是股数是确定的 ``floor(10000 / price / 100) * 100``，断言不必依赖「权重 × 本金」与
    各种上限之间的夹取细节（那部分由 tests/test_trader.py 覆盖）。
    """
    row = {
        "ok": True, "code": code, "name": code, "market": "cn", "price": price,
        "changePct": 1.0, "asOf": "2026-09-16", "bars": 300,
        "action": "buy", "actionText": "买入", "score": 80.0, "confidence": confidence,
        "signals": [{"key": "ma", "label": "均线", "dir": "up", "brief": "MA5 上穿"}],
        "kelly": {"fStar": 0.3, "kind": "discrete", "weight": weight, "rawWeight": weight,
                  "amount": amount, "shares": 0, "lot": LOT},
        "plan": {"entry": price, "stop": price * 0.9, "target1": price * 1.2,
                 "target2": price * 1.3, "riskReward": 1.5},
        "forecast": {"expectedReturn": 3.2, "upProb": 0.6, "sample": 40},
        "risk": {"atrPct": 2.1, "maxDrawdown": 12.5},
        "note": "测试行",
    }
    row.update(over)
    return row


def neutral_row(code="600519", action="hold", price=100.0, **over):
    """一行中性档位（hold / watch）：明确「无需动作」，不产生委托、也不记 skip。"""
    row = buy_row(code=code, price=price, amount=0.0, weight=0.0)
    row.update({"action": action, "actionText": "持有"})
    row.update(over)
    return row


def sell_row(code="600519", price=100.0, action="sell", confidence=0.7, **over):
    """一行「卖出 / 回避」结论：用于第二轮「清仓」以验证计数器累加。"""
    row = buy_row(code=code, price=price, amount=0.0, weight=0.0, confidence=confidence)
    row.update({"action": action, "actionText": "卖出"})
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# 公共夹具
# --------------------------------------------------------------------------- #
class SchedCase(unittest.TestCase):
    """调度器用例的公共夹具：内存库 + 假时钟 + 假日志 + 假研判。

    ``Store(":memory:")`` 而不是临时文件：用例之间互不污染、也不需要清理目录
    （存储层的文件读写由 tests/test_storage.py 覆盖）。

    **但线程用例必须用 ``file_store()``**：SQLite 的 ``:memory:`` 库是「每个连接一个库」，
    而 ``Store`` 给每个线程各建一条连接（thread-local），因此调度线程读到的会是一个**空库**
    （配置全是默认值 → 表现为「静默地什么都没做」）。这不是日志问题而是夹具问题，
    所以线程用例一律换文件库，让所有线程看到同一份数据。
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        #: 默认停在 A股上午开市时刻（周三 01:40 UTC）
        self.clock = FakeClock(T_CN_OPEN)
        self.log = FakeLog()
        self.recommend = FakeRecommend([buy_row()])

    def file_store(self):
        """文件库（线程用例专用，理由见类 docstring）。"""
        tmp = tempfile.mkdtemp(prefix="sched-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(os.path.join(tmp, "strategy.db"))
        self.addCleanup(store.close)
        return store

    # ------------------------------------------------------------ 夹具 --
    def set_cfg(self, **over):
        """写配置并返回归一化后的配置。

        默认「三层里开了两层」（enabled + scheduler，autoExecute 关）：
        这是「自动出计划但不自动成交」的推荐姿势，大多数用例只需在此基础上改一两项。
        """
        patch = {"enabled": True, "scheduler": True, "autoExecute": False,
                 "mode": "dryrun", "market": "cn", "universe": ["600519"], "interval": 60}
        patch.update(over)
        return T.save_config(self.store, patch)

    def scheduler(self, recommend=None, store=None, log=None, **over):
        """构造调度器（step 给大值：避免线程在用例里空转，需要线程的用例自己传小 step）。"""
        s = T.TradeScheduler(self.store if store is None else store,
                             self.recommend if recommend is None else recommend,
                             clock=self.clock, step=3600.0,
                             log=self.log if log is None else log, **over)
        self.addCleanup(s.stop)      # 保险：任何用例都不留下活着的调度线程
        return s

    # ------------------------------------------------------------ 断言辅助 --
    def json_ok(self, obj, label=""):
        """公开返回值必须能 ``json.dumps(allow_nan=False)``：NaN / inf 会让前端解析直接失败。"""
        try:
            return json.dumps(obj, allow_nan=False)
        except (TypeError, ValueError) as exc:   # pragma: no cover - 失败即断言
            self.fail("%s 无法 json.dumps(allow_nan=False)：%s" % (label, exc))

    def fingerprint(self, mode="dryrun", market="cn"):
        """账户指纹：回答「钱有没有动」的比对基准。

        只取与资金有关的字段（现金 / 持仓 / 累计已实现盈亏 / 手续费总额 / 权益点数量），
        **刻意不含 updatedAt** —— 时间戳变化不等于钱动了，把它算进来会让「dryrun 不动钱」
        变成必然失败的假阳性断言。
        """
        aid = T.account_id(market, mode)
        state = self.store.get_trade_state(aid) or {}
        meta = self.store.meta_get(T.TRADE_META_PREFIX + aid, {}) or {}
        points = self.store.list_trade_equity(aid, limit=500) or []
        return json.dumps({
            "cash": state.get("cash"),
            "positions": state.get("positions") or [],
            "realizedPnl": meta.get("realizedPnl"),
            "feeTotal": meta.get("feeTotal"),
            "equityPoints": len(points),
        }, sort_keys=True, allow_nan=False)

    def orders(self, status=None):
        res = self.store.list_trade_orders(status=status, limit=100) or {}
        return list(res.get("rows") or [])


# --------------------------------------------------------------------------- #
# A. 交易时段（UTC 判定）
# --------------------------------------------------------------------------- #
class TestSessionCn(SchedCase):
    """A股：四个时刻 + 四个边界。"""

    def test_cn_intraday_moments(self):
        """A股四个时刻：北京 09:40 开市、12:00 午休、14:00 开市、16:00 已收盘。

        为什么用 UTC 写断言而不是「北京时间」：判定函数本身就是按 UTC 定义时段
        （01:30–03:30 / 05:00–07:00），用同一套口径写断言才不会出现「测试里偷偷帮实现
        做了时区换算，实现换算错了也测不出来」。
        """
        self.assertTrue(T.in_session("cn", T_CN_OPEN), "北京 09:40（01:40 UTC）应开市")
        self.assertFalse(T.in_session("cn", T_CN_LUNCH), "北京 12:00（04:00 UTC）午休应休市")
        self.assertTrue(T.in_session("cn", T_CN_PM), "北京 14:00（06:00 UTC）应开市")
        self.assertFalse(T.in_session("cn", T_CN_CLOSE), "北京 16:00（08:00 UTC）应已收盘")

    def test_cn_boundaries_are_left_closed_right_open(self):
        """边界左闭右开：01:30 开、03:30 收、05:00 开、07:00 收（前后各多验一分钟）。

        为什么专门盯边界：相邻一分钟就决定「今天扫不扫」。写成闭区间会让 03:30 多扫一轮，
        写成开区间会让 01:30 这一分钟漏扫（开盘瞬间的行情恰恰是最关心的）。
        """
        cases = [
            (utc(*WED, 1, 29), False, "开盘前一分钟"),
            (utc(*WED, 1, 30), True, "上午开盘（左闭）"),
            (utc(*WED, 3, 29), True, "上午收盘前一分钟"),
            (utc(*WED, 3, 30), False, "上午收盘（右开）"),
            (utc(*WED, 5, 0), True, "下午开盘（左闭）"),
            (utc(*WED, 6, 59), True, "下午收盘前一分钟"),
            (utc(*WED, 7, 0), False, "下午收盘（右开）"),
        ]
        for ts, want, label in cases:
            self.assertIs(T.in_session("cn", ts), want, label)


class TestSessionWeekend(unittest.TestCase):
    """周末：中/美两市全天休市。"""

    def test_weekend_closed_all_day(self):
        """周六 2026-09-19 / 周日 2026-09-20 全天休市（两个市场都是）。

        为什么挑这两个日期：需求指定的锚点，而且它们跨过了「周六 01:30 UTC」这种
        「只看分钟数就会误判为开市」的时刻 —— 若实现忘了判星期，这里会立刻红。
        """
        for day, label in ((SAT, "周六"), (SUN, "周日")):
            for h, m in ((1, 30), (2, 0), (6, 0), (13, 30), (20, 59)):
                ts = utc(*day, h, m)
                self.assertFalse(T.in_session("cn", ts),
                                 "%s %02d:%02d UTC 不应被判定为 A股开市" % (label, h, m))
                self.assertFalse(T.in_session("us", ts),
                                 "%s %02d:%02d UTC 不应被判定为 美股开市" % (label, h, m))


class TestSessionUs(unittest.TestCase):
    """美股：13:30–21:00 UTC 的并集窗口。"""

    def test_us_union_window(self):
        """美股按 13:30–21:00 UTC 的**并集**判定：13:29 之前与 21:00 之后休市。

        为什么断言的是并集（而不是夏/冬令时各自的真实时段）：实现故意取
        「夏令时 13:30–20:00 ∪ 冬令时 14:30–21:00」再多放宽的并集，因为没有交易日历与
        夏令时库 —— 漏扫一整天才致命，多扫一次最坏只是「拿到与上一交易日相同的静态行情」。
        把有意为之的放宽写进断言，才能避免后来者以为这是 bug 而「顺手收紧」。
        """
        self.assertFalse(T.in_session("us", utc(*WED, 13, 29)), "13:29 UTC 尚未开盘")
        self.assertTrue(T.in_session("us", utc(*WED, 13, 30)), "13:30 UTC（夏令时开盘）应开市")
        self.assertTrue(T.in_session("us", T_US_OPEN), "14:00 UTC 在窗口内")
        self.assertTrue(T.in_session("us", utc(*WED, 20, 59)), "20:59 UTC 在窗口内")
        self.assertFalse(T.in_session("us", utc(*WED, 21, 0)), "21:00 UTC 应收盘（右开）")
        self.assertFalse(T.in_session("us", utc(*WED, 8, 0)),
                         "08:00 UTC（A股收盘）美股尚未开盘：两个市场的时段不能串台")


class TestSessionDirtyAndNextOpen(unittest.TestCase):
    """脏 market 不抛异常；next_session_open 必须指向「未来且真的开市」。"""

    def test_dirty_market_never_raises(self):
        """脏 market（None / 大写 / 'US' / 未知串 / 数字 / dict / list / 任意对象）不抛异常。

        为什么：``status()`` 会把配置里的原值直接喂给 ``in_session``，一旦抛异常就是
        ``/api/trade/status`` 直接 500 —— 用户打开「自动交易」页就白屏，而问题只是
        配置里少了个 market。语义上：'US' 前缀走美股口径，其余一律回退 A股口径。
        """
        for dirty in (None, "", "CN", "cn", "xx", "??", "  cn  ", 123, 1.5,
                      {"a": 1}, ["us"], object()):
            got = T.in_session(dirty, T_CN_OPEN)
            self.assertIsInstance(got, bool, "脏 market=%r 必须返回 bool" % (dirty,))
        # 'us' 前缀（大小写混合）按美股口径：01:40 UTC 美股休市、14:00 UTC 美股开市
        self.assertFalse(T.in_session("US", T_CN_OPEN))
        self.assertTrue(T.in_session("US", T_US_OPEN))
        self.assertTrue(T.in_session("Us", T_US_OPEN))
        # 未知串回退 A股口径
        self.assertTrue(T.in_session("不存在的市场", T_CN_OPEN))
        self.assertFalse(T.in_session("不存在的市场", T_CN_LUNCH))

    def test_next_session_open_is_future_and_really_open(self):
        """``next_session_open`` 的两条硬性质：① 严格未来；② 该时刻 ``in_session`` 为真。

        为什么断言这两条而不是某个固定数字：它是给界面显示「下次什么时候扫」用的，
        只要指向一个真的开市的未来时刻就不会骗用户（界面上一个错的时间比空白更糟）。

        单位提醒：返回值是**毫秒**，``in_session`` 收**秒** —— 这里显式 ``/ 1000``；
        这个不对称本身在 TestKnownGaps 里有专门的缺陷锚点。
        """
        for market in ("cn", "us"):
            for ts, label in ((T_CN_LUNCH, "A股午休"), (T_CN_CLOSE, "A股收盘后"),
                              (utc(*SAT, 12, 0), "周六"), (utc(*SUN, 23, 59), "周日深夜")):
                nxt = T.next_session_open(market, ts)
                self.assertIsNotNone(nxt, "%s / %s 应能算出下次开市" % (market, label))
                self.assertGreater(nxt, ts * 1000, "%s / %s：必须是未来时刻" % (market, label))
                self.assertTrue(T.in_session(market, nxt / 1000),
                                "%s / %s：返回时刻必须真的开市" % (market, label))

    def test_next_session_open_from_weekend_is_monday(self):
        """极端输入（整周唯一没有交易时段的两天）也要能算出**下周一**的开市时间。

        为什么断言精确值：实现是「逐分钟向前试，最多找 7 天」。若把「周末要跳到周一」写错
        （例如只扫一天、或把周末也算进循环上界之外），界面上「下次开市」就会是空白或周日；
        精确值断言能一次性锁住这个推算。
        """
        self.assertEqual(T.next_session_open("cn", utc(*SAT, 12, 0)),
                         utc(*MON, 1, 30) * 1000, "周六 → 下周一 01:30 UTC（A股开盘）")
        self.assertEqual(T.next_session_open("cn", utc(*SUN, 0, 0)),
                         utc(*MON, 1, 30) * 1000, "周日 → 下周一 01:30 UTC（A股开盘）")
        self.assertEqual(T.next_session_open("us", utc(*SUN, 0, 0)),
                         utc(*MON, 13, 30) * 1000, "周日 → 下周一 13:30 UTC（美股开盘）")

    def test_dirty_market_in_next_session_open(self):
        """``next_session_open`` 遇到脏 market 同样不抛异常，且与 A股口径一致。"""
        want = T.next_session_open("cn", utc(*SAT, 12, 0))
        for dirty in (None, "", "xx", 123, {"a": 1}, object()):
            self.assertEqual(T.next_session_open(dirty, utc(*SAT, 12, 0)), want)


class TestSessionTimezoneIndependence(unittest.TestCase):
    """判定只与传入的 ts 有关，与本机时区无关。"""

    @contextmanager
    def zone(self, tz):
        """临时切换进程时区（用 TZ + time.tzset()），退出时无条件还原。

        为什么要在进程内切换：调度器跑在用户机器上，时区可能是 Asia/Shanghai、UTC 或
        America/New_York。若实现里混进了 ``time.localtime`` / ``mktime`` 这类本地时区调用，
        同一时刻会得出不同结论，夜间/跨日的扫描窗口整体错位 —— 这种问题在开发机上
        （作者本机时区）永远看不出来。
        """
        old = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_verdict_depends_only_on_ts(self):
        """同一组固定 ts 在四个时区下结果必须逐条相同（只用固定 ts，不碰 ``time.time()``）。

        为什么不用 now()：用当前时间做断言，用例的成败会随「几点跑测试」而变；
        固定 ts 才能把「时区无关」这件事本身变成可复现的断言。
        """
        if not hasattr(time, "tzset"):
            self.skipTest("本平台不支持 time.tzset，无法在进程内切换时区")
        samples = [T_CN_OPEN, T_CN_LUNCH, T_CN_PM, T_CN_CLOSE, T_US_OPEN,
                   utc(*WED, 13, 30), utc(*WED, 21, 0), utc(*SAT, 3, 0), utc(*WED, 0, 0)]
        base = {(m, ts): T.in_session(m, ts) for m in ("cn", "us") for ts in samples}
        base_next = {m: T.next_session_open(m, T_CN_LUNCH) for m in ("cn", "us")}
        for tz in ("UTC", "Asia/Shanghai", "America/New_York", "Pacific/Kiritimati"):
            with self.zone(tz):
                for (m, ts), want in base.items():
                    self.assertIs(T.in_session(m, ts), want,
                                  "时区 %s 下 in_session(%r, %d) 结论漂移" % (tz, m, ts))
                for m, want in base_next.items():
                    self.assertEqual(T.next_session_open(m, T_CN_LUNCH), want,
                                     "时区 %s 下 next_session_open(%r) 结果漂移" % (tz, m))


# --------------------------------------------------------------------------- #
# B. 三层闸门与跳过原因
# --------------------------------------------------------------------------- #
class TestGateSkipReasons(SchedCase):
    """三层闸门与四类跳过原因：每种都要写 ``lastSkip``。"""

    def test_scheduler_off_skips(self):
        """scheduler=false → skip，原因含「定时调度未启用」，且 ``lastSkip`` 被写入。

        为什么必须有 lastSkip：界面（web/js/views/trade.js 的 skipReason）回答
        「为什么现在没动作」时，优先级是 lastError → lastSkip → 三层开关。
        只返回 reason 而不落 lastSkip 的话，用户刷新一次页面原因就消失了。
        """
        self.set_cfg(scheduler=False)
        s = self.scheduler()
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("定时调度未启用", res["reason"])
        self.assertEqual(s.last_skip, res["reason"])
        self.assertEqual(s.status()["lastSkip"], res["reason"], "status 必须能读到原因")
        self.assertEqual(s.runs, 0)
        self.assertEqual(self.recommend.calls, [], "被跳过时不得触碰上游（离线）")

    def test_enabled_off_skips(self):
        """scheduler=true 但 enabled=false → skip，原因含「自动交易总开关未开启」。

        为什么要和上一层分开断言：用户常犯的错是「开了定时调度、忘了开自动交易」，
        原因文案必须直接指出是哪一层没开（否则用户只会看到「什么都没发生」）。
        """
        cfg = self.set_cfg(enabled=False, scheduler=True)
        self.assertTrue(cfg["scheduler"])
        s = self.scheduler()
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("自动交易总开关未开启", res["reason"])
        self.assertEqual(s.last_skip, res["reason"])
        self.assertEqual(self.recommend.calls, [])

    def test_empty_universe_skips(self):
        """标的池为空（universe 与 whitelist 都空）→ skip，原因含「标的池为空」。

        为什么调度层要自己再判一次（scan 里也判）：scan 那次判断会产出一份「空计划」，
        调度层若不拦，界面会显示「已生成 0 笔计划」—— 用户看不出是「没填标的」还是
        「研判这轮没结论」，而这是两种完全不同的处置。
        """
        self.set_cfg(universe=[], whitelist=[])
        s = self.scheduler()
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("标的池为空", res["reason"])
        self.assertEqual(s.last_skip, res["reason"])
        self.assertEqual(self.recommend.calls, [], "池子空的时候不该打上游接口")

    def test_out_of_session_skips_with_next_open(self):
        """非交易时段 → skip，原因含「非交易时段」，并**在跳过返回值里**给出 nextOpen。

        为什么 nextOpen 要放在跳过返回值里（而不只是 status）：用户在「立即试跑一次」
        的响应里就能直接看到「现在不是交易时段，下次 13:00」，不用再发一次状态请求。
        """
        self.clock.now = T_CN_LUNCH
        self.set_cfg(ignoreMarketHours=False)
        s = self.scheduler()
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("非交易时段", res["reason"])
        self.assertEqual(s.last_skip, res["reason"])
        self.assertEqual(res.get("nextOpen"), utc(*WED, 5, 0) * 1000,
                         "午休时下次开市应是 05:00 UTC（北京 13:00）")
        self.assertEqual(self.recommend.calls, [])
        self.assertIsNone(s.last_error, "「没到点」不是错误，不该写 lastError")


class TestGateOrder(SchedCase):
    """三层闸门是有序的。"""

    def test_scheduler_gate_reported_first(self):
        """scheduler 关着时应先报「定时调度未启用」，**不**因 enabled 也关着而改报总开关。

        为什么在意顺序：一份配置可能同时缺两层开关，用户是照着界面提示逐个打开的。
        若顺序漂移（例如先判 enabled），用户会先被指去开总开关，开完仍然一动不动，
        白白多一轮来回；而且这类「顺序漂移」在人工联调里极难发现。
        """
        self.set_cfg(enabled=False, scheduler=False)
        s = self.scheduler()
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("定时调度未启用", res["reason"])
        self.assertNotIn("自动交易总开关", res["reason"], "第一层没开时不该越过它去报第二层")
        self.assertIsNone(s.last_error)
        self.assertEqual(s.status()["lastSkip"], res["reason"])

    def test_force_reports_enabled_gate(self):
        """force 绕过调度开关后，缺的下一层必须报出来（顺序在 force 下依然成立）。"""
        self.set_cfg(enabled=False, scheduler=False)
        s = self.scheduler()
        res = s.tick_once(force=True)
        self.assertEqual(res["action"], "skip")
        self.assertIn("自动交易总开关未开启", res["reason"])


class TestSkipDedup(SchedCase):
    """同一原因连续跳过只计一次（本轮修掉的噪声问题）。"""

    def test_same_reason_counted_once(self):
        """连续 5 次同原因跳过 → ``skips == 1``，但 ``lastSkip`` 始终有值。

        为什么：调度线程默认每 5 秒醒一次，「非交易时段」这种整晚成立的原因若每次都累加，
        一晚上就能堆到上万次，``skips`` 这个指标会彻底失去意义（联调时半小时就被顶起来过）。
        但 ``lastSkip`` 必须每次刷新 —— 界面上「为什么没动作」永远要有答案。
        """
        self.set_cfg(scheduler=False)
        s = self.scheduler()
        reasons = []
        for _ in range(5):
            res = s.tick_once()
            self.assertEqual(res["action"], "skip")
            reasons.append(res["reason"])
            self.assertEqual(s.last_skip, res["reason"], "lastSkip 不能被去重吃掉")
        self.assertEqual(len(set(reasons)), 1, "本用例的前提：五次原因完全相同")
        self.assertEqual(s.skips, 1, "同一原因连续跳过只计一次")
        self.assertEqual(self.recommend.calls, [])

    def test_reason_change_counts_again(self):
        """原因变一次就再计一次（去重是「同原因」，不是「全局只记一次」）。"""
        self.set_cfg(enabled=False, scheduler=False)
        s = self.scheduler()
        s.tick_once()
        self.assertEqual(s.skips, 1)
        T.save_config(self.store, {"scheduler": True})      # 原因换成「总开关未开启」
        res = s.tick_once()
        self.assertIn("自动交易总开关未开启", res["reason"])
        self.assertEqual(s.skips, 2)
        s.tick_once()                                        # 同一原因第三次出现
        self.assertEqual(s.skips, 2, "换过原因之后，同原因的重复依然只计一次")

    def test_same_reason_recounted_after_a_real_run(self):
        """真正跑过一轮后，同一个原因再出现要**重新计数**（噪声修复的回归用例）。

        为什么：跑过一轮说明这期间调度确实做了动作，此后再回到同一原因是一段新的情况；
        若被去重吃掉，会出现「昨天跳过 3 次、今天又跳过」被合并成一条的错觉，
        也会让「这一晚到底被卡住几次」无法回答。

        复现路径：scheduler=false 跳过（skips=1）→ 打开开关真跑一轮 → 再关掉，
        同一条「定时调度未启用」应把 skips 推到 2。
        """
        self.set_cfg(scheduler=False)
        s = self.scheduler()
        first = s.tick_once()
        self.assertIn("定时调度未启用", first["reason"])
        self.assertEqual(s.skips, 1)

        T.save_config(self.store, {"scheduler": True})       # enabled / universe 已是 true
        ran = s.tick_once()
        self.assertEqual(ran["action"], "planned", "打开开关后应真的跑一轮")
        self.assertEqual(s.runs, 1)
        self.assertIsNone(s.last_skip, "跑过一轮后不应还留着旧的跳过原因")
        self.assertIsNone(s.last_error)

        T.save_config(self.store, {"scheduler": False})
        again = s.tick_once()
        self.assertIn("定时调度未启用", again["reason"])
        self.assertEqual(s.skips, 2, "跑过一轮之后，同原因的跳过应重新计数")


class TestIntervalThrottle(SchedCase):
    """间隔限流：未到 interval → quiet 跳过（不计 skips）。"""

    def test_interval_skip_is_quiet(self):
        """跑过一轮后未到 interval → skip（原因含「未到调度间隔」）且**不计入 skips**。

        为什么 quiet：这是调度器对**上游**（研判接口）的自我保护，不是「因为外部条件没动」。
        把它计入 skips，会让「跳过次数」这个指标在 interval=3600 时每小时多出几十条噪声，
        与「同原因只计一次」的初衷相冲突。但 ``lastSkip`` 仍要写，界面才解释得清。
        """
        self.set_cfg(interval=60)
        s = self.scheduler()
        first = s.tick_once()
        self.assertEqual(first["action"], "planned")
        self.assertEqual(len(self.recommend.calls), 1)

        self.clock.advance(5)                    # 只过了 5 秒
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("未到调度间隔", res["reason"])
        self.assertEqual(s.skips, 0, "节流跳过不写 skips（quiet）")
        self.assertEqual(s.last_skip, res["reason"], "但 lastSkip 必须写")
        self.assertEqual(s.runs, 1)
        self.assertEqual(len(self.recommend.calls), 1, "限流的意义：上游最多每 interval 打一次")

    def test_interval_boundary_and_elapsed(self):
        """``interval - 1`` 秒仍跳过；恰好到 ``interval`` 秒即放行（边界左闭右开）。

        为什么用注入时钟：interval 最小 15 秒、默认 60 秒，真睡会让用例慢到不可接受；
        推进假时钟 61 秒既有同样的验证效果，又不依赖真实时间。
        """
        self.set_cfg(interval=60)
        s = self.scheduler()
        s.tick_once()
        self.assertEqual(s.runs, 1)

        self.clock.advance(59)
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("未到调度间隔", res["reason"])

        self.clock.advance(1)                    # 恰好 60 秒
        res2 = s.tick_once()
        self.assertNotEqual(res2["action"], "skip", "到点必须放行")
        self.assertEqual(s.runs, 2, "到点后应重新执行一轮")
        self.assertEqual(s.skips, 0, "限流期间一次都不该写 skips")


class TestForceSemantics(SchedCase):
    """``force=True``（界面的「立即试跑一次」）的语义。"""

    def test_force_bypasses_scheduler_and_interval(self):
        """force 跳过「调度开关」与「间隔限流」：调度关着、不推进时钟也能连着试跑两次。

        为什么要这个后门：用户点「立即试跑一次」就是想立刻确认链路通不通，被「调度开关关着」
        挡住会让按钮变成摆设。但 force 绕不过 enabled，也绕不过 dryrun（下面两个用例）。

        注意本用例**必须在交易时段内**跑：当前实现里 force 并没有绕过交易时段判断，
        关市时刻 force 依旧会被跳过（这是缺陷，见 TestKnownGaps.test_force_bypasses_market_hours）。
        """
        self.set_cfg(enabled=True, scheduler=False, ignoreMarketHours=False)
        s = self.scheduler()
        first = s.tick_once(force=True)          # 时钟停在 A股开市时刻
        self.assertNotEqual(first["action"], "skip", "force 下不该因调度关闭被跳过")
        self.assertEqual(s.runs, 1)
        second = s.tick_once(force=True)         # 不推进时钟，仍在 interval 内
        self.assertNotEqual(second["action"], "skip", "force 下不该被间隔限流挡住")
        self.assertEqual(s.runs, 2)
        self.assertEqual(s.skips, 0)

    def test_force_still_requires_enabled(self):
        """``enabled=false`` + force → 仍然是 skip（总开关是硬约束，任何路径都不能绕）。

        为什么这条最要紧：force 是从 HTTP 接口进来的（POST /api/trade/scheduler {once:true}），
        如果它能绕过总开关，那「默认不出手」这条安全默认就被一个按钮废掉了。
        """
        self.set_cfg(enabled=False, scheduler=False)
        s = self.scheduler()
        res = s.tick_once(force=True)
        self.assertEqual(res["action"], "skip")
        self.assertIn("自动交易总开关未开启", res["reason"])
        self.assertEqual(self.recommend.calls, [], "总开关关着时连研判都不该打")
        self.assertEqual(s.runs, 0)

    def test_force_dryrun_never_fills(self):
        """dryrun + autoExecute=true + force → 仍然不成交（dryrun 是硬约束，force 也不例外）。

        为什么必须单独验：这是「试跑」最容易被误解的一点 —— 用户可能以为「试跑会真的走一遍
        成交」，于是要么看着账户没变以为链路坏了，要么以为试跑能成交而不敢点。
        """
        cfg = self.set_cfg(mode="dryrun", autoExecute=True, scheduler=False)
        T.ensure_account(self.store, cfg, "cn")  # 先建账，避免「建账」被误算成「动了钱」
        before = self.fingerprint()
        s = self.scheduler()
        res = s.tick_once(force=True)
        self.assertEqual(res["action"], "planned")
        self.assertIs(res["execute"], False)
        self.assertEqual(res["filled"], 0)
        self.assertEqual(self.fingerprint(), before, "force + dryrun 也不许改账户")
        self.assertTrue(self.orders(), "计划还是要落库的（否则界面看不到「试跑产出」）")
        self.assertTrue(all(o["status"] == "pending" for o in self.orders()))


class TestIgnoreMarketHours(SchedCase):
    """``ignoreMarketHours`` 开关。"""

    def test_ignore_market_hours_runs_out_of_session(self):
        """ignoreMarketHours=true → 非交易时段（周末）也执行（演示 / 回放用）。

        为什么保留这个口子：节假日或周末想演示自动化、或按历史数据回放时，没有它就只能
        改系统时间。文档（SCHEDULER_NOTE）也把这个开关的用途写清楚了。
        """
        self.clock.now = utc(*SAT, 12, 0)
        self.set_cfg(ignoreMarketHours=True)
        s = self.scheduler()
        res = s.tick_once()
        self.assertEqual(res["action"], "planned")
        self.assertEqual(s.runs, 1)
        self.assertIs(res["execute"], False, "autoExecute 默认关闭，ignore 只是解除时段限制")

    def test_market_hours_on_skips_with_next_open(self):
        """ignoreMarketHours=false（默认）→ 非交易时段跳过，并给出 nextOpen。

        为什么默认必须是 false：默认只在交易时段动作，才不会在凌晨用「上一交易日收盘价」
        反复生成同一批计划（虽然不会错成交，但会污染当日委托计数与审计）。
        """
        self.clock.now = utc(*SAT, 12, 0)
        self.set_cfg(ignoreMarketHours=False)
        s = self.scheduler()
        res = s.tick_once()
        self.assertEqual(res["action"], "skip")
        self.assertIn("非交易时段", res["reason"])
        self.assertEqual(res["nextOpen"], utc(*MON, 1, 30) * 1000)
        self.assertIsNone(s.last_error, "时段问题不是错误")
        self.assertEqual(self.recommend.calls, [])


# --------------------------------------------------------------------------- #
# C. 计划与成交
# --------------------------------------------------------------------------- #
class TestDryrun(SchedCase):
    """dryrun：只出计划、绝不动钱。"""

    def test_dryrun_plans_only_and_keeps_account_intact(self):
        """dryrun → 产出 pending 委托、``filled == 0``、账户现金与持仓**逐字段不变**。

        为什么用「前后指纹比对」而不是只看返回值里的 filled：filled 只是返回值，账户才是真相。
        dryrun 下动钱是本项目最不可接受的缺陷（会污染模拟盘的历史与统计），
        而「账户没变」这件事只能靠前后快照比对来证明。
        注意 ``autoExecute`` 这里故意开着 —— 用来证明 **mode 才是成交的最终裁决者**。
        """
        cfg = self.set_cfg(mode="dryrun", autoExecute=True)
        T.ensure_account(self.store, cfg, "cn")      # 先建账：否则「建账」会被误算成「动了钱」
        before = self.fingerprint("dryrun")
        s = self.scheduler()
        res = s.tick_once()
        after = self.fingerprint("dryrun")

        self.assertEqual(res["action"], "planned")
        self.assertEqual(res["orders"], 1)
        self.assertEqual(res["filled"], 0)
        self.assertFalse(res["execute"])
        self.assertEqual(before, after, "dryrun 不得改动现金 / 持仓 / 累计量 / 权益点")

        rows = self.orders()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["mode"], "dryrun")
        self.assertTrue(rows[0]["qty"] > 0)
        self.assertIn("未自动成交", res["note"], "note 必须写明没成交")
        self.assertIn("dryrun 模式不成交", res["note"], "并且要指出是 dryrun 而非开关没开")


class TestPaper(SchedCase):
    """paper：autoExecute 决定「自动出计划」还是「自动成交」。"""

    def test_paper_auto_execute_fills_and_matches_manual_math(self):
        """paper + autoExecute=true → 成交：状态 filled、持仓出现、现金减少，且**金额手算一致**。

        手算口径（与 trader._fill_buy 同源，故意在测试里重写）：
        含滑点成交价 = 报价×(1+滑点)，成交额 = 股数×含滑点价，费用 = 成交额×费率，
        现金减少 = 成交额 + 费用，持仓均价 = (成交额 + 费用)/股数。
        容差 1e-6：实现里对金额做过 round(…, 6)，除此之外两边应当逐位相同。
        """
        cfg = self.set_cfg(mode="paper", autoExecute=True)
        T.ensure_account(self.store, cfg, "cn")
        before = self.store.get_trade_state(T.account_id("cn", "paper"))["cash"]
        s = self.scheduler()
        res = s.tick_once()

        self.assertEqual(res["action"], "executed")
        self.assertEqual(res["filled"], 1)
        self.assertTrue(res["execute"])

        rows = self.orders("filled")
        self.assertEqual(len(rows), 1)
        order = rows[0]
        self.assertEqual(order["status"], "filled")
        qty, price = int(order["qty"]), 100.0
        math = buy_math(qty, price)
        self.assertAlmostEqual(order["fillPrice"], math["price"], delta=1e-6)
        self.assertAlmostEqual(order["amount"], math["notional"], delta=1e-6)
        self.assertAlmostEqual(order["fee"], math["fee"], delta=1e-6)

        state = self.store.get_trade_state(T.account_id("cn", "paper"))
        self.assertAlmostEqual(state["cash"], before - math["cost"], delta=1e-6,
                               msg="现金减少额必须等于手算的「成交额 + 费用」")
        self.assertEqual(len(state["positions"]), 1)
        pos = state["positions"][0]
        self.assertEqual(pos["code"], "600519")
        self.assertEqual(int(pos["qty"]), qty)
        self.assertAlmostEqual(pos["avgPrice"], math["avg"], delta=1e-6)
        # 权益曲线只在成交时追加（execute_orders 的口径）：成交一笔 → 恰好多一个权益点
        self.assertEqual(len(self.store.list_trade_equity(T.account_id("cn", "paper"))), 1)

    def test_paper_without_auto_execute_only_plans(self):
        """paper + autoExecute=false → 只生成计划、不成交。

        为什么必须单独验证：这是「自动出计划」与「自动成交」的分界线。两者混淆的后果很严重 ——
        用户以为只是「看着方便」，账户却被自动改了钱（或反过来以为在成交，其实一直是空跑）。
        """
        cfg = self.set_cfg(mode="paper", autoExecute=False)
        T.ensure_account(self.store, cfg, "cn")
        before = self.fingerprint("paper")
        s = self.scheduler()
        res = s.tick_once()

        self.assertEqual(res["action"], "planned")
        self.assertFalse(res["execute"])
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["orders"], 1)
        self.assertEqual(self.fingerprint("paper"), before, "没开自动成交就不许改账户")
        self.assertEqual([o["status"] for o in self.orders()], ["pending"])
        self.assertIn("autoExecute 未开启", res["note"])


class TestNoActionable(SchedCase):
    """无可执行档位 / 全量失败：不要把「没结论」显示成「已生成 0 笔计划」。"""

    def test_all_neutral_is_planned_not_error(self):
        """假研判只给 hold / watch → 0 委托，但 ``action == 'planned'``（不是 error）。

        为什么不能报错：hold / watch 是**明确结论**（「无需动作」），属于正常工作状态；
        把它记成错误会让用户以为系统坏了，进而去改配置 —— 而其实什么也不用改。
        """
        self.set_cfg()
        rec = FakeRecommend([neutral_row("600519", "hold"), neutral_row("000001", "watch")])
        s = self.scheduler(recommend=rec)
        res = s.tick_once()
        self.assertEqual(res["action"], "planned")
        self.assertEqual(res["orders"], 0)
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["skipped"], 0, "中性档位不计入 skipped")
        self.assertEqual(res["result"]["analyzed"], 2)
        self.assertEqual(self.orders(), [])
        self.assertIsNone(s.last_error, "中性档位不是错误")
        self.assertEqual(s.runs, 1)

    def test_zero_analyzed_is_error(self):
        """``analyzed == 0``（上游一只都没研判出来）→ ``action == 'error'`` + lastError。

        回归点：曾把这种全量失败显示成「已生成 0 笔计划」—— 用户会以为自动化在正常工作，
        这是最危险的静默失败。判定依据是 adviceSummary.analyzed（而不是 orders 数量）。
        """
        self.set_cfg()
        rec = FakeRecommend([], analyzed=0)
        s = self.scheduler(recommend=rec)
        res = s.tick_once()
        self.assertEqual(res["action"], "error")
        self.assertTrue(s.last_error, "必须写下 lastError，界面上才会有解释")
        self.assertEqual(s.status()["lastError"], s.last_error)
        self.assertEqual(res["symbols"], 1)
        self.assertEqual(self.orders(), [], "全量失败不该留下任何委托")
        self.assertEqual(s.runs, 1, "失败的一轮也算跑过（runs 是「尝试次数」）")

    def test_scan_error_field_is_error(self):
        """``res['error']`` 非空（研判内部失败）→ ``action == 'error'``。

        为什么与上一条分开：错误可能来自两个不同位置（scan 的 error 字段 / analyzed 为 0），
        两条路径都要收敛成 error，否则漏掉其中一条就会回到「静默失败」。
        """
        self.set_cfg()
        rec = FakeRecommend(exc=RuntimeError("上游取数超时"))
        s = self.scheduler(recommend=rec)
        res = s.tick_once()
        self.assertEqual(res["action"], "error")
        self.assertIn("超时", s.last_error or "")
        self.assertEqual(self.orders(), [])


class TestRecommendFailures(SchedCase):
    """研判函数异常 / 脏返回值：不抛异常、不失效。"""

    def test_recommend_raises_is_error_and_scheduler_recovers(self):
        """``recommend_fn`` 抛异常 → error + lastError，且**下一次 tick 仍能正常执行**。

        为什么要专门验「下一次还能跑」：一次网络抖动就让「自动」永久失效、而用户毫无察觉，
        是自动化系统最难排查的故障。同一条约束也写在类 docstring 里（异常只进 lastError）。
        注意：失败的一轮同样消耗 interval（本轮已设置 last_run_at），
        因此这里把时钟推进 61 秒 —— 这也顺带固定了「失败也限流」这个行为。
        """
        self.set_cfg(interval=60)
        rec = FakeRecommend(exc=RuntimeError("连接被重置"))
        s = self.scheduler(recommend=rec)
        first = s.tick_once()
        self.assertEqual(first["action"], "error")
        self.assertIn("连接被重置", s.last_error or "")
        self.assertIsNone(s.last_skip)

        self.clock.advance(61)
        rec.exc = None                      # 上游恢复
        rec.rows = [buy_row()]
        second = s.tick_once()
        self.assertEqual(second["action"], "planned", "上游恢复后必须能继续工作")
        self.assertEqual(s.runs, 2)
        self.assertIsNone(s.last_error, "成功后应清掉旧的错误")
        self.assertEqual(len(self.orders()), 1)

    def test_recommend_dirty_return_never_raises(self):
        """``recommend_fn`` 返回非 dict / 空 dict → 不抛异常（记成 error 即可）。

        为什么：研判函数是外部注入的（server 里是 partial 过的真实现），返回值形态一旦跑偏，
        调度线程若直接崩掉就会「永久静默」。这里要求「最坏也只是记一条错误」。
        """
        for raw, label in ((None, "None"), ({}, "空 dict"), ("字符串", "字符串"),
                           (123, "数字"), ([], "空 list")):
            store = Store(":memory:")            # 每个脏值一份干净库，避免互相干扰
            self.addCleanup(store.close)
            T.save_config(store, {"enabled": True, "scheduler": True, "mode": "dryrun",
                                  "market": "cn", "universe": ["600519"], "interval": 60})
            s = T.TradeScheduler(store, FakeRecommend(raw=raw), clock=self.clock,
                                 step=3600.0, log=self.log)
            self.addCleanup(s.stop)
            res = s.tick_once()
            self.json_ok(res, "脏返回值 %s 的 tick 结果" % label)
            self.assertEqual(res["action"], "error", "%s 应记错误" % label)
            self.assertTrue(s.last_error, "%s 必须留下 lastError" % label)
            left = store.list_trade_orders(limit=50) or {}
            self.assertEqual(list(left.get("rows") or []), [], "%s 不该留下委托" % label)


class TestSingleFlight(SchedCase):
    """单次调度不重叠。"""

    def test_lock_held_reports_busy(self):
        """锁被占住时立刻返回「上一次调度尚未结束」，不排队、不并发（确定性版本）。

        为什么必须「立刻返回」：调度线程每 step 秒醒一次，若第二次选择等锁，
        扫描耗时长时会堆起一串待执行的 tick；直接跳过才能保证「上游最多被一个 tick 打」。
        这里直接借用内部锁（不改实现），把「别人正在跑」变成一个确定事实。
        """
        self.set_cfg()
        s = self.scheduler()
        self.assertTrue(s._lock.acquire(blocking=False), "测试自己占住调度锁")
        try:
            res = s.tick_once()
        finally:
            s._lock.release()
        self.assertEqual(res["action"], "skip")
        self.assertIn("上一次调度尚未结束", res["reason"])
        self.assertEqual(s.last_skip, "上一次调度尚未结束")
        self.assertEqual(self.recommend.calls, [], "重叠被拒时绝不触碰上游")

        res2 = s.tick_once()                 # 锁放开后照常工作
        self.assertEqual(res2["action"], "planned")

    def test_concurrent_tick_is_rejected(self):
        """真并发：慢研判（Event 握手）占住锁，另一个线程 tick 必须拿到「上一次调度尚未结束」。

        为什么用 Event 而不是 sleep 猜时长：sleep 在慢机器 / 高负载下会 flaky，而 Event
        让「第一个 tick 已经进入研判」成为确定事实。

        为什么要换文件库：``:memory:`` 库每连接一个（Store 是 thread-local 连接），
        另一个线程里的 tick 会读到空配置（enabled=False）而直接跳过 —— 那样这个用例就
        测不到「锁」而只是在测「空库」。
        """
        store = self.file_store()
        T.save_config(store, {"enabled": True, "scheduler": True, "mode": "dryrun",
                              "market": "cn", "universe": ["600519"], "interval": 60})
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow_recommend(symbols, **kwargs):
            calls.append(list(symbols))
            entered.set()
            release.wait(1.5)                # 只等上限，避免用例挂死
            return advice([buy_row()])

        s = self.scheduler(recommend=slow_recommend, store=store)
        holder = {}

        def first_tick():
            holder["res"] = s.tick_once()

        worker = threading.Thread(target=first_tick, name="test-slow-tick")
        worker.start()
        try:
            self.assertTrue(entered.wait(1.0), "第一个 tick 未能在预期时间内进入研判")
            res = s.tick_once()
            self.assertEqual(res["action"], "skip")
            self.assertIn("上一次调度尚未结束", res["reason"])
            self.assertEqual(len(calls), 1, "重叠期间上游只应被调用一次")
        finally:
            release.set()
            worker.join(2.0)
        self.assertFalse(worker.is_alive(), "慢 tick 应已结束")
        self.assertEqual(holder["res"]["action"], "planned")
        self.assertEqual(s.runs, 1)


# --------------------------------------------------------------------------- #
# D. 状态与生命周期
# --------------------------------------------------------------------------- #
class TestStatusContract(SchedCase):
    """``status()`` 的字段契约。"""

    STATUS_KEYS = ("running", "startedAt", "step", "enabled", "scheduler", "autoExecute",
                   "mode", "market", "interval", "inSession", "ignoreMarketHours",
                   "nextOpen", "nextRunAt", "lastRunAt", "lastResult", "lastSkip",
                   "lastError", "runs", "planned", "filled", "skips", "note")

    def test_status_fields_complete(self):
        """``status()`` 的 22 个字段一个都不能少，且初始值语义正确。

        为什么锁字段名：server 直接把 status() 原样透传给前端（/api/trade/status 的
        scheduler 段），缺字段不会报错，只会让界面上某一项永远显示 undefined ——
        这种「静默缺字段」只有靠字段集断言才能提前发现。
        """
        self.set_cfg()
        s = self.scheduler()
        st = s.status()
        for key in self.STATUS_KEYS:
            self.assertIn(key, st, "status() 缺少字段 %s" % key)
        self.assertEqual(set(st), set(self.STATUS_KEYS))
        self.assertIs(st["running"], False)
        self.assertIs(st["enabled"], True)
        self.assertIs(st["scheduler"], True)
        self.assertIs(st["autoExecute"], False)
        self.assertEqual(st["mode"], "dryrun")
        self.assertEqual(st["market"], "cn")
        self.assertEqual(st["interval"], 60)
        self.assertIs(st["inSession"], True, "夹具时钟停在 A股开市时刻")
        self.assertIs(st["ignoreMarketHours"], False)
        self.assertIsNone(st["nextRunAt"])
        self.assertIsNone(st["lastRunAt"])
        self.assertIsNone(st["lastResult"])
        self.assertIsNone(st["lastSkip"])
        self.assertIsNone(st["lastError"])
        self.assertEqual((st["runs"], st["planned"], st["filled"], st["skips"]), (0, 0, 0, 0))
        self.assertTrue(st["note"], "note 必须带口径说明（前端直接展示）")

    def test_last_result_after_run(self):
        """跑过一轮后 ``lastResult`` 要含 at/market/symbols/analyzed/orders/skipped/filled/
        execute/mode —— 这是界面「上一轮干了什么」的唯一数据源。"""
        self.set_cfg(mode="paper", autoExecute=True, universe=["600519", "000001"])
        rec = FakeRecommend([buy_row("600519", 100.0), buy_row("000001", 20.0)])
        s = self.scheduler(recommend=rec)
        res = s.tick_once()
        last = res["result"]
        self.assertEqual(set(last), {"at", "market", "symbols", "analyzed", "orders",
                                     "skipped", "filled", "execute", "mode"})
        self.assertEqual(last["market"], "cn")
        self.assertEqual(last["symbols"], 2)
        self.assertEqual(last["analyzed"], 2)
        self.assertEqual(last["orders"], 2)
        self.assertEqual(last["filled"], 2)
        self.assertIs(last["execute"], True)
        self.assertEqual(last["mode"], "paper")
        self.assertEqual(last["at"], res["ts"])
        self.assertEqual(s.status()["lastResult"], last)

    def test_next_run_at_uses_interval(self):
        """``nextRunAt`` = 上次运行时刻 + interval×1000（毫秒），状态接口据此显示「下次预计」。"""
        self.set_cfg(interval=120)
        s = self.scheduler()
        res = s.tick_once()
        st = s.status()
        self.assertEqual(st["lastRunAt"], res["ts"])
        self.assertEqual(st["nextRunAt"], res["ts"] + 120 * 1000)


class TestLifecycle(SchedCase):
    """start / stop 的幂等与「线程只是外壳」。

    这三个用例都用**文件库**：``:memory:`` 库是「每连接一个库」，调度线程会读到空配置，
    于是「线程到底跑了什么」就无法验证（详见 SchedCase 的 docstring）。
    """

    def sched_threads(self):
        return [t for t in threading.enumerate()
                if t.name == "trade-scheduler" and t.is_alive()]

    def test_start_is_idempotent(self):
        """连续两次 ``start()``：running() 为 True，但**只存在一个**调度线程。

        为什么必须验证「只有一个线程」：server.py 每次保存配置都会调用 start()。
        若 start() 每次新建线程，用户每改一次配置就多一个调度线程，扫描频率成倍增长，
        且线程之间互相抢锁（表现为「有时候没动作」）—— 这类问题在人工点几下时很难发现。
        """
        store = self.file_store()
        T.save_config(store, {"enabled": True, "scheduler": False, "mode": "dryrun",
                              "market": "cn", "universe": ["600519"], "interval": 60})
        s = T.TradeScheduler(store, self.recommend, clock=self.clock,
                             step=3600.0, log=self.log)
        self.addCleanup(s.stop)
        self.assertFalse(s.running())
        s.start()
        first_started = s.started_at
        s.start()                            # 幂等调用
        self.assertTrue(s.running())
        self.assertEqual(len(self.sched_threads()), 1, "第二次 start() 不该再起线程")
        self.assertEqual(s.started_at, first_started, "幂等的 start() 不该刷新 startedAt")
        self.assertIsNotNone(s.started_at)
        self.assertEqual(s.status()["running"], True)

    def test_stop_is_idempotent_and_logic_still_usable(self):
        """``stop()`` 幂等（重复调用不抛异常、running() 为 False），且 stop 后 ``tick_once`` 仍可用。

        为什么在意「停线程后还能手动 tick」：线程只是外壳，判断逻辑必须与线程解耦 ——
        server 的「立即试跑」在调度关闭时也要能回答「为什么没动作」（此时不会有线程）。
        """
        store = self.file_store()
        T.save_config(store, {"enabled": True, "scheduler": True, "mode": "dryrun",
                              "market": "cn", "universe": ["600519"], "interval": 60})
        s = T.TradeScheduler(store, self.recommend, clock=self.clock,
                             step=3600.0, log=self.log)
        self.addCleanup(s.stop)
        s.start()
        s.stop()
        self.assertFalse(s.running())
        s.stop()                             # 重复 stop 不抛异常
        self.assertFalse(s.running())
        self.assertEqual(self.sched_threads(), [], "stop() 之后不该还有调度线程")
        # 线程停了，逻辑照旧：这里用 force 是因为线程第一轮已经真的跑过一次
        # （last_run_at 已被写入），不 force 会被 interval 限流挡住，测不到「逻辑还能用」
        res = s.tick_once(force=True)
        self.assertEqual(res["action"], "planned")
        self.assertTrue(s.runs >= 1)

    def test_thread_really_ticks_and_dedups(self):
        """真线程 + 真实时间：step=0.05 秒时线程会 tick 若干轮，同一原因仍只计一次。

        为什么允许这一次 0.25 秒的真实等待（< 0.3 秒）：这是唯一能证明「去重逻辑在线程里
        同样生效」的办法（噪声问题最早就是在联调线程里被发现的）。断言「skips == 1」
        在「只跑了一轮」和「跑了五轮」下都成立，因此不会因为机器快慢而 flaky。
        """
        store = self.file_store()
        T.save_config(store, {"enabled": True, "scheduler": False, "mode": "dryrun",
                              "market": "cn", "universe": ["600519"], "interval": 60})
        s = T.TradeScheduler(store, self.recommend, clock=self.clock,
                             step=0.05, log=self.log)
        self.addCleanup(s.stop)
        s.start()
        time.sleep(0.25)                     # 让调度线程多醒几轮（理由见 docstring）
        self.assertTrue(s.running())
        self.assertEqual(s.skips, 1, "同一原因在线程里也只计一次")
        self.assertIn("定时调度未启用", s.last_skip or "")
        self.assertEqual(s.runs, 0)
        self.assertEqual(self.recommend.calls, [], "调度关闭时线程不该打上游")
        s.stop()
        self.assertFalse(s.running())


class TestCounters(SchedCase):
    """计数器自洽（多轮）。"""

    def test_counters_accumulate_over_rounds(self):
        """``planned`` / ``filled`` 等于**累计**生成与成交的委托数（两轮 tick 断言）。

        为什么：这两个数字在界面上是「自动交易干了多少活」的唯一直接指标。若只统计最后一轮、
        或把跳过也算进去，用户对自动化的判断就会失真（例如「本轮 0 笔」被显示成「共 0 笔」）。
        做法：两轮（买入 2 只 → 清仓 2 只），把每轮返回值加总与被测计数器比对，
        并与存储里的委托单数量交叉验证。
        """
        self.set_cfg(mode="paper", autoExecute=True, universe=["600519", "000001"])
        rec = FakeRecommend([buy_row("600519", 100.0), buy_row("000001", 20.0)])
        s = self.scheduler(recommend=rec)

        r1 = s.tick_once()
        planned, filled = r1["orders"], r1["filled"]
        self.assertEqual((planned, filled), (2, 2), "第一轮：两只都开仓成交")

        self.clock.advance(61)                                # 越过 interval
        rec.rows = [sell_row("600519", 100.0), sell_row("000001", 20.0)]
        r2 = s.tick_once()
        planned += r2["orders"]
        filled += r2["filled"]
        self.assertEqual((r2["orders"], r2["filled"]), (2, 2), "第二轮：两只都清仓成交")

        self.assertEqual(s.runs, 2)
        self.assertEqual(s.planned, planned, "planned 应等于两轮生成的委托数之和")
        self.assertEqual(s.filled, filled, "filled 应等于两轮成交数之和")
        self.assertEqual(s.planned, 4)
        self.assertEqual(s.filled, 4)
        self.assertEqual(len(self.orders()), 4, "存储里的委托单数量也应一致")
        self.assertEqual(len(self.orders("filled")), 4)
        self.assertEqual(s.skips, 0)

    def test_counters_untouched_by_skips(self):
        """跳过不写入 planned / filled（否则「干了多少活」会被「没动作」污染）。"""
        self.set_cfg(scheduler=False)
        s = self.scheduler()
        for _ in range(3):
            s.tick_once()
        self.assertEqual((s.planned, s.filled, s.runs), (0, 0, 0))
        self.assertEqual(s.skips, 1)


class TestOnResult(SchedCase):
    """``on_result`` 回调（推送 / 外发的接线点）。"""

    def test_on_result_receives_res_and_cfg(self):
        """回调必须拿到 ``(res, cfg)``：res 用于推送内容，cfg 是**当次生效**的配置。

        注意回调拿到的是 **scan() 的结果**（含 orders / adviceSummary / gates / account），
        而不是 ``tick_once()`` 返回的那份摘要 —— server 的 ``_on_schedule_result`` 正是按
        scan 结果的字段推单（``for order in res.get("orders")``）。这里把两种结构都钉住，
        避免以后有人以为「回调拿的是 tick 的返回值」，从而在推送里读不到 orders。

        为什么把 cfg 一起给回调（而不是让它自己去读库）：回调通常立刻把结果推出去，
        若它再读一次配置，配置可能在这中间被改，推出去的内容与实际执行口径不一致。
        """
        self.set_cfg()
        got = []
        s = self.scheduler(on_result=lambda res, cfg: got.append((res, cfg)))
        res = s.tick_once()
        self.assertEqual(len(got), 1)
        scan_res, cfg = got[0]
        self.assertEqual(len(scan_res["orders"]), 1, "回调应拿到 scan 结果里的委托明细")
        self.assertEqual(scan_res["adviceSummary"]["analyzed"], 1)
        self.assertIn("gates", scan_res)
        self.assertIn("account", scan_res)
        self.assertEqual(cfg["mode"], "dryrun")
        self.assertEqual(cfg["universe"], ["600519"])
        self.assertEqual(cfg["interval"], 60)
        self.assertIn("orders", res)

        # 跳过时不应推送「空结果」：否则前端会收到一条毫无信息量的事件
        T.save_config(self.store, {"scheduler": False})
        s.tick_once()
        self.assertEqual(len(got), 1, "跳过不该触发 on_result")

    def test_on_result_exception_does_not_break_scheduling(self):
        """回调抛异常不影响调度：推送 / 外发失败不能拖垮自动化。

        为什么单独测：回调是外部接线（SSE / webhook），最容易出问题。一旦异常冒泡，
        server 的「立即试跑」接口会 500，调度线程也会白记一次失败 —— 而实际上这一轮的
        研判与计划都是成功的，不该被一次推送失败抹掉。
        """
        self.set_cfg()
        calls = []

        def boom(res, cfg):
            calls.append(res)
            raise RuntimeError("推送通道挂了")

        s = self.scheduler(on_result=boom)
        res = s.tick_once()
        self.assertEqual(res["action"], "planned", "回调异常不该改变本轮结论")
        self.assertEqual(len(calls), 1)
        self.assertEqual(s.runs, 1)
        self.assertIsNone(s.last_error, "回调失败不是调度失败，不该写 lastError")
        self.assertEqual(s.status()["lastResult"]["orders"], 1)

        self.clock.advance(61)               # 下一次 tick 照常工作
        res2 = s.tick_once()
        self.assertEqual(res2["action"], "planned")
        self.assertEqual(s.runs, 2)
        self.assertEqual(len(calls), 2)


class TestLogHook(SchedCase):
    """``log`` 钩子。

    这里有两层：**调用点**（执行 / 跳过 / 异常 / 启停分别写什么级别与字段）与
    **守卫**（``_log`` 里 ``callable(self.log)`` 的判断）。前者用可调用的假 logger 验证
    （守卫要求的形态），后者见 TestKnownGaps.test_log_hook_is_used_with_project_logger ——
    项目自带的 ``core.logs.Logger`` 不是可调用对象，因此它当前的收不到任何调度日志。
    """

    def test_non_callable_logger_also_receives_logs(self):
        """回归②：非 callable 的 log（即项目自带的 ``core.logs.Logger``）也必须收到日志。

        守卫曾经写成 ``if not callable(self.log): return``，而 ``core.logs.Logger`` 提供
        ``info``/``error`` 方法但并不实现 ``__call__`` —— 于是服务端注入的真实 logger
        一条调度日志都收不到：功能看起来一切正常，事后却查不到「它做过什么」。
        这类「静默无输出」比报错难查得多，因此把「必须收到」钉成断言。
        """
        self.set_cfg()
        s = self.scheduler()
        s.start()
        s.stop()
        s.tick_once()
        T.save_config(self.store, {"scheduler": False})
        s.tick_once()
        events = [e for _, e, _ in self.log.calls]
        self.assertIn("scheduler.started", events)
        self.assertIn("scheduler.stopped", events)
        self.assertIn("scheduler.tick", events)
        self.assertIn("scheduler.skip", events)

    def test_callable_logger_receives_run_skip_and_error(self):
        """传「可调用 + 有 info/error 方法」的 logger 时：执行 / 跳过 / 异常都写日志。

        为什么这样造 logger：``_log`` 的守卫要求 ``callable(self.log)``，因此只有可调用对象
        才进得来。用它能验证「调用点本身是通的」—— 执行写 info ``scheduler.tick``（带
        market / orders / filled / analyzed 字段），跳过写 info ``scheduler.skip`` 且同原因不重复写。

        本用例**只做顺序调用、不起线程**：线程会引入「哪一 tick 先跑」的不确定性，
        而这里要断言的是日志条数（线程行为由 TestLifecycle 覆盖）。
        """
        logs = CallableFakeLog()
        self.set_cfg()
        s = self.scheduler(log=logs)
        s.tick_once()
        run_logs = logs.find("scheduler.tick")
        self.assertEqual(len(run_logs), 1)
        level, event, fields = run_logs[0]
        self.assertEqual(level, "info")
        self.assertEqual(fields["market"], "cn")
        self.assertEqual(fields["orders"], 1)
        self.assertEqual(fields["filled"], 0)
        self.assertEqual(fields["analyzed"], 1)

        self.clock.advance(61)
        T.save_config(self.store, {"scheduler": False})
        s.tick_once()
        self.assertEqual(logs.count("scheduler.skip"), 1, "首次跳过要写日志")
        s.tick_once()
        self.assertEqual(logs.count("scheduler.skip"), 1, "同一原因不重复写（不刷屏）")
        self.assertEqual(logs.levels("scheduler.skip"), ["info"])

    def test_callable_logger_error_paths_and_lifecycle(self):
        """异常路径写 **error** 级别；start / stop 写 info。

        为什么区分级别：一次全量取数失败必须能在日志里被筛出来（error），
        而不是只在界面上闪一句 note —— 静默失败是最危险的失效方式。

        本用例用文件库：``:memory:`` 库是「每连接一个」，调度线程会读到空配置，
        于是线程那一 tick 的日志会变成「读不到配置的跳过」，把断言变成噪声。
        """
        store = self.file_store()
        logs = CallableFakeLog()
        T.save_config(store, {"enabled": True, "scheduler": False, "mode": "dryrun",
                              "market": "cn", "universe": ["600519"], "interval": 60})
        s = self.scheduler(store=store, log=logs)
        s.start()
        s.stop()
        self.assertEqual(logs.levels("scheduler.started"), ["info"])
        self.assertEqual(logs.levels("scheduler.stopped"), ["info"])

        # 研判抛异常（scan 内部吞成 error 字段）→ error 级别
        T.save_config(store, {"enabled": True, "scheduler": True})
        s.recommend_fn = FakeRecommend(exc=RuntimeError("研判炸了"))
        res = s.tick_once(force=True)            # force 绕开调度开关与时段，走到 scan
        self.assertEqual(res["action"], "error")
        self.assertEqual(self.log.levels("scheduler.scan_empty"), [])
        self.assertEqual(logs.levels("scheduler.scan_empty"), ["error"])
        self.assertTrue(logs.find("scheduler.scan_empty")[0][2].get("error"))

    def test_callable_logger_error_on_store_failure(self):
        """``scan`` 真的抛异常时写 error ``scheduler.scan_failed``（存储故障注入）。

        为什么用「注入故障的 store」：scan 内部把研判异常吞成了 error 字段，
        真正「异常冒到 tick」的分支只能靠存储层故障触发；这条分支决定 lastError 与日志，
        不覆盖就等于没测异常兜底。
        """
        logs = CallableFakeLog()
        self.set_cfg()
        flaky = FlakyStore(self.store, boom_on="save_trade_order")
        s = self.scheduler(store=flaky, log=logs)   # 只有「落库委托」这一层会炸
        res = s.tick_once()
        self.assertEqual(res["action"], "error")
        self.assertIn("注入的存储故障", s.last_error or "")
        self.assertEqual(logs.levels("scheduler.scan_failed"), ["error"])

    def test_log_hook_failure_is_swallowed(self):
        """日志钩子自己抛异常也不能影响调度（外部接线最不可靠）。

        为什么：日志后端挂了（磁盘满 / 落点崩了）不该连带把自动交易停掉 ——
        这与「推送失败不影响调度」是同一类保护。
        """
        class BadLog(object):
            def __call__(self, *args, **kwargs):
                raise RuntimeError("日志后端挂了")

            def info(self, event, **fields):
                raise RuntimeError("日志后端挂了")

            def error(self, event, **fields):
                raise RuntimeError("日志后端挂了")

        self.set_cfg(scheduler=False)        # 线程每轮只会「跳过」，不抢着扫描
        s = self.scheduler(log=BadLog())
        s.start()
        s.stop()                             # start / stop 里的日志调用同样会抛，也要被吞掉
        res = s.tick_once(force=True)        # force 绕开调度开关，走到 scan
        self.assertEqual(res["action"], "planned")
        self.assertIsNone(s.last_error, "日志失败不该被当成调度失败")


class TestJsonSafety(SchedCase):
    """所有 tick_once / status 返回值都可 JSON 序列化。"""

    def test_all_return_paths_are_json_safe(self):
        """跳过 / 计划 / 成交 / 错误四条路径 + status() 都要能 ``json.dumps(allow_nan=False)``。

        为什么把四条路径都过一遍：它们的字段集不同（跳过带 nextOpen、错误带 result、
        成交带 result/note），只测成功路径会漏掉 NaN 与不可序列化对象混进来的分支。
        NaN / inf 会让前端 JSON.parse 直接失败（本项目历史上出现过一次）。
        """
        # 1) 跳过：调度关闭
        self.set_cfg(scheduler=False)
        s = self.scheduler()
        self.json_ok(s.tick_once(), "跳过（调度关闭）")
        self.json_ok(s.tick_once(force=True), "跳过（总开关关闭）")
        self.json_ok(s.status(), "status（跳过之后）")

        # 2) 计划 + 成交
        self.set_cfg(mode="paper", autoExecute=True)
        s2 = self.scheduler()
        planned = s2.tick_once()
        self.json_ok(planned, "成交")
        self.json_ok(s2.status(), "status（成交之后）")

        # 3) 错误（研判抛异常）
        self.set_cfg()
        s3 = self.scheduler(recommend=FakeRecommend(exc=RuntimeError("炸")))
        self.json_ok(s3.tick_once(), "错误（研判异常）")

        # 4) 非交易时段跳过（带 nextOpen）
        self.clock.now = utc(*SAT, 12, 0)
        self.set_cfg(ignoreMarketHours=False)
        s4 = self.scheduler()
        res = s4.tick_once()
        self.json_ok(res, "跳过（非交易时段）")
        self.json_ok(s4.status(), "status（周末）")


# --------------------------------------------------------------------------- #
# E. 配置
# --------------------------------------------------------------------------- #
class TestConfigKeys(SchedCase):
    """三个新配置键的归一化。"""

    def test_new_keys_default_false(self):
        """三个新键默认全 False，且字段集与 DEFAULT_CONFIG 完全一致。

        为什么默认必须 False：默认「装好也不会自己下单」是这个模块最重要的一条安全属性；
        新增一个默认 True 的调度开关，等于让所有已部署的实例在升级后自动开始扫盘。
        """
        cfg = T.default_config()
        for key in NEW_KEYS:
            self.assertIn(key, cfg)
            self.assertIs(cfg[key], False, "%s 默认必须是 False" % key)
        self.assertEqual(set(cfg), set(T.DEFAULT_CONFIG))
        for key in NEW_KEYS:
            self.assertIs(T.DEFAULT_CONFIG[key], False)

    def test_truthy_strings_become_true(self):
        """字符串真值（'1' / 'true' / 'yes' / 'on'）能转 True。

        为什么必须支持字符串：配置来自 HTTP 表单 / 环境变量 / curl，值往往是字符串，
        只认 Python 的 True 会让「界面上明明打开了，服务端却是 False」这种问题反复出现。
        """
        for word in ("1", "true", "TRUE", "True", "yes", "YES", "on", "ON", "y", "是", "开"):
            for key in NEW_KEYS:
                cfg = T.normalize_config({key: word})
                self.assertIs(cfg[key], True, "%s=%r 应转成 True" % (key, word))

    def test_illegal_values_fall_back_false(self):
        """非法值回退 False（不抛异常）；0/False/关 类词也一律是 False。

        为什么要覆盖「看起来像真值但不是」的输入：'maybe' / '2' / 随机对象都必须落回安全侧
        （False），而不是被 ``bool()`` 蒙成 True —— 配置项上的错误方向必须永远是「更安全」。
        """
        for bad in ("maybe", "2", "-1", "null", "none", "off", "0", "", "   ", "no",
                    None, [], {}, tuple(), object()):
            for key in NEW_KEYS:
                cfg = T.normalize_config({key: bad})
                self.assertIs(cfg[key], False, "%s=%r 应回退 False" % (key, bad))
        for key in NEW_KEYS:
            self.assertIs(T.normalize_config({key: False})[key], False)
            self.assertIs(T.normalize_config({key: 0})[key], False)
            self.assertIs(T.normalize_config({key: True})[key], True)
            self.assertIs(T.normalize_config({key: 1})[key], True)

    def test_unknown_keys_dropped_and_roundtrip(self):
        """未在 DEFAULT_CONFIG 里的键仍被丢弃；新键经 store 存取一轮后值不变。

        为什么在意「丢弃未知键 + 回读一致」：配置整体以 JSON 存库、由前端按字段读，
        结构一旦漂移（多出字段或类型变了），前端会读到自己不认识的形态；
        而调度开关的布尔化必须在**落库之后**依然是 bool（否则界面上会出现 "yes" 这种值）。
        """
        cfg = T.normalize_config({"schedulerx": True, "auto_execute": True, "interval2": 30,
                                  "scheduler": "yes"})
        self.assertEqual(set(cfg), set(T.DEFAULT_CONFIG))
        self.assertNotIn("schedulerx", cfg)
        self.assertNotIn("auto_execute", cfg)
        self.assertIs(cfg["scheduler"], True)

        saved = T.save_config(self.store, {"scheduler": "on", "autoExecute": "1",
                                           "ignoreMarketHours": "no"})
        for key in NEW_KEYS:
            self.assertIsInstance(saved[key], bool, "%s 落库前后都应是 bool" % key)
        back = T.get_config(self.store)
        self.assertIs(back["scheduler"], True)
        self.assertIs(back["autoExecute"], True)
        self.assertIs(back["ignoreMarketHours"], False)
        self.assertEqual(set(back), set(T.DEFAULT_CONFIG))


# --------------------------------------------------------------------------- #
# Z. 缺陷锚点（按要求不修改 core/trader.py，改用 expectedFailure 固化「期望行为」）
# --------------------------------------------------------------------------- #
class TestKnownGaps(SchedCase):
    """已发现、但**按要求不修改 core/trader.py** 的缺陷锚点。

    ``unittest.expectedFailure`` 的语义：下面的断言在「缺陷被修好之后」会变成
    unexpected success（xpass）—— 那时把装饰器删掉，它们就直接变成回归用例。
    因此每条断言写的都是**期望行为**，而不是当前行为。
    当前行为另有普通用例覆盖（见每条 docstring 里的「当前行为」）。

    六个锚点：
      1. ``test_force_bypasses_market_hours``        force 没绕过交易时段（与 server 注释矛盾）
      2. ``test_log_hook_is_used_with_project_logger``  log 钩子被 callable 守卫挡死
      3. ``test_market_override_is_honored``         构造参数 market 被忽略
      4. ``test_injected_fetch_quotes_is_used``      构造参数 fetch_quotes 从未被调用
      5. ``test_in_session_accepts_next_session_open_result`` 秒 / 毫秒单位不一致
      6. ``test_busy_skip_also_dedups``              锁竞争路径绕过了跳过去重
    """

    def test_force_bypasses_market_hours(self):
        """缺陷 1（影响最直接）：``force=True`` **没有**绕过交易时段判断。

        复现：把时钟停在周六 12:00 UTC，``scheduler=false``、``enabled=true``，
        调 ``tick_once(force=True)`` → 当前返回
        ``skip / 「非交易时段（cn）；ignoreMarketHours 可关掉此判断」``；期望直接执行。

        为什么这是缺陷而不是「设计如此」：``server.api_trade_scheduler`` 的 docstring 明写
        「试跑（force）的语义：跳过『调度开关 / 间隔 / 交易时段』三项限制」，前端按钮也写着
        「立即试跑一次：确认链路通不通」。实现里只有 interval 那一行带了 ``not force``，
        时段那一行没有 —— 于是**用户在周末 / 收盘后点「试跑」，得到的永远是「非交易时段」**，
        恰好把「试跑」最需要的场景（非交易时段检查链路）堵死了。

        建议修法：把时段判断改成
        ``if not force and not cfg.get("ignoreMarketHours") and not in_session(...)``
        （并在返回的 note 里说明「这是 force 试跑，不在交易时段」）；
        或者反过来把 server / 前端的文案改成「试跑也要在交易时段内」，二者必须一致。
        """
        self.clock.now = utc(*SAT, 12, 0)
        self.set_cfg(enabled=True, scheduler=False, ignoreMarketHours=False)
        s = self.scheduler()
        res = s.tick_once(force=True)
        self.assertNotEqual(res["action"], "skip", "force 试跑不该被非交易时段挡住")

    def test_log_hook_is_used_with_project_logger(self):
        """缺陷 2：``_log`` 的 ``callable(self.log)`` 守卫把项目自带的日志器挡在门外。

        复现：用项目自己的 ``core.logs.Logger(None)``（只写内存缓冲、不落盘；server 注入的
        ``runner().logger`` 就是它的实例）作为 ``log=``，跑一轮 start / stop / tick / skip：
        当前 ``logger.count == 0``、``logger.ring() == []`` —— **一条调度日志都没有**，
        而这段代码明明在每个分支都调了 ``self._log(...)``。

        根因：``_log`` 的守卫要求 ``callable(self.log)``，但方法体走的是
        ``getattr(self.log, level)(event, **fields)``（对象 + 方法），而 ``core.logs.Logger``
        没有 ``__call__``（``callable(...) is False``）。两者只有一个能成立，
        于是真实链路里「启动 / 停止 / 跳过 / 扫描失败 / 扫到空」全部静默丢失 ——
        这是最难察觉的一类失效：功能看起来正常，只是再也查不到「它做过什么」。

        建议修法（择一）：
        ① 把守卫换成 ``if self.log is None: return``（同时兼容 callable 与对象两种形态）；
        ② 或按 callable 契约调用 ``self.log(level, event, **fields)`` 并把 server 的注入
           改成 ``lambda level, event, **f: getattr(runner().logger, level)(event, **f)``。
        建议配一条 ``core.logs.Logger`` 的集成断言（本用例就是它）。
        """
        logger = L.Logger(None)               # 内存缓冲，不落盘
        self.assertFalse(callable(logger), "前提：项目自带的 Logger 不是可调用对象")
        self.set_cfg()
        s = self.scheduler(log=logger)
        s.start()
        s.stop()
        s.tick_once()
        T.save_config(self.store, {"scheduler": False})
        s.tick_once()
        self.assertTrue(logger.count > 0, "调度器应当在 Logger 里留下日志")
        events = [rec.get("event") for rec in logger.ring()]
        self.assertIn("scheduler.tick", events)
        self.assertIn("scheduler.skip", events)

    def test_market_override_is_honored(self):
        """缺陷 3：``TradeScheduler(..., market="us")`` 的 market 参数被静默忽略。

        复现：配置 ``market="cn"``，构造时传 ``market="us"``，在 A股开市时刻
        （周三 01:40 UTC）tick 一次 —— 期望按美股口径判定「非交易时段」而跳过，
        当前行为是按 cn 口径直接执行（``planned``）。实现里 ``self.market_override``
        只被赋值、从未被读取（``_tick_locked`` / ``status`` 都用 ``cfg["market"]``）。

        影响：这是一个「看起来能注入、实际不起作用」的参数。server.py 现在传的是
        ``market=None``，所以线上没暴露；但任何调用方想用它固定扫描市场都会无声失效，
        排查时会怀疑配置、怀疑上游，就是不会怀疑一个「已经传进去的参数」。

        建议修法：``_tick_locked`` / ``status`` 里统一用
        ``market = _market(self.market_override or cfg.get("market"))``
        （或删掉该参数，不给接线方虚假的期待）。
        """
        self.set_cfg(market="cn", ignoreMarketHours=False)
        s = self.scheduler(market="us")
        res = s.tick_once()                       # 01:40 UTC 对美股是休市
        self.assertEqual(res["action"], "skip")
        self.assertIn("非交易时段", res["reason"])

    def test_injected_fetch_quotes_is_used(self):
        """缺陷 4：构造参数 ``fetch_quotes`` 从未被调用（server 注入的实时报价被丢掉）。

        复现：注入一个记录调用的假报价抓取器，paper + autoExecute 跑一轮，
        当前 ``fetched.calls == []``（期望：成交前按最新报价取数）。

        影响：server.py 明确传了 ``fetch_quotes=trade_quotes``，接线方会认为「自动成交用的
        是最新报价」；实际成交价来自**研判快照价**（``scan`` 用 advice 行里的 price 当报价）。
        行情在研判与成交之间变动时，模拟成交价会偏（金额/均价都不对），
        审计里也看不出「这是快照价还是最新价」。

        建议修法：``tick_once`` 里 ``quotes = self.fetch_quotes(market, symbols)``（失败回退
        快照价），并把 quotes 传进 ``execute_orders``（或给 ``scan`` 增加 quotes 入参）；
        若决定不接，就删掉这个参数以免误导。
        """
        self.set_cfg(mode="paper", autoExecute=True)
        fetched = FakeQuotes()
        s = self.scheduler(fetch_quotes=fetched)
        s.tick_once()
        self.assertTrue(fetched.calls, "注入的 fetch_quotes 应被调用")

    def test_in_session_accepts_next_session_open_result(self):
        """缺陷 5：``next_session_open`` 返回**毫秒**，``in_session`` 收**秒**，
        两者最自然的组合会抛 ``ValueError``，而不是返回 bool。

        复现：``T.in_session("cn", T.next_session_open("cn", T_CN_LUNCH))``
        → ``ValueError: year 58691 is out of range``（毫秒被当成秒）。

        影响：同一模块里同一个概念两套单位，调用方只要漏一次 ``*1000`` / ``/1000``，
        轻则结论反了（把 1970 年当今天），重则直接抛异常 —— 而 ``in_session`` 目前没有任何
        「只接受秒」的校验或命名提示。``TestSessionDirtyAndNextOpen`` 里之所以要显式写
        ``/ 1000``，就是因为实现没有把这件事表达出来。

        建议修法（择一）：① ``in_session`` 内部把 > 1e11 的时间戳按毫秒处理
        （``if stamp > 1e11: stamp //= 1000``）；② 明确命名 ``in_session_ms`` /
        ``next_session_open_s``，并在 docstring 里写清单位；③ 让 ``next_session_open``
        的返回单位与 ``in_session`` 的入参一致（毫秒是前端要的，可在 status 里再乘 1000）。
        """
        nxt = T.next_session_open("cn", T_CN_LUNCH)
        self.assertTrue(T.in_session("cn", nxt))

    def test_busy_skip_also_dedups(self):
        """缺陷 6（轻微）：锁被占用时的「上一次调度尚未结束」**每次都累加 skips**，且不写日志。

        复现：连续两次在锁被占住的情况下 ``tick_once()`` → ``skips == 2``。
        期望（与 ``_skip()`` 的口径一致）：同一原因连续出现只计一次，并写一条 skip 日志。

        影响：这条路径绕过了 ``_skip()`` 的去重与日志逻辑，是「同原因只计一次」这个
        不变式上唯一的例外；一旦扫描变慢（上游变慢或 step 小于扫描耗时），skips 会被这类
        内部节流顶起来，正是当初想去掉的噪声。

        建议修法：把这条 return 也走 ``self._skip("上一次调度尚未结束")``
        （注意它发生在**未持锁**的情况下，``_skip`` 不碰锁，所以是安全的）。
        """
        self.set_cfg()
        s = self.scheduler()
        self.assertTrue(s._lock.acquire(blocking=False))
        try:
            s.tick_once()
            s.tick_once()
        finally:
            s._lock.release()
        self.assertEqual(s.skips, 1, "同一原因（重叠被拒）连续出现应只计一次")
        self.assertEqual(self.log.count("scheduler.skip"), 1, "重叠被拒也应写一条日志")


if __name__ == "__main__":
    unittest.main(verbosity=2)
