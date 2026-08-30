from __future__ import annotations

"""质询星探——关键结论的独立对抗审查（星尘记忆的可审计防线）。

两类把关：
1. complete 收束把关（同步）：decide 判 Goal 满足后、写入图前，对 complete 结论
   跑一次对抗审查；refute → 拒绝收束并留痕（hint），搜索继续——拦"提前收束"，
   多旗题尤其受益。任何质询失败 fail-open 放行，绝不卡死收束。
2. 关键事实审计（异步）：execute 收束写入凭据/flag 级事实后投递后台队列异步
   质询（不阻塞旗提交）；质疑成立写 hint 留痕，决策链可回放。

有界纪律：每项目质询计数封顶（默认 4 次，共用预算，超出直接放行）；计数器
超规模整体清空；审计队列有界满即丢弃。ASTRA_CHALLENGE_MODE=0 整体关闭
（托管跑分默认关，本地默认开）。
"""

import logging
import os
import queue
import threading

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.contracts import parse_json_output, validate_challenge_payload
from astra.dispatcher.prompting import load_prompt, render_prompt
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.tasks.common import (
    cancel_reason,
    did_timeout,
    preview,
    run_worker_process,
    write_graph_snapshot_reference,
)
from astra.dispatcher.workers.registry import get_driver
from astra.server.models import ProjectDetail

LOG = logging.getLogger(__name__)

_challenge_counts: dict[str, int] = {}
_counts_lock = threading.Lock()

_audit_queue: "queue.Queue[dict]" = queue.Queue(maxsize=16)
_audit_thread: threading.Thread | None = None
_audit_thread_lock = threading.Lock()


def challenge_enabled() -> bool:
    return os.environ.get("ASTRA_CHALLENGE_MODE", "1") not in ("0", "false", "no")


def reset_challenge_state_for_tests() -> None:
    """测试隔离：清计数器与队列（生产不调用）。"""
    with _counts_lock:
        _challenge_counts.clear()
    while not _audit_queue.empty():
        try:
            _audit_queue.get_nowait()
        except queue.Empty:
            break


def _reserve_challenge_slot(project_id: str, limit: int) -> bool:
    """每项目质询次数有界：超出后不再质询（返回 False = 放行/不入队）。"""
    with _counts_lock:
        current = _challenge_counts.get(project_id, 0)
        if current >= limit:
            if len(_challenge_counts) > 1024:  # 长跑清理：防计数器无界
                _challenge_counts.clear()
            return False
        _challenge_counts[project_id] = current + 1
        return True


def run_challenge_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation | None,
    claim: str,
    claim_context: str,
    lease: HeartbeatLease | None = None,
) -> tuple[str, str | None]:
    """跑一次对抗审查，返回 (verdict, reason)：("uphold", None) / ("refute", reason)。

    fail-open 契约：超时/解析失败/worker 拒答/任何异常 → ("uphold", None)——
    质询故障绝不阻塞主链路（收束/写图照常），只在日志留痕。
    """
    driver = get_driver(worker.type)
    try:
        container_name = container_manager.ensure_running(project.project.id)
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "challenge.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="challenge",
                ),
                "claim": claim,
                "claim_context": claim_context,
            },
        )
        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="challenge",
            timeout_seconds=config.tasks.challenge.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        if cancel_reason(result, cancellation) is not None or did_timeout(result) or result.returncode != 0:
            LOG.warning(
                "challenge worker failed (fail-open) project=%s worker=%s code=%s timed_out=%s stderr=%s",
                project.project.id, worker.name, result.returncode, result.timed_out, preview(result.stderr),
            )
            return "uphold", None
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, data = validate_challenge_payload(payload)
        if kind == "refute":
            LOG.info(
                "challenge refuted claim project=%s worker=%s claim=%.120s reason=%.200s",
                project.project.id, worker.name, claim, data["reason"],
            )
            return "refute", data["reason"]
        LOG.info(
            "challenge upheld claim project=%s worker=%s claim=%.120s",
            project.project.id, worker.name, claim,
        )
        return "uphold", None
    except Exception as exc:  # noqa: BLE001 —— fail-open：质询故障不阻塞主链路
        LOG.warning("challenge task crashed (fail-open) project=%s error=%s", project.project.id, exc)
        return "uphold", None


