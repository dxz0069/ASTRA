from __future__ import annotations

import json
import re
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)


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
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        for start in _object_start_positions(segment):
            try:
                parsed, _end = decoder.raw_decode(segment[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def _candidate_segments(text: str) -> list[str]:
    """候选段：原文、各闭合围栏内容，以及未闭合围栏的尾部（模型截断时 ```json 后
    再无闭合符——R5 实测场景），并剥离杂散空白/BOM。"""
    cleaned = text.strip().lstrip("\ufeff").strip()
    segments = [cleaned]
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
    return [index for index, char in enumerate(text) if char == "{"]
