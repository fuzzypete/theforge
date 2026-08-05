"""The per-invocation identity ledger: writer, reader, and index (#2205).

An invocation collapses three different facts into one if it records a single
model identity: what the run was *configured* as, what that configuration
*resolved* to at invocation time, and which models were actually *billed*
inside it. These tests pin that all three survive as separate values from the
renderer through the reader to the sqlite index, that a consumer can tell when
the first two differ, and that a record written before the ledger existed still
reads — and is still identifiable as one that does not carry the full set.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from theforge.agent_types import (
    COST_ESTIMATED,
    COST_PROVIDER_REPORTED,
    COST_UNKNOWN,
    AgentResult,
    ModelUsage,
)
from theforge.coordinator import audit_substrate
from theforge.coordinator.agent_identity import (
    SOURCE_DIRECT,
    SOURCE_RECOVERED,
    dev_identity_ledger,
    dev_model_identity_detail,
    entry_identity_ledger,
    entry_model_identity_detail,
)
from theforge.coordinator.audit_render import (
    INVOCATION_LEDGER_VERSION,
    build_invocation_ledger,
)


def _result(**overrides: object) -> AgentResult:
    """A dev AgentResult with the whole ledger populated, overridable per test."""
    base: dict = {
        "success": True,
        "output": "done",
        "session_id": "s1",
        "cost_usd": 10.902,
        "exit_code": 0,
        "raw": {},
        "profile_name": "dev",
        "model_usage": (
            ModelUsage(
                model="claude-opus-5",
                input_tokens=100,
                output_tokens=200,
                cache_read_tokens=10,
                cache_creation_tokens=5,
                cost_usd=10.892,
                thinking_tokens=50,
                cost_provenance=COST_PROVIDER_REPORTED,
            ),
            # Billed, never configured, and absent from the catalog — the group
            # that is the only place any of this survived before the ledger.
            ModelUsage(
                model="claude-haiku-4-5",
                input_tokens=10,
                output_tokens=20,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.010,
                cost_provenance=COST_PROVIDER_REPORTED,
            ),
        ),
        "model_used": "claude-opus-5",
        "transport_used": "cli",
        "configured_model": "opus",
        "configured_transport": "cli",
        "reasoning_effort": "high",
        "cost_provenance": COST_PROVIDER_REPORTED,
    }
    base.update(overrides)
    return AgentResult(**base)  # type: ignore[arg-type]


def _ledger(**overrides: object) -> dict:
    return build_invocation_ledger(
        _result(**overrides), "dev", "dev", complexity="large", complexity_score=8
    )


def _record(entries: list[dict]) -> dict:
    return {"run_id": "r1", "cost": {"agents": entries}}


# ── Writer: three identities, not one ────────────────────────────────────


def test_ledger_records_configured_resolved_and_billed_separately() -> None:
    """Each of the three identities is its own value, none standing in for another."""
    ledger = _ledger()

    assert ledger["configured_identity"]["raw"] == "opus"
    assert ledger["resolved_primary_identity"]["raw"] == "claude-opus-5"
    billed = [c["identity"]["raw"] for c in ledger["billed_components"]]
    assert billed == ["claude-opus-5", "claude-haiku-4-5"]
    # The component the operator never configured and the catalog does not know
    # is still recorded, with its own cost attached to it rather than folded in.
    haiku = ledger["billed_components"][1]
    assert haiku["cost_usd"] == 0.010
    assert haiku["identity"]["resolution"] == "unresolved"


def test_ledger_records_run_conditions_and_usage_by_class() -> None:
    """Role, complexity, reasoning effort, and usage counts by class are recorded."""
    ledger = _ledger()

    assert ledger["version"] == INVOCATION_LEDGER_VERSION
    assert ledger["role"] == "dev"
    assert ledger["profile"] == "dev"
    assert ledger["complexity"] == "large"
    assert ledger["complexity_score"] == 8
    assert ledger["reasoning_effort"] == "high"
    assert ledger["usage"] == {
        "input_tokens": 110,
        "output_tokens": 220,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 5,
        # Thinking tokens are a distinct usage class, not folded into output —
        # a reasoning model's spend is unreadable if they are.
        "thinking_tokens": 50,
    }


def test_ledger_states_whether_cost_was_reported_or_estimated() -> None:
    """A billed figure and a forge-derived one are distinguishable, per component."""
    assert _ledger()["cost_provenance"] == COST_PROVIDER_REPORTED

    estimated = _ledger(
        cost_provenance=COST_ESTIMATED,
        model_usage=(
            ModelUsage(
                model="gpt-5.4",
                input_tokens=1,
                output_tokens=2,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.5,
                cost_provenance=COST_ESTIMATED,
            ),
        ),
    )
    assert estimated["cost_provenance"] == COST_ESTIMATED
    assert estimated["billed_components"][0]["cost_provenance"] == COST_ESTIMATED


def test_unmeasured_cost_is_unknown_not_reported_or_estimated() -> None:
    """No observed cost means no provenance claim — never a fabricated one."""
    ledger = _ledger(cost_usd=None, cost_provenance=COST_UNKNOWN, model_usage=())

    assert ledger["cost_usd"] is None
    assert ledger["cost_provenance"] == COST_UNKNOWN
    assert ledger["billed_components"] == []


# ── Configured vs resolved divergence ────────────────────────────────────


def test_alias_and_concrete_version_are_both_kept_and_not_reported_as_differing() -> None:
    """An alias resolving to its own concrete version is one model, spelled twice.

    Both spellings stay visible in ``raw``; the canonical projection is what the
    divergence flag compares, so an alias is not reported as a different model
    from the version it names.
    """
    ledger = _ledger()

    assert ledger["configured_differs_from_resolved"] is False
    assert ledger["configured_identity"]["raw"] != ledger["resolved_primary_identity"]["raw"]


def _diverged() -> AgentResult:
    """An invocation configured for a CLI transport that resolved onto the API.

    Same model name, different canonical identity — the case #2225 established
    is a real distinction and #2205 has to keep on both sides of the pair.
    """
    return _result(
        configured_model="gpt-5.4",
        configured_transport="cli",
        model_used="gpt-5.4",
        transport_used="api",
        model_usage=(),
    )


def test_transport_fallback_is_reported_as_configured_differing_from_resolved() -> None:
    """A CLI profile that resolved onto an API fallback ran a different identity."""
    ledger = build_invocation_ledger(_diverged(), "dev", "dev")

    assert ledger["configured_differs_from_resolved"] is True
    assert ledger["configured_identity"]["identity"] == "openai/gpt-5.4/cli"
    assert ledger["resolved_primary_identity"]["identity"] == "openai/gpt-5.4/api"


def test_divergence_falls_back_to_raw_spellings_when_neither_canonicalizes() -> None:
    """Two unresolvable spellings are still comparable, verbatim."""
    ledger = build_invocation_ledger(
        _result(
            configured_model="house-model",
            configured_transport="api",
            model_used="house-model-v2",
            transport_used="api",
            model_usage=(),
        ),
        "dev",
        "dev",
    )

    assert ledger["configured_identity"]["resolution"] == "unresolved"
    assert ledger["configured_differs_from_resolved"] is True


def test_divergence_is_null_rather_than_false_when_one_side_is_absent() -> None:
    """ "Not recorded" must not read as "the same" — that is the collapse again."""
    ledger = build_invocation_ledger(
        _result(configured_model=None, configured_transport=None, model_usage=()),
        "dev",
        "dev",
    )

    assert ledger["configured_identity"] is None
    assert ledger["configured_differs_from_resolved"] is None


# ── Reader ───────────────────────────────────────────────────────────────


def test_reader_projects_the_full_ledger_from_an_entry() -> None:
    entry = {"role": "dev", "ledger": _ledger()}

    projection = entry_identity_ledger(entry)

    assert projection is not None
    assert projection["full_ledger"] is True
    assert projection["version"] == INVOCATION_LEDGER_VERSION
    assert projection["configured"][1] == SOURCE_DIRECT
    assert projection["resolved"][1] == SOURCE_DIRECT
    assert projection["differs"] is False
    assert len(projection["billed"]) == 2
    assert projection["cost_provenance"] == COST_PROVIDER_REPORTED
    assert projection["reasoning_effort"] == "high"
    assert projection["complexity_score"] == 8


def test_single_identity_projection_reads_the_resolved_model_from_the_ledger() -> None:
    """``entry_model_identity_detail`` stays one identity, now defined as resolved."""
    entry = {"role": "dev", "ledger": build_invocation_ledger(_diverged(), "dev", "dev")}

    detail = entry_model_identity_detail(entry)

    assert detail is not None
    assert detail[0] == "openai/gpt-5.4/api"
    assert detail[1] == SOURCE_DIRECT


def test_ledger_without_a_resolved_identity_falls_back_to_configured_as_recovered() -> None:
    """A profile that never dispatched still names what it would have run."""
    entry = {
        "role": "dev",
        "ledger": build_invocation_ledger(
            _result(model_used=None, transport_used=None, model_usage=()), "dev", "dev"
        ),
    }

    detail = entry_model_identity_detail(entry)

    assert detail is not None
    assert detail[1] == SOURCE_RECOVERED


# ── Legacy records ───────────────────────────────────────────────────────


def test_pre_ledger_entry_still_reads_and_is_marked_as_not_full() -> None:
    """A record written before the ledger keeps reading exactly as it did."""
    legacy = {"role": "dev", "model_used": "claude-sonnet-4-6", "transport_used": "cli"}

    projection = entry_identity_ledger(legacy)
    assert projection is not None
    assert projection["full_ledger"] is False
    assert projection["configured"] is None
    assert projection["resolved"] is not None
    assert projection["resolved"][1] == SOURCE_DIRECT

    # The pre-ledger single-identity read is byte-for-byte unchanged.
    assert entry_model_identity_detail(legacy) == projection["resolved"]


def test_no_ledger_is_fabricated_for_a_legacy_record_by_migration() -> None:
    """v20 → v21 must not invent a ledger; the absence is the readable fact."""
    v20 = _record([{"role": "dev", "model_used": "claude-sonnet-4-6"}])

    migrated = audit_substrate._migrate_v20_to_v21(v20)

    assert "ledger" not in migrated["cost"]["agents"][0]
    assert dev_identity_ledger(migrated)["full_ledger"] is False


def test_a_full_ledger_dev_entry_wins_over_a_legacy_one_regardless_of_order() -> None:
    """A later attempt carrying the ledger must not be reported as pre-ledger."""
    record = _record(
        [
            {"role": "dev", "model_used": "claude-sonnet-4-6"},
            {"role": "dev", "ledger": _ledger()},
        ]
    )

    assert dev_identity_ledger(record)["full_ledger"] is True


def test_record_without_any_dev_entry_returns_the_empty_projection() -> None:
    projection = dev_identity_ledger(_record([{"role": "review", "ledger": _ledger()}]))

    assert projection["full_ledger"] is False
    assert projection["configured"] is None
    assert projection["resolved"] is None


# ── Index ────────────────────────────────────────────────────────────────


def _seed(tmp_path: Path, record: dict) -> dict:
    audit_substrate.seed_records(tmp_path, [record])
    conn = audit_substrate.create_or_open(tmp_path)
    try:
        row = conn.execute(
            "SELECT dev_model, dev_configured_model, dev_configured_model_resolution, "
            "dev_resolved_model, dev_resolved_model_resolution, "
            "dev_identity_ledger_version, dev_identity_ledger_full "
            "FROM audit_records WHERE run_id = ?",
            (audit_substrate.derive_run_id(record),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


def test_index_carries_configured_and_resolved_identity_separately(tmp_path: Path) -> None:
    """A consumer reading the index sees the same identities the record carries."""
    record = _record(
        [{"role": "dev", "ledger": build_invocation_ledger(_diverged(), "dev", "dev")}]
    )

    row = _seed(tmp_path, record)

    assert row["dev_configured_model"] == "openai/gpt-5.4/cli"
    assert row["dev_resolved_model"] == "openai/gpt-5.4/api"
    # dev_model stays the resolved identity so existing consumers are unmoved.
    assert row["dev_model"] == row["dev_resolved_model"]
    assert row["dev_identity_ledger_version"] == INVOCATION_LEDGER_VERSION
    assert row["dev_identity_ledger_full"] == 1


def test_index_marks_a_legacy_record_as_not_carrying_the_full_ledger(
    tmp_path: Path,
) -> None:
    record = _record([{"role": "dev", "model_used": "claude-sonnet-4-6", "transport_used": "cli"}])

    row = _seed(tmp_path, record)

    assert row["dev_identity_ledger_full"] == 0
    assert row["dev_configured_model"] is None
    # The resolved column is still populated: the pre-ledger value *is* the
    # resolved identity, just recovered rather than recorded.
    assert row["dev_resolved_model"] == row["dev_model"]
    assert row["dev_model"]


def test_reindex_rederives_both_old_and_new_identity_columns(tmp_path: Path) -> None:
    """The repair path must fill the new columns on already-indexed history.

    A substrate written under an older version has the new columns empty on
    every row. Opening it re-derives them from ``raw_json``, which is what keeps
    the projection from applying only to rows written after the bump (#2201).
    """
    record = _record([{"role": "dev", "ledger": _ledger()}])
    audit_substrate.seed_records(tmp_path, [record])

    conn = audit_substrate.create_or_open(tmp_path)
    try:
        conn.execute(
            "UPDATE audit_records SET dev_model = NULL, dev_configured_model = NULL, "
            "dev_resolved_model = NULL, dev_identity_ledger_full = NULL"
        )
        conn.commit()
        updated = audit_substrate._reindex_dev_model_identity(conn)
        row = conn.execute(
            "SELECT dev_model, dev_configured_model, dev_resolved_model, "
            "dev_identity_ledger_full FROM audit_records"
        ).fetchone()
    finally:
        conn.close()

    assert updated == 1
    assert row["dev_model"]
    assert row["dev_configured_model"]
    assert row["dev_resolved_model"]
    assert row["dev_identity_ledger_full"] == 1


# ── Routing projections ──────────────────────────────────────────────────


def test_escalation_projection_carries_both_identities(tmp_path: Path) -> None:
    """Cost and outcome are attributable to configured and resolved independently."""
    record = {
        "run_id": "r1",
        "task": {"slug": "story"},
        "outcome": {"success": True},
        "preflight": {"complexity": "large", "complexity_score": 8},
        "timing": {"started_at": "2026-01-01T00:00:00+00:00"},
        "cost": {
            "agents": [
                {"role": "dev", "ledger": build_invocation_ledger(_diverged(), "dev", "dev")}
            ]
        },
    }

    projected = audit_substrate._derive_escalation(record)

    assert projected is not None
    assert projected["dev_configured_model"] == "openai/gpt-5.4/cli"
    assert projected["dev_resolved_model"] == "openai/gpt-5.4/api"
    # Routing still keys off the model that actually ran, as it always has.
    assert projected["dev_model"] == projected["dev_resolved_model"]
    assert projected["dev_configured_differs_from_resolved"] is True
    assert projected["dev_identity_ledger_full"] is True


def test_escalation_projection_of_a_legacy_record_says_so(tmp_path: Path) -> None:
    record = {
        "run_id": "r1",
        "task": {"slug": "story"},
        "outcome": {"success": False},
        "preflight": {"complexity": "medium"},
        "timing": {"started_at": "2026-01-01T00:00:00+00:00"},
        "cost": {"agents": [{"role": "dev", "model_used": "claude-sonnet-4-6"}]},
    }

    projected = audit_substrate._derive_escalation(record)

    assert projected is not None
    assert projected["dev_identity_ledger_full"] is False
    assert projected["dev_configured_model"] is None
    assert projected["dev_configured_differs_from_resolved"] is None
    # Legacy records keep resolving to the identity they always did.
    assert projected["dev_resolved_model"] == dev_model_identity_detail(record)[0]


# ── Seam: coordinator phases → record → index ────────────────────────────


def test_every_agent_invoking_phase_records_a_ledger_through_a_real_run(
    tmp_path: Path,
) -> None:
    """The ledger is a property of the run's audit, not of one phase's renderer.

    A cross-phase test rather than a unit one because the failure this guards is
    a phase whose results never reach ``build_agent_entries`` at all — which is
    exactly how preflight, plan, and plan review went unrecorded before #2205,
    and a unit test of the renderer cannot see it.
    """
    from coord_test_helpers import (  # noqa: PLC0415
        _PREFLIGHT_RESULT,
        APPROVE_REVIEW,
        _make_agent_result,
        _make_config,
        _make_task,
        _shell_with_gate,
        patch_gate_shell,
    )

    from theforge.coordinator.audit import generate_audit_log  # noqa: PLC0415
    from theforge.coordinator.engine import run_task  # noqa: PLC0415

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    with (
        patch_gate_shell() as mock_shell,
        patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        # Identity fields as a real runner would stamp them at the dispatch
        # seam: configured alias, resolved concrete version, billed components.
        mock_dev.return_value = dataclasses.replace(
            _make_agent_result(),
            configured_model="sonnet",
            configured_transport="cli",
            model_used="claude-sonnet-4-6",
            transport_used="cli",
            cost_provenance=COST_PROVIDER_REPORTED,
        )
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]
        result = run_task(config, task)

    record = generate_audit_log(config, task, result)
    agents = record["cost"]["agents"]

    assert {a["role"] for a in agents} == {"preflight", "dev", "review"}
    for entry in agents:
        assert entry["ledger"]["version"] == INVOCATION_LEDGER_VERSION
        assert entry["ledger"]["role"] == entry["role"]
        # The classification the invocation ran under, carried on every entry.
        assert entry["ledger"]["complexity"] == record["preflight"]["complexity"]

    # And the identity the index carries is the identity the record carries.
    audit_substrate.seed_records(tmp_path, [record])
    conn = audit_substrate.create_or_open(tmp_path)
    try:
        row = conn.execute(
            "SELECT dev_resolved_model, dev_identity_ledger_full FROM audit_records "
            "WHERE run_id = ?",
            (record["run_id"],),
        ).fetchone()
    finally:
        conn.close()

    dev_entry = next(a for a in agents if a["role"] == "dev")
    resolved = dev_entry["ledger"]["resolved_primary_identity"]
    assert row["dev_identity_ledger_full"] == 1
    assert row["dev_resolved_model"] == resolved["identity"]


def test_each_preflight_attempt_carries_its_own_ledger(tmp_path: Path) -> None:
    """A superseded preflight attempt exists nowhere but ``preflight.attempts``.

    Only the final attempt reaches ``cost.agents``, so without a ledger on each
    attempt the parse-retries and fallbacks — invocations that ran and were paid
    for — would keep the collapsed identity the rest of the run no longer has.
    """
    from coord_test_helpers import (  # noqa: PLC0415
        APPROVE_REVIEW,
        _make_agent_result,
        _make_config,
        _make_task,
        _shell_with_gate,
        patch_gate_shell,
    )

    from theforge.coordinator.engine import run_task  # noqa: PLC0415

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    proceed = (
        "```yaml\nverdict: PROCEED\ncomplexity: medium\ncomplexity_score: 5\n"
        "work_type: feature\nsufficiency: sufficient\nreason: ok\n```"
    )
    with (
        patch_gate_shell() as mock_shell,
        patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # First attempt narrates (parse-degraded) and is retried on the same
        # profile; only the second reaches state.preflight_result.
        mock_preflight.side_effect = [
            _make_agent_result(output="I think this is fine.", cost_usd=0.05),
            _make_agent_result(output=proceed, cost_usd=0.11),
        ]
        mock_dev.return_value = _make_agent_result()
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]
        result = run_task(config, task)

    attempts = result.state.preflight_result.raw["attempts"]
    assert len(attempts) == 2
    for attempt in attempts:
        ledger = attempt["ledger"]
        assert ledger["version"] == INVOCATION_LEDGER_VERSION
        assert ledger["role"] == "preflight"
        assert "configured_identity" in ledger
        assert "resolved_primary_identity" in ledger
    # The superseded attempt keeps its own cost, separately attributable.
    assert [a["cost_usd"] for a in attempts] == [0.05, 0.11]


# ── The renderer must not become a crash site ────────────────────────────


def test_unreadable_preflight_result_costs_one_entry_not_the_whole_record(
    tmp_path: Path,
) -> None:
    """Rendering the audit must never be the thing that kills a run.

    ``preflight_result`` is the one AgentResult the renderer reads that is a
    single reassigned field rather than a phase-appended list, and the rest of
    the writer already reads it duck-typed. When #2205 started rendering it, an
    unreadable value raised out of ``generate_audit_log`` and the run lost its
    *entire* audit record — the opposite of what instrumenting a phase is for.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from coord_test_helpers import _make_config, _make_task  # noqa: PLC0415

    from theforge.coordinator.audit import generate_audit_log  # noqa: PLC0415
    from theforge.coordinator.audit_render import build_agent_entries  # noqa: PLC0415
    from theforge.coordinator.state import (  # noqa: PLC0415
        CoordinatorResult,
        CoordinatorState,
        Phase,
    )

    config = _make_config(tmp_path)
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    # A stand-in used purely as a cost carrier — the shape several sprint-level
    # fixtures build, and the shape any future non-AgentResult assignment has.
    state.preflight_result = MagicMock()
    state.dev_results.append(_result())
    state.dev_durations.append(1.0)

    entries = build_agent_entries(state, config)

    # The unreadable preflight invocation is skipped rather than fabricated
    # from an unknown object's attributes...
    assert [e["role"] for e in entries] == ["dev"]
    # ...and the record as a whole is still produced.
    record = generate_audit_log(
        config,
        _make_task(tmp_path),
        CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done"),
    )
    assert record["cost"]["agents"] == entries
