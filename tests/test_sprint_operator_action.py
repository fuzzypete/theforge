"""Tests for the operator-action intake refusal at sprint entry."""

from __future__ import annotations

from pathlib import Path

from theforge.sprint.shape_gate import (
    OPERATOR_ACTION_CODE,
    OPERATOR_ACTION_LABEL,
    OPERATOR_ACTION_LABEL_CONFLICT_CODE,
    OPERATOR_ACTION_MISSING_AC_CODE,
    apply_shape_gate,
    format_operator_action_notice,
)
from theforge.sprint.status_reader import _outcome_to_status
from theforge.sprint.story_state import StoryOutcome

_OPERATOR_ACTION_BODY = """## What

Run real validation sprints and capture authentic output.

## Why

Confidence in v0.11 audit substrate requires real-world dogfood evidence.

## Acceptance criteria

- operator runs three real sprints back-to-back and captures the audit YAML
- evidence is filed in the v0.11 retro doc
"""


def _fake_detail(body: str, labels: list[str], title: str = "Operator deliverable"):
    def _fetch(_number, _project_root):
        return {"title": title, "body": body, "labels": labels}

    return _fetch


# ── Happy path: deliberate non-dispatch ────────────────────────────────────


def test_operator_action_label_partitions_to_operator_action_list(tmp_path: Path) -> None:
    issues = [{"number": 1471, "title": "Validate v0.11 substrate"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL]),
    )

    assert result.runnable == []
    assert result.skipped == []
    assert len(result.operator_action) == 1
    entry = result.operator_action[0]
    assert entry.issue_number == 1471
    assert entry.reason_codes == (OPERATOR_ACTION_CODE,)
    assert entry.source == "label"
    assert "operator deliverable" in entry.detail


# ── Conflict: type-label mutual exclusion ──────────────────────────────────


def test_operator_action_with_bug_conflicts_lands_in_skipped(tmp_path: Path) -> None:
    issues = [{"number": 1, "title": "ambiguous"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL, "bug"]),
    )

    assert result.runnable == []
    assert result.operator_action == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.reason_codes == (OPERATOR_ACTION_LABEL_CONFLICT_CODE,)
    assert "bug" in entry.detail


def test_operator_action_with_enhancement_conflicts(tmp_path: Path) -> None:
    issues = [{"number": 2, "title": "ambiguous2"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL, "enhancement"]),
    )

    assert result.skipped[0].reason_codes == (OPERATOR_ACTION_LABEL_CONFLICT_CODE,)


def test_operator_action_with_epic_conflicts(tmp_path: Path) -> None:
    issues = [{"number": 3, "title": "tracking"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL, "epic"]),
    )

    assert result.skipped[0].reason_codes == (OPERATOR_ACTION_LABEL_CONFLICT_CODE,)


def test_operator_action_with_task_conflicts(tmp_path: Path) -> None:
    issues = [{"number": 4, "title": "ambiguous4"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL, "task"]),
    )

    assert result.skipped[0].reason_codes == (OPERATOR_ACTION_LABEL_CONFLICT_CODE,)


# ── AC requirement ────────────────────────────────────────────────────────


def test_operator_action_without_ac_section_is_refused_with_distinct_code(
    tmp_path: Path,
) -> None:
    body_no_ac = "## What\n\nDo something operator-shaped.\n\n## Why\n\nReasons.\n"
    issues = [{"number": 5, "title": "no AC"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(body_no_ac, [OPERATOR_ACTION_LABEL]),
    )

    assert result.operator_action == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.reason_codes == (OPERATOR_ACTION_MISSING_AC_CODE,)
    # Distinct from the generic missing_acceptance_criteria code:
    assert entry.reason_codes != ("missing_acceptance_criteria",)
    assert "Acceptance criteria" in entry.detail


# ── --force does not bypass operator-action ────────────────────────────────


def test_force_does_not_bypass_operator_action(tmp_path: Path) -> None:
    """The label is the operator's deliberate signal — --force still surfaces it
    in the operator_action list and excludes it from runnable."""
    issues = [{"number": 1471, "title": "deliberate"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL]),
        force=True,
    )

    assert result.runnable == []
    assert len(result.operator_action) == 1
    assert result.operator_action[0].reason_codes == (OPERATOR_ACTION_CODE,)


# ── Banner formatting ──────────────────────────────────────────────────────


def test_format_operator_action_notice_uses_distinct_phrasing(tmp_path: Path) -> None:
    issues = [{"number": 1471, "title": "Validate v0.11 substrate"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_OPERATOR_ACTION_BODY, [OPERATOR_ACTION_LABEL]),
    )
    rendered = format_operator_action_notice(result.operator_action)

    assert "deliberately non-dispatched" in rendered
    assert "operator-action" in rendered
    assert "#1471" in rendered
    assert "Validate v0.11 substrate" in rendered


def test_format_operator_action_notice_empty_returns_empty_string() -> None:
    assert format_operator_action_notice([]) == ""


# ── StoryOutcome additions ────────────────────────────────────────────────


def test_story_outcome_operator_action_is_terminal_and_skipped() -> None:
    assert StoryOutcome.OPERATOR_ACTION.is_terminal
    # Operator paid $0 and the system did not fail — operator-action belongs
    # in the skipped bucket, NOT the failed bucket.
    assert StoryOutcome.OPERATOR_ACTION.is_skipped
    assert not StoryOutcome.OPERATOR_ACTION.is_failed
    assert not StoryOutcome.OPERATOR_ACTION.is_succeeded


def test_outcome_to_status_maps_operator_action_to_distinct_status() -> None:
    # Distinct from the generic "skipped" string so display can show a row that
    # is not conflated with shape-gate skips.
    assert _outcome_to_status("OPERATOR_ACTION") == "operator-action"
    assert _outcome_to_status("SKIPPED") == "skipped"


# ── Mixed sprint partitioning ─────────────────────────────────────────────


def test_mixed_sprint_partitions_runnable_skipped_and_operator_action(
    tmp_path: Path,
) -> None:
    issues = [
        {"number": 100, "title": "runnable"},
        {"number": 101, "title": "operator"},
        {"number": 102, "title": "malformed"},
    ]

    def fetch(number, _project_root):
        if number == 100:
            return {
                "title": "runnable",
                "body": _OPERATOR_ACTION_BODY,
                "labels": ["enhancement"],
            }
        if number == 101:
            return {
                "title": "operator",
                "body": _OPERATOR_ACTION_BODY,
                "labels": [OPERATOR_ACTION_LABEL],
            }
        return {"title": "malformed", "body": "no structure", "labels": []}

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert [i["number"] for i in result.runnable] == [100]
    assert [s.issue_number for s in result.operator_action] == [101]
    assert [s.issue_number for s in result.skipped] == [102]
