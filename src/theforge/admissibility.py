"""Sprint-entry admissibility: the single verdict both the gate and the queue use.

``forge status --ready`` and ``sprint.shape_gate`` answer the same question —
"may this issue enter a sprint?" — and previously answered it from different
data: the queue from the ``ready`` label alone, the gate from a live
``shape_check`` run. That let a surface advertising work as sprint-ready
present issues the gate refused (#2027).

This module holds the classification both call, so a disagreement is a code
change rather than a drift. It is pure: callers supply the issue's title, body
and labels; nothing here fetches from GitHub or reads coordinator state.

Stdlib + ``shape_check`` imports only (``shape_check`` is itself stdlib-only),
so the low-dependency ``ready_queue`` surface can import it freely.
"""

from __future__ import annotations

from dataclasses import dataclass

from .shape_check import Shape, ShapeResult, ShapeVerdict, check
from .shape_check.parsing import extract_ac_section, extract_bullets
from .shape_check.types import VERDICT_DESCRIPTIONS, Severity
from .shape_check.verdict import derive_verdict

NEEDS_GROOMING_LABEL = "needs-grooming"

# operator-action: deliberate non-dispatch type. The label declares an issue
# whose deliverable is human action no dev agent can perform. The gate refuses
# to dispatch these issues to dev cycles — distinct from "wrong shape" skips.
OPERATOR_ACTION_LABEL = "operator-action"
OPERATOR_ACTION_CODE = "operator_action"
OPERATOR_ACTION_LABEL_CONFLICT_CODE = "operator_action_label_conflict"
OPERATOR_ACTION_MISSING_AC_CODE = "operator_action_missing_ac"
# operator-action is mutually exclusive with the runnable type labels.
OPERATOR_ACTION_CONFLICT_LABELS = frozenset({"bug", "enhancement", "epic", "task"})

# No longer emitted by classify_admissibility (ADR-0003 clause 2: a bare
# label refusal with no concomitant blocking finding is invalid). Kept as
# the string identity for historical skip records and skip_taxonomy's
# classification of them.
NEEDS_GROOMING_LABEL_CODE = "needs_grooming_label"

_VALID_CLASSIFIER_MODES = frozenset({"heuristic", "off", "llm"})


@dataclass(frozen=True)
class Admissibility:
    """Whether an issue may enter a sprint, and why not when it may not.

    ``source`` mirrors the shape gate's provenance vocabulary: ``"label"`` when
    a label alone decided the outcome (``needs-grooming``, ``operator-action``),
    ``"local_check"`` when the live ``shape_check`` run did.
    """

    admissible: bool
    source: str
    verdict: str
    verdict_description: str
    reason_codes: tuple[str, ...]
    detail: str
    # True when the issue carries the ``operator-action`` label at all, however
    # it classified. ``--force`` must not override that label, so callers need
    # to see it even when the outcome was a conflict or missing-AC skip.
    operator_action_label: bool = False
    # True only for a clean operator-action issue: deliberate non-dispatch
    # rather than a wrong-shape refusal.
    deliberate_non_dispatch: bool = False
    # The underlying shape_check result, or None when a label short-circuited
    # ahead of the check (operator-action).
    result: ShapeResult | None = None


def resolve_classifier(classifier_mode: str, llm_caller=None) -> str:
    """Return the classifier mode to actually use at sprint time.

    Honors the configured ``shape_check.classifier`` value when supported.
    Falls back to ``heuristic`` when:

    - The mode is unknown (defensive — misconfiguration shouldn't block sprints).
    - The mode is ``llm`` but no ``llm_caller`` is available to refine fuzzy
      reasons. (The ``classifier.classify`` function already silently falls
      back in this case; resolving explicitly keeps the sprint gate honest
      about what it actually ran.)
    """
    if classifier_mode not in _VALID_CLASSIFIER_MODES:
        return "heuristic"
    if classifier_mode == "llm" and llm_caller is None:
        return "heuristic"
    return classifier_mode


