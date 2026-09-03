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
from theforge.shape_check.producer import label_names, validate_issue_body
from theforge.shape_check.types import ShapeVerdict
from theforge.spike_guard import check_spike_closure

TODO_DRAFT_LABEL = "todo:draft"

#: A todo draft is a deliberately non-admissible object: it carries no type
#: label, so the shared gate answers ``needs_type``. That is the state
#: ``forge todo`` declares, pinned to the literal verdict rather than to
#: whatever the gate happens to return — a label-set change that moved the
#: draft to some other state should trip this, not be absorbed by it.
TODO_DRAFT_VERDICT = ShapeVerdict.NEEDS_TYPE


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


def _merge_labels(fetched: object, added: str) -> list[str]:
    """Post-triage label set: what the issue carries plus what triage adds."""
    names = label_names(fetched)
    names.extend(part.strip() for part in added.split(",") if part.strip())
    return list(dict.fromkeys(names))


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
    validation = validate_issue_body(
        producer="forge-todo-create",
        title=text,
        body=body,
        labels=[TODO_DRAFT_LABEL],
        declared=TODO_DRAFT_VERDICT,
    )
    if not validation.conforms:
        print(validation.report(), file=sys.stderr)
        return 1
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
        view_proc = _run_gh(
            ["gh", "issue", "view", issue_number, "--json", "title,body,labels"], project_root
        )
        if view_proc.returncode != 0:
            err = view_proc.stderr.strip() or view_proc.stdout.strip() or "gh issue view failed"
            print(err, file=sys.stderr)
            return 1
        try:
            viewed = json.loads(view_proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            print(f"gh issue view returned malformed JSON: {exc}", file=sys.stderr)
            return 1
        current_body = viewed.get("body", "") or ""
        current_title = viewed.get("title", "") or ""
        # The label edits above have already landed, so the fetched set is the
        # post-triage one; union in the requested adds anyway in case the view
        # raced them.
        post_triage_labels = _merge_labels(viewed.get("labels"), labels)

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

            edited_body = temp_path.read_text(encoding="utf-8")
            # Triage edits an object somebody else owns, so the state declared
            # here is "unchanged, or better": the edit may move the draft to
            # runnable, but it must not introduce a refusal the draft did not
            # already carry. Refusal leaves the body exactly as it was.
            validation = validate_issue_body(
                producer="forge-todo-triage",
                title=current_title,
                body=edited_body,
                labels=post_triage_labels,
                declared=None,
                previous_body=current_body,
            )
            if not validation.conforms:
                print(validation.report(), file=sys.stderr)
                print(f"todo #{issue_number} body left unchanged.", file=sys.stderr)
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
        # A spike closes on a recorded outcome or not at all (#2600). Triage is
        # a repository-controlled close path like any other, so it asks the same
        # guard rather than trusting the closing operator to remember.
        decision = check_spike_closure(int(issue_number), project_root)
        if not decision.allowed:
            print(decision.reason, file=sys.stderr)
            print(f"todo #{issue_number} left open.", file=sys.stderr)
            return 1
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
