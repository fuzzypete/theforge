"""Audit log generation for coordinator runs."""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

from theforge import __version__ as FORGE_VERSION
from theforge.config import PREFLIGHT_GATE_DECOMPOSE, ForgeConfig
from theforge.config import provenance as config_provenance
from theforge.config.sandbox_capabilities import resolve_capabilities
from theforge.review import parse_plan_review_output
from theforge.task import TaskStory

from . import story_budget as _story_budget
from .agent_failure import NO_JUDGMENT
from .audit_render import build_agent_entries, build_reviews
from .audit_substrate import CURRENT_RECORD_SCHEMA_VERSION as SCHEMA_VERSION
from .audit_substrate import MIGRATION_HELPERS
from .changed_files import resolve_changed_files
from .iteration_usage import dev_usage
from .landing_record import build_landing_record
from .preflight import complexity_source
from .state import CoordinatorResult, CoordinatorState
from .trust_status import derive_trust_status
from .util import _round_cost as _util_round_cost
from .worktree_provenance import PROVENANCE_CHANGED

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
    return _util_round_cost(value, 6)


def _effective_audit_config(config: ForgeConfig, result: CoordinatorResult) -> ForgeConfig:
    """Return the config the coordinator actually executed under for this result."""
    runtime_config = getattr(result, "runtime_config", None)
    return runtime_config if isinstance(runtime_config, ForgeConfig) else config


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


def latest_run_outcome(project_root: Path, slug: str) -> dict | None:
    """Return the recorded outcome of the most recent run for ``slug``.

    Read-only companion to :func:`has_review_approve`, which is a positive-only
    signal (did *any* run APPROVE?). Merge-evidence resolution also needs the
    negative case — the last run ended unsuccessfully with nothing landed — so
    a textual commit-message match cannot claim a merge the audit trail
    contradicts (#2374).

    Returns ``None`` when the repo has no audit history at all or no record for
    the slug. Substrate errors propagate: a caller that wants to treat an
    unreadable audit as "no opinion" must catch them itself.
    """
    from theforge.coordinator import audit_substrate

    sub_path = audit_substrate.substrate_path(project_root)
    if not sub_path.exists() and not audit_substrate.has_audit_inputs(project_root):
        return None
    conn = audit_substrate.require_substrate(project_root)
    try:
        return audit_substrate.latest_run_outcome_in_substrate(conn, slug)
    finally:
        conn.close()


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


def _build_reviewer_value(entries: list[dict] | None, *, index_key: str) -> list[dict]:
    """Per-reviewer mechanical value telemetry for the audit record (#1443/#2156).

    Surfaces the deterministic signals captured at reviewer-pool completion —
    unique P1 count, total P1 count, latency, and parse-error count — plus the
    derived ``latency_per_p1_s`` so an operator can answer "is this reviewer
    earning its wall-clock cost?" from structured telemetry without grepping logs.
    Parse-error count is carried straight from the per-reviewer capture, which
    derives it from the existing parse step (no parallel parse-failure writer).

    ``index_key`` names the per-phase pool index the capture is keyed on: plan
    review records one row per pool ``attempt``, code review one per review
    ``cycle``. Both are echoed under the same key they were captured with, so the
    audit row and the native capture stay trivially cross-referenceable.
    """
    out: list[dict] = []
    for v in entries or []:
        if not isinstance(v, dict):
            continue
        total_p1 = int(v.get("total_p1_count", 0))
        latency_s = v.get("latency_s")
        latency_per_p1 = (
            round(latency_s / total_p1, 2) if latency_s is not None and total_p1 > 0 else None
        )
        out.append(
            {
                index_key: v.get(index_key),
                "reviewer": v.get("reviewer"),
                "complexity": v.get("complexity"),
                "unique_p1_count": int(v.get("unique_p1_count", 0)),
                "total_p1_count": total_p1,
                "latency_s": latency_s,
                "latency_per_p1_s": latency_per_p1,
                "parse_error_count": int(v.get("parse_error_count", 0)),
            }
        )
    return out


def _preflight_no_judgment_run(state: CoordinatorState) -> bool:
    """True when PREFLIGHT ran but obtained no model output at all (#1951).

    Such a run records no verdict — there is none to record — so every audit
    gate keyed on ``preflight_verdict is not None`` would silently drop the
    phase. This predicate keeps it visible and labelled for what it is.
    """
    return (
        state.preflight_verdict is None
        and (state.infrastructure_failure or {}).get("phase") == "PREFLIGHT"
    )


