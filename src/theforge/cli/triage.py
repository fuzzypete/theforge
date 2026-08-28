"""``forge triage`` — backlog reporting, advisory proposals, and ratification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theforge.cli.shared import _find_config, load_config_checked
from theforge.config import load_config
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.triage_backlog_report import (
    TriageBacklogReportError,
    collect_backlog_report,
    render_backlog_report,
    write_backlog_report,
)
from theforge.triage_proposal import render_run_summary
from theforge.triage_ratification import TERMINAL_STATUSES, render_ratification_summary
from theforge.triage_shelved import (
    TRIAGE_PROPOSALS_SHELVED_EXIT_CODE,
    triage_proposals_shelved_lines,
)


def stdin_is_interactive() -> bool:
    """True when an operator is at the keyboard for this invocation.

    A headless run is defined by what it *cannot do* — prompt — so the test is
    on stdin rather than on a flag the caller might forget to pass: a cron entry
    and a post-sprint pass both arrive with a non-TTY stdin without knowing they
    should have declared themselves. Anything that makes ``isatty`` unavailable
    (a closed or replaced stdin, as in a captured test or a CI runner) is read
    as headless, which is the safe direction: the worst outcome is a persisted
    decision nobody needed, never an unattended prompt or an unattended apply.
    """
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _load_config_for_triage(args: argparse.Namespace) -> tuple[int, object | None]:
    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    config_path: Path | None = (
        Path(args.config).resolve() if args.config else _find_config(Path.cwd())
    )
    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1, None
    return 0, load_config_checked(
        config_path,
        loader=load_config,
        emit_startup_auth_warnings=False,
    )


def _config_current_milestone(config: object) -> str | None:
    milestone = getattr(
        getattr(getattr(config, "conventions_advisory", None), "issue_filing", None),
        "milestone",
        None,
    )
    if isinstance(milestone, str) and milestone.strip():
        return milestone.strip()
    return None


def _cmd_triage_report(args: argparse.Namespace, config: object) -> int:
    try:
        report = collect_backlog_report(
            config.project_root,
            current_milestone=args.current_milestone or _config_current_milestone(config),
        )
        output_path = Path(args.output).resolve() if args.output else None
        artifact_path = write_backlog_report(config.project_root, report, output_path=output_path)
    except TriageBacklogReportError as exc:
        print(f"[forge] triage: {exc}", file=sys.stderr)
        return 1
    print(render_backlog_report(report, artifact_path, config.project_root))
    return 0


def _reject_shelved_triage_proposals() -> int:
    for line in triage_proposals_shelved_lines():
        print(line, file=sys.stderr)
    return TRIAGE_PROPOSALS_SHELVED_EXIT_CODE


def _cmd_triage_proposals(args: argparse.Namespace, config: object, *, report: object) -> int:
    """Propose a disposition for every finding in a backlog report."""
    from theforge.coordinator.triage_proposal_flow import (  # noqa: PLC0415
        run_triage_proposals,
    )

    summary = run_triage_proposals(
        report,
        config,
        project_root=config.project_root,
        current_milestone=args.current_milestone,
        record=not args.no_audit,
    )
    print(render_run_summary(summary))
    return 1 if summary.run_level_failure else 0


def _cmd_triage_headless(
    args: argparse.Namespace,
    config: object,
    *,
    report: object | None,
) -> int:
    """Propose, review, and persist — the whole product of a run with no operator."""
    from theforge.coordinator.triage_headless_flow import (  # noqa: PLC0415
        HEADLESS_SUPERSEDED,
        HeadlessTriageError,
        run_headless_triage,
    )

    if args.no_audit:
        # A run recorded nowhere cannot be ratified later, so a headless run
        # invoked with --no-audit could only ever produce a package the operator
        # is unable to act on. Refuse before spending rather than after.
        print(
            "[forge] triage: --no-audit cannot be used without an operator present; "
            "a headless run must be recorded to be ratifiable later",
            file=sys.stderr,
        )
        return 1

    try:
        outcome = run_headless_triage(
            config,
            project_root=config.project_root,
            report=report,
            current_milestone=args.current_milestone,
            output_path=Path(args.output).resolve() if args.output else None,
        )
    except HeadlessTriageError as exc:
        print(f"[forge] triage: {exc}", file=sys.stderr)
        return 1

    if outcome.status == HEADLESS_SUPERSEDED or not outcome.ok:
        for line in outcome.lines:
            print(line, file=sys.stderr)
        if not outcome.lines:
            print(f"[forge] triage: {outcome.message}", file=sys.stderr)
        return 1
    for line in outcome.lines:
        print(line)
    return 0


def _cmd_triage_discard(args: argparse.Namespace, config: object) -> int:
    """Drop a persisted triage decision without applying any disposition."""
    from theforge import pending as _pending  # noqa: PLC0415

    entry = _pending.discard_triage_pending(args.discard, config.project_root)
    if entry is None:
        print(
            f"[forge] triage: no pending triage decision matches {args.discard!r}",
            file=sys.stderr,
        )
        return 1
    print(
        f"Discarded pending triage decision {entry.get('run_id')} "
        f"({int(entry.get('findings_count') or 0)} finding(s)); no disposition was applied."
    )
    return 0


def _cmd_triage_ratify(args: argparse.Namespace, config: object) -> int:
    from theforge import pending as _pending  # noqa: PLC0415
    from theforge.coordinator.triage_ratification_flow import (  # noqa: PLC0415
        TriageRatificationError,
        ratify_triage_run,
    )

    if not stdin_is_interactive():
        # Refused before prompting and before any application: ratification is
        # the operator's act, and a non-interactive ratify would either block on
        # a prompt nobody can answer or apply what nobody ratified.
        print(
            "[forge] triage: --ratify requires an interactive terminal; a headless "
            "invocation can only propose and persist a pending decision",
            file=sys.stderr,
        )
        return 1

    # An operator reads the triage_run_id off a run summary and the pending id
    # off `forge status`; both name the same run, so both are accepted.
    pending_entry = _pending.find_triage_pending(args.ratify, config.project_root)
    triage_run_id = (
        str(pending_entry.get("triage_run_id") or "") if pending_entry else ""
    ) or args.ratify

    try:
        summary = ratify_triage_run(triage_run_id, config, project_root=config.project_root)
    except TriageRatificationError as exc:
        print(f"[forge] triage: {exc}", file=sys.stderr)
        return 1
    print(render_ratification_summary(summary))

    if pending_entry is not None:
        _resolve_pending_after_ratification(config, pending_entry, summary)
    return 0


def _resolve_pending_after_ratification(
    config: object,
    pending_entry: dict,
    summary: object,
) -> None:
    """Remove the pending artifact only once every outcome is terminal.

    A ``ratified`` or ``failed`` finding is one the next ratification pass will
    pick up and try to apply again; deleting the pending record while any remain
    would take the run off the operator's surface with work still outstanding.
    """
    from theforge import pending as _pending  # noqa: PLC0415

    findings = getattr(summary, "findings", ()) or ()
    if any(getattr(f, "status", "") not in TERMINAL_STATUSES for f in findings):
        print(
            f"Pending triage decision {pending_entry.get('run_id')} kept — "
            "some findings are not terminal yet."
        )
        return
    _pending.discard_triage_pending(str(pending_entry.get("run_id") or ""), config.project_root)
    print(f"Pending triage decision {pending_entry.get('run_id')} resolved and removed.")


def _incompatible_flags(args: argparse.Namespace, flag: str) -> int:
    """Report flags that cannot accompany ``--ratify``/``--discard``; 0 when clean."""
    incompatible: list[str] = []
    if args.report:
        incompatible.append("--report")
    if args.output:
        incompatible.append("--output")
    if args.current_milestone:
        incompatible.append("--current-milestone")
    if args.no_audit:
        incompatible.append("--no-audit")
    if incompatible:
        print(
            f"[forge] triage: {flag} cannot be combined with " + ", ".join(incompatible),
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    """Dispatch ``forge triage`` in report, proposal, ratify, or discard mode."""
    code, config = _load_config_for_triage(args)
    if code != 0 or config is None:
        return code
    if args.discard:
        if _incompatible_flags(args, "--discard"):
            return 1
        return _cmd_triage_discard(args, config)
    if args.ratify:
        if _incompatible_flags(args, "--ratify"):
            return 1
        return _cmd_triage_ratify(args, config)

    headless = not stdin_is_interactive()
    if args.report:
        return _reject_shelved_triage_proposals()

    # No mode named. With an operator present this is the report-only stage they
    # read before choosing to propose. Without one there is nobody to read it
    # and nobody to run the second command, so the whole non-interactive
    # workflow — collect, report, propose, review, persist — runs here (#2231).
    if headless:
        return _reject_shelved_triage_proposals()
    return _cmd_triage_report(args, config)


def register_parser(subparsers: object) -> None:
    """Register the ``triage`` subcommand parser."""
    p = subparsers.add_parser(
        "triage",
        help=(
            "Generate a backlog report (no flag, interactive); disposition proposals from "
            "--report or headless runs are shelved by ADR-0010; ratify with --ratify or "
            "discard with --discard"
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--report",
        help=(
            "Path to a backlog report artifact to consume (JSON or YAML); consuming reports "
            "for disposition proposals is shelved by ADR-0010"
        ),
    )
    mode.add_argument(
        "--ratify",
        help=("Recorded triage_run_id (or pending decision id) to ratify and apply interactively"),
    )
    mode.add_argument(
        "--discard",
        help=(
            "Drop a persisted pending triage decision (by pending id or "
            "triage_run_id) without applying any disposition"
        ),
    )
    p.add_argument(
        "--output",
        help="Where to write the generated backlog report artifact (default: .forge/triage/...)",
    )
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument(
        "--current-milestone",
        dest="current_milestone",
        default=None,
        help=(
            "Milestone to stamp into a generated report; the proposal stage that would "
            "consume this override is shelved by ADR-0010"
        ),
    )
    p.add_argument(
        "--no-audit",
        action="store_true",
        default=False,
        help=(
            "Skip writing proposal runs to the audit substrate; report generation never "
            "audits, and proposal-producing paths are shelved by ADR-0010"
        ),
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )
