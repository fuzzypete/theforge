"""``forge shape`` — classify rough drafts into typed work objects.

Refusal-capable: when the classifier's confidence is low, the command
keeps the item as ``todo:draft`` and emits structured ambiguity questions
rather than forcing a classification. Without ``--apply`` it prints a
diff and exits non-zero; with ``--apply`` it edits the issue via ``gh``.
The command never auto-invokes a producer (`forge diagnose`, `forge groom`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.coordinator.shape_audit import emit_shape_event
from theforge.intake.shape_classify import (
    Classification,
    ShapeProposal,
    classify,
)
from theforge.intake.shape_render import (
    next_command,
    render_proposal,
    restructure_body,
)


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


def _load_issue(issue_number: int, cwd: Path) -> tuple[str, str, list[str]] | None:
    proc = _gh(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "title,body,labels",
        ],
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
    title = str(data.get("title") or "")
    body = str(data.get("body") or "")
    labels_raw = data.get("labels") or []
    labels: list[str] = []
    for entry in labels_raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                labels.append(name)
        elif isinstance(entry, str):
            labels.append(entry)
    return title, body, labels


def _load_from_file(path: Path) -> tuple[str, str, list[str]]:
    text = path.read_text(encoding="utf-8")
    title, body = _split_title_body(text)
    return title, body, []


def _split_title_body(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines:
        return "", ""
    first = lines[0].strip()
    if first.startswith("#"):
        title = first.lstrip("#").strip()
        body = "\n".join(lines[1:]).lstrip("\n")
        return title, body
    return first, "\n".join(lines[1:]).lstrip("\n")


def _apply_to_github(
    issue_number: int,
    proposal: ShapeProposal,
    new_body: str,
    cwd: Path,
) -> bool:
    """Apply label edits + body restructure via gh. Returns True on success."""
    # Add labels (one --add-label per label keeps gh happy across versions).
    for label in proposal.proposed_labels:
        proc = _gh(
            ["gh", "issue", "edit", str(issue_number), "--add-label", label],
            cwd,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit --add-label failed"
            print(err, file=sys.stderr)
            return False
    for label in proposal.removed_labels:
        proc = _gh(
            ["gh", "issue", "edit", str(issue_number), "--remove-label", label],
            cwd,
        )
        if proc.returncode != 0:
            err = (
                proc.stderr.strip() or proc.stdout.strip() or "gh issue edit --remove-label failed"
            )
            print(err, file=sys.stderr)
            return False
    # Body restructure goes via --body-file so we don't have to escape.
    import tempfile

    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".md", delete=False) as tmp:
        tmp.write(new_body)
        tmp.flush()
        tmp_path = Path(tmp.name)
    try:
        proc = _gh(
            [
                "gh",
                "issue",
                "edit",
                str(issue_number),
                "--body-file",
                str(tmp_path),
            ],
            cwd,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit --body-file failed"
            print(err, file=sys.stderr)
            return False
    finally:
        tmp_path.unlink(missing_ok=True)
    return True


def cmd_shape(args: argparse.Namespace) -> int:
    project_root = _project_root(args)
    if project_root is None:
        return 1

    title: str
    body: str
    labels: list[str]
    issue_number: int | None
    input_source: str

    if args.from_brief:
        brief_path = Path(args.from_brief)
        if not brief_path.exists():
            print(f"--from-brief: file not found: {brief_path}", file=sys.stderr)
            return 1
        title, body, labels = _load_from_file(brief_path)
        issue_number = None
        input_source = "file"
    elif args.from_stdin:
        raw = sys.stdin.read()
        title, body = _split_title_body(raw)
        labels = []
        issue_number = None
        input_source = "stdin"
    elif args.issue is not None:
        loaded = _load_issue(args.issue, project_root)
        if loaded is None:
            return 1
        title, body, labels = loaded
        issue_number = args.issue
        input_source = "issue"
    else:
        print(
            "forge shape: provide an issue number, --from-brief PATH, or --from-stdin",
            file=sys.stderr,
        )
        return 1

    proposal = classify(title, body, labels)

    # --next: print only the operator-facing next command and exit 0.
    if args.next:
        print(next_command(proposal, issue_number))
        _safe_emit(
            project_root,
            issue_number=issue_number,
            input_source=input_source,
            proposal=proposal,
            apply_mutated=False,
        )
        return 0

    source_label = (
        f"#{issue_number} {title!r}".strip()
        if issue_number is not None
        else f"brief: {title!r}"
        if title
        else "<draft>"
    )
    rendered = render_proposal(
        proposal,
        source_label=source_label,
        current_body=body,
        show_diff=True,
    )
    print(rendered, end="")
    print(next_command(proposal, issue_number))

    mutated = False
    if args.apply:
        if proposal.classification is Classification.UNRESOLVED:
            print(
                "--apply refused: classification is unresolved (kept as todo:draft).",
                file=sys.stderr,
            )
            _safe_emit(
                project_root,
                issue_number=issue_number,
                input_source=input_source,
                proposal=proposal,
                apply_mutated=False,
            )
            return 1
        if issue_number is None:
            print(
                "--apply requires an issue number; --from-brief and --from-stdin cannot mutate.",
                file=sys.stderr,
            )
            _safe_emit(
                project_root,
                issue_number=issue_number,
                input_source=input_source,
                proposal=proposal,
                apply_mutated=False,
            )
            return 1
        new_body = restructure_body(proposal, body)
        ok = _apply_to_github(issue_number, proposal, new_body, project_root)
        if not ok:
            _safe_emit(
                project_root,
                issue_number=issue_number,
                input_source=input_source,
                proposal=proposal,
                apply_mutated=False,
            )
            return 1
        print(f"Applied proposal to #{issue_number}.")
        mutated = True

    _safe_emit(
        project_root,
        issue_number=issue_number,
        input_source=input_source,
        proposal=proposal,
        apply_mutated=mutated,
    )

    if args.apply:
        return 0
    # Without --apply: non-zero when changes would be needed.
    needs_change = (
        proposal.classification is Classification.UNRESOLVED
        or bool(proposal.proposed_labels)
        or bool(proposal.removed_labels)
        or restructure_body(proposal, body) != body
    )
    return 1 if needs_change else 0


def _safe_emit(
    project_root: Path,
    *,
    issue_number: int | None,
    input_source: str,
    proposal: ShapeProposal,
    apply_mutated: bool,
) -> None:
    try:
        emit_shape_event(
            project_root,
            issue_number=issue_number,
            input_source=input_source,
            classification=proposal.classification.value,
            confidence=proposal.confidence.value,
            ambiguity_question_count=len(proposal.ambiguity_questions),
            apply_mutated=apply_mutated,
            diagnosis_state=(
                proposal.diagnosis_state.value if proposal.diagnosis_state is not None else None
            ),
        )
    except Exception as exc:  # noqa: BLE001 — audit emission is fire-and-forget
        print(f"[forge shape] audit emission failed: {exc}", file=sys.stderr)


def register_parser(subparsers: object) -> None:
    p = subparsers.add_parser(
        "shape",
        help="Classify a rough draft into a typed work object (no auto-mutate without --apply)",
    )
    p.add_argument("issue", nargs="?", type=int, help="GitHub issue number to shape")
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument(
        "--from-brief",
        dest="from_brief",
        help="Path to a freeform brief file instead of an issue",
    )
    p.add_argument(
        "--from-stdin",
        dest="from_stdin",
        action="store_true",
        default=False,
        help="Read a freeform brief from stdin",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply the proposed label edits + body restructure via gh",
    )
    p.add_argument(
        "--next",
        action="store_true",
        default=False,
        help="Print only the recommended next operator command and exit (never auto-invokes it)",
    )
    p.set_defaults(func=cmd_shape)


__all__ = ["cmd_shape", "register_parser"]
