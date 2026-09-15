# -*- coding: utf-8 -*-
"""凯利公式仓位管理与组合分配（纯 Python 标准库实现，无任何第三方依赖）。

对外暴露四个函数：

==========================  ==============================================================
:func:`kelly_fraction`      离散凯利：f* = (p·b − q) / b，适合「胜率 + 赔率」可估的场景
:func:`kelly_continuous`    连续凯利：f* = μ / σ²，适合「期望收益 + 方差」可估的场景
:func:`fractional`          分数凯利 + 上下限截断（单标的，实盘落地的关键一步）
:func:`allocate`            组合分配：归一化 + 现金缓冲 + 整手取整 + 资金不足缩减
==========================  ==============================================================

口径约定（重要）
----------------
1. 所有「仓位 / 权重 / 胜率 / 比例」一律为**小数**：``0.25`` 表示 25%（不是 25）。
2. ``f* ≤ 0`` 表示该机会**无优势**，不建议建仓；因此对外返回的 ``f`` 一律截断为
   ``≥ 0``，未截断的原始值放在 ``raw`` 字段（可能为负），便于归因与审计。
3. 凯利公式给出的是「理论最优仓位」，实盘必须再做三件事：**分数化**（half-Kelly
   等）、**单标的上限**、**下限归零**（太小的仓位没有意义）。这三件事统一由
   :func:`fractional` 完成，它不关心组合。
4. 组合层面（:func:`allocate`）另有两条约束：
   a. **现金缓冲**：计划仓位上限 ``capacity = 1 − cash_buffer``，永远留出一部分
      现金，避免同时满仓、踩踏与流动性风险；
   b. **单标的上限**：归一化后若某只超过 ``max_weight`` 则截断，截断产生的剩余
      额度**不再二次分配**（避免超配），直接留作现金。
5. 股数取整：A 股按 100 股一手、美股按 1 股，采用**就近取整（半手向上）**而不是
   一律向下取整 —— 否则小资金账户会长期买不到一手，仓位长期系统性偏低。就近
   取整可能让实际占用金额略微超出计划投入资金，此时进入第 6.b 条。
6. **资金不足的两种「按比例缩减」**：
   a. 归一化阶段：若各标的分数凯利权重之和超过 ``capacity``，乘以
      ``capacity / 权重之和`` 等比缩减到计划仓位；
   b. 取整阶段：若整手取整后实际占用金额超过 ``capacity × 总资金``，乘以
      ``预算 / 实际金额`` 等比缩减并**向下取整**到整手，确保预算不被突破
      （向下取整保证不会因浮点误差再次超支）。
7. 数据不足、参数非法、除零等退化情形一律**不抛异常**，返回 0 仓位并在
   ``note`` / ``reason`` 字段给出中文说明（与本项目 ``core/metrics.py`` 的风格一致）。
8. 本模块只回答「买多少」：不产生信号、不决定成交价、不考虑手续费与滑点，
   也不考虑 T+1 与卖空限制。成交价与成本分别由 ``core/fills.py``、
   ``core/portfolio.py`` 负责，保持职责单一、可独立替换。
"""

import math

__all__ = [
    "kelly_fraction", "kelly_continuous", "fractional", "allocate",
    "LOTS", "MIN_SAMPLES", "DEFAULT_FRACTION", "DEFAULT_MAX_WEIGHT",
    "DEFAULT_MIN_WEIGHT", "DEFAULT_CASH_BUFFER",
]

#: 各市场最小交易单位（A 股 100 股一手，美股 1 股）
LOTS = {"cn": 100, "us": 1}
#: 默认分数凯利系数（半凯利，实盘最常用）
DEFAULT_FRACTION = 0.5
#: 默认单标的上限（占总资金比例）
DEFAULT_MAX_WEIGHT = 0.25
#: 默认单标的下限（低于该权重直接归零，避免碎仓）
DEFAULT_MIN_WEIGHT = 0.02
#: 默认现金缓冲（占总资金比例）
DEFAULT_CASH_BUFFER = 0.1
#: 默认最小样本量：候选自带 samples / n / trades 字段且低于该值时视为样本不足
MIN_SAMPLES = 30

_EPS = 1e-12
_INF = float("inf")

