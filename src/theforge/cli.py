"""CLI entry point for TheForge.

Usage:
    forge run <spec-file> [--slug SLUG] [--config forge.yaml]
    forge init
    forge audit <audit-file>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .campaign import run_campaign
from .config import ForgeConfig, generate_default_config, load_config
from .coordinator import (
    CoordinatorResult,
    _fmt_dur,
    generate_audit_log,
    run_from_review,
    run_task,
)
from .coordinator import set_log_level as coordinator_set_log_level
from .ideate import run_ideation
from .runner import LogLevel
from .runner import set_log_level as runner_set_log_level
from .task import TaskSpec, build_dev_prompt, build_review_prompt, load_spec


def _find_config(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) looking for forge.yaml."""
    current = start or Path.cwd()
    for parent in [current, *current.parents]:
        candidate = parent / "forge.yaml"
        if candidate.exists():
            return candidate
    return None


def _parse_spec_frontmatter(spec_path: Path) -> dict:
    """Extract YAML frontmatter from a spec file.

    Spec files can optionally have YAML frontmatter delimited by ---:

        ---
        name: Phase 6H: per-user export
        slug: export-service
        file_scope:
          - src/export/
          - tests/test_export.py
        pytest_target: tests/test_export.py
        ---

        # Spec content starts here...

    If no frontmatter is present, returns empty dict.
    """
    text = spec_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}

    # Find closing ---
    end = text.find("---", 3)
    if end == -1:
        return {}

    frontmatter = text[3:end].strip()
    try:
        result = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return {}

    if not isinstance(result, dict):
        return {}
    return result


def _build_task(spec_path: Path, slug: str | None = None) -> TaskSpec:
    """Build a TaskSpec from a spec file, using frontmatter if available."""
    fm = _parse_spec_frontmatter(spec_path)

    # Slug: CLI arg > frontmatter > filename stem
    resolved_slug = slug or fm.get("slug") or spec_path.stem

    return TaskSpec(
        name=fm.get("name", spec_path.stem.replace("_", " ").replace("-", " ").title()),
        spec_path=spec_path.resolve(),
        slug=resolved_slug,
        file_scope=fm.get("file_scope", []),
        pytest_target=fm.get("pytest_target"),
    )


