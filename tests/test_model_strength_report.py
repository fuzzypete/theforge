"""Declared model strength vs. observed dev behaviour (#2308)."""

from __future__ import annotations

import copy

from theforge.config.model_identity import AgentSpec, RoutingPolicy, TransportSpec
from theforge.model_profiles_read_model import get_dev_signal_for_keys, list_dev_evidence_keys
from theforge.model_strength_report import (
    REASON_NOT_DEV_CAPABLE,
    REASON_NOT_IN_CATALOG,
    REASON_UNRESOLVED,
    STATUS_INSUFFICIENT,
    STATUS_OBSERVED,
    STATUS_UNATTRIBUTED,
    STATUS_UNDERPERFORMING,
    STATUS_UNOBSERVED,
    build_model_strength_report,
)


def _spec(
    provider: str,
    model: str,
    *,
    tier: str = "strong",
    capability: int = 9,
    dev_capable: bool = True,
    kind: str = "cli",
) -> AgentSpec:
    return AgentSpec(
        provider=provider,
        model=model,
        transport=TransportSpec(
            kind=kind,
            runner=f"{provider}-{kind}",
            executable=f"{provider}-cli" if kind == "cli" else None,
        ),
        routing=RoutingPolicy(
            tier=tier,
            capability=capability,
            cost_rank=2,
            dev_capable=dev_capable,
        ),
    )


def _dev_entry(
    band_runs: dict[str, tuple[int, float]], *, identity: tuple[str, str] | None = None
):
    by_complexity = {
        band: {"runs": runs, "success_rate": rate, "_successes": round(runs * rate, 4)}
        for band, (runs, rate) in band_runs.items()
    }
    total = sum(runs for runs, _ in band_runs.values())
    entry: dict = {
        "dev": {
            "runs": total,
            "success_rate": 0.0,
            "by_complexity": by_complexity,
        }
    }
    if identity is not None:
        entry["_identity"] = {"provider": identity[0], "model": identity[1], "transport": "cli"}
    return entry


def _registry() -> dict[str, AgentSpec]:
    return {
        "acme/alpha/cli": _spec("acme", "alpha"),
        "acme/beta/cli": _spec("acme", "beta"),
        "acme/gamma/cli": _spec("acme", "gamma"),
        "acme/delta/cli": _spec("acme", "delta"),
    }


def _row(report, canonical_id: str, band: str):
    return next(
        row for row in report.rows if row.canonical_id == canonical_id and row.complexity == band
    )


def _underperformer_profiles() -> dict:
    """delta observed at 0.66/47 runs against peers at 0.85 and 0.92."""
    return {
        "models": {
            "acme/alpha/cli": _dev_entry({"large": (120, 0.92)}),
            "acme/beta/cli": _dev_entry({"large": (80, 0.85)}),
            "acme/delta/cli": _dev_entry({"large": (47, 0.66)}),
        }
    }


class TestObservedModels:
    def test_observed_model_reports_declared_values_rate_and_samples(self):
        profiles = {"models": {"acme/alpha/cli": _dev_entry({"medium": (40, 0.9)})}}
        report = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=10
        )
        row = _row(report, "acme/alpha/cli", "medium")
        assert row.status == STATUS_OBSERVED
        assert row.runs == 40
        assert row.observed_rate == 0.9
        assert (row.declared_tier, row.declared_capability) == ("strong", 9)

    def test_model_with_no_observations_is_unobserved_not_agreeing(self):
        report = build_model_strength_report(
            model_registry=_registry(), profiles={"models": {}}, evidence_floor=10
        )
        row = _row(report, "acme/alpha/cli", "large")
        assert row.status == STATUS_UNOBSERVED
        assert row.runs == 0
        assert row.observed_rate is None
        assert report.disagreements == ()

    def test_never_selected_and_underperforming_are_distinct_statuses(self):
        report = build_model_strength_report(
            model_registry=_registry(), profiles=_underperformer_profiles(), evidence_floor=10
        )
        assert _row(report, "acme/delta/cli", "large").status == STATUS_UNDERPERFORMING
        assert _row(report, "acme/gamma/cli", "large").status == STATUS_UNOBSERVED

    def test_bands_are_reported_separately(self):
        profiles = {
            "models": {"acme/alpha/cli": _dev_entry({"medium": (40, 0.9), "large": (30, 0.6)})}
        }
        report = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=10
        )
        assert _row(report, "acme/alpha/cli", "medium").observed_rate == 0.9
        assert _row(report, "acme/alpha/cli", "large").observed_rate == 0.6
        assert _row(report, "acme/alpha/cli", "small").status == STATUS_UNOBSERVED


