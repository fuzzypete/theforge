"""Tests for forge sprint-status command and supporting modules."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_state_file(tmp_path: Path, run_id: str, sprint_name: str, stories: list[dict]) -> Path:
    """Write a .forge/runs/{run_id}.state fixture file."""
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = runs_dir / f"{run_id}.state"
    data = {"sprint_name": sprint_name, "stories": stories}
    with open(state_path, "w") as f:
        yaml.dump(data, f)
    return state_path


def _make_summary_file(tmp_path: Path, sprint_name: str, run_id: str, stories: list[dict]) -> Path:
    """Write a .forge/logs/{sprint_name}/sprint-summary.yaml fixture file."""
    log_dir = tmp_path / ".forge" / "logs" / sprint_name
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / "sprint-summary.yaml"
    data = {
        "sprint": {
            "name": sprint_name,
            "run_id": run_id,
            "budget_usd": 10.0,
            "max_parallel": 2,
            "total_cost_usd": 1.5,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T01:00:00Z",
            "duration_seconds": 3600.0,
            "specs_total": len(stories),
            "specs_succeeded": 0,
            "specs_failed": 0,
            "specs_skipped": 0,
            "stopped_reason": None,
            "ci_break_slug": None,
        },
        "stories": stories,
        "iteration_usage_distribution": [],
    }
    with open(summary_path, "w") as f:
        yaml.dump(data, f)
    return summary_path


# ── read_live_status ──────────────────────────────────────────────────────────


def test_read_live_status_returns_none_when_missing(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_live_status

    result = read_live_status("nonexistent-run", tmp_path)
    assert result is None


def test_read_live_status_parses_state_file(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_live_status

    _make_state_file(
        tmp_path,
        "abc123",
        "my-sprint",
        [
            {
                "slug": "issue-1",
                "path": "Issue #1",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.12,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": "medium",
                "detail": {"review_cycle": 0, "dev_iteration": 2},
            },
            {
                "slug": "issue-2",
                "path": "Issue #2",
                "status": "waiting",
                "phase": "PREFLIGHT",
                "cost_usd": 0.0,
                "bundle_candidate": True,
                "blocked_by": [],
                "detail": {},
            },
        ],
    )

    entries = read_live_status("abc123", tmp_path)
    assert entries is not None
    assert len(entries) == 2

    e1 = entries[0]
    assert e1.slug == "issue-1"
    assert e1.status == "running"
    assert e1.phase == "DEV"
    assert e1.cost_usd == pytest.approx(0.12)
    assert e1.bundle_candidate is False
    assert e1.complexity == "medium"
    assert e1.detail == "c1 i2"

    e2 = entries[1]
    assert e2.slug == "issue-2"
    assert e2.status == "waiting"
    assert e2.bundle_candidate is True
    assert e2.detail == "waiting"


def test_read_live_status_blocked_story(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_live_status

    _make_state_file(
        tmp_path,
        "run42",
        "sprint-x",
        [
            {
                "slug": "issue-99",
                "path": "Issue #99",
                "status": "blocked",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": ["issue-98"],
            }
        ],
    )

    entries = read_live_status("run42", tmp_path)
    assert entries is not None
    assert len(entries) == 1
    assert entries[0].status == "blocked"
    assert entries[0].blocked_by == ["issue-98"]


# ── find_sprint_summary ───────────────────────────────────────────────────────


def test_find_sprint_summary_matches_run_id(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import find_sprint_summary

    _make_summary_file(tmp_path, "sprint-a", "run-001", [])
    _make_summary_file(tmp_path, "sprint-b", "run-002", [])

    result = find_sprint_summary("run-002", tmp_path)
    assert result is not None
    assert result.parent.name == "sprint-b"


def test_find_sprint_summary_returns_none_when_not_found(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import find_sprint_summary

    _make_summary_file(tmp_path, "sprint-a", "run-001", [])

    result = find_sprint_summary("run-999", tmp_path)
    assert result is None


def test_find_sprint_summary_returns_none_when_no_logs(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import find_sprint_summary

    result = find_sprint_summary("run-001", tmp_path)
    assert result is None


# ── read_completed_status ────────────────────────────────────────────────────


def test_read_completed_status_outcome_mapping(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_completed_status

    stories = [
        {"slug": "s1", "path": "Issue #1", "outcome": "DONE", "cost_usd": 0.5},
        {"slug": "s2", "path": "Issue #2", "outcome": "ALREADY_DONE", "cost_usd": 0.0},
        {"slug": "s3", "path": "Issue #3", "outcome": "SKIPPED", "cost_usd": 0.0},
        {"slug": "s4", "path": "Issue #4", "outcome": "ESCALATE", "cost_usd": 0.8},
    ]
    summary_path = _make_summary_file(tmp_path, "sprint-done", "run-xyz", stories)

    entries = read_completed_status(summary_path)
    assert len(entries) == 4

    by_slug = {e.slug: e for e in entries}
    assert by_slug["s1"].status == "done"
    assert by_slug["s2"].status == "done"
    assert by_slug["s3"].status == "skipped"
    assert by_slug["s4"].status == "failed"


def test_read_completed_status_bundle_candidate_from_audit(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_completed_status

    stories = [
        {"slug": "issue-10", "path": "Issue #10", "outcome": "DONE", "cost_usd": 0.2},
        {"slug": "issue-11", "path": "Issue #11", "outcome": "DONE", "cost_usd": 0.2},
    ]
    summary_path = _make_summary_file(tmp_path, "bundle-sprint", "runB", stories)

    # Write per-story audit for issue-10 with bundle_candidate=True
    audit_dir = tmp_path / ".forge" / "logs" / "bundle-sprint" / "issue-10"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_data = {"preflight": {"bundle_candidate": True, "verdict": "PROCEED"}}
    with open(audit_dir / "audit.yaml", "w") as f:
        yaml.dump(audit_data, f)

    entries = read_completed_status(summary_path)
    by_slug = {e.slug: e for e in entries}

    assert by_slug["issue-10"].bundle_candidate is True
    assert by_slug["issue-11"].bundle_candidate is False  # no audit file


def test_read_completed_status_blocked_by_from_depends_on(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_completed_status

    stories = [
        {"slug": "issue-5", "path": "Issue #5", "outcome": "DONE", "cost_usd": 0.3},
        {
            "slug": "issue-6",
            "path": "Issue #6",
            "outcome": "SKIPPED",
            "cost_usd": 0.0,
            "depends_on": ["issue-5"],
        },
        # DONE story with depends_on should NOT populate blocked_by
        {
            "slug": "issue-7",
            "path": "Issue #7",
            "outcome": "DONE",
            "cost_usd": 0.1,
            "depends_on": ["issue-5"],
        },
    ]
    summary_path = _make_summary_file(tmp_path, "dep-sprint", "run-dep", stories)

    entries = read_completed_status(summary_path)
    by_slug = {e.slug: e for e in entries}

    assert by_slug["issue-6"].status == "skipped"
    assert by_slug["issue-6"].blocked_by == ["issue-5"]
    # Non-skipped stories should not show depends_on as blocked_by
    assert by_slug["issue-5"].blocked_by == []
    assert by_slug["issue-7"].blocked_by == []


# ── cmd_sprint_status (output) ───────────────────────────────────────────────


def _make_forge_config(tmp_path: Path) -> Path:
    """Write a minimal forge.yaml and return its path."""
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        "project: test\n"
        "workspace:\n"
        "  path_pattern: .forge/worktrees/{slug}\n"
        "  base_branch: main\n"
        "  branch_pattern: feat/{slug}\n"
        "models:\n"
        "  dev: claude-haiku-4-5-20251001\n"
        "  reviewer: claude-haiku-4-5-20251001\n"
        "sprint:\n"
        "  max_parallel: 1\n"
    )
    return forge_yaml


def _run_sprint_status(tmp_path: Path, run_id: str) -> tuple[int, str]:
    """Run cmd_sprint_status and return (exit_code, captured_stdout)."""
    from unittest.mock import MagicMock

    from theforge.cli.sprint_status import cmd_sprint_status

    forge_yaml = _make_forge_config(tmp_path)

    # Build a minimal config mock so we don't need a complete ForgeConfig object.
    fake_config = MagicMock()
    fake_config.project_root = tmp_path

    class FakeArgs:
        pass

    args = FakeArgs()
    args.run_id = run_id  # type: ignore[attr-defined]

    buf = io.StringIO()
    with (
        patch("theforge.cli.shared._find_config", return_value=forge_yaml),
        patch("theforge.config.load_config", return_value=fake_config),
        patch("sys.stdout", buf),
    ):
        code = cmd_sprint_status(args)

    return code, buf.getvalue()


def test_cmd_sprint_status_completed_sprint(tmp_path: Path) -> None:
    stories = [
        {"slug": "issue-1", "path": "Issue #1", "outcome": "DONE", "cost_usd": 0.42},
        {"slug": "issue-2", "path": "Issue #2", "outcome": "ESCALATE", "cost_usd": 0.10},
        {"slug": "issue-3", "path": "Issue #3", "outcome": "SKIPPED", "cost_usd": 0.0},
    ]
    _make_summary_file(tmp_path, "my-sprint", "run123", stories)

    code, output = _run_sprint_status(tmp_path, "run123")

    assert code == 0
    assert "my-sprint" in output
    assert "run123" in output
    assert "Issue #1" in output
    assert "done" in output
    assert "Issue #2" in output
    assert "ESCALATE" in output or "failed" in output
    assert "Issue #3" in output
    assert "skipped" in output


def test_cmd_sprint_status_bundle_grouping(tmp_path: Path) -> None:
    stories = [
        {"slug": "issue-10", "path": "Issue #10", "outcome": "DONE", "cost_usd": 0.1},
        {"slug": "issue-11", "path": "Issue #11", "outcome": "DONE", "cost_usd": 0.1},
        {"slug": "issue-12", "path": "Issue #12", "outcome": "DONE", "cost_usd": 0.3},
    ]
    _make_summary_file(tmp_path, "bundle-sprint", "runB2", stories)

    # issue-10 and issue-11 are bundle candidates
    for slug in ("issue-10", "issue-11"):
        audit_dir = tmp_path / ".forge" / "logs" / "bundle-sprint" / slug
        audit_dir.mkdir(parents=True, exist_ok=True)
        with open(audit_dir / "audit.yaml", "w") as f:
            yaml.dump({"preflight": {"bundle_candidate": True}}, f)

    code, output = _run_sprint_status(tmp_path, "runB2")

    assert code == 0
    # Bundle header should appear before the individual bundle entries
    assert "[bundle:" in output
    assert "Issue #10" in output
    assert "Issue #11" in output
    assert "Issue #12" in output


def test_cmd_sprint_status_live_sprint(tmp_path: Path) -> None:
    # Write PID file to simulate live sprint
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "live-run.pid").write_text("99999\nmy-live-sprint\n")

    _make_state_file(
        tmp_path,
        "live-run",
        "my-live-sprint",
        [
            {
                "slug": "issue-20",
                "path": "Issue #20",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": "medium",
                "detail": {"review_cycle": 0, "dev_iteration": 2},
            },
            {
                "slug": "issue-21",
                "path": "Issue #21",
                "status": "waiting",
                "phase": "PREFLIGHT",
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {
                    "preflight_verdict": "PROCEED",
                    "preflight_sufficiency": "needs_planning",
                },
            },
        ],
    )

    code, output = _run_sprint_status(tmp_path, "live-run")

    assert code == 0
    assert "[live]" in output
    assert "Issue #20" in output
    assert "DEV" in output
    assert "medium" in output
    assert "c1 i2" in output
    assert "Issue #21" in output
    assert "PROCEED / needs_planning" in output


def test_cmd_sprint_status_blocked_story_shows_dependency(tmp_path: Path) -> None:
    _make_state_file(
        tmp_path,
        "blocked-run",
        "dep-sprint",
        [
            {
                "slug": "issue-30",
                "path": "Issue #30",
                "status": "done",
                "phase": "DONE",
                "cost_usd": 0.2,
                "bundle_candidate": False,
                "blocked_by": [],
            },
            {
                "slug": "issue-31",
                "path": "Issue #31",
                "status": "blocked",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": ["issue-30"],
            },
        ],
    )

    # Write PID file to simulate live sprint
    runs_dir = tmp_path / ".forge" / "runs"
    (runs_dir / "blocked-run.pid").write_text("99998\ndep-sprint\n")

    code, output = _run_sprint_status(tmp_path, "blocked-run")

    assert code == 0
    assert "Issue #31" in output
    assert "issue-30" in output  # shows blocked_by slug in the dependency list


def test_cmd_sprint_status_not_found(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from theforge.cli.sprint_status import cmd_sprint_status

    forge_yaml = _make_forge_config(tmp_path)
    fake_config = MagicMock()
    fake_config.project_root = tmp_path

    class FakeArgs:
        run_id = "no-such-run"

    buf = io.StringIO()
    with (
        patch("theforge.cli.shared._find_config", return_value=forge_yaml),
        patch("theforge.config.load_config", return_value=fake_config),
        patch("sys.stderr", buf),
    ):
        code = cmd_sprint_status(FakeArgs())

    assert code == 1


# ── display_sprint_status: header and row field coverage ─────────────────────


def test_display_sprint_status_header_includes_cost_and_duration(tmp_path: Path) -> None:
    """Completed sprint header includes total cost and duration."""
    stories = [
        {"slug": "issue-1", "path": "Issue #1", "outcome": "DONE", "cost_usd": 0.50},
    ]
    _make_summary_file(tmp_path, "cost-sprint", "run-cost-1", stories)
    # Patch the sprint summary fixture to include aggregate fields.
    import yaml

    log_dir = tmp_path / ".forge" / "logs" / "cost-sprint"
    summary_path = log_dir / "sprint-summary.yaml"
    with open(summary_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["sprint"]["total_cost_usd"] = 1.23
    data["sprint"]["duration_seconds"] = 3720.0  # 62 minutes
    with open(summary_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)

    code, output = _run_sprint_status(tmp_path, "run-cost-1")

    assert code == 0
    assert "cost: $1.23" in output
    assert "duration: 62m" in output


def test_display_sprint_status_row_shows_status_and_detail(tmp_path: Path) -> None:
    """Sprint rows include status and detail fields."""
    stories = [
        {"slug": "issue-2", "path": "Issue #2", "outcome": "DONE", "cost_usd": 0.30},
        {"slug": "issue-3", "path": "Issue #3", "outcome": "ESCALATE", "cost_usd": 0.10},
    ]
    _make_summary_file(tmp_path, "row-sprint", "run-row-1", stories)

    code, output = _run_sprint_status(tmp_path, "run-row-1")

    assert code == 0
    # status column
    assert "done" in output
    assert "failed" in output
    # detail column for DONE → "APPROVE", for ESCALATE → "ESCALATE"
    assert "APPROVE" in output
    assert "ESCALATE" in output


def test_display_sprint_status_live_row_shows_phase_and_status(tmp_path: Path) -> None:
    """Live sprint rows include both status and phase columns."""
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "live-row.pid").write_text("99999\nrow-sprint\n")

    _make_state_file(
        tmp_path,
        "live-row",
        "row-sprint",
        [
            {
                "slug": "issue-10",
                "path": "Issue #10",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.07,
                "bundle_candidate": False,
                "blocked_by": [],
            },
            {
                "slug": "issue-11",
                "path": "Issue #11",
                "status": "blocked",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": ["issue-10"],
            },
        ],
    )

    code, output = _run_sprint_status(tmp_path, "live-row")

    assert code == 0
    assert "running" in output
    assert "DEV" in output
    assert "blocked" in output
    assert "issue-10" in output  # blocked_by appears in detail


def test_display_sprint_status_crashed_sprint(tmp_path: Path) -> None:
    """Crashed sprint (no PID, .state present, no summary) shows crash banner."""
    _make_state_file(
        tmp_path,
        "crashed-run",
        "crashed-sprint",
        [
            {
                "slug": "issue-40",
                "path": "Issue #40",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
            }
        ],
    )
    # No PID file and no sprint-summary — simulates an unexpected exit.

    code, output = _run_sprint_status(tmp_path, "crashed-run")

    assert code == 0
    assert "crashed" in output
    assert "unexpectedly" in output


def test_display_sprint_status_column_header_present(tmp_path: Path) -> None:
    """Sprint display includes a column header row."""
    stories = [
        {"slug": "issue-50", "path": "Issue #50", "outcome": "DONE", "cost_usd": 0.10},
    ]
    _make_summary_file(tmp_path, "col-sprint", "run-col-1", stories)

    code, output = _run_sprint_status(tmp_path, "run-col-1")

    assert code == 0
    assert "STATUS" in output
    assert "PHASE" in output
    assert "COMPLEXITY" in output
    assert "COST" in output
    assert "ELAPSED" in output
    assert "DETAIL" in output


# ── SprintStateWriter ────────────────────────────────────────────────────────


def test_state_writer_init_and_update(tmp_path: Path) -> None:
    from theforge.sprint.state_writer import SprintStateWriter

    writer = SprintStateWriter("test-run", tmp_path, "my-sprint")
    writer.init(
        [
            {
                "slug": "issue-1",
                "path": "Issue #1",
                "status": "waiting",
                "phase": "PREFLIGHT",
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": None,
                "detail": {},
            }
        ]
    )

    state_path = tmp_path / ".forge" / "runs" / "test-run.state"
    assert state_path.exists()

    with open(state_path) as f:
        data = yaml.safe_load(f)
    assert data["sprint_name"] == "my-sprint"
    assert data["stories"][0]["status"] == "waiting"
    assert data["stories"][0]["phase"] == "PREFLIGHT"

    writer.update(
        "issue-1",
        status="running",
        phase="DEV",
        complexity="medium",
        detail={"review_cycle": 0, "dev_iteration": 2},
    )
    with open(state_path) as f:
        data = yaml.safe_load(f)
    assert data["stories"][0]["status"] == "running"
    assert data["stories"][0]["phase"] == "DEV"
    assert data["stories"][0]["complexity"] == "medium"
    assert data["stories"][0]["detail"] == {"review_cycle": 0, "dev_iteration": 2}


def test_state_writer_remove(tmp_path: Path) -> None:
    from theforge.sprint.state_writer import SprintStateWriter

    writer = SprintStateWriter("rm-run", tmp_path, "sprint")
    _story = {
        "slug": "s",
        "path": "s",
        "status": "waiting",
        "phase": None,
        "cost_usd": 0.0,
        "bundle_candidate": False,
        "blocked_by": [],
    }
    writer.init([_story])

    state_path = tmp_path / ".forge" / "runs" / "rm-run.state"
    assert state_path.exists()

    writer.remove()
    assert not state_path.exists()

    # Second remove should not raise
    writer.remove()


def test_state_writer_update_unknown_slug_is_noop(tmp_path: Path) -> None:
    from theforge.sprint.state_writer import SprintStateWriter

    writer = SprintStateWriter("noop-run", tmp_path, "sprint")
    _story = {
        "slug": "s",
        "path": "s",
        "status": "waiting",
        "phase": None,
        "cost_usd": 0.0,
        "bundle_candidate": False,
        "blocked_by": [],
    }
    writer.init([_story])

    # Should not raise, just ignore
    writer.update("nonexistent", status="done")
