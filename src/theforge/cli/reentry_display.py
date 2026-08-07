"""Operator-facing rendering of a stopped story's re-entry disclosure.

``forge status`` has three surfaces that can be the one an operator is looking
at when they decide how to re-enter a stopped story: the live/crashed sprint
table (``cli/sprint_status.py``), the completed-sprint postmortem digest
(``cli/sprint_digest.py``), and the pending-decision list (``cli/status.py``).
A story with an unrun review cycle has to be distinguishable from one whose
review completed on *all* of them — a disclosure present on two of three is a
disclosure the operator can miss by looking at the wrong one (#2239).

This module is the single formatter for that. The judgement itself is not made
here: :mod:`theforge.coordinator.resume_persistence` derives it from the story's
persisted resume record, and this only lays the result out. Best-effort by
inheritance — the loader returns None for an absent or unreadable record, so a
status view never fails on a missing sidecar.
"""

from __future__ import annotations

from pathlib import Path


def load_reentry_display(project_root: Path | None, slug: str) -> tuple[list[str], str] | None:
    """Return ``(outstanding_phases, reentry_note)`` for ``slug``, or None.

    None means there is nothing to disclose: no record, no review progress in
    it, or a review that completed. ``reentry_note`` is empty when both re-entry
    paths would do the same thing, even though a phase is still outstanding.
    """
    if project_root is None or not slug:
        return None
    try:
        from theforge.coordinator.resume_persistence import (  # noqa: PLC0415
            describe_outstanding_phases,
            describe_reentry_paths,
            load_reentry_analysis,
        )

        analysis = load_reentry_analysis(Path(project_root), slug)
    except Exception:
        return None
    if not analysis:
        return None
    phases = describe_outstanding_phases(analysis)
    note = describe_reentry_paths(analysis)
    if not phases and not note:
        return None
    return phases, note


def reentry_lines(project_root: Path | None, slug: str, *, indent: str = "    ") -> list[str]:
    """Ready-to-print ``outstanding:`` / ``re-entry:`` lines for ``slug``.

    Empty list when there is nothing to disclose, so a caller can splice the
    result into its own layout without a conditional.
    """
    loaded = load_reentry_display(project_root, slug)
    if loaded is None:
        return []
    phases, note = loaded
    lines: list[str] = []
    if phases:
        lines.append(f"{indent}outstanding: {', '.join(phases)}")
    if note:
        lines.append(f"{indent}re-entry: {note}")
    return lines
