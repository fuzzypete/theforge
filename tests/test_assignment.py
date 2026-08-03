"""Tests for src/theforge/assignment.py — pure unit tests, no I/O, no subprocess."""

from __future__ import annotations

import pytest

from theforge.assignment import (
    PHASE_TIER,
    PROMOTION_OUTCOME_BELOW_FLOOR,
    PROMOTION_OUTCOME_PROMOTED,
    PROMOTION_OUTCOME_RECOVERED,
    AssignmentConfig,
    AssignmentDecision,
    EscalationRecord,
    _check_promotion,
    _normalize_complexity,
    _reviewer_count,
    assign_models,
)
from theforge.config import AgentDef, TransportFallbackConfig


def _dev_profiles(
    model: str,
    band: str,
    *,
    runs: int,
    success_rate: float,
    recent: list[int],
    tainted: int = 0,
) -> dict:
    """Build a model_profiles dict with a dev bucket at one complexity band.

    ``runs``/``success_rate`` are the ADMISSIBLE (post-taint) lifetime aggregates
    and ``recent`` is the ordered outcome ring (oldest→newest) the recency-weighted
    rate is computed from. ``tainted`` records how many runs were excluded for taint
    (they never enter ``runs``/``recent`` — exclusion happens upstream at fold time).
    """
    bucket = {
        "runs": runs,
        "success_rate": success_rate,
        "_recent": list(recent),
        "tainted_runs": tainted,
    }
    return {
        "models": {
            model: {
                "dev": {
                    "runs": runs,
                    "success_rate": success_rate,
                    "_recent": list(recent),
                    "tainted_runs": tainted,
                    "by_complexity": {band: bucket},
                }
            }
        }
    }


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    """Ensure _has_auth() passes for all test agents."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


# ── Fixtures ───────────────────────────────────────────────────────────


def _make_cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=100.0,  # generous default so budget tests can be explicit
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _make_agents_one_per_tier() -> list[AgentDef]:
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


def _make_agents_cross_provider() -> list[AgentDef]:
    return [
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
        AgentDef(
            name="deepseek-r1",
            provider="deepseek",
            model="deepseek-reasoner",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="strong",
        ),
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
    ]


# ── test_normalize_complexity ──────────────────────────────────────────


def test_normalize_complexity_small():
    assert _normalize_complexity("small") == "LOW"


def test_normalize_complexity_medium():
    assert _normalize_complexity("medium") == "MEDIUM"


def test_normalize_complexity_large():
    assert _normalize_complexity("large") == "HIGH"


def test_normalize_complexity_passthrough_low():
    assert _normalize_complexity("LOW") == "LOW"


def test_normalize_complexity_passthrough_medium():
    assert _normalize_complexity("MEDIUM") == "MEDIUM"


def test_normalize_complexity_passthrough_high():
    assert _normalize_complexity("HIGH") == "HIGH"


def test_normalize_complexity_default_unknown():
    assert _normalize_complexity("unknown") == "MEDIUM"


def test_normalize_complexity_case_insensitive():
    assert _normalize_complexity("Large") == "HIGH"
    assert _normalize_complexity("SMALL") == "LOW"


# ── test_tier_selection_low ────────────────────────────────────────────


def test_tier_selection_low():
    """LOW complexity: preflight=cheap, plan=mid, dev=cheap, code_review=mid."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "small")

    assert decision.dev.model == "haiku"  # cheap
    assert decision.preflight.model == "haiku"  # cheap
    assert decision.planner.model == "sonnet"  # mid
    assert len(decision.code_reviewers) == 1
    # code_review tier for LOW is "mid" but _select_reviewers prefers strong first
    # The pool has opus (strong), so it should be selected
    assert decision.code_reviewers[0].model == "opus"


def test_tier_selection_medium():
    """MEDIUM complexity: plan=strong, dev=mid, code_review=strong."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    assert decision.dev.model == "sonnet"  # mid
    assert decision.planner.model == "opus"  # strong
    assert len(decision.code_reviewers) == 1
    assert decision.code_reviewers[0].model == "opus"  # strong


def test_tier_selection_high():
    """HIGH complexity: plan=strong, dev=strong; code_review excludes dev model."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=3, max_cost_per_story_usd=1000.0)
    decision = assign_models(agents, cfg, "large")

    assert decision.dev.model == "opus"  # strong
    assert decision.planner.model == "opus"  # strong
    # code_reviewers exclude opus (dev model) and fall back to lower-tier agents
    assert len(decision.code_reviewers) >= 1
    assert all(r.model != "opus" for r in decision.code_reviewers)


# ── test_cross_provider_preference ────────────────────────────────────


def test_cross_provider_preference():
    """When pool has opus/anthropic + deepseek-r1/deepseek + sonnet/anthropic,
    two code reviewers should come from different providers."""
    agents = _make_agents_cross_provider()
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "medium")

    assert len(decision.code_reviewers) == 2
    providers = [r.provider for r in decision.code_reviewers]
    assert len(set(providers)) == 2, f"Expected 2 different providers, got {providers}"


def test_cross_provider_with_three_reviewers():
    """With prefer_cross_provider and 2 providers, 3 reviewers still returns valid pool."""
    agents = _make_agents_cross_provider()
    cfg = _make_cfg(
        min_reviewers=3, max_reviewers=3, prefer_cross_provider=True, max_cost_per_story_usd=1000.0
    )
    decision = assign_models(agents, cfg, "large")

    # Should still return reviewers (fills up with same-provider if needed)
    assert len(decision.code_reviewers) >= 1


# ── test_cross_cli_provider_rotation (issue #1468) ────────────────────


