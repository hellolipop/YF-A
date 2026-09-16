# -*- coding: utf-8 -*-
"""core.advisor（AI 选股引擎）的单元测试（仅用标准库 unittest，可直接 python 运行）。

覆盖范围（与需求 15 组断言一一对应）
------------------------------------
  1. TestResponseContract   响应契约：顶层字段 / 每行字段 / action 取值域 / 投票明细；
  2. TestUnits              单位口径：百分数 vs 小数（含 expectedReturn 的 100 倍关系核对）；
  3. TestJsonStrictness     JSON 严格性：allow_nan=False 可序列化、递归检查无 inf/NaN；
  4. TestDirection          建议档位与数据方向一致（上升→偏多档位、深跌→偏空档位）；
  5. TestNoCapitalForExit   非买入档位绝不分配资金（「建议回避却给了 25% 资金」的回归测试）；
  6. TestPortfolio          组合分配自洽：权重之和 = 总仓位、总仓位 + 现金 = 1、整手取整；
  7. TestKellyBounds        凯利约束：f* ≤ KELLY_CAP、shrink ≤ 1、rawWeight ≤ maxWeight；
  8. TestPlan               交易计划单调性与价格为正、盈亏比为正；
  9. TestForecastOverlay    预测带：长度 = horizon、不含锚点、逐点 lo ≤ mid ≤ hi；
 10. TestMarks              K线标记：dir 合法、t 可回溯、advice 标记唯一且落在最后一根；
 11. TestEdge               统计优势口径：笔数 / 明细上限 / 胜率范围 / 赔率语义；
 12. TestDegenerate         退化与脏输入不抛异常、失败标的隔离、参数兜底；
 12b.TestDataInjection      数据源注入契约（签名 / limit 截断 / 行情兜底 / 绝不联网）；
 13. TestBatchLimits        批量上限、去重、保序（含多线程分支）；
 14. TestStances            7 个策略的 stance 契约与净票口径；
 15. TestParamSource        周期参数来源确实是 core/strategies.default_params（非本地硬编码）；
 16. TestRegressionFixes     三处已修复缺陷的回归测试（分位键、动量窗口文案、零波动中性票）。

当前状态：111 个用例全部通过（无 expectedFailure），
单文件跑完约 15 秒（recommend 一次要跑 7 个策略的历史回放，因此共用结果做了缓存）。

为什么这样造数据（可复现性优先）
--------------------------------
· 全部合成序列都是**确定性**的：要么是纯 sin + 线性漂移的波形，要么是固定种子的
  线性同余伪随机（`_uniforms` / `_normal`，刻意不用 `random`，因为 `random` 的默认
  种子依赖系统熵源，会让测试结果随环境漂移）；
· 日收益**有界**：Irwin-Hall 由 12 个均匀分布求和构成，取值严格落在 [-6, 6]，因此
  对数收益 |Δ| ≤ 6σ + |drift|，价格不会出现指数爆炸，也不会跌到负数（价格越低越
  容易触发 ATR/止损的边界，会让测试变成对浮点边界的赌博）；
· 日历**严格递增**：每个 K 线相隔一天，使 forecast 的时间轴外推（取尾部中位间隔）
  可被精确断言，也让「标记的 t 能在输入里回溯到」有唯一解；
· 数据方向与档位的对应关系全部**事先实测并注释在 FIXTURES 里**，测试只锁定已确认
  的行为，不写「猜出来的期望值」。

为什么不发起任何网络请求
------------------------
`recommend(symbols, fetch_bars, fetch_quote)` 的两个数据获取器是**依赖注入**的参数。
本文件所有调用都注入本地假函数（`FakeFeed`），并且 TestDegenerate 里还专门用
`socket.socket` 打桩，确保即使将来有人把网络调用偷偷写回 core/advisor.py，测试也会
立刻报错，而不是静默发请求。

运行方式（tests/ 下无 __init__.py，直接跑文件最稳）：
    python tests/test_advisor.py
    python -m unittest discover -s tests -t tests -p "test_advisor.py"
"""

import json
import math
import os
import re
import socket
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

# 让 tests/ 目录之外的包（core）可被导入，兼容任意工作目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import advisor as A           # noqa: E402  被测模块
from core import strategies as S        # noqa: E402  参数表来源（供口径一致性断言）
from core.forecast import forecast as build_forecast  # noqa: E402 用于 100 倍关系核对

# --------------------------------------------------------------------------- #
# 契约常量（全部从被测模块读取，避免测试里再抄一份口径造成「双重标准」）
# --------------------------------------------------------------------------- #
#: 允许出现的建议档位
OK_ACTIONS = frozenset(A.ACTION_LABEL.keys())
#: 允许开新仓的档位（advisor 里用同一个常量做资金闸门）
ENTRY_ACTIONS = frozenset(A.ENTRY_ACTIONS)
#: 顶层必备字段
TOP_KEYS = ("ok", "market", "horizon", "capital", "rows", "portfolio",
            "disclaimer", "model", "updated")
#: 每一行必备字段（需求逐字列出）
ROW_KEYS = ("code", "name", "market", "price", "changePct", "action", "actionText",
            "score", "confidence", "signals", "ensemble", "edge", "kelly",
            "forecast", "plan", "risk", "advisor")
#: 各市场最小交易单位（A 股 100 股、美股 1 股），与 core/kelly.LOTS 对齐
LOTS = {"cn": 100, "us": 1}
#: 预测带的 5 个分位点键（字符串化后落在 forecast.quantiles 里）
QUANTILE_KEYS = {"5", "25", "50", "75", "95"}
#: 建议档位 → 图上标记方向（与模块常量一致，这里显式写出以便锁定语义）
EXPECTED_MARK = {"buy": "buy", "add": "buy", "reduce": "sell", "sell": "sell",
                 "avoid": "sell", "hold": None, "watch": None}

BEGIN = date(2024, 1, 2)
#: 测试用本金：取一个方便的整数，便于人工核对「现金 + 投入 = 本金」
TEST_CAPITAL = 200000.0
#: 组合口径允许的偏差：权重四舍五入到 6 位、金额到 2 位，30 行累计也不会超过该量级
TOL_WEIGHT = 1e-4
#: 「总仓位 + 现金/本金 = 1」的容差：权重 6 位小数取整 + 现金 2 位小数取整
TOL_BALANCE = 1e-3


# --------------------------------------------------------------------------- #
# 合成数据（确定性、可复现、有界）
# --------------------------------------------------------------------------- #
def _uniforms(n, seed):
    """线性同余伪随机序列（固定实现，跨环境可复现；刻意不用 random 模块）。"""
    a, c, m = 1103515245, 12345, 2 ** 31
    x, out = int(seed), []
    for _ in range(n):
        x = (a * x + c) % m
        out.append(x / m)
    return out


def _normal(n, seed):
    """Irwin-Hall 近似标准正态：12 个均匀分布求和减 6（严格有界于 [-6, 6]）。"""
    u = _uniforms(n * 12, seed)
    return [sum(u[i * 12:(i + 1) * 12]) - 6.0 for i in range(n)]


def make_bars(closes, begin=BEGIN, with_time=True):
    """由收盘价序列构造标准K线（OHLCV 齐备，日历严格递增一天）。"""
    bars = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        bars.append({
            "t": (begin + timedelta(days=i)).isoformat() if with_time else None,
            "open": prev, "high": max(prev, c) * 1.002, "low": min(prev, c) * 0.998,
            "close": c, "volume": 1000000 + i,
        })
    return bars


def walk_closes(n=320, sigma=0.012, seed=1, drift=0.0):
    """固定种子的对数随机游走。

    日对数收益 = drift + σ·z，z 由 Irwin-Hall 生成且 |z| ≤ 6，因此单日收益有界；
    价格恒为正（指数函数），不会出现负价或指数爆炸。
    """
    out, log_p = [], 0.0
    for z in _normal(n, seed):
        log_p += drift + sigma * z
        out.append(100.0 * math.exp(log_p))
    return out


def wave_closes(n=300, amp=0.02, period=27.0, slope=0.002):
    """确定性正弦 + 线性漂移：完全没有随机数，价格包络有界且恒为正。"""
    return [100.0 * math.exp(slope * i) * (1.0 + amp * math.sin(i / period))
            for i in range(n)]


def decline_closes(n=320, rate=0.012):
    """单调下跌序列（每根固定 -rate），用于构造深回撤；价格恒为正。"""
    return [100.0 * (1.0 - rate) ** i for i in range(n)]


#: 全部测试序列。注释里的档位是**实测确认**的结果（见文件末尾 TestKnownDefects 的说明），
#: 测试用它们做「方向 ↔ 档位」的一致性锚点，而不是凭空假设。
FIXTURES = {
    "BUY": wave_closes(300, 0.02, 27.0, 0.002),        # 稳步上行 → buy
    "BUY2": wave_closes(300, 0.01, 9.0, 0.002),        # 上行（另一种波动节奏）→ buy
    "BUY3": wave_closes(300, 0.005, 5.0, 0.002),       # 小振幅上行 → buy
    "ADD": wave_closes(300, 0.02, 15.0, 0.0),          # 震荡偏多 → add
    # ADD0：评分够到 add、但条件分布均值 ≤ 0 → 连续凯利 f* = 0（「档位允许 ≠ 一定有仓位」）。
    # 振幅 0.04 → 0.05 是**重新标定的结果**（本轮）：core/forecast._rank_quantile 由
    # 「全样本分位」改成 expanding 分位（只用 t 时刻及之前的数据、去掉前视偏差）之后，
    # 旧参数 (0.04, 9.0, 0.002) 的前提失效 —— 条件分布均值由负转正
    # （μ = +0.0992%、upProb 0.25 → fStar = 0.958），于是「档位达标但凯利为 0」不再成立。
    # 振幅 0.05 下：μ = −0.9888%、fStar = 0.0，且评分 63.5 ≥ 58 仍判 add。
    # **该夹具对预测口径敏感**：以后动 core/forecast（分位口径 / 近邻权重 / 窗口）都要重新标定，
    # 详见 TestNoCapitalForExit.test_add_with_zero_kelly_is_not_funded 的 docstring。
    "ADD0": wave_closes(300, 0.05, 9.0, 0.002),        # 评分达标但凯利为 0 → add（0 仓）
    "HOLD": wave_closes(300, 0.02, 9.0, 0.0),          # 方向未一致 → hold
    "WATCH": wave_closes(300, 0.02, 15.0, -0.002),     # 偏弱但空方共识不足 → watch
    "REDUCE": wave_closes(300, 0.02, 5.0, 0.0),        # 偏弱 → reduce（凯利分权重却 > 0）
    "SELL": wave_closes(300, 0.04, 5.0, -0.002),       # 明确转空 → sell（凯利分权重 > 0）
    "SIDE": walk_closes(320, 0.012, 7),                # 无信息震荡 → sell，且有赢有亏样本
    "AVOID_DD": decline_closes(320, 0.012),            # 深跌：最大回撤 ≥ 55% 红线 → avoid
    "AVOID_VOL": walk_closes(320, 0.065, 1, 0.004),    # 高波动：年化波动 ≥ 90% 红线 → avoid
}
#: 代码 → K线（全部带 t，便于断言「标记的 t 可回溯」）
BARS = {code: make_bars(closes) for code, closes in FIXTURES.items()}


class FakeFeed(object):
    """假数据源：按代码返回内存里的合成K线，并把调用参数记录下来供断言。

    `recommend()` 的接口约定是 ``fetch_bars(market, code, period, limit)`` 与
    ``fetch_quote(market, code)``，这里用完全一样的签名实现，既能验证注入契约，
    又保证测试期间**不产生任何网络请求**。
    """

    def __init__(self, series=None, quote=None):
        self.series = BARS if series is None else series
        self.quote_value = quote
        self.calls = []

    def bars(self, market, code, period, limit):
        self.calls.append(("bars", market, code, period, limit))
        return list(self.series.get(str(code).upper(), []))[:limit]

    def quote(self, market, code):
        self.calls.append(("quote", market, code))
        return self.quote_value


