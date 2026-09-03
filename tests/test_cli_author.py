from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.author import cmd_author
from theforge.cli.main import build_parser
from theforge.intake.author_flow import available_type_labels


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


def test_author_parser_rejects_non_dispatchable_types() -> None:
    assert available_type_labels() == ("bug", "enhancement", "task", "spike")
    parser = build_parser()
    try:
        parser.parse_args(["author", "--type", "epic"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected parser to reject epic")


@patch("theforge.cli.author.subprocess.run")
def test_author_from_issue_rejects_non_dispatchable_inferred_type(
    mock_run, tmp_path, monkeypatch, capsys
):
    mock_run.return_value = _proc(
        stdout=json.dumps(
            {
                "title": "Track the v0.12 release train",
                "body": "## Acceptance criteria\n\n- Child issues are linked.\n",
                "labels": [{"name": "epic"}],
            }
        )
    )
    args = _make_args(tmp_path, from_issue=2408)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": (_ for _ in ()).throw(AssertionError("prompt should not run")),
    )

    rc = cmd_author(args)

    assert rc == 1
    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls == [["gh", "issue", "view", "2408", "--json", "title,body,labels"]]
    assert "not dispatchable through forge author" in capsys.readouterr().err


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
def test_author_refuses_create_without_output_and_emits_incomplete_draft(
    mock_run, tmp_path, monkeypatch, capsys
):
    args = _make_args(
        tmp_path,
        title="Surface issue-body requirements before submission",
        type_label="enhancement",
        create=True,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    rc = cmd_author(args)

    assert rc == 1
    assert not mock_run.called
    captured = capsys.readouterr()
    assert "> Status: incomplete draft" in captured.out
    assert "emitted to stdout" in captured.err


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
    assert calls[1][0:5] == ["gh", "issue", "edit", "2408", "--title"]
    assert calls[1][5] == "Surface issue-body requirements before submission"
    assert calls[1][6] == "--body-file"
    assert calls[2] == ["gh", "issue", "edit", "2408", "--remove-label", "todo:draft"]
    err = capsys.readouterr().err
    assert "Status: runnable" in err
    assert "Updated issue #2408" in err


@patch("theforge.cli.author.subprocess.run")
def test_author_does_not_remove_draft_label_if_issue_body_edit_fails(
    mock_run, tmp_path, monkeypatch
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
        _proc(returncode=1, stderr="body edit failed"),
    ]
    args = _make_args(tmp_path, from_issue=2408, create=True)
    responses = iter(
        [
            "- A reviewer can see which body part is still missing before submission.",
            "- The authoring path does not require a verb checklist.",
            "END",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    rc = cmd_author(args)

    assert rc == 1
    calls = [call.args[0] for call in mock_run.call_args_list]
    assert len(calls) == 2
    assert calls[1][0:5] == ["gh", "issue", "edit", "2408", "--title"]
    assert all("--remove-label" not in call for call in calls)


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


@patch("theforge.cli.author.subprocess.run")
def test_author_resuming_draft_file_removes_stale_incomplete_marker(
    mock_run, tmp_path, monkeypatch
):
    draft = tmp_path / "draft.md"
    draft.write_text(
        "---\n"
        'title: "Surface issue-body requirements before submission"\n'
        'labels: ["enhancement", "todo:draft"]\n'
        "---\n\n"
        "> Status: incomplete draft — do not submit yet. Missing before submission: "
        "Acceptance criteria.\n\n"
        "## Why\n\nAuthors discover the rules from refusals.\n\n"
        "## Acceptance criteria\n\nTODO: replace with real criteria.\n",
        encoding="utf-8",
    )
    args = _make_args(tmp_path, from_draft=str(draft), output=str(draft))
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
    assert not mock_run.called
    content = draft.read_text(encoding="utf-8")
    assert "> Status: incomplete draft" not in content
    assert content.count("## Acceptance criteria") == 1


@patch("theforge.cli.author.subprocess.run")
def test_author_resaving_incomplete_draft_does_not_stack_status_banner(
    mock_run, tmp_path, monkeypatch
):
    draft = tmp_path / "draft.md"
    draft.write_text(
        "---\n"
        'title: "Surface issue-body requirements before submission"\n'
        'labels: ["enhancement", "todo:draft"]\n'
        "---\n\n"
        "> Status: incomplete draft — do not submit yet. Missing before submission: "
        "Acceptance criteria.\n\n"
        "## Why\n\nAuthors discover the rules from refusals.\n\n"
        "## Acceptance criteria\n\nTODO: replace with real criteria.\n",
        encoding="utf-8",
    )
    args = _make_args(tmp_path, from_draft=str(draft), output=str(draft))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    rc = cmd_author(args)

    assert rc == 1
    assert not mock_run.called
    content = draft.read_text(encoding="utf-8")
    assert content.count("> Status: incomplete draft") == 1


@patch("theforge.cli.author.subprocess.run")
def test_author_from_draft_without_title_prompts_for_title(mock_run, tmp_path, monkeypatch):
    draft = tmp_path / "draft.md"
    draft.write_text(
        "## Why\n\nAuthors discover the rules from refusals.\n\n"
        "## Acceptance criteria\n\nTODO: replace with real criteria.\n",
        encoding="utf-8",
    )
    output = tmp_path / "completed.md"
    args = _make_args(
        tmp_path,
        from_draft=str(draft),
        type_label="enhancement",
        output=str(output),
    )
    responses = iter(
        [
            "Surface issue-body requirements before submission",
            "- A reviewer can see which body part is still missing before submission.",
            "- The authoring path does not require a verb checklist.",
            "END",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    rc = cmd_author(args)

    assert rc == 0
    assert not mock_run.called
    content = output.read_text(encoding="utf-8")
    assert 'title: "Surface issue-body requirements before submission"' in content
