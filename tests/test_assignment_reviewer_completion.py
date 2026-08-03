"""Reviewer attempt-completion reranking in _select_reviewers (#1388).

Mirrors the dev-side _rerank_by_profiles behavior for reviewers: below min_runs
the completion signal is ignored (cold start preserves existing order); at/above
min_runs a below-threshold reviewer is sorted *after* higher-completion
candidates — a sort-after, never a filter-out.

Both directions are covered here (#1880): the deprioritization above and its
registered inverse, the K-consecutive-clean-attempts re-inclusion rule that
restores a recovered reviewer to normal ranking while stale failures still hold
its recency-weighted rate under threshold.
"""

from __future__ import annotations

import pytest

from theforge.assignment import (
    AssignmentConfig,
    _select_reviewers,
    assign_models,
)
from theforge.config import AgentDef


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")


def _two_reviewers() -> list[AgentDef]:
    # Same tier, same budget → without profiles the stable order is input order
    # (rev-a first). Distinct providers so cross-provider selection is a no-op
    # differentiator; we drive ordering purely off completion.
    return [
        AgentDef("rev-a", "anthropic", "sonnet", 5.0, 600, "strong"),
        AgentDef("rev-b", "openai", "gpt", 5.0, 600, "strong"),
    ]


def _review_entry(provider: str, model: str, attempted: int, completed: int) -> dict:
    # Stamp identity the way the real fold path (_ensure_model) does, so the
    # router's identity-based profile lookup resolves the canonical entry.
    return {
        "_identity": {"provider": provider, "model": model, "transport": "api"},
        "review": {
            "_attempted_count": attempted,
            "_completed_count": completed,
            "completion_rate": round(completed / attempted, 4) if attempted else 0.0,
            # All-1 or all-0 ring keeps the recency-weighted rate unambiguous
            # regardless of decay parameters.
            "_completion_recent": [1] * completed + [0] * (attempted - completed),
        },
    }


def _profiles(**by_key: dict) -> dict:
    return {"models": dict(by_key)}


def test_cold_start_below_min_runs_preserves_existing_order():
    # rev-a has a perfect record but only 2 attempts (< min_runs=5): cold start,
    # so completion is ignored and the existing budget/order pick (rev-a) stands.
    # rev-a is 0% but with only 2 attempts (below the floor) it is not deprioritized.
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _review_entry("anthropic", "sonnet", 2, 0),
            "openai/gpt/api": _review_entry("openai", "gpt", 2, 2),
        }
    )
    selected = _select_reviewers(
        _two_reviewers(),
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=5,
    )
    assert [a.name for a in selected] == ["rev-a"]


def test_full_vs_zero_completion_sorts_full_first():
    # rev-a is 0/6 (below 0.5 threshold, floor met); rev-b is 6/6. The high-
    # completion reviewer must sort first even though rev-a led the pool.
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _review_entry("anthropic", "sonnet", 6, 0),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    selected = _select_reviewers(
        _two_reviewers(),
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=5,
    )
    assert [a.name for a in selected] == ["rev-b"]


def test_tie_break_falls_back_to_existing_order():
    # Both reviewers pass the threshold at the same completion rate → neither is
    # deprioritized → existing (input/budget) order is preserved: rev-a first.
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _review_entry("anthropic", "sonnet", 6, 6),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    selected = _select_reviewers(
        _two_reviewers(),
        tier="strong",
        n=2,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=5,
    )
    assert [a.name for a in selected] == ["rev-a", "rev-b"]


def test_sort_after_not_filter_out_low_completion_still_selected():
    # A single low-completion reviewer is still selected when no better candidate
    # exists — deprioritization is a sort-after, not a filter-out.
    profiles = _profiles(**{"anthropic/sonnet/api": _review_entry("anthropic", "sonnet", 6, 0)})
    selected = _select_reviewers(
        [AgentDef("rev-a", "anthropic", "sonnet", 5.0, 600, "strong")],
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=5,
    )
    assert [a.name for a in selected] == ["rev-a"]


