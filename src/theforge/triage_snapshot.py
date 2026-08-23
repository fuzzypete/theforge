"""Canonical finding snapshots for triage stale-ratification checks.

The advisory proposal stage records a proposal-time finding snapshot in the
audit substrate, and ratification later compares that reviewed state against the
live backlog. The comparison must ignore producer-only formatting differences
while still surfacing real finding drift, so both sides normalize through this
module before they are hashed or compared.
"""

from __future__ import annotations

from collections.abc import Mapping


def _normalized_text(value: object) -> str:
    return str(value or "").strip()


def _normalized_issue_number(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalized_labels(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(label).strip() for label in raw if str(label).strip()]


def _normalized_evidence(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, (list, tuple)):
        return []
    normalized: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        evidence_id = _normalized_text(item.get("id") or item.get("evidence_id"))
        if not evidence_id:
            continue
        normalized.append(
            {
                "id": evidence_id,
                "kind": _normalized_text(item.get("kind") or "report"),
                "summary": _normalized_text(item.get("summary")),
                "checkable": bool(item.get("checkable", False)),
                "detail": _normalized_text(item.get("detail")),
            }
        )
    return normalized


def canonicalize_finding_snapshot(snapshot: Mapping[str, object] | None) -> dict[str, object]:
    """Return the stable snapshot shape used for ratification freshness checks."""
    raw = snapshot if isinstance(snapshot, Mapping) else {}
    return {
        "issue_ref": _normalized_text(raw.get("issue_ref")),
        "issue_number": _normalized_issue_number(raw.get("issue_number")),
        "title": _normalized_text(raw.get("title")),
        "body": _normalized_text(raw.get("body")),
        "labels": _normalized_labels(raw.get("labels")),
        "pool_state": _normalized_text(raw.get("pool_state")),
        "verification_status": _normalized_text(raw.get("verification_status")),
        "evidence": _normalized_evidence(raw.get("evidence")),
    }


__all__ = ["canonicalize_finding_snapshot"]
