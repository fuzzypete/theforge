"""Sprint DAG scheduler and story triage logic."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import ForgeConfig
from ..coordinator.audit import has_review_approve
from ..coordinator.gate import _run_gate
from ..task import TaskStory as TaskSpec  # noqa: F401
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

    def __init__(self, tasks: list[TaskSpec]) -> None:
        self._tasks: dict[str, TaskSpec] = {t.slug: t for t in tasks}
        self._deps: dict[str, set[str]] = {t.slug: set(t.depends_on) for t in tasks}
        self._completed: set[str] = set()  # satisfied: merged or ALREADY_DONE
        self._finished: set[str] = set()  # all done: any outcome

    def ready(self) -> list[TaskSpec]:
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

    def remaining(self) -> list[TaskSpec]:
        """Tasks not yet finished (not in _finished)."""
        return [task for slug, task in self._tasks.items() if slug not in self._finished]


def build_dag(tasks: list[TaskSpec]) -> StoryDAG:
    """Build a StoryDAG from a list of TaskSpec objects.

    Raises ValueError if:
    - any depends_on slug references a story not present in the manifest
    - circular dependencies are detected
    """
    known_slugs = {t.slug for t in tasks}

    # Validate all depends_on slugs exist in the manifest
    for task in tasks:
        missing = [dep for dep in task.depends_on if dep not in known_slugs]
        if missing:
            raise ValueError(
                f"Story '{task.slug}' depends on unknown slug(s): {', '.join(missing)}. "
                f"All depends_on slugs must reference stories in the sprint manifest."
            )

    # Detect circular dependencies via DFS (gray/white/black coloring)
    deps = {t.slug: list(t.depends_on) for t in tasks}
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

    return StoryDAG(tasks)


def _triage_spec(
    story_path: str,
    config: ForgeConfig,
    project_root: Path,
) -> StoryTriage:
    """Determine the optimal re-entry point for a story.

    Decision tree:
      merged to base?           → skip_merged
      no worktree?              → full
      0 commits ahead?          → full (stale; run_task will clean up)
      gate passes?              → review
      gate fails?               → dev
    """
    full_path = (project_root / story_path).resolve()
    task = _build_task_from_story(full_path)
    slug = task.slug

    branch = config.workspace.branch_pattern.format(slug=slug)
    base_branch = config.workspace.base_branch
    worktree_path = project_root / config.workspace.path_pattern.format(slug=slug)

    # 1. Check if already merged to base branch
    # --is-ancestor alone is not enough: a branch created at main HEAD with
    # zero new commits also passes --is-ancestor.  We must also verify the
    # branch has commits that diverge from main (i.e. it's not just pointing
    # at the same commit or an older one).
    try:
        merge_result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_branch],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if merge_result.returncode == 0:
            # Verify branch actually has unique commits (not just at base HEAD)
            ahead_result = subprocess.run(
                ["git", "rev-list", f"{base_branch}..{branch}", "--count"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            ahead_count = int(ahead_result.stdout.decode("utf-8", errors="replace").strip() or "0")
            if ahead_count > 0:
                return StoryTriage(
                    story_path=story_path,
                    action="skip_merged",
                    reason=f"already merged to {base_branch}",
                    worktree_path=None,
                    slug=slug,
                )
            # ahead_count == 0: branch exists at base HEAD, not truly merged —
            # fall through to worktree checks below
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass  # Branch may not exist yet — treat as not merged

    # 2. Check if worktree exists
    if not worktree_path.exists():
        return StoryTriage(
            story_path=story_path,
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug=slug,
        )

    # 3. Check commits ahead of base branch
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

    # 4. Check audit trail for a prior review APPROVE
    if has_review_approve(project_root, slug, base_branch, branch):
        return StoryTriage(
            story_path=story_path,
            action="skip",
            reason=f"prior APPROVE in audit trail ({len(commits_ahead)} commits ahead)",
            worktree_path=worktree_path,
            slug=slug,
        )

    # 5. Gate pre-check to decide REVIEW vs DEV entry
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
