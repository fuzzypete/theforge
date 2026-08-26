"""Deterministic Reason → sprint-admission verdict mapping.

Stable precedence: highest-precedence admission-refusing verdict wins when
multiple Reasons fire. Advisory-only findings stay in ``ShapeResult.reasons``
for rendering, but they do not decide admission unless explicitly listed as a
lifecycle refusal. Identifiers are part of the audit/summary contract — do
not rename without updating ADR-0001 and the downstream surfaces.
"""

from __future__ import annotations

from theforge.shape_check.issue_spec import lifecycle_refusals
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


def _explicit_lifecycle_refusals() -> tuple[tuple[str, ShapeVerdict], ...]:
    """Lifecycle refusals, derived from the typed issue specification.

    A state a type can occupy that does not admit implementation is a refusal
    (ADR-0009 clause 4) — the specification says so, and this layer reads it
    rather than keeping a second list. Refusal codes already covered by the
    blocking precedence table above are skipped; only the states that refuse
    *without* a blocking finding — today, a complete diagnosis with no asserted
    cause — need an entry here.
    """
    blocking = {code for code, _ in _BLOCKING_PRECEDENCE}
    refusals: list[tuple[str, ShapeVerdict]] = []
    for code, _state_key in lifecycle_refusals():
        if code in blocking:
            continue
        try:
            refusals.append((code, ShapeVerdict(code)))
        except ValueError:
            # A lifecycle state whose refusal has no verdict of its own falls
            # through to the operator-action fallback below.
            continue
    return tuple(refusals)


_EXPLICIT_LIFECYCLE_REFUSALS: tuple[tuple[str, ShapeVerdict], ...] = _explicit_lifecycle_refusals()


def blocking_codes(reasons: tuple[Reason, ...]) -> frozenset[str]:
    """Return the blocking reason codes in ``reasons``.

    Producers compare this across an edit to answer a question the verdict
    alone cannot: did *my* edit add a defect? The verdict can hide one — a
    finding added underneath a higher-precedence one leaves the summary word
    unchanged — and it can also move for a good reason, since resolving
    ``needs_diagnosis`` by writing an open Diagnosis section trades a blocking
    finding for the advisory ``diagnosis_cause_unknown``. Counting blocking
    findings distinguishes those two: the first grows the set, the second
    shrinks it.
    """
    return frozenset(
        reason.code for reason in reasons if reason.severity is Severity.BLOCKING
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
