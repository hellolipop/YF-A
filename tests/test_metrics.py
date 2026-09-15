# -*- coding: utf-8 -*-
"""core.metrics 的单元测试（仅用标准库 unittest，可直接 python 运行）。

覆盖边界：空数据 / 单点 / 全亏损 / 零方差 / 含手续费与滑点 / 脏数据 /
排序 / 月度聚合 / 基准 alpha-beta / 回撤持续期 / 两种收益口径。

运行方式：
    python -m unittest discover -s tests -t .
    python tests/test_metrics.py
"""

import math
import os
import sys
import unittest
from datetime import date, timedelta

# 让 tests/ 目录之外的包（core）可被导入，兼容任意工作目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.metrics import summarize  # noqa: E402

#: summarize 的输出字段（顺序无关，用于锁定对外接口）
EXPECTED_KEYS = {
    "total_return", "annualized_return", "annualized_vol", "sharpe", "sortino",
    "calmar", "max_drawdown", "max_drawdown_days", "win_rate", "profit_factor",
    "avg_win", "avg_loss", "expectancy", "trades", "avg_hold", "cost_total",
    "fee_total", "slippage_total", "exposure", "alpha", "beta", "var95",
    "best_day", "worst_day", "monthly",
}

BEGIN = date(2024, 1, 1)


def curve(rets, start=100000.0, begin=BEGIN, positions=None):
    """由单期收益序列构造权益曲线（首点为 start）。

    positions 为可选的布尔列表，长度需与 rets 一致（首点固定为 False）。
    """
    pts = [{"t": begin.isoformat(), "v": start, "close": 10.0, "position": False}]
    v, d = start, begin
    for i, r in enumerate(rets):
        d = d + timedelta(days=1)
        v = v * (1.0 + r)
        pos = bool(positions[i]) if positions else None
        item = {"t": d.isoformat(), "v": v, "close": 10.0}
        if pos is not None:
            item["position"] = pos
        pts.append(item)
    return pts


def values_curve(vals, begin=BEGIN):
    """由净值序列构造权益曲线。"""
    pts = []
    for i, v in enumerate(vals):
        pts.append({"t": (begin + timedelta(days=i)).isoformat(), "v": float(v),
                    "close": 10.0})
    return pts


class TestInterface(unittest.TestCase):
    """对外字段与基础契约。"""

    def test_key_set(self):
        r = summarize([], [], 100000)
        self.assertEqual(set(r.keys()), EXPECTED_KEYS)

    def test_monthly_item_keys(self):
        r = summarize(curve([0.01, -0.01]), [], 100000)
        self.assertEqual(len(r["monthly"]), 1)
        self.assertEqual(set(r["monthly"][0].keys()),
                         {"month", "return", "realized", "trades", "wins"})

    def test_no_exception_on_garbage(self):
        """任何脏输入都不应抛异常。"""
        cases = [
            (None, None, 0),
            ([], None, None),
            ([], [], -1),
            ("not-a-list", "not-a-list", 100000),
            ([None, "x", {}, {"v": None}, {"t": "2024-01-02"}], [None, {}, "x"], 100000),
            ([{"v": float("inf")}, {"v": float("-inf")}], [], 100000),
        ]
        for eq, tr, init in cases:
            with self.subTest(eq=eq, tr=tr, init=init):
                r = summarize(eq, tr, init)
                self.assertEqual(set(r.keys()), EXPECTED_KEYS)
                for k in ("total_return", "sharpe", "max_drawdown", "var95"):
                    self.assertEqual(r[k], 0.0)
                self.assertEqual(r["trades"], 0)
                self.assertEqual(r["monthly"], [])


