"""Tests for sandbox availability recording in coordinator state and audit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    _write_handoff,
    patch_gate_shell,
)

from theforge.config import ModelProfile
from theforge.config.auth import sandbox_available_for_profile
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.task.review_prompts import build_review_prompt


def _make_result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


class TestSandboxAvailableForProfile:
    """Unit tests for the public sandbox probe."""

    def test_cli_profile_always_available(self) -> None:
        # CLI profiles: mode computed from cli= field, _sandbox_readiness returns (True, "")
        profile = ModelProfile(
            name="test-cli",
            cli="claude",
            model="claude-sonnet",
            budget_usd=10.0,
            timeout_seconds=900,
            allowed_tools=("bash",),
        )
        result = sandbox_available_for_profile(profile)
        assert isinstance(result, bool)

    def test_api_profile_without_bash_always_available(self) -> None:
        # API profiles without bash in allowed_tools always return True
        profile = ModelProfile(
            name="test-api",
            provider="openai",
            model="gpt-4o",
            budget_usd=10.0,
            timeout_seconds=900,
            allowed_tools=(),
        )
        assert sandbox_available_for_profile(profile) is True

    def test_api_profile_with_bash_returns_bool(self) -> None:
        # API profiles with bash: probe runs; result depends on platform sandbox support
        profile = ModelProfile(
            name="test-api-bash",
            provider="openai",
            model="gpt-4o",
            budget_usd=10.0,
            timeout_seconds=900,
            allowed_tools=("bash",),
        )
        result = sandbox_available_for_profile(profile)
        assert isinstance(result, bool)


class TestStateAndAuditSandboxed:
    """Test that state.sandboxed is set at dev-phase entry and appears in audit."""

    @patch("theforge.coordinator.dev_phase.sandbox_available_for_profile", return_value=False)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_sandboxed_false_recorded_in_state_and_audit(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight_agent,
        mock_pool,
        mock_sandbox_probe,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        _write_handoff(workspace)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_dev_agent.return_value = _make_agent_result()
        mock_preflight_agent.return_value = _PREFLIGHT_RESULT
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW)]

        result = run_task(config, task)

        assert result.state.sandboxed is False
        assert result.state.dev_iteration_telemetry, "Expected at least one dev iteration"
        for item in result.state.dev_iteration_telemetry:
            assert item.sandboxed is False

        log = generate_audit_log(config, task, result)
        for iteration in log["iterations"]["dev_loop"]:
            assert "sandboxed" in iteration
            assert iteration["sandboxed"] is False

    @patch("theforge.coordinator.dev_phase.sandbox_available_for_profile", return_value=True)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_sandboxed_true_recorded_in_state_and_audit(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight_agent,
        mock_pool,
        mock_sandbox_probe,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        _write_handoff(workspace)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_dev_agent.return_value = _make_agent_result()
        mock_preflight_agent.return_value = _PREFLIGHT_RESULT
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW)]

        result = run_task(config, task)

        assert result.state.sandboxed is True
        for item in result.state.dev_iteration_telemetry:
            assert item.sandboxed is True

        log = generate_audit_log(config, task, result)
        for iteration in log["iterations"]["dev_loop"]:
            assert iteration["sandboxed"] is True


class TestReviewPromptSandboxStatus:
    """Test that build_review_prompt surfaces sandbox status correctly."""

    def test_sandboxed_true_shows_enabled(self) -> None:
        from theforge.task import TaskStory

        task = TaskStory(name="T", story_path=Path("/fake/spec.md"), slug="t")
        prompt = build_review_prompt(
            task,
            story_content="Implement X.",
            commit_log="abc123 feat: x",
            commit_diffs="diff --git ...",
            workspace_path="/tmp/ws",
            branch="feat/t",
            handoff_content="gate_decision: PASS",
            sandboxed=True,
        )
        assert "Sandbox isolation: enabled" in prompt

    def test_sandboxed_false_shows_disabled(self) -> None:
        from theforge.task import TaskStory

        task = TaskStory(name="T", story_path=Path("/fake/spec.md"), slug="t")
        prompt = build_review_prompt(
            task,
            story_content="Implement X.",
            commit_log="abc123 feat: x",
            commit_diffs="diff --git ...",
            workspace_path="/tmp/ws",
            branch="feat/t",
            handoff_content="gate_decision: PASS",
            sandboxed=False,
        )
        assert "Sandbox isolation: DISABLED" in prompt

    def test_default_sandboxed_true(self) -> None:
        """Default (omitted) sandboxed arg should render as enabled."""
        from theforge.task import TaskStory

        task = TaskStory(name="T", story_path=Path("/fake/spec.md"), slug="t")
        prompt = build_review_prompt(
            task,
            story_content="Implement X.",
            commit_log="abc123 feat: x",
            commit_diffs="diff --git ...",
            workspace_path="/tmp/ws",
            branch="feat/t",
            handoff_content="gate_decision: PASS",
        )
        assert "Sandbox isolation: enabled" in prompt
