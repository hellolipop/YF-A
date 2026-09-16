#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易规则引擎（core/rules.py）的单元测试。

覆盖范围（与需求逐条对应）
--------------------------
A. TestBoardOf                 板块识别：主板 / 创业板 / 科创板 / 北交所 / 美股，
                               以及「代码无法识别时回落到主板」这条保守口径；
B. TestLimitPrices             涨跌停价：逐位相等、两个边界补丁、四舍五入到分（不是银行家舍入）、
                               缺少基准价时**不臆造数字**、兼容 ``levels._limit_hint`` 的
                               ``limit_prices(code, price)`` 调用形态；
C. TestLimitOf                 不设涨跌幅的四种情形（新股前 5 日 / 北交所首日 / 退市整理首日 / 美股）；
D. TestFeeOf                   费用逐项：佣金最低 5 元、印花税仅卖出单边、过户费与经手费双向、
                               北交所经手费 0.0125% 与沪深 0.00341% 不同；
E. TestFeeMinCommissionRegression  **已修复缺陷 F1 的回归**：命中最低佣金时，最低值必须写进
                               佣金项的 amount 并计入 total（1 万元买入 5.441 / 卖出 10.441），
                               同时保留 rateAmount 这个审计字段（「按费率本来要收多少」）；
F. TestSession                 时段：开盘集合竞价（撤单窗口）/ 静默期 / 连续竞价 / 午休 /
                               收盘竞价（不可撤单）/ 盘后固定价格 / 周末 / 美股；
G. TestLotAndT1                最小申报单位、整手取整、零股整笔卖出、T+1 可卖数量；
H. TestCheckOrder              下单校验：9 类拒单场景逐条断言 + 正常单 + warnings + checks 结构；
I. TestCheckOrderCashRegression **缺陷 F6 的回归**：``cash`` 无法解析（""/"abc"/{}）时不再抛
                               TypeError，也不再「告警 + 放行」，而是**拒单**（``ok=False`` +
                               中文 rejects + ``checks`` 里该项 ``pass=False``），字段齐全；
J. TestCanFill                 撮合可行性：涨停买不到、跌停卖不出、停牌不成交、美股照价成交；
K. TestRulesTable              规则总表结构 + json 安全 + 版本串体现 2026-07-06 的两项新规；
L. TestDirtyInput              脏输入（None / 空串 / 非 dict / 负价 / 字符串数字 / NaN / meta 脏）
                               对所有公开函数都不抛异常；
M. TestTsUnitRegression        **已修复缺陷 F3/F4 的回归**：``ts`` 收到脏值（"" / "abc" / NaN /
                               {} / [] / 1e30）退回「现在」且不抛异常；同一时刻的秒与毫秒两种
                               写法给出同一结论。

expectedFailure 现状（本轮已清零）
H. TestCheckOrder.test_unparseable_cash_is_rejected 与
   TestCheckOrderCashRegression.test_unparseable_cash_is_rejected 都是**常规回归用例**：
   「无法解析的资金必须拒单（ok=False）」这条更严的口径已实现 —— 资金是下单的硬约束，
   解析不出来就不能当成「通过」，否则「资金够不够」这道闸门会被静默跳过。

设计原则（为什么这样断言）
--------------------------
· **确定性**：时段用例全部用固定日期（2026-09-17 周四、2026-09-19 周六）构造时间戳，
  不依赖运行时刻、不依赖本机时区 —— 否则「现在是否开市」会随机器设置漂移；
· **逐位核对**：价格与金额一律 ``assertEqual`` 或 ``delta=1e-9``，绝不放宽成「大致相等」。
  交易规则错误是**静默**的（不抛异常，只让收益算错），放宽断言等于放弃唯一那层保护；
· **既断言「是什么」也断言「为什么」**：docstring 写清这条规则在实盘里的后果，
  而不是复述被测代码；
· **``ts`` 的单位**：``core/rules.py`` 用 ``_stamp_seconds`` 统一收口，秒与毫秒都能接：
  **先判单位**（``> 1e11`` 视为毫秒 → ``/1000``）**再判范围**（``> 4.2e9`` 秒 ≈ 2103 年视为
  越界），只有脏值（"" / "abc" / NaN / {} / [] / 1e30）才退回「现在」。
  因此**历史**毫秒时间戳现在会被正确换算成它代表的时刻（本轮修复：此前上界写成 4e10，
  会先把真实毫秒判成越界、静默换成「现在」，毫秒分支实际不可达）。
  ``core/trader.py`` 全链路传毫秒（``now_ms()``），换算后与秒口径一致。
  这个单位不一致曾是缺陷 F3/F4（毫秒被当秒 → ``ValueError: year 58679 is out of range``，
  美股模拟成交直接 500），现已修复；回归见 :class:`TestTsUnitRegression` 与
  ``tests/test_trader.py::TestTraderRulesRegression``。

运行方式::
    python3 tests/test_rules.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import datetime
import json
import math
import os
import re
import sys
import time
import unittest
from unittest import mock

# 让测试既能在 stock-terminal/ 下跑，也能在仓库根目录下跑
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import rules as R                          # noqa: E402

# --------------------------------------------------------------------------- #
# 常量与手算公式（与 core/rules.DEFAULT_FEES / DEFAULT_RULE_PARAMS 同源，
# 故意在测试里重写一遍：断言必须能脱离实现自证）
# --------------------------------------------------------------------------- #
COMMISSION_RATE = 0.00025      # 佣金万 2.5（双向，≤3‰ 上限内）
COMMISSION_MIN = 5.0           # 佣金不足 5 元按 5 元
STAMP_RATE = 0.0005            # 印花税 0.05%（仅卖出单边）
TRANSFER_RATE = 0.00001        # 过户费 0.001%（双向）
HANDLING_RATE = 0.0000341      # 经手费 0.00341%（沪深，双向）
HANDLING_RATE_BSE = 0.000125   # 经手费 0.0125%（北交所，双向）

#: 固定基准日：2026-09-17 是周四、2026-09-19 是周六（断言里反复引用，集中定义避免漂移）
THU = (2026, 9, 17)
SAT = (2026, 9, 19)

BJ = datetime.timezone(datetime.timedelta(hours=8))
#: 美东夏令时（4–10 月）UTC−4 —— 与 rules._local_minutes 的粗略口径一致
EDT = datetime.timezone(datetime.timedelta(hours=-4))


def cn_ts(hour, minute, day=THU):
    """北京时间 ``hour:minute`` → **秒级**时间戳（rules 的 ts 口径）。"""
    return int(datetime.datetime(day[0], day[1], day[2], hour, minute, tzinfo=BJ).timestamp())


def us_ts(hour, minute, day=THU):
    """美东时间 ``hour:minute`` → **秒级**时间戳（夏令时 UTC−4）。"""
    return int(datetime.datetime(day[0], day[1], day[2], hour, minute, tzinfo=EDT).timestamp())


def itemized_cn(side, qty, price, board="main", apply_min=True):
    """A 股逐项费用手算（元）：

    佣金 ``notional×0.00025``（**不足 5 元按 5 元**）+ 印花税（仅卖出）``notional×0.0005``
    + 过户费 ``notional×0.00001`` + 经手费（沪深 0.0000341 / 北交所 0.000125），
    每项四舍五入到 0.0001 元后求和。

    ``apply_min=False`` 复刻**修复前的错误口径**（缺陷 F1：命中最低值时佣金项记的仍是按
    费率算出的数、最低值没进 total）。只用于「钉住这个错误数字不再回来」的负向断言，
    正常断言一律用默认的 ``apply_min=True``。
    """
    notional = qty * price
    comm = notional * COMMISSION_RATE
    if comm < COMMISSION_MIN and apply_min:
        comm = COMMISSION_MIN
    items = [comm]
    if side == "sell":
        items.append(notional * STAMP_RATE)
    items.append(notional * TRANSFER_RATE)
    items.append(notional * (HANDLING_RATE_BSE if board == "bse" else HANDLING_RATE))
    return round(sum(round(x, 4) for x in items), 4)


def has_cjk(text):
    """rejects / warnings 必须是**中文**原因：只给英文或空串等于用户看不懂为什么被拒。"""
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


#: 脏输入矩阵（覆盖 None / 空串 / 空白 / 0 / 负数 / 非数字串 / 字符串数字 / NaN / inf / list / dict / bool）
DIRTY = [None, "", "  ", 0, -1, -3.5, "abc", "100", "1,000", float("nan"), float("inf"),
         [], {}, True]


# --------------------------------------------------------------------------- #
# A. 板块识别
# --------------------------------------------------------------------------- #
class TestBoardOf(unittest.TestCase):
    def test_main_board_prefixes(self):
        """沪深主板 600/601/603/605/000/001/002/003 必须全部识别为主板 10%：
        这几个前缀写成 20% 会让回测在主板股票上凭空多出 10% 的日内空间。"""
        for code in ("600519", "601398", "603000", "605000",
                     "000001", "001979", "002594", "003816"):
            spec = R.board_of(code)
            self.assertEqual(spec["board"], "main", code)
            self.assertEqual(spec["limit"], 0.10, code)
            self.assertEqual(spec["lot"], 100, code)
            self.assertEqual(spec["minQty"], 100, code)
            self.assertFalse(spec["riskWarning"], code)

    def test_gem_prefixes(self):
        """创业板 300/301 → 20%：与主板同为 100 股整数倍，但日内空间翻倍。"""
        for code in ("300750", "301029"):
            spec = R.board_of(code)
            self.assertEqual(spec["board"], "gem", code)
            self.assertEqual(spec["limit"], 0.20, code)
            self.assertEqual(spec["lot"], 100, code)
            self.assertEqual(spec["minQty"], 100, code)

    def test_star_prefixes_min_200_and_lot_1(self):
        """科创板 688/689 → 20%、最小申报 200 股、**lot=1**（超过 200 股后可按 1 股递增）。
        把 lot 写成 100 会让 250 股这种合法申报被误判成废单。"""
        for code in ("688111", "689009"):
            spec = R.board_of(code)
            self.assertEqual(spec["board"], "star", code)
            self.assertEqual(spec["limit"], 0.20, code)
            self.assertEqual(spec["lot"], 1, code)
            self.assertEqual(spec["minQty"], 200, code)

    def test_bse_prefixes(self):
        """北交所 43/83/87/88/920 → 30%：这是 A 股里日内空间最大的板块，
        按 10% 处理会系统性低估风险；按 20% 也会算错涨跌停价。"""
        for code in ("430047", "830799", "871981", "889999", "920001"):
            spec = R.board_of(code)
            self.assertEqual(spec["board"], "bse", code)
            self.assertEqual(spec["limit"], 0.30, code)
            self.assertEqual(spec["minQty"], 100, code)

    def test_unknown_code_falls_back_to_main(self):
        """无法识别的代码一律按**主板**口径（宁可保守）：
        识别失败时若回落成 20%/30%，会让一个未知代码在纸面上获得两倍以上的涨跌停空间 ——
        这是「静默高估收益」的典型来源。因此这里断言比例不大于主板、且 lot/minQty 取主板值。
        """
        for code in ("999999", "abc", "", "  ", "hk00700", "sh600519", None, 12345, "1"):
            spec = R.board_of(code)
            self.assertEqual(spec["board"], "main", repr(code))
            self.assertEqual(spec["limit"], 0.10, repr(code))
            self.assertLessEqual(spec["limit"], R.BOARDS["main"]["limit"],
                                 "未知代码的涨跌幅不能比主板更宽（宁可保守）")
            self.assertEqual(spec["minQty"], 100, repr(code))

    def test_risk_warning_detected_from_name(self):
        """名称含 ST/*ST 即认定风险警示；2026-07-06 起比例与主板同为 10%
        （旧口径 5% 可经 stLimitRatio 参数改回，属于「可覆盖」而不是硬编码）。"""
        spec = R.board_of("600519", name="*ST 测试")
        self.assertTrue(spec["riskWarning"])
        self.assertEqual(spec["board"], "main")
        self.assertEqual(R.limit_of("600519", name="*ST 测试")["ratio"], 0.10)
        # 名称里的 ST 大小写都算
        self.assertTrue(R.board_of("600519", name="st 测试")["riskWarning"])
        self.assertFalse(R.board_of("600519", name="贵州茅台")["riskWarning"])

    def test_us_board_has_no_price_limit(self):
        """美股无个股涨跌幅限制，最小 1 股：把 A 股口径套到美股会凭空拒单。"""
        spec = R.board_of("AAPL", market="us")
        self.assertEqual(spec["board"], "us")
        self.assertIsNone(spec["limit"])
        self.assertEqual(spec["lot"], 1)
        self.assertEqual(spec["minQty"], 1)
        self.assertEqual(spec["market"], "us")

    def test_dirty_market_values_default_to_cn(self):
        """market 脏输入（None / 数字 / 空串）一律按 A 股处理，不能抛异常。"""
        for mkt in (None, "", 0, [], {}, "CN", "cn"):
            self.assertEqual(R.board_of("600519", market=mkt)["market"], "cn", repr(mkt))


