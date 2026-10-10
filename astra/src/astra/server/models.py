from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from astra.server.finding_identity import IDENTITY_FIELDS


def validate_finding_identity(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS):
        raise ValueError("finding identity requires all six structural fields")
    cleaned = {}
    for key in IDENTITY_FIELDS:
        item = value[key]
        if not isinstance(item, str) or not item.strip() or len(item) > 2048:
            raise ValueError(f"finding identity {key} must be nonempty and at most 2048 characters")
        cleaned[key] = item.strip()
    return cleaned


class Settings(BaseModel):
    # step_timeout=执行租约超时，decide_timeout=决策租约超时（下限 5s，上限 1h）
    step_timeout: int = Field(ge=5, le=3600)
    decide_timeout: int = Field(ge=5, le=3600)


class Fact(BaseModel):
    id: str
    description: str
    kind: Literal["regular", "negative"] = "regular"


class Step(BaseModel):
    """星图的 Step：从既有事实出发、预期产出新事实的因果行动。

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
    closed_at: str | None = None
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    dispatch_count: int = 0  # 投入卡：被派发执行的次数（跨心跳累计），Decide 评估低产步骤用
    created_at: str
    concluded_at: str | None = None
    task_type: Literal["execute", "strike"] = "execute"
    finding_id: str | None = None

    model_config = {"populate_by_name": True}


class Finding(BaseModel):
    """星图的 Finding：搜索过程的沿途发现（如漏洞）——与 Goal 终点相对的产出物。"""

    id: str
    description: str
    created_at: str
    high_value: bool = False
    verification_status: Literal["not_requested", "pending", "confirmed", "refuted", "blocked"] = "not_requested"
    source_fact_id: str | None = None
    source_step_id: str | None = None
    verification_step_id: str | None = None
    verification_fact_id: str | None = None
    verification_summary: str | None = None
    source_evidence_id: str | None = None
    verification_evidence_id: str | None = None
    # Human reproduction is a separate review process. Model-facing endpoints
    # never accept an update to this field.
    human_reproduction_status: Literal["not_started", "passed", "failed", "blocked"] = "not_started"
    identity: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def load_identity_from_row(cls, value):
        if isinstance(value, dict) and value.get("identity_json") is not None:
            return {**value, "identity": json.loads(value["identity_json"])}
        return value


class SubGoal(BaseModel):
    """星图的动态 Sub Goal：阶段性里程碑，Decide 可增删。"""

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
    content: str = Field(max_length=65536)
    creator: str = Field(max_length=256)

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateFactRequest(BaseModel):
    description: str = Field(max_length=65536)
    kind: Literal["regular", "negative"] = "regular"
    creator: str = Field(default="system", max_length=256)

    @field_validator("description")
    @classmethod
    def validate_non_empty_description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str = Field(max_length=512)  # 审计14轮：唯一漏网的请求侧字符串（origin/goal 65K 有帽）
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
    content: str = Field(max_length=65536)
    creator: str = Field(max_length=256)

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateStepRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1, max_length=500)
    description: str = Field(max_length=65536)
    expect: str | None = Field(default=None, max_length=8192)
    creator: str = Field(max_length=256)
    worker: str | None = Field(default=None, max_length=256)

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
            if len(text) > 128:
                raise ValueError("fact id too long")
            cleaned.append(text)
        # 去重保序：LLM 输出可能带重复 id，step_sources 主键冲突会抛 500
        return list(dict.fromkeys(cleaned))


class CreateFindingRequest(BaseModel):
    description: str = Field(max_length=65536)
    identity: dict[str, str] | None = None

    model_config = {"extra": "forbid"}

    @field_validator("description")
    @classmethod
    def validate_non_empty_description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("identity")
    @classmethod
    def validate_identity(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_finding_identity(value)


class CreateSubGoalRequest(BaseModel):
    description: str = Field(max_length=65536)

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
    worker: str = Field(max_length=256)
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
    worker: str = Field(max_length=256)
    trigger: str = Field(max_length=256)

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
    finding_high_value: bool = False
    finding_identity: dict[str, str] | None = None
    reuse_fact_id: str | None = Field(default=None, max_length=128)
    verification_status: Literal["confirmed", "refuted", "blocked"] | None = None
    verification_summary: str | None = Field(default=None, max_length=65536)
    # Model tool call IDs are resolved to ev_ IDs by the dispatcher. The server
    # verifies exact project/step ownership of these imported records.
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)

    model_config = {"extra": "forbid"}

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("evidence refs must be nonempty tool call IDs of at most 256 characters")
        if len(set(value)) != len(value):
            raise ValueError("evidence refs must be unique")
        return value

    @field_validator("finding_identity")
    @classmethod
    def validate_identity(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_finding_identity(value)

    @field_validator("worker", "description", "finding", "reuse_fact_id", "verification_summary")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1, max_length=500)
    description: str = Field(max_length=65536)
    worker: str = Field(max_length=256)
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


class EvidenceImportRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    url: str = Field(min_length=1, max_length=4096, pattern=r"^https?://")
    method: Literal["GET", "HEAD"]
    status: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict, max_length=128)
    body_base64: str = Field(max_length=100000)
    started_at: str = Field(min_length=1, max_length=64)
    finished_at: str = Field(min_length=1, max_length=64)
    pinned_address: str = Field(min_length=1, max_length=64)

    model_config = {"extra": "forbid"}


class EvidenceRecord(BaseModel):
    id: str
    uri: str
    sha256: str
    body_sha256: str
    project_id: str
    step_id: str
    worker: str
    session_id: str
    tool_call_id: str
    scope_sha256: str
    url: str
    method: Literal["GET", "HEAD"]
    status: int
    created_at: str


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str = Field(max_length=4096)

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str = Field(max_length=65536)
    creator: str = Field(max_length=256)

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