# --------------------------------------------------------------------------- #
# 通用口径说明（挂到返回值 note 上，方便调用方与前端直接展示）
# --------------------------------------------------------------------------- #
_NOTE_FRACTION = (
    "离散凯利口径：f* = (p·b − q) / b，p 为胜率（小数），q = 1 − p，"
    "b 为赔率（平均盈利 / 平均亏损）；f* ≤ 0 表示无优势、不建仓；"
    "f* > cap 时按 cap 截断；结果为占总资金的比例，未做分数缩放，"
    "实盘请配合 fractional 使用；未计手续费、滑点与破产风险。"
)
_NOTE_CONTINUOUS = (
    "连续凯利口径：f* = μ / σ²，μ 为单期期望收益（小数），σ² 为单期收益方差；"
    "假设收益近似正态且可无限分割，方差越小编出的仓位越激进（f* > 1 意味着需要"
    "杠杆）；f* ≤ 0 表示无优势、不建仓；实盘请配合 fractional 做分数化与截断。"
)
_NOTE_FRACTIONAL = (
    "单标的分数凯利口径：weight = f × fraction（默认半凯利）；先按 max_weight 截断，"
    "再对低于 min_weight 的结果归零；f ≤ 0（无优势）直接归零；"
    "本函数不做组合归一化，组合层面见 allocate。"
)
_NOTE_ALLOCATE = (
    "组合分配口径：先按分数凯利给每只定权（f × fraction，上限 max_weight、"
    "下限 min_weight），再等比归一化到计划仓位（capacity = 1 − 现金缓冲 cash_buffer）；"
    "股数按 A 股 100 股 / 美股 1 股就近取整（半手向上）；取整后若超出计划投入资金"
    "则按比例缩减并向下取整；weight / amount 为占总资金的比例与金额（目标值），"
    "actual 为实际占用资金；未计手续费、印花税、滑点，也未考虑 T+1 与卖空限制。"
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(x):
    """宽松转 float：None / 布尔 / 无法解析的字符串 / NaN / ±inf 一律返回 None。"""
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
    if v != v or v == _INF or v == -_INF:
        return None
    return v


def _param(x, default):
    """比例类控制参数取值：非法（None / 非数值 / NaN / inf）回退默认值，数值按 [0, 1] 截断。"""
    v = _num(x)
    if v is None:
        return float(default)
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _clean(v):
    """消除 -0.0，避免污染调用方与快照对比。"""
    return 0.0 if v == 0 else float(v)


def _pct(v):
    """小数 → 百分比字符串，仅用于中文说明文案。"""
    return "%.2f%%" % (v * 100.0)


def _round_lot(shares, lot):
    """就近取整到整手（半手向上），返回 int 股数。"""
    if shares <= 0 or lot <= 0:
        return 0
    return int(shares / lot + 0.5) * lot


def _floor_lot(shares, lot):
    """向下取整到整手，返回 int 股数。"""
    if shares <= 0 or lot <= 0:
        return 0
    return int(shares // lot) * lot


# --------------------------------------------------------------------------- #
# 一、离散凯利
# --------------------------------------------------------------------------- #
def kelly_fraction(win_rate, payoff, cap=1.0):
    """离散凯利公式：``f* = (p·b − q) / b``。

    参数
    ----
    win_rate : float
        胜率 p，取值 ``[0, 1]`` 的**小数**（0.55 表示 55%）。
    payoff : float
        赔率 b = 平均盈利 / 平均亏损，必须 ``> 0``（例如 2 表示「赢 2 元亏 1 元」）。
    cap : float
        仓位上限，默认 ``1.0``（不加杠杆）。``f* > cap`` 时按 cap 截断。
        非数值或负数时回退默认值 1.0。

    返回
    ----
    dict
        ``f`` 建议仓位（已截断，``≥ 0``）；``raw`` 未截断的 f*（可能为负，
        即无优势）；``edge`` 期望优势 ``p·b − q``；``capped`` 是否被上限截断；
        ``valid`` 输入是否合法；``advice`` 中文结论；``note`` 口径说明。

    说明
    ----
    非法输入（胜率不在 [0, 1]、赔率 ≤ 0、非数值）一律按 0 处理并置
    ``valid = False``，不抛异常；``f* ≤ 0`` 表示**无优势，不建议建仓**。
    """
    # cap 允许显式给 0（强制空仓），但不允许负数：负数与非数值一样回退默认 1.0；
    # 同时不设上限，便于调用方自行留出加杠杆空间。
    cap_num = _num(cap)
    cap_v = 1.0 if (cap_num is None or cap_num < 0.0) else cap_num
    p = _num(win_rate)
    b = _num(payoff)
    valid = (p is not None and 0.0 <= p <= 1.0 and b is not None and b > 0.0)

    if not valid:
        return {
            "f": 0.0, "raw": 0.0, "edge": 0.0,
            "win_rate": p, "payoff": b, "cap": cap_v,
            "capped": False, "valid": False,
            "advice": "输入非法（胜率需在 0~1、赔率需 > 0），按 0 处理：不建仓",
            "note": _NOTE_FRACTION,
        }

    q = 1.0 - p
    edge = p * b - q              # 每下注 1 元的数学期望优势
    raw = edge / b                # f* = (p·b − q) / b
    capped = raw > cap_v
    f = cap_v if capped else raw
    if f < 0.0:
        f = 0.0                   # f* ≤ 0：无优势，输出 0 而非负仓位

    if raw <= 0.0:
        advice = "无优势（期望优势 %.4f ≤ 0），不建议建仓" % (edge,)
    elif capped:
        advice = "具备优势，原始凯利 %s 超过上限 %s，已截断为 %s" % (
            _pct(raw), _pct(cap_v), _pct(f))
    else:
        advice = "具备优势，建议仓位 %s" % (_pct(f),)

    return {
        "f": _clean(f), "raw": _clean(raw), "edge": _clean(edge),
        "win_rate": p, "payoff": b, "cap": cap_v,
        "capped": bool(capped), "valid": True,
        "advice": advice, "note": _NOTE_FRACTION,
    }


# --------------------------------------------------------------------------- #
# 二、连续凯利
# --------------------------------------------------------------------------- #
def kelly_continuous(mean_ret, var_ret):
    """连续凯利公式：``f* = μ / σ²``。

    参数
    ----
    mean_ret : float
        单期期望收益 μ（小数，例如日频 0.001 表示日均 0.1%）。
    var_ret : float
        单期收益方差 σ²，必须 ``> 0``（方差为 0 时仓位数学上发散，按非法处理）。

    返回
    ----
    dict
        ``f`` 建议仓位（``≥ 0``）；``raw`` 原始 ``μ / σ²``（可能为负）；
        ``vol`` 波动率 σ（便于解读）；``valid``；``advice``；``note``。

    说明
    ----
    与 :func:`kelly_fraction` 保持同一对外口径：无优势（``μ ≤ 0``）时
    ``f = 0`` 且保留负的 ``raw``。本函数**不做上限截断** —— 连续凯利在
    低波动样本上会给出极端值（``f* > 1`` 意味着需要杠杆），实盘请务必
    再用 :func:`fractional` 做分数化与截断。
    """
    mu = _num(mean_ret)
    var = _num(var_ret)
    valid = mu is not None and var is not None and var > 0.0

    if not valid:
        return {
            "f": 0.0, "raw": 0.0, "mean_ret": mu, "var_ret": var, "vol": 0.0,
            "valid": False,
            "advice": "输入非法（均值需为数值、方差需 > 0），按 0 处理：不建仓",
            "note": _NOTE_CONTINUOUS,
        }

    raw = mu / var
    f = raw if raw > 0.0 else 0.0
    vol = math.sqrt(var)

    if raw <= 0.0:
        advice = "无优势（μ = %.6f ≤ 0），不建议建仓" % (mu,)
    elif f > 1.0:
        advice = "建议仓位 %s（> 100%%，意味着需要杠杆，实盘须截断）" % (_pct(f),)
    else:
        advice = "具备优势，建议仓位 %s（波动率 σ = %.6f）" % (_pct(f), vol)

    return {
        "f": _clean(f), "raw": _clean(raw),
        "mean_ret": mu, "var_ret": var, "vol": vol,
        "valid": True,
        "advice": advice, "note": _NOTE_CONTINUOUS,
    }


# --------------------------------------------------------------------------- #
# 三、分数凯利 + 上下限截断（单标的）
# --------------------------------------------------------------------------- #
def fractional(f, fraction=DEFAULT_FRACTION, max_weight=DEFAULT_MAX_WEIGHT,
               min_weight=DEFAULT_MIN_WEIGHT):
    """对凯利值做分数缩放与上下限截断，得到单标的可执行权重。

    ``weight = f × fraction``，随后：

    1. 超过 ``max_weight`` → 截断到 ``max_weight``（``capped = True``）；
    2. 低于 ``min_weight`` → 归零（``zeroed = True``，避免碎仓与手续费吞噬）；
    3. ``f ≤ 0``（无优势）→ 直接归零。

    参数
    ----
    f : float
        凯利值（通常来自 :func:`kelly_fraction` 或 :func:`kelly_continuous`）。
    fraction : float
        分数凯利系数，默认 ``0.5``（半凯利）。按 ``[0, 1]`` 截断。
    max_weight : float
        单标的上限，默认 ``0.25``。按 ``[0, 1]`` 截断；为 0 时全部归零。
    min_weight : float
        单标的下限，默认 ``0.02``。按 ``[0, 1]`` 截断。

    返回
    ----
    dict
        ``weight`` 最终权重；``raw`` 截断前的 ``f × fraction``；``kelly`` 输入 f；
        ``capped`` / ``zeroed`` 标记；``reason`` 处理原因；``note`` 口径说明。

    边界
    ----
    ``max_weight < min_weight``（参数矛盾）时，任何权重都会先被上限压低、
    再被下限归零，结果为全 0，并在 ``reason`` 中明确标注该矛盾。
    """
    frac = _param(fraction, DEFAULT_FRACTION)
    mx = _param(max_weight, DEFAULT_MAX_WEIGHT)
    mn = _param(min_weight, DEFAULT_MIN_WEIGHT)
    k = _num(f)
    valid = k is not None
    kelly = k if valid else 0.0
    raw = kelly * frac

    weight = raw
    capped = False
    zeroed = False
    reason = ""

    if raw <= 0.0:
        weight = 0.0
        zeroed = True
        if valid:
            reason = "无优势（凯利 %s ≤ 0），权重归零" % (_pct(kelly),)
        else:
            reason = "凯利值非法，权重归零"
    else:
        if weight > mx:
            weight = mx
            capped = True
        if weight < mn:
            weight = 0.0
            zeroed = True
            reason = "分数凯利 %s 低于下限 %s，权重归零" % (_pct(raw), _pct(mn))
        elif capped:
            reason = "分数凯利 %s 超过上限 %s，已截断" % (_pct(raw), _pct(mx))
        else:
            reason = "分数凯利 %s 位于 [%s, %s] 区间内" % (_pct(raw), _pct(mn), _pct(mx))

    if mx < mn:
        reason = "参数矛盾：max_weight(%s) < min_weight(%s)，权重全部归零" % (
            _pct(mx), _pct(mn))

    return {
        "weight": _clean(weight), "raw": _clean(raw), "kelly": kelly,
        "fraction": frac, "max_weight": mx, "min_weight": mn,
        "capped": bool(capped), "zeroed": bool(zeroed), "valid": bool(valid),
        "reason": reason, "note": _NOTE_FRACTIONAL,
    }


# --------------------------------------------------------------------------- #
# 四、组合分配
# --------------------------------------------------------------------------- #
def _pick_kelly(d):
    """从候选字典提取凯利值，返回 ``(kelly, kind, source)``。

    优先级：显式 ``kelly`` / ``f`` → 离散（``win_rate`` + ``payoff``）→
    连续（``mean_ret`` + ``var_ret``）→ 无法估计（0）。
    负的凯利值统一截断为 0（无优势不建仓）。
    """
    if "kelly" in d or "f" in d:
        key = "kelly" if "kelly" in d else "f"
        v = _num(d.get(key))
        if v is None:
            return 0.0, "invalid", "显式凯利值无法解析，按 0 处理"
        return max(0.0, v), "explicit", "采用调用方显式给定的凯利值"
    if d.get("win_rate") is not None or d.get("payoff") is not None:
        r = kelly_fraction(d.get("win_rate"), d.get("payoff"))
        return r["f"], "discrete", r["advice"]
    if d.get("mean_ret") is not None or d.get("var_ret") is not None:
        r = kelly_continuous(d.get("mean_ret"), d.get("var_ret"))
        return r["f"], "continuous", r["advice"]
    return 0.0, "none", "缺少 kelly / (win_rate, payoff) / (mean_ret, var_ret)，无法估计优势"


def allocate(candidates, capital, fraction=DEFAULT_FRACTION,
             max_weight=DEFAULT_MAX_WEIGHT, cash_buffer=DEFAULT_CASH_BUFFER,
             min_weight=DEFAULT_MIN_WEIGHT, min_samples=MIN_SAMPLES):
    """按凯利口径把资金分配到多只标的上，输出每只的权重 / 金额 / 股数。

    流程（每一步都写入返回值，便于回测与前端解释）

    1. **单只定权**：每只候选按 ``显式 kelly → 离散 → 连续`` 的顺序取凯利值，
       经 :func:`fractional` 得到分数凯利权重（含上限截断与下限归零）；
       自带 ``samples`` / ``n`` / ``trades`` 字段且低于 ``min_samples`` 的候选
       视为**样本不足**，权重归零并标注原因。
    2. **归一化**：以分数凯利权重为比例，等比缩放到计划仓位
       ``capacity = 1 − cash_buffer``（权重之和超过 capacity 时缩小，
       不足时放大，保证既定风险预算被用起来）；归一化后若单只超过
       ``max_weight`` 则截断，剩余额度不再二次分配、留作现金。
    3. **整手取整**：``amount = 资金 × weight``，股数按 A 股 100 股 / 美股 1 股
       **就近取整**；目标金额不足一手时跳过并标注「不足一手」。
    4. **资金不足缩减**：若取整后总金额超过计划投入资金
       ``budget = 资金 × capacity``，按 ``budget / 实际总金额`` 等比缩减并
       **向下取整**到整手，确保预算不被突破。

    参数
    ----
    candidates : list[dict]
        候选标的，每只支持字段：

        - ``code`` / ``symbol`` / ``name``：标识（缺省用 ``#序号``）；
        - ``market``：``"cn"``（默认，100 股一手）或 ``"us"``（1 股）；
        - ``price`` / ``last`` / ``close``：现价；
        - ``kelly`` / ``f``：直接给定凯利值（优先级最高）；
        - ``win_rate`` + ``payoff``：走离散凯利；
        - ``mean_ret`` + ``var_ret``：走连续凯利；
        - ``samples`` / ``n`` / ``trades``：样本量（可触发样本不足）；
        - ``lot``：自定义最小交易单位。
    capital : float
        总资金（必须 > 0，否则全部按 0 股处理并标注）。
    fraction : float
        分数凯利系数，默认 ``0.5``。
    max_weight : float
        单标的上限，默认 ``0.25``。
    cash_buffer : float
        现金缓冲，默认 ``0.1``（计划仓位上限 90%）。
    min_weight : float
        单标的下限，默认 ``0.02``。
    min_samples : int
        最小样本量，默认 30。

    返回
    ----
    dict
        ``positions`` 全部候选明细（含未建仓的，用 ``action`` 区分，按权重降序）；
        ``orders`` 实际建仓子集（``shares > 0``）；``skipped`` 未建仓子集；
        资金汇总 ``capital / budget / invested / cash / invested_weight /
        cash_weight / target_cash``；参数回显与 ``reduced`` / ``shrink`` /
        ``count`` / ``note``。

    边界
    ----
    无优势（权重全零）、样本不足、权重超上限、目标金额不足一手、资金非法、
    取整后超预算（触发缩减）等情形均不抛异常，全部落到 ``reason`` / ``note``
    中说明。
    """
    cap_num = _num(capital)
    ok_capital = cap_num is not None and cap_num > 0.0
    cap_v = cap_num if ok_capital else 0.0

    frac = _param(fraction, DEFAULT_FRACTION)
    mx = _param(max_weight, DEFAULT_MAX_WEIGHT)
    mn = _param(min_weight, DEFAULT_MIN_WEIGHT)
    cb = _param(cash_buffer, DEFAULT_CASH_BUFFER)

    n_min_num = _num(min_samples)
    n_min = int(n_min_num) if (n_min_num is not None and n_min_num > 0) else MIN_SAMPLES

    capacity = max(0.0, 1.0 - cb)      # 计划仓位上限
    budget = cap_v * capacity          # 计划投入资金

    items = candidates if isinstance(candidates, (list, tuple)) else []
    rows = []

    # ---------------- 1. 单只定权 ----------------
    for idx, cand in enumerate(items):
        d = cand if isinstance(cand, dict) else None
        if d is None:
            rows.append({
                "code": "#%d" % (idx + 1), "name": "", "market": "cn",
                "price": None, "lot": LOTS["cn"], "samples": None,
                "kelly": 0.0, "kelly_kind": "none",
                "kelly_weight": 0.0, "weight": 0.0, "amount": 0.0,
                "shares": 0, "actual": 0.0, "capped": False, "zeroed": True,
                "action": "skip",
                "reason": "候选格式非法（应为 dict），已跳过",
                "note": _NOTE_ALLOCATE,
            })
            continue

        code = d.get("code") or d.get("symbol") or d.get("name") or ("#%d" % (idx + 1))
        code = str(code)
        name = str(d.get("name") or code)
        mkt = str(d.get("market") or "cn").strip().lower()
        mkt = "us" if mkt.startswith("us") else "cn"
        lot_num = _num(d.get("lot"))
        lot = int(lot_num) if (lot_num is not None and lot_num >= 1) else LOTS[mkt]

        price = _num(d.get("price", d.get("last", d.get("close"))))
        n = _num(d.get("samples", d.get("n", d.get("trades"))))

        kelly, kind, source = _pick_kelly(d)
        fr = fractional(kelly, frac, mx, mn)
        kelly_weight = fr["weight"]
        # kind 为 none / invalid 时，真正的原因是「估不出优势」或「值无法解析」，
        # 此时 fr 给出的「无优势归零」文案会掩盖根因，直接用 source 更准确。
        reason = source if kind in ("none", "invalid") else fr["reason"]

        if n is not None and int(n) < n_min:
            reason = "样本不足（%d < %d），权重归零" % (int(n), n_min)
            kelly_weight = 0.0

        rows.append({
            "code": code, "name": name, "market": mkt,
            "price": price, "lot": lot, "samples": int(n) if n is not None else None,
            "kelly": _clean(kelly), "kelly_kind": kind,
            "kelly_weight": _clean(kelly_weight), "weight": 0.0, "amount": 0.0,
            "shares": 0, "actual": 0.0,
            "capped": bool(fr["capped"]), "zeroed": bool(kelly_weight <= 0),
            "action": "skip", "reason": reason, "note": _NOTE_ALLOCATE,
        })

    # ---------------- 2. 归一化到计划仓位 ----------------
    total_weight = 0.0
    for r in rows:
        total_weight += r["kelly_weight"]

    scale = 0.0
    if total_weight > 0.0 and capacity > 0.0 and ok_capital:
        scale = capacity / total_weight
        for r in rows:
            w = r["kelly_weight"] * scale
            if w > mx:
                w = mx
                r["capped"] = True
            r["weight"] = _clean(w)

    allocated = 0.0
    for r in rows:
        allocated += r["weight"]
    residual = max(0.0, capacity - allocated)

    # ---------------- 3. 目标金额与整手取整 ----------------
    for r in rows:
        amount = cap_v * r["weight"]
        r["amount"] = _clean(amount)

        if not ok_capital:
            r["reason"] = "总资金非法（≤ 0），无法分配"
            continue
        if r["weight"] <= 0.0:
            continue
        if r["price"] is None or r["price"] <= 0.0:
            r["reason"] = "无有效价格，无法计算股数"
            continue

        shares = _round_lot(amount / r["price"], r["lot"])
        if shares <= 0:
            r["reason"] = "资金不足：目标金额 %.2f 不足一手（一手 ≈ %.2f）" % (
                amount, r["price"] * r["lot"])
        else:
            r["reason"] = "建仓：分数凯利权重 %s → 归一化权重 %s%s" % (
                _pct(r["kelly_weight"]), _pct(r["weight"]),
                "（已按上限 %s 截断）" % _pct(mx) if r["capped"] else "")
        r["shares"] = shares
        r["actual"] = _clean(shares * r["price"])

    # ---------------- 4. 资金不足：按比例缩减 ----------------
    invested = 0.0
    for r in rows:
        invested += r["actual"]

    over_budget = _clean(invested)
    shrink = 1.0
    reduced = False
    if invested > budget + _EPS and invested > 0.0:
        shrink = budget / invested
        reduced = True
        for r in rows:
            if r["shares"] > 0:
                r["shares"] = _floor_lot(r["shares"] * shrink, r["lot"])
                r["actual"] = _clean(r["shares"] * (r["price"] or 0.0))
                if r["shares"] <= 0:
                    r["reason"] = "资金不足：按比例缩减（×%.4f）后不足一手" % (shrink,)
        invested = 0.0
        for r in rows:
            invested += r["actual"]

    # ---------------- 5. 汇总与说明 ----------------
    for r in rows:
        r["action"] = "buy" if r["shares"] > 0 else "skip"
        bits = [
            "凯利=%s（%s）" % (_pct(r["kelly"]), r["kelly_kind"]),
            "分数凯利权重=%s" % _pct(r["kelly_weight"]),
            "归一化系数=%.4f" % (scale,),
            "目标权重=%s" % _pct(r["weight"]),
            "目标金额=%.2f" % (r["amount"],),
            "股数=%d（%d 股/手）" % (r["shares"], r["lot"]),
            "实际金额=%.2f" % (r["actual"],),
        ]
        r["note"] = _NOTE_ALLOCATE + "｜本只：" + "，".join(bits)

    rows.sort(key=lambda r: -r["weight"])          # 稳定排序：同权重保持输入顺序
    positions = rows
    orders = [r for r in rows if r["shares"] > 0]
    skipped = [r for r in rows if r["shares"] <= 0]

    cash = _clean(cap_v - invested)
    invested_weight = _clean(invested / cap_v) if cap_v > 0 else 0.0
    cash_weight = _clean(cash / cap_v) if cap_v > 0 else 0.0

    notes = []
    if not ok_capital:
        notes.append("总资金非法（≤ 0），未做任何分配，全部候选按 0 股返回")
    elif not rows:
        notes.append("候选列表为空，建议空仓")
    elif capacity <= 0.0:
        notes.append("现金缓冲为 100%，计划仓位为 0，建议空仓")
    elif total_weight <= 0.0:
        notes.append("全部候选无优势或样本不足（分数凯利权重合计为 0），建议空仓")
    else:
        notes.append("计划仓位上限 %.2f（现金缓冲 %s），计划投入资金 %.2f" % (
            capacity, _pct(cb), budget))
        if abs(scale - 1.0) > 1e-9:
            notes.append("分数凯利权重合计 %s，按 ×%.4f 归一化到计划仓位" % (
                _pct(total_weight), scale))
    if ok_capital and total_weight > 0.0 and capacity > 0.0 and residual > _EPS:
        notes.append("单标的上限截断后剩余 %s 额度不再二次分配，留作现金" % _pct(residual))
    if reduced:
        notes.append("资金不足：整手取整后需投入 %.2f，超出计划投入资金 %.2f，"
                     "已按 ×%.4f 等比缩减并向下取整到整手" % (over_budget, budget, shrink))
    if rows and not orders and ok_capital and total_weight > 0.0 and capacity > 0.0:
        notes.append("无标的满足「一手」资金要求，建议空仓或增加资金")
    notes.append("实际投入 %.2f（占资金 %s），现金 %.2f（占资金 %s）" % (
        invested, _pct(invested_weight), cash, _pct(cash_weight)))

    return {
        "positions": positions,
        "orders": orders,
        "skipped": skipped,
        "capital": cap_v,
        "valid_capital": bool(ok_capital),
        "fraction": frac,
        "max_weight": mx,
        "min_weight": mn,
        "cash_buffer": cb,
        "capacity": _clean(capacity),
        "budget": _clean(budget),
        "target_cash": _clean(cap_v * cb),
        "invested": _clean(invested),
        "invested_weight": invested_weight,
        "cash": cash,
        "cash_weight": cash_weight,
        "reduced": bool(reduced),
        "shrink": _clean(shrink) if reduced else 1.0,
        "count": len(orders),
        "note": _NOTE_ALLOCATE + "｜本次：" + "；".join(notes),
    }
