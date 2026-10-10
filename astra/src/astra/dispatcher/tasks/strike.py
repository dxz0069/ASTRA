from __future__ import annotations

import logging
import time

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.contracts import parse_json_output, validate_strike_payload
from astra.dispatcher.evidence_collector import collect_scoped_request_evidence, resolve_evidence_refs
from astra.dispatcher.prompting import format_json_block, load_prompt, render_prompt
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.tasks.common import (
    best_effort_release,
    cancel_reason,
    did_timeout,
    preview,
    run_healthcheck,
    run_worker_process,
    run_worker_process_with_retry,
    task_healthcheck_enabled,
    write_conclude_result,
    write_graph_snapshot_reference,
)
from astra.dispatcher.workers.registry import get_driver
from astra.server.models import ProjectDetail, Step

LOG = logging.getLogger(__name__)


def run_strike_task(
    config: DispatchConfig,
    client: ASTRAClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    step: Step,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """Independently verify a queued Finding and conclude it atomically with a Fact."""
    project_id = project.project.id
    finding_id = step.finding_id
    lease = HeartbeatLease.for_step(client, project_id, step.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        finding = next((item for item in project.findings if item.id == finding_id), None)
        if finding is None or getattr(finding, "verification_status", "pending") != "pending":
            LOG.warning("strike has no pending finding project=%s step=%s finding=%s", project_id, step.id, finding_id)
            best_effort_release(client, project_id, step.id, worker.name)
            return "failed"

        driver = get_driver(worker.type)
        container_name = container_manager.ensure_running(project_id)
        if task_healthcheck_enabled(config):
            healthcheck = run_healthcheck(
                container_manager,
                container_name,
                worker,
                driver.build_healthcheck(worker),
                timeout_seconds=config.runtime.healthcheck_timeout,
                lease=lease,
                cancellation=cancellation,
            )
            if cancel_reason(healthcheck.result, cancellation) is not None:
                best_effort_release(client, project_id, step.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                best_effort_release(client, project_id, step.id, worker.name)
                return "failed"
            if healthcheck.result.returncode != 0:
                LOG.warning("strike worker unhealthy project=%s step=%s worker=%s stderr=%s", project_id, step.id, worker.name, preview(healthcheck.result.stderr))
                best_effort_release(client, project_id, step.id, worker.name)
                return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "strike.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager, container_name, export_yaml.strip(), phase="strike"
                ),
                "step_id": step.id,
                "finding_id": finding.id,
                "finding_description": format_json_block(finding.description),
            },
        )
        command = driver.build_strike(worker, prompt, driver.prepare_session())
        started = time.perf_counter()
        result = run_worker_process_with_retry(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="strike",
            timeout_seconds=config.tasks.strike.timeout,
            lease=lease,
            cancellation=cancellation,
            runner=run_worker_process,
            project_id=project_id,
            step_id=step.id,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        if cancel_reason(result, cancellation) is not None:
            best_effort_release(client, project_id, step.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            best_effort_release(client, project_id, step.id, worker.name)
            return "failed"
        if did_timeout(result) or result.returncode != 0:
            LOG.warning(
                "strike command failed project=%s step=%s worker=%s code=%s timed_out=%s stderr=%s",
                project_id, step.id, worker.name, result.returncode, did_timeout(result), preview(result.stderr),
            )
            best_effort_release(client, project_id, step.id, worker.name)
            return "failed"
        imported_evidence = collect_scoped_request_evidence(
            client, worker, project_id, step.id, result
        )
        try:
            payload = parse_json_output(driver.extract_response_text(result.stdout, result.stderr))
            verdict, summary, requested_evidence_refs = validate_strike_payload(payload)
            evidence_refs = resolve_evidence_refs(requested_evidence_refs, imported_evidence)
        except ValueError as exc:
            LOG.warning("strike response invalid project=%s step=%s worker=%s error=%s stdout=%s", project_id, step.id, worker.name, exc, preview(result.stdout))
            best_effort_release(client, project_id, step.id, worker.name)
            return "failed"
        # The verification Fact describes what was checked; the Finding is updated
        # by the same conclude transaction. This path never creates another Finding.
        description = f"Strike verification of Finding {finding.id}: {verdict}. {summary}"
        return write_conclude_result(
            client,
            project_id,
            step.id,
            worker.name,
            description,
            source="strike",
            phase_ms=duration_ms,
            kind="negative" if verdict == "refuted" else "regular",
            verification_status=verdict,
            verification_summary=summary,
            evidence_refs=evidence_refs,
        )
    except Exception:
        LOG.exception("strike task crashed project=%s step=%s worker=%s", project_id, step.id, worker.name)
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    finally:
        lease.stop()
