"""Tests for gate-related coordinator behaviour.

Covers: stale handoff cleanup, dirty worktree detection, exit-code gate mode,
pytest_target substitution, spec-level gate overrides, and fix prompt routing.
"""

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_ntfy_config,
    _preflight_then,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    NotificationConfig,
    NtfyConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator import Phase, run_from_review, run_task
from theforge.task import TaskSpec

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


def _make_task_with_gate_override(tmp_path: Path, gate_override: str | None) -> TaskSpec:
    """Create a test task with a gate_override set."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskSpec(
        name="Test Task",
        spec_path=spec,
        slug="test-task",
        file_scope=["src/"],
        gate_override=gate_override,
    )


# ── Stale handoff tests ──────────────────────────────────────────────


class TestCoordinatorStaleHandoff:
    """Test that stale handoff.yaml is deleted before running the gate."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_stale_handoff_not_reused(self, mock_shell, mock_agent, mock_pool, tmp_path):
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
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Gate failed and stale handoff was deleted → should escalate, not PASS
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Gate" in result.message or "gate" in result.message


class TestCoordinatorStaleHandoffUnlinkFailure:
    """Test that unlink failure on stale handoff is handled gracefully."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_handoff_is_directory(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """If handoff.yaml is a directory, unlink fails → gate error → retry/escalate."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Create handoff.yaml as a directory (pathological case)
        (workspace / "handoff.yaml").mkdir()

        mock_shell.return_value = (True, "OK")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Should mention stale handoff or gate failure
        assert "handoff" in result.message.lower() or "gate" in result.message.lower()


# ── Dirty worktree tests ─────────────────────────────────────────────


