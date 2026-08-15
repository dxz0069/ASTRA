from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import sys

from astra.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from astra.dispatcher.workers.adapters.codex import CodexDriver
from astra.dispatcher.workers.adapters.dsh import DshDriver
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
                "task_types": ["explore"],
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
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"MOCK_UNKNOWN": "{}"},
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
            "task_types": ["explore"],
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
        prompt_idx = result.argv.index("-p")
        assert result.argv[prompt_idx + 1].startswith("@")  # prompt 以 @file 传入
    else:
        models = json.loads(result.argv[5])
        assert models["providers"]["astra"]["models"][0]["contextWindow"] == 131072
        assert "--tools" in result.argv
        assert result.argv[-2:] == ["-p", "prompt"]


def test_codex_driver_execute_argv_passes_model_endpoint_and_prompt() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "codex",
            "type": "codex",
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "gpt-test",
                "CODEX_BASE_URL": "http://api/v1",
                "OPENAI_API_KEY": "secret",
            },
        }
    )

    argv = CodexDriver().build_execute(worker, "prompt", None).argv

    assert "--model" in argv
    assert "gpt-test" in argv
    assert 'model_providers.astra.base_url="http://api/v1"' in argv
    assert argv[-2:] == ["--", "prompt"]


def _dsh_worker(**env_overrides: str) -> WorkerConfig:
    env: dict[str, str] = {
        "DSH_MODEL": "deepseek-v4-pro",
        "DEEPSEEK_API_KEY": "secret",
        "DSH_PATCH": "/opt/astra/dsh/astra-headless.patch.yml",
    }
    env.update(env_overrides)
    return WorkerConfig.model_validate(
        {
            "name": "dsh-worker",
            "type": "dsh",
            "task_types": ["bootstrap", "reason", "explore"],
            "max_running": 1,
            "priority": 0,
            "env": env,
        }
    )


def test_dsh_worker_requires_model_and_api_key() -> None:
    with pytest.raises(ValidationError, match="missing env keys: DSH_MODEL"):
        WorkerConfig.model_validate(
            {
                "name": "dsh-worker",
                "type": "dsh",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {},
            }
        )


def test_dsh_worker_anthropic_mode_requires_anthropic_token() -> None:
    # deepseek 模式缺 DEEPSEEK_API_KEY
    with pytest.raises(ValidationError, match="missing env keys: DEEPSEEK_API_KEY"):
        WorkerConfig.model_validate(
            {
                "name": "dsh-worker",
                "type": "dsh",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"DSH_MODEL": "k3", "DSH_PROVIDER": "deepseek"},
            }
        )
    # anthropic 模式缺 ANTHROPIC_AUTH_TOKEN
    with pytest.raises(ValidationError, match="missing env keys: ANTHROPIC_AUTH_TOKEN"):
        WorkerConfig.model_validate(
            {
                "name": "dsh-worker",
                "type": "dsh",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"DSH_MODEL": "k3", "DSH_PROVIDER": "anthropic"},
            }
        )
    # 非法 provider
    with pytest.raises(ValidationError, match="DSH_PROVIDER must be deepseek, anthropic or zhipu"):
        WorkerConfig.model_validate(
            {
                "name": "dsh-worker",
                "type": "dsh",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"DSH_MODEL": "k3", "DSH_PROVIDER": "moonshot"},
            }
        )
    # anthropic 模式合法配置
    worker = WorkerConfig.model_validate(
        {
            "name": "dsh-worker",
            "type": "dsh",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {"DSH_MODEL": "k3", "DSH_PROVIDER": "anthropic", "ANTHROPIC_AUTH_TOKEN": "sk-kimi"},
        }
    )
    assert worker.env["ANTHROPIC_AUTH_TOKEN"] == "sk-kimi"


def test_dsh_driver_anthropic_mode_healthcheck() -> None:
    worker = _dsh_worker(
        DSH_PROVIDER="anthropic",
        ANTHROPIC_AUTH_TOKEN="sk-kimi",
        ANTHROPIC_BASE_URL="https://api.kimi.com/coding/",
    )
    argv = DshDriver().build_healthcheck(worker)

    assert "https://api.kimi.com/coding/v1/messages" in argv
    assert "x-api-key: sk-kimi" in argv
    assert "anthropic-version: 2023-06-01" in argv
    assert '"model":"deepseek-v4-pro"' in argv[-1]  # DSH_MODEL 仍是 wire 模型名


def test_dsh_driver_anthropic_mode_describe_uses_env_expansion() -> None:
    worker = _dsh_worker(
        DSH_PROVIDER="anthropic",
        ANTHROPIC_AUTH_TOKEN="sk-kimi",
        ANTHROPIC_BASE_URL="https://api.kimi.com/coding/",
    )
    described = DshDriver().describe_startup_healthcheck(worker)
    assert "x-api-key: $ANTHROPIC_AUTH_TOKEN" in described
    assert "anthropic-version: 2023-06-01" in described


