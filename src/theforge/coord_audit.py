"""Audit log generation for coordinator runs."""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path

from .config import ForgeConfig
from .coord_state import CoordinatorResult, CoordinatorState
from .task import TaskStory as TaskSpec  # noqa: F401


def _branch_has_unmerged_commits(project_root: Path, branch: str, base: str) -> bool:
    """Return True if branch exists and has commits ahead of base.

    Returns False on missing branch (non-zero exit), timeout, OSError, or
    non-integer output — all treated as "no unmerged commits".
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", f"{base}..{branch}", "--count"],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False
        count = int(result.stdout.decode("utf-8", errors="replace").strip())
        return count > 0
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False


def has_review_approve(
    project_root: Path,
    slug: str,
    base_branch: str = "main",
    branch: str | None = None,
) -> bool:
    """Return True if any prior run for slug produced a review APPROVE.

    Reads .forge/audits/history.jsonl line-by-line. Returns False on missing
    file, parse errors, or if no matching APPROVE record exists (safe default:
    assume no APPROVE so review is never skipped incorrectly).

    An APPROVE record is skipped if the feature branch still has unmerged
    commits ahead of base_branch — that indicates an abandoned run.

    Args:
        branch: The feature branch name (e.g. config.workspace.branch_pattern
            formatted with slug). If None, defaults to 'feat/<slug>'.
    """
    history_path = project_root / ".forge" / "audits" / "history.jsonl"
    if not history_path.exists():
        return False
    feature_branch = branch if branch is not None else f"feat/{slug}"
    # Cache the branch state check — it does not change mid-loop.
    branch_is_stale: bool | None = None
    try:
        with open(history_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                task_info = record.get("task", {})
                if task_info.get("slug") != slug:
                    continue
                for review in record.get("reviews", []):
                    if review.get("verdict") == "APPROVE":
                        if branch_is_stale is None:
                            branch_is_stale = _branch_has_unmerged_commits(
                                project_root, feature_branch, base_branch
                            )
                        if branch_is_stale:
                            continue  # stale APPROVE from abandoned run
                        return True
    except OSError:
        pass
    return False


def _build_phases_block(state: CoordinatorState, config: ForgeConfig) -> dict:
    """Build the phases + totals block for the audit log.

    Each phase entry is None when the phase did not run (e.g. preflight skipped).
    dev_durations and review_durations already exist on CoordinatorState and are
    populated by coordinator.py — no additional tracking needed for those two phases.
    """
    # ── preflight ─────────────────────────────────────────────────────────────
    preflight_block: dict | None = None
    if state.preflight_verdict is not None:
        preflight_block = {
            "cost_usd": round(state.total_preflight_cost, 6),
            "duration_s": round(state.preflight_duration_s, 2)
            if state.preflight_duration_s is not None
            else None,
            "outcome": state.preflight_verdict.lower() if state.preflight_verdict else None,
        }

    # ── plan ──────────────────────────────────────────────────────────────────
    plan_block: dict | None = None
    if state.plan_results:
        plan_block = {
            "cost_usd": round(state.total_plan_cost, 6),
            "duration_s": round(sum(state.plan_durations), 2) if state.plan_durations else None,
            "outcome": "success",
        }

    # ── plan_review ───────────────────────────────────────────────────────────
    plan_review_block: dict | None = None
    if state.plan_review_decision is not None:
        plan_review_block = {
            "cost_usd": round(state.total_plan_review_cost, 6),
            "duration_s": round(sum(state.plan_review_durations), 2)
            if state.plan_review_durations
            else None,
            "iterations": len(state.plan_review_results),
            "outcome": state.plan_review_decision,
        }

    # ── dev ───────────────────────────────────────────────────────────────────
    dev_block: dict | None = None
    if state.dev_results:
        dev_block = {
            "cost_usd": round(state.total_dev_cost, 6),
            "duration_s": round(sum(state.dev_durations), 2) if state.dev_durations else None,
            "iterations": len(state.dev_results),
            "outcome": "success",
        }

    # ── validate ──────────────────────────────────────────────────────────────
    validate_block: dict | None = None
    if state.gate_decisions:
        validate_block = {
            "cost_usd": 0.0,
            "duration_s": round(sum(state.validate_durations), 2)
            if state.validate_durations
            else None,
            "outcome": state.gate_decisions[-1].lower() if state.gate_decisions else None,
        }

    # ── review ────────────────────────────────────────────────────────────────
    review_block: dict | None = None
    if state.review_agent_results:
        # Build per_reviewer: non-synthesis agents, summing cost, cross-referencing
        # last_cycle_reviewer_results for verdict. Note: last_cycle_reviewer_results
        # only holds the final review cycle — reviewers absent from the last cycle
        # will have no verdict entry. This matches the spec's intent (final state).
        _last_verdicts: dict[str, str] = {
            name: rr.verdict for name, rr in state.last_cycle_reviewer_results
        }
        _reviewer_costs: dict[str, float] = {}
        for r in state.review_agent_results:
            if r.profile_name and r.profile_name != "synthesis":
                _reviewer_costs[r.profile_name] = _reviewer_costs.get(r.profile_name, 0.0) + (
                    r.cost_usd if r.cost_usd is not None else 0.0
                )
        per_reviewer = {
            name: {
                "cost": round(cost, 6),
                "verdict": _last_verdicts.get(name),
            }
            for name, cost in _reviewer_costs.items()
        }
        # Final verdict from most recent review cycle
        _final_verdict: str | None = None
        if state.review_results:
            _final_verdict = state.review_results[-1].verdict.lower()
        review_block = {
            "cost_usd": round(state.total_review_cost, 6),
            "duration_s": round(sum(state.review_durations), 2)
            if state.review_durations
            else None,
            "cycles": state.review_cycle,
            "outcome": _final_verdict,
            "per_reviewer": per_reviewer,
        }

    # ── totals ────────────────────────────────────────────────────────────────
    all_durations = [
        state.preflight_duration_s if state.preflight_duration_s is not None else 0.0,
        sum(state.plan_durations),
        sum(state.plan_review_durations),
        sum(state.dev_durations),
        sum(state.validate_durations),
        sum(state.review_durations),
    ]
    totals = {
        "cost_usd": round(state.total_cost, 6),
        "duration_s": round(sum(all_durations), 2),
        "dev_iterations": state.dev_iteration,
        "review_cycles": state.review_cycle,
    }

    return {
        "phases": {
            "preflight": preflight_block,
            "plan": plan_block,
            "plan_review": plan_review_block,
            "dev": dev_block,
            "validate": validate_block,
            "review": review_block,
        },
        "totals": totals,
    }


def generate_audit_log(config: ForgeConfig, task: TaskSpec, result: CoordinatorResult) -> dict:
    """Generate a structured audit log for the entire coordination run.

    This is the orchestrator's own handoff — a complete record of what happened.
    """
    state = result.state

    # Compute overall timing
    finished_at = datetime.datetime.now(datetime.timezone.utc)
    finished_at_str = finished_at.isoformat()
    duration_seconds: float | None = None
    if state.started_at:
        try:
            started = datetime.datetime.fromisoformat(state.started_at)
            duration_seconds = (finished_at - started).total_seconds()
        except ValueError:
            pass

    # Build per-agent invocation list for cost breakdown.
    # Durations are measured in the coordinator around each agent call.
    agents: list[dict] = []
    for i, r in enumerate(state.dev_results):
        dur = state.dev_durations[i] if i < len(state.dev_durations) else None
        entry: dict = {
            "role": "dev",
            "profile": r.profile_name or config.dev_profile.name,
            "cost_usd": r.cost_usd,
            "duration_seconds": dur,
        }
        if r.model_usage:
            entry["model_usage"] = [
                {
                    "model": u.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_tokens": u.cache_read_tokens,
                    "cache_creation_tokens": u.cache_creation_tokens,
                    "cost_usd": u.cost_usd,
                }
                for u in r.model_usage
            ]
        agents.append(entry)
    for i, r in enumerate(state.review_agent_results):
        dur = state.review_durations[i] if i < len(state.review_durations) else None
        role = "synthesis" if r.profile_name == "synthesis" else "review"
        entry = {
            "role": role,
            "profile": r.profile_name,
            "cost_usd": r.cost_usd,
            "duration_seconds": dur,
        }
        if r.model_usage:
            entry["model_usage"] = [
                {
                    "model": u.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_tokens": u.cache_read_tokens,
                    "cache_creation_tokens": u.cache_creation_tokens,
                    "cost_usd": u.cost_usd,
                }
                for u in r.model_usage
            ]
        agents.append(entry)

    # Build reviews list from cycle metadata (primary) joined with parsed results
    reviews = []
    for i, meta in enumerate(state.review_cycle_metadata):
        entry: dict = {
            "cycle": i + 1,
            "pool_models": meta.pool_models,
            "successful": meta.successful,
            "failed": meta.failed,
            "failed_detail": meta.failed_detail,
            "synthesized": meta.synthesized,
            "parse_retries": meta.parse_retries,
        }
        if i < len(state.review_results):
            r = state.review_results[i]
            findings_list = [
                {
                    "severity": f.severity,
                    "file": f.file,
                    "line": f.line,
                    "description": f.description,
                }
                for f in r.findings
            ]
            entry.update(
                {
                    "verdict": r.verdict,
                    "summary": r.summary,
                    "p1_count": sum(1 for f in r.findings if f.severity == "P1"),
                    "p2_count": sum(1 for f in r.findings if f.severity == "P2"),
                    "findings": findings_list,
                }
            )
        reviews.append(entry)

    return {
        "forge_version": "0.1.0",
        "task": {
            "name": task.name,
            "slug": task.slug,
            "story_path": str(task.story_path),
        },
        "outcome": {
            "success": result.success,
            "final_phase": result.phase.name,
            "message": result.message,
        },
        "timing": {
            "started_at": state.started_at,
            "finished_at": finished_at_str,
            "duration_seconds": duration_seconds,
        },
        "workspace": {
            "path": str(state.workspace_path) if state.workspace_path else None,
            "branch": state.branch_name,
        },
        "iterations": {
            "review_cycles": state.review_cycle,
            "dev_iterations": state.dev_iteration,
            "gate_decisions": state.gate_decisions,
        },
        "cost": {
            "total_usd": state.total_cost,
            "dev_usd": state.total_dev_cost,
            "review_usd": state.total_review_cost,
            "dev_invocations": len(state.dev_results),
            "review_invocations": len(state.review_agent_results),
            "agents": agents,
        },
        "preflight": (
            {
                "verdict": state.preflight_verdict,
                "reason": state.preflight_reason,
                "cost_usd": state.preflight_result.cost_usd if state.preflight_result else 0.0,
            }
            if state.preflight_verdict is not None
            else None
        ),
        "dev_handoffs": [
            {"iteration": i + 1, "handoff": snap}
            for i, snap in enumerate(state.dev_handoff_snapshots)
        ],
        "reviews": reviews,
        "human_review": (
            {
                "mode": state.human_review_mode or "interactive",
                "decision": state.human_review_decision,
                "feedback": state.human_review_feedback,
                "waited_seconds": (
                    round(state.human_review_waited_seconds, 1)
                    if state.human_review_waited_seconds is not None
                    else None
                ),
                "extra_cycles_granted": state.human_review_extra_cycles,
            }
            if state.human_review_decision is not None
            else None
        ),
        "plan_review": (
            {
                "reviewer": ("agent" if state.plan_review_mode == "agent" else "human"),
                "mode": (
                    state.plan_review_mode
                    if state.plan_review_mode == "agent"
                    else config.plan_review.mode
                ),
                "decision": state.plan_review_decision,
                "regenerated": state.plan_regen_count > 0,
                "waited_seconds": round(state.plan_review_waited_seconds or 0, 2),
                "findings": state.plan_agent_review_findings,
                "cost_usd": state.total_plan_review_cost,
            }
            if state.plan_review_decision is not None
            else None
        ),
        "plan_validation": (
            {"skipped": True, "findings": [], "finding_count": 0}
            if state.plan_structured is None
            else {
                "skipped": False,
                "findings": state.plan_validation_findings,
                "finding_count": len(state.plan_validation_findings),
            }
        ),
        "merge": result.merge,
        "escalation": (
            {
                "reason": state.escalate_reason,
                "reviewer_verdicts": (
                    {name: rr.verdict for name, rr in state.last_cycle_reviewer_results}
                    if state.last_cycle_reviewer_results
                    else {}
                ),
                "gate_result": state.gate_decisions[-1] if state.gate_decisions else None,
                "human_decision": state.escalate_decision,
                "waited_seconds": (
                    round(state.human_review_waited_seconds, 1)
                    if state.human_review_waited_seconds is not None
                    else None
                ),
            }
            if state.escalate_decision is not None
            else None
        ),
        "error": state.error,
        "story_validation": (
            {
                "verdict": state.story_validation_result.verdict,
                "cost_usd": state.story_validation_result.cost_usd,
                "duration_s": state.story_validation_result.duration_s,
                "findings": [
                    {
                        "category": f.category,
                        "description": f.description,
                        "split_suggestion": f.split_suggestion,
                    }
                    for f in state.story_validation_result.findings
                ],
            }
            if state.story_validation_result is not None
            else None
        ),
        "finding_registry": [
            {
                "finding_id": r.finding_id,
                "cycle_first_seen": r.cycle_first_seen,
                "cycle_last_seen": r.cycle_last_seen,
                "file": r.file,
                "line": r.line,
                "severity": r.severity,
                "description": r.description,
                "reporter": r.reporter,
                "disposition": r.disposition,
            }
            for r in state.finding_registry
        ],
        "non_blocking_p1s": [
            {
                "finding_id": r.finding_id,
                "cycle_first_seen": r.cycle_first_seen,
                "file": r.file,
                "line": r.line,
                "description": r.description,
                "reporter": r.reporter,
                "disposition": "net_new",
            }
            for r in state.finding_registry
            if r.severity == "P1" and r.disposition == "net_new"
        ],
        **_build_phases_block(state, config),
    }
