"""Tests for shared PID helpers."""

from __future__ import annotations

from unittest.mock import patch

from theforge.pid import _is_pid_alive


def test_is_pid_alive_returns_false_for_missing_process() -> None:
    with patch("theforge.pid.os.kill", side_effect=ProcessLookupError):
        assert _is_pid_alive(12345) is False


def test_is_pid_alive_returns_true_for_permission_error() -> None:
    with patch("theforge.pid.os.kill", side_effect=PermissionError):
        assert _is_pid_alive(12345) is True


def test_is_pid_alive_returns_true_for_other_os_error() -> None:
    with patch("theforge.pid.os.kill", side_effect=OSError):
        assert _is_pid_alive(12345) is True


def test_is_pid_alive_returns_true_when_kill_succeeds() -> None:
    with patch("theforge.pid.os.kill", return_value=None):
        assert _is_pid_alive(12345) is True
