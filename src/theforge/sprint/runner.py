"""Sprint runner: parallel story scheduling and the run_sprint entry point."""

from __future__ import annotations

import datetime
import sys
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

import yaml

from ..config import ForgeConfig
from ..coordinator.engine import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    StructuredLogger,
    _fmt_duration,
    _generate_run_id,
    _notify,
    _ntfy_publish,
    run_from_dev,
    run_from_review,
    run_task,
)
from ..coordinator.workspace import _merge_branch
from ..task import TaskStory as TaskSpec  # noqa: F401
from .audit import _write_sprint_audit, _write_sprint_summary, _write_story_audit
from .dag import StoryDAG, StoryTriage, _triage_spec, build_dag
from .display import _print_worker_status, _story_header
from .manifest import (
    SprintResult,
    _build_task_from_story,
    _validate_story_paths,
    load_sprint_manifest,
)


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


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
    if manifest.max_parallel is None:
        manifest.max_parallel = config.sprint.max_parallel
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
        from ..notify_backends import send_notifications

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
                            from ..notify_backends import send_notifications

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

                _write_story_audit(config, task, result)

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
        if config.notifications.backend != "none":
            _notify(
                f"TheForge: {manifest.name}",
                f"✓ {specs_succeeded} passed, ✗ {specs_failed} failed",
            )
        if config.notifications.ntfy is not None:
            _ntfy_title = f'TheForge: sprint done \u2014 "{manifest.name}"'
            _ntfy_body_lines = [
                f"{total} specs: {specs_succeeded} succeeded \u00b7 {specs_failed} failed",
                f"Total cost: ${final_cost:.2f}   Duration: {_sprint_dur}",
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
            from ..notify_backends import send_notifications

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
        from ..coordinator.hooks import build_post_sprint_payload
        from ..coordinator.hooks import run_hook as _run_hook

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
