from __future__ import annotations

import logging
import time

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.context import build_focus_fact_ids, build_focus_open_steps
from astra.dispatcher.contracts import parse_json_output, validate_decide_payload
from astra.dispatcher.prompting import (
    format_fact_ids,
    format_json_block,
    load_prompt,
    render_prompt,
)
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.tasks.common import (
    best_effort_release_decide,
    cancel_reason,
    did_timeout,
    find_duplicate_fact,
    preview,
    record_failure_hint,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from astra.dispatcher.workers.registry import get_driver
from astra.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_decide_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
    lease_token: str | None = None,
) -> str:
    """Decide：串行、事件触发、干净上下文——只读图与操作图，不碰世界。

    每次触发从全新会话起跑（无历史对话注入），决策成本 O(图规模)。
    """
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_decide(client, project.project.id, worker.name, config.runtime.interval, lease_token)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "starting container exec project=%s worker=%s phase=decide_healthcheck timeout=%ss",
                project.project.id,
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
                    "decide cancelled during healthcheck project=%s worker=%s reason=%s",
                    project.project.id,
                    worker.name,
                    cancelled,
                )
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during decide healthcheck project=%s worker=%s status=%s",
                    project.project.id,
                    worker.name,
                    lease.failure.status_code,
                )
                return "failed"
            if healthcheck.result.returncode != 0:
                LOG.warning(
                    "worker unhealthy project=%s worker=%s healthcheck_ms=%s stderr=%s",
                    project.project.id,
                    worker.name,
                    healthcheck.duration_ms,
                    preview(healthcheck.result.stderr),
                )
                return "unhealthy"
        budget = config.runtime.context_budget
        open_steps = build_focus_open_steps(project, budget.max_inline_steps)
        allowed_fact_ids = build_focus_fact_ids(project, budget.max_inline_facts)
        LOG.debug(
            "decide context prepared project=%s worker=%s facts=%s focus_facts=%s hints=%s focus_open_steps=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_steps),
        )
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "decide.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="decide_execute",
                ),
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_steps": format_json_block(open_steps),
                "max_steps": str(config.tasks.decide.max_steps),
            },
        )

        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="decide_execute",
            timeout_seconds=config.tasks.decide.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "decide cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during decide project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "decide timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "decide command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            if preview(result.stderr):
                record_failure_hint(
                    client, project.project.id, "decide",
                    f"命令失败 code={result.returncode}: {preview(result.stderr, 200)}",
                )
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            kind, data = validate_decide_payload(
                payload, open_steps_empty=not open_steps, max_steps=config.tasks.decide.max_steps,
            )
        except Exception as exc:
            LOG.warning(
                "decide parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            record_failure_hint(
                client, project.project.id, "decide",
                f"决策输出解析失败: {exc}",
            )
            return "failed"
        if kind == "rejected":
            LOG.warning(
                "decide rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"
        if kind == "complete":
            # D2 复查（v0.2 复活修复）：decide 长跑（默认 900s）期间租约可能已过期——
            # 服务端已清 decide_worker，另一 decide 可并行认领。写图前复查，失效即放弃
            # 写入（否则与重派 decide 并发双写图；complete 由服务端原子守卫兜底 403）
            if lease.failure is not None:
                LOG.warning(
                    "decide lease lost before complete, aborting write project=%s worker=%s status=%s",
                    project.project.id,
                    worker.name,
                    lease.failure.status_code,
                )
                return "failed"
            response = client.complete(project.project.id, data["from"], data["description"], worker.name, lease_token)
            if response.status_code in (403, 404):
                LOG.info("project became inactive during decide complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning(
                    "decide complete write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                return "failed"
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                data["from"],
                execute_ms,
                total_ms,
            )
            return "success"
        if kind == "ops":
            assert isinstance(data, dict)
            created = 0
            closed = 0
            subgoals_added = 0
            subgoals_dropped = 0
            duplicate_skipped = 0

            for step_data in data["steps"]:
                if find_duplicate_fact(project, step_data["description"]) is not None:
                    duplicate_skipped += 1
                    LOG.info(
                        "decide step skipped as duplicate of existing fact project=%s from=%s description=%s",
                        project.project.id,
                        step_data["from"],
                        step_data["description"][:120],
                    )
                    continue
                # D2 复查：每个写操作前租约必须仍有效（长跑期间过期 → 放弃本轮写入）
                if lease.failure is not None:
                    LOG.warning(
                        "decide lease lost during ops, aborting remaining writes project=%s worker=%s created=%s",
                        project.project.id,
                        worker.name,
                        created,
                    )
                    return "success" if created else "failed"
                response = client.create_step(
                    project.project.id,
                    step_data["from"],
                    step_data["description"],
                    worker.name,
                    expect=step_data.get("expect"),
                )
                if response.status_code in (403, 404):
                    LOG.info("project became inactive during decide step create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("decide step lost race project=%s worker=%s from=%s", project.project.id, worker.name, step_data["from"])
                    continue
                if not response.ok:
                    LOG.warning(
                        "decide step write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
                    continue
                created += 1
                LOG.info(
                    "decide created step project=%s worker=%s from=%s description=%s",
                    project.project.id,
                    worker.name,
                    step_data["from"],
                    step_data["description"],
                )

            open_step_ids = {step.id for step in project.steps if step.to is None and step.status == "open"}
            for close_data in data["close_steps"]:
                if lease.failure is not None:
                    LOG.warning(
                        "decide lease lost during close ops, aborting project=%s worker=%s closed=%s",
                        project.project.id, worker.name, closed,
                    )
                    break
                step_id = close_data["id"]
                if step_id not in open_step_ids:
                    LOG.info(
                        "decide close skipped (not an open step) project=%s step=%s",
                        project.project.id,
                        step_id,
                    )
                    continue
                response = client.close_step(project.project.id, step_id, close_data.get("reason", ""))
                if not response.ok and response.status_code not in (403, 404, 409):
                    LOG.warning(
                        "decide close failed project=%s step=%s status=%s",
                        project.project.id,
                        step_id,
                        response.status_code,
                    )
                    continue
                closed += 1
                LOG.info(
                    "decide closed step project=%s step=%s reason=%s",
                    project.project.id,
                    step_id,
                    close_data.get("reason", ""),
                )

            for description in data["subgoals"]:
                response = client.create_subgoal(project.project.id, description)
                if response.ok:
                    subgoals_added += 1
                    LOG.info("decide added subgoal project=%s description=%s", project.project.id, description[:120])
                elif response.status_code not in (403, 404):
                    LOG.warning(
                        "decide subgoal write failed project=%s status=%s",
                        project.project.id,
                        response.status_code,
                    )

            known_subgoal_ids = {sg.id for sg in project.subgoals}
            for sg_id in data["drop_subgoals"]:
                if sg_id not in known_subgoal_ids:
                    continue
                response = client.update_subgoal_status(project.project.id, sg_id, "dropped")
                if response.ok:
                    subgoals_dropped += 1
                    LOG.info("decide dropped subgoal project=%s subgoal=%s", project.project.id, sg_id)

            LOG.info(
                "decide finished project=%s worker=%s created_steps=%s closed_steps=%s subgoals_added=%s subgoals_dropped=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                closed,
                subgoals_added,
                subgoals_dropped,
                execute_ms,
                total_ms,
            )
            if created == 0 and closed == 0 and subgoals_added == 0 and subgoals_dropped == 0:
                if duplicate_skipped > 0:
                    # 全部被去重跳过：方向已覆盖，属正常收敛而非失败（防重复侦察）
                    LOG.info(
                        "decide steps all deduplicated project=%s worker=%s skipped=%s execute_ms=%s total_ms=%s",
                        project.project.id, worker.name, duplicate_skipped, execute_ms, total_ms,
                    )
                    return "success"
                LOG.warning(
                    "decide applied no operations project=%s worker=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    execute_ms,
                    total_ms,
                )
                return "failed"
            return "success"
        LOG.info(
            "decide finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return "success"
    finally:
        lease.stop()
        best_effort_release_decide(client, project.project.id, worker.name, lease_token)
