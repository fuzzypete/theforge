"""``forge triage`` — evidence-backed disposition proposals for the backlog.

Thin command layer over :mod:`theforge.coordinator.triage_proposal_flow`:
resolve config, load the backlog report artifact, run the proposal stage,
render one proposal per finding with its spend. Every judgment — the taxonomy,
what counts as grounded, when a finding falls back to ``needs_verification`` —
lives in the flow and the schema module so the terminal view cannot diverge
from what was validated and recorded.

This command is advisory and read-only with respect to the tracker: it does not
edit, comment on, label, or close any issue, and it holds no code path that
could. Applying a proposal is a later slice of epic #1033.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theforge.cli.shared import _find_config, load_config_checked
from theforge.config import load_config
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.triage_proposal import render_run_summary
from theforge.triage_report import BacklogReportError, load_backlog_report


def cmd_triage(args: argparse.Namespace) -> int:
    """Propose a disposition for every finding in a backlog report."""
    from theforge.coordinator.triage_proposal_flow import (  # noqa: PLC0415
        run_triage_proposals,
    )

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
        return 1
    config = load_config_checked(
        config_path,
        loader=load_config,
        emit_startup_auth_warnings=False,
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


def register_parser(subparsers: object) -> None:
    """Register the ``triage`` subcommand parser."""
    p = subparsers.add_parser(
        "triage",
        help="Propose a disposition for each backlog-report finding (advisory only)",
    )
    p.add_argument(
        "--report",
        required=True,
        help="Path to the backlog report artifact (JSON or YAML)",
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
        help="Skip writing the run to the audit substrate (proposals are still printed)",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )
