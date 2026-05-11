"""Tests for the `forge shape` CLI command."""

from __future__ import annotations

import argparse
import json
import sqlite3
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.main import build_parser
from theforge.cli.shape import cmd_shape
from theforge.coordinator.audit_substrate import substrate_path


def _make_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    data: dict[str, object] = {
        "config": str(forge_yaml),
        "issue": None,
        "from_brief": None,
        "from_stdin": False,
        "apply": False,
        "next": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_parser_registers_shape_command():
    parser = build_parser()
    args = parser.parse_args(["shape", "1497"])
    assert args.command == "shape"
    assert args.issue == 1497
    assert args.apply is False
    assert args.next is False


def test_parser_accepts_flags():
    parser = build_parser()
    args = parser.parse_args(["shape", "1497", "--apply", "--next"])
    assert args.apply is True
    assert args.next is True


def test_shape_from_brief_classifies_and_emits_audit(tmp_path, capsys):
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# add forge shape command\n## Acceptance criteria\n- works\n",
        encoding="utf-8",
    )
    args = _make_args(tmp_path, from_brief=str(brief))
    rc = cmd_shape(args)

    captured = capsys.readouterr()
    assert "Proposed classification: enhancement" in captured.out
    assert "Next: forge groom" in captured.out
    # Without --apply, command exits non-zero when restructure is proposed.
    # Enhancement already has AC, but proposed_labels=enhancement triggers change.
    assert rc == 1

    # Audit row exists.
    db = substrate_path(tmp_path)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT input_source, classification, apply_mutated FROM shape_events"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("file", "enhancement", 0)


