from __future__ import annotations

import json
import sys

import pytest

from astra.dispatcher.runtime.bounded_output import BoundedOutput, MAX_STDOUT_CHARS, TRUNCATION_MARKER
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.local_process import LocalProcess
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks import execute
from astra.dispatcher.tasks.common import HealthcheckRun, worker_completion_failure
from astra.dispatcher.workers.adapters.pi import PiDriver

from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_project, make_step


def _pi_stream(text: str, *, tool: str = "success", settled: bool = True, ended: bool = True) -> str:
    message = {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": "stop"}
    events: list[dict] = [{"type": "session", "id": "session-001"}]
    if tool != "absent":
        events.append({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "read"})
        if tool != "pending":
            events.append({
                "type": "tool_execution_end", "toolCallId": "call-1", "toolName": "read",
                "isError": tool == "error", "result": {"content": []},
            })
    events.append({"type": "message_end", "message": message})
    if ended:
        events.append({"type": "agent_end", "messages": [message]})
    if settled:
        events.append({"type": "agent_settled", "aborted": False})
    return "\n".join(json.dumps(event) for event in events) + "\n"


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        (_pi_stream("", tool="success"), "final assistant text"),
        (_pi_stream("ok", tool="absent"), "no successful tool"),
        (_pi_stream("ok", tool="error"), "no successful tool"),
        (_pi_stream("ok", tool="pending"), "without matching completion"),
        (_pi_stream("ok", settled=False), "agent_settled"),
        (_pi_stream("ok", ended=False), "agent_end"),
        (_pi_stream("ok") + "{broken-json\n", "malformed JSON"),
    ],
)
def test_pi_completion_requires_settled_text_and_tool_evidence(stdout: str, reason: str) -> None:
    assert reason in (PiDriver().completion_failure(stdout, require_tool=True) or "")


def test_pi_completion_accepts_successful_tool_and_rejects_truncated_stream() -> None:
    stdout = _pi_stream('{"accepted":true,"data":{"description":"read evidence"}}')
    driver = PiDriver()
    evidence = driver.completion_evidence(stdout)
    assert evidence.successful_tool_calls == 1
    assert evidence.incomplete_tool_calls == 0
    assert driver.completion_failure(stdout, require_tool=True) is None
    assert driver.completion_failure(stdout, stdout_truncated=True) == "stdout was truncated"


def test_pi_completion_rejects_stale_text_before_last_tool_call() -> None:
    events = [json.loads(line) for line in _pi_stream("claim").splitlines()]
    final_message = events.pop(3)  # message_end is moved before the tool lifecycle.
    events.insert(1, final_message)
    events[4]["messages"] = []  # agent_end has no replacement assistant message.
    stdout = "\n".join(json.dumps(event) for event in events)
    assert "did not follow tool execution" in (PiDriver().completion_failure(stdout, require_tool=True) or "")


def test_pi_completion_allows_final_text_without_tools_when_tools_are_optional() -> None:
    stdout = _pi_stream("answer", tool="absent")
    assert PiDriver().completion_failure(stdout, require_tool=False) is None


def test_bounded_output_preserves_head_tail_and_caps_size() -> None:
    output = BoundedOutput(100)
    output.append("HEAD" + "x" * 50)
    output.append("y" * 80 + "TAIL")
    text, truncated = output.snapshot()
    assert truncated
    assert text.startswith("HEAD")
    assert text.endswith("TAIL")
    assert TRUNCATION_MARKER in text
    assert len(text) <= 100


