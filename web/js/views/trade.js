/* ==========================================================================
   视图 · 模拟交易（计划盘 / 模拟盘）

   定位：本页是「模拟盘 / 计划盘」，**不接任何真实券商通道**，不会产生真实委托；
        「自动交易」总开关默认关闭，需显式开启。

   后端契约（前端只消费，字段缺失一律降级为「—」，绝不臆造数值）：
     GET  /api/trade/config              → { ok, config:{ enabled, mode, market, capital,
            maxWeight, maxPositions, maxOrdersPerDay, maxOrderAmount, minConfidence,
            allowReduce, universe:[], whitelist:[], interval, webhook, confirmToken,
            updatedAt }, gates:{...}, accountId, note }
     POST /api/trade/config { patch:{} } → { ok, config, note }
     GET  /api/trade/account?market=cn   → { ok, account:{ accountId, market, mode, cash,
            initial, equity, marketValue, pnl, returnPct, realizedPnl, feeTotal,
            positionCount, positions:[{ code, name, market, qty, avgPrice, cost, lastPrice,
            marketValue, pnl, pnlPct, weight, openedAt, priced }], weights },
            gates, counts:{ ordersToday, orders, positions } }
     POST /api/trade/reset  { market }   → { ok, account, note }
     POST /api/trade/plan   { market?, execute?:bool, symbols?:[] }
            → { ok, market, symbols, orders:[委托单], skipped:[{ code, action, reason }],
                gates, adviceSummary:{ analyzed, actions:{ buy, add, ... } }, filled,
                account, note }
     POST /api/trade/execute { ids?:[], confirm:"<口令>" } → { ok, filled, rejected, orders, account }
     POST /api/trade/cancel  { id }      → { ok, order }
     POST /api/trade/close   { market, code, qty? } → { ok, order, account }
     GET  /api/trade/orders?status=&market=&code=&limit=&offset=
            → { ok, rows:[委托单], total, limit, offset }
     GET  /api/trade/export?since=&limit= → { ok, generatedAt, count, orders:[{ id, code, side,
            intent, qty, limitPrice, createdAt, payload }] }
     POST /api/trade/ack    { id, extRef, status? } → { ok, order }

   自动交易接口（给「你自己的券商桥接」消费）：
     GET /api/trade/export 取待执行意图 → 外部系统执行 → POST /api/trade/ack 回执。
     X-Trade-Confirm / confirmToken 只是**防误触口令，不是安全边界**：本服务是本机单
     用户工具，任何能访问该端口的人都能直接调接口，切勿把端口暴露到公网。

   实时推送（web/js/stream.js → /api/stream/trade）：
     order / fill / account / config / note / error 六类事件就地更新；
     连不上或断开时由 stream 层自动降级为 15 秒轮询（fallbackTick 复用本视图 pollTick）。
     独立于推送的 15 秒自动刷新保留为最终兜底（可切换为手动）。

   destroy() 约定：置 st.destroyed，关闭推送句柄并清空所有定时器；
     所有异步回调与推送回调进入时先判 st.destroyed，销毁后绝不再碰 DOM。

   可测试钩子（data-* 定位，便于自动化用例与人工排查）：
     [data-host=…] 渲染容器：config/metrics/gates/positions/orders/skipped/plan/fills/export
     [data-cfg=…]  配置控件：enabled/mode/capital/maxWeight/maxPositions/maxOrdersPerDay/
                   maxOrderAmount/minConfidence/interval/allowReduce/universe/whitelist/webhook
     [data-act=…]  操作控件：plan/execute/token/confirm-token/reset/export/more/cancel/close/ack
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;
  const isNum = window.AD.isNum;
  const MARKET_LABEL = window.AD.MARKET_LABEL || { cn: 'A股', us: '美股' };

  const POLL_MS = 15000;        /* 自动刷新 / 推送降级后的轮询间隔 */
  const ORDERS_LIMIT = 50;      /* 委托单每次拉取条数 */
  const ORDERS_MAX = 200;       /* 本地最多保持的委托单条数（避免无限增长） */
  const FILL_MAX = 40;          /* 成交回报最多保留条数 */
  const REASON_MAX = 42;        /* 「原因」列截断长度（全文挂 title） */
  const SEP = /[\s,，、;；|]+/;   /* 代码分隔：逗号 / 空格 / 换行 / 分号 / 顿号 */

  /* 模式：dryrun 只出计划不成交，paper 按模拟撮合成交 */
  const MODE_LABEL = { dryrun: '只出计划（dryrun，不成交）', paper: '模拟成交（paper）' };
  const MODE_SHORT = { dryrun: 'dryrun 只出计划', paper: 'paper 模拟成交' };

  const STATUS_LABEL = {
    pending: '待执行', submitted: '已外发待回执', acked: '已回执',
    filled: '已成交', rejected: '已拒绝', cancelled: '已撤销', expired: '已过期',
  };
  /* 状态配色（契约约定）：
       filled → up、rejected → down、pending → accent、cancelled/expired → dim
     其余状态（submitted / acked）契约未约定，按语义就近取色，仅作展示 */
  const STATUS_CLS = {
    pending: 'accent', filled: 'up', rejected: 'down', cancelled: 'dim', expired: 'dim',
    submitted: 'accent', acked: 'up',
  };
  const SIDE_LABEL = { buy: '买', sell: '卖' };
  const SIDE_CLS = { buy: 'up', sell: 'down' };
  const INTENT_LABEL = { open: '开仓', add: '加仓', reduce: '减仓', close: '清仓' };
  const ACTION_LABEL = {
    buy: '买入', add: '加仓', hold: '持有', reduce: '减仓', sell: '卖出',
    watch: '观望', avoid: '回避', open: '开仓', close: '清仓',
  };

  /* ------------------------------------------------------------ 小工具 */

  /* 空值统一降级为「—」 */
  function text(v, d) {
    if (v === null || v === undefined || v === '') return d === undefined ? '—' : d;
    return String(v);
  }

  function clip(s, n) {
    const t = String(s === null || s === undefined ? '' : s);
    return t.length > n ? t.slice(0, n) + '…' : t;
  }

  /* 时间展示：毫秒 / 秒时间戳 / ISO 串都能吃，取不到就「—」 */
  function timeText(v) {
    if (v === null || v === undefined || v === '') return '—';
    if (isNum(v)) return F.clock(v < 1e12 ? v * 1000 : v);
    const s = String(v);
    if (s.length >= 19) return s.slice(5, 19).replace('T', ' ');
    return s;
  }

  /* 权重 / 置信度这类「小数 or 百分数」口径未定的字段：
     |v| ≤ 1.5 视为小数口径并按百分比展示（与 advisor.js 的 asPct 同一思路），
     仅作展示，不参与任何提交值换算 */
  function ratioPct(v) {
    if (!isNum(v)) return '—';
    return F.num(Math.abs(v) <= 1.5 ? v * 100 : v, 0) + '%';
  }

  function boolText(v) {
    if (v === true) return '是';
    if (v === false) return '否';
    return '—';
  }

  /* 代码列表：数组或「逗号 / 空格 / 换行分隔的字符串」都接受，统一大写去重 */
  function codesOf(v) {
    let arr = [];
    if (Array.isArray(v)) arr = v.slice();
    else if (typeof v === 'string') arr = v.split(SEP);
    const out = [];
    arr.forEach((x) => {
      const c = String(x === null || x === undefined ? '' : x).trim().toUpperCase();
      if (c && out.indexOf(c) < 0) out.push(c);
    });
    return out;
  }

  function orderTs(o) {
    const v = o && o.createdAt;
    if (isNum(v)) return v < 1e12 ? v * 1000 : v;
    const t = Date.parse(String(v === undefined || v === null ? '' : v));
    return isFinite(t) ? t : 0;
  }

  /* 推送 / 响应体里「委托单」的取法：{order:…}、{rows:[…]} 或裸对象都兼容 */
  function asOrder(p) {
    if (!p || typeof p !== 'object') return null;
    if (p.order && typeof p.order === 'object') return p.order;
    if (p.id || p.code || p.status) return p;
    return null;
  }

  function marketText(m) { return MARKET_LABEL[m] || text(m, '—'); }

  /* ------------------------------------------------------------ 视图 */

  function mount(root, ctx) {
    const state = (ctx && ctx.state) || {};
    const viewMarket = state.market === 'us' ? 'us' : 'cn';

    const st = {
      market: viewMarket,
      /* 服务端真相 */
      config: null, configNote: '', gates: null, accountId: '', account: null, counts: null,
      /* 委托单：本地窗口（服务端分页 + 推送 upsert） */
      orders: [], ordersTotal: 0, ordersLoading: false,
      filters: { status: '', market: '', code: '' },
      /* 上一次「立即扫描」结果 */
      plan: null, planTask: null, skipped: [], planAt: null,
      /* 成交回报流水（推送 onFill） */
      fills: [],
      /* 导出结果 */
      exportText: '', exportAt: null, exportCount: null,
      /* 文案 / 状态 */
      note: '', cfgHint: '改动即保存（乐观更新，失败自动回滚）', opHint: '',
      cfgFocused: false, cfgPending: false, tokenEdited: false,
      auto: true, destroyed: false,
      /* 推送句柄与状态 */
      push: { handle: null, state: '', lastError: '', errToasted: false },
    };
    let timer = null;           /* 15 秒兜底刷新 */
    let pollBusy = false;

    const toast = (msg, type) => {
      if (ctx && typeof ctx.toast === 'function') {
        try { ctx.toast(msg, type); } catch (e) { /* toast 失败不能影响视图 */ }
      }
    };

    /* ------------------------------------------------------ 持久节点 */

    const noticeHost = h('div');                                     /* 说明区（静态） */
    const connChipHost = h('span', { class: 'legend-inline', style: { gap: '8px' } });
    const tradeChipHost = h('span', { class: 'legend-inline', style: { gap: '8px' }, dataset: { host: 'trade-state' } });
    const noteHost = h('span', { class: 'hint dim3', dataset: { host: 'note' } });
    const cfgHost = h('div', { dataset: { host: 'config' } });
    const cfgHintHost = h('span', { class: 'hint', text: '改动即保存（乐观更新，失败自动回滚）' });
    const metricHost = h('div', { class: 'metric-list', dataset: { host: 'metrics' } });
    const gateHost = h('div', { class: 'metric-list', dataset: { host: 'gates' } });
    const posHost = h('div', { dataset: { host: 'positions' } });
    const orderHost = h('div', { dataset: { host: 'orders' } });
    const planHost = h('div', { dataset: { host: 'plan' } });
    const skipHost = h('div', { dataset: { host: 'skipped' } });
    const fillHost = h('div', { class: 'news-list', dataset: { host: 'fills' } });
    const opHost = h('div');
    const opHintHost = h('span', { class: 'hint', dataset: { host: 'op-hint' } });
    const filterHost = h('div', { dataset: { host: 'filters' } });
    const exportHost = h('pre', {
      class: 'monospaced',
      dataset: { host: 'export' },
      style: {
        margin: '0', padding: '10px 12px', border: '1px solid var(--line)',
        borderRadius: 'var(--r)', background: 'var(--surface)', color: 'var(--text-2)',
        maxHeight: '320px', overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
      },
      text: '尚未导出：点上方「导出待执行意图」拉取 GET /api/trade/export',
    });

    /* 「执行计划」口令输入框：持久节点，配置刷新时只补默认值，不打断用户输入 */
    const tokenInp = h('input', {
      class: 'inp', style: { width: '190px' }, dataset: { act: 'confirm-token' },
      placeholder: 'confirmToken（防误触口令）',
      title: 'POST /api/trade/execute 的 confirm 字段；默认填入配置里的 confirmToken，可修改',
    });
    tokenInp.addEventListener('input', () => { st.tokenEdited = true; });

    /* --------------------------------------------------- 接口（带兜底） */

    function apiConfig() {
      if (typeof api.tradeConfig === 'function') return api.tradeConfig();
      return api.get('trade/config', null, { noDedupe: true });
    }
    function apiConfigSave(patch) {
      if (typeof api.tradeConfigSave === 'function') return api.tradeConfigSave(patch);
      return api.post('trade/config', { patch });
    }
    function apiAccount(market) {
      if (typeof api.tradeAccount === 'function') return api.tradeAccount(market);
      return api.get('trade/account', { market }, { noDedupe: true });
    }
    function apiReset(market) {
      if (typeof api.tradeReset === 'function') return api.tradeReset(market);
      return api.post('trade/reset', { market });
    }
    function apiPlan(body) {
      if (typeof api.tradePlan === 'function') return api.tradePlan(body);
      return api.post('trade/plan', body);
    }
    function apiExecute(body) {
      if (typeof api.tradeExecute === 'function') return api.tradeExecute(body);
      return api.post('trade/execute', body);
    }
    function apiCancel(id) {
      if (typeof api.tradeCancel === 'function') return api.tradeCancel(id);
      return api.post('trade/cancel', { id });
    }
    function apiClose(market, code, qty) {
      const body = { market, code };
      if (isNum(qty) && qty > 0) body.qty = qty;
      if (typeof api.tradeClose === 'function') return api.tradeClose(market, code, qty);
      return api.post('trade/close', body);
    }
    function apiOrders(params) {
      if (typeof api.tradeOrders === 'function') return api.tradeOrders(params);
      return api.get('trade/orders', params, { noDedupe: true });
    }
    function apiExport(params) {
      if (typeof api.tradeExport === 'function') return api.tradeExport(params);
      return api.get('trade/export', params, { noDedupe: true });
    }
    function apiAck(id, extRef, status) {
      const body = { id, extRef };
      if (status) body.status = status;
      if (typeof api.tradeAck === 'function') return api.tradeAck(id, extRef, status);
      return api.post('trade/ack', body);
    }

    /* 复制：优先剪贴板 API，失败回落到「选中 + execCommand」，都不行就提示手动复制 */
    function copyText(value, okMsg) {
      const t = String(value === null || value === undefined ? '' : value);
      if (!t) { toast('没有可复制的内容', 'warn'); return; }
      const fallback = () => {
        try {
          if (typeof document !== 'undefined' && document.execCommand) {
            const tmp = h('input', { class: 'inp', value: t });
            tmp.style.position = 'fixed';
            tmp.style.opacity = '0';
            if (document.body && document.body.appendChild) {
              document.body.appendChild(tmp);
              tmp.select();
              if (document.execCommand('copy')) { toast(okMsg || '已复制', 'ok'); document.body.removeChild(tmp); return; }
              document.body.removeChild(tmp);
            }
          }
        } catch (e) { /* 继续走下面的提示 */ }
        toast('浏览器不允许自动复制，请手动选中后复制', 'warn');
      };
      try {
        if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          navigator.clipboard.writeText(t).then(() => toast(okMsg || '已复制', 'ok'), fallback);
          return;
        }
      } catch (e) { /* 走回落 */ }
      fallback();
    }

    /* ------------------------------------------------ 顶部说明 + 状态 */

    function paintTradeState() {
      clear(tradeChipHost);
      const cfg = st.config;
      if (!cfg) {
        /* 配置读不到时不能假装知道开关状态：只声明「默认按关闭处理」 */
        tradeChipHost.appendChild(h('span', {
          class: 'chip', text: '自动交易：配置未知（默认按关闭处理）',
          title: 'GET /api/trade/config 未返回，无法确认服务端开关状态；在读到配置前不要假定它是开启的',
        }));
        tradeChipHost.appendChild(h('span', { class: 'chip', text: '模式：—' }));
        tradeChipHost.appendChild(h('span', { class: 'chip', text: '市场：' + marketText(st.market) }));
        tradeChipHost.appendChild(h('span', { class: 'chip', text: '配置更新：—' }));
        return;
      }
      const on = cfg.enabled === true;
      tradeChipHost.appendChild(h('span', {
        class: 'chip' + (on ? ' warn' : ''),
        text: on ? '自动交易：已开启' : '自动交易：已关闭（默认关闭）',
        title: on
          ? '已开启自动交易：仍受风控上限与 confirmToken 口令约束，且仅在 dryrun / paper 模拟通道内成交'
          : '总开关默认关闭；关闭时「立即扫描」仍可出计划，但不会产生任何成交',
      }));
      tradeChipHost.appendChild(h('span', {
        class: 'chip' + (String(cfg.mode) === 'paper' ? ' accent' : ''),
        text: '模式：' + (MODE_SHORT[cfg.mode] || text(cfg.mode)),
      }));
      tradeChipHost.appendChild(h('span', { class: 'chip', text: '市场：' + marketText(st.market) }));
      tradeChipHost.appendChild(h('span', {
        class: 'chip',
        text: '配置更新：' + (cfg.updatedAt ? timeText(cfg.updatedAt) : '—'),
      }));
    }

    function chipTitle() {
      return '数据来自 GET /api/stream/trade（order / fill / account / config / note）；' +
        '连接不可用时 stream 层自动降级为 15 秒轮询兜底。';
    }

    /* 推送状态 chip：文案与配色由 AD.stream.chip 统一提供 */
    function paintChip(state, title) {
      const s = window.AD.stream;
      clear(connChipHost);
      const next = state || st.push.state || 'connecting';
      if (s && typeof s.chip === 'function') {
        connChipHost.appendChild(s.chip(next, title || chipTitle()));
      } else {
        connChipHost.appendChild(h('span', { class: 'chip', text: '推送状态：' + next }));
      }
      st.push.state = next;
    }

    function paintNote() {
      const bits = [];
      if (st.note) bits.push(st.note);
      if (st.push.lastError) bits.push('推送提示：' + clip(st.push.lastError, 120));
      noteHost.textContent = bits.join(' · ') || '等待服务端提示…';
    }

    function renderNotice() {
      clear(noticeHost);
      noticeHost.appendChild(h('div', {
        class: 'adjust-box', style: { borderStyle: 'solid', borderColor: 'var(--accent-line)' },
      }, [
        h('div', { class: 'legend-inline', style: { gap: '10px', alignItems: 'center', marginBottom: '8px' } }, [
          h('span', { class: 'chip warn', text: '模拟盘 / 计划盘' }),
          h('strong', { text: '不接任何真实券商通道，不会产生真实委托' }),
        ]),
        h('div', { style: { lineHeight: '1.9' } }, [
          h('div', { text: '· 本页的委托、成交、持仓与盈亏全部由本地服务按公开行情模拟撮合并记账，只写本地存储，' +
            '不会向任何券商下单，也不会动到真实资金。' }),
          h('div', { text: '· 「自动交易」总开关默认关闭，需要在下方配置区显式开启；关闭时仍可「立即扫描」出计划，但不会写入成交。' }),
          h('div', { text: '· 委托单状态流转：待执行（pending）→ 已成交（filled）或已拒绝（rejected）；被风控拦下的标的不会生成委托，' +
            '只记入「被风控拦截」列表并写明原因。' }),
        ]),
      ]));

      noticeHost.appendChild(h('div', { class: 'adjust-box', style: { marginTop: '10px' } }, [
        h('div', { class: 'legend-inline', style: { gap: '10px', alignItems: 'center', marginBottom: '8px' } }, [
          h('span', { class: 'chip accent', text: '自动交易接口' }),
          h('span', { class: 'dim', text: '把「待执行意图」交给外部系统（你自己的券商桥接）执行' }),
        ]),
        h('div', { class: 'monospaced', style: { lineHeight: '2' } }, [
          h('div', { text: '① GET  /api/trade/export?limit=50        取出待执行意图（含 payload，可直接消费）' }),
          h('div', { text: '② 外部系统按意图执行（这一步在本项目之外，由你自己的券商桥接负责）' }),
          h('div', { text: '③ POST /api/trade/ack { id, extRef, status }   把执行结果回执回来' }),
        ]),
        h('div', { style: { lineHeight: '1.9', marginTop: '8px' } }, [
          h('div', { text: '· X-Trade-Confirm / confirmToken 只是防误触口令，**不是安全边界**：本服务是本机单用户工具，' +
            '任何能访问该端口的人都能直接调接口，口令只能防止误点。' }),
          h('div', { text: '· 因此不要把这个端口暴露到公网或不可信局域网；本页所有「执行类」按钮都带二次确认或口令校验。' }),
        ]),
      ]));

      noticeHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '10px', alignItems: 'center', gap: '10px' } }, [
        connChipHost, tradeChipHost, noteHost,
      ]));
    }

    /* --------------------------------------------------------- 配置区 */

    /** 提交配置改动：乐观更新 → POST → 失败回滚（并回滚界面上的输入框） */
    async function saveConfig(key, value, rollback) {
      if (!st.config) { toast('配置尚未就绪，稍后再试', 'warn'); return; }
      const prev = st.config[key];
      st.config[key] = value;                       /* 乐观更新：界面立即响应 */
      paintTradeState();
      cfgHintHost.textContent = '保存中…（' + key + '）';
      try {
        const patch = {};
        patch[key] = value;
        const res = await apiConfigSave(patch);
        if (st.destroyed) return;
        if (res && res.config) st.config = res.config;
        if (res && res.note) st.configNote = res.note;
        cfgHintHost.textContent = '已保存「' + key + '」· ' + F.clock(Date.now()) +
          (st.configNote ? ' · ' + st.configNote : '');
        toast('配置已保存：' + key, 'ok');
        /* 非强制重绘：正在输入时自动挂起，失焦后再补（不打断、不吞点击） */
        renderConfig();
        paintTradeState();
      } catch (e) {
        if (st.destroyed) return;
        st.config[key] = prev;                      /* 回滚服务端真相 */
        if (typeof rollback === 'function') { try { rollback(); } catch (err) { /* 忽略 */ } }
        cfgHintHost.textContent = '保存失败，已回滚：' + e.message;
        toast('配置保存失败，已回滚到上一次生效值：' + e.message, 'err');
        renderConfig(true);
        paintTradeState();
      }
    }

    function toggleCfg(key, label, title) {
      const on = !!(st.config && st.config[key] === true);   /* 与状态 chip 同一判定口径（契约里是布尔） */
      const btn = h('button', {
        class: 'btn ghost sm' + (on ? ' active' : ''),
        text: label,
        dataset: { cfg: key },
        title: title || '',
      });
      btn.addEventListener('click', () => {
        /* 先把按钮外观切到新状态：乐观更新的即时反馈不能依赖整表重绘
           （重绘可能因为「正在输入」被挂起，也可能在 mousedown 与 click 之间
             把节点换掉，导致这一次点击丢失） */
        const next = !(st.config && st.config[key] === true);
        btn.classList.toggle('active', next);
        saveConfig(key, next, () => renderConfig(true));
      });
      return btn;
    }

    function numCfg(key, label, title) {
      const cfg = st.config || {};
      const cur = cfg[key];
      const inp = h('input', {
        class: 'inp', value: isNum(cur) ? String(cur) : '', placeholder: '—',
        step: 'any', dataset: { cfg: key }, title: title || '',
      });
      inp.addEventListener('change', () => {
        const raw = String(inp.value || '').trim();
        if (!raw) {
          toast('「' + label + '」不能为空，已恢复原值', 'warn');
          renderConfig(true);
          return;
        }
        const num = Number(raw);
        if (!isFinite(num)) {
          toast('「' + label + '」不是有效数值，已恢复原值', 'err');
          renderConfig(true);
          return;
        }
        if (isNum(cur) && Math.abs(cur - num) < 1e-9) return;   /* 值没变：不发请求 */
        saveConfig(key, num, () => renderConfig(true));
      });
      return h('div', { class: 'field' }, [h('label', { text: label }), inp]);
    }

    function codesCfg(key, label, title) {
      const cfg = st.config || {};
      const ta = h('textarea', {
        class: 'inp', rows: 3, dataset: { cfg: key }, title: title || '',
        style: { height: 'auto', padding: '6px 8px', lineHeight: '1.6', resize: 'vertical' },
        value: codesOf(cfg[key]).join('\n'),
      });
      ta.addEventListener('change', () => {
        const list = codesOf(ta.value);
        const cur = codesOf(cfg[key]);
        if (list.join(',') === cur.join(',')) return;            /* 无变化：不发请求 */
        saveConfig(key, list, () => renderConfig(true));
      });
      return h('div', { class: 'field wide', style: { alignItems: 'flex-start' } }, [
        h('label', { text: label, style: { paddingTop: '6px' } }), ta,
      ]);
    }

    function renderConfig(force) {
      if (st.destroyed) return;
      /* 正在输入时不重绘（重绘会把输入框连同光标一起换掉）；等失焦后补一次 */
      if (!force && st.cfgFocused) { st.cfgPending = true; return; }
      clear(cfgHost);
      paintTradeState();
      const cfg = st.config;
      if (!cfg) {
        cfgHost.appendChild(ui.empty('配置尚未就绪：GET /api/trade/config 无返回时展示此空态（不臆造默认值）；' +
          '接口就绪后这里会出现总开关、模式与各风控上限。'));
        return;
      }

      const modeSel = h('select', { class: 'inp', dataset: { cfg: 'mode' } });
      ['dryrun', 'paper'].forEach((m) => modeSel.appendChild(h('option', { value: m, text: MODE_LABEL[m] })));
      modeSel.value = String(cfg.mode || 'dryrun');
      modeSel.addEventListener('change', () => {
        if (String(cfg.mode) === modeSel.value) return;
        saveConfig('mode', modeSel.value, () => renderConfig(true));
      });

      const webhookInp = h('input', {
        class: 'inp', value: text(cfg.webhook, ''), placeholder: '留空表示不外发下单指令',
        dataset: { cfg: 'webhook' }, title: '下单指令外发地址（本机单用户工具，留空 = 不外发）',
      });
      webhookInp.addEventListener('change', () => {
        const v = String(webhookInp.value || '').trim();
        if (v === String(cfg.webhook || '')) return;
        saveConfig('webhook', v, () => renderConfig(true));
      });

      const tokenRead = h('input', {
        class: 'inp', value: text(cfg.confirmToken, ''), readonly: 'readonly',
        dataset: { cfg: 'confirmToken' },
        title: '防误触口令：仅用于 POST /api/trade/execute 的 confirm 校验，不是安全边界',
      });

      cfgHost.appendChild(h('div', { class: 'run-form' }, [
        h('div', { class: 'field' }, [
          h('label', { text: '总开关' }),
          toggleCfg('enabled', '自动交易（默认关闭）', '关闭时不会产生任何成交；开启后仍受风控上限与口令约束'),
        ]),
        h('div', { class: 'field' }, [h('label', { text: '模式' }), modeSel]),
        numCfg('capital', '模拟本金'),
        numCfg('maxWeight', '单只权重上限', '按服务端口径原样提交，不做百分比换算'),
        numCfg('maxPositions', '持仓只数上限'),
        numCfg('maxOrdersPerDay', '单日委托上限'),
        numCfg('maxOrderAmount', '单笔金额上限'),
        numCfg('minConfidence', '置信度门槛', '按服务端口径原样提交，不做百分比换算'),
        numCfg('interval', '扫描间隔（秒）'),
        h('div', { class: 'field' }, [
          h('label', { text: '减仓' }),
          toggleCfg('allowReduce', '允许减仓', '关闭后只允许开仓 / 加仓，减仓信号会被风控拦下'),
        ]),
        h('div', { class: 'field wide' }, [
          h('label', { text: '防误触口令' }), tokenRead,
          h('button', {
            class: 'btn ghost sm', text: '复制', dataset: { act: 'copy-token' },
            on: { click: () => copyText(cfg.confirmToken, '防误触口令已复制') },
          }),
        ]),
      ]));

      cfgHost.appendChild(h('div', { class: 'run-form', style: { marginTop: '10px' } }, [
        codesCfg('universe', '标的池', '逗号 / 空格 / 换行分隔，代码统一大写；留空表示用服务端默认股票池'),
        codesCfg('whitelist', '白名单', '仅这些代码允许自动下单；留空表示不额外限制'),
        h('div', { class: 'field wide' }, [h('label', { text: '下单指令外发地址' }), webhookInp]),
      ]));

      cfgHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
        '标的池 ' + codesOf(cfg.universe).length + ' 个 · 白名单 ' + codesOf(cfg.whitelist).length + ' 个' +
        (cfg.accountId || st.accountId ? ' · 账户 ' + text(cfg.accountId || st.accountId) : '') +
        (cfg.market ? ' · 配置市场 ' + marketText(cfg.market) : '') +
        ' · 所有改动即时 POST /api/trade/config（失败自动回滚）',
      ]));
    }

    /* 输入聚焦时挂起配置重绘，失焦后补一次，避免把正在输入的内容冲掉。
       补绘延迟 150ms：若在 mousedown 与 click 之间重绘，按钮会被换成新节点，
       这一次点击就丢了（用户看到「点了没反应」）——延后一拍即可避开。 */
    cfgHost.addEventListener('focusin', () => { st.cfgFocused = true; });
    cfgHost.addEventListener('focusout', () => {
      setTimeout(() => {
        if (st.destroyed) return;
        const act = typeof document !== 'undefined' ? document.activeElement : null;
        st.cfgFocused = !!(act && typeof cfgHost.contains === 'function' && cfgHost.contains(act));
        if (!st.cfgFocused && st.cfgPending) { st.cfgPending = false; renderConfig(true); }
      }, 150);
    });

    /* ----------------------------------------------------- 账户总览 */

    function metricCell(k, v, cls, title) {
      return h('div', { class: 'metric' }, [
        h('div', { class: 'k', text: k }),
        h('div', { class: 'v ' + (cls || ''), text: v, title: title || '' }),
      ]);
    }

    /* 当日已用委托数：优先后端 counts.ordersToday，其次 gates，最后「—」 */
    function ordersTodayText() {
      const c = st.counts || {};
      if (isNum(c.ordersToday)) return String(c.ordersToday);
      const g = st.gates || {};
      const cands = ['ordersToday', 'orders_today', 'usedOrders', 'used'];
      for (let i = 0; i < cands.length; i++) {
        if (isNum(g[cands[i]])) return String(g[cands[i]]);
      }
      return '—';
    }

    function limitText(v) {
      if (isNum(v)) return String(v);
      return '—';
    }

    function renderMetrics() {
      clear(metricHost);
      const acc = st.account;
      if (!acc) {
        metricHost.appendChild(ui.empty('账户数据未就绪：GET /api/trade/account?market=' + st.market +
          ' 无有效返回时展示此空态。模拟账户由服务端在首次读取时创建（即「初始化账户」），' +
          '本页不会自己造一个账户，也不会臆造任何数字。'));
        return;
      }
      const mkt = acc.market || st.market;
      const cfg = st.config || {};
      const sameDay = ordersTodayText();
      const dayCap = isNum(acc.maxOrdersPerDay) ? acc.maxOrdersPerDay : cfg.maxOrdersPerDay;
      const cells = [
        ['账户 ID', text(acc.accountId || st.accountId)],
        ['模式', MODE_LABEL[acc.mode || cfg.mode] || text(acc.mode || cfg.mode)],
        ['初始本金', F.amt(acc.initial, mkt)],
        ['现金', F.amt(acc.cash, mkt)],
        ['持仓市值', F.amt(acc.marketValue, mkt)],
        ['总权益', F.amt(acc.equity, mkt)],
        ['累计盈亏', F.amt(acc.pnl, mkt) + '　' + F.pct(acc.returnPct), F.dir(acc.pnl),
          '金额 / 收益率（相对初始本金）'],
        ['已实现盈亏', F.amt(acc.realizedPnl, mkt), F.dir(acc.realizedPnl)],
        ['累计费用', F.amt(acc.feeTotal, mkt), '', '手续费 + 滑点合计（模拟口径）'],
        ['持仓只数', isNum(acc.positionCount) ? acc.positionCount + ' 只' : '—'],
        ['当日委托', sameDay + ' / ' + limitText(dayCap), 'dim', '当日已用 / 上限'],
      ];
      cells.forEach((c) => metricHost.appendChild(metricCell(c[0], c[1], c[2], c[3])));
    }

    /* ----------------------------------------------------- 风控快照 */

    /* gates 的字段名契约里没有逐项约定，这里按常见写法逐个候选取；
       取不到就回落到配置值，并在「生效上限来源」里如实标注，不假装是服务端生效值 */
    function gatePick(keys, fallback) {
      const g = st.gates || {};
      for (let i = 0; i < keys.length; i++) {
        const v = g[keys[i]];
        if (v !== undefined && v !== null) return { v: v, src: true };
      }
      return { v: fallback, src: false };
    }

    function renderGates() {
      clear(gateHost);
      const cfg = st.config || {};
      const acc = st.account || {};
      const hasGates = !!(st.gates && Object.keys(st.gates).length);
      const w = gatePick(['maxWeight', 'max_weight', 'maxSingleWeight', 'maxPositionWeight', 'singleMaxWeight'], cfg.maxWeight);
      const perDay = gatePick(['maxOrdersPerDay', 'max_orders_per_day', 'maxOrders'], cfg.maxOrdersPerDay);
      const perOrder = gatePick(['maxOrderAmount', 'max_order_amount', 'maxAmountPerOrder', 'maxOrderValue'], cfg.maxOrderAmount);
      const maxPos = gatePick(['maxPositions', 'max_positions', 'maxHoldings'], cfg.maxPositions);
      const minConf = gatePick(['minConfidence', 'min_confidence', 'confidenceFloor'], cfg.minConfidence);
      const reduce = gatePick(['allowReduce', 'allow_reduce'], cfg.allowReduce);
      const cells = [
        ['单只权重上限', ratioPct(w.v), '', w.src ? '来源：服务端 gates' : '来源：配置值（gates 未提供该项）'],
        ['单日委托上限', limitText(perDay.v) + '　已用 ' + ordersTodayText(),
          '', perDay.src ? '来源：服务端 gates' : '来源：配置值（gates 未提供该项）'],
        ['单笔金额上限', F.amt(perOrder.v, acc.market || st.market), '',
          perOrder.src ? '来源：服务端 gates' : '来源：配置值（gates 未提供该项）'],
        ['持仓只数上限', limitText(maxPos.v) + '　当前 ' + (isNum(acc.positionCount) ? acc.positionCount : '—')],
        ['置信度门槛', ratioPct(minConf.v)],
        ['允许减仓', boolText(reduce.v), reduce.v === false ? 'dim' : ''],
        ['生效上限来源', hasGates ? '服务端 gates（当前生效值）' : '配置值兜底（gates 缺失，非服务端生效值）',
          hasGates ? '' : 'dim'],
      ];
      cells.forEach((c) => gateHost.appendChild(metricCell(c[0], c[1], c[2], c[3])));
    }

    /* --------------------------------------------------------- 持仓 */

    function posPriceCell(p) {
      if (p.priced === false) {
        return h('span', {
          class: 'num dim3', text: '—',
          title: '无可用报价，按成本价暂估（市值 / 盈亏可能偏离真实值）',
        });
      }
      return h('span', { class: 'num ' + F.dir(p.pnl), text: F.price(p.lastPrice, p.market || st.market) });
    }

    function renderPositions() {
      clear(posHost);
      const acc = st.account;
      const rows = (acc && Array.isArray(acc.positions)) ? acc.positions : [];
      if (!acc) { posHost.appendChild(ui.empty('账户未就绪，暂无持仓')); return; }
      if (!rows.length) {
        posHost.appendChild(ui.empty('当前无持仓：模拟账户尚未买入，或已全部平仓。可先「立即扫描」生成计划。'));
        return;
      }
      const mkt = acc.market || st.market;
      posHost.appendChild(ui.tbl({
        cols: [
          {
            key: 'code', label: '标的', noSort: true,
            render: (p) => h('span', {}, [
              h('span', { class: 'name', text: text(p.name, p.code) }),
              h('span', { class: 'code', text: (p.market === 'us' ? 'US:' : '') + text(p.code) }),
            ]),
            onCell: (p) => { if (ctx && typeof ctx.openSymbol === 'function') ctx.openSymbol(p.market || mkt, p.code, p.name); },
          },
          { key: 'qty', label: '数量', cls: 'n', noSort: true, render: (p) => h('span', { class: 'num', text: text(p.qty) }) },
          { key: 'avgPrice', label: '成本', cls: 'n', noSort: true, render: (p) => h('span', { class: 'num', text: F.price(p.avgPrice, p.market || mkt) }) },
          { key: 'lastPrice', label: '最新价', cls: 'n', noSort: true, render: (p) => posPriceCell(p) },
          { key: 'marketValue', label: '市值', cls: 'n', noSort: true, render: (p) => h('span', { class: 'num', text: F.amt(p.marketValue, mkt) }) },
          {
            key: 'pnl', label: '盈亏', cls: 'n', noSort: true,
            render: (p) => h('span', { class: 'num ' + F.dir(p.pnl) }, [
              h('span', { text: F.amt(p.pnl, mkt) }),
              h('small', { class: 'dim3', text: ' ' + F.pct(p.pnlPct) }),
            ]),
          },
          { key: 'weight', label: '权重', cls: 'n', noSort: true, render: (p) => h('span', { class: 'num', text: ratioPct(p.weight) }) },
          {
            key: 'openedAt', label: '建仓时间', noSort: true,
            render: (p) => h('span', { class: 'num dim', text: p.openedAt ? timeText(p.openedAt) : '—' }),
          },
          {
            key: 'act', label: '操作', noSort: true, width: '96px',
            render: (p) => h('div', { style: { display: 'flex', gap: '5px' } }, [
              h('button', {
                class: 'btn ghost sm', text: '平仓', dataset: { act: 'close' },
                title: 'POST /api/trade/close（模拟撮合，可全平）',
                on: {
                  click: (e) => {
                    if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
                    closePosition(p);
                  },
                },
              }),
            ]),
          },
        ],
        rows,
        rowKey: (p) => p.code,
        maxHeight: '420px',
        emptyText: '暂无持仓',
      }));
    }

    async function closePosition(p) {
      const mkt = p.market || st.market;
      const label = text(p.name, p.code);
      const ok = window.confirm('确认平仓 ' + label + '（' + text(p.qty) + ' 股）？\n' +
        '模拟盘会按最新报价撮合（无报价时按成本价暂估），只写本地记录，不会产生真实委托。');
      if (!ok) return;
      try {
        const res = await apiClose(mkt, p.code, p.qty);
        if (st.destroyed) return;
        toast('已提交平仓：' + label, 'ok');
        applyAccountPayload(res);
        await Promise.all([loadAccount(true), loadOrders(false)]);
      } catch (e) {
        if (st.destroyed) return;
        toast('平仓失败：' + e.message, 'err');
      }
    }

    /* ------------------------------------------------------- 委托单 */

    function chipEl(label, cls) {
      return h('span', { class: 'chip' + (cls ? ' ' + cls : ''), text: label });
    }

    function statusChip(o) {
      const s = String(o && o.status || '');
      return chipEl(STATUS_LABEL[s] || text(s), STATUS_CLS[s] === undefined ? '' : STATUS_CLS[s]);
    }

    function sideChip(o) {
      const s = String(o && o.side || '');
      return chipEl(SIDE_LABEL[s] || text(s), SIDE_CLS[s] || '');
    }

    function intentText(o) {
      const i = String(o && o.intent || '');
      if (INTENT_LABEL[i]) return INTENT_LABEL[i];
      const a = String(o && o.action || '');
      return ACTION_LABEL[a] || text(i !== '' ? i : a);
    }

    function sortOrders(list) {
      return list.slice().sort((a, b) => orderTs(b) - orderTs(a));
    }

    /* 本地再过滤一次：服务端筛选负责取数，推送进来的新行也要按同一口径落位 */
    function visibleOrders() {
      const f = st.filters;
      const code = String(f.code || '').trim().toUpperCase();
      return st.orders.filter((o) => {
        if (f.status && String(o.status || '') !== f.status) return false;
        if (f.market && String(o.market || '') !== f.market) return false;
        if (code && String(o.code || '').toUpperCase().indexOf(code) < 0) return false;
        return true;
      });
    }

    function upsertOrder(o) {
      if (!o || !o.id) return false;
      const i = st.orders.findIndex((x) => x.id === o.id);
      if (i >= 0) st.orders[i] = o; else st.orders.push(o);
      if (st.orders.length > ORDERS_MAX) st.orders = sortOrders(st.orders).slice(0, ORDERS_MAX);
      return true;
    }

    function renderOrders() {
      clear(orderHost);
      const rows = sortOrders(visibleOrders());
      const pager = h('div', { class: 'pager' }, [
        h('span', {
          text: '共 ' + text(st.ordersTotal) + ' 条 · 已载入 ' + st.orders.length + ' 条' +
            (rows.length !== st.orders.length ? ' · 当前筛选命中 ' + rows.length + ' 条' : ''),
        }),
        h('span', { class: 'spacer' }),
        h('button', {
          class: 'btn sm', text: st.ordersLoading ? '加载中…' : '加载更多', dataset: { act: 'more' },
          disabled: st.ordersLoading || st.orders.length >= st.ordersTotal,
          title: '每次加载 ' + ORDERS_LIMIT + ' 条',
          on: { click: () => loadOrders(true) },
        }),
        h('button', {
          class: 'btn sm ghost', text: '刷新', dataset: { act: 'orders-refresh' },
          on: { click: () => loadOrders(false) },
        }),
      ]);

      orderHost.appendChild(ui.tbl({
        cols: [
          { key: 'createdAt', label: '时间', noSort: true, render: (o) => h('span', { class: 'num dim', text: timeText(o.createdAt) }) },
          {
            key: 'code', label: '标的', noSort: true,
            render: (o) => h('span', {}, [
              h('span', { class: 'name', text: text(o.name, o.code) }),
              h('span', { class: 'code', text: (o.market === 'us' ? 'US:' : '') + text(o.code) }),
            ]),
            onCell: (o) => { if (ctx && typeof ctx.openSymbol === 'function') ctx.openSymbol(o.market || st.market, o.code, o.name); },
          },
          { key: 'side', label: '方向', noSort: true, width: '62px', render: (o) => sideChip(o) },
          { key: 'intent', label: '意图', noSort: true, render: (o) => h('span', { text: intentText(o) }) },
          { key: 'status', label: '状态', noSort: true, render: (o) => statusChip(o) },
          { key: 'qty', label: '数量', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: text(o.qty) }) },
          { key: 'limitPrice', label: '委托价', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: F.price(o.limitPrice, o.market || st.market) }) },
          { key: 'fillPrice', label: '成交价', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: F.price(o.fillPrice, o.market || st.market) }) },
          { key: 'amount', label: '金额', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: F.amt(o.amount, o.market || st.market) }) },
          { key: 'fee', label: '费用', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: isNum(o.fee) ? F.num(o.fee, 2) : '—' }) },
          {
            key: 'reason', label: '原因', noSort: true,
            render: (o) => h('span', { title: text(o.reason, '（服务端未给出原因）'), text: clip(o.reason, REASON_MAX) }),
          },
          {
            key: 'act', label: '操作', noSort: true, width: '84px',
            render: (o) => h('div', { style: { display: 'flex', gap: '5px' } }, [
              String(o.status) === 'pending'
                ? h('button', {
                  class: 'btn ghost sm', text: '撤单', dataset: { act: 'cancel' },
                  title: 'POST /api/trade/cancel（仅 pending 可撤）',
                  on: {
                    click: (e) => {
                      if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
                      cancelOrder(o);
                    },
                  },
                })
                : null,
              String(o.status) === 'submitted'
                ? h('button', {
                  class: 'btn ghost sm', text: '回执', dataset: { act: 'ack' },
                  title: 'POST /api/trade/ack：外部系统执行后回执（对接调试用）',
                  on: {
                    click: (e) => {
                      if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
                      ackOrder(o);
                    },
                  },
                })
                : null,
            ]),
          },
        ],
        rows,
        pager,
        maxHeight: '420px',
        compact: true,
        emptyText: '暂无委托单：点上方「立即扫描」生成计划，或调整筛选条件',
      }));
    }

    async function cancelOrder(o) {
      const ok = window.confirm('确认撤销委托单 ' + text(o.code) + '（' + text(o.id) + '）？');
      if (!ok) return;
      try {
        const res = await apiCancel(o.id);
        if (st.destroyed) return;
        if (res && res.order) upsertOrder(res.order);
        toast('已撤单：' + text(o.code), 'ok');
        renderOrders();
      } catch (e) {
        if (st.destroyed) return;
        toast('撤单失败：' + e.message, 'err');
      }
    }

    async function ackOrder(o) {
      let extRef = '';
      try { extRef = window.prompt('外部系统回执编号 extRef（可留空）', o.extRef || '') || ''; } catch (e) { extRef = ''; }
      try {
        const res = await apiAck(o.id, extRef, 'acked');
        if (st.destroyed) return;
        if (res && res.order) upsertOrder(res.order);
        toast('已回执：' + text(o.code), 'ok');
        renderOrders();
      } catch (e) {
        if (st.destroyed) return;
        toast('回执失败：' + e.message, 'err');
      }
    }

    /* --------------------------------------------- 本次计划 / 被拦截 */

    function renderPlan() {
      clear(planHost);
      const p = st.plan;
      if (!p) {
        planHost.appendChild(ui.empty('尚未扫描：点「立即扫描（只出计划）」生成计划（execute=false，不会成交）'));
        return;
      }
      const summary = p.adviceSummary || {};
      const acts = summary.actions || {};
      const actBits = Object.keys(acts).map((k) => (ACTION_LABEL[k] || k) + ' ' + acts[k]).join(' · ');
      planHost.appendChild(h('div', { class: 'legend-inline', style: { marginBottom: '8px', alignItems: 'center', gap: '10px' } }, [
        chipEl('本次计划', 'accent'),
        h('span', { text: '扫描时间 ' + (st.planAt ? F.clock(st.planAt) : '—') }),
        h('span', { text: '市场 ' + marketText(p.market || st.market) }),
        h('span', { text: '标的 ' + (Array.isArray(p.symbols) ? p.symbols.length : 0) + ' 个' }),
        h('span', { text: '研判 ' + text(summary.analyzed) + ' 个' }),
        actBits ? h('span', { text: actBits }) : null,
        h('span', { text: '生成委托 ' + ((p.orders || []).length) + ' 笔 · 成交 ' + text(p.filled) }),
      ]));
      const rows = Array.isArray(p.orders) ? p.orders : [];
      if (!rows.length) {
        planHost.appendChild(ui.empty('本次没有生成任何委托（可能是全部被风控拦截，或当前没有可执行信号）'));
        return;
      }
      planHost.appendChild(ui.tbl({
        cols: [
          {
            key: 'code', label: '标的', noSort: true,
            render: (o) => h('span', {}, [
              h('span', { class: 'name', text: text(o.name, o.code) }),
              h('span', { class: 'code', text: (o.market === 'us' ? 'US:' : '') + text(o.code) }),
            ]),
          },
          { key: 'side', label: '方向', noSort: true, width: '62px', render: (o) => sideChip(o) },
          { key: 'intent', label: '意图', noSort: true, render: (o) => h('span', { text: intentText(o) }) },
          { key: 'status', label: '状态', noSort: true, render: (o) => statusChip(o) },
          { key: 'qty', label: '数量', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: text(o.qty) }) },
          { key: 'limitPrice', label: '委托价', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: F.price(o.limitPrice, o.market || st.market) }) },
          { key: 'targetWeight', label: '目标权重', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: ratioPct(o.targetWeight) }) },
          { key: 'confidence', label: '置信度', cls: 'n', noSort: true, render: (o) => h('span', { class: 'num', text: isNum(o.confidence) ? F.num(o.confidence, 2) : '—' }) },
          {
            key: 'reason', label: '原因', noSort: true,
            render: (o) => h('span', { title: text(o.reason, '（服务端未给出原因）'), text: clip(o.reason, REASON_MAX) }),
          },
        ],
        rows,
        maxHeight: '340px',
        compact: true,
        emptyText: '本次没有生成委托',
      }));
    }

    /* 被风控拦截：每条必须写明「代码 + 档位 + 原因」 */
    function renderSkipped() {
      clear(skipHost);
      if (!st.plan) return;
      skipHost.appendChild(h('div', { class: 'legend-inline', style: { margin: '12px 0 6px', alignItems: 'center', gap: '10px' } }, [
        chipEl('被风控拦截', st.skipped.length ? 'warn' : ''),
        h('span', { text: st.skipped.length ? st.skipped.length + ' 条' : '本次没有被拦截的标的' }),
      ]));
      if (!st.skipped.length) return;
      const list = h('div', { class: 'news-list' });
      st.skipped.forEach((s) => {
        const code = text(s.code);
        const action = ACTION_LABEL[s.action] || text(s.action);
        const reason = text(s.reason, '（服务端未给出原因）');
        list.appendChild(h('div', { class: 'news-item' }, [
          h('div', { class: 'time', text: code }),
          h('div', { class: 'body' }, [
            h('div', { class: 'txt', title: reason }, [
              h('strong', { text: code }),
              '　' + action + '　',
              h('span', { class: 'chip warn', text: '被风控拦截' }),
            ]),
            h('div', { class: 'monospaced', style: { marginTop: '4px' }, text: '原因：' + reason }),
          ]),
        ]));
      });
      skipHost.appendChild(list);
    }

    /* ----------------------------------------------------- 成交回报 */

    function renderFills() {
      clear(fillHost);
      if (!st.fills.length) {
        fillHost.appendChild(ui.empty('暂无成交回报：推送 onFill 或撮合成交后这里会追加流水'));
        return;
      }
      st.fills.slice(0, FILL_MAX).forEach((f) => {
        const mkt = f.market || st.market;
        fillHost.appendChild(h('div', { class: 'news-item' }, [
          h('div', { class: 'time', text: timeText(f.at) }),
          h('div', { class: 'body' }, [
            h('div', { class: 'txt' }, [
              h('strong', { class: (SIDE_CLS[f.side] || '') , text: (SIDE_LABEL[f.side] || text(f.side)) + ' ' + text(f.name, f.code) }),
              '　' + text(f.qty) + ' 股 @ ' + F.price(f.fillPrice, mkt),
              isNum(f.amount) ? '　金额 ' + F.amt(f.amount, mkt) : '',
            ]),
            h('div', { style: { marginTop: '4px', display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' } }, [
              h('span', { class: 'phase-tag', text: INTENT_LABEL[f.intent] || text(f.intent) }),
              isNum(f.fee) ? h('span', { class: 'chip', text: '费用 ' + F.num(f.fee, 2) }) : null,
              isNum(f.slippage) ? h('span', { class: 'chip', text: '滑点 ' + F.num(f.slippage, 2) }) : null,
              h('span', { class: 'mono-sm', text: f.id ? '单号 ' + f.id : '单号 —' }),
            ]),
          ]),
        ]));
      });
    }

    /* --------------------------------------------------------- 导出 */

    function renderExport() {
      clear(exportHost);
      if (!st.exportText) {
        exportHost.textContent = '尚未导出：点上方「导出待执行意图」拉取 GET /api/trade/export';
        return;
      }
      exportHost.textContent = st.exportText;
    }

    /* ------------------------------------------------------- 操作区 */

    function planOrderIds() {
      const p = st.plan;
      const rows = (p && Array.isArray(p.orders)) ? p.orders : [];
      const ids = [];
      rows.forEach((o) => {
        if (!o || !o.id) return;
        const s = String(o.status || '');
        if (s === 'pending' || s === 'submitted') ids.push(o.id);
      });
      return ids;
    }

    async function runPlan() {
      const ok = window.confirm('立即扫描会触发一次 AI 研判并生成委托计划（execute=false：只出计划，不会成交）。\n' +
        '确认继续？');
      if (!ok) return;
      opHintHost.textContent = '扫描中…（多标的研判可能需要数秒）';
      try {
        const res = await apiPlan({ market: st.market, execute: false });
        if (st.destroyed) return;
        st.plan = res;
        st.planAt = Date.now();
        st.skipped = (res && Array.isArray(res.skipped)) ? res.skipped : [];
        if (res && res.gates) st.gates = res.gates;
        applyAccountPayload(res);
        renderPlan();
        renderSkipped();
        renderGates();
        opHintHost.textContent = '扫描完成 · ' + F.clock(st.planAt) + ' · 生成 ' +
          ((res && res.orders) || []).length + ' 笔委托 · 拦截 ' + st.skipped.length + ' 个' +
          (res && res.note ? ' · ' + res.note : '');
        toast('已生成计划：' + ((res && res.orders) || []).length + ' 笔委托，拦截 ' + st.skipped.length + ' 个', 'ok');
        await loadOrders(false);       /* 计划可能落库为 pending 委托单 */
      } catch (e) {
        if (st.destroyed) return;
        opHintHost.textContent = '扫描失败：' + e.message;
        toast('扫描失败：' + e.message, 'err');
      }
    }

    async function runExecute() {
      const token = String(tokenInp.value || '').trim();
      const real = st.config ? String(st.config.confirmToken || '') : '';
      /* 口令为空 / 与配置不一致时**不发请求**（服务端同样会校验，这里只是提前拦一下） */
      if (!token) {
        toast('口令为空：请在「执行计划」右侧填写 confirmToken（配置里可复制），本次未发起请求', 'warn');
        opHintHost.textContent = '口令为空，未发起请求';
        return;
      }
      if (real && token !== real) {
        toast('口令与配置中的 confirmToken 不一致，本次未发起请求', 'err');
        opHintHost.textContent = '口令不匹配，未发起请求';
        return;
      }
      const ids = planOrderIds();
      const mode = st.config ? String(st.config.mode || '') : '';
      const ok = window.confirm('确认执行计划？\n' +
        '· 模式：' + (MODE_LABEL[mode] || text(mode)) + '\n' +
        '· 范围：' + (ids.length ? ids.length + ' 笔指定委托单' : '服务端默认范围（未指定 id）') + '\n' +
        '· 模拟盘不会产生真实委托，但会写入本地成交与持仓记录。');
      if (!ok) return;
      opHintHost.textContent = '执行中…';
      try {
        const body = { confirm: token };
        if (ids.length) body.ids = ids;
        const res = await apiExecute(body);
        if (st.destroyed) return;
        applyAccountPayload(res);
        if (res && Array.isArray(res.orders)) res.orders.forEach((o) => upsertOrder(asOrder(o)));
        renderOrders();
        opHintHost.textContent = '执行完成 · ' + F.clock(Date.now()) + ' · 成交 ' + text(res && res.filled) +
          ' · 拒绝 ' + text(res && res.rejected);
        toast('执行完成：成交 ' + text(res && res.filled) + ' 笔，拒绝 ' + text(res && res.rejected) + ' 笔', 'ok');
        await Promise.all([loadAccount(true), loadOrders(false)]);
      } catch (e) {
        if (st.destroyed) return;
        opHintHost.textContent = '执行失败：' + e.message;
        toast('执行失败：' + e.message, 'err');
      }
    }

    async function runReset() {
      const ok = window.confirm('重置模拟账户（' + marketText(st.market) + '）？\n' +
        '将清空模拟持仓、现金与盈亏记录，**不会删除历史委托单**。\n确认继续？');
      if (!ok) return;
      opHintHost.textContent = '重置中…';
      try {
        const res = await apiReset(st.market);
        if (st.destroyed) return;
        applyAccountPayload(res);
        st.plan = null;
        st.skipped = [];
        renderPlan();
        renderSkipped();
        opHintHost.textContent = '已重置 · ' + F.clock(Date.now()) + (res && res.note ? ' · ' + res.note : '');
        toast('模拟账户已重置（历史委托单保留）', 'ok');
        await Promise.all([loadAccount(true), loadOrders(false)]);
      } catch (e) {
        if (st.destroyed) return;
        opHintHost.textContent = '重置失败：' + e.message;
        toast('重置失败：' + e.message, 'err');
      }
    }

    async function runExport() {
      const ok = window.confirm('导出当前待执行意图（GET /api/trade/export）并在下方以文本展示，可直接复制给外部系统。\n确认继续？');
      if (!ok) return;
      opHintHost.textContent = '导出中…';
      try {
        const res = await apiExport({ limit: ORDERS_LIMIT });
        if (st.destroyed) return;
        const orders = (res && Array.isArray(res.orders)) ? res.orders : [];
        st.exportCount = res && isNum(res.count) ? res.count : orders.length;
        st.exportAt = (res && res.generatedAt) || Date.now();
        st.exportText = JSON.stringify({
          ok: true,
          generatedAt: (res && res.generatedAt) || null,
          count: st.exportCount,
          orders: orders.map((o) => ({
            id: o.id, code: o.code, side: o.side, intent: o.intent, qty: o.qty,
            limitPrice: o.limitPrice, createdAt: o.createdAt, payload: o.payload,
          })),
        }, null, 2);
        renderExport();
        opHintHost.textContent = '导出完成 · ' + timeText(st.exportAt) + ' · ' + st.exportCount + ' 条待执行意图';
        toast('已导出 ' + st.exportCount + ' 条待执行意图', 'ok');
      } catch (e) {
        if (st.destroyed) return;
        opHintHost.textContent = '导出失败：' + e.message;
        toast('导出失败：' + e.message, 'err');
      }
    }

    function renderOps() {
      clear(opHost);
      opHost.appendChild(h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' } }, [
        h('button', {
          class: 'btn primary sm', text: '立即扫描（只出计划）', dataset: { act: 'plan' },
          title: 'POST /api/trade/plan { execute:false }：只生成计划，不成交',
          on: { click: runPlan },
        }),
        h('button', {
          class: 'btn sm', text: '执行计划', dataset: { act: 'execute' },
          title: 'POST /api/trade/execute：需要 confirmToken 口令；口令为空或不匹配时不会发起请求',
          on: { click: runExecute },
        }),
        h('span', { class: 'legend-inline', style: { gap: '6px', alignItems: 'center' } }, ['口令', tokenInp]),
        h('button', {
          class: 'btn sm', text: '重置模拟账户', dataset: { act: 'reset' },
          title: 'POST /api/trade/reset：清空模拟持仓与盈亏，不删除历史委托单',
          on: { click: runReset },
        }),
        h('button', {
          class: 'btn sm', text: '导出待执行意图', dataset: { act: 'export' },
          title: 'GET /api/trade/export：给外部桥接系统消费',
          on: { click: runExport },
        }),
      ]));
      opHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
        '· 「执行计划」使用上方口令（默认填配置里的 confirmToken，可改）；dryrun 模式下即使执行也只出计划。',
        '· 「重置」不会删除历史委托单；「导出」是只读 GET，不会改变任何状态。',
      ]));
    }

    /* ------------------------------------------------------- 筛选 */

    function renderFilters() {
      clear(filterHost);
      const statusSel = h('select', { class: 'inp', dataset: { filter: 'status' } });
      statusSel.appendChild(h('option', { value: '', text: '全部状态' }));
      Object.keys(STATUS_LABEL).forEach((k) => statusSel.appendChild(h('option', { value: k, text: STATUS_LABEL[k] })));
      statusSel.value = st.filters.status;
      statusSel.addEventListener('change', () => applyFilter('status', statusSel.value));

      const marketSel = h('select', { class: 'inp', dataset: { filter: 'market' } });
      [['', '全部市场'], ['cn', 'A股'], ['us', '美股']].forEach(([v, l]) => {
        marketSel.appendChild(h('option', { value: v, text: l }));
      });
      marketSel.value = st.filters.market;
      marketSel.addEventListener('change', () => applyFilter('market', marketSel.value));

      const codeInp = h('input', {
        class: 'inp', value: st.filters.code, placeholder: '代码（前缀匹配）',
        dataset: { filter: 'code' },
      });
      const submitCode = () => applyFilter('code', String(codeInp.value || '').trim().toUpperCase());
      codeInp.addEventListener('change', submitCode);
      codeInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitCode(); });

      filterHost.appendChild(h('div', { class: 'filter-grid', style: { marginBottom: '8px', maxWidth: '760px' } }, [
        h('div', { class: 'field' }, [h('label', { text: '状态' }), statusSel]),
        h('div', { class: 'field' }, [h('label', { text: '市场' }), marketSel]),
        h('div', { class: 'field' }, [h('label', { text: '代码' }), codeInp]),
      ]));
    }

    function applyFilter(key, value) {
      st.filters[key] = value;
      loadOrders(false, true);        /* 服务端筛选重新取数（本地同时按同一口径过滤） */
    }

    /* ------------------------------------------------------- 数据加载 */

    /* 把任意响应体里的 account / gates / counts 落库并重绘（plan / execute / close / reset 共用） */
    function applyAccountPayload(res) {
      if (!res || typeof res !== 'object') return false;
      const hasAcc = !!(res.account && typeof res.account === 'object');
      if (hasAcc) st.account = res.account;
      if (res.gates && typeof res.gates === 'object') st.gates = res.gates;
      if (res.counts && typeof res.counts === 'object') st.counts = res.counts;
      if (res.accountId) st.accountId = res.accountId;
      if (hasAcc) { renderMetrics(); renderPositions(); }
      if (res.gates || res.counts) renderGates();
      return hasAcc;
    }

    async function loadConfig(silent) {
      try {
        const res = await apiConfig();
        if (st.destroyed) return;
        st.config = (res && res.config) || null;
        st.configNote = (res && res.note) || '';
        if (res && res.gates) st.gates = res.gates;
        if (res && res.accountId) st.accountId = res.accountId;
        if (!st.config) {
          renderConfig(true);
          paintTradeState();
          if (!silent) toast('配置接口未返回 config：GET /api/trade/config', 'warn');
          return;
        }
        /* 口令输入框默认填配置里的 confirmToken，但用户改过之后就不再覆盖 */
        if (!st.tokenEdited && !String(tokenInp.value || '') && st.config.confirmToken) {
          tokenInp.value = String(st.config.confirmToken);
        }
        renderConfig();          /* 正在输入时自动挂起，避免把用户的编辑冲掉 */
        renderGates();
        paintTradeState();
        if (!silent) cfgHintHost.textContent = '配置已刷新 · ' + F.clock(Date.now());
      } catch (e) {
        if (st.destroyed) return;
        st.config = null;
        clear(cfgHost);
        cfgHost.appendChild(ui.empty('配置获取失败：' + e.message + '（接口 /api/trade/config）'));
        paintTradeState();
        if (!silent) toast('配置获取失败：' + e.message, 'err');
      }
    }

    async function loadAccount(silent) {
      try {
        const res = await apiAccount(st.market);
        if (st.destroyed) return;
        if (!res || !res.account) {
          st.account = null;
          renderMetrics();
          renderPositions();
          if (!silent) toast('账户接口未返回 account：GET /api/trade/account', 'warn');
          return;
        }
        st.account = res.account;
        if (res.gates) st.gates = res.gates;
        if (res.counts) st.counts = res.counts;
        if (res.accountId || res.account.accountId) st.accountId = res.accountId || res.account.accountId;
        renderMetrics();
        renderPositions();
        renderGates();
      } catch (e) {
        if (st.destroyed) return;
        st.account = null;
        renderMetrics();
        renderPositions();
        if (!silent) toast('账户获取失败：' + e.message, 'err');
      }
    }

    /** 委托单：append=true 追加下一页；silent=true 时失败不弹 toast（轮询用） */
    async function loadOrders(append, silent) {
      if (st.destroyed) return;
      st.ordersLoading = true;
      const keep = Math.min(ORDERS_MAX, Math.max(ORDERS_LIMIT, st.orders.length));
      const params = {
        status: st.filters.status,
        market: st.filters.market,
        code: String(st.filters.code || '').trim().toUpperCase(),
        limit: append ? ORDERS_LIMIT : keep,
        offset: append ? st.orders.length : 0,
      };
      try {
        const res = await apiOrders(params);
        if (st.destroyed) return;
        const rows = (res && Array.isArray(res.rows)) ? res.rows : [];
        st.orders = append ? st.orders.concat(rows) : rows;
        /* 推送可能已把同一 id 插入本地：按 id 去重，避免出现两行 */
        const seen = {};
        st.orders = st.orders.filter((o) => {
          if (!o || !o.id) return true;
          if (seen[o.id]) return false;
          seen[o.id] = 1;
          return true;
        });
        st.ordersTotal = (res && isNum(res.total)) ? res.total : st.orders.length;
        st.ordersLoading = false;
        renderOrders();
      } catch (e) {
        if (st.destroyed) return;
        st.ordersLoading = false;
        if (!silent) {
          clear(orderHost);
          orderHost.appendChild(ui.empty('委托单获取失败：' + e.message + '（接口 /api/trade/orders）'));
          toast('委托单获取失败：' + e.message, 'err');
        }
      }
    }

    function refreshAll(silent) {
      return Promise.all([
        loadConfig(true),
        loadAccount(silent),
        loadOrders(false, silent),
      ]);
    }

    /* ----------------------------------------------------- 实时推送 */

    function closeStream() {
      const hd = st.push.handle;
      st.push.handle = null;
      if (hd && typeof hd.close === 'function') {
        try { hd.close(); } catch (e) { /* 关闭失败也要继续 */ }
      }
    }

    function onStreamStatus(s) {
      if (st.destroyed) return;
      paintChip(s);
    }

    function onStreamError(info) {
      if (st.destroyed) return;
      const msg = (info && info.message) || '未知错误';
      st.push.lastError = msg;
      /* 降级为轮询是预期行为，最多提示一次，避免刷屏 */
      if (!st.push.errToasted) {
        st.push.errToasted = true;
        toast('实时推送不可用，已降级为 ' + (POLL_MS / 1000) + ' 秒轮询兜底：' + msg, 'warn');
      }
      paintNote();
    }

    /* order：追加 / 更新委托表对应行 */
    function onStreamOrder(payload) {
      if (st.destroyed) return;
      const o = asOrder(payload);
      if (o) { upsertOrder(o); renderOrders(); }
      if (payload && (payload.note || payload.message)) {
        st.note = String(payload.note || payload.message);
        paintNote();
      }
    }

    /* fill：追加一条成交回报流水，并同步账户与对应委托单行 */
    function onStreamFill(payload) {
      if (st.destroyed) return;
      const o = asOrder(payload) || {};
      const at = (payload && (payload.ts || payload.at || payload.time)) || Date.now();
      st.fills.unshift({
        id: o.id, code: o.code, name: o.name, market: o.market, side: o.side,
        intent: o.intent, qty: o.qty, fillPrice: o.fillPrice, amount: o.amount,
        fee: o.fee, slippage: o.slippage, at: at,
      });
      if (st.fills.length > FILL_MAX) st.fills.length = FILL_MAX;
      renderFills();
      if (o.id) { upsertOrder(o); renderOrders(); }
      /* 推送没带账户体：补拉一次，保证总览与持仓跟得上成交 */
      if (!applyAccountPayload(payload)) loadAccount(true);
    }

    function onStreamAccount(payload) {
      if (st.destroyed) return;
      if (!applyAccountPayload(payload)) loadAccount(true);
    }

    function onStreamConfig(payload) {
      if (st.destroyed) return;
      const cfg = payload && payload.config ? payload.config : null;
      if (!cfg) { loadConfig(true); return; }
      st.config = cfg;
      if (payload.gates) st.gates = payload.gates;
      if (!st.tokenEdited && !String(tokenInp.value || '') && cfg.confirmToken) {
        tokenInp.value = String(cfg.confirmToken);
      }
      renderConfig();          /* 正在输入时内部会挂起，失焦后补绘一次 */
      renderGates();
      paintTradeState();
      cfgHintHost.textContent = '配置已由服务端推送更新 · ' + F.clock(Date.now());
    }

    function onStreamNote(payload) {
      if (st.destroyed) return;
      const msg = payload && (payload.message || payload.note || payload.text);
      if (!msg) return;
      st.note = String(msg);
      paintNote();
    }

    function startStream() {
      const s = window.AD.stream;
      if (st.destroyed) return;
      if (!s || typeof s.trade !== 'function') {
        paintChip('unsupported', 'AD.stream.trade 未接入：仅使用 ' + (POLL_MS / 1000) + ' 秒轮询');
        return;
      }
      closeStream();
      st.push.errToasted = false;
      st.push.lastError = '';
      paintChip('connecting');
      try {
        st.push.handle = s.trade({
          market: st.market,
          fallbackMs: POLL_MS,
          onReady: () => { /* ready 只表示订阅被受理，无需额外处理 */ },
          onOrder: onStreamOrder,
          onFill: onStreamFill,
          onAccount: onStreamAccount,
          onConfig: onStreamConfig,
          onNote: onStreamNote,
          onStatus: onStreamStatus,
          onError: onStreamError,
          fallbackTick: pollTick,
        });
      } catch (e) {
        st.push.handle = null;
        paintChip('unsupported', 'AD.stream.trade 订阅失败：' + e.message);
        return;
      }
      /* 构造订阅时 onStatus 可能已同步给出 unsupported / fallback，别用 connecting 盖掉 */
      paintChip(st.push.state || 'connecting');
    }

    /* 15 秒兜底轮询：既服务「自动刷新」，也是推送降级后的取数路径 */
    function pollTick() {
      if (st.destroyed || !root.isConnected) return Promise.resolve();
      if (!st.auto) return Promise.resolve();
      if (pollBusy) return Promise.resolve();
      pollBusy = true;
      return Promise.all([
        loadAccount(true),
        loadOrders(false, true),      /* 只刷新当前窗口，避免冲掉「加载更多」的结果 */
      ]).then(() => { pollBusy = false; }, () => { pollBusy = false; });
    }

    /* --------------------------------------------------------- 骨架 */

    const autoSeg = ui.seg(
      [{ value: 'on', label: '自动刷新 15 秒' }, { value: 'off', label: '手动' }],
      'on',
      (v) => {
        st.auto = v === 'on';
        toast(st.auto
          ? '已开启自动刷新（15 秒，同时作为推送降级后的兜底）'
          : '已切换为手动刷新', 'info');
        if (st.auto) pollTick();
      }
    );

    const refreshBtn = h('button', {
      class: 'btn sm', text: '刷新',
      on: { click: () => refreshAll(false) },
    });

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('模拟交易',
        '模拟盘 / 计划盘：只用公开行情做模拟撮合与记账，<b>不接任何真实券商通道、不会产生真实委托</b>；自动交易默认关闭，需显式开启',
        [autoSeg, refreshBtn]),
      ui.section('这是什么 / 怎么用', '模拟盘说明 · 自动交易接口 · 连接状态', [], noticeHost),
      ui.section('自动交易配置', '改动即保存（乐观更新，失败自动回滚）；总开关默认关闭', [],
        h('div', {}, [cfgHost, h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [cfgHintHost])])),
      ui.section('账户总览', '服务端在首次读取 /api/trade/account 时创建模拟账户（本页不臆造账户）', [], metricHost),
      ui.section('风控快照', '当前生效上限：优先取服务端 gates，缺失项回落配置值并如实标注来源', [], gateHost),
      ui.section('持仓', '「平仓」按最新报价模拟撮合（POST /api/trade/close）；无报价时最新价显示「—」', [], posHost),
      ui.section('操作', '扫描只出计划；执行需 confirmToken 口令；重置不会删除历史委托单', [],
        h('div', {}, [opHost, opHintHost])),
      ui.section('本次计划 / 被风控拦截', '「立即扫描」的结果：上方为本次委托计划，下方为被风控拦截的原因', [],
        h('div', {}, [planHost, skipHost])),
      ui.section('成交回报', '推送 onFill 的落地流水（最多保留最近 ' + FILL_MAX + ' 条）', [], fillHost),
      ui.section('委托单', '筛选 / 分页（每次 ' + ORDERS_LIMIT + ' 条）；pending 可撤单，submitted 可回执', [],
        h('div', {}, [filterHost, orderHost])),
      ui.section('导出待执行意图', 'GET /api/trade/export 的返回，可直接复制给外部桥接系统', [], exportHost),
    ]));

    renderNotice();
    renderConfig(true);
    renderMetrics();
    renderGates();
    renderPositions();
    renderFilters();
    renderOrders();
    renderPlan();
    renderSkipped();
    renderFills();
    renderExport();
    renderOps();
    paintChip('connecting');
    paintNote();

    (async () => {
      /* 启动顺序：先配置（口令默认值）、再账户与委托单，最后订阅推送 */
      await loadConfig(true);
      if (st.destroyed) return;
      await Promise.all([loadAccount(true), loadOrders(false, true)]);
      if (st.destroyed) return;
      startStream();
    })();

    timer = setInterval(() => {
      if (st.destroyed || !root.isConnected) return;
      pollTick();
    }, POLL_MS);

    return {
      refresh: () => refreshAll(false),
      destroy() {
        st.destroyed = true;          /* 先置位：之后所有回调一律直接返回 */
        closeStream();
        if (timer) { clearInterval(timer); timer = null; }
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.trade = { mount };
})();
