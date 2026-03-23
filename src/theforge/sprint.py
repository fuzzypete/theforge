"""Sprint mode: run multiple stories sequentially through the full pipeline.

A sprint is defined by a YAML manifest listing story paths to run in order,
with an aggregate budget ceiling (Claude costs only).
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ForgeConfig
from .coord_audit import has_review_approve
from .coord_workspace import _merge_branch
from .coordinator import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    StructuredLogger,
    _fmt_duration,
    _generate_run_id,
    _is_remote_mode,
    _notify,
    _ntfy_publish,
    _run_gate,
    generate_audit_log,
    run_from_dev,
    run_from_review,
    run_task,
)
from .task import TaskStory as TaskSpec  # noqa: F401


@dataclass
class SprintManifest:
    """Parsed sprint.yaml manifest."""

    name: str
    budget_usd: float
    stories: list[str]  # relative paths to story files
    max_parallel: int = 1


@dataclass
class SprintResult:
    """Aggregate result from running a sprint."""

    name: str
    specs_total: int
    specs_succeeded: int
    specs_failed: int
    specs_skipped: int  # ALREADY_DONE or budget-stopped
    total_cost_usd: float
    budget_usd: float
    results: list[tuple[str, CoordinatorResult]] = field(default_factory=list)
    stopped_reason: str | None = None  # why sprint stopped early, if it did


def load_sprint_manifest(manifest_path: Path) -> SprintManifest:
    """Load and validate a sprint.yaml manifest.

    Raises ValueError if the manifest is invalid.
    """
    if not manifest_path.exists():
        raise ValueError(f"Sprint manifest not found: {manifest_path}")

    with open(manifest_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Sprint manifest must be a YAML mapping: {manifest_path}")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("Sprint manifest must have a non-empty 'name' field")

    budget_usd = raw.get("budget_usd")
    if budget_usd is None:
        raise ValueError("Sprint manifest must have a 'budget_usd' field")
    try:
        budget_usd = float(budget_usd)
    except (TypeError, ValueError):
        raise ValueError(f"Sprint 'budget_usd' must be a number, got {budget_usd!r}")
    if budget_usd <= 0:
        raise ValueError(f"Sprint 'budget_usd' must be > 0, got {budget_usd}")

    max_parallel_raw = raw.get("max_parallel", 1)
    if not isinstance(max_parallel_raw, int):
        raise ValueError(f"Sprint 'max_parallel' must be an integer, got {max_parallel_raw!r}")
    if max_parallel_raw < 1:
        raise ValueError(f"Sprint 'max_parallel' must be >= 1, got {max_parallel_raw}")
    max_parallel = max_parallel_raw

    # Accept both 'stories' (new) and 'specs' (deprecated) keys
    stories = raw.get("stories")
    if stories is None:
        specs_legacy = raw.get("specs")
        if specs_legacy is not None:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Sprint manifest uses deprecated 'specs:' key — rename to 'stories:'"
            )
            stories = specs_legacy
    if not stories or not isinstance(stories, list):
        raise ValueError("Sprint manifest must have a non-empty 'stories' list")
    if not all(isinstance(s, str) for s in stories):
        raise ValueError("All entries in 'stories' must be strings (file paths)")

    return SprintManifest(
        name=name, budget_usd=budget_usd, stories=stories, max_parallel=max_parallel
    )


def _validate_story_paths(manifest: SprintManifest, project_root: Path) -> list[Path]:
    """Resolve and validate all story paths. Raises ValueError if any are missing."""
    resolved: list[Path] = []
    missing: list[str] = []
    for spec_str in manifest.stories:
        path = (project_root / spec_str).resolve()
        if not path.exists():
            missing.append(spec_str)
        else:
            resolved.append(path)
    if missing:
        raise ValueError(
            f"Sprint manifest references {len(missing)} missing story/stories:\n"
            + "\n".join(f"  {s}" for s in missing)
        )
    return resolved


def _build_task_from_story(story_path: Path) -> TaskSpec:
    """Build a TaskStory from a story file using frontmatter if available."""
    # Import here to avoid circular imports; cli._build_task is essentially the same logic
    text = story_path.read_text(encoding="utf-8")
    fm: dict = {}
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            try:
                parsed = yaml.safe_load(text[3:end].strip()) or {}
                if isinstance(parsed, dict):
                    fm = parsed
            except yaml.YAMLError:
                pass

    slug = fm.get("slug") or story_path.stem
    name = fm.get("name", story_path.stem.replace("_", " ").replace("-", " ").title())
    raw_deps = fm.get("depends_on", [])
    if isinstance(raw_deps, str):
        depends_on = [raw_deps]
    elif isinstance(raw_deps, list):
        depends_on = [str(d) for d in raw_deps]
    else:
        depends_on = []
    return TaskSpec(
        name=name,
        story_path=story_path,
        slug=slug,
        pytest_target=fm.get("pytest_target"),
        gate_override=fm.get("gate"),
        depends_on=depends_on,
    )


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


def _story_header(idx: int, total: int, slug: str) -> str:
    """Format a story header line: [N/total] slug ─────... (fills to 60 chars)."""
    prefix = f"[{idx}/{total}] {slug} "
    dashes = "─" * max(0, 60 - len(prefix))
    return prefix + dashes


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


def _run_single_story(
    config: ForgeConfig,
    task: TaskSpec,
    triage: "StoryTriage | None",
    sprint_run_id: str,
    sprint_name: str,
    interactive: bool,
    notify: bool,
    resume: bool,
    effective_auto_merge: bool,
    state_update_fn: "Callable[[dict], None] | None",
) -> "tuple[TaskSpec, CoordinatorResult, float, datetime.datetime, datetime.datetime]":
    """Execute a single story and return (task, result, elapsed, started_at, finished_at).

    Designed to run in a worker thread. Dispatches to run_task / run_from_review /
    run_from_dev based on triage (resume mode) or always run_task (fresh mode).
    """
    started_at = datetime.datetime.now(datetime.timezone.utc)

    if state_update_fn is not None:
        state_update_fn({"spec": task.slug, "phase": "STARTING"})

    if resume and triage is not None:
        if triage.action == "review" and triage.worktree_path is not None:
            result = run_from_review(
                config,
                task,
                triage.worktree_path,
                interactive=interactive,
                auto_merge=effective_auto_merge,
                notify=notify,
                run_id=sprint_run_id,
                sprint_name=sprint_name,
                state_update_fn=state_update_fn,
            )
        elif triage.action == "dev" and triage.worktree_path is not None:
            result = run_from_dev(
                config,
                task,
                triage.worktree_path,
                interactive=interactive,
                auto_merge=effective_auto_merge,
                notify=notify,
                run_id=sprint_run_id,
                sprint_name=sprint_name,
                state_update_fn=state_update_fn,
            )
        else:
            result = run_task(
                config,
                task,
                interactive=interactive,
                auto_merge=effective_auto_merge,
                notify=notify,
                run_id=sprint_run_id,
                sprint_name=sprint_name,
                state_update_fn=state_update_fn,
            )
    else:
        result = run_task(
            config,
            task,
            interactive=interactive,
            auto_merge=effective_auto_merge,
            notify=notify,
            run_id=sprint_run_id,
            sprint_name=sprint_name,
            state_update_fn=state_update_fn,
        )

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    elapsed = (finished_at - started_at).total_seconds()
    return task, result, elapsed, started_at, finished_at


def _make_worker_phase_fn(
    slug: str,
    worker_phases: dict[str, str],
    phase_lock: threading.Lock,
    outer_fn: "Callable[[dict], None] | None",
) -> "Callable[[dict], None]":
    """Return a thread-safe state_update_fn wrapper that tracks per-worker phase.

    Updates worker_phases[slug] from updates["phase"] and (under lock) forwards
    updates to the outer daemon state_update_fn if provided.
    """

    def _update(updates: dict) -> None:
        phase = updates.get("phase", "")
        with phase_lock:
            if phase:
                worker_phases[slug] = phase
            if outer_fn is not None:
                outer_fn(updates)

    return _update


def _print_worker_status(
    active: "dict[str, Future[object]]",
    worker_phases: dict[str, str],
    dag: StoryDAG,
    total: int,
) -> None:
    """Print one status line per active worker, plus a summary of waiting stories."""
    if not active:
        return
    lines = []
    for slug in sorted(active):
        phase = worker_phases.get(slug, "RUNNING")
        lines.append(f"  [{slug}] {phase}")
    waiting = dag.remaining()
    waiting_active = [t for t in waiting if t.slug not in active]
    if waiting_active:
        names = ", ".join(t.slug for t in waiting_active[:5])
        suffix = f" (+{len(waiting_active) - 5} more)" if len(waiting_active) > 5 else ""
        lines.append(f"  waiting: {names}{suffix}")
    if lines:
        print("\n".join(lines), file=sys.stderr, flush=True)


def _classify_and_record(
    task: TaskSpec,
    result: CoordinatorResult,
    dag: StoryDAG,
    merged_slugs: set[str],
) -> tuple[int, int, int]:
    """Classify result and update DAG state. Returns (succeeded, failed, skipped) deltas."""
    preflight_verdict = result.state.preflight_verdict
    delta_succeeded = 0
    delta_failed = 0
    delta_skipped = 0

    if preflight_verdict == "ALREADY_DONE":
        delta_skipped = 1
    elif result.success:
        delta_succeeded = 1
    else:
        delta_failed = 1

    # DAG: mark_complete (satisfies deps) only when merged or ALREADY_DONE
    if preflight_verdict == "ALREADY_DONE" or (
        result.merge is not None and result.merge.get("merged", False)
    ):
        merged_slugs.add(task.slug)
        dag.mark_complete(task.slug)
    else:
        # Story finished but deps not satisfied for downstream scheduling
        dag.mark_skipped(task.slug)

    return delta_succeeded, delta_failed, delta_skipped


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


def _read_prior_sprint_cost(project_root: Path) -> float:
    """Read total_cost_usd from the prior sprint-audit.yaml, if it exists."""
    audit_path = project_root / ".forge" / "audits" / "sprint-audit.yaml"
    if not audit_path.exists():
        return 0.0
    try:
        with open(audit_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return float(data.get("sprint", {}).get("total_cost_usd", 0.0))
    except (OSError, ValueError, TypeError):
        return 0.0


def run_sprint(
    config: ForgeConfig,
    manifest_path: Path,
    *,
    auto_merge: bool = False,
    interactive: bool = False,
    notify: bool = False,
    resume: bool = False,
    state_update_fn: "Callable[[dict], None] | None" = None,
) -> SprintResult:
    """Run all stories in a sprint manifest with optional concurrency.

    When max_parallel > 1, stories with no unmet dependencies are launched
    concurrently up to max_parallel. Budget is pooled across all workers.
    Merge ordering respects dependency order when auto_merge is True.

    When max_parallel == 1 (default), behavior is identical to the original
    sequential runner.

    Args:
        config: Loaded ForgeConfig for the project.
        manifest_path: Path to the sprint.yaml manifest.
        auto_merge: If True, merge each story's branch after APPROVE.
        interactive: If True, pause for human review at each story.
        resume: If True, triage each story to find the optimal re-entry point
            (skip_merged / review / dev / full) and carry forward prior costs.

    Returns:
        SprintResult with per-story outcomes and aggregate stats.
    """
    manifest = load_sprint_manifest(manifest_path)
    story_paths = _validate_story_paths(manifest, config.project_root)

    total = len(story_paths)
    noun = "stories" if total != 1 else "story"
    print(
        f'[sprint] "{manifest.name}"  {total} {noun}  budget=${manifest.budget_usd:.2f}'
        f"  parallel={manifest.max_parallel}",
        file=sys.stderr,
        flush=True,
    )
    _log("⚠ Budget tracks Claude costs only (Codex/Gemini report $0.00)")

    # Sprint-level structured logger
    _sprint_run_id = _generate_run_id()
    _sprint_logger = StructuredLogger(
        run_id=_sprint_run_id,
        project=config.project,
        task=manifest.name,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    _sprint_logger.emit(
        "run_start",
        stories=manifest.stories,
        budget_usd=manifest.budget_usd,
        max_parallel=manifest.max_parallel,
        resume=resume,
    )

    # Create sprint-level log directory
    _sprint_log_dir = config.project_root / ".forge" / "logs" / manifest.name
    try:
        _sprint_log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        _sprint_log_dir = None  # type: ignore[assignment]

    started_at = datetime.datetime.now(datetime.timezone.utc)
    accumulated_cost = 0.0
    prior_cost = 0.0
    results: list[tuple[str, CoordinatorResult]] = []
    if notify and config.notifications.backend not in ("ntfy", "none"):
        from .notify_backends import send_notifications

        send_notifications(
            config,
            f'TheForge: sprint started \u2014 "{manifest.name}"',
            f"{total} stories \u00b7 budget ${manifest.budget_usd:.2f}",
        )
    specs_succeeded = 0
    specs_failed = 0
    specs_skipped = 0
    stopped_reason: str | None = None
    merged_slugs: set[str] = set()

    # Pre-scan: parse all TaskSpecs once and build maps.
    _parsed_tasks: dict[Path, TaskSpec] = {_sp: _build_task_from_story(_sp) for _sp in story_paths}
    slug_to_spec: dict[str, str] = {}
    for spec_str, story_path in zip(manifest.stories, story_paths):
        task = _parsed_tasks[story_path]
        slug_to_spec[task.slug] = spec_str

    dependent_slugs: set[str] = set()
    for _t in _parsed_tasks.values():
        dependent_slugs.update(_t.depends_on)  # type: ignore[union-attr]

    # Resume mode: triage all stories and carry forward prior costs
    triages: dict[str, StoryTriage] = {}
    if resume:
        prior_cost = _read_prior_sprint_cost(config.project_root)
        if prior_cost > 0.0:
            _log(f"Resuming with prior cost: ${prior_cost:.2f}")
        _log("Triaging specs...")
        for spec_str in manifest.stories:
            triage = _triage_spec(spec_str, config, config.project_root)
            triages[spec_str] = triage
            action_label = triage.action.upper().replace("_", " ")
            _log(f"  {triage.slug:<20} {action_label} ({triage.reason})")

    # Build DAG
    dag = build_dag(list(_parsed_tasks.values()))

    # Resume mode: pre-mark skip_merged / skip stories as complete in DAG
    if resume:
        for spec_str in manifest.stories:
            triage = triages.get(spec_str)
            if triage and triage.action in ("skip_merged", "skip"):
                # Look up task by spec_str
                story_path = (config.project_root / spec_str).resolve()
                task = _parsed_tasks.get(story_path)
                if task is None:
                    continue
                slug = task.slug
                action_label = triage.action.upper().replace("_", " ")
                _log(f"SKIP {slug} ({triage.reason})")
                specs_succeeded += 1
                merged_slugs.add(slug)
                dag.mark_complete(slug)

    # Parallel scheduling state
    active: dict[str, Future[object]] = {}
    cost_lock = threading.Lock()
    story_times: dict[str, tuple[datetime.datetime, datetime.datetime]] = {}
    batch_assignments: dict[str, int] = {}
    batch_number = 0
    worker_phases: dict[str, str] = {}
    phase_lock = threading.Lock()
    pending_merges: dict[str, tuple[TaskSpec, CoordinatorResult]] = {}
    _submission_counter = [0]  # mutable for closure capture; counts submitted stories

    with ThreadPoolExecutor(max_workers=manifest.max_parallel) as pool:
        while not dag.is_done():
            _log(f"[debug] loop: active={list(active.keys())} fin={dag._finished}")
            ready = [t for t in dag.ready() if t.slug not in active]

            for task in ready:
                # Cap concurrent submissions at max_parallel
                if len(active) >= manifest.max_parallel:
                    break

                with cost_lock:
                    cumulative = prior_cost + accumulated_cost
                if cumulative >= manifest.budget_usd:
                    dag.mark_skipped(task.slug)
                    specs_skipped += 1
                    if stopped_reason is None:
                        stopped_reason = (
                            f"Budget exhausted (${cumulative:.2f} >= ${manifest.budget_usd:.2f})"
                        )
                        if notify and config.notifications.backend not in ("ntfy", "none"):
                            from .notify_backends import send_notifications

                            send_notifications(
                                config,
                                f'TheForge: budget exceeded \u2014 "{manifest.name}"',
                                f"${cumulative:.2f} >= ${manifest.budget_usd:.2f}"
                                " \u2014 remaining stories skipped",
                            )
                    _log(f"SKIPPED {task.slug} (budget exhausted)")
                    continue

                # Eager merge for sequential mode; disabled in parallel mode
                effective_am = (
                    False
                    if manifest.max_parallel > 1
                    else (auto_merge or task.slug in dependent_slugs)
                )

                spec_str = slug_to_spec[task.slug]
                triage = triages.get(spec_str) if resume else None
                batch_assignments[task.slug] = batch_number
                _submission_counter[0] += 1
                print(
                    _story_header(_submission_counter[0], total, task.slug),
                    file=sys.stderr,
                    flush=True,
                )

                state_fn = _make_worker_phase_fn(
                    task.slug, worker_phases, phase_lock, state_update_fn
                )
                fut = pool.submit(
                    _run_single_story,
                    config,
                    task,
                    triage,
                    _sprint_run_id,
                    manifest.name,
                    interactive,
                    notify,
                    resume,
                    effective_am,
                    state_fn,
                )
                active[task.slug] = fut

            _log(f"[debug] post-submit: active={list(active.keys())}")
            if not active:
                # Deadlock: remaining tasks have unmet or budget-blocked deps
                for t in dag.remaining():
                    unmet = dag.unmet_deps(t.slug)
                    if unmet:
                        dep_list = ", ".join(unmet)
                        _log(f"SKIPPED {t.slug} (dependency failed: {dep_list})")
                    else:
                        _log(f"SKIPPED {t.slug} (blocked)")
                    dag.mark_skipped(t.slug)
                    specs_skipped += 1
                break

            _log(f"[debug] calling wait() with {len(active)} active futures")
            done_futs, _ = wait(list(active.values()), return_when=FIRST_COMPLETED, timeout=3600)
            _log(f"[debug] wait() returned: {len(done_futs)} done")
            batch_number += 1

            if not done_futs:
                # Timeout: all active workers hung for >3600s — cancel and fail them
                for slug, fut in list(active.items()):
                    fut.cancel()
                    _log(f"TIMEOUT {slug} (worker unresponsive after 3600s — marking as failed)")
                    spec_str = slug_to_spec[slug]
                    _timeout_state = CoordinatorState()
                    _timeout_state.error = "Worker timeout (>3600s)"
                    _timeout_result = CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=_timeout_state,
                        message="Worker thread timed out after 3600s",
                    )
                    results.append((spec_str, _timeout_result))
                    dag.mark_skipped(slug)
                    specs_failed += 1
                active.clear()
                stopped_reason = stopped_reason or "Worker timeout (>3600s)"
                continue

            for slug, fut in list(active.items()):
                if fut not in done_futs:
                    continue
                try:
                    task, result, elapsed, t0, t1 = fut.result()  # type: ignore[misc]
                except Exception as exc:
                    _log(f"ERROR {slug}: worker thread raised {type(exc).__name__}: {exc}")
                    del active[slug]
                    spec_str = slug_to_spec[slug]
                    _exc_state = CoordinatorState()
                    _exc_state.error = f"Worker exception: {exc}"
                    _exc_result = CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=_exc_state,
                        message=f"Worker thread raised {type(exc).__name__}: {exc}",
                    )
                    results.append((spec_str, _exc_result))
                    dag.mark_skipped(slug)
                    specs_failed += 1
                    continue
                del active[slug]
                story_times[slug] = (t0, t1)

                with cost_lock:
                    accumulated_cost += result.state.total_cost

                spec_str = slug_to_spec[slug]
                results.append((spec_str, result))

                # Write per-spec audit to worktree for diagnostics
                workspace_path = config.project_root / config.workspace.path_pattern.format(
                    slug=slug
                )
                if workspace_path.exists():
                    audit_data = generate_audit_log(config, task, result)
                    audit_path = workspace_path / "forge_audit.yaml"
                    with open(audit_path, "w", encoding="utf-8") as f:
                        yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
                    _log(f"Per-story audit written: {audit_path}")
                # Copy audit to durable per-story log dir
                if result.state.log_dir is not None:
                    try:
                        audit_data = generate_audit_log(config, task, result)
                        _story_audit_path = result.state.log_dir / "audit.yaml"
                        _story_audit_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(_story_audit_path, "w", encoding="utf-8") as f:
                            yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
                    except Exception:
                        pass  # best-effort

                spec_cost = result.state.total_cost
                icon = "✓" if result.success else "✗"
                dur = _fmt_duration(elapsed)
                _log(f"{icon} {slug}   ${spec_cost:.2f}  {dur}")

                ds, df, dsk = _classify_and_record(task, result, dag, merged_slugs)
                specs_succeeded += ds
                specs_failed += df
                specs_skipped += dsk

                # Parallel merge ordering: queue successful stories for dependency-ordered merge
                if manifest.max_parallel > 1 and auto_merge and result.success:
                    pending_merges[slug] = (task, result)

                _print_worker_status(active, worker_phases, dag, total)

            # Flush pending merges in dependency order (parallel mode only)
            if manifest.max_parallel > 1 and auto_merge and pending_merges:
                changed = True
                while changed:
                    changed = False
                    for slug, (task, result) in list(pending_merges.items()):
                        if result.success and all(d in merged_slugs for d in task.depends_on):
                            branch = config.workspace.branch_pattern.format(slug=slug)
                            wt = config.project_root / config.workspace.path_pattern.format(
                                slug=slug
                            )
                            merge_info = _merge_branch(
                                config.project_root,
                                config.workspace.base_branch,
                                branch,
                                slug,
                                wt,
                                config=config,
                                task_name=task.name,
                            )
                            if merge_info.get("merged"):
                                merged_slugs.add(slug)
                                # Re-classify in DAG since we now know it merged
                                dag.mark_complete(slug)
                            del pending_merges[slug]
                            changed = True

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    final_cost = accumulated_cost + prior_cost
    sprint_result = SprintResult(
        name=manifest.name,
        specs_total=total,
        specs_succeeded=specs_succeeded,
        specs_failed=specs_failed,
        specs_skipped=specs_skipped,
        total_cost_usd=final_cost,
        budget_usd=manifest.budget_usd,
        results=results,
        stopped_reason=stopped_reason,
    )

    _sprint_elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_at).total_seconds()
    _sprint_dur = _fmt_duration(_sprint_elapsed)
    _log(
        f"Sprint complete: {specs_succeeded} succeeded, {specs_failed} failed, "
        f"{specs_skipped} skipped. Total: ${final_cost:.2f}  {_sprint_dur}"
    )
    _sprint_outcome = "done" if specs_failed == 0 and stopped_reason is None else "partial"
    _sprint_logger.emit(
        "run_end",
        outcome=_sprint_outcome,
        total_cost_usd=round(final_cost, 6),
        total_duration_s=round(_sprint_elapsed, 2),
    )
    if notify:
        _notify(
            f"TheForge: {manifest.name}",
            f"✓ {specs_succeeded} passed, ✗ {specs_failed} failed"
            f" — ${final_cost:.2f}  {_fmt_duration(_sprint_elapsed)}",
        )
        # R10: ntfy summary notification when remote mode is active
        if _is_remote_mode(notify, config):
            assert config.notifications.ntfy is not None
            _ntfy_title = f'TheForge sprint complete \u2014 "{manifest.name}"'
            _ntfy_body_lines = [
                f"{total} specs: {specs_succeeded} succeeded \u00b7 {specs_failed} failed",
                f"Total cost: ${final_cost:.2f}   Duration: {_fmt_duration(_sprint_elapsed)}",
            ]
            if stopped_reason:
                _ntfy_body_lines.append(f"Stopped: {stopped_reason}")
            _ntfy_publish(
                config.notifications.ntfy.url,
                _ntfy_title,
                "\n".join(_ntfy_body_lines),
                priority=config.notifications.ntfy.priority,
            )
        if config.notifications.backend not in ("ntfy", "none"):
            from .notify_backends import send_notifications

            _sc_title = f'TheForge sprint complete \u2014 "{manifest.name}"'
            _sc_body_lines = [
                f"{total} specs: {specs_succeeded} succeeded \u00b7 {specs_failed} failed",
                f"Total cost: ${final_cost:.2f}   Duration: {_fmt_duration(_sprint_elapsed)}",
            ]
            if stopped_reason:
                _sc_body_lines.append(f"Stopped: {stopped_reason}")
            send_notifications(config, _sc_title, "\n".join(_sc_body_lines))

    # Build slug map for audit writers
    slug_map: dict[str, str] = {}
    for spec_str, sp in zip(manifest.stories, story_paths):
        t = _parsed_tasks.get(sp)
        if t is not None:
            slug_map[spec_str] = t.slug

    # Write sprint-audit.yaml (existing format; kept for backward compatibility)
    _write_sprint_audit(
        manifest=manifest,
        result=sprint_result,
        story_paths=story_paths,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        project_root=config.project_root,
        story_times=story_times,
        batch_assignments=batch_assignments,
        slug_map=slug_map,
    )

    # Write sprint-summary.yaml to .forge/logs/<sprint-name>/
    if _sprint_log_dir is not None:
        _write_sprint_summary(
            manifest=manifest,
            result=sprint_result,
            story_paths=story_paths,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
            sprint_log_dir=_sprint_log_dir,
            story_times=story_times,
            batch_assignments=batch_assignments,
            slug_map=slug_map,
        )

    # ── POST_SPRINT hook ──────────────────────────────────────────────
    if config.hooks and config.hooks.post_sprint:
        from .coord_hooks import build_post_sprint_payload
        from .coord_hooks import run_hook as _run_hook

        _stories = []
        for spec_str, res in results:
            # Derive slug: use workspace_path leaf (set during WORKSPACE phase) or spec stem
            _ws = res.state.workspace_path
            if _ws is not None:
                _slug = _ws.name
            else:
                _slug = Path(spec_str).stem
            _verdict = ""
            if res.state.review_results:
                _verdict = res.state.review_results[-1].verdict
            elif res.success:
                _verdict = "APPROVE"
            _stories.append(
                {
                    "slug": _slug,
                    "outcome": "done" if res.success else "escalate",
                    "verdict": _verdict,
                    "merged": res.merge is not None and res.merge.get("merged", False),
                }
            )
        _ps_payload = build_post_sprint_payload(
            sprint_name=manifest.name,
            stories=_stories,
            run_id=_sprint_run_id,
            config=config,
            total_cost_usd=final_cost,
            duration_seconds=_sprint_elapsed,
        )
        _run_hook(
            config.hooks.post_sprint,
            _ps_payload,
            config.hooks.timeout_seconds,
            "post_sprint",
            _sprint_logger,
            secrets=config.secrets,
        )

    return sprint_result


def _write_sprint_audit(
    manifest: SprintManifest,
    result: SprintResult,
    story_paths: list[Path],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    project_root: Path,
    story_times: "dict[str, tuple[datetime.datetime, datetime.datetime]] | None" = None,
    batch_assignments: "dict[str, int] | None" = None,
    slug_map: "dict[str, str] | None" = None,
) -> None:
    """Write sprint-audit.yaml to the project root."""
    story_times = story_times or {}
    batch_assignments = batch_assignments or {}
    slug_map = slug_map or {}

    # Build per-spec entries
    spec_entries = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    for spec_str, story_path in zip(manifest.stories, story_paths):
        if spec_str in results_by_spec:
            res = results_by_spec[spec_str]
            preflight = res.state.preflight_verdict or "PROCEED"
            outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else res.phase.name

            # Build reviews summary for this spec
            reviews_summary = []
            for i, meta in enumerate(res.state.review_cycle_metadata):
                cycle_entry: dict = {
                    "cycle": i + 1,
                    "pool": meta.pool_models,
                    "successful": meta.successful,
                    "failed": meta.failed,
                    "synthesized": meta.synthesized,
                    "parse_retries": meta.parse_retries,
                }
                if i < len(res.state.review_results):
                    r = res.state.review_results[i]
                    cycle_entry["verdict"] = r.verdict
                    cycle_entry["p1_count"] = sum(1 for f in r.findings if f.severity == "P1")
                    cycle_entry["p2_count"] = sum(1 for f in r.findings if f.severity == "P2")
                reviews_summary.append(cycle_entry)

            slug = slug_map.get(spec_str, story_path.stem)
            entry: dict = {
                "path": spec_str,
                "outcome": outcome,
                "cost_usd": round(res.state.total_cost, 4),
                "preflight": preflight,
                "merge": res.merge is not None and res.merge.get("merged", False),
                "reviews": reviews_summary,
            }
            if slug in story_times:
                entry["started_at"] = story_times[slug][0].strftime("%Y-%m-%dT%H:%M:%SZ")
                entry["finished_at"] = story_times[slug][1].strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                entry["started_at"] = None
                entry["finished_at"] = None
            entry["batch"] = batch_assignments.get(slug, 0)
        else:
            # Skipped due to budget or pre-skip (resume)
            entry = {
                "path": spec_str,
                "outcome": "SKIPPED",
                "cost_usd": 0.0,
                "preflight": None,
                "merge": False,
                "reviews": [],
                "started_at": None,
                "finished_at": None,
                "batch": batch_assignments.get(slug_map.get(spec_str, story_path.stem), 0),
            }
        spec_entries.append(entry)

    audit = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "max_parallel": manifest.max_parallel,
            "total_cost_usd": round(result.total_cost_usd, 4),
            "budget_note": "Costs reflect Claude invocations only; Codex/Gemini report $0.00",
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration, 1),
            "specs_total": result.specs_total,
            "specs_succeeded": result.specs_succeeded,
            "specs_failed": result.specs_failed,
            "specs_skipped": result.specs_skipped,
            "stopped_reason": result.stopped_reason,
        },
        "specs": spec_entries,
    }

    audits_dir = project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "sprint-audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Append to history log (JSONL, never overwritten).
    history_path = audits_dir / "history.jsonl"
    try:
        import json

        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit, default=str) + "\n")
    except OSError:
        pass
    _log(f"Audit written: {audit_path}")


def _write_sprint_summary(
    manifest: SprintManifest,
    result: SprintResult,
    story_paths: list[Path],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    sprint_log_dir: Path,
    story_times: "dict[str, tuple[datetime.datetime, datetime.datetime]] | None" = None,
    batch_assignments: "dict[str, int] | None" = None,
    slug_map: "dict[str, str] | None" = None,
) -> None:
    """Write sprint-summary.yaml to <project_root>/.forge/logs/<sprint-name>/."""
    story_times = story_times or {}
    batch_assignments = batch_assignments or {}
    slug_map = slug_map or {}

    spec_entries = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    for spec_str, story_path in zip(manifest.stories, story_paths):
        slug = slug_map.get(spec_str, story_path.stem)
        if spec_str in results_by_spec:
            res = results_by_spec[spec_str]
            preflight = res.state.preflight_verdict or "PROCEED"
            outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else res.phase.name
            last_verdict = ""
            if res.state.review_results:
                last_verdict = res.state.review_results[-1].verdict
            elif res.success:
                last_verdict = "APPROVE"
            entry: dict = {
                "path": spec_str,
                "slug": slug,
                "outcome": outcome,
                "verdict": last_verdict or None,
                "cost_usd": round(res.state.total_cost, 4),
                "preflight": preflight,
                "merge": res.merge is not None and res.merge.get("merged", False),
            }
            if slug in story_times:
                entry["started_at"] = story_times[slug][0].strftime("%Y-%m-%dT%H:%M:%SZ")
                entry["finished_at"] = story_times[slug][1].strftime("%Y-%m-%dT%H:%M:%SZ")
            entry["batch"] = batch_assignments.get(slug, 0)
        else:
            entry = {
                "path": spec_str,
                "slug": slug,
                "outcome": "SKIPPED",
                "verdict": None,
                "cost_usd": 0.0,
                "preflight": None,
                "merge": False,
                "batch": batch_assignments.get(slug, 0),
            }
        spec_entries.append(entry)

    summary = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "max_parallel": manifest.max_parallel,
            "total_cost_usd": round(result.total_cost_usd, 4),
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration, 1),
            "specs_total": result.specs_total,
            "specs_succeeded": result.specs_succeeded,
            "specs_failed": result.specs_failed,
            "specs_skipped": result.specs_skipped,
            "stopped_reason": result.stopped_reason,
        },
        "stories": spec_entries,
    }

    try:
        sprint_log_dir.mkdir(parents=True, exist_ok=True)
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        with open(summary_path, "w", encoding="utf-8") as f:
            yaml.dump(summary, f, default_flow_style=False, sort_keys=False)
        _log(f"Sprint summary written: {summary_path}")
    except Exception as exc:
        _log(f"Warning: sprint summary write failed: {exc}")
