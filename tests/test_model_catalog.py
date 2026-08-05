"""One data-backed model catalog on a single schema (#2204).

The shipped default set is data (``config/data/models.yaml``) read through the
same canonical parser that reads project-declared models, so both sources
produce identical structures. These tests pin the properties that make that
claim load-bearing:

- the packaged defaults really do come from data, through the shared parser;
- the shapes projects already write (``models.custom`` flat mappings, inline
  ``models.enabled`` mappings) still load unchanged;
- a project declaration can express every field the shipped set can;
- a provider with no adapter fails at load time, naming the adapters that exist;
- where a project declaration overlays a shipped definition, the resolved entry
  reports which source supplied each field.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.cli.check_config import cmd_check_config
from theforge.config import load_config
from theforge.config.model_catalog import (
    PROVENANCE_FIELDS,
    SOURCE_BUILTIN,
    SOURCE_PROJECT,
    load_packaged_catalog,
    parse_definition,
    resolve_packaged,
    resolve_project,
)
from theforge.config.models import AGENT_REGISTRY, transport_for
from theforge.config.pricing import (
    COST_BAND_BASIS_DECLARED_POLICY,
    COST_BAND_BASIS_VENDOR_TIER,
    PRICING_PROVENANCE_OPERATOR_DECLARED,
)


def _write(tmp_path: Path, body: dict) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.dump(body), encoding="utf-8")
    return path


# ── The shipped set is data ───────────────────────────────────────────────


class TestPackagedCatalog:
    def test_the_builtin_registry_is_what_the_catalog_data_says(self):
        """AGENT_REGISTRY is not a literal — it is the packaged catalog loaded."""
        assert load_packaged_catalog() == AGENT_REGISTRY
        assert AGENT_REGISTRY  # and the data is actually there

    def test_catalog_data_ships_inside_the_package(self):
        catalog = Path(__file__).resolve().parents[1] / "src/theforge/config/data/models.yaml"
        assert catalog.is_file()
        document = yaml.safe_load(catalog.read_text(encoding="utf-8"))
        assert len(document["models"]) == len(AGENT_REGISTRY)

    def test_shipped_pricing_attribution_survives_the_data_round_trip(self):
        """The vendor shorthands stay unattributed with a declared band basis.

        Inferring operator attribution from the presence of a ``cost:`` block —
        which is right for a project declaration — would re-band these off
        literals nothing can vouch for, the exact defect #2217 fixed.
        """
        sonnet = AGENT_REGISTRY["anthropic/sonnet/cli"]
        assert sonnet.pricing_provenance is None
        assert sonnet.routing.cost_rank_basis == COST_BAND_BASIS_VENDOR_TIER
        assert sonnet.cost_rank == 1
        reasoner = AGENT_REGISTRY["deepseek/deepseek-reasoner/api"]
        assert reasoner.routing.cost_rank_basis == COST_BAND_BASIS_DECLARED_POLICY
        assert AGENT_REGISTRY["openai/gpt-5.4/cli"].pricing_provenance == "gpt-5.4"

    def test_a_new_shipped_entry_is_a_data_edit_not_a_code_edit(self):
        """Re-pinning a model on an already-supported adapter adds no code."""
        definition = {
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "transport": {"kind": "cli"},
            "routing": {"tier": "strong", "capability": 10, "cost_rank": 3},
            "cost": {
                "input_per_mtok": 15.0,
                "output_per_mtok": 75.0,
                "pricing_provenance": "claude-opus-4-6",
            },
        }
        resolved = resolve_packaged(parse_definition(definition, where="x"), where="x")
        assert resolved.canonical_id == "anthropic/claude-opus-4-6/cli"
        assert resolved.spec.transport == transport_for("anthropic", "cli")
        assert resolved.spec.routing.cost_rank_basis == "price:claude-opus-4-6"

    def test_a_shipped_band_with_no_traceable_source_is_refused(self):
        """The packaged data is held to the same attribution rule code was."""
        definition = {
            "provider": "anthropic",
            "model": "shorthand",
            "transport": {"kind": "cli"},
            "routing": {"tier": "strong", "capability": 10, "cost_rank": 3},
            "cost": {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
        }
        with pytest.raises(ValueError, match="cannot be attributed"):
            resolve_packaged(parse_definition(definition, where="x"), where="x")

    def test_one_schema_governs_both_sources(self):
        """The same definition text resolves to the same routing either way."""
        definition = {
            "provider": "openai",
            "model": "gpt-5.9",
            "transport": {"kind": "api"},
            "routing": {"tier": "strong", "capability": 9, "cost_rank": 2},
            "cost": {
                "input_per_mtok": 1.25,
                "output_per_mtok": 10.0,
                "pricing_provenance": "gpt-5.9",
            },
        }
        packaged = resolve_packaged(parse_definition(definition, where="x"), where="x")
        project = resolve_project(parse_definition(definition, where="x"), where="x", builtin=None)
        assert packaged.canonical_id == project.canonical_id
        assert packaged.spec.routing == project.spec.routing
        assert packaged.spec.base_url == project.spec.base_url
        assert packaged.spec.pricing_provenance == project.spec.pricing_provenance
        # Only the entry-level source differs — that is what it is for.
        assert (packaged.spec.registry_source, project.spec.registry_source) == (
            SOURCE_BUILTIN,
            SOURCE_PROJECT,
        )


# ── Adapter validation ────────────────────────────────────────────────────


class TestAdapterValidation:
    def test_a_provider_with_no_adapter_names_the_ones_that_exist(self):
        definition = {
            "provider": "mistral",
            "model": "large",
            "transport": {"kind": "api"},
            "routing": {"tier": "strong"},
        }
        with pytest.raises(ValueError, match="No API adapter for provider 'mistral'"):
            parse_definition(definition, where="models.enabled[0]")

    def test_an_unsupported_provider_in_models_custom_names_the_adapters(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli"],
                    "custom": {
                        "mistral/large/api": {
                            "provider": "mistral",
                            "model": "large",
                            "tier": "strong",
                            "input_cost_per_mtok": 2.0,
                            "output_cost_per_mtok": 6.0,
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        with pytest.raises(ValueError, match="Known providers/adapters:"):
            load_config(path)

    def test_an_unsupported_provider_in_enabled_fails_at_load(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        {
                            "provider": "mistral",
                            "model": "large",
                            "transport": {"kind": "api"},
                            "routing": {"tier": "strong"},
                        }
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        with pytest.raises(ValueError, match="No API adapter for provider 'mistral'"):
            load_config(path)


# ── Backward compatibility ────────────────────────────────────────────────


class TestExistingShapesStillLoad:
    def test_models_custom_flat_mapping_is_unchanged(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "openai/gpt-5.5/cli"],
                    "custom": {
                        "gpt-5.5": {
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "tier": "strong",
                            "input_cost_per_mtok": 1.5,
                            "output_cost_per_mtok": 12.0,
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        spec = (config.model_registry or {})["openai/gpt-5.5/cli"]
        assert spec.tier == "strong"
        assert spec.capability == 9  # still derived from tier
        assert spec.dev_capable is True
        assert spec.cost_rank == 2  # banded from the operator-declared price
        assert spec.pricing_provenance == PRICING_PROVENANCE_OPERATOR_DECLARED
        assert config.model_registry_sources["openai/gpt-5.5/cli"] == "forge.yaml"

    def test_models_enabled_can_still_select_a_custom_declaration_by_its_key(self, tmp_path):
        """The operator-chosen declaration key stays a valid selector."""
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "gpt-5.5"],
                    "custom": {
                        "gpt-5.5": {
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "tier": "strong",
                            "input_cost_per_mtok": 1.5,
                            "output_cost_per_mtok": 12.0,
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert "openai/gpt-5.5/cli" in config.models
        assert "gpt-5.5" not in config.models

    def test_models_custom_provider_alias_tokens_still_normalize(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "local"],
                    "custom": {
                        "local": {
                            "provider": "openai-api",
                            "model": "qwen3",
                            "tier": "fast",
                            "base_url": "http://localhost:11434/v1",
                            "input_cost_per_mtok": 0.0,
                            "output_cost_per_mtok": 0.0,
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        spec = (config.model_registry or {})["openai/qwen3/api"]
        assert spec.transport == transport_for("openai", "api")
        assert spec.base_url == "http://localhost:11434/v1"

    def test_inline_enabled_mapping_is_unchanged(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        "anthropic/sonnet/cli",
                        {
                            "provider": "openai",
                            "model": "gpt-5.9",
                            "transport": {"kind": "api"},
                            "routing": {"tier": "strong"},
                            "cost": {"input_per_mtok": 30.0, "output_per_mtok": 60.0},
                        },
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        spec = (config.model_registry or {})["openai/gpt-5.9/api"]
        # A declared cost with no stated attribution is still attributed to the
        # operator's own declaration, and still bands from it.
        assert spec.pricing_provenance == PRICING_PROVENANCE_OPERATOR_DECLARED
        assert spec.cost_rank == 3

    def test_selecting_a_builtin_by_mapping_does_not_redeclare_it(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        {
                            "provider": "anthropic",
                            "model": "sonnet",
                            "transport": {"kind": "cli"},
                        },
                        "anthropic/opus/cli",
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        assert config.model_registry_sources["anthropic/sonnet/cli"] == "builtin"


# ── Expressiveness parity ─────────────────────────────────────────────────


class TestProjectDeclarationsAreAsExpressiveAsShipped:
    def test_a_project_definition_can_set_every_shipped_field(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        "anthropic/sonnet/cli",
                        {
                            "provider": "google",
                            "model": "gemini-4-pro",
                            "transport": {"kind": "api"},
                            "base_url": "https://example.invalid/v1",
                            "routing": {
                                "tier": "strong",
                                "capability": 10,
                                "cost_rank": 3,
                                "dev_capable": False,
                                "phase_eligibility": ["dev", "plan", "review"],
                                "cost_rank_basis": COST_BAND_BASIS_DECLARED_POLICY,
                            },
                            "cost": {
                                "input_per_mtok": 2.0,
                                "output_per_mtok": 12.0,
                                "pricing_provenance": "gemini-4-pro-2026-08",
                            },
                        },
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        spec = (load_config(path).model_registry or {})["google/gemini-4-pro/api"]
        assert spec.capability == 10  # not derivable from tier before this change
        assert spec.phase_eligibility == frozenset({"dev", "plan", "review"})
        assert spec.dev_capable is False
        assert spec.cost_rank == 3
        assert spec.routing.cost_rank_basis == COST_BAND_BASIS_DECLARED_POLICY
        assert spec.pricing_provenance == "gemini-4-pro-2026-08"
        assert spec.base_url == "https://example.invalid/v1"

    def test_a_declaration_may_state_its_price_is_unattributable(self, tmp_path):
        """The vendor-shorthand case a project could not express before."""
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
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
                                "cost_rank_basis": COST_BAND_BASIS_VENDOR_TIER,
                            },
                            "cost": {
                                "input_per_mtok": 30.0,
                                "output_per_mtok": 60.0,
                                "pricing_provenance": None,
                            },
                        },
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        spec = (load_config(path).model_registry or {})["anthropic/haiku/cli"]
        assert spec.pricing_provenance is None
        # An unattributed literal must not band the entry.
        assert spec.cost_rank == 1
        assert spec.routing.cost_rank_basis == COST_BAND_BASIS_VENDOR_TIER


# ── Field-level provenance ────────────────────────────────────────────────


class TestFieldLevelProvenance:
    def test_a_partial_overlay_reports_which_source_supplied_each_field(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        "anthropic/sonnet/cli",
                        {
                            "provider": "openai",
                            "model": "gpt-5.4",
                            "transport": {"kind": "cli"},
                            "routing": {"capability": 10},
                        },
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        sources = config.model_registry_field_sources["openai/gpt-5.4/cli"]
        assert sources["capability"] == SOURCE_PROJECT
        assert sources["tier"] == SOURCE_BUILTIN
        assert sources["input_cost_per_mtok"] == SOURCE_BUILTIN
        assert sources["pricing_provenance"] == SOURCE_BUILTIN
        assert sources["phase_eligibility"] == SOURCE_BUILTIN
        assert set(sources) == set(PROVENANCE_FIELDS)
        # The overlaid entry keeps the built-in values it did not restate.
        spec = (config.model_registry or {})["openai/gpt-5.4/cli"]
        assert spec.capability == 10
        assert spec.tier == "strong"
        assert spec.input_cost_per_mtok == 1.25

    def test_check_config_names_the_fields_each_source_supplied(self, tmp_path, capsys):
        """The provenance map has a consumer: the operator reading check-config."""
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": [
                        "anthropic/sonnet/cli",
                        {
                            "provider": "openai",
                            "model": "gpt-5.4",
                            "transport": {"kind": "cli"},
                            "routing": {"capability": 10},
                        },
                    ]
                },
                "budget_usd": 30.0,
                "workspace": {
                    "create_command": "true",
                    "path_pattern": str(tmp_path / "{slug}"),
                    "branch_pattern": "x/{slug}",
                },
                "validation": {"gate_command": "true"},
            },
        )
        args = argparse.Namespace(config=str(path))
        with patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")):
            cmd_check_config(args)
        out = capsys.readouterr().out
        assert "field provenance for openai/gpt-5.4/cli:" in out
        assert "forge.yaml: capability" in out
        assert "tier" in out.split("builtin:")[-1]

    def test_a_shipped_entry_reports_every_field_as_builtin(self, tmp_path):
        path = _write(
            tmp_path,
            {"project": "p", "models": ["anthropic/sonnet/cli"], "budget_usd": 30.0},
        )
        sources = load_config(path).model_registry_field_sources["anthropic/sonnet/cli"]
        assert set(sources.values()) == {SOURCE_BUILTIN}

    def test_a_standalone_project_declaration_owns_every_field(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "gpt-5.5"],
                    "custom": {
                        "gpt-5.5": {
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "tier": "strong",
                            "input_cost_per_mtok": 1.5,
                            "output_cost_per_mtok": 12.0,
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        sources = load_config(path).model_registry_field_sources["openai/gpt-5.5/cli"]
        assert set(sources.values()) == {SOURCE_PROJECT}
