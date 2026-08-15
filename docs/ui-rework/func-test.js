// 功能回归: node func-test.js — 点击驱动验证全部交互路径
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

  // 1. 项目列表加载 + 自动选中
  check('项目列表渲染 3 个', await page.locator('.proj-card').count() === 3);
  check('自动选中第一个', await page.locator('.proj-card.active').count() === 1);

  // 2. 图渲染
  const n0 = await page.evaluate(() => window.__astra_cy ? window.__astra_cy.nodes().length : 0);
  check('星图节点已渲染 (>0)', n0 > 0);

  // 3. 节点点击 → 详情
  await page.evaluate(() => { window.__astra_cy.$('node[nodeId="f001"]').emit('tap'); });
  await page.waitForTimeout(300);
  check('节点详情显示编号 f001', (await page.locator('#right .body').innerText()).includes('f001'));
  check('详情含定位按钮', await page.locator('text=在星图中定位').count() === 1);
  await page.locator('text=在星图中定位').click();
  await page.waitForTimeout(400);
  check('定位后无报错', errors.length === 0);

  // 4. 点空白取消选中
  await page.evaluate(() => { window.__astra_cy.emit('tap'); });
  await page.waitForTimeout(300);
  check('取消选中回到星域详情', (await page.locator('#right .body').innerText()).includes('归航'));

  // 5. 图工具栏
  const z0 = await page.evaluate(() => window.__astra_cy.zoom());
  await page.locator('.graph-tools .icon-btn').nth(0).click(); await page.waitForTimeout(150);
  const z1 = await page.evaluate(() => window.__astra_cy.zoom());
  check('放大按钮生效', z1 > z0);
  await page.locator('.graph-tools .icon-btn').nth(1).click(); await page.waitForTimeout(150);
  await page.locator('.graph-tools .icon-btn').nth(2).click(); await page.waitForTimeout(350);
  await page.locator('.graph-tools .icon-btn').nth(3).click(); await page.waitForTimeout(450);
  check('缩放/适应/重排无报错', errors.length === 0);

  // 6. tabs 切换 (数量与 API 对齐, 避免历史数据污染)
  const hintTotal = await page.evaluate(async () => {
    const list = await (await fetch('/projects')).json();
    const r = await fetch('/projects/' + list[0].id); return (await r.json()).hints.length;
  });
  await page.locator('#right .tabs button').nth(1).click(); await page.waitForTimeout(200);
  check(`审查轨迹条数 = API hints (${hintTotal})`, await page.locator('.tl-item').count() === hintTotal);
  await page.locator('#right .tabs button').nth(2).click(); await page.waitForTimeout(200);
  check('指引卡数量 = API hints', await page.locator('.hint-card').count() === hintTotal);
  check('审查否决徽章渲染', await page.locator('.hint-tag:visible').count() === 2);
  await page.locator('#right .tabs button').nth(0).click();

  // 7. 模态: 新建星域 开/Esc关
  await page.locator('header .btn').nth(3).click(); await page.waitForTimeout(300);
  check('新建模态打开', await page.locator('.modal').count() === 1);
  await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  check('Esc 关闭模态', await page.locator('.modal').count() === 0);

  // 8. 模态: 声明航向校验 (不选星记 → 错误 toast)
  await page.locator('text=声明航向').click(); await page.waitForTimeout(250);
  await page.locator('.modal .foot .btn-accent').click(); await page.waitForTimeout(250);
  check('未选星记 → 错误提示', (await page.locator('#toasts').innerText()).includes('至少选择'));
  check('模态未误关', await page.locator('.modal').count() === 1);
  // 选中一个 chip 后声明
  await page.locator('.chip').first().click();
  await page.locator('.modal textarea').fill('UI 回归测试航向');
  await page.locator('.modal .foot .btn-accent').click(); await page.waitForTimeout(800);
  const n1 = await page.evaluate(() => window.__astra_cy.nodes().length);
  check('声明航向后节点 +1', n1 === n0 + 1);

  // 9. 归航该新航向 (动态找未归航的航向)
  await page.evaluate(() => {
    const cy = window.__astra_cy;
    const n = cy.nodes().filter(n => n.data('nodeType') === 'intent' && !n.data('to'))[0];
    n.emit('tap');
  });
  await page.waitForTimeout(300);
  await page.locator('text=归航此航向').click(); await page.waitForTimeout(250);
  await page.locator('.modal textarea').fill('UI 回归结论星记 flag{ui-regression-ok}');
  await page.locator('.modal .foot .btn-accent').click(); await page.waitForTimeout(800);
  check('归航写回后星记增加', await page.evaluate(async () => {
    const list = await (await fetch('/projects')).json(); const r = await fetch('/projects/' + list[0].id); const p = await r.json();
    return p.facts.some(f => (f.description||'').includes('ui-regression-ok'));
  }));

  // 10. 添加指引 (先取消节点选中, 回到星域详情)
  await page.evaluate(() => { window.__astra_cy.emit('tap'); });
  await page.waitForTimeout(300);
  await page.locator('.action-col >> text=添加指引').click(); await page.waitForTimeout(250);
  await page.locator('.modal textarea').fill('UI 回归指引');
  await page.locator('.modal .foot .btn-accent').click(); await page.waitForTimeout(800);
  check('指引已添加', await page.evaluate(async () => {
    const list = await (await fetch('/projects')).json(); const r = await fetch('/projects/' + list[0].id); const p = await r.json();
    return p.hints.some(h => (h.content||'').includes('UI 回归指引'));
  }));

  // 11. 删除模态 → 取消
  await page.locator('text=删除星域').click(); await page.waitForTimeout(250);
  check('删除确认模态', (await page.locator('.modal').innerText()).includes('不可恢复'));
  await page.locator('.modal .foot .btn').first().click(); await page.waitForTimeout(200);
  check('取消删除', await page.locator('.modal').count() === 0);

  // 12. 设置
  await page.locator('header .btn').nth(1).click(); await page.waitForTimeout(400);
  check('设置模态打开', (await page.locator('.modal').innerText()).includes('航向超时'));
  await page.locator('.modal .foot .btn-accent').click(); await page.waitForTimeout(400);
  check('设置保存后关闭', await page.locator('.modal').count() === 0);

  // 13. 切换项目 (空图 zoom clamp) — 读 zoom 前确保渲染完成
  await page.locator('.proj-card').nth(2).click();
  await page.waitForFunction(() => window.__astra_cy && window.__astra_cy.nodes().length === 2, null, { timeout: 5000 });
  await page.waitForTimeout(300);
  const z2 = await page.evaluate(() => window.__astra_cy.zoom());
  console.log('  (zoom =', z2.toFixed(3) + ')');
  check('稀疏图 zoom 受控 (<=1.2)', z2 <= 1.2);

  // 14. 停航/激活 (按当前状态点击存在的开关, 验证翻转后复位)
  const st0 = await page.evaluate(async () => {
    const list = await (await fetch('/projects')).json(); const r = await fetch('/projects/' + list[2].id); return (await r.json()).project.status;
  });
  const flipLabel = st0 === 'active' ? '停航' : '激活星域';
  const backLabel = st0 === 'active' ? '激活星域' : '停航';
  await page.locator(`.action-col >> text=${flipLabel}`).click(); await page.waitForTimeout(700);
  const st1 = await page.evaluate(async () => {
    const list = await (await fetch('/projects')).json(); const r = await fetch('/projects/' + list[2].id); return (await r.json()).project.status;
  });
  check(`状态翻转 ${st0} -> ${st1}`, st1 !== st0);
  await page.locator(`.action-col >> text=${backLabel}`).click(); await page.waitForTimeout(700);
  const st2 = await page.evaluate(async () => {
    const list = await (await fetch('/projects')).json(); const r = await fetch('/projects/' + list[2].id); return (await r.json()).project.status;
  });
  check('状态复位', st2 === st0);

  check('全程无 JS 错误', errors.length === 0);
  if (errors.length) console.log('  errors:', errors.slice(0, 5));

  console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
