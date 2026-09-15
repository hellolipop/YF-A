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
          const err = new Error((json && json.message) || ('请求失败 HTTP ' + status));
          err.url = url;
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
  };

  window.AD = window.AD || {};
  window.AD.api = api;
})();
