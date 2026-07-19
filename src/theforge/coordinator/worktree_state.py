"""Worktree git-state consistency checker: a boundary invariant, not a command police.

A dev iteration has no legitimate need to mutate the branch state of its
worktree: the worktree is scratch space for committing work, and integrating
that work onto the base branch is the coordinator's job, done once at
integration time. Residue from a partially applied operation (an in-progress
rebase/merge/cherry-pick/revert/bisect), or a clean but illegitimate change to
HEAD or refs (a ``reset --hard`` / force-push that rewound the base, a
dev-introduced merge commit, a checkout onto the wrong branch), is corrupted
state that must never flow silently into review or integration.

This module provides a single stdlib-only checker used at two seams:

* the end of the DEV phase — fail-closed, attributed to DEV, before advancing;
* the start of integration's fetch/rebase — refusing inherited residue with a
  structured, phase-attributed diagnosis instead of git's raw
  "rebase-merge directory already exists" error.

It is pure process control: stdlib :mod:`subprocess` only, no coordinator
imports, no LLM, no prompt logic. Kept low-dependency like
:mod:`theforge.coordinator.commit_guard`.

Fail-open vs fail-closed mirrors ``commit_guard``: residue detection is
fail-closed (residue present ⇒ inconsistent), while the ancestry / merge-commit
/ branch checks fail-open on a git error so a transient git failure does not
spuriously escalate an otherwise healthy run.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# In-progress operation markers. Each is checked under the worktree's own git
# directory (resolved on-disk by :func:`_resolve_git_dir`) so the path lands
# inside ``.git/worktrees/<slug>/`` for a linked worktree rather than the shared
# ``.git`` — checking the wrong location would make the guard blind in exactly
# the worktree case it exists to protect.
_RESIDUE_MARKERS: tuple[tuple[str, str], ...] = (
    ("rebase-merge", "in-progress rebase (rebase-merge)"),
    ("rebase-apply", "in-progress rebase/am (rebase-apply)"),
    ("MERGE_HEAD", "in-progress merge (MERGE_HEAD)"),
    ("CHERRY_PICK_HEAD", "in-progress cherry-pick (CHERRY_PICK_HEAD)"),
    ("REVERT_HEAD", "in-progress revert (REVERT_HEAD)"),
    ("BISECT_LOG", "in-progress bisect (BISECT_LOG)"),
)


@dataclass(frozen=True)
class WorktreeStateResult:
    """Outcome of a worktree git-state consistency check.

    ``consistent`` is True when no inconsistency was found. When False,
    ``inconsistency`` names the offending state (a short human-readable label)
    and ``detail`` carries any additional context.
    """

    consistent: bool
    inconsistency: str | None = None
    detail: str | None = None


def _run_git(
    workspace_path: Path, args: list[str], timeout: int = 10
) -> subprocess.CompletedProcess | None:
    """Run a git command in ``workspace_path``; return None on any failure.

    A None return means the invocation could not be completed (git missing,
    timeout, not a repo). Callers decide fail-open vs fail-closed on None.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None


def _stdout_str(proc: subprocess.CompletedProcess) -> str:
    """Return a git process's stdout as a str, tolerating bytes.

    ``_run_git`` requests ``text=True`` so real invocations yield str, but a
    mocked ``subprocess.run`` may hand back bytes; coerce defensively so a test
    double (or an odd locale) cannot crash the checker into a hard failure that
    would be worse than the fail-open it is designed to degrade to.
    """
    out = proc.stdout
    if isinstance(out, bytes):
        return out.decode("utf-8", errors="replace")
    if isinstance(out, str):
        return out
    # None, or a non-string stub (e.g. a MagicMock): treat as empty rather than
    # letting a non-str value flow into int()/Path() and crash the check.
    return ""


def _resolve_git_dir(workspace_path: Path) -> Path:
    """Resolve the git directory backing ``workspace_path`` via the filesystem.

    In a normal checkout ``<workspace>/.git`` is a directory. In a linked
    worktree it is a *file* containing ``gitdir: <path>`` that points into
    ``<repo>/.git/worktrees/<slug>/`` — where this worktree's in-progress
    operation markers actually live. Resolving this on-disk (rather than via
    ``git rev-parse``) keeps the residue check free of subprocess: it cannot
    perturb call-counting test doubles and cannot be blinded by a mocked git.

    Falls back to ``<workspace>/.git`` when the pointer cannot be read, so a
    malformed ``.git`` file degrades to the conventional location rather than
    silently skipping the residue check.
    """
    dot_git = workspace_path / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return dot_git
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("gitdir:"):
                target = Path(line[len("gitdir:") :].strip())
                if not target.is_absolute():
                    target = (workspace_path / target).resolve()
                return target
    return dot_git


