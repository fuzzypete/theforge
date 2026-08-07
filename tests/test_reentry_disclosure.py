"""Two re-entry paths, one disclosure of which one skips review.

``forge review`` runs the review phase against a worktree. ``forge sprint
--resume`` recovers the phase records an earlier attempt produced and continues
from what they say — including a recorded escalate-gate decision. From a story
that stopped with an unrun review cycle *and* a recorded decision, those produce
different outcomes: the first runs the cycle, the second continues from the
decision and never runs it.

Both behaviours are defensible; neither was discoverable. These tests cover the
one derivation of that fact (``classify_review_obligation`` /
``analyze_reentry``) and each surface that must state it before an operator
spends anything: resume's own startup output, the sprint status view, and the
pending-decision list (#2239).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import _make_agent_result, _make_config, patch_gate_shell

from theforge.coordinator.engine import run_from_review
from theforge.coordinator.resume_persistence import (
    RECORD_VERSION,
    REVIEW_PROGRESS_KEY,
    analyze_reentry,
    capture_phase_blocks,
    classify_review_obligation,
    describe_reentry_paths,
    load_reentry_analysis,
    recover_phase_state,
    resume_record_path,
    save_resume_record,
)
from theforge.coordinator.state import CoordinatorState
from theforge.review import ReviewResult
from theforge.task import TaskStory

STORY_CONTENT = "# Test\n\nDo the thing."

APPROVE_YAML = (
    "```yaml\nverdict: APPROVE\nsummary: ok\nfindings: []\n"
    "story_compliance:\n  matches_spec: true\n"
    "test_coverage:\n  adequate: true\n"
    "ac_verification:\n  - criterion: c\n    status: VERIFIED\n"
    "    evidence: e\n```\n"
)


def _review_result(verdict: str) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="s",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _stopped_state(verdict: str = "REQUEST_CHANGES") -> CoordinatorState:
    """The observed stopped state: gate PASS after a fix, cycle 1 REQUEST_CHANGES,
    cycle 2 never ran, and an escalate-gate decision recorded against the story."""
    state = CoordinatorState()
    state.review_cycle = 1
    state.review_results.append(_review_result(verdict))
    state.last_gate_decision = "PASS"
    state.last_gate_commit = "abc1234"
    state.gate_runs = 2
    state.escalate_decision = "land_core_defer_edges"
    state.escalate_selected_action = "land_core"
    state.escalate_reason = "review cycles exhausted"
    return state


def _write_record(project_root: Path, slug: str, state: CoordinatorState) -> Path:
    path = save_resume_record(
        project_root,
        state,
        slug=slug,
        story_content=STORY_CONTENT,
        run_id="prior-run",
    )
    assert path is not None
    return path


class TestReviewObligationClassification:
    """The record must say which of the two states the story is in — or say it
    does not know. Silence must never read as "review completed"."""

    def test_unrun_cycle_is_named_with_its_number(self, tmp_path: Path) -> None:
        _write_record(tmp_path, "test-story", _stopped_state())
        record = json.loads(resume_record_path(tmp_path, "test-story").read_text())

        obligation = classify_review_obligation(record)

        assert obligation["review_obligation"] == "cycle_not_run"
        assert obligation["outstanding_review_cycle"] == 2
        assert obligation["latest_review_verdict"] == "REQUEST_CHANGES"
        assert obligation["last_gate_decision"] == "PASS"

    def test_completed_review_owes_nothing(self, tmp_path: Path) -> None:
        _write_record(tmp_path, "test-story", _stopped_state(verdict="APPROVE"))
        record = json.loads(resume_record_path(tmp_path, "test-story").read_text())

        obligation = classify_review_obligation(record)

        assert obligation["review_obligation"] == "none"
        assert obligation["outstanding_review_cycle"] is None

    def test_record_without_review_progress_reports_unknown(self) -> None:
        """A record written before this key existed knows nothing about review.
        Reporting that as "none" would claim a review completed on no evidence."""
        record = {"version": RECORD_VERSION, "escalation": {"decision": "land_core"}}

        obligation = classify_review_obligation(record)

        assert obligation["review_obligation"] == "unknown"
        assert obligation["outstanding_review_cycle"] is None
        assert analyze_reentry(record)["skips_review"] is False

    def test_review_progress_is_not_a_recovered_phase(self, tmp_path: Path) -> None:
        """Nothing restores review progress onto a resumed state, so it must not
        appear in ``recovered_phases`` — that list names phases lifted off disk."""
        _write_record(tmp_path, "test-story", _stopped_state())

        recovery = recover_phase_state(
            tmp_path, CoordinatorState(), slug="test-story", story_content=STORY_CONTENT
        )

        assert REVIEW_PROGRESS_KEY not in recovery["recovered_phases"]
        assert REVIEW_PROGRESS_KEY not in recovery["recorded_phases"]

    def test_review_progress_alone_never_creates_a_record(self, tmp_path: Path) -> None:
        """Review counters are evidence about a record, not a record: a story
        with no recoverable phase output still writes no sidecar."""
        state = CoordinatorState()
        state.review_cycle = 1
        state.review_results.append(_review_result("REQUEST_CHANGES"))

        assert set(capture_phase_blocks(state)) == {REVIEW_PROGRESS_KEY}
        assert save_resume_record(tmp_path, state, slug="s", story_content=STORY_CONTENT) is None


class TestReentryAnalysis:
    """``skips_review`` is the disclosure; it needs both halves to be true."""

    def test_decision_plus_unrun_cycle_skips_review(self, tmp_path: Path) -> None:
        _write_record(tmp_path, "test-story", _stopped_state())

        analysis = load_reentry_analysis(tmp_path, "test-story")

        assert analysis is not None
        assert analysis["skips_review"] is True
        assert analysis["outstanding_phases"] == ["REVIEW"]
        assert analysis["escalation_decision"] == "land_core_defer_edges"
        assert analysis["escalation_selected_action"] == "land_core"

        note = describe_reentry_paths(analysis)
        assert "forge review runs REVIEW cycle 2" in note
        assert "forge sprint --resume" in note
        assert "skips REVIEW" in note

    def test_completed_review_states_no_divergence(self, tmp_path: Path) -> None:
        _write_record(tmp_path, "test-story", _stopped_state(verdict="APPROVE"))

        analysis = load_reentry_analysis(tmp_path, "test-story")

        assert analysis is not None
        assert analysis["skips_review"] is False
        assert analysis["outstanding_phases"] == []
        assert describe_reentry_paths(analysis) == ""

    def test_unrun_cycle_without_a_decision_is_not_a_skip(self, tmp_path: Path) -> None:
        """Nothing stands in for the cycle, so resume does not skip it — the
        paths agree and there is nothing to disclose."""
        state = _stopped_state()
        state.escalate_decision = None
        state.escalate_selected_action = None
        state.preflight_verdict = "PROCEED"
        _write_record(tmp_path, "test-story", state)

        analysis = load_reentry_analysis(tmp_path, "test-story")

        assert analysis is not None
        assert analysis["skips_review"] is False
        assert analysis["outstanding_phases"] == ["REVIEW"]

    def test_missing_record_yields_no_analysis(self, tmp_path: Path) -> None:
        assert load_reentry_analysis(tmp_path, "never-ran") is None


class TestResumeReportsThePathItTakes:
    """Resume must state the recovered state it acts on when that state changes
    which phases run — before the coordinator loop spends anything."""

    def test_recovery_carries_the_impact(self, tmp_path: Path) -> None:
        _write_record(tmp_path, "test-story", _stopped_state())

        recovery = recover_phase_state(
            tmp_path, CoordinatorState(), slug="test-story", story_content=STORY_CONTENT
        )

        assert recovery["status"] == "recovered"
        assert "escalation" in recovery["recovered_phases"]
        impact = recovery["reentry_impact"]
        assert impact is not None
        assert impact["escalation_decision"] == "land_core_defer_edges"
        assert impact["outstanding_review_cycle"] == 2

    def test_no_impact_when_this_attempt_made_its_own_decision(self, tmp_path: Path) -> None:
        """A decision this attempt produced is not a recovery; reporting it as
        one would make the disclosure noise on every escalating run."""
        _write_record(tmp_path, "test-story", _stopped_state())
        live = CoordinatorState()
        live.escalate_decision = "reject"

        recovery = recover_phase_state(
            tmp_path, live, slug="test-story", story_content=STORY_CONTENT
        )

        assert recovery["reentry_impact"] is None

    def test_report_names_both_paths(self, capsys) -> None:
        from theforge.coordinator.run_setup import _report_reentry_impact

        _report_reentry_impact(
            {
                "status": "recovered",
                "source_run_id": "prior-run",
                "recovered_phases": ["escalation"],
                "reentry_impact": {
                    "escalation_decision": "land_core_defer_edges",
                    "escalation_selected_action": "land_core",
                    "outstanding_review_cycle": 2,
                    "latest_review_verdict": "REQUEST_CHANGES",
                    "last_gate_decision": "PASS",
                },
            }
        )

        out = capsys.readouterr().err
        assert "land_core_defer_edges / land_core" in out
        assert "prior-run" in out
        assert "outstanding: REVIEW cycle 2 has not run" in out
        assert "REQUEST_CHANGES" in out
        assert "will NOT run REVIEW" in out
        assert "`forge review` runs REVIEW cycle 2" in out

    def test_report_is_silent_without_an_impact(self, capsys) -> None:
        from theforge.coordinator.run_setup import _report_reentry_impact

        _report_reentry_impact({"status": "recovered", "reentry_impact": None})

        assert capsys.readouterr().err == ""


def _shell(cmd: str, cwd, **kwargs):
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "abc1234 feat: work", 0, False)
    if "git status --porcelain" in cmd:
        return (True, "", 0, False)
    return (True, "OK", 0, False)


class TestResumeSeamReportsBeforeItSpends:
    """Seam test across the resume boundary: the disclosure has to reach the
    operator from the real entry point, before the coordinator loop runs."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell)
    def test_run_from_review_discloses_the_skipped_review(
        self, _mock_shell, _mock_dev, mock_pool, tmp_path: Path, capsys
    ) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text(STORY_CONTENT, encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()
        _write_record(tmp_path, "test-story", _stopped_state())
        mock_pool.side_effect = lambda **kwargs: [
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name=p.name)
            for p in kwargs["profiles"]
        ]

        result = run_from_review(config, task, workspace, cached_preflight_state=None)

        out = capsys.readouterr().err
        assert "recovered phase record: escalation" in out
        assert "acting on escalation decision land_core_defer_edges" in out
        assert "outstanding: REVIEW cycle 2 has not run" in out
        assert "will NOT run REVIEW" in out
        assert result.state.phase_recovery["reentry_impact"] is not None


