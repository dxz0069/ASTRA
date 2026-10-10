from __future__ import annotations

import pytest

from astra.dispatcher.completion_gate import (
    PersistedLaneArtifact,
    PersistedLaneRun,
    check_lane_completion,
    check_project_work_completion,
)
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks import decide
from astra.server.finding_identity import (
    deduplicate_finding_candidates,
    find_duplicate_finding,
    finding_fingerprint,
)
from astra.server.models import Finding

from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_project, make_step


def _candidate(**changes: str) -> dict[str, str]:
    candidate = {
        "asset_origin": "https://example.test",
        "entry_point": "GET /api/orders/{id}",
        "category": "IDOR",
        "root_cause": "missing owner check",
        "impact": "read another user's order",
        "conditions": "authenticated ordinary account",
        "description": "Order access defect",
    }
    candidate.update(changes)
    return candidate


def test_fingerprint_uses_structured_identity_not_presentation_text() -> None:
    first = _candidate()
    renamed = _candidate(description="A completely different title")
    reversed_order = dict(reversed(list(renamed.items())))

    assert finding_fingerprint(first) == finding_fingerprint(reversed_order)
    assert find_duplicate_finding(renamed, [first]) is first
    assert deduplicate_finding_candidates([first, renamed]) == [first]


@pytest.mark.parametrize("field,value", [
    ("asset_origin", "https://other.test"),
    ("entry_point", "POST /api/orders/{id}"),
    ("category", "SQL injection"),
    ("root_cause", "missing tenant check"),
    ("impact", "modify another user's order"),
    ("conditions", "requires administrator account"),
])
def test_fingerprint_retains_distinct_surface_cause_impact_or_conditions(field: str, value: str) -> None:
    first = _candidate()
    second = _candidate(**{field: value})

    assert finding_fingerprint(first) != finding_fingerprint(second)
    assert deduplicate_finding_candidates([first, second]) == [first, second]


def test_incomplete_identity_never_deduplicates_by_matching_title() -> None:
    complete = _candidate()
    missing_impact = _candidate()
    del missing_impact["impact"]
    missing_impact["description"] = complete["description"]
    empty_conditions = _candidate(conditions=" ")

    assert finding_fingerprint(missing_impact) is None
    assert finding_fingerprint(empty_conditions) is None
    assert find_duplicate_finding(missing_impact, [complete]) is None
    assert deduplicate_finding_candidates([complete, missing_impact, empty_conditions]) == [
        complete, missing_impact, empty_conditions,
    ]


def test_project_work_gate_checks_steps_and_strike_verification() -> None:
    project = make_project(steps=[make_step().model_copy(update={"to": "f002"})])
    assert check_project_work_completion(project).status == "complete"

    project.steps.append(make_step("open"))
    assert check_project_work_completion(project).status == "incomplete"
    project.steps[-1] = project.steps[-1].model_copy(update={
        "status": "closed", "close_reason": "Out of scope",
    })
    assert check_project_work_completion(project).status == "complete"

    project.steps.append(make_step("strike").model_copy(update={
        "task_type": "strike", "finding_id": "fnd1",
    }))
    project.findings = [Finding(
        id="fnd1", description="candidate", created_at="2026-01-01T00:00:00Z",
        high_value=True, verification_status="pending", verification_step_id="strike",
    )]
    assert check_project_work_completion(project).status == "incomplete"

    project.steps[-1] = project.steps[-1].model_copy(update={"to": "f003"})
    project.findings[0] = project.findings[0].model_copy(update={"verification_status": "blocked"})
    assert check_project_work_completion(project).status == "blocked"
    project.findings[0] = project.findings[0].model_copy(update={"verification_status": "confirmed"})
    assert check_project_work_completion(project).status == "complete"


def test_project_work_gate_blocks_pending_finding_without_strike_record() -> None:
    project = make_project()
    project.findings = [Finding(
        id="fnd1", description="candidate", created_at="2026-01-01T00:00:00Z",
        high_value=True, verification_status="pending", verification_step_id="missing",
    )]
    result = check_project_work_completion(project)
    assert result.status == "blocked"
    assert result.reasons == ("finding:fnd1:verification_step_missing",)


def test_project_work_gate_distinguishes_high_value_unverified_from_ordinary_candidate() -> None:
    project = make_project()
    ordinary = Finding(id="ordinary", description="candidate", created_at="2026-01-01T00:00:00Z")
    project.findings = [ordinary]
    assert check_project_work_completion(project).status == "complete"

    project.findings = [ordinary.model_copy(update={"high_value": True})]
    result = check_project_work_completion(project)
    assert result.status == "blocked"
    assert result.reasons == ("finding:ordinary:verification_not_requested",)


def test_project_work_gate_blocks_inconsistent_finished_strike_with_pending_finding() -> None:
    project = make_project(steps=[make_step("strike").model_copy(update={
        "task_type": "strike", "finding_id": "fnd1", "to": "f002",
    })])
    project.findings = [Finding(
        id="fnd1", description="candidate", created_at="2026-01-01T00:00:00Z",
        high_value=True, verification_status="pending", verification_step_id="strike",
    )]
    assert check_project_work_completion(project).status == "blocked"


