/* ==========================================================================
   视图 · 预警中心（条件规则 + 后台轮询引擎 + 浏览器通知）
   引擎挂在 AD.alertEngine，由 app.js 启动
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct, paint, reconcile } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;
  const store = window.AD.store;

  const TYPES = [
    { value: 'pctAbove', label: '涨幅 ≥ N%', unit: '%', def: 5, hint: '当日涨幅达到阈值时提醒' },
    { value: 'pctBelow', label: '跌幅 ≤ -N%', unit: '%', def: 5, hint: '当日跌幅达到阈值时提醒' },
    { value: 'priceAbove', label: '价格上穿 N', unit: '', def: 0, hint: '最新价 >= 阈值' },
    { value: 'priceBelow', label: '价格下穿 N', unit: '', def: 0, hint: '最新价 <= 阈值' },
    { value: 'volumeRatioAbove', label: '量比 ≥ N', unit: '', def: 2, hint: '放量提醒（仅 A股数据源提供）' },
    { value: 'turnoverAbove', label: '换手率 ≥ N%', unit: '%', def: 10, hint: '高换手提醒' },
    { value: 'speedUp', label: '涨速 ≥ N%', unit: '%', def: 1, hint: '短线快速拉升' },
    { value: 'speedDown', label: '涨速 ≤ -N%', unit: '%', def: 1, hint: '短线快速跳水' },
    { value: 'macdGolden', label: '日线 MACD 金叉', unit: '', def: 0, hint: '基于日K收盘计算，每 5 分钟复核' },
    { value: 'macdDead', label: '日线 MACD 死叉', unit: '', def: 0, hint: '基于日K收盘计算' },
    { value: 'rsiOversold', label: 'RSI14 ≤ N', unit: '', def: 30, hint: '超卖区间' },
    { value: 'rsiOverbought', label: 'RSI14 ≥ N', unit: '', def: 70, hint: '超买区间' },
    { value: 'breakout20', label: '突破 20 日高点', unit: '', def: 0, hint: '收盘价创 20 日新高' },
  ];
  const TYPE_MAP = {};
  TYPES.forEach((t) => { TYPE_MAP[t.value] = t; });

  function typeDesc(rule) {
    const t = TYPE_MAP[rule.type] || { label: rule.type, unit: '' };
    if (t.unit || ['priceAbove', 'priceBelow', 'rsiOversold', 'rsiOverbought', 'volumeRatioAbove'].indexOf(rule.type) >= 0) {
      return t.label.replace('N', F.num(rule.threshold, 2));
    }
    return t.label;
  }

  /* ======================================================= 预警引擎 */

  const engine = {
    rules: [],
    logs: [],
    started: false,
    klineCache: {},
    sound: true,
    notify: false,
    _timerQuote: null,
    _timerKline: null,
    _ctx: null,

    load() {
      this.rules = store.get('alertRules', []);
      this.logs = store.get('alertLogs', []);
      this.sound = store.get('alertSound', true);
      return this.rules;
    },
    save() { store.set('alertRules', this.rules); },
    saveLogs() { store.set('alertLogs', this.logs.slice(0, 200)); },

    add(rule) {
      rule.id = 'r' + Date.now() + Math.random().toString(36).slice(2, 6);
      rule.enabled = true;
      rule.createdAt = Date.now();
      this.rules.push(rule);
      this.save();
      window.AD.bus.emit('alert:rules');
      return rule;
    },
    remove(id) {
      this.rules = this.rules.filter((r) => r.id !== id);
      this.save();
      window.AD.bus.emit('alert:rules');
    },
    toggle(id, enabled) {
      const r = this.rules.find((x) => x.id === id);
      if (r) { r.enabled = enabled; this.save(); window.AD.bus.emit('alert:rules'); }
    },
    clearLogs() { this.logs = []; this.saveLogs(); window.AD.bus.emit('alert:logs'); },

    beep() {
      if (!this.sound) return;
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        const ctx = new Ctx();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain); gain.connect(ctx.destination);
        osc.frequency.value = 880;
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.0001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.16, ctx.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.32);
        osc.start(); osc.stop(ctx.currentTime + 0.34);
        setTimeout(() => ctx.close(), 600);
      } catch (e) { /* 忽略音频异常 */ }
    },

    fire(rule, quote, message) {
      const now = Date.now();
      if (rule.lastTrigger && now - rule.lastTrigger < (rule.cooldownMin || 10) * 60000) return;
      rule.lastTrigger = now;
      this.save();
      const log = {
        ts: now, ruleId: rule.id, market: rule.market, code: rule.code,
        name: rule.name || rule.code, type: rule.type, desc: typeDesc(rule),
        message, price: quote && quote.price, changePct: quote && quote.changePct, note: rule.note || '',
      };
      this.logs.unshift(log);
      this.saveLogs();
      window.AD.bus.emit('alert:trigger', log);
      this.beep();
      if (this.notify && 'Notification' in window && Notification.permission === 'granted') {
        try {
          new Notification('AlphaDesk 预警 · ' + log.name, { body: log.message });
        } catch (e) { /* 忽略 */ }
      }
    },

    evalQuoteRules(rule, quote) {
      if (!quote || !isFinite(quote.price)) return;
      const th = Number(rule.threshold) || 0;
      const c = quote.changePct;
      const msg = (txt) => log_text(rule, quote, txt);
      switch (rule.type) {
        case 'pctAbove':
          if (isFinite(c) && c >= th) this.fire(rule, quote, msg('涨幅 ' + F.pct(c) + ' 达到阈值 ' + th + '%'));
          break;
        case 'pctBelow':
          if (isFinite(c) && c <= -Math.abs(th)) this.fire(rule, quote, msg('跌幅 ' + F.pct(c) + ' 触发阈值 -' + Math.abs(th) + '%'));
          break;
        case 'priceAbove':
          if (quote.price >= th) this.fire(rule, quote, msg('价格 ' + F.price(quote.price, rule.market) + ' 上穿 ' + th));
          break;
        case 'priceBelow':
          if (quote.price <= th) this.fire(rule, quote, msg('价格 ' + F.price(quote.price, rule.market) + ' 下穿 ' + th));
          break;
        case 'volumeRatioAbove':
          if (isFinite(quote.volumeRatio) && quote.volumeRatio >= th) this.fire(rule, quote, msg('量比 ' + F.num(quote.volumeRatio, 2) + ' 达到阈值 ' + th));
          break;
        case 'turnoverAbove':
          if (isFinite(quote.turnover) && quote.turnover >= th) this.fire(rule, quote, msg('换手率 ' + F.num(quote.turnover, 2) + '% 达到阈值 ' + th + '%'));
          break;
        case 'speedUp':
          if (isFinite(quote.speed) && quote.speed >= th) this.fire(rule, quote, msg('涨速 ' + F.pct(quote.speed) + ' 触发快速拉升'));
          break;
        case 'speedDown':
          if (isFinite(quote.speed) && quote.speed <= -Math.abs(th)) this.fire(rule, quote, msg('涨速 ' + F.pct(quote.speed) + ' 触发快速跳水'));
          break;
        default:
          break;
      }
    },

    async evalKlineRules(list) {
      const needs = list.filter((r) => ['macdGolden', 'macdDead', 'rsiOversold', 'rsiOverbought', 'breakout20'].indexOf(r.type) >= 0);
      if (!needs.length) return;
      const groups = {};
      needs.forEach((r) => {
        const key = r.market + ':' + r.code;
        (groups[key] = groups[key] || { market: r.market, code: r.code, rules: [] }).rules.push(r);
      });
      const entries = Object.keys(groups).map((k) => groups[k]);
      for (const g of entries) {
        try {
          const cacheKey = g.market + ':' + g.code;
          const cached = this.klineCache[cacheKey];
          let bars;
          if (cached && Date.now() - cached.ts < 300000) bars = cached.bars;
          else {
            const res = await api.kline(g.market, g.code, 'day', 1, 200);
            bars = res.bars || [];
            this.klineCache[cacheKey] = { ts: Date.now(), bars };
          }
          if (bars.length < 40) continue;
          const ana = window.AD.quant.analyze(bars);
          const ind = ana.ind || {};
          const last = bars[bars.length - 1];
          const prev = bars[bars.length - 2];
          const I = window.AD.ind;
          const closes = bars.map((b) => b.close);
          const macd = I.MACD(closes, 12, 26, 9);
          const i = bars.length - 1;
          g.rules.forEach((r) => {
            const quote = { price: last.close, changePct: last.changePct };
            const th = Number(r.threshold) || 0;
            const golden = macd.dif[i] > macd.dea[i] && macd.dif[i - 1] <= macd.dea[i - 1];
            const dead = macd.dif[i] < macd.dea[i] && macd.dif[i - 1] >= macd.dea[i - 1];
            if (r.type === 'macdGolden' && golden) this.fire(r, quote, '日线 MACD 金叉（DIF ' + F.num(macd.dif[i], 3) + ' / DEA ' + F.num(macd.dea[i], 3) + '）');
            if (r.type === 'macdDead' && dead) this.fire(r, quote, '日线 MACD 死叉（DIF ' + F.num(macd.dif[i], 3) + ' / DEA ' + F.num(macd.dea[i], 3) + '）');
            if (r.type === 'rsiOversold' && isFinite(ind.rsi14) && ind.rsi14 <= th) this.fire(r, quote, 'RSI14 = ' + F.num(ind.rsi14, 1) + '，进入超卖区间');
            if (r.type === 'rsiOverbought' && isFinite(ind.rsi14) && ind.rsi14 >= th) this.fire(r, quote, 'RSI14 = ' + F.num(ind.rsi14, 1) + '，进入超买区间');
            if (r.type === 'breakout20' && isFinite(ind.hi20) && last.close >= ind.hi20 * 0.999 && prev.close < ind.hi20 * 0.999) {
              this.fire(r, quote, '收盘 ' + F.price(last.close, r.market) + ' 突破 20 日高点 ' + F.price(ind.hi20, r.market));
            }
          });
        } catch (e) { /* 单标的失败不影响其他规则 */ }
      }
    },

    async tick() {
      const rules = this.rules.filter((r) => r.enabled);
      if (!rules.length) return;
      const groups = { cn: new Set(), us: new Set() };
      rules.forEach((r) => { (groups[r.market] || groups.cn).add(r.code); });
      const quotes = {};
      for (const market of ['cn', 'us']) {
        const codes = Array.from(groups[market] || []);
        if (!codes.length) continue;
        try {
          const res = await api.quote(market, codes);
          (res.rows || []).forEach((q) => { quotes[market + ':' + q.code] = q; });
        } catch (e) { /* 忽略单市场失败 */ }
      }
      rules.forEach((r) => this.evalQuoteRules(r, quotes[r.market + ':' + r.code]));
    },

    start(ctx) {
      if (this.started) return;
      this.started = true;
      this._ctx = ctx;
      if (!this.rules.length) this.load();
      const quoteMs = Math.max(5000, (ctx && ctx.state && ctx.state.pollMs) || 6000);
      this._timerQuote = setInterval(() => this.tick().catch(() => {}), quoteMs);
      this._timerKline = setInterval(() => this.evalKlineRules(this.rules.filter((r) => r.enabled)).catch(() => {}), 300000);
      setTimeout(() => { this.tick().catch(() => {}); }, 3000);
    },
    stop() {
      if (this._timerQuote) clearInterval(this._timerQuote);
      if (this._timerKline) clearInterval(this._timerKline);
      this.started = false;
    },
    requestNotify() {
      if (!('Notification' in window)) return Promise.resolve(false);
      return Notification.requestPermission().then((p) => {
        this.notify = p === 'granted';
        return this.notify;
      });
    },
  };

  function log_text(rule, quote, txt) {
    return (rule.name || rule.code) + '（' + (rule.market === 'us' ? 'US:' : '') + rule.code + '）' +
      F.price(quote.price, rule.market) + '　' + txt;
  }

  /* ======================================================= 视图 */

  function mount(root, ctx) {
    const typeSel = h('select', { class: 'inp', style: { width: '190px' } });
    TYPES.forEach((t) => typeSel.appendChild(h('option', { value: t.value, text: t.label })));
    const codeInput = h('input', { class: 'inp', placeholder: '代码，如 600519 / AAPL', style: { width: '200px' } });
    const thInput = h('input', { class: 'inp value', value: '5', style: { width: '90px' } });
    const noteInput = h('input', { class: 'inp', placeholder: '备注（可选）', style: { width: '180px' } });
    const hint = h('span', { class: 'dim3', text: TYPE_MAP[typeSel.value].hint });
    const rulesHost = h('div');
    const logsHost = h('div');
    const statusHost = h('span', { class: 'hint' });

    /* 常驻实例（放在 mount 内，避免跨页面串数据）：
       规则表首次 appendChild，之后只 update(rows)；
       触发记录表体常驻，行按 key 复用 —— 后台按 quoteMs 轮询触发时不再重建 DOM */
    let rulesTbl = null;
    const logsWrap = h('div', { class: 'tbl-wrap' });
    const logsScroll = h('div', { class: 'tbl-scroll', style: { maxHeight: '360px' } });
    const logsTable = h('table', { class: 'tbl compact' });
    const logsBody = h('tbody');
    logsTable.appendChild(h('thead', {}, [h('tr', {}, [
      h('th', { text: '时间' }), h('th', { text: '标的' }), h('th', { class: 'n', text: '现价' }),
      h('th', { class: 'n', text: '涨跌幅' }), h('th', { text: '触发说明' }),
    ])]));
    logsTable.appendChild(logsBody);
    logsScroll.appendChild(logsTable);
    logsWrap.appendChild(logsScroll);

    if (ctx.state.symbol && ctx.state.symbol.code) {
      codeInput.value = ctx.state.symbol.code;
    }
    thInput.value = String(TYPE_MAP[typeSel.value].def);
    typeSel.addEventListener('change', () => {
      hint.textContent = TYPE_MAP[typeSel.value].hint;
      thInput.value = String(TYPE_MAP[typeSel.value].def);
    });

    async function addRule() {
      const code = codeInput.value.trim().toUpperCase();
      if (!code) { ctx.toast('请填写标的代码', 'warn'); return; }
      const market = /^\d{6}$/.test(code) ? 'cn' : 'us';
      let name = code;
      try {
        const s = await api.search(code);
        const hit = (s.rows || []).find((r) => r.market === market && r.code.toUpperCase() === code) || (s.rows || [])[0];
        if (hit) name = hit.name || code;
      } catch (e) { /* 名称可缺省 */ }
      engine.add({
        market, code, name, type: typeSel.value,
        threshold: Number(thInput.value) || 0,
        note: noteInput.value.trim(), cooldownMin: 10,
      });
      ctx.toast('预警已创建：' + name + ' · ' + TYPE_MAP[typeSel.value].label.replace('N', thInput.value), 'ok');
      codeInput.value = '';
      noteInput.value = '';
      render();
    }

    function render() {
      engine.load();
      const rules = engine.rules;
      statusHost.textContent = '规则 ' + rules.length + ' 条 · 启用 ' + rules.filter((r) => r.enabled).length +
        ' 条 · 触发记录 ' + engine.logs.length + ' 条';

      if (!rules.length) {
        rulesTbl = null;                       /* 空态替换掉了表体，实例随之失效 */
        paint(rulesHost, [ui.empty('还没有预警规则，先在上方创建一个')]);
      } else {
        const cols = [
          {
            key: 'name', label: '标的', noSort: true,
            render: (r) => h('span', {}, [
              h('span', { class: 'name', text: r.name || r.code }),
              h('span', { class: 'code', text: (r.market === 'us' ? 'US:' : '') + r.code }),
            ]),
          },
          { key: 'type', label: '触发条件', noSort: true, render: (r) => h('span', { text: typeDesc(r) }) },
          { key: 'note', label: '备注', noSort: true, render: (r) => h('span', { class: 'dim', text: r.note || '—' }) },
          {
            key: 'lastTrigger', label: '最近触发', noSort: true,
            render: (r) => h('span', { class: 'num dim', text: r.lastTrigger ? F.clock(r.lastTrigger) : '—' }),
          },
          {
            key: 'enabled', label: '状态', noSort: true,
            render: (r) => h('span', { class: 'chip ' + (r.enabled ? 'accent' : ''), text: r.enabled ? '监控中' : '已暂停' }),
          },
          {
            key: 'act', label: '操作', noSort: true, width: '150px',
            render: (r) => h('div', { style: { display: 'flex', gap: '5px' } }, [
              h('button', {
                class: 'btn ghost sm', text: r.enabled ? '暂停' : '启用',
                on: { click: (e) => { e.stopPropagation(); engine.toggle(r.id, !r.enabled); render(); } },
              }),
              h('button', {
                class: 'btn ghost sm', text: '打开',
                on: { click: (e) => { e.stopPropagation(); ctx.openSymbol(r.market, r.code, r.name); } },
              }),
              h('button', {
                class: 'btn ghost sm', text: '删除',
                on: { click: (e) => { e.stopPropagation(); engine.remove(r.id); render(); } },
              }),
            ]),
          },
        ];
        if (!rulesTbl) {
          rulesTbl = ui.tbl({ cols, rows: rules, compact: true });
          clear(rulesHost);
          rulesHost.appendChild(rulesTbl);
        } else {
          if (rulesTbl.parentNode !== rulesHost) {   /* 曾被空态替换过：重新挂载 */
            clear(rulesHost);
            rulesHost.appendChild(rulesTbl);
          }
          rulesTbl.update(rules);                    /* 列固定：只更新行，不重建表体 */
        }
      }

      renderLogs();
    }

    /* 触发记录：一条记录 = 一行，key 由 ts + 规则 + 类型 + 代码组成（同一个规则同一毫秒只会记一条） */
    function logKey(l) {
      return String(l.ts) + '|' + String(l.ruleId || '') + '|' + String(l.type || '') + '|' + String(l.code || '');
    }

    function logRow(l) {
      const tr = h('tr', {}, [
        h('td', { class: 'num dim', text: F.clock(l.ts) }),
        h('td', { class: 'name', text: l.name + ' ' + (l.market === 'us' ? 'US:' : '') + l.code }),
        h('td', { class: 'n' }, [h('span', { class: 'num', text: F.price(l.price, l.market) })]),
        h('td', { class: 'n' }, [pct(l.changePct)]),
        h('td', { class: 'dim', text: l.message }),
      ]);
      /* reconcile 每次刷新会把最新记录写到节点上：点击时读 __row，不读闭包里的旧值 */
      tr.addEventListener('click', () => {
        const row = tr.__row || l;
        ctx.openSymbol(row.market, row.code, row.name);
      });
      return tr;
    }

    function renderLogs() {
      const logs = engine.logs;
      if (!logs.length) {
        /* 空态与表体是两种结构：先把常驻表体摘下来，再原位换成空态 */
        if (logsWrap.parentNode === logsHost) logsHost.removeChild(logsWrap);
        paint(logsHost, [ui.empty('暂无触发记录')]);
        return;
      }
      if (logsWrap.parentNode !== logsHost) {
        clear(logsHost);
        logsHost.appendChild(logsWrap);
      }
      /* 行按 key 复用：新触发只插到最前面，滚动位置与 hover 都不会丢 */
      reconcile(logsBody, logs, { key: logKey, render: logRow });
    }

    const soundToggle = h('button', {
      class: 'btn sm ' + (engine.sound ? 'active' : ''), text: '提示音',
      on: {
        click: (e) => {
          engine.sound = !engine.sound;
          store.set('alertSound', engine.sound);
          e.target.classList.toggle('active', engine.sound);
        },
      },
    });

    const notifyBtn = h('button', {
      class: 'btn sm', text: '系统通知',
      on: {
        click: async () => {
          const ok = await engine.requestNotify();
          ctx.toast(ok ? '已开启浏览器系统通知' : '未获得通知权限，将只在页面内提示', ok ? 'ok' : 'warn');
          notifyBtn.classList.toggle('active', ok);
        },
      },
    });

    window.AD.bus.on('alert:trigger', () => { if (root.isConnected) render(); });
    window.AD.bus.on('alert:rules', () => { if (root.isConnected) render(); });

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('预警中心', '条件规则由后台轮询引擎持续监控（价格类实时、指标类每 5 分钟复核）', [
        statusHost, soundToggle, notifyBtn,
        h('button', { class: 'btn sm', text: '清空记录', on: { click: () => { engine.clearLogs(); render(); } } }),
      ]),
      ui.section('新建预警规则', '价格突破、涨跌幅、量比换手、技术指标均可监控', [], h('div', {}, [
        h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' } }, [
          h('div', { class: 'field' }, [h('label', { text: '标的' }), codeInput]),
          h('div', { class: 'field' }, [h('label', { text: '条件' }), typeSel]),
          h('div', { class: 'field' }, [h('label', { text: '阈值' }), thInput]),
          h('div', { class: 'field' }, [h('label', { text: '备注' }), noteInput]),
          h('button', { class: 'btn primary sm', text: '创建预警', on: { click: addRule } }),
        ]),
        h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [hint]),
      ])),
      ui.section('监控规则', '', [], rulesHost),
      ui.section('触发记录', '', [], logsHost),
    ]));

    engine.load();
    render();
    if (engine.rules.some((r) => r.enabled)) { /* 引擎已在 app.js 启动 */ }
    return {
      refresh() { render(); },
      destroy() {},
    };
  }

  window.AD = window.AD || {};
  window.AD.alertEngine = engine;
  window.AD.views = window.AD.views || {};
  window.AD.views.alerts = { mount };
})();