def test_shape_from_stdin_handles_unresolved(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", StringIO("# ???\n\n"))
    args = _make_args(tmp_path, from_stdin=True)
    rc = cmd_shape(args)
    out = capsys.readouterr().out
    assert "Kept as: todo:draft" in out
    assert "Ambiguity questions:" in out
    assert rc == 1


def test_shape_next_prints_only_command(tmp_path, capsys):
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# bug: thing is broken\n\n## Observed\nfoo\n",
        encoding="utf-8",
    )
    args = _make_args(tmp_path, from_brief=str(brief), next=True)
    rc = cmd_shape(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip().startswith("Next:")
    # Audit still emitted.
    db = substrate_path(tmp_path)
    assert db.exists()


def test_shape_apply_requires_issue_number(tmp_path, capsys):
    brief = tmp_path / "brief.md"
    brief.write_text("# bug: broken\n## Observed\nfoo\n", encoding="utf-8")
    args = _make_args(tmp_path, from_brief=str(brief), apply=True)
    rc = cmd_shape(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--apply requires an issue number" in err


def test_shape_apply_refused_for_unresolved(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", StringIO(""))
    args = _make_args(tmp_path, from_stdin=True, apply=True, issue=42)
    # Make _load_issue fail by not stubbing it — we won't reach gh because
    # classify will declare unresolved and refuse before gh runs. But we are
    # using from_stdin so the issue arg is unused.
    rc = cmd_shape(args)
    err = capsys.readouterr().err
    assert rc == 1
    assert "--apply refused" in err


@patch("theforge.cli.shape.subprocess.run")
def test_shape_issue_calls_gh_and_applies(mock_run, tmp_path, capsys):
    gh_view = _proc(
        stdout=json.dumps(
            {
                "title": "cut-rc.sh broke something",
                "body": "happens on every release",
                "labels": [{"name": "bug"}, {"name": "todo:draft"}],
            }
        )
    )
    gh_edit = _proc(stdout="ok")
    # Order: view, then add-label(s), remove-label(s), body-file.
    mock_run.side_effect = [gh_view, gh_edit, gh_edit, gh_edit]
    args = _make_args(tmp_path, issue=1497, apply=True)
    rc = cmd_shape(args)
    out = capsys.readouterr().out
    assert "Proposed classification: bug" in out
    assert "Applied proposal to #1497." in out
    assert rc == 0

    db = substrate_path(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT input_source, classification, apply_mutated, diagnosis_state FROM shape_events"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("issue", "bug", 1, "no-diagnosis")


def test_shape_no_input_errors(tmp_path, capsys):
    args = _make_args(tmp_path)
    rc = cmd_shape(args)
    err = capsys.readouterr().err
    assert rc == 1
    assert "provide an issue number" in err


def _shape_event_row(tmp_path: Path) -> tuple:
    """Helper: return the (input_source, classification, apply_mutated)
    of the single shape_events row written under tmp_path, or None if none."""
    db = substrate_path(tmp_path)
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT input_source, classification, apply_mutated FROM shape_events"
        ).fetchone()
    finally:
        conn.close()


def test_audit_emitted_when_from_brief_file_missing(tmp_path, capsys):
    args = _make_args(tmp_path, from_brief=str(tmp_path / "missing.md"))
    rc = cmd_shape(args)
    assert rc == 1
    assert "file not found" in capsys.readouterr().err
    # Acceptance criterion: every invocation writes a structured event.
    assert _shape_event_row(tmp_path) == ("file", "unresolved", 0)


def test_audit_emitted_when_no_input_given(tmp_path, capsys):
    args = _make_args(tmp_path)
    rc = cmd_shape(args)
    assert rc == 1
    assert _shape_event_row(tmp_path) == ("none", "unresolved", 0)


@patch("theforge.cli.shape.subprocess.run")
def test_audit_emitted_when_gh_issue_view_fails(mock_run, tmp_path, capsys):
    mock_run.return_value = _proc(returncode=1, stderr="no such issue")
    args = _make_args(tmp_path, issue=999999)
    rc = cmd_shape(args)
    assert rc == 1
    assert "no such issue" in capsys.readouterr().err
    assert _shape_event_row(tmp_path) == ("issue", "unresolved", 0)


@patch("theforge.cli.shape.subprocess.run")
def test_audit_emitted_when_gh_returns_malformed_json(mock_run, tmp_path, capsys):
    mock_run.return_value = _proc(stdout="not-json")
    args = _make_args(tmp_path, issue=42)
    rc = cmd_shape(args)
    assert rc == 1
    assert "malformed JSON" in capsys.readouterr().err
    assert _shape_event_row(tmp_path) == ("issue", "unresolved", 0)


def test_parser_help_lists_shape_command(capsys):
    parser = build_parser()
    try:
        parser.parse_args(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "shape" in out


def test_classify_treats_story_label_as_enhancement():
    from theforge.intake.shape_classify import Classification, classify

    proposal = classify("title", "body without ac", ["story"])
    assert proposal.classification is Classification.ENHANCEMENT


def test_classify_treats_feature_label_as_enhancement():
    from theforge.intake.shape_classify import Classification, classify

    proposal = classify("title", "body without ac", ["feature"])
    assert proposal.classification is Classification.ENHANCEMENT


@patch("theforge.cli.shape.subprocess.run")
def test_normal_render_never_invokes_downstream_producers(mock_run, tmp_path, capsys):
    """Regression: a non-apply, non-next shape run must NOT spawn any
    subprocess beyond the gh issue view it needs to load the issue."""
    gh_view = _proc(
        stdout=json.dumps(
            {
                "title": "cut-rc.sh broke",
                "body": "happens on every release",
                "labels": [{"name": "bug"}],
            }
        )
    )
    mock_run.return_value = gh_view
    args = _make_args(tmp_path, issue=1497)
    cmd_shape(args)
    # Exactly one subprocess call — the gh issue view to load the issue.
    assert mock_run.call_count == 1
    call_args = mock_run.call_args.args[0]
    assert call_args[:3] == ["gh", "issue", "view"]
    # And none of the producer command names appear in any argv.
    for call in mock_run.call_args_list:
        argv = call.args[0]
        for forbidden in ("diagnose", "groom"):
            assert forbidden not in argv, f"unexpected producer call: {argv}"
