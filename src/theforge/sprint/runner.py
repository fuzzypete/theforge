"""Sprint runner: parallel story scheduling and the run_sprint entry point."""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

import yaml

from ..config import ForgeConfig
from ..coordinator.engine import run_from_dev, run_from_review, run_task
from ..coordinator.gate import run_gate_full
from ..coordinator.log_tee import _make_story_log_dir, get_worker_slug, set_worker_slug
from ..coordinator.logging import StructuredLogger
from ..coordinator.notify import _notify
from ..coordinator.ntfy_client import _ntfy_publish
from ..coordinator.state import CoordinatorResult, CoordinatorState, Phase
from ..coordinator.util import _fmt_duration, _generate_run_id
from ..coordinator.workspace import sweep_orphan_worktrees
from ..log_util import _log_line
from ..task import TaskStory
from .audit import (
    _get_or_create_sprint_id,
    _write_sprint_audit,
    _write_sprint_summary,
    _write_story_audit,
    persist_accumulated_story_state,
)
from .ci_checks import poll_required_checks
from .collision import (
    compute_bundle_assignments,
    compute_synthetic_edges,
    inject_synthetic_deps,
    run_batch_preflight,
)
from .dag import (
    StoryDAG,
    StoryTriage,
    _triage_spec,
    build_dag,
    resolve_satisfied_dependencies,
)
from .display import _print_worker_status, _story_header
from .lock import integration_lock
from .manifest import (
    ResolvedSprint,
    SprintResult,
    _build_task_from_story,
    resolve_from_manifest,
)
from .query import normalize_dependency_plan
from .sources import StorySource
from .state_writer import SprintStateWriter

_UNTRACKED_COST_CLIS: frozenset[str] = frozenset({"codex", "gemini"})


def _log(msg: str) -> None:
    slug = get_worker_slug()
    prefix = f"[{slug}] " if slug else ""
    _log_line("[sprint]", f"{prefix}{msg}")


def _scrub_root_forge_artifacts(config: ForgeConfig) -> None:
    """Best-effort: remove tracked .forge artifacts from the project-root index."""
    from ..coordinator.workspace import _deindex_forge_artifacts  # noqa: PLC0415

    _deindex_forge_artifacts(config.project_root)


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


def _run_baseline_gate(config: ForgeConfig, resolved: ResolvedSprint) -> dict[str, object]:
    """Run the configured gate on the sprint merge base before any agent work starts."""

    git_dir_check = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if git_dir_check.returncode != 0:
        now = datetime.datetime.now(datetime.timezone.utc)
        return {
            "status": "skipped",
            "passed": True,
            "exit_code": 0,
            "duration_seconds": 0.0,
            "started_at": now,
            "finished_at": now,
            "merge_base": None,
            "command": config.validation.gate_command,
            "message": "Baseline gate skipped: project root is not a git checkout",
        }

    baseline_started_at = datetime.datetime.now(datetime.timezone.utc)
    started_monotonic = time.monotonic()
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", config.workspace.base_branch],
        cwd=config.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_base.returncode != 0:
        duration = time.monotonic() - started_monotonic
        stderr = (merge_base.stderr or "").strip()
        return {
            "status": "error",
            "passed": False,
            "exit_code": merge_base.returncode,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": datetime.datetime.now(datetime.timezone.utc),
            "merge_base": None,
            "command": config.validation.gate_command,
            "message": (
                "Broken baseline: unable to determine merge base against "
                f"{config.workspace.base_branch}: {stderr or 'git merge-base failed'}"
            ),
        }

    merge_base_ref = (merge_base.stdout or "").strip()
    if not merge_base_ref:
        duration = time.monotonic() - started_monotonic
        return {
            "status": "error",
            "passed": False,
            "exit_code": merge_base.returncode,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": datetime.datetime.now(datetime.timezone.utc),
            "merge_base": None,
            "command": config.validation.gate_command,
            "message": (
                "Broken baseline: unable to determine merge base against "
                f"{config.workspace.base_branch}: empty merge-base result"
            ),
        }

    show_top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    show_top_path = (show_top.stdout or "").strip()
    same_toplevel = False
    if show_top.returncode == 0 and show_top_path:
        try:
            same_toplevel = os.path.samefile(show_top_path, config.project_root)
        except OSError:
            same_toplevel = Path(show_top_path).resolve() == config.project_root.resolve()
    if not same_toplevel:
        duration = time.monotonic() - started_monotonic
        return {
            "status": "error",
            "passed": False,
            "exit_code": show_top.returncode,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": datetime.datetime.now(datetime.timezone.utc),
            "merge_base": merge_base_ref,
            "command": config.validation.gate_command,
            "message": (
                "Broken baseline: sprint baseline gate requires running from the root checkout; "
                "current workspace is not the project toplevel"
            ),
        }

    forge_temp_root = config.project_root / ".forge"
    forge_temp_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="forge-baseline-", dir=forge_temp_root))
    baseline_worktree = temp_root / "worktree"
    try:
        add_worktree = subprocess.run(
            ["git", "worktree", "add", "--detach", str(baseline_worktree), merge_base_ref],
            cwd=config.project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if add_worktree.returncode != 0:
            duration = time.monotonic() - started_monotonic
            stderr = (add_worktree.stderr or "").strip()
            return {
                "status": "error",
                "passed": False,
                "exit_code": add_worktree.returncode,
                "duration_seconds": round(duration, 2),
                "started_at": baseline_started_at,
                "finished_at": datetime.datetime.now(datetime.timezone.utc),
                "merge_base": merge_base_ref,
                "command": config.validation.gate_command,
                "message": (
                    "Broken baseline: unable to create temporary worktree for merge base "
                    f"{merge_base_ref}: {stderr or 'git worktree add failed'}"
                ),
            }

        decision, error, output_tail, resolved_gate_cmd, gate_exit_code = run_gate_full(
            config, baseline_worktree
        )
        duration = time.monotonic() - started_monotonic
        finished_at = datetime.datetime.now(datetime.timezone.utc)
        exit_code = gate_exit_code if gate_exit_code is not None else 1
        if decision == "PASS" and error is None:
            exit_code = gate_exit_code if gate_exit_code is not None else 0
            return {
                "status": "pass",
                "passed": True,
                "exit_code": exit_code,
                "duration_seconds": round(duration, 2),
                "started_at": baseline_started_at,
                "finished_at": finished_at,
                "merge_base": merge_base_ref,
                "command": resolved_gate_cmd,
                "decision": decision,
                "output_tail": output_tail,
                "message": (
                    "Baseline gate passed on sprint merge base "
                    f"{merge_base_ref} before dev iterations started"
                ),
            }

        message = (
            "Broken baseline: configured gate failed on sprint merge base "
            f"{merge_base_ref} before any dev work started ({error or 'Gate returned FAIL'})"
        )
        return {
            "status": "fail",
            "passed": False,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": finished_at,
            "merge_base": merge_base_ref,
            "command": resolved_gate_cmd,
            "decision": decision,
            "output_tail": output_tail,
            "message": message,
        }
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(baseline_worktree)],
            cwd=config.project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(temp_root, ignore_errors=True)


