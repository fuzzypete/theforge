"""Sprint-launch model-availability gate (#2950).

A phase with no invocable model cannot produce anything, and the run should
establish that *before* it spends. In the 2026-09-04 incident the refusal
arrived after WORKSPACE, preflight, routing and plan had all been charged, and
four stories produced nothing. The account answer was knowable the whole time.

This module owns the pre-dispatch half of the answer: resolve the current
account catalog for every candidate each required phase may draw from, and
refuse to launch when a phase has nothing available or unverified left. It runs
alongside :mod:`theforge.sprint.auth_gate`, ahead of intake remediation, batch
preflight, the base pull and every worktree touch, so the stop costs seconds and
no story acquires a verdict.

Deliberately tier-independent. The router narrows a phase's pool further — by
tier, by exploration, by budget — and enforces availability exactly at that
point (``assign_models`` raises
:class:`~theforge.assignment.NoAvailableModelError`). This gate answers the
coarser question that is knowable before any spend: is *every* candidate this
phase could ever draw from unavailable? A phase that survives here may still be
refused by the router when tier narrowing empties it, which is why the two
layers exist rather than one.

Refuses only on **positive** evidence, like the auth gate: an ``unverified``
answer — no catalog to ask, or an ask that failed — leaves every candidate
eligible. A model availability probe that cannot run must never be able to stop
a sprint.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING, NamedTuple

from ..config.model_identity import ModelAvailability
from ..model_availability import (
    availability_detail,
    availability_target,
    dispatch_key,
    format_unavailable_detail,
    is_unavailable,
    profile_dispatch_key,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import AgentDef, ModelProfile
    from ..config.types import ForgeConfig


class SprintNoAvailableModel(RuntimeError):
    """A sprint cannot start because a required phase has no invocable model.

    Raised before any story is dispatched, so no story acquires a failure
    verdict and nothing about the work is asserted. This is a routing outcome:
    no model was asked to do anything, so nothing is recorded against any
    model's capability history.
    """


class PhaseUnavailable(NamedTuple):
    """One phase whose entire candidate set the account cannot invoke.

    ``excluded`` is keyed by *dispatch identity*, never by model name. Two
    candidates that happen to share a model string under different credentials
    or endpoints are two candidates, and collapsing them would make a phase with
    two unavailable models look like a phase with one unavailable model and one
    survivor — which reads as "safe to launch". Each record carries its own
    ``model`` for display.
    """

    phase: str
    excluded: dict[str, dict[str, object]]

    @property
    def models(self) -> list[str]:
        """Display names of the excluded candidates, in stable order."""
        return sorted(str(record.get("model") or "?") for record in self.excluded.values())


def phase_candidate_profiles(config: "ForgeConfig") -> dict[str, list["ModelProfile"]]:
    """Map each required phase to the profiles it may actually dispatch.

    Two different questions, and getting them the wrong way round is how an
    unavailable model gets paid for:

    - **preflight** dispatches ``config.preflight_profile`` (and its configured
      fallback). It runs *before* routing — routing needs the complexity score
      preflight produces — so the adaptive pool is not its candidate set, and
      admitting the phase because some other agent in the pool is available
      would clear a dispatch that is about to fail (#2950 review).
    - every **later** phase is chosen by ``assign_models`` from the adaptive
      pool, unless the operator pinned it, in which case the pin is the whole
      candidate set — the same answer the router reaches from the same
      derivation.
    """
    from ..coordinator.preflight import explicit_role_overrides  # noqa: PLC0415

    overrides = explicit_role_overrides(config)
    adaptive = bool(config.assignment.enabled and config.agents)

    def pool(role: str, dev_only: bool = False) -> list["ModelProfile"]:
        pinned = overrides.profiles.get(role)
        if pinned is not None:
            return [pinned]
        if adaptive:
            agents: Iterable[AgentDef] = config.agents
            if dev_only:
                agents = [a for a in config.agents if a.dev_capable]
            return [a.to_model_profile(allowed_tools=()) for a in agents]
        return []

    candidates: dict[str, list[ModelProfile]] = {
        "preflight": preflight_dispatch_profiles(config),
        "dev": pool("dev", dev_only=True) or [config.dev_profile],
        "code_review": pool("code_review") or list(config.review_pool),
    }
    if config.plan.enabled:
        from ..config.bridge import model_ref_to_profile  # noqa: PLC0415

        candidates["plan"] = pool("planner") or [model_ref_to_profile("plan", config.plan.ref)]
    if config.plan_agent_review.enabled and config.plan_agent_review.profiles:
        candidates["plan_review"] = pool("plan_review") or list(config.plan_agent_review.profiles)
    return {phase: profiles for phase, profiles in candidates.items() if profiles}


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
            excluded[key] = {"model": profile.model, **availability_detail(answer)}
    return excluded, len(identities)


def check_sprint_availability(
    config: "ForgeConfig",
    *,
    resolve: Callable[..., dict[str, ModelAvailability]] | None = None,
) -> list[PhaseUnavailable]:
    """Return the phases whose every candidate the account cannot invoke.

    One resolution serves every phase: catalog lookups are shared by dispatch
    identity, so a pool of N models on one account costs one fetch regardless of
    how many phases draw from it.
    """
    from ..config.auth import resolve_model_availability  # noqa: PLC0415

    resolver = resolve or resolve_model_availability
    try:
        candidates = phase_candidate_profiles(config)
    except Exception:  # noqa: BLE001 - an unreadable config never stops a sprint here
        return []
    targets: dict[str, object] = {}
    for profiles in candidates.values():
        for profile in profiles:
            try:
                target = availability_target(profile, config, key="")
            except Exception:  # noqa: BLE001 - a candidate with no derivable identity
                continue
            if target is None:
                continue
            key = dispatch_key(target)
            targets.setdefault(key, _dc_replace(target, key=key))
    if not targets:
        return []
    try:
        answers = resolver(list(targets.values()), config.secrets)
    except Exception:  # noqa: BLE001 - an unrunnable probe never stops a sprint
        return []

    stops: list[PhaseUnavailable] = []
    for phase, profiles in candidates.items():
        excluded, total = unavailable_candidates(profiles, config, answers)
        if total and len(excluded) == total:
            stops.append(PhaseUnavailable(phase=phase, excluded=excluded))
    return stops


def format_availability_stop(stops: list[PhaseUnavailable]) -> str:
    """Render the operator-facing abort message for *stops*.

    Names every excluded model and its reason, and states the spend explicitly:
    an operator reading this must not have to work out whether the run charged
    them for the discovery. This gate runs before any dispatch, so here the
    spend genuinely is zero — the per-story stop states its own spend, which is
    not always the same claim (#2950 review).
    """
    lines: list[str] = []
    for stop in stops:
        detail = ", ".join(
            f"{record.get('model') or '?'} excluded ({format_unavailable_detail(record)})"
            for _identity, record in sorted(
                stop.excluded.items(), key=lambda item: str(item[1].get("model") or "")
            )
        )
        lines.append(f"✗ ROUTING  no model available for phase {stop.phase}: {detail}")
    lines.append("Nothing dispatched, $0.00 spent.")
    lines.append(
        "No story was run and none was marked failed — this is an account "
        "availability condition, not a property of the work."
    )
    return "\n".join(lines)


def enforce_sprint_availability(
    config: "ForgeConfig",
    *,
    log: Callable[[str], None] | None = None,
    resolve: Callable[..., dict[str, ModelAvailability]] | None = None,
) -> None:
    """Stop the sprint launch when a required phase has no invocable model.

    Raises:
        SprintNoAvailableModel: when every candidate for at least one required
            phase carries positive account evidence that it cannot be invoked.
    """
    stops = check_sprint_availability(config, resolve=resolve)
    if not stops:
        return
    message = format_availability_stop(stops)
    if log is not None:
        for line in message.splitlines():
            log(line)
    raise SprintNoAvailableModel(message)


__all__ = [
    "PhaseUnavailable",
    "SprintNoAvailableModel",
    "check_sprint_availability",
    "enforce_sprint_availability",
    "format_availability_stop",
    "phase_candidate_profiles",
    "preflight_dispatch_profiles",
    "unavailable_candidates",
]