def _residue_inconsistency(workspace_path: Path) -> tuple[str, str] | None:
    """Return ``(label, detail)`` for the first operation-residue marker present.

    Fail-closed: residue present ⇒ inconsistent. Markers are located under the
    worktree's own git directory so a linked worktree's mid-operation state is
    detected, not the shared repo's. Returns None only when no residue is
    positively found.
    """
    git_dir = _resolve_git_dir(workspace_path)
    for name, label in _RESIDUE_MARKERS:
        marker = git_dir / name
        if marker.exists():
            return label, f"{name} present at {marker}"
    return None


def check_worktree_git_consistency(
    workspace_path: Path,
    expected_base_sha: str | None = None,
    base_branch: str | None = None,
    expected_branch_name: str | None = None,
) -> WorktreeStateResult:
    """Assert the worktree is in a consistent git state.

    Checks, in order (first inconsistency wins):

    1. **Operation residue** (fail-closed): an in-progress rebase / merge /
       cherry-pick / revert / bisect left in the worktree.
    2. **Base ancestry** (fail-open): when ``expected_base_sha`` is given, HEAD
       must still descend from it — catching a ``reset --hard`` / force-push
       that rewound or moved HEAD off the pre-dev base.
    3. **Dev-introduced merge commits** (fail-open): no merge commits in
       ``expected_base_sha..HEAD``.
    4. **Expected branch** (fail-open): when ``expected_branch_name`` is given,
       HEAD must be on that branch — catching a clean checkout/detach onto a
       different ref that would otherwise pass the ancestry and merge checks.

    ``base_branch`` is accepted for symmetry with the callers and is currently
    unused by the checks (ancestry is anchored to the concrete
    ``expected_base_sha``); it is kept in the signature so integration seams can
    pass it without a shape change if a future check needs it.

    The ancestry / merge / branch checks fail *open* (return consistent on a git
    error) so a transient git failure does not spuriously escalate a healthy
    run; residue detection fails *closed*.
    """
    _ = base_branch  # reserved; see docstring

    residue = _residue_inconsistency(workspace_path)
    if residue is not None:
        label, detail = residue
        return WorktreeStateResult(consistent=False, inconsistency=label, detail=detail)

    if expected_base_sha:
        ancestor = _run_git(
            workspace_path,
            ["merge-base", "--is-ancestor", expected_base_sha, "HEAD"],
        )
        # returncode 0 = ancestor, 1 = not ancestor, other/None = git error.
        if ancestor is not None and ancestor.returncode == 1:
            return WorktreeStateResult(
                consistent=False,
                inconsistency="HEAD no longer descends from the pre-dev base",
                detail=(
                    f"HEAD is not a descendant of {expected_base_sha} — a reset/force-push "
                    "rewound or moved the branch off its pre-dev base"
                ),
            )

        # ``git log --merges`` (not ``rev-list``): enumerate merge commit SHAs in
        # the range and count non-empty lines. A count > 0 means dev created a
        # merge commit, which the coordinator — the sole party that integrates
        # onto the base branch — must never inherit.
        merges = _run_git(
            workspace_path,
            ["log", "--merges", "--format=%H", f"{expected_base_sha}..HEAD"],
        )
        if merges is not None and merges.returncode == 0:
            merge_shas = [line for line in _stdout_str(merges).splitlines() if line.strip()]
            if merge_shas:
                return WorktreeStateResult(
                    consistent=False,
                    inconsistency="dev-introduced merge commit(s)",
                    detail=(
                        f"{len(merge_shas)} merge commit(s) in {expected_base_sha}..HEAD — "
                        "integrating onto the base branch is the coordinator's job, not dev's"
                    ),
                )

    if expected_branch_name:
        head_ref = _run_git(workspace_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        if head_ref is not None and head_ref.returncode == 0:
            current = _stdout_str(head_ref).strip()
            if current and current != expected_branch_name:
                return WorktreeStateResult(
                    consistent=False,
                    inconsistency="worktree on unexpected branch",
                    detail=(
                        f"HEAD is on '{current}', expected '{expected_branch_name}' — "
                        "dev moved the worktree off the coordinator's story branch"
                    ),
                )
        elif head_ref is not None and head_ref.returncode == 1:
            # Exit 1 from symbolic-ref --quiet ⇒ detached HEAD; a fatal error
            # (e.g. not a repo, exit 128) fails open instead of being misread as
            # detached. A detached HEAD is not the coordinator's expected branch.
            return WorktreeStateResult(
                consistent=False,
                inconsistency="worktree HEAD detached",
                detail=(
                    f"HEAD is detached, expected branch '{expected_branch_name}' — "
                    "dev detached the worktree off the coordinator's story branch"
                ),
            )

    return WorktreeStateResult(consistent=True)
