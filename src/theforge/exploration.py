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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Exploration modes recorded in the routing_decision ``exploration`` block.
MODE_WINNER = "winner"
MODE_CHALLENGER = "challenger"

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


def primary_domain(domains: list[str] | None) -> str | None:
    """Pick the deterministic primary domain for a routing key.

    A story may carry several domain tags; the routing key uses a single
    stable axis so a key is reconstructable and comparable across runs. The
    lexicographically-first non-empty tag is chosen (deterministic, order-
    independent). ``None`` when the story carried no domains.
    """
    tags = sorted({d.strip() for d in (domains or []) if isinstance(d, str) and d.strip()})
    return tags[0] if tags else None


@dataclass(frozen=True)
class RoutingKey:
    """The (phase, domain, complexity band) slot a challenger race is scoped to.

    Domain is optional (``None`` for stories with no domain tags). The string
    form is what gets recorded in the routing_decision block so the slot is
    reconstructable.
    """

    phase: str
    band: str
    domain: str | None = None

    @classmethod
    def build(
        cls, *, phase: str, complexity: str | None, domains: list[str] | None
    ) -> "RoutingKey":
        return cls(phase=phase, band=normalize_band(complexity), domain=primary_domain(domains))

    def as_str(self) -> str:
        return f"{self.phase}:{self.band}:{self.domain or '-'}"


@dataclass(frozen=True)
class ModelAggregate:
    """Per-model derived aggregate for one routing key (audit-derived view).

    All fields are materialized from the native ``model_profiles`` view, which
    already excludes tainted runs and applies recency weighting to
    ``success_rate``. ``runs`` counts admissible (non-tainted) runs only.
    """

    model_id: str
    runs: int
    success_rate: float | None
    avg_cost_usd: float | None
    avg_iterations: float | None
    avg_duration_s: float | None
    tainted_runs: int = 0

    def meets_floor(self, min_sample_size: int) -> bool:
        return self.runs >= min_sample_size and self.success_rate is not None


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
    audit records). Success rate is the recency-weighted, taint-excluded value
    the deterministic router already ranks on (:func:`model_profiles.get_dev_signal`);
    cost/iteration/duration averages come from the per-band stats reader. No
    ``performance_table.yaml`` is consulted — the cache is never authoritative.
    """
    from theforge import model_profiles as mp  # noqa: PLC0415

    aggregates: dict[str, ModelAggregate] = {}
    profiles = model_profiles or {}
    for cand in candidates:
        signal = mp.get_dev_signal(
            profiles,
            cand.id,
            key.band,
            min_sample_size,
            actual_model=cand.model,
            provider=cand.provider,
            cli=cand.cli,
            recency=recency,
        )
        runs = int(signal.get("runs", 0))
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
        aggregates[cand.id] = ModelAggregate(
            model_id=cand.id,
            runs=runs,
            success_rate=signal.get("rate"),
            avg_cost_usd=avg_cost,
            avg_iterations=avg_iters,
            avg_duration_s=avg_dur,
            tainted_runs=int(signal.get("tainted_runs", 0)),
        )
    return aggregates


def _winner_sort_key(agg: ModelAggregate) -> tuple:
    """Rank admissible models: highest success, then cheaper/fewer-iters/faster.

    Tie-breakers mirror the deterministic router's cost/iteration ordering so a
    winner declared by exploration is consistent with normal-mode selection. A
    missing metric sorts last (treated as worst) so it never wins a tie on
    absent evidence.
    """
    success = agg.success_rate if agg.success_rate is not None else -1.0
    cost = agg.avg_cost_usd if agg.avg_cost_usd is not None else float("inf")
    iters = agg.avg_iterations if agg.avg_iterations is not None else float("inf")
    dur = agg.avg_duration_s if agg.avg_duration_s is not None else float("inf")
    # Negate success so higher rate sorts first under ascending sort.
    return (-success, cost, iters, dur, agg.model_id)


def select_winner(aggregates: dict[str, ModelAggregate], min_sample_size: int) -> str | None:
    """Return the current winner id for a routing key, or None on cold start.

    A winner is declared only among models that meet the sample floor (clause
    2.3). Because the aggregates are recency-weighted, re-running this after a
    challenger race can return a *different* winner — the built-in dethrone/
    return path (clause 5). Returns ``None`` when no model is admissible yet, so
    the slot stays in cold-start/exploring mode.
    """
    admissible = [a for a in aggregates.values() if a.meets_floor(min_sample_size)]
    if not admissible:
        return None
    return sorted(admissible, key=_winner_sort_key)[0].model_id


def total_admissible_runs(aggregates: dict[str, ModelAggregate]) -> int:
    """Sum admissible (non-tainted) runs across all candidates for a key."""
    return sum(max(0, a.runs) for a in aggregates.values())


def choose_challenger(
    pool: list[str],
    winner: str | None,
    rng: random.Random,
) -> str | None:
    """Stochastically pick a challenger from eligible non-winners.

    Returns ``None`` when there is no eligible non-winner to race. The draw is
    from an injected RNG so the selection is reproducible in tests and the
    recorded pool + selection fully reconstruct the decision.
    """
    eligible = [m for m in pool if m != winner]
    if not eligible:
        return None
    return rng.choice(sorted(eligible))


@dataclass(frozen=True)
class ExplorationOutcome:
    """The recorded, reconstructable result of an exploration decision.

    ``mode`` is ``winner`` or ``challenger``. ``selected`` is the model id the
    router should run. ``pool`` is every candidate considered (the draw space).
    ``reason`` is a closed-vocabulary tag explaining why this mode was chosen.
    ``consumes_slot`` is True only when a challenger actually fires and must be
    counted against the per-sprint cap.
    """

    mode: str
    routing_key: str
    pool: list[str]
    selected: str | None
    winner: str | None
    reason: str
    consumes_slot: bool = False

    def to_block(self) -> dict[str, object]:
        """Serialize to the routing_decision ``exploration`` block shape."""
        return {
            "mode": self.mode,
            "routing_key": self.routing_key,
            "pool": list(self.pool),
            "selected": self.selected,
            "winner": self.winner,
            "reason": self.reason,
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
    - **Bounded**: if the per-sprint budget is exhausted, exploration downgrades
      to winner mode but still records the key + pool (an off-policy run must
      never be silently unlabeled; an on-policy fallback is labeled too).
    - **Recorded**: the pool drawn from and the challenger selected are captured.
    """
    pool = [c.id for c in candidates]
    key_str = key.as_str()
    budget_open = sprint_budget_remaining is None or sprint_budget_remaining > 0

    def _winner(reason: str) -> ExplorationOutcome:
        return ExplorationOutcome(
            mode=MODE_WINNER,
            routing_key=key_str,
            pool=pool,
            selected=winner,
            winner=winner,
            reason=reason,
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
    challenger = choose_challenger(pool, winner, rng)
    if challenger is None:
        return _winner(REASON_NO_CHALLENGER)
    return ExplorationOutcome(
        mode=MODE_CHALLENGER,
        routing_key=key_str,
        pool=pool,
        selected=challenger,
        winner=winner,
        reason=REASON_CADENCE,
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
        winner = select_winner(aggregates, min_sample_size)
        table[key.as_str()] = {
            "winner": winner,
            "models": {
                agg.model_id: {
                    "runs": agg.runs,
                    "success_rate": agg.success_rate,
                    "avg_cost_usd": agg.avg_cost_usd,
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
