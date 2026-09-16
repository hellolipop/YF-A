#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.review（复盘绩效统计）的单元测试（仅标准库 unittest，无网络，可直接 python 运行）。

覆盖范围（与需求的 13 组断言一一对应）
--------------------------------------
 1. TestRoundTrips        往返交易配对：单笔开平 / 部分平仓 / 多次加仓后一次平仓 /
                          反手（平仓量超过开仓量）/ 先空后多 / 只有开仓未平仓不计为已完成交易 /
                          传入顺序打乱也按成交时间配对 / 脏委托单只记 skipped 不抛异常；
 2. TestTradeMetrics      逐笔指标与手工复算逐位核对（容差 1e-9/1e-6）：
                          trades / wins / losses / winRate / avgWin / avgLoss / payoffRatio /
                          profitFactor / expectancy / largestWin / largestLoss /
                          maxConsecutiveWins / maxConsecutiveLosses / avgBarsHeld /
                          totalPnl / totalFee / avgReturnPct；手续费计入 pnl 与 returnPct；
                          0 亏损或 0 盈利时 payoffRatio / profitFactor 返回 None 不抛异常、
                          不返回 inf；样本为 0 的比率一律 None；平局计入 winRate 分母；
                          样本不足告警 + minSampleForConfidence；MAE/MFE 恒为 None 且有告警；
 3. TestPeriodMetrics     周期口径：总收益 / 年化（含短样本外推截断为 None）/
                          Sharpe（×√252）/ Sortino / 最大回撤 / 最长水下时间 /
                          水下占比 / Ulcer Index / Calmar 的手工核对；
                          「回撤很深但很快恢复」与「回撤不深但长期水下」两组用例分别断言
                          深度族与时间族指标各自反映自己的特征；kellyFraction 公式核对；
                          无权益数据 → 全部 None 且不抛异常；零/负权益不崩；
 4. TestCacheFlow         出入金链式处理：入金 / 出金两条曲线，剔除后与含出入金的口径
                          **显著不同**且等于手算值；dict / 逐点 flow / 等长数字列表三种入参；
                          未匹配的资金流如实回报；未检测到出入金时 two 口径相等；
 5. TestAdvisorBreakdown  分组命中率：7 个档位键始终给全、看多/看空/中性三组、
                          样本 < 5 标注「样本过少」且不参与排名、中性档位只记录涨跌、
                          pending/nodata 不补判、策略代理口径、脏输入不抛异常；
 6. TestReviewReport      端到端（Store(":memory:")）：窗口 / 逐笔 / 周期 / 回撤 / byAdvisor /
                          bySource(ai/manual/scheduled) / note / warnings 的契约，
                          days 过滤与非法 days 兜底、无权益、空库、store=None、
                          存储层缺方法、出入金剔除、未平仓告警、AI 记录分布；
 7. TestJsonStrictness    所有输出都能 json.dumps(allow_nan=False) 通过（不允许 inf/NaN）。

为什么这样造数据
----------------
· 所有价格 / 数量 / 手续费都取**二进制可精确表示**的数（整数、0.5、0.25），
  这样「逐位核对」不会被浮点四舍五入的噪声破坏；
· 期望值要么直接写出（可心算），要么在测试里用**与实现无关的公式重算一遍**
  （例如 Sharpe = mean/sd×√252、Ulcer = sqrt(mean(dd²))），不做「看着像」的断言；
· 权益曲线由显式数值列表构造，不用随机数、不联网；
· 时间戳用 `now_ms()` 相对偏移，避免把毫秒值写死后依赖运行时刻。

运行方式::
    python3 tests/test_review.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import json
import math
import os
import sys
import unittest

# 让测试既能在 stock-terminal/ 下跑，也能在仓库根目录下跑
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import review as R                      # noqa: E402
from core import trader as T                      # noqa: E402
from core.storage import Store, now_ms            # noqa: E402

DAY_MS = 86400000


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

_SEQ = [0]


def order(**over):
    """一条「已成交」委托单（字段名与 store.save_trade_order 的列一致）。"""
    _SEQ[0] += 1
    o = {"id": "o-%03d" % _SEQ[0], "market": "cn", "code": "600000", "name": "浦发银行",
         "side": "buy", "intent": "open", "action": "buy", "mode": "paper",
         "status": "filled", "source": "ai", "qty": 100, "fillPrice": 10.0, "fee": 0.0,
         "amount": 1000.0, "filledAt": 1000}
    o.update(over)
    return o


def trip(pnl, fee=0.0, returnPct=None, bars=None, code="600000", source="ai"):
    """一条手工构造的往返交易（用于隔离测试 trade_metrics）。"""
    return {"code": code, "openAt": 1, "closeAt": 2, "qty": 100.0, "entry": 10.0,
            "exit": 10.0 + pnl / 100.0, "fee": fee, "pnl": pnl,
            "returnPct": returnPct, "bars": bars, "source": source}


def curve(values, start_ts=1000, step=DAY_MS):
    """数值列表 → 权益点列表（t 递增）。"""
    return [{"t": start_ts + i * step, "v": float(v)} for i, v in enumerate(values)]


def curve_from_returns(rets, start=100.0):
    """由逐 bar 收益构造权益曲线（首点 = start）。"""
    vals = [start]
    for r in rets:
        vals.append(vals[-1] * (1.0 + r))
    return curve(vals)


def mean(seq):
    return sum(seq) / float(len(seq))


def stdev(seq):
    mu = mean(seq)
    return math.sqrt(sum((v - mu) ** 2 for v in seq) / float(len(seq) - 1))


def downsample_vol(rets):
    return math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / float(len(rets)))


# --------------------------------------------------------------------------- #
# 1. 往返交易配对
# --------------------------------------------------------------------------- #

