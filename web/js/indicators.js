/* ==========================================================================
   AlphaDesk · 技术指标与信号引擎
   ========================================================================== */
(function () {
  'use strict';

  const isNum = (v) => typeof v === 'number' && isFinite(v);

  /* ------------------------------------------------------------ 均线族 */

  function SMA(values, n) {
    const out = new Array(values.length).fill(null);
    let sum = 0, cnt = 0;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (isNum(v)) { sum += v; cnt++; }
      if (i >= n) {
        const old = values[i - n];
        if (isNum(old)) { sum -= old; cnt--; }
      }
      if (i >= n - 1 && cnt === n) out[i] = sum / n;
    }
    return out;
  }

  function EMA(values, n) {
    const out = new Array(values.length).fill(null);
    const k = 2 / (n + 1);
    let prev = null;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (!isNum(v)) continue;
      prev = prev === null ? v : v * k + prev * (1 - k);
      out[i] = prev;
    }
    return out;
  }

  function MA(values, n) { return SMA(values, n); }

  /* 指数平滑移动平均（用于 MACD） */
  function MACD(closes, fast, slow, signal) {
    fast = fast || 12; slow = slow || 26; signal = signal || 9;
    const ef = EMA(closes, fast), es = EMA(closes, slow);
    const dif = closes.map((_, i) => (isNum(ef[i]) && isNum(es[i]) ? ef[i] - es[i] : null));
    const valid = dif.filter(isNum);
    const deaValid = EMA(valid, signal);
    const dea = new Array(closes.length).fill(null);
    let k = 0;
    for (let i = 0; i < dif.length; i++) {
      if (isNum(dif[i])) { dea[i] = deaValid[k] === undefined ? null : deaValid[k]; k++; }
    }
    const macd = closes.map((_, i) => {
      if (!isNum(dif[i]) || !isNum(dea[i])) return null;
      return (dif[i] - dea[i]) * 2;
    });
    return { dif, dea, macd };
  }

  function BOLL(closes, n, k) {
    n = n || 20; k = k || 2;
    const mid = SMA(closes, n);
    const up = new Array(closes.length).fill(null);
    const low = new Array(closes.length).fill(null);
    for (let i = n - 1; i < closes.length; i++) {
      const win = closes.slice(i - n + 1, i + 1).filter(isNum);
      if (win.length < n) continue;
      const m = mid[i];
      const variance = win.reduce((a, v) => a + (v - m) * (v - m), 0) / n;
      const sd = Math.sqrt(variance);
      up[i] = m + k * sd;
      low[i] = m - k * sd;
    }
    return { mid, up, low };
  }

  function RSI(closes, n) {
    n = n || 14;
    const out = new Array(closes.length).fill(null);
    let ag = 0, al = 0;
    for (let i = 1; i < closes.length; i++) {
      const ch = closes[i] - closes[i - 1];
      const gain = ch > 0 ? ch : 0, loss = ch < 0 ? -ch : 0;
      if (i <= n) {
        ag += gain / n; al += loss / n;
        if (i === n) out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
      } else {
        ag = (ag * (n - 1) + gain) / n;
        al = (al * (n - 1) + loss) / n;
        out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
      }
    }
    return out;
  }

  function KDJ(bars, n, m1, m2) {
    n = n || 9; m1 = m1 || 3; m2 = m2 || 3;
    const rsv = new Array(bars.length).fill(null);
    for (let i = 0; i < bars.length; i++) {
      if (i < n - 1) continue;
      const win = bars.slice(i - n + 1, i + 1);
      const hi = Math.max.apply(null, win.map((b) => b.high));
      const lo = Math.min.apply(null, win.map((b) => b.low));
      const c = bars[i].close;
      rsv[i] = hi === lo ? 50 : ((c - lo) / (hi - lo)) * 100;
    }
    const K = new Array(bars.length).fill(null);
    const D = new Array(bars.length).fill(null);
    const J = new Array(bars.length).fill(null);
    let pk = 50, pd = 50;
    for (let i = 0; i < bars.length; i++) {
      if (!isNum(rsv[i])) continue;
      pk = (2 / 3) * pk + (1 / 3) * rsv[i];
      pd = (2 / 3) * pd + (1 / 3) * pk;
      K[i] = pk; D[i] = pd; J[i] = 3 * pk - 2 * pd;
    }
    return { K, D, J };
  }

  function ATR(bars, n) {
    n = n || 14;
    const tr = bars.map((b, i) => {
      if (i === 0) return b.high - b.low;
      const pc = bars[i - 1].close;
      return Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
    });
    return EMA(tr, n);
  }

  function OBV(bars) {
    const out = new Array(bars.length).fill(0);
    let acc = 0;
    for (let i = 1; i < bars.length; i++) {
      const d = bars[i].close - bars[i - 1].close;
      const v = bars[i].volume || 0;
      acc += d > 0 ? v : (d < 0 ? -v : 0);
      out[i] = acc;
    }
    return out;
  }

  /* -------------------------------------------------------- 信号引擎 */

  function crossUp(a, b, i) {
    return i > 0 && isNum(a[i]) && isNum(b[i]) && isNum(a[i - 1]) && isNum(b[i - 1]) &&
      a[i - 1] <= b[i - 1] && a[i] > b[i];
  }
  function crossDown(a, b, i) {
    return i > 0 && isNum(a[i]) && isNum(b[i]) && isNum(a[i - 1]) && isNum(b[i - 1]) &&
      a[i - 1] >= b[i - 1] && a[i] < b[i];
  }

  /**
   * 对一段K线做技术信号研判
   * @returns { score, bias, signals:[{name, dir, weight, desc}], ind }
   */
  function analyze(bars) {
    const res = { score: 0, bias: '中性', signals: [], ind: null };
    if (!bars || bars.length < 30) return res;

    const closes = bars.map((b) => b.close);
    const i = bars.length - 1;
    const ma5 = SMA(closes, 5), ma10 = SMA(closes, 10), ma20 = SMA(closes, 20), ma60 = SMA(closes, 60);
    const macd = MACD(closes);
    const rsi6 = RSI(closes, 6), rsi14 = RSI(closes, 14);
    const kdj = KDJ(bars);
    const boll = BOLL(closes, 20, 2);
    const atr = ATR(bars, 14);
    const obv = OBV(bars);

    res.ind = {
      ma5: ma5[i], ma10: ma10[i], ma20: ma20[i], ma60: ma60[i],
      dif: macd.dif[i], dea: macd.dea[i], macd: macd.macd[i],
      rsi6: rsi6[i], rsi14: rsi14[i],
      k: kdj.K[i], d: kdj.D[i], j: kdj.J[i],
      bollUp: boll.up[i], bollMid: boll.mid[i], bollLow: boll.low[i],
      atr: atr[i], obv: obv[i], obvPrev: obv[Math.max(0, i - 5)],
    };

    const signals = [];
    const close = closes[i];

    /* 均线多头/空头排列 */
    if (isNum(ma5[i]) && isNum(ma20[i])) {
      if (ma5[i] > ma20[i] && ma5[i - 1] <= ma20[i - 1]) {
        signals.push({ name: 'MA5 上穿 MA20', dir: 1, weight: 18, desc: '短期均线金叉中期均线，趋势转强' });
      } else if (ma5[i] < ma20[i] && ma5[i - 1] >= ma20[i - 1]) {
        signals.push({ name: 'MA5 下穿 MA20', dir: -1, weight: 18, desc: '短期均线死叉中期均线，趋势转弱' });
      }
    }
    if (isNum(ma5[i]) && isNum(ma10[i]) && isNum(ma20[i])) {
      if (ma5[i] > ma10[i] && ma10[i] > ma20[i]) {
        signals.push({ name: '均线多头排列', dir: 1, weight: 12, desc: 'MA5 > MA10 > MA20，多头结构' });
      } else if (ma5[i] < ma10[i] && ma10[i] < ma20[i]) {
        signals.push({ name: '均线空头排列', dir: -1, weight: 12, desc: 'MA5 < MA10 < MA20，空头结构' });
      }
    }
    if (isNum(ma60[i])) {
      const above = close > ma60[i];
      signals.push({
        name: above ? '站上 60 日均线' : '跌破 60 日均线', dir: above ? 1 : -1, weight: 10,
        desc: above ? '价格位于中期均线上方，中期偏强' : '价格位于中期均线下方，中期偏弱',
      });
    }

    /* MACD */
    if (crossUp(macd.dif, macd.dea, i)) {
      signals.push({ name: 'MACD 金叉', dir: 1, weight: 16, desc: 'DIF 上穿 DEA，动能转多' });
    } else if (crossDown(macd.dif, macd.dea, i)) {
      signals.push({ name: 'MACD 死叉', dir: -1, weight: 16, desc: 'DIF 下穿 DEA，动能转空' });
    }
    if (isNum(macd.macd[i]) && isNum(macd.macd[i - 1])) {
      if (macd.macd[i] > 0 && macd.macd[i - 1] <= 0) {
        signals.push({ name: 'MACD 柱翻红', dir: 1, weight: 8, desc: '红柱出现，多头动能增强' });
      } else if (macd.macd[i] < 0 && macd.macd[i - 1] >= 0) {
        signals.push({ name: 'MACD 柱翻绿', dir: -1, weight: 8, desc: '绿柱出现，空头动能增强' });
      }
    }

    /* RSI */
    if (isNum(rsi14[i])) {
      if (rsi14[i] >= 75) signals.push({ name: 'RSI 超买', dir: -1, weight: 12, desc: 'RSI14 = ' + rsi14[i].toFixed(1) + '，短线过热' });
      else if (rsi14[i] <= 28) signals.push({ name: 'RSI 超卖', dir: 1, weight: 12, desc: 'RSI14 = ' + rsi14[i].toFixed(1) + '，短线超跌' });
      else if (rsi14[i] > 55) signals.push({ name: 'RSI 偏强', dir: 1, weight: 6, desc: 'RSI14 = ' + rsi14[i].toFixed(1) });
      else if (rsi14[i] < 45) signals.push({ name: 'RSI 偏弱', dir: -1, weight: 6, desc: 'RSI14 = ' + rsi14[i].toFixed(1) });
    }

    /* KDJ */
    if (isNum(kdj.K[i]) && isNum(kdj.D[i])) {
      if (kdj.K[i - 1] <= kdj.D[i - 1] && kdj.K[i] > kdj.D[i] && kdj.K[i] < 40) {
        signals.push({ name: 'KDJ 低位金叉', dir: 1, weight: 12, desc: 'K 上穿 D 且位于低位，反弹信号' });
      } else if (kdj.K[i - 1] >= kdj.D[i - 1] && kdj.K[i] < kdj.D[i] && kdj.K[i] > 70) {
        signals.push({ name: 'KDJ 高位死叉', dir: -1, weight: 12, desc: 'K 下穿 D 且位于高位，回调信号' });
      }
    }

    /* 布林带 */
    if (isNum(boll.up[i]) && isNum(boll.low[i])) {
      if (close > boll.up[i]) signals.push({ name: '突破布林上轨', dir: -1, weight: 8, desc: '价格触及上轨，短期波动加大' });
      else if (close < boll.low[i]) signals.push({ name: '跌破布林下轨', dir: 1, weight: 8, desc: '价格触及下轨，超跌区域' });
    }

    /* 量能 */
    const vols = bars.map((b) => b.volume || 0);
    const v5 = SMA(vols, 5), v20 = SMA(vols, 20);
    if (isNum(v5[i]) && isNum(v20[i]) && v20[i] > 0) {
      const vr = v5[i] / v20[i];
      if (vr >= 1.6) signals.push({ name: '成交量显著放大', dir: 1, weight: 10, desc: '5日均量 / 20日均量 = ' + vr.toFixed(2) });
      else if (vr <= 0.6) signals.push({ name: '成交量萎缩', dir: -1, weight: 6, desc: '5日均量 / 20日均量 = ' + vr.toFixed(2) });
    }

    /* 价格位置 / 突破 */
    const win20 = bars.slice(-20);
    const win60 = bars.slice(-60);
    const hi20 = Math.max.apply(null, win20.map((b) => b.high));
    const lo20 = Math.min.apply(null, win20.map((b) => b.low));
    const hi60 = Math.max.apply(null, win60.map((b) => b.high));
    const lo60 = Math.min.apply(null, win60.map((b) => b.low));
    if (close >= hi20 * 0.998) signals.push({ name: '创 20 日新高', dir: 1, weight: 14, desc: '突破近一个月高点' });
    if (close <= lo20 * 1.002) signals.push({ name: '创 20 日新低', dir: -1, weight: 14, desc: '跌破近一个月低点' });
    res.ind.hi20 = hi20; res.ind.lo20 = lo20; res.ind.hi60 = hi60; res.ind.lo60 = lo60;

    /* OBV 趋势 */
    if (isNum(obv[i]) && isNum(obv[Math.max(0, i - 5)])) {
      const up = obv[i] > obv[Math.max(0, i - 5)];
      signals.push({
        name: up ? 'OBV 能量潮上行' : 'OBV 能量潮下行', dir: up ? 1 : -1, weight: 6,
        desc: '近 5 日量能累积' + (up ? '增加' : '减少'),
      });
    }

    /* 波动率 */
    if (isNum(atr[i]) && close) {
      const atrPct = (atr[i] / close) * 100;
      res.ind.atrPct = atrPct;
      if (atrPct >= 4) signals.push({ name: '波动率偏高', dir: -1, weight: 4, desc: 'ATR/价格 = ' + atrPct.toFixed(2) + '%' });
    }

    let score = 0, total = 0;
    signals.forEach((s) => { score += s.dir * s.weight; total += s.weight; });
    const norm = total ? Math.round((score / Math.max(total, 40)) * 100) : 0;
    res.score = Math.max(-100, Math.min(100, norm));
    res.bias = res.score >= 35 ? '偏多' : (res.score >= 12 ? '温和偏多' :
      (res.score <= -35 ? '偏空' : (res.score <= -12 ? '温和偏空' : '中性')));
    res.signals = signals;
    return res;
  }

  /* ------------------------------------------------------- 策略回测 */

  const STRATEGIES = {
    maCross: {
      name: '双均线交叉',
      desc: '快线上穿慢线买入，下穿卖出',
      params: [
        { key: 'fast', label: '快线周期', def: 5, min: 2, max: 120 },
        { key: 'slow', label: '慢线周期', def: 20, min: 3, max: 250 },
      ],
      run(bars, p, i) {
        const closes = bars.map((b) => b.close);
        const f = SMA(closes, p.fast), s = SMA(closes, p.slow);
        if (crossUp(f, s, i)) return 'buy';
        if (crossDown(f, s, i)) return 'sell';
        return null;
      },
    },
    macd: {
      name: 'MACD 金叉死叉',
      desc: 'DIF 上穿 DEA 买入，下穿卖出',
      params: [
        { key: 'fast', label: '快线 EMA', def: 12, min: 3, max: 60 },
        { key: 'slow', label: '慢线 EMA', def: 26, min: 5, max: 120 },
        { key: 'signal', label: '信号线', def: 9, min: 2, max: 40 },
      ],
      run(bars, p, i) {
        const closes = bars.map((b) => b.close);
        const m = MACD(closes, p.fast, p.slow, p.signal);
        if (crossUp(m.dif, m.dea, i)) return 'buy';
        if (crossDown(m.dif, m.dea, i)) return 'sell';
        return null;
      },
    },
    rsi: {
      name: 'RSI 超卖反转',
      desc: 'RSI 低于下限买入，高于上限卖出',
      params: [
        { key: 'n', label: 'RSI 周期', def: 14, min: 3, max: 40 },
        { key: 'low', label: '买入阈值', def: 30, min: 5, max: 50 },
        { key: 'high', label: '卖出阈值', def: 70, min: 50, max: 95 },
      ],
      run(bars, p, i) {
        const r = RSI(bars.map((b) => b.close), p.n);
        if (!isNum(r[i]) || !isNum(r[i - 1])) return null;
        if (r[i - 1] < p.low && r[i] >= p.low) return 'buy';
        if (r[i - 1] > p.high && r[i] <= p.high) return 'sell';
        return null;
      },
    },
    breakout: {
      name: 'N 日通道突破',
      desc: '突破前 N 日高点买入，跌破 M 日低点卖出',
      params: [
        { key: 'n', label: '突破周期', def: 20, min: 3, max: 120 },
        { key: 'm', label: '止损周期', def: 10, min: 2, max: 120 },
      ],
      run(bars, p, i) {
        if (i < Math.max(p.n, p.m) + 1) return null;
        const prevHi = Math.max.apply(null, bars.slice(i - p.n, i).map((b) => b.high));
        const prevLo = Math.min.apply(null, bars.slice(i - p.m, i).map((b) => b.low));
        if (bars[i].close > prevHi) return 'buy';
        if (bars[i].close < prevLo) return 'sell';
        return null;
      },
    },
    momentum: {
      name: '动量轮动',
      desc: '近 N 日涨幅超过阈值买入，动量转负卖出',
      params: [
        { key: 'n', label: '动量周期', def: 20, min: 3, max: 120 },
        { key: 'th', label: '入场阈值%', def: 5, min: 0.5, max: 50, step: 0.5 },
      ],
      run(bars, p, i) {
        if (i < p.n) return null;
        const prev = bars[i - p.n].close;
        const mom = (bars[i].close / prev - 1) * 100;
        const prevMom = (bars[i - 1] / bars[i - 1 - p.n].close - 1) * 100;
        if (prevMom <= p.th && mom > p.th) return 'buy';
        if (prevMom >= 0 && mom < 0) return 'sell';
        return null;
      },
    },
  };

  /**
   * 回测：单标的、全仓、次日开盘成交（避免未来函数）
   */
  function backtest(bars, strategyKey, rawParams, cfg) {
    const st = STRATEGIES[strategyKey];
    const c = Object.assign({ feeRate: 0.0003, slippage: 0.001, initial: 100000, stopLoss: 0, takeProfit: 0 }, cfg || {});
    if (!st || !bars || bars.length < 40) {
      return { ok: false, message: '数据不足，至少需要 40 根K线' };
    }
    const p = {};
    st.params.forEach((pd) => { p[pd.key] = Number(rawParams && rawParams[pd.key] !== undefined ? rawParams[pd.key] : pd.def); });

    const signals = new Array(bars.length).fill(null);
    const sig = new Array(bars.length).fill(null);
    for (let i = 1; i < bars.length; i++) sig[i] = st.run(bars, p, i);

    let cash = c.initial, shares = 0, entryPrice = 0, entryIdx = 0, entryFee = 0;
    const trades = [], equity = [];
    let pending = null;

    for (let i = (st.params.reduce((a, x) => Math.max(a, x.def * 2), 20)) + 1; i < bars.length; i++) {
      const bar = bars[i];
      /* 执行上一根K线产生的信号（本根开盘价成交，规避未来函数） */
      if (pending) {
        const px = bar.open * (1 + (pending === 'buy' ? c.slippage : -c.slippage));
        if (pending === 'buy' && shares === 0) {
          const qty = Math.floor((cash / px) / 100) * 100 || Math.floor(cash / px);
          if (qty > 0) {
            const cost = qty * px;
            const fee = cost * c.feeRate;
            cash -= cost + fee;
            shares = qty;
            entryPrice = px; entryIdx = i; entryFee = fee;
          }
        } else if (pending === 'sell' && shares > 0) {
          const gross = shares * px;
          const fee = gross * c.feeRate;
          cash += gross - fee;
          trades.push({
            inDate: bars[entryIdx].t, outDate: bar.t, inPrice: entryPrice, outPrice: px,
            qty: shares, pnl: (px - entryPrice) * shares - fee - entryFee,
            pnlPct: (px / entryPrice - 1) * 100 - (c.feeRate * 2) * 100,
            holdBars: i - entryIdx, reason: '信号',
          });
          shares = 0;
        }
        pending = null;
      }
      /* 风控：止损 / 止盈（收盘触达，记账于收盘价） */
      if (shares > 0 && (c.stopLoss > 0 || c.takeProfit > 0)) {
        const chg = (bar.close / entryPrice - 1) * 100;
        let reason = null;
        if (c.stopLoss > 0 && chg <= -c.stopLoss) reason = '止损';
        else if (c.takeProfit > 0 && chg >= c.takeProfit) reason = '止盈';
        if (reason) {
          const px = bar.close * (1 - c.slippage);
          const gross = shares * px;
          const fee = gross * c.feeRate;
          cash += gross - fee;
          trades.push({
            inDate: bars[entryIdx].t, outDate: bar.t, inPrice: entryPrice, outPrice: px,
            qty: shares, pnl: (px - entryPrice) * shares - fee - entryFee,
            pnlPct: (px / entryPrice - 1) * 100 - (c.feeRate * 2) * 100,
            holdBars: i - entryIdx, reason,
          });
          shares = 0;
        }
      }
      /* 记录信号 */
      if (sig[i]) {
        signals[i] = sig[i];
        if (sig[i] === 'buy' && shares === 0 && pending !== 'buy') pending = 'buy';
        else if (sig[i] === 'sell' && shares > 0) pending = 'sell';
      }
      equity.push({ t: bar.t, v: cash + shares * bar.close, close: bar.close, holding: shares > 0 });
    }

    /* 收尾平仓 */
    const lastBar = bars[bars.length - 1];
    if (shares > 0) {
      const px = lastBar.close;
      const gross = shares * px;
      const fee = gross * c.feeRate;
      cash += gross - fee;
      trades.push({
        inDate: bars[entryIdx].t, outDate: lastBar.t, inPrice: entryPrice, outPrice: px,
        qty: shares, pnl: (px - entryPrice) * shares - fee - entryFee,
        pnlPct: (px / entryPrice - 1) * 100 - (c.feeRate * 2) * 100,
        holdBars: bars.length - 1 - entryIdx, reason: '期末平仓',
      });
      shares = 0;
    }

    const finalEquity = cash;
    const totalReturn = (finalEquity / c.initial - 1) * 100;
    const wins = trades.filter((t) => t.pnl > 0);
    const losses = trades.filter((t) => t.pnl <= 0);
    const grossWin = sumPnl(wins), grossLoss = Math.abs(sumPnl(losses));
    let peak = -Infinity, maxDD = 0;
    equity.forEach((e) => {
      peak = Math.max(peak, e.v);
      maxDD = Math.max(maxDD, (peak - e.v) / peak * 100);
    });
    const days = bars.length;
    const years = Math.max(days / 244, 0.08);
    const annual = (Math.pow(finalEquity / c.initial, 1 / years) - 1) * 100;
    /* 基准：买入持有 */
    const bh = (lastBar.close / bars[Math.min(30, bars.length - 1)].close - 1) * 100;

    return {
      ok: true,
      strategy: st.name,
      params: p,
      trades,
      equity: equity.map((e) => ({ t: e.t, v: e.v, close: e.close, holding: e.holding })),
      marks: signals.map((s, i) => (s ? { t: bars[i].t, dir: s, price: bars[i].close, idx: i } : null)).filter(Boolean),
      stats: {
        totalReturn, annual, maxDD,
        winRate: trades.length ? (wins.length / trades.length) * 100 : 0,
        trades: trades.length,
        profitFactor: grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? 99 : 0),
        avgWin: wins.length ? sumPnl(wins) / wins.length : 0,
        avgLoss: losses.length ? -sumPnl(losses) / losses.length : 0,
        finalEquity, initial: c.initial, bars: bars.length, benchmark: bh,
        avgHold: trades.length ? trades.reduce((a, t) => a + t.holdBars, 0) / trades.length : 0,
      },
    };
  }

  function sumPnl(list) { return list.reduce((a, t) => a + t.pnl, 0); }

  window.AD = window.AD || {};
  Object.assign(window.AD, {
    ind: { SMA, EMA, MA, MACD, BOLL, RSI, KDJ, ATR, OBV },
    quant: { analyze, backtest, STRATEGIES },
  });
})();
