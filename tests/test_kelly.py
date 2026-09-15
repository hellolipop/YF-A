# -*- coding: utf-8 -*-
"""core.kelly 的单元测试（仅用标准库 unittest，可直接 python 运行）。

覆盖内容：
* 离散凯利 / 连续凯利的数值正确性与非法输入容错；
* 分数凯利的缩放、上限截断、下限归零、参数矛盾；
* 组合分配的归一化、现金缓冲、A股/美股整手取整、资金不足按比例缩减；
* 边界：无优势、样本不足、权重全零、权重超上限、取整后不足一手、
  资金非法、脏数据（None / 非字典候选 / 缺价格）等。

运行方式：
    python -m unittest discover -s tests -t .
    python tests/test_kelly.py
"""

import os
import sys
import unittest

# 让 tests/ 目录之外的包（core）可被导入，兼容任意工作目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.kelly import (  # noqa: E402
    DEFAULT_CASH_BUFFER, DEFAULT_FRACTION, DEFAULT_MAX_WEIGHT, DEFAULT_MIN_WEIGHT,
    LOTS, MIN_SAMPLES, allocate, fractional, kelly_continuous, kelly_fraction,
)

#: 各函数输出字段（顺序无关，用于锁定对外接口）
KF_KEYS = {"f", "raw", "edge", "win_rate", "payoff", "cap", "capped", "valid",
           "advice", "note"}
KC_KEYS = {"f", "raw", "mean_ret", "var_ret", "vol", "valid", "advice", "note"}
FR_KEYS = {"weight", "raw", "kelly", "fraction", "max_weight", "min_weight",
           "capped", "zeroed", "valid", "reason", "note"}
ALLOC_KEYS = {
    "positions", "orders", "skipped", "capital", "valid_capital", "fraction",
    "max_weight", "min_weight", "cash_buffer", "capacity", "budget", "target_cash",
    "invested", "invested_weight", "cash", "cash_weight", "reduced", "shrink",
    "count", "note",
}
ROW_KEYS = {
    "code", "name", "market", "price", "lot", "samples", "kelly", "kelly_kind",
    "kelly_weight", "weight", "amount", "shares", "actual", "capped", "zeroed",
    "action", "reason", "note",
}


def cand(code, price=9.0, win_rate=0.6, payoff=1.0, market="cn", **kw):
    """构造一个候选（默认：胜率 60% / 赔率 1 → 凯利 0.2 → 半凯利权重 0.1）。"""
    d = {"code": code, "price": price, "win_rate": win_rate, "payoff": payoff,
         "market": market}
    d.update(kw)
    return d


def alloc(row, capital=100000.0, **kw):
    return allocate([row], capital, **kw)


