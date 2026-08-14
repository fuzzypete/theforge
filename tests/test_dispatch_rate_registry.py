"""Identity-keyed pricing: one resolution, consumed by routing and accounting (#2335).

A model could be priced in project configuration, routed on the strength of
those figures, run, and still record its spend as unknown — routing read the
merged model registry while accounting read a table compiled into the code. The
fix compiles ONE registry at configuration load, keyed by
``(provider, model, transport)``, and every accounting site looks up the
identity that actually dispatched.

The dispatch paths exercised here are the ones a prior attempt discovered one
review cycle at a time: seated primary, adaptive pool candidate, model fallback,
transport fallback with the model identifier unchanged and changed, CLI and API
for the same model name, a model priced only in ``forge.yaml``, and a model
priced nowhere. Plus the invariant that keeps them from recurring: **a lookup
never crosses transports**.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import load_config
from theforge.config.dispatch_rates import (
    compile_rate_registry,
    reachable_identities,
)
from theforge.config.types import ModelProfile, TransportFallbackConfig
from theforge.runners import rate_registry as rr
from theforge.runners import schema_utils as su
from theforge.runners.rate_registry import (
    AccountingMode,
    DispatchIdentity,
    RateEntry,
    RateRegistry,
    RateSource,
    identity_of,
    make_identity,
)
from theforge.runners.schema_utils import ModelRates, _estimate_cost, pricing_for

_auth_ok = patch("theforge.config.load.check_agent_auth", return_value=(True, ""))
_import_ok = patch("importlib.import_module")


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _load(tmp_path: Path, raw: dict):
    with _auth_ok, _import_ok:
        return load_config(_write(tmp_path, raw))


def _custom(model: str, kind: str, *, runner: str | None = None, cost: dict | None = None) -> dict:
    transport: dict = {"kind": kind}
    if runner is not None:
        transport["runner"] = runner
    entry: dict = {
        "provider": "openai",
        "model": model,
        "transport": transport,
        "routing": {"tier": "strong", "capability": 9, "cost_rank": 2},
    }
    if cost is not None:
        entry["cost"] = cost
    return entry


# ── The invariant: a lookup never crosses transports ──────────────────


class TestNoBorrowAcrossTransports:
    def test_installed_registry_refuses_to_price_another_transport(self):
        """A CLI price is not an API price, even for the identical model name."""
        cli = DispatchIdentity("anthropic", "model-x", "cli")
        api = DispatchIdentity("anthropic", "model-x", "api")
        registry = RateRegistry(
            entries={
                cli: RateEntry(
                    identity=cli,
                    rates=ModelRates(input_per_mtok=3.0, output_per_mtok=15.0),
                    mode=AccountingMode.PROVIDER_REPORTED,
                    source=RateSource.PROJECT,
                )
            }
        )
        with rr.scoped_registry(registry):
            assert rr.resolve(cli).rates == ModelRates(3.0, 15.0)
            assert rr.resolve(api).rates is None
            assert rr.resolve(api).priced is False

    def test_lookup_never_returns_an_entry_for_a_different_transport(self):
        """Whatever comes back is keyed to the transport that was asked about."""
        entries = {
            DispatchIdentity("openai", "m", "cli"): RateEntry(
                identity=DispatchIdentity("openai", "m", "cli"),
                rates=ModelRates(1.0, 2.0),
                mode=AccountingMode.TOKEN_ESTIMATED,
                source=RateSource.CATALOG,
            ),
            DispatchIdentity("openai", "m", "api"): RateEntry(
                identity=DispatchIdentity("openai", "m", "api"),
                rates=ModelRates(9.0, 18.0),
                mode=AccountingMode.TOKEN_ESTIMATED,
                source=RateSource.CATALOG,
            ),
        }
        registry = RateRegistry(entries=entries)
        with rr.scoped_registry(registry):
            for transport in ("cli", "api", "nonsense"):
                identity = DispatchIdentity("openai", "m", transport)
                entry = rr.resolve(identity)
                assert entry.identity is not None
                assert entry.identity.transport == transport

    def test_project_cli_price_does_not_leak_to_the_api_path(self, tmp_path, caplog):
        """The reported P1, end to end.

        ``zeta-9`` is priced by the project on the CLI and dispatched on the API
        with no API price. The API run must record cost as unknown, the load must
        warn naming the conflict, and no figure derived from the CLI rate may
        appear anywhere.
        """
        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            _load(
                tmp_path,
                {
                    "models": {
                        "enabled": ["openai/zeta-9/cli", "openai/zeta-9/api"],
                        "custom": {
                            "openai/zeta-9/cli": _custom(
                                "zeta-9",
                                "cli",
                                runner="codex",
                                cost={"input_per_mtok": 7.0, "output_per_mtok": 21.0},
                            ),
                            "openai/zeta-9/api": _custom("zeta-9", "api"),
                        },
                    },
                    "budget_usd": 50.0,
                },
            )

        cli_cost = _estimate_cost("openai", "zeta-9", 1_000_000, 1_000_000, transport="cli")
        api_cost = _estimate_cost("openai", "zeta-9", 1_000_000, 1_000_000, transport="api")
        assert cli_cost == pytest.approx(28.0)
        assert api_cost is None, "the API path must not borrow the CLI's declared rate"

        warnings = [
            record.getMessage()
            for record in caplog.records
            if "zeta-9" in record.getMessage() and "(api)" in record.getMessage()
        ]
        assert warnings, "load must name the unaccountable API identity"
        assert "transport fallback" in warnings[0] or "review pool" in warnings[0]

    def test_a_price_declared_on_one_transport_leaves_the_other_unpriced(self, tmp_path, caplog):
        """A per-transport gap surfaces; it is not filled from somewhere else.

        The project prices ``gpt-5.5`` on the CLI only. The API path must stay
        unpriced and be named at load: there is no packaged row behind it to
        borrow from (#2388), and the CLI's own figure must never widen onto it.
        """
        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            _load(
                tmp_path,
                {
                    "models": {
                        "enabled": ["openai/gpt-5.5/cli", "openai/gpt-5.5/api"],
                        "custom": {
                            "openai/gpt-5.5/cli": {
                                **_custom(
                                    "gpt-5.5",
                                    "cli",
                                    runner="codex",
                                    cost={"input_per_mtok": 4.0, "output_per_mtok": 25.0},
                                ),
                                "override": True,
                            },
                            "openai/gpt-5.5/api": {
                                **_custom("gpt-5.5", "api"),
                                "override": True,
                            },
                        },
                    },
                    "budget_usd": 50.0,
                },
            )

        assert _estimate_cost("openai", "gpt-5.5", 1_000_000, 0, transport="cli") == pytest.approx(
            4.0
        )
        assert _estimate_cost("openai", "gpt-5.5", 1_000_000, 0, transport="api") is None

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "gpt-5.5" in message and "(api)" in message and "no rate card" in message
            for message in messages
        ), messages


# ── One declaration surface: the catalog, or forge.yaml ───────────────


class TestSingleDeclarationSurface:
    """Every rate in a compiled registry came from a place config can edit (#2388).

    The packaged ``PRICING_TABLE`` these tests used to exercise was the second
    place a rate could be declared: compiled into the runner package, unreachable
    from any configuration, and consulted whenever the registry missed. It is
    gone, and what replaced it is not a different fallback — it is the catalog
    entries that already priced the identities anything could dispatch.
    """

    def test_a_catalog_price_reaches_a_reachable_api_identity(self, tmp_path):
        cfg = _load(tmp_path, {"models": ["openai/gpt-5.4/api"], "budget_usd": 50.0})
        assert cfg.dev_profile.mode == "api"

        registry = rr.active()
        assert registry is not None
        entry = registry.lookup(DispatchIdentity("openai", "gpt-5.4", "api"))
        assert entry.priced
        assert entry.source is RateSource.CATALOG
        assert entry.origin == "openai/gpt-5.4/api"
        assert _estimate_cost("openai", "gpt-5.4", 1_000_000, 0, transport="api") == pytest.approx(
            pricing_for("openai", "gpt-5.4").input_per_mtok
        )

    def test_every_priced_entry_names_a_registry_entry_as_its_origin(self, tmp_path):
        """No entry can be priced from a source configuration could not supply."""
        cfg = _load(
            tmp_path,
            {
                "models": ["openai/gpt-5.4/api", "openai/gpt-5.4-mini/api"],
                "budget_usd": 50.0,
            },
        )
        registry = rr.active()
        assert registry is not None
        priced = [entry for entry in registry.entries.values() if entry.priced]
        assert priced
        for entry in priced:
            assert entry.source in (RateSource.CATALOG, RateSource.PROJECT), entry
            assert entry.origin in cfg.model_registry, entry

    def test_an_identity_no_registry_entry_describes_is_unpriced(self):
        """A model the merged registry does not name is priced by nothing.

        ``gpt-4o`` was a packaged-table row and is reachable on nothing here. It
        used to be materialized onto any identity that named it; now the absence
        of a registry entry IS the answer.
        """
        identity = rr.make_identity("openai", "gpt-4o", "api")
        from theforge.config.dispatch_rates import ReachableIdentity

        registry = compile_rate_registry(
            {},
            (ReachableIdentity(identity=identity, runner="openai", paths=("dev",)),),
        )
        assert registry.lookup(identity).priced is False
        assert registry.lookup(identity).source is RateSource.NONE


# ── Every dispatch path is priced by what it dispatched ───────────────


class TestDispatchPathsAreEnumerated:
    def test_seated_primary_and_model_fallback_are_both_reachable(self):
        profile = ModelProfile(
            name="dev",
            model="gpt-5.4",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            provider="openai",
            fallback_models=("gpt-5.4-mini",),
        )
        config = type("_Cfg", (), {"dev_profile": profile})()
        labels = {reach.identity.label for reach in reachable_identities(config)}
        assert "openai/gpt-5.4 (api)" in labels
        assert "openai/gpt-5.4-mini (api)" in labels

    def test_a_cli_profiles_model_fallback_is_enumerated_on_the_api_transport(self):
        """A CLI ``fallback_models`` entry dispatches on the API, so price it there.

        ``runners/cli.py:_build_cli_fallback_api_profile`` sends a CLI profile's
        fallback entries through the provider's API adapter — the failure that
        triggers a model fallback is a quota or model-not-found refusal, which
        the CLI would only reproduce. Enumerating the entry under the primary's
        CLI transport named an identity that never runs and left the one that
        does unchecked.
        """
        profile = ModelProfile(
            name="dev",
            model="gpt-5.4",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            cli="codex",
            fallback_models=("gpt-5.4-mini",),
        )
        assert profile.mode == "cli"
        config = type("_Cfg", (), {"dev_profile": profile})()
        by_label = {reach.identity.label: reach for reach in reachable_identities(config)}

        assert "openai/gpt-5.4 (cli)" in by_label, "the primary still dispatches on the CLI"
        assert "openai/gpt-5.4-mini (api)" in by_label, (
            "the fallback dispatches on the API and must be priced there"
        )
        assert "openai/gpt-5.4-mini (cli)" not in by_label, (
            "no CLI identity for the fallback — nothing ever dispatches it there"
        )
        assert "model fallback" in " ".join(by_label["openai/gpt-5.4-mini (api)"].paths)

    def test_the_enumerated_fallback_identity_is_the_one_the_runner_builds(self):
        """Load-time enumeration and the runner read ONE definition of the rule.

        Asserting against ``_build_cli_fallback_api_profile`` directly, rather
        than against a second copy of the expectation, is what stops the two
        drifting apart again: if the runner's transport choice changes, this
        fails rather than silently mispricing.
        """
        from theforge.runners.cli import _build_cli_fallback_api_profile

        profile = ModelProfile(
            name="dev",
            model="gpt-5.4",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            cli="codex",
            fallback_models=("gpt-5.4-mini",),
        )
        dispatched = _build_cli_fallback_api_profile(profile, "gpt-5.4-mini")
        assert dispatched is not None
        expected = identity_of(dispatched)

        enumerated = {
            reach.identity
            for reach in reachable_identities(type("_Cfg", (), {"dev_profile": profile})())
            if "model fallback" in " ".join(reach.paths)
        }
        assert enumerated == {expected}

    def test_a_provider_with_no_api_adapter_yields_no_fallback_identity(self):
        """No API adapter means the runner can never attempt it (rule b).

        Both sides read :func:`model_fallback_transport`, so both answer None and
        the entry names no reachable identity. Warning about a path that cannot
        run is noise an operator cannot act on.
        """
        from theforge.config.model_identity import model_fallback_transport

        assert model_fallback_transport("not-a-provider") is None
        assert model_fallback_transport(None) is None
        assert model_fallback_transport("openai") is not None

    def test_an_unpriced_cli_model_fallback_is_reported_under_its_api_identity(
        self, tmp_path, caplog
    ):
        """The finding, end to end: the API identity is named before it can run."""
        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            _load(
                tmp_path,
                {
                    "models": {
                        "enabled": ["openai/zeta-9/cli"],
                        "custom": {
                            "openai/zeta-9/cli": _custom(
                                "zeta-9",
                                "cli",
                                runner="codex",
                                cost={"input_per_mtok": 7.0, "output_per_mtok": 21.0},
                            )
                        },
                    },
                    "overrides": {"dev": {"fallback_models": ["zeta-unpriced"]}},
                    "budget_usd": 50.0,
                },
            )

        named = [
            record.getMessage()
            for record in caplog.records
            if "zeta-unpriced" in record.getMessage()
        ]
        assert named, "the unpriced model fallback must be named at load"
        assert "(api)" in named[0], f"named under the wrong transport: {named[0]}"
        assert "model fallback" in named[0]
        assert _estimate_cost("openai", "zeta-unpriced", 1_000, 1_000, transport="api") is None

    def test_transport_fallback_is_reachable_with_the_identifier_unchanged(self):
        """CLI→API on the SAME model name is a distinct identity, not the same one."""
        profile = ModelProfile(
            name="dev",
            model="gpt-5.4",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            cli="codex",
            api_fallback=TransportFallbackConfig(provider="openai", model="gpt-5.4"),
        )
        config = type("_Cfg", (), {"dev_profile": profile})()
        by_label = {reach.identity.label: reach for reach in reachable_identities(config)}
        assert "openai/gpt-5.4 (cli)" in by_label
        assert "openai/gpt-5.4 (api)" in by_label
        assert "transport fallback" in " ".join(by_label["openai/gpt-5.4 (api)"].paths)

    def test_transport_fallback_is_reachable_with_a_changed_identifier(self):
        profile = ModelProfile(
            name="dev",
            model="sonnet",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            cli="claude",
            api_fallback=TransportFallbackConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )
        config = type("_Cfg", (), {"dev_profile": profile})()
        labels = {reach.identity.label for reach in reachable_identities(config)}
        assert "anthropic/sonnet (cli)" in labels
        assert "anthropic/claude-sonnet-4-6 (api)" in labels

    def test_adaptive_pool_candidates_are_reachable(self, tmp_path):
        """Adaptive routing selects from the registry, so the registry is reachable."""
        cfg = _load(tmp_path, {"models": ["anthropic/sonnet/cli"], "budget_usd": 50.0})
        assert cfg.assignment.adaptive_enabled
        paths = {reach.identity.label: reach.paths for reach in reachable_identities(cfg)}
        assert any("adaptive pool" in " ".join(entry_paths) for entry_paths in paths.values())


class TestSameNameTwoTransports:
    def test_cli_and_api_for_one_model_name_resolve_to_different_rates(self, tmp_path):
        _load(
            tmp_path,
            {
                "models": {
                    "enabled": ["openai/zeta-9/cli", "openai/zeta-9/api"],
                    "custom": {
                        "openai/zeta-9/cli": _custom(
                            "zeta-9",
                            "cli",
                            runner="codex",
                            cost={"input_per_mtok": 1.0, "output_per_mtok": 2.0},
                        ),
                        "openai/zeta-9/api": _custom(
                            "zeta-9",
                            "api",
                            cost={"input_per_mtok": 10.0, "output_per_mtok": 20.0},
                        ),
                    },
                },
                "budget_usd": 50.0,
            },
        )
        cli = _estimate_cost("openai", "zeta-9", 1_000_000, 1_000_000, transport="cli")
        api = _estimate_cost("openai", "zeta-9", 1_000_000, 1_000_000, transport="api")
        assert cli == pytest.approx(3.0)
        assert api == pytest.approx(30.0)


class TestProjectDeclaredPricingNeedsNoCodeChange:
    def test_a_model_priced_only_in_forge_yaml_reports_measured_cost(self, tmp_path):
        """The acceptance criterion, stated directly."""
        assert pricing_for("openai", "zeta-9") is None, "fixture model must be code-unknown"

        _load(
            tmp_path,
            {
                "models": {
                    "enabled": ["openai/zeta-9/api"],
                    "custom": {
                        "openai/zeta-9/api": _custom(
                            "zeta-9",
                            "api",
                            cost={"input_per_mtok": 6.0, "output_per_mtok": 12.0},
                        )
                    },
                },
                "budget_usd": 50.0,
            },
        )
        assert _estimate_cost(
            "openai", "zeta-9", 2_000_000, 1_000_000, transport="api"
        ) == pytest.approx(24.0)

    def test_declaring_a_price_does_not_mutate_the_shipped_registry(self, tmp_path):
        from theforge.config.models import AGENT_REGISTRY

        before = dict(AGENT_REGISTRY)
        _load(
            tmp_path,
            {
                "models": {
                    "enabled": ["openai/zeta-9/api"],
                    "custom": {
                        "openai/zeta-9/api": _custom(
                            "zeta-9",
                            "api",
                            cost={"input_per_mtok": 6.0, "output_per_mtok": 12.0},
                        )
                    },
                },
                "budget_usd": 50.0,
            },
        )
        assert dict(AGENT_REGISTRY) == before
        assert "openai/zeta-9/api" not in AGENT_REGISTRY


class TestUnpriceableCandidate:
    def test_it_is_reported_at_load_and_records_cost_none_at_runtime(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            _load(
                tmp_path,
                {
                    "models": {
                        "enabled": ["openai/no-price-at-all/api"],
                        "custom": {
                            "openai/no-price-at-all/api": _custom("no-price-at-all", "api")
                        },
                    },
                    "budget_usd": 50.0,
                },
            )
        messages = [record.getMessage() for record in caplog.records]
        named = [m for m in messages if "no-price-at-all" in m]
        assert named, messages
        assert "dev" in named[0], "the report must name the paths it is dispatched on"
        assert _estimate_cost("openai", "no-price-at-all", 1_000, 1_000, transport="api") is None

    def test_load_warns_rather_than_raising(self, tmp_path):
        """An unpriced model is allowed to run and record cost as unknown."""
        cfg = _load(
            tmp_path,
            {
                "models": {
                    "enabled": ["openai/no-price-at-all/api"],
                    "custom": {"openai/no-price-at-all/api": _custom("no-price-at-all", "api")},
                },
                "budget_usd": 50.0,
            },
        )
        assert cfg.dev_profile.model == "no-price-at-all"


# ── Identity replacement sites price the NEW identity ─────────────────


class TestReplacedProfilesPriceWhatTheyBecame:
    """The reason no candidate-construction path needs converting.

    Nothing carries a rate, so a profile produced by ``dataclasses.replace``,
    ``apply_model_info``, ``model_ref_to_profile`` or ``_make_fast_profile``
    cannot carry a stale one: ``identity_of`` reads the replaced profile.
    """

    @staticmethod
    def _registry() -> RateRegistry:
        cli = DispatchIdentity("openai", "zeta-9", "cli")
        api = DispatchIdentity("openai", "zeta-9", "api")
        other = DispatchIdentity("openai", "zeta-10", "api")
        return RateRegistry(
            entries={
                cli: RateEntry(cli, ModelRates(1.0, 1.0), AccountingMode.TOKEN_ESTIMATED),
                api: RateEntry(api, ModelRates(2.0, 2.0), AccountingMode.TOKEN_ESTIMATED),
                other: RateEntry(other, ModelRates(3.0, 3.0), AccountingMode.TOKEN_ESTIMATED),
            }
        )

    def test_a_transport_swap_prices_the_new_transport(self):
        import dataclasses

        profile = ModelProfile(
            name="dev",
            model="zeta-9",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            cli="codex",
        )
        swapped = dataclasses.replace(profile, cli=None, provider="openai")
        with rr.scoped_registry(self._registry()):
            assert su.entry_for_profile(profile).rates == ModelRates(1.0, 1.0)
            assert su.entry_for_profile(swapped).rates == ModelRates(2.0, 2.0)

    def test_a_model_swap_prices_the_new_model(self):
        import dataclasses

        profile = ModelProfile(
            name="dev",
            model="zeta-9",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            provider="openai",
        )
        swapped = dataclasses.replace(profile, model="zeta-10")
        with rr.scoped_registry(self._registry()):
            assert su.entry_for_profile(profile).rates == ModelRates(2.0, 2.0)
            assert su.entry_for_profile(swapped).rates == ModelRates(3.0, 3.0)

    def test_apply_model_info_result_prices_the_model_it_applied(self):
        from theforge.config.model_identity import AgentSpec, RoutingPolicy, TransportSpec
        from theforge.config.models import _spec_to_model_info, apply_model_info

        profile = ModelProfile(
            name="dev",
            model="zeta-9",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
            provider="openai",
        )
        spec = AgentSpec(
            provider="openai",
            model="zeta-10",
            transport=TransportSpec(kind="api", runner="openai"),
            routing=RoutingPolicy(tier="strong", capability=9, cost_rank=2),
        )
        applied = apply_model_info(profile, _spec_to_model_info(spec))
        with rr.scoped_registry(self._registry()):
            assert identity_of(applied) == DispatchIdentity("openai", "zeta-10", "api")
            assert su.entry_for_profile(applied).rates == ModelRates(3.0, 3.0)


# ── Partial identities, and the no-registry baseline ──────────────────


class TestPartialIdentities:
    def test_a_profile_with_no_provider_or_transport_has_no_identity(self):
        profile = ModelProfile(
            name="bare",
            model="whatever",
            budget_usd=1.0,
            timeout_seconds=1,
            allowed_tools=(),
        )
        assert profile.provider_family is None
        assert identity_of(profile) is None

    def test_an_unresolvable_identity_is_unpriced_rather_than_a_partial_key(self):
        assert make_identity(None, "m", "cli") is None
        assert make_identity("openai", "m", None) is None
        entry = rr.resolve(None)
        assert entry.priced is False
        assert entry.identity is None

    def test_an_unresolvable_profile_is_not_enumerated(self):
        profile = ModelProfile(
            name="bare",
            model="whatever",
            budget_usd=1.0,
            timeout_seconds=1,
            allowed_tools=(),
        )
        config = type("_Cfg", (), {"dev_profile": profile})()
        assert reachable_identities(config) == ()


class TestNoRegistryBaseline:
    def test_uninstalled_registry_reproduces_pricing_for_exactly(self):
        with rr.scoped_registry(None):
            for provider, model in (
                ("openai", "gpt-5.4"),
                ("anthropic", "claude-sonnet-4-6"),
                ("google", "gemini-2.5-flash"),
                ("openai", "codestral"),
            ):
                for transport in ("cli", "api", None):
                    assert su.rates_for(provider, model, transport) == pricing_for(provider, model)

    def test_estimate_cost_without_a_transport_takes_the_baseline(self):
        with rr.scoped_registry(None):
            assert _estimate_cost("openai", "gpt-5.4", 1_000_000, 0) == pytest.approx(
                _estimate_cost("openai", "gpt-5.4", 1_000_000, 0, transport="api")
            )


class TestInstallContract:
    def test_last_install_wins_and_scoping_restores(self):
        first = RateRegistry(entries={})
        second = RateRegistry(
            entries={
                DispatchIdentity("openai", "m", "api"): RateEntry(
                    DispatchIdentity("openai", "m", "api"), ModelRates(1.0, 1.0)
                )
            }
        )
        with rr.scoped_registry(first):
            assert rr.active() is first
            with rr.scoped_registry(second):
                assert rr.active() is second
            assert rr.active() is first
        assert rr.active() is None

    def test_a_load_that_raises_leaves_no_registry_installed(self, tmp_path):
        """Installation happens only after the ForgeConfig is fully constructed."""
        assert rr.active() is None
        with pytest.raises(Exception):
            _load(tmp_path, {"models": ["openai/definitely-not-a-model/api"], "budget_usd": 50.0})
        assert rr.active() is None


# ── Accounting capability, per transport ──────────────────────────────


class TestAccountingModes:
    def test_claude_cli_is_provider_reported_and_needs_no_rate_card(self):
        mode = rr.accounting_mode_for("cli", "claude")
        assert mode is AccountingMode.PROVIDER_REPORTED
        assert mode.measures_without_rates
        assert not mode.needs_rates
        assert RateEntry(None, None, mode).accountable

    def test_ghaw_measures_spend_independently_of_any_rate_card(self):
        """gh-aw converts AI-credit consumption, so it must not draw a missing-rate
        warning for a transport that already measures (runner_ghaw._parse_agent_usage)."""
        mode = rr.accounting_mode_for("cli", "ghaw")
        assert mode is AccountingMode.INDEPENDENTLY_MEASURED
        assert RateEntry(None, None, mode).accountable
        assert RateEntry(None, None, mode).unaccountable_reason() is None

    def test_api_transports_need_a_rate_card(self):
        mode = rr.accounting_mode_for("api", "openai")
        assert mode is AccountingMode.TOKEN_ESTIMATED
        assert not RateEntry(None, None, mode).accountable
        assert RateEntry(None, ModelRates(1.0, 1.0), mode).accountable

    def test_gemini_cli_measures_only_when_the_cli_reports_usage(self):
        assert rr.accounting_mode_for("cli", "gemini") is (
            AccountingMode.TOKEN_ESTIMATED_IF_REPORTED
        )

    def test_an_unrecognised_cli_is_unmeasurable_not_assumed_estimable(self):
        mode = rr.accounting_mode_for("cli", "some-new-binary")
        assert mode is AccountingMode.UNMEASURABLE
        entry = RateEntry(None, ModelRates(1.0, 1.0), mode)
        assert not entry.accountable, "rates alone do not make spend measurable"
        assert "reports no usage" in entry.unaccountable_reason()


class TestThinkingSpendCapture:
    """``_thinking_spend_captured`` no longer uses table membership as a proxy."""

    @staticmethod
    def _profile(**kwargs) -> ModelProfile:
        base = dict(
            name="dev",
            model="zeta-9",
            budget_usd=10.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        base.update(kwargs)
        return ModelProfile(**base)

    def test_a_provider_reporting_transport_captures_spend_without_rates(self):
        from theforge.assignment import _thinking_spend_captured
        from theforge.routing import EffortKnob

        knob = EffortKnob(kind="token_budget", captures_thinking_spend=True)
        profile = self._profile(cli="claude", model="sonnet")
        identity = identity_of(profile)
        registry = RateRegistry(
            entries={
                identity: RateEntry(
                    identity, None, AccountingMode.PROVIDER_REPORTED, RateSource.NONE
                )
            }
        )
        with rr.scoped_registry(registry):
            assert _thinking_spend_captured(profile, knob) is True

    def test_an_unmeasurable_transport_does_not_capture_spend_despite_rates(self):
        from theforge.assignment import _thinking_spend_captured
        from theforge.routing import EffortKnob

        knob = EffortKnob(kind="token_budget", captures_thinking_spend=True)
        profile = self._profile(provider="openai")
        identity = identity_of(profile)
        registry = RateRegistry(
            entries={
                identity: RateEntry(
                    identity,
                    ModelRates(1.0, 1.0),
                    AccountingMode.UNMEASURABLE,
                    RateSource.PROJECT,
                )
            }
        )
        with rr.scoped_registry(registry):
            assert _thinking_spend_captured(profile, knob) is False


# ── Behaviour that must NOT change ────────────────────────────────────


class TestUnchangedBehaviour:
    def test_claude_cli_self_reported_cost_still_wins(self):
        """The CLI's billed total is untouched by any of this."""
        from theforge.runners import runner_claude

        assert runner_claude._resolve_anthropic_pricing_key("claude-sonnet-4-6") is not None
        # A dated id still resolves to its family entry.
        assert (
            runner_claude._resolve_anthropic_pricing_key("claude-sonnet-4-6-20260101")
            == "claude-sonnet-4-6"
        )

    def test_anthropic_kill_path_pricing_matches_the_packaged_rates(self):
        from theforge.runners.runner_claude import _estimate_anthropic_cost

        cost = _estimate_anthropic_cost("claude-sonnet-4-6", 1_000_000, 1_000_000, 0, 0)
        rates = pricing_for("anthropic", "claude-sonnet-4-6")
        assert cost == pytest.approx(rates.input_per_mtok + rates.output_per_mtok)

    def test_a_cli_price_cannot_reach_the_anthropic_kill_path_reconstruction(self):
        """The dated-id prefix match runs against CLI keys only."""
        from theforge.runners.runner_claude import _estimate_anthropic_cost

        api_only = DispatchIdentity("anthropic", "claude-invented-9", "api")
        registry = RateRegistry(entries={api_only: RateEntry(api_only, ModelRates(999.0, 999.0))})
        with rr.scoped_registry(registry):
            assert _estimate_anthropic_cost("claude-invented-9", 1_000_000, 0, 0, 0) is None

    def test_a_model_carrying_a_rate_card_prices_as_before(self):
        with rr.scoped_registry(None):
            assert _estimate_cost(
                "deepseek", "deepseek-v4-pro", 1_000_000, 0, transport="api"
            ) == pytest.approx(pricing_for("deepseek", "deepseek-v4-pro").input_per_mtok)


class TestGeminiCliAccounting:
    def test_reported_usage_is_priced_from_the_gemini_cli_identity(self):
        from theforge.runners.runner_gemini import _parse_gemini_usage

        cli = DispatchIdentity("google", "gemini-2.5-pro", "cli")
        api = DispatchIdentity("google", "gemini-2.5-pro", "api")
        registry = RateRegistry(
            entries={
                cli: RateEntry(cli, ModelRates(1.0, 2.0), AccountingMode.TOKEN_ESTIMATED),
                api: RateEntry(api, ModelRates(500.0, 500.0), AccountingMode.TOKEN_ESTIMATED),
            }
        )
        result_json = {
            "response": "ok",
            "stats": {
                "models": {
                    "gemini-2.5-pro": {
                        "tokens": {"prompt": 1_000_000, "candidates": 1_000_000, "cached": 0}
                    }
                }
            },
        }
        profile = ModelProfile(
            name="r",
            model="gemini-2.5-pro",
            budget_usd=1.0,
            timeout_seconds=1,
            allowed_tools=(),
            cli="gemini",
        )
        with rr.scoped_registry(registry):
            cost, usage = _parse_gemini_usage(result_json, profile)
        assert cost == pytest.approx(3.0), "must use the CLI rate, not the API rate"
        assert len(usage) == 1
        assert usage[0].cost_provenance == "estimated"

    def test_absent_usage_stays_unknown_rather_than_fabricated(self):
        from theforge.runners.runner_gemini import _parse_gemini_usage

        profile = ModelProfile(
            name="r",
            model="gemini-2.5-pro",
            budget_usd=1.0,
            timeout_seconds=1,
            allowed_tools=(),
            cli="gemini",
        )
        assert _parse_gemini_usage({"response": "ok"}, profile) == (None, ())
        assert _parse_gemini_usage({"stats": {"models": {}}}, profile) == (None, ())

    def test_unpriced_reported_usage_records_tokens_with_cost_unknown(self):
        from theforge.runners.runner_gemini import _parse_gemini_usage

        profile = ModelProfile(
            name="r",
            model="mystery",
            budget_usd=1.0,
            timeout_seconds=1,
            allowed_tools=(),
            cli="gemini",
        )
        result_json = {
            "stats": {"models": {"mystery": {"tokens": {"prompt": 10, "candidates": 5}}}}
        }
        with rr.scoped_registry(RateRegistry(entries={})):
            cost, usage = _parse_gemini_usage(result_json, profile)
        assert cost is None
        assert usage[0].input_tokens == 10
        assert usage[0].cost_provenance == "unknown"


class TestRateRegistryIsAnImportLeaf:
    """The registry module must not reach back into ``theforge`` at all.

    Configuration load compiles the registry, so ``theforge.config`` imports
    this module. Anything it imported — even lazily, inside a function — would
    become reachable from ``theforge.config`` and put the pricing key type in a
    cycle with half the runners package, which is exactly what the first
    iteration of this change did. The catalog-backed resolution that genuinely
    needs ``theforge.config.models`` lives in ``schema_utils`` instead, one
    direction only.
    """

    def test_no_theforge_import_anywhere_in_the_module(self):
        import ast
        from pathlib import Path

        from theforge.runners import rate_registry

        source = Path(rate_registry.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    alias.name for alias in node.names if alias.name.startswith("theforge")
                )
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    # A relative import is a theforge import by definition.
                    offenders.append(f".{node.module or ''}")
                elif node.module and node.module.startswith("theforge"):
                    offenders.append(node.module)
        # TYPE_CHECKING-only imports are erased at runtime and create no edge,
        # but the convention checker walks the AST, so they count too.
        assert offenders == [], f"rate_registry must stay an import leaf; found {offenders}"

    def test_schema_utils_re_exports_the_moved_names(self):
        """Existing imports keep working after the pricing types moved.

        ``PRICING_TABLE`` is deliberately absent from both: the packaged rate
        dictionary was removed in #2388, so there is nothing to re-export.
        """
        from theforge.runners import rate_registry, schema_utils

        assert schema_utils.ModelRates is rate_registry.ModelRates
        assert schema_utils.CACHED_INPUT_RATE_MULT == rate_registry.CACHED_INPUT_RATE_MULT
        assert not hasattr(rate_registry, "PRICING_TABLE")
        assert not hasattr(schema_utils, "PRICING_TABLE")
