#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AlphaDesk · 策略持续跟踪引擎（Paper Trading Engine）

职责：让策略在服务端「一直跑」。
  · 创建跟踪任务时，先用最近 3 个月（可配置）的历史K线做一次「回溯建仓」，立刻得到观察期内的胜率与盈亏；
  · 之后由常驻线程按固定频率（默认 60 秒）向前推进：收盘K线产生信号 → 下一根K线开盘价成交（模拟实盘执行）；
  · 全部成交、权益、信号、月度盈亏落盘到 data/strategy_runs.json，关闭浏览器后依然继续运行；
  · 输出观察期进度（已观测交易日 / 目标 90 天）、胜率、盈亏、最大回撤、盈亏比、月度分解等。

与前端 indicators.js 中的回测逻辑保持一致：信号在收盘确认，成交在下一根K线开盘，含手续费与滑点，
止损止盈在收盘价触发；区别是这里的任务是长期存活、持续累积记录。
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORE_FILE = os.path.join(DATA_DIR, "strategy_runs.json")

CN_TZ = timezone(timedelta(hours=8))
try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    NY_TZ = timezone(timedelta(hours=-4))

LOCK = threading.RLock()
RUNS = []
_LOOP = {"thread": None, "stop": False, "interval": 60, "lastTick": None, "ticks": 0}
_STORE_DIRTY = {"flag": False, "ts": 0}
_FETCH = None  # 由 server 注入：fetch_bars(market, code, period, limit) -> list[bar]
_QUOTE = None  # 由 server 注入：fetch_quote(market, code) -> dict
_MARKET_OPEN = None  # 由 server 注入：market_open(market) -> bool


# --------------------------------------------------------------------------- #
# 指标（与前端 indicators.js 保持一致）
# --------------------------------------------------------------------------- #

def _ok(v):
    return isinstance(v, (int, float)) and v == v


def SMA(values, n):
    out = [None] * len(values)
    s, cnt = 0.0, 0
    for i, v in enumerate(values):
        if _ok(v):
            s += v
            cnt += 1
        if i >= n:
            old = values[i - n]
            if _ok(old):
                s -= old
                cnt -= 1
        if i >= n - 1 and cnt == n:
            out[i] = s / n
    return out


def EMA(values, n):
    out = [None] * len(values)
    k = 2.0 / (n + 1)
    prev = None
    for i, v in enumerate(values):
        if not _ok(v):
            continue
        prev = v if prev is None else v * k + prev * (1 - k)
        out[i] = prev
    return out


def MACD(closes, fast=12, slow=26, signal=9):
    ef, es = EMA(closes, fast), EMA(closes, slow)
    dif = [None if not (_ok(ef[i]) and _ok(es[i])) else ef[i] - es[i] for i in range(len(closes))]
    valid = [v for v in dif if _ok(v)]
    dea_valid = EMA(valid, signal)
    dea = [None] * len(closes)
    k = 0
    for i, v in enumerate(dif):
        if _ok(v):
            dea[i] = dea_valid[k] if k < len(dea_valid) else None
            k += 1
    macd = [None if not (_ok(dif[i]) and _ok(dea[i])) else (dif[i] - dea[i]) * 2 for i in range(len(closes))]
    return {"dif": dif, "dea": dea, "macd": macd}


def RSI(closes, n=14):
    out = [None] * len(closes)
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


def ATR(bars, n=14):
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            tr.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return EMA(tr, n)


# --------------------------------------------------------------------------- #
# 策略（与前端一致）
# --------------------------------------------------------------------------- #

def _cross_up(a, b, i):
    return (i > 0 and _ok(a[i]) and _ok(b[i]) and _ok(a[i - 1]) and _ok(b[i - 1])
            and a[i - 1] <= b[i - 1] and a[i] > b[i])


def _cross_down(a, b, i):
    return (i > 0 and _ok(a[i]) and _ok(b[i]) and _ok(a[i - 1]) and _ok(b[i - 1])
            and a[i - 1] >= b[i - 1] and a[i] < b[i])


