"""forge todo subcommand — frictionless GitHub issue capture and triage."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from theforge.cli.shared import _find_config

TODO_DRAFT_LABEL = "todo:draft"


def _project_root_from_args(args: argparse.Namespace) -> Path | None:
    config_path = Path(args.config).resolve() if getattr(args, "config", None) else _find_config()
    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return None
    return config_path.parent


def _run_gh(command: list[str], project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=30,
    )


def _format_provenance_block(args: argparse.Namespace) -> str:
    entries: list[tuple[str, str]] = []
    if getattr(args, "from_sprint", None):
        entries.append(("from_sprint", str(args.from_sprint)))
    if getattr(args, "issue", None) is not None:
        entries.append(("issue", str(args.issue)))
    if getattr(args, "run_id", None):
        entries.append(("run_id", str(args.run_id)))
    if not entries:
        return ""

    lines = ["## Provenance", ""]
    lines.extend(f"- {key}: {value}" for key, value in entries)
    return "\n".join(lines)


def _editor_command() -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return shlex.split(editor)
    return ["vi"]


def _create_todo(args: argparse.Namespace) -> int:
    project_root = _project_root_from_args(args)
    if project_root is None:
        return 1

    text = getattr(args, "text", None)
    if text is None or not text.strip():
        print("todo text is required", file=sys.stderr)
        return 1

    body = _format_provenance_block(args)
    command = [
        "gh",
        "issue",
        "create",
        "--title",
        text,
        "--body",
        body,
        "--label",
        TODO_DRAFT_LABEL,
    ]

    proc = _run_gh(command, project_root)
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue create failed"
        print(err, file=sys.stderr)
        return 1

    output = proc.stdout.strip()
    if output:
        print(output)
    return 0


def _list_todos(args: argparse.Namespace) -> int:
    project_root = _project_root_from_args(args)
    if project_root is None:
        return 1
    proc = _run_gh(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            TODO_DRAFT_LABEL,
            "--json",
            "number,title",
        ],
        project_root,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue list failed"
        print(err, file=sys.stderr)
        return 1

    try:
        issues = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(f"gh issue list returned malformed JSON: {exc}", file=sys.stderr)
        return 1

    if not issues:
        print("No open todo drafts.")
        return 0

    for issue in issues:
        print(f"#{issue['number']}\t{issue['title']}")
    return 0


def _promote_todo(args: argparse.Namespace) -> int:
    project_root = _project_root_from_args(args)
    if project_root is None:
        return 1
    proc = _run_gh(
        ["gh", "issue", "edit", str(args.number), "--remove-label", TODO_DRAFT_LABEL],
        project_root,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit failed"
        print(err, file=sys.stderr)
        return 1
    print(f"Promoted todo #{args.number}")
    return 0


def _triage_todo(args: argparse.Namespace) -> int:
    project_root = _project_root_from_args(args)
    if project_root is None:
        return 1
    issue_number = str(args.number)

    print(f"Triage todo #{issue_number}")
    labels = input("Labels to add (comma-separated, blank to skip): ").strip()
    milestone = input("Milestone to set (blank to skip): ").strip()
    edit_body = input("Edit body in $EDITOR? [y/N]: ").strip().lower() in {"y", "yes"}
    close_issue = input("Close issue? [y/N]: ").strip().lower() in {"y", "yes"}

    if labels:
        proc = _run_gh(["gh", "issue", "edit", issue_number, "--add-label", labels], project_root)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit failed"
            print(err, file=sys.stderr)
            return 1

    if milestone:
        proc = _run_gh(
            ["gh", "issue", "edit", issue_number, "--milestone", milestone], project_root
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit failed"
            print(err, file=sys.stderr)
            return 1

    if edit_body:
        view_proc = _run_gh(["gh", "issue", "view", issue_number, "--json", "body"], project_root)
        if view_proc.returncode != 0:
            err = view_proc.stderr.strip() or view_proc.stdout.strip() or "gh issue view failed"
            print(err, file=sys.stderr)
            return 1
        try:
            current_body = json.loads(view_proc.stdout or "{}").get("body", "")
        except json.JSONDecodeError as exc:
            print(f"gh issue view returned malformed JSON: {exc}", file=sys.stderr)
            return 1

        with tempfile.NamedTemporaryFile(
            "w+", encoding="utf-8", suffix=".md", delete=False
        ) as tmp:
            tmp.write(current_body)
            tmp.flush()
            temp_path = Path(tmp.name)

        try:
            editor_proc = subprocess.run(
                [*_editor_command(), str(temp_path)], cwd=str(project_root)
            )
            if editor_proc.returncode != 0:
                print("editor exited with a non-zero status", file=sys.stderr)
                return 1

            edit_proc = _run_gh(
                ["gh", "issue", "edit", issue_number, "--body-file", str(temp_path)], project_root
            )
            if edit_proc.returncode != 0:
                err = (
                    edit_proc.stderr.strip() or edit_proc.stdout.strip() or "gh issue edit failed"
                )
                print(err, file=sys.stderr)
                return 1
        finally:
            temp_path.unlink(missing_ok=True)

    if close_issue:
        proc = _run_gh(["gh", "issue", "close", issue_number], project_root)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gh issue close failed"
            print(err, file=sys.stderr)
            return 1
        print(f"Closed todo #{issue_number}")
        return 0

    print(f"Updated todo #{issue_number}")
    return 0


def _parse_issue_number(raw_number: object) -> int | None:
    if raw_number is None:
        return None
    try:
        return int(raw_number)
    except (TypeError, ValueError):
        return None


def cmd_todo(args: argparse.Namespace) -> int:
    action = getattr(args, "todo_action", None)
    extra_args = list(getattr(args, "todo_args", []))
    number = _parse_issue_number(getattr(args, "number", None))

    if action == "list" and not extra_args:
        return _list_todos(args)

    if action in {"triage", "promote"}:
        if number is None:
            if not extra_args:
                print(f"issue number required for {action}", file=sys.stderr)
                return 1
            if len(extra_args) != 1:
                print(f"invalid arguments for {action}", file=sys.stderr)
                return 1
            number = _parse_issue_number(extra_args[0])
            if number is None:
                print(f"invalid issue number for {action}: {extra_args[0]}", file=sys.stderr)
                return 1
        elif extra_args:
            print(f"invalid arguments for {action}", file=sys.stderr)
            return 1

        args.number = number
        if action == "triage":
            return _triage_todo(args)
        return _promote_todo(args)

    if action is not None:
        text_parts = [action, *extra_args]
        args.text = " ".join(text_parts)
    return _create_todo(args)


def register_parser(subparsers: object) -> None:
    parser = subparsers.add_parser("todo", help="Capture and triage draft GitHub todo issues")
    parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    parser.add_argument("--from-sprint", dest="from_sprint", help="Record originating sprint id")
    parser.add_argument("--issue", type=int, help="Record originating issue number")
    parser.add_argument("--run-id", dest="run_id", help="Record originating run id")
    parser.add_argument("todo_action", nargs="?", help="Subcommand name or todo title text")
    parser.add_argument("todo_args", nargs="*", help=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_todo)