class TestRoundTrips(unittest.TestCase):
    """配对规则：同向合并、反向平仓、部分平仓、反手、未平仓不计。"""

    def test_single_open_close(self):
        """一笔开仓 + 一笔等量平仓 = 1 笔已完成交易，盈亏与收益手算核对。"""
        orders = [order(side="buy", qty=100, fillPrice=10.0, filledAt=1000),
                  order(side="sell", qty=100, fillPrice=12.0, filledAt=2000)]
        trips = R.round_trips(orders)
        self.assertEqual(len(trips), 1)
        t = trips[0]
        for key in ("code", "openAt", "closeAt", "qty", "entry", "exit", "fee", "pnl",
                    "returnPct", "bars", "source"):
            self.assertIn(key, t)
        self.assertEqual(t["openAt"], 1000)
        self.assertEqual(t["closeAt"], 2000)
        self.assertEqual(t["qty"], 100.0)
        self.assertEqual(t["entry"], 10.0)
        self.assertEqual(t["exit"], 12.0)
        self.assertEqual(t["fee"], 0.0)
        self.assertEqual(t["pnl"], 200.0)                 # (12-10)×100
        self.assertEqual(t["returnPct"], 20.0)            # 200 / (10×100)
        self.assertEqual(t["dir"], 1)
        self.assertEqual(t["source"], "ai")

    def test_fee_is_charged_into_pnl_and_return_pct(self):
        """手续费必须计入 pnl 与 returnPct（开仓费 + 平仓费都要算）。"""
        orders = [order(side="buy", qty=100, fillPrice=10.0, fee=5.0, filledAt=1000),
                  order(side="sell", qty=100, fillPrice=12.0, fee=6.0, filledAt=2000)]
        t = R.round_trips(orders)[0]
        self.assertEqual(t["fee"], 11.0)
        self.assertEqual(t["pnl"], 189.0)                 # 200 - 11
        self.assertEqual(t["returnPct"], 18.9)            # 189 / (10×100) × 100

    def test_partial_close_makes_one_trip_per_close(self):
        """部分平仓：一笔开仓腿被两次平掉 = 2 笔已完成交易（一笔平仓腿 = 一笔交易）。"""
        orders = [order(side="buy", qty=200, fillPrice=10.0, filledAt=1000),
                  order(side="sell", qty=100, fillPrice=12.0, filledAt=2000),
                  order(side="sell", qty=100, fillPrice=11.0, filledAt=3000)]
        trips = R.round_trips(orders)
        self.assertEqual(len(trips), 2)
        self.assertEqual([t["exit"] for t in trips], [12.0, 11.0])   # 按平仓时间升序
        self.assertEqual([t["qty"] for t in trips], [100.0, 100.0])
        self.assertEqual([t["pnl"] for t in trips], [200.0, 100.0])
        self.assertEqual([t["returnPct"] for t in trips], [20.0, 10.0])

    def test_partial_close_prorates_fees(self):
        """部分平仓时手续费按量分摊：开仓费按平仓比例、平仓费按本笔量占比。"""
        orders = [order(side="buy", qty=200, fillPrice=10.0, fee=4.0, filledAt=1000),
                  order(side="sell", qty=100, fillPrice=12.0, fee=6.0, filledAt=2000),
                  order(side="sell", qty=100, fillPrice=11.0, fee=0.0, filledAt=3000)]
        t1, t2 = R.round_trips(orders)
        # 第一笔平掉一半：开仓费分摊 4×100/200 = 2，平仓费 6 → fee 8，pnl = 200-8
        self.assertEqual(t1["fee"], 8.0)
        self.assertEqual(t1["pnl"], 192.0)
        self.assertEqual(t1["returnPct"], 19.2)
        # 第二笔平掉剩余一半：开仓费剩 2，平仓费 0 → fee 2，pnl = 100-2
        self.assertEqual(t2["fee"], 2.0)
        self.assertEqual(t2["pnl"], 98.0)
        self.assertEqual(t2["returnPct"], 9.8)

    def test_scale_in_then_single_close(self):
        """多次加仓后一次平仓 = 1 笔交易，开仓价 = 成交量加权均价，openAt 取第一笔。"""
        orders = [order(side="buy", qty=100, fillPrice=10.0, fee=2.0, filledAt=1000),
                  order(side="buy", qty=100, fillPrice=12.0, fee=3.0, filledAt=1500),
                  order(side="sell", qty=200, fillPrice=15.0, fee=5.0, filledAt=2000)]
        trips = R.round_trips(orders)
        self.assertEqual(len(trips), 1)
        t = trips[0]
        self.assertEqual(t["qty"], 200.0)
        self.assertEqual(t["entry"], 11.0)                # (100×10 + 100×12) / 200
        self.assertEqual(t["openAt"], 1000)               # 第一笔开仓时间
        self.assertEqual(t["fee"], 10.0)                  # 2 + 3 + 5
        self.assertEqual(t["pnl"], 790.0)                 # (15-11)×200 - 10
        self.assertEqual(t["returnPct"], round(790.0 / 2200.0 * 100.0, 3))

    def test_scale_out_plus_reverse(self):
        """反手：平仓量超过开仓量时，多出的部分开成反向新腿（本次成交里平仓 + 开仓）。"""
        bought = [order(side="buy", qty=100, fillPrice=10.0, filledAt=1000),
                  order(side="sell", qty=150, fillPrice=12.0, filledAt=2000)]
        detail = R.round_trips_detail(bought)
        self.assertEqual(len(detail["trips"]), 1)
        self.assertEqual(detail["trips"][0]["pnl"], 200.0)      # (12-10)×100
        self.assertEqual(len(detail["openLegs"]), 1)
        leg = detail["openLegs"][0]
        self.assertEqual(leg["dir"], -1)                        # 反向新腿是空头
        self.assertEqual(leg["qty"], 50.0)
        self.assertEqual(leg["entry"], 12.0)
        # 再用一笔买回把反手腿平掉 → 变成第 2 笔已完成交易
        bought.append(order(side="buy", qty=50, fillPrice=10.0, filledAt=3000))
        trips = R.round_trips(bought)
        self.assertEqual(len(trips), 2)
        t2 = trips[1]
        self.assertEqual((t2["dir"], t2["qty"], t2["entry"], t2["exit"]),
                         (-1, 50.0, 12.0, 10.0))
        self.assertEqual(t2["pnl"], 100.0)                      # -(10-12)×50
        self.assertEqual(t2["returnPct"], round(100.0 / 600.0 * 100.0, 3))

    def test_short_first_then_cover(self):
        """先卖后买同样成对（空头腿：entry = 卖出均价，exit = 买回价）。"""
        orders = [order(side="sell", qty=100, fillPrice=12.0, filledAt=1000),
                  order(side="buy", qty=100, fillPrice=10.0, filledAt=2000)]
        t = R.round_trips(orders)[0]
        self.assertEqual(t["dir"], -1)
        self.assertEqual(t["entry"], 12.0)
        self.assertEqual(t["exit"], 10.0)
        self.assertEqual(t["pnl"], 200.0)
        self.assertEqual(t["returnPct"], round(200.0 / 1200.0 * 100.0, 3))

    def test_unclosed_leg_is_not_a_completed_trade(self):
        """只有开仓没有平仓 → round_trips 里没有它，但 openLegs 要如实回报。"""
        detail = R.round_trips_detail([order(side="buy", qty=100, fillPrice=10.0)])
        self.assertEqual(detail["trips"], [])
        self.assertEqual(len(detail["openLegs"]), 1)
        self.assertEqual(detail["openLegs"][0]["note"], "仅开仓未平仓，不计入已完成交易")
        self.assertEqual(R.round_trips([order(side="buy", qty=100, fillPrice=10.0)]), [])

    def test_out_of_order_input_is_sorted_by_time(self):
        """store 的列表是时间倒序：配对必须以成交时间为准，否则会配成反向的空头。"""
        orders = [order(side="sell", qty=100, fillPrice=12.0, filledAt=2000),
                  order(side="buy", qty=100, fillPrice=10.0, filledAt=1000)]
        t = R.round_trips(orders)[0]
        self.assertEqual(t["dir"], 1)
        self.assertEqual(t["entry"], 10.0)
        self.assertEqual(t["pnl"], 200.0)

    def test_two_symbols_are_paired_separately(self):
        """不同标的（或不同市场）不能互相抵消。"""
        orders = [order(code="AAA", side="buy", qty=100, fillPrice=10.0, filledAt=1000),
                  order(code="BBB", side="sell", qty=100, fillPrice=20.0, filledAt=2000),
                  order(code="AAA", side="sell", qty=100, fillPrice=11.0, filledAt=3000),
                  order(code="BBB", side="buy", qty=100, fillPrice=19.0, filledAt=4000)]
        trips = R.round_trips(orders)
        self.assertEqual([t["code"] for t in trips], ["AAA", "BBB"])
        self.assertEqual([t["pnl"] for t in trips], [100.0, 100.0])

    def test_bars_field_passes_through(self):
        """委托单自带 bars 时透传到交易（本项目委托单没有该列，只有外部来源才有）。"""
        orders = [order(side="buy", qty=100, fillPrice=10.0, bars=5, filledAt=1000),
                  order(side="sell", qty=100, fillPrice=12.0, filledAt=2000)]
        self.assertEqual(R.round_trips(orders)[0]["bars"], 5)

    def test_dirty_orders_only_record_skipped(self):
        """脏委托单：缺代码 / 非已成交 / 无数量 / 无成交价 / 出入金单 → 只记 skipped。"""
        orders = [
            None,
            42,
            {"code": ""},
            order(code="", side="buy"),
            order(status="pending"),
            order(qty=0),
            order(qty="abc"),                                  # 数量无法解析
            order(fillPrice=None),                             # 没有成交价
            {"id": "dep-1", "side": "in", "intent": "deposit", "amount": 1000.0,
             "status": "filled", "filledAt": 1000},
        ]
        detail = R.round_trips_detail(orders)
        self.assertEqual(detail["trips"], [])
        self.assertEqual(detail["openLegs"], [])
        self.assertEqual(detail["cashOrders"], 1)
        self.assertEqual(len(detail["skipped"]), 8)            # 其余 8 条各有原因
        reasons = " ".join(s["reason"] for s in detail["skipped"])
        self.assertIn("缺少标的代码", reasons)
        self.assertIn("不是已成交", reasons)
        self.assertIn("成交量", reasons)
        self.assertIn("成交价", reasons)

    def test_string_numbers_are_coerced(self):
        """HTTP JSON 里数值常被写成字符串：能解析就照常配对。"""
        orders = [order(side="buy", qty="100", fillPrice="10.5", fee="1"),
                  order(side="sell", qty="100", fillPrice="11.5", fee="1")]
        t = R.round_trips(orders)[0]
        self.assertEqual(t["entry"], 10.5)
        self.assertEqual(t["exit"], 11.5)
        self.assertEqual(t["fee"], 2.0)
        self.assertEqual(t["pnl"], 98.0)

    def test_none_and_garbage_input(self):
        """orders 为 None / 非法类型 / 全部非 dict：返回空列表而不是抛异常。"""
        for bad in (None, [], "x", 42, [None, 3, "s"], [{}]):
            self.assertEqual(R.round_trips(bad), [])
            detail = R.round_trips_detail(bad)
            self.assertEqual(detail["trips"], [])


# --------------------------------------------------------------------------- #
# 2. 逐笔口径指标
# --------------------------------------------------------------------------- #

#: 手工夹具：3 赢 2 亏（0 亏损或 0 盈利的极端情形另有用例）
PNLS = [200.0, 150.0, 100.0, -50.0, -30.0]
FEES = [1.0, 2.0, 3.0, 4.0, 5.0]
RETS = [20.0, 15.0, 10.0, -5.0, -3.0]
BARS = [5, 10, 3, 2, 7]
TRIPS = [trip(p, f, r, b) for p, f, r, b in zip(PNLS, FEES, RETS, BARS)]


