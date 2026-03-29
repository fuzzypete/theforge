"""Per-story flock-based concurrency guard for sprint execution."""

from __future__ import annotations

import fcntl
from pathlib import Path


class SprintConflictError(Exception):
    """Raised when a sprint conflicts with an already-running sprint."""

    def __init__(self, conflicting_slugs: list[str]) -> None:
        self.conflicting_slugs = conflicting_slugs
        super().__init__(f"Stories already running: {', '.join(conflicting_slugs)}")


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
        fd = open(lock_path, "w")  # noqa: WPS515,SIM115
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked_fds.append(fd)
        except BlockingIOError:
            fd.close()
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
