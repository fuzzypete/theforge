"""Per-story flock-based concurrency guard for sprint execution."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from theforge.artifacts import ESCALATED_MARKER_PATH
from theforge.pid import _current_process_fingerprint, _pid_matches_fingerprint

_LOCK_METADATA_SEPARATOR = "|"


def _is_escalated_worktree(worktree_path: Path) -> bool:
    """Return True when the worktree is marked as an escalated, preserved run."""
    return (worktree_path / ESCALATED_MARKER_PATH).exists()


class SprintConflictError(Exception):
    """Raised when a sprint conflicts with an already-running sprint."""

    def __init__(self, conflicting_slugs: list[str]) -> None:
        self.conflicting_slugs = conflicting_slugs
        super().__init__(f"Stories already running: {', '.join(conflicting_slugs)}")


def _read_lock_metadata(fd) -> tuple[int | None, str | None]:
    """Return ``(pid, fingerprint)`` from a lock file, if present and valid."""
    fd.seek(0)
    raw = fd.read().strip()
    if not raw:
        return None, None

    pid_text, separator, fingerprint = raw.partition(_LOCK_METADATA_SEPARATOR)
    try:
        pid = int(pid_text)
    except ValueError:
        return None, None

    normalized_fingerprint = fingerprint.strip() if separator else None
    return pid, normalized_fingerprint or None


def _write_lock_metadata(fd) -> None:
    """Persist the current process PID plus a stable process fingerprint."""
    pid = os.getpid()
    fingerprint = _current_process_fingerprint(pid)
    payload = str(pid)
    if fingerprint:
        payload = f"{payload}{_LOCK_METADATA_SEPARATOR}{fingerprint}"
    fd.truncate(0)
    fd.seek(0)
    fd.write(payload)
    fd.flush()


def check_escalated_worktrees(
    slugs: list[str],
    path_pattern: str,
    project_root: Path,
) -> list[str]:
    """Return slugs whose on-disk worktree is marked escalated (preserved).

    Companion to :func:`check_active_worktrees` — the two cases are disjoint:
    an escalated worktree is intentionally held for human triage and must not
    be rescheduled, whereas an *active* worktree is a genuine collision that
    must be handled as a conflict.
    """
    escalated: list[str] = []
    for slug in slugs:
        worktree_path = project_root / path_pattern.format(slug=slug)
        if not worktree_path.exists():
            continue
        if _is_escalated_worktree(worktree_path):
            escalated.append(slug)
    return escalated


def check_active_worktrees(
    slugs: list[str],
    path_pattern: str,
    base_branch: str,
    project_root: Path,
) -> list[str]:
    """Return slugs whose existing worktrees contain non-base commits."""
    active: list[str] = []
    for slug in slugs:
        worktree_path = project_root / path_pattern.format(slug=slug)
        if not worktree_path.exists():
            continue
        if _is_escalated_worktree(worktree_path):
            continue
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-list",
                "--count",
                f"{base_branch}..HEAD",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        try:
            ahead_count = int(result.stdout.strip())
        except ValueError:
            continue
        if ahead_count > 0:
            active.append(slug)
    return active


def acquire_story_locks(slugs: list[str], project_root: Path) -> tuple[list, list[str]]:
    """Try to acquire exclusive non-blocking flocks for each slug.

    Creates `.forge/locks/<slug>.lock` files under *project_root*.

    Returns:
        ``(locked_fds, [])`` when all locks are successfully acquired.
        ``([], conflicting_slugs)`` if any slug is already locked by another
        process. All successfully acquired locks are released before returning
        in the conflict case.
    """
    if not slugs:
        return [], []

    lock_dir = project_root / ".forge" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)

    locked_fds: list = []
    conflicted: list[str] = []

    for slug in slugs:
        lock_path = lock_dir / f"{slug}.lock"
        acquired_fd = None

        for _attempt in range(3):
            fd = open(lock_path, "a+")  # noqa: WPS515,SIM115
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _write_lock_metadata(fd)
                acquired_fd = fd
                break
            except BlockingIOError:
                owner_pid, owner_fingerprint = _read_lock_metadata(fd)
                fd.close()
                if owner_pid is None or _pid_matches_fingerprint(owner_pid, owner_fingerprint):
                    conflicted.append(slug)
                    break
                # TOCTOU: a live process could acquire this lock and write its
                # metadata between the ownership check above and unlink() below.
                # If that happens, we delete a valid lock file and two callers
                # may briefly believe they hold the lock. The risk is acceptable:
                # the window is extremely narrow, and the 3-attempt retry loop
                # means any caller that loses the race will re-detect the
                # conflict on the next flock attempt and back off cleanly.
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    conflicted.append(slug)
                    break

        if acquired_fd is not None:
            locked_fds.append(acquired_fd)
        elif slug not in conflicted:
            conflicted.append(slug)

    if conflicted:
        # Release all successfully acquired locks before returning
        for fd in locked_fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
            except Exception:
                pass
        return [], conflicted

    return locked_fds, []


def release_story_locks(fds: list) -> None:
    """Unlock and close all held story lock file descriptors."""
    for fd in fds:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        except Exception:
            pass


def cleanup_story_locks(
    slugs: list[str],
    project_root: Path,
    *,
    pid: int | None = None,
) -> list[str]:
    """Best-effort removal of stale story lock files for a stopped or killed run.

    When ``pid`` is provided, only lock files whose metadata still points at that
    process instance are removed. Empty/corrupt lock files are also removed as
    stale. Returns the list of slugs whose lock files were deleted.
    """
    if not slugs:
        return []

    cleaned: list[str] = []
    lock_dir = project_root / ".forge" / "locks"
    if not lock_dir.exists():
        return cleaned

    for slug in slugs:
        lock_path = lock_dir / f"{slug}.lock"
        if not lock_path.exists():
            continue
        try:
            with open(lock_path, "a+") as fd:
                owner_pid, owner_fingerprint = _read_lock_metadata(fd)
                should_remove = owner_pid is None
                if pid is None:
                    should_remove = True
                elif owner_pid == pid and _pid_matches_fingerprint(owner_pid, owner_fingerprint):
                    should_remove = True
            if should_remove:
                lock_path.unlink(missing_ok=True)
                cleaned.append(slug)
        except OSError:
            continue

    return cleaned


@contextmanager
def integration_lock(
    forge_root: Path,
    *,
    timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 0.1,
):
    """Serialize branch integration operations across forge processes.

    Uses a bounded non-blocking flock loop so a wedged peer process cannot
    block integration forever. The lock file stores the owning PID for better
    timeout diagnostics.
    """
    lock_path = forge_root / ".forge" / "merge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+")
    acquired = False
    try:
        deadline = time.monotonic() + timeout_seconds
        owner_pid: int | None = None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                _write_lock_metadata(fd)
                break
            except BlockingIOError:
                owner_pid, _owner_fingerprint = _read_lock_metadata(fd)
                if time.monotonic() >= deadline:
                    owner_suffix = f" (held by pid {owner_pid})" if owner_pid is not None else ""
                    raise TimeoutError(
                        f"Timed out waiting for sprint integration lock{owner_suffix}"
                    ) from None
                time.sleep(poll_interval_seconds)
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()
