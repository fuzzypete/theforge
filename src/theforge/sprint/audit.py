"""Sprint audit and summary YAML writers."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..log_util import _log_line
from .manifest import ResolvedSprint, SprintManifest, SprintResult

if TYPE_CHECKING:
    from ..config import ForgeConfig
    from ..coordinator.state import CoordinatorResult
    from ..task import TaskStory


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


# ── Sprint-level stable identity ──────────────────────────────────────────────


def _get_or_create_sprint_id(sprint_name: str, project_root: Path) -> str:
    """Return stable sprint_id for this logical sprint, creating it on first call.

    Stored at .forge/logs/<sprint-name>/.sprint_id so it persists across
    worker restarts, detach/attach, and --resume invocations.
    """
    sprint_log_dir = project_root / ".forge" / "logs" / sprint_name
    sprint_id_path = sprint_log_dir / ".sprint_id"
    try:
        sprint_log_dir.mkdir(parents=True, exist_ok=True)
        if sprint_id_path.exists():
            return sprint_id_path.read_text(encoding="utf-8").strip()
        from ..coordinator.util import _generate_run_id  # noqa: PLC0415

        new_id = _generate_run_id()
        sprint_id_path.write_text(new_id, encoding="utf-8")
        return new_id
    except OSError:
        from ..coordinator.util import _generate_run_id  # noqa: PLC0415

        return _generate_run_id()


def _load_accumulated_stories(sprint_id: str, project_root: Path) -> list[dict]:
    """Load per-story data from .forge/sprints/<sprint_id>/state.yaml.

    Returns the stories list, or [] if the file does not exist or cannot be read.
    Each entry includes a ``canonical_ref`` key used for cross-run matching.
    """
    state_path = project_root / ".forge" / "sprints" / sprint_id / "state.yaml"
    if not state_path.exists():
        return []
    try:
        with open(state_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("stories", [])
    except Exception:
        return []


def _save_accumulated_stories(
    sprint_id: str,
    sprint_name: str,
    project_root: Path,
    stories: list[dict],
) -> None:
    """Save story entries to .forge/sprints/<sprint_id>/state.yaml.

    Each entry should have a ``canonical_ref`` field for cross-run matching.
    Writes atomically via a temp file.
    """
    state_dir = project_root / ".forge" / "sprints" / sprint_id
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "state.yaml"
        tmp_path = state_path.with_suffix(".tmp")
        data: dict = {
            "sprint_id": sprint_id,
            "sprint_name": sprint_name,
            "stories": stories,
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(state_path)
    except Exception:
        pass


def _write_sprint_audit(
    manifest: SprintManifest | ResolvedSprint,
    result: SprintResult,
    canonical_refs: list[str],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    project_root: Path,
    story_times: "dict[str, tuple[datetime.datetime, datetime.datetime]] | None" = None,
    batch_assignments: "dict[str, int] | None" = None,
    slug_map: "dict[str, str] | None" = None,
    tasks_by_slug: "dict[str, TaskStory] | None" = None,
    ci_break_slug: str | None = None,
    sprint_id: str | None = None,
) -> None:
    """Write sprint-audit.yaml to the project root."""
    story_times = story_times or {}
    batch_assignments = batch_assignments or {}
    slug_map = slug_map or {}
    tasks_by_slug = tasks_by_slug or {}

    # Build per-spec entries
    spec_entries = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    for canonical_ref in canonical_refs:
        display_key = (
            f"Issue #{canonical_ref.split(':')[1]}"
            if canonical_ref.startswith("issue:")
            else canonical_ref
        )
        slug = slug_map.get(canonical_ref, Path(canonical_ref).stem)
        task = tasks_by_slug.get(slug)
        if canonical_ref in results_by_spec:
            res = results_by_spec[canonical_ref]
            preflight = (
                "cached"
                if getattr(res.state, "preflight_cached", False)
                else (res.state.preflight_verdict or "PROCEED")
            )
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

            dev_used = len(getattr(res.state, "dev_iteration_telemetry", []))
            review_used = len(getattr(res.state, "review_iteration_telemetry", []))
            dev_max = (
                getattr(
                    getattr(res.state, "dev_iteration_telemetry", [None])[0],
                    "max_iterations",
                    None,
                )
                if getattr(res.state, "dev_iteration_telemetry", [])
                else None
            )
            review_max = (
                getattr(
                    getattr(res.state, "review_iteration_telemetry", [None])[0],
                    "max_iterations",
                    None,
                )
                if getattr(res.state, "review_iteration_telemetry", [])
                else None
            )
            entry: dict = {
                "path": display_key,
                "outcome": outcome,
                "cost_usd": round(res.state.total_cost, 4),
                "preflight": preflight,
                "preflight_original_verdict": getattr(
                    res.state, "preflight_cached_original_verdict", None
                ),
                "preflight_source_run_id": getattr(
                    res.state, "preflight_cached_from_run_id", None
                ),
                "error": res.state.error,
                "error_type": res.state.error_type,
                "merge": res.merge is not None and res.merge.get("merged", False),
                "iteration_usage": {
                    "dev": {
                        "used": dev_used,
                        "max": dev_max,
                        "hit_limit": (dev_used >= dev_max)
                        if dev_max is not None and dev_used > 0
                        else False,
                        "early_finish": (0 < dev_used < dev_max) if dev_max is not None else False,
                    },
                    "review": {
                        "used": review_used,
                        "max": review_max,
                        "hit_limit": (review_used >= review_max)
                        if review_max is not None and review_used > 0
                        else False,
                        "early_finish": (0 < review_used < review_max)
                        if review_max is not None
                        else False,
                    },
                },
                "reviews": reviews_summary,
                "depends_on": task.depends_on if task else [],
                "inferred_dependencies": {
                    "manifest": [
                        dep
                        for dep in (task.depends_on if task else [])
                        if dep not in (task.inferred_dependencies if task else [])
                    ],
                    "github_blockers": task.inferred_dependencies if task else [],
                },
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
                "path": display_key,
                "outcome": "SKIPPED",
                "cost_usd": 0.0,
                "preflight": None,
                "error": None,
                "error_type": None,
                "merge": False,
                "reviews": [],
                "depends_on": task.depends_on if task else [],
                "inferred_dependencies": {
                    "manifest": [
                        dep
                        for dep in (task.depends_on if task else [])
                        if dep not in (task.inferred_dependencies if task else [])
                    ],
                    "github_blockers": task.inferred_dependencies if task else [],
                },
                "started_at": None,
                "finished_at": None,
                "batch": batch_assignments.get(slug, 0),
            }
        spec_entries.append(entry)

    usage_distribution = []
    for spec_str, res in result.results:
        dev_used = len(getattr(res.state, "dev_iteration_telemetry", []))
        review_used = len(getattr(res.state, "review_iteration_telemetry", []))
        dev_max = (
            getattr(
                getattr(res.state, "dev_iteration_telemetry", [None])[0], "max_iterations", None
            )
            if getattr(res.state, "dev_iteration_telemetry", [])
            else None
        )
        review_max = (
            getattr(
                getattr(res.state, "review_iteration_telemetry", [None])[0], "max_iterations", None
            )
            if getattr(res.state, "review_iteration_telemetry", [])
            else None
        )
        usage_distribution.append(
            {
                "spec": spec_str,
                "slug": slug_map.get(spec_str, Path(spec_str).stem),
                "dev": {"used": dev_used, "max": dev_max},
                "review": {"used": review_used, "max": review_max},
            }
        )

    audit = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "max_parallel": manifest.max_parallel,
            "sprint_id": sprint_id,
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
            "ci_break_slug": ci_break_slug,
        },
        "specs": spec_entries,
        "iteration_usage_distribution": usage_distribution,
    }

    audits_dir = project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "sprint-audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Append to history log (JSONL, never overwritten).
    try:
        with open(audits_dir / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(audit, default=str) + "\n")
    except OSError:
        pass
    _log(f"Audit written: {audit_path}")


def _write_sprint_summary(
    manifest: SprintManifest | ResolvedSprint,
    result: SprintResult,
    canonical_refs: list[str],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    sprint_log_dir: Path,
    story_times: "dict[str, tuple[datetime.datetime, datetime.datetime]] | None" = None,
    batch_assignments: "dict[str, int] | None" = None,
    slug_map: "dict[str, str] | None" = None,
    run_id: str | None = None,
    tasks_by_slug: "dict[str, TaskStory] | None" = None,
    ci_break_slug: str | None = None,
    sprint_id: str | None = None,
    project_root: Path | None = None,
) -> None:
    """Write sprint-summary.yaml to <project_root>/.forge/logs/<sprint-name>/.

    When sprint_id and project_root are provided, prior story entries from
    .forge/sprints/<sprint_id>/state.yaml are merged in for stories that did
    not run in this invocation (e.g., completed under an earlier run_id).
    This ensures the summary reflects the full logical sprint across all
    worker-process boundaries.
    """
    story_times = story_times or {}
    batch_assignments = batch_assignments or {}
    slug_map = slug_map or {}
    tasks_by_slug = tasks_by_slug or {}

    # Load prior accumulated story entries from the sprint-level state file.
    # Keyed by canonical_ref so we can substitute them for stories not in
    # this invocation's results (e.g., stories completed under an earlier run_id).
    prior_by_ref: dict[str, dict] = {}
    if sprint_id and project_root:
        prior_stories = _load_accumulated_stories(sprint_id, project_root)
        prior_by_ref = {s["canonical_ref"]: s for s in prior_stories if "canonical_ref" in s}

    spec_entries = []
    # Tracks story entries with canonical_ref for saving to accumulated state.
    accumulated_for_state: list[dict] = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    for canonical_ref in canonical_refs:
        display_key = (
            f"Issue #{canonical_ref.split(':')[1]}"
            if canonical_ref.startswith("issue:")
            else canonical_ref
        )
        slug = slug_map.get(canonical_ref, Path(canonical_ref).stem)
        if canonical_ref in results_by_spec:
            res = results_by_spec[canonical_ref]
            preflight = (
                "cached"
                if getattr(res.state, "preflight_cached", False)
                else (res.state.preflight_verdict or "PROCEED")
            )
            outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else res.phase.name
            last_verdict = ""
            if res.state.review_results:
                last_verdict = res.state.review_results[-1].verdict
            elif res.success:
                last_verdict = "APPROVE"
            dev_used = len(getattr(res.state, "dev_iteration_telemetry", []))
            review_used = len(getattr(res.state, "review_iteration_telemetry", []))
            dev_max = (
                getattr(
                    getattr(res.state, "dev_iteration_telemetry", [None])[0],
                    "max_iterations",
                    None,
                )
                if getattr(res.state, "dev_iteration_telemetry", [])
                else None
            )
            review_max = (
                getattr(
                    getattr(res.state, "review_iteration_telemetry", [None])[0],
                    "max_iterations",
                    None,
                )
                if getattr(res.state, "review_iteration_telemetry", [])
                else None
            )
            entry: dict = {
                "path": display_key,
                "slug": slug,
                "outcome": outcome,
                "verdict": last_verdict or None,
                "cost_usd": round(res.state.total_cost, 4),
                "preflight": preflight,
                "preflight_original_verdict": getattr(
                    res.state, "preflight_cached_original_verdict", None
                ),
                "preflight_source_run_id": getattr(
                    res.state, "preflight_cached_from_run_id", None
                ),
                "error": res.state.error,
                "error_type": res.state.error_type,
                "merge": res.merge is not None and res.merge.get("merged", False),
                "iteration_usage": {
                    "dev": {
                        "used": dev_used,
                        "max": dev_max,
                        "hit_limit": (dev_used >= dev_max)
                        if dev_max is not None and dev_used > 0
                        else False,
                        "early_finish": (0 < dev_used < dev_max) if dev_max is not None else False,
                    },
                    "review": {
                        "used": review_used,
                        "max": review_max,
                        "hit_limit": (review_used >= review_max)
                        if review_max is not None and review_used > 0
                        else False,
                        "early_finish": (0 < review_used < review_max)
                        if review_max is not None
                        else False,
                    },
                },
            }
            if slug in story_times:
                entry["started_at"] = story_times[slug][0].strftime("%Y-%m-%dT%H:%M:%SZ")
                entry["finished_at"] = story_times[slug][1].strftime("%Y-%m-%dT%H:%M:%SZ")
            entry["batch"] = batch_assignments.get(slug, 0)
            entry["depends_on"] = list(getattr(tasks_by_slug.get(slug), "depends_on", None) or [])
            spec_entries.append(entry)
            accumulated_for_state.append({"canonical_ref": canonical_ref, **entry})
        elif canonical_ref in prior_by_ref:
            # Story ran under an earlier run_id — use its accumulated data instead
            # of emitting a SKIPPED entry (which would hide a completed story).
            prior = prior_by_ref[canonical_ref]
            entry = {k: v for k, v in prior.items() if k != "canonical_ref"}
            spec_entries.append(entry)
            accumulated_for_state.append(prior)
        else:
            entry = {
                "path": display_key,
                "slug": slug,
                "outcome": "SKIPPED",
                "verdict": None,
                "cost_usd": 0.0,
                "preflight": None,
                "error": None,
                "error_type": None,
                "merge": False,
                "batch": batch_assignments.get(slug, 0),
                "depends_on": list(getattr(tasks_by_slug.get(slug), "depends_on", None) or []),
            }
            spec_entries.append(entry)

    # Persist accumulated state so future runs can find stories from this invocation.
    if sprint_id and project_root:
        _save_accumulated_stories(sprint_id, manifest.name, project_root, accumulated_for_state)

    usage_distribution = []
    for spec_str, res in result.results:
        dev_used = len(getattr(res.state, "dev_iteration_telemetry", []))
        review_used = len(getattr(res.state, "review_iteration_telemetry", []))
        dev_max = (
            getattr(
                getattr(res.state, "dev_iteration_telemetry", [None])[0], "max_iterations", None
            )
            if getattr(res.state, "dev_iteration_telemetry", [])
            else None
        )
        review_max = (
            getattr(
                getattr(res.state, "review_iteration_telemetry", [None])[0], "max_iterations", None
            )
            if getattr(res.state, "review_iteration_telemetry", [])
            else None
        )
        usage_distribution.append(
            {
                "spec": spec_str,
                "slug": slug_map.get(spec_str, Path(spec_str).stem),
                "dev": {"used": dev_used, "max": dev_max},
                "review": {"used": review_used, "max": review_max},
            }
        )

    # Recompute all aggregate metrics from spec_entries so prior-run stories
    # contributed by the accumulated state are included in the totals.
    effective_specs_total = len(spec_entries)
    effective_cost_usd = round(sum(e.get("cost_usd", 0.0) for e in spec_entries), 4)
    effective_succeeded = sum(
        1 for e in spec_entries if e.get("outcome") in ("DONE", "ALREADY_DONE")
    )
    effective_failed = sum(
        1
        for e in spec_entries
        if e.get("outcome") not in ("DONE", "ALREADY_DONE", "SKIPPED", None)
    )
    effective_skipped = sum(1 for e in spec_entries if e.get("outcome") in ("SKIPPED", None))

    summary = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "max_parallel": manifest.max_parallel,
            "run_id": run_id,
            "sprint_id": sprint_id,
            "run_log": f"run-{run_id}.log" if run_id else None,
            "total_cost_usd": effective_cost_usd,
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration, 1),
            "specs_total": effective_specs_total,
            "specs_succeeded": effective_succeeded,
            "specs_failed": effective_failed,
            "specs_skipped": effective_skipped,
            "stopped_reason": result.stopped_reason,
            "ci_break_slug": ci_break_slug,
        },
        "stories": spec_entries,
        "iteration_usage_distribution": usage_distribution,
    }

    try:
        sprint_log_dir.mkdir(parents=True, exist_ok=True)
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        with open(summary_path, "w", encoding="utf-8") as f:
            yaml.dump(summary, f, default_flow_style=False, sort_keys=False)
        _log(f"Sprint summary written: {summary_path}")
    except Exception as exc:
        _log(f"Warning: sprint summary write failed: {exc}")


def _write_story_audit(
    config: "ForgeConfig",
    task: "TaskStory",
    result: "CoordinatorResult",
    sprint_id: str | None = None,
) -> None:
    """Write per-story audit.yaml to the worktree and the durable log directory.

    Best-effort: silently ignores missing workspace or log dir.
    """
    from ..artifacts import AUDIT_PATH, ensure_parent_dir  # noqa: PLC0415
    from ..coordinator import audit as coordinator_audit  # noqa: PLC0415

    try:
        audit_data = coordinator_audit.generate_audit_log(config, task, result)
    except Exception as exc:
        _log(f"Warning: failed to generate story audit log for {task.slug}: {exc}")
        return

    if sprint_id is not None:
        audit_data["sprint_id"] = sprint_id

    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    if workspace_path.exists() and not (
        result.state.workspace_path is None and result.state.preflight_verdict == "ALREADY_DONE"
    ):
        audit_path = workspace_path / AUDIT_PATH
        ensure_parent_dir(audit_path)
        with open(audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        _log(f"Per-story audit written: {audit_path}")

    audits_dir = config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    try:
        with open(audits_dir / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(audit_data, default=str) + "\n")
    except OSError:
        pass

    if result.state.log_dir is not None:
        try:
            _story_audit_path = result.state.log_dir / "audit.yaml"
            _story_audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_story_audit_path, "w", encoding="utf-8") as f:
                yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass  # best-effort
