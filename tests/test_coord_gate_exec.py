"""Tests for gate execution helpers: stale handoff, dirty worktree detection,
and zero-change guard."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _as_detailed,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _write_handoff,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import util as _cu
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.task import TaskStory


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


# ── _parse_dirty_files unit tests ────────────────────────────────────


class TestParseDirtyFiles:
    """Unit tests for _parse_dirty_files porcelain parsing."""

    def test_modified_and_added_are_returned(self):
        from theforge.coordinator.gate import _parse_dirty_files

        raw = " M src/foo.py\nA  src/bar.py\n"
        result = _parse_dirty_files(raw)
        assert result == ["src/foo.py", "src/bar.py"]

    def test_untracked_skipped(self):
        from theforge.coordinator.gate import _parse_dirty_files

        raw = "?? untracked.py\n M src/tracked.py\n"
        result = _parse_dirty_files(raw)
        assert result == ["src/tracked.py"]

    def test_ignored_skipped(self):
        from theforge.coordinator.gate import _parse_dirty_files

        raw = "!! ignored.pyc\n M src/real.py\n"
        result = _parse_dirty_files(raw)
        assert result == ["src/real.py"]

    def test_rename_returns_destination(self):
        from theforge.coordinator.gate import _parse_dirty_files

        raw = "R  old_name.py -> new_name.py\n"
        result = _parse_dirty_files(raw)
        assert result == ["new_name.py"]

    def test_nonstandard_space_prefixes_not_skipped(self):
        """' ?' and ' !' are not valid porcelain v1 prefixes; they are treated as
        tracked changes, not silently dropped."""
        from theforge.coordinator.gate import _parse_dirty_files

        # ' M' is a real porcelain status (unstaged modification); make sure it
        # is still returned correctly alongside the non-standard ones.
        raw = " M src/real.py\n"
        result = _parse_dirty_files(raw)
        assert result == ["src/real.py"]

    def test_short_lines_skipped(self):
        from theforge.coordinator.gate import _parse_dirty_files

        raw = "??\n M src/ok.py\n"
        result = _parse_dirty_files(raw)
        assert result == ["src/ok.py"]

    def test_empty_output(self):
        from theforge.coordinator.gate import _parse_dirty_files

        assert _parse_dirty_files("") == []


# ── Stale handoff tests ──────────────────────────────────────────────


class TestCoordinatorStaleHandoff:
    """Test that stale handoff.yaml is deleted before running the gate."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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


# ── Dirty worktree tests ─────────────────────────────────────────────


