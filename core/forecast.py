# -*- coding: utf-8 -*-
"""AlphaDesk · 概率化预测：历史条件分布（core/forecast.py）

定位
----
把「当前状态 + 未来 horizon 根K线」这件事从「点预测」改造成「分布预测」：
不去猜明天涨多少，而是回答「历史上长得像现在的时候，后面 horizon 根K线
实际是怎么走的」。输出一组分位数 + 中位路径 + 上下轨，供前端画预测带。

算法（四步）
------------
1. 状态编码：把每根K线的状态编成 4 维特征，并对每一维做「分位归一」
   （取该维在自身历史中的平均秩 / (N-1)），得到 4 个 [0, 1] 分位：
     · ma_dev  均线偏离 = close / SMA(ma) - 1
     · rsi     RSI(rsi)（Wilder 平滑，复用 core.indicators.RSI）
     · mom     动量 = close / close[mom 根之前] - 1
     · vol     波动 = 最近 vol 根单期收益的标准差
   分位归一的目的：四类特征量纲完全不同（百分比 / 0~100 / 比率 / 波动率），
   直接算距离会被量纲大的特征主导，所以统一压到 [0, 1] 分位空间再比较。
2. 相似状态回找（距离度量，自选并说明）：
       d(a, b) = sqrt( Σ_k w_k · (a_k - b_k)² / Σ_k w_k )
   即在 4 维分位空间上的「加权欧氏距离」。因为每维都已落在 [0, 1]，
   且除以权重和做了归一，所以 d ∈ [0, 1]：0 = 四项分位完全一致，
   1 = 四项分位全部顶到两端。默认四项等权，可用 weights 覆盖。
   为什么是欧氏而不是别的：
     · 分位归一后各维等尺度，欧氏距离 = 「四项状态偏离的平方和」，可解释、
       可复现、无额外参数；
     · 余弦距离只看方向不看幅度，对「RSI 从 80 掉到 50」这类幅度变化不敏感；
     · DTW 需要序列对齐，复杂度 O(n²) 起，对「单点 4 维状态」没有必要。
3. 收益统计：取距离最近的 k 个历史窗口（默认 60），统计它们「其后 horizon
   根K线」的实际收益（close[i+h] / close[i] - 1）的分布：
   expected_return / median_return / up_prob / 5·25·50·75·95 分位。
4. 路径与预测带：对每个历史窗口保留完整路径（逐步比值），在每一步做横截面
   分位，得到中位路径与 5% / 95% 外轨、25% / 75% 内轨；首点固定为当前价
   （锚点），可直接拼在K线右侧画带状图。

防未来函数（关键）
------------------
候选窗口 i 必须同时满足：
  · i + horizon <= 最后一根（否则它的未来收益观测不到）；
  · i <= 最后一根 - horizon - 1（与「当前窗口」重叠的窗口剔除，否则等于拿
    同一段未来数据来评估自己）。
因此大约需要「预热 20 根 + horizon + 1」根K线才可能进入正常（非降级）模式。

降级链（degraded = True 时 note 中会写明原因）
---------------------------------------------
  level 0 正常：相似状态样本数 >= min_sample（默认 20）
  level 1 降级：满足特征筛选的窗口不足 → 改用「历史整体（无条件）分布」，即
                全部可观测窗口（不再筛状态；因为不要求特征预热，样本反而更多）
  level 2 降级：K线太短，连 horizon 期前视收益都构不出来 → 用单根收益按
                (1 + r)^horizon 复利外推到 horizon，样本 = 单根收益个数
  level 3 降级：有效K线 < 2 根（或 bars 为空）→ 零分布；bars 为空时 ok = False

置信度
------
confidence = sample_score × dispersion_score × (降级 ? 0.5 : 1)，截断到 [0, 1]：
  · sample_score = n / (n + 40)：样本越多越接近 1（40 个样本得 0.5）；
  · dispersion_score = 1 / (1 + std_matched / std_all)：条件分布相对无条件分布
    越集中（离散度越小），说明状态筛选越有信息量，越接近 1。
  它衡量的是「这次统计本身可信不可信」，不是涨跌概率，更不是收益承诺。

输入 / 输出口径
---------------
bars : list[dict]，元素形如 {"t": "2024-01-02", "open": 1.0, "high": 1.0,
    "low": 1.0, "close": 1.0, "volume": 100}。按「旧 → 新」排列（本函数不做
    排序校正）；仅需要 close，其余字段可缺。close 无效（缺失 / NaN / 字符串 /
    <= 0）的K线被整根剔除。t 缺省或无法解析时 path["t"] 返回 None。
horizon : 未来K线根数（>= 1）。
返回字段（JSON 友好，可直接丢给前端）：
    ok, horizon, asOf, bars, lastClose,
    expected_return, median_return, up_prob, quantiles{5,25,50,75,95},
    sample, confidence, degraded, degrade_reason, degrade_level,
    path{t, start, median, lo, hi, q25, q75, levels, horizon},
    state{当前特征原始值与分位}, match{distance, weights, k, pool, nearest},
    note, source
  其中 match.pool = 本次实际用作比较的历史窗口数，match.k = 最终进入统计的样本数
  （正常模式下 k = min(入参 k, pool)）；match.nearest 为最近 5 个样本的回溯明细。

  所有收益 / 概率均为小数：0.05 = +5%；up_prob = 「严格大于 0」的样本占比。
  对脏数据（缺字段 / NaN / 字符串 / 负数价格 / 空数组 / None）一律不抛异常。
纯标准库实现，仅复用同包 core.indicators，无任何第三方依赖。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta

from .indicators import RSI, SMA

__all__ = ["forecast", "DEFAULT_HORIZON", "DEFAULT_K", "MIN_SAMPLE",
           "FEATURE_NAMES", "QUANTILE_LEVELS"]

#: 默认预测跨度（未来K线根数）
DEFAULT_HORIZON = 5
#: 默认目标相似样本数（从候选池里取距离最近的 K 个）
DEFAULT_K = 60
#: 低于该样本量即降级为历史整体分布
MIN_SAMPLE = 20
#: 四项状态特征的顺序（后续所有向量按这个顺序拼）
FEATURE_NAMES = ("ma_dev", "rsi", "mom", "vol")
#: 特征中文名（便于前端 / 日志展示）
FEATURE_CN = {"ma_dev": "均线偏离", "rsi": "RSI", "mom": "动量", "vol": "波动"}
#: 默认特征权重（等权；分位空间内比较，量纲已对齐）
DEFAULT_WEIGHTS = {"ma_dev": 1.0, "rsi": 1.0, "mom": 1.0, "vol": 1.0}
#: 默认指标窗口
DEFAULT_WINDOWS = {"ma": 20, "rsi": 14, "mom": 10, "vol": 20}
#: 输出的分位点（百分位）
QUANTILE_LEVELS = (5, 25, 50, 75, 95)
#: 距离度量说明（回填到 match.distance）
DISTANCE_DESC = ("加权欧氏距离：4 维状态特征各自在自身历史中分位归一（落在 [0,1]），"
                 "按权重平方和归一后 d ∈ [0,1]，0 表示四项分位完全一致")
#: note 里用的距离度量简写
DISTANCE_SHORT = "4 维分位空间上的加权欧氏距离（各维分位归一后等尺度）"
SOURCE = "历史条件分布（相似状态回找 + 分位统计）"

#: 置信度的样本量半衰点（n = 40 时 sample_score = 0.5）
_CONF_SAMPLE_HALF = 40.0
#: 降级时的置信度折扣（降级结果只描述无条件分布，信息量更低）
_DEGRADE_PENALTY = 0.5
_EPS = 1e-12


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


def _r(x, nd=6):
    """四舍五入到 nd 位；无效值返回 None（避免 NaN 污染 JSON）。"""
    v = _num(x)
    return None if v is None else round(v, nd)


def _int_arg(v, dflt, lo):
    """宽松取整：解析失败用 dflt，结果不得小于 lo。"""
    x = _num(v)
    if x is None:
        x = dflt
    try:
        iv = int(x)
    except (TypeError, ValueError, OverflowError):
        iv = int(dflt)
    return iv if iv >= lo else lo


def _clean(bars):
    """清洗K线：只保留 close 有效的记录，返回 (closes, times)。

    close <= 0 视为无效（价格非正会让后续比值爆炸，直接剔除）。
    """
    closes, times = [], []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        c = _num(b.get("close"))
        if c is None or c <= 0:
            continue
        closes.append(c)
        times.append(b.get("t"))
    return closes, times


def _bar_returns(closes):
    """单期收益序列（长度 = len(closes) - 1）：rets[j] 对应 closes[j] → closes[j+1]。"""
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]


# --------------------------------------------------------------------------- #
# 1. 状态特征
# --------------------------------------------------------------------------- #
def _ma_dev(closes, n):
    """均线偏离：close / SMA(n) - 1（正 = 在均线上方，负 = 下方）。"""
    ma = SMA(closes, n)
    out = [None] * len(closes)
    for i, m in enumerate(ma):
        mv = _num(m)
        if mv is None or abs(mv) < _EPS:
            continue
        out[i] = closes[i] / mv - 1.0
    return out


def _momentum(closes, n):
    """动量：close / close[n 根之前] - 1。"""
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        base = closes[i - n]
        if base > 0:
            out[i] = closes[i] / base - 1.0
    return out


def _volatility(closes, n):
    """波动：最近 n 根单期收益的标准差（截至当前K线）。"""
    rets = _bar_returns(closes)
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        win = rets[i - n:i]
        if len(win) < n:
            continue
        m = sum(win) / n
        out[i] = (sum((v - m) ** 2 for v in win) / n) ** 0.5
    return out


def _rank_quantile(values):
    """把一维序列映射为「在自身历史中的平均秩分位」（[0, 1]，缺失值保持 None）。

    并列值取平均秩，保证同一个值得到同一个分位；历史样本 <= 1 时全部返回 None。
    """
    valid = sorted(v for v in values if v is not None)
    n = len(valid)
    out = [None] * len(values)
    if n <= 1:
        return out
    for i, v in enumerate(values):
        if v is None:
            continue
        lo, hi = bisect_left(valid, v), bisect_right(valid, v)   # [lo, hi) 为并列区
        out[i] = ((lo + hi - 1) / 2.0) / (n - 1)
    return out


def _features(closes, win):
    """四项状态特征的原始值（未做分位归一）。"""
    return {
        "ma_dev": _ma_dev(closes, win["ma"]),
        "rsi": RSI(closes, win["rsi"]),
        "mom": _momentum(closes, win["mom"]),
        "vol": _volatility(closes, win["vol"]),
    }


# --------------------------------------------------------------------------- #
# 2. 距离与统计
# --------------------------------------------------------------------------- #
def _distance(vec_a, vec_b, weights, weight_sum):
    """加权欧氏距离（分位空间，已按权重和归一，值域 [0, 1]）。"""
    acc = 0.0
    for k, name in enumerate(FEATURE_NAMES):
        d = vec_a[k] - vec_b[k]
        acc += weights[name] * d * d
    return (acc / weight_sum) ** 0.5


def _quantile(values, p):
    """分位数（线性插值，p 为百分位）。空序列返回 0.0。"""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(vals[0])
    pos = (p / 100.0) * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(vals[lo])
    frac = pos - lo
    return float(vals[lo] * (1.0 - frac) + vals[hi] * frac)


def _stats(rets):
    """把一组收益压成分布统计量（收益 / 概率为小数）。"""
    n = len(rets)
    if n == 0:
        return {"expected_return": 0.0, "median_return": 0.0, "up_prob": 0.0,
                "std": 0.0, "quantiles": {q: 0.0 for q in QUANTILE_LEVELS},
                "sample": 0}
    mean = sum(rets) / n
    std = (sum((v - mean) ** 2 for v in rets) / n) ** 0.5
    up = sum(1 for v in rets if v > 0) / n          # 严格大于 0 才算上涨
    qs = {q: _r(_quantile(rets, q)) for q in QUANTILE_LEVELS}
    return {"expected_return": _r(mean), "median_return": _r(_quantile(rets, 50)),
            "up_prob": round(up, 4), "std": _r(std), "quantiles": qs, "sample": n}


def _confidence(sample, matched_std, base_std, degraded):
    """置信度：样本量因子 × 离散度因子（降级再打 0.5 折扣），截断到 [0, 1]。"""
    if sample <= 0:
        return 0.0
    sample_score = sample / (sample + _CONF_SAMPLE_HALF)
    rel = matched_std / base_std if base_std > _EPS else 1.0
    disp_score = 1.0 / (1.0 + max(0.0, rel))
    conf = sample_score * disp_score
    if degraded:
        conf *= _DEGRADE_PENALTY
    return round(max(0.0, min(1.0, conf)), 4)


# --------------------------------------------------------------------------- #
# 3. 时间轴推断（仅用于画图，失败返回 None，不影响统计）
# --------------------------------------------------------------------------- #
_TIME_PATTERNS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                  "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                  "%Y%m%d%H%M", "%Y%m%d",
                  "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")


def _parse_time(x):
    """把时间字段解析为 datetime；支持常见字符串格式与 Unix 时间戳。"""
    if isinstance(x, datetime):
        return x
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            return datetime.fromtimestamp(float(x))
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(x, str):
        return None
    s = x.strip()
    for p in _TIME_PATTERNS:
        try:
            return datetime.strptime(s, p)
        except ValueError:
            continue
    return None


def _fmt_like(sample, dt):
    """按输入字段的格式输出时间（尽量与K线时间轴保持一致）。"""
    if not isinstance(sample, str):
        return dt.isoformat(sep=" ")
    s = sample.strip()
    if "-" in s and ":" in s:
        return dt.strftime("%Y-%m-%d %H:%M:%S" if len(s) >= 19 else "%Y-%m-%d %H:%M")
    if "/" in s and ":" in s:
        return dt.strftime("%Y/%m/%d %H:%M:%S" if len(s) >= 19 else "%Y/%m/%d %H:%M")
    if "-" in s:
        return dt.strftime("%Y-%m-%d")
    if "/" in s:
        return dt.strftime("%Y/%m/%d")
    if s.isdigit() and len(s) == 12:
        return dt.strftime("%Y%m%d%H%M")
    if s.isdigit() and len(s) == 8:
        return dt.strftime("%Y%m%d")
    return dt.isoformat(sep=" ")


def _future_times(times, horizon):
    """推断未来 horizon 根K线的时间轴（长度 horizon + 1，首点为最后一根）。

    用序列尾部若干时间点的**中位间隔**外推，兼容日线 / 分钟线；无法解析返回 None。
    """
    if not times:
        return None
    tail = times[-6:]
    parsed = [_parse_time(t) for t in tail]
    if len(parsed) < 2 or any(p is None for p in parsed):
        return None
    gaps = sorted(g.total_seconds() for g in
                  (parsed[i] - parsed[i - 1] for i in range(1, len(parsed)))
                  if g.total_seconds() > 0)
    if not gaps:
        return None
    step = gaps[len(gaps) // 2]
    cur = parsed[-1]
    out = [times[-1]]
    for _ in range(horizon):
        cur = cur + timedelta(seconds=step)
        out.append(_fmt_like(times[-1], cur))
    return out


# --------------------------------------------------------------------------- #
# 4. 说明文本
# --------------------------------------------------------------------------- #
def _note(degraded, reason, horizon):
    """生成 note：必须写明「历史条件分布」而非收益承诺，并在降级时写明原因。"""
    txt = (
        "本结果为历史条件分布统计：在历史K线中回找与当前状态（均线偏离 / RSI / 动量 / 波动 "
        "四项分位特征，采用 %s）最相似的窗口，统计这些窗口其后 %d 根K线的实际收益分布，"
        "据此给出中位路径与 5%% / 95%% 上下轨。它描述的是「历史上出现过类似状态时后来怎么走」，"
        "属于历史条件分布，而非对未来收益的预测或收益承诺，也不构成任何投资建议；"
        "样本量有限、市场结构变化、停牌与幸存者偏差都可能让实际结果显著偏离该分布。"
        % (DISTANCE_SHORT, horizon)
    )
    if degraded:
        txt += " 注意：本次已触发降级 —— %s，此时分位数与置信度的参考价值更低。" % (
            reason or "可用样本不足")
    return txt


def _empty(horizon, reason):
    """无有效K线时的兜底结果（零分布 + ok = False），字段与正常结果完全对齐。"""
    st = _stats([])
    return {
        "ok": False,
        "horizon": horizon,
        "asOf": None,
        "bars": 0,
        "lastClose": None,
        "expected_return": st["expected_return"],
        "median_return": st["median_return"],
        "up_prob": st["up_prob"],
        "quantiles": st["quantiles"],
        "sample": 0,
        "confidence": 0.0,
        "degraded": True,
        "degrade_reason": reason,
        "degrade_level": 3,
        "path": {"t": None, "start": None, "median": [], "lo": [], "hi": [],
                 "q25": [], "q75": [], "levels": _path_levels(), "horizon": horizon},
        "state": None,
        "match": {"distance": DISTANCE_DESC, "weights": dict(DEFAULT_WEIGHTS),
                  "k": 0, "pool": 0, "nearest": []},
        "note": _note(True, reason, horizon),
        "source": SOURCE,
    }


def _path_levels():
    """预测带各轨对应的分位点（百分位）。"""
    return {"lo": 5, "q25": 25, "q50": 50, "q75": 75, "hi": 95}


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #
def forecast(bars, horizon=DEFAULT_HORIZON, k=None, min_sample=MIN_SAMPLE,
             weights=None, windows=None):
    """对 bars 给出未来 horizon 根K线的收益分布、中位路径与上下轨。

    参数
    ----
    bars : list[dict]  历史K线（旧 → 新），需要 close；可选 t / open / high / low / volume。
    horizon : int      预测跨度（未来K线根数），默认 5。
    k : int | None     取多少个最近邻样本，默认 60（受候选池大小限制）。
    min_sample : int   样本数低于该值即降级为历史整体分布，默认 20；传 0 可关闭降级。
    weights : dict     四项特征的权重（键取自 FEATURE_NAMES），默认等权。
    windows : dict     指标窗口（ma / rsi / mom / vol），默认 20 / 14 / 10 / 20。

    返回
    ----
    dict，字段见模块 docstring；任何输入都不抛异常。
    """
    hz = _int_arg(horizon, DEFAULT_HORIZON, 1)
    ms = _int_arg(min_sample, MIN_SAMPLE, 0)
    k_target = _int_arg(k, DEFAULT_K, 1)

    # 权重与窗口（宽松覆盖，非法值忽略）
    w = dict(DEFAULT_WEIGHTS)
    if isinstance(weights, dict):
        for name in FEATURE_NAMES:
            v = _num(weights.get(name))
            if v is not None and v >= 0:
                w[name] = v
    win = dict(DEFAULT_WINDOWS)
    if isinstance(windows, dict):
        for key in win:
            v = _num(windows.get(key))
            if v is not None and v >= 1:
                win[key] = int(v)
    weight_sum = sum(w.values())
    if weight_sum <= _EPS:                      # 权重全 0 时退回等权，避免除零
        w = dict(DEFAULT_WEIGHTS)
        weight_sum = float(len(FEATURE_NAMES))

    closes, times = _clean(bars)
    n = len(closes)
    if n == 0:
        return _empty(hz, "无有效K线（bars 为空或 close 全部无效），已降级为零分布")
    last_close = closes[-1]
    now_idx = n - 1

    # ---------------- 1. 当前状态编码 ----------------
    raw = _features(closes, win)
    quant = {name: _rank_quantile(raw[name]) for name in FEATURE_NAMES}
    now_vec = [quant[name][now_idx] for name in FEATURE_NAMES]
    state_ok = all(v is not None for v in now_vec)
    # 特征预热长度：四项特征同时可用的最小下标
    need = max(win["ma"] - 1, win["rsi"], win["mom"], win["vol"])

    state = {
        "t": times[-1],
        "close": _r(last_close),
        "raw": {name: _r(raw[name][now_idx]) for name in FEATURE_NAMES},
        "quantile": {name: _r(quant[name][now_idx], 4) for name in FEATURE_NAMES},
        "features": list(FEATURE_NAMES),
        "windows": dict(win),
    }

    # ---------------- 2. 相似状态回找 ----------------
    # 所有「未来收益可观测且不与当前窗口重叠」的历史窗口（防未来函数）
    last_valid = now_idx - hz - 1
    windows_all = []
    for i in range(n):
        if i > last_valid:
            continue
        vec = [quant[name][i] for name in FEATURE_NAMES]
        ratios = [closes[i + s] / closes[i] for s in range(1, hz + 1)]
        windows_all.append({
            "i": i, "t": times[i], "ratios": ratios, "ret": ratios[-1] - 1.0,
            "vec": None if any(v is None for v in vec) else vec,
        })
    # 其中「四项特征都有效」的可比较窗口（正常模式只能用这些）
    pool = [it for it in windows_all if it["vec"] is not None]

    matched, degraded, level, reason = [], False, 0, None
    if state_ok and len(pool) >= ms:
        for it in pool:
            it["dist"] = _distance(it["vec"], now_vec, w, weight_sum)
        pool.sort(key=lambda it: (it["dist"], it["i"]))
        matched = pool[:k_target]
        pool_size = len(pool)
    else:
        # 降级 1：不筛状态，改用历史整体（无条件）分布 —— 即全部可观测窗口
        degraded, level, pool_size = True, 1, len(windows_all)
        matched = list(windows_all)
        if not state_ok:
            reason = ("当前状态特征不完整（有效K线 %d 根 < 预热所需 %d 根），"
                      "已降级为历史整体分布（%d 个可观测窗口）"
                      % (n, need + 1, len(windows_all)))
        else:
            reason = ("满足相似状态筛选的历史窗口仅 %d 个（低于阈值 %d），已降级为"
                      "历史整体（无条件）分布，样本改用全部 %d 个可观测窗口"
                      % (len(pool), ms, len(windows_all)))

    if matched:
        rets = [it["ret"] for it in matched]
        rows = [it["ratios"] for it in matched]
    else:
        # 降级 2 / 3：连历史整体分布都取不到
        rets1 = _bar_returns(closes)
        degraded = True
        if rets1:
            level = 2
            rows = [[(1.0 + r) ** s for s in range(1, hz + 1)] for r in rets1]
            rets = [row[-1] - 1.0 for row in rows]
            reason = ("有效K线仅 %d 根，无法构造 %d 根前视收益，"
                      "已降级为单根收益按 %d 根复利外推" % (n, hz, hz))
        else:
            level, rows, rets = 3, [], []
            reason = "有效K线不足 2 根，无任何可计量的历史收益，已降级为零分布"

    # ---------------- 3. 收益分布 ----------------
    stats = _stats(rets)
    # 离散度基准 = 全部可观测窗口的无条件分布（条件分布越集中 → 置信度越高）
    base_rets = [it["ret"] for it in windows_all] or rets
    conf = _confidence(stats["sample"], stats["std"], _stats(base_rets)["std"], degraded)

    # ---------------- 4. 中位路径与预测带（价格口径） ----------------
    anchor = _r(last_close)
    median = [anchor]
    lo = [anchor]
    hi = [anchor]
    q25 = [anchor]
    q75 = [anchor]
    for step in range(hz):
        col = [row[step] for row in rows]
        if not col:
            for arr in (median, lo, hi, q25, q75):
                arr.append(anchor)
            continue
        median.append(_r(last_close * _quantile(col, 50)))
        lo.append(_r(last_close * _quantile(col, 5)))
        hi.append(_r(last_close * _quantile(col, 95)))
        q25.append(_r(last_close * _quantile(col, 25)))
        q75.append(_r(last_close * _quantile(col, 75)))

    path = {
        "t": _future_times(times, hz),
        "start": anchor,
        "median": median,
        "lo": lo,
        "hi": hi,
        "q25": q25,
        "q75": q75,
        "levels": _path_levels(),
        "horizon": hz,
    }

    return {
        "ok": True,
        "horizon": hz,
        "asOf": times[-1],
        "bars": n,
        "lastClose": anchor,
        "expected_return": stats["expected_return"],
        "median_return": stats["median_return"],
        "up_prob": stats["up_prob"],
        "quantiles": stats["quantiles"],
        "sample": stats["sample"],
        "confidence": conf,
        "degraded": bool(degraded),
        "degrade_reason": reason,
        "degrade_level": level,
        "path": path,
        "state": state,
        "match": {
            "distance": DISTANCE_DESC,
            "weights": {name: w[name] for name in FEATURE_NAMES},
            "k": len(matched),
            "pool": pool_size,
            "nearest": [
                {"i": it["i"], "t": it["t"],
                 "dist": _r(it.get("dist"), 4), "ret": _r(it["ret"])}
                for it in matched[:5]
            ],
        },
        "note": _note(degraded, reason, hz),
        "source": SOURCE,
    }
