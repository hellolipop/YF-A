/* ==========================================================================
   AlphaDesk · 实时推送层（SSE / EventSource）

   服务端契约（已由 core/stream.py 实现，HTTP 路由由服务端负责，前端不定义）：
     GET /api/stream/quotes?market=cn&symbols=600519,000001&interval=3
        ready  {channel, market, symbols, interval, ts, note}
        quotes {ts, market, interval, rows:[{code,name,market,price,changePct,change,
                volume,amount,updated,open,high,low,prevClose,source}],
                failed:[codes], elapsedMs, source, degraded}
        error  {message, ts}
     GET /api/stream/advisor?market=cn&symbols=...&horizon=20&capital=100000
                &kellyFraction=0.5&maxWeight=0.25&interval=30
        ready / snapshot {ts, elapsedMs, checked, market, analyzed, result:<同 /api/advisor/recommend>}
        change {ts, elapsedMs, checked, market, analyzed, changed,
                changes:[{code,name,market,action,actionText,prevAction,score,confidence,
                          price,changePct,kelly:{weight,amount,shares},
                          plan:{entry,stop,target1,target2},
                          forecast:{expectedReturn,upProb},reasons:[中文原因]}], portfolio}
        pulse  {ts, changed:0, checked, elapsedMs}
        error
     GET /api/stream/trade?market=cn
        ready / order / fill / account / config / note / error
     GET /api/stream/status  → JSON 中枢状态

   设计要点：
     1. 「推送挂了要能活下去」——连不上 / 连到一半断掉都会自动降级为轮询，
        任何异常都被吞掉，绝不向调用方抛错，也绝不打断视图。
     2. 每个视图持有自己的 EventSource 实例，不做全局单例复用：
        否则一个视图 close() 会把别人的连接一起关掉。
     3. 断线重连交给浏览器自带逻辑（它会带上 Last-Event-ID，服务端 retry: 3000
        也是给浏览器的），本层不写重连定时器；只在「浏览器已放弃
        （readyState===2）」这种情况借轮询节奏重建一次实例，否则永远无法恢复。
     4. close() 幂等；关闭后任何回调（含 onStatus）都不再触发 —— 视图 destroy()
        后不能再碰已经卸载的 DOM。
   ========================================================================== */
