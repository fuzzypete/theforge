"""Plan-reviewer value reranking in _select_reviewers + routing_decision (#1443).

The value analog of the #1388 completion rerank: below the sample floor the
uniqueness signal is ignored (cold start preserves order); at/above the floor a
below-threshold (redundant) reviewer is sorted *after* higher-value candidates —
a sort-after, never a filter-out — and only when the operator opted in via
``reviewer_value_enabled``. The routing_decision records the consulted signals
and ranking effect.
"""

from __future__ import annotations

import pytest

from theforge.assignment import (
    _rerank_reviewers_by_value,
    _reviewer_value_check,
    _select_reviewers,
)
from theforge.config import AgentDef
from theforge.model_profiles import RunOutcome, apply_run
from theforge.reviewer_value import PlanReviewerValueSample


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")


def _agent(name: str, provider: str, model: str) -> AgentDef:
    return AgentDef(name, provider, model, 5.0, 600, "strong")


def _profiles_with_value(specs: list[tuple[str, str, str, int, int, float]], n: int) -> dict:
    """Fold ``n`` samples per (name, provider, model, unique, total, latency) spec."""
    data: dict = {"models": {}}
    for _ in range(n):
        data = apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model="d",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=0.0,
                plan_reviewer_values=[
                    PlanReviewerValueSample(name, "medium", uniq, total, latency, model, provider)
                    for (name, provider, model, uniq, total, latency) in specs
                ],
            ),
        )
    return data


# ── Core rerank behavior ────────────────────────────────────────────────────


def test_below_floor_preserves_order():
    # revLow is redundant but only 3 samples (< min_runs=5): cold start, no rerank.
    profiles = _profiles_with_value(
        [("revLow", "openai", "gpt", 0, 3, 600.0), ("revHigh", "anthropic", "sonnet", 2, 2, 60.0)],
        n=3,
    )
    cands = [_agent("revLow", "openai", "gpt"), _agent("revHigh", "anthropic", "sonnet")]
    audit: dict = {}
    out = _rerank_reviewers_by_value(
        cands, profiles, uniqueness_threshold=0.34, min_runs=5, complexity="medium", audit=audit
    )
    assert [a.name for a in out] == ["revLow", "revHigh"]
    assert audit["applied"] is False


def test_above_floor_deprioritizes_redundant_reviewer():
    profiles = _profiles_with_value(
        [("revLow", "openai", "gpt", 0, 3, 600.0), ("revHigh", "anthropic", "sonnet", 2, 2, 60.0)],
        n=6,
    )
    cands = [_agent("revLow", "openai", "gpt"), _agent("revHigh", "anthropic", "sonnet")]
    signals: dict = {}
    audit: dict = {}
    out = _rerank_reviewers_by_value(
        cands,
        profiles,
        uniqueness_threshold=0.34,
        min_runs=5,
        complexity="medium",
        signals_out=signals,
        audit=audit,
    )
    assert [a.name for a in out] == ["revHigh", "revLow"]
    assert audit["applied"] is True
    assert audit["deprioritized"] == ["revLow"]
    # Consulted signals are exposed for the routing_decision.
    assert signals["revLow"]["uniqueness_rate"]["rate"] == 0.0
    assert signals["revLow"]["latency_per_p1"]["rate"] == 200.0  # 600 / 3


def test_sort_after_not_filter_out():
    # A single redundant reviewer is still returned — never removed from the pool.
    profiles = _profiles_with_value([("revLow", "openai", "gpt", 0, 3, 600.0)], n=6)
    cands = [_agent("revLow", "openai", "gpt")]
    out = _rerank_reviewers_by_value(
        cands, profiles, uniqueness_threshold=0.34, min_runs=5, complexity="medium"
    )
    assert [a.name for a in out] == ["revLow"]


# ── Seam: ordering flips only after enough admissible history ────────────────


