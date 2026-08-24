"""双星决策（质询+裁决）测试。"""

from __future__ import annotations

from astra.dispatcher.contracts import (
    validate_challenge_payload,
    validate_verdict_payload,
)
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.tasks import reason

from conftest import FakeClient, FakeContainerManager, FakeDriver, make_config, make_project


def test_validate_challenge_payload_accepted() -> None:
    outcome, result = validate_challenge_payload(
        {"accepted": True, "objections": ["no evidence"], "confidence": "low"}
    )
    assert outcome == "accepted"
    assert result == {"objections": ["no evidence"], "confidence": "low"}


def test_validate_challenge_payload_defaults() -> None:
    outcome, result = validate_challenge_payload({"accepted": True})
    assert outcome == "accepted"
    assert result == {"objections": [], "confidence": "medium"}


def test_validate_challenge_payload_rejected() -> None:
    outcome, _ = validate_challenge_payload({"accepted": False, "reason": "wrong direction"})
    assert outcome == "rejected"


def test_validate_verdict_payload_complete() -> None:
    payload = {"accepted": True, "data": {"complete": {"from": ["f001"], "description": "goal met"}}}
    outcome, data = validate_verdict_payload(payload, "complete")
    assert outcome == "complete"
    assert data["description"] == "goal met"


def test_validate_verdict_payload_intents() -> None:
    payload = {"accepted": True, "data": {"intents": [{"from": ["f001"], "description": "next"}]}}
    outcome, data = validate_verdict_payload(payload, "intents")
    assert outcome == "intents"
    assert data[0]["description"] == "next"


def test_validate_verdict_payload_kind_mismatch() -> None:
    import pytest

    payload = {"accepted": True, "data": {"complete": {"from": ["f001"], "description": "x"}}}
    with pytest.raises(ValueError):
        validate_verdict_payload(payload, "intents")


def _review_config() -> tuple:
    config = make_config()
    project = make_project()
    # V7 证据自复验契约：complete 引用的锚点须携带可重放命令
    from astra.server.models import Fact
    project.facts.append(Fact(id="f_anchor", description="verified result", evidence="curl -s http://t/x", confidence="high"))
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    return config, project, client, containers, driver


def test_dual_star_review_approves(monkeypatch) -> None:
    config, project, _client, containers, driver = _review_config()
    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)

    def _fake_stage(*_a, phase: str = "", **_k):
        if phase == "challenge":
            return {"accepted": True, "objections": [], "confidence": "high"}
        return {"accepted": True, "data": {"intents": [{"from": ["f001"], "description": "next"}]}}

    monkeypatch.setattr(reason, "_run_review_stage", _fake_stage)
    verdict = reason.dual_star_review(
        config, _client, containers, "container-proj_001", config.workers[0], project,
        "yaml", "intents", [{"from": ["f001"], "description": "next"}],
        TaskCancellation(),
    )
    assert verdict is True


def test_dual_star_review_challenge_rejects(monkeypatch) -> None:
    config, project, _client, containers, driver = _review_config()
    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)

    def _stage(**_kwargs):
        raise AssertionError("verdict should not run after rejection")

    calls = {"n": 0}

    def _fake_stage(*_a, **_k):
        calls["n"] += 1
        if "challenge" in _a:
            return {"accepted": False, "reason": "unsupported"}
        return None

    monkeypatch.setattr(reason, "_run_review_stage", _fake_stage)
    verdict = reason.dual_star_review(
        config, _client, containers, "container-proj_001", config.workers[0], project,
        "yaml", "complete", {"from": ["f_anchor"], "description": "done"},
        TaskCancellation(),
    )
    assert verdict is False
    assert calls["n"] == 1  # 质询否决后不再裁决


def test_dual_star_review_verdict_rejects(monkeypatch) -> None:
    config, project, _client, containers, driver = _review_config()
    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)

    def _fake_stage(*_a, phase: str = "", **_k):
        if phase == "challenge":
            return {"accepted": True, "objections": [], "confidence": "medium"}
        return {"accepted": False, "reason": "goal not met"}

    monkeypatch.setattr(reason, "_run_review_stage", _fake_stage)
    verdict = reason.dual_star_review(
        config, _client, containers, "container-proj_001", config.workers[0], project,
        "yaml", "intents", [{"from": ["f001"], "description": "next"}],
        TaskCancellation(),
    )
    assert verdict is False


def test_reason_does_not_write_when_challenged(monkeypatch) -> None:
    """集成：提案被质询否决时 reason 不写回星图。"""
    from astra.dispatcher.runtime.process import ProcessResult
    from astra.dispatcher.tasks.common import HealthcheckRun
    from conftest import FakeLease

    config, project, client, containers, driver = _review_config()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *_a, **_k: lease)
    monkeypatch.setattr(
        reason,
        "run_healthcheck",
        lambda *_a, **_k: HealthcheckRun(ProcessResult(0, "", ""), 1),
    )
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )
    monkeypatch.setattr(
        reason,
        "dual_star_review",
        lambda *_a, **_k: False,  # 质询否决
    )

    outcome = reason.run_reason_task(
        config, client, containers, project, "yaml", config.workers[0], TaskCancellation(),
    )
    assert outcome == "success"
    assert client.created_intents == []  # 未被否决时写入的航向


