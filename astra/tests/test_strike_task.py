from __future__ import annotations

import json

import pytest

from astra.dispatcher.config import MOCK_ALLOWED_OUTCOMES, DispatchConfig, WorkerConfig
from astra.dispatcher.context import build_focus_open_steps
from astra.dispatcher.protocol.client import ApiResult
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks import strike
from astra.dispatcher.workers.adapters.mock import MockDriver
from astra.dispatcher.workers.adapters.pi import PiDriver
from astra.server.models import Finding

from conftest import FakeClient, FakeContainerManager, FakeLease, make_config, make_project, make_step


def _pending_strike():
    step = make_step().model_copy(update={"task_type": "strike", "finding_id": "fnd001"})
    project = make_project(steps=[step])
    project.findings.append(Finding(
        id="fnd001", description="Reported high-value claim; independently check its evidence.",
        created_at="2026-01-01T00:00:03Z", high_value=True, verification_status="pending",
        source_fact_id="f001", source_step_id="s-source", verification_step_id=step.id,
    ))
    return project, step


def _run_strike(monkeypatch, result: ProcessResult, *, client_override=None):
    project, step = _pending_strike()
    client = client_override or FakeClient(project)
    lease = FakeLease()
    runs: list[dict] = []
    monkeypatch.setattr(strike.HeartbeatLease, "for_step", lambda *_args, **_kwargs: lease)

    def run_process(*_args, **kwargs):
        runs.append(kwargs)
        return result

    monkeypatch.setattr(strike, "run_worker_process_with_retry", run_process)
    config = make_config()
    status = strike.run_strike_task(
        config, client, FakeContainerManager(), project, "graph", step,
        config.workers[0], TaskCancellation(),
    )
    return status, client, lease, runs


@pytest.mark.parametrize("verdict", ["confirmed", "refuted", "blocked"])
def test_strike_writes_explicit_verdict_without_creating_finding(monkeypatch, verdict) -> None:
    summary = "Independent request reproduced observed response; evidence at verification.txt."
    result = ProcessResult(0, json.dumps({"accepted": True, "data": {"verdict": verdict, "summary": summary}}), "")
    status, client, lease, runs = _run_strike(monkeypatch, result)

    assert status == "success"
    assert len(client.concluded) == 1
    assert verdict in client.concluded[0][3]
    assert client.conclude_options[0]["verification_status"] == verdict
    assert client.conclude_options[0]["verification_summary"] == summary
    assert client.created_findings == []
    assert client.released == []
    assert lease.started and lease.stopped
    assert runs[0]["phase"] == "strike"


@pytest.mark.parametrize("stdout", [
    "{invalid-json",
    '{"accepted":true,"data":{"verdict":"maybe","summary":"uncertain"}}',
    '{"accepted":true,"data":{"verdict":"confirmed","summary":""}}',
    '{"accepted":true,"data":{"verdict":"confirmed","summary":"test","finding":"new"}}',
    '{"accepted":false,"reason":"refused"}',
])
def test_invalid_strike_output_releases_step_and_keeps_finding_pending(monkeypatch, stdout) -> None:
    status, client, lease, _runs = _run_strike(monkeypatch, ProcessResult(0, stdout, ""))

    assert status == "failed"
    assert client.concluded == []
    assert client.released == [("proj_001", "s001", "test-worker")]
    assert client.project.findings[0].verification_status == "pending"
    assert lease.stopped


@pytest.mark.parametrize("result", [
    ProcessResult(1, "", "command failed"),
    ProcessResult(124, "", "", timed_out=True),
    ProcessResult(1, "", "", cancelled=True, cancel_reason="stopped"),
])
def test_strike_process_failure_or_cancellation_leaves_finding_pending(monkeypatch, result) -> None:
    status, client, _lease, _runs = _run_strike(monkeypatch, result)
    assert status == ("cancelled" if result.cancelled else "failed")
    assert client.concluded == []
    assert client.released == [("proj_001", "s001", "test-worker")]
    assert client.project.findings[0].verification_status == "pending"


