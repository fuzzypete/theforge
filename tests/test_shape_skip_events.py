"""Substrate coverage for shape-gate skip events (issue #1453 AC1/AC3/AC6).

The ``shape_skip_events`` table is the canonical per-skip record. These tests
pin: prior-skip-history embedding at write time (AC1), the date-range/code query
surface (AC6), and stuck-issue detection (AC3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.coordinator.audit_substrate import (
    SKIP_STATUS_NO_PRIOR,
    SKIP_STATUS_STILL_BLOCKED,
    SKIP_STATUS_UNBLOCKED,
    SubstrateError,
    create_or_open,
    iter_shape_skip_events,
    rebuild_from_runs,
    record_shape_skip_event,
    record_shape_verdict_event,
    repeated_shape_skip_blocks,
    runs_dir,
)


def _emit(project_root: Path, issue_id: str, code: str, run: str, when: str, **extra) -> None:
    event = {
        "issue_id": issue_id,
        "reason_code": code,
        "severity": "blocking",
        "source": "local_check",
        "run_id": run,
        "emitted_at": when,
    }
    event.update(extra)
    record_shape_skip_event(project_root, event)


def test_missing_required_keys_raises(tmp_path: Path) -> None:
    with pytest.raises(SubstrateError):
        record_shape_skip_event(tmp_path, {"issue_id": "1"})
    with pytest.raises(SubstrateError):
        record_shape_skip_event(tmp_path, {"reason_code": "x"})


def test_prior_block_history_embedded_at_write_time(tmp_path: Path) -> None:
    _emit(tmp_path, "1135", "reopened_stale_contract", "r1", "2026-05-04T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r2", "2026-05-05T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r3", "2026-05-06T00:00:00Z")

    conn = create_or_open(tmp_path)
    try:
        events = list(iter_shape_skip_events(conn, issue_id="1135"))
    finally:
        conn.close()

    # Chronological order; each record embeds the count of prior blocks.
    assert [e["prior_block_count"] for e in events] == [0, 1, 2]
    assert events[-1]["first_blocked_at"] == "2026-05-04T00:00:00Z"
    assert events[-1]["last_blocked_at"] == "2026-05-05T00:00:00Z"


def test_advisories_do_not_count_as_prior_blocks(tmp_path: Path) -> None:
    record_shape_skip_event(
        tmp_path,
        {
            "issue_id": "42",
            "reason_code": "reopened_stale_contract",
            "severity": "advisory",
            "emitted_at": "2026-05-04T00:00:00Z",
        },
    )
    _emit(tmp_path, "42", "reopened_stale_contract", "r2", "2026-05-05T00:00:00Z")

    conn = create_or_open(tmp_path)
    try:
        blocking = [
            e for e in iter_shape_skip_events(conn, issue_id="42") if e["severity"] == "blocking"
        ]
    finally:
        conn.close()
    # The advisory row must not inflate the block count of the later block.
    assert blocking[0]["prior_block_count"] == 0


def test_date_range_and_code_query(tmp_path: Path) -> None:
    _emit(tmp_path, "1135", "reopened_stale_contract", "r1", "2026-05-04T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r2", "2026-05-06T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r3", "2026-05-09T00:00:00Z")
    _emit(tmp_path, "7", "needs_grooming_label", "r2", "2026-05-06T00:00:00Z")

    conn = create_or_open(tmp_path)
    try:
        rows = list(
            iter_shape_skip_events(
                conn,
                reason_code="reopened_stale_contract",
                since="2026-05-05T00:00:00Z",
                until="2026-05-07T00:00:00Z",
            )
        )
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["run_id"] == "r2"


def test_repeated_block_detection(tmp_path: Path) -> None:
    for i in range(4):
        _emit(tmp_path, "1135", "reopened_stale_contract", f"r{i}", f"2026-05-0{i + 4}T00:00:00Z")
    _emit(tmp_path, "7", "needs_grooming_label", "r1", "2026-05-05T00:00:00Z")

    conn = create_or_open(tmp_path)
    try:
        stuck = repeated_shape_skip_blocks(conn, threshold=3)
    finally:
        conn.close()

    assert len(stuck) == 1
    assert stuck[0]["issue_id"] == "1135"
    assert stuck[0]["reason_code"] == "reopened_stale_contract"
    assert stuck[0]["block_count"] == 4
    assert stuck[0]["run_ids"] == ["r0", "r1", "r2", "r3"]


def test_repeated_block_threshold_excludes_below(tmp_path: Path) -> None:
    _emit(tmp_path, "1135", "reopened_stale_contract", "r1", "2026-05-04T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r2", "2026-05-05T00:00:00Z")

    conn = create_or_open(tmp_path)
    try:
        stuck = repeated_shape_skip_blocks(conn, threshold=3)
    finally:
        conn.close()
    assert stuck == []


# ── last_status (AC1: "last unblocked-or-still-blocked status") ────────────────


def test_last_status_no_prior_block(tmp_path: Path) -> None:
    _emit(tmp_path, "1", "c", "r1", "2026-05-01T00:00:00Z")
    conn = create_or_open(tmp_path)
    try:
        events = list(iter_shape_skip_events(conn, issue_id="1"))
    finally:
        conn.close()
    assert events[0]["last_status"] == SKIP_STATUS_NO_PRIOR


def test_last_status_still_blocked_when_never_cleared(tmp_path: Path) -> None:
    _emit(tmp_path, "1", "c", "r1", "2026-05-01T00:00:00Z")
    _emit(tmp_path, "1", "c", "r2", "2026-05-02T00:00:00Z")
    conn = create_or_open(tmp_path)
    try:
        events = list(iter_shape_skip_events(conn, issue_id="1"))
    finally:
        conn.close()
    # No intervening RUNNABLE verdict — the second block is continuous.
    assert events[-1]["last_status"] == SKIP_STATUS_STILL_BLOCKED


def test_last_status_unblocked_after_runnable_verdict(tmp_path: Path) -> None:
    _emit(tmp_path, "1", "c", "r1", "2026-05-01T00:00:00Z")
    # The issue cleared the gate between blocks (a RUNNABLE shape verdict).
    record_shape_verdict_event(
        tmp_path, {"issue_id": "1", "verdict": "runnable", "emitted_at": "2026-05-02T00:00:00Z"}
    )
    _emit(tmp_path, "1", "c", "r3", "2026-05-03T00:00:00Z")
    conn = create_or_open(tmp_path)
    try:
        events = list(iter_shape_skip_events(conn, issue_id="1"))
    finally:
        conn.close()
    # Cleared then re-blocked — the #1135/#1405 pass/re-block shape.
    assert events[-1]["last_status"] == SKIP_STATUS_UNBLOCKED


def test_last_status_persisted_in_indexed_column(tmp_path: Path) -> None:
    _emit(tmp_path, "1", "c", "r1", "2026-05-01T00:00:00Z")
    _emit(tmp_path, "1", "c", "r2", "2026-05-02T00:00:00Z")
    conn = create_or_open(tmp_path)
    try:
        rows = conn.execute(
            "SELECT last_status FROM shape_skip_events "
            "WHERE issue_id = '1' ORDER BY emitted_at ASC"
        ).fetchall()
    finally:
        conn.close()
    # Both the indexed column and raw_json carry the status.
    assert [r[0] for r in rows] == [SKIP_STATUS_NO_PRIOR, SKIP_STATUS_STILL_BLOCKED]


# ── rebuild preservation (AC6: skip history is durable audit output) ──────────


def test_rebuild_preserves_shape_skip_events(tmp_path: Path) -> None:
    runs_dir(tmp_path).mkdir(parents=True, exist_ok=True)  # rebuild scans this dir
    _emit(tmp_path, "1135", "reopened_stale_contract", "r1", "2026-05-04T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r2", "2026-05-05T00:00:00Z")
    record_shape_verdict_event(
        tmp_path,
        {"issue_id": "1135", "verdict": "runnable", "emitted_at": "2026-05-06T00:00:00Z"},
    )
    _emit(tmp_path, "1135", "reopened_stale_contract", "r3", "2026-05-07T00:00:00Z")

    rebuild_from_runs(tmp_path)

    conn = create_or_open(tmp_path)
    try:
        events = list(iter_shape_skip_events(conn, issue_id="1135"))
        stuck = repeated_shape_skip_blocks(conn, threshold=3)
    finally:
        conn.close()

    # All three skip rows survive with their computed history/status intact.
    assert len(events) == 3
    assert [e["prior_block_count"] for e in events] == [0, 1, 2]
    assert [e["last_status"] for e in events] == [
        SKIP_STATUS_NO_PRIOR,
        SKIP_STATUS_STILL_BLOCKED,
        SKIP_STATUS_UNBLOCKED,
    ]
    # And the stuck-issue query still fires post-rebuild — the whole point.
    assert len(stuck) == 1
    assert stuck[0]["block_count"] == 3