class TestTradeMetrics(unittest.TestCase):
    """逐笔指标与手算逐位核对。"""

    def setUp(self):
        self.m = R.trade_metrics(TRIPS)

    def test_counts_and_win_rate(self):
        self.assertEqual(self.m["trades"], 5)
        self.assertEqual(self.m["wins"], 3)
        self.assertEqual(self.m["losses"], 2)
        self.assertEqual(self.m["flats"], 0)
        self.assertEqual(self.m["winRate"], 3.0 / 5.0)

    def test_gross_and_averages_hand_checked(self):
        gross_profit = sum(p for p in PNLS if p > 0)          # 450
        gross_loss = sum(p for p in PNLS if p < 0)            # -80
        avg_win = gross_profit / 3.0                          # 150
        avg_loss = gross_loss / 2.0                           # -40
        self.assertAlmostEqual(self.m["grossProfit"], gross_profit, places=9)
        self.assertAlmostEqual(self.m["grossLoss"], gross_loss, places=9)
        self.assertAlmostEqual(self.m["avgWin"], avg_win, places=9)
        self.assertAlmostEqual(self.m["avgLoss"], avg_loss, places=9)
        self.assertEqual(self.m["avgWin"], 150.0)
        self.assertEqual(self.m["avgLoss"], -40.0)

    def test_ratio_metrics_hand_checked(self):
        """payoffRatio = 平均盈利/|平均亏损|、profitFactor = Σ盈/Σ亏、expectancy = 平均每笔。"""
        avg_win, avg_loss = 150.0, -40.0
        self.assertEqual(self.m["payoffRatio"], round(avg_win / abs(avg_loss), 4))   # 3.75
        self.assertEqual(self.m["payoffRatio"], 3.75)
        self.assertEqual(self.m["profitFactor"], round(450.0 / 80.0, 4))             # 5.625
        self.assertEqual(self.m["profitFactor"], 5.625)
        self.assertEqual(self.m["expectancy"], round(sum(PNLS) / 5.0, 6))            # 74.0
        self.assertEqual(self.m["expectancy"], 74.0)
        self.assertEqual(self.m["totalPnl"], sum(PNLS))
        self.assertEqual(self.m["totalFee"], sum(FEES))
        self.assertEqual(self.m["largestWin"], max(PNLS))
        self.assertEqual(self.m["largestLoss"], min(PNLS))
        self.assertEqual(self.m["avgReturnPct"], round(sum(RETS) / 5.0, 3))          # 7.4
        self.assertEqual(self.m["avgReturnPct"], 7.4)

    def test_streaks_and_bars_hand_checked(self):
        """连续盈/亏与平均持仓周期（含「加仓后一次平仓」会拉长 bars 的语义）。"""
        self.assertEqual(self.m["maxConsecutiveWins"], 3)
        self.assertEqual(self.m["maxConsecutiveLosses"], 2)
        self.assertEqual(self.m["avgBarsHeld"], round(sum(BARS) / 5.0, 4))           # 5.4
        self.assertEqual(self.m["avgBarsHeld"], 5.4)

    def test_mae_mfe_are_none_with_warning(self):
        """MAE/MFE 需要盘中价格路径：如实返回 None 并在 warnings 说明。"""
        self.assertIsNone(self.m["mae"])
        self.assertIsNone(self.m["mfe"])
        self.assertTrue(any("MAE/MFE" in w for w in self.m["warnings"]))

    def test_sample_warning_and_threshold(self):
        """样本 < 30 给告警，≥30 不给；minSampleForConfidence 恒为 30。"""
        self.assertEqual(self.m["minSampleForConfidence"], 30)
        self.assertFalse(self.m["sampleEnough"])
        self.assertIn("样本量不足", self.m["sampleWarning"])
        enough = R.trade_metrics([trip(1.0, 0.0, 1.0, 1) for _ in range(30)])
        self.assertIsNone(enough["sampleWarning"])
        self.assertTrue(enough["sampleEnough"])
        self.assertEqual(enough["trades"], 30)
        # 29 笔仍然不足（边界）
        self.assertIsNotNone(R.trade_metrics([trip(1.0, 0.0, 1.0, 1) for _ in range(29)])["sampleWarning"])

    def test_flat_trades_count_in_win_rate_denominator(self):
        """平局（pnl = 0）既不算赢也不算亏，但计入 winRate 分母（fugazi 口径）。"""
        m = R.trade_metrics([trip(10.0, 0.0, 1.0, 1), trip(0.0, 0.0, 0.0, 1)])
        self.assertEqual((m["trades"], m["wins"], m["losses"], m["flats"]), (2, 1, 0, 1))
        self.assertEqual(m["winRate"], 0.5)

    def test_no_losers_returns_none_not_inf(self):
        """全部盈利：avgLoss / payoffRatio / profitFactor 都是 None（不是 0、不是 inf）。"""
        m = R.trade_metrics([trip(10.0, 0.0, 1.0, 1), trip(20.0, 0.0, 2.0, 1)])
        self.assertIsNone(m["avgLoss"])
        self.assertIsNone(m["payoffRatio"])
        self.assertIsNone(m["profitFactor"])
        self.assertEqual(m["winRate"], 1.0)
        self.assertEqual(m["largestLoss"], None)
        json.dumps(m, allow_nan=False)

    def test_no_winners_returns_none_payoff_and_zero_profit_factor(self):
        """全部亏损：avgWin / payoffRatio 为 None；profitFactor = 0/Σ亏 = 0.0（有限值）。"""
        m = R.trade_metrics([trip(-10.0, 0.0, -1.0, 1), trip(-20.0, 0.0, -2.0, 1)])
        self.assertIsNone(m["avgWin"])
        self.assertIsNone(m["payoffRatio"])
        self.assertEqual(m["profitFactor"], 0.0)
        self.assertEqual(m["winRate"], 0.0)
        self.assertEqual(m["largestWin"], None)
        json.dumps(m, allow_nan=False)

    def test_empty_sample_returns_none(self):
        """样本为 0：相关指标一律 None（不是 0），始终良定义的量才给 0。"""
        m = R.trade_metrics([])
        for key in ("winRate", "avgWin", "avgLoss", "payoffRatio", "profitFactor",
                    "expectancy", "largestWin", "largestLoss", "avgBarsHeld",
                    "avgReturnPct", "mae", "mfe", "sampleWarning", "returnOnCapital",
                    "equityChange", "pnlGap", "capital"):
            self.assertIsNone(m[key], key)
        self.assertEqual(m["trades"], 0)
        self.assertEqual(m["totalPnl"], 0.0)
        self.assertEqual(m["totalFee"], 0.0)
        self.assertEqual(m["maxConsecutiveWins"], 0)
        self.assertEqual(m["maxConsecutiveLosses"], 0)
        json.dumps(m, allow_nan=False)

    def test_dirty_trips_do_not_raise(self):
        """缺 pnl 的条目按 0 计入并告警；None / 非法输入不抛异常。"""
        m = R.trade_metrics([{"code": "A"}, trip(10.0, 0.0, 1.0, 1)])
        self.assertEqual(m["trades"], 2)
        self.assertEqual(m["totalPnl"], 10.0)
        self.assertTrue(any("缺少 pnl" in w for w in m["warnings"]))
        for bad in (None, [], "x", 42, [None, "s"]):
            self.assertEqual(R.trade_metrics(bad)["trades"], 0)

    def test_capital_return_and_equity_gap(self):
        """给本金算 returnOnCapital；给权益曲线算「权益变动 − 已实现盈亏」的差额并告警。"""
        m = R.trade_metrics(TRIPS, equity=[{"t": 1, "v": 10000.0}, {"t": 2, "v": 10370.0}],
                            capital=10000.0)
        self.assertEqual(m["returnOnCapital"], round(sum(PNLS) / 10000.0 * 100.0, 3))   # 3.7
        self.assertEqual(m["equityChange"], 370.0)
        self.assertEqual(m["pnlGap"], 0.0)
        self.assertFalse(any("不一致" in w for w in m["warnings"]))

        gap = R.trade_metrics(TRIPS, equity=[{"t": 1, "v": 10000.0}, {"t": 2, "v": 10500.0}])
        self.assertEqual(gap["pnlGap"], 130.0)                                          # 500 - 370
        self.assertTrue(any("不一致" in w for w in gap["warnings"]))

    def test_missing_bars_warning(self):
        """没有 bars 字段时 avgBarsHeld 为 None 并告警，而不是编一个「持仓天数」。"""
        m = R.trade_metrics([trip(10.0, 0.0, 1.0, None)])
        self.assertIsNone(m["avgBarsHeld"])
        self.assertTrue(any("bars" in w for w in m["warnings"]))

    def test_output_unit_note_mentions_both_conventions(self):
        """模块级口径说明必须区分逐笔 / 周期两套口径（前端原样展示）。"""
        self.assertIn("逐笔口径", R.METRIC_NOTE)
        self.assertIn("周期口径", R.METRIC_NOTE)
        self.assertIn("QuantStats", R.METRIC_NOTE)
        self.assertIn("MAE/MFE", R.METRIC_NOTE)
        self.assertIn("时间加权", R.METRIC_NOTE)
        self.assertIn("r_i = (E_i - F_i) / E_{i-1} - 1", R.FLOW_FORMULA)

    def test_required_keys_present(self):
        """对外契约：服务端依赖的逐笔字段一个都不能少（缺字段比字段为 null 更难排查）。"""
        required = ("trades", "wins", "losses", "winRate", "avgWin", "avgLoss",
                    "payoffRatio", "profitFactor", "expectancy", "largestWin",
                    "largestLoss", "maxConsecutiveWins", "maxConsecutiveLosses",
                    "avgBarsHeld", "totalPnl", "totalFee", "mae", "mfe")
        for key in required:
            self.assertIn(key, self.m, key)
        for key in required:
            self.assertIn(key, R.trade_metrics([]), "空样本也要给全字段：%s" % key)


