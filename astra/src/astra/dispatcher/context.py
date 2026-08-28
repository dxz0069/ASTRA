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


# ---------------- 焦点检索的结构信号（区别于逐条独立打分） ----------------

# 关键信息钉住（LOP 深度分级的读侧实现）：凭据/flag/RCE 级发现不参与预算竞争。
# 深度不靠写入时标注（侵入 schema），而按内容+置信度在读侧判定——挑战中被否决的不算关键。
_CRITICAL_RE = re.compile(
    r"(?i)flag\{|凭据|密码|私钥|口令|password|passwd|secret|api[_-]?key|"
    r"webshell|getshell|反弹|rce|ak/sk|access[_-]?key|session[_-]?id"
)


def _is_critical(fact: Any) -> bool:
    description = getattr(fact, "description", "") or ""
    return bool(_CRITICAL_RE.search(description)) and not getattr(fact, "challenged", False)


def _open_chain_depths(project: ProjectDetail) -> dict[str, int]:
    """图邻近检索：以未决航向为锚，返回各事实的图距（1=未决航向直接依赖，2=二跳）。

    词面/语义打分召回的是"描述像不像"，图距召回的是"因果上正在推进的链条"——
    描述完全改写过的关联发现（先发现服务、后才在它上面打出注入）靠词面永远召不回。
    """
    facts_ids = {fact.id for fact in project.facts}
    open_anchors: set[str] = set()
    for intent in project.intents:
        if intent.to is None:
            open_anchors.update(sid for sid in intent.from_ if sid in facts_ids)
    if not open_anchors:
        return {}
    # 二跳：已归航航向的落点，其来源含一跳锚点（锚点结论催生的下游发现）
    depth2: set[str] = set()
    for intent in project.intents:
        if intent.to is not None and intent.to in facts_ids:
            if any(sid in open_anchors for sid in intent.from_):
                depth2.add(intent.to)
    depth2 -= open_anchors
    return {**{fid: 1 for fid in open_anchors}, **{fid: 2 for fid in depth2}}


_CHAIN_BONUS = {1: 1.2, 2: 0.4}


def build_focus_fact_ids(project: ProjectDetail, budget: int) -> list[str]:
    """选出焦点星记 id 子集，输出保持星图原始顺序（时间线可读）。

    评分 = 相关度×2 [+ 语义×2] + 时间近度 + 未决链图距加成。
    关键事实（凭据/flag/RCE 级且未被质询否决）钉住：不参与预算竞争，永远内联。
    语义召回（embeddings.py 开启时）：token 重叠召不回的同义表述（如
    "SQL 注入教训" vs "MySQL 注入"）由向量余弦补足；嵌入不可用则静默降级为纯 token 打分。
    """
    allowed = [fact for fact in project.facts if fact.id != "goal"]
    if budget <= 0:
        return []
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

    chain_depths = _open_chain_depths(project)
    total = max(len(allowed) - 1, 1)
    scored: list[tuple[float, str]] = []
    pinned: set[str] = set()
    for index, fact in enumerate(allowed):
        relevance = _relevance_score(fact.description, focus_texts)
        semantic = _semantic_score(fact.id, fact.description)
        recency = index / total  # 0..1，越新越高
        chain = _CHAIN_BONUS.get(chain_depths.get(fact.id, 0), 0.0)
        # V8 负结果一等公民：已穷尽的方向（negative）在焦点中保活——防止
        # 同类死路被反复开航向（榜首实测靠关闭账本避免重复劳动）
        if fact.kind == "negative":
            chain += 0.8
        if _is_critical(fact):
            pinned.add(fact.id)  # 钉住：预算外保底，防关键发现被裁剪丢失
        scored.append((relevance * 2.0 + semantic * 2.0 + recency + chain, fact.id))

    scored.sort(key=lambda item: item[0], reverse=True)
    if len(pinned) > budget:
        # 钉住也受预算硬上限（零膨胀原则）：超额时保最近的（凭据类发现新者覆盖旧者）
        order_index = {fact.id: i for i, fact in enumerate(allowed)}
        pinned = set(sorted(pinned, key=lambda fid: order_index[fid])[-budget:])
    remaining = max(budget - len(pinned), 0)
    fill = [fact_id for _, fact_id in scored if fact_id not in pinned][:remaining]
    chosen = pinned | set(fill)
    return [fact.id for fact in allowed if fact.id in chosen]


def build_focus_open_intents(project: ProjectDetail, budget: int) -> list[dict[str, Any]]:
    """未完成航向（open intents）裁剪：最新优先，最多 budget 条。"""
    open_intents = [
        {
            "id": intent.id,
            "from": intent.from_,
            "description": intent.description,
            "worker": intent.worker,
            # UCB 航向投入卡：派发次数（投入）供 reason 显式做 explore/exploit 权衡
            "dispatch_count": getattr(intent, "dispatch_count", 0) or 0,
            "heartbeat": intent.last_heartbeat_at,
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
