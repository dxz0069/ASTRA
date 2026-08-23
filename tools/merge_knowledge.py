#!/usr/bin/env python3
"""L4 记忆自动固化：把赛后沉淀文件合并进知识库 challenge-approaches.md。

闭环：解出题 → runner 自动沉淀（脱敏）到 /tmp/astra-knowledge-append.json →
本脚本去重合并进 container/knowledge/challenge-approaches.md → 下轮开局自动注入。
把原先"赛后人工读日志合并"压缩为一条命令，去重规则：题码已存在则跳过（保留首条）。

用法：
  python tools/merge_knowledge.py [--input /tmp/astra-knowledge-append.json] \
      [--kb container/knowledge/challenge-approaches.md] [--dry-run] [--mark-merged]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_INPUT = Path("/tmp/astra-knowledge-append.json") if sys.platform != "win32" else Path(
    __import__("tempfile").gettempdir()
) / "astra-knowledge-append.json"
DEFAULT_KB = Path(__file__).resolve().parent.parent / "container" / "knowledge" / "challenge-approaches.md"
KB_ENTRY_RE = re.compile(r"^## (.+?)（([a-z0-9-]+)）\s*$", re.MULTILINE)


def existing_codes(kb_text: str) -> set[str]:
    return {m.group(2).lower() for m in KB_ENTRY_RE.finditer(kb_text)}


def format_entry(code: str, data: dict, source_tag: str) -> str:
    minutes = round((data.get("elapsed_seconds") or data.get("first_flag_seconds") or 0) / 60)
    awarded = data.get("awarded")
    score_part = f"{int(awarded)}" if awarded else "未知"
    approach = (data.get("approach") or "").strip() or "（无攻击链摘要）"
    return (
        f"\n## {data.get('name') or code}（{code}）\n"
        f"- 分值/难度：{score_part} / 待定 ｜ 首解耗时：{minutes}min ｜ 来源：[{source_tag}] auto-mined\n"
        f"- 思路1：{approach}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="赛后沉淀 JSON 文件")
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB, help="目标知识库 markdown")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要合并的条目")
    parser.add_argument("--mark-merged", action="store_true", help="合并后把输入文件改名为 .merged")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"[skip] 沉淀文件不存在：{args.input}")
        return 0
    try:
        pending = json.loads(args.input.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[error] 沉淀文件损坏：{exc}")
        return 1
    if not pending:
        print("[skip] 沉淀文件为空")
        return 0

    kb_text = args.kb.read_text(encoding="utf-8") if args.kb.exists() else "# 已解题思路知识库（参考用）\n"
    known = existing_codes(kb_text)

    merged, skipped = [], []
    for code, data in pending.items():
        if code.lower() in known:
            skipped.append(code)
            continue
        kb_text += format_entry(code, data, source_tag=datetime.now().strftime("merge%m%d"))
        merged.append(code)

    print(f"知识库条目：{len(known)} ｜ 沉淀待合并：{len(pending)} ｜ 新增：{len(merged)} ｜ 已存在跳过：{len(skipped)}")
    for code in merged:
        print(f"  + {code}")
    if skipped:
        print(f"  跳过（已存在）：{', '.join(skipped)}")
    if args.dry_run or not merged:
        return 0

    args.kb.parent.mkdir(parents=True, exist_ok=True)
    args.kb.write_text(kb_text, encoding="utf-8")
    if args.mark_merged:
        args.input.rename(args.input.with_suffix(".merged.json"))
    print(f"[done] 已写入 {args.kb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
