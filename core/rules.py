# -*- coding: utf-8 -*-
"""AlphaDesk · 交易规则引擎（core/rules.py）

把「下单与撮合必须遵守的规则」集中成一份纯函数口径，供模拟交易、点位引擎、买入扫描器
与前端提示共用。**集中化的理由**：涨跌幅、费用、最小申报单位、交易时段这几个数字一旦在
多个模块各写一份，必然出现「回测按 10% 判定、模拟成交按 20% 判定」这类互相矛盾的结果 ——
而交易规则错误是**静默**的：它不会报错，只会让收益算错、让策略在纸面上成立。

规则版本（必须显示给用户）
--------------------------
A股交易规则自 **2026-07-06** 起施行新版：沪深主板 ST/*ST 涨跌幅由 ±5% 放宽至 ±10%；
盘后固定价格交易由科创板 / 创业板扩展至全部 A股与 ETF。本模块据此实现，并且：

* 把版本与来源做成显式常量 :data:`RULES_VERSION` / :data:`RULES_SOURCES`（界面要展示，
  用户才知道引擎按哪一版在算）；
* 把易变项做成**可覆盖参数**（例如需要回到旧口径时把 ``stLimitRatio`` 改回 0.05），
  而不是硬编码一个数字散落在各处；
* 找不到权威来源的项**不写成硬规则**，在 :data:`UNVERIFIED` 里如实列出，宁缺勿造。

能力一览
--------
============================  ==================================================
:func:`board_of`              板块识别（主板 / 创业板 / 科创板 / 北交所 / 美股）
:func:`limit_of`              涨跌幅比例（按板块 + 风险警示 + 新股状态）
:func:`limit_prices`          涨跌停价（含「不足 0.01 元按 0.01 元」等边界补丁）
:func:`fee_of`                逐项费用（印花税 / 佣金含最低值 / 过户费 / 经手费）
:func:`session_of`            当前处于哪个交易时段（含 09:25–09:30 静默期）
:func:`is_tradable_now`       此刻能否申报
:func:`lot_of` / :func:`round_qty`  最小申报单位与整手取整
:func:`sellable_qty`          T+1 可卖数量（昨仓 − 今日已卖）
:func:`check_order`           下单前校验（时段 / 涨跌停 / 笼子 / 整手 / T+1 / 资金 / 持仓）
:func:`can_fill`              撮合可行性（涨停买不到、跌停卖不出、停牌不成交）
============================  ==================================================

三条最容易搞错、这里显式处理的规则
----------------------------------
1. **涨跌停板不是「价格可达即成交」**：涨停时同价买单按**时间优先**排队，无卖单则零成交；
   跌停反之。因此 :func:`can_fill` 在「成交价 == 涨停价且当日最高价未突破涨停」时判定
   买入不可成交 —— 把它做成「价格可取即成交」会凭空造出连板股的虚假收益。
2. **T+1 不能简化成「持仓当天不可卖」**：正确口径是「可卖 = 昨仓 − 今日已卖」，
   必须单独维护「今日买入」篮子，不能用总持仓量当可卖量。
3. **费用不是一个费率**：印花税**仅卖出单边** 0.05%，佣金双向且**不足 5 元按 5 元**计，
   过户费与经手费双向。把费用写成单一双边费率会系统性低估小资金策略的成本
   （最低 5 元佣金在小单上可能占成交额的 0.5% 以上）。

纯标准库实现，无网络、无第三方依赖；任何输入都不抛异常。
"""

from __future__ import annotations

import datetime
import math
import time

__all__ = [
    "RULES_VERSION", "RULES_SOURCES", "UNVERIFIED", "RULES_NOTE",
    "MARKET_CN", "MARKET_US", "BOARDS", "DEFAULT_FEES", "DEFAULT_RULE_PARAMS",
    "board_of", "limit_of", "limit_prices", "fee_of", "session_of",
    "is_tradable_now", "session_windows", "lot_of", "min_qty_of", "round_qty",
    "sellable_qty", "check_order", "can_fill", "stamp_tax_of", "rules_table",
]

MARKET_CN = "cn"
MARKET_US = "us"

RULES_VERSION = "A股交易规则（2026-07-06 新版口径）· 美股（T+1 结算）"

RULES_SOURCES = [
    "上交所交易规则（2026 年修订）· 涨跌幅 / 申报 / 时段 / 风险警示板",
    "http://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml",
    "证监会上海监管局：A股交易新规 2026-07-06 起施行（ST 涨跌幅放宽至 10%）",
    "http://www.csrc.gov.cn/shanghai/c105566/c7643909/content.shtml",
    "财政部/税务总局 2023 年第 39 号公告：证券交易印花税减半（1‰ → 0.05‰）",
    "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html",
    "证监会：沪深北交易所进一步降低证券交易经手费（沪深 0.00341% 双向）",
    "http://www.csrc.gov.cn/csrc/c100028/c7426794/content.shtml",
    "券商公示的证券交易收费标准（佣金 ≤3‰、不足 5 元按 5 元；过户费 0.01‰ 双向）",
    "https://www.stocke.com.cn/main/a/20250228/7643439.shtml",
]

#: 本轮调研中**未取得权威来源**、因此没有写成硬规则的项（界面与文档要如实告知）
UNVERIFIED = [
    "卖出资金「T+0 可用、T+1 可取」：属券商资金结算安排，未找到交易所/证监会条文，"
    "本模块只按「卖出所得资金立即可用于买入」处理并可配置。",
    "证管费 0.002%：仅券商/媒体口径，未取得监管原文。",
    "过户费降至 0.01‰ 的中国结算原始公告：仅券商公示口径。",
    "美股最小申报数量、盘前盘后时段：各家券商差异大，本模块只给可配默认值。",
    "风险警示股「首次买入需签署风险揭示书」：未取得条文链接，仅作提示。",
]

RULES_NOTE = (
    "规则口径：" + RULES_VERSION +
    "。涨跌停价按「前收盘价 ×(1±比例) 四舍五入到 0.01 元」，并处理两个边界补丁"
    "（涨跌停价与前收盘价之差不足 0.01 元时按 ±0.01 元；涨跌停价低于 0.01 元时按 0.01 元）。"
    "费用逐项计：印花税仅卖出单边 0.05%，佣金双向、不足 5 元按 5 元，过户费与经手费双向。"
    "T+1 可卖 = 昨仓 − 今日已卖，当日买入部分不可卖。涨跌停封板时买入/卖出分别不可成交。"
)