def _build_phases_block(state: CoordinatorState, config: ForgeConfig) -> dict:
    """Build the phases + totals block for the audit log.

    Each phase entry is None when the phase did not run (e.g. preflight skipped).
    dev_durations and review_durations already exist on CoordinatorState and are
    populated by coordinator.py — no additional tracking needed for those two phases.
    """
    # ── preflight ─────────────────────────────────────────────────────────────
    preflight_block: dict | None = None
    # A preflight that produced no model output records no verdict (#1951) but
    # still ran and still cost money — emit the phase with an explicit
    # ``no_judgment`` outcome rather than dropping it, which would read as
    # "preflight never ran".
    _preflight_no_judgment = _preflight_no_judgment_run(state)
    if state.preflight_verdict is not None or _preflight_no_judgment:
        preflight_block = {
            "cost_usd": _round_cost(state.total_preflight_cost_measured),
            "duration_s": round(state.preflight_duration_s, 2)
            if state.preflight_duration_s is not None
            else None,
            "outcome": (
                state.preflight_verdict.lower() if state.preflight_verdict else "no_judgment"
            ),
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
            **(
                {"transport_retries": state.plan_transport_retries}
                if state.plan_transport_retries
                else {}
            ),
        }

    # ── plan_review ───────────────────────────────────────────────────────────
    plan_review_block: dict | None = None
    # A plan review that ran but obtained too few model judgments records no
    # decision (#1951) — it did not reject the plan. Keep the phase in the audit
    # with an explicit ``no_judgment`` outcome so the reviewers that DID answer,
    # and the ones that did not, both stay inspectable.
    if state.plan_review_decision is not None or state.plan_review_results:
        _per_reviewer = _build_plan_review_per_reviewer(state, config)
        plan_review_block = {
            "cost_usd": _round_cost(state.total_plan_review_cost_measured),
            "duration_s": round(sum(state.plan_review_durations), 2)
            if state.plan_review_durations
            else None,
            "iterations": len(state.plan_review_results),
            "outcome": state.plan_review_decision or "no_judgment",
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
    if (
        state.gate_decisions
        or state.gate_debug_telemetry
        or state.gate_diagnostic_telemetry
        or state.validate_durations
    ):
        validate_block = {
            "cost_usd": 0.0,
            "duration_s": round(sum(state.validate_durations), 2)
            if state.validate_durations
            else None,
            "outcome": state.gate_decisions[-1].lower()
            if state.gate_decisions
            else ("error" if state.validate_durations else None),
            "gate_debug": _serialize_gate_debug_metrics(state),
            "gate_diagnostic": _serialize_gate_diagnostic_metrics(state),
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
            # Reviewer cycles only — this block describes the REVIEW phase, and a
            # cycle VALIDATE opened for its own finding never ran one (#1981).
            "cycles": state.reviewer_cycles_run,
            "outcome": _final_verdict,
            "per_reviewer": per_reviewer,
            # Code-review mechanical value telemetry (#2156): the same
            # uniqueness / latency-per-P1 rows the plan_review block carries,
            # captured at code-review pool completion.
            **(
                {
                    "per_reviewer_value": _build_reviewer_value(
                        state.code_reviewer_value, index_key="cycle"
                    )
                }
                if state.code_reviewer_value
                else {}
            ),
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
        # Reviewer cycles only; cycles VALIDATE opened are reported separately so
        # neither readers nor the adaptive learner conflate them (#1981).
        "review_cycles_total": state.reviewer_cycles_run,
        "review_cycles_opened_by_validate": state.validate_opened_review_cycles,
        # kept for backward compatibility with older audit readers
        "dev_iterations": len(state.dev_results),
        "review_cycles": state.reviewer_cycles_run,
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
    # Per-cycle used against the per-cycle max: dev_max is a per-cycle budget
    # limit, so counting every dev iteration the story ever ran reported
    # used > max for any story spanning more than one review cycle (#1985).
    dev_used, dev_max = dev_usage(state, default_max=config.retry.max_dev_iterations)
    # Budget view: a review cycle VALIDATE bought for a gate or convention
    # finding was really spent, so exhaustion must not report zero (#1981).
    review_used = state.review_cycles_spent
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
            "hit_limit": (review_used >= review_max and review_used > 0)
            or state.review_budget_exhausted,
            "early_finish": (0 < review_used < review_max and not state.review_budget_exhausted),
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
            "gate_output_format_recognized": item.gate_output_format_recognized,
            # The value the stall brake compares across iterations. A terminal
            # "gate output identical" decision is only checkable if the operator
            # can see the fingerprints it was made from (#1981).
            "gate_output_fingerprint": item.gate_output_fingerprint,
            "cost_usd": item.cost_usd,
            "duration_s": round(item.duration_s, 2),
            "meaningful_progress": item.meaningful_progress,
            "files_changed": item.files_changed,
            "files_changed_count": item.files_changed_count,
            "tests_fixed_count": item.tests_fixed_count,
            "sandboxed": item.sandboxed,
            "containment": item.containment,
            # Capability profile granted for this iteration (#1947). Falls back
            # to the explicit default payload so an older/never-set record reads
            # as "default containment", not "capability data missing".
            "sandbox_capabilities": (
                item.sandbox_capabilities or resolve_capabilities(None).audit_payload()
            ),
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
            # Declared verification commands the coordinator ran outside the dev
            # sandbox at this iteration's request (ADR-0007 / #2050). Every
            # unconfined execution the run performed on the agent's behalf is
            # named here with its outcome and a trace path to the full output.
            "verification_requests": item.verification_requests,
        }
        for item in state.dev_iteration_telemetry
    ]


