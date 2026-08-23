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


def _entry_chunks(kb_text: str) -> dict[str, tuple[int, int]]:
    """{code: (条目 chunk 起止)}——chunk 为该条目标题行之后到下一条目之前的内容。"""
    matches = list(KB_ENTRY_RE.finditer(kb_text))
    chunks: dict[str, tuple[int, int]] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(kb_text)
        chunks[m.group(2).lower()] = (m.end(), end)
    return chunks


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", text.lower()))


def _similarity(a: str, b: str) -> float:
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


CONFLICT_SIMILARITY_THRESHOLD = 0.35  # 低于此值视为“不同打法”，触发冲突检测


def _merge_conflicting_entry(kb_text: str, chunk: tuple[int, int], new_approach: str, tag: str) -> str:
    """冲突检测：同码新攻击链与旧思路差异大时，追加为“思路N（更新版）”并标注差异。

    保留新旧两版而非覆盖——历史打法可能与当前实例都有效，交由开局注入时的战绩权重裁决。
    """
    start, end = chunk
    body = kb_text[start:end]
    existing_ideas = re.findall(r"^- 思路(\d+)：", body, re.MULTILINE)
    next_n = max((int(n) for n in existing_ideas), default=0) + 1
    line = f"- 思路{next_n}（更新版：与上述思路差异显著，{tag} 检出冲突并双版本保留）：{new_approach.strip()}\n"
    return kb_text[:end] + line + kb_text[end:]


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
    chunks = _entry_chunks(kb_text)
    tag = datetime.now().strftime("merge%m%d")

    merged, skipped, conflicted = [], [], []
    for code, data in pending.items():
        new_approach = (data.get("approach") or "").strip()
        if code.lower() not in known:
            kb_text += format_entry(code, data, source_tag=tag)
            merged.append(code)
            continue
        # 冲突检测：同码条目比对新旧攻击链，相似→重复跳过；差异大→双版本保留
        start, end = chunks[code.lower()]
        old_approach = "；".join(
            re.findall(r"^- 思路\d+：(.+)$", kb_text[start:end], re.MULTILINE)
        )
        if _similarity(old_approach, new_approach) < CONFLICT_SIMILARITY_THRESHOLD and new_approach:
            kb_text = _merge_conflicting_entry(kb_text, chunks[code.lower()], new_approach, tag)
            chunks = _entry_chunks(kb_text)  # 插入后偏移失效，重算
            conflicted.append(code)
        else:
            skipped.append(code)

    print(
        f"知识库条目：{len(known)} ｜ 沉淀待合并：{len(pending)} ｜ 新增：{len(merged)}"
        f" ｜ 重复跳过：{len(skipped)} ｜ 冲突双版本保留：{len(conflicted)}"
    )
    for code in merged:
        print(f"  + {code}")
    if conflicted:
        print(f"  ⚠ 冲突检出（新旧攻击链差异显著，双版本保留待战绩裁决）：{', '.join(conflicted)}")
    if skipped:
        print(f"  跳过（已存在且思路相似）：{', '.join(skipped)}")
    if args.dry_run or not (merged or conflicted):
        return 0

    args.kb.parent.mkdir(parents=True, exist_ok=True)
    args.kb.write_text(kb_text, encoding="utf-8")
    if args.mark_merged:
        args.input.rename(args.input.with_suffix(".merged.json"))
    print(f"[done] 已写入 {args.kb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
