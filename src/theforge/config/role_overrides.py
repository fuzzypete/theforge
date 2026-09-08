"""Which phase roles an operator pinned, derived from config alone.

Two components have to agree on this question and neither may own it. The
router (:func:`theforge.coordinator.preflight._apply_preflight_config`) asks it
to decide which roles bypass candidate selection; the pre-dispatch availability
gate (#2950) asks it to decide which candidates a phase may actually draw from.
A gate that guessed differently from the router would refuse a sprint the router
would have routed, or clear one it is about to refuse.

It lives here rather than in either caller because the derivation reads nothing
but ``ForgeConfig``: no run state, no phase, no I/O. Putting it beside the
configuration it interprets is also what keeps the coordinator and the sprint
packages from importing each other to share it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .bridge import model_ref_to_profile
from .defaults import DEFAULT_INVESTIGATION_TOOLS
from .model_identity import PHASE_PLAN
from .types import ModelProfile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .types import ForgeConfig

__all__ = ["ExplicitRoleOverrides", "explicit_role_overrides"]


@dataclass(frozen=True)
class ExplicitRoleOverrides:
    """The roles an operator pinned in config, and to which profiles.

    ``profiles`` is the single-profile-per-role map ``assign_models`` consumes
    as ``explicit_profiles``; ``review_pool`` / ``plan_review_pool`` carry the
    full pinned pools, whose first entry is what locks the corresponding role
    against budget downgrade.
    """

    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    roles: frozenset[str] = frozenset()
    review_pool: tuple[ModelProfile, ...] = ()
    plan_review_pool: tuple[ModelProfile, ...] = ()


def explicit_role_overrides(config: "ForgeConfig") -> ExplicitRoleOverrides:
    """Derive which roles config pins, from config alone.

    Collects overrides from both the legacy-agents path (``models`` is None) and
    the v0.8 ``models:`` path. The ``is_default`` flags are authoritative
    regardless of which YAML path set them, so those guards are not limited to
    ``models is None``.
    """
    from .defaults import DEFAULT_DEV_PROFILE, DEFAULT_PREFLIGHT_PROFILE  # noqa: PLC0415

    profiles: dict[str, ModelProfile] = {}
    roles: set[str] = set()
    review_pool: tuple[ModelProfile, ...] = ()
    plan_review_pool: tuple[ModelProfile, ...] = ()

    if config.models is None:
        if config.dev_profile is not DEFAULT_DEV_PROFILE:
            profiles["dev"] = config.dev_profile
            roles.add("dev")
        if config.preflight_profile is not DEFAULT_PREFLIGHT_PROFILE:
            profiles["preflight"] = config.preflight_profile
            roles.add("preflight")
    # Materialize before testing emptiness: ``profiles`` below is a computed
    # property, so "is it non-empty" and "what is in it" must be one question
    # asked once, not two that can disagree.
    configured_review_pool = tuple(config.review_pool or ())
    if configured_review_pool and not config.review_pool_is_default:
        roles.add("review_pool")
        review_pool = configured_review_pool
        # Lock code_review against budget downgrade and audit it as overridden.
        profiles["code_review"] = review_pool[0]
    if not config.plan_model_is_default:
        roles.add("planner")
        profiles["planner"] = model_ref_to_profile(
            "plan",
            config.plan.ref,
            # See plan_flow: the plan role names the investigation set rather
            # than borrowing preflight's narrowed one (#2346).
            allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
            phase=PHASE_PLAN,
        )
    configured_plan_review_pool = tuple(config.plan_agent_review.profiles or ())
    if config.plan_agent_review.enabled and configured_plan_review_pool:
        roles.add("plan_agent_review")
        plan_review_pool = configured_plan_review_pool
        profiles["plan_review"] = plan_review_pool[0]

    return ExplicitRoleOverrides(
        profiles=profiles,
        roles=frozenset(roles),
        review_pool=review_pool,
        plan_review_pool=plan_review_pool,
    )
