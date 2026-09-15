/* ==========================================================================
   视图 · 策略回测（单标的 · 全仓 · 含手续费 / 滑点 / 止损止盈）

   口径说明：信号计算与撮合全部在服务端完成（POST /api/backtest），
   与「策略跟踪」共用同一份实现，前端只负责画图与展示，
   不再自行计算信号，避免同一策略出现两份口径。
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  function mount(root, ctx) {
    const sym = ctx.state.symbol || { market: ctx.state.market, code: '', name: '' };
    const st = {
      market: sym.market || ctx.state.market,
      code: sym.code || '',
      name: sym.name || '',
      period: 'day', strategy: 'maCross', params: {},
      fillModel: 'nextOpen', metricsMode: 'compound',
      bars: null, result: null, meta: null, search: null, loading: false,
    };
    let eqChart = null;
    let kChart = null;

    const paramHost = h('div', { class: 'filter-grid' });
    const statHost = h('div');
    const eqHost = h('div', { style: { height: '240px' } });
    const kHost = h('div');
    const tradeHost = h('div');
    const searchHost = h('div');
    const codeInput = h('input', { class: 'inp', value: st.code, placeholder: '代码，如 600519 / AAPL', style: { width: '170px' } });
    const nameLabel = h('span', { class: 'dim', text: st.name || '—' });
    const periodSel = h('select', { class: 'inp', style: { width: '100px' } });
    const stratDesc = h('span', { class: 'dim3', text: '' });

    const stratSel = h('select', { class: 'inp', style: { width: '170px' } });
    const fillSel = h('select', { class: 'inp', style: { width: '160px' } });
    const modeSel = h('select', { class: 'inp', style: { width: '190px' } });
    const feeInput = h('input', { class: 'inp', value: '0.0003', title: '单边手续费率' });
    const slipInput = h('input', { class: 'inp', value: '0.001', title: '滑点' });
    const slInput = h('input', { class: 'inp', value: '8', title: '止损百分比，0 表示不启用' });
    const tpInput = h('input', { class: 'inp', value: '0', title: '止盈百分比，0 表示不启用' });
    const capInput = h('input', { class: 'inp', value: '100000', title: '初始资金' });

    function currentParams() {
      const params = {};
      Object.keys(st.params).forEach((k) => { params[k] = Number(st.params[k].value); });
      return params;
    }

    function renderParams() {
      const strategies = (st.meta && st.meta.strategies) || {};
      const strat = strategies[st.strategy] || { params: [] };
      clear(paramHost);
      st.params = {};
      strat.params.forEach((pd) => {
        const inp = h('input', { class: 'inp', value: String(pd.def), min: pd.min, max: pd.max, step: pd.step || 1 });
        st.params[pd.key] = inp;
        paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: pd.label }), inp]));
      });
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '手续费率' }), feeInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '滑点' }), slipInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '止损 %' }), slInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '止盈 %' }), tpInput]));
      paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: '初始资金' }), capInput]));
      stratDesc.textContent = strat.desc || '';
    }

    stratSel.addEventListener('change', () => {
      st.strategy = stratSel.value;
      const strategies = (st.meta && st.meta.strategies) || {};
      const strat = strategies[st.strategy] || { params: [] };
      const params = {};
      strat.params.forEach((pd) => { params[pd.key] = pd.def; });
      st.strategy = stratSel.value;
      renderParams();
      Object.keys(params).forEach((k) => {
        if (st.params[k]) st.params[k].value = String(params[k]);
      });
    });

    function statCard(k, v, cls) {
      return h('div', { class: 'bt-stat' }, [h('div', { class: 'k', text: k }), h('div', { class: 'v ' + (cls || ''), text: v })]);
    }

    function body() {
      return {
        market: st.market, code: st.code, strategy: st.strategy, params: currentParams(),
        period: st.period, limit: 800,
        fee: Number(feeInput.value) || 0, slippage: Number(slipInput.value) || 0,
        stopLoss: Number(slInput.value) || 0, takeProfit: Number(tpInput.value) || 0,
        initial: Number(capInput.value) || 100000,
        fillModel: fillSel.value || 'nextOpen', metricsMode: modeSel.value || 'compound',
      };
    }

    async function run() {
      const code = (codeInput.value || '').trim().toUpperCase();
      if (!code) { ctx.toast('请先填写标的代码', 'warn'); return; }
      st.code = code;
      clear(kHost); clear(tradeHost); clear(statHost); clear(eqHost);
      statHost.appendChild(ui.loading('服务端回测中：拉取行情并统一撮合…'));
      try {
        if (!st.name) {
          try {
            const s = await api.search(code);
            const hit = (s.rows || []).find((r) => r.market === st.market) || (s.rows || [])[0];
            if (hit) { st.name = hit.name; nameLabel.textContent = hit.name; if (hit.market) st.market = hit.market; }
          } catch (e) { /* 名称可选 */ }
        }
        const res = await api.backtest(body());
        if (!res.ok) {
          clear(statHost);
          statHost.appendChild(ui.empty(res.message || '回测失败'));
          return;
        }
        st.result = res;
        st.bars = null;                       // K线由服务端结果驱动，避免两侧不一致
        const bars = await api.kline(st.market, st.code, st.period, 1, 800);
        st.bars = bars.bars || [];
        renderStats(res);
        renderEquity(res);
        renderKline(res);
        renderTrades(res);
      } catch (e) {
        clear(statHost);
        statHost.appendChild(ui.empty('回测失败：' + e.message));
      }
    }

    function renderStats(res) {
      clear(statHost);
      const s = res.stats;
      const invested = s.initial || 1;
      const costRatio = s.costTotal ? (s.costTotal / invested) * 100 : 0;
      const grid = h('div', { class: 'bt-stats' });
      grid.appendChild(statCard('策略总收益', F.pct(s.returnPct / 100), F.dir(s.returnPct)));
      grid.appendChild(statCard('年化收益', F.pct(s.annualizedPct / 100), F.dir(s.annualizedPct)));
      grid.appendChild(statCard('最大回撤', '-' + F.num(s.maxDrawdown, 2) + '%', 'down'));
      grid.appendChild(statCard('夏普比率', F.num(s.sharpe, 2), s.sharpe > 1 ? 'up' : ''));
      grid.appendChild(statCard('索提诺', F.num(s.sortino, 2), s.sortino > 1 ? 'up' : ''));
      grid.appendChild(statCard('卡玛比率', F.num(s.calmar, 2), s.calmar > 1 ? 'up' : ''));
      grid.appendChild(statCard('胜率', F.num(s.winRate, 1) + '%', s.winRate >= 50 ? 'up' : ''));
      grid.appendChild(statCard('盈亏比', (s.profitFactorInfinite || s.profitFactor === null || !isFinite(s.profitFactor)) ? '∞（无亏损）' : F.num(s.profitFactor, 2)));
      grid.appendChild(statCard('期望值/笔', F.amt(s.expectancy, st.market), F.dir(s.expectancy)));
      grid.appendChild(statCard('交易次数', String(s.trades)));
      grid.appendChild(statCard('平均持仓', F.num(s.avgHoldBars, 1) + ' 根'));
      grid.appendChild(statCard('持仓占比', F.num(s.exposure, 1) + '%'));
      grid.appendChild(statCard('年化波动', F.num(s.annualizedVol, 2) + '%'));
      grid.appendChild(statCard('Alpha / Beta', F.num(s.alpha, 3) + ' / ' + F.num(s.beta, 2)));
      grid.appendChild(statCard('单日 VaR(95%)', F.num(s.var95, 2) + '%', 'down'));
      grid.appendChild(statCard('期末权益', F.amt(s.equityNow, st.market)));
      grid.appendChild(statCard('买入持有基准', F.pct((s.benchmarkPct || 0) / 100), F.dir(s.benchmarkPct || 0)));
      grid.appendChild(statCard('超额收益', F.pct((s.excessPct || 0) / 100), F.dir(s.excessPct || 0)));
      grid.appendChild(statCard('交易成本合计', F.amt(s.costTotal, st.market), 'down'));
      grid.appendChild(statCard('成本 / 初始资金', F.num(costRatio, 2) + '%', 'down'));
      grid.appendChild(statCard('其中手续费 / 滑点', F.num(s.feeTotal, 0) + ' / ' + F.num(s.slippageTotal, 0)));
      statHost.appendChild(grid);
      if (res.skippedBuys) {
        const need = res.minCapital || 0;
        const box = h('div', { class: 'adjust-box', style: { marginTop: '12px', borderColor: '#FFE0B8', background: '#FFFBF5' } }, [
          h('div', { class: 'legend-inline' }, [
            h('span', { class: 'chip warn', text: '有信号被资金约束跳过 ' + res.skippedBuys + ' 次' }),
            res.trades === 0 || s.trades === 0
              ? '当前初始资金买不起一手（' + (res.cost ? res.cost.lot : 100) + ' 股约需 ' + F.amt(need, st.market) + '），所以没有成交'
              : '部分买入信号因资金不足被跳过，统计口径已排除这些信号',
          ]),
          need > Number(capInput.value) ? h('button', {
            class: 'btn sm', style: { marginTop: '8px' },
            text: '把初始资金提高到 ' + F.amt(Math.ceil(need / 10000) * 10000, st.market) + ' 并重跑',
            on: {
              click: () => {
                capInput.value = String(Math.ceil(need / 10000) * 10000);
                run();
              },
            },
          }) : null,
        ]);
        statHost.appendChild(box);
      }
      const fm = (st.meta && st.meta.fillModels && st.meta.fillModels[res.fillModel]) || {};
      statHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '10px' } }, [
        '标的：' + (st.name || st.code) + '（' + (st.market === 'us' ? 'US:' : '') + st.code + '） · 周期 ' +
        periodSel.options[periodSel.selectedIndex].text + ' · 策略：' + res.strategyName + '（' + describeParams(res.params) + '） · ' +
        '共 ' + s.barCount + ' 根K线（预热 ' + s.warmupBars + ' 根）',
        '成交模型：' + (fm.name || res.fillModel) + ' · ' + (fm.desc || '') + ' · 累计口径：' +
        (res.metricsMode === 'simple' ? '算术累加' : '几何累乘') + ' · 手续费 ' + feeInput.value + ' / 滑点 ' + slipInput.value +
        (Number(slInput.value) ? ' / 止损 ' + slInput.value + '%' : ''),
        '信号与撮合均由服务端引擎计算（与策略跟踪同源），信号在收盘确认、按成交模型执行；未考虑涨跌停与流动性限制',
      ]));
    }

    function describeParams(params) {
      const keys = Object.keys(params || {});
      if (!keys.length) return '';
      return keys.map((k) => k + '=' + F.num(params[k], 2).replace(/\.00$/, '')).join(' ');
    }

    function renderEquity(res) {
      if (eqChart) { eqChart.destroy(); eqChart = null; }
      clear(eqHost);
      const data = res.equity.map((e) => ({ t: e.t, v: e.v }));
      const firstClose = res.equity.length && res.equity[0].close ? res.equity[0].close : null;
      const base = res.stats.initial;
      const bench = firstClose
        ? res.equity.map((e) => ({ t: e.t, v: e.close ? base * (e.close / firstClose) : base }))
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

    function renderKline(res) {
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
      const bars = st.bars || [];
      const marks = (res.marks || []).map((m) => {
        const idx = bars.findIndex((b) => b.t === m.t);
        return { idx, dir: m.dir };
      }).filter((m) => m.idx >= 0);
      kChart = window.AD.chart.kline(canvasWrap, {
        height: 360, period: st.period, market: st.market, showMA: true, sub: 'MACD', marks,
        onLegend: (rows) => {
          legend.innerHTML = '';
          rows.forEach((row) => legend.appendChild(h('i', { style: { color: row.color }, text: row.text })));
        },
      });
      kChart.setData(bars, { marks });
    }

    function renderTrades(res) {
      clear(tradeHost);
      if (!res.trades.length) { tradeHost.appendChild(ui.empty('该策略在此区间内没有产生交易')); return; }
      tradeHost.appendChild(ui.tbl({
        cols: [
          { key: 'inDate', label: '买入日', noSort: true, render: (x) => h('span', { class: 'num', text: x.inDate }) },
          { key: 'inPrice', label: '买入价', cls: 'n', noSort: true, render: (x) => h('span', { class: 'num', text: F.price(x.inPrice, st.market) }) },
          { key: 'outDate', label: '卖出日', noSort: true, render: (x) => h('span', { class: 'num', text: x.outDate }) },
          { key: 'outPrice', label: '卖出价', cls: 'n', noSort: true, render: (x) => h('span', { class: 'num', text: F.price(x.outPrice, st.market) }) },
          { key: 'qty', label: '数量', cls: 'n', noSort: true, render: (x) => h('span', { class: 'num', text: String(x.qty) }) },
          { key: 'pnlPct', label: '收益率', cls: 'n', value: (x) => x.pnlPct, render: (x) => pct(x.pnlPct) },
          { key: 'pnl', label: '盈亏', cls: 'n', value: (x) => x.pnl, render: (x) => h('span', { class: 'num ' + F.dir(x.pnl), text: F.amt(x.pnl, st.market) }) },
          { key: 'fee', label: '成本', cls: 'n', value: (x) => (x.fee || 0) + (x.slippage || 0), render: (x) => h('span', { class: 'num', text: F.num((x.fee || 0) + (x.slippage || 0), 2) }) },
          { key: 'bars', label: '持仓', cls: 'n', value: (x) => x.bars, render: (x) => h('span', { class: 'num', text: x.bars + ' 根' }) },
          { key: 'reason', label: '平仓原因', noSort: true },
        ],
        rows: res.trades.slice().reverse(),
        sortKey: 'pnlPct', maxHeight: '340px', compact: true,
      }));
    }

    /* ------------------------------------------------------- 参数寻优 */

    const metricSel = h('select', { class: 'inp', style: { width: '150px' } }, [
      h('option', { value: 'sharpe', text: '夏普比率' }),
      h('option', { value: 'calmar', text: '卡玛比率' }),
      h('option', { value: 'returnPct', text: '累计收益' }),
      h('option', { value: 'annualizedPct', text: '年化收益' }),
      h('option', { value: 'profitFactor', text: '盈亏比' }),
    ]);
    const comboCtx = h('span', { class: 'dim3', text: '' });
    const spaceHost = h('div', { class: 'filter-grid' });

    function renderSpace() {
      clear(spaceHost);
      const strategies = (st.meta && st.meta.strategies) || {};
      const strat = strategies[st.strategy] || { params: [] };
      st.space = {};
      strat.params.forEach((pd) => {
        const minI = h('input', { class: 'inp', value: String(pd.min), style: { width: '70px' } });
        const maxI = h('input', { class: 'inp', value: String(pd.def), style: { width: '70px' } });
        const stepI = h('input', { class: 'inp', value: String(pd.step || 1), style: { width: '60px' } });
        const chk = h('input', { type: 'checkbox' });
        chk.addEventListener('change', updateCombo);
        minI.addEventListener('input', updateCombo);
        maxI.addEventListener('input', updateCombo);
        stepI.addEventListener('input', updateCombo);
        st.space[pd.key] = { pd, minI, maxI, stepI, chk };
        spaceHost.appendChild(h('div', { class: 'field' }, [
          h('label', {}, [chk, h('span', { text: ' ' + pd.label })]),
          h('div', { style: { display: 'flex', gap: '4px', alignItems: 'center' } }, [
            minI, h('span', { class: 'dim3', text: '~' }), maxI, h('span', { class: 'dim3', text: '步' }), stepI,
          ]),
        ]));
      });
      updateCombo();
    }

    function comboCount() {
      let n = 1;
      Object.keys(st.space || {}).forEach((k) => {
        const s = st.space[k];
        if (!s.chk.checked) return;
        const lo = Number(s.minI.value), hi = Number(s.maxI.value), step = Number(s.stepI.value) || 1;
        if (!(hi > lo) || step <= 0) return;
        n *= Math.min(12, Math.floor((hi - lo) / step) + 1);
      });
      return n;
    }

    function updateCombo() {
      const n = comboCount();
      const capped = Math.min(n, 64);
      comboCtx.textContent = '将测试 ' + capped + ' 组参数' + (n > 64 ? '（原 ' + n + ' 组，已截断）' : '');
    }

    async function search() {
      clear(searchHost);
      searchHost.appendChild(ui.loading('服务端执行网格寻优中…'));
      const space = {};
      Object.keys(st.space || {}).forEach((k) => {
        const s = st.space[k];
        space[k] = { enabled: s.chk.checked, min: Number(s.minI.value), max: Number(s.maxI.value), step: Number(s.stepI.value) || 1 };
      });
      try {
        const res = await api.searchParams(Object.assign(body(), {
          space, metric: metricSel.value, maxCombos: 64,
        }));
        st.search = res;
        renderSearch(res);
      } catch (e) {
        clear(searchHost);
        searchHost.appendChild(ui.empty('寻优失败：' + e.message));
      }
    }

    function renderSearch(res) {
      clear(searchHost);
      if (!res.rows || !res.rows.length) {
        searchHost.appendChild(ui.empty('没有可比较的结果，请检查参数区间'));
        return;
      }
      const best = res.best || res.rows[0];
      searchHost.appendChild(h('div', { class: 'legend-inline', style: { marginBottom: '10px' } }, [
        h('span', { class: 'chip accent', text: '最优：' + describeParams(best.params) }),
        '按 ' + metricLabel(res.metric) + ' 排序 · 共测试 ' + res.tested + ' 组 · 达标 ' + res.rows.length + ' 组',
        res.failed && res.failed.length ? h('span', { class: 'chip warn', text: res.failed.length + ' 组失败' }) : null,
      ]));
      const tbl = ui.tbl({
        cols: [
          { key: 'params', label: '参数', noSort: true, render: (x) => h('span', { class: 'mono', text: describeParams(x.params) }) },
          { key: 'score', label: metricLabel(res.metric), cls: 'n', value: (x) => x.score, render: (x) => h('span', { class: 'num', text: F.num(x.score, 3) }) },
          { key: 'returnPct', label: '累计收益', cls: 'n', value: (x) => x.returnPct, render: (x) => h('span', { class: 'num ' + F.dir(x.returnPct), text: F.num(x.returnPct, 2) + '%' }) },
          { key: 'annualizedPct', label: '年化', cls: 'n', value: (x) => x.annualizedPct, render: (x) => h('span', { class: 'num', text: F.num(x.annualizedPct, 1) + '%' }) },
          { key: 'maxDrawdown', label: '最大回撤', cls: 'n', value: (x) => x.maxDrawdown, render: (x) => h('span', { class: 'num down', text: '-' + F.num(x.maxDrawdown, 2) + '%' }) },
          { key: 'calmar', label: '卡玛', cls: 'n', value: (x) => x.calmar, render: (x) => h('span', { class: 'num', text: F.num(x.calmar, 2) }) },
          { key: 'winRate', label: '胜率', cls: 'n', value: (x) => x.winRate, render: (x) => h('span', { class: 'num', text: F.num(x.winRate, 1) + '%' }) },
          { key: 'trades', label: '交易', cls: 'n', value: (x) => x.trades, render: (x) => h('span', { class: 'num', text: String(x.trades) }) },
          { key: 'costTotal', label: '成本', cls: 'n', value: (x) => x.costTotal, render: (x) => h('span', { class: 'num', text: F.num(x.costTotal, 0) }) },
          {
            key: 'apply', label: '', noSort: true, render: (x) => h('button', {
              class: 'btn sm', text: '应用',
              on: {
                click: () => {
                  Object.keys(x.params).forEach((k) => {
                    if (st.params[k]) st.params[k].value = String(x.params[k]);
                  });
                  ctx.toast('已应用参数：' + describeParams(x.params) + '，点击「运行回测」查看', 'ok');
                },
              },
            }),
          },
        ],
        rows: res.rows, maxHeight: '320px', compact: true,
      });
      searchHost.appendChild(tbl);
    }

    function metricLabel(key) {
      const map = { sharpe: '夏普', calmar: '卡玛', returnPct: '累计收益', annualizedPct: '年化收益', profitFactor: '盈亏比' };
      return map[key] || key;
    }

    /* ------------------------------------------------------- 启动 */

    async function boot() {
      try {
        st.meta = await api.strategyMeta();
      } catch (e) {
        st.meta = { strategies: {}, periods: [], fillModels: {}, metricsModes: [] };
      }
      const strategies = st.meta.strategies || {};
      Object.keys(strategies).forEach((k) => stratSel.appendChild(h('option', { value: k, text: strategies[k].name })));
      if (!strategies[st.strategy] && Object.keys(strategies).length) {
        st.strategy = Object.keys(strategies)[0];
      }
      stratSel.value = st.strategy;
      const periods = st.meta.periods || [{ value: 'day', label: '日K' }];
      periods.forEach((p) => periodSel.appendChild(h('option', { value: p.value, text: p.label })));
      st.period = 'day';
      periodSel.value = st.period;
      Object.keys(st.meta.fillModels || {}).forEach((k) => {
        fillSel.appendChild(h('option', { value: k, text: st.meta.fillModels[k].name }));
      });
      fillSel.value = 'nextOpen';
      (st.meta.metricsModes || []).forEach((m) => modeSel.appendChild(h('option', { value: m.value, text: m.label })));
      modeSel.value = 'compound';
      renderParams();
      renderSpace();
      if (st.code) run();
    }

    renderParams();
    renderSpace();

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('策略回测', '服务端统一引擎 · 单标的全仓 · 可切换成交模型与累计口径', [
        nameLabel,
        codeInput,
        periodSel,
        h('button', {
          class: 'btn sm', text: '转为持续跟踪',
          title: '把这个策略放到服务端一直跑，观测目标日内的胜率与盈亏',
          on: {
            click: () => {
              const params = currentParams();
              ctx.openTracker(st.market, (codeInput.value || st.code || '').trim().toUpperCase(),
                st.name || nameLabel.textContent, { strategy: st.strategy, params, period: st.period });
            },
          },
        }),
        h('button', { class: 'btn primary sm', text: '运行回测', on: { click: run } }),
      ]),
      ui.section('策略与参数', '策略、参数与成交假设。信号计算在服务端执行，与策略跟踪同源', [], h('div', {}, [
        h('div', { class: 'field', style: { marginBottom: '12px' } }, [h('label', { text: '策略' }), stratSel, stratDesc]),
        paramHost,
        h('div', { class: 'field', style: { marginTop: '12px' } }, [
          h('label', { text: '成交模型' }), fillSel,
          h('label', { text: ' 累计口径' }), modeSel,
        ]),
      ])),
      ui.section('回测结果', '', [], statHost),
      h('div', { class: 'grid g-2' }, [
        ui.section('策略权益曲线', '初始资金等权全仓', [], eqHost),
        ui.section('买卖点标注', '', [], kHost),
      ]),
      ui.section('参数寻优', '网格搜索：勾选参与寻优的参数并设置区间（上限 64 组），按目标指标排名', [
        metricSel, comboCtx,
        h('button', { class: 'btn primary sm', text: '开始寻优', on: { click: search } }),
      ], h('div', {}, [spaceHost, searchHost])),
      ui.section('交易明细', '每笔同时记录手续费与滑点成本', [], tradeHost),
    ]));

    boot();
    return { refresh() {}, destroy() { if (eqChart) eqChart.destroy(); if (kChart) kChart.destroy(); } };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.backtest = { mount };
})();
