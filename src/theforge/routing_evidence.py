"""Named routing state for a single assignment pass (#2349).

:func:`theforge.assignment.assign_models` decides which model runs each phase
and, along the way, accumulates the evidence for *why*. Before this module that
evidence had no name: it lived as ~76 locals inside ``assign_models`` and was
handed to ``_build_routing_decision`` one field at a time as a 36-parameter
call. This module gives that state a name and an owner so the routing trace is
a value the system holds rather than something reconstructed at the call site.

Two types, split by **when the value becomes known**:

- :class:`RoutingInputs` — everything fixed *before* candidate scoring begins:
  the caller's request (origin, score, domains, reviewer bounds, explicit role
  overrides) and the environment the router selects against (secrets, unhealthy
  models, the score-derived tier floors). Frozen: nothing in the routing pass
  may edit its own inputs.
- :class:`RoutingEvidence` — everything *produced* by the routing pass and the
  post-budget pass: profile signals consulted, rerank audits, the promoted dev
  tier, the exploration decision, and the resolved reasoning-effort block.
  Mutable and owned by ``assign_models``, which is the only writer.

**The rule for new fields**: known before candidate scoring → ``RoutingInputs``;
produced by the routing or budget pass → ``RoutingEvidence``. Two cases that
read the other way and are classified by that rule, not by how they read:

- ``excluded_for_taint`` is *evidence about history* but is computed by the
  caller before ``assign_models`` runs, so it is an input.
- ``dev_effective_tier`` reads like a tier input but is the promotion pass's
  output, so it is evidence.

Pure data only — stdlib imports, no I/O, no policy. The blocks stored here are
the same plain dicts the audit trail records; this module names their owner, it
does not reshape them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SignalAudit:
    """One mechanism's consulted signals plus its ranking-effect audit.

    ``signals`` is keyed by agent name (the per-candidate evidence the rerank
    weighed); ``audit`` is the single block describing whether the mechanism
    fired and what it changed. Both are populated in place by the selection
    helpers that receive them as out-params, so a caller can hand the same
    instance to a selector and read the result back afterwards.
    """

    signals: dict[str, dict] = field(default_factory=dict)
    audit: dict[str, object] = field(default_factory=dict)


@dataclass
class ReviewerEvidence:
    """Rerank evidence accumulated for one reviewer phase.

    Plan review and code review each own an instance so their completion-rate
    (#1388) and value (#1443 / #2156) histories never mix — the two phases read
    separate profile sections and are independently omitted from the routing
    block when their mechanism was not consulted.
    """

    completion: SignalAudit = field(default_factory=SignalAudit)
    value: SignalAudit = field(default_factory=SignalAudit)


@dataclass(frozen=True)
class RoutingInputs:
    """The fixed inputs of one routing pass — known before candidate scoring.

    Frozen because the routing pass is a consumer of its inputs, never an
    editor of them: anything the pass computes belongs in
    :class:`RoutingEvidence`.
    """

    origin: str
    score: int | None
    dev_base_tier: str
    preflight_tier: str | None = None
    planner_tier: str | None = None
    explicit_roles: frozenset[str] = frozenset()
    secrets: dict[str, str] | None = None
    unhealthy_models: set[str] | None = None
    domains: tuple[str, ...] = ()
    min_reviewers: int = 1
    max_reviewers: int = 1
    # Count of historical runs the centralized taint gate set aside before the
    # router read them (ADR-0006 clause 4). Computed by the caller, hence an
    # input despite being evidence about history.
    excluded_for_taint: int = 0

    @property
    def requested_domains(self) -> list[str]:
        """The story's domain tags, filtered to the non-empty strings."""
        return [d for d in self.domains if isinstance(d, str) and d]


@dataclass
class RoutingEvidence:
    """Evidence accumulated *during* one routing pass. Owner: ``assign_models``.

    ``assign_models`` is the sole writer: it constructs one instance at the top
    of the pass, hands the sub-objects to the selection helpers as out-params,
    overwrites the scalar/block fields as each mechanism resolves, and passes
    the whole value to ``_build_routing_decision``. Every other reader treats it
    as read-only.

    Because it is a value, the routing trace can be constructed and asserted
    against without running ``assign_models`` — see
    ``tests/test_routing_evidence.py``.
    """

    # Dev tier after the profile-backed pre-promotion (#158) resolves. Output of
    # the promotion pass, not an input tier.
    dev_effective_tier: str = "cheap"
    # Per-candidate dev signals the router weighed, keyed by agent name.
    dev_signals: dict[str, dict] = field(default_factory=dict)
    # Per-domain (#155) and cost-tiebreak slices of the same rerank pass.
    dev_domain_signals: dict[str, dict] = field(default_factory=dict)
    dev_cost_signals: dict[str, dict] = field(default_factory=dict)
    # Whether the domain tiebreak actually moved the dev selection.
    dev_domain_match: dict[str, object] = field(default_factory=dict)
    # Dev pre-promotion check block (#158); the "not_checked" default stands for
    # explicit-override / static-mode dev where the check never fires.
    promotion_block: dict[str, object] = field(default_factory=dict)
    # Challenger-sampling exploration decision for the dev slot (#325). None
    # when the router produced no exploration decision (static / explicit dev).
    dev_exploration: dict[str, object] | None = None
    plan_review: ReviewerEvidence = field(default_factory=ReviewerEvidence)
    code_review: ReviewerEvidence = field(default_factory=ReviewerEvidence)
    # Single-model reliability rerank for the non-dev roles (#1489).
    preflight_reliability: SignalAudit = field(default_factory=SignalAudit)
    planner_reliability: SignalAudit = field(default_factory=SignalAudit)
    # Per-phase reasoning-effort axis (#1108). Resolved after budget enforcement
    # so it lands on the profiles that actually run; None until then.
    reasoning_effort_block: dict[str, object] | None = None