def gate_complete_claim(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
    lease: HeartbeatLease | None,
    complete_data: dict,
) -> tuple[str, str | None]:
    """complete 收束把关：开关与计数预算内的对抗审查；否则直接放行。"""
    if not challenge_enabled():
        return "uphold", None
    if not _reserve_challenge_slot(project.project.id, config.tasks.challenge.max_per_project):
        LOG.info(
            "challenge budget exhausted project=%s — complete allowed without review",
            project.project.id,
        )
        return "uphold", None
    claim = f"Project Goal is satisfied. Completion argument: {complete_data.get('description', '')}"
    claim_context = f"Supporting facts: {', '.join(complete_data.get('from', []))}"
    return run_challenge_task(
        config,
        client,
        container_manager,
        project,
        export_yaml,
        worker,
        cancellation,
        claim=claim,
        claim_context=claim_context,
        lease=lease,
    )


def submit_critical_fact_audit(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project_id: str,
    export_yaml: str,
    worker: WorkerConfig,
    fact_description: str,
) -> bool:
    """关键事实异步审计：入队即返回（不阻塞旗提交）；队列满丢弃（有界纪律）。"""
    if not challenge_enabled():
        return False
    if not _reserve_challenge_slot(project_id, config.tasks.challenge.max_per_project):
        return False
    job = {
        "config": config,
        "client": client,
        "container_manager": container_manager,
        "project_id": project_id,
        "export_yaml": export_yaml,
        "worker": worker,
        "fact_description": fact_description,
    }
    try:
        _audit_queue.put_nowait(job)
    except queue.Full:
        LOG.info("challenge audit queue full project=%s — audit dropped", project_id)
        return False
    _ensure_audit_thread()
    return True


def drain_audit_queue_for_tests() -> None:
    """测试同步点：处理完队列中全部审计任务（生产不调用）。"""
    _ensure_audit_thread()
    _audit_queue.join()


def _ensure_audit_thread() -> None:
    global _audit_thread
    with _audit_thread_lock:
        if _audit_thread is None or not _audit_thread.is_alive():
            _audit_thread = threading.Thread(target=_audit_loop, name="astra-challenge-audit", daemon=True)
            _audit_thread.start()


def _audit_loop() -> None:
    while True:
        job = _audit_queue.get()
        try:
            _run_audit(job)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("challenge audit failed project=%s error=%s", job.get("project_id"), exc)
        finally:
            _audit_queue.task_done()


def _run_audit(job: dict) -> None:
    client: ASTRAClient = job["client"]
    project_id = job["project_id"]
    try:
        project = client.get_project(project_id)
    except Exception:  # noqa: BLE001 —— 项目已删/不可达 → 审计作废
        return
    verdict, reason = run_challenge_task(
        job["config"],
        client,
        job["container_manager"],
        project,
        job["export_yaml"],
        job["worker"],
        None,
        claim=f"Critical fact under review: {job['fact_description']}",
        claim_context=(
            "The executor self-certified this credential/flag-level discovery. "
            "Attack its evidence chain; refute only if the proof is missing or contradicted."
        ),
    )
    if verdict == "refute":
        hint = f"[质询星探质疑·关键结论] {job['fact_description'][:200]}——{reason[:400]}"
        response = client.create_hint(project_id, hint, creator="astra.challenge")
        if response.ok:
            LOG.info("challenge audit refuted critical fact project=%s hint written", project_id)
        else:
            LOG.warning(
                "challenge audit hint write failed project=%s status=%s",
                project_id, response.status_code,
            )
