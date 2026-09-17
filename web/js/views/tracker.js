/* ==========================================================================
   视图 · 策略持续跟踪（Paper Trading）
   服务端常驻引擎按固定频率推进策略，这里负责创建任务、观测胜率与盈亏
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct, paint } = window.AD.dom;
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

    /* ------------------------------------------------ 无感刷新（增量更新）

       15 秒自动刷新（refreshAll → renderTotals / renderEngine / renderList /
       loadDetail → renderDetail）一律不做 clear + 重建：
         · 表格：实例缓存在 mount 作用域内（放模块级会跨页面串数据），
           首次挂到槽位上，之后只调 ref.update(rows)；列定义随任务变化时才 setCols；
         · 其余区块：paint(槽位, [子节点…]) 原位改写；空态也只改自己那一块。
       「表格 / 空态」是二选一：两个常驻槽位 + display 切换，表格实例从不卸载
       （卸载会丢滚动位置与 hover）。传给 paint 的子节点必须是新构造的（h() 产物），
       已挂载的节点交给 paint 会被摘下来再插回去 —— 那就等于重建了，表格只走 update。 */

    /* 一对槽位：tbl 只挂一次表格实例；empty 只由 paint 改写空态 */
    function slotPair(host) {
      const tbl = h('div');
      const empty = h('div');
      host.appendChild(tbl);
      host.appendChild(empty);
      return { tbl, empty };
    }

    /* 显示 / 隐藏（只改 display，节点不卸载） */
    function vis(el, on) { el.style.display = on ? '' : 'none'; }

    let listTbl = null;        /* 跟踪任务表实例 */
    let listCfg = null;        /* 它的配置对象：activeKey 决定高亮行，ui.tbl 每次渲染都会读 */
    let monthlyTbl = null;     /* 详情 · 月度盈亏表实例 */
    let tradesTbl = null;      /* 详情 · 逐笔交易表实例 */
    let tblRunId = null;       /* 详情两张表当前对应的任务 id（换任务时才 setCols） */
    let eqChartRunId = null;   /* 权益曲线当前对应的任务 id（同一任务只 setData，不重建画布） */

    const listSlot = slotPair(listHost);

    /* 任务详情骨架：只建一次（各区块的容器常驻），刷新时只往容器里 paint / update */
    const dtHead = h('div', { class: 'run-detail-head' });
    const dtStats = h('div', { class: 'bt-stats' });
    const dtPos = h('div');
    const dtProgress = h('div');
    const eqHost = h('div', { style: { height: '240px' } });
    const monthlyTblSlot = h('div');
    const monthlyEmptySlot = h('div');
    const tradesTblSlot = h('div');
    const tradesEmptySlot = h('div');
    const signalsHost = h('div', { class: 'news-list' });
    const detailEmpty = h('div');
    const detailSkeleton = h('div', {}, [
      dtHead,
      dtStats,
      h('div', { style: { height: '16px' } }),
      ui.section('权益曲线', '策略权益 vs 买入持有基准（初始资金等比）', [], eqHost),
      h('div', { class: 'grid g-2' }, [
        ui.section('当前持仓', '', [], dtPos),
        ui.section('观察期进度', '', [], dtProgress),
      ]),
      ui.section('调整任务', '手续费 / 滑点 / 止损止盈 / 观察目标即时生效；策略类字段需重置并重新回溯', [], adjustHost),
      ui.section('月度盈亏', '按自然月拆解：月末权益变动 + 已实现盈亏', [], h('div', {}, [monthlyTblSlot, monthlyEmptySlot])),
      ui.section('逐笔交易', '信号次日开盘成交，含手续费与滑点', [], h('div', {}, [tradesTblSlot, tradesEmptySlot])),
      ui.section('最近信号', '引擎识别到的买卖信号（含因资金不足被跳过的信号）', [], signalsHost),
      ui.section('变更记录', '该任务的配置调整历史（最近 30 次）', [], revHost),
    ]);
    vis(detailSkeleton, false);          /* 有详情时才展开（避免空骨架先闪一下） */
    detailHost.appendChild(detailEmpty);
    detailHost.appendChild(detailSkeleton);

    /* ----------------------------------------------------- 组合总览 */

    function renderTotals() {
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
      /* 卡片数量与顺序固定 → paint 只改文本，不重建节点 */
      paint(totalsHost, cells.map((c) => h('div', { class: 'metric' }, [
        h('div', { class: 'k', text: c[0] }),
        h('div', { class: 'v ' + (c[2] || ''), text: c[1] }),
      ])));
    }

    function renderEngine() {
      const e = st.engine || {};
      engineChip.className = 'engine-chip' + (e.running ? '' : ' off');
      paint(engineChip, [
        h('i', { class: 'dot' }),
        h('span', {
          text: e.running ? '引擎运行中' : '引擎未运行（启动 server.py 即自动开启）',
        }),
        h('span', {
          class: 'meta',
          text: '每 ' + (e.interval || 60) + ' 秒推进' +
            (e.lastTick ? ' · 上次 ' + F.clock(e.lastTick) : ' · 等待首次推进') +
            ' · 已完成 ' + (e.ticks || 0) + ' 轮',
        }),
      ]);
    }

    /* ------------------------------------------------- 新建任务表单 */

    function renderForm() {
      clear(formHost);
      const f = st.form;
      const meta = st.meta || { strategies: {}, periods: [], windows: [], targets: [] };
      /* 记录所有输入框引用，提交时以 DOM 为唯一真源（见 syncFromDom） */
      st.fields = {};

      const marketSeg = ui.seg([{ value: 'cn', label: 'A股' }, { value: 'us', label: '美股' }], f.market, (v) => {
        f.market = v;
        f.lot = v === 'cn' ? 100 : 1;
        f.initial = v === 'cn' ? 200000 : 100000;
        f.minCapital = null;
        f.price = null;
        renderForm();
      });

      const codeInput = h('input', { class: 'inp', value: f.code, placeholder: '如 600519 / AAPL' });
      st.fields.code = codeInput;
      /* 输入即同步：不能只依赖 blur（macOS Safari 点击按钮不会让输入框失焦） */
      codeInput.addEventListener('input', () => {
        f.code = codeInput.value.trim().toUpperCase();
      });
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
          if (hit) { f.name = hit.name; f.nameFor = code; }
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
        st.fields[key] = inp;
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
      st.fields.note = noteInput;
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
      paint(hintHost, [h('span', { text: bits.join(' · ') })]);
    }

    /** 提交前从输入框读回表单状态：以 DOM 为唯一真源。

    两个真实踩过的坑：
      1) macOS Safari 点击按钮不会让输入框失焦，只靠 blur 同步会导致
         「输入框明明有值，点创建却提示要填写」；
      2) 创建成功后如果只清空状态而不重绘表单，输入框仍显示旧代码，
         再次点击就会拿不到代码。
    所以提交前一律以输入框内容为准。
    */
    function syncFromDom() {
      const f = st.form;
      const fl = st.fields || {};
      if (fl.code) {
        const code = (fl.code.value || '').trim().toUpperCase();
        if (code) f.code = code;
      }
      ['initial', 'lot', 'fee', 'slippage', 'stopLoss', 'takeProfit', 'participation'].forEach((k) => {
        if (fl[k] && String(fl[k].value).trim() !== '') f[k] = fl[k].value;
      });
      if (fl.note) f.note = fl.note.value;
      return f.code || '';
    }

    async function create() {
      const f = st.form;
      const code = syncFromDom();
      if (!code) { ctx.toast('请先填写标的代码', 'warn'); return; }
      /* 名称未知、或名称属于另一个标的时即时解析
         （输入框被改成别的代码后，预填的旧名称不能跟着提交） */
      if (!f.name || f.nameFor !== code) {
        try {
          const s = await api.search(code);
          const hit = (s.rows || []).find((r) => String(r.code).toUpperCase() === code) || (s.rows || [])[0];
          if (hit && hit.name) { f.name = hit.name; f.nameFor = code; }
        } catch (e) { /* 名称可选 */ }
      }
      const initialUsed = Number(f.initial) || 100000;
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
      const label = f.name || code;
      ctx.toast('正在创建并回溯历史…', 'info');
      try {
        const res = await api.strategyCreate(body);
        st.selected = res.run && res.run.id;
        ctx.toast('跟踪任务已创建：' + label, 'ok');
        /* 资金不够买一手时明确提示，否则用户会看到"信号很多但没成交" */
        if (res.minCapital && res.minCapital > initialUsed) {
          ctx.toast('注意：' + (f.lot || 100) + ' 股约需 ' + F.amt(res.minCapital, body.market) +
            '，当前初始资金 ' + F.amt(initialUsed, body.market) + '，买入信号会被跳过（可在详情里调整）', 'warn');
        }
        f.code = '';
        f.name = '';
        f.nameFor = null;
        f.params = {};
        f.note = '';
        f.price = null;
        f.minCapital = null;
        /* 手续费 / 滑点 / 止损止盈 属于成本假设，保留给下一个任务 */
        renderForm();              /* 重绘表单，使界面与状态一致（输入框同时被清空） */
        renderHint();
        await refreshAll();
        if (st.selected) await loadDetail();
      } catch (e) {
        ctx.toast('创建失败：' + e.message, 'err');
      }
    }

    /* ----------------------------------------------------- 任务列表 */

    function renderList() {
      const rows = (st.overview && st.overview.rows) || [];
      if (!rows.length) {
        /* 空态：只切显示并原位改写文案；表格实例留在槽位里（下次有任务直接复用） */
        vis(listSlot.tbl, false);
        vis(listSlot.empty, true);
        paint(listSlot.empty, [ui.empty('还没有跟踪任务：在上方选择标的与策略创建，系统会回溯历史并持续跟踪')]);
        return;
      }
      vis(listSlot.empty, false);
      vis(listSlot.tbl, true);
      /* 高亮行由 cfg.activeKey 决定，ui.tbl 每次渲染都会读它 ——
         这里与 wrap.update 改写 cfg.rows 同一路子，先同步再 update，避免选中态停在旧行 */
      if (listTbl) { listCfg.activeKey = st.selected; listTbl.update(rows); return; }
      listCfg = {
        cols: [
          {
            key: 'status', label: '状态', noSort: true, width: '92px',
            // 除了「跟踪中」，还必须能看到引擎**消化到哪一天**：曾经出现过游标静默冻结的
            // 事故（任务全是空仓、tick 次数照涨、界面毫无提示），所以把推进健康度做成显式提示
            render: (r) => {
              const kids = [h('span', {
                class: 'chip ' + (r.status === 'running' ? 'accent' : ''),
                text: r.status === 'running' ? '跟踪中' : '已暂停',
              })];
              if (r.stalled) {
                kids.push(h('span', {
                  class: 'chip warn', text: '落后 ' + r.lagDays + ' 天',
                  title: '引擎已处理至 ' + (r.lastBarDate || '?') + '，最新K线 ' + (r.availableTo || '?')
                    + '：尚未消化新K线，请检查数据源或重启引擎',
                }));
              } else if (r.lastBarDate) {
                kids.push(h('span', {
                  class: 'dim3', text: '至 ' + String(r.lastBarDate).slice(5),
                  title: '已处理至 ' + r.lastBarDate + (r.availableTo ? '，最新K线 ' + r.availableTo : ''),
                }));
              }
              return h('span', {}, kids);
            },
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
      };
      listTbl = ui.tbl(listCfg);
      listSlot.tbl.appendChild(listTbl);
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

    /* 变更摘要：15 秒自动刷新也会走到这里（buildAdjust → updateDiff），
       所以同样原位更新 —— 子节点数是「1 + 变更项数 + 0/1」，paint 按位置合并即可 */
    function updateDiff() {
      const run = st.detail && st.detail.run;
      if (!run) { paint(diffHost, []); return; }
      const { patch, logicKeys } = currentPatch(run);
      const keys = Object.keys(patch);
      if (!keys.length) {
        saveBtn.disabled = true;
        paint(diffHost, [h('span', { class: 'dim3', text: '尚未修改任何字段' })]);
        return;
      }
      const logic = keys.filter((k) => logicKeys.indexOf(k) >= 0);
      const kids = [h('span', { class: 'dim3', text: '待提交 ' + keys.length + ' 项：' })];
      keys.forEach((k) => {
        const from = run[k];
        kids.push(h('span', { class: 'chip' + (logicKeys.indexOf(k) >= 0 ? ' warn' : ' accent') }, [
          h('span', { text: fieldLabel(k) + ' ' + fmtVal(from) + ' → ' + fmtVal(patch[k]) }),
        ]));
      });
      if (logic.length && !adjustState.reset) {
        saveBtn.disabled = true;
        kids.push(h('span', { class: 'chip warn', text: '需勾选「重置并重新回溯」后才能提交' }));
      } else {
        saveBtn.disabled = false;
      }
      paint(diffHost, kids);
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
      const revs = (run.revisions || []);
      if (!revs.length) {
        paint(revHost, [h('div', { class: 'legend-inline' }, ['该任务创建后尚未调整过配置'])]);
        return;
      }
      /* 列表按位置原位改写：新记录一次性插到最前时，也只重写各行文本，不重建节点 */
      paint(revHost, revs.map((rev) => {
        const rows = Object.keys(rev.fields || {}).map((k) => h('div', { class: 'legend-inline monospaced' }, [
          h('span', { class: 'chip', text: fieldLabel(k) }),
          h('span', { class: 'dim3', text: fmtVal(rev.fields[k].from) + ' → ' }),
          h('span', { text: fmtVal(rev.fields[k].to) }),
        ]));
        return h('div', { class: 'news-item' }, [
          h('div', { class: 'time', text: F.clock(rev.ts) }),
          h('div', { class: 'body' }, [
            h('div', { class: 'txt' }, [
              h('strong', { text: '调整 ' + Object.keys(rev.fields || {}).length + ' 项' }),
              rev.reset ? '　' : '　仅影响后续成交　',
              rev.reset ? h('span', { class: 'chip warn', text: '已重置并重新回溯' }) : null,
            ]),
            h('div', { style: { marginTop: '6px', display: 'grid', gap: '3px' } }, rows),
          ]),
        ]);
      }));
    }

    /* ----------------------------------------------------- 任务详情 */

    function renderDetail() {
      const d = st.detail;
      if (!d) {
        /* 详情为空：收起骨架 + 释放旧图，展示空态（骨架下次直接复用，不重建） */
        if (eqChart) { eqChart.destroy(); eqChart = null; eqChartRunId = null; }
        vis(detailSkeleton, false);
        vis(detailEmpty, true);
        paint(detailEmpty, [ui.empty('点击任务行的「详情」查看权益曲线、月度收益与逐笔交易')]);
        return;
      }
      vis(detailEmpty, false);
      vis(detailSkeleton, true);
      const run = d.run;
      const s = d.stats;

      /* 换任务时才销毁旧图；同一任务的 15 秒刷新只 setData —— 重建画布会闪 */
      if (eqChart && eqChartRunId !== run.id) { eqChart.destroy(); eqChart = null; eqChartRunId = null; }

      paint(dtHead, [
        h('h3', { text: (run.name || run.code) + ' · ' + run.strategyName }),
        h('span', { class: 'mono-sm', text: (run.market === 'us' ? 'US:' : '') + run.code }),
        h('span', { class: 'chip', text: '周期 ' + run.period }),
        h('span', { class: 'chip', text: '初始 ' + F.amt(run.initial, run.market) }),
        h('span', { class: 'chip', text: Object.keys(run.params || {}).map((k) => k + '=' + run.params[k]).join(' · ') }),
        h('span', { class: 'chip' + (run.status === 'running' ? ' accent' : ''), text: run.status === 'running' ? '跟踪中' : '已暂停' }),
        h('span', { class: 'mono-sm', text: '观察期 ' + (s.startedFrom || run.startDate) + ' → 至今（' + s.daysObserved + ' 个交易日）' }),
      ]);

      /* 指标卡数量与顺序固定 → paint 只改数值与配色 */
      paint(dtStats, [
        statCard('累计收益率', F.pct(s.returnPct), F.dir(s.returnPct)),
        statCard('年化收益', F.pct(s.annualizedPct), F.dir(s.annualizedPct)),
        statCard('盈亏金额', F.amt(s.totalPnl, run.market), F.dir(s.totalPnl)),
        statCard('已实现 / 浮动', F.amt(s.realized, run.market) + ' / ' + F.amt(s.unrealized, run.market), F.dir(s.unrealized)),
        statCard('胜率', F.num(s.winRate, 1) + '%', s.winRate >= 50 ? 'up' : (s.trades ? 'down' : '')),
        statCard('交易次数', s.trades + ' 笔（' + s.wins + ' 胜 / ' + s.losses + ' 负）'),
        statCard('盈亏比', (s.profitFactorInfinite || !isFinite(s.profitFactor)) ? '∞（无亏损）' : F.num(s.profitFactor, 2)),
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

      /* 当前持仓：有仓 / 空仓两种形态按位置合并（结构不同时 morph 会换掉具体单元格） */
      const posKids = [];
      if (d.position) {
        const p = d.position;
        posKids.push(h('div', { class: 'pos-card' }, [
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
          posKids.push(h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
            h('span', { class: 'phase-tag live', text: '实时' }),
            '该持仓由引擎在观察期内按真实行情成交（非历史回溯），是策略前向验证的有效样本',
          ]));
        }
      } else {
        posKids.push(h('div', { class: 'legend-inline' }, ['当前空仓' + (run.pending ? '，已有待执行信号：' + (run.pending === 'buy' ? '买入' : '卖出') + '（下一根K线开盘成交）' : '')]));
      }
      paint(dtPos, posKids);

      paint(dtProgress, [
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
      ]);

      buildAdjust(run);
      renderRevisions(run);

      /* 权益曲线：同一任务只换数据（setData），换任务 / 数据不足时才销毁重建 */
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
        if (!eqChart) {
          clear(eqHost);                       /* 首次 / 换任务：清掉可能残留的空态或旧画布 */
          eqChart = window.AD.chart.line(eqHost, {
            height: 240, fmt: (v) => F.amt(v, run.market), series,
          });
          eqChartRunId = run.id;
        }
        eqChart.setData(series);
      } else {
        if (eqChart) { eqChart.destroy(); eqChart = null; eqChartRunId = null; }
        paint(eqHost, [ui.empty('权益数据不足')]);
      }

      /* 月度 / 逐笔：列定义只在换任务时重建（币种跟着任务走），同一任务只 update */
      if (tblRunId !== run.id) {
        if (monthlyTbl) monthlyTbl.setCols(monthlyCols(run));
        if (tradesTbl) tradesTbl.setCols(tradesCols(run));
        tblRunId = run.id;
      }

      const monthly = d.monthly || [];
      if (!monthly.length) {
        vis(monthlyTblSlot, false);
        vis(monthlyEmptySlot, true);
        paint(monthlyEmptySlot, [ui.empty('暂无月度数据')]);
      } else {
        vis(monthlyEmptySlot, false);
        vis(monthlyTblSlot, true);
        if (monthlyTbl) monthlyTbl.update(monthly);
        else {
          monthlyTbl = ui.tbl({ cols: monthlyCols(run), rows: monthly, compact: true });
          monthlyTblSlot.appendChild(monthlyTbl);
        }
      }

      const trades = d.trades || [];
      if (!trades.length) {
        vis(tradesTblSlot, false);
        vis(tradesEmptySlot, true);
        paint(tradesEmptySlot, [ui.empty('观察期内尚未产生完整交易（可能一直持有或尚未触发卖出信号）')]);
      } else {
        vis(tradesEmptySlot, false);
        vis(tradesTblSlot, true);
        if (tradesTbl) tradesTbl.update(trades.slice(0, 100));
        else {
          tradesTbl = ui.tbl({ cols: tradesCols(run), rows: trades.slice(0, 100), maxHeight: '340px', compact: true });
          tradesTblSlot.appendChild(tradesTbl);
        }
      }

      /* 信号 */
      const sigs = d.signals || [];
      if (!sigs.length) {
        paint(signalsHost, [ui.empty('暂无信号记录')]);
      } else {
        paint(signalsHost, sigs.slice(0, 20).map((sg) => h('div', { class: 'news-item' }, [
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
        ])));
      }
    }

    /* 详情两张表的列定义：随任务（币种）变化，因此抽成函数，供 setCols 复用 */
    function monthlyCols(run) {
      return [
        { key: 'month', label: '月份', noSort: true },
        { key: 'days', label: '交易日', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num', text: String(m.days) }) },
        { key: 'equityEnd', label: '月末权益', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num', text: F.amt(m.equityEnd, run.market) }) },
        { key: 'monthlyReturnPct', label: '当月收益', cls: 'n', noSort: true, render: (m) => pct(m.monthlyReturnPct) },
        { key: 'realized', label: '已实现盈亏', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num ' + F.dir(m.realized), text: F.amt(m.realized, run.market) }) },
        { key: 'trades', label: '交易', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num', text: String(m.trades) }) },
        { key: 'winRate', label: '当月胜率', cls: 'n', noSort: true, render: (m) => h('span', { class: 'num ' + (m.winRate >= 50 ? 'up' : (m.trades ? 'down' : 'flat')), text: m.trades ? F.num(m.winRate, 0) + '%' : '—' }) },
      ];
    }

    function tradesCols(run) {
      return [
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
      ];
    }

    /* --------------------------------------------------------- 刷新 */

    async function loadDetail() {
      if (!st.selected) { renderDetail(); return; }
      try {
        st.detail = await api.strategyRun(st.selected);
        renderDetail();
      } catch (e) {
        /* 详情获取失败：只改这一块（骨架留在原位，下一次成功时接着用） */
        if (eqChart) { eqChart.destroy(); eqChart = null; eqChartRunId = null; }
        vis(detailSkeleton, false);
        vis(detailEmpty, true);
        paint(detailEmpty, [ui.empty('详情获取失败：' + e.message)]);
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
        st.form.nameFor = ctx.state.symbol.code;   // 预填名称属于这个代码
        st.form.market = ctx.state.symbol.market || st.form.market;
        st.form.lot = st.form.market === 'cn' ? 100 : 1;
      }
      // 从回测页跳转过来时，带入策略与参数
      const pre = ctx.state.trackerPrefill;
      if (pre) {
        if (pre.strategy && st.meta.strategies[pre.strategy]) st.form.strategy = pre.strategy;
        if (pre.params) st.form.params = Object.assign({}, pre.params);
        if (pre.period) st.form.period = pre.period;
        if (pre.name) { st.form.name = pre.name; st.form.nameFor = st.form.code; }
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
