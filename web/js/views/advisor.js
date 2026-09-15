/* ==========================================================================
   视图 · AI 选股（多标的批量研判：建议 / 凯利仓位 / 组合分配）

   接口：POST /api/advisor/recommend（由主程接入 api.advisorRecommend）
   请求体：{ market, codes: [...], symbols: [{ code, market }],
            horizon, capital, kellyFraction, maxWeight }
   返回体：{ ok, rows: [{ code, name, price, changePct, action, actionText, score,
                         confidence, signals: [{ key, label, dir, brief }],
                         ensemble: { buy, sell, hold, votes: [{ strategy, signal }] },
                         edge: { trades, winRate, payoff, edge, expectancy, sample, note },
                         kelly: { fStar, fraction, weight, amount, shares, note },
                         forecast: { expectedReturn, upProb, bandLow, bandHigh, note },
                         plan: { entry, stop, target1, target2, riskReward },
                         risk: { atrPct, vol, maxDrawdown, note } }],
             portfolio: { totalWeight, cash, rows: [{ code, name, weight, amount }], note },
             disclaimer }

   约定：本视图只消费上述字段，任何字段缺失一律降级为「—」，绝不臆造数值；
        自动刷新默认 60 秒，且仅在「提交过标的」之后才开始轮询。
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;
  const isNum = window.AD.isNum;
  const MARKET_LABEL = window.AD.MARKET_LABEL || { cn: 'A股', us: '美股' };

  const AUTO_MS = 60000;              /* 自动刷新间隔 */
  const MAX_SYMBOLS = 30;             /* 单次批量上限，避免一次性打爆服务端 */
  const SEP = /[\s,，、;；|]+/;         /* 逗号 / 空格 / 换行 / 分号分隔 */
  const FACTOR_MAX = 3;               /* 因子 chips 最多展示数量 */
  const ALLOC_COLORS = ['var(--accent)', '#7fb0ff', 'var(--warn)', 'var(--down)', '#8b95a5'];

  /* 六个建议档位 —— 只用项目已有 chip 配色 */
  const ACTION_LABEL = {
    buy: '买入', add: '增持', hold: '持有', reduce: '减仓', sell: '卖出', watch: '观望', avoid: '回避',
  };
  const ACTION_CLS = {
    buy: 'chip up', add: 'chip up', hold: 'chip', reduce: 'chip warn',
    sell: 'chip down', watch: 'chip accent', avoid: 'chip warn',
  };
  /* 因子方向 -> 涨跌色（兼容多种服务端写法） */
  const DIR_CLS = {
    up: 'up', bull: 'up', bullish: 'up', long: 'up', buy: 'up', pos: 'up', positive: 'up',
    down: 'down', bear: 'down', bearish: 'down', short: 'down', sell: 'down', neg: 'down', negative: 'down',
  };

  const MODEL_NOTES = [
    '预测窗口（horizon）默认 20 个交易日：期望收益、上涨概率与价格区间均在该窗口内计算。',
    '建议档位：买入 / 增持 / 持有 / 减仓 / 卖出 / 观望 —— 由多策略共识票数、统计优势（胜率 × 盈亏比 → 期望值）与风险度量共同裁定，而非单一指标。',
    '凯利仓位：先由历史期望值估计最优下注比例 f*，再按「凯利折扣」打折，并受单只权重上限约束；仓位金额 = 本金 × 权重，股数为金额折算后的整手 / 整股。',
    '风险列：ATR% 衡量近期日内波动，年化波动衡量收益离散度，历史最大回撤衡量极端回撤承受度，仅作风险提示，不参与收益预测。',
    '组合分配：逐只权重求和为总仓位，其余计为现金；单只权重上限用于避免过度集中。',
  ];
  const DISCLAIMER_DEFAULT = '本页结论由公开行情数据经统计模型自动计算得出，仅用于技术研究与学习，不构成任何投资建议；' +
    '模型存在失效风险，历史统计不代表未来表现，据此操作的盈亏由投资者自行承担。';

  /* ------------------------------------------------------------ 小工具 */

  /* 数值文本；空值 / 非数值统一降级为「—」 */
  function text(v, d) {
    if (v === null || v === undefined || v === '') return d === undefined ? '—' : d;
    return String(v);
  }
  function dash() { return h('span', { class: 'num dim3', text: '—' }); }
  function pctText(v, d) { return isNum(v) ? F.num(v, d === undefined ? 2 : d) + '%' : '—'; }

  /* 百分比口径兼容：0.25 与 25 都按 25% 展示（服务端口径未定时不做臆造） */
  function asPct(v) {
    if (!isNum(v)) return null;
    return Math.abs(v) <= 1.5 ? v * 100 : v;
  }
  function dirCls(d) { return DIR_CLS[String(d || '').toLowerCase()] || ''; }

  function noteLine(t) {
    return h('div', { class: 'legend-inline', style: { marginTop: '10px', lineHeight: '1.7' } }, [
      h('span', { class: 'dim3', text: text(t, '—') }),
    ]);
  }

  /* 指标卡（复用 .metric-list / .metric） */
  function metricList(items) {
    const wrap = h('div', { class: 'metric-list' });
    items.forEach((it) => {
      const cell = h('div', { class: 'metric' }, [h('div', { class: 'k', text: it[0] })]);
      const box = h('div', { class: 'v' + (it[2] ? ' ' + it[2] : '') });
      const v = it[1];
      if (v instanceof Node) box.appendChild(v);
      else box.textContent = v === null || v === undefined || v === '' ? '—' : String(v);
      cell.appendChild(box);
      wrap.appendChild(cell);
    });
    return wrap;
  }

  /* ui.seg 不会自行切换高亮态，这里按顺序手动同步（只用已有 CSS 类） */
  function markSeg(seg, active, values) {
    Array.prototype.forEach.call(seg.children, (b, i) => b.classList.toggle('active', values[i] === active));
  }

  /* 单个 token -> { code, market, name }；无法识别为代码时返回 null（交给搜索接口解析中文名） */
  function parseToken(raw) {
    let s = String(raw || '').replace(/[（(]/g, '').replace(/[）)]/g, '').trim();
    if (!s) return null;
    let name = '';
    const kv = /^([A-Za-z0-9.\-]+)[:：](.+)$/.exec(s);   /* 支持「600519:贵州茅台」写法 */
    if (kv) { s = kv[1]; name = kv[2].trim(); }
    /* 只在后面紧跟 6 位数字时剥离交易所前缀，避免把 SHEL / SHOP 这类美股代码截断 */
    s = s.replace(/^(sh|sz|bj)[.\-]?(?=\d{6})/i, '').replace(/^us[:.]/i, '');
    if (/^\d{6}$/.test(s)) return { code: s, market: 'cn', name };
    if (/^[A-Za-z][A-Za-z0-9.\-]{0,9}$/.test(s)) return { code: s.toUpperCase(), market: 'us', name };
    return null;
  }

  /* ------------------------------------------------------------ 视图 */

  function mount(root, ctx) {
    const st = {
      rows: [], portfolio: null, disclaimer: '',
      submitted: false, auto: true, loading: false, destroyed: false,
      lastBody: null, symbolMap: {}, fields: {},
    };
    let timer = null;

    const statHost = h('span', { class: 'hint', text: '待提交标的' });
    const formHint = h('span', { class: 'dim3', text: '已输入 0 个标的' });
    const modelHost = h('div');
    const disclaimerHost = h('div');
    const formHost = h('div');
    const tableHost = h('div');
    const portfolioHost = h('div');

    /* ------------------------------------------------- 标的 / 参数表单 */

    const codeInput = h('textarea', {
      class: 'inp',
      rows: 3,
      placeholder: '如：600519 300750 601318\n或：AAPL, MSFT, NVDA（也支持中文名：茅台 / 宁德时代）',
      style: {
        width: '100%', minHeight: '58px', padding: '7px 8px', borderRadius: '5px',
        background: 'var(--surface-2)', border: '1px solid var(--line-2)', color: 'var(--text)',
        fontFamily: 'var(--mono)', fontSize: '12px', lineHeight: '1.7', outline: 'none', resize: 'vertical',
      },
      on: {
        input: () => renderCodeHint(),
        /* ⌘/Ctrl + Enter 直接提交，符合批量粘贴后的操作习惯 */
        keydown: (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submit(); },
      },
    });
    st.fields.codes = codeInput;

    function readCodes() {
      return String(codeInput.value || '').split(SEP).map((s) => s.trim()).filter(Boolean);
    }
    function renderCodeHint() {
      const n = readCodes().length;
      formHint.textContent = '已输入 ' + n + ' 个标的' +
        (n > MAX_SYMBOLS ? '（超过上限 ' + MAX_SYMBOLS + '，提交时自动截断）' : '');
    }

    function numField(key, label, value, step, title) {
      const inp = h('input', { class: 'inp', type: 'number', value: String(value), step: step || 'any', title: title || '' });
      st.fields[key] = inp;
      return h('div', { class: 'field' }, [h('label', { text: label }), inp]);
    }

    const kellySel = h('select', { class: 'inp', title: '对凯利公式算出的 f* 打折，越小越保守' }, [
      h('option', { value: '0.25', text: '四分之一凯利（保守）' }),
      h('option', { value: '0.5', text: '半凯利（默认）' }),
      h('option', { value: '0.75', text: '四分之三凯利' }),
      h('option', { value: '1', text: '全凯利（激进）' }),
    ]);
    kellySel.value = '0.5';
    st.fields.kellyFraction = kellySel;

    /* 提交时从 DOM 读参数：以输入框为唯一真源（点击按钮不一定触发 blur） */
    function readParams() {
      const fl = st.fields;
      const pick = (el, def, positive) => {
        const v = el ? Number(String(el.value).trim()) : NaN;
        if (!isFinite(v)) return def;
        if (positive && v <= 0) return def;
        return v;
      };
      let maxWeight = pick(fl.maxWeight, 0.25, true);
      if (maxWeight > 1.5) maxWeight = maxWeight / 100;      /* 允许直接填 25 表示 25% */
      return {
        horizon: Math.max(1, Math.round(pick(fl.horizon, 20, true))),
        capital: pick(fl.capital, 100000, true),
        kellyFraction: pick(fl.kellyFraction, 0.5, true),
        maxWeight: Math.min(1, Math.max(0.01, maxWeight)),
      };
    }

    async function importWatch() {
      const all = (ctx.getWatch ? ctx.getWatch() : []) || [];
      const market = ctx.state.market;
      const mine = all.filter((w) => (w.market || 'cn') === market);
      if (!all.length) { ctx.toast('自选股为空，先到「自选股」页面添加标的', 'warn'); return; }
      if (!mine.length) {
        ctx.toast('自选股中没有' + MARKET_LABEL[market] + '标的（当前市场为' + MARKET_LABEL[market] + '）', 'warn');
        return;
      }
      const have = {};
      readCodes().forEach((t) => { have[t.toUpperCase()] = 1; });
      const add = [];
      mine.forEach((w) => {
        const c = String(w.code || '').toUpperCase();
        if (c && !have[c]) { have[c] = 1; add.push(w.code); }
      });
      if (!add.length) { ctx.toast('自选股中的' + MARKET_LABEL[market] + '标的已在列表中', 'info'); return; }
      codeInput.value = (codeInput.value.trim() ? codeInput.value.replace(/\s+$/, '') + '\n' : '') + add.join(' ');
      renderCodeHint();
      ctx.toast('已从自选股导入 ' + add.length + ' 只' + MARKET_LABEL[market] + '标的', 'ok');
    }

    function renderForm() {
      clear(formHost);
      formHost.appendChild(h('div', { class: 'run-form' }, [
        h('div', { class: 'field wide' }, [
          h('label', { text: '标的' }),
          h('div', { style: { flex: '1 1 auto', minWidth: '0' } }, [
            codeInput,
            h('div', { class: 'legend-inline', style: { marginTop: '6px', alignItems: 'center' } }, [
              formHint,
              h('button', { class: 'btn ghost sm', text: '从自选股导入', on: { click: importWatch } }),
              h('button', {
                class: 'btn ghost sm', text: '清空',
                on: { click: () => { codeInput.value = ''; renderCodeHint(); } },
              }),
            ]),
          ]),
        ]),
        numField('horizon', '预测窗口（交易日）', 20, '1', '模型对未来多少个交易日做预测，默认 20'),
        numField('capital', '本金', 100000, 'any', '用于折算凯利仓位金额与股数，默认 100000'),
        h('div', { class: 'field' }, [h('label', { text: '凯利折扣' }), kellySel]),
        numField('maxWeight', '单只权重上限', 0.25, '0.05', '小数或百分数：0.25 与 25 都表示 25%'),
      ]));
    }

    /* ------------------------------------------------- 顶部说明 / 免责 */

    function renderModel() {
      clear(modelHost);
      modelHost.appendChild(h('div', { class: 'legend-inline', style: { lineHeight: '1.9', marginBottom: '6px' } }, [
        h('span', { class: 'chip accent', text: '模型说明' }),
        h('span', { class: 'dim3', text: '批量输入标的代码（逗号 / 空格 / 换行分隔），一次拿到逐只研判与组合仓位建议。' }),
      ]));
      MODEL_NOTES.forEach((t) => {
        modelHost.appendChild(h('div', { class: 'legend-inline', style: { lineHeight: '1.8' } }, [
          h('span', { class: 'dim3', text: '· ' + t }),
        ]));
      });
      modelHost.appendChild(disclaimerHost);
    }

    function renderDisclaimer() {
      clear(disclaimerHost);
      disclaimerHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '8px', lineHeight: '1.8' } }, [
        h('span', { class: 'chip warn', text: '免责声明' }),
        h('span', { class: 'dim3', text: text(st.disclaimer, DISCLAIMER_DEFAULT) }),
      ]));
    }

    /* ------------------------------------------------------- 主表 */

    /* 行 -> 市场 / 名称：优先服务端字段，缺失时回落到提交时的输入映射 */
    function rowMarket(r) {
      if (r && r.market) return r.market;
      const hit = st.symbolMap[String((r && r.code) || '').toUpperCase()];
      return (hit && hit.market) || ctx.state.market;
    }
    function rowName(r) {
      const hit = st.symbolMap[String((r && r.code) || '').toUpperCase()];
      return text(r && r.name, text(hit && hit.name, text(r && r.code)));
    }

    function addWatch(r) {
      const market = rowMarket(r);
      const name = rowName(r);
      if (ctx.isWatched && ctx.isWatched(market, r.code)) { ctx.toast('已在自选股中：' + name, 'info'); return; }
      const ok = ctx.addWatch(market, r.code, name);
      ctx.toast(ok ? '已加入自选：' + name : '已在自选股中：' + name, ok ? 'ok' : 'info');
    }

    function toTracker(r) {
      const market = rowMarket(r);
      const name = rowName(r);
      const p = readParams();
      ctx.openTracker(market, r.code, r.name || name, {
        name: name + ' · AI选股 h' + p.horizon,
      });
      ctx.toast('已带入「' + name + '」到策略跟踪，选择策略即可创建跟踪任务', 'info');
    }

    function buildCols() {
      return [
        {
          key: 'name', label: '标的', cls: 'name', noSort: true,
          render: (r) => {
            const nm = rowName(r);
            const code = text(r.code);
            /* 用户只输入代码时名称会回落到代码本身，此时不再重复显示一行代码 */
            const same = String(nm) === String(code);
            return h('span', {}, [
              h('span', { class: 'name', text: nm }),
              same ? null : h('span', { class: 'code', text: (rowMarket(r) === 'us' ? 'US:' : '') + code }),
            ]);
          },
        },
        {
          key: 'price', label: '现价 / 涨跌', cls: 'n', value: (r) => r.changePct,
          render: (r) => h('span', { class: 'num ' + F.dir(r.changePct) }, [
            h('span', { text: F.price(r.price, rowMarket(r)) }),
            h('span', { class: 'code', text: isNum(r.changePct) ? ' ' + F.pct(r.changePct) : ' —' }),
          ]),
        },
        {
          key: 'action', label: '建议', width: '104px', noSort: true,
          render: (r) => {
            const a = String(r.action || '').toLowerCase();
            const label = ACTION_LABEL[a] || text(r.actionText, '—');
            return h('span', { class: ACTION_CLS[a] || 'chip', title: text(r.actionText, label), text: label });
          },
        },
        {
          key: 'score', label: '评分', cls: 'n', value: (r) => r.score,
          render: (r) => h('span', {
            class: 'num' + (isNum(r.score) ? '' : ' dim3'),
            title: '综合评分（越高越积极）', text: F.num(r.score, 1),
          }),
        },
        {
          key: 'confidence', label: '置信度', cls: 'n', value: (r) => asPct(r.confidence),
          render: (r) => {
            const v = asPct(r.confidence);
            return h('span', {
              class: 'num' + (isNum(v) ? '' : ' dim3'),
              title: '模型对该结论的置信度',
              text: isNum(v) ? F.num(v, 0) + '%' : '—',
            });
          },
        },
        {
          key: 'ensemble', label: '策略共识', noSort: true, width: '196px',
          render: (r) => {
            const e = r.ensemble || {};
            if (!isNum(e.buy) && !isNum(e.sell) && !isNum(e.hold)) return dash();
            const votes = (e.votes || []).map((v) => text(v.strategy, '?') + ' → ' + text(v.signal, '?')).join('，');
            const line = h('span', {
              style: { display: 'inline-flex', gap: '4px' },
              title: votes || '服务端未返回逐策略票数',
            });
            if (isNum(e.buy)) line.appendChild(h('span', { class: 'chip up', text: '买 ' + e.buy }));
            if (isNum(e.hold)) line.appendChild(h('span', { class: 'chip', text: '持 ' + e.hold }));
            if (isNum(e.sell)) line.appendChild(h('span', { class: 'chip down', text: '卖 ' + e.sell }));
            return line;
          },
        },
        {
          key: 'edge', label: '统计优势', noSort: true, width: '180px',
          render: (r) => {
            const e = r.edge || {};
            if (!isNum(e.winRate) && !isNum(e.payoff) && !isNum(e.trades) && !isNum(e.expectancy)) return dash();
            const wr = asPct(e.winRate);
            const bits = '胜 ' + (isNum(wr) ? F.num(wr, 1) + '%' : '—') +
              ' · 赔 ' + F.num(e.payoff, 2) + ' · ' + (isNum(e.trades) ? e.trades : '—') + ' 笔';
            const tip = '样本 ' + text(e.sample, '—') +
              ' · 期望值/笔 ' + F.num(e.expectancy, 3) +
              ' · Edge ' + F.num(e.edge, 3) + (e.note ? ' · ' + e.note : '');
            return h('span', { class: 'num', title: tip, text: bits });
          },
        },
        {
          key: 'forecast', label: '预测（窗口内）', noSort: true, width: '288px',
          render: (r) => {
            const f = r.forecast || {};
            if (!isNum(f.expectedReturn) && !isNum(f.upProb) && !isNum(f.bandLow) && !isNum(f.bandHigh)) return dash();
            const mkt = rowMarket(r);
            const up = asPct(f.upProb);
            const lo = isNum(f.bandLow) ? F.price(f.bandLow, mkt) : '—';
            const hi = isNum(f.bandHigh) ? F.price(f.bandHigh, mkt) : '—';
            return h('span', {
              class: 'num',
              title: text(f.note, '预测窗口内的期望收益 / 上涨概率 / 价格区间'),
            }, [
              h('span', { class: isNum(f.expectedReturn) ? F.dir(f.expectedReturn) : 'flat', text: '期望 ' + F.pct(f.expectedReturn) }),
              h('span', { class: 'dim3', text: ' · 概率 ' + (isNum(up) ? F.num(up, 0) + '%' : '—') }),
              h('span', { class: 'dim3', text: ' · 区间 ' + lo + '~' + hi }),
            ]);
          },
        },
        {
          key: 'kelly', label: '凯利仓位', noSort: true, value: (r) => asPct((r.kelly || {}).weight), width: '196px',
          render: (r) => {
            const k = r.kelly || {};
            const w = asPct(k.weight);
            if (!isNum(w) && !isNum(k.amount) && !isNum(k.shares)) return dash();
            const mkt = rowMarket(r);
            const tip = '凯利 f* ' + F.num(k.fStar, 3) + ' · 折扣系数 ' + F.num(k.fraction, 2) +
              (k.note ? ' · ' + k.note : '');
            return h('span', { class: 'num', title: tip }, [
              h('span', { text: isNum(w) ? F.num(w, 1) + '%' : '—' }),
              h('span', {
                class: 'dim3',
                text: ' · ' + (isNum(k.amount) ? F.amt(k.amount, mkt) : '—') +
                  (isNum(k.shares) ? ' / ' + F.num(k.shares, 0) + ' 股' : ''),
              }),
            ]);
          },
        },
        {
          key: 'plan', label: '交易计划', noSort: true, width: '252px',
          render: (r) => {
            const p = r.plan || {};
            if (!isNum(p.entry) && !isNum(p.stop) && !isNum(p.target1) && !isNum(p.target2)) return dash();
            const mkt = rowMarket(r);
            const bits = '入 ' + F.price(p.entry, mkt) + ' · 损 ' + F.price(p.stop, mkt) +
              ' · 标 ' + F.price(p.target1, mkt) + ' / ' + F.price(p.target2, mkt) +
              (isNum(p.riskReward) ? ' · 盈亏比 ' + F.num(p.riskReward, 2) : '');
            return h('span', { class: 'num', title: '入场 / 止损 / 目标位由服务端模型给出，仅作计划参考', text: bits });
          },
        },
        {
          key: 'signals', label: '关键因子', noSort: true, width: '246px',
          render: (r) => {
            const sigs = Array.isArray(r.signals) ? r.signals : [];
            if (!sigs.length) return dash();
            const box = h('span', { style: { display: 'inline-flex', gap: '4px' } });
            sigs.slice(0, FACTOR_MAX).forEach((s) => {
              box.appendChild(h('span', {
                class: 'chip ' + dirCls(s.dir),
                title: text(s.brief, text(s.label, '')),
                text: text(s.label, text(s.key, '因子')),
              }));
            });
            if (sigs.length > FACTOR_MAX) {
              box.appendChild(h('span', {
                class: 'chip',
                text: '+' + (sigs.length - FACTOR_MAX),
                title: sigs.slice(FACTOR_MAX).map((s) => text(s.label, s.key)).join('、'),
              }));
            }
            return box;
          },
        },
        {
          key: 'risk', label: '风险', noSort: true, width: '236px',
          render: (r) => {
            const k = r.risk || {};
            if (!isNum(k.atrPct) && !isNum(k.vol) && !isNum(k.maxDrawdown)) return dash();
            return h('span', { class: 'num', title: text(k.note, 'ATR% / 年化波动 / 历史最大回撤') }, [
              h('span', { text: 'ATR ' + pctText(k.atrPct, 2) }),
              h('span', { class: 'dim3', text: ' · 波动 ' + pctText(k.vol, 2) }),
              h('span', { class: 'down', text: ' · 回撤 ' + (isNum(k.maxDrawdown) ? '-' + F.num(k.maxDrawdown, 2) + '%' : '—') }),
            ]);
          },
        },
        {
          key: 'act', label: '操作', noSort: true, width: '236px',
          render: (r) => h('div', { style: { display: 'flex', gap: '5px' } }, [
            h('button', {
              class: 'btn ghost sm', text: '查看K线', title: '打开个股详情与K线图',
              on: { click: (e) => { e.stopPropagation(); ctx.openSymbol(rowMarket(r), r.code, rowName(r)); } },
            }),
            h('button', {
              class: 'btn ghost sm', text: '加入自选', title: '加入自选股列表',
              on: { click: (e) => { e.stopPropagation(); addWatch(r); } },
            }),
            h('button', {
              class: 'btn ghost sm', text: '转为策略跟踪', title: '带入策略跟踪页，选择策略后创建跟踪任务',
              on: { click: (e) => { e.stopPropagation(); toTracker(r); } },
            }),
          ]),
        },
      ];
    }

    function renderTable() {
      clear(tableHost);
      if (!st.rows.length) {
        tableHost.appendChild(ui.empty(st.submitted
          ? '服务端未返回任何标的的研判结果，请检查代码是否正确或稍后重试'
          : '在上方填写标的（代码或中文名）后点击「开始 AI 分析」'));
        return;
      }
      tableHost.appendChild(ui.tbl({
        cols: buildCols(),
        rows: st.rows,
        sortKey: 'score',
        sortDir: 'desc',
        maxHeight: 'calc(100vh - 460px)',
        rowKey: (r) => rowMarket(r) + ':' + text(r.code),
        onRow: (r) => ctx.openSymbol(rowMarket(r), r.code, rowName(r)),
        emptyText: '暂无标的',
      }));
    }

    /* --------------------------------------------------- 组合分配 */

    function renderPortfolio() {
      clear(portfolioHost);
      if (!st.submitted) {
        portfolioHost.appendChild(ui.empty('提交标的后显示组合权重分配'));
        return;
      }
      const p = st.portfolio || {};
      const rows = Array.isArray(p.rows) ? p.rows : [];
      const cap = st.lastBody && isNum(st.lastBody.capital) ? st.lastBody.capital : null;
      const mkt = ctx.state.market;
      const mktOfRow = (r) => (r.market || (st.symbolMap[String(r.code || '').toUpperCase()] || {}).market || mkt);
      const ws = rows.map((r) => asPct(r.weight)).filter(isNum);
      const sumW = isNum(p.totalWeight) ? asPct(p.totalWeight) : (ws.length ? ws.reduce((a, b) => a + b, 0) : null);
      const amts = rows.map((r) => r.amount).filter(isNum);
      const sumAmt = amts.length ? amts.reduce((a, b) => a + b, 0) : null;
      const cash = isNum(p.cash) ? p.cash : (isNum(cap) && sumAmt !== null ? Math.max(0, cap - sumAmt) : null);

      if (!rows.length && !isNum(sumW) && !isNum(cash)) {
        portfolioHost.appendChild(ui.empty('服务端未返回组合分配数据'));
        if (p.note) portfolioHost.appendChild(noteLine(p.note));
        return;
      }

      portfolioHost.appendChild(metricList([
        ['纳入标的', rows.length ? rows.length + ' 只' : (st.rows.length + ' 只（无分配明细）')],
        ['总仓位', isNum(sumW) ? F.num(sumW, 1) + '%' : '—'],
        ['现金 / 未分配', isNum(cash) ? F.amt(cash, mkt) : '—'],
        ['资金合计', isNum(sumAmt) ? F.amt(sumAmt, mkt) : '—'],
        ['本金', isNum(cap) ? F.amt(cap, mkt) : '—'],
        ['单只上限', isNum(st.lastBody && st.lastBody.maxWeight) ? F.num(asPct(st.lastBody.maxWeight), 1) + '%' : '—'],
      ]));

      /* 权重条：按权重成比例分配宽度，剩余为现金 */
      if (ws.length) {
        const bar = h('div', { class: 'breadth-bar', style: { marginTop: '12px' } });
        rows.forEach((r, i) => {
          const w = asPct(r.weight);
          if (!isNum(w) || w <= 0) return;
          bar.appendChild(h('div', {
            style: { flex: String(w), background: ALLOC_COLORS[i % ALLOC_COLORS.length] },
            title: text(r.name, r.code) + ' ' + F.num(w, 1) + '%',
          }));
        });
        const rest = isNum(sumW) ? Math.max(0, 100 - sumW) : 0;
        if (rest > 0.01) {
          bar.appendChild(h('div', {
            style: { flex: String(rest), background: 'var(--surface-3)' },
            title: '现金 / 未分配 ' + F.num(rest, 1) + '%',
          }));
        }
        portfolioHost.appendChild(bar);

        const legend = h('div', { class: 'breadth-legend' });
        rows.slice(0, 12).forEach((r, i) => {
          const w = asPct(r.weight);
          legend.appendChild(h('span', {}, [
            h('span', {
              style: {
                display: 'inline-block', width: '8px', height: '8px', borderRadius: '2px',
                background: ALLOC_COLORS[i % ALLOC_COLORS.length], marginRight: '5px',
              },
            }),
            h('span', { text: text(r.name, r.code) }),
            h('b', { text: ' ' + (isNum(w) ? F.num(w, 1) + '%' : '—') }),
          ]));
        });
        if (rows.length > 12) legend.appendChild(h('span', { text: '… 其余 ' + (rows.length - 12) + ' 只略' }));
        if (rest > 0.01) {
          legend.appendChild(h('span', {}, [
            h('span', {
              style: {
                display: 'inline-block', width: '8px', height: '8px', borderRadius: '2px',
                background: 'var(--surface-3)', marginRight: '5px',
              },
            }),
            h('span', { text: '现金 / 未分配' }),
            h('b', { text: ' ' + F.num(rest, 1) + '%' }),
          ]));
        }
        portfolioHost.appendChild(legend);
      }

      if (rows.length) {
        const maxW = Math.max.apply(null, ws.concat([1]));
        portfolioHost.appendChild(h('div', { style: { marginTop: '12px' } }, [
          ui.tbl({
            cols: [
              {
                key: 'name', label: '标的', cls: 'name', noSort: true,
                render: (r) => {
                  const nm = text(r.name, text(r.code));
                  const same = String(nm) === String(r.code);
                  return h('span', {}, [
                    h('span', { class: 'name', text: nm }),
                    same ? null : h('span', { class: 'code', text: text(r.code) }),
                  ]);
                },
              },
              {
                key: 'weight', label: '权重', cls: 'n', value: (r) => asPct(r.weight),
                render: (r) => {
                  const w = asPct(r.weight);
                  return h('span', { class: 'num' + (isNum(w) ? '' : ' dim3'), text: isNum(w) ? F.num(w, 1) + '%' : '—' });
                },
              },
              {
                key: 'amount', label: '金额', cls: 'n', value: (r) => r.amount,
                render: (r) => h('span', {
                  class: 'num' + (isNum(r.amount) ? '' : ' dim3'),
                  text: isNum(r.amount) ? F.amt(r.amount, mktOfRow(r)) : '—',
                }),
              },
              {
                key: 'bar', label: '占比', noSort: true, width: '180px',
                render: (r) => {
                  const w = asPct(r.weight);
                  const p = isNum(w) ? Math.min(100, (w / maxW) * 100) : 0;
                  return h('div', { class: 'prog' }, [
                    h('div', { class: 'prog-bar' }, [h('i', { style: { width: p.toFixed(1) + '%' } })]),
                  ]);
                },
              },
            ],
            rows,
            compact: true,
            emptyText: '无分配明细',
          }),
        ]));
      }

      if (p.note) portfolioHost.appendChild(noteLine(p.note));
      portfolioHost.appendChild(noteLine('组合分配由服务端按凯利折扣与单只权重上限折算，权重之和即总仓位；' +
        '本金与现金按当前市场本币口径，不做跨市场汇率换算。'));
    }

    /* --------------------------------------------------------- 取数 */

    /* api.advisorRecommend 尚未接入时，兜底直接 POST /api/advisor/recommend */
    function recommend(body) {
      if (typeof api.advisorRecommend === 'function') return api.advisorRecommend(body);
      if (typeof api.post === 'function') return api.post('advisor/recommend', body);
      return Promise.reject(new Error('api.advisorRecommend 未接入'));
    }

    async function load(body) {
      const b = body || st.lastBody;
      if (!b) return;
      st.lastBody = b;
      st.loading = true;
      submitBtn.disabled = true;
      statHost.textContent = '模型计算中…';
      clear(tableHost);
      tableHost.appendChild(ui.loading('模型计算中…（多标的批量研判可能需要数秒）'));
      try {
        const res = await recommend(b);
        if (st.destroyed) return;
        if (!res || res.ok === false) {
          throw new Error((res && (res.message || res.error)) || '服务端未返回有效结果');
        }
        st.rows = Array.isArray(res.rows) ? res.rows : [];
        st.portfolio = res.portfolio || null;
        st.disclaimer = res.disclaimer || '';
        renderDisclaimer();
        renderTable();
        renderPortfolio();
        const bulls = st.rows.filter((r) => ['buy', 'add'].indexOf(String(r.action || '').toLowerCase()) >= 0).length;
        statHost.textContent = '共 ' + st.rows.length + ' 只 · 买入/增持 ' + bulls + ' 只 · 窗口 h=' + b.horizon +
          ' · 本金 ' + F.amt(b.capital, b.market) + ' · 更新 ' + F.clock(Date.now());
      } catch (e) {
        if (st.destroyed) return;
        st.rows = [];
        st.portfolio = null;
        statHost.textContent = '分析失败';
        clear(tableHost);
        tableHost.appendChild(ui.empty('分析失败：' + e.message + '（接口 /api/advisor/recommend）'));
        clear(portfolioHost);
        portfolioHost.appendChild(ui.empty('无组合分配数据'));
        ctx.toast('AI 选股失败：' + e.message, 'err');
      } finally {
        st.loading = false;
        if (!st.destroyed) submitBtn.disabled = false;
      }
    }

    /* 提交：解析标的（代码直接识别，中文名走本地搜索接口）-> 组装 body -> 取数 */
    async function submit() {
      if (st.loading) return;
      const raw = readCodes();
      if (!raw.length) { ctx.toast('请先输入标的：代码或中文名，逗号 / 空格 / 换行分隔', 'warn'); return; }
      let tokens = raw;
      if (tokens.length > MAX_SYMBOLS) {
        tokens = tokens.slice(0, MAX_SYMBOLS);
        ctx.toast('单次最多分析 ' + MAX_SYMBOLS + ' 只，已截断为前 ' + MAX_SYMBOLS + ' 个', 'warn');
      }

      const list = [];
      const seen = {};
      const unknown = [];
      const nameTokens = [];
      const push = (x) => {
        const key = x.market + ':' + x.code;
        if (!x.code || seen[key]) return;
        seen[key] = 1;
        list.push(x);
      };
      tokens.forEach((t) => {
        const parsed = parseToken(t);
        if (parsed) push(parsed); else nameTokens.push(t);
      });

      if (nameTokens.length) {
        const probe = nameTokens.slice(0, 10);
        const hits = await Promise.all(probe.map((t) => api.search(t)
          .then((r) => (r.rows || [])[0] || null)
          .catch(() => null)));
        hits.forEach((hit, i) => {
          if (hit && hit.code) push({ code: hit.code, market: hit.market || 'cn', name: hit.name });
          else unknown.push(probe[i]);
        });
        nameTokens.slice(10).forEach((t) => unknown.push(t));
      }

      if (unknown.length) ctx.toast('未识别的输入：' + unknown.join('、'), 'warn');
      if (!list.length) { ctx.toast('没有识别出有效标的，请检查代码格式', 'err'); return; }
      if (st.destroyed) return;

      st.symbolMap = {};
      list.forEach((x) => { st.symbolMap[String(x.code).toUpperCase()] = { market: x.market, name: x.name || x.code }; });
      st.submitted = true;

      const p = readParams();
      const market = ctx.state.market;
      const off = list.filter((x) => x.market !== market);
      if (off.length) {
        ctx.toast('以下标的与当前市场（' + MARKET_LABEL[market] + '）不一致：' + off.map((x) => x.code).join('、') +
          '，已按代码自动判别市场', 'warn');
      }

      const body = {
        market,
        codes: list.map((x) => x.code),
        symbols: list.map((x) => ({ code: x.code, market: x.market })),
        horizon: p.horizon,
        capital: p.capital,
        kellyFraction: p.kellyFraction,
        maxWeight: p.maxWeight,
      };
      await load(body);
    }

    /* --------------------------------------------------------- 骨架 */

    const submitBtn = h('button', {
      class: 'btn primary sm', text: '开始 AI 分析',
      on: { click: () => submit() },
    });
    const refreshBtn = h('button', {
      class: 'btn sm', text: '刷新',
      on: {
        click: () => {
          if (!st.submitted) { ctx.toast('请先提交标的', 'warn'); return; }
          load(st.lastBody);
        },
      },
    });
    const autoSeg = ui.seg(
      [{ value: 'on', label: '自动刷新 60 秒' }, { value: 'off', label: '手动' }],
      'on',
      (v) => {
        st.auto = v === 'on';
        markSeg(autoSeg, st.auto ? 'on' : 'off', ['on', 'off']);
        ctx.toast(st.auto ? '已开启自动刷新（60 秒，仅在已提交标的后轮询）' : '已切换为手动刷新', 'info');
        if (st.auto && st.submitted && st.lastBody) load(st.lastBody);
      }
    );

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('AI 选股', '批量提交标的，一次拿到逐只研判结论与凯利仓位建议：建议档位 / 评分 / 置信度 / 凯利仓位 / 预测 / 关键因子 / 风险，并给出组合权重分配', [
        statHost,
        autoSeg,
        refreshBtn,
        submitBtn,
      ]),
      ui.section('模型说明与免责声明', '结论由统计模型给出，字段缺失时按「—」降级展示，不做任何推断填充', [], modelHost),
      ui.section('标的与参数', '标的支持代码（600519 / AAPL）与中文名，逗号 / 空格 / 换行分隔，也可从自选股导入', [], formHost),
      ui.section('研判结果', '点击行可打开个股详情；每行右侧可直接查看K线、加入自选或转为策略跟踪', [], tableHost),
      ui.section('组合分配', '按凯利折扣与单只权重上限折算的权重与金额分配', [], portfolioHost),
    ]));

    renderModel();
    renderDisclaimer();
    renderForm();
    renderCodeHint();
    renderTable();
    renderPortfolio();

    /* 自动刷新：默认 60 秒，仅在提交过标的后轮询 */
    timer = setInterval(() => {
      if (!st.auto || !st.submitted || st.loading) return;
      if (st.destroyed || !root.isConnected) return;
      load(st.lastBody);
    }, AUTO_MS);

    return {
      refresh() {
        if (!st.submitted) { ctx.toast('请先提交标的', 'warn'); return Promise.resolve(); }
        return load(st.lastBody);
      },
      destroy() {
        st.destroyed = true;
        if (timer) { clearInterval(timer); timer = null; }
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.advisor = { mount };
})();