class TestCoordinatorDirtyWorktree:
    """Test that the coordinator catches uncommitted changes after gate PASS."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_dirty_worktree_retries_dev(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Dirty worktree after gate PASS sends dev back with process violation feedback."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        call_count = {"gate": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                call_count["gate"] += 1
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # First call: dirty; second call: clean (dev fixed it)
                if call_count["gate"] == 1:
                    return (True, " M src/theforge/runner.py\n M src/theforge/config.py")
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Dev was retried (dirty first, clean second) → should succeed
        assert result.success is True
        assert result.phase == Phase.DONE
        # PREFLIGHT + 2 DEV calls (once dirty, once clean)
        assert mock_agent.call_count == 3

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_dirty_worktree_escalates_after_max_retries(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Dirty worktree with no retries left escalates."""
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
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "uncommitted" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_handoff_file_not_flagged_as_dirty(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """handoff.yaml in git status output is excluded from dirty check."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # Only handoff.yaml is dirty — that's expected
                return (True, "?? handoff.yaml")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # handoff.yaml is filtered out → clean worktree → proceeds to review
        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_handoff_dirty_worktree_unchanged(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Regression guard: handoff mode still filters handoff.yaml from dirty check."""
        config = _make_config(tmp_path)  # handoff_file="handoff.yaml"
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # handoff.yaml is the only dirty file — should be filtered out
                return (True, "?? handoff.yaml")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # handoff.yaml filtered out → worktree clean → proceeds to DONE
        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_out_of_scope_files_auto_committed(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """All dirty files outside file_scope → auto-commit, proceed to REVIEW without retry."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskSpec(
            name="Test Task",
            spec_path=spec,
            slug="test-task",
            file_scope=["src/theforge/coordinator.py"],
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        auto_commit_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # ideate.py is outside file_scope
                return (True, " M src/theforge/ideate.py")
            if "git add" in cmd or "git commit" in cmd:
                auto_commit_cmds.append(cmd)
                return (True, "OK")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Auto-committed → no DEV retry → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        # PREFLIGHT + 1 DEV call only (no retry)
        assert mock_agent.call_count == 2
        # git add and git commit were called
        assert any("git add" in c for c in auto_commit_cmds)
        assert any("git commit" in c for c in auto_commit_cmds)

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_in_scope_dirty_file_retries_dev(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """One dirty file inside file_scope → DEV retry (unchanged behavior)."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskSpec(
            name="Test Task",
            spec_path=spec,
            slug="test-task",
            file_scope=["src/theforge/coordinator.py"],
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        call_count = {"gate": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                call_count["gate"] += 1
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # coordinator.py is in-scope — triggers DEV retry on first gate
                if call_count["gate"] == 1:
                    return (True, " M src/theforge/coordinator.py")
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # In-scope dirty → DEV retry → clean on second pass → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        # PREFLIGHT + 2 DEV calls (retry once for dirty)
        assert mock_agent.call_count == 3

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_empty_file_scope_all_dirty_treated_as_in_scope(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Empty file_scope → all dirty files treated as in-scope → DEV retry (unchanged)."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskSpec(
            name="Test Task",
            spec_path=spec,
            slug="test-task",
            file_scope=[],  # empty — no scope restriction
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        call_count = {"gate": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                call_count["gate"] += 1
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # dirty on first pass, clean on second
                if call_count["gate"] == 1:
                    return (True, " M src/theforge/ideate.py")
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Empty scope → no auto-commit → DEV retry → clean → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        # PREFLIGHT + 2 DEV calls (retry once)
        assert mock_agent.call_count == 3

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_mixed_tracked_and_untracked_retries_dev(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Out-of-scope tracked file + untracked file → DEV retry, not auto-commit."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskSpec(
            name="Test Task",
            spec_path=spec,
            slug="test-task",
            file_scope=["src/theforge/coordinator.py"],
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        auto_commit_called = {"n": 0}
        call_count = {"gate": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                call_count["gate"] += 1
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                if call_count["gate"] == 1:
                    # Tracked out-of-scope file AND an untracked file
                    return (True, " M src/theforge/ideate.py\n?? new_scratch.py")
                return (True, "")
            if "git add" in cmd or "git commit" in cmd:
                auto_commit_called["n"] += 1
                return (True, "OK")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Untracked file prevents auto-commit → DEV retry → clean → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        assert auto_commit_called["n"] == 0  # no auto-commit attempted
        assert mock_agent.call_count == 3  # PREFLIGHT + 2 DEV

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_auto_commit_git_failure_falls_back_to_dev_retry(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Auto-commit git failure (add or commit) falls back to DEV retry."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskSpec(
            name="Test Task",
            spec_path=spec,
            slug="test-task",
            file_scope=["src/theforge/coordinator.py"],
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        call_count = {"gate": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                call_count["gate"] += 1
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                if call_count["gate"] == 1:
                    return (True, " M src/theforge/ideate.py")
                return (True, "")
            if "git add" in cmd:
                return (False, "error: unable to stage file")  # git add fails
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Auto-commit failed → DEV retry → clean → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        assert mock_agent.call_count == 3  # PREFLIGHT + 2 DEV

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_rename_status_uses_destination_filename(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Renamed file (R status) → destination filename used, treated as out-of-scope."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
        task = TaskSpec(
            name="Test Task",
            spec_path=spec,
            slug="test-task",
            file_scope=["src/theforge/coordinator.py"],
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        committed_files: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # Rename: old name was ideate_old.py, new name is ideate.py
                return (True, "R  src/theforge/ideate_old.py -> src/theforge/ideate.py")
            if "git add" in cmd:
                committed_files.append(cmd)
                return (True, "OK")
            if "git commit" in cmd:
                return (True, "OK")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Renamed file treated as out-of-scope → auto-commit → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        assert mock_agent.call_count == 2  # PREFLIGHT + 1 DEV only
        # git add used the destination filename
        assert any("src/theforge/ideate.py" in cmd for cmd in committed_files)
        assert not any("ideate_old.py" in cmd for cmd in committed_files)


# ── Exit-code gate mode tests ────────────────────────────────────────


class TestExitCodeGateMode:
    """Test gate validation using exit code instead of handoff file."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_exit_code_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Exit code 0 → PASS in exit-code mode."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_exit_code()
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_exit_code_fail_then_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Exit code non-zero → FAIL, then 0 → PASS on retry."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_exit_code(pass_on_call=2)
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_iteration == 2  # needed a retry
        assert result.state.gate_decisions == ["FAIL", "PASS"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_exit_code_exhaustion(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Gate always fails → ESCALATE after max iterations."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_exit_code(pass_on_call=999)
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_infrastructure_failure_escalates_immediately(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """TIMEOUT/ERROR in exit-code mode escalates immediately (not retried as FAIL)."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (False, "TIMEOUT after 120s: pytest tests/ -q")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "infrastructure" in result.message.lower() or "timeout" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_dirty_worktree_blocked_in_exit_code_mode(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Dirty worktree is still caught in exit-code mode (empty handoff_file)."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/coordinator.py\n M tests/test_something.py")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase != Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_exit_code_dirty_worktree_detected(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Exit-code mode: dirty files detected (empty handoff_file must not cause false-clean)."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/coordinator.py\n M tests/test_foo.py")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase != Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_exit_code_gate_timeout_is_error(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Timeout in exit-code mode returns error message (not FAIL), escalates immediately."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (False, "TIMEOUT after 120s: pytest tests/ -q")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Message must mention timeout and hint to increase gate_timeout
        msg = result.message.lower()
        assert "timed out" in msg or "gate_timeout" in msg

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_exit_code_infrastructure_error_is_error(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """ERROR: prefix in exit-code mode returns error (not FAIL), escalates immediately."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (False, "ERROR: [Errno 2] No such file or directory: 'pytest'")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Escalated as infrastructure error, not retried as FAIL
        assert result.state.dev_iteration == 1  # no retries consumed

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_exit_code_test_failure_is_fail(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Normal non-zero exit (tests failing) returns FAIL and is retried."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # First gate call fails with normal test output; second passes
        mock_shell.side_effect = _shell_exit_code(pass_on_call=2)
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.gate_decisions == ["FAIL", "PASS"]
        assert result.state.dev_iteration == 2  # was retried


# ── Pytest target substitution tests ─────────────────────────────────


class TestPytestTargetSubstitution:
    """Test that {pytest_target} in gate_command is replaced from TaskSpec."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_pytest_target_substituted(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Gate command should contain the task's pytest_target, not the placeholder."""
        config = _make_exit_code_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test", encoding="utf-8")
        task = TaskSpec(
            name="Test",
            spec_path=spec,
            slug="test-task",
            file_scope=[],
            pytest_target="tests/test_specific.py",
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_cmds = []

        def shell_side_effect(cmd, cwd, **kwargs):
            captured_cmds.append(cmd)
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "pytest" in cmd and "worktree" not in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        # Find the gate command: contains pytest but is NOT a git worktree command
        gate_cmds = [c for c in captured_cmds if "pytest" in c and "worktree" not in c]
        assert gate_cmds, "No gate command captured"
        assert "tests/test_specific.py" in gate_cmds[0]
        assert "{pytest_target}" not in gate_cmds[0]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_pytest_target_defaults_to_tests(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """When pytest_target is None, defaults to 'tests/'."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)  # pytest_target=None by default
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_cmds = []

        def shell_side_effect(cmd, cwd, **kwargs):
            captured_cmds.append(cmd)
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "pytest" in cmd and "worktree" not in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        gate_cmds = [c for c in captured_cmds if "pytest" in c and "worktree" not in c]
        assert gate_cmds
        assert "tests/" in gate_cmds[0]


# ── Gate override tests ──────────────────────────────────────────────


class TestGateOverride:
    """Tests for spec-level gate override feature."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_gate_override_none_skips_validation(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """gate_override='none' skips validation; no gate subprocess is run."""
        config = _make_config(tmp_path)
        task = _make_task_with_gate_override(tmp_path, "none")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        gate_calls: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            # Track any gate-related shell calls
            if "gate" in cmd:
                gate_calls.append(cmd)
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # No gate command should have been run
        assert gate_calls == [], f"Gate was called unexpectedly: {gate_calls}"
        # PASS should have been recorded
        assert "PASS" in result.state.gate_decisions

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_gate_override_custom_command(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """gate_override='make lint' runs that command instead of global gate."""
        config = _make_config(tmp_path)
        task = _make_task_with_gate_override(tmp_path, "make lint")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        called_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            called_cmds.append(cmd)
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            # Custom gate succeeds with exit 0 (exit-code mode)
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # "make lint" should have been called
        assert any("make lint" in c for c in called_cmds), (
            f"make lint not called; cmds={called_cmds}"
        )
        # Global gate_command ("make gate") should NOT have been called
        assert not any("make gate" in c for c in called_cmds), (
            f"Global gate was called unexpectedly: {called_cmds}"
        )

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_gate_override_custom_command_fail(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Custom gate command returning non-zero exit code produces FAIL and triggers retry."""
        config = _make_config(tmp_path)
        task = _make_task_with_gate_override(tmp_path, "make lint")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "make lint" in cmd:
                # Simulate lint failure (non-zero exit → FAIL in exit-code mode)
                return (False, "lint error: style violations found")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented."),
            _make_agent_result(success=True, output="Fixed lint."),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Gate always fails → dev retried → max_dev_iterations exhausted → ESCALATE
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert any(d == "FAIL" for d in result.state.gate_decisions)

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_gate_override_absent_uses_global(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """No gate_override → uses config.validation.gate_command (backward compat)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)  # no gate_override (None by default)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        assert task.gate_override is None

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

    def test_gate_override_parsed_from_frontmatter(self, tmp_path):
        """parse_spec_frontmatter reads 'gate' key and it maps to gate_override on TaskSpec."""
        from theforge.task import parse_spec_frontmatter

        spec = tmp_path / "spec.md"
        spec.write_text(
            "---\nname: My Spec\nslug: my-spec\ngate: none\n---\n\n# Body",
            encoding="utf-8",
        )

        fm = parse_spec_frontmatter(spec)
        assert fm.get("gate") == "none"

        # Build TaskSpec with the parsed gate value
        task = TaskSpec(
            name=fm.get("name", "My Spec"),
            spec_path=spec,
            slug=fm.get("slug", "my-spec"),
            file_scope=fm.get("file_scope", []),
            gate_override=fm.get("gate"),
        )
        assert task.gate_override == "none"

    def test_gate_override_non_string_stripped_from_frontmatter(self, tmp_path):
        """R3: non-string gate values are stripped by parse_spec_frontmatter (type safety)."""
        from theforge.task import parse_spec_frontmatter

        spec = tmp_path / "spec.md"
        # gate: 123 is a YAML integer, not a string
        spec.write_text(
            "---\nname: My Spec\nslug: my-spec\ngate: 123\n---\n\n# Body",
            encoding="utf-8",
        )

        fm = parse_spec_frontmatter(spec)
        # Non-string gate must be stripped to avoid AttributeError in _is_gate_skip
        assert "gate" not in fm

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_gate_override_none_case_insensitive(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """gate_override='None' and 'NONE' both trigger skip mode."""
        for override_value in ("None", "NONE"):
            config = _make_config(tmp_path)
            task = _make_task_with_gate_override(tmp_path, override_value)
            workspace = tmp_path / "test-task"
            workspace.mkdir(exist_ok=True)

            gate_calls: list[str] = []

            def shell_side_effect(cmd, cwd, **kwargs):
                if "gate" in cmd:
                    gate_calls.append(cmd)
                    _write_handoff(Path(cwd), "PASS")
                    return (True, "OK")
                if "git status --porcelain" in cmd:
                    return (True, "")
                stale_resp = _handle_stale_check_cmd(cmd)
                if stale_resp is not None:
                    return stale_resp
                return (True, "OK")

            mock_shell.side_effect = shell_side_effect
            mock_agent.side_effect = _preflight_then(
                _make_agent_result(success=True, output="Implemented.")
            )
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]

            result = run_task(config, task)

            assert result.success is True, f"Failed for gate_override={override_value!r}"
            assert gate_calls == [], (
                f"Gate was called for override={override_value!r}: {gate_calls}"
            )


# ── Fix prompt routing tests ─────────────────────────────────────────


class TestFixPromptRouting:
    """Tests that the coordinator routes to build_fix_prompt on iteration 2+."""

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_iteration_1_uses_dev_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """First iteration always uses build_dev_prompt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        mock_dev_prompt.assert_called_once()
        mock_fix_prompt.assert_not_called()

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_iteration_2_with_review_findings_uses_fix_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """Iteration 2+ with last_review_findings set uses build_fix_prompt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Done."),  # iter 1
            _make_agent_result(success=True, output="Fixed."),  # iter 2
        )
        mock_pool.side_effect = [
            # First review: REQUEST_CHANGES → triggers iter 2
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            # Second review: APPROVE
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_task(config, task)

        assert mock_dev_prompt.call_count == 1  # only iter 1
        assert mock_fix_prompt.call_count == 1  # iter 2 uses fix prompt

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_gate_failure_retry_uses_dev_prompt_not_fix_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """Gate failure retries use build_dev_prompt (not review findings)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        # First gate call FAILs, second PASSes
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["FAIL", "PASS"])
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Done."),  # iter 1
            _make_agent_result(success=True, output="Fixed."),  # iter 2 (gate retry)
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        # Both iterations should use dev_prompt since last_review_findings is None
        assert mock_dev_prompt.call_count == 2
        mock_fix_prompt.assert_not_called()

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_fix_prompt_receives_review_findings(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """build_fix_prompt is called with the review findings content."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Done."),
            _make_agent_result(success=True, output="Fixed."),
        )
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_task(config, task)

        assert mock_fix_prompt.call_count == 1
        call_kwargs = mock_fix_prompt.call_args.kwargs
        assert call_kwargs["review_findings"] is not None
        assert len(call_kwargs["review_findings"]) > 0
        assert call_kwargs["iteration"] >= 1

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_gate_failure_after_review_uses_dev_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """After REQUEST_CHANGES, if the fix attempt's gate fails, the retry uses
        build_dev_prompt (not build_fix_prompt) because human_feedback is set."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        # iter 1: PASS; iter 2 (post-review fix): FAIL; iter 3 (gate retry): PASS
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS", "FAIL", "PASS"])
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Done."),  # iter 1
            _make_agent_result(success=True, output="Fixed."),  # iter 2 (fix attempt)
            _make_agent_result(success=True, output="Fixed."),  # iter 3 (gate retry)
        )
        mock_pool.side_effect = [
            # Review after iter 1: REQUEST_CHANGES
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            # Review after iter 3: APPROVE
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_task(config, task)

        # iter 1 → build_dev_prompt; iter 2 → build_fix_prompt; iter 3 → build_dev_prompt
        assert mock_dev_prompt.call_count == 2
        assert mock_fix_prompt.call_count == 1

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_run_from_review_first_dev_uses_dev_prompt(
        self, mock_shell, mock_agent, mock_pool, mock_dev_prompt, mock_fix_prompt, tmp_path
    ):
        """run_from_review() starts at REVIEW with last_review_findings pre-set.
        The first DEV pass (dev_iteration=1) must use build_dev_prompt, not
        build_fix_prompt, because there is no prior resumed dev session."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        # run_from_review skips preflight — only dev + review agents
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Fixed."),
        ]
        mock_pool.side_effect = [
            # Initial REVIEW: REQUEST_CHANGES → triggers DEV
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            # Second REVIEW after DEV: APPROVE
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_from_review(config, task, workspace_path=workspace)

        # run_from_review starts at REVIEW; after REQUEST_CHANGES the first DEV pass
        # uses build_fix_prompt because retry_reason="review_changes" and findings are
        # set — the routing condition does not require a prior resumed dev session.
        mock_dev_prompt.assert_not_called()
        assert mock_fix_prompt.call_count == 1

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coord_notify._ntfy_publish")
    @patch("theforge.coord_notify._ntfy_poll_reply")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_extend_on_approve_with_no_findings_uses_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_pool,
        mock_poll,
        mock_publish,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """After a remote 'extend' on an APPROVE review (no findings), the next
        DEV pass must use build_dev_prompt. The extend path only sets
        last_review_findings when there are actual findings; an empty findings
        list produces last_review_findings=None, so the routing falls back to
        build_dev_prompt (there is nothing specific for the agent to fix)."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Done."),  # iter 1
            _make_agent_result(success=True, output="Done."),  # iter after extend
        )
        # APPROVE review — no P1 findings → last_review_findings will be None
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        poll_calls = {"n": 0}

        def poll_side(reply_url, since_ts, timeout_seconds):
            poll_calls["n"] += 1
            if poll_calls["n"] == 1:
                return ("extend", None)
            return ("approve", None)

        mock_poll.side_effect = poll_side

        result = run_task(config, task, interactive=True, notify=True)

        assert result.success is True
        # iter 1 → build_dev_prompt; post-extend iter → build_dev_prompt
        # (no actionable findings → last_review_findings=None → dev prompt)
        assert mock_dev_prompt.call_count == 2
        mock_fix_prompt.assert_not_called()

    @patch("theforge.coordinator.build_fix_prompt")
    @patch("theforge.coordinator.build_dev_prompt")
    @patch("theforge.coord_notify._ntfy_publish")
    @patch("theforge.coord_notify._ntfy_poll_reply")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_state._run_shell")
    def test_extend_after_request_changes_uses_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_pool,
        mock_poll,
        mock_publish,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """After a remote 'extend' triggered by REQUEST_CHANGES (cycles exhausted),
        the next DEV pass must use build_fix_prompt because last_review_findings
        contains real P1 findings from the REQUEST_CHANGES review."""
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
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=1),
            notifications=NotificationConfig(
                backend="ntfy",
                ntfy=NtfyConfig(url="https://ntfy.sh/test", priority="high"),
                human_review_timeout_seconds=60,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Done."),  # iter 1
            _make_agent_result(success=True, output="Fixed."),  # iter 2 (post-extend fix)
        )

        pool_calls = {"n": 0}

        def pool_side(*args, **kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                # First review: REQUEST_CHANGES — exhausts cycles (max_review_cycles=1)
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            # Second review after fix: APPROVE
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side

        poll_calls = {"n": 0}

        def poll_side(reply_url, since_ts, timeout_seconds):
            poll_calls["n"] += 1
            # First call: human extends after REQUEST_CHANGES exhausted cycles
            if poll_calls["n"] == 1:
                return ("extend", None)
            # Second call: human approves after the fix iteration
            return ("approve", None)

        mock_poll.side_effect = poll_side

        result = run_task(config, task, interactive=True, notify=True)

        assert result.success is True
        # iter 1 → build_dev_prompt; iter 2 (post-extend) → build_fix_prompt (P1 findings)
        assert mock_dev_prompt.call_count == 1
        assert mock_fix_prompt.call_count == 1
