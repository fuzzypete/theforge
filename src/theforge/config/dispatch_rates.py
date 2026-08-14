"""Compile the identity-keyed rate registry from a loaded configuration (#2335).

This is the half of the fix that lives at configuration load: the merged model
registry — the very specs routing reads its prices from — is consumed ONCE into
a :class:`~theforge.runners.rate_registry.RateRegistry`, and every accounting
site thereafter looks the dispatched identity up in that registry. One
declaration of a model's cost therefore serves both the decision to use it and
the record of what using it cost.

Two things happen here, in order:

1. **Compile.** Every ``AgentSpec`` in the merged registry yields an entry keyed
   by ``(provider, model, transport.kind)``. Because the key includes the
   transport, the same model offered over CLI and API no longer collides — the
   ambiguity-drop ``_catalog_rates`` performs does not apply and is not
   reproduced (the merged dict is itself keyed by canonical id, so two specs
   cannot collide on one identity by construction).

2. **Report.** Identities the configuration can actually dispatch on but cannot
   account for are warned about at load, naming the paths they are reachable on,
   rather than discovered as a cost-unknown after the spend has happened.

There used to be a third step between them: a packaged, transport-agnostic
``PRICING_TABLE`` was *materialized* onto concrete transport identities here, so
a rate compiled into the runner package could price an identity the catalog
never described. That table was a second declaration surface no configuration
could override, and it is gone (#2388) — the registry is compiled from the
merged model registry and nothing else, so every figure in it came from the
shipped catalog or from ``forge.yaml``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from theforge.runners.rate_registry import (
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

from .model_identity import AgentSpec, canonical_model_id, model_fallback_transport

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
    tried on quota/not-found failure, and its ``api_fallback`` target (same
    provider, API transport, possibly a different model identifier).

    A ``fallback_models`` entry dispatches on the transport
    :func:`model_fallback_transport` names, NOT on the primary's transport. For
    an API profile those coincide; for a CLI profile they do not — the runner
    sends the fallback through the provider's API adapter, because the failure
    that triggered it (quota exhausted, model not found) is one the CLI would
    just reproduce. Enumerating it under the CLI transport meant the identity
    that could actually run unpriced was never the one named at load, and the
    API identity it really dispatches was never checked at all.
    """
    if profile is None:
        return
    provider = getattr(profile, "provider_family", None)
    kind = getattr(profile, "mode", None)
    transport = getattr(profile, "transport", None)
    runner = getattr(transport, "runner", None)
    _add(found, make_identity(provider, getattr(profile, "model", None), kind), runner, path)
    fallback_models = getattr(profile, "fallback_models", ()) or ()
    # None when the provider has no API adapter: no model fallback can be
    # attempted at all, so those entries name no reachable identity and are not
    # enumerated.
    model_fb_transport = model_fallback_transport(provider) if fallback_models else None
    if model_fb_transport is not None:
        for fallback_model in fallback_models:
            _add(
                found,
                make_identity(provider, fallback_model, model_fb_transport.kind),
                model_fb_transport.runner,
                f"{path} (model fallback)",
            )
    api_fallback = getattr(profile, "api_fallback", None)
    if api_fallback is not None:
        # Read the transport off the same TransportFallbackConfig.transport()
        # the runner dispatches through (runners/cli.py:_build_api_fallback_profile)
        # rather than assuming "api" here — one definition of where it goes, for
        # the same reason the model-fallback transport is not assumed above.
        try:
            fb_transport = api_fallback.transport()
        except Exception:  # pragma: no cover - defensive: malformed fallback
            fb_transport = None
        if fb_transport is not None:
            _add(
                found,
                make_identity(
                    getattr(api_fallback, "provider", None),
                    getattr(api_fallback, "model", None),
                    fb_transport.kind,
                ),
                fb_transport.runner,
                f"{path} (transport fallback)",
            )


def reachable_identities(config: object) -> tuple[ReachableIdentity, ...]:
    """Every identity the loaded configuration can dispatch on.

    Computed once and used both to give every dispatchable identity a registry
    entry (priced or not, so its accounting mode survives the lookup) and to
    drive the load-time report, so what is compiled and what is reported are
    derived from one list rather than two enumerations that can drift apart.
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

    if not spec.uses_rate_card:
        # The entry states that its transport reports what it was billed, so no
        # rate card is consulted for it. Honoured here rather than trusted to be
        # accompanied by absent figures: an overlay can declare the basis and
        # inherit a built-in entry's numbers, and those numbers must not become
        # a price nothing keeps current.
        return None
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


def compile_rate_registry(
    model_registry: dict[str, AgentSpec],
    reachable: tuple[ReachableIdentity, ...],
) -> RateRegistry:
    """Build the registry for one configuration.

    Every priced entry comes from a spec in ``model_registry`` — the merged
    catalog + ``forge.yaml`` view — so a figure in the compiled registry is one
    an operator could have supplied. Nothing is widened onto an identity the
    registry does not describe.
    """
    entries: dict[DispatchIdentity, RateEntry] = {}

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

    return RateRegistry(
        entries=entries,
        reachable={reach.identity: reach.paths for reach in reachable},
    )


def report_unaccountable_identities(
    registry: RateRegistry,
    reachable: tuple[ReachableIdentity, ...],
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
    registry = compile_rate_registry(getattr(config, "model_registry", None) or {}, reachable)
    install(registry)
    report_unaccountable_identities(registry, reachable)
    return registry
