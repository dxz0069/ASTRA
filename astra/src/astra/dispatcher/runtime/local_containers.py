from __future__ import annotations

"""本地容器管理（local execution 模式）：以宿主工作目录与 subprocess 替代 Docker 容器。

接口与 docker 版 ContainerManager 对齐，供 dispatcher 无 Docker 运行
（托管平台、Windows 本机调试、离线环境）。
"""

import logging
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from astra.dispatcher.config import ContainerConfig
from astra.dispatcher.runtime.local_process import LocalProcess

LOG = logging.getLogger(__name__)


def _workspace_seed_dir() -> Path | None:
    """工作区种子目录（含 AGENTS.md / .agents，复制进每个 local workspace）。

    优先 env ASTRA_WORKSPACE_SEED；其次仓库内默认 <repo>/container
    （与 Docker 模式的 /home/kali/workspace 种子一致，保证题型模式库/协作规则生效）。
    """
    env_seed = os.environ.get("ASTRA_WORKSPACE_SEED")
    if env_seed:
        candidate = Path(env_seed)
        return candidate if candidate.is_dir() else None
    candidate = Path(__file__).resolve().parents[5] / "container"
    return candidate if (candidate / "AGENTS.md").exists() else None


def _seed_workspace(workspace: Path) -> None:
    """把 AGENTS.md 与 .agents/skills 复制进工作区（首次创建时）。

    DSH 的 agent-instructions 从工作区加载 AGENTS.md；题型模式库与靶场协作规则
    依赖此文件，缺失会导致模型缺少关键先验（local 模式曾漏复制）。
    """
    seed = _workspace_seed_dir()
    if seed is None:
        return
    try:
        agents = seed / "AGENTS.md"
        if agents.is_file():
            (workspace / "AGENTS.md").write_text(agents.read_text(encoding="utf-8"), encoding="utf-8")
        skills = seed / ".agents"
        if skills.is_dir():
            shutil.copytree(skills, workspace / ".agents", dirs_exist_ok=True)
        LOG.info("workspace seeded project_workspace=%s from=%s", workspace, seed)
    except OSError as exc:
        LOG.warning("workspace seed failed workspace=%s seed=%s error=%s", workspace, seed, exc)


class LocalContainerManager:
    """每项目一个工作目录，命令直接以宿主进程执行。"""

    _PREFIX = "astra-local-"
    _STARTUP_PREFIX = "astra-startup-local-"

    def __init__(self, config: ContainerConfig, workspace_root: Path | None = None):
        self._config = config
        self._workspace_root = (workspace_root or Path(tempfile.gettempdir()) / "astra-local").resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._workspaces: dict[str, Path] = {}
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        sanitized = project_id.replace("/", "-")
        return f"{self._PREFIX}{sanitized}"

    def ensure_running(self, project_id: str) -> str:
        name = self.container_name(project_id)
        with self._lock(name):
            if name not in self._workspaces:
                workspace = self._workspace_root / project_id.replace("/", "-")
                workspace.mkdir(parents=True, exist_ok=True)
                _seed_workspace(workspace)
                self._workspaces[name] = workspace
                LOG.info("local workspace ready project=%s workspace=%s", project_id, workspace)
            return name

    def create_startup_container(self) -> str:
        return f"{self._STARTUP_PREFIX}{uuid.uuid4().hex[:12]}"

    def inspect_state(self, name: str) -> str | None:
        return "running"

    def cleanup_completed(self, project_id: str) -> bool:
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        return True

    def cleanup_orphan(self, name: str) -> bool:
        return True

    def managed_container_names(self) -> list[str]:
        return sorted(self._workspaces)

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return False

    def needs_orphan_cleanup(self, name: str) -> bool:
        return False

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def workspace_of(self, container_name: str) -> Path:
        return self._workspaces.get(container_name, self._workspace_root)

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        return LocalProcess(
            command,
            env,
            cwd=self.workspace_of(container_name),
            timeout_seconds=timeout_seconds,
            kill_after_seconds=kill_after_seconds,
        )

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        host_path = self._to_host_path(container_name, path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content, encoding="utf-8")

    def remove_container(self, name: str, *, force: bool = True) -> None:
        with self._lock(name):
            self._workspaces.pop(name, None)

    def _to_host_path(self, container_name: str, path: str) -> Path:
        """容器内绝对路径 → 宿主路径。

        /tmp/... 映射到 Windows 的 /tmp 惯例路径 C:\\tmp（node/pi 解析 /tmp/x 为
        当前盘符的 \\tmp\\x，cwd 在 C 盘时即 C:\\tmp），保证 agent 读得到快照文件；
        其余路径映射到项目工作目录下（去掉首斜杠）。
        """
        if path.startswith("/tmp/"):
            if sys.platform == "win32":
                target = Path("C:/tmp") / path[len("/tmp/"):]
            else:
                target = Path(tempfile.gettempdir()) / path[len("/tmp/"):]
            target.parent.mkdir(parents=True, exist_ok=True)
            return target
        workspace = self.workspace_of(container_name)
        relative = path.lstrip("/")
        return workspace / relative

    def _lock(self, name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._locks[name] = lock
            return lock


def build_container_manager(config: ContainerConfig, execution: str):
    """按 execution 模式构造容器管理实现。"""
    if execution == "local":
        return LocalContainerManager(config)
    from astra.dispatcher.runtime.containers import ContainerManager

    return ContainerManager(config)
