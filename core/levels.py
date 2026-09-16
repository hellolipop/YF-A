# -*- coding: utf-8 -*-
"""AlphaDesk · 买卖点位引擎（core/levels.py）

定位
----
`core/advisor.py` 回答「该买谁、该不该买」，`core/kelly.py` 回答「按统计优势给多少仓位」。
本模块回答第三个问题：**「具体在哪个价买、哪个价卖、错了在哪里认错、什么条件下离场」**——
把「买点 / 卖点 / 止损 / 止盈 / 分批 / 风险预算 / 退出判据」做成一组可复核的数字，
每个数字都能说清「它是怎么来的」（`basis` / `note` / `warnings`）。

设计口径与依据（本模块所有默认值都能追溯到下面几条）
----------------------------------------------------
1. **止损用波动率自适应，不用固定百分比**。Chandelier Exit 的标准形态是
   ``最高价(n) − m × ATR(n)``，默认 ``n = 22、m = 3.0``（nexusfi / equitiesindia 的
   Chandelier 口径）。同一段行情下的实证对比（**厂商级回测，非同行评审，仅供量级参考**）：

   ==============================  ==========  ==========
   止损口径                        胜率        盈亏比
   ==============================  ==========  ==========
   固定 $3 止损                    38.2%       2.7
   ATR 2.0×                        47.6%       2.1
   Chandelier(22, 3)               51.3%       1.9
   ==============================  ==========  ==========

   结论：波动率自适应止损在**期望值**上更好（固定金额止损在高波动期被频繁扫损、
   在低波动期又过宽）；但**移动止损天然胜率更低**——它会在震荡里被反复触发，
   代价换来的是趋势中回吐更少。这是一笔明确的取舍，不是「更优」的单向改进。
   本模块因此同时给出两档：默认 ``2.0×ATR(14)``（更紧：胜率更低、单笔亏损更小）
   与 Chandelier(22, 3)（更宽：胜率更高、单笔亏损更大），并取**更紧者**作为初始止损。
   前提是 Chandelier 必须位于现价**下方**：如果它已经贴住或高于现价（= 现价已跌破
   ``最高价(22) − 3×ATR(22)``），那就说明结构上已经处于「应离场」区间，此时它不能
   当新仓的初始止损（那是「开仓即离场」），只作结构参照并记一条 warning。
2. **三段式止损（freqtrade 的标准形态）**：``initial`` → ``trailStart`` → ``trailLockIn``。
   等价于 freqtrade 的 ``trailing_stop_positive_offset``（涨到该偏移后才开始移动）
   + ``trailing_stop_positive``（移动后保留的回撤距离）
   + ``trailing_only_offset_is_reached = True``（没到偏移就不移动）。
   本模块把这三件事翻译成三个**价格**：涨破 ``trailStart`` 后，止损抬到 ``entry`` 上方
   （``trailLockIn``）并随价格上移；没涨到 ``trailStart`` 之前，止损一直是 ``initial``。
3. **支撑阻力多源交叉，并标注强度**：枢轴点（机械计算、无主观）、摆动高低点（前高前低）、
   成交量密集区（近似筹码分布：按价格分箱统计成交量占比，取占比最高的 2~3 个箱中心）、
   均线（MA20 / MA60）。四个来源各自给出候选价，再按 ``mergeAtr × ATR`` 的容差合并；
   **多个来源重叠的价位更有效**（合并后来源数 ≥ 2 会被显式标注）。
   强度 = 被触及次数 + 成交量占比 + 距现价远近 + 来源数 → 强 / 中 / 弱。
4. **分批建仓（无现成开源标准，本模块自定并说明理由）**：一笔买入很难同时满足
   「不错过」与「不追高」，所以拆成 2~3 档：首档 50% 在现价（试探，先建立仓位与纪律），
   次档 30% 在 ``−1.0×ATR`` 或**首个支撑**（回踩确认），末档 20% 在 ``−1.5×ATR`` 或
   **第二支撑**（左侧补仓）。权重 5:3:2 的理由：越靠近现价的档位越确定、越深的档位
   越依赖行情继续给机会，因此权重随深度递减。硬约束：**所有建仓档都必须高于初始止损**，
   否则该档被剔除（并把权重归一化到剩下的档），同时写一条 warning —— 把仓位买在止损之下
   等于自己放大风险。
5. **仓位由风险反算，而不是只靠凯利**：
   ``shares = floor(capital × risk_pct / (entry − stop) / lot) × lot``，
   默认 ``risk_pct = 1%``（Van Tharp 的 1% 规则）。1% 规则的数学含义：
   每次用「本金 × 1%」去买「每股风险」，因此单笔最大亏损被钉死在 1%；
   连续 10 次全额止损的累计回撤约 ``1 − 0.99^10 ≈ 9.6%``（这就是它被广泛使用的原因）。
   **失效边界**（必须在 warnings / basis 里写清，否则用户会误以为 1% 是硬约束）：
   不防跳空缺口、不防滑点与流动性枯竭、不防多标的**相关性同时爆发**（10 只同板块标的
   各 1% 风险，实际是一笔 10% 的风险），也不防涨跌停无法成交。
   与 `core/kelly.py` 的关系：凯利回答「按统计优势该给多少」，1% 规则回答「按价格结构
   该给多少」，两者是**两道独立闸门**，最终手数取小者（本模块只负责第二道）。
6. **优先级式退出**（第一个触发者胜出，参考 Penny 的 7 级退出实践）：
   ①RSI > 70 且 MACD 死叉（动量衰竭）②移动止损（自最高价回撤 X%）
   ③放量杀跌（量 > 4×5 日均量且当日跌 > 5%）④跌破 20 日低点（支撑破位）
   ⑤止盈目标 ⑥滞涨（持有 N 天且收益 < 阈值）⑦ATR 止损。
   每条都给 ``priority / rule / condition / action``，另外附 ``triggered / detail``：
   把「是哪一条规则触发的」直接说清楚，比只丢一个价格有用得多。
7. **风险提示不是装饰**：涨跌停无法成交、T+1 当日买入不可卖出、跳空缺口让实际止损价
   远差于计划价、样本不足与点位失效、以及「点位是概率分布的产物、不是承诺」——
   这五条是 `warnings` 的固定组成部分（无论行情好坏都会返回）。
   涨跌停价判断按约定配合 `core/rules.py`：该模块存在且提供 ``limit_prices()`` 时自动
   引用其数字，否则只输出文案（见 :func:`_limit_hint`）。
8. **样本与精度**：``confidence`` 按参与点位统计的K线根数给 低 / 中 / 高（60 / 120 根为界），
   并给出样本数。「样本不足时不要给两位小数的精确价位」——样本 < 60 根时，所有价位
   精度降到 **0.1 元**（小数位 1），并在 note 里标注「参考」，避免假精确。
9. **数据时效**：``asOf`` 取**最后一根K线的时间**（不是 ``time.time()``），
   调用方据此判断「这份点位对应哪一根K线」。

与同包其它模块的口径关系（避免两套口径打架）
--------------------------------------------
· `core/indicators.py`：ATR / RSI / MACD / SMA 一律复用，本模块**不重复实现**任何指标；
  清洗K线沿用 `core/advisor.py` `_clean_bars()` 的口径（只丢脏根、不重排），
  因为跨模块导入对方的私有函数会让两份实现互相牵制，所以这里按同一口径独立实现一份。
· `core/advisor.py` `_plan()`：研判页的计划口径是「止损 = max(现价 − 1.5×ATR(14), 现价×0.88)，
  目标 = max(1.5×ATR, 条件分布 75% 分位) / max(3×ATR, 95% 分位)」。本模块是同一波动率尺度上的
  **点位引擎版**：默认止损取 2.0×ATR(14)（调研里的「更紧档」）并沿用 advisor 的 **−12% 上限**；
  目标位只用 ATR 倍数（不引入 forecast，保持点位模块的纯技术口径），默认首目标 4.0×ATR（配 2.0×ATR
  的止损即 2:1）。它与 advisor 的目标**同源于波动率尺度、方向一致**，但两者不互为上下限：
  advisor 会在条件分布给出更高分位时把目标上移（本模块不读 forecast），因此同一标的可能出现
  「advisor T1 = q75 > 本模块 T1」或相反的情况 —— 需要严格一致时，调用方要么把
  ``params["targetAtr"]`` 对齐，要么直接把 forecast 的分位价传进来。
  止损要对齐研判页时，把 ``params["atrMult"] = 1.5`` 即可。
· `core/kelly.py`：本模块**不调用**凯利，只在 note 里说明两者取小；这样两个模块都能独立替换。

不做什么（保持诚实）
--------------------
· **任何输入都不抛异常**：返回 dict / list；数据不足时 ``ok = False`` + 中文 ``error``，
  并且**不臆造任何价位**（失败返回值里没有 support / entries / stop 等字段）。
· 不预测涨跌、不承诺点位；不做涨跌停 / T+1 / 停牌 / 做空的成交模拟（只给文案）；
· 不算手续费与滑点（成本由 `core/fills.py` 负责）。

纯标准库实现，仅复用同包 `core.indicators`，无网络、无第三方依赖。
"""

from __future__ import annotations

from .indicators import ATR, MACD, RSI, SMA

__all__ = [
    "DEFAULT_LEVELS", "MIN_BARS",
    "plan_levels", "pivot_points", "atr_stops", "swing_levels",
    "volume_nodes", "risk_budget", "exit_rules",
]

_INF = float("inf")

#: 低于该有效K线根数即认为无法给出可靠点位（ok = False）
MIN_BARS = 30

#: 默认参数表。键名同时支持 camelCase 与 snake_case（例如 atrMult / atr_mult）。
#: 全部可被 ``params`` 覆盖，非法值回退默认并截断到合理区间。
DEFAULT_LEVELS = {
    # —— 波动率与止损 ——
    "atrN": 14,                # ATR 周期（与 core/advisor.py 一致）
    "atrMult": 2.0,            # 更紧档：2.0×ATR(14)（调研中的「ATR 2.0×」口径）
    "chandelierN": 22,         # Chandelier Exit 的 n（最高价窗口 = ATR 周期）
    "chandelierMult": 3.0,     # Chandelier Exit 的 m
    "maxStopPct": 0.12,        # 止损距离上限 −12%（与 core/advisor.py 一致）
    "minStopPct": 0.005,       # 止损距现价的最小比例（防止结构价高于现价）
    # —— 三段式移动止损（freqtrade 等价形态）——
    "trailStartMult": 2.0,     # trailStart = 现价 + 2.0×ATR
    "trailLockMult": 1.0,      # trailLockIn = trailStart − 1.0×ATR（即成本上方 1.0×ATR）
    "trailPct": 0.08,          # 退出规则②：自持仓最高价回撤 8% 离场
    # —— 支撑阻力来源 ——
    "swingLookback": 120,
    "swingTop": 4,
    "swingK": 3,               # 局部极值的左右半宽（严格极值）
    "volumeLookback": 120,
    "volumeBins": 24,
    "volumeTop": 3,
    "mergeAtr": 0.35,          # 多源价位合并容差（×ATR）
    "levelTop": 4,             # 支撑 / 阻力各保留前几名（按距现价远近）
    # —— 分批建仓 / 分批止盈（最多 3 档）——
    "entryWeights": (0.5, 0.3, 0.2),
    "entryAtr": (0.0, 1.0, 1.5),      # 距现价的 ATR 倍数
    "targetWeights": (0.4, 0.35, 0.25),
    # 首目标刻意取 4.0×ATR：默认止损是 2.0×ATR，T1/T2 才刚好落在 2:1 / 3:1 ——
    # 「止损 2×ATR + 目标 2×ATR」会让盈亏比恒等于 1:1、结论永远是「偏低」，
    # 那是自相矛盾的默认值（保留各档倍数本身可调）。
    "targetAtr": (4.0, 6.0, 9.0),
    "anchorSupport": True,            # 次/末档是否锚定支撑位（测试可关掉以隔离口径）
    # —— 风险预算（1% 规则）——
    "capital": 100000.0,       # 未传 capital 时的演示本金（只用于算股数，10 万口径）
    "riskPct": 0.01,
    "lot": 100,
    "maxWeight": 0.25,         # 单标的上限（与 core/kelly.py DEFAULT_MAX_WEIGHT 一致）
    # —— 退出判据 ——
    "rsiN": 14,
    "rsiOverbought": 70.0,
    "volMult": 4.0,
    "volDropPct": 0.05,
    "lowN": 20,
    "stagnantDays": 20,        # 默认与 horizon 对齐（见 _options）
    "stagnantRet": 0.02,
    # —— 样本与精度 ——
    "sampleMid": 60,           # < 60 根 → 低置信度 + 0.1 元精度
    "sampleFull": 120,         # >= 120 根 → 高置信度
    "precision": 4,
    "precisionLow": 1,
    "minBars": MIN_BARS,
}


