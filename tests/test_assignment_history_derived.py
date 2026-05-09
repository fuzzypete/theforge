"""Tests for the audit-substrate-derived assignment-history view (#793).

Covers three boundaries the YAML-snapshot replacement crosses:

1. ``audit_substrate.derive_assignment_history`` reconstructs the legacy
   ``EscalationRecord`` shape from real substrate rows.
2. ``_apply_preflight_config`` sources its escalation history from the
   substrate (not ``.forge/assignment_history.yaml``) and feeds those
   records into ``assign_models``.
3. ``forge audits export-assignment-history`` writes a YAML snapshot in
   the legacy human-readable shape from the substrate.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import yaml
from coord_test_helpers import _make_config

from theforge.config import AgentDef, AssignmentConfig
from theforge.coordinator import audit_substrate as sub
from theforge.coordinator.state import CoordinatorState


def _audit_record(
    *,
    run_id: str,
    slug: str,
    success: bool,
    complexity: str = "medium",
    complexity_score: int = 5,
    dev_canonical: str = "anthropic/sonnet/cli",
    dev_model: str = "sonnet",
    started_at: str = "2026-05-08T10:00:00+00:00",
    reason: str = "",
) -> dict:
    record: dict = {
        "run_id": run_id,
        "task": {"slug": slug, "name": slug},
        "outcome": {"success": success, "final_phase": "DONE" if success else "ESCALATE"},
        "timing": {"started_at": started_at},
        "totals": {"cost_usd": 1.0},
        "preflight": {
            "verdict": "PROCEED",
            "complexity": complexity,
            "complexity_score": complexity_score,
            "complexity_routing": {
                "complexity": complexity,
                "complexity_score": complexity_score,
                "assignments": {
                    "dev": {
                        "model": dev_model,
                        "source": "builtin",
                        "canonical_id": dev_canonical,
                    }
                },
            },
        },
    }
    if reason:
        record["escalation"] = {"reason": reason}
    return record


def _seed_substrate(project_root: Path, records: list[dict]) -> None:
    runs = sub.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    for rec in records:
        (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    sub.rebuild_from_runs(project_root)


# ── derive_assignment_history ────────────────────────────────────────────


def test_derive_assignment_history_orders_chronologically(tmp_path: Path) -> None:
    _seed_substrate(
        tmp_path,
        [
            _audit_record(
                run_id="r-late",
                slug="story-b",
                success=False,
                started_at="2026-05-08T11:00:00+00:00",
                reason="persistent P1",
            ),
            _audit_record(
                run_id="r-early",
                slug="story-a",
                success=True,
                started_at="2026-05-08T09:00:00+00:00",
            ),
        ],
    )
    conn = sub.require_substrate(tmp_path)
    try:
        derived = sub.derive_assignment_history(conn)
    finally:
        conn.close()

    assert [d["story"] for d in derived] == ["story-a", "story-b"]
    assert [d["outcome"] for d in derived] == ["DONE", "ESCALATE"]
    assert [d["complexity"] for d in derived] == ["MEDIUM", "MEDIUM"]
    assert [d["dev_model"] for d in derived] == [
        "anthropic/sonnet/cli",
        "anthropic/sonnet/cli",
    ]
    assert derived[1]["reason"] == "persistent P1"
    assert derived[0]["complexity_score"] == 5


def test_derive_assignment_history_skips_records_without_routing(tmp_path: Path) -> None:
    rec_no_routing = {
        "run_id": "r-old",
        "task": {"slug": "ancient"},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": "2026-01-01T00:00:00+00:00"},
        "preflight": {"verdict": "PROCEED"},  # no complexity / routing
    }
    _seed_substrate(
        tmp_path,
        [
            rec_no_routing,
            _audit_record(run_id="r-new", slug="modern", success=True),
        ],
    )
    conn = sub.require_substrate(tmp_path)
    try:
        derived = sub.derive_assignment_history(conn)
    finally:
        conn.close()

    assert [d["story"] for d in derived] == ["modern"]


# ── preflight seam: substrate → assign_models ────────────────────────────


def test_preflight_loads_escalation_history_from_substrate(tmp_path, monkeypatch) -> None:
    """The new read path must hand substrate-derived records to assign_models."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    _seed_substrate(
        tmp_path,
        [
            _audit_record(
                run_id="esc-1",
                slug="prior-1",
                success=False,
                started_at="2026-05-08T09:00:00+00:00",
                reason="persistent P1 cycle 1",
            ),
            _audit_record(
                run_id="esc-2",
                slug="prior-2",
                success=False,
                started_at="2026-05-08T10:00:00+00:00",
                reason="persistent P1 cycle 2",
            ),
        ],
    )

    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="claude-sonnet",
                provider="anthropic",
                model="sonnet",
                budget_usd=10.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            ),
            AgentDef(
                name="claude-opus",
                provider="anthropic",
                model="opus",
                budget_usd=20.0,
                timeout_seconds=900,
                tier="strong",
                cli="claude",
            ),
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=True,
            max_cost_per_story_usd=50.0,
            min_reviewers=1,
            max_reviewers=1,
        ),
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    captured: dict = {}

    def _fake_assign_models(*args, **kwargs):
        captured["escalation_history"] = kwargs.get("escalation_history")
        from theforge.assignment import AssignmentDecision

        return AssignmentDecision(
            preflight=config.preflight_profile,
            planner=config.dev_profile,
            plan_reviewers=[],
            dev=config.dev_profile,
            code_reviewers=[],
            rationale={},
        )

    monkeypatch.setattr("theforge.assignment.assign_models", _fake_assign_models)

    _pf._apply_preflight_config(config, state)

    history = captured.get("escalation_history")
    assert history is not None and len(history) == 2, (
        "preflight must source escalation history from the audit substrate "
        "and pass it to assign_models when escalation_memory is enabled"
    )
    assert all(r.outcome == "ESCALATE" for r in history)
    assert all(r.dev_model == "anthropic/sonnet/cli" for r in history)
    assert all(r.complexity == "MEDIUM" for r in history)
    # Ordered chronologically by started_at — the YAML snapshot was append-ordered.
    assert [r.story for r in history] == ["prior-1", "prior-2"]
    assert [r.reason for r in history] == [
        "persistent P1 cycle 1",
        "persistent P1 cycle 2",
    ]
    # The legacy YAML at .forge/assignment_history.yaml is no longer the
    # source of truth — it doesn't even need to exist.
    assert not (tmp_path / ".forge" / "assignment_history.yaml").exists()


