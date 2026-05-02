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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from theforge.cli.status_run_helpers import live_sprint_run_ids as _live_sprint_run_ids

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
STALL_CHAR = "·"
DONE_CHAR = "✓"
FAIL_CHAR = "✗"
WAIT_CHAR = " "

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_BELOW = "\x1b[0J"  # erase from cursor to end of screen
CARRIAGE_RETURN = "\r"


def CURSOR_UP(n: int) -> str:
    """ANSI: move cursor up N lines (no-op when N<=0)."""
    return f"\x1b[{n}A" if n > 0 else ""


GREEN = "\x1b[32m"
RED = "\x1b[31m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

# Terminals that don't support ANSI color sequences.
COLORLESS_TERMS = frozenset({"dumb", "unknown", ""})
STALL_THRESHOLD_ENV = "FORGE_STATUS_WATCH_STALE_SECONDS"


def is_tty(stream: Any = None) -> bool:
    """Return True when stream is a real terminal."""
    if stream is None:
        stream = sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def color_enabled(no_color_flag: bool, stream: Any = None) -> bool:
    """Decide whether to emit ANSI color sequences.

    Disables color when:
    - ``--no-color`` was passed,
    - the ``NO_COLOR`` environment variable is set (https://no-color.org),
    - stdout is not a TTY,
    - or ``TERM`` indicates a colorless terminal (``dumb``, ``unknown``, unset).
    """
    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if not is_tty(stream):
        return False
    term = os.environ.get("TERM", "").strip().lower()
    if term in COLORLESS_TERMS:
        return False
    return True


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


def _stall_threshold_seconds(interval: float, override: float | None = None) -> float:
    if override is None:
        raw = os.environ.get(STALL_THRESHOLD_ENV, "").strip()
        if raw:
            try:
                override = float(raw)
            except ValueError:
                override = None
    if override is not None:
        return max(0.0, override)
    return max(5.0, interval * 2)


def _resolve_followed_run_id(run_id: str, project_root: Path) -> str:
    from theforge.sprint.status_reader import _follow_redirect_chain

    return _follow_redirect_chain(run_id, project_root)


