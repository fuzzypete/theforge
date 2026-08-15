"""The audit-visible record of which project invariants a run was offered (#1875).

Split from :mod:`theforge.task.invariant_selector` for the same reason
``prior_run_manifest`` is split from its selector: the selector answers *what an
agent may read*, this answers *what an operator can later see*. For the
invariant spike the operator question is sharper than usual, because the whole
claim under test is about scope decisions — so the manifest reports not just
included and dropped, but **uncertain**: the rules that were included broadly
precisely because TheForge could not confidently narrow them.
"""

from __future__ import annotations

from .invariant_selector import (
    CONFIDENCE_LOW,
    ELIGIBLE_PHASES,
    InvariantCandidate,
    InvariantSelection,
)


def disabled_manifest() -> dict:
    """The payload for a run whose ``knowledge.invariant_context`` is off."""
    return {
        "enabled": False,
        "phase": "",
        "selection_mode": "",
        "included": [],
        "dropped": [],
        "uncertain": [],
        "note": "invariant context disabled (knowledge.invariant_context)",
    }


def build_manifest(
    selection: InvariantSelection,
    *,
    included_ids: set[str],
    phase: str,
) -> dict:
    """Render what invariant knowledge this assembly offered, and what it withheld.

    Candidates absent from ``included_ids`` lost the context budget, so they are
    reported as ``budget_pressure`` — distinct from the applicability exclusions
    the selector already decided.
    """
    included: list[dict] = []
    dropped: list[dict] = []
    uncertain: list[dict] = []

    for candidate in selection.candidates:
        record = _record(candidate)
        if candidate.invariant_id in included_ids:
            included.append(record)
            if candidate.scope_confidence == CONFIDENCE_LOW:
                uncertain.append(record)
        else:
            dropped.append({**record, "reason": "budget_pressure"})

    for exclusion in selection.excluded:
        dropped.append(
            {
                "id": exclusion.invariant_id,
                "source_path": exclusion.source_path,
                "reason": exclusion.reason,
            }
        )

    return {
        "enabled": True,
        "phase": selection.phase or phase,
        "selection_mode": selection.selection_mode,
        "included": included,
        "dropped": dropped,
        "uncertain": uncertain,
        "note": _note(selection, included_count=len(included), uncertain_count=len(uncertain)),
    }


def _record(candidate: InvariantCandidate) -> dict:
    return {
        "id": candidate.invariant_id,
        "source_path": candidate.source_path,
        "source_anchor": candidate.source_anchor,
        "enforcement": candidate.enforcement,
        "rendering_mode": candidate.rendering_mode,
        "scope_confidence": candidate.scope_confidence,
        "reason": candidate.reason,
        "score": candidate.score,
        "source_digest_matches": candidate.source_digest_matches,
    }


def _note(selection: InvariantSelection, *, included_count: int, uncertain_count: int) -> str:
    """Separate 'no invariants are marked' from 'invariants exist and were withheld'."""
    if not selection.phase_eligible:
        return (
            f"invariant context is not injected in the {selection.phase} phase "
            f"(eligible phases: {', '.join(sorted(ELIGIBLE_PHASES))})"
        )
    if selection.entry_count == 0:
        return "no project invariants are indexed (run 'forge index --invariants')"

    parts = [f"{included_count} of {selection.entry_count} indexed invariants included"]
    if uncertain_count:
        parts.append(
            f"{uncertain_count} included as full source sections because scope confidence was low"
        )
    stale = sum(1 for candidate in selection.candidates if not candidate.source_digest_matches)
    if stale:
        parts.append(f"{stale} source regions changed since the index was built")
    return "; ".join(parts)
