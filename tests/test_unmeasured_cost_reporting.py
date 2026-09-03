"""Unmeasured cost must survive aggregation instead of being coerced to zero (#1992).

The motivating shape is story #1945: three dev iterations on a cost-unmeasured
CLI transport plus a review pool on a transport that does report cost. The story
was reported as ``$0.99`` — the review pool's share — with nothing indicating the
dev phase was absent rather than free. These tests pin the seam end to end: state
aggregation, the operator-facing coordinator surfaces, the structured run/audit
records, and the sprint-level story/summary surfaces.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import yaml
from coord_test_helpers import _make_config, _make_task
from sprint_test_helpers import stub_resolved

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.hooks import build_post_run_payload, build_post_sprint_payload
from theforge.coordinator.model_profiles_bridge import _extract_reviewers
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    ReviewCycleMetadata,
    ReviewIterationTelemetry,
)
from theforge.coordinator.util import _fmt_cost_total, _round_cost, sum_costs
from theforge.runners import AgentResult
from theforge.sprint.audit import _write_sprint_audit, _write_sprint_summary
from theforge.sprint.manifest import ResolvedSprint, SprintResult
from theforge.sprint.state_writer import SprintStateWriter
from theforge.sprint.status_reader import read_completed_status
from theforge.sprint.story_state import SprintStoryState, StoryOutcome

MEASURED_REVIEW_COST = 0.99


def _agent_result(cost: float | None, *, profile_name: str = "agent") -> AgentResult:
    return AgentResult(
        success=True,
        output="ok",
        session_id=None,
        cost_usd=cost,
        exit_code=0,
        raw={},
        profile_name=profile_name,
    )


def _issue_1945_state() -> CoordinatorState:
    """Three unmeasured dev iterations plus a review pool that reported cost."""
    state = CoordinatorState()
    for _ in range(3):
        state.dev_results.append(_agent_result(None, profile_name="codex-dev"))
        state.dev_durations.append(120.0)
    state.review_agent_results.append(
        _agent_result(MEASURED_REVIEW_COST, profile_name="claude-reviewer")
    )
    state.review_durations.append(60.0)
    return state


def _result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


class TestStateAggregation:
    def test_measured_total_is_unknown_when_dev_unmeasured(self) -> None:
        state = _issue_1945_state()
        # The legacy coercing accessor still yields the review-only subtotal —
        # it is a lower bound, not the story's cost.
        assert state.total_cost == MEASURED_REVIEW_COST
        assert state.total_cost_measured is None
        assert state.total_dev_cost_measured is None
        assert state.total_review_cost_measured == MEASURED_REVIEW_COST

    def test_fully_measured_total_is_unchanged(self) -> None:
        state = CoordinatorState()
        state.dev_results.append(_agent_result(0.25))
        state.dev_durations.append(10.0)
        state.review_agent_results.append(_agent_result(0.75))
        state.review_durations.append(10.0)
        assert state.total_cost_measured == 1.0
        assert state.total_cost == 1.0

    def test_sum_costs_treats_a_mix_as_unknown(self) -> None:
        assert sum_costs([0.5, 0.25]) == 0.75
        assert sum_costs([0.5, None]) is None
        assert sum_costs([]) == 0.0
        # A genuinely free run is still free, not unknown.
        assert sum_costs([0.0, 0.0]) == 0.0

    def test_round_cost_preserves_none(self) -> None:
        assert _round_cost(None) is None
        assert _round_cost(0.123456789) == 0.123457


class TestOperatorRendering:
    def test_partial_total_never_renders_as_a_plain_dollar_figure(self) -> None:
        rendered = _fmt_cost_total(None, MEASURED_REVIEW_COST)
        assert rendered != f"${MEASURED_REVIEW_COST:.2f}"
        assert rendered.startswith("unknown")
        # The measured spend stays visible as an explicit lower bound.
        assert "0.99" in rendered

    def test_unknown_with_no_measured_spend(self) -> None:
        assert _fmt_cost_total(None, 0.0) == "unknown"
        assert _fmt_cost_total(None) == "unknown"

    def test_measured_total_renders_as_dollars(self) -> None:
        assert _fmt_cost_total(1.5, 1.5) == "$1.50"


class TestStructuredRecords:
    def test_audit_totals_null_for_partially_unmeasured_story(self, tmp_path: Path) -> None:
        state = _issue_1945_state()
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _result(state))
        assert log["totals"]["cost_usd"] is None
        assert log["cost"]["total_usd"] is None
        assert log["cost"]["dev_usd"] is None
        # The phase that WAS measured keeps its measured value.
        assert log["cost"]["review_usd"] == MEASURED_REVIEW_COST

    def test_review_iteration_telemetry_cost_stays_null(self, tmp_path: Path) -> None:
        state = _issue_1945_state()
        state.review_iteration_telemetry.append(
            ReviewIterationTelemetry(
                iteration=1,
                max_iterations=3,
                cost_usd=None,
                duration_s=12.0,
                verdict="APPROVE",
                findings_by_severity={"P1": 0, "P2": 0},
                new_findings_by_severity={"P1": 0, "P2": 0},
                repeated_findings_by_severity={"P1": 0, "P2": 0},
                novel_findings=0,
                restated_findings=0,
            )
        )
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _result(state))
        assert log["iterations"]["review_loop"][0]["cost_usd"] is None

    def test_post_run_hook_payload_reports_unknown_cost(self, tmp_path: Path) -> None:
        state = _issue_1945_state()
        payload = build_post_run_payload(
            state,
            _make_config(tmp_path),
            _make_task(tmp_path),
            _result(state),
            run_id="run-1992",
            duration_seconds=90.0,
        )
        assert payload["total_cost_usd"] is None

    def test_post_sprint_hook_payload_passes_none_through(self, tmp_path: Path) -> None:
        payload = build_post_sprint_payload(
            sprint_name="s",
            stories=[],
            run_id="run-1992",
            config=_make_config(tmp_path),
            total_cost_usd=None,
            duration_seconds=1.0,
        )
        assert payload["total_cost_usd"] is None

    def test_profile_fold_receives_unmeasured_reviewer_cost(self) -> None:
        """Routing evidence must not learn that an unpriced reviewer was free."""
        state = CoordinatorState()
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["codex-reviewer"],
                successful=["codex-reviewer"],
                failed=[],
                synthesized=False,
                parse_retries=0,
            )
        )
        state.review_iteration_telemetry.append(
            ReviewIterationTelemetry(
                iteration=1,
                max_iterations=3,
                cost_usd=None,
                duration_s=10.0,
                verdict="APPROVE",
                findings_by_severity={"P1": 0, "P2": 0},
                new_findings_by_severity={"P1": 0, "P2": 0},
                repeated_findings_by_severity={"P1": 0, "P2": 0},
                novel_findings=0,
                restated_findings=0,
            )
        )
        cycles, findings, cost = _extract_reviewers(state)["codex-reviewer"]
        assert cycles == 1
        assert findings == 0
        assert cost is None


class TestSprintStorySurfaces:
    def test_story_state_round_trips_unknown_cost(self) -> None:
        state = SprintStoryState()
        state.register("issue-1945", "Issue #1945")
        state.transition("issue-1945", outcome=StoryOutcome.DONE, cost_usd=None)
        assert state.get("issue-1945").cost_usd is None
        restored = SprintStoryState.from_dict(state.as_dict())
        assert restored.get("issue-1945").cost_usd is None

    def test_state_writer_persists_null_cost(self, tmp_path: Path) -> None:
        writer = SprintStateWriter("run-1992", tmp_path, "s", sprint_id="sid")
        writer.init([{"slug": "issue-1945", "path": "Issue #1945", "status": "running"}])
        writer.update("issue-1945", status="running", cost_usd=None)
        data = yaml.safe_load((tmp_path / ".forge" / "runs" / "run-1992.state").read_text())
        assert data["stories"][0]["cost_usd"] is None

    def test_completed_status_entry_keeps_cost_unknown(self, tmp_path: Path) -> None:
        summary = tmp_path / "sprint-summary.yaml"
        summary.write_text(
            yaml.safe_dump(
                {
                    "sprint": {"name": "s", "total_cost_usd": None, "cost_complete": False},
                    "stories": [
                        {
                            "slug": "issue-1945",
                            "path": "Issue #1945",
                            "outcome": "DONE",
                            "cost_usd": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        entries = read_completed_status(summary)
        assert [e.cost_usd for e in entries] == [None]

    def test_story_row_renders_unknown_not_a_dash(self, capsys) -> None:
        """A cost-unknown story must not render like a story that spent nothing."""
        from theforge.cli.sprint_status import _print_story_line

        entry = type(
            "E",
            (),
            {
                "slug": "issue-1945",
                "path": "Issue #1945",
                "status": "done",
                "phase": "DONE",
                "stage": "",
                "cost_usd": None,
                "elapsed_seconds": 60.0,
                "detail": "",
                "complexity": None,
                "complexity_score": None,
                "model": None,
            },
        )()
        _print_story_line(entry, {"done": "✓"}, 0)
        rendered = capsys.readouterr().out
        assert "unknown" in rendered
        assert "$" not in rendered


def _resolved_sprint() -> ResolvedSprint:
    return ResolvedSprint(name="test-sprint", budget_usd=10.0, stories=[], max_parallel=1)


class TestSprintTotals:
    def test_sprint_audit_total_is_null_when_a_story_cost_is_unknown(self, tmp_path: Path) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        result = SprintResult(
            name="test-sprint",
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=MEASURED_REVIEW_COST,
            budget_usd=10.0,
            cost_complete=False,
            results=[],
        )
        _write_sprint_audit(
            manifest=_resolved_sprint(),
            result=result,
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=0.0,
            project_root=tmp_path,
        )
        audit = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
        assert audit["sprint"]["total_cost_usd"] is None
        assert audit["sprint"]["cost_complete"] is False
        assert audit["sprint"]["total_cost_measured_usd"] == MEASURED_REVIEW_COST

    def test_sprint_summary_total_is_null_when_a_story_cost_is_unknown(
        self, tmp_path: Path
    ) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        story_state = SprintStoryState()
        story_state.register("issue-1945", "Issue #1945")
        story_state.transition("issue-1945", outcome=StoryOutcome.DONE, cost_usd=None)
        story_state.register("issue-1946", "Issue #1946")
        story_state.transition("issue-1946", outcome=StoryOutcome.DONE, cost_usd=0.5)
        result = SprintResult(
            name="test-sprint",
            specs_total=2,
            specs_succeeded=2,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.5,
            budget_usd=10.0,
            cost_complete=False,
            results=[],
        )
        _write_sprint_summary(
            manifest=_resolved_sprint(),
            result=result,
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=0.0,
            sprint_log_dir=tmp_path / "logs",
            project_root=tmp_path,
            story_state=story_state,
        )
        summary = yaml.safe_load((tmp_path / "logs" / "sprint-summary.yaml").read_text())
        assert summary["sprint"]["total_cost_usd"] is None
        assert summary["sprint"]["cost_complete"] is False
        assert summary["sprint"]["total_cost_measured_usd"] == 0.5

    def test_fully_measured_sprint_still_reports_a_total(self, tmp_path: Path) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        story_state = SprintStoryState()
        story_state.register("issue-1946", "Issue #1946")
        story_state.transition("issue-1946", outcome=StoryOutcome.DONE, cost_usd=0.5)
        result = SprintResult(
            name="test-sprint",
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.5,
            budget_usd=10.0,
            results=[],
        )
        _write_sprint_summary(
            manifest=_resolved_sprint(),
            result=result,
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=0.0,
            sprint_log_dir=tmp_path / "logs",
            project_root=tmp_path,
            story_state=story_state,
        )
        summary = yaml.safe_load((tmp_path / "logs" / "sprint-summary.yaml").read_text())
        assert summary["sprint"]["total_cost_usd"] == 0.5
        assert summary["sprint"]["cost_complete"] is True


class TestCliRenderers:
    def test_forge_audit_renders_unknown_instead_of_zero(self) -> None:
        from theforge.cli.audit import _cost_str

        assert _cost_str(None) == "unknown"
        assert _cost_str(0.0) == "$0.0000"
        assert _cost_str(1.5) == "$1.5000"

    def test_recent_runs_row_shows_unknown_for_incomplete_sprint(self) -> None:
        from theforge.cli.status import _historical_row_from_substrate

        record = {
            "run_id": "abc123",
            "timing": {"started_at": "2026-07-26T00:00:00", "duration_seconds": 60},
            "sprint": {
                "total_cost_usd": None,
                "cost_complete": False,
                "total_cost_measured_usd": MEASURED_REVIEW_COST,
                "duration_seconds": 60,
            },
            "totals": {"cost_usd": MEASURED_REVIEW_COST},
        }
        row = _historical_row_from_substrate(record)
        assert row is not None
        assert row[4] == "unknown"

    def test_recent_runs_row_still_shows_a_measured_sprint_total(self) -> None:
        from theforge.cli.status import _historical_row_from_substrate

        record = {
            "run_id": "abc123",
            "timing": {"started_at": "2026-07-26T00:00:00", "duration_seconds": 60},
            "sprint": {
                "total_cost_usd": 2.5,
                "cost_complete": True,
                "duration_seconds": 60,
            },
        }
        row = _historical_row_from_substrate(record)
        assert row is not None
        assert row[4] == "$2.50"

    def test_run_status_does_not_scrape_a_lower_bound_as_the_total(self, tmp_path: Path) -> None:
        """`forge status` must not recover a dollar figure from an unknown total."""
        from theforge import detach

        log_dir = tmp_path / ".forge" / "logs" / "issue-1945"
        log_dir.mkdir(parents=True)
        (log_dir / "run-run1992.log").write_text(
            "[forge] Total cost: unknown (>= $0.99 measured)   Duration: 13m\n",
            encoding="utf-8",
        )
        status = detach.read_run_status("run1992", "issue-1945", tmp_path)
        assert status["cost_usd"] is None


class TestPhaseAggregationSeams:
    def test_dev_iteration_with_mixed_attempts_records_unknown_cost(self, tmp_path: Path) -> None:
        """A retry that reported cost does not make the whole iteration measured."""
        from unittest.mock import patch

        from theforge.coordinator.dev_phase import record_dev_iteration_telemetry

        state = CoordinatorState()
        state.dev_results.append(_agent_result(None, profile_name="codex-dev"))
        state.dev_durations.append(100.0)
        state.dev_results.append(_agent_result(0.42, profile_name="api-fallback"))
        state.dev_durations.append(50.0)
        state.pending_dev_transport_retry_count = 1

        with patch("theforge.coordinator.dev_phase._git_lines", return_value=[]):
            record_dev_iteration_telemetry(
                state,
                tmp_path,
                max_iterations=3,
                gate_result="PASS",
            )

        assert state.dev_iteration_telemetry[-1].cost_usd is None

    def test_dev_iteration_with_all_measured_attempts_sums_them(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from theforge.coordinator.dev_phase import record_dev_iteration_telemetry

        state = CoordinatorState()
        state.dev_results.append(_agent_result(0.10))
        state.dev_durations.append(100.0)
        state.dev_results.append(_agent_result(0.42))
        state.dev_durations.append(50.0)
        state.pending_dev_transport_retry_count = 1

        with patch("theforge.coordinator.dev_phase._git_lines", return_value=[]):
            record_dev_iteration_telemetry(
                state,
                tmp_path,
                max_iterations=3,
                gate_result="PASS",
            )

        assert state.dev_iteration_telemetry[-1].cost_usd == 0.52


class TestPullRequestBody:
    """The PR body is the durable operator-facing record of a story's cost."""

    @staticmethod
    def _capture_pr_body(tmp_path: Path, state: CoordinatorState) -> str:
        from unittest.mock import MagicMock, patch

        from test_operator_action_lifecycle import (
            _make_issue_task,
            _make_merge_pr_config,
            _make_review_result,
        )

        from theforge.coordinator import completion

        bodies: list[str] = []

        def _sub(returncode: int, stdout: str = "") -> MagicMock:
            proc = MagicMock()
            proc.returncode = returncode
            proc.stdout = stdout
            proc.stderr = ""
            return proc

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list):
                if cmd[:2] == ["gh", "issue"] and "labels" in cmd:
                    import json

                    return _sub(0, stdout=json.dumps({"labels": []}))
                if cmd[:3] == ["git", "rev-list", "--count"]:
                    return _sub(0, stdout="1\n")
                if "pr" in cmd and "list" in cmd:
                    return _sub(0, stdout="[]")
                for i, arg in enumerate(cmd):
                    if arg == "--body" and i + 1 < len(cmd):
                        bodies.append(cmd[i + 1])
            return _sub(0, stdout="https://github.com/x/y/pull/10")

        config = _make_merge_pr_config(tmp_path)
        task = _make_issue_task(tmp_path, 1945)
        with patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run):
            result = completion._create_pr(
                config, task, "forge/issue-1945", _make_review_result(), state
            )
        assert result["success"] is True, result
        assert len(bodies) == 1, bodies
        return bodies[0]

    def test_pr_body_reports_unknown_cost_for_partially_unmeasured_story(
        self, tmp_path: Path
    ) -> None:
        body = self._capture_pr_body(tmp_path, _issue_1945_state())
        assert "- **Cost:** unknown" in body
        assert "- **Cost:** $0.99" not in body

    def test_pr_body_reports_a_measured_cost_unchanged(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.dev_results.append(_agent_result(0.5))
        state.dev_durations.append(10.0)
        body = self._capture_pr_body(tmp_path, state)
        assert "- **Cost:** $0.50" in body


class TestBudgetEnforcement:
    """The cap can only be enforced against a number that means what it says.

    ``accumulated_cost`` sums measured spend, so a story whose transport
    reported no cost contributes ``0.0`` while having spent an unknown amount.
    Comparing that understated total against the cap would certify a budget the
    sprint cannot show it is within, so the check fails closed instead (#1992).
    """

    def test_measured_spend_under_cap_dispatches(self) -> None:
        from theforge.sprint.budget import evaluate_budget

        assert (
            evaluate_budget(
                accumulated_cost=1.0,
                prior_cost=0.5,
                budget_usd=10.0,
                unmeasured_spend=[],
            )
            is None
        )

    def test_measured_spend_over_cap_still_reports_exhausted(self) -> None:
        from theforge.sprint.budget import evaluate_budget

        block = evaluate_budget(
            accumulated_cost=0.0,
            prior_cost=6.0,
            budget_usd=5.0,
            unmeasured_spend=[],
        )
        assert block is not None
        assert block.kind == "exhausted"
        # Wording is load-bearing for existing operator surfaces and tests.
        assert block.stopped_reason == (
            "Budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )

    def test_exhaustion_wins_over_unverifiable(self) -> None:
        """A definite over-cap answer stays definite even with unmeasured spend."""
        from theforge.sprint.budget import evaluate_budget

        block = evaluate_budget(
            accumulated_cost=6.0,
            prior_cost=0.0,
            budget_usd=5.0,
            unmeasured_spend=["issue-1945"],
        )
        assert block is not None
        assert block.kind == "exhausted"

    def test_unmeasured_spend_under_cap_blocks_dispatch(self) -> None:
        from theforge.sprint.budget import evaluate_budget

        block = evaluate_budget(
            accumulated_cost=MEASURED_REVIEW_COST,
            prior_cost=0.0,
            budget_usd=10.0,
            unmeasured_spend=["issue-1945"],
            acceptable_unmeasured_spend_sources=["issue-1945"],
        )
        assert block is not None
        assert block.kind == "unverifiable"
        assert "issue-1945" in block.story_reason
        assert "lower bound" in block.story_reason
        assert "--accept-unmeasured-spend issue-1945" in block.story_reason
        assert block.stopped_reason.startswith("Budget unverifiable")

    def test_unmeasured_source_list_is_elided_not_dropped(self) -> None:
        from theforge.sprint.budget import describe_unmeasured_spend

        rendered = describe_unmeasured_spend([f"issue-{n}" for n in range(8)])
        assert rendered.startswith("issue-0, issue-1")
        assert "+3 more" in rendered

    def test_intake_agent_without_reported_cost_is_unmeasured_not_free(self) -> None:
        """An intake auto-fix that ran on an unpriced transport is not free."""
        from theforge.sprint.runner import _intake_outcome_cost, _intake_outcome_cost_measured

        class _Outcome:
            def __init__(self, agent: dict) -> None:
                self.audit = {"agent": agent}

        attempted_unpriced = _Outcome({"attempted": True, "cost_usd": None})
        assert _intake_outcome_cost_measured(attempted_unpriced) is None
        # The numeric accessor still yields a usable lower bound for rollups.
        assert _intake_outcome_cost(attempted_unpriced) == 0.0

        never_ran = _Outcome({"attempted": False, "cost_usd": None})
        assert _intake_outcome_cost_measured(never_ran) == 0.0

        priced = _Outcome({"attempted": True, "cost_usd": 0.25})
        assert _intake_outcome_cost_measured(priced) == 0.25


class TestBudgetEnforcementSeam:
    """End-to-end: an unmeasured story must stop the sprint dispatching more."""

    @staticmethod
    def _unmeasured_dev_result():
        from theforge.coordinator.state import CoordinatorResult as _CR
        from theforge.coordinator.state import CoordinatorState as _CS

        state = _CS()
        state.preflight_verdict = "PROCEED"
        state.dev_results.append(_agent_result(None, profile_name="codex-dev"))
        state.dev_durations.append(60.0)
        state.review_agent_results.append(
            _agent_result(MEASURED_REVIEW_COST, profile_name="claude-reviewer")
        )
        state.review_durations.append(30.0)
        return _CR(success=True, phase=Phase.DONE, state=state, message="Done.")

    def test_second_story_is_not_dispatched_after_unmeasured_spend(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.dag import StoryTriage

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
        config = _make_config(tmp_path)

        def _triage(spec_path, *args, **kwargs):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                return_value=self._unmeasured_dev_result(),
            ) as mock_run:
                result = run_sprint_ctx(config, manifest_path)

        # The budget is nowhere near exhausted at the measured lower bound —
        # the sprint stops because that bound is not the spend.
        assert mock_run.call_count == 1
        assert result.stopped_reason is not None
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert result.specs_skipped == 1
        assert result.cost_complete is False

    def test_sprint_records_which_spend_was_unmeasured(self, tmp_path: Path) -> None:
        """Convention 6: the refusal must be traceable to the work that caused it."""
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.dag import StoryTriage

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
        config = _make_config(tmp_path)

        def _triage(spec_path, *args, **kwargs):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                return_value=self._unmeasured_dev_result(),
            ):
                result = run_sprint_ctx(config, manifest_path)

        assert result.unmeasured_spend_sources  # names the story that ran unpriced
        audit = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )
        assert audit["sprint"]["cost_complete"] is False
        assert audit["sprint"]["total_cost_usd"] is None
        assert audit["sprint"]["unmeasured_spend_sources"]
        skipped = [s for s in audit["specs"] if s["outcome"] == "SKIPPED"]
        assert skipped, audit["specs"]
        assert "budget unverifiable" in (skipped[0]["error"] or "")

    def test_fully_measured_sprint_dispatches_every_story(self, tmp_path: Path) -> None:
        """The fail-closed path must not fire when every story reports cost."""
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import (
            _make_config,
            _make_coordinator_result,
            _make_manifest,
            _make_spec_file,
        )

        from theforge.sprint.dag import StoryTriage

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
        config = _make_config(tmp_path)

        def _triage(spec_path, *args, **kwargs):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                side_effect=lambda *a, **k: _make_coordinator_result(success=True, cost=1.0),
            ) as mock_run:
                result = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 2
        assert result.stopped_reason is None
        assert result.cost_complete is True
        assert result.unmeasured_spend_sources == ()

    def test_resume_inherits_the_unmeasured_flag_from_the_prior_generation(
        self, tmp_path: Path
    ) -> None:
        """Carried spend from an incomplete generation is a lower bound too."""
        from theforge.sprint.runner import _prior_sprint_cost_incomplete

        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        (audits / "sprint-audit.yaml").write_text(
            yaml.safe_dump(
                {
                    "sprint": {
                        "sprint_id": "sid",
                        "total_cost_usd": None,
                        "total_cost_measured_usd": MEASURED_REVIEW_COST,
                        "cost_complete": False,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert _prior_sprint_cost_incomplete(tmp_path, "sid") is True
        # A different sprint's record must not leak its incompleteness.
        assert _prior_sprint_cost_incomplete(tmp_path, "other-sid") is False

    def test_resume_from_a_complete_generation_is_not_flagged(self, tmp_path: Path) -> None:
        from theforge.sprint.runner import _prior_sprint_cost_incomplete

        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        (audits / "sprint-audit.yaml").write_text(
            yaml.safe_dump(
                {"sprint": {"sprint_id": "sid", "total_cost_usd": 2.0, "cost_complete": True}}
            ),
            encoding="utf-8",
        )
        assert _prior_sprint_cost_incomplete(tmp_path, "sid") is False
        # A pre-#1992 record carries no completeness claim and must not block.
        (audits / "sprint-audit.yaml").write_text(
            yaml.safe_dump({"sprint": {"sprint_id": "sid", "total_cost_usd": 2.0}}),
            encoding="utf-8",
        )
        assert _prior_sprint_cost_incomplete(tmp_path, "sid") is False


# ── Resolving unmeasured spend deliberately (#2310) ───────────────────────────
#
# Refusing to spend against a cap that cannot be verified is right. A guard that
# no action can satisfy is not: its only exit is to abandon the pipeline for the
# story, which is the outcome the guard exists to prevent. These tests pin the
# deliberate exit — the unknown is bounded, attributed, accepted by name, and the
# resolution is recorded WITHOUT the cost ever being relabelled as measured.

_ALLOCATION_USD = 5.0
_MEASURED_BEFORE_FAILURE = 0.5


def _bounded_story_audit(*, run_id: str = "194febaf01fd") -> dict:
    """A per-story audit for a run whose reviewer exited without a cost."""
    return {
        "run_id": run_id,
        "outcome": {"final_phase": "FAILED"},
        "error_type": "provider_quota",
        "cost": {
            "total_usd": None,
            "agents": [
                {
                    "role": "dev",
                    "profile": "claude-dev",
                    "cost_usd": _MEASURED_BEFORE_FAILURE,
                    "success": True,
                },
                {
                    "role": "review",
                    "profile": "gpt-reviewer",
                    "cost_usd": None,
                    "success": False,
                    "failure_code": "quota_exhausted",
                },
            ],
            "story_allocation": {
                "allocation_usd": _ALLOCATION_USD,
                "basis": "substrate_band",
                "complexity_score": 4,
                "fallback_configured_usd": 8.0,
            },
        },
    }


def _write_story_audit(tmp_path: Path, sprint_name: str, slug: str, data: dict) -> None:
    story_dir = tmp_path / ".forge" / "logs" / sprint_name / slug
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "audit.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


class TestUnmeasuredSourceIdentity:
    """A carried source and a fresh one name the same work."""

    def test_carried_and_bare_ids_normalize_together(self) -> None:
        from theforge.sprint.unmeasured import normalize_source_id

        assert normalize_source_id("carried:issue-2206") == "issue-2206"
        assert normalize_source_id("issue-2206") == "issue-2206"
        assert normalize_source_id("  carried:carried:issue-2206 ") == "issue-2206"
        assert normalize_source_id(None) == ""

    def test_only_story_sources_resolve_to_a_slug(self) -> None:
        from theforge.sprint.unmeasured import source_slug

        assert source_slug("carried:issue-2206") == "issue-2206"
        assert source_slug("issue-2206") == "issue-2206"
        # Kind-prefixed sources are not story runs, and the whole-generation
        # marker is not work at all — neither has a per-story audit to read.
        assert source_slug("intake:issue-2206") is None
        assert source_slug("carried:prior-generation") is None


class TestUnmeasuredSourceDerivation:
    """The bound and the origin are read off records, never invented."""

    def test_recorded_allocation_bounds_the_unmeasured_remainder(self) -> None:
        from theforge.sprint.unmeasured import build_source

        source = build_source("carried:issue-2206", _bounded_story_audit())
        assert source.source == "issue-2206"
        assert source.acceptable is True
        # The measured part is already in the sprint's accumulated total; only
        # the remainder of the allocation stands in for what is unknown.
        assert source.measured_lower_bound_usd == _MEASURED_BEFORE_FAILURE
        assert source.ceiling_usd == round(_ALLOCATION_USD - _MEASURED_BEFORE_FAILURE, 4)
        assert source.origin["run_id"] == "194febaf01fd"
        assert source.origin["role"] == "review"
        assert source.origin["phase"] == "REVIEW"
        assert source.origin["profile"] == "gpt-reviewer"
        assert source.origin["failure_code"] == "quota_exhausted"

    def test_a_source_with_no_recorded_allocation_stays_unbounded(self) -> None:
        """No fabricated bound: an unreadable origin keeps the guard closed."""
        from theforge.sprint.unmeasured import accept, build_source

        assert build_source("issue-2206", None).acceptable is False
        no_allocation = build_source(
            "issue-2206", {"run_id": "r1", "cost": {"total_usd": None, "agents": []}}
        )
        assert no_allocation.acceptable is False
        assert accept(no_allocation, accepted_at="2026-08-08T00:00:00+00:00") is None

    def test_acceptance_record_round_trips_through_persistence(self) -> None:
        from theforge.sprint.unmeasured import (
            AcceptedUnmeasuredSpend,
            accept,
            build_source,
        )

        record = accept(
            build_source("carried:issue-2206", _bounded_story_audit()),
            accepted_at="2026-08-08T00:00:00+00:00",
            reason="reviewer hit a provider quota; landed by hand",
        )
        assert record is not None
        restored = AcceptedUnmeasuredSpend.from_dict(record.as_dict())
        assert restored == record
        # A record without a numeric ceiling resolves nothing and is dropped.
        assert AcceptedUnmeasuredSpend.from_dict({"source": "issue-2206"}) is None

    def test_only_sources_this_run_carries_are_charged(self) -> None:
        from theforge.sprint.unmeasured import (
            accept,
            accepted_by_source,
            accepted_ceiling_total,
            build_source,
            partition,
        )

        record = accept(
            build_source("issue-2206", _bounded_story_audit()),
            accepted_at="2026-08-08T00:00:00+00:00",
        )
        index = accepted_by_source([record])
        # Matches the carried spelling of the same source...
        unresolved, applied = partition(["carried:issue-2206", "carried:prior-generation"], index)
        assert unresolved == ["carried:prior-generation"]
        assert accepted_ceiling_total(applied) == 4.5
        # ...and charges nothing when this run carries no such spend.
        unresolved, applied = partition(["issue-9999"], index)
        assert unresolved == ["issue-9999"]
        assert accepted_ceiling_total(applied) == 0.0


class TestBudgetWithAcceptedCeilings:
    def test_accepted_ceiling_replaces_the_unknown_and_dispatch_resumes(self) -> None:
        from theforge.sprint.budget import evaluate_budget

        assert (
            evaluate_budget(
                accumulated_cost=0.5,
                prior_cost=0.0,
                budget_usd=100.0,
                unmeasured_spend=[],
                accepted_unmeasured_ceiling_usd=4.5,
            )
            is None
        )

    def test_accepted_ceiling_is_charged_not_forgiven(self) -> None:
        """Accepting never buys headroom — the ceiling counts against the cap."""
        from theforge.sprint.budget import evaluate_budget

        block = evaluate_budget(
            accumulated_cost=1.0,
            prior_cost=0.0,
            budget_usd=5.0,
            unmeasured_spend=[],
            accepted_unmeasured_ceiling_usd=4.5,
        )
        assert block is not None
        assert block.kind == "exhausted"
        assert "accepted unmeasured ceiling $4.50" in block.detail

    def test_exhaustion_wording_is_unchanged_when_nothing_was_accepted(self) -> None:
        from theforge.sprint.budget import evaluate_budget

        block = evaluate_budget(
            accumulated_cost=0.0,
            prior_cost=6.0,
            budget_usd=5.0,
            unmeasured_spend=[],
        )
        assert block is not None
        assert block.stopped_reason == (
            "Budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )

    def test_refusal_names_the_amount_and_the_origin(self) -> None:
        """An unknown can only be accepted if it is bounded and attributed."""
        from theforge.sprint.budget import evaluate_budget
        from theforge.sprint.unmeasured import build_source

        source = build_source("carried:issue-2206", _bounded_story_audit())
        block = evaluate_budget(
            accumulated_cost=0.0,
            prior_cost=0.0,
            budget_usd=100.0,
            unmeasured_spend=["carried:issue-2206"],
            source_details={"carried:issue-2206": source.describe()},
        )
        assert block is not None
        assert block.kind == "unverifiable"
        assert "at most $4.50 more" in block.detail
        assert "run_id=194febaf01fd" in block.detail
        assert "role=review" in block.detail

    def test_budget_verification_spend_sums_measured_and_accepted(self) -> None:
        from theforge.sprint.budget import budget_verification_spend

        assert (
            budget_verification_spend(
                accumulated_cost=1.0,
                prior_cost=0.5,
                accepted_unmeasured_ceiling_usd=4.5,
            )
            == 6.0
        )


class TestPriorGenerationCarryIsSourceAware:
    @staticmethod
    def _write_incomplete_audit(tmp_path: Path, sources: list[str] | None) -> None:
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True, exist_ok=True)
        block = {
            "sprint_id": "sid",
            "total_cost_usd": None,
            "total_cost_measured_usd": 0.0,
            "cost_complete": False,
        }
        if sources is not None:
            block["unmeasured_spend_sources"] = sources
        (audits / "sprint-audit.yaml").write_text(
            yaml.safe_dump({"sprint": block}), encoding="utf-8"
        )

    def test_accepting_every_named_source_clears_the_generation_carry(
        self, tmp_path: Path
    ) -> None:
        from theforge.sprint.runner import _prior_sprint_cost_incomplete
        from theforge.sprint.unmeasured import accept, accepted_by_source, build_source

        self._write_incomplete_audit(tmp_path, ["issue-2206"])
        assert _prior_sprint_cost_incomplete(tmp_path, "sid") is True
        accepted = accepted_by_source(
            [
                accept(
                    build_source("issue-2206", _bounded_story_audit()),
                    accepted_at="2026-08-08T00:00:00+00:00",
                )
            ]
        )
        assert _prior_sprint_cost_incomplete(tmp_path, "sid", accepted) is False

    def test_a_partially_accepted_generation_still_carries(self, tmp_path: Path) -> None:
        from theforge.sprint.runner import _prior_sprint_cost_incomplete
        from theforge.sprint.unmeasured import accept, accepted_by_source, build_source

        self._write_incomplete_audit(tmp_path, ["issue-2206", "intake:issue-2207"])
        accepted = accepted_by_source(
            [
                accept(
                    build_source("issue-2206", _bounded_story_audit()),
                    accepted_at="2026-08-08T00:00:00+00:00",
                )
            ]
        )
        assert _prior_sprint_cost_incomplete(tmp_path, "sid", accepted) is True

    def test_an_incomplete_generation_that_named_nothing_still_carries(
        self, tmp_path: Path
    ) -> None:
        """Nothing there for an operator to have resolved — stay closed."""
        from theforge.sprint.runner import _prior_sprint_cost_incomplete
        from theforge.sprint.unmeasured import accept, accepted_by_source, build_source

        self._write_incomplete_audit(tmp_path, None)
        accepted = accepted_by_source(
            [
                accept(
                    build_source("issue-2206", _bounded_story_audit()),
                    accepted_at="2026-08-08T00:00:00+00:00",
                )
            ]
        )
        assert _prior_sprint_cost_incomplete(tmp_path, "sid", accepted) is True


class TestUnmeasuredResolutionSeam:
    """The reported failure end to end: refuse, accept, run, record."""

    @staticmethod
    def _arrange(tmp_path: Path):
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.audit import persist_accumulated_story_state

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _make_config(tmp_path)

        sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
        sprint_dir.mkdir(parents=True, exist_ok=True)
        (sprint_dir / ".sprint_id").write_text("sprint-2310", encoding="utf-8")
        _write_story_audit(tmp_path, "Test Sprint", "feature-a", _bounded_story_audit())
        persist_accumulated_story_state(
            "sprint-2310",
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                }
            ],
        )
        return config, manifest_path

    @staticmethod
    def _run(config, manifest_path, **kwargs):
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_coordinator_result

        from theforge.sprint.dag import StoryTriage

        def _triage(spec_path, *args, **kw):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                side_effect=lambda *a, **k: _make_coordinator_result(success=True, cost=1.0),
            ) as mock_run:
                result = run_sprint_ctx(config, manifest_path, **kwargs)
        return result, mock_run

    def test_carried_unmeasured_story_is_refused_with_its_amount_and_origin(
        self, tmp_path: Path
    ) -> None:
        config, manifest_path = self._arrange(tmp_path)
        result, mock_run = self._run(config, manifest_path)

        assert mock_run.call_count == 0
        assert result.stopped_reason is not None
        assert result.stopped_reason.startswith("Budget unverifiable")
        # The refusal is no longer opaque: it names the bound and the call.
        assert "at most $4.50 more" in result.stopped_reason
        assert "role=review" in result.stopped_reason
        assert "--accept-unmeasured-spend feature-a" in result.stopped_reason
        assert result.unresolved_unmeasured_spend_sources
        assert result.accepted_unmeasured_spend == ()
        audit = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )
        skipped = [story for story in audit["specs"] if story["outcome"] == "SKIPPED"]
        assert skipped, audit["specs"]
        assert "--accept-unmeasured-spend feature-a" in (skipped[0]["error"] or "")

    def test_accepting_the_source_lets_the_same_story_run(self, tmp_path: Path) -> None:
        config, manifest_path = self._arrange(tmp_path)
        result, mock_run = self._run(
            config,
            manifest_path,
            accept_unmeasured_spend=["carried:feature-a"],
            accept_unmeasured_reason="reviewer hit a provider quota",
        )

        assert mock_run.call_count == 1
        assert result.stopped_reason is None
        assert result.unresolved_unmeasured_spend_sources == ()
        # The spend is still unmeasured — acceptance resolved the budget
        # question, not the measurement one.
        assert result.cost_complete is False
        assert "carried:feature-a" in result.unmeasured_spend_sources
        [accepted] = result.accepted_unmeasured_spend
        assert accepted["source"] == "feature-a"
        assert accepted["accepted_ceiling_usd"] == 4.5
        assert accepted["origin_run_id"] == "194febaf01fd"
        assert accepted["origin_role"] == "review"
        assert accepted["origin_failure_code"] == "quota_exhausted"
        assert accepted["reason"] == "reviewer hit a provider quota"
        # Budget verification charged the ceiling on top of measured spend.
        assert result.budget_verification_spend_usd >= 4.5

    def test_an_unbounded_source_cannot_be_accepted(self, tmp_path: Path) -> None:
        """Without a recorded bound there is nothing to accept — stay closed."""
        config, manifest_path = self._arrange(tmp_path)
        # Strip the allocation that made the source bounded.
        _write_story_audit(
            tmp_path,
            "Test Sprint",
            "feature-a",
            {"run_id": "r1", "cost": {"total_usd": None, "agents": []}},
        )
        result, mock_run = self._run(config, manifest_path, accept_unmeasured_spend=["feature-a"])

        assert mock_run.call_count == 0
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert result.accepted_unmeasured_spend == ()

    def test_a_source_this_sprint_never_flagged_is_refused(self, tmp_path: Path) -> None:
        config, manifest_path = self._arrange(tmp_path)
        result, mock_run = self._run(config, manifest_path, accept_unmeasured_spend=["issue-9999"])

        assert mock_run.call_count == 0
        assert result.accepted_unmeasured_spend == ()

    def test_audit_and_summary_record_the_resolution(self, tmp_path: Path) -> None:
        config, manifest_path = self._arrange(tmp_path)
        self._run(config, manifest_path, accept_unmeasured_spend=["feature-a"])

        audit = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )["sprint"]
        # Still incomplete: an acceptance is not a measurement.
        assert audit["cost_complete"] is False
        assert audit["total_cost_usd"] is None
        assert audit["unresolved_unmeasured_spend_sources"] == []
        assert audit["unmeasured_spend_sources"]
        [accepted] = audit["accepted_unmeasured_spend"]
        assert accepted["source"] == "feature-a"
        assert accepted["accepted_ceiling_usd"] == 4.5
        assert accepted["origin_run_id"] == "194febaf01fd"
        assert audit["budget_verification_spend_usd"] >= 4.5

        summary = yaml.safe_load(
            (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text(
                encoding="utf-8"
            )
        )["sprint"]
        assert summary["cost_complete"] is False
        assert summary["unresolved_unmeasured_spend_sources"] == []
        assert summary["accepted_unmeasured_spend"][0]["source"] == "feature-a"
        assert summary["budget_verification_spend_usd"] >= 4.5

    def test_the_resolution_survives_into_the_next_run(self, tmp_path: Path) -> None:
        """The operator resolves it once; a later run reads the record."""
        config, manifest_path = self._arrange(tmp_path)
        self._run(config, manifest_path, accept_unmeasured_spend=["feature-a"])

        persisted = yaml.safe_load(
            (tmp_path / ".forge" / "sprints" / "sprint-2310" / "state.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert persisted["accepted_unmeasured_spend"][0]["source"] == "feature-a"

        # The story goes unmeasured again on a later attempt: the recorded
        # resolution still stands, with no flag on this invocation at all. The
        # current generation flags it bare (``feature-a``) rather than carried,
        # so acceptance has to key on the normalized id or it would re-block.
        state_path = tmp_path / ".forge" / "sprints" / "sprint-2310" / "state.yaml"
        state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        for story in state["stories"]:
            story["cost_usd"] = None
        state_path.write_text(yaml.safe_dump(state), encoding="utf-8")

        result, mock_run = self._run(config, manifest_path)
        assert mock_run.call_count == 1
        assert result.stopped_reason is None
        assert result.unresolved_unmeasured_spend_sources == ()
        assert result.accepted_unmeasured_spend[0]["source"] == "feature-a"


class TestAcceptanceReachesTheRunner:
    """CLI plumbing: the option is parsed and threaded through every path."""

    def test_option_is_repeatable_and_parsed(self) -> None:
        import argparse

        from theforge.cli.sprint import register_parser

        parser = argparse.ArgumentParser()
        register_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(
            [
                "sprint",
                "sprint.yaml",
                "--accept-unmeasured-spend",
                "issue-2206",
                "--accept-unmeasured-spend",
                "carried:prior-generation",
                "--accept-unmeasured-reason",
                "quota failure, landed by hand",
            ]
        )
        assert args.accept_unmeasured_spend == ["issue-2206", "carried:prior-generation"]
        assert args.accept_unmeasured_reason == "quota failure, landed by hand"
        # Absent by default, so nothing is accepted unless asked for.
        assert parser.parse_args(["sprint", "sprint.yaml"]).accept_unmeasured_spend is None

    def test_cmd_sprint_passes_the_acceptance_to_run_sprint(self, tmp_path: Path) -> None:
        import argparse
        from unittest.mock import MagicMock, patch

        from test_sprint_resume import _make_manifest, _make_spec_file

        from theforge.cli import sprint as sprint_cli

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config_path = tmp_path / "forge.yaml"
        config_path.write_text("project: test\n", encoding="utf-8")

        fake_config = MagicMock()
        fake_config.project_root = tmp_path
        fake_config.project = "test"
        fake_result = MagicMock()
        fake_result.specs_failed = 0

        args = argparse.Namespace(
            manifest=str(manifest_path),
            milestone=None,
            label=None,
            issues=None,
            config=str(config_path),
            base_branch=None,
            name=None,
            fg=True,
            detach=False,
            dry_run=False,
            verbose=False,
            auto_merge=False,
            interactive=False,
            resume=False,
            no_pull=False,
            parallel=None,
            force=False,
            no_notify=True,
            accept_unmeasured_spend=["carried:issue-2206"],
            accept_unmeasured_reason="quota failure",
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=fake_config),
            patch("theforge.cli.sprint.apply_base_branch_override", return_value=fake_config),
            patch("theforge.cli.sprint._find_config", return_value=config_path),
            patch("theforge.cli.sprint.parse_manifest_slugs", return_value=["feature-a"]),
            patch("theforge.cli.sprint._acquire_launch_locks", return_value=([], None, {})),
            patch("theforge.cli.sprint.release_story_locks"),
            patch("theforge.cli.sprint.run_sprint", return_value=fake_result) as mock_run_sprint,
            patch("theforge.sprint.runner.resolve_from_manifest", return_value=stub_resolved()),
        ):
            sprint_cli.cmd_sprint(args)

        run_context = mock_run_sprint.call_args.args[0]
        assert run_context.accept_unmeasured_spend == ["carried:issue-2206"]
        assert run_context.accept_unmeasured_reason == "quota failure"

    def test_daemon_submission_forwards_the_acceptance(self, tmp_path: Path) -> None:
        """--detach enumerates run_sprint kwargs by hand; the flag must be there."""
        from unittest.mock import MagicMock, patch

        from theforge.daemon import DaemonServer

        daemon = DaemonServer.__new__(DaemonServer)
        daemon.forge_root = tmp_path
        (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")

        with (
            patch("theforge.config.load_config", return_value=MagicMock()),
            patch("theforge.sprint.runner.parse_manifest_slugs", return_value=["feature-a"]),
            patch("theforge.sprint.lock.acquire_story_locks", return_value=([], [])),
            patch("theforge.sprint.lock.release_story_locks"),
            patch("theforge.sprint.run_sprint") as mock_run_sprint,
            patch("theforge.sprint.runner.resolve_from_manifest", return_value=stub_resolved()),
        ):
            daemon._execute_sprint(
                str(tmp_path / "sprint.yaml"),
                {
                    "accept_unmeasured_spend": ["issue-2206"],
                    "accept_unmeasured_reason": "quota failure",
                },
                lambda _state: None,
            )

        run_context = mock_run_sprint.call_args.args[0]
        assert run_context.accept_unmeasured_spend == ["issue-2206"]
        assert run_context.accept_unmeasured_reason == "quota failure"


class TestAcceptanceCoversOneOccurrence:
    """An acceptance stands in for one recorded call, not for the story.

    Keying it to the story would turn a one-time operator decision into a
    standing licence to spend unmeasured — the guard would never close on that
    story again, however many further calls went unpriced.
    """

    def test_a_fresh_unmeasured_call_is_not_absorbed_by_an_older_acceptance(self) -> None:
        from theforge.sprint.unmeasured import (
            accept,
            accepted_by_source,
            accepted_ceiling_total,
            build_source,
            partition,
        )

        index = accepted_by_source(
            [
                accept(
                    build_source("carried:feature-a", _bounded_story_audit()),
                    accepted_at="2026-08-08T00:00:00+00:00",
                )
            ]
        )
        # The carried occurrence is resolved; the one this run produced is not.
        unresolved, applied = partition(
            ["carried:feature-a", "feature-a"],
            index,
            current_generation={"feature-a"},
        )
        assert unresolved == ["feature-a"]
        # The accepted occurrence is still charged — both unknowns are accounted
        # for, one by a ceiling and one by refusing to certify the cap.
        assert accepted_ceiling_total(applied) == 4.5

    def test_the_accepted_occurrence_alone_still_resolves(self) -> None:
        from theforge.sprint.unmeasured import (
            accept,
            accepted_by_source,
            build_source,
            partition,
        )

        index = accepted_by_source(
            [
                accept(
                    build_source("carried:feature-a", _bounded_story_audit()),
                    accepted_at="2026-08-08T00:00:00+00:00",
                )
            ]
        )
        unresolved, applied = partition(
            ["carried:feature-a"], index, current_generation={"feature-b"}
        )
        assert unresolved == []
        assert [r.source for r in applied] == ["feature-a"]

    def test_accepted_story_that_goes_unmeasured_again_stops_the_next_story(
        self, tmp_path: Path
    ) -> None:
        """Seam: the guard closes again on the second unknown, unprompted."""
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.audit import persist_accumulated_story_state
        from theforge.sprint.dag import StoryTriage

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
        config = _make_config(tmp_path)

        sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
        sprint_dir.mkdir(parents=True, exist_ok=True)
        (sprint_dir / ".sprint_id").write_text("sprint-2310", encoding="utf-8")
        _write_story_audit(tmp_path, "Test Sprint", "feature-a", _bounded_story_audit())
        persist_accumulated_story_state(
            "sprint-2310",
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                }
            ],
        )

        def _triage(spec_path, *args, **kwargs):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        # feature-a runs again on the operator's acceptance — and goes unmeasured
        # a second time. That is new unknown spend, not the accepted one.
        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                return_value=TestBudgetEnforcementSeam._unmeasured_dev_result(),
            ) as mock_run:
                result = run_sprint_ctx(
                    config,
                    manifest_path,
                    accept_unmeasured_spend=["feature-a"],
                )

        # The accepted story ran; the independent next story did not.
        assert mock_run.call_count == 1
        assert result.stopped_reason is not None
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert "feature-a" in result.unresolved_unmeasured_spend_sources
        # The earlier occurrence stays resolved and charged — the refusal is
        # about the new call, not a retraction of the operator's decision.
        assert [r["source"] for r in result.accepted_unmeasured_spend] == ["feature-a"]
        assert result.budget_verification_spend_usd >= 4.5


