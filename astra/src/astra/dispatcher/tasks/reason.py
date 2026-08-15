from __future__ import annotations

import logging
import re
import time
from typing import Any

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.context import build_focus_fact_ids, build_focus_open_intents
from astra.dispatcher.contracts import (
    parse_json_output,
    validate_challenge_payload,
    validate_reason_payload,
    validate_verdict_payload,
)
from astra.dispatcher.prompting import (
    format_fact_ids,
    format_json_block,
    format_open_intents,
    load_prompt,
    render_prompt,
)
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.tasks.common import (
    REVIEW_HINT_PREFIX,
    best_effort_release_reason,
    cancel_reason,
    did_timeout,
    find_duplicate_fact,
    preview,
    record_failure_hint,
    review_graph_summary,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from astra.dispatcher.workers.registry import get_driver
from astra.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def _resolve_review_worker(config: DispatchConfig, worker: WorkerConfig) -> tuple[WorkerConfig, Any]:
    """选择审查阶段（challenge/verdict）的执行 worker 与 driver。

    审查对输出契约稳定性要求高，且不再硬编码 claude 可执行文件——命令一律由
    driver 构造。优先使用产生提案的 worker 自身 driver；若该 driver 声明
    不支持审查（supports_review()=False，如 pi 实测偶发提前退出），则回退到
    配置中的 claudecode worker（如有，其 driver 输出契约实测稳定）；否则仍用
    自身驱动（能力降级，由链路的重试 + 降级放行兜底）。
    """
    driver = get_driver(worker.type)
    if getattr(driver, "supports_review", lambda: True)():
        return worker, driver
    fallback = next((w for w in config.workers if w.type == "claudecode"), None)
    if fallback is not None:
        LOG.info(
            "review falls back to claudecode worker=%s reason=driver_%s_lacks_review_support",
            fallback.name,
            worker.type,
        )
        return fallback, get_driver(fallback.type)
    LOG.warning(
        "review driver %s lacks review support and no claudecode worker is configured; using it anyway",
        worker.type,
    )
    return worker, driver


def dual_star_review(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    project: ProjectDetail,
    export_yaml: str,
    kind: str,
    data: dict[str, Any] | list[dict[str, Any]],
    cancellation: TaskCancellation,
) -> bool | None:
    """双星决策：对定航提案先质询（challenge）再裁决（verdict）。

    返回 True=通过（可写回）；False=被质询/裁决否决；None=审查链路失败（不应写回）。
    """
    proposal: dict[str, Any] = {"complete": data} if kind == "complete" else {"intents": data}
    goal = next((fact.description for fact in project.facts if fact.id == "goal"), "")
    graph_ref = write_graph_snapshot_reference(
        container_manager,
        container_name,
        export_yaml.strip(),
        phase="review",
    )
    review_worker, driver = _resolve_review_worker(config, worker)
    # 审查读图提速（候选 20）：文件路径引用 + 紧凑星图摘要
    graph_context = graph_ref + "\n\n" + review_graph_summary(project)
    common = {"graph_yaml": graph_context, "goal": goal, "proposal": format_json_block(proposal)}

    # 机器预审：归航提案引用的证据含低置信星记 → 直接否决（低置信不作完成证据）
    if kind == "complete" and isinstance(data, dict):
        from_ids = set(data.get("from", []))
        low_evidence = [
            f.id for f in project.facts
            if f.id in from_ids and f.confidence == "low"
        ]
        if low_evidence:
            LOG.info(
                "complete rejected by machine precheck project=%s low_confidence_evidence=%s",
                project.project.id,
                low_evidence,
            )
            record_failure_hint(
                client, project.project.id, "review",
                f"归航提案引用的证据星记置信度为 low（{low_evidence}），不能作为完成依据；请先用巡猎补强证据或换方向",
                prefix=REVIEW_HINT_PREFIX,
            )
            return False

        # 机器预审（Linghun 声明契约）：complete 声称已获取 flag，但星图无任何 flag 星记 → 否决
        if re.search(r"flag", data.get("description", ""), re.IGNORECASE):
            has_flag_fact = any(
                re.search(r"flag\{", f.description, re.IGNORECASE)
                for f in project.facts if f.id not in ("goal", "origin")
            )
            if not has_flag_fact:
                LOG.info(
                    "complete rejected by machine precheck project=%s reason=flag_claimed_without_evidence",
                    project.project.id,
                )
                record_failure_hint(
                    client, project.project.id, "review",
                    "归航声明提到 flag，但星图没有任何 flag{...} 星记；请先以巡猎写回带证据的 flag 星记再归航",
                    prefix=REVIEW_HINT_PREFIX,
                )
                return False

    challenge_payload = _run_review_stage_with_retry(
        config, container_manager, container_name, review_worker, driver,
        "challenge", common, cancellation,
    )
    if challenge_payload is None:
        LOG.warning(
            "challenge unavailable after retry project=%s worker=%s（降级放行）",
            project.project.id,
            worker.name,
        )
        record_failure_hint(
            client, project.project.id, "review",
            "质询链路不可用（重试后仍失败），本次提案未经质询放行；请在后续定航中自行核对方向",
            prefix=REVIEW_HINT_PREFIX,
        )
        return True
    try:
        outcome, challenge_result = validate_challenge_payload(challenge_payload)
    except ValueError as exc:
        LOG.warning("challenge payload invalid project=%s worker=%s error=%s", project.project.id, worker.name, exc)
        return None
    if outcome != "accepted":
        LOG.info(
            "proposal challenged down project=%s worker=%s kind=%s reason=%s",
            project.project.id,
            worker.name,
            kind,
            challenge_payload.get("reason", ""),
        )
        return False

    verdict_payload = _run_review_stage_with_retry(
        config, container_manager, container_name, review_worker, driver,
        "verdict", {**common, "challenge": format_json_block(challenge_result)}, cancellation,
    )
    if verdict_payload is None:
        LOG.warning(
            "verdict unavailable after retry project=%s worker=%s（降级放行）",
            project.project.id,
            worker.name,
        )
        record_failure_hint(
            client, project.project.id, "review",
            "裁决链路不可用（重试后仍失败），本次提案未经裁决放行；请在后续定航中自行核对方向",
            prefix=REVIEW_HINT_PREFIX,
        )
        return True
    try:
        verdict_kind, _ = validate_verdict_payload(verdict_payload, expected_kind=kind)
    except ValueError as exc:
        LOG.warning("verdict payload invalid project=%s worker=%s error=%s", project.project.id, worker.name, exc)
        return None
    if verdict_kind != kind:
        LOG.info(
            "verdict rejected proposal project=%s worker=%s kind=%s reason=%s",
            project.project.id,
            worker.name,
            kind,
            verdict_payload.get("reason", ""),
        )
        return False
    return True


def _run_review_stage_with_retry(
    config: DispatchConfig,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    phase: str,
    replacements: dict[str, str],
    cancellation: TaskCancellation,
) -> dict[str, Any] | None:
    """审查阶段执行 + 一次重试（pi 偶发提前退出，重试提升稳定性）。"""
    payload = _run_review_stage(
        config, container_manager, container_name, worker, driver,
        phase, replacements, cancellation,
    )
    if payload is None:
        LOG.warning("review stage retry worker=%s phase=%s", worker.name, phase)
        payload = _run_review_stage(
            config, container_manager, container_name, worker, driver,
            phase, replacements, cancellation,
        )
    return payload


def _run_review_stage(
    config: DispatchConfig,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    phase: str,
    replacements: dict[str, str],
    cancellation: TaskCancellation,
) -> dict[str, Any] | None:
    """执行一次审查阶段（challenge/verdict），返回解析后的 payload；失败返回 None。

    命令一律由 driver.build_execute 构造（与主任务链路一致），不再硬编码
    claude 可执行文件；审查 worker 由 _resolve_review_worker 选定——pi 等
    输出契约不稳定的驱动会回退到 claudecode worker。
    """
    prompt = render_prompt(load_prompt(config.runtime.prompt_group, f"{phase}.md"), replacements)
    session = driver.prepare_session()
    command = driver.build_execute(worker, prompt, session)
    result = run_worker_process(
        container_manager,
        container_name,
        worker,
        command.argv,
        phase=f"{phase}_execute",
        timeout_seconds=config.tasks.challenge.timeout,
        cancellation=cancellation,
    )
    if result.cancelled or result.timed_out or result.returncode != 0:
        LOG.warning(
            "review stage failed worker=%s phase=%s code=%s stderr=%s",
            worker.name,
            phase,
            result.returncode,
            preview(result.stderr),
        )
        return None
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
    except ValueError as exc:
        LOG.warning(
            "review stage parse failed worker=%s phase=%s error=%s stdout=%s",
            worker.name,
            phase,
            exc,
            preview(result.stdout, 600),
        )
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("accepted"), bool):
        LOG.warning(
            "review stage payload invalid worker=%s phase=%s payload=%s",
            worker.name,
            phase,
            preview(result.stdout, 600),
        )
        return None
    return payload