# --------------------------------------------------------------------------- #
# 一、离散凯利
# --------------------------------------------------------------------------- #
class TestKellyFraction(unittest.TestCase):
    """f* = (p·b − q) / b 及其非法输入、无优势、上限截断。"""

    def test_key_set(self):
        self.assertEqual(set(kelly_fraction(0.6, 2.0).keys()), KF_KEYS)

    def test_classic_half_edge(self):
        """胜率 50%、赔率 2 → f* = (0.5×2 − 0.5) / 2 = 0.25。"""
        r = kelly_fraction(0.5, 2.0)
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["f"], 0.25, places=12)
        self.assertAlmostEqual(r["raw"], 0.25, places=12)
        self.assertAlmostEqual(r["edge"], 0.5, places=12)
        self.assertFalse(r["capped"])
        self.assertEqual(r["cap"], 1.0)
        self.assertIn("25.00%", r["advice"])

    def test_payoff_one(self):
        """赔率 1（赢亏同额）时 f* = p − q = 0.2。"""
        self.assertAlmostEqual(kelly_fraction(0.6, 1.0)["f"], 0.2, places=12)

    def test_third_edge(self):
        """胜率 50%、赔率 3 → f* = (1.5 − 0.5) / 3 = 1/3。"""
        self.assertAlmostEqual(kelly_fraction(0.5, 3.0)["f"], 1.0 / 3.0, places=12)

    def test_fair_bet_has_no_edge(self):
        r = kelly_fraction(0.5, 1.0)
        self.assertTrue(r["valid"])
        self.assertEqual(r["f"], 0.0)
        self.assertAlmostEqual(r["edge"], 0.0, places=12)
        self.assertIn("无优势", r["advice"])

    def test_no_edge_never_returns_negative_f(self):
        # 赔率 1 / 2 / 10 的盈亏平衡胜率分别为 50% / 33.3% / 9.1%
        for p, b in ((0.0, 10.0), (0.2, 1.0), (0.5, 1.0), (0.3, 2.0), (0.09, 10.0)):
            with self.subTest(p=p, b=b):
                r = kelly_fraction(p, b)
                self.assertEqual(r["f"], 0.0)
                self.assertLessEqual(r["raw"], 0.0)
                self.assertIn("不建议建仓", r["advice"])

    def test_full_win_at_default_cap(self):
        """胜率 100%、赔率 10 → f* = 1.0，恰好等于默认上限，不算截断。"""
        r = kelly_fraction(1.0, 10.0)
        self.assertAlmostEqual(r["f"], 1.0, places=12)
        self.assertFalse(r["capped"])

    def test_cap_truncation(self):
        r = kelly_fraction(0.9, 10.0, cap=0.5)
        self.assertAlmostEqual(r["raw"], 0.89, places=12)
        self.assertAlmostEqual(r["f"], 0.5, places=12)
        self.assertTrue(r["capped"])
        self.assertIn("已截断", r["advice"])

    def test_cap_zero_forces_flat(self):
        r = kelly_fraction(0.9, 10.0, cap=0)
        self.assertEqual(r["f"], 0.0)
        self.assertTrue(r["capped"])

    def test_illegal_cap_falls_back_to_default(self):
        a = kelly_fraction(0.6, 2.0, cap=None)
        b = kelly_fraction(0.6, 2.0, cap="abc")
        c = kelly_fraction(0.6, 2.0, cap=-3)
        d = kelly_fraction(0.6, 2.0)
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertEqual(c["cap"], 1.0)
        self.assertEqual(d["f"], c["f"])

    def test_invalid_inputs_return_zero(self):
        cases = [
            (None, 2.0), (0.6, None), ("abc", 2.0), (0.6, "abc"), (True, 2.0),
            (0.6, True), (float("nan"), 2.0), (float("inf"), 2.0),
            (-0.1, 2.0), (1.5, 2.0), (0.6, 0), (0.6, -1.0), (0.6, float("nan")),
            ("", 2.0), ([], 2.0),
        ]
        for p, b in cases:
            with self.subTest(p=p, b=b):
                r = kelly_fraction(p, b)
                self.assertEqual(set(r.keys()), KF_KEYS)
                self.assertEqual(r["f"], 0.0)
                self.assertEqual(r["raw"], 0.0)
                self.assertEqual(r["edge"], 0.0)
                self.assertFalse(r["valid"])
                self.assertFalse(r["capped"])
                self.assertIn("输入非法", r["advice"])

    def test_boundaries_are_valid(self):
        self.assertTrue(kelly_fraction(0.0, 1.0)["valid"])
        self.assertTrue(kelly_fraction(1.0, 1.0)["valid"])
        self.assertAlmostEqual(kelly_fraction(1.0, 1.0)["f"], 1.0, places=12)

    def test_monotonic_in_win_rate(self):
        """赔率 1 时：胜率 ≤ 50% 一律无优势（0），此后随胜率严格递增。"""
        prev = -1.0
        for p in (0.3, 0.5):
            self.assertEqual(kelly_fraction(p, 1.0)["f"], 0.0)
        for p in (0.5, 0.6, 0.7, 0.8, 0.9):
            f = kelly_fraction(p, 1.0)["f"]
            self.assertGreater(f, prev)
            prev = f

    def test_string_number_tolerated(self):
        r = kelly_fraction("0.6", "2")
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["f"], 0.4, places=12)      # (0.6×2 − 0.4) / 2

    def test_negative_zero_normalised(self):
        self.assertNotEqual(repr(kelly_fraction(0.5, 1.0)["f"]), "-0.0")
        self.assertNotEqual(repr(kelly_fraction(0.5, 1.0)["raw"]), "-0.0")

    def test_note_mentions_formula(self):
        self.assertIn("f* = (p·b − q) / b", kelly_fraction(0.6, 2.0)["note"])


