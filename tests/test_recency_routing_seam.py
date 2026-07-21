"""Seam-level coverage for recency-weighted routing (#1392).

Exercises ``assign_models`` end-to-end: when one physical model has old-noisy
runs plus recent-clean runs and another has only old-noisy runs at the *same*
raw success rate, the router prefers the recently-clean model — even when it is
the more expensive candidate — and the routing_decision block records the raw
rate, weighted rate, admissible sample count, sample-floor result, and
taint-excluded count so the divergence is operator-visible.
"""

from __future__ import annotations

import pytest

from theforge.assignment import assign_models
from theforge.config import AgentDef, AssignmentConfig
from theforge.config.types import RecencyConfig
from theforge.model_profiles import RunOutcome, apply_run


@pytest.fixture()
def _anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def _agents() -> list[AgentDef]:
    # Both strong-tier so they compete for the HIGH/score-9 dev pick. The
    # recent-clean model is listed first AND priced higher, so neither ordering
    # nor price can explain a win for it — only the weighted rate can.
    return [
        AgentDef(
            name="recent_clean",
            provider="anthropic",
            model="opus",
            budget_usd=9.0,
            timeout_seconds=900,
            tier="strong",
            input_cost_per_mtok=15.0,
            output_cost_per_mtok=75.0,
        ),
        AgentDef(
            name="only_old_noisy",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="strong",
            input_cost_per_mtok=1.0,
            output_cost_per_mtok=5.0,
        ),
    ]


def _cfg(recency: RecencyConfig | None = None) -> AssignmentConfig:
    kwargs = dict(enabled=True, max_cost_per_story_usd=100.0)
    if recency is not None:
        kwargs["recency"] = recency
    return AssignmentConfig(**kwargs)


def _profiles() -> dict:
    data: dict = {"models": {}}
    # recent_clean: 10 old failures, then 10 recent successes (raw 0.5).
    for _ in range(10):
        apply_run(data, RunOutcome("large", "recent_clean", False, 1, 0.0))
    for _ in range(10):
        apply_run(data, RunOutcome("large", "recent_clean", True, 1, 0.0))
    # only_old_noisy: 10 successes then 10 failures — same raw 0.5, but its
    # recent runs are the failures, so its weighted rate is lower.
    for _ in range(10):
        apply_run(data, RunOutcome("large", "only_old_noisy", True, 1, 0.0))
    for _ in range(10):
        apply_run(data, RunOutcome("large", "only_old_noisy", False, 1, 0.0))
    return data


def test_routing_prefers_recently_clean_model_at_same_raw_rate(_anthropic_key):
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(),
    )
    # Weighted recency drives the selection over both list order and price.
    assert decision.dev.name == "recent_clean"


def test_off_mode_falls_back_to_price_at_tied_raw_rate(_anthropic_key):
    """Sanity check that recency is what moved the selection: with weighting off,
    the two models tie on raw rate and the cheaper one wins on price."""
    decision = assign_models(
        _agents(),
        _cfg(RecencyConfig(mode="off")),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(),
    )
    assert decision.dev.name == "only_old_noisy"


def test_routing_decision_records_raw_weighted_floor_and_taint(_anthropic_key):
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(),
    )
    pool = {e["name"]: e for e in decision.routing_decision["dev"]["candidate_pool"]}
    sig = pool["recent_clean"]["signals"]["success_rate"]
    # Raw cumulative vs weighted rate diverge and both are recorded (clause 7).
    assert sig["raw"] == 0.5
    assert sig["weighted"] > sig["raw"]
    # Admissible sample count, sample-floor result, weighting params, and the
    # taint-excluded count are all present for operator inspection.
    assert sig["runs"] == 20
    assert sig["floor"] == "pass"
    assert sig["tainted_runs"] == 0
    assert sig["weighting"]["mode"] == "exponential"
