from __future__ import annotations

import logging
import time

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.contracts import (
    parse_json_output,
    validate_challenge_payload,
    validate_explore_payload,
)
from astra.dispatcher.prompting import format_json_block, load_prompt, render_prompt
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.tasks.common import (
    REVIEW_HINT_PREFIX,
    best_effort_release,
    cancel_reason,
    did_timeout,
    find_duplicate_fact,
    preview,
    project_allows_conclude_fallback,
    record_failure_hint,
    review_graph_summary,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
    write_graph_snapshot_reference,
)
from astra.dispatcher.workers.registry import get_driver

# V8 负结果一等公民：从星记描述推断 fact kind——"此路不通/已穷尽/无结果"类
# 发现标记为 negative，焦点裁剪时加权保活（防同类死路被反复开航向）。
_NEGATIVE_RE = re.compile(
    r"(?i)(此路不通|已穷尽|无结果|未发现|不存在|已排除|dead.?end|exhausted|"
    r"no.?result|not.?found|ruled.?out|deadend|穷尽|排除|无可用|无有效|zero.?hit)"
)


def _infer_fact_kind(description: str) -> str:
    """正/负结果自动分流：负面结论 → negative kind（与 regular 同权存储）。"""
    if _NEGATIVE_RE.search(description or ""):
        return "negative"
    return "regular"

from astra.server.models import Intent, ProjectDetail
import re

LOG = logging.getLogger(__name__)


def _challenge_low_confidence_fact(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    project: ProjectDetail,
    export_yaml: str,
    description: str,
    evidence: str | None,
    cancellation: TaskCancellation,
) -> tuple[bool, str]:
    """低置信（confidence=low）巡猎发现写回前质询（重构备忘候选 4）。

    返回 (允许写回?, 审查说明)。与 reason 审查链路一致：
    - 质询链路不可用（重试后仍失败）→ 降级放行 + 记 hint；
    - 质询解析失败 → 降级放行 + 记 hint（低置信 fact 不值得因审查基础设施
      故障而整体丢失，但会留下提示供后续定航核对）；
    - 质询明确否决 → 不写回 + 记 [审查否决] hint，防止误报进星图。
    """
    from astra.dispatcher.tasks.reason import _resolve_review_worker, _run_review_stage_with_retry

    review_worker, driver = _resolve_review_worker(config, worker)
    goal = next((fact.description for fact in project.facts if fact.id == "goal"), "")
    graph_ref = write_graph_snapshot_reference(
        container_manager,
        container_name,
        export_yaml.strip(),
        phase="review",
    )
    # 审查读图提速（候选 20）：文件路径引用 + 紧凑星图摘要
    graph_context = graph_ref + "\n\n" + review_graph_summary(project)
    proposal = {"fact": {"description": description, "evidence": evidence or ""}}
    payload = _run_review_stage_with_retry(
        config, container_manager, container_name, review_worker, driver,
        "challenge",
        {"graph_yaml": graph_context, "goal": goal, "proposal": format_json_block(proposal)},
        cancellation,
    )
    if payload is None:
        record_failure_hint(
            client, project.project.id, "review",
            "低置信巡猎发现质询链路不可用（重试后仍失败），本次降级放行；请在后续定航中自行核对证据",
            prefix=REVIEW_HINT_PREFIX,
        )
        return True, "challenge_unavailable"
    try:
        outcome, _ = validate_challenge_payload(payload)
    except ValueError as exc:
        LOG.warning(
            "low-confidence fact challenge parse failed project=%s error=%s stdout_preview=%s",
            project.project.id, exc, preview(str(payload), 300),
        )
        record_failure_hint(
            client, project.project.id, "review",
            "低置信巡猎发现质询输出解析失败，本次降级放行；请在后续定航中自行核对证据",
            prefix=REVIEW_HINT_PREFIX,
        )
        return True, "challenge_parse_failed"
    if outcome != "accepted":
        LOG.info(
            "low-confidence fact challenged down project=%s reason=%s",
            project.project.id, payload.get("reason", ""),
        )
        record_failure_hint(
            client, project.project.id, "review",
            f"低置信巡猎发现被质询否决（{payload.get('reason', '')}），未写回星图；请补强证据或换方向",
            prefix=REVIEW_HINT_PREFIX,
        )
        return False, "challenged"
    return True, "accepted"