def _serialize_gate_debug_metrics(state: CoordinatorState) -> list[dict]:
    """Serialize the post-timeout gate debug command runs.

    ``trace_index`` (not ``iteration``) is the monotonic counter that names this
    entry's trace file, and ``trace_path`` is that file — so the artifact an
    escalation quotes resolves to the entry it is attached to. ``iteration`` in
    ``iterations.dev_loop`` is a different, per-review-cycle counter; giving both
    the same name made the two disagree from the second cycle on (#1986).
    """
    return [
        {
            "trace_index": item.trace_index,
            "trace_path": item.trace_path,
            "command": item.command,
            "ran": item.ran,
            "timeout_s": item.timeout_s,
            "exit_code": item.exit_code,
            "output_tail": item.output_tail,
            "output_truncated": item.output_truncated,
        }
        for item in state.gate_debug_telemetry
    ]


def _serialize_gate_diagnostic_metrics(state: CoordinatorState) -> list[dict]:
    """Serialize the gate-timeout diagnostic re-run passes (issue #1217).

    ``trace_index``/``trace_path`` carry the same contract as in
    :func:`_serialize_gate_debug_metrics` (#1986).
    """
    return [
        {
            "trace_index": item.trace_index,
            "trace_path": item.trace_path,
            "command": item.command,
            "ran": item.ran,
            "workload_executed": item.ran,
            "budget_s": item.budget_s,
            "per_test_timeout_s": item.per_test_timeout_s,
            "exit_code": item.exit_code,
            "timed_out": item.timed_out,
            "hanging_test": item.hanging_test,
            "output_tail": item.output_tail,
            "output_truncated": item.output_truncated,
        }
        for item in state.gate_diagnostic_telemetry
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
            "cost_usd": _round_cost(item.cost_usd),
            "duration_s": round(item.duration_s, 2),
        }
        for item in state.review_iteration_telemetry
    ]


