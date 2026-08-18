"""forge ideate subcommand — multi-LLM deliberation to generate a story."""

from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

import yaml

from theforge.cli.shared import _find_config, load_config_checked
from theforge.config import load_config
from theforge.ideate import generate_ideation_audit, run_ideation
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level


def cmd_ideate(args: object) -> int:
    """Run multi-LLM deliberation to generate a story from a brief."""
    # Load brief from file or inline string
    brief_arg = args.brief
    brief_path = Path(brief_arg)
    brief_is_file = brief_path.suffix in (".md", ".txt") and brief_path.exists()
    if brief_is_file:
        brief = brief_path.read_text(encoding="utf-8")
    else:
        brief = brief_arg

    # Find config — search from brief file's directory when brief is a file,
    # mirroring how cmd_run/cmd_sprint search relative to their input files.
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).resolve()
    elif brief_is_file:
        config_path = _find_config(brief_path.parent)
    else:
        config_path = _find_config()

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

    if getattr(args, "verbose", False):
        runner_set_log_level(LogLevel.VERBOSE)

    # Validate and cap rounds: must be in the inclusive range 1..3.
    if args.rounds < 1 or args.rounds > 3:
        print(
            f"--rounds must be between 1 and 3 (got {args.rounds})",
            file=sys.stderr,
        )
        return 1
    max_rounds = args.rounds

    dry_run: bool = args.dry_run

    # Compute output_path and stories_dir once before calling run_ideation.
    # dry-run → no file written (both None); explicit --output → output_path set;
    # default → stories_dir set so run_ideation derives the slug-based filename.
    run_output_path: Path | None = None
    run_stories_dir: Path | None = None
    if not dry_run:
        if args.output:
            run_output_path = Path(args.output).resolve()
        else:
            run_stories_dir = config.project_root / "stories"

    ideation_started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ideation_start_mono = time.monotonic()

    try:
        result = run_ideation(
            config, brief, run_output_path, stories_dir=run_stories_dir, max_rounds=max_rounds
        )
    except ValueError as exc:
        print(f"Ideation error: {exc}", file=sys.stderr)
        return 1

    duration_seconds = time.monotonic() - ideation_start_mono
    ideation_finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if dry_run:
        if not result.success:
            print(f"Ideation failed: {result.final_synthesis}", file=sys.stderr)
            return 1
        print(result.final_synthesis)
        return 0

    # Write audit record only for real (non-dry-run) runs.
    audit = generate_ideation_audit(
        config,
        brief,
        result,
        started_at=ideation_started_at,
        finished_at=ideation_finished_at,
        duration_seconds=duration_seconds,
    )
    audits_dir = config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "forge_ideation_audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    print(f"[forge] Audit log: {audit_path}", file=sys.stderr)

    return 0 if result.success else 1


def register_parser(subparsers: object) -> None:
    """Register the 'ideate' subcommand parser."""
    ideate_parser = subparsers.add_parser(
        "ideate", help="Run multi-LLM deliberation to generate a story from a brief"
    )
    ideate_parser.add_argument(
        "brief",
        help=(
            "Brief text or path to a .md/.txt file containing the brief. "
            "If the argument ends in .md or .txt and the file exists, it is read as a file; "
            "otherwise it is treated as inline text."
        ),
    )
    ideate_parser.add_argument(
        "--output",
        help="Output path for generated story (default: stories/<slug>.md)",
    )
    ideate_parser.add_argument(
        "--rounds",
        type=int,
        default=2,
        help="Max deliberation rounds before surfacing residual divergence (default: 2, max: 3)",
    )
    ideate_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
    ideate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run deliberation and print synthesized spec to stdout without writing a spec file. "
            "LLM agents are still invoked; only the output spec file is skipped. "
            "An audit record is always written."
        ),
    )
    ideate_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )
