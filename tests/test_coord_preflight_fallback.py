"""Tests for preflight fallback and conservative PROCEED scenarios.

Covers fallback retry behavior plus existing degraded PROCEED handling.
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

from theforge.config.types import ModelProfile
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


PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Needs implementation."
complexity: medium
sufficiency: needs_planning
work_type: feature
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found"
```
"""


class TestPreflightFallbackRetry:
    def test_primary_success_does_not_use_fallback(self, tmp_path):
        config = _make_config(tmp_path)
        config = config.__class__(
            **{
                **config.__dict__,
                "preflight_fallback_profile": ModelProfile(
                    name="preflight_fallback",
                    cli="gemini",
                    model="gemini-2.5-pro",
                    budget_usd=1.0,
                    timeout_seconds=300,
                    allowed_tools=("Read", "Bash", "Glob", "Grep"),
                    phase="preflight",
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch("theforge.coordinator.util._run_shell") as mock_shell,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            mock_preflight.return_value = _make_agent_result(
                success=True, output=PREFLIGHT_PROCEED, cost_usd=0.11
            )
            mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
            mock_plan_agent.side_effect = mock_dev
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]

            result = run_task(config, task)

        assert result.success is True
        assert mock_preflight.call_count == 1
        assert result.state.total_preflight_cost == 0.11
        assert result.state.preflight_result.raw["attempts"] == [
            {
                "profile_name": "preflight",
                "model": config.preflight_profile.model,
                "cost_usd": 0.11,
                "duration_s": result.state.preflight_result.raw["attempts"][0]["duration_s"],
                "success": True,
                "exit_code": 0,
            }
        ]

    def test_primary_failure_fallback_success(self, tmp_path):
        config = _make_config(tmp_path)
        fallback = ModelProfile(
            name="preflight_fallback",
            cli="gemini",
            model="gemini-2.5-pro",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash", "Glob", "Grep"),
            phase="preflight",
        )
        config = config.__class__(**{**config.__dict__, "preflight_fallback_profile": fallback})
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch("theforge.coordinator.util._run_shell") as mock_shell,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            mock_preflight.side_effect = [
                _make_agent_result(success=False, output="", cost_usd=0.07),
                _make_agent_result(success=True, output=PREFLIGHT_PROCEED, cost_usd=0.13),
            ]
            mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
            mock_plan_agent.side_effect = mock_dev
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]

            result = run_task(config, task)

        assert result.success is True
        assert mock_preflight.call_count == 2
        assert [call.kwargs["profile"].name for call in mock_preflight.call_args_list] == [
            "preflight",
            "preflight_fallback",
        ]
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.total_preflight_cost == 0.20

    def test_both_fail_proceed_conservatively(self, tmp_path):
        config = _make_config(tmp_path)
        fallback = ModelProfile(
            name="preflight_fallback",
            cli="gemini",
            model="gemini-2.5-pro",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash", "Glob", "Grep"),
            phase="preflight",
        )
        config = config.__class__(**{**config.__dict__, "preflight_fallback_profile": fallback})
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch("theforge.coordinator.util._run_shell") as mock_shell,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            mock_preflight.side_effect = [
                _make_agent_result(success=False, output="", cost_usd=0.07),
                _make_agent_result(success=False, output="", cost_usd=0.13),
            ]
            mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
            mock_plan_agent.side_effect = mock_dev
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]

            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "timeout_no_verdict"
        assert result.state.total_preflight_cost == 0.20


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