class TestEmpty(unittest.TestCase):
    """空数据：全部指标归零，不产生 NaN。"""

    def test_empty_equity(self):
        r = summarize([], [], 100000)
        self.assertEqual(r["total_return"], 0.0)
        self.assertEqual(r["annualized_return"], 0.0)
        self.assertEqual(r["annualized_vol"], 0.0)
        self.assertEqual(r["sharpe"], 0.0)
        self.assertEqual(r["sortino"], 0.0)
        self.assertEqual(r["calmar"], 0.0)
        self.assertEqual(r["max_drawdown"], 0.0)
        self.assertEqual(r["max_drawdown_days"], 0)
        self.assertEqual(r["win_rate"], 0.0)
        self.assertEqual(r["profit_factor"], 0.0)
        self.assertEqual(r["expectancy"], 0.0)
        self.assertEqual(r["avg_hold"], 0.0)
        self.assertEqual(r["cost_total"], 0.0)
        self.assertEqual(r["exposure"], 0.0)
        self.assertEqual(r["alpha"], 0.0)
        self.assertEqual(r["beta"], 0.0)
        self.assertEqual(r["var95"], 0.0)
        self.assertEqual(r["best_day"], 0.0)
        self.assertEqual(r["worst_day"], 0.0)
        self.assertEqual(r["monthly"], [])

    def test_all_points_invalid(self):
        eq = [{"t": "2024-01-02", "v": None}, {"t": "2024-01-03", "v": "abc"},
              {"t": "2024-01-04", "v": float("nan")}, {"t": "2024-01-05", "v": True}]
        r = summarize(eq, [], 100000)
        self.assertEqual(r["total_return"], 0.0)
        self.assertEqual(r["monthly"], [])

    def test_only_equity_no_trades(self):
        r = summarize(curve([0.01, 0.02]), [], 100000)
        self.assertEqual(r["trades"], 0.0)
        self.assertEqual(r["win_rate"], 0.0)
        self.assertAlmostEqual(r["total_return"], 1.01 * 1.02 - 1, places=12)

    def test_only_trades_no_equity(self):
        """没有权益曲线时，成交统计依然可用。"""
        trades = [{"pnl": 100.0, "outDate": "2024-01-05"},
                  {"pnl": 50.0, "outDate": "2024-01-06"}]
        r = summarize([], trades, 100000)
        self.assertEqual(r["trades"], 2)
        self.assertEqual(r["win_rate"], 1.0)
        self.assertTrue(math.isinf(r["profit_factor"]))
        self.assertEqual(r["monthly"][0]["month"], "2024-01")
        self.assertEqual(r["monthly"][0]["realized"], 150.0)
        self.assertEqual(r["monthly"][0]["return"], 0.0)


class TestSinglePoint(unittest.TestCase):
    """单点：无收益样本，但首点相对初始资金的涨跌仍应计入。"""

    def test_single_point_with_gain(self):
        eq = [{"t": "2024-01-02", "v": 110000.0, "close": 11.0}]
        r = summarize(eq, [], 100000)
        self.assertAlmostEqual(r["total_return"], 0.1, places=12)
        self.assertEqual(r["annualized_vol"], 0.0)
        self.assertEqual(r["sharpe"], 0.0)
        self.assertEqual(r["sortino"], 0.0)
        self.assertEqual(r["calmar"], 0.0)
        self.assertEqual(r["max_drawdown"], 0.0)
        self.assertEqual(r["max_drawdown_days"], 0)
        self.assertAlmostEqual(r["best_day"], 0.1, places=12)
        self.assertAlmostEqual(r["worst_day"], 0.1, places=12)
        self.assertTrue(r["annualized_return"] > 0)
        self.assertTrue(math.isfinite(r["annualized_return"]))
        self.assertEqual(len(r["monthly"]), 1)
        self.assertEqual(r["monthly"][0]["month"], "2024-01")
        self.assertAlmostEqual(r["monthly"][0]["return"], 0.1, places=12)

    def test_single_point_equal_to_initial(self):
        eq = [{"t": "2024-03-11", "v": 100000.0}]
        r = summarize(eq, [], 100000)
        self.assertEqual(r["total_return"], 0.0)
        self.assertEqual(r["annualized_return"], 0.0)
        self.assertEqual(r["best_day"], 0.0)
        self.assertEqual(len(r["monthly"]), 1)
        self.assertEqual(r["monthly"][0]["month"], "2024-03")
        self.assertEqual(r["monthly"][0]["return"], 0.0)

    def test_single_point_loss(self):
        eq = [{"t": "2024-01-02", "v": 90000.0}]
        r = summarize(eq, [], 100000)
        self.assertAlmostEqual(r["total_return"], -0.1, places=12)
        self.assertEqual(r["max_drawdown"], 0.0)   # 单点无法构成回撤
        self.assertLess(r["annualized_return"], 0)


