"""Pure-data result types for shape_check. Stdlib only."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Shape(str, Enum):
    RUNNABLE = "runnable"
    NEEDS_GROOMING = "needs_grooming"
    TRACKING_ONLY = "tracking_only"
    SUPERSEDED = "superseded"


class Severity(str, Enum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


class SuggestedAction(str, Enum):
    SPLIT = "split"
    CLARIFY = "clarify"
    REMOVE_FROM_SPRINT = "remove_from_sprint"
    CLOSE = "close"
    PROCEED = "proceed"


@dataclass(frozen=True)
class Reason:
    code: str
    severity: Severity
    detail: str


class ShapeVerdict(str, Enum):
    """Bounded verdict taxonomy emitted by the shape gate.

    Stable string identifiers — used in audit YAML, sprint summary, and
    operator-facing status surfaces. Per ADR-0001, verdicts are vocabulary
    only; routing to a producer command is the operator's call.
    """

    RUNNABLE = "runnable"
    NEEDS_TYPE = "needs_type"
    NEEDS_DIAGNOSIS = "needs_diagnosis"
    NEEDS_GROOMING_TYPE_SHAPE = "needs_grooming_type_shape"
    DIAGNOSIS_CAUSE_UNKNOWN = "diagnosis_cause_unknown"
    NEEDS_GROOMING_MISSING_AC = "needs_grooming_missing_ac"
    NEEDS_GROOMING_MISSING_EXAMPLE = "needs_grooming_missing_example"
    NEEDS_GROOMING_SCOPE_SPLIT = "needs_grooming_scope_split"
    NEEDS_OPERATOR_ACTION = "needs_operator_action"
    ADR_CANDIDATE = "adr_candidate"
    DUPLICATE_OR_STALE = "duplicate_or_stale"


VERDICT_DESCRIPTIONS: dict[ShapeVerdict, str] = {
    ShapeVerdict.RUNNABLE: "issue passes shape gate; safe to enter implementation sprint",
    ShapeVerdict.NEEDS_TYPE: (
        "issue has no recognized type label; add bug/enhancement/epic/task before sprinting"
    ),
    ShapeVerdict.NEEDS_DIAGNOSIS: (
        "bug filing has no diagnosis section; run forge diagnose to investigate"
    ),
    ShapeVerdict.NEEDS_GROOMING_TYPE_SHAPE: (
        "declared issue type contradicts the body's section shape; relabel it or rewrite the body"
    ),
    ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN: (
        "investigation-ready; further diagnosis or operator-driven RCA needed"
    ),
    ShapeVerdict.NEEDS_GROOMING_MISSING_AC: (
        "feature-shaped issue without acceptance criteria; run forge groom to draft"
    ),
    ShapeVerdict.NEEDS_GROOMING_MISSING_EXAMPLE: (
        "feature-shaped issue without a concrete example; add a target sketch or sample"
    ),
    ShapeVerdict.NEEDS_GROOMING_SCOPE_SPLIT: (
        "issue spans too many behavioral clusters; split into smaller issues"
    ),
    ShapeVerdict.NEEDS_OPERATOR_ACTION: (
        "issue requires operator triage (epic/tracking, untriaged finding, stale reopen, "
        "or an acceptance criterion the dev loop cannot satisfy)"
    ),
    ShapeVerdict.ADR_CANDIDATE: (
        "issue body contains design/implementation content; "
        "promote to ADR or refine in plan-review"
    ),
    ShapeVerdict.DUPLICATE_OR_STALE: (
        "issue is superseded by another or otherwise stale; close or merge"
    ),
}


@dataclass(frozen=True)
class ShapeResult:
    shape: Shape
    reasons: tuple[Reason, ...] = field(default_factory=tuple)
    suggested_action: SuggestedAction = SuggestedAction.PROCEED
    verdict: ShapeVerdict = ShapeVerdict.RUNNABLE
