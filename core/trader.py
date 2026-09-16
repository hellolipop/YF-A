#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AlphaDesk 模拟交易 / 自动交易引擎（core/trader.py）。

模块定位
--------
把 ``core/advisor.py`` 的逐只研判（``recommend()`` 返回的 ``rows``）落成**可审计的
委托单**，并在 paper 模式下按调用方给的最新报价做**模拟成交**。数据流单向、每层
可单独替换::

    研判 recommend()  →  计划 plan_orders()  →  撮合 execute_orders()  →  落库 store

对外 API（服务端按这些名字调用，签名稳定）
------------------------------------------
``MODES`` / ``ORDER_STATUS`` / ``CONFIRM_HEADER`` / ``DEFAULT_CONFIG`` /
``default_config`` / ``normalize_config`` / ``get_config`` / ``save_config`` /
``account_id`` / ``ensure_account`` / ``account_view`` / ``plan_orders`` /
``execute_orders`` / ``cancel_order`` / ``close_position`` / ``reset_account`` /
``scan`` / ``ack_order`` / ``export_intents`` / ``trade_snapshot``。

安全属性（本项目最重要的约束）
------------------------------
1. **dryrun 绝不改钱**：``config["mode"] == "dryrun"``（默认值）时
   :func:`execute_orders` 只把订单写回 ``pending``，不动 ``cash`` / ``positions`` /
   ``realizedPnl`` / ``feeTotal``，也不追加权益点；:func:`close_position` 同样受
   dryrun 保护（手动操作只是**不受 enabled 限制**，不是不受演练模式限制）。
2. **默认不出手**：``DEFAULT_CONFIG["enabled"] is False``，即「装好引擎也不会自己
   下单」；``scan(..., execute=False)`` 是默认值，默认只出计划。
3. **不静默丢弃**：:func:`plan_orders` 的每一只标的要么产出委托单，要么进
   ``skipped`` 并写清「卡在哪道闸门、用了什么参数」，绝不出现「既没订单也没解释」
   的标的（``hold`` / ``watch`` 属明确无需动作，只计数不记 skip）。
4. **不部分成交**：paper 模式下现金不足 / 持仓不足一律 ``rejected`` + 中文
   ``error``，绝不悄悄缩量成交。
5. **不抛异常**：脏输入（``advice`` 不是 dict、``rows`` 缺字段、``quotes`` 为空、
   代码为空、``orders`` 里混入非 dict…）一律降级为 ``skipped`` / ``ok=False`` /
   默认值，且全部返回值都能 ``json.dumps(..., allow_nan=False)``。

成本口径（与 ``core/advisor.py`` 的 ``_round_trip`` 完全一致）
-------------------------------------------------------------
    买入价 = 中间价 × (1 + slippage)，卖出价 = 中间价 × (1 − slippage)，双边按费率计费：

    buy_cost = qty × price × (1 + slippage) × (1 + feeRate)
    sell_net = qty × price × (1 − slippage) × (1 + feeRate)
    new_avg  = (qty × avg + fillQty × fillPrice + fee) / (qty + fillQty)

减仓 / 清仓**不动** ``avgPrice``，只累计 ``realizedPnl``（已实现盈亏，买入费用已
摊进 ``avgPrice``，因此已实现盈亏是「净收入 − 含费成本」）。``fillPrice`` 指的是
**含滑点的成交价**，``slippage`` 字段指的是**滑点成本金额**（= qty × 中间价 × 滑点率），
与 ``core/storage.py`` 里 trades 表「fee / slippage 都是金额」的口径一致；费率与
滑点**率**放在订单的 ``payload.gates``（风控快照）里备查。

为什么不复用 core/portfolio.Account 与 core/fills
-------------------------------------------------
``core/portfolio.Account`` 是「**单标的、按 bar 撮合**」的策略账户（全仓进全仓出，
成交价由 ``core/fills`` 的回测成交模型给出）；``core/fills`` 是「回测逐根 K 线成交」
的模型。本模块是「**多标的组合账户、按实时报价成交**」的模拟盘：持仓是一个列表、
按整手取整、按调用方给的最新报价成交，语义与前者不同。硬复用会让两边都变形
（要么把组合账户压成单标的，要么让回测模型承担实时报价语义），因此这里只**对齐
成本公式**，不共享对象。

账户状态与 realizedPnl / feeTotal 的存放（重要取舍）
-----------------------------------------------------
``store.save_trade_state()`` 只透传 ``positions``（外加现金与成本参数），账户级累计量
``realizedPnl`` / ``feeTotal`` 放不进去；把它们冗余进每一条持仓、或在 ``positions``
里塞一个「特殊项」都会污染持仓结构。因此这两个量用 ``store.meta_get/meta_set`` 以
``trade:meta:<accountId>`` 为键存 JSON：既不改存储层，也不破坏持仓语义。
``meta_*`` 不可用时（传入的是简化 store）读回 0.0 并跳过写入，不抛异常。

账户 id
-------
``account_id(market, mode)`` = ``"<mode>:<market>"``，例如 ``"paper:cn"`` /
``"dryrun:cn"``。**dryrun 与 paper 各自一本账**：演练产生的计划永远不会污染模拟
成交账户（这是「dryrun 绝不改钱」之外的第二道保险）。``account_id(market)`` 不带
mode 时按 ``"paper"`` 处理，便于服务端单独查模拟成交账户。

整手（lot）
-----------
取自 ``core/kelly.py`` 的 ``LOTS``：A 股 100 股/手、美股 1 股/手。本模块的股数
**一律向下取整**（与 ``kelly.allocate`` 的「就近取整（半手向上）」不同：那里是
**资金分配**的近似，多买半手只是权重略偏；这里是**委托数量**，多买一手会直接
击穿单笔金额上限与现金，因此必须向下取整）。

时间与 id
---------
所有时间戳都是毫秒整数（与 ``core/storage.now_ms`` 一致）；委托单 id 形态
``ord-<13位毫秒>-<4位十六进制>``（与 ``core/advisor.record_id`` 同构，可读可排序、
并发不冲突）。

运行方式::
    python3 tests/test_trader.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import datetime
import functools
import inspect
import re
import threading
import time
import uuid

from . import advisor as A
from . import kelly as K
from . import rules as RULES
from .storage import now_ms

