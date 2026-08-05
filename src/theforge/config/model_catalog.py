"""One canonical schema for model definitions, and the parser that reads it.

A model definition is not behaviour. It is a canonical identity
(``provider`` + ``model`` + transport kind) paired with a routing policy,
endpoint metadata and cost seeds. The only part that genuinely requires code is
the *adapter* that reaches a provider, because a provider needs a runner module.

So there is one schema and one parser for both sources of definitions:

- the shipped default set, which lives in ``config/data/models.yaml`` and is
  loaded by :func:`load_packaged_catalog`;
- project-declared models in ``forge.yaml`` (``models.custom`` and inline
  ``models.enabled`` mappings), which reach the same parser through the
  compatibility adapters in :mod:`theforge.config.load`.

Canonical shape::

    provider: openai
    model: gpt-5.5
    transport: {kind: cli}          # optional `runner:` for ambiguous tuples
    base_url: http://…              # endpoint metadata, optional
    routing:
      tier: strong
      capability: 9
      cost_rank: 2
      dev_capable: true
      phase_eligibility: [preflight, dev, plan, review]
      cost_rank_basis: declared-policy
    cost:
      input_per_mtok: 1.25
      output_per_mtok: 10.00
      pricing_provenance: gpt-5.5

Two resolution modes sit on top of that one syntax:

- :func:`resolve_packaged` — no built-in entry to fall back on, so the routing
  policy must be complete, and a cost band that cannot be attributed to an
  attributable price must name its non-price basis or construction raises. This
  is what the shipped catalog is held to.
- :func:`resolve_project` — overlay semantics. Anything the declaration omits
  falls back to the built-in entry with the same canonical identity (when there
  is one), then to a value derived from ``tier``/transport. Each resolved field
  records which of the two sources supplied it, which is what makes a partial
  project overlay auditable rather than a silent replacement.

Layering: the identity types this module builds on live in
:mod:`theforge.config.model_identity`, a leaf both this module and
:mod:`theforge.config.models` import. That is what lets ``models.py`` build
``AGENT_REGISTRY`` by calling :func:`load_packaged_catalog` at import time
without the two modules importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable

import yaml

from .model_identity import (
    _DEFAULT_PHASE_ELIGIBILITY,
    CAPABILITY_RANGE,
    COST_RANK_RANGE,
    KNOWN_PHASES,
    MODEL_TIERS,
    TRANSPORT_KINDS,
    AgentSpec,
    RoutingPolicy,
    TransportSpec,
    canonical_model_id,
    custom_model_capability,
    custom_model_dev_capable,
    transport_for,
)
from .pricing import (
    COST_BAND_BASIS_OPERATOR_DECLARED,
    COST_BAND_BASIS_UNPRICED_DEFAULT,
    PRICING_PROVENANCE_OPERATOR_DECLARED,
    UNPRICED_COST_RANK,
    custom_model_cost_rank,
    price_cost_band_basis,
    resolve_cost_band_basis,
)

# Where a resolved field came from.
SOURCE_BUILTIN = "builtin"
SOURCE_PROJECT = "forge.yaml"

DEFINITION_KEYS: frozenset[str] = frozenset(
    {"provider", "model", "transport", "routing", "base_url", "cost"}
)
ROUTING_KEYS: frozenset[str] = frozenset(
    {
        "tier",
        "capability",
        "cost_rank",
        "dev_capable",
        "phase_eligibility",
        "cost_rank_basis",
    }
)
COST_KEYS: frozenset[str] = frozenset({"input_per_mtok", "output_per_mtok", "pricing_provenance"})

# The resolved fields provenance is reported for. Identity (provider, model,
# transport) is deliberately absent: it is what the two sources *agree* on when
# they describe the same entry, so there is nothing to attribute.
PROVENANCE_FIELDS: tuple[str, ...] = (
    "tier",
    "capability",
    "cost_rank",
    "cost_rank_basis",
    "dev_capable",
    "phase_eligibility",
    "base_url",
    "input_cost_per_mtok",
    "output_cost_per_mtok",
    "pricing_provenance",
)

_CATALOG_PACKAGE = "theforge.config"
_CATALOG_RESOURCE = "data/models.yaml"


@dataclass(frozen=True)
class ParsedDefinition:
    """A syntax-validated definition, before routing policy is resolved.

    ``declared`` holds the *resolved* field names the raw mapping actually
    stated (``tier``, ``input_cost_per_mtok``, …), which is what lets overlay
    resolution tell "the project set this" from "this was inherited".
    """

    canonical_id: str
    provider: str
    model: str
    transport: TransportSpec
    routing: dict[str, Any]
    cost: dict[str, Any]
    base_url: str | None
    declared: frozenset[str]


@dataclass(frozen=True)
class ResolvedModel:
    """A definition resolved into a registry entry plus per-field provenance."""

    canonical_id: str
    spec: AgentSpec
    field_sources: dict[str, str]


# ── Syntax ────────────────────────────────────────────────────────────────


def _require_str(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"forge.yaml '{where}' must be a non-empty string")
    return value


def _require_int(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"forge.yaml '{where}' must be an integer, got {value!r}")
    return value


def _require_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"forge.yaml '{where}' must be a boolean, got {value!r}")
    return value


def _require_price(value: Any, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < 0:
        raise ValueError(f"forge.yaml '{where}' must be a non-negative number, got {value!r}")
    return float(value)


def _require_tier(value: Any, *, where: str) -> str:
    tier = _require_str(value, where=where)
    if tier not in MODEL_TIERS:
        raise ValueError(
            f"forge.yaml '{where}' must be one of {sorted(MODEL_TIERS)}, got {tier!r}"
        )
    return tier


def _require_in_range(value: Any, bounds: tuple[int, int], *, where: str) -> int:
    """Bound a routing integer to the scale its consumer actually reads.

    Type alone is not enough for these: ``capability: 999`` and ``cost_rank: -8``
    are well-typed and load, then sort a model to the top or bottom of every
    candidate list forever. Out-of-scale is never a meaningful declaration, so it
    is a load error rather than a value to clamp — clamping would resolve a
    definition to something the operator did not write.
    """
    number = _require_int(value, where=where)
    low, high = bounds
    if not low <= number <= high:
        raise ValueError(f"forge.yaml '{where}' must be between {low} and {high}, got {number}")
    return number


def parse_transport_block(raw: Any, where: str) -> tuple[str, str | None]:
    """Read the bounded ``transport: {kind: cli|api, runner: …}`` object.

    ``runner`` only needs naming for ``(provider, kind)`` tuples that admit more
    than one executor; :func:`theforge.config.model_identity.transport_for`
    derives it everywhere else and rejects a runner that contradicts the tuple.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"forge.yaml '{where}.transport' must be a mapping with a 'kind' key")
    unknown = set(raw) - {"kind", "runner"}
    if unknown:
        raise ValueError(
            f"forge.yaml '{where}.transport' only supports 'kind' and 'runner'; "
            f"got {sorted(unknown)}"
        )
    kind = raw.get("kind")
    if kind not in TRANSPORT_KINDS:
        raise ValueError(
            f"forge.yaml '{where}.transport.kind' must be one of {sorted(TRANSPORT_KINDS)}, "
            f"got {kind!r}"
        )
    runner = raw.get("runner")
    if runner is not None and (not isinstance(runner, str) or not runner):
        raise ValueError(f"forge.yaml '{where}.transport.runner' must be a non-empty string")
    return str(kind), runner


