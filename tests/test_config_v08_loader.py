"""Tests for the v0.8 loader path (models: + overrides:)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.config import load_config

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
