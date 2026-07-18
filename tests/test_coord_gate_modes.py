"""Tests for exit-code gate mode, test_target substitution, and gate overrides.

Covers: TestExitCodeGateMode, TestTestTargetSubstitution, TestGateOverride.
"""

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _as_detailed,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _shell_with_gate,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.task import TaskStory

# ── Local helpers ─────────────────────────────────────────────────────


def _make_exit_code_config(tmp_path: Path) -> ForgeConfig:
    """Config with exit-code gate mode."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(
            gate_command="pytest {test_target} -q",
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


# ── Exit-code gate mode tests ────────────────────────────────────────


class TestExitCodeGateMode:
    """Test gate validation using exit code instead of handoff file."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_exit_code_pass(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        """Exit code 0 → PASS in exit-code mode."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_exit_code())
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_exit_code_fail_then_pass(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Exit code non-zero → FAIL, then 0 → PASS on retry."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_exit_code(pass_on_call=2))
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_iteration == 2  # needed a retry
        assert result.state.gate_decisions == ["FAIL", "PASS"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_exit_code_exhaustion(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Gate always fails → ESCALATE after max iterations."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_exit_code(pass_on_call=999))
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_infrastructure_failure_escalates_immediately(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "infrastructure" in result.message.lower() or "timed out" in result.message.lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_dirty_worktree_blocked_in_exit_code_mode(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dirty worktree is still caught in exit-code mode."""
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        result = run_task(config, task)

        assert result.success is False
        assert result.phase != Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_exit_code_dirty_worktree_detected(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Exit-code mode: dirty files detected correctly."""
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        result = run_task(config, task)

        assert result.success is False
        assert result.phase != Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_exit_code_gate_timeout_is_error(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Message must mention timeout and hint to increase gate_timeout
        msg = result.message.lower()
        assert "timed out" in msg or "gate_timeout" in msg

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_exit_code_infrastructure_error_is_error(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Escalated as infrastructure error, not retried as FAIL
        assert result.state.dev_iteration == 1  # no retries consumed

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_exit_code_test_failure_is_fail(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Normal non-zero exit (tests failing) returns FAIL and is retried."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # First gate call fails with normal test output; second passes
        mock_shell.side_effect = _as_detailed(_shell_exit_code(pass_on_call=2))
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.gate_decisions == ["FAIL", "PASS"]
        assert result.state.dev_iteration == 2  # was retried


# ── Test target substitution tests ───────────────────────────────────


class TestTestTargetSubstitution:
    """Test that {test_target} in gate_command is replaced from TaskStory."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_test_target_substituted(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Gate command should contain the task's test_target, not the placeholder."""
        config = _make_exit_code_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test", encoding="utf-8")
        task = TaskStory(
            name="Test",
            story_path=spec,
            slug="test-task",
            test_target="tests/test_specific.py",
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        # Find the gate command: contains pytest but is NOT a git worktree command
        gate_cmds = [c for c in captured_cmds if "pytest" in c and "worktree" not in c]
        assert gate_cmds, "No gate command captured"
        assert "tests/test_specific.py" in gate_cmds[0]
        assert "{test_target}" not in gate_cmds[0]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_test_target_defaults_to_project_root(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """When test_target is None, defaults to the project root placeholder '.' ."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)  # test_target=None by default
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        gate_cmds = [c for c in captured_cmds if "pytest" in c and "worktree" not in c]
        assert gate_cmds
        assert "pytest . -q" == gate_cmds[0]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_test_target_non_python_gate_command(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """A non-Python gate command (e.g. `go test {test_target}`) substitutes correctly.

        Seam-level coverage that the `{test_target}` placeholder is language-neutral and
        propagates the story's test_target into whatever gate command the project configures.
        """
        config = _make_exit_code_config(tmp_path)
        # Replace the default Python gate command with a Go-style command.
        from dataclasses import replace

        config = replace(
            config,
            validation=replace(config.validation, gate_command="go test {test_target}"),
        )
        spec = tmp_path / "spec.md"
        spec.write_text("# Test", encoding="utf-8")
        task = TaskStory(
            name="Test",
            story_path=spec,
            slug="test-task",
            test_target="./internal/foo/...",
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            captured_cmds.append(cmd)
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "go test" in cmd and "worktree" not in cmd:
                return (True, "ok")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        gate_cmds = [c for c in captured_cmds if "go test" in c and "worktree" not in c]
        assert gate_cmds, f"No gate command captured: {captured_cmds}"
        assert "./internal/foo/..." in gate_cmds[0]
        assert "{test_target}" not in gate_cmds[0]
        # pytest-specific terminology must not appear in the substituted command.
        assert "pytest" not in gate_cmds[0]


# ── Gate override tests ──────────────────────────────────────────────


class TestGateOverride:
    """Tests for spec-level gate override feature."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_gate_override_none_skips_validation(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
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
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_gate_override_custom_command(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """gate_override='make lint' runs that command instead of global gate."""
        config = _make_config(tmp_path)
        task = _make_task_with_gate_override(tmp_path, "make lint")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        called_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            called_cmds.append(cmd)
            if "make lint" in cmd:
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_gate_override_custom_command_fail(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented."),
            _make_agent_result(success=True, output="Fixed lint."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Gate always fails → dev retried → max_dev_iterations exhausted → ESCALATE
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert any(d == "FAIL" for d in result.state.gate_decisions)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_gate_override_absent_uses_global(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """No gate_override → uses config.validation.gate_command (backward compat)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)  # no gate_override (None by default)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        assert task.gate_override is None

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE

    def test_gate_override_parsed_from_frontmatter(self, tmp_path):
        """parse_spec_frontmatter reads 'gate' key and it maps to gate_override on TaskStory."""
        from theforge.task import parse_spec_frontmatter

        spec = tmp_path / "spec.md"
        spec.write_text(
            "---\nname: My Spec\nslug: my-spec\ngate: none\n---\n\n# Body",
            encoding="utf-8",
        )

        fm = parse_spec_frontmatter(spec)
        assert fm.get("gate") == "none"

        # Build TaskStory with the parsed gate value
        task = TaskStory(
            name=fm.get("name", "My Spec"),
            story_path=spec,
            slug=fm.get("slug", "my-spec"),
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_gate_override_none_case_insensitive(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
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
                    return (True, "OK")
                if "git status --porcelain" in cmd:
                    return (True, "")
                stale_resp = _handle_stale_check_cmd(cmd)
                if stale_resp is not None:
                    return stale_resp
                return (True, "OK")

            mock_shell.side_effect = _as_detailed(shell_side_effect)
            mock_preflight.return_value = _PREFLIGHT_RESULT
            mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]

            result = run_task(config, task)

            assert result.success is True, f"Failed for gate_override={override_value!r}"
            assert gate_calls == [], (
                f"Gate was called for override={override_value!r}: {gate_calls}"
            )
