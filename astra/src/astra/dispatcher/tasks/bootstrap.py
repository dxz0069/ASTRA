from __future__ import annotations

import logging
import re
import time

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_bootstrap_stream,
)
from astra.dispatcher.context import build_focus_hints
from astra.dispatcher.prompting import format_hints, load_prompt, render_prompt
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.tasks.common import (
    best_effort_release,
    cancel_reason,
    did_timeout,
    project_allows_conclude_fallback,
    preview,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
    write_conclude_result_with_fact_id,
)
from astra.dispatcher.workers.registry import get_driver
from astra.server.models import ProjectDetail, Step

LOG = logging.getLogger(__name__)

_FLAG_RE = re.compile(r"flag\{[^}\s]{3,}\}", re.IGNORECASE)
_PLACEHOLDER_FLAG_RE = re.compile(r"^flag\{\s*\.{3}\s*\}$", re.IGNORECASE)


def _extract_flags_from_text(text: str) -> list[str]:
    """从叙述文本中提取 flag（模型未按 JSON 行输出时的兜底）。"""
    seen: set[str] = set()
    flags: list[str] = []
    for match in _FLAG_RE.findall(text or ""):
        flag = match.strip()
        if _PLACEHOLDER_FLAG_RE.match(flag):
            continue
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    return flags


