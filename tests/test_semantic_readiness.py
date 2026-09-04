"""Ratified semantic readiness: policy, revision scoping, and admission wiring (#2785)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from theforge.admissibility import classify_admissibility
from theforge.eval.semantic_input import build_semantic_evaluation_input
from theforge.eval.semantic_readiness import (
    REQUIREMENT_NOT_REQUIRED,
    REQUIREMENT_REQUIRED,
    SEMANTIC_ACCEPTED_CONCERNS_CODE,
    SEMANTIC_EVALUATION_FAILED_CODE,
    SEMANTIC_NOT_RATIFIED_CODE,
    SEMANTIC_REVIEW_REQUIRED_TYPES,
    STATE_ACCEPTED_CONCERNS,
    STATE_AWAITING_RATIFICATION,
    STATE_EVALUATION_FAILED,
    STATE_REVIEWED_READY,
    STATE_UNEVALUATED,
    derive_semantic_readiness,
    semantic_readiness_for_issue,
)
from theforge.eval.semantic_storage import (
    SemanticConcernDecision,
    SemanticEvaluationRecord,
    SemanticRatificationRecord,
    SemanticReviewStore,
)
from theforge.eval.semantic_types import (
    DECISION_ACCEPTED,
    DECISION_REJECTED,
    OUTCOME_FINDINGS,
    OUTCOME_NO_FINDINGS,
    STATUS_EVALUATION_FAILED,
    STATUS_FINDINGS,
    STATUS_NO_FINDINGS,
    SemanticFinding,
)
from theforge.ready_queue import build_ready_queue
from theforge.shape_check import ShapeVerdict
from theforge.shape_check.skip_taxonomy import SkipCategory, classify_skip
from theforge.sprint.shape_gate import apply_shape_gate

# ── Fixtures ─────────────────────────────────────────────────────────────────

_RUNNABLE_BODY = """## What

Add a CLI flag.

## Why

Users need a way to bypass the gate.

## Example

```text
$ forge sprint --force
[forge] 2 issue(s) flagged by shape gate
```

## Acceptance Criteria

