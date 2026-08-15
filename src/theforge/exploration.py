"""Challenger-sampling exploration — empirical routing over the audit substrate.

The adaptive router (``theforge.assignment``) exploits what it knows but never
*explores*: without this module it can only escape bad choices (escalation
memory), never discover that a different — possibly cheaper — model has become
good enough. This module is the single sanctioned deviation from deterministic
routing (ADR-0006 clause 8): the MongoDB query-planner analogy, where competing
plans are raced on sampled queries and the winner is cached until stats drift.

Design constraints this module enforces (ADR-0006 clause 8 + 2.3/2.4/4/5):

- **Derived, never authoritative.** Per-routing-key aggregates are materialized
  from the native model-profiles view (built from audit records, ADR-0002
  clauses 1-2). No local ``performance_table.yaml`` is ever *read* for a
  routing decision; :func:`build_performance_cache` only *writes* a rebuildable
  operator-inspection cache.
- **Sample floor + recency + taint.** A winner is declared only after
  ``min_sample_size`` admissible runs; success comparisons use the shared
  recency-weighting mechanism (#1392) and exclude tainted runs (#1852) — both
  already applied by the ``model_profiles`` readers this module consults.
- **Objective: cost to trusted completion, under a reliability floor** (#2392).
  The system exists to complete an issue with the *right-sized* model, so the
  winner is the candidate with the lowest expected cost to carry a story to
  trusted completion (``avg_cost_usd / success_rate``) among those clearing an
  explicit ``reliability_floor``. Ranking role-level success first made price
  unreachable metadata — with recency-weighted fractional rates the exact tie
  that would have consulted cost never occurs — and let whichever model had
  accumulated the most history win indefinitely. A candidate whose cost was
  never measured is reported as unmeasured and left to exploration, never
  silently ranked as the most expensive model possible.
- **Evidence rates bounded below, not just above.** Challengers are drawn
  least-sampled-first, so every eligible alternative gets a comparison-grade
  race within a bounded number of cadence hits; an incumbent's historical
  volume cannot make its own displacement impossible.
- **Bounded + labeled + reconstructable.** Challenger selection is stochastic,
  so :class:`ExplorationOutcome` records the routing key, the full pool drawn
  from, and the selection made — the run is reconstructable after the fact.
- **Promotion has an inverse.** :func:`select_winner` reads the current decayed
  evidence every time, so a later challenger race can dethrone a winner; no
  winner is permanent (clause-5 return path).

Every public function here is pure (no I/O, no LLM calls) except
:func:`write_performance_cache`, which materializes the derived view to disk.
The one non-determinism is challenger *selection*, which draws from an injected
:class:`random.Random` so tests are reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Exploration modes recorded in the routing_decision ``exploration`` block.
MODE_WINNER = "winner"
MODE_CHALLENGER = "challenger"

# Challenger draw policies (mirrors ``ExplorationConfig.challenger_rotation``).
ROTATION_LEAST_SAMPLED = "least_sampled"
ROTATION_RANDOM = "random"

# Complexity bands used in the routing key (mirrors model_profiles.COMPLEXITY_BANDS).
_BANDS = ("small", "medium", "large")
_BAND_ALIASES = {"low": "small", "high": "large"}
# Map the coordinator's LOW/MEDIUM/HIGH complexity onto the profile bands.
_COMPLEXITY_TO_BAND = {"LOW": "small", "MEDIUM": "medium", "HIGH": "large"}


def normalize_band(complexity: str | None) -> str:
    """Map any complexity/band spelling onto small|medium|large.

    Accepts the coordinator's ``LOW``/``MEDIUM``/``HIGH`` as well as the profile
    bands and legacy ``low``/``high`` aliases so the routing key is stable
    regardless of which vocabulary the caller uses.
    """
    if not complexity:
        return "medium"
    upper = complexity.upper()
    if upper in _COMPLEXITY_TO_BAND:
        return _COMPLEXITY_TO_BAND[upper]
    lower = complexity.lower()
    if lower in _BANDS:
        return lower
    return _BAND_ALIASES.get(lower, "medium")


def normalize_domains(domains: list[str] | None) -> tuple[str, ...]:
    """Normalize a story's domain tags to a deterministic, deduped tuple.

    Order-independent (sorted) so the recorded preference is stable across runs.
    Empty when the story carried no domains.
    """
    return tuple(sorted({d.strip() for d in (domains or []) if isinstance(d, str) and d.strip()}))


@dataclass(frozen=True)
class RoutingKey:
    """The routing slot a challenger race is scoped to: ``(phase, domain, band)``.

    Matches the spec's exploration contract exactly — phase, domain, and
    complexity band all identify the slot. Because the flat profile marginals
    (``by_complexity[band]`` / ``by_domain[domain]``) each collapse one axis, the
    aggregate is read from the nested ``by_domain[domain].by_complexity[band]``
    cross slice (folded in ``model_profiles._update_dev``), so two keys differing
    on EITHER axis — ``dev:small:api`` vs ``dev:large:api``, or ``dev:large:api``
    vs ``dev:large:web`` — get distinct aggregates and distinct cadence counters.

    ``domains`` is the normalized (sorted, deduped) tag set; a multi-tag story is
    its own slot (its cross aggregate sums the tag slices). A story with no
    domains falls back to the band-only slot ``(phase, band)``.
    """

    phase: str
    band: str
    domains: tuple[str, ...] = ()

    @classmethod
    def build(
        cls, *, phase: str, complexity: str | None, domains: list[str] | None
    ) -> "RoutingKey":
        return cls(
            phase=phase, band=normalize_band(complexity), domains=normalize_domains(domains)
        )

    def as_str(self) -> str:
        if self.domains:
            return f"{self.phase}:{self.band}:{'+'.join(self.domains)}"
        return f"{self.phase}:{self.band}"


@dataclass(frozen=True)
class ModelAggregate:
    """Per-model derived aggregate for one routing key (audit-derived view).

    All fields are materialized from the native ``model_profiles`` view, which
    already excludes tainted runs and applies recency weighting. ``runs`` and
    ``success_rate`` are scoped to the FULL routing key — the
    ``(domain, band)`` cross slice when the key carries domains, else the
    ``by_complexity[band]`` slice — so keys differing on either axis never share
    an aggregate or a cadence count. Cost/iteration/duration remain band-level.

    ``cost_measured_runs`` is how many of the band's runs had a *measurable*
    cost. It exists because the profile schema reports ``avg_cost_usd: 0.0``
    both for a genuinely free band and for an entirely unmeasured one; cost-
    ranked selection (#2392) must never read the latter as "free".
    """

    model_id: str
    runs: int
    success_rate: float | None
    avg_cost_usd: float | None
    avg_iterations: float | None
    avg_duration_s: float | None
    tainted_runs: int = 0
    cost_measured_runs: int = 0

    def meets_floor(self, min_sample_size: int) -> bool:
        return self.runs >= min_sample_size and self.success_rate is not None

    @property
    def cost_measured(self) -> bool:
        """True when at least one run at this band produced a measurable cost."""
        return self.avg_cost_usd is not None and self.cost_measured_runs > 0

    def meets_reliability(self, reliability_floor: float) -> bool:
        """True when the recency-weighted success rate clears the floor (#2392)."""
        return self.success_rate is not None and self.success_rate >= reliability_floor

    def completion_cost(self) -> float | None:
        """Expected dollars to carry ONE story to trusted completion, or ``None``.

        ``avg_cost_usd`` is the measured average cost of one *attempt* at this
        band (already inclusive of that attempt's dev iterations). A story is
        only done when the attempt succeeds, so the expected number of attempts
        to trusted completion is ``1 / success_rate`` and the expected cost is

            ``avg_cost_usd / success_rate``   (COMPLETION_COST_FORMULA)

        This is a derived *estimate*, not a recorded metric — the substrate
        stores no retry-inclusive cost field — so the formula travels with the
        number in the routing audit (#2392) and stays falsifiable.

        Returns ``None`` when the cost was never measured or the success rate is
        absent/zero: an unmeasured or never-succeeding candidate has no
        completion-cost evidence and must not be ranked as if it did.
        """
        if not self.cost_measured or not self.success_rate or self.success_rate <= 0:
            return None
        assert self.avg_cost_usd is not None  # guarded by cost_measured
        return self.avg_cost_usd / self.success_rate


# A candidate is described to this module by an opaque identity plus the fields
# the model_profiles readers need to look it up. Kept as a small structural type
# so the module does not depend on assignment.AgentDef.
@dataclass(frozen=True)
class Candidate:
    """A routable agent as seen by the explorer.

    ``id`` is the stable identity used in pools/records (the agent name). The
    remaining fields are passed through to the model_profiles readers so the
    per-key aggregate resolves the same bucket the deterministic router would.
    """

    id: str
    model: str | None = None
    provider: str | None = None
    cli: str | None = None
    tier: str | None = None


def derive_key_aggregates(
    model_profiles: dict | None,
    candidates: list[Candidate],
    key: RoutingKey,
    *,
    min_sample_size: int,
    recency: Any | None = None,
) -> dict[str, ModelAggregate]:
    """Materialize per-candidate aggregates for ``key`` from the audit-derived view.

    Reads ONLY the in-memory ``model_profiles`` dict (which is built from native
    audit records). No ``performance_table.yaml`` is consulted — the cache is
    never authoritative.

    The primary aggregate (``runs``, ``success_rate``) is scoped to the FULL
    routing key. When the key carries domains it is read from the nested
    ``(domain, band)`` cross slice
    (:func:`model_profiles.get_dev_domain_complexity_signal`); otherwise from the
    band slice (:func:`model_profiles.get_dev_signal`). Both are recency-weighted
    and taint-excluded. Because the cross slice pools across NEITHER axis, keys
    differing in domain OR band get distinct runs/rate/cadence — so
    ``dev:large:api``, ``dev:large:web``, and ``dev:small:api`` are three
    separate slots. Cost/iteration/duration stay band-level (tie-breakers only;
    the schema keeps no domain-sliced duration).
    """
    from theforge import model_profiles_read_model as mp  # noqa: PLC0415

    aggregates: dict[str, ModelAggregate] = {}
    profiles = model_profiles or {}
    for cand in candidates:
        if key.domains:
            # Per-(domain, band) cross slice — the true per-routing-key aggregate.
            # Honors the shared configurable recency policy, same as the band path.
            sig = mp.get_dev_domain_complexity_signal(
                profiles,
                cand.id,
                list(key.domains),
                key.band,
                min_sample_size,
                actual_model=cand.model,
                provider=cand.provider,
                cli=cand.cli,
                recency=recency,
            )
        else:
            # No domains → the band slot is the whole key.
            sig = mp.get_dev_signal(
                profiles,
                cand.id,
                key.band,
                min_sample_size,
                actual_model=cand.model,
                provider=cand.provider,
                cli=cand.cli,
                recency=recency,
            )
        runs = int(sig.get("runs", 0))
        success_rate = sig.get("rate")
        tainted = int(sig.get("tainted_runs", 0))
        stats = mp.get_dev_complexity_stats(
            profiles,
            cand.id,
            key.band,
            min_runs=1,
            actual_model=cand.model,
            provider=cand.provider,
            cli=cand.cli,
        )
        avg_cost = stats.get("avg_cost_usd") if stats else None
        avg_iters = stats.get("avg_iterations") if stats else None
        avg_dur = stats.get("max_duration_s") if stats else None
        cost_measured_runs = int(stats.get("cost_measured_runs", 0)) if stats else 0
        aggregates[cand.id] = ModelAggregate(
            model_id=cand.id,
            runs=runs,
            success_rate=success_rate,
            avg_cost_usd=avg_cost,
            avg_iterations=avg_iters,
            avg_duration_s=avg_dur,
            tainted_runs=tainted,
            cost_measured_runs=cost_measured_runs,
        )
    return aggregates


# The objective the winner is selected on, recorded verbatim in the routing
# audit so the estimate that moved a routing decision is falsifiable (#2392).
COMPLETION_COST_FORMULA = "avg_cost_usd / success_rate"

# Default minimum recency-weighted success rate for exploitation eligibility.
# Mirrors ``ExplorationConfig.reliability_floor``; kept here so the pure module
# has a usable default without importing the config layer.
DEFAULT_RELIABILITY_FLOOR = 0.7

# Why a winner was (or was not) declared — closed vocabulary, machine-queryable.
SELECTION_NO_ADMISSIBLE = "no_admissible_evidence"
SELECTION_BELOW_FLOOR = "no_candidate_meets_reliability_floor"
SELECTION_UNMEASURED = "no_measured_completion_cost"
SELECTION_COST_QUALIFIED = "cost_qualified"


def _winner_sort_key(agg: ModelAggregate) -> tuple:
    """Rank qualified models by expected cost to trusted completion (#2392).

    The objective is *total cost to carry a story to trusted completion*, not
    role-level success rate: ranking on success first made price unreachable
    metadata, because with recency-weighted fractional rates the exact tie that
    would have consulted cost never occurs. Reliability is not discarded — it is
    enforced as a hard floor before this key is ever applied (see
    :func:`select_winner_evidence`) and folded into the objective itself
    (a flakier model pays for more attempts).

    Every candidate reaching this key therefore has a real
    :meth:`ModelAggregate.completion_cost`; reliability, iterations, duration,
    and model id are secondary tie-breaks only.
    """
    cost = agg.completion_cost()
    reliability = agg.success_rate if agg.success_rate is not None else -1.0
    iters = agg.avg_iterations if agg.avg_iterations is not None else float("inf")
    dur = agg.avg_duration_s if agg.avg_duration_s is not None else float("inf")
    # Negate reliability so the more reliable of two equal-cost models sorts first.
    return (cost if cost is not None else float("inf"), -reliability, iters, dur, agg.model_id)


@dataclass(frozen=True)
class WinnerSelection:
    """The winner decision for a routing key plus the evidence that produced it.

    ``winner`` is ``None`` whenever exploitation is not justified — no
    admissible sample, nothing clearing the reliability floor, or nothing with
    measured cost evidence. The caller must then fall back to its declared
    static-tier pick rather than to whichever model happens to have the most
    history (#2392).
    """

    winner: str | None
    reason: str
    reliability_floor: float
    qualified: tuple[str, ...] = ()
    below_reliability_floor: tuple[str, ...] = ()
    unmeasured_cost: tuple[str, ...] = ()
    completion_costs: dict[str, float] = field(default_factory=dict)


def select_winner_evidence(
    aggregates: dict[str, ModelAggregate],
    min_sample_size: int,
    *,
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR,
) -> WinnerSelection:
    """Select the exploitation winner for a key and report the evidence used.

    Three gates, in order — a candidate must clear ALL of them to be exploited:

    1. **Sample floor** (clause 2.3): ``min_sample_size`` admissible runs.
    2. **Reliability floor** (#2392): the recency-weighted success rate must
       reach ``reliability_floor``. Because ranking is cost-first, without this
       gate the cheapest model would win however often it fails. A candidate
       below the floor is excluded no matter how cheap — but it stays in the
       challenger pool, so misjudging a model degrades gracefully both ways.
    3. **Measured cost**: an unmeasured candidate has no completion-cost
       evidence. It is reported as ``unmeasured_cost`` (not silently ranked as
       the most expensive model possible) and left to bounded exploration to
       measure.

    Returns ``winner=None`` with the reason when no candidate qualifies, so a
    slot with no usable evidence routes by its declared tier.
    """
    admissible = [a for a in aggregates.values() if a.meets_floor(min_sample_size)]
    if not admissible:
        return WinnerSelection(None, SELECTION_NO_ADMISSIBLE, reliability_floor)
    reliable = [a for a in admissible if a.meets_reliability(reliability_floor)]
    reliable_ids = {a.model_id for a in reliable}
    below = tuple(sorted(a.model_id for a in admissible if a.model_id not in reliable_ids))
    if not reliable:
        return WinnerSelection(
            None, SELECTION_BELOW_FLOOR, reliability_floor, below_reliability_floor=below
        )
    measured = [a for a in reliable if a.completion_cost() is not None]
    unmeasured = tuple(sorted(a.model_id for a in reliable if a.completion_cost() is None))
    costs = {a.model_id: a.completion_cost() for a in measured}
    if not measured:
        return WinnerSelection(
            None,
            SELECTION_UNMEASURED,
            reliability_floor,
            below_reliability_floor=below,
            unmeasured_cost=unmeasured,
        )
    ranked = sorted(measured, key=_winner_sort_key)
    return WinnerSelection(
        ranked[0].model_id,
        SELECTION_COST_QUALIFIED,
        reliability_floor,
        qualified=tuple(a.model_id for a in ranked),
        below_reliability_floor=below,
        unmeasured_cost=unmeasured,
        completion_costs={k: float(v) for k, v in costs.items() if v is not None},
    )


def select_winner(
    aggregates: dict[str, ModelAggregate],
    min_sample_size: int,
    *,
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR,
) -> str | None:
    """Return the current winner id for a routing key, or None when unqualified.

    Thin wrapper over :func:`select_winner_evidence` for callers that only need
    the id. Because the aggregates are recency-weighted and the ranking is
    cost-first, re-running this after a challenger race can return a *different*
    winner — the built-in dethrone/return path (clause 5). ``None`` means the
    slot has no exploitable evidence and stays on its declared static tier.
    """
    return select_winner_evidence(
        aggregates, min_sample_size, reliability_floor=reliability_floor
    ).winner


def total_admissible_runs(aggregates: dict[str, ModelAggregate]) -> int:
    """Sum admissible (non-tainted) runs across all candidates for a key."""
    return sum(max(0, a.runs) for a in aggregates.values())


def choose_challenger(
    pool: list[str],
    winner: str | None,
    rng: random.Random,
    *,
    aggregates: dict[str, ModelAggregate] | None = None,
    rotation: str = ROTATION_LEAST_SAMPLED,
) -> str | None:
    """Pick a challenger from eligible non-winners, least-sampled first.

    A uniform draw over the whole non-winner pool bounds each alternative's
    evidence-accumulation rate only from *above*: with a shared cadence and a
    growing pool, a specific alternative can accumulate comparison-grade runs
    more slowly than recency decay erodes them, so the incumbent's own
    displacement becomes impossible (#2392).

    ``least_sampled`` rotation fixes the lower bound: the draw is restricted to
    the eligible candidates with the FEWEST admissible runs for this key, with
    the injected RNG breaking ties. Racing a candidate raises its run count, so
    within ``len(eligible)`` cadence hits every eligible alternative — measured
    or not — has had a race. ``random`` restores the legacy uniform draw.

    Returns ``None`` when there is no eligible non-winner. The RNG is injected
    so the selection is reproducible and the recorded pool + selection fully
    reconstruct the decision.
    """
    eligible = sorted({m for m in pool if m != winner})
    if not eligible:
        return None
    if rotation == ROTATION_RANDOM or not aggregates:
        return rng.choice(eligible)

    def _runs(model_id: str) -> int:
        agg = aggregates.get(model_id)
        return max(0, agg.runs) if agg is not None else 0

    fewest = min(_runs(m) for m in eligible)
    return rng.choice([m for m in eligible if _runs(m) == fewest])


@dataclass(frozen=True)
class ExplorationOutcome:
    """The recorded, reconstructable result of an exploration decision.

    ``mode`` is ``winner`` or ``challenger``. ``selected`` is the model id the
    router should run. ``pool`` is every candidate considered (the draw space).
    ``reason`` is a closed-vocabulary tag explaining why this mode was chosen.
    ``domains`` are the story's domain tags — recorded for reconstruction as a
    preference alongside the ``(phase, band)`` routing key (they influence winner
    selection only as a tie-breaker, so they are metadata, not key identity).
    ``consumes_slot`` is True only when a challenger actually fires and must be
    counted against the per-sprint cap. ``evidence`` carries the selection
    evidence for the model that actually runs (#2392) — reliability floor,
    observed reliability, sample size, measured/unmeasured cost, estimated
    completion cost, and which of static-fallback / cost-qualified exploitation
    / bounded exploration produced the pick — so a routing decision can be
    audited without reconstructing the profiles it was derived from.
    """

    mode: str
    routing_key: str
    pool: list[str]
    selected: str | None
    winner: str | None
    reason: str
    domains: tuple[str, ...] = ()
    consumes_slot: bool = False
    evidence: dict[str, object] | None = None

    def to_block(self) -> dict[str, object]:
        """Serialize to the routing_decision ``exploration`` block shape."""
        return {
            "mode": self.mode,
            "routing_key": self.routing_key,
            "pool": list(self.pool),
            "selected": self.selected,
            "winner": self.winner,
            "reason": self.reason,
            "domains": list(self.domains),
            "evidence": dict(self.evidence or {}),
        }


# Reason vocabulary for the exploration block (closed set — machine-queryable).
REASON_COLD_START = "cold_start_exploring"
REASON_CADENCE = "challenger_cadence_hit"
REASON_ON_POLICY = "on_policy_winner"
REASON_CADENCE_MISS = "not_cadence_run"
REASON_SPRINT_CAP = "sprint_cap_reached"
REASON_NO_CHALLENGER = "no_eligible_challenger"
REASON_DISABLED = "exploration_disabled"


def decide_exploration(
    *,
    key: RoutingKey,
    candidates: list[Candidate],
    aggregates: dict[str, ModelAggregate],
    winner: str | None,
    explore_every_n: int,
    min_sample_size: int,
    sprint_budget_remaining: int | None,
    rng: random.Random,
    rotation: str = ROTATION_LEAST_SAMPLED,
) -> ExplorationOutcome:
    """Decide whether this run is a challenger race and, if so, pick the challenger.

    The decision honors every ADR-0006 clause-8 constraint:

    - **Cold start** (no admissible winner): the slot is *exploring* — the run is
      recorded as a challenger race so cold-start runs teach the router, but the
      *selection* is left to the caller's static-tier routing (``selected`` is
      ``None`` here). Still bounded by the sprint budget.
    - **Cadence**: once a winner exists, only every ``explore_every_n``-th run for
      the key is a candidate challenger race (deterministic in the key's
      admissible run count so it is reconstructable).
    - **Rotating**: the challenger is drawn least-sampled-first (``rotation``),
      so each eligible alternative's evidence-accumulation rate is bounded from
      below as well as above (#2392).
    - **Bounded**: if the per-sprint budget is exhausted, exploration downgrades
      to winner mode but still records the key + pool (an off-policy run must
      never be silently unlabeled; an on-policy fallback is labeled too).
    - **Recorded**: the pool drawn from and the challenger selected are captured.
    """
    pool = [c.id for c in candidates]
    key_str = key.as_str()
    key_domains = key.domains
    budget_open = sprint_budget_remaining is None or sprint_budget_remaining > 0

    def _winner(reason: str) -> ExplorationOutcome:
        return ExplorationOutcome(
            mode=MODE_WINNER,
            routing_key=key_str,
            pool=pool,
            selected=winner,
            winner=winner,
            reason=reason,
            domains=key_domains,
        )

    budget_exhausted = sprint_budget_remaining is not None and sprint_budget_remaining <= 0
    if explore_every_n < 1 or budget_exhausted:
        # Cap of 0 / exhausted budget: never explore. Still labeled with the key.
        reason = REASON_DISABLED if explore_every_n < 1 else REASON_SPRINT_CAP
        return _winner(reason)

    # ── Cold start: no admissible winner yet → exploring challenger race ──────
    if winner is None:
        if not budget_open:
            return _winner(REASON_SPRINT_CAP)
        # The selection stays with the caller's static-tier routing; we only
        # LABEL the run as an exploring challenger race so it teaches the router.
        return ExplorationOutcome(
            mode=MODE_CHALLENGER,
            routing_key=key_str,
            pool=pool,
            selected=None,
            winner=None,
            reason=REASON_COLD_START,
            domains=key_domains,
            consumes_slot=True,
        )

    # ── Steady state: challenger only on the cadence run ─────────────────────
    runs_for_key = total_admissible_runs(aggregates)
    # "Every Nth run" — this run is the (runs_for_key + 1)-th. Fire when that
    # count is a positive multiple of the cadence.
    this_run_index = runs_for_key + 1
    is_cadence_run = this_run_index % explore_every_n == 0
    if not is_cadence_run:
        return _winner(REASON_CADENCE_MISS)
    if not budget_open:
        return _winner(REASON_SPRINT_CAP)
    challenger = choose_challenger(pool, winner, rng, aggregates=aggregates, rotation=rotation)
    if challenger is None:
        return _winner(REASON_NO_CHALLENGER)
    return ExplorationOutcome(
        mode=MODE_CHALLENGER,
        routing_key=key_str,
        pool=pool,
        selected=challenger,
        winner=winner,
        reason=REASON_CADENCE,
        domains=key_domains,
        consumes_slot=True,
    )


@dataclass(frozen=True)
class ChallengerRecovery:
    """The plan for retrying a story after its challenger attempt failed.

    ``retry_selection`` is the model the story should be retried through (the
    current winner, or ``None`` to fall back to normal-tier assignment). The
    ``failure_record`` is written into the audit substrate so the failed
    challenger is remembered as an *exploration* failure, not as the story's
    final routing outcome (clause-8 recoverable).
    """

    retry_selection: str | None
    failure_record: dict[str, object]


def recover_from_failed_challenger(
    outcome: ExplorationOutcome,
) -> ChallengerRecovery | None:
    """Build the recovery plan for a failed challenger attempt.

    Returns ``None`` when the run was not a challenger (nothing to recover). A
    failed challenger does not count as the story's final outcome: the story is
    retried through the current winner (or normal-tier assignment when there is
    no winner yet), and the challenger failure is recorded separately.
    """
    if outcome.mode != MODE_CHALLENGER:
        return None
    return ChallengerRecovery(
        retry_selection=outcome.winner,
        failure_record={
            "kind": "exploration_failure",
            "routing_key": outcome.routing_key,
            "challenger": outcome.selected,
            "winner": outcome.winner,
            "pool": list(outcome.pool),
            "recovered_via": "winner" if outcome.winner else "normal_tier",
        },
    )


def build_performance_cache(
    model_profiles: dict | None,
    keys: list[RoutingKey],
    candidates_by_phase: dict[str, list[Candidate]],
    *,
    min_sample_size: int,
    recency: Any | None = None,
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR,
) -> dict[str, object]:
    """Materialize the derived per-key performance view (operator inspection).

    This is a rebuildable cache/derived view (ADR-0002 clauses 1-2), NOT an
    authority. It is produced purely from the audit-derived ``model_profiles``
    and can be discarded and recomputed at any time; no routing path reads it.
    """
    out: dict[str, object] = {
        "_note": "derived cache — never authoritative; rebuildable from audit"
    }
    table: dict[str, object] = {}
    for key in keys:
        candidates = candidates_by_phase.get(key.phase, [])
        aggregates = derive_key_aggregates(
            model_profiles,
            candidates,
            key,
            min_sample_size=min_sample_size,
            recency=recency,
        )
        selection = select_winner_evidence(
            aggregates, min_sample_size, reliability_floor=reliability_floor
        )
        table[key.as_str()] = {
            "winner": selection.winner,
            "winner_reason": selection.reason,
            "reliability_floor": selection.reliability_floor,
            "completion_cost_formula": COMPLETION_COST_FORMULA,
            "models": {
                agg.model_id: {
                    "runs": agg.runs,
                    "success_rate": agg.success_rate,
                    "avg_cost_usd": agg.avg_cost_usd,
                    "cost_status": "measured" if agg.cost_measured else "unmeasured",
                    "completion_cost_usd": agg.completion_cost(),
                    "avg_iterations": agg.avg_iterations,
                    "avg_duration_s": agg.avg_duration_s,
                    "tainted_runs": agg.tainted_runs,
                }
                for agg in aggregates.values()
            },
        }
    out["keys"] = table
    return out


def write_performance_cache(
    path: Path,
    cache: dict[str, object],
) -> None:
    """Write the derived performance cache to disk (best-effort, non-authoritative).

    The path is expected to live under ``.forge/`` (gitignored). This is the
    only impure function in the module and exists solely for operator
    inspection — nothing reads it back for a routing decision.
    """
    import yaml  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cache, sort_keys=True), encoding="utf-8")