# --------------------------------------------------------------------------- #
# B. 涨跌停价
# --------------------------------------------------------------------------- #
class TestLimitPrices(unittest.TestCase):
    def test_main_board_up_down_exact(self):
        """600519 前收 100 元 → 涨停 110.00、跌停 90.00，**逐位相等**：
        0.01 元的偏差就是实盘里的废单。"""
        res = R.limit_prices("600519", 100)
        self.assertEqual(res["up"], 110.00)
        self.assertEqual(res["down"], 90.00)
        self.assertEqual(res["basis"], 100.0)
        self.assertFalse(res["unlimited"])
        self.assertEqual(res["ratio"], 0.10)
        self.assertEqual(res["board"], "main")

    def test_gem_and_bse_ratio(self):
        """创业板 20%、北交所 30% 各一例：涨跌停价必须按板块比例算，不能一律 10%。"""
        gem = R.limit_prices("300750", 100)
        self.assertEqual((gem["up"], gem["down"]), (120.00, 80.00))
        bse = R.limit_prices("830799", 100)
        self.assertEqual((bse["up"], bse["down"]), (130.00, 70.00))
        # 北交所另一种前缀（43 开头，如新三板精选层转板）同样 30%
        self.assertEqual(R.limit_prices("430047", 100)["up"], 130.00)

    def test_patch_tick_when_diff_below_one_tick(self):
        """边界补丁一：涨跌停价与前收盘价之差不足 0.01 元时按 ±0.01 元。
        前收 0.06 元按 10% 算是 0.066/0.054（与收盘价只差 0.006/0.006），
        不补丁就会得出「涨跌停价几乎等于现价」的结论，据此下单必然废单。"""
        res = R.limit_prices("600519", 0.06)
        self.assertEqual(res["up"], 0.07)
        self.assertEqual(res["down"], 0.05)
        self.assertEqual(round(res["up"] - res["basis"], 4), 0.01)
        self.assertEqual(round(res["basis"] - res["down"], 4), 0.01)

    def test_patch_floor_one_tick(self):
        """边界补丁二：算出的涨跌停价低于 0.01 元时按 0.01 元。
        前收 0.005 元按 10% 算跌停价是 0.0045 —— 若不做地板补丁会得到 0.00（甚至负数），
        而 A 股最小变动单位是 0.01 元，「0 元跌停价」是不存在的价格。"""
        res = R.limit_prices("600519", 0.005)
        self.assertEqual(res["down"], 0.01, "跌停价必须是 0.01 而不是 0")
        self.assertGreater(res["down"], 0)
        self.assertGreaterEqual(res["down"], 0.01)
        self.assertEqual(res["up"], 0.02)

    def test_rounding_is_half_up_not_bankers(self):
        """四舍五入到分：构造一个**恰好落在半分区**的用例。

        基准价 9.0 元 × (1+0.125) = 10.125（1/8 = 0.125 与 10.125 都能被二进制精确表示，
        因此这是真正的「恰好半分」而不是浮点近似）。Python 的 ``round`` 是**银行家舍入**
        （``round(10.125, 2) == 10.12``），而交易所口径是四舍五入 → 必须得到 10.13。
        这里用可覆盖参数 ``delistingLimitRatio=0.125``（退市整理期比例）来精确构造该临界值。
        """
        self.assertEqual(round(10.125, 2), 10.12, "先确认 Python 的 round 确实是银行家舍入")
        res = R.limit_prices("600519", prev_close=9.0, meta={"delistingDays": 2},
                             params={"delistingLimitRatio": 0.125})
        self.assertEqual(res["up"], 10.13, "半分位必须向上进位（四舍五入），不是取偶")
        self.assertEqual(res["down"], 7.88)      # 9×0.875 = 7.875 → 7.88（同样是四舍五入）

    def test_missing_basis_returns_none_and_says_so(self):
        """缺少基准价时 ``up``/``down``/``basis`` 全为 None，并在 note 里说明原因。
        「不知道」比「猜一个前收盘价」安全得多：猜出来的涨跌停价会被下游当成硬约束用。"""
        res = R.limit_prices("600519")
        self.assertIsNone(res["up"])
        self.assertIsNone(res["down"])
        self.assertIsNone(res["basis"])
        self.assertIn("缺少基准价", res["note"])
        self.assertEqual(res["ratio"], 0.10, "比例仍然给出（可算，只是缺基准价）")

    def test_basis_must_be_positive(self):
        """基准价为 0 / 负数 / 脏值时同样不臆造数字（返回 None 而不是 0.00）。"""
        for price in (0, -1, -3.5, "", "abc", None, float("nan"), []):
            res = R.limit_prices("600519", price)
            self.assertIsNone(res["up"], repr(price))
            self.assertIsNone(res["down"], repr(price))

    def test_prev_close_wins_over_price(self):
        """同时给 prev_close 与 price 时以前收盘价为准，且 note 不再标注「近似」。"""
        res = R.limit_prices("600519", 105.0, prev_close=100.0)
        self.assertEqual(res["up"], 110.00)
        self.assertEqual(res["down"], 90.00)
        self.assertEqual(res["basis"], 100.0)
        self.assertNotIn("近似", res["note"])
        only_price = R.limit_prices("600519", 105.0)
        self.assertIn("近似", only_price["note"])
        self.assertEqual(only_price["up"], 115.50, "只有最新价时按最新价算（近似）")

    def test_levels_hook_call_shape(self):
        """兼容 ``core/levels._limit_hint`` 的调用形态 ``limit_prices(code, price)``：
        只传两个位置参数也必须能用，且能取出 ``up`` / ``down`` 两个键。
        这是「module 不在仓库里 → 现在出现了」的可选钩子契约，签名一变 levels 会静默降级。
        """
        res = R.limit_prices("600519", 12.34)
        self.assertIsInstance(res, dict)
        self.assertIsNotNone(res.get("up"))
        self.assertIsNotNone(res.get("down"))
        self.assertEqual(res["up"], 13.57)          # 12.34×1.1 = 13.574 → 13.57
        self.assertEqual(res["down"], 11.11)        # 12.34×0.9 = 11.106 → 11.11
        # 服务端另一种形态：limit_prices(code, price, market, name)
        self.assertEqual(R.limit_prices("600519", 100.0, "cn", "贵州茅台")["up"], 110.00)

    def test_unlimited_or_us_has_no_prices(self):
        """不设涨跌幅（新股前 5 日）与美股：up/down 为 None、unlimited=True，
        绝不给一个「等于现价」的假涨跌停价。"""
        new = R.limit_prices("600519", 100.0, meta={"listedDays": 3})
        self.assertTrue(new["unlimited"])
        self.assertIsNone(new["up"])
        self.assertIsNone(new["down"])
        self.assertIn("不设涨跌幅", new["note"])
        us = R.limit_prices("AAPL", 100.0, market="us")
        self.assertTrue(us["unlimited"])
        self.assertIsNone(us["up"])
        self.assertIsNone(us["down"])

    def test_negative_qty_or_dirty_code_never_raises(self):
        """脏代码 + 负价：返回结构完整的 dict，不抛异常（公开函数的硬约束）。"""
        for code in (None, "", 0, [], {}, "abc"):
            res = R.limit_prices(code, -5)
            self.assertIn("up", res)
            self.assertIn("note", res)


# --------------------------------------------------------------------------- #
# C. 不设涨跌幅的情形
# --------------------------------------------------------------------------- #
class TestLimitOf(unittest.TestCase):
    def test_new_listing_first_five_days_unlimited(self):
        """主板/创业板/科创板新股上市后前 5 个交易日不设涨跌幅：
        ``listedDays=3`` → unlimited，``listedDays=8`` → 恢复 10%。
        少了这条，新股上市首日就会被 10% 的假限制拒单或误判。"""
        early = R.limit_of("600519", meta={"listedDays": 3})
        self.assertTrue(early["unlimited"])
        self.assertIsNone(early["ratio"])
        self.assertIn("前 5 个交易日", early["note"])
        # 边界：第 5 个交易日仍不限、第 6 日起受限
        self.assertTrue(R.limit_of("600519", meta={"listedDays": 5})["unlimited"])
        later = R.limit_of("600519", meta={"listedDays": 8})
        self.assertFalse(later["unlimited"])
        self.assertEqual(later["ratio"], 0.10)

    def test_bse_only_first_day_unlimited(self):
        """北交所只有**首日**不设涨跌幅（不是 5 日）：``listedDays=2`` 就已经受限 30%。
        把北交所和主板共用「前 5 日」会把新股第 2–5 日的风险敞口算成无限。"""
        self.assertTrue(R.limit_of("830799", meta={"listedDays": 1})["unlimited"])
        day2 = R.limit_of("830799", meta={"listedDays": 2})
        self.assertFalse(day2["unlimited"])
        self.assertEqual(day2["ratio"], 0.30)
        # 同样的 listedDays=2 在主板仍然是「不限」（因为主板是前 5 日）
        self.assertTrue(R.limit_of("600519", meta={"listedDays": 2})["unlimited"])

    def test_delisting_first_day_unlimited_then_ten_percent(self):
        """退市整理期：首个交易日不设涨跌幅，次日起 10%。"""
        first = R.limit_of("600519", meta={"delistingDays": 1})
        self.assertTrue(first["unlimited"])
        self.assertIn("退市整理期首个交易日", first["note"])
        second = R.limit_of("600519", meta={"delistingDays": 2})
        self.assertFalse(second["unlimited"])
        self.assertEqual(second["ratio"], 0.10)
        self.assertEqual(second["label"], "退市整理")

    def test_st_is_ten_percent_after_2026_07_06(self):
        """风险警示股自 2026-07-06 起与主板同为 10%（旧口径 5%）。
        版本串与 note 都要写明这个口径日期，否则用户不知道界面按哪一版在算。"""
        res = R.limit_of("600519", name="*ST 测试")
        self.assertFalse(res["unlimited"])
        self.assertEqual(res["ratio"], 0.10)
        self.assertIn("2026-07-06", res["note"])
        self.assertIn("风险警示", res["label"])
        # 可覆盖：改回旧口径仍应生效（做成参数而不是硬编码）
        old = R.limit_of("600519", name="*ST 测试", params={"stLimitRatio": 0.05})
        self.assertEqual(old["ratio"], 0.05)

    def test_missing_meta_is_conservative(self):
        """拿不到 listedDays / delistingDays 时**按有限制处理**（保守方向），
        并在 note 里说明「实际比例可能不同」。把「未知」当成「不限」会让任何拿不到
        新股元数据的数据源都产生无限涨跌幅的假信号。"""
        for meta in (None, {}, "x", 3, [], {"listedDays": None}, {"listedDays": "abc"}):
            res = R.limit_of("600519", meta=meta)
            self.assertFalse(res["unlimited"], repr(meta))
            self.assertEqual(res["ratio"], 0.10, repr(meta))

    def test_us_is_always_unlimited(self):
        """美股：不限涨跌幅（只有全市场熔断）。"""
        res = R.limit_of("AAPL", market="us")
        self.assertTrue(res["unlimited"])
        self.assertIsNone(res["ratio"])
        self.assertIn("熔断", res["note"])


