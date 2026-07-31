"""What a story that was never scheduled left behind in its worktree.

A story dropped at launch is normally a story that never ran: nothing was spent,
nothing was produced, and its worktree is disposable. That assumption is load
bearing in two places — the audit records the drop at ``cost_usd: 0.0``, and RCA
recommends clearing the worktree — and when it is wrong, both cause harm: a run
that did work is reported as free, and the operator is advised to delete the only
copy of it (#2079).

This module answers the question those two places should have asked first: does
the dropped story's worktree hold work? It reports commits ahead of the base
branch and uncommitted changes, and it fails *closed* — a git lookup that cannot
be completed yields ``None``, meaning "unknown", never ``0``. Callers must treat
unknown as "there may be work here", because the cost of the opposite mistake is
destroyed work.

Stdlib-only, per convention 4.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WorktreeWork",
    "describe_worktree_work",
    "inspect_worktree_work",
]

_GIT_TIMEOUT = 10


@dataclass(frozen=True)
class WorktreeWork:
    """Evidence of work held in a story worktree that this sprint did not run."""

    slug: str
    path: str | None = None
    branch: str | None = None
    exists: bool = False
    #: Commits on the story branch not reachable from base. ``None`` = unknown.
    commits_ahead: int | None = None
    #: Uncommitted changes present. ``None`` = unknown.
    dirty: bool | None = None

    @property
    def determined(self) -> bool:
        """True only when *both* forms of work were actually established.

        Committed and uncommitted work are two independent ways a worktree can
        hold the only copy of something. Ruling out one says nothing about the
        other, so a zero commit count paired with an unreadable ``git status`` is
        not a worktree known to be empty — treating it as one is the same
        unknown-means-nothing inference this whole change exists to remove.
        """
        return self.commits_ahead is not None and self.dirty is not None

    @property
    def has_work(self) -> bool:
        """True only when work is *positively confirmed* present."""
        return bool(self.commits_ahead) or self.dirty is True

    @property
    def may_have_work(self) -> bool:
        """True when work is confirmed present, or could not be ruled out.

        The predicate every destructive decision must consult: a worktree whose
        state could not be read is not a worktree known to be empty. Absence is
        reported only through ``determined`` — a worktree that does not exist
        sets both counters explicitly rather than leaving them unknown — so no
        unreadable case can reach this as a False.
        """
        return self.has_work or not self.determined

    def as_state_fields(self) -> dict[str, object]:
        """Fields recorded on the story's audit/state entry."""
        return {
            "worktree": self.path,
            "branch": self.branch,
            "unmerged_commits": self.commits_ahead,
            "unmerged_work_determined": self.determined,
            "worktree_dirty": self.dirty,
        }


def _git_out(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 - any git failure is "unknown", not "none"
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _commits_ahead(worktree: Path, base_branch: str) -> int | None:
    for ref in (f"origin/{base_branch}", base_branch):
        out = _git_out(["rev-list", "--count", f"{ref}..HEAD"], worktree)
        if out is None:
            continue
        try:
            return int(out.strip() or "0")
        except ValueError:
            continue
    return None


def _is_dirty(worktree: Path) -> bool | None:
    out = _git_out(["status", "--porcelain"], worktree)
    if out is None:
        return None
    return bool(out.strip())


def inspect_worktree_work(
    slug: str,
    *,
    project_root: Path,
    path_pattern: str,
    base_branch: str,
    branch_pattern: str | None = None,
) -> WorktreeWork:
    """Inspect *slug*'s worktree for work this sprint is about to write off.

    Never raises: a malformed pattern, a missing directory, or a git failure all
    resolve to a ``WorktreeWork`` whose unknowns stay unknown.
    """
    branch: str | None = None
    if branch_pattern:
        try:
            branch = branch_pattern.format(slug=slug)
        except (IndexError, KeyError):
            branch = None
    try:
        worktree = (Path(project_root) / path_pattern.format(slug=slug)).resolve()
    except (IndexError, KeyError, OSError, ValueError):
        return WorktreeWork(slug=slug, branch=branch)

    try:
        exists = worktree.is_dir()
    except OSError:
        # Whether there is a worktree at all is itself unknown — leave every
        # counter unset so the caller treats it as possibly holding work.
        return WorktreeWork(slug=slug, path=str(worktree), branch=branch, exists=True)
    if not exists:
        # No worktree: nothing was left behind, and that is a determined answer.
        return WorktreeWork(
            slug=slug,
            path=str(worktree),
            branch=branch,
            exists=False,
            commits_ahead=0,
            dirty=False,
        )

    return WorktreeWork(
        slug=slug,
        path=str(worktree),
        branch=branch,
        exists=True,
        commits_ahead=_commits_ahead(worktree, base_branch),
        dirty=_is_dirty(worktree),
    )


def describe_worktree_work(work: WorktreeWork) -> str | None:
    """An operator-facing description of the work at risk, or None if there is none.

    Deliberately concrete — how much work, on which branch, in which directory —
    so the audit record for a dropped story names what it dropped instead of
    echoing whatever text happened to be nearby.
    """
    if not work.may_have_work:
        return None
    where = f"branch {work.branch}" if work.branch else "its branch"
    if work.commits_ahead:
        head = f"{work.commits_ahead} unmerged commit(s) on {where}"
    elif work.determined:
        head = f"uncommitted changes on {where}"
    elif work.commits_ahead is None:
        head = f"unread worktree state on {where} (git could not be queried)"
    else:
        # Commits ruled out, uncommitted changes not: still possibly the only
        # copy of something.
        head = f"unread uncommitted state on {where} (no commits ahead; git status failed)"
    if work.dirty and work.commits_ahead:
        head += " plus uncommitted changes"
    if work.path:
        head += f", preserved at {work.path}"
    return head
