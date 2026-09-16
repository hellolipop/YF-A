#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复盘绩效统计（review）：把「已成交委托单」与「权益曲线」折算成可核对的绩效指标。

模块定位
--------
`core/advisor.py` 的 :func:`core.advisor.review` 回答的是「**AI 当时的建议方向对不对**」
（按标的做 hit / miss 判定）。本模块回答的是另外两个问题，两者互不重叠：

1. **账户赚没赚、赚得稳不稳** —— 从委托单还原「往返交易」（逐笔口径），从权益曲线
   算 Sharpe / Sortino / 回撤族指标（周期口径）；
2. **按来源拆账** —— 按 AI 任务来源（ai / manual / scheduled）与 AI 建议档位分组统计。

本模块**不重复实现** advisor 已有的命中率口径：档位 → 方向（看多 / 看空）的映射直接复用
:data:`core.advisor.REVIEW_DIRECTION` 与 :data:`core.advisor.REVIEW_NEUTRAL`，
档位中文名复用 :data:`core.advisor.ACTION_LABEL`，避免两处定义漂移。

口径与出处要点（联网调研结论，落实为下面的实现约定）
----------------------------------------------------
1. **必须同时给两套口径，不能混用（QuantStats 的教训）**：
   QuantStats 的 `stats.py` 全部作用于**收益率序列**（period returns）——它的
   `consecutive_wins` / `consecutive_losses` 是「连续 N 个**周期**」、
   `exposure` 是「非零收益**周期**占比」，因此 `win_rate` = 正收益周期占比、
   `payoff_ratio` = 平均盈利周期 / 平均亏损周期，都是**周期口径**。
   同一笔持 5 天的盈利交易，在周期口径里可能是 3 胜 2 负 —— 于是
   「逐笔胜率」与「周期胜率」天然会不一致，谁也不能替代谁。
   → 本模块：:func:`trade_metrics`（逐笔）与 :func:`period_metrics`（周期）**各算一套**，
   :data:`METRIC_NOTE` 把差别写清楚（前端原样展示），避免「AI 选股胜率」被误读。

2. **指标清单对齐 fugazi 的 `metrics` 模块，一个指标一个函数**：
   fugazi 明确「No aggregate `compute` — every metric is its own `pub fn`」，且共享的
   昂贵中间量（per-bar returns / reconstructed round-trip trades / drawdown segments）
   由各自的公开函数**只构建一次**后传给下游指标。
   → 本模块照此拆分：:func:`per_bar_returns` / :func:`drawdown_segments`
   / :func:`round_trips` 是三个共享中间量，`expectancy` / `payoff_ratio` /
   `profit_factor` / `max_drawdown` / `max_drawdown_duration` /
   `time_in_drawdown_ratio` / `ulcer_index` / `kelly_fraction` 等各自独立成函数。

3. **分母为 0 的比率返回 None，不返回 0、也不返回 inf**（fugazi：`Option<Real>`，
   读作 `None`）：`profit_factor` 在「没有亏损笔」时是 `None`（`inf` 还会让
   `json.dumps` 直接抛错）；`payoff_ratio` 在「0 亏损或 0 盈利」时是 `None`；
   样本为 0 时胜率 / 期望值为 `None` —— 0 会被前端读成「真的很差」，`None` 才会显示成「—」。
   与 fugazi 的一处**有意分歧**：无权益数据时 `maxDrawdown` 返回 `None` 而不是 `0.0`
   （「没有数据」和「从未回撤」是两件事，不能混）。

4. **MAE / MFE 必须自建，且宁可空着也不冒充**：fugazi 的指标清单里没有 MAE / MFE，
   开源绩效库普遍不提供 —— 它需要**盘中价格路径**（每根 bar 内的最高 / 最低价）。
   本项目的 `trade_orders` 只记录成交价（没有 bar 内路径），因此
   :func:`trade_metrics` 的 `mae` / `mfe` 一律返回 `None`，并在 warnings 写明
   「缺少盘中价格序列，MAE/MFE 不可用」，**绝不用收盘价冒充盘中极值**。

5. **出入金会静默污染收益与回撤**：fugazi 的 *Closed system* 一节指出，所有指标都假设
   权益曲线是封闭系统，「一笔出金在曲线上与一笔亏损长得一模一样，且被污染的 bar 会
   永久留在序列里」，正确做法是调用方先做**链式时间加权**处理：
   `r_i = (E_i − F_i) / E_{i−1} − 1`（F 为当期净流入；把 F 归到期初则写成
   `r_i = E_i / (E_{i−1} + F_i) − 1`，两者**必须二选一**）。
   → :func:`cache_flow_metrics` 实现前者（F 归属期末）并写成模块常量 :data:`FLOW_FORMULA`
   注明所选口径，:func:`review_report` 在 `metrics["flowNote"]` 里说明
   「已按时间加权剔除出入金」或「未检测到出入金」。

6. **样本量必须一起给**：逐笔样本 < 30 时给出 `sampleWarning`
   （「样本量不足，胜率/盈亏比不具统计意义，仅作记录」）与 `minSampleForConfidence`；
   `advisor_breakdown` 里样本 < 5 的分组标注「样本过少」，**不参与命中率排名**。

7. **不要假精确**：金额 6 位、比率 4 位、百分数 3 位、价格 4 位（与项目里
   `core.advisor._r(x, 4)` / `trader.ND_MONEY = 6` 的既有约定一致）；
   单位一律取**自然单位**（fugazi：fractions / ratios / bar counts，
   `0.15` 就是 +15%），只有名字里带 `Pct` 的字段是百分数。

数据来源（只读，不改动任何其它模块）
------------------------------------
· `trade_orders` → :func:`round_trips` / `trade_metrics` / `bySource`；
· `trade_equity` → :func:`period_metrics` / :func:`drawdown_detail` / :func:`cache_flow_metrics`；
· `advisor_runs` + `advisor_items` → :func:`advisor_breakdown`。