# --------------------------------------------------------------------------- #
# 3. 周期口径指标
# --------------------------------------------------------------------------- #

class TestPeriodMetrics(unittest.TestCase):
    """周期口径：逐 bar 收益 → 各比率与回撤族指标。"""

    def test_total_return_and_annualized_at_one_year(self):
        """n = 252 个 bar（正好一年）时年化 == 总收益：(1+total)^(252/252) - 1。"""
        pts = curve_from_returns([0.001] * 252)
        vals = [p["v"] for p in pts]
        m = R.period_metrics(pts)
        total = (vals[-1] - vals[0]) / vals[0]
        self.assertEqual(m["returns"], 252)
        self.assertEqual(m["barsPerYear"], 252)
        self.assertEqual(m["totalReturn"], round(total, 4))
        self.assertEqual(m["annualized"], round(total, 4))
        self.assertFalse(m["annualizedTruncated"])

    def test_annualized_truncated_for_tiny_sample(self):
        """样本太短时年化外推会爆炸（2 个 bar 的 +21% ≈ 1e10）：如实返回 None 并标记。"""
        m = R.period_metrics(curve([100.0, 110.0, 121.0]))
        self.assertEqual(m["totalReturn"], 0.21)
        self.assertIsNone(m["annualized"])
        self.assertTrue(m["annualizedTruncated"])

    def test_sharpe_and_sortino_hand_checked(self):
        """Sharpe = mean/sd×√252（样本标准差）；Sortino 的下行标准差用 n 分母、MAR = 0。"""
        pts = curve_from_returns([0.01, -0.005, 0.02, 0.0])
        vals = [p["v"] for p in pts]
        # 与被测实现同源地「自己再算一遍」逐 bar 收益（曲线是累乘出来的，不能直接抄输入的小数）
        rets = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))]
        m = R.period_metrics(pts)
        mu = mean(rets)
        sharpe = mu / stdev(rets) * math.sqrt(252.0)
        sortino = mu / downsample_vol(rets) * math.sqrt(252.0)
        self.assertEqual(m["sharpe"], round(sharpe, 4))
        self.assertEqual(m["sortino"], round(sortino, 4))
        self.assertEqual(m["positiveBarsRatio"], 0.5)        # 2 正 / 4 个 bar
        self.assertEqual(m["meanReturn"], round(mu, 6))
        self.assertEqual(m["volatility"], round(stdev(rets) * math.sqrt(252.0), 4))
        self.assertGreater(m["sortino"], m["sharpe"])        # 下行波动 < 总波动

    def test_deep_drawdown_that_recovers_fast(self):
        """用例 A：回撤很深（-50%）但两个 bar 就收复。"""
        vals = [100.0, 50.0, 100.0, 100.0, 100.0]
        m = R.period_metrics(curve(vals))
        d = R.drawdown_detail(curve(vals))
        self.assertEqual(m["maxDrawdown"], 0.5)
        self.assertEqual(m["maxDrawdownDuration"], 2)        # 前高(0) → 收复(2)
        self.assertEqual(m["timeInDrawdownRatio"], 0.2)      # 只有 1 个 bar 在水下
        self.assertEqual(m["ulcerIndex"], round(math.sqrt(0.5 ** 2 / 5.0), 4))
        self.assertEqual(d["maxDrawdownSegment"]["depth"], 0.5)
        self.assertEqual(d["maxDrawdownSegment"]["durationBars"], 2)
        self.assertEqual(d["maxDrawdownSegment"]["underwaterBars"], 1)
        self.assertTrue(d["maxDrawdownSegment"]["recovered"])

    def test_shallow_drawdown_that_lasts_long(self):
        """用例 B：回撤只有 -1%，却在水下待了 6 个 bar（深度族与时间族各自的特征）。"""
        vals = [100.0, 100.0, 99.0, 99.0, 99.0, 99.0, 99.0, 100.0]
        m = R.period_metrics(curve(vals))
        self.assertEqual(m["maxDrawdown"], 0.01)
        self.assertEqual(m["maxDrawdownDuration"], 6)        # 前高(1) → 收复(7)
        self.assertEqual(m["timeInDrawdownRatio"], round(5.0 / 8.0, 4))
        self.assertEqual(m["ulcerIndex"], round(math.sqrt(5 * 0.01 ** 2 / 8.0), 4))

    def test_depth_and_duration_are_independent_dimensions(self):
        """同两组用例交叉断言：深的那次恢复更快，浅的那次水下更久。"""
        deep = R.period_metrics(curve([100.0, 50.0, 100.0, 100.0, 100.0]))
        shallow = R.period_metrics(curve([100.0, 100.0, 99.0, 99.0, 99.0, 99.0, 99.0, 100.0]))
        self.assertGreater(deep["maxDrawdown"], shallow["maxDrawdown"])
        self.assertLess(deep["maxDrawdownDuration"], shallow["maxDrawdownDuration"])
        self.assertLess(deep["timeInDrawdownRatio"], shallow["timeInDrawdownRatio"])

    def test_time_in_drawdown_and_ulcer_hand_checked(self):
        """逐 bar 手算：水下 bar 计数、Ulcer = sqrt(mean(dd²))、未收复段的处理。"""
        vals = [100.0, 90.0, 95.0, 105.0, 100.0]
        dds = [0.0, 90 / 100 - 1, 95 / 100 - 1, 0.0, 100 / 105 - 1]   # 相对各自 running peak
        expected_ulcer = math.sqrt(sum(d * d for d in dds) / 5.0)
        m = R.period_metrics(curve(vals))
        d = R.drawdown_detail(curve(vals))
        self.assertEqual(m["timeInDrawdownRatio"], round(3.0 / 5.0, 4))
        self.assertEqual(m["ulcerIndex"], round(expected_ulcer, 4))
        self.assertEqual(m["maxDrawdown"], 0.1)                       # 100 → 90
        self.assertEqual(m["maxDrawdownDuration"], 3)                 # 前高(0) → 新高(3)
        self.assertEqual(d["drawdownCount"], 2)                       # 段 1 已收复 + 段 2 未收复
        self.assertEqual([s["underwaterBars"] for s in d["segments"]], [2, 1])
        self.assertEqual([s["recovered"] for s in d["segments"]], [True, False])
        # 末值 100 仍低于历史最高 105 → 期末处于回撤中（1 − 100/105）
        self.assertEqual(d["currentDrawdown"], round(1 - 100.0 / 105.0, 4))
        self.assertEqual(d["segments"][1]["depth"], round(1 - 100 / 105, 4))

    def test_current_drawdown_and_unrecovered_segment(self):
        """期末仍未收复：currentDrawdown 是「末值相对历史最高」的正数幅度。"""
        d = R.drawdown_detail(curve([100.0, 80.0, 75.0]))
        self.assertEqual(d["maxDrawdown"], 0.25)
        self.assertEqual(d["maxDrawdownDuration"], 2)
        self.assertEqual(d["timeInDrawdownRatio"], round(2.0 / 3.0, 4))
        self.assertEqual(d["currentDrawdown"], 0.25)
        seg = d["maxDrawdownSegment"]
        self.assertFalse(seg["recovered"])
        self.assertIsNone(seg["recoveryIndex"])

    def test_monotone_curve_has_zero_drawdown_and_ulcer(self):
        """有数据且单调不降：深度族与时间族都真的是 0.0（不是 None）。"""
        m = R.period_metrics(curve([100.0, 101.0, 102.0, 103.0]))
        for key in ("maxDrawdown", "maxDrawdownDuration", "timeInDrawdownRatio", "ulcerIndex"):
            self.assertEqual(m[key], 0.0, key)
        self.assertEqual(m["drawdownCount"], 0)
        self.assertEqual(R.drawdown_segments([1.0, 2.0, 3.0]), [])

    def test_kelly_fraction_formula(self):
        """kellyFraction = p − (1−p)/b，p 与 b 都来自逐 bar（周期口径）。"""
        rets = [0.02, -0.01, 0.02, -0.01, 0.02]
        m = R.period_metrics(curve_from_returns(rets))
        self.assertEqual(m["positiveBarsRatio"], 0.6)
        self.assertEqual(m["kellyFraction"], round(0.6 - 0.4 / 2.0, 4))   # b = 0.02/0.01 = 2
        self.assertEqual(m["kellyFraction"], 0.4)
        # 直接核对工具函数（含 b 非正 / 未定义时返回 None 的边界）
        self.assertEqual(R.kelly_fraction(0.6, 2.0), 0.4)
        self.assertEqual(R.kelly_fraction(0.5, 1.0), 0.0)
        self.assertEqual(R.kelly_fraction(0.4, 1.0), -0.2)                # 允许为负 = 无优势
        self.assertIsNone(R.kelly_fraction(0.6, 0.0))
        self.assertIsNone(R.kelly_fraction(0.6, None))
        self.assertIsNone(R.kelly_fraction(None, 2.0))

    def test_calmar_and_helpers(self):
        """Calmar = 年化 / 最大回撤；回撤为 0 或年化缺失时 None。"""
        self.assertEqual(R.calmar(0.2, 0.1), 2.0)
        self.assertIsNone(R.calmar(0.2, 0.0))
        self.assertIsNone(R.calmar(None, 0.1))
        # 100 → 80（−20%）后横盘：最大回撤 = 0.2，年化 = 0.8^(252/251) − 1（复合口径）
        vals = [100.0] * 126 + [80.0] + [80.0] * 125
        m = R.period_metrics(curve(vals))
        expected_ann = math.expm1(math.log1p(-0.2) * (252.0 / 251.0))
        self.assertEqual(m["maxDrawdown"], 0.2)
        self.assertEqual(m["annualized"], round(expected_ann, 4))
        self.assertEqual(m["calmar"], round(expected_ann / 0.2, 4))
        self.assertIsNone(m["kellyFraction"])           # 没有上涨 bar → b 未定义 → None
        self.assertEqual(m["positiveBarsRatio"], 0.0)

    def test_no_equity_data_returns_none(self):
        """无权益数据：所有指标为 None 且不抛异常（0 会被误读成「真的很差」）。"""
        for bad in (None, [], [None, "x"], "nope", 42, [{}], [{"t": 1}]):
            m = R.period_metrics(bad)
            for key in ("totalReturn", "annualized", "sharpe", "sortino", "maxDrawdown",
                        "maxDrawdownDuration", "timeInDrawdownRatio", "ulcerIndex",
                        "calmar", "kellyFraction", "positiveBarsRatio"):
                self.assertIsNone(m[key], "%s for %r" % (key, bad))
            self.assertEqual(m["bars"], 0)
            json.dumps(m, allow_nan=False)
            d = R.drawdown_detail(bad)
            self.assertIsNone(d["maxDrawdown"])
            self.assertEqual(d["segments"], [])

    def test_zero_and_negative_equity_do_not_crash(self):
        """零 / 负权益是脏数据：不制造 inf/NaN，也不抛异常。"""
        for bad in ([0.0, 0.0, 0.0], [-5.0, -10.0], [100.0, 0.0], [1e9, 1e-9]):
            m = R.period_metrics(curve(bad))
            json.dumps(m, allow_nan=False)
            self.assertTrue(m["bars"] == len(bad))

    def test_single_point_curve(self):
        """只有一个点：没有收益序列可算，比率全 None，但 bars = 1。"""
        m = R.period_metrics(curve([100.0]))
        self.assertEqual((m["bars"], m["returns"]), (1, 0))
        self.assertIsNone(m["sharpe"])
        self.assertIsNone(m["totalReturn"])

    def test_required_keys_present(self):
        """对外契约：周期口径的字段集（含 sharpe ×√252、回撤族、kellyFraction）。"""
        required = ("totalReturn", "annualized", "sharpe", "sortino", "maxDrawdown",
                    "maxDrawdownDuration", "timeInDrawdownRatio", "ulcerIndex", "calmar",
                    "kellyFraction")
        m = R.period_metrics(curve([100.0, 99.0, 101.0]))
        for key in required:
            self.assertIn(key, m, key)
        self.assertEqual(m["barsPerYear"], 252)
        # drawdown_detail 的字段集
        for key in ("segments", "maxDrawdown", "maxDrawdownDuration", "timeInDrawdownRatio",
                    "ulcerIndex", "currentDrawdown", "drawdownCount", "averageDrawdown"):
            self.assertIn(key, R.drawdown_detail(curve([100.0, 99.0])))


