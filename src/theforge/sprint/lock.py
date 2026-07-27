"""Per-story flock-based concurrency guard for sprint execution."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from theforge import detach as _detach_mod
from theforge.artifacts import ESCALATED_MARKER_PATH
from theforge.pid import _current_process_fingerprint, _is_pid_alive, _pid_matches_fingerprint

_LOCK_METADATA_SEPARATOR = "|"
_UNAVAILABLE_FINGERPRINT = "unavailable"


def _is_escalated_worktree(worktree_path: Path) -> bool:
    """Return True when the worktree is marked as an escalated, preserved run."""
    return (worktree_path / ESCALATED_MARKER_PATH).exists()


def _remove_leftover_worktree_dir(path: Path) -> None:
    """Best-effort cleanup for leftover forge-only directories after removal."""
    if not path.exists():
        return
    forge_dir = path / ".forge"
    if not forge_dir.exists():
        return
    for child in path.iterdir():
        if child.name != ".forge":
            return
    shutil.rmtree(path, ignore_errors=True)


def _worktree_has_preserved_evidence(worktree_path: Path, base_branch: str) -> bool | None:
    """Return whether an escalated worktree contains commits or dirty files.

    Returns ``None`` when git state cannot be determined, so the caller can
    fail closed and keep the worktree preserved.
    """
    ahead_result = subprocess.run(
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
    if ahead_result.returncode != 0:
        return None
    try:
        ahead_count = int(ahead_result.stdout.strip())
    except ValueError:
        return None

    status_result = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if status_result.returncode != 0:
        return None

    return ahead_count > 0 or bool(status_result.stdout.strip())


def _cleanup_empty_worktree(
    worktree_path: Path,
    branch_name: str,
    project_root: Path,
) -> bool:
    """Remove an empty worktree/branch pair. Returns True on full success."""
    remove_result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if remove_result.returncode != 0:
        return False
    _remove_leftover_worktree_dir(worktree_path)

    branch_result = subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    return branch_result.returncode == 0


class SprintConflictError(Exception):
    """Raised when a sprint conflicts with an already-running sprint."""

    def __init__(self, conflicting_slugs: list[str]) -> None:
        self.conflicting_slugs = conflicting_slugs
        super().__init__(f"Stories already running: {', '.join(conflicting_slugs)}")


@dataclass(frozen=True)
class StoryLockRecord:
    """Parsed lock-file metadata."""

    pid: int | None
    timestamp: str | None
    fingerprint: str | None


@dataclass(frozen=True)
class StoryLockConflict:
    """Operator-facing description of a conflicting story lock."""

    slug: str
    lock_path: Path
    pid: int | None
    pid_alive: bool
    timestamp: str | None


def _read_lock_record(fd) -> StoryLockRecord:
    """Return parsed lock metadata from *fd*.

    Supported formats:

    - ``<pid>|<timestamp>|<fingerprint>`` (current)
    - ``<pid>|<fingerprint>`` (legacy)
    - ``<pid>`` (legacy/corrupt)
    """
    fd.seek(0)
    raw = fd.read().strip()
    if not raw:
        return StoryLockRecord(pid=None, timestamp=None, fingerprint=None)

    parts = [part.strip() for part in raw.split(_LOCK_METADATA_SEPARATOR)]
    try:
        pid = int(parts[0])
    except ValueError:
        return StoryLockRecord(pid=None, timestamp=None, fingerprint=None)

    if len(parts) >= 3:
        timestamp = parts[1] or None
        fingerprint = _LOCK_METADATA_SEPARATOR.join(parts[2:]).strip() or None
        return StoryLockRecord(pid=pid, timestamp=timestamp, fingerprint=fingerprint)

    if len(parts) == 2:
        legacy_field = parts[1] or None
        return StoryLockRecord(pid=pid, timestamp=legacy_field, fingerprint=legacy_field)

    return StoryLockRecord(pid=pid, timestamp=None, fingerprint=None)


def _read_lock_metadata(fd) -> tuple[int | None, str | None]:
    """Return ``(pid, fingerprint)`` from a lock file, if present and valid."""
    record = _read_lock_record(fd)
    return record.pid, record.fingerprint


def _write_lock_metadata(fd) -> None:
    """Persist the current process PID plus a stable process fingerprint."""
    pid = os.getpid()
    fingerprint = _current_process_fingerprint(pid) or _UNAVAILABLE_FINGERPRINT
    timestamp = time.ctime()
    payload = f"{pid}{_LOCK_METADATA_SEPARATOR}{timestamp}{_LOCK_METADATA_SEPARATOR}{fingerprint}"
    fd.truncate(0)
    fd.seek(0)
    fd.write(payload)
    fd.flush()


def _record_claims_live_holder(record: StoryLockRecord) -> bool:
    """Return True when *record* still points at a live owning process instance."""
    if record.pid is None:
        return False
    return _pid_matches_fingerprint(record.pid, record.fingerprint)


def _conflict_from_record(
    slug: str, lock_path: Path, record: StoryLockRecord
) -> StoryLockConflict:
    """Build a stable conflict description from lock metadata."""
    pid_alive = False if record.pid is None else _is_pid_alive(record.pid)
    return StoryLockConflict(
        slug=slug,
        lock_path=lock_path,
        pid=record.pid,
        pid_alive=pid_alive,
        timestamp=record.timestamp,
    )


def check_escalated_worktrees(
    slugs: list[str],
    path_pattern: str,
    branch_pattern: str,
    base_branch: str,
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
        if not _is_escalated_worktree(worktree_path):
            continue
        has_evidence = _worktree_has_preserved_evidence(worktree_path, base_branch)
        if has_evidence is True:
            escalated.append(slug)
            continue
        if has_evidence is None:
            escalated.append(slug)
            continue
        branch_name = branch_pattern.format(slug=slug)
        if not _cleanup_empty_worktree(worktree_path, branch_name, project_root):
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


def acquire_story_locks_detailed(
    slugs: list[str], project_root: Path
) -> tuple[list, list[StoryLockConflict]]:
    """Try to acquire exclusive non-blocking flocks for each slug.

    Creates `.forge/locks/<slug>.lock` files under *project_root*.

    Returns:
        ``(locked_fds, [])`` when all locks are successfully acquired.
        ``([], conflicting_locks)`` if any slug is already locked by another
        process. All successfully acquired locks are released before returning
        in the conflict case.
    """
    if not slugs:
        return [], []

    lock_dir = project_root / ".forge" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)

    locked_fds: list = []
    conflicted: list[StoryLockConflict] = []

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
                record = _read_lock_record(fd)
                fd.close()
                if record.pid is None or _record_claims_live_holder(record):
                    conflicted.append(_conflict_from_record(slug, lock_path, record))
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
                    conflicted.append(_conflict_from_record(slug, lock_path, record))
                    break

        if acquired_fd is not None:
            locked_fds.append(acquired_fd)
        elif all(conflict.slug != slug for conflict in conflicted):
            conflicted.append(
                StoryLockConflict(
                    slug=slug,
                    lock_path=lock_path,
                    pid=None,
                    pid_alive=False,
                    timestamp=None,
                )
            )

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


def acquire_story_locks(slugs: list[str], project_root: Path) -> tuple[list, list[str]]:
    """Backwards-compatible wrapper around :func:`acquire_story_locks_detailed`."""
    locked_fds, conflicts = acquire_story_locks_detailed(slugs, project_root)
    return locked_fds, [conflict.slug for conflict in conflicts]


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
    stale. If the owning PID matches ``pid`` but the process is no longer alive,
    the lock file is also removed so a freshly stopped run does not leave stale
    lock files behind. Returns the list of slugs whose lock files were deleted.
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
                elif owner_pid == pid:
                    if owner_fingerprint is None:
                        should_remove = True
                    else:
                        should_remove = not _detach_mod._is_pid_alive(
                            owner_pid
                        ) or _pid_matches_fingerprint(owner_pid, owner_fingerprint)
            if should_remove:
                lock_path.unlink(missing_ok=True)
                cleaned.append(slug)
        except OSError:
            continue

    return cleaned


def sweep_story_locks(
    project_root: Path,
    *,
    slugs: list[str] | None = None,
    exclude_slugs: set[str] | None = None,
) -> list[Path]:
    """Remove unlocked story lock files at sprint launch.

    Policy: any lock file in ``.forge/locks`` whose flock is not currently held
    at sprint launch is stale and is removed, regardless of whether its recorded
    PID is dead, recycled, or still alive. Live holders keep their flock and are
    left in place for the conflict gate to inspect precisely.

    ``exclude_slugs`` names stories whose lock file must survive the sweep even
    though nothing holds its flock. That combination is not a contradiction after
    a mid-run re-exec: ``os.execv`` closes the lock fd (and so releases the
    flock) while the story's agent process group keeps running, so an unheld lock
    is exactly what a live story looks like from the new process image.
    """
    lock_dir = project_root / ".forge" / "locks"
    if not lock_dir.exists():
        return []

    if slugs is None:
        lock_paths = sorted(lock_dir.glob("*.lock"))
    else:
        lock_paths = [lock_dir / f"{slug}.lock" for slug in slugs]
    if exclude_slugs:
        excluded_names = {f"{slug}.lock" for slug in exclude_slugs}
        lock_paths = [p for p in lock_paths if p.name not in excluded_names]

    removed: list[Path] = []
    for lock_path in lock_paths:
        if not lock_path.exists():
            continue
        try:
            fd = open(lock_path, "a+")  # noqa: WPS515,SIM115
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            # Unlink while still holding the flock so a concurrent acquirer
            # cannot take the lock on the file we are about to remove.
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                continue
            removed.append(lock_path)
        finally:
            fd.close()

    return removed


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
