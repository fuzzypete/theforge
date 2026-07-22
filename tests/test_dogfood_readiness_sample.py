"""Guard the committed v0.13 dogfood-readiness sample routing_decision record.

The readiness checklist (`docs/v0.13-dogfood-readiness.md`, #1869) cites a
committed sample per-run audit record —
``docs/assets/sample-routing-decision-run.json`` — as the evidence for the
"sample recent run has a usable routing_decision explanation" acceptance
criterion. That record is not a hand-authored fixture: it is emitted by the
production router (``theforge.assignment.assign_models``) with adaptive routing
enabled. This test re-runs the same router with the same inputs and asserts the
committed record still matches on the reconstruction-critical fields, and that
the shipping ``forge explain`` renderer reproduces the selected/avoided/evidence
lines the doc quotes. If the router's block shape drifts, this fails so the
committed evidence and the doc are regenerated rather than silently going stale.

Provider auth is env-based (no provider SDK import is required), so the test
controls the environment exactly as ``test_assignment_routing_decision`` does:
Anthropic/OpenAI authed, DeepSeek/Google absent → a stable ``auth_missing``
exclusion regardless of the CI environment's real keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theforge.assignment import AssignmentConfig, assign_models
from theforge.cli import explain
from theforge.config import AgentDef

_SAMPLE_PATH = Path(__file__).resolve().parents[1] / "docs" / "assets" / "sample-routing-decision-run.json"


@pytest.fixture()
def _controlled_keys(monkeypatch):
    """Anthropic/OpenAI authed; DeepSeek/Google absent → auth_missing for deepseek."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _agents() -> list[AgentDef]:
    return [
        AgentDef(name="sonnet", provider="anthropic", model="sonnet", budget_usd=3.0, timeout_seconds=600, tier="mid"),
        AgentDef(name="opus", provider="anthropic", model="opus", budget_usd=5.0, timeout_seconds=900, tier="strong"),
        AgentDef(name="gpt", provider="openai", model="gpt-5.4", budget_usd=8.0, timeout_seconds=900, tier="strong"),
        AgentDef(name="deepseek", provider="deepseek", model="deepseek-reasoner", budget_usd=1.0, timeout_seconds=600, tier="strong"),
    ]


def _profiles() -> dict:
    """opus dev: stale lifetime rate (0.30) rescued by strong recent runs.

    Demonstrates recency weighting (ADR-0006 clause 2.4): the routing-admissible
    weighted rate diverges from the poisoned lifetime cumulative average.
    """
    recent = [1, 1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1]
    return {
        "models": {
            "opus": {
                "dev": {
                    "runs": 60,
                    "success_rate": 0.30,
                    "_recent": recent,
                    "by_complexity": {"large": {"runs": 60, "success_rate": 0.30, "_recent": recent}},
                }
            },
        }
    }


def _live_block() -> dict:
    decision = assign_models(
        _agents(),
        AssignmentConfig(
            enabled=True,
            min_reviewers=1,
            max_reviewers=2,
            prefer_cross_provider=True,
            max_cost_per_story_usd=100.0,
            escalation_memory=True,
        ),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(),
    )
    return decision.routing_decision


def _committed_record() -> dict:
    return json.loads(_SAMPLE_PATH.read_text(encoding="utf-8"))


def test_sample_record_exists_and_is_a_routing_decision(_controlled_keys):
    assert _SAMPLE_PATH.exists(), f"missing committed sample record: {_SAMPLE_PATH}"
    record = _committed_record()
    assert isinstance(record.get("routing_decision"), dict), "sample must carry a routing_decision block"


def test_committed_sample_matches_live_router_on_reconstruction_fields(_controlled_keys):
    """The committed sample is emitted by the real router, not hand-authored.

    Asserting equality on the reconstruction-critical fields ties the committed
    evidence to the production code path and fails if the block shape drifts.
    """
    committed = _committed_record()["routing_decision"]
    live = _live_block()

    # DEV: opus selected on strong tier, with the recency-rescued signal.
    committed_dev = {e["name"]: e for e in committed["dev"]["candidate_pool"]}
    live_dev = {e["name"]: e for e in live["dev"]["candidate_pool"]}
    assert committed["dev"]["final"]["model"] == live["dev"]["final"]["model"] == "opus"

    c_sig = committed_dev["opus"]["signals"]["success_rate"]
    l_sig = live_dev["opus"]["signals"]["success_rate"]
    for key in ("raw", "weighted", "runs", "floor"):
        assert c_sig[key] == l_sig[key], f"opus signal '{key}' drifted from live router"
    # The adaptive decision is genuine: stale lifetime rate, recency-rescued.
    assert c_sig["raw"] == 0.30
    assert c_sig["weighted"] > c_sig["raw"]  # recency weighting moved the needle
    assert c_sig["floor"] == "pass"

    # Avoided candidate: deepseek excluded for auth_missing (a canonical reason).
    assert committed_dev["deepseek"]["included"] is False
    assert committed_dev["deepseek"]["reason"] == live_dev["deepseek"]["reason"] == "auth_missing"


def test_explain_renders_selected_avoided_and_evidence(_controlled_keys):
    """The shipping renderer reproduces the lines the readiness doc quotes."""
    block = _committed_record()["routing_decision"]
    text = "\n".join(explain.render_routing_decision(block))
    # Selected model + tier.
    assert "selected: opus [strong]" in text
    # Evidence fields needed to reconstruct the choice (raw + weighted + floor).
    assert "raw=0.3000 weighted=0.9138 runs=60 (sample-floor pass)" in text
    # Avoided candidate with a human-readable canonical reason.
    assert "deepseek [strong] — excluded — provider credentials missing" in text
