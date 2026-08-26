"""Tests for the shared sprint-entry admissibility decision (#2027).

``classify_admissibility`` is the single answer to "may this issue enter a
sprint?", called by both ``sprint.shape_gate`` (which enforces it) and
``ready_queue`` (which renders it). These tests pin the decision itself; the
gate's and the queue's use of it are covered by their own modules' tests.
"""

from __future__ import annotations

from theforge.admissibility import (
    NEEDS_GROOMING_LABEL,
    OPERATOR_ACTION_CODE,
    OPERATOR_ACTION_LABEL,
    OPERATOR_ACTION_LABEL_CONFLICT_CODE,
    OPERATOR_ACTION_MISSING_AC_CODE,
    blocking_codes,
    classify_admissibility,
    has_acceptance_criteria,
    refusal_detail,
    resolve_classifier,
    skip_detail,
)
from theforge.shape_check import Reason, Severity, Shape, ShapeResult, ShapeVerdict

# ── Fixtures ────────────────────────────────────────────────────────────────

_RUNNABLE_BODY = """## What

Add a CLI flag.

## Why

Users need a way to bypass the gate.

## Example

    $ forge sprint --force
    [forge] 2 issue(s) flagged by shape gate

## Acceptance Criteria

- `forge sprint --force` emits every issue regardless of shape check
- the warning output reports every skipped issue's reason codes
"""

_ADVISORY_DONE_STATE_BODY = """## What

Add a CLI flag.

## Why

Users need a way to bypass the gate.

## Example

```text
Before:
$ forge sprint
[forge] 2 issue(s) flagged by shape gate

After:
$ forge sprint --force
[forge] sprint started with every issue
```

## Acceptance Criteria

- The toggle is available to operators.
"""

# A bug filing with no `## Diagnosis` section — symptom-only, the shape the
# gate refuses with a BLOCKING `needs_diagnosis` reason.
_UNDIAGNOSED_BUG_BODY = """## Observed behavior

`forge status --ready` lists issues the gate refuses.

## Expected behavior

The listing agrees with the gate.
"""

# A bug that has done the diagnosis work but has not landed a cause:
# investigation-ready, admissible as a *shape*, not implementation-runnable.
_CAUSE_UNKNOWN_BUG_BODY = """## Observed behavior

`forge status --ready` lists issues the gate refuses.

## Expected behavior

The listing agrees with the gate.

## Diagnosis

- **Observed symptom:** the ready listing and the gate disagree.
- **Evidence:** run id `1ff6b0bb7992` — five ready issues, five refusals.
- **Confirmed cause:** unknown
- **Affected code path:** `ready_queue.build_ready_queue`.
- **Fix-success criterion:** the listing and sprint entry cannot disagree.
"""

_DIAGNOSED_BUG_WITH_FIX_NOTES = """## What happened

Contract tests never run in CI.

## What was expected

Provider argv drift is caught before release.

## Diagnosis

- **Observed symptom:** Contract tests never run in CI.
- **Evidence:** CI job logs show the contract target never executes.
- **Ruled out:** Missing test files; the suite is present locally.
- **Confirmed cause:** The sprint runner never invokes the contract target.
- **Affected code path:** sprint.runner dispatch setup.
- **Fix-success criterion:** CI runs the contract target before release.

## Notes

the fix belongs in `runners/api.py`
"""

_CAUSE_UNKNOWN_BUG_WITH_FIX_NOTES = """## Observed behavior

`forge status --ready` lists issues the gate refuses.

## Expected behavior

The listing agrees with the gate.

## Diagnosis

- **Observed symptom:** the ready listing and the gate disagree.
- **Evidence:** run id `1ff6b0bb7992` — five ready issues, five refusals.
- **Confirmed cause:** unknown
- **Affected code path:** `ready_queue.build_ready_queue`.
- **Fix-success criterion:** the listing and sprint entry cannot disagree.

## Notes

the fix belongs in `ready_queue.py`
"""

_SUPERSEDED_CAUSE_UNKNOWN_BUG_BODY = f"""Superseded by #99.

{_CAUSE_UNKNOWN_BUG_BODY}"""

_ENHANCEMENT_WITH_BUG_SHAPE_BODY = """## What happened

`forge status --ready` routes this enhancement through bug diagnosis.

## What was expected

Enhancements written in bug shape are refused as a type/body contradiction.
"""

_OPERATOR_ACTION_BODY = """## What

Rotate the production API token.

## Acceptance criteria

- the token is rotated and the new value is stored in `.forge/.env`
"""


# ── resolve_classifier ──────────────────────────────────────────────────────


def test_resolve_classifier_passes_through_supported_modes() -> None:
    assert resolve_classifier("heuristic") == "heuristic"
    assert resolve_classifier("off") == "off"


def test_resolve_classifier_falls_back_when_llm_has_no_caller() -> None:
    assert resolve_classifier("llm") == "heuristic"
    assert resolve_classifier("llm", llm_caller=None) == "heuristic"


def test_resolve_classifier_passes_through_llm_when_caller_available() -> None:
    assert resolve_classifier("llm", llm_caller=lambda _b, _r: None) == "llm"


def test_resolve_classifier_falls_back_on_unknown_mode() -> None:
    # Misconfiguration must not block sprints.
    assert resolve_classifier("bogus-mode") == "heuristic"


# ── has_acceptance_criteria ─────────────────────────────────────────────────


def test_has_acceptance_criteria_true_for_populated_section() -> None:
    assert has_acceptance_criteria(_RUNNABLE_BODY) is True


def test_has_acceptance_criteria_false_when_section_absent() -> None:
    assert has_acceptance_criteria("## What\n\nSomething.\n") is False


def test_has_acceptance_criteria_false_when_section_has_no_bullets() -> None:
    assert has_acceptance_criteria("## Acceptance criteria\n\nTBD\n") is False


# ── blocking_codes / skip_detail ────────────────────────────────────────────


def _result(*reasons: Reason, shape: Shape = Shape.NEEDS_GROOMING) -> ShapeResult:
    return ShapeResult(shape=shape, reasons=reasons)


def test_blocking_codes_keeps_only_blocking_reasons() -> None:
    result = _result(
        Reason(code="needs_diagnosis", severity=Severity.BLOCKING, detail="no diagnosis"),
        Reason(code="advice", severity=Severity.ADVISORY, detail="consider splitting"),
    )
    assert blocking_codes(result) == ["needs_diagnosis"]


def test_skip_detail_prefers_blocking_details() -> None:
    result = _result(
        Reason(code="a", severity=Severity.BLOCKING, detail="blocking one"),
        Reason(code="b", severity=Severity.BLOCKING, detail="blocking two"),
        Reason(code="c", severity=Severity.ADVISORY, detail="advisory"),
    )
    assert skip_detail(result, "fallback") == "blocking one; blocking two"


def test_skip_detail_falls_back_when_no_blocking_details() -> None:
    result = _result(Reason(code="c", severity=Severity.ADVISORY, detail="advisory"))
    assert skip_detail(result, "fallback") == "fallback"


def test_skip_detail_uses_advisories_when_opted_in() -> None:
    result = _result(Reason(code="c", severity=Severity.ADVISORY, detail="advisory"))
    detail = skip_detail(result, "fallback", include_advisory_when_no_blocking=True)
    assert detail == "advisory"


def test_refusal_detail_prioritizes_reason_matching_selected_verdict() -> None:
    result = ShapeResult(
        shape=Shape.NEEDS_GROOMING,
        reasons=(
            Reason(code="needs_diagnosis", severity=Severity.BLOCKING, detail="diagnosis first"),
            Reason(
                code="type_shape_contradiction",
                severity=Severity.BLOCKING,
                detail="contradiction second",
            ),
        ),
        verdict=ShapeVerdict.NEEDS_GROOMING_TYPE_SHAPE,
    )
    assert refusal_detail(result, "fallback") == "contradiction second; diagnosis first"


# ── classify_admissibility: admissible ──────────────────────────────────────


def test_well_shaped_enhancement_is_admissible() -> None:
    verdict = classify_admissibility("Add a flag", _RUNNABLE_BODY, ["enhancement"])

    assert verdict.admissible is True
    assert verdict.verdict == ShapeVerdict.RUNNABLE.value
    assert verdict.reason_codes == ()
    assert verdict.source == "local_check"
    assert verdict.result is not None


def test_admissible_entry_carries_no_operator_action_flags() -> None:
    verdict = classify_admissibility("Add a flag", _RUNNABLE_BODY, ["enhancement"])
    assert verdict.operator_action_label is False
    assert verdict.deliberate_non_dispatch is False