#: 默认假数据源（缓存调用结果的测试共用；quote 返回 None 表示「取不到实时行情」，
#: 此时 advisor 必须回落到最后一根收盘价）
FEED = FakeFeed()


def fetch_quote_none(market, code):
    """取不到实时行情的行情源（返回 None 而不是 {}，覆盖防御分支）。"""
    return None


def run_recommend(symbols, feed=None, quote=None, **kwargs):
    """统一的调用入口：默认注入假数据源与固定本金，避免每个用例重复样板。"""
    kwargs.setdefault("market", "cn")
    kwargs.setdefault("capital", TEST_CAPITAL)
    f = feed if feed is not None else FEED
    q = quote if quote is not None else f.quote
    return A.recommend(symbols, fetch_bars=f.bars, fetch_quote=q, **kwargs)


#: 单只/批量结果的缓存：recommend 一次要跑 7 个策略的历史回放，重复调用会拖慢测试
_CACHE = {}


def cached(key, symbols, **kwargs):
    """按 key 缓存 recommend 结果（同一次测试运行内只算一次）。"""
    if key not in _CACHE:
        _CACHE[key] = run_recommend(symbols, **kwargs)
    return _CACHE[key]


#: 组合测试用的混合标的（含美股，用于覆盖「美股 1 股 / A股 100 股」两种最小单位）
BATCH = [
    {"code": "BUY", "name": "上升甲"},
    {"code": "BUY2", "market": "us", "name": "上升乙"},
    {"code": "BUY3", "name": "上升丙"},
    {"code": "ADD", "name": "增持丙"},
    {"code": "ADD0", "name": "增持零"},
    {"code": "HOLD", "name": "持有己"},
    {"code": "WATCH", "name": "观望庚"},
    {"code": "REDUCE", "name": "减仓丁"},
    {"code": "SELL", "name": "卖出戊"},
    {"code": "SIDE", "name": "震荡子"},
    {"code": "AVOID_DD", "name": "回避辛"},
    {"code": "AVOID_VOL", "name": "回避壬"},
]


def batch_result():
    """12 只标的的批量结果（覆盖 7 种档位 + cn/us 两个市场 + 有赢有亏/零亏损样本）。"""
    return cached("batch", BATCH)


def row_of(result, code):
    """从批量结果里取某只标的的行（断言存在，避免后续断言抛 AttributeError）。"""
    for r in result["rows"]:
        if r["code"] == code:
            return r
    raise AssertionError("批量结果里没有 %s 这一行" % code)


def rows_by_action(result, actions):
    return [r for r in result["rows"] if r.get("ok") and r.get("action") in actions]


def iter_floats(node, path="res"):
    """递归遍历响应里的所有 float（含嵌套 dict/list），产出 (路径, 值)。"""
    if isinstance(node, dict):
        for k, v in node.items():
            for item in iter_floats(v, "%s.%s" % (path, k)):
                yield item
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            for item in iter_floats(v, "%s[%d]" % (path, i)):
                yield item
    elif isinstance(node, float):
        yield path, node


def max_drawdown_pct(closes):
    """独立复算历史最大回撤（百分数）：峰值到谷底的最大跌幅。"""
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = max(mdd, (peak - c) / peak)
    return mdd * 100.0


# --------------------------------------------------------------------------- #
# 1. 响应契约
# --------------------------------------------------------------------------- #
class TestResponseContract(unittest.TestCase):
    """顶层与逐行字段必须齐全，且 action 的取值域被严格约束。

    为什么先测契约：前端 web/js/views/advisor.js 直接按字段名消费这份响应，
    字段改名或漏字段不会报错，只会静默渲染成空白，所以用断言把接口钉死。
    """

    def test_top_level_fields(self):
        res = batch_result()
        for key in TOP_KEYS:
            self.assertIn(key, res, "顶层缺少字段 %s" % key)
        self.assertTrue(res["ok"])
        self.assertIsInstance(res["rows"], list)
        self.assertIsInstance(res["portfolio"], dict)
        self.assertEqual(len(res["rows"]), res["count"])
        self.assertEqual(res["count"], res["requested"])
        self.assertEqual(res["horizon"], A.DEFAULT_HORIZON)
        self.assertEqual(res["capital"], TEST_CAPITAL)
        # updated 是毫秒时间戳（int），用来给前端判断数据新鲜度
        self.assertIsInstance(res["updated"], int)
        self.assertGreater(res["updated"], 0)
        # 免责声明必须真的是一句免责声明，而不是空串
        self.assertIn("不构成任何投资建议", res["disclaimer"])
        for key in ("score", "confidence", "edge", "kelly", "forecast", "stance"):
            self.assertIn(key, res["model"], "model 缺少口径说明 %s" % key)

    def test_row_fields(self):
        res = batch_result()
        for row in res["rows"]:
            self.assertTrue(row["ok"], "%s 不应失败" % row["code"])
            for key in ROW_KEYS:
                self.assertIn(key, row, "%s 行缺少字段 %s" % (row["code"], key))
            self.assertIsInstance(row["code"], str)
            self.assertTrue(row["code"])
            self.assertIsInstance(row["name"], str)
            self.assertIn(row["market"], ("cn", "us"))
            self.assertIsInstance(row["price"], float)
            self.assertGreater(row["price"], 0)
            self.assertIsInstance(row["changePct"], float)
            self.assertIn(row["action"], OK_ACTIONS)
            self.assertIsInstance(row["actionText"], str)
            # 档位文案必须以档位名开头，否则前端 tooltip 会自相矛盾
            self.assertIn(A.ACTION_LABEL[row["action"]], row["actionText"])
            self.assertGreaterEqual(row["score"], 0.0)
            self.assertLessEqual(row["score"], 100.0)
            self.assertGreaterEqual(row["confidence"], 0.0)
            self.assertLessEqual(row["confidence"], 1.0)
            self.assertIsInstance(row["signals"], list)
            for sub in ("ensemble", "edge", "kelly", "forecast", "plan", "risk", "advisor"):
                self.assertIsInstance(row[sub], dict, "%s.%s 应为 dict" % (row["code"], sub))

    def test_row_subfield_contract(self):
        """子结构的关键字段同样要齐全（前端逐字段读取，缺一个就是空白格）。"""
        res = batch_result()
        for row in res["rows"]:
            self.assertEqual(set(row["kelly"]) >=
                             {"fStar", "fStarRaw", "kind", "fraction", "shrink",
                              "shrinkSample", "rawWeight", "weight", "amount",
                              "shares", "lot", "entryAction", "note"}, True)
            self.assertEqual(set(row["risk"]) >= {"atrPct", "vol", "maxDrawdown", "note"}, True)
            self.assertEqual(set(row["forecast"]) >=
                             {"expectedReturn", "upProb", "medianReturn", "bandLow",
                              "bandHigh", "quantiles", "sample", "confidence",
                              "degraded", "note"}, True)
            self.assertEqual(set(row["plan"]) >=
                             {"entry", "stop", "target1", "target2", "riskReward",
                              "atr", "direction", "forecastTarget", "note"}, True)
            self.assertEqual(set(row["advisor"]) >= {"marks", "forecast", "plan"}, True)
            self.assertEqual(set(row["advisor"]["forecast"]) >= {"path", "horizon", "levels"}, True)

    def test_kelly_kind_domain(self):
        """kelly.kind 只有 4 个合法取值，前端据此决定文案，不允许出现别的拼写。"""
        res = batch_result()
        for row in res["rows"]:
            self.assertIn(row["kelly"]["kind"], ("discrete", "continuous", "none", "invalid"))

    def test_action_domain_all_fixtures(self):
        """所有档位都落在 7 个合法值内，且 7 个档位在批量结果里都被覆盖到。

        覆盖全部档位是刻意的：只测 buy 的测试对「卖出/回避」分支毫无保护。
        """
        res = batch_result()
        seen = set()
        for row in res["rows"]:
            self.assertIn(row["action"], OK_ACTIONS)
            seen.add(row["action"])
        self.assertTrue({"buy", "add", "hold", "watch", "reduce", "sell", "avoid"} <= seen,
                        "批量用例应覆盖全部 7 个档位，实际 %s" % sorted(seen))

    def test_error_row_action_is_none(self):
        """失败行（ok=False）的 action 是 None —— 这是**约定**而非缺陷。

        `_error_row()` 用 action=None + actionText="数据不足" 表达「没有建议」；
        前端 advisor.js 用 `String(r.action || '')` 兜底，会退化显示 actionText。
        这里把这个约定写进测试，避免有人改成空字符串导致前端 `ACTION_LABEL['']` 变 undefined。
        """
        res = run_recommend(["BOOM"], feed=DirtyFeed())
        row = res["rows"][0]
        self.assertFalse(row["ok"])
        self.assertIsNone(row["action"])
        self.assertEqual(row["actionText"], "数据不足")
        self.assertTrue(row["error"])
        for key in ROW_KEYS:
            self.assertIn(key, row)
        self.assertIsNone(row["kelly"])
        self.assertIsNone(row["advisor"])

    def test_ensemble_vote_detail(self):
        """共识明细必须自洽：7 票、计数与票面一致、净票 = 各 stance 之和。"""
        res = batch_result()
        for row in res["rows"]:
            ens = row["ensemble"]
            self.assertEqual(len(ens["votes"]), len(S.keys()))
            self.assertEqual([v["strategy"] for v in ens["votes"]], list(S.keys()))
            for vote in ens["votes"]:
                self.assertIn(vote["signal"], ("buy", "hold", "sell"))
                self.assertIn(vote["stance"], (-1, 0, 1))
                self.assertTrue(vote["brief"])
                self.assertEqual(vote["strategyName"], S.STRATEGIES[vote["strategy"]]["name"])
                # 票面与数值必须一致：1→buy、0→hold、-1→sell
                self.assertEqual(vote["signal"], A.VOTE_OF[vote["stance"]])
            self.assertEqual(ens["buy"], sum(1 for v in ens["votes"] if v["signal"] == "buy"))
            self.assertEqual(ens["hold"], sum(1 for v in ens["votes"] if v["signal"] == "hold"))
            self.assertEqual(ens["sell"], sum(1 for v in ens["votes"] if v["signal"] == "sell"))
            self.assertEqual(ens["buy"] + ens["hold"] + ens["sell"], len(ens["votes"]))
            self.assertEqual(ens["net"], sum(v["stance"] for v in ens["votes"]))


