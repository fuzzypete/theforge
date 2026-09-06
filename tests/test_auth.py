"""Tests for src/theforge/config/auth.py — unified auth resolution."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.config import ModelProfile
from theforge.config.auth import (
    check_agent_auth,
    sandbox_available_for_profile,
    sandbox_containment_mode,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _cli_profile(cli: str, name: str = "test") -> ModelProfile:
    return ModelProfile(
        name=name,
        cli=cli,
        provider=None,
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )


def _api_profile(
    provider: str,
    name: str = "test",
    base_url: str | None = None,
) -> ModelProfile:
    return ModelProfile(
        name=name,
        cli=None,
        provider=provider,
        model="model-name",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
        base_url=base_url,
    )


def _neither_profile() -> ModelProfile:
    """Profile with neither cli nor provider — should raise ValueError."""
    return ModelProfile(
        name="bad",
        cli=None,
        provider=None,
        model="x",
        budget_usd=0.0,
        timeout_seconds=0,
        allowed_tools=(),
    )


# ── CLI profiles ───────────────────────────────────────────────────────


def test_cli_claude_binary_present():
    with patch("theforge.config.auth.shutil.which", return_value="/usr/bin/claude"):
        ok, _ = check_agent_auth(_cli_profile("claude"))
        assert ok


def test_cli_claude_binary_missing():
    with patch("theforge.config.auth.shutil.which", return_value=None):
        ok, reason = check_agent_auth(_cli_profile("claude"))
        assert not ok
        assert reason


def test_cli_codex_checks_npx_not_binary():
    """codex CLI should check for npx, not a 'codex' binary."""
    calls: list[str] = []

    def _which(name: str) -> str | None:
        calls.append(name)
        return "/usr/bin/npx" if name == "npx" else None

    with patch("theforge.config.auth.shutil.which", side_effect=_which):
        result = check_agent_auth(_cli_profile("codex"))

    ok, _ = result
    assert ok
    assert "npx" in calls
    assert "codex" not in calls


def test_cli_gemini_checks_npx_not_binary():
    """gemini CLI should check for npx, not a 'gemini' binary."""
    calls: list[str] = []

    def _which(name: str) -> str | None:
        calls.append(name)
        return "/usr/bin/npx" if name == "npx" else None

    with patch("theforge.config.auth.shutil.which", side_effect=_which):
        result = check_agent_auth(_cli_profile("gemini"))

    ok, _ = result
    assert ok
    assert "npx" in calls
    assert "gemini" not in calls


def test_cli_npx_missing_returns_false():
    with patch("theforge.config.auth.shutil.which", return_value=None):
        ok1, reason1 = check_agent_auth(_cli_profile("codex"))
        assert not ok1
        assert reason1
        ok2, reason2 = check_agent_auth(_cli_profile("gemini"))
        assert not ok2
        assert reason2


def test_cli_unsupported_raises_value_error():
    with pytest.raises(ValueError, match="unsupported CLI"):
        check_agent_auth(_cli_profile("unknown-cli"))


# ── API profiles — local endpoints ────────────────────────────────────


def test_api_local_endpoint_skips_key_openai():
    """openai with localhost base_url should not require an API key."""
    profile = _api_profile("openai", base_url="http://localhost:11434")
    ok, _ = check_agent_auth(profile, {})
    assert ok


def test_api_local_endpoint_skips_key_deepseek():
    profile = _api_profile("deepseek", base_url="http://127.0.0.1:8080")
    ok, _ = check_agent_auth(profile, {})
    assert ok


def test_api_google_local_endpoint_still_requires_key(monkeypatch):
    """google does not support local endpoints — key is always required."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _api_profile("google", base_url="http://localhost:11434")
    ok, reason = check_agent_auth(profile, {})
    assert not ok
    assert reason


# ── API profiles — Google key resolution ──────────────────────────────


def test_api_google_key_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "real-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ok, _ = check_agent_auth(_api_profile("google"), {})
    assert ok


def test_api_gemini_fallback_from_env(monkeypatch):
    """GEMINI_API_KEY should be accepted when GOOGLE_API_KEY is absent."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "real-key")
    ok, _ = check_agent_auth(_api_profile("google"), {})
    assert ok


def test_api_google_key_from_secrets(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ok, _ = check_agent_auth(_api_profile("google"), {"GOOGLE_API_KEY": "secret-key"})
    assert ok


def test_api_gemini_key_from_secrets(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ok, _ = check_agent_auth(_api_profile("google"), {"GEMINI_API_KEY": "secret-key"})
    assert ok


def test_api_neither_google_key_returns_false(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ok, reason = check_agent_auth(_api_profile("google"), {})
    assert not ok
    assert reason


# ── API profiles — secrets merge ──────────────────────────────────────


def test_api_secrets_merge_key_in_secrets_not_env(monkeypatch):
    """Auth should pass when key is only in secrets, not in os.environ."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ok, _ = check_agent_auth(_api_profile("openai"), {"OPENAI_API_KEY": "s3cret"})
    assert ok


