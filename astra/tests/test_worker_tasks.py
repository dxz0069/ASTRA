from __future__ import annotations

from collections.abc import Iterator

from astra.dispatcher.protocol.client import ApiResult
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks.common import HealthcheckRun
from astra.dispatcher.tasks import bootstrap, decide, execute

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_step,
    make_project,
)


def _healthy(*_args, **_kwargs) -> HealthcheckRun:
    return HealthcheckRun(ProcessResult(0, "", ""), duration_ms=1)


def _lease_factory(lease: FakeLease):
    return lambda *_args, **_kwargs: lease


def test_decide_writes_graph_snapshot_and_creates_step(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    graph_yaml = "project:\n  title: huge\n" + ("x" * 100_000)

    monkeypatch.setattr(decide, "get_driver", lambda _name: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"next step","expect":"new fact"}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        containers,
        project,
        graph_yaml,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_steps == [("proj_001", ["f001"], "next step", "test-worker")]
    assert client.released_decides == [("proj_001", "test-worker")]
    assert lease.started and lease.stopped
    assert len(containers.writes) == 1  # Decide 只写一份执行快照（无审查链）
    container_name, path, content = containers.writes[0]
    assert container_name == "container-proj_001"
    assert path.startswith("/tmp/astra-prompts/decide_execute-")
    assert path.endswith("/graph.yaml")
    assert content == graph_yaml
    assert graph_yaml not in driver.execute_prompts[0]
    assert path in driver.execute_prompts[0]


def test_decide_close_and_subgoal_ops(monkeypatch) -> None:
    """Decide 输出 close_steps/subgoals → 关步骤留痕 + 增子目标。"""
    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"close_steps":[{"id":"s001","reason":"exhausted"}],'
            '"subgoals":["get a foothold"]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config, client, containers, project, "graph",
        config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.closed_steps == [("proj_001", "s001", "exhausted")]
    assert client.created_subgoals == [("proj_001", "get a foothold")]


def test_execute_early_plain_text_exit_uses_conclude_fallback(monkeypatch) -> None:
    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
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

    monkeypatch.setattr(execute, "get_driver", lambda _name: driver)
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(lease))
    monkeypatch.setattr(execute, "run_healthcheck", _healthy)
    monkeypatch.setattr(execute, "_run_process", lambda *_args, **_kwargs: next(results))

    outcome = execute.run_execute_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: f001\n",
        step,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "s001", "test-worker", "confirmed fact")]
    assert len(containers.writes) == 2
    assert "/execute_execute-" in containers.writes[0][1]
    assert "/execute_conclude-" in containers.writes[1][1]
    assert len(driver.execute_prompts) == 1
    assert len(driver.conclude_prompts) == 1
    assert lease.started and lease.stopped


def test_execute_finding_submitted_with_fact(monkeypatch) -> None:
    """Execute 沿途发现：conclude 携 finding 一并写回。"""
    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(execute, "get_driver", lambda _name: driver)
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(lease))
    monkeypatch.setattr(execute, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        execute,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"description":"SQLi confirmed","finding":{"description":"SQL injection at /login"}}}',
            "",
        ),
    )

    outcome = execute.run_execute_task(
        config, client, containers, project, "graph",
        step, config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "s001", "test-worker", "SQLi confirmed")]
    assert client.created_findings == [("proj_001", "SQL injection at /login")]


def test_execute_healthcheck_failure_releases_claim(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_and_task"
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(execute, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(lease))
    monkeypatch.setattr(
        execute,
        "run_healthcheck",
        lambda *_args, **_kwargs: HealthcheckRun(ProcessResult(1, "", "unhealthy"), duration_ms=1),
    )

    outcome = execute.run_execute_task(
        config,
        client,
        containers,
        project,
        "graph",
        step,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "unhealthy"
    assert client.released == [("proj_001", "s001", "test-worker")]
    assert containers.writes == []


def test_bootstrap_success_concludes_fact_then_completes_project(monkeypatch) -> None:
    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda _name: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_step", _lease_factory(lease))
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
        step,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "s001", "test-worker", "solved")]
    assert client.completed == [("proj_001", ["f002"], "goal met", "test-worker")]
    assert lease.started and lease.stopped


