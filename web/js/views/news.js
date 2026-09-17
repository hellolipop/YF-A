/* ==========================================================================
   视图 · 资讯快讯（自动提取个股代码，可直接跳转个股）
   ========================================================================== */
(function () {
  'use strict';

  const { h, reconcile } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  function extractCodes(text) {
    const set = new Set();
    const re = /\((\d{6})\)|（(\d{6})）|(\d{6})/g;
    let m;
    while ((m = re.exec(text || '')) !== null) {
      const code = m[1] || m[2] || m[4];
      if (code) set.add(code);
    }
    return Array.from(set).slice(0, 4);
  }

  function mount(root, ctx) {
    let timer = null;
    const state = { rows: [], kw: '', auto: true };
    const EMPTY = { __adEmpty: true };                    /* 空态哨兵：交给 reconcile 统一管理 */
    const listHost = h('div');
    const listWrap = h('div', { class: 'news-list' });    /* 列表容器只创建一次 */
    listWrap.__adKeep = true;                             /* 缓存实例标记：不与普通块做 morph 合并 */
    const statHost = h('span', { class: 'hint', text: '加载中…' });

    /* 原位挂载：同一状态内只做增量改写，实现见 ui.js（listWrap 带 __adKeep 不会被合并） */
    const mountInto = ui.mountInto;

    /* 单条快讯：结构与原来一致，节点由 reconcile 按 url / id 复用 */
    function newsItem(r) {
      const body = h('div', { class: 'body' });
      body.appendChild(h('div', {
        class: 'txt',
        html: ('<strong>' + (r.title || '') + '</strong>' +
          (r.title ? '<br>' : '') +
          (r.summary || '').replace(/[<>]/g, '')).replace(/\n/g, '<br>'),
      }));
      const codes = extractCodes(r.summary + ' ' + r.title);
      if (codes.length) {
        const chips = h('div', { style: { marginTop: '7px', display: 'flex', gap: '6px', flexWrap: 'wrap' } });
        codes.forEach((c) => {
          const chip = h('button', { class: 'chip accent', text: c, style: { cursor: 'pointer' } });
          /* 节点会被原位复用：点击时以按钮上的文本（即代码）为准，避免读到首次渲染的旧闭包 */
          chip.addEventListener('click', () => { const code = chip.textContent || c; ctx.openSymbol('cn', code, code); });
          chips.appendChild(chip);
        });
        body.appendChild(chips);
      }
      return h('div', { class: 'news-item' }, [
        h('div', { class: 'time', text: F.timeOf(r.time) }),
        body,
      ]);
    }

    function render() {
      const rows = state.rows.filter((r) => {
        if (!state.kw) return true;
        const k = state.kw.toUpperCase();
        return (r.title + r.summary).toUpperCase().indexOf(k) >= 0;
      });
      mountInto(listHost, listWrap);
      /* 按 url / id 做 key 复用节点：顺序不变时列表 DOM 完全不动，滚动位置也不会被重置 */
      reconcile(listWrap, rows.length ? rows : [EMPTY], {
        key: (r, i) => (r === EMPTY ? '__ad-empty' : String(r.url || r.id || r.code || ('#' + i))),
        render: (r) => (r === EMPTY
          ? ui.empty(state.kw ? '没有匹配「' + state.kw + '」的快讯' : '暂无快讯')
          : newsItem(r)),
      });
    }

    async function refresh() {
      try {
        const res = await api.news(40);
        state.rows = res.rows || [];
        statHost.textContent = '共 ' + state.rows.length + ' 条 · 更新 ' + F.clock(res.updated || Date.now()) +
          ' · 来源 ' + (res.source || '东方财富');
        render();
      } catch (e) {
        statHost.textContent = '获取失败';
        mountInto(listHost, ui.empty('资讯获取失败：' + e.message));   /* 失败提示同样原位挂载 */
      }
    }

    const kwInput = h('input', {
      class: 'inp', placeholder: '过滤关键词，如 业绩 / 减持 / 半导体', style: { width: '240px' },
      on: {
        input: (e) => { state.kw = e.target.value.trim(); render(); },
      },
    });

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('资讯快讯', '东方财富 7×24 快讯流，自动识别正文中的个股代码并可跳转', [
        statHost, kwInput,
        h('button', { class: 'btn sm', text: '刷新', on: { click: refresh } }),
      ]),
      listHost,
    ]));

    refresh();
    timer = setInterval(() => { if (state.auto) refresh(); }, 60000);
    return { refresh, destroy() { if (timer) clearInterval(timer); } };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.news = { mount };
})();
