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

BASIS_SUBSTRATE_BAND = "substrate_band"
BASIS_CONFIGURED_FALLBACK = "configured_fallback"
BASIS_OBSERVED_REVIEW_CYCLE = "observed_review_cycle"
BASIS_REVIEW_CEILING_FALLBACK = "ceiling_fallback"

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

    @property
    def derived(self) -> bool:
        return self.basis == BASIS_OBSERVED_REVIEW_CYCLE

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
) -> ReviewCyclePlanningPrice:
    """Return the deterministic planning price for one review cycle."""
    usable = [float(s) for s in samples if s is not None and float(s) > 0.0]
    if len(usable) < min_samples:
        fallback_reason = (
            f"only {len(usable)} measured review cycle(s), below the {min_samples}-cycle floor"
        )
        return ReviewCyclePlanningPrice(
            planned_cost_usd=round(configured_ceiling_usd, 4),
            basis=BASIS_REVIEW_CEILING_FALLBACK,
            fallback_configured_usd=configured_ceiling_usd,
            sample_count=len(usable),
            reason=fallback_reason,
            excluded_for_taint=excluded_for_taint,
        )

    median = percentile(usable, 0.5)
    p90 = percentile(usable, 0.9)
    observed_max = max(usable)
    planned = max(median * headroom_multiplier, MIN_REVIEW_CYCLE_PRICE_USD)
    return ReviewCyclePlanningPrice(
        planned_cost_usd=round(planned, 4),
        basis=BASIS_OBSERVED_REVIEW_CYCLE,
        fallback_configured_usd=configured_ceiling_usd,
        sample_count=len(usable),
        median_usd=median,
        p90_usd=p90,
        max_usd=observed_max,
        headroom_multiplier=headroom_multiplier,
        reason=(
            f"derived from median observed review-cycle spend ${median:.2f} "
            f"x {headroom_multiplier} headroom over {len(usable)} cycle(s)"
        ),
        excluded_for_taint=excluded_for_taint,
    )


def _extract_review_cycle_cost_samples(records: list[dict]) -> tuple[list[float], int]:
    """Return measured per-cycle review costs from prior audit records."""
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
        for cycle in review_loop:
            if not isinstance(cycle, dict):
                continue
            try:
                cost = float(cycle.get("cost_usd"))
            except (TypeError, ValueError):
                continue
            if cost > 0.0:
                samples.append(cost)
    return samples, excluded_for_taint


def derive_review_cycle_planning_price(
    project_root: Path,
    *,
    configured_ceiling_usd: float,
    min_samples: int = REVIEW_CYCLE_PRICE_MIN_SAMPLES,
    headroom_multiplier: float = REVIEW_CYCLE_PRICE_HEADROOM,
) -> ReviewCyclePlanningPrice:
    """Read observed review-cycle spend from the substrate and derive one price."""
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

    samples, excluded = _extract_review_cycle_cost_samples(records)
    planning = review_cycle_planning_from_samples(
        samples,
        configured_ceiling_usd,
        excluded_for_taint=excluded,
        min_samples=min_samples,
        headroom_multiplier=headroom_multiplier,
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
    remaining = allocation_usd - float(observed_usd)
    if remaining >= planned_usd:
        return None
    return {
        "phase": phase,
        "participants": list(participants),
        "planned_usd": round(planned_usd, 4),
        "observed_usd": round(float(observed_usd), 4),
        "allocation_usd": round(allocation_usd, 2),
        "remaining_usd": round(remaining, 4),
        "basis": allocation.get("basis"),
        "complexity_score": allocation.get("complexity_score"),
        "median_usd": allocation.get("median_usd"),
        "p90_usd": allocation.get("p90_usd"),
        "max_usd": allocation.get("max_usd"),
        "sample_count": allocation.get("sample_count"),
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
RECONCILE_AFFORDABLE = "within_allocation"
RECONCILE_REDUCED = "review_max_reduced"
RECONCILE_UNFUNDABLE = "review_unfundable"


def reconcile_review_cycles(
    allocation: dict | None,
    *,
    dev_cost_estimate_usd: float | None,
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
        "action": RECONCILE_AFFORDABLE,
    }
    try:
        allocation_usd = float((allocation or {}).get("allocation_usd"))
    except (TypeError, ValueError, AttributeError):
        record["action"] = RECONCILE_NO_ALLOCATION
        return record
    record["allocation_usd"] = round(allocation_usd, 2)
    record["basis"] = (allocation or {}).get("basis")

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

    if requested_review_max <= 0:
        record["action"] = RECONCILE_AFFORDABLE
        return record

    remaining = allocation_usd - float(spent_so_far_usd) - dev_estimate
    record["remaining_after_dev_usd"] = round(remaining, 4)
    affordable = 0 if remaining < cycle_cost else int(remaining // cycle_cost)
    record["affordable_review_cycles"] = affordable
    if affordable == 0:
        record["reconciled_review_max"] = int(requested_review_max)
        record["action"] = RECONCILE_UNFUNDABLE
        record["shortfall_usd"] = round(cycle_cost - remaining, 4)
        return record
    if affordable >= requested_review_max:
        record["action"] = RECONCILE_AFFORDABLE
        return record
    record["reconciled_review_max"] = affordable
    record["action"] = RECONCILE_REDUCED
    record["shortfall_usd"] = round(
        (requested_review_max - affordable) * cycle_cost - (remaining - affordable * cycle_cost),
        4,
    )
    return record


def _review_cycle_basis_text(record: dict) -> str:
    basis = record.get("review_cycle_cost_basis")
    if basis == BASIS_OBSERVED_REVIEW_CYCLE:
        median = record.get("review_cycle_cost_median_usd")
        headroom = record.get("review_cycle_cost_headroom_multiplier")
        sample_count = record.get("review_cycle_cost_sample_count")
        if median is not None and headroom is not None:
            sample_text = ""
            if isinstance(sample_count, (int, float)):
                sample_text = f" over {int(sample_count)} cycle(s)"
            return (
                f" (observed median ${float(median):.2f} x "
                f"{float(headroom):.2f} headroom{sample_text})"
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
