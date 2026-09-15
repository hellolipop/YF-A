/* 前端回归测试：策略跟踪「新建任务」表单状态同步（需要 jsdom）

准备：npm i -g jsdom（或在有 jsdom 的目录执行），并先启动服务：python3 server.py --port 8848
运行：node tests/ui/create_tracker_sync.js

覆盖用户实际遇到的故障：输入框里有标的，点「创建跟踪任务」却提示要填写。
成因有三种，逐一验证：
  1) 输入框有值（程序化赋值 / 自动填充 / 拖拽粘贴），但 blur 未触发；
  2) macOS Safari 点击按钮不会让输入框失焦，只靠 blur 同步拿不到值；
  3) 创建成功后只清状态不重绘，输入框残留旧代码，再次点击拿不到值。
另有附带缺陷：预填名称与手动改动的代码不匹配时，旧名称被一起提交。
测试通过拦截 api.strategyCreate 捕获提交体，不写入任何真实任务。
*/
let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  console.log('跳过：未安装 jsdom。安装后重跑：npm i jsdom（或在已安装 jsdom 的目录执行本文件）');
  process.exit(0);
}
const BASE = process.env.AD_BASE || 'http://127.0.0.1:8848';
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
  try {
    await fetch(BASE + '/api/health');
  } catch (e) {
    console.log('跳过：服务未启动。先运行 python3 server.py --port 8848 再执行本文件');
    process.exit(0);
  }
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

  /* 模拟「从个股页跳转过来」：预填 600519 贵州茅台，再手动改成别的代码 */
  w.AD.app.ctx.state.symbol = { market: 'cn', code: '600519', name: '贵州茅台' };

  /* 拦截创建请求，记录提交体；沿用已存在任务的 id 让后续刷新正常 */
  const ov = await (await fetch(BASE + '/api/strategy/overview')).json();
  const existingId = (ov.rows[0] || {}).id;
  const captured = [];
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
  const findCode = () => Array.from(root.querySelectorAll('input.inp'))
    .find((i) => (i.placeholder || '').indexOf('600519 / AAPL') >= 0);
  const warnCount = () => toasts.filter((t) => t.msg.indexOf('请先填写标的代码') >= 0).length;
  const click = (el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));

  let pass = 0, fail = 0;
  const check = (name, ok, extra) => {
    console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (ok ? '' : '  ' + (extra || '')));
    ok ? pass++ : fail++;
  };

  check('找到「创建跟踪任务」按钮', !!findBtn());
  check('找到标的输入框', !!findCode());
  check('预填生效（输入框显示 600519）', findCode() && findCode().value === '600519', findCode() && findCode().value);

  /* 场景 1：预填名称与代码一致的直接提交 */
  click(findBtn());
  await wait(4500);
  check('场景1 预填直接提交 → 创建成功', captured.length === 1, '提交 ' + captured.length + ' 次');
  check('场景1 名称与代码一致', captured[0] && captured[0].code === '600519' && captured[0].name === '贵州茅台',
    JSON.stringify(captured[0] && { code: captured[0].code, name: captured[0].name }));

  /* 场景 2：创建后表单应重置（重新取节点，避免拿到被替换掉的旧节点） */
  check('场景2 创建后输入框已清空', findCode() && findCode().value === '', 'value=' + (findCode() && findCode().value));
  const w1 = warnCount();
  click(findBtn());
  await wait(2500);
  check('场景2 空表单点击 → 提示要填写且不发请求',
    warnCount() === w1 + 1 && captured.length === 1, '提示 ' + (warnCount() - w1) + '，请求 ' + captured.length);

  /* 场景 3：无 blur / 无 input 事件（自动填充、拖拽粘贴）也要能提交 */
  let inp = findCode();
  inp.value = '601398';
  const w2 = warnCount();
  click(findBtn());
  await wait(6000);
  check('场景3 输入框有值但无事件 → 创建成功', captured.length === 2,
    '提交 ' + captured.length + ' 次，提示要填写 ' + (warnCount() - w2));
  check('场景3 未再出现「请先填写标的代码」', warnCount() === w2);

  /* 场景 4：改代码后不能沿用旧名称 */
  const s3 = captured[1] || {};
  check('场景4 代码已改用新值', s3.code === '601398', 'code=' + s3.code);
  check('场景4 名称未沿用旧的「贵州茅台」', s3.name && s3.name !== '贵州茅台', 'name=' + s3.name);
  check('场景4 名称已重新解析', s3.name && /工商|601398/.test(s3.name), 'name=' + s3.name);

  /* 场景 5：输入即同步（模拟正常键入） */
  inp = findCode();
  inp.value = '000001';
  inp.dispatchEvent(new w.Event('input', { bubbles: true }));
  click(findBtn());
  await wait(6000);
  check('场景5 键入后提交成功', captured.length === 3, '提交 ' + captured.length + ' 次');
  check('场景5 提交代码正确', captured[2] && captured[2].code === '000001', captured[2] && captured[2].code);
  check('场景5 名称按新代码解析', captured[2] && captured[2].name !== '贵州茅台', captured[2] && captured[2].name);

  /* 场景 6：成本假设应保留，且未写入真实任务 */
  const last = captured[captured.length - 1] || {};
  check('场景6 手续费/滑点保留成本假设', Number(last.fee) > 0 && Number(last.slippage) >= 0,
    'fee=' + last.fee + ' slippage=' + last.slippage);
  const after = await (await fetch(BASE + '/api/strategy/overview')).json();
  check('场景6 未写入真实任务', after.rows.length === ov.rows.length, '任务数 ' + after.rows.length + ' vs ' + ov.rows.length);

  console.log('\n运行时错误:', errs.length);
  errs.slice(0, 4).forEach((e) => console.log('  - ' + e));
  console.log('结果: ' + pass + ' 通过 / ' + fail + ' 失败');
  process.exit(fail || errs.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