# --------------------------------------------------------------------------- #
# 4. 出入金的时间加权处理
# --------------------------------------------------------------------------- #

class TestCacheFlow(unittest.TestCase):
    """r_i = (E_i − F_i)/E_{i-1} − 1（F 归属期末）。"""

    DEPOSIT = [{"t": 1, "v": 100.0}, {"t": 2, "v": 210.0}, {"t": 3, "v": 231.0}]

    def test_deposit_is_removed_and_equals_hand_value(self):
        """入金 100 的曲线：含出入金 +131%，剔除后 +21%（手算 = 1.10×1.10−1）。"""
        res = R.cache_flow_metrics(self.DEPOSIT, [{"ts": 2, "amount": 100.0}])
        self.assertTrue(res["detected"])
        self.assertEqual(res["flowCount"], 1)
        self.assertEqual(res["flowsTotal"], 100.0)
        self.assertEqual(res["rawReturn"], 1.31)
        self.assertEqual(res["totalReturn"], 0.21)
        self.assertEqual(res["difference"], 1.1)
        self.assertEqual([p["v"] for p in res["adjusted"]], [1.0, 1.1, 1.21])
        self.assertEqual(res["returns"], [0.1, 0.1])
        self.assertGreater(abs(res["difference"]), 0.5)          # 两套口径显著不同
        # 手算：r1 = (210-100)/100-1 = 0.10，r2 = 231/210-1 = 0.10
        self.assertAlmostEqual(res["totalReturn"], 0.1 * 1.0 + 0.1 * 1.1, places=6)
        self.assertEqual(res["period"]["totalReturn"], 0.21)
        self.assertEqual(res["rawPeriod"]["totalReturn"], 1.31)
        self.assertIn("时间加权", res["note"])

    def test_withdrawal_is_added_back(self):
        """出金 40：不剔除会显示 −34%，剔除后是 +10%（出金在曲线上与亏损长得一样）。"""
        equity = [{"t": 1, "v": 100.0}, {"t": 2, "v": 60.0}, {"t": 3, "v": 66.0}]
        res = R.cache_flow_metrics(equity, [{"ts": 2, "amount": -40.0}])
        self.assertTrue(res["detected"])
        self.assertEqual(res["rawReturn"], -0.34)
        self.assertEqual(res["totalReturn"], 0.1)                 # (60+40)/100-1 = 0，66/60-1 = 0.10
        self.assertEqual([p["v"] for p in res["adjusted"]], [1.0, 1.0, 1.1])
        self.assertEqual(res["outflowTotal"], -40.0)

    def test_flow_inputs_are_equivalent(self):
        """dict（时间戳字符串键）/ 逐点 flow 字段 / 等长数字列表 三种入参等价。"""
        by_dict = R.cache_flow_metrics(self.DEPOSIT, {"2": 100.0})
        by_index = R.cache_flow_metrics(self.DEPOSIT, [0.0, 100.0, 0.0])
        by_point = R.cache_flow_metrics([dict(p, flow=(100.0 if p["t"] == 2 else 0.0))
                                        for p in self.DEPOSIT])
        for res in (by_dict, by_index, by_point):
            self.assertTrue(res["detected"])
            self.assertEqual(res["totalReturn"], 0.21)
            self.assertEqual(res["rawReturn"], 1.31)

    def test_no_flow_detected_keeps_raw_convention(self):
        """未检测到出入金：两套口径相等，且 note 写明「未检测到 ≠ 没有」。"""
        res = R.cache_flow_metrics(self.DEPOSIT)
        self.assertFalse(res["detected"])
        self.assertEqual(res["flowCount"], 0)
        self.assertEqual(res["totalReturn"], res["rawReturn"])
        self.assertEqual(res["adjusted"][0]["v"], 1.0)
        self.assertIn("未检测到", res["note"])
        self.assertIn("不等于", res["note"])
        m = R.period_metrics(self.DEPOSIT)
        self.assertFalse(m["flowAdjusted"])
        self.assertEqual(m["flowNote"], "未检测到出入金")

    def test_period_metrics_honours_point_flows(self):
        """period_metrics 遇到逐点 flow 也会剔除，并在 flowNote 说明。"""
        pts = [dict(p, flow=(100.0 if p["t"] == 2 else 0.0)) for p in self.DEPOSIT]
        m = R.period_metrics(pts)
        self.assertTrue(m["flowAdjusted"])
        self.assertEqual(m["flowNote"], "已按时间加权剔除出入金")
        self.assertEqual(m["totalReturn"], 0.21)          # 剔除后
        self.assertEqual(m["rawTotalReturn"], 1.31)       # 含出入金的原始口径同时保留

    def test_unmatched_flows_are_reported(self):
        """对不上的资金流（时间戳不在曲线上）如实回报，不静默当成已剔除。"""
        res = R.cache_flow_metrics(self.DEPOSIT, [{"ts": 999, "amount": 50.0}])
        self.assertFalse(res["detected"])
        self.assertEqual(len(res["unmatchedFlows"]), 1)
        self.assertEqual(res["unmatchedFlows"][0]["ts"], 999)
        self.assertEqual(res["totalReturn"], res["rawReturn"])
        wrong_len = R.cache_flow_metrics(self.DEPOSIT, [1.0, 2.0])
        self.assertFalse(wrong_len["detected"])
        self.assertTrue(wrong_len["unmatchedFlows"])

    def test_dirty_inputs_do_not_raise(self):
        """脏输入（None / 非法类型 / flow 缺金额）不抛异常。"""
        for bad in (None, [], "x", 42, [{"t": 1, "v": None}], [{"flow": 1}]):
            res = R.cache_flow_metrics(bad)
            self.assertIn("detected", res)
            json.dumps(res, allow_nan=False)
        res = R.cache_flow_metrics(self.DEPOSIT, [{"ts": 2}, "x", None])
        self.assertFalse(res["detected"])
        self.assertEqual(len(res["unmatchedFlows"]), 3)


