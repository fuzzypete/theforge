"""Seam coverage for the band-derived per-story allocation (#2169).

Unit coverage of the allocator lives in ``test_story_budget_allocation.py``.
What is proved here is the cross-phase flow: preflight derives the allocation
from the complexity score and installs it on state before any phase spends;
the review phase refuses to seat a smaller panel than it planned when the
allocation cannot fund one; the audit and sprint reporters carry the story's
spend against BOTH its allocation and the sprint ceiling.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config import load_config
from theforge.coordinator import audit_substrate as sub
from theforge.coordinator import story_budget as sb
from theforge.coordinator.preflight import _apply_preflight_config
from theforge.coordinator.state import CoordinatorState, Phase

_auth_ok = patch(
    "theforge.config._loaders.check_agent_auth",
    return_value=(True, ""),
)

# Eight admissible score-2 runs: median $0.46, max $1.35 → allocation $1.69.
_SCORE_2_COSTS = [0.21, 0.30, 0.38, 0.46, 0.52, 0.61, 1.08, 1.35]
# Eight admissible score-9 runs: max $40.64 → allocation $50.80, comfortably
# above the three-reviewer panel's planned $3.00.
_SCORE_9_COSTS = [1.9, 3.4, 6.1, 7.5, 9.2, 14.0, 19.7, 40.64]


def _seed_band(project_root: Path, score: int, costs: list[float]) -> None:
    runs = sub.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    records = []
    for index, cost in enumerate(costs):
        rec = {
            "run_id": f"seed-{score}-{index}",
            "task": {"slug": f"seed-{score}-{index}", "name": "seed"},
            "outcome": {"success": True, "final_phase": "DONE"},
            "timing": {"started_at": "2026-03-01T10:00:00+00:00", "duration_seconds": 60.0},
            "cost": {"total_usd": cost},
            "totals": {"cost_usd": cost, "duration_s": 60.0},
            "preflight": {"complexity": "small", "complexity_score": score},
            "reviews": [],
        }
        (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
        records.append(rec)
    conn = sub.create_or_open(project_root)
    try:
        for rec in records:
            sub.upsert_run_record(conn, rec, provenance="native")
        conn.commit()
    finally:
        conn.close()


def _seed_review_cycle_history(project_root: Path, cycle_costs: list[float]) -> None:
    runs = sub.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    records = []
    for index, cycle_cost in enumerate(cycle_costs):
        rec = {
            "run_id": f"review-cycle-{index}",
            "task": {"slug": f"review-cycle-{index}", "name": "seed"},
            "outcome": {"success": True, "final_phase": "DONE"},
            "timing": {"started_at": "2026-03-01T10:00:00+00:00", "duration_seconds": 60.0},
            "cost": {"total_usd": cycle_cost},
            "totals": {"cost_usd": cycle_cost, "duration_s": 60.0},
            "preflight": {"complexity": "medium", "complexity_score": 5},
            "iterations": {
                "review_cycles_total": 1,
                "review_loop": [{"iteration": 1, "cost_usd": cycle_cost}],
            },
            "reviews": [{"cycle": 1, "verdict": "APPROVE"}],
        }
        (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
        records.append(rec)
    conn = sub.create_or_open(project_root)
    try:
        for rec in records:
            sub.upsert_run_record(conn, rec, provenance="native")
        conn.commit()
    finally:
        conn.close()


def _config(tmp_path: Path, budget_usd: float = 50.0):
    cfg_path = tmp_path / "forge.yaml"
    cfg_path.write_text(
        f"""
models:
  - anthropic/sonnet/cli
  - openai/gpt-5.4-pro/cli
