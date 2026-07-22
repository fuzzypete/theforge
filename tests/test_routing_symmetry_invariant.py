"""Routing-symmetry invariant enforcement (#1389).

Mechanical backstop for the rule documented in
``src/theforge/coordinator/CONVENTIONS.md`` → "Adaptive routing symmetry
invariant": every adaptive, history-driven promotion/deprioritization path must
have either a landed+tested inverse (demotion/re-inclusion) or a catalogued open
follow-up. This test walks ``ROUTING_SYMMETRY_REGISTRY`` and fails when a
promotion path has neither — so a future PR adding a promotion mechanism cannot
land alone without registering its inverse or its asymmetry.

The test is registry-driven, not source-scanning: it resolves the named code
symbols, checks the named test modules reference them, and (for the dev-tier
pair) exercises both directions end-to-end so the named mechanism and its
routing_rationale audit label are tied to real code paths.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from theforge.assignment import (
    MECHANISM_DEV_PROMOTION,
    MECHANISM_POST_PLAN_DEMOTION,
    ROUTING_RATIONALE_DEMOTED,
    ROUTING_RATIONALE_PROMOTED,
    ROUTING_RATIONALE_STATES,
    ROUTING_RATIONALE_STAYED,
    ROUTING_SYMMETRY_REGISTRY,
    AssignmentConfig,
    EscalationRecord,
    RoutingSymmetryPair,
    apply_post_plan_checkpoint,
    assign_models,
)
from theforge.config import AgentDef

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CATALOGUE_DOC = _REPO_ROOT / "docs" / "routing-symmetry-followups.md"


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


# ── Helpers ────────────────────────────────────────────────────────────


def _resolve_symbol(dotted: str) -> object:
    """Import-resolve ``module.qualname`` and return the object (proves reachable)."""
    module_path, _, attr = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    obj = module
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def _test_file_references(rel_paths, needle: str) -> bool:
    """True when ``needle`` appears in at least one of the named test modules."""
    for rel in rel_paths:
        path = _REPO_ROOT / rel
        if path.exists() and needle in path.read_text(encoding="utf-8"):
            return True
    return False


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
        ),
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
    ]


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=100.0,
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


# ── Registry integrity ─────────────────────────────────────────────────


def test_registry_is_non_empty_and_covers_dev_promotion():
    """The dev-tier promotion path (_check_promotion) is registered."""
    assert ROUTING_SYMMETRY_REGISTRY, "routing-symmetry registry must not be empty"
    promo_names = {pair.promotion.name for pair in ROUTING_SYMMETRY_REGISTRY}
    assert MECHANISM_DEV_PROMOTION in promo_names


@pytest.mark.parametrize("pair", ROUTING_SYMMETRY_REGISTRY, ids=lambda p: p.promotion.name)
def test_every_promotion_path_is_reachable_and_tested(pair: RoutingSymmetryPair):
    """Each promotion path resolves to real code and is exercised by a named test."""
    # Reachable code symbol.
    assert callable(_resolve_symbol(pair.promotion.symbol))
    # Named, non-empty audit label.
    assert pair.promotion.audit_label
    # At least one named test module exists and references the promotion path.
    promo_token = pair.promotion_test_token or pair.promotion.symbol.rsplit(".", 1)[-1]
    assert pair.promotion_tests, f"{pair.promotion.name} must name promotion tests"
    assert _test_file_references(pair.promotion_tests, promo_token), (
        f"no named test references {promo_token}"
    )


@pytest.mark.parametrize("pair", ROUTING_SYMMETRY_REGISTRY, ids=lambda p: p.promotion.name)
def test_every_promotion_path_has_inverse_or_catalogued_followup(pair: RoutingSymmetryPair):
    """The invariant: a landed+tested demotion, OR a catalogued open follow-up.

    Neither → fail. This is the backstop that prevents a new promotion mechanism
    from landing without its symmetric counterpart being defined or catalogued.
    """
    if pair.demotion is not None:
        # Landed inverse: reachable symbol, named audit label, tested.
        assert callable(_resolve_symbol(pair.demotion.symbol))
        assert pair.demotion.audit_label
        demo_token = pair.demotion_test_token or pair.demotion.symbol.rsplit(".", 1)[-1]
        assert pair.demotion_tests, f"{pair.demotion.name} must name demotion tests"
        assert _test_file_references(pair.demotion_tests, demo_token), (
            f"no named test references {demo_token}"
        )
    else:
        # No landed inverse → must be catalogued as an open follow-up in the doc.
        assert pair.open_followup, (
            f"{pair.promotion.name} has no demotion and no catalogued follow-up"
        )
        assert _CATALOGUE_DOC.exists(), "routing-symmetry follow-up catalogue missing"
        assert pair.open_followup in _CATALOGUE_DOC.read_text(encoding="utf-8"), (
            f"open follow-up {pair.open_followup!r} not catalogued in {_CATALOGUE_DOC.name}"
        )


def test_dev_tier_pair_has_landed_tested_inverse_both_directions():
    """The dev promotion↔post-plan-checkpoint pair is the worked example (#1387).

    Both directions must be landed, reachable, and each covered by a test —
    the concrete invariant the story requires to exist after it lands.
    """
    pair = next(
        p for p in ROUTING_SYMMETRY_REGISTRY if p.promotion.name == MECHANISM_DEV_PROMOTION
    )
    assert pair.demotion is not None
    assert pair.demotion.name == MECHANISM_POST_PLAN_DEMOTION
    assert callable(_resolve_symbol(pair.promotion.symbol))
    assert callable(_resolve_symbol(pair.demotion.symbol))
    # Both directions carry a valid routing_rationale audit label.
    assert pair.promotion.audit_label == ROUTING_RATIONALE_PROMOTED
    assert pair.demotion.audit_label == ROUTING_RATIONALE_DEMOTED
    assert {pair.promotion.audit_label, pair.demotion.audit_label} <= ROUTING_RATIONALE_STATES


# ── Audit label ↔ code path (both directions exercised end-to-end) ──────


def test_routing_rationale_reports_stayed_when_no_ratchet_fires():
    decision = assign_models(_agents(), _cfg(), "medium", escalation_history=[])
    rationale = decision.routing_decision["dev"]["routing_rationale"]
    assert rationale["state"] == ROUTING_RATIONALE_STAYED
    assert rationale["mechanism"] is None
    assert rationale["from_tier"] == rationale["to_tier"]


def test_routing_rationale_reports_promoted_by_named_mechanism():
    history = [
        EscalationRecord(
            story=f"s{i}", complexity="MEDIUM", dev_model="sonnet", outcome="ESCALATE"
        )
        for i in range(2)
    ]
    decision = assign_models(_agents(), _cfg(), "medium", escalation_history=history)
    rationale = decision.routing_decision["dev"]["routing_rationale"]
    assert rationale["state"] == ROUTING_RATIONALE_PROMOTED
    assert rationale["mechanism"] == MECHANISM_DEV_PROMOTION
    # Promotion moved the tier up (mid → strong).
    assert rationale["from_tier"] == "mid"
    assert rationale["to_tier"] == "strong"
    assert decision.dev.model == "opus"


def test_routing_rationale_reports_demoted_by_post_plan_checkpoint():
    """After a clean medium plan-review, the checkpoint overwrites the rationale."""
    agents = _agents()
    # Land a real strong-tier dev via promotion (2 ESCALATE on mid → strong), so
    # the recorded baseline tier resolves to "strong" from the agent registry and
    # the one-step demotion has room to fire.
    history = [
        EscalationRecord(
            story=f"s{i}", complexity="MEDIUM", dev_model="sonnet", outcome="ESCALATE"
        )
        for i in range(2)
    ]
    decision = assign_models(agents, _cfg(), "medium", escalation_history=history)
    assert decision.dev.model == "opus"  # promoted to strong
    assert decision.routing_decision["dev"]["routing_rationale"]["state"] == (
        ROUTING_RATIONALE_PROMOTED
    )
    out = apply_post_plan_checkpoint(
        decision,
        agents,
        _cfg(),
        "MEDIUM",
        plan_review_decision="APPROVE",
        plan_review_cycles=1,
        p1_count=0,
        p2_count=0,
    )
    rationale = out.routing_decision["dev"]["routing_rationale"]
    assert rationale["state"] == ROUTING_RATIONALE_DEMOTED
    assert rationale["mechanism"] == MECHANISM_POST_PLAN_DEMOTION
    assert rationale["from_tier"] == "strong"
    assert rationale["to_tier"] == "mid"
