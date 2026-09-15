/* ==========================================================================
   视图 · 自选股（跨市场混合，本地持久化）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  const PRESETS_CN = [
    { market: 'cn', code: '600519', name: '贵州茅台' },
    { market: 'cn', code: '300750', name: '宁德时代' },
    { market: 'cn', code: '601318', name: '中国平安' },
    { market: 'cn', code: '000858', name: '五粮液' },
    { market: 'cn', code: '002594', name: '比亚迪' },
  ];
  const PRESETS_US = [
    { market: 'us', code: 'AAPL', name: '苹果' },
    { market: 'us', code: 'NVDA', name: '英伟达' },
    { market: 'us', code: 'TSLA', name: '特斯拉' },
    { market: 'us', code: 'MSFT', name: '微软' },
    { market: 'us', code: 'BABA', name: '阿里巴巴' },
  ];

  function mount(root, ctx) {
    let table = null;
    let timer = null;
    const state = { rows: [], filter: '' };

    const tableHost = h('div');
    const statHost = h('span', { class: 'hint' });

    const input = h('input', { class: 'inp', placeholder: '代码 / 名称，如 600519 / AAPL', style: { width: '210px' } });

    async function addByInput() {
      const q = input.value.trim();
      if (!q) return;
      try {
        const res = await api.search(q);
        const rows = res.rows || [];
        if (!rows.length) { ctx.toast('未找到匹配标的：' + q, 'warn'); return; }
        const hit = rows.find((r) => r.code.toUpperCase() === q.toUpperCase()) || rows[0];
        ctx.addWatch(hit.market, hit.code, hit.name);
        input.value = '';
        await refresh();
        ctx.toast('已加入自选：' + hit.name, 'ok');
      } catch (e) { ctx.toast('搜索失败：' + e.message, 'err'); }
    }

    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') addByInput(); });

    function cols() {
      return [
        { key: 'name', label: '名称', noSort: true, render: (r) => h('span', {}, [ui.cells.name(r), h('span', { class: 'chip', style: { marginLeft: '6px' }, text: r.market === 'us' ? '美股' : 'A股' })]) },
        { key: 'price', label: '最新价', cls: 'n', value: (r) => r.price, render: (r) => ui.cells.price(r) },
        { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
        { key: 'change', label: '涨跌额', cls: 'n', value: (r) => r.change, render: (r) => h('span', { class: 'num ' + F.dir(r.change), text: F.signed(r.change, 2) }) },
        { key: 'amount', label: '成交额', cls: 'n', value: (r) => r.amount, render: (r) => ui.cells.amount(r) },
        { key: 'turnover', label: '换手', cls: 'n', value: (r) => r.turnover, render: (r) => h('span', { class: 'num', text: isFinite(r.turnover) ? F.num(r.turnover, 2) + '%' : '—' }) },
        { key: 'volumeRatio', label: '量比', cls: 'n', value: (r) => r.volumeRatio, render: (r) => h('span', { class: 'num', text: F.num(r.volumeRatio, 2) }) },
        { key: 'marketCap', label: '总市值', cls: 'n', value: (r) => r.marketCap, render: (r) => ui.cells.cap(r) },
        { key: 'peTtm', label: 'PE(TTM)', cls: 'n', value: (r) => r.peTtm, render: (r) => h('span', { class: 'num', text: F.num(r.peTtm, 1) }) },
        { key: 'amplitude', label: '振幅', cls: 'n', value: (r) => r.amplitude, render: (r) => h('span', { class: 'num', text: isFinite(r.amplitude) ? F.num(r.amplitude, 2) + '%' : '—' }) },
        {
          key: 'act', label: '操作', noSort: true, width: '120px',
          render: (r) => h('div', { style: { display: 'flex', gap: '5px' } }, [
            h('button', {
              class: 'btn ghost sm', text: '详情',
              on: { click: (e) => { e.stopPropagation(); ctx.openSymbol(r.market, r.code, r.name); } },
            }),
            h('button', {
              class: 'btn ghost sm', text: '移除',
              on: {
                click: (e) => {
                  e.stopPropagation();
                  ctx.removeWatch(r.market, r.code);
                  refresh();
                },
              },
            }),
          ]),
        },
      ];
    }

    async function refresh() {
      const list = ctx.getWatch();
      clear(statHost);
      if (!list.length) {
        clear(tableHost);
        const presets = h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'center', marginTop: '10px' } });
        presets.appendChild(h('button', {
          class: 'btn sm', text: '导入 A股示例（茅台 / 宁德时代 / 中国平安 …）',
          on: { click: async () => { PRESETS_CN.concat(PRESETS_US).forEach((p) => ctx.addWatch(p.market, p.code, p.name)); await refresh(); } },
        }));
        tableHost.appendChild(h('div', { class: 'empty' }, [
          h('div', { text: '自选列表为空' }),
          h('div', { style: { marginTop: '6px' }, text: '在上方输入代码加入，或一键导入示例组合' }),
          presets,
        ]));
        return;
      }
      const byMarket = { cn: [], us: [] };
      list.forEach((it) => { (byMarket[it.market] || byMarket.cn).push(it.code); });
      const tasks = [];
      if (byMarket.cn.length) tasks.push(api.quote('cn', byMarket.cn));
      if (byMarket.us.length) tasks.push(api.quote('us', byMarket.us));
      try {
        const res = await Promise.all(tasks);
        const map = {};
        res.forEach((r) => (r.rows || []).forEach((q) => { map[q.market + ':' + q.code] = q; }));
        state.rows = list.map((it) => {
          const q = map[it.market + ':' + it.code];
          return q ? Object.assign({}, q, { name: q.name || it.name }) : {
            market: it.market, code: it.code, name: it.name, price: null, changePct: null,
            amount: null, turnover: null, volumeRatio: null, marketCap: null, peTtm: null, amplitude: null,
            _missing: true,
          };
        }).filter((r) => !state.filter || (r.name || '').indexOf(state.filter) >= 0 || r.code.indexOf(state.filter) >= 0);

        statHost.textContent = '共 ' + state.rows.length + ' 只 · A股 ' + byMarket.cn.length +
          ' 只 · 美股 ' + byMarket.us.length + ' 只 · 更新 ' + F.clock(Date.now());
        const up = state.rows.filter((r) => (r.changePct || 0) > 0).length;
        const down = state.rows.filter((r) => (r.changePct || 0) < 0).length;
        statHost.textContent += ' · 上涨 ' + up + ' / 下跌 ' + down;

        if (state.filter) {
          statHost.textContent += ' · 筛选「' + state.filter + '」命中 ' + state.rows.length + ' 只';
        }
        clear(tableHost);
        tableHost.appendChild(ui.tbl({
          cols: cols(), rows: state.rows, maxHeight: 'calc(100vh - 300px)',
          onRow: (r) => ctx.openSymbol(r.market, r.code, r.name),
          emptyText: '没有匹配的自选股',
        }));
      } catch (e) {
        ctx.toast('自选行情刷新失败：' + e.message, 'err');
      }
    }

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('自选股', '支持 A股 / 美股混合自选，数据自动轮询刷新', [
        statHost,
        input,
        h('button', { class: 'btn primary sm', text: '加入自选', on: { click: addByInput } }),
        h('button', {
          class: 'btn sm', text: '清空',
          on: {
            click: () => {
              if (ctx.getWatch().length && window.confirm('确认清空自选列表？')) {
                ctx.setWatch([]);
                refresh();
              }
            },
          },
        }),
      ]),
      tableHost,
    ]));

    table = ui.tbl({ cols: cols(), rows: [], onRow: (r) => ctx.openSymbol(r.market, r.code, r.name), maxHeight: 'calc(100vh - 300px)' });
    tableHost.appendChild(table);

    const filterInput = h('input', {
      class: 'inp', placeholder: '本地筛选（名称 / 代码）', style: { width: '170px' },
      on: {
        input: (e) => { state.filter = e.target.value.trim().toUpperCase(); refresh(); },
      },
    });
    root.querySelector('.head-actions').insertBefore(filterInput, input);

    refresh();
    timer = setInterval(refresh, Math.max(4000, ctx.state.pollMs));
    return { refresh, destroy() { if (timer) clearInterval(timer); } };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.watchlist = { mount };
})();
