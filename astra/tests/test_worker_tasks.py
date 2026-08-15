from __future__ import annotations

from collections.abc import Iterator

from astra.dispatcher.protocol.client import ApiResult
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks.common import HealthcheckRun
from astra.dispatcher.tasks import bootstrap, explore, reason

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_intent,
    make_project,
)


def _healthy(*_args, **_kwargs) -> HealthcheckRun:
    return HealthcheckRun(ProcessResult(0, "", ""), duration_ms=1)


def _lease_factory(lease: FakeLease):
    return lambda *_args, **_kwargs: lease


def test_reason_writes_graph_snapshot_and_creates_intent(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    graph_yaml = "project:\n  title: huge\n" + ("x" * 100_000)

    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(reason, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next step"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        graph_yaml,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next step", "test-worker")]
    assert client.released_reasons == [("proj_001", "test-worker")]
    assert lease.started and lease.stopped
    assert len(containers.writes) == 2  # 执行快照 + 双星审查快照
    container_name, path, content = containers.writes[0]
    assert container_name == "container-proj_001"
    assert path.startswith("/tmp/astra-prompts/reason_execute-")
    assert path.endswith("/graph.yaml")
    assert content == graph_yaml
    assert graph_yaml not in driver.execute_prompts[0]
    assert path in driver.execute_prompts[0]
    assert containers.writes[1][1].startswith("/tmp/astra-prompts/review-")


def test_explore_early_plain_text_exit_uses_conclude_fallback(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Need inspect files and keep working.", ""),
            ProcessResult(0, '{"accepted":true,"data":{"description":"confirmed fact"}}', ""),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "run_healthcheck", _healthy)
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: next(results))

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "confirmed fact")]
    assert len(containers.writes) == 2
    assert "/explore_execute-" in containers.writes[0][1]
    assert "/explore_conclude-" in containers.writes[1][1]
    assert len(driver.execute_prompts) == 1
    assert len(driver.conclude_prompts) == 1
    assert lease.started and lease.stopped


def test_explore_healthcheck_failure_releases_claim(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_and_task"
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(explore, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(
        explore,
        "run_healthcheck",
        lambda *_args, **_kwargs: HealthcheckRun(ProcessResult(1, "", "unhealthy"), duration_ms=1),
    )

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "graph",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "unhealthy"
    assert client.released == [("proj_001", "i001", "test-worker")]
    assert containers.writes == []


def test_bootstrap_success_concludes_fact_then_completes_project(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda _name: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(bootstrap, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        bootstrap,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"fact":{"description":"solved"},'
            '"complete":{"description":"goal met"}}}',
            "",
        ),
    )

    outcome = bootstrap.run_bootstrap_task(
        config,
        client,
        containers,
        project,
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "solved")]
    assert client.completed == [("proj_001", ["f002"], "goal met", "test-worker")]
    assert lease.started and lease.stopped


def test_reason_complete_treats_inactive_project_as_success(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    def complete(*_args, **_kwargs) -> ApiResult:
        return ApiResult(403, text="inactive")

    client.complete = complete  # type: ignore[method-assign]
    monkeypatch.setattr(reason, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(reason, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.released_reasons == [("proj_001", "test-worker")]


def test_reason_startup_only_mode_skips_task_healthcheck(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_only"
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_healthcheck",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("task healthcheck should be skipped")),
    )
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next", "test-worker")]


def test_reason_inline_context_capped_by_budget(monkeypatch) -> None:
    """星尘记忆：大图场景下 reason 内联 fact_ids/open_intents 有硬上限。"""
    import json

    from astra.server.models import Fact

    config = make_config()
    config.runtime.context_budget.max_inline_facts = 5
    config.runtime.context_budget.max_inline_intents = 2

    project = make_project()
    for i in range(40):
        project.facts.append(Fact(id=f"f{i:03d}", description=f"finding number {i} about port {80 + i}"))
    for i in range(6):
        project.intents.append(
            make_intent(intent_id=f"i{i:03d}").model_copy(
                update={"created_at": f"2026-01-01T00:00:{i:02d}Z"}
            )
        )

    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(reason, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f000"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "yaml",
        config.workers[0],
        TaskCancellation(),
    )
    assert outcome == "success"

    prompt_text = driver.execute_prompts[0]
    fact_ids_block = prompt_text.split("### Valid facts")[1].split("```")[1]
    fact_ids = json.loads(fact_ids_block)
    assert len(fact_ids) <= config.runtime.context_budget.max_inline_facts
    assert "f000" not in fact_ids  # 最老的星记已被裁剪出内联

    open_intents_block = prompt_text.split("### Open Intents")[1].split("```")[1]
    open_intents = json.loads(open_intents_block)
    assert len(open_intents) <= config.runtime.context_budget.max_inline_intents
    # 最新航向优先
    assert open_intents[0]["id"] == "i005"


def test_bootstrap_incremental_stream_writes_all_facts(monkeypatch) -> None:
    """bootstrap 增量流：多行星记全部写回，末行 complete 归航。"""
    from astra.dispatcher.tasks import bootstrap

    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda _name: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(bootstrap, "run_healthcheck", _healthy)
    stream = (
        '{"accepted": true, "data": {"fact": {"description": "first finding"}}}\n'
        '{"accepted": true, "data": {"fact": {"description": "second finding"}}}\n'
        '{"accepted": true, "data": {"fact": {"description": "flag captured"}, "complete": {"description": "goal met"}}}\n'
    )
    monkeypatch.setattr(
        bootstrap,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(0, stream, ""),
    )

    outcome = bootstrap.run_bootstrap_task(
        config, client, containers, project, intent,
        config.workers[0], TaskCancellation(),
    )
    assert outcome == "success"
    assert client.created_facts == [
        ("proj_001", "first finding", "regular", "test-worker"),
        ("proj_001", "second finding", "regular", "test-worker"),
    ]
    assert client.concluded == [("proj_001", "i001", "test-worker", "flag captured")]
    assert client.completed


def test_find_duplicate_fact_detects_similar_and_skips_flag() -> None:
    from astra.dispatcher.tasks.common import find_duplicate_fact
    from astra.server.models import Fact

    project = make_project()
    project.facts.append(Fact(id="f_scan", description="端口 8080 开放，运行 tomcat 9.0 存在弱口令", confidence="high"))

    # 同一发现几乎同措辞 → 命中
    dup = find_duplicate_fact(project, "端口 8080 开放，tomcat 9.0 弱口令可登录")
    assert dup is not None and dup.id == "f_scan"
    # 主题不同 → 不命中
    assert find_duplicate_fact(project, "SSH 端口 22 允许弱口令登录") is None
    # 含 flag 的描述永不参与去重
    assert find_duplicate_fact(project, "端口 8080 开放 tomcat flag{abc123def456}") is None


def test_explore_low_confidence_fact_challenged_down(monkeypatch) -> None:
    """低置信巡猎发现被质询否决 → 不写回 + [审查否决] hint（重构备忘候选 4）。"""
    from astra.server.models import Fact

    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    project.facts.append(Fact(id="f_existing", description="已确认的服务指纹", confidence="high"))
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"端口 9999 疑似存在后门服务","confidence":"low"}}',
                "",
            ),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "run_healthcheck", _healthy)
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: next(results))
    # 质询链路：明确否决
    monkeypatch.setattr(
        reason,
        "_run_review_stage_with_retry",
        lambda *_a, **_k: {"accepted": False, "reason": "无命令执行证据，仅推断"},
    )

    outcome = explore.run_explore_task(
        config, client, containers, project, "graph",
        intent, config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == []  # 未写回
    assert any("[审查否决]" in content for _, content, _ in client.created_hints)


def test_explore_low_confidence_fact_challenge_unavailable_degrades(monkeypatch) -> None:
    """质询链路不可用 → 降级放行（不因审查基础设施故障丢失发现）。"""
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"端口 9999 疑似后门","confidence":"low"}}',
                "",
            ),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "run_healthcheck", _healthy)
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(reason, "_run_review_stage_with_retry", lambda *_a, **_k: None)

    outcome = explore.run_explore_task(
        config, client, containers, project, "graph",
        intent, config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "端口 9999 疑似后门")]
    assert any("[审查否决]" in content for _, content, _ in client.created_hints)


