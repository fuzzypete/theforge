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

from .config import ForgeConfig, generate_default_config, load_config
from .coordinator import CoordinatorResult, generate_audit_log, run_task
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
    """Write the audit log to forge_audit.yaml in the project root."""
    audit = generate_audit_log(config, task, result)
    audit_path = config.project_root / "forge_audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
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
    print(f"  Project:   {config.project}", file=sys.stderr)
    print(f"  Task:      {task.name}", file=sys.stderr)
    print(f"  Slug:      {task.slug}", file=sys.stderr)
    print(f"  Dev model: {config.dev_profile.model}", file=sys.stderr)
    print(f"  Rev model: {config.review_profile.model}", file=sys.stderr)
    print(f"  Max cycles: {config.retry.max_review_cycles}", file=sys.stderr)
    print(f"  Max iters:  {config.retry.max_dev_iterations}", file=sys.stderr)
    print(file=sys.stderr)

    if getattr(args, "dry_run", False):
        return _cmd_dry_run(config, task, spec_path)

    result = run_task(config, task)

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
    reviews = audit.get("reviews", [])

    icon = "✓" if outcome.get("success") else "✗"
    print(f"{icon} {task.get('name', '?')}")
    print(f"  Phase: {outcome.get('final_phase', '?')}")
    print(f"  Message: {outcome.get('message', '?')}")
    print(f"  Dev iterations: {iterations.get('dev_iterations', '?')}")
    print(f"  Review cycles: {iterations.get('review_cycles', '?')}")
    print(f"  Gate decisions: {iterations.get('gate_decisions', [])}")
    print(f"  Cost: ${cost.get('total_usd', 0):.3f}")

    if reviews:
        print("  Reviews:")
        for r in reviews:
            print(
                f"    Cycle {r.get('cycle', '?')}: {r.get('verdict', '?')} "
                f"({r.get('p1_count', 0)} P1, {r.get('p2_count', 0)} P2) "
                f"— {r.get('summary', '')}"
            )

    if audit.get("error"):
        print(f"  Error: {audit['error']}")

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

    # forge audit
    audit_parser = subparsers.add_parser("audit", help="Print audit log summary")
    audit_parser.add_argument("file", help="Path to forge_audit.yaml")

    args = parser.parse_args()

    commands = {
        "init": cmd_init,
        "run": cmd_run,
        "audit": cmd_audit,
    }

    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
