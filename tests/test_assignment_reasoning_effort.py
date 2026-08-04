"""Score-driven reasoning effort (#1108) — assignment-level application.

Covers the seam where the routing policy's per-phase effort bucket becomes a
concrete field on the ModelProfile the coordinator hands to a runner, plus the
provider-support statuses recorded in ``routing_decision``.
"""

from __future__ import annotations

import pytest

from theforge.assignment import AssignmentDecision, resolve_reasoning_effort
from theforge.config import ModelProfile, ProviderReasoningEffortConfig, ReasoningEffortConfig


def _profile(name: str, model: str, *, cli=None, provider=None, **kwargs) -> ModelProfile:
    return ModelProfile(
        name=name,
        model=model,
        cli=cli,
        provider=provider,
        budget_usd=5.0,
        timeout_seconds=600,
        allowed_tools=(),
        **kwargs,
    )


def _codex(model: str = "gpt-5.4", **kwargs) -> ModelProfile:
    return _profile("codex", model, cli="codex", **kwargs)


def _google(model: str = "gemini-2.5-pro", **kwargs) -> ModelProfile:
    return _profile("gemini", model, provider="google", **kwargs)


def _claude(model: str = "claude-opus-4-6", **kwargs) -> ModelProfile:
    return _profile("claude", model, cli="claude", **kwargs)


def _decision(
    *,
    planner: ModelProfile | None = None,
    dev: ModelProfile | None = None,
    plan_reviewers: list[ModelProfile] | None = None,
    code_reviewers: list[ModelProfile] | None = None,
) -> AssignmentDecision:
    return AssignmentDecision(
        preflight=_claude(),
        planner=planner or _codex(),
        plan_reviewers=plan_reviewers if plan_reviewers is not None else [_google()],
        dev=dev or _codex(),
        code_reviewers=code_reviewers if code_reviewers is not None else [_google()],
    )


def _resolve(decision, score, cfg=None):
    return resolve_reasoning_effort(decision, score=score, cfg=cfg or ReasoningEffortConfig())


# ── Per-phase mapping ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("score", "plan", "dev", "review"),
    [
        (1, "medium", "low", "low"),
        (3, "medium", "low", "low"),
        (4, "high", "medium", "medium"),
        (5, "high", "medium", "medium"),
        (6, "high", "medium", "medium"),
        (7, "high", "high", "high"),
        (10, "high", "high", "high"),
    ],
)
def test_default_table_applies_independently_per_phase(score, plan, dev, review):
    """Each phase resolves its own band; plan leads dev at the same score."""
    updated, block = _resolve(_decision(), score)

    assert block["phases"]["plan"]["output"] == plan
    assert block["phases"]["dev"]["output"] == dev
    assert block["phases"]["review"]["output"] == review
    # And the resolved level lands on the profile the coordinator hands the runner.
    assert updated.planner.reasoning_effort == plan
    assert updated.dev.reasoning_effort == dev


def test_effort_reaches_the_profiles_that_run():
    """Cross-phase handoff: assignment's profiles are what preflight passes to
    the runners, so the knob must be set on them, not recorded beside them."""
    updated, _ = _resolve(_decision(), 8)

    assert updated.dev.reasoning_effort == "high"
    assert updated.planner.reasoning_effort == "high"
    # Google reviewers get the token-budget form of the same bucket.
    assert updated.plan_reviewers[0].thinking_budget == 24576
    assert updated.code_reviewers[0].thinking_budget == 24576
    assert updated.plan_reviewers[0].reasoning_effort is None


def test_codex_profile_renders_the_effort_flag():
    """The applied field is the one runner_codex actually passes to the CLI."""
    from pathlib import Path

    from theforge.runners.runner_codex import build_argv

    updated, _ = _resolve(_decision(), 2)
    cmd = build_argv(
        profile=updated.dev,
        working_dir=Path("/tmp"),
        output_file=Path("/tmp/out.txt"),
        prompt="prompt",
    )

    assert "model_reasoning_effort=low" in cmd


# ── Token budgets ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("score", "budget"), [(2, 2048), (5, 8192), (9, 24576)])
def test_token_budget_providers_resolve_each_level_to_a_count(score, budget):
    updated, block = _resolve(_decision(), score)

    assert updated.code_reviewers[0].thinking_budget == budget
    entry = block["phases"]["review"]["models"][0]
    assert entry["field"] == "thinking_budget"
    assert entry["value"] == budget
    assert entry["knob"] == "budget"


def test_token_budgets_are_configurable_per_provider():
    cfg = ReasoningEffortConfig(
        token_budgets={"high": 30000},
        providers={"google": ProviderReasoningEffortConfig(token_budgets={"high": 40000})},
    )
    updated, _ = _resolve(_decision(), 9, cfg)
    assert updated.code_reviewers[0].thinking_budget == 40000

    # Without the provider entry the sprint-wide override applies.
    sprint_only = ReasoningEffortConfig(token_budgets={"high": 30000})
    updated, _ = _resolve(_decision(), 9, sprint_only)
    assert updated.code_reviewers[0].thinking_budget == 30000


