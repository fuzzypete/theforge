"""Zero-commit guards: detect empty branches before they reach DONE.

A dev iteration that produces zero commits ahead of the base branch must not
flow through to APPROVE/DONE. The guards in this module are invoked at the
DEV → VALIDATE, VALIDATE → REVIEW, and REVIEW → DONE seams to escalate empty
runs early instead of letting integration silently skip PR creation.

This module also owns the checkpoint-commit that preserves a killed/failed dev
iteration's uncommitted work before those guards run: work stranded as
working-tree state is invisible to every commit-reasoning mechanism, so the
coordinator commits it first (the agent may be gone — SIGKILL — but the
coordinator owns the worktree).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Fixed subject marking a coordinator checkpoint commit. Kept distinct from any
# normal dev commit message so the commit is identifiable in history and audit.
CHECKPOINT_COMMIT_SUBJECT = "chore(checkpoint): coordinator-preserved dev iteration work"


def _has_commits_ahead_of_base(workspace_path: Path, base_branch: str) -> bool:
    """Return True if HEAD has commits not reachable from base_branch.

    Tries `origin/{base_branch}` first, then falls back to the local ref. If
    both git invocations fail, returns True (fail-open) so transient git
    failures do not trigger spurious escalations on otherwise healthy runs.
    """
    for ref in (f"origin/{base_branch}", base_branch):
        try:
            proc = subprocess.run(
                ["git", "rev-list", "--count", f"{ref}..HEAD"],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            continue
        if proc.returncode != 0:
            continue
        try:
            return int(proc.stdout.strip() or "0") > 0
        except ValueError:
            continue
    return True


def _commits_exist_strict(workspace_path: Path, base_branch: str) -> bool:
    """Return True only when commits are positively confirmed ahead of base_branch.

    Fail-closed: returns False on any git error or when the workspace is not a
    real git repository. Used where a false positive (treating a no-commit
    state as having commits) would be harmful — specifically, when deciding
    whether to skip a post-hoc budget escalation for a run that may have
    produced no observable work.
    """
    for ref in (f"origin/{base_branch}", base_branch):
        try:
            proc = subprocess.run(
                ["git", "rev-list", "--count", f"{ref}..HEAD"],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            continue
        if proc.returncode != 0:
            continue
        try:
            return int(proc.stdout.strip() or "0") > 0
        except ValueError:
            continue
    return False


def _worktree_has_changes(workspace_path: Path) -> bool:
    """Return True if the worktree has any uncommitted changes.

    Covers tracked edits, staged content, and untracked files (anything
    ``git status --porcelain`` reports). Fail-closed: returns False on any git
    error. A checkpoint commit is only worth attempting when the worktree is
    positively confirmed dirty; if status cannot be read there is nothing safe
    to preserve, and returning False leaves the existing empty-diff guard's
    behaviour unchanged.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return False
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def _worktree_changed_since_commit(workspace_path: Path, base_commit: str | None) -> bool | None:
    """Return whether the worktree changed since ``base_commit``.

    ``base_commit`` is the HEAD captured immediately before the dev iteration
    started. Comparing against it answers a different question from
    ``_has_commits_ahead_of_base``: whether *this invocation* changed the
    workspace, even when preserved commits from an earlier run already leave the
    branch ahead of base.

    Returns ``None`` when git cannot answer safely (invalid/missing commit,
    non-repo path, command failure) so callers can fail open to their broader
    branch-level guard instead of falsely claiming "no changes".
    """
    if not isinstance(base_commit, str) or not base_commit.strip():
        return None
    try:
        diff = subprocess.run(
            ["git", "diff", "--quiet", base_commit, "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None
    if diff.returncode == 1:
        return True
    if diff.returncode != 0:
        return None
    return _worktree_has_changes(workspace_path)


def _checkpoint_commit(workspace_path: Path, reason: str) -> bool:
    """Commit the worktree's uncommitted changes as a coordinator checkpoint.

    Preserves whatever a killed or failed dev iteration produced as a real
    commit on the story branch, so every commit-reasoning mechanism — the
    zero-commit guard, the next iteration, integration, and the audit trail —
    can see it. The commit is authored by the coordinator process; a
    SIGKILLed agent cannot commit its own work.

    The entire transient ``.forge`` directory (traces, quarantine, handoff,
    sessions) is unstaged after staging so the checkpoint captures only real
    work. A commit is created only when there is staged content after that: a
    truly empty iteration produces no commit and this returns False, leaving the
    empty-diff guard to escalate as before. The subject is the fixed
    :data:`CHECKPOINT_COMMIT_SUBJECT` marker so the commit is distinguishable
    from a normal dev commit; ``reason`` (the failure detail) is recorded in the
    commit body. Returns True iff a commit was made.
    """
    try:
        add = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if add.returncode != 0:
            return False
        # Unstage the whole transient .forge directory (traces, quarantine,
        # handoff, sessions) that `git add -A` may have picked up when it is not
        # gitignored, so no forge state enters the checkpoint. No-op when
        # nothing under .forge is staged.
        subprocess.run(
            ["git", "reset", "-q", "--", ".forge"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
        )
        # Nothing staged after excluding .forge → no real work to preserve; do
        # not create an empty commit.
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
        )
        if staged.returncode == 0:
            return False
        body = (reason or "").strip()[:2000] or "no failure detail available"
        commit = subprocess.run(
            ["git", "commit", "-m", CHECKPOINT_COMMIT_SUBJECT, "-m", body],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return commit.returncode == 0
    except Exception:  # noqa: BLE001
        return False
