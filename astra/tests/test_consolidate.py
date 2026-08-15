"""星尘记忆整理（consolidate）任务测试。"""

from __future__ import annotations

from astra.dispatcher.contracts import validate_consolidate_payload
from astra.dispatcher.protocol.client import ApiResult
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks import consolidate
from astra.dispatcher.tasks.consolidate import pick_stale_facts, run_consolidate_task
from astra.server.models import Fact

from conftest import FakeClient, FakeContainerManager, FakeDriver, make_config, make_project


def test_pick_stale_facts_excludes_goal_origin_and_summary() -> None:
    project = make_project()
    project.facts.append(Fact(id="f002", description="second", kind="summary"))
    project.facts.append(Fact(id="f003", description="third"))
    stale = pick_stale_facts(project, batch_size=10)
    ids = [item["id"] for item in stale]
    assert ids == ["f001", "f003"]  # goal/origin 与 summary 星记被排除


def test_pick_stale_facts_caps_batch_size() -> None:
    project = make_project()
    for i in range(5):
        project.facts.append(Fact(id=f"f0{i}", description=f"fact {i}"))
    stale = pick_stale_facts(project, batch_size=2)
    assert len(stale) == 2
    # 保持星图顺序取最老的两条
    assert [item["id"] for item in stale] == ["f001", "f00"]


def test_consolidate_writes_summary_fact(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()

    monkeypatch.setattr(consolidate, "get_driver", lambda _name: driver)
    monkeypatch.setattr(
        consolidate,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"description":"compressed old findings"}}',
            "",
        ),
    )

    outcome = run_consolidate_task(
        config,
        client,
        containers,
        project,
        "yaml",
        config.workers[0],
        TaskCancellation(),
    )
    assert outcome == "success"
    assert client.created_facts == [("proj_001", "compressed old findings", "summary", "test-worker")]
    # 被压缩的原始星记必须回收，防止只增不减反复触发整理
    assert client.archived_facts == [("proj_001", ["f001"])]


def test_consolidate_noop_when_no_stale_facts(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    project.facts = [fact for fact in project.facts if fact.id in ("goal", "origin")]
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()

    monkeypatch.setattr(consolidate, "get_driver", lambda _name: driver)

    outcome = run_consolidate_task(
        config,
        client,
        containers,
        project,
        "yaml",
        config.workers[0],
        TaskCancellation(),
    )
    assert outcome == "noop"


def test_validate_consolidate_payload() -> None:
    outcome, description = validate_consolidate_payload(
        {"accepted": True, "data": {"description": "summary text"}}
    )
    assert outcome == "fact"
    assert description == "summary text"

    outcome, _ = validate_consolidate_payload({"accepted": False, "reason": "no"})
    assert outcome == "rejected"


def test_validate_bootstrap_stream_multi_line() -> None:
    """bootstrap 增量流：多行 JSON 提取星记与 complete。"""
    from astra.dispatcher.contracts import validate_bootstrap_stream

    stream = (
        '{"accepted": true, "data": {"fact": {"description": "port 80 open"}}}\n'
        '{"accepted": true, "data": {"fact": {"description": "nginx 1.30"}}}\n'
        '{"accepted": true, "data": {"fact": {"description": "flag found"}, "complete": {"description": "goal met"}}}\n'
    )
    facts, complete = validate_bootstrap_stream(stream)
    assert facts == ["port 80 open", "nginx 1.30", "flag found"]
    assert complete == "goal met"


def test_validate_bootstrap_stream_single_object() -> None:
    """兼容旧单对象格式（含 markdown 围栏）。"""
    from astra.dispatcher.contracts import validate_bootstrap_stream

    single = '```json\n{"accepted": true, "data": {"fact": {"description": "goal done"}, "complete": {"description": "done"}}}\n```'
    facts, complete = validate_bootstrap_stream(single)
    assert facts == ["goal done"]
    assert complete == "done"


def test_validate_bootstrap_stream_partial() -> None:
    """超时抢救：未完成但已有部分增量行。"""
    from astra.dispatcher.contracts import validate_bootstrap_stream

    stream = (
        '{"accepted": true, "data": {"fact": {"description": "found 1"}}}\n'
        'noise line that is not json\n'
        '{"accepted": true, "data": {"fact": {"description": "found 2"}}}\n'
    )
    facts, complete = validate_bootstrap_stream(stream)
    assert facts == ["found 1", "found 2"]
    assert complete is None


def test_pick_stale_facts_excludes_intent_targets() -> None:
    """被 intent.to 引用的星记不参与压缩（归档它们会让 intent.to 悬挂）。"""
    from astra.server.models import Intent

    project = make_project()
    project.facts.append(Fact(id="f002", description="second"))
    project.intents.append(
        Intent(
            id="i001",
            from_=["origin"],
            to="f001",
            description="concluded",
            creator="explorer",
            worker="explorer",
            created_at="2026-01-01T00:00:02Z",
            concluded_at="2026-01-01T00:00:03Z",
        )
    )
    stale = pick_stale_facts(project, batch_size=10)
    assert [item["id"] for item in stale] == ["f002"]


def test_consolidate_crash_returns_failed(monkeypatch) -> None:
    """ensure_running 等基础设施异常必须兜底为 failed（进冷却），而非穿透 future 崩溃。"""
    config = make_config()
    project = make_project()
    client = FakeClient(project)

    class CrashingContainers(FakeContainerManager):
        def ensure_running(self, project_id: str) -> str:
            raise RuntimeError("docker daemon unavailable")

    outcome = run_consolidate_task(
        config,
        client,
        CrashingContainers(),
        project,
        "yaml",
        config.workers[0],
        TaskCancellation(),
    )
    assert outcome == "failed"