class TestEvidenceThreshold:
    def test_thin_evidence_is_insufficient_not_disagreement(self):
        profiles = _underperformer_profiles()
        profiles["models"]["acme/delta/cli"] = _dev_entry({"large": (3, 0.33)})
        report = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=10
        )
        row = _row(report, "acme/delta/cli", "large")
        assert row.status == STATUS_INSUFFICIENT
        assert row.runs == 3
        assert row.observed_rate == 0.33

    def test_sample_count_accompanies_every_comparison(self):
        report = build_model_strength_report(
            model_registry=_registry(), profiles=_underperformer_profiles(), evidence_floor=10
        )
        row = _row(report, "acme/delta/cli", "large")
        assert row.runs == 47
        assert (row.peer_low, row.peer_high, row.peer_count) == (0.85, 0.92, 2)

    def test_floor_is_configurable(self):
        profiles = _underperformer_profiles()
        profiles["models"]["acme/delta/cli"] = _dev_entry({"large": (12, 0.5)})
        strict = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=25
        )
        lenient = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=10
        )
        assert _row(strict, "acme/delta/cli", "large").status == STATUS_INSUFFICIENT
        assert _row(lenient, "acme/delta/cli", "large").status == STATUS_UNDERPERFORMING


class TestPeerComparison:
    def test_single_qualifying_peer_suppresses_the_disagreement_claim(self):
        profiles = _underperformer_profiles()
        del profiles["models"]["acme/beta/cli"]
        report = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=10
        )
        row = _row(report, "acme/delta/cli", "large")
        assert row.status == STATUS_OBSERVED
        assert row.peer_count == 1

    def test_peers_below_the_floor_do_not_qualify(self):
        profiles = _underperformer_profiles()
        profiles["models"]["acme/beta/cli"] = _dev_entry({"large": (4, 0.85)})
        report = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=10
        )
        row = _row(report, "acme/delta/cli", "large")
        assert row.peer_count == 1
        assert row.status == STATUS_OBSERVED

    def test_peers_are_scoped_to_the_same_tier_and_band(self):
        registry = _registry()
        registry["acme/beta/cli"] = _spec("acme", "beta", tier="fast", capability=7)
        report = build_model_strength_report(
            model_registry=registry, profiles=_underperformer_profiles(), evidence_floor=10
        )
        row = _row(report, "acme/delta/cli", "large")
        assert row.peer_count == 1
        assert (row.peer_low, row.peer_high) == (0.92, 0.92)

    def test_rate_within_margin_of_weakest_peer_is_not_a_disagreement(self):
        profiles = _underperformer_profiles()
        profiles["models"]["acme/delta/cli"] = _dev_entry({"large": (47, 0.83)})
        report = build_model_strength_report(
            model_registry=_registry(), profiles=profiles, evidence_floor=10
        )
        assert _row(report, "acme/delta/cli", "large").status == STATUS_OBSERVED

    def test_evidence_recorded_under_one_transport_does_not_peer_for_the_other(self):
        """The API entry claims no key, so it is not a second peer for delta."""
        registry = _registry()
        registry["acme/alpha/api"] = _spec("acme", "alpha", kind="api")
        del registry["acme/beta/cli"]
        report = build_model_strength_report(
            model_registry=registry,
            profiles=_underperformer_profiles(),
            evidence_floor=10,
            resolve_key=lambda key, entry: key,
        )
        row = _row(report, "acme/delta/cli", "large")
        assert row.peer_count == 1
        assert row.status == STATUS_OBSERVED
        assert _row(report, "acme/alpha/api", "large").status == STATUS_UNOBSERVED


