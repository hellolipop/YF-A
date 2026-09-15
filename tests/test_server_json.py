# -*- coding: utf-8 -*-
"""服务端 JSON 出口的回归测试。

背景：绩效指标里 profit_factor 在「无亏损交易」时是 inf，Python 的
json.dumps 会输出 Infinity 字面量，但这不是合法 JSON。浏览器 JSON.parse
会直接抛错（Safari 的文案是 "The string did not match the expected pattern."），
表现为前端「策略任务获取失败」这类难以定位的报错。所以在出口统一清洗。
"""

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def strict_loads(text):
    """严格解析：把 Infinity / NaN 视为错误（与浏览器行为一致）"""
    def bad(value):
        raise ValueError("非有限数值字面量: %s" % value)
    return json.loads(text, parse_constant=bad)


class TestSanitizeJson(unittest.TestCase):
    def test_inf_to_none(self):
        self.assertIsNone(server.sanitize_json(float("inf")))
        self.assertIsNone(server.sanitize_json(float("-inf")))
        self.assertIsNone(server.sanitize_json(float("nan")))

    def test_finite_untouched(self):
        for v in (0.0, -1.5, 3, 1e308, -0.0):
            self.assertEqual(server.sanitize_json(v), v)

    def test_nested(self):
        data = {
            "rows": [
                {"code": "600667", "stats": {"profitFactor": float("inf"), "sharpe": 2.43}},
                {"code": "000811", "stats": {"profitFactor": 1.89, "var95": float("nan")}},
            ],
            "totals": {"returnPct": 6.72, "x": [float("-inf"), 1.0]},
            "ok": True,
            "name": "太极实业",
            "none": None,
        }
        out = server.sanitize_json(data)
        self.assertIsNone(out["rows"][0]["stats"]["profitFactor"])
        self.assertEqual(out["rows"][0]["stats"]["sharpe"], 2.43)
        self.assertIsNone(out["rows"][1]["stats"]["var95"])
        self.assertIsNone(out["totals"]["x"][0])
        self.assertEqual(out["totals"]["returnPct"], 6.72)
        self.assertTrue(out["ok"])
        self.assertEqual(out["name"], "太极实业")
        self.assertIsNone(out["none"])

    def test_serializable_as_strict_json(self):
        payload = {"profitFactor": float("inf"), "nested": [{"v": float("nan")}]}
        text = json.dumps(server.sanitize_json(payload), ensure_ascii=False, allow_nan=False)
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)
        parsed = strict_loads(text)          # 模拟浏览器 JSON.parse 的严格性
        self.assertIsNone(parsed["profitFactor"])
        self.assertIsNone(parsed["nested"][0]["v"])

    def test_original_payload_would_fail_browsers(self):
        """反证：未清洗时确实是非法 JSON（说明这个清洗不是多余的）"""
        text = json.dumps({"profitFactor": float("inf")})
        self.assertIn("Infinity", text)
        with self.assertRaises(ValueError):
            strict_loads(text)

    def test_bytes_and_other_types_pass_through(self):
        class Weird:
            pass

        w = Weird()
        out = server.sanitize_json({"a": w, "b": b"x", "c": (1, 2)})
        self.assertIs(out["a"], w)
        self.assertEqual(out["b"], b"x")
        self.assertEqual(out["c"], [1, 2])


class TestRunnerStatsAreStrict(unittest.TestCase):
    """集成层：跟踪/回测统计里允许出现 inf，但必须能被严格 JSON 序列化"""

    def test_stats_payload_is_strict_json(self):
        from core import metrics as M

        # 构造「全是盈利交易」的场景：profit_factor 必然为 inf
        equity = [{"t": "2026-01-%02d" % (i + 1), "v": 100000 * (1 + 0.01 * i), "close": 10 + i}
                  for i in range(20)]
        trades = [{"pnl": 1200.0, "pnlPct": 1.2, "bars": 3, "inDate": "2026-01-02",
                   "outDate": "2026-01-05", "fee": 6.0, "slippage": 4.0}]
        stats = M.summarize(equity, trades, 100000)
        self.assertEqual(stats["profit_factor"], float("inf"))

        text = json.dumps(server.sanitize_json(stats), ensure_ascii=False, allow_nan=False)
        parsed = strict_loads(text)
        self.assertIsNone(parsed["profit_factor"])
        self.assertEqual(parsed["trades"], 1)


if __name__ == "__main__":
    unittest.main()