# --------------------------------------------------------------------------- #
# 2. 单位口径（百分数 vs 小数）
# --------------------------------------------------------------------------- #
class TestUnits(unittest.TestCase):
    """单位混用是本项目最容易出错的地方：0.05 与 5.0 差 100 倍。

    这里逐个字段把口径钉死，并且**用 forecast 模块的原始输出做交叉核对**：
    同一个 horizon、同一份K线，行内的 expectedReturn 必须等于原始小数 × 100，
    这条断言一旦失败就说明有人在某一层多乘或少乘了 100。
    """

    def test_expected_return_is_percent_of_raw(self):
        res = cached("single_buy", [{"code": "BUY"}], horizon=20)
        row = row_of(res, "BUY")
        raw = build_forecast(BARS["BUY"], horizon=20)
        # 原始值是小数（0.0832 = +8.32%）
        self.assertTrue(-1.0 < raw["expected_return"] < 1.0)
        # 行内值是百分数：先核对 100 倍关系，再核对与原始值的绝对一致性
        self.assertAlmostEqual(row["forecast"]["expectedReturn"],
                              raw["expected_return"] * 100.0, delta=1e-3)
        self.assertAlmostEqual(row["forecast"]["expectedReturn"] / 100.0,
                              raw["expected_return"], delta=1e-5)
        # 上升趋势的期望收益是正数，且量级证明它是百分数而不是小数
        self.assertGreater(row["forecast"]["expectedReturn"], 0.5)
        # 中位收益同理
        self.assertAlmostEqual(row["forecast"]["medianReturn"],
                              raw["median_return"] * 100.0, delta=1e-3)

    def test_forecast_quantiles_are_percent(self):
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=20), "BUY")
        raw = build_forecast(BARS["BUY"], horizon=20)
        fc = row["forecast"]
        self.assertEqual(set(fc["quantiles"]), QUANTILE_KEYS)
        for level in (5, 25, 50, 75, 95):
            self.assertAlmostEqual(fc["quantiles"][str(level)],
                                  raw["quantiles"][level] * 100.0, delta=1e-3)
        # 50% 分位价与中位收益是同一件事的两种表达，必须一致
        self.assertAlmostEqual(fc["quantiles"]["50"], fc["medianReturn"], delta=1e-3)
        self.assertLessEqual(float(fc["quantiles"]["5"]), float(fc["quantiles"]["95"]))

    def test_ratio_fields_are_fractions(self):
        """upProb / confidence / kelly.weight / edge.winRate / totalWeight 都是 [0,1] 小数。"""
        res = batch_result()
        for row in res["rows"]:
            self.assertGreaterEqual(row["forecast"]["upProb"], 0.0)
            self.assertLessEqual(row["forecast"]["upProb"], 1.0)
            self.assertGreaterEqual(row["confidence"], 0.0)
            self.assertLessEqual(row["confidence"], 1.0)
            self.assertGreaterEqual(row["kelly"]["rawWeight"], 0.0)
            self.assertLessEqual(row["kelly"]["rawWeight"], 1.0)
            self.assertGreaterEqual(row["kelly"]["weight"], 0.0)
            self.assertLessEqual(row["kelly"]["weight"], 1.0)
            # 零笔共识交易时 winRate 为 None（不是 0，也不是 1）—— 有样本才谈胜率
            if row["edge"]["winRate"] is not None:
                self.assertGreaterEqual(row["edge"]["winRate"], 0.0)
                self.assertLessEqual(row["edge"]["winRate"], 1.0)
        portfolio = res["portfolio"]
        self.assertGreaterEqual(portfolio["totalWeight"], 0.0)
        self.assertLessEqual(portfolio["totalWeight"], 1.0)
        for placed in portfolio["rows"]:
            self.assertGreaterEqual(placed["weight"], 0.0)
            self.assertLessEqual(placed["weight"], 1.0)

    def test_max_drawdown_is_positive_percent(self):
        """risk.maxDrawdown 是「正的百分数」，用独立复算来核对量纲。"""
        res = batch_result()
        for row in res["rows"]:
            mdd = row["risk"]["maxDrawdown"]
            self.assertGreaterEqual(mdd, 0.0)
            self.assertLessEqual(mdd, 100.0)
            expect = max_drawdown_pct(FIXTURES[row["code"]])
            self.assertAlmostEqual(mdd, expect, delta=0.01,
                                   msg="%s 的最大回撤口径不是百分数" % row["code"])
        # 深跌序列必须明显 > 55（这也正是 avoid 的红线）
        self.assertGreater(row_of(res, "AVOID_DD")["risk"]["maxDrawdown"], 55.0)
        # 稳步上行序列的回撤很小（说明没有把小数错当百分数）
        self.assertLess(row_of(res, "BUY")["risk"]["maxDrawdown"], 5.0)

    def test_change_pct_is_percent(self):
        """changePct 是百分数：无实时行情时 = 最后两根收盘价之比 -1（再 ×100）。"""
        res = batch_result()
        for row in res["rows"]:
            closes = FIXTURES[row["code"]]
            expect = (closes[-1] / closes[-2] - 1.0) * 100.0
            self.assertAlmostEqual(row["changePct"], expect, delta=0.01,
                                   msg="%s 的涨跌幅口径不是百分数" % row["code"])
        # 下跌序列的涨跌幅为负，证明符号没写反
        self.assertLess(row_of(res, "AVOID_DD")["changePct"], 0.0)

    def test_atr_pct_is_percent(self):
        """risk.atrPct 与 plan.atr 的量纲关系：atrPct = atr / price × 100。"""
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=20), "BUY")
        self.assertAlmostEqual(row["risk"]["atrPct"],
                              row["plan"]["atr"] / row["price"] * 100.0, delta=0.01)


# --------------------------------------------------------------------------- #
# 3. JSON 严格性
# --------------------------------------------------------------------------- #
class TestJsonStrictness(unittest.TestCase):
    """出口必须能被标准 JSON 编码器严格序列化。

    `json.dumps(..., allow_nan=False)` 会在遇到 inf / NaN 时抛 ValueError ——
    这正是服务端 HTTP 响应最怕的情况（浏览器 JSON.parse 会直接失败），
    所以它比「看起来是数字」更值得断言。
    """

    RESULTS = ("batch", "single_buy", "horizon_1", "horizon_250", "dirty")

    def _all_results(self):
        cached("single_buy", [{"code": "BUY"}], horizon=20)
        cached("horizon_1", [{"code": "SIDE"}], horizon=1)
        cached("horizon_250", [{"code": "SIDE"}], horizon=250)
        cached("dirty", DIRTY_SYMBOLS, feed=DirtyFeed())
        return [_CACHE[key] for key in self.RESULTS]

    def test_dumps_with_allow_nan_false(self):
        for res in self._all_results():
            text = json.dumps(res, allow_nan=False)
            self.assertIsInstance(text, str)
            self.assertNotIn("NaN", text)
            self.assertNotIn("Infinity", text)
            # 往返一次，确认没有丢字段（dict/list 结构保持一致）
            again = json.loads(text)
            self.assertEqual(again["count"], res["count"])
            self.assertEqual([r["code"] for r in again["rows"]],
                             [r["code"] for r in res["rows"]])

    def test_every_float_is_finite(self):
        for res in self._all_results():
            for path, value in iter_floats(res):
                self.assertTrue(math.isfinite(value),
                                "%s 泄漏了非有限浮点：%r" % (path, value))

    def test_no_bool_masquerading_as_number(self):
        """bool 是 int 的子类，混进数值字段会让 JSON 变成 true/false。

        只针对数值字段做检查：signals/notes 等文本字段里出现 True/False 是合法的。
        """
        res = batch_result()
        numeric_paths = ("price", "changePct", "score", "confidence")
        for row in res["rows"]:
            for key in numeric_paths:
                self.assertNotIsInstance(row[key], bool, "%s.%s 不应是 bool" % (row["code"], key))
            for key in ("fStar", "rawWeight", "weight", "shrink"):
                self.assertNotIsInstance(row["kelly"][key], bool)
            for key in ("expectedReturn", "upProb", "confidence"):
                self.assertNotIsInstance(row["forecast"][key], bool)


# --------------------------------------------------------------------------- #
# 脏输入夹具（供 JSON 严格性与退化用例共用）
# --------------------------------------------------------------------------- #
#: 脏输入批量：空 / 太短 / 只有 close / 抓取抛异常 / 脏K线混入 / 正常
DIRTY_SYMBOLS = ["EMPTY", "SHORT", "ONLYCLOSE", "BOOM", "BLANK", "BUY"]


def mixed_dirty_bars():
    """在 80 根正常K线里塞进 6 根脏K线（缺字段 / 字符串 / 负价 / None / NaN / inf）。

    为什么要「脏+净混合」而不是全脏：全脏只会走到「有效根数为 0」这一条分支，
    而真实故障往往是「个别K线缺字段」，此时正确的行为是**整根剔除后继续算**。
    """
    bars = list(BARS["WATCH"][:80])
    bars[3] = {"t": None, "open": None, "high": None, "low": None,
               "close": None, "volume": None}
    bars[7] = {"close": "abc"}
    bars[9] = {"close": -5.0}
    bars.insert(15, None)
    bars.append({"close": float("nan")})
    bars.append({"close": float("inf")})
    return bars


def is_valid_bar(b):
    """测试侧的独立有效性判定（与 core/advisor._clean_bars 的口径一致：close 为正数）。"""
    if not isinstance(b, dict):
        return False
    c = b.get("close")
    if isinstance(c, bool) or not isinstance(c, (int, float)):
        return False
    return c == c and c != float("inf") and c != float("-inf") and c > 0


class DirtyFeed(object):
    """脏输入专用假数据源：按代码返回不同形态的坏数据，BOOM 直接抛异常。"""

    def __init__(self):
        self.calls = []

    def bars(self, market, code, period, limit):
        self.calls.append(("bars", market, code, period, limit))
        key = str(code).upper()
        if key == "BOOM":
            raise RuntimeError("模拟网络故障")
        if key == "EMPTY":
            return []
        if key == "SHORT":
            return BARS["WATCH"][:30]
        if key == "ONLYCLOSE":
            return [{"close": c} for c in FIXTURES["WATCH"]]
        if key == "BLANK":
            return mixed_dirty_bars()
        return BARS.get(key, [])

    def quote(self, market, code):
        self.calls.append(("quote", market, code))
        return None


# --------------------------------------------------------------------------- #
# 4. 建议档位与数据方向一致
# --------------------------------------------------------------------------- #
#: 显然上行的序列（前视收益几乎恒为正）
UP_CODES = ("BUY", "BUY2", "BUY3", "ADD")
#: 显然下行 / 风险红线的序列
DOWN_CODES = ("AVOID_DD", "AVOID_VOL", "SELL", "REDUCE", "SIDE")


class TestDirection(unittest.TestCase):
    """方向与档位必须同向：不是「涨了就说买」，而是档位阈值真的落在合理的区间。

    这条断言把 _decide() 的两个关键性质钉住：
      · 单调性：评分越高档位越偏多（buy/add 的评分必须整体高于 reduce/sell）；
      · 红线优先：avoid 只允许由风险红线触发，且必须在文案里说清是哪条红线。
    """

    def test_up_trend_gets_bullish_action(self):
        res = batch_result()
        bullish = []
        for code in UP_CODES:
            row = row_of(res, code)
            self.assertIn(row["action"], ("buy", "add", "hold"),
                          "%s 是上行序列，不该给出 %s" % (code, row["action"]))
            # 评分是五因子加权（共识只占 0.30），所以只要求它落在中性之上；
            # 不要求净票为正：小振幅上行样本可能只有 -2 票、评分却有 70+
            # （均线排列 + 动量 + 条件分布共同把评分推上去），这是加权口径的预期结果。
            self.assertGreaterEqual(row["score"], 45.0, code)
            if row["action"] in ENTRY_ACTIONS:
                bullish.append(code)
        self.assertIn("BUY", bullish, "强趋势序列至少应有一只给出买入/增持")
        self.assertGreaterEqual(row_of(res, "BUY")["score"], 58.0)

    def test_deep_decline_gets_defensive_action(self):
        res = batch_result()
        for code in DOWN_CODES:
            row = row_of(res, code)
            self.assertIn(row["action"], ("avoid", "sell", "reduce"),
                          "%s 是下行/高风险序列，不该给出 %s" % (code, row["action"]))
        # 深跌序列的回撤红线必须真的被触发（否则上面的失败原因会难以定位）
        self.assertGreaterEqual(row_of(res, "AVOID_DD")["risk"]["maxDrawdown"], 55.0)

    def test_avoid_is_always_backed_by_a_redline(self):
        """avoid 只有两条触发路径，二者必居其一，且文案必须点名。"""
        avoids = rows_by_action(batch_result(), ("avoid",))
        self.assertGreaterEqual(len(avoids), 2, "批量用例应至少覆盖两个 avoid 样本")
        for row in avoids:
            risky = (row["risk"]["maxDrawdown"] >= 55.0) or (row["risk"]["vol"] >= 90.0)
            self.assertTrue(risky, "%s 的 avoid 没有风险红线支撑" % row["code"])
            self.assertIn("红线", row["actionText"])
            self.assertFalse(row["kelly"]["entryAction"])

    def test_volatility_redline_alone_is_enough(self):
        """波动红线可独立触发 avoid：构造「回撤 < 55% 但年化波动 ≥ 90%」的序列。

        为什么必须单独验证：如果把两条红线写成 and，深跌样本（回撤 98%）依旧
        avoid，测试照样全绿，但「高波动、回撤不深」的标的会被漏掉。
        """
        row = row_of(batch_result(), "AVOID_VOL")
        self.assertGreaterEqual(row["risk"]["vol"], 90.0)
        self.assertLess(row["risk"]["maxDrawdown"], 55.0)
        self.assertEqual(row["action"], "avoid")
        self.assertIn("年化波动", row["actionText"])

    def test_sell_requires_negative_consensus(self):
        """sell 的前提是「评分偏弱」且「空方共识明确」，二者缺一不可。"""
        sells = rows_by_action(batch_result(), ("sell",))
        self.assertTrue(sells, "批量用例里应至少有一个 sell 样本")
        for row in sells:
            self.assertLess(row["score"], 36.0)
            self.assertLessEqual(row["ensemble"]["net"], -2)

    def test_score_ordering_matches_action(self):
        res = batch_result()
        bullish = [r["score"] for r in res["rows"] if r["action"] in ("buy", "add")]
        bearish = [r["score"] for r in res["rows"]
                   if r["action"] in ("reduce", "sell", "avoid")]
        self.assertTrue(bullish and bearish)
        self.assertGreater(min(bullish), max(bearish),
                           "偏多档位的评分应整体高于偏空档位：%r vs %r" % (bullish, bearish))