# --------------------------------------------------------------------------- #
# 5. AI 记录分组
# --------------------------------------------------------------------------- #

ROWS = [
    {"code": "A", "action": "buy", "verdict": "hit", "verdictReturn": 5.0},
    {"code": "B", "action": "buy", "verdict": "miss", "verdictReturn": -2.0},
    {"code": "C", "action": "buy", "sinceReturn": 3.0},                  # 推导口径
    {"code": "D", "action": "add", "verdict": "hit", "verdictReturn": 1.0},
    {"code": "E", "action": "reduce", "verdict": "hit", "verdictReturn": -4.0},
    {"code": "F", "action": "sell", "verdict": "miss", "verdictReturn": 2.0},
    {"code": "G", "action": "sell", "verdict": "hit", "verdictReturn": -1.0},
    {"code": "H", "action": "avoid", "verdict": "hit", "verdictReturn": -3.0},
    {"code": "I", "action": "hold", "sinceReturn": 10.0},                # 中性：只记录涨跌
    {"code": "J", "action": "watch", "verdict": "pending"},
    {"code": "K", "action": "buy", "verdict": "pending", "sinceReturn": 99.0},
    {"code": "L", "action": "buy", "verdict": "nodata"},
    {"code": "M"},                                                        # 无档位
]
RECORD = {"id": "ar-1", "createdAt": 1000, "rows": ROWS}


class TestAdvisorBreakdown(unittest.TestCase):
    """分组统计：7 档全给、看多/看空/中性三组、小样本只标注不排名。"""

    def setUp(self):
        self.b = R.advisor_breakdown([RECORD])

    def test_seven_action_keys_always_present(self):
        """7 个档位的键始终给全（前端不会读到 undefined），另加 unknown 兜底。"""
        for act in ("buy", "add", "hold", "reduce", "sell", "watch", "avoid"):
            self.assertIn(act, self.b["byAction"])
            self.assertIn("hitRate", self.b["byAction"][act])
        self.assertIn("unknown", self.b["byAction"])
        self.assertEqual(self.b["byAction"]["buy"]["label"], "买入")

    def test_action_counts_and_hit_rates(self):
        """按档位统计：buy 4 判定（3 中）/ add 1 判定 / sell 2 判定（1 中）/ 其余各 1。"""
        buy = self.b["byAction"]["buy"]
        self.assertEqual(buy["count"], 5)          # A B C K L
        self.assertEqual(buy["graded"], 3)         # K(pending) 与 L(nodata) 不参与
        self.assertEqual(buy["hits"], 2)           # A(hit) + C(推导 hit)，B 是 miss
        self.assertEqual(buy["hitRate"], round(2 / 3.0, 4))
        self.assertEqual(buy["avgReturn"], round((5.0 - 2.0 + 3.0) / 3.0, 3))
        sell = self.b["byAction"]["sell"]
        self.assertEqual((sell["graded"], sell["hits"], sell["hitRate"]), (2, 1, 0.5))
        self.assertEqual(self.b["byAction"]["add"]["hitRate"], 1.0)
        self.assertEqual(self.b["byAction"]["unknown"]["count"], 1)
        self.assertIsNone(self.b["byAction"]["unknown"]["hitRate"])

    def test_bull_bear_neutral_groups(self):
        """看多组（buy+add）4 判定 3 中；看空组（reduce+sell+avoid）4 判定 3 中；中性不判定。"""
        self.assertEqual(sorted(self.b["byGroup"]["bull"]["actions"]), ["add", "buy"])
        self.assertEqual(sorted(self.b["byGroup"]["bear"]["actions"]),
                         ["avoid", "reduce", "sell"])
        self.assertEqual(self.b["byGroup"]["bull"]["graded"], 4)
        self.assertEqual(self.b["byGroup"]["bull"]["hits"], 3)
        self.assertEqual(self.b["bullHitRate"], 0.75)
        self.assertEqual(self.b["byGroup"]["bear"]["graded"], 4)
        self.assertEqual(self.b["byGroup"]["bear"]["hits"], 3)
        self.assertEqual(self.b["bearHitRate"], 0.75)
        neutral = self.b["byGroup"]["neutral"]
        self.assertEqual(neutral["count"], 2)
        self.assertEqual(neutral["graded"], 0)
        self.assertIsNone(neutral["hitRate"])
        self.assertIn("中性档位", neutral["note"])

    def test_neutral_actions_record_return_but_not_hit(self):
        """hold/watch 只记录涨跌：avgReturn 有值、hitRate 为 None（与 advisor 口径一致）。"""
        hold = self.b["byAction"]["hold"]
        self.assertEqual(hold["avgReturn"], 10.0)        # 中性档位也记录实际涨跌
        self.assertIsNone(hold["hitRate"])
        self.assertFalse(hold["rankable"])
        watch = self.b["byAction"]["watch"]
        self.assertIsNone(watch["hitRate"])
        self.assertIsNone(watch["avgReturn"])            # 未到期且没有收益记录 → None 而不是 0

    def test_small_sample_groups_are_labelled_and_not_ranked(self):
        """样本 < 5 的分组标注「样本过少」且不参与排名（ranking 为空）。"""
        buy = self.b["byAction"]["buy"]
        self.assertTrue(buy["smallSample"])
        self.assertFalse(buy["rankable"])
        self.assertIn("样本过少", buy["note"])
        self.assertEqual(self.b["ranking"], [])
        self.assertEqual(self.b["minGroupSample"], 5)

    def test_ranking_only_contains_rankable_groups(self):
        """6 笔 buy（5 中 1 负）达到样本门槛 → 进排名；2 笔 sell 仍不进。"""
        rows = [{"code": "c%d" % i, "action": "buy",
                 "verdict": "hit" if i < 5 else "miss", "verdictReturn": 1.0 if i < 5 else -1.0}
                for i in range(6)]
        rows += [{"code": "s1", "action": "sell", "verdict": "hit", "verdictReturn": -1.0},
                 {"code": "s2", "action": "sell", "verdict": "miss", "verdictReturn": 1.0}]
        b = R.advisor_breakdown([{"id": "r", "rows": rows}])
        buy = b["byAction"]["buy"]
        self.assertEqual((buy["graded"], buy["hits"]), (6, 5))
        self.assertTrue(buy["rankable"])
        self.assertFalse(b["byAction"]["sell"]["rankable"])
        keys = [r["key"] for r in b["ranking"]]
        self.assertIn("buy", keys)          # 6 笔 buy 达到门槛
        self.assertIn("bull", keys)         # 看多组 = 同一批 buy，同样达到门槛
        self.assertNotIn("sell", keys)      # 2 笔 sell 未达门槛 → 不给排名
        self.assertEqual(b["ranking"][0]["hitRate"], round(5 / 6.0, 4))

    def test_pending_and_nodata_are_never_guessed(self):
        """pending（未走满窗口）/ nodata 即便带 sinceReturn 也不补判（否则会虚增命中）。"""
        b = R.advisor_breakdown([{"id": "r", "rows": [
            {"code": "K", "action": "buy", "verdict": "pending", "sinceReturn": 99.0},
            {"code": "L", "action": "buy", "verdict": "nodata", "sinceReturn": 99.0},
        ]}])
        self.assertEqual(b["graded"], 0)
        self.assertEqual(b["byAction"]["buy"]["hitRate"], None)
        self.assertEqual(b["byBasis"]["pending"], 1)
        self.assertEqual(b["byBasis"]["nodata"], 1)
        self.assertTrue(any("pending" in w for w in b["warnings"]))

    def test_by_basis_and_warnings(self):
        """byBasis 说明每条明细用了哪套判定；混用两套口径必须告警。"""
        self.assertEqual(self.b["byBasis"]["verdict"], 7)
        self.assertEqual(self.b["byBasis"]["sinceReturn"], 1)
        self.assertEqual(self.b["byBasis"]["neutral"], 2)     # hold + watch
        self.assertEqual(self.b["byBasis"]["pending"], 1)     # 只剩非中性的 K
        self.assertEqual(self.b["byBasis"]["nodata"], 1)
        self.assertEqual(self.b["byBasis"]["no-action"], 1)
        self.assertEqual((self.b["graded"], self.b["ungraded"]), (8, 5))
        # byVerdict 是按 advisor.review 的 verdict 原样计数（与档位口径分开）
        self.assertEqual(self.b["byVerdict"]["pending"], 2)   # J + K，中性档位也照实计数
        self.assertEqual(self.b["byVerdict"]["hit"], 5)
        self.assertTrue(any("sinceReturn" in w for w in self.b["warnings"]))
        self.assertTrue(any("pending" in w for w in self.b["warnings"]))

    def test_records_without_verdicts_get_distribution_only(self):
        """store 里的记录没有复盘判定（需要行情）→ 只给分布，命中率一律 None 并告警。"""
        rec = {"id": "ar-2", "rows": [{"code": "A", "action": "buy"},
                                      {"code": "B", "action": "sell"}]}
        b = R.advisor_breakdown([rec])
        self.assertEqual(b["byAction"]["buy"]["count"], 1)
        self.assertIsNone(b["byAction"]["buy"]["hitRate"])
        self.assertEqual(b["graded"], 0)
        self.assertTrue(any("行情" in w for w in b["warnings"]))
        self.assertEqual(b["ranking"], [])

    def test_strategy_breakdown_is_proxy_convention(self):
        """策略分组：用标的复盘收益近似信号前瞻收益（代理口径），并标注样本量。"""
        rows = [{"code": "A", "action": "buy", "verdict": "hit", "verdictReturn": 5.0,
                 "advisor": {"marks": [{"strategy": "ma_cross", "dir": 1, "t": "2024-01-01"},
                                       {"strategy": "rsi", "dir": -1, "t": "2024-01-01"}]}}]
        b = R.advisor_breakdown([{"id": "r", "rows": rows}])
        self.assertEqual(b["byStrategy"]["ma_cross"]["hitRate"], 1.0)
        self.assertEqual(b["byStrategy"]["rsi"]["hitRate"], 0.0)      # 看空但涨了 → 未命中
        self.assertEqual(b["byStrategy"]["rsi"]["hits"], 0)
        self.assertEqual(b["byStrategy"]["rsi"]["basis"], "proxy")
        self.assertTrue(b["byStrategy"]["rsi"]["smallSample"])
        self.assertIn("样本过少", b["byStrategy"]["rsi"]["note"])

    def test_dirty_input_does_not_raise(self):
        """None / 非法类型 / rows 里混非 dict：ok=False 或跳过，绝不抛异常。"""
        for bad in (None, [], "x", 42, [None], {"rows": [None, 3]}):
            b = R.advisor_breakdown(bad)
            self.assertIn("byAction", b)
            self.assertIn("byGroup", b)
            json.dumps(b, allow_nan=False)
        single = R.advisor_breakdown({"action": "buy", "verdict": "hit", "verdictReturn": 1.0})
        self.assertEqual((single["records"], single["graded"]), (1, 1))
        self.assertFalse(R.advisor_breakdown(None)["ok"])


