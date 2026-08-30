"""质询星探测试：契约校验 / complete 把关 / fail-open / 预算有界 / 关键事实异步审计。"""

from __future__ import annotations

import pytest

from astra.dispatcher.protocol.client import ApiResult
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks import challenge, decide, execute
from astra.dispatcher.contracts import validate_challenge_payload

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_step,
    make_project,
)


def _healthy(*_args, **_kwargs):
    from astra.dispatcher.tasks.common import HealthcheckRun

    return HealthcheckRun(ProcessResult(0, "", ""), duration_ms=1)


def _lease_factory(lease: FakeLease):
    return lambda *_args, **_kwargs: lease


@pytest.fixture(autouse=True)
def _isolate_challenge_state(monkeypatch):
    monkeypatch.delenv("ASTRA_CHALLENGE_MODE", raising=False)
    challenge.reset_challenge_state_for_tests()
    yield
    challenge.reset_challenge_state_for_tests()


# ---------------- 契约 ----------------

def test_challenge_payload_contract():
    assert validate_challenge_payload({"accepted": True, "data": {"verdict": "uphold"}}) == ("uphold", None)
    assert validate_challenge_payload({"verdict": "UPHELD"}) == ("uphold", None)
    kind, data = validate_challenge_payload(
        {"accepted": True, "data": {"verdict": "refute", "reason": "no proof of file write"}}
    )
    assert kind == "refute" and data == {"reason": "no proof of file write"}
    assert validate_challenge_payload({"accepted": False, "reason": "policy"})[0] == "rejected"
    with pytest.raises(ValueError):
        validate_challenge_payload({"verdict": "refute"})  # refute 必须给理由
    with pytest.raises(ValueError):
        validate_challenge_payload({"verdict": "maybe"})


# ---------------- complete 收束把关 ----------------

def _run_decide_complete(monkeypatch, challenge_output: str | None, *, env_off: bool = False):
    """跑一次输出 complete 的 decide；challenge_output=None 表示质询不应被调用。"""
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()
    challenge_calls: list[int] = []

    monkeypatch.setattr(decide, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0, '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"all done"}}}', ""
        ),
    )
    monkeypatch.setattr(challenge, "get_driver", lambda _name: FakeDriver())

    def _challenge_process(*_a, **_k):
        challenge_calls.append(1)
        assert challenge_output is not None
        return ProcessResult(0, challenge_output, "")

    monkeypatch.setattr(challenge, "run_worker_process", _challenge_process)
    if env_off:
        monkeypatch.setenv("ASTRA_CHALLENGE_MODE", "0")

    outcome = decide.run_decide_task(
        config, client, containers, project, "graph", config.workers[0], TaskCancellation(),
    )
    return outcome, client, challenge_calls


def test_decide_complete_refuted_by_challenge(monkeypatch):
    outcome, client, calls = _run_decide_complete(
        monkeypatch,
        '{"accepted":true,"data":{"verdict":"refute","reason":"flag unverified on second target"}}',
    )
    assert outcome == "success"
    assert calls, "质询应被调用"
    assert client.completed == []  # 收束被驳：不写 complete
    refute_hints = [h for h in client.created_hints if "质询星探" in h[1]]
    assert refute_hints and "flag unverified" in refute_hints[0][1]


def test_decide_complete_upheld_writes_complete(monkeypatch):
    outcome, client, calls = _run_decide_complete(
        monkeypatch, '{"accepted":true,"data":{"verdict":"uphold"}}'
    )
    assert outcome == "success"
    assert calls
    assert client.completed == [("proj_001", ["f001"], "all done", "test-worker")]


def test_decide_complete_challenge_fail_open_on_garbage(monkeypatch):
    """质询输出坏 JSON → fail-open 放行（质询故障绝不卡死收束）。"""
    outcome, client, _ = _run_decide_complete(monkeypatch, "not json at all{{{")
    assert outcome == "success"
    assert client.completed == [("proj_001", ["f001"], "all done", "test-worker")]


def test_decide_complete_challenge_disabled_skips_review(monkeypatch):
    outcome, client, calls = _run_decide_complete(
        monkeypatch, '{"accepted":true,"data":{"verdict":"refute","reason":"x"}}', env_off=True
    )
    assert outcome == "success"
    assert calls == []  # 开关关闭零调用
    assert client.completed == [("proj_001", ["f001"], "all done", "test-worker")]


def test_decide_complete_challenge_budget_exhausted(monkeypatch):
    config = make_config()
    with challenge._counts_lock:
        challenge._challenge_counts["proj_001"] = config.tasks.challenge.max_per_project
    outcome, client, calls = _run_decide_complete(
        monkeypatch, '{"accepted":true,"data":{"verdict":"refute","reason":"x"}}'
    )
    assert outcome == "success"
    assert calls == []  # 预算耗尽不再质询
    assert client.completed == [("proj_001", ["f001"], "all done", "test-worker")]


# ---------------- 关键事实异步审计 ----------------

def test_execute_critical_fact_submits_audit(monkeypatch):
    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()
    submitted: list[str] = []

    monkeypatch.setattr(execute, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(lease))
    monkeypatch.setattr(execute, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        execute,
        "_run_process",
        lambda *_a, **_k: ProcessResult(
            0, '{"accepted":true,"data":{"description":"found flag{abc} in /tmp/proof.txt"}}', ""
        ),
    )
    monkeypatch.setattr(execute, "submit_critical_fact_audit", lambda *a, **k: submitted.append(a[-1]))

    outcome = execute.run_execute_task(
        config, client, containers, project, "graph", step, config.workers[0], TaskCancellation(),
    )
    assert outcome == "success"
    assert submitted == ["found flag{abc} in /tmp/proof.txt"]


def test_execute_plain_fact_skips_audit(monkeypatch):
    config = make_config()
    step = make_step()
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()
    submitted: list[str] = []

    monkeypatch.setattr(execute, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(lease))
    monkeypatch.setattr(execute, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        execute,
        "_run_process",
        lambda *_a, **_k: ProcessResult(0, '{"accepted":true,"data":{"description":"端口 8080 开放"}}', ""),
    )
    monkeypatch.setattr(execute, "submit_critical_fact_audit", lambda *a, **k: submitted.append(a[-1]))

    outcome = execute.run_execute_task(
        config, client, containers, project, "graph", step, config.workers[0], TaskCancellation(),
    )
    assert outcome == "success"
    assert submitted == []  # 非关键事实不审计


def test_audit_refute_writes_hint(monkeypatch):
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    monkeypatch.setattr(challenge, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(
        challenge,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0, '{"accepted":true,"data":{"verdict":"refute","reason":"凭据未在目标服务实测"}}', ""
        ),
    )
    queued = challenge.submit_critical_fact_audit(
        config, client, containers, project.project.id, "graph",
        config.workers[0], "password=adm1n 已确认可登录",
    )
    assert queued
    challenge.drain_audit_queue_for_tests()
    refute_hints = [h for h in client.created_hints if "质询星探质疑·关键结论" in h[1]]
    assert refute_hints and "凭据未在目标服务实测" in refute_hints[0][1]


def test_audit_uphold_writes_no_hint(monkeypatch):
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    monkeypatch.setattr(challenge, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(
        challenge,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(0, '{"accepted":true,"data":{"verdict":"uphold"}}', ""),
    )
    assert challenge.submit_critical_fact_audit(
        config, client, containers, project.project.id, "graph",
        config.workers[0], "password=adm1n 已确认可登录",
    )
    challenge.drain_audit_queue_for_tests()
    assert client.created_hints == []