def test_dsh_driver_deepseek_healthcheck_unaffected_by_anthropic_mode() -> None:
    worker = _dsh_worker(DSH_PROVIDER="deepseek")
    argv = DshDriver().build_healthcheck(worker)
    assert "https://api.deepseek.com/chat/completions" in argv
    assert "Authorization: Bearer secret" in argv


def _assert_last_arg_is_prompt(argv: list[str], expected: str) -> None:
    """Windows 下 prompt 走 @file（由 astra 定制 startup 展开），其余平台直接 argv。"""
    import sys
    from pathlib import Path

    last = argv[-1]
    if sys.platform == "win32":
        assert last.startswith("@")
        assert Path(last[1:]).read_text(encoding="utf-8") == expected
    else:
        assert last == expected


def test_dsh_driver_execute_argv_includes_profile_patch_session_and_prompt() -> None:
    worker = _dsh_worker()
    result = DshDriver().build_execute(worker, "prompt", "session-abc")

    assert result.session == "session-abc"
    # Windows 下 node 直跑 lib/bin.js（.CMD shim 会破坏含换行的长 prompt）；其他平台直接 dsh
    if sys.platform == "win32":
        assert result.argv[0] == "node"
        assert result.argv[1].endswith("bin.js")
    else:
        assert result.argv[0] == "dsh"
    assert result.argv[1:3] == ["--profile", "headless"] if sys.platform != "win32" else result.argv[2:4] == ["--profile", "headless"]
    patch_idx = result.argv.index("--patch")
    assert result.argv[patch_idx + 1] == "/opt/astra/dsh/astra-headless.patch.yml"
    session_idx = result.argv.index("--session")
    assert result.argv[session_idx + 1] == "session-abc"
    _assert_last_arg_is_prompt(result.argv, "prompt")


def test_dsh_driver_conclude_reuses_same_session() -> None:
    worker = _dsh_worker()
    argv = DshDriver().build_conclude(worker, "conclude-prompt", "session-abc")

    _assert_last_arg_is_prompt(argv, "conclude-prompt")
    session_idx = argv.index("--session")
    assert argv[session_idx + 1] == "session-abc"


def test_dsh_driver_resume_disabled_drops_session_flag() -> None:
    worker = _dsh_worker(DSH_RESUME="0")
    argv = DshDriver().build_execute(worker, "prompt", "session-abc").argv

    assert "--session" not in argv
    _assert_last_arg_is_prompt(argv, "prompt")


def test_dsh_driver_prepare_session_uses_dsh_id_shape() -> None:
    session = DshDriver().prepare_session()
    assert session.startswith("session-")
    assert len(session) > len("session-")


def test_dsh_driver_healthcheck_hits_chat_completions_with_bearer() -> None:
    worker = _dsh_worker(DEEPSEEK_BASE_URL="https://gateway.example.com")
    argv = DshDriver().build_healthcheck(worker)

    assert argv[0] == "curl"
    assert "https://gateway.example.com/chat/completions" in argv
    assert "Authorization: Bearer secret" in argv
    assert '"model":"deepseek-v4-pro"' in argv[-1]


def test_dsh_driver_healthcheck_defaults_to_official_base_url() -> None:
    argv = DshDriver().build_healthcheck(_dsh_worker())
    assert "https://api.deepseek.com/chat/completions" in argv


def test_dsh_driver_startup_healthcheck_and_describe() -> None:
    worker = _dsh_worker()
    driver = DshDriver()

    startup = driver.build_startup_healthcheck(worker)
    assert "curl" in startup
    assert "http_status=" in " ".join(startup)

    described = driver.describe_startup_healthcheck(worker)
    assert "Authorization: Bearer $DEEPSEEK_API_KEY" in described
    assert "https://api.deepseek.com/chat/completions" in described


def test_dsh_driver_windows_uses_node_direct_and_atfile_prompt(monkeypatch, tmp_path) -> None:
    from astra.dispatcher.workers.adapters import dsh as dsh_module

    bin_js = tmp_path / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    bin_js.parent.mkdir(parents=True)
    bin_js.write_text("", encoding="utf-8")
    cmd_shim = tmp_path / "dsh.CMD"
    cmd_shim.write_text("", encoding="utf-8")

    monkeypatch.setattr(dsh_module.sys, "platform", "win32")
    monkeypatch.setattr(dsh_module.shutil, "which", lambda _name: str(cmd_shim))

    argv = DshDriver().build_execute(_dsh_worker(), "prompt", "session-abc").argv

    assert argv[0] == "node"
    assert argv[1] == str(bin_js)
    assert argv[-1].startswith("@")  # prompt 以 @file 传入
