from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import threading
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from astra.dispatcher.config import DispatchConfig
from astra.dispatcher.models import DecideCheckpoint
from astra.dispatcher.protocol.client import ApiResult
from astra.dispatcher.runtime.process import ProcessResult
from astra.dispatcher.scheduler.loop import DispatcherLoop
from astra.server import db
from astra.server.app import app
from astra.server.models import ProjectDetail, ProjectSummary, Settings


class InProcessClient:
    def __init__(self, http: TestClient):
        self.http = http
        self._summaries = TypeAdapter(list[ProjectSummary])
        # 实例级令牌记账（类属性会被跨测试共享旧令牌 → 心跳 403 的隔离坑）
        self._lease_tokens: dict[str, str | None] = {}

    def close(self) -> None:
        return None

    def list_projects(self) -> list[ProjectSummary]:
        response = self.http.get("/projects")
        response.raise_for_status()
        return self._summaries.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self.http.get(f"/projects/{project_id}")
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self.http.get("/settings")
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self.http.get(f"/projects/{project_id}/export?format=yaml")
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/steps/{step_id}/heartbeat", {"worker": worker})

    def claim_decide(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        result = self._post(f"/projects/{project_id}/decide/claim", {"worker": worker, "trigger": trigger})
        if result.ok and isinstance(result.data, dict):
            # 真实 server 会下发租约令牌；桩侧记账，后续心跳/释放/完成携带
            self._lease_tokens[project_id] = result.data.get("decide_token")
        return result

    def _token_of(self, project_id: str, explicit: str | None) -> str | None:
        return explicit if explicit is not None else self._lease_tokens.get(project_id)

    def decide_heartbeat(self, project_id: str, worker: str, lease_token: str | None = None) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/decide/heartbeat",
            {"worker": worker, "lease_token": self._token_of(project_id, lease_token)},
        )

    def release_decide(self, project_id: str, worker: str, lease_token: str | None = None) -> ApiResult:
        token = self._token_of(project_id, lease_token)
        result = self._post(
            f"/projects/{project_id}/decide/release",
            {"worker": worker, "lease_token": token},
        )
        if result.ok:
            self._lease_tokens.pop(project_id, None)
        return result

    def release(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/steps/{step_id}/release", {"worker": worker})

    def conclude(
        self,
        project_id: str,
        step_id: str,
        worker: str,
        description: str,
        kind: str = "regular",
        finding: str | None = None,
    ) -> ApiResult:
        body: dict[str, Any] = {"worker": worker, "description": description}
        if kind != "regular":
            body["kind"] = kind
        if finding:
            body["finding"] = finding
        return self._post(
            f"/projects/{project_id}/steps/{step_id}/conclude",
            body,
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str, lease_token: str | None = None) -> ApiResult:
        result = self._post(
            f"/projects/{project_id}/complete",
            {
                "from": from_ids,
                "description": description,
                "worker": worker,
                "lease_token": self._token_of(project_id, lease_token),
            },
        )
        if result.ok:
            self._lease_tokens.pop(project_id, None)
        return result

    def create_step(
        self, project_id: str, from_ids: list[str], description: str, creator: str, expect: str | None = None
    ) -> ApiResult:
        body: dict[str, Any] = {"from": from_ids, "description": description, "creator": creator, "worker": None}
        if expect:
            body["expect"] = expect
        return self._post(f"/projects/{project_id}/steps", body)

    def create_hint(self, project_id: str, content: str, creator: str = "human") -> ApiResult:
        return self._post(f"/projects/{project_id}/hints", {"content": content, "creator": creator})

    def create_fact(self, project_id: str, description: str, kind: str = "regular", creator: str = "system") -> ApiResult:
        return self._post(f"/projects/{project_id}/facts", {"description": description, "kind": kind, "creator": creator})

    def _post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        response = self.http.post(path, json=payload)
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        return ApiResult(response.status_code, data, response.text)


class LocalProcess:
    def __init__(self, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self._process: subprocess.Popen[str] | None = None
        self._cancel_reason: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._process = subprocess.Popen(
                self.command,
                env={**os.environ, **self.env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        timed_out = False
        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill()
            stdout, stderr = self._process.communicate()
        return ProcessResult(
            returncode=self._process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()


class LocalContainerManager:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return f"local-{project_id}"

    def ensure_running(self, project_id: str) -> str:
        return self.container_name(project_id)

    def build_exec_process(
        self,
        _container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        assert timeout_seconds is not None
        assert kill_after_seconds == 5
        return LocalProcess(command, env)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))

    def needs_completed_cleanup(self, _project_id: str) -> bool:
        return False

    def needs_stopped_cleanup(self, _project_id: str) -> bool:
        return False

    def managed_container_names(self) -> list[str]:
        return []


@pytest.fixture
def http_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as client:
        yield client


def _phase(
    outcome: str,
    *,
    rules: list[dict[str, Any]] | None = None,
    zero_outcomes: list[str] | None = None,
) -> str:
    outcomes = {name: 0 for name in zero_outcomes or []}
    outcomes[outcome] = 1
    payload: dict[str, Any] = {"delay": [0, 0], "outcomes": outcomes}
    if rules is not None:
        payload["rules"] = rules
    return json.dumps(payload)


def _config(
    *,
    bootstrap: str,
    decide: str,
    execute: str,
    task_types: list[str] | None = None,
) -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 2, "conclude_timeout": 2},
                "decide": {"timeout": 2, "max_steps": 1},
                "execute": {"timeout": 2, "conclude_timeout": 2},
            },
            "container": {
                "image": "unused",
                "network_mode": "host",
                "completed_action": "stop",
            },
            "workers": [
                {
                    "name": "mock-worker",
                    "type": "mock",
                    "task_types": task_types or ["bootstrap", "decide", "execute"],
                    "max_running": 1,
                    "priority": 0,
                    "env": {
                        "MOCK_HEALTHCHECK": _phase("ok"),
                        "MOCK_BOOTSTRAP": bootstrap,
                        "MOCK_DECIDE": decide,
                        "MOCK_EXECUTE_EXECUTE": execute,
                    },
                }
            ],
        }
    )


