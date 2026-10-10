"""CLI contract for the opt-in, bounded static JavaScript analysis pilot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import astra  # noqa: E402
from astra.cli import main  # noqa: E402


@pytest.fixture
def analyzer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """Replace only the analyzer boundary; exercise Click's real argument parsing."""
    module = ModuleType("astra.static_js")

    class StaticJsError(RuntimeError):
        pass

    module.StaticJsError = StaticJsError
    module.MAX_SUMMARY_BYTES = 12 * 1024 - 1
    module.analyze = Mock(return_value={"status": "candidate_only", "label": "候选关系"})
    module.inspect = Mock(return_value={"status": "candidate_only", "file": "src/app.js"})
    monkeypatch.setitem(sys.modules, "astra.static_js", module)
    monkeypatch.setattr(astra, "static_js", module, raising=False)
    monkeypatch.setenv("ASTRA_STATIC_JS_ENABLED", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").mkdir()
    return module


def _json_output(result) -> dict:
    assert result.exit_code == 0, result.output
    assert len(result.output.encode("utf-8")) <= 12 * 1024
    parsed = json.loads(result.output)
    assert result.output == json.dumps(
        parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ) + "\n"
    return parsed


def test_analysis_forwards_directory_and_emits_compact_bounded_json(analyzer: ModuleType, tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["static-js", "source"])

    assert _json_output(result) == {"status": "candidate_only", "label": "候选关系"}
    analyzer.analyze.assert_called_once_with("source", workspace=tmp_path)
    analyzer.inspect.assert_not_called()


def test_inspect_uses_defaults_and_explicit_view_pagination(analyzer: ModuleType, tmp_path: Path) -> None:
    args = ["static-js", "--run-id", "a" * 32, "--file", "src/app.js"]
    result = CliRunner().invoke(main, args)

    assert _json_output(result)["file"] == "src/app.js"
    analyzer.inspect.assert_called_once_with(
        "a" * 32, "src/app.js", workspace=tmp_path, view="application", offset=0, limit=10
    )
    analyzer.analyze.assert_not_called()

    analyzer.inspect.reset_mock()
    result = CliRunner().invoke(
        main, args + ["--view", "semantic", "--offset", "20", "--limit", "40"]
    )
    assert _json_output(result)["status"] == "candidate_only"
    analyzer.inspect.assert_called_once_with(
        "a" * 32, "src/app.js", workspace=tmp_path, view="semantic", offset=20, limit=40
    )


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--run-id", "a" * 32],
        ["--file", "src/app.js"],
        ["source", "--run-id", "a" * 32, "--file", "src/app.js"],
        ["--run-id", "a" * 32, "--file", "src/app.js", "--view", "other"],
        ["--run-id", "a" * 32, "--file", "src/app.js", "--offset", "-1"],
        ["--run-id", "a" * 32, "--file", "src/app.js", "--limit", "0"],
        ["--run-id", "a" * 32, "--file", "src/app.js", "--limit", "41"],
    ],
)
def test_invalid_mode_or_pagination_fails_before_analyzer_call(analyzer: ModuleType, args: list[str]) -> None:
    result = CliRunner().invoke(main, ["static-js", *args])

    assert result.exit_code != 0
    assert "No such command" not in result.output
    assert "Traceback" not in result.output
    analyzer.analyze.assert_not_called()
    analyzer.inspect.assert_not_called()


@pytest.mark.parametrize("enabled", [None, "0", "true"])
def test_feature_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: str | None) -> None:
    if enabled is None:
        monkeypatch.delenv("ASTRA_STATIC_JS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ASTRA_STATIC_JS_ENABLED", enabled)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["static-js", "."])

    assert result.exit_code != 0
    assert "disabled" in result.output.lower()
    assert "Traceback" not in result.output


@pytest.mark.parametrize("mode", ["analyze", "inspect"])
def test_analyzer_errors_become_click_errors(analyzer: ModuleType, mode: str) -> None:
    args = ["static-js", "source"]
    method = analyzer.analyze
    if mode == "inspect":
        args = ["static-js", "--run-id", "a" * 32, "--file", "src/app.js"]
        method = analyzer.inspect
    method.side_effect = analyzer.StaticJsError("saved evidence is unavailable")

    result = CliRunner().invoke(main, args)

    assert result.exit_code != 0
    assert "Error: saved evidence is unavailable" in result.output
    assert "Traceback" not in result.output


def test_cli_rejects_oversized_result_even_if_analyzer_returns_it(analyzer: ModuleType) -> None:
    analyzer.analyze.return_value = {"large": "中" * 5000}

    result = CliRunner().invoke(main, ["static-js", "source"])

    assert result.exit_code != 0
    assert "limit" in result.output.lower()
    assert "中" * 100 not in result.output
    assert "Traceback" not in result.output


def test_output_limit_includes_terminating_newline(analyzer: ModuleType) -> None:
    empty_size = len(json.dumps({"large": ""}, separators=(",", ":")).encode("utf-8"))
    analyzer.analyze.return_value = {"large": "x" * (12 * 1024 - empty_size - 1)}

    accepted = CliRunner().invoke(main, ["static-js", "source"])
    _json_output(accepted)
    assert len(accepted.output.encode("utf-8")) == 12 * 1024

    analyzer.analyze.return_value["large"] += "x"
    rejected = CliRunner().invoke(main, ["static-js", "source"])
    assert rejected.exit_code != 0
    assert "limit" in rejected.output.lower()
    assert "x" * 100 not in rejected.output


def test_nonfinite_json_is_a_user_facing_error(analyzer: ModuleType) -> None:
    analyzer.analyze.return_value = {"duration_ms": float("nan")}
    result = CliRunner().invoke(main, ["static-js", "source"])

    assert result.exit_code != 0
    assert "Error:" in result.output
    assert "Traceback" not in result.output


def test_import_and_help_do_not_load_analyzer(tmp_path: Path) -> None:
    source_dir = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_dir) + os.pathsep + env.get("PYTHONPATH", "")
    script = """
import sys
from click.testing import CliRunner
from astra.cli import main
assert 'astra.static_js' not in sys.modules
for args in (['--help'], ['static-js', '--help']):
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert 'astra.static_js' not in sys.modules
"""

    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