def test_record_failure_hint_writes_learning_hint() -> None:
    """失败学习：命令失败后写风险提示 hint，且不影响主流程。"""
    from astra.dispatcher.tasks.common import record_failure_hint

    config, project, client, _containers, _driver = _review_config()
    hints_written: list[tuple[str, str]] = []

    def _create_hint(project_id: str, content: str, creator: str = "human"):
        hints_written.append((content, creator))
        from astra.dispatcher.protocol.client import ApiResult

        return ApiResult(201, {})

    client.create_hint = _create_hint  # type: ignore[method-assign]
    record_failure_hint(client, project.project.id, "explore", "命令失败 code=1: timeout")
    assert hints_written == [("[失败学习] explore：命令失败 code=1: timeout", "astra.learning")]


def test_reason_challenged_writes_review_hint(monkeypatch) -> None:
    """审查否决后写回风险提示，供下一轮定航参考。"""
    from astra.dispatcher.runtime.process import ProcessResult
    from astra.dispatcher.tasks.common import HealthcheckRun
    from conftest import FakeLease

    config, project, client, containers, driver = _review_config()
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *_a, **_k: lease)
    monkeypatch.setattr(
        reason,
        "run_healthcheck",
        lambda *_a, **_k: HealthcheckRun(ProcessResult(0, "", ""), 1),
    )
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )
    monkeypatch.setattr(reason, "dual_star_review", lambda *_a, **_k: False)

    outcome = reason.run_reason_task(
        config, client, containers, project, "yaml", config.workers[0], TaskCancellation(),
    )
    assert outcome == "success"
    assert client.created_intents == []
    assert any("[审查否决]" in content for _, content, _ in client.created_hints)


def test_dual_star_review_machine_precheck_rejects_low_confidence(monkeypatch) -> None:
    """机器预审：complete 引用 low 置信星记 → 直接否决（不触发 LLM 审查）。"""
    from astra.server.models import Fact

    config, project, client, containers, driver = _review_config()
    project.facts.append(Fact(id="f_low", description="猜测的结论", confidence="low"))

    calls = {"n": 0}
    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(
        reason,
        "_run_review_stage_with_retry",
        lambda *_a, **_k: (calls.__setitem__("n", calls["n"] + 1) or None),
    )

    verdict = reason.dual_star_review(
        config, client, containers, "container-proj_001", config.workers[0], project,
        "yaml", "complete", {"from": ["f_low"], "description": "goal met"},
        TaskCancellation(),
    )
    assert verdict is False
    assert calls["n"] == 0  # 机器层否决，未触发 LLM 审查
    assert any("[审查否决]" in content for _, content, _ in client.created_hints)


def test_dual_star_review_rejects_flag_claim_without_evidence(monkeypatch) -> None:
    """机器预审：complete 声称 flag 但星图无 flag 星记 → 否决。"""
    config, project, client, containers, driver = _review_config()
    # 星图有普通星记但无 flag
    from astra.server.models import Fact
    project.facts.append(Fact(id="f_scan", description="端口 80 开放", evidence="nmap -p 80 t", confidence="high"))

    calls = {"n": 0}
    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(
        reason,
        "_run_review_stage_with_retry",
        lambda *_a, **_k: (calls.__setitem__("n", calls["n"] + 1) or None),
    )

    verdict = reason.dual_star_review(
        config, client, containers, "container-proj_001", config.workers[0], project,
        "yaml", "complete", {"from": ["f_scan"], "description": "flag 已获取，目标达成"},
        TaskCancellation(),
    )
    assert verdict is False
    assert calls["n"] == 0
    assert any("flag" in content for _, content, _ in client.created_hints)


def test_dual_star_review_allows_flag_claim_with_evidence(monkeypatch) -> None:
    """星图已有 flag 星记时，flag 声明放行进入 LLM 审查。"""
    from astra.server.models import Fact
    config, project, client, containers, driver = _review_config()
    project.facts.append(Fact(id="f_flag", description="获取到 flag：flag{abc123def456}", evidence="cat /flag", confidence="high"))

    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(
        reason,
        "_run_review_stage_with_retry",
        lambda *_a, phase="", **_k: {"accepted": True, "objections": [], "confidence": "high"} if phase == "challenge"
        else {"accepted": True, "data": {"complete": {"from": ["f_flag"], "description": "flag 已获取"}}},
    )
    verdict = reason.dual_star_review(
        config, client, containers, "container-proj_001", config.workers[0], project,
        "yaml", "complete", {"from": ["f_flag"], "description": "flag 已获取，目标达成"},
        TaskCancellation(),
    )
    assert verdict is True


