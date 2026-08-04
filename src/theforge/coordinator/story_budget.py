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

BASIS_SUBSTRATE_BAND = "substrate_band"
BASIS_CONFIGURED_FALLBACK = "configured_fallback"

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
    return (
        f"Story allocation exhausted: {story_label} cannot fund its planned "
        f"{shortfall['phase']} participants "
        f"({', '.join(shortfall['participants'])}) — needs "
        f"${shortfall['planned_usd']:.2f}, ${shortfall['remaining_usd']:.2f} left of the "
        f"${shortfall['allocation_usd']:.2f} allocation "
        f"(complexity score {shortfall.get('complexity_score')}, basis "
        f"{shortfall.get('basis')}, expected {expected}); observed "
        f"${shortfall['observed_usd']:.2f}. Sprint headroom is reported "
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