# --------------------------------------------------------------------------- #
# 5. 非买入档位绝不分配资金（回归测试）
# --------------------------------------------------------------------------- #
class TestNoCapitalForExit(unittest.TestCase):
    """「建议回避却分配了 25% 资金」缺陷的回归测试。

    _portfolio() 的注释写明：**建议档位是总闸门，凯利只决定闸门内的仓位大小**。
    除 buy/add 外的档位一律 0 仓，但行内保留 kelly.rawWeight（「如果建仓会是多少」）
    与原因说明，便于用户理解「模型认为有优势，但档位不允许」。

    这里刻意挑了 rawWeight 明显大于 0 的样本（REDUCE = 0.25、AVOID_VOL > 0），
    否则「权重为 0」可能只是因为凯利本身就是 0，断言就退化成空断言了。
    """

    def test_non_entry_rows_get_zero_capital(self):
        res = batch_result()
        funded = set(x["code"] for x in res["portfolio"]["rows"])
        blocked = [r for r in res["rows"] if r["action"] not in ENTRY_ACTIONS]
        self.assertTrue(blocked)
        for row in blocked:
            self.assertEqual(row["kelly"]["weight"], 0.0, row["code"])
            self.assertEqual(row["kelly"]["shares"], 0, row["code"])
            self.assertEqual(row["kelly"]["amount"], 0.0, row["code"])
            self.assertEqual(row["kelly"]["actual"], 0.0, row["code"])
            self.assertFalse(row["kelly"]["entryAction"], row["code"])
            self.assertNotIn(row["code"], funded)

    def test_reduce_with_positive_kelly_is_still_blocked(self):
        """reduce 行的分数凯利权重 > 0，但绝不能因此拿到资金。"""
        res = batch_result()
        row = row_of(res, "REDUCE")
        self.assertEqual(row["action"], "reduce")
        self.assertGreater(row["kelly"]["rawWeight"], 0.1,
                           "该样本的分数凯利权重本应 > 0，否则本用例失去意义")
        self.assertGreater(row["kelly"]["fStar"], 0.0)
        self.assertEqual(row["kelly"]["weight"], 0.0)
        self.assertEqual(row["kelly"]["shares"], 0)
        self.assertNotIn("REDUCE", [x["code"] for x in res["portfolio"]["rows"]])

    def test_avoid_with_positive_kelly_is_still_blocked(self):
        """avoid 行同样可能带着正的分数凯利权重，闸门必须拦住它。"""
        row = row_of(batch_result(), "AVOID_VOL")
        self.assertEqual(row["action"], "avoid")
        self.assertGreater(row["kelly"]["rawWeight"], 0.0)
        self.assertEqual(row["kelly"]["weight"], 0.0)
        self.assertEqual(row["kelly"]["shares"], 0)
        self.assertEqual(row["kelly"]["actual"], 0.0)

    def test_blocked_rows_explain_why(self):
        """被闸门拦下的行必须留下原因，否则用户会误以为「模型不看好」。"""
        for row in batch_result()["rows"]:
            if row["action"] in ENTRY_ACTIONS:
                continue
            reason = row["kelly"].get("reason") or ""
            self.assertIn("建议档位", reason,
                          "%s 的 kelly.reason 应说明是档位闸门拦下的" % row["code"])
            self.assertIn(A.ACTION_LABEL[row["action"]], reason)
            self.assertIn(reason, row["kelly"]["note"])

    def test_portfolio_rows_are_entry_actions_only(self):
        for placed in batch_result()["portfolio"]["rows"]:
            self.assertIn(placed["action"], ENTRY_ACTIONS)
            self.assertGreater(placed["shares"], 0)
            self.assertGreater(placed["weight"], 0.0)

    def test_add_with_zero_kelly_is_not_funded(self):
        """档位允许建仓不代表一定有仓位：凯利为 0 时同样 0 股（两道闸门串联）。

        断言本身**没有放宽**（仍是「fStar 必须逐位等于 0.0」「weight / shares 必须为 0」），
        业务含义也保持不变：ADD0 的评分够到 add（entryAction=True），但凯利为 0（无优势），
        于是它既不在组合里，也不该出现「0 股但非零权重」的怪状态。

        为什么改夹具（振幅 0.04 → 0.05），以及原来依赖了什么：
          · 旧夹具 ``wave_closes(300, 0.04, 9.0, 0.002)`` 的前提是「条件分布均值为负」。
            它在**旧口径**下成立 —— 当时 ``core/forecast._rank_quantile`` 用**全样本分位**
            给状态特征归一（先对整段序列排序再取分位），于是「与当前状态最相似的 60 个
            历史窗口」里混进了**当前 K 线之后**才出现的信息，条件分布均值 μ = −0.717%、
            upProb = 0.20 → 凯利 f* = 0。
          · 上一轮主程把 ``_rank_quantile`` 改成 **expanding 分位**（只用 t 时刻及之前的数据，
            修掉前视偏差）后，同样的波形得到 μ = +0.0992%（**由负转正**）、upProb = 0.25
            → 连续凯利 f* = min(1.2773, KELLY_CAP) × shrink = 0.958，夹具的**前提失效**，
            而不是断言错。这是「修掉前视偏差后样本的行为变了」这类连带影响，必须显式记录。
          · 处理方式：只重新标定 ``wave_closes`` 的参数（扫描振幅 / 周期 / 斜率后取
            ``amp = 0.05``）：新口径下 μ = −0.9888%、upProb = 0.25 → f* = 0，
            且评分 63.5 ≥ 58 仍落在 add 档（entryAction=True），原意完整保留。
            振幅取 0.05 而不是「刚好让 f* 归零」的边界值，是为了留出余量：
            0.045–0.055 这一带在新口径下都是「fStar = 0 且 add」。

        **注意：该夹具对预测口径敏感** —— 以后任何改动 ``core/forecast``（分位口径、近邻数 k、
        特征窗口、权重）或 ``core/advisor._kelly_input``（μ/σ 的取法）的改动，
        都要回来重新标定 ADD0，否则这条用例会以「fStar 不再是 0」的形式失败，
        而那并不代表业务逻辑坏了。
        """
        row = row_of(batch_result(), "ADD0")
        self.assertEqual(row["action"], "add")
        self.assertTrue(row["kelly"]["entryAction"])
        self.assertEqual(row["kelly"]["fStar"], 0.0)
        self.assertEqual(row["kelly"]["weight"], 0.0)
        self.assertEqual(row["kelly"]["shares"], 0)
        # 顺带钉住夹具前提本身：条件分布均值 ≤ 0（否则 f* 为 0 就只是巧合）
        self.assertLessEqual(row["forecast"]["expectedReturn"], 0.0,
                             "ADD0 的前提是「条件分布均值 ≤ 0 → 无优势」，夹具需重新标定")

    def test_entry_action_flag_matches_action(self):
        for row in batch_result()["rows"]:
            self.assertEqual(row["kelly"]["entryAction"], row["action"] in ENTRY_ACTIONS,
                             "%s 的 entryAction 与档位不一致" % row["code"])


# --------------------------------------------------------------------------- #
# 6. 组合分配自洽
# --------------------------------------------------------------------------- #
class TestPortfolio(unittest.TestCase):
    """组合层的账必须能对上：权重之和 = 总仓位、总仓位 + 现金 = 1、股数整手。

    容差说明（工具取整带来的必然偏差，不是缺陷）：
      · 每行 weight = actual / capital，四舍五入到 6 位小数，10 行累计 ≤ 5e-6；
      · cash 是金额，四舍五入到 2 位小数，除以 20 万本金后 ≤ 2.5e-8；
      · 整手取整（就近取整、半手向上）只让**实际权重**偏离**计划权重**，
        不影响上述恒等式，因此 TOL_BALANCE = 1e-3 足够宽松，
        但仍能抓住「权重口径错乘 100」「现金没有回填」这类量级错误。
    """

    def test_weight_sum_equals_total(self):
        portfolio = batch_result()["portfolio"]
        total = sum(x["weight"] for x in portfolio["rows"])
        self.assertAlmostEqual(total, portfolio["totalWeight"], delta=TOL_WEIGHT)
        self.assertEqual(portfolio["count"], len(portfolio["rows"]))

    def test_total_weight_plus_cash_equals_one(self):
        res = batch_result()
        portfolio = res["portfolio"]
        ratio = portfolio["totalWeight"] + portfolio["cash"] / portfolio["capital"]
        self.assertAlmostEqual(ratio, 1.0, delta=TOL_BALANCE)
        # 同一恒等式的另一条路径：总仓位 ≈ 实际投入 / 本金
        self.assertAlmostEqual(portfolio["totalWeight"],
                               portfolio["invested"] / portfolio["capital"], delta=TOL_BALANCE)
        self.assertAlmostEqual(portfolio["invested"] + portfolio["cash"],
                               portfolio["capital"], delta=0.01)
        self.assertEqual(portfolio["capital"], res["capital"])

    def test_shares_are_lot_multiples(self):
        """A 股 100 股一手、美股 1 股一手：股数必须是整手倍数，否则无法下单。"""
        res = batch_result()
        markets = set()
        for placed in res["portfolio"]["rows"]:
            lot = LOTS[placed["market"]]
            markets.add(placed["market"])
            self.assertGreater(placed["shares"], 0)
            self.assertEqual(placed["shares"] % lot, 0,
                             "%s 的股数 %d 不是最小单位 %d 的整数倍"
                             % (placed["code"], placed["shares"], lot))
        self.assertEqual(markets, {"cn", "us"}, "用例应同时覆盖 A 股与美股两种最小单位")

    def test_kelly_lot_echo_matches_market(self):
        for row in batch_result()["rows"]:
            self.assertEqual(row["kelly"]["lot"], LOTS[row["market"]],
                             "%s 的最小交易单位回显与市场不符" % row["code"])

    def test_rows_sorted_by_weight_desc(self):
        weights = [x["weight"] for x in batch_result()["portfolio"]["rows"]]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_funded_rows_backfilled_into_row(self):
        """组合分配结果必须回填到对应行的 kelly 字段（前端只读 rows）。"""
        res = batch_result()
        by_code = {r["code"]: r for r in res["rows"]}
        for placed in res["portfolio"]["rows"]:
            row = by_code[placed["code"]]
            self.assertEqual(row["kelly"]["weight"], placed["weight"])
            self.assertEqual(row["kelly"]["shares"], placed["shares"])
            self.assertEqual(row["kelly"]["amount"], placed["amount"])
            self.assertEqual(row["kelly"]["actual"], placed["amount"])
            self.assertGreater(row["kelly"]["shares"], 0)
            self.assertTrue(row["kelly"]["entryAction"])
            self.assertIsNotNone(row["kelly"].get("targetWeight"))

    def test_budget_is_capacity_times_capital(self):
        """计划仓位 = 1 − 现金缓冲；实际投入不得突破计划投入资金（风险预算）。"""
        res = batch_result()
        portfolio = res["portfolio"]
        self.assertAlmostEqual(portfolio["capacity"], 1.0 - res["cashBuffer"], delta=1e-9)
        self.assertAlmostEqual(portfolio["budget"],
                               portfolio["capital"] * portfolio["capacity"], delta=0.01)
        self.assertLessEqual(portfolio["invested"], portfolio["budget"] + 1e-6)

    def test_empty_batch_keeps_full_cash(self):
        """没有标的时不能凭空产生仓位，也不能因为 rows 为空而报错。"""
        res = run_recommend([])
        self.assertTrue(res["ok"])
        self.assertEqual(res["rows"], [])
        self.assertEqual(res["portfolio"]["count"], 0)
        self.assertEqual(res["portfolio"]["rows"], [])
        self.assertEqual(res["portfolio"]["totalWeight"], 0.0)
        self.assertEqual(res["portfolio"]["cash"], res["capital"])
        self.assertIsNone(res["model"]["forecast"])


