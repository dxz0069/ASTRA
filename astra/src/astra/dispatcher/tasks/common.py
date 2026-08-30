from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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
# P1-4：图快照目录最长保留时长——超过即视为陈旧可清理（任务超时远小于该值）
GRAPH_SNAPSHOT_MAX_AGE_SECONDS = 2 * 3600
# P1-4：陈旧快照清理节流间隔——不必每次派任务都全量扫描目录
_GRAPH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS = 600
_last_snapshot_cleanup_at = [0.0]
LOG = logging.getLogger(__name__)

FAILURE_HINT_PREFIX = "[失败学习] "

# 天枢去重：新发现与既有天枢描述的 Jaccard 词集合相似度达到该阈值视为重复（防重复侦察）。
# token 粒度：ASCII 词 + 连续 CJK 段（中文描述按短语段匹配，兼顾中英文混排）。
FACT_SIMILARITY_THRESHOLD = 0.6


def _fact_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_./:\-{}]+|[\u4e00-\u9fff]+", text.lower()))


def find_duplicate_fact(project: ProjectDetail, description: str, *, exclude_ids: tuple[str, ...] = ("origin", "goal")) -> Fact | None:
    """在既有天枢中找与 description 高度相似的（防重复侦察/重复写回）。

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


def _cleanup_stale_graph_snapshots() -> None:
    """P1-4：清理宿主侧超过 2 小时的旧图快照目录，防止 /tmp/astra-prompts 无限膨胀。

    每次派任务都写一份 <phase>-<uuid>/graph.yaml 且从不清理；本地执行模式下
    快照落在宿主临时目录（POSIX: $TMPDIR/astra-prompts，Windows: C:\\tmp\\
    astra-prompts——与 LocalContainerManager._to_host_path 的 /tmp 映射约定一致），
    跨项目累积成磁盘泄漏；docker 模式容器随项目整体回收，不受此影响。
    目录 mtime 即创建时间（写 graph.yaml 后不再变动），任务最长超时远小于
    2 小时，按此判龄不会删到在用快照。清理失败静默忽略——不影响派发主流程。
    """
    now = time.time()
    if now - _last_snapshot_cleanup_at[0] < _GRAPH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS:
        return
    _last_snapshot_cleanup_at[0] = now
    try:
        if sys.platform == "win32":
            root = Path("C:/tmp") / "astra-prompts"
        else:
            root = Path(tempfile.gettempdir()) / "astra-prompts"
        if not root.is_dir():
            return
        for entry in root.iterdir():
            try:
                if entry.is_dir() and now - entry.stat().st_mtime > GRAPH_SNAPSHOT_MAX_AGE_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue  # 单个目录清理失败不影响其余
    except OSError:
        pass


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    # P1-4：写入新快照前顺手清理陈旧快照目录（节流，见函数内说明）
    _cleanup_stale_graph_snapshots()
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
    _pending_usage = {"logged": False}
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
        result = process.communicate(timeout=communicate_timeout(timeout_seconds))
        _log_phase_usage(worker.name, phase, result.stdout or "")
        return result
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def _log_phase_usage(worker_name: str, phase: str, stdout: str) -> None:
    """从 pi 的 json 事件流提取本阶段 token 用量并记日志（会话文件落盘不稳定，
    stdout 事件才是可靠载体；usage 口径=各 turn 累计）。"""
    import json as _json

    total = 0
    hits = 0
    for line in stdout.splitlines():
        line = line.strip()
        if '"usage"' not in line or not line.startswith("{"):
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        u = msg.get("usage") or {}
        if not u:
            continue
        hits += 1
        total = max(total, int(u.get("totalTokens") or 0))
    if hits:
        LOG.info("phase usage worker=%s phase=%s turns=%s totalTokens~%s", worker_name, phase, hits, total)


def project_allows_conclude_fallback(client: ASTRAClient, project_id: str, *, worker_name: str, step_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s step=%s worker=%s status=%s",
        project_id,
        step_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_decide(client: ASTRAClient, project_id: str, worker_name: str, lease_token: str | None = None) -> None:
    response = client.release_decide(project_id, worker_name, lease_token)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "decide release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released decide project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "decide release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: ASTRAClient,
    project_id: str,
    step_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    kind: str = "regular",
    finding: str | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        step_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
        kind=kind,
        finding=finding,
    ).status


def write_conclude_result_with_fact_id(
    client: ASTRAClient,
    project_id: str,
    step_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    kind: str = "regular",
    finding: str | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(
        project_id,
        step_id,
        worker_name,
        description,
        kind=kind,
        finding=finding,
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
                "step concluded project=%s step=%s worker=%s source=%s phase_ms=%s",
                project_id,
                step_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "step concluded project=%s step=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                step_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code in (403, 404):
        LOG.info(
            "project became inactive during conclude project=%s step=%s worker=%s",
            project_id,
            step_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s step=%s worker=%s status=%s body=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, step_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: ASTRAClient, project_id: str, step_id: str, worker_name: str) -> None:
    response = client.release(project_id, step_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s step=%s worker=%s status=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released step project=%s step=%s worker=%s", project_id, step_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s step=%s worker=%s status=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
        )
