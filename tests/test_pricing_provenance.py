"""Pricing attribution gates the routing inputs derived from price (issue #2203).

A per-MTok price on a registry entry is a routing input: it sets the cost band
and breaks equal-band ties. Entries identified by a vendor shorthand (the Claude
CLI's ``opus``/``sonnet``) resolve to some other concrete version at invocation
time, so their stored literal describes an identity that can move underneath it.
These tests pin the fix: a price that carries no ``pricing_provenance`` is
treated exactly like a missing price — it never bands and never breaks a tie —
while attributed prices keep the #1617 behaviour intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.config import load_config
from theforge.config.models import (
    AGENT_REGISTRY,
    MODEL_REGISTRY,
    AgentSpec,
    RoutingPolicy,
    _spec_to_model_info,
    transport_for,
)
from theforge.config.pricing import (
    PRICING_PROVENANCE_LOCAL_ENDPOINT,
    PRICING_PROVENANCE_OPERATOR_DECLARED,
    custom_model_cost_rank,
    price_tiebreak_signal,
    price_tiebreak_signal_for,
)
from theforge.config.profiles import _agents_from_models
from theforge.config.role_derivation import derive_roles
from theforge.coordinator.preflight import _build_pool_entries

_UNPRICED = float("inf")


def _spec(
    model: str,
    *,
    provider: str = "openai",
    kind: str = "api",
    capability: int = 7,
    cost_rank: int = 1,
    input_cost: float | None = None,
    output_cost: float | None = None,
    provenance: str | None = None,
) -> AgentSpec:
    return AgentSpec(
        provider=provider,
        model=model,
        transport=transport_for(provider, kind),
        routing=RoutingPolicy(tier="cheap", capability=capability, cost_rank=cost_rank),
        input_cost_per_mtok=input_cost,
        output_cost_per_mtok=output_cost,
        pricing_provenance=provenance,
    )


# ── The attribution gate itself ────────────────────────────────────────


def test_unattributed_prices_are_unknown_to_routing():
    """Present figures with no provenance read as "no price" on the routing path."""
    spec = _spec("shorthand", input_cost=1.0, output_cost=4.0)
    # The literals are still recorded — they just cannot be vouched for.
    assert (spec.input_cost_per_mtok, spec.output_cost_per_mtok) == (1.0, 4.0)
    assert spec.pricing_attributable is False
    assert spec.effective_input_cost_per_mtok is None
    assert spec.effective_output_cost_per_mtok is None
    assert price_tiebreak_signal_for(spec) == _UNPRICED


def test_attributed_prices_still_steer_the_tiebreak():
    spec = _spec("pinned", input_cost=1.0, output_cost=4.0, provenance="pinned-2026-07")
    assert spec.pricing_attributable is True
    assert spec.effective_input_cost_per_mtok == 1.0
    assert spec.effective_output_cost_per_mtok == 4.0
    # Unchanged from the numeric primitive: max(input, output).
    assert price_tiebreak_signal_for(spec) == price_tiebreak_signal(1.0, 4.0) == 4.0


def test_attributed_but_unpriced_entry_still_reads_as_unpriced():
    spec = _spec("no-figures", provenance="pinned-2026-07")
    assert price_tiebreak_signal_for(spec) == _UNPRICED


def test_cost_rank_needs_attribution_to_band():
    """An unattributable price yields no band rather than a fabricated one."""
    assert custom_model_cost_rank(15.0, 75.0, pricing_provenance=None) is None
    # With attribution the existing banding is untouched.
    assert custom_model_cost_rank(1.0, 5.0, pricing_provenance="x") == 1
    assert custom_model_cost_rank(15.0, 25.0, pricing_provenance="x") == 2
    assert custom_model_cost_rank(15.0, 75.0, pricing_provenance="x") == 3


def test_provenance_survives_the_projections_routing_reads():
    """ModelInfo and AgentDef carry the attribution, not just AgentSpec."""
    opus = _spec_to_model_info("anthropic/opus/cli", AGENT_REGISTRY["anthropic/opus/cli"])
    assert opus.input_cost_per_mtok == 15.00  # literal preserved for reference
    assert opus.pricing_provenance is None
    assert price_tiebreak_signal_for(opus) == _UNPRICED

    agents = _agents_from_models(["anthropic/opus/cli", "openai/gpt-5.4/cli"], budget_usd=10.0)
    by_model = {a.model: a for a in agents}
    assert by_model["opus"].pricing_provenance is None
    assert price_tiebreak_signal_for(by_model["opus"]) == _UNPRICED
    assert by_model["gpt-5.4"].pricing_provenance == "gpt-5.4"
    assert price_tiebreak_signal_for(by_model["gpt-5.4"]) == 10.00


# ── Built-in registry attribution ──────────────────────────────────────


@pytest.mark.parametrize("key", ["anthropic/opus/cli", "anthropic/sonnet/cli"])
def test_cli_shorthand_entries_are_unattributed(key):
    """The Claude CLI resolves these identities at invocation, so their stored
    literal cannot be attributed to what is billed."""
    assert AGENT_REGISTRY[key].pricing_provenance is None
    assert MODEL_REGISTRY[key].pricing_provenance is None


def test_every_other_priced_builtin_entry_records_its_attribution():
    unattributed = {
        key
        for key, spec in AGENT_REGISTRY.items()
        if spec.input_cost_per_mtok is not None and spec.pricing_provenance is None
    }
    assert unattributed == {"anthropic/opus/cli", "anthropic/sonnet/cli"}


def test_local_endpoint_entries_are_attributed_to_the_endpoint():
    spec = AGENT_REGISTRY["openai/codestral/api"]
    assert spec.pricing_provenance == PRICING_PROVENANCE_LOCAL_ENDPOINT
    assert price_tiebreak_signal_for(spec) == 0.0


# ── Selection seams: the literal no longer steers ──────────────────────


def _rigged_registry() -> dict[str, AgentSpec]:
    """Two same-band, same-capability candidates. The *unattributed* one is much
    cheaper on paper, so under the old behaviour it won every tie-break."""
    cheap_literal = _spec("shorthand", input_cost=1.0, output_cost=1.0)
    attributed = _spec("pinned", input_cost=9.0, output_cost=9.0, provenance="pinned-2026-07")
    return {
        "openai/shorthand/api": cheap_literal,
        "openai/pinned/api": attributed,
    }


@pytest.mark.parametrize(
    "models",
    [
        ["openai/shorthand/api", "openai/pinned/api"],
        ["openai/pinned/api", "openai/shorthand/api"],
    ],
)
def test_build_pool_entries_ignores_the_unattributed_literal(models):
    """Coordinator preflight seam: the attributed candidate leads regardless of
    the cheaper unattributable figure (and regardless of list order)."""
    entries = _build_pool_entries(models, registry=_rigged_registry())
    assert [key for _rank, key, _info in entries][0] == "openai/pinned/api"


@pytest.mark.parametrize(
    "models",
    [
        ["openai/shorthand/api", "openai/pinned/api"],
        ["openai/pinned/api", "openai/shorthand/api"],
    ],
)
def test_derive_roles_ignores_the_unattributed_literal(models):
    """Role-derivation seam: same ordering rule at config-load time."""
    assignment = derive_roles(models, registry=_rigged_registry())
    assert assignment.dev.ref.model == "pinned"


def test_attributed_pool_still_breaks_ties_by_price():
    """Control: with both candidates attributed, the cheaper one wins as before."""
    registry = _rigged_registry()
    registry["openai/shorthand/api"] = _spec(
        "shorthand", input_cost=1.0, output_cost=1.0, provenance="pinned-2026-07"
    )
    entries = _build_pool_entries(["openai/pinned/api", "openai/shorthand/api"], registry=registry)
    assert [key for _rank, key, _info in entries][0] == "openai/shorthand/api"


# ── Config-load seam ───────────────────────────────────────────────────


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


def test_inherited_unattributed_price_does_not_reband_the_entry(tmp_path):
    """An enabled-mapping override that only touches routing inherits the
    built-in band — it does not re-derive one from the inherited literal."""
    config_path = _write_config(
        {
            "models": {
                "enabled": [
                    {
                        "provider": "anthropic",
                        "model": "opus",
                        "transport": {"kind": "cli"},
                        "routing": {"capability": 8},
                    }
                ]
            },
            "budget_usd": 50.0,
        },
        tmp_path,
    )
    config = load_config(config_path)
    spec = config.model_registry["anthropic/opus/cli"]
    assert spec.pricing_provenance is None
    # 15.00/75.00 would band 3 anyway; the point is the band came from the
    # built-in policy, and the figures stay out of the tie-break.
    assert spec.cost_rank == AGENT_REGISTRY["anthropic/opus/cli"].cost_rank
    assert price_tiebreak_signal_for(spec) == _UNPRICED


def test_operator_declared_cost_is_attributed_and_bands(tmp_path):
    """A `cost:` block is the operator attributing figures to this identity."""
    config_path = _write_config(
        {
            "models": {
                "enabled": [
                    {
                        "provider": "anthropic",
                        "model": "opus",
                        "transport": {"kind": "cli"},
                        "cost": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
                    }
                ]
            },
            "budget_usd": 50.0,
        },
        tmp_path,
    )
    config = load_config(config_path)
    spec = config.model_registry["anthropic/opus/cli"]
    assert spec.pricing_provenance == PRICING_PROVENANCE_OPERATOR_DECLARED
    assert spec.cost_rank == 2  # re-banded from the declared figures, not the literal
    assert price_tiebreak_signal_for(spec) == 25.0


def test_check_config_names_the_models_whose_price_is_ignored(tmp_path, capsys):
    """Operator-visible: the registry section says which enabled entries carry a
    price that routing refuses to use, so the literal isn't read as the figure
    selection ran on."""
    from unittest.mock import patch

    from theforge.cli.check_config import _unattributed_pricing_models, cmd_check_config

    assert _unattributed_pricing_models(
        ["anthropic/opus/cli", "openai/gpt-5.4/cli", "openai/gpt-5.4-mini/cli"]
    ) == ["anthropic/opus/cli"]

    config_path = _write_config(
        {
            "models": {"enabled": ["anthropic/opus/cli", "openai/gpt-5.4-mini/cli"]},
            "budget_usd": 50.0,
        },
        tmp_path,
    )
    config = load_config(config_path)
    args = type("Args", (), {"config": str(config_path), "verbose": False, "json": False})()
    with (
        patch("theforge.cli.check_config._find_config", return_value=config_path),
        patch("theforge.cli.check_config.load_config", return_value=config),
        patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
    ):
        cmd_check_config(args)
    out = capsys.readouterr().out
    assert "unattributed price:" in out
    assert "anthropic/opus/cli" in out.split("unattributed price:")[1].splitlines()[0]


def test_models_custom_declaration_is_attributed(tmp_path):
    config_path = _write_config(
        {
            "models": {
                "enabled": ["anthropic/sonnet/cli", "gpt-5.5"],
                "custom": {
                    "gpt-5.5": {
                        "provider": "openai",
                        "model": "gpt-5.5",
                        "tier": "strong",
                        "input_cost_per_mtok": 5,
                        "output_cost_per_mtok": 30,
                    }
                },
            },
            "budget_usd": 50.0,
        },
        tmp_path,
    )
    config = load_config(config_path)
    spec = config.model_registry["openai/gpt-5.5/cli"]
    assert spec.pricing_provenance == PRICING_PROVENANCE_OPERATOR_DECLARED
    assert spec.cost_rank == 3
    assert price_tiebreak_signal_for(spec) == 30.0
