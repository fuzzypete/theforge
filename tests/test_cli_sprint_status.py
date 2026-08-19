"""Tests for forge sprint-status command and supporting modules."""

from __future__ import annotations

import datetime
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

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
                "complexity_score": 7,
                "detail": {"dev_iteration": 2, "dev_max_iterations": 3},
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
    assert e1.complexity_score == 7
    assert e1.stage == "iter=2/3"
    assert e1.detail == "running"

    e2 = entries[1]
    assert e2.slug == "issue-2"
    assert e2.status == "waiting"
    assert e2.bundle_candidate is True
    assert e2.stage == ""
    assert e2.detail == "waiting"


def test_live_dev_stage_shows_review_cycle_and_dev_iteration(tmp_path: Path) -> None:
    """DEV rows include the outer review-cycle position, not only the dev attempt.

    In live coordinator state, ``review_cycle`` is the story's position in the
    full dev->review feedback loop and ``dev_iteration`` is the current dev
    attempt inside that cycle.
    """
    from theforge.sprint.status_reader import read_live_status

    _make_state_file(
        tmp_path,
        "run-cycle",
        "cycle-sprint",
        [
            {
                "slug": "issue-130",
                "path": "Issue #130",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 15.82,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {
                    "review_cycle": 0,
                    "review_max_cycles": 3,
                    "dev_iteration": 0,
                    "dev_max_iterations": 3,
                },
            },
        ],
    )

    entry = read_live_status("run-cycle", tmp_path)[0]
    assert entry.stage == "cycle=0/3 iter=0/3"


def test_live_dev_stage_later_cycle_remains_global_context(tmp_path: Path) -> None:
    """A DEV retry after review feedback must not collapse back to bare iter=N."""
    from theforge.sprint.status_reader import read_live_status

    _make_state_file(
        tmp_path,
        "run-cycle-2",
        "cycle-sprint",
        [
            {
                "slug": "issue-131",
                "path": "Issue #131",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 2.00,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {
                    "review_cycle": 2,
                    "dev_iteration": 1,
                    "dev_max_iterations": 3,
                },
            },
        ],
    )

    entry = read_live_status("run-cycle-2", tmp_path)[0]
    assert entry.stage == "cycle=2 iter=1/3"


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
    assert entries[0].phase == "waiting"
    assert entries[0].stage == "dependency"
    assert entries[0].blocked_by == ["issue-98"]
    assert entries[0].detail == "depends on #98"


def test_read_live_status_merges_accumulated_prior_run_stories(tmp_path: Path) -> None:
    from theforge.sprint.audit import persist_accumulated_story_state
    from theforge.sprint.status_reader import read_live_status

    state_path = _make_state_file(
        tmp_path,
        "run-reexec",
        "issues-959,960",
        [
            {
                "slug": "issue-960",
                "path": "Issue #960",
                "status": "running",
                "phase": "PLAN",
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {"plan_attempt": 1, "plan_max_attempts": 3},
            }
        ],
    )
    data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    data["sprint_id"] = "sprint-abc"
    state_path.write_text(yaml.dump(data), encoding="utf-8")

    persist_accumulated_story_state(
        "sprint-abc",
        "issues-959,960",
        tmp_path,
        [
            {
                "canonical_ref": "issue:959",
                "slug": "issue-959",
                "path": "Issue #959",
                "outcome": "DONE",
                "cost_usd": 10.31,
                "depends_on": [],
            }
        ],
    )

    entries = read_live_status("run-reexec", tmp_path)
    assert entries is not None
    by_slug = {entry.slug: entry for entry in entries}
    assert set(by_slug) == {"issue-959", "issue-960"}
    assert by_slug["issue-959"].status == "done"
    assert by_slug["issue-959"].detail == "DONE"
    assert by_slug["issue-959"].cost_usd == pytest.approx(10.31)
    assert by_slug["issue-960"].status == "running"


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