def _ma_cross(bars, p, i, cache):
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    f = cache.setdefault("f%s" % p["fast"], SMA(closes, int(p["fast"])))
    s = cache.setdefault("s%s" % p["slow"], SMA(closes, int(p["slow"])))
    if _cross_up(f, s, i):
        return "buy"
    if _cross_down(f, s, i):
        return "sell"
    return None


def _macd(bars, p, i, cache):
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    m = cache.setdefault("macd", MACD(closes, int(p["fast"]), int(p["slow"]), int(p["signal"])))
    if _cross_up(m["dif"], m["dea"], i):
        return "buy"
    if _cross_down(m["dif"], m["dea"], i):
        return "sell"
    return None


def _rsi(bars, p, i, cache):
    closes = cache.setdefault("closes", [b["close"] for b in bars])
    r = cache.setdefault("rsi", RSI(closes, int(p["n"])))
    if not (_ok(r[i]) and _ok(r[i - 1])):
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


STRATEGIES = {
    "maCross": {
        "name": "双均线交叉", "desc": "快线上穿慢线买入，下穿卖出",
        "params": [{"key": "fast", "label": "快线周期", "def": 5, "min": 2, "max": 120},
                   {"key": "slow", "label": "慢线周期", "def": 20, "min": 3, "max": 250}],
        "fn": _ma_cross,
    },
    "macd": {
        "name": "MACD 金叉死叉", "desc": "DIF 上穿 DEA 买入，下穿卖出",
        "params": [{"key": "fast", "label": "快线 EMA", "def": 12, "min": 3, "max": 60},
                   {"key": "slow", "label": "慢线 EMA", "def": 26, "min": 5, "max": 120},
                   {"key": "signal", "label": "信号线", "def": 9, "min": 2, "max": 40}],
        "fn": _macd,
    },
    "rsi": {
        "name": "RSI 超卖反转", "desc": "RSI 低于下限买入，高于上限卖出",
        "params": [{"key": "n", "label": "RSI 周期", "def": 14, "min": 3, "max": 40},
                   {"key": "low", "label": "买入阈值", "def": 30, "min": 5, "max": 50},
                   {"key": "high", "label": "卖出阈值", "def": 70, "min": 50, "max": 95}],
        "fn": _rsi,
    },
    "breakout": {
        "name": "N 日通道突破", "desc": "突破前 N 日高点买入，跌破 M 日低点卖出",
        "params": [{"key": "n", "label": "突破周期", "def": 20, "min": 3, "max": 120},
                   {"key": "m", "label": "止损周期", "def": 10, "min": 2, "max": 120}],
        "fn": _breakout,
    },
    "momentum": {
        "name": "动量轮动", "desc": "近 N 日涨幅超过阈值买入，动量转负卖出",
        "params": [{"key": "n", "label": "动量周期", "def": 20, "min": 3, "max": 120},
                   {"key": "th", "label": "入场阈值%", "def": 5, "min": 0.5, "max": 50}],
        "fn": _momentum,
    },
}


def strategy_meta():
    return {k: {"name": v["name"], "desc": v["desc"], "params": v["params"]} for k, v in STRATEGIES.items()}


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #

def configure(fetch_bars, fetch_quote, market_open):
    global _FETCH, _QUOTE, _MARKET_OPEN
    _FETCH, _QUOTE, _MARKET_OPEN = fetch_bars, fetch_quote, market_open


def load():
    global RUNS
    with LOCK:
        try:
            with open(STORE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            RUNS = data.get("runs") or []
        except Exception:  # noqa: BLE001
            RUNS = []
    return RUNS


def save(force=False):
    """落盘（默认 8 秒节流，避免频繁写盘）"""
    now = time.time()
    if not force and now - _STORE_DIRTY["ts"] < 8 and _STORE_DIRTY["flag"]:
        return
    with LOCK:
        payload = {"version": 1, "savedAt": now, "runs": RUNS}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, STORE_FILE)
        _STORE_DIRTY["flag"] = False
        _STORE_DIRTY["ts"] = now
    except Exception:  # noqa: BLE001
        pass