(function () {
  'use strict';

  const AD = (window.AD = window.AD || {});
  const F = AD.fmt;

  const DEFAULT_FALLBACK_MS = 15000;   /* 降级为轮询后的默认间隔 */
  const MAX_ERRORS = 3;                /* 连续错误次数达到该值即降级 */
  const ES_CLOSED = 2;                 /* EventSource.CLOSED：浏览器不会再自动重连 */

  /* 连接状态 -> chip 外观（只用项目已有 chip 配色：''/up/warn） */
  const CHIP = {
    open: { text: '实时已连接', cls: 'up' },
    connecting: { text: '连接中', cls: '' },
    fallback: { text: '已降级为轮询', cls: 'warn' },
    unsupported: { text: '浏览器不支持推送', cls: 'warn' },
    closed: { text: '推送已关闭', cls: '' },
  };
  const CHIP_TITLE = '数据来自 SSE 长连接推送；无实质性变化时服务端只发 pulse 心跳，不重复推送整表。';

  function supported() {
    return typeof window.EventSource === 'function';
  }

  /* 标的列表：数组或逗号串都接受 */
  function symbolsText(v) {
    if (Array.isArray(v)) return v.filter(Boolean).join(',');
    return v === null || v === undefined ? '' : String(v);
  }

  function query(params) {
    const usp = new URLSearchParams();
    const p = params || {};
    Object.keys(p).forEach((k) => {
      const v = p[k];
      if (v === null || v === undefined || v === '') return;
      usp.set(k, Array.isArray(v) ? v.join(',') : String(v));
    });
    const s = usp.toString();
    return s ? '?' + s : '';
  }

  /* 服务端 ts 可能是毫秒时间戳 / 秒时间戳 / ISO 串，统一成 HH:MM:SS */
  function tsText(ts) {
    if (ts === null || ts === undefined || ts === '') return '—';
    if (typeof ts === 'number' && isFinite(ts)) {
      const ms = ts < 1e12 ? ts * 1000 : ts;          /* 秒级时间戳换算为毫秒 */
      return F && F.clock ? F.clock(ms) : new Date(ms).toTimeString().slice(0, 8);
    }
    const s = String(ts);
    if (F && F.hhmmss) return F.hhmmss(s);
    return s.length >= 19 ? s.slice(11, 19) : s.slice(-8);
  }

  /* 状态 chip 工厂：视图只需要 replace 一个节点，避免各自复制一份文案表 */
  function chip(state, title) {
    const spec = CHIP[state] || { text: '推送状态未知', cls: '' };
    const el = document.createElement('span');
    el.className = 'chip' + (spec.cls ? ' ' + spec.cls : '');
    el.textContent = spec.text;
    el.title = title || CHIP_TITLE;
    el.setAttribute('data-stream-state', String(state || ''));
    return el;
  }

  /* ------------------------------------------------------------------ 核心 */

  /**
   * 建立一路订阅
   * cfg = { channel, params, handlers:{事件名: fn}, onStatus, onError,
   *         fallbackMs, fallbackTick }
   * 返回 { close(), info() }
   */
  function connect(cfg) {
    const c = cfg || {};
    const channel = c.channel || 'quotes';
    const handlers = c.handlers || {};
    const url = '/api/stream/' + channel + query(c.params);

    const info = {
      channel: channel,
      url: url,
      state: 'connecting',
      mode: 'sse',            /* sse | poll | none（不支持推送且无轮询函数） */
      attempts: 0,            /* 连续错误次数（open 后清零） */
      lastError: '',
      since: Date.now(),
      events: 0,              /* 已分发的业务事件数 */
      lastEventAt: null,
      pollMs: 0,
      reopenAttempts: 0,      /* 浏览器已放弃后借轮询重建的次数 */
    };

    let es = null;
    let pollTimer = null;
    let closed = false;
    let lastState = null;

    function snap() {
      return {
        channel: info.channel, url: info.url, state: info.state, mode: info.mode,
        attempts: info.attempts, lastError: info.lastError, since: info.since,
        events: info.events, lastEventAt: info.lastEventAt, pollMs: info.pollMs,
        reopenAttempts: info.reopenAttempts,
      };
    }

    /* 所有回调统一出口：closed 后一律不触发；回调自身抛错也不能打断连接 */
    function fire(fn, payload, name) {
      if (closed || typeof fn !== 'function') return;
      try {
        fn(payload);
      } catch (e) {
        console.error('[stream:' + channel + '] ' + (name || 'callback') + ' 抛出异常', e);
      }
    }

    function setState(state) {
      if (closed || state === lastState) return;
      lastState = state;
      info.state = state;
      info.since = Date.now();
      fire(c.onStatus, state, snap(), 'onStatus');
    }

    function reportError(message, raw) {
      info.lastError = String(message || '未知错误');
      fire(c.onError, {
        channel: channel, message: info.lastError, raw: raw,
        attempts: info.attempts, url: url,
      }, 'onError');
    }

    /* ---- 轮询降级 ---- */

    function pollTick() {
      if (closed || typeof c.fallbackTick !== 'function') return;
      /* 浏览器已把连接判死（readyState=2）时不会再重连：借这一拍重建一次，
         成功后 handleOpen() 会停掉轮询回到 open；失败不影响轮询继续跑 */
      if (es && es.readyState === ES_CLOSED) {
        info.reopenAttempts += 1;
        open();
      }
      let r;
      try {
        r = c.fallbackTick({
          channel: channel, mode: 'poll', interval: info.pollMs, attempts: info.attempts,
        });
      } catch (e) {
        reportError('轮询回调异常：' + (e && e.message ? e.message : e), null);
        return;
      }
      if (r && typeof r.then === 'function') {
        r.then(null, (e) => { info.lastError = '轮询失败：' + (e && e.message ? e.message : e); });
      }
    }

    function startPolling() {
      if (closed || pollTimer || typeof c.fallbackTick !== 'function') return;
      const ms = Number(c.fallbackMs) > 0 ? Number(c.fallbackMs) : DEFAULT_FALLBACK_MS;
      info.mode = 'poll';
      info.pollMs = ms;
      pollTick();                                        /* 先补一次，别让用户干等一个周期 */
      pollTimer = setInterval(pollTick, ms);
    }

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      info.pollMs = 0;
      if (info.mode === 'poll') info.mode = 'sse';
    }

    function enterFallback(reason) {
      if (closed) return;
      if (reason && !info.lastError) info.lastError = String(reason);
      info.mode = typeof c.fallbackTick === 'function' ? 'poll' : 'none';
      setState('fallback');
      startPolling();
    }

    /* ---- 事件分发 ---- */

    function dispatch(name, ev) {
      const raw = ev && typeof ev.data === 'string' ? ev.data : '';
      let payload = {};
      if (raw) {
        try {
          payload = JSON.parse(raw);
        } catch (e) {
          /* 解析失败只报错，不中断连接、不丢弃后续事件 */
          reportError('推送数据不是合法 JSON（事件 ' + name + '）：' + (e && e.message ? e.message : e), raw);
          return;
        }
      }
      info.events += 1;
      info.lastEventAt = Date.now();
      const fn = handlers[name];
      if (typeof fn === 'function') fire(fn, payload, name);
      else if (name === 'error') reportError(payload.message || '服务端推送错误', raw);
    }

    function transportError(ev) {
      info.attempts += 1;
      info.lastError = '连接异常（第 ' + info.attempts + ' 次）';
      fire(c.onError, {
        channel: channel, message: info.lastError, attempts: info.attempts, url: url,
        event: ev,
      }, 'onError');
      const dead = !es || es.readyState === ES_CLOSED;
      if (dead || info.attempts >= MAX_ERRORS) {
        enterFallback(dead ? '连接已被浏览器关闭' : ('连续 ' + info.attempts + ' 次连接异常'));
      } else {
        /* 还没到阈值：浏览器自己在重连，界面上保持「连接中」 */
        setState('connecting');
      }
    }

    function handleOpen() {
      info.attempts = 0;
      info.lastError = '';
      info.mode = 'sse';
      stopPolling();
      setState('open');
    }

    function open() {
      if (closed || !supported()) return;
      let inst;
      try {
        inst = new window.EventSource(url);
      } catch (e) {
        reportError('EventSource 构造失败：' + (e && e.message ? e.message : e), null);
        enterFallback('EventSource 构造失败');
        return;
      }
      es = inst;
      inst.onopen = () => { if (!closed && es === inst) handleOpen(); };
      /* 注意：onerror 同时承载「传输错误」与「服务端 event: error 消息」，
         后者带 data（MessageEvent），前者没有，据此区分 */
      inst.onerror = (ev) => {
        if (closed || es !== inst) return;
        if (ev && typeof ev.data === 'string' && ev.data) { dispatch('error', ev); return; }
        transportError(ev);
      };
      Object.keys(handlers).forEach((name) => {
        if (name === 'error' || name === 'open' || name === 'message') return;
        inst.addEventListener(name, (ev) => {
          if (closed || es !== inst) return;
          dispatch(name, ev);
        });
      });
      /* 服务端若漏写 event: 行，默认 message 也能兜底分发（默认交 onMessage） */
      inst.onmessage = (ev) => {
        if (closed || es !== inst) return;
        const fn = c.onMessage || handlers.message;
        if (typeof fn !== 'function') return;
        const raw = ev && typeof ev.data === 'string' ? ev.data : '';
        let payload = {};
        try { payload = raw ? JSON.parse(raw) : {}; } catch (e) {
          reportError('推送数据不是合法 JSON（默认 message）：' + (e && e.message ? e.message : e), raw);
          return;
        }
        info.events += 1;
        info.lastEventAt = Date.now();
        fire(fn, payload, 'onMessage');
      };
    }

    function close() {
      if (closed) return;                 /* 幂等 */
      closed = true;
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (es) {
        try {
          es.onopen = null; es.onerror = null; es.onmessage = null;
          es.close();
        } catch (e) { /* 忽略：可能已被浏览器关闭 */ }
        es = null;
      }
      info.state = 'closed';
      info.mode = 'none';
      info.pollMs = 0;
    }

    /* ---- 启动 ---- */
    if (!supported()) {
      /* 无 EventSource：直接按轮询跑，首个状态就是 unsupported，
         不让视图先闪一下「连接中」再变成「浏览器不支持推送」 */
      info.mode = typeof c.fallbackTick === 'function' ? 'poll' : 'none';
      setState('unsupported');
      startPolling();
    } else {
      setState('connecting');
      open();
    }

    return { close: close, info: snap, url: url };
  }

  /* ------------------------------------------------------------- 对外接口 */

  function quotes(opts) {
    const o = opts || {};
    return connect({
      channel: 'quotes',
      params: {
        market: o.market || 'cn',
        symbols: symbolsText(o.symbols),
        interval: o.interval || 3,
      },
      handlers: { ready: o.onReady, quotes: o.onQuotes },
      onMessage: o.onMessage,
      onError: o.onError,
      onStatus: o.onStatus,
      fallbackMs: o.fallbackMs,
      fallbackTick: o.fallbackTick,
    });
  }

  function advisor(opts) {
    const o = opts || {};
    return connect({
      channel: 'advisor',
      params: {
        market: o.market || 'cn',
        symbols: symbolsText(o.symbols),
        horizon: o.horizon,
        capital: o.capital,
        kellyFraction: o.kellyFraction,
        maxWeight: o.maxWeight,
        interval: o.interval || 30,
      },
      handlers: {
        ready: o.onReady,
        snapshot: o.onSnapshot,
        change: o.onChange,
        pulse: o.onPulse,
      },
      onMessage: o.onMessage,
      onError: o.onError,
      onStatus: o.onStatus,
      fallbackMs: o.fallbackMs,
      fallbackTick: o.fallbackTick,
    });
  }

  function trade(opts) {
    const o = opts || {};
    return connect({
      channel: 'trade',
      params: { market: o.market || 'cn' },
      handlers: {
        ready: o.onReady,
        order: o.onOrder,
        fill: o.onFill,
        account: o.onAccount,
        config: o.onConfig,
        note: o.onNote,
      },
      onMessage: o.onMessage,
      onError: o.onError,
      onStatus: o.onStatus,
      fallbackMs: o.fallbackMs,
      fallbackTick: o.fallbackTick,
    });
  }

  /* 中枢状态（普通 JSON 接口，非长连接）。失败时 reject，调用方自行吞掉 */
  function status() {
    try {
      if (AD.api && typeof AD.api.get === 'function') {
        return AD.api.get('stream/status', null, { noDedupe: true });
      }
      return fetch('/api/stream/status', { headers: { Accept: 'application/json' } })
        .then((r) => r.text().then((t) => ({ ok: r.ok, status: r.status, text: t })))
        .then(({ ok, status: code, text }) => {
          let json = null;
          try { json = JSON.parse(text); } catch (e) { /* 下方统一报错 */ }
          if (!json) throw new Error('服务端返回了非 JSON 响应（HTTP ' + code + '）');
          if (!ok || json.error) throw new Error(json.message || ('请求失败 HTTP ' + code));
          return json;
        });
    } catch (e) {
      return Promise.reject(e);
    }
  }

  AD.stream = {
    quotes: quotes,
    advisor: advisor,
    trade: trade,
    status: status,
    supported: supported,
    chip: chip,
    tsText: tsText,
    DEFAULT_FALLBACK_MS: DEFAULT_FALLBACK_MS,
    MAX_ERRORS: MAX_ERRORS,
  };
})();
