/* 前端运行时审计：打开 8321 页面，收集 JS 错误 + 断言核心交互路径。
 * 用法： node scripts/audit_frontend.js   （依赖 astra/node_modules/playwright-core + 系统 Edge）
 */
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', 'astra', 'node_modules', 'playwright-core'));
const BASE = 'http://127.0.0.1:8321';
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('  PASS', name); }
  else { fail++; console.log('  FAIL', name, detail !== undefined ? ':: ' + detail : ''); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  const errors = [], consoleErrors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('requestfailed', r => { if (!r.url().includes('favicon')) consoleErrors.push('requestfailed: ' + r.url()); });

  await page.goto(BASE + '/static/index.html', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  console.log('== 基础 ==');
  check('标题', (await page.title()).includes('星图'));
  check('无 pageerror', errors.length === 0, errors.join(' | '));
  check('无 console.error', consoleErrors.length === 0, consoleErrors.join(' | '));

  console.log('== 星域列表 ==');
  const projCards = await page.locator('.proj-card').count();
  check('星域列表渲染 3 个', projCards === 3, 'got ' + projCards);

  console.log('== 星图渲染 ==');
  await page.waitForTimeout(1500); // 等待 cytoscape 布局
  const nodeCount = await page.evaluate(() => window.__astra_cy ? window.__astra_cy.nodes().length : -1);
  const edgeCount = await page.evaluate(() => window.__astra_cy ? window.__astra_cy.edges().length : -1);
  check('cytoscape 实例存在', nodeCount >= 0);
  check('星图节点 > 0', nodeCount > 0, 'nodes=' + nodeCount);
  check('星图边 > 0', edgeCount > 0, 'edges=' + edgeCount);
  const nodeKinds = await page.evaluate(() => {
    const s = new Set(); window.__astra_cy.nodes().forEach(n => s.add(n.data('nodeType'))); return [...s].sort().join(',');
  });
  check('节点类型齐全', nodeKinds.includes('origin') && nodeKinds.includes('fact') && nodeKinds.includes('intent'), nodeKinds);

  console.log('== 交互 ==');
  // 点击星记节点 → 详情
  await page.evaluate(() => {
    const fact = window.__astra_cy.nodes('[nodeType="fact"]').first();
    if (fact.length) fact.emit('tap');
  });
  await page.waitForTimeout(300);
  const detailText = await page.locator('#right .body').innerText().catch(() => '');
  check('节点详情面板出现', detailText.includes('内容') || detailText.includes('星记'));

  // 图内搜索
  await page.fill('.graph-search input', 'WAF');
  await page.waitForTimeout(300);
  const hitCount = await page.evaluate(() => window.__astra_cy ? window.__astra_cy.nodes('.hit').length : 0);
  check('图内搜索命中', hitCount > 0, 'hits=' + hitCount);
  await page.fill('.graph-search input', '');
  await page.waitForTimeout(200);

  // 控制台抽屉
  await page.click('header button[title="界面控制台"]');
  await page.waitForTimeout(300);
  const consoleVisible = await page.locator('.console').isVisible().catch(() => false);
  check('控制台抽屉打开', consoleVisible);
  await page.click('.console .c-head .icon-btn'); // 点关闭按钮（Escape 键盘事件在无焦点元素时不触发 Alpine handler）
  await page.waitForTimeout(400);
  const consoleGone = !(await page.locator('.console').isVisible().catch(() => true));
  check('控制台抽屉关闭', consoleGone);

  // Tab 切换：审查轨迹 / 指引
  await page.click('#right .tabs button:nth-child(2)');
  await page.waitForTimeout(200);
  const traceText = await page.locator('#right .body').innerText().catch(() => '');
  check('审查轨迹 tab 有内容', traceText.includes('WAF') || traceText.includes('审查') || traceText.includes('失败'), traceText.slice(0, 60));
  await page.click('#right .tabs button:nth-child(3)');
  await page.waitForTimeout(200);
  const guideText = await page.locator('#right .body').innerText().catch(() => '');
  check('指引 tab 有内容', guideText.includes('multipart') || guideText.includes('指引'), guideText.slice(0, 60));

  // 新建星域模态
  await page.click('header .btn-accent');
  await page.waitForTimeout(300);
  const modalVisible = await page.locator('.modal-mask').first().isVisible().catch(() => false);
  check('新建星域模态打开', modalVisible);
  await page.keyboard.press('Escape');

  // 新建项目 API 全链路（含 hints 数组）
  const created = await page.evaluate(async () => {
    const r = await fetch('/projects', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ title: 'audit-e2e-01', origin: 'http://t', goal: 'g', bootstrap_enabled: false, hints: [{content:'h1', creator:'human'}] }) });
    if (!r.ok) return 'HTTP ' + r.status;
    const p = await r.json(); return p.project ? p.project.id : 'no-project-field';
  });
  check('POST /projects 全链路', typeof created === 'string' && created.startsWith('proj_'), created);

  console.log('== 安全 ==');
  const html = await page.content();
  check('无内联事件注入面（x-on 白名单）', !/<script[^>]*>[^<]*fetch\(/.test(html));
  const hasVText = (await page.content()).includes('x-text');
  check('使用 x-text 渲染用户内容', hasVText);

  await browser.close();
  console.log(`\n结果: ${pass} PASS / ${fail} FAIL`);
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error('审计脚本崩溃:', e); process.exit(2); });
