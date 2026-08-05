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
