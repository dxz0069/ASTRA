"""LocalAstraEngine —— 本地模式 ASTRA 引擎封装（server + dispatcher 进程管理 + API）。

- server/dispatcher 为进程级共享单例（幂等启动，最后 shutdown）
- dispatch.yaml 由环境变量动态生成：execution=local，DeepSeek 主力（claudecode +
  ANTHROPIC 兼容端点），reason 任务与双星审查复用同一 worker 配置
- 每个题目一个 ASTRA 项目，origin=靶场地址，goal=题目描述
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests

LOG = logging.getLogger("astra-runner.engine")

ASTRA_SERVER_URL = os.environ.get("ASTRA_SERVER_URL", "http://127.0.0.1:8000")
ASTRA_SERVER_CMD = os.environ.get("ASTRA_SERVER_CMD", "astra serve")
ASTRA_DISPATCH_CMD = os.environ.get("ASTRA_DISPATCH_CMD", "astra dispatch")

PROJECT_COMPLETED_STATUSES = {"completed", "stopped"}


def _claude_home_root() -> str:
    """claudecode worker 的 CLAUDE_CONFIG_DIR 根目录（ASTRA_CLAUDE_HOME 可覆盖）。
    按 worker 稳定命名（不加 uuid）：引擎崩溃重启后 `claude -r <session>` 仍能
    找回会话，defer 续跑跨重启存活（R5 会话丢失税教训）；同时不读宿主 ~/.claude。"""
    return os.environ.get("ASTRA_CLAUDE_HOME") or str(Path(tempfile.gettempdir()) / "astra-claude")


def _claude_home(worker_name: str) -> Path:
    return Path(_claude_home_root()) / worker_name


def _cleanup_stale_engine_files(keep_dbs: int = 5) -> None:
    """文件系统审计：清理旧引擎 db 文件与含密钥的 dispatch yaml。

    每次引擎重启产生新 uuid db（含 WAL/SHM），自愈重启频繁时 temp 目录膨胀。
    保留最近 keep_dbs 个（正在使用的排最近），超过的删除。
    dispatch yaml 含 API key 明文，进程退出后必须删除。
    """
    temp_dir = Path(tempfile.gettempdir())
    # 旧 db 文件（按 mtime 排序，保留最新几个）
    db_files = sorted(temp_dir.glob("astra-runner-*.db*"), key=lambda p: p.stat().st_mtime)
    for old in db_files[:-keep_dbs * 3]:  # ×3 因为每个 db 可能带 .db-wal/.db-shm
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
    # 含密钥的 dispatch yaml（超过 1 小时的）
    import time as _time
    cutoff = _time.time() - 3600
    for yaml_file in temp_dir.glob("astra-dispatch-*.yaml"):
        try:
            if yaml_file.stat().st_mtime < cutoff:
                yaml_file.unlink(missing_ok=True)
        except OSError:
            pass


class AstraDaemon:
    """server + dispatcher 进程单例。"""

    _instance: "AstraDaemon | None" = None
    _lock = threading.Lock()
    _start_lock = threading.Lock()

    def __init__(self) -> None:
        self._server: subprocess.Popen | None = None
        self._dispatcher: subprocess.Popen | None = None
        self._dispatch_config: Path | None = None

    @classmethod
    def instance(cls) -> "AstraDaemon":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def shutdown(self) -> None:
        """优雅关闭引擎子进程（execv 自愈重启前必须调用，否则旧进程占端口+烧 token）。

        P0 修复：os.execv 原位替换不清子进程，旧 server 持 8000 端口导致新进程
        exited early → 整轮报废。此方法在 execv 前被调用，确保干净换血。
        """
        import signal as _signal

        for name, proc in [("dispatcher", self._dispatcher), ("server", self._server)]:
            if proc is not None and proc.poll() is None:
                try:
                    # POSIX 下杀整组（claude 的 bun→nmap/curl 孙进程）
                    if sys.platform != "win32":
                        try:
                            os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            proc.terminate()
                    else:
                        proc.terminate()
                    proc.wait(timeout=10)
                    LOG.info("daemon %s stopped (pid=%s)", name, proc.pid)
                except subprocess.TimeoutExpired:
                    LOG.warning("daemon %s SIGTERM timeout, SIGKILL", name)
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("daemon %s stop failed: %s", name, exc)
        self._server = None
        self._dispatcher = None
        # 等 8000 端口真正释放（新进程才能绑定）
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                requests.get(f"{ASTRA_SERVER_URL}/projects", headers=_auth_headers(),  timeout=1)
                time.sleep(0.5)  # 还活着，继续等
            except Exception:  # noqa: BLE001
                break  # 连不上 = 端口已释放
        # 文件系统审计：清理本次引擎的 dispatch yaml（含 API key）与旧 db 文件
        if self._dispatch_config is not None:
            try:
                self._dispatch_config.unlink(missing_ok=True)
            except OSError:
                pass
            self._dispatch_config = None
        _cleanup_stale_engine_files()

    def ensure_started(self) -> None:
        if os.environ.get("ASTRA_EXTERNAL_ENGINE") == "1":
            # 外部引擎模式：server/dispatcher 由外部管理（避免进程组级联退出）
            LOG.info("using external astra engine at %s", ASTRA_SERVER_URL)
            return
        if self._server is not None and self._server.poll() is None:
            return
        # 并行题目冷启动竞态：多线程同时看到 _server is None 会各自起 server 抢端口，
        # 后到者 exited early 导致该题静默失败——启动全程持锁（double-checked）
        with self._start_lock:
            if self._server is not None and self._server.poll() is None:
                return
            self._start_locked()

    def _start_locked(self) -> None:
        _cleanup_stale_engine_files()
        self._cleanup_claude_homes()
        db_path = Path(tempfile.gettempdir()) / f"astra-runner-{uuid.uuid4().hex[:8]}.db"
        LOG.info("starting astra server db=%s", db_path)
        self._server = _popen([*ASTRA_SERVER_CMD.split(), "--db-path", str(db_path), "--no-access-log"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                raise RuntimeError(f"astra server exited early: {self._server.returncode}")
            try:
                requests.get(f"{ASTRA_SERVER_URL}/projects", headers=_auth_headers(),  timeout=2).raise_for_status()
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        else:
            raise RuntimeError("astra server did not become ready in 60s")
        self._ensure_dispatcher()

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is not None and self._dispatcher.poll() is None:
            return
        config = self._render_dispatch_config()
        LOG.info("starting astra dispatcher config=%s", config)
        self._dispatcher = _popen([*ASTRA_DISPATCH_CMD.split(), "--config", str(config)])

    def _render_dispatch_config(self) -> Path:
        """由环境变量生成 dispatch.yaml（local 执行）。

        ASTRA_WORKER_TYPE=claudecode（默认，2026-08-28 起）：claude CLI +
        DeepSeek Anthropic 兼容端点（ANTHROPIC_*）。tsecbench 前十实测主流栈
        （6/9 家 Claude Code/Agent SDK，0 家 dsh）——见
        docs/Tsecbench前十名日志机制拆解.md §6。dsh 路线已整体移除。
        """
        worker_type = os.environ.get("ASTRA_WORKER_TYPE", "claudecode")
        if worker_type != "claudecode":
            raise RuntimeError(
                f"不支持的 ASTRA_WORKER_TYPE: {worker_type}（dsh 已于 2026-08-28 移除，仅 claudecode）"
            )
        common_env = {
            "BENCHMARK_TOKEN": os.environ.get("BENCHMARK_TOKEN", ""),
            "BENCHMARK_BASE_URL": os.environ.get("BENCHMARK_BASE_URL", ""),
        }
        common_env = {k: v for k, v in common_env.items() if v}
        worker_block = self._render_claudecode_fleet()
        yaml = f"""server: "{ASTRA_SERVER_URL}"
