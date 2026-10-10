"""Black-box regression tests for the upgraded Pi 1.1 CLI.

The tests deliberately exercise the command produced by :class:`PiDriver` and
the real Pi JSON event stream.  A local OpenAI-compatible SSE endpoint stands
in for a model, so no credentials or network service are involved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from astra.dispatcher.config import WorkerConfig
from astra.dispatcher.runtime.local_process import LocalProcess
from astra.dispatcher.workers.adapters.pi import PiDriver


REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_PI_PACKAGE = REPO_ROOT / "tmp" / "pi-upgrade-check" / "node_modules" / "@earendil-works" / "pi-coding-agent"
LOCAL_PI_BUNDLE = LOCAL_PI_PACKAGE / "dist" / "bundle" / "cli.js"


def _sse_response(handler: BaseHTTPRequestHandler, chunks: list[dict[str, Any]]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    for chunk in chunks:
        data = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
        handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        handler.wfile.flush()
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def _text_chunks(text: str, *, model: str = "astra-test") -> list[dict[str, Any]]:
    return [
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]


def _read_tool_chunks(path: Path) -> list[dict[str, Any]]:
    return [
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "model": "astra-test",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-read-once",
                                "type": "function",
                                "function": {"name": "read", "arguments": json.dumps({"path": str(path)})},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "model": "astra-test",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    ]


class _ScenarioServer:
    """Threaded local SSE server with a deterministic request scenario."""

    def __init__(self, responder: Callable[[int, dict[str, Any]], tuple[int, list[dict[str, Any]]]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._responder = responder
        scenario = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib protocol hook
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(size)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = {}
                scenario.requests.append(body if isinstance(body, dict) else {})
                status, chunks = scenario._responder(len(scenario.requests), body if isinstance(body, dict) else {})
                if status != 200:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"synthetic transient error"}}')
                    return
                _sse_response(self, chunks)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self) -> "_ScenarioServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _pi_runtime_or_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make an isolated 1.1.0 package (or the CI-installed Pi) executable."""
    node = shutil.which("node")
    if node and LOCAL_PI_BUNDLE.exists():
        package = json.loads((LOCAL_PI_PACKAGE / "package.json").read_text(encoding="utf-8"))
        if package.get("version") != "1.1.0":
            pytest.skip("the isolated Pi package is not version 1.1.0")
        if sys.platform == "win32":
            monkeypatch.setattr(PiDriver, "_pi_cli_js", staticmethod(lambda: str(LOCAL_PI_BUNDLE)))
        else:
            shim_dir = tmp_path / "bin"
            shim_dir.mkdir()
            shim = shim_dir / "pi"
            shim.write_text(f'#!/bin/sh\nexec "{node}" "{LOCAL_PI_BUNDLE}" "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
            monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
        return

    # CI installs the released CLI globally.  Keep this fallback so the test
    # remains useful there while still refusing to run against an older CLI.
    pi = shutil.which("pi")
    if not pi:
        pytest.skip("Pi 1.1.0 isolated CLI or global Pi is not installed")
    version = subprocess.run([pi, "--version"], capture_output=True, text=True, check=False)
    if "1.1.0" not in f"{version.stdout}\n{version.stderr}":
        pytest.skip("global Pi CLI is not version 1.1.0")


def _worker(server: _ScenarioServer, name: str, agent_dir: Path) -> WorkerConfig:
    return WorkerConfig(
        name=name,
        type="pi",
        task_types=["execute"],
        max_running=1,
        priority=0,
        env={
            "PI_MODEL": "astra-test-model",
            "PI_BASE_URL": server.base_url,
            "PI_API_KEY": "synthetic-key",
            "PI_PROVIDER_API": "openai-completions",
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_OFFLINE_MODEL_POLICY": "loopback",
        },
    )


def _run(driver: PiDriver, worker: WorkerConfig, prompt: str, session: str | None = None):
    result = driver.build_execute(worker, prompt, session)
    # The real local/container runner passes the worker environment through to
    # the child.  In particular this exposes PI_CODING_AGENT_DIR alongside the
    # provider model catalog written by PiDriver.
    process = LocalProcess(result.argv, worker.env)
    process.start()
    return result, process.communicate(timeout=45)


def _events(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]


def test_pi_110_read_then_json_and_session_conclude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pi_runtime_or_skip(monkeypatch, tmp_path)
    source = tmp_path / "evidence.txt"
    source.write_text("synthetic evidence from the read tool", encoding="utf-8")

    def respond(index: int, _body: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        if index == 1:
            return 200, _read_tool_chunks(source)
        return 200, _text_chunks('{"accepted":true,"data":{"description":"read evidence"}}')

    with _ScenarioServer(respond) as server:
        agent_dir = tmp_path / "agent"
        worker = _worker(server, "pi-read-regression", agent_dir)
        driver = PiDriver()
        first, first_result = _run(driver, worker, "Use the read tool once, then return the required JSON.")

        assert first_result.returncode == 0, first_result.stderr
        assert len(server.requests) == 2
        first_tools = server.requests[0].get("tools", [])
        assert any(
            tool.get("function", {}).get("name") == "read"
            for tool in first_tools
            if isinstance(tool, dict)
        )
        tool_messages = [
            message
            for message in server.requests[1].get("messages", [])
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        assert len(tool_messages) == 1
        assert "synthetic evidence from the read tool" in str(tool_messages[0].get("content"))
        assert any(event.get("type") == "agent_settled" for event in _events(first_result.stdout))
        first_events = _events(first_result.stdout)
        first_end = max(i for i, event in enumerate(first_events) if event.get("type") == "agent_end")
        first_settled = max(i for i, event in enumerate(first_events) if event.get("type") == "agent_settled")
        assert first_settled > first_end
        assert first_events[first_settled].get("aborted") is False
        assert driver.completion_failure(first_result.stdout, require_tool=True) is None
        session = driver.extract_session(first.session, first_result.stdout, first_result.stderr)
        assert session
        assert driver.extract_response_text(first_result.stdout, first_result.stderr) == '{"accepted":true,"data":{"description":"read evidence"}}'

        conclude_argv = driver.build_conclude(worker, "Conclude this recovered session with the same JSON.", session)
        conclude_process = LocalProcess(conclude_argv, worker.env)
        conclude_process.start()
        conclude_result = conclude_process.communicate(timeout=45)
        assert conclude_result.returncode == 0, conclude_result.stderr
        assert any(event.get("type") == "agent_settled" for event in _events(conclude_result.stdout))
        assert driver.completion_failure(conclude_result.stdout, require_tool=False) is None
        assert driver.extract_response_text(conclude_result.stdout, conclude_result.stderr) == '{"accepted":true,"data":{"description":"read evidence"}}'
        assert len(server.requests) == 3


def test_pi_110_retry_settles_only_after_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pi_runtime_or_skip(monkeypatch, tmp_path)

    def respond(index: int, _body: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        if index == 1:
            return 503, []
        return 200, _text_chunks('{"accepted":true,"data":{"description":"retry succeeded"}}')

    with _ScenarioServer(respond) as server:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "settings.json").write_text(
            json.dumps({"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 1, "maxAgentDelayMs": 1}}),
            encoding="utf-8",
        )
        worker = _worker(server, "pi-retry-regression", agent_dir)
        driver = PiDriver()
        request, result = _run(driver, worker, "Return the required JSON even after a recoverable provider error.")

        assert result.returncode == 0, result.stderr
        events = _events(result.stdout)
        assert len(server.requests) == 2
        assert any(event.get("type") == "auto_retry_start" for event in events)
        ends = [event for event in events if event.get("type") == "agent_end"]
        assert len(ends) >= 2

        def assistant_message(event: dict[str, Any]) -> dict[str, Any]:
            messages = event.get("messages", [])
            return next((message for message in reversed(messages) if message.get("role") == "assistant"), {})

        assert assistant_message(ends[0]).get("stopReason") == "error"
        assert assistant_message(ends[-1]).get("stopReason") == "stop"
        settled_index = max(i for i, event in enumerate(events) if event.get("type") == "agent_settled")
        last_end_index = max(i for i, event in enumerate(events) if event.get("type") == "agent_end")
        assert settled_index > last_end_index
        assert events[settled_index].get("aborted") is False
        assert driver.completion_failure(result.stdout, require_tool=False) is None
        assert driver.extract_response_text(result.stdout, result.stderr) == '{"accepted":true,"data":{"description":"retry succeeded"}}'
        assert request.session is None
