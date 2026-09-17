# -*- coding: utf-8 -*-
"""交易计划的方向一致性（tests/test_plan_direction.py）

起因是一个真实的误读：个股详情里冰轮环境（000811）显示「建议买入 38.66」与「止损 42.04」，
**止损价高于买入价**。查下来不是点位算错，而是**语义没讲清**：

档位为 减仓 / 卖出 / 回避 时，服务端给的其实是**离场计划** —— 参考价取现价，
上方那个价是「涨破则离场判断失效」，下方两个价是「下行参考目标」。这套几何自洽，
但字段名沿用 `entry / stop / target1 / target2`，界面又按买入计划的标签渲染，
于是用户看到的是一个自相矛盾的买入计划。

修复做法：服务端在计划里补 `side / tradeable / inverted / labels`，把方位与中文标签
显式给出；界面（`detail.js` 的计划区块、`chart.js` 的叠加线）按方向渲染。

本文件把三件事钉住，防止这类「标签与方向不一致」的误读复发：
1. 做多计划的价位顺序必须是 止损 < 入场 < 目标1 < 目标2；
2. 离场计划的价位顺序必须**反向**，且 `tradeable=False` 且 `labels` 不得把参考价叫成「建议买入」；
3. `direction` 这个标记本身必须与价位几何一致（不许出现「标记说是离场、价位却是做多」）。
"""

import sys
import unittest

sys.path.insert(0, ".")

from core import advisor as A  # noqa: E402

#: 档位全集（与 core/advisor 的档位定义保持一致；新增档位时这里必须同步，
#: 否则「每个档位都有明确方向与标签」这条断言会失败 —— 这正是想要的提醒）
LONG_ACTIONS = ("buy", "hold", "watch")
EXIT_ACTIONS = ("reduce", "sell", "avoid")


def _fc(last=100.0):
    """合成条件分布：分位数是**小数**（-0.05 = -5%），与 core/forecast 的口径一致"""
    return {"sample": 60, "degraded": False, "lastClose": last,
            "quantiles": {"5": -0.15, "25": -0.05, "50": 0.03, "75": 0.14, "95": 0.22}}


class PlanDirectionCase(unittest.TestCase):
    def plan(self, action, price=100.0, atr=2.0, fc=None):
        return A._plan(price, atr, action, fc if fc is not None else _fc(price), "cn")


class TestLongPlan(PlanDirectionCase):
    def test_prices_are_ordered_for_a_long(self):
        """做多：止损必须在入场下方，目标必须在入场上方，且目标递增"""
        for act in LONG_ACTIONS:
            p = self.plan(act)
            self.assertEqual(p["direction"], "long", act)
            self.assertLess(p["stop"], p["entry"], "%s：止损必须低于入场" % act)
            self.assertGreater(p["target1"], p["entry"], "%s：目标1 必须高于入场" % act)
            self.assertGreater(p["target2"], p["target1"], "%s：目标位必须递增" % act)
            self.assertTrue(p["tradeable"], "%s：做多计划可据此建仓" % act)
            self.assertFalse(p["inverted"])

    def test_long_labels_say_buy_and_stop(self):
        p = self.plan("buy")
        self.assertEqual(p["labels"]["entry"], "建议买入")
        self.assertIn("止损", p["labels"]["stop"])
        self.assertIn("建仓计划", p["note"])
        self.assertNotIn("不新开仓位", p["note"])

    def test_risk_reward_matches_manual_math(self):
        p = self.plan("buy")
        manual = (p["target1"] - p["entry"]) / (p["entry"] - p["stop"])
        self.assertAlmostEqual(p["riskReward"], manual, delta=1e-3)

    def test_stop_respects_the_twelve_percent_cap(self):
        """高波动标的下止损不得宽于 -12%（避免一次止损吃掉过多本金）"""
        p = self.plan("buy", price=100.0, atr=9.0)
        self.assertGreaterEqual(p["stop"], 88.0 - 1e-9)
        self.assertAlmostEqual(p["stop"], 88.0, delta=0.01)


