/* 前端回归测试：策略跟踪「新建任务」表单状态同步（需要 jsdom）

准备：npm i -g jsdom（或在有 jsdom 的目录执行），并先启动服务：python3 server.py
运行：node tests/ui/create_tracker_sync.js

本用例覆盖用户实际遇到的故障：输入框里有标的，点「创建跟踪任务」却提示要填写。
/* 原始说明：策略跟踪「新建任务」表单状态同步

复现用户报的问题：输入框里有标的，点「创建跟踪任务」却提示要填写。
成因有三种，这里逐一验证：
  1) 输入框有值（程序化赋值 / 自动填充 / 拖拽粘贴），但 blur 未触发；
  2) macOS Safari 点击按钮不会让输入框失焦，只靠 blur 同步拿不到值；
  3) 创建成功后只清状态不重绘，输入框残留旧代码，再次点击拿不到值。
测试通过拦截 api.strategyCreate 捕获提交体，不写入任何真实任务。
*/
const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = 'http://127.0.0.1:8848';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const stub = () => {
  const n = () => {}; const g = { addColorStop: n };
  return { canvas: { width: 900, height: 400 }, setTransform: n, clearRect: n, save: n, restore: n,
    beginPath: n, closePath: n, moveTo: n, lineTo: n, arc: n, rect: n, fill: n, stroke: n,
    fillRect: n, strokeRect: n, clip: n, fillText: n, strokeText: n, setLineDash: n,
    translate: n, rotate: n, scale: n, measureText: () => ({ width: 34 }),
    createLinearGradient: () => g, createRadialGradient: () => g, drawImage: n, putImageData: n,
    font: '', textAlign: '', textBaseline: '', fillStyle: '', strokeStyle: '', lineWidth: 1, globalAlpha: 1 };
};

(async () => {
  const html = await (await fetch(BASE + '/')).text();
  const errs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errs.push(String(e && e.message)));
  vc.on('error', (...a) => errs.push(a.map(String).join(' ').slice(0, 200)));
  const dom = new JSDOM(html, { url: BASE + '/', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window;
  w.fetch = (u, o) => fetch(new URL(String(u), BASE).toString(), o);
  w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  w.HTMLCanvasElement.prototype.getContext = () => stub();
  w.AudioContext = class { constructor() { this.currentTime = 0; this.destination = {}; } createOscillator() { return { connect() {}, start() {}, stop() {}, frequency: { value: 0 }, type: '' }; } createGain() { return { connect() {}, gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } }; } close() {} };
  delete w.Notification;
  for (const s of ['/js/util.js', '/js/api.js', '/js/ui.js', '/js/indicators.js', '/js/chart.js',
    '/js/views/market.js', '/js/views/watchlist.js', '/js/views/detail.js', '/js/views/screener.js',
    '/js/views/features.js', '/js/views/backtest.js', '/js/views/tracker.js', '/js/views/system.js',
    '/js/views/alerts.js', '/js/views/news.js', '/js/app.js']) {
    w.eval(await (await fetch(BASE + s)).text());
  }
  await wait(2500);

  /* 拦截创建请求，记录提交体；用已存在任务的 id 让后续刷新正常 */
  const ov = await (await fetch(BASE + '/api/strategy/overview')).json();
  const existingId = (ov.rows[0] || {}).id;
  const captured = [];
  const realCreate = w.AD.api.strategyCreate;
  w.AD.api.strategyCreate = async (body) => {
    captured.push(JSON.parse(JSON.stringify(body)));
    return { ok: true, run: { id: existingId, code: body.code, name: body.name } };
  };

  const toasts = [];
  const origToast = w.AD.app.ctx.toast;
  w.AD.app.ctx.toast = (m, t) => { toasts.push({ msg: String(m), type: t || 'info' }); return origToast(m, t); };

  w.AD.app.switchView('tracker');
  await wait(8000);

  const d = w.document;
  const root = d.getElementById('view-root');
  const findBtn = () => Array.from(root.querySelectorAll('button')).find((b) => b.textContent.trim() === '创建跟踪任务');
  const findCodeInput = () => {
    const inputs = Array.from(root.querySelectorAll('input.inp'));
    return inputs.find((i) => (i.placeholder || '').indexOf('600519 / AAPL') >= 0);
  };
  const warnCount = () => toasts.filter((t) => t.msg.indexOf('请先填写标的代码') >= 0).length;
  const click = (el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));

  let pass = 0, fail = 0;
  const check = (name, ok, extra) => {
    console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (ok ? '' : '  ' + (extra || '')));
    ok ? pass++ : fail++;
  };

  check('找到「创建跟踪任务」按钮', !!findBtn());
  check('找到标的输入框', !!findCodeInput());

  /* 场景 1：程序化赋值（无 input / blur 事件），模拟自动填充或脚本赋值 */
  let inp = findCodeInput();
  inp.value = '600519';
  const w0 = warnCount();
  click(findBtn());
  await wait(4500);
  check('场景1 输入框有值但未触发 blur → 创建成功', captured.length === 1,
    '提交次数=' + captured.length + '，提示要填写=' + (warnCount() - w0));
  check('场景1 提交的代码正确', captured[0] && captured[0].code === '600519', JSON.stringify(captured[0] && captured[0].code));
  check('场景1 未再出现「请先填写标的代码」', warnCount() === w0, '多出 ' + (warnCount() - w0) + ' 次');

  /* 场景 2：创建成功后表单应重置，输入框不应残留旧值 */
  inp = findCodeInput();
  check('场景2 创建后输入框已清空', inp && inp.value === '', 'value=' + (inp && inp.value));
  const w1 = warnCount();
  click(findBtn());
  await wait(2500);
  check('场景2 空表单点击 → 正常提示需要填写（且未发请求）',
    warnCount() === w1 + 1 && captured.length === 1,
    '提示 ' + (warnCount() - w1) + ' 次，请求 ' + captured.length + ' 次');

  /* 场景 3：模拟 Safari 输入（先触发 input 再点击），确保走 input 同步路径 */
  inp = findCodeInput();
  inp.value = '000001';
  inp.dispatchEvent(new w.Event('input', { bubbles: true }));
  click(findBtn());
  await wait(4500);
  check('场景3 输入即同步 → 创建成功', captured.length === 2, '提交次数=' + captured.length);
  check('场景3 提交的代码正确', captured[1] && captured[1].code === '000001', JSON.stringify(captured[1] && captured[1].code));

  /* 场景 4：中文名称与成本假设是否保留 */
  const last = captured[captured.length - 1] || {};
  check('场景4 提交体名称已解析（非空）', !!last.name && last.name !== '—', 'name=' + last.name);
  check('场景4 手续费/滑点保留成本假设', Number(last.fee) > 0 && Number(last.slippage) >= 0,
    'fee=' + last.fee + ' slippage=' + last.slippage);
  check('场景4 未写入真实任务（仅拦截）', captured.length === 2, '提交次数=' + captured.length);

  console.log('\n运行时错误:', errs.length);
  errs.slice(0, 4).forEach((e) => console.log('  - ' + e));
  console.log('结果: ' + pass + ' 通过 / ' + fail + ' 失败');
  w.AD.api.strategyCreate = realCreate;
  process.exit(fail || errs.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
