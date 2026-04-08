from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.runners.sandbox import sandbox_command
from theforge.runners.tool_runtime import _handle_bash


def test_sandbox_command_macos_wraps(tmp_path: Path) -> None:
    cmd = ["claude", "-p"]
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = sandbox_command(cmd, tmp_path)
    assert wrapped[:2] == ["sandbox-exec", "-p"]
    assert "claude" in wrapped


def test_sandbox_command_linux_wraps(tmp_path: Path) -> None:
    cmd = ["bash", "-c", "pwd"]
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = sandbox_command(cmd, tmp_path)
    assert wrapped[0] == "bwrap"
    assert "bash" in wrapped


def test_sandbox_command_fallback_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cmd = ["claude", "-p"]
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=False),
        caplog.at_level(logging.WARNING),
    ):
        wrapped = sandbox_command(cmd, tmp_path)
    assert wrapped == cmd


def test_handle_bash_uses_sandbox_command(tmp_path: Path) -> None:
    with (
        patch(
            "theforge.runners.tool_runtime.sandbox_command", return_value=["bash", "-c", "pwd"]
        ) as mock_sandbox,
        patch("theforge.runners.tool_runtime.subprocess.run") as mock_run,
    ):
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0
        _handle_bash(command="pwd", working_dir=tmp_path)
    mock_sandbox.assert_called_once()
    assert mock_run.call_args[0][0] == ["bash", "-c", "pwd"]


def test_bash_outside_allowed_root_rejected_when_sandbox_denies() -> None:
    with (
        patch(
            "theforge.runners.tool_runtime.sandbox_command",
            return_value=["bash", "-c", "cat ../other/file"],
        ),
        patch(
            "theforge.runners.tool_runtime.subprocess.run",
            side_effect=PermissionError("Operation not permitted"),
        ),
    ):
        output = _handle_bash(command="cat ../other/file", working_dir=Path("/tmp/worktree"))
    assert "PermissionError" in output


def test_macos_profile_does_not_allow_global_reads(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _macos_profile

    profile = _macos_profile(tmp_path)

    assert "(allow file-read*)\n" not in profile
    assert f'(subpath "{tmp_path.resolve()}")' in profile


def test_sandbox_command_linux_probe_uses_hashable_cache_key(tmp_path: Path) -> None:
    cmd = ["bash", "-c", "pwd"]

    def fake_available(binary: str, probe_key: tuple[str, ...]) -> bool:
        assert isinstance(probe_key, tuple)
        return True

    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", side_effect=fake_available),
    ):
        wrapped = sandbox_command(cmd, tmp_path)

    assert wrapped[0] == "bwrap"
