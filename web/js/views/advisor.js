/* ==========================================================================
   视图 · AI 选股（多标的批量研判：建议 / 凯利仓位 / 组合分配）

   接口：POST /api/advisor/recommend（由主程接入 api.advisorRecommend）
   请求体：{ market, codes: [...], symbols: [{ code, market, name }],
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

   标的识别（服务端识别层，前端只调用、不自建规则）：
     POST /api/symbols/resolve  { market, tokens: [...], limit: 5, max: 30 }
     返回：{ ok, market, items: [{ raw, kind: code|name|ambiguous|unknown,
                                  code, market, name,
                                  hits: [{ code, name, market, tier, score, source,
                                           echo, classify, type }], note, guess }],
             summary: { total, resolved, code, name, ambiguous, unknown, named,
                        guessed, indexed, truncated },
             localIndex: { available, count, note }, note, updated }
     语义（界面必须如实体现，不得替用户猜）：
       · kind='code'   ：用户输的就是代码，name 可能为空 → 显示「名称待回填」而不是「—」；
       · kind='name'   ：唯一命中，hits 里可能还有候选（可改选）；
       · kind='ambiguous'：**多命中，绝不自动选第一个**，必须让用户从 hits 里挑一只，
                          未挑选前不允许提交（否则会把分析对象静默换成另一只股票）；
       · kind='unknown'：本地名录与远端都没命中 → 显示服务端 note（说明试过什么）；
       · guess=true    ：名称与远端都没命中、只能按代码处理的猜测（远端只回显，
                         没有真实名称）→ 用 chip warn 标出「按代码处理，可能不存在」。
     识别规则只有服务端一套：前端已删除本地 parseToken 参与识别的路径，
     只在识别接口故障时降级为「按输入原样当代码提交」，并在「识别结果」区块说明降级原因。

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

   实时推送（web/js/stream.js）：
     提交研判成功后建立两条订阅（默认开启，界面上有开关）：
       · /api/stream/quotes  interval=3   → 就地刷新主表「现价 / 涨跌」与组合分配金额
       · /api/stream/advisor interval=30  → snapshot / change / pulse 就地刷新研判结论
     推送连不上或断了会自动降级为 15 秒轮询（轮询体里 save 恒为 false，不会刷爆历史记录）；
     原有的 60 秒自动刷新保留为独立兜底。
     查看历史记录（st.history 非空）时订阅一律关闭，退出历史视图后按开关状态重新订阅。
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, paint } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;
  const isNum = window.AD.isNum;
  const MARKET_LABEL = window.AD.MARKET_LABEL || { cn: 'A股', us: '美股' };

  const AUTO_MS = 60000;              /* 自动刷新间隔 */
  const PUSH_QUOTES_SEC = 3;          /* 行情推送 interval（秒） */
  const PUSH_ADVISOR_SEC = 30;        /* 研判推送 interval（秒） */
  const PUSH_FALLBACK_MS = 15000;     /* 推送降级为轮询后的间隔 */
  const FLASH_MS = 2000;              /* 推送变更行的高亮时长 */
  const PUSH_NOTE_MAX = 120;          /* 「最近变化」提示行最大展示字符数 */
  const MAX_SYMBOLS = 30;             /* 单次批量上限，避免一次性打爆服务端 */
  const SEP = /[\s,，、;；|]+/;         /* 逗号 / 空格 / 换行 / 分号分隔 */
  const RESOLVE_DEBOUNCE_MS = 400;    /* 输入变化后去抖多久自动识别（边打字边看结果） */
  const RESOLVE_LIMIT = 5;            /* 单条输入最多返回多少个候选（服务端 limit） */
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

  /* 列定义指纹：列没变就只更新行，避免动表头（setCols 会重建表头，打断 hover） */
  function colsSig(cols) {
    return (cols || []).map((c) => c.key + '\u0001' + (c.label || '') + '\u0001' + (c.width || '')).join('\u0002');
  }

  /* 降级路径的极简形态判断：**只在识别接口不可用时使用**。

  这里刻意不做任何名称 / 拼音 / 别名推断 —— 识别规则只有服务端一套，前端不再复刻，
  否则同一个输入会在「识别」与「提交」两条路径上得到不同结果（历史上本地 parseToken
  与服务端规则就是这么漂移的）。降级时只回答一个问题：这个 token 长得像不像代码。 */
  function shapeOfToken(raw) {
    const s = String(raw || '').trim();
    if (!s) return null;
    if (/^\d{6}$/.test(s)) return { code: s, market: 'cn' };
    if (/^[A-Za-z][A-Za-z0-9.\-]{0,9}$/.test(s)) return { code: s.toUpperCase(), market: 'us' };
    return null;
  }

  /* ------------------------------------------------------------ 视图 */

  function mount(root, ctx) {
    const st = {
      rows: [], portfolio: null, disclaimer: '',
      submitted: false, auto: true, loading: false, destroyed: false,
      lastBody: null, symbolMap: {}, fields: {},
      /* 标的识别（服务端 /api/symbols/resolve）
         key   = 当前结果对应的输入指纹（market + tokens），输入没变就不重复请求；
         items = 服务端 items（原样保留 kind / note / guess 语义）；
         pick  = 歧义项的用户选择（raw -> code），只有用户选过的才能提交；
         degraded = 识别接口故障时的降级原因，界面必须如实展示。 */
      resolve: {
        key: '', market: '', tokens: [], items: [], summary: null, localIndex: null,
        note: '', loading: false, loaded: false, degraded: '', pick: {},
      },
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
      /* 实时推送（SSE）：on=开关（默认开启）；quotes/advisor=订阅句柄；
         states/sig 用于 chip 合成与「同一批标的 + 同一组参数」判定 */
      push: {
        on: true, quotes: null, advisor: null, sig: '',
        states: { quotes: '', advisor: '' },
        lastChange: null, lastPulse: null, lastQuoteAt: null,
        lastError: '', errToasted: false,
      },
    };
    let timer = null;
    const flashTimers = [];        /* 推送行高亮的延时器，destroy 时统一清理 */

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
    /* 实时推送：状态 chip / 提示行（最近变化 / 上次检查 / 行情推送 / 异常） */
    const pushChipHost = h('span');
    const pushChangeHost = h('span', { class: 'dim3' });
    const pushPulseHost = h('span', { class: 'dim3' });
    const pushQuoteHost = h('span', { class: 'dim3' });
    const pushErrHost = h('span', { class: 'dim3' });
    const pushNoteHost = h('div', {
      class: 'legend-inline', style: { lineHeight: '1.8', marginTop: '4px' },
    }, [pushChangeHost, pushPulseHost, pushQuoteHost, pushErrHost]);

    /* 「识别结果」区块的挂载点：放在「标的与参数」区块内、标的输入框正下方 */
    const resolveHost = h('div');

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
        input: () => codesChanged(),
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
    /* 输入变化（键入 / 导入自选 / 清空）统一入口：更新计数 + 去抖自动识别 */
    function codesChanged() {
      renderCodeHint();
      scheduleResolve();
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
      codesChanged();
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

    /* 实时推送开关：默认开启（提交研判成功后开始订阅） */
    const pushToggle = h('button', {
      class: 'btn ghost sm active', text: '实时推送',
      title: '开启后：提交研判成功后自动订阅 /api/stream/quotes（推送行情）与 '
        + '/api/stream/advisor（研判变化）；连接失败会自动降级为 15 秒轮询，不弹错、不影响页面。',
      on: { click: (e) => togglePush(e.currentTarget) },
    });

    /* 生成交易计划：只生成计划，不在本页下单 */
    const planBtn = h('button', {
      class: 'btn ghost sm', text: '生成交易计划',
      title: 'POST /api/trade/plan { market, symbols }：按当前输入的标的生成交易计划，到模拟交易页执行；'
        + '接口未就绪时仅提示失败',
      on: { click: () => genTradePlan() },
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
                on: { click: () => { codeInput.value = ''; codesChanged(); } },
              }),
              planBtn,
            ]),
            /* 识别结果：紧贴输入框下方，输入变化去抖 400ms 自动识别 */
            resolveHost,
          ]),
        ]),
        numField('horizon', '预测窗口（交易日）', 20, '1', '模型对未来多少个交易日做预测，默认 20'),
        numField('capital', '本金', 100000, 'any', '用于折算凯利仓位金额与股数，默认 100000'),
        h('div', { class: 'field' }, [h('label', { text: '凯利折扣' }), kellySel]),
        numField('maxWeight', '单只权重上限', 0.25, '0.05', '小数或百分数：0.25 与 25 都表示 25%'),
        h('div', { class: 'field' }, [h('label', { text: '历史记录' }), saveToggle]),
        h('div', { class: 'field wide' }, [
          h('label', { text: '实时推送' }),
          h('div', { style: { flex: '1 1 auto', minWidth: '0' } }, [
            h('div', { class: 'legend-inline', style: { alignItems: 'center' } }, [pushToggle, pushChipHost]),
            pushNoteHost,
          ]),
        ]),
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
      /* 免责声明只有一句文案会变：原位改写文本，不重建节点 */
      paint(disclaimerHost, [h('div', { class: 'legend-inline', style: { marginTop: '8px', lineHeight: '1.8' } }, [
        h('span', { class: 'chip warn', text: '免责声明' }),
        h('span', { class: 'dim3', text: text(st.disclaimer, DISCLAIMER_DEFAULT) }),
      ])]);
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

    /* ---- 单列渲染 ----
       抽成独立函数是为了让「实时推送就地更新」与「整表渲染」共用同一套口径：
       推送只替换某一列，绝不能出现两套渲染逻辑导致同一列前后样式/文案不一致。 */

    function nameCell(r) {
      const nm = rowName(r);
      const code = text(r.code);
      /* 用户只输入代码时名称会回落到代码本身，此时不再重复显示一行代码 */
      const same = String(nm) === String(code);
      return h('span', {}, [
        h('span', { class: 'name', text: nm }),
        same ? null : h('span', { class: 'code', text: (rowMarket(r) === 'us' ? 'US:' : '') + code }),
      ]);
    }

    function priceCell(r) {
      return h('span', { class: 'num ' + F.dir(r.changePct) }, [
        h('span', { text: F.price(r.price, rowMarket(r)) }),
        h('span', { class: 'code', text: isNum(r.changePct) ? ' ' + F.pct(r.changePct) : ' —' }),
      ]);
    }

    function actionCell(r) {
      const a = String(r.action || '').toLowerCase();
      const label = ACTION_LABEL[a] || text(r.actionText, '—');
      /* 推送带来的原因摘要挂在 title 上，不占列宽 */
      const reasons = Array.isArray(r.pushedReasons) ? r.pushedReasons.filter(Boolean) : [];
      return h('span', {
        class: ACTION_CLS[a] || 'chip',
        title: reasons.length ? '推送变更：' + reasons.join('；') : text(r.actionText, label),
        text: label,
      });
    }

    function scoreCell(r) {
      return h('span', {
        class: 'num' + (isNum(r.score) ? '' : ' dim3'),
        title: '综合评分（越高越积极）', text: F.num(r.score, 1),
      });
    }

    function confidenceCell(r) {
      const v = asPct(r.confidence);
      return h('span', {
        class: 'num' + (isNum(v) ? '' : ' dim3'),
        title: '模型对该结论的置信度',
        text: isNum(v) ? F.num(v, 0) + '%' : '—',
      });
    }

    function forecastCell(r) {
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
    }

    function kellyCell(r) {
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
    }

    function planCell(r) {
      const p = r.plan || {};
      if (!isNum(p.entry) && !isNum(p.stop) && !isNum(p.target1) && !isNum(p.target2)) return dash();
      const mkt = rowMarket(r);
      const bits = '入 ' + F.price(p.entry, mkt) + ' · 损 ' + F.price(p.stop, mkt) +
        ' · 标 ' + F.price(p.target1, mkt) + ' / ' + F.price(p.target2, mkt) +
        (isNum(p.riskReward) ? ' · 盈亏比 ' + F.num(p.riskReward, 2) : '');
      return h('span', { class: 'num', title: '入场 / 止损 / 目标位由服务端模型给出，仅作计划参考', text: bits });
    }

    function buildCols() {
      return [
        {
          key: 'name', label: '标的', cls: 'name', noSort: true,
          render: nameCell,
        },
        {
          key: 'price', label: '现价 / 涨跌', cls: 'n', value: (r) => r.changePct,
          render: priceCell,
        },
        {
          key: 'action', label: '建议', width: '104px', noSort: true,
          render: actionCell,
        },
        {
          key: 'score', label: '评分', cls: 'n', value: (r) => r.score,
          render: scoreCell,
        },
        {
          key: 'confidence', label: '置信度', cls: 'n', value: (r) => asPct(r.confidence),
          render: confidenceCell,
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
          render: forecastCell,
        },
        {
          key: 'kelly', label: '凯利仓位', noSort: true, value: (r) => asPct((r.kelly || {}).weight), width: '196px',
          render: kellyCell,
        },
        {
          key: 'plan', label: '交易计划', noSort: true, width: '252px',
          render: planCell,
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

    /* 主表实例 / 列指纹（必须缓存在 mount 内，跨刷新复用）：
       首次挂载后只调 update(rows)，60 秒轮询与推送不再重建表体。 */
    let tableRef = null;
    let tableColsSig = '';

    function renderTable() {
      if (!st.rows.length) {
        /* 空态：原位改写提示；旧表实例随提示一起被替换，下次有数据时重建一次 */
        paint(tableHost, [ui.empty(st.history
          ? '该历史记录没有逐只研判明细（记录 #' + text(st.history.id) + '）'
          : (st.submitted
            ? '服务端未返回任何标的的研判结果，请检查代码是否正确或稍后重试'
            : '在上方填写标的（代码或中文名）后点击「开始 AI 分析」'))]);
        tableRef = null;
        tableColsSig = '';
        return;
      }
      const cols = buildCols();
      const sig = colsSig(cols);
      if (!tableRef) {
        tableRef = ui.tbl({
          cols,
          rows: st.rows,
          sortKey: 'score',
          sortDir: 'desc',
          maxHeight: 'calc(100vh - 460px)',
          rowKey: (r) => rowMarket(r) + ':' + text(r.code),
          onRow: (r) => ctx.openSymbol(rowMarket(r), r.code, rowName(r)),
          emptyText: '暂无标的',
        });
        tableColsSig = sig;
        paint(tableHost, []);                  /* 清掉空态 / 加载占位（只删节点，不重建表格） */
        tableHost.appendChild(tableRef);       /* 首次挂载；之后只 update，绝不重复挂载 */
        return;
      }
      if (sig !== tableColsSig) {              /* 列定义变化：换列不换整表 */
        tableRef.setCols(cols);
        tableColsSig = sig;
      }
      tableRef.update(st.rows);
    }

    /* --------------------------------------------------- 组合分配 */

    /* 组合分配区的稳定挂载点（mount 内一次性建好，刷新时各自原位更新）：
       统计卡只改数值文本、权重条只改 flex、明细表只换行 —— 不重建节点，
       所以滚动位置、hover 与用户的阅读位置都不会被打断。 */
    const pfAltHost = h('div');                                       /* 空态 / 未提交提示 */
    const pfMetricHost = h('div');                                    /* 汇总指标卡 */
    const pfBarHost = h('div');                                       /* 权重条 */
    const pfLegendHost = h('div');                                    /* 权重图例 */
    const pfTableHost = h('div', { style: { marginTop: '12px' } });    /* 分配明细表 */
    const pfNoteHost = h('div');                                      /* 备注行 */
    portfolioHost.appendChild(pfAltHost);
    portfolioHost.appendChild(pfMetricHost);
    portfolioHost.appendChild(pfBarHost);
    portfolioHost.appendChild(pfLegendHost);
    portfolioHost.appendChild(pfTableHost);
    portfolioHost.appendChild(pfNoteHost);

    let pfRef = null;                  /* 明细表实例（mount 内缓存，绝不放模块级） */
    let pfColsSig = '';
    let pfMaxW = 1;                    /* 占比条基准：列定义只建一次，渲染时读这里的值 */
    let pfMarket = ctx.state.market;   /* 金额口径市场：同上 */

    /* 行市场：优先服务端字段，其次提交时的输入映射，最后当前市场（与整表渲染同一口径） */
    function pfMktOfRow(r) {
      const hit = st.symbolMap[String((r && r.code) || '').toUpperCase()] || {};
      return (r && r.market) || hit.market || pfMarket;
    }

    /* 明细表列定义：只依赖 pfMaxW / pfMarket / st.symbolMap，所以可以只建一次 */
    function buildPfCols() {
      return [
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
            text: isNum(r.amount) ? F.amt(r.amount, pfMktOfRow(r)) : '—',
          }),
        },
        {
          key: 'bar', label: '占比', noSort: true, width: '180px',
          render: (r) => {
            const w = asPct(r.weight);
            const p = isNum(w) ? Math.min(100, (w / pfMaxW) * 100) : 0;
            return h('div', { class: 'prog' }, [
              h('div', { class: 'prog-bar' }, [h('i', { style: { width: p.toFixed(1) + '%' } })]),
            ]);
          },
        },
      ];
    }

    /* 空态 / 未提交：提示原位改写，其余区块清空并隐藏（表实例作废，下次有数据时重建一次） */
    function paintPfEmpty(msg, note) {
      paint(pfAltHost, [ui.empty(msg), note ? noteLine(note) : null]);
      pfAltHost.style.display = '';
      [pfMetricHost, pfBarHost, pfLegendHost, pfTableHost, pfNoteHost].forEach((el) => {
        el.style.display = 'none';
        paint(el, []);
      });
      pfRef = null;
      pfColsSig = '';
    }

    function renderPortfolio() {
      if (!st.submitted) {
        paintPfEmpty('提交标的后显示组合权重分配');
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
      pfMarket = mkt;
      const ws = rows.map((r) => asPct(r.weight)).filter(isNum);
      const sumW = isNum(p.totalWeight) ? asPct(p.totalWeight) : (ws.length ? ws.reduce((a, b) => a + b, 0) : null);
      const amts = rows.map((r) => r.amount).filter(isNum);
      const sumAmt = amts.length ? amts.reduce((a, b) => a + b, 0) : null;
      const cash = isNum(p.cash) ? p.cash : (isNum(cap) && sumAmt !== null ? Math.max(0, cap - sumAmt) : null);

      if (!rows.length && !isNum(sumW) && !isNum(cash)) {
        paintPfEmpty('服务端未返回组合分配数据', p.note || '');
        return;
      }

      pfAltHost.style.display = 'none';
      paint(pfAltHost, []);
      pfMetricHost.style.display = '';
      paint(pfMetricHost, [metricList([
        ['纳入标的', rows.length ? rows.length + ' 只' : (st.rows.length + ' 只（无分配明细）')],
        ['总仓位', isNum(sumW) ? F.num(sumW, 1) + '%' : '—'],
        ['现金 / 未分配', isNum(cash) ? F.amt(cash, mkt) : '—'],
        ['资金合计', isNum(sumAmt) ? F.amt(sumAmt, mkt) : '—'],
        ['本金', isNum(cap) ? F.amt(cap, mkt) : '—'],
        ['单只上限', isNum(mw) ? F.num(asPct(mw), 1) + '%' : '—'],
      ])]);

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
        pfBarHost.style.display = '';
        paint(pfBarHost, [bar]);

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
        pfLegendHost.style.display = '';
        paint(pfLegendHost, [legend]);
      } else {
        /* 没有权重明细：两块清空并隐藏，不留占位高度 */
        pfBarHost.style.display = 'none';
        pfLegendHost.style.display = 'none';
        paint(pfBarHost, []);
        paint(pfLegendHost, []);
      }

      if (rows.length) {
        pfMaxW = Math.max.apply(null, ws.concat([1]));
        const cols = buildPfCols();
        const sig = colsSig(cols);
        if (!pfRef) {
          pfRef = ui.tbl({ cols, rows, compact: true, emptyText: '无分配明细' });
          pfColsSig = sig;
          pfTableHost.style.display = '';
          paint(pfTableHost, []);             /* 只删节点，不重建表格 */
          pfTableHost.appendChild(pfRef);     /* 首次挂载；之后只 update，绝不重复挂载 */
        } else {
          if (sig !== pfColsSig) { pfRef.setCols(cols); pfColsSig = sig; }
          pfTableHost.style.display = '';
          pfRef.update(rows);
        }
      } else {
        pfTableHost.style.display = 'none';
        paint(pfTableHost, []);
        pfRef = null;
        pfColsSig = '';
      }

      pfNoteHost.style.display = '';
      paint(pfNoteHost, [
        p.note ? noteLine(p.note) : null,
        noteLine('组合分配由服务端按凯利折扣与单只权重上限折算，权重之和即总仓位；' +
          '本金与现金按当前市场本币口径，不做跨市场汇率换算。'),
      ]);
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
      /* 已有结果时保留当前表格（后台轮询不闪、不跳滚动、不丢 hover），
         只有「还没有任何结果」才用加载占位（与原来首次提交的观感一致） */
      if (!tableRef || !st.rows.length) {
        paint(tableHost, [ui.loading('模型计算中…（多标的批量研判可能需要数秒）')]);
        tableRef = null;
        tableColsSig = '';
      }
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
        /* 拿到结果：建立/续订实时推送（同标的同参数时保持既有连接；
           历史视图下 startPush 会自行拒绝） */
        if (st.push.on && st.rows.length) startPush(b);
      } catch (e) {
        if (st.destroyed) return;
        st.rows = [];
        st.portfolio = null;
        statHost.textContent = '分析失败';
        /* 失败提示原位改写；旧表实例随之作废，下次成功时重建一次 */
        paint(tableHost, [ui.empty('分析失败：' + e.message + '（接口 /api/advisor/recommend）')]);
        tableRef = null;
        tableColsSig = '';
        paintPfEmpty('无组合分配数据');
        ctx.toast('AI 选股失败：' + e.message, 'err');
      } finally {
        st.loading = false;
        if (!st.destroyed) submitBtn.disabled = false;
      }
    }

    /* ============================================ 标的识别（服务端 /api/symbols/resolve）

       识别规则只有服务端一套：前端只负责「切 token → 调接口 → 如实渲染」，
       不再自己解析代码 / 拼拼音 / 取搜索结果第一个（老实现的三处问题：
       只输代码不显示名称、中文名多命中时静默取第一个会把分析对象换成另一只股票）。
       歧义项必须由用户从 hits 里挑一只，未挑选前 submit() / genTradePlan() 一律不提交。 */

    let resolveTimer = null;        /* 输入去抖定时器（destroy 时清理） */

    function resolveKey(list, market) {
      return String(market || '') + '|' + (list || []).join('\u0001');
    }

    /* 手动识别：输入没变也强制重发一次（用户点按钮就是要重试） */
    const resolveBtn = h('button', {
      class: 'btn ghost sm', text: '识别',
      title: 'POST /api/symbols/resolve { market, tokens }：按当前输入重新识别标的；'
        + '输入变化后 400ms 也会自动识别',
      on: {
        click: () => {
          if (resolveTimer) { clearTimeout(resolveTimer); resolveTimer = null; }
          if (!readCodes().length) { ctx.toast('请先输入标的：代码或中文名，逗号 / 空格 / 换行分隔', 'warn'); return; }
          resolveInput(readCodes(), true);
        },
      },
    });

    /* 输入变化 → 去抖 400ms 自动识别（用户一边打字一边看到识别结果） */
    function scheduleResolve() {
      if (resolveTimer) { clearTimeout(resolveTimer); resolveTimer = null; }
      const tokens = readCodes();
      if (!tokens.length) {                     /* 空输入：清掉结果，只留引导文案 */
        resetResolve();
        renderResolve();
        return;
      }
      resolveTimer = setTimeout(() => {
        resolveTimer = null;
        if (st.destroyed) return;
        resolveInput(tokens);
      }, RESOLVE_DEBOUNCE_MS);
    }

    function resetResolve() {
      const r = st.resolve;
      r.key = ''; r.market = ''; r.tokens = []; r.items = []; r.summary = null;
      r.localIndex = null; r.note = ''; r.loaded = false; r.degraded = '';
      r.pick = {};
    }

    /* 服务端字段兜底：缺字段一律降级，绝不让渲染层因为一个 undefined 崩掉 */
    function normItem(it) {
      const o = (it && typeof it === 'object') ? it : {};
      return {
        raw: String(o.raw === undefined || o.raw === null ? '' : o.raw),
        kind: String(o.kind || 'unknown'),
        code: o.code ? String(o.code).toUpperCase() : null,
        market: o.market || null,
        name: o.name === undefined || o.name === null || o.name === '' ? '' : String(o.name),
        hits: Array.isArray(o.hits) ? o.hits.filter((x) => x && x.code) : [],
        note: o.note ? String(o.note) : '',
        guess: o.guess === true,
      };
    }

    /* 接口故障时的降级结果：按输入原样当代码（**不做任何名称识别**）。
       名字形态的 token 无法在本地解析成代码，标为未识别并在界面上说明原因 ——
       把「茅台」当代码提交只会换来一行服务端错误，不如如实说「解析不了」。 */
    function degradedItems(tokens, reason) {
      return (tokens || []).map((t) => {
        const shape = shapeOfToken(t);
        if (shape) {
          return {
            raw: t, kind: 'code', code: shape.code, market: shape.market, name: '', hits: [],
            guess: false, note: '识别接口不可用：按输入原样当作代码提交，名称待服务端回填',
          };
        }
        return {
          raw: t, kind: 'unknown', code: null, market: ctx.state.market, name: '', hits: [],
          guess: false, note: '识别接口不可用，且这不是代码形态，无法解析成标的（' + reason + '）',
        };
      });
    }

    /* 服务端结果落地：保留仍然有效的用户选择（输入没变时不把用户挑好的候选清掉） */
    function applyItems(items, market) {
      const r = st.resolve;
      const list = (Array.isArray(items) ? items : []).map(normItem);
      const nextPick = {};
      Object.keys(r.pick || {}).forEach((raw) => {
        const it = list.find((x) => x.raw === raw);
        if (!it || it.kind !== 'ambiguous') return;
        if (!(it.hits || []).some((h) => String(h.code).toUpperCase() === String(r.pick[raw]).toUpperCase())) return;
        nextPick[raw] = String(r.pick[raw]).toUpperCase();
      });
      r.pick = nextPick;
      r.items = list;
      r.market = market;
      r.loaded = true;
      return list;
    }

    /* 调服务端识别接口；任何失败都降级为「按输入原样提交」，绝不把提交流程 block 住 */
    async function resolveInput(tokens, force) {
      const list = (tokens || []).map((t) => String(t).trim()).filter(Boolean);
      const market = ctx.state.market;
      const r = st.resolve;
      const key = resolveKey(list, market);
      if (!force && r.loaded && r.key === key) return r;      /* 输入没变：复用，不重复请求 */

      r.loading = true;
      r.key = key;
      r.tokens = list;
      renderResolve();
      const body = { market, tokens: list, limit: RESOLVE_LIMIT, max: MAX_SYMBOLS };
      try {
        const call = typeof api.symbolsResolve === 'function'
          ? api.symbolsResolve(body)
          : (typeof api.post === 'function' ? api.post('symbols/resolve', body) : null);
        if (!call) throw new Error('api.symbolsResolve 未接入');
        const res = await call;
        if (st.destroyed) return r;
        if (!res || res.ok === false || !Array.isArray(res.items)) {
          throw new Error((res && (res.message || res.error)) || '服务端未返回有效识别结果');
        }
        r.degraded = '';
        r.summary = res.summary || null;
        r.localIndex = res.localIndex || null;
        r.note = res.note || '';
        applyItems(res.items, res.market || market);
      } catch (e) {
        if (st.destroyed) return r;
        /* 识别接口故障 ≠ 提交失败：按输入原样当代码，并在区块里说明降级原因 */
        r.degraded = (e && e.message) || '识别接口不可用';
        r.summary = null;
        r.localIndex = null;
        r.note = '';
        applyItems(degradedItems(list, r.degraded), market);
      } finally {
        if (!st.destroyed) {
          r.loading = false;
          renderResolve();
        }
      }
      return r;
    }

    /* 提交前的统一入口：复用当前输入对应的识别结果，没有或已过期就现识别一次 */
    async function ensureResolved(tokens) {
      const list = (tokens || []).map((t) => String(t).trim()).filter(Boolean);
      const key = resolveKey(list, ctx.state.market);
      const r = st.resolve;
      if (r.loaded && r.key === key) return r;
      return resolveInput(list);
    }

    /* 单条输入的有效标的；歧义未选 / 未识别返回 null（= 不可提交） */
    function itemSymbol(it) {
      if (!it) return null;
      if (it.kind === 'ambiguous') {
        const code = st.resolve.pick[it.raw];
        if (!code) return null;
        const hit = (it.hits || []).find((x) => String(x.code).toUpperCase() === String(code).toUpperCase()) || {};
        return {
          code: String(hit.code || code).toUpperCase(),
          market: hit.market || it.market || ctx.state.market,
          name: hit.name ? String(hit.name) : '',
        };
      }
      if (it.code) {
        return { code: it.code, market: it.market || ctx.state.market, name: it.name || '' };
      }
      return null;
    }

    /* 仍未选择的歧义项（有它就不允许提交） */
    function pendingAmbiguous(items) {
      return (items || []).filter((it) => it.kind === 'ambiguous' && !itemSymbol(it));
    }

    /* 可提交标的：去重（同一只股票只提交一次），顺序按输入顺序 */
    function usableSymbols(items) {
      const out = [];
      const seen = {};
      (items || []).forEach((it) => {
        const sym = itemSymbol(it);
        if (!sym) return;
        const key = sym.market + ':' + sym.code;
        if (seen[key]) return;
        seen[key] = 1;
        out.push(sym);
      });
      return out;
    }

    /* symbols 提交体：名称来自识别结果，为空则**不带 name 字段**（交给服务端回填） */
    function symbolBody(x) {
      const o = { code: x.code, market: x.market };
      if (x.name) o.name = x.name;
      return o;
    }

    /* 摘要计数：按当前 items + 用户选择实时重算（选中歧义项后「歧义」会立刻回落） */
    function resolveCounts() {
      const c = { total: 0, resolved: 0, code: 0, name: 0, ambiguous: 0, unknown: 0, guessed: 0 };
      (st.resolve.items || []).forEach((it) => {
        c.total++;
        const sym = itemSymbol(it);
        if (sym) c.resolved++;
        if (it.kind === 'code') c.code++;
        else if (it.kind === 'name') c.name++;
        else if (it.kind === 'ambiguous') { if (!sym) c.ambiguous++; }
        else c.unknown++;
        if (it.guess) c.guessed++;
      });
      return c;
    }

    function hitText(x) {
      const code = (x.market === 'us' ? 'US:' : '') + String(x.code || '');
      return code + ' ' + (x.name || '—') + '（' + (x.tier || '候选') + '）';
    }

    function pickAmbiguous(raw, code) {
      const r = st.resolve;
      if (!code) delete r.pick[raw];
      else r.pick[raw] = String(code).toUpperCase();
      renderResolve();       /* 选中后立即更新明细与摘要 */
    }

    /* 歧义未选时把「识别结果」滚入视野，让用户知道卡在哪 */
    function focusResolve() {
      if (resolveHost && typeof resolveHost.scrollIntoView === 'function') {
        try { resolveHost.scrollIntoView({ block: 'center' }); } catch (e) { resolveHost.scrollIntoView(); }
      }
    }

    /* 单条明细：原始输入 → 代码 名称（/ 未识别 / 歧义选择） */
    function resolveRow(it) {
      const row = h('div', {
        class: 'legend-inline',
        style: { alignItems: 'center', gap: '8px', marginTop: '3px', lineHeight: '1.8' },
      });
      row.appendChild(h('span', { class: 'num', style: { color: 'var(--text-2)' }, text: it.raw || '—' }));
      row.appendChild(h('span', { class: 'dim3', text: '→' }));

      if (it.kind === 'ambiguous') {
        const sel = h('select', {
          class: 'inp',
          style: { minWidth: '210px', padding: '3px 6px' },
          title: '多命中：必须选择一只要分析的标的（不选则无法提交）',
          on: { change: (e) => pickAmbiguous(it.raw, e.target ? e.target.value : '') },
        }, [h('option', { value: '', text: '请选择' })].concat(
          (it.hits || []).map((x) => h('option', { value: String(x.code), text: hitText(x) }))
        ));
        sel.value = st.resolve.pick[it.raw] || '';
        row.appendChild(sel);
        row.appendChild(h('span', { class: 'chip warn', text: '歧义' }));
        row.appendChild(h('span', {
          class: 'dim3',
          text: (st.resolve.pick[it.raw] ? '已选择 ' + st.resolve.pick[it.raw] + ' · ' : '') +
            text(it.note, '命中多个候选，请选择'),
        }));
        return row;
      }

      if (it.kind === 'unknown') {
        row.appendChild(h('span', { class: 'chip down', text: '未识别' }));
        row.appendChild(h('span', { class: 'dim3', text: text(it.note, '本地名录与远端搜索都没有命中') }));
        return row;
      }

      const sym = itemSymbol(it);
      row.appendChild(h('span', {
        class: 'num',
        text: sym ? (sym.market === 'us' ? 'US:' : '') + sym.code : '—',
      }));
      const nm = sym && sym.name ? sym.name : '';
      if (nm) row.appendChild(h('span', { text: nm }));
      /* 只输代码时服务端会在取行情后回填名称：这里如实显示「待回填」，不显示「—」 */
      else row.appendChild(h('span', { class: 'dim3', text: '名称待回填' }));
      if (it.guess) {
        row.appendChild(h('span', {
          class: 'chip warn', text: '按代码处理，可能不存在',
          title: '名称与远端搜索都没有命中，只能按代码处理；并不代表这只股票一定存在',
        }));
      }
      if (it.note) row.appendChild(h('span', { class: 'dim3', text: it.note }));
      return row;
    }

    /* 「识别结果」区块（挂在「标的与参数」里、输入框下方） */
    function renderResolve() {
      clear(resolveHost);
      const tokens = readCodes();
      const r = st.resolve;
      const items = r.items || [];
      const c = resolveCounts();

      /* 头部：区块标题 + 摘要 chips + 手动识别按钮（空输入时也保留，按钮位置固定不跳动） */
      const head = h('div', {
        class: 'legend-inline', style: { alignItems: 'center', marginTop: '8px', lineHeight: '1.8' },
      }, [h('span', { class: 'chip accent', text: '识别结果' })]);

      if (items.length) {
        head.appendChild(h('span', {
          class: 'chip up', text: '已识别 ' + c.resolved + ' 只',
          title: '共 ' + c.total + ' 个输入，其中 ' + c.resolved + ' 个可直接提交' +
            (c.ambiguous ? '；另有 ' + c.ambiguous + ' 个待选择' : ''),
        }));
        head.appendChild(h('span', { class: 'chip', text: '名称 ' + c.name, title: '按中文名 / 简称 / 拼音命中的输入' }));
        head.appendChild(h('span', { class: 'chip', text: '代码 ' + c.code, title: '按代码识别的输入' }));
        head.appendChild(h('span', {
          class: 'chip warn', text: '歧义 ' + c.ambiguous,
          title: '命中多只股票，必须选择后才能提交',
        }));
        head.appendChild(h('span', { class: 'chip down', text: '未识别 ' + c.unknown, title: '本地名录与远端搜索都没有命中的输入' }));
        head.appendChild(h('span', {
          class: 'chip warn', text: '按代码处理 ' + c.guessed,
          title: '名称与远端都没命中、只能按代码处理的猜测结果（可能不存在）',
        }));
      }
      head.appendChild(resolveBtn);
      if (r.loading) head.appendChild(h('span', { class: 'dim3', text: '识别中…' }));
      resolveHost.appendChild(head);

      /* 空输入：只留引导文案（区块不隐藏，用户知道这里会有识别结果） */
      if (!tokens.length) {
        resolveHost.appendChild(h('div', {
          class: 'dim3', style: { fontSize: '11px', marginTop: '2px', lineHeight: '1.8' },
          text: '输入代码 / 中文名 / 拼音（如 600519、贵州茅台、gzmt），停顿 0.4 秒自动识别；' +
            '多命中的标的必须先在下方选择后才能提交。',
        }));
        return;
      }

      /* 输入数与明细数不一致要如实说明：服务端会把解析到同一只股票的重复输入合并
         （实测 600519 / 茅台 / gzmt 三条输入只回一条 600519），不解释会让人以为丢了输入 */
      if (items.length && items.length !== tokens.length) {
        resolveHost.appendChild(h('div', {
          class: 'dim3', style: { fontSize: '11px', margin: '2px 0 2px' },
          text: '输入 ' + tokens.length + ' 个 → 明细 ' + items.length + ' 条' +
            (r.summary && r.summary.truncated ? '（超过单次上限，已截断）' : '（解析到同一只股票的重复输入已合并）'),
        }));
      }

      /* 本地名录来源说明（来自服务端 localIndex） */
      const li2 = r.localIndex || {};
      if (isNum(li2.count) && li2.count > 0) {
        resolveHost.appendChild(h('div', {
          class: 'dim3', style: { fontSize: '11px', margin: '2px 0 2px' },
          title: r.note || '',
          text: '本地名录 ' + li2.count + ' 条' + (li2.note ? '：' + li2.note : ''),
        }));
      } else if (items.length) {
        resolveHost.appendChild(h('div', {
          class: 'dim3', style: { fontSize: '11px', margin: '2px 0 2px' },
          text: '本地名录当前不可用（所有输入都走了远端搜索）',
        }));
      }

      if (r.degraded) {
        resolveHost.appendChild(h('div', {
          class: 'legend-inline', style: { alignItems: 'center', marginTop: '4px' },
        }, [
          h('span', { class: 'chip warn', text: '识别接口不可用，已降级' }),
          h('span', {
            class: 'dim3',
            text: '原因：' + r.degraded + '；降级为「按输入原样当代码提交」，名称不会自动补全，' +
              '中文名 / 拼音请改用 6 位代码或字母代码。',
          }),
        ]));
      }
      if (r.summary && r.summary.truncated) {
        resolveHost.appendChild(h('div', {
          class: 'dim3', style: { fontSize: '11px', marginTop: '2px' },
          text: '输入超过单次上限（' + MAX_SYMBOLS + ' 个），识别只处理了前 ' + MAX_SYMBOLS + ' 个',
        }));
      }
      if (!items.length && !r.loading && !r.degraded) {
        resolveHost.appendChild(h('div', {
          class: 'dim3', style: { fontSize: '11px', marginTop: '4px' }, text: '尚未识别到标的',
        }));
      }

      items.forEach((it) => resolveHost.appendChild(resolveRow(it)));
    }

    /* 提交：识别标的 -> 组装 body -> 取数。
       识别走服务端接口；歧义项未选择前**一律不提交**（不替用户选股票）。 */
    async function submit() {
      if (st.loading) return;
      const raw = readCodes();
      if (!raw.length) { ctx.toast('请先输入标的：代码或中文名，逗号 / 空格 / 换行分隔', 'warn'); return; }
      let tokens = raw;
      if (tokens.length > MAX_SYMBOLS) {
        tokens = tokens.slice(0, MAX_SYMBOLS);
        ctx.toast('单次最多分析 ' + MAX_SYMBOLS + ' 只，已截断为前 ' + MAX_SYMBOLS + ' 个', 'warn');
      }

      /* 提交前用最新输入识别一次（已有对应当前输入的结果就直接复用） */
      const r = await ensureResolved(tokens);
      if (st.destroyed) return;

      const pending = pendingAmbiguous(r.items);
      if (pending.length) {
        ctx.toast('有 ' + pending.length + ' 个输入命中多只股票，请在「识别结果」里选择后重试', 'warn');
        focusResolve();
        return;
      }

      const list = usableSymbols(r.items);
      if (!list.length) {
        ctx.toast(r.degraded
          ? '识别接口不可用，且输入里没有可用的代码：请改用 6 位代码或字母代码后重试'
          : '没有识别出有效标的，请检查代码格式', 'err');
        focusResolve();
        return;
      }

      const unknown = (r.items || []).filter((x) => x.kind === 'unknown').map((x) => x.raw);
      if (unknown.length) ctx.toast('未识别的输入：' + unknown.join('、'), 'warn');
      if (r.degraded) ctx.toast('标的识别接口不可用，已降级为按输入原样提交：' + r.degraded, 'warn');

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
        /* symbols 必须带名称：名称来自识别结果，为空则不带 name 字段（交给服务端回填） */
        symbols: list.map(symbolBody),
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

    /* 生成交易计划：把当前输入的标的交给 /api/trade/plan（模拟交易页负责执行）。
       这里只生成计划，绝不下单；接口未就绪时只 toast，不改动页面其它状态。
       与 submit() 共用同一套识别结果：歧义未选的输入同样不允许提交。 */
    async function genTradePlan() {
      const raw = readCodes();
      if (!raw.length) { ctx.toast('请先输入标的：代码或中文名，逗号 / 空格 / 换行分隔', 'warn'); return; }
      if (planBtn.disabled) return;
      if (typeof api.post !== 'function') { ctx.toast('生成交易计划失败：api.post 未接入', 'err'); return; }
      planBtn.disabled = true;
      try {
        const tokens = raw.length > MAX_SYMBOLS ? raw.slice(0, MAX_SYMBOLS) : raw;
        const r = await ensureResolved(tokens);
        if (st.destroyed) return;
        const pending = pendingAmbiguous(r.items);
        if (pending.length) {
          ctx.toast('有 ' + pending.length + ' 个输入命中多只股票，请在「识别结果」里选择后重试', 'warn');
          focusResolve();
          return;
        }
        const list = usableSymbols(r.items);
        if (!list.length) {
          ctx.toast(r.degraded ? '识别接口不可用，且输入里没有可用的代码，无法生成交易计划'
            : '没有识别出有效标的，无法生成交易计划', 'err');
          focusResolve();
          return;
        }
        const res = await api.post('trade/plan', {
          market: ctx.state.market,
          symbols: list.map((x) => x.code),
        });
        if (st.destroyed) return;
        const n = planCount(res, list.length);
        ctx.toast('已生成 ' + n + ' 笔交易计划（模拟交易页可执行）', 'ok');
      } catch (e) {
        if (st.destroyed) return;
        ctx.toast('生成交易计划失败：' + e.message, 'err');
      } finally {
        if (!st.destroyed) planBtn.disabled = false;
      }
    }

    /* 计划条数：接口返回体字段未定，按常见字段兜底，取不到就用标的不数 */
    function planCount(res, fallback) {
      if (!res) return fallback;
      if (isNum(res.count)) return res.count;
      if (isNum(res.total)) return res.total;
      const arr = res.plans || res.orders || res.rows || res.items;
      if (Array.isArray(arr)) return arr.length;
      return fallback;
    }

    /* ==================================================== 实时推送（SSE） */

    /* 连接状态文案（与 stream.js 里的 chip 文案保持一致，这里只用于 title 说明） */
    const STATE_TEXT = {
      open: '已连接', connecting: '连接中', fallback: '已降级为轮询',
      unsupported: '浏览器不支持推送', closed: '已关闭',
    };
    /* chip 合成优先级：任一路处于更差状态就按更差的显示 */
    const STATE_RANK = ['unsupported', 'fallback', 'connecting', 'open'];
    let pushChipState = null;      /* 当前 chip 显示的状态，避免推送每 3 秒重建节点 */

    function tsText(ts) {
      const s = window.AD.stream;
      return s && typeof s.tsText === 'function' ? s.tsText(ts) : String(ts === undefined ? '—' : ts);
    }

    function pushTitle() {
      const parts = [];
      ['quotes', 'advisor'].forEach((k) => {
        const stt = st.push.states[k];
        if (stt) parts.push((k === 'quotes' ? '行情 ' : '研判 ') + (STATE_TEXT[stt] || stt));
      });
      return '数据来自 /api/stream/advisor（研判变化）与 /api/stream/quotes（推送行情）；'
        + '无实质性变化时不重复推送（服务端只发 pulse 心跳）。'
        + (parts.length ? ' 当前：' + parts.join(' · ') : '');
    }

    function combinedState() {
      const list = [st.push.states.quotes, st.push.states.advisor].filter(Boolean);
      if (!list.length) return null;
      for (let i = 0; i < STATE_RANK.length; i++) {
        if (list.indexOf(STATE_RANK[i]) >= 0) return STATE_RANK[i];
      }
      return list[0];
    }

    function paintPushChip(state, title) {
      const s = window.AD.stream;
      if (!state || !s || typeof s.chip !== 'function') {
        clear(pushChipHost);
        pushChipState = null;
        return;
      }
      const t = title || pushTitle();
      /* 状态没变就只刷新 title：行情推送每 3 秒都会走到这里，不该反复重建节点 */
      if (pushChipState === state && pushChipHost.firstChild) {
        pushChipHost.firstChild.title = t;
        return;
      }
      pushChipState = state;
      clear(pushChipHost);
      pushChipHost.appendChild(s.chip(state, t));
    }

    function setNote(el, s) {
      el.textContent = s || '';
      el.style.display = s ? '' : 'none';
    }

    function renderPushNote() {
      const lc = st.push.lastChange;
      const lp = st.push.lastPulse;
      if (!st.push.on) {
        setNote(pushChangeHost, '实时推送已关闭：仍保留 60 秒自动刷新兜底，可点「刷新」立即更新。');
        setNote(pushPulseHost, '');
      } else if (st.history) {
        setNote(pushChangeHost, '历史视图：已暂停实时推送与自动刷新，退出历史视图后自动恢复。');
        setNote(pushPulseHost, '');
      } else if (!st.push.quotes && !st.push.advisor) {
        setNote(pushChangeHost, '提交一次「开始 AI 分析」后开始订阅实时推送（行情 3 秒 / 研判 30 秒）。');
        setNote(pushPulseHost, '');
      } else if (lc) {
        const parts = lc.changes.map((c) => {
          const nm = text(c.name, text(c.code));
          const act = ACTION_LABEL[String(c.action || '').toLowerCase()] || text(c.actionText, '—');
          const rs = (Array.isArray(c.reasons) ? c.reasons.filter(Boolean) : []).join('；');
          return nm + ' ' + act + (rs ? '（' + rs + '）' : '');
        });
        const full = '最近变化 ' + tsText(lc.ts) + ' · ' + lc.n + ' 只：' + parts.join('；');
        setNote(pushChangeHost, full.length > PUSH_NOTE_MAX ? full.slice(0, PUSH_NOTE_MAX) + '…' : full);
        pushChangeHost.title = full;
        setNote(pushPulseHost, lp
          ? '上次检查 ' + tsText(lp.ts) + ' · 无变化（已检查 ' + text(lp.checked, '—') + ' 只）'
          : '');
      } else {
        setNote(pushChangeHost, '已订阅实时推送：服务端仅在结论变化时推送，其余时间发 pulse 心跳。');
        setNote(pushPulseHost, lp
          ? '上次检查 ' + tsText(lp.ts) + ' · 无变化（已检查 ' + text(lp.checked, '—') + ' 只）'
          : '');
      }
      if (st.push.lastQuoteAt && !st.history) {
        setNote(pushQuoteHost, '行情推送 ' + tsText(st.push.lastQuoteAt));
      } else {
        setNote(pushQuoteHost, '');
      }
      if (st.push.lastError && st.push.on && !st.history) {
        setNote(pushErrHost, '推送异常：' + st.push.lastError + '（已自动降级为轮询，不影响页面）');
      } else {
        setNote(pushErrHost, '');
      }
    }

    /* ---- 行定位：按 code 匹配（表头排序会重排行，不能用行号） ---- */

    function tableRowsIn(host) {
      return Array.prototype.slice.call(host.querySelectorAll('table.tbl tbody tr'));
    }

    function trCode(tr) {
      if (!tr || !tr.cells || !tr.cells.length) return '';
      const cell = tr.cells[0];
      const codeEl = cell.querySelector ? cell.querySelector('.code') : null;
      const raw = codeEl ? codeEl.textContent : cell.textContent;
      return String(raw === null || raw === undefined ? '' : raw).replace(/^US:/i, '').trim().toUpperCase();
    }

    function findTr(host, code) {
      const want = String(code || '').toUpperCase();
      if (!want) return null;
      const rows = tableRowsIn(host);
      for (let i = 0; i < rows.length; i++) {
        if (trCode(rows[i]) === want) return rows[i];
      }
      return null;
    }

    function rowData(code) {
      const want = String(code || '').toUpperCase();
      return st.rows.find((r) => String((r && r.code) || '').toUpperCase() === want) || null;
    }

    /* 主表列序（与 buildCols 一一对应） */
    const COL = {
      name: 0, price: 1, action: 2, score: 3, confidence: 4, ensemble: 5,
      edge: 6, forecast: 7, kelly: 8, plan: 9, signals: 10, risk: 11, act: 12,
    };

    /* 只替换单个单元格：整表重绘会打断用户的选择与滚动位置。
       结构一致时进一步原位改写（chip 只换文案与配色，不换节点） */
    function replaceCell(tr, idx, node) {
      const td = tr && tr.cells ? tr.cells[idx] : null;
      if (!td || !node) return false;
      paint(td, [node]);
      return true;
    }

    /* 现价 / 涨跌：结构一致时只改文本与涨跌色 class，不重建节点 */
    function paintPriceCell(tr, r) {
      const td = tr && tr.cells ? tr.cells[COL.price] : null;
      if (!td) return false;
      const span = td.firstElementChild;
      const t1 = F.price(r.price, rowMarket(r));
      const t2 = isNum(r.changePct) ? ' ' + F.pct(r.changePct) : ' —';
      if (span && span.children && span.children.length === 2) {
        span.className = 'num ' + F.dir(r.changePct);
        span.children[0].textContent = t1;
        span.children[1].textContent = t2;
        return true;
      }
      return replaceCell(tr, COL.price, priceCell(r));
    }

    /* 短暂高亮：项目 CSS 里没有 .flash，用 inline 过渡做 1.2s 淡黄背景，
       同时挂上 class 以便将来接入 CSS（不改 CSS 文件） */
    function flashRow(tr) {
      if (!tr) return;
      tr.classList.add('flash');
      tr.style.transition = 'background-color 1.2s ease';
      tr.style.backgroundColor = 'rgba(245, 165, 36, .20)';
      flashTimers.push(setTimeout(() => {
        if (st.destroyed) return;
        tr.style.backgroundColor = '';        /* 清掉 inline 值，恢复 CSS 的 hover 效果 */
      }, 1200));
      flashTimers.push(setTimeout(() => {
        if (st.destroyed) return;
        tr.classList.remove('flash');
        tr.style.transition = '';
      }, FLASH_MS));
    }

    /* ---- 组合分配区：按 code 就地更新「权重 / 金额 / 占比」与汇总指标 ---- */

    function setNumCell(td, s) {
      if (!td) return;
      const sp = td.firstElementChild;
      if (sp && typeof sp.className === 'string' && sp.className.indexOf('num') >= 0) {
        if (sp.textContent !== s) {
          sp.textContent = s;
          sp.classList.toggle('dim3', s === '—');
        }
        return;
      }
      if (td.textContent !== s) td.textContent = s;
    }

    /* 汇总指标的顺序与 renderPortfolio 里的 metricList 一致：
       纳入标的 / 总仓位 / 现金·未分配 / 资金合计 / 本金 / 单只上限 */
    function syncAllocMetrics() {
      const p = st.portfolio || {};
      const rows = Array.isArray(p.rows) ? p.rows : [];
      const cap = (st.lastBody && isNum(st.lastBody.capital)) ? st.lastBody.capital : null;
      const mkt = (st.history && st.history.market) || ctx.state.market;
      const ws = rows.map((r) => asPct(r.weight)).filter(isNum);
      const sumW = isNum(p.totalWeight) ? asPct(p.totalWeight) : (ws.length ? ws.reduce((a, b) => a + b, 0) : null);
      const amts = rows.map((r) => r.amount).filter(isNum);
      const sumAmt = amts.length ? amts.reduce((a, b) => a + b, 0) : null;
      const cash = isNum(p.cash) ? p.cash : (isNum(cap) && sumAmt !== null ? Math.max(0, cap - sumAmt) : null);
      const vals = [
        rows.length ? rows.length + ' 只' : null,
        isNum(sumW) ? F.num(sumW, 1) + '%' : null,
        isNum(cash) ? F.amt(cash, mkt) : null,
        isNum(sumAmt) ? F.amt(sumAmt, mkt) : null,
      ];
      const cells = portfolioHost.querySelectorAll('.metric-list .metric .v');
      vals.forEach((v, i) => {
        if (v === null || !cells[i]) return;
        if (cells[i].textContent !== v) cells[i].textContent = v;
      });
    }

    /**
     * 组合分配就地刷新。
     * patchRows：推送带来的新分配明细（按 code 合并进 st.portfolio.rows）。
     * 全部为 null 时只按现有权重重算金额，不臆造新数据。
     */
    function syncAllocAmounts(patchRows) {
      if (st.destroyed || st.history || !st.submitted) return;
      const p = st.portfolio || {};
      const rows = Array.isArray(p.rows) ? p.rows : [];
      if (!rows.length) return;
      const cap = (st.lastBody && isNum(st.lastBody.capital)) ? st.lastBody.capital : null;

      if (Array.isArray(patchRows) && patchRows.length) {
        patchRows.forEach((nr) => {
          if (!nr || !nr.code) return;
          const hit = rows.find((x) => String((x && x.code) || '').toUpperCase() === String(nr.code).toUpperCase());
          if (!hit) return;
          if (isNum(nr.weight)) hit.weight = nr.weight;
          if (isNum(nr.amount)) hit.amount = nr.amount;
          if (nr.name && !hit.name) hit.name = nr.name;
          /* 权重变了但推送没带金额：按「本金 × 权重」重算，否则金额列与权重列自相矛盾
             （与「组合分配由服务端按权重折算」的口径一致，不是凭空造数） */
          if (isNum(nr.weight) && !isNum(nr.amount) && isNum(cap)) {
            hit.amount = cap * (asPct(nr.weight) || 0) / 100;
          }
        });
      }

      const trs = tableRowsIn(portfolioHost);
      if (!trs.length) return;                 /* 分配明细表尚未渲染：交由整表渲染处理 */
      const maxW = Math.max.apply(null, rows.map((r) => asPct(r.weight)).filter(isNum).concat([1]));
      rows.forEach((r) => {
        const code = String((r && r.code) || '').toUpperCase();
        const tr = trs.find((x) => trCode(x) === code);
        if (!tr) return;                       /* 找不到对应行就跳过 */
        const w = asPct(r.weight);
        const amt = isNum(r.amount) ? r.amount : (isNum(cap) && isNum(w) ? cap * w / 100 : null);
        setNumCell(tr.cells[1], isNum(w) ? F.num(w, 1) + '%' : '—');
        setNumCell(tr.cells[2], isNum(amt) ? F.amt(amt, r.market || ctx.state.market) : '—');
        const bar = tr.cells[3] ? tr.cells[3].querySelector('.prog-bar > i') : null;
        if (bar) bar.style.width = (isNum(w) ? Math.min(100, (w / maxW) * 100) : 0).toFixed(1) + '%';
      });
      syncAllocMetrics();
    }

    /* ---- 各类推送事件的处理 ---- */

    function applyQuotes(payload) {
      if (st.destroyed || st.history) return;
      const rows = (payload && Array.isArray(payload.rows)) ? payload.rows : [];
      rows.forEach((q) => {
        if (!q || !q.code) return;
        const r = rowData(q.code);
        if (!r) return;                        /* 推送里出现本地没有的标的：跳过 */
        if (isNum(q.price)) r.price = q.price;
        if (isNum(q.changePct)) r.changePct = q.changePct;
        if (isNum(q.change)) r.change = q.change;
        if (q.updated) r.updated = q.updated;
        if (q.source) r.source = q.source;
        const tr = findTr(tableHost, q.code);
        if (tr) paintPriceCell(tr, r);          /* 表格里没有这一行（历史/空态）就只更新数据 */
      });
      if (rows.length) {
        st.push.lastQuoteAt = (payload && payload.ts) || Date.now();
        syncAllocAmounts();
        paintPushChip(combinedState());
        renderPushNote();
      }
    }

    function mergeChange(r, c) {
      if (c.action) r.action = c.action;
      if (c.actionText !== undefined) r.actionText = c.actionText;
      if (isNum(c.score)) r.score = c.score;
      if (isNum(c.confidence)) r.confidence = c.confidence;
      if (isNum(c.price)) r.price = c.price;
      if (isNum(c.changePct)) r.changePct = c.changePct;
      /* 推送体的子对象只带部分字段（kelly 无 fStar/fraction/note，plan 无 riskReward，
         forecast 无 bandLow/bandHigh）：逐字段合并，推送没给的继续沿用上一次已知值，
         两次都没有的字段仍由渲染函数降级为「—」 */
      if (c.kelly) r.kelly = Object.assign({}, r.kelly || {}, c.kelly);
      if (c.plan) r.plan = Object.assign({}, r.plan || {}, c.plan);
      if (c.forecast) r.forecast = Object.assign({}, r.forecast || {}, c.forecast);
      if (c.prevAction) r.prevAction = c.prevAction;
      if (Array.isArray(c.reasons) && c.reasons.length) r.pushedReasons = c.reasons.slice();
    }

    function applyChanges(payload) {
      if (st.destroyed || st.history) return;   /* 历史视图：忽略推送回调 */
      const list = Array.isArray(payload && payload.changes) ? payload.changes : [];
      const applied = [];
      list.forEach((c) => {
        if (!c || !c.code) return;
        const r = rowData(c.code);
        const tr = findTr(tableHost, c.code);
        if (!r || !tr) return;                  /* 找不到对应行就跳过 */
        mergeChange(r, c);
        replaceCell(tr, COL.action, actionCell(r));
        replaceCell(tr, COL.score, scoreCell(r));
        replaceCell(tr, COL.confidence, confidenceCell(r));
        replaceCell(tr, COL.forecast, forecastCell(r));
        replaceCell(tr, COL.kelly, kellyCell(r));
        replaceCell(tr, COL.plan, planCell(r));
        if (isNum(c.price) || isNum(c.changePct)) paintPriceCell(tr, r);
        flashRow(tr);
        applied.push(c);
      });
      if (!applied.length) return;
      st.push.lastChange = {
        ts: (payload && payload.ts) || Date.now(),
        n: applied.length,
        changes: applied,
      };
      const port = payload && payload.portfolio;
      syncAllocAmounts(Array.isArray(port && port.rows) ? port.rows : null);
      renderPushNote();
    }

    /* snapshot：整表快照。仅在本地没有结果时（例如刷新页面后）才拿来渲染，
       否则会把用户刚提交的结果覆盖掉 */
    function applySnapshot(payload) {
      if (st.destroyed || st.history) return;
      if (st.submitted && st.rows.length) return;
      const res = payload && payload.result;
      if (!res || res.ok === false) return;
      const rows = Array.isArray(res.rows) ? res.rows : [];
      if (!rows.length) return;
      st.rows = rows;
      st.portfolio = res.portfolio || null;
      st.disclaimer = res.disclaimer || st.disclaimer;
      st.submitted = true;
      st.symbolMap = {};
      rows.forEach((r) => {
        if (r && r.code) {
          st.symbolMap[String(r.code).toUpperCase()] = {
            market: r.market || ctx.state.market, name: r.name || r.code,
          };
        }
      });
      renderDisclaimer();
      renderTable();
      renderPortfolio();
      statHost.textContent = '服务端推送快照 · ' + rows.length + ' 只 · 更新 ' + tsText(payload.ts);
    }

    function applyPulse(payload) {
      if (st.destroyed || st.history) return;
      st.push.lastPulse = {
        ts: (payload && payload.ts) || Date.now(),
        checked: payload && payload.checked,
      };
      st.push.lastError = '';
      renderPushNote();
    }

    function onPushStatus(channel, state) {
      if (st.destroyed) return;
      st.push.states[channel] = state;
      if (state === 'open') st.push.lastError = '';
      paintPushChip(combinedState());
      renderPushNote();
    }

    function onPushError(info) {
      if (st.destroyed) return;
      const msg = (info && info.message) || '未知错误';
      st.push.lastError = msg;
      /* 最多弹一次：降级为轮询是预期行为，不能刷屏 */
      if (!st.push.errToasted) {
        st.push.errToasted = true;
        ctx.toast('实时推送不可用，已自动降级为轮询：' + msg, 'warn');
      }
      renderPushNote();
    }

    /* ---- 订阅生命周期 ---- */

    function pushSignature(body) {
      return [body.market, (body.codes || []).join(','), body.horizon, body.capital,
        body.kellyFraction, body.maxWeight].join('|');
    }

    function closePush() {
      ['quotes', 'advisor'].forEach((k) => {
        const hd = st.push[k];
        if (hd && typeof hd.close === 'function') hd.close();
        st.push[k] = null;
      });
      st.push.sig = '';
      st.push.states = { quotes: '', advisor: '' };
    }

    function stopPush(why) {
      closePush();
      paintPushChip('closed', why || pushTitle());
      renderPushNote();
    }

    function startPush(body) {
      const s = window.AD.stream;
      if (!s || typeof s.quotes !== 'function') return;
      if (!st.push.on || st.destroyed) return;
      if (st.history) return;                                      /* 历史视图：绝不订阅 */
      if (!body || !Array.isArray(body.codes) || !body.codes.length) return;
      const sig = pushSignature(body);
      if (st.push.quotes && st.push.advisor && st.push.sig === sig) return;   /* 同标的同参数，保持现有连接 */
      closePush();
      st.push.sig = sig;
      st.push.errToasted = false;
      st.push.lastChange = null;
      st.push.lastPulse = null;
      const base = {
        market: body.market,
        symbols: body.codes,
        fallbackMs: PUSH_FALLBACK_MS,
        onError: onPushError,
      };
      st.push.quotes = s.quotes(Object.assign({}, base, {
        interval: PUSH_QUOTES_SEC,
        onReady: () => { /* ready 只表示订阅已受理，无需额外处理 */ },
        onQuotes: applyQuotes,
        onStatus: (state) => onPushStatus('quotes', state),
        fallbackTick: quoteFallbackTick,
      }));
      st.push.advisor = s.advisor(Object.assign({}, base, {
        interval: PUSH_ADVISOR_SEC,
        horizon: body.horizon,
        capital: body.capital,
        kellyFraction: body.kellyFraction,
        maxWeight: body.maxWeight,
        onReady: () => { /* 同上 */ },
        onSnapshot: applySnapshot,
        onChange: applyChanges,
        onPulse: applyPulse,
        onStatus: (state) => onPushStatus('advisor', state),
        fallbackTick: advisorFallbackTick,
      }));
      /* 构造订阅时 onStatus 可能已经同步给出了 unsupported / fallback，
         这里按合成后的真实状态收尾，别用 connecting 把它盖掉 */
      paintPushChip(combinedState() || 'connecting',
        '已订阅 /api/stream/quotes（行情）与 /api/stream/advisor（研判变化），等待服务端 ready…'
        + '无实质性变化时不重复推送（只发 pulse 心跳）。');
      renderPushNote();
    }

    /* 降级轮询 1（quotes 通道）：只拉批量报价，不重算模型 */
    let quotePollBusy = false;
    function quoteFallbackTick() {
      const b = st.lastBody;
      if (st.destroyed || st.history || !b || !Array.isArray(b.codes) || !b.codes.length) return;
      if (quotePollBusy) return;
      quotePollBusy = true;
      return Promise.resolve()
        .then(() => api.quote(b.market, b.codes))
        .then((res) => {
          if (!st.destroyed) applyQuotes({ rows: (res && res.rows) || [], ts: Date.now() });
        })
        .catch(() => { /* 轮询失败静默：等下一轮，界面已有 chip 提示 */ })
        .then(() => { quotePollBusy = false; });
    }

    /* 降级轮询 2（advisor 通道）：复用现有刷新路径 load()。
       注意 load(body) 不带 asSubmit 时 save 恒为 false，降级不会把历史记录刷爆 */
    function advisorFallbackTick() {
      if (st.destroyed || st.history || !root.isConnected) return;
      if (!st.auto) return;                  /* 用户显式关了自动刷新：不代替他轮询模型 */
      if (!st.submitted || !st.lastBody || st.loading) return;
      return load(st.lastBody);
    }

    function togglePush(btn) {
      st.push.on = !st.push.on;
      if (btn) btn.classList.toggle('active', st.push.on);
      if (!st.push.on) {
        closePush();
        paintPushChip('closed', '实时推送已关闭：数据仅靠 60 秒自动刷新（可点「刷新」手动更新）。');
        ctx.toast('已关闭实时推送（保留 60 秒自动刷新兜底）', 'info');
      } else if (st.history) {
        paintPushChip('closed', '历史视图下不订阅推送；退出历史视图后自动恢复。');
        ctx.toast('历史视图下不启用推送，退出历史视图后自动订阅', 'warn');
      } else if (st.submitted && st.lastBody) {
        startPush(st.lastBody);
        ctx.toast('已开启实时推送', 'ok');
      } else {
        paintPushChip(null);
        ctx.toast('已开启实时推送：提交一次「开始 AI 分析」后开始订阅', 'info');
      }
      renderPushNote();
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
      if (!st.history) { bannerHost.style.display = 'none'; paint(bannerHost, []); return; }
      bannerHost.style.display = '';
      const rec = st.history;
      const mkt = histMarket(rec);
      const args = [];
      if (isNum(rec.horizon)) args.push('h=' + rec.horizon);
      if (isNum(rec.capital)) args.push('本金 ' + F.amt(rec.capital, mkt));
      if (isNum(rec.kellyFraction)) args.push(kellyText(rec.kellyFraction));
      if (isNum(rec.maxWeight)) args.push('上限 ' + F.num(asPct(rec.maxWeight), 0) + '%');
      /* 两条提示行原位改写（按钮节点被复用，不会因为重绘而丢点击态） */
      paint(bannerHost, [h('div', {
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
      ]), h('div', { class: 'legend-inline', style: { marginBottom: '8px', lineHeight: '1.8' } }, [
        h('span', {
          class: 'dim3',
          text: '历史记录按保存时的结果原样回放：字段缺失一律显示「—」，不做推断填充；' +
            '为避免 60 秒自动刷新把历史数据覆盖成实时结果，历史视图下已暂停轮询，点「退出历史视图」即可恢复。',
        }),
        rec.note ? h('span', { class: 'dim3', text: '备注：' + text(rec.note) }) : null,
      ])]);
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
      /* 统计行只改文本，原位改写即可（历史列表刷新时不闪） */
      paint(histStatsHost, [
        h('span', { class: 'dim3', text: parts.join(' · ') }),
        st.hist.note ? h('span', { class: 'dim3', text: st.hist.note }) : null,
      ]);
    }

    /* 备注编辑器：行内 input / 按钮交给 morph 原位保留 ——
       刷新时结构一致，输入框节点不会被换掉，所以正在输入的备注不丢、焦点也不会被抢走
       （morph 会跳过正处于焦点中的控件，不去改写它的 value）。 */
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

    /* 历史记录表实例 / 列指纹 / 配置对象（mount 内缓存）：
       loadHistory 每次都先置 loading 再重渲染，重建表体会连带把备注输入框冲掉；
       这里首次挂载后只 update(rows)，行内 input 由 morph 原位保留（不丢焦点）。 */
    let histRef = null;
    let histColsSig = '';
    let histCfg = null;

    function renderHistory() {
      if (st.hist.loading && histRef) return;    /* 已有列表：保留当前内容，等结果回来原位更新 */
      if (st.hist.loading) { paint(histTableHost, [ui.loading('历史记录加载中…')]); return; }
      if (st.hist.error) {
        paint(histTableHost, [ui.empty('历史记录暂不可用：' + st.hist.error + '（接口 /api/advisor/history）')]);
        histRef = null;
        histColsSig = '';
        histCfg = null;
        return;
      }
      if (!st.hist.rows.length) {
        const f = st.hist.filter;
        const filtered = !!(f.market || f.action || f.q || f.pinned);
        paint(histTableHost, [ui.empty(filtered
          ? '没有符合筛选条件的历史记录：可放宽筛选或点「刷新」重试'
          : '暂无历史记录：保持「自动保存到历史记录」开启，提交一次「开始 AI 分析」后即会出现在这里')]);
        histRef = null;
        histColsSig = '';
        histCfg = null;
        return;
      }
      const cols = buildHistCols();
      const sig = colsSig(cols);
      if (!histRef) {
        histCfg = {
          cols,
          rows: st.hist.rows,
          compact: true,
          maxHeight: '340px',
          rowKey: (r) => r.id,
          activeKey: st.history ? st.history.id : null,
          emptyText: '没有符合条件的历史记录',
        };
        histRef = ui.tbl(histCfg);
        histColsSig = sig;
        paint(histTableHost, []);                /* 只删节点，不重建表格 */
        histTableHost.appendChild(histRef);      /* 首次挂载；之后只 update，绝不重复挂载 */
        return;
      }
      /* 当前记录高亮：行类名在 update 时按最新的 activeKey 重算 */
      histCfg.activeKey = st.history ? st.history.id : null;
      if (sig !== histColsSig) { histRef.setCols(cols); histColsSig = sig; }
      histRef.update(st.hist.rows);
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
      bannerHost.style.display = '';
      paint(bannerHost, [ui.loading('历史记录载入中…（#' + id + '）')]);
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
        /* 历史视图：关闭推送订阅（即使有回调漏进来，applyXxx 也会因 st.history 非空直接返回） */
        stopPush('历史视图：已暂停实时推送与自动刷新，退出历史视图后自动恢复。');
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
          paint(bannerHost, []);
          /* 失败提示原位改写；旧表实例随之作废，下次渲染时重建一次 */
          paint(tableHost, [ui.empty('历史记录载入失败：' + e.message + '（接口 /api/advisor/record）')]);
          tableRef = null;
          tableColsSig = '';
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
      /* 退出历史视图：按开关状态恢复推送订阅（有实时结果才订阅） */
      if (st.push.on && st.submitted && st.lastBody) startPush(st.lastBody);
      else {
        paintPushChip(st.push.on ? null : 'closed',
          st.push.on ? null : '实时推送已关闭：数据仅靠 60 秒自动刷新。');
      }
      renderPushNote();
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
      ui.section('标的与参数', '标的支持代码（600519 / AAPL）、中文名与简称、拼音首字母（贵州茅台 / 茅台 / gzmt），也可写「代码:名称」；逗号 / 空格 / 换行分隔，也可从自选股导入。识别结果会自动补全名称，命名有歧义时请你选择', [], formHost),
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
    renderResolve();        /* 初始：空输入 → 「识别结果」区块只显示引导文案 */
    renderTable();
    renderPortfolio();
    renderBanner();
    renderHistBar();
    renderHistStats();
    renderHistory();
    renderReview();
    renderPushNote();       /* 初始提示（此时尚未订阅） */
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
        closePush();                                    /* 关闭两路推送订阅，之后不再有任何回调 */
        if (timer) { clearInterval(timer); timer = null; }
        if (resolveTimer) { clearTimeout(resolveTimer); resolveTimer = null; }   /* 识别去抖 */
        flashTimers.splice(0).forEach((t) => clearTimeout(t));
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.advisor = { mount };
})();
