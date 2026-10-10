"""Conservative identity for structured security findings.

Descriptions and titles are presentation text. They must never determine whether
two candidates represent the same issue. Legacy records without all identity
fields deliberately receive no fingerprint and are retained separately.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


IDENTITY_FIELDS = (
    "asset_origin",
    "entry_point",
    "category",
    "root_cause",
    "impact",
    "conditions",
)


def _identity_parts(candidate: Mapping[str, Any]) -> tuple[str, ...] | None:
    parts: list[str] = []
    for field in IDENTITY_FIELDS:
        value = candidate.get(field)
        if not isinstance(value, str):
            return None
        # Only canonicalize Unicode representation and surrounding whitespace.
        # Paths, hosts, effects and conditions can be case-sensitive; no fuzzy
        # similarity or title-based inference is safe for deduplication.
        normalized = unicodedata.normalize("NFC", value.strip())
        if not normalized:
            return None
        parts.append(normalized)
    return tuple(parts)


def finding_fingerprint(candidate: Mapping[str, Any]) -> str | None:
    """Return a stable fingerprint only for a complete structured identity.

    ``conditions`` must be explicit, including when the value is e.g. ``none``.
    A missing field never receives a synthetic default because that could merge
    distinct attack conditions or impacts.
    """
    parts = _identity_parts(candidate)
    if parts is None:
        return None
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def find_duplicate_finding(
    candidate: Mapping[str, Any], existing: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Find an exact structured duplicate; preserve incomplete legacy records."""
    fingerprint = finding_fingerprint(candidate)
    if fingerprint is None:
        return None
    for row in existing:
        if finding_fingerprint(row) == fingerprint:
            return row
    return None


def deduplicate_finding_candidates(
    candidates: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep the first complete identity and every candidate lacking one."""
    retained: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        fingerprint = finding_fingerprint(candidate)
        if fingerprint is not None:
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
        retained.append(candidate)
    return retained