class TestUnattributedEvidence:
    def test_unresolvable_key_is_flagged_and_excluded(self):
        profiles = {
            "models": {
                "acme/alpha/cli": _dev_entry({"large": (40, 0.9)}),
                "dev": _dev_entry({"medium": (46, 0.78)}),
            }
        }
        report = build_model_strength_report(
            model_registry=_registry(),
            profiles=profiles,
            evidence_floor=10,
            resolve_key=lambda key, entry: key if key in _registry() else None,
        )
        flagged = {entry.key: entry for entry in report.unattributed}
        assert flagged["dev"].reason == REASON_UNRESOLVED
        assert flagged["dev"].status == STATUS_UNATTRIBUTED
        assert flagged["dev"].runs == 46
        assert all(row.runs == 0 for row in report.rows if row.complexity == "medium")

    def test_key_resolving_outside_the_live_catalog_is_flagged(self):
        profiles = {"models": {"acme/retired/cli": _dev_entry({"large": (30, 0.7)})}}
        report = build_model_strength_report(
            model_registry=_registry(),
            profiles=profiles,
            evidence_floor=10,
            resolve_key=lambda key, entry: key,
        )
        flagged = {entry.key: entry for entry in report.unattributed}
        assert flagged["acme/retired/cli"].reason == REASON_NOT_IN_CATALOG
        assert flagged["acme/retired/cli"].canonical_id == "acme/retired/cli"

    def test_evidence_for_a_non_dev_capable_model_is_flagged_not_compared(self):
        registry = _registry()
        registry["acme/epsilon/cli"] = _spec("acme", "epsilon", dev_capable=False)
        profiles = {"models": {"acme/epsilon/cli": _dev_entry({"large": (30, 0.7)})}}
        report = build_model_strength_report(
            model_registry=registry,
            profiles=profiles,
            evidence_floor=10,
            resolve_key=lambda key, entry: key,
        )
        assert [entry.reason for entry in report.unattributed] == [REASON_NOT_DEV_CAPABLE]
        assert "acme/epsilon/cli" in report.excluded_non_dev_models
        assert all(row.canonical_id != "acme/epsilon/cli" for row in report.rows)

    def test_excluded_key_is_never_also_counted_in_a_live_row(self):
        """#2308 review: identity metadata must not smuggle excluded evidence back in.

        ``impostor`` cannot be named canonically, but its ``_identity`` shares
        ``(provider, model)`` with a live entry — exactly what the router's
        matching keys on. It must land in one place only: excluded.
        """
        profiles = {
            "models": {
                "acme/alpha/cli": _dev_entry({"large": (40, 0.9)}),
                "impostor": _dev_entry({"large": (60, 0.1)}, identity=("acme", "alpha")),
            }
        }
        report = build_model_strength_report(
            model_registry=_registry(),
            profiles=profiles,
            evidence_floor=10,
            resolve_key=lambda key, entry: key if key in _registry() else None,
        )
        row = _row(report, "acme/alpha/cli", "large")
        assert row.runs == 40
        assert row.observed_rate == 0.9
        assert "impostor" not in row.contributing_keys
        assert [entry.key for entry in report.unattributed] == ["impostor"]
        assert report.unattributed[0].reason == REASON_UNRESOLVED

    def test_key_resolving_to_a_retired_identity_is_only_excluded(self):
        """Same partition rule for a key that resolves, but not to a live model."""
        profiles = {
            "models": {
                "acme/alpha/cli": _dev_entry({"large": (40, 0.9)}),
                "acme/alpha-legacy/cli": _dev_entry(
                    {"large": (60, 0.1)}, identity=("acme", "alpha")
                ),
            }
        }
        report = build_model_strength_report(
            model_registry=_registry(),
            profiles=profiles,
            evidence_floor=10,
            resolve_key=lambda key, entry: key,
        )
        row = _row(report, "acme/alpha/cli", "large")
        assert (row.runs, row.observed_rate) == (40, 0.9)
        assert [entry.reason for entry in report.unattributed] == [REASON_NOT_IN_CATALOG]

    def test_non_dev_capable_evidence_is_not_borrowed_by_a_sibling_transport(self):
        registry = _registry()
        registry["acme/alpha/api"] = _spec("acme", "alpha", kind="api", dev_capable=False)
        profiles = {"models": {"acme/alpha/api": _dev_entry({"large": (60, 0.1)})}}
        report = build_model_strength_report(
            model_registry=registry,
            profiles=profiles,
            evidence_floor=10,
            resolve_key=lambda key, entry: key,
        )
        assert _row(report, "acme/alpha/cli", "large").status == STATUS_UNOBSERVED
        assert [entry.reason for entry in report.unattributed] == [REASON_NOT_DEV_CAPABLE]

    def test_legacy_key_contributing_to_a_live_model_is_named_on_the_row(self):
        profiles = {
            "models": {
                "acme/alpha/cli": _dev_entry({"large": (40, 0.9)}),
                "alpha-shorthand": _dev_entry({"large": (10, 0.5)}, identity=("acme", "alpha")),
            }
        }
        report = build_model_strength_report(
            model_registry=_registry(),
            profiles=profiles,
            evidence_floor=10,
            resolve_key=lambda key, entry: "acme/alpha/cli",
        )
        row = _row(report, "acme/alpha/cli", "large")
        assert row.contributing_keys == ("acme/alpha/cli", "alpha-shorthand")
        assert row.runs == 50
        assert report.unattributed == ()


