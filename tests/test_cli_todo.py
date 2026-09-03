from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.main import build_parser
from theforge.cli.todo import cmd_todo


def _make_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    data: dict[str, object] = {
        "config": str(forge_yaml),
        "todo_action": None,
        "text": "agent abstraction conflates provider/model/transport",
        "from_sprint": None,
        "issue": None,
        "run_id": None,
        "number": None,
        "todo_args": [],
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_parser_registers_todo_command():
    parser = build_parser()
    args = parser.parse_args(["todo", "list"])
    assert args.command == "todo"
    assert args.todo_action == "list"


def test_parser_accepts_bare_todo_capture_text():
    parser = build_parser()
    args = parser.parse_args(["todo", "agent abstraction conflates provider/model/transport"])
    assert args.command == "todo"
    assert args.todo_action == "agent abstraction conflates provider/model/transport"


def test_parser_accepts_bare_todo_capture_with_provenance_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "todo",
            "--from-sprint",
            "issues-855",
            "--issue",
            "855",
            "--run-id",
            "run-123",
            "check-config misreports API providers",
        ]
    )
    assert args.command == "todo"
    assert args.from_sprint == "issues-855"
    assert args.issue == 855
    assert args.run_id == "run-123"
    assert args.todo_action == "check-config misreports API providers"


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_create_adds_draft_label(mock_run, tmp_path, capsys):
    mock_run.return_value = _proc(stdout="https://github.com/acme/repo/issues/123\n")
    args = _make_args(tmp_path)

    rc = cmd_todo(args)

    assert rc == 0
    cmd = (
        mock_run.call_args.kwargs["args"]
        if "args" in mock_run.call_args.kwargs
        else mock_run.call_args[0][0]
    )
    assert cmd == [
        "gh",
        "issue",
        "create",
        "--title",
        "agent abstraction conflates provider/model/transport",
        "--body",
        "",
        "--label",
        "todo:draft",
    ]
    assert "issues/123" in capsys.readouterr().out


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_create_appends_provenance_block(mock_run, tmp_path):
    mock_run.return_value = _proc(stdout="https://github.com/acme/repo/issues/124\n")
    args = _make_args(
        tmp_path,
        from_sprint="issues-855",
        issue=855,
        run_id="run-123",
        text="check-config misreports API providers",
    )

    rc = cmd_todo(args)

    assert rc == 0
    cmd = mock_run.call_args[0][0]
    body = cmd[cmd.index("--body") + 1]
    assert "## Provenance" in body
    assert "- from_sprint: issues-855" in body
    assert "- issue: 855" in body
    assert "- run_id: run-123" in body


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_create_rejects_missing_text(mock_run, tmp_path, capsys):
    args = _make_args(tmp_path, text=None)

    rc = cmd_todo(args)

    assert rc == 1
    assert not mock_run.called
    assert "todo text is required" in capsys.readouterr().err


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_list_shows_open_drafts(mock_run, tmp_path, capsys):
    mock_run.return_value = _proc(
        stdout='[{"number": 12, "title": "first"}, {"number": 14, "title": "second"}]'
    )
    args = _make_args(tmp_path, todo_action="list", text=None)

    rc = cmd_todo(args)

    assert rc == 0
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        "todo:draft",
        "--json",
        "number,title",
    ]
    out = capsys.readouterr().out
    assert "#12\tfirst" in out
    assert "#14\tsecond" in out


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_promote_removes_draft_label(mock_run, tmp_path, capsys):
    mock_run.return_value = _proc()
    args = _make_args(tmp_path, todo_action="promote", number=12, text=None)

    rc = cmd_todo(args)

    assert rc == 0
    assert mock_run.call_args[0][0] == [
        "gh",
        "issue",
        "edit",
        "12",
        "--remove-label",
        "todo:draft",
    ]
    assert "Promoted todo #12" in capsys.readouterr().out


