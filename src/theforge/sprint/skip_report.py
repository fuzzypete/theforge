"""Shape-gate skip observability: emit per-skip records and build the postmortem
classification block.

This is the seam between the sprint-entry shape gate and the audit substrate for
issue #1453. Two responsibilities:

* :func:`emit_shape_skip_events` — called from ``cli/sprint.py`` after the gate
  and intake remediation have settled, so each ``(issue, reason_code)`` skip is
  recorded with its taxonomy category (including the remediation outcome) into
  the ``shape_skip_events`` substrate table.
* :func:`build_shape_gate_skip_block` — called from the sprint summary/audit
  writers to project this run's skip events into the operator-facing
  ``shape_gate_skips`` block that the postmortem digest and RCA render. Stuck
  patterns (an issue blocked by the same code ``>= threshold`` times across
  runs) are surfaced here rather than reconstructed from log files.

Emission is observability, not gating: a substrate write failure is logged and
swallowed so the sprint proceeds exactly as before.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable

from ..shape_check.skip_taxonomy import (
    RemediationOutcome,
    SkipSeverity,
    classify_skip,
    group_by_category,
)

_log = logging.getLogger(__name__)


def _skip_reason_codes(skip: object) -> list[str]:
    codes = getattr(skip, "reason_codes", None)
    if codes is None and isinstance(skip, dict):
        codes = skip.get("reason_codes")
    return [str(c) for c in (codes or []) if str(c).strip()]


def _skip_number(skip: object) -> int | None:
    num = getattr(skip, "issue_number", None)
    if num is None and isinstance(skip, dict):
        num = skip.get("issue_number")
    try:
        return int(num) if num is not None else None
    except (TypeError, ValueError):
        return None


def _skip_source(skip: object) -> str:
    src = getattr(skip, "source", None)
    if src is None and isinstance(skip, dict):
        src = skip.get("source")
    return str(src or "")


def emit_shape_skip_events(
    project_root: Path,
    *,
    run_id: str | None,
    sprint_name: str | None = None,
    sprint_id: str | None = None,
    milestone: str | None = None,
    skipped: Iterable[object] = (),
    advisories: Iterable[object] = (),
    remediated_numbers: "set[int] | frozenset[int]" = frozenset(),
    declined_numbers: "set[int] | frozenset[int]" = frozenset(),
    record: Callable[[Path, dict], int] | None = None,
) -> int:
    """Record one substrate skip event per ``(issue, reason_code)``.

    ``skipped`` events are blocking, ``advisories`` are advisory. The
    remediation outcome for an issue (``remediated_numbers`` /
    ``declined_numbers``) sets the taxonomy category so the postmortem can
    separate "the gate fixed it and it ran" from "the gate refused" from "still
    blocked". Returns the number of events written. All failures are logged at
    WARNING and swallowed — this is observability, never gating.
    """
    if record is None:
        from ..coordinator.audit_substrate import record_shape_skip_event as record

    written = 0
    remediated = {int(n) for n in remediated_numbers}
    declined = {int(n) for n in declined_numbers}

    def _emit(skip: object, severity: SkipSeverity) -> None:
        nonlocal written
        number = _skip_number(skip)
        if number is None:
            return
        if number in remediated:
            remediation = RemediationOutcome.REMEDIATED
        elif number in declined:
            remediation = RemediationOutcome.DECLINED
        else:
            remediation = RemediationOutcome.NONE
        source = _skip_source(skip)
        for code in _skip_reason_codes(skip):
            classification = classify_skip(
                code, source, severity=severity, remediation=remediation
            )
            event = {
                "issue_id": str(number),
                "reason_code": code,
                "source": source,
                "severity": classification.severity.value,
                "category": classification.category.value,
                "four_question_axis": classification.four_question_axis.value,
                "run_id": run_id,
                "sprint_id": sprint_id,
                "sprint_name": sprint_name,
                "milestone": milestone,
            }
            try:
                record(project_root, event)
                written += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "shape-gate skip substrate write failed for issue=%s code=%s: %s",
                    number,
                    code,
                    exc,
                )

    for skip in skipped:
        _emit(skip, SkipSeverity.BLOCKING)
    for advisory in advisories:
        _emit(advisory, SkipSeverity.ADVISORY)
    return written


def _slim_event(event: dict) -> dict:
    """Project a stored skip event to the fields the postmortem block renders."""
    return {
        "issue_id": event.get("issue_id"),
        "reason_code": event.get("reason_code"),
        "source": event.get("source"),
        "severity": event.get("severity"),
        "category": event.get("category"),
        "four_question_axis": event.get("four_question_axis"),
        "prior_block_count": event.get("prior_block_count", 0),
    }


def build_shape_gate_skip_block(
    project_root: Path,
    run_id: str | None,
    *,
    threshold: int,
) -> dict | None:
    """Build the ``shape_gate_skips`` classification block for one sprint run.

    Reads this run's skip events from the substrate, groups them by taxonomy
    category, and flags stuck-issue patterns — an issue this run skipped that
    has been blocked by the same code ``>= threshold`` times across all runs.
    Returns ``None`` when this run recorded no skip events (so the summary/audit
    omit the block entirely) or when the substrate is unavailable. Never raises:
    the block is observability layered on top of the canonical summary.
    """
    if not run_id:
        return None
    try:
        from ..coordinator import audit_substrate

        conn = audit_substrate.create_or_open(project_root)
    except Exception as exc:  # noqa: BLE001
        _log.warning("shape-gate skip block: substrate unavailable: %s", exc)
        return None
    try:
        events = list(audit_substrate.iter_shape_skip_events(conn, run_id=run_id))
        if not events:
            return None
        grouped = group_by_category(events)
        all_stuck = audit_substrate.repeated_shape_skip_blocks(conn, threshold=threshold)
    except Exception as exc:  # noqa: BLE001
        _log.warning("shape-gate skip block: query failed: %s", exc)
        return None
    finally:
        conn.close()

    run_pairs = {(str(e.get("issue_id")), str(e.get("reason_code"))) for e in events}
    stuck = [s for s in all_stuck if (str(s["issue_id"]), str(s["reason_code"])) in run_pairs]

    categories = {cat: [_slim_event(e) for e in rows] for cat, rows in grouped.items()}
    return {
        "threshold": threshold,
        "total": len(events),
        "category_counts": {cat: len(rows) for cat, rows in categories.items()},
        "categories": categories,
        "stuck_issues": stuck,
    }
