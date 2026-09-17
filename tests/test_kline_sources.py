# -*- coding: utf-8 -*-
"""K 线多源与复权口径标注的回归测试（离线：全部走桩，不访问网络）。

背景（2026-09 真实故障）：个股详情里 600667 显示
「图表加载失败：腾讯K线为空（600667）；东财接口不可用: 上游请求失败: https://push2his...」

排查结论：不是某一只票的问题。当时
  · 腾讯 A股日K 的 web.ifzq.gtimg.cn/appstock/app/fqkline/get 被腾讯 WAF 拦成 HTTP 501（返回 HTML 而非 JSON）；
  · 东财 push2his.eastmoney.com 直接断连（curl 报 Empty reply from server）；
两者同时不可用，于是**所有** A 股标的的日/周/月K 都拿不到——详情页 K 线、扫描器取样 K 线、
策略跟踪的历史窗口一起断掉；而原报错只留了一句「腾讯K线为空」，看不出是上游被拦还是该股没数据。

修复与本次锁定的行为：
  1) 腾讯日/周/月K 增加仍在服务的 App 域名（proxy.finance.qq.com/.../newfqkline），
     A 股优先走它、老域名兜底；美股反之（老的 usfqkline 正常）；
  2) 报错必须带来源名与上游真实原因（WAF 501 / 连接被拒 / 该周期无数据）；
  3) 新增新浪日/周/月K 作为**第三个独立来源**（腾讯与东财会同时挂）；
  4) 新浪源只有不复权价格、volume 单位是「股」，因此必须换算成「手」并在 fq≠0 时
     附上 fqActual / fqNote——绝不让「前复权」的标签配着不复权的价格。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


TX_APP = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
TX_LEGACY = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_HOST = "money.finance.sina.com.cn"


def _no_cache(key, ttl, producer, allow_stale=True):
    """绕过 TTL 缓存，保证每次断言都真的走一遍多源链路"""
    return producer()


def tx_payload(sym="sh600519", key="qfqday", n=4):
    bars = [["2026-09-%02d" % (11 + i), "10.00", "10.50", "10.80", "9.90", "1234.00"]
            for i in range(n)]
    return {"data": {sym: {key: bars}}}


def sina_payload(n=5, volume="3480142"):
    return [{"day": "2026-09-%02d" % (10 + i), "open": "10.000", "high": "10.800",
             "low": "9.900", "close": "10.500", "volume": volume} for i in range(n)]


class StubGet:
    """按 URL 关键字派发的 http_get 桩；记录调用顺序"""

    def __init__(self, rules):
        self.rules = rules            # [(关键字, 返回值 or Exception)]
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(url)
        for key, val in self.rules:
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise RuntimeError("桩未覆盖的地址: %s" % url[:80])


class TestTencentKlineSource(unittest.TestCase):
    def test_cn_day_prefers_app_endpoint(self):
        stub = StubGet([(TX_APP, tx_payload())])
        with mock.patch.object(server, "http_get", stub):
            bars, src = server.tx_kline("cn", "600519", "day", 1, 320)
        self.assertEqual(len(bars), 4)
        self.assertEqual(src, "腾讯行情")
        self.assertEqual(len(stub.calls), 1)
        self.assertIn(TX_APP, stub.calls[0])

    def test_legacy_host_used_when_app_endpoint_blocked(self):
        """腾讯 WAF 拦掉 App 域名时，老域名仍要能兜住"""
        stub = StubGet([(TX_APP, RuntimeError("上游请求失败: ... 501")),
                        (TX_LEGACY, tx_payload())])
        with mock.patch.object(server, "http_get", stub):
            bars, src = server.tx_kline("cn", "600519", "day", 1, 320)
        self.assertEqual(len(bars), 4)
        self.assertIn(TX_APP, stub.calls[0])
        self.assertIn(TX_LEGACY, stub.calls[-1])

    def test_waf_blocked_first_host_is_still_first_for_cn(self):
        """A 股顺序固定为「App 域名 → 老域名」，不因一次失败而反过来"""
        stub = StubGet([(TX_APP, tx_payload()), (TX_LEGACY, tx_payload())])
        with mock.patch.object(server, "http_get", stub):
            server.tx_kline("cn", "600519", "day", 1, 320)
        self.assertEqual([u.split("?")[0] for u in stub.calls],
                         [TX_APP.split("?")[0]])

    def test_us_keeps_legacy_first(self):
        """美股用老的 usfqkline（新的 newusfqkline 实测拿不到 bars）"""
        server.SYM_MEMO["AAPL"] = None          # 跳过全代码查询，避免多余请求
        stub = StubGet([("usfqkline", tx_payload(sym="usAAPL", key="qfqday"))])
        with mock.patch.object(server, "http_get", stub):
            bars, _src = server.tx_kline("us", "AAPL", "day", 1, 320)
        self.assertEqual(len(bars), 4)
        self.assertIn("web.ifzq.gtimg.cn/appstock/app/usfqkline/get", stub.calls[0])

    def test_error_message_keeps_upstream_reason(self):
        """只报「腾讯K线为空」无法区分 WAF 拦截与真的没数据——必须带原因"""
        stub = StubGet([("gtimg.cn", RuntimeError("上游请求失败: HTTP 501"))])
        with mock.patch.object(server, "http_get", stub):
            with self.assertRaises(RuntimeError) as ctx:
                server.tx_kline("cn", "600667", "day", 1, 320)
        msg = str(ctx.exception)
        self.assertIn("腾讯K线为空（600667）", msg)
        self.assertIn("501", msg)

    def test_limit_is_clamped_to_upstream_cap(self):
        """上游对 lmt 有上限（实测 1500 会被截成 640），统一钳到 1000"""
        stub = StubGet([(TX_APP, tx_payload())])
        with mock.patch.object(server, "http_get", stub):
            server.tx_kline("cn", "600519", "day", 1, 5000)
        self.assertIn(",1000,", stub.calls[0])

    def test_week_and_month_keys(self):
        for period, key in (("week", "qfqweek"), ("month", "hfqmonth")):
            fq = 2 if period == "month" else 1
            stub = StubGet([(TX_APP, tx_payload(sym="sh600667", key=key))])
            with mock.patch.object(server, "http_get", stub):
                bars, _src = server.tx_kline("cn", "600667", period, fq, 320)
            self.assertEqual(len(bars), 4, period)
            self.assertIn(",%s," % period, stub.calls[0])


class TestSinaKlineSource(unittest.TestCase):
    def test_volume_converted_from_shares_to_lots(self):
        stub = StubGet([(SINA_HOST, sina_payload(volume="3480142"))])
        with mock.patch.object(server, "http_get", stub):
            bars, src = server.sina_kline("cn", "600519", "day", 1, 320)
        self.assertEqual(bars[0]["volume"], 34801.42)
        self.assertIn("不复权", src)

    def test_scale_mapping(self):
        for period, scale in (("day", 240), ("week", 1200), ("month", 7200)):
            stub = StubGet([(SINA_HOST, sina_payload())])
            with mock.patch.object(server, "http_get", stub):
                server.sina_kline("cn", "600519", period, 0, 320)
            self.assertIn("scale=%d" % scale, stub.calls[0])

    def test_symbol_uses_exchange_prefix(self):
        stub = StubGet([(SINA_HOST, sina_payload())])
        with mock.patch.object(server, "http_get", stub):
            server.sina_kline("cn", "000001", "day", 0, 320)
        self.assertIn("symbol=sz000001", stub.calls[0])

    def test_limit_clamped(self):
        stub = StubGet([(SINA_HOST, sina_payload())])
        with mock.patch.object(server, "http_get", stub):
            server.sina_kline("cn", "600519", "day", 0, 99999)
        self.assertIn("datalen=1500", stub.calls[0])

    def test_rejects_us_market(self):
        with self.assertRaises(RuntimeError):
            server.sina_kline("us", "AAPL", "day", 1, 320)

    def test_rejects_unsupported_period(self):
        for period in ("5m", "15m", "60m", "1m"):
            with self.assertRaises(RuntimeError):
                server.sina_kline("cn", "600519", period, 1, 320)

    def test_empty_payload_raises(self):
        stub = StubGet([(SINA_HOST, [])])
        with mock.patch.object(server, "http_get", stub):
            with self.assertRaises(RuntimeError) as ctx:
                server.sina_kline("cn", "600519", "day", 1, 320)
        self.assertIn("新浪K线为空", str(ctx.exception))

    def test_dirty_rows_are_dropped(self):
        rows = sina_payload(3) + ["坏行", {"day": "2026-09-20"},
                                 {"day": "2026-09-21T00:00:00", "close": "11.000"}]
        stub = StubGet([(SINA_HOST, rows)])
        with mock.patch.object(server, "http_get", stub):
            bars, _src = server.sina_kline("cn", "600519", "day", 1, 320)
        # 「坏行」与缺少 close 的行被丢弃；带时间戳的日期截断成 10 位
        self.assertEqual(len(bars), 4)
        self.assertEqual(bars[-1]["t"], "2026-09-21")

    def test_too_few_rows_raises(self):
        stub = StubGet([(SINA_HOST, sina_payload(2))])
        with mock.patch.object(server, "http_get", stub):
            with self.assertRaises(RuntimeError) as ctx:
                server.sina_kline("cn", "600519", "day", 1, 320)
        self.assertIn("解析为空", str(ctx.exception))


class TestApiKlineFallback(unittest.TestCase):
    def _call(self, market="cn", code="600667", period="day", fq=1, limit=320):
        with mock.patch.object(server, "cached", _no_cache):
            return server.api_kline(market, code, period, fq, limit)

    def test_tencent_has_no_fq_note(self):
        """腾讯是复权源，不该出现口径说明"""
        with mock.patch.object(server, "tx_kline", lambda *a: ([(1,)] * 5, "腾讯行情")), \
                mock.patch.object(server, "http_get", StubGet([(TX_APP, tx_payload())])):
            res = self._call()
        self.assertEqual(res["source"], "腾讯行情")
        self.assertNotIn("fqNote", res)

    def test_falls_back_to_sina_when_tencent_and_eastmoney_both_down(self):
        """本次故障的核心场景：两个源同时不可用时仍要出图，并如实标注口径"""
        def boom(*a, **k):
            raise RuntimeError("模拟：上游请求失败（WAF 501）")
        with mock.patch.object(server, "tx_kline", boom), \
                mock.patch.object(server, "em_kline", boom), \
                mock.patch.object(server, "http_get", StubGet([(SINA_HOST, sina_payload())])):
            res = self._call(fq=1)
        self.assertEqual(res["source"], "新浪财经（不复权）")
        self.assertEqual(len(res["bars"]), 5)
        self.assertEqual(res["fqActual"], 0)
        self.assertIn("不复权", res["fqNote"])
        self.assertIn("前复权", res["fqNote"])

    def test_no_note_when_unadjusted_was_requested(self):
        def boom(*a, **k):
            raise RuntimeError("模拟：上游挂掉")
        with mock.patch.object(server, "tx_kline", boom), \
                mock.patch.object(server, "em_kline", boom), \
                mock.patch.object(server, "http_get", StubGet([(SINA_HOST, sina_payload())])):
            res = self._call(fq=0)
        self.assertEqual(res["source"], "新浪财经（不复权）")
        self.assertNotIn("fqNote", res)
        self.assertNotIn("fqActual", res)

    def test_sina_not_used_for_us_or_minute(self):
        """新浪只覆盖 A 股日/周/月：美股与分钟线不许出现「新浪」这个来源名"""
        def boom(*a, **k):
            raise RuntimeError("模拟：上游挂掉")
        for market, period in (("us", "day"), ("cn", "5m"), ("cn", "60m")):
            with mock.patch.object(server, "tx_kline", boom), \
                    mock.patch.object(server, "em_kline", boom), \
                    mock.patch.object(server, "api_trends", boom):
                res = self._call(market=market, period=period)
            self.assertEqual(res["bars"], [])
            self.assertNotIn("新浪", res["error"], "%s %s" % (market, period))

    def test_error_message_names_every_source(self):
        def boom(*a, **k):
            raise RuntimeError("模拟：上游挂掉")
        with mock.patch.object(server, "tx_kline", boom), \
                mock.patch.object(server, "em_kline", boom), \
                mock.patch.object(server, "sina_kline", boom):
            res = self._call()
        self.assertEqual(res["bars"], [])
        for name in ("腾讯：", "东财：", "新浪："):
            self.assertIn(name, res["error"])
        self.assertIn("上游挂掉", res["error"])

    def test_first_success_wins_and_order_is_tencent_eastmoney_sina(self):
        calls = []

        def tx(*a):
            calls.append("tx")
            raise RuntimeError("腾讯挂")

        def em(*a):
            calls.append("em")
            raise RuntimeError("东财挂")

        def sina(*a):
            calls.append("sina")
            return [(1,)] * 5, "新浪财经（不复权）"

        with mock.patch.object(server, "tx_kline", tx), \
                mock.patch.object(server, "em_kline", em), \
                mock.patch.object(server, "sina_kline", sina):
            res = self._call()
        self.assertEqual(calls, ["tx", "em", "sina"])
        self.assertEqual(res["source"], "新浪财经（不复权）")

    def test_tencent_success_short_circuits(self):
        calls = []

        def tx(*a):
            calls.append("tx")
            return [(1,)] * 5, "腾讯行情"

        def em(*a):
            calls.append("em")
            return [(1,)] * 5, "东方财富"

        with mock.patch.object(server, "tx_kline", tx), \
                mock.patch.object(server, "em_kline", em):
            res = self._call()
        self.assertEqual(calls, ["tx"])
        self.assertEqual(res["source"], "腾讯行情")

    def test_fq_note_helper(self):
        self.assertIsNone(server.fq_note_of("腾讯行情", 1))
        self.assertIsNone(server.fq_note_of("新浪财经（不复权）", 0))
        self.assertIn("后复权", server.fq_note_of("新浪财经（不复权）", 2))


if __name__ == "__main__":
    unittest.main()
