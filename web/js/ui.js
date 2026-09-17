/* ==========================================================================
   AlphaDesk · 通用 UI 组件（页面骨架 / 数据表 / 空状态）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct, paint, reconcile } = window.AD.dom;
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
   *
   * 刷新用 wrap.update(rows)：按行 key 增量更新（行没变就不碰 DOM），
   * 不重建表体、不清空滚动容器，所以看不到闪动，滚动位置与 hover 也不会丢。
   */
  function tbl(cfg) {
    const wrap = h('div', { class: 'tbl-wrap' });
    const scroll = h('div', { class: 'tbl-scroll', style: { maxHeight: cfg.maxHeight || 'none' } });
    const table = h('table', { class: 'tbl' + (cfg.compact ? ' compact' : '') });
    const thead = h('thead');
    const tbody = h('tbody');
    let cols = cfg.cols || [];
    let sortKey = cfg.sortKey || null;
    let sortDir = cfg.sortDir || 'desc';
    let headSig = null;
    const rowRefs = new Map();
    const EMPTY = { __adEmptyRow: true };

    function rowKeyOf(row, i) {
      if (cfg.rowKey) return String(cfg.rowKey(row, i));
      if (row && row.id !== undefined && row.id !== null && row.id !== '') return 'id:' + row.id;
      if (row && (row.code || row.symbol)) return (row.market || '') + ':' + (row.code || row.symbol);
      return '#' + i;
    }

    /* 同一行在整个生命周期复用同一个对象引用：单元格里的按钮闭包捕获它之后，
       刷新时读到的是最新数据，而不会停在首次渲染时的旧值 */
    function rowRef(key, row) {
      let ref = rowRefs.get(key);
      if (!ref) { ref = {}; rowRefs.set(key, ref); }
      Object.keys(ref).forEach((k) => { if (!(k in row)) { try { delete ref[k]; } catch (e) { /* 忽略 */ } } });
      Object.assign(ref, row);
      return ref;
    }

    function sortedRows() {
      const rows = (cfg.rows || []).slice();
      if (sortKey) {
        const col = cols.find((c) => c.key === sortKey);
        if (col && col.value) {
          rows.sort((a, b) => {
            const av = col.value(a), bv = col.value(b);
            if (av === null || av === undefined) return 1;
            if (bv === null || bv === undefined) return -1;
            return sortDir === 'desc' ? bv - av : av - bv;
          });
        }
      }
      return rows;
    }

    /* 表头只在列定义或排序状态变化时重建（重建表头会打断 hover，没必要每次刷新都做） */
    function renderHead() {
      const sig = cols.map((c) => c.key + '\u0001' + (c.label || '')).join('\u0002') + '|' + sortKey + '|' + sortDir;
      if (sig === headSig) return;
      headSig = sig;
      const tr = h('tr');
      cols.forEach((c) => {
        const isSorted = sortKey === c.key && !c.noSort;
        const th = h('th', {
          class: (c.noSort ? 'no-sort ' : '') + (c.cls || '') + (isSorted ? ' sorted' : ''),
          style: c.width ? { width: c.width } : null,
          title: c.title || '',
          on: c.noSort ? null : {
            click: () => {
              if (sortKey === c.key) sortDir = sortDir === 'desc' ? 'asc' : 'desc';
              else { sortKey = c.key; sortDir = 'desc'; }
              renderHead();
              renderBody();
            },
          },
        }, [c.label]);
        if (!c.noSort) th.appendChild(h('span', { class: 'arrow', text: isSorted ? (sortDir === 'desc' ? '↓' : '↑') : '↕' }));
        tr.appendChild(th);
      });
      clear(thead);
      thead.appendChild(tr);
    }

    function rowTr(key, row) {
      const ref = rowRef(key, row);
      const tr = h('tr', {
        class: cfg.activeKey && cfg.rowKey && cfg.rowKey(ref) === cfg.activeKey ? 'row-active' : '',
        on: cfg.onRow ? { click: () => cfg.onRow(ref) } : null,
      });
      cols.forEach((c) => {
        const td = h('td', { class: (c.cls || '') + (c.cellCls ? ' ' + c.cellCls(ref) : '') });
        const content = c.render ? c.render(ref) : ref[c.key];
        if (content instanceof Node) td.appendChild(content);
        else td.textContent = content === null || content === undefined ? '—' : String(content);
        if (c.onCell) {
          td.style.cursor = 'pointer';
          td.title = c.title || '';
          td.addEventListener('click', (e) => { e.stopPropagation(); c.onCell(ref); });
        }
        tr.appendChild(td);
      });
      return tr;
    }

    function renderBody() {
      const rows = sortedRows();
      const items = rows.length ? rows : [EMPTY];
      const used = new Set();
      reconcile(tbody, items, {
        key: (row, i) => {
          const k = row === EMPTY ? '__ad-empty' : rowKeyOf(row, i);
          used.add(k);
          return k;
        },
        render: (row, i) => (row === EMPTY
          ? h('tr', {}, [h('td', { colspan: Math.max(1, cols.length) }, [empty(cfg.emptyText || '没有符合条件的标的')])])
          : rowTr(rowKeyOf(row, i), row)),
      });
      /* 行引用只保留当前还在表里的 key：长会话里反复刷新不会把旧行的代理对象越攒越多 */
      if (rowRefs.size > used.size) {
        Array.from(rowRefs.keys()).forEach((k) => { if (!used.has(k)) rowRefs.delete(k); });
      }
    }

    renderHead();
    renderBody();
    scroll.appendChild(table);
    table.appendChild(thead);
    table.appendChild(tbody);
    wrap.appendChild(scroll);
    if (cfg.pager) wrap.appendChild(cfg.pager);
    wrap.update = (rows, keepSort) => {
      cfg.rows = rows;
      if (!keepSort) { sortKey = cfg.sortKey || null; sortDir = cfg.sortDir || 'desc'; }
      renderHead();
      renderBody();
      return wrap;
    };
    /* 列定义随市场等外部条件变化时用这个，避免整表重建 */
    wrap.setCols = (next) => { cols = next || []; headSig = null; renderHead(); renderBody(); return wrap; };
    return wrap;
  }

  /**
   * 原位挂载：容器里只有「一份当前内容」时用它（例如表格实例与空态互切）。
   * 同一状态内走增量改写（paint/morph），只有与「长期复用的缓存实例」互切时才真正换节点。
   * 长期复用的节点（表格实例、列表容器）请打 __adKeep 标记，
   * 否则会被当成普通块与别的结构做 morph 合并。
   */
  function mountInto(host, node) {
    const cur = host.firstChild;
    if (cur === node) return host;
    if (cur && !cur.__adKeep && !node.__adKeep) return paint(host, [node]);
    clear(host);
    host.appendChild(node);
    return host;
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
  window.AD.ui = { pageHead, section, tbl, mountInto, empty, loading, seg, cells };
})();
