#!/usr/bin/env python3
"""DeepSeek Pro 激活检测：按 we/let 判据扫描 dsh 会话推理文本。

判据（社区实证）：Pro 思考人格激活后，推理文本以 "We ..." 自称为主；
未激活则高频出现 "Let's/Let me ..."。（本地 R5 会话实测：标准 persona 下
we=0/let=9，符合未激活预期。）

dsh 会话格式：~/.dsh/sessions/<工作区>/session-<id>/session.jsonl.zstd，
多帧 zstd（需流式解压），推理文本在 type=reasoning-chunks 事件的
data.texts（token 分片数组）。

用法：
  python tools/check_pro_activation.py                 # 扫描全部会话汇总
  python tools/check_pro_activation.py --recent 5      # 只看最近 5 个会话
  python tools/check_pro_activation.py --json          # 机器可读输出

依赖：pip install zstandard
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DSH_SESSIONS = Path.home() / ".dsh" / "sessions"
WE_RE = re.compile(r"\bWe\b")
LET_RE = re.compile(r"\bLet(?:'s| me| us)?\b")


def read_session_text(path: Path) -> str:
    import zstandard

    with path.open("rb") as fh:
        raw = zstandard.ZstdDecompressor().stream_reader(fh).read()
    return raw.decode("utf-8", errors="replace")


def reasoning_text_of(raw: str) -> str:
    """拼接 reasoning-chunks 事件的 texts 分片（只取推理文本，不看正文）。"""
    chunks: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "reasoning-chunks":
            texts = (obj.get("data") or {}).get("texts")
            if isinstance(texts, list):
                chunks.extend(t for t in texts if isinstance(t, str))
    return "".join(chunks)


def scan_session(path: Path) -> dict:
    text = reasoning_text_of(read_session_text(path))
    we = len(WE_RE.findall(text))
    let = len(LET_RE.findall(text))
    return {"we": we, "let": let, "total": we + let, "chars": len(text)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recent", type=int, default=0, help="只扫描最近 N 个会话")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    files = sorted(DSH_SESSIONS.glob("*/*/session.jsonl.zstd"), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"未找到会话文件：{DSH_SESSIONS}")
        return 1
    if args.recent:
        files = files[-args.recent:]

    try:
        import zstandard  # noqa: F401
    except ImportError:
        print("缺少依赖：pip install zstandard")
        return 1

    results: dict[str, dict] = {}
    for f in files:
        try:
            results[f.parent.name] = scan_session(f)
        except Exception as exc:  # noqa: BLE001
            results[f.parent.name] = {"error": str(exc)}

    with_text = {k: v for k, v in results.items() if v.get("total", 0) > 0}
    tot_we = sum(v["we"] for v in with_text.values())
    tot_let = sum(v["let"] for v in with_text.values())
    activated = sum(1 for v in with_text.values() if v["we"] >= max(3, v["let"]))

    if args.json:
        print(json.dumps({
            "sessions": len(files),
            "with_reasoning_text": len(with_text),
            "we": tot_we, "let": tot_let,
            "activated_sessions": activated,
            "per_session": results,
        }, ensure_ascii=False, indent=1))
        return 0

    total = tot_we + tot_let
    ratio = tot_we / total if total else 0
    verdict = (
        "✅ Pro 已激活（we 主导）" if total >= 10 and ratio >= 0.6
        else "❌ 未激活（let 主导）" if total >= 10 and ratio <= 0.4
        else "⚠️ 信号不足或混合（多跑几轮再判）" if total >= 10
        else "⚠️ 推理文本不足（<10 处），多跑几轮再判"
    )
    print(f"扫描会话：{len(files)}（含推理文本 {len(with_text)} 个）")
    print(f"we 出现：{tot_we} 次 ｜ let 出现：{tot_let} 次 ｜ we 占比：{ratio:.0%}")
    print(f"we≥let 的会话：{activated}/{len(with_text)}")
    print(f"判定：{verdict}")
    if with_text:
        print("\n最近会话明细：")
        for k, v in sorted(with_text.items(), key=lambda kv: kv[1]["we"] - kv[1]["let"], reverse=True)[:5]:
            print(f"  {k}: we={v['we']} let={v['let']} (推理 {v['chars']} 字符)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
