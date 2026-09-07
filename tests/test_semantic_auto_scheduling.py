"""Automatic semantic evaluation at admission time (#2907).

The gate used to withhold a policy-required document for want of an evaluation
nothing had been scheduled to perform. These tests hold the line between the two
things that change and the many that must not: *whether* an evaluation happens is
now automatic, *what a result means* is not.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from theforge.agent_types import AgentResult
from theforge.config.types import ModelProfile
from theforge.eval.semantic_auto import ensure_semantic_evaluation
from theforge.eval.semantic_input import build_semantic_evaluation_input
from theforge.eval.semantic_prompt import PROMPT_CONTRACT_VERSION
from theforge.eval.semantic_readiness import (
    SEMANTIC_EVALUATION_FAILED_CODE,
    SEMANTIC_NOT_RATIFIED_CODE,
    STATE_AWAITING_RATIFICATION,
    STATE_EVALUATION_FAILED,
    STATE_REVIEWED_READY,
    STATE_UNEVALUATED,
)
from theforge.eval.semantic_storage import (
    BASELINE_PROVENANCE_AUTOMATIC,
    BASELINE_PROVENANCE_HUMAN,
    SemanticConcernDecision,
    SemanticEvaluationRecord,
    SemanticRatificationRecord,
    SemanticReviewStore,
)
from theforge.eval.semantic_types import (
    DECISION_REJECTED,
    OUTCOME_NO_FINDINGS,
    STATUS_EVALUATION_FAILED,
    STATUS_NO_FINDINGS,
)

# Captured at import, before conftest's ``_neutral_semantic_readiness_overlay``
# replaces these module attributes: the suite-wide fixture keeps the overlay out
# of tests that are not about it, and these tests *are* about it.
from theforge.eval.semantic_auto import (  # isort: skip
    semantic_dispatch_withholding as _live_dispatch_withholding,
)
from theforge.ready_queue import _semantic_readiness as _live_ready_queue_readiness

from theforge.sprint.manifest import (  # isort: skip
    semantic_manifest_admission as _live_manifest_admission,
)

TITLE = "Add an automatic semantic evaluation"

BODY = """## What

Schedule the semantic evaluator when policy requires it.

## Why

The gate withholds work nothing has been asked to do.

## Acceptance Criteria

