from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
import yaml

from astra.server import db
from astra.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as test_client:
        yield test_client


def test_finding_verification_state_and_links_are_exported(client: TestClient) -> None:
    project = client.post(
        "/projects",
        json={"title": "export findings", "origin": "start", "goal": "finish"},
    ).json()
    project_id = project["project"]["id"]
    source_fact = client.post(
        f"/projects/{project_id}/facts", json={"description": "source evidence"},
    ).json()["id"]
    verdict_fact = client.post(
        f"/projects/{project_id}/facts", json={"description": "verification evidence"},
    ).json()["id"]
    source_step = client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "discover", "creator": "agent", "worker": None},
    ).json()["id"]
    verification_step = client.post(
        f"/projects/{project_id}/steps",
        json={"from": [source_fact], "description": "verify", "creator": "agent", "worker": None},
    ).json()["id"]

    finding_ids = {}
    for status in ("not_requested", "pending", "confirmed", "refuted", "blocked"):
        response = client.post(
            f"/projects/{project_id}/findings", json={"description": f"finding {status}"},
        )
        assert response.status_code == 201
        finding_ids[status] = response.json()["id"]

    with db.get_conn() as conn:
        for status in ("pending", "confirmed", "refuted", "blocked"):
            conn.execute(
                """UPDATE findings SET high_value = 1, verification_status = ?,
                   source_fact_id = ?, source_step_id = ?, verification_step_id = ?,
                   verification_fact_id = ?, verification_summary = ?
                   WHERE project_id = ? AND id = ?""",
                (status, source_fact, source_step, verification_step,
                 verdict_fact if status != "pending" else None,
                 f"review {status}" if status != "pending" else None,
                 project_id, finding_ids[status]),
            )
        conn.execute(
            "UPDATE steps SET task_type = 'strike', finding_id = ? WHERE project_id = ? AND id = ?",
            (finding_ids["pending"], project_id, verification_step),
        )

    yaml_response = client.get(f"/projects/{project_id}/export?format=yaml")
    assert yaml_response.status_code == 200
    exported = yaml.safe_load(yaml_response.text)
    findings = {entry["id"]: entry for entry in exported["findings"]}
    legacy = findings[finding_ids["not_requested"]]
    assert legacy["high_value"] is False
    assert legacy["verification_status"] == "not_requested"
    assert "verification_summary" not in legacy
    pending = findings[finding_ids["pending"]]
    assert pending["high_value"] is True
    assert pending["verification_status"] == "pending"
    assert pending["source_fact_id"] == source_fact
    assert pending["source_step_id"] == source_step
    assert pending["verification_step_id"] == verification_step
    assert "verification_fact_id" not in pending
    for status in ("confirmed", "refuted", "blocked"):
        entry = findings[finding_ids[status]]
        assert entry["verification_status"] == status
        assert entry["verification_fact_id"] == verdict_fact
        assert entry["verification_summary"] == f"review {status}"
    strike_step = next(entry for entry in exported["steps"] if entry["id"] == verification_step)
    assert strike_step["task_type"] == "strike"
    assert strike_step["finding_id"] == finding_ids["pending"]

    timeline_response = client.get(f"/projects/{project_id}/export?format=timeline")
    assert timeline_response.status_code == 200
    timeline = timeline_response.text
    for status, finding_id in finding_ids.items():
        assert f"FINDING {finding_id}" in timeline
        assert f"verification_status_at_export: {status}" in timeline
    assert f"finding_id: {finding_ids['pending']}" in timeline
    assert f"verification_fact_id: {verdict_fact}" in timeline