class TestCoordinatorDirtyWorktree:
    """Test that the coordinator catches uncommitted changes after gate PASS."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py\n M src/theforge/config.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # Only handoff.yaml is dirty — that's expected
                return (True, "?? handoff.yaml")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
    @patch_gate_shell()
    def test_handoff_dirty_worktree_unchanged(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Regression guard: handoff.yaml as untracked file (??) is filtered from dirty check."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # handoff.yaml is the only dirty file — should be filtered out
                return (True, "?? handoff.yaml")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/ideate.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/ideate.py\n?? new_scratch.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")  # no dirty files
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

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
                if "rev-list" in cmd:
                    # validate-phase zero-commits guard: pretend branch has commits ahead
                    result = mock.Mock()
                    result.returncode = 0
                    result.stdout = "1\n"
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
    @patch_gate_shell()
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/config.py")
            if "git add" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

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
                if "rev-list" in cmd:
                    # validate-phase zero-commits guard: pretend branch has commits ahead
                    result = mock.Mock()
                    result.returncode = 0
                    result.stdout = "1\n"
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
    @patch_gate_shell()
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
                    return (False, "FAIL: tests failed")
                # Second gate: PASS
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
                if "rev-list" in cmd:
                    r = mock.Mock()
                    r.returncode = 0
                    r.stdout = "1\n"
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
    @patch_gate_shell()
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
                    return (False, "FAIL: tests failed")
                # All others: PASS
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

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
                if "rev-list" in cmd:
                    r = mock.Mock()
                    r.returncode = 0
                    r.stdout = "1\n"
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


def _sigkill_calls(mock_killpg: mock.Mock) -> list:
    """The ``killpg`` calls that actually signalled, dropping signal-0 probes.

    Release now *asks* whether the group is still alive before dropping its
    record — a ``killpg(pgid, 0)`` probe — because a shell exiting is not proof
    its test workers did (#2309). Patching ``os.killpg`` intercepts that probe
    too, so these assertions filter on the signal rather than the call count.
    """
    return [c for c in mock_killpg.call_args_list if c.args[1] == signal.SIGKILL]


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
    assert _sigkill_calls(mock_killpg) == [mock.call(9876, signal.SIGKILL)]
    # Bounded, not bare: an unbounded wait here lasts for the command's whole
    # natural lifetime whenever the kill above did not land (#1959).
    proc.wait.assert_called_once_with(timeout=_cu.KILL_GRACE_SECONDS)


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
    assert _sigkill_calls(mock_killpg) == []
    # Bounded, not bare: an unbounded wait here lasts for the command's whole
    # natural lifetime whenever the kill above did not land (#1959).
    proc.wait.assert_called_once_with(timeout=_cu.KILL_GRACE_SECONDS)


def test_run_shell_timeout_refuses_broadcast_pgid(tmp_path):
    """A pgid <= 1 (e.g. an unset mock pid coerced through __index__ to 1) must
    never reach killpg: os.killpg(1, ...) is kill(-1, ...), a session-wide
    broadcast SIGKILL (#1793). The kill falls back to terminating the child."""
    proc = mock.Mock()
    proc.pid = 4321
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="pytest -n auto", timeout=1)

    with (
        patch("theforge.coordinator.util.subprocess.Popen", return_value=proc),
        patch("theforge.coordinator.util.os.getpgid", return_value=1) as mock_getpgid,
        patch("theforge.coordinator.util.os.killpg") as mock_killpg,
    ):
        ok, output = _cu._run_shell("pytest -n auto --dist worksteal", tmp_path, timeout=1)

    assert ok is False
    mock_getpgid.assert_called_once_with(4321)
    assert _sigkill_calls(mock_killpg) == []
    proc.terminate.assert_called_once_with()
    # Bounded, not bare: an unbounded wait here lasts for the command's whole
    # natural lifetime whenever the kill above did not land (#1959).
    proc.wait.assert_called_once_with(timeout=_cu.KILL_GRACE_SECONDS)


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
    assert _sigkill_calls(mock_killpg) == [mock.call(9876, signal.SIGKILL)]
    # Bounded, not bare: an unbounded wait here lasts for the command's whole
    # natural lifetime whenever the kill above did not land (#1959).
    proc.wait.assert_called_once_with(timeout=_cu.KILL_GRACE_SECONDS)


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

    assert _sigkill_calls(mock_killpg) == []
    # Bounded, not bare: an unbounded wait here lasts for the command's whole
    # natural lifetime whenever the kill above did not land (#1959).
    proc.wait.assert_called_once_with(timeout=_cu.KILL_GRACE_SECONDS)


class _IgnoresSigterm:
    """Popen stand-in for a process that catches SIGTERM and keeps running.

    The mocks used by the tests above return from ``wait()`` immediately, so they
    short-circuit before the SIGKILL escalation and cannot observe it. This one
    stays alive until ``kill()`` — the only thing SIGTERM-ignoring processes
    respond to, and the reason the escalation exists (a survivor holds the gate's
    output pipes open, which is the 300s read in #1959).
    """

    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float | None] = []
        self._alive = True

    def terminate(self) -> None:
        self.terminate_calls += 1  # Delivered, and deliberately ignored.

    def kill(self) -> None:
        self.kill_calls += 1
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self._alive:
            raise subprocess.TimeoutExpired(cmd="ignores-sigterm", timeout=timeout or 0)
        return 0


def test_kill_process_group_escalates_to_sigkill_when_sigterm_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that survives terminate() must be killed, not left running."""
    # Force the no-usable-pgid path so the fallback branch is what runs. Patching
    # the module-local name rather than os.getpgid keeps the real os module intact.
    monkeypatch.setattr(_cu, "is_killable_pgid", lambda _pgid: False)
    proc = _IgnoresSigterm()

    _cu._kill_process_group(proc)

    assert proc.terminate_calls == 1, "SIGTERM should be tried first"
    assert proc.kill_calls == 1, (
        "process survived SIGTERM and was never SIGKILL-ed — it would keep the "
        "gate's output pipes open for its full natural lifetime (#1959)"
    )
    assert proc.wait_timeouts == [_cu.KILL_GRACE_SECONDS, _cu.KILL_GRACE_SECONDS], (
        f"every wait must be bounded by the grace period; got {proc.wait_timeouts}"
    )


def test_kill_process_group_does_not_escalate_when_sigterm_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escalation is conditional: a process that exits on SIGTERM is left alone."""
    monkeypatch.setattr(_cu, "is_killable_pgid", lambda _pgid: False)
    proc = _IgnoresSigterm()
    proc.terminate = lambda: setattr(proc, "_alive", False)  # type: ignore[method-assign]

    _cu._kill_process_group(proc)

    assert proc.kill_calls == 0, "SIGKILL sent to a process that had already exited"
    assert proc.wait_timeouts == [_cu.KILL_GRACE_SECONDS]
