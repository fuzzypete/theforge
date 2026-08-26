"""Deterministic Reason → sprint-admission verdict mapping.

Stable precedence: highest-precedence admission-refusing verdict wins when
multiple Reasons fire. Advisory-only findings stay in ``ShapeResult.reasons``
for rendering, but they do not decide admission unless explicitly listed as a
lifecycle refusal. Identifiers are part of the audit/summary contract — do
not rename without updating ADR-0001 and the downstream surfaces.
"""

from __future__ import annotations

from theforge.shape_check.types import Reason, Severity, ShapeVerdict

# Precedence order: earlier entries win over later.
_BLOCKING_PRECEDENCE: tuple[tuple[str, ShapeVerdict], ...] = (
    ("superseded", ShapeVerdict.DUPLICATE_OR_STALE),
    ("epic_or_tracking", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("untriaged_finding", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("reopened_stale_contract", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("criterion_needs_live_evidence", ShapeVerdict.NEEDS_OPERATOR_ACTION),
    ("missing_type", ShapeVerdict.NEEDS_TYPE),
    ("type_shape_contradiction", ShapeVerdict.NEEDS_GROOMING_TYPE_SHAPE),
    ("needs_diagnosis", ShapeVerdict.NEEDS_DIAGNOSIS),
    ("too_many_behavioral_clusters", ShapeVerdict.NEEDS_GROOMING_SCOPE_SPLIT),
    ("missing_acceptance_criteria", ShapeVerdict.NEEDS_GROOMING_MISSING_AC),
    ("no_observable_done_state", ShapeVerdict.NEEDS_GROOMING_MISSING_AC),
    ("missing_example", ShapeVerdict.NEEDS_GROOMING_MISSING_EXAMPLE),
)

_EXPLICIT_LIFECYCLE_REFUSALS: tuple[tuple[str, ShapeVerdict], ...] = (
    ("diagnosis_cause_unknown", ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN),
)


def derive_verdict(reasons: tuple[Reason, ...]) -> ShapeVerdict:
    """Return the single ShapeVerdict implied by ``reasons``.

    ``RUNNABLE`` is the default. A non-runnable verdict is selected only from:

    - blocking reasons mapped to an admission-refusing verdict,
    - explicit lifecycle refusals such as ``diagnosis_cause_unknown``, or
    - the closed-table fallback for an unmapped blocking reason.

    Advisory-only reason sets that contain no explicit refusal stay runnable.
    """
    if not reasons:
        return ShapeVerdict.RUNNABLE
    by_code: dict[str, list[Reason]] = {}
    for reason in reasons:
        by_code.setdefault(reason.code, []).append(reason)

    blocking_codes = {reason.code for reason in reasons if reason.severity is Severity.BLOCKING}
    for code, verdict in _BLOCKING_PRECEDENCE:
        if code in blocking_codes:
            return verdict

    for code, verdict in _EXPLICIT_LIFECYCLE_REFUSALS:
        if code in by_code:
            return verdict

    if blocking_codes:
        return ShapeVerdict.NEEDS_OPERATOR_ACTION
    return ShapeVerdict.RUNNABLE
