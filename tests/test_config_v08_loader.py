"""Tests for the v0.8 loader path (models: + overrides:)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.config import load_config
from theforge.config.profiles import override_constrains_model
from theforge.coordinator.preflight import _apply_preflight_config
from theforge.coordinator.state import CoordinatorState

# Patch check_agent_auth to always succeed so tests don't need real credentials.
_auth_ok = patch(
    "theforge.config._loaders.check_agent_auth",
    return_value=(True, ""),
)


def _write(tmp_path, text: str):
    p = tmp_path / "forge.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ── Simple mode ───────────────────────────────────────────────────────────────


def test_v08_simple_mode_produces_forgeconfig(tmp_path):
    """Single model list → ForgeConfig with dev_profile populated."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile.model == "sonnet"
    assert cfg.dev_profile.cli == "claude"
    assert cfg.models == ["claude/sonnet"]


def test_v08_simple_mode_two_models(tmp_path):
    """Two models → review pool has one entry (not dev), dev gets cheapest."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
  - claude/opus
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile.model == "sonnet"
    assert len(cfg.review_pool) == 1
    assert cfg.review_pool[0].model == "opus"


def test_v08_plan_agent_review_enabled_does_not_pin_explicit_model(tmp_path, monkeypatch):
    """With adaptive assignment, enabled-only plan_agent_review lets adaptive choose."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
  - openai/gpt-5.4-pro
  - google/gemini-3.1-pro-preview
assignment:
  enabled: true
  max_cost_per_story_usd: 100.0
plan:
  enabled: true
plan_agent_review:
  enabled: true
""",
    )
    with (
        _auth_ok,
        patch("theforge.config.load.check_agent_auth", return_value=(True, "")),
    ):
        cfg = load_config(cfg_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    assert cfg.plan_agent_review.enabled is True
    assert cfg.plan_agent_review.profiles == []

    state = CoordinatorState()
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 9
    updated = _apply_preflight_config(cfg, state)

    assert updated.plan_agent_review.pool
    assert state._adaptive_decision is not None
    assert "explicit override" not in state._adaptive_decision.rationale["plan_review"]


def test_v08_plan_agent_review_enabled_defaults_to_claude_without_adaptive_pool(tmp_path):
    """Legacy enabled-only plan_agent_review still creates a Claude reviewer."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
plan:
  enabled: true
  model: opus
plan_agent_review:
  enabled: true
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    profiles = cfg.plan_agent_review.profiles
    assert len(profiles) == 1
    assert profiles[0].cli == "claude"
    assert profiles[0].model == "sonnet"


# ── override_constrains_model ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "data",
    [
        {"timeout_seconds": 1200},
        {"timeout_medium_seconds": 1800, "timeout_large_seconds": 3600},
        {"budget_usd": 20.0},
        {"sandbox_mode": "read-only"},
        {"allowed_tools": ["Read"]},
        {},
        None,
    ],
)
def test_override_constrains_model_false_for_non_model_keys(data):
    assert override_constrains_model(data) is False


@pytest.mark.parametrize(
    "data",
    [
        {"model": "opus"},
        {"models": ["opus", "sonnet"]},
        {"fallback_models": ["sonnet"]},
        {"provider": "anthropic"},
        {"cli": "codex"},
        {"timeout_seconds": 1200, "model": "opus"},
    ],
)
def test_override_constrains_model_true_for_model_keys(data):
    assert override_constrains_model(data) is True


# ── Simple mode + overrides ───────────────────────────────────────────────────


def test_v08_overrides_dev_timeout(tmp_path):
    """overrides.dev.timeout_seconds is applied to the derived dev profile."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
overrides:
  dev:
    timeout_seconds: 1200
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile.timeout_seconds == 1200


def test_v08_timeout_only_dev_override_keeps_routing_active(tmp_path):
    """A resource-only overrides.dev (timeouts) must not pin the dev model.

    Regression for #1764: raising the dev timeout is a statement about budgets,
    not model selection, so complexity-aware dev routing stays active.
    """
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
  - claude/opus
overrides:
  dev:
    timeout_medium_seconds: 1800
    timeout_large_seconds: 3600
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile_is_default is True
    # Operator's timeout override is still applied to the derived dev profile.
    assert cfg.dev_profile.timeout_medium_seconds == 1800
    assert cfg.dev_profile.timeout_large_seconds == 3600


def test_v08_model_dev_override_pins_routing(tmp_path):
    """A model-constraining overrides.dev pins the role and disables routing."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
  - claude/opus
overrides:
  dev:
    model: opus
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile_is_default is False
    assert cfg.dev_profile.model == "opus"


