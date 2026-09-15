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

   历史记录（持久化）：
     接口：GET  /api/advisor/history?limit=&offset=&market=&code=&action=&q=&pinned=
            GET  /api/advisor/record?id=ar-...      单条记录全文（rows 与 recommend 同构，用于回放）
            GET  /api/advisor/review?id=ar-...&horizons=5,20
            POST /api/advisor/note   { id, note, pinned }
            POST /api/advisor/delete { id }
            POST /api/advisor/prune  { keep }
     约定：历史记录里的 advisor.forecast.path 已被裁剪为空数组（预测带锚在保存时的价格上，
          事后无意义），advisor.marks 与 advisor.plan 仍在；所有缺失字段一律降级为「—」。
          历史视图下暂停 60 秒自动刷新，避免把回放数据覆盖成实时结果。

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

  /* 凯利折扣数值 -> 中文档位（服务端只回数值，这里做展示映射） */
  const KELLY_LABEL = { '0.25': '¼凯利', '0.5': '半凯利', '0.75': '¾凯利', '1': '全凯利' };
  /* 记录来源（trigger）：仅用于展示 */
  const TRIGGER_LABEL = { list: 'AI选股', detail: '个股研判', auto: '自动' };
  /* 复盘判定 -> chip 外观 / 文案 */
  const VERDICT_CLS = { hit: 'chip up', miss: 'chip down', pending: 'chip', neutral: 'chip', nodata: 'chip warn' };
  const VERDICT_LABEL = { hit: '命中', miss: '未命中', pending: '未到期', neutral: '中性不计', nodata: '无数据' };
  /* 复盘口径兜底文案（服务端 note 缺失时使用） */
  const REVIEW_NOTE = '这是事后回看，样本少且不含手续费与滑点，不代表未来表现';
  const HIST_LIMIT = 50;        /* 历史记录单次拉取条数 */
  const PRUNE_ROUNDS = 5;       /* 「清空全部」最多循环轮次（服务端可能按保留策略分批清理） */

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

  /* 毫秒时间戳 -> YYYY-MM-DD HH:MM:SS（服务端 timeText 缺失时的兜底展示） */
  function dateTimeText(ts) {
    if (!isNum(ts)) return '—';
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* 记录时间：优先服务端 timeText，其次 createdAt 时间戳，最后 createdDate */
  function recTimeText(rec) {
    if (!rec) return '—';
    if (rec.timeText) return String(rec.timeText);
    if (isNum(rec.createdAt)) return dateTimeText(rec.createdAt);
    return text(rec.createdDate, '—');
  }

  /* 凯利折扣展示：数值 -> 中文档位，未知数值原样输出，缺失降级「—」 */
  function kellyText(v) {
    if (!isNum(v)) return '—';
    return KELLY_LABEL[String(v)] || F.num(v, 2);
  }

  /* 未识别数 = symbolCount - analyzed；任一缺失则为 null（展示「—」，不臆造） */
  function unparsedCount(r) {
    const a = r && r.symbolCount;
    const b = r && r.analyzed;
    if (!isNum(a) || !isNum(b)) return null;
    return Math.max(0, a - b);
  }

  /* 建议分布 chips：买 X 增 X 持 X 减 X 卖 X（只用已有 chip 配色，缺字段跳过） */
  function actionChips(actions) {
    const a = actions || {};
    const keys = ['buy', 'add', 'hold', 'reduce', 'sell'];
    const box = h('span', { style: { display: 'inline-flex', gap: '4px', flexWrap: 'wrap' } });
    let any = false;
    keys.forEach((k) => {
      if (!isNum(a[k])) return;
      any = true;
      box.appendChild(h('span', {
        class: ACTION_CLS[k] || 'chip',
        title: ACTION_LABEL[k] + ' ' + a[k] + ' 只',
        text: ACTION_LABEL[k].slice(0, 1) + ' ' + a[k],
      }));
    });
    return any ? box : dash();
  }

  /* topRows（最多 3 只，可操作性优先）作为悬停提示：不占列宽，但信息可达 */
  function topRowsTip(r) {
    const list = (r && Array.isArray(r.topRows)) ? r.topRows : [];
    if (!list.length) return '';
    return list.map((x) => {
      const w = asPct(x.kellyWeight);
      return text(x.name, text(x.code, '?')) + ' ' + text(x.code, '') +
        ' · ' + (ACTION_LABEL[String(x.action || '').toLowerCase()] || text(x.actionText, '—')) +
        ' · 评分 ' + F.num(x.score, 1) +
        (isNum(w) ? ' · 权重 ' + F.num(w, 1) + '%' : '');
    }).join('\n');
  }

  /* codes（该记录内出现的标的）作为悬停提示 */
  function codesTip(r) {
    const list = (r && Array.isArray(r.codes)) ? r.codes : [];
    if (!list.length) return '';
    return '含：' + list.map((c) => text(c.name, text(c.code, '?'))).join('、');
  }

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
      /* 保存开关：默认开启，提交时把 save / trigger 一起带给服务端 */
      save: true,
      /* 历史记录（列表 / 统计 / 保留策略 / 筛选条件） */
      hist: {
        rows: [], stats: null, retention: null, note: '', total: 0,
        loading: false, error: '',
        filter: { market: '', action: '', q: '', pinned: false },
      },
      /* 历史视图：非 null 时主表展示的是该历史记录（自动刷新暂停） */
      history: null,
      /* 进入历史视图前的实时结果快照，退出历史视图时恢复 */
      live: null,
      /* 复盘面板数据（null 表示未展开） */
      review: null,
    };
    let timer = null;

    const statHost = h('span', { class: 'hint', text: '待提交标的' });
    const formHint = h('span', { class: 'dim3', text: '已输入 0 个标的' });
    const modelHost = h('div');
    const disclaimerHost = h('div');
    const formHost = h('div');
    const tableHost = h('div');
    const portfolioHost = h('div');
    /* 历史视图提示条（在「研判结果」区块上方） */
    const bannerHost = h('div', { style: { display: 'none' } });
    /* 历史记录区块：工具条 / 统计摘要 / 记录表 */
    const histBarHost = h('div');
    const histStatsHost = h('div', { class: 'legend-inline', style: { margin: '2px 0 10px', lineHeight: '1.8' } });
    const histTableHost = h('div');
    /* 复盘回看区块 */
    const reviewHost = h('div');

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

    /* 保存开关：默认开启，提交时随请求体带上 save=true / trigger='list' */
    const saveToggle = h('button', {
      class: 'btn ghost sm active', text: '自动保存到历史记录',
      title: '开启后每次「开始 AI 分析」的结果都会落库，可在下方「历史记录」中回放与复盘',
      on: {
        click: (e) => {
          st.save = !st.save;
          e.currentTarget.classList.toggle('active', st.save);
          ctx.toast(st.save
            ? '已开启：每次分析结果自动保存到历史记录'
            : '已关闭：本次分析结果不写入历史记录', 'info');
        },
      },
    });

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
        h('div', { class: 'field' }, [h('label', { text: '历史记录' }), saveToggle]),
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
        tableHost.appendChild(ui.empty(st.history
          ? '该历史记录没有逐只研判明细（记录 #' + text(st.history.id) + '）'
          : (st.submitted
            ? '服务端未返回任何标的的研判结果，请检查代码是否正确或稍后重试'
            : '在上方填写标的（代码或中文名）后点击「开始 AI 分析」')));
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
      /* 历史视图下本金 / 单只上限取自历史记录本身，避免误用最近一次实时请求的参数 */
      const hp = st.history;
      const cap = hp && isNum(hp.capital) ? hp.capital
        : (st.lastBody && isNum(st.lastBody.capital) ? st.lastBody.capital : null);
      const mw = hp && isNum(hp.maxWeight) ? hp.maxWeight
        : (st.lastBody && isNum(st.lastBody.maxWeight) ? st.lastBody.maxWeight : null);
      const mkt = (hp && hp.market) || ctx.state.market;
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
        ['单只上限', isNum(mw) ? F.num(asPct(mw), 1) + '%' : '—'],
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

    /* asSubmit 为 true 时才允许落库：自动刷新 / 手动刷新一律 save=false，
       否则 60 秒轮询会源源不断产生历史记录 */
    async function load(body, asSubmit) {
      const b = body || st.lastBody;
      if (!b) return;
      st.lastBody = b;
      const req = Object.assign({}, b, { save: !!asSubmit && b.save !== false });
      st.loading = true;
      submitBtn.disabled = true;
      statHost.textContent = '模型计算中…';
      clear(tableHost);
      tableHost.appendChild(ui.loading('模型计算中…（多标的批量研判可能需要数秒）'));
      try {
        const res = await recommend(req);
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
        /* 服务端已落库：提示记录号并刷新历史列表（saved 为 false 时不提示成功） */
        if (asSubmit && res.saved !== false && res.recordId) {
          statHost.textContent += ' · 已存 #' + res.recordId;
          ctx.toast('已保存记录 #' + res.recordId, 'ok');
          loadHistory();
        }
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
      /* 提交新的一次实时研判：先退出历史视图（主表即将被实时结果覆盖，避免状态混淆） */
      if (st.history) exitHistory(true);

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
        /* 历史记录：是否落库 + 来源标记（服务端按此写入记录） */
        save: st.save !== false,
        trigger: 'list',
      };
      await load(body, true);
    }

    /* ================================================= 历史记录（持久化） */

    /* 接口兜底：api.advisorXxx 未接入时直接走通用 get / post（与 recommend 同一套思路） */
    function historyApi(params) {
      if (typeof api.advisorHistory === 'function') return api.advisorHistory(params);
      if (typeof api.get === 'function') return api.get('advisor/history', params, { noDedupe: true });
      return Promise.reject(new Error('api.advisorHistory 未接入'));
    }
    function recordApi(id) {
      if (typeof api.advisorRecord === 'function') return api.advisorRecord(id);
      if (typeof api.get === 'function') return api.get('advisor/record', { id }, { noDedupe: true });
      return Promise.reject(new Error('api.advisorRecord 未接入'));
    }
    function reviewApi(id, horizons) {
      if (typeof api.advisorReview === 'function') return api.advisorReview(id, horizons);
      if (typeof api.get === 'function') {
        return api.get('advisor/review', { id, horizons: (horizons || []).join(',') }, { noDedupe: true });
      }
      return Promise.reject(new Error('api.advisorReview 未接入'));
    }
    function deleteApi(id) {
      if (typeof api.advisorDelete === 'function') return api.advisorDelete(id);
      if (typeof api.post === 'function') return api.post('advisor/delete', { id });
      return Promise.reject(new Error('api.advisorDelete 未接入'));
    }
    function noteApi(id, note, pinned) {
      if (typeof api.advisorNote === 'function') return api.advisorNote(id, note, pinned);
      if (typeof api.post === 'function') {
        return api.post('advisor/note', pinned === undefined ? { id, note } : { id, note, pinned });
      }
      return Promise.reject(new Error('api.advisorNote 未接入'));
    }
    function pruneApi(keep) {
      if (typeof api.advisorPrune === 'function') return api.advisorPrune(keep);
      if (typeof api.post === 'function') return api.post('advisor/prune', { keep });
      return Promise.reject(new Error('api.advisorPrune 未接入'));
    }

    function histMarket(rec) {
      return (rec && rec.market) || ctx.state.market;
    }

    /* ---- 历史视图提示条 ---- */

    function renderBanner() {
      clear(bannerHost);
      if (!st.history) { bannerHost.style.display = 'none'; return; }
      bannerHost.style.display = '';
      const rec = st.history;
      const mkt = histMarket(rec);
      const args = [];
      if (isNum(rec.horizon)) args.push('h=' + rec.horizon);
      if (isNum(rec.capital)) args.push('本金 ' + F.amt(rec.capital, mkt));
      if (isNum(rec.kellyFraction)) args.push(kellyText(rec.kellyFraction));
      if (isNum(rec.maxWeight)) args.push('上限 ' + F.num(asPct(rec.maxWeight), 0) + '%');
      bannerHost.appendChild(h('div', {
        class: 'legend-inline',
        style: {
          alignItems: 'center', gap: '10px', marginBottom: '8px', padding: '8px 10px',
          border: '1px solid var(--accent-line)', borderRadius: '6px', background: 'var(--accent-soft)',
        },
      }, [
        h('span', { class: 'chip accent', text: '历史记录' }),
        h('span', {
          text: '正在查看历史记录 #' + text(rec.id, '—') + '（保存于 ' + recTimeText(rec) +
            (args.length ? ' · 参数 ' + args.join(' · ') : '') + '）',
        }),
        h('span', { class: 'chip warn', text: '自动刷新已暂停' }),
        h('button', {
          class: 'btn ghost sm', text: '退出历史视图',
          title: '回到最近一次实时研判结果（若无则回到空态）',
          on: { click: () => exitHistory() },
        }),
      ]));
      bannerHost.appendChild(h('div', { class: 'legend-inline', style: { marginBottom: '8px', lineHeight: '1.8' } }, [
        h('span', {
          class: 'dim3',
          text: '历史记录按保存时的结果原样回放：字段缺失一律显示「—」，不做推断填充；' +
            '为避免 60 秒自动刷新把历史数据覆盖成实时结果，历史视图下已暂停轮询，点「退出历史视图」即可恢复。',
        }),
        rec.note ? h('span', { class: 'dim3', text: '备注：' + text(rec.note) }) : null,
      ]));
    }

    /* ---- 历史记录表 ---- */

    function histFilterParams() {
      const f = st.hist.filter;
      const p = { limit: HIST_LIMIT, offset: 0 };
      if (f.market) p.market = f.market;
      if (f.action) p.action = f.action;
      if (f.q) p.q = f.q;
      if (f.pinned) p.pinned = '1';
      return p;
    }

    function renderHistStats() {
      clear(histStatsHost);
      const s = st.hist.stats || {};
      const total = isNum(s.records) ? s.records : (isNum(st.hist.total) ? st.hist.total : st.hist.rows.length);
      const parts = [
        '记录 ' + total + ' 条',
        '标的 ' + text(s.items, '—') + ' 个',
        '买入/增持 ' + text(s.buyTotal, '—') + ' 次',
        '平均总仓位 ' + (isNum(s.avgTotalWeight) ? F.num(asPct(s.avgTotalWeight), 1) + '%' : '—'),
        '最近 ' + (isNum(s.latestAt) ? dateTimeText(s.latestAt) : '—'),
      ];
      const ret = st.hist.retention || {};
      if (isNum(ret.limit)) parts.push('保留上限 ' + ret.limit + ' 条 · 已清理 ' + text(ret.pruned, '0') + ' 条');
      histStatsHost.appendChild(h('span', { class: 'dim3', text: parts.join(' · ') }));
      if (st.hist.note) histStatsHost.appendChild(h('span', { class: 'dim3', text: st.hist.note }));
    }

    function noteCell(r) {
      const cur = r.note === null || r.note === undefined ? '' : String(r.note);
      const inp = h('input', {
        class: 'inp', value: cur, placeholder: '备注（回车保存）',
        title: '输入备注后按回车，或点右侧「保存」按钮保存到服务端',
      });
      const btn = h('button', {
        class: 'btn ghost sm', text: '保存', disabled: true,
        title: '把备注保存到服务端（未改动时不可点）',
      });
      /* 未改动时按钮禁用：避免把「没改」当成一次写入，也让用户看清当前是否已保存 */
      const sync = () => {
        const changed = String(inp.value || '') !== String(r.note || '');
        btn.disabled = !changed;
        btn.classList.toggle('active', changed);
      };
      const submit = () => {
        const v = String(inp.value || '');
        if (v === String(r.note || '')) return Promise.resolve();
        return saveNote(r, v).then(sync);
      };
      /* 行内编辑：阻断冒泡，避免触发行点击 */
      inp.addEventListener('click', (e) => e.stopPropagation());
      inp.addEventListener('input', sync);
      inp.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') submit();
      });
      /* 失焦也提交一次（点表格空白处等场景）。
         注意不能只依赖失焦：**macOS 上点按钮不会让输入框失焦**，用户点「刷新 / 置顶」
         时输入框仍持有焦点，备注就会一直留在输入框里没落库（本项目在跟踪页表单上
         踩过同一个坑）。因此显式「保存」按钮才是主路径，失焦只是补充。 */
      inp.addEventListener('blur', () => { submit(); });
      btn.addEventListener('click', (e) => { e.stopPropagation(); submit(); });
      sync();
      return h('div', { style: { display: 'flex', gap: '5px', alignItems: 'center' } }, [inp, btn]);
    }

    function buildHistCols() {
      return [
        {
          key: 'createdAt', label: '时间', noSort: true, width: '200px', value: (r) => r.createdAt,
          render: (r) => h('span', { class: 'num', title: '记录 #' + text(r.id, '—') }, [
            h('span', { text: recTimeText(r) }),
            r.pinned ? h('span', { class: 'chip accent', text: '置顶' }) : null,
            h('span', { class: 'chip', text: TRIGGER_LABEL[String(r.trigger || '')] || text(r.trigger, '—') }),
            h('span', { class: 'code', text: '#' + text(r.id, '—') }),
          ]),
        },
        {
          key: 'symbolCount', label: '标的数', cls: 'n', value: (r) => r.symbolCount,
          render: (r) => h('span', {
            class: 'num' + (isNum(r.symbolCount) ? '' : ' dim3'),
            title: '提交标的数 / 已识别数' + (codesTip(r) ? ' · ' + codesTip(r) : ''),
            text: isNum(r.symbolCount) ? r.symbolCount + ' 只（已识别 ' + text(r.analyzed, '—') + '）' : '—',
          }),
        },
        {
          key: 'params', label: '参数', noSort: true, width: '218px',
          render: (r) => h('span', {
            class: 'num', title: '预测窗口 / 本金 / 凯利折扣 / 单只权重上限',
            text: 'h=' + text(r.horizon, '—') + ' · ' + (isNum(r.capital) ? F.amt(r.capital, histMarket(r)) : '—') +
              ' · ' + kellyText(r.kellyFraction) +
              ' · 上限 ' + (isNum(r.maxWeight) ? F.num(asPct(r.maxWeight), 0) + '%' : '—'),
          }),
        },
        {
          key: 'actions', label: '建议分布', noSort: true, width: '188px',
          render: (r) => h('span', {
            title: topRowsTip(r) || '服务端未返回明细标的（topRows）',
            style: { display: 'inline-flex' },
          }, [actionChips(r.actions)]),
        },
        {
          key: 'totalWeight', label: '总仓位', cls: 'n', value: (r) => asPct(r.totalWeight),
          render: (r) => {
            const w = asPct(r.totalWeight);
            return h('span', { class: 'num' + (isNum(w) ? '' : ' dim3'), text: isNum(w) ? F.num(w, 1) + '%' : '—' });
          },
        },
        {
          key: 'unparsed', label: '未识别', cls: 'n', value: (r) => unparsedCount(r),
          render: (r) => {
            const n = unparsedCount(r);
            if (n === null) return dash();
            if (n > 0) return h('span', { class: 'chip warn', title: '提交了但未识别出代码的标的数', text: String(n) });
            return h('span', { class: 'num dim3', text: '0' });
          },
        },
        { key: 'note', label: '备注', noSort: true, width: '190px', render: (r) => noteCell(r) },
        {
          key: 'act', label: '操作', noSort: true, width: '232px',
          render: (r) => h('div', { style: { display: 'flex', gap: '5px', flexWrap: 'wrap' } }, [
            h('button', {
              class: 'btn ghost sm', text: '载入', title: '把该记录的结果回填到上方主表与组合分配区',
              on: { click: (e) => { e.stopPropagation(); loadRecord(r.id); } },
            }),
            h('button', {
              class: 'btn ghost sm', text: '复盘', title: '回看保存时点之后的实际表现（5 / 20 根K线）',
              on: { click: (e) => { e.stopPropagation(); loadReview(r.id); } },
            }),
            h('button', {
              class: 'btn ghost sm', text: r.pinned ? '取消置顶' : '置顶',
              on: { click: (e) => { e.stopPropagation(); togglePin(r); } },
            }),
            h('button', {
              class: 'btn ghost sm', text: '删除',
              on: { click: (e) => { e.stopPropagation(); removeRecord(r); } },
            }),
          ]),
        },
      ];
    }

    function renderHistory() {
      clear(histTableHost);
      if (st.hist.loading) { histTableHost.appendChild(ui.loading('历史记录加载中…')); return; }
      if (st.hist.error) {
        histTableHost.appendChild(ui.empty('历史记录暂不可用：' + st.hist.error + '（接口 /api/advisor/history）'));
        return;
      }
      if (!st.hist.rows.length) {
        const f = st.hist.filter;
        const filtered = !!(f.market || f.action || f.q || f.pinned);
        histTableHost.appendChild(ui.empty(filtered
          ? '没有符合筛选条件的历史记录：可放宽筛选或点「刷新」重试'
          : '暂无历史记录：保持「自动保存到历史记录」开启，提交一次「开始 AI 分析」后即会出现在这里'));
        return;
      }
      histTableHost.appendChild(ui.tbl({
        cols: buildHistCols(),
        rows: st.hist.rows,
        compact: true,
        maxHeight: '340px',
        rowKey: (r) => r.id,
        activeKey: st.history ? st.history.id : null,
        emptyText: '没有符合条件的历史记录',
      }));
    }

    /* 历史列表请求序号：筛选条件变化时允许并发，只采用最后一次请求的结果 */
    let histSeq = 0;

    async function loadHistory() {
      const seq = ++histSeq;
      st.hist.loading = true;
      renderHistory();
      try {
        const res = await historyApi(histFilterParams());
        if (st.destroyed || seq !== histSeq) return;
        if (!res || res.ok === false) {
          throw new Error((res && (res.message || res.error)) || '服务端未返回有效结果');
        }
        st.hist.rows = Array.isArray(res.rows) ? res.rows : [];
        st.hist.stats = res.stats || null;
        st.hist.retention = res.retention || null;
        st.hist.note = res.note || '';
        st.hist.total = isNum(res.total) ? res.total : st.hist.rows.length;
        st.hist.error = '';
      } catch (e) {
        if (st.destroyed || seq !== histSeq) return;
        st.hist.rows = [];
        st.hist.stats = null;
        st.hist.retention = null;
        st.hist.error = e.message;
        ctx.toast('历史记录获取失败：' + e.message, 'err');
      } finally {
        /* 已有更新的请求在途时，状态交给它收尾 */
        if (seq === histSeq && !st.destroyed) {
          st.hist.loading = false;
          renderHistStats();
          renderHistory();
        }
      }
    }

    /* ---- 行内操作：载入 / 置顶 / 备注 / 删除 ---- */

    async function loadRecord(id) {
      if (!id) return;
      clear(bannerHost);
      bannerHost.style.display = '';
      bannerHost.appendChild(ui.loading('历史记录载入中…（#' + id + '）'));
      try {
        const res = await recordApi(id);
        if (st.destroyed) return;
        const rec = res && res.record;
        if (!res || res.ok === false || !rec) {
          throw new Error((res && (res.message || res.error)) || '服务端未返回记录内容');
        }
        /* 首次进入历史视图：把当前实时结果留一份，退出时可恢复 */
        if (!st.history) {
          st.live = {
            rows: st.rows, portfolio: st.portfolio, disclaimer: st.disclaimer,
            submitted: st.submitted, stated: statHost.textContent,
          };
        }
        st.history = rec;
        st.rows = Array.isArray(rec.rows) ? rec.rows : [];
        st.portfolio = rec.portfolio || null;
        st.disclaimer = rec.disclaimer || st.disclaimer;
        st.symbolMap = {};
        st.rows.forEach((r) => {
          if (r && r.code) {
            st.symbolMap[String(r.code).toUpperCase()] = {
              market: r.market || rec.market || ctx.state.market, name: r.name || r.code,
            };
          }
        });
        st.submitted = true;      /* 让主表与组合分配区渲染内容而不是空态 */
        renderBanner();
        renderTable();
        renderPortfolio();
        renderHistory();          /* 高亮当前记录 */
        const mkt = histMarket(rec);
        const args = [];
        if (isNum(rec.horizon)) args.push('h=' + rec.horizon);
        if (isNum(rec.capital)) args.push('本金 ' + F.amt(rec.capital, mkt));
        statHost.textContent = '历史记录 #' + text(rec.id, id) + ' · ' + recTimeText(rec) +
          (args.length ? ' · ' + args.join(' · ') : '') + ' · 自动刷新已暂停';
        ctx.toast('已载入历史记录 #' + text(rec.id, id) + '（自动刷新已暂停）', 'ok');
      } catch (e) {
        if (st.destroyed) return;
        ctx.toast('历史记录载入失败：' + e.message, 'err');
        if (st.history) renderBanner();
        else {
          bannerHost.style.display = 'none';
          clear(tableHost);
          tableHost.appendChild(ui.empty('历史记录载入失败：' + e.message + '（接口 /api/advisor/record）'));
        }
      }
    }

    /* 退出历史视图：恢复最近一次实时结果，若无则回到空态 */
    function exitHistory(silent) {
      if (!st.history) return;
      st.history = null;
      const live = st.live;
      st.live = null;
      if (live) {
        st.rows = live.rows;
        st.portfolio = live.portfolio;
        st.disclaimer = live.disclaimer;
        st.submitted = live.submitted;
        statHost.textContent = live.stated;
      } else {
        st.rows = [];
        st.portfolio = null;
        st.submitted = false;
        statHost.textContent = '待提交标的';
      }
      renderBanner();
      renderTable();
      renderPortfolio();
      renderHistory();
      if (!silent) {
        ctx.toast(live && live.submitted ? '已退出历史视图，恢复最近一次实时结果' : '已退出历史视图（暂无实时结果）', 'info');
      }
    }

    /* 只提交本次真正要改的字段 —— 服务端对缺失字段的语义是「保持不变」。
       曾经这里两个调用点都是 note 与 pinned 一起发（当时的注释写着「避免只改备注把
       置顶清掉」），结果反而制造了覆盖：同一行里输入备注后马上点「置顶」时，
       备注框先 blur 提交新备注、紧接着置顶请求带着**旧的**空 note 把刚存的备注冲掉，
       实测表现为「填了备注却存成空」。只发改动字段后两个请求互不干扰。 */
    async function saveNote(r, note) {
      const prev = r.note;
      r.note = note;                     /* 乐观更新，失败再回滚 */
      try {
        const res = await noteApi(r.id, note);
        if (st.destroyed) return;
        if (res && res.note !== undefined && res.note !== null) r.note = res.note;
        if (res && res.pinned !== undefined) r.pinned = !!res.pinned;
        ctx.toast('备注已保存（记录 #' + text(r.id) + '）', 'ok');
      } catch (e) {
        if (st.destroyed) return;
        r.note = prev;
        renderHistory();
        ctx.toast('备注保存失败：' + e.message, 'err');
      }
    }

    async function togglePin(r) {
      const want = !r.pinned;
      try {
        const res = await noteApi(r.id, undefined, want);
        if (st.destroyed) return;
        r.pinned = res && res.pinned !== undefined ? !!res.pinned : want;
        if (res && res.note !== undefined && res.note !== null) r.note = res.note;
        if (st.history && st.history.id === r.id) st.history.pinned = r.pinned;
        renderHistory();
        ctx.toast(r.pinned ? '已置顶记录 #' + text(r.id) : '已取消置顶 #' + text(r.id), 'ok');
      } catch (e) {
        ctx.toast('置顶操作失败：' + e.message, 'err');
      }
    }

    async function removeRecord(r) {
      if (!window.confirm('确认删除历史记录 #' + text(r.id) + '（' + recTimeText(r) + '）？该操作不可恢复。')) return;
      try {
        const res = await deleteApi(r.id);
        if (st.destroyed) return;
        const left = res && isNum(res.remaining) ? res.remaining : null;
        ctx.toast('已删除记录 #' + text(r.id) + (left === null ? '' : '（剩余 ' + left + ' 条）'), 'ok');
        if (st.history && st.history.id === r.id) exitHistory(true);
        if (st.review && st.review.id === r.id) { st.review = null; renderReview(); }
        await loadHistory();
      } catch (e) {
        ctx.toast('删除失败：' + e.message, 'err');
      }
    }

    /* keep=0 清空全部：服务端按保留策略可能分批删除，这里最多循环 PRUNE_ROUNDS 轮 */
    async function pruneAll() {
      const expected = isNum(st.hist.stats && st.hist.stats.records) ? st.hist.stats.records
        : (isNum(st.hist.total) ? st.hist.total : st.hist.rows.length);
      if (!window.confirm('将删除全部历史记录' + (expected ? '（当前 ' + expected + ' 条）' : '') +
        '，且不可恢复，确认继续？')) return;
      histPruneBtn.disabled = true;
      let deleted = 0;
      let remaining = null;
      try {
        for (let i = 0; i < PRUNE_ROUNDS; i++) {
          const res = await pruneApi(0);
          if (st.destroyed) return;
          if (!res || res.ok === false) {
            throw new Error((res && (res.message || res.error)) || '服务端未返回有效结果');
          }
          deleted += isNum(res.deleted) ? res.deleted : 0;
          remaining = isNum(res.remaining) ? res.remaining : null;
          if (remaining === null || remaining <= 0) break;
        }
        if (remaining !== null && remaining > 0) {
          ctx.toast('已清理 ' + deleted + ' 条，仍有 ' + remaining + ' 条未清理（服务端保留策略未允许全部删除）', 'warn');
        } else if (expected && deleted < expected) {
          ctx.toast('已清理 ' + deleted + ' 条（少于当前记录的 ' + expected + ' 条）', 'warn');
        } else {
          ctx.toast('已清空全部历史记录' + (deleted ? '（' + deleted + ' 条）' : ''), 'ok');
        }
        if (st.history) exitHistory(true);
        if (st.review) { st.review = null; renderReview(); }
        await loadHistory();
      } catch (e) {
        if (st.destroyed) return;
        ctx.toast('清空失败：' + e.message, 'err');
      } finally {
        if (!st.destroyed) histPruneBtn.disabled = false;
      }
    }

    /* ================================================= 复盘回看 */

    function verdictChip(v) {
      const k = String(v || '').toLowerCase();
      return h('span', {
        class: VERDICT_CLS[k] || 'chip',
        title: '命中判定由服务端按保存价与最新价给出',
        text: VERDICT_LABEL[k] || text(v, '—'),
      });
    }

    /* 前瞻窗口单元格：未到期 / 无数据一律显示「—」，不臆造收益 */
    function fwdCell(fwd, key) {
      const f = (fwd || {})[key] || {};
      if (f.ready === false || !isNum(f.ret)) {
        return h('span', {
          class: 'num dim3',
          title: f.ready === false ? ('尚未到期' + (f.date ? '（到期日 ' + f.date + '）' : '')) : '该窗口暂无数据',
          text: '—',
        });
      }
      return h('span', {
        class: 'num ' + F.dir(f.ret),
        title: (f.date ? '到期 ' + f.date + ' · ' : '') + (f.hit === true ? '命中' : (f.hit === false ? '未命中' : '中性')),
      }, [
        h('span', { text: F.pct(f.ret) }),
        h('span', { class: 'dim3', text: f.hit === true ? ' ✓' : (f.hit === false ? ' ✗' : '') }),
      ]);
    }

    function reviewMetrics(s) {
      const hr = asPct(s.hitRate);
      const bull = asPct(s.bullHitRate);
      const bear = asPct(s.bearHitRate);
      const tw = asPct(s.totalWeight);
      return metricList([
        ['回看标的', isNum(s.total) ? s.total + ' 只' : '—'],
        ['已到期 / 未到期', (isNum(s.ready) ? s.ready : '—') + ' / ' + (isNum(s.pending) ? s.pending : '—')],
        ['命中 / 未命中', (isNum(s.hits) ? s.hits : '—') + ' / ' + (isNum(s.misses) ? s.misses : '—')],
        ['命中率', isNum(hr) ? F.num(hr, 1) + '%' : '—', isNum(hr) ? (hr >= 50 ? 'up' : 'down') : ''],
        ['中性不计', isNum(s.neutral) ? s.neutral + ' 只' : '—'],
        ['看多组命中率', isNum(bull) ? F.num(bull, 1) + '%' : '—'],
        ['看空组命中率', isNum(bear) ? F.num(bear, 1) + '%' : '—'],
        ['平均收益', F.pct(s.avgReturn), F.dir(s.avgReturn)],
        ['命中组平均', F.pct(s.avgHitReturn), F.dir(s.avgHitReturn)],
        ['未命中组平均', F.pct(s.avgMissReturn), F.dir(s.avgMissReturn)],
        ['已建仓加权收益', F.pct(s.positionReturn), F.dir(s.positionReturn)],
        ['占本金收益', F.pct(s.accountReturn), F.dir(s.accountReturn)],
        ['总仓位', isNum(tw) ? F.num(tw, 1) + '%' : '—'],
      ]);
    }

    function renderReview() {
      clear(reviewHost);
      const rv = st.review;
      if (!rv) {
        reviewHost.appendChild(ui.empty('在「历史记录」中点「复盘」，回看保存时点之后的实际表现（再次点击可收起）'));
        return;
      }
      if (rv.loading) { reviewHost.appendChild(ui.loading('复盘计算中…（需要拉取保存至今的K线）')); return; }
      if (rv.error) {
        reviewHost.appendChild(ui.empty('复盘数据不可用：' + rv.error + '（接口 /api/advisor/review）'));
        return;
      }
      const s = rv.summary || {};
      const rows = Array.isArray(rv.rows) ? rv.rows : [];

      reviewHost.appendChild(h('div', { class: 'legend-inline', style: { alignItems: 'center', gap: '10px', marginBottom: '8px' } }, [
        h('span', { class: 'chip accent', text: '事后回看' }),
        h('span', { text: '记录 #' + text(rv.id, '—') + ' · 回看基准日 ' + text(rv.asOf, '—') }),
        h('span', { class: 'chip warn', text: '不含手续费 / 滑点' }),
      ]));
      reviewHost.appendChild(h('div', { class: 'legend-inline', style: { marginBottom: '10px', lineHeight: '1.8' } }, [
        h('span', { class: 'chip warn', text: '重要提示' }),
        h('span', { class: 'dim3', text: text(rv.note, REVIEW_NOTE) }),
      ]));
      reviewHost.appendChild(reviewMetrics(s));

      if (!rows.length) {
        reviewHost.appendChild(h('div', { style: { marginTop: '12px' } }, [
          ui.empty('该记录没有可回看的标的明细'),
        ]));
        return;
      }
      reviewHost.appendChild(h('div', { style: { marginTop: '12px' } }, [
        ui.tbl({
          cols: [
            {
              key: 'name', label: '标的', cls: 'name', noSort: true,
              render: (r) => {
                const nm = text(r.name, text(r.code));
                const same = String(nm) === String(r.code);
                return h('span', {}, [
                  h('span', { class: 'name', text: nm }),
                  same ? null : h('span', { class: 'code', text: (r.market === 'us' ? 'US:' : '') + text(r.code) }),
                ]);
              },
            },
            {
              key: 'action', label: '建议', noSort: true, width: '92px',
              render: (r) => {
                const a = String(r.action || '').toLowerCase();
                return h('span', {
                  class: ACTION_CLS[a] || 'chip',
                  title: text(r.actionText, ''),
                  text: ACTION_LABEL[a] || text(r.actionText, '—'),
                });
              },
            },
            {
              key: 'weight', label: '权重', cls: 'n', noSort: true,
              render: (r) => {
                const w = asPct(r.weight);
                return h('span', { class: 'num' + (isNum(w) ? '' : ' dim3'), text: isNum(w) ? F.num(w, 1) + '%' : '—' });
              },
            },
            {
              key: 'savedPrice', label: '保存价', cls: 'n', noSort: true,
              render: (r) => h('span', {
                class: 'num',
                title: '保存于 ' + text(r.savedAt, text(r.baseDate, '—')),
                text: F.price(r.savedPrice, r.market || ctx.state.market),
              }),
            },
            {
              key: 'lastPrice', label: '最新价', cls: 'n', noSort: true,
              render: (r) => h('span', { class: 'num', text: F.price(r.lastPrice, r.market || ctx.state.market) }),
            },
            {
              key: 'barsElapsed', label: '经过K线', cls: 'n', noSort: true,
              render: (r) => h('span', {
                class: 'num' + (isNum(r.barsElapsed) ? '' : ' dim3'),
                title: '保存时点至今经过的K线根数',
                text: isNum(r.barsElapsed) ? r.barsElapsed + ' 根' : '—',
              }),
            },
            {
              key: 'sinceReturn', label: '至今涨跌', cls: 'n', value: (r) => r.sinceReturn,
              render: (r) => h('span', { class: 'num ' + F.dir(r.sinceReturn), text: F.pct(r.sinceReturn) }),
            },
            { key: 'fwd5', label: '5 根', cls: 'n', noSort: true, render: (r) => fwdCell(r.fwd, '5') },
            { key: 'fwd20', label: '20 根', cls: 'n', noSort: true, render: (r) => fwdCell(r.fwd, '20') },
            {
              key: 'contribution', label: '账户贡献', cls: 'n', noSort: true,
              render: (r) => (isNum(r.contribution)
                ? h('span', {
                  class: 'num ' + F.dir(r.contribution),
                  title: '权重 × 至今涨跌（账户口径）',
                  text: F.pct(r.contribution),
                })
                : dash()),
            },
            { key: 'verdict', label: '判定', noSort: true, width: '96px', render: (r) => verdictChip(r.verdict) },
          ],
          rows,
          compact: true,
          maxHeight: '340px',
          emptyText: '无可回看标的',
        }),
      ]));
    }

    async function loadReview(id) {
      if (!id) return;
      /* 再次点击同一条记录：收起复盘面板 */
      if (st.review && st.review.id === id && !st.review.error && !st.review.loading) {
        st.review = null;
        renderReview();
        return;
      }
      st.review = { id, loading: true };
      renderReview();
      try {
        const res = await reviewApi(id, ['5', '20']);
        if (st.destroyed) return;
        if (!res || res.ok === false) {
          throw new Error((res && (res.message || res.error)) || '服务端未返回有效结果');
        }
        st.review = res;
      } catch (e) {
        if (st.destroyed) return;
        st.review = { id, error: e.message };
        ctx.toast('复盘失败：' + e.message, 'err');
      } finally {
        if (!st.destroyed) renderReview();
      }
    }

    /* ---- 历史记录工具条（只构建一次，保证输入框焦点不被刷新打断） ---- */

    const histMarketSeg = ui.seg(
      [{ value: '', label: '全部' }, { value: 'cn', label: 'A股' }, { value: 'us', label: '美股' }],
      '',
      (v) => {
        st.hist.filter.market = v;
        markSeg(histMarketSeg, v, ['', 'cn', 'us']);
        loadHistory();
      }
    );

    const histActionSel = h('select', {
      class: 'inp', title: '按建议档位筛选：服务端按记录内是否出现该档位过滤',
    }, [h('option', { value: '', text: '全部档位' })].concat(
      Object.keys(ACTION_LABEL).map((k) => h('option', { value: k, text: ACTION_LABEL[k] }))
    ));
    histActionSel.addEventListener('change', () => {
      st.hist.filter.action = histActionSel.value;
      loadHistory();
    });

    const histKeyword = h('input', {
      class: 'inp', placeholder: '代码或名称，如 600519 / 茅台',
      title: '按标的代码或名称筛选历史记录（回车生效）',
    });
    histKeyword.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      st.hist.filter.q = histKeyword.value.trim();
      loadHistory();
    });
    histKeyword.addEventListener('change', () => {
      const v = histKeyword.value.trim();
      if (v === st.hist.filter.q) return;
      st.hist.filter.q = v;
      loadHistory();
    });

    const histPinBtn = h('button', {
      class: 'btn ghost sm', text: '只看置顶', title: '只显示被置顶的历史记录',
      on: {
        click: () => {
          st.hist.filter.pinned = !st.hist.filter.pinned;
          histPinBtn.classList.toggle('active', st.hist.filter.pinned);
          loadHistory();
        },
      },
    });
    const histRefreshBtn = h('button', {
      class: 'btn ghost sm', text: '刷新', title: '重新拉取历史记录列表（历史列表不参与 60 秒轮询）',
      on: { click: () => loadHistory() },
    });
    const histPruneBtn = h('button', {
      class: 'btn ghost sm', text: '清空全部', title: 'POST /api/advisor/prune { keep: 0 }，不可恢复',
      on: { click: () => pruneAll() },
    });

    function renderHistBar() {
      clear(histBarHost);
      histBarHost.appendChild(h('div', { class: 'run-form' }, [
        h('div', { class: 'field' }, [h('label', { text: '市场' }), histMarketSeg]),
        h('div', { class: 'field' }, [h('label', { text: '建议档位' }), histActionSel]),
        h('div', { class: 'field wide' }, [h('label', { text: '关键词' }), histKeyword]),
        h('div', { class: 'field' }, [
          h('label', { text: '操作' }),
          h('div', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap' } }, [histPinBtn, histRefreshBtn, histPruneBtn]),
        ]),
      ]));
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
          /* 历史视图下不重新请求实时结果，避免把回放数据冲掉 */
          if (st.history) { ctx.toast('历史视图：仅刷新历史记录列表', 'info'); loadHistory(); return; }
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
        /* 历史视图下轮询被暂停：这里只记录用户意图，退出历史视图后立即生效 */
        if (st.history) {
          ctx.toast('当前为历史视图，自动刷新已暂停；退出历史视图后按此设置恢复', 'warn');
          return;
        }
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
      ui.section('研判结果', '点击行可打开个股详情；每行右侧可直接查看K线、加入自选或转为策略跟踪',
        [], h('div', {}, [bannerHost, tableHost])),
      ui.section('组合分配', '按凯利折扣与单只权重上限折算的权重与金额分配', [], portfolioHost),
      ui.section('历史记录', '每次分析可自动落库（默认开启）；支持筛选 / 备注 / 置顶 / 载入回放 / 事后复盘；该列表不参与 60 秒轮询',
        [], h('div', {}, [histBarHost, histStatsHost, histTableHost])),
      ui.section('复盘回看', '把历史记录的结论放到保存时点之后的真实行情里回看：命中率 / 平均收益 / 账户贡献',
        [], reviewHost),
    ]));

    renderModel();
    renderDisclaimer();
    renderForm();
    renderCodeHint();
    renderTable();
    renderPortfolio();
    renderBanner();
    renderHistBar();
    renderHistStats();
    renderHistory();
    renderReview();
    loadHistory();          /* 进入视图时拉取一次；之后仅在手动刷新 / 保存成功后刷新 */

    /* 自动刷新：默认 60 秒，仅在提交过标的后轮询；历史视图下整体暂停 */
    timer = setInterval(() => {
      if (st.history) return;                                   /* 历史视图：不轮询，避免覆盖历史数据 */
      if (!st.auto || !st.submitted || st.loading) return;
      if (st.destroyed || !root.isConnected) return;
      load(st.lastBody);
    }, AUTO_MS);

    return {
      refresh() {
        /* 历史视图下不重新请求实时结果（否则会把回放数据冲掉），只刷新历史列表 */
        if (st.history) { ctx.toast('历史视图：仅刷新历史记录列表', 'info'); return loadHistory(); }
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
