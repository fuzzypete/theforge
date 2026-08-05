"""One data-backed model catalog on a single schema (#2204).

The shipped default set is data (``config/data/models.yaml``) read through the
same canonical parser that reads project-declared models, so both sources
produce identical structures. These tests pin the properties that make that
claim load-bearing:

- the packaged defaults really do come from data, through the shared parser;
- the shapes projects already write (``models.custom`` flat mappings, inline
  ``models.enabled`` mappings) still load unchanged;
- a project declaration can express every field the shipped set can, on either
  surface — inline in ``models.enabled`` or reusable under ``models.custom``;
- a provider with no adapter fails at load time, naming the adapters that exist;
- where a project declaration overlays a shipped definition, the resolved entry
  reports which source supplied each field.
"""

from __future__ import annotations

import argparse
import ast
import zipfile
from importlib import resources
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from hatchling.builders.wheel import WheelBuilder

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


# ── Module layering ───────────────────────────────────────────────────────


class TestLayering:
    """The parser and the registry it builds must not import each other.

    ``models.py`` builds AGENT_REGISTRY by calling the parser at import time, so
    a back-edge from ``model_catalog`` to ``models`` is a genuine import cycle
    (and a hard convention violation), not merely untidy. Both sit on the
    ``model_identity`` leaf instead.
    """

    def _imports(self, module_name: str) -> set[str]:
        source = Path(__file__).resolve().parents[1] / f"src/theforge/config/{module_name}.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
        return found

    def test_the_parser_does_not_import_the_registry(self):
        # Every import, including function-level ones — a deferred import is
        # still a cycle to the convention checker and to a reader.
        assert "models" not in self._imports("model_catalog")

    def test_the_identity_leaf_imports_neither(self):
        identity_imports = self._imports("model_identity")
        assert "models" not in identity_imports
        assert "model_catalog" not in identity_imports

    def test_the_registry_still_re_exports_the_identity_surface(self):
        """Callers import these from config.models and must keep being able to."""
        from theforge.config import model_identity, models

        for name in (
            "TRANSPORT_KINDS",
            "TransportSpec",
            "RoutingPolicy",
            "AgentSpec",
            "transport_for",
            "canonical_model_id",
            "canonical_id_for_spec",
            "is_canonical_model_id",
            "provider_for_cli_runner",
            "provider_for_transport",
            "mirror_fields_for_transport",
            "transport_from_raw_fields",
            "known_model_overlay_providers",
            "overlay_transport",
            "custom_model_capability",
            "custom_model_dev_capable",
        ):
            assert getattr(models, name) is getattr(model_identity, name), name


# ── The shipped set is data ───────────────────────────────────────────────

