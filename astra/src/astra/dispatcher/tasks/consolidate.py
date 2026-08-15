from __future__ import annotations

"""星尘记忆整理（consolidate）任务：把超预算的旧星记压缩为摘要星记。

触发条件由调度器判断（regular 星记数 > context_budget.max_inline_facts * 2）。
任务本身只做：选择最老的一批 regular 星记 → 轻量模型压缩为一条摘要星记 → 写回星图。
整理失败不阻断主流程（本次跳过，下次触发时再试）。
"""

import logging
import time

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.contracts import parse_json_output, validate_consolidate_payload
from astra.dispatcher.prompting import format_json_block, load_prompt, render_prompt
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.tasks.common import preview, run_worker_process
from astra.dispatcher.workers.registry import get_driver
from astra.server.models import ProjectDetail

LOG = logging.getLogger(__name__)

SUMMARY_KIND = "summary"


def pick_stale_facts(project: ProjectDetail, batch_size: int) -> list[dict[str, str]]:
    """选择最老的一批可压缩星记。

    排除 goal/origin/摘要星记，以及仍被 intent.to 引用的星记（服务端 archive
    同样拒绝回收它们——intent.to 悬挂会让前端建边抛异常、导出数据不一致）。
    保持星图顺序取前 batch_size 条。
    """
    referenced = {intent.to for intent in project.intents if intent.to}
    stale = [
        {"id": fact.id, "description": fact.description}
        for fact in project.facts
        if fact.id not in ("goal", "origin")
        and fact.id not in referenced
        and fact.kind != SUMMARY_KIND
    ]
    return stale[:batch_size]


def run_consolidate_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """consolidate 入口：异常兜底（对齐其他任务——崩溃返回 failed 进冷却，
    避免容器/Docker 故障时每个调度周期反复重派崩溃刷屏）。"""
    try:
        return _run_consolidate_task(
            config, client, container_manager, project, export_yaml, worker, cancellation
        )
    except Exception:
        LOG.exception(
            "consolidate crashed project=%s worker=%s", project.project.id, worker.name
        )
        return "failed"


def _run_consolidate_task(
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
    container_name = container_manager.ensure_running(project.project.id)

    budget = config.runtime.context_budget
    stale_facts = pick_stale_facts(project, budget.max_inline_facts)
    if not stale_facts:
        LOG.info("consolidate skipped project=%s worker=%s reason=no_stale_facts", project.project.id, worker.name)
        return "noop"

    goal = next((fact.description for fact in project.facts if fact.id == "goal"), "")
    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "consolidate.md"),
        {
            "goal": goal,
            "stale_facts": format_json_block(stale_facts),
        },
    )

    session = driver.prepare_session()
    command = driver.build_execute(worker, prompt, session)
    result = run_worker_process(
        container_manager,
        container_name,
        worker,
        command.argv,
        phase="consolidate_execute",
        timeout_seconds=config.tasks.consolidate.timeout,
        cancellation=cancellation,
    )
    execute_ms = int((time.perf_counter() - task_started) * 1000)
    if result.cancelled:
        LOG.info("consolidate cancelled project=%s worker=%s execute_ms=%s", project.project.id, worker.name, execute_ms)
        return "cancelled"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "consolidate command failed project=%s worker=%s code=%s execute_ms=%s stderr=%s",
            project.project.id,
            worker.name,
            result.returncode,
            execute_ms,
            preview(result.stderr),
        )
        return "failed"

    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        outcome, description = validate_consolidate_payload(payload)
    except ValueError as exc:
        LOG.warning(
            "consolidate payload invalid project=%s worker=%s error=%s stdout=%s",
            project.project.id,
            worker.name,
            exc,
            preview(result.stdout),
        )
        return "failed"
    if outcome != "fact" or description is None:
        LOG.info("consolidate rejected project=%s worker=%s", project.project.id, worker.name)
        return "rejected"

    response = client.create_fact(project.project.id, description, kind=SUMMARY_KIND, creator=worker.name)
    if response.status_code >= 400:
        LOG.warning(
            "consolidate write failed project=%s worker=%s status=%s",
            project.project.id,
            worker.name,
            response.status_code,
        )
        return "failed"
    # 回收被压缩的原始星记，防止 summary 与原文重复占用预算（只增不减会反复触发整理）
    stale_ids = [item["id"] for item in stale_facts]
    archive = client.archive_facts(project.project.id, stale_ids)
    if archive.status_code >= 400:
        LOG.warning(
            "consolidate archive failed project=%s worker=%s status=%s（摘要已写回，原文未回收）",
            project.project.id,
            worker.name,
            archive.status_code,
        )
    LOG.info(
        "consolidate done project=%s worker=%s facts_compressed=%s execute_ms=%s",
        project.project.id,
        worker.name,
        len(stale_facts),
        execute_ms,
    )
    return "success"
