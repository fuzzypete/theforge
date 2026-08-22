"""Tests for the ``forge report`` command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.cli.main import build_parser
from theforge.cli.report import cmd_report
from theforge.reporting.render import DEFAULT_MAX_CHUNKS
from theforge.reporting.target_gate import GateReason, TargetGateError, TargetGateVerdict

ISSUE_URL = "https://github.com/fuzzypete/theforge/issues/4242"

TARGET_VERDICT = TargetGateVerdict(
    repo="fuzzypete/theforge",
    ref="main",
    sha="1a2b3c4d5e6f7788",
    verdict="diagnosis_cause_unknown",
    shape="runnable",
    reasons=(
        GateReason(
            code="diagnosis_cause_unknown",
            severity="advisory",
            detail="no confirmed cause is asserted",
        ),
    ),
)


@pytest.fixture(autouse=True)
def stub_target_gate():
    """Every CLI test runs against a stubbed target gate.

    The real one shells out to ``gh`` and executes the target repository's own
    gate revision; tests that care about that path live in
    ``test_reporting_target_gate.py``.
    """
    with patch("theforge.cli.report.evaluate_target_gate", return_value=TARGET_VERDICT) as stub:
        yield stub


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _observing_project(tmp_path: Path, *, configuration: dict | None = None) -> Path:
    root = tmp_path / "hdp"
    forge = root / ".forge"
    _write(root / "forge.yaml", "project:\n  root: .\n")
    _write(
        root / ".git" / "config",
        '[remote "origin"]\n\turl = git@github.com:fuzzypete/hdp.git\n',
    )
    logs = forge / "logs" / "issues-320"
    _write(logs / "run-f5aa21cf2d8d.log", "sprint run log\n")
    _write(
        logs / "run-f5aa21cf2d8d-summary.yaml",
        yaml.safe_dump(
            {
                "sprint": {"name": "issues-320", "sprint_id": "5ff0"},
                "stories": [{"slug": "issue-320", "story_run_id": "aaa111"}],
            }
        ),
    )
    record: dict = {
        "run_id": "aaa111",
        "forge_version": "0.14.2",
        "sprint_name": "issues-320",
        "sprint_id": "5ff0",
        "task": {
            "slug": "issue-320",
            "story_text": "## Problem\n\nresume false-skips.\n",
            "github_issue": 320,
        },
        "cost": {"agents": []},
    }
    if configuration is not None:
        record["configuration"] = configuration
    _write(forge / "audits" / "runs" / "aaa111.json", json.dumps(record))
    _write(logs / "issue-320" / "audit.yaml", "story: issue-320\n")
    _write(logs / "issue-320" / "review-cycle-1" / "synthesized.yaml", "verdict: APPROVE\n")
    return root


def _args(root: Path, **overrides: object) -> argparse.Namespace:
    data: dict[str, object] = {
        "config": str(root / "forge.yaml"),
        "run": "aaa111",
        "to": "fuzzypete/theforge",
        "title": None,
        "description": "Sprint resume reported a story merged when no commit landed.",
        "description_file": None,
        "symptom": None,
        "cause": None,
        "code_path": None,
        "fix_criterion": None,
        "label": None,
        "max_comments": DEFAULT_MAX_CHUNKS,
        "dry_run": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _gh_success(*_args, **_kwargs):
    command = _args[0]
    if command[1:3] == ["issue", "create"]:
        return _proc(stdout=f"{ISSUE_URL}\n")
    return _proc()


def test_parser_registers_report_command():
    parser = build_parser()
    args = parser.parse_args(["report", "--run", "f5aa", "--to", "fuzzypete/theforge"])

    assert args.command == "report"
    assert args.run == "f5aa"
    assert args.to == "fuzzypete/theforge"
    assert args.dry_run is False


@patch("theforge.cli.report.subprocess.run")
def test_report_files_issue_and_attaches_evidence(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc123def456"})
    mock_run.side_effect = _gh_success

    rc = cmd_report(_args(root))

    assert rc == 0
    out = capsys.readouterr().out
    assert ISSUE_URL in out
    assert "shape gate    :" in out
    assert "publication   : complete" in out

    calls = [call.args[0] for call in mock_run.call_args_list]
    create = next(c for c in calls if c[1:3] == ["issue", "create"])
    assert "--repo" in create and "fuzzypete/theforge" in create
    assert "--label" in create and "bug" in create
    comments = [c for c in calls if c[1:3] == ["issue", "comment"]]
    assert comments, "evidence must be attached as comments"
    assert all("4242" in c for c in comments)
    edits = [c for c in calls if c[1:3] == ["issue", "edit"]]
    assert edits, "the body must be updated once publication finishes"


@patch("theforge.cli.report.subprocess.run")
def test_report_body_carries_recorded_config_not_the_readers(mock_run, tmp_path):
    root = _observing_project(
        tmp_path, configuration={"resolved_sha256": "recorded-digest", "source_path": "/obs.yaml"}
    )
    bodies: list[str] = []

    def capture(*call_args, **_kwargs):
        command = call_args[0]
        if "--body-file" in command:
            bodies.append(Path(command[command.index("--body-file") + 1]).read_text())
        return _gh_success(*call_args)

    mock_run.side_effect = capture

    assert cmd_report(_args(root)) == 0
    created = bodies[0]
    assert "forge version : 0.14.2" in created
    assert "observed in   : fuzzypete/hdp" in created
    assert "recorded-dig" in created
    payload = "\n".join(bodies)
    assert "/obs.yaml" in payload


@patch("theforge.cli.report.subprocess.run")
def test_report_names_missing_evidence_on_the_face_of_the_issue(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path, configuration=None)
    bodies: list[str] = []

    def capture(*call_args, **_kwargs):
        command = call_args[0]
        if command[1:3] == ["issue", "create"]:
            bodies.append(Path(command[command.index("--body-file") + 1]).read_text())
        return _gh_success(*call_args)

    mock_run.side_effect = capture

    assert cmd_report(_args(root)) == 0
    body = bodies[0]
    assert "### Missing evidence" in body
    assert "resolved configuration" in body
    assert "intake candidate artifacts" in body
    assert "config        : missing" in body
    assert "Evidence — resolved configuration" not in body
    assert "resolved configuration" in capsys.readouterr().out


@patch("theforge.cli.report.subprocess.run")
def test_unresolvable_target_gate_prevents_issue_creation(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    mock_run.side_effect = _gh_success

    with patch(
        "theforge.cli.report.evaluate_target_gate",
        side_effect=TargetGateError("cannot resolve fuzzypete/theforge's default branch"),
    ):
        rc = cmd_report(_args(root))

    assert rc == 1
    assert not mock_run.called
    captured = capsys.readouterr()
    assert "cannot be placed in fuzzypete/theforge's shape-gate state" in captured.out
    assert "cannot resolve fuzzypete/theforge's default branch" in captured.err


@patch("theforge.cli.report.subprocess.run")
def test_target_gate_that_cannot_be_executed_prevents_issue_creation(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    mock_run.side_effect = _gh_success

    with patch(
        "theforge.cli.report.evaluate_target_gate",
        side_effect=TargetGateError("the target repository's gate could not be executed"),
    ):
        rc = cmd_report(_args(root))

    assert rc == 1
    assert not mock_run.called
    assert "could not be executed" in capsys.readouterr().err


@patch("theforge.cli.report.subprocess.run")
def test_gate_is_evaluated_against_the_target_repositorys_own_revision(
    mock_run, tmp_path, capsys, stub_target_gate
):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    mock_run.side_effect = _gh_success

    assert cmd_report(_args(root, dry_run=True)) == 0

    kwargs = stub_target_gate.call_args.kwargs
    assert kwargs["repo"] == "fuzzypete/theforge"
    assert kwargs["labels"] == ["bug"]
    assert "## Diagnosis" in kwargs["body"]
    out = capsys.readouterr().out
    assert "shape gate    : diagnosis_cause_unknown" in out
    assert "target gate fuzzypete/theforge@1a2b3c4d5e6f (main)" in out
    assert "[advisory] diagnosis_cause_unknown" in out
    assert not mock_run.called


@patch("theforge.cli.report.subprocess.run")
def test_gated_body_is_the_body_that_gets_filed(mock_run, tmp_path, stub_target_gate):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    created: list[str] = []

    def capture(*call_args, **_kwargs):
        command = call_args[0]
        if command[1:3] == ["issue", "create"]:
            created.append(Path(command[command.index("--body-file") + 1]).read_text())
        return _gh_success(*call_args)

    mock_run.side_effect = capture

    assert cmd_report(_args(root)) == 0
    assert created[0] == stub_target_gate.call_args.kwargs["body"]


@patch("theforge.cli.report.subprocess.run")
def test_failed_comment_leaves_the_report_marked_incomplete(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    final_bodies: list[str] = []
    seen_comments = {"n": 0}

    def flaky(*call_args, **_kwargs):
        command = call_args[0]
        if command[1:3] == ["issue", "create"]:
            return _proc(stdout=f"{ISSUE_URL}\n")
        if command[1:3] == ["issue", "comment"]:
            seen_comments["n"] += 1
            if seen_comments["n"] == 2:
                return _proc(returncode=1, stderr="HTTP 502")
            return _proc()
        if command[1:3] == ["issue", "edit"]:
            final_bodies.append(Path(command[command.index("--body-file") + 1]).read_text())
        return _proc()

    mock_run.side_effect = flaky

    rc = cmd_report(_args(root))

    assert rc == 1
    assert seen_comments["n"] > 2, "a failed comment must not abort the remaining posts"
    assert final_bodies, "the body must be corrected to name what never landed"
    assert "INCOMPLETE" in final_bodies[-1]
    assert "NOT ATTACHED" in final_bodies[-1]
    err = capsys.readouterr().err
    assert "not attached:" in err


@patch("theforge.cli.report.subprocess.run")
def test_gh_issue_create_failure_returns_nonzero(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    mock_run.return_value = _proc(returncode=1, stderr="gh: not authenticated")

    assert cmd_report(_args(root)) == 1
    assert "not authenticated" in capsys.readouterr().err


@patch("theforge.cli.report.subprocess.run")
def test_unknown_run_id_fails_before_any_gh_call(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path)

    assert cmd_report(_args(root, run="nope")) == 1
    assert not mock_run.called
    assert "nope" in capsys.readouterr().err


@patch("theforge.cli.report.subprocess.run")
def test_bad_target_repo_is_rejected(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path)

    assert cmd_report(_args(root, to="not-a-repo")) == 1
    assert not mock_run.called
    assert "owner/repo" in capsys.readouterr().err


@patch("theforge.cli.report.subprocess.run")
def test_empty_description_is_rejected(mock_run, tmp_path, capsys):
    root = _observing_project(tmp_path)

    assert cmd_report(_args(root, description=None)) == 1
    assert not mock_run.called
    assert "--description" in capsys.readouterr().err