- a required, unevaluated revision is evaluated without an operator command
- an unchanged revision reuses the record it already has
"""

EDITED_BODY = BODY + "\n- a changed revision is evaluated in its own right\n"

NO_FINDINGS_OUTPUT = json.dumps({"outcome": OUTCOME_NO_FINDINGS, "findings": []})
FINDINGS_OUTPUT = json.dumps(
    {
        "outcome": "FINDINGS",
        "findings": [
            {
                "summary": "the second criterion restates the first",
                "rationale": "both describe reuse of an existing record",
                "severity": "medium",
            }
        ],
    }
)


def _profile() -> ModelProfile:
    return ModelProfile(
        name="preflight",
        cli="claude",
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=("Read", "Grep"),
        phase="preflight",
    )


def _agent_result(output: str, *, success: bool = True) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=0.02,
        exit_code=0 if success else 1,
        raw={},
        profile_name="preflight",
        model_used="sonnet",
        transport_used="cli",
        cost_provenance="estimated",
    )


class _Runner:
    """Agent runner stub that counts invocations."""

    def __init__(self, output: str = NO_FINDINGS_OUTPUT, *, raises: bool = False) -> None:
        self.output = output
        self.raises = raises
        self.calls = 0

    def __call__(self, **_kwargs) -> AgentResult:
        self.calls += 1
        if self.raises:
            raise RuntimeError("agent launcher exploded")
        return _agent_result(self.output)


def _schedule(
    tmp_path: Path,
    runner: _Runner,
    *,
    body: str = BODY,
    labels: tuple[str, ...] = ("enhancement",),
    lifecycle_state: str = "implementation_ready",
    store: SemanticReviewStore | None = None,
):
    return ensure_semantic_evaluation(
        issue_number=2907,
        title=TITLE,
        body=body,
        labels=labels,
        project_root=tmp_path,
        secrets=None,
        profile=_profile(),
        lifecycle_state=lifecycle_state,
        store=store,
        agent_runner=runner,
    )


def _digest(body: str = BODY, labels: tuple[str, ...] = ("enhancement",)) -> str:
    return build_semantic_evaluation_input(title=TITLE, body=body, labels=labels).input_digest


# ── AC1: required + unevaluated is evaluated; not-required is left alone ──────


def test_required_unevaluated_revision_is_evaluated_without_an_operator_command(
    tmp_path: Path,
) -> None:
    runner = _Runner()

    readiness = _schedule(tmp_path, runner)

    assert runner.calls == 1
    store = SemanticReviewStore(tmp_path)
    records = store.records_for_digest(_digest())
    assert [record.status for record in records] == [STATUS_NO_FINDINGS]
    # A clean evaluation is on record but admission still awaits the operator.
    assert readiness.state == STATE_AWAITING_RATIFICATION
    assert readiness.withholds_admission
    assert readiness.reason_code == SEMANTIC_NOT_RATIFIED_CODE


def test_policy_not_required_document_is_not_evaluated_and_admission_is_unchanged(
    tmp_path: Path,
) -> None:
    runner = _Runner()

    readiness = _schedule(tmp_path, runner, labels=("documentation",))

    assert runner.calls == 0
    assert SemanticReviewStore(tmp_path).iter_records() == []
    assert not readiness.withholds_admission


def test_a_record_does_not_change_admission_of_a_not_required_document(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    digest = _digest(labels=("documentation",))
    store.append_record(
        SemanticEvaluationRecord(
            issue_ref="issue-2907",
            canonical_type="documentation",
            input_digest=digest,
            model_id="anthropic/sonnet/cli",
            prompt_contract_version=PROMPT_CONTRACT_VERSION,
            status=STATUS_EVALUATION_FAILED,
            cache_hit=False,
            duration_seconds=0.0,
            cost_usd=0.0,
            failure_detail="unparseable",
        )
    )
    runner = _Runner()

    readiness = _schedule(tmp_path, runner, labels=("documentation",), store=store)

    assert runner.calls == 0
    assert not readiness.withholds_admission


def test_a_document_that_is_not_implementation_ready_is_not_evaluated(tmp_path: Path) -> None:
    runner = _Runner()

    readiness = _schedule(tmp_path, runner, lifecycle_state="needs_grooming")

    assert runner.calls == 0
    assert not readiness.withholds_admission


# ── AC2: an evaluation is bound to the revision that occasioned it ────────────


def test_a_changed_revision_does_not_inherit_the_prior_evaluation(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    first = _Runner()
    _schedule(tmp_path, first, store=store)
    assert first.calls == 1

    second = _Runner()
    readiness = _schedule(tmp_path, second, body=EDITED_BODY, store=store)

    assert readiness.input_digest == _digest(EDITED_BODY)
    assert readiness.input_digest != _digest()
    # The edited revision was evaluated in its own right, not carried forward.
    assert second.calls == 1
    assert len(store.records_for_digest(_digest(EDITED_BODY))) == 1


def test_the_evaluated_revision_is_the_one_handed_in_not_a_re_fetched_one(
    tmp_path: Path,
) -> None:
    """The evaluator's ``gh`` seam replays the caller's revision, so no re-fetch races an edit."""

    def _explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("automatic scheduling must not re-fetch the issue")

    runner = _Runner()
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("theforge.eval.semantic_runner._gh_issue_view", _explode)
        readiness = _schedule(tmp_path, runner, body=EDITED_BODY)

    assert runner.calls == 1
    assert readiness.input_digest == _digest(EDITED_BODY)


# ── AC3: ratification semantics are untouched ────────────────────────────────


def test_raised_findings_await_ratification_and_no_ratification_is_recorded(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    runner = _Runner(FINDINGS_OUTPUT)

    readiness = _schedule(tmp_path, runner, store=store)

    assert readiness.state == STATE_AWAITING_RATIFICATION
    assert readiness.withholds_admission
    assert readiness.open_finding_digests
    assert store.iter_ratifications() == []


def test_an_operator_ratification_of_the_automatic_record_clears_the_revision(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(FINDINGS_OUTPUT), store=store)
    record = store.latest_successful_record(issue_ref="issue-2907", input_digest=_digest())
    assert record is not None

    store.append_ratification(
        SemanticRatificationRecord(
            issue_ref="issue-2907",
            input_digest=_digest(),
            model_id=record.model_id,
            prompt_contract_version=record.prompt_contract_version,
            ratified_at="2026-09-06T00:00:00+00:00",
            decisions=tuple(
                SemanticConcernDecision(finding_digest=digest, decision=DECISION_REJECTED)
                for digest in record.finding_digests()
            ),
        )
    )

    later = _Runner()
    readiness = _schedule(tmp_path, later, store=store)
    assert readiness.state == STATE_REVIEWED_READY
    assert not readiness.withholds_admission
    assert later.calls == 0


# ── AC4: no failure mode reads as evaluated-clean ────────────────────────────


def test_an_agent_that_raises_leaves_the_revision_failed_and_withheld(tmp_path: Path) -> None:
    runner = _Runner(raises=True)

    readiness = _schedule(tmp_path, runner)

    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission
    assert readiness.reason_code == SEMANTIC_EVALUATION_FAILED_CODE
    records = SemanticReviewStore(tmp_path).records_for_digest(_digest())
    assert [record.status for record in records] == [STATUS_EVALUATION_FAILED]


def test_unparseable_output_is_a_failure_rather_than_a_known_result(tmp_path: Path) -> None:
    readiness = _schedule(tmp_path, _Runner("I could not comply."))

    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission


def test_an_invocation_that_cannot_be_attempted_is_recorded_as_failed(tmp_path: Path) -> None:
    """A profile with no resolvable model identity cannot be attempted at all."""
    unresolvable = ModelProfile(
        name="broken",
        cli=None,
        provider=None,
        model="",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=("Read",),
        phase="preflight",
    )
    runner = _Runner()

    readiness = ensure_semantic_evaluation(
        issue_number=2907,
        title=TITLE,
        body=BODY,
        labels=("enhancement",),
        project_root=tmp_path,
        secrets=None,
        profile=unresolvable,
        agent_runner=runner,
    )

    assert runner.calls == 0
    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission
    records = SemanticReviewStore(tmp_path).records_for_digest(_digest())
    assert [record.status for record in records] == [STATUS_EVALUATION_FAILED]
    assert "could not be attempted" in (records[0].failure_detail or "")


# ── AC5: bounded by the evidence it produces ─────────────────────────────────


def test_an_unchanged_revision_reuses_its_record_rather_than_spending_again(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    runner = _Runner()

    for _ in range(3):
        _schedule(tmp_path, runner, store=store)

    assert runner.calls == 1
    assert len(store.records_for_digest(_digest())) == 1


def test_a_recorded_failure_counts_as_the_revisions_attempt(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(raises=True), store=store)

    second = _Runner()
    readiness = _schedule(tmp_path, second, store=store)

    assert second.calls == 0
    assert readiness.state == STATE_EVALUATION_FAILED


def test_changing_the_configured_model_does_not_buy_a_second_evaluation(
    tmp_path: Path,
) -> None:
    """AC5 bounds spend per revision per prompt contract, not per model."""
    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(), store=store)

    other_model = ModelProfile(
        name="preflight-alt",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=("Read",),
        phase="preflight",
    )
    runner = _Runner()
    ensure_semantic_evaluation(
        issue_number=2907,
        title=TITLE,
        body=BODY,
        labels=("enhancement",),
        project_root=tmp_path,
        secrets=None,
        profile=other_model,
        store=store,
        agent_runner=runner,
    )

    assert runner.calls == 0
    assert len(store.records_for_digest(_digest())) == 1


def test_a_peer_holding_the_scheduling_lock_stops_a_second_invocation(
    tmp_path: Path,
) -> None:
    """Two admissions observing "no attempt" must not both spend."""
    import fcntl

    from theforge.eval import semantic_auto

    lock_dir = tmp_path / semantic_auto.SEMANTIC_LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = semantic_auto._lock_key("issue-2907", _digest(), PROMPT_CONTRACT_VERSION)
    held = (lock_dir / f"{key}.lock").open("a+")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        runner = _Runner()
        readiness = _schedule(tmp_path, runner)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert runner.calls == 0
    assert readiness.state == STATE_UNEVALUATED
    assert readiness.withholds_admission


# ── Baselines: automatic freeze is scaffolding, not a calibration claim ───────


def test_the_automatic_path_freezes_an_empty_baseline_marked_automatic(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(), store=store)

    baseline = store.frozen_baseline(_digest())
    assert baseline is not None
    assert baseline.defect_ids == ()
    assert baseline.provenance == BASELINE_PROVENANCE_AUTOMATIC


def test_an_existing_human_baseline_is_preserved_by_automatic_scheduling(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    store.freeze_baseline(
        issue_ref="issue-2907",
        input_digest=_digest(),
        canonical_type="enhancement",
        defect_ids=("DEF-1",),
    )

    _schedule(tmp_path, _Runner(), store=store)

    baseline = store.frozen_baseline(_digest())
    assert baseline is not None
    assert baseline.defect_ids == ("DEF-1",)
    assert baseline.provenance == BASELINE_PROVENANCE_HUMAN


def test_a_human_baseline_supersedes_an_automatically_frozen_empty_one(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(), store=store)

    baseline, created = store.freeze_baseline(
        issue_ref="issue-2907",
        input_digest=_digest(),
        canonical_type="enhancement",
        defect_ids=("DEF-1",),
    )

    assert created is True
    assert baseline.defect_ids == ("DEF-1",)
    assert store.frozen_baseline(_digest()).provenance == BASELINE_PROVENANCE_HUMAN


def test_a_human_baseline_still_cannot_be_silently_rewritten(tmp_path: Path) -> None:
    store = SemanticReviewStore(tmp_path)
    store.freeze_baseline(
        issue_ref="issue-2907",
        input_digest=_digest(),
        canonical_type="enhancement",
        defect_ids=("DEF-1",),
    )
    with pytest.raises(ValueError, match="already frozen"):
        store.freeze_baseline(
            issue_ref="issue-2907",
            input_digest=_digest(),
            canonical_type="enhancement",
            defect_ids=("DEF-2",),
        )


# ── Storage seams: the manual path keeps its own cache semantics ─────────────


def test_latest_attempt_for_revision_sees_failures_that_the_identity_cache_hides(
    tmp_path: Path,
) -> None:
    store = SemanticReviewStore(tmp_path)
    store.append_record(
        SemanticEvaluationRecord(
            issue_ref="issue-2907",
            canonical_type="enhancement",
            input_digest=_digest(),
            model_id="anthropic/sonnet/cli",
            prompt_contract_version=PROMPT_CONTRACT_VERSION,
            status=STATUS_EVALUATION_FAILED,
            cache_hit=False,
            duration_seconds=0.0,
            cost_usd=0.0,
            failure_detail="unparseable",
        )
    )

    assert (
        store.latest_attempt_for_revision(
            issue_ref="issue-2907",
            input_digest=_digest(),
            prompt_contract_version=PROMPT_CONTRACT_VERSION,
        )
        is not None
    )
    # The manual path's cache is unchanged: a failure is not a cache hit, so
    # `forge review-semantic` re-invokes rather than replaying the failure.
    assert (
        store.latest_record_for_identity(
            input_digest=_digest(),
            model_id="anthropic/sonnet/cli",
            prompt_contract_version=PROMPT_CONTRACT_VERSION,
        )
        is None
    )


# ── AC6: the operator command is unchanged ───────────────────────────────────


def test_manual_review_still_runs_for_a_policy_not_required_document(tmp_path: Path) -> None:
    from theforge.eval.semantic_runner import review_issue_semantically

    runner = _Runner()
    payload = json.dumps({"title": TITLE, "body": BODY, "labels": [{"name": "documentation"}]})
    result = review_issue_semantically(
        issue_number=2907,
        project_root=tmp_path,
        secrets=None,
        profile=_profile(),
        baseline_defect_ids=(),
        gh_issue_view=lambda _n, _r: subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=payload, stderr=""
        ),
        agent_runner=runner,
    )

    assert runner.calls == 1
    assert result.record.status == STATUS_NO_FINDINGS
    assert result.baseline.provenance == BASELINE_PROVENANCE_HUMAN


def test_manual_review_after_a_recorded_failure_re_invokes_the_agent(tmp_path: Path) -> None:
    from theforge.eval.semantic_runner import review_issue_semantically

    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(raises=True), store=store)

    runner = _Runner()
    payload = json.dumps({"title": TITLE, "body": BODY, "labels": [{"name": "enhancement"}]})
    result = review_issue_semantically(
        issue_number=2907,
        project_root=tmp_path,
        secrets=None,
        profile=_profile(),
        store=store,
        gh_issue_view=lambda _n, _r: subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=payload, stderr=""
        ),
        agent_runner=runner,
    )

    assert runner.calls == 1
    assert result.record.status == STATUS_NO_FINDINGS


# ── Seam wiring: sprint query mode, manifest, ready queue ────────────────────


def test_shape_gate_admits_after_the_bound_scheduler_records_a_clean_ratified_review(
    tmp_path: Path,
) -> None:
    """The gate's readiness seam is where scheduling happens in query mode."""
    from theforge.cli.sprint import _semantic_readiness_scheduler
    from theforge.sprint.shape_gate import apply_shape_gate

    config = SimpleNamespace(project_root=tmp_path, secrets=None, preflight_profile=_profile())
    runner = _Runner()
    scheduler = _semantic_readiness_scheduler(config)

    def _bound(**kwargs):
        from theforge.eval.semantic_auto import ensure_semantic_evaluation as _ensure

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(
                "theforge.eval.semantic_auto.ensure_semantic_evaluation",
                lambda **kw: _ensure(**kw, agent_runner=runner),
            )
            return scheduler(**kwargs)

    def _fetch(number, _root):
        return {
            "title": TITLE,
            "body": BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "closedAt": None,
            "stateReason": None,
            "updatedAt": None,
            "lastEditedAt": None,
            "comments": [],
            "timeline": [],
        }

    result = apply_shape_gate(
        [{"number": 2907, "title": TITLE}],
        tmp_path,
        fetch_detail=_fetch,
        semantic_readiness=_bound,
    )

    # The evaluation ran, and the issue is still withheld pending ratification.
    assert runner.calls == 1
    assert result.runnable == []
    assert [entry.reason_codes for entry in result.skipped] == [(SEMANTIC_NOT_RATIFIED_CODE,)]
    assert SemanticReviewStore(tmp_path).records_for_digest(_digest())


