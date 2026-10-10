from __future__ import annotations

from fastapi.testclient import TestClient

from astra.dispatcher.contracts import validate_execute_payload
from astra.server import db
from astra.server.app import app


def _identity(impact: str) -> dict[str, str]:
    return {
        "asset_origin": "https://example.test",
        "entry_point": "/account",
        "category": "authorization",
        "root_cause": "missing owner check",
        "impact": impact,
        "conditions": "authenticated user",
    }


def test_execute_contract_preserves_complete_identity_and_keeps_partial_candidate() -> None:
    data = {"description": "observation", "finding": {
        "description": "candidate", "high_value": True,
        "identity": _identity("read another user's record"),
    }}
    kind, result = validate_execute_payload(data)
    assert kind == "fact"
    assert result is not None
    assert result["finding_identity"] == _identity("read another user's record")

    data["finding"]["identity"] = {"asset_origin": "https://example.test"}
    kind, result = validate_execute_payload(data)
    assert kind == "fact"
    assert result is not None
    assert result["finding"] == "candidate"
    assert result["finding_identity"] is None


def _candidate(client: TestClient, project_id: str, description: str, identity: dict[str, str] | None):
    created = client.post(f"/projects/{project_id}/steps", json={
        "from": ["origin"], "description": "local review", "creator": "planner",
    })
    assert created.status_code == 201
    step_id = created.json()["id"]
    claim = client.post(f"/projects/{project_id}/steps/{step_id}/heartbeat", json={"worker": "executor"})
    assert claim.status_code == 200
    payload = {"worker": "executor", "description": "observed candidate", "finding": description,
               "finding_high_value": True}
    if identity is not None:
        payload["finding_identity"] = identity
    concluded = client.post(f"/projects/{project_id}/steps/{step_id}/conclude", json=payload)
    assert concluded.status_code == 200, concluded.text
    return concluded.json()["finding"]


def test_structured_identity_dedupes_only_same_root_cause_and_impact(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as client:
        created = client.post("/projects", json={
            "title": "local identity", "origin": "start", "goal": "finish", "bootstrap_enabled": False,
        })
        assert created.status_code == 201
        project_id = created.json()["project"]["id"]
        first = _candidate(client, project_id, "candidate A", _identity("read another user's record"))
        same = _candidate(client, project_id, "candidate A, different title", _identity("read another user's record"))
        different = _candidate(client, project_id, "candidate A", _identity("modify another user's record"))
        incomplete = _candidate(client, project_id, "candidate A", None)

        assert same["id"] == first["id"]
        assert different["id"] != first["id"]
        assert incomplete["id"] not in {first["id"], different["id"]}
        assert first["identity"]["impact"] == "read another user's record"
        detail = client.get(f"/projects/{project_id}").json()
        assert len(detail["findings"]) == 3
        assert len([s for s in detail["steps"] if s["task_type"] == "strike"]) == 3
