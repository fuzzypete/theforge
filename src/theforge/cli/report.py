"""``forge report`` — file a forge bug from the project that observed it.

Run in the consuming project, the command captures the observed run's evidence
out of that project's ``.forge/`` tree, renders a bug-shaped issue body plus a
deterministic evidence payload, evaluates the body against the shape gate, and
creates the issue in the target repository with the payload attached as
comments. Nothing is copied between checkouts by hand, and nothing the report
asserts about the run is answered from the reader's own configuration.

The gate the body is placed against is the **target repository's** — resolved
and executed from that repo's own default-branch revision by
:mod:`theforge.reporting.target_gate`, never the observing checkout's installed
release. The observing project routinely runs an older release than the repo it
reports into, so a local verdict would name a gate state the target does not
hold. When the target-owned gate cannot be resolved, executed, or read, the
body has no known gate state and the command refuses to file it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.reporting.evidence import EvidenceError, RunEvidence, collect_run_evidence
from theforge.reporting.render import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_MAX_CHUNKS,
    Diagnosis,
    EvidenceChunk,
    Publication,
    build_evidence_chunks,
    default_title,
    dropped_as_missing,
    render_issue_body,
)
from theforge.reporting.target_gate import (
    TargetGateError,
    TargetGateVerdict,
    evaluate_target_gate,
)

GH_TIMEOUT_SECONDS = 60
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$")


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
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=GH_TIMEOUT_SECONDS,
    )


def _gh_error(proc: subprocess.CompletedProcess[str], fallback: str) -> str:
    return proc.stderr.strip() or proc.stdout.strip() or fallback


def _read_description(args: argparse.Namespace) -> str | None:
    if args.description_file:
        path = Path(args.description_file)
        if not path.is_file():
            print(f"--description-file: file not found: {path}", file=sys.stderr)
            return None
        return path.read_text(encoding="utf-8")
    if args.description == "-":
        return sys.stdin.read()
    return args.description or ""


def _build_diagnosis(args: argparse.Namespace, description: str) -> Diagnosis:
    symptom = args.symptom or description
    kwargs: dict[str, str] = {"symptom": symptom}
    if args.cause:
        kwargs["cause"] = args.cause
    if args.code_path:
        kwargs["code_path"] = args.code_path
    if args.fix_criterion:
        kwargs["fix_criterion"] = args.fix_criterion
    return Diagnosis(**kwargs)


def _evaluate_gate(
    *, target: str, title: str, body: str, labels: list[str]
) -> TargetGateVerdict | None:
    """Return the *target repository's* verdict, or ``None`` when unobtainable.

    ``None`` is not a soft failure — the caller refuses to file rather than
    create an issue whose gate state nobody can name. There is deliberately no
    fallback to this checkout's own gate: a local verdict would name a state
    the target repository does not hold.
    """
    try:
        return evaluate_target_gate(repo=target, title=title, body=body, labels=labels)
    except TargetGateError as exc:
        print(f"{target}'s shape gate could not be evaluated: {exc}", file=sys.stderr)
        return None


def _write_temp(text: str) -> Path:
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(text)
        handle.flush()
        return Path(handle.name)


def _create_issue(
    *, target: str, title: str, body: str, labels: list[str], cwd: Path
) -> str | None:
    path = _write_temp(body)
    try:
        command = ["gh", "issue", "create", "--repo", target, "--title", title]
        command += ["--body-file", str(path)]
        for label in labels:
            command += ["--label", label]
        proc = _gh(command, cwd)
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        print(_gh_error(proc, "gh issue create failed"), file=sys.stderr)
        return None
    for line in reversed(proc.stdout.strip().splitlines()):
        if "/issues/" in line:
            return line.strip()
    print("gh issue create produced no issue URL", file=sys.stderr)
    return None


def _post_chunk(*, target: str, issue: str, chunk: EvidenceChunk, cwd: Path) -> bool:
    path = _write_temp(chunk.body)
    try:
        proc = _gh(
            ["gh", "issue", "comment", issue, "--repo", target, "--body-file", str(path)],
            cwd,
        )
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        print(
            f"failed to attach evidence comment '{chunk.label}': "
            f"{_gh_error(proc, 'gh issue comment failed')}",
            file=sys.stderr,
        )
        return False
    return True


def _update_body(*, target: str, issue: str, body: str, cwd: Path) -> bool:
    path = _write_temp(body)
    try:
        proc = _gh(
            ["gh", "issue", "edit", issue, "--repo", target, "--body-file", str(path)],
            cwd,
        )
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        print(
            "failed to update the report's publication state: "
            f"{_gh_error(proc, 'gh issue edit failed')}",
            file=sys.stderr,
        )
        return False
    return True


def _print_gate(verdict: TargetGateVerdict) -> None:
    """Print the verdict and its reasons, naming the gate revision that ruled."""
    print(f"shape gate    : {verdict.verdict} (target gate {verdict.source})")
    for reason in verdict.reasons:
        severity = f"[{reason.severity}] " if reason.severity else ""
        print(f"  - {severity}{reason.code}: {reason.detail or ''}".rstrip())


def _print_summary(evidence: RunEvidence, chunks: tuple[EvidenceChunk, ...]) -> None:
    print(f"run           : {evidence.run_id} ({evidence.run_kind} run)")
    print(f"observed in   : {evidence.observed_project or 'unrecorded (no git origin)'}")
    print(f"forge version : {evidence.forge_version or 'unrecorded in this run'}")
    print(f"artifacts     : {', '.join(evidence.available_labels()) or 'none'}")
    if evidence.missing:
        print("missing       :")
        for entry in evidence.missing:
            print(f"  - {entry.display}")
    else:
        print("missing       : none")
    print(f"payload       : {len(chunks)} evidence comment(s)")


def cmd_report(args: argparse.Namespace) -> int:
    project_root = _project_root(args)
    if project_root is None:
        return 1

    target = args.to
    if not _REPO_RE.match(target):
        print(f"--to: expected owner/repo, got {target!r}", file=sys.stderr)
        return 1

    description = _read_description(args)
    if description is None:
        return 1
    if not description.strip():
        print(
            "forge report: describe what you observed with --description, "
            "--description-file, or --description -",
            file=sys.stderr,
        )
        return 1

    try:
        evidence = collect_run_evidence(project_root, args.run)
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    chunks, dropped = build_evidence_chunks(
        evidence, chunk_chars=DEFAULT_CHUNK_CHARS, max_chunks=args.max_comments
    )
    if dropped:
        evidence = evidence.with_missing(dropped_as_missing(dropped, args.max_comments))

    title = args.title or default_title(evidence, args.symptom or description)
    labels = list(dict.fromkeys(["bug", *(args.label or [])]))
    diagnosis = _build_diagnosis(args, description)
    publication = Publication(expected=tuple(chunk.label for chunk in chunks))
    body = render_issue_body(
        evidence, description=description, diagnosis=diagnosis, publication=publication
    )

    verdict = _evaluate_gate(target=target, title=title, body=body, labels=labels)
    if verdict is None:
        print(
            f"refusing to file: the report body cannot be placed in {target}'s shape-gate state."
        )
        return 1
    _print_gate(verdict)
    _print_summary(evidence, chunks)

    if args.dry_run:
        print("\n--dry-run: nothing filed. Report body follows.\n")
        print(body)
        return 0

    issue_url = _create_issue(
        target=target, title=title, body=body, labels=labels, cwd=project_root
    )
    if issue_url is None:
        return 1
    match = _ISSUE_URL_RE.search(issue_url)
    issue_ref = match.group(1) if match else issue_url

    posted: list[str] = []
    failed: list[str] = []
    for chunk in chunks:
        if _post_chunk(target=target, issue=issue_ref, chunk=chunk, cwd=project_root):
            posted.append(chunk.label)
        else:
            failed.append(chunk.label)

    final = Publication(
        expected=publication.expected,
        posted=tuple(posted),
        started=True,
    )
    final_body = render_issue_body(
        evidence, description=description, diagnosis=diagnosis, publication=final
    )
    body_updated = _update_body(target=target, issue=issue_ref, body=final_body, cwd=project_root)

    print(f"filed         : {issue_url}")
    if failed:
        print(
            f"publication   : INCOMPLETE — {len(posted)} of {len(chunks)} "
            "evidence comments attached",
            file=sys.stderr,
        )
        for label in failed:
            print(f"  - not attached: {label}", file=sys.stderr)
        if not body_updated:
            print(
                "  - the report body still claims those comments are pending; update it by hand",
                file=sys.stderr,
            )
        return 1
    if not body_updated:
        print(
            "publication   : evidence attached, but the report body could not be "
            "updated to say so",
            file=sys.stderr,
        )
        return 1
    print(f"publication   : complete — {len(posted)} evidence comment(s) attached")
    return 0


def register_parser(subparsers: object) -> None:
    p = subparsers.add_parser(
        "report",
        help="File a forge bug in a target repository from the project that observed it",
    )
    p.add_argument("--run", required=True, help="Run id observed in this project")
    p.add_argument("--to", required=True, help="Target repository as owner/repo")
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument("--title", help="Issue title (default: derived from the description)")
    p.add_argument(
        "--description",
        help="What you observed; '-' reads it from stdin",
    )
    p.add_argument(
        "--description-file",
        dest="description_file",
        help="Path to a file holding the description",
    )
    p.add_argument("--symptom", help="Observed symptom for the Diagnosis section")
    p.add_argument(
        "--cause",
        help="Confirmed cause, when known (default: an explicit non-assertion)",
    )
    p.add_argument(
        "--code-path",
        dest="code_path",
        help="Affected code path, when known (default: an explicit non-assertion)",
    )
    p.add_argument(
        "--fix-criterion",
        dest="fix_criterion",
        help="Observable condition that would mean the defect is fixed",
    )
    p.add_argument(
        "--label",
        action="append",
        help="Additional label for the created issue (repeatable; 'bug' is always applied)",
    )
    p.add_argument(
        "--max-comments",
        dest="max_comments",
        type=int,
        default=DEFAULT_MAX_CHUNKS,
        help=f"Cap on evidence comments (default: {DEFAULT_MAX_CHUNKS})",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print the gate verdict and rendered body without filing anything",
    )
    p.set_defaults(func=cmd_report)


__all__ = ["cmd_report", "register_parser"]
