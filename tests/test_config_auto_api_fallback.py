from __future__ import annotations

from unittest.mock import patch

from theforge.config import load_config
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


def test_sprint_warning_mentions_tracked_api_fallback(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
models:
  - openai/gpt-5.4
""",
    )
    with _auth_ok, _import_ok:
        cfg = load_config(cfg_path)

    warnings = _agent_cost_tracking_warnings(cfg)
    assert any(
        "API fallback to openai/gpt-5.4 will be tracked if it triggers" in warning
        for warning in warnings
    )