def test_strike_conclude_failure_releases_step_for_retry(monkeypatch) -> None:
    project, _step = _pending_strike()

    class FailedWrite(FakeClient):
        def conclude(self, *_args, **_kwargs):
            return ApiResult(503, text="temporary failure")

    result = ProcessResult(0, '{"accepted":true,"data":{"verdict":"confirmed","summary":"independent evidence"}}', "")
    status, client, _lease, _runs = _run_strike(monkeypatch, result, client_override=FailedWrite(project))
    assert status == "failed"
    assert client.released == [("proj_001", "s001", "test-worker")]
    assert project.findings[0].verification_status == "pending"


def test_decide_open_step_context_excludes_strike_without_spending_budget() -> None:
    normal = make_step("normal")
    verify = make_step("verify").model_copy(update={"task_type": "strike", "finding_id": "fnd001", "created_at": "2026-01-01T00:00:04Z"})
    assert [item["id"] for item in build_focus_open_steps(make_project(steps=[normal, verify]), 1)] == ["normal"]


@pytest.mark.parametrize("outcome", ["confirmed", "refuted", "blocked", "invalid_json", "invalid_payload", "command_fail"])
def test_mock_strike_outcomes_run_through_real_driver(tmp_path, outcome) -> None:
    from astra.dispatcher.runtime.local_containers import LocalContainerManager

    weights = {name: "1.0" if name == outcome else "0.0" for name in MOCK_ALLOWED_OUTCOMES["strike"]}
    worker = WorkerConfig.model_validate({
        "name": "mock-strike", "type": "mock", "task_types": ["strike"],
        "max_running": 1, "priority": 0,
        "env": {"MOCK_STRIKE": json.dumps({"delay": [0, 0], "outcomes": weights})},
    })
    driver = MockDriver()
    command = driver.build_strike(worker, '{"phase":"strike","step_id":"s001","finding_id":"fnd001"}', None)
    manager = LocalContainerManager(make_config().container, workspace_root=tmp_path)
    container_name = manager.ensure_running("proj_001")
    process = manager.build_exec_process(container_name, worker.env, command.argv)
    process.start()
    result = process.communicate(timeout=5)

    if outcome == "command_fail":
        assert result.returncode != 0
    elif outcome == "invalid_json":
        with pytest.raises(ValueError):
            strike.parse_json_output(result.stdout)
    elif outcome == "invalid_payload":
        with pytest.raises(ValueError):
            strike.validate_strike_payload(strike.parse_json_output(result.stdout))
    else:
        verdict, summary = strike.validate_strike_payload(strike.parse_json_output(result.stdout))
        assert verdict == outcome
        assert "fnd001" in summary


def test_pi_strike_has_execution_tools_for_independent_reproduction(monkeypatch) -> None:
    worker = WorkerConfig.model_validate({
        "name": "pi-strike", "type": "pi", "task_types": ["strike"], "max_running": 1, "priority": 0,
        "env": {"PI_MODEL": "model", "PI_BASE_URL": "https://example.test", "PI_API_KEY": "key", "PI_PROVIDER_API": "anthropic-messages"},
    })
    driver = PiDriver()
    built: list[dict] = []

    def build(_worker, _prompt, session, **kwargs):
        built.append(kwargs)
        from astra.dispatcher.workers.base import DriverResult
        return DriverResult(["pi"], session)

    monkeypatch.setattr(driver, "_build_run", build)
    driver.build_strike(worker, "verify", None)
    assert built == [{}]
    assert "bash" in driver._tool_list(worker).split(",")


def test_strike_config_uses_default_timeout_and_accepts_task_type() -> None:
    config = make_config()
    payload = config.model_dump()
    payload["workers"][0]["task_types"] = ["strike"]
    assert DispatchConfig.model_validate(payload).tasks.strike.timeout == 120
    payload["tasks"]["strike"]["timeout"] = 0
    with pytest.raises(ValueError):
        DispatchConfig.model_validate(payload)
