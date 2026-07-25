"""Tests for Gemini CLI runner: sandbox wrapper injection."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

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

    def test_uses_workspace_venv_env(self, tmp_path: Path) -> None:
        """Gemini subprocess env prefers the worktree virtualenv."""
        profile = _make_profile(sandbox_mode="none")
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        expected_env = {
            "PATH": os.pathsep.join([str(venv_bin), "/usr/bin"]),
            "VIRTUAL_ENV": str(tmp_path / ".venv"),
            "GOOGLE_API_KEY": "secret",
        }
        mock_proc = _make_subprocess_mock()

        with (
            patch("theforge.runners.runner_gemini.build_workspace_env", return_value=expected_env),
            patch(
                "theforge.runners.runner_gemini.subprocess.run",
                return_value=mock_proc,
            ) as mock_run,
        ):
            _run_gemini(
                prompt="debug run",
                profile=profile,
                working_dir=tmp_path,
                secrets={"GOOGLE_API_KEY": "secret"},
            )

        env_passed = mock_run.call_args[1]["env"]
        assert env_passed["PATH"].split(os.pathsep)[0] == str(venv_bin)
        assert env_passed["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        assert env_passed["GOOGLE_API_KEY"] == "secret"


# ---------------------------------------------------------------------------
# Lifecycle tests — real subprocess, fake-CLI fixture
# ---------------------------------------------------------------------------


class TestGeminiLifecycle:
    """Real-subprocess lifecycle tests for the Gemini CLI runner.

    Uses tests/fake_bin/npx (routing @google/gemini-cli) rather than mocking
    subprocess.run, so the tests exercise real process invocation and JSON-
    output parsing that mocks cannot detect.
    """

    _FAKE_BIN: ClassVar[Path] = Path(__file__).parent / "fake_bin"

    @pytest.fixture(autouse=True)
    def _ensure_executable(self) -> None:
        script = self._FAKE_BIN / "npx"
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _patch_env(self, monkeypatch: pytest.MonkeyPatch, mode: str = "happy") -> None:
        """Patch runner_gemini.build_workspace_env to put fake_bin first on PATH."""
        fake_bin = self._FAKE_BIN
        from theforge.workspace_env import build_workspace_env as _orig

        def _build(
            workspace_path: Path,
            base_env: object = None,
            *,
            extra: object = None,
        ) -> dict:
            env = _orig(workspace_path, base_env, extra=extra)  # type: ignore[arg-type]
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env["FAKE_GEMINI_MODE"] = mode
            return env

        monkeypatch.setattr("theforge.runners.runner_gemini.build_workspace_env", _build)

    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Fake npx(gemini) prints JSON to stdout; runner parses it and returns success."""
        self._patch_env(monkeypatch, "happy")
        profile = _make_profile(sandbox_mode="none")
        result = _run_gemini(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        assert result.success is True
        assert result.output == "Task complete."
        assert result.exit_code == 0
