"""Bridge between CoordinatorState and the model_profiles aggregator.

The coordinator records every run outcome in ``model_profiles.yaml`` so the
assignment system can inform future decisions. This module extracts the
per-role telemetry from ``CoordinatorState`` and hands a ``RunOutcome`` to
:mod:`theforge.model_profiles`. Kept as a thin adapter to keep ``engine.py``
free of aggregation logic.
"""

from __future__ import annotations

from pathlib import Path

from theforge.config import ForgeConfig
from theforge.model_profiles import ReviewerAttempt, RunOutcome, update_from_run
from theforge.reviewer_value import PlanReviewerValueSample

from .state import CoordinatorState
from .trust_status import derive_trust_status, is_tainted


def _extract_reviewers(
    state: CoordinatorState,
) -> dict[str, tuple[int, int, float]]:
    """Per-reviewer ``(cycles, findings, cost)`` from cycle metadata + telemetry.

    Findings are the per-cycle aggregate (shared view across reviewers); cost
    is split evenly across successful reviewers in each cycle. Attribution is
    approximate but reasonable without per-reviewer telemetry.
    """
    out: dict[str, tuple[int, int, float]] = {}
    meta_list = state.review_cycle_metadata or []
    tele_list = state.review_iteration_telemetry or []
    for idx, meta in enumerate(meta_list):
        tele = tele_list[idx] if idx < len(tele_list) else None
        findings = sum(int(v) for v in tele.findings_by_severity.values()) if tele else 0
        cost = float(tele.cost_usd) if tele else 0.0
        participants = list(meta.successful or [])
        if not participants:
            continue
        per_head_cost = cost / len(participants) if participants else 0.0
        for name in participants:
            prev = out.get(name, (0, 0, 0.0))
            out[name] = (prev[0] + 1, prev[1] + findings, prev[2] + per_head_cost)
    return out


def _extract_reviewer_attempts(state: CoordinatorState) -> list[ReviewerAttempt]:
    """Convert the native per-run attempt dicts into ``ReviewerAttempt`` records.

    ``state.reviewer_attempts`` is the authoritative capture written at the review
    invocation boundary (#1388); this pure adapter lifts it into the typed carrier
    the profile aggregator folds. Every attempt — success and failure alike — is
    carried through, keyed by the reviewer's canonical identity so completion
    telemetry folds under the same model entry the router looks it up by.
    """
    attempts: list[ReviewerAttempt] = []
    for a in state.reviewer_attempts or []:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        attempts.append(
            ReviewerAttempt(
                name=str(a["name"]),
                completed_parseable_verdict=bool(a.get("completed_parseable_verdict")),
                outcome=str(a.get("outcome") or "completed"),
                actual_model=a.get("model"),
                provider=a.get("provider"),
                cli=a.get("cli"),
                failure_reason=a.get("failure_reason"),
            )
        )
    return attempts


def _extract_plan_reviewer_values(state: CoordinatorState) -> list[PlanReviewerValueSample]:
    """Convert per-plan-reviewer value telemetry dicts into typed samples (#1443).

    ``state.plan_reviewer_value`` is the native per-(reviewer, pool-attempt) capture
    written at plan-review pool completion. This pure adapter lifts it into the
    carrier the profile aggregator folds, keyed by each reviewer's canonical
    identity so the value signal lands under the same model entry the router looks
    it up by.
    """
    samples: list[PlanReviewerValueSample] = []
    for v in state.plan_reviewer_value or []:
        if not isinstance(v, dict) or not v.get("reviewer"):
            continue
        samples.append(
            PlanReviewerValueSample(
                name=str(v["reviewer"]),
                complexity=str(v.get("complexity") or "medium"),
                unique_p1=int(v.get("unique_p1_count", 0)),
                total_p1=int(v.get("total_p1_count", 0)),
                latency_s=v.get("latency_s"),
                actual_model=v.get("actual_model"),
                provider=v.get("provider"),
                cli=v.get("cli"),
            )
        )
    return samples


