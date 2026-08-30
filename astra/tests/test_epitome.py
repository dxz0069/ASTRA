"""Epitome 图导出压缩测试：阈值触发 / 保留集正确 / 开关关闭零影响。"""

from __future__ import annotations

import yaml
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
        json={"title": "test", "origin": "starting point", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _add_fact(client: TestClient, project_id: str, description: str, kind: str = "regular") -> str:
    response = client.post(
        f"/projects/{project_id}/facts",
        json={"description": description, "kind": kind, "creator": "tester"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _bulk(client: TestClient, project_id: str, count: int) -> None:
    for i in range(count):
        _add_fact(client, project_id, f"bulk fact {i}\ndetail line {i}")


def test_epitome_folds_only_stale_unreferenced_facts(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASTRA_EPITOME", "1")
    monkeypatch.setenv("ASTRA_EPITOME_THRESHOLD", "10")
    project_id = _create_project(client)

    early_stale = _add_fact(client, project_id, "早期浅层侦察记录\nSECONDLINE-EARLY-STALE")
    critical = _add_fact(client, project_id, "found credential\nSECONDLINE-CRITICAL flag{x}")
    negative = _add_fact(client, project_id, "此路不通\nSECONDLINE-NEGATIVE", kind="negative")
    stepref = _add_fact(client, project_id, "端口指纹\nSECONDLINE-STEPREF")
    step = client.post(
        f"/projects/{project_id}/steps",
        json={"from": [stepref], "description": "investigate", "creator": "decider"},
    )
    assert step.status_code == 201
    _bulk(client, project_id, 40)

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    text = exported.text
    data = yaml.safe_load(text)

    # 早期老旧且脱离因果链 → 折叠：首行保留、第二行丢失、带 [Epitome] 标记
    assert "早期浅层侦察记录" in text and "SECONDLINE-EARLY-STALE" not in text
    assert any("[Epitome]" in f["description"] for f in data["facts"])
    assert data["epitome"]["folded_facts"] >= 1

    # 保留集：关键（凭据/flag）/ 负结果 / 因果骨架（步骤来源）/ 近期 → 全量
    assert "SECONDLINE-CRITICAL" in text
    assert "SECONDLINE-NEGATIVE" in text
    assert "SECONDLINE-STEPREF" in text
    assert "detail line 39" in text  # 最近写入的 bulk 事实不折叠

    # 折叠不破坏 YAML 结构
    assert isinstance(data["facts"], list) and len(data["facts"]) >= 40


def test_epitome_respects_threshold_and_switch(client: TestClient, monkeypatch):
    project_id = _create_project(client)
    _add_fact(client, project_id, "小图事实\nSECONDLINE-SMALL")

    # 低于阈值：不折叠
    monkeypatch.setenv("ASTRA_EPITOME", "1")
    monkeypatch.setenv("ASTRA_EPITOME_THRESHOLD", "120")
    text = client.get(f"/projects/{project_id}/export?format=yaml").text
    assert "SECONDLINE-SMALL" in text and "[Epitome]" not in text

    # 超阈值但开关关闭：不折叠
    _bulk(client, project_id, 20)
    monkeypatch.setenv("ASTRA_EPITOME_THRESHOLD", "10")
    monkeypatch.setenv("ASTRA_EPITOME", "0")
    text = client.get(f"/projects/{project_id}/export?format=yaml").text
    assert "SECONDLINE-SMALL" in text and "[Epitome]" not in text
