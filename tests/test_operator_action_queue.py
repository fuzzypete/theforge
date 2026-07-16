"""Tests for the operator-action readiness queue (epic #1469, slice 3).

Covers dependency parsing, ready/blocked classification, pending counts, stable
rendering, and the ``forge status --operator-actions`` CLI surface.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.operator_action_queue import (
    DependencyState,
    OperatorActionEntry,
    build_operator_action_queue,
    format_operator_action_queue,
)

_BODY_WITH_DEPS = """---
depends_on:
  - issue-1326
  - issue-1437
---

## What

Validate the v0.11 substrate.
"""

_BODY_PROSE_DEP = "## What\n\nCut the release. Depends on #1450.\n"

_BODY_TWO_DEPS = """---
depends_on:
  - issue-1450
  - issue-1469
---

## What

Cut the release once router work merges.
"""

_BODY_NO_DEPS = "## What\n\nCoordinate with the vendor on quota.\n"


def _issue(number: int, title: str, body: str) -> dict:
    return {"number": number, "title": title, "body": body}


# ── build_operator_action_queue ───────────────────────────────────────────


def test_ready_when_all_dependencies_closed(tmp_path: Path) -> None:
    entries = build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [_issue(1471, "Validate substrate", _BODY_WITH_DEPS)],
        fetch_states=lambda nums: {1326: True, 1437: True},
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.ready is True
    assert entry.pending_count == 0
    assert [dep.number for dep in entry.dependencies] == [1326, 1437]


def test_blocked_when_any_dependency_open(tmp_path: Path) -> None:
    entries = build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [_issue(1480, "Cut rc1", _BODY_TWO_DEPS)],
        fetch_states=lambda nums: {1450: False, 1469: False},
    )

    entry = entries[0]
    assert entry.ready is False
    assert entry.pending_count == 2
    assert {dep.number for dep in entry.pending} == {1450, 1469}


def test_blocked_from_prose_depends_on(tmp_path: Path) -> None:
    """Prose 'Depends on #N' declarations feed readiness too."""
    entries = build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [_issue(1480, "Cut rc1", _BODY_PROSE_DEP)],
        fetch_states=lambda nums: {1450: False},
    )

    entry = entries[0]
    assert [dep.number for dep in entry.dependencies] == [1450]
    assert entry.ready is False
    assert entry.pending_count == 1


def test_blocked_counts_only_open_dependencies(tmp_path: Path) -> None:
    entries = build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [_issue(1480, "Cut rc1", _BODY_WITH_DEPS)],
        fetch_states=lambda nums: {1326: True, 1437: False},
    )

    entry = entries[0]
    assert entry.ready is False
    assert entry.pending_count == 1
    assert [dep.number for dep in entry.pending] == [1437]


def test_no_dependencies_is_ready_by_default(tmp_path: Path) -> None:
    entries = build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [_issue(1495, "Vendor quota", _BODY_NO_DEPS)],
        fetch_states=lambda nums: {},
    )

    entry = entries[0]
    assert entry.dependencies == ()
    assert entry.ready is True
    assert entry.pending_count == 0


def test_unverifiable_dependency_state_defaults_to_blocked(tmp_path: Path) -> None:
    """A dependency whose state could not be resolved must never be reported as
    landed — the operator-action stays blocked rather than falsely ready."""
    entries = build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [_issue(1480, "Cut rc1", _BODY_WITH_DEPS)],
        fetch_states=lambda nums: {},  # states unknown
    )

    entry = entries[0]
    assert entry.ready is False
    assert entry.pending_count == 2


def test_entries_sorted_by_issue_number(tmp_path: Path) -> None:
    entries = build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [
            _issue(1495, "Vendor quota", _BODY_NO_DEPS),
            _issue(1471, "Validate substrate", _BODY_WITH_DEPS),
        ],
        fetch_states=lambda nums: {1326: True, 1437: True},
    )

    assert [entry.issue_number for entry in entries] == [1471, 1495]


def test_dependency_numbers_deduped_across_issues(tmp_path: Path) -> None:
    """A dependency shared by two operator-actions is resolved once."""
    calls: list[list[int]] = []

    def fetch_states(nums: list[int]) -> dict[int, bool]:
        calls.append(nums)
        return {n: True for n in nums}

    build_operator_action_queue(
        tmp_path,
        fetch_issues=lambda: [
            _issue(1471, "A", "Depends on #1326.\n"),
            _issue(1480, "B", "Depends on #1326.\n"),
        ],
        fetch_states=fetch_states,
    )

    assert calls == [[1326]]


# ── format_operator_action_queue ──────────────────────────────────────────


def test_format_empty_queue() -> None:
    assert format_operator_action_queue([]) == "Operator-action queue: none."


def test_format_ready_and_blocked_entries() -> None:
    entries = [
        OperatorActionEntry(
            1471,
            "Validate substrate",
            (DependencyState(1326, True), DependencyState(1437, True)),
        ),
        OperatorActionEntry(
            1480,
            "Cut rc1",
            (DependencyState(1450, False), DependencyState(1469, False)),
        ),
        OperatorActionEntry(1495, "Vendor quota", ()),
    ]

    rendered = format_operator_action_queue(entries)

    assert "Operator-action queue (3 issues):" in rendered
    assert "ready" in rendered
    assert "blocked" in rendered
    # Blocked entry names its pending deps and counts them.
    assert "#1450 open" in rendered
    assert "2 pending" in rendered
    # Ready-with-deps entry shows landed deps but no pending count.
    assert "#1326 ok" in rendered
    # No-deps entry is ready and labeled.
    assert "(no deps)" in rendered
    assert "#1471" in rendered and "Validate substrate" in rendered


def test_format_single_issue_uses_singular_noun() -> None:
    rendered = format_operator_action_queue([OperatorActionEntry(1495, "Vendor quota", ())])
    assert "(1 issue):" in rendered


# ── CLI: forge status --operator-actions ──────────────────────────────────


def _make_forge_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=[],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


def test_cmd_status_operator_actions_flag_renders_queue(tmp_path: Path, capsys: object) -> None:
    from theforge.cli import cmd_status

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n")
    config = _make_forge_config(tmp_path)
    args = argparse.Namespace(operator_actions=True)

    entries = [
        OperatorActionEntry(1471, "Validate substrate", (DependencyState(1326, True),)),
        OperatorActionEntry(1480, "Cut rc1", (DependencyState(1450, False),)),
    ]

    with (
        patch("theforge.cli.status._find_config", return_value=forge_yaml),
        patch("theforge.cli.status.load_config", return_value=config),
        patch(
            "theforge.operator_action_queue.build_operator_action_queue",
            return_value=entries,
        ),
    ):
        result = cmd_status(args)

    assert result == 0
    captured = capsys.readouterr()
    assert "Operator-action queue (2 issues):" in captured.out
    assert "ready" in captured.out
    assert "blocked" in captured.out
    assert "#1471" in captured.out
    assert "#1480" in captured.out
