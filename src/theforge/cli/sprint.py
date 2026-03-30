"""forge sprint subcommand — run multiple stories from a sprint manifest."""

from __future__ import annotations

import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.config import load_config
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.sprint import run_sprint
from theforge.sprint.lock import acquire_story_locks, release_story_locks
from theforge.sprint.runner import parse_manifest_slugs


def cmd_sprint(args: object) -> int:
    """Run multiple stories sequentially via a sprint manifest."""
    from theforge import daemon as _daemon
    from theforge import detach as _detach
    from theforge.coordinator.util import _generate_run_id

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"Sprint manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    # Find config (search from manifest's directory)
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).resolve()
    else:
        config_path = _find_config(manifest_path.parent)

    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    config = load_config(config_path)

    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    auto_merge = getattr(args, "auto_merge", False)
    interactive = getattr(args, "interactive", False)
    resume = getattr(args, "resume", False)
    no_pull = getattr(args, "no_pull", False)

    # ── Concurrency guard: refuse if any story is already running ───────
    slugs = parse_manifest_slugs(config, manifest_path)
    locked_fds, conflicted = acquire_story_locks(slugs, config.project_root)
    if conflicted:
        print(
            f"[forge] Stories already running: {', '.join(conflicted)}. Aborting.",
            file=sys.stderr,
        )
        return 1

    # ── Daemonization (default, before daemon-queue check) ─────────────
    if not getattr(args, "fg", False) and not getattr(args, "detach", False):
        run_id = _generate_run_id()
        slug = manifest_path.stem
        _detach.daemonize_run(run_id, slug, config.project_root)
        # Grandchild continues here; parent has already exited above.
        # locked_fds are inherited through the double-fork and remain held.
        # suppress_app_nap uses PyObjC which can SIGABRT in forked processes
        # due to ObjC runtime state. Skip it — the process is already detached
        # and setsid'd, which is sufficient protection.
        _detach.install_cleanup_handler(run_id, config.project_root)
        print("[forge] Detached sprint starting", file=sys.stderr, flush=True)
    else:
        run_id = _generate_run_id()
        slug = manifest_path.stem

    # If daemon is running (and --detach was given), submit to it instead.
    # Release our pre-check locks immediately — the daemon acquires its own.
    if getattr(args, "detach", False) and _daemon.is_daemon_running(config.project_root):
        release_story_locks(locked_fds)
        sprint_args: dict = {
            "auto_merge": auto_merge,
            "notify": not args.no_notify,
            "resume": resume,
            "config": str(config_path),
            "no_pull": no_pull,
        }
        response = _daemon.submit_sprint(config.project_root, str(manifest_path), sprint_args)
        if response.get("ok"):
            slug = response.get("queued", manifest_path.stem)
            pos = response.get("position", 1)
            print(f"[daemon] Queued '{slug}' (position {pos})")
            if not getattr(args, "detach", False):
                print("[daemon] Use 'forge status' to monitor progress.")
            return 0
        else:
            err = response.get("error", "unknown error")
            print(f"[daemon] Submit failed: {err}", file=sys.stderr)
            return 1

    try:
        result = run_sprint(
            config,
            manifest_path,
            auto_merge=auto_merge,
            interactive=interactive,
            notify=not args.no_notify,
            resume=resume,
            no_pull=no_pull,
        )
    except Exception as exc:
        import traceback

        print(f"Sprint error: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return 1
    finally:
        release_story_locks(locked_fds)

    # Remove PID file on completion
    _detach.remove_pid(run_id, config.project_root)

    return 0 if result.specs_failed == 0 else 1


def register_parser(subparsers: object) -> None:
    """Register the 'sprint' subcommand parser."""
    sprint_parser = subparsers.add_parser(
        "sprint", help="Run multiple stories sequentially from a sprint manifest"
    )
    sprint_parser.add_argument("manifest", help="Path to sprint.yaml manifest")
    sprint_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    sprint_parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="Merge each story's branch after APPROVE",
    )
    sprint_parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Pause for human review at each story",
    )
    sprint_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )
    sprint_parser.add_argument(
        "--no-notify",
        action="store_true",
        default=False,
        help="Suppress OS notifications",
    )
    sprint_parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Auto-triage failed stories and pick optimal re-entry point",
    )
    sprint_parser.add_argument(
        "--detach",
        action="store_true",
        default=False,
        help="When daemon is running, submit and return immediately without tailing logs",
    )
    sprint_parser.add_argument(
        "--fg",
        action="store_true",
        default=False,
        help="Run in foreground (skip daemonization)",
    )
    sprint_parser.add_argument(
        "--no-pull",
        action="store_true",
        default=False,
        help="Skip git pull --ff-only before creating fresh worktrees (offline/CI use)",
    )
