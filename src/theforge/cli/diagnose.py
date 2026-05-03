"""``forge diagnose`` — root cause discovery for symptom bugs.

Distinct from ``forge sprint``: takes one or more issue references, runs an
investigative agent against each, and lands a structured diagnosis artifact
that makes the issue fix-ready.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.config import load_config
from theforge.coordinator.diagnose_flow import run_diagnose_flow
from theforge.diagnose_types import DIAGNOSE_OUTPUT_DESTINATIONS


def _parse_issue_refs(refs: list[str]) -> list[int]:
    """Accept ``42``, ``#42``, ``42,43``, or repeated --issue flags."""
    numbers: list[int] = []
    for raw in refs:
        for piece in raw.split(","):
            piece = piece.strip().lstrip("#")
            if not piece:
                continue
            try:
                numbers.append(int(piece))
            except ValueError as exc:
                raise SystemExit(f"--issue: {piece!r} is not a valid issue number") from exc
    if not numbers:
        raise SystemExit("--issue: at least one issue number is required")
    return numbers


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Run the diagnose flow against one or more issues."""
    issues = _parse_issue_refs(args.issue or [])

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

    destination = args.output_destination or config.diagnose.output_destination
    if destination not in DIAGNOSE_OUTPUT_DESTINATIONS:
        print(
            f"--output: {destination!r} is not a valid destination. "
            f"Valid: {sorted(DIAGNOSE_OUTPUT_DESTINATIONS)}",
            file=sys.stderr,
        )
        return 1

    # Mode selection: interactive (operator-in-the-loop) or autonomous.
    # Default follows config.diagnose.autonomous_default; --interactive forces
    # the operator-in-the-loop path; --autonomous forces autonomous.
    if args.interactive and args.autonomous:
        print("--interactive and --autonomous are mutually exclusive", file=sys.stderr)
        return 1
    if args.interactive:
        interactive = True
    elif args.autonomous:
        interactive = False
    else:
        interactive = not config.diagnose.autonomous_default

    overall_ok = True
    for number in issues:
        print(f"[forge] diagnose: starting issue #{number}", file=sys.stderr)
        result = run_diagnose_flow(
            issue_number=number,
            config=config,
            project_root=config.project_root,
            interactive=interactive,
            output_destination=destination,
            dry_run=args.dry_run,
        )
        icon = "✓" if result.success else "✗"
        print(
            f"[forge] {icon} #{number}: {result.message}  "
            f"(phase={result.state.phase.name}, cost=${result.state.agent_cost_usd:.3f}, "
            f"duration={result.state.agent_duration_s:.1f}s)",
            file=sys.stderr,
        )
        if not result.success:
            overall_ok = False

    return 0 if overall_ok else 1


def register_parser(subparsers: object) -> None:
    """Register the ``diagnose`` subcommand parser."""
    p = subparsers.add_parser(
        "diagnose",
        help="Run a separate root-cause investigation flow for symptom bugs",
    )
    p.add_argument(
        "--issue",
        action="append",
        required=True,
        help="Issue number(s) to diagnose; comma-separated or repeat the flag",
    )
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument(
        "--output",
        dest="output_destination",
        choices=sorted(DIAGNOSE_OUTPUT_DESTINATIONS),
        default=None,
        help=(
            "Where to land the diagnosis artifact "
            "(overrides forge.yaml diagnose.output_destination)"
        ),
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Operator-in-the-loop: confirm before landing the artifact",
    )
    p.add_argument(
        "--autonomous",
        action="store_true",
        default=False,
        help="Land the artifact without operator confirmation (overrides config default)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run the investigation but do not actually land the artifact",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )
