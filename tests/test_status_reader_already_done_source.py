"""Regression: forge status DETAIL must distinguish ALREADY_DONE source paths.

Resume-skip-merged classifications (mechanical merge detection — trustworthy)
and preflight-verdict ALREADY_DONE outcomes (historically the suspect path
hardened by #1446) must not collapse to the same DETAIL string. Operators
need to tell at a glance which closure decisions warrant manual verification
and which do not, without running ``gh`` commands per story.
"""

from __future__ import annotations

import datetime
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


def test_live_state_accumulated_stories_distinguish_already_done_sources(
    tmp_path: Path,
) -> None:
    """read_live_status must respect outcome_source for accumulated stories
    loaded from the sprint-level state file (cross-process resume), not just
    stories already in the current run's live .state file."""
    from theforge.sprint.audit import _save_accumulated_stories

    sprint_id = "sprint-accumulated-ad"
    _save_accumulated_stories(
        sprint_id,
        "live-sprint",
        tmp_path,
        [
            {
                "canonical_ref": "issue:1101",
                "slug": "issue-1101",
                "path": "Issue #1101",
                "outcome": "ALREADY_DONE",
                "outcome_source": "resume_skip_merged",
                "cost_usd": 0.0,
                "depends_on": [],
            },
            {
                "canonical_ref": "issue:2002",
                "slug": "issue-2002",
                "path": "Issue #2002",
                "outcome": "ALREADY_DONE",
                "outcome_source": "preflight_verdict",
                "cost_usd": 0.05,
                "depends_on": [],
            },
        ],
    )

    run_id = "run-live-ad-acc"
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.state").write_text(
        yaml.dump(
            {
                "sprint_name": "live-sprint",
                "sprint_id": sprint_id,
                "stories": [],
            }
        ),
        encoding="utf-8",
    )

    entries = {e.slug: e for e in (read_live_status(run_id, tmp_path) or [])}
    assert entries["issue-1101"].detail == "ALREADY_DONE (merged)"
    assert entries["issue-2002"].detail == "ALREADY_DONE (preflight)"


def test_write_sprint_audit_carries_outcome_source_for_both_paths(
    tmp_path: Path,
) -> None:
    """Sprint audit YAML must record outcome_source so audit/postmortem
    consumers can distinguish trust paths without parsing strings."""
    from unittest.mock import MagicMock

    from theforge.sprint.audit import _write_sprint_audit
    from theforge.sprint.manifest import ResolvedSprint, SprintResult

    # Preflight ALREADY_DONE story — runs through results path.
    preflight_state = MagicMock()
    preflight_state.preflight_cached = False
    preflight_state.preflight_verdict = "ALREADY_DONE"
    preflight_state.preflight_reason = "Spec already satisfied."
    preflight_state.preflight_cached_original_verdict = None
    preflight_state.preflight_cached_from_run_id = None
    preflight_state.total_cost = 0.05
    preflight_state.error = None
    preflight_state.error_type = None
    preflight_state.review_results = []
    preflight_state.review_cycle_metadata = []
    preflight_state.dev_iteration_telemetry = []
    preflight_state.review_iteration_telemetry = []
    preflight_phase = MagicMock()
    preflight_phase.name = "DONE"
    preflight_result = MagicMock()
    preflight_result.state = preflight_state
    preflight_result.phase = preflight_phase
    preflight_result.merge = None

    # Resume-skip-merged story — never produced a CoordinatorResult; reaches
    # the drop path with triage_action == "skip_merged".
    resolved = ResolvedSprint(
        name="audit-source-sprint",
        budget_usd=10.0,
        stories=[],
    )
    sprint_result = SprintResult(
        name="audit-source-sprint",
        specs_total=2,
        specs_succeeded=2,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=0.05,
        budget_usd=10.0,
        results=[("issue:2001", preflight_result)],
        stopped_reason=None,
    )

    started = datetime.datetime(2026, 5, 9, 9, 0, tzinfo=datetime.timezone.utc)
    finished = datetime.datetime(2026, 5, 9, 9, 5, tzinfo=datetime.timezone.utc)

    _write_sprint_audit(
        manifest=resolved,
        result=sprint_result,
        canonical_refs=["issue:2001", "issue:1101"],
        started_at=started,
        finished_at=finished,
        duration=300.0,
        project_root=tmp_path,
        slug_map={"issue:2001": "issue-2001", "issue:1101": "issue-1101"},
        triage_actions_by_ref={"issue:1101": "skip_merged"},
    )

    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    data = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    specs_by_path = {entry["path"]: entry for entry in data["specs"]}

    preflight_entry = specs_by_path["Issue #2001"]
    resume_entry = specs_by_path["Issue #1101"]

    assert preflight_entry["outcome"] == "ALREADY_DONE"
    assert preflight_entry["outcome_source"] == "preflight_verdict"
    assert resume_entry["outcome"] == "ALREADY_DONE"
    assert resume_entry["outcome_source"] == "resume_skip_merged"