def test_explore_fact_duplicate_skipped(monkeypatch) -> None:
    """巡猎发现与既有星记高度相似 → 去重不写回 + hint（防重复侦察）。"""
    from astra.server.models import Fact

    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    project.facts.append(Fact(id="f_scan", description="端口 8080 开放，运行 tomcat 9.0 存在弱口令", confidence="high"))
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"端口 8080 开放，tomcat 9.0 弱口令可登录","confidence":"high"}}',
                "",
            ),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "run_healthcheck", _healthy)
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: next(results))

    outcome = explore.run_explore_task(
        config, client, containers, project, "graph",
        intent, config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == []  # 重复未写回
    assert any("重复" in content for _, content, _ in client.created_hints)


def test_review_graph_summary_compact() -> None:
    """审查图摘要：内联 Goal/Facts/Intents/Hints，截断长描述（读图提速候选 20）。"""
    from astra.dispatcher.tasks.common import review_graph_summary
    from astra.server.models import Fact, Hint

    intent = make_intent()
    project = make_project(intents=[intent])
    project.facts.append(Fact(id="f_scan", description="端口 8080 开放，运行 tomcat 9.0 存在弱口令，可进一步利用", confidence="high"))
    project.hints.append(Hint(id="h_extra", content="提示：检查默认凭据", creator="human", created_at="2026-01-01T00:00:03Z"))

    summary = review_graph_summary(project)

    assert "Goal: finish" in summary
    assert "f001: known fact" in summary
    assert "f_scan: 端口 8080" in summary
    assert "i001: investigate" in summary  # intent 摘要
    assert "Hints: 2 条" in summary or "Hints: 1 条" in summary
    # 长描述被截断（不超过 120 字）
    for line in summary.splitlines():
        if line.startswith("  - "):
            assert len(line) <= 140


def test_review_graph_summary_omits_origin_goal() -> None:
    """摘要 Facts 列表不含 origin/goal 星记（只保留可审查的发现）。"""
    from astra.dispatcher.tasks.common import review_graph_summary

    summary = review_graph_summary(make_project())
    assert "  - origin:" not in summary
    assert "  - goal:" not in summary  # goal 只出现在 Goal 标题行，不出现在 Facts 列表
    assert "Goal:" in summary


def test_reason_intent_duplicate_of_existing_fact_skipped(monkeypatch) -> None:
    """定航提议的航向与既有星记高度相似 → 不创建 intent。"""
    from astra.server.models import Fact

    config = make_config()
    project = make_project()
    project.facts.append(Fact(id="f_scan", description="端口 8080 开放，运行 tomcat 9.0 存在弱口令", confidence="high"))
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(reason, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"端口 8080 开放 tomcat 9.0 弱口令"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config, client, containers, project, "graph",
        config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == []  # 重复方向未创建
