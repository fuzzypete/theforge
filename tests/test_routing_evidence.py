"""Routing evidence as a value (#2349).

The routing trace used to exist only as a 36-argument call into
``_build_routing_decision``: it could not be constructed, mutated, or asserted
against without running ``assign_models`` end to end. These tests exercise the
named value directly — building evidence by hand, handing it to
``_build_routing_decision``, and reading the audit block back — so routing
evidence can change independently of the 650-line routing pass.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from theforge.assignment import AssignmentDecision, _build_routing_decision
from theforge.config import AgentDef
from theforge.config.types import ModelProfile
from theforge.routing_evidence import (
    ReviewerEvidence,
    RoutingEvidence,
    RoutingInputs,
    SignalAudit,
)


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="gpt",
            provider="openai",
            model="gpt-5.4",
            budget_usd=8.0,
            timeout_seconds=900,
            tier="strong",
        ),
    ]


def _profile(name: str, provider: str, model: str) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=600,
        allowed_tools=[],
    )


def _decision() -> AssignmentDecision:
    return AssignmentDecision(
        preflight=_profile("sonnet", "anthropic", "sonnet"),
        planner=_profile("opus", "anthropic", "opus"),
        plan_reviewers=[_profile("gpt", "openai", "gpt-5.4")],
        dev=_profile("opus", "anthropic", "opus"),
        code_reviewers=[_profile("gpt", "openai", "gpt-5.4")],
        rationale={"dev": "hand-built", "code_review": "hand-built"},
    )


def _inputs(**kwargs) -> RoutingInputs:
    defaults: dict = {
        "origin": "preflight",
        "score": 9,
        "dev_base_tier": "strong",
        "preflight_tier": "mid",
        "planner_tier": "strong",
        "secrets": {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"},
    }
    defaults.update(kwargs)
    return RoutingInputs(**defaults)


def test_evidence_is_constructible_without_assign_models():
    """The accumulator has a name and a default shape of its own (AC 1/3)."""
    evidence = RoutingEvidence()
    assert evidence.dev_signals == {}
    assert evidence.dev_exploration is None
    assert evidence.reasoning_effort_block is None
    # Each reviewer phase owns an independent completion/value pair — writing one
    # must never leak into the other's history (#1443 / #2156).
    evidence.plan_review.value.signals["gpt"] = {"uniqueness": 0.9}
    assert evidence.code_review.value.signals == {}
    assert isinstance(evidence.plan_review, ReviewerEvidence)
    assert isinstance(evidence.preflight_reliability, SignalAudit)

    # Two instances share no mutable state (default_factory, not a class attr).
    other = RoutingEvidence()
    assert other.plan_review.value.signals == {}


def test_inputs_are_frozen_and_normalize_domains():
    inputs = _inputs(domains=("backend", "", "cli"))
    assert inputs.requested_domains == ["backend", "cli"]
    with pytest.raises(FrozenInstanceError):
        inputs.origin = "post_plan"  # type: ignore[misc]


def test_build_routing_decision_reads_evidence_not_call_site():
    """Every accumulated block reaches the audit trail from the named value."""
    evidence = RoutingEvidence(
        dev_effective_tier="strong",
        dev_signals={"opus": {"raw": 0.75, "weighted": 0.7, "runs": 12, "floor": "pass"}},
        dev_domain_signals={"opus": {"rate": 0.8, "runs": 9}},
        dev_cost_signals={
            "opus": {"value": 1.2, "source": "observed", "observations": 4, "cohort": "dev"}
        },
        dev_domain_match={
            "domain_influenced": True,
            "complexity_only_head": "gpt",
            "domain_head": "opus",
        },
        promotion_block={"mechanism": "_check_promotion", "fired": True, "outcome": "promoted"},
        dev_exploration={"mode": "challenger", "routing_key": "dev:large"},
        reasoning_effort_block={"applied": True, "phases": {}},
    )
    block = _build_routing_decision(
        _decision(), _agents(), _inputs(domains=("backend",), excluded_for_taint=3), evidence
    )

    assert block["origin"] == "preflight"
    assert block["excluded_for_taint"] == 3
    assert block["reasoning_effort"] is evidence.reasoning_effort_block
    assert block["dev"]["promotion_check"] is evidence.promotion_block
    assert block["dev"]["exploration"] is evidence.dev_exploration
    assert block["dev"]["base_tier_from_score"] == "strong"

    opus_entry = next(e for e in block["dev"]["candidate_pool"] if e["name"] == "opus")
    assert opus_entry["signals"]["success_rate"] is evidence.dev_signals["opus"]
    assert opus_entry["signals"]["domain"] is evidence.dev_domain_signals["opus"]
    assert opus_entry["signals"]["cost_tiebreak"] is evidence.dev_cost_signals["opus"]

    assert block["dev"]["domain_match"]["influenced"] is True
    assert block["dev"]["domain_match"]["domains"] == ["backend"]
    assert block["dev"]["cost_tiebreak"]["selected_model"] == "opus"


def test_domain_and_cost_blocks_absent_when_evidence_is_empty():
    """Omission is driven by the evidence, not by the caller's argument list."""
    block = _build_routing_decision(_decision(), _agents(), _inputs(), RoutingEvidence())
    assert "domain_match" not in block["dev"]
    assert "cost_tiebreak" not in block["dev"]
    assert block["dev"]["exploration"] == {"mode": "winner"}
    # No completion/value/reliability evidence was collected → those blocks stay
    # omitted rather than being recorded as empty.
    assert "completion_check" not in block["plan_review"]
    assert "value_check" not in block["code_review"]
    assert "reliability_check" not in block["planner"]


