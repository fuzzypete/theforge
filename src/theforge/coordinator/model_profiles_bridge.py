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
from theforge.model_profiles_storage import (
    ReviewerAttempt,
    RoleAttempt,
    RunOutcome,
    update_from_run,
)
from theforge.reviewer_value import PlanReviewerValueSample, ReviewerValueSample

from .agent_identity import resolved_identity_for_result
from .state import CoordinatorState
from .trust_status import derive_trust_status, is_tainted


def _last_resolved_identity(results: object) -> str | None:
    """Resolved identity of the newest invocation in a list of AgentResults.

    The phase-level identity fold records one identity for a phase that may have
    run several invocations. The newest one that reported a served model is the
    truthful answer: it is what the phase most recently ran, and an earlier
    attempt that reported nothing must not blank it out.
    """
    if not isinstance(results, list):
        return None
    for result in reversed(results):
        resolved = resolved_identity_for_result(result)
        if resolved:
            return resolved
    return None


def _review_resolved_cycle_counts(state: CoordinatorState) -> dict[str, dict[str, int]]:
    """Map reviewer name → {served identity: cycles it served}.

    ``_extract_reviewers`` aggregates a reviewer's findings/cost across every
    cycle it participated in, so the population under ``review.runs`` is counted
    in CYCLES. One reviewer profile can be served by different concrete versions
    across those cycles, and attributing the whole aggregate to a single version
    would be a false claim about which model produced that evidence — the exact
    failure this story exists to end, one level down.

    Both this and :func:`_extract_reviewers` iterate ``review_cycle_metadata``
    and count the same ``meta.successful`` participants, so the cycle count and
    its version breakdown are derived from one source and cannot disagree. The
    served identity comes from ``meta.resolved_by_reviewer``, which the pool
    records against the invocation whose output the cycle actually *used* — a
    parse retry supersedes the initial output and can be served by a different
    version, and the per-invocation attempt log cannot express which of its
    entries survived.

    A reviewer whose cycle recorded no served identity is simply absent, so the
    breakdown under-claims rather than over-claims: it never sums past the cycle
    count it explains.
    """
    counts: dict[str, dict[str, int]] = {}
    for meta in state.review_cycle_metadata or []:
        resolved_by_reviewer = getattr(meta, "resolved_by_reviewer", None) or {}
        for name in list(meta.successful or []):
            served = resolved_by_reviewer.get(name)
            if not served:
                continue
            by_version = counts.setdefault(str(name), {})
            by_version[str(served)] = by_version.get(str(served), 0) + 1
    return counts


def _extract_reviewers(
    state: CoordinatorState,
) -> dict[str, tuple[int, int, float | None]]:
    """Per-reviewer ``(cycles, findings, cost)`` from cycle metadata + telemetry.

    Findings are the per-cycle aggregate (shared view across reviewers); cost
    is split evenly across successful reviewers in each cycle. Attribution is
    approximate but reasonable without per-reviewer telemetry.
    """
    out: dict[str, tuple[int, int, float | None]] = {}
    meta_list = state.review_cycle_metadata or []
    tele_list = state.review_iteration_telemetry or []
    for idx, meta in enumerate(meta_list):
        tele = tele_list[idx] if idx < len(tele_list) else None
        findings = sum(int(v) for v in tele.findings_by_severity.values()) if tele else 0
        # An unmeasured cycle cost stays None all the way into the profile fold
        # (_update_review handles it): splitting $0.00 across reviewers would
        # teach the router that a cost-unmeasured transport is free (#1992).
        cost = (float(tele.cost_usd) if tele.cost_usd is not None else None) if tele else 0.0
        participants = list(meta.successful or [])
        if not participants:
            continue
        per_head_cost = (cost / len(participants)) if cost is not None else None
        for name in participants:
            prev = out.get(name, (0, 0, 0.0))
            prev_cost = prev[2]
            new_cost = (
                None if per_head_cost is None or prev_cost is None else prev_cost + per_head_cost
            )
            out[name] = (prev[0] + 1, prev[1] + findings, new_cost)
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
                resolved_model=a.get("resolved_model"),
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
                # Stamped on the row at capture time by the pool that ran it, so
                # a reviewer served by different versions across attempts/cycles
                # keeps each sample attributed to the invocation it measures.
                resolved_model=v.get("resolved_model"),
            )
        )
    return samples