class TestAllLosses(unittest.TestCase):
    """全亏损：胜率为 0、盈亏比为 0、卡玛为负。"""

    def setUp(self):
        self.eq = curve([-0.02, -0.01, -0.015, -0.005], 100000)
        self.trades = [
            {"pnl": -500.0, "pnlPct": -5.0, "bars": 3, "inDate": "2024-01-02",
             "outDate": "2024-01-05", "fee": 5.0, "slippage": 20.0},
            {"pnl": -300.0, "pnlPct": -3.0, "bars": 2, "inDate": "2024-01-03",
             "outDate": "2024-01-04", "fee": 3.0, "slippage": 12.0},
            {"pnl": -200.0, "pnlPct": -2.0, "bars": 4, "inDate": "2024-01-02",
             "outDate": "2024-01-06", "fee": 2.0, "slippage": 8.0},
        ]
        self.r = summarize(self.eq, self.trades, 100000)

    def test_trade_stats(self):
        r = self.r
        self.assertEqual(r["trades"], 3)
        self.assertEqual(r["win_rate"], 0.0)
        self.assertEqual(r["profit_factor"], 0.0)
        self.assertEqual(r["avg_win"], 0.0)
        self.assertAlmostEqual(r["avg_loss"], -1000.0 / 3, places=6)
        self.assertAlmostEqual(r["expectancy"], -1000.0 / 3, places=6)
        self.assertAlmostEqual(r["avg_hold"], 3.0, places=12)
        self.assertLess(r["avg_loss"], 0)

    def test_return_and_risk(self):
        r = self.r
        self.assertLess(r["total_return"], 0)
        self.assertLess(r["annualized_return"], 0)
        self.assertGreater(r["annualized_vol"], 0)
        self.assertLess(r["sharpe"], 0)
        self.assertLess(r["sortino"], 0)
        self.assertGreater(r["max_drawdown"], 0)
        self.assertLess(r["calmar"], 0)          # 负年化 / 正回撤
        self.assertLess(r["worst_day"], 0)
        self.assertLess(r["var95"], 0)

    def test_monthly_realized(self):
        m = self.r["monthly"]
        self.assertEqual([x["month"] for x in m], ["2024-01"])
        self.assertEqual(m[0]["realized"], -1000.0)
        self.assertEqual(m[0]["trades"], 3)
        self.assertEqual(m[0]["wins"], 0)
        self.assertLess(m[0]["return"], 0)

    def test_all_wins_profit_factor_is_inf(self):
        r = summarize(curve([0.01, 0.02]), [{"pnl": 10.0}, {"pnl": 20.0}], 100000)
        self.assertTrue(math.isinf(r["profit_factor"]))
        self.assertGreater(r["profit_factor"], 0)
        self.assertEqual(r["win_rate"], 1.0)
        self.assertEqual(r["avg_loss"], 0.0)


