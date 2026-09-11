"""Seam coverage for accepting a decomposition proposal (#2824).

Applying a proposal crosses every boundary the gate touches: the pause's
options, the operator's answer, the tracker mutation, the resume record, the
sprint's outcome classification, and the audit. Unit coverage of the
application alone would not catch a split that lands and is then reported as a
failed story, nor a pause that offers ``accept`` where nothing could apply it —
which are the two ways this feature fails in a way that matters.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_config

from theforge import pending
from theforge.cli.status import _preflight_gate_lines, cmd_decide
from theforge.config import (
    PREFLIGHT_GATE_ACTIONS,
    PREFLIGHT_GATE_ALL_ACTIONS,
    RetryPolicy,
    normalize_preflight_gate_no_decision,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.decomposition_application import (
    APPLY_STATUS_APPLIED,
    APPLY_STATUS_FAILED,
)
from theforge.coordinator.preflight_complexity_gate import (
    PREFLIGHT_GATE_EXTRA_KEY,
    evaluate_preflight_complexity_gate,
    offered_actions,
    returned_for_decomposition,
)
from theforge.coordinator.resume_persistence import (
    apply_resume_record_to_state,
    load_resume_record,
    save_resume_record,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.dag import StoryDAG
from theforge.sprint.runner import _classify_and_record
from theforge.sprint.story_state import SprintStoryState, StoryOutcome
from theforge.task import TaskStory

_STORY = """\
## What

Make the diagnosis record portable across projects.

## Acceptance criteria