def _extract_code_reviewer_values(state: CoordinatorState) -> list[ReviewerValueSample]:
    """Convert per-code-reviewer value telemetry dicts into typed samples (#2156).

    The code-review counterpart of :func:`_extract_plan_reviewer_values`, over
    ``state.code_reviewer_value`` — the native per-(reviewer, review-cycle) capture
    written at code-review pool completion. Same pure-adapter shape, keyed by each
    reviewer's canonical identity, so the value signal lands under the same model
    entry the router looks it up by.
    """
    samples: list[ReviewerValueSample] = []
    for v in state.code_reviewer_value or []:
        if not isinstance(v, dict) or not v.get("reviewer"):
            continue
        samples.append(
            ReviewerValueSample(
                name=str(v["reviewer"]),
                complexity=str(v.get("complexity") or "medium"),
                unique_p1=int(v.get("unique_p1_count", 0)),
                total_p1=int(v.get("total_p1_count", 0)),
                latency_s=v.get("latency_s"),
                actual_model=v.get("actual_model"),
                provider=v.get("provider"),
                cli=v.get("cli"),
                # Stamped on the row at capture time by the pool that ran it, so
                # a reviewer served by different versions across attempts/cycles
                # keeps each sample attributed to the invocation it measures.
                resolved_model=v.get("resolved_model"),
            )
        )
    return samples


def _extract_preflight_attempts(state: CoordinatorState) -> list[RoleAttempt]:
    """Per-attempt preflight reliability from the native attempts list (#1489).

    ``state.preflight_result.raw['attempts']`` is the authoritative per-invocation
    record written by preflight_flow — one entry per primary/parse-retry/fallback
    call, each carrying its own model identity and ``completed`` outcome. Lifting
    every attempt (not just the final one) keeps the derived completion signal
    complete over attempts and attributes a failure to the model that produced it,
    so a failed primary followed by a healthy fallback never improves the primary's
    completion history. A cached preflight ran no model this story, so it records
    nothing.
    """
    if state.preflight_cached:
        return []
    result = state.preflight_result
    if result is None or not isinstance(getattr(result, "raw", None), dict):
        return []
    attempts_raw = result.raw.get("attempts")
    if not isinstance(attempts_raw, list):
        return []
    attempts: list[RoleAttempt] = []
    for a in attempts_raw:
        if not isinstance(a, dict) or not a.get("profile_name"):
            continue
        attempts.append(
            RoleAttempt(
                name=str(a["profile_name"]),
                completed=bool(a.get("completed")),
                actual_model=a.get("model"),
                provider=a.get("provider"),
                cli=a.get("cli"),
                cost_usd=a.get("cost_usd"),
                # Each attempt carries its own ledger (#2205), so a fallback
                # attempt is attributed to the version that served IT rather than
                # to whatever the final attempt resolved to.
                resolved_model=_ledger_resolved_identity(a.get("ledger")),
            )
        )
    return attempts


def _ledger_resolved_identity(ledger: object) -> str | None:
    """Read ``resolved_primary_identity.identity`` out of a recorded ledger block."""
    if not isinstance(ledger, dict):
        return None
    block = ledger.get("resolved_primary_identity")
    if not isinstance(block, dict):
        return None
    identity = str(block.get("identity") or block.get("raw") or "").strip()
    return identity or None


def _extract_planner_attempts(state: CoordinatorState, planner_profile) -> list[RoleAttempt]:
    """Per-attempt planner reliability (#1489).

    Complete over the reliability-relevant plan-generation invocations:

    - every ``plan_transport_retries`` entry is a transport failure of the planner
      model — a failed attempt that a later successful plan output must not hide;
    - every ``plan_results`` entry is a finished plan-generation invocation whose
      ``success`` is its completion outcome (review-driven regens are healthy
      invocations and count as successes; only transport/crash failures fail).

    All attributed to the planner profile identity: the transport retries are the
    same model retried, and in the common (non-escalation) path every plan_results
    entry is that same model. Mid-plan model escalation is rare and would attribute
    the escalated invocation to the base planner — a known minor imprecision far
    smaller than collapsing every attempt into one boolean.
    """
    name = planner_profile.name
    actual_model = getattr(planner_profile, "model", None)
    provider = getattr(planner_profile, "provider", None)
    cli = getattr(planner_profile, "cli", None)

    def _attempt(completed: bool, cost: float | None, resolved: str | None) -> RoleAttempt:
        return RoleAttempt(
            name=name,
            completed=completed,
            actual_model=actual_model,
            provider=provider,
            cli=cli,
            cost_usd=cost,
            resolved_model=resolved,
        )

    attempts: list[RoleAttempt] = []
    for retry in state.plan_transport_retries or []:
        if isinstance(retry, dict):
            # A transport failure produced no invocation identity to record: the
            # call never reached a model that could report one.
            attempts.append(_attempt(False, None, None))
    for r in state.plan_results or []:
        attempts.append(
            _attempt(
                bool(getattr(r, "success", False)),
                getattr(r, "cost_usd", None),
                resolved_identity_for_result(r),
            )
        )
    return attempts


