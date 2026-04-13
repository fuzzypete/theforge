"""Tests for preflight conservative fallback scenarios.

Covers #332-style timeout and #257-style false BLOCKED:
  1. Agent non-success (timeout / exit=1) → degraded PROCEED with needs_planning / large.
  2. Ambiguous BLOCKED with prior-execution evidence → downgraded to PROCEED.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# ── BLOCKED output with an ambiguous/verifiability reason ────────────────────

PREFLIGHT_BLOCKED_AMBIGUOUS = """\
```yaml
verdict: BLOCKED
reason: "Acceptance criteria are ambiguous and not objectively verifiable."
complexity: small
sufficiency: needs_planning
work_type: feature
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Cannot verify without external API"
```
"""

PREFLIGHT_BLOCKED_CONCRETE = """\
```yaml
verdict: BLOCKED
reason: "Required dependency removed_function() no longer exists in the codebase."
complexity: medium
sufficiency: needs_planning
work_type: feature
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "removed_function() was deleted"
```
"""


class TestPreflightConservativeFallback:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_timeout_fallback_proceeds_with_degraded_status(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """#332: agent failure (timeout/exit=1) → degraded PROCEED, not BLOCKED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(success=False, output="", cost_usd=0.0)
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "timeout_no_verdict"
        assert result.state.preflight_sufficiency == "needs_planning"
        assert result.state.preflight_complexity == "large"
        # Run should proceed to completion, not ESCALATE
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow._has_prior_execution_evidence", return_value=True)
    def test_ambiguous_blocked_with_prior_commits_is_downgraded(
        self,
        mock_evidence,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """#257: ambiguous BLOCKED + prior-execution evidence → downgraded to PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_AMBIGUOUS, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "blocked_downgraded_prior_evidence"
        assert result.state.preflight_sufficiency == "needs_planning"
        # small→medium upgrade should have fired
        assert result.state.preflight_complexity in ("medium", "large")
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow._has_prior_execution_evidence", return_value=False)
    def test_genuine_blocked_without_prior_commits_not_downgraded(
        self,
        mock_evidence,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """Ambiguous BLOCKED *without* prior-execution evidence stays BLOCKED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_AMBIGUOUS, cost_usd=0.05
        )

        result = run_task(config, task)

        assert result.state.preflight_verdict == "BLOCKED"
        assert result.state.preflight_degraded is False
        assert result.phase == Phase.ESCALATE
        assert result.success is False

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow._has_prior_execution_evidence", return_value=True)
    def test_genuine_blocked_concrete_reason_not_downgraded(
        self,
        mock_evidence,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """Concrete BLOCKED (non-ambiguous) is NOT downgraded even with prior commits."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_CONCRETE, cost_usd=0.05
        )

        result = run_task(config, task)

        assert result.state.preflight_verdict == "BLOCKED"
        assert result.state.preflight_degraded is False
        assert result.phase == Phase.ESCALATE
        assert result.success is False
