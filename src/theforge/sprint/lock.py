"""Per-story flock-based concurrency guard for sprint execution."""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

from theforge.pid import _is_pid_alive


class SprintConflictError(Exception):
    """Raised when a sprint conflicts with an already-running sprint."""

    def __init__(self, conflicting_slugs: list[str]) -> None:
        self.conflicting_slugs = conflicting_slugs
        super().__init__(f"Stories already running: {', '.join(conflicting_slugs)}")


def _read_lock_pid(fd) -> int | None:
    """Return the lock owner PID from the file, if present and valid."""
    fd.seek(0)
    raw_pid = fd.read().strip()
    if not raw_pid:
        return None
    try:
        return int(raw_pid)
    except ValueError:
        return None


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
                fd.truncate(0)
                fd.seek(0)
                fd.write(str(os.getpid()))
                fd.flush()
                acquired_fd = fd
                break
            except BlockingIOError:
                owner_pid = _read_lock_pid(fd)
                fd.close()
                if owner_pid is None or _is_pid_alive(owner_pid):
                    conflicted.append(slug)
                    break
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
