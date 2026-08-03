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
from theforge.config.models import (
    RoutingPolicy,
    canonical_id_for_spec,
    is_canonical_model_id,
    transport_for,
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

    def test_every_key_is_a_canonical_identity(self):
        """Keys are provider/model/transport-kind — never a provider-like prefix."""
        for key, spec in AGENT_REGISTRY.items():
            assert is_canonical_model_id(key), key
            assert key == canonical_id_for_spec(spec), key

    def test_cli_backed_models_resolve_to_cli_transport(self):
        for key in ("anthropic/sonnet/cli", "anthropic/opus/cli", "openai/gpt-5.4/cli"):
            assert AGENT_REGISTRY[key].transport.kind == "cli"

    def test_api_backed_models_resolve_to_api_transport(self):
        for key in ("deepseek/deepseek-reasoner/api", "deepseek/deepseek-chat/api"):
            assert AGENT_REGISTRY[key].transport.kind == "api"

    def test_openai_api_transport_entries_exist(self):
        """Operators select the OpenAI API transport by transport kind, not a prefix."""
        for key in ("openai/gpt-5.4/api", "openai/gpt-5.4-mini/api", "openai/gpt-5.4-pro/api"):
            spec = AGENT_REGISTRY[key]
            assert spec.transport.kind == "api"
            assert spec.transport.runner == "openai"
            assert spec.provider == "openai"

    def test_google_api_and_cli_entries_are_distinct_identities(self):
        for key in (
            "google/gemini-2.5-pro/api",
            "google/gemini-3-flash-preview/api",
            "google/gemini-3.1-pro-preview/api",
        ):
            spec = AGENT_REGISTRY[key]
            assert spec.transport.kind == "api"
            assert spec.transport.runner == "google"
            assert spec.provider == "google"

    def test_gemini_cli_transport_entries_exist(self):
        """Operators opt into the Gemini CLI via transport kind, not a 'gemini-cli/' prefix."""
        for key in (
            "google/gemini-2.5-pro/cli",
            "google/gemini-3-flash-preview/cli",
            "google/gemini-3.1-pro-preview/cli",
        ):
            spec = AGENT_REGISTRY[key]
            assert spec.transport.kind == "cli"
            assert spec.transport.executable == "gemini"
            assert spec.provider == "google"

    def test_local_models_are_api_transports_with_base_url(self):
        """AC5: locality is endpoint metadata on an API transport."""
        for model in ("codestral", "deepseek-coder", "llama3.1", "qwen2.5-coder"):
            spec = AGENT_REGISTRY[f"openai/{model}/api"]
            assert spec.transport.kind == "api"
            assert spec.provider == "openai"
            assert spec.base_url is not None
            assert "localhost" in spec.base_url

    def test_no_local_provider_or_transport_kind(self):
        """AC5: there is no 'local/' provider and no 'local' transport kind."""
        for key, spec in AGENT_REGISTRY.items():
            assert not key.startswith("local/"), key
            assert spec.transport.kind in ("cli", "api"), key
            assert spec.provider != "local", key

    def test_deepseek_openai_google_and_all_cli_models_expressible(self):
        """AC: DeepSeek, OpenAI, Google, and CLI-backed models are all expressible."""
        providers = {s.provider for s in AGENT_REGISTRY.values()}
        assert {"anthropic", "openai", "google", "deepseek"} <= providers

    def test_model_registry_is_derived_from_agent_registry(self):
        assert set(MODEL_REGISTRY) == set(AGENT_REGISTRY)

    def test_resolve_agent_spec_normalizes_legacy_prefix_aliases(self):
        """Legacy spellings resolve, and land on the canonical identity."""
        assert resolve_agent_spec("openai-api/gpt-5.4") is AGENT_REGISTRY["openai/gpt-5.4/api"]
        assert resolve_agent_spec("openai/gpt-5.4") is AGENT_REGISTRY["openai/gpt-5.4/cli"]
        assert (
            resolve_agent_spec("gemini-cli/gemini-2.5-pro")
            is AGENT_REGISTRY["google/gemini-2.5-pro/cli"]
        )
        assert resolve_agent_spec("claude/opus") is AGENT_REGISTRY["anthropic/opus/cli"]

    def test_resolve_agent_spec_known(self):
        spec = resolve_agent_spec("deepseek/deepseek-reasoner/api")
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
            routing=RoutingPolicy(tier="fast", capability=7, cost_rank=1),
        )
        from theforge.config.models import _spec_to_model_info

        key = "openai/fake-api-model/api"
        monkeypatch.setitem(AGENT_REGISTRY, key, new_spec)
        monkeypatch.setitem(MODEL_REGISTRY, key, _spec_to_model_info(key, new_spec))

        assignment = derive_roles(
            [key, "anthropic/opus/cli"],
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
            ["deepseek/deepseek-chat/api", "anthropic/opus/cli"],
            budget_usd=10.0,
        )
        assert assignment.dev.ref.model == "deepseek-chat"
        assert assignment.dev.ref.provider == "deepseek"