def test_completion_audit_records_rerank_effect():
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _review_entry("anthropic", "sonnet", 6, 0),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    audit: dict = {}
    signals: dict = {}
    _select_reviewers(
        _two_reviewers(),
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=5,
        completion_signals_out=signals,
        completion_audit=audit,
    )
    assert audit["applied"] is True
    assert audit["deprioritized"] == ["rev-a"]
    assert audit["original_order"] == ["rev-a", "rev-b"]
    assert audit["final_order"] == ["rev-b", "rev-a"]
    assert signals["rev-a"]["rate"] == 0.0
    assert signals["rev-b"]["rate"] == 1.0


def _assign_cfg(**overrides) -> AssignmentConfig:
    base = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        max_cost_per_story_usd=100.0,
        escalation_memory=False,
    )
    base.update(overrides)
    return AssignmentConfig(**base)


def test_assign_models_deprioritizes_low_completion_reviewer_end_to_end():
    # Full assign_models pass: the dev model is a separate cheap agent so both
    # reviewers survive anti-self-review; completion reorders the code reviewers.
    agents = [
        AgentDef("dev-cheap", "deepseek", "ds", 1.0, 300, "cheap"),
        AgentDef("rev-a", "anthropic", "sonnet", 5.0, 600, "strong"),
        AgentDef("rev-b", "openai", "gpt", 5.0, 600, "strong"),
    ]
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _review_entry("anthropic", "sonnet", 6, 0),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    # LOW keeps dev on the cheap agent so both strong reviewers stay in the pool
    # (a HIGH story would promote a strong reviewer into the dev slot).
    decision = assign_models(
        agents,
        _assign_cfg(),
        complexity="LOW",
        model_profiles=profiles,
    )
    assert [p.name for p in decision.code_reviewers] == ["rev-b"]
    completion = decision.routing_decision["code_review"]["completion_check"]
    assert completion["fired"] is True
    assert "rev-a" in completion["deprioritized"]


# ── Re-inclusion: the registered inverse (#1880) ───────────────────────────


def _recovered_entry(provider: str, model: str, stale_failures: int, clean_tail: int) -> dict:
    """A reviewer whose lifetime record is poor but whose newest attempts are clean.

    The recency half-life is ~50 runs, so a short clean tail on a long failure
    history does NOT lift the weighted rate over the threshold — passive decay
    alone would leave this reviewer deprioritized. That is exactly the case the
    K-consecutive-clean-attempts re-inclusion rule exists to handle.
    """
    attempted = stale_failures + clean_tail
    return {
        "_identity": {"provider": provider, "model": model, "transport": "api"},
        "review": {
            "_attempted_count": attempted,
            "_completed_count": clean_tail,
            "completion_rate": round(clean_tail / attempted, 4),
            "_completion_recent": [0] * stale_failures + [1] * clean_tail,
        },
    }


def test_recovered_reviewer_still_scores_below_threshold():
    # Guards the premise of the re-inclusion tests: if passive recency decay had
    # already lifted this reviewer over the threshold there would be nothing for
    # the recovery rule to do, and the tests below would pass vacuously.
    from theforge.model_profiles import get_review_signal

    profiles = _profiles(
        **{"anthropic/sonnet/api": _recovered_entry("anthropic", "sonnet", 20, 5)}
    )
    signal = get_review_signal(profiles, "rev-a", 5, actual_model="sonnet", provider="anthropic")
    assert signal["floor"] == "pass"
    assert signal["rate"] < 0.5
    assert signal["recovery"]["clean_streak"] == 5
    assert signal["recovery"]["clean_attempts_required"] == 5
    assert signal["recovery"]["recovered"] is True