def parse_definition(entry: Any, *, where: str) -> ParsedDefinition:
    """Validate one canonical model definition's syntax.

    Adapter validation happens here: the ``(provider, transport.kind)`` tuple is
    resolved through :func:`transport_for`, so a provider with no runner module
    fails at load time with a message naming the adapters that do exist.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"forge.yaml '{where}' must be a mapping")
    unknown = set(entry) - DEFINITION_KEYS
    if unknown:
        raise ValueError(
            f"forge.yaml '{where}' only supports {sorted(DEFINITION_KEYS)}; "
            f"unknown key(s): {sorted(unknown)}"
        )
    for required in ("provider", "model", "transport"):
        if required not in entry:
            raise ValueError(f"forge.yaml '{where}' is missing required field {required!r}")

    provider = _require_str(entry["provider"], where=f"{where}.provider")
    model = _require_str(entry["model"], where=f"{where}.model")
    kind, runner = parse_transport_block(entry["transport"], where)
    transport = transport_for(provider, kind, runner=runner)

    routing_raw = entry.get("routing") or {}
    if not isinstance(routing_raw, dict):
        raise ValueError(f"forge.yaml '{where}.routing' must be a mapping")
    unknown_routing = set(routing_raw) - ROUTING_KEYS
    if unknown_routing:
        raise ValueError(
            f"forge.yaml '{where}.routing' only supports {sorted(ROUTING_KEYS)}; "
            f"unknown key(s): {sorted(unknown_routing)}"
        )
    cost_raw = entry.get("cost") or {}
    if not isinstance(cost_raw, dict):
        raise ValueError(f"forge.yaml '{where}.cost' must be a mapping")
    unknown_cost = set(cost_raw) - COST_KEYS
    if unknown_cost:
        raise ValueError(
            f"forge.yaml '{where}.cost' only supports {sorted(COST_KEYS)}; "
            f"unknown key(s): {sorted(unknown_cost)}"
        )

    base_url = entry.get("base_url")
    if base_url is not None:
        base_url = _require_str(base_url, where=f"{where}.base_url")

    routing: dict[str, Any] = {}
    if "tier" in routing_raw:
        routing["tier"] = _require_tier(routing_raw["tier"], where=f"{where}.routing.tier")
    if "capability" in routing_raw:
        routing["capability"] = _require_in_range(
            routing_raw["capability"], CAPABILITY_RANGE, where=f"{where}.routing.capability"
        )
    if "cost_rank" in routing_raw:
        routing["cost_rank"] = _require_in_range(
            routing_raw["cost_rank"], COST_RANK_RANGE, where=f"{where}.routing.cost_rank"
        )
    if "dev_capable" in routing_raw:
        routing["dev_capable"] = _require_bool(
            routing_raw["dev_capable"], where=f"{where}.routing.dev_capable"
        )
    if "phase_eligibility" in routing_raw:
        phases = routing_raw["phase_eligibility"]
        if not isinstance(phases, (list, tuple, set, frozenset)) or not phases:
            raise ValueError(
                f"forge.yaml '{where}.routing.phase_eligibility' must be a non-empty list"
            )
        parsed_phases = frozenset(
            _require_str(p, where=f"{where}.routing.phase_eligibility") for p in phases
        )
        unknown_phases = parsed_phases - KNOWN_PHASES
        if unknown_phases:
            raise ValueError(
                f"forge.yaml '{where}.routing.phase_eligibility' only supports "
                f"{sorted(KNOWN_PHASES)}; got {sorted(unknown_phases)}"
            )
        routing["phase_eligibility"] = parsed_phases
    if "cost_rank_basis" in routing_raw:
        routing["cost_rank_basis"] = _require_str(
            routing_raw["cost_rank_basis"], where=f"{where}.routing.cost_rank_basis"
        )

    cost: dict[str, Any] = {}
    if "input_per_mtok" in cost_raw:
        cost["input_cost_per_mtok"] = _require_price(
            cost_raw["input_per_mtok"], where=f"{where}.cost.input_per_mtok"
        )
    if "output_per_mtok" in cost_raw:
        cost["output_cost_per_mtok"] = _require_price(
            cost_raw["output_per_mtok"], where=f"{where}.cost.output_per_mtok"
        )
    if "pricing_provenance" in cost_raw:
        provenance = cost_raw["pricing_provenance"]
        # An explicit null is meaningful: it says "these figures are not
        # attributable to this identity", which is exactly what a vendor
        # shorthand must be able to state.
        if provenance is not None:
            provenance = _require_str(provenance, where=f"{where}.cost.pricing_provenance")
        cost["pricing_provenance"] = provenance

    declared = frozenset(routing) | frozenset(cost) | ({"base_url"} if base_url else set())
    return ParsedDefinition(
        canonical_id=canonical_model_id(provider, model, transport.kind),
        provider=provider,
        model=model,
        transport=transport,
        routing=routing,
        cost=cost,
        base_url=base_url,
        declared=declared,
    )


# ── Resolution ────────────────────────────────────────────────────────────


def resolve_packaged(defn: ParsedDefinition, *, where: str) -> ResolvedModel:
    """Resolve a definition that has no built-in entry behind it.

    The routing policy must be complete — there is nothing to inherit from —
    and the cost band is held to the attribution rule in
    :func:`config.pricing.resolve_cost_band_basis`: a band that is not this
    entry's own attributable price band has to name its non-price basis.
    """
    for required in ("tier", "capability", "cost_rank"):
        if required not in defn.routing:
            raise ValueError(
                f"forge.yaml '{where}.routing' is missing required field {required!r}"
            )
    input_cost = defn.cost.get("input_cost_per_mtok")
    output_cost = defn.cost.get("output_cost_per_mtok")
    provenance = defn.cost.get("pricing_provenance")
    basis = resolve_cost_band_basis(
        defn.routing["cost_rank"],
        input_cost_per_mtok=input_cost,
        output_cost_per_mtok=output_cost,
        pricing_provenance=provenance,
        declared_basis=defn.routing.get("cost_rank_basis"),
    )
    spec = AgentSpec(
        provider=defn.provider,
        model=defn.model,
        transport=defn.transport,
        routing=RoutingPolicy(
            tier=defn.routing["tier"],
            capability=defn.routing["capability"],
            cost_rank=defn.routing["cost_rank"],
            dev_capable=defn.routing.get("dev_capable", True),
            phase_eligibility=defn.routing.get("phase_eligibility", _DEFAULT_PHASE_ELIGIBILITY),
            cost_rank_basis=basis,
        ),
        base_url=defn.base_url,
        registry_source=SOURCE_BUILTIN,
        input_cost_per_mtok=input_cost,
        output_cost_per_mtok=output_cost,
        pricing_provenance=provenance,
    )
    return ResolvedModel(
        canonical_id=defn.canonical_id,
        spec=spec,
        field_sources={field: SOURCE_BUILTIN for field in PROVENANCE_FIELDS},
    )


def resolve_project(
    defn: ParsedDefinition,
    *,
    where: str,
    builtin: AgentSpec | None,
) -> ResolvedModel:
    """Resolve a project-declared definition, overlaying a built-in entry.

    Precedence per field: what the declaration states, then the built-in entry
    with the same canonical identity, then a value derived from ``tier`` and the
    transport. Every resolved field records which of the two sources supplied it.
    """
    sources: dict[str, str] = {}

    def _pick(
        field: str,
        declared: Any,
        builtin_value: Any,
        derived: Callable[[], Any] = lambda: None,
    ) -> Any:
        """Resolve one field and record where the value came from.

        ``derived`` is a thunk: the fallback for a field the built-in registry
        would have supplied must not be *computed* when a built-in entry exists
        (deriving capability from tier rejects tiers an overlay may legitimately
        use when it is only refining an existing entry).
        """
        if field in defn.declared:
            sources[field] = SOURCE_PROJECT
            return declared
        if builtin is not None:
            sources[field] = SOURCE_BUILTIN
            return builtin_value
        sources[field] = SOURCE_PROJECT
        return derived()

    tier = _pick("tier", defn.routing.get("tier"), builtin.tier if builtin is not None else None)
    if not tier:
        raise ValueError(
            f"forge.yaml '{where}.routing.tier' is required for a model that is not "
            "in the built-in registry"
        )

    input_cost = _pick(
        "input_cost_per_mtok",
        defn.cost.get("input_cost_per_mtok"),
        builtin.input_cost_per_mtok if builtin is not None else None,
    )
    output_cost = _pick(
        "output_cost_per_mtok",
        defn.cost.get("output_cost_per_mtok"),
        builtin.output_cost_per_mtok if builtin is not None else None,
    )
    # Declaring a `cost:` figure without saying what it is attributed to
    # attributes it to this identity — the operator asserted it here. An
    # explicit `pricing_provenance` (including an explicit null) wins over that
    # inference. Figures merely *inherited* from a built-in entry keep that
    # entry's attribution, which for a vendor-shorthand identity is None: the
    # literal is carried for reference but must not band.
    declares_price = bool({"input_cost_per_mtok", "output_cost_per_mtok"} & defn.declared)
    if "pricing_provenance" in defn.declared:
        sources["pricing_provenance"] = SOURCE_PROJECT
        provenance = defn.cost["pricing_provenance"]
    elif declares_price:
        sources["pricing_provenance"] = SOURCE_PROJECT
        provenance = PRICING_PROVENANCE_OPERATOR_DECLARED
    else:
        sources["pricing_provenance"] = SOURCE_BUILTIN if builtin is not None else SOURCE_PROJECT
        provenance = builtin.pricing_provenance if builtin is not None else None

    banded_cost_rank = (
        custom_model_cost_rank(
            float(input_cost or 0.0),
            float(output_cost or 0.0),
            pricing_provenance=provenance,
        )
        if input_cost is not None or output_cost is not None
        else None
    )
    # The band travels with the reason it holds, in the same order of precedence:
    # the operator's own declaration, then this entry's attributable price, then
    # whatever the built-in entry already justified, then the unpriced default.
    # Nothing here can reach the band of an unattributed literal — the second
    # branch is gated on `banded_cost_rank`, which is None in that case.
    declared_basis = defn.routing.get("cost_rank_basis")
    if "cost_rank" in defn.declared:
        cost_rank = defn.routing["cost_rank"]
        # A stated band that *is* this entry's attributable price band is
        # attributed to that price, exactly as the packaged path attributes it —
        # the same definition must not read differently depending on which file
        # it was written in. Any other stated band is the operator's own.
        cost_rank_basis = declared_basis or (
            price_cost_band_basis(str(provenance))
            if banded_cost_rank == cost_rank
            else COST_BAND_BASIS_OPERATOR_DECLARED
        )
        sources["cost_rank"] = SOURCE_PROJECT
    elif banded_cost_rank is not None:
        cost_rank = banded_cost_rank
        cost_rank_basis = declared_basis or price_cost_band_basis(str(provenance))
        # The band is derived from *both* figures, so it is the project's as soon
        # as either one is — reading only the input side reported an output-only
        # overlay's band as builtin, which is the opposite of what happened. Fall
        # back to the input side's source when neither is declared, which is the
        # inherited-figures case.
        sources["cost_rank"] = (
            SOURCE_PROJECT
            if declares_price
            else sources.get("input_cost_per_mtok", SOURCE_BUILTIN)
        )
    elif builtin is not None:
        cost_rank = builtin.cost_rank
        cost_rank_basis = declared_basis or builtin.routing.cost_rank_basis
        sources["cost_rank"] = SOURCE_BUILTIN
    else:
        cost_rank = UNPRICED_COST_RANK
        cost_rank_basis = declared_basis or COST_BAND_BASIS_UNPRICED_DEFAULT
        sources["cost_rank"] = SOURCE_PROJECT
    sources["cost_rank_basis"] = (
        SOURCE_PROJECT if declared_basis is not None else sources["cost_rank"]
    )

    capability = _pick(
        "capability",
        defn.routing.get("capability"),
        builtin.capability if builtin is not None else None,
        lambda: custom_model_capability(tier),
    )
    dev_capable = _pick(
        "dev_capable",
        defn.routing.get("dev_capable"),
        builtin.dev_capable if builtin is not None else None,
        lambda: custom_model_dev_capable(defn.transport),
    )
    phase_eligibility = _pick(
        "phase_eligibility",
        defn.routing.get("phase_eligibility"),
        builtin.phase_eligibility if builtin is not None else None,
        lambda: _DEFAULT_PHASE_ELIGIBILITY,
    )
    base_url = _pick(
        "base_url",
        defn.base_url,
        builtin.base_url if builtin is not None else None,
    )

    spec = AgentSpec(
        provider=defn.provider,
        model=defn.model,
        transport=defn.transport,
        routing=RoutingPolicy(
            tier=tier,
            capability=capability,
            cost_rank=cost_rank,
            dev_capable=dev_capable,
            phase_eligibility=phase_eligibility,
            cost_rank_basis=cost_rank_basis,
        ),
        base_url=base_url,
        registry_source=SOURCE_PROJECT,
        input_cost_per_mtok=input_cost,
        output_cost_per_mtok=output_cost,
        pricing_provenance=provenance,
    )
    return ResolvedModel(
        canonical_id=defn.canonical_id,
        spec=spec,
        field_sources={field: sources.get(field, SOURCE_PROJECT) for field in PROVENANCE_FIELDS},
    )


# ── Packaged catalog ──────────────────────────────────────────────────────


def _read_catalog_document() -> Any:
    resource = resources.files(_CATALOG_PACKAGE).joinpath(_CATALOG_RESOURCE)
    return yaml.safe_load(resource.read_text(encoding="utf-8"))


def load_packaged_catalog() -> dict[str, AgentSpec]:
    """Load the shipped default model set from ``config/data/models.yaml``.

    Read through the same parser project declarations use, so both sources
    produce identical structures and one schema governs them.
    """
    document = _read_catalog_document()
    if not isinstance(document, dict) or not isinstance(document.get("models"), list):
        raise ValueError(
            "packaged model catalog (config/data/models.yaml) must be a mapping with a "
            "'models' list"
        )
    registry: dict[str, AgentSpec] = {}
    for index, entry in enumerate(document["models"]):
        where = f"models[{index}]"
        defn = parse_definition(entry, where=where)
        resolved = resolve_packaged(defn, where=where)
        if resolved.canonical_id in registry:
            raise ValueError(
                f"packaged model catalog declares {resolved.canonical_id!r} twice "
                f"({where}); canonical identities must be unique"
            )
        registry[resolved.canonical_id] = resolved.spec
    return registry