def test_reviewer_evidence_lands_on_its_own_phase():
    """plan_review and code_review evidence never cross-contaminate."""
    evidence = RoutingEvidence(
        plan_review=ReviewerEvidence(
            completion=SignalAudit(
                signals={"gpt": {"completion_rate": 0.9, "runs": 10, "floor": "pass"}},
                audit={"applied": True, "threshold": 0.7, "min_runs": 5},
            )
        ),
    )
    block = _build_routing_decision(_decision(), _agents(), _inputs(), evidence)
    assert "completion_check" in block["plan_review"]
    assert block["plan_review"]["completion_check"]["fired"] is True
    assert "completion_check" not in block["code_review"]


def test_evidence_can_be_mutated_after_construction():
    """The accumulator is writable between routing steps, as assign_models needs."""
    evidence = RoutingEvidence()
    evidence.dev_signals["opus"] = {"raw": 0.5}
    evidence.dev_effective_tier = "strong"
    evidence.reasoning_effort_block = {"applied": False, "phases": {}}

    block = _build_routing_decision(_decision(), _agents(), _inputs(), evidence)
    opus_entry = next(e for e in block["dev"]["candidate_pool"] if e["name"] == "opus")
    assert opus_entry["signals"]["success_rate"] == {"raw": 0.5}
    assert block["reasoning_effort"] == {"applied": False, "phases": {}}


def test_final_models_track_the_decision_not_the_inputs():
    """Post-budget downgrades reach the block through the decision argument, so
    the self-exclusion targets can never go stale (the reason planner_model /
    dev_model are read from the decision rather than passed separately)."""
    downgraded = replace(_decision(), dev=_profile("sonnet", "anthropic", "sonnet"))
    block = _build_routing_decision(
        downgraded,
        _agents(),
        _inputs(dev_base_tier="mid"),
        RoutingEvidence(dev_effective_tier="mid"),
    )
    assert block["dev"]["final"]["model"] == "sonnet"
    cr_pool = {e["name"]: e for e in block["code_review"]["candidate_pool"]}
    # The seated dev model is the anti-self-review exclusion target.
    assert cr_pool["sonnet"]["included"] is False