class TestZeroVariance(unittest.TestCase):
    """零方差：波动与风险调整指标为 0，不出现 inf / NaN。"""

    def setUp(self):
        # 每期精确 +50%（1024 × 1.5**i 在二进制下精确可表示，收益严格相等）
        self.vals = [1024.0 * (1.5 ** i) for i in range(6)]
        self.r = summarize(values_curve(self.vals), [], 1024)

    def test_flat_volatility(self):
        r = self.r
        self.assertEqual(r["annualized_vol"], 0.0)
        self.assertEqual(r["sharpe"], 0.0)
        self.assertEqual(r["sortino"], 0.0)
        self.assertEqual(r["calmar"], 0.0)
        self.assertEqual(r["max_drawdown"], 0.0)
        self.assertEqual(r["max_drawdown_days"], 0)
        self.assertTrue(math.isfinite(r["annualized_return"]))
        self.assertGreater(r["annualized_return"], 0)

    def test_flat_returns(self):
        r = self.r
        self.assertAlmostEqual(r["best_day"], 0.5, places=12)
        self.assertAlmostEqual(r["worst_day"], 0.5, places=12)
        self.assertAlmostEqual(r["var95"], 0.5, places=12)
        self.assertAlmostEqual(r["total_return"], self.vals[-1] / 1024.0 - 1, places=12)

    def test_zero_variance_bench_has_zero_beta(self):
        flat = values_curve([100.0] * 6)
        r = summarize(values_curve(self.vals), [], 1024, bench=flat)
        self.assertEqual(r["beta"], 0.0)


class TestDrawdown(unittest.TestCase):
    """回撤幅度与最长回撤持续期（含无日期退化为期数）。"""

    def test_drawdown_with_dates(self):
        eq = [
            {"t": "2024-01-01", "v": 100.0},
            {"t": "2024-01-02", "v": 120.0},   # 前高
            {"t": "2024-01-03", "v": 90.0},    # 谷底：回撤 25%
            {"t": "2024-01-05", "v": 100.0},
            {"t": "2024-01-08", "v": 110.0},
            {"t": "2024-01-11", "v": 120.0},   # 回到前高，回撤结束（9 天）
            {"t": "2024-01-12", "v": 130.0},
        ]
        r = summarize(eq, [], 100)
        self.assertAlmostEqual(r["max_drawdown"], 0.25, places=12)
        self.assertEqual(r["max_drawdown_days"], 9)      # 01-02 → 01-11
        self.assertAlmostEqual(r["total_return"], 0.3, places=12)

    def test_drawdown_without_dates_counts_periods(self):
        r = summarize([100, 120, 90, 100, 110, 120, 130], [], 100)
        self.assertAlmostEqual(r["max_drawdown"], 0.25, places=12)
        self.assertEqual(r["max_drawdown_days"], 4)      # 索引 1 → 5，共 4 期
        self.assertEqual(r["monthly"], [])               # 无日期则无法按月归集

    def test_unrecovered_drawdown_runs_to_end(self):
        eq = values_curve([100.0, 120.0, 90.0, 80.0])
        r = summarize(eq, [], 100)
        self.assertAlmostEqual(r["max_drawdown"], (120.0 - 80.0) / 120.0, places=12)
        self.assertEqual(r["max_drawdown_days"], 2)      # 01-02 → 01-04


