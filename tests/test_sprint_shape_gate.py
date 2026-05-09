"""Tests for sprint-entry shape gate (needs-grooming + local re-check)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.sprint.shape_gate import (
    NEEDS_GROOMING_LABEL,
    REOPENED_STALE_CONTRACT_CODE,
    ShapeGateResult,
    SkippedIssue,
    _fetch_bot_reason_codes,
    apply_shape_gate,
    format_skipped_warning,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

_RUNNABLE_BODY = """## What

Add a CLI flag.

## Why

Users need a way to bypass the gate.

## Acceptance Criteria

- `forge sprint --force` runs every issue regardless of shape check
- warnings still list every skipped issue's reason codes
"""

_BAD_BODY = "just a one-liner, no acceptance criteria, no structure"


def _fake_detail(body: str, labels: list[str], title: str = "Some issue"):
    def _fetch(_number, _project_root):
        return {"title": title, "body": body, "labels": labels}

    return _fetch


# ── Label-skip path ─────────────────────────────────────────────────────────


def test_stale_bot_comment_does_not_affect_runnable_issue(tmp_path: Path) -> None:
    issues = [{"number": 42, "title": "Runnable issue"}]

    with patch(
        "theforge.sprint.shape_gate._fetch_bot_reason_codes",
        side_effect=AssertionError("bot comment must not be consulted for verdicts"),
    ):
        result = apply_shape_gate(
            issues,
            tmp_path,
            fetch_detail=_fake_detail(_RUNNABLE_BODY, ["enhancement"]),
        )

    assert result.runnable == issues
    assert result.skipped == []


def test_label_skip_uses_live_reason_details_when_issue_is_tracking_only(
    tmp_path: Path,
) -> None:
    issues = [{"number": 7, "title": "Stale label"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, [NEEDS_GROOMING_LABEL, "epic"]),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "label"
    assert entry.reason_codes == ("epic_or_tracking",)
    assert "Label 'epic' present" in entry.detail


def test_label_skip_falls_back_to_label_detail_when_live_check_is_runnable(
    tmp_path: Path,
) -> None:
    issues = [{"number": 8, "title": "Label only"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, [NEEDS_GROOMING_LABEL, "enhancement"]),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "label"
    assert entry.reason_codes == ("needs_grooming_label",)
    assert entry.detail == "issue carries 'needs-grooming' label"


# ── Local-check-skip path ───────────────────────────────────────────────────


def test_local_check_skip_catches_stale_label_loophole(tmp_path: Path) -> None:
    """Issue without needs-grooming but still malformed must be skipped."""
    issues = [{"number": 13, "title": "Edited but Action never ran"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_BAD_BODY, []),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "local_check"
    assert entry.reason_codes
    assert "No acceptance criteria section" in entry.detail


def test_local_check_allows_runnable_issue(tmp_path: Path) -> None:
    issues = [{"number": 99, "title": "Well-formed"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, ["enhancement"]),
    )

    assert result.runnable == issues
    assert result.skipped == []


def test_reopened_issue_with_stale_body_is_advisory_not_blocking(tmp_path: Path) -> None:
    """A reopened bug whose body predates the reopen event is no longer
    refused at sprint entry; the rule is downgraded to a non-blocking
    advisory because the underlying check verifies only timeline-event
    ordering, not body content."""
    issues = [{"number": 55, "title": "Reopened"}]

    def fetch(_number, _project_root):
        return {
            "title": "Reopened",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "The original close only covered half the work.",
                    "createdAt": "2026-05-02T12:30:00Z",
                }
            ],
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                }
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert result.runnable == issues
    assert result.skipped == []
    assert len(result.advisories) == 1
    entry = result.advisories[0]
    assert entry.reason_codes == (REOPENED_STALE_CONTRACT_CODE,)
    # Honest framing: no instruction to "reconcile the body before sprinting".
    assert "reconcile the body before sprinting" not in entry.detail
    assert "postdates the last body edit" in entry.detail


def test_reopened_issue_with_body_edit_after_reopen_is_runnable(tmp_path: Path) -> None:
    issues = [{"number": 56, "title": "Reconciled"}]

    def fetch(_number, _project_root):
        return {
            "title": "Reconciled",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "Need one more follow-up change.",
                    "createdAt": "2026-05-02T12:30:00Z",
                }
            ],
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                },
                {
                    "event": "edited",
                    "created_at": "2026-05-02T13:00:00Z",
                    "changes": {"body": {"from": "old body"}},
                },
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert result.runnable == issues
    assert result.skipped == []


# ── Force override ─────────────────────────────────────────────────────────


def test_force_override_returns_all_issues_runnable_but_keeps_skipped_list(
    tmp_path: Path,
) -> None:
    issues = [
        {"number": 1, "title": "Good"},
        {"number": 2, "title": "Flagged"},
    ]

    def fetch(number, _project_root):
        if number == 1:
            return {"title": "Good", "body": _RUNNABLE_BODY, "labels": ["enhancement"]}
        return {"title": "Flagged", "body": _RUNNABLE_BODY, "labels": [NEEDS_GROOMING_LABEL]}

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=fetch,
        force=True,
    )

    # Force: every input issue is runnable...
    assert result.runnable == issues
    # ...but the skipped list is still populated so the CLI can warn.
    assert len(result.skipped) == 1
    assert result.skipped[0].issue_number == 2
    assert result.skipped[0].source == "label"


def test_force_override_preserves_reopened_stale_contract_advisory(tmp_path: Path) -> None:
    """Under --force the reopen advisory remains visible (advisory channel),
    matching the prior force-mode behavior of preserving the warning while
    still running every issue."""
    issues = [{"number": 57, "title": "Reopened"}]

    def fetch(_number, _project_root):
        return {
            "title": "Reopened",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "Still missing the reopen follow-up.",
                    "createdAt": "2026-05-02T12:30:00Z",
                }
            ],
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                }
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch, force=True)

    assert result.runnable == issues
    assert len(result.advisories) == 1
    assert result.advisories[0].reason_codes == (REOPENED_STALE_CONTRACT_CODE,)


# ── Mixed sprint ───────────────────────────────────────────────────────────


def test_mixed_sprint_partitions_runnable_vs_skipped(tmp_path: Path) -> None:
    issues = [
        {"number": 1, "title": "runnable"},
        {"number": 2, "title": "stale label"},
        {"number": 3, "title": "malformed"},
    ]

    def fetch(number, _project_root):
        if number == 1:
            return {"title": "runnable", "body": _RUNNABLE_BODY, "labels": ["enhancement"]}
        if number == 2:
            return {
                "title": "stale label",
                "body": _RUNNABLE_BODY,
                "labels": [NEEDS_GROOMING_LABEL],
            }
        return {"title": "malformed", "body": _BAD_BODY, "labels": []}

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=fetch,
    )

    assert [i["number"] for i in result.runnable] == [1]
    sources = {s.issue_number: s.source for s in result.skipped}
    assert sources == {2: "label", 3: "local_check"}


# ── Fail-open on gh errors ─────────────────────────────────────────────────


def test_fetch_failure_leaves_issue_runnable(tmp_path: Path) -> None:
    """If gh returns nothing, do not invent a skip — leave it to sources.py."""
    issues = [{"number": 1, "title": "unknown"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=lambda _n, _r: None,
    )

    assert result.runnable == issues
    assert result.skipped == []


# ── Warning rendering ──────────────────────────────────────────────────────


def test_format_skipped_warning_lists_every_issue() -> None:
    skipped = [
        SkippedIssue(
            issue_number=7,
            reason_codes=("too_many_behavioral_clusters",),
            source="local_check",
            title="Too big",
            detail="Body covers multiple independent behaviors.",
        ),
        SkippedIssue(
            issue_number=12,
            reason_codes=("needs-grooming-label",),
            source="label",
            title="Stale",
            detail="issue carries 'needs-grooming' label",
        ),
    ]

    rendered = format_skipped_warning(skipped)
    assert "#7" in rendered
    assert "#12" in rendered
    assert "local_check" in rendered
    assert "label" in rendered
    assert "too_many_behavioral_clusters" in rendered
    assert "Body covers multiple independent behaviors." in rendered


def test_format_skipped_warning_empty_returns_empty_string() -> None:
    assert format_skipped_warning([]) == ""


# ── SkippedIssue.as_dict is machine-readable ───────────────────────────────


def test_skipped_issue_as_dict_is_machine_readable() -> None:
    entry = SkippedIssue(
        issue_number=5,
        reason_codes=("a", "b"),
        source="label",
        title="t",
    )
    data = entry.as_dict()
    assert data["issue_number"] == 5
    assert data["reason_codes"] == ["a", "b"]
    assert data["source"] == "label"


# ── Bot-comment parser (integration-ish) ───────────────────────────────────


def test_fetch_bot_reason_codes_parses_csv_marker(tmp_path: Path) -> None:
    stdout = (
        '{"comments":[{"body":"some text\\n'
        "<!-- shape-check-reasons: too_many_behavioral_clusters,missing_ac -->"
        '"}]}'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        codes = _fetch_bot_reason_codes(42, tmp_path)
    assert codes == ["too_many_behavioral_clusters", "missing_ac"]


def test_fetch_bot_reason_codes_parses_json_array(tmp_path: Path) -> None:
    stdout = '{"comments":[{"body":"<!-- shape-check-reasons: [\\"a\\", \\"b\\"] -->"}]}'
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        codes = _fetch_bot_reason_codes(42, tmp_path)
    assert codes == ["a", "b"]


def test_fetch_bot_reason_codes_returns_empty_on_gh_failure(tmp_path: Path) -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="fail")
        assert _fetch_bot_reason_codes(42, tmp_path) == []


# ── Sanity: result dataclass ───────────────────────────────────────────────


def test_shape_gate_result_defaults() -> None:
    r = ShapeGateResult()
    assert r.runnable == []
    assert r.skipped == []


# ── Classifier resolution ─────────────────────────────────────────────────


def test_resolve_classifier_honors_heuristic_and_off() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    assert _resolve_classifier("heuristic") == "heuristic"
    assert _resolve_classifier("off") == "off"


def test_resolve_classifier_falls_back_when_llm_has_no_caller() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    # "llm" mode with no llm_caller can't actually refine — fall back.
    assert _resolve_classifier("llm", llm_caller=None) == "heuristic"


def test_resolve_classifier_passes_through_llm_when_caller_available() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    assert _resolve_classifier("llm", llm_caller=lambda _b, _r: None) == "llm"


def test_resolve_classifier_falls_back_on_unknown_mode() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    assert _resolve_classifier("bogus-mode") == "heuristic"