def _write_sprint_summary(tmp_path: Path, slug: str) -> Path:
    log_dir = tmp_path / ".forge" / "logs" / "sprint-1"
    log_dir.mkdir(parents=True)
    summary = log_dir / "sprint-summary.yaml"
    summary.write_text(
        yaml.dump(
            {
                "sprint": {"name": "s"},
                "stories": [{"slug": slug, "path": slug, "outcome": "ESCALATED"}],
            }
        ),
        encoding="utf-8",
    )
    return summary


class TestStatusDistinguishesUnrunReview:
    """An unrun review cycle must be visible in the status surface, and must not
    look like a review that completed."""

    def test_unrun_cycle_is_surfaced(self, tmp_path: Path) -> None:
        from theforge.sprint.status_reader import read_completed_status

        _write_record(tmp_path, "test-story", _stopped_state())
        summary = _write_sprint_summary(tmp_path, "test-story")

        entries = read_completed_status(summary, tmp_path)

        assert entries[0].outstanding_phases == ["REVIEW cycle 2 not run"]
        assert "forge review runs REVIEW cycle 2" in entries[0].reentry_note

    def test_completed_review_shows_neither_line(self, tmp_path: Path) -> None:
        from theforge.sprint.status_reader import read_completed_status

        _write_record(tmp_path, "test-story", _stopped_state(verdict="APPROVE"))
        summary = _write_sprint_summary(tmp_path, "test-story")

        entries = read_completed_status(summary, tmp_path)

        assert entries[0].outstanding_phases == []
        assert entries[0].reentry_note == ""

    def test_reader_without_a_project_root_still_works(self, tmp_path: Path) -> None:
        """The re-entry fields need ``.forge``; every other field must not."""
        from theforge.sprint.status_reader import read_completed_status

        _write_record(tmp_path, "test-story", _stopped_state())
        summary = _write_sprint_summary(tmp_path, "test-story")

        entries = read_completed_status(summary)

        assert entries[0].slug == "test-story"
        assert entries[0].outstanding_phases == []

    def test_story_row_prints_both_lines(self, tmp_path: Path, capsys) -> None:
        from theforge.cli.sprint_status import _print_story_line
        from theforge.sprint.status_reader import read_completed_status

        _write_record(tmp_path, "test-story", _stopped_state())
        summary = _write_sprint_summary(tmp_path, "test-story")
        entry = read_completed_status(summary, tmp_path)[0]

        _print_story_line(entry, {"failed": "✗"}, indent=0, title_cache={})

        out = capsys.readouterr().out
        assert "outstanding: REVIEW cycle 2 not run" in out
        assert "re-entry: forge review runs REVIEW cycle 2" in out


