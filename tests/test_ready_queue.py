"""Tests for the ready-label queue (mid-sprint workflow, issue #1512).

Covers type-label projection, stable rendering, milestone scoping passed through
to the gh listing, and the ``forge status --ready [--milestone]`` CLI surface.
The queue carries no ordering/priority semantics — it is simply the set of open,
``ready``-labeled issues, so tests assert eligibility surfacing, not ranking.

Also covers the shape-gate agreement property (#2027): an issue the sprint gate
would refuse must not be presented as sprint-ready by this listing.
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
from theforge.ready_queue import (
    ReadyEntry,
    build_ready_queue,
    format_ready_queue,
)

# An enhancement body the shape gate admits: type label, acceptance criteria,
# and a concrete example.
_RUNNABLE_BODY = """## What

Add a CLI flag.

## Why

Users need a way to bypass the gate.

## Example

    $ forge sprint --force
    [forge] 2 issue(s) flagged by shape gate

## Acceptance Criteria

- `forge sprint --force` runs every issue regardless of shape check
- warnings still list every skipped issue's reason codes
"""

# A bug filing with no `## Diagnosis` section — the exact shape the gate refuses
# with a BLOCKING `needs_diagnosis` reason (#1983-#1987 in the story).
_UNDIAGNOSED_BUG_BODY = """## Observed behavior

`forge status --ready` lists issues the gate refuses.

## Expected behavior

