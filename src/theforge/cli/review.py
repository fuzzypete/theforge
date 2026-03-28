"""forge review subcommand — run only the review pool on an existing worktree."""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

from theforge.cli.shared import _build_task, _find_config, _write_audit
from theforge.config import load_config
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level


def cmd_review(args: object) -> int:
    """Run only the review pool on an existing worktree."""
    story_path = Path(args.story).resolve()
    if not story_path.exists():
        print(f"Story file not found: {story_path}", file=sys.stderr)
        return 1

    # Find config
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).resolve()
    else:
        config_path = _find_config(story_path.parent)

    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    config = load_config(config_path)
    task = _build_task(story_path, slug=args.slug)

    # Resolve workspace path
    if args.worktree:
        workspace_path = Path(args.worktree).resolve()
    else:
        workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)

    print(
        f"TheForge v{importlib.metadata.version('theforge')} — review-only mode", file=sys.stderr
    )
    print(f"  Project:    {config.project}", file=sys.stderr)
    print(f"  Task:       {task.name}", file=sys.stderr)
    print(f"  Slug:       {task.slug}", file=sys.stderr)
    print(f"  Workspace:  {workspace_path}", file=sys.stderr)
    if len(config.review_pool) == 1:
        print(f"  Rev model:  {config.review_pool[0].model}", file=sys.stderr)
    else:
        pool_info = ", ".join(f"{p.name}({p.model})" for p in config.review_pool)
        print(f"  Rev pool:   {pool_info}", file=sys.stderr)
    print(file=sys.stderr)

    auto_merge = getattr(args, "auto_merge", False)
    result = run_from_review(
        config,
        task,
        workspace_path,
        auto_merge=auto_merge,
        notify=not args.no_notify,
    )

    # Write audit log
    audit_path = _write_audit(result, config, task)

    # Summary
    print(file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    icon = "✓" if result.success else "✗"
    print(f"  {icon} {result.message}", file=sys.stderr)
    print(f"  Audit log: {audit_path}", file=sys.stderr)
    print(f"  Total cost: ${result.state.total_cost:.3f}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)

    return 0 if result.success else 1


def register_parser(subparsers: object) -> None:
    """Register the 'review' subcommand parser."""
    review_parser = subparsers.add_parser(
        "review", help="Run only the review pool on an existing worktree"
    )
    review_parser.add_argument("story", help="Path to the story file")
    review_parser.add_argument("--slug", help="Workspace slug (default: story filename stem)")
    review_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    review_parser.add_argument(
        "--worktree",
        help="Explicit worktree path (default: derived from slug)",
    )
    review_parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="Merge feature branch into base branch after review APPROVE",
    )
    review_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )
    review_parser.add_argument(
        "--no-notify",
        action="store_true",
        default=False,
        help="Suppress OS notifications",
    )
