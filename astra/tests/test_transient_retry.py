"""瞬时模型错误识别与退避重试的回归锁（incomplete SSE response 修复）。

背景：托管模式模型流量走平台网关（http + .tsecbench.gw），SSE 流被截断时
pi 零重试直接非零退出（pi-ai 0.73 实证），整步作废。修复 = 派发层识别传输层
瞬时错误并退避重跑（common.run_worker_process_with_retry）。
"""

from __future__ import annotations

from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.tasks import common
from astra.dispatcher.tasks.common import (
    is_transient_model_failure,
    run_worker_process_with_retry,
)


class _FakeWorker:
    """run_worker_process_with_retry 只读取 .name 做日志。"""

    name = "w-test"


# ---------- is_transient_model_failure ----------


def test_incomplete_sse_in_stderr_detected():
    assert is_transient_model_failure(ProcessResult(1, "", "Error: incomplete SSE response"))


def test_legacy_stream_ended_wording_detected():
    assert is_transient_model_failure(
        ProcessResult(1, "", "Anthropic stream ended before message_stop")
    )


def test_gateway_5xx_in_stderr_detected():
    assert is_transient_model_failure(ProcessResult(1, "", "HTTP 502 Bad Gateway"))
    assert is_transient_model_failure(ProcessResult(1, "", "529 overloaded"))


def test_error_event_in_stdout_detected():
    stdout = '{"type":"error","error":{"message":"incomplete SSE response"}}\n'
    assert is_transient_model_failure(ProcessResult(1, stdout, ""))
    stdout_str = '{"type":"error","error":"fetch failed"}\n'
    assert is_transient_model_failure(ProcessResult(1, stdout_str, ""))


def test_success_timeout_cancelled_never_transient():
    assert not is_transient_model_failure(ProcessResult(0, "", "incomplete SSE response"))
    assert not is_transient_model_failure(
        ProcessResult(124, "", "incomplete SSE response", timed_out=True)
    )
    assert not is_transient_model_failure(
        ProcessResult(1, "", "incomplete SSE response", cancelled=True, cancel_reason="x")
    )


def test_model_text_mentioning_429_not_transient():
    """模型正文/普通事件里的 429、overloaded 字样不算传输错误（防误重试）。"""
    stdout = (
        '{"type":"message","message":{"role":"assistant","content":'
        '[{"type":"text","text":"目标接口返回 429 overloaded，需要降频"}]}}\n'
    )
    assert not is_transient_model_failure(ProcessResult(1, stdout, ""))


def test_generic_failure_not_transient():
    assert not is_transient_model_failure(
        ProcessResult(2, "", "Traceback (most recent call last): SyntaxError")
    )


# ---------- run_worker_process_with_retry ----------


def _patch_run(monkeypatch, results):
    calls: list[int] = []

    def fake_run(*_args, **_kwargs):
        calls.append(1)
        return results[len(calls) - 1]

    monkeypatch.setattr(common, "run_worker_process", fake_run)
    sleeps: list[float] = []
    monkeypatch.setattr(common.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def test_retry_recovers_on_second_attempt(monkeypatch):
    calls, sleeps = _patch_run(
        monkeypatch,
        [
            ProcessResult(1, "", "incomplete SSE response"),
            ProcessResult(0, '{"type":"session","id":"s1"}', ""),
        ],
    )
    out = run_worker_process_with_retry(
        None, "c", _FakeWorker(), ["x"], phase="p", timeout_seconds=10
    )
    assert out.returncode == 0
    assert len(calls) == 2
    assert sleeps == [5.0]


def test_retry_exhausts_and_returns_last_result(monkeypatch):
    calls, sleeps = _patch_run(
        monkeypatch,
        [
            ProcessResult(1, "", "incomplete SSE response"),
            ProcessResult(1, "", "stream ended before message_stop"),
            ProcessResult(1, "", "HTTP 503"),
        ],
    )
    out = run_worker_process_with_retry(
        None, "c", _FakeWorker(), ["x"], phase="p", timeout_seconds=10
    )
    assert out.returncode == 1
    assert len(calls) == 3  # 首发 + 默认 2 次重试
    assert sleeps == [5.0, 15.0]


def test_non_transient_failure_not_retried(monkeypatch):
    calls, sleeps = _patch_run(monkeypatch, [ProcessResult(2, "", "SyntaxError")])
    out = run_worker_process_with_retry(
        None, "c", _FakeWorker(), ["x"], phase="p", timeout_seconds=10
    )
    assert out.returncode == 2
    assert len(calls) == 1
    assert sleeps == []


def test_retry_disabled_via_env(monkeypatch):
    monkeypatch.setenv("ASTRA_MODEL_RETRY_MAX", "0")
    calls, sleeps = _patch_run(
        monkeypatch, [ProcessResult(1, "", "incomplete SSE response")]
    )
    out = run_worker_process_with_retry(
        None, "c", _FakeWorker(), ["x"], phase="p", timeout_seconds=10
    )
    assert out.returncode == 1
    assert len(calls) == 1
    assert sleeps == []
