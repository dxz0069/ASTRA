"""local execution 模式测试：LocalProcess / LocalContainerManager / 调度集成。"""

from __future__ import annotations

import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from astra.dispatcher.config import DispatchConfig
from astra.dispatcher.runtime.local_containers import LocalContainerManager, build_container_manager
from astra.dispatcher.runtime.local_process import LocalProcess
from astra.dispatcher.scheduler.loop import DispatcherLoop
from astra.server import db
from astra.server.app import app

from test_mock_end_to_end import (
    InProcessClient,
    _config,
    _create_project,
    _dispatch_and_wait,
    _loop,
)
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def http_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as client:
        yield client


def test_local_process_runs_and_captures_output() -> None:
    proc = LocalProcess(
        [sys.executable, "-c", "import sys; print('hello'); print('err', file=sys.stderr)"],
        {},
    )
    proc.start()
    result = proc.communicate(timeout=10)
    assert result.returncode == 0
    assert "hello" in result.stdout
    assert "err" in result.stderr
    assert not result.timed_out


def test_local_process_timeout_kills() -> None:
    proc = LocalProcess(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        {},
    )
    proc.start()
    result = proc.communicate(timeout=1)
    assert result.timed_out
    assert result.returncode != 0


def test_local_container_manager_workspace_and_paths() -> None:
    from astra.dispatcher.config import ContainerConfig

    root = Path(tempfile.mkdtemp(prefix="astra-local-test-"))
    config = ContainerConfig(image="unused", network_mode="host", completed_action="stop")
    manager = LocalContainerManager(config, workspace_root=root)

    name = manager.ensure_running("proj_abc")
    assert name.startswith("astra-local-")
    workspace = manager.workspace_of(name)
    assert workspace == root.resolve() / "proj_abc"

    # /tmp/... 映射：Windows 走 C:\tmp 惯例（node/pi 按当前盘符解析 /tmp），posix 走系统 tempdir
    manager.write_text_file(name, "/tmp/astra-prompts/phase-1/graph.yaml", "graph: yaml")
    import sys as _sys
    if _sys.platform == "win32":
        mapped = Path("C:/tmp") / "astra-prompts" / "phase-1" / "graph.yaml"
    else:
        mapped = Path(tempfile.gettempdir()) / "astra-prompts" / "phase-1" / "graph.yaml"
    assert mapped.read_text(encoding="utf-8") == "graph: yaml"
    manager.close()


def test_local_workspace_seeded_with_agents_and_skills(monkeypatch, tmp_path) -> None:
    """local workspace 创建时复制 AGENTS.md 与 .agents/skills（题型模式库/协作规则生效）。"""
    from astra.dispatcher.config import ContainerConfig
    from astra.dispatcher.runtime.local_containers import LocalContainerManager

    seed = tmp_path / "seed"
    (seed / ".agents" / "skills" / "demo").mkdir(parents=True)
    (seed / "AGENTS.md").write_text("# 靶场协作\n拿到 flag 写回星图", encoding="utf-8")
    (seed / ".agents" / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nskill", encoding="utf-8")
    monkeypatch.setenv("ASTRA_WORKSPACE_SEED", str(seed))

    root = tmp_path / "workspaces"
    config = ContainerConfig(image="unused", network_mode="host", completed_action="stop")
    manager = LocalContainerManager(config, workspace_root=root)
    name = manager.ensure_running("proj_seed")
    workspace = manager.workspace_of(name)

    assert (workspace / "AGENTS.md").exists()
    assert "写回星图" in (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert (workspace / ".agents" / "skills" / "demo" / "SKILL.md").exists()
    manager.close()


def test_build_container_manager_selects_local() -> None:
    from astra.dispatcher.config import ContainerConfig

    config = ContainerConfig(image="unused", network_mode="host", completed_action="stop")
    manager = build_container_manager(config, "local")
    assert isinstance(manager, LocalContainerManager)
    manager.close()


def test_local_execution_end_to_end(http_client: TestClient) -> None:
    """local execution + mock worker 跑完整调度链路（bootstrap→complete）。"""
    config = _config(
        bootstrap='{"delay":[0,0],"outcomes":{"complete":"1.0","fact":"0.0","rejected":"0.0","invalid_json":"0.0","invalid_payload":"0.0","command_fail":"0.0"}}',
        decide='{"delay":[0,0],"outcomes":{"complete":"1.0","ops":"0.0","noop":"0.0","rejected":"0.0","invalid_json":"0.0","invalid_payload":"0.0","command_fail":"0.0"}}',
        execute='{"delay":[0,0],"outcomes":{"fact":"1.0","rejected":"0.0","invalid_json":"0.0","invalid_payload":"0.0","command_fail":"0.0"}}',
    )
    # 注入 local execution（mock 端到端 harness 的 config 无 execution 字段，默认 docker；这里显式 local）
    config.runtime.execution = "local"

    client = InProcessClient(http_client)
    containers = LocalContainerManager(config.container)

    loop = _loop(config, client, containers)
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
