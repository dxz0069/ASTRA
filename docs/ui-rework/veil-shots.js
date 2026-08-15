// 蒙版四模式对比截图 — node veil-shots.js
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', '..', 'astra', 'node_modules', 'playwright-core'));
const BASE = 'http://127.0.0.1:8321';
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  await page.goto(BASE + '/static/index.html', { waitUntil: 'networkidle' });
  await page.evaluate(() => localStorage.removeItem('astra-ui-v1'));
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(2500); // 流体铺开后
  for (const mode of ['darken', 'frost', 'vignette', 'none']) {
    await page.evaluate(m => { window.__astra_apply_ui({ veilMode: m, veil: 0.55 }); }, mode);
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(__dirname, `veil-${mode}.png`) });
    console.log('shot', mode);
  }
  await page.evaluate(() => { window.__astra_apply_ui({ veilMode: 'darken', veil: 0.45 }); localStorage.removeItem('astra-ui-v1'); });
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