def test_bounded_output_four_megabyte_boundary_and_large_stream() -> None:
    output = BoundedOutput(MAX_STDOUT_CHARS)
    chunk = "x" * 4096
    for _ in range(MAX_STDOUT_CHARS // len(chunk)):
        output.append(chunk)
    text, truncated = output.snapshot()
    assert len(text) == MAX_STDOUT_CHARS
    assert not truncated

    output.append("Z")
    text, truncated = output.snapshot()
    assert truncated and len(text) <= MAX_STDOUT_CHARS
    assert text.endswith("Z")
    assert TRUNCATION_MARKER in text

    output.append("Y" * (2 * MAX_STDOUT_CHARS))
    text, truncated = output.snapshot()
    assert truncated and len(text) <= MAX_STDOUT_CHARS
    assert text.endswith("Y" * 128)
    assert output._tail_length <= output._tail_limit
    assert not output._head_chunks


def test_local_process_marks_truncated_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from astra.dispatcher.runtime import local_process

    monkeypatch.setattr(local_process, "MAX_STDOUT_CHARS", 256)
    monkeypatch.setattr(local_process, "MAX_STDERR_CHARS", 128)
    process = LocalProcess(
        [sys.executable, "-c", "import sys; print('H' + 'x'*2000 + 'T'); print('E'*1000, file=sys.stderr)"],
        {},
    )
    process.start()
    result = process.communicate(timeout=10)
    assert result.returncode == 0
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout) <= 256
    assert len(result.stderr) <= 128
    assert result.stdout.startswith("H") and result.stdout.rstrip().endswith("T")


class _PiTaskDriver(FakeDriver):
    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return PiDriver().extract_response_text(stdout, stderr)

    def completion_failure(self, stdout: str, **kwargs) -> str | None:
        return PiDriver().completion_failure(stdout, **kwargs)

    def completion_evidence(self, stdout: str):
        return PiDriver().completion_evidence(stdout)


@pytest.mark.parametrize("tool,expected", [("absent", "failed"), ("success", "success")])
def test_offline_execute_gate_controls_project_write(monkeypatch: pytest.MonkeyPatch, tool: str, expected: str) -> None:
    config = make_config()
    worker = config.workers[0].model_copy(update={"type": "pi", "env": {"PI_OFFLINE_MODEL_POLICY": "loopback"}})
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    lease = FakeLease()
    monkeypatch.setattr(execute, "get_driver", lambda _name: _PiTaskDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", lambda *_args: lease)
    monkeypatch.setattr(
        execute, "run_healthcheck",
        lambda *_args, **_kwargs: HealthcheckRun(ProcessResult(0, "", ""), duration_ms=1),
    )
    stdout = _pi_stream('{"accepted":true,"data":{"description":"read evidence"}}', tool=tool)
    monkeypatch.setattr(execute, "run_worker_process", lambda *_args, **_kwargs: ProcessResult(0, stdout, ""))

    outcome = execute.run_execute_task(
        config, client, FakeContainerManager(), project, "graph", step, worker, TaskCancellation()
    )

    assert outcome == expected
    assert bool(client.concluded) is (expected == "success")


@pytest.mark.parametrize("prior_tool,expected", [("absent", "failed"), ("success", "success")])
def test_offline_conclude_can_reuse_prior_session_tool_evidence(
    monkeypatch: pytest.MonkeyPatch, prior_tool: str, expected: str
) -> None:
    config = make_config()
    worker = config.workers[0].model_copy(update={"type": "pi", "env": {"PI_OFFLINE_MODEL_POLICY": "loopback"}})
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    lease = FakeLease()
    driver = _PiTaskDriver()
    monkeypatch.setattr(execute, "get_driver", lambda _name: driver)
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", lambda *_args: lease)
    monkeypatch.setattr(
        execute, "run_healthcheck",
        lambda *_args, **_kwargs: HealthcheckRun(ProcessResult(0, "", ""), duration_ms=1),
    )
    outputs = iter([
        ProcessResult(0, _pi_stream("Need a structured conclusion.", tool=prior_tool), ""),
        ProcessResult(0, _pi_stream('{"accepted":true,"data":{"description":"confirmed"}}', tool="absent"), ""),
    ])
    monkeypatch.setattr(execute, "_run_process", lambda *_args, **_kwargs: next(outputs))

    outcome = execute.run_execute_task(
        config, client, FakeContainerManager(), project, "graph", step, worker, TaskCancellation()
    )

    assert outcome == expected
    assert bool(client.concluded) is (expected == "success")


def test_non_offline_worker_keeps_existing_result_contract() -> None:
    worker = make_config().workers[0]
    assert worker_completion_failure(FakeDriver(), worker, ProcessResult(0, "plain mock output", "")) is None
