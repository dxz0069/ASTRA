from __future__ import annotations

import json
import re
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)

# 审计19轮：解析资源边界——超大 stdout（工具回显可达 MB 级）逐 { 起点 raw_decode
# 在深嵌套病态输入下是 O(N²)（对抗/失控输出可拖死 dispatcher 线程分钟级）。
# 头 64K + 尾 256K 双窗口（结论 JSON 实际 <16KB，"JSON 在末尾"是常见形态），
# 起点尝试数封顶防最坏情形；超窗内容本就不可能是合法结论。
MAX_PARSE_TEXT_CHARS = 262144
MAX_PARSE_HEAD_CHARS = 65536
MAX_OBJECT_START_TRIES = 512


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    seen: set[str] = set()

    for candidate in _candidate_segments(text):
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment)
        except (json.JSONDecodeError, RecursionError):
            # RecursionError：深嵌套炸弹（数万层未闭合）会打爆递归栈而非解码失败——
            # 一并按"解析失败"处理，保持本函数只抛 ValueError 的完备契约
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        for start in _object_start_positions(segment):
            try:
                parsed, _end = decoder.raw_decode(segment[start:])
            except (json.JSONDecodeError, RecursionError):
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def _candidate_segments(text: str) -> list[str]:
    """候选段：原文、各闭合围栏内容，以及未闭合围栏的尾部（模型截断时 ```json 后
    再无闭合符——R5 实测场景），并剥离杂散空白/BOM。超大文本附头/尾双窗口。"""
    cleaned = text.strip().lstrip("\ufeff").strip()
    segments = [cleaned] if len(cleaned) <= MAX_PARSE_TEXT_CHARS else []
    if len(cleaned) > MAX_PARSE_TEXT_CHARS:
        segments.append(cleaned[:MAX_PARSE_HEAD_CHARS])
        segments.append(cleaned[-MAX_PARSE_TEXT_CHARS:])
    for match in FENCED_BLOCK_RE.finditer(cleaned):
        segments.append(match.group(1).strip())
    # 未闭合围栏：```json 开头但无 ``` 结尾——取围栏标记后的全部内容
    for match in re.finditer(r"```(?:json)?[ \t]*\r?\n(.*)$", cleaned, re.IGNORECASE | re.DOTALL):
        tail = match.group(1).strip()
        # 排除已被闭合围栏正则覆盖的情形（尾部含闭合符时闭合正则已处理）
        if "```" not in tail:
            segments.append(tail)
    return segments


def _object_start_positions(text: str) -> list[int]:
    return [index for index, char in enumerate(text) if char == "{"][:MAX_OBJECT_START_TRIES]