class TestPendingDecisionsCarryTheSameWarning:
    """The pending-decision list is where an operator looks when a story is
    waiting on them; it must not be the one place the unrun cycle is missing."""

    def test_pending_entry_names_the_unrun_cycle(self, tmp_path: Path, capsys) -> None:
        from theforge.cli.status import _show_pending_decisions

        _write_record(tmp_path, "test-story", _stopped_state())

        class _Pending:
            @staticmethod
            def list_pending(project_root):
                return [
                    {
                        "run_id": "prior-run",
                        "story": "test-story",
                        "phase": "ESCALATE",
                        "options": ["accept", "reject"],
                    }
                ]

        _show_pending_decisions(_Pending, tmp_path)

        out = capsys.readouterr().out
        assert "outstanding: REVIEW cycle 2 not run" in out
        assert "skips REVIEW" in out

    def test_pending_entry_for_a_completed_review_stays_quiet(
        self, tmp_path: Path, capsys
    ) -> None:
        from theforge.cli.status import _show_pending_decisions

        _write_record(tmp_path, "test-story", _stopped_state(verdict="APPROVE"))

        class _Pending:
            @staticmethod
            def list_pending(project_root):
                return [{"run_id": "prior-run", "story": "test-story", "phase": "ESCALATE"}]

        _show_pending_decisions(_Pending, tmp_path)

        out = capsys.readouterr().out
        assert "outstanding:" not in out
        assert "re-entry:" not in out