def test_ordering_changes_only_after_enough_history():
    cands = [_agent("revLow", "openai", "gpt"), _agent("revHigh", "anthropic", "sonnet")]
    specs = [
        ("revLow", "openai", "gpt", 0, 3, 600.0),
        ("revHigh", "anthropic", "sonnet", 2, 2, 60.0),
    ]
    orders: list[list[str]] = []
    for n in range(0, 7):
        profiles = _profiles_with_value(specs, n=n) if n else {"models": {}}
        out = _rerank_reviewers_by_value(
            cands, profiles, uniqueness_threshold=0.34, min_runs=5, complexity="medium"
        )
        orders.append([a.name for a in out])
    # With < 5 admissible samples the incoming order is preserved; only once the
    # sample floor is crossed does the value signal reorder the pool.
    assert orders[0] == ["revLow", "revHigh"]  # no history
    assert orders[4] == ["revLow", "revHigh"]  # 4 samples < floor
    assert orders[5] == ["revHigh", "revLow"]  # 5 samples == floor → reorders
    assert orders[6] == ["revHigh", "revLow"]


# ── Gating via _select_reviewers ────────────────────────────────────────────


def _selectable_agents() -> list[AgentDef]:
    return [_agent("revHigh", "anthropic", "sonnet"), _agent("revLow", "openai", "gpt")]


def test_select_reviewers_disabled_by_default_ignores_value():
    profiles = _profiles_with_value(
        [("revHigh", "anthropic", "sonnet", 2, 2, 60.0), ("revLow", "openai", "gpt", 0, 3, 600.0)],
        n=6,
    )
    audit: dict = {}
    _select_reviewers(
        _selectable_agents(),
        "strong",
        2,
        prefer_cross_provider=False,
        model_profiles=profiles,
        value_enabled=False,
        value_complexity="medium",
        value_audit=audit,
    )
    # Disabled → the value mechanism never ran, so nothing was recorded.
    assert audit == {}


def test_select_reviewers_enabled_records_and_reorders():
    # Order candidates so the redundant reviewer would otherwise come first.
    agents = [_agent("revLow", "openai", "gpt"), _agent("revHigh", "anthropic", "sonnet")]
    profiles = _profiles_with_value(
        [("revHigh", "anthropic", "sonnet", 2, 2, 60.0), ("revLow", "openai", "gpt", 0, 3, 600.0)],
        n=6,
    )
    audit: dict = {}
    selected = _select_reviewers(
        agents,
        "strong",
        2,
        prefer_cross_provider=False,
        model_profiles=profiles,
        value_enabled=True,
        value_uniqueness_threshold=0.34,
        value_min_runs=5,
        value_complexity="medium",
        value_audit=audit,
    )
    assert audit.get("applied") is True
    assert [a.name for a in selected][0] == "revHigh"


# ── routing_decision explainability block ───────────────────────────────────


def test_reviewer_value_check_block_shape_when_fired():
    signals = {
        "revLow": {
            "uniqueness_rate": {"raw": 0.0, "weighted": 0.0, "rate": 0.0, "floor": "pass"},
            "latency_per_p1": {"raw": 200.0, "weighted": 200.0, "rate": 200.0, "floor": "pass"},
            "runs": 6,
            "tainted_runs": 0,
        }
    }
    audit = {
        "applied": True,
        "uniqueness_threshold": 0.34,
        "min_runs": 5,
        "complexity": "medium",
        "deprioritized": ["revLow"],
        "original_order": ["revLow", "revHigh"],
        "final_order": ["revHigh", "revLow"],
    }
    block = _reviewer_value_check(signals, audit, {"revHigh"})
    assert block["mechanism"] == "reviewer_value"
    assert block["fired"] is True
    assert block["uniqueness_threshold"] == 0.34
    assert block["deprioritized"] == ["revLow"]
    assert block["final_order"] == ["revHigh", "revLow"]
    consulted = block["signals"]["revLow"]
    assert consulted["uniqueness_rate"]["rate"] == 0.0
    assert consulted["latency_per_p1"]["rate"] == 200.0
    assert consulted["selected"] is False


def test_reviewer_value_check_empty_when_not_consulted():
    assert _reviewer_value_check(None, None, set()) == {}
