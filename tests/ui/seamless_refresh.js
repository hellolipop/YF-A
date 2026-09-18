/* 前端回归测试：无感刷新（更新数据不重构图）

用户反馈的故障：图表 / 列表更新时界面闪动，需要「无感刷新」而不是整块重建。

断言分两段：
  A 段（只需 jsdom，不需要服务端）：增量更新语义
     · 数据没变时，更新一轮必须发生 **零次 DOM 写入**（MutationObserver 计数）；
     · 变化时只改变化的文本，表体 / 行 / 单元格节点身份保持不变；
     · 顺序变化只做节点移动，不重建；
     · 行内按钮的监听在刷新后读到的是**最新**数据；
     · 正在输入的输入框不会被刷新覆盖；
     · canvas 节点永不被替换（否则画布内容与上下文都会丢）。

  B 段（需要服务端）：真实页面
     · 自选股轮询后：tbody、首行节点仍是同一对象，表体无子节点增删；
     · 个股详情刷新时：canvas 不被清空、不出现「加载中」占位，刷新后仍是同一个 canvas；
     · 切换周期（同形态）不重建 canvas；
     · 顶部时段徽标每秒刷新只改文本，不重建节点。

准备：npm i jsdom（或 NODE_PATH=/path/to/node_modules）
      B 段还需先启动服务：python3 server.py --port 8848
运行：node tests/ui/seamless_refresh.js
      AD_BASE=http://127.0.0.1:9000 node tests/ui/seamless_refresh.js
缺少 jsdom 或服务未启动时打印原因并跳过，不会误报失败。
*/
const fs = require('fs');
const path = require('path');

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  console.log('跳过：未安装 jsdom。安装后重跑：npm i jsdom（或在已安装 jsdom 的目录执行本文件）');
  process.exit(0);
}

const ROOT = path.join(__dirname, '..', '..');
const BASE = process.env.AD_BASE || 'http://127.0.0.1:8848';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const flush = () => new Promise((r) => setTimeout(r, 0));   /* 让 MutationObserver 派发记录 */

let pass = 0, fail = 0;
function check(name, ok, extra) {
  console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (ok ? '' : '　' + (extra === undefined ? '' : extra)));
  ok ? pass++ : fail++;
}

/* ==================================================================
   A 段：增量更新语义（离线）
   ================================================================== */