def build_run_outcome(config: ForgeConfig, state: CoordinatorState, success: bool) -> RunOutcome:
    """Pure: assemble a :class:`RunOutcome` from coordinator state."""
    complexity = state.preflight_complexity or "medium"
    # ``dev_trace_count`` is monotonic (never reset on cycle boundaries) so it
    # captures total dev attempts across the run. ``budget.total_count`` is the
    # other such counter — both are incremented together at the single dev call
    # site (engine.py) and track the same quantity.
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
    # did. A harness-imposed ending (a deadline kill, a stuck-pattern terminate,
    # or the coordinator exhausting its iteration budget before submit) is
    # evidence about the budget or the harness, not about the model, so the
    # aggregator segregates these runs out of the capability statistics. Timeout
    # takes precedence when multiple termination signals happen to be set.
    #
    # All three read the sticky state flags rather than ``state.error_type``.
    # ``error_type`` also names the no-submit cut-off, but it is last-writer-wins
    # (dev_phase overwrites it with a later iteration's ``failure_code``), so the
    # classification the coordinator makes can be gone by the time this runs —
    # which is how a coordinator-ended run came to be recorded as a model failure
    # in the first place (#2921).
    dev_termination_cause = (
        "timeout"
        if state.dev_process_timeout_killed
        else (
            "stuck_pattern"
            if state.dev_process_stuck_terminated
            else ("max_iterations_no_submit" if state.dev_max_iterations_no_submit else None)
        )
    )
    # Taint marker (ADR-0006 clause 4, #1852): derive the run's aggregate
    # trust_status from its mechanically-computed trust checks — the same
    # derivation the audit writer records — and exclude a tainted run from every
    # router-consumed capability aggregate. A run with no implemented check stays
    # "unchecked" (admissible); only an affirmative failed check taints it.
    trust_status = derive_trust_status(state.trust_checks.values())
    run_tainted = is_tainted(trust_status)
    preflight_attempts = _extract_preflight_attempts(state)
    # Planner reliability (#1489): the planner that actually ran is the adaptive
    # decision's planner (plan_flow uses ``_adaptive.planner`` unless the role is an
    # explicit override, in which case that same profile is what ran). Recorded only
    # when planning was attempted this story, so a story that skipped planning folds
    # nothing.
    adaptive = getattr(state, "_adaptive_decision", None)
    planner_profile = getattr(adaptive, "planner", None) if adaptive is not None else None
    planner_model: str | None = None
    planner_actual_model: str | None = None
    planner_provider: str | None = None
    planner_cli: str | None = None
    planner_cost: float | None = None
    planner_attempts: list[RoleAttempt] = []
    if planner_profile is not None:
        planner_model = planner_profile.name
        planner_actual_model = getattr(planner_profile, "model", None)
        planner_provider = getattr(planner_profile, "provider", None)
        planner_cli = getattr(planner_profile, "cli", None)
        planner_cost = state.total_plan_cost_measured
        planner_attempts = _extract_planner_attempts(state, planner_profile)
        # No admissible attempt (planning skipped) ⇒ record no cost/identity either,
        # so the planner section is untouched for a story that never planned.
        if not planner_attempts:
            planner_model = None
            planner_cost = None
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
        # The concrete model that served the dev phase (#2226) — distinct from
        # ``dev_model``/``dev_actual_model``, which name what was selected.
        dev_resolved_model=_last_resolved_identity(state.dev_results),
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
        preflight_resolved_model=resolved_identity_for_result(state.preflight_result),
        preflight_cost_usd=state.total_preflight_cost_measured,
        preflight_attempts=preflight_attempts,
        planner_model=planner_model,
        planner_actual_model=planner_actual_model,
        planner_provider=planner_provider,
        planner_cli=planner_cli,
        planner_resolved_model=_last_resolved_identity(state.plan_results),
        planner_cost_usd=planner_cost,
        planner_attempts=planner_attempts,
        reviewers=_extract_reviewers(state),
        # Per-reviewer {served identity: cycles} (#2226). The findings/cost
        # aggregate is cycle-denominated, so its version breakdown has to be too.
        reviewer_resolved_cycles=_review_resolved_cycle_counts(state),
        reviewer_attempts=_extract_reviewer_attempts(state),
        plan_reviewer_values=_extract_plan_reviewer_values(state),
        code_reviewer_values=_extract_code_reviewer_values(state),
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