# --------------------------------------------------------------------------- #
# 二、连续凯利
# --------------------------------------------------------------------------- #
class TestKellyContinuous(unittest.TestCase):
    """f* = μ / σ² 及其退化情形。"""

    def test_key_set(self):
        self.assertEqual(set(kelly_continuous(0.01, 0.04).keys()), KC_KEYS)

    def test_basic(self):
        r = kelly_continuous(0.01, 0.04)
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["f"], 0.25, places=12)
        self.assertAlmostEqual(r["raw"], 0.25, places=12)
        self.assertAlmostEqual(r["vol"], 0.2, places=12)

    def test_vol_is_sqrt_var(self):
        r = kelly_continuous(0.005, 0.0025)
        self.assertAlmostEqual(r["vol"], 0.05, places=12)

    def test_over_one_needs_leverage(self):
        r = kelly_continuous(0.02, 0.01)
        self.assertAlmostEqual(r["f"], 2.0, places=12)
        self.assertIn("杠杆", r["advice"])

    def test_tiny_variance_blows_up(self):
        r = kelly_continuous(0.01, 1e-6)
        self.assertAlmostEqual(r["f"], 10000.0, places=6)
        self.assertIn("杠杆", r["advice"])

    def test_negative_mean_has_no_edge(self):
        r = kelly_continuous(-0.01, 0.04)
        self.assertTrue(r["valid"])
        self.assertEqual(r["f"], 0.0)
        self.assertAlmostEqual(r["raw"], -0.25, places=12)
        self.assertIn("无优势", r["advice"])

    def test_zero_mean(self):
        r = kelly_continuous(0.0, 0.04)
        self.assertEqual(r["f"], 0.0)
        self.assertIn("无优势", r["advice"])

    def test_zero_and_negative_variance_invalid(self):
        for var in (0.0, -0.04, None, "abc", float("nan"), float("inf")):
            with self.subTest(var=var):
                r = kelly_continuous(0.01, var)
                self.assertEqual(r["f"], 0.0)
                self.assertEqual(r["vol"], 0.0)
                self.assertFalse(r["valid"])
                self.assertIn("输入非法", r["advice"])

    def test_invalid_mean(self):
        for mu in (None, "x", True, float("nan")):
            with self.subTest(mu=mu):
                self.assertFalse(kelly_continuous(mu, 0.04)["valid"])
                self.assertEqual(kelly_continuous(mu, 0.04)["f"], 0.0)

    def test_monotonic_in_variance(self):
        a = kelly_continuous(0.01, 0.01)["f"]
        b = kelly_continuous(0.01, 0.04)["f"]
        self.assertGreater(a, b)

    def test_note_mentions_formula(self):
        self.assertIn("f* = μ / σ²", kelly_continuous(0.01, 0.04)["note"])


