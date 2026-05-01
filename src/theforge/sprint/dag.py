"""Sprint DAG scheduler and story triage logic."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import ForgeConfig
from ..coordinator.audit import has_review_approve
from ..coordinator.gate import _run_gate
from ..log_util import _log_line
from ..task import TaskStory
from .manifest import _build_task_from_story


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def _issue_number_from_slug(slug: str) -> int | None:
    match = re.fullmatch(r"issue-(\d+)", slug)
    if match is None:
        return None
    return int(match.group(1))


def _issue_number_from_ref(ref: str) -> int | None:
    match = re.search(r"(?:^|[/-])issue-(\d+)(?:$|[^0-9])", ref)
    if match is None:
        return None
    return int(match.group(1))


@dataclass
class StoryTriage:
    """Result of triaging a story for sprint resume."""

    story_path: str
    action: str  # "skip_merged", "skip", "review", "dev", "full"
    reason: str
    worktree_path: Path | None = None
    slug: str = ""


@dataclass(frozen=True)
class MergeEvidence:
    """Structured merge detection result for resume triage."""

    merged: bool
    source: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None


def _has_prior_review_approve(
    project_root: Path,
    slug: str,
    base_branch: str,
    branch: str,
) -> bool:
    """Return True when audit history shows a landed APPROVE for this story.

    Resume merged detection needs the persisted review outcome even when the
    feature branch still appears ahead of base (squash merges rewrite commits,
    so git topology alone cannot prove the merge). This helper intentionally
    bypasses the stale-branch guard inside has_review_approve, but it still
    requires landing evidence so a zero-delta APPROVE or failed merge attempt
    does not satisfy the story during resume.
    """
    return has_review_approve(
        project_root,
        slug,
        base_branch,
        branch,
        allow_unmerged_commits=True,
        require_landed=True,
    )


def _has_base_commit_referencing_issue(
    project_root: Path,
    base_branch: str,
    issue_number: int,
) -> bool:
    """Return True when base has a commit message that references the issue.

    GitHub squash commits commonly preserve PR or issue references in the final
    base-branch commit even though the branch tip is not topologically merged.
    This git-level check catches externally merged branches that never produced
    a forge APPROVE audit record.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--format=%H",
                "--max-count=1",
                "--fixed-strings",
                f"--grep=(#{issue_number})",
                base_branch,
            ],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0 and bool(
            result.stdout.decode("utf-8", errors="replace").strip()
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


def _lookup_merged_pr_for_branch(
    branch: str,
    project_root: Path,
) -> MergeEvidence | None:
    """Return merged PR metadata for ``branch`` when GitHub reports one."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "closed",
                "--json",
                "number,url,mergedAt",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "[]")
        if not isinstance(data, list):
            return None
        merged_prs: list[tuple[str, int, str | None]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            merged_at = item.get("mergedAt")
            number = item.get("number")
            url = item.get("url")
            if not isinstance(merged_at, str) or not merged_at:
                continue
            if not isinstance(number, int):
                continue
            merged_prs.append((merged_at, number, url if isinstance(url, str) else None))
        if not merged_prs:
            return None
        _merged_at, pr_number, pr_url = max(merged_prs, key=lambda item: item[0])
        return MergeEvidence(
            merged=True,
            source="github_pr",
            pr_number=pr_number,
            pr_url=pr_url,
        )
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None


def _with_pr_metadata(
    evidence: MergeEvidence,
    branch: str,
    project_root: Path,
    issue_number: int | None,
) -> MergeEvidence:
    """Attach merged-PR metadata when GitHub can identify the landing PR."""
    if not evidence.merged or issue_number is None or evidence.pr_number is not None:
        return evidence
    merged_pr = _lookup_merged_pr_for_branch(branch, project_root)
    if merged_pr is None:
        return evidence
    return MergeEvidence(
        merged=True,
        source=evidence.source,
        pr_number=merged_pr.pr_number,
        pr_url=merged_pr.pr_url,
    )


def _branch_merge_evidence(
    branch: str,
    base_branch: str,
    project_root: Path,
    slug: str | None = None,
) -> MergeEvidence:
    """Return structured merge evidence for ``branch`` against ``base_branch``."""
    no_merge = MergeEvidence(merged=False)
    issue_number = _issue_number_from_slug(slug) if slug is not None else None
    if issue_number is None:
        issue_number = _issue_number_from_ref(branch)
    issue_is_closed = issue_number is None or _is_issue_closed(issue_number, project_root)

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
                    if issue_is_closed:
                        return _with_pr_metadata(
                            MergeEvidence(merged=True, source="topology"),
                            branch,
                            project_root,
                            issue_number,
                        )
                    return no_merge
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass

    if not issue_is_closed:
        return no_merge

    if issue_number is not None and _has_base_commit_referencing_issue(
        project_root,
        base_branch,
        issue_number,
    ):
        return _with_pr_metadata(
            MergeEvidence(merged=True, source="issue_commit"),
            branch,
            project_root,
            issue_number,
        )

    # Fast-forward merges at the same tip and squash merges both need the audit
    # trail fallback. In real squash merges, --is-ancestor returns non-zero, so
    # this check must live outside the topology-success branch above.
    if slug is not None:
        try:
            if _has_prior_review_approve(project_root, slug, base_branch, branch):
                return _with_pr_metadata(
                    MergeEvidence(merged=True, source="audit"),
                    branch,
                    project_root,
                    issue_number,
                )
        except Exception:
            return no_merge

    merged_pr = (
        _lookup_merged_pr_for_branch(branch, project_root) if issue_number is not None else None
    )
    if merged_pr is not None:
        return merged_pr
    return no_merge


def _is_branch_merged(
    branch: str,
    base_branch: str,
    project_root: Path,
    slug: str | None = None,
) -> bool:
    """Return True if branch has been merged into base_branch.

    Three detection paths handle the merge strategies theforge uses:

    1. Regular merge commit (git merge --no-edit fallback):
       --is-ancestor passes AND branch..base_branch count > 0 (base advanced
       past the branch tip via a merge commit) AND base_branch..branch count > 0
       (the branch had unique commits before merge).

    2. Fast-forward merge (git merge --ff-only, preferred):
       After an FF merge, branch and base point at the same commit, so
       branch..base_branch count == 0.

    3. Squash merge (configured default):
       The feature branch tip remains an ancestor of base because it was based
       on base, but the squash commit on base is a new commit with no parent
       relationship to the branch. Git topology alone therefore cannot prove
       the merge. Issue-backed branches first look for a base commit that
       references the issue, then fall back to the forge APPROVE audit trail
       when slug is provided.

    A branch that was merely created at base HEAD (count == 0, no audit entry)
    correctly returns False.
    """
    return _branch_merge_evidence(branch, base_branch, project_root, slug=slug).merged


def _is_issue_closed(issue_number: int, project_root: Path) -> bool:
    """Return True when GitHub issue ``issue_number`` is not OPEN.

    GitHub is the source of truth for issue-backed external dependencies. Any
    CLI, parsing, or schema failure returns False so missing state does not
    spuriously unblock dependent sprint work.
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "state"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        state = data.get("state")
        if not isinstance(state, str):
            return False
        return state.upper() != "OPEN"
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
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
        self._hard_deps: dict[str, set[str]] = {t.slug: set(t.depends_on) for t in tasks}
        self._soft_deps: dict[str, set[str]] = {t.slug: set(t.collision_deps) for t in tasks}
        self._completed: set[str] = (
            set(satisfied) if satisfied else set()
        )  # satisfied: merged or ALREADY_DONE
        self._finished: set[str] = set()  # all done: any outcome

    def ready(self) -> list[TaskStory]:
        """Return tasks that are not finished and whose deps are all released.

        Hard deps (``depends_on``) must be in ``_completed``. Soft deps
        (``collision_deps``) release on any terminal state — completed OR
        finished — because the upstream reaching a terminal-but-not-merged
        state means the collision the synthetic edge guarded against does
        not exist on disk, so the downstream can proceed.
        """
        return [
            task
            for slug, task in self._tasks.items()
            if slug not in self._finished
            and all(dep in self._completed for dep in self._hard_deps[slug])
            and all(
                dep in self._completed or dep in self._finished for dep in self._soft_deps[slug]
            )
        ]

    def mark_complete(self, slug: str) -> None:
        """Story satisfied deps (merged / ALREADY_DONE). Unlocks dependents."""
        self._completed.add(slug)
        if slug in self._tasks:
            self._finished.add(slug)

    def mark_skipped(self, slug: str) -> None:
        """Story finished without satisfying deps (failed / budget / blocked)."""
        self._finished.add(slug)

    def is_done(self) -> bool:
        """True when every task has been finished (any outcome)."""
        return len(self._finished) == len(self._tasks)

    def unmet_deps(self, slug: str) -> list[str]:
        """Dependency slugs that genuinely block this story (for skip messages).

        Hard deps are unmet when not yet completed. Soft (collision) deps are
        unmet only when their upstream has not reached any terminal state —
        a terminal-but-not-merged upstream releases the soft edge.
        """
        unmet = [dep for dep in self._hard_deps[slug] if dep not in self._completed]
        unmet.extend(
            dep
            for dep in self._soft_deps[slug]
            if dep not in self._completed and dep not in self._finished
        )
        return unmet

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
            dep
            for dep in task.depends_on
            if dep not in known_slugs
            and dep not in _satisfied
            and _issue_number_from_slug(dep) is None
        ]
        if missing:
            raise ValueError(
                f"Story '{task.slug}' depends on unknown slug(s): {', '.join(missing)}. "
                f"All depends_on slugs must reference stories in the sprint manifest."
            )

    # Detect circular dependencies via DFS (gray/white/black coloring).
    # Exclude satisfied slugs — they are external and not in the DFS graph.
    deps = {
        t.slug: [d for d in t.depends_on if d not in _satisfied and d in known_slugs]
        for t in tasks
    }
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
            continue
        issue_number = _issue_number_from_slug(dep_slug)
        if issue_number is not None and _is_issue_closed(issue_number, project_root):
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
    merge_evidence = _branch_merge_evidence(branch, base_branch, project_root, slug=slug)
    if merge_evidence.merged:
        merged_reason = f"already merged to {base_branch}"
        if merge_evidence.pr_number is not None:
            merged_reason = f"already merged in PR #{merge_evidence.pr_number}, skipping"
        elif merge_evidence.source == "audit":
            merged_reason = f"prior APPROVE in audit trail; already merged to {base_branch}"
        return StoryTriage(
            story_path=story_path,
            action="skip_merged",
            reason=merged_reason,
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
