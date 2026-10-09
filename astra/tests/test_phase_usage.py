"""Pi phase accounting from streamed JSON events."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks.common import _log_phase_usage, _parse_pi_usage, run_worker_process


def _event(event_type: str, message: dict) -> str:
    return json.dumps({"type": event_type, "message": message}, ensure_ascii=False)


def test_pi_usage_sums_each_assistant_response_once() -> None:
    first = {
        "role": "assistant",
        "content": [{"type": "text", "text": "secret prompt material"}],
        "usage": {"input": 100, "output": 20, "cacheRead": 60, "totalTokens": 180},
    }
    second = {
        "role": "assistant",
        "content": [{"type": "text", "text": "second response"}],
        "usage": {"inputTokens": 30, "outputTokens": 10, "cacheReadInputTokens": 5},
    }
    stdout = "\n".join(
        [
            _event("message_end", first),
            _event("turn_end", first),  # Pi repeats this message at turn end.
            _event("message_end", {"role": "toolResult", "usage": first["usage"]}),
            _event("turn_end", second),  # Older streams may only have turn_end.
            json.dumps({"type": "agent_end", "messages": [first, second]}),
        ]
    )
    assert _parse_pi_usage(stdout) == {
        "assistant_turns": 2,
        "usage_turns": 2,
        "input_tokens": 130,
        "output_tokens": 30,
        "cache_read_tokens": 65,
        "cache_read_turns": 2,
        "usage_complete": True,
        "cache_read_complete": True,
    }


def test_pi_usage_marks_missing_or_unusable_usage() -> None:
    stdout = "\n".join(
        [
            _event("turn_end", {"role": "assistant", "content": []}),
            _event("turn_end", {"role": "assistant", "usage": {"totalTokens": 99}}),
            "{broken json",
        ]
    )
    assert _parse_pi_usage(stdout) == {
        "assistant_turns": 2,
        "usage_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_read_turns": 0,
        "usage_complete": False,
        "cache_read_complete": False,
    }
    assert _parse_pi_usage('{"type":"session","id":"s1"}')["usage_complete"] is False


def test_pi_usage_turn_end_can_supply_missing_message_end_usage() -> None:
    partial = {"role": "assistant", "content": [{"type": "text", "text": "done"}]}
    final = {**partial, "usage": {"input": 12, "output": 4, "cacheRead": 0}}
    usage = _parse_pi_usage("\n".join([_event("message_end", partial), _event("turn_end", final)]))
    assert usage["assistant_turns"] == 1
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 4
    assert usage["usage_complete"] is True
    assert usage["cache_read_complete"] is True
    incomplete = {**partial, "usage": {"input": 12}}
    usage = _parse_pi_usage("\n".join([_event("message_end", incomplete), _event("turn_end", final)]))
    assert usage["assistant_turns"] == 1
    assert usage["output_tokens"] == 4
    assert usage["cache_read_complete"] is True


def test_pi_process_emits_metadata_only_record(tmp_path, monkeypatch, caplog) -> None:
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("ASTRA_PHASE_USAGE_JSONL", str(path))
    secret = "API_KEY=never-log-this"
    stdout = _event(
        "turn_end",
        {
            "role": "assistant",
            "content": [{"type": "text", "text": secret}],
            "usage": {"input": 8, "output": 3, "cacheRead": 2},
        },
    )

    class FakeProcess:
        def start(self) -> None:
            pass

        def communicate(self, timeout: int) -> ProcessResult:
            assert timeout > 0
            return ProcessResult(0, stdout, secret)

    manager = SimpleNamespace(build_exec_process=lambda *_a, **_k: FakeProcess())
    worker = SimpleNamespace(name="w1", type="pi", env={})
    with caplog.at_level("INFO"):
        result = run_worker_process(
            manager, "container", worker, [secret], phase="execute_execute",
            timeout_seconds=10, project_id="p1", step_id="s1",
        )
    assert result.returncode == 0
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["project_id"] == "p1"
    assert row["step_id"] == "s1"
    assert row["worker"] == "w1"
    assert row["phase"] == "execute_execute"
    assert row["outcome"] == "completed"
    assert row["returncode"] == 0
    assert row["duration_ms"] >= 0
    assert row["input_tokens"] == 8
    assert row["output_tokens"] == 3
    assert row["cache_read_tokens"] == 2
    assert row["cache_read_complete"] is True
    assert secret not in path.read_text(encoding="utf-8")
    assert secret not in caplog.text


def test_pi_process_without_usage_still_records_failure(tmp_path, monkeypatch) -> None:
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("ASTRA_PHASE_USAGE_JSONL", str(path))

    class FakeProcess:
        def start(self) -> None:
            pass

        def communicate(self, timeout: int) -> ProcessResult:
            return ProcessResult(1, '{"type":"session","id":"s1"}', "gateway failed")

    manager = SimpleNamespace(build_exec_process=lambda *_a, **_k: FakeProcess())
    worker = SimpleNamespace(name="w1", type="pi", env={})
    result = run_worker_process(
        manager, "container", worker, ["pi"], phase="bootstrap",
        timeout_seconds=10, project_id="p1",
    )
    assert result.returncode == 1
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["outcome"] == "failed"
    assert row["assistant_turns"] == row["usage_turns"] == 0
    assert row["usage_complete"] is False
    assert row["cache_read_complete"] is False
    assert row["input_tokens"] == row["output_tokens"] == row["cache_read_tokens"] == 0


def test_pi_usage_jsonl_concurrent_appends(tmp_path, monkeypatch) -> None:
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("ASTRA_PHASE_USAGE_JSONL", str(path))
    stdout = _event("turn_end", {"role": "assistant", "usage": {"input": 1, "output": 2}})

    def write(index: int) -> None:
        _log_phase_usage(
            "w1", "decide_execute", stdout,
            project_id=f"p{index}", duration_ms=index,
            result=ProcessResult(0, stdout, ""),
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(60)))

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 60
    assert {row["project_id"] for row in rows} == {f"p{i}" for i in range(60)}
    assert len({row["run_id"] for row in rows}) == 60
    assert all(row["usage_complete"] and row["input_tokens"] == 1 for row in rows)
    assert all(row["cache_read_complete"] is False for row in rows)
