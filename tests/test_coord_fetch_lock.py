"""Tests for fetch serialization via FETCH_LOCK.

Covers:
- pull_base_branch() from two threads simultaneously — FETCH_LOCK serializes calls
- _rebase_onto_main() with "incorrect old value provided" fetch failure — lock is
  released and function returns (False, err) without hanging
- pull_base_branch() retries once when "incorrect old value" appears in pull_out
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

from coord_test_helpers import _make_config

# ── pull_base_branch concurrent serialization ────────────────────────────────


class TestPullBaseBranchFetchLock:
    """FETCH_LOCK serializes git fetch/pull calls across concurrent threads."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_concurrent_pulls_both_succeed(self, mock_log, mock_shell, tmp_path):
        """Two threads calling pull_base_branch simultaneously both succeed.

        Replaces FETCH_LOCK with a spy wrapper to confirm concurrent entry
        never occurs (lock held by at most one thread at a time).
        """
        import theforge.coordinator.git_lock as git_lock_mod
        import theforge.coordinator.workspace as ws_mod
        from theforge.coordinator.workspace import pull_base_branch

        config = _make_config(tmp_path)
        base = config.workspace.base_branch

        concurrency_detected = threading.Event()
        inside_count = [0]
        count_lock = threading.Lock()

        class SpyLock:
            """Wraps a real Lock and detects concurrent held state."""

            def __init__(self):
                self._lock = threading.Lock()

            def acquire(self, blocking=True, timeout=-1):
                result = self._lock.acquire(blocking=blocking, timeout=timeout)
                if result:
                    with count_lock:
                        inside_count[0] += 1
                        if inside_count[0] > 1:
                            concurrency_detected.set()
                return result

            def release(self):
                with count_lock:
                    inside_count[0] -= 1
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *args):
                self.release()

        spy = SpyLock()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse HEAD:src/theforge" in cmd:
                return (True, "abc123")
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, base)
            if "pull --ff-only" in cmd or ("fetch origin" in cmd and ":" in cmd):
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        errors = []

        def run_pull():
            try:
                pull_base_branch(config)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with (
            patch.object(git_lock_mod, "FETCH_LOCK", spy),
            patch.object(ws_mod, "FETCH_LOCK", spy),
        ):
            t1 = threading.Thread(target=run_pull)
            t2 = threading.Thread(target=run_pull)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert not t1.is_alive(), "Thread 1 hung — possible deadlock"
        assert not t2.is_alive(), "Thread 2 hung — possible deadlock"
        assert not errors, f"Unexpected errors: {errors}"
        assert not concurrency_detected.is_set(), "FETCH_LOCK held by two threads at once"

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_incorrect_old_value_triggers_retry(self, mock_log, mock_shell, tmp_path):
        """When first fetch fails with 'incorrect old value', a retry is attempted."""
        from theforge.coordinator.workspace import pull_base_branch

        config = _make_config(tmp_path)
        fetch_call_count = [0]

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse HEAD:src/theforge" in cmd:
                return (True, "abc123")
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/other")  # NOT on base → uses fetch refspec
            if "fetch origin" in cmd and ":" in cmd:
                fetch_call_count[0] += 1
                if fetch_call_count[0] == 1:
                    # First attempt: race error
                    return (False, "error: incorrect old value provided")
                # Second attempt: success
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        with patch("theforge.coordinator.workspace.time.sleep"):
            result = pull_base_branch(config)

        assert result is True
        assert fetch_call_count[0] == 2, "Expected exactly 2 fetch attempts (original + retry)"


# ── _rebase_onto_main lock-release on fetch failure ──────────────────────────


class TestRebaseOntoMainFetchLock:
    """FETCH_LOCK is always released even when fetch fails."""

    def test_incorrect_old_value_returns_false_err_and_releases_lock(self, tmp_path):
        """When fetch returns 'incorrect old value', function returns (False, err).

        The lock must be released after the call so subsequent callers are not blocked.
        """
        from theforge.coordinator import git_lock
        from theforge.coordinator.run_setup import _rebase_onto_main

        # Create a fake .git dir so the git_dir.exists() guard passes
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        race_err = (
            "error: fetching ref refs/remotes/origin/main failed: incorrect old value provided"
        )

        fetch_result = SimpleNamespace(returncode=1, stderr=race_err, stdout="")

        with patch("theforge.coordinator.run_setup.subprocess.run", return_value=fetch_result):
            ok, err = _rebase_onto_main(str(tmp_path), "main", logger=None)

        assert ok is False
        assert "incorrect old value" in err

        # Lock must be released — acquire/release must succeed immediately
        acquired = git_lock.FETCH_LOCK.acquire(blocking=False)
        assert acquired, "FETCH_LOCK was not released after fetch failure"
        git_lock.FETCH_LOCK.release()

    def test_fetch_success_proceeds_to_rebase(self, tmp_path):
        """When fetch succeeds, rebase is attempted on the worktree branch."""
        from theforge.coordinator.run_setup import _rebase_onto_main

        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        fetch_ok = SimpleNamespace(returncode=0, stderr="", stdout="")
        rebase_ok = SimpleNamespace(returncode=0, stderr="", stdout="")
        # merge-base --is-ancestor returns 1 (not an ancestor) so the branch is
        # NOT already-integrated and the rebase short-circuit (issue #1794) is
        # skipped — this test exercises the fetch-then-rebase path.
        not_ancestor = SimpleNamespace(returncode=1, stderr="", stdout="")

        call_log = []

        def fake_run(cmd, **kwargs):
            call_log.append(cmd[1])  # capture git subcommand
            if cmd[1] == "fetch":
                return fetch_ok
            if cmd[1] == "merge-base":
                return not_ancestor
            return rebase_ok

        with patch("theforge.coordinator.run_setup.subprocess.run", side_effect=fake_run):
            ok, err = _rebase_onto_main(str(tmp_path), "main", logger=None)

        assert ok is True
        assert err == ""
        assert "fetch" in call_log
        assert "rebase" in call_log
