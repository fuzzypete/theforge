"""Post-plan dev-tier checkpoint (#1387, absorbs #1109).

Gate-condition and seam coverage for :func:`apply_post_plan_checkpoint`, the
single post-plan dev-tier demotion mechanism (ADR-0006 clause 5 recovery side).
Each gate condition independently preserves the preflight-assigned dev tier; a
clean plan-review on a medium story steps that tier down by exactly one level.
"""

from __future__ import annotations

import pytest

from theforge.assignment import (
    POST_PLAN_CHECKPOINT_RATIONALES,
    AssignmentConfig,
    AssignmentDecision,
    _agent_to_profile,
    apply_post_plan_checkpoint,
)
from theforge.config import AgentDef


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(enabled=True, max_cost_per_story_usd=100.0)
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="cheap",
        ),
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
    ]


def _decision(agents: list[AgentDef], dev_name: str, baseline_tier: str) -> AssignmentDecision:
    """Build a minimal decision whose dev role sits at ``baseline_tier``."""
    dev_agent = next(a for a in agents if a.name == dev_name)
    dev_profile = _agent_to_profile(dev_agent, role="dev")
    return AssignmentDecision(
        preflight=dev_profile,
        planner=dev_profile,
        plan_reviewers=[dev_profile],
        dev=dev_profile,
        code_reviewers=[dev_profile],
        routing_decision={
            "dev": {
                "final": {
                    "model": dev_agent.model,
                    "tier": baseline_tier,
                    "rationale": "[preflight] dev",
                },
                "post_plan_checkpoint": {
                    "fired": False,
                    "decision": "pending",
                    "reason": "checkpoint_runs_after_plan_review",
                },
            }
        },
    )


def _clean(**overrides):
    """Kwargs for a clean medium APPROVE in 1 cycle, P1=0, P2<=1."""
    base = dict(
        complexity="MEDIUM",
        plan_review_decision="APPROVE",
        plan_review_cycles=1,
        p1_count=0,
        p2_count=0,
    )
    base.update(overrides)
    return base


def _block(decision: AssignmentDecision) -> dict:
    return decision.routing_decision["dev"]["post_plan_checkpoint"]


# ── Fire path: one-step demotion ───────────────────────────────────────


def test_strong_to_mid_on_clean_medium():
    agents = _agents()
    decision = _decision(agents, "opus", "strong")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), **_clean())
    block = _block(out)
    assert block["fired"] is True
    assert block["decision"] == "downgrade"
    assert block["baseline_tier"] == "strong"
    assert block["final_tier"] == "mid"
    assert block["plan_present"] is True
    assert block["rationale"] == "plan_review_clean_medium"
    assert out.dev.model == "sonnet"
    # dev.final is updated so the recorded final reflects the running model.
    assert out.routing_decision["dev"]["final"]["tier"] == "mid"
    assert out.routing_decision["dev"]["final"]["model"] == "sonnet"


def test_mid_to_cheap_on_clean_medium():
    agents = _agents()
    decision = _decision(agents, "sonnet", "mid")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), **_clean())
    block = _block(out)
    assert block["fired"] is True
    assert block["final_tier"] == "cheap"
    assert out.dev.model == "haiku"


def test_never_two_steps():
    """strong baseline reduces to mid, never straight to cheap."""
    agents = _agents()
    decision = _decision(agents, "opus", "strong")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), **_clean())
    assert _block(out)["final_tier"] == "mid"
    assert out.dev.model == "sonnet"


def test_p2_of_one_still_fires():
    agents = _agents()
    decision = _decision(agents, "opus", "strong")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), **_clean(p2_count=1))
    assert _block(out)["fired"] is True


# ── Bypass paths (skipped) ─────────────────────────────────────────────


def test_disabled_config_skips():
    agents = _agents()
    decision = _decision(agents, "opus", "strong")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(plan_tier_reduction=False), **_clean())
    block = _block(out)
    assert block["fired"] is False
    assert block["decision"] == "skipped"
    assert block["rationale"] == "plan_tier_reduction_disabled"
    assert out.dev.model == "opus"


def test_explicit_dev_override_skips():
    agents = _agents()
    decision = _decision(agents, "opus", "strong")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), explicit_roles={"dev"}, **_clean())
    block = _block(out)
    assert block["fired"] is False
    assert block["decision"] == "skipped"
    assert block["rationale"] == "explicit_dev_override"
    assert out.dev.model == "opus"


# ── Gate failures (preserve) — each independent ────────────────────────


@pytest.mark.parametrize(
    "overrides,rationale",
    [
        (dict(complexity="small"), "complexity_not_medium"),
        (dict(complexity="large"), "complexity_not_medium"),
        (dict(plan_review_decision="REJECT"), "plan_review_not_approve"),
        (dict(plan_review_cycles=2), "plan_review_cycles_exceeded"),
        (dict(p1_count=1), "plan_review_p1_present"),
        (dict(p2_count=2), "plan_review_p2_exceeded"),
    ],
)
def test_each_gate_failure_preserves(overrides, rationale):
    agents = _agents()
    decision = _decision(agents, "opus", "strong")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), **_clean(**overrides))
    block = _block(out)
    assert block["fired"] is False
    assert block["decision"] == "preserve"
    assert block["baseline_tier"] == "strong"
    assert block["final_tier"] == "strong"
    assert block["rationale"] == rationale
    assert out.dev.model == "opus"


def test_no_reduced_tier_candidate_at_floor():
    """A cheap-tier baseline cannot step down further — preserve."""
    agents = _agents()
    decision = _decision(agents, "haiku", "cheap")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), **_clean())
    block = _block(out)
    assert block["fired"] is False
    assert block["decision"] == "preserve"
    assert block["rationale"] == "no_reduced_tier_candidate"
    assert out.dev.model == "haiku"


def test_no_authed_candidate_at_target_preserves(monkeypatch):
    """When the only target-tier model lacks auth, the tier is preserved."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # opus (strong) baseline, but the sole mid model (sonnet) is anthropic with
    # no key → no authed target candidate.
    agents = [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="mid",
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
    decision = _decision(agents, "gpt", "strong")
    out = apply_post_plan_checkpoint(decision, agents, _cfg(), **_clean())
    block = _block(out)
    assert block["fired"] is False
    assert block["rationale"] == "no_reduced_tier_candidate"


# ── Rationale vocabulary is closed ─────────────────────────────────────


def test_all_emitted_rationales_are_enumerable():
    agents = _agents()
    seen = set()
    scenarios = [
        _clean(),
        _clean(complexity="small"),
        _clean(plan_review_decision="REJECT"),
        _clean(plan_review_cycles=3),
        _clean(p1_count=2),
        _clean(p2_count=5),
    ]
    for kwargs in scenarios:
        out = apply_post_plan_checkpoint(
            _decision(agents, "opus", "strong"), agents, _cfg(), **kwargs
        )
        seen.add(_block(out)["rationale"])
    # Plus the bypass + floor rationales.
    seen.add(
        _block(
            apply_post_plan_checkpoint(
                _decision(agents, "opus", "strong"),
                agents,
                _cfg(plan_tier_reduction=False),
                **_clean(),
            )
        )["rationale"]
    )
    seen.add(
        _block(
            apply_post_plan_checkpoint(
                _decision(agents, "haiku", "cheap"), agents, _cfg(), **_clean()
            )
        )["rationale"]
    )
    assert seen <= POST_PLAN_CHECKPOINT_RATIONALES