def test_manifest_file_stories_never_reach_semantic_admission(tmp_path: Path) -> None:
    from theforge.sprint.manifest import SprintManifest, build_tasks_from_manifest

    calls: list[int] = []

    def _admit(number, _root):
        calls.append(number)
        return None

    story = tmp_path / "story.md"
    story.write_text(
        "---\nname: A story\nslug: a-story\n---\n\n## Acceptance Criteria\n\n- it runs\n",
        encoding="utf-8",
    )
    manifest = SprintManifest(
        name="s",
        budget_usd=1.0,
        stories=["story.md"],
        max_parallel=1,
    )

    build_tasks_from_manifest(manifest, tmp_path, semantic_admission=_admit)

    assert calls == []


def test_manifest_issue_admission_without_config_stays_read_only(tmp_path: Path) -> None:
    """Callers resolving a manifest outside a run schedule nothing."""

    called: list[str] = []

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "theforge.eval.semantic_runner.load_semantic_issue",
            lambda **_kw: SimpleNamespace(
                issue_ref="issue-2907", title=TITLE, body=BODY, labels=("enhancement",)
            ),
        )
        patcher.setattr(
            "theforge.eval.semantic_auto.ensure_semantic_evaluation",
            lambda **_kw: called.append("scheduled"),
        )
        withheld = _live_manifest_admission(2907, tmp_path)

    assert called == []
    assert withheld is not None
    assert withheld.reason_code == SEMANTIC_NOT_RATIFIED_CODE