# --------------------------------------------------------------------------- #
# 参数表（全部可覆盖）
# --------------------------------------------------------------------------- #
#: 板块与涨跌幅。2026-07-06 起沪深主板风险警示股（ST/*ST）与主板同为 10%。
BOARDS = {
    "main": {"label": "主板", "limit": 0.10, "lot": 100, "minQty": 100, "board": "main"},
    "gem": {"label": "创业板", "limit": 0.20, "lot": 100, "minQty": 100, "board": "gem"},
    "star": {"label": "科创板", "limit": 0.20, "lot": 1, "minQty": 200, "board": "star"},
    "bse": {"label": "北交所", "limit": 0.30, "lot": 100, "minQty": 100, "board": "bse"},
    "us": {"label": "美股", "limit": None, "lot": 1, "minQty": 1, "board": "us"},
}

DEFAULT_RULE_PARAMS = {
    # 涨跌幅
    "stLimitRatio": 0.10,      # 风险警示股（ST/*ST）涨跌幅；旧口径为 0.05，可改回
    "newListingDays": 5,       # 主板/创业板/科创板新股上市后不设涨跌幅的交易日数
    "bseNewListingDays": 1,    # 北交所仅首日不设涨跌幅
    "delistingFirstDayUnlimited": True,   # 退市整理期首个交易日不设涨跌幅
    "delistingLimitRatio": 0.10,          # 退市整理期次日起 10%
    "priceCageRatio": 0.02,    # 连续竞价限价申报有效价格范围 ±2%（越界为废单）
    "priceCageTicks": 10,      # 笼子的第二口径：基准价 ±10 个最小变动单位
    "tick": 0.01,              # 最小变动单位
    "stBuyLimit": 500000,      # 风险警示股单日买入上限（股）
    # 时段（本地时间 = 北京时间 UTC+8）
    "tzOffsetHours": 8,
    "allowAfterHoursFixed": True,   # 2026-07-06 起全 A股 + ETF 均可盘后固定价格交易
    "auctionSilence": True,         # 09:25–09:30 不接受申报
    "closeAuctionNoCancel": True,   # 14:57–15:00 不可撤单
}