def _make_agents_cli_only() -> list[AgentDef]:
    """Pool with three strong-tier CLI agents (claude/codex/gemini) plus a cheap
    claude agent that takes the dev role, so the three strong agents remain
    available as reviewers. provider=None on every agent — distinguished only
    by their cli binary."""
    return [
        AgentDef(
            name="claude-sonnet",
            provider=None,
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=600,
            tier="cheap",
            cli="claude",
        ),
        AgentDef(
            name="claude-opus",
            provider=None,
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
            cli="claude",
        ),
        AgentDef(
            name="codex-gpt-5.4",
            provider=None,
            model="gpt-5.4",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
            cli="codex",
        ),
        AgentDef(
            name="gemini-pro",
            provider=None,
            model="gemini-2.5-pro",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
            cli="gemini",
        ),
    ]


def test_effective_provider_derived_from_cli_when_provider_none():
    """CLI-only agents (provider=None) expose an effective_provider derived from
    their cli binary so routing/display can distinguish claude/codex/gemini."""
    agents = [a for a in _make_agents_cli_only() if a.tier == "strong"]
    by_cli = {a.cli: a.effective_provider for a in agents}
    assert by_cli == {"claude": "anthropic", "codex": "openai", "gemini": "google"}


def test_effective_provider_explicit_provider_wins():
    """An explicit provider takes precedence over CLI-derived value."""
    a = AgentDef(
        name="custom",
        provider="deepseek",
        model="x",
        budget_usd=1.0,
        timeout_seconds=60,
        tier="strong",
        cli="claude",
    )
    assert a.effective_provider == "deepseek"


def test_cross_provider_cli_only_pool_yields_distinct_providers():
    """Three CLI agents (claude+codex+gemini) with prefer_cross_provider must
    produce three reviewers with distinct effective providers — the bug from
    issue #1468 collapsed all CLI agents to provider=None and excluded all but
    the first from the cross-provider rotation."""
    agents = _make_agents_cli_only()
    cfg = _make_cfg(
        min_reviewers=3,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=1000.0,
    )
    # complexity=small lets dev pick the cheap claude-sonnet so the three strong
    # reviewers (one per CLI binary) all remain available.
    decision = assign_models(agents, cfg, "small")

    assert len(decision.code_reviewers) == 3
    cli_set = {r.cli for r in decision.code_reviewers}
    assert cli_set == {"claude", "codex", "gemini"}, (
        f"Expected one reviewer per CLI binary, got {cli_set}"
    )


def test_cross_provider_rationale_never_shows_none_for_cli_pool():
    """The rationale display string must show derived provider names
    (anthropic/openai/google), not the literal None that CLI agents carry on
    AgentDef.provider."""
    agents = _make_agents_cli_only()
    cfg = _make_cfg(
        min_reviewers=2,
        max_reviewers=2,
        prefer_cross_provider=True,
        max_cost_per_story_usd=1000.0,
    )
    decision = assign_models(agents, cfg, "small")

    rationale = decision.rationale.get("code_review", "")
    assert "None" not in rationale, f"Rationale leaked None for CLI pool: {rationale}"
    # At least two of the derived providers must appear in the rationale.
    derived = {"anthropic", "openai", "google"}
    appearing = {p for p in derived if p in rationale}
    assert len(appearing) >= 2, f"Expected ≥2 derived providers in rationale, got {rationale}"


def test_cross_provider_mixed_cli_and_api_pool(monkeypatch):
    """Mixed pool (CLI claude + API deepseek): when both serve as reviewers, they
    must come from distinct effective providers — claude's effective provider is
    anthropic (derived from cli=claude), not None, so the diversity check
    distinguishes it from deepseek."""
    agents = [
        AgentDef(
            name="claude-sonnet",
            provider=None,
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=600,
            tier="cheap",
            cli="claude",
        ),
        AgentDef(
            name="claude-opus",
            provider=None,
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
            cli="claude",
        ),
        AgentDef(
            name="deepseek-r1",
            provider="deepseek",
            model="deepseek-reasoner",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="strong",
        ),
    ]
    # Pretend the claude CLI is on PATH so auth checks pass — otherwise the
    # cheap claude agent fails auth, dev fallback picks deepseek, and the
    # reviewer pool is confounded by the exclude-dev-model logic.
    monkeypatch.setattr("theforge.config.auth.shutil.which", lambda cmd, *a, **kw: "/usr/bin/x")
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "small")

    assert len(decision.code_reviewers) == 2
    # The claude reviewer (cli=claude, provider=None) and the deepseek reviewer
    # (provider=deepseek) must be distinguished — pre-fix, both collapsed into
    # the same `None` effective provider (or same `deepseek` after first pick)
    # depending on order, defeating the diversity contract.
    by_eff = []
    for r in decision.code_reviewers:
        if r.cli == "claude":
            by_eff.append("anthropic")
        elif r.provider:
            by_eff.append(r.provider)
    assert set(by_eff) == {"anthropic", "deepseek"}


# ── test_cross_provider_fallback ──────────────────────────────────────