- The record type exists and round-trips through the audit.
- Diagnosis output is assembled from the record.
- The CLI prints from the record.
"""

_ASSESSMENT = {
    "slices": [
        {
            "id": 1,
            "title": "extract the portable diagnosis record",
            "scope": "Define the record type and its serialization; leave callers alone.",
            "depends_on": [],
            "covers_criteria": [1],
        },
        {
            "id": 2,
            "title": "route diagnosis through the record",
            "scope": "Assemble diagnosis output from the record. Leaves the CLI to slice 3.",
            "depends_on": [1],
            "covers_criteria": [2],
        },
        {
            "id": 3,
            "title": "port the CLI surface",
            "scope": "Print from the record in the CLI; no change to the record itself.",
            "depends_on": [1],
            "covers_criteria": [3],
        },
    ],
    "unsettled": [],
}


def _issue_task(*, issue: int | None = 2541, type_: str | None = "enhancement") -> TaskStory:
    return TaskStory(
        name="Make the diagnosis record portable",
        slug="issue-2541",
        story_path=None,
        story_text=_STORY,
        github_issue=issue,
        type=type_,
    )


def _config(tmp_path: Path, **retry_fields: object):
    config = _make_config(tmp_path)
    merged = {
        "preflight_complexity_gate_threshold": RetryPolicy().preflight_complexity_gate_threshold,
        **retry_fields,
    }
    return replace(config, retry=replace(config.retry, **merged))


def _gated_state(*, with_assessment: bool = True) -> CoordinatorState:
    state = CoordinatorState()
    state.run_id = "7c1e04b9d3af"
    state.preflight_verdict = "PROCEED"
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 10
    state.preflight_implementation_complexity_score = 10
    state.preflight_validation_complexity_score = 4
    state.preflight_criteria_checked = [{"criterion": "sized", "satisfied": False}]
    state.story_content = _STORY
    if with_assessment:
        state.preflight_complexity_gate_assessment = _ASSESSMENT
        state.preflight_complexity_gate_assessment_generated = True
    else:
        state.preflight_complexity_gate_assessment_none_reason = "judged atomic"
    return state


def _answer_with(action: str):
    def _poll(run_id, timeout_seconds, **kwargs):
        pending.resolve_pending(run_id, action, kwargs.get("project_root"))
        return action, "2026-01-01T00:00:00+00:00"

    return _poll


def _never_answered(run_id, timeout_seconds, **kwargs):
    return "timeout", None


def _stamp_decided_at(run_id: str, project_root, *, seconds_past_deadline: int) -> None:
    """Move a recorded answer's timestamp relative to the pause's own deadline.

    Positive seconds put the answer after ``timeout_at`` (the pause had already
    lapsed when it was written); negative put it inside the window the operator
    was shown.
    """
    import datetime

    import yaml

    path = Path(project_root) / ".forge" / "pending" / f"{run_id}.yaml"
    record = yaml.safe_load(path.read_text(encoding="utf-8"))
    deadline = datetime.datetime.fromisoformat(record["timeout_at"])
    record["decided_at"] = (
        deadline + datetime.timedelta(seconds=seconds_past_deadline)
    ).isoformat()
    path.write_text(yaml.safe_dump(record), encoding="utf-8")


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


class FakeGh:
    """A ``gh`` stand-in for the whole gate path."""

    def __init__(
        self,
        *,
        fail_on_slice: str | None = None,
        state: str = "OPEN",
        state_reason: str | None = None,
    ):
        self.calls: list[list[str]] = []
        self.fail_on_slice = fail_on_slice
        # The original's live state. `gh issue close` moves it the way the real
        # command does: an already-closed issue keeps the reason it was closed
        # with, which is why the close is verified by reading it back.
        self.state = state
        self.state_reason = state_reason
        self.next_number = 2900

    def __call__(self, args: list[str], project_root: Path):
        self.calls.append(list(args))
        if args[:3] == ["gh", "issue", "view"]:
            reason = f'"{self.state_reason}"' if self.state_reason else "null"
            return _ok(
                f'{{"state": "{self.state}", "stateReason": {reason}, '
                '"labels": [{"name": "enhancement"}], "milestone": {"title": "v0.16.0"}}'
            )
        if args[:3] == ["gh", "issue", "create"]:
            title = args[args.index("--title") + 1]
            if self.fail_on_slice and title.startswith(self.fail_on_slice):
                return subprocess.CompletedProcess(
                    args=["gh"], returncode=1, stdout="", stderr="gh: server error"
                )
            number = self.next_number
            self.next_number += 1
            return _ok(f"https://github.com/acme/theforge/issues/{number}")
        if args[:3] == ["gh", "issue", "close"]:
            if self.state == "OPEN":
                self.state = "CLOSED"
                self.state_reason = "NOT_PLANNED"
            return _ok("")
        raise AssertionError(f"unexpected gh call: {args}")

    def kinds(self) -> list[str]:
        return [" ".join(call[1:3]) for call in self.calls]


def _run_gate(state, config, task, gh, *, answer: str):
    with (
        patch("theforge.pending.poll_pending", side_effect=_answer_with(answer)),
        patch("theforge.coordinator.decomposition_application._run_gh", gh),
    ):
        return evaluate_preflight_complexity_gate(state, config, task, "PROCEED")


# ── The pause offers accept only where a proposal could be applied ───────


class TestWhatThePauseOffers:
    def test_a_tracker_backed_story_with_a_proposal_is_offered_accept_and_decline(self):
        assert offered_actions(_gated_state(), _issue_task()) == PREFLIGHT_GATE_ALL_ACTIONS

    def test_a_pause_with_no_assessment_offers_only_the_scope_decision(self):
        state = _gated_state(with_assessment=False)
        assert offered_actions(state, _issue_task()) == PREFLIGHT_GATE_ACTIONS

    def test_a_file_backed_story_never_offers_accept(self):
        """There is no issue to create slices beside, and none to close."""
        assert offered_actions(_gated_state(), _issue_task(issue=None)) == PREFLIGHT_GATE_ACTIONS

    def test_a_bug_original_never_offers_accept(self):
        assert offered_actions(_gated_state(), _issue_task(type_="bug")) == PREFLIGHT_GATE_ACTIONS

    def test_the_pending_record_lists_and_glosses_exactly_what_is_offered(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()
        captured: dict = {}
        real_write = pending.write_pending

        def _capture(**kwargs):
            path = real_write(**kwargs)
            captured.update(kwargs)
            return path

        with (
            patch("theforge.pending.write_pending", side_effect=_capture),
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert captured["options"] == ["approve", "decompose", "accept", "decline"]
        payload = captured["extra"][PREFLIGHT_GATE_EXTRA_KEY]
        assert payload["proposal_appliable"] is True
        assert "forge decide 7c1e04b9d3af accept" in captured["reason"]
        assert "forge decide 7c1e04b9d3af decline" in captured["reason"]

    def test_status_glosses_the_proposal_actions(self):
        entry = {
            "run_id": "7c1e04b9d3af",
            "options": ["approve", "decompose", "accept", "decline"],
            PREFLIGHT_GATE_EXTRA_KEY: {
                "complexity_score": 10,
                "threshold": 9,
                "no_decision_action": "decompose",
                "proposal_appliable": True,
            },
        }

        rendered = "\n".join(_preflight_gate_lines(entry, "7c1e04b9d3af"))

        assert "forge decide 7c1e04b9d3af accept" in rendered
        assert "apply the proposal" in rendered
        assert "forge decide 7c1e04b9d3af decline" in rendered

    def test_status_does_not_offer_accept_on_a_record_that_did_not(self):
        entry = {
            "run_id": "abc",
            "options": ["approve", "decompose"],
            PREFLIGHT_GATE_EXTRA_KEY: {"complexity_score": 10, "threshold": 9},
        }

        rendered = "\n".join(_preflight_gate_lines(entry, "abc"))

        assert "accept" not in rendered


# ── Accepting applies; nothing else does ─────────────────────────────────


class TestOnlyAnOperatorAcceptanceMutates:
    def test_accept_creates_the_slices_and_closes_the_original(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()
        gh = FakeGh()

        result = _run_gate(state, config, task, gh, answer="accept")

        assert result is not None and result.success is False
        assert state.preflight_complexity_gate_decision == "accept"
        assert state.preflight_complexity_gate_decision_source == "operator"
        assert state.preflight_decomposition_application_status == APPLY_STATUS_APPLIED
        assert [r["issue"] for r in state.preflight_decomposition_created] == [2900, 2901, 2902]
        assert state.preflight_decomposition_source_issue_closed is True
        assert gh.kinds().count("issue create") == 3
        # Close after every create, then the read-back that verifies the close
        # actually left the original closed as decomposed.
        assert gh.kinds()[-2:] == ["issue close", "issue view"]
        assert "created #2900, #2901, #2902" in result.message

    def test_decline_creates_nothing_and_leaves_the_original_intact(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()
        gh = FakeGh()

        result = _run_gate(state, config, task, gh, answer="decline")

        assert result is not None and result.success is False
        assert state.preflight_complexity_gate_decision == "decline"
        assert gh.calls == []
        assert state.preflight_decomposition_application_status is None
        assert state.preflight_decomposition_source_issue_closed is False
        assert "declined" in result.message

    def test_decompose_still_creates_nothing(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh()

        result = _run_gate(state, config, _issue_task(), gh, answer="decompose")

        assert result is not None and result.success is False
        assert gh.calls == []
        assert state.preflight_decomposition_application_status is None

    def test_approve_still_creates_nothing_and_continues(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh()

        result = _run_gate(state, config, _issue_task(), gh, answer="approve")

        assert result is None
        assert gh.calls == []

    def test_an_unanswered_pause_creates_nothing(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh()

        with (
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
            patch("theforge.coordinator.decomposition_application._run_gh", gh),
        ):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert result is not None and result.success is False
        assert state.preflight_complexity_gate_decision == "decompose"
        assert state.preflight_complexity_gate_decision_source == "no_decision"
        assert gh.calls == []

    @pytest.mark.parametrize("configured", ["accept", "decline", "ACCEPT"])
    def test_no_timeout_policy_can_ever_resolve_to_a_proposal_action(self, configured):
        """The no-decision vocabulary is narrower than the offered one, on purpose."""
        action, fallback = normalize_preflight_gate_no_decision(configured)

        assert action == "decompose"
        assert fallback is not None

    def test_a_configured_accept_fallback_does_not_apply_a_split(self, tmp_path: Path):
        config = _config(tmp_path, preflight_complexity_gate_no_decision="accept")
        state = _gated_state()
        gh = FakeGh()

        with (
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
            patch("theforge.coordinator.decomposition_application._run_gh", gh),
        ):
            evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert state.preflight_complexity_gate_decision == "decompose"
        assert gh.calls == []

    def test_an_accept_answer_on_a_pause_that_did_not_offer_it_is_not_honoured(
        self, tmp_path: Path
    ):
        """A hand-written answer naming an action this pause never offered."""
        config = _config(tmp_path)
        task = _issue_task(issue=None)  # file-backed: accept is never offered
        state = _gated_state()
        gh = FakeGh()

        result = _run_gate(state, config, task, gh, answer="accept")

        assert result is not None and result.success is False
        assert state.preflight_complexity_gate_decision == "decompose"
        assert state.preflight_complexity_gate_decision_source == "no_decision"
        assert gh.calls == []

    def test_cmd_decide_refuses_accept_when_the_record_did_not_offer_it(
        self, tmp_path: Path, capsys, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        pending.write_pending(
            run_id="abc123",
            story="issue-2541",
            phase="PREFLIGHT",
            reason="scope decision",
            options=["approve", "decompose"],
            timeout_seconds=60,
            project_root=tmp_path,
        )

        with patch("theforge.cli.status._find_config", return_value=None):
            code = cmd_decide(SimpleNamespace(run_id="abc123", action="accept"))

        assert code == 1
        assert "Invalid action" in capsys.readouterr().err
        assert pending.decision_of(pending.read_pending("abc123", tmp_path) or {}) is None


# ── A failure partway through is actionable ──────────────────────────────


class TestPartialApplicationIsActionable:
    def test_a_failed_create_reports_what_exists_and_does_not_close(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh(fail_on_slice="port the CLI")

        result = _run_gate(state, config, _issue_task(), gh, answer="accept")

        assert result is not None and result.success is False
        assert state.preflight_decomposition_application_status == APPLY_STATUS_FAILED
        assert [r["issue"] for r in state.preflight_decomposition_created] == [2900, 2901]
        assert state.preflight_decomposition_source_issue_closed is False
        assert "issue close" not in gh.kinds()
        assert "#2900" in result.message and "#2901" in result.message
        # Not reported as a clean split: the operator has tracker state to act on.
        assert returned_for_decomposition(state) is False
        assert state.error_type == "decomposition_application_failed"

    def test_reentry_finishes_the_split_instead_of_duplicating_it(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()
        first = FakeGh(fail_on_slice="port the CLI")
        _run_gate(state, config, task, first, answer="accept")
        assert len(state.preflight_decomposition_created) == 2

        # The recorded decision is honoured rather than re-asked, and the
        # persisted slice map is what keeps the retry additive.
        second = FakeGh()
        second.next_number = 2910
        with (
            patch("theforge.pending.write_pending") as mock_write,
            patch("theforge.coordinator.decomposition_application._run_gh", second),
        ):
            result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        mock_write.assert_not_called()
        assert result is not None
        assert state.preflight_decomposition_application_status == APPLY_STATUS_APPLIED
        created_titles = [
            call[call.index("--title") + 1]
            for call in second.calls
            if call[:3] == ["gh", "issue", "create"]
        ]
        assert created_titles == ["port the CLI surface"]
        assert [r["issue"] for r in state.preflight_decomposition_created] == [2900, 2901, 2910]
        assert state.preflight_decomposition_source_issue_closed is True


# ── The applied split is not a failed story ──────────────────────────────


class TestAppliedIsReportedAsDecomposed:
    def _state_for(self, status: str) -> CoordinatorState:
        state = _gated_state()
        state.preflight_complexity_gate_opened = True
        state.preflight_complexity_gate_decision = "accept"
        state.preflight_complexity_gate_decision_source = "operator"
        state.preflight_complexity_gate_threshold = 9
        state.preflight_complexity_gate_score = 10
        state.preflight_decomposition_application_status = status
        state.preflight_decomposition_created = [
            {"slice_id": 1, "title": "extract", "issue": 2900, "depends_on_issues": []},
        ]
        state.preflight_decomposition_source_issue = 2541
        state.preflight_decomposition_source_issue_closed = status == APPLY_STATUS_APPLIED
        state.started_at = "2026-01-01T00:00:00+00:00"
        return state

    def test_sprint_classifies_an_applied_accept_as_decomposed(self, tmp_path: Path):
        task = _issue_task()
        state = self._state_for(APPLY_STATUS_APPLIED)
        result = CoordinatorResult(
            success=False, phase=Phase.PREFLIGHT, state=state, message="applied"
        )
        dag = StoryDAG([task])
        stories = SprintStoryState()
        stories.register(task.slug, task.slug)

        outcome = _classify_and_record(task, result, dag, set(), story_state=stories)

        assert outcome is StoryOutcome.DECOMPOSED
        assert outcome.is_failed is False
        assert stories.counts()["failed"] == 0

    def test_sprint_does_not_classify_a_failed_application_as_decomposed(self, tmp_path: Path):
        task = _issue_task()
        state = self._state_for(APPLY_STATUS_FAILED)
        result = CoordinatorResult(
            success=False, phase=Phase.PREFLIGHT, state=state, message="not applied"
        )
        dag = StoryDAG([task])
        stories = SprintStoryState()
        stories.register(task.slug, task.slug)

        outcome = _classify_and_record(task, result, dag, set(), story_state=stories)

        assert outcome is not StoryOutcome.DECOMPOSED

    def test_the_audit_records_the_proposal_as_applied_and_its_disposition(self, tmp_path: Path):
        config = _config(tmp_path)
        state = self._state_for(APPLY_STATUS_APPLIED)

        record = generate_audit_log(
            config,
            _issue_task(),
            CoordinatorResult(
                success=False, phase=Phase.PREFLIGHT, state=state, message="applied"
            ),
        )

        gate = record["preflight_complexity_gate"]
        assert gate["decision"] == "accept"
        assert gate["assessment_disposition"] == "operator_accept"
        application = gate["assessment_application"]
        assert application["status"] == APPLY_STATUS_APPLIED
        assert application["created"][0]["issue"] == 2900
        assert application["source_issue"] == 2541
        assert application["source_issue_closed"] is True
        assert record["outcome"]["returned_for_decomposition"] is True

    def test_the_audit_separates_an_accept_that_did_not_land(self, tmp_path: Path):
        config = _config(tmp_path)
        state = self._state_for(APPLY_STATUS_FAILED)
        state.preflight_decomposition_application_error = "gh: server error"

        record = generate_audit_log(
            config,
            _issue_task(),
            CoordinatorResult(
                success=False, phase=Phase.PREFLIGHT, state=state, message="not applied"
            ),
        )

        gate = record["preflight_complexity_gate"]
        assert gate["assessment_disposition"] == "operator_accept"
        assert gate["assessment_application"]["status"] == APPLY_STATUS_FAILED
        assert gate["assessment_application"]["error"] == "gh: server error"
        assert gate["assessment_application"]["source_issue_closed"] is False
        assert record["outcome"]["returned_for_decomposition"] is False

    def test_a_declined_proposal_records_its_disposition_and_no_application(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state()
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.preflight_complexity_gate_opened = True
        state.preflight_complexity_gate_decision = "decline"
        state.preflight_complexity_gate_decision_source = "operator"

        record = generate_audit_log(
            config,
            _issue_task(),
            CoordinatorResult(
                success=False, phase=Phase.PREFLIGHT, state=state, message="declined"
            ),
        )

        gate = record["preflight_complexity_gate"]
        assert gate["assessment_disposition"] == "operator_decline"
        assert gate["assessment_application"] is None
        assert record["outcome"]["returned_for_decomposition"] is True


# ── State handoff ────────────────────────────────────────────────────────


class TestApplicationSurvivesResume:
    def test_the_resume_record_round_trips_the_created_slices(self, tmp_path: Path):
        saved = _gated_state()
        saved.preflight_complexity_gate_opened = True
        saved.preflight_complexity_gate_decision = "accept"
        saved.preflight_complexity_gate_decision_source = "operator"
        saved.preflight_decomposition_application_status = APPLY_STATUS_FAILED
        saved.preflight_decomposition_created = [
            {"slice_id": 1, "title": "extract", "issue": 2900, "depends_on_issues": []},
        ]
        saved.preflight_decomposition_source_issue = 2541
        saved.preflight_decomposition_source_issue_closed = False
        saved.preflight_decomposition_application_error = "gh: server error"
        saved.preflight_decomposition_applied_at = "2026-01-01T00:00:00+00:00"

        assert save_resume_record(tmp_path, saved, slug="issue-2541") is not None
        record = load_resume_record(tmp_path, "issue-2541")
        assert record is not None

        resumed = CoordinatorState()
        apply_resume_record_to_state(resumed, record)

        assert resumed.preflight_complexity_gate_decision == "accept"
        assert resumed.preflight_decomposition_application_status == APPLY_STATUS_FAILED
        assert resumed.preflight_decomposition_created[0]["issue"] == 2900
        assert resumed.preflight_decomposition_source_issue == 2541
        assert resumed.preflight_decomposition_source_issue_closed is False
        assert resumed.preflight_decomposition_application_error == "gh: server error"

    def test_a_fully_applied_split_is_not_applied_twice_on_resume(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()
        first = FakeGh()
        _run_gate(state, config, task, first, answer="accept")

        second = FakeGh()
        with (
            patch("theforge.pending.write_pending") as mock_write,
            patch("theforge.coordinator.decomposition_application._run_gh", second),
        ):
            result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        mock_write.assert_not_called()
        assert result is not None and returned_for_decomposition(state)
        assert second.calls == []


class TestAuditRecordMigration:
    """A record written before this feature says "nothing was applied", explicitly."""

    def test_a_legacy_record_gains_an_explicit_null_application(self):
        from theforge.coordinator import audit_storage

        migrated = audit_storage._migrate_v46_to_v47(
            {"schema_version": 46, "preflight_complexity_gate": {"decision": "decompose"}}
        )

        assert migrated["preflight_complexity_gate"]["assessment_application"] is None
        assert migrated["preflight_complexity_gate"]["decision"] == "decompose"

    def test_a_record_that_already_carries_one_is_untouched(self):
        from theforge.coordinator import audit_storage

        record = {
            "schema_version": 46,
            "preflight_complexity_gate": {"assessment_application": {"status": "applied"}},
        }
        assert audit_storage._migrate_v46_to_v47(record) is record

    def test_the_migration_is_registered_for_the_current_version(self):
        from theforge.coordinator import audit_storage

        assert audit_storage.CURRENT_RECORD_SCHEMA_VERSION == 48
        assert audit_storage.MIGRATION_HELPERS[47] is audit_storage._migrate_v47_to_v48
        assert audit_storage.MIGRATION_HELPERS[46] is audit_storage._migrate_v46_to_v47
        assert audit_storage.MIGRATION_HELPERS[45] is audit_storage._migrate_v45_to_v46


# ── An acceptance is only actionable with live operator provenance ───────


class TestAcceptanceProvenance:
    """Both routes into the mutation re-check who accepted, and when."""

    def _accepted_state(self, source: str | None) -> CoordinatorState:
        state = _gated_state()
        state.preflight_complexity_gate_opened = True
        state.preflight_complexity_gate_decision = "accept"
        state.preflight_complexity_gate_decision_source = source
        state.preflight_complexity_gate_threshold = 9
        state.preflight_complexity_gate_score = 10
        return state

    @pytest.mark.parametrize("source", [None, "no_decision", ""])
    def test_a_restored_accept_with_no_operator_behind_it_mutates_nothing(
        self, tmp_path: Path, source
    ):
        """A resume record is a file. An accept it carries that no operator made
        describes something that cannot have happened, and the safe reading of an
        impossible record is to create nothing."""
        config = _config(tmp_path)
        state = self._accepted_state(source)
        gh = FakeGh()

        with (
            patch("theforge.pending.write_pending") as mock_write,
            patch("theforge.coordinator.decomposition_application._run_gh", gh),
        ):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        mock_write.assert_not_called()
        assert gh.calls == []
        assert result is not None and result.success is False
        assert state.preflight_decomposition_application_status == APPLY_STATUS_FAILED
        assert state.preflight_decomposition_created == []
        assert state.preflight_decomposition_source_issue_closed is False
        assert "only an operator may apply a split" in (
            state.preflight_decomposition_application_error or ""
        )
        # Not reported as a clean split — the operator has to see the refusal.
        assert returned_for_decomposition(state) is False

    def test_a_restored_operator_accept_still_applies(self, tmp_path: Path):
        config = _config(tmp_path)
        state = self._accepted_state("operator")
        gh = FakeGh()

        with (
            patch("theforge.pending.write_pending") as mock_write,
            patch("theforge.coordinator.decomposition_application._run_gh", gh),
        ):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        mock_write.assert_not_called()
        assert result is not None
        assert state.preflight_decomposition_application_status == APPLY_STATUS_APPLIED
        assert returned_for_decomposition(state) is True

    def test_a_restored_accept_with_no_assessment_to_apply_mutates_nothing(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state(with_assessment=False)
        state.preflight_complexity_gate_decision = "accept"
        state.preflight_complexity_gate_decision_source = "operator"
        gh = FakeGh()

        with patch("theforge.coordinator.decomposition_application._run_gh", gh):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert gh.calls == []
        assert result is not None and result.success is False
        assert state.preflight_decomposition_application_status == APPLY_STATUS_FAILED
        assert "none to apply" in (state.preflight_decomposition_application_error or "")

    def test_an_accept_recorded_after_the_pause_expired_is_not_honoured(self, tmp_path: Path):
        """The pause has already resolved by the no-decision route; an answer
        written after its deadline is not the answer that resolved it, and must
        not be able to mutate the tracker."""
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh()

        def _expire_then_answer(run_id, timeout_seconds, **kwargs):
            project_root = kwargs.get("project_root")
            pending.resolve_pending(run_id, "accept", project_root)
            _stamp_decided_at(run_id, project_root, seconds_past_deadline=60)
            return "timeout", None

        with (
            patch("theforge.pending.poll_pending", side_effect=_expire_then_answer),
            patch("theforge.coordinator.decomposition_application._run_gh", gh),
        ):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert gh.calls == []
        assert state.preflight_complexity_gate_decision == "decompose"
        assert state.preflight_complexity_gate_decision_source == "no_decision"
        assert state.preflight_decomposition_application_status is None
        assert result is not None and result.success is False

    def test_an_answer_given_inside_the_window_is_still_honoured_after_an_expiry(
        self, tmp_path: Path
    ):
        """The race the post-poll record read exists for: the operator answered
        in time, the poller just did not see it before returning."""
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh()

        def _answer_then_expire(run_id, timeout_seconds, **kwargs):
            project_root = kwargs.get("project_root")
            pending.resolve_pending(run_id, "accept", project_root)
            _stamp_decided_at(run_id, project_root, seconds_past_deadline=-60)
            return "timeout", None

        with (
            patch("theforge.pending.poll_pending", side_effect=_answer_then_expire),
            patch("theforge.coordinator.decomposition_application._run_gh", gh),
        ):
            evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert state.preflight_complexity_gate_decision == "accept"
        assert state.preflight_complexity_gate_decision_source == "operator"
        assert state.preflight_decomposition_application_status == APPLY_STATUS_APPLIED


# ── An interrupted application is recoverable ────────────────────────────


class TestInterruptedApplication:
    def test_every_created_slice_is_on_the_state_before_the_next_create(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()
        observed: list[tuple[int, str | None]] = []

        class _WatchingGh(FakeGh):
            def __call__(self, args, project_root):
                if args[:3] == ["gh", "issue", "create"]:
                    observed.append(
                        (
                            len(state.preflight_decomposition_created or []),
                            state.preflight_decomposition_application_status,
                        )
                    )
                return super().__call__(args, project_root)

        _run_gate(state, config, task, _WatchingGh(), answer="accept")

        # Before create N, N-1 slices are already recorded — the record never
        # lags the tracker by more than the create in flight.
        assert [count for count, _ in observed] == [0, 1, 2]
        assert [status for _, status in observed][1:] == ["in_progress", "in_progress"]
        assert state.preflight_decomposition_application_status == APPLY_STATUS_APPLIED

    def test_a_kill_between_creates_leaves_the_created_issues_on_the_resume_record(
        self, tmp_path: Path
    ):
        """The interruption the per-create recorder exists for: the process dies
        after slice 1 is filed, and the split resumes into it rather than
        filing it a second time."""
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()

        class _Killed(BaseException):
            """Not an Exception: a kill is not something the apply path catches."""

        class _DyingGh(FakeGh):
            def __call__(self, args, project_root):
                if args[:3] == ["gh", "issue", "create"] and len(self.calls) > 1:
                    raise _Killed("the process was killed mid-application")
                return super().__call__(args, project_root)

        with pytest.raises(_Killed):
            _run_gate(state, config, task, _DyingGh(), answer="accept")

        # What the killed process left behind, read back off the resume record
        # rather than off the in-memory state.
        record = load_resume_record(tmp_path, task.slug)
        assert record is not None
        resumed = CoordinatorState()
        apply_resume_record_to_state(resumed, record)
        assert resumed.preflight_complexity_gate_decision == "accept"
        assert resumed.preflight_complexity_gate_decision_source == "operator"
        assert [entry["issue"] for entry in resumed.preflight_decomposition_created] == [2900]
        assert resumed.preflight_decomposition_application_status == "in_progress"

        # The resumed run finishes the split without filing slice 1 again.
        resumed.run_id = state.run_id
        second = FakeGh()
        second.next_number = 2910
        with (
            patch("theforge.pending.write_pending") as mock_write,
            patch("theforge.coordinator.decomposition_application._run_gh", second),
        ):
            evaluate_preflight_complexity_gate(resumed, config, task, "PROCEED")

        mock_write.assert_not_called()
        created_titles = [
            call[call.index("--title") + 1]
            for call in second.calls
            if call[:3] == ["gh", "issue", "create"]
        ]
        assert "extract the portable diagnosis record" not in created_titles
        assert resumed.preflight_decomposition_application_status == APPLY_STATUS_APPLIED
        assert [entry["issue"] for entry in resumed.preflight_decomposition_created] == [
            2900,
            2910,
            2911,
        ]


class TestAnExpiredPauseCannotBeAnsweredEitherWay:
    """The deadline is checked on the answer, not on how the gate came by it.

    The poller hands back whatever the record says the moment it looks, so a
    decision written after the window closed can arrive as a live answer rather
    than through the expiry path — the same late acceptance by another route.
    """

    def test_a_late_accept_returned_by_the_poller_itself_is_not_honoured(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh()

        def _poll_returns_a_late_answer(run_id, timeout_seconds, **kwargs):
            project_root = kwargs.get("project_root")
            pending.resolve_pending(run_id, "accept", project_root)
            _stamp_decided_at(run_id, project_root, seconds_past_deadline=90)
            record = pending.read_pending(run_id, project_root=project_root) or {}
            # The poller reports the answer it found, exactly as it does when it
            # sees a decision land — it has no opinion about the deadline.
            return "accept", record.get("decided_at")

        with (
            patch("theforge.pending.poll_pending", side_effect=_poll_returns_a_late_answer),
            patch("theforge.coordinator.decomposition_application._run_gh", gh),
        ):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert gh.calls == []
        assert state.preflight_complexity_gate_decision == "decompose"
        assert state.preflight_complexity_gate_decision_source == "no_decision"
        assert state.preflight_decomposition_application_status is None
        assert result is not None and result.success is False

    def test_a_late_approve_returned_by_the_poller_is_not_honoured_either(self, tmp_path: Path):
        """Not an accept-only rule: an expired pause resolves by its no-decision
        route whatever the late answer says."""
        config = _config(tmp_path)
        state = _gated_state()

        def _poll_returns_a_late_answer(run_id, timeout_seconds, **kwargs):
            project_root = kwargs.get("project_root")
            pending.resolve_pending(run_id, "approve", project_root)
            _stamp_decided_at(run_id, project_root, seconds_past_deadline=90)
            record = pending.read_pending(run_id, project_root=project_root) or {}
            return "approve", record.get("decided_at")

        with patch("theforge.pending.poll_pending", side_effect=_poll_returns_a_late_answer):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert state.preflight_complexity_gate_decision == "decompose"
        assert state.preflight_complexity_gate_decision_source == "no_decision"
        assert result is not None and result.success is False

    def test_an_in_window_answer_returned_by_the_poller_is_honoured(self, tmp_path: Path):
        """The ordinary path stays ordinary: answered in time, applied."""
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh()

        result = _run_gate(state, config, _issue_task(), gh, answer="accept")

        assert state.preflight_complexity_gate_decision == "accept"
        assert state.preflight_complexity_gate_decision_source == "operator"
        assert state.preflight_decomposition_application_status == APPLY_STATUS_APPLIED
        assert result is not None


class TestInterruptionBeforeTheFirstRecord:
    """The window the per-create recorder itself cannot cover.

    ``gh issue create`` returns, and the process dies before that slice reaches
    the record. What makes this recoverable is that the *intent* to apply was
    durable before the first create: without it the resumed run reads "accept,
    nothing attempted", skips reconciliation, and files the slice again.
    """

    def test_the_intent_to_apply_is_durable_before_the_first_create(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()
        observed: list[str | None] = []

        class _WatchingGh(FakeGh):
            def __call__(self, args, project_root):
                if args[:3] == ["gh", "issue", "create"] and not observed:
                    record = load_resume_record(tmp_path, task.slug) or {}
                    preflight = record.get("preflight") or {}
                    observed.append(preflight.get("decomposition_application_status"))
                return super().__call__(args, project_root)

        _run_gate(state, config, task, _WatchingGh(), answer="accept")

        # Read off the resume record, not the in-memory state: what a killed
        # process leaves behind is the file.
        assert observed == ["in_progress"]

    def test_a_kill_before_the_first_slice_is_recorded_does_not_duplicate_it(self, tmp_path: Path):
        config = _config(tmp_path)
        task = _issue_task()
        state = _gated_state()

        class _Killed(BaseException):
            """Not an Exception: a kill is not something the apply path catches."""

        def _die_before_recording(on_created, record):
            raise _Killed("the process died between the create and its record")

        with pytest.raises(_Killed):
            with patch(
                "theforge.coordinator.decomposition_application._notify_created",
                _die_before_recording,
            ):
                _run_gate(state, config, task, FakeGh(), answer="accept")

        # #2900 exists on GitHub and appears on no record — the state a re-entry
        # has to be able to recover from.
        resumed = CoordinatorState()
        record = load_resume_record(tmp_path, task.slug)
        assert record is not None
        apply_resume_record_to_state(resumed, record)
        assert resumed.preflight_complexity_gate_decision == "accept"
        assert resumed.preflight_complexity_gate_decision_source == "operator"
        assert resumed.preflight_decomposition_created == []
        # The intent survived, which is what turns the retry into a re-entry.
        assert resumed.preflight_decomposition_application_status == "in_progress"

        class _ReconcilingGh(FakeGh):
            def __call__(self, args, project_root):
                if args[:3] == ["gh", "issue", "list"]:
                    self.calls.append(list(args))
                    body = "<!-- forge-decomposition-v1 source=2541 slice=1 -->"
                    return _ok(f'[{{"number": 2900, "body": "{body}"}}]')
                return super().__call__(args, project_root)

        resumed.run_id = state.run_id
        second = _ReconcilingGh()
        second.next_number = 2910
        with (
            patch("theforge.pending.write_pending") as mock_write,
            patch("theforge.coordinator.decomposition_application._run_gh", second),
        ):
            evaluate_preflight_complexity_gate(resumed, config, task, "PROCEED")

        mock_write.assert_not_called()
        created_titles = [
            call[call.index("--title") + 1]
            for call in second.calls
            if call[:3] == ["gh", "issue", "create"]
        ]
        assert "extract the portable diagnosis record" not in created_titles
        assert resumed.preflight_decomposition_application_status == APPLY_STATUS_APPLIED
        assert [entry["issue"] for entry in resumed.preflight_decomposition_created] == [
            2900,
            2910,
            2911,
        ]


class TestARefusalKeepsWhatExists:
    def test_a_provenance_refusal_does_not_erase_already_created_slices(self, tmp_path: Path):
        """Refusing to continue an application does not un-create its issues."""
        config = _config(tmp_path)
        state = _gated_state()
        state.preflight_complexity_gate_opened = True
        state.preflight_complexity_gate_decision = "accept"
        state.preflight_complexity_gate_decision_source = "no_decision"
        state.preflight_decomposition_application_status = "in_progress"
        state.preflight_decomposition_created = [
            {"slice_id": 1, "title": "extract", "issue": 2900, "depends_on_issues": []},
        ]
        gh = FakeGh()

        with patch("theforge.coordinator.decomposition_application._run_gh", gh):
            result = evaluate_preflight_complexity_gate(state, config, _issue_task(), "PROCEED")

        assert gh.calls == []
        assert state.preflight_decomposition_application_status == APPLY_STATUS_FAILED
        # The refusal reports what exists rather than replacing it with nothing.
        assert [entry["issue"] for entry in state.preflight_decomposition_created] == [2900]
        assert result is not None and "#2900" in result.message


class TestTheOriginalCanChangeUnderAnOpenPause:
    """The pause can stand for hours; the tracker does not hold still for it."""

    def test_an_original_closed_as_completed_during_the_pause_creates_nothing(
        self, tmp_path: Path
    ):
        config = _config(tmp_path)
        state = _gated_state()
        gh = FakeGh(state="CLOSED", state_reason="COMPLETED")

        result = _run_gate(state, config, _issue_task(), gh, answer="accept")

        # The state read happens, and stops there.
        assert gh.kinds() == ["issue view"]
        assert state.preflight_decomposition_application_status == APPLY_STATUS_FAILED
        assert state.preflight_decomposition_created == []
        assert state.preflight_decomposition_source_issue_closed is False
        assert "closed while the pause was open" in (
            state.preflight_decomposition_application_error or ""
        )
        # Not a decomposition: the original is closed as completed, and the run
        # says so rather than reporting a split that did not happen.
        assert returned_for_decomposition(state) is False
        assert result is not None and result.success is False

    def test_the_audit_separates_it_from_an_applied_split(self, tmp_path: Path):
        config = _config(tmp_path)
        state = _gated_state()
        state.started_at = "2026-01-01T00:00:00+00:00"
        gh = FakeGh(state="CLOSED", state_reason="COMPLETED")

        _run_gate(state, config, _issue_task(), gh, answer="accept")
        record = generate_audit_log(
            config,
            _issue_task(),
            CoordinatorResult(
                success=False, phase=Phase.PREFLIGHT, state=state, message="not applied"
            ),
        )

        gate = record["preflight_complexity_gate"]
        assert gate["assessment_disposition"] == "operator_accept"
        assert gate["assessment_application"]["status"] == APPLY_STATUS_FAILED
        assert gate["assessment_application"]["created"] == []
        assert gate["assessment_application"]["source_issue_closed"] is False
        assert record["outcome"]["returned_for_decomposition"] is False
