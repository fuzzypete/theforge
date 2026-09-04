"""Sprint DAG scheduler and story triage logic."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import ForgeConfig
from ..coordinator.branch_landing import (
    BranchLanding,
    _issue_number_from_slug,
    resolve_branch_landing,
)
from ..coordinator.gate import _run_gate
from ..coordinator.state import EntryGateOutcome, GateLabel, GateRunFacts
from ..log_util import _log_line
from ..task import TaskStory
from .manifest import _build_task_from_story

#: Story phase written to live sprint state while a story's reuse gate runs.
#: Resume triage runs one gate per story before any story is dispatched; without
#: a phase of its own that window reads as ``waiting`` for its whole (possibly
#: many-minute) duration (#2014).
REUSE_GATE_PHASE = "REUSE_GATE"

#: ``GateLabel.purpose`` for the per-story resume-triage gate.
REUSE_GATE_PURPOSE = "reuse gate"


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


@dataclass
class StoryTriage:
    """Result of triaging a story for sprint resume."""

    story_path: str
    action: str  # "skip_merged", "skip", "review", "dev", "full"
    reason: str
    worktree_path: Path | None = None
    slug: str = ""
    # Structured outcome of the reuse gate when it is what routed this story to
    # DEV, for the phase that has to act on it. ``reason`` above stays the
    # operator-facing record for the sprint log; nothing downstream parses it
    # (#2796). None for every action the reuse gate did not decide, and for a
    # gate that failed outright rather than running out of time.
    gate_outcome: EntryGateOutcome | None = None


def _is_branch_merged(
    branch: str,
    base_branch: str,
    project_root: Path,
    slug: str | None = None,
) -> bool:
    """Return True if branch has been merged into base_branch.

    The boolean face of :func:`resolve_branch_landing`, kept for the callers
    whose question genuinely is yes/no — external dependency satisfaction here,
    and cached-preflight reuse in ``coordinator.preflight_flow``. Everything
    that has to *report* the answer consumes the tri-state directly, because a
    branch nothing could speak for is not the same as one that did not land.
    """
    return resolve_branch_landing(branch, base_branch, project_root, slug=slug).landed


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


def _branch_tip_sha(commits_ahead: list[str] | None) -> str | None:
    """Return the branch tip's abbreviated SHA from ``git log --oneline`` lines.

    ``commits_ahead`` is newest-first, so its first entry starts with the tip's
    abbreviated SHA. Reading it here costs nothing; asking git again for a value
    already in hand would add a subprocess per story to the triage loop.
    """
    if not commits_ahead:
        return None
    head = commits_ahead[0].split(maxsplit=1)
    return head[0] if head else None


def _merged_skip_reason(landing: BranchLanding, base_branch: str) -> str:
    """Render a skip reason that names the evidence the skip rests on.

    A skip discards preserved work, so it is reported as a skip and states what
    produced it — an operator reading ``SKIP issue-N (...)`` can then contest
    the specific claim instead of reading a bare "already merged" as completion
    (#2374). Every source is named, including the weakest one.
    """
    return f"already merged to {base_branch} (evidence: {landing.describe_source(base_branch)})"


def _triage_spec(
    story_path: str,
    config: ForgeConfig,
    project_root: Path,
    *,
    task: TaskStory | None = None,
    on_gate_start: Callable[[GateLabel], None] | None = None,
    on_gate_end: Callable[[GateLabel], None] | None = None,
) -> StoryTriage:
    """Determine the optimal re-entry point for a story.

    Decision tree:
      merged to base?           → skip_merged
      no worktree?              → full
      0 commits ahead?          → full (stale; run_task will clean up)
      gate passes?              → review
      gate fails?               → dev

    When *task* is provided, skip building from disk (used for issue-backed stories).

    ``on_gate_start`` / ``on_gate_end`` bracket the reuse gate — the only step
    here that can run for minutes. They fire with the gate's ``GateLabel`` so the
    caller can publish live progress for exactly that window, rather than for
    the cheap git probes that surround it (#2014). Every other path returns
    without calling either.
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

    # 2. Check if already merged to base branch. This must stay ahead of the
    # stale/empty classification below: a same-tip branch is ambiguous, and
    # resolving it toward "no work was done" discards a landed story, while
    # resolving it toward "already merged" at worst repeats a skip (#2111).
    # Pass slug so the resolver can use the audit trail as a tiebreaker
    # for fast-forward merges where branch and base land on the same commit.
    landing = resolve_branch_landing(branch, base_branch, project_root, slug=slug)
    if landing.landed:
        return StoryTriage(
            story_path=story_path,
            action="skip_merged",
            reason=_merged_skip_reason(landing, base_branch),
            worktree_path=None,
            slug=slug,
        )

    # 3. A branch at the base tip (0 ahead, 0 behind) with no merge evidence of
    # any kind is stale/empty, not merged. This can happen when a prior run
    # created the branch/worktree but never produced story commits. Treat it as
    # full so WORKSPACE recreates it, even if the old worktree directory has
    # already been removed. Step 2 above has already claimed every same-tip
    # branch backed by audit, PR, or issue-commit evidence.
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

    # No second landing decision lives here. Step 2 above asked
    # resolve_branch_landing, which consults the audit trail — including a prior
    # APPROVE — as its first and strongest source. A separate APPROVE check at
    # this point answered the same question from weaker evidence: it accepted an
    # APPROVE with no record of the work reaching base, and it ran *after* the
    # resolver had already declined (#2795, cycle 2).

    # 6. Gate pre-check to decide REVIEW vs DEV entry
    gate_label = GateLabel(
        purpose=REUSE_GATE_PURPOSE,
        slug=slug,
        target=branch,
        commit=_branch_tip_sha(commits_ahead),
        worktree_path=str(worktree_path),
    )
    if on_gate_start is not None:
        on_gate_start(gate_label)
    # Which profile this pre-check ran, and what its result is worth. The triage
    # reason is this decision's durable record — it is what the sprint log and
    # state report for "why did this story enter at REVIEW" — so the profile
    # travels in it rather than being reconstructible only from config (#2358).
    gate_selection: list = []
    gate_facts: list[GateRunFacts] = []
    _gate_started = time.monotonic()
    try:
        gate_decision, gate_err, gate_output_tail = _run_gate(
            config,
            worktree_path,
            task=task,
            label=gate_label,
            selection_out=gate_selection,
            facts_out=gate_facts,
        )
    finally:
        gate_elapsed_s = time.monotonic() - _gate_started
        if on_gate_end is not None:
            on_gate_end(gate_label)

    profile_note = f", {gate_selection[0].describe()}" if gate_selection else ""

    if gate_err is None and gate_decision == "PASS":
        return StoryTriage(
            story_path=story_path,
            action="review",
            reason=(
                f"worktree exists, gate passes ({len(commits_ahead)} commits ahead{profile_note})"
            ),
            worktree_path=worktree_path,
            slug=slug,
        )

    reason_detail = gate_err or f"gate returned {gate_decision}"
    # A gate that ran out of time is a different condition from a gate that
    # failed, and the story it routes to DEV has to be told which one happened.
    # The flag comes from the gate's own run facts rather than from matching its
    # error text, so nothing downstream re-derives it (#2796).
    facts = gate_facts[0] if gate_facts else None
    gate_outcome = None
    if facts is not None and facts.timed_out:
        gate_outcome = EntryGateOutcome(
            outcome="timeout",
            command=facts.command,
            timeout_s=facts.timeout_s,
            elapsed_s=gate_elapsed_s,
            output_tail=gate_output_tail or "",
            profile=gate_selection[0].describe() if gate_selection else None,
        )
        _log(
            f"  reuse gate did not finish for {slug}:"
            f" killed at its {facts.timeout_s}s budget after {gate_elapsed_s:.1f}s"
        )
    return StoryTriage(
        story_path=story_path,
        action="dev",
        reason=f"worktree exists, gate fails ({reason_detail}{profile_note})",
        worktree_path=worktree_path,
        slug=slug,
        gate_outcome=gate_outcome,
    )