- `forge sprint --force` emits every issue regardless of shape check
- the warning output reports every skipped issue's reason codes
"""

_EDITED_BODY = _RUNNABLE_BODY + "\n- the audit records which issues were forced\n"


def _finding(summary: str) -> SemanticFinding:
    return SemanticFinding(
        summary=summary,
        rationale=f"rationale for {summary}",
        severity="medium",
    )


def _record(
    *,
    issue_ref: str,
    input_digest: str,
    findings: tuple[SemanticFinding, ...] = (),
    status: str | None = None,
) -> SemanticEvaluationRecord:
    if status is None:
        status = STATUS_FINDINGS if findings else STATUS_NO_FINDINGS
    outcome = None
    if status == STATUS_FINDINGS:
        outcome = OUTCOME_FINDINGS
    elif status == STATUS_NO_FINDINGS:
        outcome = OUTCOME_NO_FINDINGS
    return SemanticEvaluationRecord(
        issue_ref=issue_ref,
        canonical_type="enhancement",
        input_digest=input_digest,
        model_id="anthropic/sonnet/cli",
        prompt_contract_version="semantic-review.v1",
        status=status,
        cache_hit=False,
        duration_seconds=1.0,
        cost_usd=0.1,
        outcome=outcome,
        findings=findings,
        failure_detail="model returned unparseable output"
        if status == STATUS_EVALUATION_FAILED
        else None,
    )


def _ratification(
    *,
    issue_ref: str,
    input_digest: str,
    decisions: tuple[SemanticConcernDecision, ...] = (),
) -> SemanticRatificationRecord:
    return SemanticRatificationRecord(
        issue_ref=issue_ref,
        input_digest=input_digest,
        model_id="anthropic/sonnet/cli",
        prompt_contract_version="semantic-review.v1",
        ratified_at="2026-09-04T00:00:00+00:00",
        decisions=decisions,
    )


def _digest(body: str = _RUNNABLE_BODY, labels: tuple[str, ...] = ("enhancement",)) -> str:
    return build_semantic_evaluation_input(
        title="Add a force flag", body=body, labels=labels
    ).input_digest


def _readiness(
    store: SemanticReviewStore,
    *,
    body: str = _RUNNABLE_BODY,
    labels: tuple[str, ...] = ("enhancement",),
    lifecycle_state: str = "implementation_ready",
):
    return derive_semantic_readiness(
        issue_ref="issue-2785",
        title="Add a force flag",
        body=body,
        labels=labels,
        store=store,
        lifecycle_state=lifecycle_state,
    )


# ── Storage ──────────────────────────────────────────────────────────────────


def test_ratifications_round_trip_and_stay_separate_from_evaluations(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    finding = _finding("ambiguous criterion")
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest, findings=(finding,)))
    store.append_ratification(
        _ratification(
            issue_ref="issue-2785",
            input_digest=digest,
            decisions=(
                SemanticConcernDecision(
                    finding_digest=finding.finding_digest, decision=DECISION_REJECTED
                ),
            ),
        )
    )

    assert store.ratifications_path != store.records_path
    assert len(store.iter_records()) == 1
    stored = store.iter_ratifications()
    assert len(stored) == 1
    assert stored[0].decision_by_digest() == {finding.finding_digest: DECISION_REJECTED}
    assert stored[0].accepted_digests() == ()
    # The evaluation record file carries no readiness of its own.
    raw = json.loads(store.records_path.read_text(encoding="utf-8").splitlines()[0])
    assert "decisions" not in raw and "ratified_at" not in raw


def test_concern_decision_rejects_an_unknown_verb() -> None:
    with pytest.raises(ValueError):
        SemanticConcernDecision(finding_digest="abc", decision="maybe")


# ── Policy axis ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", sorted(SEMANTIC_REVIEW_REQUIRED_TYPES))
def test_named_types_require_semantic_review_when_implementation_ready(
    tmp_path: Path, label: str
) -> None:
    readiness = _readiness(SemanticReviewStore(tmp_path), labels=(label,))
    assert readiness.requirement == REQUIREMENT_REQUIRED
    assert readiness.withholds_admission


@pytest.mark.parametrize("label", ["epic", "documentation", "operator-action", "wontfix"])
def test_unnamed_types_are_not_required_and_still_report_their_evaluation_state(
    tmp_path: Path, label: str
) -> None:
    """The two axes stay separate: policy-exempt is not an evaluation status."""
    readiness = _readiness(SemanticReviewStore(tmp_path), labels=(label,))
    assert readiness.requirement == REQUIREMENT_NOT_REQUIRED
    # No record exists, so the evaluation axis says so regardless of policy...
    assert readiness.state == STATE_UNEVALUATED
    # ...and the absence withholds nothing, leaving the structural result intact.
    assert not readiness.withholds_admission
    assert readiness.reason_codes == ()


def test_requirement_does_not_apply_outside_implementation_ready(tmp_path: Path) -> None:
    readiness = _readiness(SemanticReviewStore(tmp_path), lifecycle_state="ungroomed")
    assert readiness.requirement == REQUIREMENT_NOT_REQUIRED
    assert not readiness.withholds_admission


# ── Derived state ────────────────────────────────────────────────────────────


def test_no_recorded_evaluation_reports_unevaluated(tmp_path: Path) -> None:
    readiness = _readiness(SemanticReviewStore(tmp_path))
    assert readiness.state == STATE_UNEVALUATED
    assert not readiness.reviewed_ready
    assert readiness.reason_code == SEMANTIC_NOT_RATIFIED_CODE


def test_clean_evaluation_alone_is_not_reviewed_ready(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    store.append_record(_record(issue_ref="issue-2785", input_digest=_digest()))

    readiness = _readiness(store)
    assert readiness.state == STATE_AWAITING_RATIFICATION
    assert not readiness.reviewed_ready
    assert readiness.withholds_admission


def test_ratified_clean_evaluation_is_reviewed_ready(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest))
    store.append_ratification(_ratification(issue_ref="issue-2785", input_digest=digest))

    readiness = _readiness(store)
    assert readiness.state == STATE_REVIEWED_READY
    assert readiness.reviewed_ready
    assert not readiness.withholds_admission
    assert readiness.reason_codes == ()


def test_all_rejected_concerns_yield_reviewed_ready(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    findings = (_finding("one"), _finding("two"))
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest, findings=findings))
    store.append_ratification(
        _ratification(
            issue_ref="issue-2785",
            input_digest=digest,
            decisions=tuple(
                SemanticConcernDecision(
                    finding_digest=f.finding_digest, decision=DECISION_REJECTED
                )
                for f in findings
            ),
        )
    )

    readiness = _readiness(store)
    assert readiness.state == STATE_REVIEWED_READY
    assert readiness.reviewed_ready


def test_accepted_concern_withholds_readiness_until_the_document_changes(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    accepted, rejected = _finding("real defect"), _finding("not a defect")
    store.append_record(
        _record(issue_ref="issue-2785", input_digest=digest, findings=(accepted, rejected))
    )
    store.append_ratification(
        _ratification(
            issue_ref="issue-2785",
            input_digest=digest,
            decisions=(
                SemanticConcernDecision(
                    finding_digest=accepted.finding_digest, decision=DECISION_ACCEPTED
                ),
                SemanticConcernDecision(
                    finding_digest=rejected.finding_digest, decision=DECISION_REJECTED
                ),
            ),
        )
    )

    at_r1 = _readiness(store)
    assert at_r1.state == STATE_ACCEPTED_CONCERNS
    assert at_r1.accepted_finding_digests == (accepted.finding_digest,)
    assert at_r1.reason_code == SEMANTIC_ACCEPTED_CONCERNS_CODE
    assert at_r1.withholds_admission

    # The edit produces r2; the r1 acceptance no longer speaks for it.
    at_r2 = _readiness(store, body=_EDITED_BODY)
    assert at_r2.state == STATE_UNEVALUATED
    assert at_r2.accepted_finding_digests == ()


def test_ratification_goes_stale_when_the_revision_changes(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest))
    store.append_ratification(_ratification(issue_ref="issue-2785", input_digest=digest))

    assert _readiness(store).state == STATE_REVIEWED_READY
    stale = _readiness(store, body=_EDITED_BODY)
    assert stale.state == STATE_UNEVALUATED
    assert stale.input_digest != digest


def test_a_non_canonical_label_change_leaves_the_ratification_valid(tmp_path: Path) -> None:
    """The digest covers title, body and canonical type — not every label."""
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest))
    store.append_ratification(_ratification(issue_ref="issue-2785", input_digest=digest))

    relabeled = _readiness(store, labels=("enhancement", "p2", "ready"))
    assert relabeled.input_digest == digest
    assert relabeled.state == STATE_REVIEWED_READY


def test_a_late_evaluation_of_r1_does_not_restore_readiness_at_r2(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    r1, r2 = _digest(), _digest(_EDITED_BODY)
    # r2 already exists and is unevaluated; the r1 run only now completes.
    store.append_record(_record(issue_ref="issue-2785", input_digest=r1))
    store.append_ratification(_ratification(issue_ref="issue-2785", input_digest=r1))

    at_r2 = _readiness(store, body=_EDITED_BODY)
    assert at_r2.input_digest == r2
    assert at_r2.state == STATE_UNEVALUATED
    assert at_r2.withholds_admission


def test_failed_evaluation_reports_evaluation_failed(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    store.append_record(
        _record(
            issue_ref="issue-2785",
            input_digest=_digest(),
            status=STATUS_EVALUATION_FAILED,
        )
    )

    readiness = _readiness(store)
    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.reason_code == SEMANTIC_EVALUATION_FAILED_CODE


def test_partial_ratification_does_not_count_as_a_decision(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    one, two = _finding("one"), _finding("two")
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest, findings=(one, two)))
    store.append_ratification(
        _ratification(
            issue_ref="issue-2785",
            input_digest=digest,
            decisions=(
                SemanticConcernDecision(
                    finding_digest=one.finding_digest, decision=DECISION_REJECTED
                ),
            ),
        )
    )

    assert _readiness(store).state == STATE_AWAITING_RATIFICATION


def test_unevaluated_and_awaiting_ratification_share_one_reason_code(tmp_path: Path) -> None:
    """Unratified concerns are not a finding-derived refusal — same code as no record."""
    empty = SemanticReviewStore(tmp_path / "empty")
    with_concerns = SemanticReviewStore(tmp_path / "concerns")
    digest = _digest()
    with_concerns.append_record(
        _record(
            issue_ref="issue-2785",
            input_digest=digest,
            findings=(_finding("a"), _finding("b")),
        )
    )

    unevaluated = _readiness(empty)
    unratified = _readiness(with_concerns)
    assert unevaluated.state == STATE_UNEVALUATED
    assert unratified.state == STATE_AWAITING_RATIFICATION
    assert unevaluated.reason_code == unratified.reason_code == SEMANTIC_NOT_RATIFIED_CODE


# ── Skip taxonomy ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        SEMANTIC_NOT_RATIFIED_CODE,
        SEMANTIC_ACCEPTED_CONCERNS_CODE,
        SEMANTIC_EVALUATION_FAILED_CODE,
    ],
)
def test_semantic_reason_codes_classify_as_a_semantic_gate(code: str) -> None:
    assert classify_skip(code, "local_check").category is SkipCategory.SEMANTIC_GATE


# ── Admission seams ──────────────────────────────────────────────────────────


def _detail(body: str = _RUNNABLE_BODY, labels: list[str] | None = None):
    def fetch(number: int, project_root: Path | None) -> dict:
        return {
            "title": "Add a force flag",
            "body": body,
            "labels": list(labels or ["enhancement"]),
            "state": "OPEN",
            "closedAt": None,
            "stateReason": None,
            "updatedAt": None,
            "lastEditedAt": None,
            "comments": [],
            "timeline": [],
        }

    return fetch


def _gate(store_root: Path, *, force: bool = False, body: str = _RUNNABLE_BODY):
    store = SemanticReviewStore(store_root)

    def readiness(*, issue_number, title, body, labels, project_root):
        return derive_semantic_readiness(
            issue_ref=f"issue-{issue_number}",
            title=title,
            body=body,
            labels=labels,
            store=store,
        )

    return apply_shape_gate(
        [{"number": 2785, "title": "Add a force flag"}],
        None,
        force=force,
        fetch_detail=_detail(body),
        semantic_readiness=readiness,
    )


def test_shape_gate_withholds_an_unevaluated_but_structurally_runnable_issue(
    tmp_path: Path,
) -> None:
    result = _gate(tmp_path)
    assert result.runnable == []
    assert len(result.skipped) == 1
    skip = result.skipped[0]
    assert skip.reason_codes == (SEMANTIC_NOT_RATIFIED_CODE,)
    # The structural verdict is untouched: no structural refusal is claimed.
    assert skip.verdict == ""
    structural = classify_admissibility("Add a force flag", _RUNNABLE_BODY, ["enhancement"])
    assert structural.admissible
    assert structural.verdict == ShapeVerdict.RUNNABLE.value


def test_shape_gate_admits_a_reviewed_ready_issue(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest))
    store.append_ratification(_ratification(issue_ref="issue-2785", input_digest=digest))

    result = _gate(tmp_path)
    assert [issue["number"] for issue in result.runnable] == [2785]
    assert result.runnable[0]["shape_verdict"] == ShapeVerdict.RUNNABLE.value
    assert result.runnable[0]["semantic_state"] == STATE_REVIEWED_READY
    assert result.skipped == []


def test_structural_refusals_never_become_semantic_ones(tmp_path: Path) -> None:
    """A document the structural gate refuses is refused on structural grounds.

    The semantic overlay is consulted only past the structural verdict, so a
    refusal here keeps its structural reason codes and verdict — this stage
    adds concerns, never a new structural rule.
    """

    def readiness(*, issue_number, title, body, labels, project_root):  # pragma: no cover
        raise AssertionError("semantic readiness must not be consulted before admission")

    result = apply_shape_gate(
        [{"number": 2785, "title": "Half an issue"}],
        None,
        fetch_detail=_detail("just a one-liner, no acceptance criteria"),
        semantic_readiness=readiness,
    )
    assert result.runnable == []
    skip = result.skipped[0]
    assert skip.verdict and skip.verdict != ShapeVerdict.RUNNABLE.value
    assert not any(code.startswith("semantic_") for code in skip.reason_codes)


@pytest.mark.parametrize(
    "state",
    [
        STATE_UNEVALUATED,
        STATE_AWAITING_RATIFICATION,
        STATE_EVALUATION_FAILED,
        STATE_ACCEPTED_CONCERNS,
    ],
)
def test_force_does_not_bypass_any_withholding_semantic_state(tmp_path: Path, state: str) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    finding = _finding("real defect")
    if state == STATE_AWAITING_RATIFICATION:
        store.append_record(
            _record(issue_ref="issue-2785", input_digest=digest, findings=(finding,))
        )
    elif state == STATE_EVALUATION_FAILED:
        store.append_record(
            _record(
                issue_ref="issue-2785",
                input_digest=digest,
                status=STATUS_EVALUATION_FAILED,
            )
        )
    elif state == STATE_ACCEPTED_CONCERNS:
        store.append_record(
            _record(issue_ref="issue-2785", input_digest=digest, findings=(finding,))
        )
        store.append_ratification(
            _ratification(
                issue_ref="issue-2785",
                input_digest=digest,
                decisions=(
                    SemanticConcernDecision(
                        finding_digest=finding.finding_digest, decision=DECISION_ACCEPTED
                    ),
                ),
            )
        )

    result = _gate(tmp_path, force=True)
    assert result.runnable == []
    assert result.skipped[0].reason_codes[0].startswith("semantic_")


def test_ready_queue_and_shape_gate_agree_on_the_same_body(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)

    def readiness(*, issue_number, title, body, labels, project_root):
        return derive_semantic_readiness(
            issue_ref=f"issue-{issue_number}",
            title=title,
            body=body,
            labels=labels,
            store=store,
        )

    issues = [
        {
            "number": 2785,
            "title": "Add a force flag",
            "body": _RUNNABLE_BODY,
            "labels": [{"name": "enhancement"}, {"name": "ready"}],
        }
    ]

    entries = build_ready_queue(
        tmp_path, fetch_issues=lambda: issues, semantic_readiness=readiness
    )
    gate = _gate(tmp_path)
    assert entries[0].admissible is False
    assert entries[0].verdict == SEMANTIC_NOT_RATIFIED_CODE
    assert gate.runnable == []

    digest = _digest()
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest))
    store.append_ratification(_ratification(issue_ref="issue-2785", input_digest=digest))

    entries = build_ready_queue(
        tmp_path, fetch_issues=lambda: issues, semantic_readiness=readiness
    )
    assert entries[0].admissible is True
    assert [issue["number"] for issue in _gate(tmp_path).runnable] == [2785]


def test_manifest_issue_entries_reach_the_same_admission_boundary(tmp_path: Path) -> None:
    from theforge.sprint.manifest import SprintManifest, build_tasks_from_manifest

    manifest = SprintManifest(name="s", budget_usd=1.0, stories=[{"issue": 2785}])
    calls: list[int] = []

    def withhold(issue_number: int, project_root: Path):
        calls.append(issue_number)
        return derive_semantic_readiness(
            issue_ref=f"issue-{issue_number}",
            title="Add a force flag",
            body=_RUNNABLE_BODY,
            labels=("enhancement",),
            store=SemanticReviewStore(tmp_path),
        )

    tasks = build_tasks_from_manifest(manifest, tmp_path, semantic_admission=withhold)
    assert calls == [2785]
    assert tasks == []


def test_manifest_file_stories_are_not_required(tmp_path: Path) -> None:
    from theforge.sprint.manifest import SprintManifest, build_tasks_from_manifest

    manifest = SprintManifest(name="s", budget_usd=1.0, stories=["stories/a.md"])

    def withhold(issue_number: int, project_root: Path):  # pragma: no cover - must not run
        raise AssertionError("file stories carry no semantic requirement")

    with patch("theforge.sprint.sources.resolve") as resolve:
        resolve.return_value = (SimpleNamespace(fetch=lambda *a: "task"), "ref", "stories/a.md")
        tasks = build_tasks_from_manifest(manifest, tmp_path, semantic_admission=withhold)
    assert len(tasks) == 1


def test_manifest_admission_does_not_re_decide_the_structural_verdict(tmp_path: Path) -> None:
    """A structurally inadmissible manifest entry is not withheld semantically."""
    from theforge.sprint.manifest import semantic_manifest_admission

    issue = SimpleNamespace(
        number=2785,
        issue_ref="issue-2785",
        title="Add a force flag",
        body="no structure at all",
        labels=("enhancement",),
    )
    with patch("theforge.eval.semantic_runner.load_semantic_issue", return_value=issue):
        assert semantic_manifest_admission(2785, tmp_path) is None


def test_semantic_readiness_for_issue_reads_the_project_store(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest()
    store.append_record(_record(issue_ref="issue-2785", input_digest=digest))

    readiness = semantic_readiness_for_issue(
        issue_number=2785,
        title="Add a force flag",
        body=_RUNNABLE_BODY,
        labels=("enhancement",),
        project_root=tmp_path,
    )
    assert readiness.issue_ref == "issue-2785"
    assert readiness.state == STATE_AWAITING_RATIFICATION