# Routing policy of every entry the catalog shipped with, as ``(tier, capability,
# cost_rank, dev_capable, phase_eligibility)`` — the whole of RoutingPolicy that
# decides *which* model gets work, so a change to any of it changes dispatch for
# every project on the release.
#
# ``dev_capable`` and ``phase_eligibility`` are in here rather than left to the
# first three because they gate rather than rank: dropping a phase removes a
# model from that pool outright, and role derivation falls back to the unfiltered
# list when a filter empties a pool, so the removal shows up as a quietly
# different assignment rather than as an error.
#
# ``cost_rank_basis`` is deliberately excluded — it explains a band rather than
# selecting anything, and pinning it here would duplicate the attribution tests
# above.
#
# Asserted as a *subset*, which is what keeps the guard aligned with the point of
# #2204 rather than fighting it: adding a model stays a pure data edit and needs
# no test change, while re-tiering one of these fails until the expectation is
# updated in the same commit. A silent re-tier is the failure #2217 was about,
# and moving the set into YAML is what made it a one-line edit.
_ALL_PHASES = ("dev", "plan", "preflight", "review")
_SHIPPED_ROUTING: dict[str, tuple[str, int, int, bool, tuple[str, ...]]] = {
    "anthropic/opus/cli": ("strong", 10, 3, True, _ALL_PHASES),
    "anthropic/sonnet/cli": ("fast", 7, 1, True, _ALL_PHASES),
    "deepseek/deepseek-chat/api": ("fast", 7, 1, True, _ALL_PHASES),
    "deepseek/deepseek-reasoner/api": ("strong", 9, 2, True, _ALL_PHASES),
    "google/gemini-2.5-pro/api": ("strong", 8, 2, True, _ALL_PHASES),
    "google/gemini-2.5-pro/cli": ("strong", 8, 2, False, _ALL_PHASES),
    "google/gemini-3-flash-preview/api": ("cheap", 7, 1, True, _ALL_PHASES),
    "google/gemini-3-flash-preview/cli": ("cheap", 7, 1, False, _ALL_PHASES),
    "google/gemini-3.1-pro-preview/api": ("strong", 9, 2, True, _ALL_PHASES),
    "google/gemini-3.1-pro-preview/cli": ("strong", 9, 2, False, _ALL_PHASES),
    "openai/codestral/api": ("fast", 7, 1, True, _ALL_PHASES),
    "openai/deepseek-coder/api": ("fast", 7, 1, True, _ALL_PHASES),
    "openai/gpt-5.4-mini/api": ("cheap", 7, 1, True, _ALL_PHASES),
    "openai/gpt-5.4-mini/cli": ("cheap", 7, 1, True, _ALL_PHASES),
    # No preflight: the pro tier is kept out of the phase that runs before
    # complexity is known, so it cannot be drawn for cheap up-front work.
    "openai/gpt-5.4-pro/api": ("strong", 10, 3, True, ("dev", "plan", "review")),
    "openai/gpt-5.4-pro/cli": ("strong", 10, 3, True, _ALL_PHASES),
    "openai/gpt-5.4/api": ("strong", 9, 2, True, _ALL_PHASES),
    "openai/gpt-5.4/cli": ("strong", 9, 2, True, _ALL_PHASES),
    "openai/llama3.1/api": ("fast", 6, 1, True, _ALL_PHASES),
    "openai/qwen2.5-coder/api": ("fast", 7, 1, True, _ALL_PHASES),
}


