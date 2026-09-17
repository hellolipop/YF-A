# -*- coding: utf-8 -*-
"""AlphaDesk · AI 选股引擎（core/advisor.py）

定位
----
回答一个问题：**「给我一批标的，现在该买谁、买多少、什么价位买卖、后面大概怎么走」**。
它不做「黑箱打分」，而是把四件彼此独立、各自可验证的证据拼成一个结论：

====================================  ================================================
多策略方向共识（stance）              7 个策略在**当前这根K线**上的多空状态 → 投票
统计优势（edge）                      用同一份共识做历史回放，量出「单笔收益率分布」
凯利仓位（kelly）                     由胜率 × 赔率（或条件分布 μ/σ²）推出 f*，再折扣截断
概率预测（forecast）                  历史相似状态回找 → 条件分布 → 中位路径与上下轨
====================================  ================================================

四条证据分别落在 `ensemble` / `edge` / `kelly` / `forecast` 四个字段里，前端与用户
都能看到每一票、每一笔样本、每一个分位点，而不是只看到最终那个建议。评分与建议只是
这四条证据的加权汇总，汇总结论另附 `scoreParts` / `confidenceParts` 便于归因审计。

两套口径必须区分清楚（本模块的核心设计取舍）
--------------------------------------------
1. **触发信号（trigger）**：来自 `core/strategies.py`，稀疏、事件式（金叉/死叉/突破）。
   用途是**在图上的买卖标记**——用户要看的是「哪一天出现了什么信号」。
2. **方向状态（stance）**：本模块实现，稠密、状态式（快线在慢线上方 / RSI 处于超卖区）。
   用途是**共识票数与统计优势回放**——「此刻七个策略里几个偏多」不能用事件式信号
   回答（同一根K线上通常只有 0~1 个策略触发，凑不出共识）。

两者共用同一个 `core/indicators.py`，且 stance 的周期参数**从 `core/strategies.py`
的参数表读取默认值**（`S.default_params`），不在本模块二次硬编码，避免两处口径漂移。

口径与单位（前端按此消费，不可混用）
------------------------------------
· **小数（ratio，[0,1]）**：`winRate` `upProb` `confidence` `kelly.weight`
  `portfolio.totalWeight` `portfolio.rows[].weight`；
· **百分数（直接带 % 语义）**：`changePct` `forecast.expectedReturn` `risk.atrPct`
  `risk.vol` `risk.maxDrawdown`（正数表示回撤幅度）；
· **原始比值 / 价格 / 股数**：`edge.payoff` `plan.riskReward` `kelly.fStar`
  `kelly.amount` `kelly.shares` `score`（0~100）。

建议档位（action）
------------------
`buy` 买入 / `add` 增持 / `hold` 持有 / `reduce` 减仓 / `sell` 卖出 /
`watch` 观望 / `avoid` 回避。由「评分 + 置信度 + 凯利权重 + 风险红线」共同裁定，
红线优先（数据不足、波动或回撤极端时不建议建仓），规则全部写在 `_decide()` 里。

不做什么（保持诚实）
--------------------
· 不预测点位、不承诺收益：`forecast` 是**历史条件分布**，note 里写明措辞；
· 不做汇率换算：多市场标的按各自本币口径分配，`portfolio.note` 有说明；
· 不抛异常：单只标的失败只影响它自己那一行（`ok=False` + `error`），批量的其余标的照常返回；
· 不算 T+1、涨跌停、停牌、做空与杠杆，仓位上限统一由 `max_weight` 与现金缓冲约束。
"""

from __future__ import annotations

import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import indicators as I
from . import kelly as K
from . import strategies as S
from .forecast import forecast as build_forecast

__all__ = [
    "recommend", "stances", "stance_net", "DEFAULT_HORIZON", "DEFAULT_CAPITAL",
    "MIN_BARS", "MIN_TRADES_FOR_EDGE", "ACTION_LABEL", "DISCLAIMER", "STANCE_KEYS",
    "to_record", "record_id", "record_note", "review", "REVIEW_NOTE", "REVIEW_HORIZONS",
]

# --------------------------------------------------------------------------- #
# 常量与文案
# --------------------------------------------------------------------------- #
#: 默认预测窗口（未来交易日根数），与前端默认值保持一致
DEFAULT_HORIZON = 20
#: 默认本金
DEFAULT_CAPITAL = 100000.0
#: 默认分数凯利系数（半凯利）
DEFAULT_FRACTION = K.DEFAULT_FRACTION
#: 默认单只权重上限
DEFAULT_MAX_WEIGHT = K.DEFAULT_MAX_WEIGHT
#: 默认现金缓冲
DEFAULT_CASH_BUFFER = K.DEFAULT_CASH_BUFFER
#: 默认单只权重下限
DEFAULT_MIN_WEIGHT = K.DEFAULT_MIN_WEIGHT
#: 可分析的最少K线根数（低于此值只给「数据不足」，不硬算）
MIN_BARS = 60
#: 统计优势可用的最少完整交易笔数（低于此值改走连续凯利，并在 note 中说明）
MIN_TRADES_FOR_EDGE = 6
#: 共识回放的入场 / 出场阈值（7 票制）：净票 ≥ +2 建仓，≤ −1 离场
ENTRY_NET = 2
EXIT_NET = -1
#: 回放预热根数（保证 7 个策略的指标全部可用）
REPLAY_WARMUP = 60
#: K 线图上标记的回溯窗口（只画最近的信号，避免历史被标记糊满）
MARK_WINDOW = 90
#: 图上标记的数量上限
MAX_MARKS = 18
#: 因子 chip 的返回上限（前端展示 3 个 + 「+N」）
MAX_FACTORS = 8
#: 默认手续费率 / 滑点（与 core/runner.py 回测默认值一致）
DEFAULT_FEE = 0.0003
DEFAULT_SLIPPAGE = 0.001
#: 状态特征的默认参数来源说明
PARAM_SOURCE = "方向状态（stance）的周期参数取自 core/strategies.py 的策略参数表默认值"
#: 凯利值上限（分数化之前先截断）：连续凯利在低波动样本上会给出极端值
#: （f* = μ/σ² 当 σ→0 时发散，> 1 意味着需要杠杆），不截断会让单只权重被极端值主导
KELLY_CAP = 3.0
#: 凯利样本量收缩半衰点：f_used = f* × n / (n + KELLY_SHRINK_N)。
#: 11 笔样本只能拿到 0.35 的信任度，30 笔 0.60、100 笔 0.83 —— 避免小样本上
#: 估出来的赔率（常见 10 倍以上）被当成真值直接放大成满仓
KELLY_SHRINK_N = 20.0
#: 因子里动量项的窗口（固定 20 根，与 `_indicators_at` 的计算窗口必须一致，
#: 否则文案会写出与实际计算不符的窗口 —— 这是实测发现的文案/口径不一致缺陷）
MOM_WINDOW = 20
#: 评分增益：方向分实测落在 ±0.6，增益取 60 让 0~100 刻度被充分利用
SCORE_GAIN = 60.0
#: 建议档位是「总闸门」：只有这两个档位才在本次组合中开新仓
ENTRY_ACTIONS = ("buy", "add")

ACTION_LABEL = {
    "buy": "买入", "add": "增持", "hold": "持有", "reduce": "减仓",
    "sell": "卖出", "watch": "观望", "avoid": "回避",
}
#: 建议档位 → 图上标记方向（None 表示不画标记）
ACTION_MARK = {
    "buy": "buy", "add": "buy", "reduce": "sell", "sell": "sell", "avoid": "sell",
    "hold": None, "watch": None,
}
#: 标记颜色（区别于触发信号的涨跌色，便于一眼分辨是 AI 结论还是策略信号）
MARK_COLOR = {"buy": "#4d8dff", "sell": "#ff8c42"}
#: stance → 票面用词
VOTE_OF = {1: "buy", 0: "hold", -1: "sell"}

DISCLAIMER = (
    "本页结论由公开行情数据经统计模型自动计算得出，仅用于技术研究与学习，不构成任何投资建议；"
    "模型存在失效风险，历史统计不代表未来表现，据此操作的盈亏由投资者自行承担。"
)

