from __future__ import annotations

from dataclasses import dataclass, field

from astra.dispatcher.config import DispatchConfig
from astra.dispatcher.protocol.client import ApiResult
from astra.dispatcher.workers.base import DriverResult
from astra.server.models import Fact, Finding, Hint, ProjectDetail, ProjectMeta, Step, SubGoal


def make_config() -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "runtime": {
                "interval": 60,
                "max_workers": 2,
                "max_running_projects": 1,
                "max_project_workers": 2,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 10, "conclude_timeout": 5},
                "decide": {"timeout": 10, "max_steps": 3},
                "execute": {"timeout": 10, "conclude_timeout": 5},
            },
            "container": {
                "image": "test-image",
                "network_mode": "host",
                "completed_action": "stop",
            },
            "workers": [
                {
                    "name": "test-worker",
                    "type": "mock",
                    "task_types": ["bootstrap", "decide", "execute"],
                    "max_running": 1,
                    "priority": 0,
                }
            ],
        }
    )


def make_project(*, steps: list[Step] | None = None) -> ProjectDetail:
    return ProjectDetail(
        project=ProjectMeta(
            id="proj_001",
            title="test",
            status="active",
            bootstrap_enabled=True,
            created_at="2026-01-01T00:00:00Z",
        ),
        facts=[
            Fact(id="origin", description="start"),
            Fact(id="goal", description="finish"),
            Fact(id="f001", description="known fact"),
        ],
        steps=steps or [],
        hints=[
            Hint(
                id="h001",
                content="use the clue",
                creator="human",
                created_at="2026-01-01T00:00:01Z",
            )
        ],
        findings=[],
        subgoals=[],
    )


def make_step(step_id: str = "s001") -> Step:
    return Step(
        id=step_id,
        from_=["f001"],
        description="investigate",
        creator="decider",
        worker="test-worker",
        created_at="2026-01-01T00:00:02Z",
    )


def make_finding() -> Finding:
    return Finding(
        id="fnd001",
        description="a finding",
        created_at="2026-01-01T00:00:03Z",
    )


def make_subgoal() -> SubGoal:
    return SubGoal(
        id="sg001",
        description="milestone",
        status="active",
        created_at="2026-01-01T00:00:03Z",
    )


class FakeLease:
    def __init__(self) -> None:
        self.failure = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def attach_process(self, _process) -> None:
        return None


@dataclass
class FakeContainerManager:
    writes: list[tuple[str, str, str]] = field(default_factory=list)

    def ensure_running(self, project_id: str) -> str:
        return f"container-{project_id}"

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))


@dataclass
class FakeClient:
    project: ProjectDetail
    concluded: list[tuple[str, str, str, str]] = field(default_factory=list)
    completed: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    created_steps: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    created_facts: list[tuple[str, str, str, str]] = field(default_factory=list)
    created_hints: list[tuple[str, str, str]] = field(default_factory=list)
    created_findings: list[tuple[str, str]] = field(default_factory=list)
    created_subgoals: list[tuple[str, str]] = field(default_factory=list)
    closed_steps: list[tuple[str, str, str]] = field(default_factory=list)
    released: list[tuple[str, str, str]] = field(default_factory=list)
    released_decides: list[tuple[str, str]] = field(default_factory=list)

    def get_project(self, _project_id: str) -> ProjectDetail:
        return self.project

    def conclude(
        self,
        project_id: str,
        step_id: str,
        worker: str,
        description: str,
        kind: str = "regular",
        finding: str | None = None,
    ) -> ApiResult:
        self.concluded.append((project_id, step_id, worker, description))
        if finding:
            self.created_findings.append((project_id, finding))
        return ApiResult(200, {"fact": {"id": "f002", "kind": kind}})

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str, lease_token: str | None = None) -> ApiResult:
        self.completed.append((project_id, from_ids, description, worker))
        return ApiResult(200, {})

    def create_step(self, project_id: str, from_ids: list[str], description: str, creator: str, expect: str | None = None) -> ApiResult:
        self.created_steps.append((project_id, from_ids, description, creator))
        return ApiResult(201, {})

    def close_step(self, project_id: str, step_id: str, reason: str) -> ApiResult:
        self.closed_steps.append((project_id, step_id, reason))
        return ApiResult(200, {})

    def create_fact(self, project_id: str, description: str, kind: str = "regular", creator: str = "system") -> ApiResult:
        self.created_facts.append((project_id, description, kind, creator))
        return ApiResult(201, {})

    def create_finding(self, project_id: str, description: str) -> ApiResult:
        self.created_findings.append((project_id, description))
        return ApiResult(201, {})

    def create_subgoal(self, project_id: str, description: str) -> ApiResult:
        self.created_subgoals.append((project_id, description))
        return ApiResult(201, {})

    def update_subgoal_status(self, project_id: str, subgoal_id: str, status: str) -> ApiResult:
        return ApiResult(200, {})

    def create_hint(self, project_id: str, content: str, creator: str = "human") -> ApiResult:
        self.created_hints.append((project_id, content, creator))
        return ApiResult(201, {})

    def release(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        self.released.append((project_id, step_id, worker))
        return ApiResult(200, {})

    def release_decide(self, project_id: str, worker: str, lease_token: str | None = None) -> ApiResult:
        self.released_decides.append((project_id, worker))
        return ApiResult(200, {})

    def heartbeat(self, _project_id: str, _step_id: str, _worker: str) -> ApiResult:
        return ApiResult(200, {})

    def decide_heartbeat(self, _project_id: str, _worker: str, _lease_token: str | None = None) -> ApiResult:
        return ApiResult(200, {})


class FakeDriver:
    def __init__(self) -> None:
        self.execute_prompts: list[str] = []
        self.conclude_prompts: list[str] = []

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str:
        return "session-001"

    def build_healthcheck(self, _worker) -> list[str]:
        return ["healthcheck"]

    def build_execute(self, _worker, prompt: str, session: str | None) -> DriverResult:
        self.execute_prompts.append(prompt)
        return DriverResult(["execute"], session=session)

    def build_conclude(self, _worker, prompt: str, _session: str) -> list[str]:
        self.conclude_prompts.append(prompt)
        return ["conclude"]

    def extract_session(self, session: str | None, _stdout: str, _stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, _stderr: str) -> str:
        return stdout
