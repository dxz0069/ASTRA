"""Opt-in, bounded static analysis of a local JavaScript application directory.

The external analyzer is installed separately. Its output is only a candidate
source map; nothing here updates the graph or verifies a finding.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from astra.analysis_process import AnalysisProcessError, run_bounded

MAX_FILES = 500
MAX_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 16
MAX_ENTRIES = 2000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_SUMMARY_BYTES = 12 * 1024
MAX_ANALYZER_ITEMS = 100_000
TIMEOUT_SECONDS = 30
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_REPARSE_POINT = 0x400


class StaticJsError(RuntimeError):
    """A disabled, unsafe, or unsuccessful local analysis."""


def _enabled() -> None:
    if os.environ.get("ASTRA_STATIC_JS_ENABLED") != "1":
        raise StaticJsError("static JS analysis is disabled; set ASTRA_STATIC_JS_ENABLED=1")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _checked_source(raw: str, workspace: Path) -> Path:
    try:
        if ".." in Path(raw).parts:
            raise StaticJsError("input path must not contain traversal")
        if _unsafe_entry(workspace.lstat()):
            raise StaticJsError("workspace must not be a reparse point")
        root = workspace.resolve(strict=True)
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        if not _within(path, root):
            raise StaticJsError("input must be inside the current workspace")
        chain = [root]
        for part in path.relative_to(root).parts:
            chain.append(chain[-1] / part)
        for item in chain:
            # Windows has no O_NOFOLLOW equivalent for the full tree walk.
            metadata = item.lstat()
            if _unsafe_entry(metadata):
                raise StaticJsError("symbolic links and reparse points are unsupported")
        resolved = path.resolve(strict=True)
        if not path.is_dir() or not _within(resolved, root.resolve(strict=True)):
            raise StaticJsError("input must be a workspace directory")
        if resolved.name.lower().endswith((".asar", ".asar.unpacked")):
            raise StaticJsError("ASAR archives and companions are outside this pilot")
        return resolved
    except StaticJsError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise StaticJsError("workspace or input path is unavailable") from exc


def _unsafe_entry(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _copy_file(source: Path, destination: Path, expected: os.stat_result) -> str:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as reader, destination.open("xb") as writer:
        opened = os.fstat(reader.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
            raise StaticJsError("input changed while staging")
        while chunk := reader.read(64 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise StaticJsError("input file exceeds the size limit")
            digest.update(chunk)
            writer.write(chunk)
        closed = os.fstat(reader.fileno())
    after = source.lstat()
    if (
        size != expected.st_size
        or _unsafe_entry(after)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns)
        or (closed.st_size, closed.st_mtime_ns) != (expected.st_size, expected.st_mtime_ns)
    ):
        raise StaticJsError("input changed while staging")
    return digest.hexdigest()


def _stage_tree(source: Path, destination: Path, workspace_root: Path) -> list[dict[str, Any]]:
    original = source
    before = original.lstat()
    if _unsafe_entry(before):
        raise StaticJsError("input directory changed while staging")
    source = original.resolve(strict=True)
    after = original.lstat()
    if (
        _unsafe_entry(after)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or not _within(source, workspace_root)
    ):
        raise StaticJsError("input directory changed while staging")
    manifest: list[dict[str, Any]] = []
    byte_count = 0
    entry_count = 0
    stack = [(source, destination, 0, source.lstat())]
    while stack:
        current, target, depth, expected_dir = stack.pop()
        if depth > MAX_DEPTH:
            raise StaticJsError("input directory exceeds the depth limit")
        try:
            actual = current.lstat()
            resolved_current = current.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise StaticJsError("input directory changed while staging") from exc
        if _unsafe_entry(actual) or (actual.st_dev, actual.st_ino) != (expected_dir.st_dev, expected_dir.st_ino) or not _within(resolved_current, source):
            raise StaticJsError("input directory changed while staging")
        target.mkdir()
        try:
            with os.scandir(current) as listing:
                entries = []
                for entry in listing:
                    entry_count += 1
                    if entry_count > MAX_ENTRIES:
                        raise StaticJsError("input exceeds the directory entry limit")
                    entries.append(entry)
        except OSError as exc:
            raise StaticJsError("input directory is unavailable") from exc
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            try:
                # DirEntry.stat on Windows can report (st_dev, st_ino)=(0, 0),
                # which cannot be compared with fstat/lstat for identity.
                metadata = path.lstat()
            except OSError as exc:
                raise StaticJsError("input changed while staging") from exc
            if _unsafe_entry(metadata):
                raise StaticJsError("symbolic links and reparse points are unsupported")
            if path.name.lower().endswith((".asar", ".asar.unpacked")):
                raise StaticJsError("ASAR archives and companions are outside this pilot")
            relative = path.relative_to(source)
            staged = target / entry.name
            if stat.S_ISDIR(metadata.st_mode):
                stack.append((path, staged, depth + 1, metadata))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise StaticJsError("special files are unsupported")
            if metadata.st_size > MAX_FILE_BYTES:
                raise StaticJsError("input file exceeds the size limit")
            byte_count += metadata.st_size
            if byte_count > MAX_BYTES or len(manifest) >= MAX_FILES:
                raise StaticJsError("input exceeds the file or byte limit")
            digest = _copy_file(path, staged, metadata)
            manifest.append({"path": relative.as_posix(), "bytes": metadata.st_size, "sha256": digest})
    return sorted(manifest, key=lambda file: file["path"])


def _store_root() -> Path:
    try:
        root = Path(tempfile.gettempdir()) / "astra-static-js"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = root.lstat()
        if _unsafe_entry(metadata):
            raise StaticJsError("analysis store must not be a reparse point")
        if os.name != "nt" and (
            stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_uid != os.getuid()
        ):
            raise StaticJsError("analysis store must be private to this user")
        return root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StaticJsError("analysis store is unavailable") from exc


def _relative_file(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value or ":" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in (".", "..", "") for part in value.split("/"))


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StaticJsError(f"analyzer returned invalid {label}")
    return value


def _items(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > MAX_ANALYZER_ITEMS:
        raise StaticJsError(f"analyzer returned invalid {label}")
    return value


def _text_field(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise StaticJsError(f"analyzer returned invalid {label}")
    return value


def _validate_evidence(value: Any, label: str, contexts: set[str] | None = None) -> None:
    evidence = _object(value, label)
    if contexts is None:
        allowed = {
            "state": {"observed", "inferred", "unknown", "unavailable"},
            "authority": {"artifact-bytes", "ast-static-analysis", "static-relationship-inference", "shipped-artifact", "unknown"},
            "confidence": {"exact", "high", "medium", "low", "unknown"},
        }
        for field, choices in allowed.items():
            if evidence.get(field) not in choices:
                raise StaticJsError(f"analyzer returned invalid {label}.{field}")
        artifact = _object(evidence.get("artifact"), f"{label}.artifact")
        if artifact.get("available") is not False and not _sha256(artifact.get("sha256")):
            raise StaticJsError("analyzer returned invalid artifact digest")
    elif _text_field(evidence.get("context_id"), f"{label}.context_id") not in contexts:
        raise StaticJsError("analyzer evidence context is missing")
    location = evidence.get("location")
    if location is not None:
        loc = _object(location, f"{label}.location")
        if not isinstance(loc.get("available"), bool):
            raise StaticJsError("analyzer returned invalid location")
        if loc["available"]:
            val = _object(loc.get("value"), f"{label}.location.value")
            path = val.get("source", val.get("path"))
            if path is not None and (not isinstance(path, str) or len(path) > 4096 or not _relative_file(path)):
                raise StaticJsError("analyzer returned invalid source location")


def _validate_envelope(evidence: Any, stage: Path, *, saved: bool = False) -> dict[str, Any]:
    envelope = _object(evidence, "evidence envelope")
    if envelope.get("operation") != "analyze_javascript_application":
        raise StaticJsError("static analyzer returned the wrong operation")
    _text_field(envelope.get("evidence_id"), "evidence_id")
    provider = _object(envelope.get("provider"), "provider")
    if provider.get("id") != "rea-javascript-application":
        raise StaticJsError("analyzer provider is unsupported")
    _text_field(provider.get("version"), "provider.version")
    subject = _object(envelope.get("subject"), "subject")
    normalized = _object(envelope.get("normalized_result"), "normalized_result")
    if subject.get("format") != "directory" or normalized.get("format") != "directory":
        raise StaticJsError("analyzer output is not a directory analysis")
    paths = (subject.get("local_path"), normalized.get("input_path"))
    try:
        same_input = all(
            isinstance(value, str) and Path(value).is_absolute()
            and (value == str(stage) if saved else Path(value).samefile(stage))
            for value in paths
        )
    except (OSError, ValueError):
        same_input = False
    if not same_input:
        raise StaticJsError("analyzer output refers to a different input")
    digest = _object(subject.get("digest"), "subject.digest").get("sha256")
    if not _sha256(digest) or normalized.get("root_artifact_sha256") != digest:
        raise StaticJsError("analyzer root digest is invalid")
    graph = _object(normalized.get("graph"), "application graph")
    semantic = _object(normalized.get("semantic_graph"), "semantic graph")
    if semantic.get("root_artifact_sha256") != digest:
        raise StaticJsError("analyzer graph digest mismatch")
    for name, data in (("application", graph), ("semantic", semantic)):
        _coverage(data.get("coverage"))
        if not isinstance(data.get("schema"), str):
            raise StaticJsError(f"analyzer {name} graph has no schema")
    nodes = _items(graph.get("nodes"), "application nodes")
    edges = _items(graph.get("edges"), "application edges")
    for node in nodes:
        n = _object(node, "application node")
        _text_field(n.get("node_id"), "application node id")
        _text_field(n.get("kind"), "application node kind")
        for observation in _items(n.get("observations"), "observations"):
            o = _object(observation, "observation")
            _validate_evidence(o.get("evidence"), "observation evidence")
            if not isinstance(o.get("properties"), dict):
                raise StaticJsError("analyzer returned invalid observation properties")
    for edge in edges:
        e = _object(edge, "application edge")
        for key in ("source_node_id", "target_node_id", "relation"):
            _text_field(e.get(key), f"application edge {key}")
        _validate_evidence(e.get("evidence"), "edge evidence")
        if not isinstance(e.get("properties"), dict):
            raise StaticJsError("analyzer returned invalid edge properties")
    contexts: set[str] = set()
    for item in _items(semantic.get("evidence_contexts"), "evidence contexts"):
        context = _object(item, "evidence context")
        context_id = _text_field(context.get("context_id"), "context id")
        if context_id in contexts:
            raise StaticJsError("analyzer returned duplicate context id")
        contexts.add(context_id)
        _validate_evidence(context, "evidence context")
        _coverage(context.get("coverage"))
    for key in ("nodes", "relations", "unknowns"):
        for item in _items(semantic.get(key), f"semantic {key}"):
            obj = _object(item, f"semantic {key} item")
            _validate_evidence(obj.get("evidence"), f"semantic {key} evidence", contexts)
            if key == "unknowns":
                _text_field(obj.get("reason"), "unknown reason")
            elif key == "relations":
                _text_field(obj.get("relation"), "semantic relation")
            else:
                _text_field(obj.get("kind"), "semantic node kind")
            if key != "unknowns" and not isinstance(obj.get("properties"), dict):
                raise StaticJsError("analyzer returned invalid semantic properties")
    for field in ("statistics", "summary"):
        _object(normalized.get(field), field)
    _items(normalized.get("limitations"), "limitations")
    return envelope


def _location(evidence: Any) -> dict[str, Any] | None:
    if not isinstance(evidence, dict):
        return None
    value = evidence.get("location", {}).get("value") if isinstance(evidence.get("location"), dict) else None
    if not isinstance(value, dict):
        return None
    source = value.get("source", value.get("path"))
    if not isinstance(source, str):
        return None
    line = value.get("start", {}).get("line") if isinstance(value.get("start"), dict) else None
    return {"file": source, "line": line if isinstance(line, int) else None}


def _safe_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value if isinstance(value, (bool, int, float)) else None
    if "://" in value:
        try:
            parsed = urlsplit(value)
            host = parsed.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:120]
        except ValueError:
            return "[redacted URL]"
    if re.search(r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|bearer)", value):
        return "[redacted]"
    return value[:120]


def _small_properties(properties: Any) -> dict[str, Any]:
    if not isinstance(properties, dict):
        return {}
    keys = ("side", "operation", "channel", "resolution", "method", "route", "member", "specifier", "url", "presence", "value_status")
    return {key: _safe_value(properties[key]) for key in keys if key in properties}


def _counts(value: Any, depth: int = 0) -> Any:
    """Only aggregate counts cross into the default agent context."""
    if isinstance(value, dict) and depth < 3:
        return {
            key[:80]: _counts(item, depth + 1)
            for key, item in list(value.items())[:40]
            if isinstance(key, str) and _safe_value(key) == key[:120]
        }
    return value if isinstance(value, (int, float, bool)) else None


def _coverage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in ("complete", "partial", "unknown", "unavailable") or not isinstance(value.get("truncated"), bool):
        raise StaticJsError("analyzer omitted explicit coverage")
    for key in ("omitted_count", "omitted_nodes", "omitted_relations"):
        item = value.get(key)
        if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item < 0):
            raise StaticJsError("analyzer returned invalid coverage counts")
    limits = _items(value.get("limits"), "coverage limits")
    families = value.get("families", [])
    _items(families, "coverage families")
    for family in families:
        f = _object(family, "coverage family")
        _text_field(f.get("family"), "coverage family name")
        if f.get("status") not in ("complete", "partial", "unknown", "unavailable"):
            raise StaticJsError("analyzer returned invalid family coverage")
    result = {key: value[key] for key in ("status", "truncated", "omitted_count", "omitted_nodes", "omitted_relations") if key in value}
    result["limits"] = [_safe_value(item) for item in limits[:8]]
    result["limits_omitted"] = len(limits) - min(len(limits), 8)
    return result


def _bound(result: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    for key in keys:
        result[key + "_omitted"] = result.get(key + "_total", len(result[key])) - len(result[key])
    for key in keys:
        while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) >= MAX_SUMMARY_BYTES and result[key]:
            result[key].pop()
            result[key + "_omitted"] += 1
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) >= MAX_SUMMARY_BYTES:
        raise StaticJsError("analysis view exceeds the output limit")
    return result


def _summarize(evidence: dict[str, Any], run_id: str, source: Path, manifest: list[dict[str, Any]], metrics: Any) -> dict[str, Any]:
    if not isinstance(evidence.get("normalized_result"), dict):
        raise StaticJsError("analyzer returned an invalid evidence envelope")
    normalized = evidence["normalized_result"]
    graph = normalized.get("graph")
    semantic = normalized.get("semantic_graph")
    if not isinstance(graph, dict) or not isinstance(semantic, dict):
        raise StaticJsError("analyzer omitted application or semantic graph")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise StaticJsError("analyzer graph is malformed")
    unknowns = semantic.get("unknowns")
    if not isinstance(unknowns, list):
        raise StaticJsError("analyzer omitted semantic unknowns")
    graph_unknowns = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("kind") != "unknown":
            continue
        for observation in node.get("observations", []):
            if not isinstance(observation, dict):
                continue
            graph_unknowns.append({
                "operation": _safe_value(observation.get("properties", {}).get("operation")),
                "location": _location(observation.get("evidence")),
            })
    relation_counts = Counter(_safe_value(edge["relation"]) for edge in edges)
    candidates = []
    lookup = {node.get("node_id"): node for node in nodes if isinstance(node, dict)}
    def endpoint(node_id: str) -> dict[str, Any]:
        node = lookup.get(node_id, {})
        observations = node.get("observations", [])
        label = observations[0].get("label") if observations and isinstance(observations[0], dict) else None
        return {"kind": _safe_value(node.get("kind")), "label": _safe_value(label)}
    for edge in sorted((edge for edge in edges if isinstance(edge, dict)), key=lambda edge: (edge.get("relation") in ("contains", "imports"), edge.get("relation", ""))):
        ev = edge.get("evidence")
        if not isinstance(ev, dict):
            continue
        candidates.append({
            "relation": _safe_value(edge.get("relation")),
            "from": endpoint(edge.get("source_node_id")),
            "to": endpoint(edge.get("target_node_id")),
            "state": ev.get("state"),
            "authority": ev.get("authority"),
            "confidence": ev.get("confidence"),
            "artifact_sha256": ev.get("artifact", {}).get("sha256"),
            "location": _location(ev),
            "properties": _small_properties(edge.get("properties")),
        })
        if len(candidates) == 12:
            break
    semantic_unknown_counts = Counter(_safe_value(item["reason"]) for item in unknowns)
    summary = {
        "status": "candidate_only",
        "run_id": run_id,
        "source_dir": str(source),
        "source_files": len(manifest),
        "source_bytes": sum(file["bytes"] for file in manifest),
        "evidence_id": _safe_value(evidence.get("evidence_id")),
        "artifact_sha256": normalized.get("root_artifact_sha256"),
        "duration_ms": metrics.elapsed_ms,
        "stdout_bytes": metrics.stdout_bytes,
        "statistics": _counts(normalized.get("statistics", {})),
        "summary": _counts(normalized.get("summary", {})),
        "application_coverage": _coverage(graph.get("coverage")),
        "semantic_coverage": _coverage(semantic.get("coverage")),
        "semantic_family_status": {_safe_value(item["family"]): item["status"] for item in semantic["coverage"].get("families", [])[:24]},
        "unknown_operations": graph_unknowns[:12],
        "unknown_operations_total": len(graph_unknowns),
        "semantic_unknowns_total": len(unknowns),
        "semantic_unknown_reasons": dict(semantic_unknown_counts.most_common(10)),
        "relation_counts": dict(relation_counts.most_common(24)),
        "candidate_relations": candidates,
        "candidate_relations_total": len(edges),
        "limitations": [_safe_value(item) for item in normalized["limitations"][:8]],
        "limitations_total": len(normalized.get("limitations", [])),
        "warning": "Static candidates only; dynamic execution and security impact are unverified. A complete application inventory does not mean complete semantic coverage.",
    }
    return summary


def analyze(source_arg: str, *, workspace: Path | None = None) -> dict[str, Any]:
    _enabled()
    current = workspace or Path.cwd()
    source = _checked_source(source_arg, current)
    workspace_root = current.resolve(strict=True)
    cli_arg = os.environ.get("ASTRA_STATIC_JS_CLI", "")
    cli = Path(cli_arg)
    if not cli.is_absolute() or not cli.is_file() or cli.suffix.lower() not in (".mjs", ".js"):
        raise StaticJsError("ASTRA_STATIC_JS_CLI must name an installed absolute JS entrypoint")
    node = shutil.which("node")
    if not node:
        raise StaticJsError("Node.js is unavailable")
    run_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="astra-static-js-stage-") as temp:
        temp_path = Path(temp)
        if _within(temp_path, source):
            raise StaticJsError("input must not contain the analysis staging directory")
        stage = temp_path / "input"
        try:
            manifest = _stage_tree(source, stage, workspace_root)
        except OSError as exc:
            raise StaticJsError("input or staging directory is unavailable") from exc
        stdout_path = temp_path / "evidence.json"
        stderr_path = temp_path / "stderr.txt"
        try:
            metrics = run_bounded(
                [node, "--max-old-space-size=512", str(cli), "analyze-javascript-application", str(stage), "--json"],
                cwd=temp_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=TIMEOUT_SECONDS,
                stdout_limit=MAX_OUTPUT_BYTES,
                stderr_limit=MAX_STDERR_BYTES,
            )
        except AnalysisProcessError as exc:
            raise StaticJsError(f"static analyzer failed: {exc}") from exc
        if metrics.returncode != 0:
            raise StaticJsError(f"static analyzer exited with code {metrics.returncode}")
        try:
            raw = stdout_path.read_bytes()
            evidence = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            json.dumps(evidence, allow_nan=False)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError, OverflowError) as exc:
            raise StaticJsError("static analyzer returned invalid JSON") from exc
        _validate_envelope(evidence, stage)
        summary = _summarize(evidence, run_id, source, manifest, metrics)
        store = _store_root()
        run_dir = store / run_id
        evidence_target = run_dir / "evidence.json"
        manifest_target = run_dir / "manifest.json"
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        summary["evidence_path"] = str(evidence_target)
        summary["manifest_path"] = str(manifest_target)
        summary["evidence_sha256"] = raw_sha256
        bounded = _bound(summary, ("candidate_relations", "limitations", "unknown_operations"))
        record = {
            "source_dir": str(source),
            "workspace_dir": str(workspace_root),
            "staged_input_path": evidence["normalized_result"]["input_path"],
            "files": manifest,
            "stage_deleted": True,
            "evidence_sha256": raw_sha256,
        }
        try:
            run_dir.mkdir(mode=0o700)
            evidence_target.write_bytes(raw)
            manifest_target.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            evidence_target.chmod(0o600)
            manifest_target.chmod(0o600)
        except (OSError, ValueError) as exc:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise StaticJsError("analysis evidence could not be saved") from exc
        return bounded


def _saved_run(run_id: str, workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = _store_root() / run_id
    manifest_path = run_dir / "manifest.json"
    evidence_path = run_dir / "evidence.json"
    try:
        if _unsafe_entry(run_dir.lstat()) or not run_dir.is_dir():
            raise StaticJsError("saved run is unavailable")
        for path, limit in ((manifest_path, 256 * 1024), (evidence_path, MAX_OUTPUT_BYTES)):
            metadata = path.lstat()
            if _unsafe_entry(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise StaticJsError("saved evidence is unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw = evidence_path.read_bytes()
        if not isinstance(manifest, dict) or not _sha256(manifest.get("evidence_sha256")):
            raise StaticJsError("saved manifest is invalid")
        if hashlib.sha256(raw).hexdigest() != manifest["evidence_sha256"]:
            raise StaticJsError("saved evidence hash mismatch")
        if manifest.get("workspace_dir") != str(workspace.resolve(strict=True)):
            raise StaticJsError("saved run belongs to another workspace")
        if manifest.get("stage_deleted") is not True:
            raise StaticJsError("saved manifest is invalid")
        source = manifest.get("source_dir")
        workspace_root = workspace.resolve(strict=True)
        if not isinstance(source, str) or not Path(source).is_absolute() or ".." in Path(source).parts:
            raise StaticJsError("saved manifest is invalid")
        try:
            resolved_source = Path(source).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise StaticJsError("saved manifest is invalid") from exc
        if not _within(resolved_source, workspace_root):
            raise StaticJsError("saved manifest is invalid")
        files = _items(manifest.get("files"), "saved file manifest")
        if not files or len(files) > MAX_FILES:
            raise StaticJsError("saved file manifest is invalid")
        seen: set[str] = set()
        for item in files:
            f = _object(item, "saved file record")
            name = f.get("path")
            if not isinstance(name, str) or not _relative_file(name) or name in seen:
                raise StaticJsError("saved file manifest is invalid")
            if not isinstance(f.get("bytes"), int) or f["bytes"] < 0 or not _sha256(f.get("sha256")):
                raise StaticJsError("saved file manifest is invalid")
            seen.add(name)
        stage = manifest.get("staged_input_path")
        if not isinstance(stage, str) or not Path(stage).is_absolute():
            raise StaticJsError("saved manifest is invalid")
        evidence = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        json.dumps(evidence, allow_nan=False)
        _validate_envelope(evidence, Path(stage), saved=True)
        return manifest, evidence
    except StaticJsError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError, RuntimeError, TypeError, OverflowError) as exc:
        raise StaticJsError("saved evidence is invalid or unavailable") from exc


def _view_item(item: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": _safe_value(item.get("kind")),
        "label": _safe_value(item.get("label")),
        "relation": _safe_value(item.get("relation")),
        "resolution": _safe_value(item.get("resolution")),
        "reason": _safe_value(item.get("reason")),
        "detail": _safe_value(item.get("detail")),
        "state": evidence.get("state"),
        "authority": evidence.get("authority"),
        "confidence": evidence.get("confidence"),
        "artifact_sha256": evidence.get("artifact", {}).get("sha256"),
        "evidence_coverage": _coverage(evidence.get("coverage")) if evidence.get("coverage") else None,
        "location": _location(item.get("evidence")),
        "properties": _small_properties(item.get("properties")),
    }


def inspect(
    run_id: str, file: str, *, workspace: Path | None = None,
    view: str = "application", offset: int = 0, limit: int = 10,
) -> dict[str, Any]:
    _enabled()
    if not _RUN_ID.fullmatch(run_id):
        raise StaticJsError("invalid run id")
    if not isinstance(file, str) or not _relative_file(file):
        raise StaticJsError("file must be a relative artifact path")
    if view not in ("application", "semantic") or not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 40:
        raise StaticJsError("invalid inspection view or pagination")
    manifest, evidence = _saved_run(run_id, workspace or Path.cwd())
    if file not in {item["path"] for item in manifest["files"]}:
        raise StaticJsError("file is not in the saved manifest")
    normalized = evidence["normalized_result"]
    graph = normalized["graph"]
    semantic = normalized["semantic_graph"]
    groups: dict[str, list[dict[str, Any]]] = {}
    if view == "application":
        observations: list[dict[str, Any]] = []
        for node in graph["nodes"]:
            for obs in node["observations"]:
                if (location := _location(obs["evidence"])) and location["file"] == file:
                    observations.append(_view_item({"kind": node["kind"], **obs}, obs["evidence"]))
        relations = []
        for edge in graph["edges"]:
            if (location := _location(edge["evidence"])) and location["file"] == file:
                relations.append(_view_item(edge, edge["evidence"]))
        groups = {"observations": observations, "relations": relations}
    else:
        contexts = {item["context_id"]: item for item in semantic["evidence_contexts"]}
        for key in ("nodes", "relations", "unknowns"):
            selected = []
            for item in semantic[key]:
                if (location := _location(item["evidence"])) and location["file"] == file:
                    selected.append(_view_item(item, contexts[item["evidence"]["context_id"]]))
            groups[key] = selected
    maximum = max((len(items) for items in groups.values()), default=0)
    page_count = min(limit, max(0, maximum - offset))
    while True:
        result: dict[str, Any] = {
            "status": "candidate_only", "run_id": run_id, "file": file, "view": view,
            "offset": offset, "page_size": page_count,
            "next_offset": offset + page_count if offset + page_count < maximum else None,
            "application_coverage": _coverage(graph["coverage"]),
            "semantic_coverage": _coverage(semantic["coverage"]),
            "warning": "Static candidates only; verify source and impact independently.",
        }
        for key, items in groups.items():
            result[key + "_total"] = len(items)
            result[key] = items[offset:offset + page_count]
            result[key + "_omitted"] = len(items) - len(result[key])
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= MAX_SUMMARY_BYTES - 1:
            return result
        if page_count == 0:
            raise StaticJsError("inspection exceeds the output limit")
        page_count -= 1
