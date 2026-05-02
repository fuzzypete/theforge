"""forge status/logs/stop/decide subcommands — runtime monitoring."""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.cli.status_run_helpers import is_sprint_run as _is_sprint_run
from theforge.config import load_config

_SENTINEL_EOF = "[forge test sentinel EOF]"
_WATCH_SPRINT_STARTUP_GRACE_SECONDS = 5.0
_WATCH_SPRINT_STARTUP_POLL_SECONDS = 0.1


def _follow_log_with_redirect(
    log_path: Path, current_run_id: str, *, runs_dir: Path | None = None
) -> tuple[str, Path] | None:
    """Stream log_path to stdout; poll ``runs_dir/<current_run_id>.redirect`` on EOF.

    Returns (new_run_id, new_log_path) on redirect, None on sentinel EOF.
    Runs indefinitely on real EOF (tail-f style); caller catches KeyboardInterrupt.
    """
    redirect_file = (runs_dir / f"{current_run_id}.redirect") if runs_dir else None

    with open(log_path) as fh:
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                if redirect_file is not None and redirect_file.exists():
                    try:
                        d = json.loads(redirect_file.read_text(encoding="utf-8"))
                        return d["new_run_id"], Path(d["new_log"])
                    except (OSError, KeyError, ValueError, json.JSONDecodeError):
                        pass
                time.sleep(0.1)
                continue

            if not line.endswith("\n"):  # partial write — rewind and wait
                fh.seek(pos)
                time.sleep(0.1)
                continue

            text = line.rstrip("\n")

            if text == _SENTINEL_EOF:
                return None

            print(text)


def _list_active_run_ids(project_root: Path) -> list[str]:
    """Return the run_ids of all alive runs."""
    from theforge import detach as _detach

    active = _detach.list_active_runs(project_root)
    return [str(run["run_id"]) for run in active]


def _await_watchable_sprint_run(
    run_id: str,
    project_root: Path,
    *,
    grace_seconds: float = _WATCH_SPRINT_STARTUP_GRACE_SECONDS,
    poll_seconds: float = _WATCH_SPRINT_STARTUP_POLL_SECONDS,
) -> bool:
    """Wait briefly for a live run to become recognizable as a sprint.

    Kept as a local wrapper so existing tests can patch ``status._is_sprint_run``
    and ``status.time`` directly while the shared watch helpers live elsewhere.
    """
    if _is_sprint_run(run_id, project_root):
        return True

    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    if not pid_file.exists():
        return False

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        time.sleep(max(0.0, poll_seconds))
        if _is_sprint_run(run_id, project_root):
            return True
        if not pid_file.exists():
            return False

    return _is_sprint_run(run_id, project_root)


