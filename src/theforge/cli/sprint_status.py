"""forge sprint-status subcommand — per-story phase display for a sprint run."""

from __future__ import annotations

import shutil
import sys
import textwrap
from pathlib import Path


def display_sprint_status(run_id: str, project_root: Path) -> int:
    """Display per-story status for a sprint run.

    Handles live (PID file present), completed (sprint-summary.yaml), and
    crashed (PID gone, .state still present) sprints.
    Returns 0 on success, 1 if no sprint data is found.
    """
    import yaml

    from theforge.sprint.status_reader import (
        find_live_state_path,
        find_sprint_summary,
        read_completed_status,
        read_live_status,
    )

    # Determine if the sprint is live (PID file present).
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    is_live = pid_file.exists()

    entries = None
    sprint_name = ""
    unexpected_end = False
    total_cost_usd: float | None = None
    duration_seconds: float | None = None
    sprint_phase: str | None = None
    base_branch: str | None = None
    budget_usd: float | None = None
    max_parallel: int | None = None

    if is_live:
        entries = read_live_status(run_id, project_root)
        state_path = find_live_state_path(run_id, project_root)
        if state_path is not None:
            sprint_name = _read_sprint_name_from_state(state_path)
            meta = _read_sprint_meta_from_state(state_path)
            sprint_phase = meta.get("sprint_phase")
            base_branch = meta.get("base_branch")
            budget_usd = meta.get("budget_usd")
            max_parallel = meta.get("max_parallel")
        # Approximate elapsed from the process start time via detach
        try:
            from theforge import detach as _detach

            parsed = _detach._read_pid_file(pid_file)
            if parsed:
                _, slug = parsed
                run_st = _detach.read_run_status(run_id, slug, project_root)
                elapsed_s = run_st.get("elapsed_seconds")
                if elapsed_s is not None:
                    duration_seconds = elapsed_s
        except Exception:
            pass
    else:
        # PID file gone — check if state file still exists (unexpected exit).
        state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
        if state_path.exists():
            unexpected_end = True
            entries = read_live_status(run_id, project_root)
            if entries is not None:
                sprint_name = _read_sprint_name_from_state(state_path)

    # For crashed sprints with an unreadable state file, show the banner with
    # an empty story list rather than falling through to sprint-summary (which
    # won't exist for crashed sprints).
    if entries is None and unexpected_end:
        entries = []

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
        # Read aggregate metrics from the completed summary.
        try:
            with open(summary_path, encoding="utf-8") as f:
                summary_data = yaml.safe_load(f) or {}
            sp = summary_data.get("sprint", {})
            total_cost_usd = sp.get("total_cost_usd")
            duration_seconds = sp.get("duration_seconds")
        except Exception:
            pass

    # For live/crashed sprints compute total cost from story entries.
    if total_cost_usd is None and entries:
        total_cost_usd = sum(getattr(e, "cost_usd", 0.0) for e in entries)

    # ── Header ───────────────────────────────────────────────────────────
    if is_live:
        state_label = "live"
    elif unexpected_end:
        state_label = "crashed"
    else:
        state_label = "completed"

    header_parts: list[str] = [f"Sprint: {sprint_name}  run: {run_id}  [{state_label}]"]
    if sprint_phase:
        header_parts.append(f"phase: {sprint_phase}")
    if total_cost_usd is not None:
        header_parts.append(f"cost: ${total_cost_usd:.2f}")
    if duration_seconds is not None:
        if is_live:
            header_parts.append(f"elapsed: {int(duration_seconds // 60)}m")
        else:
            header_parts.append(f"duration: {int(duration_seconds // 60)}m")
    print("  ".join(header_parts))

    # Second line: sprint configuration context (base branch, budget, parallel).
    # These let the operator confirm the sprint launched against the right
    # branch and ceiling without scrolling back to the launch banner.
    config_parts: list[str] = []
    if base_branch:
        config_parts.append(f"base: {base_branch}")
    if budget_usd is not None:
        config_parts.append(f"budget: ${float(budget_usd):.2f}")
    if max_parallel is not None:
        config_parts.append(f"parallel: {max_parallel}")
    if config_parts:
        print("  ".join(config_parts))

    if unexpected_end:
        print("  ⚠  Sprint ended unexpectedly (PID file missing, state file present)")
    print()

    if not entries:
        print("  No stories found.")
        return 0

    # ── Story rows ───────────────────────────────────────────────────────
    status_icons = {
        "done": "✓",
        "running": "▸",
        "waiting": "○",
        "failed": "✗",
        "skipped": "⊘",
        "blocked": "⊘",
    }

    # Column header
    header = (
        f"  {'STORY':<28}  {'STATUS':<8}  {'PHASE':<12}  {'MODEL':<24}  {'STAGE':<16}  "
        f"{'COMPLEXITY':<10}  {'COST':>7}  {'ELAPSED':>7}  DETAIL"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

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


def cmd_sprint_status(args: object) -> int:
    """Show per-story status for a sprint run."""
    from theforge.cli.shared import _find_config
    from theforge.config import load_config

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

    return display_sprint_status(run_id, project_root)


def _read_sprint_meta_from_state(state_path: object) -> dict:
    """Return sprint-level metadata (phase, base_branch, budget, parallel).

    Empty dict on any error or missing fields. Lets the live-status header
    surface watch-mode context during the pre-preflight window when the
    SprintStateWriter has not yet replaced the bootstrap state file.
    """
    import yaml  # noqa: PLC0415

    try:
        with open(Path(str(state_path)), encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    if isinstance(data.get("sprint_phase"), str):
        out["sprint_phase"] = data["sprint_phase"]
    if isinstance(data.get("base_branch"), str):
        out["base_branch"] = data["base_branch"]
    if isinstance(data.get("budget_usd"), (int, float)):
        out["budget_usd"] = float(data["budget_usd"])
    if isinstance(data.get("max_parallel"), int):
        out["max_parallel"] = data["max_parallel"]
    return out


def _read_sprint_name_from_state(state_path: object) -> str:
    """Read sprint_name from a .state YAML file.  Returns '' on any error."""
    import yaml  # noqa: PLC0415

    try:
        with open(Path(str(state_path)), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("sprint_name", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _read_sprint_name_from_summary(summary_path: object) -> str:
    """Read sprint.name from a sprint-summary.yaml file.  Returns '' on any error."""
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
    stage = getattr(entry, "stage", "")
    cost_usd = getattr(entry, "cost_usd", 0.0)
    elapsed_s = getattr(entry, "elapsed_seconds", None)
    detail = getattr(entry, "detail", "")
    complexity = getattr(entry, "complexity", None)
    complexity_score = getattr(entry, "complexity_score", None)
    model = getattr(entry, "model", None)

    phase_str = phase if phase else "—"
    stage_str = stage if stage else "—"
    if isinstance(complexity_score, int):
        complexity_str = str(complexity_score)
    elif complexity:
        complexity_str = complexity
    else:
        complexity_str = "—"
    model_str = model if model else "—"
    if len(model_str) > 24:
        model_str = model_str[:24]
    cost_str = f"${cost_usd:.2f}" if cost_usd else "   —"
    elapsed_str = _format_story_elapsed(elapsed_s)

    prefix = " " * indent
    detail_width = _detail_column_width(indent)
    path_lines = _wrap_cell(path, 28)
    phase_lines = _wrap_cell(phase_str, 12)
    model_lines = _wrap_cell(model_str, 24)
    stage_lines = _wrap_cell(stage_str, 16)
    detail_lines = _wrap_cell(detail if detail else "—", detail_width)

    line_count = max(
        len(path_lines),
        len(phase_lines),
        len(model_lines),
        len(stage_lines),
        len(detail_lines),
    )
    for index in range(line_count):
        icon_cell = icon if index == 0 else " "
        status_cell = status if index == 0 else ""
        complexity_cell = complexity_str if index == 0 else ""
        cost_cell = cost_str if index == 0 else ""
        elapsed_cell = elapsed_str if index == 0 else ""
        line = (
            f"{icon_cell} {path_lines[index] if index < len(path_lines) else '':<28}  "
            f"{status_cell:<8}  "
            f"{phase_lines[index] if index < len(phase_lines) else '':<12}  "
            f"{model_lines[index] if index < len(model_lines) else '':<24}  "
            f"{stage_lines[index] if index < len(stage_lines) else '':<16}  "
            f"{complexity_cell:<10}  {cost_cell:>7}  {elapsed_cell:>7}  "
            f"{detail_lines[index] if index < len(detail_lines) else ''}"
        )
        print(f"{prefix}{line}")


def _format_story_elapsed(elapsed_s: float | None) -> str:
    """Format story elapsed time for display."""
    if elapsed_s is None:
        return "—"
    total_seconds = max(0, int(elapsed_s))
    if total_seconds >= 3600:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    return f"{total_seconds // 60}m"


def _detail_column_width(indent: int) -> int:
    """Return an adaptive width for DETAIL so values wrap instead of truncating."""
    terminal_width = shutil.get_terminal_size((140, 20)).columns
    fixed_width = indent + 2 + 28 + 2 + 8 + 2 + 12 + 2 + 24 + 2 + 16 + 2 + 10 + 2 + 7 + 2 + 7 + 2
    return max(20, terminal_width - fixed_width)


def _wrap_cell(value: str, width: int) -> list[str]:
    """Wrap a cell value to the requested width without dropping content."""
    if width <= 0:
        return [value] if value else [""]
    wrapped = textwrap.wrap(
        value,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapped or [""]


def register_parser(subparsers: object) -> None:
    """Register the 'sprint-status' subcommand parser."""
    p = subparsers.add_parser(
        "sprint-status",
        help="Show per-story status for a sprint run",
    )
    p.add_argument("run_id", help="Sprint run ID (from 'forge status')")
