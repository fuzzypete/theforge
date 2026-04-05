"""Tests for the detach module: PID files, active run management, App Nap suppression."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from theforge import detach

# ── PID file helpers ──────────────────────────────────────────────────


class TestWritePid:
    def test_creates_file_with_pid_and_slug(self, tmp_path):
        pid_file = detach.write_pid("abc123", "my-story", tmp_path)
        assert pid_file.exists()
        lines = pid_file.read_text().splitlines()
        assert int(lines[0]) == os.getpid()
        assert lines[1] == "my-story"

    def test_creates_runs_dir(self, tmp_path):
        detach.write_pid("abc123", "slug", tmp_path)
        assert (tmp_path / ".forge" / "runs").is_dir()

    def test_returns_correct_path(self, tmp_path):
        pid_file = detach.write_pid("deadbeef", "slug", tmp_path)
        assert pid_file == tmp_path / ".forge" / "runs" / "deadbeef.pid"


class TestRemovePid:
    def test_deletes_existing_file(self, tmp_path):
        pid_file = detach.write_pid("abc123", "slug", tmp_path)
        assert pid_file.exists()
        detach.remove_pid("abc123", tmp_path)
        assert not pid_file.exists()

    def test_no_error_if_missing(self, tmp_path):
        # Should not raise
        detach.remove_pid("nonexistent", tmp_path)


class TestReadPidFile:
    def test_parses_pid_and_slug(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\nmy-slug\n")
        result = detach._read_pid_file(pid_file)
        assert result == (12345, "my-slug")

    def test_returns_none_on_corrupt(self, tmp_path):
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-pid\n")
        assert detach._read_pid_file(pid_file) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        pid_file = tmp_path / "missing.pid"
        assert detach._read_pid_file(pid_file) is None

    def test_slug_falls_back_to_stem(self, tmp_path):
        pid_file = tmp_path / "myrun.pid"
        pid_file.write_text("99999\n")  # only PID, no slug line
        result = detach._read_pid_file(pid_file)
        assert result == (99999, "myrun")


# ── Active run management ─────────────────────────────────────────────


class TestListActiveRuns:
    def test_returns_empty_when_no_runs_dir(self, tmp_path):
        runs = detach.list_active_runs(tmp_path)
        assert runs == []

    def test_returns_alive_runs(self, tmp_path):
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        pid_file = runs_dir / "abc123.pid"
        pid_file.write_text(f"{os.getpid()}\nmy-slug\n")

        runs = detach.list_active_runs(tmp_path)
        assert len(runs) == 1
        assert runs[0]["run_id"] == "abc123"
        assert runs[0]["slug"] == "my-slug"
        assert runs[0]["alive"] is True

    def test_removes_stale_pid_files(self, tmp_path):
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        stale = runs_dir / "dead123.pid"
        # Use PID 1 — we mock os.kill to raise ProcessLookupError for it
        stale.write_text("99999999\ndead-slug\n")

        with patch("theforge.detach.os.kill", side_effect=ProcessLookupError):
            runs = detach.list_active_runs(tmp_path)

        assert runs == []
        assert not stale.exists()

    def test_filters_stale_and_keeps_alive(self, tmp_path):
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)

        alive_pid = os.getpid()
        dead_pid = 99999999

        (runs_dir / "alive.pid").write_text(f"{alive_pid}\nalive-slug\n")
        (runs_dir / "dead.pid").write_text(f"{dead_pid}\ndead-slug\n")

        def _mock_kill(pid, sig):
            if pid == dead_pid:
                raise ProcessLookupError
            # alive_pid is fine

        with patch("theforge.detach.os.kill", side_effect=_mock_kill):
            runs = detach.list_active_runs(tmp_path)

        assert len(runs) == 1
        assert runs[0]["run_id"] == "alive"
        assert not (runs_dir / "dead.pid").exists()


# ── App Nap suppression ────────────────────────────────────────────────


class TestSuppressAppNap:
    def test_noop_on_non_darwin(self):
        with patch.object(sys, "platform", "linux"):
            # Should not raise
            detach.suppress_app_nap()

    def test_noop_when_foundation_raises(self):
        """suppress_app_nap should no-op when all backends raise."""
        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict("sys.modules", {"Foundation": None}),
            patch("ctypes.util.find_library", return_value=None),
        ):
            # Should not raise even when Foundation is unavailable and ctypes finds nothing
            detach.suppress_app_nap()


# ── Daemonize run (no actual fork in tests) ───────────────────────────


class TestDaemonizeRun:
    def test_grandchild_writes_pid_and_redirects(self, tmp_path):
        """Simulate grandchild path (both forks return 0 = child) without actually forking."""
        mock_fd = MagicMock()
        mock_fd.fileno.return_value = 99

        # fork() returning 0 means "we are the child"
        with (
            patch("theforge.detach.os.fork", return_value=0),
            patch("theforge.detach.os.setsid"),
            patch("theforge.detach.os.dup2"),
            patch("theforge.detach.os.waitpid"),
            patch("theforge.detach.os.devnull", "/dev/null"),
            patch("theforge.detach.os.open", side_effect=[10, 11]) as mock_os_open,
            patch("theforge.detach.os.close"),
            patch("theforge.detach.write_pid") as mock_write_pid,
            patch(
                "builtins.open",
                return_value=MagicMock(
                    __enter__=lambda s: mock_fd,
                    __exit__=MagicMock(return_value=False),
                    fileno=lambda: 99,
                    write=MagicMock(),
                    flush=MagicMock(),
                ),
            ),
            patch.object(sys, "stdin", MagicMock(fileno=MagicMock(return_value=0))),
            patch.object(
                sys, "stdout", MagicMock(fileno=MagicMock(return_value=1), flush=MagicMock())
            ),
            patch.object(
                sys, "stderr", MagicMock(fileno=MagicMock(return_value=2), flush=MagicMock())
            ),
        ):
            # The second fork also returns 0 — grandchild path
            detach.daemonize_run("run123", "my-slug", tmp_path)

        mock_write_pid.assert_called_once_with("run123", "my-slug", tmp_path)
        log_path = tmp_path / ".forge" / "logs" / "my-slug" / "run-run123.log"
        assert mock_os_open.call_args_list[1].args[0] == str(log_path)

    def test_raises_on_no_fork(self, tmp_path):
        with patch("theforge.detach.os.fork", side_effect=AttributeError):
            with pytest.raises(RuntimeError, match="os.fork"):
                detach.daemonize_run("run123", "slug", tmp_path)


def test_find_log_path_prefers_per_run_log(tmp_path):
    log_path = tmp_path / ".forge" / "logs" / "my-slug" / "run-run123.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("hello")

    assert detach._find_log_path("my-slug", "run123", tmp_path) == log_path


def test_find_log_path_falls_back_to_legacy_run_log(tmp_path):
    legacy_path = tmp_path / ".forge" / "logs" / "my-slug" / "run.log"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("legacy")

    assert detach._find_log_path("my-slug", "run123", tmp_path) == legacy_path
