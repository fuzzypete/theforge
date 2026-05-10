"""Regression: forge status DETAIL must distinguish ALREADY_DONE source paths.

Resume-skip-merged classifications (mechanical merge detection — trustworthy)
and preflight-verdict ALREADY_DONE outcomes (historically the suspect path
hardened by #1446) must not collapse to the same DETAIL string. Operators
need to tell at a glance which closure decisions warrant manual verification
and which do not, without running ``gh`` commands per story.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.sprint.status_reader import (
    read_completed_status,
    read_live_status,
)


def _write_summary(tmp_path: Path, stories: list[dict]) -> Path:
    summary_dir = tmp_path / ".forge" / "logs" / "alread-done-sprint"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "sprint-summary.yaml"
    summary_path.write_text(
        yaml.dump(
            {
                "sprint": {"name": "alread-done-sprint", "run_id": "run-ad"},
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return summary_path


def test_completed_summary_distinguishes_resume_skip_from_preflight_verdict(
    tmp_path: Path,
) -> None:
    summary_path = _write_summary(
        tmp_path,
        [
            {
                "slug": "issue-1101",
                "path": "Issue #1101",
                "outcome": "ALREADY_DONE",
                "outcome_source": "resume_skip_merged",
                "cost_usd": 0.0,
            },
            {
                "slug": "issue-2001",
                "path": "Issue #2001",
                "outcome": "ALREADY_DONE",
                "outcome_source": "preflight_verdict",
                "cost_usd": 0.05,
            },
        ],
    )

    entries = {e.slug: e for e in read_completed_status(summary_path)}

    resume_entry = entries["issue-1101"]
    preflight_entry = entries["issue-2001"]

    assert resume_entry.detail == "ALREADY_DONE (merged)"
    assert preflight_entry.detail == "ALREADY_DONE (preflight)"
    assert resume_entry.detail != preflight_entry.detail


def test_completed_summary_legacy_already_done_without_source_renders_default(
    tmp_path: Path,
) -> None:
    summary_path = _write_summary(
        tmp_path,
        [
            {
                "slug": "issue-9999",
                "path": "Issue #9999",
                "outcome": "ALREADY_DONE",
                "cost_usd": 0.0,
            },
        ],
    )

    entries = read_completed_status(summary_path)
    assert len(entries) == 1
    assert entries[0].detail == "ALREADY_DONE"


def _write_live_state(tmp_path: Path, stories: list[dict]) -> str:
    run_id = "run-live-ad"
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = runs_dir / f"{run_id}.state"
    state_path.write_text(
        yaml.dump(
            {
                "sprint_name": "live-sprint",
                "sprint_id": None,
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return run_id


def test_live_state_distinguishes_resume_skip_from_preflight_verdict(
    tmp_path: Path,
) -> None:
    run_id = _write_live_state(
        tmp_path,
        [
            {
                "slug": "issue-1325",
                "path": "Issue #1325",
                "status": "done",
                "outcome": "already_done",
                "phase": None,
                "cost_usd": 0.0,
                "detail": {
                    "final_outcome": "ALREADY_DONE",
                    "outcome_source": "resume_skip_merged",
                },
            },
            {
                "slug": "issue-1444",
                "path": "Issue #1444",
                "status": "done",
                "outcome": "already_done",
                "phase": "DONE",
                "cost_usd": 0.04,
                "detail": {
                    "final_outcome": "ALREADY_DONE",
                    "outcome_source": "preflight_verdict",
                    "preflight_verdict": "ALREADY_DONE",
                },
            },
        ],
    )

    entries = {e.slug: e for e in (read_live_status(run_id, tmp_path) or [])}
    assert entries["issue-1325"].detail == "ALREADY_DONE (merged)"
    assert entries["issue-1444"].detail == "ALREADY_DONE (preflight)"
    assert entries["issue-1325"].detail != entries["issue-1444"].detail