def test_manifest_issue_admission_with_config_schedules_the_evaluation(
    tmp_path: Path,
) -> None:

    runner = _Runner()
    config = SimpleNamespace(project_root=tmp_path, secrets=None, preflight_profile=_profile())

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "theforge.eval.semantic_runner.load_semantic_issue",
            lambda **_kw: SimpleNamespace(
                issue_ref="issue-2907", title=TITLE, body=BODY, labels=("enhancement",)
            ),
        )
        from theforge.eval.semantic_auto import ensure_semantic_evaluation as _ensure

        patcher.setattr(
            "theforge.eval.semantic_auto.ensure_semantic_evaluation",
            lambda **kw: _ensure(**kw, agent_runner=runner),
        )
        withheld = _live_manifest_admission(2907, tmp_path, config)

    assert runner.calls == 1
    # Evaluated, and still withheld until an operator ratifies it.
    assert withheld is not None
    assert withheld.state == STATE_AWAITING_RATIFICATION


def test_a_structurally_refused_issue_entry_is_not_evaluated(tmp_path: Path) -> None:

    called: list[str] = []
    config = SimpleNamespace(project_root=tmp_path, secrets=None, preflight_profile=_profile())

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "theforge.eval.semantic_runner.load_semantic_issue",
            lambda **_kw: SimpleNamespace(
                issue_ref="issue-2907",
                title=TITLE,
                body="no sections at all",
                labels=("enhancement", "needs-grooming"),
            ),
        )
        patcher.setattr(
            "theforge.eval.semantic_auto.ensure_semantic_evaluation",
            lambda **_kw: called.append("scheduled"),
        )
        withheld = _live_manifest_admission(2907, tmp_path, config)

    assert called == []
    assert withheld is None