def _mark_dirty():
    _STORE_DIRTY["flag"] = True


# --------------------------------------------------------------------------- #
# 市场时间
# --------------------------------------------------------------------------- #

def today_str(market):
    d = datetime.now(NY_TZ) if market == "us" else datetime.now(CN_TZ)
    return d.strftime("%Y-%m-%d")


def market_open_real(market):
    """未注入时的本地兜底判断"""
    if market == "us":
        d = datetime.now(NY_TZ)
        if d.weekday() >= 5:
            return False
        mins = d.hour * 60 + d.minute
        return 4 * 60 <= mins < 20 * 60
    d = datetime.now(CN_TZ)
    if d.weekday() >= 5:
        return False
    mins = d.hour * 60 + d.minute
    return (9 * 60 + 15) <= mins < (11 * 60 + 30) or (13 * 60) <= mins < (15 * 60)


def is_market_open(market):
    try:
        if _MARKET_OPEN:
            return bool(_MARKET_OPEN(market))
    except Exception:  # noqa: BLE001
        pass
    return market_open_real(market)


def market_day_finished(market):
    """当日交易是否已经结束（收盘）。用于判断「今日K线」是否仍在形成中：
    午间休市时当日K线并未走完，不能当作已收盘K线来做信号判断。"""
    if market == "us":
        d = datetime.now(NY_TZ)
        return d.weekday() < 5 and (d.hour * 60 + d.minute) >= 16 * 60
    d = datetime.now(CN_TZ)
    return d.weekday() < 5 and (d.hour * 60 + d.minute) >= 15 * 60


# --------------------------------------------------------------------------- #
# 运行状态
# --------------------------------------------------------------------------- #

def new_id():
    return "s" + str(int(time.time() * 1000))[-9:] + str(int(time.time() * 7919) % 97).zfill(2)


def create_run(cfg):
    market = (cfg.get("market") or "cn").lower()
    code = str(cfg.get("code") or "").strip().upper()
    if not code:
        raise RuntimeError("缺少标的代码")
    strat = cfg.get("strategy") or "maCross"
    if strat not in STRATEGIES:
        raise RuntimeError("未知策略：%s" % strat)

    params = {}
    for pd in STRATEGIES[strat]["params"]:
        v = (cfg.get("params") or {}).get(pd["key"], pd["def"])
        try:
            params[pd["key"]] = float(v)
        except (TypeError, ValueError):
            params[pd["key"]] = float(pd["def"])

    period = cfg.get("period") or "day"
    target_days = int(cfg.get("targetDays") or 90)
    lookback = str(cfg.get("lookback") or "3m")
    start_date = cfg.get("startDate")
    if not start_date:
        days = {"1m": 30, "3m": 92, "6m": 183, "1y": 365}.get(lookback, 92)
        start_date = (datetime.now(CN_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")

    run = {
        "id": new_id(),
        "createdAt": int(time.time() * 1000),
        "createdDate": today_str(market),
        "market": market,
        "code": code,
        "name": cfg.get("name") or code,
        "strategy": strat,
        "strategyName": STRATEGIES[strat]["name"],
        "params": params,
        "period": period,
        "fq": int(cfg.get("fq", 1) or 0),
        "initial": float(cfg.get("initial") or 100000),
        "lot": int(cfg.get("lot") or (100 if market == "cn" else 1)),
        "fee": float(cfg.get("fee") if cfg.get("fee") is not None else 0.0003),
        "slippage": float(cfg.get("slippage") if cfg.get("slippage") is not None else 0.001),
        "stopLoss": float(cfg.get("stopLoss") or 0),
        "takeProfit": float(cfg.get("takeProfit") or 0),
        "targetDays": target_days,
        "startDate": start_date,
        "status": "running",
        "mode": "paper",
        "barProcessed": 0,
        "startedFromIdx": 0,
        "pending": None,
        "cash": float(cfg.get("initial") or 100000),
        "qty": 0.0,
        "entryPrice": None,
        "entryDate": None,
        "entryIdx": 0,
        "entryFee": 0.0,
        "entryPhase": None,
        "entryReason": None,
        "executedOnDate": None,
        "trades": [],
        "equity": [],
        "signals": [],
        "monthly": {},
        "skippedBuys": 0,
        "benchmarkStart": None,
        "lastPrice": None,
        "lastBarTime": None,
        "lastTick": None,
        "lastError": None,
        "tickCount": 0,
        "note": cfg.get("note") or "",
    }
    with LOCK:
        RUNS.append(run)
    save(force=True)
    # 建仓回溯：立刻得到观察期内的历史表现
    tick_run(run, backfill=True)
    return get_run(run["id"], detail=True)


def get_run(rid, detail=False):
    with LOCK:
        for r in RUNS:
            if r["id"] == rid:
                return json.loads(json.dumps(r, ensure_ascii=False)) if detail else r
    return None


def summary(run, bars=None):
    """列表页摘要（轻量，不返回全部明细）"""
    st = compute_stats(run, bars)
    return {
        "id": run["id"], "market": run["market"], "code": run["code"], "name": run["name"],
        "strategy": run["strategy"], "strategyName": run.get("strategyName"),
        "params": run["params"], "period": run["period"], "status": run["status"],
        "createdDate": run.get("createdDate"), "startDate": run.get("startDate"),
        "targetDays": run.get("targetDays"), "note": run.get("note"),
        "lastBarTime": run.get("lastBarTime"), "lastTick": run.get("lastTick"),
        "lastError": run.get("lastError"), "tickCount": run.get("tickCount"),
        "position": position_view(run),
        "stats": st,
    }


def position_view(run):
    qty = run.get("qty") or 0
    if not qty:
        return None
    last = run.get("lastPrice") or run.get("entryPrice")
    cost = run["entryPrice"] * qty * (1 + run["fee"])
    mv = (last or 0) * qty
    return {
        "qty": qty, "entryPrice": run["entryPrice"], "entryDate": run.get("entryDate"),
        "entryPhase": run.get("entryPhase") or "backfill",
        "entryReason": run.get("entryReason") or "信号",
        "pending": run.get("pending"),
        "lastPrice": last, "marketValue": mv, "cost": cost,
        "pnl": mv - cost, "pnlPct": (mv / cost - 1) * 100 if cost else 0,
        "holdBars": max(0, (run.get("barProcessed") or 0) - 1 - (run.get("entryIdx") or 0) + 1),
    }


# --------------------------------------------------------------------------- #
# 核心推进逻辑
# --------------------------------------------------------------------------- #

def tick_run(run, backfill=False):
    """推进一个任务：处理尚未消化的已收盘K线。

    行情抓取在锁外完成；所有状态变更在 LOCK 内原子完成，
    避免 API 序列化任务状态时读到写了一半的结构。
    """
    try:
        if _FETCH is None:
            raise RuntimeError("行情抓取器未注入")
        bars = _FETCH(run["market"], run["code"], run["period"], 800)
        if not bars or len(bars) < 30:
            with LOCK:
                run["lastError"] = "历史K线不足（%d 根）" % (len(bars) or 0)
            return
        # 判断最后一根K线是否仍在形成中（当日未收盘，含午间休市）
        last_date = str(bars[-1]["t"])[:10]
        incomplete = (last_date == today_str(run["market"])) and not market_day_finished(run["market"])
        complete_n = len(bars) - (1 if incomplete else 0)
        if complete_n <= 1:
            return

        with LOCK:
            if not run.get("barProcessed"):
                idx = 0
                for i, b in enumerate(bars):
                    if str(b["t"])[:10] >= run["startDate"]:
                        idx = i
                        break
                else:
                    idx = max(0, len(bars) - 2)
                run["startedFromIdx"] = idx
                run["barProcessed"] = max(1, idx)
                run["benchmarkStart"] = bars[idx]["close"]
                run["benchmarkStartDate"] = str(bars[idx]["t"])[:10]

            start_i = max(1, int(run["barProcessed"]))
            if start_i >= complete_n:
                # 没有新的已收盘K线：仍处理「今日盘中」的待执行信号与风控
                intraday_tick(run, bars, complete_n, incomplete)
                run["lastPrice"] = bars[-1]["close"]
                run["lastBarTime"] = str(bars[-1]["t"])
                run["lastTick"] = int(time.time() * 1000)
                run["tickCount"] = (run.get("tickCount") or 0) + 1
                run["lastError"] = None
                return

            cache = {}
            strat = STRATEGIES[run["strategy"]]
            for i in range(start_i, complete_n):
                bar = bars[i]
                phase = "backfill" if str(bar["t"])[:10] < run["createdDate"] else "live"
                # 1) 执行上一根K线产生的信号（本根开盘价成交）
                if run["pending"]:
                    execute(run, run["pending"], bar, phase)
                    run["pending"] = None
                    run["executedOnDate"] = str(bar["t"])[:10]
                # 2) 风控：止损 / 止盈（按收盘价）
                risk_check(run, bar, phase)
                # 3) 评估本根K线信号
                sig = None
                try:
                    sig = strat["fn"](bars, run["params"], i, cache)
                except Exception as exc:  # noqa: BLE001
                    run["lastError"] = "信号计算异常：%s" % str(exc)[:80]
                if sig:
                    push_signal(run, bar, sig, phase)
                    run["pending"] = sig
                # 4) 权益快照
                equity_snapshot(run, bar)
                run["barProcessed"] = i + 1
            run["lastBarTime"] = str(bar["t"])

        run["lastPrice"] = bars[-1]["close"]
        run["lastTick"] = int(time.time() * 1000)
        run["tickCount"] = (run.get("tickCount") or 0) + 1
        run["lastError"] = None
        # 收盘后若出现新的当日K线，再处理一次盘中待执行
        intraday_tick(run, bars, complete_n, incomplete)
        _mark_dirty()
    except Exception as exc:  # noqa: BLE001
        run["lastError"] = str(exc)[:160]
        run["lastTick"] = int(time.time() * 1000)


def intraday_tick(run, bars, complete_n, incomplete):
    """盘中处理：用当日开盘价执行挂起的信号；按盘中价做止损止盈"""
    if not incomplete or complete_n >= len(bars):
        return
    bar = bars[complete_n]           # 今日尚未收盘的K线
    bar_date = str(bar["t"])[:10]
    done_date = run.get("executedOnDate") or str(run.get("lastBarTime") or "")[:10]
    if run.get("pending") and bar_date > done_date:
        execute(run, run["pending"], bar, "live")
        run["pending"] = None
        run["executedOnDate"] = bar_date
    if run.get("qty"):
        risk_check(run, bar, "live")
    run["lastBarTime"] = str(bars[-1]["t"])


def execute(run, side, bar, phase):
    px_raw = bar["open"]
    if not px_raw or px_raw <= 0:
        return
    lot = int(run.get("lot") or (100 if run["market"] == "cn" else 1))
    if side == "buy" and not run["qty"]:
        px = px_raw * (1 + run["slippage"])
        qty = int((run["cash"] / (px * (1 + run["fee"]))) // lot) * lot
        if qty <= 0:
            need = px * lot * (1 + run["fee"])
            run["skippedBuys"] = (run.get("skippedBuys") or 0) + 1
            run["lastError"] = ("资金不足：买入 %s 股需约 %.0f，可用 %.0f，已跳过该信号"
                                % (lot, need, run["cash"]))
            run["signals"].insert(0, {
                "t": str(bar["t"]), "side": "buy", "price": bar["close"], "phase": phase,
                "hadPosition": False, "skipped": True,
                "note": "资金不足（需≥%.0f）" % need,
            })
            del run["signals"][120:]
            return
        cost = qty * px
        fee = cost * run["fee"]
        run["cash"] -= cost + fee
        run["qty"] = qty
        run["entryPrice"] = px
        run["entryDate"] = str(bar["t"])[:10]
        run["entryIdx"] = run["barProcessed"]
        run["entryFee"] = fee
        run["entryPhase"] = phase
        run["entryReason"] = "信号"
    elif side == "sell" and run["qty"]:
        px = px_raw * (1 - run["slippage"])
        gross = run["qty"] * px
        fee = gross * run["fee"]
        run["cash"] += gross - fee
        close_trade(run, px, str(bar["t"])[:10], fee, "信号", phase)
        run["qty"] = 0
        run["entryPrice"] = None
        run["entryDate"] = None
        run["entryPhase"] = None
        run["entryReason"] = None


def close_trade(run, px, out_date, exit_fee, reason, phase):
    qty = run["qty"]
    entry = run["entryPrice"] or 0
    pnl = (px - entry) * qty - exit_fee - (run.get("entryFee") or 0)
    run["trades"].append({
        "inDate": run.get("entryDate"), "inPrice": entry, "qty": qty,
        "outDate": out_date, "outPrice": px, "pnl": pnl,
        "pnlPct": (px / entry - 1) * 100 - run["fee"] * 200 if entry else 0,
        "bars": max(0, (run.get("barProcessed") or 0) - (run.get("entryIdx") or 0)),
        "reason": reason, "phase": phase,
    })
    m = out_date[:7]
    bucket = run["monthly"].setdefault(m, {"realized": 0.0, "trades": 0, "wins": 0})
    bucket["realized"] += pnl
    bucket["trades"] += 1
    if pnl > 0:
        bucket["wins"] += 1


def risk_check(run, bar, phase):
    if not run["qty"] or not run["entryPrice"]:
        return
    sl, tp = run.get("stopLoss") or 0, run.get("takeProfit") or 0
    if not sl and not tp:
        return
    chg = (bar["close"] / run["entryPrice"] - 1) * 100
    reason = None
    if sl and chg <= -abs(sl):
        reason = "止损"
    elif tp and chg >= abs(tp):
        reason = "止盈"
    if not reason:
        return
    px = bar["close"] * (1 - run["slippage"])
    gross = run["qty"] * px
    fee = gross * run["fee"]
    run["cash"] += gross - fee
    close_trade(run, px, str(bar["t"])[:10], fee, reason, phase)
    run["qty"] = 0
    run["entryPrice"] = None
    run["entryDate"] = None
    run["entryPhase"] = None
    run["entryReason"] = None
    run["pending"] = None
    run["executedOnDate"] = str(bar["t"])[:10]


def push_signal(run, bar, sig, phase):
    run["signals"].insert(0, {
        "t": str(bar["t"]), "side": "buy" if sig == "buy" else "sell",
        "price": bar["close"], "phase": phase,
        "hadPosition": bool(run["qty"]),
    })
    del run["signals"][120:]


def equity_snapshot(run, bar):
    run["equity"].append({
        "t": str(bar["t"])[:10],
        "v": run["cash"] + run["qty"] * bar["close"],
        "close": bar["close"],
        "position": bool(run["qty"]),
    })
    if len(run["equity"]) > 1500:
        run["equity"] = run["equity"][-1500:]


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def compute_stats(run, bars=None):
    trades = list(run.get("trades") or [])
    closes = [t for t in trades if t.get("outDate")]
    wins = [t for t in closes if (t.get("pnl") or 0) > 0]
    losses = [t for t in closes if (t.get("pnl") or 0) <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    realized = sum(t["pnl"] for t in closes)

    last_px = run.get("lastPrice")
    equity_now = run["cash"] + (run["qty"] * last_px if (run["qty"] and last_px) else 0)
    initial = run["initial"]
    unrealized = equity_now - initial - realized

    curve = run.get("equity") or []
    peak, max_dd = -1e18, 0.0
    for p in curve:
        v = p["v"]
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)

    bench = None
    if run.get("benchmarkStart") and last_px:
        bench = (last_px / run["benchmarkStart"] - 1) * 100

    days_observed = len(set(p["t"] for p in curve))
    target = run.get("targetDays") or 90
    ret = (equity_now / initial - 1) * 100 if initial else 0
    years = max(days_observed / 244.0, 0.02)
    try:
        annual = ((equity_now / initial) ** (1 / years) - 1) * 100 if initial > 0 and equity_now > 0 else 0
    except Exception:  # noqa: BLE001
        annual = 0

    return {
        "initial": initial,
        "equityNow": equity_now,
        "cash": run["cash"],
        "realized": realized,
        "unrealized": unrealized,
        "totalPnl": equity_now - initial,
        "returnPct": ret,
        "annualizedPct": annual,
        "maxDrawdown": max_dd,
        "trades": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "winRate": (len(wins) / len(closes) * 100) if closes else 0,
        "profitFactor": (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0),
        "avgWin": (gross_win / len(wins)) if wins else 0,
        "avgLoss": (-gross_loss / len(losses)) if losses else 0,
        "avgHoldBars": (sum(t.get("bars") or 0 for t in closes) / len(closes)) if closes else 0,
        "openPosition": bool(run["qty"]),
        "daysObserved": days_observed,
        "targetDays": target,
        "progressPct": min(100.0, (days_observed / target * 100) if target else 0),
        "remainingDays": max(0, target - days_observed),
        "benchmarkPct": bench,
        "excessPct": (ret - bench) if bench is not None else None,
        "signals": len(run.get("signals") or []),
        "skippedBuys": run.get("skippedBuys") or 0,
        "lot": run.get("lot"),
        "startDate": run.get("startDate"),
        "startedFrom": run.get("benchmarkStartDate"),
        "lastBarTime": run.get("lastBarTime"),
        "lastTick": run.get("lastTick"),
        "tickCount": run.get("tickCount"),
        "strategy": run.get("strategyName"),
        "params": run.get("params"),
    }


def monthly_table(run):
    """按自然月聚合：已实现盈亏 + 月末权益 + 当月交易数"""
    curve = run.get("equity") or []
    months = {}
    for p in curve:
        m = p["t"][:7]
        d = months.setdefault(m, {"month": m, "equityEnd": p["v"], "equityStart": p["v"], "days": 0})
        d["equityEnd"] = p["v"]
        d["days"] += 1
        if d["days"] == 1:
            d["equityStart"] = p["v"]
    for m, b in (run.get("monthly") or {}).items():
        d = months.setdefault(m, {"month": m, "equityEnd": None, "equityStart": None, "days": 0})
        d["realized"] = b["realized"]
        d["trades"] = b["trades"]
        d["wins"] = b["wins"]
    prev_equity = run["initial"]
    out = []
    for m in sorted(months.keys()):
        d = months[m]
        start = d.get("equityStart") if d.get("equityStart") is not None else prev_equity
        end = d.get("equityEnd") if d.get("equityEnd") is not None else prev_equity
        monthly_ret = (end / start - 1) * 100 if start else 0
        d.update({
            "monthlyReturnPct": monthly_ret,
            "realized": d.get("realized", 0.0),
            "trades": d.get("trades", 0),
            "wins": d.get("wins", 0),
            "winRate": (d.get("wins", 0) / d["trades"] * 100) if d.get("trades") else 0,
        })
        prev_equity = end
        out.append(d)
    return out


def api_overview():
    # 深拷贝后再统计：引擎线程可能正在追加交易/权益，避免并发修改异常
    with LOCK:
        runs = json.loads(json.dumps(RUNS, ensure_ascii=False))
    rows = []
    total_equity = 0.0
    total_initial = 0.0
    total_trades = total_wins = 0
    running = 0
    for r in runs:
        st = compute_stats(r)
        rows.append(summary(r))
        total_equity += st["equityNow"]
        total_initial += st["initial"]
        total_trades += st["trades"]
        total_wins += st["wins"]
        if r["status"] == "running":
            running += 1
    rows.sort(key=lambda x: (x["status"] != "running", -(x["stats"]["returnPct"] or 0)))
    return {
        "rows": rows,
        "totals": {
            "runs": len(rows), "running": running,
            "initial": total_initial, "equity": total_equity,
            "pnl": total_equity - total_initial,
            "returnPct": ((total_equity / total_initial - 1) * 100) if total_initial else 0,
            "trades": total_trades, "wins": total_wins,
            "winRate": (total_wins / total_trades * 100) if total_trades else 0,
        },
        "engine": engine_status(),
        "updated": int(time.time() * 1000),
    }


def api_detail(rid):
    run = get_run(rid, detail=True)
    if not run:
        return None
    return {
        "run": run,
        "stats": compute_stats(run),
        "position": position_view(run),
        "monthly": monthly_table(run),
        "trades": list(reversed(run.get("trades") or []))[:200],
        "signals": (run.get("signals") or [])[:60],
        "equity": run.get("equity") or [],
        "updated": int(time.time() * 1000),
    }


def action(rid, act):
    run = get_run(rid)
    if not run:
        raise RuntimeError("任务不存在")
    if act == "pause":
        run["status"] = "paused"
    elif act == "resume":
        run["status"] = "running"
        tick_run(run)
    elif act == "delete":
        with LOCK:
            RUNS.remove(run)
        save(force=True)
        return {"ok": True, "deleted": rid}
    elif act == "tick":
        tick_run(run)
    elif act == "reset":
        with LOCK:
            keep = {k: run[k] for k in ("id", "createdAt", "market", "code", "name", "strategy",
                                        "strategyName", "params", "period", "fq", "initial", "lot",
                                        "fee", "slippage", "stopLoss", "takeProfit", "targetDays",
                                        "startDate", "status", "mode", "note")}
        keep.update({
            "barProcessed": 0, "startedFromIdx": 0, "pending": None, "cash": run["initial"],
            "qty": 0, "entryPrice": None, "entryDate": None, "entryIdx": 0, "entryFee": 0,
            "trades": [], "equity": [], "signals": [], "monthly": {}, "benchmarkStart": None,
            "lastPrice": None, "lastBarTime": None, "lastTick": None, "lastError": None,
            "tickCount": 0, "skippedBuys": 0,
        })
        with LOCK:
            RUNS[RUNS.index(run)] = keep
        tick_run(keep, backfill=True)
    else:
        raise RuntimeError("未知操作：%s" % act)
    save(force=True)
    return {"ok": True, "id": rid, "status": run.get("status")}


# --------------------------------------------------------------------------- #
# 常驻线程
# --------------------------------------------------------------------------- #

def engine_status():
    return {
        "running": bool(_LOOP["thread"] and _LOOP["thread"].is_alive()),
        "interval": _LOOP["interval"],
        "lastTick": _LOOP["lastTick"],
        "ticks": _LOOP["ticks"],
        "runs": len(RUNS),
        "active": len([r for r in RUNS if r["status"] == "running"]),
    }


def tick_all():
    with LOCK:
        active = [r for r in RUNS if r["status"] == "running"]
    if not active:
        return 0
    def one(r):
        tick_run(r)
        return r["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, active))
    _LOOP["lastTick"] = int(time.time() * 1000)
    _LOOP["ticks"] += 1
    save()
    return len(active)


def start_loop(interval=None):
    if interval:
        _LOOP["interval"] = int(interval)
    if _LOOP["thread"] and _LOOP["thread"].is_alive():
        return _LOOP["thread"]
    load()

    def loop():
        while not _LOOP["stop"]:
            try:
                tick_all()
            except Exception:  # noqa: BLE001
                pass
            for _ in range(_LOOP["interval"]):
                if _LOOP["stop"]:
                    return
                time.sleep(1)

    th = threading.Thread(target=loop, name="alphadesk-strategy", daemon=True)
    th.start()
    _LOOP["thread"] = th
    return th


def stop_loop():
    _LOOP["stop"] = True
    save(force=True)