# --------------------------------------------------------------------------- #
# 三、分数凯利 + 上下限截断
# --------------------------------------------------------------------------- #
class TestFractional(unittest.TestCase):
    """weight = f × fraction，上限截断、下限归零。"""

    def test_key_set(self):
        self.assertEqual(set(fractional(0.4).keys()), FR_KEYS)

    def test_plain_scaling(self):
        r = fractional(0.4)
        self.assertEqual(r["fraction"], DEFAULT_FRACTION)
        self.assertEqual(r["max_weight"], DEFAULT_MAX_WEIGHT)
        self.assertEqual(r["min_weight"], DEFAULT_MIN_WEIGHT)
        self.assertAlmostEqual(r["raw"], 0.2, places=12)
        self.assertAlmostEqual(r["weight"], 0.2, places=12)
        self.assertFalse(r["capped"])
        self.assertFalse(r["zeroed"])
        self.assertTrue(r["valid"])

    def test_defaults(self):
        self.assertEqual(DEFAULT_FRACTION, 0.5)
        self.assertEqual(DEFAULT_MAX_WEIGHT, 0.25)
        self.assertEqual(DEFAULT_MIN_WEIGHT, 0.02)

    def test_capped_at_max_weight(self):
        r = fractional(0.8)
        self.assertAlmostEqual(r["raw"], 0.4, places=12)
        self.assertAlmostEqual(r["weight"], 0.25, places=12)
        self.assertTrue(r["capped"])
        self.assertFalse(r["zeroed"])
        self.assertIn("上限", r["reason"])

    def test_below_min_weight_goes_to_zero(self):
        r = fractional(0.02)
        self.assertAlmostEqual(r["raw"], 0.01, places=12)
        self.assertEqual(r["weight"], 0.0)
        self.assertTrue(r["zeroed"])
        self.assertIn("下限", r["reason"])

    def test_exactly_min_weight_is_kept(self):
        """恰好等于下限不归零（边界含等号）。"""
        r = fractional(0.04)
        self.assertAlmostEqual(r["weight"], 0.02, places=12)
        self.assertFalse(r["zeroed"])

    def test_exactly_max_weight_is_kept(self):
        r = fractional(0.5)
        self.assertAlmostEqual(r["weight"], 0.25, places=12)
        self.assertFalse(r["capped"])

    def test_no_edge_zeroed(self):
        for f in (0.0, -0.2, -3.0):
            with self.subTest(f=f):
                r = fractional(f)
                self.assertEqual(r["weight"], 0.0)
                self.assertTrue(r["zeroed"])
                self.assertIn("无优势", r["reason"])

    def test_invalid_kelly(self):
        for bad in (None, "abc", True, float("nan"), float("inf"), []):
            with self.subTest(bad=bad):
                r = fractional(bad)
                self.assertEqual(r["weight"], 0.0)
                self.assertFalse(r["valid"])
                self.assertTrue(r["zeroed"])
                self.assertIn("非法", r["reason"])

    def test_fraction_param(self):
        self.assertAlmostEqual(fractional(0.2, fraction=1.0)["weight"], 0.2, places=12)
        self.assertAlmostEqual(fractional(0.2, fraction=0.25)["weight"], 0.05, places=12)
        self.assertEqual(fractional(0.2, fraction=0.0)["weight"], 0.0)

    def test_fraction_is_clamped(self):
        self.assertEqual(fractional(0.2, fraction=5.0)["fraction"], 1.0)
        self.assertEqual(fractional(0.2, fraction=-1)["fraction"], 0.0)

    def test_illegal_params_fall_back_to_defaults(self):
        a = fractional(0.4, fraction=None, max_weight="x", min_weight=float("nan"))
        b = fractional(0.4)
        self.assertEqual(a, b)

    def test_max_weight_zero_forces_flat(self):
        r = fractional(0.4, max_weight=0.0)
        self.assertEqual(r["weight"], 0.0)
        self.assertTrue(r["zeroed"])

    def test_conflicting_bounds_zero_everything(self):
        r = fractional(0.5, max_weight=0.01, min_weight=0.02)
        self.assertEqual(r["weight"], 0.0)
        self.assertTrue(r["capped"])
        self.assertTrue(r["zeroed"])
        self.assertIn("参数矛盾", r["reason"])

    def test_weight_never_exceeds_max(self):
        for f in (0.01, 0.1, 0.3, 0.6, 1.0, 5.0):
            with self.subTest(f=f):
                self.assertLessEqual(fractional(f)["weight"], 0.25 + 1e-12)

    def test_note_mentions_scope(self):
        self.assertIn("不做组合归一化", fractional(0.4)["note"])


