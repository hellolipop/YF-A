# -*- coding: utf-8 -*-
"""core.scanner 的单元测试（仅标准库 unittest，无网络、无随机，全部为确定性合成数据）。

覆盖范围
--------
· 三道硬闸门：各自能拦下并给出中文原因、边界值、字段缺失时跳过闸门且计数正确、
  A 股 / 美股阈值差异、脏输入不抛异常；
· 风险分：打准 5 / 6 / 7 分阈值（HIGH-AVOID / CRITICAL-AVOID）、单日与两日回撤、
  量比阶梯、≥150% 涨幅单项直接 CRITICAL、总分截断到 10、无证据时不臆造；
· 因子：动量按短期反转取向（RSI 50–68 满分、RSI > 75 扣分、**高涨幅不得正分**的回归）、
  低换手加分 / 高换手扣分、极端放量与缩量都扣分、距阻力位过近扣分 / 适度满分 / 已突破中性；
· 复合评分：默认权重与等权重的精确落点、等级边界、缺失因子重新归一、权重非法回退；
· 单标的评分：封顶规则、无 K 线不臆造得分、短 K 线降级、reasons 不含单指标结论；
· 全市场扫描：排序（score 降序、同分按 amount 降序）、limit 截断与 stats 如实、
  无 K 线进 rejected、缺字段计数、空输入 / 脏输入、json.dumps(allow_nan=False) 通过。

运行方式
--------
    python3 tests/test_scanner.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest

# 让 tests/ 目录之外的包（core）可被导入，兼容任意工作目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import indicators as I  # noqa: E402
from core.scanner import (  # noqa: E402
    DEFAULT_SCAN_PARAMS, FACTORS, GRADE_BANDS, VERDICT_BUCKETS, VERDICT_LABELS,
    composite_score, grade_of, hard_filters, risk_score, scan_candidates,
    score_candidate, verdict_bucket,
)


# --------------------------------------------------------------------------- #
# 确定性合成数据
# --------------------------------------------------------------------------- #
def make_bars(closes, volume=1e6, amount=1e8, high=None, low=None):
    """由收盘价序列构造 K 线；high/low 缺省时等于收盘价（避免假的日内上影）。"""
    out = []
    for i, c in enumerate(closes):
        h = high[i] if isinstance(high, list) else (c if high is None else high)
        lw = low[i] if isinstance(low, list) else (c * 0.99 if low is None else low)
        out.append({"t": "d%03d" % i, "open": c, "high": h, "low": lw, "close": c,
                    "volume": volume, "amount": amount})
    return out


def alt_closes(n=90, up=0.01, down=0.0052, start=10.0):
    """交替 +up / -down：温和走强的典型形态（RSI 落在 50–68 甜点区）。"""
    c = [start]
    for i in range(n - 1):
        c.append(c[-1] * (1 + (up if i % 2 == 0 else -down)))
    return c


def mild_bars(n=90, volume=1e6, amount=1e8):
    return make_bars(alt_closes(n), volume=volume, amount=amount)


def mild_bars_no_vol(n=90):
    """不带成交量 / 成交额的温和走强序列（用于检验因子降级）。"""
    return make_bars(alt_closes(n), volume=None, amount=None)


def spike_bars(d, n=90):
    """温和走强 + 一处位于现价上方 d% 的历史高点（即「最近阻力位」）。"""
    b = mild_bars(n)
    last = b[-1]["close"]
    b[n // 2]["high"] = last * (1 + d / 100.0)
    return b


def breakout_bars(n=90):
    """已突破：单调上涨且 high == close（近 60 根没有高于现价的高点）。"""
    return make_bars([10 * 1.002 ** i for i in range(n)],
                     high=[10 * 1.002 ** i for i in range(n)])


def hot_bars(n=90):
    """单边 2%/日：RSI 100、60 日涨幅 228%（典型「已经涨上天」）。"""
    return make_bars([10 * 1.02 ** i for i in range(n)])


def overheat_bars(n=90):
    """交替 2%/0.5%：RSI ≈ 81（越过 75 过热线，但未到 150% 涨幅剔除线）。"""
    return make_bars(alt_closes(n, up=0.02, down=0.005))


def downtrend_bars(n=90):
    """单边 -0.5%/日：空头排列，用于趋势对照。"""
    return make_bars([10 * 0.995 ** i for i in range(n)])


def ret5_spike_bars(n=90):
    """温和走强 + 末段 5 根拉到 +15%：检验「高涨幅不得正分」的回归用例。"""
    c = alt_closes(n)
    base = c[-6]
    for k in range(5):
        c[-5 + k] = base * (1 + 0.15 * (k + 1) / 5.0)
    return make_bars(c)


def drop2d_bars():
    """两日累计跌幅 -10.7%，但单日最大跌幅仅 -5.5%（检验回撤项不叠加）。"""
    return make_bars([10.0] * 40 + [9.45, 8.93, 8.93, 8.93])


def rising_volume_bars(n=90):
    """最后一个交易日量能放大到前 5 日均量的 2 倍（量比只能由 K 线推算）。"""
    bars = mild_bars(n)
    bars[-1]["volume"] = 2e6
    bars[-1]["amount"] = 2e8
    return bars


def snap(**kw):
    """默认快照行（字段齐备、可通过三道硬闸门）。"""
    row = {"code": "600000", "name": "测试标的", "market": "cn", "price": 12.44,
           "changePct": 1.0, "amount": 3e8, "volumeRatio": 1.8, "turnover": 3.0}
    row.update(kw)
    return row


def flat_bars(n=40, price=10.0, volume=1e6, amount=1e8):
    return make_bars([price] * n, volume=volume, amount=amount)


def json_ok(obj):
    """严格 JSON 序列化（禁止 NaN / Infinity 混入）。"""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)


# --------------------------------------------------------------------------- #
# 一、三道硬闸门
# --------------------------------------------------------------------------- #
class TestHardFilters(unittest.TestCase):
    """硬闸门：不含技术面，只做流动性 / 反追高 / 量能三道过滤。"""

    def test_gate1_liquidity_rejects_low_amount(self):
        r = hard_filters(snap(amount=1.2e8))
        self.assertFalse(r["pass"])
        self.assertEqual(r["tags"], ["流动性不足"])
        self.assertIn("流动性不足", r["reasons"][0])
        self.assertIn("1.20 亿", r["reasons"][0])
        self.assertIn("硬闸门①", r["reasons"][0])
        self.assertEqual(r["missingGates"], 0)

    def test_gate1_boundary_at_threshold_passes(self):
        self.assertTrue(hard_filters(snap(amount=2e8))["pass"])
        self.assertFalse(hard_filters(snap(amount=2e8 - 1))["pass"])

    def test_gate2_anti_chase_rejects_high_change(self):
        r = hard_filters(snap(changePct=9.3))
        self.assertFalse(r["pass"])
        self.assertEqual(r["tags"], ["反追高"])
        self.assertIn("反追高", r["reasons"][0])
        self.assertIn("9.30%", r["reasons"][0])

    def test_gate2_boundary_seven_percent_passes(self):
        self.assertTrue(hard_filters(snap(changePct=7.0))["pass"])
        self.assertFalse(hard_filters(snap(changePct=7.01))["pass"])

    def test_gate3_volume_ratio_alone_passes(self):
        r = hard_filters(snap(volumeRatio=1.8, amountRatio=None))
        self.assertTrue(r["pass"])
        self.assertIsNone(r["metrics"]["amountRatio"])

    def test_gate3_amount_ratio_alone_passes(self):
        """量比不足但 5/20 日均额比够 —— 后者更抗单日异动。"""
        r = hard_filters(snap(volumeRatio=0.9, amountRatio=1.5))
        self.assertTrue(r["pass"])
        self.assertIn("amountRatio", r["metrics"])

    def test_gate3_both_low_rejects(self):
        r = hard_filters(snap(volumeRatio=1.0, amountRatio=1.1))
        self.assertFalse(r["pass"])
        self.assertEqual(r["tags"], ["量能不足"])
        self.assertIn("量能不足", r["reasons"][0])
        self.assertIn("硬闸门③", r["reasons"][0])

    def test_change_pct_missing_skips_gate_and_counts(self):
        r = hard_filters({"market": "cn", "amount": 3e8, "volumeRatio": 1.8})
        self.assertTrue(r["pass"], "字段缺失不拦截（避免误杀）")
        self.assertEqual(r["missingGates"], 1)
        self.assertEqual(r["missingFieldCounts"], {"changePct": 1})
        self.assertTrue(any("changePct" in w for w in r["warnings"]))

    def test_amount_missing_skips_gate_and_counts(self):
        r = hard_filters({"market": "cn", "changePct": 1.0, "volumeRatio": 1.8})
        self.assertTrue(r["pass"])
        self.assertEqual(r["missingGates"], 1)
        self.assertIn("amount", r["missingFieldCounts"])
        self.assertIsNone(r["metrics"].get("amount"))

    def test_volume_fields_missing_skips_one_gate_two_fields(self):
        r = hard_filters({"market": "cn", "amount": 3e8, "changePct": 1.0})
        self.assertTrue(r["pass"])
        self.assertEqual(r["missingGates"], 1)
        self.assertEqual(sorted(r["missingFieldCounts"]), ["amountRatio", "volumeRatio"])

    def test_nan_and_string_values_are_treated_as_missing(self):
        """NaN / 字符串等脏值按「缺失」处理：三道闸门全部跳过、一条都不拦。"""
        r = hard_filters({"market": "cn", "amount": float("nan"),
                          "changePct": "abc", "volumeRatio": "not-a-number"})
        self.assertTrue(r["pass"])
        self.assertEqual(r["missingGates"], 3)
        self.assertEqual(sorted(r["missingFieldCounts"]),
                         ["amount", "amountRatio", "changePct", "volumeRatio"])

    def test_us_market_uses_us_thresholds(self):
        us = hard_filters({"market": "us", "amount": 4e7, "changePct": 8.5, "volumeRatio": 1.9})
        self.assertTrue(us["pass"], "美股：0.3 亿美元 / 10% 涨幅上限")
        cn = hard_filters({"market": "cn", "amount": 4e7, "changePct": 8.5, "volumeRatio": 1.9})
        self.assertFalse(cn["pass"])
        self.assertEqual(len(cn["reasons"]), 2, "同一组数值在 A 股口径下同时踩①与②")

    def test_market_inferred_from_code(self):
        self.assertFalse(hard_filters({"code": "600000", "amount": 4e7, "changePct": 1.0,
                                       "volumeRatio": 2.0})["pass"])
        self.assertTrue(hard_filters({"code": "AAPL", "amount": 4e7, "changePct": 1.0,
                                      "volumeRatio": 2.0})["pass"])

    def test_scalar_param_applies_to_all_markets(self):
        us = hard_filters({"market": "us", "amount": 4e7, "changePct": 1.0, "volumeRatio": 2.0},
                          {"minAmount": 1e8})
        self.assertFalse(us["pass"], "标量参数对美股同样生效")

    def test_dirty_row_never_raises(self):
        for row in (None, [], "x", 42, {}, {"amount": None}):
            r = hard_filters(row)
            self.assertIn("pass", r)
        json_ok(hard_filters(snap(amount=float("inf"))))

    def test_default_params_declared(self):
        self.assertEqual(DEFAULT_SCAN_PARAMS["min_amount"]["cn"], 2e8)
        self.assertEqual(DEFAULT_SCAN_PARAMS["max_change_pct"]["cn"], 7.0)
        self.assertEqual(DEFAULT_SCAN_PARAMS["min_volume_ratio"], 1.2)
        self.assertEqual(DEFAULT_SCAN_PARAMS["min_amount_ratio"], 1.3)
        self.assertEqual(DEFAULT_SCAN_PARAMS["pct60d_reject"], 150.0)
        self.assertEqual(DEFAULT_SCAN_PARAMS["reject_risk_score"], 7)
        json_ok(DEFAULT_SCAN_PARAMS)


# --------------------------------------------------------------------------- #
# 二、风险分（0–10）
# --------------------------------------------------------------------------- #
class TestRiskScore(unittest.TestCase):
    """风险分阈值必须打得准：5–6 = HIGH/AVOID、7+ = CRITICAL/AVOID。"""

    def test_low_risk_is_ok(self):
        r = risk_score({"volumeRatio": 1.0, "chg60d": 30.0}, flat_bars())
        self.assertEqual(r["score"], 0)
        self.assertEqual((r["level"], r["action"]), ("LOW", "OK"))
        self.assertEqual(r["flags"], [])
        self.assertFalse(r["reject"])

    def test_volume_ratio_penalty_ladder(self):
        """量比阶梯：≥2 记 1、≥3 记 2、≥5 记 3。"""
        for rv, exp in ((2.1, 1), (3.1, 2), (5.1, 3)):
            r = risk_score({"volumeRatio": rv, "chg60d": 30.0}, flat_bars())
            self.assertEqual(r["score"], exp, "量比 %.1f 应记 %d 分" % (rv, exp))

    def test_pct60d_penalty_ladder(self):
        for pct, exp in ((30.0, 0), (45.0, 1), (65.0, 2), (95.0, 3)):
            r = risk_score({"volumeRatio": 1.0, "chg60d": pct}, flat_bars())
            self.assertEqual(r["score"], exp, "60 日涨幅 %.0f%% 应记 %d 分" % (pct, exp))

    def test_risk_five_is_high_avoid(self):
        r = risk_score({"volumeRatio": 4.0, "chg60d": 95.0}, flat_bars())
        self.assertEqual(r["score"], 5)
        self.assertEqual((r["level"], r["action"]), ("HIGH", "AVOID"))
        self.assertFalse(r["reject"])
        self.assertEqual(sorted(f["key"] for f in r["flags"]), ["pct60d", "volumeRatio"])

    def test_risk_six_is_high_avoid(self):
        r = risk_score({"volumeRatio": 6.0, "chg60d": 95.0}, flat_bars())
        self.assertEqual(r["score"], 6)
        self.assertEqual((r["level"], r["action"]), ("HIGH", "AVOID"))
        self.assertFalse(r["reject"], "6 分仍是 AVOID 但不到整只剔除线")

    def test_risk_seven_is_critical_avoid(self):
        r = risk_score({"volumeRatio": 6.0, "chg60d": 160.0}, flat_bars())
        self.assertEqual(r["score"], 7)
        self.assertEqual((r["level"], r["action"]), ("CRITICAL", "AVOID"))
        self.assertTrue(r["reject"])

    def test_pct60d_reject_flag_forces_critical(self):
        """单项极端（60 日 +200%）不得因总分只有 4 分而被稀释。"""
        r = risk_score({"volumeRatio": 1.0, "chg60d": 200.0}, flat_bars())
        self.assertEqual(r["score"], 4)
        self.assertEqual((r["level"], r["action"]), ("CRITICAL", "AVOID"))
        self.assertTrue(r["reject"])
        self.assertTrue(any(f["reject"] for f in r["flags"]))

    def test_single_day_drop_penalty_counted_once(self):
        """单日 -10% 同时满足「单日 > 7%」与「两日 > 10%」，只记一次 2 分。"""
        b = make_bars([10.0] * 30 + [9.0, 9.0, 9.0] + [9.0 + 0.1 * i for i in range(7)])
        r = risk_score({"volumeRatio": 1.0, "chg60d": 10.0}, b)
        self.assertEqual(r["score"], 2)
        self.assertEqual([f["key"] for f in r["flags"]], ["drawdown"])
        self.assertLessEqual(r["metrics"]["maxDrop1d"], -7.0)

    def test_two_day_drop_only(self):
        """单日仅 -5.5%，但两日累计 -10.7% —— 只由两日口径触发。"""
        r = risk_score({"volumeRatio": 1.0, "chg60d": 10.0}, drop2d_bars())
        self.assertEqual(r["score"], 2)
        self.assertEqual([f["key"] for f in r["flags"]], ["drawdown"])
        self.assertGreater(r["metrics"]["maxDrop1d"], -7.0)
        self.assertLessEqual(r["metrics"]["maxDrop2d"], -10.0)
        self.assertIn("两日累计跌幅", r["flags"][0]["label"])

    def test_ma20_deviation_penalty(self):
        r = risk_score({"volumeRatio": 2.5, "chg60d": 65.0}, flat_bars())
        self.assertEqual(r["score"], 3)
        ramp = risk_score({"volumeRatio": 1.0, "chg60d": 30.0}, hot_bars())
        self.assertTrue(any(f["key"] == "ma20Dev" for f in ramp["flags"]),
                        "单边上涨的 MA20 乖离应被记入风险")

    def test_score_is_capped_at_ten(self):
        """总分上限 10：极端放量 + 极端涨幅 + 大幅回撤 + 乖离 + 高波动会超上限。"""
        closes = [10.0] * 20 + [7.5, 7.5] + [8.0, 8.5, 9.0, 9.5, 10.0,
                                             11.0, 12.0, 13.0, 14.0, 15.0]
        r = risk_score({"volumeRatio": 9.0, "chg60d": 300.0}, make_bars(closes))
        self.assertEqual(r["score"], 10)
        self.assertGreaterEqual(r["rawScore"], 11, "rawScore 保留截断前的合计便于审计")
        self.assertEqual(r["level"], "CRITICAL")
        self.assertTrue(any(f["key"] == "drawdown" for f in r["flags"]))
        self.assertTrue(any(f["key"] == "ma20Dev" for f in r["flags"]))

    def test_no_evidence_means_no_verdict(self):
        """无任何可算字段时不能凭空判「安全」。"""
        r = risk_score({}, [])
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["evidence"], [])
        self.assertEqual(r["flags"], [])

    def test_dirty_inputs_never_raise(self):
        for row, bars in ((None, None), ("x", "y"), (42, [None, {}, "z"]),
                          ({"volumeRatio": "abc"}, [{"close": float("nan")}])):
            r = risk_score(row, bars)
            self.assertIn(r["score"], range(0, 11))
        json_ok(risk_score({"volumeRatio": 6.0, "chg60d": 160.0}, flat_bars()))


# --------------------------------------------------------------------------- #
# 三、评分因子（动量取反转取向、量能偏好低换手、位置看距阻力位的空间）
# --------------------------------------------------------------------------- #
class TestFactors(unittest.TestCase):
    """因子的方向性必须符合调研口径：短期反转、低换手更优、不追高。"""

    def test_momentum_sweet_spot_scores_full(self):
        bars = mild_bars()
        rsi = I.RSI([b["close"] for b in bars], 14)[-1]
        self.assertTrue(50.0 <= rsi <= 68.0, "构造数据应落在 RSI 甜点区，实际 %.2f" % rsi)
        sc = score_candidate(snap(), bars)
        self.assertEqual(sc["factors"]["momentum"], 100.0)

    def test_momentum_punishes_overheat(self):
        bars = hot_bars()
        rsi = I.RSI([b["close"] for b in bars], 14)[-1]
        self.assertGreater(rsi, 75.0)
        hot = score_candidate(snap(), bars)
        mild = score_candidate(snap(), mild_bars())
        self.assertEqual(hot["factors"]["momentum"], 30.0, "RSI 过热 + 近 5 日涨幅 > 12% + 近 20 日 > 20%")
        self.assertLess(hot["factors"]["momentum"], mild["factors"]["momentum"])

    def test_high_return_is_not_rewarded(self):
        """回归用例：把「涨幅越大越好」当正分会导致追高，必须被拦住。"""
        mild = score_candidate(snap(), mild_bars())
        spiked = score_candidate(snap(), ret5_spike_bars())
        self.assertGreaterEqual(mild["metrics"]["ret5"], 0.0)
        self.assertLessEqual(mild["metrics"]["ret5"], 5.0)
        self.assertGreater(spiked["metrics"]["ret5"], 12.0)
        self.assertEqual(spiked["factors"]["momentum"], 25.0)
        self.assertLess(spiked["factors"]["momentum"], mild["factors"]["momentum"])
        self.assertLessEqual(spiked["score"], 64, "过热封顶不允许 A/B 档")

    def test_low_turnover_beats_high_turnover(self):
        bars = mild_bars_no_vol()
        low = score_candidate({"volumeRatio": 1.8, "turnover": 3.0}, bars)
        high = score_candidate({"volumeRatio": 1.8, "turnover": 22.0}, bars)
        self.assertEqual(low["factors"]["volume"], 100.0)
        self.assertEqual(high["factors"]["volume"], 80.0)
        self.assertGreater(low["score"], high["score"])

    def test_extreme_and_shrinking_volume_are_both_penalised(self):
        bars = mild_bars_no_vol()
        normal = score_candidate({"volumeRatio": 1.8, "turnover": 3.0}, bars)
        extreme = score_candidate({"volumeRatio": 5.2, "turnover": 3.0}, bars)
        shrink = score_candidate({"volumeRatio": 0.6, "turnover": 3.0}, bars)
        self.assertEqual(extreme["factors"]["volume"], 40.0, "极端放量属拉抬风险")
        self.assertEqual(shrink["factors"]["volume"], 33.3, "缩量无资金关注")
        self.assertGreater(normal["factors"]["volume"], extreme["factors"]["volume"])
        self.assertGreater(normal["factors"]["volume"], shrink["factors"]["volume"])

    def test_volume_factor_renormalizes_when_turnover_missing(self):
        with_to = score_candidate({"volumeRatio": 1.8, "turnover": 3.0}, mild_bars())
        no_to = score_candidate({"volumeRatio": 1.8}, mild_bars())
        self.assertEqual(with_to["factors"]["volume"], 87.0, "(60 + 25 均额比 + 15 低换手) / 100")
        self.assertEqual(no_to["factors"]["volume"], 84.7, "(60 + 12 均额比) / 85（换手子项剔除）")
        self.assertEqual(no_to["metrics"]["amountRatio"], 1.0)
        self.assertTrue(any("turnover" in w for w in no_to["warnings"]))

    def test_position_near_resistance_is_penalised(self):
        near = score_candidate(snap(), spike_bars(1.5))
        tight = score_candidate(snap(), spike_bars(0.5))
        roomy = score_candidate(snap(), spike_bars(8.0))
        self.assertEqual(tight["factors"]["position"], 20.0, "几乎贴着阻力位 = 没有空间")
        self.assertEqual(near["factors"]["position"], 45.0)
        self.assertEqual(roomy["factors"]["position"], 100.0)
        self.assertLess(tight["score"], near["score"])
        self.assertLess(near["score"], roomy["score"])
        self.assertEqual(near["metrics"]["distToResistance"], 1.5)
        self.assertEqual(roomy["metrics"]["distToResistance"], 8.0)

    def test_position_far_from_resistance_is_mediocre(self):
        self.assertEqual(score_candidate(snap(), spike_bars(3.0))["factors"]["position"], 100.0)
        self.assertEqual(score_candidate(snap(), spike_bars(30.0))["factors"]["position"], 70.0)
        self.assertEqual(score_candidate(snap(), spike_bars(50.0))["factors"]["position"], 45.0)

    def test_position_breakout_is_neutral_positive(self):
        sc = score_candidate(snap(), breakout_bars())
        self.assertEqual(sc["factors"]["position"], 60.0)
        self.assertTrue(sc["metrics"]["atHigh"])
        self.assertIsNone(sc["metrics"]["resistance"])

    def test_trend_factor_direction(self):
        mild = score_candidate(snap(), mild_bars())
        down = score_candidate(snap(), downtrend_bars())
        self.assertEqual(mild["factors"]["trend"], 100.0)
        self.assertEqual(down["factors"]["trend"], 0.0)
        self.assertLess(down["score"], mild["score"])

    def test_exact_full_row_scores(self):
        """整段链路（含因子权重）的精确落点，任何调参都必须同步这两个数。"""
        self.assertEqual(score_candidate(snap(), mild_bars())["score"], 89)
        self.assertEqual(score_candidate(snap(), spike_bars(8.0))["score"], 97)
        self.assertEqual(score_candidate(snap(), spike_bars(1.5))["score"], 86)
        self.assertEqual(score_candidate(snap(), downtrend_bars())["score"], 53)


# --------------------------------------------------------------------------- #
# 四、复合评分与等级映射
# --------------------------------------------------------------------------- #
#: 固定因子组合，用于精确断言权重与落点
FIXED_FACTORS = {"momentum": 60, "trend": 70, "position": 80, "volume": 50, "risk": 40}
#: 四组等权（technical / volume / risk / strength 各 0.25）
EQUAL_GROUPS = {"technical": 0.25, "volume": 0.25, "risk": 0.25, "strength": 0.25}


class TestCompositeScore(unittest.TestCase):
    """复合评分：技术 0.40 / 量能 0.25 / 风险(反向) 0.25 / 强度 0.10。"""

    def test_default_weights_exact(self):
        r = composite_score(FIXED_FACTORS)
        self.assertEqual(r["score"], 59, "0.10×60+0.25×50+0.20×70+0.20×80+0.25×40 = 58.5 → 59")
        self.assertEqual(r["grade"], "C")
        self.assertFalse(r["renormalized"])
        self.assertEqual(sorted(r["dropped"]), [])
        self.assertEqual(r["weights"],
                         {"momentum": 0.1, "volume": 0.25, "trend": 0.2, "position": 0.2,
                          "risk": 0.25})

    def test_equal_group_weights_exact(self):
        r = composite_score(FIXED_FACTORS, {"weights": EQUAL_GROUPS})
        self.assertEqual(r["score"], 56, "technical(70,80)→75 与 60/50/40 四组等权 = 56.25 → 56")
        self.assertEqual(r["grade"], "C")
        self.assertEqual(r["weights"]["trend"], 0.125, "technical 组在组内等分")
        self.assertEqual(r["weights"]["position"], 0.125)
        self.assertEqual(r["weights"]["momentum"], 0.25)

    def test_single_factor_weight_exact(self):
        self.assertEqual(composite_score(FIXED_FACTORS, {"weights": {"risk": 1.0}})["score"], 40)
        self.assertEqual(composite_score(FIXED_FACTORS, {"weights": {"momentum": 1.0}})["score"], 60)

    def test_missing_factor_renormalized(self):
        r = composite_score(dict(FIXED_FACTORS, momentum=None), {"weights": EQUAL_GROUPS})
        self.assertEqual(r["score"], 55, "剔掉 strength 组后 = (75+50+40)/3 = 55")
        self.assertEqual(r["dropped"], ["momentum"])
        self.assertTrue(r["renormalized"])

    def test_all_factors_missing_is_zero(self):
        r = composite_score({})
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["grade"], "F")
        self.assertEqual(sorted(r["dropped"]), sorted(FACTORS))

    def test_all_zero_or_unknown_weights_fall_back(self):
        zero = {"momentum": 0, "volume": 0, "trend": 0, "position": 0, "risk": 0}
        self.assertEqual(composite_score(FIXED_FACTORS, {"weights": zero})["score"], 59)
        self.assertEqual(composite_score(FIXED_FACTORS, {"weights": {"foo": 1}})["score"], 59)
        self.assertEqual(composite_score(FIXED_FACTORS, {"weights": "bad"})["score"], 59)

    def test_out_of_range_factor_is_clamped(self):
        self.assertEqual(composite_score({"risk": 200}, {"weights": {"risk": 1.0}})["score"], 100)
        self.assertEqual(composite_score({"risk": -50}, {"weights": {"risk": 1.0}})["score"], 0)

    def test_grade_bands(self):
        for score, grade in ((100, "A"), (80, "A"), (79, "B"), (65, "B"), (64, "C"),
                             (50, "C"), (49, "D"), (35, "D"), (34, "F"), (0, "F")):
            self.assertEqual(grade_of(score), grade, "%s 分应为 %s 档" % (score, grade))
        self.assertEqual([g for g, _ in GRADE_BANDS], ["A", "B", "C", "D", "F"])
        for bad in (None, "abc", float("nan"), float("inf")):
            self.assertEqual(grade_of(bad), "F")

    def test_verdict_labels_and_buckets(self):
        for grade in ("A", "B", "C", "D", "F"):
            self.assertIn(grade, VERDICT_LABELS)
            self.assertIn(grade, VERDICT_BUCKETS)
        self.assertEqual(VERDICT_LABELS["A"], "值得买入")
        self.assertEqual(verdict_bucket("A"), "值得买入")
        self.assertEqual(verdict_bucket("B"), "观察")
        self.assertEqual(verdict_bucket("C"), "观察")
        self.assertEqual(verdict_bucket("F"), "回避")
        self.assertEqual(verdict_bucket("?"), "观察")

    def test_json_safe(self):
        json_ok(composite_score(FIXED_FACTORS, {"weights": EQUAL_GROUPS}))


# --------------------------------------------------------------------------- #
# 五、单标的评分（封顶、降级、可解释文案）
# --------------------------------------------------------------------------- #
class TestScoreCandidate(unittest.TestCase):
    """score_candidate：五因子 + 封顶 + 一句话结论，缺数据必须降级而不臆造。"""

    def test_required_structure(self):
        sc = score_candidate(snap(), mild_bars())
        for key in ("score", "grade", "verdict", "verdict3", "factors", "weights",
                    "reasons", "warnings", "metrics", "risk"):
            self.assertIn(key, sc)
        self.assertEqual(sorted(sc["factors"]), sorted(FACTORS))
        for key in ("amount", "changePct", "volumeRatio", "ma20Dev", "rsi", "turnover",
                    "distToResistance", "pct60d", "maxDrop1d", "atHigh", "bars"):
            self.assertIn(key, sc["metrics"])
        for key in ("score", "level", "action", "flags"):
            self.assertIn(key, sc["risk"])
        self.assertEqual(sc["score"], sc["rawScore"], "无封顶时两者一致")

    def test_verdict_is_one_sentence_with_reason(self):
        sc = score_candidate(snap(), mild_bars())
        self.assertTrue(sc["verdict"].startswith(VERDICT_LABELS[sc["grade"]] + "："))
        self.assertIn("风险分", sc["verdict"])
        self.assertEqual(sc["verdict3"], VERDICT_BUCKETS[sc["grade"]])
        self.assertNotIn("\n", sc["verdict"])

    def test_reasons_are_weighted_not_single_indicator(self):
        """单指标（RSI / 均线）不得独立构成买入理由。"""
        sc = score_candidate(snap(), mild_bars())
        self.assertTrue(sc["reasons"])
        for r in sc["reasons"]:
            self.assertIn("权重", r, "每条理由都要带上因子权重：" + r)
        joined = " ".join(sc["reasons"])
        self.assertNotIn("因为 RSI", joined)
        self.assertNotIn("所以买入", joined)

    def test_exact_risk_only_pipeline(self):
        """把权重全压在风险因子上，即可精确验证「风险分 → 因子 → 合成分 → 档位」。"""
        bars = mild_bars(40)              # 40 根：60 日涨幅回落到快照字段，行为可预期
        w = {"weights": {"risk": 1.0}}
        cases = (("risk0", {"volumeRatio": 1.0, "chg60d": 10.0}, 100, "A"),
                 ("risk3", {"volumeRatio": 2.5, "chg60d": 65.0}, 70, "B"),
                 ("risk5", {"volumeRatio": 4.0, "chg60d": 95.0}, 49, "D"),
                 ("risk6", {"volumeRatio": 6.0, "chg60d": 95.0}, 40, "D"),
                 ("risk7", {"volumeRatio": 6.0, "chg60d": 160.0}, 30, "F"))
        for label, row, exp_score, exp_grade in cases:
            sc = score_candidate(row, bars, w)
            self.assertEqual(sc["factors"]["risk"], 100.0 - 10.0 * sc["risk"]["score"],
                             label + " 风险因子必须等于 100 − 10×风险分")
            self.assertEqual(sc["score"], exp_score, label)
            self.assertEqual(sc["grade"], exp_grade, label)
            self.assertEqual(sc["verdict3"], VERDICT_BUCKETS[exp_grade])

    def test_avoid_risk_caps_score_into_d(self):
        sc = score_candidate({"volumeRatio": 4.0, "chg60d": 95.0}, mild_bars(40),
                             {"weights": {"risk": 1.0}})
        self.assertEqual(sc["rawScore"], 50)
        self.assertEqual(sc["score"], 49, "HIGH/AVOID 封顶 49（最高 D 档）")
        self.assertEqual([c["cap"] for c in sc["capped"]], [49, 64])
        self.assertTrue(any("封顶" in w for w in sc["warnings"]))

    def test_reject_item_caps_score_into_f(self):
        sc = score_candidate({"volumeRatio": 6.0, "chg60d": 200.0}, mild_bars(40),
                             {"weights": {"risk": 1.0}})
        self.assertTrue(sc["risk"]["reject"])
        self.assertLessEqual(sc["score"], 34)
        self.assertEqual(sc["grade"], "F")
        self.assertEqual(sc["verdict3"], "回避")

    def test_overheat_caps_at_c(self):
        sc = score_candidate(snap(), overheat_bars())
        self.assertEqual(sc["score"], 64, "RSI 81 > 75：过热封顶到 C 档上限")
        self.assertEqual(sc["grade"], "C")
        self.assertLess(sc["score"], sc["rawScore"])
        self.assertTrue(any("过热封顶" in c["why"] for c in sc["capped"]))
        self.assertTrue(any("RSI" in w and "过热" in w for w in sc["warnings"]))

    def test_hot_stock_is_f(self):
        sc = score_candidate(snap(), hot_bars())
        self.assertEqual(sc["score"], 34)
        self.assertEqual(sc["grade"], "F")
        self.assertEqual(sc["risk"]["level"], "CRITICAL")
        self.assertTrue(sc["risk"]["reject"])

    def test_no_bars_never_fabricates_factors(self):
        """没有 K 线时（且快照也没给量能字段）任何因子都不得被编造。"""
        for bars in (None, [], [None], ["x"], [{"close": float("nan")}], [{"close": -1}]):
            sc = score_candidate({}, bars)
            self.assertEqual(sc["score"], 0)
            self.assertEqual(sc["grade"], "F")
            self.assertTrue(sc["degraded"])
            for f in FACTORS:
                self.assertIsNone(sc["factors"][f], "缺 K 线时 %s 不得被编造" % f)
            self.assertTrue(any("无有效 K 线" in w for w in sc["warnings"]))

    def test_no_bars_with_snapshot_volume_is_labelled(self):
        """快照提供了量比 → 只允许量能因子有值，其余仍为缺失，并标记降级。"""
        sc = score_candidate({"volumeRatio": 1.8, "turnover": 3.0}, None)
        self.assertTrue(sc["degraded"])
        self.assertEqual(sc["factors"]["volume"], 100.0)
        for f in ("momentum", "trend", "position"):
            self.assertIsNone(sc["factors"][f])
        self.assertTrue(any("无有效 K 线" in w for w in sc["warnings"]))

    def test_single_bar_is_not_a_signal(self):
        sc = score_candidate({}, [10.0])
        self.assertEqual(sc["score"], 0)
        self.assertEqual(sc["grade"], "F")
        self.assertTrue(sc["degraded"])

    def test_snapshot_only_still_allowed(self):
        """没有 K 线时只允许用快照字段（量比 / 60 日涨幅）评估，并明确标记降级。"""
        sc = score_candidate({"volumeRatio": 3.0, "chg60d": 120.0}, None)
        self.assertTrue(sc["degraded"])
        self.assertIsNone(sc["factors"]["trend"])
        self.assertEqual(sc["factors"]["volume"], 100.0,
                         "只剩量比子项时按可用子项归一（并用 warning 说明降级）")
        self.assertLessEqual(sc["score"], 64, "60 日 +120% 触发过热封顶")
        self.assertTrue(any("无有效 K 线" in w for w in sc["warnings"]))

    def test_short_history_is_degraded(self):
        sc = score_candidate(snap(), mild_bars(25))
        self.assertTrue(sc["degraded"])
        self.assertTrue(any("K 线仅 25 根" in w for w in sc["warnings"]))
        self.assertLess(sc["score"], 100)

    def test_change_pct_missing_fallback_is_disclosed(self):
        sc = score_candidate({"volumeRatio": 1.8, "turnover": 3.0}, mild_bars())
        self.assertEqual(sc["metrics"]["changePctSource"], "bars")
        self.assertTrue(any("changePct" in w for w in sc["warnings"]))

    def test_chasing_a_hard_gate_is_warned(self):
        sc = score_candidate(snap(changePct=9.5), mild_bars())
        self.assertTrue(any("追高" in w for w in sc["warnings"]))

    def test_dirty_inputs_never_raise(self):
        for row, bars in ((None, None), ("x", "y"), (42, 7), ({}, "bars"),
                          ({"turnover": "abc", "amount": float("inf")}, [{"close": 10.0}])):
            sc = score_candidate(row, bars)
            self.assertIn(sc["grade"], ("A", "B", "C", "D", "F"))
            json_ok(sc)

    def test_json_safe(self):
        json_ok(score_candidate(snap(), mild_bars()))
        json_ok(score_candidate({"volumeRatio": 6.0, "chg60d": 160.0}, flat_bars()))


# --------------------------------------------------------------------------- #
# 六、全市场扫描
# --------------------------------------------------------------------------- #
def scan_row(code, market="cn", price=12.4, change_pct=1.0, amount=3e8,
             volume_ratio=1.8, turnover=3.0, **kw):
    row = {"code": code, "name": code + " 名称", "market": market, "price": price,
           "changePct": change_pct, "amount": amount, "volumeRatio": volume_ratio,
           "turnover": turnover}
    row.update(kw)
    return row


class TestScanCandidates(unittest.TestCase):
    """主入口：排序、截断、剔除归因与统计必须自洽且诚实。"""

    def test_sort_by_score_then_amount(self):
        rows = [scan_row("LOWAMT", amount=2.1e8), scan_row("HIAMT", amount=9e8),
                scan_row("NEAR", amount=5e8)]
        bars = {"LOWAMT": mild_bars(), "HIAMT": mild_bars(), "NEAR": spike_bars(1.5)}
        res = scan_candidates(rows, bars)
        self.assertEqual([c["code"] for c in res["candidates"]], ["HIAMT", "LOWAMT", "NEAR"])
        self.assertEqual([c["score"] for c in res["candidates"]], [89, 89, 86])
        self.assertGreater(res["candidates"][0]["amount"], res["candidates"][1]["amount"])

    def test_limit_truncates_and_stats_report_it(self):
        rows = [scan_row("A"), scan_row("B"), scan_row("C")]
        bars = {"A": mild_bars(), "B": mild_bars(), "C": mild_bars()}
        res = scan_candidates(rows, bars, limit=2)
        self.assertEqual(len(res["candidates"]), 2)
        self.assertEqual(res["stats"]["passed"], 3, "passed 记截断前的候选数")
        self.assertEqual(res["stats"]["returned"], 2)
        self.assertEqual(res["stats"]["truncated"], 1)
        self.assertEqual(res["stats"]["limit"], 2)
        self.assertIn("截断 1 只", res["note"])

    def test_invalid_limit_means_no_truncation(self):
        rows = [scan_row("A"), scan_row("B")]
        bars = {"A": mild_bars(), "B": mild_bars()}
        for bad in (None, 0, -3, "abc", float("nan")):
            res = scan_candidates(rows, bars, limit=bad)
            self.assertEqual(len(res["candidates"]), 2)
            self.assertEqual(res["stats"]["truncated"], 0)

    def test_stats_are_self_consistent(self):
        rows = [scan_row("OK"), scan_row("NOBARS"), scan_row("CHEAP", amount=1e8),
                scan_row("CHASE", change_pct=9.9), scan_row("RISKY", volume_ratio=6.0,
                                                             chg60d=160.0)]
        bars = {"OK": mild_bars(), "CHEAP": mild_bars(), "CHASE": mild_bars(),
                "RISKY": flat_bars()}
        res = scan_candidates(rows, bars)
        st = res["stats"]
        self.assertEqual(st["scanned"], 5)
        self.assertEqual(st["scanned"], st["passed"] + st["rejected"])
        self.assertEqual(sum(st["byGrade"].values()), st["passed"])
        self.assertEqual(sum(st["byRejectReason"].values()), st["rejected"])
        self.assertEqual(st["returned"] + st["truncated"], st["passed"])
        self.assertEqual(sorted(st["byGrade"]), ["A", "B", "C", "D", "F"])
        json_ok(res)

    def test_missing_bars_go_to_rejected_not_fabricated(self):
        rows = [scan_row("NOBARS", amount=9e8, volume_ratio=2.5)]
        res = scan_candidates(rows, {})
        self.assertEqual(res["candidates"], [])
        self.assertEqual(len(res["rejected"]), 1)
        rej = res["rejected"][0]
        self.assertEqual(rej["code"], "NOBARS")
        self.assertEqual(rej["stage"], "data")
        self.assertIn("无K线数据", rej["reasons"][0])
        self.assertIn("不做技术面评分", rej["reasons"][0])
        self.assertEqual(res["stats"]["byRejectReason"], {"无K线数据": 1})
        self.assertEqual(res["stats"]["scored"], 0)

    def test_dirty_bars_entries_are_rejected(self):
        rows = [scan_row("BAD")]
        res = scan_candidates(rows, {"BAD": ["x", None, {"close": "abc"}]})
        self.assertEqual(res["candidates"], [])
        self.assertIn("无K线数据", res["rejected"][0]["reasons"][0])

    def test_too_short_history_is_rejected(self):
        rows = [scan_row("SHORT")]
        res = scan_candidates(rows, {"SHORT": mild_bars(15)})
        self.assertEqual(res["candidates"], [])
        self.assertIn("K线不足", res["rejected"][0]["reasons"][0])
        self.assertEqual(res["stats"]["byRejectReason"], {"K线不足": 1})

    def test_each_hard_gate_is_reported_with_chinese_reason(self):
        rows = [scan_row("CHEAP", amount=1e8), scan_row("CHASE", change_pct=9.9),
                scan_row("LOWVOL", volume_ratio=1.0)]
        bars = {r["code"]: mild_bars() for r in rows}
        res = scan_candidates(rows, bars)
        got = {r["code"]: r for r in res["rejected"]}
        self.assertEqual(got["CHEAP"]["stage"], "hard")
        self.assertIn("流动性不足", got["CHEAP"]["reasons"][0])
        self.assertIn("反追高", got["CHASE"]["reasons"][0])
        self.assertIn("量能不足", got["LOWVOL"]["reasons"][0])
        self.assertEqual(got["CHEAP"]["tags"], ["流动性不足"])
        self.assertEqual(got["CHASE"]["tags"], ["反追高"])
        self.assertEqual(got["LOWVOL"]["tags"], ["量能不足"])
        self.assertEqual(res["stats"]["byRejectReason"],
                         {"流动性不足": 1, "反追高": 1, "量能不足": 1})
        self.assertEqual(res["candidates"], [])

    def test_risk_threshold_rejects_before_scoring_output(self):
        rows = [scan_row("RISKY", volume_ratio=6.0, chg60d=160.0)]
        res = scan_candidates(rows, {"RISKY": flat_bars()})
        self.assertEqual(res["candidates"], [])
        rej = res["rejected"][0]
        self.assertEqual(rej["stage"], "risk")
        self.assertIn("风险分", rej["reasons"][0])
        self.assertEqual(res["stats"]["byRejectReason"], {"风险分超阈值": 1})
        self.assertEqual(res["stats"]["scored"], 1, "它确实被打过分，但按规则剔除")

    def test_change_pct_missing_skips_gate_and_counts(self):
        rows = [{"code": "NOPCT", "name": "缺涨幅", "market": "cn", "amount": 3e8,
                 "volumeRatio": 1.8, "turnover": 3.0}]
        res = scan_candidates(rows, {"NOPCT": mild_bars()})
        self.assertEqual(res["stats"]["scanned"], 1)
        self.assertEqual(res["stats"]["missingFields"], 1)
        self.assertEqual(res["stats"]["missingFieldCounts"], {"changePct": 1})
        self.assertEqual(len(res["candidates"]), 1, "缺字段不拦截（避免误杀）")
        cand = res["candidates"][0]
        self.assertEqual(cand["metrics"]["changePctSource"], "bars")
        self.assertTrue(any("changePct" in w for w in cand["warnings"]))

    def test_two_gates_skipped_when_fields_absent(self):
        rows = [{"code": "THIN", "name": "字段稀缺", "market": "cn", "amount": 3e8}]
        res = scan_candidates(rows, {"THIN": mild_bars_no_vol()})
        self.assertEqual(res["stats"]["missingFields"], 2, "涨幅闸门 + 量能闸门各计一次")
        self.assertEqual(sorted(res["stats"]["missingFieldCounts"]),
                         ["amountRatio", "changePct", "volumeRatio"])
        cand = res["candidates"][0]
        self.assertIsNone(cand["factors"]["volume"])
        self.assertTrue(any("降级" in w for w in cand["warnings"]))

    def test_volume_ratio_derived_from_bars(self):
        """快照没给量比时，用注入的 K 线算 5 日量比补上硬闸门③。"""
        rows = [{"code": "BARSVOL", "name": "K线补量", "market": "cn", "price": 12.4,
                 "changePct": 1.0, "amount": 3e8, "turnover": 3.0}]
        res = scan_candidates(rows, {"BARSVOL": rising_volume_bars()})
        self.assertEqual(len(res["candidates"]), 1)
        cand = res["candidates"][0]
        self.assertEqual(cand["metrics"]["volumeRatioSource"], "bars")
        self.assertGreaterEqual(cand["metrics"]["volumeRatio"], 1.2)

    def test_mixed_markets_run(self):
        rows = [scan_row("AAPL", market="us", price=200.0, change_pct=8.5, amount=4e7,
                         turnover=None),
                scan_row("600000", market="cn", amount=3e8),
                scan_row("600001", market="cn", amount=4e7)]
        bars = {r["code"]: mild_bars() for r in rows}
        res = scan_candidates(rows, bars)
        markets = {c["code"]: c["market"] for c in res["candidates"]}
        self.assertEqual(markets, {"AAPL": "us", "600000": "cn"})
        self.assertEqual([r["code"] for r in res["rejected"]], ["600001"],
                         "同样的 4000 万成交额在 A 股口径下不合格")
        self.assertIn("流动性不足", res["rejected"][0]["reasons"][0])
        aapl = [c for c in res["candidates"] if c["code"] == "AAPL"][0]
        self.assertIsNone(aapl["metrics"]["turnover"])
        self.assertTrue(any("turnover" in w for w in aapl["warnings"]))
        self.assertNotIn("追高", " ".join(aapl["warnings"]), "美股 8.5% 未超其 10% 上限")

    def test_custom_params_take_effect(self):
        rows = [scan_row("A")]
        bars = {"A": mild_bars()}
        self.assertEqual(scan_candidates(rows, bars, {"minVolumeRatio": 3.0})
                         ["stats"]["byRejectReason"], {"量能不足": 1})
        self.assertEqual(scan_candidates(rows, bars, {"maxChangePct": 0.5})
                         ["stats"]["byRejectReason"], {"反追高": 1})
        self.assertEqual(scan_candidates(rows, bars, {"minAmount": 1e10})
                         ["stats"]["byRejectReason"], {"流动性不足": 1})
        self.assertEqual(scan_candidates(rows, bars, {"minAmountRatio": 0.5,
                                                      "minVolumeRatio": 0.5})
                         ["stats"]["passed"], 1, "放开门槛后应通过")

    def test_custom_risk_threshold_can_reject_earlier(self):
        """风险阈值可下调到 AVOID 线（且不得低于 avoid 阈值，避免语义矛盾）。"""
        rows = [scan_row("A", volume_ratio=2.5)]      # 量比 2.5 → 风险分 1
        bars = {"A": mild_bars()}
        res = scan_candidates(rows, bars, {"rejectRiskScore": 1, "avoidRiskScore": 1})
        self.assertEqual(res["stats"]["byRejectReason"], {"风险分超阈值": 1})
        self.assertEqual(res["params"]["reject_risk_score"], 1)
        loose = scan_candidates(rows, bars, {"rejectRiskScore": 1})
        self.assertEqual(loose["params"]["reject_risk_score"], 5, "不得低于 avoid 阈值")
        self.assertEqual(loose["stats"]["passed"], 1)

    def test_effective_params_and_note_are_returned(self):
        rows = [scan_row("A")]
        res = scan_candidates(rows, {"A": mild_bars()},
                              {"minAmount": {"cn": 5e8, "us": 1e7}, "maxChangePct": 5.0,
                               "weights": EQUAL_GROUPS})
        p = res["params"]
        self.assertEqual(p["min_amount"], {"cn": 5e8, "us": 1e7})
        self.assertEqual(p["max_change_pct"], {"cn": 5.0, "us": 5.0})
        self.assertEqual(p["weights"]["trend"], 0.125)
        self.assertIn("5.00 亿", res["note"])
        self.assertIn("不构成任何投资建议", res["note"])
        self.assertIn("短期反转", res["note"])
        json_ok(p)

    def test_candidate_payload_shape(self):
        rows = [scan_row("600000")]
        res = scan_candidates(rows, {"600000": mild_bars()})
        cand = res["candidates"][0]
        for key in ("code", "name", "price", "changePct", "score", "grade", "verdict",
                    "factors", "reasons", "warnings", "risk", "metrics", "spark"):
            self.assertIn(key, cand)
        self.assertLessEqual(len(cand["spark"]), 30, "迷你走势图不超过 30 个点")
        closes = [round(b["close"], 4) for b in mild_bars()][-30:]
        self.assertEqual(cand["spark"], closes)
        self.assertEqual(cand["spark"][-1], cand["metrics"]["price"])
        self.assertEqual(cand["verdict3"], VERDICT_BUCKETS[cand["grade"]])
        self.assertEqual(sorted(cand["risk"]["flags"], key=str), cand["risk"]["flags"])

    def test_empty_and_dirty_inputs(self):
        for rows, bmap in (([], None), (None, None), (None, {}), ([], {}), ("x", {})):
            res = scan_candidates(rows, bmap)
            self.assertTrue(res["ok"])
            self.assertEqual(res["candidates"], [])
            self.assertEqual(res["stats"]["scanned"], 0)
            self.assertEqual(res["stats"]["rejected"], 0)
            self.assertIn("不构成任何投资建议", res["note"])
            json_ok(res)

    def test_dirty_rows_are_counted_not_crashing(self):
        rows = [None, "x", 42, {}, {"name": "无代码"}]
        res = scan_candidates(rows, None)
        self.assertEqual(res["stats"]["scanned"], 5)
        self.assertEqual(res["stats"]["rejected"], 5)
        self.assertEqual(res["stats"]["byRejectReason"], {"数据非法": 3, "缺少代码": 2})
        self.assertEqual(res["candidates"], [])
        json_ok(res)

    def test_numeric_strings_are_accepted(self):
        rows = [{"code": "600000", "name": "字符串数值", "market": "cn", "price": "12.4",
                 "changePct": "1.0", "amount": "300000000", "volumeRatio": "1.8",
                 "turnover": "3.0"}]
        res = scan_candidates(rows, {"600000": mild_bars()})
        self.assertEqual(len(res["candidates"]), 1)
        self.assertEqual(res["candidates"][0]["score"], 89)

    def test_lot_field_and_market_differences_are_safe(self):
        """scanner 不做仓位分配（lot 由 core/kelly.py 负责），但必须容忍 lot 字段。"""
        rows = [scan_row("600000", market="cn", lot=100), scan_row("AAPL", market="us",
                                                                  lot=1, amount=4e7,
                                                                  change_pct=8.5)]
        bars = {r["code"]: mild_bars() for r in rows}
        res = scan_candidates(rows, bars)
        self.assertEqual({c["code"]: c["market"] for c in res["candidates"]},
                         {"600000": "cn", "AAPL": "us"})
        self.assertEqual(res["params"]["min_amount"]["cn"], 2e8)
        self.assertEqual(res["params"]["min_amount"]["us"], 3e7)
        json_ok(res)

    def test_plain_close_series_is_accepted(self):
        """K 线允许只给收盘价（其余 OHLC 用收盘价补齐），便于轻量上游接入。"""
        closes = alt_closes(90)
        rows = [scan_row("600000")]
        res = scan_candidates(rows, {"600000": closes})
        self.assertEqual(len(res["candidates"]), 1)
        self.assertEqual(res["candidates"][0]["metrics"]["bars"], 90)
        self.assertEqual(res["candidates"][0]["metrics"]["distToResistance"], 0.0)

    def test_full_result_is_strict_json(self):
        rows = [scan_row("OK"), scan_row("NOBARS"), scan_row("CHEAP", amount=float("nan")),
                scan_row("CHASE", change_pct="9.9"), scan_row("RISKY", volume_ratio=6.0,
                                                              chg60d=160.0)]
        bars = {"OK": mild_bars(), "CHEAP": mild_bars(), "CHASE": mild_bars(),
                "RISKY": flat_bars()}
        res = scan_candidates(rows, bars, limit=1)
        text = json_ok(res)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertIn("candidates", json.loads(text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
