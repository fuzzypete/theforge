"""Pricing attribution and the routing signals derived from price.

A stored per-MTok price is a *routing input*: it sets a model's cost band and
breaks ties between equal-band candidates. It may only act as one when the
record says what identity the figures were true of — an entry identified by a
vendor shorthand (the Claude CLI's ``opus``/``sonnet``) resolves to whichever
concrete version the vendor currently ships, so its literal can describe an
identity that has since moved (issue #2203).

``pricing_provenance`` carries that attribution: the concrete billed identity
the figures were recorded for, or one of the ``PRICING_PROVENANCE_*`` markers
below. ``None`` means *unattributed* — the figures may be present, but nothing
ties them to what is actually billed, so every routing path here treats them as
unknown rather than authoritative.

Stdlib-only and dependency-free by design: this is consumed by the registry
types (:mod:`theforge.config.models`) and by every selection path that ranks
them.
"""

from __future__ import annotations

# The operator declared these figures for this identity in forge.yaml.
PRICING_PROVENANCE_OPERATOR_DECLARED = "forge.yaml"
# A locally served endpoint: there is no vendor bill, so 0.0 is true by
# construction rather than by observation.
PRICING_PROVENANCE_LOCAL_ENDPOINT = "local-endpoint"

# Cost band used when an entry has no usable price at all (none declared, none
# attributable, no built-in band to inherit). Unchanged from the previous
# ``custom_model_cost_rank(0.0, 0.0)`` spelling — it is a default, not a figure
# derived from an untraceable price.
UNPRICED_COST_RANK = 1

# ── Cost-band attribution ────────────────────────────────────────────────
#
# ``RoutingPolicy.cost_rank`` is the *other* price-shaped routing input: it
# selects the tier a role is filled from (1=cheap, 2=mid, 3=strong), so a band
# copied off an untraceable literal steers selection just as the tie-break does.
# Every band therefore records what it is derived from. A band that matches the
# entry's *attributable* price is attributed to that price automatically; any
# other band has to name a non-price basis, which is what stops an entry from
# silently inheriting a band from figures it is not allowed to trust.

# The band restates the vendor's own tier naming (Claude's ``opus`` is the
# flagship tier, ``sonnet`` the mid tier). That claim is true of the shorthand
# identity itself and survives the shorthand resolving to a new version, which
# is exactly what the literal price does not do.
COST_BAND_BASIS_VENDOR_TIER = "vendor-tier"
# A deliberate routing decision that is not the entry's price band — e.g. a
# reasoning model whose token *volume* puts it a band above its per-MTok rate.
COST_BAND_BASIS_DECLARED_POLICY = "declared-policy"
# The operator set ``routing.cost_rank`` for this entry in forge.yaml.
COST_BAND_BASIS_OPERATOR_DECLARED = "forge.yaml"
# No usable price and no band to inherit: :data:`UNPRICED_COST_RANK` applies.
COST_BAND_BASIS_UNPRICED_DEFAULT = "default-unpriced"


def price_cost_band_basis(pricing_provenance: str) -> str:
    """Name the basis of a band that was derived from an attributed price."""
    return f"price:{pricing_provenance}"


def resolve_cost_band_basis(
    cost_rank: int,
    *,
    input_cost_per_mtok: float | None,
    output_cost_per_mtok: float | None,
    pricing_provenance: str | None,
    declared_basis: str | None,
) -> str:
    """Return the basis to record for ``cost_rank``, or raise if there is none.

    An explicit ``declared_basis`` always wins — the author stated why the band
    is what it is. Otherwise the band may only be attributed to the entry's own
    price, and only when that price is attributable *and* actually produces this
    band. Anything else (an unattributed literal, no price at all, or a band that
    disagrees with the price it would sit next to) raises: the band has no
    traceable source, and inventing one is the defect this module exists to stop.
    """
    if declared_basis is not None:
        return declared_basis
    derived = (
        custom_model_cost_rank(
            float(input_cost_per_mtok or 0.0),
            float(output_cost_per_mtok or 0.0),
            pricing_provenance=pricing_provenance,
        )
        if input_cost_per_mtok is not None or output_cost_per_mtok is not None
        else None
    )
    if derived is None and cost_rank == UNPRICED_COST_RANK:
        # No price recorded at all and the band is the unpriced default — that
        # default *is* the basis, and it cannot have come from a literal because
        # there is none.
        return COST_BAND_BASIS_UNPRICED_DEFAULT
    if derived is None or derived != cost_rank or pricing_provenance is None:
        raise ValueError(
            f"cost_rank={cost_rank} cannot be attributed to this entry's pricing "
            f"(provenance={pricing_provenance!r}, derived band={derived!r}). "
            "State a non-price basis explicitly — see COST_BAND_BASIS_* in "
            "config/pricing.py."
        )
    return price_cost_band_basis(pricing_provenance)