# --------------------------------------------------------------------------- #
# 四、组合分配：基础行为
# --------------------------------------------------------------------------- #
class TestAllocateBasics(unittest.TestCase):
    """归一化、现金缓冲、整手取整与汇总口径。"""

    def setUp(self):
        # 4 只等权候选：凯利 0.2 → 半凯利 0.1，合计 0.4 → 归一化 ×2.25 → 22.5%
        self.cands = [cand(c) for c in ("600519", "300750", "601318", "000001")]
        self.r = allocate(self.cands, 100000)

    def test_key_sets(self):
        self.assertEqual(set(self.r.keys()), ALLOC_KEYS)
        self.assertEqual(set(self.r["positions"][0].keys()), ROW_KEYS)

    def test_normalisation_to_capacity(self):
        """分数凯利权重合计 40%，归一化到 90% 计划仓位，每只 22.5%。"""
        self.assertAlmostEqual(self.r["capacity"], 0.9, places=12)
        self.assertAlmostEqual(self.r["budget"], 90000.0, places=6)
        for row in self.r["positions"]:
            self.assertAlmostEqual(row["kelly_weight"], 0.1, places=10)
            self.assertAlmostEqual(row["weight"], 0.225, places=10)
            self.assertAlmostEqual(row["amount"], 22500.0, places=6)

    def test_shares_and_lot(self):
        """A 股 100 股一手：22500 / 9 = 2500 股（25 手）。"""
        for row in self.r["positions"]:
            self.assertEqual(row["lot"], LOTS["cn"])
            self.assertEqual(row["shares"], 2500)
            self.assertEqual(row["shares"] % 100, 0)
            self.assertAlmostEqual(row["actual"], 22500.0, places=6)

    def test_cash_buffer_is_kept(self):
        self.assertEqual(self.r["cash_buffer"], DEFAULT_CASH_BUFFER)
        self.assertEqual(self.r["fraction"], DEFAULT_FRACTION)
        self.assertEqual(self.r["max_weight"], DEFAULT_MAX_WEIGHT)
        self.assertEqual(self.r["min_weight"], DEFAULT_MIN_WEIGHT)
        self.assertAlmostEqual(self.r["cash"], 10000.0, places=6)
        self.assertAlmostEqual(self.r["cash_weight"], 0.1, places=10)
        self.assertAlmostEqual(self.r["target_cash"], 10000.0, places=6)
        self.assertAlmostEqual(self.r["invested_weight"], 0.9, places=10)
        self.assertFalse(self.r["reduced"])

    def test_capital_conservation(self):
        self.assertAlmostEqual(self.r["invested"] + self.r["cash"], 100000.0, places=6)
        total = 0.0
        for row in self.r["positions"]:
            total += row["actual"]
        self.assertAlmostEqual(total, self.r["invested"], places=6)

    def test_lists_are_consistent(self):
        self.assertEqual(len(self.r["positions"]), len(self.cands))
        self.assertEqual(len(self.r["orders"]) + len(self.r["skipped"]),
                         len(self.r["positions"]))
        self.assertEqual(self.r["count"], len(self.r["orders"]))
        for row in self.r["orders"]:
            self.assertEqual(row["action"], "buy")
            self.assertGreater(row["shares"], 0)
        for row in self.r["skipped"]:
            self.assertEqual(row["action"], "skip")

    def test_weight_never_exceeds_max_weight(self):
        for row in self.r["positions"]:
            self.assertLessEqual(row["weight"], 0.25 + 1e-12)

    def test_sorted_by_weight_desc(self):
        ws = [row["weight"] for row in self.r["positions"]]
        self.assertEqual(ws, sorted(ws, reverse=True))

    def test_us_lot_is_one_share(self):
        r = allocate([cand("AAPL", price=180.0, market="us"),
                      cand("MSFT", price=180.0, market="us"),
                      cand("NVDA", price=180.0, market="us"),
                      cand("AMZN", price=180.0, market="us")], 100000)
        for row in r["positions"]:
            self.assertEqual(row["lot"], 1)
            self.assertEqual(row["shares"], 125)   # 22500 / 180
        self.assertAlmostEqual(r["invested"], 90000.0, places=6)

    def test_note_explains_scope(self):
        note = self.r["note"]
        self.assertIn("现金缓冲", note)
        self.assertIn("就近取整", note)
        self.assertIn("实际投入 90000.00", note)