class TestExitPlan(PlanDirectionCase):
    def test_prices_are_inverted_for_an_exit(self):
        """离场：参考价取现价，上方是「失效价」，下方是下行目标（几何必须反向）"""
        for act in EXIT_ACTIONS:
            p = self.plan(act)
            self.assertEqual(p["direction"], "exit", act)
            self.assertGreater(p["stop"], p["entry"], "%s：离场失效价必须在参考价上方" % act)
            self.assertLess(p["target1"], p["entry"], "%s：下行目标必须在参考价下方" % act)
            self.assertLess(p["target2"], p["target1"], "%s：下行目标必须递减" % act)
            self.assertFalse(p["tradeable"], "%s：离场计划不得据此建仓" % act)
            self.assertTrue(p["inverted"], "%s：应标记为反向口径" % act)

    def test_exit_labels_never_call_the_reference_price_a_buy(self):
        """**核心回归**：离场计划里绝不能出现「建议买入」这类做多标签

        这正是用户看到的那个矛盾（「建议买入 38.66 / 止损 42.04」）。
        """
        for act in EXIT_ACTIONS:
            p = self.plan(act)
            joined = " ".join(str(v) for v in (p["labels"] or {}).values())
            self.assertNotIn("建议买入", joined, "%s：离场计划的标签不得叫建议买入" % act)
            self.assertIn("参考价", p["labels"]["entry"])
            self.assertIn("失效", p["labels"]["stop"])
            self.assertIn("下行", p["labels"]["target1"])
            self.assertIn("下行", p["labels"]["target2"])

    def test_exit_note_states_that_no_new_position_is_opened(self):
        for act in EXIT_ACTIONS:
            p = self.plan(act)
            self.assertIn("离场计划", p["note"], act)
            self.assertIn("不新开仓位", p["note"], act)
            self.assertIn("失效", p["note"], act)


class TestDirectionFlagMatchesGeometry(PlanDirectionCase):
    """方向标记与价位几何必须一致 —— 这类「标记与数据不符」是最容易被忽略的一类错误"""

    def test_flag_never_disagrees_with_geometry(self):
        for act in LONG_ACTIONS + EXIT_ACTIONS:
            for price, atr in ((100.0, 2.0), (8.55, 0.35), (1266.98, 42.0), (2.10, 0.09)):
                p = self.plan(act, price=price, atr=atr)
                above = p["stop"] > p["entry"]
                self.assertEqual(p["direction"] == "exit", above,
                                 "%s @ %.2f/atr=%.2f：strategy 标记与几何不一致" % (act, price, atr))
                # tradeable 只在做多时为真，且与 inverted 互补
                self.assertEqual(p["tradeable"], p["direction"] == "long")
                self.assertEqual(p["inverted"], p["direction"] == "exit")

    def test_every_action_has_direction_and_labels(self):
        for act in LONG_ACTIONS + EXIT_ACTIONS:
            p = self.plan(act)
            self.assertIn(p["direction"], ("long", "exit"), act)
            self.assertIsInstance(p["labels"], dict, act)
            for key in ("entry", "stop", "target1", "target2"):
                self.assertTrue(str(p["labels"].get(key) or "").strip(), "%s 缺标签 %s" % (act, key))
            self.assertTrue(str(p["side"] or "").strip(), "%s 缺 side" % act)


class TestDegradedPlan(PlanDirectionCase):
    def test_no_price_keeps_the_contract_complete(self):
        """无有效价格时必须给出完整契约字段（界面按 labels 渲染，缺字段会显示成 undefined）"""
        p = A._plan(None, None, "buy", {}, "cn")
        for key in ("entry", "stop", "target1", "target2", "direction",
                    "side", "tradeable", "inverted", "labels", "note"):
            self.assertIn(key, p, "降级计划缺字段：%s" % key)
        self.assertIsNone(p["entry"])
        self.assertIsNone(p["labels"])

    def test_dirty_inputs_do_not_raise(self):
        for price, atr, act, fc in [(0, 1, "buy", _fc()), (-1, 1, "sell", _fc()),
                                    (100.0, 0, "buy", _fc()), (100.0, None, "avoid", _fc()),
                                    (100.0, 2, "buy", {"quantiles": {"5": "x"}}),
                                    (100.0, 2, "", {}), (100.0, 2, "unknownAction", _fc())]:
            p = A._plan(price, atr, act, fc or {}, "cn")
            self.assertIsInstance(p, dict)
            self.assertIn("direction", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
