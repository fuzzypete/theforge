"""Unit tests for the agent-entry → model identity projection (#2201)."""

from __future__ import annotations

from theforge.coordinator import agent_identity as ai


def test_model_used_is_a_direct_identity() -> None:
    assert ai.entry_model_identity({"role": "dev", "model_used": "sonnet"}) == (
        "anthropic/sonnet/cli",
        ai.SOURCE_DIRECT,
    )


def test_unresolvable_identity_is_kept_verbatim() -> None:
    """Canonicalization failure must not discard what the runner recorded."""
    identity, source = ai.entry_model_identity({"role": "dev", "model_used": "some-new-model"})
    assert (identity, source) == ("some-new-model", ai.SOURCE_DIRECT)


def test_model_config_is_a_recovered_identity() -> None:
    assert ai.entry_model_identity({"role": "dev", "model_config": ["opus", "sonnet"]}) == (
        "anthropic/opus/cli",
        ai.SOURCE_RECOVERED,
    )


def test_legacy_keys_are_a_recovered_identity() -> None:
    assert ai.entry_model_identity(
        {"phase": "dev", "provider": "openai", "model": "gpt-5", "cli": ""}
    ) == ("openai/gpt-5/api", ai.SOURCE_RECOVERED)
    assert ai.entry_model_identity({"phase": "dev", "name": "dev-profile"}) == (
        "dev-profile",
        ai.SOURCE_RECOVERED,
    )


def test_model_usage_alone_is_not_an_identity() -> None:
    """Per-component billing is not the identity of the invocation."""
    assert (
        ai.entry_model_identity(
            {"role": "dev", "model_usage": [{"model": "claude-haiku-4-5", "cost_usd": 0.01}]}
        )
        is None
    )


def test_non_dev_entries_are_ignored() -> None:
    record = {"cost": {"agents": [{"role": "review", "model_used": "sonnet"}]}}
    assert ai.dev_model_identity(record) == (None, None)


def test_dev_entry_matches_role_or_legacy_phase_key() -> None:
    assert ai.is_dev_entry({"role": "dev"})
    assert ai.is_dev_entry({"phase": "dev"})
    assert not ai.is_dev_entry({"role": "synthesis"})
    assert not ai.is_dev_entry("dev")


def test_malformed_records_project_to_nothing() -> None:
    for record in (None, {}, {"cost": "nope"}, {"cost": {"agents": "nope"}}, {"cost": {}}):
        assert ai.dev_model_identity(record) == (None, None)
        assert ai.dev_model_identity_detail(record) == (None, None, None)


# ── Resolution status (#2225) ──────────────────────────────────────────


def test_canonicalized_identity_reports_canonical_resolution() -> None:
    assert ai.entry_model_identity_detail({"role": "dev", "model_used": "sonnet"}) == (
        "anthropic/sonnet/cli",
        ai.SOURCE_DIRECT,
        ai.RESOLUTION_CANONICAL,
    )


def test_verbatim_identity_reports_unresolved_resolution() -> None:
    """Verbatim is correct; indistinguishable-from-canonical is the defect."""
    assert ai.entry_model_identity_detail({"role": "dev", "model_used": "some-new-model"}) == (
        "some-new-model",
        ai.SOURCE_DIRECT,
        ai.RESOLUTION_UNRESOLVED,
    )


def test_concrete_anthropic_version_folds_onto_the_registry_shorthand() -> None:
    """The three live spellings of one model project to one identity."""
    identities = {
        ai.entry_model_identity_detail({"role": "dev", "model_used": spelling})[0]
        for spelling in ("anthropic/sonnet/cli", "sonnet", "claude-sonnet-4-6")
    }
    assert identities == {"anthropic/sonnet/cli"}


def test_transport_used_disambiguates_a_bare_model_name() -> None:
    for transport in ("cli", "api"):
        assert ai.entry_model_identity_detail(
            {"role": "dev", "model_used": "gpt-5.4", "transport_used": transport}
        ) == (f"openai/gpt-5.4/{transport}", ai.SOURCE_DIRECT, ai.RESOLUTION_CANONICAL)


def test_bare_model_name_without_a_hint_stays_unresolved() -> None:
    assert ai.entry_model_identity_detail({"role": "dev", "model_used": "gpt-5.4"}) == (
        "gpt-5.4",
        ai.SOURCE_DIRECT,
        ai.RESOLUTION_UNRESOLVED,
    )


def test_transport_hint_does_not_leak_into_the_recovered_config_reading() -> None:
    """Which preference-list entry served is unrecorded, so the hint does not apply."""
    assert ai.entry_model_identity_detail(
        {"role": "dev", "model_config": ["gpt-5.4"], "transport_used": "cli"}
    ) == ("gpt-5.4", ai.SOURCE_RECOVERED, ai.RESOLUTION_UNRESOLVED)


def test_dev_model_identity_detail_prefers_the_direct_entry() -> None:
    record = {
        "cost": {
            "agents": [
                {"role": "dev", "model_config": ["opus"]},
                {"role": "dev", "model_used": "claude-sonnet-4-6"},
            ]
        }
    }
    assert ai.dev_model_identity_detail(record) == (
        "anthropic/sonnet/cli",
        ai.SOURCE_DIRECT,
        ai.RESOLUTION_CANONICAL,
    )
    assert ai.dev_model_identity(record) == ("anthropic/sonnet/cli", ai.SOURCE_DIRECT)