def has_acceptance_criteria(body: str) -> bool:
    """Return True if the body has a non-empty ``## Acceptance criteria`` section."""
    section = extract_ac_section(body)
    if not section:
        return False
    return bool(extract_bullets(section))


def blocking_codes(result: ShapeResult) -> list[str]:
    return [r.code for r in result.reasons if r.severity is Severity.BLOCKING]


def _reason_codes(result: ShapeResult, codes: frozenset[str]) -> list[str]:
    return [r.code for r in result.reasons if r.code in codes]


def _reason_detail(result: ShapeResult, fallback: str, *, codes: frozenset[str]) -> str:
    details = [
        reason.detail.strip()
        for reason in result.reasons
        if reason.code in codes and reason.detail.strip()
    ]
    if details:
        return "; ".join(details)
    return fallback


def advisory_reasons(result: ShapeResult) -> list[tuple[str, str]]:
    return [
        (r.code, r.detail.strip())
        for r in result.reasons
        if r.severity is Severity.ADVISORY and r.detail.strip()
    ]


def skip_detail(
    result: ShapeResult,
    fallback: str,
    *,
    include_advisory_when_no_blocking: bool = False,
) -> str:
    blocking_details = [
        reason.detail.strip()
        for reason in result.reasons
        if reason.severity is Severity.BLOCKING and reason.detail.strip()
    ]
    if blocking_details:
        return "; ".join(blocking_details)

    if include_advisory_when_no_blocking:
        reason_details = [
            reason.detail.strip() for reason in result.reasons if reason.detail.strip()
        ]
        if reason_details:
            return "; ".join(reason_details)

    return fallback


def refusal_reason_codes(result: ShapeResult) -> list[str]:
    """Return only the codes that justify a refused sprint-entry decision.

    Refused issues must keep blocking routes and advisory notes structurally
    separate. ``diagnosis_cause_unknown`` is the one refusal driven by an
    advisory-severity local reason, so its typed verdict code is the only
    advisory reason that belongs in the skip channel.
    """
    if result.verdict is ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN:
        codes = _reason_codes(
            result,
            frozenset({ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN.value}),
        )
        return codes or [ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN.value]
    return blocking_codes(result)


def refusal_detail(result: ShapeResult, fallback: str) -> str:
    if result.verdict is ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN:
        return _reason_detail(
            result,
            fallback,
            codes=frozenset({ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN.value}),
        )

    preferred_details: list[str] = []
    other_blocking_details: list[str] = []
    for reason in result.reasons:
        detail = reason.detail.strip()
        if reason.severity is not Severity.BLOCKING or not detail:
            continue
        if derive_verdict((reason,)) is result.verdict:
            preferred_details.append(detail)
        else:
            other_blocking_details.append(detail)

    if preferred_details or other_blocking_details:
        return "; ".join(preferred_details + other_blocking_details)
    return fallback


def _classify_operator_action(body: str, labels: list[str]) -> Admissibility:
    """Return the admissibility of an issue carrying the ``operator-action`` label."""
    label_set = {str(lbl).strip().lower() for lbl in labels}
    conflicts = sorted(label_set & OPERATOR_ACTION_CONFLICT_LABELS)
    if conflicts:
        return Admissibility(
            admissible=False,
            source="label",
            verdict=ShapeVerdict.NEEDS_OPERATOR_ACTION.value,
            verdict_description="",
            reason_codes=(OPERATOR_ACTION_LABEL_CONFLICT_CODE,),
            detail=(
                f"'{OPERATOR_ACTION_LABEL}' conflicts with type label(s) "
                f"{conflicts!r}; pick exactly one issue type."
            ),
            operator_action_label=True,
        )
    if not has_acceptance_criteria(body):
        return Admissibility(
            admissible=False,
            source="label",
            verdict=ShapeVerdict.NEEDS_OPERATOR_ACTION.value,
            verdict_description="",
            reason_codes=(OPERATOR_ACTION_MISSING_AC_CODE,),
            detail=(
                f"'{OPERATOR_ACTION_LABEL}' issues require an "
                "'## Acceptance criteria' section describing the operator deliverable."
            ),
            operator_action_label=True,
        )
    return Admissibility(
        admissible=False,
        source="label",
        verdict=ShapeVerdict.NEEDS_OPERATOR_ACTION.value,
        verdict_description="",
        reason_codes=(OPERATOR_ACTION_CODE,),
        detail="not sprintable; operator deliverable",
        operator_action_label=True,
        deliberate_non_dispatch=True,
    )


