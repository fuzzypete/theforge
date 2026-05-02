"""Workspace hygiene gate.

The coordinator owns deterministic phase boundaries. Some phases (PREFLIGHT,
PLAN, PLAN_REVIEW, REVIEW) must not mutate the repo tree; others (DEV) own
mutation. This module provides the mechanical enforcement: snapshot the tree
before a phase, compare after, and either quarantine stray untracked files or
fail loudly with the offending paths.

Two layers:

1. ``check_phase_no_mutation``: invoked around PLAN / PLAN_REVIEW / REVIEW.
   Any change to the porcelain set fails the phase. ``.forge/`` is gitignored,
   so coordinator-driven artifact writes are naturally excluded.

2. ``enforce_pre_dev_hygiene``: invoked before DEV iteration 1. Unexpected
   untracked paths are moved to ``.forge/quarantine/<run-id>/`` (audited, not
   silently deleted) and DEV proceeds with a clean tree. If quarantine fails
   or modifications to tracked files appear, DEV is rejected with a diagnostic
   pointing at the offending paths instead of being handed to an agent that
   will fail mysteriously.

Sanctioned scratch space (``.forge/tmp/<run-id>/``) is provisioned by
``ensure_scratch_dir``; ``.forge/`` is already gitignored so the directory is
invisible to git.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Paths agents may legitimately leave at root without tripping the gate.
# Restrict to operator-facing work artifacts; everything else is quarantined.
_ALLOWED_ROOT_UNTRACKED: frozenset[str] = frozenset()


def snapshot_porcelain(workspace_path: Path) -> set[str]:
    """Return the porcelain entry set for ``workspace_path``.

    Each entry is the raw two-character status code plus the path
    (``"?? foo.py"``, ``" M src/x.py"``, ...). ``.forge/`` is gitignored so
    Forge-managed artifacts do not appear. On git failure returns an empty
    set; callers treat that as "no snapshot" rather than crashing.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    raw = proc.stdout.decode("utf-8", errors="replace")
    if not raw:
        return set()
    # -z entries are NUL-terminated; renames split across two NUL fields.
    return {entry for entry in raw.split("\0") if entry}


def _path_of(entry: str) -> str:
    """Extract the path portion of a porcelain entry (status is 3 chars)."""
    return entry[3:] if len(entry) > 3 else entry


def unexpected_entries(before: set[str], after: set[str]) -> list[str]:
    """Return entries present in ``after`` but not in ``before``, sorted by path."""
    new = after - before
    return sorted(new, key=_path_of)


def check_phase_no_mutation(
    workspace_path: Path,
    before: set[str],
    phase_name: str,
) -> tuple[bool, str | None, list[str]]:
    """Verify ``phase_name`` left the tree unchanged.

    Returns ``(ok, diagnostic, offending_paths)``. When ``ok`` is False, the
    diagnostic names the phase and lists every path the phase introduced or
    modified — operator-grade language, no silent failures.
    """
    after = snapshot_porcelain(workspace_path)
    new_entries = unexpected_entries(before, after)
    if not new_entries:
        return True, None, []
    paths = [_path_of(e) for e in new_entries]
    rendered = ", ".join(paths)
    diagnostic = (
        f"Workspace hygiene violation: phase {phase_name} is not authorized to mutate "
        f"the repo tree, but introduced unexpected paths: {rendered}. "
        "Remove or justify."
    )
    return False, diagnostic, paths


def ensure_scratch_dir(workspace_path: Path, run_id: str) -> Path:
    """Create ``.forge/tmp/<run-id>/`` and return it.

    ``.forge/`` is already gitignored, so the directory is invisible to git
    and safe for agents to use as exploratory scratch space.
    """
    scratch = workspace_path / ".forge" / "tmp" / run_id
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _quarantine_root(workspace_path: Path, run_id: str, iteration: int) -> Path:
    return workspace_path / ".forge" / "quarantine" / run_id / f"iter-{iteration}"


def quarantine_paths(
    workspace_path: Path,
    paths: list[str],
    quarantine_root: Path,
) -> tuple[list[str], list[str]]:
    """Move ``paths`` (workspace-relative) under ``quarantine_root``.

    Returns ``(moved, failed)``. Moves preserve the relative tree so the
    operator can locate originals. ``shutil.move`` is used so cross-device
    moves degrade to copy+delete cleanly.
    """
    quarantine_root.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    failed: list[str] = []
    for rel_path in paths:
        src = workspace_path / rel_path
        if not src.exists() and not src.is_symlink():
            # Path appeared in porcelain but is gone now (race or untracked dir
            # that already lost its entries). Treat as moved — no work to do.
            moved.append(rel_path)
            continue
        dest = quarantine_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            moved.append(rel_path)
        except OSError:
            failed.append(rel_path)
    return moved, failed


def enforce_pre_dev_hygiene(
    workspace_path: Path,
    run_id: str,
    *,
    iteration: int,
) -> tuple[bool, str | None, dict]:
    """Reject or sanitise the worktree before DEV starts.

    Behaviour:
      - Untracked paths (status ``??``) outside the allow-list → quarantined
        to ``.forge/quarantine/<run-id>/iter-<n>/``. Audit-trail preserved,
        worktree returned to a clean baseline, DEV proceeds. This is the
        primary motivator: stray scratch at repo root that silently sabotages
        dev runs (issue #1179).
      - Modified tracked files (status M/A/D/...) → audited and left in
        place. These are legitimate worktree-reuse state (work-in-progress
        from a prior interrupted run); validate-phase's auto-commit owns
        cleanup. Silent quarantine of tracked changes would qualify as
        "forge ate my work".
      - If quarantine fails for any untracked path → reject with the
        offending paths so the operator knows what to clean up manually.

    Returns ``(ok, diagnostic, audit)`` where ``audit`` is always a dict with
    keys ``snapshot``, ``modified``, ``quarantined``, ``quarantine_dir``.
    """
    snapshot = snapshot_porcelain(workspace_path)
    audit: dict = {
        "snapshot": sorted(snapshot),
        "modified": [],
        "quarantined": [],
        "quarantine_dir": None,
    }
    if not snapshot:
        return True, None, audit

    untracked: list[str] = []
    modified: list[str] = []
    for entry in snapshot:
        # Porcelain format: XY␠path. "??" = untracked. Anything else = tracked
        # change (modified, added, deleted, renamed, copied, unmerged).
        status = entry[:2]
        path = _path_of(entry)
        if status == "??":
            if path in _ALLOWED_ROOT_UNTRACKED:
                continue
            untracked.append(path)
        else:
            modified.append(path)

    audit["modified"] = sorted(modified)

    if not untracked:
        return True, None, audit

    quarantine_dir = _quarantine_root(workspace_path, run_id, iteration)
    moved, failed = quarantine_paths(workspace_path, sorted(untracked), quarantine_dir)
    audit["quarantined"] = moved
    audit["quarantine_dir"] = str(quarantine_dir.relative_to(workspace_path))
    if failed:
        rendered = ", ".join(failed)
        diagnostic = (
            "Unexpected untracked files in worktree before DEV could not be "
            f"quarantined: {rendered}. Workspace hygiene gate refuses to hand a "
            "dirty tree to the dev agent. Resolve manually before retrying."
        )
        return False, diagnostic, audit

    return True, None, audit
