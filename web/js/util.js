/* ==========================================================================
   AlphaDesk · 基础工具层
   命名空间：window.AD = { fmt, dom, store, bus, util, session }
   ========================================================================== */
(function () {
  'use strict';

  const PREFIX = 'alphadesk:';

  /* ----------------------------------------------------------- 格式化 */

  const isNum = (v) => typeof v === 'number' && isFinite(v);

  function digitsFor(price, market) {
    if (!isNum(price)) return 2;
    const a = Math.abs(price);
    if (a >= 1000) return 2;
    if (a >= 1) return market === 'us' ? 2 : 2;
    if (a >= 0.1) return 3;
    return 4;
  }

  const fmt = {
    num(v, d) {
      if (!isNum(v)) return '—';
      return v.toFixed(d === undefined ? 2 : d);
    },
    price(v, market, d) {
      if (!isNum(v)) return '—';
      const dd = d === undefined ? digitsFor(v, market) : d;
      return v.toLocaleString('en-US', { minimumFractionDigits: dd, maximumFractionDigits: dd });
    },
    pct(v, d) {
      if (!isNum(v)) return '—';
      const dd = d === undefined ? 2 : d;
      return (v > 0 ? '+' : '') + v.toFixed(dd) + '%';
    },
    pctPlain(v, d) {
      if (!isNum(v)) return '—';
      const dd = d === undefined ? 2 : d;
      return (v > 0 ? '+' : '') + v.toFixed(dd);
    },
    signed(v, d) {
      if (!isNum(v)) return '—';
      const dd = d === undefined ? 2 : d;
      return (v > 0 ? '+' : '') + v.toFixed(dd);
    },
    /* 成交额：CN 用人民币万/亿，US 用美元 K/M/B */
    amt(v, market) {
      if (!isNum(v)) return '—';
      if (market === 'us') {
        if (Math.abs(v) >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
        if (Math.abs(v) >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
        if (Math.abs(v) >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
        return '$' + v.toFixed(2);
      }
      if (Math.abs(v) >= 1e12) return (v / 1e12).toFixed(2) + '万亿';
      if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿';
      if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(1) + '万';
      return v.toFixed(0);
    },
    /* 成交量：CN 输入为「手」，US 输入为「股」 */
    vol(v, market) {
      if (!isNum(v)) return '—';
      if (market === 'us') {
        if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(2) + 'B';
        if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(2) + 'M';
        if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + 'K';
        return v.toFixed(0);
      }
      if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿手';
      if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(2) + '万手';
      return v.toFixed(0) + '手';
    },
    cap(v, market) {
      if (!isNum(v)) return '—';
      if (market === 'us') {
        if (Math.abs(v) >= 1e12) return '$' + (v / 1e12).toFixed(2) + 'T';
        if (Math.abs(v) >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B';
        return '$' + (v / 1e6).toFixed(0) + 'M';
      }
      if (Math.abs(v) >= 1e12) return (v / 1e12).toFixed(2) + '万亿';
      if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(1) + '亿';
      return (v / 1e4).toFixed(0) + '万';
    },
    ratio(v, d) {
      if (!isNum(v)) return '—';
      return v.toFixed(d === undefined ? 2 : d);
    },
    dir(v) { return !isNum(v) || v === 0 ? 'flat' : (v > 0 ? 'up' : 'down'); },
    arrow(v) { return !isNum(v) || v === 0 ? '·' : (v > 0 ? '▲' : '▼'); },
    date(t) { return (t || '').slice(5, 10); },
    timeOf(t) {
      if (!t) return '—';
      const s = String(t);
      if (s.length >= 16) return s.slice(11, 16);
      return s;
    },
    hhmmss(t) {
      if (!t) return '—';
      const s = String(t);
      if (s.length >= 19) return s.slice(11, 19);
      if (s.length >= 16) return s.slice(11, 16);
      return s.slice(-8);
    },
    /* 由秒 / 毫秒时间戳格式化 */
    clock(ts) {
      const d = new Date(ts);
      const p = (n) => String(n).padStart(2, '0');
      return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    },
    money(v, market) { return fmt.amt(v, market); },
  };

  /* -------------------------------------------------------------- DOM */

  function h(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'html') el.innerHTML = v;
        else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k === 'on' && typeof v === 'object') {
          for (const evt of Object.keys(v)) el.addEventListener(evt, v[evt]);
        } else if (k in el && k !== 'list' && typeof v !== 'object') {
          try { el[k] = v; } catch (e) { el.setAttribute(k, v); }
        } else {
          el.setAttribute(k, v);
        }
      }
    }
    if (children !== null && children !== undefined) {
      const list = Array.isArray(children) ? children : [children];
      for (const c of list) {
        if (c === null || c === undefined || c === false) continue;
        el.appendChild(typeof c === 'string' || typeof c === 'number'
          ? document.createTextNode(String(c)) : c);
      }
    }
    return el;
  }

  const dom = {
    h,
    frag(children) {
      const f = document.createDocumentFragment();
      (Array.isArray(children) ? children : [children]).forEach((c) => {
        if (c !== null && c !== undefined) f.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      });
      return f;
    },
    clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); return el; },
    q(sel, root) { return (root || document).querySelector(sel); },
    qa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); },
    /* 涨跌着色文本 */
    pct(v, opts) {
      const o = opts || {};
      return h('span', {
        class: (v === null || v === undefined ? 'flat' : fmt.dir(v)) + (o.cls ? ' ' + o.cls : ''),
        text: o.raw ? fmt.pctPlain(v, o.d) : fmt.pct(v, o.d),
      });
    },
    numSpan(v, cls) {
      return h('span', { class: 'num ' + (cls || ''), text: fmt.num(v) });
    },
    chip(text, cls) { return h('span', { class: 'chip ' + (cls || ''), text }); },
    iconBtn(title, path, onClick) {
      const b = h('button', { class: 'icon-btn', title, on: { click: onClick } });
      b.innerHTML = '<svg viewBox="0 0 16 16">' + path + '</svg>';
      return b;
    },
    /* 生成可排序表头 */
    th(label, key, opts) {
      const o = opts || {};
      const el = h('th', {
        class: (o.noSort ? 'no-sort ' : '') + (o.cls || ''),
        dataset: { key: key || '' },
        title: o.title || '',
      }, [label]);
      if (!o.noSort && key) {
        el.appendChild(h('span', { class: 'arrow', text: '↕' }));
      }
      return el;
    },
  };

  /* ------------------------------------------------------------ 存储 */

  const store = {
    get(key, def) {
      try {
        const raw = localStorage.getItem(PREFIX + key);
        if (raw === null) return def;
        return JSON.parse(raw);
      } catch (e) { return def; }
    },
    set(key, val) {
      try { localStorage.setItem(PREFIX + key, JSON.stringify(val)); } catch (e) { /* ignore */ }
      return val;
    },
    del(key) { try { localStorage.removeItem(PREFIX + key); } catch (e) { /* ignore */ } },
  };

  /* ------------------------------------------------------------ 事件 */

  const bus = {
    map: {},
    on(evt, fn) {
      (this.map[evt] = this.map[evt] || []).push(fn);
      return () => this.off(evt, fn);
    },
    off(evt, fn) {
      const arr = this.map[evt] || [];
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    emit(evt, payload) {
      (this.map[evt] || []).slice().forEach((fn) => {
        try { fn(payload); } catch (e) { console.error('[bus]', evt, e); }
      });
    },
  };

  /* ------------------------------------------------------------ 工具 */

  const util = {
    debounce(fn, ms) {
      let t = null;
      return function () {
        const args = arguments;
        clearTimeout(t);
        t = setTimeout(() => fn.apply(null, args), ms || 220);
      };
    },
    throttle(fn, ms) {
      let last = 0, timer = null;
      return function () {
        const args = arguments, now = Date.now();
        if (now - last >= (ms || 200)) { last = now; fn.apply(null, args); }
        else {
          clearTimeout(timer);
          timer = setTimeout(() => { last = Date.now(); fn.apply(null, args); }, ms - (now - last));
        }
      };
    },
    clamp(v, a, b) { return Math.min(b, Math.max(a, v)); },
    sum(arr) { return arr.reduce((a, b) => a + (isNum(b) ? b : 0), 0); },
    last(arr, n) { return arr.slice(Math.max(0, arr.length - (n || 1))); },
    pctChange(a, b) { return !a ? null : (b / a - 1) * 100; },
    /* 简单线性回归斜率（归一化），用于趋势判断 */
    slope(vals) {
      const n = vals.length;
      if (n < 3) return 0;
      let sx = 0, sy = 0, sxy = 0, sxx = 0;
      for (let i = 0; i < n; i++) { sx += i; sy += vals[i]; sxy += i * vals[i]; sxx += i * i; }
      const d = n * sxx - sx * sx;
      return d === 0 ? 0 : (n * sxy - sx * sy) / d;
    },
    fmtMonthDay(t) { return fmt.date(t); },
  };

  /* ------------------------------------------------------- 交易时段 */

  const CN_SESSIONS = [[9 * 60 + 15, 9 * 60 + 25], [9 * 60 + 30, 11 * 60 + 30], [13 * 60, 15 * 60]];
  const CN_NAMES = ['集合竞价', '上午交易', '下午交易'];

  function cnParts(d) {
    const p = (n) => Number(n);
    const s = d.toLocaleString('en-US', { timeZone: 'Asia/Shanghai', hour12: false });
    const m = s.match(/(\d+)\/(\d+)\/(\d+),?\s+(\d+):(\d+):(\d+)/);
    if (!m) return null;
    return { y: p(m[3]), mo: p(m[1]), d: p(m[2]), h: p(m[4]), mi: p(m[5]), week: new Date(Date.UTC(p(m[3]), p(m[1]) - 1, p(m[2]))).getUTCDay() };
  }

  function nyParts(d) {
    const s = d.toLocaleString('en-US', { timeZone: 'America/New_York', hour12: false });
    const m = s.match(/(\d+)\/(\d+)\/(\d+),?\s+(\d+):(\d+):(\d+)/);
    if (!m) return null;
    return { y: +m[3], mo: +m[1], d: +m[2], h: +m[4], mi: +m[5], week: new Date(Date.UTC(+m[3], +m[1] - 1, +m[2])).getUTCDay() };
  }

  const session = {
    /* 返回 { open, label, cls, detail } */
    cn(now) {
      const t = cnParts(now || new Date());
      if (!t) return { open: false, label: '未知', cls: 'closed', detail: '' };
      if (t.week === 0 || t.week === 6) return { open: false, label: '周末休市', cls: 'closed', detail: 'A股 09:30 开盘' };
      const mins = t.h * 60 + t.mi;
      if (mins >= 9 * 60 + 15 && mins < 9 * 60 + 25) return { open: true, label: '集合竞价', cls: 'pre', detail: '09:25 开盘' };
      if (mins >= 9 * 60 + 30 && mins < 11 * 60 + 30) return { open: true, label: '上午交易中', cls: 'open', detail: '11:30 休市' };
      if (mins >= 11 * 60 + 30 && mins < 13 * 60) return { open: false, label: '午间休市', cls: 'closed', detail: '13:00 复盘' };
      if (mins >= 13 * 60 && mins < 15 * 60) return { open: true, label: '下午交易中', cls: 'open', detail: '15:00 收盘' };
      if (mins >= 9 * 60 + 25 && mins < 9 * 60 + 30) return { open: true, label: '开盘集合', cls: 'pre', detail: '09:30 连续竞价' };
      return { open: false, label: '已收盘', cls: 'closed', detail: '下个交易日 09:30 开盘' };
    },
    us(now) {
      const t = nyParts(now || new Date());
      if (!t) return { open: false, label: '未知', cls: 'closed', detail: '' };
      if (t.week === 0 || t.week === 6) return { open: false, label: '周末休市', cls: 'closed', detail: '美东 09:30 开盘' };
      const mins = t.h * 60 + t.mi;
      if (mins >= 4 * 60 && mins < 9 * 60 + 30) return { open: true, label: '盘前交易', cls: 'pre', detail: '美东 09:30 开盘' };
      if (mins >= 9 * 60 + 30 && mins < 16 * 60) return { open: true, label: '盘中交易', cls: 'open', detail: '美东 16:00 收盘' };
      if (mins >= 16 * 60 && mins < 20 * 60) return { open: true, label: '盘后交易', cls: 'pre', detail: '美东 20:00 结束' };
      return { open: false, label: '已收盘', cls: 'closed', detail: '美东 09:30 开盘' };
    },
    get(market) { return market === 'us' ? this.us() : this.cn(); },
  };

  /* ------------------------------------------------------------ 导出 */

  window.AD = window.AD || {};
  Object.assign(window.AD, {
    fmt, dom, store, bus, util, session,
    isNum,
    MARKET_LABEL: { cn: 'A股', us: '美股' },
    MARKET_CURRENCY: { cn: '¥', us: '$' },
  });
})();
