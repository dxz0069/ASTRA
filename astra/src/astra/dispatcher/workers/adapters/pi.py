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

    _INSTALL_HINT = (
        "npm install -g --engine-strict @earendil-works/pi-coding-agent@1.1.0"
    )
    # Pi moved from @mariozechner to @earendil-works and the bundled CLI
    # moved from dist/cli.js to dist/bundle/cli.js. Keep the old candidates
    # as a local-development fallback while making the current package first.
    _WINDOWS_CLI_CANDIDATES = (
        ("@earendil-works", "pi-coding-agent", "dist", "bundle", "cli.js"),
        ("@earendil-works", "pi-coding-agent", "dist", "cli.js"),
        ("@mariozechner", "pi-coding-agent", "dist", "cli.js"),
        ("@mariozechner", "pi-coding-agent", "dist", "bundle", "cli.js"),
    )

    _FULL_TOOLS = "read,write,edit,bash,grep,find,ls"
    _EXECUTE_TOOLS = "read,write,bash,ls"
    _READONLY_TOOLS = "read"

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
        return self._build_run(worker, prompt, session)

    def build_decide(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return self._build_run(worker, prompt, session, read_only=True)

    def build_challenge(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return self._build_run(worker, prompt, session, read_only=True)

    def build_strike(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        # Strike starts a fresh session and needs the execution tools to
        # independently reproduce a claim instead of merely reading its report.
        return self._build_run(worker, prompt, session)

    def _build_run(
        self, worker: WorkerConfig, prompt: str, session: str | None, *, read_only: bool = False
    ) -> DriverResult:
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
        command = self._wrap_with_models(worker, argv, read_only=read_only)
        return DriverResult(argv=command, session=session)

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
        # message_end is the authoritative completed assistant message in
        # Pi 1.1 JSON mode. turn_end and agent_end remain fallbacks for older
        # streams; agent_end may be followed by retry/compaction recovery.
        message_end: dict[str, Any] | None = None
        turn_end: dict[str, Any] | None = None
        agent_end: dict[str, Any] | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    message_end = message
            elif event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    turn_end = message
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            agent_end = message
                            break
        assistant_message = message_end or turn_end or agent_end
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

    def _wrap_with_models(
        self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True, read_only: bool = False
    ) -> list[str]:
        argv = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", self._tool_list(worker, read_only=read_only)])
        if sys.platform == "win32":
            # Windows：node 直跑 + prompt 走 @file——.cmd shim 与 cmd 批处理都会破坏含换行的长参数
            import tempfile

            base_dir = Path(
                worker.env.get("PI_CODING_AGENT_DIR")
                or Path(tempfile.gettempdir()) / "astra-pi" / worker.name
            )
            # 审计修复（CWE-22）：规范化并拒绝显式遍历段（..）——worker.env 虽属受信
            # 配置面，仍不放过路径逃逸写 models.json 的可能
            # 审计十一轮：必须查【原始路径】的 parts——resolve() 会把 .. 折叠掉，
            # 先 resolve 再检查等于永不清真（旧防线是死代码，E:/a/../../escape 直穿）
            raw_parts = Path(base_dir).parts
            if ".." in raw_parts:
                raise RuntimeError(f"PI_CODING_AGENT_DIR must not contain traversal segments: {base_dir}")
            base_dir = base_dir.resolve()
            cli_js = self._pi_cli_js()  # 先定位 CLI：缺失 fail-fast 带安装指引
            try:
                base_dir.mkdir(parents=True, exist_ok=True)
                (base_dir / "sessions").mkdir(exist_ok=True)
                (base_dir / "models.json").write_text(self._models_json(worker), encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"pi worker 目录/models.json 写入失败 dir={base_dir}（检查磁盘空间与权限）: {exc}"
                ) from exc
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

    @classmethod
    def _tool_list(cls, worker: WorkerConfig, *, read_only: bool = False) -> str:
        """Select Pi schemas by invocation phase, independent of worker capabilities."""
        if read_only:
            return cls._READONLY_TOOLS
        if worker.env.get("PI_TOOL_PROFILE", "minimal") == "full":
            return cls._FULL_TOOLS
        return cls._EXECUTE_TOOLS

    @staticmethod
    def _pi_cli_js() -> str:
        """定位 pi 的 node 入口（绕过 npm 的 pi.CMD / 无扩展 shim）。

        审计十一轮：CLI 完全缺失时旧版静默退化为 argv["node","pi",...]，
        报错是晦涩的 MODULE_NOT_FOUND——现在 fail-fast 给出安装指引。
        """
        resolved = shutil.which("pi")
        if sys.platform == "win32":
            # Windows：无论 .CMD 还是无扩展 shim，都优先 node_modules 原生 cli.js
            bases = []
            if resolved:
                bases.append(Path(resolved).resolve().parent)
            for candidate in bases:
                for package_parts in PiDriver._WINDOWS_CLI_CANDIDATES:
                    cli = candidate / "node_modules" / Path(*package_parts)
                    if cli.exists():
                        return str(cli)
            if not resolved:
                raise RuntimeError(
                    f"pi CLI 未安装：{PiDriver._INSTALL_HINT}"
                    "（找不到 pi 命令，也无 node_modules 入口）"
                )
        if not resolved:
            raise RuntimeError(
                f"pi CLI 未安装：{PiDriver._INSTALL_HINT}（PATH 中无 pi）"
            )
        return resolved or "pi"

    @staticmethod
    def _prompt_arg(prompt: str) -> str:
        """Windows 下 prompt 写入临时文件并以 @file 传入（命令行长度/转义安全）。

        写前顺手清理 1 小时前的旧 prompt 文件（长跑防泄漏：Windows 每任务一个文件，
        24h 高频跑可积累上万；pi 不负责回收，只能生产者自理）。清理失败静默。
        """
        if sys.platform != "win32":
            return prompt
        import tempfile
        import time

        root = Path(tempfile.gettempdir())
        try:
            cutoff = time.time() - 3600
            for stale in root.glob("astra-pi-prompt-*.txt"):
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass
        path = root / f"astra-pi-prompt-{uuid.uuid4().hex[:8]}.txt"
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
        # JSON mode uses LF framing; U+2028/U+2029 are valid inside strings.
        for line in stdout.split("\n"):
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

        # PI_PROVIDER_API 取值以 pi-ai 实现为准（spike 实证 2026-08-30）：
        # anthropic 协议端点 = "anthropic-messages"；openai 系 = "openai-completions"/"openai-responses"
        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "apiKey": env["PI_API_KEY"],
            "models": [model],
        }
        payload = {"providers": {"astra": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
