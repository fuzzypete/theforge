"""Tests for gate execution helpers: stale handoff, gate decision fallback,
dirty worktree detection, and zero-change guard."""

from __future__ import annotations

import dataclasses
import signal
import subprocess
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest
import yaml
from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator import util as _cu
from theforge.coordinator.engine import run_task
from theforge.coordinator.gate import _read_gate_decision
from theforge.coordinator.state import Phase
from theforge.task import TaskStory

# ── Local helpers ─────────────────────────────────────────────────────


def _make_exit_code_config(tmp_path: Path) -> ForgeConfig:
    """Config with exit-code gate mode (empty handoff_file)."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(
            gate_command="pytest {pytest_target} -q",
            handoff_file="",
            gate_decision_key="",
            gate_timeout=120,
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _shell_exit_code(pass_on_call: int | None = None, gate_marker: str = "pytest"):
    """Shell side_effect for exit-code gate mode.

    If pass_on_call is None, all gate calls pass.
    If pass_on_call is N, gate fails until the Nth call.
    gate_marker: string to detect which shell command is the gate command.
    """
    gate_idx = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if gate_marker in cmd:
            gate_idx["n"] += 1
            if pass_on_call is not None and gate_idx["n"] < pass_on_call:
                return (False, "FAILED: 1 error")
            return (True, "passed")
        if "git status --porcelain" in cmd:
            return (True, "")
        stale_resp = _handle_stale_check_cmd(cmd)
        if stale_resp is not None:
            return stale_resp
        return (True, "OK")

    return side_effect


def _make_task(tmp_path: Path) -> TaskStory:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


def _make_task_with_gate_override(tmp_path: Path, gate_override: str | None) -> TaskStory:
    """Create a test task with a gate_override set."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
        gate_override=gate_override,
    )


# ── Stale handoff tests ──────────────────────────────────────────────


class TestCoordinatorStaleHandoff:
    """Test that stale handoff.yaml is deleted before running the gate."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_stale_handoff_not_reused(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """A PASS from a prior gate run must not leak through on gate failure."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Pre-plant a stale PASS handoff from a prior run
        _write_handoff(workspace, "PASS")

        call_count = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            call_count["n"] += 1
            if "mkdir" in cmd:
                return (True, "OK")
            # Gate command fails (e.g. tests fail)
            if "gate" in cmd.lower() or "pytest" in cmd.lower():
                return (False, "FAIL: 1 test failed")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Gate failed and stale handoff was deleted → should escalate, not PASS
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Gate" in result.message or "gate" in result.message


