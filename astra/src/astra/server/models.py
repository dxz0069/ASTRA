from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    # step_timeout=执行租约超时，decide_timeout=决策租约超时（下限 5s，上限 1h）
    step_timeout: int = Field(ge=5, le=3600)
    decide_timeout: int = Field(ge=5, le=3600)


class Fact(BaseModel):
    id: str
    description: str
    kind: Literal["regular", "negative"] = "regular"


class Step(BaseModel):
    """FGS 的 Step：从既有事实出发、预期产出新事实的因果行动。

    生命周期：status=open 可被认领执行；Decide 可 close（附 reason，留痕防重开死路）；
    执行收束写 to_fact_id + concluded_at。
    """

    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    expect: str | None = None
    status: Literal["open", "closed"] = "open"
    close_reason: str | None = None
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    dispatch_count: int = 0  # 投入卡：被派发执行的次数（跨心跳累计），Decide 评估低产步骤用
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Finding(BaseModel):
    """FGS 的 Finding：搜索过程的沿途发现（如漏洞）——与 Goal 终点相对的产出物。"""

    id: str
    description: str
    created_at: str


class SubGoal(BaseModel):
    """FGS 的动态 Sub Goal：阶段性里程碑，Decide 可增删。"""

    id: str
    description: str
    status: Literal["active", "done", "dropped"] = "active"
    created_at: str


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectDecide(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str = Field(max_length=4096)
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    decide: ProjectDecide | None = None
    # 审计修复（租约令牌）：仅 claim 响应填充下发；其余端点恒为 None（不回显）
    decide_token: str | None = Field(default=None, max_length=128)


class ProjectSummary(ProjectMeta):
    fact_count: int
    step_count: int
    working_step_count: int
    unclaimed_step_count: int
    hint_count: int
    finding_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    steps: list[Step]
    hints: list[Hint]
    findings: list[Finding]
    subgoals: list[SubGoal]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateFactRequest(BaseModel):
    description: str
    kind: Literal["regular", "negative"] = "regular"
    creator: str = "system"

    @field_validator("description")
    @classmethod
    def validate_non_empty_description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str = Field(max_length=65536)
    goal: str = Field(max_length=65536)
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = Field(default=None, max_length=200)

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateStepRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    expect: str | None = None
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker", "expect")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        # 去重保序：LLM 输出可能带重复 id，step_sources 主键冲突会抛 500
        return list(dict.fromkeys(cleaned))


class CreateFindingRequest(BaseModel):
    description: str

    @field_validator("description")
    @classmethod
    def validate_non_empty_description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateSubGoalRequest(BaseModel):
    description: str

    @field_validator("description")
    @classmethod
    def validate_non_empty_description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateSubGoalStatusRequest(BaseModel):
    status: Literal["active", "done", "dropped"]


class CloseStepRequest(BaseModel):
    reason: str = Field(max_length=4096)

    @field_validator("reason")
    @classmethod
    def validate_non_empty_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class HeartbeatRequest(BaseModel):
    worker: str
    # 审计修复（租约令牌）：claim 下发的持有凭证；旧租约（token NULL）不强制
    lease_token: str | None = Field(default=None, max_length=128)

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class DecideClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str = Field(max_length=256)
    description: str = Field(max_length=65536)
    # 负结果："negative"=此路不通/方向已穷尽（与 regular 同等存储，Decide 侧保活）
    kind: Literal["regular", "negative"] = "regular"
    # Execute 沿途发现（可选）：与事实一并写回
    finding: str | None = Field(default=None, max_length=65536)

    @field_validator("worker", "description", "finding")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str
    lease_token: str | None = Field(default=None, max_length=128)

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        # 去重保序：LLM 输出可能带重复 id，step_sources 主键冲突会抛 500
        return list(dict.fromkeys(cleaned))


class ConcludeResponse(BaseModel):
    fact: Fact
    step: Step
    finding: Finding | None = None


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    step: Step
