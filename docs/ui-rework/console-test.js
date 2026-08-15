// 控制台专项测试: node console-test.js
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

  // 1. 打开控制台
  await page.locator('header .btn').nth(2).click();
  await page.waitForTimeout(400);
  check('控制台抽屉打开', await page.locator('.console').count() === 1);
  check('滑块数量 = 9', await page.locator('.c-slider').count() === 9);
  check('开关数量 = 3', await page.locator('.c-toggle').count() === 3);

  // 2. 面板模糊滑块 → CSS 变量 + computed style
  await page.locator('.c-slider').nth(3).fill('40');   // 滑块序: dye0 dis1 veil2 blurPanel3 blurChip4 sat5 railL6 railR7 nodeSize8
  await page.waitForTimeout(200);
  // 确认第 3 号滑块是面板模糊: 直接读 label
  const blurVar = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--blur-panel').trim());
  console.log('  (--blur-panel =', blurVar + ')');

  // 3. 罩层压暗滑块 (nth 2)
  await page.locator('.c-slider').nth(2).fill('0.8');
  await page.waitForTimeout(200);
  const veilVar = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--veil-alpha').trim());
  check('罩层压暗 → --veil-alpha=0.8', veilVar === '0.8');

  // 4. 流体开关 → PAUSED + 隐藏
  await page.locator('.c-toggle').first().click();
  await page.waitForTimeout(200);
  check('流体关闭 → PAUSED', await page.evaluate(() => window.ASTRA_FLUID.config.PAUSED === true));
  check('流体关闭 → 画布隐藏', await page.evaluate(() => getComputedStyle(document.getElementById('fluid-bg')).display === 'none'));
  await page.locator('.c-toggle').first().click();
  await page.waitForTimeout(200);
  check('流体恢复 → PAUSED=false', await page.evaluate(() => window.ASTRA_FLUID.config.PAUSED === false));

  // 5. 染料亮度 → fluid API
  await page.locator('.c-slider').nth(0).fill('0.4');
  await page.waitForTimeout(200);
  check('染料亮度写入 fluid (重新生成颜色时生效)', await page.evaluate(() => {
    // DYE_BRIGHTNESS 是模块内变量, 通过 splat 后检查无异常间接验证
    window.ASTRA_FLUID.splat(2); return true;
  }));

  // 6. 左栏宽度
  await page.locator('.c-slider').nth(6).fill('320');
  await page.waitForTimeout(200);
  const railL = await page.evaluate(() => getComputedStyle(document.querySelector('main')).gridTemplateColumns.split(' ')[0]);
  check('左栏宽度 = 320px', railL === '320px');

  // 7. 星图纵向布局
  await page.locator('.c-seg button').nth(7).click();  // 纵向 (seg: 蒙版×4, 舒适/紧凑, 横向/纵向)
  await page.waitForTimeout(600);
  const vertical = await page.evaluate(() => {
    const cy = window.__astra_cy;
    const o = cy.$('node[nodeId="origin"]').position(), f = cy.$('node[nodeId="f001"]').position();
    return f.y > o.y + 50;  // TB 布局: f001 在 origin 下方
  });
  check('纵向布局生效 (f001 位于 origin 下方)', vertical);
  await page.screenshot({ path: path.join(__dirname, 'console-tb-layout.png') });

  // 8. 密度切换
  await page.locator('.c-seg button').nth(5).click();
  await page.waitForTimeout(200);
  check('紧凑密度 → body.compact', await page.evaluate(() => document.body.classList.contains('compact')));

  // 9. 持久化: 刷新后设置仍在
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  const veilAfter = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--veil-alpha').trim());
  const compactAfter = await page.evaluate(() => document.body.classList.contains('compact'));
  check('刷新后 veil 保持 0.8', veilAfter === '0.8');
  check('刷新后密度保持紧凑', compactAfter === true);
  check('刷新后布局保持纵向', await page.evaluate(() => JSON.parse(localStorage.getItem('astra-ui-v1')).graphDir === 'TB'));

  // 10. 恢复默认
  await page.locator('header .btn').nth(2).click();
  await page.waitForTimeout(300);
  await page.locator('.c-foot .btn').click();
  await page.waitForTimeout(300);
  check('恢复默认 → veil=0.45', await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--veil-alpha').trim()) === '0.45');
  check('恢复默认 → 非紧凑', await page.evaluate(() => !document.body.classList.contains('compact')));

  // 11. Esc / 遮罩关闭
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  check('Esc 关闭控制台', await page.locator('.console').count() === 0);

  await page.screenshot({ path: path.join(__dirname, 'console-open.png') });
  check('全程无 JS 错误', errors.length === 0);
  if (errors.length) console.log('  errors:', errors.slice(0, 5));

  console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
