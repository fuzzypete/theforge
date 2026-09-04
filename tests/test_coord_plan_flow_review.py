"""Tests for the human plan-review flow (blocking / advisory / ntfy)."""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    NotificationConfig,
    NtfyConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# ── Local helpers ─────────────────────────────────────────────────────


def _make_plan_review_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    mode: str = "blocking",
    timeout_seconds: int = 300,
) -> ForgeConfig:
    """Create a test config with PLAN and PLAN_REVIEW enabled."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig.of(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_review=PlanReviewConfig(enabled=enabled, mode=mode, timeout_seconds=timeout_seconds),
        log=LogConfig(enabled=False),
    )


def _make_ntfy_plan_review_config(
    tmp_path: Path,
    *,
    mode: str = "blocking",
    timeout_seconds: int = 10,
) -> ForgeConfig:
    """Create a test config with PLAN, PLAN_REVIEW, and ntfy notifications enabled."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        notifications=NotificationConfig(
            backend="ntfy",
            ntfy=NtfyConfig(url="https://ntfy.sh/test-topic", priority="default"),
        ),
        plan=PlanConfig.of(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_review=PlanReviewConfig(enabled=True, mode=mode, timeout_seconds=timeout_seconds),
        log=LogConfig(enabled=False),
    )


# ── TestPlanReview ────────────────────────────────────────────────────


def test_plan_phase_cleans_stale_plan_files_before_new_attempt(tmp_path):
    config = _make_plan_review_config(tmp_path, enabled=False)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    stale_plan = workspace / ".forge" / "plan.md"
    stale_plan.parent.mkdir(parents=True, exist_ok=True)
    stale_plan.write_text("stale plan", encoding="utf-8")
    legacy_plan = workspace / "forge_plan.md"
    legacy_plan.write_text("legacy stale plan", encoding="utf-8")
    traces_dir = workspace / ".forge" / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    stale_trace = traces_dir / "plan-attempt-0-approved.txt"
    stale_trace.write_text("old approved plan", encoding="utf-8")

    with (
        patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None)),
        patch(
            "theforge.coordinator.review_pool.run_agent_pool",
            return_value=[
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ],
        ),
        patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
        patch_gate_shell() as mock_shell,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_plan_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nFresh plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")

        result = run_task(config, task, interactive=False)

    assert result.success is True
    assert stale_plan.read_text(encoding="utf-8") == "# Plan\n\nFresh plan."
    assert not legacy_plan.exists()
    assert not stale_trace.exists()


