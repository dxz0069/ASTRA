"""Import completed scoped_request outputs into ASTRA's evidence store.

Only Pi's structured ``tool_execution_end`` events are accepted. Assistant
text, claimed URLs, and model-authored evidence JSON are deliberately ignored.
The collector token is read in the dispatcher process and is never copied into
the worker environment.
"""

from __future__ import annotations

import os
import hashlib
import hmac
from typing import Any

from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.workers.adapters.pi import PiDriver
from astra.dispatcher.config import WorkerConfig


def collect_scoped_request_evidence(
    client: Any,
    worker: WorkerConfig,
    project_id: str,
    step_id: str,
    result: ProcessResult,
    *,
    session_id: str | None = None,
    collector_token: str | None = None,
) -> dict[str, str]:
    """Persist tool outputs and return ``tool_call_id -> evidence id``.

    Missing collector configuration yields no trusted references. A failed
    import raises so callers cannot accidentally conclude a Finding as
    confirmed while silently dropping evidence.
    """
    if worker.type != "pi" or worker.env.get("PI_TOOL_PROFILE") != "vuln":
        return {}
    if "ASTRA_EVIDENCE_COLLECTOR_TOKEN" in worker.env:
        raise RuntimeError("Collector token must not enter the Pi worker environment")
    token = collector_token if collector_token is not None else os.environ.get("ASTRA_EVIDENCE_COLLECTOR_TOKEN", "")
    if not token:
        return {}
    if result.returncode != 0 or result.timed_out or result.cancelled:
        return {}

    resolved_session = session_id or PiDriver().extract_session(None, result.stdout, result.stderr)
    events = PiDriver.extract_scoped_request_evidence(result.stdout)
    if events and not resolved_session:
        raise RuntimeError("Pi output contains scoped_request evidence but no session id")
    configured_scope = worker.env.get("ASTRA_VULN_SCOPE_JSON", "")
    expected_scope_hash = hashlib.sha256(configured_scope.encode("utf-8")).hexdigest()

    imported: dict[str, str] = {}
    for evidence in events[:20]:
        if not configured_scope or not hmac.compare_digest(evidence["scope_sha256"], expected_scope_hash):
            raise RuntimeError("Scoped request evidence does not match this worker's configured scope")
        call_id = evidence["tool_call_id"]
        payload = {"session_id": resolved_session, **evidence}
        response = client.create_evidence(project_id, step_id, payload, collector_token=token)
        if not response.ok:
            raise RuntimeError(
                f"ASTRA evidence import failed for tool call {call_id}: "
                f"HTTP {response.status_code} {response.text[:300]}"
            )
        data = response.data
        evidence_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(evidence_id, str) or not evidence_id:
            raise RuntimeError(f"ASTRA evidence import returned no evidence id for tool call {call_id}")
        imported[call_id] = evidence_id
    return imported


def resolve_evidence_refs(
    requested_tool_call_ids: list[str] | None,
    imported_evidence: dict[str, str],
) -> list[str]:
    """Resolve model references only to records already persisted by collector."""
    resolved: list[str] = []
    for call_id in requested_tool_call_ids or []:
        evidence_id = imported_evidence.get(call_id)
        if evidence_id is not None and evidence_id not in resolved:
            resolved.append(evidence_id)
    return resolved
