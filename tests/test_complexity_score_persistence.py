"""complexity_score persistence across audit substrate, model_profiles, and
escalation history (issue #1101).

Covers AC1 (substrate column), AC2 (model_profiles preserves score),
AC3 (load_escalation_history / _check_promotion backward compatible),
AC4 (assignment_history view returns complexity_score / null).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.assignment import (
    EscalationRecord,
    _check_promotion,
    append_escalation_record,
    load_escalation_history,
)
from theforge.coordinator import audit_substrate as sub
from theforge.model_profiles import RunOutcome, apply_run

# ── AC1: substrate persists complexity_score as a queryable column ────────


def _record_with_score(run_id: str, score: int | None) -> dict:
    rec: dict = {
        "run_id": run_id,
        "task": {"slug": "demo"},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": "2026-05-07T10:00:00+00:00"},
        "totals": {"cost_usd": 1.0},
        "reviews": [],
    }
    if score is not None:
        rec["preflight"] = {"complexity_score": score}
    return rec


def test_substrate_column_exposes_complexity_score(tmp_path: Path) -> None:
    conn = sub.create_or_open(tmp_path)
    try:
        sub.upsert_run_record(conn, _record_with_score("r-1", 7), provenance="native")
        sub.upsert_run_record(conn, _record_with_score("r-2", None), provenance="native")
        rows = conn.execute(
            "SELECT run_id, complexity_score FROM audit_records ORDER BY run_id"
        ).fetchall()
    finally:
        conn.close()
    by_id = {row["run_id"]: row["complexity_score"] for row in rows}
    assert by_id == {"r-1": 7, "r-2": None}


def test_substrate_legacy_schema_alters_in_column_idempotently(tmp_path: Path) -> None:
    """A pre-existing v1 substrate gets the column added on next open."""
    import sqlite3

    db_path = sub.substrate_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_conn = sqlite3.connect(str(db_path))
    legacy_conn.executescript(
        """
        CREATE TABLE audit_records (
            run_id TEXT PRIMARY KEY,
            slug TEXT,
            started_at TEXT,
            finished_at TEXT,
            total_cost_usd REAL,
            final_phase TEXT,
            outcome_success INTEGER,
            branch TEXT,
            landing_status TEXT,
            provenance TEXT NOT NULL,
            source_path TEXT,
            source_mtime REAL,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE reviews (run_id TEXT, cycle INTEGER, verdict TEXT,
            p1_count INTEGER, p2_count INTEGER, PRIMARY KEY (run_id, cycle));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    # Reopening should add the column without error.
    conn = sub.create_or_open(tmp_path)
    try:
        sub.upsert_run_record(conn, _record_with_score("r-1", 4), provenance="native")
        row = conn.execute(
            "SELECT complexity_score FROM audit_records WHERE run_id='r-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 4
    # Second open is a no-op; ALTER would otherwise raise duplicate-column.
    sub.create_or_open(tmp_path).close()


# ── AC2: model_profiles preserves raw score alongside band ────────────────


def test_apply_run_preserves_complexity_score_alongside_band() -> None:
    data: dict = {"models": {}}
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            complexity_score=4,
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=2,
            dev_cost_usd=0.5,
        ),
    )
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            complexity_score=7,
            dev_model="sonnet",
            dev_success=False,
            dev_iterations=3,
            dev_cost_usd=0.9,
        ),
    )
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            complexity_score=4,
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=0.4,
        ),
    )
    dev = data["models"]["sonnet"]["dev"]
    # Band-level aggregation unchanged.
    assert dev["by_complexity"]["medium"]["runs"] == 3
    # Score-level buckets distinguish 4 from 7.
    by_score = dev["by_complexity_score"]
    assert by_score["4"]["runs"] == 2
    assert by_score["4"]["success_rate"] == 1.0
    assert by_score["7"]["runs"] == 1
    assert by_score["7"]["success_rate"] == 0.0


def test_apply_run_omits_score_when_none() -> None:
    data: dict = {"models": {}}
    apply_run(
        data,
        RunOutcome(
            complexity="small",
            dev_model="haiku",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=0.1,
        ),
    )
    dev = data["models"]["haiku"]["dev"]
    assert "by_complexity_score" not in dev
    # Band-level still recorded.
    assert dev["by_complexity"]["small"]["runs"] == 1


# ── AC3 + AC4: escalation history round-trip ──────────────────────────────


def test_append_and_load_escalation_record_with_score(tmp_path: Path) -> None:
    path = tmp_path / "assignment_history.yaml"
    append_escalation_record(
        path,
        EscalationRecord(
            story="abc",
            complexity="MEDIUM",
            dev_model="anthropic/sonnet/cli",
            outcome="DONE",
            complexity_score=6,
            timestamp="2026-05-07T10:00:00Z",
        ),
    )
    append_escalation_record(
        path,
        EscalationRecord(
            story="def",
            complexity="HIGH",
            dev_model="anthropic/opus/cli",
            outcome="ESCALATE",
            timestamp="2026-05-07T11:00:00Z",
        ),
    )
    records = load_escalation_history(path)
    assert len(records) == 2
    assert records[0].complexity_score == 6
    # Record without score round-trips as None (AC4: null when absent).
    assert records[1].complexity_score is None
    # Persisted YAML should not write the key when score is absent.
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "complexity_score" not in raw["escalations"][1]
    assert raw["escalations"][0]["complexity_score"] == 6


def test_load_escalation_history_handles_legacy_records(tmp_path: Path) -> None:
    """Records written before this change have no complexity_score field."""
    path = tmp_path / "assignment_history.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "escalations": [
                    {
                        "story": "old",
                        "complexity": "MEDIUM",
                        "dev_model": "anthropic/sonnet/cli",
                        "outcome": "DONE",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    records = load_escalation_history(path)
    assert len(records) == 1
    assert records[0].complexity_score is None


def test_bridge_propagates_complexity_score_from_state(tmp_path: Path) -> None:
    """Seam test: state.preflight_complexity_score reaches model_profiles.yaml."""
    from dataclasses import dataclass

    from theforge.coordinator.model_profiles_bridge import (
        build_run_outcome,
        update_profiles_from_run,
    )
    from theforge.coordinator.state import CoordinatorState

    @dataclass
    class _Profile:
        name: str

    @dataclass
    class _Cfg:
        project_root: str
        dev_profile: _Profile
        preflight_profile: _Profile | None = None

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 6
    state.dev_trace_count = 1

    @dataclass
    class _AR:
        cost_usd: float | None = 0.0

    state.dev_results = [_AR(cost_usd=0.10)]
    cfg = _Cfg(project_root=str(tmp_path), dev_profile=_Profile(name="sonnet"))

    outcome = build_run_outcome(cfg, state, success=True)
    assert outcome.complexity_score == 6

    profiles_path = tmp_path / "model_profiles.yaml"
    data = update_profiles_from_run(
        profiles_path=profiles_path,
        history_path=None,
        config=cfg,
        state=state,
        success=True,
    )
    by_score = data["models"]["sonnet"]["dev"]["by_complexity_score"]
    assert by_score["6"]["runs"] == 1


def test_check_promotion_reads_profile_signal() -> None:
    """Cross-run dev promotion now flows through the capability profiles (#158).

    The complexity_score persisted on escalation records (this file's subject,
    #1101) still round-trips, but the promotion decision no longer reads those
    records: ``_check_promotion`` consults the model profile's recency-weighted
    band success rate and fires when it is below threshold over the sample floor.
    """
    from theforge.config import AgentDef

    dev_agent = AgentDef(
        name="sonnet",
        provider="anthropic",
        model="sonnet",
        budget_usd=5.0,
        timeout_seconds=900,
        tier="mid",
    )
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "runs": 6,
                    "success_rate": 0.0,
                    "_recent": [0, 0, 0, 0, 0, 0],
                    "by_complexity": {
                        "medium": {
                            "runs": 6,
                            "success_rate": 0.0,
                            "_recent": [0, 0, 0, 0, 0, 0],
                        }
                    },
                }
            }
        }
    }
    signal = _check_promotion("MEDIUM", dev_agent, profiles, threshold=0.60, min_runs=5)
    assert signal.fired is True
    assert signal.weighted is not None and signal.weighted < signal.threshold