# --------------------------------------------------------------------------- #
# 基础工具（与 core/advisor.py 同一套口径：出口一律清洗，绝不让 NaN / inf 进 JSON）
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
    if v != v or v == _INF or v == -_INF:
        return None
    return v


def _r(v, nd=4):
    """四舍五入；无效值返回 None（出口统一清洗）。"""
    n = _num(v)
    return None if n is None else round(n, nd)


def _clean(v):
    """消除 -0.0，避免污染快照对比。"""
    n = _num(v)
    return None if n is None else (0.0 if n == 0 else float(n))


def _pct(v, nd=2):
    """小数 → 百分比字符串（仅用于中文说明文案）。"""
    n = _num(v)
    return "—" if n is None else ("%.*f%%" % (nd, n * 100.0))


def _fmt(v, nd=4):
    """数值 → 简短字符串（去尾零），仅用于文案。

    只在有小数点时去尾零：``nd = 0`` 时把 ``10000000`` 去成 ``1`` 是实测踩过的坑。
    """
    n = _num(v)
    if n is None:
        return "—"
    s = "%.*f" % (nd, n)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def _int_p(v, dflt, lo, hi):
    """宽松取整：解析失败用 dflt，结果截断到 [lo, hi]。"""
    x = _num(v)
    if x is None:
        x = dflt
    try:
        iv = int(x)
    except (TypeError, ValueError, OverflowError):
        iv = int(dflt)
    return max(lo, min(hi, iv))


def _float_p(v, dflt, lo, hi):
    """宽松取浮点：解析失败用 dflt，结果截断到 [lo, hi]。"""
    x = _num(v)
    if x is None:
        x = dflt
    return max(lo, min(hi, x))


def _ratio(v, dflt):
    """比例类参数：非法回退默认值，数值截断到 [0, 1]。"""
    x = _num(v)
    if x is None:
        x = dflt
    return max(0.0, min(1.0, x))


def _tup_p(v, dflt):
    """数值序列参数：逐位宽松解析，非法位回退默认；长度以默认值为准（最多 3 档）。"""
    seq = v if isinstance(v, (list, tuple)) else dflt
    out = []
    for i, d in enumerate(dflt):
        x = _num(seq[i]) if (isinstance(seq, (list, tuple)) and i < len(seq)) else None
        out.append(d if x is None else x)
    return tuple(out)


def _snake(name):
    """camelCase → snake_case（前端两种写法都要能收）。"""
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _get(src, key):
    """按 camelCase / snake_case 两种写法取值（两者都缺 → None）。"""
    for k in (key, _snake(key)):
        if k in src and src[k] is not None:
            return src[k]
    return None


def _last_valid(seq):
    """序列中最后一个有效数值（没有则 None）。"""
    for v in reversed(list(seq or [])):
        if _num(v) is not None:
            return float(v)
    return None


