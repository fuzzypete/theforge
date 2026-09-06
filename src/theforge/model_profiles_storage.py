"""Model capability profiles — accumulation of run outcomes into stored state.

Owns ``.forge/model_profiles.yaml`` as *stored state* (#2467): the run-outcome
carriers (:class:`RunOutcome`, :class:`ReviewerAttempt`, :class:`RoleAttempt`),
the YAML load/save surface, the pure fold of one finished run into a profile
(:func:`apply_run` and its ``_fold_*``/``_update_*`` helpers), the history
backfill, the operator reset path with its audit log, and the canonical-ID
migration of legacy keys with its ``_merge_*`` catalogue.

The signals routing consults are NOT here — they live in
:mod:`theforge.model_profiles_read_model`, which reads the stored shape this
module writes without importing it. A new signal is that module's change; a new
accumulator is this one's. Both share
:mod:`theforge.model_profiles_identity` for constants and identity resolution.

Schema on disk::

    models:
      <name>:
        dev:
          runs, success_rate, avg_iterations, avg_cost_usd
          by_complexity:
            small|medium|large: {runs, success_rate, avg_iterations, avg_cost_usd}
          by_domain:
            <tag>: {runs, success_rate, avg_iterations, avg_cost_usd}
        review:
          runs, avg_findings, avg_cost_usd
          _attempted_count, _completed_count, completion_rate  # reviewer attempts (#1388)
        preflight:
          runs, avg_cost_usd

Underscore-prefixed siblings (``_successes``, ``_iterations_sum``, ``_cost_sum``,
``_findings_sum``, ``_cost_unknown_runs``) are the running accumulators used to
update the derived fields in place. They are part of the persisted schema.

``_cost_unknown_runs`` counts runs whose transport could not measure cost (a
``None`` cost signal, e.g. a CLI runner that emits no token usage). Those runs
are *not* folded into ``_cost_sum`` and ``avg_cost_usd`` is averaged over
measured runs only (``runs - _cost_unknown_runs``). This keeps a genuinely free
($0.00 measured) run distinct from an unmeasured one so spend never silently
disappears from the ledger.

This module has a thin I/O surface (``load_profiles``/``save_profiles``); all
aggregation is pure. No LLM calls.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from theforge.model_profiles_identity import (
    ALIAS_DERIVED_KEY,
    CAPABILITY_RECENCY_WINDOW,
    COMPLEXITY_BANDS,
    DOMAIN_RECENCY_WINDOW,
    RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
    ROLES,
    _fold_resolved_model,
    _identity_metadata,
    _normalize_band,
    _resolve_agent_spec_for_profile_key,
    _success_count,
    canonical_id_from_identity,
)

log = logging.getLogger(__name__)


# ── Data carrier ──────────────────────────────────────────────────────────


@dataclass
class ReviewerAttempt:
    """One reviewer invocation outcome, recorded regardless of success (#1388).

    The signal scope is intentionally narrow: ``completed_parseable_verdict`` is
    the single load-bearing boolean — did the reviewer return something the
    coordinator could act on (a schema-valid verdict) at all? Transport failures,
    timeouts, parse failures, and crashes all record ``False`` before the phase
    continues, closing the survivorship-bias gap where a reviewer that failed
    silently evaporated from the profile and kept being re-selected.

    Identity fields (``actual_model``/``provider``/``cli``) let the completion
    telemetry be folded under the same canonical model ID the router looks a
    reviewer up by, so a reviewer's completion history and its findings/cost
    history live in one profile entry. ``outcome`` is a coarse category
    (``completed`` / ``transport_failure`` / ``timeout`` / ``parse_failure`` /
    ``crash`` / ``non_verdict`` / ``budget_overrun``) kept for audit legibility;
    only ``completed_parseable_verdict`` carries routing weight.
    """

    name: str
    completed_parseable_verdict: bool
    outcome: str = "completed"
    actual_model: str | None = None
    provider: str | None = None
    cli: str | None = None
    failure_reason: str | None = None
    # The concrete identity that actually served this attempt (#2226). Distinct
    # from the identity fields above, which describe what was *selected*: when
    # the selection names a family alias, the vendor picks the version and only
    # this field records which one. ``None`` when the transport reported no
    # resolved identity, which is not the same as "the same as configured".
    resolved_model: str | None = None


@dataclass
class RoleAttempt:
    """One non-dev single-model invocation outcome, recorded per attempt (#1489).

    The preflight/planner analog of :class:`ReviewerAttempt`. A role that retries
    (preflight parse-retry) or falls back to a different model (preflight fallback)
    or is retried at transport (planner) produces *several* native invocations in a
    single run; each is one ``RoleAttempt`` so the derived reliability rate is
    complete over attempts (ADR-0006 clause 2) and each attempt is attributed to the
    model that actually ran it — a failed primary followed by a healthy fallback
    must not improve the primary's completion history.

    ``completed`` is the single load-bearing boolean: did this invocation return a
    usable, parseable result? Identity fields key the fold under the same canonical
    model ID the router looks the role up by.
    """

    name: str
    completed: bool
    actual_model: str | None = None
    provider: str | None = None
    cli: str | None = None
    cost_usd: float | None = None
    # Concrete identity that served this attempt (#2226) — see
    # :attr:`ReviewerAttempt.resolved_model`.
    resolved_model: str | None = None


@dataclass
class RunOutcome:
    """Everything ``update_from_run`` needs about one coordinator run.

    ``reviewers`` maps each reviewer profile name to a
    ``(cycles_participated, findings_observed, cost_usd)`` triple attributed to
    that reviewer. Attribution at the per-reviewer level is approximate —
    reviewers see the same code so findings_observed is the full per-cycle
    aggregate and cost is divided evenly across successful reviewers in each
    cycle.
    """

    complexity: str
    dev_model: str
    dev_success: bool
    dev_iterations: int
    dev_cost_usd: float | None  # None = the transport could not measure cost
    complexity_score: int | None = None
    # Observed wall-clock of the completed dev phase (seconds); None when unknown
    # or when the run was harness-killed (a kill's true duration is unknown).
    dev_duration_s: float | None = None
    # True when the harness terminated the dev phase at its timeout limit. Such a
    # run is a censored observation: its duration only bounds the timeout from
    # below and must never lower learned duration.
    dev_timeout_killed: bool = False
    # The granted per-story timeout (seconds) at which a killed run was terminated.
    dev_timeout_limit_s: int | None = None
    # Why the harness ended the dev process, if it did: ``"timeout"`` (deadline
    # kill), ``"stuck_pattern"`` (stuck-pattern terminate), or
    # ``"max_iterations_no_submit"`` (coordinator iteration limit); ``None`` for
    # a run the model itself finished (success or genuine failure). A
    # harness-imposed ending is evidence about the budget or the harness, not the
    # model, so the aggregator segregates these runs into a visible
    # ``harness_terminated`` sub-dict and keeps them out of
    # success_rate/avg_iterations/avg_cost stats.
    dev_termination_cause: str | None = None
    preflight_model: str | None = None
    dev_actual_model: str | None = None
    dev_provider: str | None = None
    dev_cli: str | None = None
    # The concrete model that actually served the dev phase (#2226). When
    # ``dev_model``/``dev_actual_model`` name a family alias, the vendor chooses
    # the version at invocation time and this is the only field that records
    # which one. Folding it beside the configured identity is what makes an
    # alias's accumulated evidence attributable to the versions that produced
    # it, instead of describing a subject that can move underneath the key.
    dev_resolved_model: str | None = None
    preflight_actual_model: str | None = None
    preflight_provider: str | None = None
    preflight_cli: str | None = None
    # Concrete identity that served the preflight phase (#2226).
    preflight_resolved_model: str | None = None
    preflight_cost_usd: float | None = None  # None = cost unmeasured
    # Preflight reliability, one entry per native preflight invocation (#1489).
    # Unlike a single collapsed boolean, this carries a ``RoleAttempt`` for every
    # attempt the phase ran — the primary, any same-profile parse-retry, and a
    # fallback model — each attributed to the model that actually ran it, so a
    # failed primary followed by a healthy fallback records a failure for the
    # primary and a success for the fallback (never a spurious primary success).
    # Empty ⇒ no admissible preflight attempt to record (cached / preflight
    # skipped), so the completion counters are left untouched. Mirrors the reviewer
    # attempt-completion signal (#1388).
    preflight_attempts: list[RoleAttempt] = field(default_factory=list)
    # Planner role identity + reliability (#1489). ``planner_model``/cost feed the
    # per-phase cost aggregate; ``planner_attempts`` carries one ``RoleAttempt`` per
    # native plan-generation invocation — including transport-retry failures that a
    # later successful plan output would otherwise hide — so the completion signal
    # is complete over attempts. Empty ⇒ planning did not run (or produced no
    # attempt) this run, so nothing is folded.
    planner_model: str | None = None
    planner_actual_model: str | None = None
    planner_provider: str | None = None
    planner_cli: str | None = None
    # Concrete identity that served the planner phase (#2226).
    planner_resolved_model: str | None = None
    planner_cost_usd: float | None = None  # None = cost unmeasured
    planner_attempts: list[RoleAttempt] = field(default_factory=list)
    reviewers: dict[str, tuple[int, int, float | None]] = field(default_factory=dict)
    # Per-reviewer ``{served identity: cycles it served}`` (#2226). ``reviewers``
    # above is cycle-denominated and aggregates across every cycle a reviewer
    # took part in; one profile can be served by different concrete versions
    # across those cycles, so a single served identity cannot describe the
    # aggregate. Absent/short entries under-claim rather than over-claim: the
    # breakdown never sums past the cycle count it explains.
    reviewer_resolved_cycles: dict[str, dict[str, int]] = field(default_factory=dict)
    # Every reviewer invocation this run, including failures (#1388). Unlike
    # ``reviewers`` (which is survivorship-biased — only reviewers that returned a
    # parseable verdict appear), this list carries an entry for each attempt so the
    # derived completion rate is complete over attempts. Folded into the review
    # section's ``_attempted_count`` / ``_completed_count`` / ``completion_rate``.
    reviewer_attempts: list[ReviewerAttempt] = field(default_factory=list)
    # Per-plan-reviewer mechanical value samples for this run (#1443): one
    # :class:`theforge.reviewer_value.PlanReviewerValueSample` per (reviewer, plan-
    # review pool attempt) that raised ≥1 P1. Folded into the ``plan_review_value``
    # profile section (uniqueness rate + latency-per-P1 rings) under the same taint
    # gate as every other capability aggregate. Typed as ``list`` to avoid an
    # import cycle; :func:`apply_run` reads each sample's attributes.
    plan_reviewer_values: list = field(default_factory=list)
    # Per-code-reviewer mechanical value samples for this run (#2156): one
    # :class:`theforge.reviewer_value.ReviewerValueSample` per (reviewer, review
    # cycle) that raised ≥1 blocking finding. Folded into the SEPARATE
    # ``code_review_value`` profile section, under the same taint gate, so the two
    # reviewer phases never share a history.
    code_reviewer_values: list = field(default_factory=list)
    # Domain tags for this run (issue #155), from the fixed taxonomy recorded by
    # preflight. The dev outcome is folded into a per-domain slice for each tag so
    # per-domain success rate can be aggregated deterministically. Empty = the run
    # had no domain tags and contributes to no domain slice.
    domains: list[str] = field(default_factory=list)
    # Taint marker (ADR-0006 clause 4, #1851/#1852 seam). True when the run failed
    # its own trust checks (e.g. a reviewer that reviewed a stale checkout). A
    # tainted run "doesn't teach": it is excluded from EVERY router-consumed
    # capability aggregate this run would feed — the top-level dev bucket, the
    # per-complexity and per-score bands, each per-domain slice, and the review
    # and preflight sections — and tallied visibly under ``tainted_runs`` in each
    # instead, never deleted. Derived from the run's ``trust_status`` (a
    # ``tainted`` status is the only affirmative exclusion; missing/trusted/
    # unchecked are all admissible), so this defaults False (default-admissible
    # per clause 4).
    dev_tainted: bool = False


# ── I/O ───────────────────────────────────────────────────────────────────


def load_profiles(path: Path) -> dict[str, Any]:
    """Read ``model_profiles.yaml``; return ``{"models": {}}`` if absent/bad."""
    if not path.exists():
        return {"models": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to load %s: %s", path, exc)
        return {"models": {}}
    if not isinstance(data, dict):
        return {"models": {}}
    if not isinstance(data.get("models"), dict):
        data["models"] = {}
    return data


def save_profiles(path: Path, data: dict[str, Any]) -> None:
    """Write profiles to disk; best-effort (warns but doesn't raise)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to write %s: %s", path, exc)


def load_reset_history(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read the profile reset audit log; return an empty skeleton if absent/bad."""
    if not path.exists():
        return {"resets": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to load reset history %s: %s", path, exc)
        return {"resets": []}
    if not isinstance(data, dict):
        return {"resets": []}
    resets = data.get("resets")
    if not isinstance(resets, list):
        data["resets"] = []
    return data


def save_reset_history(path: Path, data: dict[str, list[dict[str, Any]]]) -> None:
    """Write the reset audit log to disk; best-effort (warns but doesn't raise)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to write reset history %s: %s", path, exc)


def _ensure_model(
    data: dict,
    name: str,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> dict:
    models = data.setdefault("models", {})
    canonical = canonical_id_from_identity(actual_model=actual_model, provider=provider, cli=cli)
    storage_key = canonical or name
    entry = models.setdefault(storage_key, {})
    metadata = _identity_metadata(actual_model=actual_model, provider=provider, cli=cli)
    if metadata and not isinstance(entry.get("_identity"), dict):
        entry["_identity"] = metadata
    return entry


def _entry_model_label(entry: dict) -> str:
    ident = entry.get("_identity")
    if isinstance(ident, dict):
        return str(ident.get("model") or "?")
    return "?"


def _fold_cost(bucket: dict, cost_usd: float | None, *, unknown_count: int = 1) -> None:
    """Fold one run's cost into ``bucket``; needs ``bucket['runs']`` already set.

    ``cost_usd is None`` means the transport could not measure cost: the run is
    tallied in ``_cost_unknown_runs`` (never added to ``_cost_sum``) and
    ``avg_cost_usd`` is averaged over MEASURED runs only. ``unknown_count`` lets
    the review path attribute more than one unmeasured run in a single call.
    """
    cost_sum = float(bucket.get("_cost_sum", 0.0))
    unknown = int(bucket.get("_cost_unknown_runs", 0))
    if cost_usd is None:
        unknown += unknown_count
    else:
        cost_sum += float(cost_usd)
    bucket["_cost_sum"] = cost_sum
    bucket["_cost_unknown_runs"] = unknown
    measured = int(bucket.get("runs", 0)) - unknown
    bucket["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0


def _fold_duration(
    bucket: dict,
    duration_s: float | None,
    killed: bool,
    limit_s: int | None,
) -> None:
    """Fold one dev run's wall-clock signal into ``bucket``.

    Two disjoint cases, kept segregated so harness-killed runs never contaminate
    learned completed-run duration (see #1763):

    - A COMPLETED run (``not killed``) with a known ``duration_s`` accumulates
      into ``_duration_sum``/``_duration_runs`` (→ ``avg_duration_s``) and raises
      ``max_duration_s``.
    - A harness-KILLED run only raises ``max_killed_timeout_s`` (the limit that
      terminated it). Its true duration is unknown and only bounds the timeout
      from below — it never touches the duration accumulators, so a kill can
      never lower learned duration.
    """
    if not killed and duration_s is not None:
        dur_sum = float(bucket.get("_duration_sum", 0.0)) + float(duration_s)
        dur_runs = int(bucket.get("_duration_runs", 0)) + 1
        bucket["_duration_sum"] = dur_sum
        bucket["_duration_runs"] = dur_runs
        bucket["avg_duration_s"] = round(dur_sum / dur_runs, 4) if dur_runs > 0 else 0.0
        bucket["max_duration_s"] = max(float(bucket.get("max_duration_s", 0.0)), float(duration_s))
    if killed and limit_s is not None:
        bucket["max_killed_timeout_s"] = max(
            float(bucket.get("max_killed_timeout_s", 0.0)), float(limit_s)
        )


def _fold_harness_terminated(bucket: dict, cause: str | None, cost_usd: float | None) -> None:
    """Record a harness-imposed termination in ``bucket['harness_terminated']``.

    Harness-terminated runs (deadline kill, stuck-pattern terminate, coordinator
    iteration budget exhausted with submit never called) are kept out
    of the capability accumulators (``runs``/``_successes``/``_iterations_sum`` →
    ``success_rate``/``avg_iterations``) entirely so the orchestrator can never
    steer away from a model because it starved the run (#1763). They are instead
    tallied here — visibly, with a per-cause breakdown — and their spend is folded
    against this sub-dict so killed-run cost stays on the ledger without diluting
    the capability ``avg_cost_usd``.
    """
    ht = bucket.setdefault("harness_terminated", {})
    ht["runs"] = int(ht.get("runs", 0)) + 1
    by_cause = ht.setdefault("by_cause", {})
    if cause is not None:
        by_cause[cause] = int(by_cause.get(cause, 0)) + 1
    _fold_cost(ht, cost_usd)


def _fold_dev_bucket(
    bucket: dict,
    success: bool,
    iterations: int,
    cost_usd: float | None,
    duration_s: float | None,
    timeout_killed: bool,
    timeout_limit_s: int | None,
    termination_cause: str | None,
) -> None:
    """Fold one dev run into a single dev bucket (top-level / band / score).

    A harness-terminated run bypasses the capability accumulators and cost fold
    entirely (recorded via :func:`_fold_harness_terminated`); only the duration
    fold still runs with ``duration_s=None`` so a timeout kill can raise
    ``max_killed_timeout_s`` without touching completed-run duration stats.
    """
    if termination_cause is not None:
        _fold_harness_terminated(bucket, termination_cause, cost_usd)
        # duration_s=None: skip the completed-duration branch for BOTH timeout and
        # stuck; the killed/limit branch still raises max_killed_timeout_s for a
        # timeout (timeout_killed=True carries the kill floor).
        _fold_duration(bucket, None, timeout_killed, timeout_limit_s)
        return
    runs = int(bucket.get("runs", 0)) + 1
    successes = int(bucket.get("_successes", 0)) + (1 if success else 0)
    iter_sum = float(bucket.get("_iterations_sum", 0.0)) + float(iterations)
    bucket["runs"] = runs
    bucket["_successes"] = successes
    bucket["_iterations_sum"] = iter_sum
    bucket["success_rate"] = round(successes / runs, 4)
    bucket["avg_iterations"] = round(iter_sum / runs, 4)
    _fold_cost(bucket, cost_usd)
    _fold_duration(bucket, duration_s, timeout_killed, timeout_limit_s)


def _fold_dev_capability(
    bucket: dict,
    success: bool,
    iterations: int,
    cost_usd: float | None,
    duration_s: float | None,
    timeout_killed: bool,
    timeout_limit_s: int | None,
    termination_cause: str | None,
    tainted: bool,
    resolved_model: str | None = None,
) -> None:
    """Fold one dev run into a capability bucket, excluding tainted runs.

    Wraps :func:`_fold_dev_bucket` with the ADR-0006 clause-4 taint gate so the
    top-level dev bucket and the per-complexity / per-score bands exclude runs
    that failed their own trust checks exactly like the per-domain slice already
    does. A ``tainted`` run "doesn't teach": it is kept out of the capability
    accumulators (``runs``/``_successes``/…) and tallied under ``tainted_runs``
    so the exclusion stays visible in the record, never silently dropped.
    """
    _fold_resolved_model(bucket, resolved_model, success=success, tainted=tainted)
    if tainted:
        bucket["tainted_runs"] = int(bucket.get("tainted_runs", 0)) + 1
        return
    _fold_dev_bucket(
        bucket,
        success,
        iterations,
        cost_usd,
        duration_s,
        timeout_killed,
        timeout_limit_s,
        termination_cause,
    )
    # Recency ring (#1392, ADR-0006 clause 2.4): every genuine completed run
    # appends its outcome to a bounded ``_recent`` ring so the routing-admissible
    # ``weighted`` rate can decay stale history out of relevance instead of
    # anchoring a lifetime cumulative average. Harness-terminated runs never
    # contribute capability data — mirror that here so the ring population matches
    # the runs/_successes population exactly.
    if termination_cause is None:
        recent = bucket.setdefault("_recent", [])
        recent.append(1 if success else 0)
        if len(recent) > CAPABILITY_RECENCY_WINDOW:
            del recent[: len(recent) - CAPABILITY_RECENCY_WINDOW]


def _fold_domain_slice(
    bucket: dict,
    success: bool,
    iterations: int,
    cost_usd: float | None,
    duration_s: float | None,
    timeout_killed: bool,
    timeout_limit_s: int | None,
    termination_cause: str | None,
    tainted: bool,
    resolved_model: str | None = None,
) -> None:
    """Fold one dev run into a per-domain slice with recency + taint handling.

    Extends :func:`_fold_dev_bucket` with the two gates ADR-0006 requires before
    a per-domain rate may carry routing weight (issue #155):

    - **Taint exclusion (clause 4):** a ``tainted`` run "doesn't teach" — it is
      kept out of the capability accumulators entirely and tallied under
      ``tainted_runs`` so the exclusion is visible in the record, never silently
      dropped.
    - **Recency window (clause 2.4):** every genuine completed run (not tainted,
      not harness-terminated) appends its outcome to a bounded ``_recent`` ring
      (:data:`DOMAIN_RECENCY_WINDOW`). The routing-admissible rate is computed
      from this window, so stale history decays out of relevance instead of
      permanently weighting a lifetime cumulative average.
    """
    _fold_resolved_model(bucket, resolved_model, success=success, tainted=tainted)
    if tainted:
        bucket["tainted_runs"] = int(bucket.get("tainted_runs", 0)) + 1
        return
    _fold_dev_bucket(
        bucket,
        success,
        iterations,
        cost_usd,
        duration_s,
        timeout_killed,
        timeout_limit_s,
        termination_cause,
    )
    # Only genuine completed runs enter the recency window. Harness-terminated
    # runs (termination_cause set) never contribute capability data — mirror that
    # here so the windowed rate matches the runs/_successes population exactly.
    if termination_cause is None:
        recent = bucket.setdefault("_recent", [])
        recent.append(1 if success else 0)
        if len(recent) > DOMAIN_RECENCY_WINDOW:
            del recent[: len(recent) - DOMAIN_RECENCY_WINDOW]


def _update_dev(
    entry: dict,
    complexity: str,
    success: bool,
    iterations: int,
    cost_usd: float | None,
    complexity_score: int | None = None,
    duration_s: float | None = None,
    timeout_killed: bool = False,
    timeout_limit_s: int | None = None,
    termination_cause: str | None = None,
    domains: list[str] | None = None,
    tainted: bool = False,
    resolved_model: str | None = None,
) -> None:
    # A tainted run is excluded from every capability accumulator (ADR-0006
    # clause 4), so it never records a cost either — suppress the cost-unmeasured
    # warning that would otherwise fire for a run whose cost is intentionally
    # not folded.
    if cost_usd is None and not tainted:
        log.warning(
            "[model_profiles] Dev run recorded cost-unmeasured (NOT $0.00): "
            "model=%s complexity=%s — transport reported no cost.",
            _entry_model_label(entry),
            complexity,
        )
    dev = entry.setdefault("dev", {})
    _fold_dev_capability(
        dev,
        success,
        iterations,
        cost_usd,
        duration_s,
        timeout_killed,
        timeout_limit_s,
        termination_cause,
        tainted,
        resolved_model,
    )

    by = dev.setdefault("by_complexity", {})
    bc = by.setdefault(complexity, {})
    _fold_dev_capability(
        bc,
        success,
        iterations,
        cost_usd,
        duration_s,
        timeout_killed,
        timeout_limit_s,
        termination_cause,
        tainted,
        resolved_model,
    )

    if complexity_score is not None:
        score_key = str(int(complexity_score))
        by_score = dev.setdefault("by_complexity_score", {})
        sc = by_score.setdefault(score_key, {})
        _fold_dev_capability(
            sc,
            success,
            iterations,
            cost_usd,
            duration_s,
            timeout_killed,
            timeout_limit_s,
            termination_cause,
            tainted,
            resolved_model,
        )

    # Per-domain slice (issue #155): fold this run into a bucket for each domain
    # tag it carried. A run tagged [api, database] folds identically into both
    # the "api" and "database" buckets, so per-domain success rate is a real
    # aggregation over authoritative run telemetry (ADR-0006 clause B), not a
    # summary or profile-only derivation. The slice fold applies the recency
    # window and taint exclusion the domain routing signal reads through.
    for domain in domains or []:
        by_domain = dev.setdefault("by_domain", {})
        dd = by_domain.setdefault(domain, {})
        _fold_domain_slice(
            dd,
            success,
            iterations,
            cost_usd,
            duration_s,
            timeout_killed,
            timeout_limit_s,
            termination_cause,
            tainted,
            resolved_model,
        )
        # Per-(domain, band) cross slice. by_domain and by_complexity are otherwise
        # independent marginals; the challenger-sampling router (#325) needs a
        # routing key scoped to BOTH domain and complexity band at once, so fold a
        # nested band bucket inside the domain slice. Same recency/taint gates as
        # the flat domain slice — this is the truthful per-(phase, domain, band)
        # aggregate, not a marginal that collapses one axis.
        dd_by_complexity = dd.setdefault("by_complexity", {})
        dd_bc = dd_by_complexity.setdefault(complexity, {})
        _fold_domain_slice(
            dd_bc,
            success,
            iterations,
            cost_usd,
            duration_s,
            timeout_killed,
            timeout_limit_s,
            termination_cause,
            tainted,
            resolved_model,
        )


def _update_review(
    entry: dict,
    cycles: int,
    findings: int,
    cost_usd: float | None,
    tainted: bool = False,
    resolved_cycles: dict[str, int] | None = None,
) -> None:
    if cycles <= 0:
        return
    rev = entry.setdefault("review", {})
    # Taint gate (ADR-0006 clause 4): a tainted run "doesn't teach", so its
    # reviewer cycles are kept out of the completion-rate / findings / cost
    # aggregates and tallied under ``tainted_runs`` instead.
    if tainted:
        rev["tainted_runs"] = int(rev.get("tainted_runs", 0)) + cycles
        return
    # Version attribution (#2226): the findings/cost population under this
    # section is attributed to the concrete models that served it, so an alias's
    # review history can be told apart from one model's review history.
    #
    # This breaks down ``runs``, which the section counts in CYCLES, so the input
    # is a per-version CYCLE COUNT rather than one identity: a reviewer served by
    # version A for two cycles and version B for one is three cycles of evidence
    # about two models, and folding all three under either one would be a false
    # claim about which model produced them.
    #
    # It uses the runs-scoped key, not the attempt-scoped one the completion fold
    # writes. The two populations are folded from the same run at different call
    # sites; sharing one breakdown would count each invocation twice and report
    # more attributed version observations than either counter contains.
    for served, served_cycles in (resolved_cycles or {}).items():
        _fold_resolved_model(rev, served, success=None, tainted=False, count=served_cycles)
    if cost_usd is None:
        log.warning(
            "[model_profiles] Review run recorded cost-unmeasured (NOT $0.00): "
            "model=%s cycles=%d — transport reported no cost.",
            _entry_model_label(entry),
            cycles,
        )
    runs = int(rev.get("runs", 0)) + cycles
    find_sum = float(rev.get("_findings_sum", 0.0)) + float(findings)
    rev["runs"] = runs
    rev["_findings_sum"] = find_sum
    rev["avg_findings"] = round(find_sum / runs, 4)
    _fold_cost(rev, cost_usd, unknown_count=cycles)


def _fold_completion_counters(section: dict, completed: bool) -> None:
    """Fold one attempt-completion outcome into ``section``'s completion counters.

    The shared body of every role's attempt-completion signal (reviewer #1388,
    preflight/planner #1489). Records ``_attempted_count`` (every invocation) and
    ``_completed_count`` (invocations that returned a usable, parseable result) as
    running totals so ``completion_rate`` is always recomputable from the two
    authoritative counters. A bounded ``_completion_recent`` ring of ``0/1``
    outcomes feeds the shared recency-weighting mechanism (#1392) at read time,
    mirroring the dev bucket's ``_recent`` ring.

    Callers own the taint gate: a tainted run is tallied under ``tainted_runs`` and
    never reaches this helper (ADR-0006 clause 4), so this only ever folds an
    admissible outcome.
    """
    attempted = int(section.get("_attempted_count", 0)) + 1
    completed_count = int(section.get("_completed_count", 0)) + (1 if completed else 0)
    section["_attempted_count"] = attempted
    section["_completed_count"] = completed_count
    section["completion_rate"] = round(completed_count / attempted, 4) if attempted > 0 else 0.0
    ring = section.setdefault("_completion_recent", [])
    if not isinstance(ring, list):
        ring = []
        section["_completion_recent"] = ring
    ring.append(1 if completed else 0)
    if len(ring) > CAPABILITY_RECENCY_WINDOW:
        del ring[:-CAPABILITY_RECENCY_WINDOW]


def _update_review_completion(
    entry: dict,
    completed: bool,
    tainted: bool = False,
    resolved_model: str | None = None,
) -> None:
    """Fold one reviewer attempt-completion outcome into the review section (#1388).

    This is a separate fold from :func:`_update_review` (findings/cost) because it
    must record *failed* attempts too — a reviewer that timed out or emitted
    unparseable output never reaches the findings/cost path.

    Taint gate (ADR-0006 clause 4): a tainted run "doesn't teach", so its reviewer
    attempts are kept out of the completion aggregate and tallied under
    ``tainted_runs`` instead — never deleted, so the exclusion stays visible.
    """
    rev = entry.setdefault("review", {})
    # Version attribution (#2226) against ``_attempted_count`` — the counter this
    # fold moves and the one :func:`get_review_signal` divides by. Kept separate
    # from the cycles-denominated breakdown :func:`_update_review` writes.
    _fold_resolved_model(
        rev,
        resolved_model,
        success=completed,
        tainted=tainted,
        key=RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
    )
    if tainted:
        rev["tainted_runs"] = int(rev.get("tainted_runs", 0)) + 1
        return
    _fold_completion_counters(rev, completed)


def _update_role_completion(
    entry: dict,
    role: str,
    completed: bool,
    tainted: bool = False,
    resolved_model: str | None = None,
) -> None:
    """Fold one non-dev single-model invocation's completion into ``entry[role]``.

    The per-attempt reliability fold for preflight/planner (#1489), invoked once per
    :class:`RoleAttempt` so retries and fallbacks each land under the model that ran
    them. Decoupled from the phase-level runs/cost fold (:func:`_update_preflight` /
    :func:`_update_planner`): the completion signal is complete over *attempts*
    while cost is aggregated per *phase*, so the two counters are independent.

    Taint gate (ADR-0006 clause 4): a tainted run "doesn't teach", so its attempt is
    kept out of the completion aggregate. The ``tainted_runs`` tally is owned by the
    per-phase fold (:func:`_update_preflight` / :func:`_update_planner`) so a single
    tainted run is counted once, not once per attempt — this fold only skips.
    """
    if tainted:
        return
    section = entry.setdefault(role, {})
    # Version attribution (#2226) against ``_attempted_count``, the counter this
    # fold moves and the one :func:`get_role_reliability_signal` divides by. The
    # phase-level fold (_update_preflight / _update_planner) writes the
    # runs-denominated breakdown separately; one shared key would double-count
    # every invocation that produces both.
    #
    # No tainted tally here: this fold "only skips" (the per-phase fold owns the
    # single tainted-run count), so recording one would count a tainted run once
    # per attempt.
    _fold_resolved_model(
        section,
        resolved_model,
        success=completed,
        tainted=False,
        key=RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
    )
    _fold_completion_counters(section, completed)


def _update_preflight(
    entry: dict,
    cost_usd: float | None,
    tainted: bool = False,
    resolved_model: str | None = None,
) -> None:
    pf = entry.setdefault("preflight", {})
    # Taint gate (ADR-0006 clause 4): a tainted run is excluded from the preflight
    # capability aggregate and tallied under ``tainted_runs`` instead. This
    # per-phase fold owns the single tainted-run tally (the per-attempt completion
    # fold only skips) so one tainted run is counted once.
    if tainted:
        pf["tainted_runs"] = int(pf.get("tainted_runs", 0)) + 1
        return
    # Version attribution (#2226) against ``runs`` — the phase counter. The
    # per-attempt completion fold writes its own attempt-denominated breakdown.
    _fold_resolved_model(pf, resolved_model, success=None, tainted=False)
    if cost_usd is None:
        log.warning(
            "[model_profiles] Preflight run recorded cost-unmeasured (NOT $0.00): "
            "model=%s — transport reported no cost.",
            _entry_model_label(entry),
        )
    runs = int(pf.get("runs", 0)) + 1
    pf["runs"] = runs
    _fold_cost(pf, cost_usd)


def _update_planner(
    entry: dict,
    cost_usd: float | None,
    tainted: bool = False,
    resolved_model: str | None = None,
) -> None:
    """Fold one planner phase's runs + cost into the ``planner`` section (#1489).

    Parallel to :func:`_update_preflight`. Per-attempt reliability is folded
    separately by :func:`_update_role_completion`. Same taint gate as every other
    capability aggregate (ADR-0006 clause 4).
    """
    pl = entry.setdefault("planner", {})
    # Per-phase taint tally (owns the single count; the per-attempt fold only skips).
    if tainted:
        pl["tainted_runs"] = int(pl.get("tainted_runs", 0)) + 1
        return
    # Version attribution (#2226) against ``runs`` — see :func:`_update_preflight`.
    _fold_resolved_model(pl, resolved_model, success=None, tainted=False)
    if cost_usd is None:
        log.warning(
            "[model_profiles] Planner run recorded cost-unmeasured (NOT $0.00): "
            "model=%s — transport reported no cost.",
            _entry_model_label(entry),
        )
    runs = int(pl.get("runs", 0)) + 1
    pl["runs"] = runs
    _fold_cost(pl, cost_usd)


def _zero_dev_bucket(bucket: dict) -> None:
    bucket["runs"] = 0
    bucket["_successes"] = 0
    bucket["_iterations_sum"] = 0.0
    bucket["_cost_sum"] = 0.0
    bucket["_cost_unknown_runs"] = 0
    bucket["success_rate"] = 0.0
    bucket["avg_iterations"] = 0.0
    bucket["avg_cost_usd"] = 0.0
    bucket["_duration_sum"] = 0.0
    bucket["_duration_runs"] = 0
    bucket["avg_duration_s"] = 0.0
    bucket["max_duration_s"] = 0.0
    bucket["max_killed_timeout_s"] = 0.0
    bucket["tainted_runs"] = 0
    bucket["_recent"] = []
    bucket["harness_terminated"] = {
        "runs": 0,
        "by_cause": {},
        "_cost_sum": 0.0,
        "_cost_unknown_runs": 0,
        "avg_cost_usd": 0.0,
    }


def _zero_review_section(section: dict) -> None:
    section["runs"] = 0
    section["_findings_sum"] = 0.0
    section["_cost_sum"] = 0.0
    section["_cost_unknown_runs"] = 0
    section["avg_findings"] = 0.0
    section["avg_cost_usd"] = 0.0
    section["_attempted_count"] = 0
    section["_completed_count"] = 0
    section["completion_rate"] = 0.0
    section["_completion_recent"] = []


def _zero_preflight_section(section: dict) -> None:
    section["runs"] = 0
    section["_cost_sum"] = 0.0
    section["_cost_unknown_runs"] = 0
    section["avg_cost_usd"] = 0.0
    # Reliability completion counters (#1489), mirroring the review section.
    section["_attempted_count"] = 0
    section["_completed_count"] = 0
    section["completion_rate"] = 0.0
    section["_completion_recent"] = []


def _zero_planner_section(section: dict) -> None:
    section["runs"] = 0
    section["_cost_sum"] = 0.0
    section["_cost_unknown_runs"] = 0
    section["avg_cost_usd"] = 0.0
    section["_attempted_count"] = 0
    section["_completed_count"] = 0
    section["completion_rate"] = 0.0
    section["_completion_recent"] = []


def _recompute_dev_section(section: dict) -> None:
    by = section.setdefault("by_complexity", {})
    runs = 0
    successes = 0
    iterations = 0.0
    cost = 0.0
    cost_unknown = 0
    duration_sum = 0.0
    duration_runs = 0
    max_duration = 0.0
    max_killed_timeout = 0.0
    ht_runs = 0
    ht_by_cause: dict[str, int] = {}
    ht_cost_sum = 0.0
    ht_cost_unknown = 0
    tainted_runs = 0
    recent: list[int] = []
    for band in COMPLEXITY_BANDS:
        bucket = by.setdefault(band, {})
        runs += int(bucket.get("runs", 0))
        tainted_runs += int(bucket.get("tainted_runs", 0))
        band_recent = bucket.get("_recent")
        if isinstance(band_recent, list):
            recent.extend(int(v) for v in band_recent)
        successes += int(bucket.get("_successes", 0))
        iterations += float(bucket.get("_iterations_sum", 0.0))
        cost += float(bucket.get("_cost_sum", 0.0))
        cost_unknown += int(bucket.get("_cost_unknown_runs", 0))
        duration_sum += float(bucket.get("_duration_sum", 0.0))
        duration_runs += int(bucket.get("_duration_runs", 0))
        max_duration = max(max_duration, float(bucket.get("max_duration_s", 0.0)))
        max_killed_timeout = max(
            max_killed_timeout, float(bucket.get("max_killed_timeout_s", 0.0))
        )
        ht = bucket.get("harness_terminated") or {}
        if isinstance(ht, dict):
            ht_runs += int(ht.get("runs", 0))
            ht_cost_sum += float(ht.get("_cost_sum", 0.0))
            ht_cost_unknown += int(ht.get("_cost_unknown_runs", 0))
            for cause, count in (ht.get("by_cause") or {}).items():
                ht_by_cause[cause] = ht_by_cause.get(cause, 0) + int(count)
    section["runs"] = runs
    section["_successes"] = successes
    section["_iterations_sum"] = iterations
    section["_cost_sum"] = cost
    section["_cost_unknown_runs"] = cost_unknown
    section["success_rate"] = round(successes / runs, 4) if runs > 0 else 0.0
    section["avg_iterations"] = round(iterations / runs, 4) if runs > 0 else 0.0
    measured = runs - cost_unknown
    section["avg_cost_usd"] = round(cost / measured, 6) if measured > 0 else 0.0
    section["_duration_sum"] = duration_sum
    section["_duration_runs"] = duration_runs
    section["avg_duration_s"] = (
        round(duration_sum / duration_runs, 4) if duration_runs > 0 else 0.0
    )
    section["max_duration_s"] = max_duration
    section["max_killed_timeout_s"] = max_killed_timeout
    section["tainted_runs"] = tainted_runs
    # Rebuild the top-level recency ring from the surviving band rings. Cross-band
    # chronology is lost (bands are concatenated in fixed order), but this only
    # runs on an operator reset — a deliberate discard of history — so a best-
    # effort windowed view is acceptable there; live folding keeps the top-level
    # ring chronological.
    section["_recent"] = recent[-CAPABILITY_RECENCY_WINDOW:] if recent else []
    ht_measured = ht_runs - ht_cost_unknown
    section["harness_terminated"] = {
        "runs": ht_runs,
        "by_cause": ht_by_cause,
        "_cost_sum": ht_cost_sum,
        "_cost_unknown_runs": ht_cost_unknown,
        "avg_cost_usd": round(ht_cost_sum / ht_measured, 6) if ht_measured > 0 else 0.0,
    }


def _project_alias_derived(
    data: dict,
    *,
    configured_key: str,
    configured_entry: dict,
    resolved_model: str | None,
    role: str,
    success: bool,
    tainted: bool,
    cost_usd: float | None,
) -> None:
    """Record an alias-served observation under the concrete version's profile.

    Two candidates are involved in one run when the configured identity is a
    family alias: the alias the router picked, and the version the vendor
    actually served. Until #2226 only the first accumulated any history, which
    is why a pinned candidate could never be ranked — it had no evidence, even
    when hundreds of runs had been served *by exactly that model* under an
    alias.

    This projects the observation onto the served version's own profile entry so
    that evidence exists. Three properties keep it honest:

    * It lands in a **separate** ``alias_derived`` section, never in the
      concrete entry's ``runs``/``_successes``. The run is already counted in
      full under the configured candidate, so anything summing counts across
      candidates (a fleet-wide sample floor, a cost cohort, a total-observation
      read) sees it once, under the candidate that was actually selected.
    * Each projected observation records **which** configured identity it came
      from, under ``by_configured_model``. Evidence about a model gathered while
      something else was selected is a weaker claim than evidence gathered under
      the model's own name, and a consumer has to be able to tell them apart.
    * Nothing is projected when the served identity **is** the configured one
      (a pinned candidate, or a provider that does not resolve aliases): there
      is no second candidate, and folding would double the entry's own history.

    A tainted run projects nothing but its taint tally, on the same ADR-0006
    clause-4 rule every other aggregate follows.
    """
    if not resolved_model:
        return
    configured_id = _resolve_storage_key(configured_key, configured_entry)
    if resolved_model == configured_id or resolved_model == configured_key:
        return
    models = data.setdefault("models", {})
    entry = models.setdefault(resolved_model, {})
    section = entry.setdefault(ALIAS_DERIVED_KEY, {}).setdefault(role, {})
    if tainted:
        section["tainted_runs"] = int(section.get("tainted_runs", 0)) + 1
        return
    section["runs"] = int(section.get("runs", 0)) + 1
    section["_successes"] = float(section.get("_successes", 0.0)) + (1.0 if success else 0.0)
    by_configured = section.setdefault("by_configured_model", {})
    source = by_configured.setdefault(configured_id, {})
    source["runs"] = int(source.get("runs", 0)) + 1
    source["_successes"] = float(source.get("_successes", 0.0)) + (1.0 if success else 0.0)
    if cost_usd is None:
        section["_cost_unknown_runs"] = int(section.get("_cost_unknown_runs", 0)) + 1
    else:
        section["_cost_sum"] = round(float(section.get("_cost_sum", 0.0)) + float(cost_usd), 6)


def _resolve_storage_key(model_key: str, entry: dict | None) -> str:
    """Return the canonical storage key an entry lives under, else the raw key."""
    canonical = canonical_id_for_legacy_key(model_key, entry)
    return canonical or model_key


def apply_run(data: dict, outcome: RunOutcome) -> dict:
    """Pure: fold one run outcome into the profiles dict, returning it."""
    band = _normalize_band(outcome.complexity)
    dev_entry = _ensure_model(
        data,
        outcome.dev_model,
        actual_model=outcome.dev_actual_model,
        provider=outcome.dev_provider,
        cli=outcome.dev_cli,
    )
    _update_dev(
        dev_entry,
        band,
        outcome.dev_success,
        outcome.dev_iterations,
        outcome.dev_cost_usd,
        complexity_score=outcome.complexity_score,
        duration_s=outcome.dev_duration_s,
        timeout_killed=outcome.dev_timeout_killed,
        timeout_limit_s=outcome.dev_timeout_limit_s,
        termination_cause=outcome.dev_termination_cause,
        domains=outcome.domains,
        tainted=outcome.dev_tainted,
        resolved_model=outcome.dev_resolved_model,
    )
    # Project the same dev observation onto the concrete version that served it
    # (#2226), so a pinned candidate is rankable from history gathered while the
    # alias was selected. Kept in its OWN section (``alias_derived``) rather than
    # in the concrete entry's counters: the run is already counted in full under
    # the configured candidate, and a cross-candidate aggregation that summed
    # both would see it twice.
    _project_alias_derived(
        data,
        configured_key=outcome.dev_model,
        configured_entry=dev_entry,
        resolved_model=outcome.dev_resolved_model,
        role="dev",
        success=outcome.dev_success,
        tainted=outcome.dev_tainted,
        cost_usd=outcome.dev_cost_usd,
    )
    if outcome.preflight_model:
        pf_entry = _ensure_model(
            data,
            outcome.preflight_model,
            actual_model=outcome.preflight_actual_model,
            provider=outcome.preflight_provider,
            cli=outcome.preflight_cli,
        )
        _update_preflight(
            pf_entry,
            outcome.preflight_cost_usd,
            tainted=outcome.dev_tainted,
            resolved_model=outcome.preflight_resolved_model,
        )
    # Preflight reliability telemetry (#1489): fold one completion outcome per
    # native preflight invocation under the model that actually ran it, so a
    # parse-retry or fallback is attributed correctly (never collapsed onto the
    # configured primary). Decoupled from the per-phase cost fold above.
    for att in outcome.preflight_attempts:
        att_entry = _ensure_model(
            data, att.name, actual_model=att.actual_model, provider=att.provider, cli=att.cli
        )
        _update_role_completion(
            att_entry,
            "preflight",
            att.completed,
            tainted=outcome.dev_tainted,
            resolved_model=att.resolved_model,
        )
        _project_alias_derived(
            data,
            configured_key=att.name,
            configured_entry=att_entry,
            resolved_model=att.resolved_model,
            role="preflight",
            success=att.completed,
            tainted=outcome.dev_tainted,
            cost_usd=att.cost_usd,
        )
    # Planner cost telemetry (#1489): fold the planner phase's runs + cost under its
    # canonical model ID. Per-attempt reliability is folded separately below.
    if outcome.planner_model:
        pl_entry = _ensure_model(
            data,
            outcome.planner_model,
            actual_model=outcome.planner_actual_model,
            provider=outcome.planner_provider,
            cli=outcome.planner_cli,
        )
        _update_planner(
            pl_entry,
            outcome.planner_cost_usd,
            tainted=outcome.dev_tainted,
            resolved_model=outcome.planner_resolved_model,
        )
    # Planner reliability telemetry (#1489): one completion outcome per native
    # plan-generation invocation, so transport-retry failures contribute failed
    # attempts rather than being hidden by a later successful plan output.
    for att in outcome.planner_attempts:
        att_entry = _ensure_model(
            data, att.name, actual_model=att.actual_model, provider=att.provider, cli=att.cli
        )
        _update_role_completion(
            att_entry,
            "planner",
            att.completed,
            tainted=outcome.dev_tainted,
            resolved_model=att.resolved_model,
        )
        _project_alias_derived(
            data,
            configured_key=att.name,
            configured_entry=att_entry,
            resolved_model=att.resolved_model,
            role="planner",
            success=att.completed,
            tainted=outcome.dev_tainted,
            cost_usd=att.cost_usd,
        )
    # Reviewer identity from the attempt records (#1388) lets findings/cost and
    # completion telemetry fold under the SAME canonical model ID the router looks
    # a reviewer up by — otherwise findings would key by bare profile name while
    # completion keys by canonical ID, splitting one reviewer across two entries.
    _rev_identity = {
        att.name: (att.actual_model, att.provider, att.cli) for att in outcome.reviewer_attempts
    }
    for name, (cycles, findings, cost) in outcome.reviewers.items():
        _am, _pv, _cl = _rev_identity.get(name, (None, None, None))
        rev_entry = _ensure_model(data, name, actual_model=_am, provider=_pv, cli=_cl)
        _update_review(
            rev_entry,
            cycles,
            findings,
            cost,
            tainted=outcome.dev_tainted,
            resolved_cycles=outcome.reviewer_resolved_cycles.get(name),
        )
    # Attempt-completion telemetry (#1388): one fold per reviewer invocation,
    # including failures, so the completion rate is complete over attempts.
    for att in outcome.reviewer_attempts:
        rev_entry = _ensure_model(
            data, att.name, actual_model=att.actual_model, provider=att.provider, cli=att.cli
        )
        _update_review_completion(
            rev_entry,
            att.completed_parseable_verdict,
            tainted=outcome.dev_tainted,
            resolved_model=att.resolved_model,
        )
        _project_alias_derived(
            data,
            configured_key=att.name,
            configured_entry=rev_entry,
            resolved_model=att.resolved_model,
            role="review",
            success=att.completed_parseable_verdict,
            tainted=outcome.dev_tainted,
            cost_usd=None,
        )
    # Plan-reviewer mechanical value telemetry (#1443): fold each per-attempt
    # uniqueness / latency-per-P1 sample under the reviewer's canonical model ID,
    # gated by the same run-level taint marker.
    if outcome.plan_reviewer_values:
        from theforge.reviewer_value import fold_plan_reviewer_value  # noqa: PLC0415

        for sample in outcome.plan_reviewer_values:
            rev_entry = _ensure_model(
                data,
                sample.name,
                actual_model=sample.actual_model,
                provider=sample.provider,
                cli=sample.cli,
            )
            fold_plan_reviewer_value(rev_entry, sample, tainted=outcome.dev_tainted)
    # Code-reviewer mechanical value telemetry (#2156): identical fold, into the
    # ``code_review_value`` section, under the same run-level taint marker.
    if outcome.code_reviewer_values:
        from theforge.reviewer_value import CODE_PHASE, fold_reviewer_value  # noqa: PLC0415

        for sample in outcome.code_reviewer_values:
            rev_entry = _ensure_model(
                data,
                sample.name,
                actual_model=sample.actual_model,
                provider=sample.provider,
                cli=sample.cli,
            )
            fold_reviewer_value(rev_entry, sample, phase=CODE_PHASE, tainted=outcome.dev_tainted)
    return data


# ── Backfill ──────────────────────────────────────────────────────────────


def backfill_from_history(history_path: Path) -> dict:
    """Aggregate ``assignment_history.yaml`` into a profiles dict.

    Only dev-role per-complexity success rates are derivable — the escalation
    history doesn't record iteration counts or cost, so those remain zero until
    fresh runs populate them.
    """
    data: dict = {"models": {}}
    if not history_path.exists():
        return data
    try:
        with open(history_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to read history %s: %s", history_path, exc)
        return data
    if not isinstance(raw, dict):
        return data
    records = raw.get("escalations") or []
    if not isinstance(records, list):
        return data
    from theforge.coordinator.trust_status import is_tainted  # noqa: PLC0415

    for r in records:
        if not isinstance(r, dict):
            continue
        model = str(r.get("dev_model") or "").strip()
        if not model:
            continue
        band = _normalize_band(str(r.get("complexity") or ""))
        outcome = str(r.get("outcome") or "").strip().upper()
        success = outcome == "DONE"
        # Taint gate (ADR-0006 clause 4): a run marked tainted "doesn't teach", so
        # it is excluded from the seeded aggregates and tallied under
        # ``tainted_runs`` instead. Legacy history rows carry no ``trust_status``
        # and read as admissible (taint requires an affirmative failed check).
        tainted = is_tainted(r.get("trust_status"))
        entry = _ensure_model(data, model)
        _update_dev(entry, band, success, iterations=0, cost_usd=0.0, tainted=tainted)
    return data


# ── Convenience: load + update + save ─────────────────────────────────────


def update_from_run(
    profiles_path: Path,
    history_path: Path | None,
    outcome: RunOutcome,
) -> dict:
    """Load profiles (seeding from history on first run), apply, save."""
    if not profiles_path.exists() and history_path is not None:
        data = backfill_from_history(history_path)
    else:
        data = load_profiles(profiles_path)
    data = apply_run(data, outcome)
    save_profiles(profiles_path, data)
    return data


def summarize_profile_scope(
    data: dict | None,
    canonical_id: str,
    *,
    role: str | None = None,
    complexity: str | None = None,
) -> list[dict[str, Any]]:
    """Return the exact slices a reset would affect, with pre-reset counts."""
    models = (data or {}).get("models") or {}
    if not isinstance(models, dict):
        return []
    entry = models.get(canonical_id)
    if not isinstance(entry, dict):
        return []

    normalized_complexity = _normalize_band(complexity) if complexity is not None else None
    summaries: list[dict[str, Any]] = []
    if normalized_complexity is not None:
        roles = ("dev",)
    else:
        roles = (role,) if role else ROLES

    for current_role in roles:
        section = entry.get(current_role)
        if not isinstance(section, dict):
            if current_role == "dev" and normalized_complexity is not None:
                summaries.append(
                    {
                        "role": "dev",
                        "complexity": normalized_complexity,
                        "runs": 0,
                        "successes": 0,
                        "avg_iterations": 0.0,
                        "avg_cost_usd": 0.0,
                        "cost_unknown_runs": 0,
                    }
                )
            continue

        if current_role == "dev":
            if normalized_complexity is not None:
                bucket = (section.get("by_complexity") or {}).get(normalized_complexity) or {}
                if not isinstance(bucket, dict):
                    bucket = {}
                summaries.append(
                    {
                        "role": "dev",
                        "complexity": normalized_complexity,
                        "runs": int(bucket.get("runs", 0)),
                        "successes": int(bucket.get("_successes", 0)),
                        "avg_iterations": float(bucket.get("avg_iterations", 0.0)),
                        "avg_cost_usd": float(bucket.get("avg_cost_usd", 0.0)),
                        "cost_unknown_runs": int(bucket.get("_cost_unknown_runs", 0)),
                    }
                )
                continue
            summaries.append(
                {
                    "role": "dev",
                    "complexity": None,
                    "runs": int(section.get("runs", 0)),
                    "successes": int(section.get("_successes", 0)),
                    "avg_iterations": float(section.get("avg_iterations", 0.0)),
                    "avg_cost_usd": float(section.get("avg_cost_usd", 0.0)),
                    "cost_unknown_runs": int(section.get("_cost_unknown_runs", 0)),
                    "by_complexity": deepcopy(section.get("by_complexity") or {}),
                }
            )
            continue

        summary = {
            "role": current_role,
            "complexity": None,
            "runs": int(section.get("runs", 0)),
            "avg_cost_usd": float(section.get("avg_cost_usd", 0.0)),
            "cost_unknown_runs": int(section.get("_cost_unknown_runs", 0)),
        }
        if current_role == "review":
            summary["avg_findings"] = float(section.get("avg_findings", 0.0))
        summaries.append(summary)

    return summaries


def reset_profile_data(
    data: dict | None,
    canonical_id: str,
    *,
    role: str | None = None,
    complexity: str | None = None,
) -> tuple[dict, list[dict[str, Any]]]:
    """Pure: reset one canonical model's profile data, optionally scoped."""
    updated = deepcopy(data if isinstance(data, dict) else {"models": {}})
    models = updated.setdefault("models", {})
    if not isinstance(models, dict):
        updated["models"] = {}
        models = updated["models"]

    pre_reset = summarize_profile_scope(
        updated,
        canonical_id,
        role=role,
        complexity=complexity,
    )
    if canonical_id not in models or not isinstance(models.get(canonical_id), dict):
        return updated, pre_reset

    entry = models[canonical_id]
    normalized_complexity = _normalize_band(complexity) if complexity is not None else None

    if role == "dev" and normalized_complexity is not None:
        dev = entry.setdefault("dev", {})
        by = dev.setdefault("by_complexity", {})
        bucket = by.setdefault(normalized_complexity, {})
        _zero_dev_bucket(bucket)
        _recompute_dev_section(dev)
        return updated, pre_reset

    if normalized_complexity is not None:
        dev = entry.setdefault("dev", {})
        by = dev.setdefault("by_complexity", {})
        bucket = by.setdefault(normalized_complexity, {})
        _zero_dev_bucket(bucket)
        _recompute_dev_section(dev)
        return updated, pre_reset

    roles = (role,) if role else ROLES
    for current_role in roles:
        if current_role == "dev":
            dev = entry.setdefault("dev", {})
            by = dev.setdefault("by_complexity", {})
            for band in COMPLEXITY_BANDS:
                _zero_dev_bucket(by.setdefault(band, {}))
            _recompute_dev_section(dev)
        elif current_role == "review":
            _zero_review_section(entry.setdefault("review", {}))
        elif current_role == "preflight":
            _zero_preflight_section(entry.setdefault("preflight", {}))
        elif current_role == "planner":
            _zero_planner_section(entry.setdefault("planner", {}))

    return updated, pre_reset


def record_profile_reset(
    *,
    profiles_path: Path,
    reset_history_path: Path,
    canonical_id: str,
    operator: str,
    role: str | None = None,
    complexity: str | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Reset one canonical model profile slice and append an audit log entry."""
    data = load_profiles(profiles_path)
    updated, pre_reset = reset_profile_data(
        data,
        canonical_id,
        role=role,
        complexity=complexity,
    )
    save_profiles(profiles_path, updated)

    changed = any(int(summary.get("runs", 0)) > 0 for summary in pre_reset)
    entry = {
        "timestamp": timestamp
        or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "operator": operator,
        "scope": {
            "canonical_id": canonical_id,
            "role": role,
            "complexity": _normalize_band(complexity) if complexity is not None else None,
        },
        "changed": changed,
        "pre_reset": pre_reset,
    }
    if reason:
        entry["reason"] = reason

    history = load_reset_history(reset_history_path)
    history.setdefault("resets", []).append(entry)
    save_reset_history(reset_history_path, history)
    return entry


# ── Canonical-ID migration ────────────────────────────────────────────────


def canonical_id_for_legacy_key(model_key: str, entry: dict | None = None) -> str | None:
    """Return canonical ID (`provider/model/transport`) for a legacy storage key.

    Resolution order:
      1. ``_identity`` metadata stamped on the entry (richest, most reliable).
      2. Already-canonical key (idempotency: ``anthropic/sonnet/cli`` → itself).
      3. Direct AGENT_REGISTRY match (e.g. ``claude/sonnet`` registry slot).
      4. ``-cli``/``-api`` suffix-aware unique-spec lookup
         (e.g. ``sonnet-cli`` → unique anthropic/sonnet CLI spec).
      5. Bare-name unique-spec lookup with constructable transport
         (e.g. ``deepseek-deepseek-v4-pro`` → unique deepseek/deepseek-v4-pro
         API spec), narrowed by a transport hint recorded on ``entry`` when the
         bare name alone is ambiguous (``gpt-5.4`` + ``transport_used: cli`` →
         ``openai/gpt-5.4/cli``).
    A concrete served version resolves to *its own* identity or to nothing.
    Until #2226 a sixth rule folded ``claude-sonnet-4-6`` onto
    ``anthropic/sonnet/cli`` on a name-prefix heuristic, on the reasoning that
    the shorthand was the only slot the version could live in. That is exactly
    the collapse this resolver must not perform: a family shorthand and a
    concrete version are different subjects, and merging them is what let
    evidence accumulate against a moving target. The catalog now carries pinned
    entries beside the shorthands, so ``claude-sonnet-4-6`` resolves through
    rule 5 to ``anthropic/claude-sonnet-4-6/cli`` when that entry exists. A
    version with no pinned entry — a release the catalog has not caught up to —
    stays **unresolved**, which is a fact the operator can act on (see
    ``forge audit alias-drift``), unlike a silent fold into the shorthand.

    A **provider-reported release date** appended to an otherwise-resolvable
    version is normalized away and the undated form re-resolved (#2311):
    ``claude-haiku-4-5-20251001`` → ``anthropic/claude-haiku-4-5/cli``. This is
    not the #2226 fold: the date suffix is a precision the provider added to a
    name that is *already* a concrete version, so the dated and undated spellings
    name the same subject, whereas a family shorthand and a version do not. The
    normalization is a last resort — a catalog entry pinning the dated spelling
    itself still wins — and it only ever *removes* the date, so a version with no
    catalog entry (``claude-opus-4-8-20260101``) stays unresolved exactly as its
    undated form does.

    ``entry`` is the optional record the key was read from — either a profiles
    storage entry (``_identity`` metadata) or an audit ``cost.agents`` entry
    (``transport_used``). It is only ever used as a *hint*; a key that stays
    ambiguous with the hint applied is still reported unresolved.

    Returns ``None`` when the key cannot be resolved unambiguously — those keys
    are reported as ambiguous by the migration tool and left under their legacy
    storage names rather than guessed at.
    """
    key = (model_key or "").strip()
    if not key:
        return None

    entry_transport: str | None = None
    if isinstance(entry, dict):
        metadata = entry.get("_identity")
        if isinstance(metadata, dict):
            provider = str(metadata.get("provider") or "").strip()
            model = str(metadata.get("model") or "").strip()
            transport = str(metadata.get("transport") or "").strip()
            if not transport and metadata.get("cli"):
                transport = "cli"
            if provider and model and transport in ("cli", "api"):
                return f"{provider}/{model}/{transport}"
        for hint_key in ("transport_used", "transport"):
            hint = str(entry.get(hint_key) or "").strip().lower()
            if hint in ("cli", "api"):
                entry_transport = hint
                break

    resolved = _resolve_canonical_key(key, entry_transport)
    if resolved is not None:
        return resolved

    undated = _strip_release_date(key)
    if undated is not None:
        return _resolve_canonical_key(undated, entry_transport)
    return None


# A provider-appended release date: ``-20251001`` (Anthropic) or ``-2024-08-06``
# (OpenAI), optionally ahead of a ``-cli``/``-api`` transport suffix. The year is
# pinned to ``20xx`` and the month/day ranges are checked below so a model name
# that merely ends in digits (``claude-sonnet-4-6``) can never be mistaken for a
# dated release.
_RELEASE_DATE_SUFFIX = re.compile(
    r"-(?P<year>20\d{2})(?P<sep>-?)(?P<month>\d{2})(?P=sep)(?P<day>\d{2})"
    r"(?P<transport>-(?:cli|api))?$"
)


def _strip_release_date(key: str) -> str | None:
    """Return ``key`` with a provider release-date suffix removed, or ``None``.

    ``None`` means "no date suffix here" — distinct from a key that had one — so
    the caller does not re-resolve an unchanged key a second time.
    """
    match = _RELEASE_DATE_SUFFIX.search(key)
    if match is None:
        return None
    if not (1 <= int(match.group("month")) <= 12 and 1 <= int(match.group("day")) <= 31):
        return None
    base = key[: match.start()] + (match.group("transport") or "")
    return base or None


def _resolve_canonical_key(key: str, entry_transport: str | None) -> str | None:
    """Resolution rules 2–5 for one exact spelling. See the caller's docstring."""
    # Already canonical?
    parts = key.split("/")
    if len(parts) == 3 and parts[2] in ("cli", "api"):
        return key

    spec = _resolve_agent_spec_for_profile_key(key)
    if spec is not None:
        return f"{spec.provider}/{spec.model}/{spec.transport.kind}"

    transport_hint: str | None = None
    base = key
    if key.endswith("-cli"):
        transport_hint = "cli"
        base = key[: -len("-cli")]
    elif key.endswith("-api"):
        transport_hint = "api"
        base = key[: -len("-api")]

    # A suffix on the key is a stronger statement than a hint recorded
    # alongside it, so the suffix wins when both are present.
    effective_hint = transport_hint or entry_transport

    spec = _unique_registry_spec(base, effective_hint)
    if spec is not None:
        return f"{spec.provider}/{spec.model}/{spec.transport.kind}"

    if transport_hint is None:
        # Try `<provider>-<model>` style: split at the first dash where the
        # prefix matches a known provider, e.g. ``deepseek-deepseek-v4-pro``
        # → provider=deepseek, model=deepseek-v4-pro.
        try:
            from theforge.config.models import AGENT_REGISTRY  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            AGENT_REGISTRY = {}  # type: ignore[assignment]
        for spec_obj in AGENT_REGISTRY.values():
            prefix = f"{spec_obj.provider}-"
            if key.startswith(prefix):
                model_part = key[len(prefix) :]
                if model_part == spec_obj.model:
                    same_model = [
                        s
                        for s in AGENT_REGISTRY.values()
                        if s.provider == spec_obj.provider and s.model == spec_obj.model
                    ]
                    transports = {s.transport.kind for s in same_model}
                    if len(transports) == 1:
                        return f"{spec_obj.provider}/{spec_obj.model}/{spec_obj.transport.kind}"

    return None


def _unique_registry_spec(model_name: str, transport: str | None) -> Any | None:
    try:
        from theforge.config.models import AGENT_REGISTRY  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    matches = [
        spec
        for spec in AGENT_REGISTRY.values()
        if spec.model == model_name and (transport is None or spec.transport.kind == transport)
    ]
    # Unique by (provider, model, transport) — keep all distinct triples to
    # detect ambiguity (e.g. CLI + API for same model with no transport hint).
    triples = {(s.provider, s.model, s.transport.kind) for s in matches}
    if len(triples) == 1:
        return matches[0]
    return None


def _bucket_summary(entry: dict) -> dict[str, float | int]:
    """Headline numbers for an entry, used in the migration report."""
    runs = 0
    successes = 0.0
    cost = 0.0
    iterations = 0.0
    review_attempted = 0
    review_completed = 0
    for role in ("dev", "review", "preflight"):
        sec = entry.get(role)
        if not isinstance(sec, dict):
            continue
        sec_runs = int(sec.get("runs", 0))
        runs += sec_runs
        if role == "dev":
            successes += _success_count(sec, sec_runs)
            cost += float(sec.get("_cost_sum", float(sec.get("avg_cost_usd", 0.0)) * sec_runs))
            iterations += float(
                sec.get("_iterations_sum", float(sec.get("avg_iterations", 0.0)) * sec_runs)
            )
        elif role == "review":
            cost += float(sec.get("_cost_sum", float(sec.get("avg_cost_usd", 0.0)) * sec_runs))
            # Reviewer attempt-completion telemetry (#1388) — a new dimension that
            # starts empty for already-migrated installs without native attempt
            # records, so the migration report can note it explicitly.
            review_attempted += int(sec.get("_attempted_count", 0))
            review_completed += int(sec.get("_completed_count", 0))
        elif role == "preflight":
            cost += float(sec.get("_cost_sum", float(sec.get("avg_cost_usd", 0.0)) * sec_runs))
    return {
        "runs": runs,
        "successes": successes,
        "cost_usd": round(cost, 6),
        "iterations": round(iterations, 4),
        "review_attempted": review_attempted,
        "review_completed": review_completed,
    }


def _merge_duration(target: dict, src: dict) -> None:
    """Combine the duration/kill accumulators of two dev buckets.

    Sums the completed-run accumulators, maxes the maxima, and recomputes
    ``avg_duration_s``. Legacy entries predating these fields lack the keys, so
    every read tolerates absence via ``.get(..., 0)``.
    """
    dur_sum = float(target.get("_duration_sum", 0.0)) + float(src.get("_duration_sum", 0.0))
    dur_runs = int(target.get("_duration_runs", 0)) + int(src.get("_duration_runs", 0))
    target["_duration_sum"] = dur_sum
    target["_duration_runs"] = dur_runs
    target["avg_duration_s"] = round(dur_sum / dur_runs, 4) if dur_runs > 0 else 0.0
    target["max_duration_s"] = max(
        float(target.get("max_duration_s", 0.0)), float(src.get("max_duration_s", 0.0))
    )
    target["max_killed_timeout_s"] = max(
        float(target.get("max_killed_timeout_s", 0.0)),
        float(src.get("max_killed_timeout_s", 0.0)),
    )


def _merge_harness_terminated(target: dict, src: dict) -> None:
    """Combine the ``harness_terminated`` sub-dicts of two dev buckets.

    Sums the run tally, the per-cause counts, and the segregated cost ledger.
    Legacy entries predating #1763 lack the sub-dict entirely, so every read
    tolerates absence — a merge from such a ``src`` is a no-op.
    """
    src_ht = src.get("harness_terminated")
    if not isinstance(src_ht, dict):
        return
    tgt_ht = target.setdefault(
        "harness_terminated",
        {
            "runs": 0,
            "by_cause": {},
            "_cost_sum": 0.0,
            "_cost_unknown_runs": 0,
            "avg_cost_usd": 0.0,
        },
    )
    tgt_ht["runs"] = int(tgt_ht.get("runs", 0)) + int(src_ht.get("runs", 0))
    tgt_by_cause = tgt_ht.setdefault("by_cause", {})
    for cause, count in (src_ht.get("by_cause") or {}).items():
        tgt_by_cause[cause] = int(tgt_by_cause.get(cause, 0)) + int(count)
    cost_sum = float(tgt_ht.get("_cost_sum", 0.0)) + float(src_ht.get("_cost_sum", 0.0))
    cost_unknown = int(tgt_ht.get("_cost_unknown_runs", 0)) + int(
        src_ht.get("_cost_unknown_runs", 0)
    )
    tgt_ht["_cost_sum"] = cost_sum
    tgt_ht["_cost_unknown_runs"] = cost_unknown
    measured = int(tgt_ht.get("runs", 0)) - cost_unknown
    tgt_ht["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0


def _merge_tainted_runs(target: dict, src: dict) -> None:
    """Sum the visible ``tainted_runs`` counter of two buckets.

    A no-op when neither side carries the counter (legacy buckets predating the
    ADR-0006 clause-4 taint gate), so a merge never fabricates a zero key.
    """
    src_tainted = int(src.get("tainted_runs", 0))
    if src_tainted or "tainted_runs" in target:
        target["tainted_runs"] = int(target.get("tainted_runs", 0)) + src_tainted


def _merge_recent(target: dict, src: dict, cap: int) -> None:
    """Concatenate the ``_recent`` outcome rings of two buckets and re-cap.

    Cross-source chronology is approximate (target's ring precedes src's), but a
    merge only runs during canonical-ID consolidation of alias entries, so a
    best-effort windowed view is acceptable. A no-op when neither side carries a
    ring (legacy buckets predating #1392), so a merge never fabricates the key.
    """
    merged = [int(v) for v in (target.get("_recent") or [])] + [
        int(v) for v in (src.get("_recent") or [])
    ]
    if merged:
        target["_recent"] = merged[-cap:]


def _merge_dev(target: dict, src: dict) -> None:
    runs = int(target.get("runs", 0)) + int(src.get("runs", 0))
    successes = int(target.get("_successes", 0)) + int(src.get("_successes", 0))
    iter_sum = float(target.get("_iterations_sum", 0.0)) + float(src.get("_iterations_sum", 0.0))
    cost_sum = float(target.get("_cost_sum", 0.0)) + float(src.get("_cost_sum", 0.0))
    # If accumulators absent on src, derive from runs * avg fields.
    if "_successes" not in src and "runs" in src:
        successes = int(target.get("_successes", 0)) + int(
            round(float(src.get("success_rate", 0.0)) * int(src.get("runs", 0)))
        )
    if "_iterations_sum" not in src and "runs" in src:
        iter_sum = float(target.get("_iterations_sum", 0.0)) + float(
            src.get("avg_iterations", 0.0)
        ) * int(src.get("runs", 0))
    if "_cost_sum" not in src and "runs" in src:
        # Legacy reconstruction: this ``avg_cost_usd * runs`` fallback assumes
        # ``avg_cost_usd`` was computed over TOTAL runs. Since #1616 the average
        # is over MEASURED runs only (``runs - _cost_unknown_runs``), so this
        # reconstruction is only exact when ``_cost_unknown_runs == 0`` — i.e.
        # for pre-#1616 profiles, which never recorded unmeasured runs. Do not
        # rely on it when an old entry both omits ``_cost_sum`` and carries
        # unmeasured runs (a combination that cannot occur in practice, since
        # ``_cost_sum`` has always been persisted alongside ``avg_cost_usd``).
        cost_sum = float(target.get("_cost_sum", 0.0)) + float(src.get("avg_cost_usd", 0.0)) * int(
            src.get("runs", 0)
        )

    # Same measured-vs-total caveat as above applies to the by_complexity /
    # by_complexity_score and review/preflight ``avg_cost_usd * runs`` fallbacks.
    cost_unknown = int(target.get("_cost_unknown_runs", 0)) + int(src.get("_cost_unknown_runs", 0))
    target["runs"] = runs
    target["_successes"] = successes
    target["_iterations_sum"] = iter_sum
    target["_cost_sum"] = cost_sum
    target["_cost_unknown_runs"] = cost_unknown
    if runs > 0:
        target["success_rate"] = round(successes / runs, 4)
        target["avg_iterations"] = round(iter_sum / runs, 4)
        measured = runs - cost_unknown
        target["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0
    _merge_duration(target, src)
    _merge_harness_terminated(target, src)
    _merge_tainted_runs(target, src)
    _merge_recent(target, src, CAPABILITY_RECENCY_WINDOW)

    src_by = src.get("by_complexity") or {}
    if src_by:
        target_by = target.setdefault("by_complexity", {})
        for band, bc_src in src_by.items():
            if not isinstance(bc_src, dict):
                continue
            bc_target = target_by.setdefault(band, {})
            bc_runs = int(bc_target.get("runs", 0)) + int(bc_src.get("runs", 0))
            bc_succ = int(bc_target.get("_successes", 0)) + int(
                bc_src.get(
                    "_successes",
                    round(float(bc_src.get("success_rate", 0.0)) * int(bc_src.get("runs", 0))),
                )
            )
            bc_iter = float(bc_target.get("_iterations_sum", 0.0)) + float(
                bc_src.get(
                    "_iterations_sum",
                    float(bc_src.get("avg_iterations", 0.0)) * int(bc_src.get("runs", 0)),
                )
            )
            bc_cost = float(bc_target.get("_cost_sum", 0.0)) + float(
                bc_src.get(
                    "_cost_sum",
                    float(bc_src.get("avg_cost_usd", 0.0)) * int(bc_src.get("runs", 0)),
                )
            )
            bc_unknown = int(bc_target.get("_cost_unknown_runs", 0)) + int(
                bc_src.get("_cost_unknown_runs", 0)
            )
            bc_target["runs"] = bc_runs
            bc_target["_successes"] = bc_succ
            bc_target["_iterations_sum"] = bc_iter
            bc_target["_cost_sum"] = bc_cost
            bc_target["_cost_unknown_runs"] = bc_unknown
            if bc_runs > 0:
                bc_target["success_rate"] = round(bc_succ / bc_runs, 4)
                bc_target["avg_iterations"] = round(bc_iter / bc_runs, 4)
                bc_measured = bc_runs - bc_unknown
                bc_target["avg_cost_usd"] = (
                    round(bc_cost / bc_measured, 6) if bc_measured > 0 else 0.0
                )
            _merge_duration(bc_target, bc_src)
            _merge_harness_terminated(bc_target, bc_src)
            _merge_tainted_runs(bc_target, bc_src)
            _merge_recent(bc_target, bc_src, CAPABILITY_RECENCY_WINDOW)

    src_by_score = src.get("by_complexity_score") or {}
    if src_by_score:
        target_by_score = target.setdefault("by_complexity_score", {})
        for score_key, sc_src in src_by_score.items():
            if not isinstance(sc_src, dict):
                continue
            sc_target = target_by_score.setdefault(score_key, {})
            sc_runs = int(sc_target.get("runs", 0)) + int(sc_src.get("runs", 0))
            sc_succ = int(sc_target.get("_successes", 0)) + int(
                sc_src.get(
                    "_successes",
                    round(float(sc_src.get("success_rate", 0.0)) * int(sc_src.get("runs", 0))),
                )
            )
            sc_iter = float(sc_target.get("_iterations_sum", 0.0)) + float(
                sc_src.get(
                    "_iterations_sum",
                    float(sc_src.get("avg_iterations", 0.0)) * int(sc_src.get("runs", 0)),
                )
            )
            sc_cost = float(sc_target.get("_cost_sum", 0.0)) + float(
                sc_src.get(
                    "_cost_sum",
                    float(sc_src.get("avg_cost_usd", 0.0)) * int(sc_src.get("runs", 0)),
                )
            )
            sc_unknown = int(sc_target.get("_cost_unknown_runs", 0)) + int(
                sc_src.get("_cost_unknown_runs", 0)
            )
            sc_target["runs"] = sc_runs
            sc_target["_successes"] = sc_succ
            sc_target["_iterations_sum"] = sc_iter
            sc_target["_cost_sum"] = sc_cost
            sc_target["_cost_unknown_runs"] = sc_unknown
            if sc_runs > 0:
                sc_target["success_rate"] = round(sc_succ / sc_runs, 4)
                sc_target["avg_iterations"] = round(sc_iter / sc_runs, 4)
                sc_measured = sc_runs - sc_unknown
                sc_target["avg_cost_usd"] = (
                    round(sc_cost / sc_measured, 6) if sc_measured > 0 else 0.0
                )
            _merge_duration(sc_target, sc_src)
            _merge_harness_terminated(sc_target, sc_src)
            _merge_tainted_runs(sc_target, sc_src)
            _merge_recent(sc_target, sc_src, CAPABILITY_RECENCY_WINDOW)

    src_by_domain = src.get("by_domain") or {}
    if src_by_domain:
        target_by_domain = target.setdefault("by_domain", {})
        for domain, dd_src in src_by_domain.items():
            if not isinstance(dd_src, dict):
                continue
            dd_target = target_by_domain.setdefault(domain, {})
            dd_runs = int(dd_target.get("runs", 0)) + int(dd_src.get("runs", 0))
            dd_succ = int(dd_target.get("_successes", 0)) + int(
                dd_src.get(
                    "_successes",
                    round(float(dd_src.get("success_rate", 0.0)) * int(dd_src.get("runs", 0))),
                )
            )
            dd_iter = float(dd_target.get("_iterations_sum", 0.0)) + float(
                dd_src.get(
                    "_iterations_sum",
                    float(dd_src.get("avg_iterations", 0.0)) * int(dd_src.get("runs", 0)),
                )
            )
            dd_cost = float(dd_target.get("_cost_sum", 0.0)) + float(
                dd_src.get(
                    "_cost_sum",
                    float(dd_src.get("avg_cost_usd", 0.0)) * int(dd_src.get("runs", 0)),
                )
            )
            dd_unknown = int(dd_target.get("_cost_unknown_runs", 0)) + int(
                dd_src.get("_cost_unknown_runs", 0)
            )
            dd_target["runs"] = dd_runs
            dd_target["_successes"] = dd_succ
            dd_target["_iterations_sum"] = dd_iter
            dd_target["_cost_sum"] = dd_cost
            dd_target["_cost_unknown_runs"] = dd_unknown
            if dd_runs > 0:
                dd_target["success_rate"] = round(dd_succ / dd_runs, 4)
                dd_target["avg_iterations"] = round(dd_iter / dd_runs, 4)
                dd_measured = dd_runs - dd_unknown
                dd_target["avg_cost_usd"] = (
                    round(dd_cost / dd_measured, 6) if dd_measured > 0 else 0.0
                )
            _merge_duration(dd_target, dd_src)
            _merge_harness_terminated(dd_target, dd_src)
            # Recency window (#155): concatenate the recent rings and re-cap so the
            # merged slice keeps a bounded windowed view. Tainted tallies sum.
            merged_recent = [int(v) for v in (dd_target.get("_recent") or [])] + [
                int(v) for v in (dd_src.get("_recent") or [])
            ]
            if merged_recent:
                dd_target["_recent"] = merged_recent[-DOMAIN_RECENCY_WINDOW:]
            dd_target["tainted_runs"] = int(dd_target.get("tainted_runs", 0)) + int(
                dd_src.get("tainted_runs", 0)
            )


def _merge_review(target: dict, src: dict) -> None:
    runs = int(target.get("runs", 0)) + int(src.get("runs", 0))
    find_sum = float(target.get("_findings_sum", 0.0)) + float(
        src.get("_findings_sum", float(src.get("avg_findings", 0.0)) * int(src.get("runs", 0)))
    )
    cost_sum = float(target.get("_cost_sum", 0.0)) + float(
        src.get("_cost_sum", float(src.get("avg_cost_usd", 0.0)) * int(src.get("runs", 0)))
    )
    cost_unknown = int(target.get("_cost_unknown_runs", 0)) + int(src.get("_cost_unknown_runs", 0))
    target["runs"] = runs
    target["_findings_sum"] = find_sum
    target["_cost_sum"] = cost_sum
    target["_cost_unknown_runs"] = cost_unknown
    if runs > 0:
        target["avg_findings"] = round(find_sum / runs, 4)
        measured = runs - cost_unknown
        target["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0
    # Attempt-completion counters (#1388): sum the two running totals, recompute
    # the rate, and concatenate the recency rings (oldest first is preserved
    # across both sources; the tail cap keeps the ring bounded).
    _merge_completion(target, src)
    _merge_tainted_runs(target, src)


def _merge_completion(target: dict, src: dict) -> None:
    """Merge the shared attempt-completion counters of two sections (#1388/#1489).

    Sums the running totals, recomputes the rate, and concatenates the bounded
    recency rings. Shared by the review, preflight, and planner mergers so every
    role's reliability signal survives a cross-shard profile merge identically.
    """
    attempted = int(target.get("_attempted_count", 0)) + int(src.get("_attempted_count", 0))
    completed = int(target.get("_completed_count", 0)) + int(src.get("_completed_count", 0))
    if attempted > 0:
        target["_attempted_count"] = attempted
        target["_completed_count"] = completed
        target["completion_rate"] = round(completed / attempted, 4)
        t_ring = target.get("_completion_recent")
        s_ring = src.get("_completion_recent")
        merged_ring = [int(v) for v in (t_ring if isinstance(t_ring, list) else [])] + [
            int(v) for v in (s_ring if isinstance(s_ring, list) else [])
        ]
        if len(merged_ring) > CAPABILITY_RECENCY_WINDOW:
            merged_ring = merged_ring[-CAPABILITY_RECENCY_WINDOW:]
        target["_completion_recent"] = merged_ring


def _merge_cost_section(target: dict, src: dict) -> None:
    """Merge the runs + cost accumulators shared by preflight and planner."""
    runs = int(target.get("runs", 0)) + int(src.get("runs", 0))
    cost_sum = float(target.get("_cost_sum", 0.0)) + float(
        src.get("_cost_sum", float(src.get("avg_cost_usd", 0.0)) * int(src.get("runs", 0)))
    )
    cost_unknown = int(target.get("_cost_unknown_runs", 0)) + int(src.get("_cost_unknown_runs", 0))
    target["runs"] = runs
    target["_cost_sum"] = cost_sum
    target["_cost_unknown_runs"] = cost_unknown
    if runs > 0:
        measured = runs - cost_unknown
        target["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0


def _merge_preflight(target: dict, src: dict) -> None:
    _merge_cost_section(target, src)
    _merge_completion(target, src)
    _merge_tainted_runs(target, src)


def _merge_planner(target: dict, src: dict) -> None:
    _merge_cost_section(target, src)
    _merge_completion(target, src)
    _merge_tainted_runs(target, src)


def _merge_entry(target: dict, src: dict) -> None:
    if not isinstance(src, dict):
        return
    for role, merger in (
        ("dev", _merge_dev),
        ("review", _merge_review),
        ("preflight", _merge_preflight),
        ("planner", _merge_planner),
    ):
        sec = src.get(role)
        if not isinstance(sec, dict):
            continue
        target_sec = target.setdefault(role, {})
        merger(target_sec, sec)
    src_id = src.get("_identity")
    if isinstance(src_id, dict) and not isinstance(target.get("_identity"), dict):
        target["_identity"] = dict(src_id)


def migrate_profiles_data(
    data: dict | None,
) -> tuple[dict, list[dict]]:
    """Pure: rewrite ``data`` so legacy alias entries merge under canonical IDs.

    Idempotent. Re-running on already-migrated data returns the same dict.
    Ambiguous keys (those with no unambiguous resolution) are left in place and
    flagged in the report. The report is a list of dicts — each entry is either
    a merge record (``canonical_id``, ``merged_from``, ``combined``) or an
    ambiguous-skip record (``ambiguous_key``, ``reason``).
    """
    models = (data or {}).get("models") or {}
    if not isinstance(models, dict):
        models = {}

    groups: dict[str, list[tuple[str, dict]]] = {}
    ambiguous: list[tuple[str, dict]] = []

    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        canonical = canonical_id_for_legacy_key(key, entry)
        if canonical is None:
            ambiguous.append((key, entry))
        else:
            groups.setdefault(canonical, []).append((key, entry))

    new_models: dict[str, dict] = {}
    report: list[dict] = []

    for canonical, members in groups.items():
        if len(members) == 1 and members[0][0] == canonical:
            # Already canonical with no aliases to merge.
            new_models[canonical] = members[0][1]
            continue
        merged: dict = {}
        # Stamp identity early so later metadata wins are skipped.
        provider, model, transport = canonical.split("/", 2)
        merged["_identity"] = {
            "provider": provider,
            "model": model,
            "transport": transport,
        }
        if transport == "cli":
            cli_runner = _provider_to_cli_runner(provider)
            if cli_runner:
                merged["_identity"]["cli"] = cli_runner
        for _, entry in members:
            _merge_entry(merged, entry)
        new_models[canonical] = merged
        report.append(
            {
                "canonical_id": canonical,
                "merged_from": [{"key": k, **_bucket_summary(e)} for k, e in members],
                "combined": _bucket_summary(merged),
            }
        )

    for key, entry in ambiguous:
        new_models[key] = entry
        report.append(
            {
                "ambiguous_key": key,
                "reason": "could not resolve to a unique canonical identity",
            }
        )

    out = dict(data or {})
    out["models"] = new_models
    return out, report


def _provider_to_cli_runner(provider: str) -> str | None:
    return {"anthropic": "claude", "openai": "codex", "google": "gemini"}.get(provider)


def migrate_history_data(
    data: dict | None,
) -> tuple[dict, list[dict]]:
    """Pure: canonicalize ``dev_model`` field on each escalation record.

    For records whose ``dev_model`` resolves to a canonical ID, the field is
    overwritten in place. Records that cannot be resolved are left unchanged
    and reported. Idempotent.
    """
    if not isinstance(data, dict):
        return {"escalations": []}, []
    records = data.get("escalations") or []
    if not isinstance(records, list):
        return {"escalations": []}, []

    new_records: list[dict] = []
    report: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        new_r = dict(r)
        legacy = str(r.get("dev_model") or "").strip()
        canonical = canonical_id_for_legacy_key(legacy) if legacy else None
        if canonical and canonical != legacy:
            new_r["dev_model"] = canonical
            report.append(
                {
                    "from": legacy,
                    "to": canonical,
                    "story": r.get("story"),
                }
            )
        elif legacy and canonical is None:
            report.append({"ambiguous_key": legacy, "story": r.get("story")})
        new_records.append(new_r)

    out = dict(data)
    out["escalations"] = new_records
    return out, report
