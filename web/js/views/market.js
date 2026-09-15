/* ==========================================================================
   视图 · 市场总览
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  const US_HOT = ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'AVGO', 'AMD', 'NFLX'];
  const US_CN_ADR = ['BABA', 'PDD', 'JD', 'NTES', 'BIDU', 'NIO', 'XPEV', 'LI', 'TCOM', 'BEKE'];

  const RANK_TABS = [
    { value: 'gainers', label: '涨幅榜' },
    { value: 'losers', label: '跌幅榜' },
    { value: 'actives', label: '成交额' },
    { value: 'highTurnover', label: '换手率' },
    { value: 'inflow', label: '主力净流入' },
    { value: 'volumeRatio', label: '量比' },
  ];

  function idxCell(item, ctx) {
    const d = F.dir(item.changePct);
    return h('div', {
      class: 'idx-cell',
      on: { click: () => ctx.openIndex(item.market, item.code, item.name, item.indexSymbol) },
    }, [
      h('div', { class: 'n' }, [h('span', { text: item.name }), h('span', { class: 'dim3', text: F.timeOf(item.updated) })]),
      h('div', { class: 'p ' + d, text: F.price(item.price, item.market) }),
      h('div', { class: 'd ' + d }, [
        h('span', { text: F.signed(item.change, 2) }),
        h('span', { text: F.pct(item.changePct) }),
      ]),
    ]);
  }

  function breadthBlock(b) {
    const total = Math.max(1, b.up + b.down + b.flat);
    const segs = [
      { v: b.up, c: 'var(--up)', label: '上涨' },
      { v: b.flat, c: '#5d677a', label: '平盘' },
      { v: b.down, c: 'var(--down)', label: '下跌' },
    ];
    const bar = h('div', { class: 'breadth-bar' });
    segs.forEach((s) => {
      bar.appendChild(h('div', {
        style: { flex: String(Math.max(s.v / total, s.v > 0 ? 0.004 : 0)), background: s.c },
        title: s.label + ' ' + s.v + ' 家',
      }));
    });
    const legend = h('div', { class: 'breadth-legend' });
    legend.appendChild(h('span', {}, [h('b', { class: 'up', text: String(b.up) }), ' 上涨']));
    legend.appendChild(h('span', {}, [h('b', { class: 'flat', text: String(b.flat) }), ' 平盘']));
    legend.appendChild(h('span', {}, [h('b', { class: 'down', text: String(b.down) }), ' 下跌']));
    const upRatio = (b.up / Math.max(1, b.up + b.down) * 100).toFixed(1);
    legend.appendChild(h('span', {}, ['上涨占比 ', h('b', { text: upRatio + '%' })]));
    legend.appendChild(h('span', {}, ['两市成交额 ', h('b', { text: F.amt(b.amount, 'cn') })]));

    const buckets = h('div', { style: { marginTop: '12px', display: 'grid', gap: '3px' } });
    const maxCnt = Math.max.apply(null, b.buckets.map((x) => x.count).concat([1]));
    b.buckets.forEach((bk) => {
      const isUp = bk.label.indexOf('涨') >= 0 || bk.label.indexOf('≥') >= 0;
      const col = bk.label.indexOf('跌') >= 0 || bk.label.indexOf('≤') >= 0 ? 'var(--down)'
        : (bk.label.indexOf('涨') >= 0 ? 'var(--up)' : '#5d677a');
      buckets.appendChild(h('div', { style: { display: 'grid', gridTemplateColumns: '150px 1fr 52px', alignItems: 'center', gap: '8px', fontSize: '11px' } }, [
        h('span', { class: 'dim3', text: bk.label }),
        h('div', { style: { height: '7px', background: '#141821', borderRadius: '4px', overflow: 'hidden' } }, [
          h('div', { style: { width: (bk.count / maxCnt * 100).toFixed(1) + '%', height: '100%', background: col, opacity: '.75' } }),
        ]),
        h('span', { class: 'num dim', style: { textAlign: 'right' }, text: String(bk.count) }),
      ]));
    });

    return h('div', {}, [bar, legend, buckets]);
  }

  function sectorTiles(rows, ctx) {
    const grid = h('div', { class: 'sector-grid' });
    const list = rows.slice(0, 36);
    const maxAbs = Math.max.apply(null, list.map((r) => Math.abs(r.changePct || 0)).concat([1]));
    list.forEach((r) => {
      const alpha = Math.min(0.42, 0.08 + Math.abs(r.changePct || 0) / maxAbs * 0.34);
      const bg = (r.changePct >= 0 ? 'rgba(255,77,79,' : 'rgba(18,196,139,') + alpha.toFixed(2) + ')';
      grid.appendChild(h('div', {
        class: 'sector-tile',
        style: { background: bg, borderColor: 'transparent' },
        title: (r.name || '') + '  主力净流入 ' + F.amt(r.mainInflow, 'cn'),
        on: { click: () => ctx.openSector(r) },
      }, [
        h('div', { class: 's-name', text: (r.name || '') }),
        h('div', { class: 's-pct ' + F.dir(r.changePct), text: F.pct(r.changePct) }),
        h('div', { class: 's-lead', text: (r.leader ? '领涨 ' + r.leader : '') + (isFinite(r.leaderPct) ? ' ' + F.pct(r.leaderPct) : '') }),
      ]));
    });
    return grid;
  }

  function mount(root, ctx) {
    const state = { rank: 'gainers', sectorKind: 'industry', data: null, sectors: null, movers: null };
    let rankTable = null;

    const rankHost = h('div', {}, [ui.loading('榜单加载中…')]);
    const sectorHost = h('div', {}, [ui.loading('板块数据加载中…')]);
    const moversHost = h('div', {}, [ui.loading('异动扫描中…')]);
    const idxHost = h('div', { class: 'idx-strip' });
    const breadthHost = h('div', {}, [ui.loading('全市场快照加载中…首次加载约需 10~20 秒')]);
    const metaHost = h('span', { class: 'hint' });

    const meta = () => {
      metaHost.textContent = state.data
        ? (state.data.breadthScope + ' · ' + state.data.sampleSize + ' 只样本 · 更新 ' +
          F.clock(state.data.updated || Date.now()) +
          ' · 快照源 ' + (state.data.source || '—') +
          (state.data.stale ? '（本地缓存，数据可能延迟）' : ''))
        : '加载中…';
    };

    const refreshBtn = h('button', {
      class: 'btn sm', text: '刷新',
      on: { click: () => { refresh(true); } },
    });

    const rankSeg = ui.seg(RANK_TABS, state.rank, (v) => {
      state.rank = v;
      Array.prototype.forEach.call(rankSeg.children, (b, i) => {
        b.classList.toggle('active', RANK_TABS[i].value === v);
      });
      renderRank();
    });

    function rankRows() {
      if (!state.data) return [];
      const map = {
        gainers: state.data.gainers, losers: state.data.losers, actives: state.data.actives,
        highTurnover: state.data.highTurnover, inflow: state.data.inflow, volumeRatio: state.data.volumeRatio,
      };
      return map[state.rank] || [];
    }

    function rankCols() {
      const cols = [
        { key: 'name', label: '名称', noSort: true, render: (r) => ui.cells.name(r) },
        { key: 'price', label: '最新价', cls: 'n', value: (r) => r.price, render: (r) => ui.cells.price(r) },
        { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
        { key: 'amount', label: '成交额', cls: 'n', value: (r) => r.amount, render: (r) => ui.cells.amount(r) },
        { key: 'turnover', label: '换手率', cls: 'n', value: (r) => r.turnover, render: (r) => h('span', { class: 'num', text: isFinite(r.turnover) ? F.num(r.turnover, 2) + '%' : '—' }) },
        { key: 'volumeRatio', label: '量比', cls: 'n', value: (r) => r.volumeRatio, render: (r) => h('span', { class: 'num', text: F.num(r.volumeRatio, 2) }) },
        { key: 'marketCap', label: '总市值', cls: 'n', value: (r) => r.marketCap, render: (r) => ui.cells.cap(r) },
        { key: 'mainInflow', label: '主力净额', cls: 'n', value: (r) => r.mainInflow, render: (r) => ui.cells.flow(r) },
        { key: 'act', label: '', noSort: true, width: '38px', render: (r) => ui.cells.star(ctx.isWatched(r.market, r.code), () => { ctx.toggleWatch(r.market, r.code, r.name); rankTable.update(rankRows(), true); }) },
      ];
      if (ctx.state.market === 'us') return cols.filter((c) => ['name', 'price', 'changePct', 'amount', 'marketCap', 'act'].indexOf(c.key) >= 0);
      return cols;
    }

    function renderRank() {
      rankTable = ui.tbl({
        cols: rankCols(),
        rows: rankRows(),
        sortKey: state.rank === 'gainers' || state.rank === 'volumeRatio' ? 'changePct' : null,
        maxHeight: '520px',
        onRow: (r) => ctx.openSymbol(r.market, r.code, r.name),
        emptyText: '暂无榜单数据',
      });
      clear(rankHost);
      rankHost.appendChild(rankTable);
    }

    function renderMovers() {
      const rows = (state.movers && state.movers.rows) || [];
      clear(moversHost);
      const host = h('div', { class: 'tbl-wrap' });
      if (!rows.length) { host.appendChild(ui.empty('当前没有捕捉到明显异动')); moversHost.appendChild(host); return; }
      const scroll = h('div', { class: 'tbl-scroll', style: { maxHeight: '300px' } });
      const table = h('table', { class: 'tbl compact' });
      table.appendChild(h('thead', {}, [h('tr', {}, [
        h('th', { text: '标的' }), h('th', { class: 'n', text: '现价' }),
        h('th', { class: 'n', text: '涨跌幅' }), h('th', { text: '异动特征' }),
      ])]));
      const tb = h('tbody');
      rows.slice(0, 26).forEach((r) => {
        tb.appendChild(h('tr', { on: { click: () => ctx.openSymbol(r.market, r.code, r.name) } }, [
          h('td', {}, [ui.cells.name(r)]),
          h('td', { class: 'n' }, [ui.cells.price(r)]),
          h('td', { class: 'n' }, [pct(r.changePct)]),
          h('td', {}, [h('span', { style: { display: 'inline-flex', gap: '4px', flexWrap: 'wrap' } },
            (r.tags || []).map((t) => h('span', {
              class: 'chip ' + (t.indexOf('跌') >= 0 || t.indexOf('跳水') >= 0 || t.indexOf('流出') >= 0
                ? 'down' : (t.indexOf('涨停') >= 0 ? 'up' : 'accent')),
              text: t,
            })))]),
        ]));
      });
      table.appendChild(tb);
      scroll.appendChild(table);
      host.appendChild(scroll);
      moversHost.appendChild(host);
    }

    function renderSectors() {
      clear(sectorHost);
      const rows = (state.sectors && state.sectors.rows) || [];
      if (!rows.length) { sectorHost.appendChild(ui.empty('板块数据暂不可用')); return; }
      sectorHost.appendChild(sectorTiles(rows, ctx));
      const top = rows.slice(0, 5).map((r) => r.name + ' ' + F.pct(r.changePct)).join(' · ');
      const foot = h('div', { class: 'legend-inline', style: { marginTop: '10px' } }, ['领涨板块：' + top]);
      sectorHost.appendChild(foot);
    }

    async function refresh(manual) {
      if (manual) refreshBtn.classList.add('spin');
      try {
        const market = ctx.state.market;
        /* 指数条单独先请求，避免等待全市场快照 */
        api.indices(market).then((res) => {
          if (!res.rows || !res.rows.length) return;
          clear(idxHost);
          res.rows.forEach((it) => idxHost.appendChild(idxCell(it, ctx)));
        }).catch(() => {});
        const [ov, sec, mv] = await Promise.all([
          api.overview(market, 20),
          market === 'cn' ? api.sectors(state.sectorKind).catch(() => null) : Promise.resolve(null),
          api.movers(market).catch(() => null),
        ]);
        state.data = ov;
        state.sectors = sec;
        state.movers = mv;
        if (!idxHost.children.length) {
          (ov.indices || []).forEach((it) => idxHost.appendChild(idxCell(it, ctx)));
        }
        clear(breadthHost);
        if (market === 'cn') breadthHost.appendChild(breadthBlock(ov.breadth));
        else {
          breadthHost.appendChild(h('div', { class: 'legend-inline' }, [
            '上涨 ' + ov.breadth.up + ' · 平盘 ' + ov.breadth.flat + ' · 下跌 ' + ov.breadth.down,
            '（口径：' + ov.breadthScope + '）',
          ]));
          const b2 = h('div', { class: 'breadth-bar', style: { marginTop: '8px' } });
          const tot = Math.max(1, ov.breadth.up + ov.breadth.down + ov.breadth.flat);
          [['var(--up)', ov.breadth.up], ['#5d677a', ov.breadth.flat], ['var(--down)', ov.breadth.down]].forEach((s) => {
            b2.appendChild(h('div', { style: { flex: String(Math.max(s[1] / tot, 0.004)), background: s[0] } }));
          });
          breadthHost.appendChild(b2);
        }
        renderRank();
        renderSectors();
        renderMovers();
        meta();
      } catch (e) {
        ctx.toast('市场数据获取失败：' + e.message, 'err');
        clear(idxHost);
        idxHost.appendChild(ui.empty('行情数据暂不可用，请稍后重试'));
      } finally {
        refreshBtn.classList.remove('spin');
      }
    }

    const sectorSeg = ui.seg([{ value: 'industry', label: '行业' }, { value: 'concept', label: '概念' }], state.sectorKind, async (v) => {
      state.sectorKind = v;
      Array.prototype.forEach.call(sectorSeg.children, (b) => b.classList.toggle('active', b.textContent === (v === 'industry' ? '行业' : '概念')));
      state.sectors = await api.sectors(v).catch(() => null);
      renderSectors();
    });

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead(
        '市场总览 · ' + (ctx.state.market === 'cn' ? '沪深 A 股' : '美股'),
        ctx.state.market === 'cn'
          ? '全市场实时快照：涨跌分布、热门榜单、板块资金与异动雷达'
          : '美股活跃样本（成交额前 600）实时快照与热门榜单',
        [metaHost, refreshBtn]
      ),
      idxHost,
      h('div', { style: { height: '20px' } }),
      h('div', { class: 'grid g-side' }, [
        h('div', {}, [
          ui.section('涨跌分布', ctx.state.market === 'cn' ? '按全市场个股涨跌幅分档统计' : '样本口径', [], breadthHost),
          ui.section('热门榜单', '点击表头可二次排序', [rankSeg], rankHost),
        ]),
        h('div', {}, [
          ctx.state.market === 'cn'
            ? ui.section('板块热力', '点击查看板块成分', [sectorSeg], sectorHost)
            : ui.section('明星股 / 中概股', '常用观察清单', [], h('div', { id: 'us-hot' })),
          ui.section('异动雷达', '基于量比 / 涨速 / 换手 / 主力净额自动标注', [], moversHost),
        ]),
      ]),
      h('div', { class: 'legend-inline', style: { marginTop: '22px', color: 'var(--text-3)' } }, [
        '数据来源：腾讯行情 / 东方财富 / 新浪财经（公开接口，可能存在延迟）。本系统仅用于研究与学习，不构成任何投资建议。',
      ]),
    ]));

    if (ctx.state.market === 'us') {
      const host = root.querySelector('#us-hot');
      if (host) {
        const render = (rows) => {
          clear(host);
          host.appendChild(ui.tbl({
            cols: [
              { key: 'name', label: '名称', noSort: true, render: (r) => ui.cells.name(r) },
              { key: 'price', label: '最新价', cls: 'n', value: (r) => r.price, render: (r) => ui.cells.price(r) },
              { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
              { key: 'amount', label: '成交额', cls: 'n', value: (r) => r.amount, render: (r) => ui.cells.amount(r) },
            ],
            rows, onRow: (r) => ctx.openSymbol('us', r.code, r.name), maxHeight: '240px', compact: true,
          }));
        };
        api.quote('us', US_HOT.concat(US_CN_ADR)).then((res) => render(res.rows || [])).catch(() => render([]));
      }
    }

    refresh(false);
    return { refresh, destroy() {} };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.market = { mount };
})();