def test_listing_a_required_unevaluated_issue_spends_nothing(tmp_path: Path) -> None:
    """A status surface reports what admission recorded; it never schedules."""
    from theforge.ready_queue import build_ready_queue

    def _runner_must_not_run(**_kwargs):  # pragma: no cover - must never run
        raise AssertionError("forge status --ready must not invoke an agent")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("theforge.eval.semantic_runner.run_agent", _runner_must_not_run)
        entries = build_ready_queue(
            tmp_path,
            semantic_readiness=_live_ready_queue_readiness,
            fetch_issues=lambda: [
                {
                    "number": 2907,
                    "title": TITLE,
                    "body": BODY,
                    "labels": [{"name": "enhancement"}],
                }
            ],
        )

    assert [entry.verdict for entry in entries] == [SEMANTIC_NOT_RATIFIED_CODE]
    assert SemanticReviewStore(tmp_path).iter_records() == []


# ── Review iteration 1: identity, storage failure, locking, revision handoff ──


def test_two_issues_with_identical_content_each_get_their_own_record(
    tmp_path: Path,
) -> None:
    """The content cache is shared; the record naming an issue is not.

    Two documents can be word-for-word identical, which makes their input
    digests identical too. Replaying the cached outcome under the first issue's
    reference left the second with no record of its own — evaluated as far as
    spend was concerned, unevaluated as far as admission was concerned, forever.
    """
    store = SemanticReviewStore(tmp_path)
    first = _Runner()
    ensure_semantic_evaluation(
        issue_number=2907,
        title=TITLE,
        body=BODY,
        labels=("enhancement",),
        project_root=tmp_path,
        secrets=None,
        profile=_profile(),
        store=store,
        agent_runner=first,
    )

    second = _Runner()
    readiness = ensure_semantic_evaluation(
        issue_number=3001,
        title=TITLE,
        body=BODY,
        labels=("enhancement",),
        project_root=tmp_path,
        secrets=None,
        profile=_profile(),
        store=store,
        agent_runner=second,
    )

    # The outcome is reused (no second agent call), but it is recorded as the
    # second issue's own evaluation.
    assert first.calls == 1
    assert second.calls == 0
    assert readiness.issue_ref == "issue-3001"
    assert readiness.state == STATE_AWAITING_RATIFICATION
    replayed = store.latest_successful_record(issue_ref="issue-3001", input_digest=_digest())
    assert replayed is not None
    assert replayed.cache_hit is True
    assert replayed.canonical_type == "enhancement"
    # And the first issue still has its own.
    assert store.latest_successful_record(issue_ref="issue-2907", input_digest=_digest())


