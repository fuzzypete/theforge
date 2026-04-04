"""Tests for the pending decision file module."""

from __future__ import annotations

import datetime
import threading
import time
from unittest.mock import patch

import yaml

from theforge import pending


def test_write_pending_creates_correct_yaml(tmp_path):
    path = pending.write_pending(
        run_id="abc123",
        story="my-story",
        phase="HUMAN_REVIEW",
        reason="dev agent produced 0 changes",
        options=["approve", "reject", "escalate"],
        timeout_seconds=60,
        project_root=tmp_path,
    )
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data["run_id"] == "abc123"
    assert data["story"] == "my-story"
    assert data["phase"] == "HUMAN_REVIEW"
    assert data["reason"] == "dev agent produced 0 changes"
    assert data["options"] == ["approve", "reject", "escalate"]
    assert "created_at" in data
    assert "timeout_at" in data
    assert "pid" in data
    # No decision yet
    assert "decision" not in data


def test_write_pending_creates_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    pending.write_pending(
        run_id="xyz",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve"],
        timeout_seconds=10,
        project_root=project,
    )
    assert (project / ".forge" / "pending" / "xyz.yaml").exists()


def test_read_pending_returns_none_for_missing(tmp_path):
    result = pending.read_pending("nonexistent", project_root=tmp_path)
    assert result is None


def test_read_pending_returns_data(tmp_path):
    pending.write_pending(
        run_id="r1",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve", "reject"],
        timeout_seconds=30,
        project_root=tmp_path,
    )
    data = pending.read_pending("r1", project_root=tmp_path)
    assert data is not None
    assert data["run_id"] == "r1"


def test_read_pending_cleans_dead_pid_file(tmp_path):
    path = pending.write_pending(
        run_id="dead-pid",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve"],
        timeout_seconds=30,
        project_root=tmp_path,
    )

    with patch("theforge.pending.os.kill", side_effect=ProcessLookupError):
        data = pending.read_pending("dead-pid", project_root=tmp_path)

    assert data is None
    assert not path.exists()


def test_read_pending_cleans_legacy_file_without_pid(tmp_path):
    pending_dir = tmp_path / ".forge" / "pending"
    pending_dir.mkdir(parents=True)
    path = pending_dir / "legacy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "run_id": "legacy",
                "story": "s",
                "phase": "ESCALATE",
                "reason": "r",
                "options": ["approve"],
            }
        ),
        encoding="utf-8",
    )

    data = pending.read_pending("legacy", project_root=tmp_path)

    assert data is None
    assert not path.exists()


def test_resolve_pending_writes_decision(tmp_path):
    pending.write_pending(
        run_id="r2",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve", "reject"],
        timeout_seconds=30,
        project_root=tmp_path,
    )
    ok = pending.resolve_pending("r2", "approve", project_root=tmp_path)
    assert ok
    data = pending.read_pending("r2", project_root=tmp_path)
    assert data is not None
    assert data["decision"] == "approve"
    assert "decided_at" in data


def test_resolve_pending_returns_false_for_missing(tmp_path):
    ok = pending.resolve_pending("nope", "approve", project_root=tmp_path)
    assert not ok


def test_cleanup_pending_removes_file(tmp_path):
    pending.write_pending(
        run_id="r3",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve"],
        timeout_seconds=10,
        project_root=tmp_path,
    )
    pending.cleanup_pending("r3", project_root=tmp_path)
    assert pending.read_pending("r3", project_root=tmp_path) is None


def test_list_pending_returns_all_files(tmp_path):
    for i in range(3):
        pending.write_pending(
            run_id=f"run-{i}",
            story="s",
            phase="ESCALATE",
            reason="r",
            options=["approve"],
            timeout_seconds=30,
            project_root=tmp_path,
        )
    entries = pending.list_pending(project_root=tmp_path)
    assert len(entries) == 3
    run_ids = {e["run_id"] for e in entries}
    assert run_ids == {"run-0", "run-1", "run-2"}


def test_list_pending_empty_when_no_dir(tmp_path):
    entries = pending.list_pending(project_root=tmp_path)
    assert entries == []


def test_poll_pending_returns_decision_when_file_updated(tmp_path):
    pending.write_pending(
        run_id="poll-test",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve", "reject"],
        timeout_seconds=10,
        project_root=tmp_path,
    )

    def _write_decision():
        time.sleep(0.3)
        pending.resolve_pending("poll-test", "approve", project_root=tmp_path)

    t = threading.Thread(target=_write_decision, daemon=True)
    t.start()

    decision, decided_at = pending.poll_pending(
        "poll-test", timeout_seconds=5, poll_interval=0.1, project_root=tmp_path
    )
    t.join(timeout=2)
    assert decision == "approve"
    assert decided_at is not None


def test_poll_pending_returns_timeout_when_expired(tmp_path):
    pending.write_pending(
        run_id="poll-timeout",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve"],
        timeout_seconds=1,
        project_root=tmp_path,
    )
    decision, decided_at = pending.poll_pending(
        "poll-timeout", timeout_seconds=1, poll_interval=0.1, project_root=tmp_path
    )
    assert decision == "timeout"
    assert decided_at is None


def test_poll_pending_ignores_stale_dead_pid_file(tmp_path):
    path = pending.write_pending(
        run_id="stale-poll",
        story="s",
        phase="ESCALATE",
        reason="r",
        options=["approve"],
        timeout_seconds=30,
        project_root=tmp_path,
    )

    with patch("theforge.pending.os.kill", side_effect=ProcessLookupError):
        decision, decided_at = pending.poll_pending(
            "stale-poll", timeout_seconds=0.05, poll_interval=0.01, project_root=tmp_path
        )

    assert decision == "timeout"
    assert decided_at is None
    assert not path.exists()


def test_cleanup_stale_removes_expired_files(tmp_path):
    # Write a file with a past timeout
    pending_dir = tmp_path / ".forge" / "pending"
    pending_dir.mkdir(parents=True)
    past = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=10)
    ).isoformat()
    data = {
        "run_id": "stale-run",
        "story": "s",
        "phase": "ESCALATE",
        "reason": "r",
        "options": ["approve"],
        "created_at": past,
        "timeout_at": past,
        "pid": 999999,  # almost certainly not running
    }
    (pending_dir / "stale-run.yaml").write_text(yaml.safe_dump(data))

    removed = pending.cleanup_stale(project_root=tmp_path)
    assert removed == 1
    assert pending.read_pending("stale-run", project_root=tmp_path) is None
