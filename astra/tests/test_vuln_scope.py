"""Local-only contract and real Pi tool boundary checks for the vuln profile."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.runtime.local_process import LocalProcess
from astra.dispatcher.workers.adapters.pi import PiDriver
from test_pi_cli_integration import _ScenarioServer, _pi_runtime_or_skip, _text_chunks


def _manifest(origin: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    targets = [] if origin is None else [{"origin": origin, "path_prefixes": ["/allowed"], "methods": ["GET"]}]
    return json.dumps({
        "project_code": "PROJ2026_AISRC01",
        "not_before": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "not_after": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "targets": targets,
    })


def _worker(base_url: str, scope: str, agent_dir: Path) -> WorkerConfig:
    return WorkerConfig(
        name="vuln-local-test",
        type="pi",
        task_types=["execute"],
        max_running=1,
        priority=0,
        env={
            "PI_MODEL": "astra-test-model",
            "PI_BASE_URL": base_url,
            "PI_API_KEY": "synthetic-key",
            "PI_PROVIDER_API": "openai-completions",
            "PI_TOOL_PROFILE": "vuln",
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "ASTRA_VULN_LOCAL_TEST": "1",
            "ASTRA_VULN_SCOPE_JSON": scope,
        },
    )


def test_missing_scope_denies_worker_before_model_call(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ASTRA_VULN_SCOPE_JSON"):
        _worker("http://127.0.0.1:1/v1", "", tmp_path)


def test_collector_credential_cannot_be_worker_env(tmp_path: Path) -> None:
    worker = _worker("http://127.0.0.1:1/v1", _manifest(), tmp_path)
    payload = worker.model_dump()
    payload["env"]["ASTRA_EVIDENCE_COLLECTOR_TOKEN"] = "should-remain-dispatcher-only"
    with pytest.raises(ValidationError, match="collector token must not enter"):
        WorkerConfig.model_validate(payload)


def test_vuln_profile_has_no_shell_or_mcp(tmp_path: Path) -> None:
    worker = _worker("http://127.0.0.1:1/v1", _manifest(), tmp_path)
    argv = PiDriver().build_execute(worker, "test", None).argv
    assert argv[argv.index("--tools") + 1] == "read,scoped_request"
    assert "--no-extensions" in argv
    assert "--no-mcp" in argv
    assert "--extension" in argv
    assert "bash" not in argv[argv.index("--tools") + 1]


def _dispatch_payload(*, local: bool, explicit_flag: str | None = None) -> dict:
    production_scope = json.dumps({
        "project_code": "PROJ2026_AISRC01",
        "not_before": "2026-10-11T16:00:00Z",
        "not_after": "2026-10-26T15:59:59Z",
        "targets": [{"origin": "https://target.example.test", "path_prefixes": ["/allowed"], "methods": ["GET"]}],
    })
    env = {
        "PI_MODEL": "model",
        "PI_BASE_URL": "http://127.0.0.1:8000/v1" if local else "https://gateway.example.test/v1",
        "PI_API_KEY": "synthetic",
        "PI_PROVIDER_API": "openai-completions",
        "PI_TOOL_PROFILE": "vuln",
        "ASTRA_VULN_GATEWAY_URL": "https://gateway.example.test/v1",
        "ASTRA_VULN_SCOPE_JSON": _manifest("http://127.0.0.1:8001") if local else production_scope,
    }
    if explicit_flag is not None:
        env["ASTRA_VULN_LOCAL_TEST"] = explicit_flag
    return {
        "server": "http://127.0.0.1:8000",
        "runtime": {"interval": 1, "max_workers": 1, "max_running_projects": 1, "max_project_workers": 1,
                     "healthcheck_timeout": 1, "prompt_group": "vuln", "execution": "local" if local else "docker"},
        "tasks": {"bootstrap": {"timeout": 1, "conclude_timeout": 1}, "decide": {"timeout": 1, "max_steps": 1},
                  "execute": {"timeout": 1, "conclude_timeout": 1}},
        "container": {"image": "test", "network_mode": "none", "completed_action": "stop"},
        "workers": [{"name": "vuln", "type": "pi", "task_types": ["execute"], "max_running": 1, "priority": 0, "env": env}],
    }


def test_production_requires_explicit_zero_local_test_flag() -> None:
    copied_local = _dispatch_payload(local=False, explicit_flag="1")
    copied_local["workers"][0]["env"]["PI_BASE_URL"] = "http://127.0.0.1:9000/v1"
    copied_local["workers"][0]["env"]["ASTRA_VULN_SCOPE_JSON"] = _manifest("http://127.0.0.1:8080")
    with pytest.raises(ValidationError, match="ASTRA_VULN_LOCAL_TEST=0"):
        DispatchConfig.model_validate(copied_local)
    with pytest.raises(ValidationError, match="ASTRA_VULN_LOCAL_TEST=0"):
        DispatchConfig.model_validate(_dispatch_payload(local=False))


class _Target:
    def __init__(self) -> None:
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits.append(self.path)
                if self.path == "/allowed/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/outside")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"synthetic allowed evidence")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "_Target":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _tool_chunks(url: str) -> list[dict]:
    return [
        {
            "id": "chatcmpl-scope", "object": "chat.completion.chunk", "model": "astra-test-model",
            "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call-scoped", "type": "function",
                "function": {"name": "scoped_request", "arguments": json.dumps({"url": url, "method": "GET"})},
            }]}, "finish_reason": None}],
        },
        {"id": "chatcmpl-scope", "object": "chat.completion.chunk", "model": "astra-test-model",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


@pytest.mark.parametrize("case,expected_hits,expected_text", [
    ("empty", [], "scope"),
    ("outside", [], "outside authorized scope"),
    ("sibling", [], "outside authorized scope"),
    ("encoded", [], "Encoded or backslash path"),
    ("allowed", ["/allowed/data"], "synthetic allowed evidence"),
    ("redirect", ["/allowed/redirect"], "outside authorized scope"),
])
def test_real_pi_scoped_request_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    case: str, expected_hits: list[str], expected_text: str,
) -> None:
    _pi_runtime_or_skip(monkeypatch, tmp_path)
    with _Target() as target:
        url = {
            "empty": target.origin + "/allowed/data",
            "outside": target.origin + "/outside",
            "sibling": target.origin + "/allowed-evasion",
            "encoded": target.origin + "/allowed%2Foutside",
            "allowed": target.origin + "/allowed/data",
            "redirect": target.origin + "/allowed/redirect",
        }[case]

        def respond(index: int, _body: dict):
            return (200, _tool_chunks(url)) if index == 1 else (200, _text_chunks("done"))

        with _ScenarioServer(respond) as model:
            scope = _manifest(None if case == "empty" else target.origin)
            worker = _worker(model.base_url, scope, tmp_path / "agent")
            driver = PiDriver()
            invocation = driver.build_execute(worker, "Call scoped_request once, then conclude.", None)
            process = LocalProcess(invocation.argv, worker.env)
            process.start()
            result = process.communicate(timeout=45)
            assert result.returncode == 0, result.stderr
            assert target.hits == expected_hits
            collected = driver.extract_scoped_request_evidence(result.stdout)
            if case == "allowed":
                assert len(collected) == 1
                assert collected[0]["tool_call_id"] == "call-scoped"
                assert base64.b64decode(collected[0]["body_base64"]) == b"synthetic allowed evidence"
                assert collected[0]["scope_sha256"] == hashlib.sha256(scope.encode("utf-8")).hexdigest()
            else:
                assert collected == []
            tools = model.requests[0].get("tools", [])
            names = {tool.get("function", {}).get("name") for tool in tools}
            assert "scoped_request" in names
            assert "bash" not in names
            assert "powershell" not in names
            assert "write" not in names
            assert "edit" not in names
            tool_messages = [item for item in model.requests[1]["messages"] if item.get("role") == "tool"]
            assert expected_text in str(tool_messages[-1].get("content"))


def test_redirect_rechecks_authorization_window_before_next_request() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the scoped_request extension")
    extension = (Path(__file__).resolve().parents[1] / "src" / "astra" / "dispatcher"
                 / "workers" / "adapters" / "vuln_scope.js").as_uri()
    script = f"import register from {json.dumps(extension)};\n" + """
