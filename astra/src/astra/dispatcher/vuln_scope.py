"""Fail-closed configuration contract for the vulnerability-research Pi profile.

The Pi extension checks each request again at call time.  This validator keeps
malformed or missing scope from starting a worker at all.
"""

from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit


def validate_scope_json(raw: str, *, local_test: bool = False) -> dict:
    try:
        scope = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ASTRA_VULN_SCOPE_JSON must be valid JSON") from exc
    if not isinstance(scope, dict) or set(scope) != {"project_code", "not_before", "not_after", "targets"}:
        raise ValueError("scope must contain exactly project_code, not_before, not_after, targets")
    if scope["project_code"] != "PROJ2026_AISRC01":
        raise ValueError("scope project_code must match the vulnerability challenge")
    start = _timestamp(scope["not_before"])
    end = _timestamp(scope["not_after"])
    if start >= end:
        raise ValueError("scope time window must be increasing")
    if not local_test and (
        start < _timestamp("2026-10-11T16:00:00Z")
        or end > _timestamp("2026-10-26T15:59:59Z")
    ):
        raise ValueError("scope time window exceeds the published challenge window")
    if not isinstance(scope["targets"], list):
        raise ValueError("scope targets must be a list")
    if not local_test and not scope["targets"]:
        raise ValueError("production scope must include at least one explicitly approved target")
    for target in scope["targets"]:
        if not isinstance(target, dict) or set(target) != {"origin", "path_prefixes", "methods"}:
            raise ValueError("each target needs exactly origin, path_prefixes, methods")
        origin = target["origin"]
        if not isinstance(origin, str):
            raise ValueError("target origin must be a URL string")
        parsed = urlsplit(origin)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise ValueError("target origin must be an exact scheme://host[:port]")
        if parsed.geturl() != origin or parsed.netloc != parsed.netloc.lower() or parsed.hostname.endswith("."):
            raise ValueError("target origin must be canonical")
        is_loopback = False
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if local_test and not is_loopback:
            raise ValueError("local test scope may contain loopback targets only")
        if parsed.scheme != "https" and not (local_test and parsed.scheme == "http" and is_loopback):
            raise ValueError("target origin must use HTTPS (HTTP loopback only in local test mode)")
        if not local_test and is_loopback:
            raise ValueError("loopback target is restricted to local test mode")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("target origin has invalid port") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("target origin has invalid port")
        paths = target["path_prefixes"]
        if not isinstance(paths, list) or not paths or any(
            not isinstance(path, str) or not path.startswith("/")
            or "?" in path or "#" in path or "%" in path or "\\" in path
            or any(segment in {".", ".."} for segment in path.split("/"))
            for path in paths
        ):
            raise ValueError("path_prefixes must contain absolute path prefixes")
        methods = target["methods"]
        if not isinstance(methods, list) or not methods or any(method not in {"GET", "HEAD"} for method in methods):
            raise ValueError("first scoped_request slice supports GET and HEAD only")
    return scope


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("scope timestamps must be UTC ISO-8601 ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("invalid scope timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("scope timestamp must be UTC")
    return parsed