The listing agrees with the gate.
"""


def _issue(number: int, title: str, labels: list[str], body: str = _RUNNABLE_BODY) -> dict:
    return {
        "number": number,
        "title": title,
        "labels": [{"name": name} for name in labels],
        "body": body,
    }


# ── build_ready_queue ──────────────────────────────────────────────────────


def test_build_projects_type_label_from_issue_labels(tmp_path: Path) -> None:
    issues = [_issue(1512, "cut-rc.sh shim regression", ["ready", "enhancement"])]
    entries = build_ready_queue(tmp_path, fetch_issues=lambda: issues)
    assert [(e.issue_number, e.title, e.type_label) for e in entries] == [
        (1512, "cut-rc.sh shim regression", "enhancement")
    ]


def test_build_falls_back_to_dash_when_no_type_label(tmp_path: Path) -> None:
    issues = [_issue(1600, "untyped but ready", ["ready"])]
    entries = build_ready_queue(tmp_path, fetch_issues=lambda: issues)
    assert entries[0].type_label == "—"


def test_build_sorts_entries_by_issue_number(tmp_path: Path) -> None:
    issues = [
        _issue(1512, "later", ["ready", "bug"]),
        _issue(1487, "earlier", ["ready", "bug"]),
    ]
    entries = build_ready_queue(tmp_path, fetch_issues=lambda: issues)
    assert [e.issue_number for e in entries] == [1487, 1512]


def test_build_empty_when_no_ready_issues(tmp_path: Path) -> None:
    assert build_ready_queue(tmp_path, fetch_issues=lambda: []) == []


def test_build_prefers_first_known_type_when_multiple(tmp_path: Path) -> None:
    # An issue tagged both bug and enhancement reports the higher-priority type.
    issues = [_issue(1700, "multi", ["ready", "enhancement", "bug"])]
    entries = build_ready_queue(tmp_path, fetch_issues=lambda: issues)
    assert entries[0].type_label == "bug"


# ── shape-gate agreement (#2027) ───────────────────────────────────────────


def test_build_marks_admissible_issue_ready(tmp_path: Path) -> None:
    issues = [_issue(1512, "add a flag", ["ready", "enhancement"])]
    entry = build_ready_queue(tmp_path, fetch_issues=lambda: issues)[0]
    assert entry.admissible is True
    assert entry.verdict == "runnable"


def test_build_marks_undiagnosed_bug_not_admissible(tmp_path: Path) -> None:
    issues = [_issue(1983, "counter is wrong", ["ready", "bug"], _UNDIAGNOSED_BUG_BODY)]
    entry = build_ready_queue(tmp_path, fetch_issues=lambda: issues)[0]
    assert entry.admissible is False
    assert entry.verdict == "needs_diagnosis"
    assert entry.detail


def test_build_marks_needs_grooming_labeled_issue_not_admissible(tmp_path: Path) -> None:
    issues = [_issue(1900, "well shaped but flagged", ["ready", "enhancement", "needs-grooming"])]
    entry = build_ready_queue(tmp_path, fetch_issues=lambda: issues)[0]
    assert entry.admissible is False


def test_build_marks_operator_action_issue_not_admissible(tmp_path: Path) -> None:
    # The gate deliberately never dispatches operator-action issues, so the
    # ready listing must not advertise them as sprint-eligible either.
    issues = [_issue(1901, "rotate the token", ["ready", "operator-action"])]
    entry = build_ready_queue(tmp_path, fetch_issues=lambda: issues)[0]
    assert entry.admissible is False


def test_build_agrees_with_shape_gate_on_the_same_issues(tmp_path: Path) -> None:
    """Seam test: the queue's verdict matches what apply_shape_gate decides.

    This is the property the bug violated — two surfaces answering "may this
    issue enter a sprint?" from different data.
    """
    from theforge.sprint.shape_gate import apply_shape_gate

    issues = [
        _issue(1983, "counter is wrong", ["ready", "bug"], _UNDIAGNOSED_BUG_BODY),
        _issue(1512, "add a flag", ["ready", "enhancement"]),
        _issue(1901, "rotate the token", ["ready", "operator-action"]),
    ]
    by_number = {issue["number"]: issue for issue in issues}

    def _fetch_detail(number: int, _project_root):  # noqa: ANN001
        issue = by_number[number]
        return {
            "title": issue["title"],
            "body": issue["body"],
            "labels": [lbl["name"] for lbl in issue["labels"]],
        }

    gate = apply_shape_gate(
        [{"number": i["number"], "title": i["title"]} for i in issues],
        tmp_path,
        fetch_detail=_fetch_detail,
    )
    gate_runnable = {int(item["number"]) for item in gate.runnable}

    queue_admissible = {
        entry.issue_number
        for entry in build_ready_queue(tmp_path, fetch_issues=lambda: issues)
        if entry.admissible
    }

    assert queue_admissible == gate_runnable == {1512}


# ── gh listing wiring: milestone scoping ───────────────────────────────────


def test_gh_listing_passes_milestone_flag(tmp_path: Path) -> None:
    from theforge import ready_queue

    captured_cmd: list[str] = []

    class _Proc:
        returncode = 0
        stdout = "[]"

    def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured_cmd.extend(cmd)
        return _Proc()

    with patch.object(ready_queue.subprocess, "run", _fake_run):
        ready_queue._gh_list_ready_issues(tmp_path, "v0.10.0")

    assert "--label" in captured_cmd and "ready" in captured_cmd
    assert "--milestone" in captured_cmd
    assert "v0.10.0" in captured_cmd
    # The body is required to evaluate the shape gate's verdict (#2027).
    json_fields = captured_cmd[captured_cmd.index("--json") + 1].split(",")
    assert "body" in json_fields


def test_gh_listing_omits_milestone_when_none(tmp_path: Path) -> None:
    from theforge import ready_queue

    captured_cmd: list[str] = []

    class _Proc:
        returncode = 0
        stdout = "[]"

    def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured_cmd.extend(cmd)
        return _Proc()

    with patch.object(ready_queue.subprocess, "run", _fake_run):
        ready_queue._gh_list_ready_issues(tmp_path, None)

    assert "--milestone" not in captured_cmd


def test_gh_listing_degrades_to_empty_on_failure(tmp_path: Path) -> None:
    from theforge import ready_queue

    class _Proc:
        returncode = 1
        stdout = ""

    with patch.object(ready_queue.subprocess, "run", lambda *a, **k: _Proc()):
        assert ready_queue._gh_list_ready_issues(tmp_path, None) == []


# ── format_ready_queue ─────────────────────────────────────────────────────


def test_format_empty_queue() -> None:
    assert format_ready_queue([]) == "Ready for next sprint: none."


def test_format_empty_queue_names_milestone() -> None:
    assert format_ready_queue([], milestone="v0.10.0") == (
        "Ready for next sprint in v0.10.0: none."
    )


def test_format_lists_ready_entries_with_stable_columns() -> None:
    entries = [
        ReadyEntry(1487, "status --watch blank during preflight", "bug"),
        ReadyEntry(1512, "cut-rc.sh shim wrapper regression", "bug"),
    ]
    rendered = format_ready_queue(entries, milestone="v0.10.0")

    assert "Ready for next sprint in v0.10.0 (2 issues):" in rendered
    assert "#1487" in rendered and "#1512" in rendered
    assert "bug" in rendered
    assert "ready" in rendered
    assert "cut-rc.sh shim wrapper regression" in rendered


def test_format_single_issue_uses_singular_noun() -> None:
    rendered = format_ready_queue([ReadyEntry(1512, "one", "bug")])
    assert "(1 issue):" in rendered


def test_format_shows_blocking_verdict_instead_of_ready_marker() -> None:
    entries = [
        ReadyEntry(1487, "admissible", "enhancement"),
        ReadyEntry(
            1983,
            "counter is wrong",
            "bug",
            admissible=False,
            verdict="needs_diagnosis",
            detail="Bug has no Diagnosis section",
        ),
    ]
    rendered = format_ready_queue(entries, milestone="v0.13.0")

    assert "1 blocked by shape gate" in rendered
    assert "BLOCKED:needs_diagnosis" in rendered
    # The blocked row must not be presented with the ready marker.
    blocked_row = next(line for line in rendered.splitlines() if "#1983" in line)
    assert "  ready  " not in blocked_row
    assert "would be refused at sprint entry" in rendered
    assert "Bug has no Diagnosis section" in rendered
    assert "forge groom" in rendered


def test_format_bounds_long_refusal_detail_to_one_line() -> None:
    # Shape-check details embed full remediation text; unabridged they bury the
    # listing. The row stays one line and points at the full verdict.
    entries = [
        ReadyEntry(
            1983,
            "counter is wrong",
            "bug",
            admissible=False,
            verdict="needs_diagnosis",
            detail="Bug has no Diagnosis section.\n" + ("remediation prose " * 40),
        )
    ]
    rendered = format_ready_queue(entries)
    detail_row = next(line for line in rendered.splitlines() if "needs_diagnosis:" in line)

    assert len(detail_row) < 200
    assert detail_row.endswith("…")
    assert "forge shape <n>" in rendered


def test_format_omits_blocked_section_when_all_admissible() -> None:
    rendered = format_ready_queue([ReadyEntry(1487, "fine", "enhancement")])
    assert "blocked by shape gate" not in rendered
    assert "would be refused at sprint entry" not in rendered


# ── CLI: forge status --ready ──────────────────────────────────────────────


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


def test_cmd_status_ready_flag_renders_queue(tmp_path: Path, capsys: object) -> None:
    from theforge.cli import cmd_status

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n")
    config = _make_forge_config(tmp_path)
    args = argparse.Namespace(ready=True, milestone="v0.10.0")

    entries = [
        ReadyEntry(1487, "status --watch blank during preflight", "bug"),
        ReadyEntry(1512, "cut-rc.sh shim wrapper regression", "bug"),
    ]

    with (
        patch("theforge.cli.status._find_config", return_value=forge_yaml),
        patch("theforge.cli.status.load_config", return_value=config),
        patch("theforge.ready_queue.build_ready_queue", return_value=entries),
    ):
        result = cmd_status(args)

    assert result == 0
    captured = capsys.readouterr()
    assert "Ready for next sprint in v0.10.0 (2 issues):" in captured.out
    assert "#1487" in captured.out
    assert "#1512" in captured.out


def test_cmd_status_ready_flag_passes_milestone_through(tmp_path: Path) -> None:
    from theforge.cli import cmd_status

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n")
    config = _make_forge_config(tmp_path)
    args = argparse.Namespace(ready=True, milestone="v0.11.0")

    seen: dict[str, object] = {}

    def _fake_build(project_root, *, milestone=None, fetch_issues=None):  # noqa: ANN001, ANN003
        seen["milestone"] = milestone
        return []

    with (
        patch("theforge.cli.status._find_config", return_value=forge_yaml),
        patch("theforge.cli.status.load_config", return_value=config),
        patch("theforge.ready_queue.build_ready_queue", _fake_build),
    ):
        result = cmd_status(args)

    assert result == 0
    assert seen["milestone"] == "v0.11.0"
