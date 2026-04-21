from __future__ import annotations

import argparse
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


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_create_adds_draft_label_and_empty_body(mock_run, tmp_path, capsys):
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
        "--label",
        "todo:draft",
        "--body",
        "",
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


@patch("theforge.cli.todo.subprocess.run")
def test_cmd_todo_triage_runs_interactive_actions(mock_run, tmp_path, monkeypatch, capsys):
    mock_run.side_effect = [
        _proc(),
        _proc(),
        _proc(stdout='{"body": "existing body"}'),
        _proc(),
        _proc(),
    ]
    args = _make_args(tmp_path, todo_action="triage", number=12, text=None)
    responses = iter(["bug,backend", "Sprint 12", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))

    rc = cmd_todo(args)

    assert rc == 0
    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0] == ["gh", "issue", "edit", "12", "--add-label", "bug,backend"]
    assert calls[1] == ["gh", "issue", "edit", "12", "--milestone", "Sprint 12"]
    assert calls[2] == ["gh", "issue", "view", "12", "--json", "body"]
    assert calls[3][:5] == ["gh", "issue", "edit", "12", "--body-file"]
    assert calls[4] == ["gh", "issue", "close", "12"]
    assert "Closed todo #12" in capsys.readouterr().out