本模块纯标准库、零网络、纯只读：任何异常都不向上抛（存储层失败降级为 warnings）。
"""

from __future__ import annotations

import json
import math
import time

# 复用 advisor 的档位口径（方向 / 中性 / 中文名），不在这里重写一遍映射
from . import advisor as A
# 复用账户 id 约定（"<mode>:<market>"）与配置读取，避免两套账户命名
from . import trader as T

__all__ = [
    "METRIC_NOTE", "FLOW_FORMULA", "MIN_SAMPLE", "MIN_GROUP_SAMPLE",
    "round_trips", "round_trips_detail", "trade_metrics", "period_metrics",
    "drawdown_detail", "advisor_breakdown", "cache_flow_metrics", "review_report",
    "per_bar_returns", "drawdown_segments", "max_drawdown", "max_drawdown_duration",
    "time_in_drawdown_ratio", "ulcer_index", "expectancy", "payoff_ratio",
    "profit_factor", "kelly_fraction",
]

# --------------------------------------------------------------------------- #
# 一、常量与口径
# --------------------------------------------------------------------------- #

#: 金额（元）保留位数 —— 与 core/trader.py 的 ND_MONEY 一致
ND_MONEY = 6
#: 比率（0~1 的小数、倍数）保留位数 —— 与 core/advisor.py 里 `_r(x, 4)` 的费率口径一致
ND_RATIO = 4
#: 百分数保留位数 —— 与 advisor 对 sinceReturn 的 3 位一致
ND_PCT = 3
#: 价格保留位数
ND_PRICE = 4

#: 年化系数：本项目权益曲线是「逐交易日」口径，故用 252（需求指定 sharpe × √252）
BARS_PER_YEAR = 252

#: 逐笔样本量下限：低于此值只作记录，不做统计推断
MIN_SAMPLE = 30
#: 分组（档位 / 策略）最小样本量：低于此值只标注「样本过少」，不参与排名
MIN_GROUP_SAMPLE = 5

#: 年化外推上限：2 个 bar 的 +100% 折成年化是 1e30 量级，没有意义，超过就返回 None
MAX_ANNUALIZED = 1e6

#: 单次复盘读取的上限（委托单 / 权益点），避免把整库拉进内存
ORDER_LIMIT = 5000
EQUITY_LIMIT = 5000
#: 单次复盘最多展开多少条 AI 记录（每条都要再查一次 advisor_items）
RECORD_LIMIT = 50

_EPS = 1e-12

#: 出入金的时间加权链式口径（fugazi *Closed system* 一段给出的标准做法，二选一后固定用它）
FLOW_FORMULA = "r_i = (E_i - F_i) / E_{i-1} - 1（F = 当期净流入，归属**期末**）"

#: 逐笔口径 / 周期口径的差别说明（前端原样展示）
METRIC_NOTE = (
    "指标口径说明：① 本模块给出**逐笔口径**与**周期口径**两套数字，二者不可混用。"
    "逐笔口径把「一次完整往返（开仓腿→平仓腿）」记为 1 笔，胜率 = 盈利笔数 / 总笔数；"
    "周期口径按权益曲线逐 bar 统计，胜率 = 正收益周期占比（QuantStats 等库的 win_rate / "
    "payoff_ratio 就是这一套：它的 consecutive_wins 数的是连续 N 个周期、exposure 数的"
    "是非零收益周期占比）。同一笔持 5 天的盈利交易在周期口径里可能被记成 3 胜 2 负，"
    "所以「AI 选股胜率」这类数字必须写明是哪一套口径。"
    "② 比率为自然单位而非百分数（0.15 就是 +15%，倍数即倍数），只有名字带 Pct 的字段是百分数；"
    "分母为 0 的比率一律返回 null（前端显示「—」），不返回 0 —— 0 会被读成「真的很差」。"
    "③ MAE/MFE 需要盘中价格序列，本项目委托单只记录成交价、无法还原盘中路径，因此如实返回 null，"
    "不用收盘价冒充盘中极值。"
    "④ 权益曲线默认是封闭系统；出金在曲线上与亏损长得一模一样，因此检测到出入金时会按时间加权"
    "链式处理 r_i = (E_i - F_i)/E_{i-1} - 1 先剔除出入金再统计，原始含出入金的口径另存对比。"
    "⑤ 逐笔样本少于 30 笔时胜率与盈亏比不具统计意义，仅作记录。"
    "⑥ 复盘是事后回看，历史表现不代表未来。"
)

#: MAE / MFE 不可用时的固定告警文案
MAE_MFE_WARNING = "缺少盘中价格序列，MAE/MFE 不可用（不用收盘价冒充盘中最高/最低价）"

#: 逐笔口径的补充说明
TRADE_NOTE = (
    "逐笔口径：只在「已成交」委托单上配对，同向成交按成交量加权合并为开仓腿，"
    "反向成交平掉开仓腿（部分平仓 = 多笔已完成交易，超出部分视为反手开新腿）；"
    "手续费计入 pnl（开仓费按平仓比例分摊，平仓费按该笔成交量占比拆分）；"
    "尚未平仓的开仓腿不计入已完成交易。bars 只在委托单自带 bars 字段时可用，否则为 null。"
)

#: 周期口径的补充说明
PERIOD_NOTE = (
    "周期口径：按权益曲线逐 bar 计算收益率，年化系数固定 252（逐交易日假设）；"
    "Sharpe = mean(r)/stdev(r)×√252（样本标准差），Sortino 的下行标准差用 n 分母、MAR=0；"
    "回撤类字段都是**正数幅度**（0.2 = 回撤 20%），与 core/metrics.py 的 max_drawdown 同号。"
)

#: 分组统计口径说明
GROUP_NOTE = (
    "分组命中率沿用 core/advisor.review 的口径：只有 buy/add（看多组）与 reduce/sell/avoid"
    "（看空组）参与命中统计，hold/watch 属中性档位只记录涨跌；样本少于 %d 的分组标注"
    "「样本过少」且不参与排名。" % MIN_GROUP_SAMPLE
)


# --------------------------------------------------------------------------- #
# 二、基础工具（宽松取数 / 四舍五入 / 时间）
# --------------------------------------------------------------------------- #

def _num(x):
    """宽松取数：数字原样，数字字符串转 float，其余（None / bool / 不可解析）返回 None。

    委托单 / 权益点可能来自 HTTP JSON（数值被写成字符串），这里统一收口，
    避免调用方为了一个 `"12.5"` 崩溃。bool 不算数字（True 当 1.0 会静默算错）。
    """
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if not s:
            return None
        try:
            v = float(s)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None
    return None


def _r(x, nd=ND_RATIO):
    """四舍五入到 nd 位；None / 非数值原样返回 None（不用 0 冒充「算不出来」）。"""
    v = _num(x)
    if v is None:
        return None
    try:
        return round(v, nd)
    except (TypeError, ValueError, OverflowError):
        return None


def _ms(x):
    """宽松取毫秒时间戳：数字 / 数字字符串 → int；其余 → None。"""
    v = _num(x)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(ms):
    """毫秒时间戳 → 本地时间字符串（失败返回 None）。"""
    v = _ms(ms)
    if v is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v / 1000.0))
    except (ValueError, OSError, OverflowError):
        return None


def _text(x, default=""):
    if x is None:
        return default
    s = str(x).strip()
    return s if s else default


def _mean(seq):
    """算术平均；空序列返回 None（与「平均值 = 0」区分开）。"""
    vals = [v for v in (seq or []) if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _stdev(seq):
    """样本标准差（ddof=1）；样本不足 2 个返回 None。"""
    vals = [v for v in (seq or []) if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    if var <= 0 or not math.isfinite(var):
        return None
    return math.sqrt(var)


def _safe(fn, default=None):
    """执行一个只读探针；任何异常都吞掉并返回 default（存储层失败不该让复盘整体崩掉）。"""
    try:
        return fn()
    except Exception:  # noqa: BLE001  复盘是只读视图，绝不因为读不到某张表而抛错
        return default


# --------------------------------------------------------------------------- #
# 三、共享中间量 1：往返交易（逐笔）
# --------------------------------------------------------------------------- #

_BUY_WORDS = ("buy", "b", "long", "open", "entry", "add", "increase", "cover")
_SELL_WORDS = ("sell", "s", "short", "close", "exit", "reduce", "decrease")
#: 现金出入类委托（不算交易腿）：识别到这些词就跳过，否则会被当成买入腿
_CASH_WORDS = ("deposit", "withdraw", "withdrawal", "transfer", "transfer_in",
               "transfer_out", "cash_in", "cash_out", "adjust", "funding", "in", "out")
#: status 允许参与配对的取值（缺 status 视为已成交，因为调用方通常只喂 filled）
_FILLED_STATUS = ("filled", "partial", "partially_filled", "part_filled", "done", "executed")


def _order_status(order):
    return _text(order.get("status")).lower()


def _order_side(order):
    """委托单方向 → +1（买/开多）/ -1（卖/开空）/ None（无法判断）。"""
    for key in ("side", "direction"):
        raw = _text(order.get(key)).lower()
        if not raw:
            continue
        if raw in _BUY_WORDS:
            return 1
        if raw in _SELL_WORDS:
            return -1
    intent = _text(order.get("intent")).lower()
    if intent:
        if intent in _BUY_WORDS:
            return 1
        if intent in _SELL_WORDS:
            return -1
    return None


def _is_cash_order(order):
    """是否是出入金 / 资金调拨类委托（这类不是交易腿，必须排除）。"""
    for key in ("side", "intent", "action", "source"):
        raw = _text(order.get(key)).lower()
        if raw in _CASH_WORDS:
            return True
    return False


def _order_price(order):
    """成交价：只认成交价字段（fillPrice / fill_price / avgPrice / price）。

    **不回退到 limitPrice / signalPrice**：那是计划价与信号价，用它们算 pnl 会
    凭空造出一个不存在的成交结果（宁可跳过并记入 skipped 说明原因）。
    """
    for key in ("fillPrice", "fill_price", "avgPrice", "avg_price", "price"):
        v = _num(order.get(key))
        if v is not None and v > 0:
            return v
    return None


def _order_qty(order):
    """成交量：qty / quantity / shares / filledQty，必须 > 0。"""
    for key in ("qty", "quantity", "shares", "filledQty", "filled_qty"):
        v = _num(order.get(key))
        if v is not None and v > 0:
            return v
    return None


def _order_fee(order):
    """手续费：本项目 trade_orders.fee 是**金额**（trader 落库时已按费率算好）。"""
    for key in ("fee", "feeAmount", "fee_amount", "commission"):
        v = _num(order.get(key))
        if v is not None:
            return v
    return 0.0


def _order_ts(order):
    for key in ("filledAt", "filled_at", "updatedAt", "updated_at", "createdAt", "created_at"):
        v = _ms(order.get(key))
        if v is not None:
            return v
    return None


def _order_bars(order):
    for key in ("bars", "holdBars", "hold_bars"):
        v = _num(order.get(key))
        if v is not None and v >= 0:
            return int(v)
    return None


def _order_key(order):
    """配对分组键：(market, code)。market 参与分组，避免跨市场同名代码串仓。"""
    return (_text(order.get("market")), _text(order.get("code")).upper())


def round_trips_detail(orders):
    """把委托单配对成往返交易，并**如实回报没配上的部分**（:func:`round_trips` 的完整版）。

    配对规则（对齐 fugazi `reconstruct_trades`：per symbol、同向加仓按成交量加权、
    反向平掉或反手、一条平仓腿 = 一笔交易）

    * 按 `(market, code)` 分组，组内按时间升序（缺时间戳的排在最前，保持传入顺序）；
    * 同向成交 → 合并进当前开仓腿：`qty += 成交量`，`名义金额 += 成交量 × 成交价`，
      手续费累加，**开仓时间保持第一笔**（多次加仓后一次平仓仍算一笔交易）；
    * 反向成交 → 平掉 `min(平仓量, 开仓量)`：开仓价 = 开仓腿成交量加权均价，
      手续费按成交量比例分摊（开仓费按 `平仓量/开仓量`、平仓费按 `本笔平仓量/本笔成交量`），
      `pnl = 方向 × (exit − entry) × qty − 分摊手续费`；
    * 平仓量超过开仓量 → 多出的部分**反手开新腿**（同一次成交里既平仓又开新仓）；
    * 只有开仓、没有平仓的腿 → 只计入 `openLegs`，**不算已完成交易**。

    返回 ``{"trips", "openLegs", "skipped", "cashOrders", "note"}``：
    `skipped` 逐条写明「哪一笔、为什么没参与配对」（缺代码 / 缺成交量 / 缺成交价 /
    状态是 pending / 是出入金单），因为静默丢单是最难排查的一类缺陷。
    """
    raw = orders if isinstance(orders, (list, tuple)) else []
    skipped = []
    cash_orders = 0
    usable = []
    for idx, order in enumerate(raw):
        if not isinstance(order, dict):
            skipped.append({"index": idx, "code": None, "reason": "不是 dict（%s）" % type(order).__name__})
            continue
        if _is_cash_order(order):
            cash_orders += 1
            continue
        status = _order_status(order)
        if status and status not in _FILLED_STATUS:
            skipped.append({"index": idx, "code": _text(order.get("code")) or None,
                            "reason": "状态 %s 不是已成交" % status})
            continue
        market, code = _order_key(order)
        if not code:
            skipped.append({"index": idx, "code": None, "reason": "缺少标的代码"})
            continue
        side = _order_side(order)
        if side is None:
            skipped.append({"index": idx, "code": code, "reason": "无法判断买卖方向（side/intent 缺失或非法）"})
            continue
        qty = _order_qty(order)
        if qty is None:
            skipped.append({"index": idx, "code": code, "reason": "缺少有效成交量（qty ≤ 0 或缺失）"})
            continue
        price = _order_price(order)
        if price is None:
            skipped.append({"index": idx, "code": code, "reason": "缺少有效成交价（fillPrice 缺失）"})
            continue
        usable.append({"idx": idx, "market": market, "code": code, "side": side, "qty": qty,
                       "price": price, "fee": _order_fee(order), "ts": _order_ts(order),
                       "bars": _order_bars(order), "name": _text(order.get("name")) or code,
                       "source": _text(order.get("source")), "id": _text(order.get("id"))})

    # 时间升序（缺时间戳当 0，靠 idx 保序）；store 的列表是时间倒序，必须重排
    usable.sort(key=lambda o: (o["ts"] if o["ts"] is not None else 0, o["idx"]))

    trips = []
    legs = {}
    for o in usable:
        key = (o["market"], o["code"])
        done, leg = _apply_fill(o, legs.get(key))
        trips.extend(done)
        if leg is None:
            legs.pop(key, None)
        else:
            legs[key] = leg
    return {
        "trips": trips,
        "openLegs": [_leg_view(v) for v in legs.values() if v["qty"] > _EPS],
        "skipped": skipped,
        "cashOrders": cash_orders,
        "note": TRADE_NOTE,
    }


def _apply_fill(o, leg):
    """把一笔成交应用到当前开仓腿，返回 `(本次完成的交易列表, 应用后的开仓腿或 None)`。

    先平反向部分（`min(成交, 未平仓)`，因此反向成交一定被吃完），剩余量再并入同向腿
    （多次加仓）或新开腿（反手）。手续费按量比例分摊，**不允许**把整笔手续费
    都记到其中一笔上（部分平仓时分摊错了会让逐笔 pnl 失真）。
    """
    done = []
    qty_left = o["qty"]
    fee_left = o["fee"]
    if leg is not None and leg["dir"] != o["side"] and leg["qty"] > _EPS and qty_left > _EPS:
        close_qty = min(qty_left, leg["qty"])
        entry = leg["notional"] / leg["qty"]                       # 开仓腿成交量加权均价
        open_fee_share = leg["fee"] * (close_qty / leg["qty"])     # 开仓费按平仓比例分摊
        close_fee_share = fee_left * (close_qty / qty_left)        # 平仓费按本笔占比拆分
        fee_total = open_fee_share + close_fee_share
        pnl = leg["dir"] * (o["price"] - entry) * close_qty - fee_total
        basis = entry * close_qty
        done.append({
            "code": leg["code"], "market": leg["market"], "name": leg["name"],
            "dir": leg["dir"],
            "openAt": leg["openAt"], "closeAt": o["ts"],
            "qty": _r(close_qty, ND_MONEY),
            "entry": _r(entry, ND_PRICE), "exit": _r(o["price"], ND_PRICE),
            "fee": _r(fee_total, ND_MONEY), "pnl": _r(pnl, ND_MONEY),
            "returnPct": _r((pnl / basis * 100.0) if basis > 0 else None, ND_PCT),
            "bars": leg["bars"] if leg.get("bars") is not None else o["bars"],
            "source": leg["source"], "openOrderId": leg.get("orderId"),
            "closeOrderId": o.get("id"),
        })
        leg = dict(leg)
        leg["qty"] -= close_qty
        leg["notional"] -= entry * close_qty
        leg["fee"] -= open_fee_share
        qty_left -= close_qty
        fee_left -= close_fee_share
        if leg["qty"] <= _EPS:
            leg = None
    if qty_left > _EPS:
        if leg is None:
            leg = {"market": o["market"], "code": o["code"], "name": o["name"],
                   "dir": o["side"], "qty": qty_left, "notional": qty_left * o["price"],
                   "fee": fee_left, "openAt": o["ts"], "source": o["source"],
                   "bars": o["bars"], "orderId": o.get("id")}
        else:   # 到这里 leg 必与 o 同向（反向量已在上面被 min() 吃完）
            leg = dict(leg)
            leg["qty"] += qty_left
            leg["notional"] += qty_left * o["price"]
            leg["fee"] += fee_left
            if leg.get("bars") is None:
                leg["bars"] = o["bars"]
    return done, leg


def _leg_view(leg):
    """未平仓腿的对外视图（写明它为什么没进「已完成交易」）。"""
    qty = _num(leg.get("qty")) or 0.0
    entry = (leg["notional"] / qty) if qty > 0 else None
    return {"code": leg.get("code"), "market": leg.get("market"), "name": leg.get("name"),
            "dir": leg.get("dir"), "qty": _r(qty, ND_MONEY),
            "entry": _r(entry, ND_PRICE), "openedAt": leg.get("openAt"),
            "source": leg.get("source"), "note": "仅开仓未平仓，不计入已完成交易"}


def round_trips(orders):
    """委托单 → 往返交易列表（**只含已完成的**：有开仓腿也有平仓腿）。

    每条：``{"code","openAt","closeAt","qty","entry","exit","fee","pnl","returnPct",
    "bars","source"}``（另带 market / name / dir / openOrderId / closeOrderId 便于审计）。
    未平仓腿、无法配对的委托（缺代码/数量/成交价、状态非已成交）都不在这里出现，
    但会被 :func:`round_trips_detail` 如实计数，避免「静默丢单」。
    """
    got = round_trips_detail(orders)
    return got["trips"] if isinstance(got, dict) else []


# --------------------------------------------------------------------------- #
# 四、共享中间量 2 / 3：逐 bar 收益 与 回撤分段
# --------------------------------------------------------------------------- #

def _norm_equity(equity):
    """权益输入 → 规范化点列 ``[{"t","v","flow"}]``（脏数据跳过，绝不抛异常）。

    接受：``[{"t": ms, "v": 100.0, ...}]``、``[{"ts":…, "equity":…}]``、
    纯数字列表；点上的 ``flow`` / ``netFlow`` / ``cashFlow`` 视为**当期净流入**。
    """
    out = []
    if not isinstance(equity, (list, tuple)):
        return out
    for item in equity:
        if isinstance(item, dict):
            v = None
            for key in ("v", "value", "equity", "nav", "close"):
                v = _num(item.get(key))
                if v is not None:
                    break
            if v is None:
                continue
            ts = None
            for key in ("t", "ts", "time", "date"):
                ts = _ms(item.get(key))
                if ts is not None:
                    break
            flow = 0.0
            for key in ("flow", "netFlow", "net_flow", "cashFlow", "externalFlow"):
                f = _num(item.get(key))
                if f is not None:
                    flow = f
                    break
            out.append({"t": ts, "v": v, "flow": flow})
        else:
            v = _num(item)
            if v is not None:
                out.append({"t": None, "v": v, "flow": 0.0})
    return out


def per_bar_returns(values, flows=None):
    """逐 bar 收益率（共享中间量，返回值长度 = bar 数 − 1）。

    无 `flows`：``r_i = v_i / v_{i-1} - 1``；
    有 `flows`：``r_i = (v_i - F_i) / v_{i-1} - 1``（见 :data:`FLOW_FORMULA`，时间加权剔出入金）。
    前值 ≤ 0 的 bar 记 0.0（收益无法定义时不制造 inf/NaN）。
    """
    vals = []
    for v in (values or []):
        x = _num(v)
        if x is not None:
            vals.append(x)
    fl = []
    if isinstance(flows, (list, tuple)):
        fl = [_num(x) or 0.0 for x in flows]
    out = []
    for i in range(1, len(vals)):
        prev = vals[i - 1]
        if prev <= _EPS:
            out.append(0.0)
            continue
        f = fl[i] if i < len(fl) else 0.0
        out.append((vals[i] - f) / prev - 1.0)
    return out


def _segment(peak_i, peak, trough_i, trough, recovery_i, last_i, underwater):
    """构造一个回撤段（内部用；depth 为正数幅度）。"""
    depth = ((peak - trough) / peak) if peak > 0 else None
    end_i = recovery_i if recovery_i is not None else last_i
    return {
        "peakIndex": int(peak_i), "troughIndex": int(trough_i),
        "recoveryIndex": None if recovery_i is None else int(recovery_i),
        "peakValue": _r(peak, ND_MONEY), "troughValue": _r(trough, ND_MONEY),
        "depth": _r(depth, ND_RATIO),
        "durationBars": int(max(0, end_i - peak_i)),
        "underwaterBars": int(underwater),
        "recovered": recovery_i is not None,
    }


def drawdown_segments(values):
    """回撤分段（共享中间量）：一段 = 「前高 → 谷底 → 收复（或期末仍未收复）」。

    对齐 fugazi `drawdown_segments`：单调不降的曲线返回空列表。
    注意 `durationBars`（前高到收复的 bar 数）与 `depth`（跌多深）是**两个独立维度** ——
    「跌得深但很快收复」与「跌得浅但长期水下」分别由不同字段反映。
    """
    vals = []
    for v in (values or []):
        x = _num(v)
        if x is not None:
            vals.append(x)
    segs = []
    if not vals:
        return segs
    peak, peak_i = vals[0], 0
    trough, trough_i, underwater = None, None, 0
    tol = _EPS
    for i in range(1, len(vals)):
        v = vals[i]
        if v >= peak - tol * (abs(peak) + 1.0):
            if trough is not None:
                segs.append(_segment(peak_i, peak, trough_i, trough, i, i, underwater))
            peak, peak_i = v, i
            trough, trough_i, underwater = None, None, 0
        else:
            underwater += 1
            if trough is None or v < trough:
                trough, trough_i = v, i
    if trough is not None:
        last = len(vals) - 1
        segs.append(_segment(peak_i, peak, trough_i, trough, None, last, underwater))
    return segs


# --------------------------------------------------------------------------- #
# 五、一个指标一个函数（对齐 fugazi metrics 的拆分方式）
# --------------------------------------------------------------------------- #

def max_drawdown(segments, bars=None):
    """最大回撤幅度（正数小数，0.2 = 20%）。

    `bars` 为曲线 bar 数：>0 且没有回撤段 → 0.0（真的没回撤）；
    无数据（bars 为 None 或 ≤0）→ None（「没有数据」不等于「没有回撤」）。
    """
    nbars = _num(bars)
    if nbars is not None and nbars <= 0:
        return None
    depths = [s.get("depth") for s in (segments or [])
              if isinstance(s, dict) and s.get("depth") is not None]
    if depths:
        return _r(max(depths), ND_RATIO)
    return 0.0 if nbars else None


def max_drawdown_duration(segments, bars=None):
    """最长水下时间（bar 数，与回撤深度无关）；同样区分「无数据」与「没回撤」。"""
    nbars = _num(bars)
    if nbars is not None and nbars <= 0:
        return None
    durs = [int(s.get("durationBars") or 0) for s in (segments or [])
            if isinstance(s, dict)]
    if durs:
        return max(durs)
    return 0 if nbars else None


def time_in_drawdown_ratio(segments, bars=None):
    """水下 bar 占比 = 水下 bar 数 / 总 bar 数（无数据返回 None）。"""
    nbars = _num(bars)
    if nbars is None or nbars <= 0:
        return None
    under = sum(int(s.get("underwaterBars") or 0) for s in (segments or [])
                if isinstance(s, dict))
    return _r(min(under, int(nbars)) / float(nbars), ND_RATIO)


def ulcer_index(values):
    """Peter Martin 的 Ulcer Index：逐 bar 回撤的**均方根**（正数小数）。

    逐 bar 回撤 `dd_i = v_i / running_peak_i - 1 ≤ 0`，UI = sqrt(mean(dd_i²))；
    创新高的 bar 贡献 0，因此单调不降曲线得 0.0。
    """
    vals = []
    for v in (values or []):
        x = _num(v)
        if x is not None:
            vals.append(x)
    if not vals:
        return None
    peak = vals[0]
    acc = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (v / peak - 1.0) if peak > _EPS else 0.0
        acc += dd * dd
    return _r(math.sqrt(acc / float(len(vals))), ND_RATIO)


def expectancy(pnls):
    """每笔平均盈亏（= 逐笔期望值）；空样本返回 None。"""
    vals = [_num(p) for p in (pnls or [])]
    vals = [v for v in vals if v is not None]
    return _r(_mean(vals), ND_MONEY)


def payoff_ratio(avg_win, avg_loss):
    """`平均盈利 / |平均亏损|`（fugazi：count-agnostic、按幅度加权）。

    0 盈利或 0 亏损（任一侧缺失）→ None —— 不是 0、也不是 inf。
    """
    w = _num(avg_win)
    l = _num(avg_loss)
    if w is None or l is None or abs(l) <= _EPS:
        return None
    return _r(w / abs(l), ND_RATIO)


def profit_factor(gross_profit, gross_loss):
    """`Σ 盈利 / |Σ 亏损|`；**没有亏损笔时返回 None**（fugazi 口径）。

    返回 inf 会直接让 `json.dumps(..., allow_nan=False)` 抛错，因此宁可给 None。
    """
    gp = _num(gross_profit)
    gl = _num(gross_loss)
    if gp is None or gl is None or abs(gl) <= _EPS:
        return None
    return _r(gp / abs(gl), ND_RATIO)


def kelly_fraction(p, b):
    """Kelly 最优下注比例 `p − (1 − p)/b`（fugazi 同式，允许为负 = 无优势）。

    `b` 非正或无定义时返回 None（`b ≤ 0` 会让公式失去意义）。
    """
    pp = _num(p)
    bb = _num(b)
    if pp is None or bb is None or bb <= 0:
        return None
    return _r(pp - (1.0 - pp) / bb, ND_RATIO)


def total_return(values):
    """区间总收益（小数）：`(末值 − 首值) / 首值`；点不足 2 个或首值 ≤ 0 → None。"""
    vals = []
    for v in (values or []):
        x = _num(v)
        if x is not None:
            vals.append(x)
    if len(vals) < 2 or vals[0] <= _EPS:
        return None
    return (vals[-1] - vals[0]) / vals[0]


def annualized_return(total, n_returns, bars_per_year=BARS_PER_YEAR):
    """复合年化收益：`(1 + total) ** (ppy / n) - 1`。

    样本太短时外推会爆炸（2 个 bar 的 +100% 年化 ≈ 1e30），超过 :data:`MAX_ANNUALIZED`
    直接返回 None —— 这种数字没有意义，给出来只会被误读。
    """
    t = _num(total)
    n = _num(n_returns)
    if t is None or n is None or n <= 0:
        return None
    if t <= -1.0:
        return -1.0
    years = float(n) / float(bars_per_year)
    if years <= 0:
        return None
    try:
        x = math.log1p(t) / years
    except (ValueError, OverflowError):
        return None
    if x > math.log(MAX_ANNUALIZED):
        return None
    try:
        return math.expm1(x)
    except (OverflowError, ValueError):
        return None


def mean_return(returns):
    """逐 bar 平均收益（自然单位）。"""
    return _mean([_num(r) for r in (returns or []) if _num(r) is not None])


def volatility(returns, bars_per_year=BARS_PER_YEAR):
    """年化波动率 = 样本标准差 × √ppy；样本不足或无波动 → None。"""
    sd = _stdev([_num(r) for r in (returns or []) if _num(r) is not None])
    if sd is None:
        return None
    return sd * math.sqrt(float(bars_per_year))


def sharpe(returns, bars_per_year=BARS_PER_YEAR):
    """Sharpe = mean(r) / stdev(r) × √ppy（样本标准差，无风险利率取 0）。"""
    vals = [_num(r) for r in (returns or [])]
    vals = [v for v in vals if v is not None]
    mu = _mean(vals)
    sd = _stdev(vals)
    if mu is None or sd is None or sd <= _EPS:
        return None
    return mu / sd * math.sqrt(float(bars_per_year))


def sortino(returns, bars_per_year=BARS_PER_YEAR):
    """Sortino = mean(r) / 下行标准差 × √ppy（MAR = 0，下行标准差用 n 分母）。

    所有 bar 都不亏（下行标准差为 0）时返回 None —— 此时比率为无穷大，没有意义。
    """
    vals = [_num(r) for r in (returns or [])]
    vals = [v for v in vals if v is not None]
    mu = _mean(vals)
    if mu is None or not vals:
        return None
    acc = sum(min(v, 0.0) ** 2 for v in vals)
    dd = math.sqrt(acc / float(len(vals)))
    if dd <= _EPS:
        return None
    return mu / dd * math.sqrt(float(bars_per_year))


def calmar(annualized, max_dd):
    """Calmar = 年化收益 / 最大回撤；回撤为 0（或缺失）时返回 None。"""
    a = _num(annualized)
    m = _num(max_dd)
    if a is None or m is None or m <= _EPS:
        return None
    return a / m


def positive_bars_ratio(returns):
    """正收益周期占比（周期口径的「胜率」，样本为 0 → None）。"""
    vals = [_num(r) for r in (returns or [])]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return len([v for v in vals if v > 0]) / float(len(vals))


def _chain_from_returns(rets):
    """链式净值（首点 = 1.0）：剔除出入金后的净值曲线；空输入返回空列表。

    生长因子 ≤ 0（权益归零）时记 0 —— 之后无法再恢复，不制造负净值或 inf。
    """
    if not rets:
        return []
    out = [1.0]
    for r in rets:
        growth = 1.0 + r
        out.append(out[-1] * growth if growth > 0 else 0.0)
    return out


def _period_from_returns(rets, values):
    """周期口径指标（内部装配器）：`rets` 与 `values` 必须同源。

    两处调用（:func:`period_metrics` 与 :func:`cache_flow_metrics`）共用它，
    保证「同一份中间量只构建一次」，也保证两条路径的数字口径永远一致。
    """
    bars = len(values or [])
    segs = drawdown_segments(values)
    total = total_return(values)
    ann = annualized_return(total, len(rets or []))
    mdd = max_drawdown(segs, bars)
    p = positive_bars_ratio(rets)
    ups = [r for r in (rets or []) if r > 0]
    downs = [r for r in (rets or []) if r < 0]
    b = None
    if ups and downs:
        avg_down = _mean(downs)
        if avg_down:
            b = _mean(ups) / abs(avg_down)
    return {
        "bars": bars,
        "returns": len(rets or []),
        "barsPerYear": BARS_PER_YEAR,
        "totalReturn": _r(total, ND_RATIO),
        "annualized": _r(ann, ND_RATIO),
        "annualizedTruncated": bool(total is not None and ann is None and total > 0),
        "volatility": _r(volatility(rets), ND_RATIO),
        "sharpe": _r(sharpe(rets), ND_RATIO),
        "sortino": _r(sortino(rets), ND_RATIO),
        "maxDrawdown": mdd,
        "maxDrawdownDuration": max_drawdown_duration(segs, bars),
        "timeInDrawdownRatio": time_in_drawdown_ratio(segs, bars),
        "ulcerIndex": ulcer_index(values),
        "calmar": _r(calmar(ann, mdd), ND_RATIO),
        "kellyFraction": kelly_fraction(p, b),
        "positiveBarsRatio": _r(p, ND_RATIO),
        "meanReturn": _r(mean_return(rets), ND_MONEY),
        "bestBar": _r(max(rets), ND_RATIO) if rets else None,
        "worstBar": _r(min(rets), ND_RATIO) if rets else None,
        "drawdownCount": len(segs),
        "note": PERIOD_NOTE,
    }


# --------------------------------------------------------------------------- #
# 六、逐笔口径汇总
# --------------------------------------------------------------------------- #

#: 逐笔样本不足的固定告警文案（%d = 样本数）
SAMPLE_WARNING = ("样本量不足（已完成交易 %d 笔 < %d 笔）：胜率/盈亏比不具统计意义，"
                  "仅作记录；minSampleForConfidence 给出可信所需的最小样本量")


def _max_consecutive(pnls, positive=True):
    """最长连续盈 / 亏笔数（fugazi：空样本返回 0）。"""
    best = cur = 0
    for p in (pnls or []):
        hit = (p > 0) if positive else (p < 0)
        cur = cur + 1 if hit else 0
        if cur > best:
            best = cur
    return int(best)


def trade_metrics(trips, equity=None, capital=None):
    """逐笔口径指标（一笔 = 一次完整往返；样本为 0 的比率返回 None）。

    参数
    ----
    trips : list[dict]
        :func:`round_trips` 的结果（缺 pnl 的条目按 0 计入并在 warnings 说明）。
    equity : list[dict] | None
        可选：窗口内的权益曲线。给了就算「权益变动 − 委托单已实现盈亏」的差额 ——
        差额大意味着有未记录的出入金 / 未平仓 / 未落库成交，属于必须让用户看见的信号。
    capital : float | None
        可选本金，用于 `returnOnCapital`（百分数）。

    返回字段（money 6 位 / 比率 4 位 / 百分数 3 位）
    ----------------------------------------------
    ``trades / wins / losses / flats / winRate / avgWin / avgLoss / payoffRatio /
    profitFactor / expectancy / largestWin / largestLoss / maxConsecutiveWins /
    maxConsecutiveLosses / avgBarsHeld / totalPnl / totalFee / mae / mfe`` 等。
    其中 `mae` / `mfe` 恒为 None（见 :data:`MAE_MFE_WARNING`：委托单没有盘中路径）。
    `winRate` 的分母是**全部**已完成交易（含盈亏为 0 的平局，与 fugazi 一致）。
    """
    items = [t for t in trips if isinstance(t, dict)] if isinstance(trips, (list, tuple)) else []
    pnls, fees, rets, bars = [], [], [], []
    incomplete = 0
    for t in items:
        pnl = _num(t.get("pnl"))
        if pnl is None:
            incomplete += 1
            pnl = 0.0
        pnls.append(pnl)
        fee = _num(t.get("fee"))
        fees.append(fee if fee is not None else 0.0)
        r = _num(t.get("returnPct"))
        if r is not None:
            rets.append(r)
        b = _num(t.get("bars"))
        if b is not None:
            bars.append(b)

    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    total_pnl = sum(pnls)
    total_fee = sum(fees)
    avg_win = _mean(wins)
    avg_loss = _mean(losses)

    out = {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats": n - len(wins) - len(losses),
        "winRate": _r(len(wins) / float(n), ND_RATIO) if n else None,
        "avgWin": _r(avg_win, ND_MONEY),
        "avgLoss": _r(avg_loss, ND_MONEY),
        "payoffRatio": payoff_ratio(avg_win, avg_loss),
        "profitFactor": profit_factor(gross_profit, gross_loss),
        "expectancy": expectancy(pnls),
        "largestWin": _r(max(wins), ND_MONEY) if wins else None,
        "largestLoss": _r(min(losses), ND_MONEY) if losses else None,
        "maxConsecutiveWins": _max_consecutive(pnls, True),
        "maxConsecutiveLosses": _max_consecutive(pnls, False),
        "avgBarsHeld": _r(_mean(bars), ND_RATIO) if bars else None,
        "grossProfit": _r(gross_profit, ND_MONEY),
        "grossLoss": _r(gross_loss, ND_MONEY),
        "totalPnl": _r(total_pnl, ND_MONEY),
        "totalFee": _r(total_fee, ND_MONEY),
        "avgReturnPct": _r(_mean(rets), ND_PCT) if rets else None,
        # MAE/MFE 需要盘中价格路径；没有就如实给 None（不用收盘价冒充）
        "mae": None,
        "mfe": None,
        "minSampleForConfidence": MIN_SAMPLE,
        "sampleEnough": bool(n and n >= MIN_SAMPLE),
        "sampleWarning": (SAMPLE_WARNING % (n, MIN_SAMPLE)) if (n and n < MIN_SAMPLE) else None,
        "unit": "金额为元、比率为自然单位（0.15 = 15%）、returnPct 为百分数",
        "note": TRADE_NOTE,
    }

    cap = _num(capital)
    out["capital"] = _r(cap, ND_MONEY) if cap is not None else None
    out["returnOnCapital"] = _r(total_pnl / cap * 100.0, ND_PCT) if (cap and cap > 0) else None

    pts = _norm_equity(equity)
    if pts:
        change = pts[-1]["v"] - pts[0]["v"]
        out["equityChange"] = _r(change, ND_MONEY)
        out["pnlGap"] = _r(change - total_pnl, ND_MONEY)
    else:
        out["equityChange"] = None
        out["pnlGap"] = None

    warnings = []
    if n == 0:
        warnings.append("窗口内没有已完成交易（只有开仓未平仓的腿不计入），逐笔指标不可用")
    if out["sampleWarning"]:
        warnings.append(out["sampleWarning"])
    if incomplete:
        warnings.append("有 %d 笔交易缺少 pnl 字段，已按 0 计入（数据不完整，指标可能失真）" % incomplete)
    if n and not bars:
        warnings.append("委托单没有 bars 字段，持仓周期不可用，avgBarsHeld 返回 null")
    if out["pnlGap"] is not None and abs(out["pnlGap"]) > max(1.0, abs(total_pnl) * 0.05):
        warnings.append("委托单已实现盈亏合计与权益变动不一致（差额 %.4f）：可能存在未记录的出入金、"
                        "未平仓持仓或未落库成交，逐笔与周期口径不能互相校验" % out["pnlGap"])
    warnings.append(MAE_MFE_WARNING)
    out["warnings"] = warnings
    return out


def period_metrics(equity):
    """周期口径指标（按权益曲线逐 bar；无权益数据时全部为 None 且不抛异常）。

    逐 bar 收益率由 :func:`per_bar_returns` 构建**一次**后交给各指标；
    权益点若自带 `flow` / `netFlow` 字段，会自动按时间加权剔除 —— 此时**净值类与回撤类
    指标也改用剔除后的链式净值**（否则收益剔了、回撤没剔，两套口径会互相矛盾），
    并在 `flowNote` 注明。年化系数固定 :data:`BARS_PER_YEAR`（逐交易日假设），Sharpe × √252。
    """
    pts = _norm_equity(equity)
    values = [p["v"] for p in pts]
    flows = [p["flow"] for p in pts]
    has_flow = any(abs(f) > _EPS for f in flows)
    rets = per_bar_returns(values, flows if has_flow else None)
    curve_values = _chain_from_returns(rets) if has_flow else values
    out = _period_from_returns(rets, curve_values)
    out["flowAdjusted"] = bool(has_flow)
    out["flowNote"] = "已按时间加权剔除出入金" if has_flow else "未检测到出入金"
    if has_flow:
        out["rawTotalReturn"] = _r(total_return(values), ND_RATIO)
    return out


def drawdown_detail(equity):
    """回撤明细：分段（前高→谷底→收复/期末）+ 深度族 + 时间族指标。

    深度族（`maxDrawdown` / `averageDrawdown` / `ulcerIndex` / `currentDrawdown`）
    与时间族（`maxDrawdownDuration` / `averageDrawdownDuration` / `timeInDrawdownRatio`）
    分开给：深度大 ≠ 水下久，两者必须同时看。
    """
    pts = _norm_equity(equity)
    values = [p["v"] for p in pts]
    segs = drawdown_segments(values)
    bars = len(values)
    enriched = []
    for s in segs:
        item = dict(s)
        for label in ("peak", "trough", "recovery"):
            idx = item.get(label + "Index")
            if idx is None or not (0 <= idx < bars):
                continue
            item[label + "Ts"] = pts[idx]["t"]
            item[label + "Date"] = _iso(pts[idx]["t"])
        if item.get("peakTs") is not None and item.get("recoveryTs") is not None:
            item["durationDays"] = _r((item["recoveryTs"] - item["peakTs"]) / 86400000.0, 2)
        enriched.append(item)

    deepest = None
    for s in enriched:
        if s.get("depth") is None:
            continue
        if deepest is None or s["depth"] > deepest["depth"]:
            deepest = s
    depths = [s["depth"] for s in enriched if s.get("depth") is not None]
    durs = [s["durationBars"] for s in enriched]
    cur_dd = None
    if bars:
        peak = max(values)
        if peak > _EPS:
            cur_dd = _r(max(0.0, (peak - values[-1]) / peak), ND_RATIO)
    return {
        "bars": bars,
        "segments": enriched,
        "drawdownCount": len(enriched),
        "maxDrawdown": max_drawdown(enriched, bars),
        "maxDrawdownDuration": max_drawdown_duration(enriched, bars),
        "averageDrawdown": _r(_mean(depths), ND_RATIO) if depths else None,
        "averageDrawdownDuration": _r(_mean(durs), ND_RATIO) if durs else None,
        "timeInDrawdownRatio": time_in_drawdown_ratio(enriched, bars),
        "ulcerIndex": ulcer_index(values),
        "currentDrawdown": cur_dd,
        "maxDrawdownSegment": deepest,
        "first": _r(values[0], ND_MONEY) if bars else None,
        "last": _r(values[-1], ND_MONEY) if bars else None,
        "unit": "回撤类字段都是正数幅度（0.2 = 回撤 20%），持续期单位为 bar",
        "note": PERIOD_NOTE,
    }


# --------------------------------------------------------------------------- #
# 七、AI 记录分组（复用 advisor 的档位口径）
# --------------------------------------------------------------------------- #

def _flatten_records(records):
    """把「记录列表 / 单条记录 / 逐只明细列表」摊平成 `(记录数, 明细列表)`。"""
    if records is None:
        return 0, []
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, (list, tuple)):
        return 0, []
    recs, items = 0, []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rows = rec.get("rows")
        if isinstance(rows, (list, tuple)):
            recs += 1
            items.extend([r for r in rows if isinstance(r, dict)])
        elif any(k in rec for k in ("action", "verdict", "code")):
            recs += 1
            items.append(rec)              # 直接喂逐只明细也允许
        else:
            recs += 1                      # 记录存在但没有明细
    return recs, items


def _grade_item(item):
    """给一条复盘明细定档位与命中（**复用 advisor 的方向口径**）。

    判定优先级：`verdict`（advisor.review 的 hit/miss，最短已到期窗口）→
    `sinceReturn` 推导（退而求其次，口径不同，用 basis 标注）→ 不判定。
    `pending`（未走满判定窗口）/ `nodata` 一律**不**用 sinceReturn 补判 —— 那会把
    「还没到期」当成「已判定」。
    """
    act = _text(item.get("action")).lower()
    verdict = _text(item.get("verdict")).lower()
    vr = _num(item.get("verdictReturn"))
    sr = _num(item.get("sinceReturn"))
    direction = A.REVIEW_DIRECTION.get(act)
    neutral = act in A.REVIEW_NEUTRAL
    hit, basis, ret = None, "ungraded", None
    if neutral:
        basis, ret = "neutral", sr
    elif direction is None:
        basis, ret = ("unknown-action" if act else "no-action"), sr
    elif verdict in ("hit", "miss") and vr is not None:
        hit, basis, ret = (verdict == "hit"), "verdict", vr
    elif verdict in ("pending", "nodata"):
        basis, ret = verdict, None
    elif sr is not None:
        hit = (sr > 0) if direction > 0 else (sr < 0)
        basis, ret = "sinceReturn", sr
    return {"action": act, "direction": direction, "hit": hit, "ret": ret,
            "basis": basis, "verdict": verdict or None,
            "weight": _num(item.get("weight")),
            "contribution": _num(item.get("contribution"))}


def _group_view(key, label, members, neutral=False, actions=None):
    """一组（档位 / 看多组 / 看空组）的命中率与平均收益；样本过少只标注不给排名。

    命中率只用**已判定**的成员（`graded`）；平均收益用所有带收益的成员 ——
    中性档位（hold/watch）与未到期的 pending 不参与命中率，但它们**实际涨跌**仍要记录
    （这正是 advisor.review「只记录实际涨跌」的意思）。
    """
    graded = [g for g in members if g["hit"] is not None]
    n = len(graded)
    hits = len([g for g in graded if g["hit"]])
    rets = [g["ret"] for g in members if g["ret"] is not None]
    ups = [r for r in rets if r > 0]
    downs = [r for r in rets if r < 0]
    view = {
        "key": key, "label": label, "actions": list(actions or []),
        "count": len(members), "graded": n,
        "hits": hits, "misses": n - hits,
        "hitRate": _r(hits / float(n), ND_RATIO) if n else None,
        "avgReturn": _r(_mean(rets), ND_PCT) if rets else None,
        "avgWinReturn": _r(_mean(ups), ND_PCT) if ups else None,
        "avgLossReturn": _r(_mean(downs), ND_PCT) if downs else None,
        "smallSample": n < MIN_GROUP_SAMPLE,
        "rankable": bool(n >= MIN_GROUP_SAMPLE and not neutral),
    }
    if neutral:
        view["note"] = "中性档位不计入命中率（与 core.advisor.review 口径一致），只记录实际涨跌"
    elif n == 0:
        view["note"] = "样本为 0：给不出命中率（不是「命中率 0%」）"
    elif view["smallSample"]:
        view["note"] = "样本过少（已判定 n=%d < %d）：命中率仅作记录，不参与排名" % (n, MIN_GROUP_SAMPLE)
    else:
        view["note"] = None
    return view


def _strategy_breakdown(items, grades):
    """按策略（K线标记 strategy）分组的命中率（**代理口径**）。

    标记是「某根K线上该策略的方向」，这里用该标的的复盘收益近似它的前瞻收益：
    方向一致记命中。它是粗口径，因此在返回值里明确写 `basis="proxy"`，
    且样本 < 5 的分组同样只标注不排名。
    """
    buckets = {}
    for item, g in zip(items, grades):
        adv = item.get("advisor")
        marks = adv.get("marks") if isinstance(adv, dict) else None
        if not isinstance(marks, (list, tuple)):
            marks = item.get("marks")
        if not isinstance(marks, (list, tuple)):
            continue
        for mk in marks:
            if not isinstance(mk, dict):
                continue
            name = _text(mk.get("strategy")) or _text(mk.get("strategyName")) or "unknown"
            b = buckets.setdefault(name, {"marks": 0, "graded": 0, "hits": 0, "rets": []})
            b["marks"] += 1
            d = _num(mk.get("dir"))
            if d is None or abs(d) < _EPS or g["ret"] is None:
                continue
            hit = (g["ret"] > 0) if d > 0 else (g["ret"] < 0)
            b["graded"] += 1
            b["hits"] += 1 if hit else 0
            b["rets"].append(g["ret"])
    out = {}
    for name, b in buckets.items():
        n = b["graded"]
        out[name] = {
            "strategy": name, "marks": b["marks"], "graded": n, "hits": b["hits"],
            "hitRate": _r(b["hits"] / float(n), ND_RATIO) if n else None,
            "avgReturn": _r(_mean(b["rets"]), ND_PCT) if b["rets"] else None,
            "smallSample": n < MIN_GROUP_SAMPLE,
            "rankable": bool(n >= MIN_GROUP_SAMPLE),
            "basis": "proxy",
            "note": ("代理口径：用该标的的复盘收益近似该策略标记的前瞻收益；样本过少"
                     "（n=%d < %d），不参与排名" % (n, MIN_GROUP_SAMPLE)) if n < MIN_GROUP_SAMPLE
                    else "代理口径：用该标的的复盘收益近似该策略标记的前瞻收益",
        }
    return out


def advisor_breakdown(records):
    """按 AI 档位 / 看多组看空组 / 策略分组统计命中率与平均收益。

    输入可以是 `store.list_advisor_runs()` 的记录列表（带或不带 `rows`）、
    单条记录，或 `core.advisor.review()` 结果里的 `rows`（带 verdict / sinceReturn）。
    7 个档位的键**始终给全**（前端不会读到 undefined），另加 `unknown` 桶兜底。
    """
    recs, items = _flatten_records(records)
    grades = [_grade_item(it) for it in items]

    by_action = {}
    for act, label in A.ACTION_LABEL.items():
        members = [g for g in grades if g["action"] == act]
        by_action[act] = _group_view(act, label, members, neutral=act in A.REVIEW_NEUTRAL)
    unknown = [g for g in grades if g["action"] not in A.ACTION_LABEL]
    if unknown:
        by_action["unknown"] = _group_view("unknown", "未知档位", unknown)

    bull = [g for g in grades if (g["direction"] or 0) > 0]
    bear = [g for g in grades if (g["direction"] or 0) < 0]
    neutral_items = [g for g in grades if g["action"] in A.REVIEW_NEUTRAL]
    by_group = {
        "bull": _group_view("bull", "看多组", bull, actions=("buy", "add")),
        "bear": _group_view("bear", "看空组", bear, actions=("reduce", "sell", "avoid")),
        "neutral": _group_view("neutral", "中性组", neutral_items, neutral=True,
                               actions=tuple(A.REVIEW_NEUTRAL)),
    }
    by_strategy = _strategy_breakdown(items, grades)
    rankable = [v for v in list(by_action.values()) + [by_group["bull"], by_group["bear"]]
                if v.get("rankable") and v.get("hitRate") is not None]
    rankable.sort(key=lambda v: (-v["hitRate"], -v["graded"]))

    by_basis = {}
    by_verdict = {}
    for g in grades:
        by_basis[g["basis"]] = by_basis.get(g["basis"], 0) + 1
        key = g["verdict"] or "none"
        by_verdict[key] = by_verdict.get(key, 0) + 1
    graded = len([g for g in grades if g["hit"] is not None])

    warnings = []
    if items and graded == 0:
        warnings.append("记录里没有可用于命中判定的复盘结果（命中判定需要行情数据，即 "
                        "core.advisor.review 的 verdict / verdictReturn，或至少 sinceReturn）："
                        "本次只给出档位与策略分布，命中率一律为 null")
    if by_basis.get("sinceReturn"):
        warnings.append("有 %d 条命中的收益口径是 sinceReturn（自保存以来收益），并非 "
                        "advisor.review 的「最短已到期窗口」口径，两者不能直接比较（byBasis 已标注）"
                        % by_basis["sinceReturn"])
    if by_verdict.get("pending"):
        warnings.append("有 %d 条尚未走满判定窗口（pending），一律按未判定处理、不参与命中率"
                        % by_verdict["pending"])

    return {
        "ok": bool(recs),
        "records": recs,
        "items": len(items),
        "graded": graded,
        "ungraded": len(items) - graded,
        "byAction": by_action,
        "byGroup": by_group,
        "byStrategy": by_strategy,
        "bullHitRate": by_group["bull"]["hitRate"],
        "bearHitRate": by_group["bear"]["hitRate"],
        "ranking": [{"key": v["key"], "label": v["label"], "hitRate": v["hitRate"],
                     "graded": v["graded"]} for v in rankable],
        "byBasis": by_basis,
        "byVerdict": by_verdict,
        "minGroupSample": MIN_GROUP_SAMPLE,
        "note": GROUP_NOTE,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 八、出入金：时间加权处理
# --------------------------------------------------------------------------- #

def _flows_for_points(pts, flows):
    """把多种形态的 flows 归一成与 pts 等长的逐 bar 净流入，返回 (flows, 命中笔数, 未匹配)。"""
    n = len(pts)
    out = [0.0] * n
    used, unmatched = 0, []
    if flows is None or n == 0:
        return out, used, unmatched
    ts_index = {}
    for i, p in enumerate(pts):
        if p["t"] is not None:
            ts_index.setdefault(p["t"], i)
    if isinstance(flows, dict):
        for key, val in flows.items():
            amt, ts = _num(val), _ms(key)
            if amt is None or ts is None or ts not in ts_index:
                unmatched.append({"ts": ts, "amount": amt})
                continue
            out[ts_index[ts]] += amt
            used += 1
        return out, used, unmatched
    if not isinstance(flows, (list, tuple)):
        return out, used, [{"ts": None, "amount": None, "note": "flows 形态无法识别，已忽略"}]
    if flows and all(not isinstance(f, dict) for f in flows):
        nums = [_num(f) for f in flows]
        if len(nums) == n:
            for i, v in enumerate(nums):
                if v:
                    out[i] = v
                    used += 1
            return out, used, unmatched
        return out, 0, [{"ts": None, "amount": None,
                         "note": "flows 长度 %d ≠ 权益点数 %d，已整体忽略" % (len(nums), n)}]
    pending = []
    for f in flows:
        if not isinstance(f, dict):
            unmatched.append({"ts": None, "amount": None, "note": "flow 不是 dict，已忽略"})
            continue
        amt = None
        for key in ("amount", "value", "flow", "netFlow", "cash"):
            amt = _num(f.get(key))
            if amt is not None:
                break
        ts = _ms(f.get("ts"))
        if ts is None:
            ts = _ms(f.get("t"))
        if amt is None:
            unmatched.append({"ts": ts, "amount": None, "note": "缺少金额"})
            continue
        if ts is not None and ts in ts_index:
            out[ts_index[ts]] += amt
            used += 1
        elif ts is None:
            pending.append(amt)          # 没有时间戳 → 按顺序贴到第 1..k 期
        else:
            unmatched.append({"ts": ts, "amount": amt, "note": "时间戳不在权益曲线上"})
    for k, amt in enumerate(pending):
        idx = k + 1
        if idx >= n:
            unmatched.append({"ts": None, "amount": amt, "note": "无时间戳的 flow 超出曲线长度"})
            continue
        out[idx] += amt
        used += 1
    return out, used, unmatched


def cache_flow_metrics(equity, flows=None):
    """出入金的时间加权处理：产出「剔除出入金」的净值与周期口径指标。

    口径（fugazi *Closed system* 一段指定，二选一后固定用前者，见 :data:`FLOW_FORMULA`）::

        r_i = (E_i - F_i) / E_{i-1} - 1        # F_i 归属**期末**

    `flows` 支持多种形态：``{"<毫秒时间戳>": 金额}``、``[{"ts":…, "amount":…}]``、
    与权益点等长的纯数字列表；权益点自带的 `flow` / `netFlow` 字段会被一起计入。
    入金为正、出金为负。

    返回 ``adjusted``（链式净值，首点 1.0；没有出入金时它就等于「用原始收益链式累乘」的净值）、
    `totalReturn`（剔除后）、`rawReturn`（含出入金的原始口径）、`difference`、
    `period`（剔除后的周期指标）、`rawPeriod`（原始口径，便于对照）、`unmatchedFlows` 等。
    没检测到资金流时 `detected=False`，`totalReturn == rawReturn`，
    并在 note 里写明「未检测到 ≠ 没有」：本模块只看得到显式记录的资金流。
    """
    pts = _norm_equity(equity)
    values = [p["v"] for p in pts]
    given, used, unmatched = _flows_for_points(pts, flows)
    combined = [(p["flow"] or 0.0) + given[i] for i, p in enumerate(pts)]
    detected = any(abs(f) > _EPS for f in combined)
    rets = per_bar_returns(values, combined if detected else None)
    raw_rets = per_bar_returns(values)

    adj = _chain_from_returns(rets)
    adjusted = [{"t": pts[i]["t"], "v": _r(adj[i], ND_MONEY)} for i in range(len(adj))]

    raw_total = total_return(values)
    period = _period_from_returns(rets, adj)
    raw_period = _period_from_returns(raw_rets, values)
    inflow = sum(f for f in combined if f > 0)
    outflow = sum(f for f in combined if f < 0)
    adj_total = total_return(adj)
    applied = len([f for f in combined if abs(f) > _EPS])
    return {
        "ok": bool(pts),
        "formula": FLOW_FORMULA,
        "detected": bool(detected),
        "flowCount": applied,
        "flowsUsed": used,
        "flowsTotal": _r(inflow + outflow, ND_MONEY),
        "inflowTotal": _r(inflow, ND_MONEY),
        "outflowTotal": _r(outflow, ND_MONEY),
        "rawReturn": _r(raw_total, ND_RATIO),
        "totalReturn": _r(adj_total, ND_RATIO),
        "difference": _r((raw_total - adj_total), ND_RATIO) if (raw_total is not None and adj_total is not None) else None,
        "adjusted": adjusted,
        "returns": [_r(r, ND_MONEY) for r in rets],
        "rawReturns": [_r(r, ND_MONEY) for r in raw_rets],
        "period": period,
        "rawPeriod": raw_period,
        "unmatchedFlows": unmatched,
        "note": ("已按时间加权剔除出入金（%d 期有净流入：入金 %.4f / 出金 %.4f）；"
                 "adjusted 为链式净值（首点 = 1.0），%s"
                 % (applied, inflow, outflow, FLOW_FORMULA)) if detected else
                ("未检测到出入金：按封闭系统原样统计。注意「未检测到」不等于「没有」——"
                 "本模块只识别显式登记的资金流"),
    }


# --------------------------------------------------------------------------- #
# 九、汇总入口
# --------------------------------------------------------------------------- #

_SOURCE_BUCKETS = ("ai", "manual", "scheduled")


def _source_bucket(src):
    """委托单来源 → 统计分桶（ai / manual / scheduled / other）。"""
    s = _text(src).lower()
    if s in ("ai", "advice", "advisor", "model", "strategy", "scan"):
        return "ai"
    if s in ("manual", "user", "hand", "human", "close"):
        return "manual"
    if s in ("scheduled", "schedule", "cron", "timer", "auto", "automation"):
        return "scheduled"
    return "other"


def _source_profile(trips):
    """某个来源的逐笔概览（样本为 0 的比率一律 None）。"""
    pnls, rets = [], []
    for t in (trips if isinstance(trips, (list, tuple)) else []):
        if not isinstance(t, dict):
            continue
        pnl = _num(t.get("pnl"))
        pnls.append(pnl if pnl is not None else 0.0)
        r = _num(t.get("returnPct"))
        if r is not None:
            rets.append(r)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "winRate": _r(len(wins) / float(n), ND_RATIO) if n else None,
        "totalPnl": _r(sum(pnls), ND_MONEY),
        "avgReturnPct": _r(_mean(rets), ND_PCT) if rets else None,
        "payoffRatio": payoff_ratio(_mean(wins), _mean(losses)),
        "profitFactor": profit_factor(sum(wins), sum(losses)),
        "smallSample": n < MIN_SAMPLE,
    }


def _load_equity(store, market, since, warnings):
    """读取窗口内权益曲线（多账户合并去重；保留窗口前最后一个点做基准）。"""
    cfg = _safe(lambda: store.get_trade_config(), None)
    cfg = cfg if isinstance(cfg, dict) else {}
    mkt = _text(market) or _text(cfg.get("market")) or "cn"
    accounts = []
    for mode in (_text(cfg.get("mode")) or "paper", "paper", "dryrun"):
        aid = _safe(lambda mode=mode: T.account_id(mkt, mode))
        if aid and aid not in accounts:
            accounts.append(aid)
    rows = []
    for aid in accounts:
        got = _safe(lambda aid=aid: store.list_trade_equity(aid, limit=EQUITY_LIMIT), None)
        if got is None:
            continue
        for p in (got if isinstance(got, (list, tuple)) else []):
            if isinstance(p, dict):
                rows.append(p)
    if not rows:
        warnings.append("窗口内没有权益曲线数据（trade_equity），周期口径与回撤指标不可用")
        return [], mkt, cfg
    dedup = {}
    for p in rows:
        dedup[_ms(p.get("ts")) or 0] = p
    curve = [dedup[k] for k in sorted(dedup)]
    if since:
        kept = [p for p in curve if (_ms(p.get("ts")) or 0) >= since]
        before = [p for p in curve if (_ms(p.get("ts")) or 0) < since]
        if before:
            kept = [before[-1]] + kept        # 窗口前最后一个点：否则首个收益会被算成「从 0 开始」
        curve = kept
    return curve, mkt, cfg


def _flows_from_cash_orders(orders):
    """从「出入金/资金调拨」类委托里提取资金流（尽力而为，识别不到就返回空）。

    这类委托不是交易腿，因此 :func:`round_trips_detail` 会跳过它们；
    但它们的金额恰恰是权益曲线上的外部流入 / 流出，必须拿去做时间加权剔除。
    """
    flows = []
    for o in (orders or []):
        if not isinstance(o, dict) or not _is_cash_order(o):
            continue
        amt = None
        for key in ("amount", "cash", "netAmount", "net_amount"):
            amt = _num(o.get(key))
            if amt is not None:
                break
        if amt is None:
            q, p = _order_qty(o), _order_price(o)
            if q is not None and p is not None:
                amt = q * p
        if not amt:
            continue
        word = (_text(o.get("side")) + " " + _text(o.get("intent")) + " "
                + _text(o.get("action"))).lower()
        if "withdraw" in word or "out" in word or "transfer_out" in word:
            amt = -abs(amt)
        else:
            amt = abs(amt)
        flows.append({"ts": _order_ts(o), "amount": amt})
    return flows


def review_report(store, market=None, days=None):
    """复盘汇总入口（只读、零网络；任何读取失败都降级为 warnings，不抛异常）。

    返回::

        {"ok", "window": {"from","to","days"},
         "metrics": {逐笔口径},      # trade_metrics
         "period":  {周期口径},      # period_metrics（检测到出入金时换成剔除后的口径）
         "drawdown": {...},          # drawdown_detail
         "byAdvisor": {...},         # advisor_breakdown
         "bySource": {"ai","manual","scheduled", ...},
         "note": METRIC_NOTE, "warnings": [...]}

    `ok=False` 只在「存储层不可用」或「完全读不到任何数据」时出现；数据存在但样本不足
    一律 `ok=True` + warnings（前端要能看到「读到了，只是不够」）。
    """
    now = int(time.time() * 1000)
    warnings = []

    span = _num(days)
    days_i = None
    if days is not None:
        if span is None:
            warnings.append("days=%r 不是数字，已按「全部时间」处理" % (days,))
        elif span <= 0:
            warnings.append("days=%r 不是正数，已按「全部时间」处理" % (days,))
        else:
            days_i = int(min(span, 3650))
    since = (now - days_i * 86400000) if days_i else None
    window = {"from": since, "to": now, "days": days_i,
              "fromDate": _iso(since) if since else None, "toDate": _iso(now)}
    by_source = {k: _source_profile([]) for k in _SOURCE_BUCKETS}

    if store is None:
        return {"ok": False, "window": window, "metrics": {}, "period": {}, "drawdown": {},
                "trips": [], "openLegs": [], "skipped": [], "byAdvisor": {"ok": False},
                "bySource": by_source, "note": METRIC_NOTE,
                "warnings": ["未提供存储层（store=None），无数据可复盘"]}

    # 1) 委托单 → 往返交易
    found = _safe(lambda: store.list_trade_orders(status="filled", limit=ORDER_LIMIT,
                                                 market=market), None)
    orders = []
    if found is None:
        warnings.append("读取委托单失败（trade_orders），逐笔口径指标不可用")
    else:
        rows = found.get("rows") if isinstance(found, dict) else found
        orders = [o for o in (rows if isinstance(rows, (list, tuple)) else [])
                  if isinstance(o, dict)]
    if since:
        orders = [o for o in orders if (_order_ts(o) or 0) >= since]
    detail = round_trips_detail(orders)
    trips = detail["trips"]

    # 2) 权益曲线（含出入金识别）
    equity, mkt, cfg = _load_equity(store, market, since, warnings)
    state = _safe(lambda: store.get_trade_state(T.account_id(mkt, _text(cfg.get("mode")) or "paper")),
                  None)
    capital = _num((state or {}).get("initial")) if isinstance(state, dict) else None
    if not capital:
        capital = _num(cfg.get("capital"))
    explicit_flows = _flows_from_cash_orders(orders)
    flows = cache_flow_metrics(equity, explicit_flows or None)

    metrics = trade_metrics(trips, equity=equity, capital=capital)
    if flows["detected"]:
        period = dict(flows["period"])
        period["flowNote"] = "已按时间加权剔除出入金"
        metrics["flowNote"] = ("已按时间加权剔除出入金（%d 期有净流入，共 %s）："
                               "周期口径与回撤均基于剔除后的链式净值" % (flows["flowCount"], flows["formula"]))
        metrics["flowRawReturn"] = flows["rawReturn"]
        warnings.append("检测到出入金 %d 期（净流入 %.4f）：已按时间加权剔除；"
                        "含出入金的原始区间收益为 %s，剔除后为 %s"
                        % (flows["flowCount"], flows["flowsTotal"] or 0.0,
                           flows["rawReturn"], flows["totalReturn"]))
        drawdown = drawdown_detail(flows["adjusted"])
        drawdown["onAdjustedCurve"] = True
    else:
        period = period_metrics(equity)
        metrics["flowNote"] = "未检测到出入金"
        metrics["flowRawReturn"] = None
        drawdown = drawdown_detail(equity)
        drawdown["onAdjustedCurve"] = False

    # 3) AI 记录分组
    runs = _safe(lambda: store.list_advisor_runs(limit=RECORD_LIMIT, market=market), None)
    records = []
    if runs is None:
        warnings.append("读取 AI 选股记录失败（advisor_runs），档位分组不可用")
    else:
        rows = runs.get("rows") if isinstance(runs, dict) else runs
        for run in (rows if isinstance(rows, (list, tuple)) else [])[:RECORD_LIMIT]:
            if not isinstance(run, dict):
                continue
            rid = run.get("id")
            items = _safe(lambda rid=rid: store.advisor_items(rid), None) if rid else None
            records.append({"id": rid, "createdAt": run.get("createdAt"),
                            "rows": items if isinstance(items, list) else []})
        if since:
            records = [r for r in records if (_ms(r.get("createdAt")) or 0) >= since]
    by_advisor = advisor_breakdown(records)

    # 4) 按来源分桶
    buckets = {k: [] for k in _SOURCE_BUCKETS}
    for t in trips:
        buckets.setdefault(_source_bucket(t.get("source")), []).append(t)
    by_source = {k: _source_profile(buckets.get(k) or []) for k in _SOURCE_BUCKETS}
    if buckets.get("other"):
        by_source["other"] = _source_profile(buckets["other"])

    # 5) 告警汇总（顺序固定，便于前端与测试比对）
    warnings.extend(metrics.get("warnings") or [])
    warnings.extend(by_advisor.get("warnings") or [])
    if detail["openLegs"]:
        warnings.append("有 %d 个开仓腿尚未平仓，未计入已完成交易（未实现盈亏不在逐笔指标里）"
                        % len(detail["openLegs"]))
    if detail["skipped"]:
        warnings.append("有 %d 笔成交无法参与配对（缺代码/数量/成交价或状态非已成交），"
                        "详见 skipped" % len(detail["skipped"]))
    if detail["cashOrders"]:
        warnings.append("检测到 %d 笔出入金/资金调拨类委托，已从交易配对中排除" % detail["cashOrders"])
    if not orders:
        warnings.append("窗口内没有已成交委托单，逐笔口径指标不可用")
    if flows["unmatchedFlows"]:
        warnings.append("有 %d 笔资金流未能对齐到权益曲线（时间戳不匹配），未参与时间加权"
                        % len(flows["unmatchedFlows"]))

    ok = bool(orders or equity or by_advisor.get("items"))
    return {
        "ok": ok,
        "market": mkt,
        "window": window,
        "metrics": metrics,
        "period": period,
        "drawdown": drawdown,
        "byAdvisor": by_advisor,
        "bySource": by_source,
        "flow": {"detected": flows["detected"], "flowCount": flows["flowCount"],
                 "flowsTotal": flows["flowsTotal"], "formula": flows["formula"],
                 "rawReturn": flows["rawReturn"], "totalReturn": flows["totalReturn"],
                 "difference": flows["difference"], "note": flows["note"]},
        "trips": trips,
        "openLegs": detail["openLegs"],
        "skipped": detail["skipped"],
        "orders": len(orders),
        "note": METRIC_NOTE,
        "warnings": warnings,
    }


# 便于外部（服务端 / 前端契约测试）确认「输出的确可以被严格 JSON 序列化」：
# 本模块所有比率都经 _r() 收口，分母为 0 一律 None，因此不会出现 inf / NaN。
def _assert_json_safe(obj):  # pragma: no cover - 仅作为自检工具暴露
    """自检工具：`json.dumps(obj, allow_nan=False)`，不通过就抛 ValueError。"""
    return json.dumps(obj, allow_nan=False, ensure_ascii=False)
