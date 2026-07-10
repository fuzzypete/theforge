"""Tests for the detach module: PID files, active run management, App Nap suppression."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

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


# ── Daemonize run (Popen re-exec model — no fork) ─────────────────────


class TestDaemonizeRun:
    """daemonize_run spawns a fresh interpreter via subprocess.Popen and
    exits the parent. We patch Popen and sys.exit so the test doesn't
    actually fork or terminate the test runner.
    """

    def _run_and_capture(self, tmp_path, *, popen_pid: int = 4242):
        proc = MagicMock()
        proc.pid = popen_pid
        with (
            patch("theforge.detach.subprocess.Popen", return_value=proc) as mock_popen,
            patch("theforge.detach.sys.exit") as mock_exit,
        ):
            detach.daemonize_run("run123", "my-slug", tmp_path)
        return mock_popen, mock_exit

    def test_spawns_python_via_theforge_cli(self, tmp_path):
        mock_popen, _ = self._run_and_capture(tmp_path)
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "theforge.cli"]

    def test_sets_detached_env_sentinel(self, tmp_path):
        mock_popen, _ = self._run_and_capture(tmp_path)
        env = mock_popen.call_args.kwargs["env"]
        assert env["FORGE_DETACHED"] == "1"
        assert env["FORGE_DETACHED_RUN_ID"] == "run123"
        assert env["FORGE_DETACHED_SLUG"] == "my-slug"

    def test_uses_start_new_session_and_close_fds(self, tmp_path):
        mock_popen, _ = self._run_and_capture(tmp_path)
        kw = mock_popen.call_args.kwargs
        assert kw["start_new_session"] is True
        assert kw["close_fds"] is True

    def test_writes_pid_file_with_child_pid(self, tmp_path):
        self._run_and_capture(tmp_path, popen_pid=9876)
        pid_file = tmp_path / ".forge" / "runs" / "run123.pid"
        assert pid_file.exists()
        lines = pid_file.read_text().splitlines()
        assert int(lines[0]) == 9876
        assert lines[1] == "my-slug"

    def test_parent_exits_zero(self, tmp_path):
        _, mock_exit = self._run_and_capture(tmp_path)
        mock_exit.assert_called_once_with(0)

    def test_does_not_call_os_fork(self, tmp_path):
        """Regression: fork must not be invoked — that was the macOS crash."""
        with (
            patch("theforge.detach.subprocess.Popen", return_value=MagicMock(pid=1)),
            patch("theforge.detach.sys.exit"),
            patch(
                "theforge.detach.os.fork",
                side_effect=AssertionError("os.fork must not be called"),
            ),
        ):
            detach.daemonize_run("run123", "my-slug", tmp_path)


class TestIsDetachedChild:
    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("FORGE_DETACHED", raising=False)
        assert detach.is_detached_child() is False

    def test_true_when_one(self, monkeypatch):
        monkeypatch.setenv("FORGE_DETACHED", "1")
        assert detach.is_detached_child() is True

    def test_false_for_other_values(self, monkeypatch):
        monkeypatch.setenv("FORGE_DETACHED", "0")
        assert detach.is_detached_child() is False


class TestSetupDetachedChild:
    def test_writes_pid_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FORGE_PREV_RUN_ID", raising=False)
        detach.setup_detached_child("run123", "my-slug", tmp_path)
        pid_file = tmp_path / ".forge" / "runs" / "run123.pid"
        assert pid_file.exists()
        lines = pid_file.read_text().splitlines()
        assert int(lines[0]) == os.getpid()
        assert lines[1] == "my-slug"

    def test_writes_redirect_when_prev_run_id_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_PREV_RUN_ID", "old-run-1")
        detach.setup_detached_child("run123", "my-slug", tmp_path)
        redirect = tmp_path / ".forge" / "runs" / "old-run-1.redirect"
        assert redirect.exists()
        # Env var consumed.
        assert "FORGE_PREV_RUN_ID" not in os.environ


# ── Terminal marker (.ended) ──────────────────────────────────────────


class TestWriteRunEnded:
    def test_creates_ended_file(self, tmp_path):
        detach.write_run_ended("abc123", tmp_path, "stopped")
        ended = tmp_path / ".forge" / "runs" / "abc123.ended"
        assert ended.exists()
        assert ended.read_text() == "stopped"

    def test_default_outcome_is_stopped(self, tmp_path):
        detach.write_run_ended("abc123", tmp_path)
        assert (tmp_path / ".forge" / "runs" / "abc123.ended").read_text() == "stopped"

    def test_no_overwrite_by_default(self, tmp_path):
        detach.write_run_ended("abc123", tmp_path, "stopped")
        detach.write_run_ended("abc123", tmp_path, "completed")
        assert (tmp_path / ".forge" / "runs" / "abc123.ended").read_text() == "stopped"

    def test_force_overwrites(self, tmp_path):
        detach.write_run_ended("abc123", tmp_path, "stopped")
        detach.write_run_ended("abc123", tmp_path, "completed", force=True)
        assert (tmp_path / ".forge" / "runs" / "abc123.ended").read_text() == "completed"


class TestReadRunEnded:
    def test_returns_outcome_when_file_exists(self, tmp_path):
        detach.write_run_ended("abc123", tmp_path, "stopped")
        assert detach.read_run_ended("abc123", tmp_path) == "stopped"

    def test_returns_none_when_missing(self, tmp_path):
        assert detach.read_run_ended("no-such-run", tmp_path) is None


class TestReadRunStatus:
    def test_returns_stopped_when_ended_file_exists(self, tmp_path):
        detach.write_run_ended("abc123", tmp_path, "stopped")
        st = detach.read_run_status("abc123", "my-slug", tmp_path)
        assert st["phase"] == "STOPPED"

    def test_returns_completed_when_ended_file_says_completed(self, tmp_path):
        detach.write_run_ended("abc123", tmp_path, "completed")
        st = detach.read_run_status("abc123", "my-slug", tmp_path)
        assert st["phase"] == "COMPLETED"

    def test_returns_orphaned_when_no_pid_no_ended(self, tmp_path):
        # No PID file, no .ended — process exited without writing a terminal marker.
        log_dir = tmp_path / ".forge" / "logs" / "my-slug"
        log_dir.mkdir(parents=True)
        (log_dir / "run-abc123.log").write_text("some log content\n")
        st = detach.read_run_status("abc123", "my-slug", tmp_path)
        assert st["phase"] == "ORPHANED"

    def test_returns_running_when_pid_file_exists(self, tmp_path):
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "abc123.pid").write_text(f"{os.getpid()}\nmy-slug\n")
        st = detach.read_run_status("abc123", "my-slug", tmp_path)
        assert st["phase"] == "RUNNING"

    def test_ended_takes_priority_over_pid_file(self, tmp_path):
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "abc123.pid").write_text(f"{os.getpid()}\nmy-slug\n")
        detach.write_run_ended("abc123", tmp_path, "stopped")
        st = detach.read_run_status("abc123", "my-slug", tmp_path)
        assert st["phase"] == "STOPPED"


class TestInstallCleanupHandlerWritesEnded:
    def test_sigterm_writes_ended_and_removes_pid(self, tmp_path):
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        pid_file = runs_dir / "abc123.pid"
        pid_file.write_text(f"{os.getpid()}\nmy-slug\n")

        import signal

        detach.install_cleanup_handler("abc123", tmp_path)
        try:
            # Simulate SIGTERM inline by calling the handler directly.
            signal.raise_signal(signal.SIGTERM)
        except SystemExit:
            pass

        assert not pid_file.exists()
        assert detach.read_run_ended("abc123", tmp_path) == "stopped"

        # Restore default SIGTERM handler to avoid interfering with other tests.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


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