# --------------------------------------------------------------------------- #
# D. 费用逐项
# --------------------------------------------------------------------------- #
class TestFeeOf(unittest.TestCase):
    def test_buy_10k_items_hit_min_commission_branch(self):
        """1 万元买入（100 股 × 100 元）→ 佣金按万 2.5 只有 2.5 元，**命中最低值分支**；
        买入**不得**出现印花税项；过户费与经手费双向都要出现。

        这里只断言「分支命中」与「逐项结构」（无论最低值是否计入总费用都成立），
        最低值是否真的落到金额上由下一节 TestFeeMinCommissionRegression 覆盖。
        """
        res = R.fee_of("buy", 100, 100.0)
        items = res["items"]
        names = [i["name"] for i in items]
        self.assertEqual(names, ["佣金", "过户费", "经手费"], "买入 = 佣金 + 过户费 + 经手费")
        self.assertNotIn("印花税", names, "印花税仅卖出单边，买入不该有")
        comm = items[0]
        self.assertTrue(comm["minApplied"], "2.5 元 < 5 元，必须标记为命中最低值")
        self.assertEqual(comm["rate"], COMMISSION_RATE)
        self.assertIn("5.00", comm["note"], "note 必须写明「不足 5.00 元按 5.00 元」")
        # 逐项金额 = 成交额 × 费率（最低值分支只影响「佣金」这一项）
        self.assertEqual(items[1]["amount"], round(10000 * TRANSFER_RATE, 4))
        self.assertEqual(items[2]["amount"], round(10000 * HANDLING_RATE, 4))
        self.assertEqual(items[2]["rate"], HANDLING_RATE)
        self.assertEqual(res["notional"], 10000.0)

    def test_sell_10k_has_stamp_tax(self):
        """1 万元卖出 → 在买入的三项之外**多一道印花税 0.05%**（单边）。
        印花税是 A 股最容易被漏掉的一项：漏掉它，日内策略的成本会被低估一半以上。"""
        buy = R.fee_of("buy", 100, 100.0)
        sell = R.fee_of("sell", 100, 100.0)
        stamp_items = [i for i in sell["items"] if i["name"] == "印花税"]
        self.assertEqual(len(stamp_items), 1, "卖出必须且只有一项印花税")
        self.assertEqual(stamp_items[0]["rate"], STAMP_RATE)
        self.assertEqual(stamp_items[0]["amount"], 5.0)          # 10000 × 0.0005
        self.assertIn("仅卖出单边", stamp_items[0]["note"])
        # 同一成交额下：卖出总额 − 买入总额 == 印花税（买卖其余各项完全对称）
        self.assertEqual(round(sell["total"] - buy["total"], 4), 5.0)
        self.assertGreater(sell["total"], buy["total"], "卖出费用必须大于买入同额费用")

    def test_buy_1m_no_longer_hits_min_commission(self):
        """100 万元买入（1000 股 × 1000 元）→ 佣金 250 元，**不再命中最低值**：
        最低值是「小单保护」，不该在大单上生效 —— 这里同时验证门槛判定本身正确。"""
        res = R.fee_of("buy", 1000, 1000.0)
        comm = res["items"][0]
        self.assertFalse(comm["minApplied"])
        self.assertEqual(comm["amount"], 250.0)                  # 1000000 × 0.00025
        self.assertEqual(res["items"][1]["amount"], 10.0)        # 过户费 0.001%
        self.assertEqual(res["items"][2]["amount"], 34.1)        # 经手费 0.00341%
        self.assertEqual(res["total"], round(250.0 + 10.0 + 34.1, 4))
        self.assertEqual(res["totalRate"], round(res["total"] / 1000000.0, 8))

    def test_items_shape_and_total_equals_sum(self):
        """``items`` 每项必须含 name / rate / amount / note（界面要把「为什么收这 5.4 元」
        讲清楚），且 ``total`` **逐位等于**各项之和 —— 明细与总额不一致会让用户无法对账。"""
        for side in ("buy", "sell"):
            res = R.fee_of(side, 300, 45.6)
            self.assertTrue(res["items"], side)
            for item in res["items"]:
                for key in ("name", "rate", "amount", "note"):
                    self.assertIn(key, item, "%s 项缺 %s" % (side, key))
                self.assertIsInstance(item["name"], str)
                self.assertTrue(item["note"])
                self.assertGreaterEqual(item["amount"], 0.0)
            self.assertAlmostEqual(res["total"], sum(i["amount"] for i in res["items"]),
                                   delta=1e-9, msg="total 必须逐位等于 Σamount")
            self.assertEqual(res["side"], side)
            self.assertEqual(res["qty"], 300)

    def test_cn_and_us_are_two_different_schemes(self):
        """A 股与美股各一例：A 股按成交额 × 费率 + 最低 5 元；美股默认按股计费。
        两套口径混用会让跨市场回测的成本完全错位，因此这里显式区分并逐项写清算式。

        **美股默认值只是「可配置示例」、不是权威口径**（各家券商差异极大，见
        ``core/rules.DEFAULT_FEES`` 里 ``"us"`` 段的注释）—— 本用例断言的是引擎按这组
        示例参数算出来的**手算值**，不是「美股就应该是这个费率」。

        A 股买入手算（1 万元 = 100 股 × 100 元）：
          佣金 max(10000 × 0.00025, 5) = max(2.5, 5) = 5（命中最低值）
          + 过户费 10000 × 0.00001 = 0.1 + 经手费 10000 × 0.0000341 = 0.341 → 5.441
        美股买入手算（示例口径，最低 1.00 美元/笔）：
          按成交额 10000 × 0.0 = 0；按股 100 × 0.005 = 0.5 → 取大 0.5，不足最低 1.00 → 收 1.00
        美股卖出手算：
          佣金 1.00（最低值）+ SEC 规费 10000 × 0.0000278 = 0.278
          + TAF min(100 × 0.000166, 8.30) = 0.0166 → 1.2946
        """
        cn = R.fee_of("buy", 100, 100.0, market="cn")
        self.assertEqual([i["name"] for i in cn["items"]], ["佣金", "过户费", "经手费"])
        self.assertEqual(cn["items"][0]["amount"], 5.0)          # 最低值 5.00 写进 amount
        self.assertEqual(cn["total"], 5.441)                     # 5 + 0.1 + 0.341

        us = R.fee_of("buy", 100, 100.0, market="us")
        self.assertEqual(len(us["items"]), 1)
        self.assertEqual(us["items"][0]["rate"], 0.005, "美股默认按股计费 0.005/股（示例口径）")
        self.assertEqual(us["items"][0]["rateAmount"], 0.5,
                         "审计字段保留「按费率算出来的数」（100 股 × 0.005 = 0.5）")
        self.assertEqual(us["items"][0]["amount"], 1.0, "不足最低 1.00 美元 → amount 记 1.00")
        self.assertTrue(us["items"][0]["minApplied"])            # 0.5 < 最低 1.0
        self.assertEqual(us["total"], 1.0)
        self.assertEqual(us["market"], "us")
        # 卖出侧美股多 SEC 规费 + TAF（A 股是印花税，二者不可互相替代）
        us_sell = R.fee_of("sell", 100, 100.0, market="us")
        names = [i["name"] for i in us_sell["items"]]
        self.assertEqual(len(names), 3)
        self.assertTrue(any("SEC" in n for n in names))
        self.assertTrue(any("TAF" in n for n in names))
        self.assertEqual(us_sell["total"], 1.2946)               # 1.00 + 0.278 + 0.0166
        self.assertEqual([i["name"] for i in R.fee_of("sell", 100, 100.0)["items"]],
                         ["佣金", "印花税", "过户费", "经手费"])

    def test_stamp_tax_of_isolates_stamp_tax(self):
        """``stamp_tax_of`` 只返回印花税：买入 0、卖出 = 成交额 × 0.05%。
        回测里常把印花税单独拆出来做归因，取错一项会让「税费拖累」的结论整体偏。"""
        self.assertEqual(R.stamp_tax_of("buy", 100, 100.0), 0.0)
        self.assertEqual(R.stamp_tax_of("sell", 100, 100.0), 5.0)
        self.assertEqual(R.stamp_tax_of("sell", 1000, 1000.0), 500.0)
        self.assertEqual(R.stamp_tax_of("close", 200, 50.0), 5.0, "close 也是卖出")

    def test_bse_handling_fee_differs_from_shanghai_shenzhen(self):
        """北交所经手费 0.0125% 与沪深 0.00341% 不同（约为 3.7 倍）：
        按沪深口径算北交所会把成本低估 3 倍以上。"""
        sh = R.fee_of("buy", 100, 100.0)
        bse = R.fee_of("buy", 100, 100.0, board="bse")
        sh_item = [i for i in sh["items"] if i["name"] == "经手费"][0]
        bse_item = [i for i in bse["items"] if i["name"] == "经手费"][0]
        self.assertEqual(sh_item["rate"], HANDLING_RATE)
        self.assertEqual(bse_item["rate"], HANDLING_RATE_BSE)
        self.assertNotEqual(sh_item["rate"], bse_item["rate"])
        self.assertEqual(sh_item["amount"], 0.341)               # 10000 × 0.0000341
        self.assertEqual(bse_item["amount"], 1.25)               # 10000 × 0.000125
        self.assertGreater(bse["total"], sh["total"])

    def test_invalid_qty_or_price_costs_zero(self):
        """数量 / 价格为 0 或非法 → 费用记 0 且 items 为空（不是 None、不抛异常）。"""
        for qty, price in ((0, 100.0), (100, 0), (-5, 100.0), (-100, -100.0),
                           ("abc", "abc"), (None, None), (0, None)):
            res = R.fee_of("buy", qty, price)
            self.assertEqual(res["total"], 0.0, repr((qty, price)))
            self.assertEqual(res["items"], [])
            self.assertIn("数量或价格无效", res["note"])

    def test_fee_rates_are_overridable(self):
        """费率是**可覆盖参数**：换成别家券商（例如万 1、最低 1 元）必须立刻生效，
        否则「同一份回测换个券商就重算不出来」。"""
        res = R.fee_of("buy", 100, 100.0,
                       fees={"cn": {"commissionRate": 0.0001, "commissionMin": 1.0}})
        comm = res["items"][0]
        self.assertEqual(comm["rate"], 0.0001)
        self.assertEqual(comm["amount"], 1.0)                    # 10000 × 0.0001 = 1.0
        self.assertFalse(comm["minApplied"], "1.0 不小于最低 1.0，不该标记命中最低值")
        # 只覆盖一个市场时不影响另一个市场
        us = R.fee_of("buy", 100, 100.0, market="us",
                      fees={"cn": {"commissionRate": 0.0001}})
        self.assertEqual(us["items"][0]["rate"], 0.005)


# --------------------------------------------------------------------------- #
# E. 已修复缺陷 F1 的回归：最低佣金必须计入 total（原来是 report-only）
# --------------------------------------------------------------------------- #
class TestFeeMinCommissionRegression(unittest.TestCase):
    """**已修复缺陷 F1 的回归**（本轮之前这里是 ``@unittest.expectedFailure``）。

    缺陷原状：``core/rules.fee_of`` 命中「佣金不足最低值」时把 ``comm = commissionMin``
    赋值后**没有再写回 items**，而 ``total = sum(i["amount"])``；于是最低值只体现在
    ``minApplied`` 标记与 note 文案里，金额仍是按费率算出的数（1 万元买入只收 2.941 元，
    而 note 却向用户承诺「不足 5 元按 5 元」）。修好后：

      1 万元买入 = 佣金 max(10000×0.00025, 5) = 5 + 过户费 10000×0.00001 = 0.1
                 + 经手费 10000×0.0000341 = 0.341 → **5.441**
      1 万元卖出 = 上面三项 + 印花税 10000×0.0005 = 5 → **10.441**（印花税仅卖出单边）
      美股买入（示例口径，最低 1.00 美元/笔）= **1.00**（按股算只有 0.5）

    为什么必须逐位断言：最低佣金是小单成本的主体（2 万元以下成交额的佣金占成交额可超
    0.05%），漏掉它会让「小资金策略」在回测里凭空便宜 80%，而**不会报任何错**。
    同时必须保留 ``rateAmount``：审计时要能回答「这 5 元里有多少是费率的贡献」。
    """

    def test_buy_10k_total_includes_min_commission(self):
        """买入 1 万元：佣金项 amount = 最低值 5.00，total = 5 + 0.1 + 0.341 = 5.441。"""
        res = R.fee_of("buy", 100, 100.0)
        comm = res["items"][0]
        self.assertEqual(comm["name"], "佣金")
        self.assertEqual(comm["amount"], COMMISSION_MIN, "佣金项应为最低值 5.00 元")
        self.assertEqual(comm["rateAmount"], 2.5, "审计字段 = 按费率算出的 10000×0.00025")
        self.assertTrue(comm["minApplied"])
        self.assertEqual(res["total"], itemized_cn("buy", 100, 100.0))
        self.assertEqual(res["total"], 5.441)
        self.assertEqual(round(sum(i["amount"] for i in res["items"]), 4), 5.441,
                         "total 必须逐位等于 Σitems.amount（明细与总额要对得上账）")
        # 负向断言：修复前的错误数字（佣金只记 2.5）不得回来
        self.assertNotEqual(res["total"], itemized_cn("buy", 100, 100.0, apply_min=False))
        self.assertEqual(itemized_cn("buy", 100, 100.0, apply_min=False), 2.941,
                         "2.941 是缺陷 F1 时的错误合计（佣金 2.5 + 0.1 + 0.341）")

    def test_sell_10k_total_includes_min_commission_and_stamp(self):
        """卖出 1 万元：5（最低佣金）+ 5（印花税）+ 0.1 + 0.341 = 10.441。"""
        res = R.fee_of("sell", 100, 100.0)
        self.assertEqual(res["items"][0]["amount"], COMMISSION_MIN)
        self.assertEqual(res["items"][0]["rateAmount"], 2.5)
        self.assertEqual(res["total"], itemized_cn("sell", 100, 100.0))
        self.assertEqual(res["total"], 10.441)
        # 与买入的差恰好是印花税 5 元（其余各项对称）
        self.assertEqual(round(res["total"] - R.fee_of("buy", 100, 100.0)["total"], 4), 5.0)

    def test_us_min_commission_is_applied_too(self):
        """美股分支同构（**示例口径**，最低 1.00 美元/笔）：按股算 0.5 < 1.00 → total = 1.0。"""
        res = R.fee_of("buy", 100, 100.0, market="us")
        self.assertEqual(res["items"][0]["amount"], 1.0)
        self.assertEqual(res["items"][0]["rateAmount"], 0.5)
        self.assertTrue(res["items"][0]["minApplied"])
        self.assertEqual(res["total"], 1.0)


