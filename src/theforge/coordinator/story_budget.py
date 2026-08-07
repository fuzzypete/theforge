"""Per-story budget allocation derived from the observed cost distribution.

A story's expected cost is a property of its complexity band, not a single
configured constant. A flat per-story ceiling is simultaneously ~100x the
median score-2 story (so it can never detect a runaway there) and close to
binding on legitimate score-9 work (so it constrains real work). This module
derives the per-story allocation mechanically from costs already recorded in
the audit substrate for the story's preflight complexity score.

Everything here is pure arithmetic over recorded numbers — no model is
consulted, and the same inputs always yield the same allocation.

Bands with too little history are not guessed at: they fall back to the
configured per-story budget, and the record says which basis was used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# A band needs at least this many admissible runs before its observed
# distribution is allowed to govern spend. Below the floor the sample is
# noise and the configured constant is the honest answer.
MIN_BAND_SAMPLES = 8

# Headroom above the band's observed maximum. The allocation must not bind on
# a story doing ordinary work for its band — the observed max is by definition
# a cost some real story of this kind incurred — while still sitting far below
# a flat ceiling for small bands.
BAND_HEADROOM = 1.25

# Never derive an allocation below this, regardless of how cheap the band is.
# A single retry on the cheapest band would otherwise trip the allocation.
MIN_ALLOCATION_USD = 1.0
MIN_REVIEW_CYCLE_PRICE_USD = 0.01
REVIEW_CYCLE_PRICE_MIN_SAMPLES = 3
REVIEW_CYCLE_PRICE_HEADROOM = 1.25
REVIEW_COST_HISTORY_TAIL = 50

# Where a dev dollar estimate was drawn from. Only the score-scoped source
# describes the population an allocation prices (one complexity score, all
# models), so only it may be subtracted from one — see
# ``_dev_estimate_comparable_to_allocation``. Defined here rather than in
# ``adaptive_iterations`` because seating is what the vocabulary is for.
DEV_ESTIMATE_SOURCE_SCORE = "profile_observed_score"
DEV_ESTIMATE_SOURCE_BAND = "profile_observed_band"
DEV_ESTIMATE_SOURCE_CONFIGURED = "configured_fallback"
DEV_ESTIMATE_HEADROOM_BASIS_ALLOCATION = "allocation_headroom"
DEV_ESTIMATE_SCOPE_DEV_PHASE = "dev_phase"

BASIS_SUBSTRATE_BAND = "substrate_band"
BASIS_CONFIGURED_FALLBACK = "configured_fallback"
BASIS_OBSERVED_COMPOSITION = "observed_composition"
BASIS_OBSERVED_REVIEW_CYCLE = "observed_review_cycle"
BASIS_OBSERVED_SPARSE = "observed_sparse"
BASIS_REVIEW_CEILING_FALLBACK = "ceiling_fallback"

# The rungs of the review-cycle pricing ladder, most specific first. A price is
# "observed" on any of the first three: all describe measured spend, and differ
# only in which population was measurable. The ceiling is not a price at all —
# it is a permission — so it sits outside this set and is reached only when
# nothing has ever been observed.
OBSERVED_REVIEW_CYCLE_BASES = frozenset(
    {BASIS_OBSERVED_COMPOSITION, BASIS_OBSERVED_REVIEW_CYCLE, BASIS_OBSERVED_SPARSE}
)

STATUS_WITHIN = "within_allocation"
STATUS_EXCEEDED = "allocation_exceeded"
STATUS_UNKNOWN = "cost_unknown"


@dataclass(frozen=True)
class StoryAllocation:
    """The per-story dollar allocation and the evidence it was derived from."""

    allocation_usd: float
    basis: str
    complexity_score: int | None
    fallback_configured_usd: float
    sample_count: int = 0
    median_usd: float | None = None
    p90_usd: float | None = None
    max_usd: float | None = None
    reason: str = ""
    excluded_for_taint: int = 0
    scaled_profiles: dict = field(default_factory=dict)

    @property
    def derived(self) -> bool:
        """True when the allocation came from observed costs, not the constant."""
        return self.basis == BASIS_SUBSTRATE_BAND

    def as_dict(self) -> dict:
        return {
            "allocation_usd": round(self.allocation_usd, 2),
            "basis": self.basis,
            "complexity_score": self.complexity_score,
            "sample_count": self.sample_count,
            "median_usd": _round_opt(self.median_usd),
            "p90_usd": _round_opt(self.p90_usd),
            "max_usd": _round_opt(self.max_usd),
            "fallback_configured_usd": round(self.fallback_configured_usd, 2),
            "reason": self.reason,
            "excluded_for_taint": self.excluded_for_taint,
            **({"scaled_profiles": dict(self.scaled_profiles)} if self.scaled_profiles else {}),
        }

    def expected_range_text(self) -> str:
        """Human-readable expected cost range for this band."""
        if not self.derived:
            return f"no band history (configured ${self.fallback_configured_usd:.2f})"
        return (
            f"median ${self.median_usd:.2f} / p90 ${self.p90_usd:.2f} / "
            f"max ${self.max_usd:.2f} over {self.sample_count} run(s) "
            f"at complexity score {self.complexity_score}"
        )


@dataclass(frozen=True)
class ReviewCyclePlanningPrice:
    """Planned per-cycle review price plus the evidence it was derived from."""

    planned_cost_usd: float
    basis: str
    fallback_configured_usd: float
    sample_count: int = 0
    median_usd: float | None = None
    p90_usd: float | None = None
    max_usd: float | None = None
    headroom_multiplier: float = REVIEW_CYCLE_PRICE_HEADROOM
    reason: str = ""
    excluded_for_taint: int = 0
    composition: tuple[str, ...] | None = None

    @property
    def derived(self) -> bool:
        return self.basis in OBSERVED_REVIEW_CYCLE_BASES

    def as_dict(self) -> dict:
        return {
            "planned_cost_usd": round(self.planned_cost_usd, 4),
            "basis": self.basis,
            "fallback_configured_usd": round(self.fallback_configured_usd, 4),
            "sample_count": self.sample_count,
            "median_usd": _round_opt(self.median_usd),
            "p90_usd": _round_opt(self.p90_usd),
            "max_usd": _round_opt(self.max_usd),
            "headroom_multiplier": round(self.headroom_multiplier, 4),
            "reason": self.reason,
            "excluded_for_taint": self.excluded_for_taint,
            "composition": list(self.composition) if self.composition else None,
        }


def _round_opt(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over ``values`` (need not be pre-sorted).

    Deterministic and dependency-free: rank = ceil(fraction * n), clamped into
    ``[1, n]``. ``fraction=0.5`` yields the lower median for even samples, which
    is the conservative choice for a spend estimate.
    """
    if not values:
        raise ValueError("percentile() requires at least one value")
    ordered = sorted(values)
    n = len(ordered)
    rank = int(-(-fraction * n // 1))  # ceil without importing math
    if rank < 1:
        rank = 1
    if rank > n:
        rank = n
    return ordered[rank - 1]


def allocation_from_samples(
    complexity_score: int | None,
    samples: list[float],
    configured_usd: float,
    *,
    excluded_for_taint: int = 0,
    min_samples: int = MIN_BAND_SAMPLES,
) -> StoryAllocation:
    """Return the allocation for ``complexity_score`` given its cost ``samples``.

    Pure: no I/O. ``samples`` are the total costs of admissible prior runs that
    scored the same complexity. Below ``min_samples`` the configured constant is
    used and the reason records why.
    """
    if complexity_score is None:
        return StoryAllocation(
            allocation_usd=configured_usd,
            basis=BASIS_CONFIGURED_FALLBACK,
            complexity_score=None,
            fallback_configured_usd=configured_usd,
            sample_count=len(samples),
            reason="no preflight complexity score for this story",
            excluded_for_taint=excluded_for_taint,
        )
    usable = [float(s) for s in samples if s is not None and float(s) > 0.0]
    if len(usable) < min_samples:
        return StoryAllocation(
            allocation_usd=configured_usd,
            basis=BASIS_CONFIGURED_FALLBACK,
            complexity_score=complexity_score,
            fallback_configured_usd=configured_usd,
            sample_count=len(usable),
            reason=(
                f"complexity score {complexity_score} has {len(usable)} admissible "
                f"run(s), below the {min_samples}-run floor"
            ),
            excluded_for_taint=excluded_for_taint,
        )
    median = percentile(usable, 0.5)
    p90 = percentile(usable, 0.9)
    observed_max = max(usable)
    allocation = max(observed_max * BAND_HEADROOM, MIN_ALLOCATION_USD)
    return StoryAllocation(
        allocation_usd=round(allocation, 2),
        basis=BASIS_SUBSTRATE_BAND,
        complexity_score=complexity_score,
        fallback_configured_usd=configured_usd,
        sample_count=len(usable),
        median_usd=median,
        p90_usd=p90,
        max_usd=observed_max,
        reason=(
            f"derived from {len(usable)} admissible run(s) at complexity score "
            f"{complexity_score} (max ${observed_max:.2f} x {BAND_HEADROOM})"
        ),
        excluded_for_taint=excluded_for_taint,
    )


def derive_story_allocation(
    project_root: Path,
    *,
    complexity_score: int | None,
    configured_usd: float,
    min_samples: int = MIN_BAND_SAMPLES,
) -> StoryAllocation:
    """Read the band's cost distribution from the substrate and allocate.

    A missing or unreadable substrate is not a run-stopping condition here: the
    allocation degrades to the configured constant and says so, because the
    configured value is exactly the governance that existed before this
    derivation. Unlike routing history — where silently-empty history would
    produce a wrong model choice — the fallback here is the documented
    behavior the acceptance criteria require.
    """
    from theforge.coordinator import audit_substrate  # noqa: PLC0415

    samples: list[float] = []
    excluded = 0
    reason_prefix = ""
    substrate = audit_substrate.substrate_path(project_root)
    if not substrate.exists() and not audit_substrate.has_audit_inputs(project_root):
        reason_prefix = "no audit substrate; "
    else:
        try:
            conn = audit_substrate.require_substrate(project_root)
        except audit_substrate.SubstrateError as exc:
            reason_prefix = f"substrate unreadable ({type(exc).__name__}); "
        else:
            try:
                stats: dict = {}
                by_score = audit_substrate.derive_cost_samples_by_score(conn, stats=stats)
                excluded = int(stats.get("excluded_for_taint", 0))
                if complexity_score is not None:
                    samples = list(by_score.get(int(complexity_score), []))
            finally:
                conn.close()

    allocation = allocation_from_samples(
        complexity_score,
        samples,
        configured_usd,
        excluded_for_taint=excluded,
        min_samples=min_samples,
    )
    if reason_prefix:
        from dataclasses import replace  # noqa: PLC0415

        allocation = replace(allocation, reason=reason_prefix + allocation.reason)
    return allocation


def review_cycle_planning_from_samples(
    samples: list[float],
    configured_ceiling_usd: float,
    *,
    excluded_for_taint: int = 0,
    min_samples: int = REVIEW_CYCLE_PRICE_MIN_SAMPLES,
    headroom_multiplier: float = REVIEW_CYCLE_PRICE_HEADROOM,
    derived_basis: str = BASIS_OBSERVED_REVIEW_CYCLE,
    scope_suffix: str = "",
    composition: tuple[str, ...] | None = None,
) -> ReviewCyclePlanningPrice:
    """Return the deterministic planning price for one review cycle.

    Every observed price is capped at the ceiling of the panel about to run.
    History can carry costs from larger or formerly pricier panels, and a panel
    that cannot spend more than $3 must never reserve $5 — that reservation is
    for spend which is not merely unlikely but impossible.

    Three outcomes, and which one applies is decided only by how much has been
    measured — never by how the money is permitted to be spent:

    * at or above ``min_samples``, the median of observed spend plus headroom;
    * with fewer but at least one observation, the observed *maximum* plus
      headroom, capped at the ceiling. A one- or two-sample median understates
      the spread it was drawn from, and under-reserving is the failure that
      refuses a granted cycle at dispatch, so the sparse rung leans high — but
      it still describes cost, and it is still bounded by what the panel could
      possibly spend;
    * with nothing observed at all, the configured ceiling. That is a genuinely
      unobserved installation, which is the only circumstance in which a
      permission is the best available description of a cost.
    """
    usable = [float(s) for s in samples if s is not None and float(s) > 0.0]
    if not usable:
        return ReviewCyclePlanningPrice(
            planned_cost_usd=round(configured_ceiling_usd, 4),
            basis=BASIS_REVIEW_CEILING_FALLBACK,
            fallback_configured_usd=configured_ceiling_usd,
            sample_count=0,
            reason=(
                "no measured review cycle in recorded history — "
                "priced at the reviewer ceiling sum as an unobserved installation"
            ),
            excluded_for_taint=excluded_for_taint,
            composition=composition,
        )

    median = percentile(usable, 0.5)
    p90 = percentile(usable, 0.9)
    observed_max = max(usable)

    if len(usable) < min_samples:
        planned = min(
            max(observed_max * headroom_multiplier, MIN_REVIEW_CYCLE_PRICE_USD),
            configured_ceiling_usd,
        )
        return ReviewCyclePlanningPrice(
            planned_cost_usd=round(planned, 4),
            basis=BASIS_OBSERVED_SPARSE,
            fallback_configured_usd=configured_ceiling_usd,
            sample_count=len(usable),
            median_usd=median,
            p90_usd=p90,
            max_usd=observed_max,
            headroom_multiplier=headroom_multiplier,
            reason=(
                f"derived from maximum observed review-cycle spend ${observed_max:.2f} "
                f"x {headroom_multiplier} headroom over {len(usable)} cycle(s)"
                f"{scope_suffix}, below the {min_samples}-cycle floor "
                f"(capped at the ceiling sum)"
            ),
            excluded_for_taint=excluded_for_taint,
            composition=composition,
        )

    planned = min(
        max(median * headroom_multiplier, MIN_REVIEW_CYCLE_PRICE_USD),
        configured_ceiling_usd,
    )
    return ReviewCyclePlanningPrice(
        planned_cost_usd=round(planned, 4),
        basis=derived_basis,
        fallback_configured_usd=configured_ceiling_usd,
        sample_count=len(usable),
        median_usd=median,
        p90_usd=p90,
        max_usd=observed_max,
        headroom_multiplier=headroom_multiplier,
        reason=(
            f"derived from median observed review-cycle spend ${median:.2f} "
            f"x {headroom_multiplier} headroom over {len(usable)} cycle(s){scope_suffix}"
        ),
        excluded_for_taint=excluded_for_taint,
        composition=composition,
    )


def composition_key(names: list[str] | tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Return the order-independent identity of a reviewer panel.

    A panel is the set of participants, not the order they were listed in, so
    the key is sorted. ``None`` when no panel is identifiable, which keeps an
    unknown composition from silently colliding with an empty one.
    """
    if not names:
        return None
    cleaned = sorted(str(n).strip() for n in names if str(n or "").strip())
    return tuple(cleaned) or None


def _record_cycle_compositions(rec: dict) -> dict:
    """Map cycle number → panel identity for one audit record.

    The per-cycle review entries carry the panel that actually ran that cycle,
    which is what makes this a join rather than an assumption: a story whose
    panel changed between cycles contributes each cycle to its own composition.
    """
    compositions: dict = {}
    reviews = rec.get("reviews")
    if not isinstance(reviews, list):
        return compositions
    for review in reviews:
        if not isinstance(review, dict):
            continue
        key = composition_key(review.get("pool_models"))
        if key is not None:
            compositions[review.get("cycle")] = key
    return compositions


def _extract_review_cycle_cost_samples(
    records: list[dict],
    *,
    composition: tuple[str, ...] | None = None,
) -> tuple[list[float], int]:
    """Return measured per-cycle review costs from prior audit records.

    With ``composition``, only cycles run by that exact panel are returned. A
    cycle whose panel cannot be identified is excluded from a composition query
    — an unjoinable cycle is not evidence about any particular panel — but is
    still counted in the unfiltered population, where it remains evidence about
    review as an activity.
    """
    samples: list[float] = []
    excluded_for_taint = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("trust_status") or "").lower() == "tainted":
            excluded_for_taint += 1
            continue
        iterations = rec.get("iterations")
        if not isinstance(iterations, dict):
            continue
        review_loop = iterations.get("review_loop")
        if not isinstance(review_loop, list):
            continue
        compositions = _record_cycle_compositions(rec) if composition is not None else {}
        for cycle in review_loop:
            if not isinstance(cycle, dict):
                continue
            try:
                cost = float(cycle.get("cost_usd"))
            except (TypeError, ValueError):
                continue
            if cost <= 0.0:
                continue
            if composition is not None and compositions.get(cycle.get("iteration")) != composition:
                continue
            samples.append(cost)
    return samples, excluded_for_taint


def derive_review_cycle_planning_price(
    project_root: Path,
    *,
    configured_ceiling_usd: float,
    composition: list[str] | tuple[str, ...] | None = None,
    min_samples: int = REVIEW_CYCLE_PRICE_MIN_SAMPLES,
    headroom_multiplier: float = REVIEW_CYCLE_PRICE_HEADROOM,
) -> ReviewCyclePlanningPrice:
    """Read observed review-cycle spend from the substrate and derive one price.

    Descends the ladder most-specific first. The panel a story will actually
    seat is the best predictor of what its cycles cost — a three-reviewer panel
    and a two-reviewer panel are different quantities, and averaging them
    misprices both — so that history governs whenever it can support a price.
    Below the floor the same phase's cost across every panel still describes
    review, and only a total absence of observation reaches the ceiling.
    """
    from theforge.coordinator import audit_substrate  # noqa: PLC0415

    records: list[dict] = []
    reason_prefix = ""
    substrate = audit_substrate.substrate_path(project_root)
    if not substrate.exists() and not audit_substrate.has_audit_inputs(project_root):
        reason_prefix = "no audit substrate; "
    else:
        try:
            conn = audit_substrate.require_substrate(project_root)
        except audit_substrate.SubstrateError as exc:
            reason_prefix = f"substrate unreadable ({type(exc).__name__}); "
        else:
            try:
                records = [
                    rec
                    for rec in audit_substrate.tail_records(conn, REVIEW_COST_HISTORY_TAIL)
                    if isinstance(rec, dict)
                ]
            finally:
                conn.close()

    panel = composition_key(composition)
    planning: ReviewCyclePlanningPrice | None = None

    if panel is not None:
        panel_samples, panel_excluded = _extract_review_cycle_cost_samples(
            records, composition=panel
        )
        if len(panel_samples) >= min_samples:
            planning = review_cycle_planning_from_samples(
                panel_samples,
                configured_ceiling_usd,
                excluded_for_taint=panel_excluded,
                min_samples=min_samples,
                headroom_multiplier=headroom_multiplier,
                derived_basis=BASIS_OBSERVED_COMPOSITION,
                scope_suffix=" run by this exact panel",
                composition=panel,
            )

    if planning is None:
        samples, excluded = _extract_review_cycle_cost_samples(records)
        planning = review_cycle_planning_from_samples(
            samples,
            configured_ceiling_usd,
            excluded_for_taint=excluded,
            min_samples=min_samples,
            headroom_multiplier=headroom_multiplier,
            composition=panel,
        )

    if reason_prefix:
        from dataclasses import replace  # noqa: PLC0415

        planning = replace(planning, reason=reason_prefix + planning.reason)
    return planning


def evaluate_allocation_dict(allocation: dict | None, observed_usd: float | None) -> dict | None:
    """Return the reportable allocation-vs-observed block for a story.

    ``allocation`` is the serialized record carried on run state (the same shape
    :meth:`StoryAllocation.as_dict` produces), so the same evaluation runs from
    the coordinator, the audit writer and the sprint reporter without any of
    them re-deriving the band.

    ``observed_usd`` is ``None`` when any phase of the run was unmeasured — the
    status is then ``cost_unknown``, which is distinct from both a story that
    stayed within its allocation and one that exceeded it.
    """
    if not allocation:
        return None
    block = dict(allocation)
    try:
        allocation_usd = float(block.get("allocation_usd"))
    except (TypeError, ValueError):
        return block
    block["observed_usd"] = None if observed_usd is None else round(float(observed_usd), 4)
    if observed_usd is None:
        block["status"] = STATUS_UNKNOWN
        block["exceeded"] = None
        return block
    exceeded = float(observed_usd) > allocation_usd
    block["status"] = STATUS_EXCEEDED if exceeded else STATUS_WITHIN
    block["exceeded"] = exceeded
    if exceeded:
        block["overrun_usd"] = round(float(observed_usd) - allocation_usd, 4)
    return block


def evaluate_allocation(allocation: StoryAllocation, observed_usd: float | None) -> dict:
    """Typed convenience wrapper over :func:`evaluate_allocation_dict`."""
    return evaluate_allocation_dict(allocation.as_dict(), observed_usd) or {}


def _balance(value: float) -> float:
    """Round a dollar balance to the precision the funding payloads report.

    Every one of these checks subtracts one dollar figure from another and then
    decides whether work runs. In binary floating point ``4.12 - 1.01`` is
    ``3.1100000000000003``, so a phase that has spent its pool to the cent is
    left holding ``3e-16`` — and a balance of nothing reads as a balance
    remaining. Comparing at the 4-decimal precision the records already report
    makes the boundary land where the operator sees it land: exactly-exhausted
    is exhausted (#2258).
    """
    return round(float(value), 4)


def phase_funding_shortfall(
    allocation: dict | None,
    observed_usd: float | None,
    *,
    phase: str,
    participants: list[str],
    planned_usd: float,
) -> dict | None:
    """Return a shortfall record when ``phase`` cannot be funded, else ``None``.

    The check is made BEFORE the phase runs, against the participants the run
    planned to use. Running the phase with fewer participants than planned is
    precisely the silent reduction this replaces: either the whole planned set
    is funded, or the run reports that it is not.

    ``observed_usd`` of ``None`` (cost-unknown) never produces a shortfall — an
    unmeasured total is a lower bound, and refusing to run a phase on a number
    the run does not actually have would be a guess.
    """
    if not allocation or observed_usd is None or not participants:
        return None
    try:
        allocation_usd = float(allocation.get("allocation_usd"))
    except (TypeError, ValueError):
        return None
    remaining = _balance(allocation_usd - float(observed_usd))
    if remaining >= _balance(planned_usd):
        return None
    return {
        "phase": phase,
        "participants": list(participants),
        "planned_usd": round(planned_usd, 4),
        "observed_usd": round(float(observed_usd), 4),
        "allocation_usd": round(allocation_usd, 2),
        "remaining_usd": remaining,
        "basis": allocation.get("basis"),
        "complexity_score": allocation.get("complexity_score"),
        "median_usd": allocation.get("median_usd"),
        "p90_usd": allocation.get("p90_usd"),
        "max_usd": allocation.get("max_usd"),
        "sample_count": allocation.get("sample_count"),
    }


# ── Holding the seated review reservation across phases (#2258) ───────────────
# The seating reconciliation commits part of the allocation to verification.
# These two helpers are the halves of enforcing that commitment: REVIEW funds
# from the reserved balance, and DEV stops once everything that was NOT reserved
# has been spent. Both keep the cost-unknown rule the dispatch-time check
# already holds — an unmeasured total is a lower bound, and refusing work on a
# number the run does not have would be a guess.


def _reserved_review_usd(reservation: dict | None) -> float:
    """The reserved review balance on a reservation record, or 0.0."""
    if not isinstance(reservation, dict):
        return 0.0
    try:
        return max(0.0, float(reservation.get("reserved_review_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def reserved_review_shortfall(
    reservation: dict | None,
    allocation: dict | None,
    *,
    observed_usd: float | None,
    review_observed_usd: float | None,
    participants: list[str],
    planned_usd: float,
) -> dict | None:
    """Return a shortfall when a review panel can be funded from neither pool.

    A seated reservation is a floor under verification, not a ceiling on it: the
    panel runs if the reserved balance still covers it, and — for cycles beyond
    the reserved ones — if what is left of the whole allocation covers it. Only
    when both are short is the panel refused, so a story whose dev phase stayed
    within its estimate keeps every review cycle it has always had.

    ``review_observed_usd`` of ``None`` leaves the reserved balance unknowable,
    and ``observed_usd`` of ``None`` does the same for the general pool; neither
    refuses the panel.
    """
    reserved = _reserved_review_usd(reservation)
    if reserved <= 0.0 or not participants:
        # No reservation to draw on: the caller's pre-existing dispatch-time
        # check against the whole allocation is the honest answer.
        return phase_funding_shortfall(
            allocation,
            observed_usd,
            phase="review",
            participants=participants,
            planned_usd=planned_usd,
        )
    if review_observed_usd is None:
        return None
    reserved_remaining = _balance(reserved - float(review_observed_usd))
    if reserved_remaining >= _balance(planned_usd):
        return None
    shortfall = phase_funding_shortfall(
        allocation,
        observed_usd,
        phase="review",
        participants=participants,
        planned_usd=planned_usd,
    )
    if shortfall is None:
        return None
    shortfall["reserved_review_usd"] = _balance(reserved)
    shortfall["reserved_review_cycles"] = (reservation or {}).get("reserved_review_cycles")
    shortfall["reserved_review_remaining_usd"] = reserved_remaining
    return shortfall


def nonreview_funding_exhausted(
    reservation: dict | None,
    allocation: dict | None,
    *,
    observed_usd: float | None,
    review_observed_usd: float | None,
    participants: list[str],
    phase: str = "dev",
) -> dict | None:
    """Return a shortfall when the non-reserved part of the allocation is gone.

    The other half of holding a reservation: money committed to review must not
    be spendable by an earlier phase, so once non-review spend has reached
    ``allocation - reserved``, no further attempt at ``phase`` is funded. The
    reserved balance is deliberately excluded — it is still there, and it is
    still review's.

    Returns ``None`` — funded — whenever there is no reservation to protect or
    spend is unmeasured. The payload reuses the :func:`phase_funding_shortfall`
    shape so every existing consumer reads it unchanged, and carries
    ``nonreview_exhausted`` so the operator line can say which pool ran out.
    """
    reserved = _reserved_review_usd(reservation)
    if reserved <= 0.0 or observed_usd is None or review_observed_usd is None:
        return None
    try:
        allocation_usd = float((allocation or {}).get("allocation_usd"))
    except (TypeError, ValueError, AttributeError):
        return None
    nonreview_observed = _balance(max(0.0, float(observed_usd) - float(review_observed_usd)))
    ceiling = _balance(allocation_usd - reserved)
    remaining = _balance(ceiling - nonreview_observed)
    # Strictly greater: a non-review pool spent to the cent has nothing left to
    # fund another attempt with, and admitting one would spend the reserved
    # review balance — the whole point of holding it.
    if remaining > 0:
        return None
    return {
        "phase": phase,
        "participants": list(participants),
        "planned_usd": 0.0,
        "observed_usd": nonreview_observed,
        "allocation_usd": round(allocation_usd, 2),
        "remaining_usd": remaining,
        "nonreview_exhausted": True,
        "nonreview_allocation_usd": ceiling,
        "reserved_review_usd": _balance(reserved),
        "reserved_review_cycles": (reservation or {}).get("reserved_review_cycles"),
        "total_observed_usd": round(float(observed_usd), 4),
        "basis": (allocation or {}).get("basis"),
        "complexity_score": (allocation or {}).get("complexity_score"),
        "median_usd": (allocation or {}).get("median_usd"),
        "p90_usd": (allocation or {}).get("p90_usd"),
        "max_usd": (allocation or {}).get("max_usd"),
        "sample_count": (allocation or {}).get("sample_count"),
    }


# ── Seating-time reconciliation of permissions against the allocation (#2238) ──
# Actions a reconciliation can report. The first four are no-ops: the inputs do
# not admit an honest answer, so the requested permission stands and the record
# says why nothing was decided.
RECONCILE_NO_ALLOCATION = "no_allocation"
RECONCILE_NO_BAND_HISTORY = "no_band_history"
RECONCILE_NO_DEV_ESTIMATE = "no_dev_estimate"
RECONCILE_NO_REVIEW_COST = "no_review_cost"
RECONCILE_COST_UNKNOWN = "cost_unknown"
RECONCILE_NONCOMPARABLE_DEV_ESTIMATE = "dev_estimate_not_comparable"
RECONCILE_AFFORDABLE = "within_allocation"
RECONCILE_REDUCED = "review_max_reduced"
RECONCILE_UNFUNDABLE = "review_unfundable"


def reconcile_review_cycles(
    allocation: dict | None,
    *,
    dev_cost_estimate_usd: float | None,
    dev_cost_estimate_basis: dict | None = None,
    review_cycle_cost_usd: float | None,
    review_cycle_planning: dict | None = None,
    requested_review_max: int,
    spent_so_far_usd: float | None = 0.0,
) -> dict:
    """Reconcile permitted review cycles against what the allocation can fund.

    Pure arithmetic over numbers that are all known at seating time: the
    allocation, the spend already incurred (preflight/plan/plan-review), the
    adaptive dev cost estimate, and the per-cycle cost of the review panel the
    run plans to seat. Returns the audit record of the decision; the caller
    applies it.

    ``reserved_review_usd`` is the portion of the allocation the reconciliation
    commits to verification: the reconciled cycles priced at the seated
    per-cycle figure. A projection that nothing withholds is an intention, not a
    guarantee — an earlier phase that overruns spends the money review was
    seated with and review is refused anyway (#2258). Reserving it here gives
    the caller a number to hold: REVIEW funds from it, and further DEV attempts
    are refused once the rest of the allocation is gone. Nothing is reserved on
    the no-op actions or on ``review_unfundable`` — there is no decision to
    hold in the first place.

    ``reconciled_review_max`` is the number of review cycles the allocation can
    actually fund. It equals ``requested_review_max`` whenever the inputs do not
    support a decision — an absent allocation, a configured-fallback allocation,
    an absent dev estimate, a zero per-cycle review cost, or cost-unknown spend
    are recorded as explicit no-ops rather than guessed at. ``action`` is
    ``review_unfundable`` when not even one cycle fits, which is the case the
    caller must refuse before dev spends.

    Only a band-derived allocation governs here. The configured fallback is by
    construction one pass through every role (see ``_configured_story_budget``),
    so it funds exactly one review cycle for arithmetic reasons that say nothing
    about this story — reconciling against it would clamp verification to a
    single cycle on every story in a project with no band history. That is a
    guess dressed as evidence, and the pre-existing dispatch-time guard already
    covers the fallback case honestly, against measured spend.
    """
    record: dict = {
        "requested_review_max": int(requested_review_max),
        "reconciled_review_max": int(requested_review_max),
        "affordable_review_cycles": None,
        "allocation_usd": None,
        "spent_so_far_usd": None
        if spent_so_far_usd is None
        else round(float(spent_so_far_usd), 4),
        "dev_cost_estimate_usd": None,
        "review_cycle_cost_usd": None,
        "review_cycle_cost_basis": None,
        "review_cycle_cost_sample_count": None,
        "review_cycle_cost_median_usd": None,
        "review_cycle_cost_p90_usd": None,
        "review_cycle_cost_max_usd": None,
        "review_cycle_cost_headroom_multiplier": None,
        "review_cycle_cost_reason": None,
        "review_cycle_cost_composition": None,
        "dev_cost_estimate_basis": None,
        "dev_cost_estimate_basis_reason": None,
        "dev_cost_estimate_statistic": None,
        "dev_cost_estimate_scope": None,
        "dev_cost_estimate_band": None,
        "dev_cost_estimate_complexity_score": None,
        "dev_cost_estimate_sample_count": None,
        "dev_cost_estimate_headroom_multiplier": None,
        "dev_cost_estimate_headroom_basis": None,
        "dev_cost_estimate_comparable": None,
        "remaining_after_dev_usd": None,
        "reserved_review_cycles": 0,
        "reserved_review_usd": 0.0,
        "action": RECONCILE_AFFORDABLE,
    }
    try:
        allocation_usd = float((allocation or {}).get("allocation_usd"))
    except (TypeError, ValueError, AttributeError):
        record["action"] = RECONCILE_NO_ALLOCATION
        return record
    record["allocation_usd"] = round(allocation_usd, 2)
    record["basis"] = (allocation or {}).get("basis")
    # The population the allocation prices. Recorded so a reader can see which
    # score the dev estimate had to match to be subtracted from it.
    record["complexity_score"] = (allocation or {}).get("complexity_score")

    if record["basis"] != BASIS_SUBSTRATE_BAND:
        record["action"] = RECONCILE_NO_BAND_HISTORY
        return record

    if spent_so_far_usd is None:
        # An unmeasured total is a lower bound. Reducing a permission on a
        # number the run does not actually have would be a guess.
        record["action"] = RECONCILE_COST_UNKNOWN
        return record

    try:
        dev_estimate = float(dev_cost_estimate_usd)
    except (TypeError, ValueError):
        dev_estimate = 0.0
    if dev_estimate <= 0.0:
        record["action"] = RECONCILE_NO_DEV_ESTIMATE
        return record
    record["dev_cost_estimate_usd"] = round(dev_estimate, 4)
    if isinstance(dev_cost_estimate_basis, dict):
        record["dev_cost_estimate_basis"] = dev_cost_estimate_basis.get("source")
        record["dev_cost_estimate_basis_reason"] = dev_cost_estimate_basis.get("reason")
        record["dev_cost_estimate_statistic"] = dev_cost_estimate_basis.get("statistic")
        record["dev_cost_estimate_scope"] = dev_cost_estimate_basis.get("scope")
        record["dev_cost_estimate_band"] = dev_cost_estimate_basis.get("band")
        record["dev_cost_estimate_complexity_score"] = dev_cost_estimate_basis.get(
            "complexity_score"
        )
        record["dev_cost_estimate_sample_count"] = dev_cost_estimate_basis.get("sample_count")
        record["dev_cost_estimate_headroom_multiplier"] = dev_cost_estimate_basis.get(
            "headroom_multiplier"
        )
        record["dev_cost_estimate_headroom_basis"] = dev_cost_estimate_basis.get("headroom_basis")
        record["dev_cost_estimate_comparable"] = dev_cost_estimate_basis.get(
            "allocation_comparable"
        )

    try:
        cycle_cost = float(review_cycle_cost_usd)
    except (TypeError, ValueError):
        cycle_cost = 0.0
    if cycle_cost <= 0.0:
        record["action"] = RECONCILE_NO_REVIEW_COST
        return record
    record["review_cycle_cost_usd"] = round(cycle_cost, 4)
    if isinstance(review_cycle_planning, dict):
        record["review_cycle_cost_basis"] = review_cycle_planning.get("basis")
        record["review_cycle_cost_sample_count"] = review_cycle_planning.get("sample_count")
        record["review_cycle_cost_median_usd"] = review_cycle_planning.get("median_usd")
        record["review_cycle_cost_p90_usd"] = review_cycle_planning.get("p90_usd")
        record["review_cycle_cost_max_usd"] = review_cycle_planning.get("max_usd")
        record["review_cycle_cost_headroom_multiplier"] = review_cycle_planning.get(
            "headroom_multiplier"
        )
        record["review_cycle_cost_reason"] = review_cycle_planning.get("reason")
        record["review_cycle_cost_composition"] = review_cycle_planning.get("composition")

    if requested_review_max <= 0:
        record["action"] = RECONCILE_AFFORDABLE
        return record

    if not _dev_estimate_comparable_to_allocation(dev_cost_estimate_basis, allocation):
        # Two figures drawn from different populations do not have a difference
        # worth acting on. Record the decision and change nothing: the requested
        # permission stands, nothing is reserved, and DEV runs (#2284).
        record["action"] = RECONCILE_NONCOMPARABLE_DEV_ESTIMATE
        return record

    remaining = _balance(allocation_usd - float(spent_so_far_usd) - dev_estimate)
    record["remaining_after_dev_usd"] = remaining
    # Counted in whole units of the reported precision rather than by float
    # division: a remainder that covers a cycle exactly must count as a cycle,
    # or seating refuses to reserve money the allocation demonstrably holds.
    _remaining_units = int(round(remaining * 10000))
    _cycle_units = int(round(cycle_cost * 10000))
    affordable = 0 if _remaining_units < _cycle_units else _remaining_units // _cycle_units
    record["affordable_review_cycles"] = affordable
    if affordable == 0:
        record["reconciled_review_max"] = int(requested_review_max)
        record["action"] = RECONCILE_UNFUNDABLE
        record["shortfall_usd"] = round(cycle_cost - remaining, 4)
        return record
    if affordable >= requested_review_max:
        record["action"] = RECONCILE_AFFORDABLE
        record["reserved_review_cycles"] = int(requested_review_max)
        record["reserved_review_usd"] = round(requested_review_max * cycle_cost, 4)
        return record
    record["reconciled_review_max"] = affordable
    record["action"] = RECONCILE_REDUCED
    record["reserved_review_cycles"] = affordable
    record["reserved_review_usd"] = round(affordable * cycle_cost, 4)
    record["shortfall_usd"] = round(
        (requested_review_max - affordable) * cycle_cost - (remaining - affordable * cycle_cost),
        4,
    )
    return record


def _dev_estimate_comparable_to_allocation(
    dev_cost_estimate_basis: dict | None,
    allocation: dict | None,
) -> bool:
    """Whether the dev estimate describes the population the allocation prices.

    Subtracting one figure from another asserts they measure the same thing.
    The allocation is the observed cost distribution at ONE complexity score
    across ALL models, with :data:`BAND_HEADROOM` on top. An estimate may be
    subtracted from it only when it is drawn from that same population and
    carries that same caution — and when the score it was drawn at is the score
    this allocation was drawn at. Everything else (a band-and-model average, a
    configured fallback, an estimate carried over from a differently-scored
    attempt) is recorded and left alone: an incommensurable subtraction resolves
    in one direction, and the phase estimated first eats the rest of the story.
    """
    if not isinstance(dev_cost_estimate_basis, dict):
        return False
    if dev_cost_estimate_basis.get("source") != DEV_ESTIMATE_SOURCE_SCORE:
        return False
    if dev_cost_estimate_basis.get("statistic") != "avg_cost_usd":
        return False
    if dev_cost_estimate_basis.get("headroom_basis") != DEV_ESTIMATE_HEADROOM_BASIS_ALLOCATION:
        return False
    if not dev_cost_estimate_basis.get("allocation_comparable"):
        return False
    try:
        if float(dev_cost_estimate_basis.get("headroom_multiplier")) != BAND_HEADROOM:
            return False
    except (TypeError, ValueError):
        return False
    try:
        basis_score = int(dev_cost_estimate_basis.get("complexity_score"))
        allocation_score = int((allocation or {}).get("complexity_score"))
    except (TypeError, ValueError):
        return False
    return basis_score == allocation_score


def _format_dev_estimate_basis(record: dict) -> str:
    source = record.get("dev_cost_estimate_basis")
    score = record.get("dev_cost_estimate_complexity_score")
    samples = record.get("dev_cost_estimate_sample_count")
    sample_text = f" over {int(samples)} run(s)" if isinstance(samples, (int, float)) else ""
    if source == DEV_ESTIMATE_SOURCE_SCORE:
        return f"score-{score} average across all models{sample_text}"
    if source == DEV_ESTIMATE_SOURCE_BAND:
        band = record.get("dev_cost_estimate_band")
        return (
            f"{band or 'unknown'}-band, single-model average{sample_text}"
            f"{f'; scored {score}' if score is not None else ''}"
        )
    if source == DEV_ESTIMATE_SOURCE_CONFIGURED:
        reason = record.get("dev_cost_estimate_basis_reason")
        return f"configured fallback{': ' + str(reason) if reason else ''}"
    if source:
        return str(source)
    return "no recorded basis"


def _review_cycle_basis_text(record: dict) -> str:
    """Name the rung a review-cycle price came from, for the operator line.

    An operator reading a reservation needs to know whether it was measured on
    the panel that will run, inferred from review at large, stretched from one
    or two observations, or defaulted — because that is what decides whether
    the number is worth trusting.
    """
    basis = record.get("review_cycle_cost_basis")
    headroom = record.get("review_cycle_cost_headroom_multiplier")
    sample_count = record.get("review_cycle_cost_sample_count")
    sample_text = ""
    if isinstance(sample_count, (int, float)):
        sample_text = f" over {int(sample_count)} cycle(s)"

    if basis in (BASIS_OBSERVED_COMPOSITION, BASIS_OBSERVED_REVIEW_CYCLE):
        median = record.get("review_cycle_cost_median_usd")
        if median is not None and headroom is not None:
            scope = "this panel" if basis == BASIS_OBSERVED_COMPOSITION else "review at large"
            return (
                f" (observed median ${float(median):.2f} x "
                f"{float(headroom):.2f} headroom{sample_text}, {scope})"
            )
    if basis == BASIS_OBSERVED_SPARSE:
        observed_max = record.get("review_cycle_cost_max_usd")
        if observed_max is not None and headroom is not None:
            return (
                f" (sparse history: observed max ${float(observed_max):.2f} x "
                f"{float(headroom):.2f} headroom{sample_text}, capped at the ceiling sum)"
            )
    if basis == BASIS_REVIEW_CEILING_FALLBACK:
        reason = str(record.get("review_cycle_cost_reason") or "").strip()
        return f" (ceiling fallback{': ' + reason if reason else ''})"
    return ""


def format_reconciliation(record: dict) -> str:
    """One-line operator-facing summary of a seating reconciliation."""
    action = record.get("action")
    basis = _review_cycle_basis_text(record)
    if action == RECONCILE_REDUCED:
        return (
            f"review_max reduced {record['requested_review_max']} → "
            f"{record['reconciled_review_max']}: the "
            f"${float(record['allocation_usd']):.2f} allocation funds "
            f"{record['affordable_review_cycles']} review cycle(s) at "
            f"${float(record['review_cycle_cost_usd']):.2f} each{basis} after a "
            f"${float(record['dev_cost_estimate_usd']):.2f} dev estimate."
        )
    if action == RECONCILE_UNFUNDABLE:
        return (
            f"the ${float(record['allocation_usd']):.2f} allocation cannot fund one "
            f"${float(record['review_cycle_cost_usd']):.2f} review cycle{basis} after a "
            f"${float(record['dev_cost_estimate_usd']):.2f} dev estimate "
            f"(short ${float(record['shortfall_usd']):.2f})."
        )
    if action == RECONCILE_NONCOMPARABLE_DEV_ESTIMATE:
        # Deliberately not a dollar shortfall. Naming a figure here would invite
        # an operator to raise a budget that was never the constraint — the
        # constraint is that the two numbers do not describe the same runs.
        allocation_score = record.get("complexity_score")
        return (
            f"review_max left at {record['requested_review_max']}: the dev estimate and the "
            f"allocation are not on a common population, so they were not subtracted. The "
            f"allocation prices complexity score "
            f"{allocation_score if allocation_score is not None else '?'}; the "
            f"${float(record['dev_cost_estimate_usd']):.2f} dev estimate is a "
            f"{_format_dev_estimate_basis(record)}. Nothing reserved for review."
        )
    if action == RECONCILE_AFFORDABLE:
        return f"allocation funds the permitted {record['requested_review_max']} review cycle(s)."
    return f"review-cycle reconciliation not decided ({action})."


def seating_shortfall(
    allocation: dict | None,
    record: dict,
    *,
    participants: list[str],
) -> dict | None:
    """Build the allocation-exhausted payload for an unfundable seating.

    Reuses the shape :func:`phase_funding_shortfall` produces so every existing
    consumer (sprint audit, status reader, run record) reads it unchanged. The
    "observed" figure is the projected spend at the point review would be
    reached — the already-incurred spend plus the dev estimate — and the record
    says so via ``projected`` so it is never mistaken for measured cost.
    """
    if record.get("action") != RECONCILE_UNFUNDABLE:
        return None
    projected = float(record.get("spent_so_far_usd") or 0.0) + float(
        record.get("dev_cost_estimate_usd") or 0.0
    )
    shortfall = phase_funding_shortfall(
        allocation,
        projected,
        phase="review",
        participants=list(participants),
        planned_usd=float(record["review_cycle_cost_usd"]),
    )
    if shortfall is None:  # pragma: no cover - unfundable implies a shortfall
        return None
    shortfall["projected"] = True
    shortfall["seating_reconciliation"] = dict(record)
    return shortfall


def format_shortfall(shortfall: dict, *, story: str | None = None) -> str:
    """Render the operator-facing allocation-exhausted message."""
    story_label = f"story {story}" if story else "story"
    if shortfall.get("nonreview_exhausted"):
        cycles = shortfall.get("reserved_review_cycles")
        return (
            f"Story allocation exhausted: {story_label} has spent "
            f"${float(shortfall['observed_usd']):.2f} of the "
            f"${float(shortfall['nonreview_allocation_usd']):.2f} its "
            f"${float(shortfall['allocation_usd']):.2f} allocation leaves for non-review "
            f"work, so no further {shortfall['phase']} attempt is funded. The remaining "
            f"${float(shortfall['reserved_review_usd']):.2f} is reserved for the "
            f"{cycles} review cycle(s) this story was seated with and is not "
            "available to spend here. Sprint headroom is reported alongside this "
            "story in the sprint summary."
        )
    expected = "no band history"
    if shortfall.get("median_usd") is not None:
        expected = (
            f"median ${float(shortfall['median_usd']):.2f} / "
            f"p90 ${float(shortfall['p90_usd']):.2f} / "
            f"max ${float(shortfall['max_usd']):.2f} over "
            f"{shortfall.get('sample_count')} run(s)"
        )
    observed_label = "projected" if shortfall.get("projected") else "observed"
    seating = ""
    if shortfall.get("projected"):
        seating = (
            " Decided at seating, before dev spent: the permitted review cycles "
            "cost more than the allocation leaves after the dev estimate."
        )
    return (
        f"Story allocation exhausted: {story_label} cannot fund its planned "
        f"{shortfall['phase']} participants "
        f"({', '.join(shortfall['participants'])}) — needs "
        f"${shortfall['planned_usd']:.2f}, ${shortfall['remaining_usd']:.2f} left of the "
        f"${shortfall['allocation_usd']:.2f} allocation "
        f"(complexity score {shortfall.get('complexity_score')}, basis "
        f"{shortfall.get('basis')}, expected {expected}); {observed_label} "
        f"${shortfall['observed_usd']:.2f}.{seating} Sprint headroom is reported "
        f"alongside this story in the sprint summary."
    )


def scale_role_budgets(
    current: dict[str, float],
    allocation_usd: float,
    *,
    locked: set[str] | None = None,
    floor_usd: float = 0.05,
) -> dict[str, float]:
    """Proportionally rescale per-role budgets so their sum is the allocation.

    ``locked`` roles (explicit forge.yaml overrides) keep their configured
    budget; the remainder of the allocation is divided among the rest in the
    same proportions they already had. Returns a new mapping — never mutates
    ``current``. An empty/zero input is returned unchanged, since there is
    nothing to scale proportionally.
    """
    locked = locked or set()
    scalable = {name: value for name, value in current.items() if name not in locked}
    scalable_total = sum(scalable.values())
    if not scalable or scalable_total <= 0:
        return dict(current)
    locked_total = sum(value for name, value in current.items() if name in locked)
    remaining = allocation_usd - locked_total
    if remaining <= 0:
        # The locked roles alone already consume the allocation. Keep the
        # scalable roles at the floor rather than at zero: a zero budget is an
        # instruction to run nothing, which is precisely the silent reduction
        # this allocation exists to prevent.
        return {name: (current[name] if name in locked else floor_usd) for name in current}
    factor = remaining / scalable_total
    out: dict[str, float] = {}
    for name, value in current.items():
        if name in locked:
            out[name] = value
        else:
            out[name] = max(round(value * factor, 4), floor_usd)
    return out
