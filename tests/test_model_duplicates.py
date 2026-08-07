"""Comparing a project declaration against the shipped entry it duplicates (#2252).

Promoting a model into the shipped catalog leaves any configuration that already
declared it holding a duplicate. These tests pin what such a duplicate is allowed
to be assumed: not inert. A project declaration re-derives its own pricing
attribution, attribution gates the prices routing reads, so two definitions that
name the same model with the same numbers can still dispatch differently.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from theforge.config.model_duplicates import compare_duplicate_declaration
from theforge.config.model_identity import AgentSpec, RoutingPolicy, transport_for
from theforge.config.pricing import (
    COST_BAND_BASIS_VENDOR_TIER,
    PRICING_PROVENANCE_OPERATOR_DECLARED,
    price_tiebreak_signal_for,
)


def _spec(**kwargs) -> AgentSpec:
    routing_kwargs = {
        "tier": "cheap",
        "capability": 6,
        "cost_rank": 1,
        "cost_rank_basis": COST_BAND_BASIS_VENDOR_TIER,
    }
    for key in ("tier", "capability", "cost_rank", "cost_rank_basis", "dev_capable"):
        if key in kwargs:
            routing_kwargs[key] = kwargs.pop(key)
    if "phase_eligibility" in kwargs:
        routing_kwargs["phase_eligibility"] = frozenset(kwargs.pop("phase_eligibility"))
    base = {
        "provider": "anthropic",
        "model": "haiku",
        "transport": transport_for("anthropic", "cli"),
        "routing": RoutingPolicy(**routing_kwargs),
        "input_cost_per_mtok": 1.00,
        "output_cost_per_mtok": 5.00,
        "pricing_provenance": None,
    }
    base.update(kwargs)
    return AgentSpec(**base)


def test_an_exact_restatement_is_reported_as_redundant():
    builtin = _spec()
    duplicate = compare_duplicate_declaration("anthropic/haiku/cli", _spec(), builtin)
    assert duplicate.is_redundant
    assert not duplicate.routing_differs
    assert duplicate.describe() == ()


def test_attribution_alone_differing_is_reported_without_claiming_a_routing_change():
    """Both sides attributable to *something*, same figures → same tie-break.

    This is the shape of a promoted entry whose declaration transcribed it: the
    catalog attributes the price to the billed identity, the declaration to
    ``forge.yaml``. Routing reads the same numbers either way.
    """
    builtin = _spec(
        pricing_provenance="claude-haiku-4-5", cost_rank_basis="price:claude-haiku-4-5"
    )
    project = _spec(
        pricing_provenance=PRICING_PROVENANCE_OPERATOR_DECLARED,
        cost_rank_basis="price:forge.yaml",
    )
    duplicate = compare_duplicate_declaration("anthropic/haiku/cli", project, builtin)
    assert not duplicate.routing_differs
    assert not duplicate.is_redundant  # reported, not silently equated
    fields = {d.field for d in duplicate.attribution_differences}
    assert fields == {"pricing_provenance", "cost_rank_basis"}
    assert price_tiebreak_signal_for(project) == price_tiebreak_signal_for(builtin)


def test_an_unattributed_shipped_entry_versus_a_declared_price_is_a_routing_difference():
    """The case that makes a duplicate declaration load-bearing.

    A vendor shorthand ships unattributed, so routing ignores its literal. The
    same entry declared in forge.yaml attributes the figures to the operator, so
    routing *does* read them — the price tie-break moves from "behind every priced
    candidate" to a real number. Same identity, same numbers on the page,
    different selection.
    """
    builtin = _spec(pricing_provenance=None, cost_rank_basis=COST_BAND_BASIS_VENDOR_TIER)
    project = _spec(
        pricing_provenance=PRICING_PROVENANCE_OPERATOR_DECLARED,
        cost_rank_basis="price:forge.yaml",
    )
    duplicate = compare_duplicate_declaration("anthropic/haiku/cli", project, builtin)
    assert duplicate.routing_differs
    routing_fields = {d.field for d in duplicate.routing_differences}
    assert routing_fields == {
        "effective_input_cost_per_mtok",
        "effective_output_cost_per_mtok",
    }
    # And the difference is real, not notional.
    assert price_tiebreak_signal_for(builtin) == float("inf")
    assert price_tiebreak_signal_for(project) == 5.0


def test_an_unattributable_literal_differing_is_not_a_routing_difference():
    """Routing cannot read either figure, so disagreeing about them changes nothing."""
    builtin = _spec(input_cost_per_mtok=1.00)
    project = _spec(input_cost_per_mtok=99.00)
    duplicate = compare_duplicate_declaration("anthropic/haiku/cli", project, builtin)
    assert not duplicate.routing_differs
    assert {d.field for d in duplicate.attribution_differences} == {"input_cost_per_mtok"}


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("tier", "strong"),
        ("capability", 9),
        ("cost_rank", 3),
        ("dev_capable", False),
        ("phase_eligibility", ["review"]),
    ],
)
def test_every_selection_field_counts_as_a_routing_difference(field_name, value):
    duplicate = compare_duplicate_declaration(
        "anthropic/haiku/cli", _spec(**{field_name: value}), _spec()
    )
    assert duplicate.routing_differs
    assert field_name in {d.field for d in duplicate.routing_differences}


def test_a_different_endpoint_counts_as_a_routing_difference():
    project = replace(_spec(), base_url="http://localhost:11434/v1")
    duplicate = compare_duplicate_declaration("anthropic/haiku/cli", project, _spec())
    assert duplicate.routing_differs
    assert "base_url" in {d.field for d in duplicate.routing_differences}


def test_a_difference_names_both_sides_so_the_report_is_readable():
    duplicate = compare_duplicate_declaration("anthropic/haiku/cli", _spec(tier="strong"), _spec())
    assert "tier: forge.yaml='strong' builtin='cheap'" in duplicate.describe()
