from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from astra.server import db
from astra.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_project_workflow_create_conclude_complete_and_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    assert response.json()["fact"] == {
        "id": "f001",
        "description": "new fact",
        "kind": "regular",
        "confidence": "medium",
        "evidence": None,
        "challenged": False,
    }

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"] == {
        "id": "f002",
        "description": "human correction",
        "kind": "regular",
        "confidence": "medium",
        "evidence": None,
        "challenged": False,
    }
    assert payload["intent"]["from"] == ["f001"]
    assert payload["intent"]["to"] == "f002"


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403


def test_intent_creation_rejects_goal_source_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["goal"], "description": "invalid", "creator": "reasoner", "worker": None},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert response.status_code == 200
    assert client.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "goal: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def test_archive_facts_protects_origin_goal_and_intent_targets(client: TestClient) -> None:
    """origin/goal 与被 intent.to 引用的星记不可归档（防 intent.to 悬挂击垮前端渲染/导出）。"""
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    client.post(f"/projects/{project_id}/intents/i001/heartbeat", json={"worker": "explorer"})
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )

    response = client.post(
        f"/projects/{project_id}/facts/archive",
        json={"fact_ids": ["origin", "goal", "f001"]},
    )
    assert response.status_code == 200
    assert response.json()["deleted"] == 0
    assert sorted(response.json()["skipped"]) == ["f001", "goal", "origin"]
    facts = client.get(f"/projects/{project_id}").json()["facts"]
    assert {f["id"] for f in facts} == {"origin", "goal", "f001"}


def test_archive_facts_deletes_unreferenced_facts(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(f"/projects/{project_id}/facts", json={"description": "loose fact"})

    response = client.post(
        f"/projects/{project_id}/facts/archive", json={"fact_ids": ["f001"]}
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": 1, "skipped": []}
    facts = client.get(f"/projects/{project_id}").json()["facts"]
    assert {f["id"] for f in facts} == {"origin", "goal"}


def test_archive_facts_unknown_project_404(client: TestClient) -> None:
    response = client.post("/projects/proj_999/facts/archive", json={"fact_ids": ["f001"]})
    assert response.status_code == 404


def test_archive_facts_rejects_inactive_project(client: TestClient) -> None:
    project_id = _create_project(client)
    client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    response = client.post(
        f"/projects/{project_id}/facts/archive", json={"fact_ids": ["f001"]}
    )
    assert response.status_code == 403


def test_create_intent_dedupes_repeated_from_ids(client: TestClient) -> None:
    """LLM 输出的 from 含重复 id 时应去重而非主键冲突 500。"""
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin", "origin"], "description": "dup sources", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["from"] == ["origin"]


def test_conclude_persists_challenged_flag(client: TestClient) -> None:
    """双星质询过的发现写回后 challenged 落入 fact 与 intent，并出现在导出里。"""
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    client.post(f"/projects/{project_id}/intents/i001/heartbeat", json={"worker": "explorer"})
    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "vetted fact", "challenged": True},
    )
    assert response.status_code == 200
    assert response.json()["fact"]["challenged"] is True
    assert response.json()["intent"]["challenged"] is True

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["facts"][-1]["challenged"] is True
    exported = client.get(f"/projects/{project_id}/export?format=yaml").text
    assert "challenged: true" in exported
