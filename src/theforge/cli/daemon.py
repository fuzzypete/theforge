"""forge daemon subcommand — manage the forge daemon (deprecated)."""

from __future__ import annotations

import sys

from theforge.cli.shared import _find_config
from theforge.config import load_config


def _print_daemon_status(state: dict) -> None:
    """Print daemon status in a human-readable format."""
    running = state.get("running", False)
    pid = state.get("pid")
    started_at = state.get("started_at", "")

    if running:
        print(f"Daemon: RUNNING  (PID {pid}, started {started_at})")
    else:
        print("Daemon: NOT RUNNING")

    current = state.get("current_sprint")
    if current:
        spec = current.get("spec", "?")
        phase = current.get("phase", "?")
        iteration = current.get("iteration", 0)
        cost = current.get("cost_usd", 0.0)
        print(f"  Running: {spec}  phase={phase}  iter={iteration}  cost=${cost:.2f}")
    else:
        print("  Running: (none)")

    queue = state.get("queue", [])
    if queue:
        print(f"  Queue: {', '.join(str(q) for q in queue)}")
    else:
        print("  Queue: (empty)")

    completed = state.get("completed", [])
    if completed:
        print(f"  Recent completions ({len(completed)}):")
        for entry in completed[-5:]:
            spec = entry.get("spec", "?")
            outcome = entry.get("outcome", "?")
            cost = entry.get("cost_usd", 0.0)
            print(f"    {spec}  {outcome}  ${cost:.2f}")


def cmd_daemon(args: object) -> int:
    """Manage the forge daemon (start/stop/status/install/uninstall)."""
    import warnings

    from theforge import daemon as _daemon

    warnings.warn(
        "forge daemon is deprecated; forge run/sprint now auto-detach. "
        "Use --fg for foreground mode.",
        DeprecationWarning,
        stacklevel=2,
    )

    subcommand = getattr(args, "daemon_subcommand", None)

    if subcommand == "start":
        config_path = _find_config()
        if config_path is None or not config_path.exists():
            print(
                "forge.yaml not found. Run 'forge init' to create one.",
                file=sys.stderr,
            )
            return 1
        config = load_config(config_path)
        if _daemon.is_daemon_running(config.project_root):
            state = _daemon.get_daemon_status(config.project_root)
            pid = state.get("pid", "?")
            print(f"Daemon already running (PID {pid})")
            return 0
        print(f"Starting daemon for project: {config.project_root}")
        print("  PID file: .forge/daemon.pid")
        print("  Socket:   .forge/daemon.sock")
        print("  Log:      .forge/logs/daemon.log")
        print("  Tip: 'forge daemon install' for launchd auto-start (macOS)")
        try:
            _daemon.start_daemon(config, no_daemonize=getattr(args, "no_daemonize", False))
        except RuntimeError as exc:
            print(f"[daemon] Error: {exc}", file=sys.stderr)
            return 1
        # If we reach here, we're still the parent process (child daemonized)
        return 0

    elif subcommand == "stop":
        config_path = _find_config()
        if config_path is None or not config_path.exists():
            print("forge.yaml not found.", file=sys.stderr)
            return 1
        config = load_config(config_path)
        if not _daemon.is_daemon_running(config.project_root):
            print("No daemon running.")
            return 0
        try:
            _daemon.stop_daemon(config.project_root)
            print("Daemon stopped.")
        except RuntimeError as exc:
            print(f"[daemon] Error: {exc}", file=sys.stderr)
            return 1
        return 0

    elif subcommand == "status":
        config_path = _find_config()
        if config_path is None or not config_path.exists():
            print("forge.yaml not found.", file=sys.stderr)
            return 1
        config = load_config(config_path)
        state = _daemon.get_daemon_status(config.project_root)
        _print_daemon_status(state)
        return 0

    elif subcommand == "install":
        import shutil

        config_path = _find_config()
        if config_path is None or not config_path.exists():
            print("forge.yaml not found.", file=sys.stderr)
            return 1
        config = load_config(config_path)
        forge_bin = shutil.which("forge")
        if forge_bin is None:
            print("'forge' binary not found in PATH.", file=sys.stderr)
            return 1
        try:
            from pathlib import Path

            plist_path = _daemon.install_launchd(config.project_root, Path(forge_bin))
            print(f"Installed launchd plist: {plist_path}")
            print("Daemon will start automatically on login.")
        except RuntimeError as exc:
            print(f"[daemon] Error: {exc}", file=sys.stderr)
            return 1
        return 0

    elif subcommand == "uninstall":
        try:
            _daemon.uninstall_launchd()
            print("Launchd plist removed.")
        except Exception as exc:
            print(f"[daemon] Error: {exc}", file=sys.stderr)
            return 1
        return 0

    else:
        print(f"Unknown daemon subcommand: {subcommand}", file=sys.stderr)
        return 1


def register_parser(subparsers: object) -> None:
    """Register the 'daemon' subcommand parser."""
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Manage the forge daemon (persistent background sprint runner)",
    )
    daemon_parser.add_argument(
        "daemon_subcommand",
        choices=["start", "stop", "status", "install", "uninstall"],
        help="Daemon subcommand",
    )
    daemon_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
    daemon_parser.add_argument(
        "--no-daemonize",
        action="store_true",
        default=False,
        help="Run in foreground (skip double-fork); used by launchd",
    )
