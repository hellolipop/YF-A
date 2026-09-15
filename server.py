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

import engine as strategy_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

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
        if not rows and q.upper().isalpha() and len(q) <= 6:
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
            "tz": "Asia/Shanghai", "version": "1.1",
            "engine": strategy_engine.engine_status()}


# --------------------------------------------------------------------------- #
# 策略持续跟踪（Paper Trading）—— 引擎在 server 启动时常驻运行
# --------------------------------------------------------------------------- #

def _strategy_fetch_bars(market, code, period, limit):
    res = api_kline(market, code, period or "day", 1, limit or 800)
    return res.get("bars") or []


def _strategy_fetch_quote(market, code):
    try:
        rows = quotes(market, [code])
        return rows[0] if rows else {}
    except Exception:  # noqa: BLE001
        return {}


def api_strategy_meta():
    return {
        "strategies": strategy_engine.strategy_meta(),
        "periods": [
            {"value": "day", "label": "日K"}, {"value": "week", "label": "周K"},
            {"value": "60m", "label": "60分钟"}, {"value": "30m", "label": "30分钟"},
            {"value": "15m", "label": "15分钟"}, {"value": "5m", "label": "5分钟"},
        ],
        "windows": [
            {"value": "1m", "label": "近 1 个月"}, {"value": "3m", "label": "近 3 个月（推荐）"},
            {"value": "6m", "label": "近 6 个月"}, {"value": "1y", "label": "近 1 年"},
        ],
        "targets": [30, 60, 90, 180],
        "editable": {
            "safe": list(strategy_engine.SAFE_FIELDS),
            "logic": list(strategy_engine.LOGIC_FIELDS),
            "labels": strategy_engine.FIELD_LABELS,
        },
        "engine": strategy_engine.engine_status(),
        "store": strategy_engine.STORE_FILE,
    }


def api_strategy_overview():
    return strategy_engine.api_overview()


def api_strategy_detail(rid):
    data = strategy_engine.api_detail(rid)
    if not data:
        raise RuntimeError("任务不存在：%s" % rid)
    return data


def api_strategy_create(body):
    run = strategy_engine.create_run(body or {})
    price, min_cap = None, None
    try:
        q = _strategy_fetch_quote(run["market"], run["code"])
        price = q.get("price")
        lot = run.get("lot") or 100
        if price:
            min_cap = price * lot * 1.01
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "run": run, "price": price, "minCapital": min_cap}


def api_strategy_action(body):
    body = body or {}
    rid = body.get("id")
    act = body.get("action")
    if not rid or not act:
        raise RuntimeError("缺少 id 或 action")
    return strategy_engine.action(rid, act)


def api_strategy_update(body):
    """在详情里调整跟踪任务（安全字段即时生效；策略/参数/资金等需 reset=True 重新回溯）"""
    body = body or {}
    rid = body.get("id")
    if not rid:
        raise RuntimeError("缺少 id")
    patch = body.get("patch") or {}
    if not isinstance(patch, dict) or not patch:
        raise RuntimeError("缺少需要调整的字段")
    res = strategy_engine.revise_run(rid, patch, bool(body.get("reset")))
    price, min_cap = None, None
    try:
        run = res.get("run") or {}
        q = _strategy_fetch_quote(run.get("market") or "cn", run.get("code"))
        price = q.get("price")
        lot = run.get("lot") or 100
        if price:
            min_cap = price * lot * 1.01
    except Exception:  # noqa: BLE001
        pass
    res["price"] = price
    res["minCapital"] = min_cap
    return res



# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "AlphaDesk/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        if os.environ.get("AD_VERBOSE"):
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
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
            if path == "/api/news":
                return self.send_json(api_news(fnum("limit", 30)))
            if path == "/api/strategy/meta":
                return self.send_json(api_strategy_meta())
            if path == "/api/strategy/overview":
                return self.send_json(api_strategy_overview())
            if path == "/api/strategy/run":
                return self.send_json(api_strategy_detail(q.get("id")))
            return self.send_json({"error": True, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("AD_VERBOSE"):
                import traceback
                traceback.print_exc()
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
        try:
            if path == "/api/strategy/create":
                return self.send_json(api_strategy_create(body))
            if path == "/api/strategy/action":
                return self.send_json(api_strategy_action(body))
            if path == "/api/strategy/update":
                return self.send_json(api_strategy_update(body))
            return self.send_json({"error": True, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": True, "message": str(exc)[:300]}, 502)


def main():
    ap = argparse.ArgumentParser(description="AlphaDesk 行情数据服务")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    ensure_ssl()
    # 策略持续跟踪引擎：注入行情抓取器并启动常驻线程（关闭浏览器后仍继续运行）
    strategy_engine.configure(_strategy_fetch_bars, _strategy_fetch_quote, None)
    tick = int(os.environ.get("AD_STRATEGY_TICK", "60") or 60)
    strategy_engine.start_loop(tick)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("AlphaDesk 行情服务已启动:  http://%s:%d" % (args.host, args.port))
    print("数据源: 腾讯行情 / 东方财富 / 新浪财经（公开接口，仅供参考，不构成投资建议）")
    print("策略跟踪引擎: 已启动，推进间隔 %d 秒，任务文件 %s"
          % (tick, os.path.join(BASE_DIR, "data", "strategy_runs.json")))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        strategy_engine.stop_loop()
        srv.shutdown()


if __name__ == "__main__":
    main()
