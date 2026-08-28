from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from astra.dispatcher.config import WorkerConfig
from astra.dispatcher.workers.base import DriverResult, WorkerDriver


class PiDriver(WorkerDriver):
    type_name = "pi"

    def supports_review(self) -> bool:
        # 实测 pi 在审查场景偶发提前退出（输出停在 message_start 后 rc=0 退出），
        # 审查对输出契约稳定性要求高 → 声明不支持，由调度层回退到 claudecode worker。
        return False

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        return self._wrap_with_models(
            worker,
            [
                "--provider",
                "astra",
                "--model",
                self._model_arg(worker),
                "--mode",
                "json",
                "--session-dir",
                self._session_dir(worker),
                "--no-session",
                "--no-tools",
                "-p",
                self._prompt_arg("Reply with exactly pong."),
            ],
            enable_tools=False,
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        env = worker.env
        argv = [
            "--provider",
            "astra",
            "--model",
            self._model_arg(worker),
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
        ]
        if session:
            argv.extend(["--session", session])
        argv.extend(["-p", self._prompt_arg(prompt)])
        return DriverResult(argv=self._wrap_with_models(worker, argv), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        env = worker.env
        argv = [
            "--provider",
            "astra",
            "--model",
            self._model_arg(worker),
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
            "--session",
            session,
            "-p",
            self._prompt_arg(prompt),
        ]
        return self._wrap_with_models(worker, argv)

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        assistant_message: dict[str, Any] | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            assistant_message = message
                            break
        if assistant_message is None:
            return stdout
        content = assistant_message.get("content")
        if not isinstance(content, list):
            return stdout
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip() or stdout

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True) -> list[str]:
        argv = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", "read,write,edit,bash,grep,find,ls"])
        if sys.platform == "win32":
            # Windows：node 直跑 + prompt 走 @file——.cmd shim 与 cmd 批处理都会破坏含换行的长参数
            import tempfile

            base_dir = Path(
                worker.env.get("PI_CODING_AGENT_DIR")
                or Path(tempfile.gettempdir()) / "astra-pi" / worker.name
            )
            # 审计修复（CWE-22）：规范化并拒绝显式遍历段（..）——worker.env 虽属受信
            # 配置面，仍不放过路径逃逸写 models.json 的可能
            base_dir = base_dir.resolve()
            if ".." in base_dir.parts:
                raise RuntimeError(f"PI_CODING_AGENT_DIR must not contain traversal segments: {base_dir}")
            base_dir.mkdir(parents=True, exist_ok=True)
            (base_dir / "sessions").mkdir(exist_ok=True)
            (base_dir / "models.json").write_text(self._models_json(worker), encoding="utf-8")
            cli_js = self._pi_cli_js()
            return ["node", cli_js, *argv, *pi_argv]
        script = (
            'agent_dir="$1"\n'
            'models_json="$2"\n'
            "shift 2\n"
            'mkdir -p "$agent_dir"\n'
            'mkdir -p "$agent_dir/sessions"\n'
            'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
            'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
        )
        return [
            shutil.which("sh") or "/bin/sh",
            "-lc",
            script,
            "--",
            self._agent_dir(worker),
            self._models_json(worker),
            *argv,
            *pi_argv,
        ]

    @staticmethod
    def _pi_cli_js() -> str:
        """定位 pi 的 node 入口（绕过 npm 的 pi.CMD / 无扩展 shim）。"""
        resolved = shutil.which("pi")
        if sys.platform == "win32":
            # Windows：无论 .CMD 还是无扩展 shim，都优先 node_modules 原生 cli.js
            bases = []
            if resolved:
                bases.append(Path(resolved).resolve().parent)
            for candidate in bases:
                cli = candidate / "node_modules" / "@mariozechner" / "pi-coding-agent" / "dist" / "cli.js"
                if cli.exists():
                    return str(cli)
        return resolved or "pi"

    @staticmethod
    def _prompt_arg(prompt: str) -> str:
        """Windows 下 prompt 写入临时文件并以 @file 传入（命令行长度/转义安全）。"""
        if sys.platform != "win32":
            return prompt
        import tempfile

        path = Path(tempfile.gettempdir()) / f"astra-pi-prompt-{uuid.uuid4().hex[:8]}.txt"
        path.write_text(prompt, encoding="utf-8")
        return "@" + str(path)

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/astra-pi") / worker.name)

    @staticmethod
    def _model_arg(worker: WorkerConfig) -> str:
        """PI_MODEL 是注册进 models.json 的干净模型 id；可选 PI_THINKING_LEVEL
        以 "model:level" 后缀强制携带思考档位（pi resolver 会剥离后缀并作为
        本会话的 reasoning level，进而经 thinkingLevelMap 映射为线上 effort 值）。"""
        level = worker.env.get("PI_THINKING_LEVEL")
        if level:
            return f"{worker.env['PI_MODEL']}:{level}"
        return worker.env["PI_MODEL"]

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _models_json(worker: WorkerConfig) -> str:
        env = worker.env
        model: dict[str, Any] = {
            "id": env["PI_MODEL"],
            "name": env["PI_MODEL"],
        }
        context_window = env.get("PI_MODEL_CONTEXT_WINDOW")
        if context_window:
            model["contextWindow"] = int(context_window)
        max_tokens = env.get("PI_MODEL_MAX_TOKENS")
        if max_tokens:
            model["maxTokens"] = int(max_tokens)
        # 推理模型扩展（可选）：reasoning 声明模型可思考；thinkingLevelMap 把
        # 运行时思考档位改写为线上值（如智谱 GLM-5.3 全档位 → "max"）；
        # compat.thinkingFormat=deepseek 即智谱原生参数形态（thinking+reasoning_effort）。
        reasoning = env.get("PI_MODEL_REASONING")
        if reasoning:
            model["reasoning"] = reasoning.lower() in ("1", "true", "yes", "on")
        level_map = env.get("PI_THINKING_LEVEL_MAP")
        if level_map:
            parsed = json.loads(level_map)
            if isinstance(parsed, dict):
                model["thinkingLevelMap"] = parsed
        compat = env.get("PI_MODEL_COMPAT")
        if compat:
            parsed = json.loads(compat)
            if isinstance(parsed, dict):
                model["compat"] = parsed

        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "apiKey": env["PI_API_KEY"],
            "models": [model],
        }
        payload = {"providers": {"astra": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
