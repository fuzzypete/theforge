"""Canonical model identity: provider + model + transport.kind (#1415).

Covers the normalized shape end to end: runner derivation from
``(provider, transport.kind)``, canonical registry identities, the raw-input
alias boundary, local models as API transports with endpoint metadata, routing
policy held apart from identity, the CLI→API transport fallback, and the
absence of any dispatch inference from the legacy ``cli``/``provider`` pair
once config is loaded.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from theforge.config import ModelProfile, load_config
from theforge.config.models import (
    AGENT_REGISTRY,
    AgentSpec,
    RoutingPolicy,
    TransportSpec,
    canonical_id_for_spec,
    is_canonical_model_id,
    normalize_model_key,
    provider_for_transport,
    transport_for,
    transport_from_raw_fields,
)
from theforge.config.types import TransportFallbackConfig


def _write(tmp_path: Path, body: dict) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.dump(body), encoding="utf-8")
    return path


# ── AC4: runner derived from (provider, transport.kind) ──────────────────


class TestRunnerDerivation:
    @pytest.mark.parametrize(
        ("provider", "kind", "runner", "executable"),
        [
            ("openai", "cli", "codex", "codex"),
            ("anthropic", "cli", "claude", "claude"),
            ("google", "cli", "gemini", "gemini"),
            ("openai", "api", "openai", None),
            ("google", "api", "google", None),
            ("deepseek", "api", "deepseek", None),
            ("anthropic", "api", "anthropic", None),
        ],
    )
    def test_unambiguous_tuples_derive_their_executor(self, provider, kind, runner, executable):
        transport = transport_for(provider, kind)
        assert transport.kind == kind
        assert transport.runner == runner
        assert transport.executable == executable

    def test_ambiguous_tuple_requires_an_explicit_runner(self):
        """gh-aw shares (anthropic, cli) with the Claude CLI, so it must be named."""
        assert transport_for("anthropic", "cli").runner == "claude"
        ghaw = transport_for("anthropic", "cli", runner="ghaw")
        assert ghaw.runner == "ghaw"
        assert ghaw.executable == "gh"

    def test_runner_that_contradicts_the_tuple_is_rejected(self):
        with pytest.raises(ValueError, match="not valid for"):
            transport_for("openai", "cli", runner="claude")

    def test_provider_without_a_cli_is_rejected(self):
        with pytest.raises(ValueError, match="No CLI runner for provider"):
            transport_for("deepseek", "cli")

    def test_transport_kind_is_bounded(self):
        with pytest.raises(ValueError, match="must be 'cli' or 'api'"):
            transport_for("openai", "local")

    def test_provider_for_transport_is_the_inverse(self):
        for provider in ("anthropic", "openai", "google", "deepseek"):
            assert provider_for_transport(transport_for(provider, "api")) == provider
        for provider in ("anthropic", "openai", "google"):
            assert provider_for_transport(transport_for(provider, "cli")) == provider


# ── AC2/AC3: canonical identity, no provider-prefix transport encoding ───


class TestCanonicalIdentity:
    def test_registry_keys_are_derived_from_identity_alone(self):
        for key, spec in AGENT_REGISTRY.items():
            assert key == canonical_id_for_spec(spec)
            assert is_canonical_model_id(key)

    def test_same_model_over_two_transports_is_two_identities(self):
        cli = AGENT_REGISTRY["openai/gpt-5.4/cli"]
        api = AGENT_REGISTRY["openai/gpt-5.4/api"]
        assert (cli.provider, cli.model) == (api.provider, api.model)
        assert cli.transport.kind != api.transport.kind
        assert canonical_id_for_spec(cli) != canonical_id_for_spec(api)

    def test_no_provider_prefix_encodes_transport(self):
        for key, spec in AGENT_REGISTRY.items():
            provider = key.split("/", 1)[0]
            assert provider == spec.provider, key
            assert "-api" not in provider and "-cli" not in provider, key

    @pytest.mark.parametrize(
        ("alias", "canonical"),
        [
            ("openai-api/gpt-5.4", "openai/gpt-5.4/api"),
            ("openai/gpt-5.4", "openai/gpt-5.4/cli"),
            ("gemini-cli/gemini-2.5-pro", "google/gemini-2.5-pro/cli"),
            ("google/gemini-2.5-pro", "google/gemini-2.5-pro/api"),
            ("claude/sonnet", "anthropic/sonnet/cli"),
        ],
    )
    def test_legacy_spellings_normalize_at_the_input_boundary(self, alias, canonical):
        assert normalize_model_key(alias) == canonical

    def test_canonical_ids_pass_through_normalization_unchanged(self):
        for key in AGENT_REGISTRY:
            assert normalize_model_key(key) == key

    def test_loaded_config_never_carries_a_legacy_spelling(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["claude/sonnet", "openai-api/gpt-5.4", "gemini-cli/gemini-2.5-pro"],
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.models == [
            "anthropic/sonnet/cli",
            "openai/gpt-5.4/api",
            "google/gemini-2.5-pro/cli",
        ]


# ── AC1: bounded transport object in the config schema ───────────────────


class TestTransportObjectInConfig:
    def test_mapping_form_declares_transport_as_a_first_class_object(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        {
                            "provider": "openai",
                            "model": "gpt-5.4",
                            "transport": {"kind": "cli"},
                            "routing": {"tier": "strong"},
                        },
                        {
                            "provider": "anthropic",
                            "model": "sonnet",
                            "transport": {"kind": "cli"},
                        },
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.models == ["openai/gpt-5.4/cli", "anthropic/sonnet/cli"]

    def test_transport_kind_is_validated_at_the_config_boundary(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        {"provider": "openai", "model": "gpt-5.4", "transport": {"kind": "local"}}
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        with pytest.raises(ValueError, match="transport.kind' must be one of"):
            load_config(path)

    def test_missing_transport_is_rejected_rather_than_guessed(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {"enabled": [{"provider": "openai", "model": "gpt-5.4"}]},
                "budget_usd": 30.0,
            },
        )
        with pytest.raises(ValueError, match="missing required field 'transport'"):
            load_config(path)


# ── AC1/AC9: transport-only overrides on an already-derived profile ──────


class TestTransportOnlyOverrideSwitchesDispatch:
    """``overrides.<role>.transport: {kind: ...}`` alone must move dispatch.

    The trap: the role's profile/ref was derived with a mirrored ``cli``. If the
    override resolves a new transport but leaves that stale ``cli`` in place, the
    constructor re-derives the *old* transport from it and the switch silently
    no-ops.
    """

    def test_loader_switches_dev_from_cli_to_api(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["anthropic/sonnet/cli", "anthropic/opus/cli"],
                "budget_usd": 30.0,
                "overrides": {"dev": {"transport": {"kind": "api"}}},
            },
        )
        dev = load_config(path).dev_profile
        assert dev.mode == "api"
        assert dev.transport == transport_for("anthropic", "api")
        assert dev.cli is None
        assert dev.provider_family == "anthropic"

    def test_loader_switches_a_reviewer_from_api_to_cli(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["anthropic/sonnet/cli", "openai/gpt-5.4/api"],
                "budget_usd": 30.0,
                "overrides": {
                    "review_pool": [
                        {"name": "openai-gpt-5.4-api", "transport": {"kind": "cli"}},
                    ]
                },
            },
        )
        reviewer = next(p for p in load_config(path).review_pool if p.model == "gpt-5.4")
        assert reviewer.mode == "cli"
        assert reviewer.transport == transport_for("openai", "cli")
        assert reviewer.cli == "codex"
        assert reviewer.provider is None

    def test_override_without_a_transport_key_leaves_dispatch_alone(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["anthropic/sonnet/cli", "anthropic/opus/cli"],
                "budget_usd": 30.0,
                "overrides": {"dev": {"timeout_seconds": 1200}},
            },
        )
        dev = load_config(path).dev_profile
        assert dev.timeout_seconds == 1200
        assert dev.mode == "cli"
        assert dev.cli == "claude"

    @pytest.mark.parametrize(
        ("base_kwargs", "kind", "expected_transport", "expected_cli", "expected_provider"),
        [
            ({"cli": "claude"}, "api", ("anthropic", "api"), None, "anthropic"),
            ({"provider": "openai"}, "cli", ("openai", "cli"), "codex", None),
            ({"cli": "codex"}, "api", ("openai", "api"), None, "openai"),
            ({"provider": "google"}, "cli", ("google", "cli"), "gemini", None),
        ],
    )
    def test_profile_override_mirrors_the_legacy_pair_onto_the_new_transport(
        self, base_kwargs, kind, expected_transport, expected_cli, expected_provider
    ):
        from theforge.config.profiles import _apply_profile_overrides

        base = ModelProfile(
            name="dev",
            model="m",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
            **base_kwargs,
        )
        switched = _apply_profile_overrides(base, {"transport": {"kind": kind}})
        assert switched.transport == transport_for(*expected_transport)
        assert switched.mode == kind
        assert switched.cli == expected_cli
        assert switched.provider == expected_provider

    @pytest.mark.parametrize(
        ("base_kwargs", "kind", "expected_transport", "expected_cli", "expected_provider"),
        [
            ({"cli": "claude"}, "api", ("anthropic", "api"), None, "anthropic"),
            ({"provider": "openai"}, "cli", ("openai", "cli"), "codex", None),
        ],
    )
    def test_ref_override_mirrors_the_legacy_pair_onto_the_new_transport(
        self, base_kwargs, kind, expected_transport, expected_cli, expected_provider
    ):
        from theforge.config.role_derivation import _apply_ref_overrides
        from theforge.config.schema import ModelRef

        ref = ModelRef(model="m", budget_usd=1.0, timeout_seconds=60, **base_kwargs)
        switched = _apply_ref_overrides(ref, {"transport": {"kind": kind}})
        assert switched.transport == transport_for(*expected_transport)
        assert switched.cli == expected_cli
        assert switched.provider == expected_provider

    def test_transport_switch_survives_the_bridge_to_a_profile(self):
        """A ref-level switch must still be the dispatch truth after bridging."""
        from theforge.config.bridge import role_assignment_to_profiles
        from theforge.config.role_derivation import derive_roles

        assignment = derive_roles(
            ["anthropic/sonnet/cli", "anthropic/opus/cli"],
            overrides={"dev": {"transport": {"kind": "api"}}},
            budget_usd=10.0,
        )
        assert assignment.dev.ref.transport == transport_for("anthropic", "api")
        assert assignment.dev.ref.cli is None

        dev_profile = role_assignment_to_profiles(assignment)["dev_profile"]
        assert dev_profile.mode == "api"
        assert dev_profile.cli is None
        assert dev_profile.transport == transport_for("anthropic", "api")

    def test_transport_override_without_a_resolvable_provider_is_rejected(self):
        from theforge.config.profiles import _apply_profile_overrides

        base = ModelProfile(
            name="dev",
            model="m",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        with pytest.raises(ValueError, match="needs a provider"):
            _apply_profile_overrides(base, {"transport": {"kind": "api"}})

    def test_override_transport_block_is_validated(self):
        from theforge.config.profiles import _apply_profile_overrides

        base = ModelProfile(
            name="dev",
            cli="claude",
            model="m",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        with pytest.raises(ValueError, match="transport.kind' must be 'cli' or 'api'"):
            _apply_profile_overrides(base, {"transport": {"kind": "local"}})
        with pytest.raises(ValueError, match="only supports 'kind'"):
            _apply_profile_overrides(base, {"transport": {"kind": "api", "runner": "codex"}})

    def test_review_role_override_is_preserved(self, tmp_path):
        """Regression: the override constructor dropped review_role entirely."""
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["anthropic/sonnet/cli", "anthropic/opus/cli"],
                "budget_usd": 30.0,
                "overrides": {
                    "review_pool": [{"name": "anthropic-opus-cli", "review_role": "correctness"}]
                },
            },
        )
        reviewer = next(p for p in load_config(path).review_pool if p.name == "anthropic-opus-cli")
        assert reviewer.review_role == "correctness"


# ── AC5: local models are API transports with endpoint metadata ──────────


class TestLocalModelsAreApiTransports:
    def test_builtin_local_entries_carry_base_url_through_to_the_profile(self, tmp_path):
        path = _write(
            tmp_path,
            {"project": "p", "models": ["openai/qwen2.5-coder/api"], "budget_usd": 30.0},
        )
        config = load_config(path)
        assert config.dev_profile.mode == "api"
        assert config.dev_profile.provider_family == "openai"
        assert config.dev_profile.base_url == "http://localhost:11434/v1"

    def test_inline_local_declaration_overrides_the_endpoint(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        {
                            "provider": "openai",
                            "model": "qwen2.5-coder",
                            "transport": {"kind": "api"},
                            "base_url": "http://127.0.0.1:8000/v1",
                            "routing": {"tier": "fast"},
                        }
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.models == ["openai/qwen2.5-coder/api"]
        assert config.dev_profile.base_url == "http://127.0.0.1:8000/v1"
        assert config.dev_profile.mode == "api"


# ── AC6: routing policy is not identity ──────────────────────────────────


class TestRoutingPolicyIsSeparateFromIdentity:
    def test_routing_differences_do_not_change_identity(self):
        base = AGENT_REGISTRY["openai/gpt-5.4/cli"]
        rerouted = replace(
            base,
            routing=RoutingPolicy(tier="cheap", capability=1, cost_rank=3, dev_capable=False),
        )
        assert canonical_id_for_spec(rerouted) == canonical_id_for_spec(base)

    def test_identity_fields_are_not_on_the_routing_policy(self):
        policy_fields = set(RoutingPolicy.__dataclass_fields__)
        assert policy_fields == {
            "tier",
            "capability",
            "cost_rank",
            "dev_capable",
            "phase_eligibility",
            "cost_rank_basis",
        }
        assert not policy_fields & {"provider", "model", "transport", "base_url"}

    def test_pricing_is_metadata_not_identity(self):
        base = AGENT_REGISTRY["openai/gpt-5.4/api"]
        repriced = replace(base, input_cost_per_mtok=999.0, output_cost_per_mtok=999.0)
        assert canonical_id_for_spec(repriced) == canonical_id_for_spec(base)


# ── AC8: CLI→API transport fallback ──────────────────────────────────────


class TestTransportFallback:
    def test_fallback_resolves_to_an_api_transport_on_the_same_provider(self):
        fallback = TransportFallbackConfig(provider="openai", model="gpt-5.4-mini")
        transport = fallback.transport()
        assert transport.kind == "api"
        assert provider_for_transport(transport) == "openai"

    def test_built_fallback_profile_actually_switches_transport(self):
        from theforge.runners.cli import _build_api_fallback_profile

        cli_profile = ModelProfile(
            name="dev",
            cli="codex",
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
            api_fallback=TransportFallbackConfig(provider="openai", model="gpt-5.4-mini"),
        )
        assert cli_profile.mode == "cli"
        fallback_profile = _build_api_fallback_profile(cli_profile)
        assert fallback_profile is not None
        # The stale-transport trap: replace() alone would keep the Codex CLI spec.
        assert fallback_profile.mode == "api"
        assert fallback_profile.transport is not None
        assert fallback_profile.transport.runner == "openai"
        assert fallback_profile.provider_family == "openai"

    # load_config verifies provider identity, which imports the real SDK
    # transitively; the cost is machinery this module's own source does not
    # show, which is what the marker is for.
    @pytest.mark.orchestration
    @pytest.mark.timeout(20)
    def test_renamed_yaml_key_wires_the_fallback(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["openai/gpt-5.4/cli"],
                "transport_fallback": {"openai": {"model": "gpt-5.4-mini"}},
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.dev_profile.api_fallback is not None
        assert config.dev_profile.api_fallback.model == "gpt-5.4-mini"

    def test_old_provider_fallbacks_key_is_rejected_with_the_new_name(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["openai/gpt-5.4/cli"],
                "provider_fallbacks": {"openai": {"model": "gpt-5.4-mini"}},
                "budget_usd": 30.0,
            },
        )
        with pytest.raises(ValueError, match="renamed to 'transport_fallback'"):
            load_config(path)


# ── AC9/AC10: dispatch reads only the normalized transport ───────────────


class TestDispatchReadsTransportOnly:
    def test_profile_cli_mirrors_its_transport(self):
        profile = ModelProfile(
            name="dev",
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
            transport=transport_for("openai", "cli"),
        )
        assert profile.cli == "codex"
        assert profile.mode == "cli"
        assert profile.provider_family == "openai"

    def test_api_profile_has_no_cli_to_dispatch_on(self):
        profile = ModelProfile(
            name="dev",
            cli="codex",
            provider=None,
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        api = replace(profile, cli=None, provider="openai")
        assert api.cli is None
        assert api.mode == "api"
        assert api.transport is not None and api.transport.runner == "openai"

    def test_cli_runner_for_dispatch_comes_from_the_transport(self):
        from theforge.runners.cli import _profile_cli_runner, _profile_transport_kind

        profile = ModelProfile(
            name="dev",
            model="gemini-2.5-pro",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
            transport=transport_for("google", "cli"),
        )
        assert _profile_transport_kind(profile) == "cli"
        assert _profile_cli_runner(profile) == "gemini"

    def test_raw_field_normalization_is_migration_only(self):
        """The legacy pair resolves to a transport once; unknown pairs resolve to nothing."""
        assert transport_from_raw_fields("codex", None) == transport_for("openai", "cli")
        assert transport_from_raw_fields(None, "deepseek") == transport_for("deepseek", "api")
        # cli wins when both are supplied.
        assert transport_from_raw_fields("claude", "openai").kind == "cli"
        assert transport_from_raw_fields("llama", None) is None
        assert transport_from_raw_fields(None, None) is None


# ── AC7: identity and transport stay distinct in telemetry ───────────────


class TestTelemetryKeepsIdentityAndTransportDistinct:
    def test_profile_exposes_provider_and_transport_separately(self):
        cli_profile = ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        # provider stays unset on a CLI profile, but the identity is recoverable.
        assert cli_profile.provider is None
        assert cli_profile.provider_family == "anthropic"
        assert cli_profile.transport_kind == "cli"
        assert cli_profile.identity_label == "anthropic/sonnet (cli)"

    def test_check_config_renders_provider_and_transport_in_separate_columns(self):
        from theforge.cli.check_config import _split_provider_transport

        assert _split_provider_transport("openai", transport_for("openai", "cli")) == (
            "openai",
            "cli:codex",
        )
        assert _split_provider_transport("openai", transport_for("openai", "api")) == (
            "openai",
            "api",
        )

    def test_model_profile_identity_metadata_records_all_three(self):
        from theforge.model_profiles import canonical_id_from_identity

        assert (
            canonical_id_from_identity(actual_model="sonnet", provider=None, cli="claude")
            == "anthropic/sonnet/cli"
        )
        assert (
            canonical_id_from_identity(actual_model="gpt-5.4", provider="openai", cli=None)
            == "openai/gpt-5.4/api"
        )

    def test_registry_attribution_survives_load(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        {
                            "provider": "openai",
                            "model": "gpt-5.9",
                            "transport": {"kind": "api"},
                            "routing": {"tier": "strong"},
                        }
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.model_registry_sources["openai/gpt-5.9/api"] == "forge.yaml"
        assert config.dev_profile.registry_id == "openai/gpt-5.9/api"


# ── Seam: config load → coordinator phase handoff ────────────────────────


class TestTransportCrossesPhaseBoundaries:
    """Transport is state handed between phases; it must survive each hop."""

    def test_derived_roles_all_carry_a_transport_after_load(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["anthropic/sonnet/cli", "openai/gpt-5.4/api"],
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        profiles = [config.dev_profile, config.preflight_profile, *config.review_pool]
        if config.synthesis_profile is not None:
            profiles.append(config.synthesis_profile)
        for profile in profiles:
            assert profile.transport is not None, profile.name
            assert profile.mode == profile.transport.kind
            assert profile.cli == (
                profile.transport.runner if profile.transport.kind == "cli" else None
            )

    def test_complexity_adaptation_moves_transport_with_the_model(self, tmp_path):
        from theforge.coordinator.preflight import _apply_complexity_adaptation

        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["anthropic/sonnet/cli", "deepseek/deepseek-v4-pro/api"],
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        adapted = _apply_complexity_adaptation(config, "HIGH", complexity_score=9)
        dev = adapted.dev_profile
        assert dev.transport is not None
        # Whichever model won, dispatch state is internally consistent.
        assert dev.mode == dev.transport.kind
        if dev.model == "deepseek-v4-pro":
            assert dev.mode == "api"
            assert dev.cli is None
        else:
            assert dev.mode == "cli"
            assert dev.cli == dev.transport.runner

    def test_dev_escalation_lands_on_the_target_transport(self, tmp_path):
        from theforge.coordinator.review_phase import _perform_dev_model_escalation

        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": ["anthropic/sonnet/cli", "openai/gpt-5.4-pro/api"],
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        result = _perform_dev_model_escalation(config)
        assert result is not None
        _old, _new, escalated = result
        dev = escalated.dev_profile
        assert dev.model == "gpt-5.4-pro"
        assert dev.transport is not None and dev.transport.kind == "api"
        assert dev.cli is None


# ── models.custom overlays normalize to identities too ───────────────────


class TestCustomOverlayNormalization:
    def test_declaration_key_is_replaced_by_the_identity(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "my-fast-model"],
                    "custom": {
                        "my-fast-model": {
                            "provider": "openai",
                            "model": "gpt-5.7",
                            "transport": {"kind": "api"},
                            "tier": "fast",
                            "input_cost_per_mtok": 1.0,
                            "output_cost_per_mtok": 2.0,
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.models == ["anthropic/sonnet/cli", "openai/gpt-5.7/api"]
        assert "my-fast-model" not in (config.model_registry or {})
        assert config.model_registry["openai/gpt-5.7/api"].transport.kind == "api"

    def test_legacy_provider_alias_token_still_resolves_to_a_real_provider(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["legacy-alias"],
                    "custom": {
                        "legacy-alias": {
                            "provider": "openai-api",
                            "model": "gpt-5.8",
                            "tier": "fast",
                            "input_cost_per_mtok": 1.0,
                            "output_cost_per_mtok": 2.0,
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.models == ["openai/gpt-5.8/api"]
        spec: AgentSpec = config.model_registry["openai/gpt-5.8/api"]
        assert spec.provider == "openai"
        assert spec.transport == TransportSpec(kind="api", runner="openai")
