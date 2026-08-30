"""local execution 模式测试：LocalProcess / LocalContainerManager / 调度集成。"""

from __future__ import annotations

import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 孙进程树杀回归")
def test_local_process_timeout_kills_windows_grandchild_tree() -> None:
    """审计15轮：超时杀进程必须连孙进程一起收（r7 实测 node 被杀后探针孙进程成孤儿）。

    链：LocalProcess(cmd) → cmd → ping（孙进程）。超时后树杀，ping 必须消失。
    """
    import subprocess as _sp

    marker = "-n 97"
    proc = LocalProcess(
        ["cmd", "/c", f"cmd /c ping {marker} 127.0.0.1 > NUL"],
        {},
        timeout_seconds=1,
    )
    proc.start()
    result = proc.communicate(timeout=1)
    assert result.timed_out

    def _survivors() -> list[str]:
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='ping.exe'\" | "
             "Where-Object CommandLine -like '*-n 97*' | ForEach-Object ProcessId"],
            capture_output=True, text=True, timeout=30,
        )
        return [l.strip() for l in out.stdout.splitlines() if l.strip().isdigit()]

    # 树杀后给 OS 一点收尸时间，轮询确认孙进程消失
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _survivors():
            break
        time.sleep(1)
    assert not _survivors(), "超时树杀后 ping 孙进程仍存活（Windows 进程树泄漏复发）"


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


# ---------------- 审计20轮：local 工作区清理 + 会话孤儿 ----------------

def test_local_cleanup_completed_removes_workspace(tmp_path) -> None:
    """cleanup 空壳修复：完成项目的工作区目录必须真删（旧版返回 True 但目录永留）。"""
    from astra.dispatcher.config import ContainerConfig

    root = tmp_path / "wl"
    manager = LocalContainerManager(
        ContainerConfig(image="unused", network_mode="host", completed_action="stop"),
        workspace_root=root,
    )
    pid = "proj_777"
    manager.ensure_running(pid)
    name = manager.container_name(pid)
    ws = root / pid
    assert ws.is_dir()
    (ws / "probe.py").write_text("print('x')", encoding="utf-8")

    assert manager.needs_completed_cleanup(pid) is True
    assert manager.cleanup_completed(pid) is True
    assert not ws.exists()
    assert manager.needs_completed_cleanup(pid) is False

    # dispatch 重启态（_workspaces 清空但目录在）仍需识别待清理
    manager2 = LocalContainerManager(
        ContainerConfig(image="unused", network_mode="host", completed_action="stop"),
        workspace_root=root,
    )
    manager2.ensure_running(pid)
    (root / pid / "scan.out").write_text("data", encoding="utf-8")
    manager2._workspaces.clear()
    assert manager2.needs_completed_cleanup(pid) is True
    assert manager2.cleanup_completed(pid) is True
    assert not (root / pid).exists()


def test_local_cleanup_rejects_path_traversal(tmp_path) -> None:
    """防越界：恶意 name（../ 穿越）不得删到 workspace_root 之外。"""
    from astra.dispatcher.config import ContainerConfig

    root = tmp_path / "wl"
    root.mkdir()
    outside = tmp_path / "outside-keep.txt"
    outside.write_text("keep", encoding="utf-8")
    manager = LocalContainerManager(
        ContainerConfig(image="unused", network_mode="host", completed_action="stop"),
        workspace_root=root,
    )
    # 原始恶意名直击防线（container_name 会把 / 转义成 -，此处绕过它构造真实穿越）
    assert manager.cleanup_orphan("astra-local-../../outside-keep.txt") is False
    assert outside.exists()
    assert manager.cleanup_orphan("astra-local-..\\..\\escape.txt") is False
    assert outside.exists()


def test_client_closes_stale_session_on_ident_reuse() -> None:
    """会话孤儿修复：ident 复用覆盖注册项时，旧 Session 必须被关闭。"""
    from astra.dispatcher.protocol.client import ASTRAClient

    client = ASTRAClient("http://127.0.0.1:1")
    closed: list[bool] = []

    class _FakeSession:
        def close(self) -> None:
            closed.append(True)

    import threading as _t
    ident = _t.get_ident()
    client._sessions[ident] = _FakeSession()  # 模拟已死线程的残留注册
    fresh = client._session()
    assert closed == [True]  # 旧 Session 已关
    assert client._sessions[ident] is fresh
