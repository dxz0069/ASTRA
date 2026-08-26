from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from astra.dispatcher.protocol.client import ApiResult, ASTRAClient
from astra.dispatcher.runtime.process import ManagedProcess


LOG = logging.getLogger(__name__)
HEARTBEAT_FAILURE_GRACE_MULTIPLIER = 2


@dataclass(slots=True)
class HeartbeatFailure:
    status_code: int | None
    text: str


class HeartbeatLease:
    def __init__(
        self,
        heartbeat: Callable[[], ApiResult],
        scope: str,
        worker_name: str,
        interval: int,
        client: ASTRAClient | None = None,
    ):
        self._heartbeat = heartbeat
        self._scope = scope
        self._worker_name = worker_name
        self._interval = interval
        # P1-2：持有 client 引用——心跳线程退出时注销本线程 Session，
        # 防止 ASTRAClient._sessions 随每任务新建线程只增不减（Session 泄漏）
        self._client = client
        self._process: ManagedProcess | None = None
        self._failure: HeartbeatFailure | None = None
        self._last_success_at = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @classmethod
    def for_intent(
        cls,
        client: ASTRAClient,
        project_id: str,
        intent_id: str,
        worker_name: str,
        interval: int,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.heartbeat(project_id, intent_id, worker_name),
            scope=f"project={project_id} intent={intent_id}",
            worker_name=worker_name,
            interval=interval,
            client=client,
        )

    @classmethod
    def for_reason(
        cls,
        client: ASTRAClient,
        project_id: str,
        worker_name: str,
        interval: int,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.reason_heartbeat(project_id, worker_name),
            scope=f"project={project_id} reason",
            worker_name=worker_name,
            interval=interval,
            client=client,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def attach_process(self, process: ManagedProcess | None) -> None:
        with self._lock:
            self._process = process

    @property
    def failure(self) -> HeartbeatFailure | None:
        return self._failure

    def _run(self) -> None:
        try:
            while not self._stop.wait(self._interval):
                try:
                    result = self._heartbeat()
                except Exception as exc:  # noqa: BLE001 —— D3：任何异常（含 JSON 解析）
                    # 不让心跳线程静默死亡——死亡后租约无人续期→同 intent 被重派双跑
                    LOG.warning(
                        "heartbeat call raised scope=%s worker=%s error=%s（按瞬时失败处理）",
                        self._scope, self._worker_name, exc,
                    )
                    result = ApiResult(status_code=0, text=str(exc))
                if result.ok:
                    self._last_success_at = time.monotonic()
                    continue
                if result.status_code in (403, 409):
                    self._fail(result.status_code, result.text)
                    return
                elapsed = time.monotonic() - self._last_success_at
                grace_seconds = max(float(self._interval), float(self._interval * HEARTBEAT_FAILURE_GRACE_MULTIPLIER))
                LOG.warning(
                    "heartbeat transient failure scope=%s worker=%s status=%s elapsed=%.1fs grace=%.1fs",
                    self._scope,
                    self._worker_name,
                    result.status_code,
                    elapsed,
                    grace_seconds,
                )
                if elapsed < grace_seconds:
                    continue
                self._fail(result.status_code or None, result.text)
                return
        finally:
            # P1-2：心跳线程退出（失败返回或被 stop）时注销本线程在 ASTRAClient
            # 中的 Session——_sessions 只增不减会让长跑进程无限膨胀
            if self._client is not None:
                try:
                    self._client._remove_session()
                except Exception:  # noqa: BLE001 —— 清理失败不影响心跳线程退出
                    LOG.debug("heartbeat session cleanup failed scope=%s", self._scope)

    def _fail(self, status_code: int | None, text: str) -> None:
        self._failure = HeartbeatFailure(status_code, text)
        LOG.warning(
            "heartbeat failed scope=%s worker=%s status=%s",
            self._scope,
            self._worker_name,
            status_code,
        )
        with self._lock:
            process = self._process
        if process is not None:
            process.kill()
