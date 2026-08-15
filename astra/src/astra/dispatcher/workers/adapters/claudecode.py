from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from astra.dispatcher.config import WorkerConfig
from astra.dispatcher.workers.adapters._curl import build_verbose_curl_healthcheck, expand_env, render_curl_command
from astra.dispatcher.workers.base import DriverResult, SeedSessionDriver


ANTHROPIC_VERSION = "2023-06-01"


def _claude_executable() -> str:
    """Windows 下绕过 npm 的 claude.CMD shim（批处理会破坏含换行的长参数），
    优先使用 node_modules 中的原生 claude.exe。"""
    if sys.platform != "win32":
        return "claude"
    resolved = shutil.which("claude")
    if resolved and resolved.lower().endswith(".cmd"):
        candidates = [
            # claude.cmd 同目录的 node_modules（conda/venv Scripts 布局）
            Path(resolved).resolve().parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
            # npm 全局 root（nodejs 布局）
            Path(resolved).resolve().parent.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        # 兜底：npm root -g
        try:
            import subprocess

            root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=10)
            if root.returncode == 0:
                gp = Path(root.stdout.strip()) / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
                if gp.exists():
                    return str(gp)
        except Exception:  # noqa: BLE001
            pass
    return resolved or "claude"


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        return [
            "curl",
            "-sS",
            "--fail",
            "-o",
            os.devnull,
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            "-H",
            f"x-api-key: {env['ANTHROPIC_AUTH_TOKEN']}",
            "-H",
            f"anthropic-version: {ANTHROPIC_VERSION}",
            "-H",
            "content-type: application/json",
            "-d",
            (
                '{"model":"'
                + env["ANTHROPIC_MODEL"]
                + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
            ),
        ]

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        return build_verbose_curl_healthcheck(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                f"x-api-key: {env['ANTHROPIC_AUTH_TOKEN']}",
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
            ],
            payload=(
                '{"model":"'
                + env["ANTHROPIC_MODEL"]
                + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
            ),
        )

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        env = worker.env
        return render_curl_command(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                expand_env("x-api-key: $ANTHROPIC_AUTH_TOKEN"),
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
            ],
            payload=(
                '{"model":"'
                + env["ANTHROPIC_MODEL"]
                + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
            ),
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                _claude_executable(),
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "-p",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            _claude_executable(),
            "-r",
            session,
            "--dangerously-skip-permissions",
            "-p",
            "--",
            prompt,
        ]
