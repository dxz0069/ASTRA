// 截图工具：node shot.js <out-prefix> [scenario]
// 依赖 astra/node_modules/playwright-core + 系统 Edge
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', '..', 'astra', 'node_modules', 'playwright-core'));

const BASE = 'http://127.0.0.1:8321';
const OUT = __dirname;

(async () => {
  const prefix = process.argv[2] || 'shot';
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 }, deviceScaleFactor: 1 });
  page.on('console', m => { if (m.type() === 'error') console.log('[console.error]', m.text()); });
  page.on('pageerror', e => console.log('[pageerror]', e.message));

  await page.goto(BASE + '/static/index.html', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1800);
  await page.screenshot({ path: path.join(OUT, `${prefix}-1-main.png`) });

  // 选中一个图节点看详情
  await page.evaluate(() => { const cy = window.__astra_cy; if (cy) { const n = cy.$('node[nodeId="f001"]'); if (n.length) n.emit('tap'); } });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, `${prefix}-2-node-detail.png`) });

  // 审查轨迹 tab
  await page.evaluate(() => { document.querySelectorAll('#right .tabs button')[1].click(); });
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(OUT, `${prefix}-3-trace.png`) });

  // 指引 tab
  await page.evaluate(() => { document.querySelectorAll('#right .tabs button')[2].click(); });
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(OUT, `${prefix}-4-guide.png`) });

  // 新建星域模态
  await page.evaluate(() => { document.querySelectorAll('header .btn')[3].click(); });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, `${prefix}-5-modal.png`) });
  await page.keyboard.press('Escape');

  // 空态：选中空项目
  await page.evaluate(() => { const btns = document.querySelectorAll('.proj-card'); btns[btns.length - 1].click(); });
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(OUT, `${prefix}-6-empty.png`) });

  await browser.close();
  console.log('screenshots done');
})().catch(e => { console.error(e); process.exit(1); });
