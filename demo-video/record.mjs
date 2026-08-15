// 自动录制演示页视频：按 astra-narration.srt 的时间轴推进 step，
// 产出 1920x1080 webm，随后由 ffmpeg 烧录字幕。
// 用法：node record.mjs            （18 步版，与 2:05 字幕对应）
import { chromium } from '../astra/node_modules/playwright-core/index.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

// 解析 SRT → [{start, end}]
const srt = readFileSync(join(here, 'astra-narration.srt'), 'utf-8').replace(/^\ufeff/, '');
const toMs = (s) => {
  const m = s.match(/(\d+):(\d+):(\d+),(\d+)/);
  return (+m[1] * 3600 + +m[2] * 60 + +m[3]) * 1000 + +m[4];
};
const cues = [...srt.matchAll(/(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})/g)]
  .map((m) => ({ start: toMs(m[1]), end: toMs(m[2]) }));
console.log(`cues: ${cues.length}, last end: ${(cues.at(-1).end / 1000).toFixed(1)}s`);

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const tCreate = Date.now(); // 视频录制起点（context 创建后近似 t=0）
const context = await browser.newContext({
  viewport: { width: 2080, height: 1280 },
  recordVideo: { dir: join(here, 'rec'), size: { width: 2080, height: 1280 } },
});
const page = await context.newPage();
await page.goto('http://localhost:5173', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2200); // 等动画起势（含 HMR 后首帧编译）

const t0 = Date.now();
// cue k 的画面 = step k-1（cue1 对应已在屏的 step 0）。
// 在每条 cue 的 start 时刻推进（第一条除外），并按各 cue end 补齐总时长。
for (let k = 1; k < cues.length; k++) {
  const wait = cues[k].start - (Date.now() - t0);
  if (wait > 0) await page.waitForTimeout(wait);
  await page.keyboard.press('ArrowRight');
}
const tail = cues.at(-1).end + 600 - (Date.now() - t0);
if (tail > 0) await page.waitForTimeout(tail);

await context.close(); // 落盘视频
await browser.close();
writeFileSync(join(here, 'rec', 'timeline.json'), JSON.stringify({ preroll: t0 - tCreate, cues }));
console.log(`recording done (preroll=${t0 - tCreate}ms)`);