# --------------------------------------------------------------------------- #
# F. 交易时段
# --------------------------------------------------------------------------- #
#: A 股时段样本：(北京时间, 期望 session, 可申报, 可撤单)
CN_SAMPLES = [
    ((9, 15), "openAuction", True, True),      # 集合竞价开始，可申报可撤单
    ((9, 19), "openAuction", True, True),      # 09:20 之前仍可撤单
    ((9, 20), "openAuction", True, False),     # 09:20 起不再接受撤单（本时刻即撤单窗口关闭点）
    ((9, 22), "openAuction", True, False),     # 可申报、不可撤单
    ((9, 27), "silence", False, True),         # 09:25–09:30 静默期：不接受任何申报
    ((9, 30), "morning", True, True),          # 上午连续竞价
    ((10, 30), "morning", True, True),
    ((11, 30), "lunch", False, True),          # 午间休市
    ((12, 0), "lunch", False, True),
    ((13, 0), "afternoon", True, True),        # 下午连续竞价
    ((14, 58), "closeAuction", True, False),   # 收盘集合竞价，不可撤单
    ((15, 0), "closed", False, True),          # 15:00–15:05 空档
    ((15, 10), "afterHoursFixed", True, True),  # 盘后固定价格交易
    ((15, 31), "closed", False, True),
    ((8, 0), "closed", False, True),           # 开盘前
]