# --------------------------------------------------------------------------- #
# 6. 汇总入口（端到端，Store(":memory:")）
# --------------------------------------------------------------------------- #

class TestReviewReport(unittest.TestCase):
    """服务端按此调用：review_report(store, market=None, days=None)。"""

    def setUp(self):
        self.store = Store(":memory:")
        self.aid = T.account_id("cn", "paper")
        self.now = now_ms()

    def tearDown(self):
        self.store.close()

    def save(self, **over):
        o = order(**over)
        self.store.save_trade_order(o)
        return o

    def equity(self, ts, value):
        self.store.append_trade_equity({"accountId": self.aid, "ts": ts, "cash": value,
                                        "marketValue": 0.0, "equity": value, "pnl": 0.0})

    def test_contract_and_numbers_end_to_end(self):
        """一笔完整交易 + 三个权益点：检查 report 的契约与手算数字。"""
        self.store.save_trade_state({"accountId": self.aid, "market": "cn", "mode": "paper",
                                     "cash": 100000.0, "initial": 100000.0})
        self.save(side="buy", qty=100, fillPrice=10.0, fee=5.0, filledAt=self.now - 2000)
        self.save(side="sell", qty=100, fillPrice=12.0, fee=6.0, source="manual",
                  filledAt=self.now - 1000)
        self.equity(self.now - 2000, 100000.0)
        self.equity(self.now - 1000, 100189.0)
        self.equity(self.now, 100189.0)

        rep = R.review_report(self.store, market="cn")
        for key in ("ok", "window", "metrics", "period", "drawdown", "byAdvisor",
                    "bySource", "note", "warnings"):
            self.assertIn(key, rep)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["note"], R.METRIC_NOTE)
        for key in ("from", "to", "days"):
            self.assertIn(key, rep["window"])
        self.assertIsNone(rep["window"]["days"])

        m = rep["metrics"]
        self.assertEqual(m["trades"], 1)
        self.assertEqual(m["totalPnl"], 189.0)
        self.assertEqual(m["totalFee"], 11.0)
        self.assertEqual(m["returnOnCapital"], round(189.0 / 100000.0 * 100.0, 3))
        self.assertEqual(m["equityChange"], 189.0)
        self.assertEqual(m["pnlGap"], 0.0)
        self.assertEqual(m["flowNote"], "未检测到出入金")

        p = rep["period"]
        self.assertEqual(p["bars"], 3)
        self.assertEqual(p["totalReturn"], round(189.0 / 100000.0, 4))
        self.assertEqual(p["positiveBarsRatio"], 0.5)         # +0.189% 与 0.0% 各一个 bar
        # 权益曲线单调不降 → 回撤族真的是 0（不是 None），Ulcer 也是 0
        self.assertEqual(p["ulcerIndex"], 0.0)
        self.assertEqual(p["maxDrawdownDuration"], 0)
        self.assertFalse(rep["drawdown"]["onAdjustedCurve"])
        self.assertEqual(rep["drawdown"]["maxDrawdown"], 0.0)

        self.assertEqual(rep["bySource"]["ai"]["trades"], 1)
        self.assertEqual(rep["bySource"]["manual"]["trades"], 0)
        for key in ("ai", "manual", "scheduled"):
            self.assertIn(key, rep["bySource"])
        self.assertEqual(rep["bySource"]["ai"]["totalPnl"], 189.0)

        self.assertTrue(all(isinstance(w, str) for w in rep["warnings"]))
        self.assertTrue(any("MAE/MFE" in w for w in rep["warnings"]))
        self.assertTrue(any("样本量不足" in w for w in rep["warnings"]))
        json.dumps(rep, allow_nan=False)

    def test_days_window_filters_orders(self):
        """days 只保留窗口内的成交；非法 days 回退「全部时间」并告警。"""
        self.save(side="buy", qty=100, fillPrice=10.0, filledAt=self.now - 10 * DAY_MS)
        self.save(side="sell", qty=100, fillPrice=11.0, filledAt=self.now - 10 * DAY_MS + 1000)
        self.save(side="buy", qty=100, fillPrice=20.0, filledAt=self.now - 2000)
        self.save(side="sell", qty=100, fillPrice=22.0, filledAt=self.now - 1000)

        all_time = R.review_report(self.store, days=None)
        self.assertEqual(all_time["metrics"]["trades"], 2)
        self.assertEqual(all_time["window"]["days"], None)

        recent = R.review_report(self.store, days=5)
        self.assertEqual(recent["metrics"]["trades"], 1)
        self.assertEqual(recent["metrics"]["totalPnl"], 200.0)
        self.assertEqual(recent["window"]["days"], 5)
        self.assertIsNotNone(recent["window"]["fromDate"])

        as_str = R.review_report(self.store, days="7")
        self.assertEqual(as_str["window"]["days"], 7)
        self.assertEqual(as_str["metrics"]["trades"], 1)

        bad = R.review_report(self.store, days="abc")
        self.assertIsNone(bad["window"]["days"])
        self.assertEqual(bad["metrics"]["trades"], 2)
        self.assertTrue(any("days=" in w for w in bad["warnings"]))

    def test_flow_detection_removes_deposit(self):
        """一笔入金（cash 类委托）让原始收益 +1.189%，剔除后只剩 +0.187%。"""
        t0, t1, t2 = self.now - 3000, self.now - 2000, self.now - 1000
        self.save(side="buy", qty=100, fillPrice=10.0, fee=5.0, filledAt=t0)
        self.save(side="sell", qty=100, fillPrice=12.0, fee=6.0, filledAt=t2)
        self.save(id="dep-1", side="in", intent="deposit", qty=0, fillPrice=None,
                  amount=1000.0, filledAt=t1)
        self.equity(t0, 100000.0)
        self.equity(t1, 101000.0)          # 入金 1000，无交易盈亏
        self.equity(t2, 101189.0)          # 交易盈亏 189

        rep = R.review_report(self.store, market="cn")
        self.assertTrue(rep["flow"]["detected"])
        self.assertEqual(rep["flow"]["flowCount"], 1)
        self.assertIn("剔除出入金", rep["metrics"]["flowNote"])
        self.assertTrue(rep["drawdown"]["onAdjustedCurve"])
        self.assertTrue(any("出入金" in w for w in rep["warnings"]))
        raw = (101189.0 - 100000.0) / 100000.0
        adjusted = 101189.0 / 101000.0 - 1.0
        self.assertEqual(rep["flow"]["rawReturn"], round(raw, 4))
        self.assertEqual(rep["flow"]["totalReturn"], round(adjusted, 4))
        self.assertEqual(rep["period"]["totalReturn"], round(adjusted, 4))
        self.assertNotEqual(rep["flow"]["totalReturn"], rep["flow"]["rawReturn"])
        self.assertEqual(rep["metrics"]["trades"], 1)          # 出入金单不参与配对
        self.assertEqual(rep["skipped"], [])

    def test_no_equity_data_period_is_none(self):
        """只有委托单没有权益曲线：周期口径全 None + 告警，逐笔仍然可用。"""
        self.save(side="buy", qty=100, fillPrice=10.0, filledAt=self.now - 2000)
        self.save(side="sell", qty=100, fillPrice=12.0, filledAt=self.now - 1000)
        rep = R.review_report(self.store)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["metrics"]["trades"], 1)
        for key in ("totalReturn", "sharpe", "maxDrawdown", "ulcerIndex", "calmar"):
            self.assertIsNone(rep["period"][key], key)
        self.assertIsNone(rep["drawdown"]["maxDrawdown"])
        self.assertTrue(any("权益曲线" in w for w in rep["warnings"]))
        json.dumps(rep, allow_nan=False)

    def test_open_leg_and_skipped_warnings(self):
        """只有开仓未平仓 / 有脏委托：如实告警，不静默丢单。"""
        self.save(side="buy", qty=100, fillPrice=10.0, filledAt=self.now - 1000)
        self.save(side="buy", qty=100, fillPrice="", code="", filledAt=self.now - 900)
        rep = R.review_report(self.store)
        self.assertEqual(rep["metrics"]["trades"], 0)
        self.assertEqual(len(rep["openLegs"]), 1)
        self.assertEqual(len(rep["skipped"]), 1)
        self.assertTrue(any("尚未平仓" in w for w in rep["warnings"]))
        self.assertTrue(any("无法参与配对" in w for w in rep["warnings"]))

    def test_empty_store_and_none_store(self):
        """空库 ok=False + 告警；store=None 直接返回 ok=False 而不是抛异常。"""
        empty = R.review_report(self.store)
        self.assertFalse(empty["ok"])
        self.assertTrue(any("没有已成交委托单" in w for w in empty["warnings"]))
        self.assertEqual(empty["metrics"]["trades"], 0)
        none_store = R.review_report(None)
        self.assertFalse(none_store["ok"])
        self.assertIn("store=None", none_store["warnings"][0])
        for key in ("window", "metrics", "period", "drawdown", "byAdvisor", "bySource",
                    "note", "warnings"):
            self.assertIn(key, none_store)
        json.dumps(none_store, allow_nan=False)

    def test_store_without_trade_methods_degrades(self):
        """存储层缺方法 / 抛异常：降级为 warnings，不影响其它部分。"""
        class Broken(object):
            def list_trade_orders(self, **kw):
                raise RuntimeError("db gone")

            def get_trade_config(self):
                return {}

        rep = R.review_report(Broken())
        self.assertFalse(rep["ok"])
        self.assertTrue(any("委托单" in w for w in rep["warnings"]))
        self.assertIsNone(rep["metrics"].get("capital"))
        json.dumps(rep, allow_nan=False)

    def test_by_advisor_uses_stored_records(self):
        """store 里的 AI 记录只提供档位分布（命中判定要行情，故 hitRate 为 None）。"""
        rec = {"run": {"id": "ar-rep", "createdAt": self.now, "createdDate": "2024-01-01",
                       "market": "cn", "horizon": 5, "source": "list",
                       "summary": {"actions": {"buy": 1}, "codes": [], "topRows": []}},
               "rows": [{"code": "600000", "name": "浦发银行", "market": "cn",
                         "action": "buy", "price": 10.0, "kelly": {"weight": 0.1}}]}
        self.store.save_advisor_run(rec)
        rep = R.review_report(self.store)
        self.assertEqual(rep["byAdvisor"]["items"], 1)
        self.assertEqual(rep["byAdvisor"]["byAction"]["buy"]["count"], 1)
        self.assertIsNone(rep["byAdvisor"]["byAction"]["buy"]["hitRate"])
        self.assertTrue(any("行情" in w for w in rep["warnings"]))
        self.assertTrue(rep["ok"])            # 有 AI 记录也算「读到了数据」

    def test_market_defaults_to_config(self):
        """未传 market 时按交易配置的市场取账户，不抛异常。"""
        self.store.save_trade_config({"market": "us", "mode": "paper"})
        us_aid = T.account_id("us", "paper")
        self.store.append_trade_equity({"accountId": us_aid, "ts": self.now - 1000,
                                        "equity": 100000.0})
        self.store.append_trade_equity({"accountId": us_aid, "ts": self.now,
                                        "equity": 101000.0})
        rep = R.review_report(self.store)
        self.assertEqual(rep["market"], "us")
        self.assertEqual(rep["period"]["bars"], 2)
        self.assertEqual(rep["period"]["totalReturn"], 0.01)


