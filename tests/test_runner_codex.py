"""Tests for Codex CLI runner: sandbox flag injection."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from theforge.config import ModelProfile
from theforge.runners.runner_codex import _run_codex


def _make_profile(
    sandbox_mode: str = "workspace-write",
    reasoning_effort: str | None = None,
) -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="codex",
        model="o4-mini",
        budget_usd=2.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash"),
        sandbox_mode=sandbox_mode,
        reasoning_effort=reasoning_effort,
    )


def _make_subprocess_mock(returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = ""
    proc.stderr = ""
    return proc


def _extract_codex_cmd(mock_run: MagicMock) -> list[str]:
    """Return the cmd list from the subprocess.run call."""
    return mock_run.call_args[0][0]


class TestCodexSandboxFlag:
    """Assert that sandbox handling matches the Codex CLI contract."""

    def test_workspace_write_adds_sandbox_flag(self, tmp_path: Path) -> None:
        """sandbox_mode=workspace-write → --sandbox workspace-write in cmd."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex.subprocess.run", return_value=mock_proc):
            with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
                with patch(
                    "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
                ) as mock_run:
                    _run_codex(
                        prompt="implement the thing",
                        profile=profile,
                        working_dir=tmp_path,
                    )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" in cmd
        sandbox_idx = cmd.index("--sandbox")
        assert cmd[sandbox_idx + 1] == "workspace-write"

    def test_read_only_adds_sandbox_flag(self, tmp_path: Path) -> None:
        """sandbox_mode=read-only → --sandbox read-only in cmd."""
        profile = _make_profile(sandbox_mode="read-only")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(
                "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
            ) as mock_run:
                _run_codex(
                    prompt="review only",
                    profile=profile,
                    working_dir=tmp_path,
                )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" in cmd
        sandbox_idx = cmd.index("--sandbox")
        assert cmd[sandbox_idx + 1] == "read-only"

    def test_none_omits_sandbox_flag(self, tmp_path: Path) -> None:
        """sandbox_mode=none → no --sandbox flag in cmd."""
        profile = _make_profile(sandbox_mode="none")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(
                "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
            ) as mock_run:
                _run_codex(
                    prompt="debug run",
                    profile=profile,
                    working_dir=tmp_path,
                )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" not in cmd

    def test_resume_omits_sandbox_flag(self, tmp_path: Path) -> None:
        """Resume path omits --sandbox because current Codex CLI rejects it there."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch(
            "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
        ) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert "resume" in cmd
        assert "--sandbox" not in cmd

    def test_sandbox_flag_none_on_resume(self, tmp_path: Path) -> None:
        """sandbox_mode=none omits --sandbox on resume too."""
        profile = _make_profile(sandbox_mode="none")
        mock_proc = _make_subprocess_mock()
        with patch(
            "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
        ) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" not in cmd
        assert "resume" in cmd

    def test_resume_omits_working_dir_flag(self, tmp_path: Path) -> None:
        """`codex exec resume` does not accept -C; working dir is passed via cwd= instead."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch(
            "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
        ) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert "resume" in cmd
        assert "-C" not in cmd

    def test_resume_orders_session_id_after_flags(self, tmp_path: Path) -> None:
        """Resume command keeps flags before the positional session id."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch(
            "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
        ) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert cmd[:4] == ["npx", "@openai/codex", "exec", "resume"]
        assert cmd[-2:] == ["sess-abc123", "-"]

    def test_uses_workspace_venv_env(self, tmp_path: Path) -> None:
        """Codex subprocess env prefers the worktree virtualenv."""
        profile = _make_profile(sandbox_mode="none")
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        mock_proc = _make_subprocess_mock()

        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(
                "theforge.runners.runner_codex.subprocess.run", return_value=mock_proc
            ) as mock_run:
                _run_codex(
                    prompt="debug run",
                    profile=profile,
                    working_dir=tmp_path,
                    secrets={"OPENAI_API_KEY": "secret"},
                )

        env_passed = mock_run.call_args[1]["env"]
        assert env_passed["PATH"].split(os.pathsep)[0] == str(venv_bin)
        assert env_passed["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        assert env_passed["OPENAI_API_KEY"] == "secret"


# ---------------------------------------------------------------------------
# Lifecycle tests — real subprocess, fake-CLI fixture
# ---------------------------------------------------------------------------


class TestCodexLifecycle:
    """Real-subprocess lifecycle tests for the Codex CLI runner.

    Uses tests/fake_bin/npx (routing @openai/codex) rather than mocking
    subprocess.run, so the tests exercise real process invocation and output-
    file reading that mocks cannot detect.
    """

    _FAKE_BIN: ClassVar[Path] = Path(__file__).parent / "fake_bin"

    @pytest.fixture(autouse=True)
    def _ensure_executable(self) -> None:
        script = self._FAKE_BIN / "npx"
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _patch_env(self, monkeypatch: pytest.MonkeyPatch, mode: str = "happy") -> None:
        """Patch runner_codex.build_workspace_env to put fake_bin first on PATH."""
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
            env["FAKE_CODEX_MODE"] = mode
            return env

        monkeypatch.setattr("theforge.runners.runner_codex.build_workspace_env", _build)

    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Fake npx(codex) writes output file; runner reads it and returns success."""
        self._patch_env(monkeypatch, "happy")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        assert result.success is True
        assert "Task complete." in result.output
        assert result.exit_code == 0
