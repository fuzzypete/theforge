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

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from theforge.config import load_config
from theforge.config.model_catalog import parse_definition, resolve_packaged
from theforge.config.models import (
    AGENT_REGISTRY,
    MODEL_REGISTRY,
    AgentSpec,
    RoutingPolicy,
    _spec_to_model_info,
    transport_for,
)
from theforge.config.pricing import (
    COST_BAND_BASIS_OPERATOR_DECLARED,
    COST_BAND_BASIS_VENDOR_TIER,
    PRICING_PROVENANCE_LOCAL_ENDPOINT,
    PRICING_PROVENANCE_OPERATOR_DECLARED,
    custom_model_cost_rank,
    price_tiebreak_signal,
    price_tiebreak_signal_for,
    resolve_cost_band_basis,
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


# ── The cost band is attributed too ────────────────────────────────────
#
# ``cost_rank`` selects which tier a role is filled from, so a band copied off an
# untraceable literal steers selection exactly as the tie-break did. These pin
# the second half of the fix: every band names what it is derived from, and the
# alias entries' bands are not a function of their literals.


_ALIAS_KEYS = ["anthropic/opus/cli", "anthropic/sonnet/cli"]


def test_every_builtin_band_records_what_it_is_derived_from():
    assert [
        key for key, spec in AGENT_REGISTRY.items() if spec.routing.cost_rank_basis is None
    ] == []


def test_alias_bands_are_declared_on_non_price_grounds():
    """The entries whose price cannot be attributed must not be banding off it."""
    for key in _ALIAS_KEYS:
        basis = AGENT_REGISTRY[key].routing.cost_rank_basis
        assert basis == COST_BAND_BASIS_VENDOR_TIER
        assert not basis.startswith("price:")


@pytest.mark.parametrize("key", _ALIAS_KEYS)
@pytest.mark.parametrize(
    ("input_cost", "output_cost"),
    [
        (15.00, 75.00),  # what the registry records today
        (5.00, 25.00),  # what forge.yaml annotates as actually billed — bands 2
        (0.01, 0.01),  # bands 1
        (999.0, 999.0),  # bands 3
    ],
)
def test_alias_band_does_not_move_when_the_literal_moves(key, input_cost, output_cost):
    """No value of the unattributable literal changes the band — including the
    5.00/25.00 forge.yaml annotates, which is a band below the 15.00/75.00 the
    registry records and is where the reported one-band divergence came from."""
    spec = AGENT_REGISTRY[key]
    repriced = replace(spec, input_cost_per_mtok=input_cost, output_cost_per_mtok=output_cost)
    assert repriced.cost_rank == spec.cost_rank
    assert repriced.routing.cost_rank_basis == spec.routing.cost_rank_basis
    assert price_tiebreak_signal_for(repriced) == _UNPRICED


def test_sonnets_band_already_disagrees_with_its_literal():
    """Evidence the band is not price-derived rather than merely coinciding:
    3.00/15.00 would band 2, and the entry is banded 1."""
    spec = AGENT_REGISTRY["anthropic/sonnet/cli"]
    would_be = custom_model_cost_rank(
        spec.input_cost_per_mtok, spec.output_cost_per_mtok, pricing_provenance="hypothetical"
    )
    assert would_be == 2
    assert spec.cost_rank == 1


def test_the_defects_exact_entry_shape_no_longer_builds():
    """The shape the bug was reported in: a vendor-shorthand entry carrying an
    unattributable 15.00/75.00 and banded 3 — the band those figures produce, with
    nothing recording whether that was a derivation or a coincidence. Building it
    now raises; the entry only exists once it says why band 3 holds without them.

    Written as catalog data, because that is what a shipped entry is since #2204:
    the guard has to hold at the parser the packaged catalog goes through."""

    def _shipped(**routing_extra):
        definition = {
            "provider": "anthropic",
            "model": "opus",
            "transport": {"kind": "cli"},
            "routing": {"tier": "strong", "capability": 10, "cost_rank": 3, **routing_extra},
            "cost": {"input_per_mtok": 15.00, "output_per_mtok": 75.00},
        }
        return resolve_packaged(parse_definition(definition, where="x"), where="x")

    with pytest.raises(ValueError, match="cannot be attributed"):
        _shipped()

    spec = _shipped(cost_rank_basis=COST_BAND_BASIS_VENDOR_TIER).spec
    assert spec.cost_rank == 3
    assert spec.routing.cost_rank_basis == COST_BAND_BASIS_VENDOR_TIER
    # And that band still owes nothing to the figures beside it.
    assert price_tiebreak_signal_for(spec) == _UNPRICED


def test_an_untraceable_band_is_refused_at_construction():
    """The mechanical guard: a band with no attributable price and no declared
    basis cannot be built, so a future entry cannot quietly acquire one."""
    with pytest.raises(ValueError, match="cannot be attributed"):
        resolve_cost_band_basis(
            3,
            input_cost_per_mtok=15.0,
            output_cost_per_mtok=75.0,
            pricing_provenance=None,
            declared_basis=None,
        )
    # Attributed, but the band disagrees with the price it claims to come from.
    with pytest.raises(ValueError, match="cannot be attributed"):
        resolve_cost_band_basis(
            3,
            input_cost_per_mtok=1.0,
            output_cost_per_mtok=2.0,
            pricing_provenance="pinned-2026-07",
            declared_basis=None,
        )
    # A band that *is* the attributable price band is attributed to that price.
    assert (
        resolve_cost_band_basis(
            1,
            input_cost_per_mtok=1.0,
            output_cost_per_mtok=2.0,
            pricing_provenance="pinned-2026-07",
            declared_basis=None,
        )
        == "price:pinned-2026-07"
    )


def _alias_repriced_registry(input_cost: float, output_cost: float) -> dict[str, AgentSpec]:
    """The real registry with both Claude shorthand literals moved."""
    registry = dict(AGENT_REGISTRY)
    for key in _ALIAS_KEYS:
        registry[key] = replace(
            registry[key],
            input_cost_per_mtok=input_cost,
            output_cost_per_mtok=output_cost,
        )
    return registry


_FLEET = [
    "anthropic/sonnet/cli",
    "anthropic/opus/cli",
    "openai/gpt-5.4/cli",
    "openai/gpt-5.4-mini/cli",
]


@pytest.mark.parametrize(
    ("input_cost", "output_cost"), [(5.00, 25.00), (0.01, 0.01), (999.0, 999.0)]
)
def test_selection_is_identical_whatever_the_alias_literal_says(input_cost, output_cost):
    """End-to-end at both selection seams: moving the unattributable figures —
    including to the billed 5.00/25.00 that would have re-banded opus — changes
    neither the preflight pool order nor any derived role."""
    baseline_pool = _build_pool_entries(_FLEET, registry=AGENT_REGISTRY)
    moved_pool = _build_pool_entries(
        _FLEET, registry=_alias_repriced_registry(input_cost, output_cost)
    )
    assert [(rank, key) for rank, key, _info in moved_pool] == [
        (rank, key) for rank, key, _info in baseline_pool
    ]

    for complexity in ("LOW", "MEDIUM", "HIGH"):
        baseline = derive_roles(_FLEET, registry=AGENT_REGISTRY, complexity=complexity)
        moved = derive_roles(
            _FLEET,
            registry=_alias_repriced_registry(input_cost, output_cost),
            complexity=complexity,
        )
        for role in ("preflight", "plan", "dev"):
            assert getattr(moved, role).ref.model == getattr(baseline, role).ref.model
        assert [r.ref.model for r in moved.review_pool] == [
            r.ref.model for r in baseline.review_pool
        ]


def test_check_config_reports_each_bands_basis(tmp_path, capsys):
    """Operator-visible: the band and its source are printed together, so a
    selection can be traced without reading the registry."""
    from unittest.mock import patch

    from theforge.cli.check_config import _cost_band_bases, cmd_check_config

    assert _cost_band_bases(["anthropic/opus/cli", "openai/gpt-5.4/cli"]) == [
        ("anthropic/opus/cli", 3, COST_BAND_BASIS_VENDOR_TIER),
        ("openai/gpt-5.4/cli", 2, "price:gpt-5.4"),
    ]

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
    assert "cost band basis:" in out
    assert f"rank 3  from {COST_BAND_BASIS_VENDOR_TIER}" in out


def test_operator_declared_band_is_attributed_to_the_operator(tmp_path):
    """A `routing.cost_rank` in forge.yaml is the operator declaring the band."""
    config_path = _write_config(
        {
            "models": {
                "enabled": [
                    {
                        "provider": "anthropic",
                        "model": "opus",
                        "transport": {"kind": "cli"},
                        "routing": {"cost_rank": 2},
                    }
                ]
            },
            "budget_usd": 50.0,
        },
        tmp_path,
    )
    config = load_config(config_path)
    spec = config.model_registry["anthropic/opus/cli"]
    assert spec.cost_rank == 2
    assert spec.routing.cost_rank_basis == COST_BAND_BASIS_OPERATOR_DECLARED


def test_inherited_band_keeps_the_builtin_basis(tmp_path):
    """Inheriting a built-in band inherits its justification too — it does not
    become a price-derived band on the way through the loader."""
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
    assert spec.routing.cost_rank_basis == COST_BAND_BASIS_VENDOR_TIER


def test_operator_declared_cost_bands_from_the_declared_price(tmp_path):
    """The `cost:` path bands *and* records that the band came from that price."""
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
    assert spec.cost_rank == 2
    assert spec.routing.cost_rank_basis == f"price:{PRICING_PROVENANCE_OPERATOR_DECLARED}"


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
