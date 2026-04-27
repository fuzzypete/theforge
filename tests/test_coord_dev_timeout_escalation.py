"""Tests for timeout-triggered model escalation in the coordinator.

Covers three scenarios:
  (a) Dev times out with a larger model available → escalates, continues in DEV
  (b) Dev times out with no larger model available → no escalation, falls through to existing path
  (c) Second timeout in same sprint → does not re-escalate (timeout_escalation_used gate)
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.runners import AgentResult


def _make_escalation_config(
    tmp_path: Path,
    models: list[str],
    max_dev_iterations: int = 3,
) -> ForgeConfig:
    """Config with auto_model_escalation=True and sonnet as dev model."""
    dev_profile = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=30.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )
    review_profile = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=10.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=dev_profile,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[review_profile],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=max_dev_iterations,
            max_review_cycles=2,
            auto_model_escalation=True,
        ),
        models=models,
    )


def _timeout_result(session_id: str = "sess-1") -> AgentResult:
    """AgentResult representing a SIGKILL timeout (exit_code=-9)."""
    return AgentResult(
        success=False,
        output="Partial progress before timeout.",
        session_id=session_id,
        cost_usd=0.10,
        exit_code=-9,
        raw={},
        profile_name="dev",
    )


class TestTimeoutEscalationAvailable:
    """Timeout with a larger model in the chain → escalate and continue."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_timeout_escalates_model_and_continues(
        self, mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
    ):
        """Dev exits -9 once; larger model available → escalate, second dev succeeds."""
        config = _make_escalation_config(tmp_path, models=["claude/sonnet", "claude/opus"])
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        # Gate: FAIL on first iteration (dev timed out), PASS on second (escalated model)
        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])

        dev_call = {"n": 0}

        def dev_side_effect(**kwargs):
            dev_call["n"] += 1
            if dev_call["n"] == 1:
                return _timeout_result()
            return _make_agent_result(success=True, output="Done.", session_id="sess-1")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev.side_effect = dev_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.timeout_escalation_used is True
        assert result.state.timeout_escalation_audit is not None
        audit = result.state.timeout_escalation_audit
        assert audit["original_model"] == "sonnet"
        assert audit["new_model"] == "opus"
        assert audit["reason"] == "timeout"
        assert audit["original_timeout_seconds"] > 0
        assert audit["new_timeout_seconds"] > 0
        assert dev_call["n"] == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_timeout_escalation_audit_in_state(
        self, mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
    ):
        """After timeout escalation, timeout_escalation_audit contains all required keys."""
        config = _make_escalation_config(tmp_path, models=["claude/sonnet", "claude/opus"])
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        dev_call = {"n": 0}

        def dev_side_effect(**kwargs):
            dev_call["n"] += 1
            if dev_call["n"] == 1:
                return _timeout_result()
            return _make_agent_result(success=True, output="Done.", session_id="sess-1")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev.side_effect = dev_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
        ]

        result = run_task(config, task)

        required_keys = {
            "original_model",
            "new_model",
            "original_timeout_seconds",
            "new_timeout_seconds",
            "reason",
        }
        assert required_keys.issubset(result.state.timeout_escalation_audit.keys())


