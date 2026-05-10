"""Regression: sprint-summary persistence is keyed by run_id, not sprint name.

When two sprints share a sprint name (the common dogfood pattern: repeatedly
sprinting the same issue produces deterministic name like ``issues-1326``),
the later run's summary write must not destroy the earlier run's history.
``forge status <earlier-run-id>`` against any prior run must still return that
run's correct sprint state — outcome, cost, duration, per-story status —
regardless of how many later same-name runs have completed since.

This test exercises the actual writer (``_write_sprint_summary``) and the
actual ``forge status`` reader path (``find_sprint_summary`` + the
``cli.sprint_status`` lookup helpers), per the fix-success criterion in
issue #1480.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

from theforge.sprint.audit import _write_sprint_audit, _write_sprint_summary
from theforge.sprint.status_reader import find_sprint_summary, read_completed_status


def _make_result(run_cost: float, succeeded: int, failed: int) -> SimpleNamespace:
    state = SimpleNamespace(
        preflight_verdict="PROCEED",
        review_results=[],
        total_cost=run_cost,
        error=None,
        error_type=None,
        review_cycle_metadata=[],
        dev_iteration_telemetry=[],
        review_iteration_telemetry=[],
        preflight_cached=False,
    )
    return SimpleNamespace(
        results=[
            (
                "issue:1326",
                SimpleNamespace(
                    state=state,
                    phase=SimpleNamespace(name="DONE" if succeeded else "ESCALATE"),
                    success=bool(succeeded),
                    merge=None,
                ),
            )
        ],
        name="issues-1326",
        total_cost_usd=run_cost,
        specs_total=1,
        specs_succeeded=succeeded,
        specs_failed=failed,
        specs_skipped=0,
        stopped_reason=None,
    )


def _write_summary_for_run(
    *, project_root: Path, sprint_name: str, run_id: str, run_cost: float, outcome_done: bool
) -> None:
    manifest = SimpleNamespace(name=sprint_name, budget_usd=10.0, max_parallel=1)
    result = _make_result(
        run_cost,
        succeeded=1 if outcome_done else 0,
        failed=0 if outcome_done else 1,
    )
    started_at = datetime.datetime(2026, 5, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)
    finished_at = datetime.datetime(2026, 5, 9, 12, 5, 0, tzinfo=datetime.timezone.utc)
    sprint_log_dir = project_root / ".forge" / "logs" / sprint_name
    _write_sprint_summary(
        manifest=manifest,
        result=result,
        canonical_refs=["issue:1326"],
        started_at=started_at,
        finished_at=finished_at,
        duration=300.0,
        sprint_log_dir=sprint_log_dir,
        slug_map={"issue:1326": "issue-1326"},
        run_id=run_id,
        project_root=project_root,
    )


def test_two_runs_same_sprint_name_each_run_id_resolves_to_its_own_summary(
    tmp_path: Path,
) -> None:
    """The motivating scenario from issue #1480.

    Both runs use ``--issues 1326`` and share sprint name ``issues-1326``.
    After the second run completes, ``forge status <first-run-id>`` must
    return run-1's data, not "No sprint data found" and not run-2's data.
    """
    sprint_name = "issues-1326"
    first_run_id = "d7ac606000ef"
    second_run_id = "9e984775b118"

    _write_summary_for_run(
        project_root=tmp_path,
        sprint_name=sprint_name,
        run_id=first_run_id,
        run_cost=1.23,
        outcome_done=True,
    )
    _write_summary_for_run(
        project_root=tmp_path,
        sprint_name=sprint_name,
        run_id=second_run_id,
        run_cost=4.56,
        outcome_done=False,
    )

    legacy_path = tmp_path / ".forge" / "logs" / sprint_name / "sprint-summary.yaml"
    legacy = yaml.safe_load(legacy_path.read_text())
    assert legacy["sprint"]["run_id"] == second_run_id

    first_path = find_sprint_summary(first_run_id, tmp_path)
    second_path = find_sprint_summary(second_run_id, tmp_path)

    assert first_path is not None, "earlier run lookup must not return None"
    assert second_path is not None
    assert first_path != second_path

    first = yaml.safe_load(first_path.read_text())
    second = yaml.safe_load(second_path.read_text())

    assert first["sprint"]["run_id"] == first_run_id
    assert first["sprint"]["total_cost_usd"] == 1.23
    assert first["sprint"]["specs_succeeded"] == 1

    assert second["sprint"]["run_id"] == second_run_id
    assert second["sprint"]["total_cost_usd"] == 4.56
    assert second["sprint"]["specs_failed"] == 1


def test_read_completed_status_returns_per_run_entries_after_overwrite(
    tmp_path: Path,
) -> None:
    """End-to-end: read_completed_status against the resolved per-run path
    yields each run's own per-story status — proving the historical run is
    queryable through the actual forge-status reader, not just the path."""
    sprint_name = "issues-1326"
    first_run_id = "run-alpha"
    second_run_id = "run-beta"

    _write_summary_for_run(
        project_root=tmp_path,
        sprint_name=sprint_name,
        run_id=first_run_id,
        run_cost=0.50,
        outcome_done=True,
    )
    _write_summary_for_run(
        project_root=tmp_path,
        sprint_name=sprint_name,
        run_id=second_run_id,
        run_cost=2.00,
        outcome_done=False,
    )

    first_path = find_sprint_summary(first_run_id, tmp_path)
    assert first_path is not None
    first_entries = read_completed_status(first_path)
    assert len(first_entries) == 1
    assert first_entries[0].slug == "issue-1326"
    assert first_entries[0].status == "done"

    second_path = find_sprint_summary(second_run_id, tmp_path)
    assert second_path is not None
    second_entries = read_completed_status(second_path)
    assert len(second_entries) == 1
    assert second_entries[0].status == "failed"


def test_sprint_audit_yaml_per_run_record_survives_later_same_name_run(
    tmp_path: Path,
) -> None:
    """The project-level sprint-audit.yaml is also overwritten by later runs;
    the per-run-keyed copy at audits/run-<id>-sprint-audit.yaml must preserve
    earlier runs' audit data."""
    sprint_name = "issues-1326"
    first_run_id = "audit-alpha"
    second_run_id = "audit-beta"

    manifest = SimpleNamespace(name=sprint_name, budget_usd=10.0, max_parallel=1)
    started_at = datetime.datetime(2026, 5, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)
    finished_at = datetime.datetime(2026, 5, 9, 12, 5, 0, tzinfo=datetime.timezone.utc)

    _write_sprint_audit(
        manifest=manifest,
        result=_make_result(0.75, succeeded=1, failed=0),
        canonical_refs=["issue:1326"],
        started_at=started_at,
        finished_at=finished_at,
        duration=300.0,
        project_root=tmp_path,
        slug_map={"issue:1326": "issue-1326"},
        run_id=first_run_id,
    )
    _write_sprint_audit(
        manifest=manifest,
        result=_make_result(3.25, succeeded=0, failed=1),
        canonical_refs=["issue:1326"],
        started_at=started_at,
        finished_at=finished_at,
        duration=300.0,
        project_root=tmp_path,
        slug_map={"issue:1326": "issue-1326"},
        run_id=second_run_id,
    )

    audits_dir = tmp_path / ".forge" / "audits"
    legacy = yaml.safe_load((audits_dir / "sprint-audit.yaml").read_text())
    assert legacy["sprint"]["total_cost_usd"] == 3.25

    per_run_first = audits_dir / f"run-{first_run_id}-sprint-audit.yaml"
    assert per_run_first.exists()
    first_audit = yaml.safe_load(per_run_first.read_text())
    assert first_audit["sprint"]["total_cost_usd"] == 0.75

    per_run_second = audits_dir / f"run-{second_run_id}-sprint-audit.yaml"
    assert per_run_second.exists()