# --------------------------------------------------------------------------- #
# 7. JSON 严格性（不允许 inf / NaN）
# --------------------------------------------------------------------------- #

class TestJsonStrictness(unittest.TestCase):
    """所有对外结构的比率都经 _r() 收口：0 分母 → None，绝不产出 inf/NaN。"""

    def assert_json_safe(self, obj, label):
        try:
            json.dumps(obj, allow_nan=False, ensure_ascii=False)
        except (ValueError, TypeError) as exc:      # pragma: no cover - 失败时才走这里
            self.fail("%s 无法严格序列化：%s" % (label, exc))

    def test_all_endpoints_are_json_safe(self):
        self.assert_json_safe(R.trade_metrics(TRIPS), "trade_metrics")
        self.assert_json_safe(R.trade_metrics([]), "trade_metrics(empty)")
        self.assert_json_safe(R.period_metrics([100.0, 101.0]), "period_metrics")
        self.assert_json_safe(R.period_metrics([]), "period_metrics(empty)")
        self.assert_json_safe(R.drawdown_detail([100.0, 50.0, 100.0]), "drawdown_detail")
        self.assert_json_safe(R.drawdown_detail([]), "drawdown_detail(empty)")
        self.assert_json_safe(R.advisor_breakdown([RECORD]), "advisor_breakdown")
        self.assert_json_safe(R.cache_flow_metrics([{"t": 1, "v": 1.0}]), "cache_flow_metrics")
        self.assert_json_safe({}, "empty")

    def test_no_inf_or_nan_recursively(self):
        """递归检查返回值里没有 inf / NaN（allow_nan=False 已经能兜住，但信息更明确）。"""
        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, "%s.%s" % (path, k))
            elif isinstance(node, (list, tuple)):
                for i, v in enumerate(node):
                    walk(v, "%s[%d]" % (path, i))
            elif isinstance(node, float):
                self.assertTrue(math.isfinite(node), "%s = %r 不是有限值" % (path, node))

        walk(R.trade_metrics([]), "metrics")
        walk(R.period_metrics([100.0, 0.0, 100.0]), "period")
        walk(R.drawdown_detail([0.0, 0.0]), "drawdown")

    def test_profit_factor_never_infinite(self):
        """唯一的 inf 来源（无亏损时的 profitFactor）必须是 None。"""
        m = R.trade_metrics([trip(1.0, 0.0, 1.0, 1)])
        self.assertIsNone(m["profitFactor"])
        self.assertIsNone(R.profit_factor(100.0, 0.0))
        self.assertIsNone(R.payoff_ratio(100.0, 0.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
