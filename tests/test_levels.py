# -*- coding: utf-8 -*-
"""core.levels（买卖点位引擎）的单元测试（仅标准库 unittest，可直接 python 运行）。

覆盖范围（与需求一一对应）
--------------------------
  1. TestPivotPoints    枢轴点公式**逐位**核对（手算 OHLC）+ 非法输入 + 与 plan 的 pivot 一致；
  2. TestAtrStops       Chandelier Exit 与手动复算一致、ATR 与 core/indicators.py 同口径、
                        紧档（2.0×ATR）确实更靠近现价、平坦行情走兜底而不是给 NaN；
  3. TestSwingLevels    摆动高低点确实来自「严格局部极值」，且 highs 降序 / lows 升序；
  4. TestVolumeNodes    量能密集区的箱中心落在最高量箱内（手算箱中心逐位核对）；
  5. TestEntries        分批建仓权重和 = 1、每档都高于初始止损、跌破止损的档被剔除且有 warning；
  6. TestRiskBudget     风险反算股数满足「单笔亏损 ≈ 本金 × risk_pct」、整手、max_weight 取小；
  7. TestExitRules      7 条退出规则齐全、priority 连续、action / condition 非空、触发原因可读；
  8. TestRiskReward     盈亏比用**返回的展示值**复算恒等（ratio1 = (T1 − entry) / (entry − stop)）；
  9. TestPlanContract   字段契约、asOf 来自最后一根K线（不是当前时间）、必备风险提示、
                        置信度分档与「样本不足时降到 0.1 元精度」；
 10. TestDegraded       数据不足（< 30 根）返回 ok=False 且**不含任何价位**；
 11. TestJsonStrictness 出口必须能 `json.dumps(..., allow_nan=False)`，且无 inf / NaN 泄漏。

为什么全部用确定性合成K线
--------------------------
点位引擎的输出完全由「价格序列 + 参数」决定，随机序列会让断言变成抽奖。因此这里用三类
**解析式**序列：正弦锯齿（有精确的局部极值，可手算摆动点）、单调上涨 + 小幅摆动（趋势场景）、
以及手工拼装的「放量杀跌 / 破位」序列（每条退出规则的触发条件都由测试自己构造）。
所有价格都由公式生成，不依赖任何随机数，跨环境、跨 Python 版本结果完全一致。

为什么价位的精度也要断言
------------------------
需求明确：样本不足时**不要给两位小数的「精确」价位**（假精确比粗精度更危险）。所以这里既断言
「>= 60 根样本时价位保留 4 位小数」，也断言「< 60 根时所有价位都恰好是 0.1 元的整数倍，
且 confidence.note 里出现『参考』」——把「降精度」当成契约而不是文案。

运行方式（tests/ 下无 __init__.py，直接跑文件最稳）：
    python3 tests/test_levels.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import json
import math
import os
import sys
import unittest
from datetime import date, timedelta

# 让 tests/ 目录之外的包（core）可被导入，兼容任意工作目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import indicators as I                                      # noqa: E402
from core import levels as L                                          # noqa: E402

BEGIN = date(2024, 1, 2)
#: 必备的风险提示关键词（需求第 7 条：这些提示是产品的一部分，不是装饰）
MANDATORY_WARNINGS = ("涨跌停", "T+1", "跳空", "样本", "不是承诺")
#: plan_levels 的必备字段
REQUIRED_KEYS = ("ok", "code", "price", "atr", "asOf", "support", "resistance", "pivot",
                 "entries", "targets", "stop", "riskReward", "risk", "exits", "holding",
                 "confidence", "warnings", "note")
#: 七个退出规则的规则名与优先级（顺序即优先级）
EXIT_RULES = ((1, "动量衰竭"), (2, "移动止损"), (3, "放量杀跌"), (4, "支撑破位"),
              (5, "止盈目标"), (6, "滞涨"), (7, "ATR 止损"))


# --------------------------------------------------------------------------- #
# 测试数据构造（全部确定性，可复现）
# --------------------------------------------------------------------------- #
def make_bars(closes, volumes=None, begin=BEGIN, hi=0.005, lo=0.005):
    """由收盘价序列构造标准K线：high/low 围绕收盘价按固定比例展开。

    刻意让 ``high = close × (1 + hi)``、``low = close × (1 − lo)``：
    这样「比较 high」等价于「比较 close」，局部极值可以被手算验证，
    也不会出现 `max(prev_close, close)` 那种「相邻两根 high 相等」的假平台
    （真实的严格局部极值定义会因此全部失效 —— 这是实测踩过的坑）。
    """
    bars = []
    for i, c in enumerate(closes):
        bars.append({
            "t": (begin + timedelta(days=i)).isoformat(),
            "open": c, "high": c * (1.0 + hi), "low": c * (1.0 - lo),
            "close": c, "volume": (volumes[i] if volumes else 1000000.0 + i),
        })
    return bars


def wave_closes(n=200, base=100.0, amp=0.06, period=20, drift=0.0005):
    """正弦锯齿：每 period/2 根出现一个明确的峰 / 谷（左右各 3 根严格更低 / 更高）。"""
    return [base * (1.0 + drift) ** i * (1.0 + amp * math.sin(2.0 * math.pi * i / period))
            for i in range(n)]


def wave_bars(n=200):
    return make_bars(wave_closes(n))


def chip_bars():
    """筹码分布测试数据：在 [100.0, 101.0] 这一带有 30 根等量大量，其余 90 根量很小。

    30 根重仓K线的收盘价**完全相同**（100.5），因此它们的典型价
    ``(H + L + C) / 3 = close``（本构造下 high/low 对称展开，典型价恰好等于收盘价）
    落在同一个价格箱里 —— 箱中心可以被测试逐位手算出来。
    """
    closes, vols = [], []
    for i in range(90):                      # 稀疏区：95.0 ~ 104.9 均匀铺开
        closes.append(95.0 + 10.0 * i / 89.0)
        vols.append(1000.0)
    for _ in range(30):                      # 密集区：全部落在 100.5
        closes.append(100.5)
        vols.append(500000.0)
    return make_bars(closes, vols)


def crash_bars():
    """前 60 根在 100 附近横盘，最后一根放量跌 8%（同时跌破 20 日低点）。"""
    closes = [100.0 + 0.5 * math.sin(i / 3.0) for i in range(60)]
    vols = [1000000.0] * 60
    closes.append(closes[-1] * 0.92)
    vols.append(10000000.0)
    return make_bars(closes, vols)


def uptrend_then_stall_bars():
    """强趋势（40 根 +1%）后连续几根涨幅骤缩（3 根 +0.05%）再收平：

    价格仍在高位（RSI(14) ≈ 100 ≫ 70），但 DIF 已经停止上行、被滞后上行的 DEA 反超，
    于是 MACD 在最后一两根内死叉 —— 这正是「动量衰竭」的教科书形态：
    **价格没跌，但推动价格的动量没了**。实测该序列在第 44 根出现死叉。
    """
    closes = [100.0 * (1.01 ** i) for i in range(40)]
    for _ in range(3):
        closes.append(closes[-1] * 1.0005)
    closes.append(closes[-1])
    return make_bars(closes)


def iter_floats(obj, path="root"):
    """递归遍历所有 float，便于断言「没有 inf / NaN 泄漏到 JSON 出口」。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            for item in iter_floats(v, "%s.%s" % (path, k)):
                yield item
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            for item in iter_floats(v, "%s[%d]" % (path, i)):
                yield item
    elif isinstance(obj, float):
        yield path, obj


