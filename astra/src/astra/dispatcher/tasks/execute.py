from __future__ import annotations

import logging
import re
import time

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.contracts import (
    parse_json_output,
    validate_execute_payload,
)
from astra.dispatcher.prompting import load_prompt, render_prompt
from astra.dispatcher.context import _CRITICAL_RE
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.tasks.challenge import submit_critical_fact_audit
from astra.dispatcher.tasks.common import (
    best_effort_release,
    cancel_reason,
    did_timeout,
    find_duplicate_fact,
    preview,
    project_allows_conclude_fallback,
    record_failure_hint,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
    write_graph_snapshot_reference,
)
from astra.dispatcher.workers.registry import get_driver

from astra.server.models import ProjectDetail, Step

LOG = logging.getLogger(__name__)

# 负结果自动分流：从事实描述推断 kind——"此路不通/已穷尽/无结果"类
# 发现标记为 negative，焦点裁剪时加权保活（防同类死路被反复开步骤）。
_NEGATIVE_RE = re.compile(
    r"(?i)(此路不通|已穷尽|无结果|未发现|不存在|已排除|dead.?end|exhausted|"
    r"no.?result|not.?found|ruled.?out|deadend|穷尽|排除|无可用|无有效|zero.?hit)"
)


def _infer_fact_kind(description: str) -> str:
    """正/负结果自动分流：负面结论 → negative kind（与 regular 同权存储）。"""
    if _NEGATIVE_RE.search(description or ""):
        return "negative"
    return "regular"


def _should_write_fact(
    client: ASTRAClient,
    project: ProjectDetail,
    description: str,
) -> bool:
    """写回前把关：事实去重（防重复侦察）。确认责任在执行者自证（自证写回语义）。"""
    duplicate = find_duplicate_fact(project, description)
    if duplicate is not None:
        LOG.info(
            "execute fact duplicate skipped project=%s similar_fact=%s description=%s",
            project.project.id, duplicate.id, description[:120],
        )
        record_failure_hint(
            client, project.project.id, "execute",
            f"执行发现与既有事实 {duplicate.id} 重复，未重复写回；请在该事实基础上继续深化",
        )
        return False
    return True


def _rescue_streamed_facts(client: ASTRAClient, project: ProjectDetail, step: Step, stdout: str) -> int:
    """超时/解析失败时从 stdout 抢救流式天枢行（pi 会话落盘不稳定，stdout 才是可靠载体）。

    兼容两种行格式：bootstrap 形（{"fact":{"description":...}}）与 execute 形
    （{"description":...}）。抢救的行直接 create_fact 入图（步骤本体仍走原收束/回队路径）。
    返回抢救条数。条数封顶 MAX_STREAM_FACTS（审计17轮：万行倾倒防护）。
    """
    import json as _json

    from astra.dispatcher.contracts import MAX_STREAM_FACTS

    rescued = 0
    for line in (stdout or "").splitlines():
        if rescued >= MAX_STREAM_FACTS:
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        desc = None
        fact = data.get("fact")
        if isinstance(fact, dict) and isinstance(fact.get("description"), str):
            desc = fact["description"]
        elif isinstance(data.get("description"), str):
            desc = data["description"]
        if not desc or not desc.strip() or "flag{" not in desc and len(desc.strip()) < 20:
            continue  # 抢救只收有分量的确认发现
        if find_duplicate_fact(project, desc) is not None:
            continue
        response = client.create_fact(project.project.id, desc.strip(), kind=_infer_fact_kind(desc), creator="astra.rescue")
        if response.ok:
            rescued += 1
            LOG.info("rescued streamed fact project=%s step=%s desc=%.80s", project.project.id, step.id, desc)
    return rescued


