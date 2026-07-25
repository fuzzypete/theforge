"""Regression: forge status --watch must render meaningfully during the
shape-gate / intake-remediation / preflight phases that precede the first
SprintStateWriter init.

Before the bootstrap state file existed, the watcher attached to a live
sprint and rendered only its overlay headers — operators stared at an empty
table for several minutes while real work was running. These tests exercise
the actual data sources (state file on disk, display_sprint_status,
status_watch.render_frame) — no mocks at the rendering boundary.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml


def _make_forge_yaml(tmp_path: Path) -> Path:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    return forge_yaml


def _run_sprint_status_cli(tmp_path: Path, run_id: str) -> tuple[int, str]:
    from theforge.cli.sprint_status import cmd_sprint_status

    forge_yaml = _make_forge_yaml(tmp_path)
    fake_config = MagicMock()
    fake_config.project_root = tmp_path

    class _Args:
        pass

    args = _Args()
    args.run_id = run_id  # type: ignore[attr-defined]

    buf = io.StringIO()
    with (
        patch("theforge.cli.shared._find_config", return_value=forge_yaml),
        patch("theforge.config.load_config", return_value=fake_config),
        patch("sys.stdout", buf),
    ):
        code = cmd_sprint_status(args)
    return code, buf.getvalue()


def test_write_bootstrap_state_round_trip(tmp_path: Path) -> None:
    """Bootstrap state file is YAML-readable and contains issue rows + meta."""
    from theforge.sprint.state_writer import write_bootstrap_state

    write_bootstrap_state(
        "run-bootstrap-1",
        tmp_path,
        sprint_name="issues-1462",
        sprint_phase="shape-gate",
        base_branch="main",
        budget_usd=50.0,
        max_parallel=3,
        issues=[{"number": 1462, "title": "watch render"}],
    )

    state_path = tmp_path / ".forge" / "runs" / "run-bootstrap-1.state"
    assert state_path.exists()
    data = yaml.safe_load(state_path.read_text(encoding="utf-8"))

    assert data["sprint_name"] == "issues-1462"
    assert data["sprint_phase"] == "shape-gate"
    assert data["base_branch"] == "main"
    assert data["budget_usd"] == 50.0
    assert data["max_parallel"] == 3

    stories = data["stories"]
    assert len(stories) == 1
    assert stories[0]["slug"] == "issue-1462"
    assert stories[0]["status"] == "waiting"
    assert stories[0]["canonical_ref"] == "issue:1462"


def test_update_state_phase_preserves_other_fields(tmp_path: Path) -> None:
    """Phase updates must not erase base_branch / budget / parallel / stories."""
    from theforge.sprint.state_writer import update_state_phase, write_bootstrap_state

    write_bootstrap_state(
        "run-phase-1",
        tmp_path,
        sprint_name="issues-1,2",
        sprint_phase="shape-gate",
        base_branch="develop",
        budget_usd=25.0,
        max_parallel=2,
        issues=[{"number": 1}, {"number": 2}],
    )
    update_state_phase("run-phase-1", tmp_path, "preflight")

    state_path = tmp_path / ".forge" / "runs" / "run-phase-1.state"
    data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert data["sprint_phase"] == "preflight"
    assert data["base_branch"] == "develop"
    assert data["budget_usd"] == 25.0
    assert data["max_parallel"] == 2
    assert len(data["stories"]) == 2


def test_update_state_phase_noop_when_file_missing(tmp_path: Path) -> None:
    """update_state_phase must silently no-op for headless / pre-bootstrap runs."""
    from theforge.sprint.state_writer import update_state_phase

    # Should not raise.
    update_state_phase("never-written", tmp_path, "preflight")
    assert not (tmp_path / ".forge" / "runs" / "never-written.state").exists()


def test_sprint_state_writer_inherits_bootstrap_metadata(tmp_path: Path) -> None:
    """SprintStateWriter created over a bootstrap file keeps the operator context.

    Without inheritance the runner's first init() call would erase
    base_branch / budget / parallel that cli/sprint.py wrote at daemonize time
    — operators would see the watch header lose context the moment preflight
    finished.
    """
    from theforge.sprint.state_writer import SprintStateWriter, write_bootstrap_state

    write_bootstrap_state(
        "run-inherit",
        tmp_path,
        sprint_name="my-sprint",
        sprint_phase="preflight",
        base_branch="main",
        budget_usd=42.0,
        max_parallel=4,
        issues=[{"number": 7}],
    )
    writer = SprintStateWriter(
        "run-inherit",
        tmp_path,
        "my-sprint",
    )
    writer.init([])

    state_path = tmp_path / ".forge" / "runs" / "run-inherit.state"
    data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert data["sprint_phase"] == "preflight"
    assert data["base_branch"] == "main"
    assert data["budget_usd"] == 42.0
    assert data["max_parallel"] == 4


def test_bootstrap_reentry_preserves_accumulated_live_state(tmp_path: Path) -> None:
    """A later bootstrap call must not blank an already-live sprint state file."""
    from theforge.sprint.state_writer import SprintStateWriter, write_bootstrap_state

    run_id = "run-reseed-guard"
    write_bootstrap_state(
        run_id,
        tmp_path,
        sprint_name="my-sprint",
        sprint_phase="starting",
        base_branch="main",
        budget_usd=42.0,
        max_parallel=2,
        issues=[{"number": 7}, {"number": 8}],
    )

    writer = SprintStateWriter(
        run_id,
        tmp_path,
        "my-sprint",
        sprint_id="sprint-123",
        sprint_phase="executing",
        base_branch="main",
        budget_usd=42.0,
        max_parallel=2,
    )
    writer.init(
        [
            {"slug": "issue-7", "path": "Issue #7", "status": "running"},
            {"slug": "issue-8", "path": "Issue #8", "status": "waiting"},
        ]
    )
    writer.update(
        "issue-7",
        status="done",
        cost_usd=32.45,
        detail={"merged_pr": 1795},
    )

    write_bootstrap_state(
        run_id,
        tmp_path,
        sprint_name="my-sprint",
        sprint_phase="starting",
        base_branch="main",
        budget_usd=42.0,
        max_parallel=2,
        issues=[{"number": 7}, {"number": 8}],
    )

    state_path = tmp_path / ".forge" / "runs" / f"{run_id}.state"
    data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    stories_by_slug = {story["slug"]: story for story in data["stories"]}

    assert data["sprint_id"] == "sprint-123"
    assert data["sprint_phase"] == "executing"
    assert stories_by_slug["issue-7"]["status"] == "done"
    assert stories_by_slug["issue-7"]["cost_usd"] == 32.45
    assert stories_by_slug["issue-7"]["detail"]["merged_pr"] == 1795
    assert stories_by_slug["issue-8"]["status"] == "waiting"


def test_display_sprint_status_renders_bootstrap_window(tmp_path: Path) -> None:
    """display_sprint_status renders issue rows + phase + base_branch + budget
    + parallel from a bootstrap state file alone (no preflight, no per-story
    detail). Operators see this output immediately on watch attach.
    """
    from theforge.sprint.state_writer import write_bootstrap_state

    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # PID file marks the run live.
    (runs_dir / "run-bw.pid").write_text("12345\nmy-sprint\n", encoding="utf-8")

    write_bootstrap_state(
        "run-bw",
        tmp_path,
        sprint_name="issues-1461,1462",
        sprint_phase="preflight",
        base_branch="main",
        budget_usd=50.0,
        max_parallel=3,
        issues=[
            {"number": 1461, "title": "first"},
            {"number": 1462, "title": "second"},
        ],
    )

    code, output = _run_sprint_status_cli(tmp_path, "run-bw")

    assert code == 0
    # Header carries phase + base/budget/parallel context.
    assert "phase: preflight" in output
    assert "base: main" in output
    assert "budget: $50.00" in output
    assert "parallel: 3" in output
    # Per-issue rows appear with placeholder waiting status.
    assert "Issue #1461" in output
    assert "Issue #1462" in output
    assert "waiting" in output


def test_bootstrap_state_includes_shape_gate_skips(tmp_path: Path) -> None:
    """Shape-gate skips already emitted before daemonize must surface as
    terminal `skipped` rows in the bootstrap state, with the gate reason
    populated so operators can see why an issue was rejected without
    waiting for SprintStateWriter.init() to run after preflight.
    """
    from theforge.sprint.state_writer import write_bootstrap_state

    skipped = [
        {
            "issue_number": 99,
            "title": "stale contract",
            "reason_codes": ["reopened_stale_contract", "missing_acceptance"],
            "source": "shape_check",
        }
    ]

    write_bootstrap_state(
        "run-skip-1",
        tmp_path,
        sprint_name="issues-1,99",
        sprint_phase="starting",
        base_branch="main",
        budget_usd=10.0,
        max_parallel=1,
        issues=[{"number": 1, "title": "runnable"}],
        skipped_issues=skipped,
    )

    data = yaml.safe_load(
        (tmp_path / ".forge" / "runs" / "run-skip-1.state").read_text(encoding="utf-8")
    )
    stories_by_slug = {s["slug"]: s for s in data["stories"]}

    # Runnable + skipped both present.
    assert "issue-1" in stories_by_slug
    assert "issue-99" in stories_by_slug

    sk = stories_by_slug["issue-99"]
    assert sk["status"] == "skipped"
    assert sk["reason"] == "reopened_stale_contract, missing_acceptance"
    assert sk["detail"]["shape_gate_codes"] == [
        "reopened_stale_contract",
        "missing_acceptance",
    ]
    assert sk["detail"]["shape_gate_source"] == "shape_check"
    assert sk["canonical_ref"] == "issue:99"


def test_display_sprint_status_shows_shape_gate_skip_in_bootstrap(tmp_path: Path) -> None:
    """display_sprint_status surfaces shape-gate-skipped rows during the
    pre-preflight bootstrap window — not just runnable issues."""
    from theforge.sprint.state_writer import write_bootstrap_state

    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "run-skip-disp.pid").write_text("12345\nmy-sprint\n", encoding="utf-8")

    write_bootstrap_state(
        "run-skip-disp",
        tmp_path,
        sprint_name="issues-1,99",
        sprint_phase="starting",
        base_branch="main",
        budget_usd=10.0,
        max_parallel=1,
        issues=[{"number": 1, "title": "runnable"}],
        skipped_issues=[
            {
                "issue_number": 99,
                "reason_codes": ["reopened_stale_contract"],
                "source": "shape_check",
            }
        ],
    )

    code, output = _run_sprint_status_cli(tmp_path, "run-skip-disp")
    assert code == 0
    assert "Issue #99" in output
    assert "skipped" in output
    assert "reopened_stale_contract" in output


def test_bootstrap_skipped_issues_dedup_against_runnable(tmp_path: Path) -> None:
    """A slug present in both `issues` and `skipped_issues` must not appear
    twice — runnable wins, since the operator sees the issue as still alive."""
    from theforge.sprint.state_writer import write_bootstrap_state

    write_bootstrap_state(
        "run-dedup",
        tmp_path,
        sprint_name="issues-7",
        sprint_phase="starting",
        issues=[{"number": 7}],
        skipped_issues=[{"issue_number": 7, "reason_codes": ["x"]}],
    )
    data = yaml.safe_load(
        (tmp_path / ".forge" / "runs" / "run-dedup.state").read_text(encoding="utf-8")
    )
    slugs = [s["slug"] for s in data["stories"]]
    assert slugs == ["issue-7"]
    assert data["stories"][0]["status"] == "waiting"


def test_status_watch_renders_bootstrap_first_frame(tmp_path: Path) -> None:
    """The watcher's render_frame succeeds (snapshot_ok=True) and surfaces
    issue rows + sprint phase the moment a bootstrap state file exists.

    This is the symptom-resolution test: before the fix, render_frame on
    the first poll after attach would either exit (no state file) or render
    only headers (state file with no rows).
    """
    from theforge.cli.status_watch import render_frame
    from theforge.sprint.state_writer import write_bootstrap_state

    forge_yaml = _make_forge_yaml(tmp_path)
    fake_config = MagicMock()
    fake_config.project_root = tmp_path

    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "run-watch.pid").write_text("99999\nmy-sprint\n", encoding="utf-8")
    write_bootstrap_state(
        "run-watch",
        tmp_path,
        sprint_name="issues-1462",
        sprint_phase="intake-remediation",
        base_branch="main",
        budget_usd=10.0,
        max_parallel=1,
        issues=[{"number": 1462}],
    )

    state: dict = {"costs": {}, "interval": 2.0}
    with (
        patch("theforge.cli.shared._find_config", return_value=forge_yaml),
        patch("theforge.config.load_config", return_value=fake_config),
    ):
        text, snapshot_ok, captured_err = render_frame(
            "run-watch",
            tmp_path,
            state,
            frame_idx=0,
            color=False,
        )

    assert snapshot_ok is True, captured_err
    # Watcher overlay drew header + STORY row table.
    assert "Live  run=run-watch" in text
    # Underlying display_sprint_status surfaced bootstrap context.
    assert "phase: intake-remediation" in text
    assert "base: main" in text
    assert "Issue #1462" in text
