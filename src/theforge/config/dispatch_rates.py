"""Compile the identity-keyed rate registry from a loaded configuration (#2335).

This is the half of the fix that lives at configuration load: the merged model
registry — the very specs routing reads its prices from — is consumed ONCE into
a :class:`~theforge.runners.rate_registry.RateRegistry`, and every accounting
site thereafter looks the dispatched identity up in that registry. One
declaration of a model's cost therefore serves both the decision to use it and
the record of what using it cost.

Three things happen here, in order:

1. **Compile.** Every ``AgentSpec`` in the merged registry yields an entry keyed
   by ``(provider, model, transport.kind)``. Because the key includes the
   transport, the same model offered over CLI and API no longer collides — the
   ambiguity-drop ``_catalog_rates`` performs does not apply and is not
   reproduced (the merged dict is itself keyed by canonical id, so two specs
   cannot collide on one identity by construction).

2. **Materialize legacy prices.** The packaged, transport-agnostic
   :data:`PRICING_TABLE` keeps working by being resolved onto concrete
   transport identities *here*, under three restrictions spelled out in
   :func:`_materialize_legacy_rates`. This is what replaces a transport-agnostic
   fallback tier at query time, and it is why a CLI price an operator declared
   can never become the API path's price.

3. **Report.** Identities the configuration can actually dispatch on but cannot
   account for are warned about at load, naming the paths they are reachable on,
   rather than discovered as a cost-unknown after the spend has happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from theforge.runners.rate_registry import (
    PRICING_TABLE,
    AccountingMode,
    DispatchIdentity,
    ModelRates,
    RateEntry,
    RateRegistry,
    RateSource,
    accounting_mode_for,
    install,
    make_identity,
)

from .model_identity import AgentSpec, canonical_model_id

log = logging.getLogger("theforge.config")


@dataclass(frozen=True)
class ReachableIdentity:
    """An identity this configuration can dispatch on, and how it gets there."""

    identity: DispatchIdentity
    runner: str | None
    paths: tuple[str, ...] = ()


@dataclass
class _Reach:
    runner: str | None = None
    paths: list[str] = field(default_factory=list)


# ── Enumeration of dispatchable identities ────────────────────────────


def _add(
    found: dict[DispatchIdentity, _Reach],
    identity: DispatchIdentity | None,
    runner: str | None,
    path: str,
) -> None:
    if identity is None:
        return
    reach = found.setdefault(identity, _Reach())
    if reach.runner is None:
        reach.runner = runner
    if path not in reach.paths:
        reach.paths.append(path)


def _profile_identities(
    found: dict[DispatchIdentity, _Reach],
    profile: object,
    path: str,
) -> None:
    """Record the primary, model-fallback and transport-fallback identities.

    A profile can reach a runner as three different billed identities and each
    one is priced separately: its primary model, any ``fallback_models`` entry
    tried on quota/not-found failure (same provider and transport, different
    model), and its ``api_fallback`` target (same provider, API transport,
    possibly a different model identifier).
    """
    if profile is None:
        return
    provider = getattr(profile, "provider_family", None)
    kind = getattr(profile, "mode", None)
    transport = getattr(profile, "transport", None)
    runner = getattr(transport, "runner", None)
    _add(found, make_identity(provider, getattr(profile, "model", None), kind), runner, path)
    for fallback_model in getattr(profile, "fallback_models", ()) or ():
        _add(
            found,
            make_identity(provider, fallback_model, kind),
            runner,
            f"{path} (model fallback)",
        )
    api_fallback = getattr(profile, "api_fallback", None)
    if api_fallback is not None:
        fb_transport = None
        try:
            fb_transport = api_fallback.transport()
        except Exception:  # pragma: no cover - defensive: malformed fallback
            fb_transport = None
        _add(
            found,
            make_identity(
                getattr(api_fallback, "provider", None),
                getattr(api_fallback, "model", None),
                "api",
            ),
            getattr(fb_transport, "runner", None),
            f"{path} (transport fallback)",
        )


def reachable_identities(config: object) -> tuple[ReachableIdentity, ...]:
    """Every identity the loaded configuration can dispatch on.

    Computed once and used for BOTH the legacy-materialization target
    restriction and the load-time report, so what is widened and what is
    reported are derived from one list rather than two enumerations that can
    drift apart.
    """
    found: dict[DispatchIdentity, _Reach] = {}

    _profile_identities(found, getattr(config, "dev_profile", None), "dev")
    _profile_identities(found, getattr(config, "preflight_profile", None), "preflight")
    for entry in getattr(config, "review_pool", None) or ():
        _profile_identities(found, entry, f"review pool ({entry.name})")
    _profile_identities(found, getattr(config, "synthesis_profile", None), "synthesis")

    plan = getattr(config, "plan", None)
    if plan is not None and getattr(plan, "enabled", False):
        _profile_identities(found, plan, "plan")
    plan_agent_review = getattr(config, "plan_agent_review", None)
    if plan_agent_review is not None and getattr(plan_agent_review, "enabled", False):
        for entry in getattr(plan_agent_review, "profiles", None) or ():
            _profile_identities(found, entry, f"plan review ({entry.name})")
    for entry in getattr(config, "agents", None) or ():
        _profile_identities(found, entry, f"agent ({entry.name})")

    # Adaptive routing selects from the whole registry, so with it enabled every
    # registered identity is genuinely reachable — narrowing to the seated
    # profiles here is exactly the omission that let an adaptive-pool candidate
    # dispatch unpriced.
    assignment = getattr(config, "assignment", None)
    if assignment is not None and getattr(assignment, "adaptive_enabled", False):
        for canonical_id, spec in (getattr(config, "model_registry", None) or {}).items():
            _add(
                found,
                make_identity(spec.provider, spec.model, spec.transport.kind),
                spec.transport.runner,
                f"adaptive pool ({canonical_id})",
            )

    return tuple(
        ReachableIdentity(identity=identity, runner=reach.runner, paths=tuple(reach.paths))
        for identity, reach in sorted(found.items(), key=lambda item: item[0].label)
    )


# ── Compilation ───────────────────────────────────────────────────────


def _spec_rates(spec: AgentSpec) -> ModelRates | None:
    """The rate card an ``AgentSpec`` declares, honouring attribution rules."""
    from .pricing import PRICING_PROVENANCE_LOCAL_ENDPOINT  # noqa: PLC0415

    if spec.input_cost_per_mtok is None or spec.output_cost_per_mtok is None:
        return None
    if not spec.pricing_attributable:
        return None
    if spec.pricing_provenance == PRICING_PROVENANCE_LOCAL_ENDPOINT:
        # A local endpoint's 0.00 is true of the *endpoint*, not of the model
        # name — the runners already zero a genuinely local invocation by
        # base_url, so importing the figure here would only mis-price the rest.
        return None
    return ModelRates(
        input_per_mtok=spec.input_cost_per_mtok,
        output_per_mtok=spec.output_cost_per_mtok,
        cached_input_per_mtok=spec.cached_input_cost_per_mtok,
    )


def _materialize_legacy_rates(
    entries: dict[DispatchIdentity, RateEntry],
    reachable: tuple[ReachableIdentity, ...],
    project_priced: set[tuple[str, str]],
) -> set[DispatchIdentity]:
    """Widen packaged transport-agnostic prices onto concrete identities.

    Three restrictions, each closing one way a price could reach an identity it
    was never recorded for:

    (a) **Source.** Materialization draws ONLY from the packaged
        :data:`PRICING_TABLE`. A project-declared per-transport entry never
        widens to another transport — that is the reported failure (a model
        priced on the CLI lending its rate to the API path) and it is refused
        structurally, not by convention.
    (b) **Target.** A legacy price is materialized only onto an identity this
        configuration can actually dispatch on. Entries that resolve onto no
        reachable identity are dropped; nothing unscoped enters the registry.
    (c) **Conflict.** Materialization is refused for a ``(provider, model)``
        that carries any project-declared per-transport price. A genuine
        per-transport disagreement must surface at load, not be resolved by
        borrowing either figure.

    Returns the identities whose widening rule (c) suppressed, so the load-time
    report can name the conflict as the operator-actionable case it is.
    """
    conflicted: set[DispatchIdentity] = set()
    for reach in reachable:
        identity = reach.identity
        existing = entries.get(identity)
        if existing is not None and (
            existing.priced or existing.mode is AccountingMode.INDEPENDENTLY_MEASURED
        ):
            continue
        legacy = PRICING_TABLE.get((identity.provider, identity.model))
        if legacy is None:
            continue
        if (identity.provider, identity.model) in project_priced:
            conflicted.add(identity)
            continue
        mode = accounting_mode_for(identity.transport, reach.runner)
        origin = f"PRICING_TABLE[{identity.provider}/{identity.model}]"
        entries[identity] = RateEntry(
            identity=identity,
            rates=ModelRates(input_per_mtok=legacy[0], output_per_mtok=legacy[1]),
            mode=mode,
            source=RateSource.LEGACY_COMPAT,
            origin=origin,
        )
        log.info(
            "Pricing %s from the packaged rate table (%s): no per-transport entry declares it.",
            identity.label,
            origin,
        )
    return conflicted


def compile_rate_registry(
    model_registry: dict[str, AgentSpec],
    reachable: tuple[ReachableIdentity, ...],
) -> tuple[RateRegistry, set[DispatchIdentity]]:
    """Build the registry for one configuration.

    Returns ``(registry, conflicted)`` where ``conflicted`` names identities
    whose legacy widening was suppressed by a per-transport pricing conflict.
    """
    entries: dict[DispatchIdentity, RateEntry] = {}
    project_priced: set[tuple[str, str]] = set()

    from .pricing import PRICING_PROVENANCE_LOCAL_ENDPOINT  # noqa: PLC0415

    for canonical_id, spec in (model_registry or {}).items():
        identity = make_identity(spec.provider, spec.model, spec.transport.kind)
        if identity is None:  # pragma: no cover - AgentSpec cannot be partial
            continue
        rates = _spec_rates(spec)
        is_project = spec.registry_source == "forge.yaml"
        if spec.pricing_provenance == PRICING_PROVENANCE_LOCAL_ENDPOINT:
            # A local endpoint's spend IS measured — the runners record 0.00 for
            # it from base_url, with no rate card involved. Recording it as
            # rate-estimated would make the load-time report announce that a
            # genuinely free, already-measured identity cannot be accounted for.
            entries[identity] = RateEntry(
                identity=identity,
                rates=None,
                mode=AccountingMode.INDEPENDENTLY_MEASURED,
                source=RateSource.NONE,
                origin=canonical_id,
            )
            continue
        if rates is not None and is_project:
            project_priced.add((identity.provider, identity.model))
        entries[identity] = RateEntry(
            identity=identity,
            rates=rates,
            mode=accounting_mode_for(spec.transport.kind, spec.transport.runner),
            source=(
                (RateSource.PROJECT if is_project else RateSource.CATALOG)
                if rates is not None
                else RateSource.NONE
            ),
            origin=canonical_id
            or canonical_model_id(spec.provider, spec.model, spec.transport.kind),
        )

    conflicted = _materialize_legacy_rates(entries, reachable, project_priced)

    # Every reachable identity gets an entry even when it is unpriced, so its
    # accounting mode survives the lookup and the report can classify it.
    for reach in reachable:
        if reach.identity in entries:
            continue
        entries[reach.identity] = RateEntry(
            identity=reach.identity,
            rates=None,
            mode=accounting_mode_for(reach.identity.transport, reach.runner),
            source=RateSource.NONE,
        )

    registry = RateRegistry(
        entries=entries,
        reachable={reach.identity: reach.paths for reach in reachable},
    )
    return registry, conflicted


def report_unaccountable_identities(
    registry: RateRegistry,
    reachable: tuple[ReachableIdentity, ...],
    conflicted: set[DispatchIdentity],
) -> list[str]:
    """Warn once per dispatchable identity whose spend could not be accounted for.

    Warn-and-name rather than raise: an unpriced model is allowed to run and
    record its cost as unknown, so refusing the load would be a behaviour change
    beyond the bug. ``check-config`` captures ``theforge.config`` warnings into
    its own WARNINGS section, so this reaches the operator on every entry point.
    """
    messages: list[str] = []
    for reach in reachable:
        entry = registry.lookup(reach.identity)
        reason = entry.unaccountable_reason()
        if reason is None:
            continue
        if reach.identity in conflicted:
            reason = (
                f"{reach.identity.provider}/{reach.identity.model} is priced on another "
                f"transport but dispatched on {reach.identity.transport} with no "
                f"{reach.identity.transport} price — a per-transport pricing conflict "
                "suppressed the packaged fallback rather than borrowing the other "
                "transport's rate"
            )
        paths = ", ".join(reach.paths) or "unknown path"
        message = (
            f"Cost cannot be accounted for {reach.identity.label}: {reason}. "
            f"Reachable on: {paths}. Runs on this identity will record cost as unknown. "
            "Declare input_cost_per_mtok/output_cost_per_mtok (with pricing_provenance) "
            "on this model's entry to fix it."
        )
        messages.append(message)
        log.warning("%s", message)
    return messages


def install_rate_registry(config: object) -> RateRegistry:
    """Compile, install and report the rate registry for a loaded config.

    Called once, after the ``ForgeConfig`` is fully constructed and validated,
    so a load that raises never leaves its partial rates installed.
    """
    reachable = reachable_identities(config)
    registry, conflicted = compile_rate_registry(
        getattr(config, "model_registry", None) or {}, reachable
    )
    install(registry)
    report_unaccountable_identities(registry, reachable, conflicted)
    return registry
