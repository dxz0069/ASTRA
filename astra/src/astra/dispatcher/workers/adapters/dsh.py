from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

from astra.dispatcher.config import WorkerConfig
from astra.dispatcher.workers.adapters._curl import build_verbose_curl_healthcheck, expand_env, render_curl_command
from astra.dispatcher.workers.base import DriverResult, SeedSessionDriver


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
DEFAULT_DSH_MODEL = "deepseek-v4-flash"


def _dsh_launcher() -> list[str]:
    """Windows 下绕过 npm 的 dsh.CMD shim（批处理会破坏含换行的长参数），
    优先 node 直跑原生 lib/bin.js（与 pi.py 同思路）。

    兼容两种安装布局：
      - npm 全局：<root>/node_modules/.bin/dsh.CMD → <root>/node_modules/@deepseek-ai/dsh/lib/bin.js
      - npx 缓存/嵌套：<pkg>/node_modules/.bin/dsh.CMD → <pkg>/node_modules/node_modules/...（第二候选兜底）
    """
    if sys.platform != "win32":
        return ["dsh"]
    resolved = shutil.which("dsh")
    if resolved and resolved.lower().endswith(".cmd"):
        shim = Path(resolved).resolve()
        for candidate in (
            shim.parent.parent / "@deepseek-ai" / "dsh" / "lib" / "bin.js",
            shim.parent / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js",
        ):
            if candidate.exists():
                return ["node", str(candidate)]
    return ["dsh"]


def _prompt_arg(prompt: str) -> str:
    """Windows 下 prompt 写入临时文件并以 @file 传入（命令行长度/转义安全）；
    @file 由 ASTRA 自带的 dsh headless 扩展（container/dsh/ 定制 startup）展开。
    Linux/Docker 直接作为单个 argv 传入（容器 exec 不经 shell）。"""
    if sys.platform != "win32":
        return prompt
    import tempfile

    path = Path(tempfile.gettempdir()) / f"astra-dsh-prompt-{uuid.uuid4().hex[:8]}.txt"
    path.write_text(prompt, encoding="utf-8")
    return "@" + str(path)