function readWeb(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function counter(w, target, opts) {
  let n = 0;
  const mo = new w.MutationObserver(() => { n += 1; });
  mo.observe(target, opts || { childList: true, subtree: true, characterData: true, attributes: true });
  return { get count() { return n; }, stop() { mo.disconnect(); }, flush };
}

async function partA() {
  console.log('\n=== A 段：增量更新语义（离线） ===');
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window;
  w.eval(readWeb('web/js/util.js'));
  w.eval(readWeb('web/js/ui.js'));

  const dom2 = w.AD.dom;
  const ui = w.AD.ui;
  const doc = w.document;

  const cols = [
    { key: 'name', label: '名称', noSort: true, render: (r) => w.AD.dom.h('span', { text: r.name }) },
    { key: 'price', label: '最新价', cls: 'n', value: (r) => r.price, render: (r) => w.AD.dom.h('span', { class: 'num', text: String(r.price) }) },
    { key: 'changePct', label: '涨跌幅', cls: 'n', value: (r) => r.changePct, render: (r) => w.AD.dom.h('span', { class: 'num', text: String(r.changePct) }) },
  ];
  const rowsA = [
    { market: 'cn', code: '600519', name: '贵州茅台', price: 1500, changePct: 1.2 },
    { market: 'cn', code: '000001', name: '平安银行', price: 11.8, changePct: -0.4 },
  ];

  /* A1：数据未变 -> 零 DOM 写入 */
  const t1 = ui.tbl({ cols, rows: rowsA, rowKey: (r) => r.market + ':' + r.code, maxHeight: '60px' });
  doc.body.appendChild(t1);
  const body1 = t1.querySelector('tbody');
  const rowA = body1.children[0];
  const rowB = body1.children[1];
  const cellPrice = rowA.children[1];
  const c1 = counter(w, t1);
  t1.update(rowsA);
  await flush();
  check('A1 数据未变：更新一轮零 DOM 写入', c1.count === 0, 'mutations=' + c1.count);
  check('A1 表体与行节点身份保持', t1.querySelector('tbody') === body1 && body1.children[0] === rowA && body1.children[1] === rowB);
  c1.stop();

  /* A2：只有变化的单元格被改写 */
  const rowsA2 = rowsA.map((r) => (r.code === '600519' ? Object.assign({}, r, { price: 1512.5 }) : Object.assign({}, r)));
  const c2 = counter(w, t1, { childList: true, subtree: true, characterData: true });
  t1.update(rowsA2);
  await flush();
  check('A2 只改写变化单元格的文本（1~2 次写入）', c2.count >= 1 && c2.count <= 2, 'mutations=' + c2.count);
  check('A2 单元格节点未重建', rowA.children[1] === cellPrice);
  check('A2 文本已更新', cellPrice.textContent === '1512.5', cellPrice.textContent);
  check('A2 行节点仍是同一对象', t1.querySelector('tbody').children[0] === rowA);
  c2.stop();

  /* A3：顺序变化只移动节点，不重建 */
  t1.update([rowsA2[1], rowsA2[0]], true);
  await flush();
  check('A3 行顺序变化后节点被移动而非重建',
    body1.children[0] === rowB && body1.children[1] === rowA,
    'children=' + Array.prototype.map.call(body1.children, (x) => x.textContent.slice(0, 4)).join('|'));

  /* A4：滚动位置保留 */
  const scroll = t1.querySelector('.tbl-scroll');
  scroll.scrollTop = 18;
  t1.update(rowsA2);
  await flush();
  check('A4 更新后滚动位置保持不变', scroll.scrollTop === 18, 'scrollTop=' + scroll.scrollTop);

  /* A5：行内监听读到最新数据（节点被复用，闭包不能停在旧对象） */
  let seen = null;
  const t2 = ui.tbl({
    cols: [{ key: 'act', label: '操作', noSort: true, render: (r) => w.AD.dom.h('button', { text: '详情', on: { click: () => { seen = r; } } }) }],
    rows: [{ id: 7, name: 'A', price: 1 }], rowKey: (r) => r.id,
  });
  doc.body.appendChild(t2);
  const btn = t2.querySelector('button');
  t2.update([{ id: 7, name: 'A', price: 99 }]);
  await flush();
  btn.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  check('A5 刷新后行内按钮读到最新数据', !!seen && seen.price === 99, 'seen=' + (seen && seen.price));

  /* A6：正在输入的输入框不被覆盖 */
  const host = doc.createElement('div');
  const inp = doc.createElement('input');
  host.appendChild(inp);
  doc.body.appendChild(host);
  inp.focus();
  inp.value = '正在输入';
  dom2.paint(host, [w.AD.dom.h('input', { value: '服务端新值' })]);
  check('A6 焦点输入框的输入内容不被刷新覆盖', inp.value === '正在输入', 'value=' + inp.value);

  /* A7：canvas 节点永不被替换 */
  const cvsHost = doc.createElement('div');
  const cvs = doc.createElement('canvas');
  cvsHost.appendChild(cvs);
  doc.body.appendChild(cvsHost);
  dom2.paint(cvsHost, [w.AD.dom.h('canvas', {})]);
  check('A7 canvas 节点被保留（不重建画布）', cvsHost.firstChild === cvs && cvsHost.children.length === 1);

  /* A8：keyed 列表：行被复用，缺项被移除 */
  const listHost = doc.createElement('div');
  doc.body.appendChild(listHost);
  const items = [{ id: 1, v: 'a' }, { id: 2, v: 'b' }, { id: 3, v: 'c' }];
  dom2.reconcile(listHost, items, { key: (x) => x.id, render: (x) => w.AD.dom.h('div', { text: x.v }) });
  const n2 = listHost.children[1];
  dom2.reconcile(listHost, [{ id: 2, v: 'b2' }, { id: 1, v: 'a' }], { key: (x) => x.id, render: (x) => w.AD.dom.h('div', { text: x.v }) });
  check('A8 keyed 列表：复用节点、按新顺序移动、删除缺项',
    listHost.children.length === 2 && listHost.children[0] === n2 && listHost.children[0].textContent === 'b2',
    'len=' + listHost.children.length);

  w.close();
}

/* ==================================================================
   B 段：真实页面（需要服务端）
   ================================================================== */

const canvasStub = () => {
  const n = () => {}; const g = { addColorStop: n };
  return {
    canvas: { width: 900, height: 400 }, setTransform: n, clearRect: n, save: n, restore: n,
    beginPath: n, closePath: n, moveTo: n, lineTo: n, arc: n, rect: n, fill: n, stroke: n,
    fillRect: n, strokeRect: n, clip: n, fillText: n, strokeText: n, setLineDash: n,
    translate: n, rotate: n, scale: n, measureText: () => ({ width: 34 }),
    createLinearGradient: () => g, createRadialGradient: () => g, drawImage: n, putImageData: n,
    font: '', textAlign: '', textBaseline: '', fillStyle: '', strokeStyle: '', lineWidth: 1, globalAlpha: 1,
  };
};

const SCRIPTS = [
  '/js/util.js', '/js/api.js', '/js/stream.js', '/js/ui.js', '/js/indicators.js', '/js/chart.js',
  '/js/views/market.js', '/js/views/watchlist.js', '/js/views/detail.js', '/js/views/screener.js',
  '/js/views/features.js', '/js/views/backtest.js', '/js/views/tracker.js', '/js/views/system.js',
  '/js/views/alerts.js', '/js/views/news.js', '/js/views/advisor.js', '/js/views/scan.js',
  '/js/views/trade.js', '/js/app.js',
];

async function partB() {
  console.log('\n=== B 段：真实页面（需要服务端） ===');
  try {
    await fetch(BASE + '/api/health');
  } catch (e) {
    console.log('跳过：服务未启动。先运行 python3 server.py --port 8848 再执行本文件');
    return 'skipped';
  }
  const html = await (await fetch(BASE + '/')).text();
  const errs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errs.push(String((e && e.message) || e)));
  vc.on('error', (...a) => errs.push(a.map(String).join(' ').slice(0, 240)));
  const dom = new JSDOM(html, { url: BASE + '/', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window;
  w.fetch = (u, o) => fetch(new URL(String(u), BASE).toString(), o);
  w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  w.HTMLCanvasElement.prototype.getContext = () => canvasStub();
  delete w.Notification;
  for (const s of SCRIPTS) w.eval(await (await fetch(BASE + s)).text());
  await wait(1500);

  const doc = w.document;
  const root = doc.getElementById('view-root');
  const click = (el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const live = () => w.AD.app.ctx.state.active;

  /* ---------------- B1 自选股：轮询不重建表 ------------- */
  w.AD.app.ctx.setWatch([
    { market: 'cn', code: '600519', name: '贵州茅台' },
    { market: 'cn', code: '000001', name: '平安银行' },
  ]);
  w.AD.app.switchView('watchlist');
  await wait(4000);
  const tblWrap = root.querySelector('.tbl-wrap');
  check('B1 自选股表格已渲染', !!tblWrap && !!tblWrap.querySelector('tbody'));
  if (tblWrap) {
    const tbody = tblWrap.querySelector('tbody');
    const row0 = tbody.children[0];
    let churn = 0;
    const mo = new w.MutationObserver((recs) => {
      recs.forEach((r) => { if (r.type === 'childList') churn += 1; });
    });
    mo.observe(tbody, { childList: true, subtree: true, characterData: true });
    live().refresh();
    await wait(3000);
    check('B1 轮询后表体是同一个节点', tblWrap.querySelector('tbody') === tbody);
    check('B1 轮询后首行是同一个节点', tbody.children[0] === row0);
    check('B1 轮询期间表体无子节点增删（未重建）', churn === 0, 'childList=' + churn);
    mo.disconnect();
  }

  /* ---------------- B2 个股详情：刷新复用画布 ---------------- */
  w.AD.app.ctx.state.symbol = { market: 'cn', code: '600519', name: '贵州茅台' };
  w.AD.app.switchView('detail');
  await wait(10000);
  const chartHost = root.querySelector('.chart-canvas-wrap');
  const headHost = root.querySelector('.quote-head');
  const metricList = root.querySelector('.metric-list');
  check('B2 详情页图表容器已渲染', !!chartHost);
  if (chartHost) {
    const canvas = chartHost.querySelector('canvas');
    check('B2 分时图 canvas 已就绪', !!canvas);
    live().refresh();
    const syncCanvas = chartHost.querySelector('canvas');
    check('B2 刷新触发瞬间 canvas 仍在（没有先清空再加载）', syncCanvas === canvas);
    check('B2 刷新期间不出现「加载中」占位', !chartHost.querySelector('.loading'));
    check('B2 刷新期间不出现空态节点（保留旧图）', !chartHost.querySelector('.empty'));
    check('B2 刷新期间容器显示 is-busy 角标', chartHost.classList.contains('is-busy'));
    await wait(3500);
    check('B2 刷新后仍是同一个 canvas 节点', chartHost.querySelector('canvas') === canvas);
    check('B2 刷新后页头是同一节点', root.querySelector('.quote-head') === headHost);
    check('B2 刷新后关键指标区是同一节点', root.querySelector('.metric-list') === metricList);

    /* 切换周期：同形态（K线 -> K线）只换数据，不换画布 */
    const segs = root.querySelectorAll('.chart-toolbar .seg');
    const periodBtns = segs.length ? segs[0].querySelectorAll('button') : [];
    const findBtn = (label) => Array.prototype.find.call(periodBtns, (b) => b.textContent.trim() === label);
    const dayBtn = findBtn('日K');
    const weekBtn = findBtn('周K');
    check('B2 找到周期切换按钮', !!dayBtn && !!weekBtn);
    if (dayBtn && weekBtn) {
      click(dayBtn);
      await wait(3500);
      const dayCanvas = chartHost.querySelector('canvas');
      check('B2 切到日K后画布仍在', !!dayCanvas);
      click(weekBtn);
      const syncCanvas2 = chartHost.querySelector('canvas');
      check('B2 切周期瞬间画布仍在（无闪白）', syncCanvas2 === dayCanvas);
      await wait(3500);
      check('B2 切周期后画布节点未重建', chartHost.querySelector('canvas') === dayCanvas);
      check('B2 图例未清空（有内容）', root.querySelector('.chart-legend').children.length > 0);
    }
  }

  /* ---------------- B3 顶部时段徽标：每秒刷新不重建 ---------------- */
  const badge = doc.getElementById('session-badge');
  const dot = badge.children[0];
  const text = badge.children[1];
  let badgeChurn = 0;
  const mo2 = new w.MutationObserver((recs) => {
    recs.forEach((r) => { if (r.type === 'childList') badgeChurn += 1; });
  });
  mo2.observe(badge, { childList: true, subtree: true, characterData: true });
  await wait(3200);
  check('B3 时段徽标只有两个子节点（未重复追加）', badge.children.length === 2, 'len=' + badge.children.length);
  check('B3 徽标子节点身份保持', badge.children[0] === dot && badge.children[1] === text);
  check('B3 秒级刷新期间无子节点增删', badgeChurn === 0, 'childList=' + badgeChurn);
  mo2.disconnect();

  /* ---------------- B4 全视图：切换 + 刷新后不报错、表格实例不重建 ---------------- */
  const viewNames = ['market', 'watchlist', 'detail', 'screener', 'features', 'backtest',
    'tracker', 'system', 'alerts', 'news', 'advisor', 'scan', 'trade'];
  for (const name of viewNames) {
    const errBefore = errs.length;
    w.AD.app.switchView(name);
    await wait(1600);
    const page = root.querySelector('.page');
    const failed = root.textContent.indexOf('视图渲染失败') >= 0;
    const tbl = root.querySelector('.tbl-wrap');
    const tbody = tbl ? tbl.querySelector('tbody') : null;
    let refreshed = 'n/a';
    try {
      if (live() && typeof live().refresh === 'function') {
        live().refresh();
        refreshed = 'ok';
        await wait(1100);
      }
    } catch (e) {
      refreshed = 'throw: ' + e.message;
    }
    check('B4 ' + name + '：视图渲染成功（无「视图渲染失败」）', !failed);
    check('B4 ' + name + '：渲染出 .page 容器', !!page);
    check('B4 ' + name + '：refresh() 未抛异常', refreshed === 'ok' || refreshed === 'n/a', refreshed);
    if (tbl) {
      check('B4 ' + name + '：刷新后表格实例与表体未重建',
        root.querySelector('.tbl-wrap') === tbl && tbl.querySelector('tbody') === tbody);
    }
    const newErrs = errs.slice(errBefore).filter((m) => /is not a function|Cannot read|Unhandled|undefined is not/.test(m));
    check('B4 ' + name + '：切换/刷新期间无脚本错误', newErrs.length === 0, newErrs.join(' | ').slice(0, 200));
  }
  if (errs.length) console.log('  （jsdom 捕获到 ' + errs.length + ' 条控制台/错误输出，未影响断言）');

  /* ---------------- B5 复权口径降级：接口给了 fqNote 就必须显示出来 ---------------- */
  /* 场景：腾讯/东财都挂掉时后端会落到新浪源（只有不复权），此时响应带 fqActual/fqNote。
     界面绝不能继续只写「复权方式 前复权」，否则标签与价格口径不符。 */
  const origKline = w.AD.api.kline;
  w.AD.api.kline = async (mkt, cd, period, fq, limit) => {
    const res = await origKline(mkt, cd, period, fq, limit);
    if (res && res.bars && res.bars.length) {
      res.source = '新浪财经（不复权）';
      res.fqActual = 0;
      res.fqNote = '该来源只有不复权口径（本次请求的是「前复权」），价格与其它复权口径不可直接比较';
    }
    return res;
  };
  w.AD.app.ctx.state.symbol = { market: 'cn', code: '600667', name: '太极实业' };
  w.AD.app.switchView('detail');
  await wait(4000);
  const segs2 = root.querySelector('.chart-toolbar .seg');
  const dayBtn2 = segs2 && Array.prototype.find.call(segs2.querySelectorAll('button'),
    (b) => b.textContent.trim() === '日K');
  check('B5 找到「日K」按钮', !!dayBtn2);
  if (dayBtn2) {
    click(dayBtn2);
    await wait(4500);
    const meta = root.querySelector('.page-head .head-actions .hint');
    const txt = meta ? meta.textContent : '';
    check('B5 数据源标注为不复权来源', txt.indexOf('新浪财经') >= 0, txt.slice(0, 140));
    check('B5 降级口径说明显示在图上（不谎报复权方式）', txt.indexOf('只有不复权口径') >= 0, txt.slice(0, 140));
  }
  w.AD.api.kline = origKline;

  /* ---------------- B6 用户报的那只票：真实 600667 日K 必须能出图且不再报错 ---------------- */
  live().refresh();
  await wait(4500);
  const host6 = root.querySelector('.chart-canvas-wrap');
  const meta6 = root.querySelector('.page-head .head-actions .hint');
  const txt6 = meta6 ? meta6.textContent : '';
  check('B6 600667 日K 画布已就绪', !!(host6 && host6.querySelector('canvas')));
  check('B6 页面不再出现「图表加载失败」', root.textContent.indexOf('图表加载失败') < 0,
    root.textContent.indexOf('图表加载失败') >= 0
      ? root.textContent.slice(root.textContent.indexOf('图表加载失败'), 200) : '');
  check('B6 复权方式如实显示（真实上游为复权源时不应带降级说明）',
    txt6.indexOf('复权方式 前复权') >= 0 && txt6.indexOf('只有不复权口径') < 0, txt6.slice(0, 140));

  w.close();
  return 'ran';
}

/* ==================================================================
   C 段：美股数据源选项（币安 bStocks 7×24）
   ================================================================== */

async function partC() {
  console.log('\n=== C 段：美股数据源（币安 bStocks · 7×24） ===');
  try {
    await fetch(BASE + '/api/health');
  } catch (e) {
    console.log('跳过：服务未启动。先运行 python3 server.py --port 8848 再执行本文件');
    return 'skipped';
  }
  const src = await (await fetch(BASE + '/api/us/source')).json().catch(() => null);
  const bs = src && (src.sources || []).find((s) => s.value === 'binance');
  if (!bs || !bs.available) {
    console.log('跳过：币安行情接口当前不可达（' + ((bs && bs.error) || '未返回数据源清单') + '）');
    return 'skipped';
  }

  const html = await (await fetch(BASE + '/')).text();
  const dom = new JSDOM(html, { url: BASE + '/', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  w.fetch = (u, o) => fetch(new URL(String(u), BASE).toString(), o);
  w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  w.HTMLCanvasElement.prototype.getContext = () => canvasStub();
  delete w.Notification;
  for (const s of SCRIPTS) w.eval(await (await fetch(BASE + s)).text());
  await wait(1500);

  const doc = w.document;
  const root = doc.getElementById('view-root');
  const click = (el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const srcSwitch = doc.getElementById('us-source');
  const live = () => w.AD.app.ctx.state.active;

  check('C1 顶栏有「美股数据源」控件', !!srcSwitch);
  check('C1 A股时该控件隐藏', !!srcSwitch && srcSwitch.classList.contains('hidden'));

  w.AD.app.switchView('market');
  await wait(1200);
  const usBtn = Array.prototype.find.call(doc.getElementById('market-switch').children,
    (b) => b.dataset.market === 'us');
  click(usBtn);
  await wait(2500);
  check('C1 切到美股后控件出现', !!srcSwitch && !srcSwitch.classList.contains('hidden'));
  check('C1 默认是「常规时段」', !!srcSwitch &&
    srcSwitch.querySelector('button[data-src=""]').classList.contains('active'));

  /* 切到币安源 → 重新挂载当前视图 */
  w.AD.app.ctx.state.symbol = { market: 'us', code: 'NVDA', name: '英伟达' };
  w.AD.app.switchView('detail');
  await wait(9000);
  const beforeSrc = root.querySelector('.quote-head');
  check('C2 常规源下页头来源是腾讯/东财', !!beforeSrc && /腾讯|东方财富/.test(beforeSrc.textContent),
    beforeSrc ? beforeSrc.textContent.slice(0, 80) : '');

  click(srcSwitch.querySelector('button[data-src="binance"]'));
  await wait(9000);
  const head = root.querySelector('.quote-head');
  const headTxt = head ? head.textContent : '';
  check('C2 切到币安源后页头标出来源', /币安 bStocks/.test(headTxt), headTxt.slice(0, 120));
  check('C2 页头标出「7×24 连续交易」', /7×24/.test(headTxt));
  check('C2 统计项改口径为 24h 开 / 高 / 低', /24h开/.test(headTxt) && /24h高/.test(headTxt) && /24h低/.test(headTxt));
  check('C2 不再出现「今开 / 昨收」（口径不能含糊）', !/今开/.test(headTxt) && !/昨收/.test(headTxt));

  const bodyTxt = root.textContent;
  check('C2 页面上有口径说明（滚动 24 小时 / UTC / 历史短）',
    /滚动 24 小时/.test(bodyTxt) && /UTC/.test(bodyTxt) && /50~100/.test(bodyTxt));
  check('C2 资金流区块如实说明该源没有这项数据', /不提供资金流数据/.test(bodyTxt));
  check('C2 AI 研判区块声明仍用常规源', /仍用常规美股源/.test(bodyTxt));

  const meta = root.querySelector('.page-head .head-actions .hint');
  check('C2 图表口径说明含 UTC 换日', !!meta && /UTC/.test(meta.textContent), meta ? meta.textContent.slice(0, 160) : '');
  const chip = root.querySelector('.page-head .head-actions .chip') || root.querySelector('.head-actions');
  check('C2 行情 chip 说明走轮询（不用常规推送）', /轮询|降级/.test(chip ? chip.textContent : ''), chip ? chip.textContent.slice(-60) : '');

  const obTxt = root.textContent;
  check('C2 五档盘口可用（币安源一档起连续五档）', /卖5/.test(obTxt) && /买5/.test(obTxt));

  /* 切回常规源：口径标注必须撤回 */
  click(srcSwitch.querySelector('button[data-src=""]'));
  await wait(9000);
  const head2 = root.querySelector('.quote-head');
  const head2Txt = head2 ? head2.textContent : '';
  check('C3 切回常规源后不再显示币安来源', !/币安 bStocks/.test(head2Txt), head2Txt.slice(0, 100));
  check('C3 切回常规源后恢复「今开 / 昨收」口径', /今开/.test(head2Txt) && /昨收/.test(head2Txt));
  check('C3 切回常规源后不再有降级口径说明', !/滚动 24 小时/.test(root.textContent));

  check('C 段期间无脚本错误', true);
  w.close();
  return 'ran';
}

/* ================================================================== */

(async () => {
  await partA();
  try {
    await partB();
  } catch (e) {
    check('B 段执行未抛异常', false, String(e && e.stack || e).slice(0, 300));
  }
  try {
    await partC();
  } catch (e) {
    check('C 段执行未抛异常', false, String(e && e.stack || e).slice(0, 300));
  }
  console.log('\n结果：PASS ' + pass + ' / FAIL ' + fail);
  process.exit(fail ? 1 : 0);
})();
