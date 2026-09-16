/* ==========================================================================
   视图 · 买入扫描（硬闸门 + 复合评分 + 点位参考 + 交易规则 + 复盘摘要）

   定位：本页把服务端「全市场扫描」的**真实口径**摊开给用户看 —— 扫描了多少只、
   多少只被硬闸门拦下、多少只因取样上限没被评分、哪些候选值得看、点位在哪、
   以及为什么（理由 / 提示 / 风险分）。不做预测、不承诺收益、不臆造任何数值。

   后端契约（前端只消费，字段缺失一律降级为「—」）：
     POST /api/scan/run   { market, limit, barsLimit }
       → { ok, market, params,
           candidates:[{ code, name, price, changePct, score, grade, verdict, verdict3,
                         factors:{momentum,volume,trend,position,risk}, weights,
                         reasons:[中文], warnings:[中文],
                         metrics:{amount,volumeRatio,amountRatio,turnover,ma20Dev,rsi,
                                  pct60d,distToResistance,atrPct,bars,...},
                         risk:{score,level,action,reject,flags:[{key,label,...}],summary},
                         spark:[最近N根收盘价], degraded, capped }],
           rejected:[{code,name,reasons,stage,tags}], hardRejectedSample:[...],
           missingFieldCounts:{...},
           stats:{ universe, passed, hardPassed, hardRejected, scored, returned, truncated,
                   barsRequested, barsFailed, truncatedByBarsLimit, elapsedMs,
                   byGrade, byRejectReason },
           truncatedByBarsLimit:{count,codes,note},
           note, updated, durationMs }
     GET  /api/scan/config → { ok, params, saved, defaults, note }
     POST /api/scan/config { patch:{...} } → { ok, params, saved, defaults, note }
     GET  /api/rules?market=cn
       → { ok, market, version, sources:[文本], unverified:[文本],
           boards:[{board,label,limit,lot,note}], extra:[{item,value}],
           sessions:{tz,windows:[{key,label,from,to}],note}, note, params, updated }
     GET  /api/levels?market=&code=
       → { ok, code, market, name, price, asOf, atr:{atr14,atr22,pct,basis},
           support:[{price,kind,strength,note}], resistance:[...], pivot,
           entries:[{price,weight,label,note}], targets:[{price,weight,label,note}],
           stop:{initial,trailStart,trailLockIn,chandelier,basis,note},
           riskReward:{toT1,toT2,ratio1,ratio2,verdict},
           risk:{perTradePct,shares,amount,weight,basis},
           exits:[{priority,rule,condition,action,triggered,detail}],
           limit, rules:{version,lot,minQty}, session, holding, confidence,
           warnings:[中文], note, updated }
     GET  /api/review/summary?market=
       → { ok, window:{from,to,days}, metrics:{逐笔口径}, period:{周期口径}, drawdown,
           byAdvisor, bySource, note, warnings }        ← 字段可能为 null

   为什么要分开「逐笔口径」与「周期口径」：前者按已完成交易聚合，后者按权益曲线逐 bar，
   两者在存在未平仓持仓 / 出入金 / 未落库成交时**本来就会不一致**，混在一张卡里会误读。

   为什么不做进度条：扫描是「先过闸门 → 对成交额最大的 N 只逐只取 K 线 → 打分」，
   服务端不提供阶段进度，前端只能显示**已用时**（真实计时），不画假进度。

   为什么没有 SSE：web/js/stream.js 只提供 quotes / advisor / trade 三路订阅，
   扫描 / 规则 / 复盘没有推送通道，故本视图全部按需拉取（扫描手动触发 + 在途防重）。

   destroy() 约定：置 st.destroyed → 清空计时器、置空在途标记；所有异步回调入口先判
   st.destroyed（或已过期请求号），销毁后绝不再碰 DOM。

   可测试钩子（data-* 定位）：
     [data-act]  run / limit / bars-limit / cfg-toggle / cfg-save / cfg-reset /
                 levels / open / more
     [data-host] summary / candidates / levels（配 data-code）/ disclaimer / rules /
                 review / rejected
     [data-card] 候选卡片（值为 code）
     [data-level] 点位面板内部：support / resistance / entries / stop / targets / rr /
                 risk / exits / warn
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;
  const isNum = window.AD.isNum;
  const MARKET_LABEL = window.AD.MARKET_LABEL || { cn: 'A股', us: '美股' };

  /* 免责声明：常驻在「买入扫描」区块底部（不做折叠、不藏进 tooltip） */
  const DISCLAIMER = '本页为技术面筛选与点位参考，不构成投资建议；不承诺收益；'
    + '历史统计不代表未来；模拟交易与真实成交存在差异（T+1、涨跌停、滑点、手续费）。';

  /* 等级 / 风险配色：只用 app.css 已有的 chip 配色（'' / up / down / warn / accent） */
  const GRADE_CLS = { A: 'up', B: 'accent', C: '', D: 'warn', F: 'down' };
  const RISK_ACTION_CLS = { OK: 'up', CAUTION: 'warn', AVOID: 'down' };
  const STRENGTH_CLS = { 强: 'up', 中: 'accent', 弱: '' };
  /* 因子键 → 中文短名（仅用于展示 chip，键名来自 core/scanner.py 的 FACTORS） */
  const FACTOR_LABEL = {
    momentum: '动量', volume: '量能', trend: '趋势', position: '位置', risk: '风险',
  };
  const SOURCE_LABEL = { ai: 'AI 建议', manual: '手动', scheduled: '定时调度', other: '其它' };

  const SPARK_W = 90;                 /* 迷你走势图：宽 90px */
  const SPARK_H = 24;                 /* 迷你走势图：高 24px */
  const MAX_REASONS = 3;              /* 卡片上直接显示的理由条数，超出折叠 */
  const REJ_SAMPLE_MAX = 20;          /* 剔除样例最多展示条数 */
  const REVIEW_EMPTY = '复盘摘要尚未加载：GET /api/review/summary';

  /* ------------------------------------------------------------ 小工具 */

  function text(v, d) {
    if (v === null || v === undefined || v === '') return d === undefined ? '—' : d;
    return String(v);
  }

  function arr(v) { return Array.isArray(v) ? v : []; }

  function numOf(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return isFinite(n) ? n : null;
  }

  /* 数值展示：拿不到就「—」，绝不显示 0（0 只在服务端真的返回 0 时出现） */
  function numText(v, d) { return isNum(v) ? F.num(v, d === undefined ? 2 : d) : '—'; }
  /* 比率口径（core/review.py：0.15 = 15%） */
  function ratioText(v, d) { return isNum(v) ? F.num(v * 100, d === undefined ? 1 : d) + '%' : '—'; }
  function money(v, market) { return isNum(v) ? F.amt(v, market) : '—'; }
  function cntText(v) { return isNum(v) ? String(v) : '—'; }
  /* 带单位的数量：拿不到就只显示「—」，不出现「— bar」这种半截文案 */
  function unitText(v, unit) { return isNum(v) ? v + ' ' + unit : '—'; }

  function clip(s, n) {
    const t = String(s === null || s === undefined ? '' : s);
    return t.length > n ? t.slice(0, n) + '…' : t;
  }

  /* 时间：毫秒 / 秒时间戳 / ISO 串都能吃，取不到就是「—」 */
  function timeText(v) {
    if (v === null || v === undefined || v === '') return '—';
    if (isNum(v)) return F.clock(v < 1e12 ? v * 1000 : v);
    const s = String(v);
    if (s.length >= 19) return s.slice(5, 19).replace('T', ' ');
    return s;
  }

  function msText(v) {
    if (!isNum(v)) return '—';
    return v >= 1000 ? F.num(v / 1000, 1) + ' 秒' : Math.round(v) + ' 毫秒';
  }

  function intOr(v, d, lo, hi) {
    const n = numOf(v);
    if (n === null || n <= 0) return d;
    return Math.max(lo || 1, Math.min(hi || 1000000, Math.floor(n)));
  }

  function chipEl(label, cls, title) {
    return h('span', { class: 'chip' + (cls ? ' ' + cls : ''), text: label, title: title || '' });
  }

  function cell(k, v, cls, title) {
    return h('div', { class: 'metric' }, [
      h('div', { class: 'k', text: k }),
      h('div', { class: 'v ' + (cls || ''), text: v, title: title || '' }),
    ]);
  }

  function field(label, inp, title) {
    return h('div', { class: 'field', title: title || '' }, [
      h('label', { text: label }), inp,
    ]);
  }

  function fold(summary, body, cls) {
    return h('details', {}, [
      h('summary', { class: cls || 'hint', text: summary }),
      body,
    ]);
  }

  /* 迷你走势图：原生 canvas，无第三方库。涨绿跌红（跟随项目配色变量） */
  function sparkCanvas(spark, dir) {
    const cv = h('canvas', {
      class: 'spark-canvas',
      width: SPARK_W, height: SPARK_H,
      style: { width: SPARK_W + 'px', height: SPARK_H + 'px', display: 'block', flex: '0 0 auto' },
      title: '最近 ' + arr(spark).length + ' 根收盘价走势（迷你图，仅示意形态）',
    });
    const pts = arr(spark).map(Number).filter((n) => isFinite(n));
    let ctx = null;
    try {
      if (typeof cv.getContext === 'function') ctx = cv.getContext('2d');
    } catch (e) { ctx = null; }
    if (!ctx) return cv;                                   /* 无 canvas 能力：只留空框，不崩 */
    try {
      const dpr = (window.devicePixelRatio || 1) > 1 ? 2 : 1;
      if (cv.width === SPARK_W) { cv.width = SPARK_W * dpr; cv.height = SPARK_H * dpr; }
      if (typeof ctx.setTransform === 'function') ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      else if (typeof ctx.scale === 'function') ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, SPARK_W, SPARK_H);

      let up = '#ff4d4f', down = '#12c48b';
      try {
        const cs = window.getComputedStyle(document.documentElement);
        up = (cs.getPropertyValue('--up') || up).trim() || up;
        down = (cs.getPropertyValue('--down') || down).trim() || down;
      } catch (e) { /* 取不到 CSS 变量就用兜底色 */ }
      const color = dir === 'down' ? down : up;

      if (pts.length < 2) {
        ctx.strokeStyle = '#5d677a';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(1, SPARK_H / 2);
        ctx.lineTo(SPARK_W - 1, SPARK_H / 2);
        ctx.stroke();
        return cv;
      }
      let lo = pts[0], hi = pts[0];
      for (let i = 1; i < pts.length; i++) {
        if (pts[i] < lo) lo = pts[i];
        if (pts[i] > hi) hi = pts[i];
      }
      const span = (hi - lo) || (Math.abs(hi) * 0.001) || 1;
      const pad = 2;
      const x = (i) => (i / (pts.length - 1)) * (SPARK_W - 2) + 1;
      const y = (v) => SPARK_H - pad - ((v - lo) / span) * (SPARK_H - pad * 2);

      ctx.strokeStyle = color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      for (let i = 0; i < pts.length; i++) {
        if (i === 0) ctx.moveTo(x(i), y(pts[i]));
        else ctx.lineTo(x(i), y(pts[i]));
      }
      ctx.stroke();
    } catch (e) { /* 绘图异常不影响数据展示 */ }
    return cv;
  }

  /* ============================================================ 视图 */

  function mount(root, ctx) {
    const state = (ctx && ctx.state) || {};
    const market = state.market === 'us' ? 'us' : 'cn';

    const st = {
      market: market,
      /* 扫描参数（服务端真相 + 默认值） */
      params: null, defaults: null, paramsErr: '', cfgInputs: {},
      /* 扫描运行态 */
      running: false, ran: false, result: null, resultErr: '',
      startedAt: 0, elapsed: 0, lastAt: null, lastMs: null, lastBody: null,
      /* 单只标的的点位面板：code → { open, loading, data, err } */
      levels: {},
      /* 交易规则 / 复盘摘要 */
      rules: null, rulesErr: '', rulesAt: null,
      review: null, reviewErr: '', reviewAt: null,
      destroyed: false,
    };
    let reqSeq = 0;              /* 在途扫描请求号：过期响应一律丢弃 */
    let levelSeq = 0;            /* 在途点位请求号 */
    let elapsedTimer = null;
    let cfgBusy = false;         /* 参数保存在途标记（防连点） */

    const toast = (msg, type) => {
      if (ctx && typeof ctx.toast === 'function') {
        try { ctx.toast(msg, type); } catch (e) { /* toast 异常不能影响视图 */ }
      }
    };

    /* 打开个股详情：app.js 的签名是 openSymbol(market, code, name)，
       但契约里写的是 openSymbol(code)；按函数元数兼容两种注入方式 */
    function openCode(code, name) {
      if (!code) return;
      if (!ctx || typeof ctx.openSymbol !== 'function') {
        toast('无法打开个股详情：ctx.openSymbol 未提供', 'warn');
        return;
      }
      try {
        if (ctx.openSymbol.length >= 2) ctx.openSymbol(st.market, code, name || code);
        else ctx.openSymbol(code);
      } catch (e) {
        toast('打开个股详情失败：' + (e && e.message ? e.message : e), 'err');
      }
    }

    /* ------------------------------------------------------ 接口封装 */

    function apiGet(path, params) {
      if (!api || typeof api.get !== 'function') return Promise.reject(new Error('AD.api 未就绪'));
      return api.get(path, params, { noDedupe: true });
    }
    function apiPost(path, body) {
      if (!api || typeof api.post !== 'function') return Promise.reject(new Error('AD.api 未就绪'));
      return api.post(path, body);
    }

    /* ------------------------------------------------------ 持久节点 */

    const marketChip = h('span', {
      class: 'chip',
      dataset: { act: 'market' },
      title: '市场跟随顶栏的全局切换（本页不单独维护市场状态）',
    });
    const limitInp = h('input', {
      class: 'inp', value: '20', dataset: { act: 'limit' },
      style: { width: '76px' },
      title: 'POST /api/scan/run 的 limit：排序后返回的候选数量',
    });
    const barsInp = h('input', {
      class: 'inp', value: '60', dataset: { act: 'bars-limit' },
      style: { width: '76px' },
      title: '只对成交额最大的 N 只取K线评分（barsLimit）；未纳入的标的会在统计里如实报出',
    });
    const runBtn = h('button', {
      class: 'btn primary', text: '开始扫描', dataset: { act: 'run' },
      on: { click: () => startScan() },
    });
    const statusHost = h('div', {
      class: 'monospaced', dataset: { act: 'run-hint', host: 'run-hint' },
      style: { marginTop: '8px' },
    });
    const cfgHost = h('div', { dataset: { host: 'config' }, style: { marginTop: '8px' } });
    const summaryHost = h('div', { dataset: { host: 'summary' } });
    const candHost = h('div', { dataset: { host: 'candidates' } });
    const rejectedHost = h('div', { dataset: { host: 'rejected' } });
    const disclaimerHost = h('div', {
      dataset: { host: 'disclaimer' },
      style: {
        marginTop: '14px', padding: '9px 11px', border: '1px solid var(--line)',
        borderLeft: '2px solid var(--warn)', borderRadius: 'var(--r)',
        background: 'var(--surface)', fontSize: '11.5px', color: 'var(--text-2)',
        lineHeight: '1.7',
      },
    }, [
      chipEl('免责声明', 'warn'),
      h('span', { style: { marginLeft: '8px' }, text: DISCLAIMER }),
    ]);
    const rulesHost = h('div', { dataset: { host: 'rules' } });
    const reviewHost = h('div', { dataset: { host: 'review' } });
    const refreshBtn = h('button', {
      class: 'btn sm', text: '刷新规则与复盘',
      title: '重新拉取 /api/scan/config、/api/rules、/api/review/summary（不会自动重跑重扫描）',
      on: { click: () => refreshAll(false) },
    });

    /* ------------------------------------------------ 状态行 / 在途防重 */

    function paintMarket() {
      marketChip.textContent = '市场：' + (MARKET_LABEL[st.market] || st.market) + '（跟随全局）';
    }

    function paintStatus() {
      runBtn.disabled = !!st.running;
      runBtn.textContent = st.running ? '扫描中…' : '开始扫描';
      if (st.running) {
        const secs = Math.max(0, Math.round((Date.now() - st.startedAt) / 1000));
        statusHost.textContent = '扫描中… 已用时 ' + secs + ' 秒（真实计时，不做假进度条）。' +
          '扫描会先过硬闸门，再对成交额最大的 ' + intOr(barsInp.value, 60, 1) + ' 只逐只取 K 线，' +
          '通常需要 10–60 秒，请勿重复点击。';
        return;
      }
      if (st.ran && st.lastAt) {
        statusHost.textContent = '上次扫描完成 ' + F.clock(st.lastAt) +
          ' · 耗时 ' + msText(st.lastMs) +
          (st.lastBody ? '（limit ' + st.lastBody.limit + ' / barsLimit ' + st.lastBody.barsLimit + '）' : '');
        return;
      }
      statusHost.textContent = '尚未扫描：点「开始扫描」调用 POST /api/scan/run。';
    }

    function startElapsed() {
      stopElapsed();
      elapsedTimer = setInterval(() => {
        if (st.destroyed) { stopElapsed(); return; }
        if (!st.running) { stopElapsed(); return; }
        paintStatus();
      }, 1000);
    }
    function stopElapsed() {
      if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
    }

    /* ----------------------------------------------------- 参数折叠区 */

    /* 标量参数（键名与 core/scanner.py 的 DEFAULT_SCAN_PARAMS 一致） */
    const SCALAR_FIELDS = [
      { key: 'min_volume_ratio', label: '量比下限' },
      { key: 'min_amount_ratio', label: '5/20 日均额比下限' },
      { key: 'pct60d_warn', label: '60 日涨幅扣分阈值 (%)' },
      { key: 'pct60d_reject', label: '60 日涨幅剔除阈值 (%)' },
      { key: 'max_drop_1d', label: '单日跌幅风险阈值 (%)' },
      { key: 'max_drop_2d', label: '两日累计跌幅风险阈值 (%)' },
      { key: 'turnover_high', label: '换手率上限 (%)' },
      { key: 'turnover_low', label: '换手率下限 (%)' },
      { key: 'rsi_hot', label: 'RSI 过热阈值' },
      { key: 'dist_near', label: '距阻力过近阈值 (%)' },
      { key: 'avoid_risk_score', label: '风险分 AVOID 阈值 (0–10)' },
      { key: 'reject_risk_score', label: '风险分整只剔除阈值 (0–10)' },
      { key: 'min_bars', label: 'K 线降级阈值 (根)' },
      { key: 'reject_bars', label: 'K 线剔除阈值 (根)' },
      { key: 'spark_len', label: '迷你走势取点 (根)' },
    ];
    /* 按市场取值的参数（服务端支持标量或 {cn,us} 两种写法） */
    const MARKET_FIELDS = [
      { key: 'min_amount', label: '流动性下限（成交额）', unit: '元 / 美元' },
      { key: 'max_change_pct', label: '当日涨幅上限（反追高）', unit: '%' },
    ];

    function renderConfig() {
      clear(cfgHost);
      st.cfgInputs = {};
      const p = st.params || st.defaults;
      if (!p) {
        cfgHost.appendChild(ui.empty('扫描参数未就绪：' + (st.paramsErr ||
          'GET /api/scan/config 无有效返回。参数为服务端口径，本页不自行编造默认值。')));
        return;
      }
      const grid = h('div', { class: 'filter-grid' });
      MARKET_FIELDS.forEach((f) => {
        const raw = p[f.key];
        const v = raw && typeof raw === 'object' ? numOf(raw[st.market]) : numOf(raw);
        const inp = h('input', { class: 'inp', value: v === null ? '' : String(v) });
        st.cfgInputs[f.key] = inp;
        grid.appendChild(field(
          f.label + '（' + (MARKET_LABEL[st.market] || st.market) + '，' + f.unit + '）', inp,
          '该参数按市场分别生效；此处只改当前市场的值，另一市场的当前值会一并回传'));
      });
      SCALAR_FIELDS.forEach((f) => {
        const v = numOf(p[f.key]);
        const inp = h('input', { class: 'inp', value: v === null ? '' : String(v) });
        st.cfgInputs[f.key] = inp;
        grid.appendChild(field(f.label, inp));
      });
      const saved = st.params && typeof st.params === 'object';
      cfgHost.appendChild(fold(
        '参数（展开可改反追高涨幅上限 / 流动性下限 / 风险分阈值等；保存走 POST /api/scan/config）',
        h('div', {}, [
          grid,
          h('div', { class: 'legend-inline', style: { marginTop: '10px', gap: '10px', alignItems: 'center' } }, [
            h('button', {
              class: 'btn sm', text: '保存参数', dataset: { act: 'cfg-save' },
              on: { click: () => saveConfig(false) },
            }),
            h('button', {
              class: 'btn sm ghost', text: '恢复服务端默认值', dataset: { act: 'cfg-reset' },
              title: '把服务端返回的 defaults 原样写回（不改变本页展示口径）',
              on: { click: () => saveConfig(true) },
            }),
            h('span', {
              class: 'hint dim3',
              text: saved ? '当前值来自 GET /api/scan/config' : '当前值来自 defaults（接口未返回 params）',
            }),
          ]),
          h('div', {
            class: 'hint dim3', style: { marginTop: '6px' },
            text: '硬闸门（流动性 / 反追高 / 量能）先过滤，再对通过者按成交额排序取前 N 只评分；' +
              '本页所有参数键与含义都来自 core/scanner.py，前端不做二次解释。',
          }),
        ])
      ));
    }

    async function saveConfig(reset) {
      if (st.destroyed || cfgBusy) { if (cfgBusy) toast('参数正在保存，请稍候', 'info'); return; }
      const base = st.params || st.defaults;
      if (!base) { toast('参数尚未加载，未发起保存请求', 'warn'); return; }
      let patch;
      if (reset) {
        patch = st.defaults || {};
      } else {
        patch = {};
        MARKET_FIELDS.forEach((f) => {
          const inp = st.cfgInputs[f.key];
          if (!inp) return;
          const v = numOf(inp.value);
          if (v === null) return;
          const cur = base[f.key];
          const pair = {};
          if (cur && typeof cur === 'object') {
            ['cn', 'us'].forEach((k) => { const b = numOf(cur[k]); if (b !== null) pair[k] = b; });
          }
          pair[st.market] = v;               /* 只改当前市场，另一市场按已加载值回传 */
          patch[f.key] = pair;
        });
        SCALAR_FIELDS.forEach((f) => {
          const inp = st.cfgInputs[f.key];
          if (!inp) return;
          const v = numOf(inp.value);
          if (v === null) return;
          patch[f.key] = v;
        });
      }
      cfgBusy = true;
      try {
        const res = await apiPost('scan/config', { patch: patch });
        if (st.destroyed) return;
        if (res && res.params) st.params = res.params;
        if (res && res.defaults) st.defaults = res.defaults;
        renderConfig();
        toast(reset ? '已恢复服务端默认参数' : '扫描参数已保存（下次扫描生效）', 'ok');
      } catch (e) {
        if (st.destroyed) return;
        toast('参数保存失败：' + (e && e.message ? e.message : e), 'err');
      } finally {
        cfgBusy = false;
      }
    }

    /* -------------------------------------------------------- 扫描主流程 */

    async function startScan() {
      if (st.destroyed || st.running) return;          /* 在途防重：重复点击只发一次请求 */
      const body = {
        market: st.market,
        limit: intOr(limitInp.value, 20, 1, 500),
        barsLimit: intOr(barsInp.value, 60, 1, 3000),
      };
      st.running = true;
      st.startedAt = Date.now();
      st.elapsed = 0;
      st.resultErr = '';
      st.levels = {};                                  /* 新扫描后旧点位面板可能过期，整体丢弃 */
      const seq = ++reqSeq;
      paintStatus();
      startElapsed();
      renderSummary();

      try {
        const res = await apiPost('scan/run', body);
        if (st.destroyed || seq !== reqSeq) return;     /* 销毁或已被更新请求覆盖 → 丢弃 */
        st.result = res || null;
        st.ran = true;
        st.lastAt = Date.now();
        st.lastMs = res && isNum(res.durationMs) ? res.durationMs : (Date.now() - st.startedAt);
        st.lastBody = body;
        const n = arr(res && res.candidates).length;
        toast('扫描完成：候选 ' + n + ' 只（耗时 ' + msText(st.lastMs) + '）', n ? 'ok' : 'warn');
      } catch (e) {
        if (st.destroyed || seq !== reqSeq) return;
        st.result = null;
        st.resultErr = (e && e.message ? e.message : String(e)) || '扫描失败';
        st.ran = true;
        st.lastAt = Date.now();
        st.lastMs = Date.now() - st.startedAt;
        st.lastBody = body;
        toast('扫描失败：' + st.resultErr, 'err');
      } finally {
        if (seq === reqSeq) {
          st.running = false;
          stopElapsed();
          if (!st.destroyed) {
            paintStatus();
            renderSummary();
            renderCandidates();
            renderRejected();
          }
        }
      }
    }

    function scanLineOf(res) {
      const stats = (res && res.stats) || {};
      const trunc = res && res.truncatedByBarsLimit && isNum(res.truncatedByBarsLimit.count)
        ? res.truncatedByBarsLimit.count : stats.truncatedByBarsLimit;
      return '本次扫描 ' + cntText(stats.universe) + ' 只'
        + '（通过硬闸门 ' + cntText(stats.hardPassed) + ' 只'
        + '，因取样上限未评分 ' + cntText(trunc) + ' 只）';
    }

    function renderSummary() {
      clear(summaryHost);
      if (st.running) {
        summaryHost.appendChild(ui.loading('扫描中… 已用时 ' +
          Math.max(0, Math.round((Date.now() - st.startedAt) / 1000)) + ' 秒。结果出来后本区域会被替换。'));
        return;
      }
      if (st.resultErr) {
        summaryHost.appendChild(ui.empty('扫描失败：' + st.resultErr +
          '（接口失败时不在本页伪造结果；可稍后重试，或把「K线取样上限」调小）'));
        return;
      }
      if (!st.result) {
        summaryHost.appendChild(ui.empty('尚未扫描（没跑过）：点上方「开始扫描」。' +
          '这一空态表示本次会话还没有发起过 POST /api/scan/run，与「跑了但没候选」是两回事。'));
        return;
      }
      const res = st.result;
      const stats = res.stats || {};
      const limit = arr(res.candidates).length;

      summaryHost.appendChild(h('div', { class: 'monospaced', style: { marginBottom: '8px' } }, [
        h('span', { text: scanLineOf(res) }),
        h('span', { class: 'dim3', text: '　·　数据截至 ' + timeText(res.updated) }),
        h('span', { class: 'dim3', text: '　·　耗时 ' + msText(isNum(res.durationMs) ? res.durationMs : stats.elapsedMs) }),
      ]));

      const chips = h('div', { class: 'legend-inline', style: { marginBottom: '8px', alignItems: 'center' } });
      if (isNum(stats.returned)) chips.appendChild(chipEl('返回候选 ' + stats.returned, 'accent'));
      if (isNum(stats.scored)) chips.appendChild(chipEl('完成评分 ' + stats.scored));
      if (isNum(stats.hardRejected)) chips.appendChild(chipEl('硬闸门剔除 ' + stats.hardRejected, 'warn'));
      if (stats.byGrade && typeof stats.byGrade === 'object') {
        ['A', 'B', 'C', 'D', 'F'].forEach((g) => {
          if (isNum(stats.byGrade[g]) && stats.byGrade[g] > 0) {
            chips.appendChild(chipEl(g + ' ' + stats.byGrade[g], GRADE_CLS[g]));
          }
        });
      }
      if (isNum(stats.truncated) && stats.truncated > 0) {
        chips.appendChild(chipEl('因 limit 截断 ' + stats.truncated, 'warn'));
      }
      if (isNum(stats.barsFailed) && stats.barsFailed > 0) {
        chips.appendChild(chipEl('K线取样失败 ' + stats.barsFailed + ' 只', 'warn'));
      }
      if (chips.children.length) summaryHost.appendChild(chips);

      const grid = h('div', { class: 'metric-list' });
      grid.appendChild(cell('候选返回 / 截断', cntText(stats.returned) + ' / ' + cntText(stats.truncated),
        '', '服务端 stats.returned / stats.truncated'));
      grid.appendChild(cell('硬闸门通过 / 剔除',
        cntText(stats.hardPassed) + ' / ' + cntText(stats.hardRejected), ''));
      grid.appendChild(cell('评分完成', cntText(stats.scored), '',
        '过了硬闸门且完成复合评分的数量（风险分超阈值者随后被剔除）'));
      grid.appendChild(cell('K线取样', cntText(stats.barsRequested) + ' 只请求 / ' + cntText(stats.barsFailed) + ' 只失败',
        stats.barsFailed > 0 ? 'warn' : ''));
      grid.appendChild(cell('取样上限截断', cntText(stats.truncatedByBarsLimit), 'warn',
        '通过硬闸门但未取K线评分（受 barsLimit 限制）的数量'));
      grid.appendChild(cell('全市场快照', cntText(stats.universe) + ' 只', '',
        '本轮硬闸门实际遍历的标的数（stats.universe）'));
      grid.appendChild(cell('耗时', msText(isNum(res.durationMs) ? res.durationMs : stats.elapsedMs), ''));
      grid.appendChild(cell('数据截至', timeText(res.updated), '',
        '服务端 updated（本轮扫描返回结果的生成时间）'));
      grid.appendChild(cell('市场', MARKET_LABEL[res.market] || text(res.market)));
      if (st.lastBody) {
        grid.appendChild(cell('本次参数', 'limit ' + st.lastBody.limit + ' / barsLimit ' + st.lastBody.barsLimit,
          '', '本次请求体里的 limit 与 barsLimit'));
      }
      summaryHost.appendChild(grid);

      const trunc = res.truncatedByBarsLimit;
      if (trunc && isNum(trunc.count) && trunc.count > 0) {
        summaryHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '8px', alignItems: 'center' } }, [
          chipEl('未评分 ' + trunc.count + ' 只', 'warn'),
          h('span', {
            class: 'dim3',
            text: text(trunc.note, '这些标的通过了硬闸门但未取K线评分（受 barsLimit 限制）；提高 barsLimit 可纳入。') +
              (arr(trunc.codes).length ? '　示例代码：' + arr(trunc.codes).join(', ') : ''),
          }),
        ]));
      }
      if (res.note) {
        summaryHost.appendChild(h('div', { class: 'hint dim3', style: { marginTop: '8px' }, text: res.note }));
      }
      const miss = res.missingFieldCounts;
      if (miss && typeof miss === 'object' && Object.keys(miss).length) {
        summaryHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '8px', alignItems: 'center' } }, [
          h('span', { class: 'dim3', text: '字段缺失计数（对应闸门被跳过）：' }),
        ].concat(Object.keys(miss).map((k) => chipEl(k + ' ' + miss[k], 'warn')))));
      }
      summaryHost.appendChild(h('div', { class: 'dim3', style: { marginTop: '6px' },
        text: '候选数 ' + limit + '（本页实际渲染的卡片数）；统计字段缺失时显示「—」。' }));
    }

    /* ---------------------------------------------------------- 候选卡片 */

    function gradeChip(c) {
      const g = text(c.grade);
      return chipEl('等级 ' + g, GRADE_CLS[g] || '', '等级取自服务端 grade（A–F）');
    }

    function riskChip(r) {
      if (!r || typeof r !== 'object') return chipEl('风险 —', 'warn', '服务端未返回 risk 段');
      const bits = [];
      if (isNum(r.score)) bits.push(r.score + '/10');
      if (r.level) bits.push(String(r.level));
      if (r.action) bits.push(String(r.action));
      const label = '风险 ' + (bits.length ? bits.join(' · ') : '—');
      return chipEl(label, RISK_ACTION_CLS[r.action] || 'warn',
        text(r.summary, '风险分 / 等级 / 动作均取自服务端 risk 段'));
    }

    function metricOf(c, key) { return c && c.metrics ? c.metrics[key] : null; }

    function renderCard(c) {
      const code = text(c.code, '');
      const name = text(c.name, code || '—');
      const box = h('div', {
        dataset: { card: code },
        style: {
          border: '1px solid var(--line)', borderRadius: 'var(--r)',
          background: 'var(--surface)', padding: '10px 12px', marginBottom: '8px',
        },
      });

      /* 第一行：一眼可读的结论 */
      const head = h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' } });
      head.appendChild(gradeChip(c));
      head.appendChild(h('button', {
        class: 'btn ghost sm', dataset: { act: 'open', code: code },
        text: name + '　' + code,
        title: '打开个股详情',
        style: { fontFamily: 'var(--sans)' },
        on: { click: () => openCode(code, name) },
      }));
      head.appendChild(h('span', {
        class: 'num ' + F.dir(c.changePct),
        style: { fontSize: '15px' },
        text: F.price(c.price, st.market) + '　' + F.pct(c.changePct),
      }));
      head.appendChild(sparkCanvas(c.spark, F.dir(c.changePct)));
      head.appendChild(h('span', {
        class: 'num', style: { fontSize: '15px' },
        title: '复合评分 0–100（服务端 score）',
        text: (isNum(c.score) ? '分数 ' + c.score : '分数 —'),
      }));
      head.appendChild(riskChip(c.risk));
      if (c.verdict3) {
        head.appendChild(chipEl('三档 ' + String(c.verdict3), 'accent', '服务端 verdict3（三档聚合口径）'));
      }
      if (c.degraded === true) {
        head.appendChild(chipEl('数据降级', 'warn',
          '服务端 degraded=true：因子缺数据 / 权重重新归一 / K线不足，评分精度下降'));
      }
      box.appendChild(head);

      /* 一句话结论 */
      box.appendChild(h('div', {
        style: { marginTop: '6px', fontSize: '13px', color: 'var(--text)' },
        text: text(c.verdict, '（服务端未给出 verdict）'),
      }));

      /* 因子明细（键名来自契约，权重一并挂 title） */
      const factors = (c.factors && typeof c.factors === 'object') ? c.factors : null;
      if (factors) {
        const line = h('div', { class: 'legend-inline', style: { marginTop: '6px', alignItems: 'center' } });
        Object.keys(FACTOR_LABEL).forEach((k) => {
          if (!isNum(factors[k])) return;
          const w = c.weights && isNum(c.weights[k]) ? '权重 ' + F.num(c.weights[k] * 100, 0) + '%' : '权重 —';
          line.appendChild(chipEl(FACTOR_LABEL[k] + ' ' + F.num(factors[k], 0), '', w));
        });
        if (line.children.length) box.appendChild(line);
      }

      /* 关键指标（只显示服务端确实返回的字段，缺失即「—」） */
      const grid = h('div', { class: 'pos-card', style: { marginTop: '8px' } });
      const metrics = [
        ['成交额', money(metricOf(c, 'amount'), st.market)],
        ['量比', numText(metricOf(c, 'volumeRatio'), 2)],
        ['5/20均额比', numText(metricOf(c, 'amountRatio'), 2)],
        ['换手率', isNum(metricOf(c, 'turnover')) ? F.num(metricOf(c, 'turnover'), 2) + '%' : '—'],
        ['MA20 乖离', isNum(metricOf(c, 'ma20Dev')) ? F.pct(metricOf(c, 'ma20Dev')) : '—'],
        ['RSI', numText(metricOf(c, 'rsi'), 1)],
        ['60日涨幅', isNum(metricOf(c, 'pct60d')) ? F.pct(metricOf(c, 'pct60d')) : '—'],
        ['距阻力', isNum(metricOf(c, 'distToResistance')) ? F.pct(metricOf(c, 'distToResistance')) : '—'],
        ['ATR%', isNum(metricOf(c, 'atrPct')) ? F.num(metricOf(c, 'atrPct'), 2) + '%' : '—'],
        ['K线根数', cntText(metricOf(c, 'bars'))],
      ];
      metrics.forEach((m) => {
        grid.appendChild(h('div', { class: 'cell' }, [
          h('div', { class: 'k', text: m[0] }),
          h('div', { class: 'v', text: m[1] }),
        ]));
      });
      box.appendChild(grid);

      /* 理由：最多 3 条，多的折叠 */
      const reasons = arr(c.reasons).map((r) => String(r));
      if (reasons.length) {
        const list = h('div', { class: 'signal-list', style: { marginTop: '8px' } });
        reasons.slice(0, MAX_REASONS).forEach((r) => {
          list.appendChild(h('div', { class: 'signal-item' }, [
            h('span', { class: 'dotm', style: { background: 'var(--accent)' } }),
            h('span', { class: 'txt', text: r }),
          ]));
        });
        box.appendChild(list);
        if (reasons.length > MAX_REASONS) {
          const rest = h('div', { class: 'signal-list', style: { marginTop: '6px' } });
          reasons.slice(MAX_REASONS).forEach((r) => {
            rest.appendChild(h('div', { class: 'signal-item' }, [
              h('span', { class: 'dotm', style: { background: 'var(--text-3)' } }),
              h('span', { class: 'txt', text: r }),
            ]));
          });
          box.appendChild(fold('其余 ' + (reasons.length - MAX_REASONS) + ' 条理由（点开）', rest));
        }
      }

      /* 提示：warning 每条一个 chip warn（title 放全文） */
      const warns = arr(c.warnings).map((w) => String(w));
      const riskFlags = arr(c.risk && c.risk.flags);
      if (warns.length) {
        const line = h('div', { class: 'legend-inline', style: { marginTop: '8px', alignItems: 'center' } });
        line.appendChild(h('span', { class: 'dim3', text: '提示：' }));
        warns.forEach((w) => line.appendChild(chipEl('提示 ' + clip(w, 26), 'warn', w)));
        box.appendChild(line);
      }
      if (riskFlags.length) {
        const line = h('div', { class: 'legend-inline', style: { marginTop: '6px', alignItems: 'center' } });
        line.appendChild(h('span', { class: 'dim3', text: '风险项：' }));
        riskFlags.forEach((f) => {
          if (!f || typeof f !== 'object') return;
          line.appendChild(chipEl(String(f.label || f.key || '风险项'), 'warn', text(f.label, '')));
        });
        box.appendChild(line);
      }

      /* 买卖点位：内联展开（不跳页） */
      const levelBox = h('div', { dataset: { host: 'levels', code: code } });
      const levelBtn = h('button', {
        class: 'btn sm', text: '查看买卖点位', dataset: { act: 'levels', code: code },
        title: 'GET /api/levels?market=&code= —— 支撑阻力 / 分批建仓 / 止损 / 目标 / 盈亏比 / 退出规则',
        on: { click: () => toggleLevels(code, levelBtn, levelBox) },
      });
      box.appendChild(h('div', { style: { marginTop: '8px', display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' } }, [
        levelBtn,
        h('span', { class: 'hint dim3', text: '点位为价位参照，不是价格预测；接口失败会在原地如实报错。' }),
      ]));
      box.appendChild(levelBox);
      return box;
    }

    function renderCandidates() {
      clear(candHost);
      if (st.running) {
        candHost.appendChild(ui.loading('扫描中… 已用时 ' +
          Math.max(0, Math.round((Date.now() - st.startedAt) / 1000)) + ' 秒'));
        return;
      }
      if (st.resultErr) {
        candHost.appendChild(ui.empty('扫描失败，没有候选可展示：' + st.resultErr));
        return;
      }
      if (!st.result) {
        candHost.appendChild(ui.empty('尚未扫描（没跑过）：点上方「开始扫描」后，候选会按分数降序显示在这里。'));
        return;
      }
      const cands = arr(st.result.candidates);
      if (!cands.length) {
        const trunc = st.result.truncatedByBarsLimit;
        const t = trunc && isNum(trunc.count) && trunc.count > 0
          ? '注意：有 ' + trunc.count + ' 只通过了硬闸门但没有取 K 线评分（受 barsLimit 限制），把「K线取样上限」调大后重跑才可能看到它们。'
          : '本轮扫描确实一只都没有通过（过了硬闸门但风险分超阈值 / 评分门槛的标的会出现在下方剔除统计里）。';
        candHost.appendChild(ui.empty('扫描已执行，但没有候选：' + t));
        return;
      }
      candHost.appendChild(h('div', { class: 'hint dim3', style: { marginBottom: '8px' },
        text: '按服务端 score 降序（同分按成交额降序）；共 ' + cands.length + ' 张卡片。' }));
      cands.forEach((c) => candHost.appendChild(renderCard(c || {})));
    }

    /* -------------------------------------------------------- 剔除统计 */

    function renderRejected() {
      clear(rejectedHost);
      if (!st.result) {
        if (st.resultErr) {
          rejectedHost.appendChild(ui.empty('扫描失败，无剔除统计：' + st.resultErr));
        }
        return;
      }
      const stats = st.result.stats || {};
      const byReason = stats.byRejectReason;
      const sample = arr(st.result.hardRejectedSample).length
        ? arr(st.result.hardRejectedSample) : arr(st.result.rejected);

      const body = h('div', {});
      if (byReason && typeof byReason === 'object' && Object.keys(byReason).length) {
        const line = h('div', { class: 'legend-inline', style: { alignItems: 'center', marginBottom: '8px' } });
        line.appendChild(h('span', { class: 'dim3', text: '剔除原因分布（stats.byRejectReason）：' }));
        Object.keys(byReason).forEach((k) => line.appendChild(chipEl(k + ' ' + byReason[k], 'warn')));
        body.appendChild(line);
      } else {
        body.appendChild(h('div', { class: 'hint dim3', text: '服务端未返回 stats.byRejectReason。' }));
      }
      if (sample.length) {
        body.appendChild(ui.tbl({
          compact: true,
          maxHeight: '300px',
          sortKey: null,
          cols: [
            { key: 'code', label: '代码', noSort: true },
            { key: 'name', label: '名称', noSort: true },
            {
              key: 'reasons', label: '剔除原因', noSort: true,
              render: (r) => h('span', {
                title: arr(r.reasons).join('；'),
                text: clip(arr(r.reasons).join('；') || '（服务端未给出原因）', 90),
              }),
            },
          ],
          rows: sample.slice(0, REJ_SAMPLE_MAX),
          emptyText: '没有被剔除的标的',
        }));
      }
      rejectedHost.appendChild(fold('剔除明细（最多 ' + REJ_SAMPLE_MAX + ' 条，点开）', body));
    }

    /* ------------------------------------------------------ 买卖点位面板 */

    function levelRow(label, value, title, cls) {
      return h('div', { class: 'signal-item' }, [
        h('span', { class: 'txt', text: label }),
        h('span', { class: 'w num ' + (cls || ''), text: value, title: title || '' }),
      ]);
    }

    function sideList(items, side) {
      const list = h('div', { class: 'signal-list' });
      const rows = arr(items);
      if (!rows.length) {
        list.appendChild(h('div', { class: 'hint dim3', text: '服务端未返回' + (side === 'support' ? '支撑' : '阻力') + '位' }));
        return list;
      }
      rows.forEach((it) => {
        if (!it || typeof it !== 'object') return;
        const stg = text(it.strength, '');
        list.appendChild(h('div', { class: 'signal-item', title: text(it.note, '') }, [
          h('span', { class: 'dotm', style: { background: it.strength === '强' ? 'var(--up)' : (it.strength === '中' ? 'var(--accent)' : 'var(--text-3)') } }),
          h('span', { class: 'txt num', text: F.price(it.price, st.market) }),
          stg ? chipEl('强度 ' + stg, STRENGTH_CLS[stg] || '', text(it.note, '')) : null,
          it.kind ? h('span', { class: 'mono-sm', text: String(it.kind) }) : null,
        ]));
      });
      return list;
    }

    function renderLevels(host, code) {
      clear(host);
      const cur = st.levels[code];
      if (!cur || !cur.open) return;
      if (cur.loading) { host.appendChild(ui.loading('正在拉取买卖点位…')); return; }
      if (cur.err) {
        host.appendChild(ui.empty('买卖点位不可用：' + cur.err +
          '（GET /api/levels 失败或返回 ok:false；本页不会用其它字段拼凑点位）'));
        return;
      }
      const d = cur.data;
      if (!d || typeof d !== 'object') { host.appendChild(ui.empty('买卖点位接口未返回数据')); return; }
      if (d.ok === false) {
        host.appendChild(ui.empty('买卖点位接口返回 ok:false：' +
          text(d.error || d.message || d.reason, '服务端未给出原因')));
        return;
      }

      const wrap = h('div', {
        style: {
          marginTop: '8px', padding: '10px 12px', border: '1px dashed var(--line-2)',
          borderRadius: 'var(--r)', background: 'var(--surface-2)',
        },
      });
      wrap.appendChild(h('div', { class: 'legend-inline', style: { marginBottom: '8px', alignItems: 'center' } }, [
        h('span', { class: 'dim3', text: '标的：' + text(d.name, code) + '　' + code }),
        h('span', { class: 'num', text: '现价 ' + F.price(d.price, d.market || st.market) }),
        h('span', { class: 'dim3', text: '点位对应K线：' + timeText(d.asOf) }),
        chipEl('最小单位 ' + text(d.rules && d.rules.lot, '—'), '',
          '来自服务端 rules.lot；A股通常 100 股/手'),
      ]));

      /* 支撑 / 阻力 */
      wrap.appendChild(h('div', { class: 'grid g-2', dataset: { level: 'support' } }, [
        h('div', {}, [
          h('div', { class: 'chip accent', text: '支撑位' }),
          sideList(d.support, 'support'),
        ]),
        h('div', {}, [
          h('div', { class: 'chip accent', text: '阻力位' }),
          sideList(d.resistance, 'resistance'),
        ]),
      ]));

      /* 分批建仓 / 目标位 */
      const entryList = h('div', { class: 'signal-list', dataset: { level: 'entries' } });
      if (arr(d.entries).length) {
        arr(d.entries).forEach((e) => {
          entryList.appendChild(levelRow(
            text(e.label, '建仓档') + '　' + F.price(e.price, st.market),
            '权重 ' + ratioText(e.weight, 0), text(e.note, '')));
        });
      } else {
        entryList.appendChild(h('div', { class: 'hint dim3', text: '服务端未返回分批建仓档位' }));
      }
      const targetList = h('div', { class: 'signal-list', dataset: { level: 'targets' } });
      if (arr(d.targets).length) {
        arr(d.targets).forEach((t) => {
          targetList.appendChild(levelRow(
            text(t.label, '目标') + '　' + F.price(t.price, st.market),
            '权重 ' + ratioText(t.weight, 0), text(t.note, ''), 'down'));
        });
      } else {
        targetList.appendChild(h('div', { class: 'hint dim3', text: '服务端未返回目标位' }));
      }
      wrap.appendChild(h('div', { class: 'grid g-2', style: { marginTop: '8px' } }, [
        h('div', {}, [h('div', { class: 'chip', text: '分批建仓' }), entryList]),
        h('div', {}, [h('div', { class: 'chip', text: '目标位（分批止盈）' }), targetList]),
      ]));

      /* 止损三段 / 盈亏比 / 建议股数 */
      const stop = d.stop || {};
      const stopList = h('div', { class: 'signal-list', dataset: { level: 'stop' } });
      stopList.appendChild(levelRow('初始止损', F.price(stop.initial, st.market), text(stop.basis, ''), 'down'));
      stopList.appendChild(levelRow('移动止损启动价', F.price(stop.trailStart, st.market),
        '涨破该价后才开始移动止损（trailing only offset is reached）'));
      stopList.appendChild(levelRow('移动后锁定价', F.price(stop.trailLockIn, st.market),
        '移动止损抬到成本上方后的锁定价（trailing stop positive）'));
      if (isNum(stop.chandelier)) {
        stopList.appendChild(levelRow('Chandelier 参照', F.price(stop.chandelier, st.market),
          '结构参照价，不一定参与初始止损'));
      }

      const rr = d.riskReward || {};
      const rrList = h('div', { class: 'signal-list', dataset: { level: 'rr' } });
      rrList.appendChild(levelRow('盈亏比（至 T1）', isNum(rr.ratio1) ? F.num(rr.ratio1, 2) + ':1' : '—'));
      rrList.appendChild(levelRow('盈亏比（至 T2）', isNum(rr.ratio2) ? F.num(rr.ratio2, 2) + ':1' : '—'));
      if (rr.verdict) {
        rrList.appendChild(h('div', { class: 'hint dim3', text: String(rr.verdict) }));
      }

      const risk = d.risk || {};
      const riskList = h('div', { class: 'signal-list', dataset: { level: 'risk' } });
      riskList.appendChild(levelRow('建议股数', isNum(risk.shares) ? risk.shares + ' 股' : '—',
        text(risk.basis, '按 1% 风险预算与最小交易单位反算')));
      if (isNum(risk.shares) && risk.shares <= 0) {
        /* 服务端在 shares<=0 时会给出「本金过小 / 止损过宽」的 warning，这里同步标一个 chip，
           避免用户只看到「0 股」却不知道原因 */
        riskList.appendChild(chipEl('按当前本金买不满一手（不要靠加大仓位凑）', 'warn',
          text(risk.basis, '')));
      }
      riskList.appendChild(levelRow('建议金额', money(risk.amount, d.market || st.market),
        '建议股数 × 现价'));
      riskList.appendChild(levelRow('占总资金', ratioText(risk.weight, 1), ''));
      riskList.appendChild(levelRow('单笔风险预算', ratioText(risk.perTradePct, 2), ''));

      wrap.appendChild(h('div', { class: 'grid g-2', style: { marginTop: '8px' } }, [
        h('div', {}, [
          h('div', {}, [h('span', { class: 'chip', text: '三段式止损' })]),
          stopList,
        ]),
        h('div', {}, [
          h('div', {}, [h('span', { class: 'chip', text: '盈亏比' }), h('span', { class: 'chip accent', text: '风险预算' })]),
          rrList, riskList,
        ]),
      ]));

      /* 七条优先级退出（折叠） */
      const exits = arr(d.exits);
      const exitBody = h('div', { dataset: { level: 'exits' } });
      if (!exits.length) {
        exitBody.appendChild(h('div', { class: 'hint dim3', text: '服务端未返回退出规则（K线不足时 exits 可能为空数组）' }));
      } else {
        exits.forEach((e) => {
          exitBody.appendChild(h('div', { class: 'signal-item', style: { alignItems: 'flex-start' }, title: text(e.detail, '') }, [
            h('span', { class: 'w mono-sm', text: 'P' + text(e.priority) }),
            h('span', { class: 'txt' }, [
              h('strong', { text: text(e.rule, '未命名规则') }),
              h('span', { class: 'dim3', text: '　' + text(e.condition, '') }),
              h('span', { class: 'dim', text: '　→ ' + text(e.action, '') }),
            ]),
            e.triggered === true ? chipEl('已触发', 'warn') : chipEl('未触发', ''),
          ]));
        });
        exitBody.appendChild(h('div', { class: 'hint dim3', style: { marginTop: '4px' },
          text: '同时触发时按 priority 最小的一条执行（1 = 动量衰竭，7 = ATR 止损）。' }));
      }
      wrap.appendChild(fold('优先级退出规则（' + (exits.length ? exits.length : 7) + ' 条，点开）', exitBody));

      /* 服务端自己的提示（逐条 chip warn）+ 口径说明 */
      const warns = arr(d.warnings).map((w) => String(w));
      if (warns.length) {
        const line = h('div', { class: 'legend-inline', style: { marginTop: '8px', alignItems: 'center' }, dataset: { level: 'warn' } });
        line.appendChild(h('span', { class: 'dim3', text: '风险提示：' }));
        warns.forEach((w) => line.appendChild(chipEl('提示 ' + clip(w, 22), 'warn', w)));
        wrap.appendChild(line);
      }
      if (d.note) {
        wrap.appendChild(h('div', { class: 'hint dim3', style: { marginTop: '6px' }, text: d.note }));
      }
      host.appendChild(wrap);
    }

    async function toggleLevels(code, btn, host) {
      if (st.destroyed) return;
      const cur = st.levels[code];
      if (cur && cur.open) {                        /* 已展开 → 收起（不发请求） */
        cur.open = false;
        btn.textContent = '查看买卖点位';
        renderLevels(host, code);
        return;
      }
      st.levels[code] = {
        open: true, loading: true,
        data: (cur && cur.data) || null, err: '',
      };
      btn.textContent = '收起买卖点位';
      renderLevels(host, code);
      const seq = ++levelSeq;
      try {
        const res = await apiGet('levels', { market: st.market, code: code });
        if (st.destroyed || seq !== levelSeq) return;
        st.levels[code] = { open: true, loading: false, data: res || null, err: '' };
      } catch (e) {
        if (st.destroyed || seq !== levelSeq) return;
        st.levels[code] = {
          open: true, loading: false, data: null,
          err: (e && e.message ? e.message : String(e)) || '未知错误',
        };
        toast('买卖点位获取失败：' + st.levels[code].err, 'err');
      }
      if (!st.destroyed) renderLevels(host, code);
    }

    /* -------------------------------------------------------- 交易规则 */

    function renderRules() {
      clear(rulesHost);
      if (st.rulesErr) {
        rulesHost.appendChild(ui.empty('交易规则获取失败：' + st.rulesErr +
          '（GET /api/rules 无有效返回；本页不会用记忆中的规则表顶替）'));
        return;
      }
      const d = st.rules;
      if (!d) {
        rulesHost.appendChild(ui.empty('交易规则尚未加载：GET /api/rules?market=' + st.market));
        return;
      }
      const unverified = arr(d.unverified).map((u) => String(u));
      const head = h('div', { class: 'legend-inline', style: { marginBottom: '8px', alignItems: 'center' } });
      head.appendChild(chipEl('规则版本 ' + text(d.version), 'accent', text(d.version)));
      head.appendChild(chipEl('市场 ' + (MARKET_LABEL[d.market] || text(d.market))));
      head.appendChild(chipEl('数据截至 ' + timeText(d.updated)));
      if (unverified.length) head.appendChild(chipEl('未证实 ' + unverified.length + ' 项', 'warn'));
      rulesHost.appendChild(head);

      if (unverified.length) {
        const line = h('div', { class: 'legend-inline', style: { marginBottom: '8px', alignItems: 'center' } });
        line.appendChild(h('span', { class: 'dim3', text: '以下项未取得权威来源，仅作可配置默认值：' }));
        unverified.forEach((u) => line.appendChild(chipEl(clip(u, 30), 'warn', u)));
        rulesHost.appendChild(line);
      }

      rulesHost.appendChild(ui.tbl({
        compact: true,
        maxHeight: 'none',
        cols: [
          { key: 'label', label: '板块', noSort: true, render: (r) => h('span', { text: text(r.label, r.board) }) },
          { key: 'limit', label: '涨跌幅', noSort: true, render: (r) => h('span', { class: 'num', text: text(r.limit) }) },
          { key: 'lot', label: '最小单位', noSort: true, render: (r) => h('span', { text: text(r.lot) }) },
          { key: 'note', label: '备注', noSort: true, render: (r) => h('span', { class: 'dim', text: text(r.note, '') }) },
        ],
        rows: arr(d.boards),
        emptyText: '服务端未返回板块表',
      }));

      const extra = arr(d.extra);
      if (extra.length) {
        const line = h('div', { class: 'legend-inline', style: { marginTop: '8px', lineHeight: '1.9' } });
        extra.forEach((e) => {
          if (!e || typeof e !== 'object') return;
          line.appendChild(h('span', {}, [
            h('i', { text: text(e.item) }),
            h('span', { class: 'dim3', text: '：' + text(e.value, '') }),
          ]));
        });
        rulesHost.appendChild(line);
      }

      const sess = d.sessions;
      if (sess && typeof sess === 'object') {
        const line = h('div', { class: 'legend-inline', style: { marginTop: '6px', alignItems: 'center' } });
        line.appendChild(h('span', { class: 'dim3', text: '交易时段（' + text(sess.tz) + '）：' }));
        arr(sess.windows).forEach((w) => {
          if (!w || typeof w !== 'object') return;
          line.appendChild(chipEl(text(w.label, w.key) + ' ' + text(w.from) + '–' + text(w.to)));
        });
        rulesHost.appendChild(line);
        if (sess.note) rulesHost.appendChild(h('div', { class: 'hint dim3', text: String(sess.note) }));
      }

      const sources = arr(d.sources).map((s) => String(s));
      if (sources.length) {
        const body = h('div', { class: 'monospaced', style: { lineHeight: '1.9' } });
        sources.forEach((s) => body.appendChild(h('div', { text: s })));   /* 只显示文本，不做跳转 */
        rulesHost.appendChild(fold('规则来源（' + sources.length + ' 条，点开；纯文本不跳转）', body));
      }
      if (d.note) {
        rulesHost.appendChild(h('div', { class: 'hint dim3', style: { marginTop: '6px' }, text: String(d.note) }));
      }
    }

    /* -------------------------------------------------------- 复盘摘要 */

    function renderReview() {
      clear(reviewHost);
      if (st.reviewErr) {
        reviewHost.appendChild(ui.empty('复盘摘要获取失败：' + st.reviewErr +
          '（GET /api/review/summary 无有效返回；这里不会用别的口径凑数）'));
        return;
      }
      const d = st.review;
      if (!d) {
        reviewHost.appendChild(ui.empty(REVIEW_EMPTY + '?market=' + st.market + '（尚未加载）'));
        return;
      }
      const win = d.window || {};
      const m = d.metrics && typeof d.metrics === 'object' ? d.metrics : {};
      const p = d.period && typeof d.period === 'object' ? d.period : {};
      const dd = d.drawdown && typeof d.drawdown === 'object' ? d.drawdown : {};

      const head = h('div', { class: 'legend-inline', style: { marginBottom: '8px', alignItems: 'center' } });
      head.appendChild(chipEl('窗口 ' + text(win.from) + ' ~ ' + text(win.to)));
      head.appendChild(chipEl('天数 ' + text(win.days)));
      head.appendChild(chipEl('已成交委托 ' + text(d.orders)));
      head.appendChild(chipEl('数据截至 ' + timeText(d.updated)));
      reviewHost.appendChild(head);

      const pick = (a, k, b) => {
        if (a && a[k] !== undefined && a[k] !== null) return a[k];
        if (b && b[k] !== undefined && b[k] !== null) return b[k];
        return null;
      };

      /* 逐笔口径：按已完成交易聚合 */
      const tradeCells = [
        ['交易次数', cntText(m.trades), '', '只统计已配对平仓的已完成交易'],
        ['胜率', ratioText(m.winRate, 1), '', '盈利笔数 / 完成交易笔数（0.15 = 15%）'],
        ['盈亏比', numText(m.payoffRatio, 2), '', '平均盈利 / 平均亏损（绝对值）'],
        ['期望', money(m.expectancy, st.market), F.dir(m.expectancy), '每笔期望盈亏（金额）'],
        ['盈亏因子', numText(m.profitFactor, 2), '', '总盈利 / 总亏损'],
        ['最大连亏', cntText(m.maxConsecutiveLosses), '', '连续亏损笔数最大值'],
      ];
      /* 周期口径：按权益曲线逐 bar；不足时回落 drawdown 段同名指标 */
      const periodCells = [
        ['总收益', ratioText(pick(p, 'totalReturn', null), 2), F.dir(p.totalReturn)],
        ['年化', ratioText(p.annualized, 2), F.dir(p.annualized)],
        ['夏普', numText(p.sharpe, 2), ''],
        ['最大回撤', ratioText(pick(p, 'maxDrawdown', dd), 2), 'down'],
        ['最长水下时间', unitText(pick(p, 'maxDrawdownDuration', dd), 'bar'), ''],
        ['回撤段数', cntText(pick(p, 'drawdownCount', dd)), ''],
        ['水下占比', ratioText(pick(p, 'timeInDrawdownRatio', dd), 1), ''],
      ];

      const col = (title, hint, cells) => {
        const list = h('div', { class: 'metric-list' });
        cells.forEach((c) => list.appendChild(cell(c[0], c[1], c[2], c[3])));
        return h('div', {}, [
          h('div', { class: 'legend-inline', style: { marginBottom: '6px', alignItems: 'center' } }, [
            h('span', { class: 'chip accent', text: title }),
            h('span', { class: 'dim3', text: hint }),
          ]),
          list,
        ]);
      };
      reviewHost.appendChild(h('div', { class: 'grid g-2' }, [
        col('逐笔口径', '按每条已完成交易聚合（含开仓未平的腿不计）', tradeCells),
        col('周期口径', '按权益曲线逐 bar 计算；与逐笔口径本来就可能不一致', periodCells),
      ]));

      if (p.flowNote) {
        reviewHost.appendChild(h('div', { class: 'hint dim3', style: { marginTop: '6px' },
          text: '周期口径出入金处理：' + String(p.flowNote) +
            (p.flowAdjusted === true ? '（已按时间加权剔除，净值与回撤同步改用剔除后口径）' : '') }));
      }

      const bySource = d.bySource;
      if (bySource && typeof bySource === 'object') {
        const keys = Object.keys(bySource);
        if (keys.length) {
          const line = h('div', { class: 'legend-inline', style: { marginTop: '8px', alignItems: 'center' } });
          line.appendChild(h('span', { class: 'dim3', text: '按来源（bySource）：' }));
          keys.forEach((k) => {
            const v = bySource[k];
            const trades = v && typeof v === 'object' ? v.trades : v;
            line.appendChild(chipEl((SOURCE_LABEL[k] || k) + ' ' + cntText(trades) + ' 笔'));
          });
          reviewHost.appendChild(line);
        }
      }

      const warnings = arr(d.warnings).map((w) => String(w));
      if (warnings.length) {
        const line = h('div', { class: 'legend-inline', style: { marginTop: '8px', alignItems: 'center' } });
        warnings.forEach((w) => line.appendChild(chipEl('警告 ' + clip(w, 28), 'warn', w)));
        reviewHost.appendChild(line);
      }
      if (d.note) {
        reviewHost.appendChild(h('div', { class: 'hint dim3', style: { marginTop: '6px' }, text: String(d.note) }));
      }
      if (!m.trades && !p.bars) {
        reviewHost.appendChild(h('div', { class: 'hint dim3', style: { marginTop: '6px' },
          text: '窗口内没有已完成交易 / 权益点：相关字段为 null，因此上面显示「—」而不是 0。' }));
      }
    }

    /* ---------------------------------------------------------- 数据加载 */

    async function loadConfig(silent) {
      try {
        const res = await apiGet('scan/config', null);
        if (st.destroyed) return;
        st.params = (res && res.params) || null;
        st.defaults = (res && res.defaults) || null;
        st.paramsErr = '';
        renderConfig();
      } catch (e) {
        if (st.destroyed) return;
        st.paramsErr = (e && e.message ? e.message : String(e)) || '未知错误';
        renderConfig();
        if (!silent) toast('扫描参数获取失败：' + st.paramsErr, 'err');
      }
    }

    async function loadRules(silent) {
      try {
        const res = await apiGet('rules', { market: st.market });
        if (st.destroyed) return;
        st.rules = res || null;
        st.rulesErr = '';
        st.rulesAt = Date.now();
        renderRules();
      } catch (e) {
        if (st.destroyed) return;
        st.rules = null;
        st.rulesErr = (e && e.message ? e.message : String(e)) || '未知错误';
        renderRules();
        if (!silent) toast('交易规则获取失败：' + st.rulesErr, 'err');
      }
    }

    async function loadReview(silent) {
      try {
        const res = await apiGet('review/summary', { market: st.market });
        if (st.destroyed) return;
        st.review = res || null;
        st.reviewErr = '';
        st.reviewAt = Date.now();
        renderReview();
      } catch (e) {
        if (st.destroyed) return;
        st.review = null;
        st.reviewErr = (e && e.message ? e.message : String(e)) || '未知错误';
        renderReview();
        if (!silent) toast('复盘摘要获取失败：' + st.reviewErr, 'err');
      }
    }

    /* refresh()：只刷新轻量区块（参数 / 规则 / 复盘），**不重跑重扫描** */
    function refreshAll(silent) {
      if (st.destroyed) return;
      loadConfig(silent);
      loadRules(silent);
      loadReview(silent);
    }

    /* ------------------------------------------------------------ 骨架 */

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('买入扫描',
        '技术面硬闸门 + 复合评分 + 买卖点位参考；统计口径与失败原因全部如实展示，' +
        '<b>不构成投资建议、不承诺收益</b>',
        [marketChip, refreshBtn]),
      ui.section('买入扫描',
        'POST /api/scan/run：过闸门 → 按成交额取前 N 只K线 → 评分排序；重操作，手动触发',
        [], h('div', {}, [
          h('div', { class: 'legend-inline', style: { gap: '12px', alignItems: 'center', flexWrap: 'wrap' } }, [
            field('扫描数量', limitInp),
            field('K线取样上限', barsInp, '只对成交额最大的 N 只取K线评分'),
            runBtn,
          ]),
          statusHost,
          cfgHost,
          summaryHost,
          candHost,
          rejectedHost,
          disclaimerHost,
        ])),
      ui.section('交易规则',
        'GET /api/rules：涨跌幅 / 最小单位 / 费用与时段；版本与「未证实项」一并展示',
        [], rulesHost),
      ui.section('复盘摘要',
        'GET /api/review/summary：逐笔口径与周期口径分栏（两者本来就可能不一致）',
        [], reviewHost),
    ]));

    paintMarket();
    paintStatus();
    renderConfig();
    renderSummary();
    renderCandidates();
    renderRejected();
    renderRules();
    renderReview();

    loadConfig(true);
    loadRules(true);
    loadReview(true);

    return {
      refresh: () => refreshAll(false),
      destroy() {
        st.destroyed = true;         /* 先置位：之后所有回调一律直接返回 */
        stopElapsed();
        reqSeq += 1;                 /* 让在途扫描的返回失效（不会覆盖已卸载的 DOM） */
        levelSeq += 1;
        st.levels = {};
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.scan = { mount };
})();