class TestSession(unittest.TestCase):
    def test_cn_session_table(self):
        """A 股时段逐个断言（09:15 集合竞价 → 静默期 → 连续竞价 → 午休 → 收盘竞价 →
        盘后固定价格 → 休市）：把静默期当成可申报会让程序化订单在 09:25–09:30 白白废单；
        把午休当成可申报会让「立即成交」的假设在 11:30–13:00 全部落空。"""
        for (hour, minute), session, tradable, can_cancel in CN_SAMPLES:
            stamp = cn_ts(hour, minute)
            res = R.session_of(stamp)
            label = "%02d:%02d" % (hour, minute)
            self.assertEqual(res["session"], session, label)
            self.assertEqual(res["tradable"], tradable, label)
            self.assertEqual(res["canCancel"], can_cancel, label)
            self.assertEqual(res["minutes"], hour * 60 + minute, label)
            self.assertEqual(res["weekday"], 3, "2026-09-17 是周四")
            self.assertIsInstance(res["note"], str, label)
        # 「为什么不可申报 / 不可撤单」必须给出可读说明（午休与纯休市允许为空串）
        for (hour, minute) in ((9, 20), (9, 27), (10, 30), (14, 58), (15, 10)):
            res = R.session_of(cn_ts(hour, minute))
            self.assertTrue(res["note"], "%02d:%02d 必须给出可读说明" % (hour, minute))

    def test_is_tradable_now_matches_session_of(self):
        """``is_tradable_now`` 必须与 ``session_of(...)['tradable']`` 完全一致：
        两者不一致时会出现「界面说可交易、下单被拒」这类互相矛盾的提示。"""
        for (hour, minute), _session, tradable, _cancel in CN_SAMPLES:
            stamp = cn_ts(hour, minute)
            self.assertEqual(R.is_tradable_now(stamp), R.session_of(stamp)["tradable"],
                             "%02d:%02d" % (hour, minute))
            self.assertEqual(R.is_tradable_now(stamp), tradable)

    def test_open_auction_cancel_window(self):
        """开盘集合竞价的撤单窗口：09:20 之前可撤、09:20 起不可撤（交易所口径是
        「9:20–9:25 不接受撤单申报」，因此 09:20:00 这一分钟已经算在关闭区间内）。
        断言这个边界是为了让「本地排队到 09:20 再撤单」的实现不至于晚一秒就废单。"""
        before = R.session_of(cn_ts(9, 19))
        at = R.session_of(cn_ts(9, 20))
        self.assertTrue(before["canCancel"])
        self.assertFalse(at["canCancel"])
        self.assertTrue(at["tradable"], "不可撤单但**可以申报**，这两件事不能混为一谈")
        self.assertIn("撤单", at["note"])

    def test_close_auction_and_after_hours(self):
        """收盘集合竞价 14:57–15:00 不接受撤单；盘后固定价格 15:05–15:30 按**收盘价**成交
        （2026-07-06 起扩展到全部 A 股与 ETF，因此主板 600519 也能盘后交易）。"""
        close = R.session_of(cn_ts(14, 58))
        self.assertEqual(close["session"], "closeAuction")
        self.assertFalse(close["canCancel"])
        self.assertIn("收盘集合竞价", close["label"])
        after = R.session_of(cn_ts(15, 10))
        self.assertEqual(after["session"], "afterHoursFixed")
        self.assertTrue(after["tradable"])
        self.assertIn("收盘价", after["note"])
        # 盘后固定价格是可关闭的开关（旧口径只有科创板/创业板有）：关掉后应回落到休市
        off = R.session_of(cn_ts(15, 10), params={"allowAfterHoursFixed": False})
        self.assertFalse(off["tradable"])
        self.assertEqual(off["session"], "closed")

    def test_weekend_is_closed(self):
        """周末休市（2026-09-19 是周六）：把周末当交易日会让「无人值守自动成交」在休市日
        产生一堆不可能成交的委托。"""
        res = R.session_of(cn_ts(10, 0, SAT))
        self.assertEqual(res["session"], "weekend")
        self.assertFalse(res["tradable"])
        self.assertFalse(R.is_tradable_now(cn_ts(10, 0, SAT)))
        self.assertIn("周末", res["label"])
        self.assertTrue(R.session_of(us_ts(10, 0, SAT), market="us")["session"] == "weekend")

    def test_us_sessions(self):
        """美股时段（美东）：盘前 04:00–09:30、盘中 09:30–16:00、盘后 16:00–20:00。
        时段按 UTC−4/−5 粗略换算（夏令时用 −4），测试按同一口径构造时间戳。"""
        cases = [((4, 0), "preMarket", True), ((10, 0), "regular", True),
                 ((15, 59), "regular", True), ((16, 0), "afterMarket", True),
                 ((19, 30), "afterMarket", True), ((20, 30), "closed", False),
                 ((3, 0), "closed", False)]
        for (hour, minute), session, tradable in cases:
            res = R.session_of(us_ts(hour, minute), market="us")
            label = "%02d:%02d" % (hour, minute)
            self.assertEqual(res["session"], session, label)
            self.assertEqual(res["tradable"], tradable, label)

    def test_session_windows_shape(self):
        """时段表要能直接给界面用：6 个 A 股时段齐全、from/to 是 HH:MM、
        minutes 与 from/to 自洽（否则前端时间轴会画错）。"""
        table = R.session_windows()
        self.assertEqual(table["market"], "cn")
        self.assertEqual(table["tz"], "北京时间")
        windows = table["windows"]
        self.assertEqual([w["key"] for w in windows],
                         ["openAuction", "silence", "morning", "afternoon",
                          "closeAuction", "afterHoursFixed"])
        for win in windows:
            self.assertRegex(win["from"], r"^\d{2}:\d{2}$")
            self.assertRegex(win["to"], r"^\d{2}:\d{2}$")
            start, end = win["minutes"]
            self.assertEqual(win["from"], "%02d:%02d" % (start // 60, start % 60))
            self.assertEqual(win["to"], "%02d:%02d" % (end // 60, end % 60))
            self.assertLess(start, end)
            self.assertTrue(win["label"])
        self.assertIn("09:25", json.dumps(windows, ensure_ascii=False))
        self.assertIn("15:05", json.dumps(windows, ensure_ascii=False))
        us = R.session_windows(market="us")
        self.assertEqual([w["key"] for w in us["windows"]],
                         ["preMarket", "regular", "afterMarket"])
        for win in us["windows"]:
            self.assertRegex(win["from"], r"^\d{2}:\d{2}$")

    def test_dirty_market_and_params_never_raise(self):
        """时段相关函数的脏参数（market / params）不抛异常，且 market 未知时按 A 股。"""
        for mkt in (None, "", 0, [], {}, "abc"):
            self.assertEqual(R.session_of(cn_ts(10, 30), market=mkt)["market"], "cn", repr(mkt))
            self.assertEqual(R.session_windows(mkt)["market"], "cn", repr(mkt))
        for params in (None, "x", 3, [], {"tzOffsetHours": "abc"}, {"tzOffsetHours": None}):
            res = R.session_of(cn_ts(10, 30), params=params)
            self.assertIn(res["session"], ("morning", "closed", "afternoon", "lunch"), repr(params))


# --------------------------------------------------------------------------- #
# G. 最小申报单位与 T+1
# --------------------------------------------------------------------------- #
class TestLotAndT1(unittest.TestCase):
    def test_lot_of_and_min_qty_of(self):
        """最小申报单位：主板/创业板/北交所 100 股，科创板 1 股（但起报 200 股），美股 1 股。
        lot 与 minQty 是两个不同的数 —— 混用会同时误判「100 股科创板」与「250 股科创板」。"""
        self.assertEqual(R.lot_of("600519"), 100)
        self.assertEqual(R.lot_of("300750"), 100)
        self.assertEqual(R.lot_of("830799"), 100)
        self.assertEqual(R.lot_of("688111"), 1)
        self.assertEqual(R.lot_of("AAPL", market="us"), 1)
        self.assertEqual(R.min_qty_of("600519"), 100)
        self.assertEqual(R.min_qty_of("300750"), 100)
        self.assertEqual(R.min_qty_of("688111"), 200)
        self.assertEqual(R.min_qty_of("AAPL", market="us"), 1)

    def test_round_qty_buy(self):
        """买入取整：主板 250 → 200（向下取整到整手）；科创板 150 → 0（不足 200 股不能报）、
        250 → 250（超过 200 股后可 1 股递增）；美股 1 股。

        买入**绝不向上取整**：多买一手会直接击穿金额上限与资金校验的结论。
        """
        self.assertEqual(R.round_qty("600519", 250, side="buy"), 200)
        self.assertEqual(R.round_qty("600519", 150, side="buy"), 100)
        self.assertEqual(R.round_qty("300750", 250, side="buy"), 200)
        self.assertEqual(R.round_qty("830799", 250, side="buy"), 200)
        # 科创板
        self.assertEqual(R.round_qty("688111", 150), 0, "不足 200 股不能申报")
        self.assertEqual(R.round_qty("688111", 199), 0)
        self.assertEqual(R.round_qty("688111", 200), 200)
        self.assertEqual(R.round_qty("688111", 250), 250, "超过 200 股后可按 1 股递增")
        self.assertEqual(R.round_qty("688111", 201), 201)
        # 美股
        self.assertEqual(R.round_qty("AAPL", 1, market="us"), 1)
        self.assertEqual(R.round_qty("AAPL", 7, market="us"), 7)

    def test_round_qty_sell_allows_odd_lot_only_when_selling_everything(self):
        """卖出：余额不足一手时必须**一次性全部卖出**（250 股持仓卖 250 → 250），
        但不允许只卖零股（1000 股持仓报 50 股 → 0，必须整手）。
        少了这条，用户想「清掉碎股」时会被自己的整手校验挡住。"""
        self.assertEqual(R.round_qty("600519", 250, side="sell", position=250), 250)
        self.assertEqual(R.round_qty("600519", 250, side="sell", position=100), 100)
        self.assertEqual(R.round_qty("600519", 99, side="sell", position=99), 99,
                         "整笔卖出 99 股碎股是允许的")
        self.assertEqual(R.round_qty("600519", 50, side="sell", position=1000), 0)
        self.assertEqual(R.round_qty("600519", 250, side="sell", position=1000), 200)
        self.assertEqual(R.round_qty("600519", 0, side="sell"), 0)

    def test_sellable_qty_is_yesterday_position(self):
        """T+1 可卖 = 持仓 − 今日买入 − 冻结；``sellable_qty(1000, today_bought=300) == 700``。
        用总持仓当可卖量等于给模拟盘开了 T+0 后门，纸面收益会凭空多出一截。"""
        self.assertEqual(R.sellable_qty(1000, today_bought=300), 700)
        self.assertEqual(R.sellable_qty(1000), 1000)
        self.assertEqual(R.sellable_qty(1000, 1000), 0)
        self.assertEqual(R.sellable_qty(1000, 300, 100), 600)
        self.assertEqual(R.sellable_qty(1000, 2000), 0, "今日买入超过持仓时按 0（不出负数）")
        self.assertEqual(R.sellable_qty(None, None, None), 0)
        self.assertEqual(R.sellable_qty(-100, -100, -100), 0)

    def test_dirty_qty_never_raises(self):
        """数量类函数的脏输入一律降级为 0，绝不抛异常。"""
        for bad in DIRTY:
            self.assertIsInstance(R.round_qty("600519", bad), int, repr(bad))
            self.assertIsInstance(R.sellable_qty(bad, bad, bad), int, repr(bad))
            self.assertIsInstance(R.lot_of(bad), int, repr(bad))
            self.assertIsInstance(R.min_qty_of(bad), int, repr(bad))


# --------------------------------------------------------------------------- #
# H. 下单校验（check_order）
# --------------------------------------------------------------------------- #
MORNING = cn_ts(10, 30)          # 上午连续竞价（价格笼子生效）
OPEN_AUCTION = cn_ts(9, 20)      # 开盘集合竞价（笼子不生效，适合单独看涨跌停告警）
LUNCH = cn_ts(12, 0)


class TestCheckOrder(unittest.TestCase):
    def _order(self, **over):
        """一张「本应通过」的买单：600519 / 200 股 / 100 元（前收 100）/ 10 万现金 / 上午 10:30。
        每个用例只改一个变量，确保拒单原因可归因到那一个变量。"""
        kw = dict(side="buy", code="600519", qty=200, price=100.0, prev_close=100.0,
                  cash=100000.0, ts=MORNING)
        kw.update(over)
        return R.check_order(**kw)

    def _assert_reject(self, res, keyword):
        self.assertFalse(res["ok"], "必须 ok=False")
        self.assertTrue(res["rejects"], "拒单必须给出 reasons，不能静默失败")
        text = "｜".join(res["rejects"])
        self.assertIn(keyword, text, "拒单原因里应含关键词 %r，实际 %s" % (keyword, text))
        for reason in res["rejects"]:
            self.assertTrue(has_cjk(reason), "拒单原因必须是中文：%r" % reason)
        return text

    # ---- 逐条拒单场景 ---------------------------------------------------- #
    def test_reject_out_of_session(self):
        """① 时段：午间休市不可申报。真实的「不可撮合」第一来源就是时段，
        漏掉它会让程序在闭市时把委托单标成成交。"""
        self._assert_reject(self._order(ts=LUNCH), "不可申报")

    def test_reject_suspended(self):
        """② 停牌：停牌标的一律不可申报。停牌时价格仍然存在于行情快照里，
        只按价格撮合会凭空成交一笔根本不可能的买卖。"""
        res = self._order(meta={"suspended": True})
        self._assert_reject(res, "停牌")
        self.assertFalse([c for c in res["checks"] if c["name"] == "是否停牌"][0]["pass"])

    def test_reject_qty_not_round_lot(self):
        """③ 数量非整手：主板 150 股是废单（100 股整数倍）。"""
        self._assert_reject(self._order(qty=150), "申报数量不合法")

    def test_reject_star_below_min_qty(self):
        """④ 科创板不足 200 股：100 股不能申报（lot=1 但起报 200 股）。"""
        text = self._assert_reject(self._order(code="688111", qty=100), "申报数量不合法")
        self.assertIn("200", text)
        # 同一个代码报 250 股是合法的（超过 200 股后可 1 股递增）
        self.assertTrue(self._order(code="688111", qty=250)["ok"])

    def test_reject_above_limit_up(self):
        """⑤ 超涨停：报价 115 > 涨停价 110 → 废单。**不能取「价格可达」当成交依据**。"""
        text = self._assert_reject(self._order(price=115.0), "高于涨停价")
        self.assertIn("110.00", text)

    def test_reject_below_limit_down(self):
        """⑥ 低于跌停：报价 85 < 跌停价 90 → 废单。"""
        text = self._assert_reject(self._order(price=85.0), "低于跌停价")
        self.assertIn("90.00", text)

    def test_reject_price_cage(self):
        """⑦ 价格笼子：连续竞价限价申报有效范围 ±2%（或 ±10 ticks，取更宽者）。
        报价 105 在前收 100 下超出笼子上限 102 → 交易所直接作废，必须在本地就拦下。"""
        text = self._assert_reject(self._order(price=105.0), "有效价格范围")
        self.assertIn("±2%", text)
        # 边界：笼子上限 102.00 恰好合法，102.01 越界
        self.assertTrue(self._order(price=102.0)["ok"], "笼子边界内应放行")
        self._assert_reject(self._order(price=102.01), "有效价格范围")

    def test_reject_t_plus_1(self):
        """⑧ T+1 可卖：持仓 200 股但全是今日买入 → 可卖 0，卖 100 股被拒。"""
        res = self._order(side="sell", qty=100, position=200, today_bought=200)
        text = self._assert_reject(res, "T+1")
        self.assertIn("可卖 0 股", text)
        self.assertEqual(res["sellable"], 0)
        # 昨仓 200 股（今日买入 0）卖 100 股应放行
        self.assertTrue(self._order(side="sell", qty=100, position=200)["ok"])

    def test_reject_insufficient_cash(self):
        """⑨ 资金不足：现金 1000 元买 200 股 × 100 元（含费用约 2 万）→ 拒单。
        且需把「含费用」后的金额算进去，只比成交额会漏掉手续费导致的拒单。"""
        text = self._assert_reject(self._order(cash=1000.0), "可用资金不足")
        self.assertIn("含费用", text)

    def test_reject_insufficient_position(self):
        """⑩ 持仓不足：持股 200 股要卖 300 股 → 拒单（不做部分成交）。"""
        res = self._order(side="sell", qty=300, position=200)
        text = self._assert_reject(res, "持仓不足")
        self.assertIn("持股 200", text)

    # ---- 正常单与结果结构 ------------------------------------------------- #
    def test_normal_order_passes(self):
        """正常单：ok=True、rejects 为空；同时锁定返回结构（服务端与前端按这些键读）。"""
        res = self._order()
        self.assertTrue(res["ok"])
        self.assertEqual(res["rejects"], [])
        for key in ("ok", "rejects", "warnings", "checks", "side", "code", "qty", "price",
                    "market", "board", "label", "limit", "limitRatio", "fee", "lot",
                    "minQty", "sellable", "session", "limitState", "rulesVersion", "note"):
            self.assertIn(key, res, "返回结构缺 %s" % key)
        self.assertEqual(res["side"], "buy")
        self.assertEqual(res["code"], "600519")
        self.assertEqual(res["qty"], 200)
        self.assertEqual(res["lot"], 100)
        self.assertEqual(res["board"], "main")
        self.assertIsNone(res["sellable"], "买单不涉及可卖数量")
        self.assertEqual(res["limit"]["up"], 110.00)
        self.assertIn("2026-07-06", res["rulesVersion"])

    def test_checks_every_item_has_name_pass_detail(self):
        """``checks`` 是给用户看的「逐条判了什么」：每项必须含 name/pass/detail，
        且覆盖时段 / 停牌 / 数量 / 涨跌停 / 资金这几道。缺项等于校验静默失效。"""
        res = self._order()
        names = []
        for check in res["checks"]:
            self.assertEqual(set(check.keys()), {"name", "pass", "detail"})
            self.assertIsInstance(check["name"], str)
            self.assertTrue(check["name"])
            self.assertIsInstance(check["pass"], bool)
            self.assertTrue(has_cjk(check["detail"]), "detail 要写清判定依据：%r" % check["detail"])
            names.append(check["name"])
        for expected in ("交易时段", "是否停牌", "申报数量", "涨跌停", "可用资金"):
            self.assertIn(expected, names)
        # 拒单时对应项必须 pass=False（否则用户看到「全绿但被拒」）
        bad = self._order(qty=150)
        self.assertTrue(any(not c["pass"] for c in bad["checks"]))
        self.assertFalse([c for c in bad["checks"] if c["name"] == "申报数量"][0]["pass"])

    def test_warnings_not_empty_for_risk_warning_and_limit_price(self):
        """``warnings`` 是不阻断但必须告知的项：
        · 风险警示股（波动与退市风险）—— 卖出侧给出提示；
        · 报价已到涨停价 —— 买卖排队可能不成交。
        这两条都不该变成 rejects（用户仍可下单），但必须让用户看见。"""
        st = self._order(side="sell", qty=100, position=200, name="*ST测试")
        self.assertTrue(st["ok"], "风险警示只是提示，不能直接拒单")
        self.assertTrue(any("风险警示股" in w for w in st["warnings"]))
        # 单日买入上限：ST 股 600000 股 > 500000 股上限 → 提示（仍放行）
        cap = self._order(qty=600000, price=1.0, prev_close=1.0, cash=1e9,
                          name="*ST测试", ts=OPEN_AUCTION)
        self.assertTrue(any("上限" in w for w in cap["warnings"]))
        # 报价到涨停价（集合竞价时段不会触发价格笼子，便于单独观察这条提示）
        limit = self._order(price=110.0, ts=OPEN_AUCTION)
        self.assertTrue(limit["ok"])
        self.assertTrue(any("涨停价" in w for w in limit["warnings"]))
        self.assertIn("封板", "｜".join(limit["warnings"]))

    def test_warnings_when_cash_or_position_missing(self):
        """未提供现金 / 持仓时不能静默跳过校验，必须在 warnings 里说明「跳过」。"""
        no_cash = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0, ts=MORNING)
        self.assertTrue(no_cash["ok"])
        self.assertTrue(any("资金" in w for w in no_cash["warnings"]))
        no_pos = R.check_order("sell", "600519", 100, 100.0, prev_close=100.0, ts=MORNING,
                               position=None)
        self.assertTrue(any("持仓" in w for w in no_pos["warnings"]))

    def test_limit_state_field(self):
        """``limitState``：当日最高价未触及涨停时给出「未封上涨停」的判断依据，
        触及或未提供最高价时为空串（不臆造状态）。"""
        touched = self._order(high=105.0)["limitState"]
        self.assertIn("未触及涨停", touched)
        self.assertEqual(self._order(high=110.0)["limitState"], "")
        self.assertEqual(self._order()["limitState"], "")

    def test_us_order_and_session(self):
        """美股：无涨跌幅限制（「涨跌停」这项直接 pass），且时段按美东判断 ——
        用北京时间判断美股时段会把整晚的委托全判成「非交易时段」。"""
        res = R.check_order("buy", "AAPL", 10, 200.0, market="us", prev_close=200.0,
                            cash=100000.0, ts=us_ts(10, 0))
        self.assertTrue(res["ok"])
        self.assertEqual(res["board"], "us")
        self.assertIsNone(res["limit"]["up"])
        self.assertIn("无涨跌幅限制", [c["detail"] for c in res["checks"] if c["name"] == "涨跌停"][0])
        # 北京时间的 10:30 对美股是深夜 → 非交易时段
        self._assert_reject(R.check_order("buy", "AAPL", 10, 200.0, market="us",
                                          prev_close=200.0, cash=100000.0, ts=MORNING),
                            "不可申报")

    def test_unlimited_new_listing_passes_limit_check(self):
        """新股前 5 日不设涨跌幅：涨跌停校验应直接放行，且 limit.up 为 None（不给假数字）。

        用集合竞价时段构造（连续竞价时段还会叠加 ±2% 价格笼子校验，见下一条断言）：
        模块对「不设涨跌幅」的标的**仍然**套用连续竞价笼子 —— 这是保守方向，
        未取得「无涨跌幅限制股票放宽笼子」的权威来源，因此测试只钉住现状不判缺陷。
        """
        res = self._order(price=150.0, meta={"listedDays": 3}, ts=OPEN_AUCTION)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["limit"]["up"])
        self.assertTrue(res["limitRatio"]["unlimited"])
        self.assertIn("无涨跌幅限制",
                      [c["detail"] for c in res["checks"] if c["name"] == "涨跌停"][0])
        # 连续竞价：150 元距基准价 100 元远超 ±2% → 笼子这条仍然拦（保守口径）
        cage = self._order(price=150.0, meta={"listedDays": 3}, ts=MORNING)
        self.assertFalse(cage["ok"])
        self.assertTrue(any("有效价格范围" in r for r in cage["rejects"]))

    def test_dirty_arguments_never_raise(self):
        """任意脏组合（side/qty/price/cash/position 全脏）都不能抛异常：
        校验函数是「最后一道闸门」，它自己炸掉等于把拒单解释权交给了 500 页面。

        ``cash`` 只传 None（＝「未提供资金」这条合法分支，会告警但**不拒单**）；
        非 None 但无法解析的 cash（``""`` / ``"abc"`` / ``{}`` / ``True``）走的是**拒单**路径，
        由 TestCheckOrderCashRegression 单列（缺陷 F6 的回归）。
        """
        for bad in DIRTY:
            res = R.check_order(bad, bad, bad, bad, meta=bad, prev_close=bad, cash=None,
                                position=bad, frozen=bad, high=bad, low=bad, ts=MORNING)
            self.assertIn("ok", res)
            self.assertIsInstance(res["ok"], bool)
            self.assertIsInstance(res["rejects"], list)

    def test_unparseable_cash_is_rejected(self):
        """**已修复缺陷 F6 的回归（此前是 expectedFailure）**：``cash`` 非 None 但无法解析时
        既不抛 TypeError，也**不放行** —— 必须判为拒单（``ok=False``）。

        为什么必须拒单而不是「告警 + 跳过」：资金是下单的硬约束，
        ``None >= need`` 曾直接 ``TypeError``（服务端 500），而改成「只告警」后又走向另一个
        极端 —— 调用方拿到 ``ok=True`` 就会去撮合，「资金够不够」这道闸门实际没执行。
        本模块那条分支的注释写的就是「既不能当 0 也不能当通过」，因此拒单才是自洽的口径。

        期望值来源（手算 / 逐条核对，不是抄实现）：
          · ``ok`` 必须为 ``False`` —— rejects 非空即 ``not rejects`` = False；
          · rejects 里必须有一条中文原因包含「无法解析」，且用 ``%r`` 回显原值便于排查
            （例如 ``cash='abc'`` → 文案里出现 ``'abc'``）；
          · ``checks`` 里「可用资金」那一项必须 ``pass=False``（同一事实在结构化字段里可查，
            而不是只藏在字符串里）；该项存在的前提是本用例的买单在时段/价格等前置校验上全部
            通过（``ts=MORNING``：09:30–11:30 上午连续竞价、报价 = 昨收 → 不触发价格笼子），
            否则会提前 reject 到这里。
        """
        for bad in ("", "abc", " ", {}, [], True, float("nan")):
            res = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0,
                                cash=bad, ts=MORNING)
            self.assertIn("ok", res, repr(bad))
            self.assertIsInstance(res["ok"], bool, repr(bad))
            self.assertFalse(res["ok"], "无法解析的可用资金必须拒单，而不是放行：%r" % (bad,))
            hit = [r for r in res["rejects"] if "无法解析" in r]
            self.assertTrue(hit, "cash=%r 必须给出「可用资金无法解析」的拒单原因：%s"
                            % (bad, res["rejects"]))
            self.assertTrue(has_cjk(hit[0]), "拒单原因必须是中文，用户才知道为什么被拒")
            self.assertIn("已拒单", hit[0], "拒单原因要说明结论是「已拒单」而不是仅仅提示")
            self.assertIn(repr(bad), hit[0],
                          "拒单原因要用 %%r 回显原始值，便于排查脏参数：%r" % (bad,))
            cash_checks = [c for c in res["checks"] if c.get("name") == "可用资金"]
            self.assertEqual(len(cash_checks), 1,
                             "checks 里应恰好有一条「可用资金」：%s" % (res["checks"],))
            self.assertFalse(cash_checks[0]["pass"], "cash=%r 的资金校验项必须为 False" % (bad,))


# --------------------------------------------------------------------------- #
# I. 缺陷 F6 的回归：cash 无法解析时不抛异常，且必须**拒单**（不再「告警 + 放行」）
# --------------------------------------------------------------------------- #
class TestCheckOrderCashRegression(unittest.TestCase):
    """**缺陷 F6 的回归**（原来是 ``TypeError`` → 服务端 500）。

    修复路径（两段）：① ``core/rules.check_order`` 把 ``cash`` 先过 ``_num``，解析不出来时
    不再拿 ``None`` 去比较，因此崩溃已消除；② 本轮把这条分支从「只 append warning」改成
    ``_check("可用资金", False, ...)`` + ``rejects.append("…已拒单。")`` —— 与本模块注释
    「既不能当 0 也不能当通过」自洽。

    这里钉住「不抛异常 + 拒单 + 中文原因 + ``checks`` 对应项为 False + 字段齐全」；
    ``cash=None``（未提供）仍是合法分支、只告警不拒单，由下一个用例守住两者的边界。
    """

    def test_unparseable_cash_is_rejected(self):
        """``cash`` 传了但解析不出（"" / "abc" / {} / [] / True / NaN）→ **拒单**。

        期望值怎么来的（与用例完全同源的算式）：
          · ``ok = not rejects``；因为该分支必定 append 一条 rejects → ``ok is False``；
          · rejects 的文案来自实现：「可用资金无法解析（原值 %r），无法校验资金是否充足，已拒单。」
            因此分别断言 ① 含「无法解析」② 含「已拒单」③ 含 ``repr(bad)``（原值回显）
            ④ 是中文（``has_cjk``）；
          · ``checks`` 里名字为「可用资金」的项只有一条，且 ``pass is False`` —— 这是
            「结构化字段里也能看出没过」的证据，避免只靠一句文案。
        """
        for bad in ("", "abc", " ", {}, [], True, float("nan")):
            res = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0, cash=bad,
                                ts=MORNING)
            self.assertIn("ok", res, repr(bad))
            self.assertIsInstance(res["ok"], bool, repr(bad))
            self.assertIsInstance(res["rejects"], list, repr(bad))
            self.assertIsInstance(res["warnings"], list, repr(bad))
            self.assertFalse(res["ok"], "cash=%r 必须拒单（ok=False），不得放行" % (bad,))
            hit = [r for r in res["rejects"] if "无法解析" in r]
            self.assertTrue(hit, "cash=%r 必须留下「可用资金无法解析」的拒单原因：%s"
                            % (bad, res["rejects"]))
            self.assertTrue(has_cjk(hit[0]), "拒单原因必须是中文，用户才知道发生了什么")
            self.assertIn("已拒单", hit[0])
            self.assertIn(repr(bad), hit[0], "拒单原因要用 %%r 回显原始值：%r" % (bad,))
            cash_checks = [c for c in res["checks"] if c.get("name") == "可用资金"]
            self.assertEqual(len(cash_checks), 1, repr(res["checks"]))
            self.assertFalse(cash_checks[0]["pass"],
                             "cash=%r 时 checks 的「可用资金」项必须为 False" % (bad,))
            # 字段齐全：check_order 的契约字段一个都不能少
            for key in ("ok", "rejects", "warnings", "checks", "side", "code",
                        "qty", "price", "market", "board", "label", "limit",
                        "limitRatio", "fee", "lot", "minQty", "sellable",
                        "session", "rulesVersion"):
                self.assertIn(key, res, "cash=%r 时缺字段 %s" % (bad, key))

    def test_missing_cash_is_a_legal_branch_and_still_warns(self):
        """``cash=None`` 是「未提供资金」的合法分支（调用方可能只想校验规则），
        它同样只能告警、不能因此拒单 —— 与「解析不出」区分开但都不得抛异常。"""
        res = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0, cash=None,
                            ts=MORNING)
        self.assertTrue(res["ok"])
        self.assertTrue(any("未提供可用资金" in w for w in res["warnings"]))