def test_manual_review_of_a_content_twin_records_under_the_reviewed_issue(
    tmp_path: Path,
) -> None:
    from theforge.eval.semantic_runner import review_issue_semantically

    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(), store=store)

    payload = json.dumps({"title": TITLE, "body": BODY, "labels": [{"name": "enhancement"}]})
    runner = _Runner()
    result = review_issue_semantically(
        issue_number=3001,
        project_root=tmp_path,
        secrets=None,
        profile=_profile(),
        store=store,
        gh_issue_view=lambda _n, _r: subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=payload, stderr=""
        ),
        agent_runner=runner,
    )

    assert runner.calls == 0
    assert result.record.cache_hit is True
    assert result.record.issue_ref == "issue-3001"


def test_unreadable_audit_records_withhold_rather_than_admit(tmp_path: Path) -> None:
    """Unreadable audit state is not evidence of readiness."""

    class _BrokenStore(SemanticReviewStore):
        def iter_records(self):
            raise OSError("records.jsonl is unreadable")

    runner = _Runner()
    readiness = _schedule(tmp_path, runner, store=_BrokenStore(tmp_path))

    assert runner.calls == 0
    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission
    assert readiness.reason_code == SEMANTIC_EVALUATION_FAILED_CODE


def test_unreadable_audit_records_leave_a_not_required_document_alone(
    tmp_path: Path,
) -> None:
    class _BrokenStore(SemanticReviewStore):
        def iter_records(self):
            raise OSError("records.jsonl is unreadable")

    readiness = _schedule(
        tmp_path, _Runner(), labels=("documentation",), store=_BrokenStore(tmp_path)
    )

    assert not readiness.withholds_admission


def test_a_shape_gate_reading_a_broken_store_skips_rather_than_admits(
    tmp_path: Path,
) -> None:
    """The gate's fail-open handler must never see a storage failure (AC4)."""
    from theforge.cli.sprint import _semantic_readiness_scheduler
    from theforge.sprint.shape_gate import apply_shape_gate

    config = SimpleNamespace(project_root=tmp_path, secrets=None, preflight_profile=_profile())

    def _fetch(_number, _root):
        return {
            "title": TITLE,
            "body": BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "closedAt": None,
            "stateReason": None,
            "updatedAt": None,
            "lastEditedAt": None,
            "comments": [],
            "timeline": [],
        }

    def _broken_iter_records(_self):
        raise OSError("records.jsonl is unreadable")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(SemanticReviewStore, "iter_records", _broken_iter_records)
        result = apply_shape_gate(
            [{"number": 2907, "title": TITLE}],
            tmp_path,
            fetch_detail=_fetch,
            semantic_readiness=_semantic_readiness_scheduler(config),
        )

    assert result.runnable == []
    assert [entry.reason_codes for entry in result.skipped] == [(SEMANTIC_EVALUATION_FAILED_CODE,)]