def _resolve_sprint_log_dir(run_id: str, project_root: Path) -> Path | None:
    """Return the per-run log directory `.forge/logs/<sprint_name>/` for run_id.

    Resolution order:
    1. The live `.state` file at `.forge/runs/<run_id>.state` records
       ``sprint_name`` — that is the canonical mapping for in-flight runs.
    2. Otherwise, scan `.forge/logs/*/sprint-summary.yaml` for an entry whose
       ``sprint.run_id`` matches (covers completed sprints).

    Returns ``None`` when no directory can be resolved (e.g. logs not yet
    created on the very first frame). Restricting the audit-mtime lookup to
    this directory prevents stale events from previous sprint runs that
    happened to use the same story slug from leaking into the live display.
    """
    import yaml  # noqa: PLC0415

    logs_dir = project_root / ".forge" / "logs"
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    if state_path.exists():
        try:
            with open(state_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            sprint_name = data.get("sprint_name", "") if isinstance(data, dict) else ""
            if sprint_name:
                candidate = logs_dir / sprint_name
                if candidate.exists():
                    return candidate
        except Exception:
            pass

    if not logs_dir.exists():
        return None
    try:
        for summary in logs_dir.glob("*/sprint-summary.yaml"):
            try:
                with open(summary, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                sp = data.get("sprint", {})
                if isinstance(sp, dict) and sp.get("run_id") == run_id:
                    return summary.parent
            except Exception:
                continue
    except OSError:
        return None
    return None


def _last_audit_mtime(
    slug: str, run_id: str, project_root: Path, sprint_log_dir: Path | None
) -> float | None:
    """Most recent mtime of `<sprint_log_dir>/<slug>/audit.yaml` for the active run.

    Restricted to the resolved sprint log directory so a story that ran in a
    prior sprint with the same slug cannot surface its old timestamp here.
    """
    if not slug or sprint_log_dir is None:
        return None
    audit = sprint_log_dir / slug / "audit.yaml"
    try:
        return audit.stat().st_mtime
    except OSError:
        return None


def render_frame(
    run_id: str,
    project_root: Path,
    state: dict,
    frame_idx: int,
    *,
    color: bool,
    now_fn: Any = None,
) -> tuple[str, bool, str]:
    """Return ``(text, ok, captured_stderr)`` for one watch-mode frame.

    ``ok`` is False when the underlying snapshot reported it could not read
    sprint state (non-zero return) — the caller propagates that as a
    non-zero exit on the first frame and forwards ``captured_stderr`` to
    the real ``sys.stderr`` so the operator sees the diagnostic. Mid-loop
    callers must NOT print the stderr buffer or it will scroll past the
    in-place CURSOR_UP redraw region. ``state`` is mutated to record
    per-slug cost so the next frame can compute deltas.
    """
    from theforge.cli.sprint_status import display_sprint_status
    from theforge.sprint.status_reader import read_live_status

    buf = io.StringIO()
    err_buf = io.StringIO()
    # Capture stderr too: a mid-loop failure (e.g. state file just removed)
    # would otherwise scroll the terminal and break the in-place redraw math.
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err_buf):
        rc = display_sprint_status(run_id, project_root)
    base = buf.getvalue()
    snapshot_ok = rc == 0

    sprint_log_dir = _resolve_sprint_log_dir(run_id, project_root)

    entries = read_live_status(run_id, project_root) or []
    now = (now_fn or time.time)()
    spinner = SPINNER_FRAMES[frame_idx % len(SPINNER_FRAMES)]
    interval = float(state.get("interval", 2.0))
    stall_threshold = _stall_threshold_seconds(
        interval,
        state.get("stale_after_seconds"),
    )

    prev_costs = dict(state.get("costs", {}))
    new_costs: dict[str, float] = {}
    stalled_paths: list[str] = []

    overlay_lines: list[str] = []
    overlay_lines.append("")
    title = f"── Live  run={run_id}  refresh={interval:g}s  Ctrl-C to exit ──"
    overlay_lines.append(_c(title, DIM, color))
    _ACT_COL = 10
    hdr = f"{'STORY':<30}  {'ACTIVITY':<{_ACT_COL}}  {'ΔCOST':>8}  {'EVENT AGE':>9}"
    overlay_lines.append(hdr)

    for e in entries:
        slug = getattr(e, "slug", "") or ""
        path = getattr(e, "path", slug) or slug
        cost = float(getattr(e, "cost_usd", 0.0) or 0.0)
        prev = prev_costs.get(slug, cost)
        delta = cost - prev
        new_costs[slug] = cost

        last_evt = _last_audit_mtime(slug, run_id, project_root, sprint_log_dir)
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
                label = f"{STALL_CHAR} stalled"
                act = _c(label, DIM, color)
                stalled_paths.append(path)
            else:
                label = f"{spinner} live"
                act = _c(label, GREEN, color)
        elif status == "done":
            label = f"{DONE_CHAR} done"
            act = _c(label, GREEN, color)
        elif status == "failed":
            label = f"{FAIL_CHAR} failed"
            act = _c(label, RED, color)
        elif status == "skipped":
            label = f"{STALL_CHAR} skipped"
            act = _c(label, DIM, color)
        elif status == "blocked":
            label = f"{STALL_CHAR} blocked"
            act = _c(label, DIM, color)
        else:
            label = WAIT_CHAR
            act = label

        delta_str = _format_delta(delta)
        if color and delta != 0 and abs(delta) >= 0.005:
            delta_str = _c(delta_str, DIM, True)

        path_disp = path if len(path) <= 30 else path[:29] + "…"
        act_padded = act + " " * max(0, _ACT_COL - len(label))
        overlay_lines.append(f"{path_disp:<30}  {act_padded}  {delta_str:>8}  {age_str:>9}")

    if stalled_paths:
        stalled_preview = ", ".join(stalled_paths[:2])
        if len(stalled_paths) > 2:
            stalled_preview += f" +{len(stalled_paths) - 2} more"
        overlay_lines.append("")
        overlay_lines.append(
            _c(
                f"Hint: {stalled_preview} stalled; see `forge logs {run_id}`",
                DIM,
                color,
            )
        )

    state["costs"] = new_costs
    return base + "\n".join(overlay_lines) + "\n", snapshot_ok, err_buf.getvalue()


def _normalize_run_ids(run_ids: str | Sequence[str]) -> list[str]:
    if isinstance(run_ids, str):
        return [run_ids]
    return [run_id for run_id in run_ids if run_id]


def _render_multi_run_frame(
    run_ids: list[str],
    project_root: Path,
    states: dict[str, dict[str, Any]],
    frame: int,
    *,
    color: bool,
    interval: float,
    stale_after_seconds: float | None,
) -> tuple[str | None, bool, str]:
    blocks: list[str] = []
    diagnostics: list[str] = []
    any_ok = False
    separator = _c("─" * 80, DIM, color)

    for run_id in run_ids:
        state = states.setdefault(
            run_id,
            {
                "costs": {},
                "interval": float(interval),
                "stale_after_seconds": stale_after_seconds,
            },
        )
        try:
            body, snapshot_ok, captured_err = render_frame(
                run_id,
                project_root,
                state,
                frame,
                color=color,
            )
        except Exception as exc:  # noqa: BLE001
            snapshot_ok = False
            body = None
            captured_err = (
                f"forge status --watch: failed to read sprint state for {run_id}: {exc}\n"
            )

        if snapshot_ok:
            any_ok = True
        if captured_err:
            diagnostics.append(captured_err)
        if body is not None:
            blocks.append(body)

    if blocks:
        return f"\n{separator}\n".join(blocks), any_ok, "".join(diagnostics)
    if not run_ids:
        body = _c("No live sprint runs.\n", DIM, color)
        return body, True, ""
    return None, any_ok, "".join(diagnostics)


def run_watch_loop(
    run_id: str | Sequence[str],
    project_root: Path,
    interval: float,
    *,
    color: bool,
    follow_active_runs: bool = False,
    stale_after_seconds: float | None = None,
    sleep_fn: Any = None,
    max_frames: int | None = None,
) -> int:
    """Drive the watch redraw loop.

    Returns 0 on clean exit (Ctrl-C or max_frames reached) and non-zero when
    the underlying sprint snapshot cannot be read on the first frame.

    Redraws are in-place: after the first frame we record the line count and
    on each subsequent frame issue ``CURSOR_UP(n) + CARRIAGE_RETURN +
    CLEAR_BELOW`` to overwrite only the previously-rendered region. The
    operator's terminal scrollback is preserved and the visible screen is
    never cleared.
    """
    sleep = sleep_fn or time.sleep

    initial_run_ids = _normalize_run_ids(run_id)
    states: dict[str, dict[str, Any]] = {}
    frame = 0
    prev_lines = 0
    cursor_hidden = False
    try:
        if color or is_tty():
            sys.stdout.write(HIDE_CURSOR)
            sys.stdout.flush()
            cursor_hidden = True
        while True:
            candidate_run_ids = (
                _live_sprint_run_ids(project_root) if follow_active_runs else initial_run_ids
            )
            followed_run_ids: list[str] = []
            seen_run_ids: set[str] = set()
            for candidate_run_id in candidate_run_ids:
                followed_run_id = _resolve_followed_run_id(candidate_run_id, project_root)
                if followed_run_id in seen_run_ids:
                    continue
                seen_run_ids.add(followed_run_id)
                followed_run_ids.append(followed_run_id)

            active_state_ids = set(followed_run_ids)
            for state_run_id in list(states):
                if state_run_id not in active_state_ids:
                    states.pop(state_run_id, None)

            body, snapshot_ok, captured_err = _render_multi_run_frame(
                followed_run_ids,
                project_root,
                states,
                frame,
                color=color,
                interval=interval,
                stale_after_seconds=stale_after_seconds,
            )

            if frame == 0 and not snapshot_ok:
                # First frame failed: forward the snapshot's diagnostic that
                # render_frame captured. There is no in-place redraw region
                # yet, so printing to stderr is safe. (Mid-loop captures stay
                # discarded so they can't scroll past the CURSOR_UP region.)
                if captured_err:
                    sys.stderr.write(captured_err)
                    if not captured_err.endswith("\n"):
                        sys.stderr.write("\n")
                    sys.stderr.flush()
                else:
                    print(
                        "forge status --watch: failed to read sprint state.",
                        file=sys.stderr,
                    )
                return 1

            if body is not None:
                if frame == 0:
                    sys.stdout.write(body)
                else:
                    sys.stdout.write(CURSOR_UP(prev_lines) + CARRIAGE_RETURN + CLEAR_BELOW + body)
                sys.stdout.flush()
                prev_lines = body.count("\n")

            frame += 1
            if max_frames is not None and frame >= max_frames:
                return 0
            try:
                # Defense-in-depth: argparse already rejects non-positive
                # intervals, but clamp here so a programmatic caller can't
                # crash the loop with time.sleep(-1).
                sleep(max(0.0, float(interval)))
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
