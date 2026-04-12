"""forge sprint-status subcommand — per-story phase display for a sprint run."""

from __future__ import annotations

import sys


def cmd_sprint_status(args: object) -> int:
    """Show per-story status for a sprint run."""
    from theforge.cli.shared import _find_config
    from theforge.config import load_config
    from theforge.sprint.status_reader import (
        find_sprint_summary,
        read_completed_status,
        read_live_status,
    )

    run_id: str = getattr(args, "run_id", "")
    if not run_id:
        print("run_id is required", file=sys.stderr)
        return 1

    config_path = _find_config()
    if config_path is None or not config_path.exists():
        print("forge.yaml not found.", file=sys.stderr)
        return 1
    config = load_config(config_path)
    project_root = config.project_root

    # Determine if the sprint is live (PID file present).
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    is_live = pid_file.exists()

    entries = None
    sprint_name = ""
    unexpected_end = False

    if is_live:
        entries = read_live_status(run_id, project_root)
        if entries is not None:
            sprint_name = _read_sprint_name_from_state(
                project_root / ".forge" / "runs" / f"{run_id}.state"
            )
    else:
        # PID file gone — check if state file still exists (unexpected exit)
        state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
        if state_path.exists():
            unexpected_end = True
            entries = read_live_status(run_id, project_root)
            if entries is not None:
                sprint_name = _read_sprint_name_from_state(state_path)

    # Fall back to completed sprint-summary.yaml
    if entries is None:
        summary_path = find_sprint_summary(run_id, project_root)
        if summary_path is None:
            print(
                f"No sprint data found for run ID '{run_id}'.\n"
                "Is the run ID correct? Use 'forge status' to see active runs.",
                file=sys.stderr,
            )
            return 1
        entries = read_completed_status(summary_path)
        sprint_name = _read_sprint_name_from_summary(summary_path)

    # ── Display ──────────────────────────────────────────────────────────
    status_icons = {
        "done": "✓",
        "running": "▸",
        "waiting": "○",
        "failed": "✗",
        "skipped": "⊘",
        "blocked": "⊘",
    }

    header = f"Sprint: {sprint_name}  (run: {run_id})"
    if is_live:
        header += "  [live]"
    print(header)
    if unexpected_end:
        print("  ⚠  Sprint ended unexpectedly (PID file missing, state file present)")
    print()

    if not entries:
        print("  No stories found.")
        return 0

    # Separate bundle candidates from regular stories
    bundle_entries = [e for e in entries if e.bundle_candidate]
    regular_entries = [e for e in entries if not e.bundle_candidate]

    if bundle_entries:
        bundle_slugs = "  ".join(e.slug for e in bundle_entries)
        print(f"[bundle: {bundle_slugs}]")
        for entry in bundle_entries:
            _print_story_line(entry, status_icons, indent=2)
        print()

    for entry in regular_entries:
        _print_story_line(entry, status_icons, indent=0)

    return 0


def _read_sprint_name_from_state(state_path: object) -> str:
    """Read sprint_name from a .state YAML file.  Returns '' on any error."""
    from pathlib import Path  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    try:
        with open(Path(str(state_path)), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("sprint_name", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _read_sprint_name_from_summary(summary_path: object) -> str:
    """Read sprint.name from a sprint-summary.yaml file.  Returns '' on any error."""
    from pathlib import Path  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    try:
        with open(Path(str(summary_path)), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data.get("sprint", {}).get("name", "")
        return ""
    except Exception:
        return ""


def _print_story_line(entry: object, status_icons: dict, indent: int) -> None:
    """Print a single story line for forge sprint-status."""
    icon = status_icons.get(getattr(entry, "status", "waiting"), "?")
    path = getattr(entry, "path", getattr(entry, "slug", ""))
    status = getattr(entry, "status", "waiting")
    phase = getattr(entry, "phase", None)
    cost_usd = getattr(entry, "cost_usd", 0.0)
    blocked_by = getattr(entry, "blocked_by", [])

    # Build the phase/status label
    if phase and status == "running":
        phase_label = phase
    elif status == "done":
        phase_label = "done"
    elif status == "failed":
        phase_label = phase or "ESCALATE"
    elif status == "skipped":
        phase_label = "skipped"
    elif status == "blocked":
        phase_label = "blocked"
    else:
        phase_label = "waiting"

    cost_str = f"${cost_usd:.2f}" if cost_usd else "    "
    line = f"{icon} {path:<30}  {phase_label:<14}  {cost_str}"
    if blocked_by:
        line += f"  (waiting on: {', '.join(blocked_by)})"
    prefix = " " * indent
    print(f"{prefix}{line}")


def register_parser(subparsers: object) -> None:
    """Register the 'sprint-status' subcommand parser."""
    p = subparsers.add_parser(
        "sprint-status",
        help="Show per-story status for a sprint run",
    )
    p.add_argument("run_id", help="Sprint run ID (from 'forge status')")
