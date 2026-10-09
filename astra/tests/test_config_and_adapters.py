from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import sys

from astra.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from astra.dispatcher.workers.adapters.pi import PiDriver

from conftest import make_config


def test_dispatch_config_merges_common_env_with_worker_override() -> None:
    payload = make_config().model_dump()
    payload["common_env"] = {"SHARED": "common", "OVERRIDE": "common"}
    payload["workers"][0]["env"] = {"OVERRIDE": "worker"}

    config = DispatchConfig.model_validate(payload)

    assert config.workers[0].env["SHARED"] == "common"
    assert config.workers[0].env["OVERRIDE"] == "worker"


def test_dispatch_config_defaults_worker_healthcheck_and_rejects_unknown_mode() -> None:
    payload = make_config().model_dump()
    payload["runtime"].pop("worker_healthcheck")

    assert DispatchConfig.model_validate(payload).runtime.worker_healthcheck == "startup_only"

    payload["runtime"]["worker_healthcheck"] = "sometimes"
    with pytest.raises(ValidationError):
        DispatchConfig.model_validate(payload)


def test_dispatch_config_rejects_duplicate_workers_and_excess_project_parallelism() -> None:
    payload = make_config().model_dump()
    payload["workers"].append(dict(payload["workers"][0]))
    with pytest.raises(ValidationError, match="worker names must be unique"):
        DispatchConfig.model_validate(payload)

    payload = make_config().model_dump()
    payload["runtime"]["max_project_workers"] = 3
    with pytest.raises(ValidationError, match="max_project_workers cannot exceed max_workers"):
        DispatchConfig.model_validate(payload)


def test_pi_worker_rejects_invalid_context_window() -> None:
    with pytest.raises(ValidationError, match="PI_MODEL_CONTEXT_WINDOW must be greater than 0"):
        WorkerConfig.model_validate(
            {
                "name": "pi",
                "type": "pi",
                "task_types": ["execute"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "PI_MODEL": "model",
                    "PI_BASE_URL": "http://api",
                    "PI_API_KEY": "secret",
                    "PI_PROVIDER_API": "openai-completions",
                    "PI_MODEL_CONTEXT_WINDOW": "0",
                },
            }
        )


def test_mock_worker_rejects_unknown_phase_configuration() -> None:
    with pytest.raises(ValidationError, match="unsupported mock env keys"):
        WorkerConfig.model_validate(
            {
                "name": "mock",
                "type": "mock",
                "task_types": ["execute"],
                "max_running": 1,
                "priority": 0,
                "env": {"MOCK_UNKNOWN": "{}"},
            }
        )


def test_pi_worker_rejects_invalid_tool_profile() -> None:
    with pytest.raises(ValidationError, match="PI_TOOL_PROFILE must be one of: minimal, full"):
        WorkerConfig.model_validate(
            {
                "name": "pi",
                "type": "pi",
                "task_types": ["execute"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "PI_MODEL": "model",
                    "PI_BASE_URL": "http://api",
                    "PI_API_KEY": "secret",
                    "PI_PROVIDER_API": "openai-completions",
                    "PI_TOOL_PROFILE": "legacy",
                },
            }
        )


def test_bundled_prompt_groups_have_required_placeholders() -> None:
    validate_prompt_resources("default")
    validate_prompt_resources("mock")


def test_pi_driver_models_json_and_execute_argv_include_context_window_and_tools() -> None:
    import sys
    from pathlib import Path

    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker",
            "type": "pi",
            "task_types": ["execute"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MODEL_CONTEXT_WINDOW": "131072",
            },
        }
    )

    result = PiDriver().build_execute(worker, "prompt", None)
    if sys.platform == "win32":
        # Windows：node 直跑 + prompt 走 @file（.cmd shim 会破坏长参数）
        assert result.argv[0] == "node"
        cli_js = Path(result.argv[1])
        assert cli_js.name == "cli.js"
        assert "--tools" in result.argv
        tools_idx = result.argv.index("--tools")
        assert result.argv[tools_idx + 1] == "read,write,bash,ls"
        prompt_idx = result.argv.index("-p")
        assert result.argv[prompt_idx + 1].startswith("@")  # prompt 以 @file 传入
    else:
        models = json.loads(result.argv[5])
        assert models["providers"]["astra"]["models"][0]["contextWindow"] == 131072
        assert "--tools" in result.argv
        tools_idx = result.argv.index("--tools")
        assert result.argv[tools_idx + 1] == "read,write,bash,ls"
        assert result.argv[-2:] == ["-p", "prompt"]


def test_pi_driver_decide_uses_read_only_tools_by_default() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-decide",
            "type": "pi",
            "task_types": ["decide"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
            },
        }
    )
    result = PiDriver().build_decide(worker, "prompt", None)
    tools_idx = result.argv.index("--tools")
    assert result.argv[tools_idx + 1] == "read"


def test_pi_driver_challenge_is_read_only_even_on_execute_worker() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-execute",
            "type": "pi",
            "task_types": ["bootstrap", "execute"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
            },
        }
    )
    result = PiDriver().build_challenge(worker, "review", None)
    tools_idx = result.argv.index("--tools")
    assert result.argv[tools_idx + 1] == "read"


def test_pi_driver_read_only_phases_ignore_full_profile_on_mixed_worker() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-mixed",
            "type": "pi",
            "task_types": ["decide", "execute"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_TOOL_PROFILE": "full",
            },
        }
    )
    driver = PiDriver()
    for result in (
        driver.build_decide(worker, "decide", None),
        driver.build_challenge(worker, "challenge", None),
    ):
        tools_idx = result.argv.index("--tools")
        assert result.argv[tools_idx + 1] == "read"
    execute = driver.build_execute(worker, "execute", None)
    tools_idx = execute.argv.index("--tools")
    assert execute.argv[tools_idx + 1] == "read,write,edit,bash,grep,find,ls"


def test_pi_driver_full_profile_preserves_legacy_tools() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-decide",
            "type": "pi",
            "task_types": ["decide"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_TOOL_PROFILE": "full",
            },
        }
    )
    result = PiDriver().build_execute(worker, "prompt", None)
    tools_idx = result.argv.index("--tools")
    assert result.argv[tools_idx + 1] == "read,write,edit,bash,grep,find,ls"


def test_pi_driver_healthcheck_disables_tools() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-decide",
            "type": "pi",
            "task_types": ["decide"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
            },
        }
    )
    argv = PiDriver().build_healthcheck(worker)
    assert "--no-tools" in argv
    assert "--tools" not in argv
