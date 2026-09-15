# -*- coding: utf-8 -*-
"""
AlphaDesk · 策略注册表（唯一策略实现）

设计约定：
  · 每个策略声明参数空间（key / label / def / min / max / step），供创建任务、
    详情调整、以及网格寻优共用，避免参数范围在多处硬编码。
  · 策略只产出信号（buy / sell / None），不接触账户、资金与撮合，
    账户与成交由 core/portfolio.py、core/fills.py 负责（对标调研 A2 项）。
"""

from __future__ import annotations

from . import indicators as I


def _clamp(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = float(default)
    if f != f:
        f = float(default)
    return max(float(lo), min(float(hi), f))


# --------------------------------------------------------------------------- #
# 策略实现
# --------------------------------------------------------------------------- #

def _ma_cross(bars, p, i, cache):
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    fast = cache.setdefault("ma_fast", I.SMA(closes, int(p["fast"])))
    slow = cache.setdefault("ma_slow", I.SMA(closes, int(p["slow"])))
    if I.cross_up(fast, slow, i):
        return "buy"
    if I.cross_down(fast, slow, i):
        return "sell"
    return None


def _macd(bars, p, i, cache):
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    m = cache.setdefault("macd", I.MACD(closes, int(p["fast"]), int(p["slow"]), int(p["signal"])))
    if I.cross_up(m["dif"], m["dea"], i):
        return "buy"
    if I.cross_down(m["dif"], m["dea"], i):
        return "sell"
    return None


def _rsi(bars, p, i, cache):
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    r = cache.setdefault("rsi", I.RSI(closes, int(p["n"])))
    if not (I.ok(r[i]) and I.ok(r[i - 1])):
        return None
    if r[i - 1] < p["low"] and r[i] >= p["low"]:
        return "buy"
    if r[i - 1] > p["high"] and r[i] <= p["high"]:
        return "sell"
    return None


def _breakout(bars, p, i, cache):
    n, m = int(p["n"]), int(p["m"])
    if i < max(n, m) + 1:
        return None
    prev_hi = max(b["high"] for b in bars[i - n:i])
    prev_lo = min(b["low"] for b in bars[i - m:i])
    if bars[i]["close"] > prev_hi:
        return "buy"
    if bars[i]["close"] < prev_lo:
        return "sell"
    return None


def _momentum(bars, p, i, cache):
    n = int(p["n"])
    if i < n:
        return None
    mom = (bars[i]["close"] / bars[i - n]["close"] - 1) * 100
    prev_mom = (bars[i - 1]["close"] / bars[i - 1 - n]["close"] - 1) * 100
    if prev_mom <= p["th"] and mom > p["th"]:
        return "buy"
    if prev_mom >= 0 and mom < 0:
        return "sell"
    return None


def _boll_revert(bars, p, i, cache):
    """布林带均值回归：跌破下轨买入，触及上轨卖出"""
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    b = cache.setdefault("boll", I.BOLL(closes, int(p["n"]), float(p["k"])))
    if not (I.ok(b["low"][i]) and I.ok(b["up"][i])):
        return None
    if closes[i] < b["low"][i]:
        return "buy"
    if closes[i] > b["up"][i]:
        return "sell"
    return None


def _kdj_cross(bars, p, i, cache):
    """KDJ 低位金叉买入、高位死叉卖出"""
    k = cache.setdefault("kdj", I.KDJ(bars, int(p["n"]), 3, 3))
    K, D = k["K"], k["D"]
    if not (I.ok(K[i]) and I.ok(D[i]) and I.ok(K[i - 1]) and I.ok(D[i - 1])):
        return None
    if I.cross_up(K, D, i) and K[i] < p["low"]:
        return "buy"
    if I.cross_down(K, D, i) and K[i] > p["high"]:
        return "sell"
    return None


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

STRATEGIES = {
    "maCross": {
        "name": "双均线交叉",
        "desc": "快线上穿慢线买入，下穿卖出",
        "params": [
            {"key": "fast", "label": "快线周期", "def": 5, "min": 2, "max": 120, "step": 1},
            {"key": "slow", "label": "慢线周期", "def": 20, "min": 3, "max": 250, "step": 1},
        ],
        "fn": _ma_cross,
    },
    "macd": {
        "name": "MACD 金叉死叉",
        "desc": "DIF 上穿 DEA 买入，下穿卖出",
        "params": [
            {"key": "fast", "label": "快线 EMA", "def": 12, "min": 3, "max": 60, "step": 1},
            {"key": "slow", "label": "慢线 EMA", "def": 26, "min": 5, "max": 120, "step": 1},
            {"key": "signal", "label": "信号线", "def": 9, "min": 2, "max": 40, "step": 1},
        ],
        "fn": _macd,
    },
    "rsi": {
        "name": "RSI 超卖反转",
        "desc": "RSI 低于下限买入，高于上限卖出",
        "params": [
            {"key": "n", "label": "RSI 周期", "def": 14, "min": 3, "max": 40, "step": 1},
            {"key": "low", "label": "买入阈值", "def": 30, "min": 5, "max": 50, "step": 1},
            {"key": "high", "label": "卖出阈值", "def": 70, "min": 50, "max": 95, "step": 1},
        ],
        "fn": _rsi,
    },
    "breakout": {
        "name": "N 日通道突破",
        "desc": "突破前 N 日高点买入，跌破 M 日低点卖出",
        "params": [
            {"key": "n", "label": "突破周期", "def": 20, "min": 3, "max": 120, "step": 1},
            {"key": "m", "label": "止损周期", "def": 10, "min": 2, "max": 120, "step": 1},
        ],
        "fn": _breakout,
    },
    "momentum": {
        "name": "动量轮动",
        "desc": "近 N 日涨幅超过阈值买入，动量转负卖出",
        "params": [
            {"key": "n", "label": "动量周期", "def": 20, "min": 3, "max": 120, "step": 1},
            {"key": "th", "label": "入场阈值%", "def": 5, "min": 0.5, "max": 50, "step": 0.5},
        ],
        "fn": _momentum,
    },
    "bollRevert": {
        "name": "布林带均值回归",
        "desc": "跌破下轨买入，触及上轨卖出",
        "params": [
            {"key": "n", "label": "周期", "def": 20, "min": 5, "max": 120, "step": 1},
            {"key": "k", "label": "带宽倍数", "def": 2, "min": 1, "max": 4, "step": 0.1},
        ],
        "fn": _boll_revert,
    },
    "kdjCross": {
        "name": "KDJ 金叉死叉",
        "desc": "低位金叉买入，高位死叉卖出",
        "params": [
            {"key": "n", "label": "KDJ 周期", "def": 9, "min": 3, "max": 60, "step": 1},
            {"key": "low", "label": "低位阈值", "def": 40, "min": 5, "max": 60, "step": 1},
            {"key": "high", "label": "高位阈值", "def": 70, "min": 40, "max": 95, "step": 1},
        ],
        "fn": _kdj_cross,
    },
}


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #

def keys():
    return list(STRATEGIES.keys())


def meta():
    """供前端渲染：策略名、说明与参数空间"""
    return {k: {"name": v["name"], "desc": v["desc"], "params": v["params"]}
            for k, v in STRATEGIES.items()}


def default_params(key):
    st = STRATEGIES.get(key)
    if not st:
        return {}
    return {p["key"]: float(p["def"]) for p in st["params"]}


def normalize_params(key, raw):
    """按策略参数空间裁剪并补默认值，未知键丢弃"""
    st = STRATEGIES.get(key)
    if not st:
        return {}
    raw = raw or {}
    out = {}
    for p in st["params"]:
        out[p["key"]] = _clamp(raw.get(p["key"], p["def"]), p["min"], p["max"], p["def"])
    return out


def warmup_bars(key, params):
    """策略所需的最小预热K线数量，用于跳过信号尚不可用的区间"""
    st = STRATEGIES.get(key)
    if not st:
        return 30
    biggest = 20
    for p in st["params"]:
        try:
            v = float((params or {}).get(p["key"], p["def"]))
        except (TypeError, ValueError):
            v = float(p["def"])
        if p["key"] in ("fast", "n", "signal"):
            biggest = max(biggest, v * 2)
        elif p["key"] == "slow":
            biggest = max(biggest, v)
        elif p["key"] == "m":
            biggest = max(biggest, v)
    return int(max(30, biggest + 5))


def signal(key, bars, params, i, cache=None):
    """在索引 i 处求信号；cache 为同一轮回放复用的指标缓存"""
    st = STRATEGIES.get(key)
    if not st or i < 1 or i >= len(bars):
        return None
    try:
        return st["fn"](bars, params or {}, i, cache if cache is not None else {})
    except Exception:  # noqa: BLE001  单个策略计算异常不应中断回放
        return None


def describe(key, params=None):
    """人类可读的策略描述，用于日志与通知"""
    st = STRATEGIES.get(key)
    if not st:
        return str(key)
    p = params or default_params(key)
    seq = " ".join("%s=%s" % (k, _fmt(p[k])) for k in p)
    return "%s（%s）" % (st["name"], seq)


def _fmt(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else ("%.2f" % f).rstrip("0").rstrip(".")


__all__ = ["STRATEGIES", "keys", "meta", "default_params", "normalize_params",
           "warmup_bars", "signal", "describe"]
