"""Audit log generation for coordinator runs."""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.review import parse_plan_review_output
from theforge.task import TaskStory

from .audit_render import build_agent_entries, build_reviews
from .audit_substrate import CURRENT_RECORD_SCHEMA_VERSION as SCHEMA_VERSION
from .audit_substrate import MIGRATION_HELPERS
from .landing_record import build_landing_record
from .state import CoordinatorResult, CoordinatorState

# Per-run audit-record schema version and the reader-side migration registry
# are owned by audit_substrate (the reader). They are re-exported here so
# the writer module exposes a single, authoritative pair: bumping
# ``SCHEMA_VERSION`` requires a matching ``MIGRATION_HELPERS`` entry on the
# runtime path, not a writer-local copy. See ADR-0002 §"Schema versioning is
# load-bearing".
MAX_KNOWN_VERSION = max(MIGRATION_HELPERS.keys()) if MIGRATION_HELPERS else 0

__all_schema_exports__ = ("SCHEMA_VERSION", "MIGRATION_HELPERS", "MAX_KNOWN_VERSION")


def _round_cost(value: float | None) -> float | None:
    """Round a cost for the audit, preserving an unmeasured ``None`` as ``None``.

    A ``None`` cost means the phase's spend could not be measured (e.g. a run
    killed before its cost-bearing result event and no usage was reconstructable).
    It must stay ``None`` in the audit — a coerced ``0.0`` is indistinguishable
    from a genuinely free run and corrupts every cost-based view built on the
    audit substrate.
    """
    return round(value, 6) if value is not None else None


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
    *,
    allow_unmerged_commits: bool = False,
    require_landed: bool = False,
) -> bool:
    """Return True if any prior run for slug produced a review APPROVE.

    Reads .forge/audits/history.jsonl line-by-line. Returns False on missing
    file, parse errors, or if no matching APPROVE record exists (safe default:
    assume no APPROVE so review is never skipped incorrectly).

    By default, an APPROVE record is skipped if the feature branch still has
    unmerged commits ahead of base_branch — that indicates an abandoned run.
    Callers can opt out of that stale-branch guard when they only need the
    persisted audit verdict itself (for example, squash-merge detection where
    the branch necessarily remains ahead after merge).

    Args:
        branch: The feature branch name (e.g. config.workspace.branch_pattern
            formatted with slug). If None, defaults to 'feat/<slug>'.
        allow_unmerged_commits: When True, return APPROVE based solely on audit
            history without rejecting branches that still appear ahead of base.
        require_landed: When True, only count APPROVE records whose landing
            status shows story-specific work actually landed on the base branch.
    """
    from theforge.coordinator import audit_substrate

    # A truly fresh repo (no per-run files, no legacy history.jsonl, no
    # substrate) has no prior APPROVE — that is a safe default. When
    # audit inputs do exist or the substrate file is present (including
    # corrupt), the substrate path surfaces the problem; we do NOT
    # silently mask it as "no APPROVE".
    sub_path = audit_substrate.substrate_path(project_root)
    if not sub_path.exists() and not audit_substrate.has_audit_inputs(project_root):
        return False
    conn = audit_substrate.require_substrate(project_root)
    feature_branch = branch if branch is not None else f"feat/{slug}"
    branch_is_stale: bool | None = None
    try:
        for record in audit_substrate.has_review_approve_in_substrate(
            conn, slug, require_landed=require_landed
        ):
            if not allow_unmerged_commits:
                if branch_is_stale is None:
                    branch_is_stale = _branch_has_unmerged_commits(
                        project_root, feature_branch, base_branch
                    )
                if branch_is_stale:
                    continue  # stale APPROVE from abandoned run
            return True
    finally:
        conn.close()
    return False


def _serialize_plan_review_result(result: object, attempt: int) -> dict:
    profile_name = getattr(result, "profile_name", "") or "unknown"
    # Preserve an unmeasured cost (None) rather than coercing to 0.0 — a
    # reviewer run killed before its cost-bearing result event still spent money.
    _raw_cost = getattr(result, "cost_usd", None)
    cost_usd = round(_raw_cost, 6) if _raw_cost is not None else None
    if not getattr(result, "success", False):
        entry = {
            "attempt": attempt,
            "profile": profile_name,
            "verdict": "CRASHED",
            "cost_usd": cost_usd,
        }
        if getattr(result, "failure_code", None):
            entry["crash_kind"] = result.failure_code
        return entry

    parsed = parse_plan_review_output(getattr(result, "output", "") or "")
    if parsed.parse_errors:
        verdict = "PARSE_ERROR"
    elif parsed.verdict == "APPROVE":
        verdict = "APPROVE"
    else:
        verdict = "REQUEST_CHANGES"

    return {
        "attempt": attempt,
        "profile": profile_name,
        "verdict": verdict,
        "cost_usd": cost_usd,
    }


