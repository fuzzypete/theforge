"""Seam coverage for shape-gate skip emission → substrate → postmortem block.

Issue #1453: the gate's skip partition must land in the substrate with taxonomy
categories (including remediation outcomes), and the summary/RCA block must
project this run's events with stuck-issue flags. This exercises the full
emission → query → block seam the sprint entry path relies on.
"""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator.audit_substrate import (
    create_or_open,
    iter_shape_skip_events,
    record_shape_skip_event,
)
from theforge.sprint.shape_gate import SkippedIssue
from theforge.sprint.skip_report import (
    build_shape_gate_skip_block,
    emit_shape_skip_events,
)


def test_emission_records_category_and_axis(tmp_path: Path) -> None:
    skipped = [
        SkippedIssue(
            issue_number=1135,
            reason_codes=("reopened_stale_contract",),
            source="local_check",
        ),
        SkippedIssue(
            issue_number=7,
            reason_codes=("needs_grooming_label",),
            source="label",
        ),
    ]
    written = emit_shape_skip_events(
        tmp_path,
        run_id="run-1",
        sprint_name="v0.11",
        skipped=skipped,
    )
    assert written == 2

    conn = create_or_open(tmp_path)
    try:
        events = {e["issue_id"]: e for e in iter_shape_skip_events(conn, run_id="run-1")}
    finally:
        conn.close()

    assert events["1135"]["category"] == "blocked_by_semantic_gate"
    assert events["1135"]["four_question_axis"] == "response_not_yet_attempted"
    assert events["7"]["category"] == "blocked_by_stale_label"
    assert events["7"]["severity"] == "blocking"


def test_remediation_outcomes_change_category(tmp_path: Path) -> None:
    code = ("reopened_stale_contract",)
    skipped = [
        SkippedIssue(issue_number=10, reason_codes=code, source="local_check"),
        SkippedIssue(issue_number=11, reason_codes=code, source="local_check"),
    ]
    emit_shape_skip_events(
        tmp_path,
        run_id="run-1",
        skipped=skipped,
        remediated_numbers={10},
        declined_numbers={11},
    )

    conn = create_or_open(tmp_path)
    try:
        events = {e["issue_id"]: e for e in iter_shape_skip_events(conn, run_id="run-1")}
    finally:
        conn.close()

    assert events["10"]["category"] == "remediated_and_proceeded"
    assert events["11"]["category"] == "declined_by_remediation"
    assert events["11"]["four_question_axis"] == "response_attempted_gate_verification_failed"


def test_advisories_emitted_with_advisory_severity(tmp_path: Path) -> None:
    advisories = [
        SkippedIssue(
            issue_number=99,
            reason_codes=("reopened_stale_contract",),
            source="local_check",
        )
    ]
    emit_shape_skip_events(tmp_path, run_id="run-1", advisories=advisories)

    conn = create_or_open(tmp_path)
    try:
        events = list(iter_shape_skip_events(conn, run_id="run-1"))
    finally:
        conn.close()
    assert len(events) == 1
    assert events[0]["severity"] == "advisory"


def test_emission_failure_is_swallowed(tmp_path: Path) -> None:
    def _boom(_root: Path, _event: dict) -> int:
        raise RuntimeError("substrate offline")

    skipped = [SkippedIssue(issue_number=1, reason_codes=("missing_type",), source="local_check")]
    # Must not raise — observability, not gating.
    written = emit_shape_skip_events(tmp_path, run_id="r", skipped=skipped, record=_boom)
    assert written == 0


def test_build_block_scopes_stuck_to_this_run(tmp_path: Path) -> None:
    # Four prior blocks across runs, latest in this run.
    for i in range(3):
        record_shape_skip_event(
            tmp_path,
            {
                "issue_id": "1135",
                "reason_code": "reopened_stale_contract",
                "severity": "blocking",
                "category": "blocked_by_semantic_gate",
                "four_question_axis": "response_not_yet_attempted",
                "source": "local_check",
                "run_id": f"old-{i}",
                "emitted_at": f"2026-05-0{i + 4}T00:00:00Z",
            },
        )
    emit_shape_skip_events(
        tmp_path,
        run_id="run-now",
        skipped=[
            SkippedIssue(
                issue_number=1135,
                reason_codes=("reopened_stale_contract",),
                source="local_check",
            )
        ],
    )

    block = build_shape_gate_skip_block(tmp_path, "run-now", threshold=3)
    assert block is not None
    assert block["total"] == 1
    assert block["threshold"] == 3
    stuck = block["stuck_issues"]
    assert len(stuck) == 1
    assert stuck[0]["issue_id"] == "1135"
    assert stuck[0]["block_count"] == 4


def test_build_block_none_when_no_events(tmp_path: Path) -> None:
    assert build_shape_gate_skip_block(tmp_path, "absent-run", threshold=3) is None


def test_build_block_none_without_run_id(tmp_path: Path) -> None:
    assert build_shape_gate_skip_block(tmp_path, None, threshold=3) is None


def test_summary_writer_embeds_block_from_substrate(tmp_path: Path) -> None:
    """Seam: emitted skip events → ``_write_sprint_summary`` → summary YAML.

    Exercises the sprint-entry → summary-writer boundary directly: the writer
    must query the substrate by run_id and embed the ``shape_gate_skips`` block
    the digest and RCA then read (convention 8 — cross-phase state handoff).
    """
    import datetime

    import yaml

    from theforge.sprint.audit import _write_sprint_summary
    from theforge.sprint.manifest import ResolvedSprint, SprintResult
    from theforge.sprint.story_state import SprintStoryState, StoryOutcome

    emit_shape_skip_events(
        tmp_path,
        run_id="run-seam",
        sprint_name="v0.11",
        skipped=[
            SkippedIssue(
                issue_number=1135,
                reason_codes=("reopened_stale_contract",),
                source="local_check",
            )
        ],
    )

    story_state = SprintStoryState()
    story_state.register(
        "issue-1135",
        "Issue #1135",
        outcome=StoryOutcome.SKIPPED,
        reason="shape-gate",
        canonical_ref="issue:1135",
    )
    manifest = ResolvedSprint(name="v0.11", budget_usd=10.0, stories=[], max_parallel=1)
    result = SprintResult(
        name="v0.11",
        specs_total=1,
        specs_succeeded=0,
        specs_failed=0,
        specs_skipped=1,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[],
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    log_dir = tmp_path / ".forge" / "logs" / "v0.11"
    log_dir.mkdir(parents=True, exist_ok=True)
    _write_sprint_summary(
        manifest=manifest,
        result=result,
        canonical_refs=[],
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=log_dir,
        skipped_issues=[],
        story_state=story_state,
        run_id="run-seam",
        project_root=tmp_path,
    )

    summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text(encoding="utf-8"))
    block = summary["shape_gate_skips"]
    assert block["total"] == 1
    assert "blocked_by_semantic_gate" in block["categories"]
