// 从各章 narrations.ts 生成 SRT 字幕（时间轴按 中文~4字/秒 估算，剪辑时微调）
// 用法：node make-srt.mjs [--end=125]  →  astra-narration.srt
//   --end=<秒>  把最后一条字幕的结束时间等比缩放到该时长（适配实拍视频长度）
import { readFileSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "presentation/src/chapters");
const chapters = ["00-cover", "01-coldopen", "02-stardust", "03-dualstar", "04-fleet", "05-closing"];

const cues = [];
for (const ch of chapters) {
  const src = readFileSync(join(root, ch, "narrations.ts"), "utf-8");
  const m = src.match(/=\s*\[([\s\S]*?)\];/);
  if (!m) throw new Error(`narrations not found in ${ch}`);
  const lines = [...m[1].matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((x) =>
    x[1].replace(/\\"/g, '"').replace(/\\n/g, "\n"),
  );
  for (const text of lines) if (text.trim()) cues.push(text);
}

const fmt = (ms) => {
  const t = new Date(ms).toISOString().slice(11, 23).replace(".", ",");
  return `00:${t.slice(3)}`; // 一小时内，HH 固定 00
};
// 字数（去标点空白）÷ 4 字/秒，最少 2.5s；句间留 350ms
const dur = (text) => {
  const n = text.replace(/[\s，。；：、—…？！""''（）\[\]{}]/g, "").length;
  return Math.max(2500, Math.ceil((n / 4) * 1000));
};
const splitLines = (text) => {
  if (text.length <= 22) return [text];
  const mid = Math.ceil(text.length / 2);
  const brk = [text.lastIndexOf("，", mid), text.lastIndexOf("。", mid), text.lastIndexOf("；", mid)]
    .filter((i) => i > 6)
    .sort((a, b) => b - a)[0];
  const cut = brk ?? mid;
  return [text.slice(0, cut + 1), text.slice(cut + 1).trim()].filter(Boolean);
};

let ms = 800; // 开场留 0.8s
const srt = [];
cues.forEach((text, i) => {
  const d = dur(text);
  srt.push({ start: ms, end: ms + d, text });
  ms += d + 350;
});
const rawTotal = ms - 350; // 最后一条的结束时间

// --end=<秒>：等比缩放全部时间戳，让最后一条在该秒结束
const endArg = process.argv.find((a) => a.startsWith("--end="));
if (endArg) {
  const target = parseFloat(endArg.slice(6)) * 1000;
  if (!(target > 0) || target >= rawTotal) throw new Error("--end 需小于原始总时长 " + (rawTotal / 1000).toFixed(1) + "s");
  const k = target / rawTotal;
  for (const c of srt) {
    c.start = Math.round(c.start * k);
    c.end = Math.round(c.end * k);
  }
  console.log(`scaled ${(rawTotal / 1000).toFixed(1)}s → ${(target / 1000).toFixed(1)}s (×${k.toFixed(3)})`);
}

writeFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "astra-narration.srt"),
  "\ufeff" + srt.map((c, i) => `${i + 1}\n${fmt(c.start)} --> ${fmt(c.end)}\n${splitLines(c.text).join("\n")}\n`).join("\n"),
  "utf-8",
);
console.log(`astra-narration.srt: ${cues.length} 条字幕，总时长 ${((srt.at(-1).end) / 1000).toFixed(1)}s`);
