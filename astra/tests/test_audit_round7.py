"""审计第七轮（v0.2 FGS 重写后首轮）：网络面加固 + 调度闭合 + 抢救语义回归。"""

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
        json={"title": "audit", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


BIG = "x" * 70_000


def test_client_writable_fields_reject_oversized_values(client: TestClient) -> None:
    """网络面加固：所有客户端可写字段有 max_length（422 而非入库膨胀）。"""
    pid = _create_project(client)

    # step description / expect 超长
    assert client.post(
        f"/projects/{pid}/steps",
        json={"from": ["origin"], "description": BIG, "creator": "d"},
    ).status_code == 422
    assert client.post(
        f"/projects/{pid}/steps",
        json={"from": ["origin"], "description": "ok", "expect": "y" * 9_000, "creator": "d"},
    ).status_code == 422
    # finding / subgoal / fact / hint / title 超长
    assert client.post(f"/projects/{pid}/findings", json={"description": BIG}).status_code == 422
    assert client.post(f"/projects/{pid}/subgoals", json={"description": BIG}).status_code == 422
    assert client.post(f"/projects/{pid}/facts", json={"description": BIG}).status_code == 422
    assert client.post(
        f"/projects/{pid}/hints", json={"content": BIG, "creator": "h"}
    ).status_code == 422
    assert client.put(f"/projects/{pid}/title", json={"title": BIG}).status_code == 422
    # worker/trigger 超长（heartbeat/decide claim）
    client.post(f"/projects/{pid}/steps", json={"from": ["origin"], "description": "s", "creator": "d"})
    assert client.post(
        f"/projects/{pid}/steps/s001/heartbeat", json={"worker": "w" * 300}
    ).status_code == 422
    assert client.post(
        f"/projects/{pid}/decide/claim", json={"worker": "w" * 300, "trigger": "t"}
    ).status_code == 422
    # from 列表超长（防批量主键撑爆）
    assert client.post(
        f"/projects/{pid}/steps",
        json={"from": ["origin"] * 600, "description": "s", "creator": "d"},
    ).status_code == 422


def test_close_step_records_closed_at(client: TestClient) -> None:
    """死路账本可观测性：关闭步骤落 closed_at 时间戳并可导出。"""
    pid = _create_project(client)
    client.post(f"/projects/{pid}/steps", json={"from": ["origin"], "description": "dead-end", "creator": "d"})
    response = client.post(f"/projects/{pid}/steps/s001/close", json={"reason": "exhausted"})
    assert response.status_code == 200
    assert response.json()["closed_at"], "closed_at 必须落库"
    assert response.json()["status"] == "closed"

    detail = client.get(f"/projects/{pid}").json()
    assert detail["steps"][0]["closed_at"]

    exported = client.get(f"/projects/{pid}/export?format=timeline").text
    assert "STEP CLOSED" in exported


def test_open_step_count_excludes_closed_steps() -> None:
    """调度闭合：关闭的死路步骤不得计入未决步骤数（防 decide 重触发语义错乱）。"""
    from astra.dispatcher.scheduler.loop import DispatcherLoop

    loop = DispatcherLoop.__new__(DispatcherLoop)
    project = _project_with_steps()
    # 1 个未决开放 + 1 个已关闭（to=None, status=closed）+ 1 个已收束（to=f001）
    assert loop._project_open_step_count(project) == 1


def test_decide_trigger_ignores_closed_steps() -> None:
    """全部步骤被 Decide 关闭（而非收束）时，不得触发 open_steps:N->0 假信号。"""
    from astra.dispatcher.models import DecideCheckpoint
    from astra.dispatcher.scheduler.loop import DispatcherLoop

    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.decide_checkpoints = {}
    project = _project_with_steps()  # 1 open + 1 closed + 1 concluded
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=len(project.facts), hint_count=0, open_step_count=2
    )
    # 快照时 2 未决；现在 1 个被关闭 → 真实未决 1，不构成 "->0" 事件
    assert loop._decide_trigger(project) is None


def _project_with_steps():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from astra.server.models import Fact, ProjectDetail, ProjectMeta, Step
    from tests.conftest import make_step

    step_open = make_step("s001")
    step_closed = make_step("s002").model_copy(update={"status": "closed", "close_reason": "dead end"})
    step_done = make_step("s003").model_copy(update={"to": "f001", "concluded_at": "2026-01-01T00:00:09Z"})
    project = ProjectDetail(
        project=ProjectMeta(
            id="proj_001", title="t", status="active", bootstrap_enabled=True,
            created_at="2026-01-01T00:00:00Z",
        ),
        facts=[
            Fact(id="origin", description="start"),
            Fact(id="goal", description="finish"),
            Fact(id="f001", description="known"),
        ],
        steps=[step_open, step_closed, step_done],
        hints=[],
        findings=[],
        subgoals=[],
    )
    return project


