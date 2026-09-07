"""Account availability as a routing input (#2950).

``resolve_model_availability`` (see :mod:`theforge.config.auth`) answers, per
account and auth mode, whether one dispatch identity is invocable *now*. #2949
produced that answer and wired it into ``forge check-config`` as a diagnostic.
This module is where the answer becomes a routing input: it builds the targets
routing needs, resolves them at each selection boundary, and renders the
canonical operator-facing text for an exclusion.

Three states, and only one of them narrows anything:

- ``unavailable`` — positive evidence from the account catalog that this
  credential cannot invoke this model. A hard exclusion: dispatching anyway
  spends money on a call that cannot succeed.
- ``unverified`` — no catalog to ask, or the ask failed. Fully eligible,
  routed exactly as before, warned about once per model per run. A provider
  that publishes no catalog must not lose candidates to a question nobody
  can answer.
- ``available`` — invocable. No routing effect beyond staying in the pool.

The exclusion *policy* (which roles draw from which pool, and what happens when
a pool empties) lives in :mod:`theforge.assignment`, the component that decides
where work goes. This module only supplies the fact and its presentation,
mirroring the split between :mod:`theforge.model_capabilities` and the
capability gate that consumes it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from .config import ModelProfile, TransportSpec, resolve_agent_spec
from .config.auth import ModelAvailabilityTarget, resolve_model_availability
from .config.bridge import model_ref_to_profile
from .config.model_identity import (
    MODEL_AVAILABILITY_UNAVAILABLE,
    MODEL_AVAILABILITY_UNVERIFIED,
    ModelAvailability,
)
from .config.models import (
    RETIRED_MODEL_REGISTRY,
    canonical_model_id,
    model_fallback_transport,
    normalize_model_key,
)
from .config.profiles import _apply_transport_fallback

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import AgentDef, ForgeConfig

log = logging.getLogger(__name__)

__all__ = [
    "AvailabilityWarnings",
    "agent_availability_targets",
    "availability_detail",
    "availability_target",
    "config_availability_targets",
    "format_unavailable_detail",
    "format_unverified_detail",
    "is_unavailable",
    "is_unverified",
    "resolve_agent_availability",
]


# ── Target construction ────────────────────────────────────────────────


def availability_target(
    profile: ModelProfile,
    config: "ForgeConfig",
    *,
    key: str,
    model: str | None = None,
    transport: TransportSpec | None = None,
    provider: str | None = None,
    base_url: str | None = None,
) -> ModelAvailabilityTarget | None:
    """Build an account-catalog target from a profile's actual dispatch identity.

    Lives here rather than in the ``check-config`` command so routing and the
    diagnostic resolve the *same* identity: an availability answer that differs
    between the two surfaces would make the diagnostic unable to explain the
    router.
    """
    effective_transport = transport or profile.transport
    effective_provider = provider or profile.provider_family
    effective_model = model or profile.model
    if effective_transport is None or effective_provider is None:
        return None
    canonical_id = canonical_model_id(
        effective_provider, effective_model, effective_transport.kind
    )
    # Retired packaged identities deliberately raise during ordinary model
    # resolution so routing cannot select them.  Callers still need their
    # maintained identity evidence to report them unavailable rather than
    # treating the direct profile as an unknown account catalog entry.
    retired = RETIRED_MODEL_REGISTRY.get(canonical_id)
    if retired is not None:
        identity = retired.identity
    else:
        try:
            spec = resolve_agent_spec(canonical_id, registry=config.model_registry)
            identity = spec.identity
        except ValueError:
            identity = None
    kwargs = {} if identity is None else {"identity": identity}
    return ModelAvailabilityTarget(
        canonical_id=canonical_id,
        provider=effective_provider,
        model=effective_model,
        transport=effective_transport,
        base_url=profile.base_url if base_url is None else base_url,
        key=key,
        **kwargs,
    )


def config_availability_targets(config: "ForgeConfig") -> list[ModelAvailabilityTarget]:
    """Enumerate every configured identity a normal dispatch can reach.

    Keys are ``<role>:<slot>`` so a caller can ask a role-scoped question ("is
    every candidate for this phase unavailable?") without re-deriving which
    profile belongs to which phase.
    """
    targets: list[ModelAvailabilityTarget] = []
    seen: set[tuple[str, str, str, str, str | None]] = set()

    def add(target: ModelAvailabilityTarget | None) -> None:
        if target is None:
            return
        identity = (
            target.canonical_id,
            target.transport.runner,
            target.transport.kind,
            target.provider,
            target.base_url,
        )
        if identity not in seen:
            seen.add(identity)
            targets.append(target)

    def add_profile(role: str, profile: ModelProfile) -> None:
        profile = _apply_transport_fallback(profile, config.transport_fallbacks)
        add(availability_target(profile, config, key=f"{role}:primary"))
        provider = profile.provider_family
        fallback_transport = model_fallback_transport(provider)
        if provider and fallback_transport:
            for index, fallback_model in enumerate(profile.fallback_models):
                add(
                    availability_target(
                        profile,
                        config,
                        key=f"{role}:model-fallback:{index}",
                        model=fallback_model,
                        transport=fallback_transport,
                        provider=provider,
                    )
                )
        if profile.api_fallback is not None:
            fallback = profile.api_fallback
            add(
                availability_target(
                    profile,
                    config,
                    key=f"{role}:transport-fallback",
                    model=fallback.model,
                    transport=fallback.transport(),
                    provider=fallback.provider,
                    base_url=fallback.base_url,
                )
            )

    add_profile("preflight", config.preflight_profile)
    add_profile("dev", config.dev_profile)
    if config.preflight_fallback_profile is not None:
        add_profile("preflight-fallback", config.preflight_fallback_profile)
    for index, profile in enumerate(config.review_pool):
        add_profile(f"review:{index}", profile)
    if config.synthesis_profile is not None:
        add_profile("synthesis", config.synthesis_profile)
    if config.plan.enabled:
        add_profile("plan", model_ref_to_profile("plan", config.plan.ref))
    if config.plan_agent_review.enabled:
        for index, profile in enumerate(config.plan_agent_review.profiles):
            add_profile(f"plan-review:{index}", profile)
    for index, agent in enumerate(config.agents):
        add_profile(f"agent:{index}", agent.to_model_profile(allowed_tools=()))
    if config.knowledge.run_summaries and config.knowledge.ref is not None:
        add_profile(
            "knowledge-summary",
            model_ref_to_profile("knowledge_summary", config.knowledge.ref, allowed_tools=()),
        )
    for index, model_key in enumerate(config.models or ()):
        try:
            spec = resolve_agent_spec(model_key, registry=config.model_registry)
        except ValueError:
            # Retired identities cannot resolve to a dispatchable AgentSpec, but
            # the caller must still see the maintained withdrawal evidence.
            # Normalize aliases first, matching resolve_agent_spec's lookup path.
            spec = RETIRED_MODEL_REGISTRY.get(normalize_model_key(model_key))
            if spec is None:
                continue
        add(
            ModelAvailabilityTarget(
                canonical_id=canonical_model_id(spec.provider, spec.model, spec.transport.kind),
                provider=spec.provider,
                model=spec.model,
                transport=spec.transport,
                base_url=spec.base_url,
                identity=spec.identity,
                key=f"models:{index}",
            )
        )
    return targets


def agent_availability_targets(
    agents: "Iterable[AgentDef]",
    config: "ForgeConfig",
) -> list[ModelAvailabilityTarget]:
    """Build one target per routing-pool agent, keyed by ``AgentDef.name``.

    The key convention matters. Candidate pools, capability exclusions and the
    ``routing_decision`` block are all keyed on the agent *name*, while a
    catalog answer is scoped to a dispatch identity (provider, transport, auth
    mode, endpoint). Two pool entries naming the same model under different
    credentials are separate agents and get separate answers; keying on the
    name is what keeps them from collapsing into one.
    """
    targets: list[ModelAvailabilityTarget] = []
    for agent in agents:
        profile = _apply_transport_fallback(
            agent.to_model_profile(allowed_tools=()), config.transport_fallbacks
        )
        target = availability_target(profile, config, key=agent.name)
        if target is not None:
            targets.append(target)
    return targets


# ── Resolution ─────────────────────────────────────────────────────────


def resolve_agent_availability(
    agents: "Iterable[AgentDef]",
    config: "ForgeConfig",
    *,
    resolve: Callable[..., dict[str, ModelAvailability]] | None = None,
) -> dict[str, ModelAvailability]:
    """Resolve the current account answer for each pool agent, by agent name.

    Called at each selection boundary rather than once at sprint start: an
    account catalog that changes mid-sprint must change routing for the stories
    that follow, without a restart. Catalog fetches are shared within one call
    by dispatch identity, so a pool of N models on one account costs one lookup.

    Never raises. A resolver failure leaves every candidate *unverified*, which
    is fully eligible — an availability probe that cannot run must not be able
    to empty a pool.
    """
    resolver = resolve or resolve_model_availability
    targets = agent_availability_targets(agents, config)
    if not targets:
        return {}
    try:
        return resolver(targets, config.secrets)
    except Exception as exc:  # noqa: BLE001 - see docstring: never narrows a pool
        log.warning(
            "model availability could not be resolved, treating all as unverified: %s", exc
        )
        return {}


# ── Reading an answer ──────────────────────────────────────────────────


def is_unavailable(availability: ModelAvailability | None) -> bool:
    """True only for positive account evidence that the model cannot be invoked."""
    return availability is not None and availability.state == MODEL_AVAILABILITY_UNAVAILABLE


def is_unverified(availability: ModelAvailability | None) -> bool:
    """True when no account catalog could answer for this identity."""
    return availability is not None and availability.state == MODEL_AVAILABILITY_UNVERIFIED


def availability_detail(availability: ModelAvailability) -> dict[str, object]:
    """Canonical, secret-free detail recorded with an availability exclusion.

    Carries what an operator needs to decide whether the answer is believable:
    which credential family answered, when, how fresh that evidence is, and the
    provider's own reason. Never the credential itself.
    """
    return {
        "state": availability.state,
        "auth_mode": availability.auth_mode,
        "checked_at": (availability.checked_at.isoformat() if availability.checked_at else None),
        "freshness": availability.freshness,
        "reason": availability.reason,
    }


def _checked_stamp(detail: Mapping[str, Any]) -> str:
    checked_at = detail.get("checked_at")
    if not checked_at:
        return "never checked"
    return f"checked {checked_at}"


def format_unavailable_detail(detail: Mapping[str, Any]) -> str:
    """Render an availability exclusion the way the operator surfaces show it."""
    auth_mode = detail.get("auth_mode") or "unknown auth"
    reason = detail.get("reason")
    inner = f"{reason}, {_checked_stamp(detail)}" if reason else _checked_stamp(detail)
    return f"not available to this account under {auth_mode} ({inner})"


def format_unverified_detail(name: str, availability: ModelAvailability) -> str:
    """Render the one-time unverified warning for *name*."""
    detail = availability_detail(availability)
    reason = detail.get("reason") or "no account catalog to consult"
    return (
        f"{name} availability unconfirmed under {detail.get('auth_mode') or 'unknown auth'} "
        f"({reason}) — routed normally"
    )


# ── One warning per model, per run ─────────────────────────────────────


class AvailabilityWarnings:
    """Emits each model's availability warning at most once per run.

    A sprint routes every story through the same pool, so a per-selection
    warning would repeat the identical line once per story per phase and bury
    the answers that actually changed. The tracker is keyed on the candidate
    name *and* the state it was warned about, so a later story reusing the same
    model stays quiet, a newly-configured model still gets its warning, and a
    model whose answer changes mid-sprint is announced again under its new
    state rather than silently suppressed.
    """

    def __init__(self) -> None:
        self._warned: set[tuple[str, str]] = set()

    def reset(self) -> None:
        self._warned.clear()

    def pending(self, availability: Mapping[str, ModelAvailability]) -> list[str]:
        """Return names with a not-yet-warned answer, marking them warned."""
        fresh: list[str] = []
        for name in sorted(availability):
            answer = availability[name]
            if not (is_unverified(answer) or is_unavailable(answer)):
                continue
            key = (name, answer.state)
            if key in self._warned:
                continue
            self._warned.add(key)
            fresh.append(name)
        return fresh

    def emit(
        self,
        availability: Mapping[str, ModelAvailability],
        log_line: Callable[[str], None] | None = None,
    ) -> list[str]:
        """Warn once per not-yet-warned answer; return the names warned about."""
        warned = self.pending(availability)
        for name in warned:
            answer = availability[name]
            if is_unavailable(answer):
                rendered = format_unavailable_detail(availability_detail(answer))
                body = f"{name} excluded — {rendered}"
            else:
                body = format_unverified_detail(name, answer)
            message = f"⚠ ROUTING  {body}"
            if log_line is not None:
                log_line(message)
            else:
                log.warning(message)
        return warned


# Process-wide tracker. "Exactly once per model" is a property of the run, and
# the coordinator resolves availability once per story, so the counter cannot
# live inside a single selection call.
AVAILABILITY_WARNINGS = AvailabilityWarnings()
