/* ==========================================================================
   视图 · 策略回测（单标的 · 全仓 · 次日开盘成交 · 含手续费与滑点）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;
  const Q = window.AD.quant;

  const PERIODS = [{ value: 'day', label: '日K' }, { value: 'week', label: '周K' }, { value: '60m', label: '60分钟' }, { value: '30m', label: '30分钟' }];

  function mount(root, ctx) {
    const sym = ctx.state.symbol || { market: ctx.state.market, code: '', name: '' };
    const st = {
      market: sym.market || ctx.state.market,
      code: sym.code || '',
      name: sym.name || '',
      period: 'day', strategy: 'maCross', params: {},
      bars: null, result: null, loading: false,
    };
    let eqChart = null;
    let kChart = null;

    const paramHost = h('div', { class: 'filter-grid' });
    const statHost = h('div');
    const eqHost = h('div', { style: { height: '240px' } });
    const kHost = h('div');
    const tradeHost = h('div');
    const codeInput = h('input', { class: 'inp', value: st.code, placeholder: '代码，如 600519 / AAPL', style: { width: '170px' } });
    const nameLabel = h('span', { class: 'dim', text: st.name || '—' });
    const periodSel = h('select', { class: 'inp', style: { width: '100px' } });
    PERIODS.forEach((p) => periodSel.appendChild(h('option', { value: p.value, text: p.label })));
    periodSel.value = st.period;
    periodSel.addEventListener('change', () => { st.period = periodSel.value; });

    const stratSel = h('select', { class: 'inp', style: { width: '170px' } });
    Object.keys(Q.STRATEGIES).forEach((k) => stratSel.appendChild(h('option', { value: k, text: Q.STRATEGIES[k].name })));
    stratSel.value = st.strategy;

    const feeInput = h('input', { class: 'inp', value: '0.0003', title: '单边手续费率' });
    const slipInput = h('input', { class: 'inp', value: '0.001', title: '滑点' });
    const slInput = h('input', { class: 'inp', value: '8', title: '止损百分比，0 表示不启用' });
    const tpInput = h('input', { class: 'inp', value: '0', title: '止盈百分比，0 表示不启用' });
    const capInput = h('input', { class: 'inp', value: '100000', title: '初始资金' });

    function renderParams() {
      const strat = Q.STRATEGIES[st.strategy];
      clear(paramHost);
      st.params = {};
      strat.params.forEach((pd) => {
        const inp = h('input', { class: 'inp', value: String(pd.def), min: pd.min, max: pd.max });
        st.params[pd.key] = inp;
        paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: pd.label }), inp]));
      });
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '手续费率' }), feeInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '滑点' }), slipInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '止损 %' }), slInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '止盈 %' }), tpInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '初始资金' }), capInput]));
    }

    stratSel.addEventListener('change', () => { st.strategy = stratSel.value; renderParams(); });

    function statCard(k, v, cls) {
      return h('div', { class: 'bt-stat' }, [h('div', { class: 'k', text: k }), h('div', { class: 'v ' + (cls || ''), text: v })]);
    }

    async function run() {
      const code = (codeInput.value || '').trim().toUpperCase();
      if (!code) { ctx.toast('请先填写标的代码', 'warn'); return; }
      st.code = code;
      ctx.toast('回测中：' + code, 'info');
      clear(kHost); clear(tradeHost); clear(statHost); clear(eqHost);
      statHost.appendChild(ui.loading('拉取历史行情并计算中…'));
      try {
        if (!st.name) {
          try {
            const s = await api.search(code);
            const hit = (s.rows || []).find((r) => r.market === st.market) || (s.rows || [])[0];
            if (hit) { st.name = hit.name; nameLabel.textContent = hit.name; if (hit.market) st.market = hit.market; }
          } catch (e) { /* 名称可选 */ }
        }
        const res = await api.kline(st.market, code, st.period, 1, 800);
        if (!res.bars || res.bars.length < 60) {
          clear(statHost);
          statHost.appendChild(ui.empty('历史数据不足（' + (res.bars ? res.bars.length : 0) + ' 根）' + (res.error ? '：' + res.error : '')));
          return;
        }
        st.bars = res.bars;
        const params = {};
        Object.keys(st.params).forEach((k) => { params[k] = Number(st.params[k].value); });
        const result = Q.backtest(res.bars, st.strategy, params, {
          feeRate: Number(feeInput.value) || 0.0003,
          slippage: Number(slipInput.value) || 0,
          stopLoss: Number(slInput.value) || 0,
          takeProfit: Number(tpInput.value) || 0,
          initial: Number(capInput.value) || 100000,
        });
        st.result = result;
        renderStats(result, res);
        renderEquity(result);
        renderKline(result, res);
        renderTrades(result);
      } catch (e) {
        clear(statHost);
        statHost.appendChild(ui.empty('回测失败：' + e.message));
      }
    }

    function renderStats(r, res) {
      clear(statHost);
      if (!r.ok) { statHost.appendChild(ui.empty(r.message)); return; }
      const s = r.stats;
      const grid = h('div', { class: 'bt-stats' });
      grid.appendChild(statCard('策略总收益', F.pct(s.totalReturn), F.dir(s.totalReturn)));
      grid.appendChild(statCard('年化收益', F.pct(s.annual), F.dir(s.annual)));
      grid.appendChild(statCard('最大回撤', '-' + F.num(s.maxDD, 2) + '%', 'down'));
      grid.appendChild(statCard('胜率', F.num(s.winRate, 1) + '%', s.winRate >= 50 ? 'up' : ''));
      grid.appendChild(statCard('交易次数', String(s.trades)));
      grid.appendChild(statCard('盈亏比', F.num(s.profitFactor, 2)));
      grid.appendChild(statCard('平均盈利', F.amt(s.avgWin, st.market), 'up'));
      grid.appendChild(statCard('平均亏损', F.amt(s.avgLoss, st.market), 'down'));
      grid.appendChild(statCard('平均持仓', F.num(s.avgHold, 1) + ' 根'));
      grid.appendChild(statCard('期末权益', F.amt(s.finalEquity, st.market)));
      grid.appendChild(statCard('买入持有基准', F.pct(s.benchmark), F.dir(s.benchmark)));
      grid.appendChild(statCard('超额收益', F.pct(s.totalReturn - s.benchmark), F.dir(s.totalReturn - s.benchmark)));
      statHost.appendChild(grid);
      statHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '10px' } }, [
        '标的：' + (st.name || st.code) + '（' + (st.market === 'us' ? 'US:' : '') + st.code + '） · 周期 ' +
        periodSel.options[periodSel.selectedIndex].text + ' · 策略：' + r.strategy + ' · 数据源 ' + (res.source || '') +
        ' · 共 ' + s.bars + ' 根K线 · 手续费 ' + feeInput.value + ' / 滑点 ' + slipInput.value +
        (Number(slInput.value) ? ' / 止损 ' + slInput.value + '%' : ''),
        '成交假设：信号出现后的下一根K线开盘价成交，全仓买入、信号卖出，未考虑涨跌停与流动性限制',
      ]));
    }

    function renderEquity(r) {
      if (eqChart) { eqChart.destroy(); eqChart = null; }
      clear(eqHost);
      const data = r.equity.map((e) => ({ t: e.t, v: e.v }));
      const firstClose = r.equity.length && r.equity[0].close ? r.equity[0].close : null;
      const base = r.stats.initial;
      const bench = firstClose
        ? r.equity.map((e) => ({ t: e.t, v: e.close ? base * (e.close / firstClose) : base }))
        : [];
      const series = [{
        name: '策略权益', data, color: 'var(--accent)', fill: 'rgba(77,141,255,0.12)',
        width: 1.6, fmt: (v) => F.amt(v, st.market),
      }];
      if (bench.length) {
        series.push({ name: '买入持有', data: bench, color: '#8b95a5', width: 1.2, fmt: (v) => F.amt(v, st.market) });
      }
      eqChart = window.AD.chart.line(eqHost, {
        height: 240, fmt: (v) => F.amt(v, st.market), series,
      });
    }

    function renderKline(r, res) {
      clear(kHost);
      const host = h('div', { class: 'chart-panel' }, [
        h('div', { class: 'chart-toolbar' }, [h('span', { class: 'dim', text: '红 ▲ = 买入信号　绿 ▼ = 卖出信号（标记于信号出现的K线）' })]),
        h('div', { class: 'chart-legend' }, []),
        h('div', {}),
      ]);
      kHost.appendChild(host);
      const canvasWrap = host.lastChild;
      const legend = host.children[1];
      if (kChart) { kChart.destroy(); kChart = null; }
      const marks = r.marks.map((m) => {
        const idx = st.bars.findIndex((b) => b.t === m.t);
        return { idx, dir: m.dir };
      }).filter((m) => m.idx >= 0);
      kChart = window.AD.chart.kline(canvasWrap, {
        height: 360, period: st.period, market: st.market, showMA: true, sub: 'MACD', marks,
        onLegend: (rows) => {
          legend.innerHTML = '';
          rows.forEach((row) => legend.appendChild(h('i', { style: { color: row.color }, text: row.text })));
        },
      });
      kChart.setData(st.bars, { marks });
    }

    function renderTrades(r) {
      clear(tradeHost);
      if (!r.trades.length) { tradeHost.appendChild(ui.empty('该策略在此区间内没有产生交易')); return; }
      tradeHost.appendChild(ui.tbl({
        cols: [
          { key: 'inDate', label: '买入日', noSort: true, render: (x) => h('span', { class: 'num', text: x.inDate }) },
          { key: 'inPrice', label: '买入价', cls: 'n', noSort: true, render: (x) => h('span', { class: 'num', text: F.price(x.inPrice, st.market) }) },
          { key: 'outDate', label: '卖出日', noSort: true, render: (x) => h('span', { class: 'num', text: x.outDate }) },
          { key: 'outPrice', label: '卖出价', cls: 'n', noSort: true, render: (x) => h('span', { class: 'num', text: F.price(x.outPrice, st.market) }) },
          { key: 'qty', label: '数量', cls: 'n', noSort: true, render: (x) => h('span', { class: 'num', text: String(x.qty) }) },
          { key: 'pnlPct', label: '收益率', cls: 'n', value: (x) => x.pnlPct, render: (x) => pct(x.pnlPct) },
          { key: 'pnl', label: '盈亏', cls: 'n', value: (x) => x.pnl, render: (x) => h('span', { class: 'num ' + F.dir(x.pnl), text: F.amt(x.pnl, st.market) }) },
          { key: 'holdBars', label: '持仓', cls: 'n', value: (x) => x.holdBars, render: (x) => h('span', { class: 'num', text: x.holdBars + ' 根' }) },
          { key: 'reason', label: '平仓原因', noSort: true },
        ],
        rows: r.trades.slice().reverse(),
        sortKey: 'pnlPct', maxHeight: '340px', compact: true,
      }));
    }

    renderParams();

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('策略回测', '单标的 · 全仓 · 信号次日开盘成交，含手续费 / 滑点 / 止损止盈', [
        nameLabel,
        codeInput,
        periodSel,
        h('button', {
          class: 'btn sm', text: '转为持续跟踪',
          title: '把这个策略放到服务端一直跑，观测 3 个月的胜率与盈亏',
          on: {
            click: () => {
              const params = {};
              Object.keys(st.params).forEach((k) => { params[k] = Number(st.params[k].value); });
              ctx.openTracker(st.market, (codeInput.value || st.code || '').trim().toUpperCase(),
                st.name || nameLabel.textContent, { strategy: st.strategy, params, period: st.period });
            },
          },
        }),
        h('button', { class: 'btn primary sm', text: '运行回测', on: { click: run } }),
      ]),
      ui.section('策略与参数', '内置 5 类经典策略，可调整参数与交易成本假设', [], h('div', {}, [
        h('div', { class: 'field', style: { marginBottom: '12px' } }, [h('label', { text: '策略' }), stratSel,
          h('span', { class: 'dim3', text: Q.STRATEGIES[st.strategy].desc })]),
        paramHost,
      ])),
      ui.section('回测结果', '', [], statHost),
      h('div', { class: 'grid g-2' }, [
        ui.section('策略权益曲线', '初始资金等权全仓', [], eqHost),
        ui.section('买卖点标注', '', [], kHost),
      ]),
      ui.section('交易明细', '', [], tradeHost),
    ]));

    if (st.code) run();
    return { refresh() {}, destroy() { if (eqChart) eqChart.destroy(); if (kChart) kChart.destroy(); } };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.backtest = { mount };
})();