class TestAcceptedPriorAuditSourceIsCharged:
    """An acceptance that clears the generation carry must still cost something.

    The prior generation's own record can be the only place a source appears —
    the accumulated story row is gone, pruned or never written. Clearing the
    whole-generation marker on an acceptance whose ceiling was then charged to
    nothing would open the guard for free, under a cap the accepted amount might
    not even fit inside.
    """

    @staticmethod
    def _arrange(tmp_path: Path, *, budget: float):
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.audit import persist_accumulated_story_state

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=budget)
        config = _make_config(tmp_path)

        sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
        sprint_dir.mkdir(parents=True, exist_ok=True)
        (sprint_dir / ".sprint_id").write_text("sprint-2310", encoding="utf-8")
        _write_story_audit(tmp_path, "Test Sprint", "feature-a", _bounded_story_audit())

        # The prior generation named the unmeasured source...
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True, exist_ok=True)
        (audits / "sprint-audit.yaml").write_text(
            yaml.safe_dump(
                {
                    "sprint": {
                        "sprint_id": "sprint-2310",
                        "total_cost_usd": None,
                        "total_cost_measured_usd": 0.0,
                        "cost_complete": False,
                        "unmeasured_spend_sources": ["feature-a"],
                    }
                }
            ),
            encoding="utf-8",
        )
        # ...but no accumulated story row carries it.
        persist_accumulated_story_state("sprint-2310", "Test Sprint", tmp_path, [])
        return config, manifest_path

    @staticmethod
    def _run(config, manifest_path, **kwargs):
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_coordinator_result

        from theforge.sprint.dag import StoryTriage

        def _triage(spec_path, *args, **kw):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                side_effect=lambda *a, **k: _make_coordinator_result(success=True, cost=1.0),
            ) as mock_run:
                result = run_sprint_ctx(config, manifest_path, resume=True, **kwargs)
        return result, mock_run

    def test_the_ceiling_is_charged_and_can_exhaust_the_cap(self, tmp_path: Path) -> None:
        config, manifest_path = self._arrange(tmp_path, budget=4.0)
        result, mock_run = self._run(config, manifest_path, accept_unmeasured_spend=["feature-a"])

        # $4.50 accepted against a $4.00 cap: this is not headroom.
        assert mock_run.call_count == 0
        assert result.stopped_reason is not None
        assert result.stopped_reason.startswith("Budget exhausted")
        assert "accepted unmeasured ceiling $4.50" in result.stopped_reason
        assert result.budget_verification_spend_usd >= 4.5
        assert [r["source"] for r in result.accepted_unmeasured_spend] == ["feature-a"]

    def test_the_source_stays_named_in_the_ledger(self, tmp_path: Path) -> None:
        """Convention 6: the spend must not vanish just because its row did."""
        config, manifest_path = self._arrange(tmp_path, budget=100.0)
        result, mock_run = self._run(config, manifest_path, accept_unmeasured_spend=["feature-a"])

        assert mock_run.call_count == 1
        assert result.stopped_reason is None
        assert "carried:feature-a" in result.unmeasured_spend_sources
        # Still unmeasured spend, so the total is still a lower bound.
        assert result.cost_complete is False
        assert result.budget_verification_spend_usd >= 4.5

        audit = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )["sprint"]
        assert "carried:feature-a" in audit["unmeasured_spend_sources"]
        assert audit["unresolved_unmeasured_spend_sources"] == []
        assert audit["accepted_unmeasured_spend"][0]["source"] == "feature-a"

    def test_without_the_acceptance_the_generation_carry_still_closes_the_guard(
        self, tmp_path: Path
    ) -> None:
        config, manifest_path = self._arrange(tmp_path, budget=100.0)
        result, mock_run = self._run(config, manifest_path)

        assert mock_run.call_count == 0
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert "carried:prior-generation" in result.unresolved_unmeasured_spend_sources
        assert "--accept-unmeasured-spend" not in result.stopped_reason


