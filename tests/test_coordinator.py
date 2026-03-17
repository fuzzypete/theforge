"""Tests for the coordinator state machine.

Uses mocked runner to test all state transitions without real agent calls.
"""

import dataclasses
import datetime
import io
import time as _time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    NotificationConfig,
    NtfyConfig,
    PlanAgentReviewConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import (
    Phase,
    generate_audit_log,
    run_from_review,
    run_review_only,
    run_task,
)
from theforge.runner import AgentResult, LogLevel
from theforge.task import TaskSpec

# ── Fixtures ─────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config pointing at tmp_path (single reviewer, no synthesis)."""
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
        log=LogConfig(enabled=False),
    )


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
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
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
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
        plan_review=PlanReviewConfig(enabled=True, mode=mode, timeout_seconds=timeout_seconds),
        log=LogConfig(enabled=False),
    )


def _make_pool_config(
    tmp_path: Path, profiles: list[ModelProfile], synthesis: ModelProfile
) -> ForgeConfig:
    """Create a test config with multi-model review pool."""
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
        review_pool=profiles,
        synthesis_profile=synthesis,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path) -> TaskSpec:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskSpec(
        name="Test Task",
        spec_path=spec,
        slug="test-task",
        file_scope=["src/"],
    )


def _make_agent_result(
    success: bool = True,
    output: str = "Done.",
    session_id: str | None = "sess-1",
    cost_usd: float = 0.50,
    profile_name: str = "",
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=session_id,
        cost_usd=cost_usd,
        exit_code=0 if success else 1,
        raw={},
        profile_name=profile_name,
    )


def _make_pool_result(
    outputs: list[str],
    profile_names: list[str],
    success: bool = True,
    cost_usd: float = 0.20,
) -> list[AgentResult]:
    """Build a list of AgentResults as if returned by run_agent_pool."""
    return [
        AgentResult(
            success=success,
            output=out,
            session_id=None,
            cost_usd=cost_usd,
            exit_code=0 if success else 1,
            raw={},
            profile_name=name,
        )
        for out, name in zip(outputs, profile_names)
    ]


_VALID_DEV_NOTES = (
    'summary: "Implemented the feature."\n'
    "commits:\n"
    '  - sha: "abc1234"\n'
    '    message: "feat: implement"\n'
    "acceptance_criteria:\n"
    '  - criterion: "It works"\n'
    "    status: MET\n"
    '    notes: "tested"\n'
    "spec_deviations: none\n"
    "deferred_items: none\n"
    "gate_result: PASS\n"
)


def _write_handoff(workspace: Path, decision: str = "PASS") -> None:
    """Write a minimal handoff.yaml in the workspace with valid dev_notes."""
    handoff = {
        "gate_decision": decision,
        "validation": {"make_fmt": {"status": "PASS"}},
        "scope_completed": ["test item"],
        "dev_notes": _VALID_DEV_NOTES,
    }
    (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")


# Stale-worktree detection commands need specific responses so that pre-created
# workspaces in existing tests are treated as "fresh" (reused rather than removed).
_RECENT_COMMIT_TS = str(int(_time.time()) - 60)  # 1 minute ago


def _handle_stale_check_cmd(cmd: str) -> tuple[bool, str] | None:
    """Return a mock response for stale-worktree detection git commands, or None if not matched."""
    if "rev-parse --abbrev-ref HEAD" in cmd:
        return (True, "forge/test-task")
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "abc123 a recent commit")
    if "--format=%ct" in cmd:
        return (True, _RECENT_COMMIT_TS)
    return None


def _shell_with_gate(workspace: Path, decisions: list[str] | str = "PASS"):
    """Create a _run_shell side_effect that simulates gate execution via exit code.

    Gate pass/fail is determined by exit code. On PASS, also writes a valid
    handoff.yaml so downstream handoff validation succeeds. On FAIL, returns a
    non-zero exit so the coordinator treats it as a gate failure.

    Also handles stale-worktree detection commands by returning a "fresh" worktree
    (recent commit, commits ahead of base) so pre-created workspaces are reused.
    """
    if isinstance(decisions, str):
        decisions_list = [decisions] * 20
    else:
        decisions_list = list(decisions)
    gate_idx = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if "gate" in cmd:
            d = decisions_list[min(gate_idx["n"], len(decisions_list) - 1)]
            gate_idx["n"] += 1
            if d == "PASS":
                _write_handoff(Path(cwd), d)
                return (True, "OK")
            else:
                return (False, "FAIL: tests failed")
        if "git status --porcelain" in cmd:
            return (True, "")  # clean worktree
        stale_resp = _handle_stale_check_cmd(cmd)
        if stale_resp is not None:
            return stale_resp
        return (True, "OK")

    return side_effect


APPROVE_REVIEW = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
spec_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""

REQUEST_CHANGES_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Bug found."
findings:
  - severity: P1
    file: src/foo.py
    line: 10
    description: "Off by one"
    suggestion: "Fix it"
spec_compliance:
  matches_spec: false
  mismatches:
    - "Missing batch config"
test_coverage:
  adequate: false
  gaps:
    - "No edge case test"
```
"""

PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Spec requirements are not yet implemented."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

PREFLIGHT_PROCEED_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Spec requirements are not yet implemented."
criteria_checked: []
```
"""

PREFLIGHT_ALREADY_DONE = """\
```yaml
verdict: ALREADY_DONE
reason: "All acceptance criteria are already satisfied."
criteria_checked:
  - criterion: "Feature X"
    satisfied: true
    evidence: "Implemented in coordinator.py:42"
```
"""

PREFLIGHT_BLOCKED = """\
```yaml
verdict: BLOCKED
reason: "Spec references removed_function() which no longer exists."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "removed_function() was deleted in a prior commit"
```
"""


_PREFLIGHT_RESULT = _make_agent_result(
    success=True, output=PREFLIGHT_PROCEED, cost_usd=0.05, profile_name="review"
)


def _preflight_then(*dev_results: AgentResult):
    """Preflight PROCEED on first call, then dev_results."""
    preflight_result = _PREFLIGHT_RESULT
    results = [preflight_result, *dev_results]
    call_idx = {"n": 0}

    def side_effect(**kwargs):
        idx = min(call_idx["n"], len(results) - 1)
        call_idx["n"] += 1
        return results[idx]

    return side_effect


# ── Tests ────────────────────────────────────────────────────────────


class TestCoordinatorHappyPath:
    """Test the golden path: dev succeeds, gate passes, review approves."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_single_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        assert result.state.dev_iteration == 1
        assert len(result.state.dev_results) == 1
        assert len(result.state.review_results) == 1


