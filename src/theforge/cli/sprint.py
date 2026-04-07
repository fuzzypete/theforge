"""forge sprint subcommand — run multiple stories from a sprint manifest or GitHub query."""

from __future__ import annotations

import sys
from pathlib import Path

from theforge.cli.overrides import apply_base_branch_override
from theforge.cli.shared import _find_config
from theforge.config import load_config
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.sprint import run_sprint
from theforge.sprint.launch_guard import acquire_launch_story_locks
from theforge.sprint.lock import release_story_locks
from theforge.sprint.preflight import reacquire_story_locks_in_daemon
from theforge.sprint.runner import parse_manifest_slugs


def cmd_sprint(args: object) -> int:
    """Run multiple stories via a sprint manifest or GitHub query."""
    from theforge import daemon as _daemon
    from theforge import detach as _detach
    from theforge.coordinator.util import _generate_run_id

    milestone: str | None = getattr(args, "milestone", None)
    label: str | None = getattr(args, "label", None)
    issues_arg: str | None = getattr(args, "issues", None)
    query_mode = bool(milestone or label or issues_arg)
    manifest_arg: str | None = getattr(args, "manifest", None)

    # ── Validate argument combinations ──────────────────────────────────
    if not query_mode and not manifest_arg:
        print(
            "forge sprint: provide a manifest path or use --milestone/--label/--issues",
            file=sys.stderr,
        )
        return 1

    if query_mode and manifest_arg:
        print(
            "forge sprint: --milestone/--label/--issues and a manifest path "
            "are mutually exclusive",
            file=sys.stderr,
        )
        return 1

    selected_queries = [
        flag
        for flag, value in (
            ("--milestone", milestone),
            ("--label", label),
            ("--issues", issues_arg),
        )
        if value
    ]
    if len(selected_queries) > 1:
        print(
            f"forge sprint: {', '.join(selected_queries)} are mutually exclusive",
            file=sys.stderr,
        )
        return 1

    budget_str: str | None = getattr(args, "budget", None)
    if query_mode and budget_str is None:
        print(
            "forge sprint: --budget <usd> is required when using "
            "--milestone, --label, or --issues",
            file=sys.stderr,
        )
        return 1

    # ── Find and load config ─────────────────────────────────────────────
    config_path: Path | None = None
    if getattr(args, "config", None):
        config_path = Path(args.config).resolve()
    elif query_mode:
        config_path = _find_config(Path.cwd())
    else:
        manifest_path = Path(manifest_arg).resolve()
        config_path = _find_config(manifest_path.parent)

    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    config = apply_base_branch_override(
        load_config(config_path), getattr(args, "base_branch", None)
    )

    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    auto_merge = getattr(args, "auto_merge", False)
    interactive = getattr(args, "interactive", False)
    resume = getattr(args, "resume", False)
    no_pull = getattr(args, "no_pull", False)
    dry_run = getattr(args, "dry_run", False)
    max_parallel: int | None = getattr(args, "parallel", None)

    # ── Query mode: fetch issues and build ResolvedSprint ───────────────
    if query_mode:
        return _run_query_mode(
            args=args,
            config=config,
            config_path=config_path,
            milestone=milestone,
            label=label,
            issues_arg=issues_arg,
            budget_str=budget_str,
            dry_run=dry_run,
            max_parallel=max_parallel,
            auto_merge=auto_merge,
            interactive=interactive,
            resume=resume,
            no_pull=no_pull,
            _daemon=_daemon,
            _detach=_detach,
            _generate_run_id=_generate_run_id,
        )

    # ── Manifest mode (original behaviour, unchanged) ────────────────────
    manifest_path = Path(manifest_arg).resolve()
    if not manifest_path.exists():
        print(f"Sprint manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    slugs = parse_manifest_slugs(config, manifest_path)
    locked_fds, launch_error = _acquire_launch_locks(slugs=slugs, config=config, resume=resume)
    if launch_error is not None:
        return launch_error

    if not getattr(args, "fg", False) and not getattr(args, "detach", False):
        run_id = _generate_run_id()
        slug = manifest_path.stem
        _detach.daemonize_run(run_id, slug, config.project_root)
        locked_fds = reacquire_story_locks_in_daemon(
            slugs,
            config.project_root,
            locked_fds,
        )
        _detach.install_cleanup_handler(run_id, config.project_root)
        print("[forge] Detached sprint starting", file=sys.stderr, flush=True)
    else:
        run_id = _generate_run_id()
        slug = manifest_path.stem

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
            run_id=run_id,
        )
    except Exception as exc:
        import traceback

        print(f"Sprint error: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return 1
    finally:
        release_story_locks(locked_fds)

    _detach.remove_pid(run_id, config.project_root)
    return 0 if result.specs_failed == 0 else 1


def _acquire_launch_locks(
    slugs: list[str], config: object, resume: bool
) -> tuple[list, int | None]:
    return acquire_launch_story_locks(
        slugs=slugs,
        config=config,
        resume=resume,
    )


def _run_query_mode(
    *,
    args: object,
    config: object,
    config_path: Path,
    milestone: str | None,
    label: str | None,
    issues_arg: str | None = None,
    budget_str: str | None,
    dry_run: bool,
    max_parallel: int | None,
    auto_merge: bool,
    interactive: bool,
    resume: bool,
    no_pull: bool,
    _daemon: object,
    _detach: object,
    _generate_run_id: object,
) -> int:
    """Handle --milestone / --label / --issues query mode."""
    from theforge.sprint.dag import resolve_satisfied_dependencies
    from theforge.sprint.query import (
        assign_dependency_batches_with_satisfied,
        build_resolved_sprint,
        fetch_issues_by_numbers,
        fetch_issues_for_label,
        fetch_issues_for_milestone,
    )

    try:
        budget_usd = float(budget_str)
    except (TypeError, ValueError):
        print(f"forge sprint: --budget must be a number, got {budget_str!r}", file=sys.stderr)
        return 1
    if budget_usd <= 0:
        print(f"forge sprint: --budget must be > 0, got {budget_usd}", file=sys.stderr)
        return 1

    # Query mode is sequential by default unless the caller explicitly opts in.
    effective_max_parallel = 1 if max_parallel is None else max_parallel

    query_desc = (
        f"milestone '{milestone}'"
        if milestone
        else f"label '{label}'"
        if label
        else f"issues '{issues_arg}'"
    )

    # Fetch issue list (lightweight — just numbers and titles)
    try:
        if milestone:
            issues = fetch_issues_for_milestone(milestone, config.project_root)
        elif label:
            issues = fetch_issues_for_label(label, config.project_root)
        else:
            try:
                issue_numbers = [
                    int(part.strip()) for part in issues_arg.split(",") if part.strip()
                ]
            except ValueError as exc:
                raise RuntimeError(
                    "--issues must be a comma-separated list of integer issue numbers"
                ) from exc
            if not issue_numbers:
                raise RuntimeError("No issue numbers provided")
            issues = fetch_issues_by_numbers(issue_numbers, config.project_root)
    except RuntimeError as exc:
        print(f"[forge] GitHub query failed for {query_desc}: {exc}", file=sys.stderr)
        return 1

    if not issues:
        print(
            f"[forge] No open issues found for {query_desc} — nothing to run.",
            file=sys.stderr,
        )
        return 0

    sprint_name: str = getattr(args, "name", None) or milestone or label or f"issues-{issues_arg}"

    # Build full ResolvedSprint (fetches individual issue bodies via gh)
    try:
        resolved = build_resolved_sprint(
            issues=issues,
            name=sprint_name,
            budget_usd=budget_usd,
            max_parallel=effective_max_parallel,
            project_root=config.project_root,
        )
    except RuntimeError as exc:
        print(f"[forge] Failed to resolve sprint from {query_desc}: {exc}", file=sys.stderr)
        return 1

    if not resolved.stories:
        print(
            f"[forge] No stories could be fetched for {query_desc} — nothing to run.",
            file=sys.stderr,
        )
        return 0

    if dry_run:
        tasks = [task for task, _src, _ref in resolved.stories]
        satisfied = resolve_satisfied_dependencies(
            tasks,
            project_root=config.project_root,
            base_branch=config.workspace.base_branch,
            branch_pattern=config.workspace.branch_pattern,
        )
        batch_plan = assign_dependency_batches_with_satisfied(
            tasks,
            effective_max_parallel,
            satisfied=satisfied,
        )
        print(f"[dry-run] {query_desc}  {len(tasks)} issue(s)  sprint='{sprint_name}'")
        for task, _src, _ref in resolved.stories:
            deps = ", ".join(task.depends_on) if task.depends_on else "-"
            if task.slug in batch_plan.blocked:
                status = f"blocked=[{', '.join(batch_plan.blocked[task.slug])}]"
            else:
                batch = batch_plan.assignments.get(task.slug)
                status = "stalled" if batch is None else f"batch={batch}"
            print(
                f"  {status}  #{task.github_issue:>5}  {task.slug:<12} deps=[{deps}]  {task.name}"
            )
        return 0

    # ── Lock acquisition using resolved slugs (no manifest path needed) ──
    slugs = [task.slug for task, _src, _ref in resolved.stories]
    locked_fds, launch_error = _acquire_launch_locks(slugs=slugs, config=config, resume=resume)
    if launch_error is not None:
        return launch_error

    # ── Daemonization: slug from sprint name, not manifest filename ───────
    run_id = _generate_run_id()
    sprint_slug = sprint_name.replace(" ", "-").replace("/", "-").lower()[:50]

    if not getattr(args, "fg", False) and not getattr(args, "detach", False):
        _detach.daemonize_run(run_id, sprint_slug, config.project_root)
        locked_fds = reacquire_story_locks_in_daemon(
            slugs,
            config.project_root,
            locked_fds,
        )
        _detach.install_cleanup_handler(run_id, config.project_root)
        print("[forge] Detached sprint starting", file=sys.stderr, flush=True)

    # Query mode does not support daemon submission (no manifest file to pass)
    if getattr(args, "detach", False) and _daemon.is_daemon_running(config.project_root):
        release_story_locks(locked_fds)
        print(
            "[forge] --detach is not supported in query mode (--milestone/--label/--issues).\n"
            "        Run with --fg or without --detach instead.",
            file=sys.stderr,
        )
        return 1

    try:
        result = run_sprint(
            config,
            resolved,
            auto_merge=auto_merge,
            interactive=interactive,
            notify=not args.no_notify,
            resume=resume,
            no_pull=no_pull,
            run_id=run_id,
        )
    except Exception as exc:
        import traceback

        print(f"Sprint error: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return 1
    finally:
        release_story_locks(locked_fds)

    _detach.remove_pid(run_id, config.project_root)
    return 0 if result.specs_failed == 0 else 1


def register_parser(subparsers: object) -> None:
    """Register the 'sprint' subcommand parser."""
    sprint_parser = subparsers.add_parser(
        "sprint",
        help="Run multiple stories from a sprint manifest or GitHub query",
    )
    # Manifest path is now optional — query mode uses --milestone/--label/--issues instead
    sprint_parser.add_argument(
        "manifest",
        nargs="?",
        default=None,
        help="Path to sprint.yaml manifest (omit when using --milestone, --label, or --issues)",
    )
    sprint_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    sprint_parser.add_argument(
        "--base-branch",
        default=None,
        help="Override workspace.base_branch for this sprint without editing forge.yaml",
    )

    # ── GitHub query mode ────────────────────────────────────────────────
    sprint_parser.add_argument(
        "--milestone",
        metavar="NAME",
        help="Run all open issues in a GitHub milestone (requires --budget)",
    )
    sprint_parser.add_argument(
        "--label",
        metavar="LABEL",
        help="Run all open issues with a GitHub label (requires --budget)",
    )
    sprint_parser.add_argument(
        "--issues",
        metavar="N[,N,...]",
        default=None,
        help="Run specific GitHub issues by comma-separated number (requires --budget)",
    )
    sprint_parser.add_argument(
        "--budget",
        metavar="USD",
        help="Budget ceiling in USD (required for --milestone/--label/--issues)",
    )
    sprint_parser.add_argument(
        "--name",
        metavar="NAME",
        help="Override sprint name (default: milestone or label value)",
    )
    sprint_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the resolved issue list without executing",
    )
    sprint_parser.add_argument(
        "--parallel",
        metavar="N",
        type=int,
        default=None,
        help="Maximum concurrent stories (overrides forge.yaml max_parallel)",
    )

    # ── Common options ───────────────────────────────────────────────────
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
        help="When daemon is running, submit and return immediately (manifest mode only)",
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
