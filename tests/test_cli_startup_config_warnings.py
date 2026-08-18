"""Tests for startup config warnings + structural-error exit code.

Covers ``theforge.cli.shared.load_config_checked`` and its helpers, which run
after ``load_config`` succeeds to (a) surface per-profile auth warnings on
stderr before the coordinator state machine starts and (b) convert structural
config errors into exit code 2 rather than a generic exit-1 crash.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from theforge.cli import shared
from theforge.cli.substrate import Substrate
from theforge.config import ModelProfile


def _api_profile(name: str, provider: str = "anthropic") -> ModelProfile:
    return ModelProfile(
        name=name,
        cli=None,
        provider=provider,
        model="model-x",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )


def _cli_profile(name: str, cli: str = "claude") -> ModelProfile:
    return ModelProfile(
        name=name,
        cli=cli,
        provider=None,
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )


def _fake_config(**overrides) -> SimpleNamespace:
    """A minimal stand-in exposing only the attributes the warning code reads."""
    base = dict(
        dev_profile=_api_profile("dev"),
        preflight_profile=_api_profile("preflight"),
        preflight_fallback_profile=None,
        review_pool=[_api_profile("review")],
        synthesis_profile=None,
        agents=[],
        secrets={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _installed_substrate(**overrides) -> Substrate:
    base = dict(
        binary="/Users/me/.local/bin/forge",
        package_file="/tmp/rc16/site-packages/theforge/__init__.py",
        version="0.13.0rc16",
        editable=False,
        source_root=None,
        git_ref=None,
    )
    base.update(overrides)
    return Substrate(**base)


# ── Startup auth warnings ───────────────────────────────────────────────


def test_missing_api_key_emits_warning_per_profile(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _fake_config()

    shared._print_startup_auth_warnings(config)

    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY not set" in err
    # dev, preflight, and review are all anthropic API profiles missing the key.
    assert err.count("ANTHROPIC_API_KEY not set") == 3
    assert "dev profile 'dev'" in err
    assert "preflight profile 'preflight'" in err
    assert "review profile 'review'" in err


def test_ready_profiles_emit_no_warning(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-present")
    config = _fake_config()

    shared._print_startup_auth_warnings(config)

    assert capsys.readouterr().err == ""


def test_secrets_dict_resolves_credentials(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _fake_config(secrets={"ANTHROPIC_API_KEY": "sk-from-secrets"})

    shared._print_startup_auth_warnings(config)

    assert capsys.readouterr().err == ""


def test_duplicate_name_and_reason_deduped(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Two reviewers sharing the same name+reason should warn only once.
    config = _fake_config(review_pool=[_api_profile("rev"), _api_profile("rev")])

    shared._print_startup_auth_warnings(config)

    err = capsys.readouterr().err
    assert err.count("profile 'rev'") == 1


def test_agent_pool_and_synthesis_profiles_checked(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")  # ensure unset for openai agent

    agent = SimpleNamespace(to_model_profile=lambda: _api_profile("pool-a", provider="openai"))
    config = _fake_config(
        synthesis_profile=_api_profile("synth"),
        agents=[agent],
    )

    shared._print_startup_auth_warnings(config)

    err = capsys.readouterr().err
    assert "synthesis profile 'synth'" in err
    assert "agent-pool profile 'pool-a'" in err
    assert "OPENAI_API_KEY not set" in err


def test_sandbox_readiness_excluded(monkeypatch, capsys):
    """Only credential/binary readiness is reported — not host sandbox state."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-present")
    # An API profile that is auth-ready must not warn even if the host lacks a
    # workspace sandbox; the warning surface passes include_sandbox_readiness=False.
    config = _fake_config(
        dev_profile=_api_profile("dev"),
        preflight_profile=_api_profile("preflight"),
        review_pool=[_api_profile("review")],
    )

    with patch("theforge.cli.shared.check_agent_auth") as mock_check:
        mock_check.return_value = (True, "")
        shared._print_startup_auth_warnings(config)

    # Every call must opt out of sandbox readiness.
    assert mock_check.call_count > 0
    for call in mock_check.call_args_list:
        assert call.kwargs["include_sandbox_readiness"] is False
    assert capsys.readouterr().err == ""


# ── load_config_checked: structural errors + non-blocking warnings ──────


def test_structural_error_exits_code_2(capsys):
    with patch("theforge.cli.shared.load_config", side_effect=ValueError("bad key")):
        with pytest.raises(SystemExit) as exc_info:
            shared.load_config_checked("/nonexistent/forge.yaml")

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "forge.yaml is invalid" in err
    assert "bad key" in err


def test_structural_error_includes_stale_runtime_provenance(tmp_path, capsys):
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "theforge"\nversion = "0.15.0rc3"\n',
        encoding="utf-8",
    )
    err_msg = (
        "Unknown model 'anthropic/sonnet/cli': not in AGENT_REGISTRY. "
        "Known models: ['claude/opus']"
    )

    with (
        patch("theforge.cli.shared.load_config", side_effect=ValueError(err_msg)),
        patch(
            "theforge.cli.substrate.detect_substrate",
            return_value=_installed_substrate(),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            shared.load_config_checked(config_path)

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    expected_schema = str(
        Path("/tmp/rc16/site-packages/theforge/config/model_catalog.py").resolve()
    )
    expected_catalog = str(
        Path("/tmp/rc16/site-packages/theforge/config/data/models.yaml").resolve()
    )
    assert err_msg in err
    assert "Runtime binary:  /Users/me/.local/bin/forge" in err
    assert "Runtime package: /tmp/rc16/site-packages/theforge/__init__.py" in err
    assert f"Runtime schema:  {expected_schema}" in err
    assert f"Runtime catalog: {expected_catalog}" in err
    assert f"Checkout root:   {tmp_path}" in err
    assert "Checkout version: 0.15.0rc3" in err
    assert "appears ahead of it" in err


def test_unclassifiable_profile_is_skipped_not_fatal(monkeypatch, capsys):
    # A profile with neither cli nor provider makes check_agent_auth raise
    # ValueError. Because a genuinely malformed profile is already rejected by
    # load_config, the warning pass treats this as best-effort: the bad profile
    # is skipped, well-formed profiles are still checked, and the run proceeds.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bad = ModelProfile(
        name="bad",
        cli=None,
        provider=None,
        model="x",
        budget_usd=0.0,
        timeout_seconds=0,
        allowed_tools=(),
    )
    config = _fake_config(dev_profile=bad)

    with patch("theforge.cli.shared.load_config", return_value=config):
        result = shared.load_config_checked("/some/forge.yaml")

    assert result is config
    err = capsys.readouterr().err
    # The bad profile did not crash the run; the well-formed review profile is
    # still auth-checked and reported.
    assert "review profile 'review'" in err
    assert "profile 'bad'" not in err


def test_warnings_do_not_block_load(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _fake_config()

    with patch("theforge.cli.shared.load_config", return_value=config):
        result = shared.load_config_checked("/some/forge.yaml")

    # Config is still returned despite the missing-credential warnings.
    assert result is config
    assert "ANTHROPIC_API_KEY not set" in capsys.readouterr().err


def test_success_no_warnings_returns_config(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-present")
    config = _fake_config()

    with patch("theforge.cli.shared.load_config", return_value=config):
        result = shared.load_config_checked("/some/forge.yaml")

    assert result is config
    assert capsys.readouterr().err == ""