class TestCosts(unittest.TestCase):
    """手续费与滑点：金额汇总、平均值、缺省为 0。"""

    def test_fee_and_slippage_totals(self):
        trades = [
            {"pnl": 300.0, "pnlPct": 3.0, "bars": 2, "inDate": "2024-01-02",
             "outDate": "2024-01-03", "fee": 3.0, "slippage": 10.0},
            {"pnl": -100.0, "pnlPct": -1.0, "bars": 4, "inDate": "2024-01-04",
             "outDate": "2024-01-08", "fee": 2.0, "slippage": 5.0},
        ]
        r = summarize(curve([0.001, -0.002, 0.003, -0.001, 0.002]), trades, 100000)
        self.assertEqual(r["fee_total"], 5.0)
        self.assertEqual(r["slippage_total"], 15.0)
        self.assertEqual(r["cost_total"], 20.0)
        self.assertAlmostEqual(r["avg_hold"], 3.0, places=12)
        self.assertEqual(r["win_rate"], 0.5)
        self.assertAlmostEqual(r["profit_factor"], 3.0, places=12)
        self.assertAlmostEqual(r["avg_win"], 300.0, places=12)
        self.assertAlmostEqual(r["avg_loss"], -100.0, places=12)
        self.assertAlmostEqual(r["expectancy"], 100.0, places=12)
        self.assertAlmostEqual(r["monthly"][0]["realized"], 200.0, places=12)

    def test_missing_cost_fields_default_zero(self):
        """engine 产出的成交没有 fee / slippage 字段，应计为 0 而不是报错。"""
        trades = [{"pnl": 10.0, "pnlPct": 1.0, "bars": 3,
                   "inDate": "2024-01-02", "outDate": "2024-01-05"}]
        r = summarize(curve([0.01]), trades, 100000)
        self.assertEqual(r["fee_total"], 0.0)
        self.assertEqual(r["slippage_total"], 0.0)
        self.assertEqual(r["cost_total"], 0.0)
        self.assertEqual(r["avg_hold"], 3.0)

    def test_avg_hold_falls_back_to_dates(self):
        trades = [{"pnl": 10.0, "inDate": "2024-01-02", "outDate": "2024-01-05"},
                  {"pnl": -5.0, "inDate": "2024-01-03", "outDate": "2024-01-04"}]
        r = summarize(curve([0.01]), trades, 100000)
        self.assertAlmostEqual(r["avg_hold"], 2.0, places=12)   # (3 + 1) / 2

    def test_invalid_trades_ignored(self):
        trades = [None, {}, {"pnl": None}, {"pnl": "abc"}, "x",
                  {"pnl": 100.0, "outDate": "2024-01-05"}]
        r = summarize(curve([0.01]), trades, 100000)
        self.assertEqual(r["trades"], 1)
        self.assertEqual(r["expectancy"], 100.0)


class TestExposure(unittest.TestCase):
    """持仓暴露：按 position 字段统计；无信息时返回 0。"""

    def test_exposure_ratio(self):
        eq = [
            {"t": "2024-01-01", "v": 100000.0, "position": True},
            {"t": "2024-01-02", "v": 101000.0, "position": True},
            {"t": "2024-01-03", "v": 101000.0, "position": False},
            {"t": "2024-01-04", "v": 100000.0, "position": False},
        ]
        r = summarize(eq, [], 100000)
        self.assertAlmostEqual(r["exposure"], 0.5, places=12)

    def test_exposure_accepts_loose_values(self):
        eq = [
            {"t": "2024-01-01", "v": 100.0, "position": 1},
            {"t": "2024-01-02", "v": 101.0, "position": 0},
            {"t": "2024-01-03", "v": 102.0, "position": "true"},
            {"t": "2024-01-04", "v": 103.0, "position": "false"},
        ]
        r = summarize(eq, [], 100)
        self.assertAlmostEqual(r["exposure"], 0.5, places=12)

    def test_no_position_info(self):
        r = summarize(curve([0.01, 0.02]), [], 100000)
        self.assertEqual(r["exposure"], 0.0)