def generate_audit_log(config: ForgeConfig, task: TaskStory, result: CoordinatorResult) -> dict:
    """Generate a structured audit log for the entire coordination run.

    This is the orchestrator's own handoff — a complete record of what happened.
    """
    config = _effective_audit_config(config, result)
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
            "prior_run_context": entry["manifest"].prior_run_context,
            "invariant_context": entry["manifest"].invariant_context,
        }
        for entry in state.context_manifests
    ]

    return {
        "forge_version": FORGE_VERSION,
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
        # ── No-judgment invocation telemetry (#1951) ──────────────────────
        # Whether a model actually spoke is a property of the run that every
        # consumer of its verdicts needs: an outcome produced without any model
        # output is not evidence about the story. Recorded as native structured
        # telemetry alongside the outcome it explains, never inferred from prose.
        "agent_invocation": {
            # Non-null when the RUN ended because no judgment could be obtained.
            "infrastructure_failure": state.infrastructure_failure,
            # Every invocation this run that produced no model output, whether
            # or not the phase recovered from it.
            "no_judgment_failures": list(state.agent_invocation_failures),
            # Pools that completed with members lost to the substrate.
            "degraded_pools": list(state.degraded_pools),
        },
        # ── Shared run-infrastructure failures (#2107) ────────────────────
        # Failures of a resource every story shares (e.g. the rolling advisory
        # artifact all workers of a sprint write). Rendered verbatim so the
        # operator sees the real errno and path and can attribute the failure to
        # the infrastructure instead of to this story's work.
        "shared_infrastructure_failures": list(state.shared_infrastructure_failures),
        # ── Abnormal termination (#2030) ──────────────────────────────────
        # How the run ended when it did not end by its own state machine: the
        # worker raised, its deadline expired, or the launch guard dropped the
        # story before dispatch. Those exits never reach the coordinator's own
        # finalization, so without this the least recoverable runs were the ones
        # with no record of a cause at all. None for a normally-terminating run.
        "abnormal_termination": state.abnormal_termination,
        # ── Durable phase recovery (#2155) ────────────────────────────────
        # A resumed attempt allocates a fresh CoordinatorState, so the phases an
        # earlier attempt of the same story ran (preflight and the routing
        # decision derived from it, plan review, the escalate gate) exist only in
        # the durable phase record. Null on a run that produced its own phase
        # outputs. Non-null names which phases were lifted off disk — and, when
        # the record was missing or did not describe this story text, says so.
        # That distinction is the point: without it a null ``preflight`` block on
        # a resumed run is indistinguishable from a phase that never ran, and the
        # SKIPPED verdict this replaced asserted a bypass that never happened.
        "phase_recovery": state.phase_recovery,
        # ── Specification-gap backchannel (#2122) ─────────────────────────
        # What the dev agent could not read off the spec, and what the run did
        # about it. ``events`` is every gap raised (including one whose block was
        # malformed); ``resolutions`` is how each ended — an operator answer, an
        # expired pause, or an exhausted allowance — with the assumption the run
        # proceeded under when no answer was given. Empty lists on the ordinary
        # run where nothing was ambiguous. The pair is the traceability the
        # acceptance criteria require: the answer that shaped the
        # implementation, and the fact that a guess was recorded as a guess
        # rather than laundered into the spec.
        "spec_gaps": {
            "allowance": max(0, int(getattr(config.retry, "max_spec_gap_pauses", 0))),
            "pauses_used": state.spec_gap_pauses_used,
            "events": list(state.spec_gap_events),
            "resolutions": list(state.spec_gap_resolutions),
        },
        # ── Preflight complexity gate (#2681) ─────────────────────────────
        # The end-of-preflight scope decision: whether the run paused, the size
        # it paused at on both axes, the threshold that opened it, what was
        # decided, and whether an operator decided it at all. Present on every
        # record — ``opened: false`` is the readable fact that this run was not
        # over the threshold, which a null block could not distinguish from a
        # run that predates the gate.
        "preflight_complexity_gate": {
            "opened": bool(state.preflight_complexity_gate_opened),
            "complexity_score": state.preflight_complexity_gate_score,
            "implementation_complexity_score": (
                state.preflight_complexity_gate_implementation_score
            ),
            "validation_complexity_score": state.preflight_complexity_gate_validation_score,
            "threshold": state.preflight_complexity_gate_threshold,
            "decision": state.preflight_complexity_gate_decision,
            # "operator" when a human answered, "no_decision" when the wait
            # expired and the configured action was applied instead.
            "decision_source": state.preflight_complexity_gate_decision_source,
            "no_decision_action_configured": str(
                getattr(config.retry, "preflight_complexity_gate_no_decision", "")
            ),
            "no_decision_fallback": state.preflight_complexity_gate_no_decision_fallback,
            "waited_seconds": state.preflight_complexity_gate_waited_seconds,
            "decided_at": state.preflight_complexity_gate_decided_at,
            # Non-null when the gating score came from a degraded preflight, or
            # one that examined no criteria. This is context, not a suppression:
            # the gate opens on any PROCEED score at or above the threshold, and
            # the operator rules with this provenance in front of them.
            "score_provenance_note": state.preflight_complexity_gate_score_provenance,
        },
        "outcome": {
            "success": result.success,
            # Not a failure and not a success: the operator (or the configured
            # no-decision action) returned the story to be split at the
            # preflight gate. Recorded next to ``success`` so a reader of the
            # outcome block alone cannot mistake it for a story that could not
            # be made to work (#2681).
            "returned_for_decomposition": (
                state.preflight_complexity_gate_decision == PREFLIGHT_GATE_DECOMPOSE
            ),
            "final_phase": result.phase.name,
            "message": result.message,
            "error_type": state.error_type,
            "start_phase": state.start_phase.name if state.start_phase else None,
            "stop_phase": state.stop_phase.name if state.stop_phase else None,
        },
        # ── Configuration identity (#2056) ────────────────────────────────
        # What this run was a run *of*: the configuration is part of the run's
        # identity, so an outcome recorded without it cannot be compared against
        # another run's outcome. Emitted for every run, with explicit nulls when
        # identity is unknown.
        "configuration": _build_configuration_block(config),
        "timing": {
            "started_at": state.started_at,
            "finished_at": finished_at_str,
            "duration_seconds": duration_seconds,
        },
        "workspace": {
            "path": str(state.workspace_path) if state.workspace_path else None,
            "branch": state.branch_name,
            # Whether the story text that produced this workspace's contents is
            # the text the run executed — the same provenance question the
            # resume record answers about phase records, asked about the
            # artifacts those phases produced (#2288).
            "story_provenance": state.workspace_provenance_status,
            # Derived from the provenance status, never from the prompt note:
            # the note is a one-shot channel the dev phase consumes on the first
            # iteration, so reading it here reported False for exactly the runs
            # that did inherit superseded work. The status is written once at
            # WORKSPACE and never consumed.
            "inherited_superseded_work": (state.workspace_provenance_status == PROVENANCE_CHANGED),
            # Whether the dev agent was actually told. Distinct from the line
            # above: a run can inherit superseded work and stop before DEV (or
            # be resumed past it), and an audit that could not separate those
            # cases would make an unwarned dev indistinguishable from a warned
            # one.
            "inherited_work_surfaced_to_dev": state.workspace_inherited_work_surfaced_to_dev,
            # Run-level substrate decision: which forge-owned sandbox capability
            # profile widened containment, and to exactly what (#1947).
            "sandbox_capabilities": (
                state.dev_sandbox_capabilities or resolve_capabilities(None).audit_payload()
            ),
            # Run-level roll-up of every project-declared verification command the
            # coordinator executed outside the sandbox on the dev agent's behalf
            # (ADR-0007 / #2050). Kept at run level as well as per iteration
            # because some dev exit paths return before iteration telemetry is
            # recorded, and an unconfined execution must never go unrecorded.
            "dev_verification_requests": list(state.dev_verification_requests),
        },
        "iterations": {
            "dev_attempts_total": len(state.dev_results),
            "dev_iterations_productive": len(state.dev_results),
            # Reviewer view: the adaptive iteration learner percentiles these to
            # set future max_review_cycles, so cycles VALIDATE opened for its own
            # blocking findings are excluded — a gate failure must not teach the
            # router that reviewers need more cycles (#1981).
            "review_cycles_total": state.reviewer_cycles_run,
            # kept for backward compatibility with older audit readers
            "review_cycles": state.reviewer_cycles_run,
            "review_cycles_opened_by_validate": state.validate_opened_review_cycles,
            "dev_iterations": len(state.dev_results),
            "gate_decisions": state.gate_decisions,
            # Executions of the gate command, including ones that timed out or
            # errored and excluding runs where gate_override skipped the gate.
            # Differs from len(gate_decisions) by design (#1984).
            "gate_runs": state.gate_runs,
            # Provenance for each validation run: profile, authority, resolved
            # command, result, commit, skipped (#2358). The gate_decisions list
            # above says a command exited zero; this says what that was worth.
            "validation_runs": list(state.validation_runs),
            "dev_loop": _serialize_dev_iteration_metrics(state),
            "gate_debug": _serialize_gate_debug_metrics(state),
            # Gate commands that left processes running and had them killed at
            # teardown (#2309). Empty on the ordinary run where the gate's tree
            # ended with it; an entry means this run leaked work onto the host
            # and something had to end it — the one fact a leaked process
            # otherwise leaves no trace of anywhere in the record.
            "gate_process_teardowns": list(state.gate_process_teardowns),
            "gate_diagnostic": _serialize_gate_diagnostic_metrics(state),
            "review_loop": _serialize_review_iteration_metrics(state),
            "usage_summary": _build_iteration_usage_summary(state, config),
            "adaptive_limits": state.adaptive_limits_audit or None,
            # Development invocations shortened to fit the enclosing story
            # deadline (#2333). Empty on a story with room to spare; entries are
            # the difference between a timeout the run chose and one the sprint
            # scheduler imposed with a signal, so the values that produced the
            # shorter invocation are on the record rather than reconstructed.
            "dev_timeout_clamps": list(state.dev_timeout_clamps),
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
            # Per-story allocation derived from the complexity band (#2169).
            # Carries the basis and the band's expected range next to what the
            # story actually spent, so "cost $8" can be read as ordinary or
            # anomalous without the reader reconstructing the distribution.
            "story_allocation": (
                _story_budget.evaluate_allocation_dict(
                    state.story_allocation, state.total_cost_measured
                )
                if state.story_allocation
                else None
            ),
            "allocation_exhausted": state.allocation_exhausted,
            "reviewer_budget_overruns": list(state.reviewer_budget_overruns),
        },
        "preflight": (
            {
                "verdict": (
                    "cached"
                    if state.preflight_cached
                    else (
                        state.preflight_verdict
                        # Explicit marker beats a null an operator would read as
                        # "not recorded" (#1951).
                        or (NO_JUDGMENT if _preflight_no_judgment_run(state) else None)
                    )
                ),
                "reason": state.preflight_reason,
                "complexity": state.preflight_complexity,
                "complexity_score": state.preflight_complexity_score,
                "implementation_complexity_score": (
                    state.preflight_implementation_complexity_score
                ),
                "validation_complexity_score": state.preflight_validation_complexity_score,
                "complexity_projection": state.preflight_complexity_projection,
                "complexity_evidence": list(state.preflight_complexity_evidence or []),
                # Read off the implementation axis at its ceiling (#2680): the
                # story should be decomposed, stated rather than inferred from
                # the score. Routing is identical for 9 and 10.
                "scope_exceeded": state.preflight_scope_exceeded,
                "work_type": state.preflight_work_type,
                "domains": list(state.preflight_domains or []),
                "contract_change": state.preflight_contract_change,
                "bundle_candidate": state.preflight_bundle_candidate,
                "batch_group": state.preflight_batch_group,
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
                # Provenance of the complexity fields above. A degraded phase
                # still emits a conservative score, and without this label the
                # persistent record cannot distinguish it from an agent-founded
                # one (#2346).
                "complexity_source": complexity_source(state),
                # Exploration salvaged from a failed preflight run (#706): files
                # inspected, tool calls, partial conclusion. None on success.
                "partial_evidence": state.preflight_partial_evidence,
                # Policy-assertion provenance (#2137). Which kind of blocker a
                # BLOCKED verdict declared, the standing policy it cited, how each
                # citation resolved against the ratified-policy registry, and the
                # retraction/ratification candidates that fell out. Present on
                # every record so "no assertion was cited" is a readable fact.
                "blocking_basis": state.preflight_blocking_basis,
                "policy_assertions_cited": list(state.preflight_policy_assertions_cited or []),
                "policy_assertions_resolved": list(
                    state.preflight_policy_assertions_resolved or []
                ),
                "policy_retraction_candidates": list(
                    state.preflight_policy_retraction_candidates or []
                ),
                "policy_ratification_candidates": list(
                    state.preflight_policy_ratification_candidates or []
                ),
                "policy_blocking_authority": state.preflight_policy_blocking_authority,
                "policy_adjudication": dict(state.preflight_policy_adjudication or {}),
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
            if state.preflight_verdict is not None or _preflight_no_judgment_run(state)
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
        # Reviewer attempt-completion telemetry (#1388): every reviewer invocation
        # this run, including transport failures / timeouts / parse failures /
        # crashes. This is the authoritative native per-run record the derived
        # reviewer completion-rate profile is folded from (ADR-0002).
        "reviewer_attempts": list(state.reviewer_attempts or []),
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
                **(
                    {
                        "per_reviewer_value": _build_reviewer_value(
                            state.plan_reviewer_value, index_key="attempt"
                        )
                    }
                    if state.plan_reviewer_value
                    else {}
                ),
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
            # A plan review that ran but obtained too few model judgments records
            # no decision (#1951). Keep the block — with a null decision — so the
            # reviewer failures that explain it stay inspectable.
            if state.plan_review_decision is not None or state.plan_review_results
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
        # Which review the landing was taken on: a merged cycle review, or a
        # reviewer verdict selected at the escalate gate when no merged review
        # existed (#2300). None when nothing was landed.
        "landing_review": (
            {
                "source": state.landing_review_source,
                "verdict": state.landing_review_result.verdict
                if state.landing_review_result is not None
                else None,
            }
            if state.landing_review_source is not None
            else None
        ),
        "landing_event": (
            {
                "landing_status": result.landing_status,
                "landed": result.landing_status == "landed",
                "timestamp": finished_at_str,
            }
            if result.landing_status is not None
            else None
        ),
        # Gate-green salvage (#2028): the reviewed, gate-green commit this story
        # retained as a landing floor, the decision to land it instead of the
        # gate-red HEAD, and — when no salvage was taken — why not. The declined
        # record is what separates "nothing gate-green ever existed" from
        # "salvageable but forge would not land it", which the failure alone
        # cannot say. Absent when nothing about this story involved a checkpoint.
        "gate_green_salvage": (
            {
                "checkpoint": (
                    state.gate_green_checkpoint.to_audit_dict()
                    if state.gate_green_checkpoint is not None
                    else None
                ),
                "salvage": state.gate_green_salvage,
                "declined": state.gate_green_salvage_declined,
            }
            if (
                state.gate_green_checkpoint is not None
                or state.gate_green_salvage is not None
                or state.gate_green_salvage_declined is not None
            )
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
                # WHO or WHAT decided, and — for an expiry — whether the advisory
                # recommendation was applied or which absence stopped it (#2279).
                # Recorded so an operator reading a finished run can tell an
                # action they chose from one applied on their behalf from a gate
                # still waiting, without inferring it from timestamps.
                "decision_source": state.escalate_decision_source,
                "timeout_advice": state.escalate_timeout_advice,
                "awaiting_operator": state.escalate_decision == "advisory_pending",
                "advisory_recommendation": (
                    (state.advisory_report or {}).get("recommendation")
                    if isinstance(state.advisory_report, dict)
                    else None
                ),
                # A selection the gate refused to carry out. Recorded alongside
                # (not in place of) human_decision so the audit shows what the
                # operator chose AND that no outcome was substituted for it
                # (#2300).
                "declined_action": state.escalate_declined_action,
                "declined_reason": state.escalate_declined_reason,
                "advisory_generated": state.advisory_generated,
                "advisory": state.advisory_report,
                "advisory_unavailable_reason": state.advisory_unavailable_reason,
                # A pre-turn advisor exit is a configuration defect that spent
                # nothing — kept distinct in the audit from an advisor that ran
                # and produced nothing usable (#2164).
                "advisory_launch_failure": state.advisory_launch_failure,
                "advisory_launch_reason": state.advisory_launch_reason,
                "waited_seconds": (
                    round(state.human_review_waited_seconds, 1)
                    if state.human_review_waited_seconds is not None
                    else None
                ),
            }
            if state.escalate_decision is not None or state.escalate_declined_action is not None
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
        # Coordinator-raised blocking findings that bought a review cycle. Kept
        # out of `reviews` on purpose: those are reviewer verdicts, these are the
        # coordinator's own (#1981).
        "validate_blocks": state.validate_blocks or None,
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
        # Topology-walk detection (#2372). Null on every run whose review cycles
        # did not show the pattern. When present it carries the family anchor, the
        # cycle sequence, and the per-cycle locations/descriptions that produced
        # it — the detector is deterministic, so a reader must be able to re-derive
        # the decision from the record rather than trust the log line.
        "review_topology_signal": state.review_topology_signal,
        # What the latest cycle's P1s were checked against before any of them was
        # allowed to block (#2525). Names the file set and its provenance, so a
        # reader can re-derive why a finding was recorded diff_ungrounded instead
        # of taking the run's word for it. Null on runs that never reached REVIEW.
        "review_diff_grounding": state.review_diff_grounding,
        # Non-blocking / suppressed P1s: findings that were set aside without
        # blocking approval. Both net_new (latent, single-reviewer) and
        # gate_contradicted (mechanically disproven by a PASS gate) belong here so
        # a reader can see which P1s were waived and on what basis — a suppression
        # that leaves no trace here is invisible where waived findings are looked
        # for. The real disposition is preserved rather than flattened to net_new.
        # diff_ungrounded belongs here for the same reason and carries the extra
        # weight of making a would-be false rejection recoverable after the fact:
        # an operator reading this list sees exactly which findings were set aside
        # for naming code this story never touched (#2525).
        "non_blocking_p1s": [
            {
                "finding_id": r.finding_id,
                "cycle_first_seen": r.cycle_first_seen,
                "file": r.file,
                "line": r.line,
                "description": r.description,
                "reporter": r.reporter,
                "disposition": r.disposition,
            }
            for r in state.finding_registry
            if r.severity == "P1"
            and r.disposition in ("net_new", "gate_contradicted", "diff_ungrounded")
        ],
        "conventions": {"soft": config.conventions_soft} if config.conventions_soft else None,
        # Symptom-verification test escalations (#1560): P2→P1 upgrades applied
        # because a bug-fix PR's reviewer flagged an absent seam-level test for the
        # closing bug's symptom path. Emitted so the rule's hit-rate and
        # override-rate become queryable from the audit substrate.
        "symptom_test_escalations": state.symptom_test_escalations
        if state.symptom_test_escalations
        else None,
        # Per-role routing explainability block (#1391). A genuine top-level key
        # in the authoritative native per-run record (ADR-0002 clauses 1-2), NOT
        # nested under preflight.complexity_routing — it is the load-bearing
        # observability contract every v0.13 routing mechanism writes into
        # (ADR-0006 clause 7). Additive: existing fields are unchanged.
        "routing_decision": state.routing_decision,
        # Structured trust marker (#1851, ADR-0006 clause 4). trust_checks holds
        # the coordinator-computed pass/fail entries (check name, result, observed
        # evidence, producer); trust_status is derived mechanically from them —
        # any failed check taints the run, an applicable pass trusts it, and a run
        # with no implemented check stays "unchecked" (admissible for routing).
        # Never set from LLM prose. Additive: existing readers are unaffected, and
        # older records migrate to "unchecked" on read (audit_substrate v7→v8).
        "trust_checks": _build_trust_checks(state),
        "trust_status": derive_trust_status(_build_trust_checks(state)),
        # ── Changed-file set (#2347) ──────────────────────────────────────
        # What the run's spend was spent *on*. The cost block above says what a
        # run cost; without this the join from cost to code does not exist, and
        # reconstructing it afterwards from commit messages recovers ~10% of
        # spend because a third of commits name more than one issue.
        #
        # Serialized from the snapshot captured at the pre-cleanup seam
        # (``changed_files.capture_changed_files``), never recomputed here from
        # a workspace landing may already have deleted. The fallback collection
        # covers runs that terminated before landing and still have their
        # worktree — escalated and failed runs, which are the runs most worth
        # attributing. ``null`` when no comparison could be made at all; a
        # captured comparison that found nothing records ``files: []``, and the
        # two must never be conflated.
        "changed_files": resolve_changed_files(state, config),
        **_build_phases_block(state, config),
    }


def _build_configuration_block(config: ForgeConfig) -> dict:
    """Return the run's configuration-provenance block (#2056).

    Names the configuration the run executed under — ``resolved_sha256``
    distinguishes one revision from another (two runs with the same digest used
    the same resolved configuration), ``source_path``/``source_sha256`` identify
    the ``forge.yaml`` it came from at load time.

    ``changed_during_run`` is decided here, at audit time, by re-reading the same
    source file and comparing digests. That comparison is the point: a
    configuration edit that lands mid-flight is a first-class event in the run's
    own provenance rather than something reconstructible only by joining the
    audit trail against ``git log``.

    Never raises. A config that was never loaded from a file records a null
    ``source_path``/``source_sha256`` — "no source identity" is a statement the
    record must be able to make; a source file unreadable at finish records the
    load-time identity plus ``finish_read_error``, so an absent digest is never
    mistaken for an unchanged one.
    """
    try:
        config = config_provenance.refresh_provenance(config)
    except Exception:  # pragma: no cover - audit emission must degrade, not abort
        pass
    provenance = getattr(config, "provenance", None)
    source_path = getattr(provenance, "source_path", None)
    source_sha256 = getattr(provenance, "source_sha256", None)
    resolved_values = getattr(provenance, "resolved_values", None)
    resolved_value_sources = getattr(provenance, "resolved_value_sources", None)
    resolved_value_path_tokens = getattr(provenance, "resolved_value_path_tokens", None)

    # Digested from the config object the coordinator actually held, not from the
    # load-time value cached on the provenance: CLI overrides (--dev-model,
    # --base-branch, --reviewers) rebuild the config after load, and a digest that
    # ignored them would name a configuration this run did not execute under.
    try:
        resolved_sha256: str | None = config_provenance.resolved_config_sha256(config)
    except Exception:  # pragma: no cover - identity must never break audit emission
        resolved_sha256 = getattr(provenance, "resolved_sha256", None)

    finish_sha256: str | None = None
    finish_read_error: str | None = None
    if source_path:
        try:
            finish_sha256 = config_provenance.file_sha256(Path(source_path))
        except OSError as exc:
            finish_read_error = f"{type(exc).__name__}: {exc}"

    # Tri-state on purpose: True/False are claims backed by two digests; None
    # means the comparison could not be made (no identity, or unreadable at
    # finish) and must not read as "did not change".
    changed_during_run: bool | None = None
    if source_sha256 is not None and finish_sha256 is not None:
        changed_during_run = finish_sha256 != source_sha256

    recorded_values: dict[str, object] | None = None

    def _recorded_value_entry(path: str, value: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "value": value,
            "source": resolved_value_sources.get(path, config_provenance.VALUE_SOURCE_DERIVED),
        }
        if isinstance(resolved_value_path_tokens, dict):
            tokens = resolved_value_path_tokens.get(path)
            if isinstance(tokens, tuple):
                entry["path_tokens"] = list(tokens)
        return entry

    if isinstance(resolved_values, dict) and isinstance(resolved_value_sources, dict):
        recorded_values = {
            "format_version": config_provenance.RESOLVED_CONFIG_RECORD_FORMAT_VERSION,
            "entries": {
                path: _recorded_value_entry(path, value)
                for path, value in sorted(resolved_values.items())
            },
        }

    return {
        "source_path": source_path,
        "source_sha256": source_sha256,
        "resolved_sha256": resolved_sha256,
        "source_sha256_at_finish": finish_sha256,
        "changed_during_run": changed_during_run,
        "finish_read_error": finish_read_error,
        "recorded_values": recorded_values,
    }


def _build_trust_checks(state: CoordinatorState) -> list[dict]:
    """Return the run's structured trust-check entries as an ordered list.

    ``state.trust_checks`` is keyed by check name so a re-run review cycle
    replaces (rather than duplicates) a check's result; the record serializes
    them as a stable, name-sorted list.
    """
    return [state.trust_checks[name] for name in sorted(state.trust_checks)]
