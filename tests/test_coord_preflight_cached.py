"""Tests for cached-preflight verdict dispatch in batch/sprint mode.

When run_task() receives a cached_preflight_state, the coordinator must honour
ALREADY_DONE and BLOCKED verdicts and terminate the run before reaching
PLAN/DEV — exactly as it does for freshly-run preflight in single-story mode.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_agent_result, _make_config, _make_task

from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorState, Phase


def _with_cache_snapshot(state: CoordinatorState) -> CoordinatorState:
    state.preflight_cache_snapshot = {
        "worktree_head": "OK",
        "evaluation_base_branch": "main",
        "evaluation_base_branch_head": "OK",
    }
    return state


def _shell_with_matching_cache(cmd, cwd, **kwargs):
    rendered = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    if rendered.startswith("git rev-parse "):
        return (True, "OK")
    return (True, "")


class TestCachedPreflightVerdictDispatch:
    def test_cached_already_done_skips_dev(self, tmp_path):
        """Cached ALREADY_DONE verdict → DONE without invoking plan or dev agents."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        cached_state = _with_cache_snapshot(
            replace(
                CoordinatorState(),
                preflight_verdict="ALREADY_DONE",
                preflight_reason="All acceptance criteria already satisfied.",
                preflight_complexity="medium",
                preflight_sufficiency="implementation_ready",
                preflight_work_type="feature",
            )
        )

        with (
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_matching_cache),
            patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=True),
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            result = run_task(config, task, cached_preflight_state=cached_state)

        assert result.phase == Phase.DONE
        assert result.success is True
        assert "already" in result.message.lower()
        assert mock_preflight.call_count == 0
        assert mock_dev.call_count == 0
        assert mock_plan.call_count == 0
        mock_pool.assert_not_called()
        assert len(result.state.dev_results) == 0
        assert len(result.state.review_results) == 0

    def test_cached_blocked_escalates_without_dev(self, tmp_path):
        """Cached BLOCKED verdict → ESCALATE without invoking plan or dev agents."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        cached_state = _with_cache_snapshot(
            replace(
                CoordinatorState(),
                preflight_verdict="BLOCKED",
                preflight_reason="Dependency removed_function() no longer exists.",
                preflight_complexity="medium",
                preflight_sufficiency="needs_planning",
                preflight_work_type="feature",
            )
        )

        with (
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_matching_cache),
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            result = run_task(config, task, cached_preflight_state=cached_state)

        assert result.phase == Phase.ESCALATE
        assert result.success is False
        assert "blocked" in result.message.lower()
        assert mock_preflight.call_count == 0
        assert mock_dev.call_count == 0
        assert mock_plan.call_count == 0
        mock_pool.assert_not_called()
        assert len(result.state.dev_results) == 0

    def test_cached_preflight_restores_complexity_score(self, tmp_path):
        """Cache restoration propagates preflight_complexity_score into new state.

        Seam-level coverage for Convention 8: without this, cached runs silently
        fall back to enum-only routing because the numeric score is lost at the
        cache boundary.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        cached_state = _with_cache_snapshot(
            replace(
                CoordinatorState(),
                preflight_verdict="ALREADY_DONE",
                preflight_reason="All acceptance criteria already satisfied.",
                preflight_complexity="medium",
                preflight_complexity_score=7,
                preflight_sufficiency="implementation_ready",
                preflight_work_type="feature",
            )
        )

        with (
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_matching_cache),
            patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=True),
            patch("theforge.coordinator.preflight_flow.run_agent"),
            patch("theforge.coordinator.dev_phase.run_agent"),
            patch("theforge.coordinator.plan_flow.run_agent"),
            patch("theforge.coordinator.review_pool.run_agent_pool"),
        ):
            result = run_task(config, task, cached_preflight_state=cached_state)

        assert result.state.preflight_complexity_score == 7
        assert result.state.preflight_complexity == "medium"

    def test_cached_preflight_cost_flows_into_result_state(self, tmp_path):
        """Batch preflight cost must appear in result.state.total_preflight_cost.

        When a story uses a cached preflight state (e.g. ALREADY_DONE from batch
        preflight), the cost recorded in the batch run must be carried into the
        per-story result so the sprint accumulator counts it.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        preflight_agent_result = _make_agent_result(cost_usd=0.349, profile_name="preflight")

        cached_state = _with_cache_snapshot(
            replace(
                CoordinatorState(),
                preflight_verdict="ALREADY_DONE",
                preflight_reason="All acceptance criteria already satisfied.",
                preflight_complexity="medium",
                preflight_sufficiency="implementation_ready",
                preflight_work_type="feature",
                preflight_result=preflight_agent_result,
            )
        )

        with (
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_matching_cache),
            patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=True),
            patch("theforge.coordinator.preflight_flow.run_agent"),
            patch("theforge.coordinator.dev_phase.run_agent"),
            patch("theforge.coordinator.plan_flow.run_agent"),
            patch("theforge.coordinator.review_pool.run_agent_pool"),
        ):
            result = run_task(config, task, cached_preflight_state=cached_state)

        assert result.state.preflight_result is preflight_agent_result
        assert result.state.total_preflight_cost == pytest.approx(0.349)
        assert result.state.total_cost == pytest.approx(0.349)

    def test_stale_cached_preflight_is_invalidated_and_rerun(self, tmp_path):
        """Changed git state must invalidate cached preflight and trigger a fresh run."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        cached_state = replace(
            CoordinatorState(),
            preflight_verdict="ALREADY_DONE",
            preflight_reason="Stale cached verdict.",
            preflight_complexity="medium",
            preflight_sufficiency="implementation_ready",
            preflight_work_type="feature",
            preflight_cache_snapshot={
                "worktree_head": "old-head",
                "evaluation_base_branch": "main",
                "evaluation_base_branch_head": "base-head",
            },
        )

        fresh_preflight = _make_agent_result(
            output="""\
```yaml
verdict: PROCEED
complexity: small
sufficiency: implementation_ready
work_type: bug
reason: "Head changed; reevaluated."
criteria_checked: []
```
""",
            profile_name="preflight",
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            rendered = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if rendered.startswith("git rev-parse "):
                return (True, "new-head" if str(cwd) == str(workspace) else "base-head")
            return (True, "")

        with (
            patch("theforge.coordinator.util._run_shell", side_effect=shell_side_effect),
            patch(
                "theforge.coordinator.preflight_flow.run_agent",
                return_value=fresh_preflight,
            ) as mock_preflight,
        ):
            result = run_task(
                config,
                task,
                cached_preflight_state=cached_state,
                stop_phase=Phase.PREFLIGHT,
            )

        assert result.success is True
        assert mock_preflight.call_count == 1
        assert result.state.preflight_cached is False
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_cache_validation["status"] == "invalidated"
        assert result.state.preflight_cache_validation["reason"] == "worktree_head_changed"
