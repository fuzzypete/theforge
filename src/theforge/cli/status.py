"""forge status/logs/stop/decide subcommands — runtime monitoring."""

from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.config import load_config

# Sentinel written to test log files to signal clean EOF (no redirect).
_SENTINEL_EOF = "[forge test sentinel EOF]"


def _follow_log_with_redirect(log_path: Path, current_run_id: str) -> tuple[str, Path] | None:
    """Stream log_path line-by-line, printing each line to stdout.

    Returns (new_run_id, new_log_path) when a re-exec redirect block is
    detected in the log (Run ID trailer differs from current_run_id).
    Returns None when the sentinel EOF line is encountered (test use).
    Runs indefinitely on real EOF (tail-f style); caller catches KeyboardInterrupt.
    """
    _seen_run_id: str | None = None
    _seen_log_path: str | None = None

    with open(log_path) as fh:
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.1)
                continue

            text = line.rstrip("\n")

            if text == _SENTINEL_EOF:
                return None

            print(text)

            if text.startswith("[forge] Run ID:  "):
                candidate_id = text[len("[forge] Run ID:  ") :].strip()
                if candidate_id != current_run_id:
                    _seen_run_id = candidate_id
            elif text.startswith("[forge] Log:     "):
                _seen_log_path = text[len("[forge] Log:     ") :].strip()

            if _seen_run_id and _seen_log_path:
                return (_seen_run_id, Path(_seen_log_path))


def cmd_status(args: object) -> int:
    """Show active forge runs and pending decisions."""
    from theforge import detach as _detach
    from theforge import pending as _pending

    config_path = _find_config()
    if config_path is None or not config_path.exists():
        print("forge.yaml not found.", file=sys.stderr)
        return 1
    config = load_config(config_path)
    project_root = config.project_root

    # Active runs via PID scan
    active_runs = _detach.list_active_runs(project_root)
    if active_runs:
        print(f"{'RUN ID':<12}  {'STORY':<30}  {'PHASE':<12}  {'COST':>7}  {'ELAPSED':>8}")
        print("-" * 78)
        for run in active_runs:
            run_id = run["run_id"]
            slug = run["slug"]
            status = _detach.read_run_status(run_id, slug, project_root)
            phase = status.get("phase") or "RUNNING"
            cost_usd = status.get("cost_usd")
            elapsed_s = status.get("elapsed_seconds")
            cost_str = f"${cost_usd:.2f}" if cost_usd is not None else "  —"
            if elapsed_s is not None:
                elapsed_m = int(elapsed_s // 60)
                elapsed_str = f"{elapsed_m}m"
            else:
                elapsed_str = "—"
            print(f"{run_id:<12}  {slug:<30}  {phase:<12}  {cost_str:>7}  {elapsed_str:>8}")
        print()
        n = len(active_runs)
        print(f"{n} active run{'s' if n != 1 else ''}. Use 'forge logs <run-id>' for live output.")
    else:
        print("No active runs.")

    # Show pending decisions
    _pending.cleanup_stale(project_root)
    pending_entries = _pending.list_pending(project_root)
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

    return 0


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
    try:
        while True:
            print(f"[forge] Tailing {current_log} — Ctrl+C to stop", file=sys.stderr)
            result = _follow_log_with_redirect(current_log, current_run_id)
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
            current_run_id = new_run_id
            current_log = new_log
    except KeyboardInterrupt:
        pass
    return 0


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
        _detach.remove_pid(run_id, project_root)
        return 1
    except OSError as exc:
        print(f"Could not signal process {pid}: {exc}", file=sys.stderr)
        return 1

    if args.no_wait:
        return 0

    start = time.monotonic()
    while time.monotonic() - start < args.timeout:
        if not _detach._is_pid_alive(pid):
            print(f"[forge] Run {run_id} has stopped.")
            return 0
        time.sleep(0.1)

    if not _detach._is_pid_alive(pid):
        print(f"[forge] Run {run_id} has stopped.")
        return 0

    print(
        f"Timed out waiting for run {run_id} to stop (PID {pid} still alive)",
        file=sys.stderr,
    )
    return 1


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


def register_parsers(subparsers: object) -> None:
    """Register status/logs/stop/decide subcommand parsers."""
    # forge status
    subparsers.add_parser(
        "status",
        help="Show active runs and pending decisions",
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
