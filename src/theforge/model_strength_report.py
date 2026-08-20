"""Declared model strength vs. observed dev behaviour (#2308).

A catalog entry declares ``tier`` and ``capability``; those gate which roles and
complexity bands a model is eligible for. Capability profiles record, per model
and band, how often that model's dev work actually succeeded. The adaptive
router consumes the evidence to *rank within* the eligibility the declaration
produced — nothing compares the evidence back against the declaration itself, so
a wrong declaration is routed around indefinitely rather than corrected.

This module builds that comparison, and only that: it joins live catalog
declarations to profile evidence by canonical model identity and complexity
band, and returns rows. It reads the catalog and the profiles dict; it writes
neither. Acting on a reported disagreement — editing ``tier``/``capability`` —
stays the operator's, which is why nothing here has a write path.

Three distinctions the report exists to keep visible:

- **Unobserved is not agreement.** A model that is never selected produces no
  evidence, and an absence of contradiction must not read as confirmation.
- **Evidence has a size.** Every comparison carries its sample count, and a
  disagreement is only *claimed* above :data:`DEFAULT_EVIDENCE_FLOOR` runs with
  at least :data:`MIN_PEERS_FOR_DISAGREEMENT` comparable peers.
- **Evidence has an owner.** Profile keys that cannot be attributed to a live
  dev-capable catalog identity are reported separately rather than folded into
  some live model's rate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from theforge.config.model_identity import AgentSpec
from theforge.model_profiles_identity import COMPLEXITY_BANDS
from theforge.model_profiles_read_model import (
    get_dev_signal_for_keys,
    list_dev_evidence_keys,
)
from theforge.model_profiles_storage import canonical_id_for_legacy_key

# How many admissible runs a band needs before the report is willing to *claim*
# the evidence disagrees with the declaration. Deliberately well above the
# router's own ``min_runs`` (3): the router ranks continuously and can afford a
# thin sample, whereas this report asks an operator to revisit a declaration.
DEFAULT_EVIDENCE_FLOOR = 10

# How far below the weakest comparable peer an observed rate must sit before the
# gap is called a disagreement rather than ordinary spread within a tier.
UNDERPERFORMANCE_MARGIN = 0.05

# A "peer range" over a single peer is not a range. Below this the report states
# the observation and declines the disagreement claim.
MIN_PEERS_FOR_DISAGREEMENT = 2

STATUS_UNOBSERVED = "unobserved"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_OBSERVED = "observed"
STATUS_UNDERPERFORMING = "underperforming_declaration"
STATUS_UNATTRIBUTED = "unattributed_profile_key"

REASON_UNRESOLVED = "unresolved_identity"
REASON_NOT_IN_CATALOG = "not_in_live_catalog"
REASON_NOT_DEV_CAPABLE = "not_dev_capable"


@dataclass(frozen=True)
class DeclaredStrengthRow:
    """One live dev-capable catalog model at one complexity band."""

    canonical_id: str
    complexity: str
    declared_tier: str
    declared_capability: int
    status: str
    runs: int
    observed_rate: float | None
    # Peer observed range within the same declared tier and band, over peers
    # that themselves clear the evidence floor. ``None`` when no such peer exists.
    peer_low: float | None
    peer_high: float | None
    peer_count: int
    # Stored keys the rate was aggregated from. A canonical key plus a shorthand
    # says the comparison rests partly on history whose recency is unknown.
    contributing_keys: tuple[str, ...] = ()
    # Profiles carry no per-key timestamp, so no row can assert its evidence is
    # current. Stated rather than assumed.
    evidence_recency: str = "unknown"

    @property
    def disagrees(self) -> bool:
        return self.status == STATUS_UNDERPERFORMING


@dataclass(frozen=True)
class UnattributedEvidenceRow:
    """Stored dev evidence that cannot be attributed to a live catalog model."""

    key: str
    reason: str
    canonical_id: str | None
    runs: int
    runs_by_band: dict[str, int] = field(default_factory=dict)
    status: str = STATUS_UNATTRIBUTED


@dataclass(frozen=True)
class ModelStrengthReport:
    rows: tuple[DeclaredStrengthRow, ...]
    unattributed: tuple[UnattributedEvidenceRow, ...]
    evidence_floor: int
    # Live catalog models excluded from the comparison because they are not
    # dev-capable: they accrue no dev evidence by construction, so reporting them
    # as "unobserved" would misread ineligibility as an untested declaration.
    excluded_non_dev_models: tuple[str, ...] = ()

    @property
    def disagreements(self) -> tuple[DeclaredStrengthRow, ...]:
        return tuple(row for row in self.rows if row.disagrees)


def build_model_strength_report(
    *,
    model_registry: dict[str, AgentSpec],
    profiles: dict[str, Any],
    evidence_floor: int = DEFAULT_EVIDENCE_FLOOR,
    recency: Any | None = None,
    resolve_key: Callable[[str, dict], str | None] | None = None,
) -> ModelStrengthReport:
    """Join live catalog declarations to observed dev evidence, per band.

    ``model_registry`` is the *effective* registry from a loaded config
    (``config.model_registry``), so packaged defaults and ``forge.yaml`` overlays
    are both reflected. ``profiles`` is the stored profiles dict. Pure: no disk
    access, no LLM call, and no mutation of either input.

    Every stored key is classified exactly once, by
    :func:`_partition_evidence`: it is either claimed by one live dev-capable
    canonical identity and counted in that model's rows, or it is unattributable
    and reported separately. No key can be both.
    """
    floor = max(1, int(evidence_floor))
    resolver = resolve_key if resolve_key is not None else canonical_id_for_legacy_key

    dev_models = {
        canonical_id: spec
        for canonical_id, spec in (model_registry or {}).items()
        if isinstance(spec, AgentSpec) and spec.routing.dev_capable
    }
    excluded = tuple(
        sorted(
            canonical_id
            for canonical_id, spec in (model_registry or {}).items()
            if isinstance(spec, AgentSpec) and not spec.routing.dev_capable
        )
    )

    claimed, unattributed = _partition_evidence(
        profiles,
        dev_models,
        model_registry or {},
        resolver,
    )
    observations = _observe(dev_models, profiles, claimed, floor=floor, recency=recency)
    peers = _peer_rates(dev_models, observations, floor=floor)

    rows: list[DeclaredStrengthRow] = []
    for canonical_id in sorted(dev_models):
        spec = dev_models[canonical_id]
        for band in COMPLEXITY_BANDS:
            peer_rates = sorted(
                rate
                for peer_id, rate in peers.get((spec.routing.tier, band), {}).items()
                if peer_id != canonical_id
            )
            rows.append(
                _row(
                    canonical_id=canonical_id,
                    spec=spec,
                    band=band,
                    observed=observations[(canonical_id, band)],
                    peer_rates=peer_rates,
                    floor=floor,
                )
            )

    return ModelStrengthReport(
        rows=tuple(rows),
        unattributed=unattributed,
        evidence_floor=floor,
        excluded_non_dev_models=excluded,
    )


def _partition_evidence(
    profiles: dict[str, Any],
    dev_models: dict[str, AgentSpec],
    model_registry: dict[str, AgentSpec],
    resolver: Callable[[str, dict], str | None],
) -> tuple[dict[str, list[dict]], tuple[UnattributedEvidenceRow, ...]]:
    """Assign every stored dev key to one live identity, or to no one.

    Canonical resolution is the *single* classification the report runs on. The
    router's own identity matching is deliberately looser — it matches on
    ``(provider, model)`` so a candidate finds its history under whatever key it
    was recorded with — and reusing it here let a key be counted in a live
    model's rate *and* listed as unattributable evidence in the same report
    (#2308 review). A key that cannot be named, names something outside the live
    catalog, or names a model that is not dev-capable is claimed by nobody.
    """
    claimed: dict[str, list[dict]] = {canonical_id: [] for canonical_id in dev_models}
    unattributed: list[UnattributedEvidenceRow] = []
    for record in list_dev_evidence_keys(profiles, resolve=resolver):
        canonical_id = record["canonical_id"]
        if canonical_id in dev_models:
            claimed[canonical_id].append(record)
            continue
        runs = max(int(record["entry_runs"]), sum(record["runs_by_band"].values()))
        if runs <= 0:
            continue
        if canonical_id is None:
            reason = REASON_UNRESOLVED
        elif canonical_id not in model_registry:
            reason = REASON_NOT_IN_CATALOG
        else:
            reason = REASON_NOT_DEV_CAPABLE
        unattributed.append(
            UnattributedEvidenceRow(
                key=record["key"],
                reason=reason,
                canonical_id=canonical_id,
                runs=runs,
                runs_by_band=dict(record["runs_by_band"]),
            )
        )
    return claimed, tuple(unattributed)


def _observe(
    dev_models: dict[str, AgentSpec],
    profiles: dict[str, Any],
    claimed: dict[str, list[dict]],
    *,
    floor: int,
    recency: Any | None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the dev signal for every (live dev model, band) pair.

    Scoped to the keys that model *claims* — not to whatever the router's
    identity matching would sweep up — so the population behind a row is exactly
    the population the report says it is.
    """
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    for canonical_id in dev_models:
        records = claimed.get(canonical_id, [])
        keys = [record["key"] for record in records]
        for band in COMPLEXITY_BANDS:
            signal = get_dev_signal_for_keys(profiles, keys, band, floor, recency=recency)
            observations[(canonical_id, band)] = {
                "runs": int(signal["runs"]),
                # Floor-gated: the value a comparison may be drawn against.
                "rate": signal["rate"],
                # Ungated: what the (possibly too-thin) evidence says so far.
                "weighted": signal["weighted"],
                "contributors": tuple(
                    record["key"]
                    for record in records
                    if int(record["runs_by_band"].get(band, 0)) > 0
                ),
            }
    return observations


def _peer_rates(
    dev_models: dict[str, AgentSpec],
    observations: dict[tuple[str, str], dict[str, Any]],
    *,
    floor: int,
) -> dict[tuple[str, str], dict[str, float]]:
    """Group floor-clearing observed rates by (declared tier, band).

    The peer set is concrete: same declared tier, same complexity band, and
    enough evidence of its own to be compared against. A model whose declaration
    has never been tested is not a baseline for anyone else's. Peers are keyed by
    canonical ID, which now says all it needs to: each identity's population is
    the set of keys it claims, so two transports of one model are two peers only
    when each has evidence of its own.
    """
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    for (canonical_id, band), observed in observations.items():
        rate = observed["rate"]
        if rate is None or observed["runs"] < floor:
            continue
        tier = dev_models[canonical_id].routing.tier
        grouped.setdefault((tier, band), {})[canonical_id] = float(rate)
    return grouped


def _row(
    *,
    canonical_id: str,
    spec: AgentSpec,
    band: str,
    observed: dict[str, Any],
    peer_rates: list[float],
    floor: int,
) -> DeclaredStrengthRow:
    runs = observed["runs"]
    rate = observed["rate"]
    peer_low = peer_rates[0] if peer_rates else None
    peer_high = peer_rates[-1] if peer_rates else None

    if runs <= 0:
        status = STATUS_UNOBSERVED
    elif rate is None or runs < floor:
        # Observed, but not enough of it to argue with the declaration.
        status = STATUS_INSUFFICIENT
    elif (
        len(peer_rates) >= MIN_PEERS_FOR_DISAGREEMENT
        and float(rate) < peer_rates[0] - UNDERPERFORMANCE_MARGIN
    ):
        status = STATUS_UNDERPERFORMING
    else:
        status = STATUS_OBSERVED

    return DeclaredStrengthRow(
        canonical_id=canonical_id,
        complexity=band,
        declared_tier=spec.routing.tier,
        declared_capability=spec.routing.capability,
        status=status,
        runs=runs,
        observed_rate=observed["weighted"] if runs > 0 else None,
        peer_low=peer_low,
        peer_high=peer_high,
        peer_count=len(peer_rates),
        contributing_keys=observed["contributors"],
    )
