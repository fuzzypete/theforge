"""Deterministic Reason → ShapeVerdict mapping.

Stable precedence: highest-precedence verdict wins when multiple Reasons
fire. Identifiers are part of the audit/summary contract — do not rename
without updating ADR-0001 and the downstream surfaces.
"""

from __future__ import annotations

from theforge.shape_check.types import Reason, ShapeVerdict

# Precedence order: earlier entries win over later. The list is closed —
# if a Reason.code is unmapped, callers fall back to NEEDS_OPERATOR_ACTION
# so a new code never silently downgrades to RUNNABLE.
_PRECEDENCE: tuple[tuple[str, ShapeVerdict], ...] = (
    ("superseded", ShapeVerdict.DUPLICATE_OR_STALE),
    ("epic_or_tracking", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("untriaged_finding", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("reopened_stale_contract", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("criterion_needs_live_evidence", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("missing_type", ShapeVerdict.NEEDS_TYPE),
    ("needs_diagnosis", ShapeVerdict.NEEDS_DIAGNOSIS),
    ("type_shape_contradiction", ShapeVerdict.NEEDS_GROOMING_TYPE_SHAPE),
    ("diagnosis_cause_unknown", ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN),
    ("too_many_behavioral_clusters", ShapeVerdict.NEEDS_GROOMING_SCOPE_SPLIT),
    ("missing_acceptance_criteria", ShapeVerdict.NEEDS_GROOMING_MISSING_AC),
    ("no_observable_done_state", ShapeVerdict.NEEDS_GROOMING_MISSING_AC),
    ("missing_example", ShapeVerdict.NEEDS_GROOMING_MISSING_EXAMPLE),
    ("implementation_plan_in_body", ShapeVerdict.ADR_CANDIDATE),
    ("implementation_design_dump", ShapeVerdict.ADR_CANDIDATE),
    ("bug_fix_location_prescription", ShapeVerdict.RUNNABLE),
    ("bug_test_requirement", ShapeVerdict.RUNNABLE),
)


def derive_verdict(reasons: tuple[Reason, ...]) -> ShapeVerdict:
    """Return the single ShapeVerdict implied by ``reasons``.

    No reasons → RUNNABLE. Otherwise the highest-precedence mapped verdict
    wins; an unmapped code falls back to NEEDS_OPERATOR_ACTION rather than
    silently passing as RUNNABLE.
    """
    if not reasons:
        return ShapeVerdict.RUNNABLE
    codes = {r.code for r in reasons}
    for code, verdict in _PRECEDENCE:
        if code in codes:
            return verdict
    return ShapeVerdict.NEEDS_OPERATOR_ACTION
