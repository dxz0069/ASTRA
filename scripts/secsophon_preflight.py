"""Fail-closed model endpoint check and optional real Pi tool/session smoke test.

Run with ``uv run --project astra python scripts/secsophon_preflight.py ...``.
This checks the model route, not the network behavior of arbitrary bash tools.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from astra.dispatcher.config import DispatchConfig
from astra.dispatcher.runtime.local_process import LocalProcess
from astra.dispatcher.workers.adapters.pi import PiDriver


def _versions() -> dict[str, str]:
    command = ["node", PiDriver._pi_cli_js(), "--version"] if sys.platform == "win32" else ["pi", "--version"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError("Pi CLI version check failed")
    version = result.stdout.strip()
    if version != "1.1.0":
        raise RuntimeError(f"Pi CLI must be 1.1.0, got {version!r}")
    return {"pi": version, "python": platform.python_version(), "machine": platform.machine()}


def _catalog(worker) -> bool:
    base = worker.env["PI_BASE_URL"].rstrip("/")
    request = Request(base + "/models", headers={"Authorization": f"Bearer {worker.env['PI_API_KEY']}"})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    models = payload.get("data", [])
    return any(isinstance(item, dict) and item.get("id") == worker.env["PI_MODEL"] for item in models)


def _run(command: list[str], env: dict[str, str], timeout: int) -> tuple[object, float]:
    started = time.monotonic()
    process = LocalProcess(command, env)
    process.start()
    result = process.communicate(timeout=timeout)
    return result, round(time.monotonic() - started, 2)


def _events(stdout: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            counts[event["type"]] += 1
    return counts


def _tool_names(stdout: str) -> list[str]:
    names: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "tool_execution_start":
            name = event.get("toolName")
            if isinstance(name, str):
                names.append(name)
    return names


def _smoke(worker, timeout: int) -> dict[str, object]:
    driver = PiDriver()
    with tempfile.TemporaryDirectory(prefix="astra-offline-smoke-") as temporary:
        root = Path(temporary)
        marker = "ASTRA_LOCAL_MODEL_READ_PROOF_61942"
        evidence = root / "read-proof.txt"
        evidence.write_text(marker, encoding="utf-8")
        env = dict(worker.env)
        env["PI_CODING_AGENT_DIR"] = str(root / "pi-home")
        isolated_worker = worker.model_copy(update={"env": env})
        first = driver.build_execute(
            isolated_worker,
            f"Use the read tool to open {evidence}. Then reply with only the exact token in that file.",
            None,
        )
        first_result, first_seconds = _run(first.argv, env, timeout)
        first_events = _events(first_result.stdout)
        tool_names = _tool_names(first_result.stdout)
        session = driver.extract_session(first.session, first_result.stdout, first_result.stderr)
        first_text = driver.extract_response_text(first_result.stdout, first_result.stderr)
        first_ok = (
            first_result.returncode == 0
            and not first_result.timed_out
            and first_events["tool_execution_end"] >= 1
            and "read" in tool_names
            and first_events["agent_settled"] >= 1
            and marker in first_text
            and bool(session)
        )
        report: dict[str, object] = {
            "tool_turn_seconds": first_seconds,
            "tool_events": first_events["tool_execution_end"],
            "tool_names": tool_names,
            "tool_turn_settled": bool(first_events["agent_settled"]),
            "response_contains_marker": marker in first_text,
            "response_preview": first_text[:160],
            "tool_turn_ok": first_ok,
            "session_created": bool(session),
        }
        if not first_ok or not session:
            return report
        follow = driver.build_conclude(
            isolated_worker,
            "In this same session, reply with only the exact token you read from the file earlier.",
            session,
        )
        follow_result, follow_seconds = _run(follow, env, timeout)
        follow_events = _events(follow_result.stdout)
        report["resume_seconds"] = follow_seconds
        report["resume_ok"] = (
            follow_result.returncode == 0
            and not follow_result.timed_out
            and follow_events["agent_settled"] >= 1
            and marker in driver.extract_response_text(follow_result.stdout, follow_result.stderr)
        )
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--smoke", action="store_true", help="call the real model through Pi, read a local file, and resume its session")
    parser.add_argument("--timeout", type=int, default=180, help="seconds allowed per Pi turn")
    parser.add_argument("--require-arm64", action="store_true", help="fail unless running on an ARM64 host")
    args = parser.parse_args()
    try:
        config = DispatchConfig.load(args.config)
        if config.runtime.execution != "local" or config.runtime.offline_model_policy == "disabled":
            raise RuntimeError("SecSophon requires local execution and an enabled offline_model_policy")
        if args.require_arm64 and platform.machine().lower() not in {"aarch64", "arm64"}:
            raise RuntimeError("ARM64 host required; current machine is only a development proxy")
        report: dict[str, object] = {"versions": _versions(), "policy": config.runtime.offline_model_policy}
        report["model_catalog"] = {worker.name: _catalog(worker) for worker in config.workers}
        if not all(report["model_catalog"].values()):
            raise RuntimeError("configured model missing from local model catalog")
        if args.smoke:
            report["smoke"] = _smoke(config.workers[0], args.timeout)
            if not report["smoke"]["tool_turn_ok"] or not report["smoke"].get("resume_ok"):
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
