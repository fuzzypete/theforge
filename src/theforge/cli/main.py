"""forge CLI — top-level parser construction and command dispatch."""

from __future__ import annotations

import argparse
import sys

from theforge.cli import (
    audit,
    check_config,
    daemon,
    diagnose,
    eval_cmd,
    forge_yaml_guard,
    groom,
    hooks,
    ideate,
    index,
    migrate_profiles,
    profiles,
    providers,
    rca,
    review,
    run,
    shape,
    sprint,
    status,
    telemetry,
    todo,
)
from theforge.cli import (
    audits as audits_cmd,
)
from theforge.cli.init_commands import cmd_init, cmd_secrets_init, cmd_version


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="forge",
        description="TheForge — Deterministic multi-LLM development orchestrator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Simple subcommands with no arguments
    init_parser = subparsers.add_parser("init", help="Generate a starter forge.yaml")
    memory_group = init_parser.add_mutually_exclusive_group()
    memory_group.add_argument(
        "--shared-memory",
        dest="shared_memory",
        action="store_true",
        default=True,
        help="Track project memory (audit runs + knowledge summaries) in git (default)",
    )
    memory_group.add_argument(
        "--local-memory",
        dest="shared_memory",
        action="store_false",
        help="Keep project memory local — omit the project-memory .gitignore re-includes",
    )
    subparsers.add_parser(
        "secrets-init",
        help="Create .forge/.env skeleton and update .gitignore",
    )
    subparsers.add_parser("version", help="Print the installed version")
    subparsers.add_parser(
        "init-hooks",
        help="Scaffold .forge/hooks/post_run.sh reference script and README",
    )

    # Subcommands with arguments
    run.register_parser(subparsers)
    review.register_parser(subparsers)
    sprint.register_parser(subparsers)
    ideate.register_parser(subparsers)
    index.register_parser(subparsers)
    providers.register_parser(subparsers)
    check_config.register_parser(subparsers)
    forge_yaml_guard.register_parser(subparsers)
    audit.register_parser(subparsers)
    audits_cmd.register_parser(subparsers)
    telemetry.register_parser(subparsers)
    daemon.register_parser(subparsers)
    eval_cmd.register_parser(subparsers)
    todo.register_parser(subparsers)
    diagnose.register_parser(subparsers)
    rca.register_parser(subparsers)
    shape.register_parser(subparsers)
    groom.register_parser(subparsers)
    migrate_profiles.register_parser(subparsers)
    profiles.register_parser(subparsers)
    status.register_parsers(subparsers)

    return parser


def main() -> None:
    """Entry point for the forge CLI."""
    # Global socket timeout safety net — prevents any HTTP call (ntfy, GitHub,
    # etc.) from blocking the process indefinitely if the OS-level connect or
    # read hangs despite per-call timeout arguments.
    import socket

    socket.setdefaulttimeout(60)

    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "init": cmd_init,
        "secrets-init": cmd_secrets_init,
        "init-hooks": hooks.cmd_init_hooks,
        "run": run.cmd_run,
        "review": review.cmd_review,
        "sprint": sprint.cmd_sprint,
        "ideate": ideate.cmd_ideate,
        "index": index.cmd_index,
        "check-providers": providers.cmd_check_providers,
        "check-config": check_config.cmd_check_config,
        "check-story-config": forge_yaml_guard.cmd_check_story_config,
        "audit": audit.cmd_audit,
        "audits": audits_cmd.cmd_audits,
        "telemetry": telemetry.cmd_telemetry,
        "daemon": daemon.cmd_daemon,
        "eval-preflight": eval_cmd.cmd_eval_preflight,
        "todo": todo.cmd_todo,
        "diagnose": diagnose.cmd_diagnose,
        "rca": rca.cmd_rca,
        "shape": shape.cmd_shape,
        "groom": groom.cmd_groom,
        "migrate-profiles": migrate_profiles.cmd_migrate_profiles,
        "profiles": profiles.cmd_profiles,
        "status": status.cmd_status,
        "logs": status.cmd_logs,
        "stop": status.cmd_stop,
        "decide": status.cmd_decide,
        "runs-clean": status.cmd_runs_clean,
        "version": cmd_version,
    }

    try:
        sys.exit(commands[args.command](args))
    except KeyboardInterrupt:
        print("\n[forge] Interrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception:
        import traceback

        print(
            f"\n[forge] FATAL — unhandled exception in '{args.command}':\n"
            f"{traceback.format_exc()}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