def classify_admissibility(
    title: str,
    body: str,
    labels: list[str],
    *,
    classifier_mode: str = "heuristic",
    llm_caller=None,
    trust_local_over_grooming_label: bool = False,
) -> Admissibility:
    """Return whether an issue may enter a sprint, with the verdict explaining why not.

    Ordering mirrors the sprint gate exactly:

    1. ``operator-action`` short-circuits ahead of the shape check. Such an
       issue is not malformed; running the standard check would flag it for
       ``missing_type`` and conflate two distinct operator signals.
    2. A live ``shape_check`` run supplies the verdict for everything else.
    3. A ``needs-grooming`` label refuses with ``source='label'`` only when
       the live check has a concomitant blocking finding to surface; a label
       with no blocking finding is never terminal on its own (ADR-0003
       clause 2) and falls through to the shape verdict below.
    4. ``diagnosis_cause_unknown`` is admissible as a *shape* but not
       implementation-runnable per ADR-0001, so it is refused on the verdict.
    5. Any non-``RUNNABLE`` shape is refused with ``source='local_check'``.

    ``trust_local_over_grooming_label`` suppresses step 3 entirely: the
    caller has authoritatively edited the body (intake remediation) and the
    async relabeler may not have caught up.
    """
    label_set_lc = {str(lbl).strip().lower() for lbl in labels}
    if OPERATOR_ACTION_LABEL in label_set_lc:
        return _classify_operator_action(body, labels)

    local = check(
        title,
        body,
        labels,
        classifier_mode=resolve_classifier(classifier_mode, llm_caller=llm_caller),
        llm_caller=llm_caller,
    )

    if NEEDS_GROOMING_LABEL in labels and not trust_local_over_grooming_label:
        codes = refusal_reason_codes(local)
        if codes:
            verdict = (
                local.verdict
                if local.verdict is not ShapeVerdict.RUNNABLE
                else ShapeVerdict.NEEDS_OPERATOR_ACTION
            )
            return Admissibility(
                admissible=False,
                source="label",
                verdict=verdict.value,
                verdict_description=VERDICT_DESCRIPTIONS[verdict],
                reason_codes=tuple(codes),
                detail=refusal_detail(
                    local,
                    fallback=f"issue carries '{NEEDS_GROOMING_LABEL}' label",
                ),
                result=local,
            )

    if local.verdict is ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN:
        codes = refusal_reason_codes(local)
        return Admissibility(
            admissible=False,
            source="local_check",
            verdict=ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN.value,
            verdict_description=VERDICT_DESCRIPTIONS[ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN],
            reason_codes=tuple(codes),
            detail=refusal_detail(
                local,
                fallback="bug investigation-ready; confirmed cause not yet identified",
            ),
            result=local,
        )

    if local.shape is not Shape.RUNNABLE:
        codes = refusal_reason_codes(local) or [r.code for r in local.reasons]
        verdict = (
            local.verdict
            if local.verdict is not ShapeVerdict.RUNNABLE
            else ShapeVerdict.NEEDS_OPERATOR_ACTION
        )
        return Admissibility(
            admissible=False,
            source="local_check",
            verdict=verdict.value,
            verdict_description=VERDICT_DESCRIPTIONS[verdict],
            reason_codes=tuple(codes),
            detail=refusal_detail(
                local,
                fallback=f"local shape check: {local.suggested_action.value}",
            ),
            result=local,
        )

    return Admissibility(
        admissible=True,
        source="local_check",
        verdict=ShapeVerdict.RUNNABLE.value,
        verdict_description=VERDICT_DESCRIPTIONS[ShapeVerdict.RUNNABLE],
        reason_codes=(),
        detail="",
        result=local,
    )
