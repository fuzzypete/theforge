"""Durable capability-record storage semantics (#2466).

Covers the store in isolation: load/save defaults, identity+capability keying,
established_at persistence, the three lookup states, staleness on a changed
probe subject, and the deterministic merge when one run produces several
observations for the same identity+capability.
"""

from __future__ import annotations

from theforge.config import AgentDef, ModelProfile, transport_for
from theforge.model_capabilities import (
    CAPABILITY_PLAIN_STRUCTURED,
    CAPABILITY_TOOL_STRUCTURED,
    OUTCOME_ABSENT,
    OUTCOME_DEMONSTRATED,
    STATE_DEMONSTRATED,
    STATE_DEMONSTRATED_ABSENT,
    STATE_NEVER_ESTABLISHED,
    CapabilityObservation,
    capabilities_path,
    identity_for_agent,
    identity_for_profile,
    is_demonstrated_absent,
    load_capabilities,
    lookup,
    make_identity,
    record_observations,
    save_capabilities,
    signature_for_agent,
    signature_for_profile,
)


def _identity(provider="openai", model="gpt-5.4", transport="api"):
    return make_identity(provider, model, transport)


def _observation(
    *,
    outcome=OUTCOME_ABSENT,
    capability=CAPABILITY_TOOL_STRUCTURED,
    identity=None,
    signature="sig-1",
    detail="no valid verdict in structured output",
    probe_role="review",
):
    return CapabilityObservation(
        identity=identity or _identity(),
        capability=capability,
        outcome=outcome,
        subject_signature=signature,
        detail=detail,
        probe_role=probe_role,
    )


# ── Load / save ───────────────────────────────────────────────────────


def test_load_returns_empty_record_when_file_absent(tmp_path):
    data = load_capabilities(capabilities_path(tmp_path))
    assert data["identities"] == {}