def test_preflight_with_no_audit_inputs_returns_empty_history(tmp_path, monkeypatch) -> None:
    """Fresh repos with no audit records degrade gracefully to empty history."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="claude-sonnet",
                provider="anthropic",
                model="sonnet",
                budget_usd=10.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            ),
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=True,
            max_cost_per_story_usd=30.0,
            min_reviewers=1,
            max_reviewers=1,
        ),
    )
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    captured: dict = {}

    def _fake_assign_models(*args, **kwargs):
        captured["escalation_history"] = kwargs.get("escalation_history")
        from theforge.assignment import AssignmentDecision

        return AssignmentDecision(
            preflight=config.preflight_profile,
            planner=config.dev_profile,
            plan_reviewers=[],
            dev=config.dev_profile,
            code_reviewers=[],
            rationale={},
        )

    monkeypatch.setattr("theforge.assignment.assign_models", _fake_assign_models)

    _pf._apply_preflight_config(config, state)

    # Empty list (not None) — the assign_models contract is preserved.
    assert captured["escalation_history"] == []


# ── CLI: forge audits export-assignment-history ──────────────────────────


def _setup_project(tmp_path: Path) -> Path:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    return forge_yaml


def test_export_assignment_history_writes_yaml_in_legacy_shape(tmp_path: Path, capsys) -> None:
    from theforge.cli import audits as audits_cli

    forge_yaml = _setup_project(tmp_path)
    _seed_substrate(
        tmp_path,
        [
            _audit_record(
                run_id="r-done",
                slug="story-done",
                success=True,
                started_at="2026-05-08T09:00:00+00:00",
            ),
            _audit_record(
                run_id="r-esc",
                slug="story-esc",
                success=False,
                started_at="2026-05-08T10:00:00+00:00",
                reason="persistent P1",
            ),
        ],
    )

    rc = audits_cli.cmd_audits(
        SimpleNamespace(
            config=str(forge_yaml),
            audits_command="export-assignment-history",
            output=None,
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "wrote 2 records" in out

    out_path = tmp_path / ".forge" / "assignment_history.yaml"
    assert out_path.exists()
    payload = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert "escalations" in payload
    entries = payload["escalations"]
    assert len(entries) == 2
    by_story = {e["story"]: e for e in entries}
    assert by_story["story-done"]["outcome"] == "DONE"
    assert by_story["story-done"]["complexity"] == "MEDIUM"
    assert by_story["story-done"]["dev_model"] == "anthropic/sonnet/cli"
    assert by_story["story-done"]["complexity_score"] == 5
    assert by_story["story-esc"]["outcome"] == "ESCALATE"
    assert by_story["story-esc"]["reason"] == "persistent P1"


def test_export_assignment_history_respects_explicit_output(tmp_path: Path) -> None:
    from theforge.cli import audits as audits_cli

    forge_yaml = _setup_project(tmp_path)
    _seed_substrate(tmp_path, [_audit_record(run_id="r1", slug="s1", success=True)])

    explicit = tmp_path / "out" / "history.yaml"
    rc = audits_cli.cmd_audits(
        SimpleNamespace(
            config=str(forge_yaml),
            audits_command="export-assignment-history",
            output=str(explicit),
        )
    )
    assert rc == 0
    assert explicit.exists()
    payload = yaml.safe_load(explicit.read_text(encoding="utf-8"))
    assert payload["escalations"][0]["story"] == "s1"
    # Default path must NOT be written when --output is explicit.
    assert not (tmp_path / ".forge" / "assignment_history.yaml").exists()


def test_export_assignment_history_fails_without_audit_inputs(tmp_path: Path, capsys) -> None:
    from theforge.cli import audits as audits_cli

    forge_yaml = _setup_project(tmp_path)
    rc = audits_cli.cmd_audits(
        SimpleNamespace(
            config=str(forge_yaml),
            audits_command="export-assignment-history",
            output=None,
        )
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no audit records found" in err


def test_export_assignment_history_parser_wired_under_audits(tmp_path: Path, capsys) -> None:
    """Regression guard: parser must register under `forge audits`."""
    import argparse

    from theforge.cli import audits as audits_cli

    parser = argparse.ArgumentParser(prog="forge")
    sub = parser.add_subparsers(dest="command")
    audits_cli.register_parser(sub)

    forge_yaml = _setup_project(tmp_path)
    _seed_substrate(tmp_path, [_audit_record(run_id="r1", slug="s1", success=True)])
    out_path = tmp_path / "snap.yaml"
    args = parser.parse_args(
        [
            "audits",
            "export-assignment-history",
            "--config",
            str(forge_yaml),
            "--output",
            str(out_path),
        ]
    )
    rc = audits_cli.cmd_audits(args)
    assert rc == 0
    assert out_path.exists()
