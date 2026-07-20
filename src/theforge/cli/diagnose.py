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
from theforge.coordinator.log_tee import set_worker_slug
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.diagnose_types import DIAGNOSE_OUTPUT_DESTINATIONS
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level


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
    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    if args.interactive and args.autonomous:
        print("--interactive and --autonomous are mutually exclusive", file=sys.stderr)
        return 1
    if args.interactive:
        interactive = True
    elif args.autonomous:
        interactive = False
    else:
        interactive = not config.diagnose.autonomous_default

    timeout_override = getattr(args, "timeout", None)
    if timeout_override is not None and timeout_override <= 0:
        print(f"--timeout: must be > 0, got {timeout_override}", file=sys.stderr)
        return 1

    # ── Concurrency ────────────────────────────────────────────────────
    # Per-issue diagnosis is independent (fresh-context investigative agent,
    # no shared mutable state), so it parallelizes cleanly under a worker cap
    # that mirrors ``forge sprint --parallel``. Default (no flag) stays serial.
    max_parallel: int | None = getattr(args, "parallel", None)
    if max_parallel is not None and max_parallel < 1:
        print(f"--parallel: must be >= 1, got {max_parallel}", file=sys.stderr)
        return 1
    effective_parallel = 1 if max_parallel is None else max_parallel
    # Interactive mode confirms each landing on stdin; concurrent stdin prompts
    # would interleave unreadably, so it always runs serially.
    if interactive and effective_parallel > 1:
        print(
            "[forge] diagnose: --parallel is ignored in interactive mode; running issues serially",
            file=sys.stderr,
        )
        effective_parallel = 1

    def _diagnose_one(number: int, *, tagged: bool) -> bool:
        # Tag every log line from this worker with its issue number so
        # interleaved parallel output stays attributable.
        if tagged:
            set_worker_slug(f"#{number}")
        print(f"[forge] diagnose: starting issue #{number}", file=sys.stderr)
        result = run_diagnose_flow(
            issue_number=number,
            config=config,
            project_root=config.project_root,
            interactive=interactive,
            output_destination=destination,
            dry_run=args.dry_run,
            timeout_seconds=timeout_override,
        )
        icon = "✓" if result.success else "✗"
        _cost = result.state.agent_cost_usd
        cost_str = f"${_cost:.3f}" if _cost is not None else "unknown"
        print(
            f"[forge] {icon} #{number}: {result.message}  "
            f"(phase={result.state.phase.name}, cost={cost_str}, "
            f"duration={result.state.agent_duration_s:.1f}s)",
            file=sys.stderr,
        )
        return result.success

    overall_ok = _run_diagnoses(issues, _diagnose_one, effective_parallel)
    return 0 if overall_ok else 1


def _run_diagnoses(
    issues: list[int],
    diagnose_one: "callable",
    effective_parallel: int,
) -> bool:
    """Run ``diagnose_one(number, tagged=...)`` for each issue; return overall OK.

    Serial when ``effective_parallel <= 1`` (preserving pre-feature behavior,
    output untagged); otherwise a bounded ``ThreadPoolExecutor`` runs up to
    ``effective_parallel`` investigations concurrently with per-issue log tags.
    """
    if effective_parallel <= 1 or len(issues) <= 1:
        overall_ok = True
        for number in issues:
            if not diagnose_one(number, tagged=False):
                overall_ok = False
        return overall_ok

    from concurrent.futures import ThreadPoolExecutor, as_completed

    overall_ok = True
    max_workers = min(effective_parallel, len(issues))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(diagnose_one, number, tagged=True): number for number in issues}
        for future in as_completed(futures):
            number = futures[future]
            try:
                if not future.result():
                    overall_ok = False
            except Exception as exc:  # noqa: BLE001 — surface, don't abort the batch
                print(f"[forge] ✗ #{number}: diagnose crashed: {exc}", file=sys.stderr)
                overall_ok = False
    return overall_ok


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
        "--parallel",
        metavar="N",
        type=int,
        default=None,
        help="Maximum concurrent issue diagnoses (default: serial)",
    )
    p.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help=(
            "Per-invocation timeout override in seconds "
            "(overrides forge.yaml diagnose.timeout_seconds)"
        ),
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
