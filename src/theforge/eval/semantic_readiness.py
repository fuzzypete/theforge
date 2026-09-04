"""Semantic readiness: the policy that turns a ratified evaluation into state.

ADR-0009 clause 6 draws the line this module implements. The evaluator
(``semantic_runner``) is audit-only and probabilistic; its findings are
*concerns*, never verdicts. Nothing an evaluator returns changes admission.
What admission consumes is the state derived here: a recorded, operator-ratified
review of one exact document revision.

Two axes, deliberately separate (they answer different questions and a single
enum would compress them the way ADR-0009 clause 2 warns against):

* **Requirement** — does policy require a semantic review of this document
  before implementation? ``required`` for the dev-runnable types named in
  :data:`SEMANTIC_REVIEW_REQUIRED_TYPES` while the document is in the
  ``implementation_ready`` lifecycle state; ``not_required`` for every type and
  state policy does not name.
* **Evaluation state** — what is on record for the *current* revision:
  ``unevaluated``, ``awaiting_ratification``, ``accepted_concerns``,
  ``reviewed_ready``, or ``evaluation_failed``. This axis is computed the same
  way regardless of requirement, so a document policy exempts still reports
  ``unevaluated`` rather than reporting the policy decision as if it were an
  evaluation fact.

Admission is derived from both: only a ``required`` document that is not
``reviewed_ready`` is withheld, and a ``not_required`` document keeps whatever
structural/lifecycle admission result it already had.

Revision identity is ``input_digest`` (title + body + canonical type, from
``semantic_input``). Records and ratifications for any other digest are ignored
outright, which is what makes both the stale-ratification case and the
late-arriving-evaluation case fall out of the derivation rather than needing an
ordering guard: an r1 record simply is not a record *of r2*.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from theforge.eval.semantic_input import build_semantic_evaluation_input
from theforge.eval.semantic_storage import (
    SemanticEvaluationRecord,
    SemanticRatificationRecord,
    SemanticReviewStore,
)
from theforge.eval.semantic_types import STATUS_EVALUATION_FAILED

# ── Policy ──────────────────────────────────────────────────────────────────

#: Types whose documents require a ratified semantic review before
#: implementation. Every type not named here — ``epic``, ``documentation``,
#: ``operator-action``, an untyped document, a manifest file story — is
#: ``not_required`` and keeps its existing structural/lifecycle admission.
SEMANTIC_REVIEW_REQUIRED_TYPES = frozenset({"bug", "enhancement", "task", "spike"})

#: The only lifecycle state in which the requirement applies. A document that
#: is not yet implementation-ready is already refused on structural/lifecycle
#: grounds; requiring a semantic review of it would spend model budget on text
#: that is about to change.
SEMANTIC_REVIEW_REQUIRED_STATE = "implementation_ready"

REQUIREMENT_REQUIRED = "required"
REQUIREMENT_NOT_REQUIRED = "not_required"

# ── Evaluation states ───────────────────────────────────────────────────────

STATE_UNEVALUATED = "unevaluated"
STATE_AWAITING_RATIFICATION = "awaiting_ratification"
STATE_ACCEPTED_CONCERNS = "accepted_concerns"
STATE_REVIEWED_READY = "reviewed_ready"
STATE_EVALUATION_FAILED = "evaluation_failed"

# ── Admission reason codes ──────────────────────────────────────────────────

#: Emitted for BOTH ``unevaluated`` and ``awaiting_ratification``. Neither is a
#: refusal derived from finding content: in both cases the withholding fact is
#: identical — no ratified readiness exists for this revision. Giving unratified
#: concerns their own code would be admission reacting to raw model output,
#: which ADR-0009 clause 6 forbids.
SEMANTIC_NOT_RATIFIED_CODE = "semantic_review_not_ratified"
#: The operator ratified the evaluation and accepted at least one concern.
#: Withheld for this revision only; editing the document clears it.
SEMANTIC_ACCEPTED_CONCERNS_CODE = "semantic_concerns_accepted"
#: The evaluator could not produce an outcome for this revision.
SEMANTIC_EVALUATION_FAILED_CODE = "semantic_evaluation_failed"

SEMANTIC_REASON_CODES = (
    SEMANTIC_NOT_RATIFIED_CODE,
    SEMANTIC_ACCEPTED_CONCERNS_CODE,
    SEMANTIC_EVALUATION_FAILED_CODE,
)


@dataclass(frozen=True)
class SemanticReadiness:
    """The derived semantic state of one document revision, plus its policy axis."""

    issue_ref: str
    input_digest: str
    canonical_type: str | None
    requirement: str
    state: str
    accepted_finding_digests: tuple[str, ...] = ()
    open_finding_digests: tuple[str, ...] = ()
    ratified_at: str = ""
    detail: str = ""

    @property
    def required(self) -> bool:
        return self.requirement == REQUIREMENT_REQUIRED

    @property
    def reviewed_ready(self) -> bool:
        """True when a ratification recorded against *this* revision cleared it."""
        return self.state == STATE_REVIEWED_READY

    @property
    def withholds_admission(self) -> bool:
        """True only when policy requires readiness this revision does not have."""
        return self.required and not self.reviewed_ready

    @property
    def reason_code(self) -> str:
        if not self.withholds_admission:
            return ""
        if self.state == STATE_ACCEPTED_CONCERNS:
            return SEMANTIC_ACCEPTED_CONCERNS_CODE
        if self.state == STATE_EVALUATION_FAILED:
            return SEMANTIC_EVALUATION_FAILED_CODE
        return SEMANTIC_NOT_RATIFIED_CODE

    @property
    def reason_codes(self) -> tuple[str, ...]:
        code = self.reason_code
        return (code,) if code else ()


def semantic_requirement(*, canonical_type: str | None, lifecycle_state: str) -> str:
    """Return ``required`` / ``not_required`` for one type in one lifecycle state."""
    if lifecycle_state != SEMANTIC_REVIEW_REQUIRED_STATE:
        return REQUIREMENT_NOT_REQUIRED
    if canonical_type is None or canonical_type not in SEMANTIC_REVIEW_REQUIRED_TYPES:
        return REQUIREMENT_NOT_REQUIRED
    return REQUIREMENT_REQUIRED


def _ratification_for(
    record: SemanticEvaluationRecord,
    ratifications: list[SemanticRatificationRecord],
) -> SemanticRatificationRecord | None:
    """Return the latest ratification that decides every finding of *record*.

    A ratification that does not cover the current findings (an older
    evaluation of the same revision raised different concerns, say) does not
    speak for this evaluation and is ignored rather than partially applied.
    """
    current = set(record.finding_digests())
    match = None
    for ratification in ratifications:
        decided = ratification.decision_by_digest()
        if set(decided) == current:
            match = ratification
    return match


def _state_detail(state: str, readiness: "SemanticReadiness") -> str:
    if state == STATE_UNEVALUATED:
        return "no semantic evaluation is recorded for the current revision"
    if state == STATE_AWAITING_RATIFICATION:
        return (
            "a semantic evaluation is recorded for the current revision but no "
            "operator ratification is; run `forge ratify-semantic`"
        )
    if state == STATE_ACCEPTED_CONCERNS:
        count = len(readiness.accepted_finding_digests)
        noun = "concern" if count == 1 else "concerns"
        return f"{count} accepted {noun} withhold readiness until the document changes"
    if state == STATE_EVALUATION_FAILED:
        return "the semantic evaluation of the current revision failed"
    return "ratified semantic review passed for the current revision"


def derive_semantic_readiness(
    *,
    issue_ref: str,
    title: str,
    body: str,
    labels: tuple[str, ...] | list[str],
    store: SemanticReviewStore,
    lifecycle_state: str = SEMANTIC_REVIEW_REQUIRED_STATE,
) -> SemanticReadiness:
    """Derive the current revision's semantic readiness from recorded state.

    ``lifecycle_state`` is the caller's structural/lifecycle answer. Callers
    pass ``implementation_ready`` only when the structural verdict already
    admits the document; anything else makes the requirement ``not_required``,
    because the structural refusal is the operative one.

    Nothing here reads model output as a verdict: findings are consulted only
    to check that the operator decided each one, and the decisions are the
    operator's.
    """
    evaluation_input = build_semantic_evaluation_input(title=title, body=body, labels=labels)
    digest = evaluation_input.input_digest
    canonical_type = evaluation_input.canonical_type
    requirement = semantic_requirement(
        canonical_type=canonical_type,
        lifecycle_state=lifecycle_state,
    )

    record = store.latest_successful_record(issue_ref=issue_ref, input_digest=digest)
    if record is None:
        failed = [
            item
            for item in store.records_for_digest(digest)
            if item.issue_ref == issue_ref and item.status == STATUS_EVALUATION_FAILED
        ]
        state = STATE_EVALUATION_FAILED if failed else STATE_UNEVALUATED
        readiness = SemanticReadiness(
            issue_ref=issue_ref,
            input_digest=digest,
            canonical_type=canonical_type,
            requirement=requirement,
            state=state,
        )
        return _with_detail(readiness)

    ratification = _ratification_for(
        record,
        store.ratifications_for_digest(issue_ref=issue_ref, input_digest=digest),
    )
    if ratification is None:
        return _with_detail(
            SemanticReadiness(
                issue_ref=issue_ref,
                input_digest=digest,
                canonical_type=canonical_type,
                requirement=requirement,
                state=STATE_AWAITING_RATIFICATION,
                open_finding_digests=tuple(sorted(record.finding_digests())),
            )
        )

    accepted = ratification.accepted_digests()
    state = STATE_ACCEPTED_CONCERNS if accepted else STATE_REVIEWED_READY
    return _with_detail(
        SemanticReadiness(
            issue_ref=issue_ref,
            input_digest=digest,
            canonical_type=canonical_type,
            requirement=requirement,
            state=state,
            accepted_finding_digests=accepted,
            ratified_at=ratification.ratified_at,
        )
    )


def _with_detail(readiness: SemanticReadiness) -> SemanticReadiness:
    return replace(readiness, detail=_state_detail(readiness.state, readiness))


def semantic_readiness_for_issue(
    *,
    issue_number: int,
    title: str,
    body: str,
    labels: tuple[str, ...] | list[str],
    project_root: Path,
    lifecycle_state: str = SEMANTIC_REVIEW_REQUIRED_STATE,
) -> SemanticReadiness:
    """Shared admission boundary: one derivation, three consumers.

    ``sprint.shape_gate`` (query mode), the manifest issue-expansion pass and
    ``ready_queue`` all reach semantic admission through this function, so a
    disagreement between the surface that advertises work and the gate that
    admits it is a code change rather than a drift (the ADR-0009 clause 3
    property, extended to the semantic stage).
    """
    from theforge.eval.semantic_runner import normalize_issue_ref  # noqa: PLC0415

    return derive_semantic_readiness(
        issue_ref=normalize_issue_ref(issue_number),
        title=title,
        body=body,
        labels=labels,
        store=SemanticReviewStore(project_root),
        lifecycle_state=lifecycle_state,
    )