def test_v08_timeout_only_dev_override_routes_and_preserves_timeout_seam(tmp_path):
    """Seam: load → complexity adaptation with a timeout-only dev override.

    A large story must route dev to the strong model (routing active) while the
    operator's timeout override survives the model reassignment (#1764).
    """
    from theforge.coordinator.preflight import _apply_complexity_adaptation

    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
  - claude/opus
overrides:
  dev:
    timeout_large_seconds: 3600
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    # Static derivation picks the cheapest dev-capable model.
    assert cfg.dev_profile.model == "sonnet"

    updated = _apply_complexity_adaptation(cfg, "large", complexity_score=10)

    # Routing stayed active → hardest story routes to the strong model...
    assert updated.dev_profile.model == "opus"
    # ...and the operator's timeout override is preserved across the swap.
    assert updated.dev_profile.timeout_large_seconds == 3600


def test_v08_model_dev_override_bypasses_routing_seam(tmp_path):
    """Seam: a pinned dev model is not rewritten by complexity adaptation."""
    from theforge.coordinator.preflight import _apply_complexity_adaptation

    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
  - claude/opus
overrides:
  dev:
    model: sonnet
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    updated = _apply_complexity_adaptation(cfg, "large", complexity_score=10)

    # Pinned model survives — routing did not upgrade it to opus.
    assert updated.dev_profile.model == "sonnet"


def test_v08_overrides_preflight_timeout(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
overrides:
  preflight:
    timeout_seconds: 999
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    assert cfg.preflight_profile.timeout_seconds == 999


# ── Round-trip / no phantom models ───────────────────────────────────────────


def test_v08_round_trip_no_extra_models(tmp_path):
    """Bridge must not inject extra models beyond what was in the input list."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    all_models = {cfg.dev_profile.model, cfg.preflight_profile.model} | {
        p.model for p in cfg.review_pool
    }
    # Only "sonnet" (from claude/sonnet) should appear; no phantom models injected.
    assert all_models == {"sonnet"}


def test_v08_round_trip_two_models_review_pool_matches_input(tmp_path):
    """Two-model config: review_pool models are a subset of the declared models."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
  - claude/opus
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)

    declared = {"sonnet", "opus"}
    pool_models = {p.model for p in cfg.review_pool}
    assert pool_models <= declared


# ── Legacy key rejection ──────────────────────────────────────────────────────


def test_v08_rejects_legacy_par_scalar_model_field(tmp_path):
    """plan_agent_review.model is a legacy scalar — rejected alongside models:."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
plan_agent_review:
  enabled: true
  model: opus
""",
    )
    with _auth_ok, pytest.raises(ValueError, match="plan_agent_review"):
        load_config(cfg_path)


def test_v08_rejects_legacy_par_scalar_budget_field(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
plan_agent_review:
  enabled: true
  budget_usd: 0.5
""",
    )
    with _auth_ok, pytest.raises(ValueError, match="budget_usd"):
        load_config(cfg_path)


def test_v08_rejects_legacy_profiles_alongside_models(tmp_path):
    """Removed legacy 'profiles:' key alongside 'models:' must fail fast."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
profiles:
  dev:
    model: sonnet
""",
    )
    with _auth_ok, pytest.raises(ValueError, match="profiles"):
        load_config(cfg_path)


def test_v08_rejects_smart_config_models_alongside_models(tmp_path):
    """Removed legacy 'smart_config_models:' key alongside 'models:' must fail fast."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
smart_config_models:
  - claude/opus
""",
    )
    with _auth_ok, pytest.raises(ValueError, match="smart_config_models"):
        load_config(cfg_path)


def test_v08_rejects_top_level_agents_alongside_models(tmp_path):
    """Removed legacy top-level 'agents:' key alongside 'models:' must fail fast."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
agents:
  - name: foo
    model: opus
""",
    )
    with _auth_ok, pytest.raises(ValueError, match="agents"):
        load_config(cfg_path)


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_v08_empty_overrides_key_does_not_crash(tmp_path):
    """overrides: with null/empty value must not raise TypeError."""
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
overrides:
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)
    # Overrides were empty — dev profile should use bridge defaults unchanged.
    assert cfg.dev_profile.model == "sonnet"


def test_v08_overrides_par_with_explicit_par_section_no_pool(tmp_path):
    """overrides.plan_agent_review + explicit plan_agent_review: (no pool) → derived injected.

    enabled: false avoids the planner-model independence check so we can test
    pool injection in isolation without needing a second distinct model.
    """
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
overrides:
  plan_agent_review:
    timeout_seconds: 999
plan_agent_review:
  enabled: false
""",
    )
    with _auth_ok:
        cfg = load_config(cfg_path)
    # The derived profile from overrides must be injected — not silently dropped.
    assert len(cfg.plan_agent_review.pool) == 1
    assert cfg.plan_agent_review.pool[0].timeout_seconds == 999
