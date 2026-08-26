from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.author import cmd_author
from theforge.cli.main import build_parser


def _make_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    data: dict[str, object] = {
        "config": str(forge_yaml),
        "from_draft": None,
        "from_issue": None,
        "title": None,
        "type_label": None,
        "output": None,
        "create": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_parser_registers_author_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["author", "--type", "enhancement", "--title", "Draft"])
    assert args.command == "author"
    assert args.type_label == "enhancement"
    assert args.title == "Draft"


@patch("theforge.cli.author.subprocess.run")
def test_author_refuses_create_and_writes_incomplete_draft(
    mock_run, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "draft.md"
    args = _make_args(
        tmp_path,
        title="Surface issue-body requirements before submission",
        type_label="enhancement",
        output=str(output),
        create=True,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    rc = cmd_author(args)

    assert rc == 1
    assert not mock_run.called
    content = output.read_text(encoding="utf-8")
    assert 'title: "Surface issue-body requirements before submission"' in content
    assert 'labels: ["enhancement", "todo:draft"]' in content
    assert "> Status: incomplete draft" in content
    err = capsys.readouterr().err
    assert "refused to submit" in err


@patch("theforge.cli.author.subprocess.run")
def test_author_updates_issue_only_after_runnable_validation(
    mock_run, tmp_path, monkeypatch, capsys
):
    mock_run.side_effect = [
        _proc(
            stdout=json.dumps(
                {
                    "title": "Surface issue-body requirements before submission",
                    "body": (
                        "## Why\n\nAuthors discover the rules from refusals.\n\n"
                        "## Acceptance criteria\n\nTODO: replace with real criteria.\n"
                    ),
                    "labels": [{"name": "enhancement"}, {"name": "todo:draft"}],
                }
            )
        ),
        _proc(),
        _proc(),
    ]
    args = _make_args(tmp_path, from_issue=2408)
    responses = iter(
        [
            "- A reviewer can see which body part is still missing before submission.",
            "- The authoring path does not require a verb checklist.",
            "END",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    rc = cmd_author(argparse.Namespace(**{**vars(args), "create": True}))

    assert rc == 0
    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0] == ["gh", "issue", "view", "2408", "--json", "title,body,labels"]
    assert calls[1] == ["gh", "issue", "edit", "2408", "--remove-label", "todo:draft"]
    assert calls[2][0:5] == ["gh", "issue", "edit", "2408", "--title"]
    assert calls[2][5] == "Surface issue-body requirements before submission"
    assert calls[2][6] == "--body-file"
    err = capsys.readouterr().err
    assert "Status: runnable" in err
    assert "Updated issue #2408" in err


@patch("theforge.cli.author.subprocess.run")
def test_author_creates_issue_only_when_flow_is_runnable(mock_run, tmp_path, monkeypatch, capsys):
    mock_run.return_value = _proc(stdout="https://github.com/acme/repo/issues/2408\n")
    args = _make_args(
        tmp_path,
        title="Surface issue-body requirements before submission",
        type_label="enhancement",
        create=True,
    )
    responses = iter(
        [
            "- A reviewer can see which body part is still missing before submission.",
            "- The authoring path does not require a verb checklist.",
            "END",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    rc = cmd_author(args)

    assert rc == 0
    command = mock_run.call_args[0][0]
    assert command[0:4] == ["gh", "issue", "create", "--title"]
    assert "--body-file" in command
    assert "--label" in command
    assert "todo:draft" not in command
    assert "issues/2408" in capsys.readouterr().out
