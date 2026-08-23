from __future__ import annotations

"""星尘记忆——焦点子图上下文裁剪。

传统实现把整张星图的历史无差别地内联进每次推理，token 随图线性增长。
这里对 prompt 内联的星记（facts）、航向（intents）与指引（hints）做三层治理：

1. 焦点子图（Focus）：按「相关度 + 时间近度」选出与当前航向/目标最相关的星记，
   受 context_budget 硬上限约束；完整星图仍以文件引用（graph.yaml）提供。
2. 摘要记忆（Epitome）：超出预算的旧星记由轻量模型压缩为摘要星记（kind=summary），
   prompt 只呈现摘要（见 memory consolidation）。
3. 零膨胀：内联量恒有硬上限，不随星图规模增长。
"""

import re
from typing import Any

from astra.dispatcher import embeddings
from astra.server.models import ProjectDetail


def token_terms(text: str) -> set[str]:
    """分词：ASCII 词 + 连续中文串，用于相关度打分。"""
    return set(
        re.findall(
            r"[a-zA-Z0-9][a-zA-Z0-9_\-\./]{1,}|[\u4e00-\u9fff]{2,}",
            text.lower(),
        )
    )


def _relevance_score(description: str, focus_texts: list[str]) -> float:
    terms = token_terms(description)
    if not terms:
        return 0.0
    hits = 0
    for focus in focus_texts:
        hits += len(terms & token_terms(focus))
    return hits


def goal_text_of(project: ProjectDetail) -> str:
    for fact in project.facts:
        if fact.id == "goal":
            return fact.description
    return project.project.title or ""


def build_focus_fact_ids(project: ProjectDetail, budget: int) -> list[str]:
    """选出焦点星记 id 子集，输出保持星图原始顺序（时间线可读）。

    评分 = 相关度（与未完成航向/目标的描述词重叠）× 2 + 时间近度（列表越靠后越新）
    [+ 语义相关度 × 2，嵌入层可用时]。
    数量不超过 budget 时原样返回；超过时截取高分集合。
    语义召回（embeddings.py 开启时）：token 重叠召不回的同义表述（如
    “SQL 注入教训” vs "MySQL 注入"）由向量余弦补足；嵌入不可用则静默降级为纯 token 打分。
    """
    allowed = [fact for fact in project.facts if fact.id != "goal"]
    if len(allowed) <= budget:
        return [fact.id for fact in allowed]

    focus_texts = [intent.description for intent in project.intents if intent.to is None]
    goal = goal_text_of(project)
    if goal:
        focus_texts.append(goal)

    # 语义召回：focus 与全部候选星记一次性批量嵌入，失败即降级（对分）
    focus_vectors: list[list[float]] = []
    fact_vectors: dict[str, list[float]] = {}
    if focus_texts:
        vectors = embeddings.embed_texts(
            focus_texts + [fact.description for fact in allowed]
        )
        if vectors is not None:
            focus_vectors = vectors[: len(focus_texts)]
            fact_vectors = {
                fact.id: vec
                for fact, vec in zip(allowed, vectors[len(focus_texts):])
            }

    def _semantic_score(fact_id: str, description: str) -> float:
        vec = fact_vectors.get(fact_id)
        if vec is None or not focus_vectors:
            return 0.0
        return max(embeddings.cosine_similarity(vec, fv) for fv in focus_vectors)

    total = max(len(allowed) - 1, 1)
    scored: list[tuple[float, str]] = []
    for index, fact in enumerate(allowed):
        relevance = _relevance_score(fact.description, focus_texts)
        semantic = _semantic_score(fact.id, fact.description)
        recency = index / total  # 0..1，越新越高
        scored.append((relevance * 2.0 + semantic * 2.0 + recency, fact.id))

    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = {fact_id for _, fact_id in scored[:budget]}
    return [fact.id for fact in allowed if fact.id in chosen]


def build_focus_open_intents(project: ProjectDetail, budget: int) -> list[dict[str, Any]]:
    """未完成航向（open intents）裁剪：最新优先，最多 budget 条。"""
    open_intents = [
        {
            "id": intent.id,
            "from": intent.from_,
            "description": intent.description,
            "worker": intent.worker,
        }
        for intent in project.intents
        if intent.to is None
    ]
    if len(open_intents) <= budget:
        return open_intents
    created_at = {intent.id: intent.created_at or "" for intent in project.intents}
    ordered = sorted(open_intents, key=lambda item: created_at.get(item["id"], ""), reverse=True)
    return ordered[:budget]


def build_focus_hints(project: ProjectDetail, budget: int) -> list[dict[str, Any]]:
    """指引（hints）裁剪：最新优先，最多 budget 条。"""
    hints = [
        {
            "id": hint.id,
            "content": hint.content,
            "creator": hint.creator,
            "created_at": hint.created_at,
        }
        for hint in project.hints
    ]
    if len(hints) <= budget:
        return hints
    ordered = sorted(hints, key=lambda item: item["created_at"] or "", reverse=True)
    return ordered[:budget]
