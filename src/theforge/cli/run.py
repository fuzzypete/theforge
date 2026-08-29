"""forge run subcommand — execute the dev→review loop for a story."""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from theforge.artifacts import PLAN_PATH, resolve_plan_path
from theforge.cli.overrides import apply_base_branch_override
from theforge.cli.shared import (
    _apply_dev_model_override,
    _apply_plan_model_override,
    _build_task,
    _cmd_dry_run,
    _find_config,
    _write_audit,
    check_run_preconditions,
    load_config_checked,
)
from theforge.config import PREFLIGHT_GATE_DECOMPOSE, load_config
from theforge.config.provenance import VALUE_SOURCE_CLI_OVERRIDE, refresh_provenance
from theforge.coordinator.engine import (
    run_from_review,
    run_task,
)
from theforge.coordinator.state import Phase, parse_phase_name
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.sprint.dag import _triage_spec


def cmd_run(args: "argparse.Namespace") -> int:
    """Execute the dev→review loop for a story file."""
    from theforge import detach as _detach
    from theforge.coordinator.util import _generate_run_id

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

    # Runtime precondition guards: fail fast when catastrophic machine-local
    # state is tracked in git; warn (but proceed) on noise paths. This runs
    # before any agent invocation, workspace mutation, detach/daemonize, or
    # lock acquisition — see docs/plans/forge-storage-layout.md.
    blockers, warnings = check_run_preconditions(config_path.parent)
    for warning in warnings:
        print(f"⚠ {warning}", file=sys.stderr)
    if blockers:
        print("✗ forge run aborted — catastrophic paths are tracked in git:", file=sys.stderr)
        for blocker in blockers:
            print(f"  ✗ {blocker}", file=sys.stderr)
        return 1

    config = apply_base_branch_override(
        load_config_checked(config_path, loader=load_config),
        getattr(args, "base_branch", None),
    )

    # --dev-model override: provider/model@base_url
    if getattr(args, "dev_model", None):
        config = _apply_dev_model_override(config, args.dev_model)

    # --plan-model override: provider/model
    if getattr(args, "plan_model", None):
        config = _apply_plan_model_override(config, args.plan_model)

    # --reviewers N: trim review pool for this run (never mutates forge.yaml)
    if getattr(args, "reviewers", None) is not None:
        n = args.reviewers
        if n < 1:
            print(f"--reviewers must be >= 1, got {n}", file=sys.stderr)
            return 1
        trimmed_pool = config.review_pool[:n]
        new_synth = None if len(trimmed_pool) <= 1 else config.synthesis_profile
        config = refresh_provenance(
            dataclasses.replace(config, review_pool=trimmed_pool, synthesis_profile=new_synth),
            source_updates={
                "review_pool": VALUE_SOURCE_CLI_OVERRIDE,
                "synthesis_profile": VALUE_SOURCE_CLI_OVERRIDE,
            },
        )

    # --max-cycles N: cap review cycles for this run (never mutates forge.yaml)
    if getattr(args, "max_cycles", None) is not None:
        n = args.max_cycles
        if n < 1:
            print(f"--max-cycles must be >= 1, got {n}", file=sys.stderr)
            return 1
        new_retry = dataclasses.replace(config.retry, max_review_cycles=n)
        config = refresh_provenance(
            dataclasses.replace(config, retry=new_retry),
            source_updates={"retry.max_review_cycles": VALUE_SOURCE_CLI_OVERRIDE},
        )

    task = _build_task(story_path, slug=args.slug)

    # ── Detach (Popen re-exec) ─────────────────────────────────────────
    # Replaces the previous os.fork-based double-fork to avoid macOS
    # Objective-C fork-safety crashes once Foundation has been touched
    # (e.g., by `gh` subprocess calls earlier in the pipeline).
    if not getattr(args, "dry_run", False) and not getattr(args, "fg", False):
        if _detach.is_detached_child():
            import os as _os

            run_id = _os.environ.get("FORGE_DETACHED_RUN_ID") or _generate_run_id()
            _detach.setup_detached_child(run_id, task.slug, config.project_root)
            _detach.install_cleanup_handler(run_id, config.project_root)
        else:
            run_id = _generate_run_id()
            _detach.daemonize_run(run_id, task.slug, config.project_root)
            # daemonize_run never returns in the parent.
    else:
        run_id = _generate_run_id()
        # --fg / --dry-run stay in the foreground (no setup_detached_child), so
        # publish the run context here too for agent process-group registration.
        _detach.export_run_context(run_id, config.project_root)

    import theforge

    print(f"TheForge v{theforge.__version__}", file=sys.stderr)
    # Substrate provenance — name the runtime executing this run.
    try:
        from theforge.cli.substrate import emit_provenance

        emit_provenance(cwd=config.project_root, bypass_mismatch=False)
    except Exception:
        pass
    print(f"  Project:    {config.project}", file=sys.stderr)
    print(f"  Task:       {task.name}", file=sys.stderr)
    print(f"  Slug:       {task.slug}", file=sys.stderr)
    print(f"  Dev model:  {config.dev_profile.model}", file=sys.stderr)
    if len(config.review_pool) == 1:
        print(f"  Rev model:  {config.review_pool[0].model}", file=sys.stderr)
    else:
        pool_info = ", ".join(f"{p.name}({p.model})" for p in config.review_pool)
        print(f"  Rev pool:   {pool_info}", file=sys.stderr)
        if config.synthesis_profile:
            print(f"  Synthesis:  {config.synthesis_profile.model}", file=sys.stderr)
    print(f"  Max cycles: {config.retry.max_review_cycles}", file=sys.stderr)
    print(f"  Max iters:  {config.retry.max_dev_iterations}", file=sys.stderr)
    print(file=sys.stderr)

    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    if getattr(args, "dry_run", False):
        return _cmd_dry_run(config, task, story_path)

    # --interactive enables human review checkpoint on APPROVE; default is unattended
    interactive = getattr(args, "interactive", False)
    auto_merge = getattr(args, "auto_merge", False)
    plan_path = Path(args.plan).resolve() if args.plan else None
    resume = getattr(args, "resume", False)

    # ── Parse --until / --from phase flags ────────────────────────────
    stop_phase: Phase | None = None
    start_phase: Phase | None = None
    _explicit_from = bool(getattr(args, "from_phase", None))

    if getattr(args, "until", None):
        try:
            stop_phase = parse_phase_name(args.until)
        except ValueError as exc:
            print(f"✗ --until: {exc}", file=sys.stderr)
            return 1

    if _explicit_from:
        try:
            start_phase = parse_phase_name(args.from_phase)
        except ValueError as exc:
            print(f"✗ --from: {exc}", file=sys.stderr)
            return 1

    # --plan without explicit --from: sugar for --from dev when worktree already exists.
    # On fresh runs (no worktree), --plan keeps old behavior (WORKSPACE is created, PLAN skipped).
    if plan_path is not None and start_phase is None:
        _wt_for_plan = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
        if _wt_for_plan.exists():
            start_phase = Phase.DEV

    # ── Phase ordering validation ──────────────────────────────────────
    if start_phase is not None and stop_phase is not None:
        if start_phase.value > stop_phase.value:
            print(
                f"✗ --from {args.from_phase} comes after --until {args.until} in the pipeline; "
                "nothing would run.",
                file=sys.stderr,
            )
            return 1

    # ── --from / --until incompatible with --resume ────────────────────
    if resume and (start_phase is not None or stop_phase is not None):
        print(
            "✗ --from and --until are not compatible with --resume. Use one or the other.",
            file=sys.stderr,
        )
        return 1

    # ── Precondition validation for explicit --from ────────────────────
    if _explicit_from and start_phase is not None:
        expected_wt = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
        if not expected_wt.exists():
            print(
                f"✗ --from {args.from_phase}: worktree not found at {expected_wt}",
                file=sys.stderr,
            )
            return 1

        if start_phase == Phase.DEV and plan_path is None:
            # .forge/plan.md must exist in worktree (legacy forge_plan.md also accepted)
            plan_in_wt = resolve_plan_path(expected_wt)
            if not plan_in_wt.exists():
                legacy_plan_in_wt = expected_wt / "forge_plan.md"
                print(
                    "✗ --from dev: plan file not found in worktree "
                    f"({expected_wt / PLAN_PATH}; "
                    f"legacy fallback: {legacy_plan_in_wt}). "
                    "Provide --plan <file> to inject a plan.",
                    file=sys.stderr,
                )
                return 1

    no_pull = getattr(args, "no_pull", False)

    # Terminal disposition is derived from how the run actually ended: a return
    # out of the block below is an observed ending, anything that propagates is
    # a failure and must not be recorded as "completed".
    outcome = "completed"
    cause: str | None = None
    try:
        if resume:
            triage = _triage_spec(str(story_path), config, config.project_root)
            action_label = triage.action.upper().replace("_", " ")
            print(f"  Resume triage: {action_label} — {triage.reason}", file=sys.stderr)

            if triage.action in ("skip_merged", "skip"):
                print(f"  ✓ Nothing to do — {triage.reason}", file=sys.stderr)
                return 0

            if triage.action == "review" and triage.worktree_path is not None:
                result = run_from_review(
                    config,
                    task,
                    triage.worktree_path,
                    interactive=interactive,
                    auto_merge=auto_merge,
                    notify=not args.no_notify,
                    no_pull=no_pull,
                )
            elif triage.action == "dev" and triage.worktree_path is not None:
                from theforge.coordinator.engine import run_from_dev

                result = run_from_dev(
                    config,
                    task,
                    triage.worktree_path,
                    interactive=interactive,
                    auto_merge=auto_merge,
                    notify=not args.no_notify,
                    no_pull=no_pull,
                )
            else:
                # "full" or no worktree — run from scratch
                result = run_task(
                    config,
                    task,
                    interactive=interactive,
                    auto_merge=auto_merge,
                    notify=not args.no_notify,
                    plan_path=plan_path,
                    no_pull=no_pull,
                )
        else:
            result = run_task(
                config,
                task,
                interactive=interactive,
                auto_merge=auto_merge,
                notify=not args.no_notify,
                plan_path=plan_path,
                start_phase=start_phase,
                stop_phase=stop_phase,
                no_pull=no_pull,
            )

        # Write audit log
        audit_path = _write_audit(result, config, task, auto_merge=auto_merge)

        # Summary
        print(file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)
        # A story the preflight complexity gate returned gets neither mark: it
        # did not succeed and it did not fail (#2681).
        _gate_decision = getattr(result.state, "preflight_complexity_gate_decision", None)
        if _gate_decision == PREFLIGHT_GATE_DECOMPOSE:
            icon = "⤺"
        else:
            icon = "✓" if result.success else "✗"
        print(f"  {icon} {result.message}", file=sys.stderr)
        print(f"  Audit log: {audit_path}", file=sys.stderr)
        print(f"  Total cost: ${result.state.total_cost:.3f}", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)

        return 0 if result.success else 1
    except BaseException as exc:
        outcome = "failed"
        cause = _detach.format_exception_cause(exc)
        raise
    finally:
        # Write terminal marker then remove PID — ensures status is accurate even
        # if run_task raises. SIGTERM handler may have already written "stopped";
        # write_run_ended is a no-op when the file already exists.
        _detach.write_run_ended(run_id, config.project_root, outcome, cause=cause)
        _detach.remove_pid(run_id, config.project_root)


