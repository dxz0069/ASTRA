// 控制台全量审计: 逐项验证每个控制都真实生效 — node audit-test.js
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
  await page.evaluate(() => localStorage.removeItem('astra-ui-v1'));
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  await page.locator('header .btn').nth(2).click();
  await page.waitForTimeout(400);
  check('控制台打开', await page.locator('.console').count() === 1);

  // ==== 流体背景 ====
  // 1 流体模拟开关
  await page.locator('.c-toggle').nth(0).click(); await page.waitForTimeout(150);
  check('[1] 流体模拟关 → PAUSED+画布隐藏',
    await page.evaluate(() => window.ASTRA_FLUID.config.PAUSED && getComputedStyle(document.getElementById('fluid-bg')).display === 'none'));
  await page.locator('.c-toggle').nth(0).click(); await page.waitForTimeout(150);

  // 2 环境流动开关
  await page.locator('.c-toggle').nth(1).click(); await page.waitForTimeout(150);
  check('[2] 环境流动关 → isAmbient()=false', await page.evaluate(() => window.ASTRA_FLUID.isAmbient() === false));
  await page.locator('.c-toggle').nth(1).click(); await page.waitForTimeout(150);
  check('[2] 环境流动恢复 → isAmbient()=true', await page.evaluate(() => window.ASTRA_FLUID.isAmbient() === true));

  // 3 染料亮度 (slider 0)
  await page.locator('.c-slider').nth(0).fill('0.42'); await page.waitForTimeout(150);
  check('[3] 染料亮度 → getDye()=0.42', await page.evaluate(() => Math.abs(window.ASTRA_FLUID.getDye() - 0.42) < 0.001));

  // 4 消散速度 (slider 1)
  await page.locator('.c-slider').nth(1).fill('1.5'); await page.waitForTimeout(150);
  check('[4] 消散速度 → config.DENSITY_DISSIPATION=1.5',
    await page.evaluate(() => Math.abs(window.ASTRA_FLUID.config.DENSITY_DISSIPATION - 1.5) < 0.001));

  // 5 蒙版效果四模式 (seg 组1: 压暗0 柔焦1 晕影2 无3)
  const veilStyles = async () => page.evaluate(() => {
    const el = document.getElementById('fluid-veil'); const cs = getComputedStyle(el);
    return { display: cs.display, bg: cs.backgroundImage, bf: cs.backdropFilter || cs.webkitBackdropFilter, bc: cs.backgroundColor };
  });
  await page.locator('.c-slider').nth(2).fill('0.8'); await page.waitForTimeout(120); // 强度拉满便于观察
  let st = await veilStyles();
  check('[5a] 压暗(默认) → 深色罩 0.8 alpha', st.bc.includes('0.8') && st.bf === 'none' && st.display !== 'none');
  await page.locator('.c-seg button').nth(1).click(); await page.waitForTimeout(200);
  st = await veilStyles();
  check('[5b] 柔焦 → backdrop blur>0', /blur\((\d+\.?\d*)px\)/.test(st.bf) && parseFloat(st.bf.match(/blur\((\d+\.?\d*)px\)/)[1]) > 10);
  await page.locator('.c-seg button').nth(2).click(); await page.waitForTimeout(200);
  st = await veilStyles();
  check('[5c] 晕影 → 径向渐变罩', st.bg.includes('radial-gradient'));
  await page.locator('.c-seg button').nth(3).click(); await page.waitForTimeout(200);
  st = await veilStyles();
  check('[5d] 无蒙版 → display:none', st.display === 'none');
  await page.screenshot({ path: path.join(__dirname, 'audit-veil-none.png') });
  await page.locator('.c-seg button').nth(0).click(); await page.waitForTimeout(150);

  // 6 蒙版强度 (slider 2) — 上面已填 0.8, 验证变量
  check('[6] 蒙版强度 → --veil-alpha=0.8',
    await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--veil-alpha').trim() === '0.8'));

  // 7 迸发按钮
  await page.locator('.c-row .icon-btn').first().click(); await page.waitForTimeout(200);
  check('[7] 迸发按钮无异常', errors.length === 0);

  // ==== 液态玻璃 ====
  // 8 面板模糊 (slider 3) → aside#right computed
  await page.locator('.c-slider').nth(3).fill('40'); await page.waitForTimeout(200);
  check('[8] 面板模糊 40px → 右栏 computed blur(40px)', await page.evaluate(() => {
    const bf = getComputedStyle(document.querySelector('aside#right')).backdropFilter;
    return bf.includes('blur(40px)');
  }));

  // 9 小件模糊 (slider 4) → refract 按钮走变量
  await page.locator('.c-slider').nth(4).fill('20'); await page.waitForTimeout(200);
  check('[9] 小件模糊 20px → 顶栏按钮 computed blur(20px)', await page.evaluate(() => {
    const bf = getComputedStyle(document.querySelector('header .btn.glass-chip')).backdropFilter;
    return bf.includes('blur(20px)');
  }));

  // 10 色彩饱和 (slider 5)
  await page.locator('.c-slider').nth(5).fill('190'); await page.waitForTimeout(200);
  check('[10] 色彩饱和 190% → computed saturate(1.9)', await page.evaluate(() => {
    const bf = getComputedStyle(document.querySelector('aside#right')).backdropFilter;
    return bf.includes('saturate(1.9)');
  }));

  // ==== 布局 ====
  // 11 左栏宽度 (slider 6)
  await page.locator('.c-slider').nth(6).fill('320'); await page.waitForTimeout(150);
  check('[11] 左栏 320px', await page.evaluate(() =>
    getComputedStyle(document.querySelector('main')).gridTemplateColumns.split(' ')[0] === '320px'));

  // 12 右栏宽度 (slider 7)
  await page.locator('.c-slider').nth(7).fill('400'); await page.waitForTimeout(150);
  check('[12] 右栏 400px', await page.evaluate(() =>
    getComputedStyle(document.querySelector('main')).gridTemplateColumns.split(' ')[2] === '400px'));

  // 13 界面密度 (seg 组2: 舒适4 紧凑5)
  await page.locator('.c-seg button').nth(5).click(); await page.waitForTimeout(150);
  check('[13] 紧凑密度 → body.compact + 字号 12.5px', await page.evaluate(() =>
    document.body.classList.contains('compact') && getComputedStyle(document.body).fontSize === '12.5px'));
  await page.locator('.c-seg button').nth(4).click(); await page.waitForTimeout(150);

  // ==== 星图 ====
  // 14 布局方向 (seg 组3: 横向6 纵向7)
  await page.locator('.c-seg button').nth(7).click(); await page.waitForTimeout(600);
  check('[14] 纵向布局 → f001 在 origin 下方', await page.evaluate(() => {
    const cy = window.__astra_cy;
    return cy.$('node[nodeId="f001"]').position().y > cy.$('node[nodeId="origin"]').position().y + 50;
  }));
  await page.locator('.c-seg button').nth(6).click(); await page.waitForTimeout(600);

  // 15 节点大小 (slider 8)
  await page.locator('.c-slider').nth(8).fill('32'); await page.waitForTimeout(200);
  check('[15] 节点大小 32px → cy 节点 width=32', await page.evaluate(() =>
    Math.abs(window.__astra_cy.$('node[nodeId="f001"]').width() - 32) < 1));

  // ==== 动效 ====
  // 16 界面动效 (toggle 3)
  await page.locator('.c-toggle').nth(2).click(); await page.waitForTimeout(150);
  check('[16] 动效关 → body.no-motion', await page.evaluate(() => document.body.classList.contains('no-motion')));
  await page.locator('.c-toggle').nth(2).click(); await page.waitForTimeout(150);

  // ==== 持久化 ====
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(1300);
  const persisted = await page.evaluate(() => ({
    v: JSON.parse(localStorage.getItem('astra-ui-v1')),
    blur: getComputedStyle(document.querySelector('aside#right')).backdropFilter,
  }));
  check('[17] 持久化: dye=0.42', Math.abs(persisted.v.dye - 0.42) < 0.001);
  check('[17] 持久化: veilMode 存取', persisted.v.veilMode === 'darken');
  check('[17] 持久化: 面板模糊 40px 生效', persisted.blur.includes('blur(40px)'));
  check('[17] 持久化: 节点大小 32 存取', persisted.v.nodeSize === 32);

  // ==== 恢复默认 ====
  await page.locator('header .btn').nth(2).click(); await page.waitForTimeout(300);
  await page.locator('.c-foot .btn').click(); await page.waitForTimeout(300);
  check('[18] 恢复默认 → 模糊 22px', await page.evaluate(() =>
    getComputedStyle(document.querySelector('aside#right')).backdropFilter.includes('blur(22px)')));
  check('[18] 恢复默认 → veil 0.45', await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue('--veil-alpha').trim() === '0.45'));

  check('全程无 JS 错误', errors.length === 0);
  if (errors.length) console.log('  errors:', errors.slice(0, 5));
  console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
