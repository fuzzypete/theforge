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
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
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
from .config.role_overrides import explicit_role_overrides

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import AgentDef, ForgeConfig

log = logging.getLogger(__name__)

__all__ = [
    "AvailabilityAnnouncement",
    "AvailabilityWarnings",
    "StoryAvailability",
    "agent_availability_targets",
    "announcements",
    "availability_detail",
    "availability_target",
    "config_availability_targets",
    "format_unavailable_detail",
    "format_unverified_detail",
    "PhaseCandidates",
    "is_unavailable",
    "is_unverified",
    "phase_candidate_profiles",
    "phase_candidate_sets",
    "preflight_dispatch_profiles",
    "profile_dispatch_key",
    "resolve_agent_availability",
    "resolve_story_availability",
    "run_warning_key",
    "unavailable_candidates",
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


def dispatch_key(target: ModelAvailabilityTarget) -> str:
    """The identity an account answer is actually scoped to.

    Not the model name, and not the agent name. A catalog answers for a
    *dispatch identity*: this provider family, over this transport and runner,
    at this endpoint. Two configured candidates naming the same model under
    different credentials or endpoints are different identities and get
    different answers; two that agree on all four are the same identity and
    share one answer (and one catalog lookup).

    Everything that counts candidates — the launch gate's "is every candidate
    for this phase unavailable", the router's pools — keys on this, so a phase
    holding two profiles that happen to share a model string can never collapse
    into one entry and read as though a candidate were still standing.
    """
    return "|".join(
        str(part or "")
        for part in (
            target.canonical_id,
            target.transport.kind,
            target.transport.runner,
            target.provider,
            target.base_url,
        )
    )


def profile_dispatch_key(profile: ModelProfile, config: "ForgeConfig") -> str | None:
    """Dispatch key for a configured profile, or None when it names no identity.

    Returns None rather than raising for a profile whose dispatch fields cannot
    be read. A candidate whose identity is underivable has no account answer, so
    it is simply not counted — the alternative is an availability probe taking
    down a run it was only ever meant to inform.
    """
    try:
        resolved = _apply_transport_fallback(profile, config.transport_fallbacks)
        target = availability_target(resolved, config, key="")
        return None if target is None else dispatch_key(target)
    except Exception:  # noqa: BLE001 - see docstring: never fails a caller
        return None


def agent_availability_targets(
    agents: "Iterable[AgentDef]",
    config: "ForgeConfig",
) -> list[ModelAvailabilityTarget]:
    """Build one target per routing-pool agent, keyed by ``AgentDef.name``.

    Kept keyed by name because candidate pools, capability exclusions and the
    ``routing_decision`` block are all keyed on the agent name. The *answer*
    behind each key is still identity-scoped — see :func:`dispatch_key` — so
    two agents that differ only in name share one lookup, while two naming the
    same model under different credentials get their own.
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


@dataclass(frozen=True)
class StoryAvailability:
    """Every configured identity's current account answer, for one story.

    Resolved once at the story's own boundary and consulted by every decision
    that story makes: the preflight dispatch, adaptive routing, the fixed-profile
    (non-adaptive) path, and explicit pinned pools. One resolution, so those
    decisions cannot disagree with each other, and one set of catalog fetches.

    Keyed by :func:`dispatch_key`. Lookups take a profile or an agent and derive
    the key, so no caller has to know the convention.
    """

    answers: dict[str, ModelAvailability] = field(default_factory=dict)

    def for_profile(
        self, profile: ModelProfile | None, config: "ForgeConfig"
    ) -> ModelAvailability | None:
        """The answer for what *profile* would actually dispatch, if known."""
        if profile is None:
            return None
        key = profile_dispatch_key(profile, config)
        return None if key is None else self.answers.get(key)

    def for_agent(self, agent: "AgentDef", config: "ForgeConfig") -> ModelAvailability | None:
        """The answer for a routing-pool agent."""
        return self.for_profile(agent.to_model_profile(allowed_tools=()), config)

    def by_agent(
        self, agents: "Iterable[AgentDef]", config: "ForgeConfig"
    ) -> dict[str, ModelAvailability]:
        """Answers keyed by ``AgentDef.name``, the shape the router consumes."""
        resolved: dict[str, ModelAvailability] = {}
        for agent in agents:
            answer = self.for_agent(agent, config)
            if answer is not None:
                resolved[agent.name] = answer
        return resolved

    def by_profile(
        self, profiles: "Mapping[str, ModelProfile]", config: "ForgeConfig"
    ) -> dict[str, ModelAvailability]:
        """Answers for named profiles, keyed by the caller's own labels."""
        resolved: dict[str, ModelAvailability] = {}
        for label, profile in profiles.items():
            answer = self.for_profile(profile, config)
            if answer is not None:
                resolved[label] = answer
        return resolved


def resolve_story_availability(
    config: "ForgeConfig",
    *,
    resolve: Callable[..., dict[str, ModelAvailability]] | None = None,
) -> StoryAvailability:
    """Resolve every configured identity's account answer for one story.

    Called at the story's own boundary rather than once at sprint start: an
    account catalog that changes mid-sprint must change routing for the stories
    that follow, without a restart. Catalog fetches are shared by dispatch
    identity, so the whole configured surface costs one lookup per account.

    Never raises. A resolver failure yields no answers at all, which leaves
    every candidate eligible — an availability probe that cannot run must not be
    able to empty a pool or stop a run.
    """
    resolver = resolve or resolve_model_availability
    targets: dict[str, ModelAvailabilityTarget] = {}
    for target in config_availability_targets(config):
        key = dispatch_key(target)
        targets.setdefault(key, _dc_replace(target, key=key))
    if not targets:
        return StoryAvailability()
    try:
        return StoryAvailability(dict(resolver(list(targets.values()), config.secrets)))
    except Exception as exc:  # noqa: BLE001 - see docstring: never narrows a pool
        log.warning(
            "model availability could not be resolved, treating all as unverified: %s", exc
        )
        return StoryAvailability()


def resolve_agent_availability(
    agents: "Iterable[AgentDef]",
    config: "ForgeConfig",
    *,
    resolve: Callable[..., dict[str, ModelAvailability]] | None = None,
) -> dict[str, ModelAvailability]:
    """Resolve the current account answer for each pool agent, by agent name.

    A thin view over :func:`resolve_story_availability` for callers that only
    route the adaptive pool. Never raises, for the same reason.
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


# ── What each phase can actually dispatch ──────────────────────────────
#
# Derived from config alone and consumed by both the pre-dispatch launch gate
# (theforge.sprint.availability_gate) and the coordinator's routing. It lives
# here, beside the answers it is compared against, because the two callers sit
# in packages that must not import each other to share it.


@dataclass(frozen=True)
class PhaseCandidates:
    """What one phase may dispatch, and where the candidates came from.

    ``from_pool`` is load-bearing for anything deciding whether a phase can
    still be seated. Candidates drawn from the adaptive pool answer to the
    *other* hard eligibility rules too — the capability record, the
    ``dev_capable`` declaration — while a pinned or fixed profile answers to
    availability alone, because nothing else narrows a set the operator named.
    Judging a pinned phase against the pool (or the reverse) produced both a
    stop for a story whose configured candidates were all reachable and a
    charge for a pin nothing had checked (#2950 review).
    """

    profiles: tuple[ModelProfile, ...]
    from_pool: bool


def phase_candidate_sets(config: "ForgeConfig") -> dict[str, PhaseCandidates]:
    """The one derivation of what each phase dispatches, with its provenance.

    Every caller that asks "can this phase still run?" reads this: the sprint
    launch gate, the pre-spend check that runs before preflight is paid for, and
    the static filter. They had drifted into separate derivations, and each
    disagreement surfaced as either a false stop or a paid discovery.

    Two rules decide a phase's candidate set:

    - **preflight** dispatches ``config.preflight_profile`` and, when the
      primary cannot be invoked, its configured fallback. It runs *before*
      routing — routing needs the complexity score preflight produces — so the
      adaptive pool is never its candidate set, and the fallback is part of it:
      a check that saw only the primary refused a story whose fallback was
      about to run perfectly well.
    - every **later** phase is chosen by ``assign_models`` from the adaptive
      pool, unless the operator pinned it. A pinned *reviewer* role is a pool,
      not a single model — ``overrides.profiles`` holds only the head that locks
      the role against budget downgrade — so the whole pinned pool is the
      candidate set.
    """
    overrides = explicit_role_overrides(config)
    adaptive = bool(config.assignment.enabled and config.agents)

    def pool_profiles(dev_only: bool = False) -> list[ModelProfile]:
        agents: Iterable[AgentDef] = config.agents
        if dev_only:
            agents = [a for a in config.agents if a.dev_capable]
        return [a.to_model_profile(allowed_tools=()) for a in agents]

    def candidates(
        phase: str,
        *,
        pin_role: str | None = None,
        pinned_pool: "tuple[ModelProfile, ...]" = (),
        dev_only: bool = False,
        fixed: "list[ModelProfile] | None" = None,
    ) -> PhaseCandidates | None:
        """Resolve one phase, preferring a pin, then the pool, then the fixed profile."""
        if pinned_pool:
            return PhaseCandidates(tuple(pinned_pool), False)
        pinned = overrides.profiles.get(pin_role or phase)
        if pinned is not None:
            return PhaseCandidates((pinned,), False)
        if adaptive:
            return PhaseCandidates(tuple(pool_profiles(dev_only)), True)
        configured = tuple(fixed or ())
        return PhaseCandidates(configured, False) if configured else None

    resolved: dict[str, PhaseCandidates | None] = {
        # Not routed and not pinned-or-pooled: preflight is whatever the config
        # says it is, plus its fallback.
        "preflight": PhaseCandidates(tuple(preflight_dispatch_profiles(config)), False),
        "dev": candidates("dev", dev_only=True, fixed=[config.dev_profile]),
        "code_review": candidates(
            "code_review", pinned_pool=overrides.review_pool, fixed=list(config.review_pool)
        ),
    }
    if config.plan.enabled:
        resolved["plan"] = candidates(
            "plan", pin_role="planner", fixed=[model_ref_to_profile("plan", config.plan.ref)]
        )
    if config.plan_agent_review.enabled and config.plan_agent_review.profiles:
        resolved["plan_review"] = candidates(
            "plan_review",
            pinned_pool=overrides.plan_review_pool,
            fixed=list(config.plan_agent_review.profiles),
        )
    return {
        phase: entry for phase, entry in resolved.items() if entry is not None and entry.profiles
    }


def phase_candidate_profiles(config: "ForgeConfig") -> dict[str, list["ModelProfile"]]:
    """Map each required phase to the profiles it may actually dispatch.

    A view over :func:`phase_candidate_sets` for callers that do not need to
    know where each phase's candidates came from.
    """
    return {phase: list(entry.profiles) for phase, entry in phase_candidate_sets(config).items()}


def preflight_dispatch_profiles(config: "ForgeConfig") -> list["ModelProfile"]:
    """The profiles a preflight invocation can actually use, in attempt order."""
    profiles = [config.preflight_profile]
    if config.preflight_fallback_profile is not None:
        profiles.append(config.preflight_fallback_profile)
    return [p for p in profiles if p is not None]


def unavailable_candidates(
    profiles: "Iterable[ModelProfile]",
    config: "ForgeConfig",
    answers: dict[str, ModelAvailability],
) -> tuple[dict[str, dict[str, object]], int]:
    """Return ``(excluded_by_identity, distinct_candidate_count)``.

    Both sides count *identities*, so the caller's "is every candidate
    excluded?" comparison comes from one accounting rather than two that can
    disagree.
    """
    identities: dict[str, ModelProfile] = {}
    for profile in profiles:
        key = profile_dispatch_key(profile, config)
        if key is not None:
            identities.setdefault(key, profile)
    excluded: dict[str, dict[str, object]] = {}
    for key, profile in identities.items():
        answer = answers.get(key)
        if is_unavailable(answer):
            excluded[key] = {
                "model": profile.model,
                "label": profile.model,
                **availability_detail(answer),
            }
    return excluded, len(identities)


@dataclass(frozen=True)
class AvailabilityAnnouncement:
    """One dispatch identity's answer, and the name to announce it under.

    The identity is what the answer is *about*; the label is only how an
    operator recognises it. Keeping them apart is what stops the same model
    being announced twice because two configuration surfaces call it different
    things, and stops two genuinely different identities being announced once
    because they happen to share a model string (#2950 review).
    """

    identity: str
    label: str
    answer: ModelAvailability


def announcements(
    config: "ForgeConfig",
    availability: "StoryAvailability",
    *,
    agents: "Iterable[AgentDef]" = (),
    profiles: "Iterable[ModelProfile]" = (),
) -> list[AvailabilityAnnouncement]:
    """Collect one announcement per dispatch identity across both surfaces.

    A run reaches its models through two vocabularies: the adaptive pool names
    them (``AgentDef.name``) and the fixed phase configuration names them by
    model. The same identity often appears in both. Deduplicating on the
    identity — and preferring the pool's name as the label, since that is what
    the routing decision and the operator's config call it — is what makes
    "warn once per model per run" true regardless of how many surfaces mention
    it.
    """
    seen: dict[str, AvailabilityAnnouncement] = {}
    # Profiles first, so an agent name (the more recognisable label) wins the
    # tie for an identity both surfaces carry.
    for profile in profiles:
        key = profile_dispatch_key(profile, config)
        answer = availability.answers.get(key) if key else None
        if key and answer is not None:
            seen.setdefault(key, AvailabilityAnnouncement(key, profile.model, answer))
    for agent in agents:
        key = profile_dispatch_key(agent.to_model_profile(allowed_tools=()), config)
        answer = availability.answers.get(key) if key else None
        if key and answer is not None:
            seen[key] = AvailabilityAnnouncement(key, agent.name, answer)
    return sorted(seen.values(), key=lambda entry: entry.label)


# ── One warning per model, per run ─────────────────────────────────────


class AvailabilityWarnings:
    """Emits each model's availability warning at most once per run.

    A sprint routes every story through the same pool, so a per-selection
    warning would repeat the identical line once per story per phase and bury
    the answers that actually changed. The tracker is keyed on the *dispatch
    identity* and the state it was warned about, so a later story reusing the
    same model stays quiet, a newly-configured model still gets its warning, and
    a model whose answer changes mid-sprint is announced again under its new
    state rather than silently suppressed.

    Keyed on the identity rather than the display name deliberately. One model
    can appear in the run's configuration twice under different names — as a
    pool agent and as a fixed phase profile — and keying on the name would
    announce one identity twice. The label rides along on the announcement for
    display only.

    **Scoped to a run, not to a process.** Callers pass the run they are warning
    for — the sprint name, or the task's run id outside a sprint — and a new run
    clears what the previous one warned about. Without that, a second sprint in
    the same process (the daemon, the test suite, a scripted batch) inherits the
    first one's warned set and announces nothing, which is the opposite of the
    "exactly once per run" the spec asks for.

    Thread-safe: the sprint runs its stories on a ``ThreadPoolExecutor``, so
    parallel workers reach one tracker concurrently and the check-then-mark must
    be atomic or two stories can both decide they are the first to warn.
    """

    def __init__(self) -> None:
        self._warned: set[tuple[str, str]] = set()
        self._run_key: str | None = None
        self._lock = threading.Lock()

    def reset(self, run_key: str | None = None) -> None:
        """Start a new run's warning scope."""
        with self._lock:
            self._warned.clear()
            self._run_key = run_key

    def pending(
        self,
        entries: "Iterable[AvailabilityAnnouncement]",
        run_key: str | None = None,
    ) -> list[AvailabilityAnnouncement]:
        """Return announcements not yet warned about, marking them warned."""
        fresh: list[AvailabilityAnnouncement] = []
        with self._lock:
            if run_key is not None and run_key != self._run_key:
                # A different run than the one this set describes. Its warnings
                # say nothing about this run, so start clean rather than let an
                # earlier run silence this one.
                self._warned.clear()
                self._run_key = run_key
            for entry in sorted(entries, key=lambda item: item.label):
                answer = entry.answer
                if not (is_unverified(answer) or is_unavailable(answer)):
                    continue
                key = (entry.identity, answer.state)
                if key in self._warned:
                    continue
                self._warned.add(key)
                fresh.append(entry)
        return fresh

    def emit(
        self,
        entries: "Iterable[AvailabilityAnnouncement]",
        log_line: Callable[[str], None] | None = None,
        run_key: str | None = None,
    ) -> list[str]:
        """Warn once per not-yet-warned answer; return the labels warned about."""
        warned = self.pending(entries, run_key)
        for entry in warned:
            if is_unavailable(entry.answer):
                rendered = format_unavailable_detail(availability_detail(entry.answer))
                body = f"{entry.label} excluded — {rendered}"
            else:
                body = format_unverified_detail(entry.label, entry.answer)
            message = f"⚠ ROUTING  {body}"
            if log_line is not None:
                log_line(message)
            else:
                log.warning(message)
        return [entry.label for entry in warned]


# The shared tracker. "Exactly once per model" is a property of the run, and the
# coordinator resolves availability once per story, so the counter cannot live
# inside a single selection call. Run scoping is carried by the ``run_key``
# argument rather than by the object's lifetime — see :meth:`pending`.
AVAILABILITY_WARNINGS = AvailabilityWarnings()


def run_warning_key(state: object) -> str | None:
    """The run a coordinator state belongs to, for warning suppression.

    A sprint is one run spanning many stories, so its stories share a key and
    a model is announced once for the whole sprint. A standalone task run is its
    own run and uses its run id. Returns None when neither is known, which
    leaves the current scope untouched rather than inventing a new one.
    """
    sprint_name = getattr(state, "sprint_name", None)
    if sprint_name:
        return f"sprint:{sprint_name}"
    run_id = getattr(state, "run_id", None)
    return f"run:{run_id}" if run_id else None