# --------------------------------------------------------------------------- #
# 7. 凯利约束
# --------------------------------------------------------------------------- #
class TestKellyBounds(unittest.TestCase):
    """凯利的三个上限必须逐个成立，且字段之间的换算关系可被独立复算。

    连续凯利 f* = μ/σ² 在低波动样本上会发散（本套用例里就有 f*Raw ≈ 1.7e5 的样本），
    所以模块先按 KELLY_CAP 截断、再按样本量收缩。这两步都需要被复算，
    否则「仓位看起来很合理」可能只是参数恰好偏小，而不是约束真的生效。
    """

    def test_fstar_within_cap(self):
        for row in batch_result()["rows"]:
            k = row["kelly"]
            self.assertGreaterEqual(k["fStar"], 0.0)
            self.assertLessEqual(k["fStar"], A.KELLY_CAP + 1e-9,
                                 "%s 的 f* 超过杠杆上限" % row["code"])

    def test_cap_is_actually_exercised(self):
        """确认用例真的踩到了截断分支（否则上一条断言只是空跑）。"""
        rows = [r for r in batch_result()["rows"] if r["kelly"]["fStarRaw"] is not None]
        self.assertTrue(any(r["kelly"]["fStarRaw"] > A.KELLY_CAP for r in rows),
                        "批量用例应至少有一个样本触发 KELLY_CAP 截断")

    def test_moderate_max_weight_is_respected(self):
        """单只上限跟着参数走，而不是写死的常数：用 0.1 再跑一遍。

        为什么换一组参数：默认 0.25 恰好等于若干样本被截断后的取值，
        只有换参数才能确认截断阈值真的来自调用方传入的 max_weight。
        """
        res = run_recommend(BATCH, max_weight=0.1)
        self.assertEqual(res["maxWeight"], 0.1)
        for row in res["rows"]:
            self.assertLessEqual(row["kelly"]["rawWeight"], 0.1 + 1e-9, row["code"])

    def test_shrink_within_unit_interval_and_matches_sample(self):
        for row in batch_result()["rows"]:
            k = row["kelly"]
            self.assertGreaterEqual(k["shrink"], 0.0)
            self.assertLessEqual(k["shrink"], 1.0)
            # 收缩用的样本量：离散口径用共识交易笔数，其余用条件分布样本量
            expect_n = (row["edge"]["trades"] if k["kind"] == "discrete"
                        else row["forecast"]["sample"])
            self.assertEqual(k["shrinkSample"], expect_n, row["code"])
            expect = min(1.0, expect_n / (expect_n + A.KELLY_SHRINK_N)) if expect_n else 0.0
            self.assertAlmostEqual(k["shrink"], round(expect, 4), delta=1e-4,
                                   msg="%s 的样本量收缩系数与 n/(n+%s) 不符"
                                       % (row["code"], A.KELLY_SHRINK_N))

    def test_raw_weight_within_max_weight(self):
        res = batch_result()
        limit = res["maxWeight"]
        for row in res["rows"]:
            self.assertGreaterEqual(row["kelly"]["rawWeight"], 0.0)
            self.assertLessEqual(row["kelly"]["rawWeight"], limit + 1e-9,
                                 "%s 的分数凯利权重超过单只上限" % row["code"])

    def test_fstar_equals_capped_raw_times_shrink(self):
        """fStar = min(fStarRaw, KELLY_CAP) × shrink —— 三个字段互相可复核。"""
        checked = 0
        for row in batch_result()["rows"]:
            k = row["kelly"]
            if k["fStarRaw"] is None:
                continue
            expect = min(k["fStarRaw"], A.KELLY_CAP) * k["shrink"]
            self.assertAlmostEqual(k["fStar"], expect, delta=1e-3,
                                   msg="%s 的 f* 与「截断 × 收缩」不符" % row["code"])
            checked += 1
        self.assertGreater(checked, 0)

    def test_fraction_echo_and_weight_relation(self):
        """fraction 回显正确，且分数化后的权重不超过 f* × fraction（含取整容差）。"""
        res = run_recommend(["BUY"], kelly_fraction=0.25)
        row = row_of(res, "BUY")
        self.assertEqual(res["kellyFraction"], 0.25)
        self.assertEqual(row["kelly"]["fraction"], 0.25)
        self.assertLessEqual(row["kelly"]["rawWeight"],
                             row["kelly"]["fStar"] * 0.25 + 1e-3)

    def test_no_advantage_rows_have_zero_weight(self):
        """无优势（kind = none）的行必须 0 仓：f* = 0 → 权重 = 0 → 不参与分配。"""
        blocked = [r for r in batch_result()["rows"] if r["kelly"]["kind"] == "none"]
        self.assertTrue(blocked, "批量用例应覆盖「估计不出优势」的样本")
        for row in blocked:
            self.assertEqual(row["kelly"]["fStar"], 0.0, row["code"])
            self.assertEqual(row["kelly"]["rawWeight"], 0.0, row["code"])
            self.assertEqual(row["kelly"]["weight"], 0.0, row["code"])
            note = row["kelly"]["note"]
            # 两种退化原因（样本不足 / 分位差为 0）都必须写进 note，不能是空白
            self.assertTrue(("样本" in note) or ("离散度" in note),
                            "%s 的凯利说明没有交代退化的原因" % row["code"])


# --------------------------------------------------------------------------- #
# 8. 交易计划
# --------------------------------------------------------------------------- #
class TestPlan(unittest.TestCase):
    """计划必须可执行：价格全为正、方向单调、盈亏比为正。

    为什么强调单调性：止损在目标位之上（或反过来）会让前端画出一条自相矛盾的
    价格线，用户照着挂单必然亏损；这类错误只靠「值不为 None」是抓不到的。
    """

    def test_long_plan_monotonic(self):
        res = batch_result()
        for code in ("BUY", "BUY2", "ADD", "ADD0"):
            plan = row_of(res, code)["plan"]
            self.assertEqual(plan["direction"], "long", code)
            self.assertGreater(plan["stop"], 0.0, code)
            self.assertLess(plan["stop"], plan["entry"], code)
            self.assertLessEqual(plan["entry"], plan["target1"], code)
            self.assertLessEqual(plan["target1"], plan["target2"], code)
            self.assertGreater(plan["riskReward"], 0.0, code)
            self.assertGreater(plan["atr"], 0.0, code)

    def test_exit_plan_monotonic(self):
        res = batch_result()
        for code in ("REDUCE", "SELL", "AVOID_DD", "AVOID_VOL"):
            plan = row_of(res, code)["plan"]
            # 离场方向：止损在上方（价格反弹就止损），目标位在下方
            self.assertEqual(plan["direction"], "exit", code)
            self.assertGreater(plan["stop"], plan["entry"], code)
            self.assertGreaterEqual(plan["entry"], plan["target1"], code)
            self.assertGreaterEqual(plan["target1"], plan["target2"], code)
            self.assertGreater(plan["target2"], 0.0, code)
            self.assertGreater(plan["riskReward"], 0.0, code)

    def test_plan_direction_follows_action(self):
        """离场计划的触发档位是 reduce/sell/avoid，其余档位按建仓计划处理。"""
        res = batch_result()
        for row in res["rows"]:
            expect = "exit" if row["action"] in ("reduce", "sell", "avoid") else "long"
            self.assertEqual(row["plan"]["direction"], expect,
                             "%s 的 plan.direction 与档位不符" % row["code"])

    def test_entry_price_is_current_price(self):
        """现价作参考入场价：entry 必须等于行内的 price，否则用户无法核对。"""
        for row in batch_result()["rows"]:
            self.assertAlmostEqual(row["plan"]["entry"], row["price"], delta=1e-4, msg=row["code"])

    def test_long_stop_is_capped_at_12_percent(self):
        """建仓计划的止损不超过 -12%（高波动标的一次止损不该吃掉过多本金）。"""
        for row in batch_result()["rows"]:
            plan = row["plan"]
            if plan["direction"] != "long":
                continue
            self.assertLessEqual((plan["entry"] - plan["stop"]) / plan["entry"], 0.12 + 1e-9,
                                 "%s 的止损超过 -12%%" % row["code"])

    def test_plan_note_is_not_a_promise(self):
        for row in batch_result()["rows"]:
            note = row["plan"]["note"]
            self.assertIn("不是价格预测", note, row["code"])
            self.assertIn("不构成买卖建议", note, row["code"])
            self.assertIn("1.5×ATR", note, row["code"])

    def test_target_anchored_on_conditional_distribution(self):
        """回归：目标位必须优先锚定条件分布分位价。

        曾经 `_plan._level()` 用**字符串键**去查 `fc["quantiles"]`，而 forecast
        返回的是**整数键**（{5, 25, 50, 75, 95}），于是 `q.get("25")` 恒为 None：
        目标位静默退化成纯 ATR 倍数、`forecastTarget` 恒为 None，备注还写着
        「条件分布不可用」——文案与事实完全相反，是最难发现的一类缺陷。
        现已统一由 `core.advisor._qval()` 兼容两种键。
        """
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=20), "BUY")
        self.assertFalse(row["forecast"]["degraded"])
        self.assertGreater(row["forecast"]["sample"], 0)
        self.assertIsNotNone(row["plan"]["forecastTarget"],
                             "条件分布可用时，plan.forecastTarget 不应为 None")
        self.assertIn("统计分位", row["plan"]["note"])


# --------------------------------------------------------------------------- #
# 9. 预测带叠加层
# --------------------------------------------------------------------------- #
class TestForecastOverlay(unittest.TestCase):
    """advisor.forecast.path 是给图表直接画的叠加层，口径必须与 forecast 一致。

    关键约定：图表自己从最后一根收盘价起笔，所以叠加层**丢掉首点锚点**，
    只返回 horizon 个未来点。断言「长度 == horizon」等价于断言锚点没被重复画一次，
    而 `median[0] == 最后收盘价` 又反证了 forecast 原始路径确实多了那个锚点。
    """

    def test_path_length_is_horizon(self):
        cases = ((cached("single_buy", [{"code": "BUY"}], horizon=20), 20, "BUY"),
                 (cached("horizon_1", [{"code": "SIDE"}], horizon=1), 1, "SIDE"),
                 (cached("horizon_250", [{"code": "SIDE"}], horizon=250), 250, "SIDE"))
        for res, hz, code in cases:
            row = row_of(res, code)
            overlay = row["advisor"]["forecast"]
            self.assertEqual(res["horizon"], hz)
            self.assertEqual(overlay["horizon"], hz)
            self.assertEqual(len(overlay["path"]), hz,
                             "h=%d 时叠加层应是 h 个未来点（不含锚点）" % hz)
            self.assertEqual([p["i"] for p in overlay["path"]], list(range(hz)))

    def test_path_points_are_ordered_and_positive(self):
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=20), "BUY")
        for point in row["advisor"]["forecast"]["path"]:
            for key in ("t", "mid", "lo", "hi"):
                self.assertIn(key, point)
            self.assertLessEqual(point["lo"], point["mid"])
            self.assertLessEqual(point["mid"], point["hi"])
            self.assertGreater(point["lo"], 0.0)
            self.assertTrue(math.isfinite(point["mid"]))

    def test_path_drops_anchor_and_matches_forecast(self):
        """叠加层 = forecast 原始路径去掉锚点：逐点与原始中位路径严格对齐。"""
        hz = 20
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=hz), "BUY")
        raw = build_forecast(BARS["BUY"], horizon=hz)["path"]
        self.assertEqual(len(raw["median"]), hz + 1, "forecast 原始路径含锚点")
        self.assertAlmostEqual(raw["median"][0], row["price"], delta=1e-4)
        mids = [p["mid"] for p in row["advisor"]["forecast"]["path"]]
        for got, expect in zip(mids, raw["median"][1:]):
            self.assertAlmostEqual(got, expect, delta=1e-6)
        for point, lo, hi in zip(row["advisor"]["forecast"]["path"], raw["lo"][1:], raw["hi"][1:]):
            self.assertAlmostEqual(point["lo"], lo, delta=1e-6)
            self.assertAlmostEqual(point["hi"], hi, delta=1e-6)

    def test_path_levels_mapping(self):
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=20), "BUY")
        self.assertEqual(row["advisor"]["forecast"]["levels"],
                         {"lo": 5, "q25": 25, "q50": 50, "q75": 75, "hi": 95})

    def test_path_time_axis_is_strictly_increasing(self):
        """时间轴按尾部中位间隔外推：日线序列的未来点必须逐日递增且从次日起。"""
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=20), "BUY")
        axis = [p["t"] for p in row["advisor"]["forecast"]["path"]]
        last = date.fromisoformat(BARS["BUY"][-1]["t"])
        self.assertEqual(axis[0], (last + timedelta(days=1)).isoformat())
        days = [date.fromisoformat(t) for t in axis]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(set(days)), len(days))
        self.assertEqual((days[-1] - days[0]).days, len(days) - 1)

    def test_band_high_and_low_are_last_path_points(self):
        """forecast.bandLow / bandHigh 取的是预测带终点，必须与叠加层末点一致。"""
        row = row_of(cached("single_buy", [{"code": "BUY"}], horizon=20), "BUY")
        last = row["advisor"]["forecast"]["path"][-1]
        self.assertAlmostEqual(row["forecast"]["bandLow"], last["lo"], delta=1e-4)
        self.assertAlmostEqual(row["forecast"]["bandHigh"], last["hi"], delta=1e-4)