class TestAcceptancePersistenceIsReportedHonestly:
    """A resolution that did not reach disk must not be logged as recorded."""

    def test_the_writer_reports_whether_the_write_landed(self, tmp_path: Path) -> None:
        from theforge.sprint.audit import persist_accepted_unmeasured_spend

        assert (
            persist_accepted_unmeasured_spend("sprint-2310", "Test Sprint", tmp_path, []) is True
        )
        # A project root that cannot hold the state directory fails the write.
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("", encoding="utf-8")
        assert (
            persist_accepted_unmeasured_spend("sprint-2310", "Test Sprint", blocked, []) is False
        )
        # No sprint identity means there is nowhere to record it either.
        assert persist_accepted_unmeasured_spend(None, "Test Sprint", tmp_path, []) is False

    def test_a_failed_write_warns_that_the_acceptance_is_run_scoped(
        self, tmp_path: Path, capsys
    ) -> None:
        from unittest.mock import patch

        config, manifest_path = TestUnmeasuredResolutionSeam._arrange(tmp_path)
        with patch(
            "theforge.sprint.budget_runtime.persist_accepted_unmeasured_spend",
            return_value=False,
        ):
            result, mock_run = TestUnmeasuredResolutionSeam._run(
                config, manifest_path, accept_unmeasured_spend=["feature-a"]
            )

        # The acceptance still governs this run — nothing was lost yet.
        assert mock_run.call_count == 1
        assert result.accepted_unmeasured_spend
        err = capsys.readouterr().err
        assert "could not persist the unmeasured-spend acceptance" in err
        assert "THIS run only" in err


