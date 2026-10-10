"""The static JavaScript pilot's trust boundary and saved-evidence contract."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astra import static_js  # noqa: E402
from astra.analysis_process import AnalysisProcessError, ProcessMetrics  # noqa: E402


ROOT_DIGEST = "a" * 64
FILE = "src/main.js"


def _location(file: str = FILE, line: int = 4) -> dict[str, Any]:
    return {
        "available": True,
        "value": {"kind": "source-range", "source": file, "start": {"line": line, "column": 1}},
    }


def _app_evidence(file: str = FILE, line: int = 4) -> dict[str, Any]:
    return {
        "state": "observed",
        "authority": "artifact-bytes",
        "confidence": "exact",
        "artifact": {"available": True, "sha256": "b" * 64},
        "location": _location(file, line),
        "coverage": {"status": "complete", "truncated": False, "omitted_count": 0, "limits": []},
    }


def _evidence(stage: Path) -> dict[str, Any]:
    app_nodes = [
        {
            "node_id": "app-1",
            "kind": "module",
            "observations": [{
                "label": "https://alice:pass@example.com/api?token=QUERY-SECRET#fragment",
                "properties": {"url": "https://alice:pass@example.com/api?token=QUERY-SECRET"},
                "evidence": _app_evidence(line=4),
            }],
        },
        {
            "node_id": "app-2",
            "kind": "unknown",
            "observations": [{
                "label": "Bearer TOKEN-123",
                "properties": {"operation": "parse-javascript", "specifier": "Bearer TOKEN-123"},
                "evidence": _app_evidence(line=5),
            }],
        },
    ]
    app_edges = [
        {
            "source_node_id": "app-1",
            "target_node_id": "app-2",
            "relation": f"calls-{index}",
            "properties": {"url": "https://alice:pass@example.com/api?token=QUERY-SECRET"},
            "evidence": _app_evidence(line=10 + index),
        }
        for index in range(3)
    ]
    semantic_nodes = [
        {
            "node_id": f"sem-{index}",
            "kind": "call",
            "label": f"call-{index}",
            "properties": {"operation": f"call-{index}"},
            "evidence": {"context_id": "ctx-1", "location": _location(line=20 + index)},
        }
        for index in range(3)
    ]
    semantic_relations = [
        {
            "relation_id": f"relation-{index}",
            "source_node_id": "sem-0",
            "target_node_id": "sem-1",
            "relation": f"calls-{index}",
            "resolution": "candidate",
            "properties": {"channel": f"channel-{index}"},
            "evidence": {"context_id": "ctx-1", "location": _location(line=30 + index)},
        }
        for index in range(3)
    ]
    semantic_unknowns = [
        {
            "unknown_id": f"unknown-{index}",
            "node_id": "sem-0",
            "family": "call-flow",
            "reason": "dynamic-call",
            "detail": f"unresolved-{index}",
            "evidence": {"context_id": "ctx-1", "location": _location(line=40 + index)},
        }
        for index in range(3)
    ]
    return {
        "evidence_id": "ev-test",
        "provider": {"id": "rea-javascript-application", "name": "REA", "version": "1"},
        "subject": {
            "name": str(stage),
            "format": "directory",
            "local_path": str(stage),
            "digest": {"sha256": ROOT_DIGEST},
        },
        "operation": "analyze_javascript_application",
        "normalized_result": {
            "input_path": str(stage),
            "format": "directory",
            "root_artifact_sha256": ROOT_DIGEST,
            "graph": {
                "schema": "JavaScriptApplicationGraph",
                "nodes": app_nodes,
                "edges": app_edges,
                "coverage": {"status": "partial", "truncated": False, "omitted_count": 1, "limits": []},
            },
            "semantic_graph": {
                "schema": "JavaScriptSemanticRelationGraph",
                "root_artifact_sha256": ROOT_DIGEST,
                "evidence_contexts": [{
                    "context_id": "ctx-1",
                    "state": "inferred",
                    "authority": "shipped-artifact",
                    "confidence": "medium",
                    "artifact": {"available": True, "sha256": "b" * 64},
                    "coverage": {"status": "partial", "truncated": False, "limits": []},
                }],
                "nodes": semantic_nodes,
                "relations": semantic_relations,
                "unknowns": semantic_unknowns,
                "coverage": {
                    "status": "unknown",
                    "truncated": False,
                    "omitted_nodes": 2,
                    "omitted_relations": 3,
                    "limits": [],
                    "families": [{"family": "call-flow", "status": "partial"}],
                },
            },
            "statistics": {"relevant_files": 1, "parsed_javascript_files": 1},
            "summary": {"input_files": 1},
            "limitations": ["Static relationships are candidates."],
        },
    }


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    workspace = tmp_path / "workspace"
    source = workspace / "app"
    (source / "src").mkdir(parents=True)
    (source / FILE).write_bytes(b"const x = 1;\n")
    cli = tmp_path / "rea.mjs"
    cli.write_text("// stand-in entrypoint\n", encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir()
    state: dict[str, Any] = {
        "workspace": workspace,
        "source": source,
        "store": store,
        "calls": [],
        "mutate": lambda evidence: None,
    }
    monkeypatch.setenv("ASTRA_STATIC_JS_ENABLED", "1")
    monkeypatch.setenv("ASTRA_STATIC_JS_CLI", str(cli))
    monkeypatch.setattr(static_js.shutil, "which", lambda name: str(tmp_path / "node") if name == "node" else None)
    monkeypatch.setattr(static_js, "_store_root", lambda: store)

    def fake_run(command: list[str], **kwargs: Any) -> ProcessMetrics:
        stage = Path(command[-2])
        assert command[:3] == [str(tmp_path / "node"), "--max-old-space-size=512", str(cli)]
        assert command[3] == "analyze-javascript-application"
        assert command[-1] == "--json"
        assert stage != source and (stage / FILE).read_text(encoding="utf-8") == "const x = 1;\n"
        assert kwargs["timeout"] == static_js.TIMEOUT_SECONDS
        assert kwargs["stdout_limit"] == static_js.MAX_OUTPUT_BYTES
        assert kwargs["stderr_limit"] == static_js.MAX_STDERR_BYTES
        state["calls"].append({"command": command, "stage": stage})
        evidence = _evidence(stage)
        state["mutate"](evidence)
        raw = json.dumps(evidence, ensure_ascii=False).encode("utf-8")
        kwargs["stdout_path"].write_bytes(raw)
        kwargs["stderr_path"].write_bytes(b"")
        return ProcessMetrics(0, 12, len(raw), 0)

    monkeypatch.setattr(static_js, "run_bounded", fake_run)
    return state


def _analyze(harness: dict[str, Any]) -> dict[str, Any]:
    return static_js.analyze("app", workspace=harness["workspace"])


def test_opt_in_is_required_before_analyzer_launch(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_STATIC_JS_ENABLED", "0")
    with pytest.raises(static_js.StaticJsError, match="disabled"):
        _analyze(harness)
    assert not harness["calls"]


@pytest.mark.parametrize("missing", ["cli", "node"])
def test_missing_cli_or_node_is_reported_before_staging(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    if missing == "cli":
        monkeypatch.delenv("ASTRA_STATIC_JS_CLI")
    else:
        monkeypatch.setattr(static_js.shutil, "which", lambda name: None)
    with pytest.raises(static_js.StaticJsError, match="ASTRA_STATIC_JS_CLI|Node.js"):
        _analyze(harness)
    assert not harness["calls"]


def test_analyzer_dependency_failure_does_not_leak_child_output(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise AnalysisProcessError("start_failed")

    monkeypatch.setattr(static_js, "run_bounded", fail)
    with pytest.raises(static_js.StaticJsError, match="start_failed") as exc:
        _analyze(harness)
    assert "TOKEN-123" not in str(exc.value)
    assert list(harness["store"].iterdir()) == []


@pytest.mark.parametrize("path", ["../outside", "app/../app", "C:/outside"])
def test_traversal_and_outside_input_are_rejected(harness: dict[str, Any], path: str) -> None:
    with pytest.raises(static_js.StaticJsError):
        static_js.analyze(path, workspace=harness["workspace"])
    assert not harness["calls"]


def test_staging_rechecks_source_that_resolves_outside_workspace(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "staged"
    source = harness["source"]
    workspace_root = harness["workspace"].resolve(strict=True)
    real_resolve = type(source).resolve

    def redirected_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        # Model a Windows junction retargeted after _checked_source succeeded.
        if path == source:
            return outside
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(source), "resolve", redirected_resolve)
    with pytest.raises(static_js.StaticJsError, match="changed while staging"):
        static_js._stage_tree(source, destination, workspace_root)
    assert not destination.exists()


def test_symbolic_link_in_input_is_rejected(harness: dict[str, Any], tmp_path: Path) -> None:
    link = harness["source"] / "src" / "linked.js"
    try:
        link.symlink_to(tmp_path / "outside.js")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(static_js.StaticJsError, match="symbolic links|reparse"):
        _analyze(harness)
    assert not harness["calls"]


@pytest.mark.parametrize("name", ["bundle.asar", "bundle.asar.unpacked"])
def test_asar_input_is_outside_the_pilot(harness: dict[str, Any], name: str) -> None:
    (harness["source"] / name).write_bytes(b"archive")
    with pytest.raises(static_js.StaticJsError, match="ASAR"):
        _analyze(harness)
    assert not harness["calls"]


@pytest.mark.parametrize(
    ("budget", "value", "message"),
    [
        ("MAX_FILE_BYTES", 1, "size limit"),
        ("MAX_BYTES", 1, "file or byte limit"),
        ("MAX_FILES", 0, "file or byte limit"),
        ("MAX_ENTRIES", 0, "directory entry limit"),
        ("MAX_DEPTH", 0, "depth limit"),
    ],
)
def test_input_budgets_stop_before_analyzer(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, budget: str, value: int, message: str
) -> None:
    monkeypatch.setattr(static_js, budget, value)
    with pytest.raises(static_js.StaticJsError, match=message):
        _analyze(harness)
    assert not harness["calls"]


def test_process_output_limit_is_not_saved(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def overflow(*args: Any, **kwargs: Any) -> None:
        raise AnalysisProcessError("stdout_limit")

    monkeypatch.setattr(static_js, "run_bounded", overflow)
    with pytest.raises(static_js.StaticJsError, match="stdout_limit"):
        _analyze(harness)
    assert list(harness["store"].iterdir()) == []


def test_nonfinite_json_number_is_rejected_before_saving(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = static_js.run_bounded

    def nonfinite_number(command: list[str], **kwargs: Any) -> ProcessMetrics:
        metrics = original_run(command, **kwargs)
        path = kwargs["stdout_path"]
        raw = path.read_bytes()
        changed = raw.replace(b'"relevant_files": 1', b'"relevant_files": 1e999', 1)
        assert changed != raw
        path.write_bytes(changed)
        return ProcessMetrics(0, metrics.elapsed_ms, len(changed), metrics.stderr_bytes)

    monkeypatch.setattr(static_js, "run_bounded", nonfinite_number)
    with pytest.raises(static_js.StaticJsError, match="invalid JSON"):
        _analyze(harness)
    assert list(harness["store"].iterdir()) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(operation="inspect_analysis_view"),
        lambda e: e.pop("normalized_result"),
        lambda e: e["normalized_result"]["graph"].pop("coverage"),
        lambda e: e["normalized_result"]["semantic_graph"].pop("coverage"),
        lambda e: e["normalized_result"]["semantic_graph"].pop("unknowns"),
        lambda e: e["normalized_result"]["semantic_graph"]["nodes"].append("not a node"),
        lambda e: e["normalized_result"]["semantic_graph"]["relations"][0]["evidence"].update(context_id="missing"),
        lambda e: e["subject"].update(local_path="outside"),
    ],
)
def test_invalid_rea_envelope_is_never_saved(harness: dict[str, Any], mutate: Any) -> None:
    harness["mutate"] = mutate
    with pytest.raises(static_js.StaticJsError):
        _analyze(harness)
    assert list(harness["store"].iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"), [("state", "verified"), ("authority", "self-reported")]
)
def test_invalid_evidence_state_or_authority_is_rejected(
    harness: dict[str, Any], field: str, value: str
) -> None:
    def mutate(evidence: dict[str, Any]) -> None:
        evidence["normalized_result"]["graph"]["nodes"][0]["observations"][0]["evidence"][field] = value

    harness["mutate"] = mutate
    with pytest.raises(static_js.StaticJsError, match=field):
        _analyze(harness)
    assert list(harness["store"].iterdir()) == []


def test_evidence_hash_manifest_coverage_unknowns_and_redaction(harness: dict[str, Any]) -> None:
    result = _analyze(harness)
    evidence_path = Path(result["evidence_path"])
    manifest_path = Path(result["manifest_path"])
    evidence_raw = evidence_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result["status"] == "candidate_only"
    assert result["source_dir"] == str(harness["source"])
    assert result["application_coverage"]["status"] == "partial"
    assert result["semantic_coverage"]["status"] == "unknown"
    assert result["semantic_coverage"]["omitted_nodes"] == 2
    assert result["semantic_family_status"]["call-flow"] == "partial"
    assert result["unknown_operations_total"] == 1
    assert result["unknown_operations"][0]["operation"] == "parse-javascript"
    assert result["semantic_unknown_reasons"] == {"dynamic-call": 3}
    assert evidence_path.parent == harness["store"] / result["run_id"]
    assert manifest_path.parent == evidence_path.parent
    assert result["evidence_sha256"] == manifest["evidence_sha256"] == hashlib.sha256(evidence_raw).hexdigest()
    assert manifest["source_dir"] == str(harness["source"])
    assert manifest["workspace_dir"] == str(harness["workspace"])
    assert manifest["stage_deleted"] is True
    assert not Path(manifest["staged_input_path"]).exists()
    assert manifest["files"] == [{
        "path": FILE,
        "bytes": len("const x = 1;\n".encode()),
        "sha256": hashlib.sha256(b"const x = 1;\n").hexdigest(),
    }]
    assert json.loads(evidence_raw)["normalized_result"]["semantic_graph"]["unknowns"][0]["reason"] == "dynamic-call"
    displayed = json.dumps(result, ensure_ascii=False)
    for private in ("alice", "pass", "QUERY-SECRET", "TOKEN-123"):
        assert private not in displayed
    assert "https://example.com/api" in displayed


def test_semantic_unknown_reason_with_token_is_redacted(harness: dict[str, Any]) -> None:
    def mutate(evidence: dict[str, Any]) -> None:
        evidence["normalized_result"]["semantic_graph"]["unknowns"][0]["reason"] = "token=TOP-SECRET"

    harness["mutate"] = mutate
    result = _analyze(harness)
    assert result["semantic_unknown_reasons"] == {"dynamic-call": 2, "[redacted]": 1}
    assert "TOP-SECRET" not in json.dumps(result)

    detail = static_js.inspect(
        result["run_id"], FILE, workspace=harness["workspace"], view="semantic"
    )
    assert detail["unknowns"][0]["reason"] == "[redacted]"
    assert "TOP-SECRET" not in json.dumps(detail)


def test_oversized_summary_is_rejected_before_evidence_is_saved(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(static_js, "MAX_SUMMARY_BYTES", 512)
    with pytest.raises(static_js.StaticJsError, match="limit"):
        _analyze(harness)
    assert list(harness["store"].iterdir()) == []


def test_inspect_application_and_semantic_views_paginate_without_loss(harness: dict[str, Any]) -> None:
    run_id = _analyze(harness)["run_id"]
    application = static_js.inspect(
        run_id, FILE, workspace=harness["workspace"], view="application", offset=1, limit=1
    )
    assert application["status"] == "candidate_only"
    assert application["relations_total"] == 3
    assert len(application["relations"]) == 1
    assert application["relations"][0]["relation"] == "calls-1"
    assert application["relations_omitted"] == 2
    assert application["next_offset"] == 2
    assert application["application_coverage"]["status"] == "partial"

    semantic = static_js.inspect(
        run_id, FILE, workspace=harness["workspace"], view="semantic", offset=1, limit=1
    )
    assert semantic["nodes_total"] == 3
    assert semantic["relations_total"] == 3
    assert semantic["unknowns_total"] == 3
    assert len(semantic["nodes"]) == len(semantic["relations"]) == len(semantic["unknowns"]) == 1
    assert semantic["unknowns"][0]["reason"] == "dynamic-call"
    assert semantic["unknowns_omitted"] == 2
    assert semantic["next_offset"] == 2
    assert semantic["semantic_coverage"]["status"] == "unknown"


def test_inspect_matches_full_relative_path_longer_than_240_characters(harness: dict[str, Any]) -> None:
    long_file = "src/" + "a" * 120 + "/" + "b" * 120 + "/main.js"
    assert len(long_file) > 240
    result = _analyze(harness)
    evidence_path = Path(result["evidence_path"])
    manifest_path = Path(result["manifest_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    normalized = evidence["normalized_result"]
    for node in normalized["graph"]["nodes"]:
        for observation in node["observations"]:
            observation["evidence"]["location"]["value"]["source"] = long_file
    for edge in normalized["graph"]["edges"]:
        edge["evidence"]["location"]["value"]["source"] = long_file
    for kind in ("nodes", "relations", "unknowns"):
        for item in normalized["semantic_graph"][kind]:
            item["evidence"]["location"]["value"]["source"] = long_file

    # Materialize a valid saved record without depending on Windows MAX_PATH.
    raw = json.dumps(evidence, ensure_ascii=False).encode("utf-8")
    evidence_path.write_bytes(raw)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({**manifest["files"][0], "path": long_file})
    manifest["evidence_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    application = static_js.inspect(result["run_id"], long_file, workspace=harness["workspace"])
    semantic = static_js.inspect(
        result["run_id"], long_file, workspace=harness["workspace"], view="semantic"
    )
    assert application["file"] == semantic["file"] == long_file
    assert application["observations_total"] == 2
    assert application["relations_total"] == 3
    assert semantic["nodes_total"] == semantic["relations_total"] == semantic["unknowns_total"] == 3
    assert application["observations"][0]["location"]["file"] == long_file
    assert semantic["unknowns"][0]["location"]["file"] == long_file


@pytest.mark.parametrize("file", ["../secret.js", "/absolute.js", ""])
def test_inspect_rejects_unsafe_file_paths(harness: dict[str, Any], file: str) -> None:
    run_id = _analyze(harness)["run_id"]
    with pytest.raises(static_js.StaticJsError):
        static_js.inspect(run_id, file, workspace=harness["workspace"])


def test_inspect_rejects_other_workspace_and_tampered_evidence(harness: dict[str, Any], tmp_path: Path) -> None:
    result = _analyze(harness)
    with pytest.raises(static_js.StaticJsError):
        static_js.inspect(result["run_id"], FILE, workspace=tmp_path)
    evidence_path = Path(result["evidence_path"])
    evidence_path.write_bytes(evidence_path.read_bytes() + b"\n")
    with pytest.raises(static_js.StaticJsError, match="hash|integrity|invalid"):
        static_js.inspect(result["run_id"], FILE, workspace=harness["workspace"])


def test_inspect_rejects_tampered_manifest_hash(harness: dict[str, Any]) -> None:
    result = _analyze(harness)
    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(static_js.StaticJsError, match="hash|integrity|invalid"):
        static_js.inspect(result["run_id"], FILE, workspace=harness["workspace"])


@pytest.mark.parametrize("run_id", ["../bad", "A" * 32, "0" * 31])
def test_inspect_rejects_invalid_run_id(harness: dict[str, Any], run_id: str) -> None:
    with pytest.raises(static_js.StaticJsError, match="run id"):
        static_js.inspect(run_id, FILE, workspace=harness["workspace"])