class TestPhaseEligibilityFiltersCandidates:
    """AC: phase_eligibility on an AgentSpec excludes a model from selection
    for phases it's not eligible for."""

    def test_pro_model_excluded_from_preflight(self):
        # gpt-5.4-pro is not eligible for preflight; with sonnet present preflight
        # must pick a non-pro candidate.
        ra = derive_roles(["anthropic/sonnet/cli", "openai/gpt-5.4-pro/api"], budget_usd=10.0)
        assert ra.preflight.ref.model != "gpt-5.4-pro"

    def test_single_ineligible_model_falls_back(self):
        # When the pool is exhausted by eligibility, fall back to the full pool
        # rather than leaving the phase unassigned.
        ra = derive_roles(["openai/gpt-5.4-pro/api"], budget_usd=10.0)
        assert ra.preflight.ref.model == "gpt-5.4-pro"


class TestTransportPropagatesToProfile:
    """AC: ModelRef.transport flows through bridge → ModelProfile.transport, so
    runner dispatch can read the explicit TransportSpec.kind."""

    def test_api_transport_reaches_profile(self):
        from theforge.config.bridge import role_assignment_to_profiles

        ra = derive_roles(
            ["deepseek/deepseek-reasoner/api", "anthropic/opus/cli"], budget_usd=10.0
        )
        profiles = role_assignment_to_profiles(ra)
        dev_profile = profiles["dev_profile"]
        assert dev_profile.transport is not None
        assert dev_profile.transport.kind == "api"


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

    def test_explicit_transport_spec_is_the_dispatch_source_of_truth(self):
        """A ModelProfile's TransportSpec — not its provider token — decides dispatch."""
        api_transport = TransportSpec(kind="api", runner="openai")
        p = ModelProfile(
            name="t",
            cli=None,
            provider=None,
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
            transport=api_transport,
        )
        assert _profile_transport_kind(p) == "api"
        assert p.transport is api_transport

    def test_conflicting_raw_cli_redevives_transport_rather_than_diverging(self):
        """``replace(profile, cli=...)`` must move the transport, not leave it stale."""
        from dataclasses import replace

        p = self._profile(cli="claude", model="sonnet")
        swapped = replace(p, cli="codex", model="gpt-5.4")
        assert swapped.transport is not None
        assert swapped.transport.runner == "codex"
        assert _profile_transport_kind(swapped) == "cli"
        # cli always mirrors the transport it dispatches through.
        assert swapped.cli == swapped.transport.runner


class TestCheckConfigSeparateColumns:
    """AC: check-config reports provider and transport as separate columns."""

    def test_split_provider_transport_cli(self):
        from theforge.cli.check_config import _split_provider_transport

        for provider, runner in (
            ("anthropic", "claude"),
            ("openai", "codex"),
            ("google", "gemini"),
        ):
            transport = transport_for(provider, "cli")
            assert _split_provider_transport(None, transport) == (provider, f"cli:{runner}")

    def test_split_provider_transport_api(self):
        from theforge.cli.check_config import _split_provider_transport

        for provider in ("deepseek", "openai", "google"):
            assert _split_provider_transport(provider, transport_for(provider, "api")) == (
                provider,
                "api",
            )


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