def run_reason_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "starting container exec project=%s worker=%s phase=reason_healthcheck timeout=%ss",
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
                    "reason cancelled during healthcheck project=%s worker=%s reason=%s",
                    project.project.id,
                    worker.name,
                    cancelled,
                )
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during reason healthcheck project=%s worker=%s status=%s",
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
        open_intents = build_focus_open_intents(project, budget.max_inline_intents)
        allowed_fact_ids = build_focus_fact_ids(project, budget.max_inline_facts)
        LOG.debug(
            "reason context prepared project=%s worker=%s facts=%s focus_facts=%s hints=%s focus_open_intents=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_intents),
        )
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "reason.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="reason_execute",
                ),
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_intents": format_open_intents(open_intents),
                "max_intents": str(config.tasks.reason.max_intents),
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
            phase="reason_execute",
            timeout_seconds=config.tasks.reason.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
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
                "reason command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
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
                    client, project.project.id, "reason",
                    f"命令失败 code={result.returncode}: {preview(result.stderr, 200)}",
                )
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            kind, data = validate_reason_payload(
                payload, open_intents_empty=not open_intents, max_intents=config.tasks.reason.max_intents,
            )
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            record_failure_hint(
                client, project.project.id, "reason",
                f"定航输出解析失败: {exc}",
            )
            return "failed"
        if kind == "rejected":
            LOG.warning(
                "reason rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"
        if kind == "complete":
            review = dual_star_review(
                config, client, container_manager, container_name, worker, project,
                export_yaml, "complete", data, cancellation,
            )
            if review is False:
                LOG.info(
                    "proposal challenged before write project=%s worker=%s kind=complete",
                    project.project.id,
                    worker.name,
                )
                if not record_failure_hint(
                    client, project.project.id, "review",
                    "归航提案（complete）被质询/裁决否决，未写回星图；请重新核对目标达成证据后重新定航",
                    prefix=REVIEW_HINT_PREFIX,
                ):
                    return "failed"
                return "success"
            if review is None:
                LOG.warning(
                    "review unavailable before complete project=%s worker=%s（不写回，等待下次定航）",
                    project.project.id,
                    worker.name,
                )
                if not record_failure_hint(
                    client, project.project.id, "review",
                    "归航提案审查链路不可用（超时/解析失败），未写回；请重新核对证据后重新定航",
                    prefix=REVIEW_HINT_PREFIX,
                ):
                    return "failed"
                return "success"
            response = client.complete(project.project.id, data["from"], data["description"], worker.name)
            if response.status_code in (403, 404):
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning(
                    "reason complete write failed project=%s worker=%s status=%s body=%s",
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
        if kind == "intents":
            review = dual_star_review(
                config, client, container_manager, container_name, worker, project,
                export_yaml, "intents", data, cancellation,
            )
            if review is False:
                LOG.info(
                    "proposal challenged before write project=%s worker=%s kind=intents",
                    project.project.id,
                    worker.name,
                )
                if not record_failure_hint(
                    client, project.project.id, "review",
                    "新航向提案（intents）被质询/裁决否决，未写回星图；请换方向或补充证据",
                    prefix=REVIEW_HINT_PREFIX,
                ):
                    return "failed"
                return "success"
            if review is None:
                LOG.warning(
                    "review unavailable before intents project=%s worker=%s（不写回，等待下次定航）",
                    project.project.id,
                    worker.name,
                )
                if not record_failure_hint(
                    client, project.project.id, "review",
                    "新航向提案审查链路不可用（超时/解析失败），未写回；请换方向或补充证据",
                    prefix=REVIEW_HINT_PREFIX,
                ):
                    return "failed"
                return "success"
            created = 0
            duplicate_skipped = 0
            for intent_data in data:
                if find_duplicate_fact(project, intent_data["description"]) is not None:
                    duplicate_skipped += 1
                    LOG.info(
                        "reason intent skipped as duplicate of existing fact project=%s from=%s description=%s",
                        project.project.id,
                        intent_data["from"],
                        intent_data["description"][:120],
                    )
                    continue
                response = client.create_intent(project.project.id, intent_data["from"], intent_data["description"], worker.name)
                if response.status_code in (403, 404):
                    LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                    continue
                if not response.ok:
                    LOG.warning(
                        "reason intent write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
                    continue
                created += 1
                LOG.info(
                    "reason created intent project=%s worker=%s from=%s description=%s",
                    project.project.id,
                    worker.name,
                    intent_data["from"],
                    intent_data["description"],
                )
            LOG.info(
                "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                len(data),
                execute_ms,
                total_ms,
            )
            if created == 0:
                if duplicate_skipped > 0:
                    # 全部被去重跳过：方向已覆盖，属正常收敛而非失败（防重复侦察）
                    LOG.info(
                        "reason intents all deduplicated project=%s worker=%s skipped=%s execute_ms=%s total_ms=%s",
                        project.project.id, worker.name, duplicate_skipped, execute_ms, total_ms,
                    )
                    return "success"
                LOG.warning(
                    "reason created no intents project=%s worker=%s attempted=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    len(data),
                    execute_ms,
                    total_ms,
                )
                return "failed"
            return "success"
        LOG.info(
            "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return "success"
    finally:
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name)