#: 费用默认值。A股按上面引用的费率；**美股默认值仅为可配置示例**（券商差异大，
#: 不是权威口径），使用前请按自己的券商改。
DEFAULT_FEES = {
    "cn": {
        "commissionRate": 0.00025,   # 佣金（万分之 2.5，≤3‰ 上限内）
        "commissionMin": 5.0,        # 不足 5 元按 5 元
        "stampRate": 0.0005,         # 印花税 0.05%（**仅卖出**）
        "transferRate": 0.00001,     # 过户费 0.001%（双向）
        "handlingRate": 0.0000341,   # 经手费 0.00341%（双向，沪深）
        "handlingRateBSE": 0.000125,  # 北交所经手费 0.0125%（双向）
    },
    "us": {
        "commissionRate": 0.0,
        "commissionMin": 0.0,
        "perShareFee": 0.005,        # 示例：按股计费
        "perShareMin": 1.0,
        "secFeeRate": 0.0000278,     # 卖出侧 SEC 规费（示例值）
        "tafRate": 0.000166,         # 卖出侧 TAF（示例值）
        "tafMax": 8.30,
        "stampRate": 0.0,
        "transferRate": 0.0,
        "handlingRate": 0.0,
    },
}

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _num(value):
    """宽松转 float；None / bool / 非有限值 / 无法解析一律 None（NaN 会污染后续比较）"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value)
    else:
        try:
            x = float(str(value).strip().replace(",", "").replace("%", ""))
        except (TypeError, ValueError):
            return None
    return x if math.isfinite(x) else None


def _int(value, dflt=0):
    x = _num(value)
    if x is None:
        return int(dflt)
    try:
        return int(x)
    except (OverflowError, ValueError):
        return int(dflt)


def _bool(value, dflt=False):
    if value is None:
        return bool(dflt)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on", "是", "开"):
        return True
    if text in ("0", "false", "no", "n", "off", "否", "关", ""):
        return False
    return bool(dflt)


def _market(value):
    return MARKET_US if str(value or "").strip().lower().startswith("us") else MARKET_CN


def _code(value):
    return str(value or "").strip().upper()


def _params(params=None):
    out = dict(DEFAULT_RULE_PARAMS)
    if isinstance(params, dict):
        for key in list(out):
            if key in params and params[key] is not None:
                if isinstance(out[key], bool):
                    out[key] = _bool(params[key], out[key])
                elif isinstance(out[key], int):
                    out[key] = _int(params[key], out[key])
                else:
                    v = _num(params[key])
                    if v is not None:
                        out[key] = v
    return out


# --------------------------------------------------------------------------- #
# 1. 板块与涨跌幅
# --------------------------------------------------------------------------- #
def board_of(code, market=MARKET_CN, name=None):
    """按代码前缀识别板块（并给出该板块的涨跌幅、整数单位、最小申报量）。

    A股代码前缀是有语义的，因此识别不需要任何外部数据源：
    ``600/601/603/605`` 沪主板、``000/001/002/003`` 深主板、
    ``300/301`` 创业板、``688/689`` 科创板、``43/83/87/88/920`` 北交所；
    其余无法识别的一律按**更严**的主板口径处理（宁可保守）。
    """
    text = _code(code)
    mkt = _market(market)
    if mkt == MARKET_US:
        base = dict(BOARDS["us"])
        base.update({"code": text, "market": MARKET_US, "riskWarning": False,
                     "name": str(name or "").strip(),
                     "note": "美股无个股涨跌幅限制（有全市场熔断），最小 1 股。"})
        return base
    board = "main"
    if text.startswith(("300", "301")):
        board = "gem"
    elif text.startswith(("688", "689")):
        board = "star"
    elif text.startswith(("43", "83", "87", "88", "920")):
        board = "bse"
    elif text.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        board = "main"
    spec = dict(BOARDS[board])
    label = str(name or "").strip().upper()
    spec.update({
        "code": text, "market": MARKET_CN, "board": board,
        # 风险警示：名称含 ST 即认定（数据源通常把 *ST 写在名称里）
        "riskWarning": ("ST" in label),
        "name": str(name or "").strip(),
        "note": "%s：涨跌幅 %.0f%%、%d 股整数倍%s" % (
            spec["label"], (spec["limit"] or 0) * 100, spec["minQty"],
            "（科创板超过 200 股后可按 1 股递增）" if board == "star" else ""),
    })
    return spec


def limit_of(code, market=MARKET_CN, name=None, params=None, meta=None):
    """该标的当日涨跌幅比例。

    ``None`` 表示**不设涨跌幅限制**（主板/创业板/科创板新股上市后前 5 个交易日、
    北交所首日、退市整理期首个交易日）。判断需要 ``meta`` 提供
    ``listedDays``（上市交易日数）或 ``delistingDays``（退市整理期第几个交易日）；
    拿不到这些信息时**按有限制处理**（保守方向），并在 note 里说明。
    """
    cfg = _params(params)
    spec = board_of(code, market, name)
    if spec.get("market") == MARKET_US:
        return {"ratio": None, "unlimited": True, "board": "us", "label": "美股",
                "note": "美股不设个股涨跌幅限制（以标普 500 熔断 7%/13%/20% 为全市场机制）。"}
    meta = meta if isinstance(meta, dict) else {}
    board = spec["board"]
    listed_days = _num(meta.get("listedDays"))
    delisting_days = _num(meta.get("delistingDays"))
    unlimited_days = cfg["bseNewListingDays"] if board == "bse" else cfg["newListingDays"]
    if listed_days is not None and listed_days <= unlimited_days:
        return {"ratio": None, "unlimited": True, "board": board, "label": spec["label"],
                "note": "上市后前 %d 个交易日不设涨跌幅限制（listedDays=%d）。"
                        % (int(unlimited_days), int(listed_days))}
    if delisting_days is not None:
        if delisting_days <= 1 and cfg["delistingFirstDayUnlimited"]:
            return {"ratio": None, "unlimited": True, "board": board, "label": spec["label"],
                    "note": "退市整理期首个交易日不设涨跌幅限制。"}
        return {"ratio": cfg["delistingLimitRatio"], "unlimited": False, "board": board,
                "label": "退市整理", "note": "退市整理期次日起涨跌幅 %.0f%%。"
                % (cfg["delistingLimitRatio"] * 100)}
    if spec["riskWarning"]:
        return {"ratio": cfg["stLimitRatio"], "unlimited": False, "board": board,
                "label": spec["label"] + "·风险警示",
                "note": "风险警示股涨跌幅 %.0f%%（2026-07-06 起与主板同为 10%%，旧口径 5%% 可经 "
                        "stLimitRatio 参数改回）。" % (cfg["stLimitRatio"] * 100)}
    return {"ratio": spec["limit"], "unlimited": False, "board": board, "label": spec["label"],
            "note": "%s 涨跌幅 %.0f%%；若为新股上市初期或退市整理期，实际比例可能不同"
                    "（需要提供 listedDays / delistingDays 才能精确判断）。"
                    % (spec["label"], (spec["limit"] or 0) * 100)}


def _patch_tick(prev_close, raw, cfg):
    """涨跌停价的两个边界补丁。

    补丁一：涨跌停价与前收盘价之差不足一个最小变动单位时，按 ±1 个 tick 处理。
    补丁二：计算结果低于一个 tick 时，以该 tick 价为准（A股为 0.01 元）。
    这两条是最容易漏的：低价股（如 0.06 元）按比例算出的涨跌停价会退化到 0 或不足 1 分。
    """
    tick = cfg["tick"]
    diff = abs(raw - prev_close)
    if diff < tick - _EPS:
        raw = prev_close + tick if raw >= prev_close else prev_close - tick
    if raw < tick:
        raw = tick
    return round(raw + _EPS * (1 if raw >= 0 else -1), 2)


def limit_prices(code, price=None, market=MARKET_CN, name=None, params=None,
                 prev_close=None, meta=None):
    """涨跌停价。``price`` 作基准价（**传前收盘价最准确**；只有最新价时是近似）。

    返回 ``{"up","down","ratio","unlimited","basis","board","note"}``；
    未提供基准价时返回 ``None`` 价并说明原因（不臆造数字）。
    """
    cfg = _params(params)
    spec = board_of(code, market, name)
    lim = limit_of(code, market, name, cfg, meta)
    base = _num(prev_close)
    if base is None:
        base = _num(price)
    out = {"code": _code(code), "market": spec.get("market"), "board": lim["board"],
           "label": lim["label"], "ratio": lim["ratio"], "unlimited": lim["unlimited"],
           "up": None, "down": None, "basis": None, "note": lim["note"]}
    if base is None or base <= 0:
        out["note"] = "缺少基准价（前收盘价），无法计算涨跌停价。" + lim["note"]
        return out
    out["basis"] = round(base, 4)
    if lim["unlimited"]:
        out["note"] = "不设涨跌幅限制，无涨跌停价。" + lim["note"]
        return out
    ratio = lim["ratio"]
    if ratio is None:
        out["note"] = "无法确定涨跌幅比例，未给出涨跌停价。" + lim["note"]
        return out
    out["up"] = _patch_tick(base, base * (1.0 + ratio), cfg)
    out["down"] = _patch_tick(base, base * (1.0 - ratio), cfg)
    if prev_close is None:
        out["note"] = ("基准价用的是**最新价**而非前收盘价，涨跌停价为近似值。"
                       + lim["note"])
    return out


# --------------------------------------------------------------------------- #
# 2. 费用
# --------------------------------------------------------------------------- #
def _fee_spec(market, fees):
    mkt = _market(market)
    spec = dict(DEFAULT_FEES[mkt])
    if isinstance(fees, dict):
        patch = fees.get(mkt) if mkt in fees else fees
        if isinstance(patch, dict):
            for key in list(spec):
                v = _num(patch.get(key))
                if v is not None:
                    spec[key] = v
    return mkt, spec


def fee_of(side, qty, price, market=MARKET_CN, board=None, fees=None):
    """逐项费用（返回明细，便于在界面上把「为什么这笔要 5.4 元」讲清楚）。

    A股：印花税**仅卖出单边** 0.05%；佣金双向、**不足 5 元按 5 元**；
    过户费 0.001% 双向；经手费 0.00341% 双向（北交所 0.0125%）。
    美股：按股佣金 + 卖出侧 SEC/TAF（默认值仅为示例，券商差异大）。
    """
    mkt, spec = _fee_spec(market, fees)
    qty = max(0, _int(qty, 0))
    px = _num(price) or 0.0
    notional = qty * px
    is_sell = str(side or "").strip().lower() in ("sell", "reduce", "close", "卖出", "减仓", "清仓")
    items = []
    if qty <= 0 or notional <= 0:
        return {"side": "sell" if is_sell else "buy", "qty": qty, "price": round(px, 4),
                "notional": 0.0, "items": [], "total": 0.0, "totalRate": 0.0,
                "note": "数量或价格无效，费用记为 0。"}

    if mkt == MARKET_CN:
        comm = notional * spec["commissionRate"]
        if comm < spec["commissionMin"]:
            # **必须把最低值写进 amount**：total 是 items 求和，只改局部变量会让
            # 「不足 5 元按 5 元」这条规则在合计里消失 —— 小单被系统性少收佣金，
            # 而 note 里却写着按 5 元收，自相矛盾（实测 1 万元买入曾只收 2.941 元）
            rate_amount = round(comm, 4)
            comm = round(spec["commissionMin"], 4)
            items.append({"name": "佣金", "rate": spec["commissionRate"], "amount": comm,
                          "rateAmount": rate_amount, "minApplied": True,
                          "note": "按费率算 %.4f 元，**不足 %.2f 元按 %.2f 元**收取"
                                  % (rate_amount, spec["commissionMin"], spec["commissionMin"])})
        else:
            items.append({"name": "佣金", "rate": spec["commissionRate"], "amount": round(comm, 4),
                          "minApplied": False, "note": "双向收取"})
        if is_sell:
            stamp = notional * spec["stampRate"]
            items.append({"name": "印花税", "rate": spec["stampRate"], "amount": round(stamp, 4),
                          "minApplied": False, "note": "**仅卖出单边**收取"})
        transfer = notional * spec["transferRate"]
        items.append({"name": "过户费", "rate": spec["transferRate"], "amount": round(transfer, 4),
                      "minApplied": False, "note": "双向收取"})
        handling_rate = spec["handlingRateBSE"] if str(board or "") == "bse" else spec["handlingRate"]
        handling = notional * handling_rate
        items.append({"name": "经手费", "rate": handling_rate, "amount": round(handling, 4),
                      "minApplied": False,
                      "note": "双向收取（北交所 0.0125%%）" if str(board or "") == "bse"
                              else "双向收取（沪深 0.00341%）"})
    else:
        comm = max(notional * spec["commissionRate"], qty * spec["perShareFee"])
        if spec["perShareMin"] and comm < spec["perShareMin"]:
            rate_amount = round(comm, 4)
            comm = round(spec["perShareMin"], 4)
            items.append({"name": "佣金（示例口径）", "rate": spec["perShareFee"],
                          "amount": comm, "rateAmount": rate_amount, "minApplied": True,
                          "note": "按股计费 %.4f 元，不足最低 %.2f 元按最低收"
                                  % (rate_amount, spec["perShareMin"])})
        else:
            items.append({"name": "佣金（示例口径）", "rate": spec["perShareFee"],
                          "amount": round(comm, 4), "minApplied": False,
                          "note": "默认值仅为示例，请按自己券商调整"})
        if is_sell:
            sec = notional * spec["secFeeRate"]
            taf = min(qty * spec["tafRate"], spec["tafMax"])
            items.append({"name": "SEC 规费（示例口径）", "rate": spec["secFeeRate"],
                          "amount": round(sec, 4), "minApplied": False, "note": "卖出侧"})
            items.append({"name": "TAF（示例口径）", "rate": spec["tafRate"],
                          "amount": round(taf, 4), "minApplied": False,
                          "note": "卖出侧，单笔上限 %.2f" % spec["tafMax"]})
    total = sum(i["amount"] for i in items)
    return {
        "side": "sell" if is_sell else "buy", "qty": qty, "price": round(px, 4),
        "notional": round(notional, 4), "items": items, "total": round(total, 4),
        "totalRate": round(total / notional, 8) if notional else 0.0,
        "market": mkt, "board": board,
        "note": "印花税仅卖出单边；佣金双向且有最低值；过户费与经手费双向。"
                "把费用写成单一双边费率会系统性低估小资金策略的成本。",
    }


def stamp_tax_of(side, qty, price, market=MARKET_CN, fees=None):
    """只算印花税（回测里常需要单独拆出这一项）。"""
    res = fee_of(side, qty, price, market=market, fees=fees)
    for item in res["items"]:
        if item["name"].startswith("印花税"):
            return item["amount"]
    return 0.0


# --------------------------------------------------------------------------- #
# 3. 交易时段
# --------------------------------------------------------------------------- #
#: A股时段（**北京时间**，分钟数）。2026-07-06 起全部 A股与 ETF 均有盘后固定价格交易。
CN_WINDOWS = {
    "openAuction": (9 * 60 + 15, 9 * 60 + 25),        # 开盘集合竞价（09:20 后不可撤单）
    "silence": (9 * 60 + 25, 9 * 60 + 30),            # 静默期：不接受任何申报
    "morning": (9 * 60 + 30, 11 * 60 + 30),
    "afternoon": (13 * 60, 14 * 60 + 57),
    "closeAuction": (14 * 60 + 57, 15 * 60),          # 收盘集合竞价（不可撤单）
    "afterHoursFixed": (15 * 60 + 5, 15 * 60 + 30),   # 盘后固定价格（成交价 = 收盘价）
}
#: 美股时段（**美东**分钟数）。用 UTC 换算会受夏令时影响，这里按美东 + 说明处理。
US_WINDOWS = {
    "preMarket": (4 * 60, 9 * 60 + 30),
    "regular": (9 * 60 + 30, 16 * 60),
    "afterMarket": (16 * 60, 20 * 60),
}


def _stamp_seconds(ts):
    """把时间戳统一成**秒**，并把脏值降级为「现在」。

    两件必须做的事：
    ① **单位自动判定**：本项目其它模块（如 trader）习惯传毫秒时间戳，而这里按秒处理；
       不判定单位会把「毫秒当秒」变成 5 万年后（`ValueError: year 58679 is out of range`），
       实盘表现为美股模拟成交直接 500 —— 这是本项目第二次踩到同类单位不一致。
    ② 脏值不抛异常：""/"abc"/NaN/{}/[] / 1e30 一律退回当前时间，并在返回值里标注。
    """
    if ts is None:
        return int(time.time()), False
    x = _num(ts)
    if x is None or x <= 0:
        return int(time.time()), False
    if x > 1e11:                             # 毫秒（≈ 1973 年以后）→ 先判单位再判范围
        x = x / 1000.0
    # 合理性与单位判定**必须按这个顺序**：上界若写成 4e10（≈ 公元 3237 年的秒数），
    # 会把真实毫秒时间戳先判成越界、静默退回「现在」—— 那样历史时刻会被当成当下，
    # 毫秒分支永远不可达（实测踩到）
    if x <= 0 or x > 4.2e9:                  # 约 2103 年之后的秒级时间视为越界
        return int(time.time()), False
    return int(x), True


def _local_minutes(ts, market, cfg):
    """把时间戳换成对应市场的「本地分钟数 + 星期」。

    用固定偏移而不是本机时区：用户机器未必在 Asia/Shanghai，
    依赖本机时区会让「现在是否开市」随机器设置漂移。
    A股用 UTC+8（无夏令时）；美股按美东 UTC−5/−4 粗略处理并在 note 中说明。
    """
    stamp, _ok = _stamp_seconds(ts)
    utc = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    if _market(market) == MARKET_US:
        # 夏令时（3 月第二个周日 – 11 月第一个周日）用 −4，其余 −5；
        # 未引入 tz 数据库，按月份粗略判断（4–10 月为 −4），差异 1 小时已在 note 说明
        offset = -4 if 4 <= utc.month <= 10 else -5
    else:
        offset = int(cfg["tzOffsetHours"])
    local = utc + datetime.timedelta(hours=offset)
    return local.weekday(), local.hour * 60 + local.minute, local


def session_of(ts=None, market=MARKET_CN, params=None):
    """当前处于哪个时段。

    返回 ``{"session","label","tradable","canCancel","minutes","weekday","local","note"}``。
    ``session`` 取值：``weekend`` / ``pre_open`` / ``openAuction`` / ``silence`` /
    ``morning`` / ``lunch`` / ``afternoon`` / ``closeAuction`` / ``afterHoursFixed`` /
    ``closed`` / ``preMarket`` / ``regular`` / ``afterMarket``。
    """
    cfg = _params(params)
    mkt = _market(market)
    weekday, minutes, local = _local_minutes(ts, mkt, cfg)
    out = {"market": mkt, "weekday": weekday, "minutes": minutes,
           "local": local.strftime("%Y-%m-%d %H:%M:%S"),
           "session": "closed", "label": "休市", "tradable": False,
           "canCancel": True, "note": ""}
    if weekday >= 5:
        out.update({"session": "weekend", "label": "周末休市",
                    "note": "A股/美股均为周一至周五交易；法定节假日与交易所公告的休市日同样休市"
                            "（本模块无交易日历，节假日会按开市处理）。"})
        return out

    def _in(name):
        start, end = (CN_WINDOWS if mkt == MARKET_CN else US_WINDOWS)[name]
        return start <= minutes < end

    if mkt == MARKET_CN:
        if _in("openAuction"):
            out.update({"session": "openAuction", "label": "开盘集合竞价", "tradable": True,
                        "canCancel": minutes < 9 * 60 + 20,
                        "note": "09:20 之后不接受撤单申报。"})
        elif _in("silence"):
            out.update({"session": "silence", "label": "静默期（不接受申报）", "tradable": False,
                        "note": "09:25–09:30 交易所不接受任何申报（含撤单），程序化订单需本地排队到 "
                                "09:30 再报送。"})
        elif _in("morning"):
            out.update({"session": "morning", "label": "上午连续竞价", "tradable": True,
                        "note": "限价申报有效价格范围 ±2%（越界为废单）。"})
        elif minutes >= 11 * 60 + 30 and minutes < 13 * 60:
            out.update({"session": "lunch", "label": "午间休市", "tradable": False})
        elif _in("afternoon"):
            out.update({"session": "afternoon", "label": "下午连续竞价", "tradable": True,
                        "note": "限价申报有效价格范围 ±2%（越界为废单）。"})
        elif _in("closeAuction"):
            out.update({"session": "closeAuction", "label": "收盘集合竞价", "tradable": True,
                        "canCancel": False,
                        "note": "14:57–15:00 为收盘集合竞价，**不接受撤单**；成交价按最大成交量原则产生。"})
        elif _in("afterHoursFixed") and cfg["allowAfterHoursFixed"]:
            out.update({"session": "afterHoursFixed", "label": "盘后固定价格交易", "tradable": True,
                        "note": "15:05–15:30 按**当日收盘价**、时间优先撮合；2026-07-06 起扩展至"
                                "全部 A股与 ETF（申报受理时间沪深略有差异）。"})
        else:
            out.update({"label": "非交易时段", "note": "不在申报受理时间内。"})
        return out

    if _in("preMarket"):
        out.update({"session": "preMarket", "label": "盘前（美股）", "tradable": True,
                    "note": "盘前流动性差、点差宽，部分订单类型不可用；时段按美东粗略换算。"})
    elif _in("regular"):
        out.update({"session": "regular", "label": "盘中（美股）", "tradable": True,
                    "note": "美股无个股涨跌幅限制，但有全市场熔断（7%/13%/20%）。"})
    elif _in("afterMarket"):
        out.update({"session": "afterMarket", "label": "盘后（美股）", "tradable": True,
                    "note": "盘后流动性差；时段按美东粗略换算。"})
    else:
        out.update({"label": "非交易时段（美股）"})
    return out


def is_tradable_now(ts=None, market=MARKET_CN, params=None):
    """此刻能否申报（等价于 ``session_of(...)["tradable"]``）。"""
    return bool(session_of(ts, market, params).get("tradable"))


def session_windows(market=MARKET_CN, params=None):
    """时段表（供接口/界面展示与逐条核对）。"""
    mkt = _market(market)
    table = CN_WINDOWS if mkt == MARKET_CN else US_WINDOWS
    labels = {"openAuction": "开盘集合竞价", "silence": "静默期（不接受申报）",
              "morning": "上午连续竞价", "afternoon": "下午连续竞价",
              "closeAuction": "收盘集合竞价（不可撤单）",
              "afterHoursFixed": "盘后固定价格交易", "preMarket": "盘前",
              "regular": "盘中", "afterMarket": "盘后"}
    out = []
    for key, (start, end) in table.items():
        out.append({"key": key, "label": labels.get(key, key),
                    "from": "%02d:%02d" % (start // 60, start % 60),
                    "to": "%02d:%02d" % (end // 60, end % 60),
                    "minutes": [start, end]})
    return {"market": mkt, "tz": "北京时间" if mkt == MARKET_CN else "美东（粗略换算）",
            "windows": out,
            "note": "A股：09:25–09:30 不接受任何申报；14:57–15:00 不可撤单；"
                    "15:05–15:30 盘后固定价格按收盘价成交。美股时段按美东、夏令时粗略处理。"}


# --------------------------------------------------------------------------- #
# 4. 最小申报单位与整手
# --------------------------------------------------------------------------- #
def lot_of(code, market=MARKET_CN, name=None):
    """最小申报单位（A股主板/创业板 100 股整数倍；科创板 200 股起、超 200 后可按 1 股递增；
    美股 1 股）。"""
    spec = board_of(code, market, name)
    return int(spec["lot"])


def min_qty_of(code, market=MARKET_CN, name=None):
    """最小申报数量（科创板 200 股，其余等于最小单位）。"""
    spec = board_of(code, market, name)
    return int(spec["minQty"])


def round_qty(code, qty, market=MARKET_CN, name=None, side="buy", position=None):
    """把数量取整到合法申报量。

    买入：向下取整到整手，且不低于最小申报数量（不足则 0）。
    卖出：允许卖出零股，但**余额不足一手时必须一次性卖出**（A股规则），
    因此卖出量先夹到持仓，再在「等于全部持仓」或「整手」之间取合法值。
    """
    spec = board_of(code, market, name)
    lot, min_qty = int(spec["lot"]), int(spec["minQty"])
    want = max(0, _int(qty, 0))
    pos = max(0, _int(position, 0))
    sell = str(side or "").strip().lower() in ("sell", "reduce", "close", "卖出", "减仓", "清仓")
    if spec.get("market") == MARKET_US:
        return max(0, min(want, pos) if sell and pos else want)
    if sell:
        avail = min(want, pos) if pos else want
        if pos and avail >= pos:
            return pos                      # 全部卖出：允许包含零股，必须一次报出
        if avail < lot:
            return 0                        # 不足一手的部分不能单独挂单（除非是全部持仓）
        return (avail // lot) * lot if lot > 1 else avail
    if spec["board"] == "star":
        if want < min_qty:
            return 0
        return want                          # 科创板超过 200 股后可 1 股递增
    if want < min_qty:
        return 0
    return (want // lot) * lot if lot > 1 else want


def sellable_qty(position, today_bought=0, frozen=0):
    """T+1 可卖数量：``昨仓 − 今日已卖 − 冻结``。

    ``position`` 是总持仓；``today_bought`` 是**今日买入**的数量（不可卖）。
    不能用总持仓当可卖量 —— 这是 T+1 最常见的实现错误。
    """
    total = max(0, _int(position, 0))
    bought = max(0, _int(today_bought, 0))
    frozen_qty = max(0, _int(frozen, 0))
    return max(0, total - bought - frozen_qty)


# --------------------------------------------------------------------------- #
# 5. 下单校验与撮合可行性
# --------------------------------------------------------------------------- #
def check_order(side, code, qty, price, market=MARKET_CN, name=None, prev_close=None,
                position=0, today_bought=0, frozen=0, cash=None, high=None, low=None,
                meta=None, params=None, fees=None, ts=None, board=None):
    """下单前校验：返回 ``{"ok","rejects","warnings","checks","limit","fee","lot",...}``。

    ``rejects`` 里任何一条非空即 ``ok = False``：调用方**不应**继续撮合，
    而应把中文原因原样展示（模拟交易页与接口都按这个口径报错）。
    ``warnings`` 是不阻断但必须告知用户的项（如风险警示股买入上限、非交易时段）。
    """
    cfg = _params(params)
    mkt = _market(market)
    spec = board_of(code, market, name)
    if str(board or "").strip():
        spec = dict(spec, board=str(board).strip())
    px = _num(price)
    qty = _int(qty, 0)
    is_sell = str(side or "").strip().lower() in ("sell", "reduce", "close", "卖出", "减仓", "清仓")
    meta = meta if isinstance(meta, dict) else {}
    lim = limit_of(code, market, name, cfg, meta)
    base = _num(prev_close)
    limits = limit_prices(code, base if base is not None else px, market, name, cfg, meta=meta)
    session = session_of(ts, market, cfg)
    rejects, warnings, checks = [], [], []

    def _check(name_, ok_, detail):
        checks.append({"name": name_, "pass": bool(ok_), "detail": detail})
        return bool(ok_)

    # ① 时段
    if not _check("交易时段", session["tradable"],
                  "%s（%s）" % (session["label"], session["local"])):
        rejects.append("当前不可申报：%s。%s" % (session["label"], session.get("note") or ""))
    elif session["session"] in ("openAuction", "closeAuction"):
        warnings.append("%s 期间为集合竞价，成交价由撮合规则产生，你的限价未必成交。"
                        % session["label"])

    # ② 停牌
    suspended = _bool(meta.get("suspended"), False)
    if not _check("是否停牌", not suspended, "停牌" if suspended else "正常交易"):
        rejects.append("该标的当日停牌，无法申报与成交。")

    # ③ 数量合法
    lot, min_qty = int(spec["lot"]), int(spec["minQty"])
    if mkt == MARKET_CN:
        if is_sell:
            pos = max(0, _int(position, 0))
            allow_odd = pos > 0 and qty >= pos      # 余额不足一手的整笔卖出是允许的
            qty_ok = qty > 0 and (allow_odd or qty % lot == 0) and qty >= min(min_qty, pos or min_qty)
            detail = ("卖出 %d 股（%s）" % (qty, "含零股的整笔卖出" if allow_odd else "%d 股整数倍" % lot))
        else:
            qty_ok = qty > 0 and qty >= min_qty and (spec["board"] == "star" or qty % lot == 0)
            detail = ("买入 %d 股（%s：%d 股起%s）"
                      % (qty, spec["label"], min_qty,
                         "，超过部分可 1 股递增" if spec["board"] == "star" else "，%d 股整数倍" % lot))
    else:
        qty_ok = qty > 0
        detail = "美股最小 1 股（默认口径，未取得权威来源）"
    if not _check("申报数量", qty_ok, detail):
        rejects.append("申报数量不合法：%s；%s 要求 %d 股起%s。"
                       % (detail, spec["label"], min_qty,
                          "、%d 股整数倍" % lot if spec["board"] != "star" else "、超过部分可 1 股递增"))

    # ④ 价格有效（涨跌停 + 笼子）
    if px is None or px <= 0:
        _check("价格有效", False, "缺少有效价格")
        rejects.append("缺少有效价格，无法申报。")
    else:
        if limits["up"] is not None and limits["down"] is not None:
            if px > limits["up"] + _EPS:
                _check("涨跌停", False, "报价 %.4f 高于涨停价 %.2f" % (px, limits["up"]))
                rejects.append("报价高于涨停价 %.2f，按 %s 规则为废单。"
                               % (limits["up"], lim["label"]))
            elif px < limits["down"] - _EPS:
                _check("涨跌停", False, "报价 %.4f 低于跌停价 %.2f" % (px, limits["down"]))
                rejects.append("报价低于跌停价 %.2f，按 %s 规则为废单。"
                               % (limits["down"], lim["label"]))
            else:
                _check("涨跌停", True, "在 [%.2f, %.2f] 内" % (limits["down"], limits["up"]))
        else:
            _check("涨跌停", True, "无涨跌幅限制")
        if session["session"] in ("morning", "afternoon") and base:
            cage_buy = max(base * (1 + cfg["priceCageRatio"]), base + cfg["priceCageTicks"] * cfg["tick"])
            cage_sell = min(base * (1 - cfg["priceCageRatio"]), base - cfg["priceCageTicks"] * cfg["tick"])
            if is_sell:
                ok_cage = px >= cage_sell - _EPS
                detail = "卖出报价 ≥ %.4f（基准价 %.4f 下浮 2%% 与 −10 ticks 取低）" % (cage_sell, base)
            else:
                ok_cage = px <= cage_buy + _EPS
                detail = "买入报价 ≤ %.4f（基准价 %.4f 上浮 2%% 与 +10 ticks 取高）" % (cage_buy, base)
            if not _check("价格笼子(±2%)", ok_cage, detail):
                # 注意：这个字符串同时含字面百分号与 %s 占位符，字面百分号必须写成 %%
                # （本项目已在两类模块里各踩过一次 ValueError: unsupported format character）
                rejects.append("超出连续竞价限价申报的有效价格范围（±2%%），会被交易所作废：%s。"
                               % detail)

    # ⑤ T+1 可卖
    if is_sell:
        sellable = sellable_qty(position, today_bought, frozen)
        pos = max(0, _int(position, 0))
        detail = ("总持仓 %d 股，今日买入 %d 股，冻结 %d 股 → 可卖 %d 股"
                  % (pos, _int(today_bought, 0), _int(frozen, 0), sellable))
        if not _check("T+1 可卖", qty <= sellable, detail):
            rejects.append("T+1 限制：%s。A股当日买入的部分不可当日卖出。" % detail)
        if mkt == MARKET_US:
            checks[-1] = {"name": "卖出上限", "pass": qty <= pos, "detail": detail + "（美股为 T+0，可当日买卖）"}
            if qty > pos:
                rejects = [r for r in rejects if not r.startswith("T+1")]
                rejects.append("卖出数量超过持仓：%s" % detail)

    # ⑥ 资金 / 持仓
    fee = fee_of(side, qty, px, market, board=spec["board"], fees=fees)
    if not is_sell:
        need = (qty * (px or 0)) + fee["total"]
        cash_num = _num(cash)
        if cash is None:
            warnings.append("未提供可用资金，跳过资金校验。")
        elif cash_num is None:
            # 传了 cash 但解析不出（""/"abc"/{}）：既不能当 0 也不能当通过 ——
            # 资金是下单的硬约束，无法校验就必须拒单，否则「资金够不够」这道闸门被静默跳过
            _check("可用资金", False, "可用资金无法解析：%r" % (cash,))
            rejects.append("可用资金无法解析（原值 %r），无法校验资金是否充足，已拒单。"
                           % (cash,))
        elif not _check("可用资金", cash_num >= need - _EPS,
                        "需 %.2f（含费用 %.2f），可用 %.2f" % (need, fee["total"], cash_num)):
            rejects.append("可用资金不足：需 %.2f（含费用 %.2f），可用 %.2f。"
                           % (need, fee["total"], cash_num))
        cap = cfg["stBuyLimit"]
        if spec.get("riskWarning") and qty > cap:
            warnings.append("风险警示股单日买入上限 %d 股，本次申报 %d 股可能被拒。" % (cap, qty))
    else:
        if position is None:
            warnings.append("未提供持仓，跳过持仓校验。")
        elif not _check("持仓充足", qty <= max(0, _int(position, 0)),
                        "持仓 %d 股，申报卖出 %d 股" % (_int(position, 0), qty)):
            rejects.append("持仓不足：持股 %d 股，申报卖出 %d 股。" % (_int(position, 0), qty))
        if spec.get("riskWarning"):
            warnings.append("风险警示股（%s）波动与退市风险较高，请确认已阅读风险揭示。" % spec["name"])

    # ⑦ 封板提示
    if px is not None and limits["up"] is not None and abs(px - limits["up"]) < cfg["tick"]:
        warnings.append("报价已到涨停价 %.2f：涨停时买单按时间优先排队，封板则无法成交。" % limits["up"])
    if px is not None and limits["down"] is not None and abs(px - limits["down"]) < cfg["tick"]:
        warnings.append("报价已到跌停价 %.2f：跌停时卖单排队，封板则无法卖出。" % limits["down"])
    if _num(high) is not None and limits["up"] is not None and _num(high) < limits["up"] - cfg["tick"]:
        limit_state = "未触及涨停（说明当日未封上涨停）"
    else:
        limit_state = ""

    return {
        "ok": not rejects, "rejects": rejects, "warnings": warnings, "checks": checks,
        "side": "sell" if is_sell else "buy", "code": _code(code), "qty": qty,
        "price": px, "market": mkt, "board": spec["board"], "label": spec["label"],
        "limit": limits, "limitRatio": lim, "fee": fee, "lot": lot, "minQty": min_qty,
        "sellable": sellable_qty(position, today_bought, frozen) if is_sell else None,
        "session": session, "limitState": limit_state,
        "rulesVersion": RULES_VERSION,
        "note": "校验项：交易时段 / 停牌 / 申报数量 / 涨跌停 / 价格笼子 / T+1 可卖 / 资金与持仓。"
                "任何 rejects 非空即不应撮合，并把中文原因原样展示给用户。",
    }


def can_fill(side, price, prev_close=None, code=None, market=MARKET_CN, high=None, low=None,
             meta=None, params=None, ts=None, name=None, board=None):
    """撮合可行性：涨停买不到、跌停卖不出、停牌不成交。

    判定口径（**不是**「价格可取即成交」）：
    · 停牌 → 不可成交；
    · 买入且价格已达涨停价、且当日最高价未高于涨停价 → 视为封板，不可成交；
    · 卖出且价格已达跌停价、且当日最低价未低于跌停价 → 视为封板，不可成交；
    · 其余情况可成交（真实撮合还受排队与成交量约束，本模块不模拟排队深度）。
    """
    cfg = _params(params)
    meta = meta if isinstance(meta, dict) else {}
    px = _num(price)
    is_sell = str(side or "").strip().lower() in ("sell", "reduce", "close", "卖出", "减仓", "清仓")
    limits = limit_prices(code, _num(prev_close) if _num(prev_close) is not None else px,
                          market, name, cfg, meta=meta)
    if _bool(meta.get("suspended"), False):
        return {"ok": False, "reason": "停牌，不成交", "limits": limits}
    if px is None or px <= 0:
        return {"ok": False, "reason": "无有效价格", "limits": limits}
    if limits["up"] is None:
        session = session_of(ts, market, cfg)
        return {"ok": True, "reason": "无涨跌幅限制，按价格成交（不模拟熔断与流动性）",
                "limits": limits, "session": session["session"]}
    tick = cfg["tick"]
    if not is_sell and px >= limits["up"] - _EPS:
        day_high = _num(high)
        if day_high is None or day_high <= limits["up"] + _EPS:
            return {"ok": False, "reason": "涨停封板（最高价未突破涨停价 %.2f），买单排队无法成交"
                                          % limits["up"], "limits": limits}
    if is_sell and px <= limits["down"] + _EPS:
        day_low = _num(low)
        if day_low is None or day_low >= limits["down"] - _EPS:
            return {"ok": False, "reason": "跌停封板（最低价未跌破跌停价 %.2f），卖单排队无法卖出"
                                          % limits["down"], "limits": limits}
    return {"ok": True, "reason": "可成交（未封板；真实撮合还受排队与成交量约束，本模块不模拟排队深度）",
            "limits": limits}


def rules_table(market=MARKET_CN, params=None):
    """规则总表（接口与界面用：把引擎实际生效的口径逐条摊开给用户看）。"""
    cfg = _params(params)
    mkt = _market(market)
    rows = []
    if mkt == MARKET_CN:
        for key in ("main", "gem", "star", "bse"):
            spec = BOARDS[key]
            rows.append({
                "board": key, "label": spec["label"],
                "limit": "%.0f%%" % ((spec["limit"] or 0) * 100),
                "lot": "%d 股整数倍" % spec["lot"] if key != "star" else "200 股起，超过部分可 1 股递增",
                "note": {"main": "沪深主板；风险警示股按 stLimitRatio（默认 10%%）",
                         "gem": "创业板",
                         "star": "科创板",
                         "bse": "北交所；新股仅首日不设涨跌幅"}[key],
            })
        extra = [
            {"item": "新股上市初期", "value": "主板/创业板/科创板前 %d 个交易日不设涨跌幅；北交所仅首日"
             % int(cfg["newListingDays"])},
            {"item": "风险警示股（ST/*ST）", "value": "涨跌幅 %.0f%%（2026-07-06 起；旧口径 5%%）；"
             "单日买入上限 %d 股" % (cfg["stLimitRatio"] * 100, cfg["stBuyLimit"])},
            {"item": "退市整理期", "value": "15 个交易日，首日不设涨跌幅，此后 %.0f%%"
             % (cfg["delistingLimitRatio"] * 100)},
            {"item": "T+1", "value": "当日买入当日不可卖；可卖 = 昨仓 − 今日已卖 − 冻结"},
            {"item": "涨跌停价", "value": "前收盘价 ×(1±比例) 四舍五入到 0.01 元；"
             "不足 0.01 元按 ±0.01 元；低于 0.01 元按 0.01 元"},
            {"item": "价格笼子", "value": "连续竞价限价申报 ±2%（或 ±10 ticks，取更宽者），越界为废单"},
            {"item": "费用", "value": "印花税卖出 0.05%；佣金双向不足 5 元按 5 元；"
             "过户费 0.001% 双向；经手费 0.00341% 双向（北交所 0.0125%）"},
            {"item": "封板", "value": "涨停买不到、跌停卖不出（按时间优先排队，无对手单则零成交）"},
        ]
    else:
        rows.append({"board": "us", "label": "美股", "limit": "无个股涨跌幅限制",
                     "lot": "1 股（默认口径，未取得权威来源）",
                     "note": "全市场熔断 7%/13%/20%（按标普 500 单日跌幅）"})
        extra = [
            {"item": "结算", "value": "T+1 结算（2024-05-28 起），但交易本身可当日买卖（T+0）"},
            {"item": "时段", "value": "盘前 04:00–09:30、盘中 09:30–16:00、盘后 16:00–20:00（美东，券商差异大）"},
            {"item": "日内交易", "value": "PDT 规则：被认定为日内交易者的账户最低权益 25,000 美元"},
        ]
    return {"market": mkt, "version": RULES_VERSION, "sources": list(RULES_SOURCES),
            "unverified": list(UNVERIFIED), "boards": rows, "extra": extra,
            "sessions": session_windows(mkt, cfg),
            "note": RULES_NOTE, "params": cfg}