def _spike_guard_gh(labels: tuple[str, ...] = (), body: str = "") -> MagicMock:
    """Answer the spike guard's `gh issue view --json` probe."""
    return _proc(
        stdout=json.dumps(
            {
                "state": "OPEN",
                "labels": [{"name": name} for name in labels],
                "body": body,
                "comments": [],
            }
        )
    )


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_triage_runs_interactive_actions(mock_run, tmp_path, monkeypatch, capsys):
    mock_run.side_effect = [
        _proc(),
        _proc(),
        _proc(stdout='{"title": "a todo", "body": "existing body", "labels": []}'),
        _proc(returncode=0),
        _proc(),
        _spike_guard_gh(),  # the spike guard's pre-close probe: not a spike
        _proc(),
    ]
    args = _make_args(tmp_path, todo_action="triage", number=12, text=None)
    responses = iter(["bug,backend", "Sprint 12", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))
    monkeypatch.setenv("EDITOR", "nano")

    rc = cmd_todo(args)

    assert rc == 0
    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0] == ["gh", "issue", "edit", "12", "--add-label", "bug,backend"]
    assert calls[1] == ["gh", "issue", "edit", "12", "--milestone", "Sprint 12"]
    assert calls[2] == ["gh", "issue", "view", "12", "--json", "title,body,labels"]
    assert calls[3][0] == "nano"
    assert calls[3][1].endswith(".md")
    assert calls[4][0:4] == ["gh", "issue", "edit", "12"]
    assert calls[4][4] == "--body-file"
    assert calls[4][5].endswith(".md")
    assert calls[5] == ["gh", "issue", "view", "12", "--json", "state,labels,body"]
    assert calls[6] == ["gh", "issue", "close", "12"]
    assert "timeout" not in mock_run.call_args_list[3].kwargs
    assert "Closed todo #12" in capsys.readouterr().out


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_triage_refuses_to_close_an_outcomeless_spike(
    mock_run, tmp_path, monkeypatch, capsys
):
    """Triage is a repository-controlled close path, so it asks the guard too (#2600)."""
    mock_run.side_effect = [
        _proc(),
        _proc(),
        _proc(stdout='{"title": "a spike", "body": "existing body", "labels": []}'),
        _proc(returncode=0),
        _proc(),
        _spike_guard_gh(labels=("spike",), body="A question."),
    ]
    args = _make_args(tmp_path, todo_action="triage", number=12, text=None)
    responses = iter(["spike", "Sprint 12", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))
    monkeypatch.setenv("EDITOR", "nano")

    rc = cmd_todo(args)

    assert rc == 1
    calls = [call.args[0] for call in mock_run.call_args_list]
    assert not any("close" in call for call in calls), "the spike must stay open"
    err = capsys.readouterr().err
    assert "records no outcome" in err
    assert "todo #12 left open." in err


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_triage_requires_issue_number(mock_run, tmp_path, capsys):
    args = _make_args(tmp_path, todo_action="triage", text=None)

    rc = cmd_todo(args)

    assert rc == 1
    assert not mock_run.called
    assert "issue number required for triage" in capsys.readouterr().err


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_promote_requires_issue_number(mock_run, tmp_path, capsys):
    args = _make_args(tmp_path, todo_action="promote", text=None)

    rc = cmd_todo(args)

    assert rc == 1
    assert not mock_run.called
    assert "issue number required for promote" in capsys.readouterr().err


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_triage_rejects_invalid_issue_number(mock_run, tmp_path, capsys):
    args = _make_args(tmp_path, todo_action="triage", todo_args=["abc"], text=None)

    rc = cmd_todo(args)

    assert rc == 1
    assert not mock_run.called
    assert "invalid issue number for triage: abc" in capsys.readouterr().err


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_promote_rejects_invalid_issue_number(mock_run, tmp_path, capsys):
    args = _make_args(tmp_path, todo_action="promote", todo_args=["abc"], text=None)

    rc = cmd_todo(args)

    assert rc == 1
    assert not mock_run.called
    assert "invalid issue number for promote: abc" in capsys.readouterr().err