def test_reviewer_reincluded_after_k_clean_attempts():
    # Same reviewer shape as test_full_vs_zero_completion_sorts_full_first, but
    # rev-a's newest 5 attempts are clean → restored to its normal (input) rank
    # ahead of rev-b instead of being sorted after it.
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _recovered_entry("anthropic", "sonnet", 20, 5),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    audit: dict = {}
    selected = _select_reviewers(
        _two_reviewers(),
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=5,
        completion_audit=audit,
    )
    assert [a.name for a in selected] == ["rev-a"]
    assert audit["applied"] is False
    assert audit["deprioritized"] == []
    assert audit["reincluded"] == ["rev-a"]
    assert audit["reinclusion_clean_attempts_required"] == 5
    assert audit["reinclusion_checked"]["rev-a"]["below_threshold"] is True
    assert audit["reinclusion_checked"]["rev-a"]["clean_streak"] == 5


def test_partial_clean_streak_does_not_reinclude():
    # 4 clean attempts is one short of K=5 → still deprioritized. The streak is
    # strictly consecutive: an older clean run interrupted by a failure does not
    # count toward it.
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _recovered_entry("anthropic", "sonnet", 20, 4),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    audit: dict = {}
    selected = _select_reviewers(
        _two_reviewers(),
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=5,
        completion_audit=audit,
    )
    assert [a.name for a in selected] == ["rev-b"]
    assert audit["deprioritized"] == ["rev-a"]
    assert audit["reincluded"] == []
    assert audit["reinclusion_checked"]["rev-a"]["clean_streak"] == 4


def test_assign_models_reincludes_recovered_reviewer_end_to_end():
    agents = [
        AgentDef("dev-cheap", "deepseek", "ds", 1.0, 300, "cheap"),
        AgentDef("rev-a", "anthropic", "sonnet", 5.0, 600, "strong"),
        AgentDef("rev-b", "openai", "gpt", 5.0, 600, "strong"),
    ]
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _recovered_entry("anthropic", "sonnet", 20, 5),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    decision = assign_models(agents, _assign_cfg(), complexity="LOW", model_profiles=profiles)
    assert [p.name for p in decision.code_reviewers] == ["rev-a"]
    completion = decision.routing_decision["code_review"]["completion_check"]
    assert completion["deprioritized"] == []
    reinclusion = completion["reinclusion_check"]
    assert reinclusion["mechanism"] == "reviewer_completion_reinclusion"
    assert reinclusion["fired"] is True
    assert reinclusion["clean_attempts_required"] == 5
    assert reinclusion["reincluded"] == ["rev-a"]
    assert "rev-a" in reinclusion["checked"]
    assert reinclusion["reason"].startswith("clean_attempt_streak_met")
    # The consulted per-candidate signal carries the recovery evidence too.
    assert completion["signals"]["rev-a"]["recovery"]["clean_streak"] == 5


def test_assign_models_records_reinclusion_checked_but_not_fired():
    # Still-low reviewer with no clean streak: the re-inclusion path must be
    # recorded as checked-and-didn't-fire, never omitted (ADR-0006 clause 7).
    agents = [
        AgentDef("dev-cheap", "deepseek", "ds", 1.0, 300, "cheap"),
        AgentDef("rev-a", "anthropic", "sonnet", 5.0, 600, "strong"),
        AgentDef("rev-b", "openai", "gpt", 5.0, 600, "strong"),
    ]
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _review_entry("anthropic", "sonnet", 6, 0),
            "openai/gpt/api": _review_entry("openai", "gpt", 6, 6),
        }
    )
    decision = assign_models(agents, _assign_cfg(), complexity="LOW", model_profiles=profiles)
    completion = decision.routing_decision["code_review"]["completion_check"]
    assert completion["deprioritized"] == ["rev-a"]
    reinclusion = completion["reinclusion_check"]
    assert reinclusion["fired"] is False
    assert reinclusion["reincluded"] == []
    assert "rev-a" in reinclusion["checked"]
    assert reinclusion["checked_detail"]["rev-a"]["clean_streak"] == 0
    assert reinclusion["reason"] == "no_below_threshold_reviewer_met_clean_attempt_streak"
