"""Promotion of validated project-only models into the shipped catalog (#2252).

``openai/gpt-5.5/cli``, ``google/gemini-3.5-flash/api`` and ``anthropic/haiku/cli``
existed only as ``forge.yaml`` declarations even though this project routes to them
daily. These tests pin the two properties that make the promotion safe:

- the three identities resolve from the packaged catalog alone, with no project
  declaration required;
- a ``forge.yaml`` that still carries the now-redundant declaration (this
  project's own config is not touched by the promotion) keeps loading and keeps
  resolving to the same dispatch-relevant routing/cost — while a declaration that
  actually disagrees with the promoted entry is still rejected without
  ``override: true``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.config import load_config
from theforge.config.model_catalog import load_packaged_catalog
from theforge.config.models import AGENT_REGISTRY


def _write(tmp_path: Path, body: dict) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.dump(body), encoding="utf-8")
    return path


def test_the_three_promoted_identities_resolve_from_the_packaged_catalog_alone():
    catalog = load_packaged_catalog()
    assert catalog == AGENT_REGISTRY

    gpt_55 = catalog["openai/gpt-5.5/cli"]
    assert (gpt_55.tier, gpt_55.capability, gpt_55.cost_rank) == ("strong", 9, 3)
    assert (gpt_55.input_cost_per_mtok, gpt_55.output_cost_per_mtok) == (5.00, 30.00)
    assert gpt_55.pricing_provenance == "gpt-5.5"

    gemini_35_flash = catalog["google/gemini-3.5-flash/api"]
    assert (gemini_35_flash.tier, gemini_35_flash.capability, gemini_35_flash.cost_rank) == (
        "strong",
        9,
        2,
    )
    assert (gemini_35_flash.input_cost_per_mtok, gemini_35_flash.output_cost_per_mtok) == (
        1.50,
        9.00,
    )

    haiku = catalog["anthropic/haiku/cli"]
    assert (haiku.tier, haiku.capability, haiku.cost_rank) == ("cheap", 6, 1)
    assert haiku.dev_capable is False
    assert haiku.phase_eligibility == frozenset({"preflight", "review"})
    # A CLI shorthand: unattributed, same as sonnet/opus.
    assert haiku.pricing_provenance is None


def test_a_project_config_needs_no_declaration_to_resolve_them(tmp_path):
    path = _write(
        tmp_path,
        {
            "project": "p",
            "models": ["anthropic/sonnet/cli", "openai/gpt-5.5/cli", "anthropic/haiku/cli"],
            "budget_usd": 30.0,
        },
    )
    config = load_config(path)
    assert "openai/gpt-5.5/cli" in config.models
    assert "anthropic/haiku/cli" in config.models
    assert config.model_registry_sources["openai/gpt-5.5/cli"] == "builtin"


def test_a_redundant_project_declaration_still_loads_and_resolves_identically(tmp_path):
    """The exact overlay shape this project's own forge.yaml carries today.

    No ``override: true`` is set — the promotion must not force that change.
    """
    path = _write(
        tmp_path,
        {
            "project": "p",
            "models": {
                "enabled": ["anthropic/sonnet/cli", "openai/gpt-5.5/cli"],
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
            "budget_usd": 30.0,
        },
    )
    config = load_config(path)
    spec = (config.model_registry or {})["openai/gpt-5.5/cli"]
    builtin = AGENT_REGISTRY["openai/gpt-5.5/cli"]
    assert (spec.tier, spec.capability, spec.cost_rank, spec.dev_capable) == (
        builtin.tier,
        builtin.capability,
        builtin.cost_rank,
        builtin.dev_capable,
    )
    assert (spec.input_cost_per_mtok, spec.output_cost_per_mtok) == (
        builtin.input_cost_per_mtok,
        builtin.output_cost_per_mtok,
    )


def test_a_declaration_that_actually_disagrees_with_the_promoted_entry_still_needs_override(
    tmp_path,
):
    path = _write(
        tmp_path,
        {
            "project": "p",
            "models": {
                "enabled": ["anthropic/sonnet/cli", "openai/gpt-5.5/cli"],
                "custom": {
                    "openai/gpt-5.5/cli": {
                        "provider": "openai",
                        "model": "gpt-5.5",
                        "transport": {"kind": "cli"},
                        "tier": "strong",
                        "input_cost_per_mtok": 1.5,
                        "output_cost_per_mtok": 12.0,
                    }
                },
            },
            "budget_usd": 30.0,
        },
    )
    try:
        load_config(path)
    except ValueError as exc:
        assert "override: true" in str(exc)
    else:
        raise AssertionError("expected a genuinely conflicting declaration to be rejected")
