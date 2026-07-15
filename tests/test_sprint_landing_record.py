"""Seam tests: the structured landing record survives the coordinator→sprint
audit boundary and reaches every persisted surface (issue #1424).

The distinguishing signal (``land_story``'s ``landing_path``) is produced in the
coordinator's completion phase and must flow, un-collapsed, into the sprint
summary, the sprint audit, and the per-run audit record so an operator can tell
a sprint that shipped code from one that short-circuited via a guard.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.audit import _write_sprint_audit, _write_sprint_summary
from theforge.sprint.manifest import SprintResult
from theforge.task import TaskStory

FRESH_MERGE = {
    "action": "merge-pr",
    "pr_url": "https://github.com/x/y/pull/1414",
    "merged": True,
    "merge_queued": False,
    "success": True,
    "landing_path": "fresh-merge",
    "guard_evidence": {"branch": "feat/issue-1414", "pr_url": "https://github.com/x/y/pull/1414"},
}

ALREADY_MERGED = {
    "action": "merge-pr",
    "pr_url": "https://github.com/x/y/pull/1143",
    "merged": True,
    "success": True,
    "landing_path": "already-merged",
    "guard_evidence": {
        "branch": "feat/issue-1135",
        "pr_number": 1143,
        "pr_url": "https://github.com/x/y/pull/1143",
        "merged_at": "2026-04-30T14:21:12Z",
    },
}


def _result(merge: dict) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    return CoordinatorResult(
        success=True, phase=Phase.DONE, state=state, message="done", merge=merge
    )


def _manifest():
    from types import SimpleNamespace

    return SimpleNamespace(name="issues-1135,1414", budget_usd=20.0, max_parallel=2)


def _sprint_result(pairs: list[tuple[str, CoordinatorResult]]) -> SprintResult:
    return SprintResult(
        name="issues-1135,1414",
        specs_total=len(pairs),
        specs_succeeded=len(pairs),
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=2.0,
        budget_usd=20.0,
        results=pairs,
        stopped_reason=None,
    )


def test_sprint_summary_distinguishes_fresh_merge_from_short_circuit(tmp_path: Path) -> None:
    sprint_log_dir = tmp_path / ".forge" / "logs" / "issues-1135,1414"
    sprint_log_dir.mkdir(parents=True, exist_ok=True)

    pairs = [("issue:1414", _result(FRESH_MERGE)), ("issue:1135", _result(ALREADY_MERGED))]
    started_at = finished_at = datetime.datetime(2026, 5, 4, tzinfo=datetime.timezone.utc)

    _write_sprint_summary(
        manifest=_manifest(),
        result=_sprint_result(pairs),
        canonical_refs=["issue:1414", "issue:1135"],
        started_at=started_at,
        finished_at=finished_at,
        duration=60.0,
        sprint_log_dir=sprint_log_dir,
        slug_map={"issue:1414": "issue-1414", "issue:1135": "issue-1135"},
        run_id="run-1",
    )

    summary = yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text(encoding="utf-8"))
    by_slug = {s["slug"]: s for s in summary["stories"]}

    fresh = by_slug["issue-1414"]["landing"]
    guard = by_slug["issue-1135"]["landing"]
    assert fresh["outcome"] == "merged"
    assert fresh["fresh_pr_created"] is True
    assert guard["outcome"] == "already-merged-short-circuit"
    assert guard["fresh_pr_created"] is False
    assert guard["pr_merged_at"] == "2026-04-30T14:21:12Z"
    # Legacy boolean is preserved and (in this failure mode) identical for both.
    assert by_slug["issue-1414"]["merge"] is True
    assert by_slug["issue-1135"]["merge"] is True


def test_sprint_audit_carries_landing_record(tmp_path: Path) -> None:
    pairs = [("issue:1414", _result(FRESH_MERGE)), ("issue:1135", _result(ALREADY_MERGED))]
    started_at = finished_at = datetime.datetime(2026, 5, 4, tzinfo=datetime.timezone.utc)

    _write_sprint_audit(
        manifest=_manifest(),
        result=_sprint_result(pairs),
        canonical_refs=["issue:1414", "issue:1135"],
        started_at=started_at,
        finished_at=finished_at,
        duration=60.0,
        project_root=tmp_path,
        slug_map={"issue:1414": "issue-1414", "issue:1135": "issue-1135"},
    )

    audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )
    by_path = {s["path"]: s for s in audit["specs"]}
    assert by_path["Issue #1414"]["landing"]["fresh_pr_created"] is True
    assert by_path["Issue #1135"]["landing"]["outcome"] == "already-merged-short-circuit"
    assert by_path["Issue #1135"]["landing"]["fresh_pr_created"] is False


def test_per_run_audit_record_carries_landing(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec", encoding="utf-8")
    config = ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(gate_command="make gate"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )
    task = TaskStory(name="Test", slug="issue-1135", story_path=spec_path)
    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    result = CoordinatorResult(
        success=True, phase=Phase.DONE, state=state, message="done", merge=ALREADY_MERGED
    )

    record = generate_audit_log(config, task, result)
    assert record["landing"]["outcome"] == "already-merged-short-circuit"
    assert record["landing"]["fresh_pr_created"] is False
    # The raw merge_info dict remains alongside the structured field.
    assert record["merge"]["landing_path"] == "already-merged"