def _build_plan_review_per_reviewer(state: CoordinatorState, config: ForgeConfig) -> list[dict]:
    if state.plan_review_mode != "agent" or not state.plan_review_results:
        return []

    pool_size = len(config.plan_agent_review.profiles)
    if pool_size <= 0:
        return [
            _serialize_plan_review_result(result, attempt=0)
            for result in state.plan_review_results
        ]

    # Both retry paths append a pre-retry result to state.plan_review_results per
    # retry, so the per-attempt chunk size in plan_review_results is
    # pool_size + transport_retries + parse_retries for that attempt.
    retry_counts_by_attempt: dict[int, int] = {}
    for event in (*state.plan_review_transport_retries, *state.plan_review_parse_retries):
        attempt = event.get("attempt")
        if isinstance(attempt, int):
            retry_counts_by_attempt[attempt] = retry_counts_by_attempt.get(attempt, 0) + 1

    attempt_count = len(state.plan_review_durations)
    if attempt_count <= 0:
        attempt_count = 1

    per_reviewer: list[dict] = []
    cursor = 0
    for attempt in range(attempt_count):
        attempt_result_count = pool_size + retry_counts_by_attempt.get(attempt, 0)
        if cursor + attempt_result_count > len(state.plan_review_results):
            break
        attempt_results = state.plan_review_results[cursor : cursor + attempt_result_count]
        final_results = attempt_results[-pool_size:]
        per_reviewer.extend(
            _serialize_plan_review_result(result, attempt=attempt) for result in final_results
        )
        cursor += attempt_result_count

    return per_reviewer


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
            "cost_usd": _round_cost(state.total_preflight_cost_measured),
            "duration_s": round(state.preflight_duration_s, 2)
            if state.preflight_duration_s is not None
            else None,
            "outcome": state.preflight_verdict.lower() if state.preflight_verdict else None,
        }

    # ── plan ──────────────────────────────────────────────────────────────────
    plan_block: dict | None = None
    if state.plan_results:
        plan_block = {
            "cost_usd": _round_cost(state.total_plan_cost_measured),
            "duration_s": round(sum(state.plan_durations), 2) if state.plan_durations else None,
            "outcome": "success",
            "plan_structured": state.plan_structured,
            "attempt_plans": [
                {"attempt": i, "plan": p} for i, p in enumerate(state.plan_attempt_plans)
            ],
        }

    # ── plan_review ───────────────────────────────────────────────────────────
    plan_review_block: dict | None = None
    if state.plan_review_decision is not None:
        _per_reviewer = _build_plan_review_per_reviewer(state, config)
        plan_review_block = {
            "cost_usd": _round_cost(state.total_plan_review_cost_measured),
            "duration_s": round(sum(state.plan_review_durations), 2)
            if state.plan_review_durations
            else None,
            "iterations": len(state.plan_review_results),
            "outcome": state.plan_review_decision,
            **({"per_reviewer": _per_reviewer} if _per_reviewer else {}),
        }

    # ── dev ───────────────────────────────────────────────────────────────────
    dev_block: dict | None = None
    if state.dev_results:
        _dev_transport_retries = [
            event
            for item in state.dev_iteration_telemetry
            for event in item.transport_retry_events
        ]
        dev_block = {
            "cost_usd": _round_cost(state.total_dev_cost_measured),
            "duration_s": round(sum(state.dev_durations), 2) if state.dev_durations else None,
            "iterations": len(state.dev_results),
            "outcome": "success" if state.dev_results[-1].success else "failure",
            **({"transport_retries": _dev_transport_retries} if _dev_transport_retries else {}),
        }

    # ── validate ──────────────────────────────────────────────────────────────
    validate_block: dict | None = None
    if state.gate_decisions or state.gate_debug_telemetry or state.validate_durations:
        validate_block = {
            "cost_usd": 0.0,
            "duration_s": round(sum(state.validate_durations), 2)
            if state.validate_durations
            else None,
            "outcome": state.gate_decisions[-1].lower()
            if state.gate_decisions
            else ("error" if state.validate_durations else None),
            "gate_debug": _serialize_gate_debug_metrics(state),
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
        # Accumulate per reviewer, preserving an unmeasured cost: if any of a
        # reviewer's runs reports cost_usd is None (e.g. killed before its
        # cost-bearing result event), that reviewer's total is cost-unknown
        # (None), never a coerced 0.0.
        _reviewer_costs: dict[str, float] = {}
        _reviewer_unmeasured: set[str] = set()
        for r in state.review_agent_results:
            if r.profile_name and r.profile_name != "synthesis":
                if r.cost_usd is None:
                    _reviewer_unmeasured.add(r.profile_name)
                else:
                    _reviewer_costs[r.profile_name] = (
                        _reviewer_costs.get(r.profile_name, 0.0) + r.cost_usd
                    )
        per_reviewer = {
            name: {
                "cost": None if name in _reviewer_unmeasured else round(cost, 6),
                "verdict": _last_verdicts.get(name),
            }
            for name, cost in _reviewer_costs.items()
        }
        # A reviewer whose only run(s) were all unmeasured has no entry in
        # _reviewer_costs; surface it with cost None rather than dropping it.
        for name in _reviewer_unmeasured:
            if name not in per_reviewer:
                per_reviewer[name] = {"cost": None, "verdict": _last_verdicts.get(name)}
        # Final verdict from most recent review cycle
        _final_verdict: str | None = None
        if state.review_results:
            _final_verdict = state.review_results[-1].verdict.lower()
        review_block = {
            "cost_usd": _round_cost(state.total_review_cost_measured),
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
        "cost_usd": _round_cost(state.total_cost_measured),
        "duration_s": round(sum(all_durations), 2),
        "dev_attempts_total": len(state.dev_results),
        "dev_iterations_productive": len(state.dev_results),
        "review_cycles_total": state.review_cycle,
        # kept for backward compatibility with older audit readers
        "dev_iterations": len(state.dev_results),
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


def _build_iteration_usage_summary(state: CoordinatorState, config: ForgeConfig) -> dict:
    dev_used = len(state.dev_iteration_telemetry)
    dev_max = (
        state.adaptive_dev_max
        if state.adaptive_dev_max
        else (
            state.dev_iteration_telemetry[0].max_iterations
            if state.dev_iteration_telemetry
            else config.retry.max_dev_iterations
        )
    )
    review_used = len(state.review_iteration_telemetry)
    review_max = (
        state.adaptive_review_max
        if state.adaptive_review_max
        else (
            state.review_iteration_telemetry[0].max_iterations
            if state.review_iteration_telemetry
            else config.retry.max_review_cycles
        )
    )
    return {
        "dev": {
            "used": dev_used,
            "max": dev_max,
            "hit_limit": dev_used >= dev_max and dev_used > 0,
            "early_finish": 0 < dev_used < dev_max,
        },
        "review": {
            "used": review_used,
            "max": review_max,
            "hit_limit": review_used >= review_max and review_used > 0,
            "early_finish": 0 < review_used < review_max,
            "early_terminated": state.review_early_terminated,
        },
    }


def _serialize_dev_iteration_metrics(state: CoordinatorState) -> list[dict]:
    return [
        {
            "cycle": item.cycle,
            "iteration": item.iteration,
            "max_iterations": item.max_iterations,
            "gate_result": item.gate_result,
            "failed_tests": item.failed_tests,
            "cost_usd": item.cost_usd,
            "duration_s": round(item.duration_s, 2),
            "meaningful_progress": item.meaningful_progress,
            "files_changed": item.files_changed,
            "files_changed_count": item.files_changed_count,
            "tests_fixed_count": item.tests_fixed_count,
            "sandboxed": item.sandboxed,
            "agent_exit_code": item.agent_exit_code,
            "runner_failure_code": item.runner_failure_code,
            "runner_failure_summary": item.runner_failure_summary,
            "cli_quota_error_observed": item.cli_quota_error_observed,
            "transport_fallback_fired": item.transport_fallback_fired,
            "transport_fallback_reason": item.transport_fallback_reason,
            "transport_used": item.transport_used,
            "model_used": item.model_used,
            "transport_retry_count": item.transport_retry_count,
            "transport_retry_events": item.transport_retry_events,
        }
        for item in state.dev_iteration_telemetry
    ]


def _serialize_gate_debug_metrics(state: CoordinatorState) -> list[dict]:
    return [
        {
            "iteration": item.iteration,
            "command": item.command,
            "ran": item.ran,
            "timeout_s": item.timeout_s,
            "exit_code": item.exit_code,
            "output_tail": item.output_tail,
            "output_truncated": item.output_truncated,
        }
        for item in state.gate_debug_telemetry
    ]


def _serialize_review_iteration_metrics(state: CoordinatorState) -> list[dict]:
    return [
        {
            "iteration": item.iteration,
            "max_iterations": item.max_iterations,
            "verdict": item.verdict,
            "finding_counts": item.findings_by_severity,
            "new_findings_by_severity": item.new_findings_by_severity,
            "repeated_findings_by_severity": item.repeated_findings_by_severity,
            "novel_findings": item.novel_findings,
            "restated_findings": item.restated_findings,
            "cost_usd": round(item.cost_usd, 6),
            "duration_s": round(item.duration_s, 2),
        }
        for item in state.review_iteration_telemetry
    ]


def generate_audit_log(config: ForgeConfig, task: TaskStory, result: CoordinatorResult) -> dict:
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

    agents = build_agent_entries(state, config)
    reviews = build_reviews(state)
    plan_review_per_reviewer = _build_plan_review_per_reviewer(state, config)

    context_manifests = [
        {
            "phase": entry["phase"],
            "context_budget": entry["manifest"].budget,
            "git_sha": entry["manifest"].structural_index_git_sha,
            "items_included": [
                {
                    "source_path": item.source,
                    "kind": item.kind,
                    "type": item.item_type,
                    "reason": item.reason,
                    "lines": item.lines,
                }
                for item in entry["manifest"].included
            ],
            "items_dropped": [
                {
                    "source_path": item.source,
                    "kind": item.kind,
                    "type": item.item_type,
                    "reason": item.drop_reason or item.reason,
                    "lines": item.lines,
                }
                for item in entry["manifest"].dropped
            ],
        }
        for entry in state.context_manifests
    ]

    return {
        "forge_version": "0.1.0",
        "schema_version": SCHEMA_VERSION,
        "run_id": state.run_id,
        "task": {
            "name": task.name,
            "slug": task.slug,
            "story_path": str(task.story_path) if task.story_path is not None else None,
            "story_text": state.story_content,
            "github_issue": task.github_issue,
            "fix_ready": task.fix_ready,
            "readiness_warnings": list(task.readiness_warnings),
        },
        "outcome": {
            "success": result.success,
            "final_phase": result.phase.name,
            "message": result.message,
            "error_type": state.error_type,
            "start_phase": state.start_phase.name if state.start_phase else None,
            "stop_phase": state.stop_phase.name if state.stop_phase else None,
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
            "dev_attempts_total": len(state.dev_results),
            "dev_iterations_productive": len(state.dev_results),
            "review_cycles_total": state.review_cycle,
            # kept for backward compatibility with older audit readers
            "review_cycles": state.review_cycle,
            "dev_iterations": len(state.dev_results),
            "gate_decisions": state.gate_decisions,
            "dev_loop": _serialize_dev_iteration_metrics(state),
            "gate_debug": _serialize_gate_debug_metrics(state),
            "review_loop": _serialize_review_iteration_metrics(state),
            "usage_summary": _build_iteration_usage_summary(state, config),
            "adaptive_limits": state.adaptive_limits_audit or None,
            "budget_consumption_log": [
                {
                    "cycle": entry.cycle,
                    "cycle_count": entry.cycle_count,
                    "total_count": entry.total_count,
                    "timestamp": entry.timestamp,
                }
                for entry in state.budget.consumption_log
            ],
        },
        "cost": {
            # Use *_measured aggregates so an unmeasured kill-path run stays
            # cost-unknown (None) here rather than being coerced to 0.0.
            "total_usd": state.total_cost_measured,
            "dev_usd": state.total_dev_cost_measured,
            "review_usd": state.total_review_cost_measured,
            "dev_invocations": len(state.dev_results),
            "review_invocations": len(state.review_agent_results),
            "agents": agents,
        },
        "preflight": (
            {
                "verdict": ("cached" if state.preflight_cached else state.preflight_verdict),
                "reason": state.preflight_reason,
                "complexity": state.preflight_complexity,
                "complexity_score": state.preflight_complexity_score,
                "implementation_complexity_score": (
                    state.preflight_implementation_complexity_score
                ),
                "validation_complexity_score": state.preflight_validation_complexity_score,
                "complexity_projection": state.preflight_complexity_projection,
                "complexity_evidence": list(state.preflight_complexity_evidence or []),
                "work_type": state.preflight_work_type,
                "contract_change": state.preflight_contract_change,
                "bundle_candidate": state.preflight_bundle_candidate,
                "cost_usd": state.preflight_result.cost_usd if state.preflight_result else 0.0,
                "cached": state.preflight_cached,
                "original_verdict": state.preflight_cached_original_verdict,
                "source_run_id": state.preflight_cached_from_run_id,
                "cache_snapshot": state.preflight_cache_snapshot or None,
                "cache_validation": state.preflight_cache_validation or None,
                "degraded": state.preflight_degraded,
                "degraded_reason": state.preflight_degraded_reason,
                "risk_signals": list(state.preflight_risk_signals),
                "failure_action": state.preflight_failure_action,
                "attempts": (
                    list(state.preflight_result.raw.get("attempts", []))
                    if state.preflight_result is not None
                    and isinstance(state.preflight_result.raw, dict)
                    else []
                ),
                **(
                    {"complexity_routing": state.complexity_routing_audit}
                    if state.complexity_routing_audit is not None
                    else {}
                ),
            }
            if state.preflight_verdict is not None
            else None
        ),
        "context_manifests": context_manifests,
        "dev_handoffs": [
            {
                "iteration": i + 1,
                "source": snap.get("source") if isinstance(snap, dict) else None,
                "path": snap.get("path") if isinstance(snap, dict) else None,
                "handoff": snap.get("handoff") if isinstance(snap, dict) else snap,
            }
            for i, snap in enumerate(state.dev_handoff_snapshots)
        ],
        "dev_prompt_injections": [
            {"iteration": i + 1, "finding_ids": finding_ids}
            for i, finding_ids in enumerate(state.dev_prompt_injected_finding_ids)
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
                # Same measured source of truth as phases.plan_review.cost_usd, so
                # the two plan_review surfaces can never diverge on kill-path runs:
                # an unmeasured (None) cost stays null here too, never coerced 0.0.
                "cost_usd": _round_cost(state.total_plan_review_cost_measured),
                **({"per_reviewer": plan_review_per_reviewer} if plan_review_per_reviewer else {}),
                "plan_finding_registry": [
                    {
                        "description": r.description,
                        "severity": r.severity,
                        "original_severity": r.original_severity,
                        "effective_severity": r.severity,
                        "cycle_first_seen": r.cycle_first_seen,
                        "cycle_last_seen": r.cycle_last_seen,
                        "disposition": r.disposition,
                    }
                    for r in state.plan_finding_registry
                ],
                "plan_match_provenance": state.plan_match_provenance,
                **(
                    {"regen_filter_audit": state.plan_regen_filter_audit}
                    if state.plan_regen_filter_audit
                    else {}
                ),
                **(
                    {"transport_retries": state.plan_review_transport_retries}
                    if state.plan_review_transport_retries
                    else {}
                ),
                **(
                    {"parse_retries": state.plan_review_parse_retries}
                    if state.plan_review_parse_retries
                    else {}
                ),
                **(
                    {"reviewer_failures": state.plan_review_failures}
                    if state.plan_review_failures
                    else {}
                ),
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
        "landing": build_landing_record(result.merge),
        "landing_status": getattr(result, "landing_status", None),
        "landing_event": (
            {
                "landing_status": result.landing_status,
                "landed": result.landing_status == "landed",
                "timestamp": finished_at_str,
            }
            if result.landing_status is not None
            else None
        ),
        "timeout_escalation": state.timeout_escalation_audit,
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
                "selected_action": state.escalate_selected_action,
                "advisory_generated": state.advisory_generated,
                "advisory": state.advisory_report,
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
        "error_type": state.error_type,
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
        "convention_violations": state.convention_violations
        if state.convention_violations
        else None,
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
        "conventions": {"soft": config.conventions_soft} if config.conventions_soft else None,
        **_build_phases_block(state, config),
    }
