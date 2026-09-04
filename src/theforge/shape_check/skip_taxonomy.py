"""Shape-gate skip taxonomy — pure classification of skip reason codes.

Every shape-gate skip today looks the same in operator-facing output: an opaque
``reason_codes`` tuple and a single "X issue(s) flagged by shape gate" line. This
module makes the *kind* of friction visible by tagging each skip along two axes:

* a **category** an operator reads for recovery cost — a stale-label block the
  operator fixes in seconds vs. a semantic gate no operator can satisfy without
  invoking a producer vs. a genuine structural failure; and
* the **four-question framework** from #1450 (what invariant failed / what
  response is valid / was a response attempted and did the gate still refuse /
  did verification pass but another gate fire). Tagging v0.11 skip records along
  this axis is what lets the v0.12 architectural audit ground itself in real
  data rather than reconstructing intent from log files.

Stdlib-only and side-effect-free: this is the low-dependency pure-data layer the
substrate writer and the postmortem/RCA renderers all classify through, so the
taxonomy stays a single discoverable source rather than drifting across
surfaces. Grep :data:`STALE_LABEL_CODES` / :data:`SEMANTIC_GATE_CODES` /
:data:`AXIS_BY_CODE` to see everything the classifier knows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Default "stuck issue" threshold: when the same issue has been blocked by the
# same skip code this many times across runs, the postmortem flags it as a
# repeated-block pattern. #1135 hit ``reopened_stale_contract`` ten times and
# #1405 three times before manual log-walking surfaced them — a default of 3
# would have flagged both weeks earlier. Operators may raise it via
# ``shape_check.stuck_issue_threshold`` in forge.yaml.
DEFAULT_STUCK_ISSUE_THRESHOLD = 3


class SkipCategory(str, Enum):
    """Operator-facing recovery classes for a shape-gate skip.

    The five values are the minimum grouping the sprint postmortem distinguishes
    (issue #1453 AC2). ``REMEDIATED_PROCEEDED`` and ``DECLINED_REMEDIATION`` are
    remediation *outcomes* layered on top of the underlying code class; the other
    three derive from the reason code + skip source alone.
    """

    SEMANTIC_GATE = "blocked_by_semantic_gate"
    STALE_LABEL = "blocked_by_stale_label"
    REMEDIATED_PROCEEDED = "remediated_and_proceeded"
    DECLINED_REMEDIATION = "declined_by_remediation"
    UNRUNNABLE_SHAPE = "unrunnable_by_shape"


class FourQuestionAxis(str, Enum):
    """The #1450 four-question framework, one value per question.

    * ``INVARIANT_VIOLATED`` — the shape gate found a structural invariant the
      issue simply does not satisfy (missing type, missing AC, ...).
    * ``RESPONSE_NOT_ATTEMPTED`` — a valid response exists but no producer has
      run yet (a bug needs diagnosis, a reopen needs its context folded in).
    * ``RESPONSE_ATTEMPTED_GATE_FAILED`` — remediation ran and the gate still
      refused the issue.
    * ``VERIFICATION_PASSED_OTHER_GATE`` — the content check would pass but a
      separate gate (a stale label) fired anyway.
    """

    INVARIANT_VIOLATED = "invariant_violated"
    RESPONSE_NOT_ATTEMPTED = "response_not_yet_attempted"
    RESPONSE_ATTEMPTED_GATE_FAILED = "response_attempted_gate_verification_failed"
    VERIFICATION_PASSED_OTHER_GATE = "verification_passed_but_other_gate_fired"


class SkipSeverity(str, Enum):
    """Whether a skip blocks sprint entry or is advisory-only.

    Mirrors the blocking/advisory distinction the shape gate already draws
    between ``ShapeGateResult.skipped`` and ``ShapeGateResult.advisories``.
    """

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class RemediationOutcome(str, Enum):
    """What the intake remediation gate did with a skipped issue.

    ``REMEDIATED`` — the gate auto-fixed the issue and it proceeded to dev.
    ``DECLINED`` — the gate refused (the skip reason is not body-only fixable).
    ``NONE`` — remediation did not run or had no verdict for this issue.
    """

    REMEDIATED = "remediated"
    DECLINED = "declined"
    NONE = "none"


# ── Reason-code → category / axis mapping (single discoverable location) ──────
#
# Codes are the ``Reason.code`` strings emitted by shape_check/heuristics.py plus
# the gate-local synthetic codes defined in sprint/shape_gate.py. Any code not
# listed here falls through to the structural / unrunnable class — a new
# structural check needs no edit here, while a new *semantic* or *stale-label*
# check must be added explicitly so it is not silently miscategorised as
# operator-unrunnable.

# The stale ``needs-grooming`` label is the seconds-to-fix class: the local check
# may already pass, but the async relabeler has not caught up.
STALE_LABEL_CODES: frozenset[str] = frozenset({"needs_grooming_label"})

# Semantic gates verify content a producer must generate — the operator cannot
# satisfy them by hand in seconds. These are the codes #1450 wants surfaced as
# "response not yet attempted" rather than "malformed".
SEMANTIC_GATE_CODES: frozenset[str] = frozenset(
    {
        "needs_diagnosis",
        "diagnosis_cause_unknown",
        "reopened_stale_contract",
        "criterion_needs_live_evidence",
        # Ratified semantic readiness (#2785). These withhold sprint entry
        # without any structural finding — the document's shape is runnable and
        # what is missing is a recorded operator ratification of its semantic
        # review — so they belong in the semantic class, not the malformed one.
        "semantic_review_not_ratified",
        "semantic_concerns_accepted",
        "semantic_evaluation_failed",
    }
)

# Per-code four-question axis. Codes absent from this map are structural
# invariants → ``INVARIANT_VIOLATED``.
AXIS_BY_CODE: dict[str, FourQuestionAxis] = {
    "needs_grooming_label": FourQuestionAxis.VERIFICATION_PASSED_OTHER_GATE,
    "needs_diagnosis": FourQuestionAxis.RESPONSE_NOT_ATTEMPTED,
    "diagnosis_cause_unknown": FourQuestionAxis.RESPONSE_NOT_ATTEMPTED,
    "reopened_stale_contract": FourQuestionAxis.RESPONSE_NOT_ATTEMPTED,
    "criterion_needs_live_evidence": FourQuestionAxis.RESPONSE_NOT_ATTEMPTED,
    "semantic_review_not_ratified": FourQuestionAxis.RESPONSE_NOT_ATTEMPTED,
    "semantic_concerns_accepted": FourQuestionAxis.RESPONSE_NOT_ATTEMPTED,
    "semantic_evaluation_failed": FourQuestionAxis.RESPONSE_NOT_ATTEMPTED,
}


@dataclass(frozen=True)
class SkipClassification:
    """The taxonomy verdict for a single (reason_code, source, remediation) skip."""

    reason_code: str
    source: str
    severity: SkipSeverity
    category: SkipCategory
    four_question_axis: FourQuestionAxis

    def as_dict(self) -> dict:
        return {
            "reason_code": self.reason_code,
            "source": self.source,
            "severity": self.severity.value,
            "category": self.category.value,
            "four_question_axis": self.four_question_axis.value,
        }


def _base_category(reason_code: str, source: str) -> SkipCategory:
    """Category from the code + source alone, before remediation is considered."""
    if reason_code in STALE_LABEL_CODES:
        return SkipCategory.STALE_LABEL
    if reason_code in SEMANTIC_GATE_CODES:
        return SkipCategory.SEMANTIC_GATE
    return SkipCategory.UNRUNNABLE_SHAPE


def classify_skip(
    reason_code: str,
    source: str,
    *,
    severity: SkipSeverity = SkipSeverity.BLOCKING,
    remediation: RemediationOutcome = RemediationOutcome.NONE,
) -> SkipClassification:
    """Classify one skip into its category + four-question axis.

    ``remediation`` overrides the category: a remediated issue is reported as
    ``REMEDIATED_PROCEEDED`` and a refused one as ``DECLINED_REMEDIATION`` so the
    postmortem can separate "the gate fixed it" from "the gate gave up" from "the
    operator never got a fix attempt". The axis for a declined issue becomes
    ``RESPONSE_ATTEMPTED_GATE_FAILED`` (a response *was* attempted); otherwise the
    axis is derived from the reason code.
    """
    code = (reason_code or "").strip()

    if remediation is RemediationOutcome.REMEDIATED:
        category = SkipCategory.REMEDIATED_PROCEEDED
        axis = AXIS_BY_CODE.get(code, FourQuestionAxis.INVARIANT_VIOLATED)
    elif remediation is RemediationOutcome.DECLINED:
        category = SkipCategory.DECLINED_REMEDIATION
        axis = FourQuestionAxis.RESPONSE_ATTEMPTED_GATE_FAILED
    else:
        category = _base_category(code, source)
        axis = AXIS_BY_CODE.get(code, FourQuestionAxis.INVARIANT_VIOLATED)

    return SkipClassification(
        reason_code=code,
        source=source or "",
        severity=severity,
        category=category,
        four_question_axis=axis,
    )


# Deterministic display order for the postmortem — cheapest-to-recover first so
# the operator sees "you can unblock these in seconds" above "these need a
# producer" above "these are genuinely malformed".
CATEGORY_DISPLAY_ORDER: tuple[SkipCategory, ...] = (
    SkipCategory.STALE_LABEL,
    SkipCategory.REMEDIATED_PROCEEDED,
    SkipCategory.SEMANTIC_GATE,
    SkipCategory.DECLINED_REMEDIATION,
    SkipCategory.UNRUNNABLE_SHAPE,
)


def group_by_category(events: list[dict]) -> dict[str, list[dict]]:
    """Group skip-event dicts by their ``category`` field, in display order.

    Events lacking a recognised category fall under ``UNRUNNABLE_SHAPE`` so no
    event is silently dropped from the postmortem. Returns an insertion-ordered
    mapping restricted to non-empty categories.
    """
    buckets: dict[str, list[dict]] = {}
    for event in events:
        raw = str(event.get("category") or SkipCategory.UNRUNNABLE_SHAPE.value)
        buckets.setdefault(raw, []).append(event)

    ordered: dict[str, list[dict]] = {}
    for category in CATEGORY_DISPLAY_ORDER:
        rows = buckets.pop(category.value, None)
        if rows:
            ordered[category.value] = rows
    # Any category value not in the canonical order (forward-compat) trails the
    # known ones rather than vanishing.
    for leftover, rows in buckets.items():
        ordered[leftover] = rows
    return ordered