class TestAcceptablePriorSources:
    """The whole-generation marker is not a source anyone can accept."""

    def test_the_marker_is_never_offered_as_acceptable(self) -> None:
        from theforge.sprint.unmeasured import acceptable_prior_sources

        # The exact list run 6796605f9982 left behind.
        assert acceptable_prior_sources(["carried:issue-2206", "carried:prior-generation"]) == [
            "issue-2206"
        ]
        # A record naming only the marker leaves nothing to resolve.
        assert acceptable_prior_sources(["carried:prior-generation"]) == []
        # One source named twice is one source.
        assert acceptable_prior_sources(["issue-2206", "carried:issue-2206", ""]) == ["issue-2206"]

    def test_a_record_with_nothing_acceptable_never_reads_as_resolved(self) -> None:
        from theforge.sprint.unmeasured import all_sources_accepted

        assert (
            all_sources_accepted(["carried:prior-generation"], {"issue-2206": object()}) is False
        )
        assert all_sources_accepted([], {}) is False


class TestReportedShapeIsResolvable:
    """The exact record run 6796605f9982 left must not be an absorbing state.

    Its prior audit named both the carried story source and the derived
    whole-generation marker. The marker has no origin, no ceiling and no accept
    path, so re-surfacing it after the story source was accepted refuses the run
    on a condition no operator action can satisfy — the story never dispatches,
    on that resume or any later one.
    """

    @staticmethod
    def _arrange(tmp_path: Path, *, extra_specs: list[str] | None = None):
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.audit import persist_accumulated_story_state

        _make_spec_file(tmp_path, "Issue 2206", "issue-2206")
        specs = ["issue-2206.md", *(extra_specs or [])]
        manifest_path = _make_manifest(tmp_path, specs, budget=100.0)
        config = _make_config(tmp_path)

        sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
        sprint_dir.mkdir(parents=True, exist_ok=True)
        (sprint_dir / ".sprint_id").write_text("sprint-2310", encoding="utf-8")
        _write_story_audit(tmp_path, "Test Sprint", "issue-2206", _bounded_story_audit())

        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True, exist_ok=True)
        (audits / "sprint-audit.yaml").write_text(
            yaml.safe_dump(
                {
                    "sprint": {
                        "sprint_id": "sprint-2310",
                        "total_cost_usd": None,
                        "total_cost_measured_usd": 0.0,
                        "cost_complete": False,
                        "unmeasured_spend_sources": [
                            "carried:issue-2206",
                            "carried:prior-generation",
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        persist_accumulated_story_state(
            "sprint-2310",
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "issue-2206.md",
                    "slug": "issue-2206",
                    "path": "issue-2206.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                }
            ],
        )
        return config, manifest_path

    @staticmethod
    def _run(config, manifest_path, **kwargs):
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_coordinator_result

        from theforge.sprint.dag import StoryTriage

        def _triage(spec_path, *args, **kw):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                side_effect=lambda *a, **k: _make_coordinator_result(success=True, cost=1.0),
            ) as mock_run:
                result = run_sprint_ctx(config, manifest_path, resume=True, **kwargs)
        return result, mock_run

    def test_resume_without_acceptance_is_still_refused(self, tmp_path: Path) -> None:
        config, manifest_path = self._arrange(tmp_path)
        result, mock_run = self._run(config, manifest_path)

        assert mock_run.call_count == 0
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert "carried:prior-generation" in result.unresolved_unmeasured_spend_sources

    def test_accepting_the_story_source_clears_the_derived_marker(self, tmp_path: Path) -> None:
        config, manifest_path = self._arrange(tmp_path)
        result, mock_run = self._run(config, manifest_path, accept_unmeasured_spend=["issue-2206"])

        assert mock_run.call_count == 1
        assert result.stopped_reason is None
        # The marker is gone entirely — not re-surfaced under any spelling.
        assert result.unresolved_unmeasured_spend_sources == ()
        assert not any("prior-generation" in s for s in result.unmeasured_spend_sources), (
            result.unmeasured_spend_sources
        )
        # The accepted source is still charged and still reported as unmeasured.
        assert [r["source"] for r in result.accepted_unmeasured_spend] == ["issue-2206"]
        assert result.budget_verification_spend_usd >= 4.5
        assert result.cost_complete is False

    def test_a_later_resume_reads_the_recorded_resolution(self, tmp_path: Path) -> None:
        """The story must not become unrunnable again on the next resume."""
        config, manifest_path = self._arrange(tmp_path)
        self._run(config, manifest_path, accept_unmeasured_spend=["issue-2206"])

        # Re-arm the reported shape exactly as a stopped generation would, and
        # resume with no flag: the recorded resolution still stands.
        self._arrange(tmp_path)
        result, mock_run = self._run(config, manifest_path)

        assert mock_run.call_count == 1
        assert result.stopped_reason is None
        assert [r["source"] for r in result.accepted_unmeasured_spend] == ["issue-2206"]


class TestOccurrenceIdentitySurvivesAResume:
    """A stale acceptance must not clear an occurrence recorded after it.

    Within one process the two occurrences are told apart by when they were
    recorded. Once the run is resumed that distinction is gone — both are simply
    carried — so identity has to come from the record: which run the unmeasured
    call actually happened in.
    """

    @staticmethod
    def _arrange(tmp_path: Path, *, audit_run_id: str, accepted_run_id: str):
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.audit import (
            persist_accepted_unmeasured_spend,
            persist_accumulated_story_state,
        )

        _make_spec_file(tmp_path, "Issue 2206", "issue-2206")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["issue-2206.md", "feature-b.md"], budget=100.0)
        config = _make_config(tmp_path)

        sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
        sprint_dir.mkdir(parents=True, exist_ok=True)
        (sprint_dir / ".sprint_id").write_text("sprint-2310", encoding="utf-8")
        # The per-story audit records the occurrence CURRENTLY carried.
        _write_story_audit(
            tmp_path, "Test Sprint", "issue-2206", _bounded_story_audit(run_id=audit_run_id)
        )
        persist_accumulated_story_state(
            "sprint-2310",
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "issue-2206.md",
                    "slug": "issue-2206",
                    "path": "issue-2206.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                }
            ],
        )
        # The acceptance on record was made for the run named here.
        persist_accepted_unmeasured_spend(
            "sprint-2310",
            "Test Sprint",
            tmp_path,
            [
                {
                    "source": "issue-2206",
                    "accepted_ceiling_usd": 4.5,
                    "measured_lower_bound_usd": _MEASURED_BEFORE_FAILURE,
                    "ceiling_basis": "story_allocation",
                    "origin_run_id": accepted_run_id,
                    "accepted_at": "2026-08-08T00:00:00+00:00",
                    "reason": "reviewer hit a provider quota",
                }
            ],
        )
        return config, manifest_path

    @staticmethod
    def _run(config, manifest_path):
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_coordinator_result

        from theforge.sprint.dag import StoryTriage

        def _triage(spec_path, *args, **kw):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                side_effect=lambda *a, **k: _make_coordinator_result(success=True, cost=1.0),
            ) as mock_run:
                result = run_sprint_ctx(config, manifest_path, resume=True)
        return result, mock_run

    def test_a_second_occurrence_is_not_cleared_by_the_first_acceptance(
        self, tmp_path: Path
    ) -> None:
        # Accepted for run-1; the ledger now carries the run-2 occurrence.
        config, manifest_path = self._arrange(
            tmp_path, audit_run_id="run-2", accepted_run_id="run-1"
        )
        result, mock_run = self._run(config, manifest_path)

        assert mock_run.call_count == 0
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert "carried:issue-2206" in result.unresolved_unmeasured_spend_sources
        # Nothing is charged either — an acceptance of some earlier call is not
        # a ceiling on this one.
        assert result.accepted_unmeasured_spend == ()

    def test_the_occurrence_that_was_accepted_still_resolves(self, tmp_path: Path) -> None:
        config, manifest_path = self._arrange(
            tmp_path, audit_run_id="run-1", accepted_run_id="run-1"
        )
        result, mock_run = self._run(config, manifest_path)

        assert mock_run.call_count == 2
        assert result.stopped_reason is None
        assert [r["source"] for r in result.accepted_unmeasured_spend] == ["issue-2206"]

    def test_an_acceptance_with_no_recorded_origin_still_applies(self, tmp_path: Path) -> None:
        """Unknown identity is not evidence of a mismatch."""
        config, manifest_path = self._arrange(tmp_path, audit_run_id="run-2", accepted_run_id="")
        result, mock_run = self._run(config, manifest_path)

        assert mock_run.call_count == 2
        assert result.stopped_reason is None


