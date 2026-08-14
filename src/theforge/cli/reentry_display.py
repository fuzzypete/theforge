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

The same three surfaces are where an issue's running cost belongs, for the same
reason: re-entry is the moment the figure decides something. That aggregate is
computed in :mod:`theforge.coordinator.issue_cost`; this module only formats it
(#2365).
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


def issue_cost_line(project_root: Path | None, slug: str, *, indent: str = "    ") -> list[str]:
    """The ``issue to date:`` line for ``slug``, or an empty list.

    Re-entry is the decision point the aggregate exists for: what the issue has
    already cost, and how many attempts it took, are the two figures that decide
    whether a further attempt is worth making — and both were only ever visible
    as the *current run's* numbers before this (#2365). Rendered before the new
    attempt spends anything, because the substrate holds only runs that finished.

    Empty for a story with a single recorded run, so the common case reads
    exactly as it did.
    """
    from theforge.coordinator.issue_cost import load_issue_cost  # noqa: PLC0415

    aggregate = load_issue_cost(project_root, slug=slug)
    if aggregate is None or not aggregate.has_prior_attempts:
        return []
    return [
        f"{indent}issue to date: {aggregate.describe()}  "
        f"(next would be run {aggregate.attempts + 1})"
    ]


def reentry_lines(project_root: Path | None, slug: str, *, indent: str = "    ") -> list[str]:
    """Ready-to-print ``outstanding:`` / ``re-entry:`` / ``issue to date:`` lines.

    Empty list when there is nothing to disclose, so a caller can splice the
    result into its own layout without a conditional. The cost line stands on
    its own evidence: a story whose review completed has nothing outstanding to
    disclose but may still have cost five runs to get there.
    """
    lines: list[str] = []
    loaded = load_reentry_display(project_root, slug)
    if loaded is not None:
        phases, note = loaded
        if phases:
            lines.append(f"{indent}outstanding: {', '.join(phases)}")
        if note:
            lines.append(f"{indent}re-entry: {note}")
    lines.extend(issue_cost_line(project_root, slug, indent=indent))
    return lines