class TestReportIsAdvisoryOnly:
    def test_inputs_are_not_mutated(self):
        registry = _registry()
        profiles = _underperformer_profiles()
        registry_before = copy.deepcopy({k: v.routing for k, v in registry.items()})
        profiles_before = copy.deepcopy(profiles)
        build_model_strength_report(model_registry=registry, profiles=profiles, evidence_floor=10)
        assert {k: v.routing for k, v in registry.items()} == registry_before
        assert profiles == profiles_before

    def test_declared_values_echo_the_catalog_unchanged(self):
        registry = _registry()
        registry["acme/delta/cli"] = _spec("acme", "delta", tier="strong", capability=10)
        report = build_model_strength_report(
            model_registry=registry, profiles=_underperformer_profiles(), evidence_floor=10
        )
        row = _row(report, "acme/delta/cli", "large")
        assert row.status == STATUS_UNDERPERFORMING
        assert (row.declared_tier, row.declared_capability) == ("strong", 10)
        assert registry["acme/delta/cli"].routing.capability == 10

    def test_evidence_recency_is_reported_as_unknown(self):
        report = build_model_strength_report(
            model_registry=_registry(), profiles=_underperformer_profiles(), evidence_floor=10
        )
        assert {row.evidence_recency for row in report.rows} == {"unknown"}


class TestEvidenceAttributionReadModel:
    def test_keys_are_classified_canonical_legacy_and_unresolved(self):
        profiles = {
            "models": {
                "acme/alpha/cli": _dev_entry({"large": (40, 0.9)}),
                "alpha-shorthand": _dev_entry({"large": (10, 0.5)}),
                "mystery": _dev_entry({"small": (2, 0.5)}),
            }
        }
        resolve = {"acme/alpha/cli": "acme/alpha/cli", "alpha-shorthand": "acme/alpha/cli"}
        records = {
            record["key"]: record
            for record in list_dev_evidence_keys(
                profiles, resolve=lambda key, entry: resolve.get(key)
            )
        }
        assert records["acme/alpha/cli"]["resolution"] == "canonical"
        assert records["alpha-shorthand"]["resolution"] == "legacy"
        assert records["mystery"]["resolution"] == "unresolved"
        assert records["acme/alpha/cli"]["runs_by_band"] == {"small": 0, "medium": 0, "large": 40}

    def test_no_resolver_leaves_every_key_unresolved(self):
        profiles = {"models": {"acme/alpha/cli": _dev_entry({"large": (40, 0.9)})}}
        [record] = list_dev_evidence_keys(profiles)
        assert record["canonical_id"] is None
        assert record["resolution"] == "unresolved"

    def test_last_updated_is_unknown_rather_than_fabricated(self):
        profiles = {"models": {"acme/alpha/cli": _dev_entry({"large": (40, 0.9)})}}
        assert list_dev_evidence_keys(profiles)[0]["last_updated"] is None

    def test_signal_for_keys_aggregates_exactly_the_named_keys(self):
        profiles = {
            "models": {
                "acme/alpha/cli": _dev_entry({"large": (40, 0.9)}),
                "alpha-shorthand": _dev_entry({"large": (10, 0.5)}, identity=("acme", "alpha")),
                "acme/beta/cli": _dev_entry({"large": (10, 0.5)}),
            }
        }
        signal = get_dev_signal_for_keys(
            profiles, ["acme/alpha/cli", "alpha-shorthand"], "large", 10
        )
        assert signal["runs"] == 50
        assert signal["rate"] == 0.82

    def test_signal_for_keys_ignores_identity_metadata_of_unnamed_keys(self):
        """An unnamed key stays out however its ``_identity`` reads (#2308 review)."""
        profiles = {
            "models": {
                "acme/alpha/cli": _dev_entry({"large": (40, 0.9)}),
                "impostor": _dev_entry({"large": (60, 0.1)}, identity=("acme", "alpha")),
            }
        }
        signal = get_dev_signal_for_keys(profiles, ["acme/alpha/cli"], "large", 10)
        assert signal["runs"] == 40
        assert signal["rate"] == 0.9

    def test_signal_for_keys_tolerates_unknown_keys_and_empty_bands(self):
        profiles = {"models": {"acme/alpha/cli": _dev_entry({"large": (40, 0.9)})}}
        assert get_dev_signal_for_keys(profiles, ["acme/alpha/cli"], "small", 10)["runs"] == 0
        assert get_dev_signal_for_keys(profiles, ["nope"], "large", 10)["runs"] == 0
        assert get_dev_signal_for_keys(profiles, [], "large", 10)["rate"] is None
