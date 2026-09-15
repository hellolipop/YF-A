# -*- coding: utf-8 -*-
"""绩效指标库（纯 Python 标准库实现，无任何第三方依赖）。

对外只暴露一个入口 :func:`summarize`：把「权益曲线 + 已实现成交」压缩成一组
常用的绩效指标，供回测统计、实盘跟踪与前端展示复用。

口径约定（非常重要）
--------------------
1. 所有「收益率 / 比率 / 占比」类字段统一为小数：``0.25`` 表示 +25%。
   包括 total_return、annualized_return、annualized_vol、max_drawdown、
   win_rate、exposure、alpha、beta、var95、best_day、worst_day、monthly[].return。
2. ``max_drawdown`` 取**正值**表示回撤幅度（0.2 = 最大回撤 20%），
   因此 ``calmar = annualized_return / max_drawdown``。
3. ``var95`` 取**历史模拟法 95% 分位数**（通常是负数），
   即「95% 置信度下单期损失不超过 |var95|」。
4. 金额类字段（avg_win / avg_loss / expectancy / cost_total / fee_total /
   slippage_total）与传入的货币单位一致；``avg_loss`` 保留负号。
5. ``max_drawdown_days`` 为最长回撤持续期：若权益曲线每个点都有可解析的
   日期则按**自然日**计，否则退化为**期数（K线根数）**。
6. 任何指标在数据不足或数学上退化（空数据、单点、除零、零方差、无交易……）
   时都不抛异常，统一返回 0.0 或 0，避免 NaN 污染调用方。
   唯一例外：完全没有亏损交易但存在盈利交易时，``profit_factor`` 语义上为
   无穷大，返回 ``float('inf')``。

输入约定
--------
equity : list[dict] | list[number]
    权益曲线，元素形如 ``{"t": "2024-01-02", "v": 101234.5, "close": 12.3,
    "position": True}``。``v`` 为该时点总权益（现金 + 持仓市值），
    ``close`` 为标的价格（``v`` 缺失时兜底），``position`` 为当根K线收盘后
    是否持仓（用于 exposure 统计）。也接受纯数值列表（按净值序列处理）。
trades : list[dict]
    已实现成交，元素形如 ``{"pnl": 1234.5, "pnlPct": 1.23, "bars": 5,
    "inDate": "2024-01-02", "outDate": "2024-01-09",
    "fee": 3.1, "slippage": 12.0}``。
    ``pnl`` 为该笔已实现盈亏（含费用，货币口径），``pnlPct`` 为百分比收益（%）；
    ``fee`` / ``slippage`` 为该笔交易付出的手续费 / 滑点成本（**金额**，非比率），
    用于汇总 cost_total。缺少这些字段时按 0 计。
initial : float
    初始资金。大于 0 时，权益曲线首点相对 initial 的涨跌也会计入收益序列。
mode : {"simple", "compound"}
    ``compound``（默认）：几何累乘口径，区间总收益 = 末值 / 首值 - 1，
    年化 = (1 + 总收益) ** (1 / 年数) - 1；
    ``simple``：算术累加口径，区间总收益 = Σ 单期收益，
    年化 = 总收益 / 年数（等价于期均收益 × periods_per_year）。
    两种口径共用同一份单期收益序列，仅聚合与年化方式不同。
    mode 大小写与空白不敏感；无法识别的取值按 ``compound`` 处理（不抛异常）。
periods_per_year : int
    每年期数（日线按 244 交易日）。
rf : float
    年化无风险利率（小数）。
bench : list | None
    基准序列，格式与 equity 相同（净值口径）；也支持 ``{"t":..,"ret":..}``
    形式的收益率序列或纯收益率列表以外的净值列表。
    与策略收益序列按**尾部对齐**（取最近 n 期）。为空则 alpha / beta 均为 0.0。
"""

import math
from datetime import date

__all__ = ["summarize", "MODES"]

#: 支持的收益口径
MODES = ("simple", "compound")
#: 默认每年期数（A股日线常用 244 个交易日）
DEFAULT_PPY = 244
#: 浮点比较用的相对容差
_EPS = 1e-12
_INF = float("inf")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(x):
    """宽松转 float。

    None / 布尔 / 无法解析的字符串 / NaN / ±inf 一律返回 None，
    以便调用方用「是否为 None」判断数据是否有效。
    """
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
    if v != v:                       # NaN
        return None
    if v == _INF or v == -_INF:      # 无穷
        return None
    return v


