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
from theforge.triage_ratification import render_ratification_summary
from theforge.triage_report import BacklogReportError, load_backlog_report


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


def _cmd_triage_proposals(args: argparse.Namespace, config: object) -> int:
    """Propose a disposition for every finding in a backlog report."""
    from theforge.coordinator.triage_proposal_flow import (  # noqa: PLC0415
        run_triage_proposals,
    )

    try:
        report = load_backlog_report(args.report)
    except BacklogReportError as exc:
        print(f"[forge] triage: {exc}", file=sys.stderr)
        return 1

    summary = run_triage_proposals(
        report,
        config,
        project_root=config.project_root,
        current_milestone=args.current_milestone,
        record=not args.no_audit,
    )
    print(render_run_summary(summary))
    return 0


def _cmd_triage_ratify(args: argparse.Namespace, config: object) -> int:
    from theforge.coordinator.triage_ratification_flow import (  # noqa: PLC0415
        TriageRatificationError,
        ratify_triage_run,
    )

    try:
        summary = ratify_triage_run(args.ratify, config, project_root=config.project_root)
    except TriageRatificationError as exc:
        print(f"[forge] triage: {exc}", file=sys.stderr)
        return 1
    print(render_ratification_summary(summary))
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    """Dispatch ``forge triage`` in report or proposal mode."""
    code, config = _load_config_for_triage(args)
    if code != 0 or config is None:
        return code
    if args.ratify:
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
                "[forge] triage: --ratify cannot be combined with " + ", ".join(incompatible),
                file=sys.stderr,
            )
            return 1
        return _cmd_triage_ratify(args, config)
    if args.report:
        return _cmd_triage_proposals(args, config)
    return _cmd_triage_report(args, config)


def register_parser(subparsers: object) -> None:
    """Register the ``triage`` subcommand parser."""
    p = subparsers.add_parser(
        "triage",
        help="Generate a backlog report, propose from --report, or ratify with --ratify",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--report",
        help="Path to a backlog report artifact to consume (JSON or YAML)",
    )
    mode.add_argument(
        "--ratify",
        help="Recorded triage_run_id to ratify and apply interactively",
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
            "Milestone a fix_now proposal must target "
            "(overrides the report's current_milestone; without either, "
            "fix_now is unavailable)"
        ),
    )
    p.add_argument(
        "--no-audit",
        action="store_true",
        default=False,
        help="Skip writing proposal runs to the audit substrate (report mode never audits)",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )
