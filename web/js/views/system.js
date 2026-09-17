/* ==========================================================================
   视图 · 运行状态（可观测性 + 通知渠道）
   ① 引擎 / 存储 / 缓存 / 数据源摘要     GET  /api/sysinfo
   ② 结构化日志流（级别筛选 + 10 秒自动刷新） GET  /api/logs
   ③ 通知渠道配置（Webhook + 事件订阅 + 测试投递） GET/POST /api/notify、POST /api/notify/test
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, paint, reconcile } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  /* 日志流自动刷新间隔（毫秒）：与运行摘要共用同一次 tick */
  const LOG_MS = 10000;

  /* 日志条数可选值 */
  const LIMITS = [50, 100, 200, 500];

  /* 级别筛选（level 为空表示不过滤） */
  const LEVELS = [
    { value: '', label: '全部' },
    { value: 'debug', label: '调试' },
    { value: 'info', label: '信息' },
    { value: 'warn', label: '警告' },
    { value: 'error', label: '错误' },
  ];
  const LEVEL_TEXT = { debug: '调试', info: '信息', warn: '警告', warning: '警告', error: '错误', critical: '错误' };
  const LEVEL_CLS = {
    debug: 'chip dim', info: 'chip accent', warn: 'chip warn', warning: 'chip warn',
    error: 'chip up', critical: 'chip up',
  };

  /* 与 core/notify.py 的 EVENT_TEXT 保持一致 */
  const EVENTS = [
    { key: 'on_run_start', label: '任务启动' },
    { key: 'on_bar', label: 'K线推进' },
    { key: 'on_signal', label: '产生信号' },
    { key: 'on_fill', label: '成交建仓' },
    { key: 'on_exit', label: '平仓' },
    { key: 'on_skip', label: '信号被跳过' },
    { key: 'on_error', label: '引擎异常' },
    { key: 'on_run_revised', label: '配置调整' },
    { key: 'on_run_paused', label: '任务暂停' },
    { key: 'on_resume', label: '任务恢复' },
  ];

  const PROVIDER_TEXT = {
    quote: '实时行情 / 盘口',
    kline: 'K线 / 分时',
    market: '全市场快照 / 板块 / 资讯 / 搜索',
    features: '盘口事件（竞价 / 分笔 / 龙虎榜 / 涨停）',
  };

  const TABLE_TEXT = {
    runs: '策略任务 runs',
    trades: '成交明细 trades',
    equity: '权益曲线 equity',
    signals: '信号记录 signals',
    logs: '日志 logs',
    meta: '元数据 meta',
  };

  /* 日志记录的框架字段，不作为业务字段展示 */
  const RESERVED = { ts: 1, time: 1, level: 1, event: 1 };

  /* ------------------------------------------------------------ 格式化 */

  function fmtInt(v) {
    const n = Number(v);
    return isFinite(n) ? n.toLocaleString('en-US') : '—';
  }

  function fmtBytes(n) {
    const v = Number(n);
    if (!isFinite(v) || v < 0) return '—';
    if (v >= 1048576) return F.num(v / 1048576, 2) + ' MB';
    if (v >= 1024) return F.num(v / 1024, 1) + ' KB';
    return Math.round(v) + ' B';
  }

  function fmtUptime(sec) {
    const s = Number(sec);
    if (!isFinite(s)) return '—';
    const t = Math.max(0, Math.floor(s));
    const d = Math.floor(t / 86400);
    const hh = Math.floor((t % 86400) / 3600);
    const mm = Math.floor((t % 3600) / 60);
    if (d) return d + ' 天 ' + hh + ' 小时';
    if (hh) return hh + ' 小时 ' + mm + ' 分';
    if (mm) return mm + ' 分 ' + (t % 60) + ' 秒';
    return t + ' 秒';
  }

  function clip(text, n) {
    const s = String(text === null || text === undefined ? '' : text);
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  function shortVal(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'object') {
      try { return JSON.stringify(v); } catch (e) { return String(v); }
    }
    return String(v);
  }

  /* 把一条日志的业务字段拼成 "k=v" 串（最多 6 个，避免刷屏） */
  function fieldsText(rec) {
    const parts = [];
    const keys = Object.keys(rec);
    for (let i = 0; i < keys.length && parts.length < 6; i++) {
      const k = keys[i];
      if (RESERVED[k]) continue;
      const v = shortVal(rec[k]);
      if (!v) continue;
      parts.push(k + '=' + clip(v, 60));
    }
    return parts.join('　');
  }

  /* 指标卡：node 优先于文本 */
  function metricCell(label, value, node) {
    return h('div', { class: 'metric' }, [
      h('div', { class: 'k', text: label }),
      node ? h('div', { class: 'v' }, [node]) : h('div', { class: 'v', text: value }),
    ]);
  }

  /* ------------------------------------------------------------- 视图 */

  function mount(root, ctx) {
    const st = {
      info: null,        // api.sysinfo() 结果
      logs: [],          // api.logs() 行
      notify: null,      // api.notifyGet() 结果
      testLast: null,    // 最近一次「发送测试」的投递结果
      level: '',         // 日志级别筛选（'' = 全部）
      limit: 200,        // 日志条数
      auto: true,        // 10 秒自动刷新
      notifyEdited: false,
    };
    let timer = null;
    let ticking = false;

    /* 常驻实例（放在 mount 内，避免跨页面串数据）：
       表格首次 appendChild，之后只 update(rows)；列表按 key 原位复用。
       10 秒自动刷新因此不再重建表体 / 日志行，滚动位置与 hover 都不会丢 */
    let countsTbl = null;          // 存储各表行数
    let countsColsSig = '';        // 行数列标签带合计，变了才 setCols
    let providerTbl = null;        // 数据源链路
    const logList = h('div', { class: 'news-list' });   // 日志列表常驻容器

    /* 持久节点：自动刷新只重绘内容，不重建表单，避免打断输入 */
    const engineChip = h('span', { class: 'engine-chip off' });
    const engineHost = h('div', { class: 'metric-list' });
    const storageHost = h('div', { class: 'metric-list' });
    const countsHost = h('div');
    const cacheHost = h('div', { class: 'metric-list' });
    const providerHost = h('div');
    const levelHost = h('div');
    const logStat = h('span', { class: 'hint', text: '加载中…' });
    const logHost = h('div', {}, [ui.loading('日志加载中…')]);
    const webhookInput = h('input', {
      class: 'inp',
      placeholder: 'https://…（企业微信 / 钉钉 / Slack / 自建服务）',
      style: { width: '460px', maxWidth: '100%' },
      on: {
        input: () => { st.notifyEdited = true; },
        keydown: (e) => { if (e.key === 'Enter') saveNotify(); },
      },
    });
    const eventHost = h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px 18px' } });
    const eventBoxes = {};
    const notifyStat = h('span', { class: 'hint', text: '加载中…' });
    const opStat = h('div', { class: 'set-note' });          /* 保存 / 测试的操作反馈 */
    const lastHost = h('div', { class: 'metric-list' });

    EVENTS.forEach((ev) => {
      const box = h('input', { type: 'checkbox', title: ev.key });
      eventBoxes[ev.key] = box;
      eventHost.appendChild(h('label', { class: 'field', style: { cursor: 'pointer' }, title: ev.key }, [
        box, h('span', { class: 'dim', text: ev.label }),
      ]));
    });

    /* ------------------------------------------------- ① 引擎与存储 */

    function renderEngine() {
      const info = st.info;
      const e = (info && info.engine) || {};
      engineChip.className = 'engine-chip' + (e.running ? '' : ' off');
      /* 页头 chip 与指标卡都只原位改写：结构不变时不换节点 */
      paint(engineChip, [
        h('i', { class: 'dot' }),
        h('span', { text: e.running ? '引擎运行中' : '引擎未运行' }),
        h('span', {
          class: 'meta',
          text: '间隔 ' + (e.interval || '—') + 's · 轮次 ' + (e.ticks || 0),
        }),
      ]);

      if (!info) {
        paint(engineHost, [ui.empty('运行摘要获取失败，点右上「刷新」重试')]);
        return;
      }
      paint(engineHost, [
        metricCell('引擎状态', e.running ? '运行中' : '未运行（启动 server.py 即自动开启）'),
        metricCell('推进间隔', e.interval ? e.interval + ' 秒' : '—'),
        metricCell('最近 tick', e.lastTick ? F.clock(e.lastTick) : '等待首次推进'),
        metricCell('推进轮次', (e.ticks || 0) + ' 次'),
        metricCell('任务数', (e.runs || 0) + ' 个（运行中 ' + (e.active || 0) + '）'),
        metricCell('引擎存储', e.storage || '—'),
        metricCell('最近错误', '', e.lastError
          ? h('span', { class: 'chip up', text: clip(e.lastError, 40), title: String(e.lastError) })
          : h('span', { class: 'dim3', text: '无' })),
      ]);
    }

    function renderStorage() {
      const info = st.info;
      if (!info) {
        paint(storageHost, []);
        paint(countsHost, []);
        countsTbl = null;                      /* 空态替换掉了表体，实例随之失效 */
        return;
      }
      const s = info.storage || {};
      const journal = String(s.journal || '').toUpperCase();
      paint(storageHost, [
        metricCell('存储引擎', s.engine || 'sqlite'),
        metricCell('journal 模式', '', h('span', {
          class: 'chip ' + (journal === 'WAL' ? 'accent' : 'warn'),
          text: journal || '未知',
        })),
        metricCell('schema 版本', 'v' + (s.schemaVersion === undefined || s.schemaVersion === null ? '—' : s.schemaVersion)),
        metricCell('库文件路径', '', h('span', {
          class: 'monospaced', text: clip(s.path || '—', 46), title: s.path || '',
        })),
      ]);

      const counts = s.counts || {};
      const rows = Object.keys(counts).map((k) => ({ table: k, rows: counts[k] }));
      if (!rows.length) {
        countsTbl = null;
        paint(countsHost, [ui.empty('暂无数据表统计')]);
        return;
      }
      let total = 0;
      rows.forEach((r) => { total += Number(r.rows) || 0; });
      const cols = [
        {
          key: 'table', label: '数据表', noSort: true,
          render: (r) => h('span', { class: 'monospaced', text: TABLE_TEXT[r.table] || r.table }),
        },
        {
          key: 'rows', label: '行数（合计 ' + fmtInt(total) + '）', cls: 'n', noSort: true,
          render: (r) => h('span', { class: 'num', text: fmtInt(r.rows) }),
        },
      ];
      /* 行数列标签里带合计，只有它变化时才换列定义；其余情况只 update 行 */
      const sig = cols.map((c) => c.key + '\u0001' + c.label).join('\u0002');
      if (!countsTbl) {
        countsTbl = ui.tbl({ cols, rows, compact: true, emptyText: '暂无数据表统计' });
        clear(countsHost);
        countsHost.appendChild(countsTbl);
      } else {
        if (countsTbl.parentNode !== countsHost) {   /* 曾被空态替换过：重新挂载 */
          clear(countsHost);
          countsHost.appendChild(countsTbl);
        }
        if (sig !== countsColsSig) countsTbl.setCols(cols);
        countsTbl.update(rows);
      }
      countsColsSig = sig;
    }

    /* --------------------------------------------- 缓存与数据源清单 */

    function renderCache() {
      const info = st.info;
      if (!info) { paint(cacheHost, []); return; }
      const c = info.cache || {};
      paint(cacheHost, [
        metricCell('缓存条目', (c.keys || 0) + ' 个'),
        metricCell('缓存体积', fmtBytes(c.bytes)),
        metricCell('服务运行', fmtUptime(info.uptimeSec)),
        metricCell('服务端时间', info.serverTime ? F.clock(info.serverTime) : '—'),
        metricCell('接口版本', info.version || '—'),
        metricCell('时区', info.tz || '—'),
      ]);
    }

    function renderProviders() {
      const info = st.info;
      if (!info) {
        providerTbl = null;
        paint(providerHost, []);
        return;
      }
      const p = info.providers || {};
      const rows = Object.keys(p).map((k) => ({ key: k, chain: p[k] }));
      if (!rows.length) {
        providerTbl = null;
        paint(providerHost, [ui.empty('暂无数据源清单')]);
        return;
      }
      const cols = [
        {
          key: 'key', label: '数据域', noSort: true, width: '230px',
          render: (r) => h('span', { class: 'dim', text: PROVIDER_TEXT[r.key] || r.key }),
        },
        {
          key: 'chain', label: '数据源链路（左优先，异常自动降级）', noSort: true,
          render: (r) => h('span', { class: 'monospaced', text: r.chain }),
        },
      ];
      if (!providerTbl) {
        providerTbl = ui.tbl({ cols, rows, compact: true, emptyText: '暂无数据源清单' });
        clear(providerHost);
        providerHost.appendChild(providerTbl);
      } else {
        if (providerTbl.parentNode !== providerHost) {
          clear(providerHost);
          providerHost.appendChild(providerTbl);
        }
        providerTbl.update(rows);              /* 列固定：只更新行，不重建表体 */
      }
    }

    /* ------------------------------------------------- ② 日志流 */

    function renderLevelSeg() {
      /* 级别按钮固定 5 个、取值不变：原位改写只会切 active 类，回调里的取值也不会错位 */
      paint(levelHost, [ui.seg(LEVELS, st.level, (v) => {
        st.level = v;
        renderLevelSeg();
        refreshLogs();
      })]);
    }

    /* 日志行没有稳定 id（来自 /api/logs，最新在前）：
       用 ts + 级别 + 事件 + 同键出现序号做 key，轮询时只有新增的行会插入到列表头部 */
    function logKeys(rows) {
      const seen = {};
      return rows.map((r) => {
        const base = String(r.ts) + '|' + String(r.level || '') + '|' + String(r.event || '');
        seen[base] = (seen[base] || 0) + 1;
        return base + '#' + seen[base];
      });
    }

    function logItem(r) {
      const lv = String(r.level || 'info').toLowerCase();
      const body = h('div', { class: 'body' });
      body.appendChild(h('div', { class: 'txt' }, [
        h('span', { class: LEVEL_CLS[lv] || 'chip', text: LEVEL_TEXT[lv] || lv, title: '级别：' + lv }),
        h('span', {
          class: 'monospaced', style: { marginLeft: '8px' },
          text: r.event || '(未命名事件)',
        }),
      ]));
      const fields = fieldsText(r);
      if (fields) {
        body.appendChild(h('div', {
          class: 'monospaced dim',
          style: { marginTop: '3px', whiteSpace: 'pre-wrap', wordBreak: 'break-all' },
          text: fields,
        }));
      }
      return h('div', { class: 'news-item' }, [
        h('div', { class: 'time', text: F.hhmmss(r.time) || F.clock(r.ts) }),
        body,
      ]);
    }

    function renderLogs() {
      if (!st.logs.length) {
        /* 空态与列表是两种结构：先把常驻列表摘下来（节点留在内存里），再原位换成空态 */
        if (logList.parentNode === logHost) logHost.removeChild(logList);
        paint(logHost, [ui.empty(st.level
          ? '当前筛选下没有日志，可切回「全部」级别'
          : '暂无日志记录（引擎未产生事件）')]);
        return;
      }
      if (logList.parentNode !== logHost) {
        clear(logHost);
        logHost.appendChild(logList);
      }
      const keys = logKeys(st.logs);
      /* 按 key 复用行节点：日志每 10 秒刷新一次，只有新行会插到最前面，不再整块重建 */
      reconcile(logList, st.logs, {
        key: (r, i) => keys[i],
        render: (r) => logItem(r),
      });
    }

    /* ------------------------------------------------- ③ 通知渠道 */

    function applyNotify(s) {
      if (!s) return;
      st.notify = s;
      if (!st.notifyEdited) webhookInput.value = s.webhook || '';
      const events = s.events || [];
      EVENTS.forEach((ev) => {
        if (eventBoxes[ev.key]) eventBoxes[ev.key].checked = events.indexOf(ev.key) >= 0;
      });
    }

    function currentEvents() {
      return EVENTS.filter((ev) => eventBoxes[ev.key] && eventBoxes[ev.key].checked).map((ev) => ev.key);
    }

    function renderNotifyState() {
      const infoNotify = st.info && st.info.notify;
      const n = infoNotify || st.notify || {};
      const infoLast = n.last || null;
      let last = infoLast;
      if (st.testLast && (!infoLast || (st.testLast.ts || 0) >= (infoLast.ts || 0))) last = st.testLast;

      paint(lastHost, [
        metricCell('通道状态', '', h('span', {
          class: 'chip ' + (n.enabled ? 'accent' : ''),
          text: n.enabled ? '已启用' : '未启用（未配置 Webhook）',
        })),
        metricCell('订阅事件', ((n.events || []).length) + ' / ' + EVENTS.length + ' 项'),
        metricCell('上次投递', last && last.ts ? F.clock(last.ts) : '暂无投递记录'),
        metricCell('投递结果', '', last && last.ts
          ? (last.ok
            ? h('span', { class: 'chip down', text: '成功' })
            : h('span', { class: 'chip up', text: '失败' }))
          : h('span', { class: 'dim3', text: '—' })),
        metricCell('HTTP 状态', last && last.status ? String(last.status) : '—'),
        metricCell('目标地址', '', h('span', {
          class: 'monospaced', text: clip((last && last.url) || n.webhook || '—', 42),
          title: (last && last.url) || n.webhook || '',
        })),
        metricCell('失败原因', '', last && last.error
          ? h('span', { class: 'chip warn', text: clip(last.error, 40), title: String(last.error) })
          : h('span', { class: 'dim3', text: '无' })),
      ]);

      if (!st.notifyEdited) {
        notifyStat.textContent = n.enabled
          ? '已启用 · 订阅 ' + ((n.events || []).length) + ' 类事件'
          : '未启用 · 填写地址并保存后由服务端投递';
      }
    }

    async function loadNotify() {
      try {
        const res = await api.notifyGet();
        applyNotify(res);
        renderNotifyState();
      } catch (e) {
        notifyStat.textContent = '通知配置读取失败';
        opStat.textContent = '读取失败：' + e.message;
      }
    }

    async function saveNotify() {
      const url = webhookInput.value.trim();
      const events = currentEvents();
      saveBtn.disabled = true;
      try {
        const res = await api.notifySave({ webhook: url, events });
        st.notifyEdited = false;
        st.testLast = null;
        applyNotify(res);
        opStat.textContent = '已保存 · ' + F.clock(Date.now()) + ' · ' +
          (res.enabled ? 'Webhook 已启用，订阅 ' + events.length + ' 类事件' : '未填写地址，通知不会外发');
        ctx.toast(res.enabled
          ? '通知配置已保存，订阅 ' + events.length + ' 类事件'
          : '通知配置已保存（未填写 Webhook，暂不外发）', 'ok');
        refreshInfo();
      } catch (e) {
        opStat.textContent = '保存失败：' + e.message;
        ctx.toast('通知配置保存失败：' + e.message, 'err');
      } finally {
        saveBtn.disabled = false;
      }
    }

    async function testNotify() {
      const url = webhookInput.value.trim();
      if (!url) {
        ctx.toast('请先填写 Webhook 地址再发送测试', 'warn');
        webhookInput.focus();
        return;
      }
      testBtn.disabled = true;
      opStat.textContent = '测试投递中…';
      try {
        const res = await api.notifyTest({ webhook: url });
        const last = res.last || { ts: Date.now(), ok: !!res.ok, status: null, error: null, url };
        st.testLast = last;
        if (res.ok) {
          opStat.textContent = '测试成功 · ' + F.clock(last.ts || Date.now()) +
            ' · HTTP ' + (last.status || 200);
          ctx.toast('测试消息已投递，请到接收端确认', 'ok');
        } else {
          opStat.textContent = '测试失败 · ' + (last.error || '未知错误');
          ctx.toast('测试投递失败：' + (last.error || '未知错误'), 'err');
        }
        renderNotifyState();
      } catch (e) {
        opStat.textContent = '测试失败：' + e.message;
        ctx.toast('测试投递失败：' + e.message, 'err');
      } finally {
        testBtn.disabled = false;
      }
    }

    /* ----------------------------------------------------- 刷新逻辑 */

    async function refreshLogs() {
      try {
        const res = await api.logs({ limit: st.limit, level: st.level });
        st.logs = res.rows || [];
        logStat.textContent = '返回 ' + st.logs.length + ' 条 · ' +
          (st.level ? '级别 ≥ ' + (LEVEL_TEXT[st.level] || st.level) : '全部级别') +
          ' · ' + F.clock(res.updated || Date.now());
        renderLogs();
      } catch (e) {
        st.logs = [];
        logStat.textContent = '日志获取失败';
        /* 列表与失败空态结构不同：先摘下常驻列表，再原位换成错误提示 */
        if (logList.parentNode === logHost) logHost.removeChild(logList);
        paint(logHost, [ui.empty('日志获取失败：' + e.message)]);
      }
    }

    async function refreshInfo() {
      try {
        const info = await api.sysinfo();
        st.info = info;
        renderEngine();
        renderStorage();
        renderCache();
        renderProviders();
        renderNotifyState();
      } catch (e) {
        st.info = null;
        /* 失败时各区块回到空态（render* 内部走 paint / 空实现，不重建父节点） */
        renderEngine();
        renderStorage();
        renderCache();
        renderProviders();
      }
    }

    async function refresh() {
      await Promise.all([refreshLogs(), refreshInfo()]);
    }

    /* 10 秒自动刷新：日志流 + 运行摘要，互斥避免请求叠加 */
    async function tick() {
      if (!st.auto || ticking || !root.isConnected) return;
      ticking = true;
      try {
        await refresh();
      } finally {
        ticking = false;
      }
    }

    /* --------------------------------------------------- 顶部控件 */

    const autoBtn = h('button', {
      class: 'btn sm active', text: '自动刷新 10s',
      title: '开启后每 10 秒自动拉取日志流与运行摘要',
      on: {
        click: () => {
          st.auto = !st.auto;
          autoBtn.classList.toggle('active', st.auto);
          ctx.toast(st.auto ? '已开启 10 秒自动刷新' : '已暂停自动刷新，可手动点「刷新」', st.auto ? 'ok' : 'info');
        },
      },
    });

    const limitSel = h('select', {
      class: 'inp', style: { width: '96px' },
      on: { change: (e) => { st.limit = Number(e.target.value) || 200; refreshLogs(); } },
    });
    LIMITS.forEach((n) => limitSel.appendChild(h('option', { value: String(n), text: n + ' 条' })));
    limitSel.value = String(st.limit);

    const saveBtn = h('button', { class: 'btn primary sm', text: '保存配置', on: { click: saveNotify } });
    const testBtn = h('button', { class: 'btn sm', text: '发送测试', on: { click: testNotify } });

    /* ---------------------------------------------------- 组装 */

    renderLevelSeg();

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('运行状态', '引擎 / 存储 / 缓存 / 数据源 的可观测性摘要，结构化日志流与通知渠道配置', [
        engineChip, autoBtn,
        h('button', { class: 'btn sm', text: '刷新', on: { click: refresh } }),
      ]),

      h('div', { class: 'grid g-2' }, [
        ui.section('引擎状态', '服务端常驻策略推进线程', [], engineHost),
        ui.section('存储 · SQLite', 'journal 模式、schema 版本与各表行数', [],
          h('div', {}, [storageHost, h('div', { style: { marginTop: '10px' } }, [countsHost])])),
      ]),

      h('div', { class: 'grid g-2' }, [
        ui.section('缓存', '本地服务内存缓存，TTL 过期后回源', [], cacheHost),
        ui.section('数据源清单', '按优先级串行降级', [], providerHost),
      ]),

      ui.section('日志流', 'JSONL 结构化日志（ts / level / event / 业务字段），支持级别筛选与 10 秒自动刷新',
        [levelHost, limitSel, logStat], logHost),

      ui.section('通知渠道', '由服务端引擎按事件触发 webhook 投递，浏览器关闭也会送达',
        [notifyStat, saveBtn, testBtn],
        h('div', {}, [
          h('div', { class: 'field' }, [
            h('label', { text: 'Webhook' }), webhookInput,
          ]),
          h('div', { class: 'set-note', style: { margin: '8px 0 10px' },
            text: '地址需以 http:// 或 https:// 开头；企业微信 / 钉钉机器人可直接填其 Webhook 地址。勾选的事件由后台引擎在对应时点推送。' }),
          eventHost,
          h('div', { style: { marginTop: '10px' } }, [opStat]),
          h('div', { style: { marginTop: '12px' } }, [lastHost]),
        ])),
    ]));

    /* 首次加载 + 定时器（destroy 时统一清理） */
    loadNotify();
    refresh();
    timer = setInterval(tick, LOG_MS);

    return {
      refresh,
      destroy() {
        if (timer) { clearInterval(timer); timer = null; }
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.system = { mount };
})();