def _bool(x):
    """宽松转布尔：无法判定时返回 None（表示「没有该信息」）。"""
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return None if x != x else bool(x)
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "t"):
        return True
    if s in ("0", "false", "no", "n", "f", ""):
        return False
    return None


def _mean(seq):
    """算术平均；空序列返回 0.0。"""
    return math.fsum(seq) / len(seq) if seq else 0.0


def _stdev(seq):
    """样本标准差（ddof = 1）；样本数 < 2 或方差为 0 时返回 0.0。"""
    n = len(seq)
    if n < 2:
        return 0.0
    m = _mean(seq)
    var = math.fsum((x - m) ** 2 for x in seq) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _variance(seq):
    """样本方差（ddof = 1）；样本数 < 2 时返回 0.0。"""
    n = len(seq)
    if n < 2:
        return 0.0
    m = _mean(seq)
    var = math.fsum((x - m) ** 2 for x in seq) / (n - 1)
    return var if var > 0 else 0.0


def _covariance(a, b):
    """样本协方差（ddof = 1）；样本数 < 2 时返回 0.0。"""
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma, mb = _mean(a[:n]), _mean(b[:n])
    return math.fsum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (n - 1)


def _prod1(seq):
    """几何累乘 Π(1 + r)。"""
    out = 1.0
    for r in seq:
        out *= (1.0 + r)
    return out


def _percentile(seq, q):
    """线性插值分位数（与 numpy 默认算法一致），q ∈ [0, 1]。空序列返回 0.0。"""
    if not seq:
        return 0.0
    s = sorted(seq)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _month(t):
    """从时间戳截取 'YYYY-MM'；无法识别时返回 None。"""
    if t is None:
        return None
    s = str(t).strip()
    if len(s) >= 7:
        head = s[:7]
        if head[4] == "-" and head[:4].isdigit() and head[5:].isdigit():
            return head
    d = _parse_date(s)
    return "%04d-%02d" % (d.year, d.month) if d else None


def _parse_date(t):
    """把 '2024-01-02' / '2024/01/02' / '2024-01-02 15:00:00' 解析成 date。"""
    if t is None:
        return None
    s = str(t).strip()
    if not s:
        return None
    s = s[:10].replace("/", "-").replace(".", "-")
    parts = s.split("-")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (TypeError, ValueError):
        return None


def _out(x):
    """输出前归一化：None / NaN → 0.0，抹平浮点噪声，消除 -0.0。"""
    if x is None:
        return 0.0
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v:
        return 0.0
    if v == _INF or v == -_INF:
        return v
    return round(v, 12) + 0.0


# --------------------------------------------------------------------------- #
# 权益曲线
# --------------------------------------------------------------------------- #
def _clean_equity(equity):
    """规整权益曲线 → [{"t": str | None, "v": float, "position": bool | None}]。

    - 支持 dict 序列、纯数值序列、单个 dict；
    - ``v`` 缺失时依次回退 ``close``；
    - 非法（None / 非数值 / NaN）的点直接丢弃；
    - 所有点都带 ``t`` 时按 ``t`` 字符串升序排序（ISO 时间可直接比较），
      否则保持传入顺序。
    """
    if isinstance(equity, dict):
        equity = [equity]
    if not isinstance(equity, (list, tuple)):
        return []
    pts = []
    for raw in equity:
        if isinstance(raw, dict):
            v = _num(raw.get("v"))
            if v is None:
                v = _num(raw.get("close"))
            t = raw.get("t")
            pos = _bool(raw.get("position"))
        else:
            v, pos = _num(raw), None
            t = None
        if v is None:
            continue
        ts = None if t is None or str(t).strip() == "" else str(t)
        pts.append({"t": ts, "v": v, "position": pos})
    if pts and all(p["t"] is not None for p in pts):
        pts.sort(key=lambda p: p["t"])
    return pts