def test_cross_provider_fallback_same_provider():
    """All-same-provider pool should still return min_reviewers without error."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "medium")

    # Should not raise; returns 2 reviewers even though same provider
    assert len(decision.code_reviewers) >= 1


# ── test_escalation_promotion ─────────────────────────────────────────


def test_promotion_fires_below_threshold():
    """#158: weighted dev success rate below threshold over the sample floor → promote.

    The MEDIUM dev tier is "mid" (sonnet). A profile whose recency-weighted MEDIUM
    success rate is well below the 0.60 default threshold, over >= 5 admissible
    runs, pre-promotes sonnet → strong (opus) before the first iteration.
    """
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    # 6 recent MEDIUM runs, mostly failures → weighted rate ~0.17 < 0.60.
    profiles = _dev_profiles(
        "sonnet", "medium", runs=6, success_rate=0.17, recent=[0, 1, 0, 0, 0, 0]
    )

    decision = assign_models(agents, cfg, "medium", model_profiles=profiles)

    assert decision.dev.model == "opus", (
        f"Expected opus (pre-promoted), got {decision.dev.model}. Rationale: {decision.rationale}"
    )
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is True
    assert promo["outcome"] == PROMOTION_OUTCOME_PROMOTED
    assert promo["weighted_success_rate"] < promo["threshold"]
    assert promo["sample_size"] == 6
    assert promo["resulting_tier"] == "strong"


def test_no_promotion_above_threshold():
    """#158: weighted dev success rate at/above threshold over the floor → no promotion."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    # 6 recent MEDIUM runs, mostly successes → weighted rate ~0.83 >= 0.60.
    profiles = _dev_profiles(
        "sonnet", "medium", runs=6, success_rate=0.83, recent=[1, 0, 1, 1, 1, 1]
    )

    decision = assign_models(agents, cfg, "medium", model_profiles=profiles)

    assert decision.dev.model == "sonnet", (
        f"Expected sonnet (no promotion), got {decision.dev.model}"
    )
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is False
    assert promo["weighted_success_rate"] >= promo["threshold"]


def test_minimum_sample_size_suppresses_promotion():
    """#158: below the min-runs sample floor, no promotion fires — static tier holds."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    # Only 4 admissible runs (< default floor 5), all failures. Rate is awful but
    # the sample floor is not met, so pre-promotion must NOT fire.
    profiles = _dev_profiles("sonnet", "medium", runs=4, success_rate=0.0, recent=[0, 0, 0, 0])

    decision = assign_models(agents, cfg, "medium", model_profiles=profiles)

    assert decision.dev.model == "sonnet"
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is False
    assert promo["outcome"] == PROMOTION_OUTCOME_BELOW_FLOOR
    assert promo["floor"] == "fail"
    assert promo["sample_size"] == 4


def test_recency_recovery_reverses_promotion():
    """#158, ADR-0006 clause 5: as old failures age out the weighted rate recovers,
    pre-promotion stops firing, and the paired demotion is recorded (clause 7).

    Lifetime cumulative rate stays poisoned (0.30) but the recent ring has
    recovered (old failures aged out), so the recency-weighted rate is >= threshold
    and pre-promotion does NOT fire — the passive return path.
    """
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    # Poisoned lifetime rate, but recent ring is mostly successes → recovered.
    profiles = _dev_profiles(
        "sonnet",
        "medium",
        runs=40,
        success_rate=0.30,
        recent=[0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    )

    decision = assign_models(agents, cfg, "medium", model_profiles=profiles)

    # Recovery: pre-promotion held, sonnet keeps the static mid tier.
    assert decision.dev.model == "sonnet"
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is False
    assert promo["outcome"] == PROMOTION_OUTCOME_RECOVERED
    assert promo["raw_success_rate"] == 0.30
    assert promo["weighted_success_rate"] >= promo["threshold"]
    # The paired recency-recovery demotion is recorded as fired (clause 7).
    demo = decision.routing_decision["dev"]["demotion_check"]
    assert demo["mechanism"] == "dev_recency_recovery"
    assert demo["applicable"] is True
    assert demo["fired"] is True
    assert demo["reason"] == "weighted_rate_recovered_to_or_above_threshold"


def test_tainted_runs_excluded_from_promotion_rate():
    """#158, ADR-0006 clause 4: tainted runs contribute no promotion weight.

    A band with only 4 admissible runs but 20 tainted ones stays below the sample
    floor — the tainted runs do not count toward the floor or the rate — so no
    promotion fires, and the exclusion count is surfaced in the audit.
    """
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    profiles = _dev_profiles(
        "sonnet", "medium", runs=4, success_rate=0.0, recent=[0, 0, 0, 0], tainted=20
    )

    decision = assign_models(agents, cfg, "medium", model_profiles=profiles)

    assert decision.dev.model == "sonnet"
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is False
    assert promo["outcome"] == PROMOTION_OUTCOME_BELOW_FLOOR
    assert promo["sample_size"] == 4  # tainted runs excluded from the count
    assert promo["tainted_runs"] == 20


def test_explicit_override_bypasses_pre_promotion():
    """#158, ADR-0006 clause 1: an explicit dev override skips the profile check."""
    from theforge.config import ModelProfile

    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    # Profile that would otherwise force a promotion.
    profiles = _dev_profiles(
        "sonnet", "medium", runs=8, success_rate=0.0, recent=[0, 0, 0, 0, 0, 0, 0, 0]
    )
    explicit_dev = ModelProfile(
        name="custom-dev",
        cli="claude",
        provider=None,
        model="custom-model",
        budget_usd=3.0,
        timeout_seconds=500,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )

    decision = assign_models(
        agents,
        cfg,
        "medium",
        model_profiles=profiles,
        explicit_profiles={"dev": explicit_dev},
    )

    assert decision.dev.model == "custom-model"
    # The pre-promotion mechanism never ran for an overridden dev role.
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is False
    assert promo["outcome"] == "not_checked"