def _agent_cost_tracking_warnings(config: ForgeConfig) -> list[str]:
    """Return sprint-start warnings for configured CLI agents with unknown cost."""

    agents: list[tuple[str, str | None, str | None, str, object | None]] = [
        (
            config.preflight_profile.name,
            config.preflight_profile.cli,
            config.preflight_profile.provider,
            config.preflight_profile.model,
            config.preflight_profile.api_fallback,
        ),
        (
            config.dev_profile.name,
            config.dev_profile.cli,
            config.dev_profile.provider,
            config.dev_profile.model,
            config.dev_profile.api_fallback,
        ),
    ]

    if config.plan.enabled:
        agents.append(
            (
                "planner",
                config.plan.cli,
                config.plan.provider,
                config.plan.model,
                config.plan.api_fallback,
            )
        )

    if config.plan_agent_review.enabled:
        agents.extend(
            (profile.name, profile.cli, profile.provider, profile.model, profile.api_fallback)
            for profile in config.plan_agent_review.profiles
        )

    agents.extend(
        (profile.name, profile.cli, profile.provider, profile.model, profile.api_fallback)
        for profile in config.review_pool
    )

    if config.synthesis_profile is not None:
        agents.append(
            (
                config.synthesis_profile.name,
                config.synthesis_profile.cli,
                config.synthesis_profile.provider,
                config.synthesis_profile.model,
                config.synthesis_profile.api_fallback,
            )
        )

    warnings: list[str] = []
    seen: set[tuple[str, str, str, str | None, str | None]] = set()
    for name, cli, provider, model, api_fallback in agents:
        if provider is not None or cli not in _UNTRACKED_COST_CLIS:
            continue
        fallback_provider = getattr(api_fallback, "provider", None)
        fallback_model = getattr(api_fallback, "model", None)
        key = (name, cli, model, fallback_provider, fallback_model)
        if key in seen:
            continue
        seen.add(key)
        if api_fallback is not None:
            warnings.append(
                f"⚠ CLI cost not tracked for {name} ({cli} CLI, {model}); API fallback to "
                f"{fallback_provider}/{fallback_model} will be tracked if it triggers."
            )
            continue
        warnings.append(
            f"⚠ Cost not tracked for {name} ({cli} CLI, {model}). "
            "Audit totals will exclude this agent's usage."
        )
    return warnings


def parse_manifest_slugs(config: "ForgeConfig", manifest_path: Path) -> list[str]:
    """Extract story slugs from a sprint manifest without full validation.

    Returns an empty list if the manifest cannot be parsed or has no stories.
    Used for pre-launch conflict detection — does not raise on invalid manifests.
    """
    try:
        with open(manifest_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            return []
        stories = raw.get("stories") or raw.get("specs") or []
        if not isinstance(stories, list):
            return []
        slugs: list[str] = []
        for entry in stories:
            if isinstance(entry, dict) and "issue" in entry:
                slugs.append(entry.get("slug", f"issue-{entry['issue']}"))
            elif isinstance(entry, str):
                story_path = (config.project_root / entry).resolve()
                if story_path.exists():
                    task = _build_task_from_story(story_path)
                    slugs.append(task.slug)
                else:
                    # Fallback: use file stem as slug
                    slugs.append(Path(entry).stem)
        return slugs
    except Exception:
        return []


def _release_plan_gates(
    plan_done: dict[str, str],
    file_footprints: dict[str, set[str]],
    plan_gates: dict[str, threading.Event],
    active: dict[str, object],
    phase_lock: threading.Lock,
) -> None:
    """Check newly-planned stories and release their gates if no file overlap.

    Called from the scheduling loop — both the poll interval and after a future
    completes — to avoid deadlock when gated workers block in _run_fresh.
    """
    with phase_lock:
        pd_snapshot = dict(plan_done)

    for pd_slug in pd_snapshot:
        if pd_slug not in file_footprints:
            ws_path = Path(pd_snapshot[pd_slug])
            footprint = _extract_plan_footprint(ws_path)
            file_footprints[pd_slug] = footprint

            # Check overlap with stories already past their gate (in DEV)
            active_dev_files: set[str] = set()
            for other_slug, other_files in file_footprints.items():
                if other_slug != pd_slug and other_slug in active and other_slug not in plan_gates:
                    active_dev_files |= other_files

            overlap = footprint & active_dev_files
            if overlap:
                _log(
                    f"WARNING: {pd_slug} overlaps with active stories on: "
                    f"{', '.join(sorted(overlap))}"
                )
            else:
                if pd_slug in plan_gates:
                    plan_gates[pd_slug].set()
                    del plan_gates[pd_slug]

    # Re-check deferred gates (conflicting story may have finished)
    for deferred_slug, gate in list(plan_gates.items()):
        if deferred_slug in file_footprints:
            active_dev_files = set()
            for other_slug, other_files in file_footprints.items():
                if (
                    other_slug != deferred_slug
                    and other_slug in active
                    and other_slug not in plan_gates
                ):
                    active_dev_files |= other_files
            overlap = file_footprints[deferred_slug] & active_dev_files
            if not overlap:
                gate.set()
                del plan_gates[deferred_slug]


def _validate_story_paths(config: ForgeConfig, manifest_path: Path) -> list[Path]:
    """Backward-compatible shim for tests that patch story path validation.

    The sprint runner now resolves manifests through ``resolve_from_manifest`` and
    no longer needs a separate validation pass here, but daemon tests still patch
    this symbol to isolate ``run_sprint``. Keep a no-op helper so those patches
    remain valid without affecting runtime behavior.
    """
    del config, manifest_path
    return []


def _extract_plan_footprint(workspace_path: Path | None) -> set[str]:
    """Extract the set of files referenced in a plan's steps.

    Reads .forge/plan.md from the workspace, parses YAML plan data, and collects
    file paths from all steps. Returns empty set on any parse failure (best-effort).
    """
    from ..artifacts import PLAN_PATH  # noqa: PLC0415
    from ..task.plan_parser import parse_plan_output  # noqa: PLC0415

    if workspace_path is None:
        return set()
    plan_file = workspace_path / PLAN_PATH
    if not plan_file.exists():
        return set()
    try:
        text = plan_file.read_text(encoding="utf-8")
        plan_data = parse_plan_output(text)
        if plan_data is None:
            return set()
        files: set[str] = set()
        for step in plan_data.get("steps", []):
            files.update(step.get("files", []))
        return files
    except Exception:
        return set()


def _populate_resumed_story_footprint(
    slug: str,
    state: CoordinatorState,
    workspace_path: Path,
) -> CoordinatorState:
    """Populate preflight_likely_files from an existing plan.md for resumed stories."""
    if state.preflight_likely_files:
        return state

    files = sorted(_extract_plan_footprint(workspace_path))
    state.preflight_likely_files = files
    _log(
        f"Resumed story {slug}: registered {len(files)} file(s) from plan.md "
        f"for collision detection: {files}"
    )
    return state


def _register_resumed_story_footprints(
    triages: dict[str, StoryTriage],
    preflight_states: dict[str, CoordinatorState],
) -> dict[str, CoordinatorState]:
    """Ensure resumed dev/review stories contribute likely_files to collision detection."""
    for triage in triages.values():
        if triage.action not in {"review", "dev"} or triage.worktree_path is None:
            continue
        state = preflight_states.get(triage.slug)
        if state is None:
            state = CoordinatorState()
            preflight_states[triage.slug] = state
        _populate_resumed_story_footprint(triage.slug, state, triage.worktree_path)
    return preflight_states


def _run_fresh(
    config: ForgeConfig,
    task: TaskStory,
    sprint_run_id: str,
    sprint_name: str,
    interactive: bool,
    notify: bool,
    effective_auto_merge: bool,
    state_update_fn: "Callable[[dict], None] | None",
    no_pull: bool,
    plan_gate: "threading.Event | None",
    preflight_states: dict[str, CoordinatorState] | None = None,
) -> CoordinatorResult:
    """Run a fresh story, optionally splitting at PLAN_REVIEW for overlap gating."""
    if plan_gate is None:
        # Synthetic issue-backed tasks may be created before query materialization.
        # Fail them explicitly at the sprint seam instead of relying on downstream
        # task text reads inside run_task().
        if task.story_path is None and task.github_issue is not None:
            if task.story_text is None:
                return CoordinatorResult(
                    success=False,
                    phase=Phase.PREFLIGHT,
                    state=CoordinatorState(
                        phase=Phase.PREFLIGHT,
                        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        workspace_path=None,
                        log_dir=None,
                        error="Issue-backed story has no materialized story text",
                        error_type="ValueError",
                    ),
                    message="Issue-backed story has no materialized story text",
                )
            cached_state = (preflight_states or {}).get(task.slug)
            if cached_state is None:
                placeholder_state = CoordinatorState()
                placeholder_state.preflight_verdict = "PROCEED"
                cached_state = placeholder_state
            return run_task(
                config,
                task,
                interactive=interactive,
                auto_merge=effective_auto_merge,
                notify=notify,
                run_id=sprint_run_id,
                sprint_name=sprint_name,
                state_update_fn=state_update_fn,
                no_pull=no_pull,
                cached_preflight_state=cached_state,
                defer_landing=True,
            )
        return run_task(
            config,
            task,
            interactive=interactive,
            auto_merge=effective_auto_merge,
            notify=notify,
            run_id=sprint_run_id,
            sprint_name=sprint_name,
            state_update_fn=state_update_fn,
            no_pull=no_pull,
            cached_preflight_state=(preflight_states or {}).get(task.slug),
            defer_landing=True,
        )

    # Phase 1: run through PLAN only
    plan_result = run_task(
        config,
        task,
        interactive=interactive,
        auto_merge=False,
        notify=notify,
        run_id=sprint_run_id,
        sprint_name=sprint_name,
        state_update_fn=state_update_fn,
        no_pull=no_pull,
        stop_phase=Phase.PLAN_REVIEW,
        cached_preflight_state=(preflight_states or {}).get(task.slug),
        defer_landing=True,
    )

    if not plan_result.success:
        return plan_result

    workspace_path = plan_result.state.workspace_path
    if workspace_path is None:
        return plan_result

    # Signal plan completion so scheduler can check footprints
    if state_update_fn is not None:
        state_update_fn(
            {
                "spec": task.slug,
                "phase": "PLAN_DONE",
                "workspace_path": str(workspace_path),
            }
        )

    # Wait for scheduler to release the gate (with safety timeout)
    plan_gate.wait(timeout=7200)

    # Phase 2: continue from DEV
    return run_from_dev(
        config,
        task,
        workspace_path,
        interactive=interactive,
        auto_merge=effective_auto_merge,
        notify=notify,
        run_id=sprint_run_id,
        sprint_name=sprint_name,
        state_update_fn=state_update_fn,
        no_pull=no_pull,
        cached_preflight_state=(preflight_states or {}).get(task.slug),
        defer_landing=True,
    )


def _run_single_story(
    config: ForgeConfig,
    task: TaskStory,
    triage: "StoryTriage | None",
    sprint_run_id: str,
    sprint_name: str,
    interactive: bool,
    notify: bool,
    resume: bool,
    effective_auto_merge: bool,
    state_update_fn: "Callable[[dict], None] | None",
    no_pull: bool = False,
    plan_gate: "threading.Event | None" = None,
    preflight_states: dict[str, CoordinatorState] | None = None,
) -> "tuple[TaskStory, CoordinatorResult, float, datetime.datetime, datetime.datetime]":
    """Execute a single story and return (task, result, elapsed, started_at, finished_at).

    Designed to run in a worker thread. Dispatches to run_task / run_from_review /
    run_from_dev based on triage (resume mode) or always run_task (fresh mode).

    When *plan_gate* is provided (parallel overlap detection), fresh runs are split:
    1. run_task with stop_phase=PLAN_REVIEW
    2. Signal PLAN_DONE via state_update_fn
    3. Wait on plan_gate for scheduler release
    4. run_from_dev to continue
    """
    started_at = datetime.datetime.now(datetime.timezone.utc)
    set_worker_slug(task.slug)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)

    if state_update_fn is not None:
        state_update_fn({"spec": task.slug, "phase": "STARTING"})

    try:
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
                    no_pull=no_pull,
                    cached_preflight_state=(preflight_states or {}).get(task.slug),
                    defer_landing=True,
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
                    no_pull=no_pull,
                    cached_preflight_state=(preflight_states or {}).get(task.slug),
                    defer_landing=True,
                )
            else:
                result = _run_fresh(
                    config,
                    task,
                    sprint_run_id,
                    sprint_name,
                    interactive,
                    notify,
                    effective_auto_merge,
                    state_update_fn,
                    no_pull,
                    plan_gate,
                    preflight_states,
                )
        else:
            result = _run_fresh(
                config,
                task,
                sprint_run_id,
                sprint_name,
                interactive,
                notify,
                effective_auto_merge,
                state_update_fn,
                no_pull,
                plan_gate,
                preflight_states,
            )
    except Exception as exc:
        _log(f"ERROR {task.slug}: worker thread raised {type(exc).__name__}: {exc}")
        failure_state = CoordinatorState(
            phase=Phase.ESCALATE,
            started_at=started_at.isoformat(),
            workspace_path=workspace_path,
            log_dir=_make_story_log_dir(config, task.slug, sprint_name),
            error=f"Worker exception: {exc}",
            error_type=type(exc).__name__,
        )
        result = CoordinatorResult(
            success=False,
            phase=Phase.ESCALATE,
            state=failure_state,
            message=f"Worker thread raised {type(exc).__name__}: {exc}",
        )

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    set_worker_slug("")
    elapsed = (finished_at - started_at).total_seconds()
    return task, result, elapsed, started_at, finished_at