def _returns(pts, base):
    """由权益曲线构造单期收益序列。

    返回 ``(rets, tags)``：``rets[i]`` 为第 i 期收益率，``tags[i]`` 为该期
    归属的月份（无日期时为 None）。

    若 ``base``（初始资金）与曲线首点不同，会补一段「初始 → 首点」的收益，
    保证 ``prod(1 + rets) - 1`` 与「末值 / 初始 - 1」严格一致。
    """
    rets, tags = [], []
    if not pts:
        return rets, tags
    v0 = pts[0]["v"]
    if base > 0 and abs(base - v0) > _EPS * max(1.0, abs(base)):
        rets.append(v0 / base - 1.0)
        tags.append(_month(pts[0]["t"]))
    prev = v0
    for p in pts[1:]:
        cur = p["v"]
        # 权益归零属于极端退化情形，避免除零，该期收益记 0
        rets.append(cur / prev - 1.0 if prev else 0.0)
        tags.append(_month(p["t"]))
        prev = cur
    return rets, tags


def _drawdown(pts):
    """计算 (最大回撤幅度(正值), 最长回撤持续期)。"""
    if not pts:
        return 0.0, 0
    vals = [p["v"] for p in pts]
    dates = [_parse_date(p["t"]) for p in pts]
    use_days = len(vals) > 1 and all(d is not None for d in dates)

    mdd = 0.0
    peak, peak_i = vals[0], 0
    longest = 0
    underwater = False      # 是否正处于「未回到前高」的状态

    def span(a, b):
        """第 a 个点到第 b 个点的跨度：优先自然日，否则期数。"""
        return (dates[b] - dates[a]).days if use_days else (b - a)

    for i, v in enumerate(vals):
        if v < peak - _EPS * (abs(peak) + 1.0):
            # 跌破前高：进入回撤，更新最大回撤幅度
            underwater = True
            if peak > 0:
                dd = (peak - v) / peak
                if dd > mdd:
                    mdd = dd
        else:
            # 回到或创出新高：若此前确实回撤过，结算这段回撤的持续期
            if underwater and i > peak_i:
                d = span(peak_i, i)
                if d > longest:
                    longest = d
            underwater = False
            peak, peak_i = v, i

    # 期末仍未回到前高：回撤持续到最后一点
    if underwater and len(vals) - 1 > peak_i:
        d = span(peak_i, len(vals) - 1)
        if d > longest:
            longest = d
    return mdd, longest


def _exposure(pts):
    """持仓暴露：收盘后持仓的期数 / 总期数。

    没有任何 ``position`` 信息时返回 0.0（无法判断，不臆造）。
    """
    if not pts:
        return 0.0
    if not any(p["position"] is not None for p in pts):
        return 0.0
    held = sum(1 for p in pts if p["position"] is True)
    return held / float(len(pts))


def _annualize(total, n, ppy, mode):
    """把区间总收益折算成年化收益。"""
    if n <= 0 or ppy <= 0:
        return 0.0
    years = n / float(ppy)
    if years <= 0:
        return 0.0
    if mode == "compound":
        if total <= -1.0:
            return -1.0
        try:
            # log1p/expm1 比 (1+total) ** (1/years) 更稳，且能捕获极端外推溢出
            return math.expm1(math.log1p(total) / years)
        except (OverflowError, ValueError):
            return _INF if total > 0 else -1.0
    # 算术口径：总收益 / 年数 == 期均收益 × periods_per_year
    return total / years


# --------------------------------------------------------------------------- #
# 成交明细
# --------------------------------------------------------------------------- #
def _closed(trades):
    """筛出有效成交：必须是 dict 且 ``pnl`` 可解析为数值。"""
    out = []
    if isinstance(trades, dict):
        trades = [trades]
    if not isinstance(trades, (list, tuple)):
        return out
    for tr in trades:
        if not isinstance(tr, dict):
            continue
        if _num(tr.get("pnl")) is None:
            continue
        out.append(tr)
    return out


