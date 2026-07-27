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
