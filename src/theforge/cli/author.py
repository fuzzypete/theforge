"""Interactive pre-submission issue authoring."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.intake.author_flow import (
    AuthorPrompt,
    AuthorResult,
    available_type_labels,
    run_author_flow,
)
from theforge.shape_check.issue_spec import spec_for_label, spec_for_labels


@dataclass(frozen=True)
class _LoadedDraft:
    title: str
    body: str
    labels: tuple[str, ...]
    issue_number: int | None = None


def _project_root(args: argparse.Namespace) -> Path | None:
    config_path = Path(args.config).resolve() if getattr(args, "config", None) else _find_config()
    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return None
    return config_path.parent


def _gh(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, cwd=str(cwd), timeout=30)


def _load_issue(issue_number: int, cwd: Path) -> _LoadedDraft | None:
    proc = _gh(
        ["gh", "issue", "view", str(issue_number), "--json", "title,body,labels"],
        cwd,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue view failed"
        print(err, file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        print(f"gh issue view returned malformed JSON: {exc}", file=sys.stderr)
        return None

    labels: list[str] = []
    for raw in data.get("labels") or []:
        if isinstance(raw, dict):
            name = raw.get("name")
            if isinstance(name, str):
                labels.append(name)
        elif isinstance(raw, str):
            labels.append(raw)
    return _LoadedDraft(
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        labels=tuple(labels),
        issue_number=issue_number,
    )


def _load_draft_file(path: Path) -> _LoadedDraft | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return None

    title = ""
    labels: list[str] = []
    body = raw
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            frontmatter = raw[4:end]
            body = raw[end + len("\n---\n") :]
            for line in frontmatter.splitlines():
                stripped = line.strip()
                if stripped.startswith("title:"):
                    title = stripped.partition(":")[2].strip().strip('"').strip("'")
                if stripped.startswith("labels:"):
                    payload = stripped.partition(":")[2].strip()
                    if payload.startswith("[") and payload.endswith("]"):
                        labels = [
                            token.strip().strip('"').strip("'")
                            for token in payload[1:-1].split(",")
                            if token.strip()
                        ]

    if not title:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
    return _LoadedDraft(title=title, body=body, labels=tuple(labels))


def _resolve_selected_type(args: argparse.Namespace, loaded: _LoadedDraft) -> str:
    explicit = getattr(args, "type_label", None)
    if explicit:
        return explicit
    inferred = spec_for_labels(loaded.labels)
    if inferred is not None:
        return inferred.label
    matches = {
        spec.label
        for spec in (spec_for_label(label) for label in loaded.labels)
        if spec is not None and spec.label in available_type_labels()
    }
    if len(matches) == 1:
        return next(iter(matches))

    options = available_type_labels()
    while True:
        choice = input(f"Issue type [{'/'.join(options)}]: ").strip().lower()
        if choice in options:
            return choice
        print(f"Choose one of: {', '.join(options)}", file=sys.stderr)


def _answer_source(prompt: AuthorPrompt) -> str | None:
    print(f"\n{prompt.label}", file=sys.stderr)
    print(f"  {prompt.prompt}", file=sys.stderr)
    print(f"  Constraint: {prompt.constraint}", file=sys.stderr)
    if prompt.existing.strip():
        print("  Existing draft:", file=sys.stderr)
        for line in prompt.existing.strip().splitlines():
            print(f"    {line}", file=sys.stderr)

    if prompt.multiline:
        print("  Enter Markdown. Finish with a line containing only END.", file=sys.stderr)
        print("  Leave the first line blank to keep this incomplete.", file=sys.stderr)
        lines: list[str] = []
        while True:
            line = input("> ")
            if not lines and not line.strip():
                return None
            if line.strip() == "END":
                break
            lines.append(line)
        return "\n".join(lines).strip()

    value = input("> ").strip()
    return value or None


def _write_output(path: Path, result: AuthorResult) -> None:
    labels = ", ".join(json.dumps(label) for label in result.labels)
    frontmatter = f"---\ntitle: {json.dumps(result.title)}\nlabels: [{labels}]\n---\n\n"
    path.write_text(frontmatter + result.body_for_storage(), encoding="utf-8")


def _print_summary(result: AuthorResult, selected_type_label: str) -> None:
    print(f"Title: {result.title or '(missing)'}", file=sys.stderr)
    print(f"Type: {selected_type_label}", file=sys.stderr)
    print(f"Labels: {', '.join(result.labels) if result.labels else '(none)'}", file=sys.stderr)
    print(f"Status: {result.status.value}", file=sys.stderr)
    if result.missing_parts:
        print("Missing before submission:", file=sys.stderr)
        for part in result.missing_parts:
            print(f"- {part.label}: {part.detail}", file=sys.stderr)
    if result.reasons and not result.runnable:
        print("Shape-gate findings:", file=sys.stderr)
        for reason in result.reasons:
            print(f"- {reason.code}: {reason.detail}", file=sys.stderr)


def _apply_labels(
    issue_number: int,
    *,
    before: tuple[str, ...],
    after: tuple[str, ...],
    cwd: Path,
) -> bool:
    before_set = {label.lower(): label for label in before}
    after_set = {label.lower(): label for label in after}

    for lowered, label in after_set.items():
        if lowered in before_set:
            continue
        proc = _gh(["gh", "issue", "edit", str(issue_number), "--add-label", label], cwd)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit --add-label failed"
            print(err, file=sys.stderr)
            return False

    for lowered, label in before_set.items():
        if lowered in after_set:
            continue
        proc = _gh(["gh", "issue", "edit", str(issue_number), "--remove-label", label], cwd)
        if proc.returncode != 0:
            err = (
                proc.stderr.strip() or proc.stdout.strip() or "gh issue edit --remove-label failed"
            )
            print(err, file=sys.stderr)
            return False
    return True


def _create_issue(result: AuthorResult, cwd: Path) -> bool:
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".md", delete=False) as tmp:
        tmp.write(result.body_for_storage())
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        command = [
            "gh",
            "issue",
            "create",
            "--title",
            result.title,
            "--body-file",
            str(tmp_path),
        ]
        for label in result.labels:
            command.extend(["--label", label])
        proc = _gh(command, cwd)
    finally:
        tmp_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue create failed"
        print(err, file=sys.stderr)
        return False
    output = proc.stdout.strip()
    if output:
        print(output)
    return True


def _edit_issue(result: AuthorResult, loaded: _LoadedDraft, cwd: Path) -> bool:
    issue_number = loaded.issue_number
    assert issue_number is not None

    body_changed = result.title != loaded.title or result.body_for_storage() != loaded.body

    if body_changed:
        with tempfile.NamedTemporaryFile(
            "w+",
            encoding="utf-8",
            suffix=".md",
            delete=False,
        ) as tmp:
            tmp.write(result.body_for_storage())
            tmp.flush()
            tmp_path = Path(tmp.name)
        try:
            proc = _gh(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue_number),
                    "--title",
                    result.title,
                    "--body-file",
                    str(tmp_path),
                ],
                cwd,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit failed"
            print(err, file=sys.stderr)
            return False

    return _apply_labels(issue_number, before=loaded.labels, after=result.labels, cwd=cwd)


def cmd_author(args: argparse.Namespace) -> int:
    project_root = _project_root(args)
    if project_root is None:
        return 1

    loaded = _LoadedDraft(title=args.title or "", body="", labels=())
    if args.from_issue is not None:
        loaded = _load_issue(args.from_issue, project_root)
        if loaded is None:
            return 1
    elif args.from_draft is not None:
        loaded = _load_draft_file(Path(args.from_draft))
        if loaded is None:
            return 1
    elif args.title:
        loaded = _LoadedDraft(title=args.title, body="", labels=())

    if args.title:
        loaded = _LoadedDraft(
            title=args.title,
            body=loaded.body,
            labels=loaded.labels,
            issue_number=loaded.issue_number,
        )

    selected_type_label = _resolve_selected_type(args, loaded)
    result = run_author_flow(
        title=loaded.title,
        selected_type_label=selected_type_label,
        existing_body=loaded.body,
        existing_labels=loaded.labels,
        answer_source=_answer_source,
    )

    _print_summary(result, selected_type_label)

    if args.output:
        _write_output(Path(args.output), result)
        kind = "draft" if not result.runnable else "body"
        print(f"Wrote {kind} to {args.output}", file=sys.stderr)

    if not args.create:
        if not args.output:
            print(result.body_for_storage())
        return 0 if result.runnable else 1

    if not result.runnable:
        if not args.output:
            print(result.body_for_storage())
        print(
            "forge author refused to submit: required parts are still missing or the body "
            "is not yet runnable. The incomplete draft was emitted to stdout; use "
            "--output to save it and continue later.",
            file=sys.stderr,
        )
        return 1

    if loaded.issue_number is not None:
        ok = _edit_issue(result, loaded, project_root)
        if ok:
            print(f"Updated issue #{loaded.issue_number}", file=sys.stderr)
        return 0 if ok else 1

    ok = _create_issue(result, project_root)
    return 0 if ok else 1


def register_parser(subparsers: object) -> None:
    parser = subparsers.add_parser(
        "author",
        help="Interactively collect a typed issue body before submission",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--from-draft",
        default=None,
        help="Resume authoring from a local draft file",
    )
    source.add_argument(
        "--from-issue",
        type=int,
        default=None,
        help="Resume authoring from an existing GitHub issue",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Starting title for a new draft (otherwise prompted)",
    )
    parser.add_argument(
        "--type",
        dest="type_label",
        choices=available_type_labels(),
        default=None,
        help="Issue type label to author against",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the authored body or incomplete draft to PATH",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        default=False,
        help="Create or update the GitHub issue only after the body is runnable",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to forge.yaml (default: auto-detect)",
    )
