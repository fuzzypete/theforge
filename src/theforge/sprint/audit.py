"""Sprint audit and summary YAML writers."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..log_util import _log_line
from .manifest import ResolvedSprint, SprintManifest, SprintResult


def persist_accumulated_story_state(
    sprint_id: str | None,
    sprint_name: str,
    project_root: Path | None,
    stories: list[dict],
) -> None:
    """Persist sprint-level accumulated story state when identity and root are known."""
    if sprint_id and project_root:
        _save_accumulated_stories(sprint_id, sprint_name, project_root, stories)


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


def _load_story_summary_entry_from_audit(
    sprint_log_dir: Path,
    canonical_ref: str,
    slug: str,
) -> dict | None:
    """Return a sprint-summary story entry derived from per-story audit.yaml."""
    audit_path = sprint_log_dir / slug / "audit.yaml"
    if not audit_path.exists():
        return None

    try:
        with open(audit_path, encoding="utf-8") as f:
            audit_data = yaml.safe_load(f) or {}
    except Exception:
        return None

    if not isinstance(audit_data, dict):
        return None

    outcome_block = audit_data.get("outcome")
    timing_block = audit_data.get("timing")
    preflight_block = audit_data.get("preflight")
    cost_block = audit_data.get("cost")
    iteration_block = audit_data.get("iterations")

    if not isinstance(outcome_block, dict):
        return None

    final_phase = outcome_block.get("final_phase")
    if not isinstance(final_phase, str) or not final_phase:
        return None
    if isinstance(preflight_block, dict) and preflight_block.get("verdict") == "ALREADY_DONE":
        final_phase = "ALREADY_DONE"

    display_key = (
        f"Issue #{canonical_ref.split(':')[1]}"
        if canonical_ref.startswith("issue:")
        else canonical_ref
    )

    reviews = audit_data.get("reviews")
    verdict = None
    if isinstance(reviews, list) and reviews:
        last_review = reviews[-1]
        if isinstance(last_review, dict):
            raw_verdict = last_review.get("verdict")
            if isinstance(raw_verdict, str) and raw_verdict:
                verdict = raw_verdict
    if verdict is None and outcome_block.get("success") is True:
        verdict = "APPROVE"

    usage_summary = (
        iteration_block.get("usage_summary") if isinstance(iteration_block, dict) else {}
    )
    if not isinstance(usage_summary, dict):
        usage_summary = {}

    dev_usage = usage_summary.get("dev") if isinstance(usage_summary.get("dev"), dict) else {}
    review_usage = (
        usage_summary.get("review") if isinstance(usage_summary.get("review"), dict) else {}
    )

    preflight_verdict = (
        preflight_block.get("verdict") if isinstance(preflight_block, dict) else None
    )

    return {
        "canonical_ref": canonical_ref,
        "path": display_key,
        "slug": slug,
        "outcome": final_phase,
        "verdict": verdict,
        "cost_usd": round(float(cost_block.get("total_usd", 0.0)), 4)
        if isinstance(cost_block, dict)
        else 0.0,
        "story_run_id": audit_data.get("run_id"),
        "preflight": preflight_verdict,
        "preflight_original_verdict": (
            preflight_block.get("original_verdict") if isinstance(preflight_block, dict) else None
        ),
        "preflight_source_run_id": (
            preflight_block.get("source_run_id") if isinstance(preflight_block, dict) else None
        ),
        "error": audit_data.get("error"),
        "error_type": audit_data.get("error_type") or outcome_block.get("error_type"),
        "outcome_code": (
            audit_data.get("error_type") or outcome_block.get("error_type") or final_phase.lower()
        ),
        "merge": bool((audit_data.get("merge") or {}).get("merged", False)),
        "iteration_usage": {
            "dev": {
                "used": dev_usage.get("used", 0),
                "max": dev_usage.get("max"),
                "hit_limit": bool(dev_usage.get("hit_limit", False)),
                "early_finish": bool(dev_usage.get("early_finish", False)),
            },
            "review": {
                "used": review_usage.get("used", 0),
                "max": review_usage.get("max"),
                "hit_limit": bool(review_usage.get("hit_limit", False)),
                "early_finish": bool(review_usage.get("early_finish", False)),
            },
        },
        "started_at": timing_block.get("started_at") if isinstance(timing_block, dict) else None,
        "finished_at": timing_block.get("finished_at") if isinstance(timing_block, dict) else None,
    }


def _parse_summary_timestamp(value: object) -> datetime.datetime | None:
    """Parse sprint-summary timestamps of the form 2026-04-25T12:05:00Z."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _story_outcome_rank(entry: dict) -> int:
    """Higher rank wins when timing information is unavailable or tied."""
    outcome = entry.get("outcome")
    if outcome == "DONE":
        return 5
    if outcome == "ALREADY_DONE":
        return 4
    if outcome in {"SKIPPED", "PRESERVED", "DROPPED"}:
        return 3
    if outcome == "ESCALATE":
        return 1
    return 2