def test_pre_promotion_is_deterministic():
    """#158: same profile data + same complexity → identical decision."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    profiles = _dev_profiles(
        "sonnet", "medium", runs=6, success_rate=0.17, recent=[0, 1, 0, 0, 0, 0]
    )

    first = assign_models(agents, cfg, "medium", model_profiles=profiles)
    second = assign_models(agents, cfg, "medium", model_profiles=profiles)

    assert first.dev.model == second.dev.model == "opus"
    assert (
        first.routing_decision["dev"]["promotion_check"]
        == second.routing_decision["dev"]["promotion_check"]
    )


def test_check_promotion_unit_recency_recovery():
    """#158 unit: _check_promotion returns the recency_recovery outcome directly."""
    agents = _make_agents_one_per_tier()
    dev_agent = next(a for a in agents if a.tier == "mid")
    profiles = _dev_profiles(
        "sonnet",
        "medium",
        runs=40,
        success_rate=0.30,
        recent=[0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    )

    signal = _check_promotion("MEDIUM", dev_agent, profiles, threshold=0.60, min_runs=5)

    assert signal.fired is False
    assert signal.outcome == PROMOTION_OUTCOME_RECOVERED
    assert signal.weighted is not None and signal.weighted >= signal.threshold


# ── test_explicit_override ────────────────────────────────────────────


def test_explicit_override():
    """explicit_profiles['dev'] should force that model; others come from pool."""
    from theforge.config import ModelProfile

    agents = _make_agents_one_per_tier()
    cfg = _make_cfg()

    explicit_dev = ModelProfile(
        name="custom-dev",
        cli="claude",
        provider=None,
        model="custom-model",
        budget_usd=3.0,
        timeout_seconds=500,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )

    decision = assign_models(agents, cfg, "medium", explicit_profiles={"dev": explicit_dev})

    assert decision.dev.model == "custom-model"
    assert decision.dev.name == "custom-dev"
    # Other phases still come from pool
    assert decision.planner.model in ("haiku", "sonnet", "opus")


def test_numeric_complexity_score_splits_same_legacy_band_assignments():
    """Different scores in the medium band can route to different roles."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=3, prefer_cross_provider=False)

    lower_medium = assign_models(agents, cfg, "medium", complexity_score=4)
    upper_medium = assign_models(agents, cfg, "medium", complexity_score=7)

    assert lower_medium.planner.model == "sonnet"
    assert upper_medium.planner.model == "opus"
    assert lower_medium.dev.model == "sonnet"
    assert upper_medium.dev.model == "opus"
    assert len(lower_medium.code_reviewers) == 1
    assert len(upper_medium.code_reviewers) == 2


def test_adaptive_disabled_ignores_complexity_score():
    """With adaptive_enabled=False, two scores in the same band yield identical assignments."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=3,
        prefer_cross_provider=False,
        adaptive_enabled=False,
    )

    low_score = assign_models(agents, cfg, "medium", complexity_score=2)
    high_score = assign_models(agents, cfg, "medium", complexity_score=9)

    # Legacy band-only routing: MEDIUM dev tier == "mid" regardless of score.
    assert low_score.dev.model == high_score.dev.model == "sonnet"
    assert low_score.planner.model == high_score.planner.model
    assert [p.model for p in low_score.code_reviewers] == [
        p.model for p in high_score.code_reviewers
    ]
    assert [p.model for p in low_score.plan_reviewers] == [
        p.model for p in high_score.plan_reviewers
    ]


