"""Sprint DAG scheduler and story triage logic."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import ForgeConfig
from ..coordinator.audit import has_review_approve
from ..coordinator.gate import _run_gate
from ..task import TaskStory
from .manifest import _build_task_from_story


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


@dataclass
class StoryTriage:
    """Result of triaging a story for sprint resume."""

    story_path: str
    action: str  # "skip_merged", "skip", "review", "dev", "full"
    reason: str
    worktree_path: Path | None = None
    slug: str = ""


def _is_branch_merged(
    branch: str,
    base_branch: str,
    project_root: Path,
    slug: str | None = None,
) -> bool:
    """Return True if branch has been merged into base_branch.

    Two detection paths handle the two merge strategies theforge uses:

    1. Regular merge commit (git merge --no-edit fallback):
       --is-ancestor passes AND branch..base_branch count > 0 (base advanced
       past the branch tip via a merge commit).

    2. Fast-forward merge (git merge --ff-only, preferred):
       After an FF merge, branch and base point at the same commit, so
       branch..base_branch count == 0.  This is indistinguishable from a branch
       created at the current base HEAD using git state alone.  When slug is
       provided, the audit trail (has_review_approve) acts as the tiebreaker:
       a story that ran through the pipeline has an APPROVE record; a freshly
       created branch with no work does not.

    A branch that was merely created at base HEAD (count == 0, no audit entry)
    correctly returns False.
    """
    try:
        merge_result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_branch],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if merge_result.returncode == 0:
            # Count commits in base_branch NOT reachable from branch.
            ahead_result = subprocess.run(
                ["git", "rev-list", f"{branch}..{base_branch}", "--count"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            ahead_count = int(ahead_result.stdout.decode("utf-8", errors="replace").strip() or "0")
            if ahead_count > 0:
                # Distinguish a real regular merge from an abandoned branch whose
                # tip is merely behind base_branch. A merged branch must also have
                # unique commits that are now reachable from base_branch.
                unique_result = subprocess.run(
                    ["git", "rev-list", f"{base_branch}..{branch}", "--count"],
                    cwd=str(project_root),
                    capture_output=True,
                    timeout=30,
                )
                unique_count = int(
                    unique_result.stdout.decode("utf-8", errors="replace").strip() or "0"
                )
                if unique_count > 0:
                    # Regular merge: base has moved past branch and branch had
                    # unique work of its own.
                    return True
            # Fast-forward merge: branch and base at the same tip (count == 0).
            # Fall back to the audit trail when the slug is known.
            if slug is not None:
                return has_review_approve(project_root, slug, base_branch, branch)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return False


class StoryDAG:
    """Dependency-aware scheduler for concurrent story execution.

    Tracks two distinct states per story:
    - _completed: stories that satisfied their dependencies (merged or ALREADY_DONE).
      Used to unlock downstream stories in ready().
    - _finished: all stories that are done processing (any outcome).
      Used by is_done() to detect completion.

    mark_complete() adds to both sets (story ran and satisfied deps).
    mark_skipped() adds only to _finished (story is done but deps not satisfied).
    """

    def __init__(self, tasks: list[TaskStory], satisfied: set[str] | None = None) -> None:
        self._tasks: dict[str, TaskStory] = {t.slug: t for t in tasks}
        self._deps: dict[str, set[str]] = {t.slug: set(t.depends_on) for t in tasks}
        self._completed: set[str] = (
            set(satisfied) if satisfied else set()
        )  # satisfied: merged or ALREADY_DONE
        self._finished: set[str] = set()  # all done: any outcome

    def ready(self) -> list[TaskStory]:
        """Return tasks that are not finished and whose deps are all completed."""
        return [
            task
            for slug, task in self._tasks.items()
            if slug not in self._finished
            and all(dep in self._completed for dep in self._deps[slug])
        ]

    def mark_complete(self, slug: str) -> None:
        """Story satisfied deps (merged / ALREADY_DONE). Unlocks dependents."""
        self._completed.add(slug)
        self._finished.add(slug)

    def mark_skipped(self, slug: str) -> None:
        """Story finished without satisfying deps (failed / budget / blocked)."""
        self._finished.add(slug)

    def is_done(self) -> bool:
        """True when every task has been finished (any outcome)."""
        return len(self._finished) == len(self._tasks)

    def unmet_deps(self, slug: str) -> list[str]:
        """Dependency slugs that are not yet completed (for skip messages)."""
        return [dep for dep in self._deps[slug] if dep not in self._completed]

    def remaining(self) -> list[TaskStory]:
        """Tasks not yet finished (not in _finished)."""
        return [task for slug, task in self._tasks.items() if slug not in self._finished]


def build_dag(tasks: list[TaskStory], satisfied: set[str] | None = None) -> StoryDAG:
    """Build a StoryDAG from a list of TaskStory objects.

    Raises ValueError if:
    - any depends_on slug references a story not present in the manifest and not in satisfied
    - circular dependencies are detected

    Args:
        tasks: Stories in this sprint manifest.
        satisfied: Slugs already merged to main (from prior sprints). Dependencies
            on these slugs are treated as met without requiring them in the manifest.
    """
    known_slugs = {t.slug for t in tasks}
    _satisfied = satisfied or set()

    # Validate all depends_on slugs exist in the manifest or are pre-satisfied
    for task in tasks:
        missing = [
            dep for dep in task.depends_on if dep not in known_slugs and dep not in _satisfied
        ]
        if missing:
            raise ValueError(
                f"Story '{task.slug}' depends on unknown slug(s): {', '.join(missing)}. "
                f"All depends_on slugs must reference stories in the sprint manifest."
            )

    # Detect circular dependencies via DFS (gray/white/black coloring).
    # Exclude satisfied slugs — they are external and not in the DFS graph.
    deps = {t.slug: [d for d in t.depends_on if d not in _satisfied] for t in tasks}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _dfs(slug: str) -> None:
        visited.add(slug)
        in_stack.add(slug)
        for dep in deps[slug]:
            if dep in in_stack:
                raise ValueError(
                    f"Circular dependency detected: '{slug}' → '{dep}'. "
                    f"Sprint manifest contains a dependency cycle."
                )
            if dep not in visited:
                _dfs(dep)
        in_stack.discard(slug)

    for task in tasks:
        if task.slug not in visited:
            _dfs(task.slug)

    return StoryDAG(tasks, satisfied=_satisfied)


def resolve_satisfied_dependencies(
    tasks: list[TaskStory],
    *,
    project_root: Path,
    base_branch: str,
    branch_pattern: str,
    pre_satisfied: set[str] | None = None,
) -> set[str]:
    """Return external dependency slugs already satisfied outside this manifest.

    Only dependencies absent from ``tasks`` are candidates here. They count as
    satisfied when explicitly pre-marked (resume-mode skip state) or when their
    corresponding branch is already merged into the base branch.
    """
    manifest_slugs = {task.slug for task in tasks}
    dependent_slugs = {dep for task in tasks for dep in task.depends_on}
    satisfied_slugs = set(pre_satisfied or set())

    for dep_slug in dependent_slugs - manifest_slugs:
        if dep_slug in satisfied_slugs:
            continue
        branch = branch_pattern.format(slug=dep_slug)
        if _is_branch_merged(branch, base_branch, project_root, slug=dep_slug):
            satisfied_slugs.add(dep_slug)

    return satisfied_slugs


def _triage_spec(
    story_path: str,
    config: ForgeConfig,
    project_root: Path,
    *,
    task: TaskStory | None = None,
) -> StoryTriage:
    """Determine the optimal re-entry point for a story.

    Decision tree:
      merged to base?           → skip_merged
      no worktree?              → full
      0 commits ahead?          → full (stale; run_task will clean up)
      gate passes?              → review
      gate fails?               → dev

    When *task* is provided, skip building from disk (used for issue-backed stories).
    """
    if task is None:
        full_path = (project_root / story_path).resolve()
        task = _build_task_from_story(full_path)
    slug = task.slug

    branch = config.workspace.branch_pattern.format(slug=slug)
    base_branch = config.workspace.base_branch
    worktree_path = project_root / config.workspace.path_pattern.format(slug=slug)
    commits_ahead: list[str] | None = None
    commits_behind: int | None = None

    # 1. Probe branch divergence up front so same-tip branches can distinguish
    # fast-forward merges (audit-backed skip_merged) from stale/empty worktrees
    # that were merely created at the base tip.
    try:
        log_result = subprocess.run(
            ["git", "log", f"{base_branch}..{branch}", "--oneline"],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        commits_ahead = [
            ln
            for ln in log_result.stdout.decode("utf-8", errors="replace").strip().splitlines()
            if ln.strip()
        ]
        if not commits_ahead:
            behind_result = subprocess.run(
                ["git", "rev-list", f"{branch}..{base_branch}", "--count"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            commits_behind = int(
                behind_result.stdout.decode("utf-8", errors="replace").strip() or "0"
            )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        commits_ahead = None
        commits_behind = None

    # 2. Check if already merged to base branch.
    # Pass slug so _is_branch_merged can use the audit trail as a tiebreaker
    # for fast-forward merges where branch and base land on the same commit.
    if _is_branch_merged(branch, base_branch, project_root, slug=slug):
        return StoryTriage(
            story_path=story_path,
            action="skip_merged",
            reason=f"already merged to {base_branch}",
            worktree_path=None,
            slug=slug,
        )

    # 3. A branch at the base tip (0 ahead, 0 behind) is stale/empty, not merged.
    # This can happen when a prior run created the branch/worktree but never
    # produced story commits. Treat it as full so WORKSPACE recreates it, even
    # if the old worktree directory has already been removed.
    if commits_ahead == [] and commits_behind == 0:
        stale_reason = (
            f"branch is at {base_branch} HEAD with 0 commits ahead"
            if not worktree_path.exists()
            else f"worktree exists but branch is at {base_branch} HEAD (stale)"
        )
        return StoryTriage(
            story_path=story_path,
            action="full",
            reason=stale_reason,
            worktree_path=None,
            slug=slug,
        )

    # 4. Check if worktree exists
    if not worktree_path.exists():
        return StoryTriage(
            story_path=story_path,
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug=slug,
        )

    # 5. Check commits ahead of base branch
    if commits_ahead is None:
        try:
            log_result = subprocess.run(
                ["git", "log", f"{base_branch}..{branch}", "--oneline"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            commits_ahead = [
                ln
                for ln in log_result.stdout.decode("utf-8", errors="replace").strip().splitlines()
                if ln.strip()
            ]
        except (subprocess.TimeoutExpired, OSError):
            commits_ahead = []

    if not commits_ahead:
        # Stale worktree: 0 commits ahead of base. run_task WORKSPACE phase will
        # recreate it from scratch. Pass worktree_path=None so callers don't reuse it.
        return StoryTriage(
            story_path=story_path,
            action="full",
            reason=f"worktree exists but 0 commits ahead of {base_branch} (stale)",
            worktree_path=None,
            slug=slug,
        )

    # 5. Check audit trail for a prior review APPROVE
    if has_review_approve(project_root, slug, base_branch, branch):
        return StoryTriage(
            story_path=story_path,
            action="skip_merged",
            reason=(
                "prior APPROVE in audit trail; branch already satisfied "
                f"({len(commits_ahead)} commits ahead)"
            ),
            worktree_path=None,
            slug=slug,
        )

    # 6. Gate pre-check to decide REVIEW vs DEV entry
    gate_decision, gate_err, _gate_output = _run_gate(config, worktree_path, task=task)

    if gate_err is None and gate_decision == "PASS":
        return StoryTriage(
            story_path=story_path,
            action="review",
            reason=f"worktree exists, gate passes ({len(commits_ahead)} commits ahead)",
            worktree_path=worktree_path,
            slug=slug,
        )

    reason_detail = gate_err or f"gate returned {gate_decision}"
    return StoryTriage(
        story_path=story_path,
        action="dev",
        reason=f"worktree exists, gate fails ({reason_detail})",
        worktree_path=worktree_path,
        slug=slug,
    )