runtime:
  interval: 3
  max_workers: 8
  max_running_projects: 3
  max_project_workers: 8
  healthcheck_timeout: 20
  worker_healthcheck: "startup_only"
  prompt_group: "default"
  execution: "local"
  context_budget:
    max_inline_facts: 60
    max_inline_intents: 12
    max_inline_hints: 8
tasks:
  bootstrap:
    timeout: 600
    conclude_timeout: 120
  reason:
    timeout: 420
    max_intents: 2
  explore:
    timeout: 600
    conclude_timeout: 120
  consolidate:
    timeout: 240
  challenge:
    timeout: 600
container:
  image: "unused"
  network_mode: "host"
  completed_action: "stop"
common_env:
{_dump_env(common_env)}
workers:
{worker_block}
"""
        path = Path(tempfile.gettempdir()) / f"astra-dispatch-{uuid.uuid4().hex[:8]}.yaml"
        path.write_text(yaml, encoding="utf-8")
        # 含模型 API key，限制仅属主可读（共享主机 /tmp 默认 umask 可能放开）
        os.chmod(path, 0o600)
        self._dispatch_config = path
        return path

    def _render_claudecode_fleet(self) -> str:
        """claudecode 舰队（2026-08-28，tsecbench 前十主流栈，0 家 dsh）：
          - deepseek-explore-{i}  p0 bootstrap/explore ×N（ASTRA_EXPLORE_REPLICAS，
            默认 2）每副本 max_running=3（ASTRA_EXPLORE_MAXRUN）→ 峰值 6 路并发探索
          - deepseek-reason       p1 reason/consolidate ×1，max_running=2
        纯 deepseek-v4-flash 单模型（虫洞#3/ATX#5/应龙#9 同款）；
        GLM 双通道随 dsh 一并退役（coding 端点仅 chat-completions，CC 需 anthropic
        协议，托管网关未验证该路径）。
        """
        model = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if not auth_token:
            raise RuntimeError("缺少 ANTHROPIC_AUTH_TOKEN（DeepSeek anthropic 兼容端点密钥）")
        explore_replicas = max(1, int(os.environ.get("ASTRA_EXPLORE_REPLICAS", "2")))
        explore_maxrun = max(1, int(os.environ.get("ASTRA_EXPLORE_MAXRUN", "3")))
        fleet: list[str] = []
        for i in range(explore_replicas):
            name = "deepseek-explore" if explore_replicas == 1 else f"deepseek-explore-{i}"
            fleet.append(
                self._render_claudecode_worker(
                    name, ["bootstrap", "explore"],
                    max_running=explore_maxrun, priority=0,
                    model=model, base_url=base_url, auth_token=auth_token,
                )
            )
        fleet.append(
            self._render_claudecode_worker(
                "deepseek-reason", ["reason", "consolidate"],
                max_running=2, priority=1,
                model=model, base_url=base_url, auth_token=auth_token,
            )
        )
        return "\n".join(fleet)

    def _render_claudecode_worker(
        self,
        worker_name: str,
        task_types: list[str],
        *,
        max_running: int,
        priority: int,
        model: str,
        base_url: str,
        auth_token: str,
    ) -> str:
        """单个 claudecode worker 的 YAML 块。

        ANTHROPIC_SMALL_FAST_MODEL / CLAUDE_CODE_SUBAGENT_MODEL 钉到主模型：
        CC 的标题生成/子代理调用不会尝试不存在的 haiku 端点（anthropic 兼容端点
        只有主模型），子代理可用（RoundTable#8 的 CC subagent 打法）。
        """
        claude_dir = _claude_home(worker_name)
        claude_dir.mkdir(parents=True, exist_ok=True)
        self._write_claude_mcp_config(claude_dir)
        claude_dir_yaml = str(claude_dir).replace("\\", "/")  # YAML 双引号内反斜杠是转义符
        types_line = ", ".join(task_types)
        return f"""  - name: "{worker_name}"
    type: "claudecode"
    task_types: [{types_line}]
    max_running: {max_running}
    priority: {priority}
    env:
      ANTHROPIC_MODEL: "{model}"
      ANTHROPIC_BASE_URL: "{base_url}"
      ANTHROPIC_AUTH_TOKEN: "{auth_token}"
      ANTHROPIC_API_KEY: "{auth_token}"
      ANTHROPIC_SMALL_FAST_MODEL: "{model}"
      CLAUDE_CODE_SUBAGENT_MODEL: "{model}"
      CLAUDE_CONFIG_DIR: "{claude_dir_yaml}"
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"
      CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT: "1"