def iter_strings(obj, path="root"):
    """递归遍历所有 str（用于检查文案里的 % 转义是否漏到输出）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            for item in iter_strings(v, "%s.%s" % (path, k)):
                yield item
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            for item in iter_strings(v, "%s[%d]" % (path, i)):
                yield item
    elif isinstance(obj, str):
        yield path, obj


def level_prices(plan):
    """取出 plan 里所有「对外发布的价位」，用于统一检查精度与方向。"""
    out = []
    for key in ("support", "resistance", "entries", "targets"):
        for it in plan.get(key) or []:
            out.append((key, it["price"]))
    for key in ("pp", "r1", "r2", "r3", "s1", "s2", "s3"):
        out.append(("pivot." + key, plan["pivot"][key]))
    out.append(("stop.initial", plan["stop"]["initial"]))
    out.append(("price", plan["price"]))
    return out


# --------------------------------------------------------------------------- #
# 1. 枢轴点
# --------------------------------------------------------------------------- #
class TestPivotPoints(unittest.TestCase):
    """枢轴点是纯机械公式：必须逐位对得上手算结果，否则整条交叉验证链都不可信。"""

    def test_formula_step_by_step(self):
        """H=110 / L=100 / C=105 手算：PP=105、R1=110、R2=115、R3=120、S1=100、S2=95、S3=90。"""
        r = L.pivot_points(110.0, 100.0, 105.0)
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["pp"], (110.0 + 100.0 + 105.0) / 3.0, places=9)   # 105
        self.assertAlmostEqual(r["r1"], 2 * 105.0 - 100.0, places=9)               # 110
        self.assertAlmostEqual(r["r2"], 105.0 + (110.0 - 100.0), places=9)         # 115
        self.assertAlmostEqual(r["r3"], 110.0 + 2 * (105.0 - 100.0), places=9)     # 120
        self.assertAlmostEqual(r["s1"], 2 * 105.0 - 110.0, places=9)               # 100
        self.assertAlmostEqual(r["s2"], 105.0 - (110.0 - 100.0), places=9)         # 95
        self.assertAlmostEqual(r["s3"], 100.0 - 2 * (110.0 - 105.0), places=9)     # 90
        # 机械关系：PP 必须夹在 S1 与 R1 之间，且 R1 > PP > S1 > S2 > S3
        self.assertLess(r["s3"], r["s2"])
        self.assertLess(r["s2"], r["s1"])
        self.assertLess(r["s1"], r["pp"])
        self.assertLess(r["pp"], r["r1"])
        self.assertLess(r["r1"], r["r2"])
        self.assertLess(r["r2"], r["r3"])

    def test_decimal_case(self):
        """第二组小数手算：H=10.5 / L=9.5 / C=10.0 → PP=10、R1=10.5、R2=11、R3=11.5、S1=9.5、S2=9、S3=8.5。"""
        r = L.pivot_points(10.5, 9.5, 10.0)
        self.assertAlmostEqual(r["pp"], 10.0, places=9)
        self.assertAlmostEqual(r["r1"], 10.5, places=9)
        self.assertAlmostEqual(r["r2"], 11.0, places=9)
        self.assertAlmostEqual(r["r3"], 11.5, places=9)
        self.assertAlmostEqual(r["s1"], 9.5, places=9)
        self.assertAlmostEqual(r["s2"], 9.0, places=9)
        self.assertAlmostEqual(r["s3"], 8.5, places=9)

    def test_high_low_swapped(self):
        """H / L 传反了不该算出负价位：自动互换，结果与正序完全一致。"""
        a = L.pivot_points(110.0, 100.0, 105.0)
        b = L.pivot_points(100.0, 110.0, 105.0)
        for key in ("pp", "r1", "r2", "r3", "s1", "s2", "s3"):
            self.assertAlmostEqual(a[key], b[key], places=9)

    def test_invalid_input_never_raises(self):
        """非法输入（None / 负数 / 0 / 字符串 / NaN）→ 7 个价位全为 None，不抛异常。"""
        for bad in [(None, 1, 2), (1, 2, 0), (1, 2, -3), ("a", 1, 2),
                    (float("nan"), 1, 2), (1, 2, float("inf"))]:
            r = L.pivot_points(*bad)
            self.assertIsInstance(r, dict)
            self.assertFalse(r["valid"])
            for key in ("pp", "r1", "r2", "r3", "s1", "s2", "s3"):
                self.assertIsNone(r[key], "%s 非法输入下不应给出价位" % key)
            self.assertIn("非法", r["note"])

    def test_plan_pivot_matches_last_bar(self):
        """plan_levels 的 pivot 必须等于最后一根K线的枢轴点（保留 2 位小数口径一致）。"""
        bars = wave_bars(200)
        r = L.plan_levels(bars, 20)
        raw = L.pivot_points(bars[-1]["high"], bars[-1]["low"], bars[-1]["close"])
        self.assertEqual(set(r["pivot"].keys()), {"pp", "r1", "r2", "r3", "s1", "s2", "s3"})
        for key in r["pivot"]:
            self.assertAlmostEqual(r["pivot"][key], round(raw[key], 4), places=4)


# --------------------------------------------------------------------------- #
# 2. Chandelier Exit 与 ATR 口径
# --------------------------------------------------------------------------- #
class TestAtrStops(unittest.TestCase):
    """Chandelier Exit = 最高价(n) − m×ATR(n)：必须能被测试独立复算，ATR 必须来自 core.indicators。"""

    def test_chandelier_matches_manual(self):
        bars = wave_bars(200)
        n, m = 22, 3.0
        hh = max(b["high"] for b in bars[-n:])
        atr = I.ATR(bars, n)[-1]
        got = L.atr_stops(bars)
        self.assertTrue(got["ok"])
        self.assertEqual(got["n"], n)
        self.assertAlmostEqual(got["mult"], m, places=9)
        self.assertAlmostEqual(got["highest"], hh, places=9)
        self.assertAlmostEqual(got["atr"], atr, places=9)
        self.assertAlmostEqual(got["chandelier"], hh - m * atr, places=9)
        self.assertIn("Chandelier", got["basis"])

    def test_atr_uses_same_indicator_as_core_indicators(self):
        """ATR 必须复用 core/indicators.ATR（同一份实现），不允许本模块再写一份。"""
        bars = wave_bars(200)
        self.assertAlmostEqual(L.atr_stops(bars)["atr"], I.ATR(bars, 22)[-1], places=9)
        plan = L.plan_levels(bars, 20)
        # plan 里的 atr 是「对外发布值」，按样本量精度取整（样本 120+ → 4 位小数）
        self.assertAlmostEqual(plan["atr"]["atr14"], round(I.ATR(bars, 14)[-1], 4), places=9)
        self.assertAlmostEqual(plan["atr"]["atr22"], round(I.ATR(bars, 22)[-1], 4), places=9)
        self.assertAlmostEqual(plan["atr"]["atr14"], I.ATR(bars, 14)[-1], delta=5e-5)

    def test_custom_params_recompute(self):
        """参数覆盖后仍要逐位对得上手算（n=10、m=2.5）。"""
        bars = wave_bars(200)
        got = L.atr_stops(bars, {"chandelierN": 10, "chandelierMult": 2.5})
        hh = max(b["high"] for b in bars[-10:])
        self.assertAlmostEqual(got["chandelier"], hh - 2.5 * I.ATR(bars, 10)[-1], places=9)

    def test_tight_alternative_is_tighter(self):
        """价格贴在近期高点时（趋势行情）：2.0×ATR(14) 紧档确实比 Chandelier(22,3) 更靠近现价。"""
        trend = make_bars([100.0 * (1.004 ** i) for i in range(160)])
        got = L.atr_stops(trend)
        self.assertIsNotNone(got["tight"])
        self.assertGreater(got["tight"]["price"], got["chandelier"],
                           "价格在高位时，紧档应比 Chandelier 更靠近现价")
        self.assertIn("更紧", got["tight"]["basis"])
        self.assertIn("胜率更低", got["tight"]["basis"])
        self.assertIn("更紧", got["note"])

    def test_chandelier_above_price_is_not_used_as_initial_stop(self):
        """价格已从近期高点回撤很多（Chandelier 落到现价上方）时，它不能当新仓的初始止损。

        这是实测踩到的坑：跌势里 ``最高价(22) − 3×ATR(22)`` 会高于现价（= 一开仓就该离场），
        如果机械地「取更紧者」就会得到一个距现价只有 0.5% 的荒谬止损，
        连带把 2、3 档建仓价全部剔除。正确做法是只用 ATR 止损，并把它写成 warning。
        """
        bars = wave_bars(200)                      # 最后一根落在波峰附近 → 现价低于 Chandelier
        stops = L.atr_stops(bars)
        plan = L.plan_levels(bars, 20)
        self.assertGreater(stops["chandelier"], plan["price"])
        self.assertLess(plan["stop"]["initial"], stops["chandelier"])
        # 初始止损 = 现价 − 2.0×ATR(14)（用未取整的 ATR 复算，验证实现没有二次取整）
        self.assertAlmostEqual(plan["stop"]["initial"],
                               round(plan["price"] - 2.0 * I.ATR(bars, 14)[-1], 4), places=4)
        self.assertTrue(any("Chandelier" in w for w in plan["warnings"]))
        self.assertIn("未纳入初始止损", plan["stop"]["basis"])
        self.assertTrue(2 <= len(plan["entries"]) <= 3)

    def test_insufficient_bars(self):
        got = L.atr_stops(wave_bars(3))
        self.assertFalse(got["ok"])
        self.assertIsNone(got["chandelier"])
        self.assertIn("不足", got["error"])

    def test_flat_series_falls_back_with_warning(self):
        """平坦行情（ATR=0）不能给出 NaN / 除零：走「现价 2% 兜底」并写 warning。"""
        bars = make_bars([100.0] * 60, hi=0.0, lo=0.0)   # high == low == close
        r = L.plan_levels(bars, 20)
        self.assertTrue(r["ok"])
        self.assertTrue(r["atr"]["fallback"])
        self.assertAlmostEqual(r["atr"]["pct"], 2.0, places=3)
        self.assertTrue(any("兜底" in w for w in r["warnings"]))


# --------------------------------------------------------------------------- #
# 3. 摆动高低点
# --------------------------------------------------------------------------- #
class TestSwingLevels(unittest.TestCase):
    """摆动高低点只能用「严格局部极值」定义，否则横盘平台会被误标成前高/前低。"""

    def setUp(self):
        self.bars = wave_bars(200)
        self.res = L.swing_levels(self.bars, 120, 4)

    def test_extremes_are_strict_local_extrema(self):
        """每个返回点必须真的是「左右各 k 根都更低 / 更高」，且价格等于那根K线的高/低点。"""
        self.assertTrue(self.res["ok"])
        k = self.res["k"]
        for item in self.res["highs"]:
            i = item["index"]
            self.assertAlmostEqual(item["price"], self.bars[i]["high"], places=9)
            for j in range(i - k, i + k + 1):
                if j == i:
                    continue
                self.assertGreater(item["price"], self.bars[j]["high"],
                                   "第 %d 根的 high 不是严格局部最大（邻居 %d 更高）" % (i, j))
        for item in self.res["lows"]:
            i = item["index"]
            self.assertAlmostEqual(item["price"], self.bars[i]["low"], places=9)
            for j in range(i - k, i + k + 1):
                if j == i:
                    continue
                self.assertLess(item["price"], self.bars[j]["low"],
                                "第 %d 根的 low 不是严格局部最小（邻居 %d 更低）" % (i, j))

    def test_sorted_and_limited(self):
        """highs 按价格降序、lows 按价格升序（严格单调），且各不超过 top 个。"""
        highs = [it["price"] for it in self.res["highs"]]
        lows = [it["price"] for it in self.res["lows"]]
        self.assertTrue(highs and lows)
        self.assertLessEqual(len(highs), 4)
        self.assertLessEqual(len(lows), 4)
        self.assertEqual(highs, sorted(highs, reverse=True))
        self.assertEqual(lows, sorted(lows))
        for a, b in zip(highs, highs[1:]):
            self.assertGreater(a, b)
        for a, b in zip(lows, lows[1:]):
            self.assertLess(a, b)

    def test_top_parameter(self):
        got = L.swing_levels(self.bars, 120, 2)
        self.assertLessEqual(len(got["highs"]), 2)
        self.assertLessEqual(len(got["lows"]), 2)

    def test_k_parameter_controls_strictness(self):
        """k 越大越严格：k=10 识别出的极值不会比 k=3 更多。"""
        loose = L.swing_levels(self.bars, 120, 20, 3)
        strict = L.swing_levels(self.bars, 120, 20, 10)
        self.assertLessEqual(len(strict["highs"]), len(loose["highs"]))
        self.assertLessEqual(len(strict["lows"]), len(loose["lows"]))

    def test_insufficient_bars(self):
        got = L.swing_levels(wave_bars(5))
        self.assertFalse(got["ok"])
        self.assertEqual(got["highs"], [])
        self.assertEqual(got["lows"], [])
        self.assertIn("不足", got["error"])


# --------------------------------------------------------------------------- #
# 4. 成交量密集区
# --------------------------------------------------------------------------- #
class TestVolumeNodes(unittest.TestCase):
    """量能密集区是「筹码分布的近似」：箱中心必须能被测试按分箱公式逐位复算。"""

    def setUp(self):
        self.bars = chip_bars()
        self.res = L.volume_nodes(self.bars, 120, 24, 3)

    def test_highest_volume_bin_center(self):
        bars = self.bars
        lo = min(b["low"] for b in bars)
        hi = max(b["high"] for b in bars)
        nb = 24
        width = (hi - lo) / nb
        idx = int((100.5 - lo) / width)                 # 30 根重仓K线的典型价 == 收盘价 100.5
        idx = max(0, min(nb - 1, idx))
        expect_center = lo + (idx + 0.5) * width
        top = self.res["nodes"][0]
        self.assertTrue(self.res["ok"])
        self.assertEqual(top["bin"], idx)
        self.assertAlmostEqual(top["price"], expect_center, places=9)
        self.assertLessEqual(top["low"], top["price"])
        self.assertLessEqual(top["price"], top["high"])
        # 30 根 × 500000 手 / 总量 = 最大占比
        shares = [n["share"] for n in self.res["nodes"]]
        self.assertEqual(shares, sorted(shares, reverse=True))
        expect_share = 30 * 500000.0 / (30 * 500000.0 + 90 * 1000.0)
        self.assertAlmostEqual(top["share"], expect_share, delta=0.02)
        self.assertEqual(self.res["pocBin"], idx)

    def test_nodes_are_not_adjacent(self):
        """相邻箱只保留更大的那个：避免同一片区域被重复计成多个密集区。"""
        bins = [n["bin"] for n in self.res["nodes"]]
        self.assertEqual(len(bins), len(set(bins)))
        for a in bins:
            for b in bins:
                if a != b:
                    self.assertGreater(abs(a - b), 1)

    def test_price_span_covers_data(self):
        bars = self.bars
        self.assertAlmostEqual(self.res["low"], min(b["low"] for b in bars), places=9)
        self.assertAlmostEqual(self.res["high"], max(b["high"] for b in bars), places=9)
        self.assertEqual(len(self.res["profile"]), self.res["bins"])

    def test_zero_volume_fails(self):
        """成交量为 0（缺 volume）时必须明确失败，而不是给出一条平的空分布。"""
        got = L.volume_nodes(make_bars(wave_closes(60), volumes=[0.0] * 60))
        self.assertFalse(got["ok"])
        self.assertIn("成交量", got["error"])
        self.assertEqual(got["nodes"], [])

    def test_invalid_range_fails(self):
        got = L.volume_nodes(make_bars([100.0] * 60, hi=0.0, lo=0.0))
        self.assertFalse(got["ok"])
        self.assertIn("区间", got["error"])


# --------------------------------------------------------------------------- #
# 5. 分批建仓
# --------------------------------------------------------------------------- #
class TestEntries(unittest.TestCase):
    """分批建仓是自定口径：可以自由设计权重的来源，但「权重和 = 1」与「每档高于止损」是硬约束。"""

    def test_default_two_or_three_tiers_above_stop(self):
        r = L.plan_levels(wave_bars(200), 20)
        entries = r["entries"]
        stop = r["stop"]["initial"]
        self.assertTrue(2 <= len(entries) <= 3, "分批档位应为 2~3 档，实际 %d" % len(entries))
        self.assertAlmostEqual(sum(e["weight"] for e in entries), 1.0, places=6)
        prices = [e["price"] for e in entries]
        for e in entries:
            self.assertGreater(e["price"], stop, "建仓档必须高于初始止损")
            self.assertTrue(e["label"] and e["note"])
        for a, b in zip(prices, prices[1:]):
            self.assertGreater(a, b, "越深的档位必须更便宜（严格递减）")
        self.assertAlmostEqual(prices[0], r["price"], places=4)   # 首档 = 现价

    def test_tier_below_stop_is_dropped_with_warning(self):
        """把末档放到 −3.0×ATR（深于 2.0×ATR 的初始止损）：该档必须被剔除且留下 warning。"""
        r = L.plan_levels(wave_bars(200), 20,
                          {"entryAtr": (0.0, 1.0, 3.0), "atrMult": 2.0, "anchorSupport": False})
        entries = r["entries"]
        stop = r["stop"]["initial"]
        self.assertEqual(len(entries), 2, "跌破止损的末档必须被剔除")
        self.assertAlmostEqual(sum(e["weight"] for e in entries), 1.0, places=6)
        for e in entries:
            self.assertGreater(e["price"], stop)
        hit = [w for w in r["warnings"] if "第 3 档" in w and "剔除" in w and "止损" in w]
        self.assertTrue(hit, "必须明确写出「哪一档被剔除、为什么」：%r" % (r["warnings"],))

    def test_strictly_three_tiers_when_isolated(self):
        """关掉支撑锚定后，三档就是纯 ATR 倍数（0 / 1 / 1.5），权重 5:3:2 且和 = 1。"""
        r = L.plan_levels(wave_bars(200), 20, {"anchorSupport": False})
        entries = r["entries"]
        self.assertEqual(len(entries), 3)
        self.assertAlmostEqual(entries[0]["weight"], 0.5, places=6)
        self.assertAlmostEqual(entries[1]["weight"], 0.3, places=6)
        self.assertAlmostEqual(entries[2]["weight"], 0.2, places=6)
        self.assertAlmostEqual(sum(e["weight"] for e in entries), 1.0, places=9)

    def test_stop_capped_at_twelve_percent(self):
        """极端波动（2×ATR ≫ 12%）时止损必须被 −12% 上限兜住，与 core/advisor.py 的阈值一致。"""
        closes = []
        for i in range(120):
            closes.append(100.0 * (1.12 if i % 2 else 0.88))
        r = L.plan_levels(make_bars(closes), 20, {"anchorSupport": False})
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["stop"]["initial"], round(r["price"] * 0.88, 4), places=4)
        for e in r["entries"]:
            self.assertGreater(e["price"], r["stop"]["initial"])
        self.assertAlmostEqual(sum(e["weight"] for e in r["entries"]), 1.0, places=6)
        self.assertTrue(any("上限" in b for b in [r["stop"]["basis"]]))
        self.assertTrue(any("剔除" in w for w in r["warnings"]),
                        "深于止损的档位被剔除时必须留下 warning")

    def test_anchor_support_pulls_tier_to_support(self):
        """打开锚定时，次档应落在「现价 − ATR」与「首个支撑」之间（取更深的那个）。"""
        r = L.plan_levels(wave_bars(200), 20, {"anchorSupport": True})
        r2 = L.plan_levels(wave_bars(200), 20, {"anchorSupport": False})
        if len(r["entries"]) >= 2 and len(r2["entries"]) >= 2:
            self.assertLessEqual(r["entries"][1]["price"], r2["entries"][1]["price"] + 1e-9)


# --------------------------------------------------------------------------- #
# 6. 风险预算（1% 规则）
# --------------------------------------------------------------------------- #
class TestRiskBudget(unittest.TestCase):
    """仓位由风险反算：单笔亏损必须约等于本金 × risk_pct，且必须是整手。"""

    def test_shares_match_one_percent_rule(self):
        """本金 10 万、风险 1%、每股风险 1 元 → 1000 股，实际亏损恰好 1000 元（占本金 1%）。"""
        r = L.risk_budget(10.0, 9.0, 100000.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["shares"], 1000)
        self.assertEqual(r["shares"] % 100, 0, "必须是整手（100 股）")
        self.assertAlmostEqual(r["riskAmount"], 100000.0 * 0.01, places=9)
        self.assertAlmostEqual(r["perTradePct"], 0.01, places=9)
        self.assertEqual(r["limitedBy"], "risk")
        self.assertIn("1% 规则", r["basis"])
        self.assertIn("10%", r["basis"])          # 连续 10 次全亏的回撤口径必须写出来
        self.assertIn("跳空", r["basis"])          # 失效边界之一

    def test_lot_rounding_error_less_than_one_lot(self):
        """非整除情形：取整误差必须小于「一手对应的风险」（向下取整 → 永不超预算）。"""
        r = L.risk_budget(10.0, 8.33, 100000.0)
        budget = 100000.0 * 0.01
        self.assertTrue(r["ok"])
        self.assertEqual(r["shares"] % 100, 0)
        self.assertLessEqual(r["riskAmount"], budget + 1e-9, "整手取整不得突破风险预算")
        self.assertGreater(budget - r["riskAmount"], 0.0)
        self.assertLess(budget - r["riskAmount"], 100 * (10.0 - 8.33) + 1e-9,
                        "取整损失应小于一手")

    def test_max_weight_takes_smaller(self):
        """max_weight 生效时取更小者：1% 规则给 10000 股，5% 上限只给 500 股。"""
        no_cap = L.risk_budget(10.0, 9.9, 100000.0)
        capped = L.risk_budget(10.0, 9.9, 100000.0, max_weight=0.05)
        self.assertEqual(no_cap["shares"], 10000)
        self.assertEqual(capped["shares"], 500)
        self.assertEqual(capped["limitedBy"], "max_weight")
        self.assertAlmostEqual(capped["amount"], 500 * 10.0, places=9)
        self.assertLessEqual(capped["weight"], 0.05)

    def test_risk_pct_override(self):
        r = L.risk_budget(10.0, 9.0, 100000.0, risk_pct=0.005)
        self.assertEqual(r["shares"], 500)

    def test_invalid_inputs(self):
        cases = [(10.0, 10.0, 100000.0), (10.0, 11.0, 100000.0), (10.0, 9.0, 0.0),
                 (None, 9.0, 100000.0), (10.0, None, 100000.0), (10.0, 9.0, None),
                 (0.0, 9.0, 100000.0), (10.0, 0.0, 100000.0)]
        for entry, stop, cap in cases:
            r = L.risk_budget(entry, stop, cap)
            self.assertIsInstance(r, dict)
            self.assertFalse(r["ok"])
            self.assertEqual(r["shares"], 0)
            self.assertTrue(r["error"])
        # 非法 risk_pct 回退到默认 1%（而不是 0 仓位或除零）
        r = L.risk_budget(10.0, 9.0, 100000.0, risk_pct="abc")
        self.assertEqual(r["shares"], 1000)

    def test_budget_too_small_gives_zero_but_ok(self):
        """本金太小买不满一手：shares=0 但 ok=True（输入没问题，只是钱不够），并说明原因。"""
        r = L.risk_budget(100.0, 99.0, 5000.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["shares"], 0)
        self.assertIn("一手", r["basis"])


# --------------------------------------------------------------------------- #
# 7. 优先级式退出规则
# --------------------------------------------------------------------------- #
class TestExitRules(unittest.TestCase):
    """退出规则必须是「优先级式」：第一个触发者胜出，且能说清是哪一条触发的。"""

    def test_seven_rules_with_continuous_priority(self):
        rules = L.exit_rules(wave_bars(200))
        self.assertEqual(len(rules), 7)
        self.assertEqual([(r["priority"], r["rule"]) for r in rules], list(EXIT_RULES))
        self.assertEqual([r["priority"] for r in rules], [1, 2, 3, 4, 5, 6, 7])
        for r in rules:
            self.assertTrue(r["action"].strip(), "每条规则都必须给出可执行动作")
            self.assertTrue(r["condition"].strip(), "每条规则都必须写明触发条件")
            self.assertIsInstance(r["triggered"], bool)
            self.assertTrue(r["detail"].strip(), "必须能直接推给用户：写明哪一条、差多少")

    def test_volume_crash_and_breakdown(self):
        """放量杀跌（量 10× 均量、跌 8%）与支撑破位（跌破 20 日低点）必须同时被判为触发。"""
        rules = {r["rule"]: r for r in L.exit_rules(crash_bars())}
        self.assertTrue(rules["放量杀跌"]["triggered"])
        self.assertIn("倍", rules["放量杀跌"]["detail"])
        self.assertTrue(rules["支撑破位"]["triggered"])
        self.assertIn("跌破", rules["支撑破位"]["detail"])
        self.assertFalse(rules["动量衰竭"]["triggered"])
        self.assertFalse(rules["移动止损"]["triggered"])

    def test_trailing_and_atr_stop(self):
        """持仓信息齐全时：自最高价回撤与跌破初始止损都要能判定。"""
        bars = crash_bars()
        pos = {"entry": 100.0, "stop": 96.0, "high": 105.0, "days": 5}
        rules = {r["rule"]: r for r in L.exit_rules(bars, pos)}
        self.assertTrue(rules["移动止损"]["triggered"])
        self.assertIn("回撤", rules["移动止损"]["detail"])
        self.assertTrue(rules["ATR 止损"]["triggered"], "收盘已跌破 96 的初始止损")
        self.assertFalse(rules["滞涨"]["triggered"], "持有 5 天未到门槛")
        self.assertFalse(rules["止盈目标"]["triggered"])

    def test_target_and_stagnation(self):
        """止盈目标与滞涨（持有久 + 收益低）各自独立可触发：两个条件来自不同的持仓成本。"""
        bars = crash_bars()
        last = bars[-1]["close"]
        # 成本 80（远低于现价 91.5）→ 首目标（成本 + 2×ATR）已到，但不是滞涨（收益 +14%）
        rules = {r["rule"]: r for r in L.exit_rules(bars, {"entry": 80.0, "days": 3})}
        self.assertTrue(rules["止盈目标"]["triggered"])
        self.assertFalse(rules["滞涨"]["triggered"])
        # 成本略高于现价、已持有 30 天 → 收益为负且时间到，判为滞涨（但未到首目标）
        rules2 = {r["rule"]: r for r in L.exit_rules(bars, {"entry": last * 1.005, "days": 30})}
        self.assertTrue(rules2["滞涨"]["triggered"])
        self.assertIn("持有", rules2["滞涨"]["detail"])
        self.assertFalse(rules2["止盈目标"]["triggered"])
        # 持有时间不足时滞涨不成立（哪怕收益为负）
        rules3 = {r["rule"]: r for r in L.exit_rules(bars, {"entry": last * 1.005, "days": 3})}
        self.assertFalse(rules3["滞涨"]["triggered"])

    def test_momentum_exhaustion(self):
        """RSI 仍在 70 上方但 MACD 死叉（强趋势后第一根阴线）→ 动量衰竭触发。"""
        rules = {r["rule"]: r for r in L.exit_rules(uptrend_then_stall_bars())}
        self.assertTrue(rules["动量衰竭"]["triggered"], rules["动量衰竭"]["detail"])
        self.assertIn("死叉", rules["动量衰竭"]["detail"])
        self.assertIn("RSI", rules["动量衰竭"]["detail"])

    def test_first_hit_is_min_priority(self):
        """多条同时触发时，第一个触发者 = priority 最小者（不会给出自相矛盾的建议）。"""
        bars = crash_bars()
        rules = L.exit_rules(bars, {"entry": 100.0, "stop": 96.0, "high": 105.0, "days": 30})
        hits = [r for r in rules if r["triggered"]]
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(hits[0], rules[[r["priority"] for r in rules].index(
            min(r["priority"] for r in hits))])
        self.assertEqual(hits[0]["priority"], 2)                 # 移动止损优先于破位 / 放量 / 止损

    def test_position_missing_is_honest(self):
        """不传持仓信息时：需要成本价的规则只给规则、不做判定，不能凭空假设成本。"""
        rules = {r["rule"]: r for r in L.exit_rules(wave_bars(200))}
        for name in ("移动止损", "止盈目标", "滞涨", "ATR 止损"):
            self.assertFalse(rules[name]["triggered"])
            self.assertIn("未提供", rules[name]["detail"])

    def test_no_position_still_judges_bar_only_rules(self):
        """反过来：不依赖持仓的规则（动量衰竭 / 放量杀跌 / 支撑破位）照常判定。"""
        rules = {r["rule"]: r for r in L.exit_rules(crash_bars())}
        self.assertTrue(rules["放量杀跌"]["triggered"])
        self.assertTrue(rules["支撑破位"]["triggered"])
        self.assertFalse(rules["移动止损"]["triggered"])

    def test_dirty_input_returns_list(self):
        for bad in ([], None, "x", [None, 1, "a"], [{}], [{"close": None}], 123):
            self.assertIsInstance(L.exit_rules(bad), list)


# --------------------------------------------------------------------------- #
# 8. 盈亏比
# --------------------------------------------------------------------------- #
class TestRiskReward(unittest.TestCase):
    """盈亏比必须能用**返回的展示值**复算 —— 否则前端与用户都无法核对。"""

    def test_ratio_matches_displayed_prices(self):
        r = L.plan_levels(wave_bars(200), 20)
        rr = r["riskReward"]
        entry = r["price"]
        stop = r["stop"]["initial"]
        t1 = r["targets"][0]["price"]
        t2 = r["targets"][1]["price"]
        self.assertAlmostEqual(rr["toT1"], t1, places=6)
        self.assertAlmostEqual(rr["toT2"], t2, places=6)
        self.assertAlmostEqual(rr["ratio1"], (t1 - entry) / (entry - stop), places=3)
        self.assertAlmostEqual(rr["ratio2"], (t2 - entry) / (entry - stop), places=3)
        self.assertGreater(rr["ratio2"], rr["ratio1"])
        self.assertTrue(rr["verdict"])
        self.assertIn("盈亏比", rr["verdict"])

    def test_default_ratio_is_two_to_one(self):
        """默认参数必须自洽：止损 2.0×ATR + 首目标 4.0×ATR = 2:1，结论不应是「偏低」。

        「止损 2×ATR + 目标 2×ATR」这种默认会让盈亏比恒等于 1:1、结论永远是「偏低」，
        属于自相矛盾的默认值（优化过程中真的写出来过）。
        """
        r = L.plan_levels(wave_bars(200), 20)
        self.assertAlmostEqual(r["riskReward"]["ratio1"], 2.0, places=2)
        self.assertNotIn("偏低", r["riskReward"]["verdict"])

    def test_verdict_thresholds(self):
        """结论必须跟着数值走：ratio1 = 首目标倍数 / 止损倍数，所以目标倍数决定结论。"""
        r = L.plan_levels(wave_bars(200), 20, {"targetAtr": (4.0, 8.0, 12.0)})
        self.assertAlmostEqual(r["riskReward"]["ratio1"], 2.0, places=3)   # 4×ATR / 2×ATR
        self.assertIn("良好", r["riskReward"]["verdict"])
        r2 = L.plan_levels(wave_bars(200), 20, {"targetAtr": (3.2, 6.0, 9.0)})
        self.assertAlmostEqual(r2["riskReward"]["ratio1"], 1.6, places=3)
        self.assertIn("尚可", r2["riskReward"]["verdict"])
        r3 = L.plan_levels(wave_bars(200), 20, {"targetAtr": (0.5, 1.0, 1.5)})
        self.assertIn("偏低", r3["riskReward"]["verdict"])


# --------------------------------------------------------------------------- #
# 9. 契约、置信度与必备风险提示
# --------------------------------------------------------------------------- #
class TestPlanContract(unittest.TestCase):
    """对外字段名是前端与接口的契约，一个都不能少；风险提示是产品的一部分。"""

    def setUp(self):
        self.bars = wave_bars(200)
        self.plan = L.plan_levels(self.bars, 20, {"code": "TEST"})

    def test_required_keys(self):
        self.assertTrue(self.plan["ok"])
        for key in REQUIRED_KEYS:
            self.assertIn(key, self.plan)
        self.assertEqual(self.plan["code"], "TEST")
        self.assertEqual(self.plan["holding"]["days"], 20)
        self.assertEqual(len(self.plan["exits"]), 7)
        self.assertEqual(set(self.plan["confidence"].keys()), {"level", "sample", "note"})
        self.assertEqual(set(self.plan["risk"].keys()),
                         {"perTradePct", "shares", "amount", "weight", "basis"})
        self.assertEqual(set(self.plan["riskReward"].keys()),
                         {"toT1", "toT2", "ratio1", "ratio2", "verdict"})
        self.assertEqual(set(self.plan["stop"].keys()),
                         {"initial", "basis", "trailStart", "trailLockIn", "chandelier", "note"})
        for it in self.plan["support"] + self.plan["resistance"]:
            self.assertEqual(set(it.keys()), {"price", "kind", "strength", "note"})
        for it in self.plan["entries"] + self.plan["targets"]:
            self.assertEqual(set(it.keys()), {"price", "weight", "label", "note"})

    def test_stop_three_stage_shape(self):
        """三段式止损的价格关系：initial < trailLockIn < trailStart，且锁定价在成本上方。"""
        stop = self.plan["stop"]
        self.assertLess(stop["initial"], self.plan["price"])
        self.assertLess(stop["initial"], stop["trailLockIn"])
        self.assertLess(stop["trailLockIn"], stop["trailStart"])
        self.assertGreater(stop["trailLockIn"], self.plan["price"])
        self.assertIn("freqtrade", stop["note"])
        self.assertIn("trailing_stop_positive", stop["note"])

    def test_asof_is_last_bar_time_not_now(self):
        """asOf 必须来自最后一根K线（不能用当前时间冒充），且 t 缺失时给 None。"""
        self.assertEqual(self.plan["asOf"], self.bars[-1]["t"])
        self.assertNotEqual(self.plan["asOf"], date.today().isoformat())
        no_time = [dict(b, t=None) for b in self.bars]
        self.assertIsNone(L.plan_levels(no_time, 20)["asOf"])

    def test_price_override(self):
        """price 覆盖（盘中实时价）后，所有点位按新价重算，并在 note 里说明差异。"""
        px = round(self.bars[-1]["close"] * 0.9, 4)
        r = L.plan_levels(self.bars, 20, None, px)
        self.assertAlmostEqual(r["price"], px, places=6)
        self.assertLess(r["stop"]["initial"], px)
        self.assertIn("传入价", r["note"])
        self.assertEqual(r["asOf"], self.bars[-1]["t"])
        # 非法 price 回退到最后一根收盘价（而不是失败或给 0）
        r2 = L.plan_levels(self.bars, 20, None, "abc")
        self.assertTrue(r2["ok"])
        self.assertAlmostEqual(r2["price"], round(self.bars[-1]["close"], 4), places=4)

    def test_horizon_echo_and_extremes(self):
        """horizon 会被原样回显到 holding.days（滞涨门槛随之对齐），极端值不抛异常。"""
        for hz in (1, 5, 20, 60, 250):
            r = L.plan_levels(self.bars, hz)
            self.assertTrue(r["ok"], "horizon=%d 不应失败" % hz)
            self.assertEqual(r["holding"]["days"], hz)
            self.assertAlmostEqual(sum(e["weight"] for e in r["entries"]), 1.0, places=6)
        self.assertEqual(L.plan_levels(self.bars, 20, {"stagnantDays": 3})["holding"]["days"], 20)

    def test_support_resistance_sides(self):
        r = self.plan
        for it in r["support"]:
            self.assertLess(it["price"], r["price"])
            self.assertIn(it["strength"], ("强", "中", "弱"))
            self.assertIn("支撑", it["note"])
        for it in r["resistance"]:
            self.assertGreater(it["price"], r["price"])
            self.assertIn(it["strength"], ("强", "中", "弱"))
            self.assertIn("阻力", it["note"])
        # 按距现价远近排序（近的在前）
        for key in ("support", "resistance"):
            dist = [abs(it["price"] - r["price"]) for it in r[key]]
            self.assertEqual(dist, sorted(dist))
        self.assertLessEqual(len(r["support"]), 4)
        self.assertLessEqual(len(r["resistance"]), 4)

    def test_multi_source_overlap_is_flagged(self):
        """多来源重叠的价位必须被显式标注（这是「强度」的核心依据）。"""
        notes = " ".join(it["note"] for it in self.plan["support"] + self.plan["resistance"])
        self.assertIn("个来源", notes)
        if "多来源重叠" not in notes:
            self.skipTest("本次数据没有出现多来源重叠，跳过（规则本身由 note 模板保证）")
        self.assertIn("多来源重叠的价位更有效", notes)

    def test_mandatory_warnings_always_present(self):
        """五条必备风险提示必须无条件返回，且与参数无关。"""
        for plan in (self.plan,
                     L.plan_levels(wave_bars(40), 5, {"capital": 10.0})):
            text = " ".join(plan["warnings"])
            for kw in MANDATORY_WARNINGS:
                self.assertIn(kw, text, "缺少必备风险提示：%s" % kw)
            self.assertTrue(any("不是承诺" in w for w in plan["warnings"]))
            self.assertTrue(any("T+1" in w for w in plan["warnings"]))

    def test_confidence_levels_and_precision(self):
        """样本 120+ → 高（4 位小数）；60~119 → 中；< 60 → 低且价位降到 0.1 元并标注参考。"""
        high = L.plan_levels(wave_bars(240), 20)
        self.assertEqual(high["confidence"]["level"], "高")
        self.assertEqual(high["confidence"]["sample"], 120)
        mid = L.plan_levels(wave_bars(80), 20)
        self.assertEqual(mid["confidence"]["level"], "中")
        self.assertEqual(mid["confidence"]["sample"], 80)
        low = L.plan_levels(wave_bars(40), 20)
        self.assertEqual(low["confidence"]["level"], "低")
        self.assertEqual(low["confidence"]["sample"], 40)
        self.assertIn("参考", low["confidence"]["note"])
        for key, v in level_prices(low):
            self.assertAlmostEqual(v, round(v, 1), places=9,
                                   msg="样本不足时 %s 应降到 0.1 元精度" % key)
        # 高置信度下可以更精细（至少有一个价位不是 0.1 的整数倍）
        finer = [v for _, v in level_prices(high)]
        self.assertTrue(any(abs(v - round(v, 1)) > 1e-9 for v in finer))

    def test_risk_block_matches_risk_budget(self):
        """plan 里的 risk 必须与直接调用 risk_budget 的结果一致（同一个口径）。"""
        r = self.plan
        rb = L.risk_budget(r["price"], r["stop"]["initial"], 100000.0, 0.01, 100, 0.25)
        self.assertEqual(r["risk"]["shares"], rb["shares"])
        self.assertAlmostEqual(r["risk"]["amount"], round(rb["amount"], 2), places=6)
        self.assertAlmostEqual(r["risk"]["perTradePct"], 0.01, places=9)

    def test_snake_case_params_alias(self):
        """params 支持 snake_case 写法（前端传参风格不统一），且非法值回退默认。"""
        r = L.plan_levels(self.bars, 20, {"atr_mult": 1.5, "risk_pct": 0.02, "lot": 100})
        self.assertIn("1.5×ATR", r["stop"]["basis"])
        self.assertAlmostEqual(r["risk"]["perTradePct"], 0.02, places=9)
        r2 = L.plan_levels(self.bars, 20, {"atrMult": "abc", "riskPct": -5, "lot": 0})
        self.assertIn("2×ATR", r2["stop"]["basis"])          # 回退默认 2.0
        self.assertAlmostEqual(r2["risk"]["perTradePct"], 0.0, places=9)   # 负数截断到 0

    def test_note_declares_caliber(self):
        """note 必须写清口径（止损依据 / 与 advisor 的关系 / asOf 来源）。"""
        note = self.plan["note"]
        self.assertIn("Chandelier", note)
        self.assertIn("advisor", note)
        self.assertIn("asOf", note)
        self.assertIn("不是价格预测", note)


# --------------------------------------------------------------------------- #
# 10. 数据不足与脏输入
# --------------------------------------------------------------------------- #
class TestDegraded(unittest.TestCase):
    """数据不足时必须 ok=False 且**不臆造价位**：宁可什么都不给，也不给假点位。"""

    def test_insufficient_bars_gives_no_levels(self):
        for n in (0, 1, 5, 29):
            r = L.plan_levels(wave_bars(n), 20)
            self.assertIsInstance(r, dict)
            self.assertFalse(r["ok"])
            self.assertIn("不足", r["error"])
            for key in ("support", "resistance", "pivot", "entries", "targets",
                        "stop", "riskReward", "risk", "exits"):
                self.assertNotIn(key, r, "失败返回值里不允许出现价位字段（%s）" % key)

    def test_boundary_30_bars_is_ok(self):
        """30 根是边界：刚好够用（但置信度必然是低）。"""
        r = L.plan_levels(wave_bars(30), 20)
        self.assertTrue(r["ok"])
        self.assertEqual(r["confidence"]["level"], "低")
        self.assertTrue(r["entries"])
        self.assertGreater(r["stop"]["initial"], 0.0)

    def test_dirty_inputs_never_raise(self):
        bad_cases = [
            [], None, "not-a-list", 123, {"close": 1}, [None, 1, "x"], [{}],
            [{"close": None}], [{"close": "abc"}], [{"close": float("nan")}],
            [{"close": float("inf")}], [{"close": -3}], [{"close": 0}], [{"close": True}],
            [{"high": 1, "low": 2, "close": 3, "open": "x"}],
            [{"open": 1, "high": 2, "low": 0, "close": 1, "volume": "x"}],
        ]
        for bad in bad_cases:
            for hz in (1, 20, 250):
                r = L.plan_levels(bad, hz)
                self.assertIsInstance(r, dict)
                self.assertFalse(r["ok"])
                self.assertTrue(r["error"])
        # 脏K线被整根剔除：好K线仍然照常出结果
        bars = wave_bars(120)
        bars[3]["close"] = None
        bars[7]["close"] = float("nan")
        bars[9] = "not-a-dict"
        r = L.plan_levels(bars, 20)
        self.assertTrue(r["ok"])

    def test_dirty_params_never_raise(self):
        bars = wave_bars(120)
        for params in (None, "x", [], 123, {"atrMult": None, "entryAtr": "x",
                                            "entryWeights": [1, 2, 3, 4],
                                            "position": "x", "capital": "abc"},
                       {"entryAtr": (0.0,), "entryWeights": (1.0,)},
                       {"atrN": 10 ** 9, "volumeBins": -5, "swingK": 0}):
            r = L.plan_levels(bars, 20, params)
            self.assertIsInstance(r, dict)
            self.assertTrue(r["ok"], "脏参数应回退默认值而不是失败：%r" % (params,))
            self.assertTrue(r["entries"])
            json.dumps(r, allow_nan=False)


# --------------------------------------------------------------------------- #
# 11. JSON 严格性
# --------------------------------------------------------------------------- #
class TestJsonStrictness(unittest.TestCase):
    """出口必须能被标准 JSON 严格序列化：NaN / inf 会让前端 JSON.parse 直接失败。"""

    def _all_payloads(self):
        bars = wave_bars(200)
        out = {
            "plan": L.plan_levels(bars, 20, {"code": "X"}),
            "low_sample": L.plan_levels(wave_bars(35), 20),
            "flat": L.plan_levels(make_bars([100.0] * 60, hi=0.0, lo=0.0), 20),
            "crash": L.plan_levels(crash_bars(), 20, {"position": {"entry": 100.0, "days": 30}}),
            "failed": L.plan_levels([], 20),
            "pivot": L.pivot_points(110.0, 100.0, 105.0),
            "atr": L.atr_stops(bars),
            "swing": L.swing_levels(bars),
            "volume": L.volume_nodes(chip_bars()),
            "risk": L.risk_budget(10.0, 9.0, 100000.0),
            "exits": L.exit_rules(crash_bars(), {"entry": 100.0, "stop": 96.0, "high": 105.0}),
        }
        return out

    def test_dumps_with_allow_nan_false(self):
        for label, payload in self._all_payloads().items():
            try:
                text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError) as e:
                self.fail("%s 无法严格序列化：%s" % (label, e))
            self.assertNotIn("NaN", text)
            self.assertNotIn("Infinity", text)
            again = json.loads(text)
            self.assertEqual(type(again), type(payload))

    def test_every_float_is_finite(self):
        for label, payload in self._all_payloads().items():
            for path, value in iter_floats(payload):
                self.assertTrue(math.isfinite(value),
                                "%s 泄漏了非有限浮点：%s = %r" % (label, path, value))

    def test_no_bool_in_numeric_fields(self):
        """bool 是 int 的子类，混进数值字段会让 JSON 变成 true/false。"""
        plan = self._all_payloads()["plan"]
        for it in plan["entries"]:
            self.assertNotIsInstance(it["price"], bool)
            self.assertNotIsInstance(it["weight"], bool)
        self.assertNotIsInstance(plan["stop"]["initial"], bool)
        self.assertNotIsInstance(plan["risk"]["shares"], bool)
        for it in plan["exits"]:
            self.assertNotIsInstance(it["priority"], bool)
        self.assertNotIsInstance(plan["confidence"]["sample"], bool)

    def test_no_percent_escape_leak_in_text(self):
        """文案里不允许出现 ``%%``。

        本模块的文案含大量字面百分号（1% 规则、8% 回撤…），用 ``%`` 格式化时若漏写转义
        会在运行期抛 ``ValueError``（实测踩过两次）；反过来，把 ``%%`` 写进**非格式化**
        字符串则会把 ``%%`` 原样吐给用户。这条断言从输出侧堵住第二种情况。
        """
        for label, payload in self._all_payloads().items():
            for path, text in iter_strings(payload):
                self.assertNotIn("%%", text, "%s.%s 泄漏了未转义的 %%" % (label, path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
