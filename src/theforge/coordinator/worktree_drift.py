"""Classification for reused worktrees whose base branch moved underneath them.

Forge deliberately preserves a story worktree when the story escalates (the
escalate marker in ``artifacts``), because the work in it may still be wanted.
Nothing reconciles that worktree against the base branch afterwards, so a later
resume rebases it onto a base that may have advanced over the same files. When
that rebase conflicts, the condition is not "a git command failed" — it is
"preserved work predates commits that changed the same files", a recurring and
expected consequence of escalating stories and keeping their output (#1993).

This module turns that condition into a named classification: which files both
sides touched, which base-branch commits are responsible, and which resolutions
are actually available to the operator. It reads git state only; it never
mutates the repository.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from theforge.artifacts import ESCALATED_MARKER_PATH

from . import util as _cu

# First line of every message this module produces. ``engine`` matches on it to
# avoid burying the classification under a generic "Workspace creation failed:"
# prefix, so keep the two in sync through ``is_drift_classification``.
DRIFT_HEADER = "preserved work predates conflicting commits"

# Cap the evidence quoted back so a wide merge does not produce an unreadable wall.
_MAX_FILES = 20
_MAX_COMMITS = 10

# git's own remediation advice ("git add/rm", "git rebase --continue") tells the
# operator to hand-resolve conflicts inside a story worktree, which the
# surrounding workflow does not support. Drop it; keep the factual first line.
_HINT_RE = re.compile(r"^\s*hint:", re.IGNORECASE)


@dataclass(frozen=True)
class WorktreeDrift:
    """What a failed rebase of a reused worktree tells us about the two sides."""

    base_branch: str
    base_ref: str
    escalated: bool
    worktree_commits: int | None = None
    base_commits: int | None = None
    overlapping_files: list[str] = field(default_factory=list)
    responsible_commits: list[str] = field(default_factory=list)
    files_truncated: int = 0
    commits_truncated: int = 0


def is_drift_classification(message: str) -> bool:
    """True when ``message`` was produced by :func:`classify_rebase_conflict`."""
    return message.startswith(DRIFT_HEADER)


def _git_lines(command: str, cwd: Path) -> list[str] | None:
    ok, out = _cu._run_shell(command, cwd)
    if not ok:
        return None
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _git_count(command: str, cwd: Path) -> int | None:
    lines = _git_lines(command, cwd)
    if not lines:
        return None
    try:
        return int(lines[0])
    except ValueError:
        return None


def _resolve_base_ref(workspace_path: Path, base_branch: str) -> str:
    """Prefer the remote-tracking ref the rebase used; fall back to the local branch."""
    ok, _ = _cu._run_shell(
        f"git rev-parse --verify --quiet origin/{base_branch}^{{commit}}", workspace_path
    )
    return f"origin/{base_branch}" if ok else base_branch


def collect_drift(workspace_path: Path, base_branch: str) -> WorktreeDrift:
    """Gather the two-sided divergence evidence for a worktree that failed to rebase.

    Every probe degrades independently: a git command that cannot answer leaves
    its field empty rather than aborting the classification, because a partial
    explanation still beats forwarding raw rebase output.
    """
    base_ref = _resolve_base_ref(workspace_path, base_branch)
    escalated = (workspace_path / ESCALATED_MARKER_PATH).exists()

    merge_base_lines = _git_lines(f"git merge-base {base_ref} HEAD", workspace_path)
    if not merge_base_lines:
        return WorktreeDrift(base_branch=base_branch, base_ref=base_ref, escalated=escalated)
    merge_base = merge_base_lines[0]

    ours = _git_lines(f"git diff --name-only {merge_base}..HEAD", workspace_path) or []
    theirs = _git_lines(f"git diff --name-only {merge_base}..{base_ref}", workspace_path) or []
    overlap = sorted(set(ours) & set(theirs))

    responsible: list[str] = []
    commits_truncated = 0
    if overlap:
        paths = " ".join(shlex.quote(p) for p in overlap)
        log = (
            _git_lines(
                f"git log --oneline --no-decorate {merge_base}..{base_ref} -- {paths}",
                workspace_path,
            )
            or []
        )
        if len(log) > _MAX_COMMITS:
            commits_truncated = len(log) - _MAX_COMMITS
        responsible = log[:_MAX_COMMITS]

    files_truncated = max(0, len(overlap) - _MAX_FILES)

    return WorktreeDrift(
        base_branch=base_branch,
        base_ref=base_ref,
        escalated=escalated,
        worktree_commits=_git_count(f"git rev-list --count {merge_base}..HEAD", workspace_path),
        base_commits=_git_count(f"git rev-list --count {merge_base}..{base_ref}", workspace_path),
        overlapping_files=overlap[:_MAX_FILES],
        responsible_commits=responsible,
        files_truncated=files_truncated,
        commits_truncated=commits_truncated,
    )


def _summarize_git_error(rebase_error: str) -> str:
    """Keep git's factual first line, drop its manual-resolution advice."""
    lines = [ln.rstrip() for ln in (rebase_error or "").splitlines() if ln.strip()]
    kept = [ln for ln in lines if not _HINT_RE.match(ln)]
    if not kept:
        return ""
    first = kept[0]
    return first if len(first) <= 200 else first[:197] + "..."


