"""Tests for preflight fallback and conservative PROCEED scenarios.

Covers fallback retry behavior plus existing degraded PROCEED handling.

Since #1951 the degraded-PROCEED / risk-signal-BLOCKED policy applies only when
the failed preflight still produced *some* model output (salvaged partial text
or a tool trace). A preflight that produced nothing at all is an infrastructure
failure, not a story verdict, and aborts the run instead.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config.types import ModelProfile
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.runners import AgentResult


def _crashed_with_salvaged_output(*, exit_code: int = 1, cost_usd: float = 0.0) -> AgentResult:
    """A failed preflight that still left real model output behind.

    The agent explored the codebase and reached a partial conclusion before it
    died — paid-for model judgment exists, so the degraded-verdict fallback
    policy (#332) applies rather than the no-judgment abort path (#1951).
    """
    return AgentResult(
        success=False,
        output="",
        session_id=None,
        cost_usd=cost_usd,
        exit_code=exit_code,
        raw={},
        profile_name="preflight",
        tool_trace=({"tool": "Read", "target": "src/theforge/coordinator/engine.py"},),
        partial_output="The story targets the coordinator's escalation path.",
    )


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
            patch_gate_shell() as mock_shell,
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
        # Compared without the per-attempt ledger, which has its own tests
        # (#2205); this test is about which attempts were recorded.
        attempts = [
            {k: v for k, v in a.items() if k != "ledger"}
            for a in result.state.preflight_result.raw["attempts"]
        ]
        assert all("ledger" in a for a in result.state.preflight_result.raw["attempts"])
        assert attempts == [
            {
                "profile_name": "preflight",
                "model": config.preflight_profile.model,
                "provider": config.preflight_profile.provider,
                "cli": config.preflight_profile.cli,
                "cost_usd": 0.11,
                "duration_s": result.state.preflight_result.raw["attempts"][0]["duration_s"],
                "success": True,
                "exit_code": 0,
                # Reliability completion (#1489): a clean primary success is completed.
                "completed": True,
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
            patch_gate_shell() as mock_shell,
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

    def test_both_fail_with_no_model_output_aborts_as_infrastructure(self, tmp_path):
        """#1951: primary + fallback both silent ⇒ no verdict, infrastructure abort."""
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
            patch_gate_shell() as mock_shell,
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

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.infrastructure_failure is True
        # The absence of a verdict is the point: nothing may read a PROCEED here.
        assert result.state.preflight_verdict is None
        assert result.state.preflight_failure_action == "infrastructure_abort"
        assert result.state.infrastructure_failure["phase"] == "PREFLIGHT"
        assert result.state.total_preflight_cost == 0.20
        # Dev never ran — the run stopped at the substrate failure.
        assert mock_dev.call_count == 0

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
            patch_gate_shell() as mock_shell,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            # Both attempts crashed but salvaged real model output, so the
            # conservative-PROCEED fallback still applies (#332).
            mock_preflight.side_effect = [
                _crashed_with_salvaged_output(cost_usd=0.07),
                _crashed_with_salvaged_output(cost_usd=0.13),
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
    @patch_gate_shell()
    def test_timeout_fallback_proceeds_with_degraded_status(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """#332: agent failure (timeout/exit=1) → degraded PROCEED, not BLOCKED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _crashed_with_salvaged_output()
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
    @patch_gate_shell()
    def test_no_model_output_aborts_instead_of_degraded_proceed(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """#1951: a preflight that produced nothing records no verdict at all.

        "No risk signals detected" after a substrate failure means nothing
        looked, not that nothing is risky — so it may not become a PROCEED.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = AgentResult(
            success=False,
            output="Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
            session_id=None,
            cost_usd=0.0,
            exit_code=1,
            raw={},
            profile_name="preflight",
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.infrastructure_failure is True
        assert result.state.preflight_verdict is None
        assert result.state.infrastructure_failure["category"] == "auth"
        assert result.state.agent_invocation_failures[0]["phase"] == "PREFLIGHT"
        # The run is tainted, so nothing downstream may learn from it.
        assert result.state.trust_checks["agent_judgment_obtained"]["result"] == "fail"
        assert mock_dev.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow._has_prior_execution_evidence", return_value=True)
    def test_sigkill_with_prior_commits_escalates(
        self,
        mock_evidence,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """SIGKILL on a story with prior-execution evidence escalates, not PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _crashed_with_salvaged_output(exit_code=-9)
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "BLOCKED"
        assert result.state.preflight_failure_action == "escalate"
        assert "prior_execution_on_branch" in result.state.preflight_risk_signals
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "agent_failed_with_risk_signals"
        assert result.phase == Phase.ESCALATE
        assert result.success is False

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow._has_prior_execution_evidence", return_value=False)
    def test_sigkill_with_reopen_context_escalates(
        self,
        mock_evidence,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """SIGKILL on a reopened story (## Reopen Context block in body) escalates."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        # Append a reopen-context block to the spec — simulates what
        # sprint/reopen_context.py adds for a reopened issue.
        spec_text = task.story_path.read_text(encoding="utf-8")
        task.story_path.write_text(
            spec_text + "\n\n## Reopen Context\n\nThis issue was reopened on 2026-05-01.\n"
            "Operator follow-up:\n\n> the previous attempt missed the audit fields\n",
            encoding="utf-8",
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # exit_code=-9 simulates SIGKILL, with model output salvaged from the
        # stream before the kill.
        mock_preflight.return_value = _crashed_with_salvaged_output(exit_code=-9, cost_usd=0.04)
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "BLOCKED"
        assert result.state.preflight_failure_action == "escalate"
        assert "reopen_context_in_body" in result.state.preflight_risk_signals
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "agent_failed_with_risk_signals"
        assert "exit=-9" in (result.state.preflight_reason or "")
        assert result.phase == Phase.ESCALATE
        assert result.success is False

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow._has_prior_execution_evidence", return_value=False)
    def test_sigkill_without_risk_signals_still_proceeds(
        self,
        mock_evidence,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """Fresh story (no reopen, no prior commits) preserves conservative PROCEED on SIGKILL."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _crashed_with_salvaged_output(exit_code=-9)
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_failure_action == "proceed"
        assert result.state.preflight_risk_signals == []
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "timeout_no_verdict"
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
    @patch_gate_shell()
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
