"""Seam-level coverage for shape-gate → audit-substrate verdict emission.

Issue #1517: every shape-gate verdict (skipped *and* runnable) must land in
the SQLite audit substrate so refusal-economics queries can run against a
single source. Substrate write failures must be observability-only — the
gate must continue to partition issues and the failure must surface as a
WARNING log.
"""

from __future__ import annotations

import logging
import sqlite3
import textwrap
from pathlib import Path

import pytest

from theforge.coordinator.audit_substrate import (
    iter_shape_verdict_events,
    record_shape_verdict_event,
    substrate_path,
)
from theforge.shape_check import ShapeVerdict
from theforge.sprint.shape_gate import apply_shape_gate

_RUNNABLE_BODY = textwrap.dedent(
    """\
    ## What
    Add a small toggle so callers can opt in.

    ## Why
    Operators need a way to silence the warning during local runs.

    ## Acceptance criteria
    - The toggle defaults to off.
    - When on, the warning is suppressed.

    ## Example
    Before: warning printed every run. After: silent when toggle on.
    """
)

_BAD_BODY = "no headings, just a sentence."


def _emit_to_substrate(project_root: Path):
    def _emit(event: dict) -> None:
        event = dict(event)
        event.setdefault("run_id", "run-test-1")
        event.setdefault("sprint_name", "sprint-test")
        event.setdefault("milestone", "v0.11.0")
        event.setdefault("base_branch_sha", "deadbeef")
        record_shape_verdict_event(project_root, event)

    return _emit


def _fake_detail(body: str, labels: list[str]):
    def _fetch(_number, _root):
        return {"title": "issue", "body": body, "labels": labels}

    return _fetch


def test_runnable_verdict_lands_in_substrate(tmp_path: Path) -> None:
    issues = [{"number": 42, "title": "well-formed"}]

    apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, ["enhancement"]),
        emit_verdict=_emit_to_substrate(tmp_path),
    )

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        rows = conn.execute(
            "SELECT issue_id, verdict, milestone, run_id, base_branch_sha, sprint_name, source "
            "FROM shape_verdict_events"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "42"
    assert row[1] == ShapeVerdict.RUNNABLE.value
    assert row[2] == "v0.11.0"
    assert row[3] == "run-test-1"
    assert row[4] == "deadbeef"
    assert row[5] == "sprint-test"
    assert row[6] == "local_check"


def test_skipped_verdict_lands_in_substrate(tmp_path: Path) -> None:
    issues = [{"number": 7, "title": "malformed"}]

    apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_BAD_BODY, []),
        emit_verdict=_emit_to_substrate(tmp_path),
    )

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        events = list(iter_shape_verdict_events(conn, issue_id="7"))
    finally:
        conn.close()

    assert len(events) == 1
    assert events[0]["issue_id"] == "7"
    assert events[0]["verdict"] != ShapeVerdict.RUNNABLE.value
    assert events[0]["source"] == "local_check"


def test_verdict_distribution_query_groups_by_verdict_and_milestone(
    tmp_path: Path,
) -> None:
    apply_shape_gate(
        [{"number": 1, "title": "ok"}],
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, ["enhancement"]),
        emit_verdict=_emit_to_substrate(tmp_path),
    )
    apply_shape_gate(
        [{"number": 2, "title": "bad"}],
        tmp_path,
        fetch_detail=_fake_detail(_BAD_BODY, []),
        emit_verdict=_emit_to_substrate(tmp_path),
    )

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        rows = conn.execute(
            "SELECT verdict, milestone, COUNT(*) FROM shape_verdict_events "
            "GROUP BY verdict, milestone"
        ).fetchall()
    finally:
        conn.close()

    counts = {(verdict, milestone): n for verdict, milestone, n in rows}
    assert counts[(ShapeVerdict.RUNNABLE.value, "v0.11.0")] == 1
    # The malformed issue produces some non-runnable verdict — exact code is
    # owned by the shape-check classifier; assert there is exactly one row
    # for milestone v0.11.0 that is not RUNNABLE.
    non_runnable = [
        n for (v, m), n in counts.items() if m == "v0.11.0" and v != ShapeVerdict.RUNNABLE.value
    ]
    assert sum(non_runnable) == 1


def test_per_issue_trajectory_query_orders_by_timestamp(tmp_path: Path) -> None:
    # First emission: malformed → some non-runnable verdict.
    apply_shape_gate(
        [{"number": 99, "title": "draft"}],
        tmp_path,
        fetch_detail=_fake_detail(_BAD_BODY, []),
        emit_verdict=_emit_to_substrate(tmp_path),
    )
    # Second emission: same issue is now runnable.
    apply_shape_gate(
        [{"number": 99, "title": "draft"}],
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, ["enhancement"]),
        emit_verdict=_emit_to_substrate(tmp_path),
    )

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        rows = conn.execute(
            "SELECT verdict FROM shape_verdict_events "
            "WHERE issue_id = ? ORDER BY emitted_at ASC, event_id ASC",
            ("99",),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[0][0] != ShapeVerdict.RUNNABLE.value
    assert rows[-1][0] == ShapeVerdict.RUNNABLE.value


def test_substrate_write_failure_does_not_block_gate(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issues = [
        {"number": 1, "title": "good"},
        {"number": 2, "title": "bad"},
    ]

    def _fetch(number, _root):
        if number == 1:
            return {"title": "good", "body": _RUNNABLE_BODY, "labels": ["enhancement"]}
        return {"title": "bad", "body": _BAD_BODY, "labels": []}

    def _exploding_emit(_event: dict) -> None:
        raise RuntimeError("substrate offline")

    with caplog.at_level(logging.WARNING, logger="theforge.sprint.shape_gate"):
        result = apply_shape_gate(
            issues,
            tmp_path,
            fetch_detail=_fetch,
            emit_verdict=_exploding_emit,
        )

    # Gate must still partition issues — observability is not gating.
    assert [r["number"] for r in result.runnable] == [1]
    assert [s.issue_number for s in result.skipped] == [2]
    # And the failure must be visible as a WARNING.
    warning_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING
    ]
    assert any("shape-gate verdict substrate write failed" in m for m in warning_messages)


def test_record_shape_verdict_event_requires_issue_id_and_verdict(
    tmp_path: Path,
) -> None:
    from theforge.coordinator.audit_substrate import SubstrateError

    with pytest.raises(SubstrateError):
        record_shape_verdict_event(tmp_path, {"verdict": "runnable"})
    with pytest.raises(SubstrateError):
        record_shape_verdict_event(tmp_path, {"issue_id": "1"})