"""

    @staticmethod
    def _write_claude_mcp_config(claude_dir: Path) -> None:
        """写 .claude.json 注册 playwright MCP（镜像内全局安装的
        playwright-mcp；Web 类题目真实浏览器，取代 dsh 的 mcp-client 接入）。"""
        config = {
            "mcpServers": {
                "playwright": {
                    "command": "playwright-mcp",
                    "env": {"PLAYWRIGHT_MCP_BROWSER": "chromium"},
                }
            }
        }
        (claude_dir / ".claude.json").write_text(json.dumps(config), encoding="utf-8")

    @staticmethod
    def _cleanup_claude_homes(max_age_hours: float = 72.0) -> None:
        """启动时清理陈旧的 claude 配置目录（CC 会话 jsonl 不自动删除，长跑累积）。

        只在**新一轮引擎启动**时执行；worker 目录按名稳定复用，conclude/defer
        续跑依赖的近期会话（mtime 新于 max_age_hours）绝不清理——与 dsh 时代
        的 R5 会话丢失教训同一纪律。
        """
        root = Path(_claude_home_root())
        if not root.is_dir():
            return
        cutoff = time.time() - max_age_hours * 3600
        removed = 0
        for d in root.iterdir():
            if not d.is_dir():
                continue
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        if removed:
            LOG.info("cleaned stale claude homes root=%s removed=%s", root, removed)

    # 注意：shutdown 只定义一次（类头部的 P0 加固版：killpg+端口等待+含密钥
    # dispatch yaml 清理）。这里曾残留一个同名的简版 shutdown 把加固版覆盖成
    # 死代码（Python 类体后定义覆盖前定义），2026-08-28 审计修复删除。


_ENGINE_LOG_PREFIX = "astra-engine-"
_ENGINE_LOG_KEEP = 10


def _engine_log_path() -> Path:
    """按时间戳分文件 + 启动时清理旧日志（避免长跑膨胀）。"""
    temp_dir = Path(tempfile.gettempdir())
    for old in sorted(temp_dir.glob(f"{_ENGINE_LOG_PREFIX}*.log"))[:-_ENGINE_LOG_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return temp_dir / f"{_ENGINE_LOG_PREFIX}{stamp}.log"


def _auth_headers() -> dict[str, str]:
    """审计修复：服务端启用 ASTRA_AUTH_TOKEN 时引擎直连请求带 Bearer 头。"""
    token = os.environ.get("ASTRA_AUTH_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _popen(argv: list[str]) -> subprocess.Popen:
    log_file = open(_engine_log_path(), "ab", buffering=0)
    return subprocess.Popen(
        argv,
        stdout=log_file,
        stderr=log_file,
    )


def _dump_env(env: dict[str, str]) -> str:
    if not env:
        return "  {}"
    return "\n".join(f"  {k}: \"{v}\"" for k, v in env.items())


class LocalAstraEngine:
    """run_benchmark 的 AstraEngine 实现（共享 daemon 单例）。"""

    def start(self) -> None:
        AstraDaemon.instance().ensure_started()

    def create_project(self, title: str, origin: str, goal: str) -> str:
        response = requests.post(
            f"{ASTRA_SERVER_URL}/projects",
            headers=_auth_headers(),
            json={
                "title": title,
                "origin": origin,
                "goal": goal,
                "bootstrap_enabled": True,
                "hints": [],
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["project"]["id"]

    def create_hint(self, project_id: str, content: str) -> None:
        """注入指引（hint）：供 runner 把平台 hint 注入 ASTRA 项目，星探下次读取吸收。"""
        response = requests.post(
            f"{ASTRA_SERVER_URL}/projects/{project_id}/hints",
            headers=_auth_headers(),
            json={"content": content, "creator": "astra.runner"},
            timeout=15,
        )
        if response.status_code >= 400:
            LOG.warning("create_hint failed project=%s status=%s body=%s", project_id, response.status_code, response.text[:200])

    def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = requests.get(f"{ASTRA_SERVER_URL}/projects/{project_id}", headers=_auth_headers(),  timeout=10)
                response.raise_for_status()
                status = response.json()["project"]["status"]
                if status in PROJECT_COMPLETED_STATUSES:
                    return True
            except requests.RequestException:
                time.sleep(2)
                continue
            time.sleep(3)
        return False

    def list_fact_descriptions(self, project_id: str) -> list[str]:
        response = requests.get(f"{ASTRA_SERVER_URL}/projects/{project_id}", headers=_auth_headers(),  timeout=10)
        response.raise_for_status()
        return [fact["description"] for fact in response.json().get("facts", [])]

    def delete_project(self, project_id: str) -> None:
        try:
            requests.delete(f"{ASTRA_SERVER_URL}/projects/{project_id}", headers=_auth_headers(),  timeout=10)
        except requests.RequestException:
            pass

    def stop_project(self, project_id: str) -> None:
        """defer 时停项目：服务端清 worker 租约与 reason（scheduler 停止派发并取消在途
        任务），星图数据保留——修复 R5 实测的僵尸项目饿死新题问题。"""
        try:
            response = requests.put(
                f"{ASTRA_SERVER_URL}/projects/{project_id}/status",
                json={"status": "stopped"},
                headers=_auth_headers(),
                timeout=15,
            )
            response.raise_for_status()
            LOG.info("project stopped (defer) project=%s", project_id)
        except requests.RequestException as exc:
            LOG.warning("stop_project failed project=%s error=%s", project_id, exc)

    def reactivate_project(self, project_id: str) -> bool:
        """defer 回队复用：项目置回 active，恢复调度（星图进度无损）。"""
        try:
            response = requests.put(
                f"{ASTRA_SERVER_URL}/projects/{project_id}/status",
                json={"status": "active"},
                headers=_auth_headers(),
                timeout=15,
            )
            response.raise_for_status()
            LOG.info("project reactivated (defer resume) project=%s", project_id)
            return True
        except requests.RequestException as exc:
            # 终态（completed）/不存在的项目不可复用——返回 False 让调用方新建项目，
            # 否则题线程会永远轮询一个不会再有产出的星图（resume 死锁，实测 19:02 循环）
            LOG.warning("reactivate_project failed project=%s error=%s（不可复用，应新建）", project_id, exc)
            return False

    def list_active_projects(self) -> list[dict]:
        """对账扫描（R5 修复清单 1b）：引擎侧 active 项目 [{id, created_at}]。"""
        response = requests.get(f"{ASTRA_SERVER_URL}/projects", headers=_auth_headers(),  timeout=15)
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("projects", [])
        return [
            {"id": p["id"], "created_at": p["created_at"]}
            for p in items
            if isinstance(p, dict) and p.get("status") == "active"
        ]

    def create_fact(self, project_id: str, description: str) -> None:
        """V2-6：注入外部 fact（知识库历史思路参考，开局一次性写入）。"""
        try:
            response = requests.post(
                f"{ASTRA_SERVER_URL}/projects/{project_id}/facts",
                headers=_auth_headers(),
            json={"description": description, "kind": "regular", "creator": "astra-runner"},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            LOG.warning("create_fact failed project=%s error=%s", project_id, exc)

    def stats(self, project_id: str) -> dict[str, int]:
        """每题统计（评审量化口径）：星记数/指引数/驳回指引数。"""
        response = requests.get(f"{ASTRA_SERVER_URL}/projects/{project_id}", headers=_auth_headers(),  timeout=10)
        response.raise_for_status()
        payload = response.json()
        hints = payload.get("hints", [])
        return {
            "facts": len(payload.get("facts", [])),
            "hints": len(hints),
            "review_hints": sum(1 for h in hints if "[审查否决]" in h.get("content", "")),
            "failure_hints": sum(1 for h in hints if "[失败学习]" in h.get("content", "")),
        }

    def stop(self) -> None:
        return None  # daemon 单例由 main 最后统一 shutdown


def shutdown_daemon() -> None:
    daemon = AstraDaemon._instance
    if daemon is not None:
        daemon.shutdown()