def _make_worker_phase_fn(
    slug: str,
    worker_phases: dict[str, str],
    phase_lock: threading.Lock,
    outer_fn: "Callable[[dict], None] | None",
    plan_done: "dict[str, str] | None" = None,
    state_writer: "SprintStateWriter | None" = None,
) -> "Callable[[dict], None]":
    """Return a thread-safe state_update_fn wrapper that tracks per-worker phase.

    Updates worker_phases[slug] from updates["phase"] and (under lock) forwards
    updates to the outer daemon state_update_fn if provided.

    When *plan_done* is provided and a PLAN_DONE phase update arrives, stores
    the workspace_path in plan_done[slug] for the scheduler to read.

    When *state_writer* is provided, phase transitions are also written to the
    live sprint state file so ``forge sprint-status`` reflects the current phase.
    """

    def _update(updates: dict) -> None:
        phase = updates.get("phase", "")
        with phase_lock:
            if phase:
                worker_phases[slug] = phase
                if state_writer is not None:
                    incoming_detail = updates.get("detail")
                    _detail_updates: dict[str, object] = (
                        dict(incoming_detail) if isinstance(incoming_detail, dict) else {}
                    )
                    if phase == "VALIDATE" and not _detail_updates:
                        _detail_updates = {"gate_status": "running"}
                    update_kwargs: dict[str, object] = {"phase": phase}
                    if "complexity" in updates:
                        update_kwargs["complexity"] = updates["complexity"]
                    if "cost_usd" in updates:
                        update_kwargs["cost_usd"] = updates["cost_usd"]
                    if _detail_updates:
                        update_kwargs["detail"] = _detail_updates
                    state_writer.update(slug, **update_kwargs)
            if phase == "PLAN_DONE" and plan_done is not None:
                ws = updates.get("workspace_path", "")
                if ws:
                    plan_done[slug] = ws
            if outer_fn is not None:
                outer_fn(updates)

    return _update


