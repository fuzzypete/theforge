"""Model capability profiles — aggregated per-model, per-role run outcomes.

Maintains ``.forge/model_profiles.yaml`` with success rate, iteration count and
cost averages for each model, broken down by role (dev, review, preflight) and
— for the dev role — by complexity band. Updated after every run from
``CoordinatorState``; seeded from ``assignment_history.yaml`` on first run.

This module has a thin I/O surface (``load_profiles``/``save_profiles``); all
aggregation is pure. No LLM calls.

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
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROLES = ("dev", "review", "preflight", "planner")
COMPLEXITY_BANDS = ("small", "medium", "large")

# Per-domain recency window (issue #155 / ADR-0006 clause 2.4). Per-domain dev
# outcomes maintain a bounded ring of their most recent genuine (non-tainted,
# non-harness-terminated) results so the routing-admissible rate consults a
# *windowed* view of history rather than a lifetime cumulative average — the
# exact shape clause 2.4 forbids from carrying routing weight. This is a local,
# deterministic windowing mechanism; the shared decay mechanism (#1392) reuses
# the same ring shape (:func:`_weighted_rate`) without changing the read contract
# (raw + weighted both recorded).
DOMAIN_RECENCY_WINDOW = 20

# Shared recency-weighting defaults (#1392, ADR-0006 clause 2.4). Every dev
# capability bucket (top-level, per-complexity, per-score) keeps a bounded ring
# of its most recent genuine outcomes — ``_recent`` — capped at
# :data:`CAPABILITY_RECENCY_WINDOW` on disk. The routing-admissible ``weighted``
# rate is computed from that ring by :func:`_weighted_rate` using the parameters
# below; operators override them via the ``assignment.recency`` config section.
#
# The mechanism is a run-position exponential decay rather than a wall-clock one:
# profiles store an ordered outcome ring, not per-run timestamps, so "age" is
# counted in runs (a story completed N runs ago), keeping the aggregation pure
# (no clock dependency) and deterministically recomputable from stored admissible
# data after any parameter change. ``half_life_runs`` is the number of runs after
# which a run's weight halves; the default (~50 runs) means the most recent
# handful of stories dominate while a large stale slice decays out of relevance
# instead of permanently anchoring a lifetime average (the #1392 Sonnet case).
DEFAULT_RECENCY_MODE = "exponential"
DEFAULT_RECENCY_HALF_LIFE_RUNS = 50.0
DEFAULT_RECENCY_WINDOW = 200
# Maximum outcomes retained per capability ring on disk. Bounds profile growth
# while leaving headroom to tune ``window`` upward without losing history; a
# configured ``window`` beyond this simply consults everything stored.
CAPABILITY_RECENCY_WINDOW = 200


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
    # kill) or ``"stuck_pattern"`` (stuck-pattern terminate); ``None`` for a run
    # the model itself finished (success or genuine failure). A harness-imposed
    # ending is evidence about the budget or the harness, not the model, so the
    # aggregator segregates these runs into a visible ``harness_terminated``
    # sub-dict and keeps them out of success_rate/avg_iterations/avg_cost stats.
    dev_termination_cause: str | None = None
    preflight_model: str | None = None
    dev_actual_model: str | None = None
    dev_provider: str | None = None
    dev_cli: str | None = None
    preflight_actual_model: str | None = None
    preflight_provider: str | None = None
    preflight_cli: str | None = None
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
    planner_cost_usd: float | None = None  # None = cost unmeasured
    planner_attempts: list[RoleAttempt] = field(default_factory=list)
    reviewers: dict[str, tuple[int, int, float | None]] = field(default_factory=dict)
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


# ── Pure aggregation ──────────────────────────────────────────────────────


def _normalize_band(complexity: str | None) -> str:
    """Map legacy/alt complexity strings to small|medium|large."""
    if not complexity:
        return "medium"
    cl = complexity.lower()
    if cl in COMPLEXITY_BANDS:
        return cl
    return {"low": "small", "high": "large"}.get(cl, "medium")


def _provider_from_cli(cli: str | None) -> str | None:
    """Map a recorded CLI runner name to its provider family.

    Delegates to the config registry so there is one such mapping in the
    codebase rather than a copy that can drift from it.
    """
    from theforge.config.models import provider_for_cli_runner

    return provider_for_cli_runner((cli or "").strip().lower() or None)


def _transport_from_identity(provider: str | None, cli: str | None) -> str | None:
    """Classify a *recorded run's* transport from what the runner reported.

    This reads attempt telemetry, not configuration: by the time an attempt is
    recorded the dispatch decision has already been made from the profile's
    TransportSpec, and ``cli`` is that transport's runner name. Nothing here
    feeds dispatch.
    """
    if cli:
        return "cli"
    if provider:
        return "api"
    return None


def _identity_metadata(
    *,
    actual_model: str | None,
    provider: str | None,
    cli: str | None,
) -> dict[str, str] | None:
    model = (actual_model or "").strip()
    inferred_provider = (provider or _provider_from_cli(cli) or "").strip()
    transport = _transport_from_identity(inferred_provider or None, cli)
    if not model or not inferred_provider:
        return None
    out = {
        "provider": inferred_provider,
        "model": model,
    }
    if transport:
        out["transport"] = transport
    if cli:
        out["cli"] = cli
    return out


def canonical_id_from_identity(
    *,
    actual_model: str | None,
    provider: str | None,
    cli: str | None,
) -> str | None:
    """Derive a canonical model ID from runtime identity fields.

    Returns None when the input is too thin to identify a canonical
    (provider+model+transport) — e.g. role-shaped names like ``"dev"`` that
    carry no provider hint.
    """
    inferred_provider = (provider or _provider_from_cli(cli) or "").strip()
    transport = _transport_from_identity(inferred_provider or None, cli)
    model = (actual_model or "").strip()
    if not (model and inferred_provider and transport):
        return None
    return f"{inferred_provider}/{model}/{transport}"


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

    Harness-terminated runs (deadline kill, stuck-pattern terminate) are kept out
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
) -> None:
    """Fold one dev run into a capability bucket, excluding tainted runs.

    Wraps :func:`_fold_dev_bucket` with the ADR-0006 clause-4 taint gate so the
    top-level dev bucket and the per-complexity / per-score bands exclude runs
    that failed their own trust checks exactly like the per-domain slice already
    does. A ``tainted`` run "doesn't teach": it is kept out of the capability
    accumulators (``runs``/``_successes``/…) and tallied under ``tainted_runs``
    so the exclusion stays visible in the record, never silently dropped.
    """
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
        )


def _update_review(
    entry: dict, cycles: int, findings: int, cost_usd: float | None, tainted: bool = False
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


def _update_review_completion(entry: dict, completed: bool, tainted: bool = False) -> None:
    """Fold one reviewer attempt-completion outcome into the review section (#1388).

    This is a separate fold from :func:`_update_review` (findings/cost) because it
    must record *failed* attempts too — a reviewer that timed out or emitted
    unparseable output never reaches the findings/cost path.

    Taint gate (ADR-0006 clause 4): a tainted run "doesn't teach", so its reviewer
    attempts are kept out of the completion aggregate and tallied under
    ``tainted_runs`` instead — never deleted, so the exclusion stays visible.
    """
    rev = entry.setdefault("review", {})
    if tainted:
        rev["tainted_runs"] = int(rev.get("tainted_runs", 0)) + 1
        return
    _fold_completion_counters(rev, completed)


def _update_role_completion(
    entry: dict, role: str, completed: bool, tainted: bool = False
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
    _fold_completion_counters(section, completed)


def _update_preflight(entry: dict, cost_usd: float | None, tainted: bool = False) -> None:
    pf = entry.setdefault("preflight", {})
    # Taint gate (ADR-0006 clause 4): a tainted run is excluded from the preflight
    # capability aggregate and tallied under ``tainted_runs`` instead. This
    # per-phase fold owns the single tainted-run tally (the per-attempt completion
    # fold only skips) so one tainted run is counted once.
    if tainted:
        pf["tainted_runs"] = int(pf.get("tainted_runs", 0)) + 1
        return
    if cost_usd is None:
        log.warning(
            "[model_profiles] Preflight run recorded cost-unmeasured (NOT $0.00): "
            "model=%s — transport reported no cost.",
            _entry_model_label(entry),
        )
    runs = int(pf.get("runs", 0)) + 1
    pf["runs"] = runs
    _fold_cost(pf, cost_usd)


def _update_planner(entry: dict, cost_usd: float | None, tainted: bool = False) -> None:
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
    )
    if outcome.preflight_model:
        pf_entry = _ensure_model(
            data,
            outcome.preflight_model,
            actual_model=outcome.preflight_actual_model,
            provider=outcome.preflight_provider,
            cli=outcome.preflight_cli,
        )
        _update_preflight(pf_entry, outcome.preflight_cost_usd, tainted=outcome.dev_tainted)
    # Preflight reliability telemetry (#1489): fold one completion outcome per
    # native preflight invocation under the model that actually ran it, so a
    # parse-retry or fallback is attributed correctly (never collapsed onto the
    # configured primary). Decoupled from the per-phase cost fold above.
    for att in outcome.preflight_attempts:
        att_entry = _ensure_model(
            data, att.name, actual_model=att.actual_model, provider=att.provider, cli=att.cli
        )
        _update_role_completion(att_entry, "preflight", att.completed, tainted=outcome.dev_tainted)
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
        _update_planner(pl_entry, outcome.planner_cost_usd, tainted=outcome.dev_tainted)
    # Planner reliability telemetry (#1489): one completion outcome per native
    # plan-generation invocation, so transport-retry failures contribute failed
    # attempts rather than being hidden by a later successful plan output.
    for att in outcome.planner_attempts:
        att_entry = _ensure_model(
            data, att.name, actual_model=att.actual_model, provider=att.provider, cli=att.cli
        )
        _update_role_completion(att_entry, "planner", att.completed, tainted=outcome.dev_tainted)
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
        _update_review(rev_entry, cycles, findings, cost, tainted=outcome.dev_tainted)
    # Attempt-completion telemetry (#1388): one fold per reviewer invocation,
    # including failures, so the completion rate is complete over attempts.
    for att in outcome.reviewer_attempts:
        rev_entry = _ensure_model(
            data, att.name, actual_model=att.actual_model, provider=att.provider, cli=att.cli
        )
        _update_review_completion(
            rev_entry, att.completed_parseable_verdict, tainted=outcome.dev_tainted
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


# ── Reader API for assignment ─────────────────────────────────────────────


def _recency_params(recency: Any | None) -> tuple[str, float, int]:
    """Resolve (mode, half_life_runs, window) from a config object or defaults.

    ``recency`` is any object exposing ``mode`` / ``half_life_runs`` / ``window``
    (the config's ``assignment.recency`` block); ``None`` selects the module
    defaults. Kept duck-typed so this pure module never imports the config layer.
    """
    if recency is None:
        return DEFAULT_RECENCY_MODE, DEFAULT_RECENCY_HALF_LIFE_RUNS, DEFAULT_RECENCY_WINDOW
    mode = str(getattr(recency, "mode", DEFAULT_RECENCY_MODE) or DEFAULT_RECENCY_MODE)
    half_life = float(getattr(recency, "half_life_runs", DEFAULT_RECENCY_HALF_LIFE_RUNS))
    window = int(getattr(recency, "window", DEFAULT_RECENCY_WINDOW))
    return mode, half_life, window


def _weighted_rate(
    recent: list[float],
    *,
    fallback: float | None,
    mode: str = DEFAULT_RECENCY_MODE,
    half_life_runs: float = DEFAULT_RECENCY_HALF_LIFE_RUNS,
    window: int = DEFAULT_RECENCY_WINDOW,
    min_samples: int = 0,
) -> float | None:
    """Recency-weight a bounded ring of numeric outcomes (ADR-0006 clause 2.4).

    This is the single weighting path every profile-derived rate flows through
    (#1392): the per-complexity dev signal, the per-domain dev signal (via
    :func:`_windowed_rate`), the reviewer completion-rate signal, and the
    plan-reviewer value signals (:mod:`theforge.reviewer_value`). ``recent`` is an
    ordered outcome ring (oldest first, newest last), already bounded per bucket.
    Entries are ``0/1`` for the boolean signals (success / completion) and
    arbitrary floats for continuous ones (uniqueness rate, latency-per-P1); the
    weighted mean below is identical for the ``0/1`` case, so this generalization
    is behavior-preserving for every existing caller. Modes:

    - ``"exponential"`` (default): weight run at age ``a`` (0 = newest) by
      ``0.5 ** (a / half_life_runs)``, so a run decays to half its weight every
      ``half_life_runs`` runs. Recomputable from the stored ring after a
      parameter change; deterministic (no wall-clock).
    - ``"window"``: unweighted mean over the last ``window`` outcomes (the legacy
      per-domain behavior, preserved so both signals share this function).
    - ``"off"``: no recency weighting — returns ``fallback`` (the lifetime raw
      rate), an operator kill-switch.

    Falls back to ``fallback`` when no windowed data exists (e.g. a legacy bucket
    predating the ring), so the value is never silently zeroed. ``min_samples``
    extends that guard to a sample floor on the *ring itself*: until the ring has
    accumulated at least ``min_samples`` outcomes, the weighted value falls back
    to ``fallback`` (the lifetime raw rate). This is what keeps recency weighting
    composing cleanly with the sample floor for a legacy/migrated bucket whose
    lifetime ``runs`` already passes ``min_runs`` but whose ring holds only a
    handful of freshly-folded outcomes — one new failure must not be allowed to
    become the entire weighted sample and swing a model with strong long-term
    history to 0.0.
    """
    if mode == "off":
        return fallback
    if not recent:
        return fallback
    capped = recent[-window:] if window and window > 0 else list(recent)
    if not capped or len(capped) < min_samples:
        return fallback
    if mode == "window" or not half_life_runs or half_life_runs <= 0:
        return round(sum(capped) / len(capped), 4)
    decay = 0.5 ** (1.0 / half_life_runs)
    n = len(capped)
    num = 0.0
    den = 0.0
    for i, outcome in enumerate(capped):
        weight = decay ** (n - 1 - i)  # newest outcome → age 0 → weight 1.0
        num += weight * float(outcome)
        den += weight
    return round(num / den, 4) if den > 0 else fallback


def get_dev_signal(
    profiles: dict,
    model: str,
    complexity: str | None = None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return a structured dev routing signal for explainability + ranking.

    Reads only the in-memory ``profiles`` dict (no disk access, no LLM call).
    One lookup surfaces everything the router weighs for a dev candidate, so a
    caller can both rank and explain from a single read:

    - ``rate``: the ``min_runs``-gated **recency-weighted** success rate; ``None``
      below the sample floor. This is the value the router ranks on (#1392 —
      routing consults the decayed view, not the lifetime cumulative average).
    - ``raw``: the ungated lifetime cumulative success ratio (``None`` when no
      admissible runs exist), kept so the audit can show raw-vs-weighted drift.
    - ``weighted``: the recency-weighted value over the bucket's ``_recent`` ring
      (:func:`_weighted_rate`); falls back to ``raw`` until the ring itself holds
      at least ``min_runs`` outcomes, so a legacy/migrated bucket that passes the
      floor on cumulative history but has a nearly-empty ring is not driven by a
      single freshly-folded run. ``rate`` is this value gated by the sample floor.
    - ``runs``: admissible sample count consulted (tainted / harness-terminated
      runs already excluded upstream).
    - ``tainted_runs``: how many runs were excluded from this bucket for taint
      (ADR-0006 clause 4), surfaced so the exclusion stays visible in the audit.
    - ``floor``: ``"pass"`` when ``runs >= min_runs`` (and ``runs > 0``), else
      ``"fail"``.
    - ``weighting``: the recency parameters actually applied (mode / half-life /
      window) so an operator can reproduce ``weighted`` from ``raw`` history.
    """
    mode, half_life, window = _recency_params(recency)
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    runs = 0
    successes = 0.0
    tainted = 0
    recent: list[int] = []
    if matching:
        if complexity is None:
            for _, entry in matching:
                dev = entry.get("dev")
                if not isinstance(dev, dict):
                    continue
                tainted += int(dev.get("tainted_runs", 0))
                ring = dev.get("_recent")
                if isinstance(ring, list):
                    recent.extend(int(v) for v in ring)
                entry_runs = int(dev.get("runs", 0))
                if entry_runs <= 0:
                    continue
                runs += entry_runs
                successes += _success_count(dev, entry_runs)
        else:
            band = _normalize_band(complexity)
            for _, entry in matching:
                dev = entry.get("dev")
                if not isinstance(dev, dict):
                    continue
                bc = (dev.get("by_complexity") or {}).get(band)
                if not isinstance(bc, dict):
                    continue
                tainted += int(bc.get("tainted_runs", 0))
                ring = bc.get("_recent")
                if isinstance(ring, list):
                    recent.extend(int(v) for v in ring)
                entry_runs = int(bc.get("runs", 0))
                if entry_runs <= 0:
                    continue
                runs += entry_runs
                successes += _success_count(bc, entry_runs)
    raw = round(successes / runs, 4) if runs > 0 else None
    # The weighted value is gated on the *ring* reaching the same sample floor,
    # not just lifetime ``runs``: a legacy/migrated bucket can pass ``min_runs``
    # on cumulative history while its ring holds only a few freshly-folded
    # outcomes. Below that ring floor the weighted value falls back to raw so a
    # single new run cannot become the entire weighted sample (#1392 review).
    weighted = _weighted_rate(
        recent,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = runs >= min_runs and runs > 0
    return {
        "raw": raw,
        "weighted": weighted,
        "runs": runs,
        "tainted_runs": tainted,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
    }


def get_dev_success_rate(
    profiles: dict,
    model: str,
    complexity: str | None = None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> float | None:
    """Return dev success rate for (model, complexity) or None under min_runs.

    Thin wrapper over :func:`get_dev_signal` so ranking and explainability
    share one aggregation path and can never diverge on the ranked value. The
    returned rate is the recency-weighted value (#1392), gated by ``min_runs``.
    """
    return get_dev_signal(
        profiles,
        model,
        complexity,
        min_runs,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
        recency=recency,
    )["rate"]


def _trailing_clean_streak(recent: list[int]) -> int:
    """Length of the newest all-clean run in an ordered ``0/1`` outcome ring.

    ``recent`` is oldest-first / newest-last (the shape every ``_completion_recent``
    ring is folded in). Counts backwards from the newest outcome and stops at the
    first failure, so the result is the number of *consecutive* clean attempts a
    model has most recently strung together — the evidence the reviewer
    re-inclusion recovery rule is defined over (#1880).
    """
    streak = 0
    for outcome in reversed(recent):
        if int(outcome) != 1:
            break
        streak += 1
    return streak


def get_review_signal(
    profiles: dict,
    model: str,
    min_runs: int = 5,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return a structured reviewer *completion* routing signal (#1388).

    The reviewer analog of :func:`get_dev_signal`, over the ``review`` section's
    attempt-completion counters rather than dev success. Reads only the in-memory
    ``profiles`` dict (no disk, no LLM); the authoritative evidence is the native
    reviewer-attempt telemetry these counters are folded from (ADR-0002).

    - ``rate``: the ``min_runs``-gated **recency-weighted** completion rate;
      ``None`` below the sample floor so a cold-start reviewer falls through to the
      existing tier/budget/cross-provider ordering instead of being penalized.
    - ``raw``: the ungated lifetime ``_completed_count / _attempted_count`` ratio
      (``None`` when no attempts exist), kept for raw-vs-weighted audit drift.
    - ``weighted``: the recency-weighted value over the ``_completion_recent``
      ring (:func:`_weighted_rate`); falls back to ``raw`` until the ring holds at
      least ``min_runs`` outcomes. ``rate`` is this value gated by the floor.
    - ``attempted`` / ``completed``: the running totals consulted (tainted runs
      already excluded upstream), so the rate is recomputable from the audit.
    - ``floor``: ``"pass"`` when ``attempted >= min_runs`` (and ``> 0``), else
      ``"fail"``.
    - ``weighting``: the recency parameters actually applied.
    - ``recovery``: the re-inclusion evidence (#1880) — ``clean_streak`` (how many
      of the newest ring outcomes are consecutively clean),
      ``clean_attempts_required`` (``K``, pinned to ``min_runs`` so the recovery
      rule reuses the existing sample floor rather than adding a config surface),
      and ``recovered`` (``True`` once the newest ``K`` attempts are all clean).
      This is reported unconditionally; the *threshold* comparison that decides
      whether recovery is relevant lives in the router, which owns the threshold.
    """
    mode, half_life, window = _recency_params(recency)
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    attempted = 0
    completed = 0
    tainted = 0
    recent: list[int] = []
    for _, entry in matching:
        rev = entry.get("review")
        if not isinstance(rev, dict):
            continue
        tainted += int(rev.get("tainted_runs", 0))
        attempted += int(rev.get("_attempted_count", 0))
        completed += int(rev.get("_completed_count", 0))
        ring = rev.get("_completion_recent")
        if isinstance(ring, list):
            recent.extend(int(v) for v in ring)
    raw = round(completed / attempted, 4) if attempted > 0 else None
    weighted = _weighted_rate(
        recent,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = attempted >= min_runs and attempted > 0
    clean_streak = _trailing_clean_streak(recent)
    required_clean = max(int(min_runs), 1)
    return {
        "raw": raw,
        "weighted": weighted,
        "attempted": attempted,
        "completed": completed,
        "tainted_runs": tainted,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
        "recovery": {
            "clean_streak": clean_streak,
            "clean_attempts_required": required_clean,
            "recovered": clean_streak >= required_clean,
        },
    }


def get_role_reliability_signal(
    profiles: dict,
    model: str,
    role: str,
    min_runs: int = 5,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return a structured role reliability signal for a non-dev role (#1489).

    Generalizes the reviewer *completion* signal (:func:`get_review_signal`, #1388)
    to any role whose section carries the shared attempt-completion counters —
    today ``preflight`` and ``planner``. Reads only the in-memory ``profiles`` dict
    (no disk, no LLM); the authoritative evidence is the native per-phase telemetry
    the counters are deterministically folded from (ADR-0002 / ADR-0006 clause 2).

    ``role`` is the profile section key (``"preflight"`` / ``"planner"``) so the
    signal is strictly per-role-scoped: a planner's history never leaks into a
    preflight decision. The returned shape matches :func:`get_review_signal` with an
    added ``role`` field so an audit can name the consulted signal:

    - ``rate``: the ``min_runs``-gated **recency-weighted** completion rate;
      ``None`` below the sample floor (cold start), so a role with too little
      admissible history falls through to the existing tier/budget ordering rather
      than being penalized (AC: cold-start ⇒ static policy).
    - ``raw``: the ungated lifetime ``_completed_count / _attempted_count`` ratio.
    - ``weighted``: the recency-weighted value over the ``_completion_recent`` ring.
    - ``attempted`` / ``completed``: running totals consulted (tainted runs already
      excluded upstream and surfaced under ``tainted_runs``).
    - ``floor``: ``"pass"`` when ``attempted >= min_runs`` (and ``> 0``), else
      ``"fail"``.
    - ``schema_ok``: whether the section carried recognizable completion counters —
      ``False`` for a legacy/foreign section shape, which forces a cold-start
      result (schema stability, ADR-0006 clause 2).
    """
    mode, half_life, window = _recency_params(recency)
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    attempted = 0
    completed = 0
    tainted = 0
    schema_ok = True
    recent: list[int] = []
    for _, entry in matching:
        section = entry.get(role)
        if not isinstance(section, dict):
            continue
        tainted += int(section.get("tainted_runs", 0))
        # A section that carries runs/cost but no completion counters is an older
        # schema that predates #1489: it cannot answer the reliability question, so
        # it must not silently read as 0% completion. Mark it schema-incompatible
        # and let the floor force a cold-start result.
        if "_attempted_count" not in section and "completion_rate" not in section:
            if int(section.get("runs", 0)) > 0:
                schema_ok = False
            continue
        attempted += int(section.get("_attempted_count", 0))
        completed += int(section.get("_completed_count", 0))
        ring = section.get("_completion_recent")
        if isinstance(ring, list):
            recent.extend(int(v) for v in ring)
    raw = round(completed / attempted, 4) if attempted > 0 else None
    weighted = _weighted_rate(
        recent,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = schema_ok and attempted >= min_runs and attempted > 0
    return {
        "role": role,
        "raw": raw,
        "weighted": weighted,
        "attempted": attempted,
        "completed": completed,
        "tainted_runs": tainted,
        "schema_ok": schema_ok,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
    }


def get_dev_domain_signal(
    profiles: dict,
    model: str,
    domains: list[str] | None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> dict:
    """Return a per-domain dev routing signal for the story's domain tags (#155).

    Aggregates the ``dev.by_domain`` slices for each requested tag across every
    matching profile entry, then clears the full ADR-0006 clause-2 admissibility
    set so a domain rate can only ever be a *preference within the eligible
    pool*, never eligibility itself. The value the router ranks on (``rate``) is
    the **recency-weighted** rate, not the lifetime cumulative ``raw``:

    - **Mechanical provenance / completeness (2.1–2.2):** the slice is folded by
      :func:`_fold_domain_slice`, which segregates harness-terminated runs out of
      the capability counts — the same complete recording path the complexity
      slice uses. No summary prose feeds this.
    - **Sample floor (2.3):** ``rate`` is ``None`` until ``runs >= min_runs``;
      below the floor the caller falls through to static tier/budget routing
      (cold start is a static-routing condition, not a low-confidence one).
    - **Recency weighting (2.4):** ``weighted`` is computed from a bounded recent
      window (:data:`DOMAIN_RECENCY_WINDOW`), a windowed view of history rather
      than the lifetime cumulative aggregate clause 2.4 forbids. ``rate`` returns
      the **weighted** value, so stale buckets decay out of routing weight; both
      ``raw`` and ``weighted`` are recorded (clause 7). When the shared decay
      mechanism (#1392) lands it supersedes this window without changing the read
      contract.
    - **Taint exclusion (clause 4):** tainted runs are never folded into the
      capability counts or the window; they are tallied under ``tainted_runs``
      (returned per-domain and in total) so the exclusion is visible and a
      tainted bucket cannot carry routing weight.
    - **Role specificity (per-role):** this reads only ``dev`` slices; it says
      nothing about review reliability.
    - **Schema stability (2.5):** legacy profiles simply lack ``by_domain`` and
      read as no-data (cold start), never as a penalty.

    A run tagged with several of the requested domains contributes to each of
    those domain buckets, so the aggregate double-counts across overlap — this is
    intentional: it rewards a model with broad demonstrated strength across the
    story's domains. The per-domain breakdown is returned in ``by_domain`` so the
    routing_decision block can show the matching profile slice per tag.

    Returns ``rate=None`` with ``floor="fail"`` when ``domains`` is empty or no
    admissible domain data exists — an explicit no-signal status, never a
    negative score.
    """
    requested = [d for d in (domains or []) if isinstance(d, str) and d]
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    per_domain: dict[str, dict] = {}
    total_runs = 0
    total_successes = 0.0
    total_tainted = 0
    recent_all: list[int] = []
    for domain in requested:
        d_runs = 0
        d_successes = 0.0
        d_tainted = 0
        d_recent: list[int] = []
        for _, entry in matching:
            dev = entry.get("dev")
            if not isinstance(dev, dict):
                continue
            dd = (dev.get("by_domain") or {}).get(domain)
            if not isinstance(dd, dict):
                continue
            d_tainted += int(dd.get("tainted_runs", 0))
            entry_runs = int(dd.get("runs", 0))
            if entry_runs <= 0:
                continue
            d_runs += entry_runs
            d_successes += _success_count(dd, entry_runs)
            recent = dd.get("_recent")
            if isinstance(recent, list):
                d_recent.extend(int(v) for v in recent)
        d_raw = round(d_successes / d_runs, 4) if d_runs > 0 else None
        d_weighted = _windowed_rate(d_recent, fallback=d_raw)
        per_domain[domain] = {
            "runs": d_runs,
            "raw": d_raw,
            "weighted": d_weighted,
            "tainted_runs": d_tainted,
        }
        total_runs += d_runs
        total_successes += d_successes
        total_tainted += d_tainted
        recent_all.extend(d_recent)
    raw = round(total_successes / total_runs, 4) if total_runs > 0 else None
    weighted = _windowed_rate(recent_all, fallback=raw)
    floor_ok = total_runs >= min_runs and total_runs > 0
    return {
        "domains": requested,
        "raw": raw,
        # Admissible ranked value is the recency-weighted window, not lifetime raw.
        "weighted": weighted,
        "runs": total_runs,
        "tainted_runs": total_tainted,
        "floor": "pass" if floor_ok else "fail",
        "recency": "windowed",
        "rate": weighted if floor_ok else None,
        "by_domain": per_domain,
    }


def get_dev_domain_complexity_signal(
    profiles: dict,
    model: str,
    domains: list[str] | None,
    complexity: str | None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return the per-(domain, band) dev signal — the true cross aggregate (#325).

    Reads ``dev.by_domain[domain].by_complexity[band]`` — the nested slice folded
    alongside the flat marginals — for each requested domain tag, summed across
    matching profile entries. This is the aggregate a challenger-sampling routing
    key ``(phase, domain, band)`` is scoped to: unlike the flat ``by_domain``
    slice it does NOT pool across bands, and unlike ``by_complexity`` it does NOT
    pool across domains, so two keys differing on EITHER axis compute distinct
    runs / rate / cadence.

    The ranked ``rate`` is the **recency-weighted** value under the SAME shared,
    configurable mechanism as :func:`get_dev_signal` (#1392): the ``recency``
    object's ``mode`` / ``half_life_runs`` / ``window`` are honored, so an
    operator setting ``assignment.recency.mode`` to ``off`` (lifetime raw) or
    ``exponential`` gets that policy for domain-bearing routing slots too — not a
    hardcoded window. Taint-excluded.

    Returns ``rate=None`` (``floor="fail"``) when ``domains`` is empty or the
    (domain, band) slice has fewer than ``min_runs`` admissible runs — an explicit
    cold-start status for that specific routing key.
    """
    requested = [d for d in (domains or []) if isinstance(d, str) and d]
    band = _normalize_band(complexity)
    mode, half_life, window = _recency_params(recency)
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    total_runs = 0
    total_successes = 0.0
    total_tainted = 0
    recent_all: list[int] = []
    for domain in requested:
        for _, entry in matching:
            dev = entry.get("dev")
            if not isinstance(dev, dict):
                continue
            dd = (dev.get("by_domain") or {}).get(domain)
            if not isinstance(dd, dict):
                continue
            bc = (dd.get("by_complexity") or {}).get(band)
            if not isinstance(bc, dict):
                continue
            total_tainted += int(bc.get("tainted_runs", 0))
            ring = bc.get("_recent")
            if isinstance(ring, list):
                recent_all.extend(int(v) for v in ring)
            entry_runs = int(bc.get("runs", 0))
            if entry_runs <= 0:
                continue
            total_runs += entry_runs
            total_successes += _success_count(bc, entry_runs)
    raw = round(total_successes / total_runs, 4) if total_runs > 0 else None
    # Shared configurable recency mechanism (#1392) — same as get_dev_signal, so
    # assignment.recency.mode/half_life/window applies to these routing slots too.
    weighted = _weighted_rate(
        recent_all,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = total_runs >= min_runs and total_runs > 0
    return {
        "domains": requested,
        "band": band,
        "raw": raw,
        "weighted": weighted,
        "runs": total_runs,
        "tainted_runs": total_tainted,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
    }


def _windowed_rate(recent: list[int], *, fallback: float | None) -> float | None:
    """Return the recency-weighted rate over a bounded per-domain outcome window.

    ``recent`` is the concatenation of the ``_recent`` rings clause 2.4 maintains
    per domain slice (each already capped to :data:`DOMAIN_RECENCY_WINDOW`). Kept
    as a thin wrapper over the shared :func:`_weighted_rate` (#1392) in ``window``
    mode so the per-domain signal uses the same weighting function as the
    per-complexity signal — an unweighted mean over the last
    :data:`DOMAIN_RECENCY_WINDOW` outcomes — rather than a bespoke per-call-site
    computation. Falls back to ``fallback`` (the lifetime raw rate) when no
    windowed data exists so the value is never silently zeroed.
    """
    return _weighted_rate(recent, fallback=fallback, mode="window", window=DOMAIN_RECENCY_WINDOW)


def get_dev_complexity_stats(
    profiles: dict,
    model: str,
    complexity: str | None,
    *,
    min_runs: int = 3,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> dict[str, float] | None:
    """Return per-band dev averages when the complexity band has enough runs."""
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    if not matching:
        return None
    band = _normalize_band(complexity)
    runs = 0
    measured_runs = 0
    iterations_sum = 0.0
    cost_sum = 0.0
    duration_runs = 0
    max_duration_s = 0.0
    max_killed_timeout_s = 0.0
    for _, entry in matching:
        dev = entry.get("dev")
        if not isinstance(dev, dict):
            continue
        bc = (dev.get("by_complexity") or {}).get(band)
        if not isinstance(bc, dict):
            continue
        entry_runs = int(bc.get("runs", 0))
        if entry_runs <= 0:
            continue
        entry_iterations = _metric_sum(bc, entry_runs, "_iterations_sum", "avg_iterations")
        entry_cost = _metric_sum(bc, entry_runs, "_cost_sum", "avg_cost_usd")
        if entry_iterations is None or entry_cost is None:
            return None
        runs += entry_runs
        # Cost is a MEASURED-run average: divide by (runs - unmeasured), never by
        # total runs, so unmeasured runs don't dilute the average toward zero.
        measured_runs += entry_runs - int(bc.get("_cost_unknown_runs", 0))
        iterations_sum += entry_iterations
        cost_sum += entry_cost
        # Duration/kill floors: legacy profiles predate these fields, so a missing
        # key defaults to 0 rather than voiding the whole (iteration) result.
        duration_runs += int(bc.get("_duration_runs", 0))
        max_duration_s = max(max_duration_s, float(bc.get("max_duration_s", 0.0)))
        max_killed_timeout_s = max(
            max_killed_timeout_s, float(bc.get("max_killed_timeout_s", 0.0))
        )
    if runs < min_runs or runs <= 0:
        return None
    return {
        "runs": float(runs),
        "avg_iterations": round(iterations_sum / runs, 4),
        "avg_cost_usd": round(cost_sum / measured_runs, 6) if measured_runs > 0 else 0.0,
        "max_duration_s": round(max_duration_s, 4),
        "duration_runs": float(duration_runs),
        "max_killed_timeout_s": round(max_killed_timeout_s, 4),
    }


def _matching_profile_entries(
    profiles: dict,
    model_key: str,
    *,
    actual_model: str | None,
    provider: str | None,
    cli: str | None,
) -> list[tuple[str, dict]]:
    models = (profiles or {}).get("models") or {}
    if not isinstance(models, dict):
        return []
    target = _resolve_identity(
        model_key,
        None,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    matching: list[tuple[str, dict]] = []
    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        if key == model_key:
            matching.append((key, entry))
            continue
        if target is None:
            continue
        if _resolve_identity(key, entry) == target:
            matching.append((key, entry))
    return matching


def _resolve_identity(
    model_key: str,
    entry: dict | None,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> tuple[str, str] | None:
    metadata = entry.get("_identity") if isinstance(entry, dict) else None
    if isinstance(metadata, dict):
        meta_provider = str(metadata.get("provider") or "").strip()
        meta_model = str(metadata.get("model") or "").strip()
        if meta_provider and meta_model:
            return (meta_provider, meta_model)
    explicit_provider = (provider or _provider_from_cli(cli) or "").strip()
    explicit_model = (actual_model or "").strip()
    if explicit_provider and explicit_model:
        return (explicit_provider, explicit_model)
    inferred = _infer_identity_from_key(model_key)
    if inferred is not None:
        return inferred
    if explicit_provider and model_key:
        return (explicit_provider, model_key)
    return None


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


def _infer_identity_from_key(model_key: str) -> tuple[str, str] | None:
    stripped = (model_key or "").strip()
    if not stripped:
        return None
    spec = _resolve_agent_spec_for_profile_key(stripped)
    if spec is not None:
        return (spec.provider, spec.model)
    if stripped.endswith("-cli"):
        return _identity_from_unique_spec(stripped[: -len("-cli")], transport="cli")
    if stripped.endswith("-api"):
        return _identity_from_unique_spec(stripped[: -len("-api")], transport="api")
    return _identity_from_unique_spec(stripped)


def _resolve_agent_spec_for_profile_key(model_key: str) -> Any | None:
    try:
        from theforge.config.models import known_raw_model_key_prefixes, resolve_agent_spec
    except Exception:  # noqa: BLE001
        return None
    candidates = [model_key]
    # Legacy storage keys are dash-joined raw keys, so the legacy provider-prefix
    # spellings have to be tried too — resolve_agent_spec normalizes them.
    prefixes = known_raw_model_key_prefixes()
    for prefix in sorted(prefixes, key=len, reverse=True):
        if model_key.startswith(f"{prefix}-"):
            candidates.append(f"{prefix}/{model_key[len(prefix) + 1 :]}")
    for candidate in candidates:
        try:
            return resolve_agent_spec(candidate)
        except ValueError:
            continue
    return None


def _identity_from_unique_spec(
    model_name: str,
    transport: str | None = None,
) -> tuple[str, str] | None:
    try:
        from theforge.config.models import AGENT_REGISTRY
    except Exception:  # noqa: BLE001
        return None
    matches = []
    for spec in AGENT_REGISTRY.values():
        if spec.model != model_name:
            continue
        if transport is not None and spec.transport.kind != transport:
            continue
        matches.append((spec.provider, spec.model))
    unique = set(matches)
    if len(unique) == 1:
        return next(iter(unique))
    return None


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
         (e.g. ``deepseek-deepseek-reasoner`` → unique deepseek/deepseek-reasoner
         API spec), narrowed by a transport hint recorded on ``entry`` when the
         bare name alone is ambiguous (``gpt-5.4`` + ``transport_used: cli`` →
         ``openai/gpt-5.4/cli``).
      6. Anthropic CLI concrete-version family match (``claude-sonnet-4-6`` →
         ``anthropic/sonnet/cli``). This one is a *prefix heuristic*, not a
         registry-derived rule: it holds only because the Anthropic registry
         slots are the shorthands ``sonnet``/``opus`` while the runner reports
         dated concrete versions. It is gated on the shorthand slot actually
         existing in the registry, and applies only to Anthropic CLI, so a
         future ``claude-<family>-*`` model with no matching shorthand stays
         unresolved rather than being folded into the wrong slot.

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

    family = _anthropic_cli_family_id(key, effective_hint)
    if family is not None:
        return family

    if transport_hint is None:
        # Try `<provider>-<model>` style: split at the first dash where the
        # prefix matches a known provider, e.g. ``deepseek-deepseek-reasoner``
        # → provider=deepseek, model=deepseek-reasoner.
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


def _anthropic_cli_family_id(model_key: str, transport: str | None) -> str | None:
    """Map a concrete Anthropic CLI model version onto its registry shorthand.

    The Anthropic registry slots are family shorthands (``anthropic/sonnet/cli``,
    ``anthropic/opus/cli``) while the Claude CLI reports dated concrete versions
    (``claude-sonnet-4-6``). Without this the same model indexes under two
    spellings (#2225).

    Deliberately narrow, because this is the one prefix heuristic in the
    resolver rather than a registry-derived rule:

    * only ``claude-<family>-<version…>`` keys,
    * only when ``anthropic/<family>/cli`` exists in the registry, and
    * only when nothing hints at a non-CLI transport.

    A future ``claude-<family>-*`` model with no matching shorthand slot
    resolves to ``None`` and is reported unresolved, which is the behaviour a
    catalog change should surface rather than silently mis-bucket.
    """
    if transport not in (None, "cli"):
        return None
    prefix = "claude-"
    if not model_key.startswith(prefix):
        return None
    rest = model_key[len(prefix) :]
    family, sep, version = rest.partition("-")
    if not family or not sep or not version:
        return None
    try:
        from theforge.config.models import AGENT_REGISTRY  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    canonical = f"anthropic/{family}/cli"
    return canonical if canonical in AGENT_REGISTRY else None


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


def _success_count(stats: dict[str, Any], runs: int) -> float:
    if "_successes" in stats:
        return float(stats.get("_successes", 0.0))
    return float(stats.get("success_rate", 0.0)) * float(runs)


def _metric_sum(
    stats: dict[str, Any],
    runs: int,
    sum_key: str,
    avg_key: str,
) -> float | None:
    if sum_key in stats:
        return float(stats.get(sum_key, 0.0))
    if avg_key in stats:
        return float(stats.get(avg_key, 0.0)) * float(runs)
    return None
