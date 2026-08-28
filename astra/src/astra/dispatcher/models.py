from __future__ import annotations

from dataclasses import dataclass

from astra.dispatcher.runtime.cancellation import TaskCancellation


@dataclass(slots=True)
class RunningTask:
    project_id: str
    task_type: str
    worker_name: str
    cancellation: TaskCancellation
    intent_id: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    open_intent_count: int | None = None
    lease_token: str | None = None  # 审计修复：reason 租约持有凭证（清理释放时携带）


@dataclass(slots=True)
class ReasonCheckpoint:
    fact_count: int
    hint_count: int
    open_intent_count: int
