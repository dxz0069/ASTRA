from __future__ import annotations

import base64
import hashlib
import json
import sys
from types import SimpleNamespace

from astra.dispatcher.evidence_collector import collect_scoped_request_evidence, resolve_evidence_refs
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.runtime.local_process import LocalProcess


def _events(*, is_error: bool = False, without_start: bool = False) -> str:
    call_id = "call-1"
    args = {"url": "https://example.test/a", "method": "GET"}
    evidence = {
        "scope_sha256": hashlib.sha256(b"scope-test").hexdigest(),
        "url": args["url"],
        "method": "GET",
        "status": 200,
        "headers": {"content-type": "text/plain"},
        "body_base64": base64.b64encode(b"proof").decode("ascii"),
        "started_at": "2026-10-10T00:00:00.000Z",
        "finished_at": "2026-10-10T00:00:00.010Z",
        "pinned_address": "203.0.113.9",
    }
    events = []
    if not without_start:
        events.append({"type": "tool_execution_start", "toolCallId": call_id, "toolName": "scoped_request", "args": args})
    events.append({
        "type": "tool_execution_end", "toolCallId": call_id, "toolName": "scoped_request",
        "result": {"details": {"_astra_evidence": {"requested_url": args["url"], **evidence}}},
        "isError": is_error,
    })
    # A model can print plausible evidence in text, but it must never be parsed as a tool event.
    events.append({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(evidence)}]}})
    return "\n".join(json.dumps(item) for item in events)


class _Client:
    def __init__(self) -> None:
        self.imports: list[tuple[str, str, dict, str]] = []

    def create_evidence(self, project_id: str, step_id: str, payload: dict, *, collector_token: str):
        self.imports.append((project_id, step_id, payload, collector_token))
        return SimpleNamespace(ok=True, status_code=201, text="", data={"id": "ev_1"})


def _worker():
    return SimpleNamespace(type="pi", name="offline-vuln", env={
        "PI_TOOL_PROFILE": "vuln", "ASTRA_VULN_SCOPE_JSON": "scope-test",
    })


def test_collects_only_completed_non_error_pi_events(monkeypatch) -> None:
    monkeypatch.setenv("ASTRA_EVIDENCE_COLLECTOR_TOKEN", "dispatcher-secret")
    client = _Client()
    output = json.dumps({"type": "session", "id": "session-1"}) + "\n" + _events()
    result = ProcessResult(0, output, "")

    imported = collect_scoped_request_evidence(client, _worker(), "p1", "s1", result)

    assert imported == {"call-1": "ev_1"}
    assert len(client.imports) == 1
    project_id, step_id, payload, token = client.imports[0]
    assert (project_id, step_id, token) == ("p1", "s1", "dispatcher-secret")
    assert payload["session_id"] == "session-1"
    assert payload["body_base64"] == base64.b64encode(b"proof").decode("ascii")
    assert "requested_url" not in payload
    assert resolve_evidence_refs(["call-1", "made-up", "call-1"], imported) == ["ev_1"]


def test_does_not_import_unpaired_failed_or_model_authored_evidence(monkeypatch) -> None:
    monkeypatch.setenv("ASTRA_EVIDENCE_COLLECTOR_TOKEN", "dispatcher-secret")
    client = _Client()
    for output in (_events(is_error=True), _events(without_start=True)):
        result = ProcessResult(0, output, "")
        assert collect_scoped_request_evidence(client, _worker(), "p1", "s1", result) == {}
    assert client.imports == []


def test_missing_collector_token_never_imports(monkeypatch) -> None:
    monkeypatch.delenv("ASTRA_EVIDENCE_COLLECTOR_TOKEN", raising=False)
    client = _Client()
    result = ProcessResult(0, _events(), "")
    assert collect_scoped_request_evidence(client, _worker(), "p1", "s1", result) == {}
    assert client.imports == []


def test_local_pi_child_does_not_inherit_collector_token(monkeypatch) -> None:
    monkeypatch.setenv("ASTRA_EVIDENCE_COLLECTOR_TOKEN", "dispatcher-secret")
    process = LocalProcess(
        [sys.executable, "-c", "import os; print(os.getenv('ASTRA_EVIDENCE_COLLECTOR_TOKEN', 'absent'))"],
        {},
    )
    process.start()
    result = process.communicate(timeout=10)
    assert result.returncode == 0
    assert result.stdout.strip() == "absent"