# --------------------------------------------------------------------------- #
# 10. K 线标记
# --------------------------------------------------------------------------- #
class TestMarks(unittest.TestCase):
    """图上标记必须可回溯：时间点能对回原始K线，方向只有买卖两种。

    为什么要断言「advice 标记唯一且落在最后一根」：AI 结论是「此刻的建议」，
    如果它被画到历史某一根上，用户会误以为那是历史信号；而如果每根K线都画一个，
    图会被糊满、标记失去意义。持有/观望档位不画标记也是同一条设计意图
    （没有可执行动作，就不该在图上下买卖箭头）。
    """

    def test_mark_fields_and_traceability(self):
        res = batch_result()
        for row in res["rows"]:
            times = set(b["t"] for b in BARS[row["code"]])
            for mark in row["advisor"]["marks"]:
                self.assertIn(mark["dir"], ("buy", "sell"), row["code"])
                self.assertIn(mark["kind"], ("trigger", "advice"), row["code"])
                self.assertIn(mark["t"], times,
                              "%s 的标记时间 %r 无法在输入K线里回溯" % (row["code"], mark["t"]))
                self.assertGreaterEqual(mark["idx"], 0)
                self.assertLess(mark["idx"], len(BARS[row["code"]]))
                self.assertTrue(mark["label"])
                self.assertTrue(mark["strategy"])

    def test_advice_mark_unique_and_on_last_bar(self):
        res = batch_result()
        checked = 0
        for row in res["rows"]:
            advice = [m for m in row["advisor"]["marks"] if m["kind"] == "advice"]
            expect = EXPECTED_MARK[row["action"]]
            if expect is None:
                self.assertEqual(advice, [], "%s 的档位不应产生 advice 标记" % row["code"])
                continue
            self.assertEqual(len(advice), 1, "%s 应恰好有一个 advice 标记" % row["code"])
            mark = advice[0]
            bars = BARS[row["code"]]
            self.assertEqual(mark["idx"], len(bars) - 1)
            self.assertEqual(mark["t"], bars[-1]["t"])
            self.assertEqual(mark["dir"], expect)
            self.assertEqual(mark["color"], A.MARK_COLOR[expect])
            self.assertEqual(mark["strategy"], "ai")
            self.assertEqual(mark["label"], A.ACTION_LABEL[row["action"]])
            checked += 1
        self.assertGreaterEqual(checked, 4, "用例应覆盖多种会画标记的档位")

    def test_hold_and_watch_have_no_advice_mark(self):
        res = batch_result()
        for code in ("HOLD", "WATCH"):
            row = row_of(res, code)
            self.assertIn(row["action"], ("hold", "watch"))
            self.assertEqual([m for m in row["advisor"]["marks"] if m["kind"] == "advice"],
                             [], "%s 不应有 advice 标记" % code)
            # 但仍然可以（而且通常应该）有策略触发信号标记
            self.assertTrue(any(m["kind"] == "trigger" for m in row["advisor"]["marks"]), code)

    def test_trigger_marks_are_windowed_and_capped(self):
        res = batch_result()
        for row in res["rows"]:
            triggers = [m for m in row["advisor"]["marks"] if m["kind"] == "trigger"]
            self.assertLessEqual(len(triggers), A.MAX_MARKS, row["code"])
            n = len(BARS[row["code"]])
            for mark in triggers:
                self.assertGreaterEqual(mark["idx"], n - A.MARK_WINDOW, row["code"])

    def test_marks_are_sorted_by_index(self):
        """标记按 K 线顺序排列（前端据此顺序遍历画图），advice 落在最后。"""
        row = row_of(batch_result(), "BUY")
        idx = [m["idx"] for m in row["advisor"]["marks"]]
        self.assertEqual(idx, sorted(idx))
        self.assertEqual(row["advisor"]["marks"][-1]["kind"], "advice")

    def test_mark_window_parameter_is_honoured(self):
        res = run_recommend(["BUY"], mark_window=10)
        row = row_of(res, "BUY")
        n = len(BARS["BUY"])
        for mark in row["advisor"]["marks"]:
            if mark["kind"] == "trigger":
                self.assertGreaterEqual(mark["idx"], n - 10)


# --------------------------------------------------------------------------- #
# 11. 统计优势口径
# --------------------------------------------------------------------------- #
class TestEdge(unittest.TestCase):
    """edge 描述的是「共识规则在同一段历史上回放出的单笔收益率分布」。

    只暴露最近 12 笔明细是刻意的（前端表格容量有限），但**笔数必须仍然完整**，
    否则用户会把「表格里 12 行」误当成「历史上只交易了 12 次」。
    """

    def test_trades_and_detail_length(self):
        res = batch_result()
        for row in res["rows"]:
            edge = row["edge"]
            self.assertIsInstance(edge["trades"], int)
            self.assertEqual(edge["sample"], edge["trades"])
            self.assertLessEqual(len(edge["detail"]), 12, row["code"])
            self.assertEqual(len(edge["detail"]), min(edge["trades"], 12), row["code"])

    def test_win_rate_is_fraction_and_consistent(self):
        res = batch_result()
        for row in res["rows"]:
            edge = row["edge"]
            if edge["trades"] == 0:
                self.assertIsNone(edge["winRate"], row["code"])
                continue
            self.assertIsNotNone(edge["winRate"])
            self.assertGreaterEqual(edge["winRate"], 0.0)
            self.assertLessEqual(edge["winRate"], 1.0)
            self.assertAlmostEqual(edge["winRate"], round(edge["wins"] / edge["trades"], 4),
                                   delta=1e-4, msg=row["code"])
            self.assertLessEqual(edge["wins"] + edge["losses"], edge["trades"])

    def test_detail_item_fields(self):
        checked = 0
        for row in batch_result()["rows"]:
            for trade in row["edge"]["detail"]:
                self.assertIn(trade["reason"], ("共识转空", "期末平仓"), row["code"])
                self.assertGreater(trade["bars"], 0, row["code"])
                self.assertGreater(trade["entry"], 0.0)
                self.assertGreater(trade["exit"], 0.0)
                self.assertTrue(math.isfinite(trade["ret"]))
                # 信号级收益率已扣手续费与滑点，因此必然 > -1（价格不会变成负数）
                self.assertGreater(trade["ret"], -1.0)
                checked += 1
        self.assertGreater(checked, 0)

    def test_payoff_semantics_with_and_without_losses(self):
        res = batch_result()
        # 有赢有亏：赔率 = 平均盈利 / 平均亏损，必须为正
        side = row_of(res, "SIDE")
        self.assertGreater(side["edge"]["wins"], 0)
        self.assertGreater(side["edge"]["losses"], 0)
        self.assertGreater(side["edge"]["payoff"], 0.0)
        self.assertFalse(side["edge"]["payoffInfinite"])
        self.assertGreater(side["edge"]["avgWin"], 0.0)
        self.assertGreater(side["edge"]["avgLoss"], 0.0)
        # 零亏损：赔率发散，按 None + payoffInfinite=True 表达（比填一个巨大数字更保守）
        buy = row_of(res, "BUY")
        self.assertGreater(buy["edge"]["trades"], 0)
        self.assertEqual(buy["edge"]["losses"], 0)
        self.assertIsNone(buy["edge"]["payoff"])
        self.assertTrue(buy["edge"]["payoffInfinite"])
        # 零样本：所有统计量都要退化成 None，而不是 0 或 NaN
        none_row = row_of(res, "AVOID_DD")
        self.assertEqual(none_row["edge"]["trades"], 0)
        self.assertIsNone(none_row["edge"]["winRate"])
        self.assertIsNone(none_row["edge"]["payoff"])
        self.assertFalse(none_row["edge"]["payoffInfinite"])
        self.assertEqual(none_row["edge"]["detail"], [])

    def test_expectancy_is_percent_and_equals_edge(self):
        """expectancy / edge 都是「每笔期望收益的百分数」，可用明细独立复算。"""
        row = row_of(batch_result(), "SIDE")     # 该样本 10 笔 < 12，明细 = 全样本
        edge = row["edge"]
        self.assertEqual(edge["trades"], len(edge["detail"]))
        mean_ret = sum(t["ret"] for t in edge["detail"]) / edge["trades"]
        self.assertAlmostEqual(edge["expectancy"], mean_ret * 100.0, delta=0.01)
        self.assertEqual(edge["edge"], edge["expectancy"])

    def test_hold_bars_is_average(self):
        row = row_of(batch_result(), "SIDE")
        edge = row["edge"]
        expect = sum(t["bars"] for t in edge["detail"]) / edge["trades"]
        self.assertAlmostEqual(edge["holdBars"], expect, delta=0.01)

    def test_last_trade_is_forced_close(self):
        """回放到期末必须强制平仓，否则最后一笔的浮盈浮亏会消失。"""
        for row in batch_result()["rows"]:
            detail = row["edge"]["detail"]
            if not detail:
                continue
            last_trade = detail[-1]
            if last_trade["reason"] == "期末平仓":
                self.assertEqual(last_trade["exitT"], BARS[row["code"]][-1]["t"], row["code"])

    def test_note_warns_about_sample_size(self):
        for row in batch_result()["rows"]:
            self.assertIn("样本量普遍偏小", row["edge"]["note"], row["code"])