def run_execute_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
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
                "starting container exec project=%s step=%s worker=%s phase=execute_healthcheck timeout=%ss",
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
                    "execute cancelled during healthcheck project=%s step=%s worker=%s reason=%s",
                    project.project.id,
                    step.id,
                    worker.name,
                    cancelled,
                )
                best_effort_release(client, project.project.id, step.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during execute healthcheck project=%s step=%s worker=%s status=%s",
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
            load_prompt(config.runtime.prompt_group, "execute.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="execute_execute",
                ),
                "step_id": step.id,
                "step_description": step.description,
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
            phase="execute_execute",
            timeout=config.tasks.execute.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "execute cancelled project=%s step=%s worker=%s reason=%s execute_ms=%s",
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
                "heartbeat lost during execute project=%s step=%s worker=%s status=%s execute_ms=%s",
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
                payload = parse_json_output(model_output)
                kind, data = validate_execute_payload(payload)
                description = data["description"] if data else None
                finding = data["finding"] if data else None
            except Exception as exc:
                LOG.warning(
                    "execute parse failed project=%s step=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
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
                    project.project.id,
                    step,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                )
            if kind == "rejected":
                LOG.warning(
                    "execute rejected project=%s step=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    step.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, step.id, worker.name)
                return "rejected"
            if not _should_write_fact(client, client.get_project(project.project.id), description):
                best_effort_release(client, project.project.id, step.id, worker.name)
                return "success"
            conclude_status = write_conclude_result(
                client,
                project.project.id,
                step.id,
                worker.name,
                description,
                source="execute_execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
                kind=_infer_fact_kind(description),
                finding=finding,
            )
            # 质询星探·关键事实审计：凭据/flag 级发现入图后异步对抗审查
            # （不阻塞旗提交；质疑成立写 hint 留痕，决策链可回放）
            if conclude_status == "success" and _CRITICAL_RE.search(description or ""):
                submit_critical_fact_audit(
                    config,
                    client,
                    container_manager,
                    project.project.id,
                    export_yaml,
                    worker,
                    description,
                )
            return conclude_status
        if did_timeout(first):
            LOG.warning(
                "execute timed out project=%s step=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                step.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            # 流式抢救：超时前已输出的确认发现先入图（不依赖会话续接）。
            # 去重必须对新鲜快照做——派发时的 project 快照可能已落后于并行 worker 的写入
            try:
                fresh = client.get_project(project.project.id)
                rescued = _rescue_streamed_facts(client, fresh, step, first.stdout or "")
                if rescued:
                    LOG.info("execute timeout rescue project=%s step=%s rescued_facts=%s", project.project.id, step.id, rescued)
            except Exception as exc:  # noqa: BLE001 —— 抢救失败不阻塞 fallback
                LOG.warning("execute rescue failed project=%s error=%s", project.project.id, exc)
            return _try_conclude_fallback(
                config,
                client,
                container_manager,
                container_name,
                worker,
                driver,
                project.project.id,
                step,
                export_yaml,
                session,
                lease,
                cancellation,
            )
        LOG.warning(
            "execute command failed project=%s step=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            step.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        if preview(first.stderr):
            record_failure_hint(
                client, project.project.id, "execute",
                f"执行命令失败 code={first.returncode}（步骤: {step.description[:80]}）: {preview(first.stderr, 200)}",
            )
        best_effort_release(client, project.project.id, step.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("execute task crashed project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
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
    project_id: str,
    step: Step,
    export_yaml: str,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "conclude fallback unavailable project=%s step=%s worker=%s supports_conclude=%s has_session=%s",
            project_id,
            step.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning("conclude fallback skipped because heartbeat already lost project=%s step=%s worker=%s", project_id, step.id, worker.name)
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "conclude fallback skipped because task was cancelled project=%s step=%s worker=%s reason=%s",
            project_id,
            step.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project_id,
        worker_name=worker.name,
        step_id=step.id,
    ):
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project_id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "execute_conclude.md"),
        {
            "graph_yaml": write_graph_snapshot_reference(
                container_manager,
                container_name,
                export_yaml.strip(),
                phase="execute_conclude",
            ),
            "step_id": step.id,
            "step_description": step.description,
        },
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting conclude fallback project=%s step=%s worker=%s", project_id, step.id, worker.name)
    conclude_started = time.perf_counter()
    result = _run_process(
        container_manager,
        container_name,
        worker,
        conclude_argv,
        phase="execute_conclude",
        timeout=config.tasks.execute.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "conclude cancelled project=%s step=%s worker=%s reason=%s conclude_ms=%s",
            project_id,
            step.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "conclude failed project=%s step=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            step.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, data = validate_execute_payload(payload)
        description = data["description"] if data else None
        finding = data["finding"] if data else None
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s step=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            step.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "conclude rejected project=%s step=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project_id,
            step.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "rejected"
    if not _should_write_fact(client, client.get_project(project_id), description):
        best_effort_release(client, project_id, step.id, worker.name)
        return "success"
    conclude_status = write_conclude_result(
        client,
        project_id,
        step.id,
        worker.name,
        description,
        source="execute_conclude",
        phase_ms=conclude_ms,
        finding=finding,
    )
    # 质询星探·关键事实审计（conclude 兜底路径同样把关）
    if conclude_status == "success" and _CRITICAL_RE.search(description or ""):
        submit_critical_fact_audit(
            config,
            client,
            container_manager,
            project_id,
            export_yaml,
            worker,
            description,
        )
    return conclude_status


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
