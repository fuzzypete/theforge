"""Promotion of validated project-only models into the shipped catalog (#2252).

``openai/gpt-5.5/cli``, ``google/gemini-3.5-flash/api`` and ``anthropic/haiku/cli``
existed only as ``forge.yaml`` declarations even though this project routes to
them daily, so a consumer enabling the shipped defaults could not reach them.

Promotion leaves this project's own configuration holding a duplicate of each
(removing them is separate operator work, sequenced after a release carrying the
catalog entries). These tests pin both halves of that: the entries resolve from
the catalog alone, and the duplicate declarations left behind are resolved
*visibly* — including the one whose presence genuinely changes routing.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.cli.check_config import cmd_check_config
from theforge.config import load_config
from theforge.config.model_catalog import load_packaged_catalog
from theforge.config.models import AGENT_REGISTRY
from theforge.config.pricing import price_tiebreak_signal_for
from theforge.config.role_derivation import derive_roles

PROMOTED = ("openai/gpt-5.5/cli", "google/gemini-3.5-flash/api", "anthropic/haiku/cli")


def _write(tmp_path: Path, body: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.dump(body), encoding="utf-8")
    return path


def _runnable(tmp_path: Path, models: dict | list) -> dict:
    return {
        "project": "p",
        "models": models,
        "budget_usd": 30.0,
        "workspace": {
            "create_command": "true",
            "path_pattern": str(tmp_path / "{slug}"),
            "branch_pattern": "x/{slug}",
        },
        "validation": {"gate_command": "true"},
    }


# ── The entries are reachable from the shipped defaults ───────────────────


class TestReachableFromShippedDefaults:
    def test_the_three_identities_resolve_from_the_packaged_catalog_alone(self):
        catalog = load_packaged_catalog()
        assert catalog == AGENT_REGISTRY
        for model_id in PROMOTED:
            assert model_id in catalog, model_id

    def test_each_promoted_entry_carries_the_policy_it_was_validated_under(self):
        catalog = load_packaged_catalog()

        gpt_55 = catalog["openai/gpt-5.5/cli"]
        assert (gpt_55.tier, gpt_55.capability, gpt_55.cost_rank) == ("strong", 9, 3)
        assert (gpt_55.input_cost_per_mtok, gpt_55.output_cost_per_mtok) == (5.00, 30.00)
        assert gpt_55.pricing_provenance == "gpt-5.5"

        flash = catalog["google/gemini-3.5-flash/api"]
        assert (flash.tier, flash.capability, flash.cost_rank) == ("strong", 9, 2)
        assert (flash.input_cost_per_mtok, flash.output_cost_per_mtok) == (1.50, 9.00)
        assert flash.pricing_provenance == "gemini-3.5-flash"

        haiku = catalog["anthropic/haiku/cli"]
        assert (haiku.tier, haiku.capability, haiku.cost_rank) == ("cheap", 6, 1)
        assert haiku.dev_capable is False
        assert haiku.phase_eligibility == frozenset({"preflight", "review"})
        # A CLI shorthand resolves at invocation, so its literal is unattributed
        # and routing ignores it — same rule sonnet/opus ship under.
        assert haiku.pricing_provenance is None

    def test_a_consumer_declaring_none_of_them_still_resolves_them(self, tmp_path):
        path = _write(tmp_path, _runnable(tmp_path, ["anthropic/sonnet/cli", *PROMOTED]))
        config = load_config(path)
        for model_id in PROMOTED:
            assert model_id in config.models
            assert config.model_registry_sources[model_id] == "builtin"
        assert config.model_registry_duplicates == ()

    def test_the_newest_openai_tier_is_no_longer_a_generation_behind(self):
        """The gap the story is about, stated as the property that closed it."""
        openai_cli = {
            spec.model
            for key, spec in AGENT_REGISTRY.items()
            if spec.provider == "openai" and spec.transport.kind == "cli"
        }
        assert "gpt-5.5" in openai_cli
        # And a cheap-tier Anthropic option now exists at all.
        assert any(
            spec.provider == "anthropic" and spec.tier == "cheap"
            for spec in AGENT_REGISTRY.values()
        )

    def test_an_unconfirmed_identifier_was_not_promoted(self):
        """The gpt-5.6 family is blocked pending an adapter fix — it must not ship."""
        models = {spec.model for spec in AGENT_REGISTRY.values()}
        assert not {m for m in models if m.startswith("gpt-5.6")}


# ── A duplicate declaration is resolved visibly, not silently ─────────────


class TestDuplicateDeclarationsAreReported:
    def _declared(self, tmp_path: Path, declaration: dict) -> dict:
        return _runnable(
            tmp_path,
            {
                "enabled": ["anthropic/sonnet/cli", "openai/gpt-5.5/cli"],
                "custom": {"openai/gpt-5.5/cli": declaration},
            },
        )

    def test_a_transcribed_declaration_loads_and_reports_the_attribution_it_moved(self, tmp_path):
        """The exact overlay shape this project's forge.yaml carries today.

        No ``override: true`` — promoting a model must not break a configuration
        that already declared it. But the entry is not silently equated with the
        shipped one: the declaration attributes the figures to ``forge.yaml``,
        and that is reported.
        """
        path = _write(
            tmp_path,
            self._declared(
                tmp_path,
                {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "transport": {"kind": "cli"},
                    "tier": "strong",
                    "input_cost_per_mtok": 5.00,
                    "output_cost_per_mtok": 30.00,
                },
            ),
        )
        config = load_config(path)
        (duplicate,) = config.model_registry_duplicates
        assert duplicate.canonical_id == "openai/gpt-5.5/cli"
        # Routing is unaffected: both sides attribute the same figures to
        # *something*, so the price tie-break reads the same number.
        assert not duplicate.routing_differs
        # But it is not claimed to be inert either.
        assert not duplicate.is_redundant
        assert {d.field for d in duplicate.attribution_differences} == {
            "pricing_provenance",
            "cost_rank_basis",
        }
        spec = (config.model_registry or {})["openai/gpt-5.5/cli"]
        assert spec.pricing_provenance == "forge.yaml"
        assert price_tiebreak_signal_for(spec) == price_tiebreak_signal_for(
            AGENT_REGISTRY["openai/gpt-5.5/cli"]
        )

    def test_a_declaration_that_changes_routing_still_needs_override(self, tmp_path):
        path = _write(
            tmp_path,
            self._declared(
                tmp_path,
                {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "transport": {"kind": "cli"},
                    "tier": "strong",
                    "input_cost_per_mtok": 1.5,
                    "output_cost_per_mtok": 12.0,
                },
            ),
        )
        with pytest.raises(ValueError, match="duplicates a built-in model id"):
            load_config(path)

    def test_the_refusal_names_the_routing_field_that_moved(self, tmp_path):
        path = _write(
            tmp_path,
            self._declared(
                tmp_path,
                {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "transport": {"kind": "cli"},
                    "tier": "strong",
                    "input_cost_per_mtok": 1.5,
                    "output_cost_per_mtok": 12.0,
                },
            ),
        )
        with pytest.raises(ValueError, match="cost_rank"):
            load_config(path)

    def test_a_routing_changing_duplicate_that_is_permitted_is_warned_about(
        self, tmp_path, caplog
    ):
        """An inline ``models.enabled`` mapping may overlay a shipped entry.

        It is permitted, so it must not raise — but the difference cannot be
        silent, which is the whole point of reporting rather than refusing.
        This is the haiku shape: a declared price on an entry the catalog ships
        unattributed.
        """
        path = _write(
            tmp_path,
            _runnable(
                tmp_path,
                {
                    "enabled": [
                        "anthropic/sonnet/cli",
                        {
                            "provider": "anthropic",
                            "model": "haiku",
                            "transport": {"kind": "cli"},
                            "routing": {
                                "tier": "cheap",
                                "capability": 6,
                                "cost_rank": 1,
                                "dev_capable": False,
                                "phase_eligibility": ["preflight", "review"],
                            },
                            "cost": {"input_per_mtok": 1.00, "output_per_mtok": 5.00},
                        },
                    ]
                },
            ),
        )
        with caplog.at_level("WARNING", logger="theforge.config"):
            config = load_config(path)
        (duplicate,) = config.model_registry_duplicates
        assert duplicate.canonical_id == "anthropic/haiku/cli"
        assert duplicate.routing_differs
        assert {d.field for d in duplicate.routing_differences} == {
            "effective_input_cost_per_mtok",
            "effective_output_cost_per_mtok",
        }
        assert "would change model selection" in caplog.text

    def test_check_config_states_which_duplicates_are_safe_to_remove(self, tmp_path, capsys):
        """The report has an operator-facing consumer, not just a struct."""
        path = _write(
            tmp_path,
            self._declared(
                tmp_path,
                {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "transport": {"kind": "cli"},
                    "tier": "strong",
                    "input_cost_per_mtok": 5.00,
                    "output_cost_per_mtok": 30.00,
                },
            ),
        )
        args = argparse.Namespace(config=str(path))
        with patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")):
            cmd_check_config(args)
        out = capsys.readouterr().out
        assert "also defined in the shipped catalog (forge.yaml wins):" in out
        assert "openai/gpt-5.5/cli" in out
        assert "differs only in attribution recorded" in out
        assert "pricing_provenance: forge.yaml='forge.yaml' builtin='gpt-5.5'" in out


# ── Seam: config resolution → routing ─────────────────────────────────────


class TestRoutingSeam:
    """Config→routing propagation, per the seam-test convention.

    The claim under test is behavioural, not structural: whether an operator who
    deletes a duplicate declaration gets the same selection as one who keeps it.
    """

    def _roles(self, path: Path):
        config = load_config(path)
        return (
            derive_roles(
                config.models,
                {},
                budget_usd=config.models_budget_usd or 30.0,
                complexity="MEDIUM",
                registry=config.model_registry,
            ),
            config,
        )

    def test_removing_an_attribution_only_duplicate_leaves_selection_unchanged(self, tmp_path):
        enabled = ["anthropic/sonnet/cli", "openai/gpt-5.5/cli", "openai/gpt-5.4-mini/cli"]
        with_declaration = _write(
            tmp_path / "a",
            _runnable(
                tmp_path,
                {
                    "enabled": enabled,
                    "custom": {
                        "openai/gpt-5.5/cli": {
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "transport": {"kind": "cli"},
                            "tier": "strong",
                            "input_cost_per_mtok": 5.00,
                            "output_cost_per_mtok": 30.00,
                        }
                    },
                },
            ),
        )
        without = _write(tmp_path / "b", _runnable(tmp_path, {"enabled": enabled}))

        kept, kept_config = self._roles(with_declaration)
        removed, _ = self._roles(without)

        def _picks(roles) -> tuple:
            return (
                roles.dev.ref.model,
                roles.preflight.ref.model,
                roles.plan.ref.model,
                tuple(p.ref.model for p in roles.review_pool),
            )

        assert _picks(kept) == _picks(removed)
        # And the config says so rather than leaving it to be discovered.
        assert not kept_config.model_registry_duplicates[0].routing_differs

    def test_the_haiku_duplicate_really_does_move_the_price_tiebreak(self, tmp_path):
        """Why the report exists: this one is not safe to delete silently."""
        path = _write(
            tmp_path,
            _runnable(
                tmp_path,
                {
                    "enabled": [
                        "anthropic/sonnet/cli",
                        {
                            "provider": "anthropic",
                            "model": "haiku",
                            "transport": {"kind": "cli"},
                            "cost": {"input_per_mtok": 1.00, "output_per_mtok": 5.00},
                        },
                    ]
                },
            ),
        )
        declared = (load_config(path).model_registry or {})["anthropic/haiku/cli"]
        shipped = AGENT_REGISTRY["anthropic/haiku/cli"]
        # Shipped: unattributed, so it sorts behind every priced cheap-tier peer.
        assert price_tiebreak_signal_for(shipped) == float("inf")
        # Declared: routing reads the figures, so it competes on price.
        assert price_tiebreak_signal_for(declared) == 5.0

    def test_a_catalog_only_consumer_gets_the_promoted_models_in_its_pool(self, tmp_path):
        """The consumer-facing point of promotion, at the routing seam."""
        path = _write(
            tmp_path,
            _runnable(tmp_path, ["anthropic/sonnet/cli", "openai/gpt-5.5/cli"]),
        )
        roles, _ = self._roles(path)
        chosen = {roles.dev.ref.model, roles.plan.ref.model, roles.preflight.ref.model} | {
            p.ref.model for p in roles.review_pool
        }
        assert "gpt-5.5" in chosen