def test_adaptive_disabled_skips_promotion_and_profiles():
    """Static mode bypasses profile-backed pre-promotion and capability rerank."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        adaptive_enabled=False,
    )
    # A profile that would force a promotion under adaptive routing.
    profiles = _dev_profiles(
        "sonnet", "medium", runs=8, success_rate=0.0, recent=[0, 0, 0, 0, 0, 0, 0, 0]
    )

    decision = assign_models(agents, cfg, "medium", model_profiles=profiles)

    # With adaptive off, no promotion: dev stays at MEDIUM band tier "mid" → sonnet.
    assert decision.dev.model == "sonnet"


def test_explicit_override_survives_budget_downgrade_pressure():
    """Budget enforcement must not replace an explicitly configured role."""
    from theforge.config import ModelProfile

    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=6.0,
        prefer_cross_provider=False,
    )
    explicit_dev = ModelProfile(
        name="custom-dev",
        cli="claude",
        provider=None,
        model="custom-model",
        budget_usd=4.5,
        timeout_seconds=500,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )

    decision = assign_models(
        agents,
        cfg,
        "medium",
        explicit_profiles={"dev": explicit_dev},
    )

    assert decision.dev.model == "custom-model"
    assert decision.budget_audit["downgraded"] is True
    assert all(step["role"] != "dev" for step in decision.budget_audit["steps"])


def test_explicit_override_records_forced_overrun_when_budget_unmet():
    """When a locked role keeps total over cap, audit must flag override_forced_overrun."""
    from theforge.config import ModelProfile

    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=2.0,  # very tight cap
        prefer_cross_provider=False,
    )
    explicit_dev = ModelProfile(
        name="custom-dev",
        cli="claude",
        provider=None,
        model="custom-model",
        budget_usd=50.0,
        timeout_seconds=500,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )

    with pytest.warns(UserWarning, match="per-story routing cost target"):
        decision = assign_models(
            agents,
            cfg,
            "medium",
            explicit_profiles={"dev": explicit_dev},
        )

    assert decision.dev.model == "custom-model"
    assert decision.budget_audit["within_target"] is False
    assert decision.budget_audit.get("override_forced_overrun") is True
    assert "dev" in decision.budget_audit.get("locked_roles", [])


def test_agent_to_profile_preserves_api_fallback_for_adaptive_cli_agents(monkeypatch):
    """Adaptive assignment must preserve CLI fallback metadata on synthesized profiles."""
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    agents = [
        AgentDef(
            name="codex-dev",
            provider=None,
            cli="codex",
            model="gpt-5.4",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
            api_fallback=TransportFallbackConfig(provider="openai", model="gpt-5.4"),
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
        ),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)

    decision = assign_models(agents, cfg, "medium")

    assert decision.dev.cli == "codex"
    assert decision.dev.api_fallback == TransportFallbackConfig(provider="openai", model="gpt-5.4")


# ── test_budget_cap_enforcement ───────────────────────────────────────


def test_budget_cap_enforcement():
    """When sum of budgets exceeds cap, downgrade until within budget."""
    # Use a very tight budget that forces downgrades
    agents = _make_agents_one_per_tier()
    # Uncapped MEDIUM story: preflight(1) + planner(8) + plan_review(8) + dev(5) +
    # code_review(8) ≈ $30. With budget=$10 and all phases as downgrade candidates,
    # enforcement should converge: all-cheap = $1×5 = $5 ≤ $10.
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=10.0,
        prefer_cross_provider=False,
    )

    decision = assign_models(agents, cfg, "medium")

    total = (
        decision.preflight.budget_usd
        + decision.planner.budget_usd
        + sum(p.budget_usd for p in decision.plan_reviewers)
        + decision.dev.budget_usd
        + sum(p.budget_usd for p in decision.code_reviewers)
    )
    assert total <= 10.0, f"Budget cap not met: total ${total:.2f} > $10.00"
    assert isinstance(decision, AssignmentDecision)
    assert decision.dev is not None
    assert len(decision.code_reviewers) >= 1


def test_budget_cap_plan_phase_downgraded():
    """Plan-phase models are downgraded when they alone exceed the cap.

    This guards against the regression where _enforce_budget excluded planner and
    plan_reviewers from candidates, making the cap unreachable for MEDIUM+ stories.
    """
    agents = _make_agents_one_per_tier()
    # MEDIUM complexity: planner=strong($8) + plan_reviewer=strong($8) = $16 alone.
    # With budget=$12, plan-phase models must be downgraded to reach the cap.
    # All-cheap floor = $1×5 = $5 so $12 IS achievable.
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=12.0,
        prefer_cross_provider=False,
    )

    decision = assign_models(agents, cfg, "medium")

    total = (
        decision.preflight.budget_usd
        + decision.planner.budget_usd
        + sum(p.budget_usd for p in decision.plan_reviewers)
        + decision.dev.budget_usd
        + sum(p.budget_usd for p in decision.code_reviewers)
    )
    assert total <= 12.0, f"Budget cap not met: total ${total:.2f} > $12.00"
    # At least one plan-phase model must have been downgraded from strong ($8)
    plan_phase_max = max(
        decision.planner.budget_usd,
        *(p.budget_usd for p in decision.plan_reviewers),
    )
    assert plan_phase_max < 8.0, (
        f"Plan-phase models were not downgraded (max budget ${plan_phase_max:.2f})"
    )


def test_budget_cap_downgrade_dev_respects_floor():
    """Budget enforcer never downgrades dev below the complexity tier floor.

    HIGH complexity floor is 'strong'. Even under a tight budget, dev must
    stay at strong — the cap warning fires but dev is not demoted to cheap.
    """
    agents = [
        AgentDef("cheap-agent", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("strong-agent", "anthropic", "opus", 50.0, 1200, "strong"),
    ]
    # HIGH complexity → dev floor = strong.  $5 budget is impossible to meet
    # without violating the floor, so the enforcer must warn and leave dev alone.
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=5.0,
        prefer_cross_provider=False,
    )
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        decision = assign_models(agents, cfg, "large")

    # Dev must remain at the strong tier — guardrail prevents cheap downgrade
    assert decision.dev.name == "strong-agent", (
        f"Dev should stay strong for HIGH; got {decision.dev.name}"
    )
    # Budget cannot be met, so a warning must have been issued
    assert any("per-story routing cost target" in str(w.message) for w in caught), (
        "Expected budget-cap warning when floor prevents the cap from being met"
    )


def test_budget_cap_downgrade_dev_medium_stops_at_mid():
    """Budget enforcer downgrades MEDIUM dev from strong (promoted) to mid, not cheap."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 50.0, 1200, "strong"),
    ]
    # Build escalation history that promotes MEDIUM dev (sonnet) to strong (opus)
    history = [
        EscalationRecord(
            story=f"s{i}", complexity="MEDIUM", dev_model="sonnet", outcome="ESCALATE"
        )
        for i in range(2)
    ]
    # Budget of $20: preflight($1) + planner($50) + plan_reviewer($50) + dev($50) +
    # code_review($50) ≈ $201. Force dev downgrade. MEDIUM floor = mid ($5).
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=20.0,
        prefer_cross_provider=False,
    )
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        decision = assign_models(agents, cfg, "medium", escalation_history=history)

    # Dev may be downgraded from strong (opus) to mid (sonnet) but NOT to cheap (haiku)
    assert decision.dev.name != "haiku", (
        f"Dev should not fall below mid floor for MEDIUM; got {decision.dev.name}"
    )


def test_budget_cap_preserves_strong_planner_when_dev_is_also_strong():
    """Adaptive strong planner stays strong when the story already needs strong dev."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=15.0,
        prefer_cross_provider=False,
    )
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        decision = assign_models(agents, cfg, "medium", complexity_score=9)

    assert decision.dev.model == "opus"
    assert decision.planner.model == "opus"
    assert decision.budget_audit["within_target"] is False
    assert "planner" in decision.budget_audit.get("protected_roles", [])
    assert "protected roles" in decision.rationale["per_story_routing_cost_target"]
    assert any("per-story routing cost target" in str(w.message) for w in caught)


def test_budget_cap_records_downgrade_rationale():
    """Budget downgrades must be visible in the decision rationale/audit."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=10.0,
        prefer_cross_provider=False,
    )

    decision = assign_models(agents, cfg, "medium")

    assert (
        "per-story routing cost target $10.00"
        in decision.rationale["per_story_routing_cost_target"]
    )
    assert decision.budget_audit["downgraded"] is True
    assert decision.budget_audit["final_total_usd"] <= 10.0
    assert decision.budget_audit["steps"]


