"""forge review subcommand — run only the review pool on an existing worktree."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from theforge.cli.shared import _build_task, _find_config, _write_audit, load_config_checked
from theforge.config import load_config
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.run_setup import REENTRY_MODE_REVIEW
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.sprint.sources import GitHubIssueSource, IssueClosedError


def cmd_review(args: object) -> int:
    """Run only the review pool on an existing worktree."""
    issue = getattr(args, "issue", None)
    story_arg = getattr(args, "story", None)
    if (issue is None) == (story_arg is None):
        print(
            "forge review needs exactly one story source: a story file path, "
            "or --issue N for a GitHub-issue-backed story.",
            file=sys.stderr,
        )
        return 1

    story_path: Path | None = None
    if story_arg is not None:
        story_path = Path(story_arg).resolve()
        if not story_path.exists():
            print(f"Story file not found: {story_path}", file=sys.stderr)
            return 1
        # A worktree is the thing being reviewed, not the story describing it —
        # a directory here is almost always someone reaching for --worktree or
        # --issue, so name both rather than letting read_text() raise IsADirectoryError.
        if story_path.is_dir():
            print(
                f"{story_path} is a directory, not a story file.\n"
                "  To review a GitHub-issue-backed story:  forge review --issue N\n"
                "  To point at a worktree explicitly:      forge review <story> "
                "--worktree <path>",
                file=sys.stderr,
            )
            return 1

    # Find config
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).resolve()
    else:
        config_path = _find_config(story_path.parent if story_path is not None else Path.cwd())

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

    config = load_config_checked(
        config_path,
        loader=load_config,
        emit_startup_auth_warnings=False,
    )
    if story_path is not None:
        task = _build_task(story_path, slug=args.slug)
    else:
        # Same fetch the sprint path uses, so a re-review reads the issue exactly
        # as the run that produced the worktree did — including the slug, which is
        # what derives the default worktree path.
        try:
            task = GitHubIssueSource().fetch(str(issue), config.project_root)
        except IssueClosedError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        except (RuntimeError, ValueError) as exc:
            print(f"✗ could not read issue #{issue}: {exc}", file=sys.stderr)
            return 1
        if args.slug:
            task = replace(task, slug=args.slug)

    # Resolve workspace path
    if args.worktree:
        workspace_path = Path(args.worktree).resolve()
    else:
        workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)

    import theforge

    print(f"TheForge v{theforge.__version__} — review-only mode", file=sys.stderr)
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
        # This command runs the review phase. The shared resume setup would
        # otherwise disclose the *pipeline resume's* behaviour — that a
        # recovered escalation decision means REVIEW is skipped — from the one
        # command that is about to run it (#2239).
        reentry_mode=REENTRY_MODE_REVIEW,
    )

    # Write audit log
    audit_path = _write_audit(result, config, task, auto_merge=auto_merge)

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
    review_parser.add_argument(
        "story",
        nargs="?",
        help="Path to the story file (omit when using --issue)",
    )
    review_parser.add_argument(
        "--issue",
        type=int,
        help="Review a GitHub-issue-backed story by number, as the sprint path builds it",
    )
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
