/* ==========================================================================
   视图 · 选股器（多条件筛选 + 预设策略）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  /* 预设条件：市值单位（A股 亿元 / 美股 亿美元） */
  const PRESETS = [
    {
      key: 'strong', name: '强势突破',
      desc: '涨幅 > 3%，量比 > 1.5，成交额活跃',
      f: { pctMin: 3, vrMin: 1.5, active: 1 }, sort: 'changePct', order: 'desc',
    },
    {
      key: 'volume', name: '放量异动',
      desc: '量比 > 2.5，换手率 > 5%，涨幅为正',
      f: { vrMin: 2.5, turnoverMin: 5, pctMin: 0 }, sort: 'volumeRatio', order: 'desc',
    },
    {
      key: 'oversold', name: '超跌反弹候选',
      desc: '跌幅 > 5%，成交额 > 1 亿，等待企稳',
      f: { pctMax: -5, active: 1 }, sort: 'changePct', order: 'asc',
    },
    {
      key: 'bluechip', name: '低估值大盘',
      desc: 'PE 0~15，总市值 > 500 亿，涨幅为正',
      f: { peMin: 0.01, peMax: 15, capMin: 500, pctMin: 0 }, sort: 'marketCap', order: 'desc',
    },
    {
      key: 'flow', name: '主力抢筹',
      desc: '主力净流入 > 1 亿，涨幅 > 0，换手 > 2%',
      f: { pctMin: 0, turnoverMin: 2, minFlow: 1e8 }, sort: 'mainInflow', order: 'desc',
    },
  ];

  const SORTS = [
    { value: 'changePct', label: '涨跌幅' }, { value: 'amount', label: '成交额' },
    { value: 'turnover', label: '换手率' }, { value: 'volumeRatio', label: '量比' },
    { value: 'marketCap', label: '总市值' }, { value: 'peTtm', label: 'PE(TTM)' },
    { value: 'mainInflow', label: '主力净额' }, { value: 'chg60d', label: '60日涨幅' },
  ];

  function mount(root, ctx) {
    const market = ctx.state.market;
    const isCn = market === 'cn';
    const capUnit = isCn ? '亿元' : '亿美元';
    const FIELDS = [
      { key: 'pctMin', label: '涨跌幅 ≥ (%)', ph: '如 3' },
      { key: 'pctMax', label: '涨跌幅 ≤ (%)', ph: '如 -5' },
      { key: 'turnoverMin', label: '换手率 ≥ (%)', ph: '如 5' },
      { key: 'vrMin', label: '量比 ≥', ph: '如 1.5' },
      { key: 'priceMin', label: '价格 ≥', ph: '' },
      { key: 'priceMax', label: '价格 ≤', ph: '' },
      { key: 'capMin', label: '市值 ≥ (' + capUnit + ')', ph: isCn ? '如 500' : '如 100' },
      { key: 'peMin', label: 'PE(TTM) ≥', ph: '0' },
      { key: 'peMax', label: 'PE(TTM) ≤', ph: '如 30' },
    ];

    const inputs = {};
    const state = { page: 1, size: 50, sort: 'changePct', order: 'desc', total: 0, preset: null, kw: '' };
    const tableHost = h('div');
    const statHost = h('span', { class: 'hint', text: '就绪' });

    function collect() {
      const f = {};
      Object.keys(inputs).forEach((k) => {
        const v = inputs[k].value.trim();
        if (v !== '' && !isNaN(Number(v))) {
          let n = Number(v);
          if (k === 'capMin' || k === 'capMax') n = n * 1e8;
          f[k] = n;
        }
      });
      if (state.kw) f.kw = state.kw;
      if (state.preset && state.preset.f.minFlow) f.minFlow = state.preset.f.minFlow;
      return f;
    }

    function cols() {
      const base = [
        { key: 'name', label: '名称', noSort: true, render: (r) => ui.cells.name(r) },
        { key: 'price', label: '最新价', cls: 'n', value: (r) => r.price, render: (r) => ui.cells.price(r) },
        { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
        { key: 'amount', label: '成交额', cls: 'n', value: (r) => r.amount, render: (r) => ui.cells.amount(r) },
        { key: 'turnover', label: '换手率', cls: 'n', value: (r) => r.turnover, render: (r) => h('span', { class: 'num', text: isFinite(r.turnover) ? F.num(r.turnover, 2) + '%' : '—' }) },
        { key: 'volumeRatio', label: '量比', cls: 'n', value: (r) => r.volumeRatio, render: (r) => h('span', { class: 'num', text: F.num(r.volumeRatio, 2) }) },
        { key: 'marketCap', label: '总市值', cls: 'n', value: (r) => r.marketCap, render: (r) => ui.cells.cap(r) },
        { key: 'peTtm', label: 'PE(TTM)', cls: 'n', value: (r) => r.peTtm, render: (r) => h('span', { class: 'num', text: F.num(r.peTtm, 1) }) },
        { key: 'chg60d', label: '60日涨幅', cls: 'n', value: (r) => r.chg60d, render: (r) => pct(r.chg60d) },
      ];
      if (isCn) {
        base.push({ key: 'mainInflow', label: '主力净额', cls: 'n', value: (r) => r.mainInflow, render: (r) => ui.cells.flow(r) });
      }
      base.push({
        key: 'act', label: '', noSort: true, width: '40px',
        render: (r) => ui.cells.star(ctx.isWatched(r.market, r.code), () => {
          ctx.toggleWatch(r.market, r.code, r.name);
          run();
        }),
      });
      return base;
    }

    async function run(page) {
      if (page) state.page = page;
      statHost.textContent = '筛选中…';
      try {
        const res = await api.screener(market, collect(), state.sort, state.order, state.page, state.size);
        state.total = res.total;
        clear(tableHost);
        tableHost.appendChild(ui.tbl({
          cols: cols(), rows: res.rows, maxHeight: 'calc(100vh - 430px)',
          onRow: (r) => ctx.openSymbol(r.market, r.code, r.name),
          emptyText: '没有符合条件的标的，试试放宽条件',
        }));
        const pages = Math.max(1, Math.ceil(res.total / state.size));
        statHost.textContent = '命中 ' + res.total + ' 只 · 第 ' + state.page + '/' + pages + ' 页 · 口径：' + res.sampleScope +
          ' · 快照源 ' + (res.source || '—') + (res.stale ? '（本地缓存，可能延迟）' : '');
        if (res.source && res.source !== '东方财富') {
          statHost.textContent += ' · 当前快照源未提供量比/主力资金字段，相关条件可能筛不出结果';
        }
        pagerInfo.textContent = '共 ' + res.total + ' 只 · 每页 ' + state.size + ' 只';
        prevBtn.disabled = state.page <= 1;
        nextBtn.disabled = state.page >= pages;
        pageLabel.textContent = state.page + ' / ' + pages;
      } catch (e) {
        statHost.textContent = '';
        clear(tableHost);
        tableHost.appendChild(ui.empty('筛选失败：' + e.message));
      }
    }

    const presetSeg = h('div', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap' } });
    PRESETS.forEach((p) => {
      const b = h('button', {
        class: 'btn sm', text: p.name, title: p.desc,
        on: {
          click: () => {
            state.preset = state.preset && state.preset.key === p.key ? null : p;
            Array.prototype.forEach.call(presetSeg.children, (c) => c.classList.toggle('active', state.preset && c.textContent === state.preset.name));
            Object.keys(inputs).forEach((k) => { inputs[k].value = ''; });
            if (state.preset) {
              Object.keys(state.preset.f).forEach((k) => {
                if (inputs[k]) inputs[k].value = String(state.preset.f[k]);
              });
            }
            state.sort = p.sort; state.order = p.order;
            sortSel.value = p.sort; orderSel.value = p.order;
            state.page = 1;
            run();
          },
        },
      });
      presetSeg.appendChild(b);
    });

    const grid = h('div', { class: 'filter-grid' });
    FIELDS.forEach((f) => {
      const inp = h('input', { class: 'inp', placeholder: f.ph || '' });
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { state.page = 1; run(); } });
      inputs[f.key] = inp;
      grid.appendChild(h('div', { class: 'field' }, [h('label', { text: f.label }), inp]));
    });

    const kwInput = h('input', { class: 'inp', placeholder: '关键词（代码 / 名称）', style: { width: '180px' } });
    kwInput.addEventListener('input', window.AD.util.debounce(() => { state.kw = kwInput.value.trim().toUpperCase(); state.page = 1; run(); }, 400));

    const sortSel = h('select', { class: 'inp', style: { width: '128px' } });
    SORTS.forEach((s) => sortSel.appendChild(h('option', { value: s.value, text: s.label })));
    sortSel.value = state.sort;
    sortSel.addEventListener('change', () => { state.sort = sortSel.value; state.page = 1; run(); });

    const orderSel = h('select', { class: 'inp', style: { width: '86px' } }, [
      h('option', { value: 'desc', text: '降序' }), h('option', { value: 'asc', text: '升序' }),
    ]);
    orderSel.addEventListener('change', () => { state.order = orderSel.value; state.page = 1; run(); });

    const prevBtn = h('button', { class: 'btn sm', text: '上一页', on: { click: () => run(state.page - 1) } });
    const nextBtn = h('button', { class: 'btn sm', text: '下一页', on: { click: () => run(state.page + 1) } });
    const pageLabel = h('span', { class: 'num', text: '1 / 1' });
    const pagerInfo = h('span', {});

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('选股器 · ' + (isCn ? '沪深 A 股' : '美股活跃样本'), '多条件组合筛选，条件由服务端在全量快照上执行', [
        statHost,
        h('button', { class: 'btn sm', text: '重置', on: { click: () => { Object.keys(inputs).forEach((k) => { inputs[k].value = ''; }); kwInput.value = ''; state.kw = ''; state.preset = null; state.page = 1; run(); } } }),
        h('button', { class: 'btn primary sm', text: '开始筛选', on: { click: () => { state.page = 1; run(); } } }),
      ]),
      ui.section('预设策略', '一键套用常见选股思路', [], presetSeg),
      ui.section('筛选条件', '留空表示不限制；市值单位为' + capUnit, [], h('div', {}, [
        grid,
        h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', marginTop: '12px', flexWrap: 'wrap' } }, [
          kwInput,
          h('span', { class: 'dim3', text: '排序' }), sortSel, orderSel,
          h('span', { class: 'spacer' }),
          h('button', { class: 'btn sm', text: '应用', on: { click: () => { state.page = 1; run(); } } }),
        ]),
      ])),
      ui.section('筛选结果', '', [], h('div', {}, [
        tableHost,
        h('div', { class: 'pager' }, [pagerInfo, h('span', { class: 'spacer' }), prevBtn, pageLabel, nextBtn]),
      ])),
    ]));

    run(1);
    return { refresh: () => {}, destroy() {} };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.screener = { mount };
})();
