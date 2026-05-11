"""Tests for shape_events emission into the SQLite audit substrate."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from theforge.coordinator.audit_substrate import substrate_path
from theforge.coordinator.shape_audit import (
    VALID_INPUT_SOURCES,
    count_kept_as_draft,
    emit_shape_event,
)


def test_emit_shape_event_creates_row(tmp_path: Path):
    rowid = emit_shape_event(
        tmp_path,
        issue_number=1497,
        input_source="issue",
        classification="bug",
        confidence="high",
        ambiguity_question_count=0,
        apply_mutated=True,
        diagnosis_state="no-diagnosis",
    )
    assert rowid > 0
    db = substrate_path(tmp_path)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT issue_number, input_source, classification, confidence, "
            "ambiguity_question_count, apply_mutated, diagnosis_state "
            "FROM shape_events WHERE id = ?",
            (rowid,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (1497, "issue", "bug", "high", 0, 1, "no-diagnosis")


def test_emit_rejects_invalid_input_source(tmp_path: Path):
    with pytest.raises(ValueError):
        emit_shape_event(
            tmp_path,
            issue_number=None,
            input_source="email",
            classification="bug",
            confidence="high",
            ambiguity_question_count=0,
            apply_mutated=False,
        )


def test_count_kept_as_draft(tmp_path: Path):
    assert count_kept_as_draft(tmp_path) == 0
    emit_shape_event(
        tmp_path,
        issue_number=None,
        input_source="stdin",
        classification="unresolved",
        confidence="low",
        ambiguity_question_count=2,
        apply_mutated=False,
    )
    emit_shape_event(
        tmp_path,
        issue_number=None,
        input_source="stdin",
        classification="bug",
        confidence="high",
        ambiguity_question_count=0,
        apply_mutated=False,
    )
    assert count_kept_as_draft(tmp_path) == 1


def test_valid_input_sources_constant():
    assert VALID_INPUT_SOURCES == {"issue", "file", "stdin", "none"}