# Sentinel returned by ``price_tiebreak_signal`` when a candidate carries no
# usable pricing. It sorts *after* every priced candidate so unpriced models keep
# their original relative order (list-order fallback) among themselves rather
# than jumping ahead of a model whose real cost is known.
_UNPRICED_SIGNAL: float = float("inf")


class AttributablePricing:
    """Attribution gate shared by every view that carries registry pricing.

    ``AgentSpec``, ``ModelInfo`` and ``AgentDef`` all carry the same
    ``input_cost_per_mtok`` / ``output_cost_per_mtok`` / ``pricing_provenance``
    triple. The ``effective_*`` properties are what routing reads: they collapse
    to ``None`` — the same shape as "no price recorded" — whenever the figures
    cannot be attributed, so an untraceable literal cannot silently participate
    in selection.
    """

    input_cost_per_mtok: float | None
    output_cost_per_mtok: float | None
    pricing_provenance: str | None

    @property
    def pricing_attributable(self) -> bool:
        """True when the recorded prices are attributed to a concrete identity."""
        return bool(self.pricing_provenance)

    @property
    def effective_input_cost_per_mtok(self) -> float | None:
        """Input price as a routing input — ``None`` when unattributable."""
        return self.input_cost_per_mtok if self.pricing_attributable else None

    @property
    def effective_output_cost_per_mtok(self) -> float | None:
        """Output price as a routing input — ``None`` when unattributable."""
        return self.output_cost_per_mtok if self.pricing_attributable else None


def custom_model_cost_rank(
    input_cost_per_mtok: float,
    output_cost_per_mtok: float,
    *,
    pricing_provenance: str | None,
) -> int | None:
    """Map per-MTok pricing to the routing cost bands used by the registry.

    ``pricing_provenance`` is keyword-only and has no default so every caller has
    to state what the figures are attributed to. When it is ``None`` the prices
    cannot be tied to the identity being banded, so this returns ``None`` —
    "no band derivable" — and the caller falls back to a declared or built-in
    band rather than inventing one from an untraceable literal.
    """
    if pricing_provenance is None:
        return None
    price_signal = max(float(input_cost_per_mtok), float(output_cost_per_mtok))
    if price_signal <= 5.0:
        return 1
    if price_signal <= 25.0:
        return 2
    return 3


def price_tiebreak_signal(
    input_cost_per_mtok: float | None,
    output_cost_per_mtok: float | None,
) -> float:
    """Return a per-MTok price signal for breaking equal-``cost_rank`` ties.

    Two models can share a ``cost_rank`` band (and even a ``capability`` score) yet
    differ substantially in real price — e.g. the cheap-tier bucket holds both
    ``claude/sonnet`` and ``openai/gpt-5.4-mini``. Ranking helpers previously broke
    that tie by ``models.enabled`` list order, permanently starving whichever model
    was listed second. This collapses the two per-MTok costs into one comparable
    number (``max`` of input/output, mirroring :func:`custom_model_cost_rank`'s
    price banding) so the cheaper candidate wins the tie deterministically.

    Candidates with no pricing data return :data:`_UNPRICED_SIGNAL` so they rank
    behind any priced peer while preserving list order among unpriced models.

    This is the numeric primitive. Selection paths call
    :func:`price_tiebreak_signal_for` instead, which feeds it the *attributable*
    prices only.
    """
    if input_cost_per_mtok is None and output_cost_per_mtok is None:
        return _UNPRICED_SIGNAL
    return max(
        float(input_cost_per_mtok) if input_cost_per_mtok is not None else 0.0,
        float(output_cost_per_mtok) if output_cost_per_mtok is not None else 0.0,
    )


def price_tiebreak_signal_for(entry: AttributablePricing) -> float:
    """Attribution-gated tie-break signal for a registry entry or pool agent.

    Every selection path (assignment, role derivation, profile auto-assignment,
    the coordinator preflight pool) breaks equal-band ties through this function
    rather than reading the raw price fields, so an entry whose figures carry no
    ``pricing_provenance`` ranks exactly like an unpriced one instead of steering
    the tie with a literal nothing can vouch for.
    """
    return price_tiebreak_signal(
        entry.effective_input_cost_per_mtok,
        entry.effective_output_cost_per_mtok,
    )
