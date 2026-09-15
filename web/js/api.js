/* ==========================================================================
   AlphaDesk · 数据接口层
   统一走本地行情服务（/api/*），服务端已完成多源降级与缓存
   ========================================================================== */
(function () {
  'use strict';

  const inflight = new Map();

  function buildUrl(path, params) {
    const usp = new URLSearchParams();
    const p = params || {};
    Object.keys(p).forEach((k) => {
      const v = p[k];
      if (v === null || v === undefined || v === '') return;
      usp.set(k, v);
    });
    const qs = usp.toString();
    return '/api/' + path + (qs ? '?' + qs : '');
  }

  function get(path, params, opts) {
    const url = buildUrl(path, params);
    const o = opts || {};
    if (!o.noDedupe && inflight.has(url)) return inflight.get(url);
    const pr = fetch(url, { headers: { Accept: 'application/json' } })
      .then((r) => r.json().then((j) => ({ ok: r.ok, status: r.status, json: j })))
      .then(({ ok, status, json }) => {
        if (!ok || json.error) {
          /* features/* 等接口在失败（ok=false）时仍会返回「空实现」形状与中文原因：
             message 优先取服务端说明，原始响应体挂在 err.json 上供视图做降级展示 */
          const msg = (json && (json.message || (typeof json.error === 'string' ? json.error : '')))
            || ('请求失败 HTTP ' + status);
          const err = new Error(msg);
          err.url = url;
          err.json = json;
          throw err;
        }
        return json;
      })
      .finally(() => inflight.delete(url));
    if (!o.noDedupe) inflight.set(url, pr);
    return pr;
  }

  function post(path, body) {
    const url = '/api/' + path;
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body || {}),
    })
      .then((r) => r.json().then((j) => ({ ok: r.ok, status: r.status, json: j })))
      .then(({ ok, status, json }) => {
        if (!ok || json.error) {
          const err = new Error((json && json.message) || ('请求失败 HTTP ' + status));
          err.url = url;
          throw err;
        }
        return json;
      });
  }

  const api = {
    get, post,
    health: () => get('health'),

    indices: (market) => get('indices', { market }),
    overview: (market, size) => get('overview', { market, size: size || 20 }),
    movers: (market) => get('movers', { market }),
    sectors: (kind) => get('sectors', { kind: kind || 'industry' }),
    news: (limit) => get('news', { limit: limit || 30 }),
    search: (q) => get('search', { q }, { noDedupe: true }),

    quote: (market, codes) => get('quote', { market, codes: (codes || []).join(',') }, { noDedupe: true }),
    stock: (market, code) => get('stock', { market, code }, { noDedupe: true }),
    orderbook: (market, code) => get('orderbook', { market, code }, { noDedupe: true }),

    kline: (market, code, period, fq, limit) => get('kline', {
      market, code, period: period || 'day', fq: fq === undefined ? 1 : fq, limit: limit || 320,
    }, { noDedupe: true }),

    trends: (market, code, days) => get('trends', { market, code, days: days || 1 }, { noDedupe: true }),
    fundflow: (market, code) => get('fundflow', { market, code }),

    screener(market, filters, sort, order, page, size) {
      const p = Object.assign({ market, sort, order, page, size }, filters || {});
      return get('list', p, { noDedupe: true });
    },

    /* 策略持续跟踪 */
    strategyMeta: () => get('strategy/meta', null, { noDedupe: true }),
    strategyOverview: () => get('strategy/overview', null, { noDedupe: true }),
    strategyRun: (id) => get('strategy/run', { id }, { noDedupe: true }),
    strategyCreate: (body) => post('strategy/create', body),
    strategyAction: (id, action) => post('strategy/action', { id, action }),
    strategyUpdate: (id, patch, reset) => post('strategy/update', { id, patch, reset: !!reset }),
    backtest: (body) => post('backtest', body),
    searchParams: (body) => post('search/params', body),
    featuresIndex: () => get('features'),
    feature: (kind, params) => get('features/' + kind, params),
    logs: (params) => get('logs', params),
    sysinfo: () => get('sysinfo'),
    notifyGet: () => get('notify'),
    notifySave: (body) => post('notify', body),
    notifyTest: (body) => post('notify/test', body || {}),
  };

  window.AD = window.AD || {};
  window.AD.api = api;
})();
