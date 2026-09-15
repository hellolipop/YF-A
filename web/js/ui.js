/* ==========================================================================
   AlphaDesk · 通用 UI 组件（页面骨架 / 数据表 / 空状态）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;

  function pageHead(title, sub, actions) {
    return h('div', { class: 'page-head' }, [
      h('div', {}, [
        h('h1', { text: title }),
        sub ? h('div', { class: 'sub', html: sub }) : null,
      ]),
      actions && actions.length ? h('div', { class: 'head-actions' }, actions) : null,
    ]);
  }

  function section(title, hint, actions, body) {
    return h('div', { class: 'section' }, [
      h('div', { class: 'section-head' }, [
        h('h2', { text: title }),
        hint ? h('span', { class: 'hint', html: hint }) : null,
        h('span', { class: 'spacer' }),
        actions ? h('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } }, actions) : null,
      ]),
      body,
    ]);
  }

  function empty(text) { return h('div', { class: 'empty', text: text || '暂无数据' }); }
  function loading(text) { return h('div', { class: 'loading', text: text || '加载中…' }); }

  function seg(items, active, onPick) {
    const wrap = h('div', { class: 'seg' });
    items.forEach((it) => {
      const val = typeof it === 'string' ? it : it.value;
      const label = typeof it === 'string' ? it : it.label;
      wrap.appendChild(h('button', {
        class: val === active ? 'active' : '',
        text: label,
        on: { click: () => onPick(val) },
      }));
    });
    return wrap;
  }

  /**
   * 数据表
   * cols: [{ key, label, cls, noSort, width, render(row), value(row) }]
   */
  function tbl(cfg) {
    const wrap = h('div', { class: 'tbl-wrap' });
    const scroll = h('div', { class: 'tbl-scroll', style: { maxHeight: cfg.maxHeight || 'none' } });
    const table = h('table', { class: 'tbl' + (cfg.compact ? ' compact' : '') });
    let sortKey = cfg.sortKey || null;
    let sortDir = cfg.sortDir || 'desc';

    function renderRows() {
      const thead = h('thead');
      const tr = h('tr');
      cfg.cols.forEach((c) => {
        const isSorted = sortKey === c.key && !c.noSort;
        const th = h('th', {
          class: (c.noSort ? 'no-sort ' : '') + (c.cls || '') + (isSorted ? ' sorted' : ''),
          style: c.width ? { width: c.width } : null,
          title: c.title || '',
          on: c.noSort ? null : { click: () => { if (sortKey === c.key) sortDir = sortDir === 'desc' ? 'asc' : 'desc'; else { sortKey = c.key; sortDir = 'desc'; } renderRows(); } },
        }, [c.label]);
        if (!c.noSort) th.appendChild(h('span', { class: 'arrow', text: isSorted ? (sortDir === 'desc' ? '↓' : '↑') : '↕' }));
        tr.appendChild(th);
      });
      thead.appendChild(tr);

      let rows = (cfg.rows || []).slice();
      if (sortKey) {
        const col = cfg.cols.find((c) => c.key === sortKey);
        if (col && col.value) {
          rows.sort((a, b) => {
            const av = col.value(a), bv = col.value(b);
            if (av === null || av === undefined) return 1;
            if (bv === null || bv === undefined) return -1;
            return sortDir === 'desc' ? bv - av : av - bv;
          });
        }
      }
      const tbody = h('tbody');
      if (!rows.length) {
        tbody.appendChild(h('tr', {}, [h('td', { colspan: cfg.cols.length }, [empty(cfg.emptyText || '没有符合条件的标的')])]));
      }
      rows.forEach((row) => {
        const trr = h('tr', {
          class: cfg.activeKey && cfg.rowKey && cfg.rowKey(row) === cfg.activeKey ? 'row-active' : '',
          on: cfg.onRow ? { click: () => cfg.onRow(row) } : null,
        });
        cfg.cols.forEach((c) => {
          const td = h('td', { class: (c.cls || '') + (c.cellCls ? ' ' + c.cellCls(row) : '') });
          const content = c.render ? c.render(row) : row[c.key];
          if (content instanceof Node) td.appendChild(content);
          else td.textContent = content === null || content === undefined ? '—' : String(content);
          if (c.onCell) {
            td.style.cursor = 'pointer';
            td.title = c.title || '';
            td.addEventListener('click', (e) => { e.stopPropagation(); c.onCell(row); });
          }
          trr.appendChild(td);
        });
        tbody.appendChild(trr);
      });

      clear(table);
      table.appendChild(thead);
      table.appendChild(tbody);
    }

    renderRows();
    scroll.appendChild(table);
    wrap.appendChild(scroll);
    if (cfg.pager) wrap.appendChild(cfg.pager);
    wrap.update = (rows, keepSort) => { cfg.rows = rows; if (!keepSort) { sortKey = cfg.sortKey || null; sortDir = cfg.sortDir || 'desc'; } renderRows(); };
    return wrap;
  }

  /* 常用单元格渲染 */
  const cells = {
    name(row) {
      return h('span', { class: 'name' }, [
        h('span', { text: row.name || row.code }),
        h('span', { class: 'code', text: (row.market === 'us' ? 'US:' : '') + row.code }),
      ]);
    },
    num(v, d, cls) {
      return h('span', { class: 'num ' + (cls || ''), text: d === undefined ? F.num(v, 2) : F.num(v, d) });
    },
    pct(v) { return pct(v); },
    price(row) { return h('span', { class: 'num ' + F.dir(row.changePct), text: F.price(row.price, row.market) }); },
    amount(row) { return h('span', { class: 'num', text: F.amt(row.amount, row.market) }); },
    cap(row) { return h('span', { class: 'num', text: F.cap(row.marketCap, row.market) }); },
    flow(row) {
      const v = row.mainInflow;
      if (!isFinite(v)) return h('span', { class: 'num', text: '—' });
      return h('span', { class: 'num ' + F.dir(v), text: F.amt(v, row.market) });
    },
    star(on, onClick) {
      const b = h('button', { class: 'btn ghost sm', title: on ? '取消自选' : '加入自选', text: on ? '★' : '☆' });
      b.style.color = on ? 'var(--warn)' : 'var(--text-3)';
      if (onClick) b.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
      return b;
    },
  };

  window.AD = window.AD || {};
  window.AD.ui = { pageHead, section, tbl, empty, loading, seg, cells };
})();
