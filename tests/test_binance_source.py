# -*- coding: utf-8 -*-
"""币安代币化美股（bStocks）数据源的回归测试（离线：全部走桩，不访问网络）。

背景：用户提出「增加美股的选项，币安现在支持美股」。核实后币安确实有两类美股能力，
本项目选的是**公开行情**那一路（无需密钥、不碰真实下单）：
  · bStocks 是 1:1 追踪标的的代币化美股，在币安现货以 AAPLBUSDT 这类交易对交易；
  · 7×24 连续成交 —— 美股常规时段休市、盘后与周末仍能看到价格（实测周六凌晨仍在成交）；
  · 公开接口：/ticker/24hr、/klines、/depth、/ping，本项目用零依赖的 urllib 直连。

这条源与常规美股源（腾讯 / 东财）**口径不同**，测试重点因此有三块：
  1) 符号映射与可用性：没有交易对时要给出可读结论，不能与网络故障混为一谈；
  2) 解析：滚动 24 小时行情、K 线字段顺序、UTC 换日的日线 vs 北京时间的分钟线、
     碎股（数量 < 1 股）不能显示成 0；
  3) 边界：A股 不许用这条源、周期不支持要报错、批量里单只缺失不能让整批失败。
"""

import io
import json
import os
import sys
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def _no_cache(key, ttl, producer, allow_stale=True):
    """绕过 TTL 缓存，保证每次断言都真的走一遍解析逻辑"""
    return producer()


def ticker24h(**over):
    row = {
        "symbol": "AAPLBUSDT", "priceChange": "-1.56", "priceChangePercent": "-0.464",
        "weightedAvgPrice": "336.51", "prevClosePrice": "336.02", "lastPrice": "334.51",
        "bidPrice": "334.56", "bidQty": "0.029", "askPrice": "334.59", "askQty": "1.644",
        "openPrice": "336.07", "highPrice": "338.96", "lowPrice": "332.79",
        "volume": "6232.67", "quoteVolume": "2097379.60",
    }
    row.update(over)
    return row


def klines(n=3, step_ms=86400000, start_ms=1789689600000, price="334.51"):
    return [[start_ms + i * step_ms, "332.18", "335.59", "331.02", price, "3034.09",
             start_ms + i * step_ms + step_ms - 1, "1009416.65", 8623,
             "1766.41", "587699.50", "0"] for i in range(n)]


class StubGet:
    """按 URL 关键字派发；记录调用顺序"""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(url)
        for key, val in self.rules:
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise RuntimeError("桩未覆盖的地址: %s" % url[:80])


class TestPairMapping(unittest.TestCase):
    def test_pair(self):
        self.assertEqual(server.bs_pair("AAPL"), "AAPLBUSDT")
        self.assertEqual(server.bs_pair("nvda"), "NVDABUSDT")
        self.assertEqual(server.bs_pair("BRK.B"), "BRKBBUSDT")
        self.assertEqual(server.bs_pair("BRK-B"), "BRKBBUSDT")

    def test_rejects_unusable_codes(self):
        self.assertIsNone(server.bs_pair(""))
        self.assertIsNone(server.bs_pair(None))
        self.assertIsNone(server.bs_pair("600519"))            # A 股代码不是美股
        self.assertIsNone(server.bs_pair("TOOLONGSYMBOL123"))