def _write_audit(result: CoordinatorResult, config: ForgeConfig, task: TaskSpec) -> Path:
    """Write the audit log to forge_audit.yaml in the project root and worktree."""
    audit = generate_audit_log(config, task, result)
    audit_path = config.project_root / "forge_audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Also write to worktree for per-spec persistence (not overwritten by next run).
    # Skip for ALREADY_DONE — no real work was done, worktree is just a checkout.
    already_done = result.state.preflight_verdict == "ALREADY_DONE"
    if not already_done and result.state.workspace_path and result.state.workspace_path.exists():
        worktree_audit_path = result.state.workspace_path / "forge_audit.yaml"
        with open(worktree_audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    return audit_path


def _cmd_dry_run(config: ForgeConfig, task: TaskSpec, spec_path: Path) -> int:
    """Print what would happen without invoking any agents."""
    spec_content = load_spec(spec_path)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    branch_name = config.workspace.branch_pattern.format(slug=task.slug)

    dev_prompt = build_dev_prompt(
        task,
        workspace_path=workspace_path,
        branch_name=branch_name,
        spec_content=spec_content,
        gate_command=config.validation.gate_command,
    )
    review_prompt = build_review_prompt(
        task,
        spec_content=spec_content,
        diff_text="(dry run — no diff available)",
        handoff_content="(dry run — no handoff available)",
    )

    sep = "=" * 60
    print(f"{sep}")
    print("DRY RUN — no agents will be invoked")
    print(f"{sep}\n")

    print(f"Workspace command: {config.workspace.create_command.format(slug=task.slug)}")
    print(f"Workspace path:    {workspace_path}")
    print(f"Branch:            {branch_name}")
    print(f"Gate command:      {config.validation.gate_command}")
    print()

    print(f"{sep}")
    print(f"DEV PROMPT ({len(dev_prompt)} chars)")
    print(f"  CLI:     {config.dev_profile.cli}")
    print(f"  Model:   {config.dev_profile.model}")
    print(f"  Budget:  ${config.dev_profile.budget_usd:.2f}")
    print(f"  Timeout: {config.dev_profile.timeout_seconds}s")
    print(f"  Tools:   {', '.join(config.dev_profile.allowed_tools)}")
    print(f"{sep}")
    print(dev_prompt)

    print(f"\n{sep}")
    print(f"REVIEW PROMPT ({len(review_prompt)} chars)")
    print(f"  CLI:     {config.review_profile.cli}")
    print(f"  Model:   {config.review_profile.model}")
    print(f"  Budget:  ${config.review_profile.budget_usd:.2f}")
    print(f"  Timeout: {config.review_profile.timeout_seconds}s")
    print(f"  Tools:   {', '.join(config.review_profile.allowed_tools)}")
    print(f"{sep}")
    print(review_prompt)

    return 0


# ── Commands ─────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    """Generate a starter forge.yaml in the current directory."""
    target = Path.cwd() / "forge.yaml"
    if target.exists():
        print(f"forge.yaml already exists: {target}", file=sys.stderr)
        return 1

    target.write_text(generate_default_config(), encoding="utf-8")
    print(f"Created {target}")
    print("Edit the file to match your project, then run: forge run <spec-file>")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Execute the dev→review loop for a spec file."""
    spec_path = Path(args.spec).resolve()
    if not spec_path.exists():
        print(f"Spec file not found: {spec_path}", file=sys.stderr)
        return 1

    # Find config
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).resolve()
    else:
        config_path = _find_config(spec_path.parent)

    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    config = load_config(config_path)
    task = _build_task(spec_path, slug=args.slug)

    print("TheForge v0.1.0", file=sys.stderr)
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
        return _cmd_dry_run(config, task, spec_path)

    # --auto disables human review; default (no flag) is interactive
    interactive = not getattr(args, "auto", False)
    auto_merge = getattr(args, "auto_merge", False)
    result = run_task(config, task, interactive=interactive, auto_merge=auto_merge)

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


def cmd_review(args: argparse.Namespace) -> int:
    """Run only the review pool on an existing worktree."""
    spec_path = Path(args.spec).resolve()
    if not spec_path.exists():
        print(f"Spec file not found: {spec_path}", file=sys.stderr)
        return 1

    # Find config
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).resolve()
    else:
        config_path = _find_config(spec_path.parent)

    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    config = load_config(config_path)
    task = _build_task(spec_path, slug=args.slug)

    # Resolve workspace path
    if args.worktree:
        workspace_path = Path(args.worktree).resolve()
    else:
        workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)

    print("TheForge v0.1.0 — review-only mode", file=sys.stderr)
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
    result = run_from_review(config, task, workspace_path, auto_merge=auto_merge)

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


def cmd_campaign(args: argparse.Namespace) -> int:
    """Run multiple specs sequentially via a campaign manifest."""
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"Campaign manifest not found: {manifest_path}", file=sys.stderr)
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

    try:
        result = run_campaign(
            config,
            manifest_path,
            auto_merge=auto_merge,
            interactive=interactive,
        )
    except ValueError as exc:
        print(f"Campaign error: {exc}", file=sys.stderr)
        return 1

    return 0 if result.specs_failed == 0 else 1


def cmd_ideate(args: argparse.Namespace) -> int:
    """Run multi-LLM deliberation to generate a spec from a brief."""
    # Load brief from file or inline string
    brief_arg = args.brief
    brief_path = Path(brief_arg)
    brief_is_file = brief_path.suffix in (".md", ".txt") and brief_path.exists()
    if brief_is_file:
        brief = brief_path.read_text(encoding="utf-8")
    else:
        brief = brief_arg

    # Find config — search from brief file's directory when brief is a file,
    # mirroring how cmd_run/cmd_campaign search relative to their input files.
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).resolve()
    elif brief_is_file:
        config_path = _find_config(brief_path.parent)
    else:
        config_path = _find_config()

    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_config(config_path)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    # Validate and cap rounds: must be in the inclusive range 1..3.
    if args.rounds < 1 or args.rounds > 3:
        print(
            f"--rounds must be between 1 and 3 (got {args.rounds})",
            file=sys.stderr,
        )
        return 1
    max_rounds = args.rounds

    dry_run: bool = args.dry_run
    explicit_output = args.output

    # For dry-run: pass output_path=None (no file written).
    # For explicit --output: pass the given path directly.
    # For default output (no --output, no --dry-run): pass specs_dir so run_ideation
    #   can derive the slug from the synthesized frontmatter and log the correct path.
    if dry_run:
        try:
            result = run_ideation(config, brief, None, max_rounds=max_rounds)
        except ValueError as exc:
            print(f"Ideation error: {exc}", file=sys.stderr)
            return 1
    elif explicit_output:
        output_path: Path = Path(explicit_output).resolve()
        try:
            result = run_ideation(config, brief, output_path, max_rounds=max_rounds)
        except ValueError as exc:
            print(f"Ideation error: {exc}", file=sys.stderr)
            return 1
    else:
        # Run ideation with output_path=None and specs_dir set so that run_ideation
        # determines the slug-based path and writes the file, keeping the log accurate.
        specs_dir = config.project_root / "specs"
        try:
            result = run_ideation(config, brief, None, specs_dir=specs_dir, max_rounds=max_rounds)
        except ValueError as exc:
            print(f"Ideation error: {exc}", file=sys.stderr)
            return 1

    if dry_run:
        if not result.success:
            print(f"Ideation failed: {result.final_synthesis}", file=sys.stderr)
            return 1
        print(result.final_synthesis)
        return 0

    return 0 if result.success else 1


def cmd_audit(args: argparse.Namespace) -> int:
    """Print a human-readable summary of an audit file."""
    audit_path = Path(args.file).resolve()
    if not audit_path.exists():
        print(f"Audit file not found: {audit_path}", file=sys.stderr)
        return 1

    with open(audit_path, encoding="utf-8") as f:
        audit = yaml.safe_load(f) or {}

    task = audit.get("task", {})
    outcome = audit.get("outcome", {})
    iterations = audit.get("iterations", {})
    cost = audit.get("cost", {})
    timing = audit.get("timing", {})
    workspace = audit.get("workspace", {})
    reviews = audit.get("reviews", [])
    preflight = audit.get("preflight")

    sep = "=" * 60
    icon = "✓" if outcome.get("success") else "✗"
    print(f"{sep}")
    print(f"{icon} {task.get('name', '?')}  [{outcome.get('final_phase', '?')}]")
    print(f"{sep}")
    print(f"  Message:  {outcome.get('message', '?')}")

    # Workspace
    if workspace.get("path") or workspace.get("branch"):
        print(f"  Workspace: {workspace.get('path', '?')}")
        print(f"  Branch:    {workspace.get('branch', '?')}")

    # Preflight
    if preflight:
        pf_verdict = preflight.get("verdict", "?")
        pf_reason = preflight.get("reason", "")
        pf_cost = preflight.get("cost_usd", 0.0) or 0.0
        print()
        print(f"  Preflight: {pf_verdict} (${pf_cost:.4f})")
        if pf_reason:
            print(f"    Reason: {pf_reason}")

    # Timing
    started = timing.get("started_at")
    finished = timing.get("finished_at")
    duration = timing.get("duration_seconds")
    if started or finished or duration is not None:
        print()
        print("  Timing")
        if started:
            print(f"    Started:  {started}")
        if finished:
            print(f"    Finished: {finished}")
        if duration is not None:
            mins, secs = divmod(int(duration), 60)
            print(f"    Duration: {mins}m {secs}s ({duration:.1f}s)")

    # Iterations
    print()
    print("  Iterations")
    print(f"    Dev iterations: {iterations.get('dev_iterations', '?')}")
    print(f"    Review cycles:  {iterations.get('review_cycles', '?')}")
    print(f"    Gate decisions: {iterations.get('gate_decisions', [])}")

    # Cost summary
    print()
    print("  Cost")
    print(f"    Total:  ${cost.get('total_usd', 0):.4f}")
    dev_inv = cost.get("dev_invocations", 0)
    rev_inv = cost.get("review_invocations", 0)
    print(f"    Dev:    ${cost.get('dev_usd', 0):.4f}  ({dev_inv} invocation(s))")
    print(f"    Review: ${cost.get('review_usd', 0):.4f}  ({rev_inv} invocation(s))")

    # Per-agent breakdown
    agents = cost.get("agents", [])
    if agents:
        print()
        print(f"  {'Role':<10} {'Profile':<20} {'Cost (USD)':>12}  {'Duration':>10}")
        print(f"  {'-' * 10} {'-' * 20} {'-' * 12}  {'-' * 10}")
        for a in agents:
            role = a.get("role", "?")
            profile = a.get("profile", "?")
            cost_usd = a.get("cost_usd", 0.0) or 0.0
            dur = a.get("duration_seconds")
            dur_str = _fmt_dur(dur) if dur is not None else "—"
            print(f"  {role:<10} {profile:<20} ${cost_usd:>11.4f}  {dur_str:>10}")

    # Reviews
    if reviews:
        print()
        print("  Reviews")
        for r in reviews:
            cycle = r.get("cycle", "?")
            verdict = r.get("verdict", "?")
            p1 = r.get("p1_count", 0)
            p2 = r.get("p2_count", 0)
            summary = r.get("summary", "")
            print(f"    Cycle {cycle}: {verdict} ({p1} P1, {p2} P2) — {summary}")

            findings = r.get("findings", [])
            if findings:
                for finding in findings:
                    sev = finding.get("severity", "?")
                    ffile = finding.get("file", "?")
                    line = finding.get("line")
                    loc = f"{ffile}:{line}" if line else ffile
                    desc = finding.get("description", "")
                    print(f"      [{sev}] {loc} — {desc}")

    if audit.get("error"):
        print()
        print(f"  Error: {audit['error']}")

    print(f"{sep}")
    return 0


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="TheForge — Deterministic multi-LLM development orchestrator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # forge init
    subparsers.add_parser("init", help="Generate a starter forge.yaml")

    # forge run
    run_parser = subparsers.add_parser("run", help="Run dev→review loop for a spec")
    run_parser.add_argument("spec", help="Path to the spec file")
    run_parser.add_argument("--slug", help="Workspace slug (default: spec filename stem)")
    run_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts and config without invoking agents",
    )
    run_parser.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Skip human review; run fully unattended (CI mode)",
    )
    run_parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="Merge feature branch into base branch after review APPROVE",
    )
    run_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )

    # forge review
    review_parser = subparsers.add_parser(
        "review", help="Run only the review pool on an existing worktree"
    )
    review_parser.add_argument("spec", help="Path to the spec file")
    review_parser.add_argument("--slug", help="Workspace slug (default: spec filename stem)")
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

    # forge campaign
    campaign_parser = subparsers.add_parser(
        "campaign", help="Run multiple specs sequentially from a campaign manifest"
    )
    campaign_parser.add_argument("manifest", help="Path to campaign.yaml manifest")
    campaign_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    campaign_parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="Merge each spec's branch after APPROVE",
    )
    campaign_parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Pause for human review at each spec",
    )
    campaign_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )

    # forge ideate
    ideate_parser = subparsers.add_parser(
        "ideate", help="Run multi-LLM deliberation to generate a spec from a brief"
    )
    ideate_parser.add_argument(
        "brief",
        help=(
            "Brief text or path to a .md/.txt file containing the brief. "
            "If the argument ends in .md or .txt and the file exists, it is read as a file; "
            "otherwise it is treated as inline text."
        ),
    )
    ideate_parser.add_argument(
        "--output",
        help="Output path for generated spec (default: specs/<slug>.md)",
    )
    ideate_parser.add_argument(
        "--rounds",
        type=int,
        default=2,
        help="Max deliberation rounds before surfacing residual divergence (default: 2, max: 3)",
    )
    ideate_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
    ideate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run deliberation and print synthesized spec to stdout without writing a file",
    )

    # forge audit
    audit_parser = subparsers.add_parser("audit", help="Print audit log summary")
    audit_parser.add_argument("file", help="Path to forge_audit.yaml")

    args = parser.parse_args()

    commands = {
        "init": cmd_init,
        "run": cmd_run,
        "review": cmd_review,
        "campaign": cmd_campaign,
        "ideate": cmd_ideate,
        "audit": cmd_audit,
    }

    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
