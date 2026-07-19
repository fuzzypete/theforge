from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _write_summary(tmp_path: Path, stories: list[dict]) -> Path:
    summary_dir = tmp_path / ".forge" / "logs" / "elapsed-sprint"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "sprint-summary.yaml"
    summary_path.write_text(
        yaml.dump(
            {
                "sprint": {
                    "name": "elapsed-sprint",
                    "run_id": "run-elapsed",
                },
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return summary_path


def _write_live_state(
    tmp_path: Path,
    run_id: str,
    stories: list[dict],
    *,
    sprint_id: str | None = None,
) -> None:
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = runs_dir / f"{run_id}.state"
    state_path.write_text(
        yaml.dump(
            {
                "sprint_name": "elapsed-sprint",
                "sprint_id": sprint_id,
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )


def test_read_completed_status_populates_elapsed_seconds(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_completed_status

    summary_path = _write_summary(
        tmp_path,
        [
            {
                "slug": "issue-1",
                "path": "Issue #1",
                "outcome": "DONE",
                "started_at": "2026-04-29T10:00:00Z",
                "finished_at": "2026-04-29T11:03:00Z",
            }
        ],
    )

    entries = read_completed_status(summary_path)

    assert len(entries) == 1
    assert entries[0].elapsed_seconds == pytest.approx(3780.0)


def test_read_completed_status_leaves_elapsed_seconds_blank_without_timestamps(
    tmp_path: Path,
) -> None:
    from theforge.sprint.status_reader import read_completed_status

    summary_path = _write_summary(
        tmp_path,
        [{"slug": "issue-2", "path": "Issue #2", "outcome": "DONE"}],
    )

    entries = read_completed_status(summary_path)

    assert len(entries) == 1
    assert entries[0].elapsed_seconds is None


def test_completed_skipped_story_prefers_persisted_reason() -> None:
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    stage, detail, complexity = _stage_and_detail_from_completed_story(
        {
            "slug": "issue-3",
            "outcome": "SKIPPED",
            "error": "budget exhausted ($6.00 >= $5.00)",
            "drop_reason": "held by collision",
        },
        None,
    )

    assert stage == ""
    assert detail == "budget exhausted ($6.00 >= $5.00)"
    assert complexity is None


def test_completed_skipped_story_with_dependencies_keeps_waiting_detail() -> None:
    from theforge.sprint.status_reader import _stage_and_detail_from_completed_story

    stage, detail, _ = _stage_and_detail_from_completed_story(
        {
            "slug": "issue-4",
            "outcome": "SKIPPED",
            "depends_on": ["issue-1"],
            "drop_reason": "budget exhausted",
        },
        None,
    )

    assert stage == "dependency"
    assert detail == "depends on #1"


def test_live_skipped_story_uses_reason_detail() -> None:
    from theforge.sprint.status_reader import _stage_and_detail_from_live_story

    stage, detail, _ = _stage_and_detail_from_live_story(
        {
            "slug": "issue-5",
            "status": "skipped",
            "phase": "DONE",
            "reason": "held by collision",
            "detail": {"final_outcome": "SKIPPED"},
        }
    )

    assert stage == ""
    assert detail == "held by collision"


def test_read_live_status_populates_elapsed_seconds_for_running_story(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from theforge.sprint.status_reader import read_live_status

    started_at = (datetime.now(timezone.utc) - timedelta(minutes=5, seconds=7)).isoformat()
    _write_live_state(
        tmp_path,
        "run-live",
        [
            {
                "slug": "issue-6",
                "path": "Issue #6",
                "status": "running",
                "phase": "DEV",
                "started_at": started_at,
                "detail": {},
            }
        ],
    )

    entries = read_live_status("run-live", tmp_path)

    assert entries is not None
    assert len(entries) == 1
    assert entries[0].elapsed_seconds is not None
    assert 300.0 <= entries[0].elapsed_seconds <= 320.0


def test_read_live_status_populates_elapsed_seconds_for_accumulated_story(tmp_path: Path) -> None:
    from theforge.sprint.audit import persist_accumulated_story_state
    from theforge.sprint.status_reader import read_live_status

    _write_live_state(tmp_path, "run-reexec", [], sprint_id="sprint-123")
    persist_accumulated_story_state(
        "sprint-123",
        "elapsed-sprint",
        tmp_path,
        [
            {
                "canonical_ref": "issue:7",
                "slug": "issue-7",
                "path": "Issue #7",
                "outcome": "DONE",
                "started_at": "2026-04-29T10:00:00Z",
                "finished_at": "2026-04-29T10:12:00Z",
            }
        ],
    )

    entries = read_live_status("run-reexec", tmp_path)

    assert entries is not None
    assert len(entries) == 1
    assert entries[0].elapsed_seconds == pytest.approx(720.0)


@pytest.mark.parametrize(
    ("elapsed_s", "expected"),
    [
        (None, "—"),
        (59, "0m"),
        (3599, "59m"),
        (3780, "1h 3m"),
    ],
)
def test_format_story_elapsed(elapsed_s: float | None, expected: str) -> None:
    from theforge.cli.sprint_status import _format_story_elapsed

    assert _format_story_elapsed(elapsed_s) == expected
