# -*- coding: utf-8 -*-
"""
AlphaDesk · 策略跟踪引擎（分层版）

分层（对标调研报告 A2 项，参照 freqtrade 生命周期回调与 backtrader 的
「引擎不变、替换 Broker」范式）：

    ┌────────────┐   数据由外部注入，引擎不关心来源
    │  数据源层   │   fetch_bars / fetch_quote / fetch_orderbook
    ├────────────┤
    │  策略层     │   core/strategies.py，只产出 buy / sell 信号
    ├────────────┤
    │  撮合层     │   core/fills.py，可替换成交模型 + 实现滑点落库
    ├────────────┤
    │  账户层     │   core/portfolio.py，只管钱和券
    ├────────────┤
    │  绩效层     │   core/metrics.py，收益与成本分开核算
    ├────────────┤
    │  持久层     │   core/storage.py，SQLite 分表 + schema 版本 + 迁移
    └────────────┘

生命周期回调（回溯段、实时段、一次性回测三条路径共用同一套语义）：
    on_run_start / on_bar / on_signal / on_fill / on_exit / on_skip /
    on_run_revised / on_run_paused / on_error
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import fills as F
from . import logs as L
from . import metrics as M
from . import strategies as S
from .fills import MODELS as FILL_MODELS
from .portfolio import Account
from .storage import Store

CN_TZ = timezone(timedelta(hours=8))
try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    NY_TZ = timezone(timedelta(hours=-4))

# 调整任务时的字段分类（沿用已验证的语义）
SAFE_FIELDS = ("name", "note", "targetDays", "fee", "slippage", "stopLoss", "takeProfit",
               "participation", "metricsMode", "notify")
LOGIC_FIELDS = ("strategy", "params", "period", "fq", "initial", "lot", "startDate",
                "lookback", "fillModel")

FIELD_LABELS = {
    "name": "任务名称", "note": "备注", "targetDays": "观察目标", "fee": "手续费率",
    "slippage": "滑点", "stopLoss": "止损%", "takeProfit": "止盈%", "strategy": "策略",
    "params": "策略参数", "period": "周期", "fq": "复权方式", "initial": "初始资金",
    "lot": "最小交易单位", "startDate": "观察期起点", "lookback": "回溯窗口",
    "fillModel": "成交模型", "participation": "参与度上限", "metricsMode": "累计口径",
    "notify": "通知渠道",
}

PERIOD_LABEL = {"day": "日K", "week": "周K", "month": "月K", "1m": "1分钟",
                "5m": "5分钟", "15m": "15分钟", "30m": "30分钟", "60m": "60分钟"}

LOOKBACK_DAYS = {"1m": 30, "3m": 92, "6m": 183, "1y": 365}


def now_ms():
    return int(time.time() * 1000)


def today_str(market):
    d = datetime.now(NY_TZ) if market == "us" else datetime.now(CN_TZ)
    return d.strftime("%Y-%m-%d")


def resolve_start_date(lookback=None, start_date=None):
    if start_date:
        return str(start_date)
    days = LOOKBACK_DAYS.get(str(lookback or "3m"), 92)
    return (datetime.now(CN_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")


def market_day_finished(market):
    """当日行情是否已经走完（收盘）。午间休市不算走完，避免用半天K线判信号"""
    if market == "us":
        d = datetime.now(NY_TZ)
        return d.weekday() < 5 and (d.hour * 60 + d.minute) >= 16 * 60
    d = datetime.now(CN_TZ)
    return d.weekday() < 5 and (d.hour * 60 + d.minute) >= 15 * 60


def clip(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return max(lo, min(hi, f))



def _lag_days(processed, available):
    """「已处理到」与「最新可得K线」相差多少天；任一侧缺失或解析失败返回 None。

    用途是把「引擎是否真的在消化新K线」变成界面上看得见的数字。事故背景：游标曾静默冻结，
    `tickCount` 照涨、`lastBarTime` 照更新（由盘中分支写入），界面上完全看不出异常 ——
    四个任务全是空仓也没人知道为什么。一个能自证「推进到哪了」的字段比任何日志都直观。
    """
    try:
        a = _dt.date.fromisoformat(str(processed)[:10])
        b = _dt.date.fromisoformat(str(available)[:10])
    except (TypeError, ValueError):
        return None
    return max(0, (b - a).days)


class Runner:
    """策略跟踪引擎。数据抓取在锁外，状态变更在锁内原子完成。"""

    def __init__(self, db_path, data_dir=None, fetch_bars=None, fetch_quote=None,
                 fetch_orderbook=None, notifier=None, legacy_json=None):
        self.lock = threading.RLock()
        self.store = Store(db_path)
        self.store.init_schema()
        self.store.migrate()
        self.data_dir = data_dir
        self._fetch_bars = fetch_bars
        self._fetch_quote = fetch_quote
        self._fetch_orderbook = fetch_orderbook
        self.notifier = notifier
        self.logger = L.get_logger()
        self._loop = {"thread": None, "stop": False, "interval": 60, "lastTick": None,
                      "ticks": 0, "lastError": None}
        self._kline_cache = {}
        if legacy_json and os.path.isfile(legacy_json) and not self.store.list_runs():
            try:
                n = self.store.migrate_from_json(legacy_json)
                self.logger.info("storage.migrated", source="json", runs=n)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("storage.migrate_failed", error=str(exc)[:200])

    # ------------------------------------------------------------------ 注入

    def configure(self, fetch_bars=None, fetch_quote=None, fetch_orderbook=None):
        if fetch_bars:
            self._fetch_bars = fetch_bars
        if fetch_quote:
            self._fetch_quote = fetch_quote
        if fetch_orderbook:
            self._fetch_orderbook = fetch_orderbook
        return self

    # -------------------------------------------------------------- 回调分发

    def _emit(self, event, run, payload=None):
        """生命周期事件统一出口：结构化日志 + 通知器"""
        data = {"runId": run.get("id"), "market": run.get("market"),
                "code": run.get("code"), "name": run.get("name")}
        if payload:
            data.update(payload)
        level = "info"
        if event in ("on_error", "on_skip"):
            level = "warn"
        try:
            self.logger.log(level, event, **data)
        except Exception:  # noqa: BLE001
            pass
        if self.notifier is not None:
            try:
                self.notifier(event, run, payload or {})
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ 数据

    def fetch_bars(self, market, code, period, limit):
        if self._fetch_bars is None:
            raise RuntimeError("行情抓取器未注入")
        return self._fetch_bars(market, code, period, limit)

    def fetch_quote(self, market, code):
        if self._fetch_quote is None:
            return {}
        try:
            return self._fetch_quote(market, code) or {}
        except Exception:  # noqa: BLE001
            return {}

    def fetch_orderbook(self, market, code):
        if self._fetch_orderbook is None:
            return {}
        try:
            return self._fetch_orderbook(market, code) or {}
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------ 创建

    def create_run(self, cfg):
        cfg = cfg or {}
        market = str(cfg.get("market") or "cn").lower()
        code = str(cfg.get("code") or "").strip().upper()
        if not code:
            raise RuntimeError("缺少标的代码")
        strat = str(cfg.get("strategy") or "maCross")
        if strat not in S.STRATEGIES:
            raise RuntimeError("未知策略：%s" % strat)

        params = S.normalize_params(strat, cfg.get("params"))
        fill_model = str(cfg.get("fillModel") or F.DEFAULT_MODEL)
        if fill_model not in FILL_MODELS:
            fill_model = F.DEFAULT_MODEL
        initial = clip(cfg.get("initial") or 100000, 1000, 1e10)
        run = {
            "id": "s%d%s" % (now_ms() % 10**9, str(int(time.time() * 7919) % 97).zfill(2)),
            "createdAt": now_ms(),
            "createdDate": today_str(market),
            "market": market,
            "code": code,
            "name": str(cfg.get("name") or code)[:40],
            "strategy": strat,
            "strategyName": S.STRATEGIES[strat]["name"],
            "params": params,
            "period": str(cfg.get("period") or "day"),
            "fq": int(clip(cfg.get("fq", 1), 0, 2) or 1),
            "initial": float(initial),
            "lot": int(clip(cfg.get("lot") or (100 if market == "cn" else 1), 1, 100000)),
            "fee": float(clip(cfg.get("fee") if cfg.get("fee") is not None else 0.0003, 0, 0.02)),
            "slippage": float(clip(cfg.get("slippage") if cfg.get("slippage") is not None else 0.001, 0, 0.05)),
            "stopLoss": float(clip(cfg.get("stopLoss") or 0, 0, 50)),
            "takeProfit": float(clip(cfg.get("takeProfit") or 0, 0, 300)),
            "fillModel": fill_model,
            "participation": float(clip(cfg.get("participation") or 0.05, 0.005, 0.5)),
            "metricsMode": "simple" if str(cfg.get("metricsMode") or "compound") == "simple" else "compound",
            "notify": "",
            "targetDays": int(clip(cfg.get("targetDays") or 90, 5, 500)),
            "startDate": resolve_start_date(cfg.get("lookback"), cfg.get("startDate")),
            "status": "running",
            "mode": "paper",
            "note": str(cfg.get("note") or "")[:120],
            "revisions": [],
            "barProcessed": 0,
            "startedFromIdx": 0,
            "pending": None,
            "executedOnDate": None,
            "benchmarkStart": None,
            "benchmarkStartDate": None,
            "cash": float(initial),
            "qty": 0,
            "entryPrice": None,
            "entryDate": None,
            "entryIdx": 0,
            "entryFee": 0.0,
            "entryPhase": None,
            "executedOnDate": None,
            "feeTotal": 0.0,
            "slippageTotal": 0.0,
            "skippedBuys": 0,
            "lastPrice": None,
            "lastBarTime": None,
            "lastTick": None,
            "lastError": None,
            "tickCount": 0,
        }
        with self.lock:
            self.store.upsert_run(run)
        self._emit("on_run_start", run, {"strategy": strat, "params": params,
                                         "startDate": run["startDate"],
                                         "fillModel": fill_model})
        self.tick_run(run)
        return self.detail(run["id"])["run"]

    # ------------------------------------------------------------------ 读取

    def _account(self, run):
        acc = Account(run.get("initial") or 0, market=run.get("market") or "cn",
                      fee=run.get("fee") or 0, slippage=run.get("slippage") or 0,
                      lot=run.get("lot"), fill_model=run.get("fillModel") or F.DEFAULT_MODEL,
                      participation=run.get("participation") or 0.05)
        return acc.load_state(run)

    def _save_account(self, run, acc):
        run.update(acc.state())

    def get_run(self, rid):
        run = self.store.get_run(rid)
        return run

    def detail(self, rid):
        run = self.store.get_run(rid)
        if not run:
            return None
        trades = self.store.list_trades(rid, legacy=True)
        equity = self.store.list_equity(rid)
        signals = self.store.list_signals(rid, limit=200)
        acc = self._account(run)
        last_price = run.get("lastPrice") or acc.entry_price
        stats = self._stats(run, acc, equity, trades, last_price)
        return {
            "run": dict(run, trades=trades, equity=equity, signals=signals),
            "stats": stats,
            "position": acc.position(last_price),
            "monthly": stats.get("monthly") or [],
            "trades": list(reversed(trades)),
            "signals": signals,
            "equity": equity,
            "account": acc.state(),
            "updated": now_ms(),
        }

    def _stats(self, run, acc, equity, trades, last_price):
        # 基准：同一根K线的标的价格序列（用于 alpha / beta，与策略权益按时间对齐）
        bench = [{"t": p.get("t"), "v": p.get("close")} for p in equity if p.get("close")]
        m = M.summarize(equity, trades, run.get("initial") or 0,
                        mode=run.get("metricsMode") or "compound",
                        bench=bench or None)
        pf = m.get("profit_factor", 0.0)
        pf_infinite = (pf == float("inf"))     # 无亏损交易时盈亏比为无穷大
        days = len({p.get("t") for p in equity if p.get("t")})
        target = run.get("targetDays") or 90
        equity_now = acc.equity(last_price)
        realized = sum(t.get("pnl") or 0 for t in trades if t.get("outDate"))
        bench_pct = ((last_price / run["benchmarkStart"] - 1) * 100) if (run.get("benchmarkStart") and last_price) else None
        stats = {
            # 兼容旧字段（前端已在用）
            "initial": run.get("initial"),
            "equityNow": equity_now,
            "cash": acc.cash,
            "realized": realized,
            "unrealized": equity_now - (run.get("initial") or 0) - realized,
            "totalPnl": equity_now - (run.get("initial") or 0),
            "returnPct": (equity_now / run["initial"] - 1) * 100 if run.get("initial") else 0.0,
            "annualizedPct": m.get("annualized_return", 0.0) * 100,
            "maxDrawdown": m.get("max_drawdown", 0.0) * 100,
            "maxDrawdownDays": m.get("max_drawdown_days", 0),
            "trades": len([t for t in trades if t.get("outDate")]),
            "wins": len([t for t in trades if t.get("outDate") and (t.get("pnl") or 0) > 0]),
            "losses": len([t for t in trades if t.get("outDate") and (t.get("pnl") or 0) <= 0]),
            "winRate": m.get("win_rate", 0.0) * 100,
            "profitFactor": pf,
            "profitFactorInfinite": pf_infinite,
            "avgWin": m.get("avg_win", 0.0),
            "avgLoss": m.get("avg_loss", 0.0),
            "expectancy": m.get("expectancy", 0.0),
            "avgHoldBars": m.get("avg_hold", 0.0),
            "openPosition": bool(acc.qty),
            "daysObserved": days,
            "targetDays": target,
            "progressPct": min(100.0, days / target * 100) if target else 0.0,
            "remainingDays": max(0, target - days),
            "benchmarkPct": bench_pct,
            "excessPct": ((equity_now / run["initial"] - 1) * 100 - bench_pct) if bench_pct is not None else None,
            "signals": len([s for s in self.store.list_signals(run["id"], limit=500)]),
            "skippedBuys": acc.skipped_buys,
            "lot": run.get("lot"),
            "startDate": run.get("startDate"),
            "startedFrom": run.get("benchmarkStartDate"),
            "lastBarTime": run.get("lastBarTime"),
            "lastBarDate": run.get("lastBarDate"),
            "availableTo": str(run.get("lastBarTime") or "")[:10] or None,
            "lagDays": _lag_days(run.get("lastBarDate"), run.get("lastBarTime")),
            "stalled": bool((_lag_days(run.get("lastBarDate"), run.get("lastBarTime")) or 0) >= 5),
            "lastTick": run.get("lastTick"),
            "tickCount": run.get("tickCount"),
            "strategy": run.get("strategyName"),
            "params": run.get("params"),
            # 新增指标（调研报告 A4 项）
            "sharpe": m.get("sharpe", 0.0),
            "sortino": m.get("sortino", 0.0),
            "calmar": m.get("calmar", 0.0),
            "annualizedVol": m.get("annualized_vol", 0.0) * 100,
            "alpha": m.get("alpha", 0.0),
            "beta": m.get("beta", 0.0),
            "var95": m.get("var95", 0.0) * 100,
            "bestDay": m.get("best_day", 0.0) * 100,
            "worstDay": m.get("worst_day", 0.0) * 100,
            "exposure": m.get("exposure", 0.0) * 100,
            "feeTotal": m.get("fee_total", 0.0),
            "slippageTotal": m.get("slippage_total", 0.0),
            "costTotal": m.get("cost_total", 0.0),
            "metricsMode": run.get("metricsMode"),
            "monthly": m.get("monthly") or [],
        }
        return stats

    def overview(self, market=None):
        runs = self.store.list_runs(market=market)
        rows = []
        total_equity = total_initial = 0.0
        trades = wins = 0
        running = 0
        for run in runs:
            acc = self._account(run)
            last_price = run.get("lastPrice") or acc.entry_price
            equity = self.store.list_equity(run["id"])
            tl = self.store.list_trades(run["id"], legacy=True)
            st = self._stats(run, acc, equity, tl, last_price)
            rows.append({
                "id": run["id"], "market": run["market"], "code": run["code"],
                "name": run["name"], "strategy": run["strategy"],
                "strategyName": run.get("strategyName"), "params": run.get("params"),
                "period": run.get("period"), "status": run.get("status"),
                "createdDate": run.get("createdDate"), "startDate": run.get("startDate"),
                "targetDays": run.get("targetDays"), "note": run.get("note"),
                "lastBarTime": run.get("lastBarTime"), "lastTick": run.get("lastTick"),
                "lastBarDate": run.get("lastBarDate"),
                "availableTo": str(run.get("lastBarTime") or "")[:10] or None,
                "lagDays": _lag_days(run.get("lastBarDate"), run.get("lastBarTime")),
                "stalled": bool((_lag_days(run.get("lastBarDate"), run.get("lastBarTime")) or 0) >= 5),
                "lastError": run.get("lastError"), "tickCount": run.get("tickCount"),
                "fillModel": run.get("fillModel"), "initial": run.get("initial"),
                "position": acc.position(last_price), "stats": st,
            })
            total_equity += st["equityNow"]
            total_initial += run.get("initial") or 0
            trades += st["trades"]
            wins += st["wins"]
            if run.get("status") == "running":
                running += 1
        rows.sort(key=lambda x: (x["status"] != "running", -x["stats"]["returnPct"]))
        return {
            "rows": rows,
            "totals": {
                "runs": len(rows), "running": running,
                "initial": total_initial, "equity": total_equity,
                "pnl": total_equity - total_initial,
                "returnPct": ((total_equity / total_initial - 1) * 100) if total_initial else 0.0,
                "trades": trades, "wins": wins,
                "winRate": (wins / trades * 100) if trades else 0.0,
            },
            "engine": self.status(),
            "updated": now_ms(),
        }

    # ------------------------------------------------------------- 推进核心

    def tick_run(self, run):
        """推进一个任务：消化尚未处理的已收盘K线，并处理盘中待执行信号"""
        try:
            bars = self.fetch_bars(run["market"], run["code"], run.get("period") or "day", 800)
            if not bars or len(bars) < 30:
                with self.lock:
                    run["lastError"] = "历史K线不足（%d 根）" % (len(bars) or 0)
                    self.store.upsert_run(run)
                return
            last_date = str(bars[-1]["t"])[:10]
            incomplete = (last_date == today_str(run["market"])) and not market_day_finished(run["market"])
            complete_n = len(bars) - (1 if incomplete else 0)
            if complete_n <= 1:
                return

            orderbook = self.fetch_orderbook(run["market"], run["code"]) if incomplete else {}

            with self.lock:
                acc = self._account(run)
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
                    # 起点前一根的日期作为初始游标：重放会从 idx 那根开始
                    run["lastBarDate"] = str(bars[max(0, idx - 1)]["t"])[:10]
                    run["benchmarkStart"] = bars[idx]["close"]
                    run["benchmarkStartDate"] = str(bars[idx]["t"])[:10]
                    self.store.upsert_run(run)

                start_i = self._cursor_start(run, bars, complete_n)
                new_trades = []
                new_equity = []
                new_signals = []
                if start_i >= complete_n:
                    self._intraday(run, acc, bars, complete_n, incomplete, orderbook,
                                   new_trades, new_signals)
                    run["lastPrice"] = bars[-1]["close"]
                    run["lastBarTime"] = str(bars[-1]["t"])
                else:
                    self._replay(run, acc, bars, start_i, complete_n, [], [],
                                 new_trades, new_equity, new_signals, orderbook)
                    run["lastPrice"] = bars[-1]["close"]
                    self._intraday(run, acc, bars, complete_n, incomplete, orderbook,
                                   new_trades, new_signals)
                self._save_account(run, acc)
                run["lastTick"] = now_ms()
                run["tickCount"] = int(run.get("tickCount") or 0) + 1
                run["lastError"] = None
                self.store.upsert_run(run)
                for t in new_trades:
                    self.store.append_trade(run["id"], t)
                for p in new_equity:
                    self.store.append_equity(run["id"], p)
                for s in new_signals:
                    self.store.append_signal(run["id"], s)
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                run["lastError"] = str(exc)[:200]
                run["lastTick"] = now_ms()
                try:
                    self.store.upsert_run(run)
                except Exception:  # noqa: BLE001
                    pass
            self._emit("on_error", run, {"error": str(exc)[:200]})

    # --------------------------------------------------------- 回放（共用）

    def _cursor_start(self, run, bars, complete_n):
        """返回本轮要从第几根K线开始重放。**日期游标优先**，位置索引只作兜底。

        为什么要改成日期口径（这是一次真实事故的修复）：
        引擎每次固定取 800 根（`self.fetch_bars(..., 800)`），拿到的是一个**滑动窗口**，
        而位置索引 `barProcessed` 只在「窗口不滑、序列只增长」时才成立。事故链条：
        首次取到的根数比 800 多 1（801）→ `barProcessed` 被推到 801 → 此后窗口只有 800 根，
        `start_i(801) >= complete_n(≤800)` 永远成立 → **replay 分支再也不进入**，
        任务从此冻结在创建时的状态。而 `tickCount` 照涨、`lastBarTime` 照更新（由盘中分支写入），
        界面上看起来一切正常 —— 实测四个任务 3000+ 次 tick、一整天K线都没推进，
        用户看到的现象就是「策略跟踪里全是空仓」。

        返回值等于 `complete_n` 表示没有更新的已收盘K线（本轮只做盘中与风控）。
        """
        last_date = str(run.get("lastBarDate") or "")[:10]
        if not last_date:
            # 旧任务迁移（只有位置索引）：用「已记录事件的最晚日期」与创建日取较大者。
            # 该日期之后在冻结事故中确实从未被处理过，而重放「无事件区间」是幂等的
            # （不会凭空造出交易，也不会重复计数已有交易），所以这个迁移不会重复计数。
            cands = [str(run.get("createdDate") or "")[:10],
                     str(run.get("executedOnDate") or "")[:10]]
            for t in (run.get("trades") or []):
                cands.append(str(t.get("out_date") or "")[:10])
                cands.append(str(t.get("in_date") or "")[:10])
            last_date = max([c for c in cands if c] or [""])
            if last_date:
                run["lastBarDate"] = last_date
                run["cursorMigrated"] = True
        if last_date:
            for i in range(1, complete_n):
                if str(bars[i]["t"])[:10] > last_date:
                    return i
            return complete_n
        # 全新任务：用位置索引建立起点
        return max(1, min(int(run.get("barProcessed") or 0), complete_n))

    def _replay(self, run, acc, bars, from_idx, to_idx, _a, _b,
                out_trades, out_equity, out_signals, orderbook=None):
        """处理 [from_idx, to_idx) 区间的已收盘K线。

        顺序固定为：先执行上一根产生的挂起信号 → 风控 → 评估本根信号 → 记权益。
        与调研报告对齐 freqtrade 文档化的主循环次序。
        """
        strat = run["strategy"]
        params = run.get("params") or {}
        cache = {}
        model = run.get("fillModel") or F.DEFAULT_MODEL
        for i in range(from_idx, to_idx):
            bar = bars[i]
            date = str(bar["t"])[:10]
            phase = "backfill" if date < run.get("createdDate") else "live"
            if run.get("pending"):
                side = run["pending"]
                run["pending"] = None
                self._execute(run, acc, side, bar, i, phase, model, None, out_trades, out_signals)
                run["executedOnDate"] = date
            self._risk_check(run, acc, bar, i, phase, model, out_trades)
            sig = S.signal(strat, bars, params, i, cache)
            if sig:
                self._record_signal(run, acc, bar, sig, phase, out_signals)
                run["pending"] = sig
            acc.mark_bars(1)
            out_equity.append(self._equity_point(run, acc, bar, bar["close"]))
            run["barProcessed"] = i + 1
            # 日期游标：记录「最后一根已处理的已收盘K线」的日期。它才是推进任务的权威口径 ——
            # 位置索引 barProcessed 在固定长度滑动窗口下会指错（见 _cursor_start 的说明）
            run["lastBarDate"] = date
            run["lastBarTime"] = str(bar["t"])
            self._emit_bar(run, bar, i)

    def _emit_bar(self, run, bar, i):
        pass  # 预留：逐 bar 回调（大批量回放不做日志，避免噪声）

    def _intraday(self, run, acc, bars, complete_n, incomplete, orderbook,
                  out_trades, out_signals):
        """盘中：用当日开盘价执行挂起信号，并按盘中价做风控"""
        if not incomplete or complete_n >= len(bars):
            return
        bar = bars[complete_n]
        date = str(bar["t"])[:10]
        done = run.get("executedOnDate") or str(run.get("lastBarTime") or "")[:10]
        model = run.get("fillModel") or F.DEFAULT_MODEL
        if run.get("pending") and date > done:
            side = run["pending"]
            run["pending"] = None
            self._execute(run, acc, side, bar, complete_n, "live", model, orderbook,
                          out_trades, out_signals)
            run["executedOnDate"] = date
        if acc.qty:
            self._risk_check(run, acc, bar, complete_n, "live", model, out_trades)
        run["lastBarTime"] = str(bars[-1]["t"])

    def _execute(self, run, acc, side, bar, idx, phase, model, orderbook,
                 out_trades, out_signals):
        signal_price = bar.get("close")
        book = orderbook if model == "depthWeighted" else None
        if side == "buy":
            fill, err = acc.try_buy(bar, str(bar["t"])[:10], phase, idx,
                                    orderbook=book, signal_price=signal_price)
            if fill:
                self._emit("on_fill", run, {
                    "side": "buy", "price": fill["price"], "qty": fill["qty"],
                    "phase": phase, "model": model, "note": fill.get("note"),
                    "slippageBps": round(fill.get("bps") or 0, 2)})
            elif err:
                out_signals.insert(0, {
                    "t": str(bar["t"]), "side": "buy", "price": signal_price,
                    "phase": phase, "hadPosition": False, "skipped": True, "note": err,
                })
                self._emit("on_skip", run, {"reason": err, "side": "buy"})
        else:
            fill, err = acc.try_sell(bar, str(bar["t"])[:10], phase, idx, "信号",
                                     orderbook=book, signal_price=signal_price)
            if fill:
                out_trades.append(fill)
                self._emit("on_exit", run, {
                    "reason": "信号", "pnl": round(fill["pnl"], 2),
                    "pnlPct": round(fill["pnlPct"], 2), "phase": phase,
                    "slippageBps": round(fill.get("bps") or 0, 2)})

    def _risk_check(self, run, acc, bar, idx, phase, model, out_trades):
        if not acc.qty or not acc.entry_price:
            return
        sl, tp = run.get("stopLoss") or 0, run.get("takeProfit") or 0
        if not sl and not tp:
            return
        chg = (bar["close"] / acc.entry_price - 1) * 100
        reason = None
        if sl and chg <= -abs(sl):
            reason = "止损"
        elif tp and chg >= abs(tp):
            reason = "止盈"
        if not reason:
            return
        exit_bar = dict(bar)
        exit_bar["open"] = bar["close"]          # 风控按收盘价成交
        fill, err = acc.try_sell(exit_bar, str(bar["t"])[:10], phase, idx, reason,
                                 signal_price=bar["close"])
        if fill:
            fill["reason"] = reason
            out_trades.append(fill)
            run["pending"] = None
            run["executedOnDate"] = str(bar["t"])[:10]
            self._emit("on_exit", run, {"reason": reason, "pnl": round(fill["pnl"], 2),
                                        "pnlPct": round(fill["pnlPct"], 2), "phase": phase})

    def _record_signal(self, run, acc, bar, sig, phase, out_signals):
        item = {"t": str(bar["t"]), "side": "buy" if sig == "buy" else "sell",
                "price": bar.get("close"), "phase": phase,
                "hadPosition": bool(acc.qty)}
        out_signals.insert(0, item)
        del out_signals[200:]
        self._emit("on_signal", run, {"side": sig, "t": item["t"],
                                      "price": item["price"], "phase": phase})

    def _equity_point(self, run, acc, bar, price):
        return {"t": str(bar["t"])[:10], "v": acc.equity(price),
                "close": price, "position": bool(acc.qty)}

    # ------------------------------------------------------------- 一次性回测

    def backtest(self, cfg):
        """服务端唯一回测实现（前端不再自行计算，消除双份实现）"""
        cfg = cfg or {}
        market = str(cfg.get("market") or "cn").lower()
        code = str(cfg.get("code") or "").strip().upper()
        if not code:
            raise RuntimeError("缺少标的代码")
        strat = str(cfg.get("strategy") or "maCross")
        if strat not in S.STRATEGIES:
            raise RuntimeError("未知策略：%s" % strat)
        period = str(cfg.get("period") or "day")
        fq = int(clip(cfg.get("fq", 1), 0, 2) or 1)
        limit = int(clip(cfg.get("limit") or 800, 60, 2000))
        bars = self.fetch_bars(market, code, period, limit)
        if not bars or len(bars) < 40:
            return {"ok": False, "message": "历史数据不足（%d 根）" % (len(bars) or 0),
                    "bars": len(bars or [])}
        params = S.normalize_params(strat, cfg.get("params"))
        warm = S.warmup_bars(strat, params)
        initial = float(clip(cfg.get("initial") or 100000, 1000, 1e10))
        model = str(cfg.get("fillModel") or F.DEFAULT_MODEL)
        if model not in FILL_MODELS:
            model = F.DEFAULT_MODEL

        run = {
            "id": "bt", "createdDate": "9999-12-31", "market": market, "code": code,
            "name": cfg.get("name") or code, "strategy": strat,
            "strategyName": S.STRATEGIES[strat]["name"], "params": params,
            "period": period, "fq": fq, "initial": initial,
            "lot": int(clip(cfg.get("lot") or (100 if market == "cn" else 1), 1, 100000)),
            "fee": float(clip(cfg.get("fee") if cfg.get("fee") is not None else 0.0003, 0, 0.02)),
            "slippage": float(clip(cfg.get("slippage") if cfg.get("slippage") is not None else 0.001, 0, 0.05)),
            "stopLoss": float(clip(cfg.get("stopLoss") or 0, 0, 50)),
            "takeProfit": float(clip(cfg.get("takeProfit") or 0, 0, 300)),
            "fillModel": model,
            "participation": float(clip(cfg.get("participation") or 0.05, 0.005, 0.5)),
            "metricsMode": "simple" if str(cfg.get("metricsMode") or "compound") == "simple" else "compound",
            "pending": None, "barProcessed": 0, "lastBarDate": None, "startedFromIdx": 0,
            "executedOnDate": None, "qty": 0,
        }
        acc = Account(initial, market=market, fee=run["fee"], slippage=run["slippage"],
                      lot=run["lot"], fill_model=model, participation=run["participation"])
        from_idx = max(1, warm)
        sd = cfg.get("startDate")
        if sd:
            # 与跟踪任务对齐：指标仍用完整历史预热，交易从指定日期开始
            for i, b in enumerate(bars):
                if str(b["t"])[:10] >= str(sd):
                    from_idx = max(from_idx, i)
                    break
        trades, equity, signals = [], [], []
        self._replay(run, acc, bars, from_idx, len(bars), [], [], trades, equity, signals)
        last_price = bars[-1]["close"]
        if acc.qty:
            fill, _ = acc.try_sell(dict(bars[-1], open=last_price), str(bars[-1]["t"])[:10],
                                   "backfill", len(bars) - 1, "期末平仓",
                                   signal_price=last_price)
            if fill:
                trades.append(fill)
        stats = self._stats(run, acc, equity, trades, last_price)
        stats["barCount"] = len(bars)
        stats["warmupBars"] = warm
        last_close = bars[-1]["close"] or 0
        min_capital = last_close * (run["lot"] or 1) * 1.01
        return {
            "ok": True, "market": market, "code": code, "name": run["name"],
            "strategy": strat, "strategyName": run["strategyName"], "params": params,
            "period": period, "fillModel": model, "metricsMode": run["metricsMode"],
            "initial": initial, "minCapital": round(min_capital, 2),
            "skippedBuys": acc.skipped_buys, "lastPrice": last_close,
            "cost": {"fee": run["fee"], "slippage": run["slippage"],
                     "participation": run["participation"],
                     "stopLoss": run["stopLoss"],
                     "takeProfit": run["takeProfit"], "lot": run["lot"]},
            "stats": stats, "trades": list(reversed(trades)), "equity": equity,
            "signals": signals, "bars": len(bars),
            "marks": [{"t": s["t"], "dir": s["side"]} for s in reversed(signals)],
            "source": self._source_hint(market, code, period),
            "updated": now_ms(),
        }

    def _source_hint(self, market, code, period):
        try:
            bars = self.fetch_bars(market, code, period, 60)
            return "服务端统一回测（数据由本地行情服务提供，%d 根样本）" % len(bars or [])
        except Exception:  # noqa: BLE001
            return "服务端统一回测"

    # --------------------------------------------------------------- 参数寻优

    def grid_search(self, cfg):
        """网格寻优：复用同一回测实现，按指定指标排名（调研报告 C4 项）"""
        cfg = cfg or {}
        strat = str(cfg.get("strategy") or "maCross")
        if strat not in S.STRATEGIES:
            raise RuntimeError("未知策略：%s" % strat)
        space = cfg.get("space") or {}
        combos = [{}]
        for p in S.STRATEGIES[strat]["params"]:
            key = p["key"]
            spec = space.get(key) or {}
            if spec.get("enabled") is False:
                vals = [p["def"]]
            else:
                lo = clip(spec.get("min", p["min"]), p["min"], p["max"]) or p["min"]
                hi = clip(spec.get("max", p["max"]), p["min"], p["max"]) or p["max"]
                step = clip(spec.get("step", p.get("step", 1)), 0.01, 1e6) or p.get("step", 1)
                if lo > hi:
                    lo, hi = hi, lo
                n = int((hi - lo) / step) + 1
                if n > 12:
                    step = (hi - lo) / 11.0
                    n = 12
                vals = []
                v = lo
                while v <= hi + 1e-9 and len(vals) < 12:
                    vals.append(round(v, 4))
                    v += step
                if not vals:
                    vals = [p["def"]]
            combos = [dict(c, **{key: val}) for c in combos for val in vals]
        cap = int(clip(cfg.get("maxCombos") or 64, 4, 240))
        combos = combos[:cap]
        metric = str(cfg.get("metric") or "sharpe")
        results = []
        for params in combos:
            item = dict(cfg)
            item["params"] = params
            try:
                res = self.backtest(item)
            except Exception as exc:  # noqa: BLE001
                results.append({"params": params, "ok": False, "error": str(exc)[:120]})
                continue
            if not res.get("ok"):
                results.append({"params": params, "ok": False, "error": res.get("message")})
                continue
            st = res["stats"]
            results.append({
                "params": params, "ok": True,
                "totalReturn": (st.get("returnPct") or 0.0) / 100.0,
                "returnPct": st["returnPct"], "maxDrawdown": st["maxDrawdown"],
                "sharpe": st["sharpe"], "calmar": st["calmar"], "winRate": st["winRate"],
                "trades": st["trades"], "profitFactor": st["profitFactor"],
                "costTotal": st["costTotal"], "annualizedPct": st["annualizedPct"],
                "score": st.get(metric, 0.0),
            })
        ranked = sorted([r for r in results if r.get("ok")],
                        key=lambda x: (x.get("score") or 0), reverse=True)
        failed = [r for r in results if not r.get("ok")]
        return {
            "ok": True, "strategy": strat, "metric": metric,
            "combos": len(combos), "tested": len(results),
            "best": ranked[0] if ranked else None,
            "rows": ranked[:30], "failed": failed[:5],
            "space": {p["key"]: space.get(p["key"]) for p in S.STRATEGIES[strat]["params"]},
            "updated": now_ms(),
        }

    # ------------------------------------------------------------------ 调整

    def _apply_safe(self, run, key, value, changes):
        if key in ("name", "note"):
            v = str(value or "").strip()[:40 if key == "name" else 120]
            if key == "name" and not v:
                v = run["code"]
            if v != (run.get(key) or ""):
                changes[key] = [run.get(key), v]
                run[key] = v
        elif key == "targetDays":
            v = clip(value, 5, 500)
            if v is None:
                raise RuntimeError("观察目标需为 5 ~ 500 之间的交易日数")
            v = int(v)
            if v != run.get("targetDays"):
                changes[key] = [run.get("targetDays"), v]
                run[key] = v
        elif key in ("fee", "slippage", "stopLoss", "takeProfit", "participation"):
            limits = {"fee": (0, 0.02, "手续费率需为 0 ~ 0.02"), "slippage": (0, 0.05, "滑点需为 0 ~ 0.05"),
                      "stopLoss": (0, 50, "止损需为 0 ~ 50"), "takeProfit": (0, 300, "止盈需为 0 ~ 300"),
                      "participation": (0.005, 0.5, "参与度需为 0.005 ~ 0.5")}
            lo, hi, msg = limits[key]
            v = clip(value, lo, hi)
            if v is None:
                raise RuntimeError(msg)
            if abs(v - (run.get(key) or 0)) > 1e-9:
                changes[key] = [run.get(key), v]
                run[key] = v
        elif key == "metricsMode":
            v = "simple" if str(value) == "simple" else "compound"
            if v != run.get("metricsMode"):
                changes[key] = [run.get("metricsMode"), v]
                run[key] = v
        elif key == "notify":
            v = str(value or "")[:200]
            if v != (run.get("notify") or ""):
                changes[key] = [run.get("notify"), v]
                run[key] = v

    def _apply_logic(self, run, key, value, changes):
        if key == "strategy":
            v = str(value)
            if v not in S.STRATEGIES:
                raise RuntimeError("未知策略：%s" % v)
            if v != run.get("strategy"):
                changes[key] = [run.get("strategy"), v]
                run["strategy"] = v
                run["strategyName"] = S.STRATEGIES[v]["name"]
                defaults = S.default_params(v)
                changes["params"] = [run.get("params"), defaults]
                run["params"] = defaults
        elif key == "params":
            parsed = S.normalize_params(run["strategy"], value)
            if parsed != run.get("params"):
                changes["params"] = [run.get("params"), parsed]
                run["params"] = parsed
        elif key == "period":
            v = str(value)
            if v not in PERIOD_LABEL:
                raise RuntimeError("不支持的周期：%s" % v)
            if v != run.get("period"):
                changes[key] = [run.get("period"), v]
                run[key] = v
        elif key == "fq":
            v = int(clip(value, 0, 2) or 1)
            if v != (run.get("fq") if run.get("fq") is not None else 1):
                changes[key] = [run.get("fq"), v]
                run[key] = v
        elif key == "initial":
            v = clip(value, 1000, 1e10)
            if v is None:
                raise RuntimeError("初始资金需为 1000 以上")
            if abs(v - (run.get("initial") or 0)) > 1e-6:
                changes[key] = [run.get("initial"), v]
                run["initial"] = v
        elif key == "lot":
            v = int(clip(value, 1, 100000) or 1)
            if v != run.get("lot"):
                changes[key] = [run.get("lot"), v]
                run[key] = v
        elif key == "fillModel":
            v = str(value)
            if v not in FILL_MODELS:
                raise RuntimeError("未知成交模型：%s" % v)
            if v != run.get("fillModel"):
                changes[key] = [run.get("fillModel"), v]
                run[key] = v
        elif key == "startDate":
            v = resolve_start_date(None, value)
            if v != run.get("startDate"):
                changes["startDate"] = [run.get("startDate"), v]
                run["startDate"] = v
        elif key == "lookback":
            v = resolve_start_date(value, None)
            if v != run.get("startDate"):
                changes["startDate"] = [run.get("startDate"), v]
                run["startDate"] = v

    def _reset_records(self, run):
        run.update({
            "barProcessed": 0, "lastBarDate": None, "startedFromIdx": 0, "pending": None, "cash": run["initial"],
            "qty": 0, "entryPrice": None, "entryDate": None, "entryIdx": 0, "entryFee": 0,
            "entryPhase": None, "executedOnDate": None, "feeTotal": 0.0, "slippageTotal": 0.0,
            "skippedBuys": 0, "benchmarkStart": None, "benchmarkStartDate": None,
            "lastPrice": None, "lastBarTime": None, "lastTick": None, "lastError": None,
            "tickCount": 0,
        })
        self.store.replace_trades(run["id"], [])
        self.store.replace_equity(run["id"], [])
        self.store.replace_signals(run["id"], [])

    def revise(self, rid, patch, reset=False):
        run = self.store.get_run(rid)
        if not run:
            raise RuntimeError("任务不存在")
        patch = patch or {}
        logic_keys = [k for k in LOGIC_FIELDS if k in patch]
        safe_keys = [k for k in SAFE_FIELDS if k in patch]
        if not logic_keys and not safe_keys:
            raise RuntimeError("没有需要调整的字段")

        with self.lock:
            draft = json.loads(json.dumps(run, ensure_ascii=False))
            changes = {}
            for k in logic_keys:
                self._apply_logic(draft, k, patch[k], changes)
            logic_changed = bool(changes)
            if logic_keys and not reset and logic_changed:
                raise RuntimeError("修改「%s」会改变统计口径，请勾选「重置并重新回溯」后提交"
                                   % "、".join(FIELD_LABELS.get(k, k) for k in logic_keys))
            for k in safe_keys:
                self._apply_safe(draft, k, patch[k], changes)
            if logic_changed:
                self._reset_records(draft)
            if changes:
                draft.setdefault("revisions", []).insert(0, {
                    "ts": now_ms(), "reset": bool(logic_changed),
                    "fields": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()},
                })
                del draft["revisions"][30:]
            run.clear()
            run.update(draft)
            self.store.upsert_run(run)
        self._emit("on_run_revised", run, {"changed": sorted(changes.keys()),
                                           "reset": bool(logic_changed)})
        if logic_changed:
            self.tick_run(run)
        return {"ok": True, "id": rid, "reset": bool(logic_changed),
                "changed": sorted(changes.keys()), "run": self.detail(rid)["run"]}

    def action(self, rid, act):
        run = self.store.get_run(rid)
        if not run:
            raise RuntimeError("任务不存在")
        if act == "pause":
            with self.lock:
                run["status"] = "paused"
                self.store.upsert_run(run)
            self._emit("on_run_paused", run, {})
        elif act == "resume":
            with self.lock:
                run["status"] = "running"
                self.store.upsert_run(run)
            self.tick_run(run)
        elif act == "delete":
            with self.lock:
                self.store.delete_run(rid)
            self.logger.info("run.deleted", runId=rid, code=run.get("code"))
            return {"ok": True, "deleted": rid}
        elif act == "tick":
            self.tick_run(run)
        elif act == "reset":
            with self.lock:
                self._reset_records(run)
                self.store.upsert_run(run)
            self.tick_run(run)
        else:
            raise RuntimeError("未知操作：%s" % act)
        fresh = self.store.get_run(rid) or run
        return {"ok": True, "id": rid, "status": fresh.get("status")}

    # ------------------------------------------------------------- 常驻线程

    def status(self):
        th = self._loop.get("thread")
        db = {}
        try:
            db = self.store.counts()
        except Exception:  # noqa: BLE001
            pass
        return {
            "running": bool(th and th.is_alive()),
            "interval": self._loop.get("interval"),
            "lastTick": self._loop.get("lastTick"),
            "ticks": self._loop.get("ticks"),
            "lastError": self._loop.get("lastError"),
            "runs": db.get("runs", 0),
            "active": len([r for r in self.store.list_runs() if r.get("status") == "running"]),
            "storage": "sqlite",
            "counts": db,
        }

    def tick_all(self):
        runs = [r for r in self.store.list_runs() if r.get("status") == "running"]
        if not runs:
            return 0

        def one(r):
            self.tick_run(r)
            return r["id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, runs))
        self._loop["lastTick"] = now_ms()
        self._loop["ticks"] = int(self._loop.get("ticks") or 0) + 1
        return len(runs)

    def start_loop(self, interval=None):
        if interval:
            self._loop["interval"] = int(interval)
        th = self._loop.get("thread")
        if th and th.is_alive():
            return th

        def loop():
            while not self._loop.get("stop"):
                try:
                    self.tick_all()
                    self._loop["lastError"] = None
                except Exception as exc:  # noqa: BLE001
                    self._loop["lastError"] = str(exc)[:200]
                    self.logger.error("engine.tick_failed", error=str(exc)[:200])
                for _ in range(int(self._loop.get("interval") or 60)):
                    if self._loop.get("stop"):
                        return
                    time.sleep(1)

        th = threading.Thread(target=loop, name="alphadesk-runner", daemon=True)
        th.start()
        self._loop["thread"] = th
        self.logger.info("engine.started", interval=self._loop.get("interval"))
        return th

    def stop_loop(self):
        self._loop["stop"] = True
        self.logger.info("engine.stopped")

    # ------------------------------------------------------------ 一次性维护

    def backfill_costs(self):
        """为旧版迁移数据补算逐笔成本。

        旧引擎（v1）只在账户层累计费用，没有逐笔记录 fee / slippage，
        迁移后会让成本口径显示为 0。这里按当时的费率与滑点参数反推，
        并打上 costEstimated 标记，避免把推算值当成实测值。
        """
        try:
            if self.store.meta_get("cost_backfill_v1"):
                return 0
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for run in self.store.list_runs():
            fee_rate = float(run.get("fee") or 0)
            slip = float(run.get("slippage") or 0)
            if not fee_rate and not slip:
                continue
            try:
                trades = self.store.list_trades(run["id"], legacy=True)
            except Exception:  # noqa: BLE001
                continue
            changed = []
            for t in trades:
                if (t.get("fee") or 0) != 0 or (t.get("slippage") or 0) != 0:
                    continue
                qty = abs(float(t.get("qty") or 0))
                inp = float(t.get("inPrice") or 0)
                outp = float(t.get("outPrice") or 0)
                if not qty or not inp or not outp:
                    continue
                t["fee"] = round((inp + outp) * qty * fee_rate, 4)
                t["slippage"] = round((inp + outp) * qty * slip, 4)
                t["costEstimated"] = True
                changed.append(t)
                n += 1
            if changed:
                try:
                    self.store.replace_trades(run["id"], changed)
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("cost.backfill_failed", runId=run["id"], error=str(exc)[:120])
        self.store.meta_set("cost_backfill_v1", {"ts": now_ms(), "trades": n})
        if n:
            self.logger.info("cost.backfilled", trades=n)
        return n


__all__ = ["Runner", "SAFE_FIELDS", "LOGIC_FIELDS", "FIELD_LABELS", "PERIOD_LABEL",
           "LOOKBACK_DAYS", "resolve_start_date", "market_day_finished", "today_str"]