# --------------------------------------------------------------------------- #
# 五、组合分配：边界
# --------------------------------------------------------------------------- #
class TestAllocateEdges(unittest.TestCase):
    """无优势 / 样本不足 / 权重全零 / 超上限 / 不足一手 / 缩减 / 脏数据。"""

    def test_all_no_edge_is_flat(self):
        r = allocate([{"code": "A", "win_rate": 0.5, "payoff": 1.0, "price": 10},
                      {"code": "B", "win_rate": 0.4, "payoff": 1.0, "price": 10},
                      {"code": "C", "win_rate": 0.2, "payoff": 1.0, "price": 10}], 100000)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["orders"], [])
        self.assertEqual(r["invested"], 0.0)
        self.assertAlmostEqual(r["cash"], 100000.0, places=6)
        self.assertAlmostEqual(r["cash_weight"], 1.0, places=10)
        self.assertIn("空仓", r["note"])
        for row in r["skipped"]:
            self.assertEqual(row["weight"], 0.0)
            self.assertIn("无优势", row["reason"])

    def test_all_tiny_weights_zeroed_by_min_weight(self):
        """每只分数凯利 1.5% < 下限 2% → 全部归零，组合空仓。"""
        r = allocate([{"code": "A", "kelly": 0.03, "price": 9},
                      {"code": "B", "kelly": 0.03, "price": 9},
                      {"code": "C", "kelly": 0.03, "price": 9}], 100000)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["invested"], 0.0)
        for row in r["skipped"]:
            self.assertIn("下限", row["reason"])

    def test_insufficient_samples(self):
        r = allocate([cand("X", samples=10), cand("Y", samples=100)], 100000)
        by_code = {row["code"]: row for row in r["positions"]}
        self.assertEqual(by_code["X"]["shares"], 0)
        self.assertIn("样本不足", by_code["X"]["reason"])
        self.assertIn(str(MIN_SAMPLES), by_code["X"]["reason"])
        # 样本不足的候选不参与归一化：Y 独享计划仓位并被单标的上限截断
        self.assertAlmostEqual(by_code["Y"]["weight"], 0.25, places=10)
        self.assertTrue(by_code["Y"]["capped"])
        self.assertEqual(r["count"], 1)

    def test_min_samples_can_be_relaxed(self):
        r = allocate([cand("X", samples=10)], 100000, min_samples=5)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["orders"][0]["samples"], 10)

    def test_min_samples_ignored_without_samples_field(self):
        r = alloc(cand("X"))
        self.assertEqual(r["count"], 1)
        self.assertIsNone(r["positions"][0]["samples"])

    def test_single_name_hits_max_weight_and_leaves_cash(self):
        """单只归一化后 90% 会被上限截断到 25%，剩余额度留作现金。"""
        r = alloc({"code": "X", "kelly": 0.5, "price": 50}, 100000)
        row = r["positions"][0]
        self.assertAlmostEqual(row["kelly_weight"], 0.25, places=10)
        self.assertAlmostEqual(row["weight"], 0.25, places=10)
        self.assertTrue(row["capped"])
        self.assertEqual(row["shares"], 500)          # 25000 / 50
        self.assertAlmostEqual(r["cash"], 75000.0, places=6)
        self.assertAlmostEqual(r["cash_weight"], 0.75, places=10)
        self.assertIn("不再二次分配", r["note"])

    def test_cash_buffer_fully_blocks_allocation(self):
        r = alloc(cand("X"), 100000, cash_buffer=1.0)
        self.assertEqual(r["capacity"], 0.0)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["invested"], 0.0)
        self.assertIn("现金缓冲为 100%", r["note"])

    def test_zero_cash_buffer(self):
        r = alloc({"code": "X", "kelly": 0.5, "price": 50}, 100000, cash_buffer=0.0)
        self.assertAlmostEqual(r["capacity"], 1.0, places=12)
        self.assertAlmostEqual(r["positions"][0]["weight"], 0.25, places=10)

    def test_rounding_up_then_shrink_proportionally(self):
        """就近取整后超出计划投入资金 → 按比例缩减并向下取整到整手。"""
        cands = [{"code": "A", "kelly": 0.2, "price": 7},
                 {"code": "B", "kelly": 0.2, "price": 7},
                 {"code": "C", "kelly": 0.2, "price": 7}]
        r = allocate(cands, 100000, cash_buffer=0.25)
        self.assertAlmostEqual(r["budget"], 75000.0, places=6)
        self.assertTrue(r["reduced"])
        self.assertLess(r["shrink"], 1.0)
        self.assertAlmostEqual(r["shrink"], 75000.0 / 75600.0, places=9)
        for row in r["orders"]:
            self.assertAlmostEqual(row["weight"], 0.25, places=10)
            self.assertEqual(row["shares"], 3500)          # 3600 → 3500
            self.assertAlmostEqual(row["actual"], 24500.0, places=6)
        self.assertLessEqual(r["invested"], r["budget"] + 1e-9)
        self.assertIn("资金不足", r["note"])
        self.assertIn("等比缩减", r["note"])

    def test_target_amount_below_one_lot_is_skipped(self):
        """资金太小：目标金额 250 元买不起一手（5000 元）→ 标注不足一手。"""
        r = alloc({"code": "X", "kelly": 0.5, "price": 50}, 1000)
        row = r["positions"][0]
        self.assertAlmostEqual(row["weight"], 0.25, places=10)
        self.assertEqual(row["shares"], 0)
        self.assertEqual(row["action"], "skip")
        self.assertIn("不足一手", row["reason"])
        self.assertEqual(r["count"], 0)
        self.assertAlmostEqual(r["cash"], 1000.0, places=6)
        self.assertIn("建议空仓", r["note"])

    def test_no_valid_price_is_skipped(self):
        for price in (None, 0, -1, "abc"):
            with self.subTest(price=price):
                r = alloc({"code": "X", "kelly": 0.5, "price": price}, 100000)
                row = r["positions"][0]
                self.assertEqual(row["shares"], 0)
                self.assertIn("无有效价格", row["reason"])
                self.assertEqual(r["invested"], 0.0)

    def test_invalid_capital(self):
        for capital in (0, -1, None, "abc", float("nan"), float("inf")):
            with self.subTest(capital=capital):
                r = alloc(cand("X"), capital)
                self.assertFalse(r["valid_capital"])
                self.assertEqual(r["count"], 0)
                self.assertEqual(r["invested"], 0.0)
                self.assertEqual(r["capital"], 0.0)
                self.assertIn("总资金非法", r["note"])
                self.assertEqual(r["positions"][0]["shares"], 0)

    def test_string_capital_tolerated(self):
        r = alloc(cand("X"), "100000")
        self.assertTrue(r["valid_capital"])
        self.assertEqual(r["count"], 1)

    def test_dirty_candidates_do_not_raise(self):
        r = allocate([None, "x", 123, {}, {"code": "Z", "kelly": 0.5, "price": 9}],
                     100000)
        self.assertEqual(len(r["positions"]), 5)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["orders"][0]["code"], "Z")
        by_code = {row["code"]: row for row in r["positions"]}
        self.assertIn("非法", by_code["#1"]["reason"])       # None 候选
        self.assertIn("非法", by_code["#2"]["reason"])       # 字符串候选
        self.assertIn("非法", by_code["#3"]["reason"])       # 数字候选
        self.assertIn("缺少", by_code["#4"]["reason"])       # 空字典候选

    def test_non_sequence_candidates(self):
        for bad in (None, [], "x", 123, {"code": "X"}):
            with self.subTest(bad=bad):
                r = allocate(bad, 100000)
                self.assertEqual(r["positions"], [])
                self.assertEqual(r["count"], 0)
                self.assertEqual(r["invested"], 0.0)
                self.assertIn("空仓", r["note"])

    def test_explicit_kelly_field(self):
        r = alloc({"code": "AAPL", "market": "us", "price": 180, "kelly": 0.4}, 100000)
        row = r["positions"][0]
        self.assertEqual(row["kelly_kind"], "explicit")
        self.assertAlmostEqual(row["kelly"], 0.4, places=12)
        self.assertAlmostEqual(row["kelly_weight"], 0.2, places=10)
        self.assertEqual(row["shares"], 139)              # 25000 / 180 ≈ 138.9 → 139
        self.assertTrue(row["capped"])

    def test_continuous_kelly_field(self):
        cands = [{"code": "X%d" % i, "mean_ret": 0.01, "var_ret": 0.04, "price": 9}
                 for i in range(4)]
        r = allocate(cands, 100000)
        for row in r["positions"]:
            self.assertEqual(row["kelly_kind"], "continuous")
            self.assertAlmostEqual(row["kelly"], 0.25, places=12)
            self.assertAlmostEqual(row["kelly_weight"], 0.125, places=10)
            self.assertAlmostEqual(row["weight"], 0.225, places=10)
        self.assertAlmostEqual(r["invested"], 90000.0, places=6)

    def test_negative_explicit_kelly_is_flat(self):
        r = alloc({"code": "X", "kelly": -0.5, "price": 9})
        self.assertEqual(r["count"], 0)
        self.assertIn("无优势", r["positions"][0]["reason"])

    def test_unparsable_explicit_kelly(self):
        r = alloc({"code": "X", "kelly": "abc", "price": 9})
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["positions"][0]["kelly_kind"], "invalid")
        self.assertIn("无法解析", r["positions"][0]["reason"])

    def test_normalisation_scales_down_when_over_capacity(self):
        """5 只各 25% 合计 125% > 90% → 等比缩减到 18%。"""
        cands = [{"code": "S%d" % i, "win_rate": 0.7, "payoff": 2.0, "price": 9}
                 for i in range(5)]
        r = allocate(cands, 100000)
        for row in r["positions"]:
            self.assertAlmostEqual(row["kelly_weight"], 0.25, places=10)
            self.assertAlmostEqual(row["weight"], 0.18, places=10)
            self.assertEqual(row["shares"], 2000)         # 18000 / 9
        total = 0.0
        for row in r["positions"]:
            total += row["weight"]
        self.assertAlmostEqual(total, 0.9, places=10)
        self.assertAlmostEqual(r["invested"], 90000.0, places=6)

    def test_partial_allocation_is_sorted_by_weight(self):
        cands = [{"code": "W1", "kelly": 0.2, "price": 9},
                 {"code": "W2", "kelly": 0.2, "price": 9},
                 {"code": "S1", "kelly": 0.5, "price": 9},
                 {"code": "S2", "kelly": 0.5, "price": 9}]
        r = allocate(cands, 100000)
        self.assertEqual([row["code"] for row in r["positions"]],
                         ["S1", "S2", "W1", "W2"])
        self.assertGreater(r["positions"][0]["weight"], r["positions"][-1]["weight"])
        self.assertAlmostEqual(r["positions"][0]["weight"], 0.25, places=10)
        self.assertAlmostEqual(r["positions"][-1]["weight"], 0.128571, places=6)

    def test_custom_lot(self):
        r = alloc({"code": "X", "kelly": 0.5, "price": 50, "lot": 10}, 100000)
        self.assertEqual(r["positions"][0]["lot"], 10)
        self.assertEqual(r["positions"][0]["shares"], 500)
        self.assertEqual(r["positions"][0]["shares"] % 10, 0)

    def test_row_reason_and_note_are_filled(self):
        r = alloc(cand("X"), 100000)
        row = r["orders"][0]
        self.assertTrue(row["reason"])
        self.assertTrue(row["note"])
        self.assertIn("股数", row["note"])
        self.assertIn("现金缓冲", row["note"])

    def test_zero_share_rows_keep_target_weight_for_display(self):
        """被跳过的标的仍保留目标权重，便于前端解释「本来要买多少」。"""
        r = alloc({"code": "X", "kelly": 0.5, "price": 5000}, 100000)
        row = r["positions"][0]
        self.assertGreater(row["weight"], 0.0)
        self.assertGreater(row["amount"], 0.0)
        self.assertEqual(row["shares"], 0)
        self.assertEqual(row["actual"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
