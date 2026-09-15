# -*- coding: utf-8 -*-
"""core.forecast 的单元测试（仅用标准库 unittest，可直接 python 运行）。

覆盖范围（与需求一一对应）：
  1. 对外字段契约 + 分位数单调 + 路径分位带单调 + 路径长度与锚点；
  2. 样本不足时的降级（降级链 level 1 / 2 / 3、置信度折扣、note 标注降级原因）；
  3. 无信息平稳序列：up_prob ≈ 50%（多个随机种子平均，避免单条序列的抽样噪声）；
  4. 上升趋势序列：up_prob > 55%；
  5. 条件状态筛选确实带来信息量（低位状态的条件 up_prob 高于无条件分布）；
  6. 脏数据 / 极端 horizon 不抛异常；note 必须写明「历史条件分布」而非收益承诺。

为什么「无信息平稳序列」用几何随机游走（对数收益 iid 且关于 0 对称）：
  这类序列的未来与历史状态相互独立，收益分布关于 0 对称，理论上中位收益 = 0、
  up_prob = 50%，是检验「统计口径无系统性偏移」的标准基准。
  若改用「价格围绕常数波动的均值回复序列」，低位状态本身携带真实的均值回复信号，
  up_prob 会显著偏离 50%（见 TestStateFilterAddsInformation），不适合做无信息基准。
  另外，最近邻样本的前视窗口之间会有重叠（自相关），单条序列的 up_prob 抽样噪声较大，
  所以基准检验取多个随机种子求平均。

运行方式（tests/ 下无 __init__.py，直接跑文件最稳）：
    python tests/test_forecast.py
    python -m unittest discover -s tests -t tests -p "test_forecast.py"
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

from core.forecast import DEFAULT_K, FEATURE_NAMES, MIN_SAMPLE, forecast  # noqa: E402

BEGIN = date(2024, 1, 2)
#: 需求要求输出的分位点
LEVELS = (5, 25, 50, 75, 95)
#: forecast 的必备字段（锁定对外接口）
REQUIRED_KEYS = ("expected_return", "median_return", "up_prob", "quantiles",
                 "sample", "confidence", "path", "degraded", "note")


# --------------------------------------------------------------------------- #
# 测试数据构造（全部确定性，可复现）
# --------------------------------------------------------------------------- #
def make_bars(closes, begin=BEGIN, with_time=True):
    """由收盘价序列构造标准K线（open/high/low/close/volume 齐备）。"""
    bars = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        bars.append({
            "t": (begin + timedelta(days=i)).isoformat() if with_time else None,
            "open": prev, "high": max(prev, c) * 1.002, "low": min(prev, c) * 0.998,
            "close": c, "volume": 1000000 + i,
        })
    return bars


def _uniforms(n, seed):
    """线性同余伪随机序列（固定实现，保证测试跨环境可复现）。"""
    a, c, m = 1103515245, 12345, 2 ** 31
    x, out = seed, []
    for _ in range(n):
        x = (a * x + c) % m
        out.append(x / m)
    return out


def _normal(n, seed):
    """Irwin-Hall 近似标准正态：12 个均匀分布求和减 6（关于 0 对称）。"""
    u = _uniforms(n * 12, seed * 7919 + 13)
    return [sum(u[i * 12:(i + 1) * 12]) - 6.0 for i in range(n)]


def random_walk_closes(n=520, sigma=0.012, seed=1):
    """无信息平稳序列：对数收益 iid、零均值、对称（未来与历史状态独立）。"""
    out, log_p = [], 0.0
    for z in _normal(n, seed):
        log_p += sigma * z
        out.append(100.0 * math.exp(log_p))
    return out


def mean_reverting_closes(n=520, sigma=0.012, seed=20240102):
    """价格围绕常数中枢波动的序列：低位状态携带真实的均值回复信号。"""
    return [100.0 * math.exp(sigma * z) for z in _normal(n, seed)]


def trending_closes(n=320, drift=0.004, amp=0.0015):
    """上升趋势序列：每根固定涨幅叠加小幅正弦摆动，前视收益几乎恒为正。"""
    return [100.0 * (1.0 + drift) ** i * (1.0 + amp * math.sin(i / 3.0))
            for i in range(n)]


# --------------------------------------------------------------------------- #
# 1. 对外契约与脏数据
# --------------------------------------------------------------------------- #
class TestContract(unittest.TestCase):
    """字段契约、参数回显与容错。"""

    def test_required_keys(self):
        r = forecast(make_bars(random_walk_closes(300)), 5)
        for key in REQUIRED_KEYS:
            self.assertIn(key, r)
        self.assertTrue(r["ok"])
        for key in ("asOf", "bars", "lastClose", "degrade_reason", "degrade_level",
                    "state", "match", "source"):
            self.assertIn(key, r)

    def test_quantile_levels(self):
        r = forecast(make_bars(random_walk_closes(300)), 5)
        self.assertEqual(set(r["quantiles"].keys()), set(LEVELS))
        self.assertEqual(set(r["path"]["levels"].values()), set(LEVELS))

    def test_probability_and_return_ranges(self):
        r = forecast(make_bars(random_walk_closes(300)), 5)
        self.assertGreaterEqual(r["up_prob"], 0.0)
        self.assertLessEqual(r["up_prob"], 1.0)
        self.assertGreaterEqual(r["confidence"], 0.0)
        self.assertLessEqual(r["confidence"], 1.0)
        self.assertEqual(r["sample"], r["match"]["k"])

    def test_k_and_horizon_echo(self):
        bars = make_bars(random_walk_closes(400))
        r10 = forecast(bars, 3, k=10)
        self.assertEqual(r10["horizon"], 3)
        self.assertEqual(r10["sample"], 10)
        # 路径长度 = horizon + 1（首点为当前价锚点）
        for key in ("median", "lo", "hi", "q25", "q75"):
            self.assertEqual(len(r10["path"][key]), 4)
        self.assertLessEqual(r10["sample"], min(10, DEFAULT_K))

    def test_path_anchor_is_last_close(self):
        bars = make_bars(random_walk_closes(300))
        r = forecast(bars, 4)
        p = r["path"]
        self.assertAlmostEqual(p["start"], bars[-1]["close"], places=6)
        for key in ("median", "lo", "hi", "q25", "q75"):
            self.assertAlmostEqual(p[key][0], bars[-1]["close"], places=6)

    def test_path_time_axis(self):
        bars = make_bars(random_walk_closes(200))
        r = forecast(bars, 3)
        axis = r["path"]["t"]
        self.assertEqual(len(axis), 4)
        self.assertEqual(axis[0], bars[-1]["t"])
        gap = (date.fromisoformat(axis[1]) - date.fromisoformat(axis[0])).days
        self.assertEqual(gap, 1)

    def test_path_time_axis_missing(self):
        bars = make_bars(random_walk_closes(200), with_time=False)
        r = forecast(bars, 3)                      # t 缺失不应抛异常
        self.assertIsNone(r["path"]["t"])
        self.assertEqual(len(r["path"]["median"]), 4)

    def test_note_is_historical_distribution_not_promise(self):
        r = forecast(make_bars(random_walk_closes(300)), 5)
        self.assertIn("历史条件分布", r["note"])
        self.assertIn("收益承诺", r["note"])
        self.assertIn("而非", r["note"])

    def test_dirty_input_never_raises(self):
        bad_cases = [
            [], None, [None, 1, "x"], [{}], [{"close": None}], [{"close": "abc"}],
            [{"close": float("nan")}], [{"close": float("inf")}], [{"close": -3}],
            [{"close": 0}], [{"close": True}], "not-a-list",
        ]
        for bad in bad_cases:
            r = forecast(bad, 5)
            self.assertIsInstance(r, dict)
            self.assertEqual(r["sample"], 0)
            self.assertTrue(r["degraded"])
            self.assertIn("降级", r["note"])

    def test_dirty_bars_are_dropped(self):
        # 脏K线被整根剔除：有效根数 = 传入根数 - 脏根数
        closes = random_walk_closes(120)
        bars = make_bars(closes)
        bars[3]["close"] = None
        bars[7]["close"] = float("nan")
        r = forecast(bars, 5)
        self.assertEqual(r["bars"], 118)
        self.assertTrue(r["ok"])

    def test_custom_weights_and_windows(self):
        bars = make_bars(random_walk_closes(300))
        r = forecast(bars, 5, weights={"ma_dev": 2.0, "rsi": 0.0, "mom": 1.0, "vol": 1.0},
                     windows={"ma": 10, "rsi": 6, "mom": 5, "vol": 10})
        self.assertEqual(r["match"]["weights"]["ma_dev"], 2.0)
        self.assertEqual(r["state"]["windows"]["ma"], 10)
        self.assertIsInstance(r["sample"], int)
        # 非法权重（全 0 / 负数 / 字符串）不应抛异常
        for bad in ({"ma_dev": 0.0, "rsi": 0.0, "mom": 0.0, "vol": 0.0}, None, "x"):
            self.assertIsInstance(forecast(bars, 5, weights=bad), dict)


# --------------------------------------------------------------------------- #
# 2. 分位数与路径分位带单调
# --------------------------------------------------------------------------- #
class TestQuantileMonotonic(unittest.TestCase):
    """分位数必须单调不减，路径上下轨必须包住中位路径。"""

    def assert_monotonic(self, r):
        q = r["quantiles"]
        for a, b in zip(LEVELS, LEVELS[1:]):
            self.assertLessEqual(q[a], q[b] + 1e-12,
                                 "分位数不单调：%s=%r > %s=%r" % (a, q[a], b, q[b]))
        path = r["path"]
        n = r["horizon"] + 1
        for key in ("t", "median", "lo", "hi", "q25", "q75"):
            self.assertEqual(len(path[key]), n, "路径 %s 长度应为 horizon + 1" % key)
        for i in range(n):
            self.assertLessEqual(path["lo"][i], path["q25"][i] + 1e-12)
            self.assertLessEqual(path["q25"][i], path["median"][i] + 1e-12)
            self.assertLessEqual(path["median"][i], path["q75"][i] + 1e-12)
            self.assertLessEqual(path["q75"][i], path["hi"][i] + 1e-12)
        # 路径终点的中位价 = 当前价 ×（1 + 全期收益中位数）
        expect = r["lastClose"] * (1.0 + q[50])
        self.assertAlmostEqual(path["median"][-1], expect,
                               delta=max(0.01, abs(r["lastClose"]) * 1e-3))

    def test_monotonic_normal(self):
        self.assert_monotonic(forecast(make_bars(random_walk_closes(400)), 5))

    def test_monotonic_trend(self):
        self.assert_monotonic(forecast(make_bars(trending_closes()), 5))

    def test_monotonic_degraded(self):
        self.assert_monotonic(forecast(make_bars(random_walk_closes(32)), 5))

    def test_monotonic_across_horizons(self):
        bars = make_bars(random_walk_closes(400))
        for hz in (1, 2, 3, 5, 8, 12, 20, 40):
            self.assert_monotonic(forecast(bars, hz))


# --------------------------------------------------------------------------- #
# 3. 样本不足降级
# --------------------------------------------------------------------------- #
class TestDegraded(unittest.TestCase):
    """样本不足时必须降级为历史整体分布，并标注 degraded / 原因。"""

    def test_short_history_degrades(self):
        # 32 根：特征预热（20 根）后只剩极少窗口 → 降级 level 1
        closes = random_walk_closes(32)
        r = forecast(make_bars(closes), 5)
        self.assertTrue(r["degraded"])
        self.assertEqual(r["degrade_level"], 1)
        self.assertIn("降级", r["note"])
        self.assertTrue(r["degrade_reason"])
        self.assertGreaterEqual(r["sample"], 1)
        self.assertTrue(r["ok"])

    def test_degraded_uses_whole_history(self):
        """降级后样本 = 全部可观测窗口（而不是预热后的交集），可逐项复核。"""
        closes = random_walk_closes(32)
        hz = 5
        r = forecast(make_bars(closes), hz, min_sample=10 ** 6)
        self.assertTrue(r["degraded"])
        # 可观测窗口：i + hz <= n - 1 且 i <= n - 1 - hz - 1（剔除与当前重叠的窗口）
        expect_n = len(closes) - hz - 1
        self.assertEqual(r["sample"], expect_n)
        rets = [closes[i + hz] / closes[i] - 1.0 for i in range(expect_n)]
        self.assertAlmostEqual(r["expected_return"], sum(rets) / len(rets), places=5)
        self.assertAlmostEqual(r["median_return"],
                               sorted(rets)[len(rets) // 2] if len(rets) % 2 else
                               (sorted(rets)[len(rets) // 2 - 1] + sorted(rets)[len(rets) // 2]) / 2,
                               delta=0.02)

    def test_insufficient_history_uses_single_bar_scaling(self):
        # 12 根 + horizon 30：连一根完整前视收益都没有 → 降级 level 2
        closes = random_walk_closes(12)
        r = forecast(make_bars(closes), 30)
        self.assertTrue(r["degraded"])
        self.assertEqual(r["degrade_level"], 2)
        self.assertIn("降级", r["note"])
        self.assertEqual(r["sample"], len(closes) - 1)     # 单根收益个数
        self.assertEqual(len(r["path"]["median"]), 31)
        # 单根收益复利外推：中位路径与当前价同号单调
        p = r["path"]
        self.assertTrue(all(a <= b for a, b in zip(p["median"], p["median"][1:]))
                        or all(a >= b for a, b in zip(p["median"], p["median"][1:])))

    def test_single_bar_gives_zero_distribution(self):
        r = forecast(make_bars([100.0]), 5)
        self.assertTrue(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["degrade_level"], 3)
        self.assertEqual(r["sample"], 0)
        self.assertEqual(r["confidence"], 0.0)
        self.assertEqual(set(r["quantiles"].values()), {0.0})
        self.assertEqual(r["path"]["median"], [100.0] * 6)

    def test_empty_bars(self):
        r = forecast([], 5)
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["sample"], 0)
        self.assertEqual(r["up_prob"], 0.0)
        self.assertIsNone(r["lastClose"])
        self.assertEqual(r["path"]["median"], [])
        self.assertIn("降级", r["note"])

    def test_degrade_lowers_confidence(self):
        # 同一序列：正常模式 vs 强制降级，降级置信度必须更低
        bars = make_bars(random_walk_closes(300))
        normal = forecast(bars, 5)
        forced = forecast(bars, 5, min_sample=10 ** 6)
        self.assertFalse(normal["degraded"])
        self.assertTrue(forced["degraded"])
        self.assertLess(forced["confidence"], normal["confidence"])
        # 样本越多置信度越高（离散度因子相同的前提下）
        small = forecast(bars, 5, k=10)
        self.assertLess(small["confidence"], normal["confidence"])

    def test_min_sample_zero_disables_degrade(self):
        bars = make_bars(random_walk_closes(300))
        r = forecast(bars, 5, min_sample=0)
        self.assertFalse(r["degraded"])
        self.assertEqual(r["sample"], min(DEFAULT_K, r["match"]["pool"]))

    def test_no_lookahead_in_matched_windows(self):
        """最近邻窗口必须满足 i + horizon <= n - 1 且不与当前窗口重叠。"""
        bars = make_bars(random_walk_closes(200))
        hz = 5
        r = forecast(bars, hz)
        self.assertTrue(r["match"]["nearest"])
        for item in r["match"]["nearest"]:
            self.assertLessEqual(item["i"], len(bars) - 1 - hz - 1)
            self.assertIsNotNone(item["dist"])
        # 可信样本数不得超过可用窗口数
        self.assertLessEqual(r["sample"], r["match"]["pool"])


# --------------------------------------------------------------------------- #
# 4. 平稳（无信息）序列：up_prob ≈ 50%
# --------------------------------------------------------------------------- #
class TestNoInformationUpProb(unittest.TestCase):
    """无方向性信息的平稳序列，条件分布不应出现系统性偏移。"""

    N_SEEDS = 6
    K = 200

    def test_up_prob_about_half_horizon5(self):
        ups = []
        for seed in range(1, self.N_SEEDS + 1):
            bars = make_bars(random_walk_closes(520, 0.012, seed))
            r = forecast(bars, 5, k=self.K)
            self.assertFalse(r["degraded"])
            self.assertEqual(r["sample"], self.K)
            ups.append(r["up_prob"])
        for u in ups:
            self.assertGreaterEqual(u, 0.35, "单条序列 up_prob 偏离过大：%r" % (ups,))
            self.assertLessEqual(u, 0.65, "单条序列 up_prob 偏离过大：%r" % (ups,))
        mean = sum(ups) / len(ups)
        self.assertGreaterEqual(mean, 0.44, "多序列平均 up_prob=%r" % (mean,))
        self.assertLessEqual(mean, 0.56, "多序列平均 up_prob=%r" % (mean,))

    def test_median_return_about_zero(self):
        for hz in (1, 3, 5, 10):
            r = forecast(make_bars(random_walk_closes(520, 0.012, 1)), hz, k=self.K)
            self.assertAlmostEqual(r["median_return"], 0.0, delta=0.05)
            self.assertAlmostEqual(r["expected_return"], 0.0, delta=0.06)
            self.assertGreaterEqual(r["up_prob"], 0.35)
            self.assertLessEqual(r["up_prob"], 0.65)

    def test_confidence_is_low_without_information(self):
        # 无信息序列上条件分布与无条件分布离散度接近 → 置信度不应过高
        r = forecast(make_bars(random_walk_closes(520)), 5, k=self.K)
        self.assertLess(r["confidence"], 0.6)

    def test_small_k_is_noisier(self):
        # 样本越少，置信度越低（同一个序列、同一组特征）
        bars = make_bars(random_walk_closes(520))
        self.assertLess(forecast(bars, 5, k=MIN_SAMPLE)["confidence"],
                        forecast(bars, 5, k=200)["confidence"])


# --------------------------------------------------------------------------- #
# 5. 上升趋势序列：up_prob > 55%
# --------------------------------------------------------------------------- #
class TestTrendUpProb(unittest.TestCase):
    """趋势序列的后验收益应为正，up_prob 明显高于 50%。"""

    def test_up_prob_above_55(self):
        bars = make_bars(trending_closes())
        r = forecast(bars, 5)
        self.assertFalse(r["degraded"])
        self.assertGreater(r["up_prob"], 0.55)
        self.assertGreater(r["expected_return"], 0.0)
        self.assertGreater(r["median_return"], 0.0)
        self.assertGreater(r["quantiles"][50], 0.0)
        # 强趋势下最低分位（5%）也不应为负
        self.assertGreater(r["quantiles"][5], 0.0)

    def test_up_prob_above_55_across_horizons(self):
        bars = make_bars(trending_closes())
        for hz in (1, 2, 3, 5, 10):
            r = forecast(bars, hz)
            self.assertGreater(r["up_prob"], 0.55, "horizon=%d 的 up_prob=%r"
                               % (hz, r["up_prob"]))
            self.assertGreater(r["expected_return"], 0.0)

    def test_path_band_lifts_with_trend(self):
        # 趋势序列：整条 95% 预测带都高于当前价（含下轨）
        r = forecast(make_bars(trending_closes()), 5)
        self.assertGreater(r["path"]["lo"][-1], r["lastClose"])
        self.assertGreater(r["path"]["hi"][-1], r["path"]["lo"][-1])

    def test_confidence_higher_with_smaller_dispersion(self):
        # 离散度小（趋势稳定）时置信度应高于噪声序列
        trend = forecast(make_bars(trending_closes()), 5)
        noise = forecast(make_bars(random_walk_closes(320)), 5)
        self.assertGreater(trend["confidence"], noise["confidence"])


# --------------------------------------------------------------------------- #
# 6. 状态筛选的信息量（对照：条件分布 vs 无条件分布）
# --------------------------------------------------------------------------- #
class TestStateFilterAddsInformation(unittest.TestCase):
    """价格围绕中枢波动时，低位状态的条件 up_prob 应显著高于无条件分布。"""

    def test_low_state_has_higher_up_prob(self):
        bars = make_bars(mean_reverting_closes())
        cond = forecast(bars, 5)                      # 条件分布（相似状态）
        uncond = forecast(bars, 5, min_sample=10 ** 6)  # 强制降级 = 无条件分布
        self.assertFalse(cond["degraded"])
        self.assertTrue(uncond["degraded"])
        self.assertGreater(cond["up_prob"], 0.55)
        self.assertGreater(cond["up_prob"], uncond["up_prob"])
        self.assertLess(abs(uncond["up_prob"] - 0.5), 0.12)
        # 条件分布更集中 → 距离度量的确筛出了可比窗口
        self.assertLess(cond["sample"], uncond["sample"])

    def test_state_snapshot_exposed(self):
        r = forecast(make_bars(mean_reverting_closes()), 5)
        state = r["state"]
        self.assertEqual(set(state["raw"].keys()), set(FEATURE_NAMES))
        self.assertEqual(set(state["quantile"].keys()), set(FEATURE_NAMES))
        for name in FEATURE_NAMES:
            q = state["quantile"][name]
            self.assertIsNotNone(q)
            self.assertGreaterEqual(q, 0.0)
            self.assertLessEqual(q, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