def test_admissible_bug_keeps_advisory_reasons_in_result() -> None:
    verdict = classify_admissibility(
        "Contract target missing", _DIAGNOSED_BUG_WITH_FIX_NOTES, ["bug"]
    )

    assert verdict.admissible is True
    assert verdict.verdict == ShapeVerdict.RUNNABLE.value
    assert verdict.reason_codes == ()
    assert verdict.result is not None
    assert any(r.code == "bug_fix_location_prescription" for r in verdict.result.reasons)


def test_advisory_only_done_state_is_admissible() -> None:
    verdict = classify_admissibility(
        "Add a flag",
        _ADVISORY_DONE_STATE_BODY,
        ["enhancement"],
    )

    assert verdict.admissible is True
    assert verdict.verdict == ShapeVerdict.RUNNABLE.value
    assert verdict.reason_codes == ()
    assert verdict.result is not None
    assert verdict.result.verdict is ShapeVerdict.RUNNABLE
    assert any(r.code == "no_observable_done_state" for r in verdict.result.reasons)


# ── classify_admissibility: local-check refusals ────────────────────────────


def test_undiagnosed_bug_is_refused_with_needs_diagnosis() -> None:
    verdict = classify_admissibility("Counter is wrong", _UNDIAGNOSED_BUG_BODY, ["bug"])

    assert verdict.admissible is False
    assert verdict.verdict == ShapeVerdict.NEEDS_DIAGNOSIS.value
    assert verdict.source == "local_check"
    assert verdict.reason_codes
    assert verdict.detail
    assert verdict.verdict_description


def test_untyped_issue_is_refused_with_needs_type() -> None:
    verdict = classify_admissibility("Add a flag", _RUNNABLE_BODY, [])

    assert verdict.admissible is False
    assert verdict.verdict == ShapeVerdict.NEEDS_TYPE.value


def test_enhancement_with_bug_shape_prefers_type_shape_verdict() -> None:
    verdict = classify_admissibility(
        "Queue/gate disagreement",
        _ENHANCEMENT_WITH_BUG_SHAPE_BODY,
        ["enhancement"],
    )

    assert verdict.admissible is False
    assert verdict.source == "local_check"
    assert verdict.verdict == ShapeVerdict.NEEDS_GROOMING_TYPE_SHAPE.value
    assert verdict.reason_codes == ("needs_diagnosis", "type_shape_contradiction")
    assert verdict.detail.startswith("enhancement body carries bug-report-shape sections")


def test_investigation_ready_bug_is_refused_as_cause_unknown() -> None:
    # Admissible as a *shape* but not implementation-runnable per ADR-0001:
    # the verdict is the routing signal, not map_shape's binary view.
    verdict = classify_admissibility("Counter is wrong", _CAUSE_UNKNOWN_BUG_BODY, ["bug"])

    assert verdict.result is not None and verdict.result.shape is Shape.RUNNABLE
    assert verdict.admissible is False
    assert verdict.verdict == ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN.value
    assert verdict.source == "local_check"


def test_cause_unknown_refusal_keeps_phrase_advice_out_of_skip_codes() -> None:
    verdict = classify_admissibility(
        "Counter is wrong",
        _CAUSE_UNKNOWN_BUG_WITH_FIX_NOTES,
        ["bug"],
    )

    assert verdict.admissible is False
    assert verdict.reason_codes == ("diagnosis_cause_unknown",)
    assert "fix location" not in verdict.detail
    assert verdict.result is not None
    assert any(r.code == "bug_fix_location_prescription" for r in verdict.result.reasons)


def test_blocking_superseded_beats_cause_unknown_in_refusal_channel() -> None:
    verdict = classify_admissibility(
        "Counter is wrong",
        _SUPERSEDED_CAUSE_UNKNOWN_BUG_BODY,
        ["bug"],
    )

    assert verdict.admissible is False
    assert verdict.verdict == ShapeVerdict.DUPLICATE_OR_STALE.value
    assert verdict.reason_codes == ("superseded",)
    assert verdict.detail == "Issue marks itself as superseded by another issue."
    assert verdict.result is not None
    assert any(r.code == "diagnosis_cause_unknown" for r in verdict.result.reasons)


def test_missing_type_beats_cause_unknown_in_refusal_channel() -> None:
    verdict = classify_admissibility(
        "Counter is wrong",
        _CAUSE_UNKNOWN_BUG_BODY,
        [],
    )

    assert verdict.admissible is False
    assert verdict.verdict == ShapeVerdict.NEEDS_TYPE.value
    assert verdict.reason_codes == ("missing_type",)
    assert "type label" in verdict.detail
    assert verdict.result is not None
    assert any(r.code == "diagnosis_cause_unknown" for r in verdict.result.reasons)