class TestPlanReview:
    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_approve(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, mock_human_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_edit_approve(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, mock_human_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        edited_plan = "# Plan\n\nEdited by human."

        def plan_review_side_effect(*args, **kwargs):
            (workspace / "forge_plan.md").write_text(edited_plan, encoding="utf-8")
            return "approve"

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_regenerate(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, mock_human_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        plan_v1 = "# Plan\n\nFirst plan."
        plan_v2 = "# Plan\n\nRegenerated plan."

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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
        assert mock_agent.call_count == 4
        assert mock_human_review.called

    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_abandon(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
        ]
        mock_plan_review.return_value = "abandon"

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert "abandoned" in result.message.lower()
        assert workspace.exists()
        mock_pool.assert_not_called()

    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_regen_twice_abandons(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, tmp_path
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
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_skipped_on_injection(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, mock_human_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Injected plan\n\nUse this.", encoding="utf-8")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        assert result.state.plan_review_decision is None
        mock_plan_review.assert_not_called()
        assert mock_human_review.called

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_skipped_when_disabled(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, mock_human_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path, enabled=False)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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
        from theforge.coord_notify import _plan_review_interactive
        from theforge.coordinator import CoordinatorState

        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_text = "# Plan\n\nRead me."
        (workspace / "forge_plan.md").write_text(plan_text, encoding="utf-8")

        with patch("theforge.coord_notify.sys.stdin", io.StringIO("")):
            decision = _plan_review_interactive(CoordinatorState(), plan_text, workspace, task)

        captured = capsys.readouterr()
        assert decision == "abandon"
        assert plan_text in captured.out
        assert f"Plan at: {workspace / 'forge_plan.md'}" in captured.err

    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_terminal_used_without_interactive_flag(
        self,
        mock_shell,
        mock_agent,
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
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_remote")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_remote_ntfy_approve(
        self, mock_shell, mock_agent, mock_pool, mock_remote_review, mock_human_review, tmp_path
    ):
        """Non-interactive + ntfy configured → _plan_review_remote is called."""
        config = _make_ntfy_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_remote")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_remote_ntfy_abandon(
        self, mock_shell, mock_agent, mock_pool, mock_remote_review, mock_human_review, tmp_path
    ):
        """Non-interactive + ntfy + remote returns abandon → run fails at PLAN_REVIEW."""
        config = _make_ntfy_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nNtfy plan.", cost_usd=0.10),
        ]
        mock_remote_review.return_value = "abandon"

        result = run_task(config, task, interactive=False, notify=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        mock_remote_review.assert_called_once()

    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_advisory_without_ntfy_uses_terminal(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, tmp_path
    ):
        """Advisory mode without ntfy → terminal prompt is used (not auto-approve)."""
        config = _make_plan_review_config(tmp_path, mode="advisory")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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

    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_abandon_phase_not_escalate(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
        ]
        mock_plan_review.return_value = "abandon"

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert result.phase != Phase.ESCALATE

    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_review_reread_error(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def plan_review_side_effect(*args, **kwargs):
            (workspace / "forge_plan.md").unlink()
            return "approve"

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
        ]
        mock_plan_review.side_effect = plan_review_side_effect

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "unreadable after edit" in result.message
        mock_pool.assert_not_called()

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator._plan_review_interactive")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_regen_tracks_both_costs(
        self, mock_shell, mock_agent, mock_pool, mock_plan_review, mock_human_review, tmp_path
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
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


class TestCoordinatorGateFailRetry:
    """Test that gate failure retries the dev agent."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_gate_fail_then_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.dev_iteration == 2  # needed a retry


class TestCoordinatorReviewRequestChanges:
    """Test that review REQUEST_CHANGES loops back to dev."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_review_then_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.review_cycle == 2


class TestCoordinatorPromptRouting:
    """Test that the correct prompt builder is called based on retry_reason."""

    @patch("theforge.coordinator.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_review_changes_routes_to_fix_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """retry_reason='review_changes' → build_fix_prompt() on second dev call."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(), _make_agent_result())
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert mock_fix_prompt.called, "build_fix_prompt should be called on review iteration"
        assert mock_dev_prompt.call_count >= 1, "build_dev_prompt should be called on first run"

    @patch("theforge.coordinator.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_gate_fail_routes_to_dev_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """retry_reason='gate_fail' → build_dev_prompt() on retry, not build_fix_prompt()."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_agent.side_effect = _preflight_then(_make_agent_result(), _make_agent_result())
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_iteration == 2
        assert not mock_fix_prompt.called, "build_fix_prompt must NOT be called on gate_fail retry"
        assert mock_dev_prompt.call_count == 2, "build_dev_prompt should be called both times"

    @patch("theforge.coordinator.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator._human_review")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_extend_routes_to_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_pool,
        mock_human_review,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """retry_reason='extend' → build_fix_prompt() when human extends after APPROVE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(), _make_agent_result())
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        extend_review = """\
```yaml
verdict: APPROVE
summary: "Acceptable but please also add logging."
findings:
  - severity: P2
    file: src/foo.py
    line: 5
    description: "Missing log statement"
    suggestion: "Add logger.info(...)"
spec_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(success=True, output=extend_review, profile_name="review")
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        # First call: human extends; second call: human approves
        human_call = {"n": 0}

        def human_review_side_effect(*args, **kwargs):
            human_call["n"] += 1
            if human_call["n"] == 1:
                return ("extend", "")
            return ("approve", "")

        mock_human_review.side_effect = human_review_side_effect

        run_task(config, task, interactive=True)

        assert mock_fix_prompt.called, "build_fix_prompt should be called after extend"


class TestCoordinatorSessionResume:
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_dev_session_carried_on_timeout(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        timeout_result = AgentResult(
            success=False,
            output="TIMEOUT",
            session_id="sess-timeout",
            cost_usd=0.10,
            exit_code=-9,
            raw={},
            profile_name="dev",
        )
        resumed_result = _make_agent_result(
            success=True, output="Done.", session_id="sess-resumed", profile_name="dev"
        )
        dev_session_ids: list[str | None] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None):
            if profile.name == "preflight":
                return _PREFLIGHT_RESULT
            dev_session_ids.append(session_id)
            if len(dev_session_ids) == 1:
                return timeout_result
            return resumed_result

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert dev_session_ids == [None, "sess-timeout"]
        assert result.state.dev_session_id == "sess-resumed"

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_dev_session_carried_across_review_cycles(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dev_session_ids: list[str | None] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None):
            if profile.name == "preflight":
                return _PREFLIGHT_RESULT
            dev_session_ids.append(session_id)
            if len(dev_session_ids) == 1:
                return _make_agent_result(
                    success=True,
                    output="Implemented.",
                    session_id="dev-sess-1",
                    profile_name="dev",
                )
            return _make_agent_result(
                success=True, output="Fixed.", session_id="dev-sess-2", profile_name="dev"
            )

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_agent.side_effect = fake_run_agent
        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert dev_session_ids == [None, "dev-sess-1"]
        assert result.state.dev_session_id == "dev-sess-2"

    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_timeout_resume_uses_short_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "full dev prompt"
        dev_prompts: list[str] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None):
            if profile.name == "preflight":
                return _PREFLIGHT_RESULT
            dev_prompts.append(prompt)
            if len(dev_prompts) == 1:
                return AgentResult(
                    success=False,
                    output="TIMEOUT",
                    session_id="sess-timeout",
                    cost_usd=0.10,
                    exit_code=-9,
                    raw={},
                    profile_name="dev",
                )
            return _make_agent_result(success=True, output="Done.", profile_name="dev")

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert dev_prompts[0] == "full dev prompt"
        assert "You were cut off by a timeout." in dev_prompts[1]
        assert dev_prompts[1] != "full dev prompt"

    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_timeout_resume_only_overrides_gate_fail(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "full dev prompt"
        dev_prompts: list[str] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None):
            if profile.name == "preflight":
                return _PREFLIGHT_RESULT
            dev_prompts.append(prompt)
            if len(dev_prompts) == 1:
                return AgentResult(
                    success=False,
                    output="TIMEOUT",
                    session_id="sess-timeout",
                    cost_usd=0.10,
                    exit_code=-9,
                    raw={},
                    profile_name="dev",
                )
            return _make_agent_result(success=True, output="Done.", profile_name="dev")

        shell_calls = {"status": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                shell_calls["status"] += 1
                if shell_calls["status"] == 1:
                    return (True, " M src/foo.py")
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert mock_dev_prompt.call_count == 2
        assert dev_prompts == ["full dev prompt", "full dev prompt"]
        second_feedback = mock_dev_prompt.call_args_list[1].kwargs["human_feedback"]
        assert "PROCESS VIOLATION" in second_feedback

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_reviewer_sessions_accumulate(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
        )

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.reviewer_session_ids == {"review": "review-sess-2"}

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_reviewer_sessions_passed_to_pool(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_session_ids: list[list[str | None]] = []

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
        )

        def pool_side_effect(**kwargs):
            captured_session_ids.append(list(kwargs["session_ids"]))
            if len(captured_session_ids) == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert captured_session_ids == [[None], ["review-sess-1"]]


class TestCoordinatorEscalation:
    """Test that exhausting retries escalates to human."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_gate_exhaustion(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "FAIL")
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 2  # hit max

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_review_exhaustion(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()


class TestCoordinatorSchemaErrorOverride:
    """Test that APPROVE with schema errors triggers reviewer retry (not a full dev cycle)."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_approve_with_schema_errors_triggers_retry(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        # Review YAML that says APPROVE but is missing required fields
        malformed_approve = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=malformed_approve, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # Schema error triggers reviewer retry (not a dev cycle increment)
        assert result.state.review_cycle == 1
        # Parse retry was tracked in cycle metadata
        assert result.state.review_cycle_metadata[0].parse_retries == 1


class TestCoordinatorCostTracking:
    """Test that both dev and review costs are tracked."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_total_cost_includes_review(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.75,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.90,
            exit_code=0,
            raw={},
            profile_name="review",
        )

        mock_agent.side_effect = _preflight_then(dev_result)
        mock_pool.return_value = [review_result]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.total_dev_cost == 0.75
        assert result.state.total_review_cost == 0.90
        # total_cost includes preflight ($0.05) + dev + review
        assert result.state.total_cost == pytest.approx(0.05 + 0.75 + 0.90)


class TestCoordinatorBudgetEnforcement:
    """Test that budget limits are enforced for dev and review agents."""

    def _make_budget_config(
        self, tmp_path: Path, dev_budget: float, review_budget: float
    ) -> ForgeConfig:
        """Create a config with tight budgets for testing."""
        dev_profile = ModelProfile(
            name=DEFAULT_DEV_PROFILE.name,
            cli=DEFAULT_DEV_PROFILE.cli,
            model=DEFAULT_DEV_PROFILE.model,
            budget_usd=dev_budget,
            timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
        )
        review_profile = ModelProfile(
            name=DEFAULT_REVIEW_PROFILE.name,
            cli=DEFAULT_REVIEW_PROFILE.cli,
            model=DEFAULT_REVIEW_PROFILE.model,
            budget_usd=review_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
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
            retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=3),
        )

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_dev_budget_exceeded_first_call(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Dev agent exceeds budget on first call → ESCALATE with budget error."""
        config = self._make_budget_config(tmp_path, dev_budget=0.40, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")

        # Agent costs $0.50, budget is $0.40 → immediate escalation
        expensive_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_agent.side_effect = _preflight_then(expensive_result)

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "0.5000" in result.message
        assert "0.4000" in result.message
        # Only one dev invocation — escalated before retry
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_dev_budget_exceeded_on_retry(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Dev agent exceeds budget on second call (retry) → ESCALATE."""
        config = self._make_budget_config(tmp_path, dev_budget=0.50, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])

        retry_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_agent.side_effect = _preflight_then(retry_result)

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        # Two dev invocations: $0.30 + $0.30 = $0.60 > $0.50
        assert len(result.state.dev_results) == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_review_budget_exceeded(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Review agent exceeds per-profile budget → ESCALATE."""
        config = self._make_budget_config(tmp_path, dev_budget=2.00, review_budget=0.40)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.10,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        # Profile name must match pool entry name for per-profile enforcement
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="review",
        )

        mock_agent.side_effect = _preflight_then(dev_result)
        mock_pool.return_value = [review_result]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "0.5000" in result.message
        assert "0.4000" in result.message
        assert len(result.state.review_agent_results) == 1


class TestCoordinatorSchemaErrorOnRequestChanges:
    """Test that malformed REQUEST_CHANGES triggers reviewer retry (not a full dev cycle)."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_malformed_request_changes_triggers_retry(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))

        # Malformed REQUEST_CHANGES: has P1 finding but spec_compliance missing fields
        malformed_review = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Needs work"
findings:
  - severity: P1
    file: src/foo.py
    description: "Bug"
    suggestion: "Fix"
test_coverage:
  adequate: false
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=malformed_review, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        # Schema error on REQUEST_CHANGES triggers retry — task completes on second attempt
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        # Parse retry was tracked
        assert result.state.review_cycle_metadata[0].parse_retries == 1


def _make_review_profile(name: str, budget_usd: float = 1.0) -> ModelProfile:
    return ModelProfile(
        name=name,
        cli="claude",
        model="opus",
        budget_usd=budget_usd,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


SYNTHESIS_PROFILE = ModelProfile(
    name="synthesis",
    cli="claude",
    model="opus",
    budget_usd=1.50,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash"),
)


class TestCoordinatorMultiModelReview:
    """Tests for pool of 2+ reviewers with synthesis."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_pool_of_2_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Pool of 2 reviews → merge → APPROVE."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent: DEV (first call)
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_pool_of_2_request_changes(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Pool of 2 reviews → synthesis → REQUEST_CHANGES → ESCALATE after max cycles."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent calls: DEV (cycle 1), SYNTHESIS (cycle 1), DEV (cycle 2), SYNTHESIS (cycle 2)
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(
                success=True, output=REQUEST_CHANGES_REVIEW, profile_name="synthesis"
            ),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
            _make_agent_result(
                success=True, output=REQUEST_CHANGES_REVIEW, profile_name="synthesis"
            ),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1 out", profile_name="r1"),
            _make_agent_result(success=True, output="R2 out", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_pool_of_1_skips_synthesis(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Pool of 1 → uses output directly, no synthesis call."""
        # synthesis_profile is set but pool has only 1 entry
        single_profile = _make_review_profile("solo")
        config = ForgeConfig(
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
            review_pool=[single_profile],
            synthesis_profile=None,  # pool of 1 → no synthesis
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="solo"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # run_agent called for PREFLIGHT + DEV (no synthesis for pool of 1)
        assert mock_agent.call_count == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_mixed_success_failure_degrades_to_single_no_synthesis(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """1 of 2 reviewers succeeds → single output used directly (no synthesis)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent: only DEV — no synthesis since we degrade to 1 successful
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=False, output="TIMEOUT", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # PREFLIGHT + DEV called run_agent; synthesis was skipped
        assert mock_agent.call_count == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_all_reviewers_fail_escalates(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """All reviewers fail → ESCALATE."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=False, output="TIMEOUT", profile_name="r1"),
            _make_agent_result(success=False, output="CRASH", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "failed" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_per_profile_budget_enforcement(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """One pool profile over budget → ESCALATE."""
        tight_profile = _make_review_profile("tight", budget_usd=0.10)
        normal_profile = _make_review_profile("normal", budget_usd=5.00)
        config = _make_pool_config(tmp_path, [tight_profile, normal_profile], SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        # tight profile costs $0.50 which exceeds its $0.10 budget
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="tight", cost_usd=0.50),
            _make_agent_result(success=True, output="R2", profile_name="normal", cost_usd=0.10),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "tight" in result.message


class TestCoordinatorAuditTiming:
    """Test that audit log includes timing and started_at fields."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_started_at_set_in_state(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """CoordinatorState.started_at is set when run_task() begins."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.started_at is not None
        # Should be a valid ISO timestamp

        dt = datetime.datetime.fromisoformat(result.state.started_at)
        assert dt.tzinfo is not None  # timezone-aware

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_audit_log_timing_fields(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """generate_audit_log() includes started_at, finished_at, duration_seconds."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        timing = audit["timing"]
        assert "started_at" in timing
        assert "finished_at" in timing
        assert "duration_seconds" in timing
        assert timing["started_at"] is not None
        assert timing["finished_at"] is not None
        assert timing["duration_seconds"] is not None
        assert timing["duration_seconds"] >= 0


class TestCoordinatorAuditAgentBreakdown:
    """Test per-agent cost breakdown in audit log."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_cost_agents_list(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """cost.agents contains one entry per dev and review invocation."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        dev_result_30 = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_agent.side_effect = _preflight_then(dev_result_30)
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id="s2",
                cost_usd=0.20,
                exit_code=0,
                raw={},
                profile_name="review",
            )
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        agents = audit["cost"]["agents"]
        assert len(agents) == 2  # 1 dev + 1 review

        dev_entry = next(a for a in agents if a["role"] == "dev")
        assert dev_entry["profile"] == "dev"
        assert dev_entry["cost_usd"] == 0.30
        assert "duration_seconds" in dev_entry
        assert dev_entry["duration_seconds"] is not None
        assert dev_entry["duration_seconds"] >= 0

        review_entry = next(a for a in agents if a["role"] == "review")
        assert review_entry["profile"] == "review"
        assert review_entry["cost_usd"] == 0.20
        assert review_entry["duration_seconds"] is not None
        assert review_entry["duration_seconds"] >= 0


# ── Structured logging tests ──────────────────────────────────────────


class TestVerboseFlagEnablesToolLines:
    """Tool activity is printed in VERBOSE mode and suppressed in PROGRESS mode."""

    def test_verbose_prints_tool_lines(self, capsys):
        import theforge.runner as runner_mod

        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            # Simulate a tool_use assistant event
            tool_event = (
                '{"type": "assistant", "message": {"content": '
                '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
            )
            runner_mod._process_stream_event(tool_event, "test-label")
            captured = capsys.readouterr()
            assert "↳ Read" in captured.err
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)

    def test_progress_suppresses_tool_lines(self, capsys):
        import theforge.runner as runner_mod

        runner_mod.set_log_level(LogLevel.PROGRESS)
        tool_event = (
            '{"type": "assistant", "message": {"content": '
            '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
        )
        runner_mod._process_stream_event(tool_event, "test-label")
        captured = capsys.readouterr()
        assert "↳ Read" not in captured.err


class TestProgressShowsPhaseTransitions:
    """Phase transition lines always appear at PROGRESS level."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_phase_transitions_always_shown(
        self, mock_shell, mock_agent, mock_pool, tmp_path, capsys
    ):
        import theforge.coordinator as coord_mod
        import theforge.runner as runner_mod

        # Ensure we are at PROGRESS level
        coord_mod.set_log_level(LogLevel.PROGRESS)
        runner_mod.set_log_level(LogLevel.PROGRESS)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        captured = capsys.readouterr()
        # Phase transitions must appear even at PROGRESS level
        assert "▸ WORKSPACE" in captured.err
        assert "▸ DEV" in captured.err
        assert "▸ VALIDATE" in captured.err
        assert "▸ REVIEW" in captured.err
        assert "✓ DONE" in captured.err


class TestCampaignSpecHeaderPrinted:
    """Campaign emits [N/total] slug header before each spec."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_spec_header_emitted(self, mock_shell, mock_agent, mock_pool, tmp_path, capsys):
        import yaml as _yaml

        from theforge.sprint import run_sprint

        # Write a minimal forge.yaml
        config = _make_config(tmp_path)

        # Write a spec file with frontmatter
        spec_path = tmp_path / "test-spec.md"
        spec_path.write_text(
            "---\nname: Test Spec\nslug: test-spec\n---\n# Test\n",
            encoding="utf-8",
        )

        # Write a campaign manifest
        manifest_path = tmp_path / "campaign.yaml"
        manifest_path.write_text(
            _yaml.dump(
                {
                    "name": "test campaign",
                    "budget_usd": 10.0,
                    "specs": ["test-spec.md"],
                }
            ),
            encoding="utf-8",
        )

        workspace = tmp_path / "test-spec"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_sprint(config, manifest_path)

        captured = capsys.readouterr()
        # Header banner for spec [1/1] must appear
        assert "[1/1]" in captured.err
        assert "test-spec" in captured.err


class TestCoordinatorAuditFindings:
    """Test that review findings are included in audit log."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_review_findings_in_audit(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Audit reviews[] entries include findings list with severity, file, line, description."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert len(audit["reviews"]) == 2

        # First review has findings
        first_rev = audit["reviews"][0]
        assert "findings" in first_rev
        assert first_rev["p1_count"] == 1
        assert len(first_rev["findings"]) == 1
        finding = first_rev["findings"][0]
        assert finding["severity"] == "P1"
        assert finding["file"] == "src/foo.py"
        assert finding["line"] == 10
        assert "Off by one" in finding["description"]

        # Second review (APPROVE) has empty findings
        second_rev = audit["reviews"][1]
        assert "findings" in second_rev
        assert second_rev["findings"] == []
        assert second_rev["p1_count"] == 0

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_approve_review_has_empty_findings(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """APPROVE review in audit has findings: [] (not missing key)."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        rev = audit["reviews"][0]
        assert "findings" in rev
        assert rev["findings"] == []


class TestCoordinatorReviewCycleMetadata:
    """Test that review cycle metadata is populated correctly."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_metadata_present_on_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Audit metadata is populated after successful pool merge."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        result = run_task(config, task)

        assert len(result.state.review_cycle_metadata) == 1
        meta = result.state.review_cycle_metadata[0]
        assert meta.pool_models == ["r1", "r2"]
        assert meta.successful == ["r1", "r2"]
        assert meta.failed == []
        assert meta.synthesized is False

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_metadata_present_on_all_reviewers_fail(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Metadata is populated even when all reviewers fail (P2 fix)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=False, output="FAIL", profile_name="r1"),
            _make_agent_result(success=False, output="FAIL", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.phase == Phase.ESCALATE
        # Metadata must be present even though we escalated early
        assert len(result.state.review_cycle_metadata) == 1
        meta = result.state.review_cycle_metadata[0]
        assert meta.failed == ["r1", "r2"]
        assert meta.successful == []
        assert meta.synthesized is False

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_audit_log_contains_pool_metadata(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """generate_audit_log includes pool_models, synthesized, successful, failed."""

        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert len(audit["reviews"]) == 1
        rev = audit["reviews"][0]
        assert rev["cycle"] == 1
        assert rev["pool_models"] == ["r1", "r2"]
        assert rev["successful"] == ["r1", "r2"]
        assert rev["failed"] == []
        assert rev["synthesized"] is False
        assert rev["verdict"] == "APPROVE"


# ── Parse Retry Tests ─────────────────────────────────────────────────


PARSE_ERROR_OUTPUT = "this is not valid yaml: {{{ completely broken"

SCHEMA_ERROR_OUTPUT = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
```
"""
# Missing spec_compliance and test_coverage → schema errors


class TestReviewParseRetry:
    """Tests for reviewer retry on parse/schema errors (spec: review-parse-retry)."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_parse_error_does_not_increment_cycle(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Parse error on first review attempt → retry → APPROVE: review_cycle == 1."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=PARSE_ERROR_OUTPUT, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Parse error did NOT increment review_cycle — only the valid APPROVE did
        assert result.state.review_cycle == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_parse_error_then_request_changes(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Parse error then real REQUEST_CHANGES → cycle increments once, DEV retried."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(),  # dev cycle 1
            _make_agent_result(),  # dev cycle 2 (after REQUEST_CHANGES)
        )

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                # Parse error — triggers retry, NOT a cycle
                return [
                    _make_agent_result(
                        success=True,
                        output=PARSE_ERROR_OUTPUT,
                        profile_name="review",
                        cost_usd=0.1,
                    )
                ]
            if call_count["pool"] == 2:
                # Real REQUEST_CHANGES — increments cycle to 1, DEV reruns
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        profile_name="review",
                        cost_usd=0.1,
                    )
                ]
            # Cycle 2: APPROVE
            return [
                _make_agent_result(
                    success=True, output=APPROVE_REVIEW, profile_name="review", cost_usd=0.1
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # review_cycle == 2: cycle 1 (parse error + REQUEST_CHANGES), cycle 2 (APPROVE)
        assert result.state.review_cycle == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_all_parse_retries_exhausted(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """All parse retries exhausted → ESCALATE with 'unreliable' in message."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        # Always return parse errors (default max_parse_retries=2 → 3 total attempts)
        # Use low cost_usd to avoid hitting the review budget before exhausting retries
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=PARSE_ERROR_OUTPUT, profile_name="review", cost_usd=0.1
            )
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "unreliable" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_parse_retry_count_in_audit(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Audit log records parse_retries: 1 when one retry occurred."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=PARSE_ERROR_OUTPUT, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert result.success is True
        assert len(audit["reviews"]) == 1
        assert audit["reviews"][0]["parse_retries"] == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_schema_error_also_retried(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Schema validation error (not just YAML parse error) also triggers retry."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                # Valid YAML but invalid schema (missing spec_compliance, test_coverage)
                return [
                    _make_agent_result(
                        success=True, output=SCHEMA_ERROR_OUTPUT, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Schema error triggered retry — only 1 review cycle
        assert result.state.review_cycle == 1
        # parse_retries tracked in metadata
        assert result.state.review_cycle_metadata[0].parse_retries == 1


# ── Tests: run_review_only ────────────────────────────────────────────


class TestReviewOnly:
    """Tests for run_review_only — skips WORKSPACE/PREFLIGHT/DEV/VALIDATE."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_util._run_shell")
    def test_review_only_approve(self, mock_shell, mock_pool, tmp_path):
        """APPROVE → success, phase=DONE, dev_iteration=0."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")  # git diff returns empty
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_review_only(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.dev_iteration == 0
        assert result.state.review_cycle == 1
        assert len(result.state.review_results) == 1
        assert result.state.review_results[0].verdict == "APPROVE"
        # No dev agents were invoked
        assert len(result.state.dev_results) == 0

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_util._run_shell")
    def test_review_only_request_changes(self, mock_shell, mock_pool, tmp_path):
        """REQUEST_CHANGES → failure, phase=ESCALATE, findings in result."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_review_only(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 0
        # Findings are present in review results
        assert len(result.state.review_results) == 1
        assert result.state.review_results[0].verdict == "REQUEST_CHANGES"
        assert len(result.state.review_results[0].findings) > 0

    def test_review_only_missing_worktree(self, tmp_path):
        """Missing workspace_path → error result with clear message."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        missing = tmp_path / "does-not-exist"

        result = run_review_only(config, task, missing)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Worktree not found" in result.message
        assert "forge run" in result.message
        assert str(missing) in result.message

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_util._run_shell")
    def test_review_only_no_dev_cycles(self, mock_shell, mock_pool, tmp_path):
        """dev_iteration == 0 in all cases (APPROVE and REQUEST_CHANGES)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")

        for review_output in [APPROVE_REVIEW, REQUEST_CHANGES_REVIEW]:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=review_output, profile_name="review")
            ]
            result = run_review_only(config, task, workspace)
            assert result.state.dev_iteration == 0
            assert len(result.state.dev_results) == 0


# ── run_from_review tests ─────────────────────────────────────────────


class TestRunFromReview:
    """Tests for the run_from_review() full iteration loop entry point."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_util._run_shell")
    def test_run_from_review_approve_merges(self, mock_shell, mock_pool, tmp_path):
        """APPROVE on first review → DONE; auto_merge triggers merge attempt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # _run_shell: git diff (OK), git status --porcelain (clean), merge safety checks
        def shell_side_effect(cmd, cwd, **kwargs):
            if "git branch --list" in cmd:
                return (True, "main")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git log" in cmd:
                return (True, "abc123 feat: something")
            if "git checkout" in cmd or "git merge" in cmd or "git worktree" in cmd:
                return (True, "OK")
            return (True, "")

        mock_shell.side_effect = shell_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, auto_merge=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        assert result.state.dev_iteration == 0
        # No dev agent invoked
        assert len(result.state.dev_results) == 0
        # auto_merge attempted
        assert result.merge is not None
        assert result.merge["attempted"] is True

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_run_from_review_request_changes_iterates(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """REQUEST_CHANGES → dev cycle → re-review → APPROVE → DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Fixed.")

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 2
        # One dev iteration ran
        assert result.state.dev_iteration == 1
        assert len(result.state.dev_results) == 1
        # preflight was skipped
        assert result.state.preflight_verdict == "SKIPPED"
        assert result.state.preflight_result is None

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_run_from_review_exhausts_cycles(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """REQUEST_CHANGES × max_review_cycles → ESCALATE."""
        config = _make_config(tmp_path)  # max_review_cycles=2
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Attempted fix.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()
        assert result.state.review_cycle == config.retry.max_review_cycles

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_util._run_shell")
    def test_run_from_review_skips_preflight(self, mock_shell, mock_pool, tmp_path):
        """preflight_verdict is 'SKIPPED' and no preflight agent is ever invoked."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.state.preflight_verdict == "SKIPPED"
        assert result.state.preflight_result is None
        # run_agent was never called (no preflight, no dev)
        # We can verify by checking no dev results
        assert len(result.state.dev_results) == 0

        # Spec requires: audit log records preflight_verdict as 'SKIPPED' with cost 0.0

        audit = generate_audit_log(config, task, result)
        assert audit["preflight"] is not None
        assert audit["preflight"]["verdict"] == "SKIPPED"
        assert audit["preflight"]["cost_usd"] == 0.0


# ── Dev notes / review_to_dev_handoff coordinator tests ───────────────


class TestCoordinatorDevNotes:
    """Test that coordinator reads dev_notes from handoff.yaml and passes to review prompt."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_dev_notes_passed_to_review_prompt(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Coordinator reads dev_notes from handoff.yaml and injects into review prompt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Write handoff.yaml with dev_notes
        handoff = {
            "gate_decision": "PASS",
            "validation": {"make_fmt": {"status": "PASS"}},
            "scope_completed": ["test item"],
            "dev_notes": "I deviated from spec because it was better.",
        }
        (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                # handoff.yaml already exists — gate re-writes it
                (Path(cwd) / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )

        captured_prompts: list[str] = []

        def pool_side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            if isinstance(prompt, list):
                captured_prompts.extend(prompt)
            elif isinstance(prompt, str):
                captured_prompts.append(prompt)
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # At least one review prompt should contain the dev_notes content
        assert any("I deviated from spec because it was better." in p for p in captured_prompts), (
            f"dev_notes not found in any review prompt. Captured: {captured_prompts[:1]}"
        )
        assert any("## Developer Notes" in p for p in captured_prompts)

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_review_to_dev_handoff_used_on_request_changes(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Coordinator uses review_to_dev_handoff (not findings_to_markdown) on REQUEST_CHANGES."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # last_review_findings should contain the richer format from review_to_dev_handoff
        assert result.state.last_review_findings is not None
        assert "## Review Summary" in result.state.last_review_findings


# ── Dev handoff validation tests ──────────────────────────────────────


class TestCoordinatorDevHandoffValidation:
    """Test that coordinator validates structured dev handoff after gate passes."""

    def _make_structured_handoff(self, workspace: Path, dev_notes: str) -> None:
        """Write handoff.yaml with structured dev_notes."""
        handoff = {
            "gate_decision": "PASS",
            "validation": {"make_fmt": {"status": "PASS"}},
            "scope_completed": ["test item"],
            "dev_notes": dev_notes,
        }
        (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_valid_structured_handoff_passes_to_review(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Valid structured dev handoff is formatted and passed to reviewer."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        structured_notes = (
            'summary: "Implemented feature X with full test coverage."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat(x): implement feature X"\n'
            "acceptance_criteria:\n"
            '  - criterion: "Feature X works"\n'
            "    status: MET\n"
            '    notes: "Tested in test_x.py"\n'
            "spec_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                self._make_structured_handoff(Path(cwd), structured_notes)
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )

        captured_prompts: list[str] = []

        def pool_side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            if isinstance(prompt, list):
                captured_prompts.extend(prompt)
            elif isinstance(prompt, str):
                captured_prompts.append(prompt)
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # Structured summary should appear in review prompt
        assert any("Implemented feature X" in p for p in captured_prompts)

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_invalid_handoff_triggers_retry(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Invalid dev handoff triggers a handoff fix retry."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # First handoff is malformed (missing required fields)
        bad_notes = "just some unstructured text"
        good_notes = (
            'summary: "Implemented the thing."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "It works"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "spec_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        call_idx = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                self._make_structured_handoff(Path(cwd), bad_notes)
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        # After handoff fix agent runs, rewrite handoff with valid notes
        def agent_side_effect(**kwargs):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                # preflight
                return _PREFLIGHT_RESULT
            if call_idx["n"] == 2:
                # dev agent
                return _make_agent_result(success=True, output="Implemented.")
            # handoff fix agent — write valid handoff
            self._make_structured_handoff(workspace, good_notes)
            return _make_agent_result(success=True, output="Fixed handoff.")

        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Should have called agent 3 times: preflight, dev, handoff fix
        assert call_idx["n"] == 3

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_invalid_handoff_proceeds_after_max_retries(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Invalid dev handoff proceeds to review after max retries exhausted."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        bad_notes = "just some unstructured text"

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                self._make_structured_handoff(Path(cwd), bad_notes)
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        call_idx = {"n": 0}

        def agent_side_effect(**kwargs):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                return _PREFLIGHT_RESULT
            # All subsequent calls: dev agent and failed handoff fixes
            return _make_agent_result(success=True, output="Done.")

        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Should still succeed (proceeds to review even with invalid handoff)
        assert result.success is True
        # preflight + dev + max_handoff_retries (2 by default)
        assert call_idx["n"] == 4

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_missing_dev_notes_triggers_retry(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """PASS gate with no dev_notes triggers the handoff retry path."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        good_notes = (
            'summary: "Implemented the thing."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "It works"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "spec_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        def _write_handoff_no_dev_notes(ws: Path) -> None:
            handoff = {
                "gate_decision": "PASS",
                "validation": {"make_fmt": {"status": "PASS"}},
                "scope_completed": ["test item"],
            }
            (ws / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                # Write handoff.yaml WITHOUT dev_notes
                _write_handoff_no_dev_notes(Path(cwd))
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        call_idx = {"n": 0}

        def agent_side_effect(**kwargs):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                return _PREFLIGHT_RESULT
            if call_idx["n"] == 2:
                # dev agent
                return _make_agent_result(success=True, output="Implemented.")
            # handoff fix agent — write valid handoff with dev_notes
            self._make_structured_handoff(workspace, good_notes)
            return _make_agent_result(success=True, output="Fixed handoff.")

        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # preflight + dev + handoff fix = 3 agent calls
        assert call_idx["n"] == 3


# ── _has_persistent_p1 unit tests ────────────────────────────────────


class TestHasPersistentP1:
    """Unit tests for _has_persistent_p1 in coord_preflight."""

    def _make_finding(self, severity: str, description: str, file: str = "coordinator.py"):
        from theforge.review import ReviewFinding

        return ReviewFinding(
            severity=severity,
            file=file,
            line=None,
            description=description,
            suggestion="fix it",
        )

    def test_same_description_different_files_returns_true(self):
        from theforge.coord_preflight import _has_persistent_p1

        curr = [
            self._make_finding(
                "P1", "coordinator routing ignores extend path", file="coordinator.py"
            )
        ]
        prev = [
            self._make_finding("P1", "coordinator routing ignores extend path", file="task.py")
        ]
        assert _has_persistent_p1(curr, prev) is True

    def test_same_description_same_files_returns_true(self):
        from theforge.coord_preflight import _has_persistent_p1

        curr = [
            self._make_finding(
                "P1", "coordinator routing ignores extend path", file="coordinator.py"
            )
        ]
        prev = [
            self._make_finding(
                "P1", "coordinator routing ignores extend path", file="coordinator.py"
            )
        ]
        assert _has_persistent_p1(curr, prev) is True

    def test_different_descriptions_returns_false(self):
        from theforge.coord_preflight import _has_persistent_p1

        curr = [self._make_finding("P1", "missing null check on session id")]
        prev = [self._make_finding("P1", "wrong HTTP method used in upload endpoint")]
        assert _has_persistent_p1(curr, prev) is False

    def test_empty_current_findings_returns_false(self):
        from theforge.coord_preflight import _has_persistent_p1

        prev = [self._make_finding("P1", "coordinator routing ignores extend path")]
        assert _has_persistent_p1([], prev) is False

    def test_empty_previous_findings_returns_false(self):
        from theforge.coord_preflight import _has_persistent_p1

        curr = [self._make_finding("P1", "coordinator routing ignores extend path")]
        assert _has_persistent_p1(curr, []) is False


# ── Plan agent review tests ──────────────────────────────────────────


PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""

PLAN_AGENT_REJECT = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "Plan references nonexistent function parse_config()"
    suggestion: "Use load_config() from config.py instead"
```
"""


def _make_plan_agent_review_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config with PLAN and plan_agent_review enabled."""
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
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
        plan_agent_review=PlanAgentReviewConfig(enabled=True, cli="claude", model="sonnet"),
        log=LogConfig(enabled=False),
    )


class TestPlanAgentReview:
    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_agent_review_approve(
        self, mock_shell, mock_agent, mock_pool, mock_human_review, tmp_path
    ):
        """Agent returns APPROVE, pipeline continues to DEV."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.10),
            _make_agent_result(
                success=True, output=PLAN_AGENT_APPROVE, cost_usd=0.08, profile_name="plan-review"
            ),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_review_mode == "agent"
        assert len(result.state.plan_review_results) == 1
        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["reviewer"] == "agent"
        assert audit["plan_review"]["decision"] == "approve"
        assert audit["plan_review"]["cost_usd"] == pytest.approx(0.08)

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_agent_review_reject_then_approve(
        self, mock_shell, mock_agent, mock_pool, mock_human_review, tmp_path
    ):
        """First review REJECT, plan regenerated, second review APPROVE."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(
                success=True, output=PLAN_AGENT_REJECT, cost_usd=0.08, profile_name="plan-review"
            ),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(
                success=True, output=PLAN_AGENT_APPROVE, cost_usd=0.08, profile_name="plan-review"
            ),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2
        assert len(result.state.plan_review_results) == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_agent_review_double_reject_escalates(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Two REJECTs, run escalates with findings."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(
                success=True, output=PLAN_AGENT_REJECT, cost_usd=0.08, profile_name="plan-review"
            ),
            _make_agent_result(success=True, output="# Plan\n\nStill bad.", cost_usd=0.12),
            _make_agent_result(
                success=True, output=PLAN_AGENT_REJECT, cost_usd=0.08, profile_name="plan-review"
            ),
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "rejected" in result.message.lower()
        assert result.state.plan_regen_count > 0
        mock_pool.assert_not_called()

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_agent_review_disabled_by_default(
        self, mock_shell, mock_agent, mock_pool, mock_human_review, tmp_path
    ):
        """Config without plan_agent_review section — PLAN_REVIEW is skipped."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision is None

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_agent_review_skipped_on_plan_injection(
        self, mock_shell, mock_agent, mock_pool, mock_human_review, tmp_path
    ):
        """`--plan` flag skips agent review."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Injected plan\n\nUse this.", encoding="utf-8")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        assert result.state.plan_review_decision is None
        assert len(result.state.plan_review_results) == 0

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_agent_review_parse_failure(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Agent produces garbage — treated as REJECT."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="I think the plan looks okay!", cost_usd=0.08),
            _make_agent_result(success=True, output="# Plan\n\nBetter plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Still looks fine to me", cost_usd=0.08),
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        mock_pool.assert_not_called()

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_agent_review_cost_in_audit(
        self, mock_shell, mock_agent, mock_pool, mock_human_review, tmp_path
    ):
        """Plan review cost appears in audit log."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.10),
            _make_agent_result(
                success=True, output=PLAN_AGENT_APPROVE, cost_usd=0.25, profile_name="plan-review"
            ),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)
        audit = generate_audit_log(config, task, result)

        assert audit["plan_review"]["cost_usd"] == pytest.approx(0.25)
        assert result.state.total_plan_review_cost == pytest.approx(0.25)
        assert result.state.total_cost >= 0.25

    @patch("theforge.coordinator._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_plan_regen_receives_rejection_findings(
        self, mock_shell, mock_agent, mock_pool, mock_human_review, tmp_path
    ):
        """Regenerated plan prompt includes rejection findings from first review."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(
                success=True, output=PLAN_AGENT_REJECT, cost_usd=0.08, profile_name="plan-review"
            ),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(
                success=True, output=PLAN_AGENT_APPROVE, cost_usd=0.08, profile_name="plan-review"
            ),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        # The plan regen call (call index 3) should contain the rejection findings
        regen_call = mock_agent.call_args_list[3]
        regen_prompt = regen_call.kwargs.get(
            "prompt", regen_call.args[0] if regen_call.args else ""
        )
        assert "Previous Plan Review Findings" in regen_prompt
        assert "parse_config()" in regen_prompt


# ── Structured logging tests ──────────────────────────────────────────