# --------------------------------------------------------------------------- #
# 12. 退化与脏输入
# --------------------------------------------------------------------------- #
class TestDegenerate(unittest.TestCase):
    """任何坏输入都不许抛异常，且失败只影响自己那一行。

    这条是本模块对外的硬承诺（docstring 里的「不抛异常」），因为服务端是
    ThreadingHTTPServer：一个标的抛异常会让整批请求 500，用户看到的是
    「AI 选股坏了」，而不是「某只票数据缺失」。
    """

    def test_empty_bars_gives_error_row(self):
        res = run_recommend(["EMPTY"], feed=DirtyFeed())
        row = row_of(res, "EMPTY")
        self.assertTrue(res["ok"])
        self.assertFalse(row["ok"])
        self.assertIn("历史数据不足", row["error"])
        self.assertEqual(row["kelly"], None)
        self.assertEqual(row["action"], None)

    def test_short_history_gives_error_row(self):
        res = run_recommend(["SHORT"], feed=DirtyFeed())
        row = row_of(res, "SHORT")
        self.assertFalse(row["ok"])
        # 报错文案里要带上实际根数与门槛，否则用户不知道该补多少数据
        self.assertIn(str(A.MIN_BARS), row["error"])
        self.assertIn("30", row["error"])

    def test_dirty_bars_are_dropped_not_fatal(self):
        """个别K线脏掉时应「整根剔除」后继续算，而不是整只标失败。"""
        res = run_recommend(["BLANK"], feed=DirtyFeed())
        row = row_of(res, "BLANK")
        self.assertTrue(row["ok"], row.get("error"))
        expect = sum(1 for b in mixed_dirty_bars() if is_valid_bar(b))
        self.assertEqual(row["bars"], expect)
        self.assertGreaterEqual(row["bars"], A.MIN_BARS)
        self.assertAlmostEqual(row["price"], FIXTURES["WATCH"][79], delta=1e-4)

    def test_only_close_bars_is_supported(self):
        """只给 close 也要能算：open/high/low 缺失时用 close 兜底。"""
        res = run_recommend(["ONLYCLOSE"], feed=DirtyFeed())
        row = row_of(res, "ONLYCLOSE")
        self.assertTrue(row["ok"], row.get("error"))
        self.assertEqual(row["bars"], len(FIXTURES["WATCH"]))
        self.assertIsNotNone(row["plan"]["stop"])
        self.assertGreater(row["plan"]["atr"], 0.0)
        # 没有时间字段时 asOf / 标记时间只能是 None（此时无法回溯，属于预期退化）
        self.assertIsNone(row["asOf"])
        for mark in row["advisor"]["marks"]:
            self.assertIsNone(mark["t"])
        for point in row["advisor"]["forecast"]["path"]:
            self.assertIsNone(point["t"])

    def test_fetch_failure_is_isolated(self):
        """一只标的的网络异常不能影响同批其它标的。"""
        res = run_recommend(DIRTY_SYMBOLS, feed=DirtyFeed())
        self.assertEqual(res["count"], len(DIRTY_SYMBOLS))
        boom = row_of(res, "BOOM")
        self.assertFalse(boom["ok"])
        self.assertIn("行情获取失败", boom["error"])
        # 同批的正常标的照常给出结论
        good = row_of(res, "BUY")
        self.assertTrue(good["ok"])
        self.assertIn(good["action"], OK_ACTIONS)
        self.assertEqual(res["analyzed"],
                         sum(1 for r in res["rows"] if r["ok"]))
        self.assertEqual(res["analyzed"], 3, "本批应恰好有 3 只成功（ONLYCLOSE/BLANK/BUY）")

    def test_horizon_extremes(self):
        """horizon 1 / 250 是上下界；非法值一律回退默认值，且响应要回显真实取值。"""
        for given, expect in ((1, 1), (250, 250), (0, 1), (-3, 1), ("x", A.DEFAULT_HORIZON),
                              (None, A.DEFAULT_HORIZON), (10 ** 9, 250), (1.9, 1)):
            res = run_recommend(["SIDE"], horizon=given)
            self.assertEqual(res["horizon"], expect, "horizon=%r" % (given,))
            row = row_of(res, "SIDE")
            self.assertTrue(row["ok"])
            self.assertEqual(len(row["advisor"]["forecast"]["path"]), expect)
            self.assertEqual(row["advisor"]["forecast"]["horizon"], expect)

    def test_capital_is_sanitized(self):
        for given in (0, -5, None, "x", float("nan"), float("inf")):
            res = run_recommend(["BUY"], capital=given)
            self.assertEqual(res["capital"], A.DEFAULT_CAPITAL, "capital=%r" % (given,))
            self.assertTrue(row_of(res, "BUY")["ok"])
            # 本金非法时仍然要给出一份自洽的组合（不抛异常、不出现 NaN）；
            # 容差 0.01：cash 与 invested 各自四舍五入到 2 位小数
            self.assertAlmostEqual(res["portfolio"]["cash"] + res["portfolio"]["invested"],
                                   res["capital"], delta=0.01)

    def test_max_weight_is_sanitized(self):
        for given, expect in ((None, A.DEFAULT_MAX_WEIGHT), (0, A.DEFAULT_MAX_WEIGHT),
                              (-1, A.DEFAULT_MAX_WEIGHT), ("x", A.DEFAULT_MAX_WEIGHT),
                              (5, 1.0), (0.05, 0.05), (2, 1.0)):
            res = run_recommend(["BUY"], max_weight=given)
            self.assertAlmostEqual(res["maxWeight"], expect, delta=1e-9, msg="max_weight=%r" % (given,))
            row = row_of(res, "BUY")
            self.assertLessEqual(row["kelly"]["rawWeight"], expect + 1e-9)

    def test_kelly_fraction_is_sanitized(self):
        for given, expect in ((None, A.DEFAULT_FRACTION), (0, A.DEFAULT_FRACTION),
                              (-1, A.DEFAULT_FRACTION), ("x", A.DEFAULT_FRACTION),
                              (5, 1.0), (0.25, 0.25)):
            res = run_recommend(["BUY"], kelly_fraction=given)
            self.assertAlmostEqual(res["kellyFraction"], expect, delta=1e-9,
                                   msg="kelly_fraction=%r" % (given,))
            self.assertAlmostEqual(row_of(res, "BUY")["kelly"]["fraction"], expect, delta=1e-9)

    def test_cash_buffer_is_sanitized(self):
        for given, expect in ((None, A.DEFAULT_CASH_BUFFER), (-1, 0.0), (2, 1.0), (0.5, 0.5)):
            res = run_recommend(["BUY"], cash_buffer=given)
            self.assertAlmostEqual(res["cashBuffer"], expect, delta=1e-9,
                                   msg="cash_buffer=%r" % (given,))
            self.assertAlmostEqual(res["portfolio"]["capacity"], 1.0 - expect, delta=1e-9)


# --------------------------------------------------------------------------- #
# 12b. 数据源注入契约（含「绝不联网」）
# --------------------------------------------------------------------------- #
class TestDataInjection(unittest.TestCase):
    """数据获取器是注入的（依赖倒置），所以它的签名与参数传递本身就是接口。

    这类断言能抓住「把 market/period/limit 顺序写反」「limit 没有截断就传给上游」
    之类的错误 —— 它们不会抛异常，只会让上游多下载几倍数据。
    """

    def test_fetch_bars_receives_documented_arguments(self):
        feed = FakeFeed()
        run_recommend([{"code": "BUY2", "market": "us"}], feed=feed, period="week", limit=200)
        self.assertIn(("bars", "us", "BUY2", "week", 200), feed.calls)
        self.assertIn(("quote", "us", "BUY2"), feed.calls)

    def test_limit_is_clamped_before_reaching_the_feed(self):
        """limit 低于 MIN_BARS 会被抬到 MIN_BARS，过大则截到 3000（防止一次拉爆上游）。"""
        feed = FakeFeed()
        run_recommend(["BUY"], feed=feed, limit=10)
        self.assertIn(("bars", "cn", "BUY", "day", A.MIN_BARS), feed.calls)
        feed2 = FakeFeed()
        run_recommend(["BUY"], feed=feed2, limit=99999)
        self.assertIn(("bars", "cn", "BUY", "day", 3000), feed2.calls)

    def test_quote_overrides_price_and_change(self):
        res = run_recommend(["BUY"], quote=lambda market, code: {"price": 12.5, "changePct": -3.25})
        row = row_of(res, "BUY")
        self.assertEqual(row["price"], 12.5)
        self.assertEqual(row["changePct"], -3.25)
        self.assertEqual(row["plan"]["entry"], 12.5)

    def test_quote_failure_falls_back_to_last_close(self):
        def broken_quote(market, code):
            raise RuntimeError("行情接口超时")

        res = run_recommend(["BUY"], quote=broken_quote)
        row = row_of(res, "BUY")
        self.assertTrue(row["ok"])
        self.assertAlmostEqual(row["price"], FIXTURES["BUY"][-1], delta=1e-4)
        expect = (FIXTURES["BUY"][-1] / FIXTURES["BUY"][-2] - 1.0) * 100.0
        self.assertAlmostEqual(row["changePct"], expect, delta=0.01)

    def test_never_touches_the_network(self):
        """把 socket 全部打桩成异常：整条链路必须完全不依赖网络。

        为什么值得单独写：数据源是注入的，但只要有人在 advisor 里 import 了
        urllib 或 requests 做「兜底抓取」，测试环境没网就会静默退化 ——
        这个用例让这种回归立刻变成失败，而不是线上慢查询。
        """
        def boom(*args, **kwargs):
            raise AssertionError("测试期间不允许发起网络请求")

        with mock.patch.object(socket, "socket", boom), \
                mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket, "getaddrinfo", boom):
            res = run_recommend(["BUY", "SIDE"], max_workers=2)
        self.assertEqual(res["count"], 2)
        self.assertTrue(all(r["ok"] for r in res["rows"]))


# --------------------------------------------------------------------------- #
# 13. 批量上限、去重与保序（含多线程分支）
# --------------------------------------------------------------------------- #
class TestBatchLimits(unittest.TestCase):
    """批量层的三条契约：去重键、上限截断、顺序与输入一致。

    顺序尤其重要：`rows` 是前端表格的数据源，如果顺序随机（多线程最常见的坑），
    用户看到的「第一行」会每次刷新都变，无法对照。
    `pool.map` 天然保序，这里用断言把它钉住 —— 一旦有人改成 `as_completed`
    或 `submit`，测试会立刻失败。
    """

    PARALLEL_CODES = ["WATCH", "HOLD", "REDUCE", "SELL", "BUY", "ADD"]

    def test_duplicate_code_returns_single_row(self):
        res = run_recommend(["BUY", "BUY", "buy", {"code": "buy"}, {"code": "bUy", "market": "cn"}])
        self.assertEqual(res["requested"], 1)
        self.assertEqual(res["count"], 1)
        self.assertEqual([r["code"] for r in res["rows"]], ["BUY"])

    def test_same_code_different_market_is_two_symbols(self):
        """去重键是「市场:代码」：同一代码在 A 股与美股是两只不同标的。"""
        res = run_recommend([{"code": "BUY"}, {"code": "BUY", "market": "us"}])
        self.assertEqual(res["count"], 2)
        self.assertEqual([r["market"] for r in res["rows"]], ["cn", "us"])
        self.assertEqual(set(x["market"] for x in res["portfolio"]["rows"]),
                         set(r["market"] for r in res["rows"]))

    def test_codes_are_normalized_upper(self):
        self.assertEqual(run_recommend(["buy"])["rows"][0]["code"], "BUY")

    def test_max_symbols_truncates_and_keeps_input_order(self):
        codes = ["BUY", "SIDE", "HOLD", "WATCH"]
        res = run_recommend(codes, max_symbols=2)
        self.assertEqual(res["requested"], 2)
        self.assertEqual([r["code"] for r in res["rows"]], codes[:2])

    def test_max_symbols_is_clamped(self):
        # 下界 1：传 0 时至少分析一只（否则滑块调到 0 会变成「什么都不做」）
        self.assertEqual(run_recommend(["BUY", "SIDE", "HOLD"], max_symbols=0)["requested"], 1)
        # 上界 200：正常数量的标的不应被截断
        self.assertEqual(run_recommend(["BUY", "SIDE", "HOLD"], max_symbols=999)["requested"], 3)

    def test_symbols_accept_str_and_dict(self):
        res = run_recommend(["BUY", {"code": "SIDE", "name": "震荡"}])
        self.assertEqual(res["count"], 2)
        self.assertEqual(row_of(res, "SIDE")["name"], "震荡")

    def test_unusable_symbol_entries_are_skipped(self):
        for symbols in ([], None, "ABC", [None, 1, {}, {"code": ""}, {"code": "   "}], ()):
            res = run_recommend(symbols)
            self.assertTrue(res["ok"], repr(symbols))
            self.assertEqual(res["requested"], 0)
            self.assertEqual(res["rows"], [])

    def test_serial_branch_preserves_order(self):
        res = cached("serial", self.PARALLEL_CODES, max_workers=1)
        self.assertEqual(res["count"], len(self.PARALLEL_CODES))
        self.assertEqual([r["code"] for r in res["rows"]], self.PARALLEL_CODES)

    def test_parallel_branch_is_used_and_preserves_order(self):
        """并发 > 1 时必须走 ThreadPoolExecutor，且返回行数与顺序与输入一致。

        用 Spy 替换 ThreadPoolExecutor 来证明这条分支真的被执行了：
        否则「顺序正确」可能只是因为测试其实跑在单线程分支上。
        """
        real_pool = A.ThreadPoolExecutor
        used = []

        class SpyPool(object):
            def __init__(self, **kwargs):
                used.append(kwargs)
                self._pool = real_pool(**kwargs)

            def __enter__(self):
                return self._pool.__enter__()

            def __exit__(self, *exc):
                return self._pool.__exit__(*exc)

        with mock.patch.object(A, "ThreadPoolExecutor", SpyPool):
            res = run_recommend(self.PARALLEL_CODES, max_workers=4)

        self.assertEqual(len(used), 1, "并发 > 1 时应创建一次线程池")
        self.assertEqual(used[0]["max_workers"], 4)
        self.assertEqual(res["count"], len(self.PARALLEL_CODES))
        self.assertEqual([r["code"] for r in res["rows"]], self.PARALLEL_CODES)

    def test_parallel_result_matches_serial(self):
        """并发只影响「谁来算」，不能影响「算出什么」（无共享可变状态）。"""
        parallel = cached("parallel", self.PARALLEL_CODES, max_workers=4)
        serial = cached("serial", self.PARALLEL_CODES, max_workers=1)
        self.assertEqual([r["code"] for r in parallel["rows"]],
                         [r["code"] for r in serial["rows"]])
        for a, b in zip(parallel["rows"], serial["rows"]):
            self.assertEqual(a["action"], b["action"])
            self.assertEqual(a["score"], b["score"])
            self.assertEqual(a["confidence"], b["confidence"])
            self.assertEqual(a["price"], b["price"])
        self.assertEqual(parallel["portfolio"]["rows"], serial["portfolio"]["rows"])
        self.assertEqual(parallel["portfolio"]["totalWeight"],
                         serial["portfolio"]["totalWeight"])

    def test_empty_batch_does_not_touch_the_feed(self):
        feed = FakeFeed()
        run_recommend([], feed=feed)
        self.assertEqual(feed.calls, [])