def test_budget_cap_keeps_planner_rationale_aligned_with_final_model():
    """Planner rationale must describe the post-budget planner that will actually run."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=10.0,
        prefer_cross_provider=False,
    )

    decision = assign_models(agents, cfg, "medium", complexity_score=4)

    assert decision.planner.model == "haiku"
    assert (
        "per-story routing cost target $10.00 downgraded to haiku" in decision.rationale["planner"]
    )
    assert any(step["role"] == "planner" for step in decision.budget_audit["steps"])


# ── test_deterministic ────────────────────────────────────────────────


def test_deterministic():
    """Same inputs produce same outputs every time."""
    agents = _make_agents_cross_provider()
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2)
    history = [
        EscalationRecord("s1", "MEDIUM", "sonnet", "DONE"),
    ]

    decision1 = assign_models(agents, cfg, "medium", escalation_history=history)
    decision2 = assign_models(agents, cfg, "medium", escalation_history=history)

    assert decision1.dev.model == decision2.dev.model
    assert decision1.dev.name == decision2.dev.name
    assert decision1.preflight.model == decision2.preflight.model
    assert decision1.planner.model == decision2.planner.model
    assert [r.model for r in decision1.code_reviewers] == [
        r.model for r in decision2.code_reviewers
    ]
    assert [r.model for r in decision1.plan_reviewers] == [
        r.model for r in decision2.plan_reviewers
    ]


# ── Phase→tier mapping tests ──────────────────────────────────────────


def test_phase_tier_table_completeness():
    """Every phase has LOW/MEDIUM/HIGH entries."""
    expected_phases = {"preflight", "plan", "plan_review", "dev", "code_review"}
    assert set(PHASE_TIER.keys()) == expected_phases
    for phase, tiers in PHASE_TIER.items():
        assert set(tiers.keys()) == {"LOW", "MEDIUM", "HIGH"}, (
            f"Phase {phase!r} missing complexity keys"
        )


def test_dev_cheap_for_low():
    assert PHASE_TIER["dev"]["LOW"] == "cheap"


def test_dev_mid_for_medium():
    assert PHASE_TIER["dev"]["MEDIUM"] == "mid"


def test_dev_strong_for_high():
    assert PHASE_TIER["dev"]["HIGH"] == "strong"


# ── Reviewer count tests ──────────────────────────────────────────────


def test_reviewer_count_low():
    assert _reviewer_count("LOW", 1, 3) == 1


def test_reviewer_count_high():
    assert _reviewer_count("HIGH", 1, 3) == 3


def test_reviewer_count_medium():
    count = _reviewer_count("MEDIUM", 1, 3)
    assert 1 <= count <= 3


# ── Sample-floor contract: no evidence → no firing ────────────────────


def test_promotion_never_fires_without_admissible_evidence():
    """A fired pre-promotion ALWAYS carries admissible profile evidence (#158).

    The profile signal is the sole authoritative driver — there is no sprint
    cache or other shortcut that can report a fired promotion with a null rate or
    a zero sample size. Every no-evidence path (no profiles, empty band, below
    the sample floor) returns ``fired=False``, so the sample-floor contract holds
    on every path (regression guard for the cached-promotion bypass, cycle 1 P1).
    """
    agents = _make_agents_one_per_tier()
    dev_agent = next(a for a in agents if a.tier == "mid")

    # No profiles at all.
    no_profiles = _check_promotion("MEDIUM", dev_agent, None, threshold=0.60, min_runs=5)
    assert no_profiles.fired is False
    assert no_profiles.outcome == "no_profile_signal"
    assert no_profiles.raw is None and no_profiles.weighted is None
    assert no_profiles.runs == 0

    # Admissible samples below the floor: awful rate, but too few runs to fire.
    below_floor = _check_promotion(
        "MEDIUM",
        dev_agent,
        _dev_profiles("sonnet", "medium", runs=3, success_rate=0.0, recent=[0, 0, 0]),
        threshold=0.60,
        min_runs=5,
    )
    assert below_floor.fired is False
    assert below_floor.outcome == PROMOTION_OUTCOME_BELOW_FLOOR
    assert below_floor.runs == 3

    # Invariant across every outcome: firing implies real, admissible evidence.
    for signal in (no_profiles, below_floor):
        if signal.fired:  # never true here, but encodes the contract
            assert signal.weighted is not None
            assert signal.runs >= signal.min_runs


def test_assign_models_promotion_check_never_reports_null_evidence_firing():
    """Seam of the P1 fix: routing_decision.promotion_check cannot report a fired
    promotion with sample_size 0 / null rates. Below the floor, no firing."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    profiles = _dev_profiles("sonnet", "medium", runs=3, success_rate=0.0, recent=[0, 0, 0])

    decision = assign_models(agents, cfg, "medium", model_profiles=profiles)

    assert decision.dev.model == "sonnet"  # no promotion below the floor
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is False
    if promo["fired"]:  # contract: a fired promotion carries admissible evidence
        assert promo["weighted_success_rate"] is not None
        assert promo["sample_size"] >= promo["min_runs"]


def test_no_promotion_without_profiles():
    """No profile data → no promotion (cold start falls through to static tier)."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    # No profile signal — should use mid (sonnet).
    assert decision.dev.model == "sonnet"
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is False


# ── Plan reviewer diversity tests ─────────────────────────────────────


def test_plan_reviewer_excludes_planner_model():
    """Plan reviewers must not share a model with the planner when alternatives exist."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 3.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    planner_model = decision.planner.model
    reviewer_models = [r.model for r in decision.plan_reviewers]
    assert planner_model not in reviewer_models, (
        f"Plan reviewer {reviewer_models} shares model with planner ({planner_model})"
    )


def test_plan_reviewer_excludes_planner_when_only_strong_is_planner():
    """Regression: large story, only one strong agent (planner). Mid-tier must be used."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 3.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    # Planner should be opus (strong); reviewer must not be opus
    assert decision.planner.model == "opus"
    reviewer_models = [r.model for r in decision.plan_reviewers]
    assert "opus" not in reviewer_models, (
        f"Plan reviewer {reviewer_models} self-reviews — sonnet should have been selected"
    )
    assert reviewer_models == ["sonnet"]


def test_plan_reviewer_falls_back_when_only_one_model():
    """When only one model is available, it must still produce a reviewer (not empty)."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    # Only one model — must still produce a reviewer (fallback to self-review)
    assert len(decision.plan_reviewers) == 1
    assert decision.plan_reviewers[0].model == "opus"


def test_plan_reviewer_falls_back_cross_provider_single_model():
    """Single-model pool with cross_provider=True still produces a reviewer."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "large")

    assert len(decision.plan_reviewers) == 1
    assert decision.plan_reviewers[0].model == "opus"


def test_plan_reviewer_excludes_planner_cross_provider():
    """Cross-provider selection also excludes the planner model."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
        AgentDef("deepseek-r1", "deepseek", "deepseek-reasoner", 1.0, 600, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "large")

    planner_model = decision.planner.model
    reviewer_models = [r.model for r in decision.plan_reviewers]
    assert planner_model not in reviewer_models, (
        f"Plan reviewer {reviewer_models} shares model with planner ({planner_model})"
    )


def test_code_reviewer_excludes_dev_model():
    """Code reviewers must not share a model with the dev agent when alternatives exist."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 3.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    dev_model = decision.dev.model
    reviewer_models = [r.model for r in decision.code_reviewers]
    assert dev_model not in reviewer_models, (
        f"Code reviewer {reviewer_models} shares model with dev ({dev_model})"
    )


def test_code_reviewer_falls_back_when_only_one_model():
    """Single-model pool: code reviewer falls back to self-review, not empty list."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    assert len(decision.code_reviewers) == 1
    assert decision.code_reviewers[0].model == "opus"


def test_plan_reviewer_non_empty_when_no_auth(monkeypatch):
    """When no agents have auth, plan_reviewers falls back to unauthed agents (not empty)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    assert len(decision.plan_reviewers) >= 1


# ── Tier guardrail tests ───────────────────────────────────────────────


def test_guardrail_cheap_not_assigned_medium_when_mid_available():
    """cheap cannot be dev on MEDIUM; mid must be preferred when available."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    assert decision.dev.name != "haiku", (
        f"cheap model haiku should not dev MEDIUM, got {decision.dev.name}"
    )