class TestPlanReview:
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nOriginal plan."
        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["decision"] == "approve"
        assert audit["plan_review"]["regenerated"] is False
        assert audit["plan_review"]["mode"] == "blocking"
        assert mock_human_review.called

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_edit_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        edited_plan = "# Plan\n\nEdited by human."

        def plan_review_side_effect(*args, **kwargs):
            plan_path = workspace / ".forge" / "plan.md"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(edited_plan, encoding="utf-8")
            return "approve"

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.side_effect = plan_review_side_effect

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == edited_plan
        assert mock_human_review.called
        # planner original and human-edited version are both preserved
        traces_dir = workspace / ".forge" / "traces"
        assert (traces_dir / "plan-attempt-0.txt").exists()
        assert (traces_dir / "plan-attempt-0-approved.txt").exists()
        assert (traces_dir / "plan-attempt-0-approved.txt").read_text() == edited_plan

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_regenerate(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        plan_v1 = "# Plan\n\nFirst plan."
        plan_v2 = "# Plan\n\nRegenerated plan."

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=plan_v1, cost_usd=0.10),
            _make_agent_result(success=True, output=plan_v2, cost_usd=0.15),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.side_effect = ["regenerate", "approve"]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0
        assert result.state.plan_review_decision == "approve"
        assert len(result.state.plan_results) == 2
        assert result.state.plan_output == plan_v2
        assert mock_agent.call_count == 3  # PLAN + PLAN-regen + DEV (preflight mocked separately)
        assert mock_human_review.called

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_abandon(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10)
        ]
        mock_plan_review.return_value = "abandon"

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert "abandoned" in result.message.lower()
        assert workspace.exists()
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_regen_twice_abandons(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = dataclasses.replace(
            _make_plan_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nSecond plan.", cost_usd=0.15),
        ]
        mock_plan_review.side_effect = ["regenerate", "regenerate"]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert "rejected" in result.message.lower()
        assert result.state.plan_review_decision == "abandon"
        assert result.state.plan_regen_count > 0
        assert len(result.state.plan_results) == 2
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_skipped_on_injection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Injected plan\n\nUse this.", encoding="utf-8")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [_make_agent_result(success=True, output="Implemented.")]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        assert result.state.plan_review_decision is None
        mock_plan_review.assert_not_called()
        assert mock_human_review.called

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_skipped_when_disabled(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path, enabled=False)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision is None
        mock_plan_review.assert_not_called()
        assert mock_human_review.called

    def test_plan_review_eof_abandons(self, tmp_path, capsys):
        from theforge.coordinator.notify import _plan_review_interactive
        from theforge.coordinator.state import CoordinatorState

        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_text = "# Plan\n\nRead me."
        plan_path = workspace / ".forge" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan_text, encoding="utf-8")

        with patch("theforge.coordinator.notify.sys.stdin", io.StringIO("")):
            decision = _plan_review_interactive(CoordinatorState(), plan_text, workspace, task)

        captured = capsys.readouterr()
        assert decision == "abandon"
        assert plan_text in captured.out
        assert f"Plan at: {workspace / '.forge/plan.md'}" in captured.err

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_terminal_used_without_interactive_flag(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        """plan_review.enabled=True + interactive=False + no ntfy → terminal prompt is used."""
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=False)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        mock_plan_review.assert_called_once()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_remote")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_remote_ntfy_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_remote_review,
        mock_human_review,
        tmp_path,
    ):
        """Non-interactive + ntfy configured → _plan_review_remote is called."""
        config = _make_ntfy_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nNtfy plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_remote_review.return_value = "approve"

        result = run_task(config, task, interactive=False, notify=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        mock_remote_review.assert_called_once()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_remote")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_remote_ntfy_abandon(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_remote_review,
        mock_human_review,
        tmp_path,
    ):
        """Non-interactive + ntfy + remote returns abandon → run fails at PLAN_REVIEW."""
        config = _make_ntfy_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nNtfy plan.", cost_usd=0.10)
        ]
        mock_remote_review.return_value = "abandon"

        result = run_task(config, task, interactive=False, notify=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        mock_remote_review.assert_called_once()

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_advisory_without_ntfy_uses_terminal(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        """Advisory mode without ntfy → terminal prompt is used (not auto-approve)."""
        config = _make_plan_review_config(tmp_path, mode="advisory")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nAdvisory plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=False)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        mock_plan_review.assert_called_once()

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_abandon_phase_not_escalate(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10)
        ]
        mock_plan_review.return_value = "abandon"

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_human_regen_attempts_do_not_trigger_unassessable_escalation(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_run_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = dataclasses.replace(
            _make_plan_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=4,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_run_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nInitial plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nRegen 1.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nRegen 2.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nRegen 3.", cost_usd=0.10),
        ]
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.side_effect = ["regenerate", "regenerate", "regenerate", "approve"]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_disposition == "patch"
        assert result.state.plan_regen_assessment["reason_code"] == "too_few_attempts"
        assert result.state.plan_regen_assessment["attempt_count"] == 0
        assert result.state.plan_regen_assessment["recorded_attempt_count"] == 4
        assert "| Attempt |" in mock_run_agent.call_args_list[2].kwargs["prompt"]
        assert result.phase != Phase.ESCALATE
        assert mock_human_review.called

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_review_reread_error(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def plan_review_side_effect(*args, **kwargs):
            (workspace / ".forge" / "plan.md").unlink()
            return "approve"

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10)
        ]
        mock_plan_review.side_effect = plan_review_side_effect

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "unreadable after edit" in result.message
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_regen_tracks_both_costs(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nSecond plan.", cost_usd=0.15),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.side_effect = ["regenerate", "approve"]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.total_plan_cost == pytest.approx(0.25)
        assert len(result.state.plan_results) == 2
        assert mock_human_review.called
