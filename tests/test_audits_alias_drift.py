"""Tests for ``forge audits alias-drift`` (issue #2226 AC4).

A change in what a family alias resolves to has to be detectable from recorded
runs rather than only from a behavioural surprise. This is the operator surface
over the substrate's per-invocation identity index.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from theforge.cli import audits as audits_cli
from theforge.coordinator import audit_substrate as sub
from theforge.coordinator.agent_identity import canonicalize_identity


def _setup_project(tmp_path: Path) -> Path:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    return forge_yaml


def _identity_block(raw: str) -> dict:
    identity, resolution = canonicalize_identity(raw, {"transport_used": "cli"})
    return {"raw": raw, "transport": "cli", "identity": identity, "resolution": resolution}


def _record(run_id: str, started_at: str, configured: str, served: str) -> dict:
    configured_block, served_block = _identity_block(configured), _identity_block(served)
    return {
        "run_id": run_id,
        "task": {"slug": "demo", "name": "demo"},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": started_at, "duration_seconds": 60.0},
        "totals": {"cost_usd": 1.0, "duration_s": 60.0},
        "cost": {
            "total_usd": 1.0,
            "agents": [
                {
                    "role": "dev",
                    "profile": "dev",
                    "cost_usd": 1.0,
                    "ledger": {
                        "version": 1,
                        "role": "dev",
                        "profile": "dev",
                        "configured_identity": configured_block,
                        "resolved_primary_identity": served_block,
                        "configured_differs_from_resolved": (
                            configured_block["identity"] != served_block["identity"]
                        ),
                        "billed_components": [],
                    },
                }
            ],
        },
    }


def _index(tmp_path: Path, records: list[dict]) -> None:
    conn = sub.create_or_open(tmp_path)
    try:
        for record in records:
            sub.upsert_run_record(conn, record, provenance="native")
        conn.commit()
    finally:
        conn.close()


def _args(forge_yaml: Path, **kw) -> SimpleNamespace:
    base = dict(config=str(forge_yaml), audits_command="alias-drift", changed_only=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_moved_alias_is_reported_as_changed(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    _index(
        tmp_path,
        [
            _record("r1", "2026-03-01T10:00:00+00:00", "opus", "claude-opus-4-6"),
            _record("r2", "2026-04-01T10:00:00+00:00", "opus", "claude-opus-5"),
        ],
    )

    assert audits_cli.cmd_audits(_args(forge_yaml)) == 0
    out = capsys.readouterr().out

    assert "anthropic/opus/cli" in out
    assert "CHANGED" in out
    assert "anthropic/claude-opus-4-6/cli" in out
    assert "anthropic/claude-opus-5/cli" in out


def test_a_stable_alias_is_reported_as_stable_and_hidden_by_changed_only(
    tmp_path: Path, capsys
) -> None:
    forge_yaml = _setup_project(tmp_path)
    _index(
        tmp_path,
        [
            _record("r1", "2026-03-01T10:00:00+00:00", "opus", "claude-opus-4-6"),
            _record("r2", "2026-04-01T10:00:00+00:00", "opus", "claude-opus-4-6"),
        ],
    )

    assert audits_cli.cmd_audits(_args(forge_yaml)) == 0
    assert "stable" in capsys.readouterr().out

    assert audits_cli.cmd_audits(_args(forge_yaml, changed_only=True)) == 0
    assert "no recorded invocation" in capsys.readouterr().out


def test_a_pinned_candidate_shows_its_own_identity_on_both_sides(tmp_path: Path, capsys) -> None:
    """Nothing about a pinned configuration reads as drift."""
    forge_yaml = _setup_project(tmp_path)
    _index(
        tmp_path,
        [_record("r1", "2026-03-01T10:00:00+00:00", "claude-opus-4-6", "claude-opus-4-6")],
    )

    assert audits_cli.cmd_audits(_args(forge_yaml, changed_only=True)) == 0
    assert "no recorded invocation" in capsys.readouterr().out


def test_an_alias_that_returned_to_an_earlier_version_reports_it_as_current(
    tmp_path: Path, capsys
) -> None:
    """A → B → A: ``current`` is the newest resolution, not the newest *first* seen.

    ``resolved_models`` is ordered by first appearance, which is the readable
    order for a drift history. Reading its tail would name B here, even though
    the newest recorded invocation resolved back to A.
    """
    forge_yaml = _setup_project(tmp_path)
    _index(
        tmp_path,
        [
            _record("r1", "2026-03-01T10:00:00+00:00", "opus", "claude-opus-4-6"),
            _record("r2", "2026-04-01T10:00:00+00:00", "opus", "claude-opus-5"),
            _record("r3", "2026-05-01T10:00:00+00:00", "opus", "claude-opus-4-6"),
        ],
    )

    conn = sub.create_or_open(tmp_path)
    try:
        timeline = sub.alias_resolution_timeline(conn)
    finally:
        conn.close()

    entry = timeline[0]
    assert entry["changed"] is True
    assert entry["current"] == "anthropic/claude-opus-4-6/cli"
    # First-appearance ordering is preserved for the history itself.
    assert [r["resolved_model"] for r in entry["resolved_models"]] == [
        "anthropic/claude-opus-4-6/cli",
        "anthropic/claude-opus-5/cli",
    ]
    # ...and the returned-to version records both of its invocations.
    assert entry["resolved_models"][0]["invocations"] == 2
    assert entry["resolved_models"][0]["last_run_id"] == "r3"

    assert audits_cli.cmd_audits(_args(forge_yaml, changed_only=True)) == 0
    assert "CHANGED" in capsys.readouterr().out


def _preflight_ledger(configured: str, served: str) -> dict:
    configured_block, served_block = _identity_block(configured), _identity_block(served)
    return {
        "version": 1,
        "role": "preflight",
        "profile": "preflight",
        "configured_identity": configured_block,
        "resolved_primary_identity": served_block,
        "configured_differs_from_resolved": (
            configured_block["identity"] != served_block["identity"]
        ),
        "billed_components": [],
    }


def test_an_ordinary_preflight_is_counted_once_not_twice(tmp_path: Path) -> None:
    """The final preflight attempt lives in BOTH record surfaces (#2226).

    ``preflight.attempts`` records every attempt including the final one, and
    that final attempt is also the ``cost.agents`` preflight entry. Indexing both
    would double every ordinary run's preflight in the drift counts.
    """
    final = _preflight_ledger("opus", "claude-opus-5")
    record = {
        "run_id": "r1",
        "task": {"slug": "demo", "name": "demo"},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": "2026-03-01T10:00:00+00:00", "duration_seconds": 60.0},
        "totals": {"cost_usd": 1.0, "duration_s": 60.0},
        "cost": {
            "total_usd": 1.0,
            "agents": [{"role": "preflight", "profile": "preflight", "ledger": final}],
        },
        "preflight": {"attempts": [{"profile_name": "preflight", "ledger": final}]},
    }
    _setup_project(tmp_path)
    _index(tmp_path, [record])

    conn = sub.create_or_open(tmp_path)
    try:
        timeline = sub.alias_resolution_timeline(conn)
    finally:
        conn.close()

    entry = timeline[0]
    assert entry["configured_model"] == "anthropic/opus/cli"
    assert entry["invocations"] == 1
    assert entry["resolved_models"][0]["invocations"] == 1


def test_a_preflight_fallback_counts_both_invocations(tmp_path: Path) -> None:
    """De-duplication must not swallow the superseded attempt."""
    superseded = _preflight_ledger("opus", "claude-opus-4-6")
    final = _preflight_ledger("opus", "claude-opus-5")
    record = {
        "run_id": "r1",
        "task": {"slug": "demo", "name": "demo"},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": "2026-03-01T10:00:00+00:00", "duration_seconds": 60.0},
        "totals": {"cost_usd": 1.0, "duration_s": 60.0},
        "cost": {
            "total_usd": 1.0,
            "agents": [{"role": "preflight", "profile": "preflight", "ledger": final}],
        },
        "preflight": {
            "attempts": [
                {"profile_name": "preflight", "ledger": superseded},
                {"profile_name": "preflight", "ledger": final},
            ]
        },
    }
    _setup_project(tmp_path)
    _index(tmp_path, [record])

    conn = sub.create_or_open(tmp_path)
    try:
        timeline = sub.alias_resolution_timeline(conn)
    finally:
        conn.close()

    entry = timeline[0]
    assert entry["invocations"] == 2
    assert entry["distinct_resolved"] == 2
    assert entry["changed"] is True