def test_rescue_dedup_uses_fresh_snapshot(monkeypatch) -> None:
    """流式抢救去重必须对新鲜项目快照做（并行 worker 已写入的同类发现不重复入图）。"""
    from astra.dispatcher.runtime.cancellation import TaskCancellation
    from astra.dispatcher.runtime.process import ProcessResult
    from astra.dispatcher.tasks import execute as execute_mod
    from astra.server.models import Fact

    from tests.conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_project, make_step

    config = make_config()
    step = make_step()
    stale = make_project(steps=[step])  # 派发时快照：无该事实
    fresh = make_project(steps=[step])  # 并行 worker 已写入
    fresh.facts.append(Fact(id="f900", description="确认发现：端口 8080 存在未授权访问路径"))

    client = FakeClient(stale)
    client.get_project = lambda _pid: fresh  # get_project 返回新鲜快照
    containers = FakeContainerManager()
    lease = FakeLease()

    streamed = (
        '{"accepted": true, "data": {"description": "确认发现：端口 8080 存在未授权访问路径"}}\n'
    )
    import types

    def fake_run_process(*_a, **_k):
        # 超时：did_timeout 为真（returncode 124）
        return ProcessResult(124, streamed, "")

    monkeypatch.setattr(execute_mod, "get_driver", lambda _n: FakeDriver())
    monkeypatch.setattr(execute_mod.HeartbeatLease, "for_step", lambda *a, **k: lease)
    monkeypatch.setattr(execute_mod, "run_healthcheck", lambda *a, **k: types.SimpleNamespace(
        result=ProcessResult(0, "", ""), duration_ms=1
    ))
    monkeypatch.setattr(execute_mod, "_run_process", fake_run_process)
    # conclude fallback 不可用 → 抢救后 failed 返回
    monkeypatch.setattr(
        execute_mod, "project_allows_conclude_fallback", lambda *a, **k: False
    )

    outcome = execute_mod.run_execute_task(
        config, client, containers, stale, "graph", step,
        config.workers[0], TaskCancellation(),
    )
    # 去重命中新鲜快照 → 不重复创建
    assert all("8080" not in (desc or "") for _, desc, _, _ in client.created_facts)
    assert outcome in ("failed", "success")


def test_decide_ops_capped_against_dump() -> None:
    """防倾倒：单轮 decide 的 close_steps/subgoals 超量部分被截断。"""
    from astra.dispatcher.contracts import (
        MAX_CLOSE_STEPS_PER_DECIDE,
        MAX_SUBGOALS_PER_DECIDE,
        validate_decide_payload,
    )

    payload = {
        "accepted": True,
        "data": {
            "close_steps": [{"id": f"s{i:03d}", "reason": "r"} for i in range(100)],
            "subgoals": [f"goal {i}" for i in range(50)],
        },
    }
    kind, data = validate_decide_payload(payload, open_steps_empty=False, max_steps=3)
    assert kind == "ops"
    assert len(data["close_steps"]) == MAX_CLOSE_STEPS_PER_DECIDE
    assert len(data["subgoals"]) == MAX_SUBGOALS_PER_DECIDE


def test_concurrent_claim_race_only_one_wins(client: TestClient) -> None:
    """并发竞态：两 worker 同时认领同一步骤，只有守卫 UPDATE 的胜者生效。

    模拟方式：worker-a 持有未过期租约时 worker-b 认领必须 409；
    且 409 事务回滚后不留下任何副作用（dispatch_count 不变）。
    """
    pid = _create_project(client)
    client.post(
        f"/projects/{pid}/steps",
        json={"from": ["origin"], "description": "race", "creator": "a", "worker": None},
    )
    # a 首次认领（NULL→a 跃迁计一次派发）
    assert client.post(f"/projects/{pid}/steps/s001/heartbeat", json={"worker": "a"}).status_code == 200
    # a 持有新鲜租约 → b 原子认领必须败且无副作用
    r = client.post(f"/projects/{pid}/steps/s001/heartbeat", json={"worker": "b"})
    assert r.status_code == 409
    detail = client.get(f"/projects/{pid}").json()
    step = detail["steps"][0]
    assert step["worker"] == "a"
    assert step["dispatch_count"] == 1  # 败者不 inflate 计数
    # 同 worker 幂等续租不重复计数
    assert client.post(f"/projects/{pid}/steps/s001/heartbeat", json={"worker": "a"}).status_code == 200
    detail = client.get(f"/projects/{pid}").json()
    assert detail["steps"][0]["dispatch_count"] == 1


def test_concurrent_decide_claim_race_only_one_wins(client: TestClient) -> None:
    """并发竞态：decide 认领同图串行——活跃租约下第二个 worker 必须 409 且无副作用。"""
    pid = _create_project(client)
    first = client.post(f"/projects/{pid}/decide/claim", json={"worker": "w1", "trigger": "t"})
    assert first.status_code == 200
    token1 = first.json()["decide_token"]
    r = client.post(f"/projects/{pid}/decide/claim", json={"worker": "w2", "trigger": "t"})
    assert r.status_code == 409
    # w1 幂等重认领拿回自己的令牌
    again = client.post(f"/projects/{pid}/decide/claim", json={"worker": "w1", "trigger": "t"})
    assert again.status_code == 200
    assert again.json()["decide_token"] == token1


def test_conclude_lost_race_no_orphan_fact(client: TestClient) -> None:
    """并发竞态：收束败者（他 worker 持有租约）不得留下孤儿 fact。"""
    pid = _create_project(client)
    client.post(
        f"/projects/{pid}/steps",
        json={"from": ["origin"], "description": "c", "creator": "a", "worker": "a"},
    )
    r = client.post(
        f"/projects/{pid}/steps/s001/conclude",
        json={"worker": "intruder", "description": "raced fact"},
    )
    assert r.status_code == 409
    facts = client.get(f"/projects/{pid}").json()["facts"]
    assert not any("raced fact" in f["description"] for f in facts)  # 事务回滚无孤儿
