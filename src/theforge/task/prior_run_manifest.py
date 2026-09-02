"""The audit-visible record of what prior knowledge a run was offered.

Split from ``prior_run_selector`` because it answers a different question. The
selector decides *what an agent may read*; this module renders *what an operator
can later see* — and the operator-facing question the story turns on is one the
selector never asks: was nothing known, or was something known and withheld?
"""

from __future__ import annotations

from .prior_run_selector import (
    INDEX_STATE_MISSING,
    INDEX_STATE_STALE_SCHEMA,
    INDEX_STATE_UNREADABLE,
    SUPPORTED_PHASES,
    PriorRunSelection,
)


def disabled_manifest() -> dict:
    """The manifest payload for a run whose ``knowledge.prior_run_context`` is off."""
    return {
        "enabled": False,
        "phase": "",
        "rendering_mode": "",
        "index_state": None,
        "included": [],
        "dropped": [],
        "note": "prior-run context disabled (knowledge.prior_run_context)",
    }


def build_manifest(
    selection: PriorRunSelection,
    *,
    included_run_ids: set[str],
    phase: str,
) -> dict:
    """Render the audit-visible record of what prior knowledge a run was offered.

    Candidates absent from ``included_run_ids`` lost to the context budget, so
    they are reported as ``budget_pressure`` — distinct from the admissibility
    and relevance exclusions the selector already decided.
    """
    included: list[dict] = []
    dropped: list[dict] = []

    for candidate in selection.candidates:
        record = {
            "run_id": candidate.run_id,
            "phase": candidate.phase,
            "rendering_mode": candidate.rendering_mode,
            "reason": candidate.reason,
            "score": candidate.score,
            "verdict": candidate.verdict.to_dict(),
        }
        if candidate.run_id in included_run_ids:
            included.append({**record, "rendered_size": candidate.rendered_size.to_dict()})
        else:
            dropped.append({**record, "reason": "budget_pressure"})

    for exclusion in selection.excluded:
        record = {
            "run_id": exclusion.run_id,
            "phase": selection.phase or phase,
            "rendering_mode": selection.rendering_mode,
            "reason": exclusion.reason,
        }
        if exclusion.verdict is not None:
            record["verdict"] = exclusion.verdict.to_dict()
        dropped.append(record)

    return {
        "enabled": True,
        "phase": selection.phase or phase,
        "rendering_mode": selection.rendering_mode,
        "index_state": selection.index_state,
        "included": included,
        "dropped": dropped,
        "note": _build_note(
            selection,
            included_count=len(included),
            budget_dropped_count=len(selection.candidates) - len(included),
            phase=phase,
        ),
    }


def _build_note(
    selection: PriorRunSelection,
    *,
    included_count: int,
    budget_dropped_count: int,
    phase: str,
) -> str:
    """Explain the difference between 'nothing was known' and 'something was withheld'.

    Only counts drawn from *relevant* entries appear here. The selector settles
    relevance before admissibility precisely so this sentence cannot claim an
    unrelated inadmissible summary as knowledge this story was denied.
    """
    if not selection.phase_eligible:
        return (
            f"prior-run context is not injected in the {phase} phase "
            f"(supported phases: {', '.join(sorted(SUPPORTED_PHASES))})"
        )
    if selection.entry_count == 0:
        if selection.index_state == INDEX_STATE_MISSING:
            return (
                "prior-run knowledge index is missing or was never built; "
                "run `forge index` to build .forge/knowledge/index.yaml"
            )
        if selection.index_state == INDEX_STATE_UNREADABLE:
            return (
                "prior-run knowledge index is unreadable; "
                "run `forge index` to rebuild .forge/knowledge/index.yaml"
            )
        if selection.index_state == INDEX_STATE_STALE_SCHEMA:
            return (
                "prior-run knowledge index uses an unsupported schema version; "
                "run `forge index` to rebuild .forge/knowledge/index.yaml"
            )
        return "no relevant prior knowledge exists (no indexed summaries)"

    inadmissible = sum(1 for item in selection.excluded if item.admissibility_excluded)
    stale = sum(1 for item in selection.excluded if item.reason.startswith("stale("))
    over_cap = sum(
        1 for item in selection.excluded if item.reason.startswith("below_selection_cap")
    )

    parts: list[str] = []
    if included_count:
        parts.append(f"{included_count} prior summaries included")
    if budget_dropped_count:
        parts.append(f"{budget_dropped_count} relevant summaries dropped under budget pressure")
    if over_cap:
        parts.append(f"{over_cap} lower-ranked matches not offered")
    if inadmissible:
        parts.append(f"{inadmissible} summaries matched but were excluded on admissibility")
    if stale:
        parts.append(f"{stale} summaries excluded as stale")
    if not parts:
        return "no relevant prior knowledge exists"
    return "; ".join(parts)
