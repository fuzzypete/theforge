"""Sprint configuration pinning and baseline-gate diagnostics (issue #1980).

Two failures of the same kind: a config problem reported as something else. The
baseline gate discarded the output that named the missing key, and the sprint
re-resolved forge.yaml per invocation so a mid-sprint change silently split the
sprint across two configurations.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from coord_test_helpers import _make_config as _make_coord_config
from coord_test_helpers import _make_task

# Reuse the baseline-gate repo harness rather than restating it.
from test_sprint_baseline_gate import _init_repo  # noqa: E402

from theforge.coordinator import config_snapshot as cs
from theforge.coordinator.run_setup import _setup_resume_entry
from theforge.coordinator.state import Phase
from theforge.sprint.runner import (
    BASELINE_DIAGNOSTIC_MAX_LINES,
    _baseline_failure_diagnostic,
    _run_baseline_gate,
)


@pytest.fixture(autouse=True)
def _clear_active_pin():
    cs.deactivate()
    yield
    cs.deactivate()


# --------------------------------------------------------------------------
# 1. The broken-baseline message names what must change
# --------------------------------------------------------------------------

GUARD_STDERR = (
    "forge.yaml story-mutation guard failed: workspace.setup_command uses "
    "{forge_python} but workspace.python_interpreter is not set."
)


def test_broken_baseline_message_carries_the_gate_output(tmp_path: Path) -> None:
    """The operator is handed the missing key, not only the merge-base sha."""
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=f'python -c "import sys; print({GUARD_STDERR!r}); sys.exit(1)"',
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    message = str(baseline["message"])
    # The sha still names where the gate ran...
    assert base_commit in message
    # ...and the message now also names the file and the key that must change.
    assert "workspace.python_interpreter" in message
    assert "forge.yaml" in message


def test_broken_baseline_message_is_bounded(tmp_path: Path) -> None:
    """A chatty gate cannot turn the raised error into an unbounded dump."""
    config, resolved, _base = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=(
                'python -c "import sys;'
                "[print('noise line %d' % i) for i in range(500)];"
                'sys.exit(1)"'
            ),
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    message = str(baseline["message"])
    assert "truncated" in message
    body = message.split("Gate output", 1)[1]
    assert len(body.splitlines()) <= BASELINE_DIAGNOSTIC_MAX_LINES + 1
    # The full tail is still available on the record for anyone who wants it.
    assert len(str(baseline["output_tail"])) > len(body)


def test_baseline_diagnostic_empty_when_gate_captured_nothing() -> None:
    assert _baseline_failure_diagnostic("") == ""
    assert _baseline_failure_diagnostic(None) == ""
    assert _baseline_failure_diagnostic(123) == ""


# --------------------------------------------------------------------------
# 2. The sprint's config identity
# --------------------------------------------------------------------------


def _write_root_config(root: Path, text: str) -> None:
    (root / "forge.yaml").write_text(text, encoding="utf-8")


def test_capture_pins_project_config(tmp_path: Path) -> None:
    _write_root_config(tmp_path, "project: one\n")

    snap = cs.capture_or_load(tmp_path, "sprint-a")

    assert snap.present is True
    assert snap.reused is False
    assert snap.digest == cs.digest_text("project: one\n")
    assert snap.pinned_path is not None
    assert snap.pinned_path.read_text(encoding="utf-8") == "project: one\n"
    record = yaml.safe_load(cs.snapshot_record_path(tmp_path, "sprint-a").read_text())
    assert record["digest"] == snap.digest
    assert record["drift_events"] == []


def test_reentry_reuses_the_pin_and_reports_drift(tmp_path: Path) -> None:
    """A re-exec / --resume must not recapture the config it exists to pin."""
    _write_root_config(tmp_path, "project: one\n")
    first = cs.capture_or_load(tmp_path, "sprint-a")

    # A story lands a config-contract change (or the operator edits the file).
    _write_root_config(tmp_path, "project: two\n")
    second = cs.capture_or_load(tmp_path, "sprint-a")

    assert second.reused is True
    assert second.digest == first.digest
    assert second.pinned_path.read_text(encoding="utf-8") == "project: one\n"

    event = cs.check_drift(second, story="issue-1945")
    assert event is not None
    assert event["pinned_digest"] == first.digest
    assert event["project_root_digest"] == cs.digest_text("project: two\n")
    assert event["story"] == "issue-1945"
    assert event["pinned_config_in_effect"] is True
    assert "forge.yaml" in cs.describe_drift(event)

    record = yaml.safe_load(cs.snapshot_record_path(tmp_path, "sprint-a").read_text())
    assert len(record["drift_events"]) == 1
    # Re-checking the same state for the same story does not duplicate the event.
    assert cs.check_drift(second, story="issue-1945") is None


def test_no_drift_reported_while_the_root_matches(tmp_path: Path) -> None:
    _write_root_config(tmp_path, "project: one\n")
    snap = cs.capture_or_load(tmp_path, "sprint-a")
    assert cs.check_drift(snap, story="issue-1") is None


def test_missing_project_config_is_not_an_error(tmp_path: Path) -> None:
    snap = cs.capture_or_load(tmp_path, "sprint-a")

    assert snap.present is False
    assert snap.digest is None
    assert snap.pinned_path is None
    assert cs.check_drift(snap, story="issue-1") is None
    cs.activate(snap)
    assert cs.pinned_forge_yaml() is None


def test_activate_exposes_the_pin_and_survives_a_vanished_file(tmp_path: Path) -> None:
    _write_root_config(tmp_path, "project: one\n")
    snap = cs.capture_or_load(tmp_path, "sprint-a")

    cs.activate(snap)
    assert cs.pinned_forge_yaml() == snap.pinned_path
    assert cs.active_snapshot() is snap

    snap.pinned_path.unlink()
    assert cs.pinned_forge_yaml() is None  # falls back to the project root

    cs.deactivate()
    assert cs.pinned_forge_yaml() is None
    assert cs.active_snapshot() is None


def test_audit_record_is_loadable_by_sprint_id(tmp_path: Path) -> None:
    _write_root_config(tmp_path, "project: one\n")
    snap = cs.capture_or_load(tmp_path, "sprint-a")
    _write_root_config(tmp_path, "project: two\n")
    cs.check_drift(snap, story="issue-2")

    record = cs.load_audit_record(tmp_path, "sprint-a")

    assert record is not None
    assert record["digest"] == snap.digest
    assert [e["story"] for e in record["drift_events"]] == ["issue-2"]
    assert cs.load_audit_record(tmp_path, None) is None


# --------------------------------------------------------------------------
# 3. Seam: story worktree preparation uses the pin, not the live project root
# --------------------------------------------------------------------------


def _prepare_story(config, task, workspace: Path):
    with patch(
        "theforge.coordinator.run_setup._cu._run_shell", return_value=(True, "forge/test-task")
    ):
        return _setup_resume_entry(
            config,
            task,
            workspace,
            initial_phase=Phase.DEV,
            notify=False,
            run_id="test-run-id",
        )


def test_two_stories_in_one_sprint_get_the_same_config(tmp_path: Path) -> None:
    """The whole point: a mid-sprint edit does not partition the sprint."""
    _write_root_config(tmp_path, "project: sprint-entry\n")
    snap = cs.capture_or_load(tmp_path, "sprint-a")
    cs.activate(snap)

    config = _make_coord_config(tmp_path)
    task = _make_task(tmp_path)

    story_a = tmp_path / "story-a"
    story_a.mkdir()
    _prepare_story(config, task, story_a)

    # A story lands a config change / the operator edits forge.yaml mid-sprint.
    _write_root_config(tmp_path, "project: changed-mid-sprint\n")
    drift = cs.check_drift(snap, story="story-b")

    story_b = tmp_path / "story-b"
    story_b.mkdir()
    _prepare_story(config, task, story_b)

    assert (story_a / "forge.yaml").read_text(encoding="utf-8") == "project: sprint-entry\n"
    assert (story_b / "forge.yaml").read_text(encoding="utf-8") == "project: sprint-entry\n"
    # And the divergence is recorded rather than absorbed.
    assert drift is not None
    record = cs.load_audit_record(tmp_path, "sprint-a")
    assert [e["story"] for e in record["drift_events"]] == ["story-b"]


def test_standalone_run_without_a_pin_still_reads_the_project_root(tmp_path: Path) -> None:
    """No sprint, no pin: the pre-existing behaviour is unchanged."""
    _write_root_config(tmp_path, "project: live\n")
    config = _make_coord_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _prepare_story(config, task, workspace)

    assert (workspace / "forge.yaml").read_text(encoding="utf-8") == "project: live\n"


def test_sprint_audit_records_the_config_snapshot(tmp_path: Path) -> None:
    """The sprint audit carries the config identity the sprint ran under."""
    from theforge.sprint.audit import _write_sprint_audit
    from theforge.sprint.manifest import ResolvedSprint, SprintResult

    _write_root_config(tmp_path, "project: pinned\n")
    snap = cs.capture_or_load(tmp_path, "sprint-a")
    _write_root_config(tmp_path, "project: drifted\n")
    cs.check_drift(snap, story="issue-9")

    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    _write_sprint_audit(
        manifest=ResolvedSprint(name="Sprint A", budget_usd=1.0, stories=[], max_parallel=1),
        result=SprintResult(
            name="Sprint A",
            specs_total=0,
            specs_succeeded=0,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.0,
            budget_usd=1.0,
            results=[],
        ),
        canonical_refs=[],
        started_at=now,
        finished_at=now,
        duration=0.0,
        project_root=tmp_path,
        sprint_id="sprint-a",
    )

    audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )
    block = audit["config_snapshot"]
    assert block["digest"] == snap.digest
    assert [e["story"] for e in block["drift_events"]] == ["issue-9"]