class TestSecondOccurrenceIsReportedAsItself:
    """The refusal must describe the unknown it is refusing on.

    A story has one per-story audit path, and running it again overwrites that
    file. Describing sources by story alone lets the carried reading win, so the
    refusal on a NEW unmeasured call would name the run id and ceiling of the
    call the operator had already accepted — sending them to look at settled
    work, and asking them about the wrong amount.
    """

    @staticmethod
    def _second_occurrence_audit() -> dict:
        """What the coordinator records when the story goes unmeasured again."""
        audit = _bounded_story_audit(run_id="run-2")
        audit["cost"]["agents"][0]["cost_usd"] = 1.0
        audit["cost"]["story_allocation"]["allocation_usd"] = 9.0
        return audit

    def test_the_refusal_names_the_new_occurrence_not_the_accepted_one(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        from sprint_test_helpers import run_sprint_ctx
        from test_sprint_resume import _make_config, _make_manifest, _make_spec_file

        from theforge.sprint.audit import persist_accumulated_story_state
        from theforge.sprint.dag import StoryTriage

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
        config = _make_config(tmp_path)

        sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
        sprint_dir.mkdir(parents=True, exist_ok=True)
        (sprint_dir / ".sprint_id").write_text("sprint-2310", encoding="utf-8")
        # The occurrence the operator accepts: run-1, $4.50 still unknown.
        _write_story_audit(
            tmp_path, "Test Sprint", "feature-a", _bounded_story_audit(run_id="run-1")
        )
        persist_accumulated_story_state(
            "sprint-2310",
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                }
            ],
        )

        def _triage(spec_path, *args, **kwargs):
            return StoryTriage(
                story_path=str(spec_path),
                action="full",
                reason="no worktree found",
                worktree_path=None,
            )

        def _rewrite_audit(*_args, **_kwargs):
            """Stand in for the coordinator's audit write after the story ran."""
            _write_story_audit(
                tmp_path, "Test Sprint", "feature-a", self._second_occurrence_audit()
            )

        with patch("theforge.sprint.runner._triage_spec", side_effect=_triage):
            with patch("theforge.sprint.runner._write_story_audit", side_effect=_rewrite_audit):
                with patch(
                    "theforge.sprint.runner.run_task",
                    return_value=TestBudgetEnforcementSeam._unmeasured_dev_result(),
                ) as mock_run:
                    result = run_sprint_ctx(
                        config,
                        manifest_path,
                        accept_unmeasured_spend=["feature-a"],
                    )

        assert mock_run.call_count == 1
        assert result.stopped_reason.startswith("Budget unverifiable")
        # The unresolved unknown is the second occurrence: run-2, and the $8.00
        # its own allocation leaves unaccounted.
        assert "run_id=run-2" in result.stopped_reason
        assert "at most $8.00 more" in result.stopped_reason
        # ...not the occurrence that was already accepted and already charged.
        assert "run_id=run-1" not in result.stopped_reason
        assert "at most $4.50 more" not in result.stopped_reason
        # The acceptance itself still describes the occurrence it was made for.
        [accepted] = result.accepted_unmeasured_spend
        assert accepted["origin_run_id"] == "run-1"
        assert accepted["accepted_ceiling_usd"] == 4.5
