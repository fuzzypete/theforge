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
