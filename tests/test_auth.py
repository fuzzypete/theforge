"""Tests for the unified agent_is_ready() auth resolution function."""

from __future__ import annotations

from unittest.mock import patch

from theforge.config import ModelProfile
from theforge.config.auth import agent_is_ready


def _profile(**kwargs) -> ModelProfile:
    """Build a minimal ModelProfile for test use."""
    defaults = dict(
        name="test",
        model="test-model",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
        cli=None,
        provider=None,
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


# ── CLI agents ─────────────────────────────────────────────────────────


def test_claude_cli_ready_when_binary_found():
    profile = _profile(cli="claude")
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        ready, reason = agent_is_ready(profile, {})
    assert ready is True
    assert reason == ""


def test_claude_cli_not_ready_when_binary_missing():
    profile = _profile(cli="claude")
    with patch("shutil.which", return_value=None):
        ready, reason = agent_is_ready(profile, {})
    assert ready is False
    assert "claude" in reason
    assert "PATH" in reason


def test_codex_cli_uses_npx():
    """codex uses npx @openai/codex, not a codex binary."""
    profile = _profile(cli="codex")
    with patch("shutil.which", side_effect=lambda cmd: "/usr/bin/npx" if cmd == "npx" else None):
        ready, reason = agent_is_ready(profile, {})
    assert ready is True
    assert reason == ""


def test_gemini_cli_uses_npx():
    """gemini uses npx @google/gemini-cli, not a gemini binary."""
    profile = _profile(cli="gemini")
    with patch("shutil.which", side_effect=lambda cmd: "/usr/bin/npx" if cmd == "npx" else None):
        ready, reason = agent_is_ready(profile, {})
    assert ready is True
    assert reason == ""


def test_codex_not_ready_when_npx_missing():
    profile = _profile(cli="codex")
    with patch("shutil.which", return_value=None):
        ready, reason = agent_is_ready(profile, {})
    assert ready is False
    assert "npx" in reason


def test_gemini_not_ready_when_npx_missing():
    profile = _profile(cli="gemini")
    with patch("shutil.which", return_value=None):
        ready, reason = agent_is_ready(profile, {})
    assert ready is False
    assert "npx" in reason


def test_unsupported_cli_returns_false_not_key_error():
    """Unsupported CLI value returns clear error, not KeyError."""
    profile = _profile(cli="unknown-cli")
    ready, reason = agent_is_ready(profile, {})
    assert ready is False
    assert "unsupported" in reason.lower()
    assert "unknown-cli" in reason


# ── API agents ─────────────────────────────────────────────────────────


def test_api_agent_ready_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    profile = _profile(provider="anthropic")
    ready, reason = agent_is_ready(profile, {})
    assert ready is True


def test_api_agent_ready_from_secrets(monkeypatch):
    """Key in secrets satisfies auth even when not in os.environ."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(provider="anthropic")
    ready, reason = agent_is_ready(profile, {"ANTHROPIC_API_KEY": "sk-from-secrets"})
    assert ready is True


def test_secrets_take_precedence_over_empty_env(monkeypatch):
    """Secrets override an absent env var (matching runtime adapter merge order)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    profile = _profile(provider="openai")
    ready, reason = agent_is_ready(profile, {"OPENAI_API_KEY": "sk-secret"})
    assert ready is True


def test_api_agent_not_ready_when_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = _profile(provider="anthropic")
    ready, reason = agent_is_ready(profile, {})
    assert ready is False
    assert "ANTHROPIC_API_KEY" in reason


def test_deepseek_auth_check(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    profile = _profile(provider="deepseek")
    ready, reason = agent_is_ready(profile, {})
    assert ready is True


# ── Google provider — GEMINI_API_KEY fallback ──────────────────────────


def test_google_ready_from_google_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "gk-test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _profile(provider="google")
    ready, reason = agent_is_ready(profile, {})
    assert ready is True


def test_google_ready_from_gemini_api_key_fallback(monkeypatch):
    """GEMINI_API_KEY satisfies Google auth when GOOGLE_API_KEY is absent."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gm-test")
    profile = _profile(provider="google")
    ready, reason = agent_is_ready(profile, {})
    assert ready is True


def test_google_ready_from_gemini_key_in_secrets(monkeypatch):
    """GEMINI_API_KEY in secrets satisfies Google auth."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _profile(provider="google")
    ready, reason = agent_is_ready(profile, {"GEMINI_API_KEY": "gm-secret"})
    assert ready is True


def test_google_not_ready_when_both_keys_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _profile(provider="google")
    ready, reason = agent_is_ready(profile, {})
    assert ready is False
    assert "GOOGLE_API_KEY" in reason
    assert "GEMINI_API_KEY" in reason


# ── Local endpoints skip API key check ────────────────────────────────


def test_localhost_skips_key_check(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    profile = _profile(provider="openai", base_url="http://localhost:11434")
    ready, reason = agent_is_ready(profile, {})
    assert ready is True


def test_127_0_0_1_skips_key_check(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    profile = _profile(provider="deepseek", base_url="http://127.0.0.1:8080/v1")
    ready, reason = agent_is_ready(profile, {})
    assert ready is True


def test_remote_endpoint_still_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    profile = _profile(provider="openai", base_url="https://api.openai.com/v1")
    ready, reason = agent_is_ready(profile, {})
    assert ready is False


# ── Unsupported provider ───────────────────────────────────────────────


def test_unsupported_provider_returns_false_not_key_error():
    """Unsupported provider value returns clear error, not KeyError."""
    profile = _profile(provider="unknown-provider")
    ready, reason = agent_is_ready(profile, {})
    assert ready is False
    assert "unsupported" in reason.lower()
    assert "unknown-provider" in reason


# ── No cli, no provider ────────────────────────────────────────────────


def test_no_cli_no_provider_is_ready():
    """Profile with neither cli nor provider (edge case) is considered ready."""
    profile = _profile()
    ready, reason = agent_is_ready(profile, {})
    assert ready is True
    assert reason == ""


# ── Secrets merge order matches runtime adapters ───────────────────────


def test_secrets_override_env(monkeypatch):
    """Secrets take precedence over os.environ (matching {**os.environ, **secrets})."""
    # Put a placeholder in env — secrets should take precedence and provide the real key
    monkeypatch.setenv("OPENAI_API_KEY", "")  # empty string = falsy
    profile = _profile(provider="openai")
    # Empty env value + no secrets → not ready
    ready, _ = agent_is_ready(profile, {})
    assert ready is False
    # Non-empty secrets → ready
    ready, _ = agent_is_ready(profile, {"OPENAI_API_KEY": "sk-from-secrets"})
    assert ready is True
