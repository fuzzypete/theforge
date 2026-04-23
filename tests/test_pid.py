"""Tests for shared PID helpers."""

from __future__ import annotations

from unittest.mock import patch

from theforge.pid import _current_process_fingerprint, _is_pid_alive, _pid_matches_fingerprint


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


def test_current_process_fingerprint_returns_ps_start_time() -> None:
    completed = type("Completed", (), {"returncode": 0, "stdout": "Mon Apr 22 10:11:12 2026\n"})()
    with (
        patch("theforge.pid.os.kill", return_value=None),
        patch("theforge.pid.subprocess.run", return_value=completed) as mock_run,
    ):
        assert _current_process_fingerprint(12345) == "Mon Apr 22 10:11:12 2026"
    mock_run.assert_called_once()


def test_pid_matches_fingerprint_detects_recycled_pid() -> None:
    with patch("theforge.pid._current_process_fingerprint", return_value="new-start"):
        assert _pid_matches_fingerprint(12345, "old-start") is False


def test_pid_matches_fingerprint_accepts_matching_process_instance() -> None:
    with patch("theforge.pid._current_process_fingerprint", return_value="same-start"):
        assert _pid_matches_fingerprint(12345, "same-start") is True
