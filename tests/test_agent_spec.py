"""Tests for AgentSpec / TransportSpec: registry shape, role derivation,
and runner dispatch on transport.kind."""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.config import (
    AGENT_REGISTRY,
    MODEL_REGISTRY,
    AgentSpec,
    ModelProfile,
    TransportSpec,
    resolve_agent_spec,
)
from theforge.config.role_derivation import derive_roles
from theforge.runners.cli import _profile_transport_kind


class TestTransportSpec:
    def test_cli_requires_executable(self):
        with pytest.raises(ValueError, match="requires an executable"):
            TransportSpec(kind="cli", runner="claude")

    def test_api_forbids_executable(self):
        with pytest.raises(ValueError, match="must not set an executable"):
            TransportSpec(kind="api", runner="openai", executable="oai")

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="must be 'cli' or 'api'"):
            TransportSpec(kind="local", runner="x", executable="x")

    def test_cli_transport_valid(self):
        t = TransportSpec(kind="cli", runner="claude", executable="claude")
        assert t.kind == "cli"
        assert t.executable == "claude"

    def test_api_transport_valid(self):
        t = TransportSpec(kind="api", runner="deepseek")
        assert t.kind == "api"
        assert t.executable is None


class TestAgentRegistry:
    def test_every_entry_has_explicit_transport(self):
        for key, spec in AGENT_REGISTRY.items():
            assert isinstance(spec, AgentSpec), key
            assert isinstance(spec.transport, TransportSpec), key

    def test_cli_backed_models_resolve_to_cli_transport(self):
        for key in ("claude/sonnet", "claude/opus", "openai/gpt-5.4", "google/gemini-2.5-pro"):
            assert AGENT_REGISTRY[key].transport.kind == "cli"

    def test_api_backed_models_resolve_to_api_transport(self):
        for key in ("deepseek/deepseek-reasoner", "deepseek/deepseek-chat"):
            assert AGENT_REGISTRY[key].transport.kind == "api"

    def test_deepseek_openai_google_and_all_cli_models_expressible(self):
        """AC: DeepSeek, OpenAI, Google, and CLI-backed models are all expressible."""
        providers = {s.provider for s in AGENT_REGISTRY.values()}
        assert {"anthropic", "openai", "google", "deepseek"} <= providers

    def test_model_registry_is_derived_from_agent_registry(self):
        assert set(MODEL_REGISTRY) == set(AGENT_REGISTRY)

    def test_resolve_agent_spec_known(self):
        spec = resolve_agent_spec("deepseek/deepseek-reasoner")
        assert spec.provider == "deepseek"
        assert spec.transport.kind == "api"
        assert spec.transport.runner == "deepseek"

    def test_resolve_agent_spec_unknown_raises(self):
        """No prefix-to-CLI guessing: unknown keys raise."""
        with pytest.raises(ValueError, match="not in AGENT_REGISTRY"):
            resolve_agent_spec("openai/future-model-xyz")


class TestAddingNewApiModelIsSingleEntry:
    """AC: Adding a new API-backed model requires only a registry entry — no
    edits to role derivation or profiles."""

    def test_new_api_entry_flows_through_role_derivation(self, monkeypatch):
        new_spec = AgentSpec(
            provider="openai",
            model="fake-api-model",
            transport=TransportSpec(kind="api", runner="openai"),
            tier="fast",
            capability=7,
            cost_rank=1,
        )
        from theforge.config.models import _spec_to_model_info

        monkeypatch.setitem(AGENT_REGISTRY, "openai/fake-api-model", new_spec)
        monkeypatch.setitem(
            MODEL_REGISTRY,
            "openai/fake-api-model",
            _spec_to_model_info(new_spec),
        )

        assignment = derive_roles(
            ["openai/fake-api-model", "claude/opus"],
            budget_usd=10.0,
        )
        # Dev role picks cheapest (our fake-api-model at cost_rank=1)
        assert assignment.dev.ref.model == "fake-api-model"
        # And it flows through as an API transport (provider set, cli None)
        assert assignment.dev.ref.provider == "openai"
        assert assignment.dev.ref.cli is None


class TestRoleDerivationDoesNotBranchOnCliIdentity:
    """AC: Role derivation selects by tier/capability/cost/dev_capable — not by CLI string."""

    def test_dev_role_uses_cheapest_regardless_of_transport(self):
        # deepseek-chat (API, cost_rank=1) vs claude/opus (CLI, cost_rank=3).
        # If role derivation branched on CLI identity it might prefer CLI for dev,
        # but it must pick the cheapest — the API-backed deepseek-chat.
        assignment = derive_roles(
            ["deepseek/deepseek-chat", "claude/opus"],
            budget_usd=10.0,
        )
        assert assignment.dev.ref.model == "deepseek-chat"
        assert assignment.dev.ref.provider == "deepseek"


class TestRunnerDispatchUsesTransportKind:
    """AC: Runners dispatch on TransportSpec.kind, not on provider string."""

    def _profile(self, *, cli=None, provider=None, model="x"):
        return ModelProfile(
            name="t",
            cli=cli,
            provider=provider,
            model=model,
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )

    def test_cli_profile_dispatches_as_cli(self):
        p = self._profile(cli="claude", model="sonnet")
        assert _profile_transport_kind(p) == "cli"

    def test_api_profile_dispatches_as_api(self):
        p = self._profile(provider="deepseek", model="deepseek-reasoner")
        assert _profile_transport_kind(p) == "api"

    def test_api_profile_for_openai_dispatches_as_api(self):
        """'openai/' no longer implies CLI — provider set means API transport."""
        p = self._profile(provider="openai", model="gpt-4o")
        assert _profile_transport_kind(p) == "api"


class TestCheckConfigSeparateColumns:
    """AC: check-config reports provider and transport as separate columns."""

    def test_split_provider_transport_cli(self):
        from theforge.cli.check_config import _split_provider_transport

        assert _split_provider_transport("claude", None) == ("anthropic", "cli:claude")
        assert _split_provider_transport("codex", None) == ("openai", "cli:codex")
        assert _split_provider_transport("gemini", None) == ("google", "cli:gemini")

    def test_split_provider_transport_api(self):
        from theforge.cli.check_config import _split_provider_transport

        assert _split_provider_transport(None, "deepseek") == ("deepseek", "api")
        assert _split_provider_transport(None, "openai") == ("openai", "api")


class TestNoProviderPrefixGuessingInConfigLoad:
    """AC: models: entries resolve to AgentSpec with explicit TransportSpec — no
    prefix-to-CLI guessing in config load."""

    def test_unknown_key_rejected_even_for_known_prefix(self, tmp_path: Path):
        import yaml

        from theforge.config import load_config

        config_path = tmp_path / "forge.yaml"
        # 'openai/' is a known provider prefix, but this model is not in the
        # registry — the old behavior would silently route it to the Codex CLI.
        config_path.write_text(
            yaml.dump({"models": ["openai/not-in-registry"], "budget_usd": 5.0}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="not in AGENT_REGISTRY"):
            load_config(config_path)