def test_a_failure_that_cannot_be_persisted_is_still_reported_as_a_failure(
    tmp_path: Path,
) -> None:
    """The store is what failed, so the failure cannot be read back from it."""

    class _UnwritableStore(SemanticReviewStore):
        def append_record(self, record):
            raise OSError("records.jsonl is not writable")

    readiness = _schedule(tmp_path, _Runner(raises=True), store=_UnwritableStore(tmp_path))

    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission
    assert "could not be attempted" in readiness.detail


def test_a_baseline_frozen_between_the_check_and_the_freeze_is_reported_as_failed(
    tmp_path: Path,
) -> None:
    """The automatic empty freeze loses to a human baseline that lands first."""

    class _RacingStore(SemanticReviewStore):
        def frozen_baseline(self, input_digest):
            return None  # the check sees nothing...

        def freeze_baseline(self, **_kwargs):
            raise ValueError(f"baseline for {_digest()} is already frozen and cannot be changed")

    runner = _Runner()
    readiness = _schedule(tmp_path, runner, store=_RacingStore(tmp_path))

    assert runner.calls == 0
    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission


def test_an_unusable_lock_defers_rather_than_scheduling_unserialized(
    tmp_path: Path,
) -> None:
    """No lock, no scheduling — an unserialized invocation would break the bound."""
    runner = _Runner()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "theforge.eval.semantic_auto.Path.mkdir",
            lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("no lock dir")),
        )
        readiness = _schedule(tmp_path, runner)

    assert runner.calls == 0
    assert SemanticReviewStore(tmp_path).iter_records() == []
    assert readiness.state == STATE_UNEVALUATED
    assert readiness.withholds_admission


def test_a_lock_file_that_cannot_be_opened_defers_too(tmp_path: Path) -> None:
    from theforge.eval import semantic_auto

    def _refuse_open(*_args, **_kwargs):
        raise OSError("lock file cannot be opened")

    runner = _Runner()
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(semantic_auto.Path, "open", _refuse_open)
        readiness = _schedule(tmp_path, runner)

    assert runner.calls == 0
    assert readiness.state == STATE_UNEVALUATED
    assert readiness.withholds_admission


def test_a_refused_or_timed_out_agent_result_is_recorded_as_a_failure(
    tmp_path: Path,
) -> None:
    """A non-success AgentResult never reads as a clean evaluation."""

    class _RefusingRunner(_Runner):
        def __call__(self, **_kwargs):
            self.calls += 1
            return _agent_result("timed out after 60s", success=False)

    runner = _RefusingRunner()
    readiness = _schedule(tmp_path, runner)

    assert runner.calls == 1
    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission
    records = SemanticReviewStore(tmp_path).records_for_digest(_digest())
    assert [record.status for record in records] == [STATUS_EVALUATION_FAILED]


def test_an_empty_agent_output_is_a_failure_not_a_clean_result(tmp_path: Path) -> None:
    readiness = _schedule(tmp_path, _Runner("   "))

    assert readiness.state == STATE_EVALUATION_FAILED
    assert readiness.withholds_admission


# ── The revision handoff between admission and dispatch ──────────────────────


def _ratify(store: SemanticReviewStore, *, issue_ref: str, digest: str) -> None:
    record = store.latest_successful_record(issue_ref=issue_ref, input_digest=digest)
    assert record is not None
    store.append_ratification(
        SemanticRatificationRecord(
            issue_ref=issue_ref,
            input_digest=digest,
            model_id=record.model_id,
            prompt_contract_version=record.prompt_contract_version,
            ratified_at="2026-09-06T00:00:00+00:00",
            decisions=tuple(
                SemanticConcernDecision(finding_digest=d, decision=DECISION_REJECTED)
                for d in record.finding_digests()
            ),
        )
    )