def _should_write_fact(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    project: ProjectDetail,
    export_yaml: str,
    description: str,
    confidence: str,
    evidence: str | None,
    cancellation: TaskCancellation,
) -> tuple[bool, str]:
    """写回前把关：星记去重（防重复侦察）+ 低置信质询（防误报）。"""
    duplicate = find_duplicate_fact(project, description)
    if duplicate is not None:
        LOG.info(
            "explore fact duplicate skipped project=%s similar_fact=%s description=%s",
            project.project.id, duplicate.id, description[:120],
        )
        record_failure_hint(
            client, project.project.id, "explore",
            f"巡猎发现与既有星记 {duplicate.id} 重复，未重复写回；请在该星记基础上继续深化",
            prefix=REVIEW_HINT_PREFIX,
        )
        return False, f"duplicate_of_{duplicate.id}"
    if confidence == "low":
        allowed, note = _challenge_low_confidence_fact(
            config, client, container_manager, container_name, worker,
            project, export_yaml, description, evidence, cancellation,
        )
        if not allowed:
            return False, note
        # 质询阶段真实跑过并放行 → 该星记标记 challenged（前端「被质询」徽章）
        return True, note
    return True, "ok"


def run_explore_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "starting container exec project=%s intent=%s worker=%s phase=explore_healthcheck timeout=%ss",
                project.project.id,
                intent.id,
                worker.name,
                healthcheck_timeout,
            )
            healthcheck = run_healthcheck(
                container_manager,
                container_name,
                worker,
                driver.build_healthcheck(worker),
                timeout_seconds=healthcheck_timeout,
                lease=lease,
                cancellation=cancellation,
            )
            cancelled = cancel_reason(healthcheck.result, cancellation)
            if cancelled is not None:
                LOG.info(
                    "explore cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    cancelled,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during explore healthcheck project=%s intent=%s worker=%s status=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    lease.failure.status_code,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "failed"
            if healthcheck.result.returncode != 0:
                LOG.warning(
                    "worker unhealthy project=%s intent=%s worker=%s healthcheck_ms=%s stderr=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    healthcheck.duration_ms,
                    preview(healthcheck.result.stderr),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "explore.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="explore_execute",
                ),
                "intent_id": intent.id,
                "intent_description": intent.description,
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = _run_process(
            container_manager,
            container_name,
            worker,
            execute.argv,
            phase="explore_execute",
            timeout=config.tasks.explore.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "explore cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during explore project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, data = validate_explore_payload(payload)
                description = data["description"] if data else None
                confidence = data["confidence"] if data else "medium"
                evidence = data["evidence"] if data else None
            except Exception as exc:
                LOG.warning(
                    "explore parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                return _try_conclude_fallback(
                    config,
                    client,
                    container_manager,
                    container_name,
                    worker,
                    driver,
                    project.project.id,
                    intent,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                )
            if kind == "rejected":
                LOG.warning(
                    "explore rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            allowed, note = _should_write_fact(
                config, client, container_manager, container_name, worker,
                client.get_project(project.project.id), export_yaml,
                description, confidence, evidence, cancellation,
            )
            if not allowed:
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "success"
            return write_conclude_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                description,
                source="explore_execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
                confidence=confidence,
                evidence=evidence,
                challenged=(note == "accepted"),
                kind=_infer_fact_kind(description),
            )
        if did_timeout(first):
            LOG.warning(
                "explore timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            return _try_conclude_fallback(
                config,
                client,
                container_manager,
                container_name,
                worker,
                driver,
                project.project.id,
                intent,
                export_yaml,
                session,
                lease,
                cancellation,
            )
        LOG.warning(
            "explore command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        if preview(first.stderr):
            record_failure_hint(
                client, project.project.id, "explore",
                f"巡猎命令失败 code={first.returncode}（航向: {intent.description[:80]}）: {preview(first.stderr, 200)}",
            )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("explore task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    project_id: str,
    intent: Intent,
    export_yaml: str,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project_id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning("conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project_id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project_id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project_id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "explore_conclude.md"),
        {
            "graph_yaml": write_graph_snapshot_reference(
                container_manager,
                container_name,
                export_yaml.strip(),
                phase="explore_conclude",
            ),
            "intent_id": intent.id,
            "intent_description": intent.description,
        },
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting conclude fallback project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = _run_process(
        container_manager,
        container_name,
        worker,
        conclude_argv,
        phase="explore_conclude",
        timeout=config.tasks.explore.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project_id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, data = validate_explore_payload(payload)
        description = data["description"] if data else None
        confidence = data["confidence"] if data else "medium"
        evidence = data["evidence"] if data else None
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project_id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "rejected"
    allowed, note = _should_write_fact(
        config, client, container_manager, container_name, worker,
        client.get_project(project_id), export_yaml,
        description, confidence, evidence, cancellation,
    )
    if not allowed:
        best_effort_release(client, project_id, intent.id, worker.name)
        return "success"
    return write_conclude_result(
        client,
        project_id,
        intent.id,
        worker.name,
        description,
        source="explore_conclude",
        phase_ms=conclude_ms,
        confidence=confidence,
        evidence=evidence,
        challenged=(note == "accepted"),
    )


def _run_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout: int,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
):
    return run_worker_process(
        container_manager,
        container_name,
        worker,
        argv,
        phase=phase,
        timeout_seconds=timeout,
        lease=lease,
        cancellation=cancellation,
    )
