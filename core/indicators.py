# -*- coding: utf-8 -*-
"""
AlphaDesk · 唯一指标实现（服务端）

设计约定：
  · 本模块是回测、策略跟踪、参数寻优共用的唯一指标实现，前端仅用于图表展示，
    不再参与任何信号计算，避免同一策略出现两份口径（详见对标调研报告 A1 项）。
  · 所有函数对缺失值返回 None，不外抛异常；输入序列按「旧 → 新」排列。
"""

from __future__ import annotations


def ok(v):
    """是否为有效数值"""
    return isinstance(v, (int, float)) and v == v


def SMA(values, n):
    """简单移动平均；窗口内必须全为有效值，否则该点返回 None"""
    n = int(n)
    if n <= 0:
        return [None] * len(values)
    out = [None] * len(values)
    s, cnt = 0.0, 0
    for i, v in enumerate(values):
        if ok(v):
            s += v
            cnt += 1
        if i >= n:
            old = values[i - n]
            if ok(old):
                s -= old
                cnt -= 1
        if i >= n - 1 and cnt == n:
            out[i] = s / n
    return out


def EMA(values, n):
    """指数移动平均；首值作为起点递推"""
    n = int(n)
    out = [None] * len(values)
    if n <= 0:
        return out
    k = 2.0 / (n + 1)
    prev = None
    for i, v in enumerate(values):
        if not ok(v):
            continue
        prev = v if prev is None else v * k + prev * (1 - k)
        out[i] = prev
    return out


def MACD(closes, fast=12, slow=26, signal=9):
    """MACD：返回 dif / dea / macd(柱) 三条序列"""
    ef, es = EMA(closes, fast), EMA(closes, slow)
    dif = [None if not (ok(ef[i]) and ok(es[i])) else ef[i] - es[i] for i in range(len(closes))]
    valid = [v for v in dif if ok(v)]
    dea_valid = EMA(valid, signal)
    dea = [None] * len(closes)
    k = 0
    for i, v in enumerate(dif):
        if ok(v):
            dea[i] = dea_valid[k] if k < len(dea_valid) else None
            k += 1
    macd = [None if not (ok(dif[i]) and ok(dea[i])) else (dif[i] - dea[i]) * 2 for i in range(len(closes))]
    return {"dif": dif, "dea": dea, "macd": macd}


def RSI(closes, n=14):
    """相对强弱指标（Wilder 平滑）"""
    n = int(n)
    out = [None] * len(closes)
    if n <= 0:
        return out
    ag = al = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain = ch if ch > 0 else 0.0
        loss = -ch if ch < 0 else 0.0
        if i <= n:
            ag += gain / n
            al += loss / n
            if i == n:
                out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            ag = (ag * (n - 1) + gain) / n
            al = (al * (n - 1) + loss) / n
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def BOLL(closes, n=20, k=2.0):
    """布林带：返回中轨与上下轨"""
    n = int(n)
    mid = SMA(closes, n)
    up = [None] * len(closes)
    low = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        win = [v for v in closes[i - n + 1:i + 1] if ok(v)]
        if len(win) < n or not ok(mid[i]):
            continue
        m = mid[i]
        var = sum((v - m) ** 2 for v in win) / n
        sd = var ** 0.5
        up[i] = m + k * sd
        low[i] = m - k * sd
    return {"mid": mid, "up": up, "low": low}


def KDJ(bars, n=9, m1=3, m2=3):
    """随机指标 KDJ（bars 需含 high / low / close）"""
    n = int(n)
    rsv = [None] * len(bars)
    for i in range(len(bars)):
        if i < n - 1:
            continue
        win = bars[i - n + 1:i + 1]
        hi = max(b["high"] for b in win)
        lo = min(b["low"] for b in win)
        c = bars[i]["close"]
        rsv[i] = 50.0 if hi == lo else (c - lo) / (hi - lo) * 100
    K = [None] * len(bars)
    D = [None] * len(bars)
    J = [None] * len(bars)
    pk = pd = 50.0
    for i in range(len(bars)):
        if not ok(rsv[i]):
            continue
        pk = (2 / 3) * pk + (1 / 3) * rsv[i]
        pd = (2 / 3) * pd + (1 / 3) * pk
        K[i], D[i], J[i] = pk, pd, 3 * pk - 2 * pd
    return {"K": K, "D": D, "J": J}


def ATR(bars, n=14):
    """平均真实波幅"""
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            tr.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return EMA(tr, n)


def OBV(bars):
    """能量潮：收涨累加成交量，收跌累减"""
    out = [0.0] * len(bars)
    acc = 0.0
    for i in range(1, len(bars)):
        d = bars[i]["close"] - bars[i - 1]["close"]
        v = bars[i].get("volume") or 0
        acc += v if d > 0 else (-v if d < 0 else 0)
        out[i] = acc
    return out


def cross_up(a, b, i):
    """序列 a 在 i 处上穿 b"""
    return (i > 0 and ok(a[i]) and ok(b[i]) and ok(a[i - 1]) and ok(b[i - 1])
            and a[i - 1] <= b[i - 1] and a[i] > b[i])


def cross_down(a, b, i):
    """序列 a 在 i 处下穿 b"""
    return (i > 0 and ok(a[i]) and ok(b[i]) and ok(a[i - 1]) and ok(b[i - 1])
            and a[i - 1] >= b[i - 1] and a[i] < b[i])


__all__ = ["ok", "SMA", "EMA", "MACD", "RSI", "BOLL", "KDJ", "ATR", "OBV",
           "cross_up", "cross_down"]
