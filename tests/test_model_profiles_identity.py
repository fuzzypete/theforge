"""The vocabulary both model-profile halves share (#2467).

:mod:`theforge.model_profiles_identity` sits below both owners: the constants
that name stored slices, the identity resolution chain that maps a profile key
to a ``(provider, model)`` pair, the two stat primitives that read a stored
bucket's counters, and the single writer of the per-served-version breakdown.

Everything here is exercised the way both halves use it — against plain dicts
and plain strings. Nothing in this module may need a run outcome to produce its
input or a routing question to justify its output; that is the property these
tests hold it to.
"""

from __future__ import annotations

from theforge.model_profiles_identity import (
    ALIAS_DERIVED_KEY,
    CAPABILITY_RECENCY_WINDOW,
    COMPLEXITY_BANDS,
    DEFAULT_RECENCY_HALF_LIFE_RUNS,
    DEFAULT_RECENCY_MODE,
    DOMAIN_RECENCY_WINDOW,
    RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
    RESOLVED_MODEL_BREAKDOWN_KEY,
    ROLES,
    _fold_resolved_model,
    _identity_metadata,
    _metric_sum,
    _normalize_band,
    _success_count,
    _transport_from_identity,
    canonical_id_from_identity,
)


class TestStoredSliceNames:
    """The constants both halves key stored state by."""

    def test_roles_and_bands(self) -> None:
        assert ROLES == ("dev", "review", "preflight", "planner")
        assert COMPLEXITY_BANDS == ("small", "medium", "large")

    def test_section_keys_are_distinct(self) -> None:
        """The two breakdowns explain different denominators, so two keys.

        One shared key would be incremented twice per invocation and report
        more attributed observations than either population contains.
        """
        assert RESOLVED_MODEL_BREAKDOWN_KEY != RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY
        assert ALIAS_DERIVED_KEY not in (
            RESOLVED_MODEL_BREAKDOWN_KEY,
            RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
        )

    def test_recency_defaults_are_run_positional_not_wall_clock(self) -> None:
        assert DEFAULT_RECENCY_MODE == "exponential"
        assert DEFAULT_RECENCY_HALF_LIFE_RUNS > 0
        assert DOMAIN_RECENCY_WINDOW <= CAPABILITY_RECENCY_WINDOW


class TestNormalizeBand:
    """Every stored dev slice is keyed by a normalized band."""

    def test_known_bands_pass_through(self) -> None:
        for band in COMPLEXITY_BANDS:
            assert _normalize_band(band) == band

    def test_legacy_spellings_map_onto_the_taxonomy(self) -> None:
        assert _normalize_band("low") == "small"
        assert _normalize_band("high") == "large"
        assert _normalize_band("HIGH") == "large"

    def test_unknown_and_empty_default_to_medium(self) -> None:
        assert _normalize_band(None) == "medium"
        assert _normalize_band("") == "medium"
        assert _normalize_band("enormous") == "medium"


class TestIdentityDerivation:
    """Canonical IDs and stamped metadata come from runtime identity fields."""

    def test_provider_and_model_with_a_cli_yield_a_cli_transport(self) -> None:
        assert (
            canonical_id_from_identity(actual_model="sonnet", provider="anthropic", cli="claude")
            == "anthropic/sonnet/cli"
        )

    def test_provider_without_a_cli_yields_an_api_transport(self) -> None:
        assert (
            canonical_id_from_identity(actual_model="sonnet", provider="anthropic", cli=None)
            == "anthropic/sonnet/api"
        )

    def test_input_too_thin_to_identify_returns_none(self) -> None:
        """A role-shaped name carries no provider, so it names no model."""
        assert canonical_id_from_identity(actual_model="dev", provider=None, cli=None) is None
        assert (
            canonical_id_from_identity(actual_model=None, provider="anthropic", cli=None) is None
        )
        assert (
            canonical_id_from_identity(actual_model="  ", provider="anthropic", cli=None) is None
        )

    def test_transport_classification_reads_what_the_runner_reported(self) -> None:
        assert _transport_from_identity("anthropic", "claude") == "cli"
        assert _transport_from_identity("anthropic", None) == "api"
        assert _transport_from_identity(None, None) is None

    def test_metadata_carries_provider_model_and_transport(self) -> None:
        meta = _identity_metadata(actual_model="sonnet", provider="anthropic", cli="claude")
        assert meta == {
            "provider": "anthropic",
            "model": "sonnet",
            "transport": "cli",
            "cli": "claude",
        }

    def test_metadata_is_none_when_identity_is_incomplete(self) -> None:
        assert _identity_metadata(actual_model="sonnet", provider=None, cli=None) is None
        assert _identity_metadata(actual_model=None, provider="anthropic", cli=None) is None


