# -*- coding: utf-8 -*-
"""
AlphaDesk · 成交模型（可插拔）

对标结论（调研报告 A5 项）：单一滑点参数无法解释模拟与真实的偏差来源，
真实偏差来自「盘口深度 + 排队 + 执行延迟」的叠加。因此把成交价的计算
从推进逻辑中抽出来做成可替换模型，并统一记录：
  signalPrice  信号价（信号所在K线的收盘价）
  fillPrice    成交价（模型算出）
  slippageBps  两者差额（基点），落库后可直接与「账户级滑点参数」对比

模型一览：
  nextOpen       默认。下一根K线开盘价成交，按 slippage 比例让价（与历史行为一致）
  closeFill      当根收盘价成交，按 slippage 让价（信号即时执行，用于分钟级实验）
  fixedBps       下一根开盘价成交，滑点按参与度与固定基点叠加
  depthWeighted  盘口深度加权：有盘口就吃档位算 VWAP，无盘口则用当根振幅与
                 成交量参与度近似冲击成本
"""

from __future__ import annotations

import math

MODELS = {
    "nextOpen": {
        "name": "次根开盘价（默认）",
        "desc": "信号次日以开盘价成交，按账户滑点参数让价",
        "needs": ["bars"],
    },
    "closeFill": {
        "name": "当根收盘价",
        "desc": "信号当根以收盘价成交，按账户滑点参数让价（更激进的成交假设）",
        "needs": ["bars"],
    },
    "fixedBps": {
        "name": "开盘价 + 固定基点",
        "desc": "以开盘价成交，滑点按固定基点加成交量参与度折算",
        "needs": ["bars"],
    },
    "depthWeighted": {
        "name": "盘口深度加权",
        "desc": "买入吃卖档、卖出吃买档，按档位成交量加权求成交均价，缺盘口时用振幅近似",
        "needs": ["bars", "orderbook"],
    },
}

DEFAULT_MODEL = "nextOpen"


def meta():
    return {k: {"name": v["name"], "desc": v["desc"], "needs": v["needs"]}
            for k, v in MODELS.items()}


def _base_price(model, side, bar, ctx):
    """取基准价：次根开盘价或当根收盘价"""
    if model == "closeFill":
        return bar.get("close")
    return bar.get("open")


def _slip_ratio(ctx):
    try:
        return abs(float((ctx or {}).get("slippage") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _participation(ctx):
    try:
        p = float((ctx or {}).get("participation") or 0.05)
    except (TypeError, ValueError):
        p = 0.05
    return max(0.005, min(0.5, p))


def _impact_bps(bar, notional, price, ctx):
    """用当根振幅与成交量参与度近似冲击成本（基点）"""
    if not price or price <= 0:
        return 0.0
    volume = bar.get("volume") or 0
    if notional and notional > 0 and volume > 0:
        # 成交量对 A股为手、美股为股，统一折算成「股」
        shares = volume * 100 if (ctx or {}).get("market", "cn") == "cn" else volume
        turnover = shares * price
        ratio = min(0.5, (notional / turnover) if turnover > 0 else 0.0)
    else:
        ratio = _participation(ctx)
    swing = 0.0
    hi, lo = bar.get("high"), bar.get("low")
    if hi and lo and price:
        swing = max(0.0, (hi - lo) / price)
    # 冲击 = 振幅 × 参与度平方根，量纲为比例，转基点
    return math.sqrt(max(ratio, 0.0)) * max(swing, 0.0015) * 10000 * 0.5


def _book_fill(book, side, notional, unit_mult):
    """按档位吃单：返回 (加权均价, 已成交名义金额, 总委托额)"""
    levels = (book or {}).get("asks" if side == "buy" else "bids") or []
    left = notional
    shares_sum = 0.0
    cost = 0.0
    filled = 0.0
    for lv in levels:
        px = lv.get("price")
        vol = lv.get("volume") or 0
        if not px or px <= 0 or vol <= 0:
            continue
        lv_notional = vol * unit_mult * px
        take = min(left, lv_notional)
        if take <= 0:
            break
        part_shares = take / px
        cost += part_shares * px
        shares_sum += part_shares
        filled += take
        left -= take
    if shares_sum <= 0:
        return None, 0.0, notional
    return cost / shares_sum, filled, notional


def fill_price(model, side, bar, ctx):
    """计算成交价。

    side: buy / sell
    bar:  用于成交的那根K线（默认模型下为信号的下一根）
    ctx:  {slippage, market, notional, participation, orderbook, price_hint}
    返回 dict(price, base, model, bps, note)
    """
    model = model if model in MODELS else DEFAULT_MODEL
    ctx = ctx or {}
    base = _base_price(model, side, bar, ctx)
    if not base or base <= 0:
        base = bar.get("close") or bar.get("open")
    if not base or base <= 0:
        return {"price": None, "base": None, "model": model, "bps": 0.0, "note": "无有效价格"}

    ratio = _slip_ratio(ctx)
    notional = ctx.get("notional") or 0
    note = ""

    if model == "depthWeighted":
        book = ctx.get("orderbook") or {}
        unit_mult = 100 if ctx.get("market", "cn") == "cn" else 1
        if notional > 0:
            px, filled, total = _book_fill(book, side, notional, unit_mult)
            if px:
                bps = (px / base - 1) * 10000 * (1 if side == "buy" else -1)
                covered = min(1.0, filled / total) if total else 1.0
                note = "盘口深度撮合，覆盖委托 %.0f%%" % (covered * 100)
                if covered < 0.999:
                    # 盘口深度不足：未覆盖部分按冲击成本折算，避免低估滑点
                    extra = _impact_bps(bar, (total - filled), px, ctx)
                    px = px * (1 + (1 if side == "buy" else -1) * extra / 10000.0)
                    bps = (px / base - 1) * 10000 * (1 if side == "buy" else -1)
                    note += "，缺口按冲击成本近似"
                return {"price": px, "base": base, "model": model, "bps": bps, "note": note}
        # 无盘口：用振幅与参与度近似冲击
        bps = _impact_bps(bar, notional, base, ctx)
        sign = 1 if side == "buy" else -1
        px = base * (1 + sign * bps / 10000.0) * (1 + sign * ratio)
        return {"price": px, "base": base, "model": model, "bps": bps,
                "note": "无盘口数据，按振幅与参与度近似冲击成本"}

    if model == "fixedBps":
        bps = _impact_bps(bar, notional, base, ctx)
        sign = 1 if side == "buy" else -1
        px = base * (1 + sign * (bps / 10000.0 + ratio))
        return {"price": px, "base": base, "model": model, "bps": bps,
                "note": "固定基点 + 参与度冲击近似"}

    # nextOpen / closeFill：与历史行为完全一致（比例滑点）
    sign = 1 if side == "buy" else -1
    px = base * (1 + sign * ratio)
    return {"price": px, "base": base, "model": model, "bps": ratio * 10000.0,
            "note": "比例滑点" if ratio else "无滑点"}


__all__ = ["MODELS", "DEFAULT_MODEL", "meta", "fill_price"]
