/* ==========================================================================
   视图 · 策略持续跟踪（Paper Trading）
   服务端常驻引擎按固定频率推进策略，这里负责创建任务、观测胜率与盈亏
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  const REFRESH_MS = 15000;

  function progressCell(st) {
    const days = st.daysObserved || 0;
    const target = st.targetDays || 90;
    const pctv = Math.min(100, (days / target) * 100);
    const done = days >= target;
    return h('div', { class: 'prog' }, [
      h('div', { class: 'prog-bar' + (done ? ' done' : '') }, [h('i', { style: { width: pctv.toFixed(1) + '%' } })]),
      h('div', { class: 'prog-text' }, [
        h('span', { text: '已观测 ' + days + ' / ' + target + ' 交易日' }),
        h('span', { text: done ? '达标' : '剩 ' + Math.max(0, target - days) + ' 天' }),
      ]),
    ]);
  }

  function statCard(k, v, cls) {
    return h('div', { class: 'bt-stat' }, [
      h('div', { class: 'k', text: k }),
      h('div', { class: 'v ' + (cls || ''), text: v }),
    ]);
  }

  function mount(root, ctx) {
    const st = {
      meta: null, overview: null, engine: null,
      selected: null, detail: null, auto: true,
      form: {
        market: ctx.state.market, code: '', name: '', strategy: 'maCross', period: 'day',
        lookback: '3m', targetDays: 90, initial: ctx.state.market === 'cn' ? 200000 : 100000,
        lot: ctx.state.market === 'cn' ? 100 : 1, fee: '0.0003', slippage: '0.001',
        stopLoss: '0', takeProfit: '0', note: '', params: {}, minCapital: null, price: null,
      },
    };
    let eqChart = null;
    let timer = null;

    /* 调整任务面板：持久节点，避免自动刷新打断正在编辑的内容 */
    const adjustHost = h('div');
    const adjustState = { forId: null, showLogic: false, reset: false, vals: {}, logic: {}, revisions: [] };
    const revHost = h('div');

    const engineChip = h('span', { class: 'engine-chip off' });
    const totalsHost = h('div', { class: 'metric-list' });
    const formHost = h('div');
    const listHost = h('div');
    const detailHost = h('div');
    const hintHost = h('span', { class: 'hint' });

    /* ----------------------------------------------------- 组合总览 */

    function renderTotals() {
      clear(totalsHost);
      const t = (st.overview && st.overview.totals) || {};
      const cells = [
        ['跟踪任务', (t.runs || 0) + ' 个（运行中 ' + (t.running || 0) + '）'],
        ['总投入本金', F.amt(t.initial || 0, 'cn')],
        ['当前总权益', F.amt(t.equity || 0, 'cn')],
        ['组合盈亏', F.amt(t.pnl || 0, 'cn'), F.dir(t.pnl)],
        ['组合收益率', F.pct(t.returnPct || 0), F.dir(t.returnPct)],
        ['累计交易', (t.trades || 0) + ' 笔'],
        ['整体胜率', F.num(t.winRate || 0, 1) + '%', (t.winRate || 0) >= 50 ? 'up' : ''],
        ['口径说明', '跨市场按本币简单加总（不做汇率换算）'],
      ];
      cells.forEach((c) => {
        totalsHost.appendChild(h('div', { class: 'metric' }, [
          h('div', { class: 'k', text: c[0] }),
          h('div', { class: 'v ' + (c[2] || ''), text: c[1] }),
        ]));
      });
    }

    function renderEngine() {
      const e = st.engine || {};
      clear(engineChip);
      engineChip.className = 'engine-chip' + (e.running ? '' : ' off');
      engineChip.appendChild(h('i', { class: 'dot' }));
      engineChip.appendChild(h('span', {
        text: e.running ? '引擎运行中' : '引擎未运行（启动 server.py 即自动开启）',
      }));
      engineChip.appendChild(h('span', {
        class: 'meta',
        text: '每 ' + (e.interval || 60) + ' 秒推进' +
          (e.lastTick ? ' · 上次 ' + F.clock(e.lastTick) : ' · 等待首次推进') +
          ' · 已完成 ' + (e.ticks || 0) + ' 轮',
      }));
    }

    /* ------------------------------------------------- 新建任务表单 */

    function renderForm() {
      clear(formHost);
      const f = st.form;
      const meta = st.meta || { strategies: {}, periods: [], windows: [], targets: [] };

      const marketSeg = ui.seg([{ value: 'cn', label: 'A股' }, { value: 'us', label: '美股' }], f.market, (v) => {
        f.market = v;
        f.lot = v === 'cn' ? 100 : 1;
        f.initial = v === 'cn' ? 200000 : 100000;
        f.minCapital = null;
        f.price = null;
        renderForm();
      });

      const codeInput = h('input', { class: 'inp', value: f.code, placeholder: '如 600519 / AAPL' });
      const nameBox = h('span', { class: 'dim3', text: f.name || '—' });

      async function probe() {
        const code = codeInput.value.trim().toUpperCase();
        f.code = code;
        if (!code) { f.minCapital = null; f.name = ''; f.price = null; renderHint(); return; }
        try {
          const [s, q] = await Promise.all([
            api.search(code).catch(() => ({ rows: [] })),
            api.quote(f.market, [code]).catch(() => ({ rows: [] })),
          ]);
          const hit = (s.rows || []).find((r) => r.code.toUpperCase() === code) || (s.rows || [])[0];
          if (hit) f.name = hit.name;
          const row = (q.rows || [])[0];
          f.price = row && row.price;
          f.minCapital = f.price ? f.price * Number(f.lot) * 1.01 : null;
        } catch (e) { /* 忽略 */ }
        nameBox.textContent = f.name || code;
        renderHint();
      }
      codeInput.addEventListener('blur', probe);
      codeInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') probe(); });

      const stratSel = h('select', { class: 'inp' });
      Object.keys(meta.strategies).forEach((k) => {
        stratSel.appendChild(h('option', { value: k, text: meta.strategies[k].name }));
      });
      stratSel.value = f.strategy;
      stratSel.addEventListener('change', () => { f.strategy = stratSel.value; renderForm(); });

      const periodSel = h('select', { class: 'inp' });
      (meta.periods || []).forEach((p) => periodSel.appendChild(h('option', { value: p.value, text: p.label })));
      periodSel.value = f.period;
      periodSel.addEventListener('change', () => { f.period = periodSel.value; });

      const lookbackSel = h('select', { class: 'inp' });
      (meta.windows || []).forEach((p) => lookbackSel.appendChild(h('option', { value: p.value, text: p.label })));
      lookbackSel.value = f.lookback;
      lookbackSel.addEventListener('change', () => { f.lookback = lookbackSel.value; });

      const targetSel = h('select', { class: 'inp' });
      (meta.targets || [90]).forEach((t) => targetSel.appendChild(h('option', { value: String(t), text: t + ' 个交易日' })));
      targetSel.value = String(f.targetDays);
      targetSel.addEventListener('change', () => { f.targetDays = Number(targetSel.value); });

      const numField = (key, label, step, title) => {
        const inp = h('input', { class: 'inp', value: String(f[key]), step: step || 'any', title: title || '' });
        inp.addEventListener('input', () => {
          f[key] = inp.value;
          if (key === 'lot') {
            f.minCapital = f.price ? f.price * Number(f.lot) * 1.01 : null;
            renderHint();
          }
        });
        return h('div', { class: 'field' }, [h('label', { text: label }), inp]);
      };

      const paramHost = h('div', { class: 'filter-grid' });
      const sp = meta.strategies[f.strategy] ? meta.strategies[f.strategy].params : [];
      sp.forEach((pd) => {
        const cur = f.params[pd.key] !== undefined ? f.params[pd.key] : pd.def;
        const inp = h('input', { class: 'inp', value: String(cur), min: pd.min, max: pd.max });
        inp.addEventListener('input', () => {
          const v = Number(inp.value);
          if (!isNaN(v) && v > 0) f.params[pd.key] = v;
        });
        paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: pd.label }), inp]));
      });

      const noteInput = h('input', { class: 'inp', value: f.note, placeholder: '如：验证 5/20 均线在茅台上的表现' });
      noteInput.addEventListener('input', () => { f.note = noteInput.value; });

      const fillSel = h('select', { class: 'inp' });
      Object.keys(meta.fillModels || { nextOpen: { name: '次根开盘价（默认）' } }).forEach((k) => {
        fillSel.appendChild(h('option', { value: k, text: meta.fillModels[k].name }));
      });
      fillSel.value = f.fillModel || 'nextOpen';
      fillSel.addEventListener('change', () => { f.fillModel = fillSel.value; });

      const modeSel = h('select', { class: 'inp' });
      (meta.metricsModes || [{ value: 'compound', label: '几何累乘（复利口径）' },
        { value: 'simple', label: '算术累加（单利口径）' }]).forEach((m) => {
        modeSel.appendChild(h('option', { value: m.value, text: m.label }));
      });
      modeSel.value = f.metricsMode || 'compound';
      modeSel.addEventListener('change', () => { f.metricsMode = modeSel.value; });

      formHost.appendChild(h('div', {}, [
        h('div', { class: 'run-form' }, [
          h('div', { class: 'field' }, [h('label', { text: '市场' }), marketSeg]),
          h('div', { class: 'field wide' }, [h('label', { text: '标的' }), codeInput, nameBox]),
          h('div', { class: 'field' }, [h('label', { text: '策略' }), stratSel]),
          h('div', { class: 'field' }, [h('label', { text: '周期' }), periodSel]),
          h('div', { class: 'field' }, [h('label', { text: '成交模型' }), fillSel]),
          h('div', { class: 'field' }, [h('label', { text: '累计口径' }), modeSel]),
          h('div', { class: 'field' }, [h('label', { text: '回溯窗口' }), lookbackSel]),
          h('div', { class: 'field' }, [h('label', { text: '观察目标' }), targetSel]),
          numField('initial', '初始资金'),
          numField('lot', '最小单位', '1', 'A股为 100 股/手，美股为 1 股'),
          numField('fee', '手续费率'),
          numField('slippage', '滑点'),
          numField('stopLoss', '止损 %', 'any', '0 表示不启用'),
          numField('takeProfit', '止盈 %', 'any', '0 表示不启用'),
          h('div', { class: 'field wide' }, [h('label', { text: '备注' }), noteInput]),
        ]),
        h('div', { class: 'section-head', style: { marginTop: '14px' } }, [
          h('h2', { text: '策略参数' }),
          h('span', { class: 'hint', text: meta.strategies[f.strategy] ? meta.strategies[f.strategy].desc : '' }),
        ]),
        paramHost,
        h('div', { style: { display: 'flex', gap: '12px', alignItems: 'center', marginTop: '14px', flexWrap: 'wrap' } }, [
          h('button', { class: 'btn primary', text: '创建跟踪任务', on: { click: create } }),
          hintHost,
        ]),
      ]));
    }

    function renderHint() {
      const f = st.form;
      const bits = [];
      if (f.price) bits.push('现价 ' + F.price(f.price, f.market));
      if (f.minCapital) bits.push('每笔最少约需 ' + F.amt(f.minCapital, f.market) + '（' + f.lot + ' 股）');
      if (f.minCapital && Number(f.initial) < f.minCapital) {
        bits.push('⚠ 当前初始资金不足，买入信号会被跳过，建议提高到 ' +
          F.amt(Math.ceil(f.minCapital / 10000) * 10000, f.market) + ' 以上');
      }
      bits.push('创建后立即回溯所选窗口的历史表现，并持续向前推进');
      clear(hintHost);
      hintHost.appendChild(h('span', { text: bits.join(' · ') }));
    }

    async function create() {
      const f = st.form;
      const code = (f.code || '').trim().toUpperCase();
      if (!code) { ctx.toast('请先填写标的代码', 'warn'); return; }
      const body = {
        market: f.market, code, name: f.name || code, strategy: f.strategy, period: f.period,
        params: f.params, lookback: f.lookback, targetDays: f.targetDays,
        initial: Number(f.initial) || 100000, lot: Number(f.lot) || 1,
        fee: Number(f.fee), slippage: Number(f.slippage),
        stopLoss: Number(f.stopLoss) || 0, takeProfit: Number(f.takeProfit) || 0,
        fillModel: f.fillModel || 'nextOpen', metricsMode: f.metricsMode || 'compound',
        participation: Number(f.participation) || 0.05,
        note: f.note,
      };
      ctx.toast('正在创建并回溯历史…', 'info');
      try {
        const res = await api.strategyCreate(body);
        st.selected = res.run && res.run.id;
        ctx.toast('跟踪任务已创建：' + (f.name || code), 'ok');
        f.code = '';
        f.name = '';
        await refreshAll();
        if (st.selected) await loadDetail();
      } catch (e) {
        ctx.toast('创建失败：' + e.message, 'err');
      }
    }

    /* ----------------------------------------------------- 任务列表 */

    function renderList() {
      clear(listHost);
      const rows = (st.overview && st.overview.rows) || [];
      if (!rows.length) {
        listHost.appendChild(ui.empty('还没有跟踪任务：在上方选择标的与策略创建，系统会回溯历史并持续跟踪'));
        return;
      }
      listHost.appendChild(ui.tbl({
        cols: [
          {
            key: 'status', label: '状态', noSort: true, width: '74px',
            render: (r) => h('span', {
              class: 'chip ' + (r.status === 'running' ? 'accent' : ''),
              text: r.status === 'running' ? '跟踪中' : '已暂停',
            }),
          },
          {
            key: 'name', label: '标的', noSort: true,
            render: (r) => h('span', {}, [
              h('span', { class: 'name', text: r.name || r.code }),
              h('span', { class: 'code', text: (r.market === 'us' ? 'US:' : '') + r.code }),
            ]),
          },
          {
            key: 'strategy', label: '策略', noSort: true,
            render: (r) => h('span', {}, [
              h('span', { text: r.strategyName }),
              h('span', { class: 'code', text: ' ' + Object.keys(r.params || {}).map((k) => k + '=' + r.params[k]).join(' ') +
                ' · ' + r.period }),
            ]),
          },
          { key: 'progress', label: '观察进度', noSort: true, width: '150px', render: (r) => progressCell(r.stats) },
          {
            key: 'winRate', label: '胜率', cls: 'n', value: (r) => r.stats.winRate,
            render: (r) => h('span', { class: 'num ' + (r.stats.winRate >= 50 ? 'up' : (r.stats.trades ? 'down' : 'flat')) },
              [F.num(r.stats.winRate, 1) + '%' , h('small', { class: 'dim3', text: ' ' + r.stats.wins + '/' + r.stats.trades })]),
          },
          {
            key: 'returnPct', label: '收益率', cls: 'n', value: (r) => r.stats.returnPct,
            render: (r) => pct(r.stats.returnPct),
          },
          {
            key: 'totalPnl', label: '盈亏', cls: 'n', value: (r) => r.stats.totalPnl,
            render: (r) => h('span', { class: 'num ' + F.dir(r.stats.totalPnl), text: F.amt(r.stats.totalPnl, r.market) }),
          },
          { key: 'trades', label: '交易', cls: 'n', value: (r) => r.stats.trades, render: (r) => h('span', { class: 'num', text: String(r.stats.trades) }) },
          {
            key: 'maxDrawdown', label: '最大回撤', cls: 'n', value: (r) => r.stats.maxDrawdown,
            render: (r) => h('span', { class: 'num down', text: '-' + F.num(r.stats.maxDrawdown, 2) + '%' }),
          },
          {
            key: 'position', label: '持仓', noSort: true,
            render: (r) => (r.position
              ? h('span', { class: 'chip up', text: '持有 ' + r.position.qty + ' 股 ' + F.pct(r.position.pnlPct) })
              : h('span', { class: 'dim3', text: '空仓' })),
          },
          {
            key: 'lastTick', label: '最近推进', noSort: true,
            render: (r) => h('span', { class: 'num dim', text: r.lastTick ? F.clock(r.lastTick) : '—' }),
          },
          {
            key: 'act', label: '操作', noSort: true, width: '230px',
            render: (r) => h('div', { style: { display: 'flex', gap: '5px', flexWrap: 'wrap' } }, [
              h('button', {
                class: 'btn ghost sm', text: '详情',
                on: { click: (e) => { e.stopPropagation(); st.selected = r.id; loadDetail(); } },
              }),
              h('button', {
                class: 'btn ghost sm', text: r.status === 'running' ? '暂停' : '继续',
                on: { click: (e) => { e.stopPropagation(); act(r.id, r.status === 'running' ? 'pause' : 'resume'); } },
              }),
              h('button', {
                class: 'btn ghost sm', text: '推进',
                on: { click: (e) => { e.stopPropagation(); act(r.id, 'tick'); } },
              }),
              h('button', {
                class: 'btn ghost sm', text: '重置',
                on: {
                  click: (e) => {
                    e.stopPropagation();
                    if (window.confirm('重置将清空该任务的交易与权益记录，并重新回溯历史，确认？')) act(r.id, 'reset');
                  },
                },
              }),
              h('button', {
                class: 'btn ghost sm', text: '删除',
                on: {
                  click: (e) => {
                    e.stopPropagation();
                    if (window.confirm('确认删除该跟踪任务？')) {
                      if (st.selected === r.id) st.selected = null;
                      act(r.id, 'delete');
                    }
                  },
                },
              }),
            ]),
          },
        ],
        rows,
        activeKey: st.selected,
        rowKey: (r) => r.id,
        onRow: (r) => { st.selected = r.id; loadDetail(); },
        maxHeight: '460px',
        emptyText: '暂无任务',
      }));
    }

    async function act(id, action) {
      try {
        await api.strategyAction(id, action);
        ctx.toast('已执行：' + ({ pause: '暂停', resume: '继续', tick: '立即推进', reset: '重置回溯', delete: '删除' }[action] || action), 'ok');
        await refreshAll();
        if (st.selected) await loadDetail();
      } catch (e) {
        ctx.toast('操作失败：' + e.message, 'err');
      }
    }

    /* ----------------------------------------------------- 调整任务 */

    function fieldLabel(key) {
      const labels = (st.meta && st.meta.editable && st.meta.editable.labels) || {};
      return labels[key] || key;
    }

    function fmtVal(v) {
      if (v === null || v === undefined) return '—';
      if (typeof v === 'object') {
        return Object.keys(v).map((k) => k + '=' + F.num(v[k], 4)).join(' ') || '—';
      }
      if (typeof v === 'number') return String(Math.round(v * 1e6) / 1e6);
      return String(v) || '—';
    }

    /** 计算待提交的变更（只提交真正改动的字段） */
    function currentPatch(run) {
      const logicKeys = ((st.meta && st.meta.editable && st.meta.editable.logic) || []);
      const v = adjustState.vals;
      const patch = {};
      if (String(v.name || '') !== (run.name || '')) patch.name = String(v.name || '');
      if (String(v.note || '') !== (run.note || '')) patch.note = String(v.note || '');
      if (Number(v.targetDays) !== Number(run.targetDays)) patch.targetDays = Number(v.targetDays);
      if (Math.abs(Number(v.fee) - Number(run.fee)) > 1e-9) patch.fee = Number(v.fee);
      if (Math.abs(Number(v.slippage) - Number(run.slippage)) > 1e-9) patch.slippage = Number(v.slippage);
      if (Math.abs(Number(v.stopLoss) - Number(run.stopLoss)) > 1e-9) patch.stopLoss = Number(v.stopLoss);
      if (Math.abs(Number(v.takeProfit) - Number(run.takeProfit)) > 1e-9) patch.takeProfit = Number(v.takeProfit);
      if (v.participation !== undefined && Math.abs(Number(v.participation) - Number(run.participation || 0.05)) > 1e-9) {
        patch.participation = Number(v.participation);
      }
      if (v.metricsMode && v.metricsMode !== (run.metricsMode || 'compound')) patch.metricsMode = v.metricsMode;
      if ((v.notify || '') !== (run.notify || '')) patch.notify = v.notify || '';
      if (adjustState.showLogic) {
        const lv = adjustState.logic;
        if (lv.strategy !== run.strategy) {
          patch.strategy = lv.strategy;
        } else if (JSON.stringify(lv.params) !== JSON.stringify(run.params)) {
          patch.params = lv.params;
        }
        if (lv.period !== run.period) patch.period = lv.period;
        if (lv.fillModel !== (run.fillModel || 'nextOpen')) patch.fillModel = lv.fillModel;
        if (Number(lv.fq) !== Number(run.fq)) patch.fq = Number(lv.fq);
        if (Math.abs(Number(lv.initial) - Number(run.initial)) > 1e-6) patch.initial = Number(lv.initial);
        if (Number(lv.lot) !== Number(run.lot)) patch.lot = Number(lv.lot);
        if (lv.startDate !== run.startDate) patch.startDate = lv.startDate;
      }
      return { patch, logicKeys };
    }

    const diffHost = h('div', { class: 'legend-inline', style: { marginTop: '10px' } });
    const saveBtn = h('button', { class: 'btn primary sm', text: '保存调整' });
    const logicToggle = h('button', { class: 'btn sm', text: '显示会改变统计口径的字段' });
    let logicBoxRef = null;
    logicToggle.addEventListener('click', () => {
      adjustState.showLogic = !adjustState.showLogic;
      if (logicBoxRef) logicBoxRef.style.display = adjustState.showLogic ? '' : 'none';
      logicToggle.textContent = adjustState.showLogic ? '收起高级字段' : '显示会改变统计口径的字段';
      logicToggle.classList.toggle('active', adjustState.showLogic);
      updateDiff();
    });

    function updateDiff() {
      const run = st.detail && st.detail.run;
      clear(diffHost);
      if (!run) return;
      const { patch, logicKeys } = currentPatch(run);
      const keys = Object.keys(patch);
      if (!keys.length) {
        diffHost.appendChild(h('span', { class: 'dim3', text: '尚未修改任何字段' }));
        saveBtn.disabled = true;
        return;
      }
      const logic = keys.filter((k) => logicKeys.indexOf(k) >= 0);
      diffHost.appendChild(h('span', { class: 'dim3', text: '待提交 ' + keys.length + ' 项：' }));
      keys.forEach((k) => {
        const from = run[k];
        diffHost.appendChild(h('span', { class: 'chip' + (logicKeys.indexOf(k) >= 0 ? ' warn' : ' accent') }, [
          h('span', { text: fieldLabel(k) + ' ' + fmtVal(from) + ' → ' + fmtVal(patch[k]) }),
        ]));
      });
      if (logic.length && !adjustState.reset) {
        saveBtn.disabled = true;
        diffHost.appendChild(h('span', { class: 'chip warn', text: '需勾选「重置并重新回溯」后才能提交' }));
      } else {
        saveBtn.disabled = false;
      }
    }

    async function saveAdjust() {
      const run = st.detail && st.detail.run;
      if (!run) return;
      const { patch, logicKeys } = currentPatch(run);
      const keys = Object.keys(patch);
      if (!keys.length) { ctx.toast('没有检测到变更', 'warn'); return; }
      const logic = keys.some((k) => logicKeys.indexOf(k) >= 0);
      if (logic && !adjustState.reset) {
        ctx.toast('策略、参数、周期、资金、观察期窗口会改变统计口径，请勾选「重置并重新回溯」', 'err');
        return;
      }
      if (logic && !window.confirm('这将清空该任务已有的交易与权益记录，并按新配置重新回溯历史，确认继续？')) return;
      saveBtn.disabled = true;
      saveBtn.textContent = logic ? '重置并回溯中…' : '保存中…';
      try {
        const res = await api.strategyUpdate(run.id, patch, logic);
        ctx.toast('已调整：' + (res.changed || []).map(fieldLabel).join('、') +
          (res.reset ? '（已重置并重新回溯）' : '（仅影响后续成交）'), 'ok');
        adjustState.reset = false;
        await refreshAll(true);
        if (st.detail && st.detail.run) {
          buildAdjust(st.detail.run, true);
          renderDetail();
        }
      } catch (e) {
        ctx.toast('调整失败：' + e.message, 'err');
      } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = '保存调整';
        updateDiff();
      }
    }
    saveBtn.addEventListener('click', saveAdjust);

    function buildAdjust(run, force) {
      if (!force && adjustState.forId === run.id && adjustHost.children.length) {
        updateDiff();
        return;
      }
      adjustState.forId = run.id;
      adjustState.showLogic = false;
      adjustState.reset = false;
      adjustState.vals = {
        name: run.name || run.code, note: run.note || '', targetDays: run.targetDays,
        fee: run.fee, slippage: run.slippage, stopLoss: run.stopLoss, takeProfit: run.takeProfit,
        participation: run.participation === undefined ? 0.05 : run.participation,
        metricsMode: run.metricsMode || 'compound',
        notify: run.notify || '',
      };
      adjustState.logic = {
        strategy: run.strategy, period: run.period, fq: run.fq === undefined ? 1 : run.fq,
        params: Object.assign({}, run.params || {}), initial: run.initial,
        lot: run.lot, startDate: run.startDate,
        fillModel: run.fillModel || 'nextOpen',
      };

      const meta = st.meta || { strategies: {}, periods: [], windows: [], targets: [90] };
      clear(adjustHost);
      const v = adjustState.vals;

      const txt = (key, label, ph, title) => {
        const inp = h('input', { class: 'inp', value: String(v[key] === undefined ? '' : v[key]), placeholder: ph || '', title: title || '' });
        inp.addEventListener('input', () => { v[key] = inp.value; updateDiff(); });
        return h('div', { class: 'field' }, [h('label', { text: label }), inp]);
      };
      const num = (key, label, step, title) => {
        const inp = h('input', { class: 'inp', value: String(v[key]), step: step || 'any', title: title || '' });
        inp.addEventListener('input', () => { v[key] = inp.value; updateDiff(); });
        return h('div', { class: 'field' }, [h('label', { text: label }), inp]);
      };

      const modeSelField = () => {
        const sel = h('select', { class: 'inp' });
        ((meta.metricsModes) || [{ value: 'compound', label: '几何累乘（复利口径）' },
          { value: 'simple', label: '算术累加（单利口径）' }]).forEach((m) => {
          sel.appendChild(h('option', { value: m.value, text: m.label }));
        });
        sel.value = v.metricsMode || 'compound';
        sel.addEventListener('change', () => { v.metricsMode = sel.value; updateDiff(); });
        return h('div', { class: 'field' }, [h('label', { text: '累计口径' }), sel]);
      };

      const targetSel = h('select', { class: 'inp' });
      const targets = (meta.targets || [90]).slice();
      if (targets.indexOf(Number(run.targetDays)) < 0) targets.push(Number(run.targetDays));
      targets.sort((a, b) => a - b).forEach((t) => targetSel.appendChild(h('option', { value: String(t), text: t + ' 个交易日' })));
      targetSel.value = String(v.targetDays);
      targetSel.addEventListener('change', () => { v.targetDays = Number(targetSel.value); updateDiff(); });

      const safeGrid = h('div', { class: 'run-form' }, [
        txt('name', '任务名称', run.code),
        txt('note', '备注', '如：验证 5/20 均线在茅台上的表现'),
        h('div', { class: 'field' }, [h('label', { text: '观察目标' }), targetSel]),
        num('fee', '手续费率', 'any', '小数，0.0003 = 万三'),
        num('slippage', '滑点', 'any', '小数，0.001 = 千一'),
        num('stopLoss', '止损 %', 'any', '0 表示不启用'),
        num('takeProfit', '止盈 %', 'any', '0 表示不启用'),
        num('participation', '参与度上限', 'any', '深度加权成交模型使用，0.005 ~ 0.5'),
        modeSelField(),
        txt('notify', '通知 Webhook', '留空则用全局设置', '事件发生时 POST JSON 到此地址'),
      ]);

      /* 高级：会改变统计口径的字段 */
      const logicGrid = h('div', { class: 'run-form' });
      const lv = adjustState.logic;

      const stratSel = h('select', { class: 'inp' });
      Object.keys(meta.strategies || {}).forEach((k) => {
        stratSel.appendChild(h('option', { value: k, text: meta.strategies[k].name }));
      });
      stratSel.value = lv.strategy;
      const paramHost = h('div', { class: 'filter-grid', style: { gridColumn: '1 / -1', marginTop: '4px' } });
      const renderParams = () => {
        clear(paramHost);
        const sp = (meta.strategies[lv.strategy] || {}).params || [];
        sp.forEach((pd) => {
          const cur = lv.params[pd.key] !== undefined ? lv.params[pd.key] : pd.def;
          const inp = h('input', { class: 'inp', value: String(cur), min: pd.min, max: pd.max });
          inp.addEventListener('input', () => {
            const n = Number(inp.value);
            if (!isNaN(n)) { lv.params[pd.key] = n; updateDiff(); }
          });
          paramHost.appendChild(h('div', { class: 'field' }, [h('label', { text: pd.label }), inp]));
        });
      };
      stratSel.addEventListener('change', () => {
        lv.strategy = stratSel.value;
        const sp = (meta.strategies[lv.strategy] || {}).params || [];
        lv.params = {};
        sp.forEach((pd) => { lv.params[pd.key] = pd.def; });
        renderParams();
        updateDiff();
      });
      renderParams();

      const periodSel = h('select', { class: 'inp' });
      (meta.periods || []).forEach((p) => periodSel.appendChild(h('option', { value: p.value, text: p.label })));
      periodSel.value = lv.period;
      periodSel.addEventListener('change', () => { lv.period = periodSel.value; updateDiff(); });

      const fqSel = h('select', { class: 'inp' }, [
        h('option', { value: '1', text: '前复权' }), h('option', { value: '0', text: '不复权' }),
        h('option', { value: '2', text: '后复权' }),
      ]);
      fqSel.value = String(lv.fq);
      fqSel.addEventListener('change', () => { lv.fq = Number(fqSel.value); updateDiff(); });

      const initInp = h('input', { class: 'inp', value: String(lv.initial) });
      initInp.addEventListener('input', () => { lv.initial = initInp.value; updateDiff(); });
      const lotInp = h('input', { class: 'inp', value: String(lv.lot), title: 'A股为 100 股/手，美股为 1 股' });
      lotInp.addEventListener('input', () => { lv.lot = lotInp.value; updateDiff(); });

      const winSel = h('select', { class: 'inp' });
      (meta.windows || []).forEach((p) => winSel.appendChild(h('option', { value: p.value, text: p.label })));
      winSel.appendChild(h('option', { value: 'custom', text: '自定义日期' }));
      winSel.value = '3m';
      const dateInp = h('input', { class: 'inp', value: run.startDate, title: '观察期起点（YYYY-MM-DD）' });
      dateInp.addEventListener('change', () => { lv.startDate = dateInp.value; updateDiff(); });
      winSel.addEventListener('change', () => {
        if (winSel.value === 'custom') { dateInp.style.display = ''; return; }
        const days = { '1m': 30, '3m': 92, '6m': 183, '1y': 365 }[winSel.value] || 92;
        const d = new Date(Date.now() - days * 86400000);
        const p = (n) => String(n).padStart(2, '0');
        lv.startDate = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
        dateInp.value = lv.startDate;
        dateInp.style.display = '';
        updateDiff();
      });
      dateInp.style.display = '';   // 默认展示日期，便于直接微调

      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '策略' }), stratSel]));
      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '周期' }), periodSel]));
      const fillSel = h('select', { class: 'inp' });
      Object.keys((meta.fillModels) || { nextOpen: { name: '次根开盘价（默认）' } }).forEach((k) => {
        fillSel.appendChild(h('option', { value: k, text: meta.fillModels[k].name }));
      });
      fillSel.value = lv.fillModel || 'nextOpen';
      fillSel.addEventListener('change', () => { lv.fillModel = fillSel.value; updateDiff(); });
      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '成交模型' }), fillSel]));
      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '复权方式' }), fqSel]));
      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '初始资金' }), initInp]));
      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '最小交易单位' }), lotInp]));
      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '回溯窗口' }), winSel]));
      logicGrid.appendChild(h('div', { class: 'field' }, [h('label', { text: '观察期起点' }), dateInp]));
      logicGrid.appendChild(paramHost);

      const resetChk = h('input', { type: 'checkbox' });
      resetChk.addEventListener('change', () => { adjustState.reset = resetChk.checked; updateDiff(); });
      const resetRow = h('label', { class: 'legend-inline', style: { marginTop: '10px', cursor: 'pointer' } }, [
        resetChk,
        h('span', { class: 'chip warn', text: '重置并重新回溯' }),
        h('span', { text: '清空已有交易与权益记录（观察期内统计口径保持一致）' }),
      ]);

      const logicBox = h('div', { class: 'adjust-box' }, [
        h('div', { class: 'legend-inline', style: { marginBottom: '10px' } }, [
          h('span', { class: 'chip warn', text: '会改变统计口径' }),
          '策略 / 参数 / 周期 / 复权 / 初始资金 / 最小单位 / 观察期窗口，改动后需重新回溯',
        ]),
        logicGrid,
        resetRow,
      ]);
      logicBox.style.display = 'none';
      logicBoxRef = logicBox;
      logicToggle.textContent = '显示会改变统计口径的字段';
      logicToggle.classList.remove('active');

      adjustHost.appendChild(h('div', { class: 'adjust-box' }, [
        h('div', { class: 'legend-inline', style: { marginBottom: '10px' } }, [
          h('span', { class: 'chip accent', text: '即时生效' }),
          '名称、备注、观察目标、手续费、滑点、止损止盈：不影响已产生的统计，只作用于后续成交',
        ]),
        safeGrid,
      ]));
      adjustHost.appendChild(logicBox);
      adjustHost.appendChild(h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginTop: '12px' } }, [
        saveBtn,
        logicToggle,
        h('button', {
          class: 'btn sm ghost', text: '放弃修改',
          on: {
            click: () => {
              if (!st.detail || !st.detail.run) return;
              buildAdjust(st.detail.run, true);
              ctx.toast('已放弃未保存的修改', 'info');
            },
          },
        }),
      ]));
      adjustHost.appendChild(diffHost);
      updateDiff();
    }

    function renderRevisions(run) {
      clear(revHost);
      const revs = (run.revisions || []);
      if (!revs.length) {
        revHost.appendChild(h('div', { class: 'legend-inline' }, ['该任务创建后尚未调整过配置']));
        return;
      }
      revs.forEach((rev) => {
        const rows = Object.keys(rev.fields || {}).map((k) => h('div', { class: 'legend-inline monospaced' }, [
          h('span', { class: 'chip', text: fieldLabel(k) }),
          h('span', { class: 'dim3', text: fmtVal(rev.fields[k].from) + ' → ' }),
          h('span', { text: fmtVal(rev.fields[k].to) }),
        ]));
        revHost.appendChild(h('div', { class: 'news-item' }, [
          h('div', { class: 'time', text: F.clock(rev.ts) }),
          h('div', { class: 'body' }, [
            h('div', { class: 'txt' }, [
              h('strong', { text: '调整 ' + Object.keys(rev.fields || {}).length + ' 项' }),
              rev.reset ? '　' : '　仅影响后续成交　',
              rev.reset ? h('span', { class: 'chip warn', text: '已重置并重新回溯' }) : null,
            ]),
            h('div', { style: { marginTop: '6px', display: 'grid', gap: '3px' } }, rows),
          ]),
        ]));
      });
    }

    /* ----------------------------------------------------- 任务详情 */

    function renderDetail() {
      clear(detailHost);
      if (eqChart) { eqChart.destroy(); eqChart = null; }
      const d = st.detail;
      if (!d) {
        detailHost.appendChild(ui.empty('点击任务行的「详情」查看权益曲线、月度收益与逐笔交易'));
        return;
      }
      const run = d.run;
      const s = d.stats;

      const head = h('div', { class: 'run-detail-head' }, [
        h('h3', { text: (run.name || run.code) + ' · ' + run.strategyName }),
        h('span', { class: 'mono-sm', text: (run.market === 'us' ? 'US:' : '') + run.code }),
        h('span', { class: 'chip', text: '周期 ' + run.period }),
        h('span', { class: 'chip', text: '初始 ' + F.amt(run.initial, run.market) }),
        h('span', { class: 'chip', text: Object.keys(run.params || {}).map((k) => k + '=' + run.params[k]).join(' · ') }),
        h('span', { class: 'chip' + (run.status === 'running' ? ' accent' : ''), text: run.status === 'running' ? '跟踪中' : '已暂停' }),
        h('span', { class: 'mono-sm', text: '观察期 ' + (s.startedFrom || run.startDate) + ' → 至今（' + s.daysObserved + ' 个交易日）' }),
      ]);

      const stats = h('div', { class: 'bt-stats' }, [
        statCard('累计收益率', F.pct(s.returnPct), F.dir(s.returnPct)),
        statCard('年化收益', F.pct(s.annualizedPct), F.dir(s.annualizedPct)),
        statCard('盈亏金额', F.amt(s.totalPnl, run.market), F.dir(s.totalPnl)),
        statCard('已实现 / 浮动', F.amt(s.realized, run.market) + ' / ' + F.amt(s.unrealized, run.market), F.dir(s.unrealized)),
        statCard('胜率', F.num(s.winRate, 1) + '%', s.winRate >= 50 ? 'up' : (s.trades ? 'down' : '')),
        statCard('交易次数', s.trades + ' 笔（' + s.wins + ' 胜 / ' + s.losses + ' 负）'),
        statCard('盈亏比', s.profitFactor >= 99 ? '∞（无亏损）' : F.num(s.profitFactor, 2)),
        statCard('最大回撤', '-' + F.num(s.maxDrawdown, 2) + '%', 'down'),
        statCard('夏普比率', F.num(s.sharpe, 2), s.sharpe > 1 ? 'up' : ''),
        statCard('索提诺', F.num(s.sortino, 2), s.sortino > 1 ? 'up' : ''),
        statCard('卡玛比率', F.num(s.calmar, 2), s.calmar > 1 ? 'up' : ''),
        statCard('年化波动', F.num(s.annualizedVol, 2) + '%'),
        statCard('Alpha / Beta', F.num(s.alpha, 3) + ' / ' + F.num(s.beta, 2)),
        statCard('单日 VaR(95%)', F.num(s.var95, 2) + '%', 'down'),
        statCard('期望值 / 笔', F.amt(s.expectancy, run.market), F.dir(s.expectancy)),
        statCard('持仓占比', F.num(s.exposure, 1) + '%'),
        statCard('交易成本合计', F.amt(s.costTotal, run.market), 'down'),
        statCard('其中手续费 / 滑点', F.num(s.feeTotal, 0) + ' / ' + F.num(s.slippageTotal, 0)),
        statCard('平均持仓', F.num(s.avgHoldBars, 1) + ' 根K线'),
        statCard('基准（买入持有）', s.benchmarkPct === null ? '—' : F.pct(s.benchmarkPct), F.dir(s.benchmarkPct)),
        statCard('超额收益', s.excessPct === null ? '—' : F.pct(s.excessPct), F.dir(s.excessPct)),
        statCard('跳过买入信号', (s.skippedBuys || 0) + ' 次', s.skippedBuys ? 'down' : ''),
      ]);

      const posCard = h('div', {});
      if (d.position) {
        const p = d.position;
        posCard.appendChild(h('div', { class: 'pos-card' }, [
          ['持仓数量', p.qty + ' 股', ''],
          ['建仓价', F.price(p.entryPrice, run.market), ''],
          ['建仓日', p.entryDate || '—', ''],
          ['建仓阶段', p.entryPhase === 'live' ? '实时段（引擎实盘记录）' : '回溯段（历史模拟）', ''],
          ['最新价', F.price(p.lastPrice, run.market), F.dir(p.pnl)],
          ['持仓市值', F.amt(p.marketValue, run.market), ''],
          ['浮动盈亏', F.amt(p.pnl, run.market), F.dir(p.pnl)],
          ['浮动收益率', F.pct(p.pnlPct), F.dir(p.pnlPct)],
          ['已持有', p.holdBars > 0 ? p.holdBars + ' 根K线' : '当日新建仓', ''],
        ].map(([k, v, cls]) => h('div', { class: 'cell' }, [
          h('div', { class: 'k', text: k }),
          h('div', { class: 'v ' + cls, text: v }),
        ]))));
        if (p.entryPhase === 'live') {
          posCard.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
            h('span', { class: 'phase-tag live', text: '实时' }),
            '该持仓由引擎在观察期内按真实行情成交（非历史回溯），是策略前向验证的有效样本',
          ]));
        }
      } else {
        posCard.appendChild(h('div', { class: 'legend-inline' }, ['当前空仓' + (run.pending ? '，已有待执行信号：' + (run.pending === 'buy' ? '买入' : '卖出') + '（下一根K线开盘成交）' : '')]));
      }

      const eqHost = h('div', { style: { height: '240px' } });
      const monthlyHost = h('div');
      const tradesHost = h('div');
      const signalsHost = h('div', { class: 'news-list' });

      detailHost.appendChild(h('div', {}, [
        head,
        stats,
        h('div', { style: { height: '16px' } }),
        ui.section('权益曲线', '策略权益 vs 买入持有基准（初始资金等比）', [], eqHost),
        h('div', { class: 'grid g-2' }, [
          ui.section('当前持仓', '', [], posCard),
          ui.section('观察期进度', '', [], h('div', {}, [
            progressCell(s),
            h('div', { class: 'legend-inline', style: { marginTop: '10px' } }, [
              '目标 ' + s.targetDays + ' 个交易日 · 已观测 ' + s.daysObserved + ' 个交易日（' +
              F.num(s.progressPct, 1) + '%）',
              '最近K线 ' + (s.lastBarTime || '—'),
              '引擎推进 ' + (s.tickCount || 0) + ' 次',
            ]),
            h('div', { class: 'legend-inline', style: { marginTop: '6px', color: 'var(--text-3)' } }, [
              '回溯记录（backfill）为历史模拟，实时记录（live）为引擎在盘中/收盘后写入',
            ]),
          ])),
        ]),
        ui.section('调整任务', '手续费 / 滑点 / 止损止盈 / 观察目标即时生效；策略类字段需重置并重新回溯', [], adjustHost),
        ui.section('月度盈亏', '按自然月拆解：月末权益变动 + 已实现盈亏', [], monthlyHost),
        ui.section('逐笔交易', '信号次日开盘成交，含手续费与滑点', [], tradesHost),
        ui.section('最近信号', '引擎识别到的买卖信号（含因资金不足被跳过的信号）', [], signalsHost),
        ui.section('变更记录', '该任务的配置调整历史（最近 30 次）', [], revHost),
      ]));
      buildAdjust(run);
      renderRevisions(run);

      /* 权益曲线 */
      const eq = d.equity || [];
      if (eq.length > 1) {
        const base = run.initial;
        const firstClose = eq[0].close;
        const series = [
          {
            name: '策略权益', data: eq.map((p) => ({ t: p.t, v: p.v })), color: 'var(--accent)',
            fill: 'rgba(77,141,255,0.12)', width: 1.6, fmt: (v) => F.amt(v, run.market),
          },
        ];
        if (firstClose) {
          series.push({
            name: '买入持有', data: eq.map((p) => ({ t: p.t, v: base * (p.close / firstClose) })),
            color: '#8b95a5', width: 1.2, fmt: (v) => F.amt(v, run.market),
          });
        }
        eqChart = window.AD.chart.line(eqHost, {
          height: 240, fmt: (v) => F.amt(v, run.market), series,
        });
      } else {
        eqHost.appendChild(ui.empty('权益数据不足'));
      }

      /* 月度 */
      clear(monthlyHost);
      if (!d.monthly || !d.monthly.length) monthlyHost.appendChild(ui.empty('暂无月度数据'));
      else {
        monthlyHost.appendChild(ui.tbl({
          cols: [
            { key: 'month', label: '月份', noSort: true },
            { key: 'days', label: '交易日', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num', text: String(m.days) }) },
            { key: 'equityEnd', label: '月末权益', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num', text: F.amt(m.equityEnd, run.market) }) },
            { key: 'monthlyReturnPct', label: '当月收益', cls: 'n', noSort: true, render: (m) => pct(m.monthlyReturnPct) },
            { key: 'realized', label: '已实现盈亏', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num ' + F.dir(m.realized), text: F.amt(m.realized, run.market) }) },
            { key: 'trades', label: '交易', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num', text: String(m.trades) }) },
            { key: 'winRate', label: '当月胜率', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num ' + (m.winRate >= 50 ? 'up' : (m.trades ? 'down' : 'flat')), text: m.trades ? F.num(m.winRate, 0) + '%' : '—' }) },
          ],
          rows: d.monthly, compact: true,
        }));
      }

      /* 交易明细 */
      clear(tradesHost);
      if (!d.trades || !d.trades.length) tradesHost.appendChild(ui.empty('观察期内尚未产生完整交易（可能一直持有或尚未触发卖出信号）'));
      else {
        tradesHost.appendChild(ui.tbl({
          cols: [
            { key: 'inDate', label: '买入日', noSort: true, render: (t) => h('span', { class: 'num', text: t.inDate }) },
            { key: 'inPrice', label: '买入价', cls: 'n', noSort: true, render: (t) => h('span', { class: 'num', text: F.price(t.inPrice, run.market) }) },
            { key: 'outDate', label: '卖出日', noSort: true, render: (t) => h('span', { class: 'num', text: t.outDate }) },
            { key: 'outPrice', label: '卖出价', cls: 'n', noSort: true, render: (t) => h('span', { class: 'num', text: F.price(t.outPrice, run.market) }) },
            { key: 'qty', label: '数量', cls: 'n', noSort: true, render: (t) => h('span', { class: 'num', text: String(t.qty) }) },
            { key: 'pnlPct', label: '收益率', cls: 'n', value: (t) => t.pnlPct, render: (t) => pct(t.pnlPct) },
            { key: 'pnl', label: '盈亏', cls: 'n', value: (t) => t.pnl, render: (t) => h('span', { class: 'num ' + F.dir(t.pnl), text: F.amt(t.pnl, run.market) }) },
            { key: 'bars', label: '持仓', cls: 'n', value: (t) => t.bars, render: (t) => h('span', { class: 'num', text: t.bars + ' 根' }) },
            {
              key: 'cost', label: '成本', cls: 'n', noSort: true,
              render: (t) => {
                const cost = (t.fee || 0) + (t.slippage || 0);
                const est = t.costEstimated ? h('span', { class: 'dim3', text: t.costEstimated ? '（估算）' : '' }) : null;
                return h('span', { class: 'num', title: '手续费 ' + F.num(t.fee || 0, 2) + ' + 滑点 ' + F.num(t.slippage || 0, 2) }, [
                  h('span', { text: F.num(cost, 2) }), est,
                ]);
              },
            },
            {
              key: 'slipBps', label: '实现滑点', cls: 'n', noSort: true,
              render: (t) => {
                if (!t.signalPrice || !t.fillPrice) return h('span', { class: 'num dim3', text: '—' });
                const bps = (t.fillPrice / t.signalPrice - 1) * 10000;
                return h('span', { class: 'num', text: F.num(bps, 1) + ' bp' });
              },
            },
            { key: 'reason', label: '平仓原因', noSort: true },
            {
              key: 'phase', label: '阶段', noSort: true,
              render: (t) => h('span', { class: 'phase-tag' + (t.phase === 'live' ? ' live' : ''), text: t.phase === 'live' ? '实时' : '回溯' }),
            },
          ],
          rows: d.trades.slice(0, 100), maxHeight: '340px', compact: true,
        }));
      }

      /* 信号 */
      clear(signalsHost);
      const sigs = d.signals || [];
      if (!sigs.length) signalsHost.appendChild(ui.empty('暂无信号记录'));
      sigs.slice(0, 20).forEach((sg) => {
        signalsHost.appendChild(h('div', { class: 'news-item' }, [
          h('div', { class: 'time', text: F.date(sg.t) }),
          h('div', { class: 'body' }, [
            h('div', { class: 'txt' }, [
              h('strong', { class: sg.side === 'buy' ? 'up' : 'down', text: sg.side === 'buy' ? '买入信号' : '卖出信号' }),
              '　价格 ' + F.price(sg.price, run.market),
              sg.note ? '　' + sg.note : '',
              sg.hadPosition === false && sg.side === 'sell' ? '　（无持仓，忽略）' : '',
            ]),
            h('div', { style: { marginTop: '4px', display: 'flex', gap: '6px', alignItems: 'center' } }, [
              h('span', { class: 'phase-tag' + (sg.phase === 'live' ? ' live' : ''), text: sg.phase === 'live' ? '实时' : '回溯' }),
              sg.skipped ? h('span', { class: 'chip warn', text: '未成交' }) : null,
            ]),
          ]),
        ]));
      });
    }

    /* --------------------------------------------------------- 刷新 */

    async function loadDetail() {
      if (!st.selected) { renderDetail(); return; }
      try {
        st.detail = await api.strategyRun(st.selected);
        renderDetail();
      } catch (e) {
        clear(detailHost);
        detailHost.appendChild(ui.empty('详情获取失败：' + e.message));
      }
    }

    async function refreshAll(silent) {
      try {
        const [ov, meta] = await Promise.all([api.strategyOverview(), st.meta ? Promise.resolve(st.meta) : api.strategyMeta()]);
        st.overview = ov;
        st.meta = meta;
        st.engine = ov.engine || meta.engine;
        renderTotals();
        renderEngine();
        renderList();
        if (silent) await loadDetail();
      } catch (e) {
        if (!silent) ctx.toast('策略任务获取失败：' + e.message, 'err');
      }
    }

    const autoSeg = ui.seg([{ value: 'on', label: '自动刷新' }, { value: 'off', label: '手动' }], 'on', (v) => {
      st.auto = v === 'on';
      ctx.toast(st.auto ? '已开启自动刷新（15 秒）' : '已切换为手动刷新', 'info');
    });

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('策略持续跟踪', '把策略放到服务端「一直跑」：创建任务即回溯观察期历史表现，之后引擎按固定频率向前推进，持续累积胜率与盈亏记录', [
        engineChip,
        autoSeg,
        h('button', {
          class: 'btn sm', text: '推进全部',
          on: {
            click: async () => {
              const rows = (st.overview && st.overview.rows) || [];
              if (!rows.length) return;
              ctx.toast('正在推进 ' + rows.length + ' 个任务…', 'info');
              for (const r of rows) {
                try { await api.strategyAction(r.id, 'tick'); } catch (e) { /* 单个失败忽略 */ }
              }
              await refreshAll(true);
              ctx.toast('推进完成', 'ok');
            },
          },
        }),
        h('button', { class: 'btn sm', text: '刷新', on: { click: () => refreshAll().then(() => loadDetail()) } }),
      ]),
      ui.section('组合总览', '所有跟踪任务的合计表现', [], totalsHost),
      ui.section('新建跟踪任务', '观察窗口默认取近 3 个月，创建时立即回溯，随后实时推进', [], formHost),
      ui.section('跟踪任务', '点击行查看详情；「推进」可手动触发一次引擎计算', [], listHost),
      ui.section('任务详情', '', [], detailHost),
    ]));

    (async () => {
      st.meta = await api.strategyMeta().catch(() => null);
      if (!st.meta) st.meta = { strategies: {}, periods: [], windows: [], targets: [90] };
      if (ctx.state.symbol && ctx.state.symbol.code) {
        st.form.code = ctx.state.symbol.code;
        st.form.name = ctx.state.symbol.name || '';
        st.form.market = ctx.state.symbol.market || st.form.market;
        st.form.lot = st.form.market === 'cn' ? 100 : 1;
      }
      // 从回测页跳转过来时，带入策略与参数
      const pre = ctx.state.trackerPrefill;
      if (pre) {
        if (pre.strategy && st.meta.strategies[pre.strategy]) st.form.strategy = pre.strategy;
        if (pre.params) st.form.params = Object.assign({}, pre.params);
        if (pre.period) st.form.period = pre.period;
        if (pre.name) st.form.name = pre.name;
        ctx.state.trackerPrefill = null;
      }
      renderForm();
      await refreshAll();
      const rows = (st.overview && st.overview.rows) || [];
      if (!st.selected && rows.length) st.selected = rows[0].id;
      await loadDetail();
    })();

    timer = setInterval(() => {
      if (!st.auto) return;
      if (!root.isConnected) return;
      refreshAll(true);
    }, REFRESH_MS);

    return {
      refresh: () => refreshAll().then(() => loadDetail()),
      destroy() {
        if (timer) clearInterval(timer);
        if (eqChart) eqChart.destroy();
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.tracker = { mount };
})();
