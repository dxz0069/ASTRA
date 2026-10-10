from __future__ import annotations

from astra.dispatcher.config import WorkerConfig
from astra.dispatcher.workers.adapters.pi import PiDriver


def _worker(enabled: str | None = None) -> WorkerConfig:
    env = {
        "PI_MODEL": "test-model",
        "PI_BASE_URL": "http://api",
        "PI_API_KEY": "test-key",
        "PI_PROVIDER_API": "anthropic-messages",
    }
    if enabled is not None:
        env["ASTRA_STATIC_JS_ENABLED"] = enabled
    return WorkerConfig(
        name="pi-static-hint",
        type="pi",
        task_types=["execute", "decide", "strike"],
        max_running=1,
        priority=0,
        env=env,
    )


def _prompt(argv: list[str]) -> str:
    return argv[argv.index("-p") + 1]


def test_static_js_hint_is_opt_in_and_keeps_default_prompt(monkeypatch) -> None:
    driver = PiDriver()
    monkeypatch.setattr(driver, "_wrap_with_models", lambda _worker, argv, **_kwargs: argv)
    monkeypatch.setattr(driver, "_prompt_arg", lambda prompt: prompt)

    default = driver.build_execute(_worker(), "solve this", None)
    assert _prompt(default.argv) == "solve this"

    enabled = _worker("1")
    execute = driver.build_execute(enabled, "solve this", None)
    decide = driver.build_decide(enabled, "decide this", None)
    challenge = driver.build_challenge(enabled, "check this", None)
    strike = driver.build_strike(enabled, "verify this", None)
    conclude = driver.build_conclude(enabled, "summarize this", "session-1")

    for argv, original in (
        (execute.argv, "solve this"),
        (decide.argv, "decide this"),
        (challenge.argv, "check this"),
        (strike.argv, "verify this"),
        (conclude, "summarize this"),
    ):
        prompt = _prompt(argv)
        assert prompt.startswith(original + "\n\n")
        assert "astra static-js <directory>" in prompt
        assert "--run-id <run_id> --file <relative_file>" in prompt
        assert "--view semantic" in prompt
        assert "trusted local directory" in prompt
        assert "candidates, not verified findings" in prompt
        assert "original source" in prompt


def test_only_exact_opt_in_value_enables_static_js_hint(monkeypatch) -> None:
    driver = PiDriver()
    monkeypatch.setattr(driver, "_wrap_with_models", lambda _worker, argv, **_kwargs: argv)
    monkeypatch.setattr(driver, "_prompt_arg", lambda prompt: prompt)

    for value in (None, "0", "true", "yes"):
        worker = _worker(value)
        result = driver.build_decide(worker, "prompt", None)
        assert _prompt(result.argv) == "prompt"
