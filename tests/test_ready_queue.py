"""Tests for the ready-label queue (mid-sprint workflow, issue #1512).

Covers type-label projection, stable rendering, milestone scoping passed through
to the gh listing, and the ``forge status --ready [--milestone]`` CLI surface.
The queue carries no ordering/priority semantics — it is simply the set of open,
``ready``-labeled issues, so tests assert eligibility surfacing, not ranking.
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


def _issue(number: int, title: str, labels: list[str]) -> dict:
    return {
        "number": number,
        "title": title,
        "labels": [{"name": name} for name in labels],
    }


# ── build_ready_queue ──────────────────────────────────────────────────────


def test_build_projects_type_label_from_issue_labels(tmp_path: Path) -> None:
    issues = [_issue(1512, "cut-rc.sh shim regression", ["ready", "bug"])]
    entries = build_ready_queue(tmp_path, fetch_issues=lambda: issues)
    assert entries == [ReadyEntry(1512, "cut-rc.sh shim regression", "bug")]


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
