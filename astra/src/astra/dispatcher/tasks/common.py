from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.runtime.process import ProcessResult
from astra.server.models import Fact, ProjectDetail

HEALTHCHECK_COMMUNICATE_GRACE_SECONDS = 10
PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/astra-prompts"
LOG = logging.getLogger(__name__)

FAILURE_HINT_PREFIX = "[失败学习] "
REVIEW_HINT_PREFIX = "[审查否决] "

# 星记去重：新发现与既有星记描述的 Jaccard 词集合相似度达到该阈值视为重复（防重复侦察）。
# token 粒度：ASCII 词 + 连续 CJK 段（中文描述按短语段匹配，兼顾中英文混排）。
FACT_SIMILARITY_THRESHOLD = 0.6


def _fact_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_./:\-{}]+|[\u4e00-\u9fff]+", text.lower()))


def find_duplicate_fact(project: ProjectDetail, description: str, *, exclude_ids: tuple[str, ...] = ("origin", "goal")) -> Fact | None:
    """在既有星记中找与 description 高度相似的（防重复侦察/重复写回）。

    - 描述含 flag{...} 的发现不去重（flag 必须完整写回，可能多个 flag 同题）；
    - origin/goal 不参与比较；
    - 相似度按 Jaccard 词集合计算，≥ FACT_SIMILARITY_THRESHOLD 视为重复。
    """
    if re.search(r"flag\{", description, re.IGNORECASE):
        return None
    target = _fact_tokens(description)
    if not target:
        return None
    for fact in project.facts:
        if fact.id in exclude_ids:
            continue
        other = _fact_tokens(fact.description)
        if not other:
            continue
        union = target | other
        if not union:
            continue
        if len(target & other) / len(union) >= FACT_SIMILARITY_THRESHOLD:
            return fact
    return None


def review_graph_summary(project: ProjectDetail, *, max_facts: int = 15, max_intents: int = 8) -> str:
    """双星审查的紧凑星图摘要（重构备忘候选 20：减少模型读大图文件的耗时）。

    内联到审查 prompt 的 {graph_yaml} 上下文（文件路径引用之后），模型可先看摘要
    再按需读完整快照；大图时显著缩短 challenge/verdict 的读图时间。
    """
    lines = ["## 星图摘要（快速参考，完整快照见上方文件路径）"]
    goal = next((f.description for f in project.facts if f.id == "goal"), "")
    if goal:
        lines.append(f"- Goal: {goal[:200]}")
    facts = [f for f in project.facts if f.id not in ("origin", "goal")][:max_facts]
    if facts:
        lines.append("- Facts:")
        for fact in facts:
            desc = " ".join(fact.description.split())[:120]
            lines.append(f"  - {fact.id}: {desc}")
    intents = getattr(project, "intents", None) or []
    if intents:
        lines.append("- Open Intents:")
        for intent in intents[:max_intents]:
            desc = " ".join(intent.description.split())[:120]
            lines.append(f"  - {intent.id}: {desc}")
    hints = getattr(project, "hints", None) or []
    if hints:
        previews = ", ".join(" ".join(h.content.split())[:40] for h in hints[:3])
        suffix = "..." if len(hints) > 3 else ""
        lines.append(f"- Hints: {len(hints)} 条（{previews}{suffix}）")
    return "\n".join(lines)


def record_failure_hint(
    client: ASTRAClient,
    project_id: str,
    source: str,
    summary: str,
    *,
    prefix: str = FAILURE_HINT_PREFIX,
) -> bool:
    """把失败教训/审查否决原因写为指引（hint），作为后续轮次的风险提示。

    对齐 Linghun 的失败学习原则：教训只作风险提示进入上下文，
    绝不充当完成证据（hint 不会进入 facts 校验路径）。

    熔断反馈环：内容完全相同的 hint 已存在时跳过写入（审查否决 → hint 增长 →
    再触发定航 → 同样的提案再被否决 → 再写 hint 的环路会让 hints 表无界增长）。
    返回 True 表示 hint 已就位（写入成功或已存在）；False 表示写入失败——
    调用方若依赖该 hint 触发后续定航，应返回 failed 避免 checkpoint 静默停摆。
    """
    hint = f"{prefix}{source}：{summary}"
    try:
        project = client.get_project(project_id)
        if any(h.content == hint for h in project.hints):
            LOG.info("failure hint deduped project=%s source=%s", project_id, source)
            return True
        response = client.create_hint(project_id, hint, creator="astra.learning")
        if response.status_code >= 400:
            LOG.warning(
                "failure hint write failed project=%s status=%s",
                project_id,
                response.status_code,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 —— 教训记录失败不影响主流程
        LOG.debug("failure hint write error project=%s error=%s", project_id, exc)
        return False


@dataclass(slots=True)
class HealthcheckRun:
    result: ProcessResult
    duration_ms: int


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    return config.runtime.worker_healthcheck == "startup_and_task"


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def run_healthcheck(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    command: list[str],
    *,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> HealthcheckRun:
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        command,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    started = time.perf_counter()
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds, HEALTHCHECK_COMMUNICATE_GRACE_SECONDS))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return HealthcheckRun(result=result, duration_ms=duration_ms)


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def project_allows_conclude_fallback(client: ASTRAClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: ASTRAClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: ASTRAClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    confidence: str = "medium",
    evidence: str | None = None,
    challenged: bool = False,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
        confidence=confidence,
        evidence=evidence,
        challenged=challenged,
    ).status


def write_conclude_result_with_fact_id(
    client: ASTRAClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    confidence: str = "medium",
    evidence: str | None = None,
    challenged: bool = False,
) -> ConcludeWriteResult:
    response = client.conclude(
        project_id,
        intent_id,
        worker_name,
        description,
        confidence=confidence,
        evidence=evidence,
        challenged=challenged,
    )
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code in (403, 404):
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: ASTRAClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
