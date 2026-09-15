/* ==========================================================================
   AlphaDesk · 应用主控（路由 / 顶栏 / 搜索 / 设置 / 自选 / 预警引擎）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear } = window.AD.dom;
  const F = window.AD.fmt;
  const store = window.AD.store;
  const bus = window.AD.bus;
  const util = window.AD.util;
  const ui = window.AD.ui;
  const session = window.AD.session;

  const DEFAULT_SYMBOL = { cn: { market: 'cn', code: '600519', name: '贵州茅台' }, us: { market: 'us', code: 'AAPL', name: '苹果' } };
  const PRESET_WATCH = [
    { market: 'cn', code: '600519', name: '贵州茅台' },
    { market: 'cn', code: '300750', name: '宁德时代' },
    { market: 'cn', code: '601318', name: '中国平安' },
    { market: 'us', code: 'AAPL', name: '苹果' },
    { market: 'us', code: 'NVDA', name: '英伟达' },
  ];

  const state = {
    market: store.get('market', 'cn'),
    view: 'market',
    symbol: store.get('symbol', Object.assign({}, DEFAULT_SYMBOL[store.get('market', 'cn')])),
    pollMs: store.get('pollMs', 6000),
    colorMode: store.get('colorMode', 'cn'),
    watch: store.get('watchlist', null) || PRESET_WATCH.slice(),
    active: null,
  };

  const viewRoot = document.getElementById('view-root');
  const rail = document.getElementById('rail');
  const marketSwitch = document.getElementById('market-switch');
  const toasts = document.getElementById('toasts');
  const palette = document.getElementById('palette');
  const paletteInput = document.getElementById('palette-input');
  const paletteResults = document.getElementById('palette-results');
  const drawer = document.getElementById('drawer');
  const drawerBody = document.getElementById('drawer-body');
  const sessionBadge = document.getElementById('session-badge');
  const clockEl = document.getElementById('clock');
  const alertBadge = document.getElementById('alert-badge');

  /* ------------------------------------------------------------ 提示 */

  let toastTimer = null;
  function toast(msg, type) {
    const t = h('div', { class: 'toast ' + (type || 'info') }, [h('div', { text: msg })]);
    toasts.appendChild(t);
    setTimeout(() => {
      t.style.transition = 'opacity .3s ease';
      t.style.opacity = '0';
      setTimeout(() => t.remove(), 320);
    }, type === 'err' ? 6000 : 3600);
    while (toasts.children.length > 5) toasts.removeChild(toasts.firstChild);
  }

  /* ------------------------------------------------------- 自选管理 */

  function getWatch() { return state.watch.slice(); }
  function saveWatch() { store.set('watchlist', state.watch); bus.emit('watch:change'); }
  function isWatched(market, code) {
    return state.watch.some((w) => w.market === market && w.code === code);
  }
  function addWatch(market, code, name) {
    if (isWatched(market, code)) return false;
    state.watch.unshift({ market, code, name: name || code });
    saveWatch();
    return true;
  }
  function removeWatch(market, code) {
    state.watch = state.watch.filter((w) => !(w.market === market && w.code === code));
    saveWatch();
  }
  function toggleWatch(market, code, name) {
    if (isWatched(market, code)) { removeWatch(market, code); toast('已移出自选：' + (name || code)); }
    else { addWatch(market, code, name); toast('已加入自选：' + (name || code), 'ok'); }
  }

  /* --------------------------------------------------------- 路由 */

  const ctx = {
    state,
    toast,
    getWatch, setWatch: (v) => { state.watch = v; saveWatch(); },
    isWatched, addWatch, removeWatch, toggleWatch,
    openSymbol(market, code, name) {
      if (!code) return;
      state.symbol = { market: market || state.market, code: String(code).toUpperCase(), name: name || code };
      store.set('symbol', state.symbol);
      if (state.view !== 'detail') { switchView('detail'); }
      else { render(); }
    },
    openIndex(market, code, name, symbol) {
      state.symbol = { market, code: symbol || code, name: name || code, isIndex: true };
      store.set('symbol', state.symbol);
      switchView('detail');
    },
    openSector(sector) {
      if (sector && sector.leaderCode) {
        ctx.openSymbol('cn', sector.leaderCode, sector.leader + '（' + sector.name + '领涨）');
        toast('已打开板块「' + sector.name + '」领涨股', 'info');
      } else {
        toast('板块成分股明细数据未接入公开接口，可打开领涨股查看', 'warn');
      }
    },
    openAlertFor(market, code, name) {
      state.symbol = { market, code, name: name || code };
      store.set('symbol', state.symbol);
      switchView('alerts');
      toast('已带入标的：' + (name || code) + '，设置阈值后创建预警', 'info');
    },
    openBacktest(market, code, name) {
      state.symbol = { market, code, name: name || code };
      store.set('symbol', state.symbol);
      switchView('backtest');
    },
    openTracker(market, code, name, prefill) {
      if (code) {
        state.symbol = { market, code, name: name || code };
        store.set('symbol', state.symbol);
      }
      state.trackerPrefill = prefill || null;
      switchView('tracker');
    },
    setMarket(m) {
      state.market = m;
      store.set('market', m);
      Array.prototype.forEach.call(marketSwitch.children, (b) => b.classList.toggle('active', b.dataset.market === m));
      document.documentElement.setAttribute('data-color', state.colorMode === 'us' ? 'us' : 'cn');
      if (state.view === 'detail' && state.symbol.market !== m) {
        state.symbol = Object.assign({}, DEFAULT_SYMBOL[m]);
        store.set('symbol', state.symbol);
      }
      render();
    },
  };

  const VIEWS = {
    market: '市场总览', watchlist: '自选股', detail: '个股详情', screener: '选股器',
    features: '盘口事件', backtest: '策略回测', tracker: '策略跟踪', system: '运行状态',
    alerts: '预警中心', news: '资讯快讯', advisor: 'AI 选股',
  };

  function switchView(name) {
    if (!VIEWS[name]) name = 'market';
    if (state.active && state.active.destroy) {
      try { state.active.destroy(); } catch (e) { console.error(e); }
    }
    state.view = name;
    state.active = null;
    Array.prototype.forEach.call(rail.querySelectorAll('.rail-item'), (b) => {
      b.classList.toggle('active', b.dataset.view === name);
    });
    render();
  }

  function render() {
    if (state.active && state.active.destroy) {
      try { state.active.destroy(); } catch (e) { console.error(e); }
    }
    clear(viewRoot);
    const view = window.AD.views[state.view];
    if (!view) { viewRoot.appendChild(ui.empty('视图不存在')); return; }
    try {
      state.active = view.mount(viewRoot, ctx);
    } catch (e) {
      console.error(e);
      viewRoot.appendChild(h('div', { class: 'page' }, [ui.empty('视图渲染失败：' + e.message)]));
    }
    viewRoot.scrollTop = 0;
  }

  /* ------------------------------------------------------- 搜索面板 */

  const paletteState = { items: [], idx: 0, open: false };

  function renderPalette() {
    clear(paletteResults);
    if (!paletteState.items.length) {
      paletteResults.appendChild(h('div', { class: 'palette-item' }, [
        h('span', { class: 'p-name dim', text: paletteInput.value ? '没有匹配结果，可尝试完整代码（如 600519 / AAPL）' : '输入代码、名称或拼音首字母搜索' }),
      ]));
      return;
    }
    paletteState.items.forEach((it, i) => {
      paletteResults.appendChild(h('div', {
        class: 'palette-item' + (i === paletteState.idx ? ' active' : ''),
        on: {
          click: () => { pickPalette(it); },
          mouseenter: () => { paletteState.idx = i; renderPalette(); },
        },
      }, [
        h('div', {}, [
          h('div', { class: 'p-name', text: it.name }),
          h('div', { class: 'p-code', text: it.code }),
        ]),
        h('div', { class: 'p-tag' }, [
          h('span', { class: 'chip ' + (it.market === 'us' ? 'accent' : ''), text: (it.market === 'us' ? '美股 · ' : 'A股 · ') + (it.type || '') }),
        ]),
      ]));
    });
  }

  function pickPalette(it) {
    closePalette();
    ctx.openSymbol(it.market, it.code, it.name);
  }

  const doSearch = util.debounce(async () => {
    const q = paletteInput.value.trim();
    if (!q) { paletteState.items = []; paletteState.idx = 0; renderPalette(); return; }
    try {
      const res = await window.AD.api.search(q);
      paletteState.items = res.rows || [];
      paletteState.idx = 0;
      renderPalette();
    } catch (e) {
      paletteState.items = [];
      renderPalette();
    }
  }, 220);

  function openPalette() {
    paletteState.open = true;
    palette.classList.remove('hidden');
    paletteInput.value = '';
    paletteState.items = [];
    renderPalette();
    paletteInput.focus();
  }
  function closePalette() {
    paletteState.open = false;
    palette.classList.add('hidden');
  }

  paletteInput.addEventListener('input', doSearch);
  paletteInput.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      paletteState.idx = Math.min(paletteState.items.length - 1, paletteState.idx + 1);
      renderPalette();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      paletteState.idx = Math.max(0, paletteState.idx - 1);
      renderPalette();
    } else if (e.key === 'Enter') {
      const it = paletteState.items[paletteState.idx];
      if (it) pickPalette(it);
      else if (/^[A-Za-z.]{1,6}$/.test(paletteInput.value.trim())) {
        const c = paletteInput.value.trim().toUpperCase();
        closePalette();
        ctx.openSymbol('us', c, c);
      }
    } else if (e.key === 'Escape') {
      closePalette();
    }
  });
  palette.addEventListener('click', (e) => { if (e.target === palette) closePalette(); });
  document.getElementById('search-trigger').addEventListener('click', openPalette);

  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (paletteState.open) closePalette(); else openPalette();
      return;
    }
    if (e.key === 'Escape') { closePalette(); drawer.classList.add('hidden'); return; }
    if (paletteState.open) return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    /* 数字键顺序与左侧导航栏一致；超过 9 个的视图没有快捷键（单键只能到 9） */
    const order = ['market', 'watchlist', 'detail', 'screener', 'advisor', 'features',
      'backtest', 'tracker', 'system', 'alerts', 'news'];
    const n = Number(e.key);
    if (n >= 1 && n <= order.length) switchView(order[n - 1]);
    if (e.key === 'r' || e.key === 'R') { if (state.active && state.active.refresh) state.active.refresh(); }
  });

  /* --------------------------------------------------------- 设置 */

  function openDrawer() {
    const body = drawerBody;
    clear(body);

    const pollSel = h('select', { class: 'inp' });
    [[4000, '4 秒'], [6000, '6 秒'], [10000, '10 秒'], [30000, '30 秒'], [60000, '60 秒']].forEach(([v, l]) => {
      pollSel.appendChild(h('option', { value: String(v), text: l }));
    });
    pollSel.value = String(state.pollMs);
    pollSel.addEventListener('change', () => {
      state.pollMs = Number(pollSel.value);
      store.set('pollMs', state.pollMs);
      toast('刷新频率已调整为 ' + pollSel.options[pollSel.selectedIndex].text + '，切换视图后生效', 'ok');
    });

    const colorSel = h('select', { class: 'inp' }, [
      h('option', { value: 'cn', text: '红涨绿跌（A股习惯）' }),
      h('option', { value: 'us', text: '绿涨红跌（美股习惯）' }),
    ]);
    colorSel.value = state.colorMode;
    colorSel.addEventListener('change', () => {
      state.colorMode = colorSel.value;
      store.set('colorMode', state.colorMode);
      document.documentElement.setAttribute('data-color', state.colorMode === 'us' ? 'us' : 'cn');
      document.dispatchEvent(new Event('AD:theme'));
      toast('配色方案已更新', 'ok');
    });

    const defMarket = h('select', { class: 'inp' }, [
      h('option', { value: 'cn', text: 'A股' }), h('option', { value: 'us', text: '美股' }),
    ]);
    defMarket.value = state.market;
    defMarket.addEventListener('change', () => ctx.setMarket(defMarket.value));

    body.appendChild(h('div', { class: 'set-group' }, [
      h('h4', { text: '行情刷新' }),
      h('div', { class: 'set-row' }, [h('span', { text: '自动刷新频率' }), pollSel]),
      h('div', { class: 'set-note', text: '行情接口存在频率限制，建议保持 6 秒以上；市场总览的全市场快照在服务端缓存 45 秒。' }),
    ]));

    body.appendChild(h('div', { class: 'set-group' }, [
      h('h4', { text: '显示' }),
      h('div', { class: 'set-row' }, [h('span', { text: '涨跌配色' }), colorSel]),
      h('div', { class: 'set-row' }, [h('span', { text: '默认市场' }), defMarket]),
    ]));

    body.appendChild(h('div', { class: 'set-group' }, [
      h('h4', { text: '数据源' }),
      h('div', { class: 'set-note', html:
        '· 实时行情 / K线 / 分时：<b>腾讯行情</b><br>' +
        '· 全市场快照 / 板块 / 资讯 / 搜索：<b>东方财富</b><br>' +
        '· A股行情兜底：<b>新浪财经</b><br>' +
        '所有请求由本地服务统一代理并缓存，任一数据源异常会自动降级。' }),
    ]));

    body.appendChild(h('div', { class: 'set-group' }, [
      h('h4', { text: '本地数据' }),
      h('div', { class: 'set-row' }, [
        h('span', { text: '自选 ' + state.watch.length + ' 只 · 预警 ' + window.AD.alertEngine.rules.length + ' 条' }),
        h('button', {
          class: 'btn sm', text: '清除本地数据',
          on: {
            click: () => {
              if (!window.confirm('将清除自选、预警与偏好设置，确认继续？')) return;
              ['watchlist', 'alertRules', 'alertLogs', 'symbol', 'pollMs', 'colorMode', 'market'].forEach((k) => store.del(k));
              window.location.reload();
            },
          },
        }),
      ]),
    ]));

    body.appendChild(h('div', { class: 'set-group' }, [
      h('h4', { text: '免责声明' }),
      h('div', { class: 'set-note', text: '本系统基于公开行情接口构建，仅用于技术研究与学习。数据可能存在延迟或误差，不构成任何投资建议，据此操作风险自担。' }),
    ]));

    drawer.classList.remove('hidden');
  }

  document.getElementById('settings-btn').addEventListener('click', openDrawer);
  document.getElementById('drawer-close').addEventListener('click', () => drawer.classList.add('hidden'));
  drawer.addEventListener('click', (e) => { if (e.target === drawer) drawer.classList.add('hidden'); });

  document.getElementById('refresh-btn').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.classList.add('spin');
    if (state.active && state.active.refresh) state.active.refresh();
    setTimeout(() => btn.classList.remove('spin'), 700);
  });

  marketSwitch.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-market]');
    if (b) ctx.setMarket(b.dataset.market);
  });

  rail.addEventListener('click', (e) => {
    const b = e.target.closest('.rail-item');
    if (b) switchView(b.dataset.view);
  });

  /* ------------------------------------------------- 顶栏时钟 / 时段 */

  function tickClock() {
    const now = new Date();
    clockEl.textContent = F.clock(now.getTime());
    const cn = session.cn(now), us = session.us(now);
    const s = state.market === 'us' ? us : cn;
    sessionBadge.className = 'session ' + s.cls;
    const other = state.market === 'us' ? cn : us;
    const otherLabel = state.market === 'us' ? 'A股' : '美股';
    sessionBadge.innerHTML = '';
    sessionBadge.appendChild(h('i', { class: 'dot' }));
    sessionBadge.appendChild(h('span', { text: s.label + ' · ' + s.detail + '　|　' + otherLabel + '：' + other.label }));
    sessionBadge.title = 'A股：' + cn.label + '（' + cn.detail + '）　美股：' + us.label + '（' + us.detail + '）';
  }

  function updateAlertBadge(count) {
    const n = typeof count === 'number' ? count : window.AD.alertEngine.logs.length;
    alertBadge.textContent = String(n);
    alertBadge.classList.toggle('hidden', n === 0);
  }

  const trackerBadge = document.getElementById('tracker-badge');
  async function refreshTrackerBadge() {
    try {
      const ov = await window.AD.api.strategyOverview();
      const n = (ov.totals && ov.totals.running) || 0;
      trackerBadge.textContent = String(n);
      trackerBadge.classList.toggle('hidden', n === 0);
      trackerBadge.title = n ? '正在跟踪 ' + n + ' 个策略任务' : '暂无运行中的策略任务';
    } catch (e) { /* 引擎未就绪时忽略 */ }
  }
  setInterval(refreshTrackerBadge, 60000);
  refreshTrackerBadge();

  bus.on('alert:trigger', (log) => {
    updateAlertBadge();
    (log ? [log] : window.AD.alertEngine.logs.slice(0, 1)).forEach((l) => {
      toast('【预警】' + l.message, 'warn');
    });
  });
  bus.on('alert:logs', () => updateAlertBadge());
  bus.on('watch:change', () => { /* 视图各自响应 */ });

  /* --------------------------------------------------------- 启动 */

  document.documentElement.setAttribute('data-color', state.colorMode === 'us' ? 'us' : 'cn');
  Array.prototype.forEach.call(marketSwitch.children, (b) => b.classList.toggle('active', b.dataset.market === state.market));
  window.AD.alertEngine.load();
  updateAlertBadge();
  window.AD.alertEngine.start(ctx);
  tickClock();
  setInterval(tickClock, 1000);
  switchView(state.view);

  window.AD.app = { state, ctx, switchView, toast, render };
})();