def _trade_stats(trades):
    """基于已实现成交统计胜率、盈亏比、持仓周期与成本。"""
    closes = _closed(trades)
    pnls = [_num(t.get("pnl")) for t in closes]
    pnls = [p for p in pnls if p is not None]

    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = math.fsum(wins)
    gross_loss = abs(math.fsum(losses))

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = _INF          # 无亏损交易，盈亏比无穷大
    else:
        profit_factor = 0.0

    # 持仓周期：优先用 bars，全部缺失时退化为 inDate/outDate 的自然日差
    bars = [_num(t.get("bars")) for t in closes]
    bars = [b for b in bars if b is not None]
    if bars:
        avg_hold = _mean(bars)
    else:
        days = []
        for t in closes:
            d1, d2 = _parse_date(t.get("inDate")), _parse_date(t.get("outDate"))
            if d1 is not None and d2 is not None:
                days.append((d2 - d1).days)
        avg_hold = _mean(days)

    fee_total = math.fsum(_num(t.get("fee")) or 0.0 for t in closes)
    slippage_total = math.fsum(_num(t.get("slippage")) or 0.0 for t in closes)

    return {
        "trades": n,
        "win_rate": (len(wins) / float(n)) if n else 0.0,
        "profit_factor": profit_factor,
        "avg_win": _mean(wins),
        "avg_loss": _mean(losses),      # 保留负号
        "expectancy": (math.fsum(pnls) / n) if n else 0.0,
        "avg_hold": avg_hold,
        "fee_total": fee_total,
        "slippage_total": slippage_total,
        "cost_total": fee_total + slippage_total,
    }


# --------------------------------------------------------------------------- #
# 基准
# --------------------------------------------------------------------------- #
def _bench_returns(bench):
    """把基准输入解析成单期收益序列。"""
    if bench is None:
        return []
    if isinstance(bench, dict):
        bench = [bench]
    if not isinstance(bench, (list, tuple)):
        return []

    values, direct = [], []
    has_direct = False
    for item in bench:
        if isinstance(item, dict):
            ret = None
            for key in ("ret", "return", "r"):
                ret = _num(item.get(key))
                if ret is not None:
                    break
            if ret is not None:
                direct.append(ret)
                has_direct = True
                continue
            v = _num(item.get("v"))
            if v is None:
                v = _num(item.get("close"))
            if v is not None:
                values.append(v)
        else:
            v = _num(item)
            if v is not None:
                values.append(v)

    if has_direct:
        return direct
    out = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        out.append(values[i] / prev - 1.0 if prev else 0.0)
    return out


def _alpha_beta(rets, bench, mode, ppy, rf, ann_ret):
    """年化 Jensen alpha 与 beta（按尾部对齐）。无有效基准时均为 0.0。"""
    rb = _bench_returns(bench)
    if not rb or len(rets) < 2:
        return 0.0, 0.0
    k = min(len(rets), len(rb))
    a, b = rets[-k:], rb[-k:]
    var_b = _variance(b)
    beta = (_covariance(a, b) / var_b) if var_b > 0 else 0.0

    if mode == "compound":
        ann_bench = _annualize(_prod1(b) - 1.0, k, ppy, "compound")
    else:
        ann_bench = _mean(b) * ppy
    alpha = ann_ret - (rf + beta * (ann_bench - rf))
    return alpha, beta


