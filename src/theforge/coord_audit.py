"""Audit log generation for coordinator runs."""

from __future__ import annotations

import datetime

from .config import ForgeConfig
from .coord_state import CoordinatorResult
from .task import TaskSpec


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
            "spec_path": str(task.spec_path),
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
                "mode": config.plan_review.mode,
                "decision": state.plan_review_decision,
                "regenerated": state.plan_regenerated,
                "waited_seconds": round(state.plan_review_waited_seconds or 0, 2),
            }
            if state.plan_review_decision is not None
            else None
        ),
        "merge": result.merge,
        "error": state.error,
    }