def _select_historical_story_entry(prior: dict | None, audit_entry: dict | None) -> dict | None:
    """Choose the best historical story entry between accumulated state and audit.yaml."""
    if prior is None:
        return audit_entry
    if audit_entry is None:
        return prior

    prior_finished = _parse_summary_timestamp(prior.get("finished_at"))
    audit_finished = _parse_summary_timestamp(audit_entry.get("finished_at"))
    if (
        prior_finished is not None
        and audit_finished is not None
        and prior_finished != audit_finished
    ):
        return audit_entry if audit_finished > prior_finished else prior
    if prior_finished is not None and audit_finished is None:
        return prior
    if audit_finished is not None and prior_finished is None:
        return (
            audit_entry if _story_outcome_rank(audit_entry) > _story_outcome_rank(prior) else prior
        )

    if _story_outcome_rank(audit_entry) > _story_outcome_rank(prior):
        return audit_entry
    return prior


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
    dropped_slugs: "dict[str, str] | None" = None,
    skipped_issues: "list | None" = None,
) -> None:
    """Write sprint-audit.yaml to the project root."""
    story_times = story_times or {}
    batch_assignments = batch_assignments or {}
    slug_map = slug_map or {}
    tasks_by_slug = tasks_by_slug or {}
    dropped_slugs = dropped_slugs or {}
    skipped_issues = skipped_issues or []

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
                "outcome_code": res.state.error_type or outcome.lower(),
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
                "dependency_warnings": task.dependency_warnings if task else [],
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
            # Dropped by launch guard (active-worktree collision, lock held,
            # or preserved-escalated) takes precedence over the generic
            # SKIPPED path — operators need to see drop reasons explicitly.
            drop_reason = dropped_slugs.get(slug)
            if drop_reason == "preserved-escalated":
                drop_outcome = "PRESERVED"
            elif drop_reason is not None:
                drop_outcome = "DROPPED"
            else:
                drop_outcome = "SKIPPED"
            entry = {
                "path": display_key,
                "outcome": drop_outcome,
                "cost_usd": 0.0,
                "preflight": None,
                "error": drop_reason,
                "error_type": "dropped" if drop_reason else None,
                "merge": False,
                "reviews": [],
                "depends_on": task.depends_on if task else [],
                "dependency_warnings": task.dependency_warnings if task else [],
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
            if drop_reason:
                entry["drop_reason"] = drop_reason
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
        "baseline_check": (
            getattr(manifest, "baseline_gate", None)
            if isinstance(getattr(manifest, "baseline_gate", None), dict)
            else None
        ),
        "specs": spec_entries,
        "skipped": [s.as_dict() if hasattr(s, "as_dict") else dict(s) for s in skipped_issues],
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
    dropped_slugs: "dict[str, str] | None" = None,
    skipped_issues: "list | None" = None,
    triage_actions_by_ref: "dict[str, str] | None" = None,
    current_story_entries_by_ref: "dict[str, dict] | None" = None,
    story_state: "object | None" = None,
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
    dropped_slugs = dropped_slugs or {}
    skipped_issues = skipped_issues or []
    triage_actions_by_ref = triage_actions_by_ref or {}
    current_story_entries_by_ref = current_story_entries_by_ref or {}

    # Load prior accumulated story entries from the sprint-level state file.
    # Keyed by canonical_ref so we can substitute them for stories not in
    # this invocation's results (e.g., stories completed under an earlier run_id).
    prior_by_ref: dict[str, dict] = {}
    prior_stories: list[dict] = []
    if sprint_id and project_root:
        prior_stories = _load_accumulated_stories(sprint_id, project_root)
        prior_by_ref = {s["canonical_ref"]: s for s in prior_stories if "canonical_ref" in s}

    spec_entries = []
    # Tracks story entries with canonical_ref for saving to accumulated state.
    accumulated_for_state: list[dict] = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    seen_refs: set[str] = set()
    for canonical_ref in canonical_refs:
        seen_refs.add(canonical_ref)
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
                "story_run_id": run_id,
                "preflight": preflight,
                "preflight_original_verdict": getattr(
                    res.state, "preflight_cached_original_verdict", None
                ),
                "preflight_source_run_id": getattr(
                    res.state, "preflight_cached_from_run_id", None
                ),
                "error": res.state.error,
                "error_type": res.state.error_type,
                "outcome_code": res.state.error_type or outcome.lower(),
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
        elif canonical_ref in current_story_entries_by_ref:
            current_entry = dict(current_story_entries_by_ref[canonical_ref])
            spec_entries.append(current_entry)
            accumulated_for_state.append({"canonical_ref": canonical_ref, **current_entry})
        elif canonical_ref in prior_by_ref:
            # Story ran under an earlier run_id — use its accumulated data instead
            # of emitting a SKIPPED entry (which would hide a completed story).
            historical_entry = _select_historical_story_entry(
                prior_by_ref[canonical_ref],
                _load_story_summary_entry_from_audit(sprint_log_dir, canonical_ref, slug),
            )
            if historical_entry is None:
                historical_entry = prior_by_ref[canonical_ref]
            entry = {k: v for k, v in historical_entry.items() if k != "canonical_ref"}
            spec_entries.append(entry)
            accumulated_for_state.append({"canonical_ref": canonical_ref, **entry})
        else:
            drop_reason = dropped_slugs.get(slug)
            triage_action = triage_actions_by_ref.get(canonical_ref)
            if drop_reason == "preserved-escalated":
                drop_outcome = "PRESERVED"
            elif drop_reason is not None:
                drop_outcome = "DROPPED"
            elif triage_action == "skip_merged":
                drop_outcome = "ALREADY_DONE"
            else:
                drop_outcome = "SKIPPED"
            entry = {
                "path": display_key,
                "slug": slug,
                "outcome": drop_outcome,
                "verdict": None,
                "cost_usd": 0.0,
                "preflight": None,
                "error": drop_reason,
                "error_type": "dropped" if drop_reason else None,
                "merge": False,
                "batch": batch_assignments.get(slug, 0),
                "depends_on": list(getattr(tasks_by_slug.get(slug), "depends_on", None) or []),
            }
            if drop_reason:
                entry["drop_reason"] = drop_reason
            spec_entries.append(entry)

    for canonical_ref, prior in prior_by_ref.items():
        if canonical_ref in seen_refs:
            continue
        entry = {k: v for k, v in prior.items() if k != "canonical_ref"}
        spec_entries.append(entry)
        accumulated_for_state.append(prior)

    # Persist accumulated state so future runs can find stories from this invocation.
    # Preserve prior entries that were not part of this invocation's canonical_refs
    # so re-exec/resume summaries retain the full logical sprint history.
    persist_accumulated_story_state(
        sprint_id,
        manifest.name,
        project_root,
        accumulated_for_state,
    )

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

    # Project totals from the canonical SprintStoryState when supplied — by
    # construction these counts equal the banner counts in the same run. If
    # no canonical state was passed (legacy callers), fall back to recomputing
    # from spec_entries; the canonical path is the single source of truth.
    if story_state is not None and hasattr(story_state, "counts"):
        # First, propagate canonical outcomes to per-story rows so that
        # terminal-to-terminal corrections (e.g., DONE→FAILED for a queued PR
        # that did not land) appear in the summary rows AND aggregate counts.
        # The summary stories list and the summary totals must come from the
        # same source — this loop ensures both project from story_state.
        for entry in spec_entries:
            slug = entry.get("slug")
            if not slug:
                continue
            canonical_entry = story_state.get(slug)
            if canonical_entry is None:
                continue
            entry["outcome"] = canonical_entry.outcome.name
            outcome_lower = canonical_entry.outcome.name.lower()
            entry["outcome_code"] = entry.get("error_type") or outcome_lower
        canonical_counts = story_state.counts()
        effective_specs_total = canonical_counts["total"]
        effective_succeeded = canonical_counts["succeeded"]
        effective_failed = canonical_counts["failed"]
        effective_skipped = canonical_counts["skipped"]
        effective_cost_usd = round(
            sum(getattr(e, "cost_usd", 0.0) for e in story_state.stories()), 4
        )
        # Inject any shape-gate-skipped stories (and other canonical-only
        # entries that aren't in canonical_refs) so the summary surfaces them.
        canonical_slugs_in_entries = {e.get("slug") for e in spec_entries if e.get("slug")}
        for entry in story_state.stories():
            if entry.slug in canonical_slugs_in_entries:
                continue
            outcome_name = entry.outcome.name
            spec_entries.append(
                {
                    "path": entry.path,
                    "slug": entry.slug,
                    "outcome": outcome_name,
                    "verdict": None,
                    "cost_usd": entry.cost_usd,
                    "preflight": None,
                    "error": entry.reason,
                    "error_type": None,
                    "merge": False,
                    "batch": 0,
                    "depends_on": list(entry.depends_on),
                    "drop_reason": entry.reason,
                }
            )
    else:
        effective_specs_total = len(spec_entries)
        effective_cost_usd = round(sum(e.get("cost_usd", 0.0) for e in spec_entries), 4)
        effective_succeeded = sum(1 for e in spec_entries if e.get("outcome") == "DONE")
        effective_failed = sum(
            1
            for e in spec_entries
            if e.get("outcome") not in ("DONE", "ALREADY_DONE", "SKIPPED", "PRESERVED", None)
        )
        effective_skipped = sum(
            1
            for e in spec_entries
            if e.get("outcome") in ("ALREADY_DONE", "SKIPPED", "PRESERVED", None)
        )

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
        "skipped": [s.as_dict() if hasattr(s, "as_dict") else dict(s) for s in skipped_issues],
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
    """Write per-story audit.yaml to the durable log directory and preserve ESCALATE worktrees.

    Best-effort: silently ignores missing workspace or log dir.
    """
    from ..artifacts import AUDIT_PATH, ESCALATED_MARKER_PATH, ensure_parent_dir  # noqa: PLC0415
    from ..coordinator import audit as coordinator_audit  # noqa: PLC0415

    try:
        audit_data = coordinator_audit.generate_audit_log(config, task, result)
    except Exception as exc:
        _log(f"Warning: failed to generate story audit log for {task.slug}: {exc}")
        return

    if sprint_id is not None:
        audit_data["sprint_id"] = sprint_id
        sprint_name = result.state.log_dir.parent.name if result.state.log_dir else None
        if not sprint_name:
            sprint_name = audit_data.get("sprint_name")
        if not sprint_name:
            sprint_name = "Parallel Sprint"
        audit_data["sprint_name"] = sprint_name

    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    final_phase = result.phase.name
    if workspace_path.exists() and final_phase == "ESCALATE":
        audit_path = workspace_path / AUDIT_PATH
        ensure_parent_dir(audit_path)
        with open(audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        marker_path = workspace_path / ESCALATED_MARKER_PATH
        ensure_parent_dir(marker_path)
        timestamp = audit_data.get("ended_at") or audit_data.get("started_at") or ""
        marker_path.write_text(
            f"slug: {task.slug}\nfinal_phase: {final_phase}\ntimestamp: {timestamp}\n",
            encoding="utf-8",
        )
        _log(f"Per-story audit written: {audit_path}")

    audits_dir = config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    try:
        with open(audits_dir / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(audit_data, default=str) + "\n")
    except OSError:
        pass

    log_dir = result.state.log_dir
    if log_dir is None:
        sprint_name = audit_data.get("sprint_name")
        if not isinstance(sprint_name, str) or not sprint_name:
            sprint_name = None
        if isinstance(sprint_name, str) and sprint_name:
            log_dir = config.project_root / ".forge" / "logs" / sprint_name / task.slug
    if log_dir is not None:
        try:
            _story_audit_path = log_dir / "audit.yaml"
            _story_audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_story_audit_path, "w", encoding="utf-8") as f:
                yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass  # best-effort
