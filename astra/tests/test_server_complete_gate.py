from __future__ import annotations

from fastapi.testclient import TestClient

from astra.server import db
from astra.server.app import app


def test_vuln_server_complete_checks_current_graph_in_write_transaction(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setenv("ASTRA_VULN_STRICT", "1")
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as client:
        created = client.post("/projects", json={
            "title": "local gate", "origin": "start", "goal": "finish", "bootstrap_enabled": False,
        })
        assert created.status_code == 201
        project_id = created.json()["project"]["id"]
        fact = client.post(f"/projects/{project_id}/facts", json={"description": "observed result"})
        assert fact.status_code == 201
        fact_id = fact.json()["id"]
        step = client.post(f"/projects/{project_id}/steps", json={
            "from": [fact_id], "description": "pending verification", "creator": "decider",
        })
        assert step.status_code == 201
        step_id = step.json()["id"]
        completion = {"from": [fact_id], "description": "done", "worker": "decider"}

        rejected = client.post(f"/projects/{project_id}/complete", json=completion)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["work_status"] == "incomplete"
        assert client.get(f"/projects/{project_id}").json()["project"]["status"] == "active"

        closed = client.post(f"/projects/{project_id}/steps/{step_id}/close", json={"reason": "explicitly not needed"})
        assert closed.status_code == 200
        accepted = client.post(f"/projects/{project_id}/complete", json=completion)
        assert accepted.status_code == 200
        assert client.get(f"/projects/{project_id}").json()["project"]["status"] == "completed"


def test_strict_vuln_rejects_high_value_candidate_without_collected_source(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setenv("ASTRA_VULN_STRICT", "1")
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as client:
        created = client.post("/projects", json={
            "title": "local source check", "origin": "start", "goal": "finish", "bootstrap_enabled": False,
        })
        project_id = created.json()["project"]["id"]
        step = client.post(f"/projects/{project_id}/steps", json={
            "from": ["origin"], "description": "review candidate", "creator": "planner",
        })
        step_id = step.json()["id"]
        assert client.post(f"/projects/{project_id}/steps/{step_id}/heartbeat", json={"worker": "executor"}).status_code == 200
        rejected = client.post(f"/projects/{project_id}/steps/{step_id}/conclude", json={
            "worker": "executor", "description": "observed", "finding": "candidate",
            "finding_high_value": True,
        })
        assert rejected.status_code == 422
        detail = client.get(f"/projects/{project_id}").json()
        assert detail["findings"] == []
