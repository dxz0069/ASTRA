"""LocalAstraEngine —— 本地模式 ASTRA 引擎封装（server + dispatcher 进程管理 + API）。

- server/dispatcher 为进程级共享单例（幂等启动，最后 shutdown）
- dispatch.yaml 由环境变量动态生成：execution=local，DeepSeek 主力（claudecode +
  ANTHROPIC 兼容端点），reason 任务与双星审查复用同一 worker 配置
- 每个题目一个 ASTRA 项目，origin=靶场地址，goal=题目描述
"""

from __future__ import annotations

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


def _dsh_home_root() -> str:
    """dsh worker 会话根目录：优先 ASTRA_DSH_HOME（ASTRA 专用覆盖键，兼容历史
    单 worker 语义——作为根目录使用，worker 目录拼在其下）。刻意不用环境变量
    DSH_HOME——用户机器常有全局 DSH_HOME（~/.dsh），会让所有 worker 共享
    会话/凭据目录导致跨项目混杂。"""
    return os.environ.get("ASTRA_DSH_HOME") or str(Path(tempfile.gettempdir()) / "astra-dsh")


def _dsh_home(worker_name: str) -> str:
    """按 worker 隔离的 DSH_HOME（会话/凭据互不串扰）。"""
    return str(Path(_dsh_home_root()) / worker_name)


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
        self._cleanup_dsh_home()
        db_path = Path(tempfile.gettempdir()) / f"astra-runner-{uuid.uuid4().hex[:8]}.db"
        LOG.info("starting astra server db=%s", db_path)
        self._server = _popen([*ASTRA_SERVER_CMD.split(), "--db-path", str(db_path), "--no-access-log"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                raise RuntimeError(f"astra server exited early: {self._server.returncode}")
            try:
                requests.get(f"{ASTRA_SERVER_URL}/projects", timeout=2).raise_for_status()
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

        ASTRA_WORKER_TYPE 选择 worker：
          - dsh（默认，2026-08-15 起）：DeepSeek Harness 无头模式；DSH_MODEL /
            DEEPSEEK_API_KEY，双 key（+ZHIPU_API_KEY）时自动混合舰队。此前默认
            claudecode 曾导致不带该变量启动时静默回落单模型 claudecode（run 9214
            实测退步），故默认翻转为 dsh。
          - claudecode：claude CLI + DeepSeek Anthropic 兼容端点（ANTHROPIC_*）
        """
        worker_type = os.environ.get("ASTRA_WORKER_TYPE", "dsh")
        if worker_type == "claudecode":
            # 默认/推荐路径是 dsh（run 9214 曾因静默回落 claudecode 退步 1230 分）；
            # 走到这里说明被显式指定——大声提示，防手滑。
            LOG.warning(
                "ASTRA_WORKER_TYPE=claudecode（显式指定）：默认路径是 dsh 混合舰队，"
                "claudecode 为单模型旧路径，仅应在有意回退时使用",
            )
        # 并发副本数：多 worker 并行提升吞吐（默认 2，可 ASTRA_WORKER_REPLICAS 覆盖）
        replicas = max(1, int(os.environ.get("ASTRA_WORKER_REPLICAS", "2")))
        # 混合模型分流（默认开）：explore/bootstrap 用强模型，reason/consolidate 用便宜模型
        mix_models = os.environ.get("ASTRA_MIX_MODELS", "1") == "1"
        # 混合舰队（2026-08-15）：dsh 模式下 DEEPSEEK_API_KEY 与 ZHIPU_API_KEY 同时
        # 存在时自动渲染 4 worker 混编（DS 探索 + GLM 探索 + GLM 决策 + DS 兜底），
        # 同一题的多个 intent 可由不同模型并行探索（多路并进）。设 ASTRA_MIX_PROVIDERS=0 关闭。
        mix_providers = os.environ.get("ASTRA_MIX_PROVIDERS", "auto")
        mixed = (
            worker_type == "dsh"
            and mix_providers in ("auto", "1", "true")
            and os.environ.get("DEEPSEEK_API_KEY")
            and os.environ.get("ZHIPU_API_KEY")
        )
        common_env = {
            "BENCHMARK_TOKEN": os.environ.get("BENCHMARK_TOKEN", ""),
            "BENCHMARK_BASE_URL": os.environ.get("BENCHMARK_BASE_URL", ""),
        }
        common_env = {k: v for k, v in common_env.items() if v}
        if worker_type == "dsh":
            worker_block = self._render_dsh_mixed_workers() if mixed else self._render_dsh_worker()
        elif worker_type == "claudecode":
            blocks = []
            for i in range(replicas):
                name = "deepseek-main" if replicas == 1 else f"deepseek-main-{i}"
                if mix_models and replicas >= 2:
                    # 副本 0：探索+首探（强模型）；副本 1：决策+审查（便宜模型）
                    if i == 0:
                        blocks.append(self._render_claudecode_worker(name, ["bootstrap", "explore"]))
                    else:
                        blocks.append(self._render_claudecode_worker(name, ["reason", "consolidate"]))
                else:
                    blocks.append(self._render_claudecode_worker(name))
            worker_block = "\n".join(blocks)
        else:
            raise RuntimeError(f"不支持的 ASTRA_WORKER_TYPE: {worker_type}（可选 claudecode / dsh）")
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

    def _render_claudecode_worker(self, worker_name: str = "deepseek-main", task_types: list[str] | None = None) -> str:
        """claude CLI worker（DeepSeek Anthropic 兼容端点，配置自包含隔离）。

        worker_name：worker 标识（多副本时区分）；task_types：任务分流
        （None=全部，或按 [bootstrap,explore] / [reason,consolidate] 拆混合模型）。
        """
        model = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if not auth_token:
            raise RuntimeError("缺少 ANTHROPIC_AUTH_TOKEN（国内模型密钥）")
        # claude CLI 配置隔离：不读 ~/.claude/settings.json（避免与用户其他项目的中转配置冲突）
        claude_dir = Path(tempfile.gettempdir()) / f"astra-claude-{worker_name}-{uuid.uuid4().hex[:8]}"
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_dir_yaml = str(claude_dir).replace("\\", "/")  # YAML 双引号内反斜杠是转义符
        types_line = ", ".join(task_types) if task_types else "bootstrap, reason, explore, consolidate"
        return f"""  - name: "{worker_name}"
    type: "claudecode"
    task_types: [{types_line}]
    max_running: 3
    priority: 0
    env:
      ANTHROPIC_MODEL: "{model}"
      ANTHROPIC_BASE_URL: "{base_url}"
      ANTHROPIC_AUTH_TOKEN: "{auth_token}"
      ANTHROPIC_API_KEY: "{auth_token}"
      CLAUDE_CONFIG_DIR: "{claude_dir_yaml}"
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"
      CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT: "1"
"""

    def _render_dsh_worker(self) -> str:
        """单模型 dsh worker（历史行为，向后兼容）：环境变量选择 provider/model。

        前置：已安装 @deepseek-ai/dsh，且 container/dsh/astra-headless-runner.js
        已复制进 dsh 包 lib/（提供 --session 会话续接，见 container/dsh/README.md）。
        DSH_PROVIDER 选择模型路由（与 container/dsh/astra-headless.patch.yml 的
        env 表达式一致）：
          - deepseek（默认）：DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL（官方 chat-completions）
          - anthropic：ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL（Anthropic Messages，
            适配 Kimi / DeepSeek /anthropic 兼容端点等）
          - zhipu：ZHIPU_API_KEY / ZHIPU_BASE_URL（智谱 coding 端点，GLM-5.3）
        """
        model = os.environ.get("DSH_MODEL", "deepseek-v4-flash")
        provider = os.environ.get("DSH_PROVIDER", "deepseek")
        effort = os.environ.get("DSH_REASONING_EFFORT", "")
        return self._render_dsh_worker_block(
            "deepseek-main",
            ["bootstrap", "reason", "explore", "consolidate"],
            max_running=3,
            priority=0,
            provider=provider,
            model=model,
            effort=effort,
        )

    def _render_dsh_mixed_workers(self) -> str:
        """混合舰队（DS + GLM 双通道，2026-08-15 榜单数据决策）。

        每道题最多 max_project_workers 个 intent 并行，choose_worker 按
        （priority, 运行数, 随机）选 worker——同优先级的 DS/GLM 探索位自然轮转，
        同一道题的多路探索会分到不同模型（快攻 DS + 深挖 GLM）：
          - deepseek-main   p0 bootstrap/explore ×3 —— tsecbench Top6 全员同款，吞吐主力
          - glm-main        p0 bootstrap/explore ×2 —— GLM-5.3 high 档（速度档），多路并进
          - glm-reason      p1 reason/consolidate ×2 —— GLM-5.3 xhigh→max 档（深度档）
          - deepseek-fallback p3 reason/explore ×2 —— GLM 429/限流时的决策与探索兜底

        环境变量：DEEPSEEK_API_KEY（必填）/ ZHIPU_API_KEY（必填，进入混合模式的前提）/
        DSH_MODEL（DS 模型，默认 deepseek-v4-flash）/ ZHIPU_MODEL（默认 glm-5.3）/
        ZHIPU_EXPLORE_EFFORT（默认 high）/ ZHIPU_REASON_EFFORT（默认 xhigh）。
        """
        ds_model = os.environ.get("DSH_MODEL", "deepseek-v4-flash")
        glm_model = os.environ.get("ZHIPU_MODEL", "glm-5.3")
        explore_effort = os.environ.get("ZHIPU_EXPLORE_EFFORT", "high")
        reason_effort = os.environ.get("ZHIPU_REASON_EFFORT", "xhigh")
        pro_model = os.environ.get("DSH_PRO_MODEL", "")
        blocks = [
            self._render_dsh_worker_block(
                "deepseek-main",
                ["bootstrap", "explore"],
                max_running=3,
                priority=0,
                provider="deepseek",
                model=ds_model,
            ),
        ]
        if pro_model:
            # V7 Pro 深思档：DSH_PRO_MODEL 显式设置才启用——原极简 persona
            # （'You are a helpful software engineer assistant.' + complete）激活
            # DeepSeek Pro 思考人格（we 自称），负责 reason/consolidate 深度决策。
            # minimal persona 压制该 worker 的 AGENTS.md 注入——reason prompt 自含
            # 规则与图上下文，可接受；explore 类打法指导由 flash/glm 档承担。
            blocks.append(
                self._render_dsh_worker_block(
                    "deepseek-pro",
                    ["reason", "consolidate"],
                    max_running=1,
                    priority=0,
                    provider="deepseek",
                    model=pro_model,
                    effort=os.environ.get("DSH_PRO_EFFORT", "high"),
                    persona="minimal",
                )
            )
        blocks += [
            self._render_dsh_worker_block(
                "glm-main",
                ["bootstrap", "explore"],
                max_running=2,
                priority=0,
                provider="zhipu",
                model=glm_model,
                effort=explore_effort,
            ),
            self._render_dsh_worker_block(
                "glm-reason",
                ["reason", "consolidate"],
                max_running=2,
                priority=1 if pro_model else 0,
                provider="zhipu",
                model=glm_model,
                effort=reason_effort,
            ),
            self._render_dsh_worker_block(
                "deepseek-fallback",
                ["reason", "explore"],
                max_running=2,
                priority=3,
                provider="deepseek",
                model=ds_model,
            ),
        ]
        return "\n".join(blocks)

    def _render_dsh_worker_block(
        self,
        worker_name: str,
        task_types: list[str],
        *,
        max_running: int,
        priority: int,
        provider: str,
        model: str,
        effort: str = "",
        persona: str = "",
    ) -> str:
        """单个 dsh worker 的 YAML 块（混合舰队与单 worker 共用）。

        provider 决定凭据环境变量；effort 透传给 DSH_REASONING_EFFORT
        （zhipu 路由下 high→high、xhigh→max，见 patch 的 reasoningEfforts 映射）。
        """
        dsh_patch = self._resolve_dsh_patch()
        dsh_home_yaml = _dsh_home(worker_name).replace("\\", "/")
        if provider == "anthropic":
            auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
            if not auth_token:
                raise RuntimeError("缺少 ANTHROPIC_AUTH_TOKEN（dsh worker，anthropic 模式）")
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
            base_url_line = f'      ANTHROPIC_BASE_URL: "{base_url}"\n' if base_url else ""
            credential_line = f'ANTHROPIC_AUTH_TOKEN: "{auth_token}"'
        elif provider == "deepseek":
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not api_key:
                raise RuntimeError("缺少 DEEPSEEK_API_KEY（dsh worker，deepseek 模式）")
            base_url = os.environ.get("DEEPSEEK_BASE_URL", "")
            base_url_line = f'      DEEPSEEK_BASE_URL: "{base_url}"\n' if base_url else ""
            credential_line = f'DEEPSEEK_API_KEY: "{api_key}"'
        elif provider == "zhipu":
            api_key = os.environ.get("ZHIPU_API_KEY", "")
            if not api_key:
                raise RuntimeError("缺少 ZHIPU_API_KEY（dsh worker，zhipu 模式）")
            base_url = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")
            base_url_line = f'      ZHIPU_BASE_URL: "{base_url}"\n'
            credential_line = f'ZHIPU_API_KEY: "{api_key}"'
        else:
            raise RuntimeError(f"不支持的 DSH_PROVIDER: {provider}（可选 deepseek / anthropic / zhipu）")
        reasoning_line = f'      DSH_REASONING_EFFORT: "{effort}"\n' if effort else ""
        persona_line = f'      DSH_PERSONA: "{persona}"\n' if persona else ""
        types_line = ", ".join(task_types)
        return f"""  - name: "{worker_name}"
    type: "dsh"
    task_types: [{types_line}]
    max_running: {max_running}
    priority: {priority}
    env:
      DSH_MODEL: "{model}"
      DSH_PROVIDER: "{provider}"
      {credential_line}
{base_url_line}      DSH_PERMISSION_MODE: "danger-full-access"
      DSH_HOME: "{dsh_home_yaml}"
      DSH_PATCH: "{dsh_patch}"
{persona_line}{reasoning_line}"""

    @staticmethod
    def _resolve_dsh_patch() -> str:
        """定位 astra-headless.patch.yml：env DSH_PATCH 优先；其次仓库内
        container/dsh/（本地联调）；兜底容器镜像路径 /opt/astra/dsh/。"""
        env_patch = os.environ.get("DSH_PATCH")
        if env_patch:
            return env_patch.replace("\\", "/")
        repo_patch = Path(__file__).resolve().parent.parent / "dsh" / "astra-headless.patch.yml"
        if repo_patch.exists():
            return str(repo_patch).replace("\\", "/")
        return "/opt/astra/dsh/astra-headless.patch.yml"

    @staticmethod
    def _cleanup_dsh_home(keep: int = 200, max_age_hours: float = 72.0) -> None:
        """清理 dsh worker 的旧会话目录（DSH 持久化后端不自动删除，长跑会累积）。

        只在**新一轮引擎启动**时执行（上一轮会话已无价值）：扫描所有 worker 的
        DSH_HOME（<root>/<worker>/sessions），按 mtime 全局保留最近 keep 个会话
        目录，删除更旧的。execute→conclude 跨进程需要会话存活，因此运行中绝不
        清理。local 模式下 DSH_HOME 是宿主路径，可直接操作。
        """
        root = Path(_dsh_home_root())
        if not root.is_dir():
            return

        def _is_session_dir(d: Path) -> bool:
            return (d / "session.jsonl.zstd").exists() or (d / "session.jsonl").exists()

        try:
            dirs = sorted(
                (d for d in root.rglob("*") if d.is_dir() and _is_session_dir(d)),
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        # R5 修复（会话丢失税 13+ 次）：按时间删除而非数量——近期会话绝不清理，
        # defer 续跑/conclude 依赖的会话跨引擎重启存活
        import time as _time
        cutoff = _time.time() - max_age_hours * 3600
        stale = [d for d in dirs[keep:] if d.stat().st_mtime < cutoff]
        if not stale:
            return
        for old in stale:
            try:
                shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
        LOG.info("cleaned stale dsh sessions dsh_root=%s removed=%s kept=%s", root, len(stale), len(dirs) - len(stale))

    def shutdown(self) -> None:
        for process in (self._dispatcher, self._server):
            if process is not None and process.poll() is None:
                LOG.info("stopping astra process pid=%s", process.pid)
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        self._dispatcher = None
        self._server = None


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
            json={"content": content, "creator": "astra.runner"},
            timeout=15,
        )
        if response.status_code >= 400:
            LOG.warning("create_hint failed project=%s status=%s body=%s", project_id, response.status_code, response.text[:200])

    def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = requests.get(f"{ASTRA_SERVER_URL}/projects/{project_id}", timeout=10)
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
        response = requests.get(f"{ASTRA_SERVER_URL}/projects/{project_id}", timeout=10)
        response.raise_for_status()
        return [fact["description"] for fact in response.json().get("facts", [])]

    def delete_project(self, project_id: str) -> None:
        try:
            requests.delete(f"{ASTRA_SERVER_URL}/projects/{project_id}", timeout=10)
        except requests.RequestException:
            pass

    def stop_project(self, project_id: str) -> None:
        """defer 时停项目：服务端清 worker 租约与 reason（scheduler 停止派发并取消在途
        任务），星图数据保留——修复 R5 实测的僵尸项目饿死新题问题。"""
        try:
            response = requests.put(
                f"{ASTRA_SERVER_URL}/projects/{project_id}/status",
                json={"status": "stopped"},
                timeout=15,
            )
            response.raise_for_status()
            LOG.info("project stopped (defer) project=%s", project_id)
        except requests.RequestException as exc:
            LOG.warning("stop_project failed project=%s error=%s", project_id, exc)

    def reactivate_project(self, project_id: str) -> None:
        """defer 回队复用：项目置回 active，恢复调度（星图进度无损）。"""
        try:
            response = requests.put(
                f"{ASTRA_SERVER_URL}/projects/{project_id}/status",
                json={"status": "active"},
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
        response = requests.get(f"{ASTRA_SERVER_URL}/projects", timeout=15)
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
                json={"description": description, "kind": "regular", "creator": "astra-runner"},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            LOG.warning("create_fact failed project=%s error=%s", project_id, exc)

    def stats(self, project_id: str) -> dict[str, int]:
        """每题统计（评审量化口径）：星记数/指引数/驳回指引数。"""
        response = requests.get(f"{ASTRA_SERVER_URL}/projects/{project_id}", timeout=10)
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
