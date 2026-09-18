"""Zero-commit guards: detect empty branches before they reach DONE.

A dev iteration that produces zero commits ahead of the base branch must not
flow through to APPROVE/DONE. The guards in this module are invoked at the
DEV → VALIDATE, VALIDATE → REVIEW, and REVIEW → DONE seams to escalate empty
runs early instead of letting integration silently skip PR creation.

This module also owns the checkpoint-commit that preserves a dev iteration's
uncommitted work before those guards run: work stranded as working-tree state is
invisible to every commit-reasoning mechanism, so the coordinator commits it
first (the agent may be gone — SIGKILL — but the coordinator owns the worktree).
:func:`preserve_dev_output` is the single entry point every exit seam uses — the
dev phase's own retry/escalate branches *and* the coordinator's phase-boundary
cancellation checks, because a sprint budget cap or operator stop can end a
*successful* iteration in the window before VALIDATE's post-gate sweep would
have committed anything (#3059).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Fixed subject marking a coordinator checkpoint commit. Kept distinct from any
# normal dev commit message so the commit is identifiable in history and audit.
CHECKPOINT_COMMIT_SUBJECT = "chore(checkpoint): coordinator-preserved dev iteration work"

#: Outcomes of :func:`preserve_dev_output`. A preservation attempt has three
#: materially different endings and callers (and the audit trail) must be able to
#: tell them apart: work was committed, there was no real work to commit, or
#: there *was* real work and the commit did not happen.
PRESERVE_COMMITTED = "committed"
PRESERVE_NOTHING = "nothing_to_preserve"
PRESERVE_FAILED = "failed"
PRESERVE_UNSAFE = "unsafe_git_state"

#: Transient coordinator state directory, never part of preserved dev work.
_FORGE_DIR = ".forge"

#: Marker files/directories whose presence in the git dir means a multi-step git
#: operation is mid-flight. Committing into one of those states would write a
#: commit the operator did not ask for onto a ref the coordinator has already
#: declared inconsistent, so preservation refuses instead.
_GIT_OPERATION_MARKERS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


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


def _dirty_work_file_count(workspace_path: Path) -> int:
    """How many dirty entries git reports outside the transient ``.forge`` dir.

    Answers "is there real work to preserve here?" — the question
    :func:`_worktree_has_changes` cannot answer on its own, because a worktree
    dirty only with coordinator state (traces, handoff, sessions) is dirty
    without holding any dev work. The exclusion is done by git pathspec rather
    than by parsing porcelain output, so quoted and renamed paths need no special
    handling. Returns 0 on any git error: a workspace git cannot read has nothing
    that can be preserved, which is not the same as a failed preservation.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", ".", f":(exclude){_FORGE_DIR}"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return 0
    if proc.returncode != 0:
        return 0
    try:
        return len((proc.stdout or b"").strip().splitlines())
    except Exception:  # noqa: BLE001
        return 0


def _git_operation_in_progress(workspace_path: Path) -> bool:
    """True when the worktree is mid-rebase/merge/cherry-pick/revert or detached.

    Fail-open (returns False) on any git error: the caller's next step is a
    checkpoint commit, and that already fails closed on a broken repository.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return False
        git_dir = Path(proc.stdout.strip())
        if any((git_dir / marker).exists() for marker in _GIT_OPERATION_MARKERS):
            return True
        head = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Detached HEAD: a commit here would not land on the story branch at all.
        return head.returncode != 0
    except Exception:  # noqa: BLE001
        return False


def preserve_dev_output(
    workspace_path: Path,
    reason: str,
    *,
    log_fn: Callable[[str], None] | None = None,
    logger: Any = None,
    iteration: int | None = None,
) -> str:
    """Preserve a dev iteration's uncommitted work as a checkpoint commit.

    The single entry point for "this dev iteration is ending — do not let its
    work end as working-tree state". Called at every seam where an iteration's
    work stops being extended: the dev phase's own retry/escalate decisions and
    the coordinator's phase-boundary cancellation checks (sprint budget cap,
    operator stop). A preserved worktree that still carries uncommitted dev
    output is invisible to the zero-commit guard, to the next iteration, to
    integration, and to review — which reads untracked files at re-entry as
    foreign content (#3059).

    Returns one of :data:`PRESERVE_COMMITTED`, :data:`PRESERVE_NOTHING`,
    :data:`PRESERVE_FAILED`, or :data:`PRESERVE_UNSAFE`. A failed preservation
    over a genuinely dirty worktree is the one outcome an operator must be able
    to see, so it is logged as a warning naming the workspace and emitted as its
    own ``dev_checkpoint_commit_failed`` event rather than being indistinguishable
    from "nothing to commit".
    """

    def _log(message: str) -> None:
        if log_fn is not None:
            log_fn(message)

    dirty = _dirty_work_file_count(workspace_path)
    if not dirty:
        return PRESERVE_NOTHING

    if _git_operation_in_progress(workspace_path):
        _log(
            f"  ⚠ DEV   {dirty} uncommitted file(s) left in {workspace_path} — "
            "NOT checkpoint-committed: the worktree is mid git operation or on a "
            "detached HEAD, so a commit would not land on the story branch"
        )
        if logger:
            logger._safe_emit(
                "dev_checkpoint_commit_failed",
                phase="DEV",
                iteration=iteration,
                reason=reason,
                workspace=str(workspace_path),
                dirty_file_count=dirty,
                detail="git operation in progress or detached HEAD",
            )
        return PRESERVE_UNSAFE

    if _checkpoint_commit(workspace_path, reason):
        _log(f"  ⎇ DEV   checkpoint-committed stranded work ({reason})")
        if logger:
            logger._safe_emit(
                "dev_checkpoint_commit",
                phase="DEV",
                iteration=iteration,
                reason=reason,
            )
        return PRESERVE_COMMITTED

    _log(
        f"  ⚠ DEV   FAILED to checkpoint-commit {dirty} uncommitted file(s) in "
        f"{workspace_path} ({reason}) — dev work is stranded as working-tree state"
    )
    if logger:
        logger._safe_emit(
            "dev_checkpoint_commit_failed",
            phase="DEV",
            iteration=iteration,
            reason=reason,
            workspace=str(workspace_path),
            dirty_file_count=dirty,
            detail="checkpoint commit did not complete",
        )
    return PRESERVE_FAILED
