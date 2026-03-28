"""Sprint audit and summary YAML writers."""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .manifest import SprintManifest, SprintResult

if TYPE_CHECKING:
    from ..config import ForgeConfig
    from ..coordinator.engine import CoordinatorResult
    from ..task import TaskStory as TaskSpec


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


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
            slug = slug_map.get(spec_str, story_path.stem)
            entry = {
                "path": spec_str,
                "outcome": "SKIPPED",
                "cost_usd": 0.0,
                "preflight": None,
                "merge": False,
                "reviews": [],
                "started_at": None,
                "finished_at": None,
                "batch": batch_assignments.get(slug, 0),
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


def _write_story_audit(
    config: "ForgeConfig",
    task: "TaskSpec",
    result: "CoordinatorResult",
) -> None:
    """Write per-story audit.yaml to the worktree and the durable log directory.

    Best-effort: silently ignores missing workspace or log dir.
    """
    from ..artifacts import AUDIT_PATH, ensure_parent_dir  # noqa: PLC0415
    from ..coordinator.audit import generate_audit_log  # noqa: PLC0415

    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    if workspace_path.exists():
        audit_data = generate_audit_log(config, task, result)
        audit_path = workspace_path / AUDIT_PATH
        ensure_parent_dir(audit_path)
        with open(audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        _log(f"Per-story audit written: {audit_path}")

    if result.state.log_dir is not None:
        try:
            audit_data = generate_audit_log(config, task, result)
            _story_audit_path = result.state.log_dir / "audit.yaml"
            _story_audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_story_audit_path, "w", encoding="utf-8") as f:
                yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass  # best-effort
