/* ==========================================================================
   AlphaDesk · 图表引擎（原生 Canvas，无第三方依赖）
     AD.chart.kline(wrap, opts)   蜡烛图 + 成交量 + 副图指标（MA/BOLL/MACD/KDJ/RSI）
     AD.chart.trend(wrap, opts)   分时图（价格 + 均价 + 成交量）
     AD.chart.line(wrap, opts)    通用折线（资金曲线 / 资金流）
   ========================================================================== */
(function () {
  'use strict';

  const F = window.AD.fmt;
  const isNum = (v) => typeof v === 'number' && isFinite(v);

  const PALETTE = {
    grid: 'rgba(38, 45, 58, 0.55)',
    gridSoft: 'rgba(38, 45, 58, 0.28)',
    axis: '#5d677a',
    text: '#939db0',
    cross: 'rgba(180, 195, 220, 0.55)',
    ma5: '#e8b339',
    ma10: '#4d8dff',
    ma20: '#c977e0',
    ma60: '#31c4bd',
    bollMid: '#e8b339',
    bollBand: 'rgba(232, 179, 57, 0.55)',
    dif: '#e8b339',
    dea: '#4d8dff',
    macdUp: '#ff4d4f',
    macdDown: '#12c48b',
    k: '#e8b339',
    d: '#4d8dff',
    j: '#c977e0',
    rsi6: '#e8b339',
    rsi14: '#4d8dff',
    avg: '#e8b339',
    volUp: 'rgba(255, 77, 79, 0.5)',
    volDown: 'rgba(18, 196, 139, 0.5)',
    hi: '#ffdf7e',
    lo: '#7ef0d0',
    /* AI 选股叠加层（建议标记 / 交易计划线 / 预测带） */
    planEntry: '#4d8dff',
    tipBg: 'rgba(20,24,31,0.78)',
    forecastFill: 'rgba(130,80,223,0.16)',
    forecastLine: '#8250df',
  };

  let upColor = '#ff4d4f';
  let downColor = '#12c48b';
  function syncColors() {
    const cs = getComputedStyle(document.documentElement);
    upColor = cs.getPropertyValue('--up').trim() || upColor;
    downColor = cs.getPropertyValue('--down').trim() || downColor;
  }
  syncColors();
  document.addEventListener('AD:theme', syncColors);

  function dirColor(v) { return v > 0 ? upColor : (v < 0 ? downColor : '#8b95a5'); }

  function niceTicks(min, max, n) {
    if (!isNum(min) || !isNum(max)) return [];
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const step0 = span / Math.max(2, n);
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / mag;
    const step = (norm >= 5 ? 5 : norm >= 2.5 ? 2.5 : norm >= 1.5 ? 2 : 1) * mag;
    const ticks = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 0.001; v += step) ticks.push(v);
    return ticks;
  }

  function baseCanvas(wrap) {
    const canvas = document.createElement('canvas');
    canvas.style.width = '100%';
    canvas.style.display = 'block';
    wrap.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    return { canvas, ctx };
  }

  function prepare(wrap, canvas, ctx, height) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(240, wrap.clientWidth || 600);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, height);
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = 'middle';
    return { w, h: height };
  }

  /* ==================================================================
     蜡烛图
     ================================================================== */

  class KlineChart {
    constructor(wrap, opts) {
      this.wrap = wrap;
      this.opts = Object.assign({
        height: 420, period: 'day', marks: [], showMA: true, showBOLL: false,
        sub: 'MACD', onLegend: null, colorMode: 'cn',
      }, opts || {});
      wrap.classList.add('chart-canvas-wrap');
      const c = baseCanvas(wrap);
      this.canvas = c.canvas; this.ctx = c.ctx;
      this.bars = [];
      this.vis = { start: 0, count: 120 };
      this.hover = -1;
      /* AI 选股叠加层：{ marks:[{idx,dir,label,kind}], forecast:{path:[{i,mid,lo,hi}]},
         plan:{entry,stop,target1,target2} }，为 null 时与旧行为完全一致 */
      this.advisor = null;
      this.tip = document.createElement('div');
      this.tip.className = 'chart-tip hidden';
      wrap.appendChild(this.tip);
      this._bind();
      this._ro = new ResizeObserver(() => this.render());
      this._ro.observe(wrap);
    }

    setData(bars, opts) {
      this.bars = bars || [];
      this.marks = (opts && opts.marks) || this.opts.marks || [];
      const n = this.bars.length;
      this.vis.count = Math.min(n, this.vis.count || 120);
      this.vis.start = Math.max(0, n - this.vis.count);
      this.compute();
      this.render();
      this._syncLegend(n - 1);
    }

    setOptions(patch) {
      Object.assign(this.opts, patch || {});
      this.compute();
      this.render();
    }

    setSub(sub) { this.opts.sub = sub; this.compute(); this.render(); }
    setMA(on) { this.opts.showMA = on; this.render(); }
    setBOLL(on) { this.opts.showBOLL = on; this.render(); }
    /* 叠加 AI 选股建议：传入 null 可清除 */
    setAdvisor(advisor) {
      this.advisor = advisor || null;
      this.render();
    }
    resetZoom() {
      this.vis.count = Math.min(this.bars.length, 120);
      this.vis.start = Math.max(0, this.bars.length - this.vis.count);
      this.render();
    }

    compute() {
      const b = this.bars;
      const closes = b.map((x) => x.close);
      const I = window.AD.ind;
      this.ma = {
        ma5: I.SMA(closes, 5), ma10: I.SMA(closes, 10),
        ma20: I.SMA(closes, 20), ma60: I.SMA(closes, 60),
      };
      this.boll = I.BOLL(closes, 20, 2);
      this.macd = I.MACD(closes, 12, 26, 9);
      this.kdj = I.KDJ(b, 9, 3, 3);
      this.rsi = { rsi6: I.RSI(closes, 6), rsi14: I.RSI(closes, 14) };
    }

    _bind() {
      const canvas = this.canvas;
      let dragging = false, dragX = 0, dragStart = 0;

      canvas.addEventListener('mousemove', (e) => {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        if (dragging) {
          const barW = this.geo ? this.geo.barW : 6;
          const shift = Math.round((dragX - x) / barW);
          this.vis.start = Math.max(0, Math.min(this.bars.length - this.vis.count, dragStart + shift));
          this.render();
          return;
        }
        const idx = this.xToIndex(x);
        if (idx !== this.hover) {
          this.hover = idx;
          this.render();
          this._syncLegend(idx);
        }
        this._showTip(x, y, idx);
      });

      canvas.addEventListener('mouseleave', () => {
        this.hover = -1; dragging = false;
        this.tip.classList.add('hidden');
        this.render();
        this._syncLegend(this.bars.length - 1);
      });

      canvas.addEventListener('mousedown', (e) => {
        const rect = canvas.getBoundingClientRect();
        dragging = true; dragX = e.clientX - rect.left; dragStart = this.vis.start;
        canvas.style.cursor = 'grabbing';
      });
      window.addEventListener('mouseup', () => {
        if (dragging) { dragging = false; canvas.style.cursor = 'crosshair'; }
      });

      canvas.addEventListener('wheel', (e) => {
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const center = this.xToIndex(x);
        const factor = e.deltaY > 0 ? 1.18 : 0.85;
        const oldCount = this.vis.count;
        let count = Math.round(oldCount * factor);
        count = Math.max(20, Math.min(this.bars.length, count));
        if (count === oldCount) return;
        const ratio = oldCount ? (center - this.vis.start) / oldCount : 0.5;
        this.vis.count = count;
        this.vis.start = Math.max(0, Math.min(this.bars.length - count,
          Math.round(center - ratio * count)));
        this.render();
      }, { passive: false });

      canvas.addEventListener('dblclick', () => this.resetZoom());
    }

    xToIndex(x) {
      if (!this.geo || !this.bars.length) return -1;
      const g = this.geo;
      const i = Math.floor((x - g.x0) / g.barW) + this.vis.start;
      return Math.max(0, Math.min(this.bars.length - 1, i));
    }

    visibleBars() { return this.bars.slice(this.vis.start, this.vis.start + this.vis.count); }

    render() {
      syncColors();
      const { ctx, wrap } = this;
      const height = this.opts.height;
      const { w, h } = prepare(wrap, this.canvas, ctx, height);
      const bars = this.visibleBars();
      if (!bars.length) {
        ctx.fillStyle = PALETTE.axis;
        ctx.textAlign = 'center';
        ctx.fillText('暂无K线数据', w / 2, h / 2);
        return;
      }
      const pad = { l: 6, r: 58, t: 12, b: 20 };
      const innerW = w - pad.l - pad.r;
      const innerH = h - pad.t - pad.b;
      const hasSub = !!this.opts.sub;
      const gap = 8;
      const mainH = hasSub ? innerH * 0.58 : innerH * 0.76;
      const volH = hasSub ? innerH * 0.14 : innerH * 0.22;
      const subH = hasSub ? innerH - mainH - volH - gap * 2 : 0;
      const main = { x: pad.l, y: pad.t, w: innerW, h: mainH };
      const vol = { x: pad.l, y: main.y + mainH + gap, w: innerW, h: volH };
      const sub = hasSub ? { x: pad.l, y: vol.y + volH + gap, w: innerW, h: subH } : null;

      /* 右侧为「预测带」预留槽位；无预测时 slots == bars.length，与旧行为一致 */
      const ad = this.advisor;
      const fPath = (ad && ad.forecast && Array.isArray(ad.forecast.path)) ? ad.forecast.path : [];
      const extra = fPath.length;
      const slots = bars.length + extra;
      const barW = innerW / slots;
      this.geo = { x0: main.x, barW, main, vol, sub, pad, slots, extra };

      /* 主图价格范围 */
      const from = this.vis.start, to = from + bars.length - 1;
      let hi = -Infinity, lo = Infinity;
      bars.forEach((b) => { hi = Math.max(hi, b.high); lo = Math.min(lo, b.low); });
      const overlayKeys = this.opts.showBOLL ? ['bollUp', 'bollLow'] : [];
      if (this.opts.showMA) {
        ['ma5', 'ma10', 'ma20', 'ma60'].forEach((k) => {
          for (let i = from; i <= to; i++) {
            const v = this.ma[k][i];
            if (isNum(v)) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
          }
        });
      }
      if (this.opts.showBOLL) {
        for (let i = from; i <= to; i++) {
          const a = this.boll.up[i], b2 = this.boll.low[i];
          if (isNum(a)) hi = Math.max(hi, a);
          if (isNum(b2)) lo = Math.min(lo, b2);
        }
      }
      /* 预测带与交易计划线也要落在可视范围内，否则会被裁掉 */
      if (extra) {
        fPath.forEach((p) => {
          if (isNum(p.hi)) hi = Math.max(hi, p.hi);
          if (isNum(p.lo)) lo = Math.min(lo, p.lo);
          if (isNum(p.mid)) { hi = Math.max(hi, p.mid); lo = Math.min(lo, p.mid); }
        });
      }
      const plan = (ad && ad.plan) || null;
      if (plan) {
        ['entry', 'stop', 'target1', 'target2'].forEach((k) => {
          const v = plan[k];
          if (isNum(v) && v > 0) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
        });
      }
      const padPct = (hi - lo) * 0.06 || hi * 0.01;
      hi += padPct; lo -= padPct;
      const yOf = (p) => main.y + main.h - ((p - lo) / (hi - lo)) * main.h;

      /* 网格 + 价格轴 */
      const ticks = niceTicks(lo, hi, 5);
      ctx.strokeStyle = PALETTE.grid;
      ctx.lineWidth = 1;
      ctx.textAlign = 'left';
      ticks.forEach((t) => {
        const y = Math.round(yOf(t)) + 0.5;
        ctx.beginPath(); ctx.moveTo(main.x, y); ctx.lineTo(main.x + innerW, y); ctx.stroke();
        ctx.fillStyle = PALETTE.axis;
        ctx.fillText(F.price(t, this.opts.market, 2), main.x + innerW + 6, y);
      });

      /* 蜡烛 */
      const bodyW = Math.max(1, Math.min(barW * 0.7, 14));
      bars.forEach((b, k) => {
        const i = from + k;
        const cx = main.x + (k + 0.5) * barW;
        const col = dirColor(b.close - b.open === 0 ? b.close - (b.prevClose || b.open) : b.close - b.open);
        const rising = b.close >= b.open;
        const c = rising ? upColor : downColor;
        ctx.strokeStyle = c;
        ctx.fillStyle = c;
        ctx.lineWidth = 1;
        const yo = yOf(b.open), yc = yOf(b.close), yh = yOf(b.high), yl = yOf(b.low);
        ctx.beginPath();
        ctx.moveTo(Math.round(cx) + 0.5, Math.round(yh));
        ctx.lineTo(Math.round(cx) + 0.5, Math.round(yl));
        ctx.stroke();
        const top = Math.min(yo, yc), bh = Math.max(1, Math.abs(yc - yo));
        if (rising) ctx.fillRect(Math.round(cx - bodyW / 2), Math.round(top), Math.round(bodyW), Math.round(bh));
        else {
          ctx.fillRect(Math.round(cx - bodyW / 2), Math.round(top), Math.round(bodyW), Math.round(bh));
        }
      });

      /* 均线 / 布林 */
      const line = (arr, color, width, dash) => {
        ctx.save();
        ctx.strokeStyle = color; ctx.lineWidth = width || 1.2;
        if (dash) ctx.setLineDash(dash);
        ctx.beginPath();
        let started = false;
        for (let k = 0; k < bars.length; k++) {
          const v = arr[from + k];
          if (!isNum(v)) { started = false; continue; }
          const x = main.x + (k + 0.5) * barW, y = yOf(v);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
        ctx.restore();
      };
      if (this.opts.showMA) {
        line(this.ma.ma5, PALETTE.ma5); line(this.ma.ma10, PALETTE.ma10);
        line(this.ma.ma20, PALETTE.ma20); line(this.ma.ma60, PALETTE.ma60);
      }
      if (this.opts.showBOLL) {
        line(this.boll.up, PALETTE.bollBand, 1, [3, 3]);
        line(this.boll.mid, PALETTE.bollMid, 1.1);
        line(this.boll.low, PALETTE.bollBand, 1, [3, 3]);
      }

      /* ===== AI 选股叠加层 ===== */
      if (plan) {
        // 线名与颜色都按方向走：离场计划里「stop」在上方（涨破则判断失效，属上行方向），
        // 「目标」在下方（下行参考）。照买入计划的标签画，用户会把 42.04 读成买入价上的止损。
        const isExit = plan.direction === 'exit';
        const pm = plan.labels || {};
        const planRows = isExit ? [
          ['entry', pm.entry || '参考价', PALETTE.planEntry || '#4d8dff'],
          ['target1', pm.target1 || '下行目标1', downColor],
          ['target2', pm.target2 || '下行目标2', downColor],
          ['stop', pm.stop || '离场失效价', upColor],
        ] : [
          ['entry', pm.entry || '建议买入', PALETTE.planEntry || '#4d8dff'],
          ['target1', pm.target1 || '目标1', upColor],
          ['target2', pm.target2 || '目标2', upColor],
          ['stop', pm.stop || '止损', downColor],
        ];
        ctx.save();
        ctx.setLineDash([5, 4]);
        ctx.lineWidth = 1;
        planRows.forEach(([k, label, color]) => {
          const v = plan[k];
          if (!isNum(v) || v <= 0) return;
          const y = Math.round(yOf(v)) + 0.5;
          if (y < main.y + 2 || y > main.y + main.h - 2) return;
          ctx.strokeStyle = color;
          ctx.beginPath(); ctx.moveTo(main.x, y); ctx.lineTo(main.x + (bars.length + extra) * barW, y); ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = '10px -apple-system, sans-serif';
          const text = label + ' ' + F.price(v, this.opts.market, 2);
          const tw = ctx.measureText(text).width + 8;
          ctx.fillStyle = PALETTE.tipBg || 'rgba(20,24,31,0.78)';
          ctx.fillRect(main.x + 4, y - 13, tw, 13);
          ctx.fillStyle = color;
          ctx.textAlign = 'left';
          ctx.fillText(text, main.x + 8, y - 3);
          ctx.setLineDash([5, 4]);
        });
        ctx.restore();
      }

      if (extra) {
        /* 预测带：从最后一根收盘价锚点出发，画到右侧预留槽位 */
        const anchor = bars[bars.length - 1].close;
        const xs = (j) => main.x + (bars.length + j + 0.5) * barW;
        const upArea = fPath.map((p, j) => ({ x: xs(j), y: yOf(isNum(p.hi) ? p.hi : anchor) }));
        const loArea = fPath.map((p, j) => ({ x: xs(j), y: yOf(isNum(p.lo) ? p.lo : anchor) }));
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(main.x + (bars.length - 0.5) * barW, yOf(anchor));
        upArea.forEach((p) => ctx.lineTo(p.x, p.y));
        for (let j = loArea.length - 1; j >= 0; j--) ctx.lineTo(loArea[j].x, loArea[j].y);
        ctx.closePath();
        ctx.fillStyle = PALETTE.forecastFill || 'rgba(130,80,223,0.16)';
        ctx.fill();
        /* 中位路径 */
        ctx.strokeStyle = PALETTE.forecastLine || '#8250df';
        ctx.lineWidth = 1.4;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(main.x + (bars.length - 0.5) * barW, yOf(anchor));
        fPath.forEach((p, j) => ctx.lineTo(xs(j), yOf(isNum(p.mid) ? p.mid : anchor)));
        ctx.stroke();
        ctx.setLineDash([]);
        /* 末端标注预测区间 */
        const lastP = fPath[fPath.length - 1];
        if (lastP) {
          ctx.font = '10px -apple-system, sans-serif';
          ctx.fillStyle = PALETTE.forecastLine || '#8250df';
          ctx.textAlign = 'center';
          ctx.fillText('预测', xs(fPath.length - 1), main.y + 11);
          const txt = F.price(lastP.mid, this.opts.market, 2);
          ctx.textAlign = 'right';
          ctx.fillText(txt, main.x + innerW - 2, yOf(lastP.mid) - 4);
        }
        ctx.restore();
      }

      /* 买卖标记（带文字标签的为 AI 建议） */
      const allMarks = (this.marks || []).concat((ad && ad.marks) || []);
      const labelSeen = {};
      allMarks.forEach((m) => {
        const k = m.idx - from;
        if (k < 0 || k >= bars.length) return;
        const cx = main.x + (k + 0.5) * barW;
        const isBuy = m.dir === 'buy';
        const y = isBuy ? yOf(bars[k].low) + 11 : yOf(bars[k].high) - 11;
        ctx.fillStyle = m.color || (isBuy ? upColor : downColor);
        ctx.beginPath();
        if (isBuy) { ctx.moveTo(cx, y - 5); ctx.lineTo(cx - 4.5, y + 3); ctx.lineTo(cx + 4.5, y + 3); }
        else { ctx.moveTo(cx, y + 5); ctx.lineTo(cx - 4.5, y - 3); ctx.lineTo(cx + 4.5, y - 3); }
        ctx.closePath(); ctx.fill();
        if (m.label) {
          /* 只画第一个标签，避免密集标记把图糊住 */
          if (labelSeen[m.label]) return;
          labelSeen[m.label] = true;
          ctx.font = '10px -apple-system, sans-serif';
          ctx.textAlign = 'center';
          const tw = ctx.measureText(m.label).width + 10;
          const ly = isBuy ? y + 6 : y - 20;
          ctx.fillStyle = m.color || (isBuy ? upColor : downColor);
          const bx = Math.max(main.x + 1, Math.min(cx - tw / 2, main.x + innerW - tw - 1));
          ctx.fillRect(bx, ly, tw, 14);
          ctx.fillStyle = '#fff';
          ctx.fillText(m.label, bx + tw / 2, ly + 10.5);
        }
      });

      /* 成交量 */
      const vols = bars.map((b) => b.volume || 0);
      const vmax = Math.max.apply(null, vols.concat([1]));
      ctx.strokeStyle = PALETTE.grid;
      ctx.beginPath(); ctx.moveTo(vol.x, vol.y + vol.h + 0.5); ctx.lineTo(vol.x + vol.w, vol.y + vol.h + 0.5); ctx.stroke();
      bars.forEach((b, k) => {
        const cx = vol.x + (k + 0.5) * barW;
        const rising = b.close >= b.open;
        ctx.fillStyle = rising ? PALETTE.volUp : PALETTE.volDown;
        const bh = Math.max(1, ((b.volume || 0) / vmax) * (vol.h - 4));
        ctx.fillRect(Math.round(cx - bodyW / 2), Math.round(vol.y + vol.h - bh), Math.round(bodyW), Math.round(bh));
      });
      const v5 = window.AD.ind.SMA(vols.map((v, i) => this.bars[from + i] ? this.bars[from + i].volume || 0 : v), 5);
      ctx.fillStyle = PALETTE.axis;
      ctx.fillText(F.vol(vmax, this.opts.market), vol.x + vol.w + 6, vol.y + 8);
      ctx.fillText('VOL', vol.x + 2, vol.y + 8);

      /* 副图 */
      if (sub) this.drawSub(sub, bars, from, barW, bodyW);

      /* 时间轴 */
      ctx.fillStyle = PALETTE.axis;
      ctx.textAlign = 'center';
      const labelN = Math.max(2, Math.min(7, Math.floor(innerW / 82)));
      const stepK = Math.max(1, Math.floor(bars.length / labelN));
      for (let k = 0; k < bars.length; k += stepK) {
        const b = bars[k];
        const x = main.x + (k + 0.5) * barW;
        const label = this.opts.period === 'day' || this.opts.period === 'week' || this.opts.period === 'month'
          ? F.date(b.t) : F.timeOf(b.t);
        ctx.fillText(label, x, h - 9);
      }

      /* 十字光标 */
      if (this.hover >= 0 && this.hover >= from && this.hover < from + bars.length) {
        const k = this.hover - from;
        const cx = Math.round(main.x + (k + 0.5) * barW) + 0.5;
        ctx.save();
        ctx.strokeStyle = PALETTE.cross;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(cx, main.y); ctx.lineTo(cx, sub ? sub.y + sub.h : vol.y + vol.h); ctx.stroke();
        ctx.restore();
        const hb = bars[k];
        const y = yOf(hb.close);
        ctx.fillStyle = '#0c0e13';
        ctx.beginPath();
        ctx.arc(cx, y, 3, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = dirColor(hb.close - hb.open);
        ctx.stroke();
      }
    }

    drawSub(sub, bars, from, barW, bodyW) {
      const ctx = this.ctx;
      const key = this.opts.sub;
      const rect = (arr) => {
        let mx = -Infinity, mn = Infinity;
        for (let i = from; i < from + bars.length; i++) {
          const v = arr[i];
          if (isNum(v)) { mx = Math.max(mx, v); mn = Math.min(mn, v); }
        }
        if (mx === -Infinity) { mx = 1; mn = -1; }
        if (mx === mn) { mx += 1; mn -= 1; }
        const p = (mx - mn) * 0.1 || 0.1;
        return { mx: mx + p, mn: mn - p };
      };
      ctx.strokeStyle = PALETTE.gridSoft;
      ctx.beginPath(); ctx.moveTo(sub.x, sub.y + 0.5); ctx.lineTo(sub.x + sub.w, sub.y + 0.5); ctx.stroke();

      const drawLine = (arr, color, yOf, width) => {
        ctx.strokeStyle = color; ctx.lineWidth = width || 1.1;
        ctx.beginPath();
        let started = false;
        for (let i = from; i < from + bars.length; i++) {
          const v = arr[i];
          if (!isNum(v)) { started = false; continue; }
          const x = sub.x + (i - from + 0.5) * barW, y = yOf(v);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.stroke();
      };

      if (key === 'MACD') {
        const r = rect(this.macd.macd.concat(this.macd.dif, this.macd.dea));
        const yOf = (v) => sub.y + sub.h - ((v - r.mn) / (r.mx - r.mn)) * sub.h;
        const zero = yOf(0);
        ctx.strokeStyle = PALETTE.grid; ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(sub.x, zero); ctx.lineTo(sub.x + sub.w, zero); ctx.stroke();
        ctx.setLineDash([]);
        for (let i = from; i < from + bars.length; i++) {
          const v = this.macd.macd[i];
          if (!isNum(v)) continue;
          const x = sub.x + (i - from + 0.5) * barW;
          ctx.fillStyle = v >= 0 ? PALETTE.macdUp : PALETTE.macdDown;
          const y = yOf(v);
          ctx.fillRect(Math.round(x - bodyW / 2), Math.round(Math.min(y, zero)),
            Math.round(bodyW), Math.max(1, Math.abs(y - zero)));
        }
        drawLine(this.macd.dif, PALETTE.dif, yOf);
        drawLine(this.macd.dea, PALETTE.dea, yOf);
        ctx.fillStyle = PALETTE.axis;
        ctx.textAlign = 'left';
        ctx.fillText('MACD(12,26,9)', sub.x + 2, sub.y + 8);
        ctx.textAlign = 'left';
        ctx.fillText(F.num(r.mx, 2) + ' / ' + F.num(r.mn, 2), sub.x + sub.w + 6, sub.y + 8);
      } else if (key === 'KDJ') {
        const arr = this.kdj.K.concat(this.kdj.D, this.kdj.J).filter(isNum);
        const mx = Math.max.apply(null, arr.concat([100])), mn = Math.min.apply(null, arr.concat([0]));
        const yOf = (v) => sub.y + sub.h - ((v - mn) / (mx - mn)) * sub.h;
        drawLine(this.kdj.K, PALETTE.k, yOf);
        drawLine(this.kdj.D, PALETTE.d, yOf);
        drawLine(this.kdj.J, PALETTE.j, yOf);
        ctx.fillStyle = PALETTE.axis;
        ctx.textAlign = 'left';
        ctx.fillText('KDJ(9,3,3)', sub.x + 2, sub.y + 8);
        ctx.fillText('100', sub.x + sub.w + 6, yOf(100));
        ctx.fillText('0', sub.x + sub.w + 6, yOf(0));
      } else if (key === 'RSI') {
        const yOf = (v) => sub.y + sub.h - ((v - 0) / (100 - 0)) * sub.h;
        [30, 50, 70].forEach((lv) => {
          ctx.strokeStyle = PALETTE.gridSoft;
          ctx.setLineDash([2, 4]);
          ctx.beginPath(); ctx.moveTo(sub.x, yOf(lv)); ctx.lineTo(sub.x + sub.w, yOf(lv)); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = PALETTE.axis;
          ctx.fillText(String(lv), sub.x + sub.w + 6, yOf(lv));
        });
        drawLine(this.rsi.rsi6, PALETTE.rsi6, yOf);
        drawLine(this.rsi.rsi14, PALETTE.rsi14, yOf);
        ctx.fillStyle = PALETTE.axis;
        ctx.fillText('RSI(6,14)', sub.x + 2, sub.y + 8);
      }
    }

    /* 顶部指标图例（随光标变化） */
    _syncLegend(idx) {
      if (!this.opts.onLegend) return;
      const i = (idx === null || idx === undefined) ? this.bars.length - 1 : idx;
      const rows = [];
      const b = this.bars[i];
      if (b) {
        rows.push({
          text: b.t + '   开 ' + F.price(b.open, this.opts.market) + '  高 ' + F.price(b.high, this.opts.market) +
            '  低 ' + F.price(b.low, this.opts.market) + '  收 ' + F.price(b.close, this.opts.market) +
            '  量 ' + F.vol(b.volume, this.opts.market) +
            (isNum(b.changePct) ? '  ' + F.pct(b.changePct) : ''),
          color: '#939db0',
        });
      }
      if (this.opts.showMA) {
        rows.push({
          text: 'MA5 ' + F.num(this.ma.ma5[i], 2) + '   MA10 ' + F.num(this.ma.ma10[i], 2) +
            '   MA20 ' + F.num(this.ma.ma20[i], 2) + '   MA60 ' + F.num(this.ma.ma60[i], 2),
          color: PALETTE.ma5,
        });
      }
      if (this.opts.showBOLL) {
        rows.push({
          text: 'BOLL 上 ' + F.num(this.boll.up[i], 2) + '  中 ' + F.num(this.boll.mid[i], 2) +
            '  下 ' + F.num(this.boll.low[i], 2),
          color: PALETTE.bollMid,
        });
      }
      const sub = this.opts.sub;
      if (sub === 'MACD') {
        rows.push({
          text: 'DIF ' + F.num(this.macd.dif[i], 3) + '   DEA ' + F.num(this.macd.dea[i], 3) +
            '   MACD ' + F.num(this.macd.macd[i], 3),
          color: PALETTE.dif,
        });
      } else if (sub === 'KDJ') {
        rows.push({
          text: 'K ' + F.num(this.kdj.K[i], 2) + '   D ' + F.num(this.kdj.D[i], 2) + '   J ' + F.num(this.kdj.J[i], 2),
          color: PALETTE.k,
        });
      } else if (sub === 'RSI') {
        rows.push({ text: 'RSI6 ' + F.num(this.rsi.rsi6[i], 2) + '   RSI14 ' + F.num(this.rsi.rsi14[i], 2), color: PALETTE.rsi14 });
      }
      this.opts.onLegend(rows, b);
    }

    _showTip(x, y, idx) {
      const b = this.bars[idx];
      if (!b) return;
      const rows = [
        '<b>' + b.t + '</b>',
        '开 ' + F.price(b.open, this.opts.market) + '　收 ' + F.price(b.close, this.opts.market),
        '高 ' + F.price(b.high, this.opts.market) + '　低 ' + F.price(b.low, this.opts.market),
        '涨跌 ' + (isNum(b.changePct) ? F.pct(b.changePct) : (isNum(b.open) ? F.pct((b.close / b.open - 1) * 100) : '—')),
        '量 ' + F.vol(b.volume, this.opts.market) + (isNum(b.amount) ? '　额 ' + F.amt(b.amount, this.opts.market) : ''),
      ];
      this.tip.innerHTML = rows.join('<br>');
      this.tip.classList.remove('hidden');
      const wrapW = this.wrap.clientWidth;
      const tw = this.tip.offsetWidth || 170;
      const left = x + 14 + tw > wrapW ? x - tw - 14 : x + 14;
      this.tip.style.left = Math.max(4, left) + 'px';
      this.tip.style.top = Math.max(4, Math.min(y - 10, this.opts.height - (this.tip.offsetHeight || 90) - 6)) + 'px';
    }

    destroy() {
      if (this._ro) this._ro.disconnect();
      this.wrap.innerHTML = '';
    }
  }

  /* ==================================================================
     分时图
     ================================================================== */

  class TrendChart {
    constructor(wrap, opts) {
      this.wrap = wrap;
      this.opts = Object.assign({ height: 300, prevClose: null, onLegend: null, market: 'cn' }, opts || {});
      wrap.classList.add('chart-canvas-wrap');
      const c = baseCanvas(wrap);
      this.canvas = c.canvas; this.ctx = c.ctx;
      this.points = [];
      this.hover = -1;
      this.tip = document.createElement('div');
      this.tip.className = 'chart-tip hidden';
      wrap.appendChild(this.tip);
      this._bind();
      this._ro = new ResizeObserver(() => this.render());
      this._ro.observe(wrap);
    }

    setData(points, opts) {
      this.points = points || [];
      if (opts && opts.prevClose) this.opts.prevClose = opts.prevClose;
      const last = this.points.length ? this.points[this.points.length - 1].price : null;
      if (!this.opts.prevClose && this.points.length) this.opts.prevClose = this.points[0].price;
      if (last && !this._lockedPrev && !opts.prevClose) this.opts.prevClose = this.opts.prevClose || last;
      this.render();
      this._syncLegend(this.points.length - 1);
    }

    setPrevClose(v) { if (isNum(v)) { this.opts.prevClose = v; this._lockedPrev = true; this.render(); } }

    _bind() {
      const canvas = this.canvas;
      canvas.addEventListener('mousemove', (e) => {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const idx = this._xToIdx(x);
        if (idx !== this.hover) { this.hover = idx; this.render(); this._syncLegend(idx); }
        const p = this.points[idx];
        if (!p) return;
        const pc = this.opts.prevClose;
        const pct = pc ? (p.price / pc - 1) * 100 : null;
        this.tip.innerHTML = '<b>' + p.t + '</b><br>价格 ' + F.price(p.price, this.opts.market) +
          (pct !== null ? '　' + F.pct(pct) : '') +
          (isNum(p.avg) ? '<br>均价 ' + F.price(p.avg, this.opts.market) : '') +
          '<br>量 ' + F.vol(p.volume, this.opts.market);
        this.tip.classList.remove('hidden');
        const tw = this.tip.offsetWidth || 150;
        const left = x + 14 + tw > this.wrap.clientWidth ? x - tw - 14 : x + 14;
        this.tip.style.left = Math.max(4, left) + 'px';
        this.tip.style.top = Math.max(4, Math.min(e.clientY - rect.top - 10, this.opts.height - 80)) + 'px';
      });
      canvas.addEventListener('mouseleave', () => {
        this.hover = -1; this.tip.classList.add('hidden'); this.render();
        this._syncLegend(this.points.length - 1);
      });
    }

    _xToIdx(x) {
      if (!this.geo || !this.points.length) return -1;
      const g = this.geo;
      const total = this._slots || this.points.length;
      const i = Math.round(((x - g.x0) / g.innerW) * (total - 1));
      return Math.max(0, Math.min(this.points.length - 1, i));
    }

    render() {
      syncColors();
      const { ctx, wrap } = this;
      const height = this.opts.height;
      const { w, h } = prepare(wrap, this.canvas, ctx, height);
      if (!this.points.length) {
        ctx.fillStyle = PALETTE.axis; ctx.textAlign = 'center';
        ctx.fillText('暂无分时数据', w / 2, h / 2);
        return;
      }
      const pad = { l: 6, r: 62, t: 12, b: 20 };
      const innerW = w - pad.l - pad.r;
      const innerH = h - pad.t - pad.b;
      const volH = innerH * 0.22;
      const gap = 8;
      const mainH = innerH - volH - gap;
      const main = { x: pad.l, y: pad.t, w: innerW, h: mainH };
      const vol = { x: pad.l, y: main.y + mainH + gap, w: innerW, h: volH };
      this.geo = { x0: main.x, innerW, main, vol };

      const pc = this.opts.prevClose;
      let hi = -Infinity, lo = Infinity;
      this.points.forEach((p) => {
        hi = Math.max(hi, p.price); lo = Math.min(lo, p.price);
        if (isNum(p.avg)) { hi = Math.max(hi, p.avg); lo = Math.min(lo, p.avg); }
      });
      if (pc) { hi = Math.max(hi, pc); lo = Math.min(lo, pc); }
      const span = Math.max(hi - lo, (pc || hi) * 0.004);
      const mid = pc || (hi + lo) / 2;
      const bound = Math.max(span, mid * 0.01);
      const top = mid + bound, bot = mid - bound;
      const yOf = (v) => main.y + main.h - ((v - bot) / (top - bot)) * main.h;

      /* 网格：以昨收为中轴，上下对称百分比 */
      ctx.strokeStyle = PALETTE.grid;
      ctx.textAlign = 'left';
      const ratios = [1, 0.5, 0, -0.5, -1];
      ratios.forEach((r) => {
        const v = mid + bound * r;
        const y = Math.round(yOf(v)) + 0.5;
        ctx.beginPath(); ctx.moveTo(main.x, y); ctx.lineTo(main.x + innerW, y); ctx.stroke();
        ctx.fillStyle = r === 0 ? PALETTE.text : PALETTE.axis;
        const pctLabel = pc ? ((v / pc - 1) * 100).toFixed(2) + '%' : '';
        ctx.fillText(F.price(v, this.opts.market, 2), main.x + innerW + 6, y);
        ctx.textAlign = 'right';
        ctx.fillText(pctLabel, main.x + innerW - 4, y - 8);
        ctx.textAlign = 'left';
      });

      const total = Math.max(this.points.length, 240);
      this._slots = total;
      const stepX = innerW / (total - 1);

      /* 价格面积 */
      const grad = ctx.createLinearGradient(0, main.y, 0, main.y + main.h);
      const rising = pc ? this.points[this.points.length - 1].price >= pc : true;
      const mainColor = rising ? upColor : downColor;
      grad.addColorStop(0, rising ? 'rgba(255,77,79,0.22)' : 'rgba(18,196,139,0.22)');
      grad.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.beginPath();
      this.points.forEach((p, i) => {
        const x = main.x + i * stepX, y = yOf(p.price);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      const lastX = main.x + (this.points.length - 1) * stepX;
      ctx.lineTo(lastX, main.y + main.h);
      ctx.lineTo(main.x, main.y + main.h);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      if (pc) {
        ctx.save();
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = 'rgba(147,157,176,0.45)';
        ctx.beginPath(); ctx.moveTo(main.x, yOf(pc)); ctx.lineTo(main.x + innerW, yOf(pc)); ctx.stroke();
        ctx.restore();
      }

      ctx.strokeStyle = mainColor; ctx.lineWidth = 1.4;
      ctx.beginPath();
      this.points.forEach((p, i) => {
        const x = main.x + i * stepX, y = yOf(p.price);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();

      /* 均价线 */
      ctx.strokeStyle = PALETTE.avg; ctx.lineWidth = 1.1;
      ctx.beginPath();
      let started = false;
      this.points.forEach((p, i) => {
        if (!isNum(p.avg)) { started = false; return; }
        const x = main.x + i * stepX, y = yOf(p.avg);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      });
      ctx.stroke();

      /* 成交量 */
      const vmax = Math.max.apply(null, this.points.map((p) => p.volume || 0).concat([1]));
      const bw = Math.max(1, stepX * 0.7);
      this.points.forEach((p, i) => {
        const x = main.x + i * stepX;
        const bh = Math.max(1, ((p.volume || 0) / vmax) * (vol.h - 4));
        ctx.fillStyle = (pc && p.price < pc) ? PALETTE.volDown : PALETTE.volUp;
        ctx.fillRect(Math.round(x - bw / 2), Math.round(vol.y + vol.h - bh), Math.round(bw), Math.round(bh));
      });
      ctx.fillStyle = PALETTE.axis;
      ctx.fillText('VOL ' + F.vol(vmax, this.opts.market), vol.x + 2, vol.y + 8);

      /* 时间轴 */
      ctx.fillStyle = PALETTE.axis;
      ctx.textAlign = 'center';
      const marks = [0, 0.25, 0.5, 0.75, 1];
      marks.forEach((m) => {
        const i = Math.round(m * (total - 1));
        const x = main.x + i * stepX;
        const p = this.points[Math.min(i, this.points.length - 1)];
        ctx.fillText(p ? F.timeOf(p.t) : '', Math.min(main.x + innerW - 14, Math.max(main.x + 14, x)), h - 9);
      });

      if (this.hover >= 0) {
        const i = this.hover;
        const x = Math.round(main.x + i * stepX) + 0.5;
        const y = yOf(this.points[i].price);
        ctx.save();
        ctx.strokeStyle = PALETTE.cross; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, main.y); ctx.lineTo(x, vol.y + vol.h); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(main.x, y); ctx.lineTo(main.x + innerW, y); ctx.stroke();
        ctx.restore();
        ctx.fillStyle = '#0c0e13';
        ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = mainColor; ctx.stroke();
      }
    }

    _syncLegend(idx) {
      if (!this.opts.onLegend) return;
      const p = this.points[idx];
      if (!p) return;
      const pc = this.opts.prevClose;
      const pct = pc ? (p.price / pc - 1) * 100 : null;
      this.opts.onLegend([{
        text: F.timeOf(p.t) + '　价 ' + F.price(p.price, this.opts.market) +
          (pct !== null ? '　' + F.pct(pct) : '') +
          (isNum(p.avg) ? '　均价 ' + F.price(p.avg, this.opts.market) : '') +
          '　量 ' + F.vol(p.volume, this.opts.market),
        color: pct !== null && pct < 0 ? downColor : upColor,
      }], null);
    }

    destroy() { if (this._ro) this._ro.disconnect(); this.wrap.innerHTML = ''; }
  }

  /* ==================================================================
     通用折线图（资金曲线 / 资金流）
     ================================================================== */

  class LineChart {
    constructor(wrap, opts) {
      this.wrap = wrap;
      this.opts = Object.assign({ height: 220, series: [], zeroLine: false, fmt: (v) => F.num(v, 2) }, opts || {});
      wrap.classList.add('chart-canvas-wrap');
      const c = baseCanvas(wrap);
      this.canvas = c.canvas; this.ctx = c.ctx;
      this.tip = document.createElement('div');
      this.tip.className = 'chart-tip hidden';
      wrap.appendChild(this.tip);
      this._ro = new ResizeObserver(() => this.render());
      this._ro.observe(wrap);
      this.hover = -1;
      this._bind();
    }

    setData(series) { this.opts.series = series || []; this.render(); }

    _bind() {
      this.canvas.addEventListener('mousemove', (e) => {
        const rect = this.canvas.getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        const s0 = this.opts.series[0];
        if (!s0 || !s0.data.length || !this.geo) return;
        const g = this.geo;
        const i = Math.max(0, Math.min(s0.data.length - 1,
          Math.round(((x - g.x0) / g.innerW) * (s0.data.length - 1))));
        this.hover = i;
        this.render();
        const rows = ['<b>' + s0.data[i].t + '</b>'];
        this.opts.series.forEach((s) => {
          const d = s.data[i];
          if (d) rows.push(s.name + ' ' + (s.fmt ? s.fmt(d.v) : F.num(d.v, 2)));
        });
        this.tip.innerHTML = rows.join('<br>');
        this.tip.classList.remove('hidden');
        const tw = this.tip.offsetWidth || 150;
        this.tip.style.left = Math.max(4, (x + 14 + tw > this.wrap.clientWidth ? x - tw - 14 : x + 14)) + 'px';
        this.tip.style.top = Math.max(4, y - 10) + 'px';
      });
      this.canvas.addEventListener('mouseleave', () => {
        this.hover = -1; this.tip.classList.add('hidden'); this.render();
      });
    }

    render() {
      syncColors();
      const { ctx, wrap } = this;
      const { w, h } = prepare(wrap, this.canvas, ctx, this.opts.height);
      const series = this.opts.series.filter((s) => s.data && s.data.length);
      if (!series.length) {
        ctx.fillStyle = PALETTE.axis; ctx.textAlign = 'center';
        ctx.fillText('暂无数据', w / 2, h / 2);
        return;
      }
      const pad = { l: 6, r: 66, t: 14, b: 20 };
      const innerW = w - pad.l - pad.r, innerH = h - pad.t - pad.b;
      this.geo = { x0: pad.l, innerW };
      let hi = -Infinity, lo = Infinity;
      series.forEach((s) => s.data.forEach((d) => {
        hi = Math.max(hi, d.v); lo = Math.min(lo, d.v);
      }));
      if (this.opts.zeroLine) { hi = Math.max(hi, 0); lo = Math.min(lo, 0); }
      if (hi === lo) { hi += 1; lo -= 1; }
      const p = (hi - lo) * 0.08;
      hi += p; lo -= p;
      const yOf = (v) => pad.t + innerH - ((v - lo) / (hi - lo)) * innerH;
      const n = series[0].data.length;
      const xOf = (i) => pad.l + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW);

      const ticks = niceTicks(lo, hi, 4);
      ctx.strokeStyle = PALETTE.grid;
      ctx.textAlign = 'left';
      ticks.forEach((t) => {
        const y = Math.round(yOf(t)) + 0.5;
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + innerW, y); ctx.stroke();
        ctx.fillStyle = PALETTE.axis;
        ctx.fillText(this.opts.fmt(t), pad.l + innerW + 6, y);
      });
      if (this.opts.zeroLine && lo < 0 && hi > 0) {
        ctx.save(); ctx.setLineDash([3, 3]); ctx.strokeStyle = 'rgba(147,157,176,.4)';
        const y = Math.round(yOf(0)) + 0.5;
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + innerW, y); ctx.stroke();
        ctx.restore();
      }

      series.forEach((s) => {
        ctx.strokeStyle = s.color || PALETTE.dif;
        ctx.lineWidth = s.width || 1.5;
        ctx.beginPath();
        s.data.forEach((d, i) => {
          const x = xOf(i), y = yOf(d.v);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        if (s.fill) {
          const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + innerH);
          grad.addColorStop(0, s.fill);
          grad.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.lineTo(xOf(s.data.length - 1), pad.t + innerH);
          ctx.lineTo(pad.l, pad.t + innerH);
          ctx.closePath();
          ctx.fillStyle = grad;
          ctx.fill();
        }
      });

      ctx.fillStyle = PALETTE.axis;
      ctx.textAlign = 'center';
      const labelN = Math.max(2, Math.min(6, Math.floor(innerW / 90)));
      const step = Math.max(1, Math.floor(n / labelN));
      for (let i = 0; i < n; i += step) {
        ctx.fillText(F.date(series[0].data[i].t), xOf(i), h - 9);
      }

      if (this.hover >= 0 && this.hover < n) {
        const x = Math.round(xOf(this.hover)) + 0.5;
        ctx.save();
        ctx.strokeStyle = PALETTE.cross; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + innerH); ctx.stroke();
        ctx.restore();
      }
    }

    destroy() { if (this._ro) this._ro.disconnect(); this.wrap.innerHTML = ''; }
  }

  window.AD = window.AD || {};
  window.AD.chart = {
    kline: (wrap, opts) => new KlineChart(wrap, opts),
    trend: (wrap, opts) => new TrendChart(wrap, opts),
    line: (wrap, opts) => new LineChart(wrap, opts),
    palette: PALETTE,
  };
})();
