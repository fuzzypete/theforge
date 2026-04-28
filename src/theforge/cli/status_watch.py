"""Live-update (`--watch`) mode for `forge status`.

Re-renders the sprint status table on a fixed interval using ANSI cursor
positioning so the operator can monitor a running sprint without re-issuing
the command. Per-row deltas surface cost-change since the last refresh and
time since the most recent audit event; a spinner distinguishes running
stories that are emitting events from ones that have stalled.

Non-TTY output (pipes, redirects, CI captures) falls back to the
single-snapshot path — the watch loop is opt-in and TTY-only.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from pathlib import Path
from typing import Any

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
STALL_CHAR = "·"
DONE_CHAR = "✓"
FAIL_CHAR = "✗"
WAIT_CHAR = " "

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_SCREEN = "\x1b[2J"
CURSOR_HOME = "\x1b[H"

GREEN = "\x1b[32m"
RED = "\x1b[31m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"


def is_tty(stream: Any = None) -> bool:
    """Return True when stream is a real terminal."""
    if stream is None:
        stream = sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def color_enabled(no_color_flag: bool, stream: Any = None) -> bool:
    """Decide whether to emit ANSI color sequences."""
    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return is_tty(stream)


def _c(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{RESET}" if color else text


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def _format_delta(delta: float) -> str:
    if abs(delta) < 0.005:
        return "—"
    sign = "+" if delta > 0 else "-"
    return f"{sign}${abs(delta):.2f}"


def _last_audit_mtime(slug: str, project_root: Path) -> float | None:
    """Most recent mtime of any per-story audit.yaml under .forge/logs/*/<slug>/."""
    if not slug:
        return None
    logs_dir = project_root / ".forge" / "logs"
    if not logs_dir.exists():
        return None
    best: float | None = None
    for audit in logs_dir.glob(f"*/{slug}/audit.yaml"):
        try:
            m = audit.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def render_frame(
    run_id: str,
    project_root: Path,
    state: dict,
    frame_idx: int,
    *,
    color: bool,
    now_fn: Any = None,
) -> str:
    """Return the full text for one watch-mode frame.

    Mutates ``state`` to record per-slug cost and audit-event timestamps so the
    next frame can compute deltas. ``state`` should be reused across frames.
    """
    from theforge.cli.sprint_status import display_sprint_status
    from theforge.sprint.status_reader import read_live_status

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        display_sprint_status(run_id, project_root)
    base = buf.getvalue()

    entries = read_live_status(run_id, project_root) or []
    now = (now_fn or time.time)()
    spinner = SPINNER_FRAMES[frame_idx % len(SPINNER_FRAMES)]
    interval = float(state.get("interval", 2.0))
    stall_threshold = max(5.0, interval * 2)

    prev_costs = dict(state.get("costs", {}))
    new_costs: dict[str, float] = {}

    overlay_lines: list[str] = []
    overlay_lines.append("")
    title = f"── Live  refresh={interval:g}s  Ctrl-C to exit ──"
    overlay_lines.append(_c(title, DIM, color))
    overlay_lines.append(f"{'STORY':<30}  {'ACT':<3}  {'ΔCOST':>8}  {'EVT AGE':>8}")

    for e in entries:
        slug = getattr(e, "slug", "") or ""
        cost = float(getattr(e, "cost_usd", 0.0) or 0.0)
        prev = prev_costs.get(slug, cost)
        delta = cost - prev
        new_costs[slug] = cost

        last_evt = _last_audit_mtime(slug, project_root)
        if last_evt is None:
            age_str = "—"
            stalled = False
        else:
            age = max(0.0, now - last_evt)
            age_str = _format_age(age)
            stalled = age > stall_threshold

        status = getattr(e, "status", "waiting")
        if status == "running":
            if stalled:
                act = _c(STALL_CHAR, DIM, color)
            else:
                act = _c(spinner, GREEN, color)
        elif status == "done":
            act = _c(DONE_CHAR, GREEN, color)
        elif status == "failed":
            act = _c(FAIL_CHAR, RED, color)
        elif status == "skipped" or status == "blocked":
            act = _c(STALL_CHAR, DIM, color)
        else:
            act = WAIT_CHAR

        delta_str = _format_delta(delta)
        if color and delta != 0 and abs(delta) >= 0.005:
            delta_str = _c(delta_str, DIM, True)

        path = getattr(e, "path", slug) or slug
        path_disp = path if len(path) <= 30 else path[:29] + "…"
        # ``act`` may contain ANSI codes; pad based on visible width (1 char).
        act_pad = act + " " * 2
        overlay_lines.append(f"{path_disp:<30}  {act_pad}  {delta_str:>8}  {age_str:>8}")

    state["costs"] = new_costs
    return base + "\n".join(overlay_lines) + "\n"


def run_watch_loop(
    run_id: str,
    project_root: Path,
    interval: float,
    *,
    color: bool,
    sleep_fn: Any = None,
    max_frames: int | None = None,
) -> int:
    """Drive the watch redraw loop.

    Returns 0 on clean exit (Ctrl-C or max_frames reached) and non-zero if the
    sprint state cannot be read at all on the first attempt.
    """
    sleep = sleep_fn or time.sleep

    state: dict = {"costs": {}, "interval": float(interval)}
    frame = 0
    cursor_hidden = False
    try:
        if color or is_tty():
            sys.stdout.write(HIDE_CURSOR)
            sys.stdout.flush()
            cursor_hidden = True
        while True:
            try:
                body = render_frame(run_id, project_root, state, frame, color=color)
            except Exception as exc:  # noqa: BLE001
                if frame == 0:
                    print(
                        f"forge status --watch: failed to read sprint state: {exc}",
                        file=sys.stderr,
                    )
                    return 1
                # Transient failure mid-loop — keep the previous frame on screen.
                body = None

            if body is not None:
                sys.stdout.write(CLEAR_SCREEN + CURSOR_HOME + body)
                sys.stdout.flush()

            frame += 1
            if max_frames is not None and frame >= max_frames:
                return 0
            try:
                sleep(interval)
            except KeyboardInterrupt:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if cursor_hidden:
            try:
                sys.stdout.write(SHOW_CURSOR)
                sys.stdout.flush()
            except Exception:
                pass