def run_bootstrap_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    step: Step,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_step(client, project.project.id, step.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "starting container exec project=%s step=%s worker=%s phase=bootstrap_healthcheck timeout=%ss",
                project.project.id,
                step.id,
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
                    "bootstrap cancelled during healthcheck project=%s step=%s worker=%s reason=%s",
                    project.project.id,
                    step.id,
                    worker.name,
                    cancelled,
                )
                best_effort_release(client, project.project.id, step.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during bootstrap healthcheck project=%s step=%s worker=%s status=%s",
                    project.project.id,
                    step.id,
                    worker.name,
                    lease.failure.status_code,
                )
                best_effort_release(client, project.project.id, step.id, worker.name)
                return "failed"
            if healthcheck.result.returncode != 0:
                LOG.warning(
                    "worker unhealthy project=%s step=%s worker=%s healthcheck_ms=%s stderr=%s",
                    project.project.id,
                    step.id,
                    worker.name,
                    healthcheck.duration_ms,
                    preview(healthcheck.result.stderr),
                )
                best_effort_release(client, project.project.id, step.id, worker.name)
                return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap.md"),
            _bootstrap_prompt_replacements(project, config.runtime.context_budget.max_inline_hints),
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = run_worker_process(
            container_manager,
            container_name,
            worker,
            execute.argv,
            phase="bootstrap",
            timeout_seconds=config.tasks.bootstrap.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "bootstrap cancelled project=%s step=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                step.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, step.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during bootstrap project=%s step=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                step.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, step.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                facts, complete = validate_bootstrap_stream(model_output)
            except Exception as exc:
                LOG.warning(
                    "bootstrap parse failed project=%s step=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    step.id,
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
                    project,
                    step,
                    session,
                    lease,
                    cancellation,
                )
            if not facts:
                # 兜底：模型可能用叙述文本而非 JSON 行——扫描 stdout 中的 flag 写回星图
                recovered_flags = _extract_flags_from_text(model_output)
                if recovered_flags:
                    for f in recovered_flags:
                        response = client.create_fact(
                            project.project.id, f"获取到 flag：{f}", kind="regular", creator=worker.name,
                        )
                        if response.status_code >= 400:
                            LOG.warning("bootstrap recovered flag write failed project=%s status=%s", project.project.id, response.status_code)
                    LOG.info(
                        "bootstrap recovered flags from narrative project=%s step=%s worker=%s flags=%s",
                        project.project.id, step.id, worker.name, recovered_flags,
                    )
                    return write_conclude_result(
                        client,
                        project.project.id,
                        step.id,
                        worker.name,
                        f"获取到 flag：{recovered_flags[-1]}",
                        source="bootstrap",
                        phase_ms=execute_ms,
                        total_ms=int((time.perf_counter() - task_started) * 1000),
                    )
                LOG.warning(
                    "bootstrap no facts extracted project=%s step=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    step.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, step.id, worker.name)
                return "rejected"
            # 增量天枢：除最后一条外全部直接写回
            for fd in facts[:-1]:
                response = client.create_fact(project.project.id, fd, kind="regular", creator=worker.name)
                if response.status_code >= 400:
                    LOG.warning("bootstrap incremental fact write failed project=%s status=%s", project.project.id, response.status_code)
            last_fact = facts[-1]
            if complete:
                return _write_bootstrap_complete_result(
                    client,
                    project.project.id,
                    step.id,
                    worker.name,
                    last_fact,
                    complete,
                    source="bootstrap",
                    phase_ms=execute_ms,
                    total_ms=int((time.perf_counter() - task_started) * 1000),
                )
            # 未声明完成：conclude 最后一条天枢，交给 reason 接管
            LOG.info("bootstrap concluded without complete project=%s step=%s worker=%s facts=%s", project.project.id, step.id, worker.name, len(facts))
            return write_conclude_result(
                client,
                project.project.id,
                step.id,
                worker.name,
                last_fact,
                source="bootstrap",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )
        if did_timeout(first):
            LOG.warning(
                "bootstrap timed out project=%s step=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                step.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            # 抢救 stdout 中已输出的增量天枢（超时不丢中间产物）
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                rescued, _ = validate_bootstrap_stream(model_output)
                if not rescued:
                    # 兜底：叙述文本中的 flag
                    rescued = _extract_flags_from_text(model_output)
                for fd in rescued:
                    response = client.create_fact(project.project.id, fd, kind="regular", creator=worker.name)
                    if response.status_code >= 400:
                        LOG.warning("bootstrap rescued fact write failed project=%s status=%s", project.project.id, response.status_code)
                if rescued:
                    LOG.info("bootstrap rescued facts on timeout project=%s step=%s count=%s", project.project.id, step.id, len(rescued))
            except Exception as exc:
                LOG.debug("bootstrap rescue failed project=%s error=%s", project.project.id, exc)
            return _try_conclude_fallback(
                config,
                client,
                container_manager,
                container_name,
                worker,
                driver,
                project,
                step,
                session,
                lease,
                cancellation,
            )
        LOG.warning(
            "bootstrap command failed project=%s step=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            step.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("bootstrap task crashed project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
        best_effort_release(client, project.project.id, step.id, worker.name)
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
    project: ProjectDetail,
    step: Step,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "bootstrap conclude fallback unavailable project=%s step=%s worker=%s supports_conclude=%s has_session=%s",
            project.project.id,
            step.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning(
            "bootstrap conclude fallback skipped because heartbeat already lost project=%s step=%s worker=%s",
            project.project.id,
            step.id,
            worker.name,
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "bootstrap conclude fallback skipped because task was cancelled project=%s step=%s worker=%s reason=%s",
            project.project.id,
            step.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project.project.id,
        worker_name=worker.name,
        step_id=step.id,
    ):
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project.project.id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "bootstrap_conclude.md"),
        _bootstrap_prompt_replacements(project, config.runtime.context_budget.max_inline_hints),
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting bootstrap conclude fallback project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
    conclude_started = time.perf_counter()
    result = run_worker_process(
        container_manager,
        container_name,
        worker,
        conclude_argv,
        phase="bootstrap_conclude",
        timeout_seconds=config.tasks.bootstrap.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "bootstrap conclude cancelled project=%s step=%s worker=%s reason=%s conclude_ms=%s",
            project.project.id,
            step.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "bootstrap conclude failed project=%s step=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            step.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        conclude_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(conclude_data, dict) and isinstance(conclude_data.get("complete"), dict):
            LOG.warning(
                "bootstrap conclude returned unexpected complete payload project=%s step=%s worker=%s complete_preview=%s",
                project.project.id,
                step.id,
                worker.name,
                preview(str(conclude_data.get("complete"))),
            )
        kind, fact_description = validate_bootstrap_conclude_payload(payload)
    except Exception as exc:
        LOG.warning(
            "bootstrap conclude parse failed project=%s step=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            step.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "bootstrap conclude rejected project=%s step=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project.project.id,
            step.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "rejected"
    return write_conclude_result(
        client,
        project.project.id,
        step.id,
        worker.name,
        fact_description,
        source="bootstrap_conclude",
        phase_ms=conclude_ms,
    )


def _bootstrap_prompt_replacements(project: ProjectDetail, max_inline_hints: int) -> dict[str, str]:
    facts = {fact.id: fact.description for fact in project.facts}
    return {
        "origin": facts.get("origin", ""),
        "goal": facts.get("goal", ""),
        "hints": format_hints(build_focus_hints(project, max_inline_hints)),
    }


def _write_bootstrap_complete_result(
    client: ASTRAClient,
    project_id: str,
    step_id: str,
    worker_name: str,
    fact_description: str,
    complete_description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    conclude = write_conclude_result_with_fact_id(
        client,
        project_id,
        step_id,
        worker_name,
        fact_description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    )
    if conclude.status != "success":
        return "failed"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id project=%s step=%s worker=%s source=%s",
            project_id,
            step_id,
            worker_name,
            source,
        )
        return "success"

    response = client.complete(project_id, [conclude.fact_id], complete_description, worker_name)
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s step=%s worker=%s source=%s status=%s fact_id=%s",
            project_id,
            step_id,
            worker_name,
            source,
            response.status_code,
            conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s step=%s worker=%s source=%s fact_id=%s status=%s body=%s",
            project_id,
            step_id,
            worker_name,
            source,
            conclude.fact_id,
            response.status_code,
            response.text,
        )
        return "success"
    if total_ms is None:
        LOG.info(
            "bootstrap completed project=%s step=%s worker=%s source=%s from=%s phase_ms=%s",
            project_id,
            step_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
        )
    else:
        LOG.info(
            "bootstrap completed project=%s step=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
            project_id,
            step_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
            total_ms,
        )
    return "success"