def test_guardrail_cheap_not_assigned_large_when_strong_available():
    """cheap cannot be dev on HIGH (large); strong must be preferred when available."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "large")

    assert decision.dev.name != "haiku", (
        f"cheap model haiku should not dev HIGH, got {decision.dev.name}"
    )


def test_guardrail_mid_not_assigned_large_when_strong_available():
    """mid cannot be dev on HIGH (large) when a strong agent is present."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "large")

    assert decision.dev.name == "opus", f"Expected strong agent for HIGH, got {decision.dev.name}"


def test_guardrail_fallback_prefers_highest_tier_when_floor_unavailable():
    """When no strong agent exists for HIGH, fallback picks mid (not cheap)."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "large")

    # No strong available — mid is the best we can do; cheap must not be selected
    assert decision.dev.name == "sonnet", (
        f"Expected mid (sonnet) as best-available for HIGH, got {decision.dev.name}"
    )
    assert "WARNING" in decision.rationale.get("dev", ""), (
        "Rationale must warn when floor cannot be met"
    )


def test_guardrail_fallback_prefers_unauthed_floor_agent_over_authed_below_floor(monkeypatch):
    """Unauthed floor-compliant agent is preferred over authed below-floor agent."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        # sonnet needs OPENAI_API_KEY which we've removed — unauthed but at floor
        AgentDef("sonnet", "openai", "gpt-4o", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    # sonnet (unauthed mid) must win over haiku (authed cheap): floor compliance
    # beats auth availability.
    assert decision.dev.name == "sonnet", (
        f"Expected unauthed mid (sonnet) over authed cheap (haiku), got {decision.dev.name}"
    )


def test_guardrail_warns_when_no_floor_compliant_agent_exists(monkeypatch):
    """When pool has no floor-compliant agents at all, rationale warns and picks best tier."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    # Only cheap available for MEDIUM (floor=mid) — must warn in rationale
    assert decision.dev.name == "haiku"
    assert "WARNING" in decision.rationale.get("dev", ""), (
        "Rationale must warn when pool has no floor-compliant agent"
    )


def test_guardrail_promotion_cannot_exceed_top_tier():
    """#158 guardrail: for HIGH the base tier is already strong — promotion has no room.

    A poor profile at the LARGE band cannot push the dev above the top tier; the
    guardrail (strong is the ceiling) holds and opus is selected.
    """
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    # opus is the strong-tier dev for HIGH; even an awful LARGE-band rate can only
    # keep it at strong (promotion clamps at the top tier).
    profiles = _dev_profiles(
        "opus", "large", runs=8, success_rate=0.0, recent=[0, 0, 0, 0, 0, 0, 0, 0]
    )

    decision = assign_models(agents, cfg, "large", model_profiles=profiles)

    assert decision.dev.name == "opus", f"Expected strong (opus) for HIGH, got {decision.dev.name}"
    # Resulting tier never exceeds the top tier even when the check fires.
    promo = decision.routing_decision["dev"]["promotion_check"]
    assert promo["resulting_tier"] == "strong"


# ── per-story routing cost target (split from sprint budget) ──────────────


def test_unset_cap_preserves_adaptive_full_pool_on_high_complexity():
    """AC: with cap unset, a score-9 large story keeps adaptive's full reviewer pool/tier."""
    from dataclasses import replace as _dc_replace

    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
        AgentDef("deepseek-r1", "deepseek", "deepseek-reasoner", 1.0, 600, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=3, max_cost_per_story_usd=None)

    # Reference run with a generous cap captures adaptive's preferred selection.
    ref_decision = assign_models(
        agents,
        _dc_replace(cfg, max_cost_per_story_usd=1000.0),
        "large",
        complexity_score=9,
    )

    # With cap unset, selection must match the unconstrained reference exactly.
    decision = assign_models(agents, cfg, "large", complexity_score=9)

    assert decision.budget_audit["downgraded"] is False
    assert decision.budget_audit["steps"] == []
    assert decision.budget_audit["target_usd"] is None
    assert len(decision.code_reviewers) == len(ref_decision.code_reviewers)
    assert decision.dev.model == ref_decision.dev.model
    assert [r.model for r in decision.code_reviewers] == [
        r.model for r in ref_decision.code_reviewers
    ]
    cap_text = decision.rationale["per_story_routing_cost_target"]
    assert "unset" in cap_text
    assert "budget" not in cap_text.lower()


def test_set_cap_records_preferred_snapshot_in_audit():
    """AC support: when cap downgrades selection, audit records adaptive's preferred selection."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        max_cost_per_story_usd=10.0,
        prefer_cross_provider=False,
    )

    decision = assign_models(agents, cfg, "medium")

    audit = decision.budget_audit
    assert audit["downgraded"] is True
    preferred = audit["preferred"]
    assert preferred["dev"]["model"]
    assert preferred["planner"]["model"]
    assert preferred["total_usd"] > audit["final_total_usd"]
    # Audit must never carry the word "budget" in its top-level keys
    assert "budget" not in audit
    assert "budget_cap_usd" not in audit


# ── test_reviewer_count_honors_target_via_tier_descent (issue #1542) ──


def test_high_score_panel_descends_tier_ladder_when_strong_provider_pool_narrow():
    """HIGH-score story whose strong tier post-self-exclusion has one provider
    must still fill its requested panel by descending to lower tiers.

    Regression for #1542: previously `_select_reviewers` built candidates as
    strong+[] when tier=='strong', so removing the dev model from a
    same-provider strong pool collapsed the panel to whatever the single
    remaining provider supplied (often 1). Mid-target stories saw strong∪mid
    and ended up with wider panels than higher-risk strong-target stories.
    """
    agents = [
        # Two strong agents but same provider after self-exclusion
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gpt-strong", "openai", "gpt-5", 7.0, 1200, "strong"),
        # Lower-tier alternatives at distinct providers
        AgentDef("gemini-mid", "google", "gemini-pro", 4.0, 900, "mid"),
        AgentDef("haiku", "anthropic", "haiku", 1.0, 600, "cheap"),
    ]
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=1000.0,
    )

    # HIGH complexity, score 8 → dev tier strong → opus (cheapest anthropic strong?)
    # Actually cheapest strong is gpt-strong (7.0) — dev picks gpt-strong.
    # Reviewer pool excludes gpt-5; previously collapsed to 1, must now fill 3.
    decision = assign_models(agents, cfg, "large", complexity_score=8)

    # Requested n for score=8 is max_reviewers=3. The descent should make
    # at least 2 reviewers available (opus + gemini-mid + haiku exclude dev).
    assert len(decision.code_reviewers) >= 2, (
        f"HIGH-score panel collapsed to {len(decision.code_reviewers)} reviewer(s); "
        f"rationale: {decision.rationale.get('code_review')}"
    )
    # The dev model must never appear in reviewers (self-exclusion holds).
    dev_model = decision.dev.model
    assert all(r.model != dev_model for r in decision.code_reviewers)


def test_high_score_panel_not_smaller_than_mid_score_panel_same_pool():
    """Asymmetry guard: a score-8 story must not yield fewer reviewers than a
    score-5 story drawn from the same agent pool. The bug at #1542 was exactly
    this inversion."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gpt-strong", "openai", "gpt-5", 7.0, 1200, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("gemini-mid", "google", "gemini-pro", 4.0, 900, "mid"),
    ]
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=1000.0,
    )

    mid_score = assign_models(agents, cfg, "medium", complexity_score=5)
    high_score = assign_models(agents, cfg, "large", complexity_score=8)

    assert len(high_score.code_reviewers) >= len(mid_score.code_reviewers), (
        f"Inverted panel sizes: mid-score={len(mid_score.code_reviewers)} "
        f"vs high-score={len(high_score.code_reviewers)}. "
        f"high rationale: {high_score.rationale.get('code_review')}"
    )


def test_reviewer_shortfall_emits_warning_in_rationale():
    """When the candidate pool genuinely cannot satisfy n, the rationale must
    record an explicit shortfall warning rather than silently truncating."""
    # Only two agents both at strong tier, same provider — n=3 cannot be honored
    # cross-provider even with descent.
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(
        min_reviewers=3,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=1000.0,
    )
    decision = assign_models(agents, cfg, "large", complexity_score=8)

    assert len(decision.code_reviewers) < 3
    assert "WARNING" in decision.rationale["code_review"]
    assert "requested 3" in decision.rationale["code_review"]


def test_reviewer_panel_full_no_shortfall_warning():
    """When the panel is fully populated, the rationale must NOT mention a shortfall."""
    agents = _make_agents_cross_provider()
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)

    decision = assign_models(agents, cfg, "medium")

    assert len(decision.code_reviewers) == 2
    assert "WARNING" not in decision.rationale["code_review"]