def test_load_returns_empty_record_when_file_is_malformed(tmp_path):
    path = capabilities_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not: a: valid: mapping: [", encoding="utf-8")

    # A broken record means nothing has been established — never "everything is
    # absent", which would silently empty the routing pool.
    assert load_capabilities(path)["identities"] == {}


def test_save_then_load_round_trips(tmp_path):
    path = capabilities_path(tmp_path)
    data = record_observations({"identities": {}}, [_observation()])
    save_capabilities(path, data)

    reloaded = load_capabilities(path)
    assert reloaded["identities"] == data["identities"]


# ── Keying and timestamps ─────────────────────────────────────────────


def test_record_is_keyed_by_identity_and_capability(tmp_path):
    data = record_observations(
        {"identities": {}},
        [
            _observation(capability=CAPABILITY_TOOL_STRUCTURED, outcome=OUTCOME_ABSENT),
            _observation(capability=CAPABILITY_PLAIN_STRUCTURED, outcome=OUTCOME_DEMONSTRATED),
            _observation(
                identity=_identity(transport="cli"),
                capability=CAPABILITY_TOOL_STRUCTURED,
                outcome=OUTCOME_DEMONSTRATED,
            ),
        ],
    )

    assert set(data["identities"]) == {"openai/gpt-5.4/api", "openai/gpt-5.4/cli"}
    api_caps = data["identities"]["openai/gpt-5.4/api"]["capabilities"]
    assert api_caps[CAPABILITY_TOOL_STRUCTURED]["outcome"] == OUTCOME_ABSENT
    assert api_caps[CAPABILITY_PLAIN_STRUCTURED]["outcome"] == OUTCOME_DEMONSTRATED
    # Transport is part of the identity: the CLI half is a separate fact.
    cli_caps = data["identities"]["openai/gpt-5.4/cli"]["capabilities"]
    assert cli_caps[CAPABILITY_TOOL_STRUCTURED]["outcome"] == OUTCOME_DEMONSTRATED


def test_established_at_is_recorded_and_readable(tmp_path):
    data = record_observations(
        {"identities": {}}, [_observation()], established_at="2026-08-15T09:00:00Z"
    )

    result = lookup(data, _identity(), CAPABILITY_TOOL_STRUCTURED)
    assert result.established_at == "2026-08-15T09:00:00Z"
    assert result.detail == "no valid verdict in structured output"
    assert result.probe_role == "review"


def test_reprobe_updates_the_same_record(tmp_path):
    data = record_observations(
        {"identities": {}},
        [_observation(outcome=OUTCOME_ABSENT)],
        established_at="2026-08-01T00:00:00Z",
    )
    data = record_observations(
        data,
        [_observation(outcome=OUTCOME_DEMONSTRATED, signature="sig-2", detail="0.4s $0.001")],
        established_at="2026-08-15T00:00:00Z",
    )

    result = lookup(data, _identity(), CAPABILITY_TOOL_STRUCTURED)
    assert result.state == STATE_DEMONSTRATED
    assert result.established_at == "2026-08-15T00:00:00Z"


# ── Three states ──────────────────────────────────────────────────────


def test_lookup_reports_demonstrated():
    data = record_observations({"identities": {}}, [_observation(outcome=OUTCOME_DEMONSTRATED)])

    assert lookup(data, _identity(), CAPABILITY_TOOL_STRUCTURED).state == STATE_DEMONSTRATED
    assert is_demonstrated_absent(data, _identity(), CAPABILITY_TOOL_STRUCTURED) is False


def test_lookup_reports_demonstrated_absent():
    data = record_observations({"identities": {}}, [_observation(outcome=OUTCOME_ABSENT)])

    assert lookup(data, _identity(), CAPABILITY_TOOL_STRUCTURED).state == STATE_DEMONSTRATED_ABSENT
    assert is_demonstrated_absent(data, _identity(), CAPABILITY_TOOL_STRUCTURED) is True


def test_never_established_is_not_absent():
    data = record_observations({"identities": {}}, [_observation(outcome=OUTCOME_ABSENT)])

    # A different identity, and a different capability of the same identity, are
    # both never-established — not absent.
    other = lookup(data, _identity(model="other"), CAPABILITY_TOOL_STRUCTURED)
    assert other.state == STATE_NEVER_ESTABLISHED
    assert is_demonstrated_absent(data, _identity(model="other"), CAPABILITY_TOOL_STRUCTURED) is (
        False
    )

    plain = lookup(data, _identity(), CAPABILITY_PLAIN_STRUCTURED)
    assert plain.state == STATE_NEVER_ESTABLISHED


def test_empty_record_reports_never_established():
    assert lookup({}, _identity(), CAPABILITY_TOOL_STRUCTURED).state == STATE_NEVER_ESTABLISHED
    assert lookup(None, _identity(), CAPABILITY_TOOL_STRUCTURED).state == STATE_NEVER_ESTABLISHED
    assert lookup({"identities": {}}, None, CAPABILITY_TOOL_STRUCTURED).state == (
        STATE_NEVER_ESTABLISHED
    )


# ── Staleness ─────────────────────────────────────────────────────────


def test_changed_probe_subject_reports_stale_and_does_not_exclude():
    data = record_observations(
        {"identities": {}}, [_observation(outcome=OUTCOME_ABSENT, signature="sig-old")]
    )

    result = lookup(data, _identity(), CAPABILITY_TOOL_STRUCTURED, subject_signature="sig-new")
    assert result.stale is True
    assert result.state == STATE_DEMONSTRATED_ABSENT  # still reported…
    assert result.current_absent is False  # …but not presented as current
    assert (
        is_demonstrated_absent(
            data, _identity(), CAPABILITY_TOOL_STRUCTURED, subject_signature="sig-new"
        )
        is False
    )


def test_matching_probe_subject_is_not_stale():
    data = record_observations({"identities": {}}, [_observation(signature="sig-1")])

    result = lookup(data, _identity(), CAPABILITY_TOOL_STRUCTURED, subject_signature="sig-1")
    assert result.stale is False
    assert result.current_absent is True


# ── Merge determinism ─────────────────────────────────────────────────


def test_absent_wins_over_demonstrated_within_one_batch():
    """Several role probes hit one identity+capability; the merge is order-free."""
    demonstrated = _observation(outcome=OUTCOME_DEMONSTRATED, probe_role="agent-plan-review")
    absent = _observation(outcome=OUTCOME_ABSENT, probe_role="agent-code-review")

    for batch in ([demonstrated, absent], [absent, demonstrated]):
        data = record_observations({"identities": {}}, batch)
        result = lookup(data, _identity(), CAPABILITY_TOOL_STRUCTURED)
        assert result.state == STATE_DEMONSTRATED_ABSENT
        assert result.probe_role == "agent-code-review"


def test_observations_without_a_recognised_outcome_are_ignored():
    data = record_observations(
        {"identities": {}},
        [CapabilityObservation(_identity(), CAPABILITY_TOOL_STRUCTURED, "maybe", "sig")],
    )
    assert data["identities"] == {}


# ── Identity adapters ─────────────────────────────────────────────────


def test_identity_and_signature_agree_between_profile_and_agent():
    """The probe writes from a ModelProfile; the router reads from an AgentDef."""
    agent = AgentDef(
        name="gpt",
        provider="openai",
        model="gpt-5.4",
        budget_usd=8.0,
        timeout_seconds=900,
        tier="strong",
        transport=transport_for("openai", "api"),
    )
    profile = ModelProfile(
        name="gpt",
        provider="openai",
        transport=transport_for("openai", "api"),
        model="gpt-5.4",
        budget_usd=8.0,
        timeout_seconds=900,
        allowed_tools=("Read",),
    )

    assert identity_for_agent(agent) == identity_for_profile(profile)
    assert identity_for_agent(agent).key == "openai/gpt-5.4/api"
    assert signature_for_agent(agent) == signature_for_profile(profile)


def test_cli_agent_identity_uses_effective_provider():
    agent = AgentDef(
        name="claude",
        provider=None,
        cli="claude",
        model="sonnet",
        budget_usd=3.0,
        timeout_seconds=600,
        tier="mid",
    )

    assert identity_for_agent(agent).key == "anthropic/sonnet/cli"


def test_identity_is_none_when_provider_or_model_is_unknown():
    assert make_identity(None, "gpt-5.4", "api") is None
    assert make_identity("openai", "", "api") is None


def test_repointed_endpoint_changes_the_signature_but_not_the_identity():
    base = AgentDef(
        name="local",
        provider="openai",
        model="qwen",
        budget_usd=0.0,
        timeout_seconds=600,
        tier="cheap",
        transport=transport_for("openai", "api"),
        base_url="http://localhost:1234/v1",
    )
    moved = AgentDef(**{**base.__dict__, "base_url": "http://localhost:9999/v1"})

    assert identity_for_agent(base) == identity_for_agent(moved)
    assert signature_for_agent(base) != signature_for_agent(moved)
