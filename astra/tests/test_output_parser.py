from __future__ import annotations

"""output_parser 专项：围栏边界（闭合/未闭合/嵌套）+ 解析资源边界（审计19轮）。

性能回归的判定基准：深嵌套未闭合病态输入在旧实现（全量 { 起点 × 深解析）下是
O(N²)，2MB 输入分钟级；新实现头/尾窗口 + 起点封顶必须秒级拒绝。
"""

import time

import pytest

from astra.dispatcher.output_parser import extract_json_object


def test_extracts_from_closed_fence_with_noise() -> None:
    text = "分析结论如下：\n```json\n{\"accepted\": true, \"data\": {\"fact\": {\"description\": \"端口全开\"}}}\n```\n以上。"
    assert extract_json_object(text)["accepted"] is True


def test_extracts_from_unclosed_fence_tail() -> None:
    """R5 实测场景：模型截断——```json 后无闭合符。"""
    text = "```json\n{\"accepted\": true, \"data\": {}}\n（后续被截断无闭合符"
    assert extract_json_object(text) == {"accepted": True, "data": {}}


def test_extracts_json_after_huge_prefix_via_tail_window() -> None:
    """超大 stdout（工具回显 MB 级）后置 JSON——尾窗口必须命中。"""
    prefix = "工具输出行" * 120_000  # ~1MB
    tail = '\n结论：{"accepted": true, "data": {"description": "尾部结论确认"}}'
    parsed = extract_json_object(prefix + tail)
    assert parsed["data"]["description"] == "尾部结论确认"


def test_extracts_json_at_head_of_huge_text_via_head_window() -> None:
    prefix = '{"accepted": true, "data": {"description": "头部结论"}}\n'
    suffix = "回显日志" * 150_000  # ~600KB
    assert extract_json_object(prefix + suffix)["data"]["description"] == "头部结论"


def test_pathological_deep_nesting_rejected_fast() -> None:
    """审计19轮性能回归：2MB 深嵌套未闭合 JSON（每个 { 起点都会深解析才失败，
    旧实现 O(N²)）——必须秒级抛 ValueError，不得挂死。"""
    bomb = '{"x":{"y":' * 160_000  # ~1.6MB，无闭合
    started = time.perf_counter()
    with pytest.raises(ValueError):
        extract_json_object(bomb)
    elapsed = time.perf_counter() - started
    assert elapsed < 10, f"病态输入解析耗时 {elapsed:.1f}s（资源边界失效）"


def test_brace_flood_rejected_fast() -> None:
    """散布 { 的纯文本洪泛——起点封顶后必须快速失败。"""
    flood = ("乱文本 { 未闭合 { 又一个 { " * 80_000) + "结尾无 JSON"
    started = time.perf_counter()
    with pytest.raises(ValueError):
        extract_json_object(flood)
    elapsed = time.perf_counter() - started
    assert elapsed < 10