def test_the_dispatched_revision_must_be_the_one_admission_cleared(tmp_path: Path) -> None:
    semantic_dispatch_withholding = _live_dispatch_withholding

    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(), store=store)
    _ratify(store, issue_ref="issue-2907", digest=_digest())

    # The revision admission cleared dispatches.
    assert (
        semantic_dispatch_withholding(
            issue_number=2907,
            revision_digest=_digest(),
            revision_type="enhancement",
            project_root=tmp_path,
        )
        is None
    )
    # An edit landing between admission and the sprint's own fetch does not.
    stale = semantic_dispatch_withholding(
        issue_number=2907,
        revision_digest=_digest(EDITED_BODY),
        revision_type="enhancement",
        project_root=tmp_path,
    )
    assert stale is not None
    assert stale.reason_code == SEMANTIC_NOT_RATIFIED_CODE


def test_the_dispatch_check_leaves_a_not_required_document_alone(tmp_path: Path) -> None:
    semantic_dispatch_withholding = _live_dispatch_withholding

    assert (
        semantic_dispatch_withholding(
            issue_number=2907,
            revision_digest=_digest(labels=("documentation",)),
            revision_type="documentation",
            project_root=tmp_path,
        )
        is None
    )


def test_the_dispatch_check_withholds_when_the_store_cannot_be_read(tmp_path: Path) -> None:
    semantic_dispatch_withholding = _live_dispatch_withholding

    class _BrokenStore(SemanticReviewStore):
        def iter_records(self):
            raise OSError("records.jsonl is unreadable")

    withheld = semantic_dispatch_withholding(
        issue_number=2907,
        revision_digest=_digest(),
        revision_type="enhancement",
        project_root=tmp_path,
        store=_BrokenStore(tmp_path),
    )
    assert withheld is not None
    assert withheld.reason_code == SEMANTIC_EVALUATION_FAILED_CODE


def test_a_fetched_issue_story_carries_the_revision_it_was_built_from(
    tmp_path: Path,
) -> None:
    """The identity the dispatch check reads comes from the fetch itself."""
    from theforge.sprint.sources import GitHubIssueSource

    payload = json.dumps(
        {
            "title": TITLE,
            "body": BODY,
            "state": "OPEN",
            "labels": [{"name": "Enhancement"}],
            "comments": [],
        }
    )

    def _fake_run(cmd, **_kwargs):
        if "issue" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=payload, stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("theforge.sprint.sources.subprocess.run", _fake_run)
        task = GitHubIssueSource().fetch("2907", tmp_path)

    # Case-insensitive label resolution, and taken over the raw body, so it
    # equals the digest admission derived from the gate's own fetch.
    assert task.source_revision_digest == _digest()
    assert task.source_revision_type == "enhancement"


def test_query_mode_withholds_a_story_whose_revision_moved_before_resolution(
    tmp_path: Path,
) -> None:
    from dataclasses import replace as _replace

    from theforge.cli.sprint import _withhold_stale_semantic_revisions
    from theforge.sprint.manifest import ResolvedSprint
    from theforge.task.story import TaskStory

    store = SemanticReviewStore(tmp_path)
    _schedule(tmp_path, _Runner(), store=store)
    _ratify(store, issue_ref="issue-2907", digest=_digest())

    admitted = TaskStory(
        name=TITLE,
        slug="issue-2907",
        story_text=BODY,
        github_issue=2907,
        source_revision_digest=_digest(),
        source_revision_type="enhancement",
    )
    stale = _replace(admitted, source_revision_digest=_digest(EDITED_BODY))
    config = SimpleNamespace(project_root=tmp_path)

    def _resolved(task):
        return ResolvedSprint(
            name="s", budget_usd=1.0, stories=[(task, object(), "issue:2907")], max_parallel=1
        )

    from theforge.eval import semantic_auto

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(semantic_auto, "semantic_dispatch_withholding", _live_dispatch_withholding)
        kept = _withhold_stale_semantic_revisions(_resolved(admitted), config)
        dropped = _withhold_stale_semantic_revisions(_resolved(stale), config)

    assert len(kept.stories) == 1
    assert dropped.stories == []


def test_a_file_story_is_never_withheld_by_the_dispatch_check(tmp_path: Path) -> None:
    from theforge.cli.sprint import _withhold_stale_semantic_revisions
    from theforge.sprint.manifest import ResolvedSprint
    from theforge.task.story import TaskStory

    story = TaskStory(name="A story", slug="a-story", story_text="body")
    resolved = ResolvedSprint(
        name="s", budget_usd=1.0, stories=[(story, object(), "story.md")], max_parallel=1
    )

    assert (
        _withhold_stale_semantic_revisions(
            resolved, SimpleNamespace(project_root=tmp_path)
        ).stories
        == resolved.stories
    )
