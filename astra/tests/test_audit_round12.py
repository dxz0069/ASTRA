"""审计第十二轮：dispatcher 任务层异常交叠——decide 写图前租约复查（D2 复活修复）+ 引擎句柄回收。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pathlib import Path

from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks import decide as decide_mod

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_project,
)


def _healthy(*_a, **_k):
    from astra.dispatcher.tasks.common import HealthcheckRun

    return HealthcheckRun(ProcessResult(0, "", ""), duration_ms=1)


def _lease_factory(lease: FakeLease):
    return lambda *_a, **_k: lease


def test_decide_aborts_complete_when_lease_lost(monkeypatch) -> None:
    """D2 复活修复：decide 长跑期间租约失效 → complete 不写图（防与重派 decide 双写）。"""
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    monkeypatch.setattr(decide_mod, "get_driver", lambda _n: FakeDriver())
    monkeypatch.setattr(decide_mod.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide_mod, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide_mod,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0, '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}', "",
        ),
    )
    # 模拟：模型跑完时租约已失效（900s 长跑 > decide_timeout）
    lease.failure = type("F", (), {"status_code": 409})()

    outcome = decide_mod.run_decide_task(
        config, client, containers, project, "graph",
        config.workers[0], TaskCancellation(),
    )
    assert outcome == "failed"
    assert client.completed == []  # 关键：没写图


def test_decide_aborts_step_writes_when_lease_lost_mid_ops(monkeypatch) -> None:
    """ops 批量写中途租约失效：已写的保留，剩余的放弃。"""
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    # 第一次 create_step 成功后置 lease.failure（模拟写第二笔前租约过期）
    state = {"first": False}

    def fake_create_step(project_id, from_ids, description, creator, expect=None):
        from astra.dispatcher.protocol.client import ApiResult

        if not state["first"]:
            state["first"] = True
            return ApiResult(201, {})
        lease.failure = type("F", (), {"status_code": 409})()
        return ApiResult(201, {})

    client.create_step = fake_create_step  # type: ignore[method-assign]

    monkeypatch.setattr(decide_mod, "get_driver", lambda _n: FakeDriver())
    monkeypatch.setattr(decide_mod.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide_mod, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide_mod,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":['
            '{"from":["f001"],"description":"first step"},'
            '{"from":["f001"],"description":"second step"},'
            '{"from":["f001"],"description":"third step"}]}}',
            "",
        ),
    )

    outcome = decide_mod.run_decide_task(
        config, client, containers, project, "graph",
        config.workers[0], TaskCancellation(),
    )
    # 第一步已写成功（有产出 → success 语义），二三步因租约失效放弃
    assert state["first"] is True
    assert outcome == "success"


def test_engine_log_handles_recycled(monkeypatch, tmp_path) -> None:
    """引擎句柄回收：多次 _popen 后句目数有界，shutdown 清零。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "container"))
    from astra_runner import astra_runner_engine as eng

    # 不真起进程：伪造 Popen
    class FakeProc:
        def __init__(self):
            self.pid = 12345

    monkeypatch.setattr(eng.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(eng, "_engine_log_path", lambda: tmp_path / "e.log")

    for _ in range(10):
        eng._popen(["x"])
    assert len(eng._open_log_handles) <= 6  # 有界

    daemon = eng.AstraDaemon.__new__(eng.AstraDaemon)
    daemon._server = None
    daemon._dispatcher = None
    daemon._dispatch_config = None
    import requests

    monkeypatch.setattr(
        eng.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError())
    )  # 端口已释放 → 立即 break
    daemon.shutdown()
    assert len(eng._open_log_handles) == 0  # 全回收