def _loop(config: DispatchConfig, client: InProcessClient, containers: LocalContainerManager) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = config
    loop.client = client
    loop.container_manager = containers
    loop.executor = ThreadPoolExecutor(max_workers=config.runtime.max_workers)
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop.futures = {}
    loop.cleanup_futures = {}
    loop.decide_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop.project_cursor = 0
    return loop


def _dispatch_and_wait(loop: DispatcherLoop) -> None:
    loop._reap_futures()
    summaries = loop.client.list_projects()
    loop._initialize_decide_checkpoints(summaries)
    loop._refresh_runtime_projects(summaries)
    loop._cancel_inactive_tasks(summaries)
    loop._queue_container_cleanups(summaries)
    loop._dispatch_available(summaries)
    assert loop.futures
    for future in list(loop.futures):
        future.result(timeout=5)
    loop._reap_futures()


def _create_project(http: TestClient) -> str:
    response = http.post(
        "/projects",
        json={"title": "integration", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_mock_scheduler_bootstrap_completes_project_end_to_end(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            decide=_phase("complete", zero_outcomes=["ops"]),
            execute=_phase("fact"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001"]
    assert [(step.id, step.to) for step in project.steps] == [("s001", "f001"), ("s002", "goal")]


def test_mock_scheduler_runs_decide_execute_decide_complete_chain(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            decide=_phase("ops", rules=[{"fact_ids_gte": 3, "force": "complete"}]),
            execute=_phase("fact"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)
    seed = client.create_step(project_id, ["origin"], "seed", "seed-worker")
    assert seed.ok
    assert client.heartbeat(project_id, "s001", "seed-worker").ok
    assert client.conclude(project_id, "s001", "seed-worker", "seed fact").ok

    try:
        _dispatch_and_wait(loop)
        assert loop.decide_checkpoints[project_id] == DecideCheckpoint(3, 0, 0)
        _dispatch_and_wait(loop)
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001", "f002"]
    assert [(step.id, step.to) for step in project.steps] == [
        ("s001", "f001"),
        ("s002", "f002"),
        ("s003", "goal"),
    ]
    assert any("/decide_execute-" in path and "f002" in content for _, path, content in containers.writes)
    assert any("/execute_execute-" in path and "f001" in content for _, path, content in containers.writes)


def test_mock_scheduler_enabled_project_skips_bootstrap_when_worker_does_not_support_it(
    http_client: TestClient,
) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            decide=_phase("complete", zero_outcomes=["ops"]),
            execute=_phase("fact"),
            task_types=["decide", "execute"],
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)
    # complete 不接受 from_=["origin"]（系统事实=劫持原语）：
    # 先种一条真实事实作为完成依据
    seed = client.create_step(project_id, ["origin"], "seed", "seed-worker")
    assert seed.ok
    assert client.heartbeat(project_id, "s001", "seed-worker").ok
    assert client.conclude(project_id, "s001", "seed-worker", "seed fact").ok

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [(step.description, step.to) for step in project.steps] == [
        ("seed", "f001"),
        ("mock complete from f001", "goal"),
    ]
