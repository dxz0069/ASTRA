"""Completion checks over persisted graph and lane records.

These checks only establish whether known work has finished. A caller must
separately establish that the project goal was achieved before completing it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from astra.server.models import ProjectDetail


GateStatus = Literal["complete", "incomplete", "blocked"]


@dataclass(frozen=True, slots=True)
class CompletionGateResult:
    status: GateStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PersistedLaneRun:
    lane_id: str
    run_id: str
    status: Literal["queued", "running", "completed", "failed", "blocked"]


@dataclass(frozen=True, slots=True)
class PersistedLaneArtifact:
    lane_id: str
    run_id: str
    status: Literal["writing", "completed"]
    payload: Mapping[str, Any]


def check_project_work_completion(project: ProjectDetail) -> CompletionGateResult:
    """Reject a complete claim while graph work or verification is unresolved.

    A closed Step without a result is intentionally abandoned and does not
    block completion. A concluded Step has a ``to`` fact even if its status
    remains ``open`` in the current schema.
    """
    incomplete: list[str] = []
    blocked: list[str] = []
    steps = {step.id: step for step in project.steps}
    for step in project.steps:
        if step.to is None and step.status == "open":
            incomplete.append(f"step:{step.id}:open")
        elif step.to is None and step.status == "closed" and not step.close_reason:
            blocked.append(f"step:{step.id}:closed_without_reason")

    for finding in project.findings:
        if finding.verification_status == "blocked":
            blocked.append(f"finding:{finding.id}:verification_blocked")
        elif finding.verification_status == "pending":
            verification_step = steps.get(finding.verification_step_id or "")
            if verification_step is None or verification_step.task_type != "strike":
                blocked.append(f"finding:{finding.id}:verification_step_missing")
            elif verification_step.to is not None or verification_step.status != "open":
                blocked.append(f"finding:{finding.id}:verification_step_inconsistent")
            else:
                incomplete.append(f"finding:{finding.id}:verification_pending")
        elif finding.high_value and finding.verification_status == "not_requested":
            blocked.append(f"finding:{finding.id}:verification_not_requested")

    if blocked:
        return CompletionGateResult("blocked", tuple(blocked + incomplete))
    if incomplete:
        return CompletionGateResult("incomplete", tuple(incomplete))
    return CompletionGateResult("complete")


def check_lane_completion(
    expected_lane_ids: Sequence[str],
    runs: Mapping[str, PersistedLaneRun],
    artifacts: Mapping[str, PersistedLaneArtifact],
) -> CompletionGateResult:
    """Require a finished run and its matching finalized artifact for each lane.

    ``runs`` and ``artifacts`` are records loaded from durable storage by the
    caller, keyed by lane ID. Worker text alone is not a completion record.
    An explicit ``findings: []`` is a valid completed artifact.
    """
    if not expected_lane_ids or len(set(expected_lane_ids)) != len(expected_lane_ids):
        return CompletionGateResult("blocked", ("invalid_expected_lanes",))
    incomplete: list[str] = []
    blocked: list[str] = []
    for lane_id in expected_lane_ids:
        if not lane_id:
            blocked.append("empty_lane_id")
            continue
        run = runs.get(lane_id)
        if run is None:
            incomplete.append(f"lane:{lane_id}:run_missing")
            continue
        if run.lane_id != lane_id or not run.run_id:
            blocked.append(f"lane:{lane_id}:run_identity_invalid")
            continue
        if run.status in ("failed", "blocked"):
            blocked.append(f"lane:{lane_id}:run_{run.status}")
            continue
        if run.status in ("queued", "running"):
            incomplete.append(f"lane:{lane_id}:run_{run.status}")
            continue
        if run.status != "completed":
            blocked.append(f"lane:{lane_id}:run_status_invalid")
            continue
        artifact = artifacts.get(lane_id)
        if artifact is None:
            blocked.append(f"lane:{lane_id}:artifact_missing")
            continue
        if artifact.lane_id != lane_id or artifact.run_id != run.run_id:
            blocked.append(f"lane:{lane_id}:artifact_identity_mismatch")
            continue
        if artifact.status == "writing":
            incomplete.append(f"lane:{lane_id}:artifact_writing")
            continue
        if artifact.status != "completed":
            blocked.append(f"lane:{lane_id}:artifact_status_invalid")
            continue
        if not isinstance(artifact.payload, Mapping):
            blocked.append(f"lane:{lane_id}:artifact_payload_invalid")
            continue
        findings = artifact.payload.get("findings")
        if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
            blocked.append(f"lane:{lane_id}:artifact_invalid_findings")
    if blocked:
        return CompletionGateResult("blocked", tuple(blocked + incomplete))
    if incomplete:
        return CompletionGateResult("incomplete", tuple(incomplete))
    return CompletionGateResult("complete")