# ── classify_admissibility: needs-grooming label ────────────────────────────


def test_needs_grooming_label_admits_an_otherwise_runnable_issue() -> None:
    # ADR-0003 clause 2: a label-only refusal (no concomitant local blocking
    # finding) is invalid and must not drop an issue.
    verdict = classify_admissibility(
        "Add a flag", _RUNNABLE_BODY, ["enhancement", NEEDS_GROOMING_LABEL]
    )

    assert verdict.admissible is True
    assert verdict.source == "local_check"
    assert verdict.reason_codes == ()


def test_needs_grooming_label_surfaces_live_blocking_findings() -> None:
    verdict = classify_admissibility(
        "Counter is wrong", _UNDIAGNOSED_BUG_BODY, ["bug", NEEDS_GROOMING_LABEL]
    )

    assert verdict.admissible is False
    assert verdict.source == "label"
    assert verdict.verdict == ShapeVerdict.NEEDS_DIAGNOSIS.value
    assert "needs_diagnosis" in verdict.reason_codes


def test_trust_local_over_grooming_label_readmits_a_remediated_issue() -> None:
    # Intake remediation just rewrote the body; the async relabeler may not
    # have caught up, so the live check is trusted instead of the label.
    verdict = classify_admissibility(
        "Add a flag",
        _RUNNABLE_BODY,
        ["enhancement", NEEDS_GROOMING_LABEL],
        trust_local_over_grooming_label=True,
    )

    assert verdict.admissible is True
    assert verdict.verdict == ShapeVerdict.RUNNABLE.value


def test_trust_local_override_does_not_readmit_a_still_malformed_issue() -> None:
    verdict = classify_admissibility(
        "Counter is wrong",
        _UNDIAGNOSED_BUG_BODY,
        ["bug", NEEDS_GROOMING_LABEL],
        trust_local_over_grooming_label=True,
    )

    assert verdict.admissible is False
    assert verdict.source == "local_check"
    assert verdict.verdict == ShapeVerdict.NEEDS_DIAGNOSIS.value


# ── classify_admissibility: operator-action ─────────────────────────────────


def test_clean_operator_action_is_deliberate_non_dispatch() -> None:
    verdict = classify_admissibility(
        "Rotate the token", _OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL]
    )

    assert verdict.admissible is False
    assert verdict.operator_action_label is True
    assert verdict.deliberate_non_dispatch is True
    assert verdict.reason_codes == (OPERATOR_ACTION_CODE,)
    assert verdict.source == "label"


def test_operator_action_short_circuits_ahead_of_the_shape_check() -> None:
    # Running the standard check would flag missing_type (operator-action is
    # mutually exclusive with the runnable type labels), conflating two
    # distinct operator signals.
    verdict = classify_admissibility(
        "Rotate the token", _OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL]
    )
    assert verdict.result is None


def test_operator_action_with_type_label_is_a_conflict_not_a_non_dispatch() -> None:
    for conflicting in ("bug", "enhancement", "epic", "task"):
        verdict = classify_admissibility(
            "Rotate the token",
            _OPERATOR_ACTION_BODY,
            [OPERATOR_ACTION_LABEL, conflicting],
        )
        assert verdict.reason_codes == (OPERATOR_ACTION_LABEL_CONFLICT_CODE,), conflicting
        assert verdict.deliberate_non_dispatch is False, conflicting
        assert verdict.operator_action_label is True, conflicting


def test_operator_action_without_acceptance_criteria_is_refused_as_missing_ac() -> None:
    verdict = classify_admissibility(
        "Rotate the token", "## What\n\nRotate it.\n", [OPERATOR_ACTION_LABEL]
    )

    assert verdict.reason_codes == (OPERATOR_ACTION_MISSING_AC_CODE,)
    assert verdict.deliberate_non_dispatch is False
    assert verdict.operator_action_label is True


def test_operator_action_label_matching_ignores_case_and_whitespace() -> None:
    verdict = classify_admissibility(
        "Rotate the token", _OPERATOR_ACTION_BODY, [f"  {OPERATOR_ACTION_LABEL.upper()} "]
    )
    assert verdict.operator_action_label is True
