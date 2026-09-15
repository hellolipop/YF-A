#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
providers/features.py 的测试

两部分：
  1) OfflineTests —— 纯离线，用**实测抓取到的真实响应片段**做夹具，验证字段解析、
     单位缩放（涨停池 p÷1000）、时间格式化、方向映射、梯队分组、聚合逻辑、「空实现」形状；
  2) LiveTests    —— 真实联网校验四个接口（先探测网络，网络不可用时自动 skip，
     不会把「没网」误判为「逻辑错误」）。

运行：
    cd stock-terminal && python3 tests/test_features.py          # 或 python3 -m unittest discover tests
"""

import json
import os
import sys
import unittest
from unittest import mock

# 允许直接以脚本方式运行（tests/ 的上一级即 stock-terminal/）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from providers import features  # noqa: E402

# --------------------------------------------------------------------------- #
# 实测夹具（均取自 2026-09-15 真实响应，仅截取片段）
# --------------------------------------------------------------------------- #

# 东方财富分笔（600519）：2026-09-15 真机返回
EM_DETAILS_600519 = [
    "09:15:10,1277.96,1,0,4",     # 竞价委托快照（成交笔数 0）
    "09:24:52,1283.59,106,0,4",
    "09:24:58,1282.20,155,0,4",   # 撮合前最后一条委托快照
    "09:25:01,1281.00,158,117,2",  # 开盘集合竞价撮合（= 当日今开）
    "09:30:01,1281.00,2,2,2",
    "11:28:52,1277.57,12,7,1",
    "11:29:55,1277.23,1,1,1",
]
EM_PRECLOSE_600519 = 1277.96

# 腾讯分笔（sh600519）：真实响应
TX_DETAIL_PAGE = ('v_detail_data_sh600519=[0,"0/09:25:01/1281.00/-1.20/158/20239800/S'
                  '|1/09:30:01/1281.00/0.00/2/256000/B'
                  '|2/09:30:04/1281.96/0.96/1/128196/B'
                  '|9/09:44:49/1280.02/0.01/3/384030/M"];')
# 腾讯分笔页索引（action=all）：真实响应
TX_DETAIL_PAGES = ('v_detail_time_sh600519=[20260915,"09:25:01~09:33:28|09:33:31~09:37:22'
                   '|09:37:25~09:41:10"];')

# 腾讯实时行情（qt.gtimg.cn，GBK 解码后）：真实字段布局
_TX_Q = (["1", "贵州茅台", "600519", "1277.23", "1277.96", "1281.00", "7775", "4359", "3416"]
         + ["0.00"] * 20
         + ["", "20260915125658", "-0.73", "-0.06", "1284.50", "1273.00"])
TX_QUOTE = 'v_sh600519="%s";' % "~".join(_TX_Q)

# 东方财富涨停池（2026-09-15）：真实返回片段
ZT_POOL = [
    {"c": "002912", "m": 0, "n": "中新赛克", "p": 28450, "zdp": 10.015467643737793,
     "amount": 526167376, "ltsz": 4615239684.2, "tshare": 4857894400.0,
     "hs": 11.422986030578613, "lbc": 4, "fbt": 92500, "lbt": 112503, "fund": 115916765,
     "zbc": 3, "hybk": "计算机设", "zttj": {"days": 4, "ct": 4}},
    {"c": "002232", "m": 0, "n": "启明信息", "p": 16810, "zdp": 10.013089179992676,
     "amount": 150822324, "ltsz": 6867699528.549999, "tshare": 6867699410.879999,
     "hs": 2.196207046508789, "lbc": 2, "fbt": 93000, "lbt": 93000, "fund": 126641497,
     "zbc": 0, "hybk": "IT服务Ⅱ", "zttj": {"days": 2, "ct": 2}},
    {"c": "688004", "m": 1, "n": "博汇科技", "p": 27380, "zdp": 19.98, "amount": 1000,
     "ltsz": 1.0, "tshare": 1.0, "hs": 1.0, "lbc": 2, "fbt": 92503, "lbt": 92503,
     "fund": 1, "zbc": 0, "hybk": "软件服务", "zttj": {"days": 2, "ct": 2}},
]

# 东方财富龙虎榜（2026-09-14）：真实返回片段
LHB_ROWS = [
    {"TRADE_DATE": "2026-09-14 00:00:00", "DEAL_AMOUNT_RATIO": 22.157052171369,
     "BILLBOARD_DEAL_AMT": 245043047.6, "FREE_MARKET_CAP": 6479293174.94,
     "EXPLAIN": "主力做T，成功率36.18%", "SECUCODE": "000523.SZ", "SECURITY_CODE": "000523",
     "CLOSE_PRICE": 3.61, "CHANGE_RATE": -9.9751, "TURNOVERRATE": 16.8575,
     "SECURITY_NAME_ABBR": "红棉股份", "EXPLANATION": "日跌幅偏离值达到7%的前5只证券",
     "BILLBOARD_SELL_AMT": 136884973.8, "BILLBOARD_BUY_AMT": 108158073.8,
     "BILLBOARD_NET_AMT": -28726900, "DEAL_NET_RATIO": -2.597516755753,
     "ACCUM_AMOUNT": 1105937043, "MARKET": "SZ", "TRADE_MARKET": "深交所主板",
     "BUY_SEAT": 11311, "SELL_SEAT": 11111, "SUM_BUY_AMT": 108000000.0,
     "SUM_SELL_AMT": 136000000.0, "CHANGE_TYPE": "137001002002001", "TRADE_ID": 100411928,
     "D1_CLOSE_ADJCHRATE": None, "D5_CLOSE_ADJCHRATE": -9.5},
    {"TRADE_DATE": "2026-09-14 00:00:00", "DEAL_AMOUNT_RATIO": 27.010760943833,
     "BILLBOARD_DEAL_AMT": 7403139516.19, "FREE_MARKET_CAP": 67587159174.1,
     "EXPLAIN": "4家机构买入，成功率28.67%", "SECUCODE": "000636.SZ", "SECURITY_CODE": "000636",
     "CLOSE_PRICE": 58.9, "CHANGE_RATE": 5.1974, "TURNOVERRATE": 19.1456,
     "SECURITY_NAME_ABBR": "风华高科", "EXPLANATION": "连续三个交易日内，涨幅偏离值累计达到20%的证券",
     "BILLBOARD_SELL_AMT": 3559826480.5, "BILLBOARD_BUY_AMT": 3843313035.69,
     "BILLBOARD_NET_AMT": 283486555.19, "DEAL_NET_RATIO": 1.034316259512,
     "ACCUM_AMOUNT": 27408111647, "MARKET": "SZ", "TRADE_MARKET": "深交所主板",
     "BUY_SEAT": 13333, "SELL_SEAT": 13333, "SUM_BUY_AMT": 3843313035.69,
     "SUM_SELL_AMT": 3559826480.5, "CHANGE_TYPE": "137001002001002", "TRADE_ID": 100411936,
     "D1_CLOSE_ADJCHRATE": 1.0, "D5_CLOSE_ADJCHRATE": None},
    # 同一标的的第二条上榜原因（东财实测会返回多行，本模块不做金额求和）
    {"TRADE_DATE": "2026-09-14 00:00:00", "SECUCODE": "000636.SZ", "SECURITY_CODE": "000636",
     "SECURITY_NAME_ABBR": "风华高科", "CLOSE_PRICE": 58.9, "CHANGE_RATE": 5.1974,
     "TURNOVERRATE": 19.1456, "EXPLANATION": "日换手率达到20%的前5只证券",
     "BILLBOARD_NET_AMT": 100000000.0, "MARKET": "SZ", "TRADE_MARKET": "深交所主板"},
]

# 龙虎榜空数据（东财真实返回）
LHB_EMPTY = {"version": None, "result": None, "success": False,
             "message": "返回数据为空", "code": 9201}


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #

def _no_cache(_key, _ttl, producer):
    """绕过进程内缓存，直接执行 producer（离线测试用）"""
    out = dict(producer())
    out["stale"] = False
    return out


def _em_details_stub(details=None, pre_close=None, code="600519"):
    def _stub(_code, pos=0, mpi=100000):
        rows = list(details if details is not None else EM_DETAILS_600519)
        if pos < 0:                      # 模拟「最近 N 条」
            rows = rows[pos:]
        return {"host": "stub", "code": code, "preClose": pre_close,
                "decimal": 2, "details": rows}
    return _stub


ENVELOPE_KEYS = {"ok", "data", "source", "fetchedAt", "dataTime", "error", "degraded", "stale"}


class OfflineTests(unittest.TestCase):
    """纯离线：解析逻辑、单位口径、降级形状"""

    # ---------------- 代码规范化 ----------------
    def test_norm_code_variants(self):
        self.assertEqual(features._norm_code("600519"), ("sh", "600519"))
        self.assertEqual(features._norm_code("sh600519"), ("sh", "600519"))
        self.assertEqual(features._norm_code("600519.SH"), ("sh", "600519"))
        self.assertEqual(features._norm_code("SZ000001"), ("sz", "000001"))
        self.assertEqual(features._norm_code("300750"), ("sz", "300750"))
        self.assertEqual(features._norm_code("688004"), ("sh", "688004"))
        self.assertEqual(features._norm_code("430047"), ("bj", "430047"))
        self.assertEqual(features._norm_code("833171"), ("bj", "833171"))
        with self.assertRaises(ValueError):
            features._norm_code("60051")

    def test_norm_date_variants(self):
        self.assertEqual(features._norm_date("2026-09-14"), "2026-09-14")
        self.assertEqual(features._norm_date("20260914"), "2026-09-14")
        self.assertEqual(features._norm_date("2026/09/14"), "2026-09-14")
        self.assertRegex(features._norm_date(None), r"^\d{4}-\d{2}-\d{2}$")
        with self.assertRaises(ValueError):
            features._norm_date("2026-15-99")
        with self.assertRaises(ValueError):
            features._norm_date("昨天")

    def test_em_secid_and_tx_symbol(self):
        self.assertEqual(features._em_secid("600519"), "1.600519")
        self.assertEqual(features._em_secid("000001"), "0.000001")
        self.assertEqual(features._em_secid("300750"), "0.300750")
        self.assertEqual(features._tx_symbol("688004"), "sh688004")
        self.assertEqual(features._tx_symbol("833171"), "bj833171")

    # ---------------- 东方财富分笔解析 ----------------
    def test_parse_em_details_fields(self):
        rows = features._parse_em_details(EM_DETAILS_600519)
        self.assertEqual(len(rows), len(EM_DETAILS_600519))
        first = rows[0]
        self.assertEqual(first["time"], "09:15:10")
        self.assertEqual(first["price"], 1277.96)
        self.assertEqual(first["volume"], 1)          # 手
        self.assertEqual(first["trades"], 0)          # 竞价委托快照无成交笔数
        self.assertEqual(first["side"], "4")
        self.assertEqual(first["sideText"], "中性")
        self.assertEqual(first["amount"], 127796.0)   # 1 手 = 100 股

    def test_parse_em_details_amount_and_side(self):
        rows = {r["time"]: r for r in features._parse_em_details(EM_DETAILS_600519)}
        m = rows["11:28:52"]
        self.assertEqual(m["volume"], 12)
        self.assertEqual(m["trades"], 7)
        self.assertEqual(m["sideText"], "卖盘")
        self.assertAlmostEqual(m["amount"], 1277.57 * 12 * 100, places=2)
        self.assertEqual(rows["09:25:01"]["sideText"], "买盘")
        self.assertEqual(rows["09:25:01"]["trades"], 117)

    def test_parse_em_details_skips_bad_lines(self):
        rows = features._parse_em_details(["bad", "", "09:30:01,10.00,1,1,2",
                                           "09:30:02,-,-,-,-"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 10.0)

    # ---------------- 腾讯分笔 / 行情解析 ----------------
    def test_parse_tx_detail_page(self):
        rows = features._parse_tx_detail_page(TX_DETAIL_PAGE)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["seq"], 0)
        self.assertEqual(rows[0]["time"], "09:25:01")          # 首条 = 集合竞价撮合
        self.assertEqual(rows[0]["price"], 1281.00)
        self.assertEqual(rows[0]["volume"], 158)
        self.assertEqual(rows[0]["amount"], 20239800)          # 数据源真实成交额
        self.assertEqual(rows[0]["side"], "S")
        self.assertIsNone(rows[0]["trades"])                   # 腾讯不提供笔数
        self.assertEqual(rows[3]["sideText"], "中性")
        self.assertEqual(rows[1]["sideText"], "买盘")

    def test_parse_tx_detail_page_ignores_head_garbage(self):
        self.assertEqual(features._parse_tx_detail_page("v=[0,\"\"];"), [])
        self.assertEqual(features._parse_tx_detail_page("no payload"), [])

    def test_parse_tx_detail_pages_text(self):
        import re
        m = re.search(r'\[(\d{8})\s*,\s*"(.*?)"\s*\]', TX_DETAIL_PAGES, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "20260915")
        ranges = m.group(2).split("|")
        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[0], "09:25:01~09:33:28")

    def test_parse_tx_quote(self):
        q = features._parse_tx_quote(TX_QUOTE)
        self.assertEqual(q["code"], "600519")
        self.assertEqual(q["name"], "贵州茅台")       # GBK 解码后中文正常
        self.assertEqual(q["price"], 1277.23)
        self.assertEqual(q["preClose"], 1277.96)
        self.assertEqual(q["open"], 1281.00)          # 今开 = 集合竞价撮合价
        self.assertEqual(q["volume"], 7775)
        self.assertEqual(q["outer"], 4359)
        self.assertEqual(q["inner"], 3416)
        self.assertEqual(q["stamp"], "2026-09-15 12:56:58")

    def test_parse_tx_quote_rejects_short_payload(self):
        self.assertIsNone(features._parse_tx_quote('v_sh600519="1~2~3";'))

    # ---------------- 时间格式化（涨停池） ----------------
    def test_hhmmss(self):
        self.assertEqual(features._hhmmss(92500), "09:25:00")
        self.assertEqual(features._hhmmss(112503), "11:25:03")
        self.assertEqual(features._hhmmss(145959), "14:59:59")
        self.assertIsNone(features._hhmmss(0))
        self.assertIsNone(features._hhmmss(None))

    # ---------------- 涨停池解析 / 梯队分组 ----------------
    def test_parse_zt_pool_scaling_and_fields(self):
        rows = features._parse_zt_pool(ZT_POOL)
        self.assertEqual(rows[0]["code"], "002912")               # 按连板数降序
        self.assertEqual(rows[0]["ladder"], 4)
        self.assertEqual(rows[0]["statText"], "4天4板")
        self.assertEqual(rows[0]["firstSealTime"], "09:25:00")
        self.assertEqual(rows[0]["lastSealTime"], "11:25:03")
        self.assertEqual(rows[0]["openTimes"], 3)                 # 炸板次数
        self.assertEqual(rows[0]["industry"], "计算机设")
        self.assertEqual(rows[0]["market"], "sz")

    def test_parse_zt_pool_price_divide_1000(self):
        # 实测口径：原始 p 为「元 × 1000」（002912 → 28.45 与腾讯实时价一致）
        rows = {r["code"]: r for r in features._parse_zt_pool(ZT_POOL)}
        self.assertEqual(rows["002912"]["price"], 28.45)
        self.assertEqual(rows["002232"]["price"], 16.81)
        self.assertEqual(rows["688004"]["price"], 27.38)
        self.assertEqual(rows["688004"]["market"], "sh")          # m=1 → 沪市

    def test_group_ladder_and_gaps(self):
        stocks = features._parse_zt_pool(ZT_POOL)
        ladders, top, gaps = features._group_ladder(stocks)
        self.assertEqual(top, 4)
        self.assertEqual([x["level"] for x in ladders], [4, 2])   # 降序
        self.assertEqual([x["count"] for x in ladders], [1, 2])
        self.assertEqual(gaps, [1, 3])                            # 1 板与 3 板断层

    def test_group_ladder_empty(self):
        ladders, top, gaps = features._group_ladder([])
        self.assertEqual((ladders, top, gaps), ([], 0, []))

    # ---------------- 龙虎榜解析 / 聚合 ----------------
    def test_lhb_row_mapping(self):
        row = features._lhb_row(LHB_ROWS[0])
        self.assertEqual(row["code"], "000523")
        self.assertEqual(row["name"], "红棉股份")
        self.assertEqual(row["market"], "sz")
        self.assertEqual(row["close"], 3.61)
        self.assertEqual(row["changePct"], -9.9751)
        self.assertEqual(row["netAmt"], -28726900)
        self.assertEqual(row["buyAmt"], 108158073.8)
        self.assertEqual(row["sellAmt"], 136884973.8)
        self.assertEqual(row["reason"], "日跌幅偏离值达到7%的前5只证券")
        self.assertEqual(row["explain"], "主力做T，成功率36.18%")
        self.assertEqual(row["tradeMarket"], "深交所主板")
        self.assertEqual(row["performance"]["d1"], None)
        self.assertEqual(row["performance"]["d5"], -9.5)

    def test_lhb_aggregate_merges_reasons_without_summing(self):
        rows = [features._lhb_row(x) for x in LHB_ROWS]
        stocks, top_buy, top_sell = features._lhb_aggregate(rows)
        self.assertEqual(len(stocks), 2)                       # 000523 / 000636
        fh = [s for s in stocks if s["code"] == "000636"][0]
        self.assertEqual(len(fh["reasons"]), 2)                # 两个上榜原因合并
        self.assertEqual(fh["rowIndexes"], [1, 2])
        self.assertEqual(top_buy[0]["code"], "000636")         # 净买额最大
        self.assertEqual(top_sell[0]["code"], "000523")        # 净卖额最大

    # ---------------- 空实现 / 统一信封 ----------------
    def test_empty_shapes_and_envelope(self):
        auc = features._empty_auction("600519")
        self.assertEqual(auc["phase"], "no_data")
        self.assertIsNone(auc["unmatched"])
        self.assertIn("未匹配量", auc["unmatchedNote"])
        tick = features._empty_ticks("600519")
        self.assertEqual((tick["ticks"], tick["count"]), ([], 0))
        lhb = features._empty_lhb("2026-09-14")
        self.assertEqual((lhb["rows"], lhb["stocks"], lhb["count"]), ([], [], 0))
        self.assertEqual(lhb["requestedDate"], "2026-09-14")
        lad = features._empty_ladder("2026-09-14")
        self.assertEqual((lad["ladders"], lad["gaps"], lad["maxLadder"]), ([], [], 0))
        wrapped = features._wrap(False, features._empty_ticks("600519"), None,
                                 error="上游不可用", degraded=True)
        self.assertEqual(set(wrapped.keys()), ENVELOPE_KEYS)
        self.assertFalse(wrapped["ok"])
        self.assertTrue(wrapped["degraded"])
        self.assertEqual(wrapped["data"]["ticks"], [])
        self.assertIn("上游不可用", wrapped["error"])

    def test_empty_auction_has_unmatched_documented(self):
        empty = features._empty_auction("600519")
        self.assertIsNone(empty["unmatched"])
        self.assertIn("未匹配量", empty["unmatchedNote"])
        self.assertEqual(empty["phase"], "no_data")

    # ---------------- 集合竞价：离线复算 ----------------
    def test_auction_matched_offline(self):
        with mock.patch.object(features, "_em_details",
                               _em_details_stub(pre_close=EM_PRECLOSE_600519)), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.auction("600519")
        self.assertTrue(r["ok"])
        d = r["data"]
        self.assertEqual(d["phase"], "matched")
        self.assertEqual(d["auctionTime"], "09:25:01")
        self.assertEqual(d["price"], 1281.00)            # = 当日今开
        self.assertEqual(d["volume"], 158)               # 手
        self.assertEqual(d["amount"], 20239800)          # 与腾讯分笔成交额一致
        self.assertEqual(d["trades"], 117)
        self.assertEqual(d["preClose"], EM_PRECLOSE_600519)
        self.assertAlmostEqual(d["change"], 3.04, places=2)
        self.assertEqual(d["orderCount"], 3)             # 09:15:10 / 09:24:52 / 09:24:58
        self.assertEqual(d["lastOrder"]["price"], 1282.20)
        self.assertEqual(d["lastOrderVolume"], 155)      # 撮合前最后一条快照委托量
        self.assertEqual(d["orderVolume"], 1 + 106 + 155)  # 逐条快照量合计（非累计）
        self.assertIn("撮合", d["priceSource"])
        self.assertEqual(set(r.keys()), ENVELOPE_KEYS)

    def test_auction_auctioning_offline(self):
        """09:15~09:25 之间调用：尚无撮合记录，返回竞价委托快照并标注仅供观察"""
        orders = ["09:15:10,1277.96,1,0,4", "09:24:58,1282.20,155,0,4"]
        with mock.patch.object(features, "_em_details",
                               _em_details_stub(details=orders, pre_close=EM_PRECLOSE_600519)), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.auction("600519")
        self.assertTrue(r["ok"])
        d = r["data"]
        self.assertEqual(d["phase"], "auctioning")
        self.assertEqual(d["price"], 1282.20)
        self.assertIsNone(d["volume"])
        self.assertIn("尚未撮合", d["priceSource"])

    def test_auction_falls_back_to_tx(self):
        """东财失败 -> 腾讯分笔首条 + 实时行情（降级源，degraded=True）"""
        def _boom(*_a, **_k):
            raise RuntimeError("push2 连接被重置")

        with mock.patch.object(features, "_em_details", _boom), \
                mock.patch.object(features, "_tx_quote",
                                  lambda code: features._parse_tx_quote(TX_QUOTE)), \
                mock.patch.object(features, "_tx_detail_last",
                                  lambda code, limit: ("20260915", [
                                      features._parse_tx_detail_page(TX_DETAIL_PAGE)[0]])), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.auction("600519")
        self.assertTrue(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertIn("腾讯", r["source"])
        self.assertEqual(r["data"]["price"], 1281.00)
        self.assertEqual(r["data"]["volume"], 158)
        self.assertEqual(r["data"]["orderTicks"], [])
        self.assertIsNone(r["data"]["lastOrderVolume"])   # 腾讯源无竞价委托明细

    def test_auction_all_sources_fail_returns_empty(self):
        def _boom(*_a, **_k):
            raise RuntimeError("网络不可用")

        with mock.patch.object(features, "_em_details", _boom), \
                mock.patch.object(features, "_tx_quote", _boom), \
                mock.patch.object(features, "_tx_detail_last", _boom), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.auction("600519")
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertEqual(set(r.keys()), ENVELOPE_KEYS)
        self.assertEqual(r["data"]["phase"], "no_data")
        self.assertIn("网络不可用", r["error"])

    # ---------------- 分笔：离线复算 ----------------
    def test_ticks_offline_em(self):
        with mock.patch.object(features, "_em_details",
                               _em_details_stub(pre_close=EM_PRECLOSE_600519)), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.ticks("600519", 5)
        self.assertTrue(r["ok"])
        d = r["data"]
        self.assertEqual(d["code"], "600519")
        self.assertEqual(d["market"], "sh")
        self.assertEqual(d["count"], 5)
        self.assertEqual(d["ticks"][-1]["time"], "11:29:55")     # 升序，最后一条最新
        self.assertEqual(d["latestPrice"], 1277.23)
        self.assertIn("1=卖盘", d["sideRule"])

    def test_ticks_limit_clamped(self):
        with mock.patch.object(features, "_em_details",
                               _em_details_stub(pre_close=EM_PRECLOSE_600519)), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.ticks("600519", 999999)
        self.assertTrue(r["ok"])
        self.assertLessEqual(r["data"]["count"], features.TICKS_MAX)

    def test_ticks_offline_tx_fallback(self):
        def _boom(*_a, **_k):
            raise RuntimeError("push2 连接被重置")

        rows = features._parse_tx_detail_page(TX_DETAIL_PAGE)
        with mock.patch.object(features, "_em_details", _boom), \
                mock.patch.object(features, "_tx_detail_last",
                                  lambda code, limit: ("20260915", rows[-limit:])), \
                mock.patch.object(features, "_tx_quote",
                                  lambda code: features._parse_tx_quote(TX_QUOTE)), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.ticks("600519", 2)
        self.assertTrue(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["data"]["count"], 2)
        self.assertIsNone(r["data"]["ticks"][0]["trades"])       # 腾讯源无笔数
        self.assertIn("B=买盘", r["data"]["sideRule"])

    # ---------------- 龙虎榜：离线复算 + 交易日回溯 ----------------
    def _lhb_stub(self, mapping):
        def _stub(date_str):
            rows = mapping.get(date_str, [])
            if not rows:
                raise features._LhbEmpty("返回数据为空", 9201)
            return rows
        return _stub

    def test_dragon_tiger_offline(self):
        stub = self._lhb_stub({"2026-09-14": LHB_ROWS})
        with mock.patch.object(features, "_em_lhb", stub), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.dragon_tiger("2026-09-14")
        self.assertTrue(r["ok"])
        d = r["data"]
        self.assertEqual(d["date"], "2026-09-14")
        self.assertFalse(d["fallback"])
        self.assertEqual(d["count"], 3)
        self.assertEqual(len(d["rows"]), 3)
        self.assertEqual(len(d["stocks"]), 2)
        self.assertIn("数据中心", r["source"])
        self.assertEqual(set(r.keys()), ENVELOPE_KEYS)

    def test_dragon_tiger_falls_back_to_previous_trading_day(self):
        """当天无数据（盘后未发布 / 周末）时自动回溯，并标注 fallbackDays"""
        stub = self._lhb_stub({"2026-09-14": LHB_ROWS})
        with mock.patch.object(features, "_em_lhb", stub), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.dragon_tiger("2026-09-15")     # 周二，无数据
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["requestedDate"], "2026-09-15")
        self.assertEqual(r["data"]["date"], "2026-09-14")
        self.assertTrue(r["data"]["fallback"])
        self.assertEqual(r["data"]["fallbackDays"], 1)
        self.assertTrue(r["degraded"])

    def test_dragon_tiger_no_data_returns_empty(self):
        stub = self._lhb_stub({})
        with mock.patch.object(features, "_em_lhb", stub), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.dragon_tiger("2026-09-14")
        self.assertFalse(r["ok"])
        self.assertEqual(r["data"]["rows"], [])
        self.assertEqual(r["data"]["count"], 0)
        self.assertTrue(r["data"]["note"])

    def test_dragon_tiger_bad_date(self):
        r = features.dragon_tiger("2026-15-99")
        self.assertFalse(r["ok"])
        self.assertIn("日期格式", r["error"])
        self.assertEqual(r["data"]["rows"], [])

    def test_lhb_empty_exception_type(self):
        """东财空数据（success=false / 9201）应转成内部信号，用于触发回溯"""
        self.assertTrue(issubclass(features._LhbEmpty, RuntimeError))
        self.assertEqual(LHB_EMPTY["code"], 9201)

    # ---------------- 涨停梯队：离线复算 ----------------
    def test_limit_up_ladder_offline(self):
        def _stub(date_str):
            return (ZT_POOL, len(ZT_POOL)) if date_str == "20260915" else ([], 0)

        with mock.patch.object(features, "_em_zt_pool", _stub), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.limit_up_ladder("2026-09-15")
        self.assertTrue(r["ok"])
        d = r["data"]
        self.assertEqual(d["date"], "2026-09-15")
        self.assertEqual(d["count"], 3)
        self.assertEqual(d["maxLadder"], 4)
        self.assertEqual([x["level"] for x in d["ladders"]], [4, 2])
        self.assertEqual(d["gaps"], [1, 3])
        self.assertEqual(d["stocks"][0]["price"], 28.45)
        self.assertIn("涨停池", r["source"])

    def test_limit_up_ladder_fallback_marks_date(self):
        def _stub(date_str):
            return (ZT_POOL, len(ZT_POOL)) if date_str == "20260911" else ([], 0)

        with mock.patch.object(features, "_em_zt_pool", _stub), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.limit_up_ladder("2026-09-14")   # 无数据 -> 回溯到 09-11(周五)
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["date"], "2026-09-11")
        self.assertEqual(r["data"]["fallbackDays"], 3)
        self.assertTrue(r["data"]["fallback"])

    def test_limit_up_ladder_out_of_window_returns_empty(self):
        def _stub(date_str):
            return ([], 0)

        with mock.patch.object(features, "_em_zt_pool", _stub), \
                mock.patch.object(features, "_cached", _no_cache):
            r = features.limit_up_ladder("2026-06-01")
        self.assertFalse(r["ok"])
        self.assertEqual(r["data"]["ladders"], [])
        self.assertEqual(r["data"]["maxLadder"], 0)
        self.assertIn("20", r["data"]["note"] + (r["error"] or ""))

    def test_limit_up_ladder_bad_date(self):
        r = features.limit_up_ladder("2026-15-99")
        self.assertFalse(r["ok"])
        self.assertIn("日期格式", r["error"])
        self.assertEqual(r["data"]["ladders"], [])

    def test_recent_trading_day_skips_weekend(self):
        seen = []

        def probe(ds):
            seen.append(ds)
            return ds == "2026-09-11"          # 只有周五有数据

        day, back = features._recent_trading_day("2026-09-14", 5, probe)
        self.assertEqual(day, "2026-09-11")
        self.assertEqual(back, 3)
        self.assertNotIn("2026-09-13", seen)   # 周日不请求
        self.assertNotIn("2026-09-12", seen)   # 周六不请求


# --------------------------------------------------------------------------- #
# 联网校验：真实请求四个接口（网络不通时自动 skip）
# --------------------------------------------------------------------------- #

def _network_ok():
    try:
        features._tx_quote("000001")
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


class LiveTests(unittest.TestCase):
    """真实联网校验（前置探测失败则整体 skip，避免把「没网」当成「代码错」）"""

    @classmethod
    def setUpClass(cls):
        ok, err = _network_ok()
        if not ok:
            raise unittest.SkipTest("联网校验跳过（网络不可用：%s）" % err)

    def test_live_auction(self):
        r = features.auction("600519")
        if not r["ok"]:
            self.skipTest("上游暂无竞价数据（可能未开盘/非交易日）：%s" % r["error"])
        self.assertEqual(set(r.keys()), ENVELOPE_KEYS)
        d = r["data"]
        self.assertIn(d["phase"], ("matched", "auctioning"))
        self.assertEqual(d["market"], "sh")
        self.assertGreater(d["price"], 0)
        self.assertGreater(d["volume"], 0)             # 600519 竞价必然有量
        self.assertGreater(d["orderCount"], 0)         # 竞价委托快照存在
        self.assertEqual(d["amount"], round(d["price"] * d["volume"] * 100, 2))
        if d["preClose"]:
            self.assertAlmostEqual(d["changePct"], (d["price"] / d["preClose"] - 1) * 100, places=2)
        # 竞价成交价必须等于腾讯行情的「今开」（两个独立源交叉验证）
        q = features._tx_quote("600519")
        if q.get("open"):
            self.assertAlmostEqual(d["price"], q["open"], places=2)

    def test_live_ticks(self):
        r = features.ticks("000001", 20)
        if not r["ok"]:
            self.skipTest("上游暂无分笔数据（可能未开盘/非交易日）：%s" % r["error"])
        d = r["data"]
        self.assertLessEqual(d["count"], 20)
        self.assertGreater(d["count"], 0)
        times = [t["time"] for t in d["ticks"]]
        self.assertEqual(times, sorted(times))         # 时间升序
        for t in d["ticks"]:
            self.assertGreater(t["price"], 0)
            self.assertGreater(t["volume"], 0)
            self.assertIn(t["sideText"], ("买盘", "卖盘", "中性", "未知"))
        self.assertEqual(d["latestTime"], times[-1])

    def test_live_dragon_tiger(self):
        r = features.dragon_tiger()                     # None = 最近一个有数据的交易日
        if not r["ok"]:
            self.skipTest("龙虎榜暂无数据：%s" % r["error"])
        d = r["data"]
        self.assertGreater(d["count"], 0)
        self.assertRegex(d["date"], r"^\d{4}-\d{2}-\d{2}$")
        row = d["rows"][0]
        for key in ("code", "name", "close", "changePct", "netAmt", "reason"):
            self.assertIn(key, row)
        self.assertRegex(row["code"], r"^\d{6}$")
        self.assertIn(row["market"], ("sh", "sz", "bj"))
        self.assertGreaterEqual(len(d["stocks"]), 1)
        self.assertLessEqual(len(d["topNetBuy"]), 10)

    def test_live_limit_up_ladder(self):
        r = features.limit_up_ladder()
        if not r["ok"]:
            self.skipTest("涨停池暂无数据：%s" % r["error"])
        d = r["data"]
        self.assertGreater(d["count"], 0)
        self.assertGreaterEqual(d["maxLadder"], 1)
        self.assertEqual(sum(x["count"] for x in d["ladders"]), d["count"])
        self.assertEqual(d["maxLadder"], max(x["ladder"] for x in d["stocks"]))
        for s in d["stocks"]:
            self.assertGreaterEqual(s["ladder"], 1)    # 涨停股连板数至少 1
            self.assertGreater(s["price"], 0)
            self.assertRegex(s["code"], r"^\d{6}$")
        # 最高板个股价格与腾讯实时价交叉验证（验证 p÷1000 缩放口径）
        top = d["stocks"][0]
        q = features._tx_quote(top["code"])
        self.assertAlmostEqual(top["price"], q["price"], places=2)

    def test_live_northbound_board_degrades_gracefully(self):
        """北交所无分笔/竞价数据（实测）：必须返回 ok=False 的空实现而不是抛异常"""
        r = features.ticks("430047", 5)
        self.assertIn("ok", r)
        if not r["ok"]:
            self.assertEqual(r["data"]["ticks"], [])
            self.assertTrue(r["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