class TestPackagedCatalog:
    def test_the_builtin_registry_is_what_the_catalog_data_says(self):
        """AGENT_REGISTRY is not a literal — it is the packaged catalog loaded."""
        assert load_packaged_catalog() == AGENT_REGISTRY
        assert AGENT_REGISTRY  # and the data is actually there

    def test_catalog_data_ships_inside_the_package(self):
        """Reach the catalog the way production does, not by source-tree path.

        ``importlib.resources`` is what :func:`load_packaged_catalog` uses, so
        resolving it here is what proves the file is addressable *as package
        data*. A source-tree ``Path(__file__).parents[1]`` lookup passes even
        after a move that makes the resource unreachable once installed.
        """
        resource = resources.files("theforge.config").joinpath("data/models.yaml")
        assert resource.is_file()
        document = yaml.safe_load(resource.read_text(encoding="utf-8"))
        assert len(document["models"]) == len(AGENT_REGISTRY)

    def test_the_built_wheel_carries_the_catalog(self, tmp_path):
        """Build a real wheel and look inside it.

        The catalog is read at import time, so if it stops travelling in the
        wheel then ``import theforge`` fails outright for an installed copy while
        CI, which imports from the source tree, stays green — the dogfood
        substrate would find it at the next cut, not here.

        Reading the build *declaration* instead is not equivalent, which is why
        this builds. Hatch resolves inclusion through several interacting keys —
        ``exclude`` takes precedence over inclusion, and ``only-include``
        restricts traversal — so a rule that never mentions the data directory
        can still drop it (``exclude = ["src/theforge/config/**"]``,
        ``only-include = ["src/theforge/cli"]``). The artifact is the only thing
        that answers the question the operator cares about.
        """
        builder = WheelBuilder(str(Path(__file__).resolve().parents[1]))
        artifact = next(iter(builder.build(directory=str(tmp_path), versions=["standard"])))
        with zipfile.ZipFile(artifact) as wheel:
            names = set(wheel.namelist())
        assert "theforge/config/data/models.yaml" in names, (
            "the shipped catalog is missing from the built wheel — an installed "
            f"copy would fail at import. Wheel contains {len(names)} entries."
        )

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

    def test_every_shipped_entry_keeps_the_routing_it_shipped_with(self):
        """Transcribing the set into YAML must not have moved any dispatch.

        ``test_the_builtin_registry_is_what_the_catalog_data_says`` cannot see a
        drift here — ``AGENT_REGISTRY`` *is* ``load_packaged_catalog()``, so it
        compares two loads of the same file and holds whatever that file says.
        This is the assertion that holds the file to something.
        """

        def actual(spec) -> tuple:
            return (
                spec.routing.tier,
                spec.capability,
                spec.cost_rank,
                spec.routing.dev_capable,
                tuple(sorted(spec.routing.phase_eligibility)),
            )

        drifted = {
            model_id: (actual(spec), expected)
            for model_id, expected in _SHIPPED_ROUTING.items()
            if (spec := AGENT_REGISTRY.get(model_id)) is not None and actual(spec) != expected
        }
        assert not drifted, f"shipped routing changed (got, expected): {drifted}"
        missing = sorted(_SHIPPED_ROUTING.keys() - AGENT_REGISTRY.keys())
        assert not missing, f"shipped entries disappeared from the catalog: {missing}"

    @pytest.mark.parametrize(
        "routing,expected",
        [
            ({"tier": "banana", "capability": 7, "cost_rank": 1}, "must be one of"),
            ({"tier": "cheap", "capability": 999, "cost_rank": 1}, "between 1 and 10"),
            ({"tier": "cheap", "capability": 7, "cost_rank": -8}, "between 1 and 3"),
            (
                {
                    "tier": "cheap",
                    "capability": 7,
                    "cost_rank": 1,
                    "phase_eligibility": ["launch"],
                },
                "only supports",
            ),
        ],
    )
    def test_routing_values_outside_their_domain_are_refused(self, routing, expected):
        """Well-typed nonsense steers dispatch just as well as a real value.

        Each of these parsed cleanly before: ``tier`` fell through to whatever
        read it, an out-of-scale ``capability`` sorted the model to the top of
        every candidate list, and an unrecognized phase is the quietest of the
        four — ``_phase_candidates`` falls back to the unfiltered list when a
        filter empties a pool, so a misspelled phase removes the model from the
        pool it was declared for without failing anything.
        """
        definition = {
            "provider": "openai",
            "model": "m",
            "transport": {"kind": "api"},
            "routing": routing,
        }
        with pytest.raises(ValueError, match=expected):
            parse_definition(definition, where="x")

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

    def test_models_custom_accepts_the_canonical_schema(self, tmp_path):
        """A reusable declaration is as expressive as an inline one.

        Before #2204 the only fully expressive surface was an inline
        ``models.enabled`` mapping — a selection list, not a definition surface.
        """
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "fast-reviewer"],
                    "custom": {
                        "fast-reviewer": {
                            "provider": "google",
                            "model": "gemini-4-flash",
                            "transport": {"kind": "api"},
                            "base_url": "https://example.invalid/v1",
                            "routing": {
                                "tier": "cheap",
                                "capability": 8,
                                "cost_rank": 1,
                                "dev_capable": False,
                                "phase_eligibility": ["review"],
                                "cost_rank_basis": COST_BAND_BASIS_DECLARED_POLICY,
                            },
                            "cost": {
                                "input_per_mtok": 0.30,
                                "output_per_mtok": 2.50,
                                "pricing_provenance": "gemini-4-flash-2026-08",
                            },
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        spec = (config.model_registry or {})["google/gemini-4-flash/api"]
        assert spec.capability == 8
        assert spec.phase_eligibility == frozenset({"review"})
        assert spec.dev_capable is False
        assert spec.routing.cost_rank_basis == COST_BAND_BASIS_DECLARED_POLICY
        assert spec.pricing_provenance == "gemini-4-flash-2026-08"
        assert spec.base_url == "https://example.invalid/v1"
        # And the operator-chosen key still selects it.
        assert "google/gemini-4-flash/api" in config.models
        assert "fast-reviewer" not in config.models

    def test_a_canonical_models_custom_declaration_may_override_a_shipped_identity(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "pinned-opus"],
                    "custom": {
                        "pinned-opus": {
                            "provider": "anthropic",
                            "model": "opus",
                            "transport": {"kind": "cli"},
                            "override": True,
                            "routing": {"tier": "strong", "capability": 9, "cost_rank": 3},
                            "cost": {
                                "input_per_mtok": 15.0,
                                "output_per_mtok": 75.0,
                                "pricing_provenance": "claude-opus-4-6",
                            },
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        spec = (config.model_registry or {})["anthropic/opus/cli"]
        assert spec.capability == 9  # the shipped entry says 10
        assert spec.pricing_provenance == "claude-opus-4-6"
        assert config.model_registry_sources["anthropic/opus/cli"] == "forge.yaml"

    def test_an_unsupported_provider_in_a_canonical_custom_declaration_is_rejected(self, tmp_path):
        """Adapter validation reaches the canonical models.custom path too."""
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli"],
                    "custom": {
                        "mistral": {
                            "provider": "mistral",
                            "model": "large",
                            "transport": {"kind": "api"},
                            "routing": {"tier": "strong"},
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        with pytest.raises(ValueError, match="No API adapter for provider 'mistral'"):
            load_config(path)

    def test_mixing_the_flat_and_canonical_shapes_is_rejected(self, tmp_path):
        """Two spellings of tier in one declaration is ambiguous, not resolvable."""
        path = _write(
            tmp_path,
            {
                "project": "p",
                "models": {
                    "enabled": ["anthropic/sonnet/cli", "muddle"],
                    "custom": {
                        "muddle": {
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "tier": "strong",
                            "input_cost_per_mtok": 1.5,
                            "output_cost_per_mtok": 12.0,
                            "routing": {"tier": "cheap", "capability": 3},
                        }
                    },
                },
                "budget_usd": 30.0,
            },
        )
        with pytest.raises(ValueError, match="mixes the flat declaration shape"):
            load_config(path)

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
    @pytest.mark.parametrize("declared_side", ["input_per_mtok", "output_per_mtok"])
    def test_a_price_overlay_owns_the_band_it_moved_whichever_side_it_stated(
        self, tmp_path, declared_side
    ):
        """The band derives from both figures, so either one declared makes it ours.

        Reading only the input side reported an output-only overlay's band as
        ``builtin`` — the opposite of what happened, since the project's own
        figure is what re-banded the entry. That is the one thing this provenance
        map exists to answer, so it being backwards is worse than absent.
        """
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
                            "cost": {declared_side: 99.0},
                        }
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        sources = config.model_registry_field_sources["anthropic/sonnet/cli"]
        assert sources[f"{declared_side.split('_')[0]}_cost_per_mtok"] == SOURCE_PROJECT
        assert sources["cost_rank"] == SOURCE_PROJECT
        assert sources["cost_rank_basis"] == SOURCE_PROJECT
        # The band really did move off the shipped entry's, so the attribution is
        # answering a live question rather than restating a no-op.
        assert (config.model_registry or {})["anthropic/sonnet/cli"].cost_rank != AGENT_REGISTRY[
            "anthropic/sonnet/cli"
        ].cost_rank

    def test_inherited_prices_leave_the_band_attributed_to_the_shipped_entry(self, tmp_path):
        """A declaration that states no price at all has not moved the band."""
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
                            "routing": {"capability": 8},
                        }
                    ]
                },
                "budget_usd": 30.0,
            },
        )
        config = load_config(path)
        sources = config.model_registry_field_sources["anthropic/sonnet/cli"]
        assert sources["capability"] == SOURCE_PROJECT
        assert sources["cost_rank"] == SOURCE_BUILTIN
        assert sources["input_cost_per_mtok"] == SOURCE_BUILTIN

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