class TestStatPrimitives:
    """Both halves read stored counters through these, so both read them alike."""

    def test_success_count_prefers_the_stored_accumulator(self) -> None:
        assert _success_count({"_successes": 3.0, "success_rate": 0.1}, 10) == 3.0

    def test_success_count_reconstructs_from_a_legacy_rate(self) -> None:
        """A profile written before ``_successes`` existed still answers."""
        assert _success_count({"success_rate": 0.5}, 10) == 5.0
        assert _success_count({}, 10) == 0.0

    def test_metric_sum_prefers_the_sum_then_the_average(self) -> None:
        assert _metric_sum({"_cost_sum": 9.0, "avg_cost_usd": 1.0}, 3, "_cost_sum", "avg") == 9.0
        assert _metric_sum({"avg_cost_usd": 2.0}, 3, "_cost_sum", "avg_cost_usd") == 6.0

    def test_metric_sum_is_none_when_neither_is_stored(self) -> None:
        """Absent is not zero — a caller has to be able to tell them apart."""
        assert _metric_sum({}, 3, "_cost_sum", "avg_cost_usd") is None


class TestResolvedModelBreakdown:
    """The one stored shape identity owns outright, keys and writer together."""

    def test_counts_land_under_the_served_version(self) -> None:
        section: dict = {}
        _fold_resolved_model(section, "claude-sonnet-4-6", success=True, tainted=False)
        _fold_resolved_model(section, "claude-sonnet-4-6", success=False, tainted=False)
        bucket = section[RESOLVED_MODEL_BREAKDOWN_KEY]["claude-sonnet-4-6"]
        assert bucket["runs"] == 2
        assert bucket["_successes"] == 1.0

    def test_the_key_selects_which_counter_is_explained(self) -> None:
        """A breakdown must sum to the denominator it describes, not another."""
        section: dict = {}
        _fold_resolved_model(section, "v1", success=None, tainted=False, count=3)
        _fold_resolved_model(
            section,
            "v1",
            success=None,
            tainted=False,
            key=RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
            count=5,
        )
        assert section[RESOLVED_MODEL_BREAKDOWN_KEY]["v1"]["runs"] == 3
        assert section[RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY]["v1"]["runs"] == 5

    def test_a_tainted_observation_is_tallied_not_folded(self) -> None:
        section: dict = {}
        _fold_resolved_model(section, "v1", success=True, tainted=True)
        bucket = section[RESOLVED_MODEL_BREAKDOWN_KEY]["v1"]
        assert bucket["tainted_runs"] == 1
        assert "runs" not in bucket

    def test_no_resolved_identity_records_nothing(self) -> None:
        """ "The transport reported nothing" is not evidence about a version."""
        section: dict = {}
        _fold_resolved_model(section, None, success=True, tainted=False)
        _fold_resolved_model(section, "v1", success=True, tainted=False, count=0)
        assert section == {}

    def test_success_none_counts_the_run_without_claiming_an_outcome(self) -> None:
        """Role folds count attempts; only the dev fold knows about success."""
        section: dict = {}
        _fold_resolved_model(section, "v1", success=None, tainted=False)
        bucket = section[RESOLVED_MODEL_BREAKDOWN_KEY]["v1"]
        assert bucket["runs"] == 1
        assert "_successes" not in bucket
