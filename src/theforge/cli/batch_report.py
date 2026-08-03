"""``forge batch-report`` — post-sprint batchability analytics.

Thin command layer over :mod:`theforge.sprint.batch_report`: resolve config and
project root, locate the sprint summary, build the pure report, render it. All
analysis lives in the sprint module so the same report backs the terminal view,
the structured payload, and tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.config import load_config


def cmd_batch_report(args: argparse.Namespace) -> int:
    """Render the batchability report for a completed sprint run id."""
    import yaml  # noqa: PLC0415

    from theforge.sprint.batch_report import (  # noqa: PLC0415
        build_batch_report,
        render_terminal,
        report_payload,
    )
    from theforge.sprint.status_reader import find_sprint_summary  # noqa: PLC0415

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
    config = load_config(config_path)

    run_id = args.run_id
    summary_path = find_sprint_summary(run_id, config.project_root)
    if summary_path is None:
        print(
            f"No sprint data found for run ID '{run_id}'.\n"
            "Is the run ID correct? Use 'forge status' to see active runs.",
            file=sys.stderr,
        )
        return 1

    report = build_batch_report(
        summary_path,
        run_id=run_id,
        max_stories=args.max_stories,
        max_complexity_budget=args.max_complexity_budget,
        max_touched_files=args.max_touched_files,
    )
    if report is None:
        print(f"Sprint summary for run ID '{run_id}' is unreadable.", file=sys.stderr)
        return 1

    if args.format == "terminal":
        print(render_terminal(report), end="")
        return 0

    payload = report_payload(report)
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), end="")
    return 0


def register_parser(subparsers: object) -> None:
    """Register the ``batch-report`` subcommand parser."""
    p = subparsers.add_parser(
        "batch-report",
        help=(
            "Post-sprint analytics: which completed stories would have qualified "
            "for cost-aware batching, and would batching have been cheaper"
        ),
    )
    p.add_argument("run_id", help="Completed sprint run id to analyse")
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument(
        "--format",
        choices=("terminal", "yaml", "json"),
        default="terminal",
        help="Output format (default: terminal)",
    )
    # Analysis knobs, not the project's sprint.batch config: the report measures
    # the opportunity whether or not the project has opted into batching, and
    # varying these is how an operator runs a sensitivity analysis.
    from theforge.sprint.batch_report import (  # noqa: PLC0415
        DEFAULT_MAX_COMPLEXITY_BUDGET,
        DEFAULT_MAX_STORIES,
        DEFAULT_MAX_TOUCHED_FILES,
    )

    p.add_argument(
        "--max-stories",
        type=int,
        default=DEFAULT_MAX_STORIES,
        help=f"Hypothetical batch size ceiling (default: {DEFAULT_MAX_STORIES})",
    )
    p.add_argument(
        "--max-complexity-budget",
        type=int,
        default=DEFAULT_MAX_COMPLEXITY_BUDGET,
        help=f"Combined complexity budget per group (default: {DEFAULT_MAX_COMPLEXITY_BUDGET})",
    )
    p.add_argument(
        "--max-touched-files",
        type=int,
        default=DEFAULT_MAX_TOUCHED_FILES,
        help=f"Combined touched-file ceiling per group (default: {DEFAULT_MAX_TOUCHED_FILES})",
    )