class TestMode(unittest.TestCase):
    """simple（算术累加）与 compound（几何累乘）。"""

    def setUp(self):
        self.eq = [
            {"t": "2024-01-02", "v": 100000.0},
            {"t": "2024-01-31", "v": 110000.0},
            {"t": "2024-02-01", "v": 110000.0},
            {"t": "2024-02-29", "v": 121000.0},
        ]
        self.trades = [
            {"pnl": 5000.0, "outDate": "2024-01-20", "bars": 5},
            {"pnl": -1000.0, "outDate": "2024-02-10", "bars": 3},
        ]

    def test_compound_is_geometric(self):
        r = summarize(self.eq, self.trades, 100000, mode="compound")
        self.assertAlmostEqual(r["total_return"], 0.21, places=12)
        # 年化 = 1.21 ** (244 / 3) - 1
        self.assertAlmostEqual(r["annualized_return"],
                               (1.21 ** (244.0 / 3)) - 1, places=6)

    def test_simple_is_arithmetic(self):
        r = summarize(self.eq, self.trades, 100000, mode="simple")
        self.assertAlmostEqual(r["total_return"], 0.2, places=12)   # 0.1 + 0 + 0.1
        self.assertAlmostEqual(r["annualized_return"], 0.2 / (3.0 / 244), places=6)

    def test_mode_is_case_and_whitespace_insensitive(self):
        a = summarize(self.eq, [], 100000, mode="SIMPLE")
        b = summarize(self.eq, [], 100000, mode=" simple ")
        c = summarize(self.eq, [], 100000, mode="simple")
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_unknown_mode_falls_back_to_compound(self):
        a = summarize(self.eq, [], 100000, mode="bogus")
        b = summarize(self.eq, [], 100000, mode="compound")
        self.assertEqual(a, b)
        self.assertEqual(summarize(self.eq, [], 100000, mode=None)["total_return"],
                         b["total_return"])

    def test_periods_per_year_guard(self):
        a = summarize(self.eq, [], 100000, periods_per_year=0)
        b = summarize(self.eq, [], 100000, periods_per_year=244)
        c = summarize(self.eq, [], 100000, periods_per_year=None)
        self.assertEqual(a["annualized_return"], b["annualized_return"])
        self.assertEqual(c["annualized_return"], b["annualized_return"])

    def test_monthly_return_per_mode(self):
        mc = summarize(self.eq, self.trades, 100000, mode="compound")["monthly"]
        ms = summarize(self.eq, self.trades, 100000, mode="simple")["monthly"]
        self.assertEqual([m["month"] for m in mc], ["2024-01", "2024-02"])
        self.assertAlmostEqual(mc[0]["return"], 0.10, places=12)
        self.assertAlmostEqual(mc[1]["return"], 0.10, places=12)
        self.assertAlmostEqual(ms[1]["return"], 0.10, places=12)
        self.assertEqual(mc[0]["realized"], 5000.0)
        self.assertEqual(mc[1]["realized"], -1000.0)
        self.assertEqual(mc[0]["trades"], 1)
        self.assertEqual(mc[0]["wins"], 1)
        self.assertEqual(mc[1]["wins"], 0)


class TestBenchmark(unittest.TestCase):
    """基准对齐与 alpha / beta。"""

    def setUp(self):
        self.rb = [0.02, -0.01, 0.03, -0.02, 0.01, 0.04, -0.03, 0.02]
        self.ra = [0.5 * x for x in self.rb]        # 策略收益 = 0.5 × 基准收益
        self.eq = curve(self.ra, 100000.0)
        self.bench = curve(self.rb, 100.0)

    def test_beta_is_half(self):
        r = summarize(self.eq, [], 100000, bench=self.bench)
        self.assertAlmostEqual(r["beta"], 0.5, places=6)

    def test_simple_mode_alpha_is_zero_when_rf_zero(self):
        """算术口径下 策略 = 0.5 × 基准，Jensen alpha 应严格为 0。"""
        r = summarize(self.eq, [], 100000, mode="simple", bench=self.bench)
        self.assertAlmostEqual(r["alpha"], 0.0, places=9)

    def test_bench_as_return_series(self):
        bench = [{"t": "2024-01-%02d" % (i + 1), "ret": x}
                 for i, x in enumerate(self.rb)]
        r = summarize(self.eq, [], 100000, bench=bench)
        self.assertAlmostEqual(r["beta"], 0.5, places=6)

    def test_no_bench(self):
        r = summarize(self.eq, [], 100000)
        self.assertEqual(r["alpha"], 0.0)
        self.assertEqual(r["beta"], 0.0)

    def test_invalid_bench(self):
        for bench in (None, [], "x", [None, "y"], [{}]):
            with self.subTest(bench=bench):
                r = summarize(self.eq, [], 100000, bench=bench)
                self.assertEqual(r["beta"], 0.0)
                self.assertEqual(r["alpha"], 0.0)

    def test_rf_reduces_alpha_in_simple_mode(self):
        a = summarize(self.eq, [], 100000, mode="simple", rf=0.03,
                      bench=self.bench)
        b = summarize(self.eq, [], 100000, mode="simple", rf=0.0,
                      bench=self.bench)
        self.assertLess(a["alpha"], b["alpha"])

    def test_sharpe_penalised_by_rf(self):
        eq = curve([0.01, 0.02, -0.01, 0.03], 100000)
        a = summarize(eq, [], 100000)
        b = summarize(eq, [], 100000, rf=0.05)
        self.assertLess(b["sharpe"], a["sharpe"])