def test_lane_gate_accepts_only_persisted_completed_empty_artifact() -> None:
    run = PersistedLaneRun("python", "run-42", "completed")
    artifact = PersistedLaneArtifact("python", "run-42", "completed", {"findings": []})
    assert check_lane_completion(["python"], {"python": run}, {"python": artifact}).status == "complete"
    assert check_lane_completion(["python"], {"python": run}, {}).status == "blocked"
    assert check_lane_completion(["python"], {}, {}).status == "incomplete"
    assert check_lane_completion([], {}, {}).status == "blocked"


def test_lane_gate_requires_matching_run_and_explicit_valid_artifact() -> None:
    run = PersistedLaneRun("python", "run-42", "completed")
    bad_artifacts = [
        PersistedLaneArtifact("python", "run-41", "completed", {"findings": []}),
        PersistedLaneArtifact("python", "run-42", "completed", {}),
        PersistedLaneArtifact("python", "run-42", "completed", {"findings": "none"}),
        PersistedLaneArtifact("python", "run-42", "completed", {"findings": ["unparsed"]}),
    ]
    for artifact in bad_artifacts:
        assert check_lane_completion(["python"], {"python": run}, {"python": artifact}).status == "blocked"
    writing = PersistedLaneArtifact("python", "run-42", "writing", {"findings": []})
    assert check_lane_completion(["python"], {"python": run}, {"python": writing}).status == "incomplete"
    assert check_lane_completion(["python"], {"python": PersistedLaneRun("python", "run-42", "running")}, {}).status == "incomplete"
    assert check_lane_completion(["python"], {"python": PersistedLaneRun("python", "run-42", "failed")}, {}).status == "blocked"
    assert check_lane_completion(["python"], {"python": run}, {
        "python": PersistedLaneArtifact("python", "run-42", "completed", None),  # type: ignore[arg-type]
    }).status == "blocked"


def test_lane_gate_requires_every_expected_lane_to_complete() -> None:
    run = PersistedLaneRun("python", "run-42", "completed")
    artifact = PersistedLaneArtifact("python", "run-42", "completed", {"findings": []})
    result = check_lane_completion(["python", "java"], {"python": run}, {"python": artifact})
    assert result.status == "incomplete"
    assert result.reasons == ("lane:java:run_missing",)


def test_vuln_decide_completion_uses_fresh_graph_and_defers_open_work(monkeypatch) -> None:
    config = make_config()
    config.runtime.prompt_group = "vuln"
    dispatched_snapshot = make_project()
    fresh_project = make_project(steps=[make_step("fresh-open")])
    client = FakeClient(fresh_project)
    lease = FakeLease()
    monkeypatch.setattr(decide, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(decide, "run_worker_process", lambda *_args, **_kwargs: ProcessResult(
        0, '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}', "",
    ))
    monkeypatch.setattr(decide, "gate_complete_claim", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("challenge must not run before completion gate")
    ))

    result = decide.run_decide_task(
        config, client, FakeContainerManager(), dispatched_snapshot, "graph",
        config.workers[0], TaskCancellation(),
    )

    assert result == "success"
    assert client.completed == []
    assert lease.stopped


def test_vuln_decide_completion_proceeds_when_fresh_graph_is_complete(monkeypatch) -> None:
    config = make_config()
    config.runtime.prompt_group = "vuln"
    project = make_project(steps=[make_step("done").model_copy(update={"to": "f002"})])
    client = FakeClient(project)
    lease = FakeLease()
    monkeypatch.setattr(decide, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(decide, "run_worker_process", lambda *_args, **_kwargs: ProcessResult(
        0, '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}', "",
    ))
    monkeypatch.setattr(decide, "gate_complete_claim", lambda *_args, **_kwargs: ("uphold", None))

    result = decide.run_decide_task(
        config, client, FakeContainerManager(), make_project(), "graph",
        config.workers[0], TaskCancellation(),
    )

    assert result == "success"
    assert client.completed == [("proj_001", ["f001"], "done", "test-worker")]


def test_non_vuln_decide_completion_preserves_existing_flow(monkeypatch) -> None:
    config = make_config()
    fresh_project = make_project(steps=[make_step("open")])
    client = FakeClient(fresh_project)
    monkeypatch.setattr(decide, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", lambda *_args, **_kwargs: FakeLease())
    monkeypatch.setattr(decide, "run_worker_process", lambda *_args, **_kwargs: ProcessResult(
        0, '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}', "",
    ))
    monkeypatch.setattr(decide, "gate_complete_claim", lambda *_args, **_kwargs: ("uphold", ""))

    result = decide.run_decide_task(
        config, client, FakeContainerManager(), make_project(), "graph",
        config.workers[0], TaskCancellation(),
    )

    assert result == "success"
    assert client.completed == [("proj_001", ["f001"], "done", "test-worker")]