def test_api_secrets_env_takes_precedence(monkeypatch):
    """Key present in env should still work even if absent in secrets."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    ok, _ = check_agent_auth(_api_profile("anthropic"), {})
    assert ok


def test_api_no_key_at_all_returns_false(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ok, reason = check_agent_auth(_api_profile("openai"), {})
    assert not ok
    assert reason


# ── API profiles — unsupported provider ───────────────────────────────


def test_api_unsupported_provider_raises_value_error():
    with pytest.raises(ValueError, match="unsupported provider"):
        check_agent_auth(_api_profile("unknown-provider"))


# ── Neither cli nor provider ───────────────────────────────────────────


def test_neither_cli_nor_provider_raises_value_error():
    with pytest.raises(ValueError, match="neither 'cli' nor 'provider'"):
        check_agent_auth(_neither_profile())


# ── synthesis_profile ─────────────────────────────────────────────────


def test_synthesis_profile_works_like_any_api_profile(monkeypatch):
    """synthesis_profile has no special casing — it's just a ModelProfile."""
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    synth = ModelProfile(
        name="synthesis",
        cli=None,
        provider="openai",
        model="gpt-4o",
        budget_usd=2.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    ok, _ = check_agent_auth(synth, {})
    assert ok


def test_synthesis_profile_missing_key_returns_false(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    synth = ModelProfile(
        name="synthesis",
        cli=None,
        provider="openai",
        model="gpt-4o",
        budget_usd=2.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    ok, reason = check_agent_auth(synth, {})
    assert not ok
    assert reason


def test_cli_binary_outside_system_paths_is_still_ready_when_present():
    with patch("theforge.config.auth.shutil.which", return_value="/home/user/.local/bin/claude"):
        ok, reason = check_agent_auth(_cli_profile("claude"))
    assert ok
    assert reason == ""


def test_api_bash_profile_checks_workspace_sandbox_readiness(monkeypatch):
    profile = ModelProfile(
        name="api-dev",
        provider="openai",
        model="gpt-4o",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("bash",),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    with patch(
        "theforge.runners.sandbox.workspace_effect_sandbox_command",
        return_value=["sandbox-exec", "-p", "profile", "true"],
    ):
        ok, reason = check_agent_auth(profile, {})
    assert ok is True
    assert reason == ""


def test_cli_bash_profile_does_not_report_workspace_sandbox_readiness():
    profile = ModelProfile(
        name="cli-dev",
        cli="claude",
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("bash",),
    )
    with patch("theforge.config.auth.shutil.which", return_value="/usr/bin/claude"):
        ok, reason = check_agent_auth(profile, {})
    assert ok is True
    assert reason == ""


def test_cli_profile_can_skip_sandbox_readiness_for_auth_only() -> None:
    profile = ModelProfile(
        name="cli-dev",
        cli="codex",
        model="gpt-5.4",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("bash",),
    )
    with patch("theforge.config.auth.shutil.which", return_value="/usr/bin/npx"):
        ok, reason = check_agent_auth(profile, {}, include_sandbox_readiness=False)
    assert ok is True
    assert reason == ""


# ── Mechanical containment readiness (#1907) ──────────────────────────


def _sandbox_cli_profile(cli: str, sandbox_mode: str = "workspace-write") -> ModelProfile:
    return ModelProfile(
        name=f"{cli}-dev",
        cli=cli,
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Edit", "Write", "Bash"),
        sandbox_mode=sandbox_mode,
    )


def test_claude_cli_readiness_follows_host_wrapper_available() -> None:
    """Claude CLI is only 'mechanically sandboxed' when the host wrapper exists."""
    profile = _sandbox_cli_profile("claude")
    with patch("theforge.config.auth._host_sandbox_available", return_value=True):
        assert sandbox_available_for_profile(profile) is True
    with patch("theforge.config.auth._host_sandbox_available", return_value=False):
        assert sandbox_available_for_profile(profile) is False


def test_gemini_cli_readiness_follows_host_wrapper_available() -> None:
    profile = _sandbox_cli_profile("gemini")
    with patch("theforge.config.auth._host_sandbox_available", return_value=False):
        assert sandbox_available_for_profile(profile) is False


def test_claude_cli_sandbox_mode_none_is_not_mechanical() -> None:
    """sandbox_mode=none must never report mechanical containment, wrapper or not."""
    profile = _sandbox_cli_profile("claude", sandbox_mode="none")
    with patch("theforge.config.auth._host_sandbox_available", return_value=True):
        assert sandbox_available_for_profile(profile) is False


def test_codex_cli_is_mechanical_without_host_wrapper() -> None:
    """Codex asserts a provider-native --sandbox flag, so it stays mechanical."""
    profile = _sandbox_cli_profile("codex")
    with patch("theforge.config.auth._host_sandbox_available", return_value=False):
        assert sandbox_available_for_profile(profile) is True


def test_containment_mode_classifications() -> None:
    claude = _sandbox_cli_profile("claude")
    with patch("theforge.config.auth._host_sandbox_available", return_value=True):
        assert sandbox_containment_mode(claude) == "mechanical"
    with patch("theforge.config.auth._host_sandbox_available", return_value=False):
        assert sandbox_containment_mode(claude) == "unavailable"
    assert sandbox_containment_mode(_sandbox_cli_profile("claude", "none")) == "none"
    assert sandbox_containment_mode(_sandbox_cli_profile("codex")) == "native"


def test_cli_readiness_stays_a_bool_for_dispatch_gates():
    """CLI readiness is True/False — never a third value (#2909).

    ``check_agent_auth`` is a dispatch gate as well as a report input: the
    sprint launch gate, adaptive assignment and config load all read its first
    element as a boolean. check-config's third "unverified" state is derived in
    the reporting layer precisely so widening this contract cannot silently
    make every CLI profile read as not-ready at those call sites.
    """
    with patch("theforge.config.auth.shutil.which", return_value="/usr/bin/npx"):
        for cli in ("codex", "gemini"):
            ready, reason = check_agent_auth(_cli_profile(cli))
            assert ready is True
            assert reason == ""