SCORE_NOTE = (
    "综合评分 = 50 + 60 × 加权方向分 − 回撤惩罚（最高 12 分，历史最大回撤 40% 及以上打满）。"
    "方向分权重：多策略共识 0.30、均线趋势 0.20、动量 0.10、条件分布 0.25、统计优势 0.15，"
    "缺失项按剩余权重重新归一。校准说明：共识净票按 ±4 票饱和、条件分布同时看上涨概率与"
    "期望收益，因此方向分实测落在 ±0.6 区间，取增益 60 让 0~100 刻度被充分利用；"
    "档位阈值 68 / 58 / 45 / 36 对应方向分约 +0.30 / +0.13 / −0.08 / −0.23。"
    "评分只做横向比较，不是收益预测。"
)
CONF_NOTE = (
    "置信度 = 0.45 × 预测置信度 + 0.30 × 共识一致性（|净票| / 4，4 票即饱和）"
    " + 0.25 × 样本量因子（n/(n+15)），衡量的是「这次统计本身可信不可信」，"
    "不是涨跌概率，更不是收益承诺。低于 25% 时不给「买入」，最多给到「增持」。"
)
EDGE_NOTE = (
    "统计优势口径：把「7 策略方向净票 ≥ +2 建仓、≤ −1 离场、期末强制平仓」这条共识规则"
    "在同一段历史K线上回放，逐笔记录**信号级单笔收益率**（已扣手续费与滑点，与仓位无关）。"
    "样本量普遍偏小（日线数百根通常只有个位数到十几笔），只作参考，不足以支撑参数调优。"
)
PORTFOLIO_NOTE = (
    "组合分配由服务端按凯利折扣与单只权重上限折算：先逐只定权，再等比归一化到计划仓位"
    "（1 − 现金缓冲），取整后若超出预算则按比例缩减并向下取整到整手；"
    "权重之和即总仓位，其余计为现金；本金与现金按各自市场本币口径，不做跨市场汇率换算。"
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(x):
    """宽松转 float；None / bool / NaN / ±inf / 无法解析的字符串一律返回 None。"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    else:
        try:
            v = float(str(x).strip())
        except (TypeError, ValueError):
            return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _r(x, nd=4):
    """四舍五入；无效值返回 None（出口统一清洗，绝不让 inf / NaN 进 JSON）。"""
    v = _num(x)
    return None if v is None else round(v, nd)


def _clamp(v, lo, hi):
    if v is None:
        return None
    return max(lo, min(hi, v))


def _pct(v):
    """小数 → 百分比字符串（仅用于中文说明文案）。"""
    n = _num(v)
    return "—" if n is None else "%.2f%%" % (n * 100.0)


def _int_arg(v, dflt, lo, hi):
    """宽松取整：解析失败用 dflt，结果截断到 [lo, hi]。"""
    x = _num(v)
    if x is None:
        x = dflt
    try:
        iv = int(x)
    except (TypeError, ValueError, OverflowError):
        iv = int(dflt)
    return max(lo, min(hi, iv))


def _param(v, dflt):
    """比例类参数：非法回退默认值，数值截断到 [0, 1]。"""
    x = _num(v)
    if x is None:
        return float(dflt)
    return max(0.0, min(1.0, x))


def now_ms():
    return int(time.time() * 1000)


def _cache(c, key, producer):
    """指标缓存：同名指标只算一次（回放时被调用 N 次）。"""
    if key not in c:
        c[key] = producer()
    return c[key]


def _clean_bars(bars):
    """清洗K线：只保留 OHLC 齐全、价格为正的记录。

    **不重排顺序**（与 `core/forecast.py` 一致）：上游 `api_kline` 已保证
    「旧 → 新」，而按时间字段重排会在时间格式不规范（如自定义回测序列、
    非标准时间串）时把原本正确的序列打乱，反而制造出假的巨幅跳空 —— 这是
    实测踩到的坑，因此这里只做数值清洗，顺序以调用方为准。
    """
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        o, h, l, c = (_num(b.get("open")), _num(b.get("high")),
                      _num(b.get("low")), _num(b.get("close")))
        if c is None or c <= 0:
            continue
        h = h if (h is not None and h > 0) else c
        l = l if (l is not None and l > 0) else c
        o = o if (o is not None and o > 0) else c
        if h < l:
            h, l = l, h
        out.append({"t": b.get("t"), "open": o, "high": max(h, c, o),
                    "low": min(l, c, o), "close": c,
                    "volume": _num(b.get("volume")) or 0.0})
    return out


# --------------------------------------------------------------------------- #
# 一、方向状态（stance）：7 个策略在「当前这根K线」上的多空状态
# --------------------------------------------------------------------------- #
def _st_registry():
    """stance 的周期参数：从策略注册表读取默认值，避免两处硬编码。"""
    return {k: S.default_params(k) for k in S.keys()}


def _s_ma_cross(bars, i, c, p):
    closes = c["closes"]
    nf, ns = int(p["fast"]), int(p["slow"])
    fast = _cache(c, "ma_fast_%d" % nf, lambda: I.SMA(closes, nf))
    slow = _cache(c, "ma_slow_%d" % ns, lambda: I.SMA(closes, ns))
    if I.ok(fast[i]) and I.ok(slow[i]):
        if fast[i] > slow[i]:
            gap = (fast[i] / slow[i] - 1.0) * 100.0
            return 1, "MA%d 在 MA%d 上方 %.2f%%" % (nf, ns, gap)
        if fast[i] < slow[i]:
            gap = (fast[i] / slow[i] - 1.0) * 100.0
            return -1, "MA%d 在 MA%d 下方 %.2f%%" % (nf, ns, -gap)
        # 完全相等（价格不变 / 停牌一字板 / 数据补齐）属于无信息，必须给 0 票，
        # 否则会把「没有方向」错误地算成「看空」，污染共识与回放统计
        return 0, "MA%d 与 MA%d 重合（无方向）" % (nf, ns)
    return 0, "均线数据不足"


def _s_macd(bars, i, c, p):
    closes = c["closes"]
    key = "macd_%d_%d_%d" % (int(p["fast"]), int(p["slow"]), int(p["signal"]))
    m = _cache(c, key, lambda: I.MACD(closes, int(p["fast"]), int(p["slow"]),
                                     int(p["signal"])))
    dif, dea = m["dif"][i], m["dea"][i]
    if I.ok(dif) and I.ok(dea):
        if dif > dea:
            return 1, "DIF %.4f 在 DEA %.4f 上方" % (dif, dea)
        if dif < dea:
            return -1, "DIF %.4f 在 DEA %.4f 下方" % (dif, dea)
        # 与均线同理：完全重合说明无信息，给 0 票而不是看空
        return 0, "DIF 与 DEA 重合（%.4f，无方向）" % (dif,)
    return 0, "MACD 数据不足"


def _s_rsi(bars, i, c, p):
    closes = c["closes"]
    n = int(p["n"])
    # 零波动退化：Wilder 平滑在「既无上涨也无下跌」时分母为 0，实现上会返回
    # RSI = 100（见 core/indicators.RSI 的除零保护）。对停牌 / 连续一字板的K线，
    # 100 会被读成「严重超买」并投出看空票 —— 把无信息变成利空，因此显式中性化。
    win = closes[max(0, i - n):i + 1]
    if len(win) >= 2 and all(v == win[0] for v in win):
        return 0, "近 %d 根价格无波动，RSI 退化，中性票" % (len(win) - 1)
    r = _cache(c, "rsi_%d" % n, lambda: I.RSI(closes, n))[i]
    if not I.ok(r):
        return 0, "RSI 数据不足"
    low, high = float(p["low"]), float(p["high"])
    if r <= low:
        return 1, "RSI%d = %.1f，处于超卖区（≤ %.0f）" % (n, r, low)
    if r >= high:
        return -1, "RSI%d = %.1f，处于超买区（≥ %.0f）" % (n, r, high)
    return 0, "RSI%d = %.1f，多空中性" % (n, r)


def _s_breakout(bars, i, c, p):
    n, m = int(p["n"]), int(p["m"])
    if i < max(n, m) + 1:
        return 0, "通道数据不足"
    hi = max(b["high"] for b in bars[i - n:i])
    lo = min(b["low"] for b in bars[i - m:i])
    close = bars[i]["close"]
    if close > hi:
        return 1, "收盘 %.4f 突破前 %d 根高点 %.4f" % (close, n, hi)
    if close < lo:
        return -1, "收盘 %.4f 跌破前 %d 根低点 %.4f" % (close, m, lo)
    return 0, "位于 %d 根通道内（%.4f ~ %.4f）" % (n, lo, hi)


def _s_momentum(bars, i, c, p):
    n = int(p["n"])
    if i < n + 1:
        return 0, "动量数据不足"
    close, base = bars[i]["close"], bars[i - n]["close"]
    if base <= 0:
        return 0, "基准价无效"
    mom = (close / base - 1.0) * 100.0
    th = float(p["th"])
    if mom > th:
        return 1, "近 %d 根涨幅 %.2f%%，超过阈值 %.2f%%" % (n, mom, th)
    if mom < 0:
        return -1, "近 %d 根涨幅 %.2f%%，动量转负" % (n, mom)
    return 0, "近 %d 根涨幅 %.2f%%，未达阈值 %.2f%%" % (n, mom, th)


def _s_boll(bars, i, c, p):
    closes = c["closes"]
    n = int(p["n"])
    key = "boll_%d_%s" % (n, str(p["k"]))
    b = _cache(c, key, lambda: I.BOLL(closes, n, float(p["k"])))
    up, low, mid = b["up"][i], b["low"][i], b["mid"][i]
    if not (I.ok(up) and I.ok(low) and I.ok(mid)):
        return 0, "布林带数据不足"
    close = closes[i]
    if close < low:
        return 1, "收盘 %.4f 跌破布林下轨 %.4f（均值回归看多）" % (close, low)
    if close > up:
        return -1, "收盘 %.4f 突破布林上轨 %.4f（均值回归看空）" % (close, up)
    width = up - low
    pos = (close - low) / width if width > 0 else 0.5
    return 0, "位于布林带 %.0f%% 分位（下轨 %.4f / 上轨 %.4f）" % (pos * 100, low, up)


def _s_kdj(bars, i, c, p):
    n = int(p["n"])
    kdj = _cache(c, "kdj_%d" % n, lambda: I.KDJ(bars, n, 3, 3))
    k, d = kdj["K"][i], kdj["D"][i]
    if not (I.ok(k) and I.ok(d)):
        return 0, "KDJ 数据不足"
    if k > d:
        return 1, "K %.1f 在 D %.1f 上方" % (k, d)
    if k < d:
        return -1, "K %.1f 在 D %.1f 下方" % (k, d)
    return 0, "K ≈ D（%.1f）" % (k,)


_STANCE_FNS = {
    "maCross": _s_ma_cross,
    "macd": _s_macd,
    "rsi": _s_rsi,
    "breakout": _s_breakout,
    "momentum": _s_momentum,
    "bollRevert": _s_boll,
    "kdjCross": _s_kdj,
}
#: stance 的固定输出顺序（与策略注册表一致，便于前后端对齐）
STANCE_KEYS = tuple(S.keys())


def stances(bars, i, cache=None, params=None):
    """在索引 i 处给出 7 个策略的多空状态。

    返回 ``{key: {"name", "stance", "signal", "brief"}}``，其中 ``stance`` ∈
    ``{1, 0, -1}``，``signal`` ∈ ``{"buy", "hold", "sell"}``（供前端直接展示）。
    单个策略计算异常只影响它自己（视为 0），不会中断整体。
    """
    c = cache if cache is not None else {}
    c.setdefault("closes", [b["close"] for b in bars])
    reg = _st_registry()
    out = {}
    for key in STANCE_KEYS:
        fn = _STANCE_FNS.get(key)
        p = (params or {}).get(key) or reg.get(key) or {}
        try:
            sig, brief = fn(bars, i, c, p) if fn else (0, "无状态实现")
        except Exception:  # noqa: BLE001  单策略异常不拖垮整只标的
            sig, brief = 0, "状态计算异常"
        sig = 1 if sig > 0 else (-1 if sig < 0 else 0)
        out[key] = {"name": S.STRATEGIES[key]["name"], "stance": sig,
                    "signal": VOTE_OF[sig], "brief": brief}
    return out


def _stance_vec(bars, i, cache, params=None):
    """只取数值的 stance（回放热路径，避免每根K线都拼中文说明）。"""
    c = cache
    c.setdefault("closes", [b["close"] for b in bars])
    reg = _st_registry()
    vec = {}
    for key in STANCE_KEYS:
        fn = _STANCE_FNS.get(key)
        p = (params or {}).get(key) or reg.get(key) or {}
        try:
            sig = fn(bars, i, c, p)[0] if fn else 0
        except Exception:  # noqa: BLE001
            sig = 0
        vec[key] = 1 if sig > 0 else (-1 if sig < 0 else 0)
    return vec


def stance_net(vec):
    """净票：stance 之和，取值 [-7, 7]。"""
    if not vec:
        return 0
    return int(sum(vec.values()))


# --------------------------------------------------------------------------- #
# 二、统计优势：用共识规则在同一段历史上回放
# --------------------------------------------------------------------------- #
def _round_trip(entry_bar, exit_bar, entry_i, exit_i, fee, slippage, reason):
    """一笔交易的「信号级单笔收益率」：买入按滑点上浮、卖出按下浮，双边计费。"""
    buy = entry_bar["close"] * (1.0 + slippage) * (1.0 + fee)
    sell = exit_bar["close"] * (1.0 - slippage) * (1.0 - fee)
    ret = (sell / buy - 1.0) if buy > 0 else 0.0
    return {
        "entryT": entry_bar["t"], "exitT": exit_bar["t"],
        "entry": _r(entry_bar["close"], 4), "exit": _r(exit_bar["close"], 4),
        "bars": int(exit_i - entry_i), "ret": _r(ret, 6), "reason": reason,
    }


def _replay(bars, cache, params, fee, slippage, warmup):
    """共识回放：净票 ≥ ENTRY_NET 建仓、≤ EXIT_NET 离场、期末强制平仓。

    返回逐笔明细与统计量。**量的是信号级单笔收益率（与仓位无关）**，
    因此不经过 core/portfolio.py（那里管的是资金、股数与撮合），
    这样「优势」这一项不会被本金大小与整手取整污染。
    """
    n = len(bars)
    trades = []
    pos = None
    for i in range(max(1, warmup), n):
        net = stance_net(_stance_vec(bars, i, cache, params))
        if pos is None:
            if net >= ENTRY_NET:
                pos = (i, bars[i])
        elif net <= EXIT_NET:
            trades.append(_round_trip(pos[1], bars[i], pos[0], i, fee, slippage, "共识转空"))
            pos = None
    if pos is not None and n:
        trades.append(_round_trip(pos[1], bars[n - 1], pos[0], n - 1,
                                  fee, slippage, "期末平仓"))

    rets = [t["ret"] for t in trades if t["ret"] is not None]
    m = len(rets)
    wins = [v for v in rets if v > 0]
    losses = [-v for v in rets if v < 0]
    win_rate = (len(wins) / m) if m else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    payoff = (avg_win / avg_loss) if (avg_win is not None and avg_loss) else None
    expectancy = (sum(rets) / m) if m else None
    hold = (sum(t["bars"] for t in trades) / m) if m else None

    return {
        "trades": m,
        "wins": len(wins),
        "losses": len(losses),
        "winRate": _r(win_rate, 4),
        "payoff": _r(payoff, 4),
        "payoffInfinite": bool(m and not losses),
        "avgWin": _r(avg_win, 4),
        "avgLoss": _r(avg_loss, 4),
        "expectancy": _r(expectancy * 100.0, 4) if expectancy is not None else None,
        "edge": _r(expectancy * 100.0, 4) if expectancy is not None else None,
        "holdBars": _r(hold, 2),
        "sample": m,
        "detail": trades[-12:],
        "note": EDGE_NOTE,
    }


# --------------------------------------------------------------------------- #
# 三、凯利仓位
# --------------------------------------------------------------------------- #
def _qval(fc, pct):
    """取条件分布的分位值：整数键与字符串键都兼容。

    `core/forecast.py` 输出的是**整数键**（{5, 25, 50, 75, 95}），而调用方为了
    可读性常写 "25" / "95" 这样的字符串；`_plan()` 曾经只按字符串键查找，于是
    「目标位锚定条件分布」静默失效（连备注都写成「条件分布不可用」，与事实相反）。
    这类缺陷不会报错、只会让功能悄悄退化，因此统一收口成一个函数，三种写法都试。
    """
    q = fc.get("quantiles") or {}
    for key in (pct, str(pct)):
        v = _num(q.get(key))
        if v is not None:
            return v
    try:
        return _num(q.get(int(pct)))
    except (TypeError, ValueError):
        return None


def _kelly_input(edge, fc):
    """取凯利输入：优先「离散（胜率 + 赔率）」，样本不足时退回「连续（μ/σ²）」。

    连续口径的 μ 与 σ 来自 forecast 的**条件收益分布**：μ 用期望收益，
    σ 由 25% / 75% 分位差按正态 IQR = 1.349σ 反推（比直接用分位极差稳健）。
    返回 ``(f, kind, note)``；任何情形都不抛异常，退化时 f = 0。
    """
    m = int(edge.get("trades") or 0)
    p = _num(edge.get("winRate"))
    b = _num(edge.get("payoff"))
    if m >= MIN_TRADES_FOR_EDGE and p is not None and b is not None and b > 0:
        # 样本内零亏损时 payoff 为 None（赔率发散，离散凯利无有限解），自然落到下面
        # 的连续口径 —— 那比「把赔率当成无穷大」更保守。
        res = K.kelly_fraction(p, b)
        note = ("离散凯利：胜率 %.2f%%、赔率 %.2f（%d 笔共识交易样本）→ f* = %.4f。%s"
                % (p * 100.0, b, m, res["f"], res["advice"]))
        return res["f"], "discrete", note

    mu = _num(fc.get("expected_return"))
    q25 = _qval(fc, 25)
    q75 = _qval(fc, 75)
    sample = int(fc.get("sample") or 0)
    if mu is None or q25 is None or q75 is None or sample < 15:
        reason = ("统计样本不足（共识交易 %d 笔 < %d，条件分布样本 %d < 15），"
                  "无法估计优势" % (m, MIN_TRADES_FOR_EDGE, sample))
        return 0.0, "none", reason + "：凯利仓位按 0 处理，不建议建仓。"
    sigma = (q75 - q25) / 1.349
    if sigma <= 0:
        return 0.0, "none", "条件分布离散度为 0（分位差为 0），无法估计优势，仓位按 0 处理。"
    res = K.kelly_continuous(mu, sigma * sigma)
    note = ("连续凯利：用条件分布估计 μ = %s、σ = %s（由 25%%/75%% 分位差按 IQR = 1.349σ 反推），"
            "f* = μ/σ² = %.4f。%s（离散口径不可用：共识交易 %d 笔 < %d）"
            % (_pct(mu), _pct(sigma), res["f"], res["advice"], m, MIN_TRADES_FOR_EDGE))
    return res["f"], "continuous", note


# --------------------------------------------------------------------------- #
# 四、评分 / 置信度 / 建议
# --------------------------------------------------------------------------- #
def score_parts(net, ma_dev, align, mom, up_prob, expectancy, expected_return, trades, max_dd):
    """评分分量（每个分量已归一化到 [-1, 1]，供归因审计）。

    两处刻度的选择需要说明（否则阈值会不可达）：
      · **共识净票按 ±4 饱和**：七票要全部同向是极端事件，实测净票常见区间是 ±3，
        按 ±7 归一会让共识项永远只有 ±0.4，把整体评分压在 50 附近；
      · **条件分布同时看概率与期望**：只取上涨概率会在「右偏分布」（多数样本小跌、
        少数样本大涨，期望为正）时给出负分，与预测本身的结论自相矛盾。
    """
    parts = {"ensemble": None, "trend": None, "momentum": None, "forecast": None,
             "edge": None, "riskPenalty": 0.0}
    weights = {"ensemble": 0.30, "trend": 0.20, "momentum": 0.10,
               "forecast": 0.25, "edge": 0.15}
    parts["ensemble"] = _clamp(net / 4.0, -1, 1)
    if ma_dev is not None:
        trend = _clamp(ma_dev / 0.06, -1, 1) * 0.6 + (align or 0) * 0.4
        parts["trend"] = _clamp(trend, -1, 1)
    if mom is not None:
        parts["momentum"] = _clamp(mom / 0.15, -1, 1)
    if up_prob is not None or expected_return is not None:
        by_prob = _clamp((up_prob - 0.5) * 2.5, -1, 1) if up_prob is not None else 0.0
        by_mean = _clamp((expected_return or 0.0) / 6.0, -1, 1)   # 单位：百分数，6% 打满
        parts["forecast"] = _clamp(0.5 * by_prob + 0.5 * by_mean, -1, 1)
    if trades >= MIN_TRADES_FOR_EDGE and expectancy is not None:
        parts["edge"] = _clamp(expectancy / 3.0, -1, 1)   # expectancy 单位：百分数
    used = {k: weights[k] for k in weights if parts[k] is not None}
    wsum = sum(used.values())
    comp = (sum(parts[k] * used[k] for k in used) / wsum) if wsum else 0.0
    base = 50.0 + SCORE_GAIN * _clamp(comp, -1, 1)
    penalty = 12.0 * (_clamp((max_dd or 0.0) / 40.0, 0, 1) or 0.0)
    for key in ("ensemble", "trend", "momentum", "forecast", "edge"):
        if parts[key] is not None:
            parts[key] = _r(parts[key], 4)
    parts["riskPenalty"] = round(penalty, 2)
    parts["composite"] = round(_clamp(comp, -1, 1), 4)
    parts["weights"] = used
    parts["base"] = round(base, 2)
    return round(_clamp(base - penalty, 0.0, 100.0), 1), parts


def confidence_parts(fc_conf, net, trades):
    """置信度分量（0~1）。共识一致性同样按 ±4 票饱和（与评分口径保持一致）。"""
    agreement = min(1.0, abs(net) / 4.0)
    sample = (trades / (trades + 15.0)) if trades else 0.0
    conf = 0.45 * (fc_conf or 0.0) + 0.30 * agreement + 0.25 * sample
    return _r(_clamp(conf, 0, 1), 4), {
        "forecast": _r(fc_conf, 4), "agreement": _r(agreement, 4),
        "sample": _r(sample, 4),
    }


def _decide(score, net, conf, weight, risk, enough):
    """建议档位裁定：风险红线优先，其次是评分与共识，最后是凯利权重。"""
    if not enough:
        return "watch", "数据不足：K线过短或价格异常，无法给出可执行建议"
    if risk.get("maxDrawdown") is not None and risk["maxDrawdown"] >= 55:
        return "avoid", "回避：历史最大回撤 %.1f%% 超过 55%% 红线" % risk["maxDrawdown"]
    if risk.get("vol") is not None and risk["vol"] >= 90:
        return "avoid", "回避：年化波动 %.1f%% 超过 90%% 红线" % risk["vol"]
    if score >= 68 and conf >= 0.25 and (weight or 0) > 0:
        return "buy", "买入：评分 %.1f、置信度 %s、凯利权重 %s" % (
            score, _pct(conf), _pct(weight))
    if score >= 68:
        return "add", "增持：评分 %.1f 达标，但置信度 %s 或凯利权重为 0，未通过完整买入条件" % (
            score, _pct(conf))
    if score >= 58:
        return "add", "增持：评分 %.1f 偏多" % score
    if score >= 45:
        return "hold", "持有：评分 %.1f 中性，方向未形成一致" % score
    if score >= 36:
        return "reduce", "减仓：评分 %.1f 偏弱" % score
    if net <= -2:
        return "sell", "卖出：评分 %.1f 且共识净票 %d 明确转空" % (score, net)
    return "watch", "观望：评分 %.1f 偏弱但空方共识不足" % score


# --------------------------------------------------------------------------- #
# 五、交易计划与风险
# --------------------------------------------------------------------------- #
def _plan(price, atr, action, fc, market):
    """交易计划：止损用 ATR，目标位优先锚定**条件分布**，取不到才退回 ATR 倍数。

    这样做的好处是盈亏比会随「统计上行空间」变化，而不是永远等于 ATR 倍数之比
    （固定倍数会让盈亏比变成常数、失去信息量）。约束：
      · 止损不超过 -12%（避免高波动标的一次止损吃掉过多本金）；
      · 目标位单调递增 / 递减，且价格恒为正。
    """
    if price is None or price <= 0:
        return {"entry": None, "stop": None, "target1": None, "target2": None,
                "riskReward": None, "atr": None, "direction": None,
                "side": None, "tradeable": None, "inverted": None, "labels": None,
                "forecastTarget": None, "note": "无有效价格，无法给出交易计划"}
    a = atr if (atr and atr > 0) else price * 0.02
    last = _num(fc.get("lastClose"))

    def _level(pct):
        v = _qval(fc, pct)
        return (last * (1.0 + v)) if (v is not None and last) else None

    usable = bool(last) and not fc.get("degraded") and (fc.get("sample") or 0) > 0
    q25p = _level("25") if usable else None
    q75p = _level("75") if usable else None
    lo_p = _level("5") if usable else None
    hi_p = _level("95") if usable else None
    anchors = []

    if action in ("reduce", "sell", "avoid"):
        entry = price
        stop = entry + 1.5 * a
        t1 = entry - 1.5 * a
        if q25p is not None and q25p < t1:
            t1 = q25p
            anchors.append("目标1 取条件分布 25% 分位价")
        t2 = entry - 3.0 * a
        if lo_p is not None and lo_p < t2:
            t2 = lo_p
            anchors.append("目标2 取条件分布 5% 分位价")
        if t2 >= t1:
            t2 = t1 - 0.5 * a
        t1, t2 = max(t1, 0.01), max(min(t2, t1), 0.01)
        risk, reward = stop - entry, entry - t1
        direction, side = "exit", "离场"
    else:
        entry = price
        stop = max(entry - 1.5 * a, entry * 0.88)
        t1 = entry + 1.5 * a
        if q75p is not None and q75p > t1:
            t1 = q75p
            anchors.append("目标1 取条件分布 75% 分位价")
        t2 = entry + 3.0 * a
        if hi_p is not None and hi_p > t2:
            t2 = hi_p
            anchors.append("目标2 取条件分布 95% 分位价")
        if t2 <= t1:
            t2 = t1 + 0.5 * a
        risk, reward = entry - stop, t1 - entry
        direction, side = "long", "建仓"

    rr = (reward / risk) if risk > 0 else None
    long_side = direction == "long"
    stop_pct = (1.0 - stop / entry) * 100.0 if long_side else (stop / entry - 1.0) * 100.0
    # 一句话必须点明「这是建仓计划还是离场计划」以及「能不能据此买入」：
    # 离场口径下止损在上方、目标在下方，若只写「止损 X、目标 Y」，用户会读成
    # 「止损价高于买入价」这种自相矛盾的买入计划（实际发生过的误读）
    act_label = ACTION_LABEL.get(action, side)
    if long_side:
        note = ("建仓计划（档位「%s」）：建议买入 %.4f；止损 %.4f（1.5×ATR，距现价 %.2f%%，"
                "且不超过 -12%%）；目标1 %.4f、目标2 %.4f。" % (act_label, entry, stop, stop_pct, t1, t2))
    else:
        note = ("离场计划（档位「%s」，**不新开仓位**）：参考价 %.4f；涨破 %.4f 则离场判断失效"
                "（1.5×ATR，距现价 %.2f%%）；下行目标1 %.4f、下行目标2 %.4f。"
                % (act_label, entry, stop, stop_pct, t1, t2))
    if anchors:
        note += "其中" + "、".join(anchors) + "（统计分位，取不到时才退回 ATR 倍数）。"
    else:
        note += "条件分布不可用或未超出 ATR 目标，目标位为纯 ATR 倍数。"
    note += "目标位是波动与统计尺度的参照，不是价格预测，也不构成买卖建议。"
    return {
        "entry": _r(entry, 4), "stop": _r(stop, 4),
        "target1": _r(t1, 4), "target2": _r(t2, 4),
        "riskReward": _r(rr, 3), "atr": _r(a, 4),
        "forecastTarget": _r(hi_p if direction == "long" else lo_p, 4),
        "direction": direction, "note": note,
        # 让消费方无需自己推断语义：side（建仓/离场）、tradeable（能否据此新开仓位）、
        # inverted（是否「止损在上、目标在下」的离场口径）、labels（各方位的正确中文标签）。
        # 界面与导出只要照 labels 渲染，就不会再把离场计划标成「建议买入 / 止损」
        "side": side, "tradeable": long_side, "inverted": not long_side,
        "labels": ({
            "entry": "建议买入", "stop": "止损（跌破离场）",
            "target1": "目标1（上行）", "target2": "目标2（上行）",
        } if long_side else {
            "entry": "参考价（不新开仓位）", "stop": "离场失效价（涨破则判断失效）",
            "target1": "下行目标1（离场参考）", "target2": "下行目标2（离场参考）",
        }),
    }


def _risk(bars, atr, price):
    """风险度量：ATR%、年化波动（近 60 根）、历史最大回撤（全样本）。"""
    closes = [b["close"] for b in bars]
    n = len(closes)
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, n) if closes[i - 1] > 0]
    win = rets[-60:] if len(rets) > 60 else rets
    vol = None
    if len(win) >= 5:
        m = sum(win) / len(win)
        sd = (sum((v - m) ** 2 for v in win) / len(win)) ** 0.5
        vol = sd * math.sqrt(252.0) * 100.0
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = max(mdd, (peak - c) / peak)
    atr_pct = (atr / price * 100.0) if (atr and price and price > 0) else None
    flags = []
    if atr_pct is not None and atr_pct >= 4:
        flags.append("日内波动偏大（ATR%% %.2f）" % atr_pct)
    if vol is not None and vol >= 55:
        flags.append("年化波动偏高（%.1f%%）" % vol)
    if mdd >= 40:
        flags.append("历史最大回撤较深（%.1f%%）" % (mdd * 100.0))
    return {
        "atrPct": _r(atr_pct, 3), "vol": _r(vol, 3),
        "maxDrawdown": _r(mdd * 100.0, 3),
        "note": ("ATR%% 衡量日内波动，年化波动衡量近 60 根收益离散度，"
                 "历史最大回撤衡量极端回撤承受度，仅作风险提示，不参与收益预测。"
                 + ("本次提示：" + "；".join(flags) + "。" if flags else "")),
    }


# --------------------------------------------------------------------------- #
# 六、关键因子
# --------------------------------------------------------------------------- #
def _factors(ind, fc, edge, kelly_f, kelly_kind, risk, horizon):
    """把「支撑本次结论的证据」逐条列出来（含具体数值，便于用户复核）。"""
    out = []
    ma5, ma20, ma60 = ind["ma5"], ind["ma20"], ind["ma60"]
    if I.ok(ma5) and I.ok(ma20) and I.ok(ma60):
        align = 1 if (ma5 > ma20 > ma60) else (-1 if (ma5 < ma20 < ma60) else 0)
        out.append({
            "key": "ma", "label": "均线排列", "dir": "up" if align > 0 else ("down" if align < 0 else ""),
            "brief": "MA5 %.4f / MA20 %.4f / MA60 %.4f，收盘偏离 MA20 %s"
                     % (ma5, ma20, ma60, _pct(ind["maDev"])),
        })
    if ind["dif"] is not None and ind["dea"] is not None:
        up = ind["dif"] > ind["dea"]
        out.append({
            "key": "macd", "label": "MACD", "dir": "up" if up else "down",
            "brief": "DIF %.4f / DEA %.4f，柱 %.4f，%s"
                     % (ind["dif"], ind["dea"], ind["macd"] or 0.0,
                        "DIF 在 DEA 上方" if up else "DIF 在 DEA 下方"),
        })
    if ind["rsi"] is not None:
        r = ind["rsi"]
        d = "up" if r <= 35 else ("down" if r >= 70 else "")
        out.append({"key": "rsi", "label": "RSI %.0f" % r,
                    "dir": d, "brief": "RSI14 = %.1f%s" % (r, "，超卖区" if r <= 35 else ("，超买区" if r >= 70 else "，中性区"))})
    if ind["k"] is not None and ind["d"] is not None:
        out.append({
            "key": "kdj", "label": "KDJ", "dir": "up" if ind["k"] > ind["d"] else "down",
            "brief": "K %.1f / D %.1f / J %.1f" % (ind["k"], ind["d"], ind["j"] or 0.0),
        })
    if ind["bollPos"] is not None:
        pos = ind["bollPos"]
        d = "up" if pos <= 0.2 else ("down" if pos >= 0.8 else "")
        out.append({"key": "boll", "label": "布林位置", "dir": d,
                    "brief": "位于布林带 %.0f%% 分位" % (pos * 100)})
    if ind["volRatio"] is not None:
        vr = ind["volRatio"]
        d = "up" if vr >= 1.3 else ("down" if vr <= 0.6 else "")
        out.append({"key": "volume", "label": "量能", "dir": d,
                    "brief": "当日成交量为 5 日均量的 %.2f 倍" % vr})
    if ind["mom"] is not None:
        out.append({"key": "momentum", "label": "动量",
                    "dir": "up" if ind["mom"] > 0 else "down",
                    "brief": "近 %d 根涨跌 %s" % (MOM_WINDOW, _pct(ind["mom"]))})
    if fc.get("up_prob") is not None:
        up = fc["up_prob"]
        out.append({
            "key": "forecast", "label": "条件分布",
            "dir": "up" if up >= 0.55 else ("down" if up <= 0.45 else ""),
            "brief": "历史相似状态下 h=%d 上涨概率 %.1f%%、中位收益 %s、样本 %d%s"
                     % (horizon, up * 100.0, _pct(fc.get("median_return")),
                        fc.get("sample") or 0, "（已降级）" if fc.get("degraded") else ""),
        })
    if edge.get("trades"):
        out.append({
            "key": "edge", "label": "历史共识",
            "dir": "up" if (edge.get("expectancy") or 0) > 0 else "down",
            "brief": "%d 笔共识交易，胜率 %s、期望 %s/笔、平均持有 %s 根"
                     % (edge["trades"], _pct(edge.get("winRate")),
                        "%.3f%%" % (edge.get("expectancy") or 0.0),
                        edge.get("holdBars")),
        })
    if kelly_f is not None:
        out.append({
            "key": "kelly", "label": "凯利仓位",
            "dir": "up" if kelly_f > 0 else "",
            "brief": "f* = %.4f（%s）" % (kelly_f, "离散口径" if kelly_kind == "discrete" else "连续口径"),
        })
    if risk.get("atrPct") is not None:
        out.append({
            "key": "risk", "label": "波动风险",
            "dir": "down" if (risk["atrPct"] >= 4 or (risk.get("vol") or 0) >= 55) else "",
            "brief": "ATR %.2f%%、年化波动 %.1f%%、最大回撤 %.1f%%"
                     % (risk["atrPct"], risk.get("vol") or 0.0, risk.get("maxDrawdown") or 0.0),
        })
    # 有明确方向的因子优先展示（前端只展示 3 个 + 「+N」）
    order = {key: n for n, key in enumerate(
        ("ma", "macd", "rsi", "kdj", "boll", "volume", "momentum",
         "forecast", "edge", "kelly", "risk"))}
    out.sort(key=lambda x: (0 if x["dir"] else 1, order.get(x["key"], 99)))
    return out[:MAX_FACTORS]


def _indicators_at(bars, i, cache):
    """末根K线的指标快照（供因子文案与展示使用）。"""
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    ma5 = _cache(cache, "ma_fast_5", lambda: I.SMA(closes, 5))[i]
    ma20 = _cache(cache, "ma_slow_20", lambda: I.SMA(closes, 20))[i]
    ma60 = _cache(cache, "ma_slow_60", lambda: I.SMA(closes, 60))[i]
    macd = _cache(cache, "macd_12_26_9", lambda: I.MACD(closes, 12, 26, 9))
    rsi = _cache(cache, "rsi_14", lambda: I.RSI(closes, 14))[i]
    kdj = _cache(cache, "kdj_9", lambda: I.KDJ(bars, 9, 3, 3))
    boll = _cache(cache, "boll_20_2", lambda: I.BOLL(closes, 20, 2.0))
    atr = _cache(cache, "atr_14", lambda: I.ATR(bars, 14))[i]
    close = closes[i]
    boll_pos = None
    if I.ok(boll["up"][i]) and I.ok(boll["low"][i]):
        width = boll["up"][i] - boll["low"][i]
        boll_pos = ((close - boll["low"][i]) / width) if width > 0 else 0.5
    vol_ratio = None
    vols = [b.get("volume") or 0.0 for b in bars]
    if len(vols) >= 6:
        avg5 = sum(vols[i - 5:i]) / 5.0
        if avg5 > 0:
            vol_ratio = vols[i] / avg5
    mom = None
    if i >= MOM_WINDOW and closes[i - MOM_WINDOW] > 0:
        mom = closes[i] / closes[i - MOM_WINDOW] - 1.0
    return {
        "close": close, "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "maDev": (close / ma20 - 1.0) if I.ok(ma20) and ma20 else None,
        "align": (1 if (I.ok(ma5) and I.ok(ma20) and I.ok(ma60) and ma5 > ma20 > ma60)
                  else (-1 if (I.ok(ma5) and I.ok(ma20) and I.ok(ma60) and ma5 < ma20 < ma60) else 0)),
        "dif": macd["dif"][i] if I.ok(macd["dif"][i]) else None,
        "dea": macd["dea"][i] if I.ok(macd["dea"][i]) else None,
        "macd": macd["macd"][i] if I.ok(macd["macd"][i]) else None,
        "rsi": rsi if I.ok(rsi) else None,
        "k": kdj["K"][i] if I.ok(kdj["K"][i]) else None,
        "d": kdj["D"][i] if I.ok(kdj["D"][i]) else None,
        "j": kdj["J"][i] if I.ok(kdj["J"][i]) else None,
        "bollPos": boll_pos, "volRatio": vol_ratio, "mom": mom,
        "atr": atr if I.ok(atr) else None,
    }


# --------------------------------------------------------------------------- #
# 七、K 线图叠加层与买卖标记
# --------------------------------------------------------------------------- #
def _forecast_path(fc):
    """把 forecast.path（首点为当前锚点）转成**只含未来点**的叠加层路径。

    与 `web/js/chart.js` 的约定一致：图表自己从最后一根收盘价起笔，
    因此这里丢掉第 0 个锚点，返回 horizon 个点，每点带 t / mid / lo / hi。
    """
    p = fc.get("path") or {}
    med = p.get("median") or []
    lo = p.get("lo") or []
    hi = p.get("hi") or []
    ts = p.get("t") or []
    out = []
    for j in range(1, min(len(med), len(lo), len(hi))):
        out.append({
            "i": j - 1,
            "t": ts[j] if (ts and j < len(ts)) else None,
            "mid": med[j], "lo": lo[j], "hi": hi[j],
        })
    return out


def _marks(bars, mark_window):
    """策略触发信号标记（只画最近 mark_window 根内的信号）。"""
    n = len(bars)
    cache = {}
    marks = []
    start = max(1, n - mark_window)
    for i in range(start, n):
        for key in STANCE_KEYS:
            try:
                sig = S.signal(key, bars, S.default_params(key), i, cache)
            except Exception:  # noqa: BLE001
                sig = None
            if not sig:
                continue
            marks.append({
                "t": bars[i]["t"], "idx": i, "dir": sig, "kind": "trigger",
                "strategy": key,
                "label": S.STRATEGIES[key]["name"] + ("买" if sig == "buy" else "卖"),
            })
    marks.sort(key=lambda m: m["idx"])
    if len(marks) > MAX_MARKS:
        marks = marks[-MAX_MARKS:]
    return marks


def _advice_mark(bars, action, action_text):
    """AI 建议落在最后一根K线上（图上直接能看到「现在的结论」）。"""
    d = ACTION_MARK.get(action)
    if not d or not bars:
        return None
    return {
        "t": bars[-1]["t"], "idx": len(bars) - 1, "dir": d, "kind": "advice",
        "label": action_text, "color": MARK_COLOR.get(d), "strategy": "ai",
    }


# --------------------------------------------------------------------------- #
# 八、单只标的分析
# --------------------------------------------------------------------------- #
def _error_row(sym, message):
    return {
        "ok": False, "code": sym.get("code"), "name": sym.get("name") or sym.get("code"),
        "market": sym.get("market"), "price": None, "changePct": None,
        "action": None, "actionText": "数据不足", "score": None, "confidence": None,
        "signals": [], "ensemble": None, "edge": None, "kelly": None,
        "forecast": None, "plan": None, "risk": None, "advisor": None,
        "error": message, "note": message,
    }


def _analyze_one(sym, ctx):
    """单只标的全流程：数据 → 共识 → 优势 → 凯利 → 预测 → 计划 → 评分 → 标记。"""
    market = sym.get("market") or ctx["market"]
    code = str(sym.get("code") or "").strip()
    name = sym.get("name") or code
    horizon = ctx["horizon"]
    if not code:
        return _error_row(sym, "标的代码为空")

    try:
        raw = ctx["fetch_bars"](market, code, ctx["period"], ctx["limit"]) or []
    except Exception as e:  # noqa: BLE001  网络异常只影响这一只
        return _error_row(sym, "行情获取失败：%s" % e)
    bars = _clean_bars(raw)
    if len(bars) < MIN_BARS:
        return _error_row(sym, "历史数据不足（有效K线 %d 根 < %d 根）" % (len(bars), MIN_BARS))

    n = len(bars)
    i = n - 1
    cache = {"closes": [b["close"] for b in bars]}

    # 现价 / 涨跌幅：优先实时行情，取不到就用最后一根收盘价
    price, change_pct = bars[i]["close"], None
    try:
        q = ctx["fetch_quote"](market, code) if ctx["fetch_quote"] else None
    except Exception:  # noqa: BLE001
        q = None
    if isinstance(q, dict) and q:
        price = _num(q.get("price")) or price
        change_pct = _num(q.get("changePct"))
        # 名称回填：调用方只给了代码时（最常见的用法），用行情源返回的名称补上。
        # 不做这一步的话，界面与历史记录里全是「600519」这样的数字，用户得自己记代码；
        # 行情源本来就带名称，回填是零成本的。只在「没名称」或「名称就是代码」时替换，
        # 不覆盖调用方明确给出的名称（例如用户自定义的备注名）。
        qname = str(q.get("name") or "").strip()
        if qname and (not name or str(name).strip().upper() == code):
            name = qname
    if change_pct is None and n >= 2 and bars[i - 1]["close"] > 0:
        change_pct = (bars[i]["close"] / bars[i - 1]["close"] - 1.0) * 100.0

    # 1) 方向共识
    st_map = stances(bars, i, cache, ctx["params"])
    net = stance_net({k: v["stance"] for k, v in st_map.items()})
    votes = [{"strategy": k, "strategyName": v["name"], "signal": v["signal"],
              "stance": v["stance"], "brief": v["brief"]} for k, v in st_map.items()]
    ensemble = {
        "buy": sum(1 for v in votes if v["signal"] == "buy"),
        "hold": sum(1 for v in votes if v["signal"] == "hold"),
        "sell": sum(1 for v in votes if v["signal"] == "sell"),
        "net": net, "votes": votes, "paramSource": PARAM_SOURCE,
    }

    # 2) 指标快照
    ind = _indicators_at(bars, i, cache)

    # 3) 统计优势（共识回放，复用同一份指标缓存）
    edge = _replay(bars, cache, ctx["params"], ctx["fee"], ctx["slippage"], REPLAY_WARMUP)

    # 4) 概率预测
    fc = build_forecast(bars, horizon=horizon)

    # 5) 凯利：先按 KELLY_CAP 截断（连续凯利在低波动样本上会发散），
    #    再按样本量收缩（小样本上的赔率估计不可信，直接放大成满仓是过度自信）
    kelly_raw, kelly_kind, kelly_note = _kelly_input(edge, fc)
    kelly_f = min(kelly_raw, KELLY_CAP)
    if kelly_raw > KELLY_CAP:
        kelly_note += (" 原始凯利 %.4f 超过杠杆上限 %.1f（低波动样本上 μ/σ² 会发散），"
                       "已在分数化前截断为 %.1f。" % (kelly_raw, KELLY_CAP, KELLY_CAP))
    n_shrink = (edge.get("trades") or 0) if kelly_kind == "discrete" else (fc.get("sample") or 0)
    shrink = min(1.0, n_shrink / (n_shrink + KELLY_SHRINK_N)) if n_shrink else 0.0
    kelly_f *= shrink
    if shrink < 0.999:
        kelly_note += (" 样本量收缩 ×%.4f（n = %d，f_used = f* × n/(n+%d)）："
                       "小样本估出的赔率与胜率不可信，仓位按样本量打折。"
                       % (shrink, n_shrink, int(KELLY_SHRINK_N)))

    # 6) 风险 / 评分 / 置信度 / 建议
    risk = _risk(bars, ind["atr"], price)
    score, parts = score_parts(net, ind["maDev"], ind["align"], ind["mom"],
                               fc.get("up_prob"), edge.get("expectancy"),
                               (fc.get("expected_return") or 0.0) * 100.0,
                               edge.get("trades") or 0, risk["maxDrawdown"])
    conf, conf_parts = confidence_parts(fc.get("confidence"), net, edge.get("trades") or 0)
    frac_weight = K.fractional(kelly_f, ctx["fraction"], ctx["max_weight"],
                               ctx["min_weight"])["weight"]
    action, action_text = _decide(score, net, conf, frac_weight, risk, True)

    # 7) 计划 / 因子 / 标记
    plan = _plan(price, ind["atr"], action, fc, market)
    factors = _factors(ind, fc, edge, kelly_f, kelly_kind, risk, horizon)
    marks = _marks(bars, ctx["mark_window"])
    advice = _advice_mark(bars, action, ACTION_LABEL.get(action, action))
    if advice:
        marks = marks + [advice]

    q = fc.get("quantiles") or {}
    fc_note = ("历史条件分布：窗口 h=%d，样本 %d，置信度 %.2f%s"
               % (horizon, fc.get("sample") or 0, fc.get("confidence") or 0.0,
                  ("，已降级（%s）" % fc.get("degrade_reason")) if fc.get("degraded") else ""))
    row = {
        "ok": True,
        "code": code, "name": name, "market": market,
        "price": _r(price, 4), "changePct": _r(change_pct, 4),
        "asOf": bars[i]["t"], "bars": n,
        "action": action, "actionText": action_text,
        "score": score, "scoreParts": parts,
        "confidence": conf, "confidenceParts": conf_parts,
        "signals": factors,
        "ensemble": ensemble,
        "edge": edge,
        "kelly": {
            "fStar": _r(kelly_f, 4), "fStarRaw": _r(kelly_raw, 4),
            "kind": kelly_kind, "fraction": ctx["fraction"],
            "shrink": _r(shrink, 4), "shrinkSample": n_shrink,
            "rawWeight": _r(frac_weight, 4),
            "weight": None, "amount": None, "shares": None, "actual": None,
            "lot": ctx["lot_of"](market),
            "entryAction": action in ENTRY_ACTIONS,
            "note": kelly_note,
        },
        "forecast": {
            "expectedReturn": _r((fc.get("expected_return") or 0.0) * 100.0, 4),
            "upProb": _r(fc.get("up_prob"), 4),
            "medianReturn": _r((fc.get("median_return") or 0.0) * 100.0, 4),
            "bandLow": _r(fc.get("path", {}).get("lo", [None])[-1] if fc.get("path", {}).get("lo") else None, 4),
            "bandHigh": _r(fc.get("path", {}).get("hi", [None])[-1] if fc.get("path", {}).get("hi") else None, 4),
            "quantiles": {str(k): _r((v or 0.0) * 100.0, 4) for k, v in q.items()},
            "sample": fc.get("sample") or 0, "confidence": _r(fc.get("confidence"), 4),
            "degraded": bool(fc.get("degraded")), "degradeReason": fc.get("degrade_reason"),
            "note": fc_note,
        },
        "plan": plan,
        "risk": risk,
        "advisor": {
            "marks": marks,
            "forecast": {"path": _forecast_path(fc), "horizon": horizon,
                         "levels": (fc.get("path") or {}).get("levels")},
            "plan": {"entry": plan["entry"], "stop": plan["stop"],
                     "target1": plan["target1"], "target2": plan["target2"]},
        },
        "note": SCORE_NOTE,
    }
    return row


# --------------------------------------------------------------------------- #
# 九、组合分配
# --------------------------------------------------------------------------- #
def _portfolio(rows, capital, fraction, max_weight, cash_buffer, min_weight):
    """把逐只结论汇总成组合分配，并把分配结果回填到每一行。

    **建议档位是总闸门，凯利只决定闸门内的仓位大小**：只有 `buy` / `add` 的标的
    才参与本次资金分配；`hold`（已持有，不加仓）、`reduce` / `sell` / `avoid` /
    `watch` 一律按 0 仓处理，但行内仍保留 `kelly.rawWeight`（「如果建仓会是多少」）
    与 `kelly.reason` 说明被拦下的原因，避免出现「建议回避却分配了 25% 资金」这种
    自相矛盾的输出（这是实测发现的问题）。
    """
    cands = []
    for r in rows:
        if not r.get("ok"):
            continue
        k = r.get("kelly") or {}
        gate = bool(k.get("entryAction"))
        cands.append({
            "code": r.get("code"), "name": r.get("name"), "market": r.get("market"),
            "price": r.get("price"),
            "kelly": (k.get("fStar") or 0.0) if gate else 0.0,
        })
    alloc = K.allocate(cands, capital, fraction=fraction, max_weight=max_weight,
                       cash_buffer=cash_buffer, min_weight=min_weight, min_samples=1)
    by_code = {}
    for p in alloc.get("positions") or []:
        by_code[str(p.get("code"))] = p

    cap = alloc.get("capital") or 0.0
    p_rows = []
    for r in rows:
        k = r.get("kelly")
        if not r.get("ok") or not k:
            continue
        if not k.get("entryAction"):
            k["weight"], k["amount"], k["shares"], k["actual"] = 0.0, 0.0, 0, 0.0
            k["reason"] = ("建议档位为「%s」，本次不新开仓位（凯利权重 %s 仅供参考）"
                           % (ACTION_LABEL.get(r.get("action"), r.get("action") or "—"),
                              _pct(k.get("rawWeight"))))
            k["note"] = (k.get("note") or "") + "｜" + k["reason"]
            continue
        p = by_code.get(str(r.get("code")))
        if not p:
            k["weight"], k["amount"], k["shares"], k["actual"] = 0.0, 0.0, 0, 0.0
            k["reason"] = "未参与组合分配"
            continue
        actual = _num(p.get("actual")) or 0.0
        k["weight"] = _r((actual / cap) if cap > 0 else 0.0, 6)
        k["targetWeight"] = _r(p.get("weight"), 6)
        k["amount"] = _r(actual, 2)
        k["targetAmount"] = _r(p.get("amount"), 2)
        k["shares"] = int(p.get("shares") or 0)
        k["actual"] = _r(actual, 2)
        k["reason"] = p.get("reason")
        k["note"] = (k.get("note") or "") + "｜组合分配：" + (p.get("reason") or "")
        tw = _num(k.get("targetWeight"))
        if actual > 0 and tw is not None and k["weight"] > tw + 1e-9:
            # 整手取整采用「就近取整（半手向上）」，实际权重可能略高于计划权重
            k["note"] += ("｜注意：整手取整使实际权重 %s 略高于计划权重 %s"
                          "（就近取整，最多半手），金额与股数以实际建仓口径为准。"
                          % (_pct(k["weight"]), _pct(tw)))
        if actual > 0:
            p_rows.append({
                "code": r.get("code"), "name": r.get("name"), "market": r.get("market"),
                "weight": k["weight"], "amount": k["amount"], "shares": k["shares"],
                "price": r.get("price"), "action": r.get("action"),
            })
    p_rows.sort(key=lambda x: -(x.get("weight") or 0))
    total = sum((x.get("weight") or 0.0) for x in p_rows)
    return {
        "totalWeight": _r(total, 6),
        "cash": _r(alloc.get("cash"), 2),
        "invested": _r(alloc.get("invested"), 2),
        "capital": _r(cap, 2),
        "capacity": _r(alloc.get("capacity"), 4),
        "budget": _r(alloc.get("budget"), 2),
        "count": len(p_rows),
        "rows": p_rows,
        "note": PORTFOLIO_NOTE + "｜" + (alloc.get("note") or ""),
    }


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #
def _normalize_symbols(symbols, market, limit):
    """整理标的列表：去重、补市场、限流；顺序保持用户输入顺序。"""
    items = symbols if isinstance(symbols, (list, tuple)) else []
    out, seen = [], set()
    for s in list(items)[:limit]:
        if isinstance(s, str):
            s = {"code": s}
        if not isinstance(s, dict):
            continue
        code = str(s.get("code") or s.get("symbol") or "").strip().upper()
        if not code:
            continue
        mkt = str(s.get("market") or market or "cn").strip().lower()
        mkt = "us" if mkt.startswith("us") else "cn"
        key = mkt + ":" + code
        if key in seen:
            continue
        seen.add(key)
        out.append({"code": code, "market": mkt,
                    "name": s.get("name") or code})
    return out


def recommend(symbols, fetch_bars, fetch_quote=None, market="cn", horizon=DEFAULT_HORIZON,
              capital=DEFAULT_CAPITAL, kelly_fraction=DEFAULT_FRACTION,
              max_weight=DEFAULT_MAX_WEIGHT, cash_buffer=DEFAULT_CASH_BUFFER,
              min_weight=DEFAULT_MIN_WEIGHT, fee=DEFAULT_FEE, slippage=DEFAULT_SLIPPAGE,
              period="day", limit=800, mark_window=MARK_WINDOW, max_symbols=30,
              max_workers=4):
    """批量 AI 研判：一次给出逐只建议 + 组合分配。

    参数
    ----
    symbols : list[dict|str]
        标的列表，元素形如 ``{"code": "600519", "market": "cn", "name": "贵州茅台"}``，
        也接受纯代码字符串（market 取 `market` 参数）。
    fetch_bars : callable(market, code, period, limit) -> list[dict]
        数据抓取器（由 server 注入，避免本模块依赖任何网络实现）。
    fetch_quote : callable(market, code) -> dict | None
        实时行情（取不到则用最后一根收盘价与相邻两根算涨跌幅）。
    market / horizon / capital / kelly_fraction / max_weight / cash_buffer
        与前端表单一一对应；比例类参数按 [0,1] 截断，非法值回退默认。
    period / limit
        K 线周期与请求根数（默认日线 800 根，足以支撑 60 根预热 + 条件分布回找）。
    mark_window / max_symbols / max_workers
        图上标记回溯根数 / 单次批量上限 / 并发线程数。

    返回
    ----
    dict：``rows`` 逐只结论（与 `web/js/views/advisor.js` 的契约一致）、
    ``portfolio`` 组合分配、``disclaimer`` 免责声明、``model`` 口径说明。
    单只失败只影响它自己那一行（``ok=False`` + ``error``），不影响其余标的。
    """
    hz = _int_arg(horizon, DEFAULT_HORIZON, 1, 250)
    mw = _param(max_weight, DEFAULT_MAX_WEIGHT)
    mw = mw if mw > 0 else DEFAULT_MAX_WEIGHT
    frac = _param(kelly_fraction, DEFAULT_FRACTION)
    if frac <= 0:
        frac = DEFAULT_FRACTION
    cb = _param(cash_buffer, DEFAULT_CASH_BUFFER)
    mn = _param(min_weight, DEFAULT_MIN_WEIGHT)
    cap = _num(capital)
    cap = cap if (cap is not None and cap > 0) else DEFAULT_CAPITAL
    fee_v = _param(fee, DEFAULT_FEE)
    slip_v = _param(slippage, DEFAULT_SLIPPAGE)
    lim = _int_arg(limit, 800, MIN_BARS, 3000)

    mkt = str(market or "cn").strip().lower()
    mkt = "us" if mkt.startswith("us") else "cn"
    syms = _normalize_symbols(symbols, mkt, _int_arg(max_symbols, 30, 1, 200))

    ctx = {
        "market": mkt, "horizon": hz, "period": period or "day", "limit": lim,
        "fetch_bars": fetch_bars, "fetch_quote": fetch_quote,
        "params": _st_registry(),
        "fraction": frac, "max_weight": mw, "min_weight": mn,
        "fee": fee_v, "slippage": slip_v,
        "mark_window": _int_arg(mark_window, MARK_WINDOW, 5, 500),
        "lot_of": lambda m: K.LOTS.get("us" if m == "us" else "cn", 100),
    }

    rows = []
    if syms:
        workers = _int_arg(max_workers, 4, 1, 8)
        if workers <= 1 or len(syms) == 1:
            for s in syms:
                try:
                    rows.append(_analyze_one(s, ctx))
                except Exception as e:  # noqa: BLE001
                    rows.append(_error_row(s, "分析异常：%s" % e))
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(syms))) as pool:
                rows = list(pool.map(
                    lambda s: _analyze_one(s, ctx), syms,
                ))

    portfolio = _portfolio(rows, cap, frac, mw, cb, mn)
    ok_count = sum(1 for r in rows if r.get("ok"))
    return {
        "ok": True,
        "market": mkt,
        "horizon": hz,
        "capital": _r(cap, 2),
        "kellyFraction": frac,
        "maxWeight": mw,
        "cashBuffer": cb,
        "requested": len(syms),
        "count": len(rows),
        "analyzed": ok_count,
        "rows": rows,
        "portfolio": portfolio,
        "disclaimer": DISCLAIMER,
        "model": {
            "score": SCORE_NOTE, "confidence": CONF_NOTE, "edge": EDGE_NOTE,
            "kelly": PORTFOLIO_NOTE,
            "forecast": (rows[0]["forecast"] or {}).get("note")
            if rows and rows[0].get("forecast") else None,
            "stance": PARAM_SOURCE,
        },
        "source": "服务端 AI 选股引擎（core/advisor.py：多策略共识 + 共识回放统计 + 凯利仓位 + 历史条件分布）",
        "updated": now_ms(),
    }


# --------------------------------------------------------------------------- #
# 十、记录持久化：把一次研判整理成可落库的结构
# --------------------------------------------------------------------------- #
#: 建议档位的「可操作性排序」：越小越靠前（用于记录列表摘要里的优先展示）
ACTION_ORDER = {"buy": 0, "add": 1, "sell": 2, "reduce": 3, "avoid": 4, "hold": 5, "watch": 6}
#: 记录摘要里的 topRows 数量上限
TOP_ROWS = 3

RECORD_NOTE = (
    "每条记录保存的是「当时这一刻的研判快照」：参数、逐只建议、凯利仓位、预测与交易计划"
    "原样落库，便于事后回看当时的判断依据与当时的价位。为避免记录无限膨胀，"
    "单条记录**不保存预测带路径**（预测带锚定在保存时的价格上，事后回看没有意义），"
    "但买卖标记与交易计划保留。记录按时间倒序保留，超出上限的自动清理，"
    "置顶记录不参与自动清理。"
)


def record_note(keep=None):
    """记录口径说明 + 保留上限。

    保留上限**不在本模块写死**：真实生效值由存储层（``Store.ADVISOR_KEEP``，可被
    环境变量 ``AD_ADVISOR_KEEP`` 覆盖）决定，服务端拿到生效值后拼进来 ——
    曾经这里硬编码过「300 条」而实际是 500，属于会误导用户的文案缺陷。
    """
    if keep:
        try:
            return RECORD_NOTE + "当前保留上限：%d 条未置顶记录（可用环境变量 AD_ADVISOR_KEEP 调整）。" % int(keep)
        except (TypeError, ValueError):
            return RECORD_NOTE
    return RECORD_NOTE


def record_id(ts=None):
    """记录 id：``ar-毫秒时间戳-4位随机后缀``（可读、可排序，且并发不冲突）。"""
    return "ar-%d-%s" % (int(ts if ts is not None else now_ms()), uuid.uuid4().hex[:4])


def _record_top(rows):
    """摘要里优先展示的 3 只：可操作性优先，同档位按评分降序。"""
    def rank(r):
        act = str(r.get("action") or "").lower()
        return (ACTION_ORDER.get(act, 9), -(_num(r.get("score")) or 0.0))

    out = []
    for r in sorted(rows, key=rank)[:TOP_ROWS]:
        k = r.get("kelly") or {}
        out.append({
            "code": r.get("code"), "name": r.get("name"), "market": r.get("market"),
            "action": r.get("action"), "actionText": r.get("actionText"),
            "score": r.get("score"), "kellyWeight": k.get("weight"),
        })
    return out


def _slim_row(row):
    """落库前裁剪逐只结论：去掉预测带路径（保留买卖标记与交易计划）。

    预测带（``advisor.forecast.path``）是 20 个点的价格序列，锚定在保存时的收盘价上，
    事后回看既不准确也无意义，却占了单行 JSON 近一半的体积，因此明确裁掉并在
    `trimmed` 上标记，避免前端误以为是「预测带为空」。
    """
    item = dict(row)
    adv = item.get("advisor")
    if isinstance(adv, dict):
        adv = dict(adv)
        fc = adv.get("forecast")
        if isinstance(fc, dict):
            adv["forecast"] = {
                "path": [], "horizon": fc.get("horizon"),
                "levels": fc.get("levels"), "trimmed": True,
            }
        item["advisor"] = adv
    return item


def to_record(res, trigger="list", note="", rid=None, ts=None, keep=None):
    """把 :func:`recommend` 的响应整理成可落库的记录 ``{"run": {...}, "rows": [...]}``。

    纯函数、不接触存储：服务端拿到记录后再交给 ``core.storage.Store.save_advisor_run``，
    这样「怎么算」与「怎么存」互不耦合，也便于单测。
    """
    res = res if isinstance(res, dict) else {}
    rows = [r for r in (res.get("rows") or []) if isinstance(r, dict)]
    created = int(ts if ts is not None else now_ms())

    counts = {k: 0 for k in ACTION_LABEL}
    for r in rows:
        act = str(r.get("action") or "").lower()
        if act in counts:
            counts[act] += 1
    actionable = counts["buy"] + counts["add"]

    payload = {k: v for k, v in res.items() if k != "rows"}
    payload["recordedAt"] = created
    payload["recordNote"] = record_note(keep)

    return {
        "run": {
            "id": rid or record_id(created),
            "createdAt": created,
            "createdDate": time.strftime("%Y-%m-%d", time.localtime(created / 1000.0)),
            "market": res.get("market"),
            "horizon": res.get("horizon"),
            "capital": res.get("capital"),
            "kellyFraction": res.get("kellyFraction"),
            "maxWeight": res.get("maxWeight"),
            "trigger": trigger,
            "note": note,
            "pinned": False,
            "symbolCount": len(rows),
            "analyzed": res.get("analyzed") or 0,
            "buyCount": actionable,
            "actionableCount": actionable,
            "totalWeight": (res.get("portfolio") or {}).get("totalWeight"),
            "source": res.get("source"),
            "summary": {
                "actions": counts,
                "codes": [{"code": r.get("code"), "market": r.get("market"),
                           "name": r.get("name")} for r in rows],
                "topRows": _record_top(rows),
            },
            "payload": payload,
        },
        "rows": [_slim_row(r) for r in rows],
    }


# --------------------------------------------------------------------------- #
# 十一、事后回看（复盘）：用之后真实发生的行情检验当时的建议
# --------------------------------------------------------------------------- #
#: 默认复盘窗口（交易日根数）
REVIEW_HORIZONS = (5, 20)
#: 哪些档位算「看多」（+1）/「看空」（-1）：命中判定就是看方向对不对
REVIEW_DIRECTION = {"buy": 1, "add": 1, "reduce": -1, "sell": -1, "avoid": -1}
#: 中性档位：不参与命中率统计（持有 / 观望本身不含方向判断）
REVIEW_NEUTRAL = ("hold", "watch")

REVIEW_NOTE = (
    "这是**事后回看**，不是回测也不是收益承诺：以上市价（日线收盘）检验当时的建议方向"
    "在随后若干交易日内是否成立。口径说明：①「命中 / 未命中」用**最短的已到期窗口**"
    "（默认 5 根）判定，未走满该窗口的一律计为「未到期」，不参与命中率；"
    "②「持有 / 观望」属中性档位，只记录实际涨跌、不计入命中率；"
    "③ 收益为价格变动，**未计手续费、印花税与滑点**，也未考虑仓位能否成交；"
    "④ 样本量有限（一条记录通常只有几只标的），单条记录的命中率没有统计意义，"
    "只有把长期记录累积起来看才有参考价值；⑤ 历史表现不代表未来。"
)


def _fwd_return(bars, idx0, k):
    """基准K线之后第 k 根的收益（相对基准收盘价，百分数）。"""
    j = idx0 + int(k)
    if idx0 < 0 or j >= len(bars) or j < 0:
        return {"ret": None, "hit": None, "ready": False, "date": None, "price": None}
    base = bars[idx0]["close"]
    if base <= 0:
        return {"ret": None, "hit": None, "ready": False, "date": None, "price": None}
    return {
        "ret": _r((bars[j]["close"] / base - 1.0) * 100.0, 3),
        "hit": None, "ready": True, "date": bars[j].get("t"),
        "price": _r(bars[j]["close"], 4),
    }


def _review_one(item, base_date, fetch_bars, horizons, limit):
    """复盘单只标的：保存价 → 最新价，以及各窗口的后续收益与命中判定。"""
    market = str(item.get("market") or "cn")
    code = str(item.get("code") or "")
    # fwd 先按「全部未到期」初始化：取不到行情的标的也保持同样的结构，
    # 前端读 fwd['5'].ret 永远不会拿到 undefined（这类键缺失最难排查）
    out = {
        "code": code, "name": item.get("name"), "market": market,
        "action": item.get("action"), "actionText": item.get("actionText"),
        "weight": (item.get("kelly") or {}).get("weight"),
        "savedPrice": _num(item.get("price")), "savedAt": base_date,
        "baseDate": None, "basePrice": None, "refPrice": None,
        "lastPrice": None, "lastDate": None, "barsElapsed": 0,
        "sinceReturn": None, "verdict": "nodata",
        "verdictHorizon": None, "verdictReturn": None, "contribution": None,
        "note": None,
        "fwd": {str(int(k)): _fwd_return([], 0, k) for k in horizons},
    }
    if not code:
        out["note"] = "缺少标的代码"
        return out
    try:
        bars = _clean_bars(fetch_bars(market, code, "day", limit) or [])
    except Exception as e:  # noqa: BLE001  单只失败不影响整条记录的复盘
        out["note"] = "行情获取失败：%s" % e
        return out
    if not bars:
        out["note"] = "无可用日线数据"
        return out

    day = str(base_date or "")[:10]
    # 保存当天可能是非交易日：取「最后一个日期不晚于保存日」的K线作为基准
    cands = [i for i, b in enumerate(bars)
             if day and str(b.get("t") or "")[:10] <= day]
    idx0 = max(cands) if cands else 0
    base_close = bars[idx0]["close"]
    if base_close <= 0:
        out["note"] = "基准K线价格无效"
        return out

    ref = _num(item.get("price"))
    ref = ref if (ref is not None and ref > 0) else base_close
    out.update({
        "baseDate": bars[idx0].get("t"), "basePrice": _r(base_close, 4),
        "refPrice": _r(ref, 4), "lastPrice": _r(bars[-1]["close"], 4),
        "lastDate": bars[-1].get("t"), "barsElapsed": len(bars) - 1 - idx0,
        "sinceReturn": _r((bars[-1]["close"] / ref - 1.0) * 100.0, 3),
    })

    fwd = {}
    for k in horizons:
        fwd[str(int(k))] = _fwd_return(bars, idx0, k)
    out["fwd"] = fwd

    act = str(item.get("action") or "").lower()
    direction = REVIEW_DIRECTION.get(act)
    ready = None
    for k in sorted(int(x) for x in horizons):
        if (fwd.get(str(k)) or {}).get("ready"):
            ready = k
            break
    if act in REVIEW_NEUTRAL:
        out["verdict"] = "neutral"
        out["note"] = "中性档位（%s），只记录实际涨跌，不计入命中率" % ACTION_LABEL.get(act, act)
    elif direction is None:
        out["verdict"] = "nodata"
        out["note"] = "当时未给出可执行档位（数据不足或已回避风险）"
    elif ready is None:
        out["verdict"] = "pending"
        out["note"] = "尚未走满最短判定窗口（%d 根），当前仅 %d 根" % (
            min(int(x) for x in horizons), out["barsElapsed"])
    else:
        ret = fwd[str(ready)]["ret"]
        hit = bool(ret is not None and ((ret > 0) if direction > 0 else (ret < 0)))
        fwd[str(ready)]["hit"] = hit
        out.update({"verdict": "hit" if hit else "miss", "verdictHorizon": ready,
                    "verdictReturn": ret})
    w = _num(out.get("weight")) or 0.0
    if out["sinceReturn"] is not None and w > 0:
        # 账户口径贡献：权重 × 收益（两者都是百分数，乘积仍是百分数）
        out["contribution"] = _r(w * out["sinceReturn"], 4)
    return out


def review(record, fetch_bars, horizons=REVIEW_HORIZONS, limit=800, max_workers=4):
    """对一条已保存的记录做事后回看。

    参数
    ----
    record : dict
        记录（``Store.get_advisor_run`` 的返回值，或 ``to_record`` 的 ``run``+``rows``
        合并体）。只需要 ``createdDate`` 与 ``rows``。
    fetch_bars : callable(market, code, period, limit) -> list[dict]
        与 :func:`recommend` 同一份数据抓取器。
    horizons : tuple[int]
        复盘窗口（交易日根数），默认 (5, 20)。命中判定只用**最短的已到期窗口**，
        保证同一条记录内不同标的口径一致。
    limit / max_workers
        单只标的请求的K线根数 / 并发线程数。

    返回
    ----
    dict：``rows`` 逐只复盘明细、``summary`` 汇总（命中率按看多组 / 看空组分开统计，
    另给出已建仓部分的加权收益与占本金收益）、``note`` 口径说明。
    任何异常都不抛出：取不到行情的标的一律记为 ``verdict='nodata'`` 并写明原因。
    """
    record = record if isinstance(record, dict) else {}
    rows = [r for r in (record.get("rows") or []) if isinstance(r, dict)]
    base_date = record.get("createdDate") or record.get("created_at") or ""

    hs = []
    probe = horizons if isinstance(horizons, (list, tuple)) else [horizons]
    for h in probe:
        v = _num(h)
        if v is not None and int(v) >= 1:
            hs.append(int(v))
    hs = sorted(set(hs)) or list(REVIEW_HORIZONS)
    lim = _int_arg(limit, 800, MIN_BARS, 3000)

    if not rows:
        return {"ok": False, "id": record.get("id"), "asOf": None, "horizons": hs,
                "rows": [], "summary": {}, "note": record_note(),
                "message": "该记录没有可复盘的标的"}

    workers = _int_arg(max_workers, 4, 1, 8)
    if len(rows) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(rows))) as pool:
            items = list(pool.map(
                lambda it: _review_one(it, base_date, fetch_bars, hs, lim), rows))
    else:
        items = [_review_one(it, base_date, fetch_bars, hs, lim) for it in rows]

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return _r(sum(vals) / len(vals), 3) if vals else None

    def rate(group):
        return _r(len([r for r in group if r["verdict"] == "hit"]) / len(group), 4) if group else None

    graded = [r for r in items if r["verdict"] in ("hit", "miss")]
    hits = [r for r in graded if r["verdict"] == "hit"]
    bearish = [r for r in graded if str(r.get("action") or "").lower() in ("reduce", "sell", "avoid")]
    bullish = [r for r in graded if str(r.get("action") or "").lower() in ("buy", "add")]
    withret = [r for r in items if r.get("sinceReturn") is not None]
    contrib = [r for r in withret if (r.get("weight") or 0) > 0]
    wsum = sum((r.get("weight") or 0.0) for r in contrib)
    account = sum((r.get("contribution") or 0.0) for r in contrib)
    last_dates = [r["lastDate"] for r in items if r.get("lastDate")]

    summary = {
        "total": len(items),
        "ready": len(graded),
        "pending": len([r for r in items if r["verdict"] == "pending"]),
        "neutral": len([r for r in items if r["verdict"] == "neutral"]),
        "nodata": len([r for r in items if r["verdict"] == "nodata"]),
        "hits": len(hits),
        "misses": len(graded) - len(hits),
        "hitRate": rate(graded),
        "horizon": min(hs),
        "avgReturn": avg([r["sinceReturn"] for r in withret]),
        "avgHitReturn": avg([r["sinceReturn"] for r in hits]),
        "avgMissReturn": avg([r["sinceReturn"] for r in graded if r["verdict"] == "miss"]),
        "bullHitRate": rate(bullish),
        "bearHitRate": rate(bearish),
        "bullCount": len(bullish),
        "bearCount": len(bearish),
        "positionReturn": _r(account / wsum, 3) if wsum > 0 else None,
        "accountReturn": _r(account, 3),
        "totalWeight": _r(wsum, 6),
    }
    return {
        "ok": True,
        "id": record.get("id"),
        "asOf": max(last_dates) if last_dates else None,
        "baseDate": base_date or None,
        "horizons": hs,
        "rows": items,
        "summary": summary,
        "note": REVIEW_NOTE,
    }