def test_decide_complete_treats_inactive_project_as_success(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    def complete(*_args, **_kwargs) -> ApiResult:
        return ApiResult(403, text="inactive")

    client.complete = complete  # type: ignore[method-assign]
    monkeypatch.setattr(decide, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.released_decides == [("proj_001", "test-worker")]


def test_decide_startup_only_mode_skips_task_healthcheck(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_only"
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_healthcheck",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("task healthcheck should be skipped")),
    )
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_steps == [("proj_001", ["f001"], "next", "test-worker")]


def test_decide_inline_context_capped_by_budget(monkeypatch) -> None:
    """大图场景下 decide 内联 fact_ids/open_steps 有硬上限。"""
    import json

    from astra.server.models import Fact

    config = make_config()
    config.runtime.context_budget.max_inline_facts = 5
    config.runtime.context_budget.max_inline_steps = 2

    project = make_project()
    for i in range(40):
        project.facts.append(Fact(id=f"f{i:03d}", description=f"finding number {i} about port {80 + i}"))
    for i in range(6):
        project.steps.append(
            make_step(step_id=f"s{i:03d}").model_copy(
                update={"created_at": f"2026-01-01T00:00:{i:02d}Z"}
            )
        )

    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda _name: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f000"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
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
    assert "f000" not in fact_ids  # 最老的事实已被裁剪出内联

    open_steps_block = prompt_text.split("### Open Steps")[1].split("```")[1]
    open_steps = json.loads(open_steps_block)
    assert len(open_steps) <= config.runtime.context_budget.max_inline_steps
    # 最新步骤优先
    assert open_steps[0]["id"] == "s005"


def test_bootstrap_incremental_stream_writes_all_facts(monkeypatch) -> None:
    """bootstrap 增量流：多行事实全部写回，末行 complete 完成项目。"""
    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda _name: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_step", _lease_factory(lease))
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
        config, client, containers, project, step,
        config.workers[0], TaskCancellation(),
    )
    assert outcome == "success"
    assert client.created_facts == [
        ("proj_001", "first finding", "regular", "test-worker"),
        ("proj_001", "second finding", "regular", "test-worker"),
    ]
    assert client.concluded == [("proj_001", "s001", "test-worker", "flag captured")]
    assert client.completed


def test_find_duplicate_fact_detects_similar_and_skips_flag() -> None:
    from astra.dispatcher.tasks.common import find_duplicate_fact
    from astra.server.models import Fact

    project = make_project()
    project.facts.append(Fact(id="f_scan", description="端口 8080 开放，运行 tomcat 9.0 存在弱口令"))

    # 同一发现几乎同措辞 → 命中
    dup = find_duplicate_fact(project, "端口 8080 开放，tomcat 9.0 弱口令可登录")
    assert dup is not None and dup.id == "f_scan"
    # 主题不同 → 不命中
    assert find_duplicate_fact(project, "SSH 端口 22 允许弱口令登录") is None
    # 含 flag 的描述永不参与去重
    assert find_duplicate_fact(project, "端口 8080 开放 tomcat flag{abc123def456}") is None


def test_execute_fact_duplicate_skipped(monkeypatch) -> None:
    """执行发现与既有事实高度相似 → 去重不写回 + hint（防重复侦察）。"""
    from astra.server.models import Fact

    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
    project.facts.append(Fact(id="f_scan", description="端口 8080 开放，运行 tomcat 9.0 存在弱口令"))
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"端口 8080 开放，tomcat 9.0 弱口令可登录"}}',
                "",
            ),
        ]
    )

    monkeypatch.setattr(execute, "get_driver", lambda _name: driver)
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(lease))
    monkeypatch.setattr(execute, "run_healthcheck", _healthy)
    monkeypatch.setattr(execute, "_run_process", lambda *_args, **_kwargs: next(results))

    outcome = execute.run_execute_task(
        config, client, containers, project, "graph",
        step, config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == []  # 重复未写回
    assert any("重复" in content for _, content, _ in client.created_hints)


def test_decide_step_duplicate_of_existing_fact_skipped(monkeypatch) -> None:
    """Decide 提议的步骤与既有事实高度相似 → 不创建 step。"""
    from astra.server.models import Fact

    config = make_config()
    project = make_project()
    project.facts.append(Fact(id="f_scan", description="端口 8080 开放，运行 tomcat 9.0 存在弱口令"))
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda _name: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"端口 8080 开放 tomcat 9.0 弱口令"}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config, client, containers, project, "graph",
        config.workers[0], TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_steps == []  # 重复方向未创建


def test_execute_rescue_streamed_facts_capped() -> None:
    """审计17轮：超时抢救的流式天枢条数封顶——万行 stdout 只入图 MAX_STREAM_FACTS 条。"""
    from astra.dispatcher.contracts import MAX_STREAM_FACTS

    project = make_project()
    client = FakeClient(project)
    stdout = "\n".join(
        '{"accepted":true,"data":{"description":"端口 80/%d 开放且版本指纹确认存在已知漏洞利用面"' % i + "}}"
        for i in range(1000)
    )
    rescued = execute._rescue_streamed_facts(client, project, make_step(), stdout)
    assert rescued == MAX_STREAM_FACTS
    assert len(client.created_facts) == MAX_STREAM_FACTS
