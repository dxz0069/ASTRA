from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from astra.dispatcher.config import DispatchConfig


ROOT = Path(__file__).resolve().parents[2]


def _profile() -> dict:
    config = DispatchConfig.load(ROOT / "dispatch.secsophon.example.yaml")
    return config.model_dump()


def test_secsophon_example_is_single_local_pi_worker() -> None:
    config = DispatchConfig.load(ROOT / "dispatch.secsophon.example.yaml")
    assert config.runtime.execution == "local"
    assert config.runtime.offline_model_policy == "loopback"
    assert len(config.workers) == 1
    assert set(config.workers[0].task_types) == {"bootstrap", "decide", "execute", "strike"}


@pytest.mark.parametrize("url", [
    "https://api.deepseek.com/v1",
    "http://localhost:11434/v1",  # DNS names are intentionally rejected
    "http://127.0.0.1.evil.example/v1",
    "http://[::ffff:8.8.8.8]:11434/v1",
    "file:///tmp/model",
])
def test_offline_policy_rejects_non_numeric_or_external_endpoint(url: str) -> None:
    payload = _profile()
    payload["workers"][0]["env"]["PI_BASE_URL"] = url
    with pytest.raises(ValidationError, match="offline_model_policy"):
        DispatchConfig.model_validate(payload)


def test_private_lan_requires_explicit_policy_and_allows_rfc1918() -> None:
    payload = _profile()
    payload["workers"][0]["env"]["PI_BASE_URL"] = "http://192.168.5.7:8000/v1"
    with pytest.raises(ValidationError, match="offline_model_policy"):
        DispatchConfig.model_validate(payload)
    payload["runtime"]["offline_model_policy"] = "private_lan"
    assert DispatchConfig.model_validate(payload).runtime.offline_model_policy == "private_lan"


def test_offline_policy_checks_every_worker() -> None:
    payload = _profile()
    second = payload["workers"][0].copy()
    second["name"] = "pi-decide"
    second["env"] = {**second["env"], "PI_BASE_URL": "https://api.deepseek.com/v1"}
    payload["workers"].append(second)
    with pytest.raises(ValidationError, match="pi-decide PI_BASE_URL"):
        DispatchConfig.model_validate(payload)


@pytest.mark.parametrize("key", ["ASTRA_EMBED_API_KEY", "ASTRA_EMBED_MODEL", "ASTRA_LLM_API_KEY"])
def test_offline_profile_rejects_auxiliary_model_route(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setenv(key, "enabled")
    with pytest.raises(ValueError, match=key):
        DispatchConfig.load(ROOT / "dispatch.secsophon.example.yaml")
