"""Immutable evidence imported from the dispatcher collector, not model JSON."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import uuid

from fastapi import APIRouter, Header, HTTPException

from astra.server.db import get_conn
from astra.server.models import EvidenceImportRequest, EvidenceRecord
from astra.server.services import check_project_active, get_project_or_404, get_step_or_404, utcnow

router = APIRouter(tags=["evidence"])
MAX_BODY_BYTES = 65536
MAX_ARTIFACT_BYTES = 262144


def _record(row) -> EvidenceRecord:
    return EvidenceRecord(**dict(row))


@router.post(
    "/projects/{project_id}/steps/{step_id}/evidence",
    response_model=EvidenceRecord,
    status_code=201,
)
def import_evidence(
    project_id: str,
    step_id: str,
    body: EvidenceImportRequest,
    x_astra_collector_token: str | None = Header(default=None),
):
    # An API token alone does not attest a tool event. The dispatcher must keep
    # this separate token out of model/worker environments and import only
    # matched Pi tool_execution_start/end events.
    expected = os.environ.get("ASTRA_EVIDENCE_COLLECTOR_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Evidence collector is not configured")
    if not x_astra_collector_token or not secrets.compare_digest(x_astra_collector_token, expected):
        raise HTTPException(403, "Collector credential required")

    try:
        raw_body = base64.b64decode(body.body_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Invalid evidence body_base64") from None
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Evidence body is too large")

    # The stored artifact is the actual bounded tool result, not a digest
    # supplied by the model. Canonical encoding makes integrity checks stable.
    artifact_data = body.model_dump()
    artifact_data["body_base64"] = base64.b64encode(raw_body).decode("ascii")
    artifact = json.dumps(
        artifact_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(artifact) > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, "Evidence artifact is too large")
    digest = hashlib.sha256(artifact).hexdigest()
    body_digest = hashlib.sha256(raw_body).hexdigest()

    with get_conn() as conn:
        check_project_active(conn, project_id)
        step = get_step_or_404(conn, project_id, step_id)
        if step["status"] != "open" or step["to_fact_id"] is not None:
            raise HTTPException(409, "Step is no longer collecting evidence")
        if not step["worker"]:
            raise HTTPException(409, "Evidence requires a claimed step")

        existing = conn.execute(
            """SELECT * FROM evidence WHERE project_id = ? AND step_id = ?
               AND session_id = ? AND tool_call_id = ?""",
            (project_id, step_id, body.session_id, body.tool_call_id),
        ).fetchone()
        if existing is not None:
            if existing["sha256"] != digest or existing["worker"] != step["worker"]:
                raise HTTPException(409, "Tool call evidence is immutable")
            return _record(existing)

        evidence_id = f"ev_{uuid.uuid4().hex}"
        uri = f"astra://projects/{project_id}/evidence/{evidence_id}"
        conn.execute(
            """INSERT INTO evidence
               (id, project_id, step_id, worker, session_id, tool_call_id,
                scope_sha256, uri, sha256, body_sha256, url, method, status,
                artifact, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id, project_id, step_id, step["worker"], body.session_id,
                body.tool_call_id, body.scope_sha256, uri, digest, body_digest,
                body.url, body.method, body.status, artifact, utcnow(),
            ),
        )
        row = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        return _record(row)


@router.get("/projects/{project_id}/evidence/{evidence_id}")
def get_evidence(project_id: str, evidence_id: str):
    """Return the persisted artifact so an auditor can recompute its SHA256."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM evidence WHERE id = ? AND project_id = ?",
            (evidence_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Evidence not found")
        return {
            **_record(row).model_dump(),
            "artifact": json.loads(row["artifact"]),
        }