# --------------------------------------------------------------------------- #
# 14. stance 契约
# --------------------------------------------------------------------------- #
class TestStances(unittest.TestCase):
    """stances() 是「7 个策略此刻的多空状态」，共识票数与历史回放都从这里来。

    它与 strategies.signal（事件式触发信号）是两套口径：同一根K线上通常只有
    0~1 个策略触发，凑不出共识，所以这里必须是**稠密的状态式**判断。
    """

    def test_keys_match_strategy_registry(self):
        st = A.stances(BARS["SIDE"], len(BARS["SIDE"]) - 1)
        self.assertEqual(list(st.keys()), list(S.keys()))
        self.assertEqual(A.STANCE_KEYS, tuple(S.keys()))
        self.assertEqual(len(st), 7)

    def test_stance_and_signal_domain(self):
        for code in ("BUY", "SIDE", "AVOID_DD", "WATCH", "HOLD"):
            bars = BARS[code]
            for i in (len(bars) - 1, len(bars) - 40, 61):
                for key, item in A.stances(bars, i).items():
                    self.assertIn(item["stance"], (-1, 0, 1), key)
                    self.assertIn(item["signal"], ("buy", "hold", "sell"), key)
                    self.assertEqual(item["signal"], A.VOTE_OF[item["stance"]])
                    self.assertEqual(item["name"], S.STRATEGIES[key]["name"])
                    self.assertTrue(item["brief"])

    def test_stance_net_equals_sum_and_is_bounded(self):
        for code in ("BUY", "SIDE", "AVOID_DD"):
            bars = BARS[code]
            for i in (len(bars) - 1, 61, 120, 200):
                st = A.stances(bars, i)
                net = A.stance_net({k: v["stance"] for k, v in st.items()})
                self.assertEqual(net, sum(v["stance"] for v in st.values()))
                self.assertGreaterEqual(net, -len(st))
                self.assertLessEqual(net, len(st))

    def test_stance_net_of_empty_vector(self):
        self.assertEqual(A.stance_net({}), 0)
        self.assertEqual(A.stance_net(None), 0)

    def test_stances_match_ensemble_votes(self):
        """行内 ensemble.votes 必须与直接调用 stances() 一致（同一份实现、同一根K线）。"""
        res = batch_result()
        for row in res["rows"]:
            bars = BARS[row["code"]]
            st = A.stances(bars, len(bars) - 1)
            for vote in row["ensemble"]["votes"]:
                self.assertEqual(vote["stance"], st[vote["strategy"]]["stance"], row["code"])
                self.assertEqual(vote["brief"], st[vote["strategy"]]["brief"], row["code"])

    def test_indicator_cache_is_reused(self):
        """传入 cache 时结果必须幂等（回放靠它把 7 个指标算一次复用 N 次）。"""
        bars = BARS["SIDE"]
        cache = {}
        first = A.stances(bars, len(bars) - 1, cache)
        self.assertTrue(cache)
        self.assertEqual(A.stances(bars, len(bars) - 1, cache), first)

    def test_degenerate_input_never_raises(self):
        """空K线 / 越界索引不能让 stances 抛异常（回放的边界会大量触发）。"""
        for bars, i in (([], 0), (BARS["SIDE"], len(BARS["SIDE"])), (BARS["SIDE"], 0),
                        (BARS["SIDE"][:3], 2)):
            st = A.stances(bars, i)
            self.assertEqual(len(st), 7)
            for item in st.values():
                self.assertIn(item["stance"], (-1, 0, 1))

    def test_missing_indicators_stay_neutral(self):
        """指标还不足以计算时必须是中性 0，而不是猜一个方向。

        注意这里**不包含 macd**：core.indicators.MACD 用 EMA 实现，EMA 从第一根K线
        就有值（不需要窗口预热），所以 5 根K线也能算出一个 DIF/DEA 比较结果；
        它在极端输入下「相等即看空」的口径问题由 TestKnownDefects 单独覆盖。
        """
        st = A.stances(BARS["SIDE"][:5], 4)
        for key in ("maCross", "rsi", "bollRevert", "kdjCross", "breakout", "momentum"):
            self.assertEqual(st[key]["stance"], 0, key)


# --------------------------------------------------------------------------- #
# 15. 参数来源一致性
# --------------------------------------------------------------------------- #
class TestParamSource(unittest.TestCase):
    """stance 的周期参数必须取自 core/strategies.default_params。

    怎么证明「没有在 advisor 里硬编码一份周期表」：把策略注册表里的默认周期改掉，
    再看 advisor 的输出是否跟着变。如果 advisor 自己抄了一份，改注册表将毫无效果。
    这条断言直接对着模块 docstring 的承诺（「不在本模块二次硬编码，避免口径漂移」）。
    """

    def test_stance_brief_follows_registry_default(self):
        bars, i = BARS["SIDE"], len(BARS["SIDE"]) - 1
        before = A.stances(bars, i)["maCross"]["brief"]
        param = S.STRATEGIES["maCross"]["params"][0]
        self.assertEqual(param["key"], "fast")
        self.assertEqual(S.default_params("maCross")["fast"], 5.0)
        old = param["def"]
        try:
            param["def"] = 7
            after = A.stances(bars, i)["maCross"]["brief"]
        finally:
            param["def"] = old
        self.assertIn("MA5", before)
        self.assertIn("MA7", after, "改注册表默认周期后 advisor 的输出没跟着变")
        self.assertNotIn("MA5", after)
        # 还原后必须回到原口径，确认测试自身没有污染全局状态
        self.assertEqual(A.stances(bars, i)["maCross"]["brief"], before)

    def test_recommend_rebuilds_params_from_registry(self):
        """每个请求都重建参数表（而不是模块导入时缓存一份），所以改注册表即时生效。"""
        param = S.STRATEGIES["momentum"]["params"][0]
        self.assertEqual(param["key"], "n")
        self.assertEqual(S.default_params("momentum")["n"], 20.0)
        old = param["def"]
        try:
            param["def"] = 10
            res = run_recommend(["SIDE"])
            brief = [v for v in row_of(res, "SIDE")["ensemble"]["votes"]
                     if v["strategy"] == "momentum"][0]["brief"]
        finally:
            param["def"] = old
        self.assertIn("近 10 根", brief)
        # 还原以后同一份数据要回到 20 根口径
        restored = [v for v in row_of(run_recommend(["SIDE"]), "SIDE")["ensemble"]["votes"]
                    if v["strategy"] == "momentum"][0]["brief"]
        self.assertIn("近 20 根", restored)

    def test_param_source_is_documented_and_exposed(self):
        self.assertIn("core/strategies.py", A.PARAM_SOURCE)
        for row in batch_result()["rows"]:
            self.assertEqual(row["ensemble"]["paramSource"], A.PARAM_SOURCE)


# --------------------------------------------------------------------------- #
# 16. 已修复缺陷的回归测试
# --------------------------------------------------------------------------- #
class TestRegressionFixes(unittest.TestCase):
    """曾经的三处真实缺陷，现已修复，用回归测试锁住。

    这类用例的价值在于：它们描述的是「正确行为」，一旦有人把修好的分支改回去，
    套件立刻变红 —— 比在代码里留一句注释更难被绕过。
    """

    def test_momentum_factor_window_matches_its_label(self):
        """回归：动量因子的文案窗口必须与数值窗口一致。

        曾经 `_factors()` 写「近 {horizon} 根涨跌 x%」，而 x 来自 `_indicators_at()`
        里硬编码的 20 根动量。horizon=5 时界面会显示「近 5 根涨跌 3.64%」，
        实际 3.64% 是 20 根收益（真实的 5 根收益是 1.00%）：用户照文案理解必然误判。
        现已统一到 `core.advisor.MOM_WINDOW = 20`，文案与计算共用同一个常量。
        """
        row = row_of(run_recommend(["BUY"], horizon=5), "BUY")
        factor = [f for f in row["signals"] if f["key"] == "momentum"][0]
        brief = factor["brief"]
        label_window = int(re.search(r"近 (\d+) 根", brief).group(1))
        self.assertEqual(label_window, A.MOM_WINDOW, brief)
        closes = FIXTURES["BUY"]
        # 用「哪个窗口的收益能复现文案里的百分数」反推数值的真实窗口
        matched = [n for n in (5, 10, 20, 60)
                   if ("%.2f%%" % ((closes[-1] / closes[-1 - n] - 1.0) * 100.0)) in brief]
        self.assertEqual(len(matched), 1, "无法唯一定位文案里的数值窗口：%s" % brief)
        self.assertEqual(matched[0], label_window,
                         "文案称「近 %d 根」，但数值来自 %d 根窗口：%s"
                         % (label_window, matched[0], brief))

    def test_flat_prices_do_not_get_a_bearish_vote(self):
        """回归：价格完全不变时，均线与 MACD 必须给中性票（0）而不是看空（-1）。

        曾经 `_s_ma_cross()` 与 `_s_macd()` 只判断「大于 / 否则」，没有相等分支：
        价格一字不变时（A 股停牌、连续一字板，或数据源补的空K线）快线 == 慢线、
        DIF == DEA，于是两个策略都投 -1，把「无信息」变成了「利空」，既污染共识
        票数也污染历史回放统计。同一个文件里的 `_s_kdj()` 明确写了 `K ≈ D` → 0，
        说明这是遗漏而非设计。
        """
        bars = make_bars([100.0] * 120)
        st = A.stances(bars, len(bars) - 1)
        self.assertEqual(st["maCross"]["stance"], 0, st["maCross"]["brief"])
        self.assertEqual(st["macd"]["stance"], 0, st["macd"]["brief"])
        # 其余策略在「零波动」输入下同样不应投出方向票
        self.assertEqual(A.stance_net({k: v["stance"] for k, v in st.items()}), 0,
                         "价格完全不变时净票应为 0：%s"
                         % {k: v["stance"] for k, v in st.items()})


if __name__ == "__main__":
    unittest.main(verbosity=2)