# --------------------------------------------------------------------------- #
# J. 撮合可行性（can_fill）
# --------------------------------------------------------------------------- #
class TestCanFill(unittest.TestCase):
    def test_limit_up_buy_is_blocked(self):
        """涨停价买入且当日最高价未突破涨停 → 封板，买不到。
        把它判成「能成交」会在连板股上凭空造出每天都能买在涨停价的虚假收益。"""
        res = R.can_fill("buy", 110.0, prev_close=100.0, code="600519", ts=MORNING)
        self.assertFalse(res["ok"])
        self.assertIn("涨停封板", res["reason"])
        self.assertEqual(res["limits"]["up"], 110.00)

    def test_limit_up_buy_allowed_when_high_breaks_limit(self):
        """当日最高价**突破**涨停价 → 说明板被打开过，买单可以成交。"""
        res = R.can_fill("buy", 110.0, prev_close=100.0, code="600519", high=110.5, ts=MORNING)
        self.assertTrue(res["ok"])
        self.assertIn("可成交", res["reason"])
        # 最高价正好等于涨停价（没突破）仍然算封板 —— 只有「突破」才算打开
        still = R.can_fill("buy", 110.0, prev_close=100.0, code="600519", high=110.0, ts=MORNING)
        self.assertFalse(still["ok"])
        self.assertIn("涨停封板", still["reason"])

    def test_limit_down_sell_is_blocked(self):
        """跌停价卖出且当日最低价未跌破跌停 → 卖不出去（反向同理）。"""
        blocked = R.can_fill("sell", 90.0, prev_close=100.0, code="600519", ts=MORNING)
        self.assertFalse(blocked["ok"])
        self.assertIn("跌停封板", blocked["reason"])
        opened = R.can_fill("sell", 90.0, prev_close=100.0, code="600519", low=89.5, ts=MORNING)
        self.assertTrue(opened["ok"])

    def test_suspended_never_fills(self):
        """停牌 → 任何价格都不成交（哪怕报价正好在涨跌停区间内）。"""
        res = R.can_fill("buy", 100.0, prev_close=100.0, code="600519",
                         meta={"suspended": True}, ts=MORNING)
        self.assertFalse(res["ok"])
        self.assertIn("停牌", res["reason"])

    def test_normal_price_fills(self):
        """中间价成交：未封板即视为可成交（真实排队深度本模块不模拟，reason 里说清）。"""
        res = R.can_fill("buy", 105.0, prev_close=100.0, code="600519", ts=MORNING)
        self.assertTrue(res["ok"])
        self.assertIn("未封板", res["reason"])
        sell = R.can_fill("sell", 95.0, prev_close=100.0, code="600519", ts=MORNING)
        self.assertTrue(sell["ok"])

    def test_us_market_always_fills_on_price(self):
        """美股不设个股涨跌幅：不判封板，只按价格成交（熔断与流动性不在本模块模拟范围）。"""
        res = R.can_fill("buy", 999.0, prev_close=100.0, code="AAPL", market="us",
                         ts=us_ts(10, 0))
        self.assertTrue(res["ok"])
        self.assertIn("无涨跌幅限制", res["reason"])
        self.assertIsNone(res["limits"]["up"])

    def test_unlimited_new_listing_fills(self):
        """新股前 5 日不设涨跌幅：不判封板，按价格成交。"""
        res = R.can_fill("buy", 150.0, prev_close=100.0, code="600519",
                         meta={"listedDays": 3}, ts=MORNING)
        self.assertTrue(res["ok"])
        self.assertIn("无涨跌幅限制", res["reason"])

    def test_missing_price_does_not_fill(self):
        """没有有效价格就不能成交（且不抛异常）。"""
        for price in (None, 0, -1, "abc", float("nan")):
            res = R.can_fill("buy", price, prev_close=100.0, code="600519", ts=MORNING)
            self.assertFalse(res["ok"], repr(price))
            self.assertIn("无有效价格", res["reason"])

    def test_prev_close_falls_back_to_price(self):
        """没给前收盘价时用报价自身当基准（近似），不应因此拒单或抛异常。"""
        res = R.can_fill("buy", 100.0, code="600519", ts=MORNING)
        self.assertIn("ok", res)
        self.assertEqual(res["limits"]["basis"], 100.0)


