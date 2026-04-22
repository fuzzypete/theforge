"""Tests for numeric complexity scoring (1-10) emitted by preflight.

Covers: parser bounds, determinism, compat-shim mapping to the legacy enum,
and at least one consumer (dev-tier routing) producing a decision distinct
from what the three-level enum would have produced.
"""

from __future__ import annotations

from pathlib import Path

from theforge.config import (
    DEFAULT_VALIDATION,
    MODEL_REGISTRY,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.types import PlanConfig
from theforge.coordinator.preflight import (
    COMPLEXITY_SCORE_MAX,
    COMPLEXITY_SCORE_MIN,
    _apply_complexity_adaptation,
    _parse_preflight_complexity_score,
    band_to_score,
    score_to_band,
    score_to_dev_tier,
)

_GOLD_POOL = ["claude/sonnet", "openai/gpt-5.4", "claude/opus"]


def _cost_rank(cli: str | None, model: str) -> int:
    for info in MODEL_REGISTRY.values():
        if info.cli == cli and info.model == model:
            return info.cost_rank
    return 2


def _make_config(tmp_path: Path) -> ForgeConfig:
    dev = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    gpt = ModelProfile(
        name="openai-gpt-5.4",
        cli="codex",
        model="gpt-5.4",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    opus = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    synth = ModelProfile(
        name="synthesis",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    plan = PlanConfig(cli="claude", model="sonnet", budget_usd=0.5, timeout=600)
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=dev,
        preflight_profile=dev,
        review_pool=[gpt, opus],
        synthesis_profile=synth,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        models=_GOLD_POOL,
        plan=plan,
        plan_model_is_default=True,
        dev_profile_is_default=True,
        review_pool_is_default=True,
    )


# ── score_to_band / band_to_score ─────────────────────────────────────


def test_score_to_band_bands():
    assert score_to_band(1) == "small"
    assert score_to_band(3) == "small"
    assert score_to_band(4) == "medium"
    assert score_to_band(7) == "medium"
    assert score_to_band(8) == "large"
    assert score_to_band(10) == "large"


def test_band_to_score_roundtrips_through_band():
    for band in ("small", "medium", "large"):
        assert score_to_band(band_to_score(band)) == band


# ── score_to_dev_tier ─────────────────────────────────────────────────


def test_score_to_dev_tier_distinguishes_within_medium_band():
    """Score 4 and score 7 are both 'medium' under the enum but route to
    different dev tiers under the numeric scale."""
    assert score_to_dev_tier(4) == "mid"
    assert score_to_dev_tier(7) == "strong"
    # And the legacy enum mapping would collapse both to "mid".
    assert score_to_band(4) == score_to_band(7) == "medium"


# ── parser ────────────────────────────────────────────────────────────


def _yaml_output(**fields: object) -> str:
    body = "\n".join(f"{k}: {v}" for k, v in fields.items())
    return f"```yaml\n{body}\n```"


def test_parse_score_within_range():
    for s in range(COMPLEXITY_SCORE_MIN, COMPLEXITY_SCORE_MAX + 1):
        assert _parse_preflight_complexity_score(_yaml_output(complexity_score=s)) == s


def test_parse_score_clamps_above():
    assert (
        _parse_preflight_complexity_score(_yaml_output(complexity_score=42))
        == COMPLEXITY_SCORE_MAX
    )


def test_parse_score_clamps_below():
    assert (
        _parse_preflight_complexity_score(_yaml_output(complexity_score=-3))
        == COMPLEXITY_SCORE_MIN
    )


def test_parse_score_deterministic_for_same_input():
    output = _yaml_output(complexity_score=6, complexity="medium")
    runs = {_parse_preflight_complexity_score(output) for _ in range(25)}
    assert runs == {6}


def test_parse_score_missing_falls_back_to_band_midpoint():
    output = _yaml_output(complexity="large")
    assert _parse_preflight_complexity_score(output, fallback_band="large") == 9
    assert _parse_preflight_complexity_score(output, fallback_band="medium") == 5
    assert _parse_preflight_complexity_score(output, fallback_band="small") == 2


def test_parse_score_missing_no_fallback_returns_none():
    assert _parse_preflight_complexity_score(_yaml_output(complexity="medium")) is None


def test_parse_score_malformed_yaml_returns_none():
    assert _parse_preflight_complexity_score("not yaml at all: [[[") is None


def test_parse_score_non_numeric_value():
    # Non-integer value with no fallback — the field is effectively missing.
    assert _parse_preflight_complexity_score(_yaml_output(complexity_score="huge")) is None


# ── consumer: dev-tier routing diverges from enum-only routing ────────


def test_dev_routing_diverges_between_score_and_enum(tmp_path):
    """Score 7 must route dev to 'strong' while enum 'medium' routes to 'mid'.

    This is the AC that a consumer reads the numeric field and produces a
    distinct decision from the three-level enum.
    """
    enum_only = _apply_complexity_adaptation(_make_config(tmp_path), "medium", None)
    score_based = _apply_complexity_adaptation(_make_config(tmp_path), "medium", 7)

    enum_rank = _cost_rank(enum_only.dev_profile.cli, enum_only.dev_profile.model)
    score_rank = _cost_rank(score_based.dev_profile.cli, score_based.dev_profile.model)

    assert enum_rank == 2, f"enum 'medium' should route dev to mid (rank 2), got {enum_rank}"
    assert score_rank == 3, f"score 7 should route dev to strong (rank 3), got {score_rank}"


def test_dev_routing_agrees_when_score_matches_band_center(tmp_path):
    """Score 5 (center of medium) and enum 'medium' yield the same dev tier."""
    enum_only = _apply_complexity_adaptation(_make_config(tmp_path), "medium", None)
    score_based = _apply_complexity_adaptation(_make_config(tmp_path), "medium", 5)
    assert _cost_rank(enum_only.dev_profile.cli, enum_only.dev_profile.model) == _cost_rank(
        score_based.dev_profile.cli, score_based.dev_profile.model
    )