class TestTimeBasis(unittest.TestCase):
    """日线按 UTC 换日、分钟线用北京时间：这个口径差异必须可断言"""

    def test_daily_uses_utc_date(self):
        ms = 1789689600000                      # 2026-09-18 00:00 UTC（北京时间同日 08:00）
        self.assertEqual(server.bs_time(ms, False), "2026-09-18")
        self.assertEqual(server.bs_time(ms, False),
                         time.strftime("%Y-%m-%d", time.gmtime(ms // 1000)))

    def test_intraday_uses_local_time(self):
        ms = 1789754400000                      # 2026-09-19 18:00 UTC = 次日 02:00 北京
        self.assertEqual(server.bs_time(ms, True),
                         time.strftime("%Y-%m-%d %H:%M", time.localtime(ms // 1000)))
        self.assertEqual(server.bs_time(ms, True).split(" ")[1], "02:00")

    def test_bad_input(self):
        self.assertEqual(server.bs_time(None, False), "")
        self.assertEqual(server.bs_time("x", True), "")


class TestQuote(unittest.TestCase):
    def test_parses_rolling_24h_window(self):
        stub = StubGet([("/ticker/24hr", ticker24h())])
        with mock.patch.object(server, "http_get", stub), \
                mock.patch.object(server, "cached", _no_cache):
            q = server.bs_quote_row("AAPL")
        self.assertEqual(q["price"], 334.51)
        self.assertEqual(q["changePct"], -0.464)          # 滚动 24 小时涨跌幅
        self.assertEqual(q["open"], 336.07)
        self.assertEqual(q["volume"], 6232.67)
        self.assertEqual(q["amount"], 2097379.60)
        self.assertEqual(q["currency"], "USDT")
        self.assertEqual(q["window"], "24h")              # 界面据此改标签
        self.assertEqual(q["session"], "7x24")
        self.assertEqual(q["bids"][0], {"price": 334.56, "volume": 0.029})
        self.assertEqual(q["asks"][0], {"price": 334.59, "volume": 1.644})
        self.assertEqual(q["source"], server.BS_LABEL)
        self.assertIn("AAPLBUSDT", stub.calls[0])

    def test_missing_fields_stay_none(self):
        """币安没有市值 / 估值 / 换手：一律 None，不许编"""
        stub = StubGet([("/ticker/24hr", {"symbol": "AAPLBUSDT", "lastPrice": "100.0"})])
        with mock.patch.object(server, "http_get", stub), \
                mock.patch.object(server, "cached", _no_cache):
            q = server.bs_quote_row("AAPL")
        for k in ("marketCap", "pe", "peTtm", "pb", "turnover", "volumeRatio",
                  "amplitude", "limitUp", "limitDown", "week52High"):
            self.assertIsNone(q[k], k)
        self.assertEqual(q["bids"], [])
        self.assertEqual(q["asks"], [])

    def test_no_pair_is_explicit(self):
        stub = StubGet([("/ticker/24hr", {"code": -1121, "msg": "Invalid symbol."})])
        with mock.patch.object(server, "http_get", stub), \
                mock.patch.object(server, "cached", _no_cache):
            with self.assertRaises(RuntimeError) as ctx:
                server.bs_quote_row("ZZZZ")
        msg = str(ctx.exception)
        self.assertIn("暂无币安代币化美股交易对", msg)
        self.assertIn("ZZZZBUSDT", msg)

    def test_batch_skips_unusable_symbols(self):
        def fake(url, **kw):
            if "AAPLBUSDT" in url:
                return ticker24h()
            return {"code": -1121, "msg": "Invalid symbol."}
        with mock.patch.object(server, "http_get", fake), \
                mock.patch.object(server, "cached", _no_cache):
            rows = server.bs_quotes(["AAPL", "ZZZZ"])
        self.assertEqual([r["code"] for r in rows], ["AAPL"])

    def test_missing_codes_only_reports_definitive_absence(self):
        def fake(url, **kw):
            if "ZZZZBUSDT" in url:
                return {"code": -1121, "msg": "Invalid symbol."}
            if "NVDABUSDT" in url:
                raise RuntimeError("上游请求失败: 连接被重置")
            return {"symbol": "AAPLBUSDT", "price": "1"}
        with mock.patch.object(server, "http_get", fake):
            miss = server.bs_missing_codes(["AAPL", "ZZZZ", "NVDA"])
        self.assertEqual(miss, ["ZZZZ"])          # 网络问题不算「不存在」，不误报


class TestKline(unittest.TestCase):
    def test_field_order_and_units(self):
        stub = StubGet([("/klines", klines(1))])
        with mock.patch.object(server, "http_get", stub):
            bars, src = server.bs_kline("AAPL", "day", 10)
        b = bars[0]
        self.assertEqual((b["open"], b["high"], b["low"], b["close"]), (332.18, 335.59, 331.02, 334.51))
        self.assertEqual(b["volume"], 3034.09)          # 股
        self.assertEqual(b["amount"], 1009416.65)       # USDT 成交额
        self.assertEqual(b["t"], "2026-09-18")          # UTC 日
        self.assertEqual(src, server.BS_LABEL)

    def test_interval_mapping(self):
        for period, iv in (("day", "1d"), ("week", "1w"), ("month", "1M"),
                           ("5m", "5m"), ("15m", "15m"), ("30m", "30m"), ("60m", "1h")):
            stub = StubGet([("/klines", klines(1))])
            with mock.patch.object(server, "http_get", stub):
                server.bs_kline("AAPL", period, 10)
            self.assertIn("interval=%s" % iv, stub.calls[0], period)

    def test_unsupported_period(self):
        with self.assertRaises(RuntimeError):
            server.bs_kline("AAPL", "45m", 10)

    def test_limit_clamped_to_upstream_cap(self):
        stub = StubGet([("/klines", klines(1))])
        with mock.patch.object(server, "http_get", stub):
            server.bs_kline("AAPL", "day", 99999)
        self.assertIn("limit=1000", stub.calls[0])

    def test_empty_kline_raises(self):
        stub = StubGet([("/klines", [])])
        with mock.patch.object(server, "http_get", stub):
            with self.assertRaises(RuntimeError) as ctx:
                server.bs_kline("AAPL", "day", 10)
        self.assertIn("币安K线为空", str(ctx.exception))

    def test_intraday_time_has_clock(self):
        stub = StubGet([("/klines", klines(1, step_ms=300000))])
        with mock.patch.object(server, "http_get", stub):
            bars, _ = server.bs_kline("AAPL", "30m", 10)
        self.assertEqual(len(bars[0]["t"]), 16)          # YYYY-MM-DD HH:MM
        self.assertIn(":", bars[0]["t"])


class TestTrends(unittest.TestCase):
    def test_points_and_baseline(self):
        five_m = klines(4, step_ms=300000, price="334.51")
        daily = klines(2, step_ms=86400000, price="333.00")
        stub = StubGet([("interval=5m", five_m), ("/klines", daily)])
        with mock.patch.object(server, "http_get", stub):
            res = server.bs_trends("AAPL", 24)
        self.assertEqual(len(res["points"]), 4)
        self.assertEqual(res["points"][0]["price"], 334.51)
        self.assertEqual(res["points"][0]["avg"], 1009416.65 / 3034.09)   # 累计 VWAP
        self.assertEqual(res["prevClose"], 333.00)       # 上一 UTC 日收盘
        self.assertEqual(res["window"], "24h")
        self.assertEqual(res["baseline"], "上一 UTC 日收盘")
        self.assertIn("limit=288", [u for u in stub.calls if "interval=5m" in u][0])

    def test_baseline_absent_when_history_insufficient(self):
        stub = StubGet([("interval=5m", klines(1, step_ms=300000)),
                        ("/klines", klines(1))])
        with mock.patch.object(server, "http_get", stub):
            res = server.bs_trends("AAPL", 24)
        self.assertIsNone(res["prevClose"])

    def test_hours_parameter(self):
        stub = StubGet([("interval=5m", klines(1, step_ms=300000)), ("/klines", klines(2))])
        with mock.patch.object(server, "http_get", stub):
            server.bs_trends("AAPL", 6)
        self.assertIn("limit=72", [u for u in stub.calls if "interval=5m" in u][0])


class TestDepth(unittest.TestCase):
    def test_five_levels(self):
        payload = {"bids": [["334.55", "0.039"], ["334.53", "0.029"]],
                   "asks": [["334.59", "0.046"]]}
        stub = StubGet([("/depth", payload)])
        with mock.patch.object(server, "http_get", stub):
            d = server.bs_depth("AAPL", 5)
        self.assertTrue(d["supported"])
        self.assertEqual(d["bids"][0], {"price": 334.55, "volume": 0.039})
        self.assertEqual(len(d["asks"]), 1)
        self.assertEqual(d["source"], server.BS_LABEL)
        self.assertIn("limit=5", stub.calls[0])


class TestSourceInfo(unittest.TestCase):
    def test_lists_both_sources_with_notes(self):
        stub = StubGet([("/ping", {})])
        with mock.patch.object(server, "http_get", stub):
            info = server.bs_source_info()
        vals = [s["value"] for s in info["sources"]]
        self.assertEqual(vals, ["", "binance"])
        bs = info["sources"][1]
        self.assertTrue(bs["available"])
        self.assertTrue(bs["sevenBy24"])
        joined = "；".join(bs["notes"])
        self.assertIn("24 小时", joined)
        self.assertIn("UTC", joined)
        self.assertIn("50~100", joined)          # 历史短的真相必须写出来

    def test_unavailable_when_binance_blocked(self):
        stub = StubGet([("/ping", RuntimeError("币安行情接口不可用（2 个域名均失败）"))])
        with mock.patch.object(server, "http_get", stub):
            info = server.bs_source_info()
        bs = info["sources"][1]
        self.assertFalse(bs["available"])
        self.assertIn("币安", bs["error"])


class TestApiLayer(unittest.TestCase):
    def test_kline_binance_branch(self):
        stub = StubGet([("/klines", klines(2))])
        with mock.patch.object(server, "http_get", stub), \
                mock.patch.object(server, "cached", _no_cache):
            res = server.api_kline("us", "AAPL", "day", 1, 320, "binance")
        self.assertEqual(len(res["bars"]), 2)
        self.assertEqual(res["source"], server.BS_LABEL)
        self.assertTrue(res["sevenBy24"])
        self.assertIn("没有复权口径", res["fqNote"])
        self.assertIn("UTC", "；".join(res["chartNotes"]))

    def test_trends_binance_branch(self):
        stub = StubGet([("interval=5m", klines(2, step_ms=300000)), ("/klines", klines(2))])
        with mock.patch.object(server, "http_get", stub), \
                mock.patch.object(server, "cached", _no_cache):
            res = server.api_trends("us", "AAPL", 1, "binance")
        self.assertEqual(len(res["points"]), 2)
        self.assertEqual(res["window"], "24h")

    def test_stock_binance_branch_carries_notes(self):
        stub = StubGet([("/ticker/24hr", ticker24h())])
        with mock.patch.object(server, "http_get", stub), \
                mock.patch.object(server, "cached", _no_cache):
            q = server.api_stock("us", "AAPL", "binance")
        self.assertEqual(q["window"], "24h")
        self.assertIsNone(q["fundflow"])          # 币安没有资金流，不许拿别的源顶上
        self.assertEqual(len(q["notes"]), len(server.BS_NOTES))

    def test_rejects_non_us_market(self):
        for fn in (lambda: server.api_kline("cn", "600519", "day", 1, 320, "binance"),
                   lambda: server.api_trends("cn", "600519", 1, "binance"),
                   lambda: server.quotes("cn", ["600519"], "binance")):
            with self.assertRaises(RuntimeError) as ctx:
                fn()
            self.assertIn("只适用于美股", str(ctx.exception))

    def test_default_source_is_untouched(self):
        """不带 source 时仍走原来的多源链路（这里让它必然失败以确认不是币安分支）"""
        def boom(*a, **k):
            raise RuntimeError("模拟：常规源全部失败")
        with mock.patch.object(server, "tx_kline", boom), \
                mock.patch.object(server, "em_kline", boom), \
                mock.patch.object(server, "sina_kline", boom), \
                mock.patch.object(server, "cached", _no_cache):
            res = server.api_kline("us", "AAPL", "day", 1, 320)
        self.assertEqual(res["bars"], [])
        self.assertNotIn("币安", res["error"])

    def test_cache_key_includes_source(self):
        """同标的同周期，两个源的缓存不能互相命中"""
        stub = StubGet([("/ticker/24hr", ticker24h()), ("/klines", klines(3))])
        with mock.patch.object(server, "tx_kline", lambda *a: ([(1,)] * 3, "腾讯行情")), \
                mock.patch.object(server, "http_get", stub):
            a = server.api_kline("us", "AAPL", "day", 1, 320)
            b = server.api_kline("us", "AAPL", "day", 1, 320, "binance")
        self.assertEqual(a["source"], "腾讯行情")
        self.assertEqual(b["source"], server.BS_LABEL)
        self.assertEqual(len(a["bars"]), 3)
        self.assertEqual(len(b["bars"]), 3)


class TestSoftHttpError(unittest.TestCase):
    """币安用 400 + JSON 说明「交易对不存在」：要拿到结论，而不是当成网络故障重试"""

    def _http_error(self, body):
        return urllib.error.HTTPError("https://x/y", 400, "Bad Request", {},
                                      io.BytesIO(body.encode("utf-8")))

    def test_soft_mode_returns_error_body(self):
        err = self._http_error(json.dumps({"code": -1121, "msg": "Invalid symbol."}))
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=err), \
                mock.patch.object(server, "ensure_ssl", lambda: None), \
                mock.patch.object(server.time, "sleep", lambda *_: None):
            out = server.http_get("https://x/y", retry=0, soft_http_error=True)
        self.assertEqual(out["code"], -1121)

    def test_default_mode_still_raises(self):
        err = self._http_error(json.dumps({"code": -1121, "msg": "Invalid symbol."}))
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=err), \
                mock.patch.object(server, "ensure_ssl", lambda: None), \
                mock.patch.object(server.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as ctx:
                server.http_get("https://x/y", retry=0)
        self.assertIn("上游请求失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