# ── Per-provider bucket overrides ─────────────────────────────────────


def test_provider_bucket_override_changes_only_that_provider():
    """Providers differ in how they honor effort, so the table is per-provider."""
    cfg = ReasoningEffortConfig(
        providers={
            "codex": ProviderReasoningEffortConfig(phase_buckets={"dev": ((10, "high"),)}),
        }
    )
    updated, block = _resolve(_decision(), 1, cfg)

    # Codex dev is pinned high by its override…
    assert updated.dev.reasoning_effort == "high"
    assert block["phases"]["dev"]["models"][0]["output"] == "high"
    # …while the google reviewers keep the default low band.
    assert updated.code_reviewers[0].thinking_budget == 2048


def test_sprint_wide_bucket_override_applies_to_every_provider():
    cfg = ReasoningEffortConfig(phase_buckets={"dev": ((10, "high"),)})
    updated, _ = _resolve(_decision(), 1, cfg)
    assert updated.dev.reasoning_effort == "high"


# ── Provider support statuses ─────────────────────────────────────────


def test_unsupported_provider_is_recorded_and_not_applied():
    decision = _decision(dev=_claude(), planner=_claude())
    updated, block = _resolve(decision, 9)

    assert updated.dev.reasoning_effort is None
    assert updated.dev.thinking_budget is None
    dev_entry = block["phases"]["dev"]["models"][0]
    assert dev_entry["provider_support"] == "provider_unsupported"
    assert dev_entry["applied"] is False
    assert dev_entry["reason"] == "provider_has_no_reasoning_effort_passthrough"
    # The bucket is still resolved and recorded — only the application is skipped.
    assert block["phases"]["dev"]["output"] == "high"


def test_gemini_cli_is_unsupported_even_though_google_api_is_not():
    """The knob is a transport capability, not a provider-family one."""
    decision = _decision(dev=_profile("gemini-cli", "gemini-2.5-pro", cli="gemini"))
    updated, block = _resolve(decision, 9)

    assert updated.dev.thinking_budget is None
    assert block["phases"]["dev"]["models"][0]["provider_support"] == "provider_unsupported"


def test_metered_when_the_transport_prices_its_thinking_spend():
    _, block = _resolve(_decision(), 9)

    assert block["phases"]["dev"]["models"][0]["provider_support"] == "supported_metered"
    assert block["phases"]["review"]["models"][0]["provider_support"] == "supported_metered"


def test_unmetered_when_the_model_has_no_pricing_entry():
    """Score-driven spend the audit cannot price must be visible as unmetered,
    not silently folded into a $0.00 total."""
    decision = _decision(dev=_codex("gpt-unpriced-preview"))
    _, block = _resolve(decision, 9)

    entry = block["phases"]["dev"]["models"][0]
    assert entry["provider_support"] == "supported_unmetered"
    # Support status is orthogonal to application: the knob still gets set.
    assert entry["applied"] is True
    assert entry["value"] == "high"


def test_phase_support_is_mixed_when_the_reviewer_pool_spans_providers():
    decision = _decision(plan_reviewers=[_google(), _claude()], code_reviewers=[])
    _, block = _resolve(decision, 9)

    assert block["phases"]["review"]["provider_support"] == "mixed"
    statuses = [m["provider_support"] for m in block["phases"]["review"]["models"]]
    assert statuses == ["supported_metered", "provider_unsupported"]


# ── Explicit config and disabled/no-score paths ───────────────────────


def test_static_profile_value_wins_over_score_routing():
    """Explicit operator config bypasses adaptive mechanisms (ADR-0006 clause 1)."""
    decision = _decision(dev=_codex(reasoning_effort="low"), plan_reviewers=[], code_reviewers=[])
    updated, block = _resolve(decision, 9)

    assert updated.dev.reasoning_effort == "low"
    entry = block["phases"]["dev"]["models"][0]
    assert entry["applied"] is False
    assert entry["reason"] == "static_profile_override"
    assert entry["value"] == "low"


def test_disabled_config_leaves_the_axis_flat_but_recorded():
    updated, block = _resolve(_decision(), 9, ReasoningEffortConfig(enabled=False))

    assert updated.dev.reasoning_effort is None
    assert block["applied"] is False
    assert block["reason"] == "disabled_by_config"
    assert block["enabled"] is False


def test_no_numeric_score_records_the_static_fallback():
    updated, block = _resolve(_decision(), None)

    assert updated.dev.reasoning_effort is None
    assert block["applied"] is False
    assert block["reason"] == "no_numeric_score_static_band_routing"
    assert set(block["phases"]) == {"plan", "dev", "review"}
    assert block["phases"]["dev"]["applied"] is False