__all__ = [
    "MODES", "ORDER_STATUS", "CONFIRM_HEADER", "DEFAULT_CONFIG",
    "default_config", "normalize_config", "get_config", "save_config",
    "account_id", "ensure_account", "account_view", "plan_orders",
    "execute_orders", "cancel_order", "close_position", "reset_account",
    "scan", "ack_order", "export_intents", "trade_snapshot",
    "order_id", "buy_cost", "sell_net", "lot_of", "TRADE_META_PREFIX",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 交易模式：dryrun = 只生成计划不成交（默认）｜paper = 模拟成交
MODES = ("dryrun", "paper")
#: 委托单状态机（pending → submitted → acked → filled 是外发链路；dryrun 只到 pending）
ORDER_STATUS = ("pending", "filled", "rejected", "cancelled", "submitted", "acked", "expired")
#: 执行确认口令走的请求头（服务端在真正下单前必须校验它，见 `normalize_config`）
CONFIRM_HEADER = "X-Trade-Confirm"

#: 默认交易模式（**默认 dryrun**：装好引擎也不会自己下单）
DEFAULT_MODE = "dryrun"
#: 自动扫描间隔的夹取区间（秒）
INTERVAL_RANGE = (15, 3600)
#: 初始本金 / 单笔金额上限的夹取区间（上限与 core/runner.py 的 initial 口径一致）
CAPITAL_RANGE = (1.0, 1e10)
AMOUNT_RANGE = (0.0, 1e10)
#: 最大持仓只数 / 每日委托上限的夹取区间
MAX_POSITIONS_RANGE = (1, 1000)
MAX_ORDERS_RANGE = (1, 1000)
#: 标的池 / 白名单的最大长度（防止一次扫描打爆数据源）
CODES_LIMIT = 200
#: 账户级累计量（realizedPnl / feeTotal）在 meta 表里的键前缀
TRADE_META_PREFIX = "trade:meta:"
#: 确认口令形态：6~10 位十六进制（服务端生成、前端回填，校验走 CONFIRM_HEADER）
TOKEN_RE = re.compile(r"^[0-9a-f]{6,10}$")
#: 成本参数默认值（与 core/advisor.py 的 DEFAULT_FEE / DEFAULT_SLIPPAGE、storage 默认列值同源）
DEFAULT_FEE = A.DEFAULT_FEE
DEFAULT_SLIPPAGE = A.DEFAULT_SLIPPAGE
#: 自动扫描调用 recommend 时使用的预测窗口与分数凯利系数（配置里没有这两个字段，
#: 因此直接沿用 advisor / kelly 的默认值，保证与「AI 选股」页同口径）
SCAN_HORIZON = A.DEFAULT_HORIZON
SCAN_KELLY_FRACTION = K.DEFAULT_FRACTION
#: 建议档位的七个取值（与 core/advisor.ACTION_LABEL 同源）
ACTION_KEYS = tuple(A.ACTION_LABEL)
#: 数值比较用的容差（浮点金额比较一律走它，避免把 1e-13 的噪声当失败）
EPS = 1e-9
#: 展示用小数位（金额 / 价格 / 权重）
ND_PRICE = 6
ND_MONEY = 6
ND_RATIO = 6

#: 交易配置默认值。所有字段都能被 :func:`normalize_config` 覆盖并夹取；
#: ``DEFAULT_CONFIG`` 是模块级常量（不会随调用改变），``default_config()`` 是它的
#: 一份「已归一化 + 已生成口令」的副本。
DEFAULT_CONFIG = {
    "enabled": False,           # 自动交易总开关，默认必须为 False
    "mode": DEFAULT_MODE,       # dryrun = 只生成计划不成交（默认）｜paper = 模拟成交
    "market": "cn",
    "capital": 100000.0,        # 模拟账户初始本金（同时也是仓位计算的基准本金）
    "maxWeight": 0.25,          # 单只目标权重上限
    "maxPositions": 10,         # 最大持仓只数
    "maxOrdersPerDay": 20,      # 每日委托上限
    "maxOrderAmount": 50000.0,  # 单笔金额上限（超出则缩减，不是跳过）
    "minConfidence": 0.25,      # 触发下单的最低置信度
    "allowReduce": True,        # 是否允许自动减仓 / 卖出
    #: 定时调度（把「自动交易」的『自动』补上）。两个开关默认都为 False：
    #: 关掉调度时，一切动作都只能由用户点击触发，这是最安全的默认。
    "scheduler": False,         # 是否启用定时自动扫描（受 enabled 与交易时段约束）
    "autoExecute": False,       # 自动扫描后是否**自动成交**（仅 paper 模式有效；dryrun 永不成交）
    "ignoreMarketHours": False,  # 忽略交易时段（演示 / 回放用，非交易日会得到与上一交易日相同的结果）
    "universe": [],             # 自动扫描的标的池（代码数组，去重大写）
    "whitelist": [],            # 交易白名单（非空时只允许这些代码）
    "interval": 60,             # 自动扫描间隔（秒，夹取到 15..3600）
    "webhook": "",              # 下单指令外发地址（空表示不外发）
    "confirmToken": "",         # 执行确认口令（服务端生成，6-10 位十六进制）
    "updatedAt": None,
}

#: 风控闸门中「不算动作也无需解释」的档位（与 core/advisor.REVIEW_NEUTRAL 同源）
NEUTRAL_ACTIONS = tuple(A.REVIEW_NEUTRAL)

CONFIG_NOTE = (
    "配置口径：enabled 默认 False（装好引擎也不会自己下单）；mode 默认 dryrun"
    "（只生成计划、不成交）；capital 既是模拟账户本金，也是仓位计算的基准；"
    "maxWeight / maxOrderAmount / 可用现金三者取最小后**向下取整到整手**；"
    "scheduler（定时自动扫描）与 autoExecute（自动成交）默认同样为 False —— "
    "三层开关都打开、且处于交易时段内，才会出现「无人值守也自动成交」，"
    "并且成交只发生在本地模拟账户里；非法输入不抛异常，一律回退默认值并夹取到合理区间。"
)
ORDER_NOTE = (
    "委托单口径：qty 已向下取整到整手（core.kelly.LOTS）；limitPrice 是计划时的当前价、"
    "signalPrice 是研判快照价；fillPrice 是含滑点的模拟成交价，slippage 是滑点成本金额；"
    "reason 写明依据与所用参数；payload 保留当时的计划 / 预测 / 风控 / 账户快照供审计。"
)
ACCOUNT_NOTE = (
    "账户口径：dryrun 与 paper 各自一本账（account_id = '<mode>:<market>'）；"
    "持仓为多标的列表；缺少最新报价时 lastPrice 回退 avgPrice 并标 priced=false"
    "（不臆造价格）；weight = 持仓市值 / 初始本金（与目标权重同分母，便于直接与 "
    "maxWeight 比较）；pnl / returnPct 为百分数。"
)


# --------------------------------------------------------------------------- #
# 基础工具（宽松解析：脏输入一律降级，绝不抛异常）
# --------------------------------------------------------------------------- #
def _num(x):
    """宽松转 float：None / 布尔 / 空串 / 无法解析 / NaN / ±inf 一律返回 None。"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    else:
        s = str(x).strip()
        if not s:
            return None
        try:
            v = float(s)
        except (TypeError, ValueError):
            return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _r(x, nd=ND_MONEY):
    """四舍五入到 nd 位（仅用于展示与 JSON 干净；金额计算链不取整）。"""
    v = _num(x)
    return 0.0 if v is None else round(v, nd)


def _clean(x):
    """消除 -0.0，避免污染快照对比与 JSON 输出。"""
    v = _num(x)
    if v is None:
        return 0.0
    return 0.0 if v == 0 else v


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


_TRUE_WORDS = ("1", "true", "yes", "y", "on", "t", "是", "开")
_FALSE_WORDS = ("0", "false", "no", "n", "off", "f", "", "否", "关")


def _bool(v, default=False):
    """严格转 bool：真值词表内才认为 True，其余回退 default（不抛异常）。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return bool(default)
    if isinstance(v, (int, float)):
        n = _num(v)
        return bool(n) if n is not None else bool(default)
    s = str(v).strip().lower()
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    return bool(default)


def _text(v, default=""):
    """转字符串并去空白；None 取 default。"""
    if v is None:
        return default
    if isinstance(v, (dict, list, tuple, set)):
        return default
    return str(v).strip()


def _int_in(v, default, lo, hi):
    """整数字段：非数值回退 default，数值四舍五入后夹取到 [lo, hi]。"""
    n = _num(v)
    if n is None:
        n = float(default)
    return int(_clamp(round(n), lo, hi))


def _int_of(v, default=0):
    """宽松取整（脏输入回退 default），用于「从外部对象里读股数」这类场景。"""
    n = _num(v)
    return int(n) if n is not None else int(default)


def _rate(v, default):
    """比例字段：非数值回退 default，数值夹取到 [0, 1]。"""
    n = _num(v)
    if n is None:
        n = float(default)
    return float(_clamp(n, 0.0, 1.0))


def _capital(v, default=None):
    """本金：非数值或 ≤ 0 视为非法（回退默认），数值夹取到 CAPITAL_RANGE。"""
    dflt = DEFAULT_CONFIG["capital"] if default is None else float(default)
    n = _num(v)
    if n is None or n <= 0:
        n = dflt
    return float(_clamp(n, CAPITAL_RANGE[0], CAPITAL_RANGE[1]))


def _amount(v, default):
    """金额上限：非数值回退 default，负数按 0，上限 CAPITAL_RANGE 同量级。"""
    n = _num(v)
    if n is None:
        n = float(default)
    return float(_clamp(n, AMOUNT_RANGE[0], AMOUNT_RANGE[1]))


def _market(v):
    """市场归一：只有 cn / us 两种，其余一律 cn（与 advisor.recommend 同口径）。"""
    s = _text(v).lower()
    return "us" if s.startswith("us") else "cn"


def _mode(v, default=DEFAULT_MODE):
    """模式归一：只允许 dryrun / paper，非法回退 default。"""
    s = _text(v).lower()
    return s if s in MODES else (default if default in MODES else DEFAULT_MODE)


def _ms(v):
    """毫秒时间戳：非数值或 ≤ 0 返回 None（不臆造时间）。"""
    n = _num(v)
    if n is None or n <= 0:
        return None
    return int(n)


def _codes(v, default=None):
    """代码数组归一：去重、统一大写、保持输入顺序；字符串按逗号/空白切分。"""
    if v is None:
        return list(default or [])
    if isinstance(v, str):
        items = [s for s in re.split(r"[,，;；\s]+", v) if s]
    elif isinstance(v, (list, tuple, set)):
        items = list(v)
    else:
        return list(default or [])
    out = []
    for it in items:
        s = _text(it).upper()
        if not s or s in out:
            continue
        out.append(s)
        if len(out) >= CODES_LIMIT:
            break
    return out


def _token(v=None):
    """确认口令：合法则沿用，缺失 / 非法时重新生成（6~10 位十六进制）。"""
    s = _text(v).lower()
    if TOKEN_RE.match(s):
        return s
    return new_token()


def new_token():
    """生成一个 8 位十六进制确认口令（服务端生成、前端回填）。"""
    return uuid.uuid4().hex[:8]


def order_id(ts=None):
    """委托单 id：``ord-<13位毫秒>-<4位十六进制>``（与 advisor.record_id 同构）。"""
    return "ord-%d-%s" % (int(ts if ts is not None else now_ms()), uuid.uuid4().hex[:4])


def lot_of(market, lot=None):
    """最小交易单位：A 股 100 股/手、美股 1 股/手（``K.LOTS``）；显式 lot 优先。"""
    n = _num(lot)
    if n is not None and n >= 1:
        return int(n)
    return int(K.LOTS.get("us" if _market(market) == "us" else "cn", 100))


def _floor_lot(shares, lot):
    """向下取整到整手（委托数量绝不向上取整：多买一手会击穿金额上限）。"""
    if shares <= 0 or lot <= 0:
        return 0
    return int(shares // lot) * lot


def _half_lot(qty, lot):
    """减仓股数：持仓的一半向下取整到整手；不足 2 手则全部卖出。"""
    if qty <= 0:
        return 0
    if lot > 0 and qty < 2 * lot:
        return int(qty)
    half = _floor_lot(qty // 2, lot)
    return half if half > 0 else int(qty)


def buy_cost(qty, price, fee_rate, slippage):
    """买入总成本（含滑点与费用）：``qty × price × (1+slip) × (1+fee)``。

    与 ``core/advisor._round_trip`` 的买入口径逐字一致：买入价 = 中间价 × (1+滑点)，
    双边按费率计费。
    """
    q = _num(qty) or 0.0
    p = _num(price) or 0.0
    f = _num(fee_rate)
    s = _num(slippage)
    return q * p * (1.0 + (0.0 if s is None else s)) * (1.0 + (0.0 if f is None else f))


def sell_net(qty, price, fee_rate, slippage):
    """卖出净收入（扣滑点与费用）：``qty × price × (1−slip) × (1−fee)``。"""
    q = _num(qty) or 0.0
    p = _num(price) or 0.0
    f = _num(fee_rate)
    s = _num(slippage)
    return q * p * (1.0 - (0.0 if s is None else s)) * (1.0 - (0.0 if f is None else f))


def _trade_day(ts):
    """成交归属的交易日（北京时间 UTC+8，``YYYY-MM-DD``）。

    T+1 需要按「天」区分「今日买入」与「昨仓」，因此必须有一个明确的交易日口径。
    用固定偏移而不是本机时区：用户机器未必在北京时区，否则「今天买的」会算错一天。
    """
    stamp = _num(ts) or 0.0
    dt = datetime.datetime.fromtimestamp(stamp / 1000.0, datetime.timezone.utc) \
        + datetime.timedelta(hours=8)
    return dt.strftime("%Y-%m-%d")


def _sellable_of(pos, ts=None):
    """T+1 可卖数量 = 持仓 − 今日买入（跨日自动归零）。

    不能用总持仓当可卖量：那会让当日买入立刻可卖，等于给模拟盘开了 T+0 后门，
    纸面收益会凭空多出一截（这是 T+1 最常见的实现错误）。
    """
    if not isinstance(pos, dict):
        return 0
    total = _int_of(pos.get("qty"))
    bought = _int_of(pos.get("todayBought"))
    if bought <= 0:
        return max(0, total)
    if ts is not None and pos.get("todayBoughtOn") and pos.get("todayBoughtOn") != _trade_day(ts):
        return max(0, total)          # 跨日：昨仓全部可卖
    return max(0, total - bought)


def _fee_items(side, qty, price, fee_rate, code=None, market=None, board=None, name=None):
    """成交费用：**优先走交易规则引擎的逐项口径**，退化时用单一费率。

    为什么要换口径：把费用写成单一双边费率会系统性低估小资金策略的成本 ——
    1 万元买入按单一 0.03% 只有 3 元，按真实 A 股口径是「最低 5 元佣金 + 过户费 0.1 元
    + 经手费 0.341 元 ≈ 5.44 元」，高出 80%；卖出还多一道 0.05% 印花税（单边）。
    只有在能识别标的代码时才启用规则口径，否则保持旧行为（老调用不受影响）。
    """
    if code:
        try:
            res = RULES.fee_of(side, qty, price, market=market or "cn", board=board)
            items = res.get("items") or []
            if items:
                return items, float(res.get("total") or 0.0)
        except Exception:  # noqa: BLE001  规则引擎异常不应让成交失败
            pass
    rate = _num(fee_rate) or 0.0
    return [], qty * price * rate


def _fill_buy(qty, price, fee_rate, slippage, code=None, market=None, board=None, name=None):
    """买入成交明细（成交价含滑点、费用单列，便于与 trades 表的 fee/slippage 对账）。"""
    fill = price * (1.0 + slippage)
    notional = qty * fill
    fee_items, fee = _fee_items("buy", qty, fill, fee_rate, code, market, board, name)
    return {"qty": qty, "price": fill, "notional": notional, "fee": fee,
            "feeItems": fee_items, "amount": notional,
            "slippage": qty * price * slippage, "cash": notional + fee}


def _fill_sell(qty, price, fee_rate, slippage, code=None, market=None, board=None, name=None):
    """卖出成交明细（卖出价 = 中间价 × (1−滑点)）。"""
    fill = price * (1.0 - slippage)
    notional = qty * fill
    fee_items, fee = _fee_items("sell", qty, fill, fee_rate, code, market, board, name)
    return {"qty": qty, "price": fill, "notional": notional, "fee": fee,
            "feeItems": fee_items, "amount": notional,
            "slippage": qty * price * slippage, "cash": notional - fee}


# --------------------------------------------------------------------------- #
# 一、配置
# --------------------------------------------------------------------------- #
def normalize_config(patch, base=None):
    """合并 + 校验 + 夹取交易配置，返回**完整字段集**（绝不缺键，绝不抛异常）。

    参数
    ----
    patch : dict | None
        待写入的增量配置（服务端 POST 上来的表单）。非 dict 或 None 视为空 patch。
    base : dict | None
        基准配置（通常是 ``store.get_trade_config()`` 的旧值）。``patch`` 里
        **非 None** 的值覆盖 ``base``，为 None / 缺失的字段回退 ``base`` →
        ``DEFAULT_CONFIG``。注意 ``False`` / ``0`` / ``""`` / ``[]`` 都是合法覆盖值
        （否则「关掉开关」「清空白名单」都做不到）。

    返回
    ----
    dict
        完整配置；``confirmToken`` 缺失或非法时生成新的 6~10 位十六进制口令；
        ``updatedAt`` 原样透传（由 :func:`save_config` 盖时间戳）。

    夹取规则
    --------
    · 布尔字段（enabled / allowReduce）：严格转 bool，非法回退默认值；
    · 比例字段（maxWeight / minConfidence）：夹到 [0, 1]；
    · 整数（maxPositions / maxOrdersPerDay / interval）：夹到合理区间
      （interval 夹到 15..3600）；
    · capital：≤ 0 或非数值视为非法 → 回退默认 100000，否则夹到 [1, 1e10]；
    · maxOrderAmount：负数按 0（= 不允许下单），上限 1e10；
    · mode：只允许 dryrun / paper；market：只允许 cn / us；
    · universe / whitelist：统一大写去重（保持输入顺序），字符串按逗号/空白切分；
    · webhook：只接受 http(s) 开头的地址，其余（含任意其它协议）一律清空；
    · 未在 DEFAULT_CONFIG 里的键一律丢弃（配置结构稳定，前端与桥接系统可放心按字段读）。
    """
    p = patch if isinstance(patch, dict) else {}
    b = base if isinstance(base, dict) else {}

    def pick(key):
        if key in p and p[key] is not None:
            return p[key]
        return b.get(key)

    cfg = {
        "enabled": _bool(pick("enabled"), DEFAULT_CONFIG["enabled"]),
        "mode": _mode(pick("mode"), DEFAULT_CONFIG["mode"]),
        "market": _market(pick("market") or DEFAULT_CONFIG["market"]),
        "capital": _capital(pick("capital"), DEFAULT_CONFIG["capital"]),
        "maxWeight": _rate(pick("maxWeight"), DEFAULT_CONFIG["maxWeight"]),
        "maxPositions": _int_in(pick("maxPositions"), DEFAULT_CONFIG["maxPositions"],
                                *MAX_POSITIONS_RANGE),
        "maxOrdersPerDay": _int_in(pick("maxOrdersPerDay"), DEFAULT_CONFIG["maxOrdersPerDay"],
                                   *MAX_ORDERS_RANGE),
        "maxOrderAmount": _amount(pick("maxOrderAmount"), DEFAULT_CONFIG["maxOrderAmount"]),
        "minConfidence": _rate(pick("minConfidence"), DEFAULT_CONFIG["minConfidence"]),
        "allowReduce": _bool(pick("allowReduce"), DEFAULT_CONFIG["allowReduce"]),
        "scheduler": _bool(pick("scheduler"), DEFAULT_CONFIG["scheduler"]),
        "autoExecute": _bool(pick("autoExecute"), DEFAULT_CONFIG["autoExecute"]),
        "ignoreMarketHours": _bool(pick("ignoreMarketHours"),
                                   DEFAULT_CONFIG["ignoreMarketHours"]),
        "universe": _codes(pick("universe"), DEFAULT_CONFIG["universe"]),
        "whitelist": _codes(pick("whitelist"), DEFAULT_CONFIG["whitelist"]),
        "interval": _int_in(pick("interval"), DEFAULT_CONFIG["interval"], *INTERVAL_RANGE),
        "webhook": _webhook(pick("webhook")),
        "confirmToken": _token(pick("confirmToken")),
        "updatedAt": _ms(pick("updatedAt")),
    }
    return cfg


def _webhook(v):
    """外发地址：只留 http(s)（其它协议 / 空值一律清空，避免误发到本地文件等）。"""
    s = _text(v)
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return ""


def default_config():
    """默认配置（``DEFAULT_CONFIG`` 的归一化副本：含生成的 confirmToken）。

    注意与模块常量 ``DEFAULT_CONFIG`` 的差别：后者 ``confirmToken == ""``，
    本函数返回的副本**已经带上口令**（服务端第一次读配置就能拿到可用的口令）。
    """
    return normalize_config({}, DEFAULT_CONFIG)


def get_config(store):
    """读取交易配置：``store.get_trade_config()`` + normalize（默认值的唯一来源是本模块）。"""
    raw = None
    getter = getattr(store, "get_trade_config", None)
    if callable(getter):
        try:
            raw = getter()
        except Exception:  # noqa: BLE001  读配置失败不应让整个页面 500
            raw = None
    return normalize_config(raw if isinstance(raw, dict) else {}, DEFAULT_CONFIG)


def save_config(store, patch):
    """合并校验后写库（整体覆盖），返回**新配置**（含 ``updatedAt`` 时间戳）。

    写入前先按 ``get_config`` 取旧值做 base，因此支持「只提交一个字段」的
    PATCH 语义；非法输入不会污染已存配置（回退默认值而不是写入脏值）。
    """
    base = get_config(store)
    cfg = normalize_config(patch, base)
    cfg["updatedAt"] = now_ms()
    saver = getattr(store, "save_trade_config", None)
    if callable(saver):
        saver(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# 二、账户
# --------------------------------------------------------------------------- #
def account_id(market, mode=None):
    """账户 id：``"<mode>:<market>"``，例如 ``account_id("cn") == "paper:cn"``。

    mode 缺省按 ``"paper"``（模拟成交账户），非法 mode 回退 ``"paper"``；
    dryrun 与 paper 各自一本账，演练计划永不污染模拟成交账户。
    """
    mkt = _market(market)
    md = "paper" if mode is None else _mode(mode, "paper")
    return "%s:%s" % (md, mkt)


def _meta_key(aid):
    return TRADE_META_PREFIX + str(aid or "")


def _load_extra(store, aid):
    """账户级累计量（realizedPnl / feeTotal）：存 meta，读不到按 0（不抛异常）。"""
    getter = getattr(store, "meta_get", None)
    data = None
    if callable(getter):
        try:
            data = getter(_meta_key(aid), {})
        except Exception:  # noqa: BLE001
            data = None
    if not isinstance(data, dict):
        data = {}
    return {"realizedPnl": _clean(_num(data.get("realizedPnl")) or 0.0),
            "feeTotal": _clean(_num(data.get("feeTotal")) or 0.0),
            "updatedAt": _ms(data.get("updatedAt"))}


def _save_extra(store, aid, realized, fee_total, ts=None):
    """写账户级累计量；``meta_set`` 不可用时跳过（返回值里标 metaSaved=False）。"""
    setter = getattr(store, "meta_set", None)
    if not callable(setter):
        return False
    payload = {"realizedPnl": _clean(realized), "feeTotal": _clean(fee_total),
               "updatedAt": int(ts if ts is not None else now_ms())}
    try:
        setter(_meta_key(aid), payload)
        return True
    except Exception:  # noqa: BLE001
        return False


def _empty_state(cfg, market, aid=None):
    """按配置造一份空账户状态（dryrun / paper 各自一本账），不落库。"""
    mkt = _market(market or cfg.get("market"))
    cap = _capital(cfg.get("capital"), DEFAULT_CONFIG["capital"])
    return {
        "accountId": aid or account_id(mkt, cfg.get("mode")),
        "market": mkt,
        "mode": _mode(cfg.get("mode")),
        "cash": cap,
        "initial": cap,
        "lot": lot_of(mkt),
        "feeRate": DEFAULT_FEE,
        "slippage": DEFAULT_SLIPPAGE,
        "positions": [],
        "updatedAt": None,
    }


def _positions(account):
    """持仓列表归一：丢弃非法项 / 无代码项，数值字段宽松解析，返回**新列表**。"""
    raw = account.get("positions") if isinstance(account, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = _text(item.get("code")).upper()
        if not code:
            continue
        qty = _num(item.get("qty"))
        qty = int(qty) if (qty is not None and qty > 0) else 0
        avg = _clean(_num(item.get("avgPrice")) or 0.0)
        cost = _num(item.get("cost"))
        cost = _clean(cost) if (cost is not None and cost > 0) else _clean(avg * qty)
        out.append({
            "code": code, "name": _text(item.get("name")) or code,
            "market": _market(item.get("market") or (account or {}).get("market")),
            "qty": qty, "avgPrice": avg, "cost": cost,
            "openedAt": _ms(item.get("openedAt")),
            "lastPrice": _clean(_num(item.get("lastPrice")) or avg),
            "updatedAt": _ms(item.get("updatedAt")),
            # T+1 判定**只依赖这两个字段**，而归一化会把未列出的字段全丢掉 ——
            # 漏掉它们等于每次读回账户都把「今日买入」清零，同一笔买单在第二次调用里
            # 就能立刻卖出（等于给模拟盘开了 T+0 后门）。实测正是这个原因导致
            # 「同一天、分两次 execute_orders」时 T+1 校验形同虚设。
            "todayBought": _int_of(item.get("todayBought")),
            "todayBoughtOn": _text(item.get("todayBoughtOn")),
        })
    return out


def _pos_index(positions, code):
    c = _text(code).upper()
    for i, p in enumerate(positions):
        if p.get("code") == c:
            return i
    return -1


def _pos_of(positions, code):
    i = _pos_index(positions, code)
    return positions[i] if i >= 0 else None


def ensure_account(store, config, market):
    """取账户状态：不存在则按 config 建（本金 = capital），存在则**原样返回**。

    已存在的账户不会因为配置改了 capital / mode 而被重置（账户是资产快照，
    配置是运行参数，两者生命周期不同）；要重置请显式调用 :func:`reset_account`。
    返回的是**状态**（``positions`` 为持仓列表），展示用请走 :func:`account_view`。
    """
    cfg = normalize_config(config, DEFAULT_CONFIG)
    mkt = _market(market or cfg.get("market"))
    aid = account_id(mkt, cfg["mode"])

    state = None
    getter = getattr(store, "get_trade_state", None)
    if callable(getter):
        state = getter(aid)
    if isinstance(state, dict) and state:
        return state

    fresh = _empty_state(cfg, mkt, aid)
    saver = getattr(store, "save_trade_state", None)
    if callable(saver):
        saved = saver(fresh)
        if isinstance(saved, dict) and saved:
            return saved
    return fresh


def _held_positions(store, cfg, market):
    """只读持仓（账户不存在时返回空列表，**不建账**）。"""
    getter = getattr(store, "get_trade_state", None)
    state = getter(account_id(market, cfg.get("mode"))) if callable(getter) else None
    return _positions(state if isinstance(state, dict) else None)


def _view(state, cfg, quotes=None, exists=True, extra=None):
    """账户视图（展示用，不落库）：把 state + 报价 + 累计量整理成前端契约。"""
    mkt = _market((state or {}).get("market") or cfg.get("market"))
    cash = _clean(_num((state or {}).get("cash")) or 0.0)
    initial = _num((state or {}).get("initial"))
    initial = _capital(initial if initial else cfg.get("capital"), DEFAULT_CONFIG["capital"])
    lot = lot_of(mkt, (state or {}).get("lot"))
    fee_rate = _num((state or {}).get("feeRate"))
    fee_rate = DEFAULT_FEE if fee_rate is None else fee_rate
    slip = _num((state or {}).get("slippage"))
    slip = DEFAULT_SLIPPAGE if slip is None else slip

    qmap = _quote_map(quotes)
    positions = _positions(state)
    filled_rows = []
    market_value = 0.0
    priced_all = True
    for p in positions:
        price = qmap.get(p["code"])
        priced = price is not None and price > 0
        if not priced:
            priced_all = False
        last = price if priced else p["avgPrice"]
        mv = p["qty"] * last
        cost = p["cost"] if p["cost"] > 0 else p["avgPrice"] * p["qty"]
        pnl = mv - cost
        market_value += mv
        rows = dict(p)
        rows.update({
            "lastPrice": _r(last, ND_PRICE),
            "marketValue": _r(mv, ND_MONEY),
            "pnl": _r(pnl, ND_MONEY),
            "pnlPct": _r((pnl / cost * 100.0) if cost > 0 else 0.0, 4),
            "weight": _r((mv / initial) if initial > 0 else 0.0, ND_RATIO),
            "priced": bool(priced),
        })
        rows["avgPrice"] = _r(rows["avgPrice"], ND_PRICE)
        rows["cost"] = _r(rows["cost"], ND_MONEY)
        filled_rows.append(rows)

    equity = cash + market_value
    pnl = equity - initial
    acc_extra = extra if isinstance(extra, dict) else {}
    extra = {"realizedPnl": _clean(_num(acc_extra.get("realizedPnl")) or 0.0),
             "feeTotal": _clean(_num(acc_extra.get("feeTotal")) or 0.0)}
    view = {
        "accountId": _text((state or {}).get("accountId")) or account_id(mkt, cfg.get("mode")),
        "market": mkt,
        "mode": _mode((state or {}).get("mode") or cfg.get("mode")),
        "cash": _r(cash, ND_MONEY),
        "initial": _r(initial, ND_MONEY),
        "equity": _r(equity, ND_MONEY),
        "marketValue": _r(market_value, ND_MONEY),
        "pnl": _r(pnl, ND_MONEY),
        "returnPct": _r((pnl / initial * 100.0) if initial > 0 else 0.0, 4),
        "realizedPnl": _r(extra["realizedPnl"], ND_MONEY),
        "feeTotal": _r(extra["feeTotal"], ND_MONEY),
        "positionCount": len(filled_rows),
        "positions": filled_rows,
        "weights": {r["code"]: r["weight"] for r in filled_rows},
        "priced": bool(priced_all),
        "lot": lot,
        "feeRate": float(fee_rate),
        "slippage": float(slip),
        "exists": bool(exists),
        "updatedAt": _ms((state or {}).get("updatedAt")),
        "note": ACCOUNT_NOTE,
    }
    return view


def account_view(store, config, quotes=None):
    """账户概览（展示契约，服务端直接透传给前端）。

    字段：``accountId`` / ``market`` / ``mode`` / ``cash`` / ``initial`` / ``equity`` /
    ``marketValue`` / ``pnl`` / ``returnPct`` / ``realizedPnl`` / ``feeTotal`` /
    ``positionCount`` / ``positions`` / ``weights``，外加：
    · 每条持仓带 ``lastPrice`` / ``marketValue`` / ``pnl`` / ``pnlPct`` / ``weight`` /
      ``priced``；**缺报价时 ``lastPrice`` 回退 ``avgPrice`` 且 ``priced=false``**
      （绝不臆造价格，账户总览里的权益因此是「按成本估值」的下界）；
    · 顶层 ``priced`` 表示「是否所有持仓都有报价」；``exists`` 表示账户是否已落库
      （账户不存在时返回一份按 config 推出来的空账户视图，**不落库**）。
    """
    cfg = normalize_config(config, DEFAULT_CONFIG)
    mkt = _market(cfg.get("market"))
    aid = account_id(mkt, cfg["mode"])
    state = None
    getter = getattr(store, "get_trade_state", None)
    if callable(getter):
        state = getter(aid)
    exists = isinstance(state, dict) and bool(state)
    if not exists:
        state = _empty_state(cfg, mkt, aid)
    return _view(state, cfg, quotes, exists=exists, extra=_load_extra(store, aid))


def reset_account(store, config, market):
    """重置模拟账户：现金回到 capital、清空持仓、清零 realizedPnl / feeTotal。

    **不删除历史委托单与权益点**（审计与事后复盘需要），只在返回值里说明 ——
    删除审计数据是不可逆的，而「重置」在用户心里只是「把模拟盘擦干净」。
    """
    cfg = normalize_config(config, DEFAULT_CONFIG)
    mkt = _market(market or cfg.get("market"))
    aid = account_id(mkt, cfg["mode"])
    state = _empty_state(cfg, mkt, aid)
    state["updatedAt"] = now_ms()
    saver = getattr(store, "save_trade_state", None)
    if callable(saver):
        saved = saver(state)
        if isinstance(saved, dict) and saved:
            state = saved
    _save_extra(store, aid, 0.0, 0.0)
    view = _view(state, cfg, None, exists=True,
                 extra={"realizedPnl": 0.0, "feeTotal": 0.0})
    view["reset"] = True
    view["note"] = (
        "账户已重置：现金回到本金 %.2f，持仓清空，realizedPnl / feeTotal 归零；"
        "历史委托单与权益点**保留**（审计需要，如需清理请自行按时间删除）。" % cfg["capital"]
    )
    return view


# --------------------------------------------------------------------------- #
# 三、报价与风控闸门
# --------------------------------------------------------------------------- #
def _price_of(v):
    """从一个报价对象里取价格：支持裸数值 / 字符串 / ``{price|last|close|lastPrice}``。"""
    if isinstance(v, dict):
        for key in ("price", "last", "lastPrice", "close"):
            px = _num(v.get(key))
            if px is not None and px > 0:
                return px
        return None
    px = _num(v)
    return px if (px is not None and px > 0) else None


def _quote_map(quotes):
    """报价归一：``{code: 价格}``。

    兼容三种真实入参：``{"600519": 1700.0}``、``{"cn:600519": {"price": 1700}}``、
    ``[{"code": "600519", "price": 1700}]``；取不到的代码不写入（调用方按「无有效
    价格」处理，绝不臆造）。
    """
    out = {}
    if isinstance(quotes, dict):
        for k, v in quotes.items():
            key = _text(k).upper()
            px = _price_of(v)
            if px is None:
                continue
            if key:
                out[key] = px
                tail = key.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
                if tail and tail != key:
                    out.setdefault(tail, px)
    elif isinstance(quotes, (list, tuple)):
        for item in quotes:
            if not isinstance(item, dict):
                continue
            code = _text(item.get("code") or item.get("symbol")).upper()
            px = _price_of(item)
            if code and px is not None:
                out[code] = px
    return out


def _orders_today(store, market):
    """当日委托数（风控闸门 8 用）；store 缺失或统计失败按 0 处理，不阻断计划生成。"""
    fn = getattr(store, "count_trade_orders_today", None)
    if not callable(fn):
        return 0
    try:
        return max(0, int(fn(market=market) or 0))
    except Exception:  # noqa: BLE001
        return 0


def _gates(cfg, positions, orders_today, store=None):
    """风控闸门快照（进订单 payload 与 trade_snapshot，供审计与前端展示）。

    不含 ``confirmToken``（口令是密钥，绝不进审计数据与外部导出）。
    """
    held = len(positions or [])
    max_orders = cfg["maxOrdersPerDay"]
    return {
        "enabled": bool(cfg["enabled"]),
        "mode": cfg["mode"],
        "market": cfg["market"],
        "capital": _r(cfg["capital"], 2),
        "maxWeight": cfg["maxWeight"],
        "maxPositions": cfg["maxPositions"],
        "maxOrdersPerDay": max_orders,
        "maxOrderAmount": _r(cfg["maxOrderAmount"], 2),
        "minConfidence": cfg["minConfidence"],
        "allowReduce": bool(cfg["allowReduce"]),
        "interval": cfg["interval"],
        "webhook": bool(cfg["webhook"]),
        "feeRate": DEFAULT_FEE,
        "slippage": DEFAULT_SLIPPAGE,
        "universe": list(cfg["universe"]),
        "whitelist": list(cfg["whitelist"]),
        "neutralActions": list(NEUTRAL_ACTIONS),
        "ordersToday": int(orders_today or 0),
        "remainingToday": max(0, max_orders - int(orders_today or 0)),
        "positionsHeld": held,
        "positionCodes": [p["code"] for p in (positions or [])],
        "confirmRequired": True,
        "confirmHeader": CONFIRM_HEADER,
        "dryrun": cfg["mode"] != "paper",
        "store": bool(store is not None),
        "note": ("风控闸门快照：总开关 / 白名单 / 数据完整性 / 价格 / 置信度 / 每日委托上限 / "
                 "持仓只数 / 减仓开关 / 持仓存在性；成交口径为报价 ×(1±滑点) 再按费率计费。"
                 "口令（confirmToken）与 webhook 地址都不在此快照内（webhook 只记是否配置，"
                 "因为它常带签名参数）。"),
    }


def _kelly_weight(row, cfg):
    """取该行的凯利权重：``kelly.weight``（组合分配后的实际权重）优先，回退 rawWeight。

    再按 ``maxWeight`` 夹取（单只上限是硬约束，不依赖上游是否已经归一化）。
    """
    k = row.get("kelly") if isinstance(row, dict) else None
    w = 0.0
    if isinstance(k, dict):
        for key in ("weight", "rawWeight", "targetWeight"):
            if key in k:
                n = _num(k.get(key))
                if n is not None:
                    w = max(0.0, n)
                    break
    return min(w, cfg["maxWeight"])


def _confidence(row):
    c = _num(row.get("confidence")) if isinstance(row, dict) else None
    return c


def _cost_note():
    return ("成交口径：买入价 = 报价 ×(1+滑点 %.4f)、卖出价 = 报价 ×(1−滑点 %.4f)，"
            "双边按费率 %.4f 计费（与 core/advisor._round_trip 一致）"
            % (DEFAULT_SLIPPAGE, DEFAULT_SLIPPAGE, DEFAULT_FEE))


# --------------------------------------------------------------------------- #
# 四、计划（风控闸门 + 定量）
# --------------------------------------------------------------------------- #
def _advice_rows(advice):
    """从 advice 里取标的行：兼容 ``{"rows": [...]}`` 与直接传 list。"""
    if isinstance(advice, dict):
        rows = advice.get("rows")
        return list(rows) if isinstance(rows, (list, tuple)) else []
    if isinstance(advice, (list, tuple)):
        return list(advice)
    return []


def _skip_entry(code, name, action, gate, reason, market):
    return {"code": code, "name": name or code, "market": market, "action": action,
            "gate": gate, "reason": reason, "note": ORDER_NOTE}


def _by_gate(skips):
    out = {}
    for s in skips:
        out[s["gate"]] = out.get(s["gate"], 0) + 1
    return out


def _target_amount(row, kelly_w, capital):
    """定量基准金额：优先用研判给出的**可执行金额**，回退到「权重 × 本金」。

    为什么不只用「权重 × 本金」：``advisor`` 的 ``kelly.weight`` 是「实际金额 / 本金」
    再保留 6 位小数，而它自己的 ``kelly.amount`` 是**已按整手取整过**的可执行金额。
    用 6 位小数的权重乘回本金、再向下取整到整手，会**二次取整**并系统性少买 ——
    实测出现过「研判计划 200 股、自动交易只买 100 股」这种少买一半的情况，根因就在这里。
    因此有可执行金额时以它为准（仍会经过下游的单笔 / 单只 / 现金上限校验），
    权重只作为兜底。返回 ``(金额, 来源说明)``。
    """
    k = row.get("kelly") if isinstance(row, dict) else None
    if isinstance(k, dict):
        for key, label in (("amount", "研判可执行金额 kelly.amount"),
                           ("targetAmount", "研判计划金额 kelly.targetAmount")):
            n = _num(k.get(key))
            if n is not None and n > 0:
                return float(n), label
    return float(capital) * float(kelly_w), "凯利权重 × 本金"


def _build_order(code, name, market, side, intent, action, mode, source, reason,
                 confidence, score, kelly_weight, target_weight, qty, lot,
                 limit_price, signal_price, plan, forecast, gates, cash, positions,
                 review_id, ts, capital):
    """组装一条委托单（字段名与 ``store.save_trade_order`` 完全一致）。"""
    return {
        "id": order_id(ts),
        "createdAt": ts,
        "updatedAt": ts,
        "market": market,
        "code": code,
        "name": name or code,
        "side": side,
        "intent": intent,
        "action": action,
        "mode": mode,
        "status": "pending",
        "source": source,
        "reason": reason,
        "confidence": _r(confidence, 4),
        "score": _r(score, 4),
        "kellyWeight": _r(kelly_weight, ND_RATIO),
        "targetWeight": _r(target_weight, ND_RATIO),
        "qty": int(qty),
        "lot": int(lot),
        "limitPrice": _r(limit_price, ND_PRICE) if limit_price else None,
        "signalPrice": _r(signal_price, ND_PRICE) if signal_price else None,
        "fillPrice": None,
        "fee": 0.0,
        "slippage": 0.0,
        "amount": _r((qty * limit_price) if limit_price else 0.0, ND_MONEY),
        "filledAt": None,
        "reviewId": _text(review_id),
        "extRef": "",
        "error": "",
        "payload": {
            "plan": plan if isinstance(plan, dict) else {},
            "forecast": forecast if isinstance(forecast, dict) else {},
            "gates": gates,
            "account": {"cash": _r(cash, ND_MONEY), "positions": len(positions),
                        "codes": [p["code"] for p in positions]},
            "input": {"price": _r(limit_price, ND_PRICE) if limit_price else None,
                      "capital": _r(capital, 2)},
        },
    }


def plan_orders(advice, account, config, quotes, store=None):
    """把一次研判（``advice["rows"]``）落成计划委托单，**先过风控闸门再定量**。

    闸门顺序（任何一步不过都记 ``skipped`` 并写清原因，绝不静默丢弃）

    1. ``enabled`` 为 False → 全部标的 skipped（原因「自动交易未开启（总开关关闭）」），
       ``orders`` 必为空；
    2. 标的为空（``advice["rows"]`` 为空）→ 空计划，``note`` 说明；
    3. 白名单非空且代码不在其中 → skip「不在交易白名单内」；
    4. ``ok`` 为 False（或代码为空）→ skip「研判数据不足：<error>」；
    5. 价格无效（None / ≤0）→ skip「无有效价格」；
    6. 置信度缺失或低于 ``minConfidence`` → skip「置信度 X% 低于阈值 Y%」；
    7. 档位不是 buy/add/reduce/sell/avoid → 既不下单也不记 skip（hold/watch 无需动作）；
    8. 当日委托（含本批已计划的）≥ ``maxOrdersPerDay`` → skip「当日委托已达上限 N 笔」；
    9. 开仓时持仓只数（含本批已计划的 open）≥ ``maxPositions`` → skip「持仓只数已达上限 N 只」；
    10. 减仓 / 清仓且 ``allowReduce`` 为 False → skip「已关闭自动减仓」；
    11. 无持仓却要减仓 / 清仓 → skip「无对应持仓」。

    定量规则（每一条都写进 ``reason``，含所用参数）

    · **open**（buy 且无持仓，或 add 且无持仓）：目标金额 = ``capital × kelly.weight``，
      再取 ``min(目标金额, maxOrderAmount, capital × maxWeight, 可用现金)``，
      股数向下取整到整手；股数为 0 → skip「可用资金不足一手（需 X，可用 Y）」或
      「凯利权重为 0」；
    · **add**（有持仓）：目标金额 = ``capital × kelly.weight``，可加金额 =
      ``min(目标金额 − 当前持仓市值, maxOrderAmount, 可用现金)``；不足一手 →
      skip「已达目标仓位」；
    · **reduce**（有持仓）：卖出持仓的一半（向下取整到整手，不足 2 手则全部卖出）；
    · **close**（sell / avoid 且有持仓）：全部卖出。

    ``targetWeight`` 填该单成交后的持仓权重（分母是 ``capital``，与 ``maxWeight`` 同口径）；
    ``limitPrice`` = 当前价（有报价用报价，否则用研判价），``signalPrice`` = 研判里的价格。

    返回
    ----
    dict
        ``orders`` 计划单、``skipped`` 未下单明细（每条带 ``gate`` 便于归因）、
        ``skippedByGate`` 闸门计数、``neutral`` 中性档位只数、``gates`` 风控快照、
        ``symbols`` 本次研判的代码、``note`` 中文说明。``account`` 为 None 或非法时
        按「无持仓、现金 = config.capital」处理（本函数**不建账**，建账走 ensure_account）。
    """
    cfg = normalize_config(config, DEFAULT_CONFIG)
    market = _market(cfg["market"])
    mode = cfg["mode"]
    rows = _advice_rows(advice)
    qmap = _quote_map(quotes)

    account_ok = isinstance(account, dict)
    acc = account if account_ok else {}
    positions = _positions(acc)
    cash = _num(acc.get("cash")) if account_ok else None
    cash = cfg["capital"] if (cash is None or cash < 0) else cash

    used_today = _orders_today(store, market)
    gates = _gates(cfg, positions, used_today, store)

    symbols = []
    for r in rows:
        code = _text(r.get("code")).upper() if isinstance(r, dict) else ""
        if code and code not in symbols:
            symbols.append(code)

    result = {
        "ok": True, "market": market, "mode": mode, "enabled": bool(cfg["enabled"]),
        "capital": _r(cfg["capital"], 2), "symbols": symbols,
        "orders": [], "skipped": [], "neutral": 0, "count": 0, "skippedCount": 0,
        "skippedByGate": {}, "gates": gates, "note": "",
        "skippedCodes": [], "orderCodes": [],
    }
    skips = result["skipped"]

    def _add_skip(code, name, action, gate, reason):
        skips.append(_skip_entry(code, name, action, gate, reason, market))

    def _finish(note):
        result["skippedCount"] = len(skips)
        result["skippedByGate"] = _by_gate(skips)
        result["skippedCodes"] = [s["code"] for s in skips]
        result["count"] = len(result["orders"])
        result["orderCodes"] = [o["code"] for o in result["orders"]]
        result["note"] = note
        return result

    # ---------------- 闸门 1：总开关 ----------------
    if not cfg["enabled"]:
        for r in rows:
            row = r if isinstance(r, dict) else {}
            _add_skip(_text(row.get("code")).upper(), _text(row.get("name")),
                      _text(row.get("action")), "enabled", "自动交易未开启（总开关关闭）")
        return _finish(
            "自动交易总开关（enabled）为 False：本次不生成任何委托单，%d 只标的全部按 skipped "
            "记录（原因：自动交易未开启（总开关关闭））。开启方式：save_config(store, "
            "{\"enabled\": True})；建议先在 dryrun 模式观察计划。" % len(skips))

    # ---------------- 闸门 2：标的池为空 ----------------
    if not rows:
        return _finish(
            "标的池为空：plan_orders 的标的来源是 advice[\"rows\"]，本次没有任何可研判的标的，"
            "未生成任何委托单。scan() 会用 config.universe（为空时回退 whitelist）构造 symbols。")

    ts = now_ms()
    used = int(used_today)
    held = len(positions)
    max_orders = cfg["maxOrdersPerDay"]
    min_conf = cfg["minConfidence"]
    review_id = ""
    if isinstance(advice, dict):
        review_id = _text(advice.get("reviewId") or advice.get("rid") or advice.get("id"))

    for raw in rows:
        row = raw if isinstance(raw, dict) else {}
        code = _text(row.get("code")).upper()
        name = _text(row.get("name")) or code
        action = _text(row.get("action")).lower()
        signal_price = _num(row.get("price"))
        price = qmap.get(code)
        if price is None:
            price = signal_price
        conf = _confidence(row)

        # ---------------- 闸门 3：白名单 ----------------
        if cfg["whitelist"] and code not in cfg["whitelist"]:
            _add_skip(code, name, action, "whitelist", "不在交易白名单内")
            continue
        # ---------------- 闸门 4：研判数据不足（含代码为空） ----------------
        if not code:
            _add_skip(code, name, action, "data", "研判数据不足：标的代码为空")
            continue
        if not row.get("ok"):
            err = _text(row.get("error")) or "未给出原因"
            _add_skip(code, name, action, "data", "研判数据不足：%s" % err)
            continue
        # ---------------- 闸门 5：价格 ----------------
        if price is None or price <= 0:
            _add_skip(code, name, action, "price", "无有效价格：无法计算股数与金额，不生成委托单")
            continue
        # ---------------- 闸门 6：置信度 ----------------
        if conf is None:
            _add_skip(code, name, action, "confidence",
                      "置信度缺失（按 0%% 处理），低于阈值 %.2f%%" % (min_conf * 100.0))
            continue
        if conf < min_conf:
            _add_skip(code, name, action, "confidence",
                      "置信度 %.2f%% 低于阈值 %.2f%%" % (conf * 100.0, min_conf * 100.0))
            continue
        # ---------------- 闸门 7：档位是否可操作 ----------------
        direction = A.REVIEW_DIRECTION.get(action)
        if direction is None:
            # hold / watch（以及未知档位）是明确的「无需动作」：不产生订单，也不记 skip
            result["neutral"] += 1
            continue

        pos = _pos_of(positions, code)
        held_qty = pos["qty"] if pos else 0
        if direction > 0:
            intent = "add" if held_qty > 0 else "open"
            side = "buy"
        else:
            intent = "reduce" if action == "reduce" else "close"
            side = "sell"

        # ---------------- 闸门 8：每日委托上限 ----------------
        if used >= max_orders:
            _add_skip(code, name, action, "dailyLimit",
                      "当日委托已达上限 %d 笔（当日已用 %d 笔，含本批已计划）" % (max_orders, used))
            continue
        # ---------------- 闸门 9：最大持仓只数（仅开仓） ----------------
        if intent == "open" and held >= cfg["maxPositions"]:
            _add_skip(code, name, action, "maxPositions",
                      "持仓只数已达上限 %d 只（当前 %d 只），不开新仓" % (cfg["maxPositions"], held))
            continue
        # ---------------- 闸门 10：减仓开关 ----------------
        if intent in ("reduce", "close") and not cfg["allowReduce"]:
            _add_skip(code, name, action, "reduceDisabled", "已关闭自动减仓")
            continue
        # ---------------- 闸门 11：无对应持仓 ----------------
        if intent in ("reduce", "close") and held_qty <= 0:
            _add_skip(code, name, action, "noPosition", "无对应持仓：该标的当前不在持仓中")
            continue

        # ---------------- 定量 ----------------
        lot = lot_of(market, row.get("lot"))
        kelly_w = _kelly_weight(row, cfg)
        capital = cfg["capital"]
        # 单只上限是硬约束：**不给整手容差**。曾经为了「不让整手取整误伤计划」加过
        # 一手容差（capital×maxWeight + lot×price），结果对高价股几乎等于取消了上限
        # （实测某只标的实际权重冲到 27.86% > 25%）。高价股放不下整手时的正确行为是
        # 让 usable < 一手市值 → 落到「可用资金不足一手」的 skip，而不是放宽上限。
        max_weight_amt = capital * cfg["maxWeight"]
        target_w = 0.0
        qty = 0

        if intent == "open":
            target_amount, amt_src = _target_amount(row, kelly_w, capital)
            cap_host = []
            if target_amount > cfg["maxOrderAmount"]:
                cap_host.append("单笔金额上限 %.2f" % cfg["maxOrderAmount"])
            if target_amount > max_weight_amt:
                cap_host.append("单只上限 %.2f%%（%.2f）" % (cfg["maxWeight"] * 100.0, max_weight_amt))
            if target_amount > cash:
                cap_host.append("可用现金 %.2f" % cash)
            usable = min(target_amount, cfg["maxOrderAmount"], max_weight_amt, cash)
            if kelly_w <= 0:
                _add_skip(code, name, action, "sizing",
                          "凯利权重为 0（kelly.weight = 0），不建仓：%s" % _cost_note())
                continue
            qty = _floor_lot(usable / price, lot)
            if qty <= 0:
                _add_skip(code, name, action, "sizing",
                          "可用资金不足一手（需 %.2f，可用 %.2f）：一手 %d 股 × 现价 %.4f；%s"
                          % (price * lot, usable, lot, price, _cost_note()))
                continue
            reason = ("开仓：档位 %s、%s = 目标金额 %.2f（凯利权重 %.2f%% × 本金 %.2f 作对照）；"
                      "再取 min(目标金额, 单笔上限 %.2f, 单只上限 %.2f%%, 可用现金 %.2f) = %.2f%s；"
                      "股数 %d 股（%d 股/手，向下取整到底）；%s"
                      % (action or "buy", amt_src, target_amount, kelly_w * 100.0, capital,
                         cfg["maxOrderAmount"], cfg["maxWeight"] * 100.0, cash, usable,
                         ("，已按 %s 缩减" % "、".join(cap_host)) if cap_host else "（未触发任何上限）",
                         qty, lot, _cost_note()))
            target_w = (qty * price) / capital if capital > 0 else 0.0
            held += 1
        elif intent == "add":
            target_amount, amt_src = _target_amount(row, kelly_w, capital)
            current_value = pos["qty"] * price
            room = target_amount - current_value
            usable = min(room, cfg["maxOrderAmount"], cash)
            if room <= EPS:
                _add_skip(code, name, action, "sizing",
                          "已达目标仓位（目标金额 %.2f ≤ 当前持仓市值 %.2f），不再加仓：%s"
                          % (target_amount, current_value, _cost_note()))
                continue
            if usable <= EPS:
                _add_skip(code, name, action, "sizing",
                          "可用资金不足一手（需 %.2f，可用 %.2f）：加仓空间 %.2f 被单笔上限 %.2f "
                          "与可用现金 %.2f 压缩；%s"
                          % (price * lot, usable, room, cfg["maxOrderAmount"], cash, _cost_note()))
                continue
            qty = _floor_lot(usable / price, lot)
            if qty <= 0:
                _add_skip(code, name, action, "sizing",
                          "可用资金不足一手（需 %.2f，可用 %.2f）；%s"
                          % (price * lot, usable, _cost_note()))
                continue
            reason = ("加仓：档位 %s、凯利权重 %.2f%% × 本金 %.2f = 目标金额 %.2f，"
                      "当前持仓市值 %.2f，可加 %.2f（再取 min 单笔上限 %.2f / 可用现金 %.2f → %.2f）；"
                      "股数 %d 股（%d 股/手，向下取整）；成交后目标权重 %.2f%%；%s"
                      % (action or "add", kelly_w * 100.0, capital, target_amount, current_value,
                         room, cfg["maxOrderAmount"], cash, usable, qty, lot,
                         (pos["qty"] + qty) * price / capital * 100.0 if capital > 0 else 0.0,
                         _cost_note()))
            target_w = ((pos["qty"] + qty) * price / capital) if capital > 0 else 0.0
        elif intent == "reduce":
            qty = _half_lot(pos["qty"], lot)
            if pos["qty"] < 2 * lot:
                note_txt = ("持仓不足 2 手（%d 股 < %d 股），按全部卖出处理"
                            % (pos["qty"], 2 * lot))
            else:
                note_txt = ("卖出持仓的一半（%d 股 ÷ 2 = %d 股，向下取整到 %d 股/手 = %d 股）"
                            % (pos["qty"], pos["qty"] // 2, lot, qty))
            reason = ("减仓：档位 reduce，%s；成交后剩余 %d 股，目标权重 %.2f%%；%s"
                      % (note_txt, pos["qty"] - qty,
                         ((pos["qty"] - qty) * price / capital * 100.0) if capital > 0 else 0.0,
                         _cost_note()))
            target_w = ((pos["qty"] - qty) * price / capital) if capital > 0 else 0.0
        else:  # close
            qty = int(pos["qty"])
            reason = ("清仓：档位 %s，卖出全部持仓 %d 股（%d 股/手），成交后目标权重 0.00%%；%s"
                      % (action or "sell", qty, lot, _cost_note()))
            target_w = 0.0

        if qty <= 0:
            _add_skip(code, name, action, "sizing",
                      "股数为 0：可用资金不足一手或持仓不足，未生成委托单")
            continue

        used += 1
        order = _build_order(
            code=code, name=name, market=market, side=side, intent=intent,
            action=action, mode=mode, source="ai", reason=reason, confidence=conf,
            score=row.get("score"), kelly_weight=kelly_w, target_weight=target_w,
            qty=qty, lot=lot, limit_price=price, signal_price=signal_price,
            plan=row.get("plan"), forecast=row.get("forecast"), gates=gates,
            cash=cash, positions=positions, review_id=review_id, ts=ts, capital=capital)
        result["orders"].append(order)

    note = ("本次计划 %d 笔委托、跳过 %d 只（中性档位 %d 只不计入跳过）；"
            "模式 %s（%s）；当日委托已用 %d/%d 笔。闸门明细见 skippedByGate。"
            % (len(result["orders"]), len(skips), result["neutral"], mode,
               "仅出计划不成交" if mode != "paper" else "计划后按报价模拟成交",
               used, max_orders))
    return _finish(note)


# --------------------------------------------------------------------------- #
# 五、成交（paper）与安全属性
# --------------------------------------------------------------------------- #
def _replanned_qty(order, price, lot, positions):
    """按**最新报价 / 最新持仓**重算股数（执行时绝不复用计划时的价格与持仓）。

    · 买（open / add）：按「计划股数 × 计划参考价 ÷ 最新价」等比换算后向下取整到整手，
      即维持大致相同的计划金额，价格涨了自动少买（绝不多买）；
    · 卖（close）：按**当前持仓**全部卖出（持仓可能已被别的动作改变）；
    · 卖（reduce 等）：按**计划股数**执行（卖出量不随报价变化），随后由持仓校验
      决定是否拒单 —— 持仓不足时拒单而不是缩量成交。

    返回 ``(qty, planned)``；``planned`` 只用于拒单文案。
    """
    intent = _text(order.get("intent")).lower()
    side = _text(order.get("side")).lower()
    code = _text(order.get("code")).upper()
    planned = _num(order.get("qty"))
    planned = int(planned) if (planned is not None and planned > 0) else 0

    if side == "sell":
        pos = _pos_of(positions, code)
        held = pos["qty"] if pos else 0
        if intent == "close":
            return held, held
        return _floor_lot(planned, lot), planned
    if planned <= 0:
        return 0, planned
    ref = _num(order.get("limitPrice")) or _num(order.get("signalPrice")) or price
    return _floor_lot(planned * ref / price, lot), planned


def _settle(side, qty, code, name, market, price, fee_rate, slippage, cash, positions,
            realized, ts):
    """在一份账户副本上撮合一笔（调用方已完成校验），返回新的账户片段与成交明细。"""
    if side == "sell":
        idx = _pos_index(positions, code)
        pos = positions[idx]
        avg = pos["avgPrice"]
        f = _fill_sell(qty, price, fee_rate, slippage, code, market, name=name)
        realized_delta = f["notional"] - f["fee"] - avg * qty
        left = int(pos["qty"]) - int(qty)
        if left > 0:
            pos = dict(pos)
            # 今日买入的标记随剩余持仓收缩（卖出的一定是昨仓：T+1 已在执行前校验）
            bought = _int_of(pos.get("todayBought"))
            pos.update({"qty": left, "cost": avg * left, "lastPrice": price, "updatedAt": ts,
                        "todayBought": min(left, bought) if bought > 0 else 0})
            positions[idx] = pos
        else:
            positions.pop(idx)
        f.update({"cash": cash + f["cash"], "realizedDelta": realized_delta,
                  "realized": realized + realized_delta})
        return f

    f = _fill_buy(qty, price, fee_rate, slippage, code, market, name=name)
    day = _trade_day(ts)
    idx = _pos_index(positions, code)
    if idx >= 0:
        pos = dict(positions[idx])
        q_old, avg_old = int(pos["qty"]), pos["avgPrice"]
        q_new = q_old + int(qty)
        avg = (q_old * avg_old + qty * f["price"] + f["fee"]) / q_new
        bought = _int_of(pos.get("todayBought")) if pos.get("todayBoughtOn") == day else 0
        pos.update({"qty": q_new, "avgPrice": avg, "cost": avg * q_new,
                    "lastPrice": price, "updatedAt": ts,
                    "todayBought": bought + int(qty), "todayBoughtOn": day})
        positions[idx] = pos
    else:
        avg = (qty * f["price"] + f["fee"]) / qty
        positions.append({
            "code": code, "name": name or code, "market": market, "qty": int(qty),
            "avgPrice": avg, "cost": avg * qty, "openedAt": ts,
            "lastPrice": price, "updatedAt": ts,
            "todayBought": int(qty), "todayBoughtOn": day,
        })
    f.update({"cash": cash - f["cash"], "realizedDelta": 0.0, "realized": realized})
    return f


def _settle_state(store, cfg, state, aid, positions, cash, realized, fee_total, ts):
    """把账户片段写回存储（先写钱、再写口径），返回落库后的 state。"""
    state = dict(state or {})
    state.update({"positions": positions, "cash": cash, "updatedAt": ts})
    saver = getattr(store, "save_trade_state", None)
    saved = saver(state) if callable(saver) else state
    saved = saved if isinstance(saved, dict) and saved else state
    meta_saved = _save_extra(store, aid, realized, fee_total, ts)
    return saved, meta_saved


def _dump_order(store, order):
    """落库并回读（无 id 或存储层不可用时原样返回，绝不抛异常）。"""
    if not _text(order.get("id")):
        return order
    saver = getattr(store, "save_trade_order", None)
    if callable(saver):
        saver(order)
        getter = getattr(store, "get_trade_order", None)
        view = getter(order.get("id")) if callable(getter) else None
        if isinstance(view, dict) and view:
            return view
    return order


def _equity_point(store, cfg, quotes=None, ts=None):
    """追加一个权益点（只追加）：拿得到报价就按市值，拿不到就按成本估值。"""
    view = account_view(store, cfg, quotes)
    row = {"accountId": view["accountId"], "ts": int(ts if ts is not None else now_ms()),
           "cash": view["cash"], "marketValue": view["marketValue"],
           "equity": view["equity"], "pnl": view["pnl"]}
    fn = getattr(store, "append_trade_equity", None)
    if callable(fn):
        fn(row)
        return row
    return None


def execute_orders(store, config, orders, quotes):
    """按报价撮合（paper）或只回写计划（dryrun），返回逐单结果与账户视图。

    **安全属性（本项目最重要的约束）**：``mode == "dryrun"`` 时本函数只把订单写回
    ``pending``，**绝不修改现金与持仓**（也不写 realizedPnl / feeTotal / 权益点），
    返回 ``filled: 0`` 并在 ``reason`` 写明「dryrun 模式：仅生成计划，不产生成交」。

    paper 模式逐单流程

    1. 取不到报价 → ``status="rejected"``、``error="无有效价格"``；
    2. 用**最新报价**重算股数（见 :func:`_replanned_qty`，绝不复用计划价）；
    3. 校验状态（可能已被别的动作改变）：现金不足 / 无持仓 / 持仓不足 → ``rejected``
       + 中文 ``error``，**不做部分成交**；
    4. 通过后：先更新账户（现金、持仓、``realizedPnl``、``feeTotal``）并落库，再写委托单
       （``status="filled"`` + fillPrice / fee / slippage / amount / filledAt）；
    5. 全部撮合完成后追加一个权益点（``store.append_trade_equity``，只追加）。

    返回
    ----
    dict
        ``filled`` / ``rejected`` / ``count`` / ``orders``（落库后的委托单视图）/
        ``account``（账户视图）/ ``reason`` / ``equityAt``（追加的权益点时间戳，
        未成交为 None，scan 据此避免重复追加）/ ``realizedPnl`` / ``feeTotal``。
    """
    cfg = normalize_config(config, DEFAULT_CONFIG)
    mode = cfg["mode"]
    items = [o for o in orders if isinstance(o, dict)] if isinstance(orders, (list, tuple)) else []
    market = _market(cfg["market"])
    for o in items:
        if _text(o.get("market")):
            market = _market(o.get("market"))
            break
    aid = account_id(market, mode)
    qmap = _quote_map(quotes)
    store_state = ensure_account(store, cfg, market) if store is not None else None
    state = store_state if isinstance(store_state, dict) and store_state \
        else _empty_state(cfg, market, aid)

    fee_rate = _num(state.get("feeRate"))
    fee_rate = DEFAULT_FEE if fee_rate is None else fee_rate
    slip = _num(state.get("slippage"))
    slip = DEFAULT_SLIPPAGE if slip is None else slip
    extra = _load_extra(store, aid)
    realized = extra["realizedPnl"]
    fee_total = extra["feeTotal"]
    ts = now_ms()

    out = {"ok": True, "mode": mode, "market": market, "accountId": aid,
           "count": len(items), "filled": 0, "rejected": 0, "orders": [],
           "account": None, "reason": "", "note": "", "equityAt": None,
           "realizedPnl": _r(realized, ND_MONEY), "feeTotal": _r(fee_total, ND_MONEY),
           "dryrun": mode != "paper", "metaSaved": False}

    # ---------------- dryrun：只回写计划，绝不改钱 ----------------
    if mode != "paper":
        for o in items:
            o2 = dict(o)
            if not _text(o2.get("id")):
                o2["id"] = order_id(ts)
            o2["status"] = "pending"
            o2["mode"] = mode
            o2["updatedAt"] = ts
            o2["reason"] = o2.get("reason") or "dryrun 模式：仅生成计划，不产生成交"
            out["orders"].append(_dump_order(store, o2))
        out["reason"] = "dryrun 模式：仅生成计划，不产生成交（未修改现金与持仓）"
        out["note"] = ("dryrun 模式：%d 笔委托全部保持 pending，账户（现金 / 持仓 / "
                       "realizedPnl / feeTotal）与权益曲线完全不变；要模拟成交请先 "
                       "save_config(store, {\"mode\": \"paper\"})。" % len(items))
        out["account"] = account_view(store, cfg, qmap or None)
        return out

    # ---------------- paper：按报价成交 ----------------
    positions = _positions(state)
    cash = _num(state.get("cash"))
    cash = cfg["capital"] if cash is None else cash
    lot_market = lot_of(market, state.get("lot"))
    filled = rejected = 0

    for o in items:
        o2 = dict(o)
        if not _text(o2.get("id")):
            o2["id"] = order_id(now_ms())   # 外部塞进来的裸单也要有 id 才能进审计链
        code = _text(o2.get("code")).upper()
        side = _text(o2.get("side")).lower()
        if side not in ("buy", "sell"):
            side = "buy" if _text(o2.get("intent")).lower() in ("open", "add") else "sell"
        intent = _text(o2.get("intent")).lower() or ("buy" if side == "buy" else "close")
        lot = lot_of(o2.get("market") or market, o2.get("lot") or lot_market)
        price = qmap.get(code)
        ts = now_ms()
        qty = 0
        reject = None
        held_pos = _pos_of(positions, code)
        held_empty = held_pos is None or held_pos["qty"] <= 0

        if not code:
            reject = "委托单缺少标的代码"
        elif price is None or price <= 0:
            reject = "无有效价格"
        elif side == "sell" and held_empty:
            # 先判「有没有持仓」：否则 close 会被算成 0 股，报出误导性的「委托数量为 0」
            reject = "无对应持仓：%s 当前不在持仓中" % code
        else:
            qty, planned = _replanned_qty(o2, price, lot, positions)
            if planned <= 0:
                reject = "委托数量为 0（计划未给出股数），不成交"
            elif qty <= 0:
                if side == "sell":
                    pos = _pos_of(positions, code)
                    reject = "持仓不足：需卖 %d 股，当前持有 %d 股" % (
                        planned, pos["qty"] if pos else 0)
                else:
                    reject = ("按最新报价 %.4f 重算后不足一手（一手 %d 股约 %.2f，计划 %d 股）"
                              % (price, lot, price * lot, planned))
            elif side == "buy":
                need = buy_cost(qty, price, fee_rate, slip)
                if need > cash + EPS:
                    reject = "现金不足：本笔需 %.2f（含滑点与费用），可用 %.2f" % (need, cash)
            else:
                pos = _pos_of(positions, code)
                if pos is None or pos["qty"] <= 0:
                    reject = "无对应持仓：%s 当前不在持仓中" % code
                elif qty > pos["qty"]:
                    reject = "持仓不足：需卖 %d 股，当前持有 %d 股" % (qty, pos["qty"])
                elif qty > _sellable_of(pos, ts):
                    reject = ("T+1 限制：可卖 %d 股（持仓 %d 股，其中今日买入 %d 股不可当日卖出）"
                              % (_sellable_of(pos, ts), pos["qty"],
                                 _int_of(pos.get("todayBought"))))

        # 交易规则校验（涨跌停封板）：涨停买不到、跌停卖不出。这是「价格可取即成交」
        # 与「遵守交易规则」的分界线 —— 不判封板会在连板股上凭空造出每天都能买在涨停价的
        # 虚假收益（纸面收益漂亮，实盘根本成交不了）
        if not reject:
            fillable = RULES.can_fill(side, price, prev_close=_num(o2.get("signalPrice")) or price,
                                      code=code, market=market, name=o2.get("name"), ts=ts)
            if not fillable.get("ok"):
                reject = "交易规则：%s" % fillable.get("reason")

        if reject:
            o2.update({"status": "rejected", "error": reject, "mode": mode,
                       "qty": int(qty) if qty > 0 else _int_of(o2.get("qty")),
                       "updatedAt": ts})
            o2["reason"] = ((o2.get("reason") or "") + "｜拒单：" + reject).strip("｜")
            rejected += 1
            out["orders"].append(_dump_order(store, o2))
            continue

        fill = _settle(side, qty, code, o2.get("name"), market, price, fee_rate, slip,
                       cash, positions, realized, ts)
        cash = fill["cash"]
        realized = fill["realized"]
        fee_total += fill["fee"]
        state, meta_saved = _settle_state(store, cfg, state, aid, positions, cash,
                                         realized, fee_total, ts)
        out["metaSaved"] = bool(meta_saved or out["metaSaved"])
        o2.update({
            "status": "filled", "mode": mode, "qty": int(qty), "lot": int(lot),
            "fillPrice": _r(fill["price"], ND_PRICE), "fee": _r(fill["fee"], ND_MONEY),
            "slippage": _r(fill["slippage"], ND_MONEY),
            "amount": _r(fill["amount"], ND_MONEY), "filledAt": ts, "updatedAt": ts,
            "error": "",
        })
        o2["targetWeight"] = _r(_weight_after(positions, code, price, cfg["capital"]), ND_RATIO)
        if side == "buy":
            fill_note = ("已成交：%d 股 × 含滑点成交价 %.6f = %.2f，费用 %.2f，"
                         "买入总成本 %.2f（现金余额 %.2f）" % (
                             qty, fill["price"], fill["amount"], fill["fee"],
                             fill["amount"] + fill["fee"], cash))
        else:
            fill_note = ("已成交：%d 股 × 含滑点成交价 %.6f = %.2f，费用 %.2f，"
                         "净收入 %.2f（本笔已实现盈亏 %.2f，现金余额 %.2f）" % (
                             qty, fill["price"], fill["amount"], fill["fee"],
                             fill["amount"] - fill["fee"], fill["realizedDelta"], cash))
        o2["reason"] = ((o2.get("reason") or "") + "｜" + fill_note).strip("｜")
        filled += 1
        out["orders"].append(_dump_order(store, o2))

    out["filled"] = filled
    out["rejected"] = rejected
    out["realizedPnl"] = _r(realized, ND_MONEY)
    out["feeTotal"] = _r(fee_total, ND_MONEY)
    out["reason"] = ("paper 模式：按最新报价成交 %d 笔、拒单 %d 笔（拒单不部分成交）"
                     % (filled, rejected))
    out["note"] = (ORDER_NOTE + "｜本次成交 %d 笔、拒单 %d 笔；账户已按成交结果更新，"
                   "并追加权益点（仅成交时）。" % (filled, rejected))
    if filled > 0:
        point = _equity_point(store, cfg, qmap or None, ts)
        out["equityAt"] = point["ts"] if point else None
    out["account"] = account_view(store, cfg, qmap or None)
    return out


def _weight_after(positions, code, price, capital):
    pos = _pos_of(positions, code)
    if not pos or capital <= 0:
        return 0.0
    return (pos["qty"] * price) / capital


# --------------------------------------------------------------------------- #
# 六、撤单 / 回执 / 手动平仓 / 重置
# --------------------------------------------------------------------------- #
#: 允许撤单的状态（已成交 / 已拒单 / 已撤销是终态，不可再动）
CANCELLABLE = ("pending", "submitted", "acked")


def cancel_order(store, oid, reason="用户撤单"):
    """撤单：把 ``pending / submitted / acked`` 置为 ``cancelled``；不存在返回 None。

    终态（filled / rejected / cancelled / expired）**原样返回、不做任何修改**：
    已成交的单被改成「已撤销」会让审计数据与账户对不上。本引擎不预冻结资金，
    因此撤单只改状态，不需要「解冻」。
    """
    getter = getattr(store, "get_trade_order", None)
    order = getter(oid) if callable(getter) else None
    if not isinstance(order, dict) or not order:
        return None
    status = _text(order.get("status")).lower()
    if status not in CANCELLABLE:
        order = dict(order)
        order["note"] = ("当前状态 %s 不可撤单（允许撤单的状态：%s）"
                         % (status or "未知", " / ".join(CANCELLABLE)))
        return order
    ts = now_ms()
    order = dict(order)
    order.update({"status": "cancelled", "updatedAt": ts,
                  "error": "", "reason": (order.get("reason") or "") + "｜撤单：" + _text(reason, "用户撤单")})
    return _dump_order(store, order)


def close_position(store, config, code, quotes, qty=None):
    """手动平仓：按最新报价立即卖出（``qty`` 缺省为全部），**不受 enabled 限制**。

    手动操作是用户的显式意图，因此不看总开关；但仍然：
    · 遵守 ``mode``：``dryrun`` 下只生成 ``pending`` 单、不改账户（「dryrun 绝不改钱」
      是硬约束，手动操作也不例外）；
    · 需要报价：取不到报价时不成交并说明原因（绝不臆造价格）。

    返回 ``{"ok", "code", "qty", "order", "account", "filled", "note"}``；无持仓 /
    无报价 / 数量非法时 ``ok=False`` 并给出中文 ``note``（不抛异常、不建半截委托）。
    """
    cfg = normalize_config(config, DEFAULT_CONFIG)
    market = _market(cfg["market"])
    target = _text(code).upper()
    out = {"ok": False, "code": target, "qty": 0, "order": None, "filled": 0,
           "account": account_view(store, cfg, quotes), "note": "", "dryrun": cfg["mode"] != "paper"}
    if not target:
        out["note"] = "标的代码为空，无法平仓"
        return out
    if store is None:
        out["note"] = "缺少存储层（store 为 None），无法读取账户与持仓"
        return out

    state = ensure_account(store, cfg, market)
    aid = state.get("accountId") or account_id(market, cfg["mode"])
    positions = _positions(state)
    pos = _pos_of(positions, target)
    if pos is None or pos["qty"] <= 0:
        out["note"] = "无对应持仓：%s 当前不在持仓中（手动平仓不生成委托单）" % target
        return out

    qmap = _quote_map(quotes)
    price = qmap.get(target)
    if price is None or price <= 0:
        out["note"] = "无有效价格：手动平仓需要最新报价，未生成委托单"
        return out

    lot = lot_of(market, state.get("lot"))
    sell_qty = int(pos["qty"])
    if qty is not None:
        want = _num(qty)
        if want is None or want <= 0:
            out["note"] = "平仓数量非法（%s）：应为正数，缺省时按全部持仓平仓" % (qty,)
            return out
        if want >= pos["qty"]:
            sell_qty = int(pos["qty"])
        else:
            sell_qty = min(_floor_lot(want, lot), int(pos["qty"]))
        if sell_qty <= 0:
            out["note"] = ("平仓数量不足一手（要卖 %s 股，%d 股/手；持仓 %d 股）"
                           % (qty, lot, pos["qty"]))
            return out

    # T+1：手动平仓同样不能卖当日买入的部分 —— 「不受 enabled 限制」指的是不受总开关
    # 限制，不等于不受交易规则限制。此前手动路径绕过了 T+1 校验，等于给模拟盘留了一个
    # 能当日买卖的后门（自动路径被拒、手动路径放行，同一件事两种答案）
    sellable = _sellable_of(pos, now_ms())
    if sellable <= 0:
        out["note"] = ("T+1 限制：该持仓 %d 股为当日买入，当日不可卖出（手动平仓同样遵守 T+1）。"
                       % _int_of(pos.get("todayBought")))
        return out
    if sell_qty > sellable:
        out["note"] = ("T+1 限制：可卖 %d 股（持仓 %d 股，其中今日买入 %d 股不可当日卖出），"
                       "本次要卖 %d 股。"
                       % (sellable, pos["qty"], _int_of(pos.get("todayBought")), sell_qty))
        return out

    ts = now_ms()
    reason = ("手动平仓：用户显式操作（不受 enabled 总开关限制），卖出 %d 股（持仓 %d 股，%d 股/手）；%s"
              % (sell_qty, pos["qty"], lot, _cost_note()))
    order = _build_order(
        code=target, name=pos.get("name"), market=market, side="sell", intent="close",
        action="sell", mode=cfg["mode"], source="manual", reason=reason,
        confidence=0.0, score=0.0, kelly_weight=0.0, target_weight=0.0, qty=sell_qty,
        lot=lot, limit_price=price, signal_price=price, plan={}, forecast={},
        gates=_gates(cfg, positions, _orders_today(store, market), store),
        cash=_num(state.get("cash")) or 0.0, positions=positions, review_id="", ts=ts,
        capital=cfg["capital"])
    out["qty"] = sell_qty

    if cfg["mode"] != "paper":
        order["reason"] += "｜dryrun 模式：仅生成计划，不产生成交（账户未改动）"
        out["order"] = _dump_order(store, order)
        out["ok"] = True
        out["note"] = ("dryrun 模式：已生成手动平仓计划（%d 股，来源 manual），但未成交，"
                       "现金与持仓保持不变。" % sell_qty)
        out["account"] = account_view(store, cfg, qmap or None)
        return out

    fee_rate = _num(state.get("feeRate"))
    fee_rate = DEFAULT_FEE if fee_rate is None else fee_rate
    slip = _num(state.get("slippage"))
    slip = DEFAULT_SLIPPAGE if slip is None else slip
    extra = _load_extra(store, aid)
    cash = _num(state.get("cash"))
    cash = cfg["capital"] if cash is None else cash
    realized = extra["realizedPnl"]
    fee_total = extra["feeTotal"]

    fill = _settle("sell", sell_qty, target, pos.get("name"), market, price, fee_rate,
                   slip, cash, positions, realized, ts)
    cash = fill["cash"]
    realized = fill["realized"]
    fee_total = extra["feeTotal"] + fill["fee"]
    _settle_state(store, cfg, state, aid, positions, cash, realized, fee_total, ts)
    order.update({"status": "filled", "fillPrice": _r(fill["price"], ND_PRICE),
                  "fee": _r(fill["fee"], ND_MONEY),
                  "slippage": _r(fill["slippage"], ND_MONEY),
                  "amount": _r(fill["amount"], ND_MONEY), "filledAt": ts, "updatedAt": ts,
                  "error": ""})
    order["reason"] += ("｜已成交：%d 股 × 含滑点成交价 %.6f = %.2f，费用 %.2f，"
                        "净收入 %.2f（本笔已实现盈亏 %.2f）" % (
                            sell_qty, fill["price"], fill["amount"], fill["fee"],
                            fill["amount"] - fill["fee"], fill["realizedDelta"]))
    out["order"] = _dump_order(store, order)
    out["filled"] = 1
    out["ok"] = True
    out["note"] = ("手动平仓已成交：卖出 %d 股，净收入 %.2f，本笔已实现盈亏 %.2f；"
                   "账户明细见 account。" % (sell_qty, fill["amount"] - fill["fee"],
                                             fill["realizedDelta"]))
    _equity_point(store, cfg, qmap or None, ts)
    out["account"] = account_view(store, cfg, qmap or None)
    return out


# --------------------------------------------------------------------------- #
# 七、扫描 / 回执 / 导出 / 快照
# --------------------------------------------------------------------------- #
def _call_recommend(fn, symbols, kwargs):
    """调用注入的研判函数，兼容 ``functools.partial`` 已绑定的参数。

    服务端可能把 fetch_bars / horizon 等通过 partial 预先绑好，此时再传同名 kwarg 会
    抛 ``got multiple values``；这里先展开 partial 把已绑定的键剔掉（只做静态判断，
    不做「失败重试」，避免掩盖研判函数内部的 TypeError）。
    """
    target, bound = fn, set()
    while isinstance(target, functools.partial):
        bound |= set(target.keywords or {})
        target = target.func
    try:
        params = inspect.signature(target).parameters
    except (TypeError, ValueError):
        params = None
    if params and not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params and k not in bound}
    return fn(symbols, **kwargs)


def _advice_summary(advice, rows):
    actions = {k: 0 for k in ACTION_KEYS}
    for r in rows:
        a = _text(r.get("action")).lower() if isinstance(r, dict) else ""
        a = a or "none"
        actions[a] = actions.get(a, 0) + 1
    analyzed = _num(advice.get("analyzed")) if isinstance(advice, dict) else None
    if analyzed is None:
        analyzed = sum(1 for r in rows if isinstance(r, dict) and r.get("ok"))
    requested = _num(advice.get("requested")) if isinstance(advice, dict) else None
    return {"analyzed": int(analyzed), "actions": actions, "count": len(rows),
            "requested": int(requested) if requested is not None else None}


def scan(store, config, recommend_fn, market=None, execute=False):
    """一步到底：构造 symbols → 研判 → 计划 → 落库 →（可选）按报价模拟成交。

    · symbols = ``config.universe``（为空则回退 ``config.whitelist``）；
    · 调 ``recommend_fn(symbols, market=…, horizon=…, capital=…, kelly_fraction=…,
      max_weight=…)``（horizon / kelly_fraction 用 advisor / kelly 的默认值，
      因为配置里没有这两个字段）；
    · ``plan_orders`` → 逐单 ``save_trade_order``（先落计划，审计链完整）；
    · ``execute and mode == "paper"`` 才 ``execute_orders``（**默认 execute=False**，
      即默认只出计划）；
    · 成交时由 ``execute_orders`` 追加权益点（本函数不重复追加）。

    注意：scan 没有实时行情入参，因此用**研判快照里的价格**作为成交报价 —— 这是
    「最近可得的价格」，不是 tick 级实时价。要严格按最新报价成交，请单独调用
    ``execute_orders(store, config, orders, quotes)`` 并传入最新 quotes。

    返回 ``{"ok","market","symbols","adviceSummary","orders","skipped","gates",
    "filled","account","note"}``（另有 ``mode`` / ``count`` / ``executed`` / ``equityAt``
    便于前端与审计）。标的池为空或研判失败时 ``ok=False`` + ``error``。
    """
    cfg = normalize_config(config, DEFAULT_CONFIG)
    mkt = _market(market or cfg["market"])
    symbols = list(cfg["universe"] or cfg["whitelist"])
    base = {"ok": False, "market": mkt, "symbols": symbols, "mode": cfg["mode"],
            "adviceSummary": {"analyzed": 0, "actions": {k: 0 for k in ACTION_KEYS},
                              "count": 0, "requested": None},
            "orders": [], "skipped": [], "gates": _gates(cfg, [], 0, store),
            "filled": 0, "count": 0, "executed": False, "equityAt": None,
            "account": account_view(store, cfg, None), "note": "", "error": ""}
    if not symbols:
        base["note"] = ("标的池为空：config.universe 与 config.whitelist 都为空，"
                        "没有可扫描的标的。请先 save_config(store, {\"universe\": [\"600519\"]})。")
        base["error"] = "标的池为空"
        return base
    if not callable(recommend_fn):
        base["note"] = "未注入研判函数（recommend_fn 不可调用），无法扫描。"
        base["error"] = "recommend_fn 不可调用"
        return base

    kwargs = {"market": mkt, "horizon": SCAN_HORIZON, "capital": cfg["capital"],
              "kelly_fraction": SCAN_KELLY_FRACTION, "max_weight": cfg["maxWeight"]}
    try:
        advice = _call_recommend(recommend_fn, symbols, kwargs)
    except Exception as e:  # noqa: BLE001  研判失败不应让调度线程崩掉
        base["note"] = "研判失败：%s" % e
        base["error"] = "研判失败：%s" % e
        base["gates"] = _gates(cfg, _held_positions(store, cfg, mkt),
                               _orders_today(store, mkt), store)
        return base

    rows = _advice_rows(advice)
    qmap = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = _text(r.get("code")).upper()
        px = _num(r.get("price"))
        if code and px is not None and px > 0:
            qmap[code] = px

    account = ensure_account(store, cfg, mkt) if store is not None else None
    plan = plan_orders(advice, account, cfg, qmap, store)
    saved = []
    saver = getattr(store, "save_trade_order", None)
    for o in plan["orders"]:
        if callable(saver):
            saver(o)
        saved.append(o)

    res = dict(base)
    res["ok"] = True
    res["error"] = ""
    res["adviceSummary"] = _advice_summary(advice, rows)
    res["skipped"] = plan["skipped"]
    res["gates"] = plan["gates"]
    res["count"] = len(saved)
    res["orders"] = saved
    res["planNote"] = plan["note"]

    if execute and cfg["mode"] == "paper" and saved:
        exe = execute_orders(store, cfg, saved, qmap)
        res["executed"] = True
        res["orders"] = exe["orders"]
        res["filled"] = exe["filled"]
        res["equityAt"] = exe["equityAt"]
        res["account"] = exe["account"]
        res["reason"] = exe["reason"]
    else:
        res["account"] = account_view(store, cfg, qmap or None)

    res["note"] = ("扫描完成：标的 %d 只（%s），计划 %d 笔、跳过 %d 只；模式 %s；"
                   "执行开关 execute=%s%s。%s"
                   % (len(symbols), ",".join(symbols[:6]) + ("…" if len(symbols) > 6 else ""),
                      len(saved), len(plan["skipped"]), cfg["mode"], bool(execute),
                      "" if (execute and cfg["mode"] == "paper") else "（本次未成交）",
                      plan["note"]))
    return res


def ack_order(store, oid, ext_ref=None, status=None):
    """外部系统回执：把 ``pending``（或 ``submitted``）置为 ``acked`` 并写入 ``extRef``。

    ``status`` 可显式指定（必须是 ``ORDER_STATUS`` 里的值，否则按 ``acked``）；
    已是终态（filled / rejected / cancelled / expired）的单**原样返回不改动**，
    避免外部回执把已成交的单改回中间状态。不存在返回 None。
    """
    getter = getattr(store, "get_trade_order", None)
    order = getter(oid) if callable(getter) else None
    if not isinstance(order, dict) or not order:
        return None
    cur = _text(order.get("status")).lower()
    out = dict(order)
    if cur not in ("pending", "submitted"):
        out["note"] = ("当前状态 %s 不接受回执（仅 pending / submitted 可回执）" % (cur or "未知"))
        return out
    want = _text(status).lower()
    out.update({"status": want if want in ORDER_STATUS else "acked",
                "extRef": _text(ext_ref) or order.get("extRef") or "",
                "updatedAt": now_ms()})
    return _dump_order(store, out)


def export_intents(store, since=None, limit=200):
    """导出待执行意图（默认 ``status='pending'``）给外部桥接系统消费。

    含**完整 payload**（计划 / 预测 / 风控 / 账户快照），便于外部系统自己复核；
    但**不含 confirmToken** —— 执行口令是密钥，只能由服务端持有、前端回填，
    绝不随导出数据外流（外部系统要下单必须走带 ``X-Trade-Confirm`` 回执的服务端接口）。
    """
    lister = getattr(store, "list_trade_orders", None)
    res = {"rows": [], "total": 0, "limit": int(limit or 200), "offset": 0}
    if callable(lister):
        res = lister(status="pending", since=since, limit=limit) or res
    rows = res.get("rows") if isinstance(res, dict) else []
    rows = rows if isinstance(rows, list) else []
    return {
        "ok": True,
        "orders": rows,
        "count": len(rows),
        "total": int(res.get("total") or 0),
        "limit": int(res.get("limit") or limit or 200),
        "since": _ms(since),
        "status": "pending",
        "confirmHeader": CONFIRM_HEADER,
        "note": ("待执行意图（status=pending）导出：共 %d 笔（全库 %d 笔）。"
                 "出于安全考虑不导出 confirmToken；外部系统执行前须由服务端校验 %s。"
                 % (len(rows), int(res.get("total") or 0), CONFIRM_HEADER)),
    }


def trade_snapshot(store, config, quotes=None):
    """自动交易总览：``{"config","account","gates","counts"}``（+ 少量说明字段）。"""
    cfg = normalize_config(config, DEFAULT_CONFIG)
    mkt = _market(cfg["market"])
    state = None
    getter = getattr(store, "get_trade_state", None)
    if callable(getter):
        state = getter(account_id(mkt, cfg["mode"]))
    positions = _positions(state)
    used = _orders_today(store, mkt)
    counts = {}
    fn = getattr(store, "trade_counts", None)
    if callable(fn):
        try:
            counts = dict(fn() or {})
        except Exception:  # noqa: BLE001
            counts = {}
    # byStatus 始终给全 7 个键：store 不可用时也不能让前端读到 undefined
    by_status = {st: 0 for st in ORDER_STATUS}
    lister = getattr(store, "list_trade_orders", None)
    if callable(lister):
        for st in ORDER_STATUS:
            try:
                by_status[st] = int((lister(status=st, limit=1) or {}).get("total") or 0)
            except Exception:  # noqa: BLE001
                by_status[st] = 0
    counts["byStatus"] = by_status
    counts.setdefault("ordersToday", used)
    extra = _load_extra(store, account_id(mkt, cfg["mode"]))
    return {
        "ok": True,
        "market": mkt,
        "mode": cfg["mode"],
        "config": cfg,
        "account": account_view(store, cfg, quotes),
        "gates": _gates(cfg, positions, used, store),
        "counts": counts,
        "realizedPnl": _r(extra["realizedPnl"], ND_MONEY),
        "feeTotal": _r(extra["feeTotal"], ND_MONEY),
        "note": (CONFIG_NOTE + "｜" + ACCOUNT_NOTE),
    }


# --------------------------------------------------------------------------- #
# 交易时段与定时调度：把「自动交易」里真正的『自动』补上
# --------------------------------------------------------------------------- #
#: 常规交易时段（UTC，周一到周五）。用 **UTC** 而不是「本机时区」：用户机器未必在
#: Asia/Shanghai，写死 UTC 才不会因为时区设置而判断错开市时间。
#: · A股：09:30–11:30 / 13:00–15:00 北京时间 = 01:30–03:30 / 05:00–07:00 UTC（全年不变）
#: · 美股：09:30–16:00 美东 = 13:30–20:00 UTC（夏令时）/ 14:30–21:00 UTC（冬令时）。
#:   这里取 13:30–21:00 的**并集**，前后各多放宽 30 分钟 —— 没有交易日历与冬夏令时库的
#:   前提下，多扫一次最坏结果是「拿到与上一交易日相同的静态行情、生成同样的计划」，
#:   而因为夏令时切换整天不扫，才是真正的漏检。
SESSIONS_UTC = {
    "cn": (((1, 30), (3, 30)), ((5, 0), (7, 0))),
    "us": (((13, 30), (21, 0)),),
}

SCHEDULER_NOTE = (
    "定时调度口径：需要 **enabled + scheduler** 同时开启才会动作；"
    "autoExecute 默认关闭 —— 关着的时候调度只生成计划（等价于无人值守的 dryrun），"
    "打开后且 mode=paper 才会自动成交（仍只发生在本地模拟账户里）。"
    "默认只在常规交易时段内扫描（周一到周五，按 UTC 判定，无交易日历：节假日会照常判定"
    "为开市，此时行情是上一交易日的静态数据，结果与上一交易日相同，不会造成错误成交）；"
    "ignoreMarketHours 可关掉时段判断，仅供演示与回放。单次调度不重叠，间隔夹取 15~3600 秒。"
)


def _sched_minutes(ts):
    """UTC 分钟数与星期（调度判定用）"""
    dt = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc)
    return dt.weekday(), dt.hour * 60 + dt.minute


def in_session(market, ts=None):
    """当前是否处于该市场的常规交易时段（UTC 判定，只看周一到周五）。

    这是一个**粗判**：没有交易日历，所以节假日会被判成开市。这个偏差对自动交易是安全的
    （节假日行情是静态的，生成的是与上一交易日相同的计划，不会产生错误成交），
    但会在节假日多打一次上游请求 —— 取舍写在 SCHEDULER_NOTE 里，不藏着。
    """
    mkt = _market(market)
    stamp = int(ts if ts is not None else time.time())
    # 容忍毫秒：next_session_open() 返回的是毫秒时间戳，若把它直接喂回来（很自然的用法）
    # 会被当成「公元 5 万年」而抛 ValueError。这里统一按秒处理，避免两个函数单位不一致。
    if stamp > 100000000000:      # > 1e11 只可能是毫秒
        stamp //= 1000
    weekday, minutes = _sched_minutes(stamp)
    if weekday >= 5:
        return False
    for (sh, sm), (eh, em) in SESSIONS_UTC.get(mkt, ()):
        if sh * 60 + sm <= minutes < eh * 60 + em:
            return True
    return False


def next_session_open(market, ts=None):
    """下一次开市的 UTC 毫秒时间戳（界面上显示「下次扫描」用）。

    逐分钟向前试，最多找 7 天：判定函数本身极便宜，而这个值只在状态查询时算一次，
    没有必要为它引入交易日历。
    """
    stamp = int(ts if ts is not None else time.time())
    step = 60
    for i in range(1, 7 * 24 * 60 + 1):
        probe = stamp + i * step
        if in_session(market, probe):
            return probe * 1000
    return None


class TradeScheduler:
    """定时自动扫描调度器。

    为什么单独做一个类而不是在 server 里塞个线程：调度逻辑（是否该跑、跑成什么结果、
    跳过原因）是**可测的业务判断**，必须能在没有线程、没有网络的情况下用 `tick_once()`
    直接验证；线程只是它的外壳。

    安全边界（三层开关，缺一不可，默认全关）：
    1. ``enabled``     自动交易总开关 —— 关着时一切都只出计划；
    2. ``scheduler``   定时调度开关 —— 关着时只能手动点扫描；
    3. ``autoExecute`` 自动成交开关 —— 关着时调度只生成 pending 计划；且只有
       ``mode == "paper"`` 才会成交，dryrun 永远不成交。

    另外：默认只在交易时段内动作；单次调度不重叠（锁）；任何异常都不会让调度线程退出
    （异常写进 ``lastError`` 并在下一次继续尝试），否则一次网络抖动就会让「自动」永久失效
    而用户毫无察觉。
    """

    def __init__(self, store, recommend_fn, fetch_quotes=None, market=None,
                 clock=time.time, step=5.0, on_result=None, log=None):
        self.store = store
        self.recommend_fn = recommend_fn
        self.fetch_quotes = fetch_quotes
        self.market_override = market
        self.clock = clock
        self.step = max(0.05, float(step))
        self.on_result = on_result
        self.log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.started_at = None
        self.last_run_at = None
        self.last_result = None
        self.last_error = None
        self.last_skip = None
        #: 上一次被跳过的原因：用于「同一原因只计一次」，见 _skip()
        self._last_skip_reason = None
        self.runs = 0
        self.planned = 0
        self.filled = 0
        self.skips = 0

    # ------------------------------------------------------------------ 生命周期 --
    def running(self):
        return bool(self._thread is not None and self._thread.is_alive())

    def start(self):
        """启动调度线程（幂等）"""
        if self.running():
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="trade-scheduler", daemon=True)
        self._thread.start()
        self.started_at = int(self.clock() * 1000)
        self._log("info", "scheduler.started", step=self.step)
        return self.status()

    def stop(self, timeout=2.0):
        """停止调度线程（幂等）"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._log("info", "scheduler.stopped", runs=self.runs)
        return self.status()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001  调度线程绝不能因为单次异常退出
                self.last_error = str(exc)[:200]
                self._log("error", "scheduler.tick_failed", error=str(exc)[:200])
            self._stop.wait(self.step)

    def _log(self, level, event, **fields):
        # 守卫是「有没有注入」而不是「可不可调用」：项目自带的 core.logs.Logger 提供
        # info/error 方法但并不实现 __call__，用 callable() 判断会把它的日志**全部静默丢弃**
        # （表现为功能正常但事后什么都查不到）
        if self.log is None:
            return
        try:
            getattr(self.log, level)(event, **fields)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 单次调度 --
    def tick_once(self, force=False, now=None):
        """执行一次调度判断（可离线测试）。

        返回 ``{"action": "skip"|"planned"|"executed"|"error", ...}``：无论是跳过还是
        执行，都带上**原因或结果**，这样「为什么没动」在界面上永远有答案 ——
        一个静默不动的自动化系统比一个报错的系统更难用。
        """
        stamp = float(now if now is not None else self.clock())
        if not self._lock.acquire(blocking=False):
            # 走统一的 _skip：否则「锁竞争」这条路径既不计入日志、又绕过同原因去重
            # （实测连续两次争用会让 skips 直接 +2）
            return self._skip("上一次调度尚未结束")
        try:
            return self._tick_locked(stamp, force)
        finally:
            self._lock.release()

    def _tick_locked(self, stamp, force):
        cfg = get_config(self.store)
        # 构造时传入的 market 应当覆盖配置（此前这个参数只被赋值、从未被读取，
        # 是个「看着能用其实无效」的陷阱参数）
        market = _market(self.market_override or cfg.get("market"))
        if force:
            pass
        elif not cfg.get("scheduler"):
            return self._skip("定时调度未启用（配置 scheduler=false）")
        if not cfg.get("enabled"):
            return self._skip("自动交易总开关未开启（enabled=false）")
        interval = _int_in(cfg.get("interval"), DEFAULT_CONFIG["interval"], *INTERVAL_RANGE)
        if not force and self.last_run_at is not None:
            elapsed = stamp - self.last_run_at / 1000.0
            if elapsed < interval:
                return self._skip("未到调度间隔（%d 秒，已过 %.0f 秒）" % (interval, elapsed),
                                  quiet=True)
        if not force and not cfg.get("ignoreMarketHours") and not in_session(market, stamp):
            # force（界面上的「立即试跑」）跳过时段限制：试跑的目的正是「收盘后也想确认链路
            # 通不通」，若还要求交易时段，这个按钮在最需要它的时候就永远只回答「非交易时段」。
            return self._skip("非交易时段（%s）；ignoreMarketHours 可关掉此判断" % market,
                              extra={"nextOpen": next_session_open(market, stamp)})
        symbols = list(cfg.get("universe") or []) or list(cfg.get("whitelist") or [])
        if not symbols:
            return self._skip("标的池为空：请在配置里填写 universe（或 whitelist）")
        execute = bool(cfg.get("autoExecute")) and cfg.get("mode") == "paper"
        try:
            # 先只出计划（execute=False），需要成交时再单独按**最新报价**执行 ——
            # 与手动路径（/api/trade/plan → /api/trade/execute）完全同口径。
            # scan 内部的成交只能用研判快照价，而自动成交发生在快照之后，理应拿最新的价；
            # 这也是本类持有 fetch_quotes 的唯一用途。
            res = scan(self.store, cfg, self.recommend_fn, market=market, execute=False)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:200]
            self._log("error", "scheduler.scan_failed", market=market, error=str(exc)[:200])
            return {"action": "error", "reason": str(exc)[:200], "ts": int(stamp * 1000)}
        self.last_run_at = int(stamp * 1000)
        # 研判整体失败必须记成错误，而不是「已生成 0 笔计划」：scan 内部把上游异常吞成了
        # 一句 note + error 字段，如果这里只看 orders/filled，一次全量取数失败会显示成
        # 「本轮无操作」—— 用户会以为自动化在正常工作，这是最危险的静默失败
        if res.get("error") or (res.get("adviceSummary") or {}).get("analyzed") == 0:
            self.runs += 1
            self.last_error = str(res.get("error") or res.get("note") or "研判未产出任何标的")
            self.last_skip = None
            self._log("error", "scheduler.scan_empty", market=market,
                      error=self.last_error[:200], symbols=len(symbols))
            return {"action": "error", "reason": self.last_error[:200],
                    "ts": self.last_run_at, "market": market,
                    "symbols": len(symbols), "result": res.get("adviceSummary")}
        self.runs += 1
        planned = len(res.get("orders") or [])
        filled = 0
        if execute:
            # 按最新报价成交：报价取不到时**照样尝试**（execute_orders 会按无有效价格记
            # rejected 并写明原因），而不是静默跳过 —— 自动成交失败必须留下痕迹
            pending = [o for o in (res.get("orders") or []) if o.get("status") == "pending"]
            quotes, source = [], "none"
            if pending and callable(self.fetch_quotes):
                try:
                    quotes = self.fetch_quotes(market, [o.get("code") for o in pending]) or []
                    source = "live" if quotes else "none"
                except Exception as exc:  # noqa: BLE001
                    self._log("error", "scheduler.quote_failed", error=str(exc)[:200])
            if pending and not quotes:
                # 取不到实时报价时回退到**计划时的价格**（order.signalPrice / limitPrice）：
                # 宁可「按最近可得价格成交、并把来源标成 snapshot」，也不要因为一次取数失败
                # 把所有委托都记成 rejected —— 后者在界面上表现为「自动交易全是拒单」，
                # 用户会以为策略出了问题，其实是行情源抖了一下。
                quotes = [{"code": o.get("code"), "market": market,
                           "price": o.get("signalPrice") or o.get("limitPrice")}
                          for o in pending]
                source = "snapshot"
            if pending:
                try:
                    done = execute_orders(self.store, cfg, pending, quotes)
                    filled = int(done.get("filled") or 0)
                    by_id = {o.get("id"): o for o in (done.get("orders") or [])}
                    res["orders"] = [by_id.get(o.get("id"), o) for o in (res.get("orders") or [])]
                    res["filled"] = filled
                    res["account"] = done.get("account") or res.get("account")
                    res["executed"] = True
                    res["priceSource"] = source
                    if source != "live":
                        self._log("info", "scheduler.price_fallback", source=source,
                                  orders=len(pending))
                except Exception as exc:  # noqa: BLE001  成交失败不该让整轮调度算失败
                    self.last_error = str(exc)[:200]
                    self._log("error", "scheduler.execute_failed", error=str(exc)[:200])
        else:
            res["filled"] = 0
        self.planned += planned
        self.filled += filled
        self.last_result = {
            "at": self.last_run_at, "market": market, "symbols": len(symbols),
            "analyzed": (res.get("adviceSummary") or {}).get("analyzed"),
            "orders": planned, "skipped": len(res.get("skipped") or []),
            "filled": filled, "execute": execute, "mode": cfg.get("mode"),
        }
        self.last_skip = None
        self._last_skip_reason = None    # 真正跑过一轮后，下次同原因的跳过重新计数
        self.last_error = None
        self._log("info", "scheduler.tick",
                  market=market, mode=cfg.get("mode"), execute=execute,
                  orders=planned, filled=filled, analyzed=self.last_result["analyzed"])
        if callable(self.on_result):
            try:
                self.on_result(res, cfg)
            except Exception:  # noqa: BLE001  推送失败不影响调度
                pass
        return {
            "action": "executed" if filled else "planned",
            "ts": self.last_run_at, "market": market, "execute": execute,
            "mode": cfg.get("mode"),
            "orders": planned, "filled": filled,
            "skipped": len(res.get("skipped") or []),
            "result": self.last_result,
            "note": ("自动成交 %d 笔（仅模拟账户）" % filled) if filled
                    else ("已生成 %d 笔计划（未自动成交：%s）" % (
                        planned, "autoExecute 未开启" if not cfg.get("autoExecute")
                        else "dryrun 模式不成交")),
        }

    def _skip(self, reason, quiet=False, extra=None):
        """记录一次跳过。

        ``skips`` 只在**跳过原因发生变化**时累加：调度线程每 5 秒醒一次，
        「非交易时段」这种整晚都成立的原因若每次都累加，一天就能堆到上万次，
        这个指标会彻底失去意义（实测联调时半小时内就被这类噪声顶起来）。
        原因不变时不重复计数、也不重复写日志，但 ``lastSkip`` 始终保持最新值 ——
        界面上「为什么没动作」永远有答案，只是不会被同一个原因刷屏。
        """
        self.last_skip = reason
        if not quiet:
            if reason != self._last_skip_reason:
                self.skips += 1
                self._log("info", "scheduler.skip", reason=reason)
            self._last_skip_reason = reason
        out = {"action": "skip", "reason": reason, "ts": int(self.clock() * 1000)}
        if extra:
            out.update(extra)
        return out

    # ------------------------------------------------------------------ 状态 --
    def status(self):
        cfg = get_config(self.store)
        return {
            "running": self.running(),
            "startedAt": self.started_at,
            "step": self.step,
            "enabled": bool(cfg.get("enabled")),
            "scheduler": bool(cfg.get("scheduler")),
            "autoExecute": bool(cfg.get("autoExecute")),
            "mode": cfg.get("mode"),
            "market": _market(cfg.get("market")),
            "interval": cfg.get("interval"),
            "inSession": in_session(cfg.get("market"), self.clock()),
            "ignoreMarketHours": bool(cfg.get("ignoreMarketHours")),
            "nextOpen": next_session_open(cfg.get("market"), self.clock()),
            "nextRunAt": (self.last_run_at + int((cfg.get("interval") or 60) * 1000))
            if self.last_run_at else None,
            "lastRunAt": self.last_run_at,
            "lastResult": self.last_result,
            "lastSkip": self.last_skip,
            "lastError": self.last_error,
            "runs": self.runs,
            "planned": self.planned,
            "filled": self.filled,
            "skips": self.skips,
            "note": SCHEDULER_NOTE,
        }