# --------------------------------------------------------------------------- #
# K. 规则总表与版本
# --------------------------------------------------------------------------- #
class TestRulesTable(unittest.TestCase):
    def test_structure_and_json_safe(self):
        """规则总表是接口直接透传给前端的对象（``/api/rules``），
        必须含 version/sources/unverified/boards/extra/sessions/note，且能
        ``json.dumps(allow_nan=False)`` —— NaN 会让前端整页崩掉。"""
        table = R.rules_table()
        for key in ("version", "sources", "unverified", "boards", "extra", "sessions", "note"):
            self.assertIn(key, table, "规则总表缺 %s" % key)
        text = json.dumps(table, allow_nan=False, ensure_ascii=False)
        self.assertIn("2026-07-06", text)
        self.assertIsInstance(table["boards"], list)
        self.assertTrue(table["boards"])
        self.assertTrue(all(isinstance(row, dict) for row in table["boards"]))
        for row in table["boards"]:
            self.assertTrue(has_cjk(row["note"]), row["note"])
        for item in table["extra"]:
            self.assertIn("item", item)
            self.assertIn("value", item)
            self.assertTrue(has_cjk(item["value"]))

    def test_boards_rows_and_limits(self):
        """四个 A 股板块的涨跌幅与申报单位要在总表里一一对上（10/20/20/30），
        科创板还要写清「200 股起、超过部分可 1 股递增」。"""
        rows = {row["board"]: row for row in R.rules_table()["boards"]}
        self.assertEqual(sorted(rows), ["bse", "gem", "main", "star"])
        self.assertEqual(rows["main"]["limit"], "10%")
        self.assertEqual(rows["gem"]["limit"], "20%")
        self.assertEqual(rows["star"]["limit"], "20%")
        self.assertEqual(rows["bse"]["limit"], "30%")
        self.assertIn("200 股起", rows["star"]["lot"])
        self.assertIn("100 股整数倍", rows["main"]["lot"])

    def test_extra_covers_2026_07_06_changes(self):
        """2026-07-06 的两项新规必须在总表里看得见：
        ① 风险警示股涨跌幅放宽到 10%（旧口径 5%）；
        ② 盘后固定价格交易扩展到全部 A股 + ETF。
        这两项若只在代码里生效、不在界面口径里体现，用户会按旧规则理解自己的下单。"""
        table = R.rules_table()
        extra = {item["item"]: item["value"] for item in table["extra"]}
        st_row = [v for k, v in extra.items() if "风险警示" in k]
        self.assertEqual(len(st_row), 1)
        self.assertIn("2026-07-06", st_row[0])
        self.assertIn("10%", st_row[0])
        self.assertIn("5%", st_row[0], "要写明旧口径，用户才知道自己记忆里的 5% 已过时")
        self.assertTrue(table["params"]["allowAfterHoursFixed"])
        self.assertTrue(any(w["key"] == "afterHoursFixed"
                            for w in table["sessions"]["windows"]),
                        "盘后固定价格必须是总表里可见的时段")
        for item in ("T+1", "涨跌停价", "价格笼子", "费用", "封板"):
            self.assertIn(item, extra, "总表要摊开「%s」这条口径" % item)
        self.assertIn("印花税", extra["费用"])
        self.assertIn("5 元", extra["费用"])

    def test_sessions_are_embedded(self):
        """总表内嵌完整时段表（含 09:25–09:30 不接受申报、14:57–15:00 不可撤单）。"""
        sessions = R.rules_table()["sessions"]
        self.assertEqual(sessions["market"], "cn")
        self.assertTrue(sessions["windows"])
        text = json.dumps(sessions, ensure_ascii=False)
        self.assertIn("09:25", text)
        self.assertIn("15:05", text)
        self.assertIn("撤单", text)

    def test_version_string_carries_the_rule_epoch(self):
        """``RULES_VERSION`` 必须含「2026-07-06」这个口径日期（界面与总表都要展示，
        用户才知道引擎按哪一版在算）；同时断言该日期对应的两项新规确实生效。"""
        self.assertIn("2026-07-06", R.RULES_VERSION)
        self.assertIn("新版口径", R.RULES_VERSION)
        table = R.rules_table()
        self.assertEqual(table["version"], R.RULES_VERSION)
        # 版本对应的两项新规都要能在总表 / 参数里被观察到
        self.assertIn("2026-07-06 起",
                      [i["value"] for i in table["extra"] if "风险警示" in i["item"]][0])
        self.assertTrue(table["params"]["allowAfterHoursFixed"])
        self.assertEqual(table["params"]["stLimitRatio"], 0.10)

    def test_sources_and_unverified_are_honest(self):
        """``sources`` 给出条文/公告出处（至少含 http 链接），``unverified`` 如实列出
        拿不到权威来源的项 —— 宁缺勿造：把没来源的数字写成硬规则比缺一条更危险。"""
        table = R.rules_table()
        self.assertTrue(table["sources"])
        self.assertTrue(all(isinstance(s, str) and s for s in table["sources"]))
        self.assertTrue(any(s.startswith("http") for s in table["sources"]))
        self.assertTrue(table["unverified"])
        for item in table["unverified"]:
            self.assertTrue(has_cjk(item), "未验证项要用中文说清「为什么没写成硬规则」")
        self.assertNotIn(R.RULES_VERSION, "")   # 版本串非空
        self.assertIn("规则口径", table["note"])

    def test_us_table(self):
        """美股规则总表：无个股涨跌幅限制、结算 T+1、熔断 7%/13%/20%。"""
        table = R.rules_table(market="us")
        self.assertEqual(table["market"], "us")
        self.assertEqual(len(table["boards"]), 1)
        self.assertEqual(table["boards"][0]["board"], "us")
        self.assertIn("无个股涨跌幅限制", table["boards"][0]["limit"])
        text = json.dumps(table, allow_nan=False, ensure_ascii=False)
        self.assertIn("熔断", text)
        self.assertEqual([w["key"] for w in table["sessions"]["windows"]],
                         ["preMarket", "regular", "afterMarket"])

    def test_dirty_market_and_params(self):
        """脏 market / params 一律回落到 A 股默认口径，不抛异常。"""
        for mkt in DIRTY:
            table = R.rules_table(mkt, params=mkt)
            self.assertEqual(table["market"], "cn", repr(mkt))
            json.dumps(table, allow_nan=False)


# --------------------------------------------------------------------------- #
# L. 脏输入：所有公开函数都不抛异常
# --------------------------------------------------------------------------- #
class TestDirtyInput(unittest.TestCase):
    def test_public_api_surface(self):
        """``__all__`` 里声明的名字必须全部存在（服务端/前端按名字取用），
        函数类名字必须可调用。缺一个就是线上 500。"""
        self.assertTrue(R.__all__)
        for name in R.__all__:
            self.assertTrue(hasattr(R, name), "缺少公开名 %s" % name)
        for name in ("board_of", "limit_of", "limit_prices", "fee_of", "session_of",
                     "is_tradable_now", "session_windows", "lot_of", "min_qty_of",
                     "round_qty", "sellable_qty", "check_order", "can_fill",
                     "stamp_tax_of", "rules_table"):
            self.assertTrue(callable(getattr(R, name)), name)
        self.assertEqual(R.MARKET_CN, "cn")
        self.assertEqual(R.MARKET_US, "us")

    def test_every_public_function_survives_dirty_values(self):
        """脏输入矩阵逐个函数跑一遍：None / 空串 / 空白 / 0 / 负数 / 非数字串 /
        字符串数字 / NaN / inf / list / dict / bool。

        「任何输入都不抛异常」是本模块写明的契约（服务端会把这些函数直接接到 HTTP 上），
        因此这里对**每个**公开函数都过一遍，而不是抽样几个。
        """
        calls = {
            "board_of": lambda bad: R.board_of(bad),
            "board_of-name": lambda bad: R.board_of("600519", name=bad),
            "board_of-market": lambda bad: R.board_of("600519", market=bad),
            "limit_of": lambda bad: R.limit_of(bad, meta=bad),
            "limit_prices": lambda bad: R.limit_prices(bad, bad, prev_close=bad),
            "limit_prices-meta": lambda bad: R.limit_prices("600519", 100.0, meta=bad),
            "limit_prices-params": lambda bad: R.limit_prices("600519", 100.0, params=bad),
            "fee_of": lambda bad: R.fee_of(bad, bad, bad, fees=bad),
            "fee_of-board": lambda bad: R.fee_of("buy", 100, 100.0, board=bad),
            "stamp_tax_of": lambda bad: R.stamp_tax_of(bad, bad, bad),
            "session_of": lambda bad: R.session_of(None, market=bad, params=bad),
            "session_of-numeric-ts": lambda bad: R.session_of(
                bad if isinstance(bad, (int, float)) and not isinstance(bad, bool)
                and math.isfinite(bad) else None),
            "is_tradable_now": lambda bad: R.is_tradable_now(None, market=bad),
            "session_windows": lambda bad: R.session_windows(bad, params=bad),
            "lot_of": lambda bad: R.lot_of(bad),
            "min_qty_of": lambda bad: R.min_qty_of(bad),
            "round_qty": lambda bad: R.round_qty(bad, bad, side=bad, position=bad),
            "sellable_qty": lambda bad: R.sellable_qty(bad, bad, bad),
            "check_order": lambda bad: R.check_order(bad, bad, bad, bad, prev_close=bad,
                                                     position=bad, today_bought=bad,
                                                     frozen=bad, cash=None, high=bad, low=bad,
                                                     meta=bad, params=bad, fees=bad,
                                                     market=bad, name=bad, board=bad,
                                                     ts=MORNING),
            "can_fill": lambda bad: R.can_fill(bad, bad, prev_close=bad, code=bad,
                                               market=bad, high=bad, low=bad, meta=bad,
                                               params=bad, name=bad, ts=MORNING),
            "can_fill-us-ts": lambda bad: R.can_fill("buy", 100.0, prev_close=100.0,
                                                     code="AAPL", market="us", ts=None),
            "rules_table": lambda bad: R.rules_table(bad, params=bad),
        }
        self.assertEqual(len(calls), 22, "矩阵应覆盖全部公开函数与主要参数位")
        for bad in DIRTY:
            for label, fn in calls.items():
                try:
                    res = fn(bad)
                except Exception as exc:      # noqa: BLE001 —— 失败即断言，便于报告函数名
                    self.fail("脏输入 %r 让 %s 抛异常：%s: %s"
                              % (bad, label, type(exc).__name__, exc))
                self.assertIsNotNone(res, "%s(%r) 返回了 None" % (label, bad))

    def test_structures_survive_dirty_values(self):
        """脏输入下返回值仍是**结构完整**的 dict / list（不是 None、不是半截对象）：
        下游（服务端 / 前端）会直接按键取值，半截对象会变成 KeyError。"""
        for bad in DIRTY:
            self.assertIn("up", R.limit_prices(bad, bad))
            self.assertIn("note", R.limit_prices(bad, bad))
            self.assertIn("items", R.fee_of(bad, bad, bad))
            self.assertIn("total", R.fee_of(bad, bad, bad))
            self.assertIn("session", R.session_of(None, market="cn"))
            self.assertIn("ok", R.check_order(bad, bad, bad, bad, ts=MORNING))
            self.assertIn("ok", R.can_fill(bad, bad))
            self.assertIn("windows", R.session_windows())
            self.assertIn("boards", R.rules_table())