def build_run_outcome(config: ForgeConfig, state: CoordinatorState, success: bool) -> RunOutcome:
    """Pure: assemble a :class:`RunOutcome` from coordinator state."""
    complexity = state.preflight_complexity or "medium"
    # ``dev_trace_count`` is the only monotonic dev-iteration counter (never reset
    # on cycle boundaries) so it captures total dev attempts across the run.
    dev_iterations = max(int(state.dev_trace_count or 0), 1)
    # Observed wall-clock of the dev phase (sum of per-call durations). None when
    # nothing was recorded so learning never treats "unknown" as a $0-style zero.
    dev_duration_s = round(sum(state.dev_durations), 2) if state.dev_durations else None
    # A harness kill at the timeout is a censored observation, and the granted
    # timeout is the limit that terminated it — a floor the next timeout can
    # never fall below. Read the sticky ``dev_process_timeout_killed`` flag set
    # at kill time in dev_phase: the killed iteration's telemetry entry is NOT a
    # reliable signal because a later VALIDATE-phase telemetry write overwrites
    # the last entry once checkpoint-committed work (#1754) lets execution fall
    # through the terminal-kill path without recording is_timeout on the tail.
    dev_timeout_killed = bool(state.dev_process_timeout_killed)
    dev_timeout_limit_s = (
        int(state.adaptive_dev_timeout_seconds) if state.adaptive_dev_timeout_seconds else None
    )
    # Termination-cause taxonomy: why the harness ended the dev process, if it
    # did. A harness-imposed ending (a deadline kill or a stuck-pattern terminate)
    # is evidence about the budget or the harness, not about the model, so the
    # aggregator segregates these runs out of the capability statistics. Timeout
    # takes precedence when both flags happen to be set.
    dev_termination_cause = (
        "timeout"
        if state.dev_process_timeout_killed
        else ("stuck_pattern" if state.dev_process_stuck_terminated else None)
    )
    # Taint marker (ADR-0006 clause 4, #1852): derive the run's aggregate
    # trust_status from its mechanically-computed trust checks — the same
    # derivation the audit writer records — and exclude a tainted run from every
    # router-consumed capability aggregate. A run with no implemented check stays
    # "unchecked" (admissible); only an affirmative failed check taints it.
    trust_status = derive_trust_status(state.trust_checks.values())
    run_tainted = is_tainted(trust_status)
    return RunOutcome(
        complexity=complexity,
        complexity_score=state.preflight_complexity_score,
        dev_model=config.dev_profile.name,
        dev_actual_model=getattr(config.dev_profile, "model", None),
        dev_provider=getattr(config.dev_profile, "provider", None),
        dev_cli=getattr(config.dev_profile, "cli", None),
        dev_success=bool(success),
        dev_iterations=dev_iterations,
        dev_duration_s=dev_duration_s,
        dev_timeout_killed=dev_timeout_killed,
        dev_timeout_limit_s=dev_timeout_limit_s,
        dev_termination_cause=dev_termination_cause,
        # Pass the cost-unknown signal through (None) instead of coercing to
        # $0.00, so unmeasured CLI-transport runs are recorded as unmeasured.
        dev_cost_usd=state.total_dev_cost_measured,
        preflight_model=config.preflight_profile.name
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_actual_model=getattr(config.preflight_profile, "model", None)
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_provider=getattr(config.preflight_profile, "provider", None)
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_cli=getattr(config.preflight_profile, "cli", None)
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_cost_usd=state.total_preflight_cost_measured,
        reviewers=_extract_reviewers(state),
        reviewer_attempts=_extract_reviewer_attempts(state),
        plan_reviewer_values=_extract_plan_reviewer_values(state),
        # Domain tags (#155) recorded by preflight, folded into per-domain dev
        # slices so future routing can prefer models strong in the story's domains.
        domains=list(state.preflight_domains or []),
        dev_tainted=run_tainted,
    )


def update_profiles_from_run(
    *,
    profiles_path: Path,
    history_path: Path | None,
    config: ForgeConfig,
    state: CoordinatorState,
    success: bool,
) -> dict:
    """Extract telemetry and persist an updated ``model_profiles.yaml``."""
    outcome = build_run_outcome(config, state, success)
    return update_from_run(profiles_path, history_path, outcome)