def _clean_bars(bars):
    """清洗K线：只保留 OHLC 齐全、价格为正的记录（口径与 core/advisor.py 一致）。

    **不重排顺序**（与 `core/forecast.py` / `core/advisor.py` 同一取舍）：
    上游 `api_kline` 已保证「旧 → 新」；按时间字段重排会在时间格式不规范时把正确序列
    打乱、制造出假的巨幅跳空。缺 high / low / open 时用 close 兜底补齐。
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
        out.append({"t": b.get("t"), "code": b.get("code") or b.get("symbol"),
                    "open": o, "high": max(h, c, o), "low": min(l, c, o), "close": c,
                    "volume": _num(b.get("volume")) or 0.0})
    return out


def _fail(msg):
    """失败出口：**只给原因，不给任何价位**（绝不臆造）。"""
    return {"ok": False, "error": str(msg)}


def _fix_weights(items, nd=4):
    """把权重归一化到和为 1（把浮点残差给权重最大的那一档，保证前端可复算）。"""
    if not items:
        return items
    total = 0.0
    for it in items:
        total += it["weight"]
    if total <= 0:
        w = round(1.0 / len(items), nd)
        for it in items:
            it["weight"] = w
        return items
    for it in items:
        it["weight"] = round(it["weight"] / total, nd)
    idx = max(range(len(items)), key=lambda k: items[k]["weight"])
    rest = 0.0
    for j, it in enumerate(items):
        if j != idx:
            rest += it["weight"]
    items[idx]["weight"] = _clean(round(1.0 - rest, nd))
    return items


def _options(params, hz):
    """把用户 params 归一成内部参数表：非法回退默认、超界截断、未知键忽略。"""
    src = params if isinstance(params, dict) else {}

    def g(k):
        return _get(src, k)

    p = {
        "horizon": hz,
        "atrN": _int_p(g("atrN"), DEFAULT_LEVELS["atrN"], 2, 250),
        "atrMult": _float_p(g("atrMult"), DEFAULT_LEVELS["atrMult"], 0.1, 10.0),
        "chandelierN": _int_p(g("chandelierN"), DEFAULT_LEVELS["chandelierN"], 3, 250),
        "chandelierMult": _float_p(g("chandelierMult"), DEFAULT_LEVELS["chandelierMult"], 0.1, 10.0),
        "maxStopPct": _ratio(g("maxStopPct"), DEFAULT_LEVELS["maxStopPct"]),
        "minStopPct": _ratio(g("minStopPct"), DEFAULT_LEVELS["minStopPct"]),
        "trailStartMult": _float_p(g("trailStartMult"), DEFAULT_LEVELS["trailStartMult"], 0.1, 20.0),
        "trailLockMult": _float_p(g("trailLockMult"), DEFAULT_LEVELS["trailLockMult"], 0.0, 20.0),
        "trailPct": _ratio(g("trailPct"), DEFAULT_LEVELS["trailPct"]),
        "swingLookback": _int_p(g("swingLookback"), DEFAULT_LEVELS["swingLookback"], 10, 600),
        "swingTop": _int_p(g("swingTop"), DEFAULT_LEVELS["swingTop"], 1, 10),
        "swingK": _int_p(g("swingK"), DEFAULT_LEVELS["swingK"], 1, 10),
        "volumeLookback": _int_p(g("volumeLookback"), DEFAULT_LEVELS["volumeLookback"], 10, 600),
        "volumeBins": _int_p(g("volumeBins"), DEFAULT_LEVELS["volumeBins"], 5, 200),
        "volumeTop": _int_p(g("volumeTop"), DEFAULT_LEVELS["volumeTop"], 1, 10),
        "mergeAtr": _float_p(g("mergeAtr"), DEFAULT_LEVELS["mergeAtr"], 0.0, 5.0),
        "levelTop": _int_p(g("levelTop"), DEFAULT_LEVELS["levelTop"], 1, 10),
        "entryWeights": _tup_p(g("entryWeights"), DEFAULT_LEVELS["entryWeights"]),
        "entryAtr": _tup_p(g("entryAtr"), DEFAULT_LEVELS["entryAtr"]),
        "targetWeights": _tup_p(g("targetWeights"), DEFAULT_LEVELS["targetWeights"]),
        "targetAtr": _tup_p(g("targetAtr"), DEFAULT_LEVELS["targetAtr"]),
        "capital": _float_p(g("capital"), DEFAULT_LEVELS["capital"], 0.0, 1e15),
        "riskPct": _ratio(g("riskPct"), DEFAULT_LEVELS["riskPct"]),
        "lot": _int_p(g("lot"), DEFAULT_LEVELS["lot"], 1, 100000),
        "maxWeight": _ratio(g("maxWeight"), DEFAULT_LEVELS["maxWeight"]),
        "rsiN": _int_p(g("rsiN"), DEFAULT_LEVELS["rsiN"], 2, 100),
        "rsiOverbought": _float_p(g("rsiOverbought"), DEFAULT_LEVELS["rsiOverbought"], 50.0, 100.0),
        "volMult": _float_p(g("volMult"), DEFAULT_LEVELS["volMult"], 1.0, 50.0),
        "volDropPct": _ratio(g("volDropPct"), DEFAULT_LEVELS["volDropPct"]),
        "lowN": _int_p(g("lowN"), DEFAULT_LEVELS["lowN"], 2, 250),
        "stagnantDays": _int_p(g("stagnantDays"), max(hz, 1), 1, 250),
        "stagnantRet": _float_p(g("stagnantRet"), DEFAULT_LEVELS["stagnantRet"], -1.0, 10.0),
        "sampleMid": _int_p(g("sampleMid"), DEFAULT_LEVELS["sampleMid"], 2, 10 ** 6),
        "sampleFull": _int_p(g("sampleFull"), DEFAULT_LEVELS["sampleFull"], 3, 10 ** 6),
        "precision": _int_p(g("precision"), DEFAULT_LEVELS["precision"], 0, 6),
        "precisionLow": _int_p(g("precisionLow"), DEFAULT_LEVELS["precisionLow"], 0, 6),
        "minBars": _int_p(g("minBars"), DEFAULT_LEVELS["minBars"], 2, 10 ** 6),
        "market": str(g("market") or "").strip().lower(),
        "code": g("code"),
        "position": g("position") if isinstance(g("position"), dict) else None,
    }
    anchor = g("anchorSupport")
    p["anchorSupport"] = bool(anchor) if isinstance(anchor, bool) else DEFAULT_LEVELS["anchorSupport"]
    return p


# --------------------------------------------------------------------------- #
# 一、枢轴点（Pivot Points，机械计算、无主观）
# --------------------------------------------------------------------------- #
def pivot_points(high, low, close):
    """经典枢轴点（Floor Trader Pivots）：由**上一段**（这里取最后一根）K线的 H / L / C 推出 7 个价位。

    .. code-block:: text

        PP = (H + L + C) / 3
        R1 = 2·PP − L        S1 = 2·PP − H
        R2 = PP + (H − L)     S2 = PP − (H − L)
        R3 = H + 2·(PP − L)   S3 = L − 2·(H − PP)

    口径说明：枢轴点是**纯机械**计算（没有主观、没有参数），因此它是多源交叉里
    最「中立」的一票；本模块用它做当日内的第一层参照，摆动高低点与成交量密集区
    负责补上「结构」与「筹码」两个维度。

    参数
    ----
    high / low / close : float
        同一根K线的最高价 / 最低价 / 收盘价（要求 > 0；``high < low`` 时自动互换）。

    返回
    ----
    dict
        ``pp / r1 / r2 / r3 / s1 / s2 / s3``（7 个价位）+ ``valid`` / ``high`` /
        ``low`` / ``close`` / ``note``；输入非法时 7 个价位全为 ``None``、
        ``valid = False``，并在 ``note`` 里说明原因（不抛异常）。
    """
    h, l, c = _num(high), _num(low), _num(close)
    note = ("枢轴点口径：PP = (H + L + C) / 3，R1 = 2PP − L，S1 = 2PP − H，"
            "R2 = PP + (H − L)，S2 = PP − (H − L)，R3 = H + 2(PP − L)，"
            "S3 = L − 2(H − PP)；纯机械计算、无主观参数，常与摆动高低点 / 量能密集区交叉验证。")
    if h is None or l is None or c is None or h <= 0 or l <= 0 or c <= 0:
        return {"pp": None, "r1": None, "r2": None, "r3": None,
                "s1": None, "s2": None, "s3": None,
                "valid": False, "high": None, "low": None, "close": None,
                "note": "枢轴点输入非法（H / L / C 需为大于 0 的数值）：" + note}
    if h < l:
        h, l = l, h
    pp = (h + l + c) / 3.0
    out = {
        "pp": _clean(pp),
        "r1": _clean(2 * pp - l),
        "r2": _clean(pp + (h - l)),
        "r3": _clean(h + 2 * (pp - l)),
        "s1": _clean(2 * pp - h),
        "s2": _clean(pp - (h - l)),
        "s3": _clean(l - 2 * (h - pp)),
        "valid": True, "high": h, "low": l, "close": c,
        "note": note,
    }
    return out


def _pivot7(pv):
    """从 :func:`pivot_points` 的结果里取固定 7 个字段（接口契约，字段名不可变）。"""
    return {"pp": pv.get("pp"), "r1": pv.get("r1"), "r2": pv.get("r2"), "r3": pv.get("r3"),
            "s1": pv.get("s1"), "s2": pv.get("s2"), "s3": pv.get("s3")}


# --------------------------------------------------------------------------- #
# 二、Chandelier Exit（波动率自适应止损）
# --------------------------------------------------------------------------- #
def atr_stops(bars, params=None):
    """波动率自适应止损：Chandelier Exit = ``最高价(n) − m × ATR(n)``，默认 ``n = 22、m = 3.0``。

    为什么不用固定百分比止损：固定 3 元/固定 2% 在不同波动率下含义完全不同——高波动期
    被频繁扫损、低波动期又形同虚设。Chandelier Exit 把止损挂在「近期最高价往下 m 倍 ATR」
    处，让止损距离随波动率伸缩。厂商级回测（**非同行评审**）：固定 $3 止损 胜率 38.2% /
    盈亏比 2.7，ATR 2.0× 47.6% / 2.1，Chandelier(22, 3) 51.3% / 1.9 —— 波动率自适应在
    **期望值**上更好；同时要接受「移动止损天然胜率更低、但趋势中回吐更少」的取舍。

    本函数同时给出**更紧的备选** ``2.0×ATR(14)``：更紧的止损单笔亏损更小、但胜率更低
    （更容易被正常波动扫掉）。选哪一档取决于持有周期与对「频繁小亏」的耐受度，
    本模块的 `plan_levels` 默认取两者中**更紧者**作为初始止损。

    参数
    ----
    bars : list[dict]
        K线（旧 → 新）；只需 high / low / close。
    params : dict, optional
        覆盖 ``chandelierN`` / ``chandelierMult`` / ``atrN`` / ``atrMult``。

    返回
    ----
    dict
        ``ok``；``chandelier`` 与 ``highest``（窗口最高价）、``atr``（ATR(n)）、``n``、``mult``；
        ``tight`` = {price, atr, n, mult, basis}（2.0×ATR(14) 备选，price 锚定最后一根收盘价）；
        ``basis`` / ``note``（含实证数字与取舍说明）。K线不足或 ATR 不可用时 ``ok = False``。
    """
    try:
        p = _options(params, 20)
        clean = _clean_bars(bars)
        n = p["chandelierN"]
        if len(clean) < max(2, min(n, 5)):
            return {"ok": False, "error": "有效K线不足（%d 根），无法计算 Chandelier Exit" % len(clean),
                    "chandelier": None, "highest": None, "atr": None, "tight": None,
                    "n": n, "mult": p["chandelierMult"], "basis": "", "note": _ATR_NOTE}
        last_close = clean[-1]["close"]
        win = clean[-n:]
        highest = max(b["high"] for b in win)
        atr_c = _last_valid(ATR(clean, n))
        atr_t = _last_valid(ATR(clean, p["atrN"]))
        if atr_c is None or atr_c <= 0:
            return {"ok": False, "error": "ATR(%d) 不可用或为 0，无法计算 Chandelier Exit" % n,
                    "chandelier": None, "highest": highest, "atr": None, "tight": None,
                    "n": n, "mult": p["chandelierMult"], "basis": "", "note": _ATR_NOTE}
        chandelier = highest - p["chandelierMult"] * atr_c
        tight = None
        if atr_t is not None and atr_t > 0:
            tight = {
                "price": _clean(last_close - p["atrMult"] * atr_t),
                "atr": _clean(atr_t), "n": p["atrN"], "mult": p["atrMult"],
                "basis": "%s×ATR(%d)（更紧档：胜率更低、单笔亏损更小）"
                         % (_fmt(p["atrMult"]), p["atrN"]),
            }
        return {
            "ok": True,
            "chandelier": _clean(chandelier),
            "highest": _clean(highest),
            "atr": _clean(atr_c),
            "n": n, "mult": p["chandelierMult"],
            "tight": tight,
            "basis": "Chandelier Exit = 最高价(%d) %.4f − %s×ATR(%d) %.4f = %.4f"
                     % (n, highest, _fmt(p["chandelierMult"]), n, atr_c, chandelier),
            "note": _ATR_NOTE,
        }
    except Exception as e:                                   # 出口兜底：绝不抛异常
        return {"ok": False, "error": "内部异常：%s" % (e,), "chandelier": None,
                "highest": None, "atr": None, "tight": None,
                "n": None, "mult": None, "basis": "", "note": _ATR_NOTE}


_ATR_NOTE = (
    "波动率自适应止损口径：Chandelier Exit = 最高价(n) − m×ATR(n)（默认 n=22、m=3.0），"
    "紧档为 2.0×ATR(14)。依据：厂商级回测（非同行评审，仅作量级参考）显示固定 $3 止损 "
    "胜率 38.2% / 盈亏比 2.7，ATR 2.0× 为 47.6% / 2.1，Chandelier(22,3) 为 51.3% / 1.9 —— "
    "波动率自适应在期望值上更好；代价是移动止损天然胜率更低（震荡里被反复触发），"
    "换来趋势中回吐更少。更紧的止损（2.0×ATR）胜率更低但单笔亏损更小，"
    "两档都保留、默认取更紧者，调用方可按持有周期自行切换。"
)


# --------------------------------------------------------------------------- #
# 三、摆动高低点（前高 / 前低）
# --------------------------------------------------------------------------- #
def swing_levels(bars, lookback=120, top=4, k=None):
    """前高 / 前低（摆动高低点）：窗口内**严格**局部极值。

    定义：第 i 根是摆动高点，当且仅当它的 high **严格大于**左右各 ``k`` 根（默认 3 根）的
    high；摆动低点同理用 low 严格小于。用严格不等号（而不是 ≥）可以避免把横盘平台上
    任意一根都标成「前高」，这也是实测里最容易被忽略的坑。

    排序口径：``highs`` 按价格**从高到低**、``lows`` 按价格**从低到高**——
    前高/前低的首要用途是找最强的结构参照位（「上次跌到哪」比「最近的摆动低点」更重要），
    需要「距现价最近的支撑/压力」时由 :func:`plan_levels` 按距离重排。
    同一价格重复出现时只保留最早的一根（保证排序严格单调、输出稳定）。

    参数
    ----
    bars : list[dict]；lookback : 回看根数；top : 各保留几个；k : 局部极值半宽（默认 3）。

    返回
    ----
    dict
        ``ok`` / ``highs`` / ``lows`` / ``lookback`` / ``k`` / ``bars`` / ``note``；
        每项形如 ``{"price", "i", "index", "t", "kind", "note"}``（``i`` = 窗口内下标，
        ``index`` = 清洗后K线序列的绝对下标，便于前端在图上标注）。
    """
    try:
        clean = _clean_bars(bars)
        lb = _int_p(lookback, DEFAULT_LEVELS["swingLookback"], 10, 600)
        tp = _int_p(top, DEFAULT_LEVELS["swingTop"], 1, 20)
        kk = _int_p(k, DEFAULT_LEVELS["swingK"], 1, 10)
        win = clean[-lb:]
        m = len(win)
        if m < 2 * kk + 1:
            return {"ok": False, "highs": [], "lows": [],
                    "error": "有效K线不足（%d < %d 根），无法识别摆动高低点" % (m, 2 * kk + 1),
                    "lookback": lb, "k": kk, "bars": m, "note": ""}
        highs, lows = [], []
        seen_h, seen_l = set(), set()
        base = len(clean) - m
        for i in range(kk, m - kk):
            seg = win[i - kk:i + kk + 1]
            left, right = seg[:kk], seg[kk + 1:]
            h, l = win[i]["high"], win[i]["low"]
            if all(h > s["high"] for s in left) and all(h > s["high"] for s in right):
                if h not in seen_h:
                    seen_h.add(h)
                    highs.append({"price": _clean(h), "i": i, "index": base + i,
                                  "t": win[i].get("t"), "kind": "swing_high"})
            if all(l < s["low"] for s in left) and all(l < s["low"] for s in right):
                if l not in seen_l:
                    seen_l.add(l)
                    lows.append({"price": _clean(l), "i": i, "index": base + i,
                                 "t": win[i].get("t"), "kind": "swing_low"})
        highs.sort(key=lambda it: -it["price"])
        lows.sort(key=lambda it: it["price"])
        highs, lows = highs[:tp], lows[:tp]
        for it in highs:
            it["note"] = ("摆动高点：左右各 %d 根的最高价都低于它（严格局部极值），"
                          "距最新一根 %d 根" % (kk, m - 1 - it["i"]))
        for it in lows:
            it["note"] = ("摆动低点：左右各 %d 根的最低价都高于它（严格局部极值），"
                          "距最新一根 %d 根" % (kk, m - 1 - it["i"]))
        return {
            "ok": True, "highs": highs, "lows": lows,
            "lookback": lb, "k": kk, "bars": m,
            "note": ("摆动高低点口径：窗口（最近 %d 根，实际 %d 根）内左右各 %d 根严格极值；"
                     "highs 按价格降序、lows 按价格升序，同一价位只保留最早一根；"
                     "它是「结构」维度的一票，与枢轴点（机械）、量能密集区（筹码）交叉验证。"
                     % (lb, m, kk)),
        }
    except Exception as e:
        return {"ok": False, "highs": [], "lows": [], "error": "内部异常：%s" % (e,),
                "lookback": None, "k": None, "bars": 0, "note": ""}


# --------------------------------------------------------------------------- #
# 四、成交量密集区（筹码分布的近似）
# --------------------------------------------------------------------------- #
def volume_nodes(bars, lookback=120, bins=24, top=3):
    """成交量密集区（成交量分布的近似筹码分布）。

    做法：在最近 ``lookback`` 根K线的 ``[最低价, 最高价]`` 区间上等宽分 ``bins`` 个价格箱，
    每根K线把它的成交量记到「典型价 ``(H + L + C) / 3``」所在的箱里，得到「成交量占比
    随价格」的分布；取占比最高的 ``top`` 个箱中心作为密集区（相邻箱只保留更大的那个，
    避免同一片区域被重复计成多个密集区）。这是**筹码分布的近似**：真实筹码还取决于
    换手与持仓成本，日线级别只能做到这个粒度，因此 note 里明确写「近似」。

    参数
    ----
    bars : list[dict]；lookback : 回看根数；bins : 分箱数；top : 保留几个密集区。

    返回
    ----
    dict
        ``ok`` / ``nodes``（每项 price / low / high / bin / volume / share / strength / note）/
        ``poc``（成交量最大箱的中心价）/ ``profile``（全部箱）/ ``bins`` / ``width`` /
        ``low`` / ``high`` / ``bars`` / ``note``。
        成交量为空或全为 0 时 ``ok = False``（无法估算筹码分布），不抛异常。
    """
    try:
        clean = _clean_bars(bars)
        lb = _int_p(lookback, DEFAULT_LEVELS["volumeLookback"], 10, 600)
        nb = _int_p(bins, DEFAULT_LEVELS["volumeBins"], 5, 200)
        tp = _int_p(top, DEFAULT_LEVELS["volumeTop"], 1, 10)
        win = clean[-lb:]
        if len(win) < 5:
            return {"ok": False, "nodes": [], "error": "有效K线不足（%d < 5 根），无法估算成交量密集区"
                    % len(win), "poc": None, "profile": [], "bins": nb, "width": None,
                    "low": None, "high": None, "bars": len(win), "note": ""}
        lo = min(b["low"] for b in win)
        hi = max(b["high"] for b in win)
        if not (hi > lo) or lo <= 0:
            return {"ok": False, "nodes": [], "error": "价格区间无效（最高 ≤ 最低或非正数），无法分箱",
                    "poc": None, "profile": [], "bins": nb, "width": None,
                    "low": _clean(lo), "high": _clean(hi), "bars": len(win), "note": ""}
        width = (hi - lo) / nb
        vols = [0.0] * nb
        for b in win:
            typical = (b["high"] + b["low"] + b["close"]) / 3.0
            idx = int((typical - lo) / width)
            idx = 0 if idx < 0 else (nb - 1 if idx >= nb else idx)
            vols[idx] += b.get("volume") or 0.0
        total = sum(vols)
        if total <= 0:
            return {"ok": False, "nodes": [], "error": "成交量为空或全为 0，无法估算筹码分布"
                    "（请检查 bars 的 volume 字段）", "poc": None, "profile": [], "bins": nb,
                    "width": _clean(width), "low": _clean(lo), "high": _clean(hi),
                    "bars": len(win), "note": ""}
        profile = [{"bin": i, "low": _clean(lo + i * width), "high": _clean(lo + (i + 1) * width),
                    "center": _clean(lo + (i + 0.5) * width), "volume": _clean(vols[i]),
                    "share": _clean(vols[i] / total)} for i in range(nb)]
        order = sorted(range(nb), key=lambda i: (-vols[i], i))
        picked = []
        for i in order:
            if len(picked) >= tp:
                break
            if any(abs(i - j) <= 1 for j in picked):
                continue
            picked.append(i)
        picked.sort(key=lambda i: -vols[i])
        nodes = []
        for rank, i in enumerate(picked):
            share = vols[i] / total
            nodes.append({
                "price": profile[i]["center"], "low": profile[i]["low"], "high": profile[i]["high"],
                "bin": i, "volume": profile[i]["volume"], "share": _clean(share),
                "strength": "强" if (rank == 0 and share >= 0.10) else ("中" if share >= 0.05 else "弱"),
                "note": "成交量密集区（近似筹码峰）：占区间成交量 %s，箱宽 %s，"
                        "箱区间 [%s, %s]" % (_pct(share), _fmt(width), _fmt(profile[i]["low"]),
                                           _fmt(profile[i]["high"])),
            })
        poc = max(range(nb), key=lambda i: vols[i])
        return {
            "ok": True, "nodes": nodes, "poc": profile[poc]["center"], "pocBin": poc,
            "profile": profile, "bins": nb, "width": _clean(width),
            "low": _clean(lo), "high": _clean(hi), "bars": len(win),
            "note": ("成交量密集区口径：最近 %d 根K线的价格区间等宽分 %d 箱（箱宽 %s），"
                     "每根按典型价 (H+L+C)/3 把成交量记入所在箱，取占比最高的 %d 个箱中心"
                     "（相邻箱只保留更大的）；这是**筹码分布的近似**（真实筹码还取决于换手"
                     "与持仓成本，日线粒度做不到逐笔还原），只作为「支撑阻力的一票」。"
                     % (len(win), nb, _fmt(width), len(nodes))),
        }
    except Exception as e:
        return {"ok": False, "nodes": [], "error": "内部异常：%s" % (e,), "poc": None,
                "profile": [], "bins": None, "width": None, "low": None, "high": None,
                "bars": 0, "note": ""}


# --------------------------------------------------------------------------- #
# 五、单笔风险预算（1% 规则，由风险反算股数）
# --------------------------------------------------------------------------- #
def _vantage_note(rp, days=10):
    """1% 规则的数学含义与失效边界（固定文案，供 basis / note 复用）。

    刻意用字符串拼接而不是 % 格式化：文案里有大量字面百分号（1%、10%），
    混用格式化时极易踩 `unsupported format character` 这个坑（实测踩过一次）。
    """
    left = (1.0 - rp) ** days
    return ("1% 规则的数学含义：单笔最大亏损 = 本金 × " + _pct(rp)
            + "；连续 " + str(days) + " 次全额止损的累计回撤约 " + _pct(1.0 - left)
            + "（1 − " + _fmt(1.0 - rp, 4) + "^" + str(days) + " = " + _pct(1.0 - left)
            + "），这就是它被广泛使用的原因。"
            "失效边界（必须知道）：① 不防向上/向下跳空缺口——开盘直接越过止损价，实际亏损可以"
            "远超 " + _pct(rp) + "；② 不防滑点与流动性枯竭（跌停板上根本卖不出）；"
            "③ 不防多标的**相关性同时爆发**——10 只同板块标的各 1% 风险，实际是一笔约 10% 的风险；"
            "④ 只约束「价格风险」，不约束「时间风险」（滞涨占用的机会成本）。")


def risk_budget(entry, stop, capital, risk_pct=None, lot=100, max_weight=None):
    """由「单笔风险」反算可买股数：``shares = floor(capital × risk_pct / (entry − stop) / lot) × lot``。

    为什么不用「本金 × 固定比例」定仓位：那等于让止损距离决定风险大小。
    正确的顺序是「先定止损（由价格结构决定）→ 再让风险预算决定手数」，
    这样不同波动率的标的承担的是**同一个**单笔风险（默认本金的 1%）。

    参数
    ----
    entry / stop : float
        入场价与初始止损价（多头；要求 ``stop < entry``）。
    capital : float
        总资金（> 0）。
    risk_pct : float, optional
        单笔风险比例（小数），默认 ``0.01``（1% 规则，Van Tharp）。按 [0, 1] 截断。
    lot : int
        最小交易单位，默认 100（A 股一手）。**向下取整到整手**（不超预算优先于买满）。
    max_weight : float, optional
        单标的上限（占总资金比例）；给定时再按 ``capital × max_weight / entry`` 取小。
        ``None`` 表示不做上限约束。

    返回
    ----
    dict
        ``ok``（输入是否可用）；``shares`` / ``lots`` / ``amount`` / ``weight``（占总资金）；
        ``perTradePct``（单笔风险比例）；``riskPerShare`` / ``riskAmount``（实际单笔最大亏损）；
        ``limitedBy``（``"risk"`` 表示由 1% 规则决定，``"max_weight"`` 表示被上限压低）；
        ``basis``（本次计算过程 + 1% 规则的数学含义与失效边界）；``note``；失败时给 ``error``。

    边界
    ----
    输入非法（价格 ≤ 0、``stop >= entry``、本金 ≤ 0）→ ``ok = False``、``shares = 0``，
    绝不抛异常；预算买不满一手时 ``shares = 0`` 但 ``ok = True``（输入没问题，只是钱不够），
    并在 ``basis`` 里说明「一手所需金额」。
    """
    e, s, cap = _num(entry), _num(stop), _num(capital)
    rp = _ratio(risk_pct, DEFAULT_LEVELS["riskPct"])
    lot_i = _int_p(lot, DEFAULT_LEVELS["lot"], 1, 100000)
    note = ("风险反算口径：先由价格结构定止损、再由单笔风险预算定手数，"
            "保证不同波动率的标的承担同一个小数风险；股数**向下取整到整手**（宁少不超）；"
            "与 core/kelly.py 的凯利仓位是两道独立闸门，最终手数取两者较小者；"
            "未计手续费、印花税与滑点。" + _vantage_note(rp))

    def _fail(msg):
        return {"ok": False, "error": msg, "shares": 0, "lots": 0, "amount": 0.0, "weight": 0.0,
                "perTradePct": rp, "riskPerShare": None, "riskAmount": 0.0,
                "capital": cap, "entry": e, "stop": s, "lot": lot_i,
                "limitedBy": None, "basis": msg, "note": note}

    if e is None or e <= 0:
        return _fail("入场价非法（需为大于 0 的数值），无法反算股数")
    if s is None or s <= 0:
        return _fail("止损价非法（需为大于 0 的数值），无法反算股数")
    if s >= e:
        return _fail("止损价（%s）不低于入场价（%s）：多头口径下无法反算风险，"
                     "请检查是否传成了空头或止损方向写反" % (_fmt(s), _fmt(e)))
    if cap is None or cap <= 0:
        return _fail("本金非法（需为大于 0 的数值），无法反算股数")

    risk_ps = e - s
    budget = cap * rp
    raw = budget / risk_ps
    shares = int(raw // lot_i) * lot_i
    limited_by = "risk"
    cap_shares = None
    mw = _num(max_weight)
    if mw is not None and mw > 0:
        cap_shares = int((cap * mw / e) // lot_i) * lot_i
        if cap_shares < shares:
            shares = cap_shares
            limited_by = "max_weight"
    amount = shares * e
    weight = amount / cap
    actual_risk = shares * risk_ps

    bits = ["单笔风险预算 = 本金 %s × %s = %s" % (_fmt(cap, 2), _pct(rp), _fmt(budget, 2)),
            "每股风险 = 入场 %s − 止损 %s = %s" % (_fmt(e), _fmt(s), _fmt(risk_ps)),
            "理论股数 = %s / %s = %s 股" % (_fmt(budget, 2), _fmt(risk_ps), _fmt(raw, 2))]
    if cap_shares is not None:
        bits.append("单标的上限 %s → 上限股数 %d 股" % (_pct(mw), cap_shares))
    bits.append("整手（%d 股/手）向下取整 → %d 股" % (lot_i, shares))
    bits.append("实际最大亏损 = %d × %s = %s（占本金 %s）"
                % (shares, _fmt(risk_ps), _fmt(actual_risk, 2), _pct(actual_risk / cap if cap else 0)))
    if shares <= 0:
        bits.append("结论：预算买不满一手（一手约需 %s 元，风险预算 %s 元）→ 建议提高本金、"
                    "收紧止损或减少一手股数（部分券商支持零股）"
                    % (_fmt(e * lot_i, 2), _fmt(budget, 2)))
    if limited_by == "max_weight":
        bits.append("结论：本次由**单标的上限**决定手数（1% 规则算出的手数更大）")
    elif raw - shares > 1e-9:
        bits.append("结论：本次由**整手取整**决定手数（比单笔风险规则的理论值少 %.2f 股，"
                    "属于「宁可少买」的保守方向）" % (raw - shares,))
    else:
        bits.append("结论：本次由**1% 规则**决定手数")

    return {
        "ok": True, "shares": shares, "lots": shares // lot_i, "amount": _clean(amount),
        "weight": _clean(weight), "perTradePct": _clean(rp),
        "riskPerShare": _clean(risk_ps), "riskAmount": _clean(actual_risk),
        "capital": cap, "entry": e, "stop": s, "lot": lot_i,
        "maxWeight": _clean(mw) if mw is not None else None,
        "limitedBy": limited_by,
        "basis": "风险反算：" + "；".join(bits) + "。" + _vantage_note(rp),
        "note": note,
    }


# --------------------------------------------------------------------------- #
# 六、优先级式退出规则（第一个触发者胜出）
# --------------------------------------------------------------------------- #
def _squeeze(vals, i, n):
    """取 [i - n + 1, i] 的有效值（用于均量等窗口统计）。"""
    lo = max(0, i - n + 1)
    return [v for v in vals[lo:i + 1]]


def exit_rules(bars, position=None, params=None):
    """优先级式退出规则（**第一个触发者胜出**，参考 Penny 的 7 级退出实践）。

    规则与优先级（数字越小越先执行）：

    ======  ==========  ============================================  ==========================
    priority  规则       触发条件                                       建议动作
    ======  ==========  ============================================  ==========================
    1      动量衰竭     RSI(14) > 70 且 MACD 在最近 2 根内死叉            先减半仓
    2      移动止损     自持仓期最高价回撤 ≥ 8%（trailPct）              清仓
    3      放量杀跌     量 > 4×5 日均量 且 当日跌幅 > 5%                 减半仓（次日不修复则清）
    4      支撑破位     收盘跌破最近 20 根最低价                         清仓
    5      止盈目标     触及首目标（成本 + 2.0×ATR）                     按 40% / 35% / 25% 分批止盈
    6      滞涨         持有 ≥ 20 天且收益 < 2%                         减仓离场（换更有机会的标的）
    7      ATR 止损     收盘跌破初始止损（成本 − 2.0×ATR）               清仓（无条件执行）
    ======  ==========  ============================================  ==========================

    为什么要「优先级」而不是「任意一条就发信号」：多条规则常常同时成立
    （例如放量杀跌往往同时跌破 20 日低点），如果并列输出，用户会收到自相矛盾的建议
    （减半仓 vs 清仓）。给出优先级 + 命中的那条规则名，用户拿到的信息是
    「**是哪一条规则触发的**」，而不只是一个价格。

    参数
    ----
    bars : list[dict]
        K线（旧 → 新）。
    position : dict, optional
        持仓信息：``{"entry", "stop", "high", "days"}``。
        · 缺少 ``entry`` 时，需要成本价的规则（②⑤⑥⑦ 中需要成本的项）**只输出规则、不判定**
          （``triggered = False``，``detail`` 注明「未提供持仓信息」），避免凭空假设成本；
        · 缺少 ``high`` 时用最近 ``days``（缺省 20）根的最高价代替，``detail`` 里写明；
        · 不传 position 也可以：不依赖持仓的规则（①③④）照常判定。
    params : dict, optional
        覆盖阈值：``rsiN / rsiOverbought / trailPct / volMult / volDropPct / lowN /
        stagnantDays / stagnantRet / atrN / atrMult / targetAtr``。

    返回
    ----
    list[dict]
        固定 7 条（K线不足时返回空列表）：``priority / rule / condition / action /
        triggered / detail``。``detail`` 里带具体数值，可直接推给用户。
    """
    try:
        p = _options(params, _int_p((params or {}).get("horizon") if isinstance(params, dict) else None, 20, 1, 250))
        clean = _clean_bars(bars)
        if len(clean) < 2:
            return []
        n = len(clean)
        closes = [b["close"] for b in clean]
        highs = [b["high"] for b in clean]
        lows = [b["low"] for b in clean]
        vols = [b.get("volume") or 0.0 for b in clean]
        last = clean[-1]
        px = last["close"]
        pos = position if isinstance(position, dict) else {}
        entry = _num(pos.get("entry"))
        stop = _num(pos.get("stop"))
        days = _int_p(pos.get("days"), 0, 0, 100000)
        high = _num(pos.get("high"))
        high_from_bars = False
        if high is None:
            ref = days if days > 0 else p["lowN"]
            high = max(highs[-ref:]) if ref > 0 else last["high"]
            high_from_bars = True
        has_cost = entry is not None and entry > 0

        atr = _last_valid(ATR(clean, p["atrN"]))
        if atr is None or atr <= 0:
            atr = px * 0.02
        if stop is None and has_cost:
            stop = entry - p["atrMult"] * atr

        rsi = RSI(closes, p["rsiN"])
        macd = MACD(closes)
        dif, dea = macd["dif"], macd["dea"]

        # —— ① 动量衰竭：RSI 超买 + MACD 死叉（最近 2 根内） ——
        cross_i = None
        for i in (n - 1, n - 2):
            if i > 0 and _num(dif[i]) is not None and _num(dea[i]) is not None \
                    and _num(dif[i - 1]) is not None and _num(dea[i - 1]) is not None \
                    and dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
                cross_i = i
                break
        rsi_v = _num(rsi[-1])
        trig1 = bool(rsi_v is not None and rsi_v > p["rsiOverbought"] and cross_i is not None)
        if trig1:
            det1 = ("RSI(%d) = %.2f > %s，且 MACD 在最近 %d 根内死叉（第 %d 根：DIF 下穿 DEA）"
                    "→ 动量衰竭，先减半仓保住利润"
                    % (p["rsiN"], rsi_v, _fmt(p["rsiOverbought"]), n - 1 - cross_i + 1, cross_i + 1))
        else:
            det1 = ("RSI(%d) = %s（阈值 > %s）；最近 2 根内%s MACD 死叉 → 未触发"
                    % (p["rsiN"], _fmt(rsi_v, 2), _fmt(p["rsiOverbought"]), "" if cross_i is not None else "无"))

        # —— ② 移动止损：自持仓最高价回撤 ≥ trailPct（没有持仓成本就不判定）——
        dd = (high - px) / high if (high and high > 0) else 0.0
        trig2 = bool(has_cost and high and high > 0 and dd >= p["trailPct"])
        if trig2:
            det2 = ("持仓期最高价 %s，最新价 %s，回撤 %s ≥ 阈值 %s → 移动止损离场"
                    % (_fmt(high), _fmt(px), _pct(dd), _pct(p["trailPct"])))
        elif not has_cost:
            det2 = "未提供持仓信息（entry）：本条仅给规则，不做判定"
        else:
            det2 = ("持仓期最高价 %s，最新价 %s，回撤 %s（阈值 %s）→ 未触发%s"
                    % (_fmt(high), _fmt(px), _pct(dd), _pct(p["trailPct"]),
                       "；最高价取自最近 %d 根（未传 position.high）" % (days if days > 0 else p["lowN"])
                       if high_from_bars else ""))

        # —— ③ 放量杀跌：量 > 4×5 日均量 且 当日跌幅 > 5% ——
        ma_vol = None
        if n >= 7:
            prev5 = _squeeze(vols, n - 2, 5)
            if prev5 and sum(prev5) > 0:
                ma_vol = sum(prev5) / len(prev5)
        chg = (closes[-1] / closes[-2] - 1.0) if closes[-2] > 0 else 0.0
        trig3 = bool(ma_vol and ma_vol > 0 and vols[-1] > p["volMult"] * ma_vol
                     and chg <= -p["volDropPct"])
        if trig3:
            det3 = ("最新成交量 %s 手 = %.2f 倍 5 日均量（%s 手，阈值 %s 倍），当日涨跌 %s ≤ −%s "
                    "→ 放量杀跌，先减半仓"
                    % (_fmt(vols[-1], 0), vols[-1] / ma_vol, _fmt(ma_vol, 0), _fmt(p["volMult"]),
                       _pct(chg), _pct(p["volDropPct"])))
        elif ma_vol is None or ma_vol <= 0:
            det3 = "成交量数据为空或全为 0，本条无法判定"
        else:
            det3 = ("最新成交量 %s 手，5 日均量 %s 手（%.2f 倍，阈值 %s 倍）；当日涨跌 %s"
                    "（阈值 ≤ −%s）→ 未触发"
                    % (_fmt(vols[-1], 0), _fmt(ma_vol, 0), vols[-1] / ma_vol, _fmt(p["volMult"]),
                       _pct(chg), _pct(p["volDropPct"])))

        # —— ④ 支撑破位：收盘跌破最近 lowN 根最低价 ——
        ref_low = min(lows[-p["lowN"] - 1:-1]) if n > p["lowN"] else min(lows[:-1])
        trig4 = bool(px < ref_low)
        det4 = ("最近 %d 根最低价 %s，最新收盘 %s → %s"
                % (p["lowN"], _fmt(ref_low), _fmt(px),
                   "收盘已跌破该低点（支撑破位），无条件清仓" if trig4
                   else "尚未跌破（距离 %s）" % _pct((px / ref_low - 1.0) if ref_low else 0)))

        # —— ⑤ 止盈目标：触及首目标（成本 + targetAtr[0]×ATR） ——
        t1 = (entry + p["targetAtr"][0] * atr) if has_cost else None
        trig5 = bool(t1 is not None and px >= t1)
        if t1 is None:
            det5 = "未提供持仓信息（entry）：本条仅给规则，不做判定"
        elif trig5:
            det5 = ("首目标 %s（= 成本 %s + %s×ATR）已触及（最新价 %s）→ 按权重 %s 分批止盈"
                    % (_fmt(t1), _fmt(entry), _fmt(p["targetAtr"][0]), _fmt(px),
                       "/".join(_pct(w, 0) for w in p["targetWeights"])))
        else:
            det5 = ("首目标 %s（= 成本 %s + %s×ATR），最新价 %s，还差 %s → 未触发"
                    % (_fmt(t1), _fmt(entry), _fmt(p["targetAtr"][0]), _fmt(px),
                       _pct((t1 - px) / px if px else 0)))

        # —— ⑥ 滞涨：持有 ≥ stagnantDays 且收益 < stagnantRet ——
        ret = (px / entry - 1.0) if has_cost else None
        trig6 = bool(has_cost and days >= p["stagnantDays"] and ret is not None and ret < p["stagnantRet"])
        if not has_cost:
            det6 = "未提供持仓信息（entry）：本条仅给规则，不做判定"
        elif trig6:
            det6 = ("已持有 %d 天（阈值 %d 天），收益 %s < 阈值 %s → 滞涨，减仓换股"
                    % (days, p["stagnantDays"], _pct(ret), _pct(p["stagnantRet"])))
        else:
            det6 = ("已持有 %d 天（阈值 %d 天），收益 %s（阈值 < %s）→ 未触发"
                    % (days, p["stagnantDays"], _pct(ret), _pct(p["stagnantRet"])))

        # —— ⑦ ATR 止损：收盘跌破初始止损 ——
        trig7 = bool(stop is not None and px < stop)
        if stop is None:
            det7 = "未提供止损价且无法由持仓成本推出（缺 entry）：本条仅给规则，不做判定"
        elif trig7:
            det7 = "最新收盘 %s 已跌破初始止损 %s（成本 %s − %s×ATR）→ 无条件清仓" % (
                _fmt(px), _fmt(stop), _fmt(entry) if has_cost else "—", _fmt(p["atrMult"]))
        else:
            det7 = "初始止损 %s（%s），最新收盘 %s，距离 %s → 未触发" % (
                _fmt(stop), "成本 − %s×ATR" % _fmt(p["atrMult"]) if has_cost else "外部传入",
                _fmt(px), _pct((px - stop) / px if px else 0))

        return [
            {"priority": 1, "rule": "动量衰竭",
             "condition": "RSI(%d) > %s 且 MACD 在最近 2 根内死叉" % (p["rsiN"], _fmt(p["rsiOverbought"])),
             "action": "减半仓：先卖出计划仓位的 50%，剩余用移动止损跟随",
             "triggered": trig1, "detail": det1},
            {"priority": 2, "rule": "移动止损",
             "condition": "自持仓期最高价回撤 ≥ %s（trailPct）" % _pct(p["trailPct"]),
             "action": "清仓：回撤已超过容忍度，先离场再看",
             "triggered": trig2, "detail": det2},
            {"priority": 3, "rule": "放量杀跌",
             "condition": "成交量 > %s×5 日均量 且 当日跌幅 > %s" % (_fmt(p["volMult"]), _pct(p["volDropPct"])),
             "action": "减半仓：次日不能快速收复则清仓",
             "triggered": trig3, "detail": det3},
            {"priority": 4, "rule": "支撑破位",
             "condition": "收盘跌破最近 %d 根最低价" % p["lowN"],
             "action": "清仓：支撑破位后原计划失效，不在破位下方补仓",
             "triggered": trig4, "detail": det4},
            {"priority": 5, "rule": "止盈目标",
             "condition": "触及首目标（成本 + %s×ATR(%d)）" % (_fmt(p["targetAtr"][0]), p["atrN"]),
             "action": "分批止盈：按 %s 的权重依次了结" % "/".join(_pct(w, 0) for w in p["targetWeights"]),
             "triggered": trig5, "detail": det5},
            {"priority": 6, "rule": "滞涨",
             "condition": "持有 ≥ %d 天 且收益 < %s" % (p["stagnantDays"], _pct(p["stagnantRet"])),
             "action": "减仓或离场：把资金换到更有机会的标的",
             "triggered": trig6, "detail": det6},
            {"priority": 7, "rule": "ATR 止损",
             "condition": "收盘跌破初始止损（成本 − %s×ATR(%d)）" % (_fmt(p["atrMult"]), p["atrN"]),
             "action": "清仓（无条件执行，不允许下移止损）",
             "triggered": trig7, "detail": det7},
        ]
    except Exception as e:                                   # 出口兜底：绝不抛异常
        return [{"priority": 1, "rule": "规则计算异常", "condition": "内部异常",
                 "action": "请检查传入的 bars / position 结构", "triggered": False,
                 "detail": "内部异常：%s" % (e,)}]


# --------------------------------------------------------------------------- #
# 七、支撑 / 阻力（多源交叉 + 强度）
# --------------------------------------------------------------------------- #
def _limit_hint(code, px):
    """涨跌停提示：core/rules.py 若提供 limit_prices() 就引用其数字，否则只给文案。

    这是**前向兼容的可选钩子**：`core/rules.py` 目前不在仓库里，因此默认走文案分支；
    一旦该模块出现且暴露 ``limit_prices(code, price) -> {"up","down"}``，
    本函数会自动把具体涨跌停价拼进 warnings。所有调用都包在 try/except 里，绝不外抛。
    """
    global _LIMIT_HOOK
    if _LIMIT_HOOK is None:
        try:
            from . import rules as _rules                       # 可选依赖
            fn = getattr(_rules, "limit_prices", None)
            _LIMIT_HOOK = fn if callable(fn) else False
        except Exception:
            _LIMIT_HOOK = False
    if not _LIMIT_HOOK:
        return "（涨跌停价判断需配合 core/rules.py：该模块尚未提供时按本条通用文案提示。）"
    try:
        res = _LIMIT_HOOK(code, px)
        if isinstance(res, dict):
            up = _num(res.get("up", res.get("limitUp")))
            down = _num(res.get("down", res.get("limitDown")))
            if up is not None and down is not None:
                return "按 core/rules.py 口径：涨停价 %.2f、跌停价 %.2f。" % (up, down)
    except Exception:
        pass
    return "（core/rules.py 未返回可用的涨跌停价，按通用文案提示。）"


_LIMIT_HOOK = None


def _touches(clean, price, opts):
    """被触及次数：最近 volumeLookback 根里，[low, high] 区间覆盖该价位的根数。"""
    win = clean[-opts["volumeLookback"]:]
    tol = max(price * 0.001, 0.01)
    cnt = 0
    for b in win:
        if (b["low"] - tol) <= price <= (b["high"] + tol):
            cnt += 1
    return cnt


def _candidates(clean, px, atr, opts, warns):
    """收集全部点位候选（未合并、未分类）：枢轴点 / 摆动高低点 / 量能密集区 / 均线。"""
    out = []
    last = clean[-1]
    pv = pivot_points(last["high"], last["low"], last["close"])
    if pv.get("valid"):
        for key in ("pp", "s1", "s2", "s3", "r1", "r2", "r3"):
            v = _num(pv.get(key))
            if v is not None and v > 0:
                out.append({"price": v, "kind": "pivot", "source": "枢轴点 " + key.upper(),
                            "share": None})
    sw = swing_levels(clean, opts["swingLookback"], opts["swingTop"], opts["swingK"])
    if sw.get("ok"):
        for it in sw["highs"]:
            out.append({"price": it["price"], "kind": "swing_high",
                        "source": "前高 %s" % _fmt(it["price"]), "share": None,
                        "index": it.get("index")})
        for it in sw["lows"]:
            out.append({"price": it["price"], "kind": "swing_low",
                        "source": "前低 %s" % _fmt(it["price"]), "share": None,
                        "index": it.get("index")})
    vn = volume_nodes(clean, opts["volumeLookback"], opts["volumeBins"], opts["volumeTop"])
    if vn.get("ok"):
        for it in vn["nodes"]:
            out.append({"price": it["price"], "kind": "volume_node",
                        "source": "量能密集区（占比 %s）" % _pct(it["share"]),
                        "share": it["share"]})
    else:
        warns.append("成交量数据不可用（%s），本次跳过「成交量密集区」这一来源，"
                     "支撑阻力只由枢轴点 / 摆动高低点 / 均线给出。" % vn.get("error"))
    closes = [b["close"] for b in clean]
    for n in (20, 60):
        v = _last_valid(SMA(closes, n))
        if v is not None and v > 0:
            out.append({"price": v, "kind": "ma", "source": "MA%d %s" % (n, _fmt(v)),
                        "share": None, "ma": n})
    return out


def _build_side(cands, clean, px, atr, opts, side, nd):
    """把候选合并成支撑（< 现价）或阻力（> 现价），并算强度。

    强度打分（可解释、可复核）：来源数 ≥2 → +2、≥3 → 再 +1；被触及次数（上限 3）；
    量能占比 ≥10% → +2、≥5% → +1；距现价 ≤2×ATR → +1。
    满分 7 → 强（≥5）/ 中（≥3）/ 弱（其余）。
    """
    tol = max(atr * opts["mergeAtr"], px * 1e-4)
    pool = [c for c in cands if (c["price"] < px if side == "support" else c["price"] > px)]
    pool.sort(key=lambda c: c["price"])
    groups = []
    for c in pool:
        if groups and abs(c["price"] - groups[-1]["last"]) <= tol:
            groups[-1]["items"].append(c)
            groups[-1]["last"] = c["price"]
        else:
            groups.append({"items": [c], "last": c["price"]})
    out = []
    for g in groups:
        items = g["items"]
        prices = [it["price"] for it in items]
        volw = [it for it in items if it.get("share")]
        if volw:
            s = sum(it["share"] for it in volw)
            price = sum(it["price"] * it["share"] for it in volw) / s if s > 0 else sum(prices) / len(prices)
        else:
            price = sum(prices) / len(prices)
        kinds = []
        for it in items:
            if it["kind"] not in kinds:
                kinds.append(it["kind"])
        if any(k == "volume_node" for k in kinds):
            kind = "volume_node"
        elif "pivot" in kinds:
            kind = "pivot"
        elif "swing_high" in kinds:
            kind = "swing_high"
        elif "swing_low" in kinds:
            kind = "swing_low"
        else:
            kind = "ma"
        share = max([it.get("share") or 0.0 for it in items])
        touches = _touches(clean, price, opts)
        score = 0
        if len(items) >= 2:
            score += 2
        if len(items) >= 3:
            score += 1
        score += min(touches, 3)
        if share >= 0.10:
            score += 2
        elif share >= 0.05:
            score += 1
        near = abs(price - px) <= 2.0 * atr
        if near:
            score += 1
        strength = "强" if score >= 5 else ("中" if score >= 3 else "弱")
        srcs = []
        for it in items:
            if it["source"] not in srcs:
                srcs.append(it["source"])
        note = "%s（%d 个来源%s）：%s；最近 %d 根内被触及 %d 次；距现价 %s（%.2f×ATR）" % (
            "支撑" if side == "support" else "阻力", len(items),
            "，多来源重叠的价位更有效" if len(items) >= 2 else "",
            " + ".join(srcs), opts["volumeLookback"], touches,
            _pct((price - px) / px if px else 0), abs(price - px) / atr if atr > 0 else 0)
        if side == "support" and kind == "swing_high":
            note += "；该前高已被突破，按「突破后回踩视为支撑」处理"
        if side == "resistance" and kind == "swing_low":
            note += "；该前低已失守，按「反抽视为阻力」处理"
        if len(items) >= 2:
            note += "；强度 %s（来源数 %d + 触及 %d 次 + 量能占比 %s）" % (
                strength, len(items), touches, _pct(share))
        else:
            note += "；强度 %s（单一来源 + 触及 %d 次 + 量能占比 %s）" % (strength, touches, _pct(share))
        out.append({"price": price, "kind": kind, "strength": strength, "note": note,
                    "_score": score})
    out.sort(key=lambda it: (abs(it["price"] - px), -it["_score"]))
    res = []
    for it in out[:opts["levelTop"]]:
        res.append({"price": _r(it["price"], nd), "kind": it["kind"],
                    "strength": it["strength"], "note": it["note"]})
    return res


# --------------------------------------------------------------------------- #
# 八、主入口：买卖点位计划
# --------------------------------------------------------------------------- #
def plan_levels(bars, horizon=20, params=None, price=None):
    """买卖点位引擎主入口：一次性给出支撑阻力 / 枢轴 / 分批建仓 / 分批止盈 / 止损 / 盈亏比 /
    风险预算 / 退出规则 / 置信度 / 风险提示。

    完整口径见模块 docstring。要点：

    · **止损**：``max(现价 − atrMult×ATR(atrN), Chandelier(chandelierN, chandelierMult))``，
      再受 ``maxStopPct``（默认 −12%，与 core/advisor.py 一致）与 ``minStopPct`` 约束；
      取更紧者是刻意的选择（先活下来，再谈胜率）。
    · **三段式移动止损**：``initial → trailStart（现价 + 2×ATR）→ trailLockIn（trailStart − 1×ATR）``，
      等价 freqtrade 的 ``trailing_stop_positive_offset`` + ``trailing_stop_positive``
      + ``trailing_only_offset_is_reached``。
    · **分批建仓**：首档 50% 现价、次档 30% ``−1.0×ATR`` 或首个支撑、末档 20% ``−1.5×ATR``
      或第二支撑；**任一档跌破初始止损即剔除并记 warning**，剩余档权重归一化到 1。
    · **手数**：``risk_budget`` 由 1% 规则反算（``params["capital"]`` 缺省 10 万演示口径）。

    参数
    ----
    bars : list[dict]
        K线（旧 → 新），需 ``high / low / close``，``volume`` 用于量能密集区与放量杀跌；
        ``t`` 用于 ``asOf``。脏K线整根剔除。
    horizon : int
        计划跨度（K线根数，默认 20 ≈ 一个月），同时作为 ``holding.days`` 与滞涨阈值的默认值。
    params : dict, optional
        覆盖 :data:`DEFAULT_LEVELS`；额外支持 ``code`` / ``market`` / ``capital`` /
        ``position``（持仓 ``{"entry","stop","high","days"}``，用于退出规则判定）。
    price : float, optional
        覆盖最新价（盘中实时价）；缺省用最后一根收盘价。

    返回
    ----
    dict
        成功：``ok / code / price / atr / asOf / support / resistance / pivot / entries /
        targets / stop / riskReward / risk / exits / holding / confidence / warnings / note``。
        失败（有效K线 < ``minBars``、价格不可用、内部异常）：``{"ok": False, "error": "中文原因"}``，
        **不含任何价位字段**（不臆造）。
    """
    try:
        return _plan_levels(bars, horizon, params, price)
    except Exception as e:                                   # 出口兜底：绝不抛异常
        return _fail("内部异常：%s" % (e,))


def _plan_levels(bars, horizon, params, price):
    """plan_levels 的实现体（异常由外层兜底）。"""
    hz = _int_p(horizon, 20, 1, 250)
    opts = _options(params, hz)
    clean = _clean_bars(bars)
    n = len(clean)
    if n < opts["minBars"]:
        return _fail("有效K线不足（%d < %d 根），无法给出可靠点位，不臆造价位；"
                     "请补充历史数据或改用日线以上周期" % (n, opts["minBars"]))
    last = clean[-1]
    px_raw = _num(price)
    if px_raw is None or px_raw <= 0:
        px_raw = last["close"]
    if px_raw is None or px_raw <= 0:
        return _fail("无有效价格（price 与最后一根收盘价都不可用），不给出任何点位")

    # ---------------- 0. 样本量 → 置信度与价位精度 ----------------
    sample = min(n, opts["volumeLookback"])
    if sample < opts["sampleMid"]:
        level = "低"
        nd = opts["precisionLow"]
    elif sample < opts["sampleFull"]:
        level = "中"
        nd = opts["precision"]
    else:
        level = "高"
        nd = opts["precision"]
    px = round(px_raw, nd)

    warns = []
    notes = []

    # ---------------- 1. ATR 与止损三段式 ----------------
    atr = _last_valid(ATR(clean, opts["atrN"]))
    atr_fallback = False
    if atr is None or atr <= 0:
        atr = px * 0.02
        atr_fallback = True
        warns.append("ATR 不可用或为 0（缺少 high/low 或波动为 0），已按现价 2.0% 兜底估算 —— "
                     "本次止损不再是严格的波动率自适应口径，请谨慎使用。")
    atr22 = _last_valid(ATR(clean, opts["chandelierN"]))
    ch = atr_stops(clean, opts)
    chandelier = _num(ch.get("chandelier"))

    tight = px - opts["atrMult"] * atr
    floor_px = px * (1.0 - opts["maxStopPct"])
    cap_px = px * (1.0 - opts["minStopPct"])
    initial = max(tight, floor_px)
    stop_bits = ["%s×ATR(%d) = %.4f" % (_fmt(opts["atrMult"]), opts["atrN"], tight)]
    if floor_px > tight + 1e-12:
        stop_bits.append("触发 −%s 止损上限（%.4f）" % (_pct(opts["maxStopPct"], 0), floor_px))
    if chandelier is not None:
        if chandelier >= cap_px:
            # 现价已经回撤到 Chandelier 之下：这个价位对新仓来说是「立刻离场」，不能当初始止损。
            warns.append("现价 %.4f 已跌到 Chandelier Exit(%d, %s×ATR) = %.4f 下方：按移动止损口径"
                         "该标的已处于「应离场」区间，因此 Chandelier 不参与本次初始止损、只作结构参照；"
                         "此时新建仓的安全边际更薄（它同时说明结构上尚未止跌）。"
                         % (px, opts["chandelierN"], _fmt(opts["chandelierMult"]),
                            chandelier))
            stop_bits.append("Chandelier(%d, %s×ATR) = %.4f 贴住/高于现价，未纳入初始止损（仅作结构参照）"
                             % (opts["chandelierN"], _fmt(opts["chandelierMult"]), chandelier))
        elif chandelier > initial:
            initial = chandelier
            stop_bits.append("Chandelier(%d, %s×ATR) = %.4f 更紧且低于现价，取更紧者"
                             % (opts["chandelierN"], _fmt(opts["chandelierMult"]), chandelier))
    if initial >= cap_px:
        initial = cap_px
        stop_bits.append("止损距现价不足 %s（ATR 极小），已按最小距离 %.4f 处理"
                         % (_pct(opts["minStopPct"]), cap_px))
    if initial <= 0:
        initial = max(px * 0.5, 0.01)
        stop_bits.append("止损价被压到非正，已按现价 50% 兜底")
    stop_px = round(initial, nd)
    if stop_px >= px:
        stop_px = round(cap_px, nd)
    stop_dist = px - stop_px
    trail_start = round(px + opts["trailStartMult"] * atr, nd)
    trail_lock = max(round(trail_start - opts["trailLockMult"] * atr, nd), round(px * 1.001, nd))
    stop_note = (
        "三段式止损（freqtrade 等价形态）：initial 为初始止损（%s）；涨破 trailStart %.4f "
        "后才开始移动（对应 trailing_only_offset_is_reached = True，偏移 = %.2f×ATR ≈ %s）；"
        "移动后止损抬到成本上方（对应 trailing_stop_positive = %.1f×ATR，锁定价 %.4f）并随价格上移。"
        "代价：移动止损在震荡里会被反复触发（胜率更低），换来趋势中回吐更少。"
        % ("、".join(stop_bits), trail_start, opts["trailStartMult"],
           _pct((trail_start - px) / px if px else 0), opts["trailLockMult"], trail_lock))

    # ---------------- 2. 支撑 / 阻力（多源交叉） ----------------
    cands = _candidates(clean, px, atr, opts, warns)
    support = _build_side(cands, clean, px, atr, opts, "support", nd)
    resistance = _build_side(cands, clean, px, atr, opts, "resistance", nd)
    pivot = {k: _r(v, nd) for k, v in _pivot7(pivot_points(last["high"], last["low"], last["close"])).items()}
    notes.append("支撑阻力为四源交叉：枢轴点（机械计算）、摆动高低点（结构）、"
                 "成交量密集区（筹码近似）、均线 MA20/MA60；多个来源重叠的价位更有效，"
                 "强度按「来源数 + 被触及次数 + 量能占比 + 距现价远近」给 强/中/弱。")

    # ---------------- 3. 分批建仓 ----------------
    tiers = []
    for i, mult in enumerate(opts["entryAtr"]):
        p_i = px - mult * atr
        src = "现价 − %s×ATR(%d)" % (_fmt(mult), opts["atrN"]) if mult > 0 else "现价（首档）"
        if i >= 1 and opts["anchorSupport"] and len(support) >= i:
            sup = _num(support[i - 1].get("price"))
            if sup is not None and stop_px < sup < p_i:
                p_i = sup
                src = "锚定第 %d 个支撑 %s（%s）" % (i, _fmt(sup), support[i - 1].get("kind"))
        tiers.append({"price": round(p_i, nd), "w": opts["entryWeights"][i], "i": i, "src": src})

    labels = {0: "首档（试探）", 1: "次档（回踩加仓）", 2: "末档（左侧补仓）"}
    kept = []
    for t in tiers:
        if t["price"] <= stop_px:
            warns.append("第 %d 档建仓价 %s 已跌破初始止损 %s：该档已剔除"
                         "（把仓位买在止损之下等于放大风险），权重归一化到剩余档位。"
                         % (t["i"] + 1, _fmt(t["price"]), _fmt(stop_px)))
            continue
        if kept and t["price"] >= kept[-1]["price"]:
            warns.append("第 %d 档建仓价 %s 与上一档 %s 重叠（不构成更低的加仓价位）：该档已剔除。"
                         % (t["i"] + 1, _fmt(t["price"]), _fmt(kept[-1]["price"])))
            continue
        kept.append(t)
    if not kept:
        kept = [{"price": px, "w": 1.0, "i": 0, "src": "现价（唯一档）"}]
        warns.append("所有建仓档都落在初始止损之下，已退化为现价单档建仓（仓位会更小、更依赖择时）。")
    entries = []
    total_w = sum(t["w"] for t in kept)
    for t in kept:
        w = (t["w"] / total_w) if total_w > 0 else (1.0 / len(kept))
        entries.append({"price": t["price"], "weight": w,
                        "label": labels.get(t["i"], "第 %d 档" % (t["i"] + 1)),
                        "note": "%s；距现价 %s（%s×ATR%s）"
                                % (t["src"], _pct((t["price"] - px) / px if px else 0),
                                   _fmt((px - t["price"]) / atr if atr > 0 else 0),
                                   "，高于初始止损 %s" % _fmt(stop_px) if t["price"] > stop_px else "")})
    _fix_weights(entries)
    notes.append("分批建仓为自定口径（无现成开源标准）：本次权重 %s 依次对应「现价 / −%s×ATR（或首个支撑）/ "
                 "−%s×ATR（或第二支撑）」各档；权重随深度递减，是因为越靠近现价的档位越确定、"
                 "越深的档位越依赖行情继续给机会；硬约束是所有档位必须高于初始止损，"
                 "否则剔除该档并把剩余档权重重新归一化到 1。"
                 % ("/".join(_pct(w, 0) for w in opts["entryWeights"]),
                    _fmt(opts["entryAtr"][1] if len(opts["entryAtr"]) > 1 else 1.0),
                    _fmt(opts["entryAtr"][2] if len(opts["entryAtr"]) > 2 else 1.5)))

    # ---------------- 4. 分批止盈 ----------------
    targets = []
    for i, mult in enumerate(opts["targetAtr"]):
        targets.append({"price": round(px + mult * atr, nd), "weight": opts["targetWeights"][i],
                        "label": "T%d" % (i + 1),
                        "note": "现价 + %s×ATR(%d)；上方最近阻力 %s%s"
                                % (_fmt(mult), opts["atrN"],
                                   _fmt(resistance[0]["price"]) if resistance else "无",
                                   "（目标位按 ATR 倍数给，刻意不压低到单一压力位）" if resistance else "")})
    _fix_weights(targets)
    notes.append("止盈目标为 ATR 倍数（%s），刻意不用上方单一压力位向下压 —— 压力位会被突破，"
                 "而波动率尺度更稳定；分批权重 %s。"
                 % ("/".join("%s×ATR" % _fmt(m) for m in opts["targetAtr"]),
                    "/".join(_pct(w, 0) for w in opts["targetWeights"])))

    # ---------------- 5. 盈亏比 ----------------
    t1 = targets[0]["price"] if targets else None
    t2 = targets[1]["price"] if len(targets) > 1 else t1
    ratio1 = ((t1 - px) / stop_dist) if (t1 is not None and stop_dist > 0) else None
    ratio2 = ((t2 - px) / stop_dist) if (t2 is not None and stop_dist > 0) else None
    # 盈亏比刻意用「对外发布的价位」计算，保证前端与用户能用展示值复算（自洽优先）。
    # 但这样一来 4 位小数舍入会把 2.00 变成 1.99997，阈值比较必须留一点容差，
    # 否则「正好 2:1」的形态会被判成更差的一档（实测踩到过这个坑）。
    eps_rr = 0.005
    if ratio1 is None:
        verdict = "无法计算盈亏比（止损距离非正）"
    elif ratio1 >= 2.0 - eps_rr:
        verdict = "盈亏比良好（%.2f:1 ≥ 2:1，趋势跟随的常见门槛）" % ratio1
    elif ratio1 >= 1.5 - eps_rr:
        verdict = "盈亏比尚可（%.2f:1），需要胜率配合才划算" % ratio1
    else:
        verdict = ("盈亏比偏低（%.2f:1）：建议等更近的首档价、或把目标放到更远的阻力位再动手"
                   % ratio1)
    risk_reward = {"toT1": t1, "toT2": t2,
                   "ratio1": _r(ratio1, 3), "ratio2": _r(ratio2, 3), "verdict": verdict}

    # ---------------- 6. 风险预算（1% 规则） ----------------
    rb = risk_budget(px, stop_px, opts["capital"], opts["riskPct"], opts["lot"], opts["maxWeight"])
    risk = {"perTradePct": rb["perTradePct"], "shares": rb["shares"],
            "amount": _r(rb["amount"], 2), "weight": _r(rb["weight"], 6),
            "basis": rb["basis"]}
    if rb["shares"] <= 0:
        warns.append("按 1%% 风险预算与 %d 股/手计算，可买股数为 0：本金（%s）过小或止损过宽，"
                     "本计划在当前资金下无法执行（不要用「加大仓位」来凑一手）。"
                     % (opts["lot"], _fmt(opts["capital"], 2)))
    elif rb.get("limitedBy") == "max_weight":
        warns.append("手数由**单标的上限 %s**决定（1%% 规则算出的手数更大）：单一标的集中度已被系统性限制。"
                     % _pct(opts["maxWeight"]))
    if stop_dist <= 0:
        warns.append("止损距离为 0（现价与止损价相同），止损口径失效，请检查 ATR 与参数。")

    # ---------------- 7. 退出规则（优先级式） ----------------
    pos = dict(opts["position"] or {})
    if _num(pos.get("entry")) is None:
        pos["entry"] = px
    if _num(pos.get("stop")) is None:
        pos["stop"] = stop_px
    if _num(pos.get("high")) is None and hz > 0:
        pos["high"] = max(b["high"] for b in clean[-hz:])
    if pos.get("days") is None:
        pos["days"] = 0
    exits = exit_rules(clean, pos, opts)
    hit = [it for it in exits if it.get("triggered")]
    if hit:
        notes.append("退出规则：当前已触发 %d 条，按优先级应执行「%s」（priority %d）—— "
                     "优先级最小的规则胜出，避免同时收到自相矛盾的建议。"
                     % (len(hit), hit[0]["rule"], hit[0]["priority"]))
    else:
        notes.append("退出规则：当前 7 条均未触发；同时触发时按 priority 最小的执行"
                     "（priority 1 = 动量衰竭，7 = ATR 止损）。")

    # ---------------- 8. 持有周期与置信度 ----------------
    holding = {"days": hz,
               "note": "计划持有周期 = horizon = %d 根K线（%s）；滞涨规则的持有天数门槛与之对齐；"
                       "计划未在周期内走完时按退出规则处理，不为了「等回本」而下移止损。"
                       % (hz, "约一个月（20 个交易日）" if hz == 20 else "%d 个交易日" % hz)}
    if opts["position"] is not None and opts["position"].get("days") is not None:
        holding["note"] += "｜当前持仓已持有 %d 天。" % _int_p(opts["position"].get("days"), 0, 0, 100000)
    conf_note = ("样本量 = 参与点位统计的K线根数 %d（有效K线 %d 根，回看窗口上限 %d 根）："
                 "%d 根 → 置信度 %s。样本越少，支撑阻力与量能密集区越容易被单根异常K线带偏。"
                 "若按「历史窗口」口径（有效K线 − 回看窗口）则为 %d 个。"
                 % (sample, n, opts["volumeLookback"], sample, level,
                    max(0, n - opts["volumeLookback"])))
    if level == "低":
        conf_note += ("样本不足（< %d 根）：所有价位精度已降到 %s 元并标注「参考」，"
                      "不给「两位小数的精确价位」以免假精确。"
                      % (opts["sampleMid"], "0." + "0" * (nd - 1) + "1" if nd > 0 else "1"))
    confidence = {"level": level, "sample": sample, "note": conf_note}

    # ---------------- 9. 必备风险提示（产品的一部分，不是装饰） ----------------
    warnings = [
        "涨跌停无法成交：涨停时买单排不上（买不到）、跌停时卖单排不上（卖不出），"
        "此时所有价位都失效、止损形同虚设。" + _limit_hint(opts["code"], px),
        "T+1：A 股当日买入的股票当日不可卖出，止损最早只能在下一交易日执行（美股 T+0 无此限制）；"
        "隔夜跳空风险无法用日内止损规避。",
        "跳空缺口：本计划的价位建立在「连续成交」假设上，跳空开盘会让实际成交价显著差于计划价 ——"
        "止损的实际亏损可能远超单笔风险预算 %s，止盈也可能一次性跳过目标价。" % _pct(opts["riskPct"]),
        "样本与时效：本计划基于最近 %d 根有效K线（回看 %d 根）；样本不足或波动结构变化"
        "（缩量、涨跌幅制度变化、停牌复牌）时点位会失效，必须随行情滚动重算。" % (n, sample),
        "以上点位是概率分布的产物、不是承诺：触发与否取决于市场，不构成任何买卖建议。",
    ]
    warnings.extend(warns)

    # ---------------- 10. 口径说明 ----------------
    notes.append("止损 = max(现价 − %s×ATR(%d), 现价×(1 − %s))，再与 Chandelier(%d, %s×ATR) 取更紧者"
                 "（仅当 Chandelier 位于现价下方时参与；若它已贴住/高于现价，说明结构上已处于"
                 "「应离场」区间，只作参照并写 warning）；同时沿用 core/advisor.py 的 −%s 止损上限"
                 "（advisor 的研判计划用 1.5×ATR，本模块默认 2.0×ATR；要对齐研判页把 params.atrMult 设为 1.5）。"
                 % (_fmt(opts["atrMult"]), opts["atrN"], _pct(opts["maxStopPct"]),
                    opts["chandelierN"], _fmt(opts["chandelierMult"]), _pct(opts["maxStopPct"])))
    notes.append("价位精度：样本 ≥ %d 根给 %d 位小数%s。"
                 % (opts["sampleMid"], nd, "（样本不足，已降到 %s 元精度并标注参考）"
                    % ("0." + "0" * (nd - 1) + "1" if nd > 0 else "1") if level == "低" else ""))
    notes.append("asOf = 最后一根K线的时间（不是当前时间）：本点位对应 %s 这根K线。"
                 % (str(last.get("t")) if last.get("t") is not None else "未知时间（该K线缺 t 字段）"))
    if _num(price) is not None and last["close"] > 0:
        gap = abs(px_raw / last["close"] - 1.0)
        if gap >= 0.02:
            notes.append("传入价与最后一根收盘价相差 %s（%.4f → %.4f）：点位已按传入价重算，"
                         "但支撑阻力与量能分布仍来自历史K线。" % (_pct(gap), last["close"], px_raw))

    note = "口径说明：" + "；".join(notes) + "。以上均为「价位参照」，不是价格预测，也不构成买卖建议。"

    return {
        "ok": True,
        "code": opts["code"] if opts["code"] is not None else last.get("code"),
        "price": px,
        "atr": {"atr14": _r(atr, 4), "atr22": _r(atr22, 4),
                "pct": _r(atr / px * 100.0 if px else None, 3), "mult": opts["atrMult"],
                "n": opts["atrN"], "fallback": atr_fallback,
                "basis": "ATR(%d) = %s（占现价 %s）；ATR(%d) = %s；%s"
                         % (opts["atrN"], _fmt(atr), _pct(atr / px if px else 0),
                            opts["chandelierN"], _fmt(atr22),
                            "已按现价 2.0% 兜底（数据不足）" if atr_fallback else "未兜底，来自 core.indicators.ATR")},
        "asOf": last.get("t"),
        "support": support,
        "resistance": resistance,
        "pivot": pivot,
        "entries": entries,
        "targets": targets,
        "stop": {"initial": stop_px, "basis": "、".join(stop_bits) + "；距现价 %s"
                 % _pct(stop_dist / px if px else 0),
                 "trailStart": trail_start, "trailLockIn": trail_lock,
                 "chandelier": _r(chandelier, nd),
                 "note": stop_note + "｜" + _ATR_NOTE},
        "riskReward": risk_reward,
        "risk": risk,
        "exits": exits,
        "holding": holding,
        "confidence": confidence,
        "warnings": warnings,
        "note": note,
    }