# --------------------------------------------------------------------------- #
# M. 已修复缺陷 F3/F4 的回归：ts 的脏输入与「秒 / 毫秒」单位
# --------------------------------------------------------------------------- #
class TestTsUnitRegression(unittest.TestCase):
    """**已修复缺陷 F3/F4 的回归**（本轮之前这里是两个 ``@unittest.expectedFailure``）。

    缺陷原状：``_local_minutes`` 直接 ``int(ts)`` + ``datetime.fromtimestamp(stamp)``，
    于是 ``int("")`` / ``int("abc")`` / ``int(nan)`` → ValueError、``int({})`` / ``int([])``
    → TypeError、``int(1e30)`` → OverflowError；而这些异常会从 ``session_of`` 一路抛到
    ``check_order`` / ``can_fill`` 的调用方（服务端 → 500）。更隐蔽的是**单位**：
    ``core/trader.py`` 全链路传**毫秒**（``now_ms()``），被当成秒就是「公元 58679 年」→
    ``ValueError: year 58679 is out of range``，即**美股模拟成交直接不可用**
    （A 股因为有涨跌停价提前 return 而侥幸绕过）。

    修复后的口径（``core/rules._stamp_seconds``）：**先判单位再判范围** ——
    ``> 1e11`` 视为毫秒并 ``/1000``，随后 ``> 4.2e9`` 秒（≈ 2103 年）才视为越界；
    只有真正无法解析 / 非正 / 越界的脏值才**退回「现在」**。
    因此本类断言三件事：①脏 ``ts`` 不抛异常且返回结构完整；②毫秒真的被**换算**（历史毫秒
    时间戳返回的是它代表的时刻，本轮之前会被静默换成「现在」，见
    :meth:`test_historical_ms_is_converted_not_replaced_by_now`）；③**同一时刻**的秒与毫秒
    两种写法给出同一结论（``trader`` 传的正是「现在」的毫秒，这正是它能工作的原因）。

    单位判定的顺序为什么必须是这样：上界若写成 4e10（≈ 公元 3237 年的秒数），真实毫秒
    时间戳（≈ 1.8e12）会先被判成越界 → 降级为「现在」，``x > 1e11`` 那条分支永远不可达；
    实盘表现为「用历史时间戳回放时段判定时静默拿到当下」，属于不报错但算错的类型。
    """

    def test_dirty_ts_falls_back_to_now_without_raising(self):
        # 缺陷 F4 回归："" / "abc" / NaN / {} / [] / 1e30 都不得抛异常，且字段齐全
        for bad in ("", "abc", float("nan"), {}, [], 1e30):
            res = R.session_of(bad)
            for key in ("market", "weekday", "minutes", "local", "session", "label",
                        "tradable", "canCancel", "note"):
                self.assertIn(key, res, "ts=%r 时缺字段 %s" % (bad, key))
            self.assertIsInstance(res["session"], str, repr(bad))
            self.assertTrue(res["session"], "session 不能是空串（%r）" % (bad,))
            self.assertIsInstance(res["tradable"], bool, repr(bad))
            self.assertIsInstance(res["minutes"], int, repr(bad))
            self.assertIsInstance(res["weekday"], int, repr(bad))
        # 美股同样：ts 的解析与市场无关（不得只在 A 股路径上降级）
        for bad in ("", "abc", {}, 1e30):
            res = R.session_of(bad, market="us")
            self.assertIsInstance(res["session"], str, repr(bad))
            self.assertIn("tradable", res)

    def test_dirty_ts_inside_check_order_and_can_fill_does_not_raise(self):
        # 缺陷 F4 回归：check_order / can_fill 不再把 session 的异常透传给调用方
        for bad in ("", "abc", float("nan"), {}, [], 1e30):
            res = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0,
                                cash=1e6, ts=bad)
            self.assertIn("ok", res, repr(bad))
            self.assertIsInstance(res["ok"], bool, repr(bad))
            self.assertIn("session", res, repr(bad))
            self.assertIsInstance(res["session"]["session"], str, repr(bad))
            fill = R.can_fill("buy", 105.0, prev_close=100.0, code="600519", ts=bad)
            self.assertIn("ok", fill, repr(bad))
            self.assertIsInstance(fill["ok"], bool, repr(bad))

    def test_ms_ts_inside_check_order_and_can_fill_does_not_raise(self):
        """缺陷 F3 的最小子集回归：直接把**毫秒**（``now_ms()`` 的形状）喂进来，
        ``check_order`` / ``can_fill`` 都不得抛 ``ValueError: year ... is out of range``。"""
        now_ms = int(time.time() * 1000)
        self.assertGreater(now_ms, 1e11, "本用例的前提是「毫秒」，不是秒")
        res = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0, cash=1e6,
                            ts=now_ms)
        self.assertIn("ok", res)
        self.assertIsInstance(res["ok"], bool)
        fill = R.can_fill("buy", 999.0, code="AAPL", market="us", ts=now_ms)
        self.assertIn("ok", fill)
        self.assertIsInstance(fill["ok"], bool)
        # 美股没有涨跌停价 → 必然走到 session_of（这正是缺陷 F3 的爆炸点）
        self.assertIn("session", fill)

    def test_historical_ms_is_converted_not_replaced_by_now(self):
        """**本轮修复点的回归**：**历史**毫秒时间戳必须被换算成它代表的时刻，
        而不是被静默换成「现在」。

        为什么单独钉这一条：此前 ``_stamp_seconds`` 的上界是 4e10（≈ 公元 3237 年的秒数），
        而真实毫秒时间戳 ≈ 1.8e12 —— 于是它先被判成「越界」→ 退回 ``time.time()``，
        ``if x > 1e11: x /= 1000`` 这条毫秒分支**永远不可达**。表现是「不报错但算错」：
        用历史时间戳回放时段判定时，拿到的是「当下」的时段结论。

        期望值来源（手算）：取两个**固定**的北京时间时刻，毫秒 = 秒 × 1000：
          · 2026-09-17（周四）10:30 → ``morning``（上午连续竞价 09:30–11:30 内），
            ``local`` 必须逐字等于 ``"2026-09-17 10:30:00"``；
          · 2026-09-19（周六）12:00 → ``weekend``（周末全天休市）。
        这里**刻意不打桩 ``time.time``**：打的桩会让「退回现在」与「正确换算」得到同一结果，
        断言就退化成空跑。第二个断言（周六 → weekend）尤其能排除巧合 —— 只有真的换算过
        时间戳才可能得出「周末」。
        """
        thu_ms = cn_ts(10, 30) * 1000            # 2026-09-17 10:30 北京 = 02:30 UTC（周四）
        sat_ms = cn_ts(12, 0, SAT) * 1000        # 2026-09-19 12:00 北京（周六）
        self.assertGreater(thu_ms, 1e11, "本用例的前提是「毫秒」时间戳")

        thu = R.session_of(thu_ms)
        self.assertEqual(thu["session"], "morning")
        self.assertEqual(thu["local"], "2026-09-17 10:30:00",
                         "历史毫秒时间戳必须换算成它自己的时刻，不能被换成「现在」")
        self.assertEqual(thu["minutes"], 10 * 60 + 30)
        self.assertTrue(thu["tradable"])

        sat = R.session_of(sat_ms)
        self.assertEqual(sat["session"], "weekend")
        self.assertEqual(sat["local"], "2026-09-19 12:00:00")

        # 同一条路径也要从 check_order / can_fill 走通（它们的时段结论同样来自 ts）
        msg = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0, cash=1e6,
                            ts=thu_ms)
        self.assertEqual(msg["session"]["local"], thu["local"])
        self.assertEqual(msg["session"]["session"], "morning")
        # 美股无涨跌停价 → can_fill 会显式给出 session（这正是缺陷 F3 当初的爆炸点）
        fill = R.can_fill("buy", 999.0, code="AAPL", market="us", ts=sat_ms)
        self.assertEqual(fill["session"], "weekend")

    def test_ms_and_second_ts_agree_at_the_same_instant(self):
        """**同一时刻**的秒与毫秒两种写法必须给出同一结论（回归 F3）。

        做法：把 ``core.rules`` 眼里的「现在」也冻结在固定时刻（2026-09-17 10:30 北京时间），
        于是
          · ``session_of(秒)`` → 用秒值本身 → 10:30 → ``morning``；
          · ``session_of(毫秒)`` → ``_stamp_seconds`` 判出单位并 ``/1000`` → 同一时刻。
        两者必须一致（含 ``local`` / ``minutes`` 与 ``check_order`` 的 ``ok``）。
        打桩 ``time.time`` 在这里是**双保险**：即使毫秒分支再次坏掉（退回「现在」），
        这条断言仍然能过 —— 所以它有意的「宽容」由上面的历史毫秒用例补上，
        两条合起来才能同时覆盖「换算正确」与「两种单位同结论」。

        为什么必须断言「相同」而不只是「不抛异常」：毫秒被当成秒属于**不报错但算错** ——
        时段结论会整体反向（「现在能不能申报」），而调用方看不到任何异常。
        """
        fixed_sec = cn_ts(10, 30)
        fixed_ms = fixed_sec * 1000
        with mock.patch.object(R.time, "time", return_value=float(fixed_sec)):
            sec_sess = R.session_of(fixed_sec)
            ms_sess = R.session_of(fixed_ms)
            self.assertEqual(sec_sess["session"], "morning", "先确认基准时刻是上午连续竞价")
            self.assertEqual(ms_sess["session"], sec_sess["session"])
            self.assertEqual(ms_sess["local"], sec_sess["local"])
            self.assertEqual(ms_sess["minutes"], sec_sess["minutes"])

            sec_msg = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0,
                                    cash=1e6, ts=fixed_sec)
            ms_msg = R.check_order("buy", "600519", 200, 100.0, prev_close=100.0,
                                   cash=1e6, ts=fixed_ms)
            self.assertEqual(sec_msg["session"]["session"], "morning")
            self.assertEqual(ms_msg["session"]["session"], sec_msg["session"]["session"])
            self.assertEqual(ms_msg["session"]["local"], sec_msg["session"]["local"])
            self.assertEqual(ms_msg["ok"], sec_msg["ok"])
            self.assertTrue(sec_msg["ok"], "上午连续竞价的正常单应当通过校验")

            sec_fill = R.can_fill("buy", 999.0, code="AAPL", market="us", ts=fixed_sec)
            ms_fill = R.can_fill("buy", 999.0, code="AAPL", market="us", ts=fixed_ms)
            self.assertEqual(sec_fill["ok"], ms_fill["ok"])
            self.assertEqual(sec_fill.get("session"), ms_fill.get("session"))


# --------------------------------------------------------------------------- #
# 缺陷台账（本轮由主程修复，测试同步为「回归」；仍未修好的部分单独标注）
# --------------------------------------------------------------------------- #
# F1  core/rules.fee_of：命中「佣金最低值」时最低值没有写回 items/total。
#     【已修复】现在 amount = round(commissionMin, 4) 并计入 total，同时保留 rateAmount。
#     数字：R.fee_of("buy", 100, 100.0)["total"] → 5.441（修复前 2.941）；
#           R.fee_of("buy", 100, 100.0, market="us")["total"] → 1.0（修复前 0.5）。
#     回归：TestFeeMinCommissionRegression（3 个用例，含负向断言「2.941 不得回来」）。
#
# F4  core/rules.session_of（及 check_order / can_fill 的 ts 参数）：脏 ts 会抛
#     ValueError / TypeError / OverflowError。
#     【已修复】ts 统一走 ``_stamp_seconds``：无法解析 / 非正 / 越界 → 退回「现在」。
#     回归：TestTsUnitRegression.test_dirty_ts_*（2 个用例，断言不抛异常 + 字段齐全）。
#
# F3  core/trader.execute_orders 把**毫秒** ts 传给 core/rules（当时是秒口径）→
#     美股 paper 成交抛 ``ValueError: year 58679 is out of range``。
#     【已修复】毫秒不再被当成秒：``_stamp_seconds`` **先判单位**（``> 1e11`` → ``/1000``）
#     **再判范围**（``> 4.2e9`` 秒 ≈ 2103 年视为越界），trader 传的 ``now_ms()`` 换算后
#     落在当下，时段结论正确（不再 500）。
#     修复前的问题（如实记录，已修）：上界写成 4e10 会先把真实毫秒判成越界、静默退回「现在」，
#     于是 ``if x > 1e11: x /= 1000`` 分支**永远不可达**；表现为「历史毫秒时间戳被丢掉、
#     当成当下」，属于不报错但算错。
#     回归：TestTsUnitRegression.test_ms_ts_inside_check_order_and_can_fill_does_not_raise +
#           TestTsUnitRegression.test_ms_and_second_ts_agree_at_the_same_instant +
#           TestTsUnitRegression.test_historical_ms_is_converted_not_replaced_by_now
#           （本轮新增，专门覆盖「历史毫秒必须换算而不是换成现在」这条修复）+
#           tests/test_trader.py::TestTraderRulesRegression 的美股成交用例。
#
# F6  core/rules.check_order：``cash`` 非 None 但 ``_num`` 解析不出来时
#     ``None >= need`` → TypeError（服务端 500）。
#     【已修复（两段）】① 崩溃已消除：改走 ``cash_num is None`` 分支；
#     ② 本轮把该分支从「只 append warning」改为 ``_check("可用资金", False, ...)`` +
#     rejects「可用资金无法解析（原值 …），无法校验资金是否充足，已拒单。」——
#     与实现注释「既不能当 0 也不能当通过」自洽：资金是下单的硬约束，校验不了就不能放行。
#     回归：TestCheckOrderCashRegression.test_unparseable_cash_is_rejected（拒单 + 中文原因
#     + ``checks`` 项为 False + 字段齐全）、
#     TestCheckOrder.test_unparseable_cash_is_rejected（入口级同一事实，本轮由
#     expectedFailure 转为常规用例，本文件的 expectedFailure 因此清零）。
#     边界：``cash=None``（未提供资金）仍是合法分支、只告警不拒单，由
#     TestCheckOrderCashRegression.test_missing_cash_is_a_legal_branch_and_still_warns 守住。
#
# F2  core/trader._positions 把 ``todayBought`` / ``todayBoughtOn`` 丢出字段白名单，
#     跨调用的「当日买入当日卖出」不被拦截（T+1 形同虚设）。
#     【已修复】_positions 已保留这两个字段（另有 _trade_day / _sellable_of）。
#     回归：tests/test_trader.py::TestTraderRulesRegression（同批次与跨批次两组用例）。
#
# F5（口径提示，非缺陷）09:20:00 整点被判为「不可撤单」：交易所口径是 9:20–9:25 不接受撤单，
#     因此整点关闭窗口是正确且保守的；测试用 09:19 覆盖「可撤单」、09:20 覆盖「不可撤单」。


if __name__ == "__main__":
    unittest.main(verbosity=2)