def register_parser(subparsers: object) -> None:
    """Register the 'run' subcommand parser."""
    run_parser = subparsers.add_parser("run", help="Run dev→review loop for a story")
    run_parser.add_argument("story", help="Path to the story file")
    run_parser.add_argument("--slug", help="Workspace slug (default: story filename stem)")
    run_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    run_parser.add_argument(
        "--base-branch",
        default=None,
        help="Override workspace.base_branch for this run without editing forge.yaml",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts and config without invoking agents",
    )
    run_parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Pause at APPROVE for human confirmation before merging",
    )
    run_parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="Merge feature branch into base branch after review APPROVE",
    )
    run_parser.add_argument(
        "--dev-model",
        help=(
            "Override dev model. Format: provider/model@base_url. "
            "Examples: openai/qwen2.5-coder:7b@http://localhost:11434/v1, "
            "anthropic/claude-opus-4-6"
        ),
    )
    run_parser.add_argument(
        "--plan-model",
        help=(
            "Override plan model. Format: provider/model or model. "
            "Examples: opus, anthropic/claude-opus-4-6"
        ),
    )
    run_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )
    run_parser.add_argument(
        "--no-notify",
        action="store_true",
        default=False,
        help="Suppress OS notifications",
    )
    run_parser.add_argument(
        "--plan",
        metavar="PATH",
        default=None,
        help="Inject an existing plan file, skipping the PLAN phase",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Triage existing worktree and resume from correct phase (REVIEW/DEV/full)",
    )
    run_parser.add_argument(
        "--until",
        metavar="PHASE",
        default=None,
        help=(
            "Stop pipeline after specified phase. "
            "Valid phases: workspace, preflight, plan, plan-review, "
            "dev, validate, review. Worktree preserved on stop. Exit code 0."
        ),
    )
    run_parser.add_argument(
        "--from",
        dest="from_phase",
        metavar="PHASE",
        default=None,
        help=(
            "Resume pipeline from specified phase, skipping earlier phases. "
            "Requires an existing worktree. "
            "Valid phases: dev, review (and others)."
        ),
    )
    run_parser.add_argument(
        "--reviewers",
        metavar="N",
        type=int,
        default=None,
        help="Limit review pool to first N reviewers for this run (does not modify forge.yaml)",
    )
    run_parser.add_argument(
        "--max-cycles",
        metavar="N",
        type=int,
        default=None,
        help="Cap review→dev cycles to N for this run (does not modify forge.yaml)",
    )
    run_parser.add_argument(
        "--fg",
        action="store_true",
        default=False,
        help="Run in foreground (skip daemonization)",
    )
    run_parser.add_argument(
        "--no-pull",
        action="store_true",
        default=False,
        help="Skip git pull --ff-only before creating a fresh worktree (offline/CI use)",
    )
