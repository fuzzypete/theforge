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
from typing import TYPE_CHECKING, NamedTuple

from ..config.model_identity import ModelAvailability
from ..model_availability import (
    availability_detail,
    availability_target,
    format_unavailable_detail,
    is_unavailable,
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
    """One phase whose entire candidate set the account cannot invoke."""

    phase: str
    excluded: dict[str, dict[str, object]]


def _role_candidate_profiles(config: "ForgeConfig") -> dict[str, list["ModelProfile"]]:
    """Map each required phase to the profiles it may draw from, pins honored.

    Under adaptive routing an unpinned phase draws from the whole agents pool,
    so naming only the currently-derived role profile would refuse sprints the
    router would have routed around. A pinned phase draws from its pin alone —
    the same answer ``assign_models`` reaches from the same derivation.
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
        "preflight": pool("preflight") or [config.preflight_profile],
        "dev": pool("dev", dev_only=True) or [config.dev_profile],
        "code_review": pool("code_review") or list(config.review_pool),
    }
    if config.plan.enabled:
        from ..config.bridge import model_ref_to_profile  # noqa: PLC0415

        candidates["plan"] = pool("planner") or [model_ref_to_profile("plan", config.plan.ref)]
    if config.plan_agent_review.enabled and config.plan_agent_review.profiles:
        candidates["plan_review"] = pool("plan_review") or list(config.plan_agent_review.profiles)
    return {phase: profiles for phase, profiles in candidates.items() if profiles}


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
    candidates = _role_candidate_profiles(config)
    targets = []
    keys: dict[str, list[str]] = {}
    for phase, profiles in candidates.items():
        phase_keys: list[str] = []
        for index, profile in enumerate(profiles):
            key = f"{phase}:{index}:{profile.model}"
            target = availability_target(profile, config, key=key)
            if target is None:
                continue
            phase_keys.append(key)
            targets.append(target)
        keys[phase] = phase_keys
    if not targets:
        return []
    try:
        answers = resolver(targets, config.secrets)
    except Exception:  # noqa: BLE001 - an unrunnable probe never stops a sprint
        return []

    stops: list[PhaseUnavailable] = []
    for phase, phase_keys in keys.items():
        if not phase_keys:
            continue
        excluded = {
            key.split(":", 2)[2]: availability_detail(answers[key])
            for key in phase_keys
            if key in answers and is_unavailable(answers[key])
        }
        if len(excluded) == len(phase_keys):
            stops.append(PhaseUnavailable(phase=phase, excluded=excluded))
    return stops


def format_availability_stop(stops: list[PhaseUnavailable]) -> str:
    """Render the operator-facing abort message for *stops*.

    Names every excluded model and its reason, and states the spend explicitly:
    an operator reading this must not have to work out whether the run charged
    them for the discovery.
    """
    lines: list[str] = []
    for stop in stops:
        detail = ", ".join(
            f"{model} excluded ({format_unavailable_detail(record)})"
            for model, record in sorted(stop.excluded.items())
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
]
