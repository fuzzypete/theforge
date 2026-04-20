from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.config import load_config
from theforge.config.types import PlanConfig
from theforge.sprint.runner import _agent_cost_tracking_warnings


def _write(tmp_path, text: str):
    p = tmp_path / "forge.yaml"
    p.write_text(text, encoding="utf-8")
    return p


_auth_ok = patch("theforge.config.load.check_agent_auth", return_value=(True, ""))
_import_ok = patch("importlib.import_module")


def test_cli_model_auto_pairs_same_provider_api_fallback(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - openai/gpt-5.4
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile.cli == "codex"
    assert cfg.dev_profile.api_fallback is not None
    assert cfg.dev_profile.api_fallback.provider == "openai"
    assert cfg.dev_profile.api_fallback.model == "gpt-5.4"


def test_cli_model_without_matching_api_sibling_does_not_auto_pair(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - claude/sonnet
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile.cli == "claude"
    assert cfg.dev_profile.api_fallback is None


def test_auto_api_fallback_false_disables_auto_pairing(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
auto_api_fallback: false
models:
  - openai/gpt-5.4
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    assert cfg.auto_api_fallback is False
    assert cfg.dev_profile.api_fallback is None


def test_explicit_provider_fallback_wins_over_auto_pairing(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - openai/gpt-5.4
provider_fallbacks:
  openai:
    model: o4-mini
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile.api_fallback is not None
    assert cfg.dev_profile.api_fallback.provider == "openai"
    assert cfg.dev_profile.api_fallback.model == "o4-mini"


def test_multiple_cli_models_same_provider_do_not_cross_wire_auto_fallbacks(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - openai/gpt-5.4
  - openai/gpt-5.4-mini
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    assert cfg.dev_profile.cli == "codex"
    assert cfg.dev_profile.model in {"gpt-5.4", "gpt-5.4-mini"}
    assert cfg.dev_profile.api_fallback is None
    assert all(profile.api_fallback is None for profile in cfg.review_pool)


def test_plan_cli_model_receives_auto_api_fallback(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - openai/gpt-5.4
plan:
  enabled: true
  cli: codex
  model: gpt-5.4
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    assert isinstance(cfg.plan, PlanConfig)
    assert cfg.plan.api_fallback is not None
    assert cfg.plan.api_fallback.provider == "openai"
    assert cfg.plan.api_fallback.model == "gpt-5.4"


def test_legacy_plan_agent_review_cli_receives_auto_api_fallback(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - openai/gpt-5.4
plan:
  enabled: true
  cli: claude
  model: sonnet
plan_agent_review:
  enabled: true
  cli: codex
  model: gpt-5.4
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    profiles = cfg.plan_agent_review.profiles
    assert len(profiles) == 1
    assert profiles[0].api_fallback is not None
    assert profiles[0].api_fallback.provider == "openai"
    assert profiles[0].api_fallback.model == "gpt-5.4"


def test_sprint_warning_mentions_tracked_api_fallback(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - openai/gpt-5.4
plan:
  enabled: true
  cli: codex
  model: gpt-5.4
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    warnings = _agent_cost_tracking_warnings(cfg)
    assert any(
        "API fallback to openai/gpt-5.4 will be tracked if it triggers" in warning
        for warning in warnings
    )
    assert any("planner" in warning for warning in warnings)


def test_models_null_still_raises_clean_schema_error_with_legacy_plan_agent_review(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
plan_agent_review:
  enabled: true
  cli: codex
  model: gpt-5.4
""",
    )
    with (
        _auth_ok,
        _import_ok,
        pytest.raises(ValueError, match="'models' must be a non-empty list"),
    ):
        load_config(cfg_path)