def _poll_queued_pr(pr_url: str, project_root: Path, timeout_seconds: int) -> dict[str, str]:
    """Poll GitHub until a queued PR is merged, closed, or times out."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            proc = subprocess.run(
                ["gh", "pr", "view", pr_url, "--json", "state", "-q", ".state"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=30,
            )
        except Exception:
            return {"status": "timeout"}

        if proc.returncode == 0:
            state = proc.stdout.strip()
            if state == "MERGED":
                return {"status": "merged"}
            if state == "CLOSED":
                return {"status": "closed"}

        if time.monotonic() >= deadline:
            return {"status": "timeout"}
        time.sleep(30)


def _classify_and_record(
    task: TaskStory,
    result: CoordinatorResult,
    dag: StoryDAG,
    merged_slugs: set[str],
) -> tuple[int, int, int]:
    """Classify result and update DAG state. Returns (succeeded, failed, skipped) deltas."""
    preflight_verdict = result.state.preflight_verdict
    landing_status = getattr(result, "landing_status", None)
    delta_succeeded = 0
    delta_failed = 0
    delta_skipped = 0

    if preflight_verdict == "ALREADY_DONE":
        delta_skipped = 1
        merged_slugs.add(task.slug)
        dag.mark_complete(task.slug)
        return delta_succeeded, delta_failed, delta_skipped

    if landing_status == "landed":
        delta_succeeded = 1
        merged_slugs.add(task.slug)
        dag.mark_complete(task.slug)
    elif landing_status == "failed":
        delta_failed = 1
        dag.mark_skipped(task.slug)
    elif landing_status == "pending_integration":
        # Approved but merge deferred or queued — counts as succeeded, not yet in DAG
        delta_succeeded = 1
        dag.mark_skipped(task.slug)
    elif result.success:
        # No merge operation performed (on_approve=none or similar)
        delta_succeeded = 1
        dag.mark_skipped(task.slug)
    else:
        delta_failed = 1
        dag.mark_skipped(task.slug)

    return delta_succeeded, delta_failed, delta_skipped


def _refresh_external_satisfied(
    dag: StoryDAG,
    all_tasks: list[TaskStory],
    config: ForgeConfig,
    merged_slugs: set[str] | None = None,
) -> set[str]:
    """Re-check unmet external dependencies and mark newly satisfied slugs.

    External issue dependencies can close while a sprint is running. Keeping this
    refresh in the scheduler loop lets dependents become ready without requiring
    operators to stop and resume the sprint.
    """
    manifest_slugs = {task.slug for task in all_tasks}
    external_deps = {
        dep
        for task in dag.remaining()
        for dep in dag.unmet_deps(task.slug)
        if dep not in manifest_slugs
    }
    if not external_deps:
        return set()

    dependent_tasks = [
        task for task in all_tasks if any(dep in external_deps for dep in task.depends_on)
    ]
    satisfied = resolve_satisfied_dependencies(
        dependent_tasks,
        project_root=config.project_root,
        base_branch=config.workspace.base_branch,
        branch_pattern=config.workspace.branch_pattern,
    )
    newly_satisfied = {
        slug for slug in satisfied if slug in external_deps and slug not in dag._completed
    }
    for slug in sorted(newly_satisfied):
        dag.mark_complete(slug)
        if merged_slugs is not None:
            merged_slugs.add(slug)
        _log(f"dep satisfied: {slug} (GitHub issue closed)")
    return newly_satisfied


def run_sprint(
    config: ForgeConfig,
    sprint: "Path | ResolvedSprint",
    *,
    auto_merge: bool = False,
    interactive: bool = False,
    notify: bool = False,
    resume: bool = False,
    state_update_fn: "Callable[[dict], None] | None" = None,
    no_pull: bool = False,
    run_id: str | None = None,
    dropped_slugs: "dict[str, str] | None" = None,
    skipped_issues: "list | None" = None,
) -> SprintResult:
    """Run all stories in a sprint with optional concurrency.

    Accepts either a ``Path`` to a sprint.yaml manifest (backward-compatible)
    or a pre-built ``ResolvedSprint`` object (produced by query mode or
    ``resolve_from_manifest``).  The function body has no path-shaped
    assumptions — it operates entirely on the resolved object.

    When max_parallel > 1, stories with no unmet dependencies are launched
    concurrently up to max_parallel. Budget is pooled across all workers.
    Merge ordering respects dependency order when auto_merge is True.

    When max_parallel == 1 (default), behavior is identical to the original
    sequential runner.

    Args:
        config: Loaded ForgeConfig for the project.
        sprint: Either a Path to sprint.yaml or a pre-built ResolvedSprint.
        auto_merge: If True, merge each story's branch after APPROVE.
        interactive: If True, pause for human review at each story.
        resume: If True, triage each story to find the optimal re-entry point
            (skip_merged / review / dev / full) and carry forward prior costs.

    Returns:
        SprintResult with per-story outcomes and aggregate stats.
    """
    if isinstance(sprint, ResolvedSprint):
        resolved = sprint
    else:
        # Backward-compat: Path was passed — resolve via the shared helper so
        # tests can patch the boundary and query-mode behavior stays aligned.
        resolved = resolve_from_manifest(sprint, config.project_root)

    # Defensive scrub for the root checkout used by sprint commands.
    _scrub_root_forge_artifacts(config)
    sweep_orphan_worktrees(config.project_root, config)

    max_parallel = (
        resolved.max_parallel if resolved.max_parallel is not None else config.sprint.max_parallel
    )
    worker_timeout_seconds = (
        resolved.worker_timeout_seconds
        if resolved.worker_timeout_seconds is not None
        else config.sprint.worker_timeout_seconds
    )

    # Build unified context mapping: (task, source, canonical_ref) per entry
    task_entries = resolved.stories
    slug_to_context: dict[str, tuple[TaskStory, StorySource, str]] = {
        task.slug: (task, source, canonical_ref) for task, source, canonical_ref in task_entries
    }
    dependent_slugs = {dep for task, _src, _ref in task_entries for dep in task.depends_on}

    total = len(task_entries)
    noun = "stories" if total != 1 else "story"
    print(
        f'[sprint] "{resolved.name}"  {total} {noun}  budget=${resolved.budget_usd:.2f}'
        f"  parallel={max_parallel}",
        file=sys.stderr,
        flush=True,
    )
    for warning in _agent_cost_tracking_warnings(config):
        _log(warning)
    for task, _src, _ref in task_entries:
        for phrase in task.dependency_warnings:
            _log(
                "WARN: dependency-shaped prose ignored for "
                f"{task.slug} ({task.name}): {phrase!r}; "
                "declare dependencies in YAML frontmatter"
            )

    # Sprint-level structured logger
    _cli_run_id = run_id
    _sprint_run_id = _generate_run_id()
    _sprint_logger = StructuredLogger(
        run_id=_sprint_run_id,
        project=config.project,
        task=resolved.name,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    _sprint_logger.emit(
        "run_start",
        stories=[ref for _, _, ref in task_entries],
        budget_usd=resolved.budget_usd,
        max_parallel=max_parallel,
        resume=resume,
    )

    # Create sprint-level log directory
    _sprint_log_dir = config.project_root / ".forge" / "logs" / resolved.name
    try:
        _sprint_log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        _sprint_log_dir = None  # type: ignore[assignment]

    # Stable sprint_id — does not change across run_id rollovers or --resume.
    # Used to aggregate story outcomes across all worker-process boundaries.
    _sprint_id: str | None = None
    try:
        _sprint_id = _get_or_create_sprint_id(resolved.name, config.project_root)
    except Exception:
        pass

    baseline_started_at = datetime.datetime.now(datetime.timezone.utc)
    baseline_gate = _run_baseline_gate(config, resolved)
    resolved.baseline_gate = baseline_gate
    _log(str(baseline_gate.get("message", "Baseline gate check completed")))
    if not bool(baseline_gate.get("passed", False)):
        _write_sprint_audit(
            manifest=resolved,
            result=SprintResult(
                name=resolved.name,
                specs_total=total,
                specs_succeeded=0,
                specs_failed=total,
                specs_skipped=0,
                total_cost_usd=0.0,
                budget_usd=resolved.budget_usd,
                results=[],
                stopped_reason="broken_baseline",
            ),
            canonical_refs=[ref for _, _, ref in task_entries],
            started_at=baseline_started_at,
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            duration=float(baseline_gate.get("duration_seconds", 0.0)),
            project_root=config.project_root,
            slug_map={ref: task.slug for task, _src, ref in task_entries},
            tasks_by_slug={task.slug: task for task, _src, _ref in task_entries},
            sprint_id=_sprint_id,
            dropped_slugs=dropped_slugs,
            skipped_issues=skipped_issues,
        )
        raise RuntimeError(str(baseline_gate.get("message", "Broken baseline")))

    started_at = datetime.datetime.now(datetime.timezone.utc)
    accumulated_cost = 0.0
    prior_cost = 0.0
    results: list[tuple[str, CoordinatorResult]] = []
    if notify and config.notifications.backend not in ("ntfy", "none"):
        from ..notify_backends import send_notifications

        send_notifications(
            config,
            f'TheForge: sprint started \u2014 "{resolved.name}"',
            f"{total} stories \u00b7 budget ${resolved.budget_usd:.2f}",
        )
    specs_succeeded = 0
    specs_failed = 0
    specs_skipped = 0
    stopped_reason: str | None = None
    ci_halt_slug: str | None = None
    merged_slugs: set[str] = set()

    # Derive slug_to_spec from unified context mapping
    slug_to_spec: dict[str, str] = {slug: ctx[2] for slug, ctx in slug_to_context.items()}

    # Resume mode: triage all stories and carry forward prior costs
    triages: dict[str, StoryTriage] = {}
    if resume:
        prior_cost = _read_prior_sprint_cost(config.project_root)
        if prior_cost > 0.0:
            _log(f"Resuming with prior cost: ${prior_cost:.2f}")
        _log("Triaging specs...")
        for slug, (task, _src, canonical_ref) in slug_to_context.items():
            triage = _triage_spec(canonical_ref, config, config.project_root, task=task)
            triages[canonical_ref] = triage
            _log(
                f"  {triage.slug:<20} {triage.action.upper().replace('_', ' ')} ({triage.reason})"
            )

    # Build satisfied set: closed dep slugs detected at manifest build time,
    # resume-mode skip states, plus any cross-sprint depends_on slugs whose
    # branch is already merged to the base branch.
    pre_satisfied: set[str] = set(resolved.closed_dependency_slugs)
    if resume:
        for triage in triages.values():
            if triage.action in ("skip_merged", "skip"):
                pre_satisfied.add(triage.slug)

    # Build DAG
    all_tasks = [ctx[0] for ctx in slug_to_context.values()]
    satisfied_slugs = resolve_satisfied_dependencies(
        all_tasks,
        project_root=config.project_root,
        base_branch=config.workspace.base_branch,
        branch_pattern=config.workspace.branch_pattern,
        pre_satisfied=pre_satisfied,
    )
    normalized = normalize_dependency_plan(all_tasks, satisfied=satisfied_slugs)
    _preflight_tasks = [
        task
        for task in normalized.tasks
        if not (task.story_path is None and task.github_issue is not None)
    ]
    preflight_states = run_batch_preflight(
        _preflight_tasks,
        config,
        sprint_name=resolved.name,
        no_pull=no_pull,
        max_parallel=max_parallel,
        notify=notify,
    )
    for _task in normalized.tasks:
        if _task.slug not in preflight_states:
            _placeholder_state = CoordinatorState()
            _placeholder_state.preflight_verdict = "PROCEED"
            preflight_states[_task.slug] = _placeholder_state
    if resume:
        _register_resumed_story_footprints(triages, preflight_states)
    bundle_assignments = compute_bundle_assignments(preflight_states, normalized.tasks)
    if bundle_assignments:
        _log(f"Computed deterministic bundles: {bundle_assignments}")
    synthetic_edges = compute_synthetic_edges(preflight_states, normalized.tasks)
    if synthetic_edges:
        _log(f"Injected synthetic dependency constraints for {len(synthetic_edges)} stories")
    augmented_tasks = inject_synthetic_deps(normalized.tasks, synthetic_edges)
    blocked_slugs = dict(normalized.blocked)
    try:
        dag = build_dag(augmented_tasks, satisfied=satisfied_slugs)
    except ValueError as exc:
        raise ValueError(f"{exc} Synthetic collision edges: {synthetic_edges}") from exc

    # Dependencies already satisfied outside this sprint still count as landed
    # for deferred integration ordering.
    merged_slugs.update(satisfied_slugs)

    # Resume mode: pre-mark skip_merged / skip stories as complete in DAG.
    # skip_merged stories are already merged and should satisfy dependencies
    # immediately, but they still count as skipped in sprint aggregates.
    if resume:
        for slug, (_task, _src, canonical_ref) in slug_to_context.items():
            triage = triages.get(canonical_ref)
            if triage and triage.action in ("skip_merged", "skip"):
                _log(f"SKIP {slug} ({triage.reason})")
                if triage.action == "skip_merged":
                    merged_slugs.add(slug)
                    dag.mark_complete(slug)
                    specs_skipped += 1
                else:
                    dag.mark_skipped(slug)
                    specs_skipped += 1

    auto_enabled_dependency_merges = dependent_slugs - satisfied_slugs - merged_slugs
    if (
        max_parallel > 1
        and not auto_merge
        and config.workspace.on_approve != "merge-pr"
        and auto_enabled_dependency_merges
    ):
        listed = ", ".join(sorted(auto_enabled_dependency_merges))
        _log(
            "WARN: parallel dependency merging auto-enabled for "
            f"{listed} so dependent stories are not silently skipped"
        )

    # Stories blocked by unresolved external dependencies never enter the DAG.
    for slug, blocked_by in blocked_slugs.items():
        _log(f"SKIPPED {slug} (blocked: {', '.join(blocked_by)})")
        dag.mark_skipped(slug)
        specs_skipped += 1

    # Stories dropped pre-launch (e.g. re-exec collision) never enter the DAG.
    # They surface with a distinct DROPPED/PRESERVED outcome in sprint-audit and
    # the live state file so operators can see exactly which stories did not
    # run and why — a silent WARNING is not enough visibility.
    #
    # ``preserved-escalated`` is a disjoint case: the worktree is intentionally
    # kept for human review, and counts as skipped (not failed) in aggregates.
    _dropped_slugs: dict[str, str] = dict(dropped_slugs or {})
    for slug, reason in _dropped_slugs.items():
        if slug not in slug_to_context:
            continue
        if reason == "preserved-escalated":
            _log(f"PRESERVED {slug} (escalated worktree held for review)")
            dag.mark_skipped(slug)
            specs_skipped += 1
        else:
            _log(f"DROPPED {slug} (reason: {reason})")
            dag.mark_skipped(slug)
            specs_failed += 1

    # Persist resume-time already-completed stories before any possible re-exec
    # handoff so later generations can recover the full logical sprint history.
    if resume:
        _prior_accumulated_by_ref: dict[str, dict] = {}
        if _sprint_id:
            from .audit import _load_accumulated_stories  # noqa: PLC0415

            _prior_accumulated_by_ref = {
                story["canonical_ref"]: story
                for story in _load_accumulated_stories(_sprint_id, config.project_root)
                if "canonical_ref" in story
            }

        def _already_done_story_entry(
            canonical_ref: str,
            slug: str,
            *,
            depends_on: list[str],
        ) -> dict:
            display_key = (
                f"Issue #{canonical_ref.split(':')[1]}"
                if canonical_ref.startswith("issue:")
                else canonical_ref
            )
            return {
                "canonical_ref": canonical_ref,
                "path": display_key,
                "slug": slug,
                "outcome": "ALREADY_DONE",
                "verdict": None,
                "cost_usd": 0.0,
                "story_run_id": run_id,
                "preflight": None,
                "preflight_original_verdict": None,
                "preflight_source_run_id": None,
                "error": None,
                "error_type": None,
                "merge": False,
                "batch": 0,
                "depends_on": depends_on,
            }

        _resume_accumulated_by_ref: dict[str, dict] = dict(_prior_accumulated_by_ref)
        for _canonical_ref, _triage in triages.items():
            if _triage.action != "skip_merged":
                continue
            _resume_slug = _triage.slug
            _resume_accumulated_by_ref.setdefault(
                _canonical_ref,
                _already_done_story_entry(
                    _canonical_ref,
                    _resume_slug,
                    depends_on=list(
                        getattr(
                            slug_to_context.get(_resume_slug, (None, None, None))[0],
                            "depends_on",
                            None,
                        )
                        or []
                    ),
                ),
            )
        for _closed_slug in sorted(resolved.closed_dependency_slugs):
            _canonical_ref = f"issue:{_closed_slug.removeprefix('issue-')}"
            if _canonical_ref in triages:
                continue
            _resume_accumulated_by_ref.setdefault(
                _canonical_ref,
                _already_done_story_entry(_canonical_ref, _closed_slug, depends_on=[]),
            )
        if _resume_accumulated_by_ref:
            persist_accumulated_story_state(
                _sprint_id,
                resolved.name,
                config.project_root,
                list(_resume_accumulated_by_ref.values()),
            )

    # Initialise live state file for forge sprint-status (only when a CLI run_id
    # is present — headless/test invocations without a run_id skip this).
    _state_writer: SprintStateWriter | None = None
    if run_id:
        _bundle_candidate_slugs: set[str] = {s for bundle in bundle_assignments for s in bundle}
        _initial_stories: list[dict] = []
        _initial_story_slugs: set[str] = set()
        for _slug, (_task, _src, _canonical_ref) in slug_to_context.items():
            _display_key = (
                f"Issue #{_canonical_ref.split(':')[1]}"
                if _canonical_ref.startswith("issue:")
                else _canonical_ref
            )
            _blocked_by = list(blocked_slugs.get(_slug, []))
            _drop_reason = _dropped_slugs.get(_slug)
            _triage = triages.get(_canonical_ref) if resume else None
            if _drop_reason == "preserved-escalated":
                _status = "preserved"
                _blocked_by = [f"preserved: {_drop_reason}"]
                _detail = {"final_outcome": "ESCALATE"}
            elif _drop_reason:
                _status = "failed"
                _blocked_by = [f"dropped: {_drop_reason}"]
                _detail = {"final_outcome": "ESCALATE"}
            elif _triage and _triage.action == "skip_merged":
                _status = "done"
                _detail = {"final_outcome": "ALREADY_DONE"}
            elif _triage and _triage.action == "skip":
                _status = "skipped"
                _detail = {"final_outcome": "SKIPPED"}
            elif _blocked_by:
                _status = "blocked"
                _detail = {}
            else:
                _status = "waiting"
                _detail = {}
            _initial_stories.append(
                {
                    "slug": _slug,
                    "path": _display_key,
                    "status": _status,
                    "phase": "PREFLIGHT" if _status == "waiting" else None,
                    "cost_usd": 0.0,
                    "bundle_candidate": _slug in _bundle_candidate_slugs,
                    "blocked_by": _blocked_by,
                    "complexity": None,
                    "detail": _detail,
                }
            )
            _initial_story_slugs.add(_slug)
        for _closed_slug in sorted(resolved.closed_dependency_slugs):
            if _closed_slug in _initial_story_slugs:
                continue
            _issue_number = _closed_slug.removeprefix("issue-")
            _initial_stories.append(
                {
                    "slug": _closed_slug,
                    "path": f"Issue #{_issue_number}" if _issue_number.isdigit() else _closed_slug,
                    "status": "done",
                    "phase": None,
                    "cost_usd": 0.0,
                    "bundle_candidate": False,
                    "blocked_by": [],
                    "complexity": None,
                    "detail": {"final_outcome": "ALREADY_DONE"},
                }
            )
            _initial_story_slugs.add(_closed_slug)
        _state_writer = SprintStateWriter(
            run_id,
            config.project_root,
            resolved.name,
            sprint_id=_sprint_id,
        )
        _state_writer.init(_initial_stories)

    # Parallel scheduling state
    active: dict[str, Future[object]] = {}
    cost_lock = threading.Lock()
    story_times: dict[str, tuple[datetime.datetime, datetime.datetime]] = {}
    batch_assignments: dict[str, int] = {}
    batch_number = 0
    worker_phases: dict[str, str] = {}
    phase_lock = threading.Lock()
    pending_integration: dict[str, tuple[TaskStory, CoordinatorResult]] = {}
    queued_prs: dict[str, tuple[TaskStory, CoordinatorResult, str]] = {}
    _submission_counter = [0]  # mutable for closure capture; counts submitted stories

    # Overlap detection state (plan gates)
    file_footprints: dict[str, set[str]] = {}  # slug -> files from plan
    plan_gates: dict[str, threading.Event] = {}  # slug -> gate for PLAN→DEV pause
    plan_done: dict[str, str] = {}  # slug -> workspace_path (set by phase callback)
    use_plan_gates = max_parallel > 1  # only for parallel mode

    def _attempt_integration(
        slug: str,
        task: TaskStory,
        result: CoordinatorResult,
    ) -> bool:
        """Attempt to land an approved story under integration_lock.

        Returns True when integration was attempted (success, failure, or queued).
        Returns False when dependencies are unmet — caller should retry later.

        This is the sole merge site for sprint execution.  Workers never merge;
        they set landing_status="pending_integration" and return.
        """
        nonlocal stopped_reason, ci_halt_slug

        if not all(dep in merged_slugs for dep in task.depends_on):
            result.landing_status = "pending_integration"
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            return False

        branch = config.workspace.branch_pattern.format(slug=slug)
        wt = config.project_root / config.workspace.path_pattern.format(slug=slug)

        # Read effective mode from the pending merge action stored by _finalize_approve.
        # Falls back to config.workspace.on_approve for legacy/direct callers.
        effective_on_approve = (result.merge or {}).get("action") or config.workspace.on_approve

        story_logger = StructuredLogger(
            run_id=_sprint_run_id,
            project=config.project,
            task=task.slug,
            log_file=config.log.log_file,
            enabled=config.log.enabled,
            project_root=config.project_root,
        )

        with integration_lock(config.project_root):
            from ..coordinator.completion import land_story  # noqa: PLC0415

            parsed_review = (
                result.state.review_results[-1] if result.state.review_results else None
            )
            merge_info, landing_status = land_story(
                config,
                task,
                branch,
                wt,
                parsed_review,
                result.state,
                effective_on_approve,
                logger=story_logger,
                run_id=_sprint_run_id,
            )

        result.merge = merge_info
        result.landing_status = landing_status
        if landing_status == "failed":
            result.success = False

        if merge_info.get("merged"):
            merged_slugs.add(slug)
            dag.mark_complete(slug)
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            if effective_on_approve == "merge-pr" and not merge_info.get(
                "auto_merge_queued", False
            ):
                ci_result = poll_required_checks(
                    config.project_root,
                    config.workspace.base_branch,
                    config.workspace.ci_check_timeout_seconds,
                )
                if ci_result["status"] in {"fail", "timeout"}:
                    failing = ", ".join(ci_result["failing_checks"]) or "pending required checks"
                    stopped_reason = (
                        "Required CI checks "
                        f"{ci_result['status']} after merging {slug} "
                        f"at {ci_result['sha']}: {failing}"
                    )
                    ci_halt_slug = slug
                    _log(
                        f"HALT {slug}: required CI checks {ci_result['status']} "
                        f"for {ci_result['sha']} ({failing})"
                    )
            return True

        if merge_info.get("merge_queued"):
            queued_prs[slug] = (task, result, merge_info["pr_url"])
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            _log(f"INFO {slug}: PR auto-merge queued; waiting for GitHub to report MERGED")
            return True

        result.state.error = merge_info.get("error") or "integration failed"
        _log(f"WARN {slug}: integration failed: {merge_info.get('error')}")
        _write_story_audit(config, task, result, sprint_id=_sprint_id)
        return True

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while not dag.is_done():
            _log(f"[debug] loop: active={list(active.keys())} fin={dag._finished}")
            _refresh_external_satisfied(dag, all_tasks, config, merged_slugs)
            ready = [t for t in dag.ready() if t.slug not in active]

            for task in ready:
                blocked_by_queued = [dep for dep in task.depends_on if dep in queued_prs]
                if blocked_by_queued:
                    dependency_failed = False
                    for dep in blocked_by_queued:
                        dep_task, dep_result, dep_pr_url = queued_prs[dep]
                        poll_result = _poll_queued_pr(
                            dep_pr_url,
                            config.project_root,
                            config.workspace.merge_wait_timeout_seconds,
                        )
                        if poll_result["status"] == "merged":
                            merged_slugs.add(dep)
                            dag.mark_complete(dep)
                            del queued_prs[dep]
                            _write_story_audit(config, dep_task, dep_result, sprint_id=_sprint_id)
                        else:
                            dep_result.landing_status = "failed"
                            dep_result.success = False
                            if poll_result["status"] == "timeout":
                                dep_result.state.error = (
                                    f"Queued PR timed out after "
                                    f"{config.workspace.merge_wait_timeout_seconds}s: {dep_pr_url}"
                                )
                            else:
                                dep_result.state.error = (
                                    f"Queued PR {poll_result['status']}: {dep_pr_url}"
                                )
                            del queued_prs[dep]
                            _write_story_audit(config, dep_task, dep_result, sprint_id=_sprint_id)
                            _log(
                                f"✗ {dep}: queued PR {poll_result['status']} "
                                "before dependent dispatch"
                            )
                            dependency_failed = True
                    if dependency_failed:
                        continue
                    if any(dep in queued_prs for dep in task.depends_on):
                        continue

                # Cap concurrent submissions at max_parallel
                if len(active) >= max_parallel:
                    break

                with cost_lock:
                    cumulative = prior_cost + accumulated_cost
                if cumulative >= resolved.budget_usd:
                    dag.mark_skipped(task.slug)
                    specs_skipped += 1
                    if stopped_reason is None:
                        stopped_reason = (
                            f"Budget exhausted (${cumulative:.2f} >= ${resolved.budget_usd:.2f})"
                        )
                        if notify and config.notifications.backend not in ("ntfy", "none"):
                            from ..notify_backends import send_notifications

                            send_notifications(
                                config,
                                f'TheForge: budget exceeded \u2014 "{resolved.name}"',
                                f"${cumulative:.2f} >= ${resolved.budget_usd:.2f}"
                                " \u2014 remaining stories skipped",
                            )
                    _log(f"SKIPPED {task.slug} (budget exhausted)")
                    if _state_writer is not None:
                        _state_writer.update(task.slug, status="skipped")
                    continue

                # Eager merge for sequential mode; disabled in parallel mode
                effective_am = (
                    False if max_parallel > 1 else (auto_merge or task.slug in dependent_slugs)
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
                if _state_writer is not None:
                    _state_writer.update(task.slug, status="running")

                # Create plan gate for fresh parallel runs
                gate: threading.Event | None = None
                if use_plan_gates and triage is None:
                    gate = threading.Event()
                    plan_gates[task.slug] = gate

                worker_config = config

                state_fn = _make_worker_phase_fn(
                    task.slug,
                    worker_phases,
                    phase_lock,
                    state_update_fn,
                    plan_done=plan_done if use_plan_gates else None,
                    state_writer=_state_writer,
                )
                fut = pool.submit(
                    _run_single_story,
                    worker_config,
                    task,
                    triage,
                    _sprint_run_id,
                    resolved.name,
                    interactive,
                    notify,
                    resume,
                    effective_am,
                    state_fn,
                    no_pull,
                    gate,
                    preflight_states,
                )
                active[task.slug] = fut

            _log(
                f"[debug] post-submit: active={list(active.keys())}"
                f" queued_prs={list(queued_prs.keys())}"
            )
            if not active and not queued_prs:
                # Deadlock: remaining tasks have unmet or budget-blocked deps
                # Release any pending plan gates so worker threads can exit
                for g_slug, _gate in plan_gates.items():
                    _log(f"Releasing plan gate for {g_slug} (deadlock cleanup)")
                    _gate.set()
                plan_gates.clear()
                for t in dag.remaining():
                    unmet = dag.unmet_deps(t.slug)
                    if unmet:
                        dep_list = ", ".join(unmet)
                        _log(f"SKIPPED {t.slug} (dependency failed: {dep_list})")
                    else:
                        _log(f"SKIPPED {t.slug} (blocked)")
                    dag.mark_skipped(t.slug)
                    specs_skipped += 1
                    if _state_writer is not None:
                        _state_writer.update(t.slug, status="skipped")
                break

            # No active workers but queued PRs are still in flight.
            # Poll each queued PR directly so dependents can be dispatched
            # once the PR lands — do not declare deadlock while PRs are pending.
            if not active and queued_prs:
                for _qp_slug in list(queued_prs):
                    _qp_task, _qp_result, _qp_pr_url = queued_prs[_qp_slug]
                    _qp_poll = _poll_queued_pr(
                        _qp_pr_url,
                        config.project_root,
                        config.workspace.merge_wait_timeout_seconds,
                    )
                    if _qp_poll["status"] == "merged":
                        merged_slugs.add(_qp_slug)
                        dag.mark_complete(_qp_slug)
                        _qp_result.landing_status = "landed"
                        del queued_prs[_qp_slug]
                        _write_story_audit(config, _qp_task, _qp_result, sprint_id=_sprint_id)
                        _log(f"INFO {_qp_slug}: queued PR merged; unblocking dependents")
                    else:
                        specs_succeeded -= 1
                        specs_failed += 1
                        _qp_result.landing_status = "failed"
                        _qp_result.success = False
                        if _qp_poll["status"] == "timeout":
                            _qp_result.state.error = (
                                f"Queued PR timed out after "
                                f"{config.workspace.merge_wait_timeout_seconds}s: {_qp_pr_url}"
                            )
                        else:
                            _qp_result.state.error = (
                                f"Queued PR {_qp_poll['status']}: {_qp_pr_url}"
                            )
                        del queued_prs[_qp_slug]
                        _write_story_audit(config, _qp_task, _qp_result, sprint_id=_sprint_id)
                        _log(f"✗ {_qp_slug}: queued PR {_qp_poll['status']} (no active workers)")
                continue

            _log(f"[debug] calling wait() with {len(active)} active futures")
            # Use a short poll interval when plan gates are pending so the
            # scheduler can release gated workers between polls.  Without
            # this, gated workers block in _run_fresh waiting for their gate
            # while the scheduler blocks here waiting for a future to finish
            # — a deadlock.
            _wt_float = float(worker_timeout_seconds)
            _poll_interval = 2.0 if plan_gates else _wt_float
            _total_waited = 0.0
            done_futs: set = set()
            while not done_futs and _total_waited < _wt_float:
                _current_interval = _poll_interval
                done_futs, _ = wait(
                    list(active.values()),
                    return_when=FIRST_COMPLETED,
                    timeout=_current_interval,
                )
                if not done_futs and use_plan_gates:
                    # Service plan gates while polling
                    _release_plan_gates(plan_done, file_footprints, plan_gates, active, phase_lock)
                    # All gates released — switch to long poll
                    if not plan_gates:
                        _poll_interval = _wt_float
                _total_waited += _current_interval

            _log(f"[debug] wait() returned: {len(done_futs)} done")
            batch_number += 1

            if not done_futs:
                # Timeout: all active workers hung for >worker_timeout_seconds — cancel and fail
                for g_slug, _gate in plan_gates.items():
                    _log(f"TIMEOUT releasing plan gate for {g_slug}")
                    _gate.set()
                plan_gates.clear()
                for slug, fut in list(active.items()):
                    fut.cancel()
                    _log(
                        f"TIMEOUT {slug} (worker unresponsive after "
                        f"{worker_timeout_seconds}s — marking as failed)"
                    )
                    spec_str = slug_to_spec[slug]
                    timed_out_at = datetime.datetime.now(datetime.timezone.utc)
                    story_started_at = story_times.get(slug, (timed_out_at, timed_out_at))[0]
                    _timeout_state = CoordinatorState(
                        phase=Phase.ESCALATE,
                        started_at=story_started_at.isoformat(),
                        workspace_path=(
                            config.project_root / config.workspace.path_pattern.format(slug=slug)
                        ),
                        log_dir=_make_story_log_dir(config, slug, resolved.name),
                        error=f"Worker timeout (>{worker_timeout_seconds}s)",
                        error_type="TimeoutError",
                    )
                    _timeout_result = CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=_timeout_state,
                        message=f"Worker thread timed out after {worker_timeout_seconds}s",
                    )
                    story_times[slug] = (story_started_at, timed_out_at)
                    results.append((spec_str, _timeout_result))
                    _write_story_audit(
                        config, slug_to_context[slug][0], _timeout_result, sprint_id=_sprint_id
                    )
                    if _state_writer is not None:
                        _state_writer.update(slug, status="failed", phase="ESCALATE")
                    dag.mark_skipped(slug)
                    specs_failed += 1
                active.clear()
                stopped_reason = stopped_reason or f"Worker timeout (>{worker_timeout_seconds}s)"
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
                    failed_at = datetime.datetime.now(datetime.timezone.utc)
                    story_started_at = story_times.get(slug, (failed_at, failed_at))[0]
                    _exc_state = CoordinatorState(
                        phase=Phase.ESCALATE,
                        started_at=story_started_at.isoformat(),
                        workspace_path=(
                            config.project_root / config.workspace.path_pattern.format(slug=slug)
                        ),
                        log_dir=_make_story_log_dir(config, slug, resolved.name),
                        error=f"Worker exception: {exc}",
                        error_type=type(exc).__name__,
                    )
                    _exc_result = CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=_exc_state,
                        message=f"Worker thread raised {type(exc).__name__}: {exc}",
                    )
                    story_times[slug] = (story_started_at, failed_at)
                    results.append((spec_str, _exc_result))
                    _write_story_audit(
                        config, slug_to_context[slug][0], _exc_result, sprint_id=_sprint_id
                    )
                    if _state_writer is not None:
                        _state_writer.update(slug, status="failed", phase="ESCALATE")
                    dag.mark_skipped(slug)
                    specs_failed += 1
                    continue
                del active[slug]
                story_times[slug] = (t0, t1)

                with cost_lock:
                    accumulated_cost += result.state.total_cost

                spec_str = slug_to_spec[slug]
                results.append((spec_str, result))

                spec_cost = result.state.total_cost
                icon = "✓" if result.success else "✗"
                dur = _fmt_duration(elapsed)
                _log(f"{icon} {slug}   ${spec_cost:.2f}  {dur}")

                if _state_writer is not None:
                    _done_status = (
                        "done"
                        if (result.success or result.state.preflight_verdict == "ALREADY_DONE")
                        else "failed"
                    )
                    if (
                        task.story_path is None
                        and task.github_issue is not None
                        and result.phase == Phase.PREFLIGHT
                    ):
                        _done_status = "waiting"
                    _state_writer.update(
                        slug,
                        status=_done_status,
                        phase=result.phase.name,
                        cost_usd=result.state.total_cost,
                    )

                ds, df, dsk = _classify_and_record(task, result, dag, merged_slugs)
                specs_succeeded += ds
                specs_failed += df
                specs_skipped += dsk

                # Dependent stories in parallel mode need scheduler-side local merge
                # even when on_approve is "none" and auto_merge is False.
                if (
                    result.success
                    and result.landing_status is None
                    and max_parallel > 1
                    and slug in dependent_slugs
                ):
                    result.landing_status = "pending_integration"
                    result.merge = {**(result.merge or {}), "action": "merge", "pending": True}

                # The scheduler thread is the sole owner of DAG/landing state.
                # Workers set landing_status="pending_integration" and return;
                # _attempt_integration is the sole merge site for all sprint execution.
                if result.success and result.landing_status == "pending_integration":
                    integrated = _attempt_integration(slug, task, result)
                    if not integrated:
                        pending_integration[slug] = (task, result)
                    elif result.landing_status == "failed":
                        # Optimistic classify counted this as succeeded; landing failed.
                        specs_succeeded -= ds
                        specs_failed += 1
                    changed = True
                    while changed:
                        changed = False
                        for pending_slug, (pending_task, pending_result) in list(
                            pending_integration.items()
                        ):
                            if _attempt_integration(pending_slug, pending_task, pending_result):
                                del pending_integration[pending_slug]
                                if pending_result.landing_status == "failed":
                                    # Correct the optimistic classify for this pending story
                                    specs_succeeded -= 1
                                    specs_failed += 1
                                changed = True
                else:
                    _write_story_audit(config, task, result, sprint_id=_sprint_id)

                # Fire StorySource lifecycle callbacks
                ctx = slug_to_context.get(slug)
                if ctx:
                    _ctx_task, source, _ctx_ref = ctx
                    if result.success:
                        try:
                            source.on_complete(task, result, config)
                        except Exception as exc:
                            _log(f"WARN on_complete callback failed for {slug}: {exc}")
                    elif result.phase == Phase.ESCALATE:
                        try:
                            source.on_escalate(task, result.state, config)
                        except Exception as exc:
                            _log(f"WARN on_escalate callback failed for {slug}: {exc}")

                _print_worker_status(active, worker_phases, dag, total)

            # ── Overlap detection: check plan gates ────────────────────
            if use_plan_gates:
                _release_plan_gates(plan_done, file_footprints, plan_gates, active, phase_lock)

    if queued_prs:
        for slug, (task, result, pr_url) in list(queued_prs.items()):
            poll_result = _poll_queued_pr(
                pr_url,
                config.project_root,
                config.workspace.merge_wait_timeout_seconds,
            )
            if poll_result["status"] == "merged":
                merged_slugs.add(slug)
                dag.mark_complete(slug)
                result.landing_status = "landed"
            else:
                specs_succeeded -= 1
                specs_failed += 1
                result.landing_status = "failed"
                result.success = False
                if poll_result["status"] == "timeout":
                    result.state.error = (
                        f"Queued PR timed out after "
                        f"{config.workspace.merge_wait_timeout_seconds}s: {pr_url}"
                    )
                else:
                    result.state.error = f"Queued PR {poll_result['status']}: {pr_url}"
                _log(f"✗ {slug}: queued PR {poll_result['status']} during sprint wrap-up")
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            del queued_prs[slug]

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    set_worker_slug("")
    duration = (finished_at - started_at).total_seconds()

    final_cost = accumulated_cost + prior_cost
    sprint_result = SprintResult(
        name=resolved.name,
        specs_total=total,
        specs_succeeded=specs_succeeded,
        specs_failed=specs_failed,
        specs_skipped=specs_skipped,
        total_cost_usd=final_cost,
        budget_usd=resolved.budget_usd,
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
                f"TheForge: {resolved.name}",
                f"✓ {specs_succeeded} passed, ✗ {specs_failed} failed",
            )
        if config.notifications.ntfy is not None:
            _ntfy_title = f'TheForge: sprint done \u2014 "{resolved.name}"'
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

            _sc_title = f'TheForge sprint complete \u2014 "{resolved.name}"'
            _sc_body_lines = [
                f"{total} specs: {specs_succeeded} succeeded \u00b7 {specs_failed} failed",
                f"Total cost: ${final_cost:.2f}   Duration: {_fmt_duration(_sprint_elapsed)}",
            ]
            if stopped_reason:
                _sc_body_lines.append(f"Stopped: {stopped_reason}")
            send_notifications(config, _sc_title, "\n".join(_sc_body_lines))

    # Build slug map and canonical_refs for audit writers
    slug_map: dict[str, str] = {ctx[2]: slug for slug, ctx in slug_to_context.items()}
    canonical_refs = [ctx[2] for ctx in slug_to_context.values()]

    # Write sprint-audit.yaml (existing format; kept for backward compatibility)
    _write_sprint_audit(
        manifest=resolved,
        result=sprint_result,
        canonical_refs=canonical_refs,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        project_root=config.project_root,
        story_times=story_times,
        batch_assignments=batch_assignments,
        slug_map=slug_map,
        tasks_by_slug={slug: ctx[0] for slug, ctx in slug_to_context.items()},
        ci_break_slug=ci_halt_slug,
        sprint_id=_sprint_id,
        dropped_slugs=_dropped_slugs,
        skipped_issues=skipped_issues,
    )

    # Write sprint-summary.yaml to .forge/logs/<sprint-name>/
    if _sprint_log_dir is not None:
        _write_sprint_summary(
            manifest=resolved,
            result=sprint_result,
            canonical_refs=canonical_refs,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
            sprint_log_dir=_sprint_log_dir,
            story_times=story_times,
            batch_assignments=batch_assignments,
            slug_map=slug_map,
            run_id=run_id,
            tasks_by_slug={slug: ctx[0] for slug, ctx in slug_to_context.items()},
            ci_break_slug=ci_halt_slug,
            sprint_id=_sprint_id,
            project_root=config.project_root,
            dropped_slugs=_dropped_slugs,
            skipped_issues=skipped_issues,
            triage_actions_by_ref={
                canonical_ref: triage.action for canonical_ref, triage in triages.items()
            },
        )

    if _state_writer is not None:
        _state_writer.remove()

    # ── POST_SPRINT hook ──────────────────────────────────────────────
    if config.hooks and config.hooks.post_sprint:
        from ..coordinator.hooks import build_post_sprint_payload
        from ..coordinator.hooks import run_hook as _run_hook

        _stories = []
        for spec_str, res in results:
            # Derive slug: use workspace_path leaf (set during WORKSPACE phase) or slug_map
            _ws = res.state.workspace_path
            if _ws is not None:
                _slug = _ws.name
            else:
                _slug = slug_map.get(spec_str, Path(spec_str).stem)
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
            sprint_name=resolved.name,
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