# --------------------------------------------------------------------------- #
# 月度
# --------------------------------------------------------------------------- #
def _monthly(pts, rets, tags, trades, mode):
    """按月汇总：收益率 + 已实现盈亏。

    月份集合 = 权益曲线出现过的月份 ∪ 成交平仓所在的月份（升序）。
    当月收益由该月的单期收益聚合而成（compound 用连乘、simple 用求和），
    当月无收益观测时为 0.0；realized 为当月平仓交易的已实现盈亏合计。
    """
    buckets = {}

    def bucket(mo):
        return buckets.setdefault(
            mo, {"rets": [], "realized": 0.0, "trades": 0, "wins": 0})

    for p in pts:
        mo = _month(p["t"])
        if mo:
            bucket(mo)
    for r, tag in zip(rets, tags):
        if tag:
            bucket(tag)["rets"].append(r)
    for tr in _closed(trades):
        mo = _month(tr.get("outDate")) or _month(tr.get("inDate"))
        if not mo:
            continue
        pnl = _num(tr.get("pnl")) or 0.0
        b = bucket(mo)
        b["realized"] += pnl
        b["trades"] += 1
        if pnl > 0:
            b["wins"] += 1

    out = []
    for mo in sorted(buckets):
        b = buckets[mo]
        rs = b["rets"]
        if mode == "compound":
            ret = (_prod1(rs) - 1.0) if rs else 0.0
        else:
            ret = math.fsum(rs)
        out.append({
            "month": mo,
            "return": _out(ret),
            "realized": _out(b["realized"]),
            "trades": b["trades"],
            "wins": b["wins"],
        })
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def summarize(equity, trades=None, initial=0.0, mode="compound",
              periods_per_year=DEFAULT_PPY, rf=0.0, bench=None):
    """汇总权益曲线与成交明细，返回绩效指标 dict（字段名全部用下划线）。

    参数含义与口径见模块头部说明。任何异常输入都不会抛错，退化情形返回 0。
    """
    # ---- 参数归一化 ------------------------------------------------------ #
    m = str(mode).strip().lower() if mode is not None else "compound"
    if m not in MODES:
        m = "compound"                     # 未知口径按默认处理，不抛异常
    ppy = _num(periods_per_year)
    if ppy is None or ppy <= 0:
        ppy = float(DEFAULT_PPY)
    rate = _num(rf)
    rate = rate if rate is not None else 0.0
    start = _num(initial)
    start = start if (start is not None and start > 0) else None

    # ---- 收益序列 -------------------------------------------------------- #
    pts = _clean_equity(equity)
    base = start if start is not None else (pts[0]["v"] if pts else 0.0)
    rets, tags = _returns(pts, base)
    n = len(rets)

    if not pts:
        total = 0.0
    elif m == "compound":
        total = (pts[-1]["v"] / base - 1.0) if base > 0 else 0.0
    else:
        total = math.fsum(rets)

    ann_ret = _annualize(total, n, ppy, m)
    mdd, mdd_days = _drawdown(pts)

    # ---- 风险调整指标 ---------------------------------------------------- #
    if m == "compound":
        rf_per = (math.expm1(math.log1p(rate) / ppy) if rate > -1 else rate / ppy)
    else:
        rf_per = rate / ppy
    excess = _mean(rets) - rf_per
    sd = _stdev(rets)
    vol = sd * math.sqrt(ppy)
    sharpe = (excess / sd * math.sqrt(ppy)) if sd > 0 else 0.0

    if n:
        downside = math.sqrt(math.fsum(min(0.0, r - rf_per) ** 2 for r in rets) / n)
    else:
        downside = 0.0
    sortino = (excess / downside * math.sqrt(ppy)) if downside > 0 else 0.0
    calmar = (ann_ret / mdd) if mdd > 0 else 0.0

    # ---- 成交 / 基准 / 月度 ---------------------------------------------- #
    ts = _trade_stats(trades)
    alpha, beta = _alpha_beta(rets, bench, m, ppy, rate, ann_ret)

    return {
        "total_return": _out(total),
        "annualized_return": _out(ann_ret),
        "annualized_vol": _out(vol),
        "sharpe": _out(sharpe),
        "sortino": _out(sortino),
        "calmar": _out(calmar),
        "max_drawdown": _out(mdd),
        "max_drawdown_days": int(mdd_days),
        "win_rate": _out(ts["win_rate"]),
        "profit_factor": _out(ts["profit_factor"]),
        "avg_win": _out(ts["avg_win"]),
        "avg_loss": _out(ts["avg_loss"]),
        "expectancy": _out(ts["expectancy"]),
        "trades": int(ts["trades"]),
        "avg_hold": _out(ts["avg_hold"]),
        "cost_total": _out(ts["cost_total"]),
        "fee_total": _out(ts["fee_total"]),
        "slippage_total": _out(ts["slippage_total"]),
        "exposure": _out(_exposure(pts)),
        "alpha": _out(alpha),
        "beta": _out(beta),
        "var95": _out(_percentile(rets, 0.05)),
        "best_day": _out(max(rets)) if rets else 0.0,
        "worst_day": _out(min(rets)) if rets else 0.0,
        "monthly": _monthly(pts, rets, tags, trades, m),
    }