budget_usd: {budget_usd}
""",
        encoding="utf-8",
    )
    with _auth_ok:
        return load_config(cfg_path)


def _runtime_total(config) -> float:
    total = (
        config.dev_profile.budget_usd
        + config.preflight_profile.budget_usd
        + config.plan.budget_usd
        + sum(p.budget_usd for p in config.review_pool)
    )
    if config.synthesis_profile is not None:
        total += config.synthesis_profile.budget_usd
    return total


class TestPreflightInstallsTheAllocation:
    def test_score_band_governs_the_story_instead_of_the_configured_constant(
        self, tmp_path: Path
    ) -> None:
        _seed_band(tmp_path, 2, _SCORE_2_COSTS)
        config = _config(tmp_path)
        assert _runtime_total(config) == pytest.approx(50.0, abs=0.5)

        state = CoordinatorState()
        state.preflight_complexity = "small"
        state.preflight_complexity_score = 2

        updated = _apply_preflight_config(config, state)

        assert state.story_allocation is not None
        assert state.story_allocation["basis"] == sb.BASIS_SUBSTRATE_BAND
        assert state.story_allocation["allocation_usd"] == 1.69
        assert state.story_allocation["complexity_score"] == 2
        assert state.story_allocation["fallback_configured_usd"] == 50.0
        # The shares the phases will actually run under were rescaled to the
        # derived allocation — the governance is real, not just recorded.
        assert _runtime_total(updated) == pytest.approx(1.69, abs=0.05)

    def test_allocation_rides_in_the_routing_audit_so_resume_carries_it(
        self, tmp_path: Path
    ) -> None:
        _seed_band(tmp_path, 2, _SCORE_2_COSTS)
        config = _config(tmp_path)
        state = CoordinatorState()
        state.preflight_complexity = "small"
        state.preflight_complexity_score = 2

        _apply_preflight_config(config, state)

        assert state.complexity_routing_audit is not None
        assert state.complexity_routing_audit["story_allocation"] == state.story_allocation

    def test_band_with_no_history_falls_back_and_records_the_basis(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        state = CoordinatorState()
        state.preflight_complexity = "medium"
        state.preflight_complexity_score = 5

        updated = _apply_preflight_config(config, state)

        assert state.story_allocation["basis"] == sb.BASIS_CONFIGURED_FALLBACK
        assert state.story_allocation["allocation_usd"] == 50.0
        assert "no audit substrate" in state.story_allocation["reason"]
        # Fallback governs exactly as the configured value did before.
        assert _runtime_total(updated) == pytest.approx(50.0, abs=0.5)

    def test_high_band_allocation_can_exceed_the_configured_constant(self, tmp_path: Path) -> None:
        """A score-9 story is not squeezed by a ceiling sized for small work."""
        _seed_band(tmp_path, 9, _SCORE_9_COSTS)
        config = _config(tmp_path, budget_usd=20.0)
        state = CoordinatorState()
        state.preflight_complexity = "large"
        state.preflight_complexity_score = 9

        _apply_preflight_config(config, state)

        assert state.story_allocation["allocation_usd"] == round(40.64 * 1.25, 2)
        assert state.story_allocation["allocation_usd"] > 20.0


class TestAllocationExhaustionIsReported:
    """The run says the phase cannot be funded — it does not shrink the panel."""

    def _pool_config(self, tmp_path: Path):
        from tests.test_coord_routing_recovery import _make_config, _profile

        return dataclasses.replace(
            _make_config(tmp_path),
            review_pool=[_profile("a"), _profile("b"), _profile("c")],
            synthesis_profile=None,
        )

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_review_refuses_to_seat_a_smaller_panel_than_it_planned(
        self, mock_pool, _mock_log, tmp_path: Path
    ) -> None:
        from theforge.coordinator.review_pool import _run_review_pool
        from theforge.coordinator.state import ReviewCycleMetadata
        from theforge.task import TaskStory

        config = self._pool_config(tmp_path)
        task = TaskStory(name="Story", story_path=tmp_path / "spec.md", slug="issue-2169")
        task.story_path.write_text("# Story\n\nBody.\n", encoding="utf-8")
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)

        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")
        state.story_allocation = sb.allocation_from_samples(
            2, _SCORE_2_COSTS, configured_usd=50.0
        ).as_dict()
        # Dev already spent nearly the whole $1.69 allocation, so the three
        # planned reviewers cannot be funded.
        state.dev_results = [
            dataclasses.replace(_agent_result(), cost_usd=1.60),
        ]

        meta = ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)
        _successful, _failed, merged, _individual, _named = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=True,
        )

        # The panel never ran: an under-funded phase is reported, not degraded.
        assert mock_pool.called is False
        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert state.error_type == "allocation_exhausted"
        assert state.allocation_exhausted is not None
        assert state.allocation_exhausted["participants"] == ["a", "b", "c"]
        # The message names the story, its allocation, its expected range and
        # what it observed — distinguishable from an unrelated failure.
        assert "issue-2169" in state.error
        assert "$1.69" in state.error
        assert "median $0.46" in state.error
        assert "a, b, c" in state.error

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_funded_phase_runs_the_whole_planned_panel(
        self, mock_pool, _mock_log, tmp_path: Path
    ) -> None:
        from tests.test_coord_routing_recovery import APPROVE_YAML, _make_agent_result
        from theforge.coordinator.review_pool import _run_review_pool
        from theforge.coordinator.state import ReviewCycleMetadata
        from theforge.task import TaskStory

        config = self._pool_config(tmp_path)
        task = TaskStory(name="Story", story_path=tmp_path / "spec.md", slug="issue-2169")
        task.story_path.write_text("# Story\n\nBody.\n", encoding="utf-8")
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name=name, cost_usd=0.01)
            for name in ("a", "b", "c")
        ]

        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")
        state.story_allocation = sb.allocation_from_samples(
            9, _SCORE_9_COSTS, configured_usd=50.0
        ).as_dict()

        meta = ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=True,
        )

        assert state.allocation_exhausted is None
        assert sorted(meta.successful) == ["a", "b", "c"]


def _agent_result():
    from theforge.runners import AgentResult

    return AgentResult(
        success=True,
        output="done",
        session_id="s",
        cost_usd=0.0,
        exit_code=0,
        raw={},
        profile_name="dev",
    )


class TestSprintReportsBothGovernors:
    def _state(self, *, allocation: dict, exhausted: dict | None, cost: float):
        state = CoordinatorState()
        state.story_allocation = allocation
        state.allocation_exhausted = exhausted
        state.dev_results = [dataclasses.replace(_agent_result(), cost_usd=cost)]
        return state

    def test_shortfall_while_sprint_headroom_remains_is_visible_as_such(self) -> None:
        from theforge.sprint.audit import _story_allocation_summary
        from theforge.sprint.manifest import SprintResult

        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0).as_dict()
        shortfall = sb.phase_funding_shortfall(
            allocation,
            1.60,
            phase="review",
            participants=["a", "b", "c"],
            planned_usd=0.60,
        )
        state = self._state(allocation=allocation, exhausted=shortfall, cost=1.60)
        result = SprintResult(
            name="sprint",
            specs_total=1,
            specs_succeeded=0,
            specs_failed=1,
            specs_skipped=0,
            total_cost_usd=46.0,
            budget_usd=120.0,
        )

        block = _story_allocation_summary(state, result, 1.60)

        assert block["status"] == "allocation_exhausted"
        assert block["allocation_usd"] == 1.69
        assert block["observed_usd"] == 1.60
        assert block["sprint_budget_usd"] == 120.0
        assert block["sprint_remaining_usd"] == 74.0
        # The whole point: the story ran out while the sprint had not.
        assert block["sprint_headroom_remained"] is True

    def test_story_within_its_band_reports_both_numbers_without_a_condition(self) -> None:
        from theforge.sprint.audit import _story_allocation_summary
        from theforge.sprint.manifest import SprintResult

        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0).as_dict()
        state = self._state(allocation=allocation, exhausted=None, cost=0.50)
        result = SprintResult(
            name="sprint",
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.50,
            budget_usd=120.0,
        )

        block = _story_allocation_summary(state, result, 0.50)

        assert block["status"] == sb.STATUS_WITHIN
        assert block["sprint_remaining_usd"] == 119.5

    def test_unmeasured_sprint_cost_cannot_certify_headroom(self) -> None:
        from theforge.sprint.audit import _story_allocation_summary
        from theforge.sprint.manifest import SprintResult

        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0).as_dict()
        state = self._state(allocation=allocation, exhausted=None, cost=0.50)
        result = SprintResult(
            name="sprint",
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.50,
            budget_usd=120.0,
            cost_complete=False,
        )

        block = _story_allocation_summary(state, result, 0.50)

        assert block["sprint_cost_measured"] is False
        assert block["sprint_headroom_remained"] is None


class TestStatusRowSurfacesTheCondition:
    def test_exceeded_allocation_annotates_the_row_with_the_band(self) -> None:
        from theforge.sprint.status_reader import _allocation_detail

        detail = _allocation_detail(
            {
                "status": "allocation_exceeded",
                "observed_usd": 4.0,
                "allocation_usd": 1.69,
                "median_usd": 0.46,
                "p90_usd": 1.08,
                "complexity_score": 2,
                "sprint_remaining_usd": 74.0,
            }
        )

        assert "allocation over: $4.00 of $1.69" in detail
        assert "score 2 band median $0.46 / p90 $1.08" in detail
        assert "sprint remaining $74.00" in detail

    def test_story_inside_its_band_gets_no_annotation(self) -> None:
        from theforge.sprint.status_reader import _allocation_detail

        assert _allocation_detail({"status": sb.STATUS_WITHIN, "observed_usd": 0.5}) == ""


class TestAuditRecordCarriesTheAllocation:
    def test_cost_block_reports_allocation_and_observed_together(self, tmp_path: Path) -> None:
        from theforge.coordinator import story_budget as _sb

        allocation = _sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0).as_dict()
        block = _sb.evaluate_allocation_dict(allocation, 4.0)

        assert block["status"] == _sb.STATUS_EXCEEDED
        assert block["allocation_usd"] == 1.69
        assert block["observed_usd"] == 4.0
        assert block["basis"] == _sb.BASIS_SUBSTRATE_BAND


class TestResumeCarriesTheAllocation:
    def test_restored_routing_audit_reinstates_the_allocation(self) -> None:
        from theforge.coordinator.resume_persistence import apply_resume_record_to_state

        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0).as_dict()
        state = CoordinatorState()
        record = {"complexity_routing_audit": {"story_allocation": allocation}}

        apply_resume_record_to_state(state, record)

        assert state.story_allocation == allocation

    def test_resumed_preflight_reuses_the_restored_allocation(self, tmp_path: Path) -> None:
        """The restored allocation survives the resumed run's preflight re-run.

        ``--resume`` re-enters PREFLIGHT, which re-invokes
        ``_apply_preflight_config``. Re-deriving there would judge the spend
        already on this story's clock against a distribution that has moved
        since the first attempt. Seeded here with a band whose current
        distribution would derive a DIFFERENT number, so a re-derivation is
        visible rather than coincidentally equal.
        """
        _seed_band(tmp_path, 2, _SCORE_9_COSTS)  # score-2 band now looks expensive
        config = _config(tmp_path)

        state = CoordinatorState()
        state.preflight_complexity = "small"
        state.preflight_complexity_score = 2
        restored = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0).as_dict()
        state.story_allocation = dict(restored)

        updated = _apply_preflight_config(config, state)

        assert state.story_allocation["allocation_usd"] == 1.69
        assert state.story_allocation["carried"] is True
        # The carried number governs the shares this attempt actually runs under.
        assert _runtime_total(updated) == pytest.approx(1.69, abs=0.05)

    def test_a_different_band_on_resume_is_re_derived_not_inherited(self, tmp_path: Path) -> None:
        """An allocation is only carried for the band it was derived for."""
        _seed_band(tmp_path, 9, _SCORE_9_COSTS)
        config = _config(tmp_path)

        state = CoordinatorState()
        state.preflight_complexity = "large"
        state.preflight_complexity_score = 9
        # Restored from an attempt that scored the story a 2.
        state.story_allocation = sb.allocation_from_samples(
            2, _SCORE_2_COSTS, configured_usd=50.0
        ).as_dict()

        _apply_preflight_config(config, state)

        assert state.story_allocation["complexity_score"] == 9
        assert state.story_allocation["allocation_usd"] == round(40.64 * 1.25, 2)
        assert "carried" not in state.story_allocation

    def test_re_entry_within_one_run_does_not_redraw_the_allocation(self, tmp_path: Path) -> None:
        """Routing recovery re-enters _apply_preflight_config on the same state.

        The second pass must not re-read the substrate (whose fallback would
        now be the already-rescaled profile sum, not the configured budget).
        """
        config = _config(tmp_path)  # no substrate → configured fallback of $50
        state = CoordinatorState()
        state.preflight_complexity = "medium"
        state.preflight_complexity_score = 5

        once = _apply_preflight_config(config, state)
        first = dict(state.story_allocation)
        twice = _apply_preflight_config(once, state)

        assert state.story_allocation["allocation_usd"] == first["allocation_usd"]
        assert state.story_allocation["fallback_configured_usd"] == 50.0
        assert _runtime_total(twice) == pytest.approx(_runtime_total(once), abs=0.05)


class TestSeatingReconcilesPermissionsWithTheAllocation:
    """A story is never seated with review cycles it cannot pay for (#2238)."""

    def _adaptive_config(
        self,
        tmp_path: Path,
        *,
        dev_budget_usd: float,
        reviewer_budget_usd: float,
        reviewers: int = 3,
    ):
        from coord_test_helpers import _make_config, _make_review_profile

        from theforge.config.types import AssignmentConfig, RetryPolicy

        config = _make_config(tmp_path)
        return dataclasses.replace(
            config,
            dev_profile=dataclasses.replace(config.dev_profile, budget_usd=dev_budget_usd),
            review_pool=[
                _make_review_profile(name, budget_usd=reviewer_budget_usd)
                for name in ("a", "b", "c")[:reviewers]
            ],
            synthesis_profile=None,
            retry=RetryPolicy(
                max_dev_iterations=3,
                max_review_cycles=5,
                max_dev_iterations_cap=6,
                max_review_cycles_cap=5,
                adaptive_iterations=True,
            ),
            assignment=AssignmentConfig(enabled=True, adaptive_enabled=True),
        )

    def _seated_state(self, tmp_path: Path, allocation_usd: float):
        state = CoordinatorState()
        state.preflight_complexity = "large"
        state.preflight_complexity_score = 9
        state.workspace_path = tmp_path
        state.branch_name = "feat/test"
        state.story_allocation = {
            "allocation_usd": allocation_usd,
            "basis": sb.BASIS_SUBSTRATE_BAND,
            "complexity_score": 9,
            "median_usd": 7.53,
            "p90_usd": 19.74,
            "max_usd": round(allocation_usd / 1.25, 2),
            "sample_count": 12,
        }
        return state

    def test_review_max_is_reduced_to_what_the_allocation_funds_before_dev(
        self, tmp_path: Path
    ) -> None:
        from coord_test_helpers import _make_task

        from theforge.coordinator.engine import _coordinator_loop

        # The seating numbers from run 88a7e2cc81eb: $48.02 allocation, a
        # $25.0428 dev estimate, a $17.55-per-cycle panel.
        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = _make_task(tmp_path)
        state = self._seated_state(tmp_path, 48.02)

        class _StopAtDev(Exception):
            pass

        seen: dict = {}

        def _fake_dev(*_args, **_kwargs):
            seen["review_max_at_dev"] = state.adaptive_review_max
            raise _StopAtDev()

        with patch("theforge.coordinator.engine._run_dev_phase", _fake_dev):
            with pytest.raises(_StopAtDev):
                _coordinator_loop(state, config, task, "story", task_start=0.0)

        record = state.adaptive_limits_audit["review_cycle_reconciliation"]
        assert record["action"] == sb.RECONCILE_REDUCED
        assert record["requested_review_max"] > 1
        assert record["reconciled_review_max"] == 1
        assert record["review_cycle_cost_usd"] == 17.55
        assert record["review_cycle_cost_basis"] == sb.BASIS_REVIEW_CEILING_FALLBACK
        assert record["allocation_usd"] == 48.02
        # The reduction is in force before dev spends a cent, not afterwards.
        assert seen["review_max_at_dev"] == 1
        assert state.adaptive_review_max == 1
        assert state.adaptive_review_cycle_planning["planned_cost_usd"] == 17.55
        assert state.adaptive_review_cycle_planning["basis"] == sb.BASIS_REVIEW_CEILING_FALLBACK
        assert "review_max reduced" in state.adaptive_limits_audit["rationale"]

    def test_an_allocation_that_cannot_fund_one_cycle_escalates_before_dev(
        self, tmp_path: Path
    ) -> None:
        from coord_test_helpers import _make_task

        from theforge.coordinator.engine import _coordinator_loop

        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = _make_task(tmp_path)
        state = self._seated_state(tmp_path, 30.0)

        with patch("theforge.coordinator.engine._run_dev_phase") as mock_dev:
            result = _coordinator_loop(state, config, task, "story", task_start=0.0)

        # No dev spend on work that could never have been reviewed.
        assert mock_dev.called is False
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert state.phase == Phase.ESCALATE
        assert state.error_type == "allocation_exhausted"
        assert state.allocation_exhausted is not None
        assert state.allocation_exhausted["participants"] == ["a", "b", "c"]
        assert state.allocation_exhausted["planned_usd"] == 17.55
        assert "test-task" in state.error
        assert "Decided at seating" in state.error
        assert (
            state.adaptive_limits_audit["review_cycle_reconciliation"]["action"]
            == sb.RECONCILE_UNFUNDABLE
        )

    def test_seating_uses_observed_review_cycle_price_plus_headroom(self, tmp_path: Path) -> None:
        from coord_test_helpers import _make_task

        from theforge.coordinator.engine import _coordinator_loop

        _seed_review_cycle_history(tmp_path, [3.10, 3.64, 4.20])
        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = _make_task(tmp_path)
        state = self._seated_state(tmp_path, 48.02)

        class _StopAtDev(Exception):
            pass

        with patch("theforge.coordinator.engine._run_dev_phase", side_effect=_StopAtDev()):
            with pytest.raises(_StopAtDev):
                _coordinator_loop(state, config, task, "story", task_start=0.0)

        planning = state.adaptive_review_cycle_planning
        record = state.adaptive_limits_audit["review_cycle_reconciliation"]
        assert planning is not None
        assert planning["basis"] == sb.BASIS_OBSERVED_REVIEW_CYCLE
        assert planning["planned_cost_usd"] == round(3.64 * 1.25, 4)
        assert record["review_cycle_cost_usd"] == round(3.64 * 1.25, 4)
        assert record["review_cycle_cost_basis"] == sb.BASIS_OBSERVED_REVIEW_CYCLE
        assert record["review_cycle_cost_sample_count"] == 3
        assert state.adaptive_review_max == record["requested_review_max"]
        assert state.adaptive_limits_audit["review_cycle_planning"]["reason"] == (
            "derived from median observed review-cycle spend $3.64 x 1.25 headroom over 3 cycle(s)"
        )

    def test_a_sufficient_allocation_leaves_the_permitted_cycles_intact(
        self, tmp_path: Path
    ) -> None:
        from coord_test_helpers import _make_task

        from theforge.coordinator.engine import _coordinator_loop

        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = _make_task(tmp_path)
        state = self._seated_state(tmp_path, 500.0)

        class _StopAtDev(Exception):
            pass

        with patch("theforge.coordinator.engine._run_dev_phase", side_effect=_StopAtDev()):
            with pytest.raises(_StopAtDev):
                _coordinator_loop(state, config, task, "story", task_start=0.0)

        record = state.adaptive_limits_audit["review_cycle_reconciliation"]
        assert record["action"] == sb.RECONCILE_AFFORDABLE
        assert state.adaptive_review_max == record["requested_review_max"]
        assert state.allocation_exhausted is None

    def test_a_story_without_an_allocation_is_left_untouched(self, tmp_path: Path) -> None:
        from coord_test_helpers import _make_task

        from theforge.coordinator.engine import _coordinator_loop

        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = _make_task(tmp_path)
        state = self._seated_state(tmp_path, 1.0)
        state.story_allocation = None

        class _StopAtDev(Exception):
            pass

        with patch("theforge.coordinator.engine._run_dev_phase", side_effect=_StopAtDev()):
            with pytest.raises(_StopAtDev):
                _coordinator_loop(state, config, task, "story", task_start=0.0)

        record = state.adaptive_limits_audit["review_cycle_reconciliation"]
        assert record["action"] == sb.RECONCILE_NO_ALLOCATION
        assert state.adaptive_review_max == record["requested_review_max"]

    def test_a_configured_fallback_allocation_does_not_clamp_review_cycles(
        self, tmp_path: Path
    ) -> None:
        """No band history means no evidence — the dispatch guard still covers it."""
        from coord_test_helpers import _make_task

        from theforge.coordinator.engine import _coordinator_loop

        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = _make_task(tmp_path)
        state = self._seated_state(tmp_path, 48.02)
        state.story_allocation = {
            **state.story_allocation,
            "basis": sb.BASIS_CONFIGURED_FALLBACK,
        }

        class _StopAtDev(Exception):
            pass

        with patch("theforge.coordinator.engine._run_dev_phase", side_effect=_StopAtDev()):
            with pytest.raises(_StopAtDev):
                _coordinator_loop(state, config, task, "story", task_start=0.0)

        record = state.adaptive_limits_audit["review_cycle_reconciliation"]
        assert record["action"] == sb.RECONCILE_NO_BAND_HISTORY
        assert state.adaptive_review_max == record["requested_review_max"]
        assert state.allocation_exhausted is None

    def test_the_reconciliation_reaches_the_run_audit(self, tmp_path: Path) -> None:
        """The operator can see which of the two governors moved, and why."""
        from coord_test_helpers import _make_task

        from theforge.coordinator.audit import generate_audit_log
        from theforge.coordinator.engine import _coordinator_loop
        from theforge.coordinator.state import CoordinatorResult

        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = _make_task(tmp_path)
        state = self._seated_state(tmp_path, 48.02)

        class _StopAtDev(Exception):
            pass

        with patch("theforge.coordinator.engine._run_dev_phase", side_effect=_StopAtDev()):
            with pytest.raises(_StopAtDev):
                _coordinator_loop(state, config, task, "story", task_start=0.0)

        audit = generate_audit_log(
            config,
            task,
            CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message="x"),
        )
        block = audit["iterations"]["adaptive_limits"]["review_cycle_reconciliation"]
        assert block["action"] == sb.RECONCILE_REDUCED
        assert block["reconciled_review_max"] == 1
        assert (
            audit["iterations"]["adaptive_limits"]["review_cycle_planning"]["basis"]
            == sb.BASIS_REVIEW_CEILING_FALLBACK
        )

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_dispatch_uses_the_seated_review_cycle_price_without_repricing(
        self, mock_pool, _mock_log, tmp_path: Path
    ) -> None:
        from tests.test_coord_routing_recovery import APPROVE_YAML, _make_agent_result
        from theforge.coordinator.review_pool import _run_review_pool
        from theforge.coordinator.state import ReviewCycleMetadata
        from theforge.task import TaskStory

        config = self._adaptive_config(tmp_path, dev_budget_usd=25.0428, reviewer_budget_usd=5.85)
        task = TaskStory(name="Story", story_path=tmp_path / "spec.md", slug="issue-2260")
        task.story_path.write_text("# Story\n\nBody.\n", encoding="utf-8")
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name=name, cost_usd=0.01)
            for name in ("a", "b", "c")
        ]

        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")
        state.story_allocation = {
            "allocation_usd": 5.0,
            "basis": sb.BASIS_SUBSTRATE_BAND,
            "complexity_score": 5,
            "median_usd": 3.0,
            "p90_usd": 4.0,
            "max_usd": 4.2,
            "sample_count": 8,
        }
        state.adaptive_review_cycle_planning = {
            "planned_cost_usd": round(3.64 * 1.25, 4),
            "basis": sb.BASIS_OBSERVED_REVIEW_CYCLE,
            "fallback_configured_usd": 17.55,
            "sample_count": 3,
            "median_usd": 3.64,
            "p90_usd": 4.2,
            "max_usd": 4.2,
            "headroom_multiplier": 1.25,
            "reason": "seated from observed history",
            "excluded_for_taint": 0,
        }

        meta = ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)
        with patch(
            "theforge.coordinator.review_pool._story_budget.derive_review_cycle_planning_price",
            side_effect=AssertionError("dispatch should use the seated planning price"),
        ):
            _run_review_pool(
                state,
                config,
                task,
                "story",
                workspace,
                "branch",
                meta,
                notify=False,
                enforce_budgets=True,
            )

        assert state.allocation_exhausted is None
        assert mock_pool.called is True
        assert sorted(meta.successful) == ["a", "b", "c"]
        assert [float(profile.budget_usd) for profile in config.review_pool] == [5.85, 5.85, 5.85]
