/* ==========================================================================
   视图 · 盘口事件（涨停梯队 / 龙虎榜 / 集合竞价 / 分笔成交）
   接口：GET /api/features/{kind}（服务端 providers/features.py，四类接口统一信封）
   信封字段：{ ok, data, source, fetchedAt, dataTime, error, degraded, stale }
   本视图只消费服务端已给出的字段，不做任何字段臆造；降级 / 缓存 / 回溯状态原样透出。
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct, paint } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  /* 页签（与服务端 api_features_index 的四个 key 一一对应）
     auto = 自动刷新间隔（毫秒），与服务端缓存 TTL 对齐：分笔 5s / 竞价 15s / 涨停 30s / 龙虎榜 300s */
  const TABS = [
    { value: 'limit_up', label: '涨停梯队', auto: 60000 },
    { value: 'dragon_tiger', label: '龙虎榜', auto: 300000 },
    { value: 'auction', label: '集合竞价', auto: 15000 },
    { value: 'ticks', label: '分笔成交', auto: 10000 },
  ];
  const TAB_LABEL = {};
  TABS.forEach((t) => { TAB_LABEL[t.value] = t.label; });

  const DEFAULT_CODE = '600519';
  const TICK_LIMITS = [30, 60, 120, 200, 500];   // 服务端分笔上限 TICKS_MAX = 500
  const PHASE_TEXT = {
    matched: '已撮合（竞价成交价＝今开）',
    auctioning: '竞价进行中（尚未撮合）',
    no_data: '无数据',
  };
  const SIDE_CLS = { '买盘': 'up', '卖盘': 'down', '中性': '', '未知': '' };

  /* ------------------------------------------------------------ 小工具 */

  /* 'HH:MM:SS' -> 秒数（仅用于表格排序，避免字符串参与数值比较产生 NaN） */
  function secs(t) {
    const m = /^(\d{2}):(\d{2}):(\d{2})$/.exec(String(t || ''));
    return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) : null;
  }

  /* 可点击股票代码 -> 个股详情
     代码/名称同时写到 data-* 上：这两个属性会被原位改写同步，
     因此节点被复用时点击读到的仍是最新的代码，而不是首次渲染时的闭包 */
  function codeCell(code, name, ctx) {
    const btn = h('button', {
      class: 'chip accent', text: code, title: '查看 ' + (name || code) + ' 个股详情',
      style: { cursor: 'pointer' },
      dataset: { code: code || '', name: name || '' },
    });
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      ctx.openSymbol('cn', btn.dataset.code, btn.dataset.name);
    });
    return btn;
  }

  function numSpan(text, cls) {
    return h('span', { class: 'num ' + (cls || ''), text });
  }

  /* 指标卡（复用 .metric-list / .metric 样式），items = [[标签, 值（字符串或节点）]] */
  function metrics(items) {
    const wrap = h('div', { class: 'metric-list' });
    items.forEach((it) => {
      const v = it[1];
      const cell = h('div', { class: 'metric' }, [h('div', { class: 'k', text: it[0] })]);
      const box = h('div', { class: 'v' });
      if (v instanceof Node) box.appendChild(v);
      else box.textContent = v === null || v === undefined || v === '' ? '—' : String(v);
      cell.appendChild(box);
      wrap.appendChild(cell);
    });
    return wrap;
  }

  function noteLine(text) {
    return h('div', { class: 'legend-inline', style: { marginTop: '10px', lineHeight: '1.7' } }, [
      h('span', { class: 'dim3', text: text }),
    ]);
  }

  function warnLine(text) {
    return h('div', { class: 'legend-inline', style: { marginTop: '10px', lineHeight: '1.7' } }, [
      h('span', { class: 'chip warn', text: '提示' }),
      h('span', { class: 'dim3', text: text }),
    ]);
  }

  /* 信封 -> 头部说明文本 */
  function envText(env) {
    if (!env) return '加载中…';
    const parts = [];
    if (env.dataTime) parts.push('数据时间 ' + env.dataTime);
    if (env.fetchedAt) parts.push('取数 ' + F.hhmmss(env.fetchedAt));
    if (env.source) parts.push('源 ' + env.source);
    if (env.stale) parts.push('上游失败，展示缓存旧值' + (env.staleAge ? '（' + env.staleAge + ' 秒前）' : ''));
    return parts.join(' · ') || '—';
  }

  /* 信封 -> 状态标签（无数据 / 降级 / 缓存 / 交易日回溯） */
  function envTags(env) {
    const out = [];
    if (!env) return out;
    if (!env.ok) {
      out.push(h('span', { class: 'chip warn', title: env.error || '', text: '上游无数据' }));
    } else if (env.degraded) {
      out.push(h('span', { class: 'chip warn', title: env.source || '', text: '降级数据源' }));
    }
    if (env.stale) {
      out.push(h('span', {
        class: 'stale-tag',
        title: '上游请求失败，返回进程内缓存的上一次结果' + (env.staleAge ? '（' + env.staleAge + ' 秒前）' : ''),
        text: '缓存旧值',
      }));
    }
    const d = env.data || {};
    if (d.fallback && d.date) {
      out.push(h('span', { class: 'chip accent', text: '已回溯 ' + d.fallbackDays + ' 天 → ' + d.date }));
    }
    return out;
  }

  /* ------------------------------------------------------------ 视图 */

  function mount(root, ctx) {
    const sym = ctx.state.symbol || {};
    const initCode = (sym.market === 'cn' && /^\d{6}$/.test(String(sym.code || ''))) ? String(sym.code) : DEFAULT_CODE;

    const state = {
      tab: 'limit_up',
      code: initCode,          // 集合竞价 / 分笔成交
      limit: 60,               // 分笔条数
      date: '',                // 空 = 服务端自动取最近一个有数据的交易日
      level: 'all',            // 涨停梯队：连板层级筛选
      auto: true,
      env: null,               // 最近一次响应（统一信封；失败时为服务端的「空实现」信封）
      error: null,
    };
    let timer = null;

    const metaHost = h('span', { class: 'hint', text: '加载中…' });
    const tagHost = h('span', { style: { display: 'inline-flex', gap: '6px', alignItems: 'center' } });
    const bodyHost = h('div');

    /* ------------------------------------------------------------------
       无感刷新：每个页签一份常驻骨架
       区块标题 / 指标槽 / 图形槽 / 表格 / 说明槽只建一次，定时刷新只往槽位里
       paint() 与 update()，不再整块 clear + 重建，滚动位置与 hover 都不会丢。
       骨架放在 mount 内（不能放模块级），换页签时旧骨架留在内存里，回来直接复用。
       ------------------------------------------------------------------ */
    const views = {};              /* tab -> 常驻骨架 */

    /* 结果区同一时刻只显示一个节点（加载 / 失败 / 页签骨架）：
       节点身份变了就显式换掉，不参与 morph —— morph 是按位置合并的，身份变了会串内容 */
    function showOnly(node) {
      if (bodyHost.firstChild === node) return;
      clear(bodyHost);
      bodyHost.appendChild(node);
    }

    /* 常驻表格槽位：首次（或从空态切回）挂载实例，之后只 ref.update(rows) */
    function tblSlot(host) {
      let node = null;
      return {
        /* build() 只在实例不存在时调用一次；返回 { node, fresh } */
        get(build) {
          const fresh = !node;
          if (fresh) node = build();
          if (fresh || node.parentNode !== host) {
            clear(host);
            host.appendChild(node);
          }
          return { node, fresh };
        },
        /* 空态与表体是两种结构、且会互相 morph 时用这个：释放实例，下次按首屏重建 */
        drop() { node = null; },
      };
    }

    /* 互斥区块的挂载/摘除：摘下来的节点留在内存里，切回时原位挂回，结构不重建 */
    function place(parent, node, before, on) {
      const inDom = node.parentNode === parent;
      if (on && !inDom) parent.insertBefore(node, before || null);
      else if (!on && inDom) parent.removeChild(node);
    }

    /* 连板层级筛选：结构与 ui.seg 一致，但层级值写在 data-level 上、
       点击时读节点属性 —— 层级项数会随梯队变化，原位改写不会留下过期的闭包取值 */
    function levelSeg(items, onPick) {
      const wrap = h('div', { class: 'seg' });
      items.forEach((it) => {
        const btn = h('button', {
          class: it.value === state.level ? 'active' : '',
          text: it.label,
          dataset: { level: it.value },
        });
        btn.addEventListener('click', () => onPick(btn.dataset.level));
        wrap.appendChild(btn);
      });
      return wrap;
    }

    /* 加载 / 失败占位：每个页签各留一份常驻节点，避免刷新时反复换节点 */
    function showLoading(v, tab) {
      if (!v.loadingNode) {
        v.loadingNode = h('div', { class: 'section' }, [ui.loading(TAB_LABEL[tab] + '加载中…')]);
      }
      showOnly(v.loadingNode);
    }

    function showError(v, tab, msg) {
      if (!v.errorNode) v.errorNode = h('div', { class: 'section' }, [ui.empty('')]);
      paint(v.errorNode, [ui.empty(TAB_LABEL[tab] + '获取失败：' + msg)]);
      showOnly(v.errorNode);
    }

    /* -------------------------------------------- 常驻控件（不随 render 重建） */

    function applyCode() {
      const v = codeInput.value.trim() || DEFAULT_CODE;
      codeInput.value = v;
      state.code = v;
      refresh(true);
    }

    const codeInput = h('input', {
      class: 'inp', style: { width: '104px' }, placeholder: DEFAULT_CODE, value: state.code,
      title: 'A股 6 位代码，如 600519 / 000001 / 300750（北交所无分笔与竞价数据）',
      on: { keydown: (e) => { if (e.key === 'Enter') applyCode(); } },
    });
    const codeField = h('div', { class: 'field' }, [
      h('label', { text: '代码' }),
      codeInput,
      h('button', { class: 'btn sm', text: '查询', on: { click: applyCode } }),
    ]);

    const dateInput = h('input', {
      class: 'inp', type: 'date', style: { width: '148px' }, title: '留空 = 服务端自动取最近一个有数据的交易日',
      on: { change: () => { state.date = dateInput.value; refresh(true); } },
    });
    const dateField = h('div', { class: 'field' }, [
      h('label', { text: '交易日' }),
      dateInput,
      h('button', {
        class: 'btn sm', text: '最近', title: '回到最近一个有数据的交易日',
        on: { click: () => { dateInput.value = ''; state.date = ''; refresh(true); } },
      }),
    ]);

    const limitSel = h('select', { class: 'inp', style: { width: '92px' } });
    TICK_LIMITS.forEach((n) => limitSel.appendChild(h('option', { value: String(n), text: n + ' 笔' })));
    limitSel.value = String(state.limit);
    limitSel.addEventListener('change', () => { state.limit = Number(limitSel.value) || 60; refresh(true); });
    const limitField = h('div', { class: 'field' }, [h('label', { text: '条数' }), limitSel]);

    const autoBox = h('input', {
      type: 'checkbox', checked: state.auto,
      on: { change: (e) => { state.auto = e.target.checked; } },
    });
    const autoField = h('label', {
      class: 'field',
      title: '自动刷新间隔：分笔 10 秒 / 竞价 15 秒 / 涨停梯队 60 秒 / 龙虎榜 5 分钟',
    }, [autoBox, h('span', { class: 'dim3', text: '自动刷新' })]);

    const refreshBtn = h('button', { class: 'btn sm', text: '刷新', on: { click: () => refresh(true) } });

    const toolbar = h('div', { class: 'field', style: { flexWrap: 'wrap', gap: '14px', margin: '0 0 14px' } }, [
      codeField, dateField, limitField,
      h('span', { style: { flex: '1 1 auto' } }),
      autoField, refreshBtn,
    ]);

    const seg = ui.seg(TABS, state.tab, (v) => {
      if (v === state.tab) return;
      state.tab = v;
      state.level = 'all';
      state.env = null;
      state.error = null;
      syncControls();
      schedule();
      refresh(true);
    });

    function syncControls() {
      const t = state.tab;
      codeField.classList.toggle('hidden', !(t === 'auction' || t === 'ticks'));
      limitField.classList.toggle('hidden', t !== 'ticks');
      dateField.classList.toggle('hidden', !(t === 'dragon_tiger' || t === 'limit_up'));
    }

    function params() {
      if (state.tab === 'auction') return { code: state.code };
      if (state.tab === 'ticks') return { code: state.code, limit: state.limit };
      return { date: state.date || null };
    }

    function schedule() {
      if (timer) clearInterval(timer);
      const tab = TABS.filter((t) => t.value === state.tab)[0];
      timer = setInterval(() => {
        if (state.auto && !document.hidden) refresh(false);
      }, (tab && tab.auto) || 60000);
    }

    /* ------------------------------------------------------------ 取数 */

    async function refresh(manual) {
      const kind = state.tab;
      if (manual) refreshBtn.classList.add('spin');
      try {
        const res = await api.feature(kind, params());
        if (kind !== state.tab) return;              // 页签已切换，丢弃过期响应
        state.env = res;
        state.error = null;
        if (manual && res.ok !== false) {
          const d = res.data || {};
          if (d.fallback && d.date) ctx.toast('所选交易日无数据，已回溯到 ' + d.date, 'warn');
          if (res.stale) ctx.toast(TAB_LABEL[kind] + '：上游失败，展示服务端缓存旧值', 'warn');
        }
        if (res.ok === false && manual) {
          ctx.toast(TAB_LABEL[kind] + '：' + (res.error || '上游暂无数据'), 'warn');
        }
      } catch (e) {
        if (kind !== state.tab) return;
        /* api 层在信封 ok=false 时抛错，但会带上原始响应体，这里用「空实现」形状做降级展示 */
        state.env = (e.json && typeof e.json.ok === 'boolean') ? e.json : null;
        const isNew = state.error !== e.message;
        state.error = e.message;
        if (manual || isNew) ctx.toast(TAB_LABEL[kind] + '获取失败：' + e.message, 'err');
      } finally {
        if (kind === state.tab) {
          refreshBtn.classList.remove('spin');
          render();
        }
      }
    }

    /* ------------------------------------------------------------ 渲染 */

    /* 页签 -> 常驻骨架（首次取用时建结构，之后一直复用） */
    function viewOf(tab) {
      if (tab === 'limit_up') return ladderView();
      if (tab === 'dragon_tiger') return lhbView();
      if (tab === 'auction') return auctionView();
      return ticksView();
    }

    function render() {
      if (!state.env && state.error) metaHost.textContent = '获取失败：' + state.error;
      else metaHost.textContent = envText(state.env);
      /* 状态标签原位改写：结构一致时只改文本 / class，不换节点 */
      paint(tagHost, envTags(state.env));

      const tab = state.tab;
      const v = viewOf(tab);
      if (!state.env && !state.error) { showLoading(v, tab); return; }
      if (!state.env) { showError(v, tab, state.error); return; }
      if (tab === 'limit_up') renderLadder();
      else if (tab === 'dragon_tiger') renderLhb();
      else if (tab === 'auction') renderAuction();
      else renderTicks();
    }

    /* ------------------------------------------- 页签 1：涨停梯队 */

    function ladderCols() {
      return [
        { key: 'code', label: '代码', noSort: true, render: (r) => codeCell(r.code, r.name, ctx) },
        { key: 'name', label: '名称', cls: 'name', noSort: true },
        {
          key: 'price', label: '最新价', cls: 'n', value: (r) => r.price,
          render: (r) => numSpan(F.price(r.price), F.dir(r.changePct)),
        },
        { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
        {
          key: 'ladder', label: '连板', cls: 'n', value: (r) => r.ladder,
          render: (r) => h('span', { class: 'chip up', text: (r.ladder || 0) + ' 板' }),
        },
        { key: 'statText', label: '涨停统计', noSort: true, render: (r) => r.statText || '—' },
        { key: 'firstSealTime', label: '首次封板', cls: 'n', value: (r) => secs(r.firstSealTime), render: (r) => r.firstSealTime || '—' },
        { key: 'lastSealTime', label: '最后封板', cls: 'n', value: (r) => secs(r.lastSealTime), render: (r) => r.lastSealTime || '—' },
        { key: 'openTimes', label: '炸板次数', cls: 'n', value: (r) => r.openTimes },
        {
          key: 'sealFund', label: '封板资金', cls: 'n', value: (r) => r.sealFund,
          render: (r) => numSpan(F.amt(r.sealFund, 'cn')),
        },
        {
          key: 'amount', label: '成交额', cls: 'n', value: (r) => r.amount,
          render: (r) => numSpan(F.amt(r.amount, 'cn')),
        },
        {
          key: 'turnoverRate', label: '换手率', cls: 'n', value: (r) => r.turnoverRate,
          render: (r) => numSpan(F.num(r.turnoverRate, 2) + '%'),
        },
        {
          key: 'floatCap', label: '流通市值', cls: 'n', value: (r) => r.floatCap,
          render: (r) => numSpan(F.cap(r.floatCap, 'cn')),
        },
        { key: 'industry', label: '行业', noSort: true, render: (r) => r.industry || '—' },
      ];
    }

    /* 常驻骨架：层级筛选 / 指标 / 分布条 / 表格 / 说明，各自一个槽位 */
    function ladderView() {
      if (views.limit_up) return views.limit_up;
      const segHost = h('div');
      const metricHost = h('div');
      const midHost = h('div');            /* 连板分布条 + 图例 + 断层提示 */
      const tableHost = h('div');          /* 涨停梯队表（常驻实例） */
      const noteHost = h('div');
      const root = ui.section(
        '涨停梯队',
        '东方财富涨停池（push2ex）：按连板数（lbc）降序分梯队；价格原始字段 p 实测为「元 × 1000」，服务端已还原',
        [segHost],
        h('div', {}, [metricHost, midHost, tableHost, noteHost])
      );
      views.limit_up = {
        title: root.querySelector('.section-head h2'),
        segHost, metricHost, midHost, tableHost, noteHost,
        tbl: tblSlot(tableHost),
        root,
      };
      return views.limit_up;
    }

    function renderLadder() {
      const env = state.env;
      const d = env.data || {};
      const stocks = d.stocks || [];
      const ladders = d.ladders || [];
      const gaps = d.gaps || [];
      const levelValues = ladders.map((l) => String(l.level));
      if (state.level !== 'all' && levelValues.indexOf(state.level) < 0) state.level = 'all';
      const rows = state.level === 'all' ? stocks : stocks.filter((s) => String(s.ladder) === state.level);
      const v = ladderView();

      paint(v.segHost, [levelSeg(
        [{ value: 'all', label: '全部 ' + stocks.length }].concat(
          ladders.map((l) => ({ value: String(l.level), label: l.level + ' 板 ' + l.count }))
        ),
        (val) => { state.level = val; render(); }
      )]);

      paint(v.metricHost, [metrics([
        ['涨停家数', d.count],
        ['最高连板', d.maxLadder ? d.maxLadder + ' 板' : '—'],
        ['梯队层级', ladders.length ? ladders.length + ' 层' : '—'],
        ['断层层级', gaps.length ? gaps.map((g) => g + ' 板').join('、') : '无'],
        ['实际交易日', d.date],
        ['请求交易日', d.requestedDate],
        ['交易日回溯', d.fallback ? d.fallbackDays + ' 天' : '否'],
      ])]);

      const mid = [];
      if (ladders.length) {
        const total = Math.max(1, d.count || stocks.length);
        const maxLevel = Math.max(1, d.maxLadder || 1);
        const bar = h('div', { class: 'breadth-bar', style: { marginTop: '12px' } });
        ladders.forEach((l) => {
          bar.appendChild(h('div', {
            style: {
              flex: String(Math.max(l.count / total, 0.01)),
              background: 'var(--up)',
              opacity: String(0.35 + 0.65 * (l.level / maxLevel)),
            },
            title: l.level + ' 板 · ' + l.count + ' 只',
          }));
        });
        mid.push(bar);
        const legend = h('div', { class: 'breadth-legend' });
        ladders.forEach((l) => legend.appendChild(h('span', {}, [
          h('b', { text: String(l.count) }), ' 只 · ' + l.level + ' 板',
        ])));
        mid.push(legend);
      }
      if (gaps.length) {
        mid.push(h('div', { class: 'legend-inline', style: { marginTop: '10px' } }, [
          h('span', { class: 'chip warn', text: '梯队断层' }),
          h('span', { text: gaps.map((g) => g + ' 板').join('、') + ' 无个股（1 ~ ' + (d.maxLadder || 0) + ' 板之间的缺失层级，通常意味着中位连板缺位）' }),
        ]));
      }
      paint(v.midHost, mid);

      if (!rows.length) {
        /* 空态与表体是两种结构：释放常驻实例，下次按首屏重新挂载 */
        v.tbl.drop();
        paint(v.tableHost, [ui.empty(state.error
          ? ('涨停梯队不可用：' + state.error)
          : (d.note || '该交易日无涨停池数据（接口实测仅保留最近约 20 个自然日）'))]);
      } else {
        const t = v.tbl.get(() => ui.tbl({
          cols: ladderCols(),
          rows,
          sortKey: 'ladder',
          sortDir: 'desc',
          maxHeight: '560px',
          compact: true,
          onRow: (r) => ctx.openSymbol('cn', r.code, r.name),
          emptyText: '该连板层级暂无个股',
        }));
        if (!t.fresh) t.node.update(rows);   /* 只更新行：表体、滚动位置与排序状态都留着 */
      }

      const notes = [];
      if (d.note) notes.push(noteLine(d.note));
      if (state.error) notes.push(noteLine('上游返回：' + state.error));
      paint(v.noteHost, notes);

      paint(v.title, ['涨停梯队' + (d.date ? ' · ' + d.date : '')]);
      showOnly(v.root);
    }

    /* ----------------------------------------------- 页签 2：龙虎榜 */

    function lhbTopCols() {
      return [
        { key: 'code', label: '代码', noSort: true, render: (r) => codeCell(r.code, r.name, ctx) },
        { key: 'name', label: '名称', cls: 'name', noSort: true },
        {
          key: 'close', label: '收盘', cls: 'n', value: (r) => r.close,
          render: (r) => numSpan(F.price(r.close), F.dir(r.changePct)),
        },
        { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
        { key: 'turnoverRate', label: '换手率', cls: 'n', value: (r) => r.turnoverRate, render: (r) => numSpan(F.num(r.turnoverRate, 2) + '%') },
        {
          key: 'netAmt', label: '榜单净买额', cls: 'n', value: (r) => r.netAmt,
          render: (r) => numSpan(F.amt(r.netAmt, 'cn'), F.dir(r.netAmt)),
        },
        {
          key: 'reason', label: '上榜原因', noSort: true,
          render: (r) => h('span', { class: 'dim', title: r.reason || '', text: r.reason || '—' }),
        },
      ];
    }

    function lhbDetailCols() {
      const amt = (key, label) => ({
        key, label, cls: 'n', value: (r) => r[key], render: (r) => numSpan(F.amt(r[key], 'cn')),
      });
      return [
        { key: 'code', label: '代码', noSort: true, render: (r) => codeCell(r.code, r.name, ctx) },
        { key: 'name', label: '名称', cls: 'name', noSort: true },
        {
          key: 'close', label: '收盘', cls: 'n', value: (r) => r.close,
          render: (r) => numSpan(F.price(r.close), F.dir(r.changePct)),
        },
        { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
        { key: 'turnoverRate', label: '换手率', cls: 'n', value: (r) => r.turnoverRate, render: (r) => numSpan(F.num(r.turnoverRate, 2) + '%') },
        {
          key: 'netAmt', label: '榜单净买额', cls: 'n', value: (r) => r.netAmt,
          render: (r) => numSpan(F.amt(r.netAmt, 'cn'), F.dir(r.netAmt)),
        },
        amt('buyAmt', '榜单买入额'),
        amt('sellAmt', '榜单卖出额'),
        amt('dealAmt', '榜单成交额'),
        {
          key: 'netRatio', label: '净买占比', cls: 'n', value: (r) => r.netRatio,
          render: (r) => numSpan(F.num(r.netRatio, 2) + '%', F.dir(r.netRatio)),
        },
        {
          key: 'seatBuy', label: '席位买入合计', cls: 'n', value: (r) => (r.seatSum || {}).buy,
          render: (r) => numSpan(F.amt((r.seatSum || {}).buy, 'cn')),
        },
        {
          key: 'seatSell', label: '席位卖出合计', cls: 'n', value: (r) => (r.seatSum || {}).sell,
          render: (r) => numSpan(F.amt((r.seatSum || {}).sell, 'cn')),
        },
        {
          key: 'seatCount', label: '买/卖席位数', cls: 'n', noSort: true,
          render: (r) => numSpan(F.num((r.seatSum || {}).buySeat, 0) + ' / ' + F.num((r.seatSum || {}).sellSeat, 0)),
        },
        { key: 'tradeMarket', label: '交易市场', noSort: true, render: (r) => r.tradeMarket || '—' },
        {
          key: 'reason', label: '上榜原因', noSort: true,
          render: (r) => h('span', { class: 'dim', title: r.reason || '', text: r.reason || '—' }),
        },
        {
          key: 'explain', label: '席位特征', noSort: true,
          render: (r) => h('span', { class: 'dim3', title: r.explain || '', text: r.explain || '—' }),
        },
        {
          key: 'd1', label: '次日涨跌', cls: 'n', value: (r) => (r.performance || {}).d1,
          render: (r) => pct((r.performance || {}).d1),
        },
        {
          key: 'd5', label: '5 日涨跌', cls: 'n', value: (r) => (r.performance || {}).d5,
          render: (r) => pct((r.performance || {}).d5),
        },
      ];
    }

    function lhbStockCols() {
      return [
        { key: 'code', label: '代码', noSort: true, render: (r) => codeCell(r.code, r.name, ctx) },
        { key: 'name', label: '名称', cls: 'name', noSort: true },
        {
          key: 'close', label: '收盘', cls: 'n', value: (r) => r.close,
          render: (r) => numSpan(F.price(r.close), F.dir(r.changePct)),
        },
        { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => pct(r.changePct) },
        { key: 'turnoverRate', label: '换手率', cls: 'n', value: (r) => r.turnoverRate, render: (r) => numSpan(F.num(r.turnoverRate, 2) + '%') },
        { key: 'reasonCount', label: '上榜原因数', cls: 'n', value: (r) => (r.reasons || []).length, render: (r) => (r.reasons || []).length },
        {
          key: 'reasons', label: '上榜原因（合并）', noSort: true,
          render: (r) => h('span', {
            class: 'dim', title: (r.reasons || []).join(' / '),
            text: (r.reasons || []).join(' / ') || '—',
          }),
        },
        {
          key: 'rowIndexes', label: '明细行', cls: 'n', noSort: true,
          render: (r) => numSpan((r.rowIndexes || []).join(', ')),
        },
      ];
    }

    /* 常驻骨架：指标 / 三块数据区（含 4 张常驻表）/ 空态 / 说明 */
    function lhbView() {
      if (views.dragon_tiger) return views.dragon_tiger;
      const metricHost = h('div');
      const dataHost = h('div');           /* 净买额 / 净卖额 / 明细 / 聚合，表实例常驻 */
      const emptyHost = h('div');
      const noteHost = h('div');
      const openRow = (r) => ctx.openSymbol('cn', r.code, r.name);

      /* 表实例只建一次：刷新时 update 行，不重建表体 */
      const buyTbl = ui.tbl({
        cols: lhbTopCols(), rows: [], sortKey: 'netAmt', sortDir: 'desc',
        maxHeight: '300px', compact: true, onRow: openRow, emptyText: '暂无数据',
      });
      const sellTbl = ui.tbl({
        cols: lhbTopCols(), rows: [], sortKey: 'netAmt', sortDir: 'asc',
        maxHeight: '300px', compact: true, onRow: openRow, emptyText: '暂无数据',
      });
      const detailTbl = ui.tbl({
        cols: lhbDetailCols(), rows: [], sortKey: 'netAmt', sortDir: 'desc',
        maxHeight: '560px', compact: true, onRow: openRow, emptyText: '暂无明细',
      });
      const stockTbl = ui.tbl({
        cols: lhbStockCols(), rows: [], sortKey: 'changePct', sortDir: 'desc',
        maxHeight: '420px', compact: true, onRow: openRow, emptyText: '暂无数据',
      });

      dataHost.appendChild(h('div', { class: 'grid g-2', style: { marginTop: '14px' } }, [
        ui.section('净买额前 10', '基于逐条上榜记录（同一标的可能多行，未做合并）', [], buyTbl),
        ui.section('净卖额前 10', '基于逐条上榜记录（同一标的可能多行，未做合并）', [], sellTbl),
      ]));
      dataHost.appendChild(ui.section(
        '上榜明细（一行 = 一个上榜原因）',
        '同一标的当日可能因多个原因上榜，各行金额口径不同，服务端未做求和（避免编造口径）',
        [], detailTbl
      ));
      dataHost.appendChild(ui.section(
        '按个股聚合',
        '仅按代码合并上榜原因与明细行索引，不对金额求和；完整金额请查看上方逐条明细',
        [], stockTbl
      ));

      const body = h('div', {}, [metricHost, dataHost, emptyHost, noteHost]);
      const root = ui.section(
        '龙虎榜',
        '东方财富数据中心（RPT_DAILYBILLBOARD_DETAILSNEW）；当日榜单通常盘后发布，服务端会自动回溯最多 15 个自然日',
        [], body
      );
      views.dragon_tiger = {
        title: root.querySelector('.section-head h2'),
        body, metricHost, dataHost, emptyHost, noteHost,
        buyTbl, sellTbl, detailTbl, stockTbl,
        root,
      };
      return views.dragon_tiger;
    }

    function renderLhb() {
      const env = state.env;
      const d = env.data || {};
      const rows = d.rows || [];
      const stocks = d.stocks || [];
      const v = lhbView();

      paint(v.metricHost, [metrics([
        ['榜单记录', (d.count || 0) + ' 条'],
        ['涉及个股', stocks.length + ' 只'],
        ['榜单日期', d.date],
        ['请求日期', d.requestedDate],
        ['交易日回溯', d.fallback ? d.fallbackDays + ' 天' : '否'],
      ])]);

      if (rows.length) {
        /* 数据块（含 4 张常驻表）挂回原位，只 update 行；空态槽位清空 */
        place(v.body, v.dataHost, v.emptyHost, true);
        paint(v.emptyHost, []);
        v.buyTbl.update(d.topNetBuy || []);
        v.sellTbl.update(d.topNetSell || []);
        v.detailTbl.update(rows);
        v.stockTbl.update(stocks);
      } else {
        /* 无数据：数据块整体摘下（结构留在内存里），原位换成空态 */
        place(v.body, v.dataHost, v.emptyHost, false);
        paint(v.emptyHost, [ui.empty(state.error
          ? ('龙虎榜不可用：' + state.error)
          : '该交易日暂无龙虎榜数据')]);
      }

      const notes = [];
      if (d.note) notes.push(noteLine(d.note));
      if (state.error) notes.push(noteLine('上游返回：' + state.error));
      paint(v.noteHost, notes);

      paint(v.title, ['龙虎榜' + (d.date ? ' · ' + d.date : '')]);
      showOnly(v.root);
    }

    /* --------------------------------------------- 页签 3：集合竞价 */

    /* 常驻骨架：代码按钮 / 指标 / 提示行 / 未匹配量 / 表格 / 空态 / 说明 */
    function auctionView() {
      if (views.auction) return views.auction;
      const asideHost = h('div');          /* 页头「代码」按钮 */
      const metricHost = h('div');
      const preHost = h('div');            /* 竞价价来源 / 竞价中提示 */
      const unmatchedHost = h('div');      /* 未匹配量不可得（常驻一行） */
      const tblWrap = h('div', { style: { marginTop: '14px' } });
      const emptyHost = h('div');
      const noteHost = h('div');
      const root = ui.section(
        '集合竞价',
        '东方财富分笔 09:15~09:25 段为主源；腾讯分笔首条 + 实时行情为降级源。竞价成交价 = 当日今开',
        [asideHost],
        h('div', {}, [metricHost, preHost, unmatchedHost, tblWrap, emptyHost, noteHost])
      );
      views.auction = {
        title: root.querySelector('.section-head h2'),
        asideHost, metricHost, preHost, unmatchedHost, tblWrap, emptyHost, noteHost,
        tbl: tblSlot(tblWrap),
        root,
      };
      return views.auction;
    }

    function renderAuction() {
      const env = state.env;
      const d = env.data || {};
      const phase = d.phase || 'no_data';
      const ticks = d.orderTicks || [];
      const v = auctionView();

      paint(v.asideHost, d.code ? [codeCell(d.code, null, ctx)] : []);

      paint(v.metricHost, [metrics([
        ['竞价阶段', PHASE_TEXT[phase] || phase],
        ['竞价成交价（今开）', d.price === null || d.price === undefined
          ? '—' : numSpan(F.price(d.price), F.dir(d.changePct))],
        ['昨收', F.price(d.preClose)],
        ['竞价涨跌幅', pct(d.changePct)],
        ['竞价成交量', F.vol(d.volume)],
        ['竞价成交额', F.amt(d.amount, 'cn')],
        ['竞价撮合笔数', d.trades],
        ['撮合时间', d.auctionTime],
        ['交易日', d.tradeDate],
        ['市场', d.market ? String(d.market).toUpperCase() : '—'],
        ['委托快照条数', d.orderCount],
        ['委托量合计（仅参考）', F.vol(d.orderVolume)],
        ['撮合前最后委托量', F.vol(d.lastOrderVolume)],
        ['未匹配量', '—'],
      ])]);

      const pre = [];
      if (d.priceSource) pre.push(noteLine('竞价价来源：' + d.priceSource));
      if (phase === 'auctioning') {
        pre.push(warnLine(
          '仍在 09:15~09:25 竞价中：price 为最后一条竞价委托快照价，尚未撮合，成交量与笔数不可得，仅供盘中观察。'
        ));
      }
      paint(v.preHost, pre);

      /* 未匹配量 / 撤单量：公开接口缺失，服务端恒返回 null，此处按降级口径展示 */
      paint(v.unmatchedHost, [h('div', { class: 'legend-inline', style: { marginTop: '10px', lineHeight: '1.7' } }, [
        h('span', { class: 'chip warn', text: '未匹配量不可得' }),
        h('span', {
          class: 'dim3',
          text: 'unmatched = null。' + (d.unmatchedNote
            || '公开接口（腾讯 / 东方财富）均未提供集合竞价未匹配量与撤单量，该字段留空。'),
        }),
      ])]);

      const notes = [];
      if (ticks.length) {
        /* 仅做前端展示标记（不上报、不参与字段口径）：最后一条 = 撮合前最后一条委托快照 */
        const rows = ticks.map((t, i) => ({ time: t.time, price: t.price, volume: t.volume, last: i === ticks.length - 1 }));
        const cols = [
          { key: 'time', label: '快照时间', cls: 'n', value: (r) => secs(r.time), render: (r) => r.time },
          { key: 'price', label: '委托价', cls: 'n', value: (r) => r.price, render: (r) => numSpan(F.price(r.price)) },
          { key: 'volume', label: '委托量', cls: 'n', value: (r) => r.volume, render: (r) => numSpan(F.vol(r.volume)) },
          {
            key: 'last', label: '备注', noSort: true,
            render: (r) => (r.last
              ? h('span', { class: 'chip warn', text: '撮合前最后一条' })
              : h('span', { class: 'dim3', text: '竞价委托快照（成交笔数 0）' })),
          },
        ];
        const t = v.tbl.get(() => ui.tbl({
          cols, rows, sortKey: 'time', sortDir: 'desc', maxHeight: '300px', compact: true,
          emptyText: '当日无竞价委托快照',
        }));
        if (!t.fresh) t.node.update(rows);
        paint(v.emptyHost, []);
        notes.push(noteLine(
          '竞价委托快照（09:15:00~09:25:59）：该段记录的成交笔数恒为 0，不计入当日成交量；' +
          '「量」实测为非递增序列（撤单会回落），因此 orderVolume 只是逐条快照量合计，仅作参考，' +
          'lastOrderVolume（撮合前最后一条）更接近待撮合委托量口径；服务端最多回传 30 条。'
        ));
      } else {
        v.tbl.drop();
        paint(v.tblWrap, []);              /* 只摘掉常驻表格，空态槽位紧接着它 */
        paint(v.emptyHost, phase === 'no_data'
          ? [ui.empty(state.error
            ? ('集合竞价不可用：' + state.error)
            : '当日无集合竞价数据（未开盘 / 非交易日；北交所实测无分笔与竞价数据）')]
          : []);
      }
      paint(v.noteHost, notes);

      paint(v.title, ['集合竞价' + (d.code ? ' · ' + d.code : '')]);
      showOnly(v.root);
    }

    /* --------------------------------------------- 页签 4：分笔成交 */

    /* 常驻骨架：代码按钮 / 指标 / 方向分布 / 表格 / 空态 / 说明 */
    function ticksView() {
      if (views.ticks) return views.ticks;
      const asideHost = h('div');          /* 页头「代码」按钮 */
      const metricHost = h('div');
      const midHost = h('div');            /* 方向分布条 + 图例 + 汇总说明 */
      const tblWrap = h('div', { style: { marginTop: '14px' } });
      const emptyHost = h('div');
      const noteHost = h('div');
      const root = ui.section(
        '分笔成交',
        '东方财富分笔（含成交笔数与方向 1=卖盘 / 2=买盘 / 4=中性）为主源，腾讯分笔（方向 B/S/M）为降级源；两个源均仅支持当日',
        [asideHost],
        h('div', {}, [metricHost, midHost, tblWrap, emptyHost, noteHost])
      );
      views.ticks = {
        title: root.querySelector('.section-head h2'),
        asideHost, metricHost, midHost, tblWrap, emptyHost, noteHost,
        tbl: tblSlot(tblWrap),
        root,
      };
      return views.ticks;
    }

    function renderTicks() {
      const env = state.env;
      const d = env.data || {};
      const raw = d.ticks || [];
      const rows = raw.slice().reverse();          // 最新在上
      const base = window.AD.isNum(d.preClose) ? d.preClose : null;
      const v = ticksView();

      const agg = { buy: 0, sell: 0, mid: 0, other: 0 };
      raw.forEach((t) => {
        const k = t.sideText === '买盘' ? 'buy'
          : (t.sideText === '卖盘' ? 'sell' : (t.sideText === '中性' ? 'mid' : 'other'));
        agg[k] += window.AD.isNum(t.volume) ? t.volume : 0;
      });
      const chg = (window.AD.isNum(d.latestPrice) && base) ? d.latestPrice - base : null;

      paint(v.asideHost, d.code ? [codeCell(d.code, null, ctx)] : []);

      paint(v.metricHost, [metrics([
        ['最新价', window.AD.isNum(d.latestPrice) ? numSpan(F.price(d.latestPrice), F.dir(chg)) : '—'],
        ['最新成交时间', d.latestTime],
        ['返回笔数', d.count],
        ['昨收', F.price(d.preClose)],
        ['交易日', d.tradeDate],
        ['市场', d.market ? String(d.market).toUpperCase() : '—'],
        ['买盘量（按 sideText 汇总）', F.vol(agg.buy)],
        ['卖盘量（按 sideText 汇总）', F.vol(agg.sell)],
        ['中性量（按 sideText 汇总）', F.vol(agg.mid)],
      ])]);

      const mid = [];
      if (raw.length) {
        const total = Math.max(1, agg.buy + agg.sell + agg.mid + agg.other);
        const bar = h('div', { class: 'breadth-bar', style: { marginTop: '12px' } });
        const segs = [
          { v: agg.buy, c: 'var(--up)', label: '买盘' },
          { v: agg.mid, c: '#5d677a', label: '中性' },
          { v: agg.sell, c: 'var(--down)', label: '卖盘' },
          { v: agg.other, c: 'var(--warn)', label: '未知' },
        ];
        segs.forEach((s) => {
          if (s.v <= 0) return;
          bar.appendChild(h('div', {
            style: { flex: String(Math.max(s.v / total, 0.004)), background: s.c },
            title: s.label + ' ' + F.vol(s.v),
          }));
        });
        mid.push(bar);
        const legend = h('div', { class: 'breadth-legend' });
        segs.forEach((s) => legend.appendChild(h('span', {}, [
          h('b', { text: F.vol(s.v) }), ' ' + s.label,
        ])));
        legend.appendChild(h('span', {}, ['成交笔数合计 ', h('b', {
          text: String(raw.reduce((a, t) => a + (window.AD.isNum(t.trades) ? t.trades : 0), 0)),
        })]));
        mid.push(legend);
        mid.push(noteLine(
          '汇总为前端按 sideText 逐笔累加（服务端不提供方向汇总字段），仅作盘中观察；' +
          '服务端已说明：主动买卖判定与腾讯外盘/内盘口径不同，方向字段仅供参考。'
        ));
      }
      paint(v.midHost, mid);

      if (rows.length) {
        const cols = [
          { key: 'time', label: '时间', cls: 'n', value: (r) => secs(r.time), render: (r) => r.time },
          {
            key: 'price', label: '价格', cls: 'n', value: (r) => r.price,
            render: (r) => numSpan(F.price(r.price), base ? F.dir(r.price - base) : ''),
          },
          { key: 'volume', label: '成交量', cls: 'n', value: (r) => r.volume, render: (r) => numSpan(F.vol(r.volume)) },
          { key: 'amount', label: '成交额', cls: 'n', value: (r) => r.amount, render: (r) => numSpan(F.amt(r.amount, 'cn')) },
          {
            key: 'trades', label: '成交笔数', cls: 'n',
            value: (r) => (window.AD.isNum(r.trades) ? r.trades : null),
            render: (r) => numSpan(window.AD.isNum(r.trades) ? String(r.trades) : '—'),
          },
          {
            key: 'sideText', label: '方向', noSort: true,
            render: (r) => h('span', { class: 'chip ' + (SIDE_CLS[r.sideText] || ''), text: r.sideText || '—' }),
          },
        ];
        const t = v.tbl.get(() => ui.tbl({
          cols, rows, sortKey: 'time', sortDir: 'desc', maxHeight: '560px', compact: true,
          onRow: (r) => { if (d.code) ctx.openSymbol('cn', d.code, d.code); },
          emptyText: '当日无分笔明细',
        }));
        if (!t.fresh) t.node.update(rows);
        paint(v.emptyHost, []);
      } else {
        v.tbl.drop();
        paint(v.tblWrap, []);
        paint(v.emptyHost, [ui.empty(state.error
          ? ('分笔成交不可用：' + state.error)
          : '当日无分笔明细（未开盘 / 非交易日；北交所实测无分笔数据）')]);
      }

      const notes = [];
      if (d.sideRule) notes.push(noteLine('方向口径：' + d.sideRule));
      if (d.note) notes.push(noteLine(d.note));
      if (state.error) notes.push(noteLine('上游返回：' + state.error));
      notes.push(noteLine('表格按时间倒序（最新在上）；「成交笔数」为「—」表示该数据源不提供该字段（降级源腾讯分笔）。'));
      paint(v.noteHost, notes);

      paint(v.title, ['分笔成交' + (d.code ? ' · ' + d.code : '')]);
      showOnly(v.root);
    }

    /* ------------------------------------------------------------ 装配 */

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead(
        '盘口事件',
        'A股专属事件数据：涨停梯队 · 龙虎榜 · 集合竞价 · 分笔成交（东方财富 / 腾讯公开接口；涨停池仅最近约 20 天，分笔与竞价仅当日）',
        [tagHost, metaHost]
      ),
      h('div', { style: { marginBottom: '14px' } }, [seg]),
      toolbar,
      bodyHost,
      h('div', { class: 'legend-inline', style: { marginTop: '22px', color: 'var(--text-3)', lineHeight: '1.7' } }, [
        '表格中「代码」可直接跳转个股详情，表头可点击排序。数据来自公开行情接口，可能存在延迟或缺失，仅供研究学习，不构成投资建议。',
      ]),
    ]));

    syncControls();
    refresh(true);
    schedule();

    return {
      refresh: () => refresh(true),
      destroy() {
        if (timer) clearInterval(timer);
        timer = null;
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.features = { mount };
})();
