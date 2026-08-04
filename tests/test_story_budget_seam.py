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