import http from 'node:http';
import { EventEmitter } from 'node:events';

const before = Date.parse('2026-10-11T16:00:00Z');
const after = before + 1000;
let now = before + 100;
Date.now = () => now;
process.env.ASTRA_VULN_LOCAL_TEST = '1';
process.env.ASTRA_VULN_SCOPE_JSON = JSON.stringify({
  project_code: 'PROJ2026_AISRC01',
  not_before: new Date(before).toISOString(),
  not_after: new Date(after).toISOString(),
  targets: [{ origin: 'http://127.0.0.1:8080', path_prefixes: ['/allowed'], methods: ['GET'] }],
});

const calls = [];
http.request = (url, _options, onResponse) => {
  calls.push(url.href);
  const request = new EventEmitter();
  request.end = () => queueMicrotask(() => {
    const response = new EventEmitter();
    response.statusCode = calls.length === 1 ? 302 : 200;
    response.headers = calls.length === 1 ? { location: '/allowed/next' } : {};
    if (calls.length === 1) now = after + 1;
    onResponse(response);
    response.emit('end');
  });
  request.destroy = (error) => request.emit('error', error);
  return request;
};

let tool;
register({ registerTool(value) { tool = value; } });
let error = null;
try {
  await tool.execute('call-1', { url: 'http://127.0.0.1:8080/allowed/start', method: 'GET' });
} catch (caught) {
  error = caught.message;
}
console.log(JSON.stringify({ calls, error }));
if (calls.length !== 1 || !error?.includes('Outside authorized time window')) process.exitCode = 1;
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reserved_192_0_0_address_is_denied_before_request() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the scoped_request extension")
    extension = (Path(__file__).resolve().parents[1] / "src" / "astra" / "dispatcher"
                 / "workers" / "adapters" / "vuln_scope.js").as_uri()
    script = f"import register from {json.dumps(extension)};\n" + """
import https from 'node:https';

const now = Date.parse('2026-10-11T16:00:00Z');
Date.now = () => now;
process.env.ASTRA_VULN_LOCAL_TEST = '0';
process.env.ASTRA_VULN_SCOPE_JSON = JSON.stringify({
  project_code: 'PROJ2026_AISRC01',
  not_before: new Date(now - 1000).toISOString(),
  not_after: new Date(now + 1000).toISOString(),
  targets: [{ origin: 'https://192.0.0.42', path_prefixes: ['/allowed'], methods: ['GET'] }],
});

let requests = 0;
https.request = () => { requests++; throw new Error('Network request must not run'); };
let tool;
register({ registerTool(value) { tool = value; } });
let error = null;
try {
  await tool.execute('call-1', { url: 'https://192.0.0.42/allowed', method: 'GET' });
} catch (caught) {
  error = caught.message;
}
console.log(JSON.stringify({ requests, error }));
if (requests !== 0 || error !== 'Private/reserved IP is denied') process.exitCode = 1;
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