def _find_most_recent_run(project_root: Path) -> tuple[str, bool] | None:
    """Scan for the most recent completed run (sprint or single).

    Returns (run_id, is_sprint) for the newest completed/historical run,
    or None if no runs are found.  Active runs (PID files present and
    process alive) are excluded so callers see only finished work.
    """
    import yaml

    logs_dir = project_root / ".forge" / "logs"
    if not logs_dir.exists():
        return None

    # Collect active run_ids so we can skip live single runs
    active_run_ids: set[str] = set()
    runs_dir = project_root / ".forge" / "runs"
    if runs_dir.exists():
        for pid_file in runs_dir.glob("*.pid"):
            active_run_ids.add(pid_file.stem)

    best_mtime: float | None = None
    best: tuple[str, bool] | None = None

    # Sprint summaries are only written on completion — always historical.
    for summary in logs_dir.rglob("sprint-summary.yaml"):
        try:
            mtime = summary.stat().st_mtime
        except OSError:
            continue
        try:
            with open(summary, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            run_id = (data or {}).get("sprint", {}).get("run_id", "")
        except Exception:
            run_id = ""
        if run_id and (best_mtime is None or mtime > best_mtime):
            best_mtime = mtime
            best = (run_id, True)

    # Single-run log files: run-<run_id>.log in any logs subdirectory.
    for log_file in logs_dir.rglob("run-*.log"):
        run_id = log_file.stem[4:]  # strip leading "run-"
        if run_id in active_run_ids:
            continue  # still live — skip
        try:
            mtime = log_file.stat().st_mtime
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best_mtime = mtime
            best = (run_id, False)

    return best


def _resolve_run_id(run_id: str, project_root: Path) -> bool:
    """Return True if run_id can be located (active PID, sprint data, or log file)."""
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    if pid_file.exists():
        return True

    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    if state_path.exists():
        return True

    from theforge.sprint.status_reader import find_sprint_summary

    if find_sprint_summary(run_id, project_root) is not None:
        return True

    logs_dir = project_root / ".forge" / "logs"
    if logs_dir.exists():
        for _match in logs_dir.rglob(f"run-{run_id}.log"):
            return True

    return False


def _show_recent_runs(project_root: Path) -> int:
    """Print a compact table of recent runs sorted by time (newest first)."""
    import yaml

    from theforge import detach as _detach

    # Rows: (mtime_float, run_id, type, status, cost_str, elapsed_str)
    rows: list[tuple[float, str, str, str, str, str]] = []
    seen_ids: set[str] = set()
    now = time.time()

    # Active runs — treat as most recent by using current time as mtime.
    for run in _detach.list_active_runs(project_root):
        run_id = run["run_id"]
        slug = run["slug"]
        st = _detach.read_run_status(run_id, slug, project_root)
        run_type = "sprint" if _is_sprint_run(run_id, project_root) else "single"
        cost_usd = st.get("cost_usd")
        elapsed_s = st.get("elapsed_seconds")
        cost_str = f"${cost_usd:.2f}" if cost_usd is not None else "—"
        elapsed_str = f"{int(elapsed_s // 60)}m" if elapsed_s is not None else "—"
        rows.append((now, run_id, run_type, "active", cost_str, elapsed_str))
        seen_ids.add(run_id)

    logs_dir = project_root / ".forge" / "logs"

    if logs_dir.exists():
        # Completed sprints — collect with their summary file mtime.
        for summary in logs_dir.rglob("sprint-summary.yaml"):
            try:
                mtime = summary.stat().st_mtime
            except OSError:
                continue
            try:
                with open(summary, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except Exception:
                continue
            sp = data.get("sprint", {})
            run_id = sp.get("run_id", "")
            if not run_id or run_id in seen_ids:
                continue
            cost_usd = sp.get("total_cost_usd")
            dur_s = sp.get("duration_seconds")
            cost_str = f"${cost_usd:.2f}" if cost_usd is not None else "—"
            dur_str = f"{int(dur_s // 60)}m" if dur_s is not None else "—"
            stopped = sp.get("stopped_reason")
            status_str = "stopped" if stopped else "completed"
            rows.append((mtime, run_id, "sprint", status_str, cost_str, dur_str))
            seen_ids.add(run_id)

        # Historical single runs — collect with log file mtime.
        for log_file in logs_dir.rglob("run-*.log"):
            run_id = log_file.stem[4:]
            if run_id in seen_ids:
                continue
            try:
                mtime = log_file.stat().st_mtime
            except OSError:
                continue
            outcome = _detach.read_run_ended(run_id, project_root)
            if outcome is not None:
                status_str = outcome
            else:
                # Cross-check: a PID file means a run started after the
                # initial list_active_runs scan — label it active, not orphaned.
                pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
                status_str = "active" if pid_file.exists() else "orphaned"
            rows.append((mtime, run_id, "single", status_str, "—", "—"))
            seen_ids.add(run_id)

    if not rows:
        print("No recent runs found.")
        return 0

    # Sort all runs newest-first, then truncate.
    rows.sort(key=lambda r: r[0], reverse=True)

    print(f"{'RUN ID':<14}  {'TYPE':<8}  {'STATUS':<12}  {'COST':>7}  {'ELAPSED':>8}")
    print("-" * 60)
    for _, run_id, rtype, status, cost, elapsed in rows[:20]:
        print(f"{run_id:<14}  {rtype:<8}  {status:<12}  {cost:>7}  {elapsed:>8}")
    return 0


def _show_single_run_status(run_id: str, project_root: Path) -> None:
    """Print a single-row status line for a non-sprint run."""
    from theforge import detach as _detach

    # Try to find slug from PID file
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    slug: str | None = None
    if pid_file.exists():
        parsed = _detach._read_pid_file(pid_file)
        if parsed:
            _, slug = parsed

    if slug is None:
        # Infer slug from log directory
        logs_dir = project_root / ".forge" / "logs"
        if logs_dir.exists():
            for match in logs_dir.rglob(f"run-{run_id}.log"):
                slug = match.parent.name
                break

    st = _detach.read_run_status(run_id, slug or run_id, project_root)
    phase = st.get("phase") or "DONE"
    cost_usd = st.get("cost_usd")
    elapsed_s = st.get("elapsed_seconds")
    cost_str = f"${cost_usd:.2f}" if cost_usd is not None else "  —"
    elapsed_str = f"{int(elapsed_s // 60)}m" if elapsed_s is not None else "—"

    story_label = slug or run_id
    print(f"{'RUN ID':<12}  {'STORY':<30}  {'PHASE':<12}  {'COST':>7}  {'ELAPSED':>8}")
    print("-" * 78)
    print(f"{run_id:<12}  {story_label:<30}  {phase:<12}  {cost_str:>7}  {elapsed_str:>8}")


def _render_status_blocks(run_ids: list[str], project_root: Path) -> int:
    """Render one status block per run_id and aggregate return codes."""
    from theforge.cli.sprint_status import display_sprint_status

    rcs: list[int] = []
    for index, run_id in enumerate(run_ids):
        if index:
            print()
        if _is_sprint_run(run_id, project_root):
            rc = display_sprint_status(run_id, project_root)
        else:
            _show_single_run_status(run_id, project_root)
            rc = 0
        rcs.append(rc)
    if not rcs:
        return 0
    return 0 if any(rc == 0 for rc in rcs) else rcs[0]


def _resolve_watch_run_ids(active_run_ids: list[str], project_root: Path) -> list[str]:
    """Return live sprint run_ids suitable for watch mode startup."""
    watch_ids: list[str] = []
    for run_id in active_run_ids:
        if _is_sprint_run(run_id, project_root):
            watch_ids.append(run_id)
        elif _await_watchable_sprint_run(run_id, project_root):
            watch_ids.append(run_id)
    return watch_ids


def cmd_status(args: object) -> int:
    """Show active forge runs and pending decisions."""
    from theforge import pending as _pending

    config_path = _find_config()
    if config_path is None or not config_path.exists():
        print("forge.yaml not found.", file=sys.stderr)
        return 1
    config = load_config(config_path)
    project_root = config.project_root

    recent = getattr(args, "recent", False)
    last = getattr(args, "last", False)
    explicit_run_id: str | None = getattr(args, "run_id", None)
    watch_interval: int | None = getattr(args, "watch", None)
    no_color: bool = getattr(args, "no_color", False)

    if watch_interval is not None and watch_interval <= 0:
        print(
            f"--watch interval must be a positive integer (got {watch_interval}).",
            file=sys.stderr,
        )
        return 2

    # ── --recent: compact run list ────────────────────────────────────────
    if recent:
        return _show_recent_runs(project_root)

    # ── Resolve target runs ───────────────────────────────────────────────
    target_run_ids: list[str] = []
    active_run_ids: list[str] = []

    if explicit_run_id:
        if not _resolve_run_id(explicit_run_id, project_root):
            print(f"No run found with ID '{explicit_run_id}'.", file=sys.stderr)
            return 1
        target_run_ids = [explicit_run_id]
    elif last:
        result = _find_most_recent_run(project_root)
        if result is None:
            print("No recent completed runs found.")
            return 0
        target_run_ids = [result[0]]
    else:
        active_run_ids = _list_active_run_ids(project_root)
        if active_run_ids:
            target_run_ids = active_run_ids
        else:
            result = _find_most_recent_run(project_root)
            if result is not None:
                target_run_ids = [result[0]]

    if not target_run_ids:
        print("No active or recent runs found.")
        _pending.cleanup_stale(project_root)
        _show_pending_decisions(_pending, project_root)
        return 0

    # ── Watch mode (TTY-only; falls back to snapshot otherwise) ───────────
    if watch_interval is not None:
        from theforge.cli import status_watch

        watch_run_ids = (
            [explicit_run_id]
            if explicit_run_id is not None
            else _resolve_watch_run_ids(active_run_ids, project_root)
        )
        if status_watch.is_tty() and watch_run_ids:
            color = status_watch.color_enabled(no_color)
            return status_watch.run_watch_loop(
                watch_run_ids if explicit_run_id is None else explicit_run_id,
                project_root,
                float(watch_interval),
                color=color,
                follow_active_runs=explicit_run_id is None,
            )
        # Non-TTY or non-sprint run: silently fall back to single snapshot so
        # piped/redirected output and CI captures stay clean.

    # ── Display run status ────────────────────────────────────────────────
    rc = _render_status_blocks(target_run_ids, project_root)

    # ── Pending decisions (always shown) ──────────────────────────────────
    _pending.cleanup_stale(project_root)
    _show_pending_decisions(_pending, project_root)

    return rc


def _show_pending_decisions(pending_mod: object, project_root: Path) -> None:
    """Print the pending-decisions section."""
    pending_entries = pending_mod.list_pending(project_root)
    if pending_entries:
        print("\nPending decisions:")
        now = datetime.datetime.now(datetime.timezone.utc)
        for entry in pending_entries:
            run_id = entry.get("run_id", "?")
            story = entry.get("story", "?")
            phase = entry.get("phase", "?")
            reason = (entry.get("reason") or "")[:80]
            created_at = entry.get("created_at", "")
            timeout_at_str = entry.get("timeout_at", "")
            options = entry.get("options", [])
            decision = entry.get("decision")

            time_remaining = ""
            if timeout_at_str and not decision:
                try:
                    timeout_at = datetime.datetime.fromisoformat(timeout_at_str)
                    remaining = (timeout_at - now).total_seconds()
                    if remaining > 0:
                        mins = int(remaining // 60)
                        secs = int(remaining % 60)
                        time_remaining = f" ({mins}m{secs}s remaining)"
                    else:
                        time_remaining = " (expired)"
                except Exception:
                    pass

            status_str = f"decided: {decision}" if decision else f"waiting{time_remaining}"
            opts_str = "/".join(options) if options else ""
            print(f"  {run_id}  [{phase}]  story={story}  {status_str}")
            if reason:
                print(f"    reason: {reason}")
            if opts_str:
                print(f"    options: {opts_str}")
            if created_at:
                print(f"    created: {created_at}")
    else:
        print("\nPending decisions: (none)")


def cmd_logs(args: object) -> int:
    """Tail the log file for a running forge process."""
    from theforge import detach as _detach

    config_path = _find_config()
    if config_path is None or not config_path.exists():
        print("forge.yaml not found.", file=sys.stderr)
        return 1
    config = load_config(config_path)
    project_root = config.project_root
    run_id = args.run_id

    # Find slug from PID file
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    slug: str | None = None
    if pid_file.exists():
        parsed = _detach._read_pid_file(pid_file)
        if parsed:
            _, slug = parsed

    if slug is None:
        # Try to find log by run_id in any log subdir
        logs_dir = project_root / ".forge" / "logs"
        if logs_dir.exists():
            for match in logs_dir.rglob(f"run-{run_id}.log"):
                log_path = match
                break
            else:
                print(f"No log found for run {run_id}", file=sys.stderr)
                return 1
        else:
            print(f"No log found for run {run_id}", file=sys.stderr)
            return 1
    else:
        log_path = _detach._find_log_path(slug, run_id, project_root)
        if log_path is None or not log_path.exists():
            print(f"Log file not found for run {run_id}", file=sys.stderr)
            return 1

    current_run_id = run_id
    current_log = log_path
    runs_dir = project_root / ".forge" / "runs"
    try:
        while True:
            print(f"[forge] Tailing {current_log} — Ctrl+C to stop", file=sys.stderr)
            result = _follow_log_with_redirect(current_log, current_run_id, runs_dir=runs_dir)
            if result is None:
                break
            new_run_id, new_log = result
            print(
                f"[forge] Run re-exec'd — following new run {new_run_id}",
                file=sys.stderr,
            )
            # Wait briefly for the new log to be created (up to ~2 s).
            for _ in range(20):
                if new_log.exists():
                    break
                time.sleep(0.1)
            if not new_log.exists():
                print(
                    f"[forge] Timed out waiting for new log {new_log} — run may have exited",
                    file=sys.stderr,
                )
                return 1
            current_run_id = new_run_id
            current_log = new_log
    except KeyboardInterrupt:
        pass
    return 0


def _stopped_run_lock_slugs(run_id: str, project_root: Path, slug: str) -> list[str]:
    from theforge.sprint.status_reader import read_live_status

    entries = read_live_status(run_id, project_root)
    if entries:
        story_slugs = [entry.slug for entry in entries if entry.slug]
        if story_slugs:
            return story_slugs
    return [slug]


def _cleanup_stopped_run(
    run_id: str,
    project_root: Path,
    slug: str,
    *,
    pid: int | None = None,
) -> None:
    from theforge import detach as _detach
    from theforge.sprint.lock import cleanup_story_locks

    lock_slugs = _stopped_run_lock_slugs(run_id, project_root, slug)
    _detach.remove_pid(run_id, project_root)
    _detach.write_run_ended(run_id, project_root, "stopped", force=True)
    cleanup_story_locks(lock_slugs, project_root, pid=pid)


def cmd_stop(args: object) -> int:
    """Send SIGTERM to a running forge process."""
    import signal as _signal

    from theforge import detach as _detach

    config_path = _find_config()
    if config_path is None or not config_path.exists():
        print("forge.yaml not found.", file=sys.stderr)
        return 1
    config = load_config(config_path)
    project_root = config.project_root
    run_id = args.run_id

    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    if not pid_file.exists():
        print(f"No PID file found for run {run_id} — is it still running?", file=sys.stderr)
        return 1

    parsed = _detach._read_pid_file(pid_file)
    if parsed is None:
        print(f"Could not read PID file for run {run_id}", file=sys.stderr)
        return 1

    pid, slug = parsed
    try:
        os.kill(pid, _signal.SIGTERM)
        print(f"[forge] Sent SIGTERM to run {run_id} (PID {pid})")
    except ProcessLookupError:
        print(f"Process {pid} not found — cleaning up stale PID file")
        _cleanup_stopped_run(run_id, project_root, slug, pid=pid)
        return 1
    except OSError as exc:
        print(f"Could not signal process {pid}: {exc}", file=sys.stderr)
        return 1

    if args.no_wait:
        return 0

    start = time.monotonic()
    while time.monotonic() - start < args.timeout:
        if not _detach._is_pid_alive(pid):
            _cleanup_stopped_run(run_id, project_root, slug, pid=pid)
            print(f"[forge] Run {run_id} has stopped.")
            return 0
        time.sleep(0.1)

    if not _detach._is_pid_alive(pid):
        _cleanup_stopped_run(run_id, project_root, slug, pid=pid)
        print(f"[forge] Run {run_id} has stopped.")
        return 0

    try:
        os.kill(pid, _signal.SIGKILL)
        print(f"[forge] SIGTERM timed out for run {run_id}; sent SIGKILL to PID {pid}")
    except ProcessLookupError:
        _cleanup_stopped_run(run_id, project_root, slug, pid=pid)
        print(f"[forge] Run {run_id} has stopped.")
        return 0
    except OSError as exc:
        print(f"Could not SIGKILL process {pid}: {exc}", file=sys.stderr)
        print(
            f"Timed out waiting for run {run_id} to stop (PID {pid} still alive). "
            f"Kill it manually and remove stale locks for {slug} if needed.",
            file=sys.stderr,
        )
        return 1

    kill_start = time.monotonic()
    while time.monotonic() - kill_start < 5.0:
        if not _detach._is_pid_alive(pid):
            _cleanup_stopped_run(run_id, project_root, slug, pid=pid)
            print(f"[forge] Run {run_id} has stopped.")
            return 0
        time.sleep(0.1)

    print(
        f"Timed out waiting for run {run_id} to stop after SIGKILL (PID {pid} still alive). "
        f"Kill it manually and remove stale locks for {slug} if needed.",
        file=sys.stderr,
    )
    return 1


def cmd_runs_clean(args: object) -> int:
    """Mark orphaned runs (no PID, no .ended) by writing a .ended sentinel."""
    from theforge import detach as _detach

    config_path = _find_config()
    if config_path is None or not config_path.exists():
        print("forge.yaml not found.", file=sys.stderr)
        return 1
    config = load_config(config_path)
    project_root = config.project_root

    logs_dir = project_root / ".forge" / "logs"
    runs_dir = project_root / ".forge" / "runs"
    if not logs_dir.exists():
        print("No runs found.")
        return 0

    # Collect run IDs that are currently alive (have a live PID).
    active_pids: set[str] = set()
    for run in _detach.list_active_runs(project_root):
        active_pids.add(run["run_id"])

    cleaned = 0
    for log_file in logs_dir.rglob("run-*.log"):
        run_id = log_file.stem[4:]
        if run_id in active_pids:
            continue
        ended_file = runs_dir / f"{run_id}.ended"
        if ended_file.exists():
            continue
        # Orphan: has a log, no live process, no terminal marker.
        _detach.write_run_ended(run_id, project_root, "orphaned")
        print(f"[forge] Marked {run_id} as orphaned")
        cleaned += 1

    if cleaned == 0:
        print("No orphaned runs found.")
    else:
        print(f"Cleaned {cleaned} orphaned run(s).")
    return 0


def cmd_decide(args: object) -> int:
    """Write a decision to a pending file."""
    from theforge import pending as _pending

    config_path = _find_config()
    project_root = None
    if config_path is not None and config_path.exists():
        try:
            config = load_config(config_path)
            project_root = config.project_root
        except Exception:
            pass

    run_id = args.run_id
    action = args.action

    entry = _pending.read_pending(run_id, project_root)
    if entry is None:
        print(f"No pending decision found for run_id={run_id!r}", file=sys.stderr)
        return 1

    options = entry.get("options", [])
    if options and action not in options:
        print(
            f"Invalid action {action!r}. Valid options: {', '.join(options)}",
            file=sys.stderr,
        )
        return 1

    if not _pending.resolve_pending(run_id, action, project_root):
        print(f"Failed to write decision for run_id={run_id!r}", file=sys.stderr)
        return 1

    print(f"Decision '{action}' recorded for {run_id}")
    return 0


def _positive_watch_interval(raw: str) -> int:
    """argparse type for ``--watch SECONDS``: require a positive integer.

    Rejects 0 and negatives so the watch loop never sleeps for a non-positive
    duration (which would crash on ``time.sleep(-1)``) and never busy-loops at
    interval 0.
    """
    import argparse  # noqa: PLC0415

    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"--watch requires an integer number of seconds, got {raw!r}"
        ) from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(
            f"--watch interval must be a positive integer (got {value})"
        )
    return value


def register_parsers(subparsers: object) -> None:
    """Register status/logs/stop/decide subcommand parsers."""
    # forge status
    status_parser = subparsers.add_parser(
        "status",
        help="Show active runs and pending decisions",
    )
    status_parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Run ID to show status for (default: active or most recent run)",
    )
    status_parser.add_argument(
        "--recent",
        action="store_true",
        default=False,
        help="Show recent runs in compact list form",
    )
    status_parser.add_argument(
        "--last",
        action="store_true",
        default=False,
        help="Show the most recent completed or failed run",
    )
    status_parser.add_argument(
        "--watch",
        nargs="?",
        const=2,
        type=_positive_watch_interval,
        default=None,
        metavar="SECONDS",
        help=(
            "Live-update mode: re-render every SECONDS (must be a positive "
            "integer; default 2). Falls back to single snapshot if stdout is "
            "not a TTY."
        ),
    )
    status_parser.add_argument(
        "--no-color",
        dest="no_color",
        action="store_true",
        default=False,
        help="Disable ANSI color in watch mode (NO_COLOR env var also honored).",
    )

    # forge logs
    logs_parser = subparsers.add_parser(
        "logs",
        help="Tail the log file for a running forge process",
    )
    logs_parser.add_argument("run_id", help="Run ID to follow logs for")

    # forge stop
    stop_parser = subparsers.add_parser(
        "stop",
        help="Send SIGTERM to a running forge process",
    )
    stop_parser.add_argument("run_id", help="Run ID to stop")
    stop_parser.add_argument(
        "--no-wait",
        action="store_true",
        default=False,
        help="Return immediately after sending SIGTERM",
    )
    stop_parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        metavar="N",
        help="Seconds to wait for process exit (default 60)",
    )

    # forge runs-clean
    subparsers.add_parser(
        "runs-clean",
        help="Mark orphaned runs (no terminal marker) so forge status shows correct state",
    )

    # forge decide
    decide_parser = subparsers.add_parser(
        "decide",
        help="Write a decision to a pending HITL file",
    )
    decide_parser.add_argument("run_id", help="Run ID of the pending decision")
    decide_parser.add_argument(
        "action",
        help="Decision to record (e.g. approve, reject, continue, retry, skip, abort)",
    )
