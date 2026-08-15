// 图增量更新 + 图内搜索 专项: node graph-test.js
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', '..', 'astra', 'node_modules', 'playwright-core'));
const BASE = 'http://127.0.0.1:8321';
let pass = 0, fail = 0;
function check(name, cond){ if(cond){ pass++; console.log('  PASS', name); } else { fail++; console.log('  FAIL', name); } }

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(BASE + '/static/index.html', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // 1. 手动移动节点 → 等待 >5s 轮询 → 位置保持 (增量更新的核心验证)
  await page.evaluate(() => { window.__astra_cy.$('node[nodeId="f001"]').position({ x: 500, y: 500 }); });
  await page.waitForTimeout(6200);  // 跨过一次 5s 轮询
  const pos = await page.evaluate(() => window.__astra_cy.$('node[nodeId="f001"]').position());
  check(`轮询后拖动位置保持 (x=${pos.x.toFixed(0)}≈500)`, Math.abs(pos.x - 500) < 1 && Math.abs(pos.y - 500) < 1);

  // 2. 结构变化 (新增航向) 仍触发重排
  const n0 = await page.evaluate(() => window.__astra_cy.nodes().length);
  await page.evaluate(async () => {
    const list = await (await fetch('/projects')).json();
    await fetch('/projects/' + list[0].id + '/intents', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: ['f001'], description: '增量更新测试航向', creator: 'human', worker: null }) });
  });
  await page.waitForTimeout(6200);  // 等轮询拉取
  const n1 = await page.evaluate(() => window.__astra_cy.nodes().length);
  check('新增航向经轮询自动出现 (+1)', n1 === n0 + 1);

  // 3. 图内搜索: 关键词命中/压暗/计数
  await page.locator('.graph-search input').fill('WAF');
  await page.waitForTimeout(300);
  const search = await page.evaluate(() => {
    const cy = window.__astra_cy;
    return { hits: cy.$('node.hit').length, dims: cy.$('node.dim').length, total: cy.nodes().length };
  });
  console.log('  (hit=' + search.hits + ' dim=' + search.dims + ' total=' + search.total + ')');
  check('WAF 命中 ≥1 节点', search.hits >= 1);
  check('未命中节点被压暗', search.dims === search.total - search.hits);
  check('命中计数显示', (await page.locator('.gs-count').innerText()).includes(search.hits + ' 命中'));

  // 4. 回车定位到首个命中
  await page.locator('.graph-search input').press('Enter');
  await page.waitForTimeout(500);
  check('回车后详情面板显示命中节点', (await page.locator('#right .body').innerText()).includes('WAF'));

  // 5. 清空搜索恢复
  await page.locator('.graph-search input').press('Escape');
  await page.waitForTimeout(300);
  check('Esc 清空后无压暗节点', await page.evaluate(() => window.__astra_cy.$('node.dim').length === 0));

  // 6. 搜索状态下轮询不丢高亮 (跨一次轮询)
  await page.locator('.graph-search input').fill('multipart');
  await page.waitForTimeout(6200);
  check('轮询后搜索高亮仍在', await page.evaluate(() => window.__astra_cy.$('node.hit').length >= 1));
  await page.locator('.graph-search input').press('Escape');

  await page.screenshot({ path: path.join(__dirname, 'graph-search.png') });
  check('全程无 JS 错误', errors.length === 0);
  if (errors.length) console.log('  errors:', errors.slice(0, 5));
  console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