def test_find_sprint_summary_matches_story_run_id_from_prior_generation(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import find_sprint_summary

    _make_summary_file(
        tmp_path,
        "issues-959,960",
        "run-new",
        [
            {
                "slug": "issue-959",
                "path": "Issue #959",
                "outcome": "DONE",
                "cost_usd": 10.31,
                "story_run_id": "run-old",
            },
            {
                "slug": "issue-960",
                "path": "Issue #960",
                "outcome": "DONE",
                "cost_usd": 1.0,
                "story_run_id": "run-new",
            },
        ],
    )

    result = find_sprint_summary("run-old", tmp_path)
    assert result is not None
    assert result.parent.name == "issues-959,960"


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
    assert by_slug["s1"].phase == "DONE"
    assert by_slug["s2"].status == "done"
    assert by_slug["s2"].phase == "ALREADY_DONE"
    assert by_slug["s3"].status == "skipped"
    assert by_slug["s3"].phase == "SKIPPED"
    assert by_slug["s4"].status == "failed"
    assert by_slug["s4"].phase == "ESCALATE"


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
    assert by_slug["issue-6"].phase == "waiting"
    assert by_slug["issue-6"].stage == "dependency"
    assert by_slug["issue-6"].blocked_by == ["issue-5"]
    assert by_slug["issue-6"].detail == "depends on #5"
    # Non-skipped stories should not show depends_on as blocked_by
    assert by_slug["issue-5"].blocked_by == []
    assert by_slug["issue-7"].blocked_by == []


def test_read_completed_status_uses_audit_for_terminal_stage_and_detail(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_completed_status

    stories = [
        {
            "slug": "issue-8",
            "path": "Issue #8",
            "outcome": "DONE",
            "cost_usd": 0.3,
            "verdict": "APPROVE",
            "iteration_usage": {
                "dev": {"used": 1, "max": 3},
                "review": {"used": 2, "max": 3},
            },
        }
    ]
    summary_path = _make_summary_file(tmp_path, "audit-sprint", "run-audit", stories)

    audit_dir = tmp_path / ".forge" / "logs" / "audit-sprint" / "issue-8"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_data = {
        "preflight": {"complexity": "medium", "complexity_score": 4},
        "reviews": [
            {
                "verdict": "APPROVE",
                "summary": "No findings remain.",
                "findings_by_severity": {"P1": 0, "P2": 0},
            }
        ],
    }
    with open(audit_dir / "audit.yaml", "w", encoding="utf-8") as f:
        yaml.dump(audit_data, f)

    entries = read_completed_status(summary_path)

    assert len(entries) == 1
    assert entries[0].stage == "2 cyc / 1 iter"
    assert entries[0].detail == "APPROVE 0P1 0P2 — No findings remain."
    assert entries[0].complexity == "medium"
    assert entries[0].complexity_score == 4


def test_read_completed_status_already_done_prefers_preflight_reason(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_completed_status

    stories = [
        {
            "slug": "issue-1186",
            "path": "Issue #1186",
            "outcome": "ALREADY_DONE",
            "cost_usd": 0.65,
        }
    ]
    summary_path = _make_summary_file(tmp_path, "already-done-sprint", "run-audit", stories)

    audit_dir = tmp_path / ".forge" / "logs" / "already-done-sprint" / "issue-1186"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_data = {
        "preflight": {
            "verdict": "cached",
            "original_verdict": "ALREADY_DONE",
            "reason": "Main already satisfies all acceptance criteria.",
        },
        "outcome": {
            "message": (
                "Gate exited PASS but branch has no commits ahead of base — "
                "treating empty worktree as missing-work failure"
            )
        },
    }
    with open(audit_dir / "audit.yaml", "w", encoding="utf-8") as f:
        yaml.dump(audit_data, f)

    entries = read_completed_status(summary_path)

    assert len(entries) == 1
    assert entries[0].status == "done"
    assert entries[0].phase == "ALREADY_DONE"
    assert (
        entries[0].detail == "Preflight verdict: Main already satisfies all acceptance criteria."
    )


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
                "detail": {"dev_iteration": 2, "dev_max_iterations": 3},
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
    assert "iter=2/3" in output
    assert "Issue #21" in output
    assert "PROCEED" in output
    assert "needs_planning" in output


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
    assert "#30" in output  # shows blocked_by dependency in #N notation


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


def test_display_sprint_status_marks_a_completed_run_that_passed_its_budget(
    tmp_path: Path,
) -> None:
    """The reported bug's display half: cost and budget printed as strangers.

    A $70.44 run against a $50 cap rendered as two independent header fields
    with nothing saying one had passed the other. The relationship is now
    stated (#2547).
    """
    stories = [{"slug": "issue-1", "path": "Issue #1", "outcome": "DONE", "cost_usd": 70.44}]
    _make_summary_file(tmp_path, "over-sprint", "run-over-1", stories)
    log_dir = tmp_path / ".forge" / "logs" / "over-sprint"
    summary_path = log_dir / "sprint-summary.yaml"
    with open(summary_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["sprint"]["budget_usd"] = 50.0
    data["sprint"]["total_cost_usd"] = 70.44
    data["sprint"]["budget_status"] = "over"
    data["sprint"]["budget_overrun_usd"] = 20.44
    with open(summary_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)

    code, output = _run_sprint_status(tmp_path, "run-over-1")

    assert code == 0
    assert "cost: $70.44" in output
    assert "budget: $50.00" in output
    assert "OVER BUDGET by $20.44" in output


def test_display_sprint_status_marks_an_overrun_a_summary_never_recorded(
    tmp_path: Path,
) -> None:
    """A run that predates the recorded verdict is still compared to its cap."""
    stories = [{"slug": "issue-1", "path": "Issue #1", "outcome": "DONE", "cost_usd": 12.0}]
    _make_summary_file(tmp_path, "old-sprint", "run-old-1", stories)
    log_dir = tmp_path / ".forge" / "logs" / "old-sprint"
    summary_path = log_dir / "sprint-summary.yaml"
    with open(summary_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["sprint"]["budget_usd"] = 10.0
    data["sprint"]["total_cost_usd"] = 12.0
    with open(summary_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)

    code, output = _run_sprint_status(tmp_path, "run-old-1")

    assert code == 0
    assert "OVER BUDGET by $2.00" in output


def test_display_sprint_status_leaves_a_run_inside_its_budget_unmarked(
    tmp_path: Path,
) -> None:
    """The marker has to mean something: no overrun, no warning."""
    stories = [{"slug": "issue-1", "path": "Issue #1", "outcome": "DONE", "cost_usd": 1.5}]
    _make_summary_file(tmp_path, "ok-sprint", "run-ok-1", stories)

    code, output = _run_sprint_status(tmp_path, "run-ok-1")

    assert code == 0
    assert "budget: $10.00" in output
    assert "OVER BUDGET" not in output


def test_display_sprint_status_marks_a_live_run_over_its_budget(tmp_path: Path) -> None:
    """A live run reports its standing while it is still running."""
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "live-over.pid").write_text("99999\nover-sprint\n")
    state_path = runs_dir / "live-over.state"
    with open(state_path, "w", encoding="utf-8") as f:
        yaml.dump(
            {
                "sprint_name": "over-sprint",
                "budget_usd": 10.0,
                "budget_status": "over",
                "budget_overrun_usd": 4.25,
                "budget_spend_usd": 14.25,
                "stories": [
                    {
                        "slug": "issue-9",
                        "path": "Issue #9",
                        "status": "running",
                        "phase": "REVIEW",
                        "cost_usd": 14.25,
                        "blocked_by": [],
                        "detail": {},
                    }
                ],
            },
            f,
        )

    code, output = _run_sprint_status(tmp_path, "live-over")

    assert code == 0
    assert "[live]" in output
    assert "OVER BUDGET by $4.25" in output
    assert "budget: $10.00  ⚠ exceeded" in output


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
    assert "DONE" in output
    assert "ESCALATE" in output
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
    assert "dependency" in output
    assert "#10" in output  # blocked_by appears in detail as #N notation


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


def test_display_sprint_status_terminal_marker_not_crashed(tmp_path: Path) -> None:
    """A run with a .ended marker (no PID) is not crashed even if .state lingers.

    Reproduces issue-1872: a sprint that wrote <run_id>.ended (e.g. an early
    bootstrap error caught by cli/sprint.py) but left a bootstrap .state file
    behind must report the recorded terminal outcome, not the crash banner.
    """
    _make_state_file(
        tmp_path,
        "ended-run",
        "ended-sprint",
        [
            {
                "slug": "issue-41",
                "path": "Issue #41",
                "status": "waiting",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
            }
        ],
    )
    # Terminal outcome marker written by the clean-exit path.
    (tmp_path / ".forge" / "runs" / "ended-run.ended").write_text("completed")

    code, output = _run_sprint_status(tmp_path, "ended-run")

    assert code == 0
    assert "crashed" not in output
    assert "unexpectedly" not in output
    assert "[completed]" in output


def test_display_sprint_status_stopped_marker_reports_outcome(tmp_path: Path) -> None:
    """A stopped run's recorded outcome is surfaced instead of crash wording."""
    _make_state_file(
        tmp_path,
        "stopped-run",
        "stopped-sprint",
        [
            {
                "slug": "issue-42",
                "path": "Issue #42",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
            }
        ],
    )
    (tmp_path / ".forge" / "runs" / "stopped-run.ended").write_text("stopped")

    code, output = _run_sprint_status(tmp_path, "stopped-run")

    assert code == 0
    assert "crashed" not in output
    assert "unexpectedly" not in output
    assert "[stopped]" in output


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
    assert "STAGE" in output
    assert "COMPLEXITY" in output
    assert "COST" in output
    assert "ELAPSED" in output
    assert "DETAIL" in output


def test_display_sprint_status_prefers_numeric_complexity_score(tmp_path: Path) -> None:
    """When a numeric complexity_score is available, the COMPLEXITY column shows it
    instead of the band string."""
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "score-run.pid").write_text("12345\nscore-sprint\n")

    _make_state_file(
        tmp_path,
        "score-run",
        "score-sprint",
        [
            {
                "slug": "issue-77",
                "path": "Issue #77",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": "medium",
                "complexity_score": 7,
                "detail": {"dev_iteration": 1, "dev_max_iterations": 3},
            },
        ],
    )

    code, output = _run_sprint_status(tmp_path, "score-run")
    assert code == 0
    assert " 7 " in output or output.rstrip().endswith(" 7")
    # Header still says COMPLEXITY but the row value should not be the band.
    row_lines = [line for line in output.splitlines() if "Issue #77" in line]
    assert row_lines, output
    assert "medium" not in row_lines[0]


def test_display_sprint_status_falls_back_to_band_when_score_absent(tmp_path: Path) -> None:
    """Without a complexity_score, the COMPLEXITY column still shows the band."""
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "band-run.pid").write_text("12345\nband-sprint\n")

    _make_state_file(
        tmp_path,
        "band-run",
        "band-sprint",
        [
            {
                "slug": "issue-78",
                "path": "Issue #78",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": "large",
                "detail": {"dev_iteration": 1, "dev_max_iterations": 3},
            },
        ],
    )

    code, output = _run_sprint_status(tmp_path, "band-run")
    assert code == 0
    row_lines = [line for line in output.splitlines() if "Issue #78" in line]
    assert row_lines, output
    assert "large" in row_lines[0]


def test_display_sprint_status_wraps_long_detail_without_truncation(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "wrap-run.pid").write_text("99997\nwrap-sprint\n")

    long_detail = (
        "addressing 1P1 (plan_flow.py:187) while coordinating a long descriptive message "
        "that should wrap onto a continuation line without losing any content"
    )
    _make_state_file(
        tmp_path,
        "wrap-run",
        "wrap-sprint",
        [
            {
                "slug": "issue-60",
                "path": "Issue #60",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.11,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {
                    "dev_iteration": 2,
                    "current_finding": long_detail,
                },
            }
        ],
    )

    code, output = _run_sprint_status(tmp_path, "wrap-run")

    assert code == 0
    assert "addressing 1P1" in output
    assert "(plan_flow.py:187)" in output
    assert "without losing any" in output
    assert "content" in output


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


def test_worker_phase_fn_forwards_coordinator_detail(tmp_path: Path) -> None:
    """_make_worker_phase_fn must forward complexity/detail from coordinator updates.

    Regression: a prior wrapper hardcoded DEV/VALIDATE detail and discarded
    fields supplied by preflight/plan/review/validate phases, so the live
    .state file never saw the real classification or gate decision.
    """
    import threading

    from theforge.sprint.runner import _make_worker_phase_fn
    from theforge.sprint.state_writer import SprintStateWriter

    writer = SprintStateWriter("wf-run", tmp_path, "sprint")
    writer.init(
        [
            {
                "slug": "s1",
                "path": "s1",
                "status": "running",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": None,
                "detail": {},
            }
        ]
    )

    fn = _make_worker_phase_fn(
        slug="s1",
        worker_phases={},
        phase_lock=threading.Lock(),
        outer_fn=None,
        plan_done=None,
        state_writer=writer,
    )

    state_path = tmp_path / ".forge" / "runs" / "wf-run.state"

    # PREFLIGHT: complexity + verdict/sufficiency detail propagated
    fn(
        {
            "phase": "PREFLIGHT",
            "iteration": 0,
            "cost_usd": 0.01,
            "complexity": "medium",
            "complexity_score": 6,
            "detail": {
                "preflight_verdict": "PROCEED",
                "preflight_sufficiency": "implementation_ready",
            },
        }
    )
    data = yaml.safe_load(state_path.read_text())
    story = data["stories"][0]
    assert story["phase"] == "PREFLIGHT"
    assert story["complexity"] == "medium"
    assert story["complexity_score"] == 6
    assert story["detail"]["preflight_verdict"] == "PROCEED"
    assert story["detail"]["preflight_sufficiency"] == "implementation_ready"

    # PLAN: plan_attempt/plan_max_attempts forwarded
    fn(
        {
            "phase": "PLAN",
            "iteration": 0,
            "cost_usd": 0.1,
            "complexity": "medium",
            "detail": {"plan_attempt": 1, "plan_max_attempts": 3},
        }
    )
    data = yaml.safe_load(state_path.read_text())
    assert data["stories"][0]["detail"] == {"plan_attempt": 1, "plan_max_attempts": 3}

    # DEV: review_cycle comes from coordinator detail, NOT iteration
    fn(
        {
            "phase": "DEV",
            "iteration": 2,
            "cost_usd": 0.5,
            "complexity": "medium",
            "detail": {"review_cycle": 0, "dev_iteration": 2},
        }
    )
    data = yaml.safe_load(state_path.read_text())
    assert data["stories"][0]["detail"] == {"review_cycle": 0, "dev_iteration": 2}

    # REVIEW: P1/P2 counts forwarded
    fn(
        {
            "phase": "REVIEW",
            "iteration": 1,
            "cost_usd": 0.7,
            "complexity": "medium",
            "detail": {"review_p1": 1, "review_p2": 2},
        }
    )
    data = yaml.safe_load(state_path.read_text())
    assert data["stories"][0]["detail"] == {"review_p1": 1, "review_p2": 2}

    # VALIDATE with gate_status: real decision preserved, not overwritten
    fn(
        {
            "phase": "VALIDATE",
            "iteration": 2,
            "cost_usd": 0.9,
            "complexity": "medium",
            "detail": {"gate_status": "PASS"},
        }
    )
    data = yaml.safe_load(state_path.read_text())
    assert data["stories"][0]["detail"] == {"gate_status": "PASS"}

    # VALIDATE without detail: falls back to 'running'
    fn({"phase": "VALIDATE", "iteration": 3, "cost_usd": 1.0})
    data = yaml.safe_load(state_path.read_text())
    assert data["stories"][0]["detail"] == {"gate_status": "running"}


def test_stage_and_detail_skipped_with_stale_approve_does_not_leak() -> None:
    """A SKIPPED story whose detail still claims final_outcome=DONE must not show APPROVE."""
    from theforge.sprint.status_reader import _stage_and_detail_from_live_story

    story = {
        "slug": "issue-1080",
        "path": "Issue #1080",
        "status": "skipped",
        "outcome": "skipped",
        "phase": None,
        "detail": {
            "final_outcome": "DONE",
            "review_verdict": "APPROVE — All ACs met from prior run",
            "review_p1": 0,
            "review_p2": 0,
        },
        "reason": "depends on issue-1070",
    }
    _stage, detail, _complexity = _stage_and_detail_from_live_story(story)
    assert "APPROVE" not in detail
    # Either the skip reason or the canonical SKIPPED outcome is acceptable; both
    # are correct, leak of APPROVE is not.
    assert detail in {"depends on issue-1070", "SKIPPED"}


def test_stage_and_detail_failed_with_stale_done_does_not_leak() -> None:
    """An ESCALATED (failed) story with stale DONE detail must not show APPROVE."""
    from theforge.sprint.status_reader import _stage_and_detail_from_live_story

    story = {
        "slug": "issue-1070",
        "path": "Issue #1070",
        "status": "failed",
        "outcome": "escalated",
        "phase": "ESCALATE",
        "detail": {
            "final_outcome": "DONE",
            "review_verdict": "APPROVE — Adaptive timeout coverage gap closed",
        },
    }
    _stage, detail, _complexity = _stage_and_detail_from_live_story(story)
    assert "APPROVE" not in detail
    # The display should reflect the current run's escalation, not a stale APPROVE.
    assert detail in {"ESCALATED", "ESCALATE"}


def test_stage_and_detail_preserved_names_review_command() -> None:
    """A preserved issue story should name the runnable review command."""
    from theforge.sprint.preserved_resume import preserved_escalated_detail_for_story
    from theforge.sprint.status_reader import _stage_and_detail_from_live_story

    story = {
        "slug": "issue-2475",
        "path": "Issue #2475",
        "status": "preserved",
        "outcome": "preserved",
        "phase": None,
        "detail": {"final_outcome": "PRESERVED"},
        "reason": "preserved-escalated",
    }
    _stage, detail, _complexity = _stage_and_detail_from_live_story(story)
    assert detail == preserved_escalated_detail_for_story(story)


def test_completed_failed_story_with_stale_verdict_does_not_show_approve() -> None:
    """Completed FAILED row with stale verdict=APPROVE must not render APPROVE."""
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    story = {
        "slug": "issue-1070",
        "path": "Issue #1070",
        "outcome": "FAILED",
        "verdict": "APPROVE",
    }
    _stage, detail, _complexity = _stage_and_detail_from_completed_story(story, None)
    assert "APPROVE" not in detail
    assert detail == "FAILED"


def test_completed_escalated_story_with_stale_verdict_does_not_show_approve() -> None:
    """Completed ESCALATED row with stale verdict=APPROVE must not render APPROVE."""
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    story = {
        "slug": "issue-1070",
        "path": "Issue #1070",
        "outcome": "ESCALATED",
        "verdict": "APPROVE",
    }
    _stage, detail, _complexity = _stage_and_detail_from_completed_story(story, None)
    assert "APPROVE" not in detail
    assert detail == "ESCALATED"


def test_completed_skipped_story_with_stale_verdict_does_not_show_approve() -> None:
    """Completed SKIPPED row with stale verdict=APPROVE must not render APPROVE."""
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    story = {
        "slug": "issue-1080",
        "path": "Issue #1080",
        "outcome": "SKIPPED",
        "verdict": "APPROVE",
    }
    _stage, detail, _complexity = _stage_and_detail_from_completed_story(story, None)
    assert "APPROVE" not in detail
    assert detail == "SKIPPED"


def test_completed_preserved_story_names_review_command() -> None:
    """Completed PRESERVED rows should keep the same review guidance."""
    from theforge.sprint.preserved_resume import preserved_escalated_detail_for_story
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    story = {
        "slug": "issue-2475",
        "path": "Issue #2475",
        "outcome": "PRESERVED",
        "drop_reason": "preserved-escalated",
    }
    _stage, detail, _complexity = _stage_and_detail_from_completed_story(story, None)
    assert detail == preserved_escalated_detail_for_story(story)


def test_completed_preserved_story_from_current_entry_error_shape_names_review_command(
    tmp_path: Path,
) -> None:
    """Completed status must render preserved current-entry rows written with only error."""
    from theforge.sprint.audit import _write_sprint_summary
    from theforge.sprint.manifest import SprintResult
    from theforge.sprint.preserved_resume import preserved_escalated_detail_for_story
    from theforge.sprint.status_reader import read_completed_status

    manifest = MagicMock()
    manifest.name = "test-sprint"
    manifest.budget_usd = 1.0
    manifest.max_parallel = 1

    sprint_result = SprintResult(
        name="test-sprint",
        specs_total=1,
        specs_succeeded=0,
        specs_failed=1,
        specs_skipped=0,
        total_cost_usd=0.0,
        budget_usd=1.0,
        results=[],
    )

    now = datetime.datetime(2026, 8, 18, tzinfo=datetime.timezone.utc)
    sprint_log_dir = tmp_path / ".forge" / "logs" / "test-sprint"
    sprint_log_dir.mkdir(parents=True, exist_ok=True)
    _write_sprint_summary(
        manifest=manifest,
        result=sprint_result,
        canonical_refs=["issue:2475"],
        started_at=now,
        finished_at=now,
        duration=1.0,
        sprint_log_dir=sprint_log_dir,
        slug_map={"issue:2475": "issue-2475"},
        current_story_entries_by_ref={
            "issue:2475": {
                "slug": "issue-2475",
                "path": "Issue #2475",
                "outcome": "PRESERVED",
                "error": "preserved-escalated",
                "error_type": "dropped",
                "cost_usd": 0.0,
                "merge": False,
                "batch": 0,
                "depends_on": [],
            }
        },
    )

    entries = read_completed_status(sprint_log_dir / "sprint-summary.yaml")
    assert len(entries) == 1
    assert entries[0].detail == preserved_escalated_detail_for_story(
        {"slug": "issue-2475", "path": "Issue #2475"}
    )


def test_completed_done_story_verdict_is_preserved() -> None:
    """Completed DONE row with verdict=APPROVE should still render APPROVE."""
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    story = {
        "slug": "issue-1000",
        "path": "Issue #1000",
        "outcome": "DONE",
        "verdict": "APPROVE",
    }
    _stage, detail, _complexity = _stage_and_detail_from_completed_story(story, None)
    assert detail == "APPROVE"


def test_completed_already_done_story_prefers_preflight_reason_over_outcome_message() -> None:
    """ALREADY_DONE rows should render the preflight reason, not a later gate message."""
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    story = {
        "slug": "issue-1186",
        "path": "Issue #1186",
        "outcome": "ALREADY_DONE",
    }
    _stage, detail, _complexity = _stage_and_detail_from_completed_story(
        story,
        {
            "preflight": {
                "reason": "Main already satisfies all acceptance criteria.",
            },
            "outcome": {
                "message": (
                    "Gate exited PASS but branch has no commits ahead of base — "
                    "treating empty worktree as missing-work failure"
                )
            },
        },
    )

    assert detail == "Preflight verdict: Main already satisfies all acceptance criteria."


# ── Title cache + #N enrichment (issue #1486) ────────────────────────────────


def test_issue_number_from_slug_variants() -> None:
    from theforge.cli.sprint_status import _issue_number_from_slug

    assert _issue_number_from_slug("issue-1424") == 1424
    assert _issue_number_from_slug("Issue #1424") == 1424
    assert _issue_number_from_slug("#1462") == 1462
    assert _issue_number_from_slug("no-number-here") is None
    assert _issue_number_from_slug("") is None


def test_format_story_cell_uses_cached_title() -> None:
    from theforge.cli.sprint_status import _format_story_cell

    cache = {1424: "Write changelog gen"}
    assert _format_story_cell("Issue #1424", cache) == "#1424 Write changelog gen"
    # No cached title -> original path unchanged.
    assert _format_story_cell("Issue #1425", cache) == "Issue #1425"
    # Non-issue path unchanged.
    assert _format_story_cell("some-file.md", cache) == "some-file.md"


def test_enrich_detail_appends_titles_to_hash_tokens() -> None:
    from theforge.cli.sprint_status import _enrich_detail

    cache = {1462: "Fix audit rollup"}
    assert _enrich_detail("depends on #1462", cache) == "depends on #1462 Fix audit rollup"
    # Details with no #N tokens are unchanged.
    assert _enrich_detail("parallel cap", cache) == "parallel cap"
    # Unknown number left bare.
    assert _enrich_detail("depends on #999", cache) == "depends on #999"


def test_ensure_titles_fetches_only_missing_numbers(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import dataclass, field

    import theforge.sprint.query as query
    from theforge.cli.sprint_status import _ensure_titles

    @dataclass
    class _Entry:
        path: str
        blocked_by: list = field(default_factory=list)

    calls: list[list[int]] = []

    def _fake_fetch(numbers, project_root=None):
        calls.append(list(numbers))
        return [{"number": n, "title": f"Title {n}"} for n in numbers]

    monkeypatch.setattr(query, "fetch_issues_by_numbers", _fake_fetch)

    entries = [_Entry("Issue #10", ["issue-20"]), _Entry("Issue #10")]
    cache: dict = {}
    _ensure_titles(entries, tmp_path, cache)

    assert cache == {10: "Title 10", 20: "Title 20"}
    # Fetched one number at a time so a batch failure can't discard partials.
    assert calls == [[10], [20]]

    # Second call with everything cached does not fetch again.
    _ensure_titles(entries, tmp_path, cache)
    assert calls == [[10], [20]]


def test_ensure_titles_caches_partials_when_one_number_unresolvable(
    monkeypatch, tmp_path: Path
) -> None:
    """A single not-found number must not discard the titles that did resolve,
    and the not-found number must not be re-fetched on the next frame."""
    from dataclasses import dataclass, field

    import theforge.sprint.query as query
    from theforge.cli.sprint_status import _ensure_titles

    @dataclass
    class _Entry:
        path: str
        blocked_by: list = field(default_factory=list)

    calls: list[list[int]] = []

    def _fake_fetch(numbers, project_root=None):
        calls.append(list(numbers))
        # Number 20 does not exist in the repo — mirror the real primitive,
        # which raises for the whole batch when a requested number is missing.
        if 20 in numbers:
            raise RuntimeError("Issue number(s) not found in this repository: 20")
        return [{"number": n, "title": f"Title {n}"} for n in numbers]

    monkeypatch.setattr(query, "fetch_issues_by_numbers", _fake_fetch)

    entries = [_Entry("Issue #10", ["issue-20"])]
    cache: dict = {}
    _ensure_titles(entries, tmp_path, cache)

    # 10 resolved and cached; 20 recorded as a negative sentinel.
    assert cache == {10: "Title 10", 20: None}
    assert calls == [[10], [20]]

    # Next frame: nothing re-fetched — both numbers are already in the cache.
    _ensure_titles(entries, tmp_path, cache)
    assert calls == [[10], [20]]


def test_format_story_cell_ignores_negative_sentinel() -> None:
    from theforge.cli.sprint_status import _format_story_cell

    # None sentinel (issue confirmed absent) must not render "#N None".
    assert _format_story_cell("Issue #20", {20: None}) == "Issue #20"


def test_ensure_titles_leaves_transient_failure_uncached(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import dataclass, field

    import theforge.sprint.query as query
    from theforge.cli.sprint_status import _ensure_titles

    @dataclass
    class _Entry:
        path: str
        blocked_by: list = field(default_factory=list)

    def _boom(numbers, project_root=None):
        raise RuntimeError("gh issue view 10 failed: network down")

    monkeypatch.setattr(query, "fetch_issues_by_numbers", _boom)

    cache: dict = {}
    _ensure_titles([_Entry("Issue #10")], tmp_path, cache)
    # Transient failure leaves the number uncached so a later frame can retry.
    assert cache == {}


def test_live_pid_with_terminal_sprint_phase_is_not_reported_as_live(tmp_path: Path) -> None:
    """A terminal persisted phase outranks a lingering PID file (#2013).

    The runner writes the terminal sprint_phase before its own wrap-up, so the
    process can still exist while the sprint is over. Reporting "[live] phase:
    failed" next to nothing but failed stories is the contradiction operators hit.
    """
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "wrapup-run.pid").write_text("99999\nwrapup-sprint\n")

    state_path = _make_state_file(
        tmp_path,
        "wrapup-run",
        "wrapup-sprint",
        [
            {
                "slug": "issue-50",
                "path": "Issue #50",
                "status": "failed",
                "outcome": "failed",
                "phase": "ESCALATE",
                "cost_usd": 0.5,
                "detail": {"gate_status": "incomplete"},
            }
        ],
    )
    data = yaml.safe_load(state_path.read_text())
    data["sprint_phase"] = "failed"
    state_path.write_text(yaml.dump(data))

    code, output = _run_sprint_status(tmp_path, "wrapup-run")

    assert code == 0
    assert "[live]" not in output
    assert "[failed]" in output