class DshDriver(SeedSessionDriver):
    """DeepSeek Harness 无头模式驱动，对应 claude 的 `-p` 打印模式。

    - execute:  `dsh --profile headless [--patch <astra-patch>] --session <id> <prompt>`
    - conclude: 同上，`--session` 复用同一会话（对应 claude 的 `-r`，保证
      execute→conclude 双阶段共享模型探索上下文）
    - 权限跳过：env `DSH_PERMISSION_MODE=danger-full-access`（等价
      `--dangerously-skip-permissions`，由 dispatch.yaml/runner 注入）
    - 数据隔离：env `DSH_HOME`（等价 `CLAUDE_CONFIG_DIR`）
    - 模型端点（env `DSH_PROVIDER` 选择，模型配置由 DSH 侧 patch 提供）：
      - `deepseek`（默认）：`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`，官方
        chat-completions 协议（deepseek-official 路由）
      - `anthropic`：`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`，Anthropic
        Messages 协议（由容器 dsh 扩展的 llm-pi-ai anthropic 路由提供，适配
        Kimi / DeepSeek /anthropic 兼容端点 / 任意 claude 兼容网关）
      - `zhipu`：`ZHIPU_API_KEY` / `ZHIPU_BASE_URL`（coding 专用 chat-completions
        端点），智谱原生协议；patch 的 reasoningEfforts 把全部思考档位映射为
        线上值 `max`（GLM-5.3 最高推理强度）

    `--session` 参数由本仓库自带的 dsh headless 扩展（container/dsh/ 下的
    cordis 插件 + `--patch` 覆盖层）提供：有 id 走 `agents.resume()`，无 id 走
    `agents.create()`。若使用原版 headless（未配置 DSH_PATCH），把 env
    `DSH_RESUME` 设为 "0" 走无状态模式（conclude 无法复用上下文，能力降级）。
    """

    type_name = "dsh"

    def prepare_session(self) -> str:
        # DSH 会话 id 采用 SessionId(`session-<uuid>`) 规范形态
        return f"session-{uuid.uuid4()}"

    # ---- healthcheck ----

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return [
            "curl",
            "-sS",
            "--fail",
            "-o",
            "/dev/null",
            self._healthcheck_url(worker),
            *self._healthcheck_headers(worker),
            "-d",
            self._healthcheck_payload(worker),
        ]

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return build_verbose_curl_healthcheck(
            self._healthcheck_url(worker),
            headers=self._healthcheck_headers(worker),
            payload=self._healthcheck_payload(worker),
        )

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        if self._is_anthropic(worker):
            headers: list[str | ShellArgument] = [
                "-H",
                expand_env("x-api-key: $ANTHROPIC_AUTH_TOKEN"),
                "-H",
                "anthropic-version: 2023-06-01",
                "-H",
                "content-type: application/json",
            ]
        elif self._is_zhipu(worker):
            headers = [
                "-H",
                expand_env("Authorization: Bearer $ZHIPU_API_KEY"),
                "-H",
                "content-type: application/json",
            ]
        else:
            headers = [
                "-H",
                expand_env("Authorization: Bearer $DEEPSEEK_API_KEY"),
                "-H",
                "content-type: application/json",
            ]
        return render_curl_command(
            self._healthcheck_url(worker),
            headers=headers,
            payload=self._healthcheck_payload(worker),
        )

    # ---- execute / conclude ----

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                *_dsh_launcher(),
                "--profile",
                "headless",
                *self._patch_args(worker),
                *self._resume_args(worker, session),
                _prompt_arg(prompt),
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            *_dsh_launcher(),
            "--profile",
            "headless",
            *self._patch_args(worker),
            *self._resume_args(worker, session),
            _prompt_arg(prompt),
        ]

    # ---- helpers ----

    @staticmethod
    def _patch_args(worker: WorkerConfig) -> list[str]:
        patch = worker.env.get("DSH_PATCH")
        if patch:
            return ["--patch", patch]
        return []

    @staticmethod
    def _resume_args(worker: WorkerConfig, session: str) -> list[str]:
        # DSH_RESUME=0：无状态模式（原版 headless 不认识 --session，只能这样退化）
        if worker.env.get("DSH_RESUME", "1") == "0":
            return []
        return ["--session", session]

    @staticmethod
    def _is_anthropic(worker: WorkerConfig) -> bool:
        return worker.env.get("DSH_PROVIDER", "deepseek") == "anthropic"

    @staticmethod
    def _is_zhipu(worker: WorkerConfig) -> bool:
        return worker.env.get("DSH_PROVIDER", "deepseek") == "zhipu"

    @staticmethod
    def _healthcheck_url(worker: WorkerConfig) -> str:
        if DshDriver._is_anthropic(worker):
            base = worker.env.get("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL)
            return f"{base.rstrip('/')}/v1/messages"
        if DshDriver._is_zhipu(worker):
            base = worker.env.get("ZHIPU_BASE_URL", DEFAULT_ZHIPU_BASE_URL)
            return f"{base.rstrip('/')}/chat/completions"
        base = worker.env.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
        return f"{base.rstrip('/')}/chat/completions"

    @staticmethod
    def _healthcheck_headers(worker: WorkerConfig) -> list[str]:
        if DshDriver._is_anthropic(worker):
            return [
                "-H",
                f"x-api-key: {worker.env['ANTHROPIC_AUTH_TOKEN']}",
                "-H",
                "anthropic-version: 2023-06-01",
                "-H",
                "content-type: application/json",
            ]
        if DshDriver._is_zhipu(worker):
            return [
                "-H",
                f"Authorization: Bearer {worker.env['ZHIPU_API_KEY']}",
                "-H",
                "content-type: application/json",
            ]
        return [
            "-H",
            f"Authorization: Bearer {worker.env['DEEPSEEK_API_KEY']}",
            "-H",
            "content-type: application/json",
        ]

    @staticmethod
    def _healthcheck_payload(worker: WorkerConfig) -> str:
        model = worker.env.get("DSH_MODEL", DEFAULT_DSH_MODEL)
        return (
            '{"model":"'
            + model
            + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
        )