def render_drift(drift: WorktreeDrift, workspace_path: Path, rebase_error: str) -> str:
    """Render the operator-facing classification for a drifted worktree."""
    origin = "escalated" if drift.escalated else "reused"
    held = (
        f"{drift.worktree_commits} commit{'s' if drift.worktree_commits != 1 else ''}"
        if drift.worktree_commits is not None
        else "work"
    )
    landed = (
        f"{drift.base_commits} commit{'s' if drift.base_commits != 1 else ''}"
        if drift.base_commits is not None
        else "commits"
    )

    lines = [
        f"{DRIFT_HEADER} on {drift.base_branch}",
        "",
        f"The preserved {origin} worktree at {workspace_path} holds {held} made before"
        f" {landed} landed on {drift.base_branch}. Replaying the preserved work on"
        f" top of {drift.base_ref} conflicts, so this story cannot resume from it as-is."
        " The worktree itself is intact — the rebase was aborted and nothing was lost.",
    ]

    lines += ["", "Files changed on both sides:"]
    lines += [f"  - {p}" for p in drift.overlapping_files]
    if drift.files_truncated:
        lines.append(f"  - ... and {drift.files_truncated} more")

    if drift.responsible_commits:
        lines += ["", f"{drift.base_branch} commits that changed those files:"]
        lines += [f"  - {c}" for c in drift.responsible_commits]
        if drift.commits_truncated:
            lines.append(f"  - ... and {drift.commits_truncated} more")

    lines += [
        "",
        "Available resolutions:",
        f"  - Bring the work forward yourself: rebase {workspace_path} onto"
        f" {drift.base_ref} (git rebase {drift.base_ref}) outside forge, resolve the"
        " conflicts there, commit, then resume the sprint — the resume will find the"
        " worktree already current.",
        "  - Discard the preserved work and re-run the story from current"
        f" {drift.base_branch}: git worktree remove --force {workspace_path} (and delete"
        " its branch), then resume — the run recreates the worktree from"
        f" {drift.base_ref}. Any work in it is lost.",
        f"  - Leave it and drop the story from the sprint if the {drift.base_branch}"
        " commits above already supersede the preserved work.",
        "",
        "Resolving the conflicts in place with git rebase --continue is not a supported"
        " operator action here: the run does not resume a worktree left mid-rebase.",
    ]

    summary = _summarize_git_error(rebase_error)
    if summary:
        lines += ["", f"git reported: {summary}"]

    return "\n".join(lines)


def classify_rebase_conflict(
    workspace_path: Path, base_branch: str, rebase_error: str
) -> str | None:
    """Classify a failed pre-dev rebase of a reused worktree as base-branch drift.

    Returns ``None`` when drift cannot be established — a rebase can also fail
    for reasons that have nothing to do with the base moving (an unreachable
    remote, an unreadable repository), and claiming drift for those would
    mislabel the failure. Callers fall back to reporting the tool failure.
    """
    drift = collect_drift(workspace_path, base_branch)
    if not drift.overlapping_files:
        return None
    return render_drift(drift, workspace_path, rebase_error)