class TestDistribution(unittest.TestCase):
    """收益分布类指标：var95 / best_day / worst_day / 单调性。"""

    def setUp(self):
        rets = [-0.10, -0.05] + [0.01] * 19      # 共 21 期
        self.r = summarize(curve(rets, 100000), [], 100000)

    def test_var95_is_fifth_percentile(self):
        # pos = (21 - 1) * 0.05 = 1.0 → 取第二小的收益
        self.assertAlmostEqual(self.r["var95"], -0.05, places=12)

    def test_best_and_worst(self):
        self.assertAlmostEqual(self.r["best_day"], 0.01, places=12)
        self.assertAlmostEqual(self.r["worst_day"], -0.10, places=12)

    def test_ordering(self):
        r = self.r
        self.assertLessEqual(r["worst_day"], r["var95"])
        self.assertLessEqual(r["var95"], r["best_day"])
        self.assertGreater(r["annualized_vol"], 0)


class TestInputTolerance(unittest.TestCase):
    """脏数据、乱序、字符串数值等容错。"""

    def test_unsorted_dates_are_sorted(self):
        eq = [{"t": "2024-01-03", "v": 103.0},
              {"t": "2024-01-01", "v": 100.0},
              {"t": "2024-01-02", "v": 102.0}]
        r = summarize(eq, [], 100.0)
        self.assertAlmostEqual(r["total_return"], 0.03, places=12)
        self.assertEqual([m["month"] for m in r["monthly"]], ["2024-01"])

    def test_string_numbers_and_junk_points(self):
        eq = [{"t": "2024-01-01", "v": "100"},
              None,
              "x",
              {"t": "2024-01-02", "v": float("nan")},
              {"t": "2024-01-04", "v": 110}]
        r = summarize(eq, [], 100)
        self.assertAlmostEqual(r["total_return"], 0.1, places=12)

    def test_plain_number_list(self):
        r = summarize([100.0, 101.0, 102.0], [], 100.0)
        self.assertAlmostEqual(r["total_return"], 0.02, places=12)

    def test_close_used_when_v_missing(self):
        eq = [{"t": "2024-01-01", "close": 100.0},
              {"t": "2024-01-02", "close": 110.0}]
        r = summarize(eq, [], 100.0)
        self.assertAlmostEqual(r["total_return"], 0.1, places=12)

    def test_base_when_initial_missing(self):
        """未传 initial 时以曲线首点为基准，且不产生额外收益样本。"""
        eq = values_curve([100.0, 110.0, 121.0])
        r = summarize(eq, [], 0)
        self.assertAlmostEqual(r["total_return"], 0.21, places=12)
        self.assertAlmostEqual(r["best_day"], 0.1, places=12)

    def test_equity_dropping_to_zero_is_safe(self):
        eq = values_curve([100.0, 50.0, 0.0, 0.0])
        r = summarize(eq, [], 100)
        self.assertAlmostEqual(r["max_drawdown"], 1.0, places=12)
        self.assertTrue(math.isfinite(r["total_return"]))
        self.assertTrue(math.isfinite(r["annualized_return"]))

    def test_second_point_equal_to_first(self):
        eq = values_curve([100.0, 100.0, 100.0])
        r = summarize(eq, [], 100)
        self.assertEqual(r["total_return"], 0.0)
        self.assertEqual(r["annualized_vol"], 0.0)
        self.assertEqual(r["sharpe"], 0.0)
        self.assertEqual(r["max_drawdown"], 0.0)

    def test_negative_zero_normalised(self):
        r = summarize(curve([0.0, 0.0]), [], 100000)
        self.assertEqual(r["total_return"], 0.0)
        self.assertNotEqual(repr(r["total_return"]), "-0.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
