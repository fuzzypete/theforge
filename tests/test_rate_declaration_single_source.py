"""A rate is declared in one kind of place, and configuration can reach it (#2388).

Rates used to be declared twice: in the model catalog — data, per-entry
provenance, editable without a release and overridable per project — and in
``PRICING_TABLE``, a dictionary compiled into the runner package that no
configuration could change and that accounting consulted whenever the registry
missed. Of its 21 rows, 10 duplicated a catalog entry and 11 priced identities
nothing could dispatch, so the second source persisted mainly as a place a rate
could disagree with the catalog and win by being consulted second.

These tests pin what replaced it:

- every identity a *token-returning* transport can dispatch is priced from the
  one source, so nothing enabled is unpriced by construction;
- an entry whose transport reports what it was billed says so
  (``cost.rate_basis``), which is what makes "needs no rate" different from
  "rate is missing" without inferring it from an absent field;
- no accounting path answers from a rate configuration could not have supplied —
  including the Claude kill-path reconstruction, which used to read the table
  directly;
- the rows that were dropped are recorded, so re-enabling one of those models is
  a lookup rather than an archaeology exercise.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import load_config
from theforge.config.model_catalog import load_packaged_catalog, parse_definition
from theforge.config.models import AGENT_REGISTRY, RETIRED_MODEL_REGISTRY
from theforge.config.pricing import (
    PRICING_PROVENANCE_LOCAL_ENDPOINT,
    RATE_BASIS_PROVIDER_REPORTED,
    RATE_BASIS_TOKEN_RATES,
)
from theforge.runners import rate_registry as rr
from theforge.runners import runner_claude as runner_claude_mod
from theforge.runners.rate_registry import accounting_mode_for
from theforge.runners.schema_utils import _estimate_cost, catalog_rates, pricing_for

_RECORD = Path(__file__).resolve().parents[1] / "docs" / "reference" / "dropped-legacy-rates.md"


def _mode_of(spec) -> object:
    return accounting_mode_for(spec.transport.kind, spec.transport.runner)


# ── Coverage: nothing dispatchable is unpriced by construction ────────


class TestEveryTokenReturningIdentityIsPriced:
    def test_shipped_entries_that_need_rates_have_them_in_the_catalog(self):
        """A transport that returns token counts must find a rate on its entry.

        The catalog is now the only place that rate can come from, so an entry
        this check misses is an entry that would record its spend as unknown on
        every run — there is no packaged row behind it any more.
        """
        unpriced = []
        for canonical_id, spec in sorted(AGENT_REGISTRY.items()):
            if not _mode_of(spec).needs_rates:
                continue
            if spec.pricing_provenance == PRICING_PROVENANCE_LOCAL_ENDPOINT:
                # A local endpoint is free by construction and the runners record
                # 0.00 for it from base_url, with no rate card involved.
                continue
            if pricing_for(spec.provider, spec.model) is None:
                unpriced.append(canonical_id)
        assert unpriced == [], (
            "these shipped entries dispatch on a token-returning transport with no "
            f"rate reachable from the catalog: {unpriced}"
        )

    def test_a_rate_free_entry_says_so_rather_than_leaving_the_field_out(self):
        """ "Needs no rate" and "rate is missing" must not look identical."""
        silent = [
            canonical_id
            for canonical_id, spec in sorted(AGENT_REGISTRY.items())
            if spec.input_cost_per_mtok is None
            and spec.output_cost_per_mtok is None
            and spec.rate_basis == RATE_BASIS_TOKEN_RATES
        ]
        assert silent == [], (
            "these shipped entries declare no rate and no reason they need none; "
            f"state cost.rate_basis: {silent}"
        )

    def test_the_provider_reported_entries_are_the_claude_cli_ones(self):
        """The property is stated where it is true, not sprinkled about."""
        declared = {
            canonical_id
            for canonical_id, spec in AGENT_REGISTRY.items()
            if spec.rate_basis == RATE_BASIS_PROVIDER_REPORTED
        }
        assert declared == {"anthropic/claude-opus-5/cli", "anthropic/claude-sonnet-5/cli"}
        for canonical_id in declared:
            spec = AGENT_REGISTRY[canonical_id]
            assert _mode_of(spec) is rr.AccountingMode.PROVIDER_REPORTED
            assert not spec.uses_rate_card
            # Being rate-free is not being unaccountable: the transport reports
            # the bill, so a run on this identity still produces a cost record.
            entry = rr.RateEntry(
                identity=rr.make_identity(spec.provider, spec.model, spec.transport.kind),
                rates=None,
                mode=_mode_of(spec),
            )
            assert entry.accountable
            assert entry.unaccountable_reason() is None

    def test_a_provider_reported_entry_contributes_no_rate_card(self):
        """Nothing can price tokens off an entry that says it is not priced that way."""
        assert ("anthropic", "claude-opus-5") not in catalog_rates()
        assert pricing_for("anthropic", "claude-opus-5") is None
        with rr.scoped_registry(None):
            assert _estimate_cost("anthropic", "claude-opus-5", 1_000_000, 0, transport="cli") is (
                None
            )


# ── The stated property is enforced, not decorative ───────────────────


def _entry(**cost) -> dict:
    return {
        "provider": "anthropic",
        "model": "claude-whatever-9",
        "transport": {"kind": "cli"},
        "routing": {"tier": "strong", "capability": 9, "cost_rank": 3},
        "cost": cost,
    }


class TestRateBasisSchema:
    def test_provider_reported_may_not_also_declare_figures(self):
        with pytest.raises(ValueError, match="rate_basis"):
            parse_definition(
                _entry(
                    rate_basis=RATE_BASIS_PROVIDER_REPORTED,
                    input_per_mtok=1.0,
                    output_per_mtok=2.0,
                    pricing_provenance="claude-whatever-9",
                ),
                where="models[0]",
            )

    def test_an_unknown_basis_is_refused_at_load(self):
        with pytest.raises(ValueError, match="rate_basis"):
            parse_definition(_entry(rate_basis="vibes"), where="models[0]")

    def test_the_default_basis_is_token_rates(self):
        defn = parse_definition(
            _entry(input_per_mtok=1.0, output_per_mtok=2.0, pricing_provenance="x"),
            where="models[0]",
        )
        assert "rate_basis" not in defn.cost

    def test_a_declared_basis_survives_the_catalog_round_trip(self):
        registry = load_packaged_catalog()
        assert registry["anthropic/claude-opus-5/cli"].rate_basis == RATE_BASIS_PROVIDER_REPORTED
        assert registry["anthropic/claude-opus-4-6/cli"].rate_basis == RATE_BASIS_TOKEN_RATES

    def test_a_project_that_declares_figures_is_declaring_a_rate_card(self, tmp_path):
        """An overlay's own price must not be swallowed by an inherited basis.

        The shipped ``claude-opus-5`` entry states that its transport reports the
        bill. A project that declares figures for that identity is saying
        something else, and the figures it wrote have to be the ones that price
        it — inheriting ``provider_reported`` would leave them unread.
        """
        path = tmp_path / "forge.yaml"
        path.write_text(
            yaml.dump(
                {
                    "project": "p",
                    "models": {
                        "enabled": ["anthropic/sonnet/cli", "pinned-opus-5"],
                        "custom": {
                            "pinned-opus-5": {
                                "provider": "anthropic",
                                "model": "claude-opus-5",
                                "transport": {"kind": "cli"},
                                "override": True,
                                "routing": {"tier": "strong", "capability": 10, "cost_rank": 3},
                                "cost": {
                                    "input_per_mtok": 20.0,
                                    "output_per_mtok": 100.0,
                                    "pricing_provenance": "claude-opus-5",
                                },
                            }
                        },
                    },
                    "budget_usd": 30.0,
                }
            ),
            encoding="utf-8",
        )
        with patch("theforge.config.load.check_agent_auth", return_value=(True, "")):
            config = load_config(path)
        spec = (config.model_registry or {})["anthropic/claude-opus-5/cli"]
        assert spec.rate_basis == RATE_BASIS_TOKEN_RATES
        assert spec.uses_rate_card
        entry = rr.resolve(rr.make_identity("anthropic", "claude-opus-5", "cli"))
        assert entry is not None
        assert entry.rates == rr.ModelRates(input_per_mtok=20.0, output_per_mtok=100.0)


# ── No accounting path consults an unsupplied rate ────────────────────


def _dropped_identities() -> list[tuple[str, str]]:
    """The ``(provider, model)`` rows the migration record says were dropped."""
    body = _RECORD.read_text(encoding="utf-8").split("## Dropped", 1)[1]
    body = body.split("## Re-enabling", 1)[0]
    rows = re.findall(r"^\|\s*(\w[\w.]*)\s*\|\s*`([^`]+)`\s*\|", body, flags=re.MULTILINE)
    return [(provider, model) for provider, model in rows]


class TestNoPackagedFallbackSurvives:
    def test_the_migration_record_lists_the_rows_that_were_dropped(self):
        dropped = _dropped_identities()
        assert len(dropped) == 11, dropped
        assert ("openai", "gpt-4o") in dropped
        assert ("google", "gemini-2.5-flash") in dropped

    @pytest.mark.parametrize("identity", _dropped_identities())
    def test_a_dropped_row_prices_nothing_anywhere(self, identity):
        """The rows nothing could dispatch are recorded, not still consulted."""
        provider, model = identity
        canonical = {f"{provider}/{model}/cli", f"{provider}/{model}/api"}
        assert not canonical & set(AGENT_REGISTRY)
        assert not canonical & set(RETIRED_MODEL_REGISTRY)
        with rr.scoped_registry(None):
            assert pricing_for(provider, model) is None
            for transport in ("cli", "api", None):
                assert (
                    _estimate_cost(provider, model, 1_000_000, 1_000_000, transport=transport)
                    is None
                )

    def test_the_claude_kill_path_prices_from_the_catalog(self):
        """The billed ids the CLI reports resolve to catalog entries, not a table.

        ``_estimate_anthropic_cost`` read ``PRICING_TABLE`` as a last resort for
        exactly these names. They are pinned catalog entries now, so the figures
        are the ones an operator can see, date and override.
        """
        with rr.scoped_registry(None):
            cost = runner_claude_mod._estimate_anthropic_cost(
                "claude-sonnet-4-6", 1_000_000, 0, 0, 0
            )
            rates = catalog_rates()[("anthropic", "claude-sonnet-4-6")]
            assert cost == pytest.approx(rates.input_per_mtok)
            # A dated id still resolves to its family entry through the same set.
            assert runner_claude_mod._estimate_anthropic_cost(
                "claude-opus-4-6-20260115", 1_000_000, 0, 0, 0
            ) == pytest.approx(catalog_rates()[("anthropic", "claude-opus-4-6")].input_per_mtok)

    def test_the_claude_name_set_is_exactly_what_the_catalog_prices(self):
        with rr.scoped_registry(None):
            names = set(runner_claude_mod._anthropic_cli_pricing_names())
        assert names == {model for provider, model in catalog_rates() if provider == "anthropic"}