def test_pi_driver_declares_no_review_support() -> None:
    from astra.dispatcher.workers.adapters.pi import PiDriver

    assert PiDriver().supports_review() is False


def _pi_worker() -> WorkerConfig:
    from astra.dispatcher.config import WorkerConfig

    return WorkerConfig.model_validate(
        {
            "name": "pi-worker",
            "type": "pi",
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
            },
        }
    )


def test_resolve_review_worker_uses_own_driver_when_supported() -> None:
    config = make_config()
    worker = config.workers[0]  # mock worker，supports_review=True

    resolved_worker, driver = reason._resolve_review_worker(config, worker)

    assert resolved_worker is worker
    assert driver.type_name == "mock"


def test_resolve_review_worker_falls_back_to_claudecode() -> None:
    from astra.dispatcher.config import WorkerConfig

    config = make_config()
    config.workers.append(
        WorkerConfig.model_validate(
            {
                "name": "claude-fallback",
                "type": "claudecode",
                "task_types": ["reason"],
                "max_running": 1,
                "priority": 1,
                "env": {
                    "ANTHROPIC_MODEL": "model",
                    "ANTHROPIC_BASE_URL": "http://api",
                    "ANTHROPIC_AUTH_TOKEN": "secret",
                },
            }
        )
    )

    resolved_worker, driver = reason._resolve_review_worker(config, _pi_worker())

    # V7 异构语义：pi 不支持审查 → 选异构可评审者（mock 优先级更小被选，claude 亦可用）
    assert resolved_worker.type != "pi"
    assert resolved_worker.type in ("mock", "claudecode")


def test_resolve_review_worker_without_fallback_uses_own_driver() -> None:
    # V7 异构语义：fleet 内存在异构可评审者（mock）→ 选 mock 而非能力降级的 pi
    config = make_config()  # 只有 mock worker，无 claudecode 可回退

    resolved_worker, driver = reason._resolve_review_worker(config, _pi_worker())

    assert resolved_worker.type == "mock"
    assert driver.type_name == "mock"


def test_resolve_review_worker_uses_self_when_only_same_type() -> None:
    config = make_config()
    proposer = config.workers[0]  # mock 提案者，fleet 仅同 type

    resolved_worker, _ = reason._resolve_review_worker(config, proposer)

    assert resolved_worker is proposer  # 无异构可选 → 自审（能力降级兜底）


def test_review_stage_command_is_built_by_driver(monkeypatch) -> None:
    """审查命令由 driver.build_execute 构造（不再硬编码 claude 可执行文件）。"""
    from astra.dispatcher.runtime.process import ProcessResult
    from astra.dispatcher.workers.base import DriverResult

    config, _project, _client, containers, driver = _review_config()
    executed: list[str] = []
    driver.build_execute = lambda _worker, prompt, _session: (
        executed.append(prompt) or DriverResult(["driver-cmd", prompt], session="review-session")
    )
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(0, '{"accepted": true, "objections": [], "confidence": "high"}', ""),
    )

    payload = reason._run_review_stage(
        config, containers, "container-proj_001", config.workers[0], driver,
        "challenge", {"graph_yaml": "graph", "goal": "goal", "proposal": "{}"},
        TaskCancellation(),
    )

    assert payload == {"accepted": True, "objections": [], "confidence": "high"}
    assert len(executed) == 1  # 审查 prompt 走了 driver.build_execute


def test_record_failure_hint_dedupes_identical_content() -> None:
    """相同内容的 hint 已存在时跳过写入（熔断 审查否决→hint→再定航 反馈环）。"""
    from astra.dispatcher.tasks.common import record_failure_hint

    _config, project, client, _containers, _driver = _review_config()
    project.hints.append(
        type(project.hints[0])(
            id="h002",
            content="[审查否决] review：提案被否决",
            creator="astra.learning",
            created_at="2026-01-01T00:00:05Z",
        )
    )
    from astra.dispatcher.tasks.common import REVIEW_HINT_PREFIX

    assert record_failure_hint(client, project.project.id, "review", "提案被否决", prefix=REVIEW_HINT_PREFIX) is True
    assert client.created_hints == []  # 未重复写入


def test_record_failure_hint_returns_false_on_write_failure() -> None:
    """写入失败返回 False，调用方可据此返回 failed 避免 checkpoint 静默停摆。"""
    from astra.dispatcher.protocol.client import ApiResult
    from astra.dispatcher.tasks.common import record_failure_hint

    _config, project, client, _containers, _driver = _review_config()
    client.create_hint = lambda *a, **k: ApiResult(500, None, "boom")  # type: ignore[method-assign]
    assert record_failure_hint(client, project.project.id, "explore", "命令失败") is False