class TestGateDecisionFallback:
    def test_prefers_configured_handoff_path(self, tmp_path):
        config = dataclasses.replace(
            _make_config(tmp_path),
            validation=dataclasses.replace(
                _make_config(tmp_path).validation,
                handoff_file=".forge/handoff.yaml",
            ),
        )
        workspace = tmp_path / "test-task"
        (workspace / ".forge").mkdir(parents=True)
        (workspace / ".forge" / "handoff.yaml").write_text(
            yaml.dump({"gate_decision": "PASS"}), encoding="utf-8"
        )
        (workspace / "handoff.yaml").write_text(
            yaml.dump({"gate_decision": "FAIL"}), encoding="utf-8"
        )

        decision, error = _read_gate_decision(config, workspace)

        assert error is None
        assert decision == "PASS"

    def test_falls_back_to_legacy_root_handoff(self, tmp_path):
        config = dataclasses.replace(
            _make_config(tmp_path),
            validation=dataclasses.replace(
                _make_config(tmp_path).validation,
                handoff_file=".forge/handoff.yaml",
            ),
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        (workspace / "handoff.yaml").write_text(
            yaml.dump({"gate_decision": "PASS"}), encoding="utf-8"
        )

        decision, error = _read_gate_decision(config, workspace)

        assert error is None
        assert decision == "PASS"


# ── Dirty worktree tests ─────────────────────────────────────────────


class TestCoordinatorDirtyWorktree:
    """Test that the coordinator catches uncommitted changes after gate PASS."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    @patch("theforge.coordinator.util._run_shell")
    def test_dirty_worktree_auto_commits_no_retry(
        self, mock_shell, mock_deindex, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dirty worktree after gate PASS → coordinator auto-commits, no agent retry."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        shell_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            shell_cmds.append(cmd)
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py\n M src/theforge/config.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with (
            patch("theforge.coordinator.validate_phase.subprocess.run") as mock_subprocess,
            patch("theforge.runners.sandbox._sandbox_available", return_value=False),
        ):
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert mock_agent.call_count == 1
        assert mock_deindex.call_count == 2
        assert mock_deindex.call_args_list[0].args == (workspace,)
        assert mock_deindex.call_args_list[1].args == (workspace,)
        assert any("git add" in c for c in shell_cmds)
        assert any(
            c[0][0] == ["git", "commit", "-m", mock.ANY] for c in mock_subprocess.call_args_list
        )
        assert mock_subprocess.call_args[0][0] == ["git", "commit", "-m", mock.ANY]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    @patch("theforge.coordinator.util._run_shell")
    def test_dirty_worktree_deindexes_before_status_and_after_git_add(
        self, mock_shell, mock_deindex, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """PASS path scrubs forge artifacts before status and again after staging dirty files."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        shell_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            shell_cmds.append(cmd)
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("theforge.coordinator.validate_phase.subprocess.run"):
            result = run_task(config, task)

        assert result.success is True
        assert mock_deindex.call_count == 2
        assert mock_deindex.call_args_list[0].args == (workspace,)
        assert mock_deindex.call_args_list[1].args == (workspace,)
        assert any("git status --porcelain" in c for c in shell_cmds)
        assert any("git add -A" in c for c in shell_cmds)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dirty_worktree_auto_commits_even_at_max_iterations(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dirty worktree at max iterations → auto-commit succeeds, no escalation."""
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
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(max_dev_iterations=1, max_review_cycles=2),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with (
            patch("theforge.coordinator.validate_phase.subprocess.run") as mock_subprocess,
            patch("theforge.runners.sandbox._sandbox_available", return_value=False),
        ):
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert any(
            c[0][0] == ["git", "commit", "-m", mock.ANY] for c in mock_subprocess.call_args_list
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_handoff_file_not_flagged_as_dirty(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """handoff.yaml in git status output is excluded from dirty check."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # Only handoff.yaml is dirty — that's expected
                return (True, "?? handoff.yaml")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # handoff.yaml is filtered out → clean worktree → proceeds to review
        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_handoff_dirty_worktree_unchanged(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Regression guard: handoff mode still filters handoff.yaml from dirty check."""
        config = _make_config(tmp_path)  # handoff_file="handoff.yaml"
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # handoff.yaml is the only dirty file — should be filtered out
                return (True, "?? handoff.yaml")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # handoff.yaml filtered out → worktree clean → proceeds to DONE
        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dirty_files_auto_committed(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dirty files after gate PASS → coordinator auto-commits, no retry."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskStory(
            name="Test Task",
            story_path=spec,
            slug="test-task",
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        shell_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            shell_cmds.append(cmd)
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/ideate.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with (
            patch("theforge.coordinator.validate_phase.subprocess.run") as mock_subprocess,
            patch("theforge.runners.sandbox._sandbox_available", return_value=False),
        ):
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert mock_agent.call_count == 1
        assert any("git add" in c for c in shell_cmds)
        assert any(
            c[0][0] == ["git", "commit", "-m", mock.ANY] for c in mock_subprocess.call_args_list
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_untracked_file_auto_committed(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Tracked + untracked files → coordinator auto-commits both."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskStory(
            name="Test Task",
            story_path=spec,
            slug="test-task",
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        shell_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            shell_cmds.append(cmd)
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/ideate.py\n?? new_scratch.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("theforge.coordinator.validate_phase.subprocess.run"):
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert mock_agent.call_count == 1


# ── Zero-change guard tests ──────────────────────────────────────────


class TestDevZeroChangeGuard:
    """Dev retry that produces no changes should escalate, not re-review identical code."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_retry_no_changes_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev iteration 2 produces no diff and no dirty files → ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")  # no dirty files
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        # Preflight → dev (iter 1) → review REQUEST_CHANGES → dev (iter 2, no changes) → escalate
        dev_result = _make_agent_result(success=True, output="Done.")
        review_rc = _make_agent_result(
            success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
        )
        review_approve = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="review"
        )

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = dev_result
        # First review: REQUEST_CHANGES, second would be APPROVE but should never be reached
        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [review_rc]
            return [review_approve]

        mock_pool.side_effect = pool_side_effect

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0] == "git":
                if "rev-parse" in cmd:
                    result = mock.Mock()
                    result.returncode = 0
                    result.stdout = b"abc123"
                    return result
                if "diff" in cmd and "--quiet" in cmd:
                    result = mock.Mock()
                    result.returncode = 0  # no diff
                    return result
                if "status" in cmd and "--porcelain" in cmd:
                    result = mock.Mock()
                    result.returncode = 0
                    result.stdout = b""  # no dirty files
                    return result
                if "commit" in cmd:
                    result = mock.Mock()
                    result.returncode = 0
                    return result
            result = mock.Mock()
            result.returncode = 0
            result.stdout = b""
            return result

        with patch(
            "theforge.coordinator.validate_phase.subprocess.run",
            side_effect=subprocess_side_effect,
        ):
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "no changes" in result.message.lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_retry_with_dirty_files_proceeds(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev iteration 2 has dirty files (uncommitted work) → does NOT escalate."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/config.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        dev_result = _make_agent_result(success=True, output="Done.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = dev_result
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0] == "git":
                if "rev-parse" in cmd:
                    result = mock.Mock()
                    result.returncode = 0
                    result.stdout = b"abc123"
                    return result
                if "diff" in cmd and "--quiet" in cmd:
                    result = mock.Mock()
                    result.returncode = 0  # no committed diff
                    return result
                if "status" in cmd and "--porcelain" in cmd:
                    result = mock.Mock()
                    result.returncode = 0
                    result.stdout = b" M src/theforge/config.py"  # dirty files exist
                    return result
                if "commit" in cmd:
                    result = mock.Mock()
                    result.returncode = 0
                    return result
            result = mock.Mock()
            result.returncode = 0
            result.stdout = b""
            return result

        with patch(
            "theforge.coordinator.validate_phase.subprocess.run",
            side_effect=subprocess_side_effect,
        ):
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_retry_no_changes_does_not_escalate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Gate failure retry with no code changes should NOT escalate (not a review retry)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        gate_calls = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                gate_calls["n"] += 1
                if gate_calls["n"] == 1:
                    # First gate: FAIL → triggers dev retry
                    _write_handoff(Path(cwd), "FAIL")
                    return (True, "FAIL")
                # Second gate: PASS
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "abc1234 feat: implement")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0] == "git":
                if "rev-parse" in cmd:
                    r = mock.Mock()
                    r.returncode = 0
                    r.stdout = b"abc123"
                    return r
                if "diff" in cmd and "--quiet" in cmd:
                    r = mock.Mock()
                    r.returncode = 0  # no diff
                    return r
                if "status" in cmd and "--porcelain" in cmd:
                    r = mock.Mock()
                    r.returncode = 0
                    r.stdout = b""
                    return r
            r = mock.Mock()
            r.returncode = 0
            r.stdout = b""
            return r

        with patch(
            "theforge.coordinator.validate_phase.subprocess.run",
            side_effect=subprocess_side_effect,
        ):
            result = run_task(config, task)

        # Should complete successfully — gate retry is not a review retry
        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_post_review_gate_retry_no_changes_does_not_escalate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """REQUEST_CHANGES → dev retry (has diff) → gate fail → dev retry (no new diff) → OK.

        The review-driven retry produces changes (guard passes), then gate fails.
        The gate-fail retry produces no additional changes — that's legitimate
        and should NOT trigger the zero-change guard.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        gate_calls = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                gate_calls["n"] += 1
                if gate_calls["n"] == 2:
                    # Second gate (after review retry dev): FAIL → triggers gate retry
                    _write_handoff(Path(cwd), "FAIL")
                    return (True, "FAIL")
                # All others: PASS
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "abc1234 feat: implement")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        dev_result = _make_agent_result(success=True, output="Done.")
        review_rc = _make_agent_result(
            success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
        )
        review_approve = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="review"
        )

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [dev_result, dev_result, dev_result]

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [review_rc]
            return [review_approve]

        mock_pool.side_effect = pool_side_effect

        dev_trace = {"n": 0}

        def subprocess_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0] == "git":
                if "rev-parse" in cmd:
                    dev_trace["n"] += 1
                    r = mock.Mock()
                    r.returncode = 0
                    r.stdout = f"commit{dev_trace['n']}".encode()
                    return r
                if "diff" in cmd and "--quiet" in cmd:
                    r = mock.Mock()
                    # Review-driven retry (trace 2) has changes; gate retry (trace 3) does not
                    r.returncode = 1 if dev_trace["n"] <= 2 else 0
                    return r
                if "status" in cmd and "--porcelain" in cmd:
                    r = mock.Mock()
                    r.returncode = 0
                    r.stdout = b""
                    return r
            r = mock.Mock()
            r.returncode = 0
            r.stdout = b""
            return r

        with patch(
            "theforge.coordinator.validate_phase.subprocess.run",
            side_effect=subprocess_side_effect,
        ):
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE


def test_run_shell_kills_process_group_on_timeout(tmp_path):
    proc = mock.Mock()
    proc.pid = 4321
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="pytest -n auto", timeout=1)

    with (
        patch("theforge.coordinator.util.subprocess.Popen", return_value=proc) as mock_popen,
        patch("theforge.coordinator.util.os.getpgid", return_value=9876) as mock_getpgid,
        patch("theforge.coordinator.util.os.killpg") as mock_killpg,
    ):
        ok, output = _cu._run_shell("pytest -n auto --dist worksteal", tmp_path, timeout=1)

    assert ok is False
    assert output == "TIMEOUT after 1s: pytest -n auto --dist worksteal"
    mock_popen.assert_called_once()
    assert mock_popen.call_args.kwargs["start_new_session"] is True
    mock_getpgid.assert_called_once_with(4321)
    mock_killpg.assert_called_once_with(9876, signal.SIGKILL)
    proc.wait.assert_called_once_with()


def test_run_shell_timeout_ignores_missing_process_group(tmp_path):
    proc = mock.Mock()
    proc.pid = 4321
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="pytest -n auto", timeout=1)

    with (
        patch("theforge.coordinator.util.subprocess.Popen", return_value=proc),
        patch("theforge.coordinator.util.os.getpgid", side_effect=ProcessLookupError),
        patch("theforge.coordinator.util.os.killpg") as mock_killpg,
    ):
        ok, output = _cu._run_shell("pytest -n auto --dist worksteal", tmp_path, timeout=1)

    assert ok is False
    assert output == "TIMEOUT after 1s: pytest -n auto --dist worksteal"
    mock_killpg.assert_not_called()
    proc.wait.assert_called_once_with()


def test_run_shell_kills_process_group_on_keyboard_interrupt(tmp_path):
    proc = mock.MagicMock()
    proc.pid = 4321
    proc.communicate.side_effect = KeyboardInterrupt

    with (
        patch("theforge.coordinator.util.subprocess.Popen", return_value=proc),
        patch("theforge.coordinator.util.os.getpgid", return_value=9876) as mock_getpgid,
        patch("theforge.coordinator.util.os.killpg") as mock_killpg,
    ):
        with pytest.raises(KeyboardInterrupt):
            _cu._run_shell("pytest -n auto --dist worksteal", tmp_path, timeout=1)

    mock_getpgid.assert_called_once_with(4321)
    mock_killpg.assert_called_once_with(9876, signal.SIGKILL)
    proc.wait.assert_called_once_with()


def test_run_shell_keyboard_interrupt_ignores_missing_process_group(tmp_path):
    proc = mock.MagicMock()
    proc.pid = 4321
    proc.communicate.side_effect = KeyboardInterrupt

    with (
        patch("theforge.coordinator.util.subprocess.Popen", return_value=proc),
        patch("theforge.coordinator.util.os.getpgid", side_effect=ProcessLookupError),
        patch("theforge.coordinator.util.os.killpg") as mock_killpg,
    ):
        with pytest.raises(KeyboardInterrupt):
            _cu._run_shell("pytest -n auto --dist worksteal", tmp_path, timeout=1)

    mock_killpg.assert_not_called()
    proc.wait.assert_called_once_with()
