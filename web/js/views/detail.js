/* ==========================================================================
   视图 · 个股详情（行情 / 盘口 / K线 / 资金流 / 信号雷达）

   实时推送：GET /api/stream/quotes?market=cn&symbols=<code>&interval=3（web/js/stream.js）
     · quotes 事件只就地刷新页头的现价 / 涨跌 / 开高低 / 成交量额与「行情时间」标注，
       不动 K 线、不动 AI 叠加层，也不整块重绘页头；
     · 连不上或断线会自动降级为轮询（用页面既有的 6 秒定时器兜底），只影响状态 chip；
     · destroy() 时关闭订阅。

   无感刷新：本页所有刷新（定时轮询 / 手动刷新 / 切换周期复权）都不重建 DOM——
     · 图表实例按「类型 + 标的 + 周期 + 复权」复用，只有换口径才重建，数据用 setData 增量喂；
     · 头部、关键指标、五档盘口、信号雷达、图例都走 paint()/reconcile() 原位改写，
       只有文本真的变了才写 DOM，因此没有闪动，也不会丢 hover / 滚动 / 焦点；
     · 刷新中/刷新失败用容器右上角的小标签提示，不再用「加载中」替换内容。
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct, paint, reconcile } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;
  const isNum = window.AD.isNum;

  const QUOTE_PUSH_SEC = 3;            /* 行情推送 interval（秒） */
  const QUOTE_FALLBACK_MS = 15000;     /* 降级轮询间隔 */
  const QUOTE_FALLBACK_MIN_GAP = 5000; /* 降级轮询与既有定时器的最小间隔，避免重复请求 */

  const PERIODS = [
    { value: 'trend', label: '分时' }, { value: '5m', label: '5分' }, { value: '15m', label: '15分' },
    { value: '30m', label: '30分' }, { value: '60m', label: '60分' },
    { value: 'day', label: '日K' }, { value: 'week', label: '周K' }, { value: 'month', label: '月K' },
  ];
  const SUBS = [{ value: 'MACD', label: 'MACD' }, { value: 'KDJ', label: 'KDJ' }, { value: 'RSI', label: 'RSI' }, { value: '', label: '关闭' }];

  /* ------------------------------------------------------------------
     AI 研判（个股接入）
     接口：POST /api/advisor/recommend（api.advisorRecommend）
     约定：字段缺失一律降级为「—」，绝不臆造数值；接口失败只提示，不影响原有功能
     ------------------------------------------------------------------ */

  /* 建议档位：文案与 chip 配色，只复用项目已有 chip 变体 */
  const AD_ACTION_LABEL = {
    buy: '买入', add: '增持', hold: '持有', reduce: '减仓', sell: '卖出', watch: '观望', avoid: '回避',
  };
  const AD_ACTION_CLS = {
    buy: 'chip up', add: 'chip up', hold: 'chip', reduce: 'chip warn',
    sell: 'chip down', watch: 'chip accent', avoid: 'chip warn',
  };
  /* 因子方向 -> 涨跌色（兼容服务端多种写法） */
  const AD_DIR_CLS = {
    up: 'up', bull: 'up', bullish: 'up', long: 'up', buy: 'up', pos: 'up', positive: 'up',
    down: 'down', bear: 'down', bearish: 'down', short: 'down', sell: 'down', neg: 'down', negative: 'down',
  };
  const AD_PARAM_DEFAULT = { horizon: 20, capital: 100000, kellyFraction: 0.5, maxWeight: 0.25 };
  const AD_FACTOR_MAX = 5;
  const AD_AUTO_DELAY = 300;          /* 日线就绪后延迟一小段再自动研判，避开首屏渲染 */
  const AD_DISCLAIMER = '本区结论由服务端统计模型基于公开行情数据自动计算，仅用于技术研究与学习，不构成任何投资建议；' +
    '模型存在失效风险，历史统计不代表未来表现。';

  function adText(v, d) {
    if (v === null || v === undefined || v === '') return d === undefined ? '—' : d;
    return String(v);
  }
  /* 百分比口径兼容：0.25 与 25 都按 25% 展示（服务端口径未定时不做臆造） */
  function adPct(v) {
    if (!isNum(v)) return null;
    return Math.abs(v) <= 1.5 ? v * 100 : v;
  }
  /* 服务端有一部分字段**本身就是百分数**（risk.atrPct = 1.494 表示 1.494%），
     绝不能走 adPct 的「0.25 → 25%」归一：阈值 1.5 会把 1.494% 当成小数再放大 100 倍，
     实测详情页因此显示过「ATR% 149.40%」。这类字段一律用 rawPctText 原样格式化。
     适用：risk.atrPct / risk.vol（risk.maxDrawdown、forecast.expectedReturn 同理，
     它们各自已经在用 F.num / F.pct 原样输出）。 */
  function rawPctText(v, d) {
    return isNum(v) ? F.num(v, d === undefined ? 2 : d) + '%' : '—';
  }
  function adDirCls(d) { return AD_DIR_CLS[String(d || '').toLowerCase()] || ''; }
    /* 服务端 updated 为时间戳 / 时间串时按其展示，缺失时退回本地时间 */
    function adUpdatedText(v) {
      if (isNum(v)) return F.clock(v);
      if (typeof v === 'string' && v) return v.indexOf(':') >= 0 ? F.hhmmss(v) : v;
      return F.clock(Date.now());
    }
  /* 本地「YYYY-MM-DD HH:MM:SS」：用于展示「最近保存」时间（F.clock 只有时分秒） */
  function adDateTime(ts) {
    const d = new Date(isNum(ts) ? ts : Date.now());
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function adActionKey(r) { return String((r && r.action) || '').toLowerCase(); }
  function adActionText(r) {
    return AD_ACTION_LABEL[adActionKey(r)] || adText(r && r.actionText, '—');
  }

  function scoreRing(score) {
    const r = 38, c = 2 * Math.PI * r;
    const pctv = Math.abs(score) / 100;
    const color = score > 0 ? 'var(--up)' : (score < 0 ? 'var(--down)' : '#8b95a5');
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 92 92');
    svg.setAttribute('width', '92');
    svg.setAttribute('height', '92');
    svg.style.flex = '0 0 auto';
    const mk = (tag, attrs) => {
      const el = document.createElementNS(svgNS, tag);
      Object.keys(attrs).forEach((k) => el.setAttribute(k, attrs[k]));
      return el;
    };
    svg.appendChild(mk('circle', { cx: 46, cy: 46, r, fill: 'none', stroke: '#1b202b', 'stroke-width': 8 }));
    svg.appendChild(mk('circle', {
      cx: 46, cy: 46, r, fill: 'none', stroke: color, 'stroke-width': 8, 'stroke-linecap': 'round',
      'stroke-dasharray': (c * pctv).toFixed(2) + ' ' + c.toFixed(2),
      transform: 'rotate(-90 46 46)',
    }));
    const wrap = h('div', { class: 'score-ring' });
    wrap.appendChild(svg);
    wrap.appendChild(h('div', { class: 'val ' + (score > 0 ? 'up' : score < 0 ? 'down' : 'flat') }, [
      String(score > 0 ? '+' + score : score),
      h('small', { text: score > 0 ? '偏多信号' : score < 0 ? '偏空信号' : '中性' }),
    ]));
    return wrap;
  }

  function metric(k, v, cls, small) {
    return h('div', { class: 'metric' }, [
      h('div', { class: 'k', text: k }),
      h('div', { class: 'v ' + (cls || '') }, [String(v), small ? h('small', { text: small }) : null]),
    ]);
  }

  function mount(root, ctx) {
    const sym = ctx.state.symbol;
    if (!sym || !sym.code) {
      root.appendChild(h('div', { class: 'page' }, [
        ui.pageHead('个股详情', '从市场总览、自选股、选股器或搜索（⌘K）中选择一只标的'),
        ui.empty('尚未选择标的'),
      ]));
      return { refresh() {}, destroy() {} };
    }
    const market = sym.market;
    const code = sym.code;
    const st = {
      period: 'trend', fq: 1, showMA: true, showBOLL: false, sub: 'MACD',
      quote: null, kline: null, trends: null, orderbook: null, fundflow: null, analysis: null,
      /* AI 研判：row 为服务端逐只结果，advisorChart 为 K 线叠加层数据 */
      advisorRow: null, advisorChart: null, advisorOn: true, advisorLoaded: false,
      advisorLoading: false, advisorParams: null, advisorDisclaimer: '', advisorUpdated: null,
      /* 手动保存本次研判到「AI 选股」历史记录（不自动保存，避免每次打开详情都落库） */
      advisorSaving: false, advisorSaved: null,
      destroyed: false,
    };
    let chart = null;
    let flowChart = null;
    let timer = null;
    let advisorTimer = null;
    let quoteStream = null;      /* 行情推送句柄 */
    let updHost = null;          /* 「行情时间 …」标注节点，推送时只改它的文本 */
    let lastQuoteAt = 0;         /* 最近一次行情请求时间（含既有定时器），给降级轮询去重 */

    const headHost = h('div');
    const legendHost = h('div', { class: 'chart-legend' });
    const canvasHost = h('div', { class: 'chart-canvas-wrap' });
    const obHost = h('div');
    const metricHost = h('div', { class: 'metric-list' });
    /* 资金流：图表容器与文字说明分开，刷新时各自原位更新，画布不会被文字节点挤掉 */
    const flowChartHost = h('div', { style: { height: '200px' } });
    const flowNoteHost = h('div');
    const flowHost = h('div', {}, [flowChartHost, flowNoteHost]);
    const signalHost = h('div');
    const indHost = h('div', { class: 'metric-list' });
    const metaHost = h('span', { class: 'hint' });
    const advisorBody = h('div');
    const advisorParamHost = h('div');
    const advisorHint = h('span', { class: 'dim3', text: '待研判' });
    /* 「最近保存」说明行（仅手动保存成功后出现） */
    const advisorSavedHost = h('div', { class: 'legend-inline', style: { margin: '8px 0 4px', lineHeight: '1.8' } });
    const advFields = {};

    /* ---------------------------------------------------------- 头部 */

    function updText(q) {
      return '行情时间 ' + ((q && q.updated) || '—') + (q && q.stale ? ' · 数据可能延迟' : '');
    }

    /* 页头：结构不变，只改写变化的文本（paint 会保留原有节点与监听） */
    function renderHead() {
      const q = st.quote || {};
      const d = F.dir(q.changePct);
      updHost = h('span', { class: 'qh-upd', text: updText(q) });
      const stat = (k, v, cls) => h('div', { class: 'qh-stat' }, [
        h('div', { class: 'k', text: k }),
        h('div', { class: 'v ' + (cls || ''), text: v }),
      ]);
      paint(headHost, [
        h('div', { class: 'quote-head' }, [
          h('div', { class: 'qh-id' }, [
            h('div', { class: 't1' }, [
              h('h1', { text: q.name || code }),
              ui.cells.star(ctx.isWatched(market, code), () => { ctx.toggleWatch(market, code, q.name || code); renderHead(); }),
            ]),
            h('div', { style: { marginTop: '3px', display: 'flex', gap: '7px', alignItems: 'center' } }, [
              h('span', { class: 'code', text: (market === 'us' ? 'US:' : '') + code }),
              h('span', { class: 'chip', text: market === 'us' ? '美股' : (code[0] === '6' ? '沪市' : code[0] === '3' ? '创业板' : code[0] === '8' || code[0] === '4' ? '北交所' : '深市') }),
              q.source ? h('span', { class: 'chip', text: q.source }) : null,
            ]),
          ]),
          h('div', { class: 'qh-px' }, [
            h('div', { class: 'big ' + d, text: F.price(q.price, market) }),
            h('div', { class: 'chg ' + d }, [
              h('span', { text: F.signed(q.change, 2) }),
              h('span', { text: F.pct(q.changePct) }),
            ]),
          ]),
          h('div', { class: 'qh-stats' }, [
            stat('今开', F.price(q.open, market), F.dir((q.open || 0) - (q.prevClose || 0))),
            stat('最高', F.price(q.high, market), ''),
            stat('最低', F.price(q.low, market), ''),
            stat('昨收', F.price(q.prevClose, market), 'dim'),
            stat('成交量', F.vol(q.volume, market)),
            stat('成交额', F.amt(q.amount, market)),
            stat('换手率', F.num(q.turnover, 2) + '%'),
            stat('量比', F.num(q.volumeRatio, 2)),
            stat('振幅', F.num(q.amplitude, 2) + '%'),
            stat('总市值', F.cap(q.marketCap, market)),
            stat('PE(TTM)', F.num(q.peTtm, 1)),
            stat('PB', F.num(q.pb, 2)),
          ]),
          h('div', { class: 'qh-actions' }, [
            h('button', { class: 'btn sm', text: '加自选', on: { click: () => { ctx.toggleWatch(market, code, q.name || code); renderHead(); } } }),
            h('button', { class: 'btn sm', text: '设预警', on: { click: () => ctx.openAlertFor(market, code, q.name || code) } }),
            h('button', { class: 'btn sm', text: '回测', on: { click: () => ctx.openBacktest(market, code, q.name || code) } }),
            h('button', { class: 'btn sm', text: '持续跟踪', on: { click: () => ctx.openTracker(market, code, q.name || code) } }),
          ]),
        ]),
        h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
          updHost,                       /* 「行情时间 …」，推送时就地更新这一个节点 */
          q.week52High ? '52周最高 ' + F.price(q.week52High, market) : '',
          q.week52Low ? '52周最低 ' + F.price(q.week52Low, market) : '',
          q.avgPrice ? '均价 ' + F.price(q.avgPrice, market) : '',
        ]),
      ]);
      /* paint 之后节点可能被复用，重新取一次引用，保证推送刷新的是页面上那个节点 */
      updHost = headHost.querySelector('.qh-upd') || updHost;
    }

    /* ------------------------------------------------------ 实时行情推送 */

    /* 连接状态 chip：放在页头（AI 研判区块的提示行旁边同款文案，共用 stream.js 的 chip 工厂） */
    const quoteChipHost = h('span');

    function paintQuoteChip(state, title) {
      clear(quoteChipHost);
      const s = window.AD.stream;
      if (!state || !s || typeof s.chip !== 'function') return;
      quoteChipHost.appendChild(s.chip(state, title || (
        '数据来自 /api/stream/quotes（本标的行情推送，interval=' + QUOTE_PUSH_SEC + ' 秒）；'
        + '无实质性变化时不重复推送，断线会自动降级为页面既有的定时轮询。')));
    }

    /* 推送行情落到页头：只改文本与涨跌色 class，不重绘页头 */
    function paintQuoteLive() {
      const q = st.quote || {};
      const d = F.dir(q.changePct);
      const big = headHost.querySelector('.qh-px .big');
      if (big) {
        big.className = 'big ' + d;
        big.textContent = F.price(q.price, market);
      }
      const chg = headHost.querySelector('.qh-px .chg');
      if (chg && chg.children.length >= 2) {
        chg.className = 'chg ' + d;
        chg.children[0].textContent = F.signed(q.change, 2);
        chg.children[1].textContent = F.pct(q.changePct);
      }
      /* 今开 / 最高 / 最低 / 昨收 / 成交量 / 成交额：顺序与 renderHead 里的 stat() 一致 */
      const stats = headHost.querySelectorAll('.qh-stats .qh-stat .v');
      const vals = [
        F.price(q.open, market), F.price(q.high, market), F.price(q.low, market),
        F.price(q.prevClose, market), F.vol(q.volume, market), F.amt(q.amount, market),
      ];
      vals.forEach((v, i) => {
        const el = stats[i];
        if (!el) return;
        if (i === 0) el.className = 'v ' + F.dir((q.open || 0) - (q.prevClose || 0));
        if (el.textContent !== v) el.textContent = v;
      });
      if (updHost) updHost.textContent = updText(q);
    }

    /* 只覆盖推送里真的带了的字段，其余沿用 REST 结果（避免把接口独有字段冲空） */
    function applyLiveQuote(row) {
      const next = Object.assign({}, st.quote || {});
      ['price', 'change', 'changePct', 'open', 'high', 'low', 'prevClose',
        'volume', 'amount', 'updated', 'source'].forEach((k) => {
        if (row[k] !== undefined && row[k] !== null) next[k] = row[k];
      });
      st.quote = next;
      paintQuoteLive();
    }

    /* 降级轮询：既有定时器就是兜底，这里只在它刚拉过时跳过，避免重复请求 */
    function fallbackQuoteTick() {
      if (st.destroyed || !root.isConnected) return;
      if (Date.now() - lastQuoteAt < QUOTE_FALLBACK_MIN_GAP) return;
      return loadQuote();
    }

    function stopQuoteStream() {
      if (quoteStream && typeof quoteStream.close === 'function') quoteStream.close();
      quoteStream = null;                     /* close() 幂等，之后不会再有任何回调 */
    }

    function startQuoteStream() {
      const s = window.AD.stream;
      if (!s || typeof s.quotes !== 'function' || st.destroyed) return;
      stopQuoteStream();
      quoteStream = s.quotes({
        market: market,
        symbols: [code],
        interval: QUOTE_PUSH_SEC,
        onQuotes: (payload) => {
          if (st.destroyed) return;
          const rows = (payload && Array.isArray(payload.rows)) ? payload.rows : [];
          const want = String(code).toUpperCase();
          /* 只认本标的（代码一致），推送里出现别的代码一律忽略，避免串行情 */
          const hit = rows.find((r) => String((r && r.code) || '').toUpperCase() === want);
          if (hit) applyLiveQuote(hit);
        },
        /* 推送出错不打扰用户：既有定时器仍在拉行情，状态 chip 会说明已降级 */
        onError: () => {},
        onStatus: (state) => { if (!st.destroyed) paintQuoteChip(state); },
        fallbackMs: QUOTE_FALLBACK_MS,
        fallbackTick: fallbackQuoteTick,
      });
    }

    /* ---------------------------------------------------------- 盘口 */

    function renderOrderbook() {
      const ob = st.orderbook;
      if (!ob || !ob.supported) {
        paint(obHost, [
          h('div', { class: 'legend-inline' }, [
            (ob && ob.reason) || '五档盘口数据暂不可用',
          ]),
          st.quote ? h('div', { style: { marginTop: '10px' } }, [
            h('div', { class: 'ob-mid' }, [
              h('span', { text: '外盘 ' + F.vol(st.quote.outer, market) }),
              h('span', { text: '内盘 ' + F.vol(st.quote.inner, market) }),
            ]),
          ]) : null,
        ]);
        return;
      }
      const maxVol = Math.max.apply(null, ob.asks.concat(ob.bids).map((x) => x.volume || 0).concat([1]));
      const kids = [];
      ob.asks.slice().reverse().forEach((a, i) => {
        const lvl = 5 - i;
        kids.push(h('div', { class: 'ob-row' }, [
          h('span', { class: 'lvl', text: '卖' + lvl }),
          h('span', { class: 'px down', text: F.price(a.price, market) }),
          h('span', { class: 'vol', text: F.vol(a.volume, market) }),
          h('div', { class: 'fill', style: { width: ((a.volume || 0) / maxVol * 100).toFixed(1) + '%', background: 'var(--down)' } }),
        ]));
      });
      kids.push(h('div', { class: 'ob-sep' }));
      const q = st.quote || {};
      kids.push(h('div', { class: 'ob-mid' }, [
        h('span', { class: F.dir(q.changePct), text: F.price(q.price, market) + '  ' + F.pct(q.changePct) }),
        h('span', { class: 'dim3', text: '均价 ' + F.price(ob.avgPrice || q.avgPrice, market) }),
      ]));
      kids.push(h('div', { class: 'ob-sep' }));
      ob.bids.forEach((b, i) => {
        kids.push(h('div', { class: 'ob-row' }, [
          h('span', { class: 'lvl', text: '买' + (i + 1) }),
          h('span', { class: 'px up', text: F.price(b.price, market) }),
          h('span', { class: 'vol', text: F.vol(b.volume, market) }),
          h('div', { class: 'fill', style: { width: ((b.volume || 0) / maxVol * 100).toFixed(1) + '%', background: 'var(--up)' } }),
        ]));
      });
      paint(obHost, [
        h('div', { class: 'ob' }, kids),
        h('div', { class: 'legend-inline', style: { marginTop: '10px' } }, [
          '委比参考：外盘 ' + F.vol(ob.outer, market) + ' / 内盘 ' + F.vol(ob.inner, market),
        ]),
      ]);
    }

    /* ------------------------------------------------------- 关键指标 */

    function renderMetrics() {
      const q = st.quote || {};
      const items = [
        ['今开', F.price(q.open, market)], ['昨收', F.price(q.prevClose, market)],
        ['涨停价', F.price(q.limitUp, market)], ['跌停价', F.price(q.limitDown, market)],
        ['成交量', F.vol(q.volume, market)], ['成交额', F.amt(q.amount, market)],
        ['换手率', F.num(q.turnover, 2) + '%'], ['量比', F.num(q.volumeRatio, 2)],
        ['振幅', F.num(q.amplitude, 2) + '%'], ['总市值', F.cap(q.marketCap, market)],
        ['流通市值', F.cap(q.floatCap, market)], ['PE(动)', F.num(q.pe, 1)],
        ['PE(TTM)', F.num(q.peTtm, 1)], ['市净率', F.num(q.pb, 2)],
        ['均价', F.price(q.avgPrice, market)], ['货币', q.currency || '—'],
      ];
      paint(metricHost, items.map(([k, v]) => metric(k, v)));
    }

    /* --------------------------------------------------------- 图表 */

    function setPeriod(p) {
      st.period = p;
      Array.prototype.forEach.call(periodSeg.children, (b, i) => b.classList.toggle('active', PERIODS[i].value === p));
      fqSeg.style.display = (p === 'trend') ? 'none' : 'inline-flex';
      loadChart();
    }

    /* 图表实例复用：只有「形态变了」（分时 ↔ K线）才重建 canvas。
       周期 / 复权只换数据与刻度，用 setOptions + setData 就地更新，切换时不会闪。
       数据请求期间旧图一直留在页面上，新数据到了才在同一帧内换掉。 */
    let chartKind = null;
    let chartSeries = '';          /* 最近一次成功绘制的「周期|复权」，用来判断能否保留缩放位置 */
    let legendSrc = '';            /* 图例里的数据源，onLegend 回调读它，避免闭包里的旧值 */

    /* 刷新中 / 刷新失败都用右下角之外的小标签说明，不再替换内容 */
    function chartBusy(on, msg) {
      if (!on && canvasHost.classList.contains('is-warn')) return;   /* 失败提示保留到下次刷新 */
      canvasHost.classList.toggle('is-busy', !!on);
      if (msg) canvasHost.setAttribute('data-busy', msg);
    }

    function chartNote(msg, keepOld) {
      if (keepOld) {
        canvasHost.classList.add('is-busy', 'is-warn');
        canvasHost.setAttribute('data-busy', msg);
        return;
      }
      canvasHost.classList.remove('is-busy', 'is-warn');
      paint(canvasHost, [ui.empty(msg)]);
    }

    /* 清掉空态文字节点，canvas 与 tip 一律保留 */
    function clearChartNote() {
      canvasHost.classList.remove('is-warn');
      Array.prototype.slice.call(canvasHost.childNodes).forEach((n) => {
        if (n.nodeType === 1 && n.tagName !== 'CANVAS' && !n.classList.contains('chart-tip')) {
          canvasHost.removeChild(n);
        }
      });
    }

    /* 图例：槽位复用，只改文本（鼠标在图上移动时会高频调用，不能重建节点） */
    function paintLegend(rows, src) {
      const list = (rows || []).map((r, i) => ({ key: 'r' + i, tag: 'i', text: r.text, color: r.color }));
      list.push({ key: 'src', tag: 'span', text: '数据源：' + (src || '—'), cls: 'dim3' });
      reconcile(legendHost, list, {
        key: (it) => it.key,
        render: (it) => (it.tag === 'i'
          ? h('i', { style: { color: it.color }, text: it.text })
          : h('span', { class: it.cls, style: { marginLeft: 'auto' }, text: it.text })),
      });
    }

    async function loadChart(opts) {
      const o = opts || {};
      const isTrend = st.period === 'trend';
      const kind = isTrend ? 'trend' : 'kline';
      const series = isTrend ? 'trend' : (st.period + '|' + st.fq);
      /* 本视图一次只挂一只标的（换标的会重新 mount），所以复用只看「分时 / K线」这一层 */
      const reuse = !o.force && !!chart && chartKind === kind;
      const keepView = reuse && chartSeries === series;
      chartBusy(true, reuse ? '更新中…' : '加载中…');
      try {
        if (isTrend) {
          const res = await api.trends(market, code);
          if (st.destroyed || !root.isConnected) return;
          const pc = res.prevClose || (st.quote && st.quote.prevClose);
          if (!res.points || !res.points.length) {
            chartNote('暂无分时数据：' + (res.error || '数据源暂不可用'), reuse);
            return;
          }
          st.trends = res;
          legendSrc = res.source || '';
          if (!reuse) {
            if (chart) { chart.destroy(); chart = null; }
            chart = window.AD.chart.trend(canvasHost, {
              height: 340, prevClose: pc, market,
              onLegend: (rows) => paintLegend(rows, legendSrc),
            });
            chartKind = 'trend';
          }
          chart.setData(res.points, { prevClose: pc });
          chartSeries = series;
          metaHost.textContent = '分时 · ' + (res.source || '') + ' · 昨收 ' + F.price(pc || 0, market);
          loadSignals();
        } else {
          const res = await api.kline(market, code, st.period, st.fq, 320);
          if (st.destroyed || !root.isConnected) return;
          if (!res.bars || !res.bars.length) {
            chartNote('暂无K线数据：' + (res.error || '数据源暂不可用'), reuse);
            return;
          }
          st.kline = res;
          legendSrc = res.source || '';
          if (!reuse) {
            if (chart) { chart.destroy(); chart = null; }
            chart = window.AD.chart.kline(canvasHost, {
              height: 430, period: st.period, market, showMA: st.showMA, showBOLL: st.showBOLL, sub: st.sub,
              onLegend: (rows) => paintLegend(rows, legendSrc),
            });
            chartKind = 'kline';
          }
          /* 周期 / 复权 / 副图 / 均线开关都只是配置，就地同步即可 */
          chart.setOptions({ period: st.period, market, showMA: st.showMA, showBOLL: st.showBOLL, sub: st.sub });
          chart.setData(res.bars, { keepView: keepView });
          chartSeries = series;
          applyAdvisorToChart(res.bars);
          /* 复权口径以「实际拿到的数据」为准：上游只有不复权数据时（如新浪兜底）
             必须把差异写出来，不能让「前复权」的标签配着不复权的价格 */
          metaHost.textContent = res.bars.length + ' 根K线 · ' + (res.source || '') + ' · 复权方式 ' +
            (['不复权', '前复权', '后复权'][st.fq] || '—') + (res.fqNote ? ' · ' + res.fqNote : '');
          if (st.period !== 'day') loadSignals();
          else runAnalysis(res.bars);
        }
        clearChartNote();
      } catch (e) {
        chartNote('图表加载失败：' + e.message, reuse);
      } finally {
        chartBusy(false);
      }
    }

    let signalsLoaded = false;
    async function loadSignals() {
      if (signalsLoaded) return;
      signalsLoaded = true;
      try {
        const res = await api.kline(market, code, 'day', 1, 320);
        if (res.bars && res.bars.length >= 30) runAnalysis(res.bars);
        else paint(signalHost, [ui.empty('日线数据不足，暂无法生成信号雷达')]);
      } catch (e) {
        paint(signalHost, [ui.empty('信号雷达数据获取失败：' + e.message)]);
      }
    }

    /* ------------------------------------------------------- 信号雷达 */

    function runAnalysis(bars) {
      const res = window.AD.quant.analyze(bars);
      st.analysis = res;
      const kids = [h('div', { class: 'score-wrap', style: { marginBottom: '12px' } }, [
        scoreRing(res.score),
        h('div', {}, [
          h('div', { style: { fontSize: '13px' }, text: '综合技术面：' + res.bias }),
          h('div', { class: 'legend-inline', style: { marginTop: '6px' } }, [
            '基于 ' + res.signals.length + ' 项信号加权（趋势 / 动能 / 超买超卖 / 量能 / 位置）',
          ]),
          h('div', { class: 'legend-inline', style: { marginTop: '4px', color: 'var(--text-3)' } }, [
            '信号仅反映历史价量统计特征，不构成买卖建议',
          ]),
        ]),
      ])];
      if (!res.signals.length) {
        kids.push(ui.empty('当前没有明显技术信号'));
      } else {
        kids.push(h('div', { class: 'signal-list' },
          res.signals.slice().sort((a, b) => Math.abs(b.weight * b.dir) - Math.abs(a.weight * a.dir)).map((s) => h('div', { class: 'signal-item' }, [
            h('span', { class: 'dotm', style: { background: s.dir > 0 ? 'var(--up)' : 'var(--down)' } }),
            h('span', { class: 'txt' }, [h('b', { text: s.name }), '　' + s.desc]),
            h('span', { class: 'w', text: (s.dir > 0 ? '+' : '-') + s.weight }),
          ]))));
      }
      paint(signalHost, kids);
      const i = res.ind || {};
      const metrics = [
        ['均线 MA5 / MA20', F.num(i.ma5, 2) + ' / ' + F.num(i.ma20, 2)],
        ['MA60', F.num(i.ma60, 2)],
        ['MACD DIF / DEA', F.num(i.dif, 3) + ' / ' + F.num(i.dea, 3)],
        ['RSI6 / RSI14', F.num(i.rsi6, 1) + ' / ' + F.num(i.rsi14, 1)],
        ['KDJ K / D / J', F.num(i.k, 1) + ' / ' + F.num(i.d, 1) + ' / ' + F.num(i.j, 1)],
        ['布林上轨 / 下轨', F.num(i.bollUp, 2) + ' / ' + F.num(i.bollLow, 2)],
        ['ATR / 波动率', F.num(i.atr, 2) + ' / ' + F.num(i.atrPct, 2) + '%'],
        ['20日高 / 低', F.num(i.hi20, 2) + ' / ' + F.num(i.lo20, 2)],
        ['60日高 / 低', F.num(i.hi60, 2) + ' / ' + F.num(i.lo60, 2)],
      ];
      paint(indHost, metrics.map(([k, v]) => metric(k, v)));
      /* 日线数据就绪 -> 自动研判一次（仅一次，不轮询） */
      ensureAdvisor();
    }

    /* ------------------------------------------------------- AI 研判 */

    /* api.advisorRecommend 未接入时兜底直接 POST /api/advisor/recommend */
    function recommend(body) {
      if (typeof api.advisorRecommend === 'function') return api.advisorRecommend(body);
      if (typeof api.post === 'function') return api.post('advisor/recommend', body);
      return Promise.reject(new Error('api.advisorRecommend 未接入'));
    }

    /* 参数以输入框为唯一真源（点击按钮不一定触发 blur） */
    function readAdvisorParams() {
      const pick = (el, def, positive) => {
        const v = el ? Number(String(el.value).trim()) : NaN;
        if (!isFinite(v)) return def;
        if (positive && v <= 0) return def;
        return v;
      };
      let maxWeight = pick(advFields.maxWeight, AD_PARAM_DEFAULT.maxWeight, true);
      if (maxWeight > 1.5) maxWeight = maxWeight / 100;      /* 允许直接填 25 表示 25% */
      return {
        horizon: Math.max(1, Math.round(pick(advFields.horizon, AD_PARAM_DEFAULT.horizon, true))),
        capital: pick(advFields.capital, AD_PARAM_DEFAULT.capital, true),
        kellyFraction: pick(advFields.kellyFraction, AD_PARAM_DEFAULT.kellyFraction, true),
        maxWeight: Math.min(1, Math.max(0.01, maxWeight)),
      };
    }

    function advNumField(key, label, value, step, title) {
      const inp = h('input', {
        class: 'inp num', type: 'number', value: String(value), step: step || 'any', title: title || '',
        on: { keydown: (e) => { if (e.key === 'Enter') loadAdvisor(true); } },
      });
      advFields[key] = inp;
      return h('div', { class: 'field' }, [h('label', { text: label }), inp]);
    }

    const advKellySel = h('select', { class: 'inp', title: '对凯利公式算出的 f* 打折，越小越保守' }, [
      h('option', { value: '0.25', text: '¼ 凯利（保守）' }),
      h('option', { value: '0.5', text: '半凯利（默认）' }),
      h('option', { value: '0.75', text: '¾ 凯利' }),
      h('option', { value: '1', text: '全凯利（激进）' }),
    ]);
    advKellySel.value = String(AD_PARAM_DEFAULT.kellyFraction);
    advFields.kellyFraction = advKellySel;

    const advisorBtn = h('button', {
      class: 'btn sm', text: '重新研判',
      title: '按当前参数重新请求 AI 研判',
      on: { click: () => loadAdvisor(true) },
    });

    /* 手动保存：把本次研判结果写入「AI 选股」历史记录（详情页不做自动保存） */
    const advSaveBtn = h('button', {
      class: 'btn ghost sm', text: '保存本次研判',
      title: '把当前参数的研判结果保存到「AI 选股 → 历史记录」，可在那里回放与复盘',
      on: { click: () => saveAdvisor() },
    });

    const advisorToggle = h('button', {
      class: 'btn ghost sm active', text: '在K线上显示AI建议',
      title: '在日K上叠加 AI 建议的买卖标记、交易计划线与预测带',
      on: {
        click: (e) => {
          st.advisorOn = !st.advisorOn;
          e.currentTarget.classList.toggle('active', st.advisorOn);
          applyAdvisorToChart();
        },
      },
    });

    function renderAdvisorForm() {
      clear(advisorParamHost);
      advisorParamHost.appendChild(h('div', { class: 'run-form' }, [
        advNumField('horizon', '预测窗口（交易日）', AD_PARAM_DEFAULT.horizon, '1', '模型对未来多少个交易日做预测，默认 20'),
        advNumField('capital', '本金', AD_PARAM_DEFAULT.capital, 'any', '用于折算凯利仓位金额与股数，默认 100000'),
        h('div', { class: 'field' }, [h('label', { text: '凯利折扣' }), advKellySel]),
        advNumField('maxWeight', '单只权重上限', AD_PARAM_DEFAULT.maxWeight, '0.05', '小数或百分数：0.25 与 25 都表示 25%'),
      ]));
    }

    /* 指标卡：复用 .metric-list / .metric / .k / .v */
    function advMetrics(items) {
      const wrap = h('div', { class: 'metric-list' });
      items.forEach((it) => {
        const cell = h('div', { class: 'metric', title: it[3] || '' }, [h('div', { class: 'k', text: it[0] })]);
        const box = h('div', { class: 'v' + (it[2] ? ' ' + it[2] : '') });
        const v = it[1];
        if (v instanceof Node) box.appendChild(v);
        else box.textContent = v === null || v === undefined || v === '' ? '—' : String(v);
        cell.appendChild(box);
        wrap.appendChild(cell);
      });
      return wrap;
    }

    function advGroup(label, hint, body) {
      return h('div', { style: { marginTop: '10px' } }, [
        h('div', { class: 'legend-inline', style: { marginBottom: '6px' } }, [
          h('span', { class: 'chip', text: label }),
          hint ? h('span', { class: 'dim3', text: hint }) : null,
        ]),
        body,
      ]);
    }

    /* 策略共识：买 / 持 / 卖 票数（缺字段则降级为「—」） */
    function advConsensus(r) {
      const e = r.ensemble || {};
      if (!isNum(e.buy) && !isNum(e.hold) && !isNum(e.sell)) return '—';
      const votes = (e.votes || []).map((v) => adText(v.strategy, '?') + ' → ' + adText(v.signal, '?')).join('，');
      const box = h('span', {
        style: { display: 'inline-flex', gap: '4px' },
        title: votes || '服务端未返回逐策略票数',
      });
      if (isNum(e.buy)) box.appendChild(h('span', { class: 'chip up', text: '买 ' + e.buy }));
      if (isNum(e.hold)) box.appendChild(h('span', { class: 'chip', text: '持 ' + e.hold }));
      if (isNum(e.sell)) box.appendChild(h('span', { class: 'chip down', text: '卖 ' + e.sell }));
      return box;
    }

    /* 关键因子 chips */
    function advFactors(r) {
      const sigs = Array.isArray(r.signals) ? r.signals : [];
      if (!sigs.length) return '—';
      const box = h('span', { style: { display: 'inline-flex', gap: '4px', flexWrap: 'wrap' } });
      sigs.slice(0, AD_FACTOR_MAX).forEach((s) => {
        box.appendChild(h('span', {
          class: 'chip ' + adDirCls(s.dir),
          title: adText(s.brief, adText(s.label, '')),
          text: adText(s.label, adText(s.key, '因子')),
        }));
      });
      if (sigs.length > AD_FACTOR_MAX) {
        box.appendChild(h('span', {
          class: 'chip',
          text: '+' + (sigs.length - AD_FACTOR_MAX),
          title: sigs.slice(AD_FACTOR_MAX).map((s) => adText(s.label, s.key)).join('、'),
        }));
      }
      return box;
    }

    function renderAdvisor() {
      clear(advisorBody);
      const r = st.advisorRow;
      if (!r) {
        advisorBody.appendChild(ui.empty('暂无 AI 研判结果：可调整参数后点击「重新研判」重试'));
        return;
      }
      const p = st.advisorParams || AD_PARAM_DEFAULT;
      const key = adActionKey(r);
      const e = r.edge || {};
      const k = r.kelly || {};
      const f = r.forecast || {};
      const plan = r.plan || {};
      const risk = r.risk || {};
      const conf = adPct(r.confidence);
      const wr = adPct(e.winRate);
      const up = adPct(f.upProb);
      const w = adPct(k.weight);

      /* 结论条 */
      advisorBody.appendChild(h('div', { class: 'legend-inline', style: { alignItems: 'center', gap: '10px' } }, [
        h('span', { class: AD_ACTION_CLS[key] || 'chip', title: adText(r.actionText, ''), text: adActionText(r) }),
        h('span', { class: 'chip', text: '评分 ' + F.num(r.score, 1) }),
        h('span', { class: 'chip accent', text: '置信度 ' + (isNum(conf) ? F.num(conf, 0) + '%' : '—') }),
        h('span', { class: 'num ' + F.dir(r.changePct) }, [
          h('span', { text: '现价 ' + F.price(r.price, market) }),
          h('span', { text: '　' + F.pct(r.changePct) }),
        ]),
        h('span', {
          class: 'dim3',
          text: '窗口 h=' + p.horizon + ' · 本金 ' + F.amt(p.capital, market) +
            ' · 更新 ' + adUpdatedText(st.advisorUpdated),
        }),
      ]));

      /* 置信度进度条（复用 .prog / .prog-bar） */
      advisorBody.appendChild(h('div', { class: 'prog', style: { marginTop: '10px' }, title: '模型对该结论的置信度' }, [
        h('div', { class: 'prog-bar' }, [
          h('i', { style: { width: (isNum(conf) ? Math.max(0, Math.min(100, conf)) : 0).toFixed(1) + '%' } }),
        ]),
        h('div', { class: 'prog-text' }, [
          h('span', { text: '置信度' }),
          h('span', { text: isNum(conf) ? F.num(conf, 0) + '%' : '—' }),
        ]),
      ]));

      /* 核心结论 */
      advisorBody.appendChild(advGroup('核心结论', '档位 / 评分 / 共识 / 统计优势 / 关键因子', advMetrics([
        ['建议档位', h('span', { class: AD_ACTION_CLS[key] || 'chip', text: adActionText(r) }), '', adText(r.actionText, '')],
        ['综合评分', F.num(r.score, 1)],
        ['置信度', isNum(conf) ? F.num(conf, 0) + '%' : '—'],
        ['策略共识', advConsensus(r)],
        [
          '统计优势',
          '胜率 ' + (isNum(wr) ? F.num(wr, 1) + '%' : '—') +
            ' · 盈亏比 ' + F.num(e.payoff, 2) +
            ' · ' + (isNum(e.trades) ? e.trades + ' 笔' : '—'),
          '', '样本 ' + adText(e.sample, '—') + ' · 期望值/笔 ' + F.num(e.expectancy, 3) + ' · Edge ' + F.num(e.edge, 3),
        ],
        ['关键因子', advFactors(r)],
      ])));

      /* 预测与凯利仓位 */
      advisorBody.appendChild(advGroup('预测与凯利仓位', '窗口内期望收益 / 上涨概率 / 价格区间 · 仓位由凯利折扣与单只权重上限折算', advMetrics([
        ['期望收益', F.pct(f.expectedReturn), isNum(f.expectedReturn) ? F.dir(f.expectedReturn) : 'dim3', adText(f.note, '')],
        ['上涨概率', isNum(up) ? F.num(up, 0) + '%' : '—'],
        ['预测区间', F.price(f.bandLow, market) + ' ~ ' + F.price(f.bandHigh, market)],
        [
          '凯利权重', isNum(w) ? F.num(w, 1) + '%' : '—', '',
          '凯利 f* ' + F.num(k.fStar, 3) + ' · 折扣 ' + F.num(k.fraction, 2) + (k.note ? ' · ' + k.note : ''),
        ],
        ['仓位金额', F.amt(k.amount, market)],
        ['折算股数', isNum(k.shares) ? F.num(k.shares, 0) + ' 股' : '—'],
      ])));

      /* 交易计划 —— 必须按方向渲染标签：
         档位为减仓/卖出/回避时，服务端给的是**离场计划**（止损在上方 = 涨破则离场判断失效，
         目标在下方 = 下行参考），若照买入计划的标签显示，用户会读成
         「止损价 42.04 高于建议买入价 38.66」这种自相矛盾的买入计划（实际发生过的误读）。
         服务端已在 plan.labels 里给出各方位的正确标签，这里以它为准，缺失时按方向兜底。 */
      const pLabels = plan.labels || {};
      const isExit = plan.direction === 'exit';
      const noNewPosition = plan.tradeable === false || isExit;
      advisorBody.appendChild(advGroup('交易计划', isExit
        ? '当前档位不新开仓位：下方是**离场判断**（参考价 / 涨破即失效 / 下行目标），不是买入价与止损'
        : '入场 / 止损 / 目标位由服务端模型给出，仅作计划参考', advMetrics([
        [pLabels.entry || (isExit ? '参考价' : '建议买入'), F.price(plan.entry, market),
          '', noNewPosition ? '不新开仓位' : ''],
        [pLabels.stop || (isExit ? '离场失效价' : '止损'), F.price(plan.stop, market), isExit ? 'warn' : 'down',
          isExit ? '涨破则该判断失效' : ''],
        [pLabels.target1 || (isExit ? '下行目标1' : '目标1'), F.price(plan.target1, market), isExit ? 'down' : 'up'],
        [pLabels.target2 || (isExit ? '下行目标2' : '目标2'), F.price(plan.target2, market), isExit ? 'down' : 'up'],
        ['盈亏比', F.num(plan.riskReward, 2)],
        ['方向', (plan.side ? plan.side : adText(plan.direction, '—')) + (noNewPosition ? '（不新开仓位）' : '')],
      ])));

      /* 风险 */
      advisorBody.appendChild(advGroup('风险', 'ATR% / 年化波动 / 历史最大回撤', advMetrics([
        ['ATR%', rawPctText(risk.atrPct, 2), '', adText(risk.note, '')],
        ['年化波动', rawPctText(risk.vol, 2)],
        ['最大回撤', isNum(risk.maxDrawdown) ? '-' + F.num(Math.abs(risk.maxDrawdown), 2) + '%' : '—', 'down'],
        ['统计样本', adText(e.sample, '—')],
      ])));

      /* 服务端备注（有则展示） */
      const notes = [];
      [['统计优势', e.note], ['凯利仓位', k.note], ['价格预测', f.note], ['交易计划', plan.note], ['风险', risk.note]]
        .forEach(([label, note]) => {
          if (!note) return;
          notes.push(h('div', { class: 'legend-inline', style: { marginTop: '4px', lineHeight: '1.8' } }, [
            h('span', { class: 'dim3', text: '· ' + label + '：' + note }),
          ]));
        });
      if (notes.length) advisorBody.appendChild(h('div', { style: { marginTop: '10px' } }, notes));

      advisorBody.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '10px', lineHeight: '1.8' } }, [
        h('span', { class: 'chip warn', text: '免责声明' }),
        h('span', { class: 'dim3', text: adText(st.advisorDisclaimer, AD_DISCLAIMER) }),
      ]));
      advisorBody.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '6px' } }, [
        h('span', { class: 'dim3', text: advisorOverlayNote() }),
      ]));
    }

    /* 叠加层说明必须反映「当前周期」而不是笼统断言已叠加 ——
       分时 / 分钟线 / 周月K 都拿不到 advisor 叠加层（图表结构不同），
       曾经这里固定写「日K周期下，已…」，在分时页面上属于与事实不符的提示。 */
    function advisorOverlayNote() {
      if (st.period === 'day') {
        if (!st.advisorOn) return '日K周期下已关闭叠加：点击上方「在K线上显示AI建议」可重新开启。';
        return st.advisorChart
          ? '日K周期下，已在K线上叠加买卖标记、交易计划线与预测带。'
          : '日K周期下，暂无可叠加的AI研判结果（请先完成一次研判）。';
      }
      const label = (PERIODS.filter((p) => p.value === st.period)[0] || { label: st.period }).label;
      return '当前为「' + label + '」周期，AI 叠加层仅支持日K；切换到「日K」即可看到买卖标记、交易计划线与预测带。';
    }

    /* 服务端只返回日期字符串：映射到当前 bars 下标；映射不到就跳过该标记 */
    function mapAdvisorMarks(marks, bars) {
      const list = Array.isArray(bars) ? bars : [];
      const byT = {};
      const byDay = {};
      list.forEach((b, i) => {
        const t = String((b && b.t) || '');
        if (!t) return;
        if (byT[t] === undefined) byT[t] = i;
        const day = t.slice(0, 10);
        if (byDay[day] === undefined) byDay[day] = i;
      });
      const out = [];
      (Array.isArray(marks) ? marks : []).forEach((m) => {
        if (!m) return;
        const t = String(m.t || '');
        if (!t) return;
        let idx = byT[t];
        if (idx === undefined) idx = byDay[t.slice(0, 10)];       /* 兼容带时间与纯日期两种写法 */
        if (idx === undefined) return;
        out.push({ idx, dir: m.dir === 'buy' ? 'buy' : 'sell', label: m.label || '', kind: m.kind || '' });
      });
      return out;
    }

    /* 仅在日K上叠加：分时 / 分钟线（以及周月K）一律不设置 advisor */
    function applyAdvisorToChart(bars) {
      if (!chart || typeof chart.setAdvisor !== 'function') return;
      const ad = st.advisorChart;
      const barsNow = bars || (st.kline && st.kline.bars) || [];
      if (!st.advisorOn || !ad || st.period !== 'day') { chart.setAdvisor(null); return; }
      chart.setAdvisor({
        marks: mapAdvisorMarks(ad.marks, barsNow),
        forecast: { path: (ad.forecast && Array.isArray(ad.forecast.path)) ? ad.forecast.path : [] },
        plan: ad.plan || null,
      });
    }

    async function loadAdvisor(manual) {
      if (st.advisorLoading || st.destroyed) return;
      st.advisorLoading = true;
      const p = readAdvisorParams();
      st.advisorParams = p;
      advisorBtn.disabled = true;
      advisorHint.textContent = '模型计算中…';
      if (manual) {
        clear(advisorBody);
        advisorBody.appendChild(ui.loading('AI 研判计算中…'));
      }
      const body = {
        market,
        symbols: [{ code, market }],
        codes: [code],
        horizon: p.horizon,
        capital: p.capital,
        kellyFraction: p.kellyFraction,
        maxWeight: p.maxWeight,
      };
      try {
        const res = await recommend(body);
        /* 离开页面后不再回填：api 层不透传 AbortSignal，用 destroyed 标记等价中止 */
        if (st.destroyed) return;
        if (!res || res.ok === false) {
          throw new Error((res && (res.message || res.error)) || '服务端未返回有效结果');
        }
        const rows = Array.isArray(res.rows) ? res.rows : [];
        const mine = rows.filter((x) => x && String(x.code).toUpperCase() === code.toUpperCase());
        st.advisorRow = mine[0] || rows[0] || null;
        st.advisorChart = (st.advisorRow && st.advisorRow.advisor) || null;
        st.advisorDisclaimer = res.disclaimer || '';
        st.advisorUpdated = res.updated || null;
        renderAdvisor();
        applyAdvisorToChart();
        advisorHint.textContent = st.advisorRow
          ? '更新 ' + F.clock(Date.now()) + ' · 窗口 h=' + p.horizon + ' · 本金 ' + F.amt(p.capital, market)
          : '服务端未返回该标的的研判结果';
      } catch (err) {
        if (st.destroyed) return;
        /* 接口失败只提示：K线 / 行情 / 资金流等原有功能不受影响 */
        st.advisorRow = null;
        st.advisorChart = null;
        advisorHint.textContent = '研判失败';
        clear(advisorBody);
        advisorBody.appendChild(ui.empty('AI 研判暂不可用：' + err.message +
          '（接口 /api/advisor/recommend，不影响行情 / K线 / 资金流）'));
        applyAdvisorToChart();
        ctx.toast('AI 研判失败：' + err.message, 'err');
      } finally {
        st.advisorLoading = false;
        if (!st.destroyed) advisorBtn.disabled = false;
      }
    }

    /* 「最近保存」说明行：仅手动保存成功后展示 */
    function renderAdvisorSaved() {
      clear(advisorSavedHost);
      const s = st.advisorSaved;
      if (!s) return;
      advisorSavedHost.appendChild(h('span', { class: 'chip accent', text: '已保存' }));
      advisorSavedHost.appendChild(h('span', {
        class: 'dim3',
        text: '最近保存：' + adDateTime(s.ts) + ' #' + s.id + '（可在「AI 选股 → 历史记录」中回放与复盘）',
      }));
    }

    /* 手动保存本次研判：请求体带 save=true 与 trigger='detail'（详情页不自动保存） */
    async function saveAdvisor() {
      if (st.advisorSaving || st.destroyed) return;
      const p = readAdvisorParams();
      st.advisorSaving = true;
      advSaveBtn.disabled = true;
      const label = advSaveBtn.textContent;
      advSaveBtn.textContent = '保存中…';
      try {
        const res = await recommend({
          market,
          symbols: [{ code, market, name: (st.quote && st.quote.name) || code }],
          codes: [code],
          horizon: p.horizon,
          capital: p.capital,
          kellyFraction: p.kellyFraction,
          maxWeight: p.maxWeight,
          save: true,
          trigger: 'detail',
        });
        if (st.destroyed) return;
        if (!res || res.ok === false) {
          throw new Error((res && (res.message || res.error)) || '服务端未返回有效结果');
        }
        if (res.saved === false || !res.recordId) {
          ctx.toast('服务端未保存本次研判（' + (res.saved === false ? 'saved=false' : '未返回记录号') + '）', 'warn');
          return;
        }
        st.advisorSaved = { id: res.recordId, ts: Date.now() };
        renderAdvisorSaved();
        ctx.toast('已保存到 AI 选股记录 #' + res.recordId, 'ok');
      } catch (err) {
        if (st.destroyed) return;
        /* 保存失败只提示：不影响既有研判结果与图表 */
        ctx.toast('保存失败：' + err.message, 'err');
      } finally {
        st.advisorSaving = false;
        if (!st.destroyed) {
          advSaveBtn.disabled = false;
          advSaveBtn.textContent = label;
        }
      }
    }

    /* 进入详情页后日线数据就绪时自动研判一次（不轮询） */
    function ensureAdvisor() {
      if (st.advisorLoaded || st.destroyed) return;
      st.advisorLoaded = true;
      if (advisorTimer) clearTimeout(advisorTimer);
      advisorTimer = setTimeout(() => {
        advisorTimer = null;
        if (st.destroyed || !root.isConnected) return;
        loadAdvisor(false);
      }, AD_AUTO_DELAY);
    }

    /* ------------------------------------------------------- 资金流 */

    async function loadFlow() {
      flowBusy(true);
      try {
        const res = await api.fundflow(market, code);
        if (st.destroyed || !root.isConnected) return;
        if (!res.series || !res.series.length) {
          if (!flowChart) paint(flowChartHost, [ui.empty('资金流数据暂不可用' + (res.error ? '：' + res.error : ''))]);
          return;
        }
        st.fundflow = res;
        const data = res.series.map((x) => ({ t: x.t, v: x.main }));
        const series = [{
          name: '主力净额', data, color: res.series[res.series.length - 1].main >= 0 ? 'var(--up)' : 'var(--down)',
          fill: 'rgba(77,141,255,0.10)', width: 1.4,
          fmt: (v) => F.amt(v, market),
        }];
        /* 复用同一个折线实例，只喂新数据（重建会把 canvas 换掉，肉眼可见闪一下） */
        if (flowChart) {
          flowChart.setData(series);
        } else {
          clear(flowChartHost);                    /* 清掉先前的空态文字，再建画布 */
          flowChart = window.AD.chart.line(flowChartHost, { height: 200, zeroLine: true, fmt: (v) => F.amt(v, market), series });
        }
        const last = res.series[res.series.length - 1];
        const note = h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
          '最新（' + last.t + '）主力净额 ' + F.amt(last.main, market),
          res.limited ? '数据源：' + res.source : '含超大单 / 大单 / 中单 / 小单拆分',
        ]);
        if (!res.limited) {
          const detail = h('div', { class: 'metric-list', style: { marginTop: '10px' } },
            [['超大单', last.huge], ['大单', last.big], ['中单', last.mid], ['小单', last.small]]
              .map(([k, v]) => metric(k + '净额', F.amt(v, market), F.dir(v))));
          paint(flowNoteHost, [note, detail]);
        } else {
          paint(flowNoteHost, [note]);
        }
      } catch (e) {
        if (!flowChart) paint(flowChartHost, [ui.empty('资金流加载失败：' + e.message)]);
      } finally {
        flowBusy(false);
      }
    }

    /* 资金流刷新中的提示同样走容器角标，不动既有内容 */
    function flowBusy(on) {
      flowHost.classList.toggle('is-busy', !!on);
      if (on) flowHost.setAttribute('data-busy', flowChart ? '更新中…' : '加载中…');
    }

    /* --------------------------------------------------------- 工具栏 */

    const periodSeg = h('div', { class: 'seg' });
    PERIODS.forEach((p) => {
      periodSeg.appendChild(h('button', {
        class: st.period === p.value ? 'active' : '', text: p.label,
        on: { click: () => setPeriod(p.value) },
      }));
    });

    const fqSeg = h('div', { class: 'seg' }, [
      h('button', { class: 'active', text: '前复权', on: { click: (e) => { st.fq = 1; markSeg(fqSeg, e.target); loadChart(); } } }),
      h('button', { text: '不复权', on: { click: (e) => { st.fq = 0; markSeg(fqSeg, e.target); loadChart(); } } }),
      h('button', { text: '后复权', on: { click: (e) => { st.fq = 2; markSeg(fqSeg, e.target); loadChart(); } } }),
    ]);

    const subSeg = h('div', { class: 'seg' });
    SUBS.forEach((s) => {
      subSeg.appendChild(h('button', {
        class: st.sub === s.value ? 'active' : '', text: s.label,
        on: {
          click: (e) => {
            st.sub = s.value;
            markSeg(subSeg, e.target);
            if (chart && st.period !== 'trend') { chart.setSub(s.value); }
          },
        },
      }));
    });

    function markSeg(seg, btn) {
      Array.prototype.forEach.call(seg.children, (b) => b.classList.toggle('active', b === btn));
    }

    const maToggle = h('button', {
      class: 'btn sm active', text: '均线',
      on: {
        click: (e) => {
          st.showMA = !st.showMA;
          e.target.classList.toggle('active', st.showMA);
          if (chart && st.period !== 'trend') chart.setMA(st.showMA);
        },
      },
    });
    const bollToggle = h('button', {
      class: 'btn sm', text: 'BOLL',
      on: {
        click: (e) => {
          st.showBOLL = !st.showBOLL;
          e.target.classList.toggle('active', st.showBOLL);
          if (chart && st.period !== 'trend') chart.setBOLL(st.showBOLL);
        },
      },
    });

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('个股详情', '实时行情 · 五档盘口 · 多周期K线 · 技术信号雷达 · 资金流', [
        metaHost,
        quoteChipHost,
        h('button', {
          class: 'btn sm', text: '刷新',
          on: { click: () => { loadQuote(); loadChart(); loadFlow(); } },
        }),
      ]),
      headHost,
      h('div', { style: { height: '16px' } }),
      h('div', { class: 'grid g-2-1' }, [
        h('div', {}, [
          ui.section('价格走势', '滚轮缩放 · 拖拽平移 · 双击复位', [], h('div', { class: 'chart-panel' }, [
            h('div', { class: 'chart-toolbar' }, [periodSeg, fqSeg, h('span', { class: 'spacer' }), maToggle, bollToggle, h('span', { class: 'dim3', text: '副图' }), subSeg]),
            legendHost,
            canvasHost,
          ])),
          ui.section('AI 研判', '建议档位 / 评分 / 置信度 / 策略共识 / 统计优势 / 预测 / 凯利仓位 / 交易计划 / 关键因子 / 风险；字段缺失按「—」降级',
            [advisorHint, advisorToggle, advSaveBtn, advisorBtn],
            h('div', {}, [advisorParamHost, advisorSavedHost, advisorBody])),
          ui.section('技术信号雷达', '多指标加权评分', [], signalHost),
          ui.section('资金流向', '近 60 个交易日主力资金净额', [], flowHost),
        ]),
        h('div', {}, [
          ui.section('五档盘口', market === 'us' ? '美股提供最优买卖一档' : '买卖五档实时挂单', [], obHost),
          ui.section('关键指标', '', [], metricHost),
          ui.section('技术指标读数', '基于当前周期K线实时计算', [], indHost),
        ]),
      ]),
    ]));

    async function loadQuote() {
      lastQuoteAt = Date.now();          /* 记录请求时间：降级轮询据此避免与定时器重复拉取 */
      try {
        const q = await api.stock(market, code);
        st.quote = q;
        renderHead();
        renderMetrics();
        renderOrderbook();
      } catch (e) {
        ctx.toast('行情获取失败：' + e.message, 'err');
      }
    }

    /* AI 研判区骨架先渲染：参数表单 + 空态（数据由日线就绪后自动请求填充） */
    renderAdvisorForm();
    renderAdvisorSaved();
    renderAdvisor();

    (async () => {
      await loadQuote();
      await loadChart();
      loadFlow();
      try {
        const ob = await api.orderbook(market, code);
        st.orderbook = ob;
        renderOrderbook();
      } catch (e) { /* 忽略盘口失败 */ }
    })();

    timer = setInterval(() => {
      loadQuote().then(() => {
        if (st.period === 'trend') loadChart();
      });
    }, Math.max(6000, ctx.state.pollMs));

    /* 建立行情推送订阅（无 stream.js / 无 EventSource 时自动变为轮询，页面功能不受影响） */
    startQuoteStream();

    return {
      refresh: () => { loadQuote(); loadChart(); loadFlow(); },
      destroy() {
        st.destroyed = true;                       /* 标记离开页面：在途的 AI 研判结果不再回填 */
        stopQuoteStream();                         /* 关闭推送：之后不会再有任何回调 */
        if (timer) clearInterval(timer);
        if (advisorTimer) { clearTimeout(advisorTimer); advisorTimer = null; }
        if (chart) chart.destroy();
        if (flowChart) flowChart.destroy();
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.detail = { mount };
})();
