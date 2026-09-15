/* ==========================================================================
   视图 · 资讯快讯（自动提取个股代码，可直接跳转个股）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear } = window.AD.dom;
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
    const listHost = h('div');
    const statHost = h('span', { class: 'hint', text: '加载中…' });

    function render() {
      const rows = state.rows.filter((r) => {
        if (!state.kw) return true;
        const k = state.kw.toUpperCase();
        return (r.title + r.summary).toUpperCase().indexOf(k) >= 0;
      });
      clear(listHost);
      const list = h('div', { class: 'news-list' });
      if (!rows.length) {
        list.appendChild(ui.empty(state.kw ? '没有匹配「' + state.kw + '」的快讯' : '暂无快讯'));
      }
      rows.forEach((r) => {
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
            chips.appendChild(h('button', {
              class: 'chip accent', text: c,
              style: { cursor: 'pointer' },
              on: { click: () => ctx.openSymbol('cn', c, c) },
            }));
          });
          body.appendChild(chips);
        }
        list.appendChild(h('div', { class: 'news-item' }, [
          h('div', { class: 'time', text: F.timeOf(r.time) }),
          body,
        ]));
      });
      listHost.appendChild(list);
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
        clear(listHost);
        listHost.appendChild(ui.empty('资讯获取失败：' + e.message));
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