class TestTimeoutNoLargerModel:
    """Timeout with no larger model → no escalation, falls through to existing path."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_timeout_no_escalation_when_at_top(
        self, mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
    ):
        """Dev times out, current model is already the largest → no escalation."""
        # Use opus as dev model and only opus in models list → no larger model available
        config = _make_escalation_config(tmp_path, models=["claude/opus"])
        dev_profile = ModelProfile(
            name="dev",
            cli="claude",
            model="opus",
            budget_usd=30.0,
            timeout_seconds=900,
            allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
        )
        config = replace(config, dev_profile=dev_profile)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        # Gate always fails → eventually exhausts iterations → ESCALATE
        mock_shell.side_effect = _shell_with_gate(workspace, "FAIL")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev.return_value = _timeout_result()
        mock_pool.return_value = []

        result = run_task(config, task)

        assert result.state.timeout_escalation_used is False
        assert result.state.timeout_escalation_audit is None
        # Run should end in ESCALATE (max iterations exhausted with no gate pass)
        assert result.success is False


class TestTimeoutNoReEscalation:
    """Second timeout in the same sprint does not trigger a second escalation."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_second_timeout_does_not_re_escalate(
        self, mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
    ):
        """First timeout escalates; second timeout does not escalate again."""
        config = _make_escalation_config(
            tmp_path, models=["claude/sonnet", "claude/opus"], max_dev_iterations=4
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        # Gate: FAIL, FAIL, PASS (first two iterations timeout, third succeeds)
        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "FAIL", "PASS"])

        dev_call = {"n": 0}

        def dev_side_effect(**kwargs):
            dev_call["n"] += 1
            if dev_call["n"] <= 2:
                return _timeout_result()
            return _make_agent_result(success=True, output="Done.", session_id="sess-1")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev.side_effect = dev_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
        ]

        result = run_task(config, task)

        # Escalation should have fired exactly once
        assert result.state.timeout_escalation_used is True
        assert result.state.timeout_escalation_audit is not None
        # Three dev calls: first times out (escalates), second times out (no re-escalate),
        # third succeeds
        assert dev_call["n"] == 3
        # The audit records the first escalation only
        assert result.state.timeout_escalation_audit["original_model"] == "sonnet"
        assert result.state.timeout_escalation_audit["new_model"] == "opus"


class TestTimeoutEscalationSprintSticky:
    """Timeout escalation is sprint-sticky: only one escalation fires per sprint
    even across different stories (separate run_task calls with the same sprint_name)."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_second_story_in_sprint_does_not_escalate(
        self, mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
    ):
        """Story A escalates; story B in the same sprint sees the flag and does not escalate."""
        sprint_name = "test-sprint-sticky"
        config_a = _make_escalation_config(tmp_path, models=["claude/sonnet", "claude/opus"])
        config_b = _make_escalation_config(tmp_path, models=["claude/sonnet", "claude/opus"])

        # Story A: times out once, escalates, then succeeds
        task_a = _make_task(tmp_path)
        workspace_a = tmp_path / task_a.slug
        workspace_a.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace_a, ["FAIL", "PASS"])
        dev_call_a = {"n": 0}

        def dev_side_effect_a(**kwargs):
            dev_call_a["n"] += 1
            if dev_call_a["n"] == 1:
                return _timeout_result()
            return _make_agent_result(success=True, output="Done.", session_id="sess-a")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev.side_effect = dev_side_effect_a
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
        ]

        result_a = run_task(config_a, task_a, sprint_name=sprint_name)
        assert result_a.state.timeout_escalation_used is True
        assert result_a.state.timeout_escalation_audit is not None

        # Story B: times out, but the sprint flag is set so no escalation should fire
        task_b_name = tmp_path / "spec_b.md"
        task_b_name.write_text("# Story B\n\nSecond story.", encoding="utf-8")
        from theforge.task import TaskStory

        task_b = TaskStory(name="Story B", story_path=task_b_name, slug="story-b")
        workspace_b = tmp_path / "story-b"
        workspace_b.mkdir()

        # Reset mocks for story B: gate FAIL then PASS (timeout resume without escalation)
        mock_shell.side_effect = _shell_with_gate(workspace_b, ["FAIL", "PASS"])
        dev_call_b = {"n": 0}

        def dev_side_effect_b(**kwargs):
            dev_call_b["n"] += 1
            if dev_call_b["n"] == 1:
                return _timeout_result(session_id="sess-b")
            return _make_agent_result(success=True, output="Done.", session_id="sess-b")

        mock_dev.side_effect = dev_side_effect_b

        result_b = run_task(config_b, task_b, sprint_name=sprint_name)

        # Story B should NOT have triggered a new escalation (flag was already set)
        assert result_b.state.timeout_escalation_used is True  # pre-populated from flag file
        assert result_b.state.timeout_escalation_audit is None  # no new escalation fired
