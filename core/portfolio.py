# -*- coding: utf-8 -*-
"""
AlphaDesk · 账户与持仓

职责边界（对标调研 A2 项）：本模块只管钱和券，不产出信号、不关心策略，
也不决定成交价（成交价由 core/fills.py 给出）。这样撮合模型、风控规则、
绩效口径都能各自独立替换。

成本口径对齐 qlib（调研报告 A4 项）：收益与成本分开记账，每笔成交同时
落库 fee 与 slippage 金额，便于回答「收益里有多少被交易成本吃掉」。
"""

from __future__ import annotations

from . import fills as F


class Account:
    """单标的、全仓操作的模拟账户"""

    def __init__(self, initial, market="cn", fee=0.0003, slippage=0.001, lot=None,
                 fill_model=F.DEFAULT_MODEL, participation=0.05):
        self.initial = float(initial or 0)
        self.market = market
        self.fee_rate = float(fee or 0)
        self.slippage = float(slippage or 0)
        self.lot = int(lot or (100 if market == "cn" else 1))
        self.fill_model = fill_model if fill_model in F.MODELS else F.DEFAULT_MODEL
        self.participation = float(participation or 0.05)

        self.cash = self.initial
        self.qty = 0
        self.entry_price = None
        self.entry_date = None
        self.entry_idx = 0
        self.entry_fee = 0.0
        self.entry_phase = None
        self.entry_signal_price = None
        self.entry_fill_note = None
        self.entry_bars = 0

        self.fee_total = 0.0
        self.slippage_total = 0.0
        self.skipped_buys = 0
        self.last_error = None

    # ------------------------------------------------------------------ 状态

    def state(self):
        return {
            "cash": self.cash, "qty": self.qty, "entryPrice": self.entry_price,
            "entryDate": self.entry_date, "entryIdx": self.entry_idx,
            "entryFee": self.entry_fee, "entryPhase": self.entry_phase,
            "feeTotal": self.fee_total, "slippageTotal": self.slippage_total,
            "skippedBuys": self.skipped_buys, "lastError": self.last_error,
        }

    def load_state(self, state):
        s = state or {}
        self.cash = float(s.get("cash", self.initial))
        self.qty = int(s.get("qty") or 0)
        self.entry_price = s.get("entryPrice")
        self.entry_date = s.get("entryDate")
        self.entry_idx = int(s.get("entryIdx") or 0)
        self.entry_fee = float(s.get("entryFee") or 0)
        self.entry_phase = s.get("entryPhase")
        self.fee_total = float(s.get("feeTotal") or 0)
        self.slippage_total = float(s.get("slippageTotal") or 0)
        self.skipped_buys = int(s.get("skippedBuys") or 0)
        return self

    def equity(self, price):
        px = price if price else (self.entry_price or 0)
        return self.cash + self.qty * (px or 0)

    def position(self, price):
        if not self.qty:
            return None
        last = price or self.entry_price
        cost = (self.entry_price or 0) * self.qty + self.entry_fee
        mv = (last or 0) * self.qty
        return {
            "qty": self.qty, "entryPrice": self.entry_price, "entryDate": self.entry_date,
            "entryPhase": self.entry_phase or "backfill",
            "lastPrice": last, "marketValue": mv, "cost": cost,
            "pnl": mv - cost, "pnlPct": ((mv / cost - 1) * 100) if cost else 0.0,
            "holdBars": max(0, self.entry_bars),
        }

    # ------------------------------------------------------------------ 委托

    def size_for(self, price):
        """按可用资金计算可买数量（不足一手返回 0）"""
        if not price or price <= 0:
            return 0
        raw = self.cash / (price * (1 + self.fee_rate))
        return int(raw // self.lot) * self.lot

    def try_buy(self, bar, date, phase, idx, bars_held=0, orderbook=None, signal_price=None):
        """尝试买入；返回 (成交记录 or None, 失败原因 or None)"""
        if self.qty:
            return None, "已有持仓"
        notional_hint = self.cash
        ctx = self._ctx(notional_hint, orderbook)
        res = F.fill_price(self.fill_model, "buy", bar, ctx)
        px = res.get("price")
        if not px:
            self.last_error = "无有效成交价"
            return None, self.last_error
        qty = self.size_for(px)
        if qty <= 0:
            need = px * self.lot * (1 + self.fee_rate)
            self.skipped_buys += 1
            self.last_error = "资金不足：买入 %d 股需约 %.0f，可用 %.0f" % (self.lot, need, self.cash)
            return None, self.last_error

        cost = qty * px
        fee = cost * self.fee_rate
        slip_amount = abs(px - (res.get("base") or px)) * qty
        self.cash -= cost + fee
        self.fee_total += fee
        self.slippage_total += slip_amount
        self.qty = qty
        self.entry_price = px
        self.entry_date = date
        self.entry_idx = idx
        self.entry_fee = fee
        self.entry_phase = phase
        self.entry_signal_price = signal_price if signal_price is not None else (res.get("base") or px)
        self.entry_fill_note = res.get("note")
        self.entry_bars = 0
        self.last_error = None
        return {
            "side": "buy", "date": date, "price": px, "qty": qty, "fee": fee,
            "slippage": slip_amount, "phase": phase, "signalPrice": self.entry_signal_price,
            "fillPrice": px, "model": res.get("model"), "note": res.get("note"),
            "bps": res.get("bps"),
        }, None

    def try_sell(self, bar, date, phase, idx, reason="信号", orderbook=None, signal_price=None):
        """尝试卖出；返回 (平仓成交记录 or None, 失败原因 or None)"""
        if not self.qty:
            return None, "无持仓"
        entry = self.entry_price or 0
        notional_hint = entry * self.qty
        ctx = self._ctx(notional_hint, orderbook)
        res = F.fill_price(self.fill_model, "sell", bar, ctx)
        px = res.get("price")
        if not px:
            self.last_error = "无有效成交价"
            return None, self.last_error

        gross = self.qty * px
        fee = gross * self.fee_rate
        slip_amount = abs(px - (res.get("base") or px)) * self.qty
        self.cash += gross - fee
        self.fee_total += fee
        self.slippage_total += slip_amount

        qty = self.qty
        pnl = (px - entry) * qty - fee - (self.entry_fee or 0)
        bars = max(0, idx - (self.entry_idx or 0))
        trade = {
            "inDate": self.entry_date, "inPrice": entry, "qty": qty,
            "outDate": date, "outPrice": px, "pnl": pnl,
            "pnlPct": ((px / entry - 1) * 100 - self.fee_rate * 200) if entry else 0.0,
            "bars": bars, "reason": reason, "phase": phase,
            "signalPrice": signal_price if signal_price is not None else (res.get("base") or px),
            "fillPrice": px, "fee": fee + (self.entry_fee or 0),
            "slippage": slip_amount, "model": res.get("model"),
            "note": res.get("note"), "bps": res.get("bps"),
        }
        self.qty = 0
        self.entry_price = None
        self.entry_date = None
        self.entry_phase = None
        self.entry_fee = 0.0
        self.entry_signal_price = None
        self.entry_fill_note = None
        self.entry_bars = 0
        self.last_error = None
        return trade, None

    def mark_bars(self, n=1):
        if self.qty:
            self.entry_bars = max(0, self.entry_bars + n)

    # ------------------------------------------------------------------ 内部

    def _ctx(self, notional, orderbook):
        return {
            "slippage": self.slippage,
            "market": self.market,
            "notional": notional,
            "participation": self.participation,
            "orderbook": orderbook or {},
        }


__all__ = ["Account"]
