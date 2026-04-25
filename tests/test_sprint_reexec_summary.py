from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

from theforge.sprint.audit import _write_sprint_summary, persist_accumulated_story_state
from theforge.sprint.manifest import SprintResult


def test_write_sprint_summary_merges_prior_run_story_and_counts_skip_correctly(
    tmp_path: Path,
) -> None:
    sprint_log_dir = tmp_path / ".forge" / "logs" / "issues-959,960"
    sprint_log_dir.mkdir(parents=True, exist_ok=True)

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
                "story_run_id": "run-old",
                "depends_on": [],
            }
        ],
    )

    result = SprintResult(
        name="issues-959,960",
        specs_total=1,
        specs_succeeded=0,
        specs_failed=0,
        specs_skipped=1,
        total_cost_usd=0.0,
        budget_usd=20.0,
        results=[],
        stopped_reason=None,
    )

    manifest = SimpleNamespace(name="issues-959,960", budget_usd=20.0, max_parallel=2)
    started_at = finished_at = datetime.datetime(
        2026,
        1,
        1,
        tzinfo=datetime.timezone.utc,
    )

    _write_sprint_summary(
        manifest=manifest,
        result=result,
        canonical_refs=["issue:959", "issue:960"],
        started_at=started_at,
        finished_at=finished_at,
        duration=60.0,
        sprint_log_dir=sprint_log_dir,
        slug_map={"issue:959": "issue-959", "issue:960": "issue-960"},
        run_id="run-new",
        tasks_by_slug={
            "issue-959": SimpleNamespace(depends_on=[]),
            "issue-960": SimpleNamespace(depends_on=[]),
        },
        sprint_id="sprint-abc",
        project_root=tmp_path,
    )

    summary = yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text(encoding="utf-8"))
    assert summary["sprint"]["specs_total"] == 2
    assert summary["sprint"]["specs_succeeded"] == 1
    assert summary["sprint"]["specs_failed"] == 0
    assert summary["sprint"]["specs_skipped"] == 1
    assert summary["sprint"]["total_cost_usd"] == 10.31

    by_slug = {story["slug"]: story for story in summary["stories"]}
    assert by_slug["issue-959"]["story_run_id"] == "run-old"
    assert by_slug["issue-960"]["outcome"] == "SKIPPED"


def test_write_sprint_summary_prefers_story_audit_over_stale_accumulated_state(
    tmp_path: Path,
) -> None:
    sprint_log_dir = tmp_path / ".forge" / "logs" / "issues-1015,1016"
    sprint_log_dir.mkdir(parents=True, exist_ok=True)

    persist_accumulated_story_state(
        "sprint-xyz",
        "issues-1015,1016",
        tmp_path,
        [
            {
                "canonical_ref": "issue:1015",
                "slug": "issue-1015",
                "path": "Issue #1015",
                "outcome": "ESCALATE",
                "cost_usd": 0.0,
                "story_run_id": "run-old-failed",
                "preflight": "PROCEED",
                "error": "WORKSPACE abort: base branch has diverged",
                "error_type": "workspace_abort",
                "depends_on": [],
            }
        ],
    )

    story_audit_dir = sprint_log_dir / "issue-1015"
    story_audit_dir.mkdir(parents=True, exist_ok=True)
    with open(story_audit_dir / "audit.yaml", "w", encoding="utf-8") as f:
        yaml.dump(
            {
                "run_id": "run-new-success",
                "outcome": {
                    "success": True,
                    "final_phase": "DONE",
                    "message": "done",
                    "error_type": None,
                },
                "timing": {
                    "started_at": "2026-04-25T12:00:00Z",
                    "finished_at": "2026-04-25T12:05:00Z",
                },
                "cost": {"total_usd": 4.25},
                "preflight": {
                    "verdict": "PROCEED",
                    "original_verdict": None,
                    "source_run_id": None,
                },
                "iterations": {
                    "usage_summary": {
                        "dev": {"used": 1, "max": 3, "hit_limit": False, "early_finish": True},
                        "review": {
                            "used": 1,
                            "max": 2,
                            "hit_limit": False,
                            "early_finish": True,
                        },
                    }
                },
                "reviews": [{"verdict": "APPROVE"}],
                "merge": {"merged": True},
                "error": None,
                "error_type": None,
            },
            f,
            default_flow_style=False,
            sort_keys=False,
        )

    result = SprintResult(
        name="issues-1015,1016",
        specs_total=1,
        specs_succeeded=0,
        specs_failed=0,
        specs_skipped=1,
        total_cost_usd=0.0,
        budget_usd=20.0,
        results=[],
        stopped_reason=None,
    )

    manifest = SimpleNamespace(name="issues-1015,1016", budget_usd=20.0, max_parallel=2)
    started_at = finished_at = datetime.datetime(
        2026,
        4,
        25,
        tzinfo=datetime.timezone.utc,
    )

    _write_sprint_summary(
        manifest=manifest,
        result=result,
        canonical_refs=["issue:1015"],
        started_at=started_at,
        finished_at=finished_at,
        duration=60.0,
        sprint_log_dir=sprint_log_dir,
        slug_map={"issue:1015": "issue-1015"},
        run_id="run-summary",
        tasks_by_slug={"issue-1015": SimpleNamespace(depends_on=[])},
        sprint_id="sprint-xyz",
        project_root=tmp_path,
    )

    summary = yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text(encoding="utf-8"))
    story = summary["stories"][0]

    assert story["outcome"] == "DONE"
    assert story["story_run_id"] == "run-new-success"
    assert story["verdict"] == "APPROVE"
    assert story["error"] is None
    assert story["merge"] is True
