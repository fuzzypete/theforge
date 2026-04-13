"""Tests for Gemini CLI runner: sandbox wrapper injection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.config import ModelProfile
from theforge.runners.runner_gemini import _run_gemini


def _make_profile(sandbox_mode: str = "workspace-write") -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="gemini",
        model="gemini-2.5-flash",
        budget_usd=2.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash"),
        sandbox_mode=sandbox_mode,
    )


def _make_subprocess_mock(returncode: int = 0, stdout: str = "{}") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = ""
    return proc


class TestGeminiSandboxWrapper:
    """Assert that workspace_effect_sandbox_command is called when sandboxing is requested."""

    def test_workspace_write_uses_sandbox_wrapper(self, tmp_path: Path) -> None:
        """sandbox_mode=workspace-write → workspace_effect_sandbox_command is called."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        wrapped_cmd = ["sandbox-exec", "-f", "/tmp/profile.sb", "npx", "@google/gemini-cli"]
        with patch(
            "theforge.runners.runner_gemini.workspace_effect_sandbox_command",
            return_value=wrapped_cmd,
        ) as mock_sandbox:
            with patch(
                "theforge.runners.runner_gemini.subprocess.run", return_value=mock_proc
            ) as mock_run:
                _run_gemini(
                    prompt="implement the thing",
                    profile=profile,
                    working_dir=tmp_path,
                )
        mock_sandbox.assert_called_once()
        # The cmd passed to subprocess.run should be the sandboxed version
        actual_cmd = mock_run.call_args[0][0]
        assert actual_cmd == wrapped_cmd

    def test_read_only_uses_sandbox_wrapper(self, tmp_path: Path) -> None:
        """sandbox_mode=read-only → workspace_effect_sandbox_command is called."""
        profile = _make_profile(sandbox_mode="read-only")
        mock_proc = _make_subprocess_mock()
        wrapped_cmd = ["sandbox-exec", "-f", "/tmp/profile.sb", "npx", "@google/gemini-cli"]
        with patch(
            "theforge.runners.runner_gemini.workspace_effect_sandbox_command",
            return_value=wrapped_cmd,
        ) as mock_sandbox:
            with patch("theforge.runners.runner_gemini.subprocess.run", return_value=mock_proc):
                _run_gemini(
                    prompt="review only",
                    profile=profile,
                    working_dir=tmp_path,
                )
        mock_sandbox.assert_called_once()

    def test_none_skips_sandbox_wrapper(self, tmp_path: Path) -> None:
        """sandbox_mode=none → workspace_effect_sandbox_command is NOT called."""
        profile = _make_profile(sandbox_mode="none")
        mock_proc = _make_subprocess_mock()
        with patch(
            "theforge.runners.runner_gemini.workspace_effect_sandbox_command"
        ) as mock_sandbox:
            with patch(
                "theforge.runners.runner_gemini.subprocess.run", return_value=mock_proc
            ) as mock_run:
                _run_gemini(
                    prompt="debug run",
                    profile=profile,
                    working_dir=tmp_path,
                )
        mock_sandbox.assert_not_called()
        # The raw npx command is passed directly
        actual_cmd = mock_run.call_args[0][0]
        assert actual_cmd[0] == "npx"

    def test_sandbox_unavailable_fails_closed(self, tmp_path: Path) -> None:
        """When sandbox wrapper returns cmd unchanged, the runner fails closed."""
        profile = _make_profile(sandbox_mode="workspace-write")
        # Return the original cmd unchanged → sandbox unavailable
        with patch(
            "theforge.runners.runner_gemini.workspace_effect_sandbox_command",
            side_effect=lambda cmd, wd: list(cmd),
        ):
            with patch("theforge.runners.runner_gemini.subprocess.run") as mock_run:
                result = _run_gemini(
                    prompt="test",
                    profile=profile,
                    working_dir=tmp_path,
                )
        # subprocess.run must NOT have been called — we fail before invoking the agent
        mock_run.assert_not_called()
        assert result.success is False
        assert result.startup_failure is True
        assert result.exit_code == -1
        assert "SANDBOX_UNAVAILABLE" in result.output

    def test_sandbox_unavailable_none_mode_still_runs(self, tmp_path: Path) -> None:
        """sandbox_mode=none → sandbox wrapper not called, subprocess runs normally."""
        profile = _make_profile(sandbox_mode="none")
        mock_proc = _make_subprocess_mock()
        with patch(
            "theforge.runners.runner_gemini.workspace_effect_sandbox_command"
        ) as mock_sandbox:
            with patch(
                "theforge.runners.runner_gemini.subprocess.run", return_value=mock_proc
            ) as mock_run:
                result = _run_gemini(
                    prompt="debug run",
                    profile=profile,
                    working_dir=tmp_path,
                )
        mock_sandbox.assert_not_called()
        mock_run.assert_called_once()
        assert result.success is True
