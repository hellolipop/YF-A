#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AlphaDesk · A股 / 美股 实时分析终端 —— 后端数据服务

零第三方依赖（仅 Python 标准库）。

数据源分层（自动降级，任一源异常不影响系统可用）：
  · 腾讯行情（qt.gtimg.cn / ifzq.gtimg.cn）  —— 实时报价、五档盘口、K线、分时（A股 + 美股）
  · 东方财富（push2.eastmoney.com）          —— 全市场快照、板块资金、资讯、搜索、备用K线
  · 新浪财经（hq.sinajs.cn）                 —— A股行情兜底

对外统一 REST 接口，服务端完成聚合 / 缓存 / 降级，前端无需处理跨域。

启动： python3 server.py --port 8848
免责声明：数据来自公开行情接口，仅供研究学习，不构成投资建议。
"""

import argparse
import gzip
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import datetime

from core import advisor as core_advisor
from core import fills as core_fills
from core import logs as core_logs
from core import metrics as core_metrics
from core import notify as core_notify
from core import runner as core_runner
from core import storage as core_storage
from core import strategies as core_strategies
from core import stream as core_stream
from core import symbols as core_symbols
from core import trader as core_trader
from providers import features as feat_provider

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
_BOOT_TS = time.time()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 12
RETRY = 1

_SSL_CTX_UNVERIFIED = ssl._create_unverified_context()  # noqa: SLF001
_SSL_CTX = None
_SSL_READY = False


def ensure_ssl():
    """探测一次证书链；macOS python.org 版本常缺根证书，兜底为非校验上下文"""
    global _SSL_CTX, _SSL_READY
    if _SSL_READY:
        return
    _SSL_READY = True
    try:
        probe = urllib.request.Request(
            "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001&fields=f2,f12",
            headers={"User-Agent": UA})
        urllib.request.urlopen(probe, timeout=6, context=ssl.create_default_context()).read()
        _SSL_CTX = ssl.create_default_context()
    except Exception:  # noqa: BLE001
        _SSL_CTX = _SSL_CTX_UNVERIFIED


# --------------------------------------------------------------------------- #
# 抓取与缓存
# --------------------------------------------------------------------------- #

_CACHE = {}
_FLIGHT = {}
_CACHE_LOCK = threading.Lock()


def cache_get(key, ttl):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    if not item:
        return None
    ts, val = item
    return None if time.time() - ts > ttl else (val, time.time() - ts)


def cache_peek(key):
    """忽略 TTL 读取（用于降级时返回最近一次成功的数据）"""
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    return (item[1], time.time() - item[0]) if item else (None, None)


def cache_put(key, val):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), val)


def cached(key, ttl, producer, allow_stale=True):
    hit = cache_get(key, ttl)
    if hit:
        val, age = hit
        if isinstance(val, dict):
            val = dict(val)
            val["stale"] = False
        return val
    lock = _FLIGHT.setdefault(key, threading.Lock())
    with lock:
        hit = cache_get(key, ttl)
        if hit:
            val, age = hit
            if isinstance(val, dict):
                val = dict(val)
                val["stale"] = False
            return val
        try:
            val = producer()
            cache_put(key, val)
            out = dict(val) if isinstance(val, dict) else val
            if isinstance(out, dict):
                out["stale"] = False
            return out
        except Exception:
            if allow_stale:
                val, age = cache_peek(key)
                if val is not None:
                    out = dict(val) if isinstance(val, dict) else val
                    if isinstance(out, dict):
                        out["stale"] = True
                        out["staleAge"] = int(age) if age else None
                    return out
            raise


def http_get(url, referer="https://quote.eastmoney.com/", encoding="utf-8", raw=False, retry=RETRY):
    ensure_ssl()
    last_err = None
    for _ in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": referer, "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate", "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            return data.decode(encoding, errors="ignore") if raw else json.loads(
                data.decode("utf-8", errors="ignore"))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.2)
    raise RuntimeError("上游请求失败: %s (%s)" % (url[:120], last_err))


def num(v, default=None):
    if v is None or v == "-" or v == "":
        return default
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 腾讯行情
# --------------------------------------------------------------------------- #

CN_INDEX_SYMBOLS = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("sh000300", "沪深300"), ("sh000688", "科创50"), ("sz399905", "中证500"),
    ("sh000016", "上证50"), ("sz399852", "中证1000"),
]
US_INDEX_SYMBOLS = [
    ("usDJI", "道琼斯"), ("usIXIC", "纳斯达克"), ("usINX", "标普500"), ("hkHSI", "恒生指数"),
]


KNOWN_TX_INDEX = {
    "DJI": "usDJI", "DJIA": "usDJI", "IXIC": "usIXIC", "NDX": "usIXIC",
    "INX": "usINX", "SPX": "usINX", "HSI": "hkHSI",
}


def tx_symbols(market, codes):
    """腾讯行情代码：A股 sh/sz/bj 前缀；美股 us 前缀；指数使用固定别名"""
    out = []
    for c in codes:
        s = str(c).strip()
        if not s:
            continue
        u = s.upper()
        if re.match(r"^(SH|SZ|BJ)\d{6}$", u):
            out.append(s.lower())
            continue
        m = re.match(r"^(US|HK)([A-Z0-9.]+)$", u)
        if m and m.group(2) in ("DJI", "IXIC", "INX", "NDX", "SPX", "HSI"):
            out.append(m.group(1).lower() + m.group(2))
            continue
        if u in KNOWN_TX_INDEX:
            out.append(KNOWN_TX_INDEX[u])
            continue
        if market == "us":
            out.append("us" + u.replace(".", ""))
        elif re.match(r"^\d{6}$", s):
            out.append(("sh" if s[0] in "6957" else "sz") + s)
        else:
            out.append(s.lower())
    return out


def tx_raw(symbols):
    """批量抓取腾讯行情原文，返回 {symbol: [fields]}"""
    result = {}
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        txt = http_get("https://qt.gtimg.cn/q=" + ",".join(chunk),
                       referer="https://gu.qq.com/", encoding="gbk", raw=True)
        for line in txt.split(";"):
            if '="' not in line:
                continue
            sym = line.split("=")[0].replace("v_", "").strip()
            body = line.split('"')[1]
            if not body:
                continue
            result[sym] = body.split("~")
    return result


def tx_parse(sym, f, market):
    """腾讯行情字段 -> 统一结构（A股 88 字段 / 美股 73 字段）"""
    is_us = market == "us" or sym.startswith("us")
    code = f[2] if len(f) > 2 else sym
    price = num(f[3]) if len(f) > 3 else None
    prev = num(f[4]) if len(f) > 4 else None
    high = num(f[33]) if len(f) > 33 else None
    low = num(f[34]) if len(f) > 34 else None
    volume = num(f[36]) if len(f) > 36 else (num(f[6]) if len(f) > 6 else None)
    amount_raw = num(f[37]) if len(f) > 37 else None
    turnover = num(f[38]) if len(f) > 38 else None
    pe = num(f[39]) if len(f) > 39 else None
    amplitude = num(f[43]) if len(f) > 43 else None
    float_cap = num(f[44]) if len(f) > 44 else None
    total_cap = num(f[45]) if len(f) > 45 else None
    pb = num(f[46]) if len(f) > 46 else None
    limit_up = num(f[47]) if len(f) > 47 else None
    limit_down = num(f[48]) if len(f) > 48 else None
    volume_ratio = num(f[49]) if len(f) > 49 else None
    avg_price = num(f[51]) if len(f) > 51 else None
    pe_dyn = num(f[52]) if len(f) > 52 else None
    outer = num(f[7]) if len(f) > 7 else None
    inner = num(f[8]) if len(f) > 8 else None

    if is_us:
        amount = amount_raw  # 美元
        market_cap = (total_cap or 0) * 1e8 or None
        float_cap = (float_cap or 0) * 1e8 or None
        pe_ttm = pe
        name = f[1] if len(f) > 1 else code
        week52_high = num(f[48]) if len(f) > 48 else None
        week52_low = num(f[49]) if len(f) > 49 else None
        currency = "USD"
        bids = []
        asks = []
        if num(f[9]) and num(f[9]) > 0:
            bids = [{"price": num(f[9]), "volume": num(f[10])}]
        if num(f[19]) and num(f[19]) > 0:
            asks = [{"price": num(f[19]), "volume": num(f[20])}]
    else:
        amount = (amount_raw or 0) * 1e4 if amount_raw else None  # 万元 -> 元
        market_cap = (total_cap or 0) * 1e8 or None  # 亿元 -> 元
        float_cap = (float_cap or 0) * 1e8 or None
        pe_ttm = pe
        name = f[1] if len(f) > 1 else code
        week52_high = None
        week52_low = None
        currency = "CNY"
        bids, asks = [], []
        try:
            for i in range(5):
                bp, bv = num(f[9 + i * 2]), num(f[10 + i * 2])
                ap, av = num(f[19 + i * 2]), num(f[20 + i * 2])
                if bp:
                    bids.append({"price": bp, "volume": bv})
                if ap:
                    asks.append({"price": ap, "volume": av})
        except IndexError:
            pass

    change = num(f[31]) if len(f) > 31 else (price - prev if price and prev else None)
    change_pct = num(f[32]) if len(f) > 32 else (
        (price / prev - 1) * 100 if price and prev else None)
    ts_field = f[30] if len(f) > 30 else ""
    ts_field = ts_field.strip()
    if is_us:
        updated = ts_field
    else:
        updated = ("%s-%s-%s %s:%s:%s" % (ts_field[0:4], ts_field[4:6], ts_field[6:8],
                                          ts_field[8:10], ts_field[10:12], ts_field[12:14])
                   if len(ts_field) >= 14 else ts_field)
    return {
        "code": code.split(".")[0] if is_us else code,
        "symbol": code,
        "name": name,
        "market": "us" if is_us else "cn",
        "price": price, "prevClose": prev, "open": num(f[5]) if len(f) > 5 else None,
        "high": high, "low": low, "change": change, "changePct": change_pct,
        "volume": volume, "amount": amount, "turnover": turnover,
        "pe": pe_dyn, "peTtm": pe_ttm, "pb": pb,
        "marketCap": market_cap, "floatCap": float_cap,
        "amplitude": amplitude, "volumeRatio": volume_ratio,
        "limitUp": limit_up, "limitDown": limit_down, "avgPrice": avg_price,
        "outer": outer, "inner": inner,
        "week52High": week52_high, "week52Low": week52_low,
        "currency": currency, "updated": updated,
        "bids": bids, "asks": asks,
        "source": "腾讯行情",
    }


def quotes(market, codes):
    """批量实时报价（腾讯为主，A股缺失时用新浪兜底）"""
    symbols = tx_symbols(market, codes)
    raw = {}
    err = None
    try:
        raw = tx_raw(symbols)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    rows = []
    missing = []
    for sym, c in zip(symbols, codes):
        f = raw.get(sym)
        if f and len(f) > 5:
            rows.append(tx_parse(sym, f, market))
        else:
            missing.append(c)
    if missing and market == "cn":
        for r in sina_quotes(missing):
            rows.append(r)
    if not rows and err:
        raise RuntimeError(err)
    return rows


def sina_quotes(codes):
    """新浪财经兜底（A股）"""
    try:
        syms = [(("sh" if c[0] in "69" else "sz") + c) for c in codes]
        txt = http_get("https://hq.sinajs.cn/list=" + ",".join(syms),
                       referer="https://finance.sina.com.cn/", encoding="gbk", raw=True)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in txt.split(";"):
        if '="' not in line:
            continue
        body = line.split('"')[1]
        p = body.split(",")
        if len(p) < 32:
            continue
        price = num(p[3])
        prev = num(p[2])
        out.append({
            "code": p[0][2:], "symbol": p[0], "name": p[0][2:], "market": "cn",
            "price": price, "prevClose": prev, "open": num(p[1]), "high": num(p[4]),
            "low": num(p[5]), "change": (price - prev) if price and prev else None,
            "changePct": ((price / prev - 1) * 100) if price and prev else None,
            "volume": (num(p[8]) or 0) / 100, "amount": num(p[9]), "turnover": None,
            "pe": None, "peTtm": None, "pb": None, "marketCap": None, "floatCap": None,
            "amplitude": None, "volumeRatio": None, "limitUp": None, "limitDown": None,
            "avgPrice": None, "outer": None, "inner": None,
            "currency": "CNY", "updated": "%s %s" % (p[30], p[31]),
            "bids": [{"price": num(p[11]), "volume": (num(p[10]) or 0) / 100}],
            "asks": [{"price": num(p[21]), "volume": (num(p[20]) or 0) / 100}],
            "source": "新浪财经",
        })
    return out


def indices(market):
    spec = CN_INDEX_SYMBOLS if market == "cn" else US_INDEX_SYMBOLS

    def build():
        syms = [s for s, _ in spec]
        raw = tx_raw(syms)
        rows = []
        for sym, label in spec:
            f = raw.get(sym)
            if not f or len(f) < 35:
                continue
            is_us = sym.startswith("us") or sym.startswith("hk")
            item = tx_parse(sym, f, "us" if is_us else "cn")
            item["name"] = label
            item["indexSymbol"] = sym
            rows.append(item)
        return {"market": market, "rows": rows, "source": "腾讯行情", "updated": now_ms()}

    return cached("idx_%s" % market, 6, build)


# --------------------------------------------------------------------------- #
# K 线 / 分时
# --------------------------------------------------------------------------- #

PERIOD_MAP_TX = {"day": "day", "week": "week", "month": "month",
                 "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60", "1m": "m1"}


def tx_kline(market, code, period, fq, limit):
    sym = tx_symbols(market, [code])[0]
    if period in ("day", "week", "month"):
        fq_tag = {1: "qfq", 2: "hfq"}.get(fq, "")
        ep = "usfqkline" if market == "us" else "fqkline"
        cand = [sym]
        if market == "us":
            full = tx_full_symbol(code)
            if full and full != sym:
                cand.insert(0, full)
        rows = None
        used = sym
        for s in cand:
            try:
                url = "https://web.ifzq.gtimg.cn/appstock/app/%s/get?param=%s,%s,,,%d,%s" % (
                    ep, s, PERIOD_MAP_TX[period], limit, fq_tag)
                res = http_get(url, referer="https://gu.qq.com/")
                node = ((res or {}).get("data") or {}).get(s) or {}
                for key in (("%s%s" % (fq_tag, PERIOD_MAP_TX[period])), PERIOD_MAP_TX[period]):
                    got = node.get(key)
                    if isinstance(got, list) and len(got) >= 3:
                        rows, used = got, s
                        break
            except Exception:  # noqa: BLE001
                continue
            if rows:
                break
        if not rows:
            raise RuntimeError("腾讯K线为空（%s）" % code)
        bars = []
        for r in rows:
            bars.append({
                "t": r[0], "open": num(r[1]), "close": num(r[2]),
                "high": num(r[3]), "low": num(r[4]), "volume": num(r[5]),
                "amount": None,
            })
        return bars, "腾讯行情"
    if market == "us":
        raise RuntimeError("腾讯无美股分钟K线，改用其他数据源")
    # 分钟级（A股）
    url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=%s,%s,,%d" % (
        sym, PERIOD_MAP_TX[period], limit)
    res = http_get(url, referer="https://gu.qq.com/")
    node = ((res or {}).get("data") or {}).get(sym) or {}
    rows = node.get(PERIOD_MAP_TX[period])
    if not rows:
        raise RuntimeError("腾讯分钟K线为空")
    bars = []
    for r in rows:
        t = str(r[0])
        stamp = ("%s-%s-%s %s:%s" % (t[0:4], t[4:6], t[6:8], t[8:10], t[10:12])
                 if len(t) >= 12 else t)
        bars.append({"t": stamp, "open": num(r[1]), "close": num(r[2]),
                     "high": num(r[3]), "low": num(r[4]), "volume": num(r[5]), "amount": None})
    return bars, "腾讯行情"


SYM_MEMO = {}


def tx_full_symbol(code):
    """美股需要交易所后缀（如 AAPL.OQ）才能拿到腾讯完整K线"""
    key = code.upper()
    if key in SYM_MEMO:
        return SYM_MEMO[key]
    full = None
    try:
        raw = tx_raw(["us" + key])
        f = raw.get("us" + key)
        if f and len(f) > 2 and "." in f[2]:
            full = "us" + f[2]
    except Exception:  # noqa: BLE001
        full = None
    SYM_MEMO[key] = full
    return full



def em_kline(market, code, period, fq, limit):
    klt = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
           "day": 101, "week": 102, "month": 103}.get(period, 101)
    secid = em_secid(market, code)
    params = {
        "secid": secid, "klt": klt, "fqt": fq, "beg": 0, "end": 20500101,
        "lmt": max(60, min(int(limit), 2000)),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    res = em("/api/qt/stock/kline/get", params)
    data = (res or {}).get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        raise RuntimeError("东财K线为空（可能被限流）")
    bars = []
    for line in klines:
        p = line.split(",")
        if len(p) < 6:
            continue
        bars.append({"t": p[0], "open": num(p[1]), "close": num(p[2]), "high": num(p[3]),
                     "low": num(p[4]), "volume": num(p[5]), "amount": num(p[6]),
                     "amplitude": num(p[7]) if len(p) > 7 else None,
                     "changePct": num(p[8]) if len(p) > 8 else None,
                     "change": num(p[9]) if len(p) > 9 else None,
                     "turnover": num(p[10]) if len(p) > 10 else None})
    return bars, "东方财富"


def aggregate_bars(bars, minutes):
    """把 1 分钟序列聚合成 N 分钟K线"""
    out, bucket = [], None
    for b in bars:
        t = b["t"]
        try:
            mm = int(t[14:16])
        except (ValueError, IndexError):
            mm = 0
        key = t[:14] + "%02d" % (mm // minutes * minutes)
        if bucket is None or bucket["t"] != key:
            bucket = {"t": key, "open": b["open"], "close": b["close"],
                      "high": b["high"], "low": b["low"], "volume": b["volume"] or 0,
                      "amount": b["amount"] or 0}
            out.append(bucket)
        else:
            bucket["close"] = b["close"]
            bucket["high"] = max(bucket["high"], b["high"])
            bucket["low"] = min(bucket["low"], b["low"])
            bucket["volume"] += b["volume"] or 0
            bucket["amount"] += b["amount"] or 0
    return out


def api_kline(market, code, period, fq, limit):
    def build():
        attempts = []
        errors = []
        if period in ("day", "week", "month", "5m", "15m", "30m", "60m"):
            attempts.append(lambda: tx_kline(market, code, period, fq, limit))
        attempts.append(lambda: em_kline(market, code, period, fq, limit))
        for fn in attempts:
            try:
                bars, src = fn()
                if bars:
                    return {"code": code, "market": market, "period": period, "fq": fq,
                            "bars": bars, "source": src, "updated": now_ms()}
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:120])
        # 美股分钟线兜底：用分时数据聚合
        if market == "us" and period in ("5m", "15m", "30m", "60m", "1m"):
            try:
                tr = api_trends(market, code)
                pts = tr.get("points") or []
                if pts:
                    bars = [{"t": p["t"], "open": p["price"], "close": p["price"],
                             "high": p["price"], "low": p["price"],
                             "volume": p.get("volume"), "amount": p.get("amount")} for p in pts]
                    n = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}[period]
                    return {"code": code, "market": market, "period": period, "fq": fq,
                            "bars": aggregate_bars(bars, n),
                            "source": "由分时数据聚合（估算）", "updated": now_ms()}
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:120])
        return {"code": code, "market": market, "period": period, "fq": fq, "bars": [],
                "source": None, "error": "；".join(errors) or "无数据", "updated": now_ms()}

    return cached("kline_v3_%s_%s_%s_%s_%s" % (market, code, period, fq, limit), 20, build)


def api_trends(market, code, days=1):
    """分时：东财 trends2 优先（含均价/成交额），腾讯 minute 兜底"""
    def build():
        errors = []
        try:
            secid = em_secid(market, code)
            res = em("/api/qt/stock/trends2/get", {
                "secid": secid, "ndays": days, "iscr": 0,
                "fields1": "f1,f2,f3,f7", "fields2": "f51,f53,f56,f57,f58",
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            })
            data = (res or {}).get("data") or {}
            pts = []
            for line in data.get("trends") or []:
                p = line.split(",")
                if len(p) < 5:
                    continue
                pts.append({"t": p[0], "price": num(p[1]), "volume": num(p[2]),
                            "amount": num(p[3]), "avg": num(p[4])})
            if pts:
                prev = num(data.get("preSettlement"))
                if not prev:
                    prev = trend_prev_close(market, code)
                return {"code": code, "market": market, "prevClose": prev, "points": pts,
                        "days": days, "source": "东方财富（含均价/成交额）", "updated": now_ms()}
            errors.append("东财分时为空")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:100])

        # 腾讯分时兜底
        try:
            sym = tx_symbols(market, [code])[0]
            ep = "usMinute" if market == "us" else "minute"
            res = http_get("https://web.ifzq.gtimg.cn/appstock/app/%s/query?code=%s" % (ep, sym),
                           referer="https://gu.qq.com/")
            node = ((res or {}).get("data") or {}).get(sym) or {}
            lines = ((node.get("data") or {}).get("data")) or []
            qtf = (node.get("qt") or {}).get(sym)
            if not (isinstance(qtf, list) and len(qtf) > 5):
                qtf = None
            prev = num(qtf[4]) if qtf else trend_prev_close(market, code)
            pts = []
            cum_vol = 0.0
            cum_amt = 0.0
            unit = 100 if market == "cn" else 1
            for line in lines:
                p = line.split()
                if len(p) < 3:
                    continue
                hhmm = p[0]
                t = "%s %s:%s" % (date_hint(), hhmm[0:2], hhmm[2:4])
                price = num(p[1])
                vol = num(p[2]) or 0
                amt = num(p[3]) if len(p) > 3 else None
                d_vol = max(0.0, vol - cum_vol)
                cum_vol = vol
                if amt is not None:
                    d_amt = max(0.0, amt - cum_amt)
                    cum_amt = amt
                else:
                    d_amt = d_vol * unit * (price or 0)
                pts.append({"t": t, "price": price, "volume": d_vol, "amount": d_amt,
                            "avg": (cum_amt / (cum_vol * unit) if cum_amt and cum_vol else None)})
            if pts:
                return {"code": code, "market": market, "prevClose": prev, "points": pts,
                        "days": 1, "source": "腾讯行情（均价为估算）", "updated": now_ms(),
                        "partial": len(lines) < 5}
            errors.append("腾讯分时为0点")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:100])

        return {"code": code, "market": market, "prevClose": None, "points": [],
                "source": None, "error": "；".join(errors), "updated": now_ms()}

    return cached("trends_v3_%s_%s_%d" % (market, code, days), 12, build)


def date_hint():
    return time.strftime("%Y-%m-%d")


def trend_prev_close(market, code):
    try:
        rows = quotes(market, [code])
        return rows[0].get("prevClose") if rows else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 东方财富：全市场列表 / 板块 / 资讯 / 搜索
# --------------------------------------------------------------------------- #

def em(path, params):
    last = None
    for host in ("https://push2.eastmoney.com", "https://push2his.eastmoney.com"):
        try:
            return http_get(host + path + "?" + urllib.parse.urlencode(params))
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError("东财接口不可用: %s" % str(last)[:120])


CN_LIST_FIELDS = ",".join([
    "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f12", "f13", "f14",
    "f15", "f16", "f17", "f18", "f20", "f21", "f22", "f23", "f24", "f25", "f62", "f115",
])
US_LIST_FIELDS = CN_LIST_FIELDS
CN_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
US_FS = "m:105,m:106,m:107"


def norm_row(row, market):
    code = str(row.get("f12") or "").strip()
    mkt = int(num(row.get("f13"), 1) or 1)
    return {
        "code": code, "name": str(row.get("f14") or "").strip(), "market": market,
        "mktNum": mkt, "secid": "%d.%s" % (mkt, code),
        "price": num(row.get("f2")), "changePct": num(row.get("f3")),
        "change": num(row.get("f4")), "volume": num(row.get("f5")),
        "amount": num(row.get("f6")), "amplitude": num(row.get("f7")),
        "turnover": num(row.get("f8")), "pe": num(row.get("f9")),
        "volumeRatio": num(row.get("f10")), "high": num(row.get("f15")),
        "low": num(row.get("f16")), "open": num(row.get("f17")),
        "prevClose": num(row.get("f18")), "marketCap": num(row.get("f20")),
        "floatCap": num(row.get("f21")), "speed": num(row.get("f22")),
        "pb": num(row.get("f23")), "chg60d": num(row.get("f24")),
        "chgYtd": num(row.get("f25")), "mainInflow": num(row.get("f62")),
        "peTtm": num(row.get("f115")), "source": "东方财富",
    }


def fetch_list_page(market, page, size, sort_field, order):
    res = em("/api/qt/clist/get", {
        "pn": page, "pz": size, "po": 1 if order == "desc" else 0, "np": 1,
        "fltt": 2, "invt": 2, "dect": 1, "fid": sort_field,
        "fs": CN_FS if market == "cn" else US_FS,
        "fields": CN_LIST_FIELDS if market == "cn" else US_LIST_FIELDS,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    })
    data = (res or {}).get("data") or {}
    return data.get("total") or 0, [norm_row(r, market) for r in (data.get("diff") or [])]


def sina_snapshot():
    """A股全市场快照备用源（新浪财经，每页 100 只）"""
    def fetch(page):
        txt = http_get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                       "Market_Center.getHQNodeData?page=%d&num=100&sort=changepercent&asc=0"
                       "&node=hs_a&symbol=&_s_r_a=page" % page,
                       referer="https://finance.sina.com.cn/", raw=True)
        txt = txt.strip().replace("'", '"')
        return json.loads(txt) if txt and txt != "null" else []

    first = fetch(1)
    if not first:
        raise RuntimeError("新浪快照为空")
    out = list(first)

    def one(p):
        try:
            return fetch(p)
        except Exception:  # noqa: BLE001
            return []

    with ThreadPoolExecutor(max_workers=5) as pool:
        for rows in pool.map(one, range(2, 62)):
            if not rows:
                continue
            out.extend(rows)
    norm = []
    for r in out:
        try:
            prev = num(r.get("settlement")) or 0
            hi = num(r.get("high")); lo = num(r.get("low"))
            norm.append({
                "code": str(r.get("code") or "").strip(), "name": r.get("name") or "",
                "market": "cn", "mktNum": 1 if str(r.get("symbol", "")).startswith("sh") else 0,
                "secid": "",
                "price": num(r.get("trade")), "changePct": num(r.get("changepercent")),
                "change": num(r.get("pricechange")),
                "volume": (num(r.get("volume")) or 0) / 100, "amount": num(r.get("amount")),
                "amplitude": ((hi - lo) / prev * 100) if (prev and hi and lo) else None,
                "turnover": num(r.get("turnoverratio")), "pe": num(r.get("per")),
                "volumeRatio": None, "high": hi, "low": lo, "open": num(r.get("open")),
                "prevClose": prev or None,
                "marketCap": (num(r.get("mktcap")) or 0) * 1e4 or None,
                "floatCap": (num(r.get("nmc")) or 0) * 1e4 or None,
                "speed": None, "pb": num(r.get("pb")), "chg60d": None, "chgYtd": None,
                "mainInflow": None, "peTtm": num(r.get("per")), "source": "新浪财经",
            })
        except Exception:  # noqa: BLE001
            continue
    seen, uniq = set(), []
    for r in norm:
        if r["code"] and r["code"] not in seen and r["price"]:
            seen.add(r["code"])
            uniq.append(r)
    if len(uniq) < 1000:
        raise RuntimeError("新浪快照不完整(%d)" % len(uniq))
    return uniq


DATA_DIR = os.path.join(BASE_DIR, "data")


def disk_save(name, obj):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "rows": obj}, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def disk_load(name):
    try:
        with open(os.path.join(DATA_DIR, name + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("rows"), data.get("ts")
    except Exception:  # noqa: BLE001
        return None, None


SNAPSHOT_META = {"cn": {"source": "未知", "ts": None}, "us": {"source": "未知", "ts": None}}


def em_snapshot():
    total, first = fetch_list_page("cn", 1, 100, "f3", "desc")
    if not first:
        raise RuntimeError("A股快照为空")
    pages = min(70, (total + 99) // 100)
    out = list(first)

    def one(p):
        try:
            _, rows = fetch_list_page("cn", p, 100, "f3", "desc")
            return rows
        except Exception:  # noqa: BLE001
            return []

    if pages > 1:
        with ThreadPoolExecutor(max_workers=5) as pool:
            for rows in pool.map(one, range(2, pages + 1)):
                out.extend(rows)
    seen, uniq = set(), []
    for r in out:
        if r["code"] and r["code"] not in seen and r["price"] is not None:
            seen.add(r["code"])
            uniq.append(r)
    need = max(1000, int(total * 0.6))
    if len(uniq) < need:
        raise RuntimeError("A股快照数据不完整(%d/%d)" % (len(uniq), total))
    return uniq


def build_cn_snapshot():
    """多源降级：东方财富 -> 新浪财经 -> 本地磁盘快照"""
    errors = []
    for name, fn in (("东方财富", em_snapshot), ("新浪财经", sina_snapshot)):
        try:
            rows = fn()
            SNAPSHOT_META["cn"] = {"source": name, "ts": time.time()}
            disk_save("cn_snapshot", rows)
            return rows
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (name, str(exc)[:90]))
    rows, ts = disk_load("cn_snapshot")
    if rows:
        SNAPSHOT_META["cn"] = {"source": "本地缓存", "ts": ts}
        return rows
    raise RuntimeError("A股快照不可用（" + "；".join(errors) + "）")


def cn_snapshot():
    return cached("cn_snapshot_v4", 75, build_cn_snapshot)


def us_snapshot():
    def build():
        out = []
        for p in (1, 2, 3, 4, 5, 6):
            try:
                _, rows = fetch_list_page("us", p, 100, "f6", "desc")
                out.extend(rows)
            except Exception:  # noqa: BLE001
                pass
        out = [r for r in out if r["code"] and r["price"] is not None]
        if len(out) < 200:
            rows, ts = disk_load("us_snapshot")
            if rows:
                SNAPSHOT_META["us"] = {"source": "本地缓存", "ts": ts}
                return rows
            raise RuntimeError("美股样本不足(%d)" % len(out))
        SNAPSHOT_META["us"] = {"source": "东方财富", "ts": time.time()}
        disk_save("us_snapshot", out)
        return out

    return cached("us_snapshot_v4", 120, build)



BREADTH_BUCKETS = [
    ("涨停/接近涨停 ≥9.8%", 9.8, 1e9), ("大涨 5%~9.8%", 5, 9.8),
    ("上涨 2%~5%", 2, 5), ("微涨 0%~2%", 0, 2),
    ("微跌 -2%~0%", -2, 0), ("下跌 -5%~-2%", -5, -2),
    ("大跌 -9.8%~-5%", -9.8, -5), ("跌停/接近跌停 ≤-9.8%", -1e9, -9.8),
]


def breadth(rows):
    up = down = flat = 0
    amount = 0.0
    for r in rows:
        p = r.get("changePct")
        if p is None:
            continue
        if p > 0.0001:
            up += 1
        elif p < -0.0001:
            down += 1
        else:
            flat += 1
        amount += r.get("amount") or 0
    buckets = []
    for label, lo, hi in BREADTH_BUCKETS:
        cnt = sum(1 for r in rows if r.get("changePct") is not None
                  and r.get("price") is not None and lo <= r["changePct"] < hi)
        buckets.append({"label": label, "count": cnt})
    return {"up": up, "down": down, "flat": flat, "amount": amount, "buckets": buckets}


def api_overview(market, size=20):
    rows = cn_snapshot() if market == "cn" else us_snapshot()
    size = max(5, min(int(size), 60))

    def top(key, reverse=True, min_amount=0, positive_only=False):
        pool = [r for r in rows if r.get(key) is not None]
        if min_amount:
            pool = [r for r in pool if (r.get("amount") or 0) >= min_amount]
        if positive_only:
            pool = [r for r in pool if (r.get("changePct") or 0) > 0]
        pool.sort(key=lambda r: r.get(key) or 0, reverse=reverse)
        return pool[:size]

    return {
        "market": market, "updated": now_ms(),
        "indices": indices(market)["rows"],
        "breadth": breadth(rows),
        "sampleSize": len(rows),
        "breadthScope": "沪深京全市场" if market == "cn" else ("成交额前 %d 活跃样本" % len(rows)),
        "gainers": top("changePct", True, min_amount=1e7),
        "losers": top("changePct", False, min_amount=1e7),
        "actives": top("amount", True),
        "highTurnover": top("turnover", True, min_amount=1e7),
        "inflow": top("mainInflow", True, min_amount=1e7),
        "volumeRatio": top("volumeRatio", True, min_amount=1e7) if market == "cn" else [],
        "source": SNAPSHOT_META[market]["source"],
        "snapshotTs": int((SNAPSHOT_META[market]["ts"] or time.time()) * 1000),
        "stale": SNAPSHOT_META[market]["source"] == "本地缓存",
    }


SORT_WHITELIST = {"changePct", "price", "amount", "volume", "turnover", "volumeRatio",
                  "marketCap", "floatCap", "pe", "peTtm", "pb", "amplitude", "speed",
                  "mainInflow", "chg60d", "chgYtd", "change"}


def api_list(market, sort, order, page, size, filters):
    rows = cn_snapshot() if market == "cn" else us_snapshot()
    sort = sort if sort in SORT_WHITELIST else "changePct"
    kw = (filters.get("kw") or "").strip().upper()
    out = []
    for r in rows:
        if kw and kw not in r["code"].upper() and kw not in (r["name"] or "").upper():
            continue
        if r.get("changePct") is None:
            continue
        ok = True
        for key, lo, hi in (
            ("changePct", filters.get("pctMin"), filters.get("pctMax")),
            ("turnover", filters.get("turnoverMin"), filters.get("turnoverMax")),
            ("volumeRatio", filters.get("vrMin"), filters.get("vrMax")),
            ("price", filters.get("priceMin"), filters.get("priceMax")),
            ("marketCap", filters.get("capMin"), filters.get("capMax")),
            ("peTtm", filters.get("peMin"), filters.get("peMax")),
        ):
            if lo is None and hi is None:
                continue
            v = r.get(key)
            if v is None:
                ok = False
                break
            if lo is not None and v < lo:
                ok = False
                break
            if hi is not None and v > hi:
                ok = False
                break
        if not ok:
            continue
        if filters.get("minFlow") is not None:
            if (r.get("mainInflow") or 0) < filters["minFlow"]:
                continue
        out.append(r)
    out.sort(key=lambda r: (r.get(sort) if r.get(sort) is not None else -1e18),
             reverse=(order != "asc"))
    page = max(1, int(page))
    size = max(10, min(int(size), 200))
    start = (page - 1) * size
    return {"market": market, "total": len(out), "page": page, "size": size,
            "sort": sort, "order": order, "rows": out[start:start + size],
            "sampleScope": "沪深京全市场" if market == "cn" else ("成交额前 %d 活跃样本" % len(rows)),
            "source": SNAPSHOT_META[market]["source"],
            "snapshotTs": int((SNAPSHOT_META[market]["ts"] or time.time()) * 1000),
            "stale": SNAPSHOT_META[market]["source"] == "本地缓存",
            "updated": now_ms()}


def api_movers(market="cn"):
    rows = cn_snapshot() if market == "cn" else us_snapshot()

    def build():
        out = []
        for r in rows:
            pct = r.get("changePct")
            if pct is None or (r.get("amount") or 0) < 5e7:
                continue
            tags = []
            if pct >= 9.8:
                tags.append("涨停")
            elif pct <= -9.8:
                tags.append("跌停")
            if (r.get("volumeRatio") or 0) >= 2.5 and pct > 3:
                tags.append("放量上攻")
            if (r.get("turnover") or 0) >= 10 and pct > 2:
                tags.append("高换手")
            if (r.get("speed") or 0) >= 0.8:
                tags.append("快速拉升")
            if (r.get("speed") or 0) <= -0.8:
                tags.append("快速跳水")
            if (r.get("mainInflow") or 0) > 1e8 and pct > 0:
                tags.append("主力净流入")
            if (r.get("mainInflow") or 0) < -1e8 and pct < 0:
                tags.append("主力净流出")
            if tags:
                item = dict(r)
                item["tags"] = tags
                out.append(item)
        out.sort(key=lambda x: (len(x["tags"]), abs(x.get("changePct") or 0)), reverse=True)
        return {"market": market, "rows": out[:60], "source": "东方财富", "updated": now_ms()}

    return cached("movers_v3_%s" % market, 45, build)


def api_sectors(kind="industry"):
    fs = {"industry": "m:90+t:2+f:!50", "concept": "m:90+t:3+f:!50",
          "region": "m:90+t:1+f:!50"}.get(kind, "m:90+t:2+f:!50")

    def build():
        out = []
        for page in (1, 2):
            res = em("/api/qt/clist/get", {
                "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": fs,
                "fields": "f2,f3,f4,f6,f8,f12,f14,f62,f104,f105,f128,f136,f140,f141",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            })
            for r in ((res or {}).get("data") or {}).get("diff") or []:
                out.append({
                    "code": r.get("f12"), "name": r.get("f14"), "price": num(r.get("f2")),
                    "changePct": num(r.get("f3")), "amount": num(r.get("f6")),
                    "turnover": num(r.get("f8")), "mainInflow": num(r.get("f62")),
                    "upCount": num(r.get("f104")), "downCount": num(r.get("f105")),
                    "leader": r.get("f128"), "leaderPct": num(r.get("f136")),
                    "leaderCode": r.get("f140"),
                })
        if not out:
            raise RuntimeError("板块数据为空")
        return {"kind": kind, "rows": out, "source": "东方财富", "updated": now_ms()}

    return cached("sector_v3_%s" % kind, 40, build)


def api_news(limit=30):
    def build():
        res = http_get("https://np-listapi.eastmoney.com/comm/web/getFastNewsList?" +
                       urllib.parse.urlencode({
                           "client": "web", "biz": "web_724", "fastColumn": 102,
                           "sortEnd": "", "pageSize": max(10, min(int(limit), 50)),
                           "req_trace": int(time.time() * 1000)}),
                       referer="https://kuaixun.eastmoney.com/")
        items = ((res or {}).get("data") or {}).get("fastNewsList") or []
        out = []
        for it in items:
            out.append({
                "title": it.get("title") or "",
                "summary": re.sub(r"<[^>]+>", "", it.get("summary") or ""),
                "time": it.get("showTime"), "code": it.get("code"),
                "url": it.get("uniqueUrl"),
            })
        if not out:
            raise RuntimeError("资讯为空")
        return {"rows": out, "source": "东方财富", "updated": now_ms()}

    return cached("news_v3_%d" % limit, 35, build)


def api_search(q):
    q = (q or "").strip()
    if not q:
        return {"rows": []}

    def build():
        try:
            res = http_get("https://searchapi.eastmoney.com/api/suggest/get?" +
                           urllib.parse.urlencode({
                               "input": q, "type": 14,
                               "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 12}),
                           referer="https://www.eastmoney.com/")
            data = ((res or {}).get("QuotationCodeTable") or {}).get("Data") or []
        except Exception:  # noqa: BLE001
            data = []
        rows = []
        for d in data:
            cls = d.get("Classify")
            if cls not in ("AStock", "UsStock", "BStock", "Index", "Fund", "HkStock",
                           "NEEQ", "KCB"):
                continue
            rows.append({
                "code": d.get("Code"), "name": d.get("Name"),
                "market": "us" if cls in ("UsStock",) else "cn",
                "secid": d.get("QuoteID"), "type": d.get("SecurityTypeName"),
                "classify": cls,
            })
        # 兜底：像美股代码的字母串在接口没收录时也放行，让用户仍能尝试导航。
        # 必须限定 ASCII：Python 的 str.isalpha() 对中文也返回 True，于是「不存在的公司」
        # 这种 6 字以内的中文会被回显成一个同名美股代码 —— 实测过，搜索结果里出现
        # 一只并不存在的股票，比「搜不到」误导性大得多。
        if not rows and q.isascii() and q.isalpha() and len(q) <= 6:
            rows.append({"code": q.upper(), "name": q.upper(), "market": "us",
                         "secid": "105." + q.upper(), "type": "美股", "classify": "UsStock"})
        return {"rows": rows[:12], "source": "东方财富"}

    return cached("search_v3_%s" % q.lower(), 1800, build)


SECID_MEMO = {}


def em_secid(market, code):
    code = str(code).strip()
    if market == "cn":
        if re.match(r"^\d\.[A-Za-z0-9]+$", code):
            return code
        cu = code.upper()
        alias = {"NDX": "100.NDX", "DJIA": "100.DJIA", "SPX": "100.SPX", "IXIC": "100.IXIC",
                 "HSI": "100.HSI", "N225": "100.N225"}
        if cu in alias:
            return alias[cu]
        return ("1." if code[0] in "6957" else "0.") + code
    key = "us:" + code.upper()
    if key in SECID_MEMO:
        return SECID_MEMO[key]
    secid = "105." + code.upper()
    alias = {"DJI": "100.DJIA", "IXIC": "100.NDX", "INX": "100.SPX", "NDX": "100.NDX",
             "DJIA": "100.DJIA", "SPX": "100.SPX"}
    if code.upper() in alias:
        secid = alias[code.upper()]
    else:
        try:
            res = http_get("https://searchapi.eastmoney.com/api/suggest/get?" +
                           urllib.parse.urlencode({
                               "input": code, "type": 14,
                               "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 8}),
                           referer="https://www.eastmoney.com/")
            for d in ((res or {}).get("QuotationCodeTable") or {}).get("Data") or []:
                if d.get("Classify") == "UsStock" and \
                        str(d.get("Code", "")).upper() == code.upper():
                    secid = d.get("QuoteID") or secid
                    break
        except Exception:  # noqa: BLE001
            pass
    SECID_MEMO[key] = secid
    return secid


def sina_fundflow(code):
    """新浪资金流兜底（仅 A股，主力=超大单+大单）"""
    sym = ("sh" if code[0] in "69" else "sz") + code
    txt = http_get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   "MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=30&sort=opendate&asc=0&daima=" + sym,
                   referer="https://finance.sina.com.cn/", raw=True)
    txt = txt.strip().replace("'", '"')
    try:
        data = json.loads(txt)
    except Exception:  # noqa: BLE001
        return []
    series = []
    for it in reversed(data or []):
        series.append({"t": it.get("opendate"), "main": num(it.get("r0_net")),
                       "small": None, "mid": None, "big": num(it.get("r0_net")),
                       "huge": None, "mainPct": num(it.get("r0_ratio")),
                       "close": num(it.get("trade")),
                       "changePct": (num(it.get("changeratio")) or 0) * 100})
    return series


def api_fundflow(market, code):
    def build():
        errors = []
        try:
            res = em("/api/qt/stock/fflow/daykline/get", {
                "secid": em_secid(market, code), "klt": 101, "lmt": 120,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "ut": "b2884a393a59ad64002292a3e90d46a5",
            })
            series = []
            for line in ((res or {}).get("data") or {}).get("klines") or []:
                p = line.split(",")
                if len(p) < 6:
                    continue
                series.append({"t": p[0], "main": num(p[1]), "small": num(p[2]),
                               "mid": num(p[3]), "big": num(p[4]), "huge": num(p[5]),
                               "mainPct": num(p[6]) if len(p) > 6 else None,
                               "close": num(p[11]) if len(p) > 11 else None,
                               "changePct": num(p[12]) if len(p) > 12 else None})
            if series:
                return {"code": code, "market": market, "series": series[-60:],
                        "source": "东方财富", "updated": now_ms()}
            errors.append("东财资金流为空")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:100])
        if market == "cn":
            try:
                series = sina_fundflow(code)
                if series:
                    return {"code": code, "market": market, "series": series[-30:],
                            "source": "新浪财经（仅主力净额）", "updated": now_ms(),
                            "limited": True}
                errors.append("新浪资金流为空")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:100])
        return {"code": code, "market": market, "series": [], "source": None,
                "error": "；".join(errors) or "无资金流数据", "updated": now_ms()}

    return cached("ff_v4_%s_%s" % (market, code), 120, build)



def api_stock(market, code):
    """个股详情：腾讯全文行情（含五档、市值、估值）"""
    def build():
        rows = quotes(market, [code])
        if not rows:
            raise RuntimeError("未获取到 %s 行情" % code)
        q = rows[0]
        q["fundflow"] = None
        try:
            ff = api_fundflow(market, code)
            if ff.get("series"):
                last = ff["series"][-1]
                q["fundflow"] = {"date": last["t"], "main": last["main"],
                                 "mainPct": last.get("mainPct"),
                                 "big": last.get("big"), "huge": last.get("huge"),
                                 "mid": last.get("mid"), "small": last.get("small")}
        except Exception:  # noqa: BLE001
            pass
        q["updated"] = now_ms()
        return q

    return cached("stock_v3_%s_%s" % (market, code), 5, build)


def now_ms():
    return int(time.time() * 1000)


def api_health():
    return {"ok": True, "serverTime": now_ms(), "cacheKeys": len(_CACHE),
            "tz": "Asia/Shanghai", "version": "2.0",
            "engine": (RUNNER.status() if RUNNER is not None else {"running": False})}


# --------------------------------------------------------------------------- #
# 策略跟踪 / 回测 / 参数寻优（服务端统一引擎）
#   分层见 core/runner.py：数据源 → 策略 → 撮合 → 账户 → 绩效 → 持久
#   回测与跟踪共用同一份实现，避免出现两份口径（对标调研报告 A1/A2 项）
# --------------------------------------------------------------------------- #

RUNNER = None


def _runner_fetch_bars(market, code, period, limit):
    res = api_kline(market, code, period or "day", 1, limit or 800)
    return res.get("bars") or []


def _runner_fetch_quote(market, code):
    rows = quotes(market, [code])
    return rows[0] if rows else {}


def _runner_fetch_orderbook(market, code):
    """盘口（深度加权成交模型需要）；美股只有一档，取不到就返回空由模型降级"""
    try:
        st = api_stock(market, code)
    except Exception:  # noqa: BLE001
        return {}
    return {"bids": st.get("bids") or [], "asks": st.get("asks") or []}


def init_runner():
    """初始化引擎：SQLite 存储 + 结构化日志 + 通知器 + 数据抓取器注入"""
    global RUNNER
    if RUNNER is not None:
        return RUNNER
    data_dir = os.path.join(BASE_DIR, "data")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    db_path = os.environ.get("AD_DB") or os.path.join(data_dir, "alphadesk.db")
    legacy = os.path.join(data_dir, "strategy_runs.json")
    try:
        core_logs.configure(os.path.join(data_dir, "alphadesk.log"))
    except Exception:  # noqa: BLE001
        pass
    RUNNER = core_runner.Runner(db_path, data_dir=data_dir, legacy_json=legacy)
    RUNNER.notifier = core_notify.Notifier(store=RUNNER.store,
                                          echo=bool(os.environ.get("AD_VERBOSE")))
    RUNNER.configure(_runner_fetch_bars, _runner_fetch_quote, _runner_fetch_orderbook)
    try:
        RUNNER.backfill_costs()
    except Exception:  # noqa: BLE001
        pass
    return RUNNER


def runner():
    return RUNNER if RUNNER is not None else init_runner()


def api_strategy_meta():
    meta = core_strategies.meta()
    today = datetime.date.today()
    windows = []
    for key, label, days in (("1m", "近 1 个月", 30), ("3m", "近 3 个月（推荐）", 92),
                             ("6m", "近 6 个月", 183), ("1y", "近 1 年", 365)):
        start = today - datetime.timedelta(days=days)
        windows.append({"value": key, "label": label, "startDate": start.strftime("%Y-%m-%d")})
    return {
        "strategies": meta,
        "periods": [{"value": v, "label": lb} for v, lb in
                    (("day", "日K"), ("week", "周K"), ("month", "月K"), ("60m", "60分钟"),
                     ("30m", "30分钟"), ("15m", "15分钟"), ("5m", "5分钟"))],
        "windows": windows,
        "targets": [30, 60, 90, 180],
        "fillModels": core_fills.meta(),
        "metricsModes": [
            {"value": "compound", "label": "几何累乘（复利口径）"},
            {"value": "simple", "label": "算术累加（单利口径）"},
        ],
        "metrics": [
            {"key": "total_return", "label": "累计收益"},
            {"key": "annualized_return", "label": "年化收益"},
            {"key": "sharpe", "label": "夏普比率"},
            {"key": "calmar", "label": "卡玛比率"},
            {"key": "max_drawdown", "label": "最大回撤"},
        ],
        "editable": {
            "safe": list(core_runner.SAFE_FIELDS),
            "logic": list(core_runner.LOGIC_FIELDS),
            "labels": core_runner.FIELD_LABELS,
        },
        "engine": runner().status(),
        "storage": runner().store.meta_get("db_path", None) or "sqlite",
        "store": os.path.join(BASE_DIR, "data", "alphadesk.db"),
    }


def api_strategy_overview():
    return runner().overview()


def api_strategy_detail(rid):
    data = runner().detail(rid)
    if not data:
        raise RuntimeError("任务不存在：%s" % rid)
    return data


def api_strategy_create(body):
    r = runner()
    run = r.create_run(body or {})
    price, min_cap = None, None
    try:
        q = _runner_fetch_quote(run["market"], run["code"])
        price = q.get("price")
        lot = run.get("lot") or 100
        if price:
            min_cap = price * lot * 1.01
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "run": run, "price": price, "minCapital": min_cap}


def api_strategy_update(body):
    body = body or {}
    rid = body.get("id")
    if not rid:
        raise RuntimeError("缺少 id")
    patch = body.get("patch") or {}
    if not isinstance(patch, dict) or not patch:
        raise RuntimeError("缺少需要调整的字段")
    res = runner().revise(rid, patch, bool(body.get("reset")))
    price, min_cap = None, None
    try:
        run = res.get("run") or {}
        q = _runner_fetch_quote(run.get("market") or "cn", run.get("code"))
        price = q.get("price")
        lot = run.get("lot") or 100
        if price:
            min_cap = price * lot * 1.01
    except Exception:  # noqa: BLE001
        pass
    res["price"] = price
    res["minCapital"] = min_cap
    return res


def api_strategy_action(body):
    body = body or {}
    rid = body.get("id")
    act = body.get("action")
    if not rid or not act:
        raise RuntimeError("缺少 id 或 action")
    return runner().action(rid, act)


def api_backtest(body):
    """服务端统一回测（前端只负责画图，不再自行计算信号）"""
    return runner().backtest(body or {})


def api_params_search(body):
    """参数网格寻优"""
    return runner().grid_search(body or {})


# --------------------------------------------------------------------------- #
# AI 选股（多标的批量研判：建议 / 凯利仓位 / 预测 / 组合分配）
# --------------------------------------------------------------------------- #

def api_advisor_recommend(body):
    """AI 选股：一次提交多只标的，返回逐只建议 + 凯利仓位 + 预测 + 组合分配。

    标的字段兼容两种写法：
      · ``symbols``：[{code, market, name}]，逐只带市场（推荐，支持 A股 + 美股混合）；
      · ``codes``：["600519", "AAPL"]，统一用顶层 ``market`` 解释（前端旧写法兜底）。

    落库开关：``save`` 为真时把本次研判整理成一条记录写进 SQLite（`advisor_runs`
    + `advisor_items`），响应里回带 ``recordId``；``trigger`` / ``note`` 一并入库，
    便于在历史记录里区分来源（列表批量研判 / 个股详情手动保存）。
    保存失败**不影响研判结果**：只在响应里带 ``saveError`` 并记一条错误日志。
    """
    body = body or {}
    market = str(body.get("market") or "cn").strip().lower()
    market = "us" if market.startswith("us") else "cn"

    symbols = body.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        symbols = body.get("codes")
    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError("缺少标的：请提供 symbols 或 codes")
    if len(symbols) > 60:
        raise RuntimeError("单次最多提交 60 只标的（当前 %d 只）" % len(symbols))

    res = core_advisor.recommend(
        symbols,
        fetch_bars=_runner_fetch_bars,
        fetch_quote=_runner_fetch_quote,
        market=market,
        horizon=body.get("horizon"),
        capital=body.get("capital"),
        kelly_fraction=body.get("kellyFraction"),
        max_weight=body.get("maxWeight"),
        cash_buffer=body.get("cashBuffer"),
        fee=body.get("fee"),
        slippage=body.get("slippage"),
        period=body.get("period") or "day",
        limit=body.get("limit") or 800,
    )

    res["recordId"] = None
    res["saved"] = False
    if body.get("save"):
        try:
            rec = core_advisor.to_record(
                res,
                trigger=str(body.get("trigger") or "list"),
                note=str(body.get("note") or ""),
                keep=advisor_keep(),
            )
            res["recordId"] = advisor_store().save_advisor_run(rec, keep=advisor_keep())
            res["saved"] = True
            try:
                runner().logger.info("advisor.saved", recordId=res["recordId"],
                                     market=market, symbols=rec["run"]["symbolCount"],
                                     analyzed=rec["run"]["analyzed"],
                                     trigger=rec["run"]["trigger"])
            except Exception:  # noqa: BLE001  日志失败不影响业务
                pass
        except Exception as exc:  # noqa: BLE001  保存失败不拖垮研判
            res["saveError"] = "记录保存失败：%s" % str(exc)[:200]
            try:
                runner().logger.error("advisor.save_failed", error=str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
    res["historyCount"] = safe_call(lambda: advisor_store().advisor_stats().get("records"))
    return res


# --------------------------------------------------------------------------- #
# AI 选股记录（持久化：历史列表 / 载入 / 复盘 / 备注置顶 / 删除清理）
# --------------------------------------------------------------------------- #

def advisor_store():
    """AI 选股记录与策略跟踪共用同一个 SQLite 库（data/alphadesk.db），避免多库并存。"""
    return runner().store


def advisor_keep():
    """记录保留上限：默认取 Store.ADVISOR_KEEP（500），可用 AD_ADVISOR_KEEP 覆盖。"""
    try:
        v = int(os.environ.get("AD_ADVISOR_KEEP") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else core_storage.Store.ADVISOR_KEEP


def _advisor_query(q):
    """把 GET 查询串整理成 list_advisor_runs 的过滤参数（非法值一律忽略）。"""
    pinned = str((q.get("pinned") or "")).strip().lower() in ("1", "true", "yes", "on")
    return {
        "limit": int(num(q.get("limit"), 50) or 50),
        "offset": int(num(q.get("offset"), 0) or 0),
        "market": (q.get("market") or "").strip().lower() or None,
        "code": (q.get("code") or "").strip().upper() or None,
        "action": (q.get("action") or "").strip().lower() or None,
        "q": (q.get("q") or "").strip() or None,
        "pinned": pinned,
    }


def api_advisor_history(q):
    """历史记录列表（置顶优先 + 时间倒序），带汇总统计与保留策略说明。"""
    st = advisor_store()
    found = st.list_advisor_runs(**_advisor_query(q or {}))
    stats = st.advisor_stats()
    return {
        "ok": True,
        "rows": found["rows"],
        "total": found["total"],
        "limit": found["limit"],
        "offset": found["offset"],
        "stats": {
            "records": stats["records"], "items": stats["items"],
            "buyTotal": stats["buyTotal"], "avgTotalWeight": stats["avgTotalWeight"],
            "latestAt": stats["latestAt"],
        },
        "retention": {"limit": advisor_keep(), "pruned": stats["prunedTotal"],
                      "pinned": advisor_store().advisor_pinned_count()},
        "note": core_advisor.record_note(advisor_keep()),
        "updated": now_ms(),
    }


def api_advisor_record(q):
    """载入一条记录：完整还原当时的逐只结论与组合分配（预测带已裁剪，见 note）。"""
    rid = (q.get("id") or "").strip()
    if not rid:
        raise RuntimeError("缺少 id")
    rec = advisor_store().get_advisor_run(rid)
    if not rec:
        raise RuntimeError("记录不存在：%s" % rid)
    return {"ok": True, "record": rec, "review": None, "updated": now_ms()}


def api_advisor_review(q):
    """复盘：用保存之后真实发生的行情检验当时的建议方向是否成立。"""
    rid = (q.get("id") or "").strip()
    if not rid:
        raise RuntimeError("缺少 id")
    rec = advisor_store().get_advisor_run(rid)
    if not rec:
        raise RuntimeError("记录不存在：%s" % rid)
    raw = (q.get("horizons") or "").strip()
    hs = []
    for piece in re.split(r"[,\s]+", raw):
        if piece.isdigit() and int(piece) >= 1:
            hs.append(int(piece))
    res = core_advisor.review(rec, _runner_fetch_bars,
                              horizons=tuple(hs) or core_advisor.REVIEW_HORIZONS)
    res["note"] = core_advisor.record_note(advisor_keep())
    res["recordId"] = rid
    res["updated"] = now_ms()
    return res


def api_advisor_delete(body):
    """删除一条记录（逐只明细级联删除）"""
    rid = str((body or {}).get("id") or "").strip()
    if not rid:
        raise RuntimeError("缺少 id")
    ok = advisor_store().delete_advisor_run(rid)
    return {"ok": True, "id": rid, "deleted": bool(ok),
            "remaining": advisor_store().advisor_stats()["records"]}


def api_advisor_note(body):
    """更新记录的备注 / 置顶状态（只改传进来的字段）"""
    body = body or {}
    rid = str(body.get("id") or "").strip()
    if not rid:
        raise RuntimeError("缺少 id")
    note = body.get("note")
    pinned = body.get("pinned")
    if note is None and pinned is None:
        raise RuntimeError("缺少 note 或 pinned")
    view = advisor_store().update_advisor_run(
        rid,
        note=None if note is None else str(note)[:500],
        pinned=None if pinned is None else bool(pinned),
    )
    if view is None:
        raise RuntimeError("记录不存在：%s" % rid)
    # 记录一条日志：备注/置顶是用户手工数据，出了问题要能从上到下追溯到底收到了什么
    try:
        runner().logger.info("advisor.note_updated", recordId=rid,
                             noteLen=len(str(note)) if note is not None else None,
                             pinned=None if pinned is None else bool(pinned))
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "id": rid, "note": view.get("note"), "pinned": view.get("pinned")}


def api_advisor_prune(body):
    """按保留上限清理最旧的未置顶记录（keep=0 表示清空全部未置顶记录）"""
    keep = int(num((body or {}).get("keep"), 0) or 0)
    st = advisor_store()
    deleted = st.advisor_prune(keep=keep)
    stats = st.advisor_stats()
    return {"ok": True, "deleted": deleted, "remaining": stats["records"],
            "keep": keep, "pinned": st.advisor_pinned_count()}


# --------------------------------------------------------------------------- #
# 标的名称识别（自动识别股票名称 / 简称 / 拼音 / 代码）
# --------------------------------------------------------------------------- #

def _symbol_search(query, market):
    """识别用的远端搜索：复用 /api/search 的实现与缓存，避免两处口径漂移。

    美股语境下把美股结果排到前面 —— 同名时（例如「苹果」）优先给用户想要的那个市场。
    """
    res = api_search(query) or {}
    rows = list(res.get("rows") or [])
    if str(market or "").startswith("us"):
        rows.sort(key=lambda r: 0 if (r.get("market") == "us") else 1)
    return {"rows": rows}


def symbol_index(market):
    """全市场名录索引（名称识别用）：复用个股列表快照，不引入新数据源。

    TTL 取 15 分钟：代码 ↔ 名称一个月也未必变一次，但新股与更名要能跟上；快照本身
    已有缓存，这里只是把它整理成便于匹配的索引。取不到时返回 None，识别会自动退化为
    「全部走远端搜索」，而不是整个接口失败。
    """
    mkt = "us" if str(market or "").lower().startswith("us") else "cn"

    def build():
        rows = us_snapshot() if mkt == "us" else cn_snapshot()
        idx = core_symbols.build_index(rows, mkt)
        if not idx.get("count"):
            raise RuntimeError("名录为空")
        return idx

    try:
        idx = cached("symbol_index_v1_%s" % mkt, 900, build)
    except Exception as exc:  # noqa: BLE001
        try:
            runner().logger.error("symbols.index_failed", market=mkt, error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return None
    return idx if isinstance(idx, dict) else None


def _split_tokens(raw):
    """把输入串切成列表：逗号（含中文逗号）、空格、换行、分号、制表符都算分隔符。"""
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if str(x).strip()]
    text = str(raw or "")
    for ch in ("，", "；", "\n", "\r", "\t", ";", "|", "、"):
        text = text.replace(ch, ",")
    text = text.replace(" ", ",")
    return [s.strip() for s in text.split(",") if s.strip()]


def api_symbols_resolve(body):
    """批量识别标的：代码 / 中文名 / 简称 / 拼音首字母 / 「代码:名称」混合写法。

    返回里每条都带 ``kind``（code / name / ambiguous / unknown）与 ``note``，
    歧义项另外带 ``hits`` 候选清单 —— 前端据此让用户选择，而不是替用户猜。
    """
    body = body or {}
    market = str(body.get("market") or "cn").strip().lower()
    tokens = body.get("tokens")
    if tokens is None:
        tokens = body.get("q") or body.get("inputs") or body.get("symbols") or []
    items = _split_tokens(tokens)
    if not items:
        raise RuntimeError("请提供 tokens（代码或名称，逗号 / 空格 / 换行分隔）")
    idx = symbol_index(market)
    res = core_symbols.resolve(
        items, market=market, index=idx, search_fn=_symbol_search,
        limit=int(num(body.get("limit"), 5) or 5),
        max_tokens=int(num(body.get("max"), core_symbols.MAX_TOKENS) or core_symbols.MAX_TOKENS))
    res["ok"] = True
    res["market"] = "us" if market.startswith("us") else "cn"
    res["updated"] = now_ms()
    res["localIndex"] = {
        "available": idx is not None,
        "count": int((idx or {}).get("count") or 0),
        "note": ("本地名录来自个股列表快照（A 股为全市场，美股为活跃样本）；"
                 "本地命中即无需请求远端，拼音与错别字由远端搜索兜底"),
    }
    if idx is None:
        res["note"] = res["note"] + "｜注意：本地名录当前不可用，所有输入都走了远端搜索。"
    return res


def api_symbols_lookup(q):
    """单条查询的便捷入口：按代码查名称，或按名称查代码。"""
    q = q or {}
    text = str(q.get("code") or q.get("q") or "").strip()
    if not text:
        raise RuntimeError("请提供 code 或 q")
    return api_symbols_resolve({"market": q.get("market") or "cn", "tokens": [text]})


# --------------------------------------------------------------------------- #
# 实时推送中枢（SSE：行情 / 研判变化 / 交易事件）
# --------------------------------------------------------------------------- #

STREAM = None


def _stream_recommend(symbols, **kwargs):
    """给推送中枢用的研判入口：与 /api/advisor/recommend 共用同一份实现（单一口径）"""
    return core_advisor.recommend(symbols, fetch_bars=_runner_fetch_bars,
                                 fetch_quote=_runner_fetch_quote, **kwargs)


def _post_json(url, body, timeout=6):
    """外发 webhook：失败只记日志，绝不向上抛。

    对接方（用户自己的券商桥接）挂掉、超时、返回 500 都不该影响本地下单与推送，
    因此这里吞掉全部异常并留一条结构化日志便于排查。
    """
    try:
        payload = json.dumps(sanitize_json(body), ensure_ascii=False,
                             allow_nan=False, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
        try:
            runner().logger.info("trade.webhook_sent", event=body.get("event"), status=code)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        try:
            runner().logger.error("trade.webhook_failed", event=body.get("event"),
                                  error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass


def _stream_hook(kind, event, data):
    """推送旁路：交易事件按配置外发到 webhook（**与有没有浏览器订阅者无关**）"""
    if kind != core_stream.KIND_TRADE:
        return
    try:
        cfg = core_trader.get_config(advisor_store())
    except Exception:  # noqa: BLE001
        return
    url = cfg.get("webhook")
    if not url:
        return
    events = cfg.get("webhookEvents") or []
    if events and event not in events:
        return
    body = {"source": "alphadesk", "kind": kind, "event": event, "ts": now_ms(), "data": data}
    # 不阻塞发布线程：外发走后台线程（推送节奏不该被对接方的响应时间拖住）
    try:
        threading.Thread(target=_post_json, args=(url, body), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def init_stream():
    """初始化推送中枢：注入批量报价与 AI 研判，两者都复用既有实现"""
    global STREAM
    if STREAM is not None:
        return STREAM
    STREAM = core_stream.StreamHub(fetch_quotes=quotes, recommend=_stream_recommend,
                                  publish_hook=_stream_hook)
    return STREAM


def stream():
    return STREAM if STREAM is not None else init_stream()


def api_stream_status():
    """推送中枢状态（JSON）：订阅者数、各通道节拍与上游调用量、被丢弃事件数"""
    return stream().status()


def api_stream_test(q):
    """推送自检：按通道类型取一次真实上游结果，用于排查「为什么没推送」。

    不改动任何订阅状态：只做一次同步调用并返回结果摘要，便于区分「上游拿不到数据」
    与「推送链路有问题」。
    """
    kind = str((q or {}).get("kind") or "quotes").strip()
    params = core_stream.parse_params(kind, q)
    if kind == core_stream.KIND_QUOTES:
        codes = params.get("symbols") or []
        if not codes:
            raise RuntimeError("请提供 symbols")
        rows = quotes(params.get("market") or "cn", codes)
        return {"ok": True, "kind": kind, "market": params.get("market"),
                "requested": codes, "received": len(rows),
                "sample": rows[0] if rows else None}
    if kind == core_stream.KIND_ADVISOR:
        codes = params.get("symbols") or []
        if not codes:
            raise RuntimeError("请提供 symbols")
        res = _stream_recommend([{"code": c, "market": params.get("market")} for c in codes],
                                market=params.get("market"),
                                horizon=params.get("horizon"), capital=params.get("capital"),
                                kelly_fraction=params.get("kellyFraction"),
                                max_weight=params.get("maxWeight"))
        return {"ok": True, "kind": kind, "analyzed": res.get("analyzed"),
                "actions": {r.get("code"): r.get("action") for r in (res.get("rows") or [])},
                "note": "仅自检，不落库、不推送"}
    raise RuntimeError("trade 通道是事件驱动的，没有可自检的上游；请查看 /api/trade/orders")


# --------------------------------------------------------------------------- #
# 模拟交易 / 自动交易接口
# --------------------------------------------------------------------------- #
#: 自动交易的三条硬约束（写在接口说明与前端提示里，避免用户误解成真实下单）：
#: ① 默认关闭（enabled=False），且默认 dryrun（只出计划、不成交，不改账户）；
#: ② 不连接任何券商通道 —— 成交只发生在本地模拟账户里；
#: ③ 对外只提供「下单指令」的导出与回执：真正的报单由用户自己的桥接系统完成。

def trade_store():
    """自动交易与策略跟踪、AI 选股共用同一个 SQLite 库"""
    return runner().store


def trade_cfg(store=None):
    """当前交易配置（合并默认值后的完整字段集）"""
    return core_trader.get_config(store if store is not None else trade_store())


def trade_quotes(codes, market):
    """批量取报价（成交定价用）。取不到就返回空列表，由 trader 记 rejected 而不是抛异常。"""
    codes = [str(c).strip().upper() for c in (codes or []) if str(c).strip()]
    if not codes:
        return []
    try:
        return quotes(market, codes) or []
    except Exception as exc:  # noqa: BLE001  行情源故障不该让接口 500
        try:
            runner().logger.error("trade.quote_failed", market=market, error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return []


def _trade_publish(event, data):
    """把交易事件推给所有 trade 订阅者（同时触发外发钩子）"""
    try:
        return stream().publish_trade(event, data)
    except Exception:  # noqa: BLE001
        return 0


def _trade_account_event(cfg, market, quotes_list=None):
    """推一次账户快照（下单/成交/重置后都推，前端总览才能跟上）"""
    try:
        view = core_trader.account_view(trade_store(), cfg, quotes_list)
    except Exception:  # noqa: BLE001
        return None
    _trade_publish("account", dict(view, ts=now_ms()))
    return view


def api_trade_config_get():
    """交易配置 + 生效风控快照 + 账户概览（前端进页面第一个请求）"""
    st = trade_store()
    cfg = core_trader.get_config(st)
    snap = core_trader.trade_snapshot(st, cfg)
    return {
        "ok": True,
        "config": snap.get("config") or cfg,
        "gates": snap.get("gates") or {},
        "account": snap.get("account") or {},
        "accountId": (snap.get("account") or {}).get("accountId"),
        "counts": snap.get("counts") or {},
        "note": ("自动交易默认关闭；dryrun 模式只生成计划、不产生任何成交与账户变动。"
                 "confirmToken 是**防误触口令**，不是安全边界：本工具面向本机单用户，"
                 "请勿把端口暴露到公网。"),
    }


def api_trade_config_set(body):
    """更新交易配置（局部更新，未传字段保持不变）"""
    body = body or {}
    patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
    st = trade_store()
    cfg = core_trader.save_config(st, patch)
    try:
        runner().logger.info("trade.config_updated",
                             enabled=cfg.get("enabled"), mode=cfg.get("mode"),
                             market=cfg.get("market"),
                             keys=sorted(k for k in (patch or {}) if k != "confirmToken"))
    except Exception:  # noqa: BLE001
        pass
    _trade_publish("config", dict(cfg, ts=now_ms()))
    return {"ok": True, "config": cfg, "gates": core_trader.trade_snapshot(st, cfg).get("gates") or {},
            "note": "配置已保存；enabled 与 mode 变更会立即生效。"}


def api_trade_account(q):
    """账户概览（现金 / 持仓 / 市值 / 权益 / 盈亏 + 生效风控）"""
    q = q or {}
    market = str(q.get("market") or "").strip().lower() or None
    st = trade_store()
    cfg = core_trader.get_config(st)
    if market:
        cfg = dict(cfg, market="us" if market.startswith("us") else "cn")
    codes = []
    try:
        acc = core_trader.ensure_account(st, cfg, cfg["market"])
        codes = [p.get("code") for p in (acc.get("positions") or []) if p.get("code")]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("账户初始化失败：%s" % str(exc)[:160])
    ql = trade_quotes(codes, cfg["market"])
    snap = core_trader.trade_snapshot(st, cfg, ql)
    return {"ok": True, "account": snap.get("account") or {}, "gates": snap.get("gates") or {},
            "counts": snap.get("counts") or {}, "updated": now_ms()}


def api_trade_orders(q):
    """委托单列表（支持 status/market/code/side/since 过滤与分页）"""
    q = q or {}
    found = trade_store().list_trade_orders(
        status=(q.get("status") or "").strip() or None,
        market=(q.get("market") or "").strip().lower() or None,
        code=(q.get("code") or "").strip().upper() or None,
        side=(q.get("side") or "").strip().lower() or None,
        since=int(num(q.get("since"), 0) or 0) or None,
        limit=int(num(q.get("limit"), 100) or 100),
        offset=int(num(q.get("offset"), 0) or 0))
    found["ok"] = True
    found["updated"] = now_ms()
    return found


def api_trade_export(q):
    """导出待执行意图（给外部桥接系统消费的稳定结构）"""
    q = q or {}
    res = core_trader.export_intents(trade_store(),
                                     since=int(num(q.get("since"), 0) or 0) or None,
                                     limit=int(num(q.get("limit"), 200) or 200))
    res["generatedAt"] = now_ms()
    res["markdown"] = ("外部系统执行后请调用 POST /api/trade/ack（body: "
                       "{id, extRef}）回执；未回执的委托会一直留在待执行列表里。")
    return res


def _trade_symbols(body, cfg):
    """计划标的：优先用请求里的 symbols，其次用配置里的 universe / whitelist"""
    raw = (body or {}).get("symbols")
    if isinstance(raw, (list, tuple)) and raw:
        return core_trader._codes(raw) or []
    return list(cfg.get("universe") or []) or list(cfg.get("whitelist") or [])


def api_trade_plan(body):
    """生成交易计划：研判 → 风控闸门 → 落库为 pending 委托（默认不成交）。

    ``execute`` 为真且模式是 paper 时立即按最新报价成交；dryrun 下**永不成交**。
    """
    body = body or {}
    st = trade_store()
    cfg = core_trader.get_config(st)
    market = str(body.get("market") or cfg.get("market") or "cn").lower()
    market = "us" if market.startswith("us") else "cn"
    symbols = _trade_symbols(body, cfg)
    cfg = dict(cfg, market=market)
    if symbols:
        cfg = dict(cfg, universe=symbols)
    res = core_trader.scan(st, cfg, _stream_recommend, market=market,
                           execute=bool(body.get("execute")))
    # 逐个新委托推事件：外部系统与前端都按 order 事件增量更新，不必轮询
    for order in res.get("orders") or []:
        _trade_publish("order", dict(order, ts=now_ms()))
    if res.get("filled"):
        for order in res.get("orders") or []:
            if order.get("status") == "filled":
                _trade_publish("fill", {
                    "orderId": order.get("id"), "code": order.get("code"),
                    "side": order.get("side"), "qty": order.get("qty"),
                    "fillPrice": order.get("fillPrice"), "fee": order.get("fee"),
                    "amount": order.get("amount"), "ts": now_ms()})
    _trade_account_event(cfg, market)
    res["ok"] = True
    res["updated"] = now_ms()
    res["note"] = (res.get("note") or "") + ("｜计划已落库为 pending 委托；"
                  "dryrun 模式不会成交，paper 模式需再调 /api/trade/execute 或带 execute=true。")
    return res


def _confirm_ok(body):
    """校验确认口令：`X-Trade-Confirm` 请求头或 body.confirm 任一匹配即可。

    再次强调这是**防误触**而不是鉴权：本工具面向本机单用户，口令会随配置接口返回。
    真正的安全边界是「不要把这个端口暴露给不可信网络」。
    """
    cfg = core_trader.get_config(trade_store())
    token = str(cfg.get("confirmToken") or "")
    got = str((body or {}).get("confirm") or "").strip()
    if not got:
        try:
            got = str(self_confirmation_header()).strip()
        except Exception:  # noqa: BLE001
            got = ""
    if not token or got != token:
        raise RuntimeError("确认口令不正确或缺失：请在配置区复制 confirmToken，"
                           "或通过请求头 %s 传入" % core_trader.CONFIRM_HEADER)
    return True


#: 当前请求的确认头（由 do_POST 在执行前写入，避免把 handler 传进业务函数）
_CONFIRM_HEADERS = threading.local()


def set_confirm_header(value):
    _CONFIRM_HEADERS.value = value


def self_confirmation_header():
    return getattr(_CONFIRM_HEADERS, "value", "")


def api_trade_execute(body):
    """执行委托：需要确认口令；只处理 pending 委托，逐单重算报价与可成交性"""
    body = body or {}
    _confirm_ok(body)
    st = trade_store()
    cfg = core_trader.get_config(st)
    market = str(body.get("market") or cfg.get("market") or "cn").lower()
    market = "us" if market.startswith("us") else "cn"
    ids = body.get("ids")
    if isinstance(ids, (list, tuple)) and ids:
        orders = []
        for oid in ids:
            order = st.get_trade_order(str(oid))
            if order is None:
                raise RuntimeError("委托不存在：%s" % oid)
            orders.append(order)
    else:
        orders = st.list_trade_orders(status="pending", market=market, limit=500).get("rows") or []
    codes = [o.get("code") for o in orders if o.get("code")]
    ql = trade_quotes(codes, market)
    cfg = dict(cfg, market=market)
    res = core_trader.execute_orders(st, cfg, orders, ql)
    for order in res.get("orders") or []:
        _trade_publish("order", dict(order, ts=now_ms()))
        if order.get("status") == "filled":
            _trade_publish("fill", {
                "orderId": order.get("id"), "code": order.get("code"),
                "side": order.get("side"), "qty": order.get("qty"),
                "fillPrice": order.get("fillPrice"), "fee": order.get("fee"),
                "amount": order.get("amount"), "ts": now_ms()})
    res["account"] = _trade_account_event(cfg, market, ql)
    try:
        runner().logger.info("trade.executed", mode=cfg.get("mode"),
                             filled=res.get("filled"), rejected=res.get("rejected"))
    except Exception:  # noqa: BLE001
        pass
    res["ok"] = True
    res["updated"] = now_ms()
    return res


def api_trade_cancel(body):
    """撤单：只能撤 pending 委托"""
    oid = str((body or {}).get("id") or "").strip()
    if not oid:
        raise RuntimeError("缺少 id")
    order = core_trader.cancel_order(trade_store(), oid,
                                     reason=str((body or {}).get("reason") or "用户撤单"))
    if order is None:
        raise RuntimeError("委托不存在：%s" % oid)
    _trade_publish("order", dict(order, ts=now_ms()))
    return {"ok": True, "order": order}


def api_trade_close(body):
    """手动平仓（不受 enabled 限制：这是用户的显式意图），按最新报价成交"""
    body = body or {}
    code = str(body.get("code") or "").strip().upper()
    if not code:
        raise RuntimeError("缺少 code")
    st = trade_store()
    cfg = core_trader.get_config(st)
    market = str(body.get("market") or cfg.get("market") or "cn").lower()
    market = "us" if market.startswith("us") else "cn"
    ql = trade_quotes([code], market)
    res = core_trader.close_position(st, dict(cfg, market=market), code, ql,
                                     qty=body.get("qty"))
    order = res.get("order") if isinstance(res, dict) else None
    if order:
        _trade_publish("order", dict(order, ts=now_ms()))
        if order.get("status") == "filled":
            _trade_publish("fill", {"orderId": order.get("id"), "code": code, "side": "sell",
                                    "qty": order.get("qty"), "fillPrice": order.get("fillPrice"),
                                    "fee": order.get("fee"), "amount": order.get("amount"),
                                    "ts": now_ms()})
    if isinstance(res, dict):
        res["account"] = _trade_account_event(dict(cfg, market=market), market, ql)
        res["ok"] = True
    return res


def api_trade_reset(body):
    """重置模拟账户（不删除历史委托单：审计需要）"""
    market = str((body or {}).get("market") or "").strip().lower() or None
    st = trade_store()
    cfg = core_trader.get_config(st)
    if market:
        cfg = dict(cfg, market="us" if market.startswith("us") else "cn")
    view = core_trader.reset_account(st, cfg, cfg["market"])
    _trade_publish("account", dict(core_trader.account_view(st, cfg), ts=now_ms()))
    # 统一成 {ok, account, note}：前端按 account 字段取账户视图，
    # 不要让它去猜「这一次返回的是账户对象本身还是包了一层」
    if isinstance(view, dict) and isinstance(view.get("account"), dict):
        account = view["account"]
    else:
        account = view if isinstance(view, dict) else {}
    return {"ok": True, "account": account,
            "note": "账户已重置；历史委托单保留（审计用），可用 /api/trade/orders 查询。"}


def api_trade_ack(body):
    """外部系统回执：把委托标记为已由外部系统接手"""
    body = body or {}
    oid = str(body.get("id") or "").strip()
    if not oid:
        raise RuntimeError("缺少 id")
    order = core_trader.ack_order(trade_store(), oid,
                                  ext_ref=str(body.get("extRef") or ""),
                                  status=str(body.get("status") or "") or None)
    if order is None:
        raise RuntimeError("委托不存在：%s" % oid)
    _trade_publish("order", dict(order, ts=now_ms()))
    return {"ok": True, "order": order}


# --------------------------------------------------------------------------- #
# A股新数据（集合竞价 / 分笔 / 龙虎榜 / 涨停梯队）
# --------------------------------------------------------------------------- #

def api_feature(kind, q):
    if kind == "auction":
        code = (q.get("code") or "").strip()
        if not code:
            raise RuntimeError("缺少 code")
        return feat_provider.auction(code)
    if kind == "ticks":
        code = (q.get("code") or "").strip()
        if not code:
            raise RuntimeError("缺少 code")
        limit = int(num(q.get("limit"), 60) or 60)
        return feat_provider.ticks(code, limit=limit)
    if kind == "dragon_tiger":
        return feat_provider.dragon_tiger(q.get("date") or None)
    if kind == "limit_up":
        return feat_provider.limit_up_ladder(q.get("date") or None)
    raise RuntimeError("未知数据接口：%s" % kind)


def api_features_index():
    return {
        "items": [
            {"key": "auction", "name": "集合竞价", "desc": "09:15~09:25 委托与撮合快照（A股）"},
            {"key": "ticks", "name": "分笔成交", "desc": "当日逐笔成交明细（A股，仅当日）"},
            {"key": "dragon_tiger", "name": "龙虎榜", "desc": "当日上榜个股与席位明细"},
            {"key": "limit_up", "name": "涨停梯队", "desc": "涨停池与连板高度"},
        ],
        "note": "数据来自东方财富 / 腾讯公开接口，仅当日或近 20 个交易日有效",
    }


# --------------------------------------------------------------------------- #
# 可观测性：日志端点与运行摘要
# --------------------------------------------------------------------------- #

def api_logs(limit=100, level=None):
    r = runner()
    out = []
    try:
        out = r.logger.ring(limit=limit, level=level)
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        try:
            out = r.store.list_logs(limit=limit, min_level=level)
        except Exception:  # noqa: BLE001
            out = []
    return {"rows": out, "count": len(out), "level": level, "updated": now_ms()}


def api_sysinfo():
    r = runner()
    info = api_health()
    info.update({
        "storage": {
            "engine": "sqlite",
            "path": os.path.join(BASE_DIR, "data", "alphadesk.db"),
            "journal": safe_call(r.store.journal_mode),
            "schemaVersion": safe_call(r.store.schema_version),
            "counts": safe_call(r.store.counts) or {},
        },
        "cache": {"keys": len(_CACHE), "bytes": cache_bytes()},
        "providers": {
            "quote": "腾讯行情 → 新浪财经",
            "kline": "腾讯行情 → 东方财富",
            "market": "东方财富 → 新浪财经 → 本地快照",
            "features": "东方财富 / 腾讯（集合竞价、分笔、龙虎榜、涨停梯队）",
        },
        "notify": r.notifier.settings() if r.notifier else {},
        "uptimeSec": int(time.time() - _BOOT_TS),
    })
    return info


def safe_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:  # noqa: BLE001
        return None


def cache_bytes():
    try:
        total = 0
        for val in _CACHE.values():
            total += len(json.dumps(val[1], ensure_ascii=False, default=str))
        return total
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------- #
# 通知设置
# --------------------------------------------------------------------------- #

def api_notify_settings():
    r = runner()
    return r.notifier.settings() if r.notifier else {"enabled": False}


def api_notify_update(body):
    r = runner()
    if not r.notifier:
        raise RuntimeError("通知器未初始化")
    return r.notifier.save_settings(body or {})


def api_notify_test(body):
    r = runner()
    if not r.notifier:
        raise RuntimeError("通知器未初始化")
    url = (body or {}).get("webhook")
    return r.notifier.test(url)


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

def sanitize_json(obj):
    """把非有限浮点数（inf / -inf / NaN）替换为 None。

    Python 的 json.dumps 默认会输出 Infinity / NaN 字面量，但这不是合法 JSON：
    浏览器 JSON.parse 会直接抛错，Safari 的文案是
    "The string did not match the expected pattern."，从报错完全看不出原因。
    绩效指标里 profit_factor 在「无亏损交易」时就是 inf，所以必须在出口统一兜底。
    """
    if isinstance(obj, float):
        if obj != obj or obj == float("inf") or obj == float("-inf"):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_json(v) for v in obj]
    return obj


def _log_api_error(method, path, exc):
    """把接口异常写进结构化日志：/api/logs 能看到，否则只能靠猜。

    这个洞是实测踩到的：一个 502 响应里只有一句中文提示，服务端日志一片安静，
    排查时无法区分「参数错」「上游挂」「代码 bug」。现在连类型与堆栈末行一起记。
    """
    try:
        import traceback
        tail = traceback.format_exception_only(type(exc), exc)[-1].strip()
        runner().logger.error("api.error", method=method, path=path,
                              kind=type(exc).__name__, detail=str(exc)[:300], at=tail[:200])
    except Exception:  # noqa: BLE001
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "AlphaDesk/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        if os.environ.get("AD_VERBOSE"):
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ----------------------------------------------------------- SSE 长连接 --
    #: 心跳间隔（秒）：空闲超过这个时间就发一个注释帧，让代理与浏览器都知道连接还活着，
    #: 也让服务端能及时发现「客户端已经走了」（写失败即断开）
    SSE_HEARTBEAT = 10

    def _sse_chunk(self, payload):
        """按 HTTP/1.1 分块传输写一帧。

        本项目 protocol_version 是 HTTP/1.1，而长连接响应没有 Content-Length，因此必须
        自己分块，否则严格按 RFC 的客户端无法判断帧边界。每个 SSE 帧写成一个 chunk。
        """
        self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def _sse_send(self, event, data, eid=None, retry=None):
        lines = []
        if retry:
            lines.append("retry: %d" % int(retry))
        if eid:
            lines.append("id: %s" % eid)
        lines.append("event: %s" % event)
        # 严格 JSON：与 send_json 同一套清洗，绝不让 Infinity / NaN 漏进浏览器
        body = json.dumps(sanitize_json(data if data is not None else {}),
                          ensure_ascii=False, allow_nan=False, default=str)
        payload = ("\n".join(lines) + "\ndata: " + body + "\n\n").encode("utf-8")
        self._sse_chunk(payload)

    def _sse_comment(self, text="ping"):
        self._sse_chunk((": %s\n\n" % text).encode("utf-8"))

    def sse_stream(self, kind, q):
        """推送端点：首帧 ready → 补发重放 → 按订阅队列推送 → 空闲发心跳。

        连接的生命周期与订阅一一对应：客户端断开（写失败）或队列被关闭时立刻退订，
        不会留下「有人订阅但没人收」的僵尸通道把上游请求一直打下去。
        """
        params = core_stream.parse_params(kind, q)
        # 参数不合格时**快速失败**（400 JSON）而不是开一条只会推 error 的长连接：
        # 客户端能立刻知道自己少传了标的，审计脚本 / 桥接系统也不会被挂住的流卡死
        if kind in (core_stream.KIND_QUOTES, core_stream.KIND_ADVISOR) and not params.get("symbols"):
            return self.send_json({"error": True,
                                   "message": "请提供 symbols（逗号分隔的代码列表，"
                                              "支持 600519:贵州茅台 这种带名称写法）"}, status=400)
        try:
            hub = stream()
            sub, replay = hub.subscribe(kind, params, self.headers.get("Last-Event-ID"))
        except Exception as exc:  # noqa: BLE001  订阅失败按普通 JSON 错误返回，便于排查
            return self.send_json({"error": True, "message": str(exc)}, status=400)

        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            hub.unsubscribe(sub)
            return
        try:
            self._sse_send("ready", sub.ready, retry=3000)
            for item in replay:
                self._sse_send(item["event"], item["data"], eid=item["id"])
            while True:
                item = sub.get(timeout=self.SSE_HEARTBEAT)
                if item is None:
                    if sub.closed:
                        break
                    self._sse_comment()
                    continue
                self._sse_send(item["event"], item["data"], eid=item["id"])
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                      # 客户端走了，属正常结束
        except Exception as exc:  # noqa: BLE001  推送线程内的异常不能让服务端挂掉
            try:
                self._sse_send("error", {"message": "推送中断：%s" % str(exc)[:120]})
            except Exception:  # noqa: BLE001
                pass
        finally:
            hub.unsubscribe(sub)
            try:
                self.wfile.write(b"0\r\n\r\n")     # 分块结束标记
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass

    def send_json(self, obj, status=200):
        # 严格 JSON：先清洗非有限浮点数，再以 allow_nan=False 序列化作为兜底断言，
        # 避免 Infinity / NaN 流入浏览器导致 JSON.parse 失败（见 sanitize_json 说明）
        try:
            body = json.dumps(sanitize_json(obj), ensure_ascii=False, allow_nan=False,
                              default=str).encode("utf-8")
        except ValueError:
            body = json.dumps(sanitize_json(obj), ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def send_file(self, relpath):
        path = os.path.normpath(os.path.join(WEB_DIR, relpath.lstrip("/")))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon",
                 ".woff2": "font/woff2"}.get(os.path.splitext(path)[1],
                                             "application/octet-stream")
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path, q = parsed.path, {k: v[0] for k, v in
                                urllib.parse.parse_qs(parsed.query).items()}

        def fnum(name, default=None):
            v = q.get(name)
            if v in (None, ""):
                return default
            try:
                return float(v)
            except ValueError:
                return default

        market = (q.get("market") or "cn").lower()
        if market not in ("cn", "us"):
            market = "cn"
        code = (q.get("code") or "").strip()

        try:
            if path in ("/", "/index.html"):
                return self.send_file("index.html")
            if not path.startswith("/api/"):
                return self.send_file(path)

            if path == "/api/health":
                return self.send_json(api_health())
            if path == "/api/indices":
                return self.send_json(indices(market))
            if path == "/api/overview":
                return self.send_json(api_overview(market, fnum("size", 20)))
            if path == "/api/list":
                filters = {
                    "kw": q.get("kw"),
                    "pctMin": fnum("pctMin"), "pctMax": fnum("pctMax"),
                    "turnoverMin": fnum("turnoverMin"), "turnoverMax": fnum("turnoverMax"),
                    "vrMin": fnum("vrMin"), "vrMax": fnum("vrMax"),
                    "priceMin": fnum("priceMin"), "priceMax": fnum("priceMax"),
                    "capMin": fnum("capMin"), "capMax": fnum("capMax"),
                    "peMin": fnum("peMin"), "peMax": fnum("peMax"),
                    "minFlow": fnum("minFlow"),
                }
                return self.send_json(api_list(market, q.get("sort", "changePct"),
                                               q.get("order", "desc"), fnum("page", 1),
                                               fnum("size", 50), filters))
            if path == "/api/movers":
                return self.send_json(api_movers(market))
            if path == "/api/stock":
                return self.send_json(api_stock(market, code))
            if path == "/api/orderbook":
                st = api_stock(market, code)
                return self.send_json({
                    "code": code, "market": market, "supported": bool(st.get("bids")),
                    "bids": st.get("bids") or [], "asks": st.get("asks") or [],
                    "outer": st.get("outer"), "inner": st.get("inner"),
                    "avgPrice": st.get("avgPrice"),
                    "reason": None if st.get("bids") else "该市场暂无五档盘口数据",
                    "source": st.get("source")})
            if path == "/api/kline":
                return self.send_json(api_kline(market, code, q.get("period", "day"),
                                                int(fnum("fq", 1) or 0), fnum("limit", 320)))
            if path == "/api/trends":
                return self.send_json(api_trends(market, code, int(fnum("days", 1) or 1)))
            if path == "/api/fundflow":
                return self.send_json(api_fundflow(market, code))
            if path == "/api/quote":
                codes = [c for c in (q.get("codes") or "").split(",") if c.strip()]
                return self.send_json({"market": market, "rows": quotes(market, codes),
                                       "updated": now_ms()})
            if path == "/api/sectors":
                return self.send_json(api_sectors(q.get("kind", "industry")))
            if path == "/api/search":
                return self.send_json(api_search(q.get("q")))
            if path == "/api/symbols/resolve":
                return self.send_json(api_symbols_resolve(
                    {"market": q.get("market"), "tokens": q.get("tokens") or q.get("q")
                     or q.get("codes"), "limit": q.get("limit"), "max": q.get("max")}))
            if path == "/api/symbols/lookup":
                return self.send_json(api_symbols_lookup(q))
            if path == "/api/news":
                return self.send_json(api_news(fnum("limit", 30)))
            if path == "/api/strategy/meta":
                return self.send_json(api_strategy_meta())
            if path == "/api/strategy/overview":
                return self.send_json(api_strategy_overview())
            if path == "/api/strategy/run":
                return self.send_json(api_strategy_detail(q.get("id")))
            if path == "/api/features":
                return self.send_json(api_features_index())
            if path.startswith("/api/features/"):
                return self.send_json(api_feature(path.rsplit("/", 1)[-1], q))
            if path == "/api/stream/quotes":
                return self.sse_stream(core_stream.KIND_QUOTES, q)
            if path == "/api/stream/advisor":
                return self.sse_stream(core_stream.KIND_ADVISOR, q)
            if path == "/api/stream/trade":
                return self.sse_stream(core_stream.KIND_TRADE, q)
            if path == "/api/stream/status":
                return self.send_json(api_stream_status())
            if path == "/api/stream/test":
                return self.send_json(api_stream_test(q))
            if path == "/api/trade/config":
                return self.send_json(api_trade_config_get())
            if path == "/api/trade/account":
                return self.send_json(api_trade_account(q))
            if path == "/api/trade/orders":
                return self.send_json(api_trade_orders(q))
            if path == "/api/trade/export":
                return self.send_json(api_trade_export(q))
            if path == "/api/advisor/history":
                return self.send_json(api_advisor_history(q))
            if path == "/api/advisor/record":
                return self.send_json(api_advisor_record(q))
            if path == "/api/advisor/review":
                return self.send_json(api_advisor_review(q))
            if path == "/api/logs":
                return self.send_json(api_logs(int(fnum("limit", 100) or 100), q.get("level")))
            if path == "/api/sysinfo":
                return self.send_json(api_sysinfo())
            if path == "/api/notify":
                return self.send_json(api_notify_settings())
            return self.send_json({"error": True, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("AD_VERBOSE"):
                import traceback
                traceback.print_exc()
            _log_api_error("GET", path, exc)
            return self.send_json({"error": True, "message": str(exc)[:300]}, 502)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length).decode("utf-8", errors="ignore"))
        except Exception:  # noqa: BLE001
            body = {}
        # 确认口令可以走请求头（curl / 桥接系统更方便），也可以走 body.confirm。
        # 放在线程局部里而不是层层传参：业务函数不必知道 HTTP 细节。
        set_confirm_header(self.headers.get(core_trader.CONFIRM_HEADER))
        try:
            if path == "/api/strategy/create":
                return self.send_json(api_strategy_create(body))
            if path == "/api/strategy/action":
                return self.send_json(api_strategy_action(body))
            if path == "/api/strategy/update":
                return self.send_json(api_strategy_update(body))
            if path == "/api/backtest":
                return self.send_json(api_backtest(body))
            if path == "/api/search/params":
                return self.send_json(api_params_search(body))
            if path == "/api/symbols/resolve":
                return self.send_json(api_symbols_resolve(body))
            if path == "/api/advisor/recommend":
                return self.send_json(api_advisor_recommend(body))
            if path == "/api/advisor/delete":
                return self.send_json(api_advisor_delete(body))
            if path == "/api/advisor/note":
                return self.send_json(api_advisor_note(body))
            if path == "/api/advisor/prune":
                return self.send_json(api_advisor_prune(body))
            if path == "/api/trade/config":
                return self.send_json(api_trade_config_set(body))
            if path == "/api/trade/plan":
                return self.send_json(api_trade_plan(body))
            if path == "/api/trade/execute":
                return self.send_json(api_trade_execute(body))
            if path == "/api/trade/cancel":
                return self.send_json(api_trade_cancel(body))
            if path == "/api/trade/close":
                return self.send_json(api_trade_close(body))
            if path == "/api/trade/reset":
                return self.send_json(api_trade_reset(body))
            if path == "/api/trade/ack":
                return self.send_json(api_trade_ack(body))
            if path == "/api/notify":
                return self.send_json(api_notify_update(body))
            if path == "/api/notify/test":
                return self.send_json(api_notify_test(body))
            return self.send_json({"error": True, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:  # noqa: BLE001
            _log_api_error("POST", path, exc)
            return self.send_json({"error": True, "message": str(exc)[:300]}, 502)


def main():
    ap = argparse.ArgumentParser(description="AlphaDesk 行情数据服务")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-engine", action="store_true", help="不启动常驻策略跟踪引擎")
    args = ap.parse_args()
    ensure_ssl()
    # 服务端统一引擎：SQLite 存储 + 分层撮合/账户/绩效 + 常驻推进线程
    r = init_runner()
    tick = int(os.environ.get("AD_STRATEGY_TICK", "60") or 60)
    if not args.no_engine:
        r.start_loop(tick)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("AlphaDesk 行情服务已启动:  http://%s:%d" % (args.host, args.port))
    print("数据源: 腾讯行情 / 东方财富 / 新浪财经（公开接口，仅供参考，不构成投资建议）")
    st = r.status()
    print("策略跟踪引擎: %s，推进间隔 %d 秒，任务数 %d（SQLite: data/alphadesk.db）"
          % ("已启动" if st.get("running") else "未启动", tick, st.get("runs") or 0))
    print("日志端点: /api/logs    运行摘要: /api/sysinfo    回测: POST /api/backtest")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        r.stop_loop()
        srv.shutdown()


if __name__ == "__main__":
    main()
