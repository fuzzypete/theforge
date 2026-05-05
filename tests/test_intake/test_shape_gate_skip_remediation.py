"""Tests for body-only remediation of shape-gate skip reasons.

Covers ``reopened_stale_contract``: the gate flags an issue because the body
hasn't been edited since the reopen-context comment landed, and intake
auto-edit folds that comment material into the body so the next gate run
clears the flag.
"""

from __future__ import annotations

from pathlib import Path

from theforge.intake import (
    BODY_FIXABLE_SKIP_REASONS,
    ShapeGateSkipRemediationKind,
    remediate_shape_gate_skip,
)


def _detail_with_reopen_comment() -> dict:
    return {
        "title": "Reopened",
        "body": "## What\n\nDo a thing.\n",
        "labels": ["bug"],
        "state": "OPEN",
        "lastEditedAt": None,
        "comments": [
            {
                "author": {"login": "operator"},
                "body": "Reopen with new diagnosis material.",
                "createdAt": "2026-05-02T14:35:00Z",
            }
        ],
        "timeline": [
            {
                "event": "reopened",
                "created_at": "2026-05-02T14:00:00Z",
                "actor": {"login": "operator"},
            }
        ],
    }


def test_body_fixable_set_includes_reopened_stale_contract() -> None:
    assert "reopened_stale_contract" in BODY_FIXABLE_SKIP_REASONS


def test_remediate_reopened_stale_contract_edits_body(tmp_path: Path) -> None:
    edits: list[tuple] = []

    def fake_edit(number, body, root):
        edits.append((number, body, root))
        return True

    result = remediate_shape_gate_skip(
        issue_number=1135,
        reason_codes=("reopened_stale_contract",),
        project_root=tmp_path,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        fetch_detail=lambda _n, _r: _detail_with_reopen_comment(),
        edit_body=fake_edit,
    )

    assert result.kind is ShapeGateSkipRemediationKind.REMEDIATED
    assert len(edits) == 1
    new_body = edits[0][1]
    assert "## Reopen Context" in new_body
    assert "Reopen with new diagnosis material." in new_body


def test_remediate_declines_when_auto_fix_disabled(tmp_path: Path) -> None:
    result = remediate_shape_gate_skip(
        issue_number=1135,
        reason_codes=("reopened_stale_contract",),
        project_root=tmp_path,
        auto_fix_enabled=False,
        auto_fix_mode="edit",
        fetch_detail=lambda _n, _r: _detail_with_reopen_comment(),
        edit_body=lambda *_: True,
    )

    assert result.kind is ShapeGateSkipRemediationKind.DROPPED_DISABLED


def test_remediate_declines_in_comment_mode(tmp_path: Path) -> None:
    """Body remediation requires edit mode; comment mode posts no body change."""
    result = remediate_shape_gate_skip(
        issue_number=1135,
        reason_codes=("reopened_stale_contract",),
        project_root=tmp_path,
        auto_fix_enabled=True,
        auto_fix_mode="comment",
        fetch_detail=lambda _n, _r: _detail_with_reopen_comment(),
        edit_body=lambda *_: True,
    )

    assert result.kind is ShapeGateSkipRemediationKind.DROPPED_DISABLED


def test_remediate_skips_unknown_reason(tmp_path: Path) -> None:
    """Reasons not in BODY_FIXABLE_SKIP_REASONS are declined explicitly."""
    result = remediate_shape_gate_skip(
        issue_number=42,
        reason_codes=("missing_acceptance_criteria",),
        project_root=tmp_path,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        fetch_detail=lambda _n, _r: _detail_with_reopen_comment(),
        edit_body=lambda *_: True,
    )

    assert result.kind is ShapeGateSkipRemediationKind.DROPPED_NOT_COVERED


def test_remediate_handles_fetch_failure(tmp_path: Path) -> None:
    result = remediate_shape_gate_skip(
        issue_number=1135,
        reason_codes=("reopened_stale_contract",),
        project_root=tmp_path,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        fetch_detail=lambda _n, _r: None,
        edit_body=lambda *_: True,
    )

    assert result.kind is ShapeGateSkipRemediationKind.DROPPED_FETCH


def test_remediate_handles_edit_failure(tmp_path: Path) -> None:
    result = remediate_shape_gate_skip(
        issue_number=1135,
        reason_codes=("reopened_stale_contract",),
        project_root=tmp_path,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        fetch_detail=lambda _n, _r: _detail_with_reopen_comment(),
        edit_body=lambda *_: False,
    )

    assert result.kind is ShapeGateSkipRemediationKind.DROPPED_EDIT


def test_remediate_drops_when_no_reopen_context_on_refetch(tmp_path: Path) -> None:
    """If the issue no longer has a reopen comment, drop after fix.

    Could happen if the operator already reconciled between shape gate and
    remediation passes, or if the issue isn't actually reopened.
    """
    detail_no_reopen = {
        "title": "No reopen",
        "body": "## What\n\nDo a thing.\n",
        "labels": ["bug"],
        "state": "OPEN",
        "lastEditedAt": None,
        "comments": [],
        "timeline": [],
    }

    result = remediate_shape_gate_skip(
        issue_number=1135,
        reason_codes=("reopened_stale_contract",),
        project_root=tmp_path,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        fetch_detail=lambda _n, _r: detail_no_reopen,
        edit_body=lambda *_: True,
    )

    assert result.kind is ShapeGateSkipRemediationKind.DROPPED_AFTER_FIX
