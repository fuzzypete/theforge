"""CLI entry point for TheForge.

Usage:
    forge run <story-file> [--slug SLUG] [--config forge.yaml]
    forge init
    forge audit <audit-file>
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from .config import PROVIDER_API_KEY_MAP, ForgeConfig, generate_default_config, load_config
from .coordinator import (
    CoordinatorResult,
    _fmt_duration,
    generate_audit_log,
    run_from_review,
    run_task,
)
from .coordinator import set_log_level as coordinator_set_log_level
from .ideate import generate_ideation_audit, run_ideation
from .runner import AgentResult, LogLevel
from .runner import set_log_level as runner_set_log_level
from .runner_api import run_api_agent
from .sprint import _triage_spec, run_sprint
from .task import TaskSpec, TaskStory, build_dev_prompt, build_review_prompt, load_story


def _find_config(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) looking for forge.yaml."""
    current = start or Path.cwd()
    for parent in [current, *current.parents]:
        candidate = parent / "forge.yaml"
        if candidate.exists():
            return candidate
    return None


def _parse_story_frontmatter(story_path: Path) -> dict:
    """Extract YAML frontmatter from a story file.

    Story files can optionally have YAML frontmatter delimited by ---:

        ---
        name: Phase 6H: per-user export
        slug: export-service
        pytest_target: tests/test_export.py
        ---

        # Story content starts here...

    If no frontmatter is present, returns empty dict.
    """
    text = story_path.read_text(encoding="utf-8")
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


def _build_task(story_path: Path, slug: str | None = None) -> TaskStory:
    """Build a TaskStory from a story file, using frontmatter if available."""
    fm = _parse_story_frontmatter(story_path)

    # Slug: CLI arg > frontmatter > filename stem
    resolved_slug = slug or fm.get("slug") or story_path.stem

    return TaskStory(
        name=fm.get("name", story_path.stem.replace("_", " ").replace("-", " ").title()),
        story_path=story_path.resolve(),
        slug=resolved_slug,
        pytest_target=fm.get("pytest_target"),
        gate_override=fm.get("gate"),
    )


def _append_history(audits_dir: Path, record: dict) -> None:
    """Append a record to .forge/audits/history.jsonl (never overwritten)."""
    history_path = audits_dir / "history.jsonl"
    try:
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass  # best-effort — never block a run on history write failure


def _write_audit(result: CoordinatorResult, config: ForgeConfig, task: TaskSpec) -> Path:
    """Write the audit log to .forge/audits/forge_audit.yaml and worktree."""
    audit = generate_audit_log(config, task, result)
    audits_dir = config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "forge_audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Append to history log (JSONL, never overwritten).
    _append_history(audits_dir, audit)
    # Also write to worktree for per-story persistence (not overwritten by next run).
    # Skip for ALREADY_DONE — no real work was done, worktree is just a checkout.
    already_done = result.state.preflight_verdict == "ALREADY_DONE"
    if not already_done and result.state.workspace_path and result.state.workspace_path.exists():
        worktree_audit_path = result.state.workspace_path / "forge_audit.yaml"
        with open(worktree_audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Copy to durable per-story log dir (survives worktree cleanup)
    if result.state.log_dir is not None:
        try:
            log_audit_path = result.state.log_dir / "audit.yaml"
            log_audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_audit_path, "w", encoding="utf-8") as f:
                yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass  # best-effort
    return audit_path


def _cmd_dry_run(config: ForgeConfig, task: TaskStory, story_path: Path) -> int:
    """Print what would happen without invoking any agents."""
    story_content = load_story(story_path)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    branch_name = config.workspace.branch_pattern.format(slug=task.slug)

    dev_prompt = build_dev_prompt(
        task,
        workspace_path=workspace_path,
        branch_name=branch_name,
        story_content=story_content,
        gate_command=config.validation.gate_command,
    )
    review_prompt = build_review_prompt(
        task,
        story_content=story_content,
        commit_log="(dry run — no commits available)",
        workspace_path=str(workspace_path),
        branch=branch_name,
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

_SECRETS_FILE = ".forge/.env"
_GITIGNORE_ENTRY = ".forge/.env"

_STORY_TEMPLATE = """\
---
# Story frontmatter — required fields
name: "Short human-readable title"
slug: my-feature-slug        # used for branch and worktree names
pytest_target: tests/        # path passed to pytest for gate
---

# Story title

## Problem / context

One paragraph explaining WHY this change is needed and WHAT problem it solves.
Background and motivation belong here. This section is informational — it is
NOT a list of requirements.

## Acceptance criteria

<!--
  ACs are the definitive checklist. The dev agent will implement exactly what
  is stated here. Write them as observable, testable behaviors.
  Each AC should be a single bullet starting with a verb.
-->

- The system does X when Y
- Existing tests continue to pass
- New test verifies Z

## Notes (optional)

Any additional context, constraints, or design guidance. The dev agent will
read this as context, not as requirements.
"""


def _ensure_gitignored(project_root: Path) -> None:
    """Append .forge/.env to .gitignore if not already present."""
    gitignore = project_root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if _GITIGNORE_ENTRY in content.splitlines():
            return
        separator = "" if content.endswith("\n") else "\n"
        gitignore.write_text(content + separator + _GITIGNORE_ENTRY + "\n", encoding="utf-8")
    else:
        gitignore.write_text(_GITIGNORE_ENTRY + "\n", encoding="utf-8")


def _generate_secrets_skeleton() -> str:
    """Generate .env skeleton from PROVIDER_API_KEY_MAP."""
    lines = [
        "# .forge/.env — project-scoped secrets for TheForge",
        "# Copy this file to .forge/.env and fill in the values you need.",
        "# This file (.env.example) is tracked; .env is gitignored.",
        "",
    ]
    for _provider, key in PROVIDER_API_KEY_MAP.items():
        lines.append(f"# {key}=")
    lines.append("# NTFY_URL=https://ntfy.sh/your-topic-here")
    lines.append("")
    return "\n".join(lines)


def cmd_secrets_init(args: argparse.Namespace) -> int:
    """Create .forge/.env skeleton and update .gitignore."""
    project_root = Path.cwd()
    env_path = project_root / ".forge" / ".env"
    secrets_yaml_path = project_root / ".forge" / "secrets.yaml"

    if secrets_yaml_path.exists() and not env_path.exists():
        print(
            "⚠ .forge/secrets.yaml detected — migrate to .forge/.env (see .forge/.env.example)",
            file=sys.stderr,
        )

    if env_path.exists():
        print(
            f"Warning: {env_path} already exists. Not overwriting.",
            file=sys.stderr,
        )
        return 0

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_generate_secrets_skeleton(), encoding="utf-8")
    print(f"Created {env_path}")

    _ensure_gitignored(project_root)
    print(f"Updated .gitignore to exclude {_GITIGNORE_ENTRY}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Generate a starter forge.yaml in the current directory."""
    target = Path.cwd() / "forge.yaml"
    if target.exists():
        print(f"forge.yaml already exists: {target}", file=sys.stderr)
        return 1

    target.write_text(generate_default_config(), encoding="utf-8")
    print(f"Created {target}")

    stories_dir = Path.cwd() / "stories"
    stories_dir.mkdir(exist_ok=True)
    template_path = stories_dir / "TEMPLATE.md"
    if not template_path.exists():
        template_path.write_text(_STORY_TEMPLATE, encoding="utf-8")
        print(f"Created {template_path}")

    print("Edit forge.yaml to match your project, then run: forge run <story-file>")

    _ensure_gitignored(Path.cwd())
    return 0


def _apply_dev_model_override(config: "ForgeConfig", spec: str) -> "ForgeConfig":
    """Override the dev profile with a --dev-model spec.

    Format: provider/model@base_url
    Examples:
        openai/qwen2.5-coder:7b@http://localhost:11434/v1
        anthropic/claude-opus-4-6
    """
    from dataclasses import replace

    base_url = None
    if "@" in spec:
        spec, base_url = spec.rsplit("@", 1)

    if "/" in spec:
        provider, model = spec.split("/", 1)
    else:
        provider = "openai"
        model = spec

    new_dev = replace(
        config.dev_profile,
        cli=None,
        provider=provider,
        model=model,
        base_url=base_url,
        budget_usd=config.dev_profile.budget_usd,
    )
    return replace(config, dev_profile=new_dev)


def cmd_run(args: argparse.Namespace) -> int:
    """Execute the dev→review loop for a story file."""
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

    # AC-5: warn if .forge/secrets.yaml is tracked by git (not gitignored)
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", _SECRETS_FILE],
            cwd=str(config_path.parent),
            capture_output=True,
        )
        if tracked.returncode == 0:
            print(
                f"⚠ {_SECRETS_FILE} is not gitignored — run 'forge secrets-init' to fix",
                file=sys.stderr,
            )
    except (OSError, FileNotFoundError):
        pass  # not a git repo or git not installed — ignore

    config = load_config(config_path)

    # --dev-model override: provider/model@base_url
    if getattr(args, "dev_model", None):
        config = _apply_dev_model_override(config, args.dev_model)

    task = _build_task(story_path, slug=args.slug)

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
        return _cmd_dry_run(config, task, story_path)

    # --interactive enables human review checkpoint on APPROVE; default is unattended
    interactive = getattr(args, "interactive", False)
    auto_merge = getattr(args, "auto_merge", False)
    plan_path = Path(args.plan).resolve() if args.plan else None
    resume = getattr(args, "resume", False)

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
            )
        elif triage.action == "dev" and triage.worktree_path is not None:
            from .coordinator import run_from_dev

            result = run_from_dev(
                config,
                task,
                triage.worktree_path,
                interactive=interactive,
                auto_merge=auto_merge,
                notify=not args.no_notify,
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
            )
    else:
        result = run_task(
            config,
            task,
            interactive=interactive,
            auto_merge=auto_merge,
            notify=not args.no_notify,
            plan_path=plan_path,
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


def cmd_review(args: argparse.Namespace) -> int:
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


def cmd_sprint(args: argparse.Namespace) -> int:
    """Run multiple stories sequentially via a sprint manifest."""
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

    try:
        result = run_sprint(
            config,
            manifest_path,
            auto_merge=auto_merge,
            interactive=interactive,
            notify=not args.no_notify,
            resume=resume,
        )
    except ValueError as exc:
        print(f"Sprint error: {exc}", file=sys.stderr)
        return 1

    return 0 if result.specs_failed == 0 else 1


def cmd_ideate(args: argparse.Namespace) -> int:
    """Run multi-LLM deliberation to generate a story from a brief."""
    # Load brief from file or inline string
    brief_arg = args.brief
    brief_path = Path(brief_arg)
    brief_is_file = brief_path.suffix in (".md", ".txt") and brief_path.exists()
    if brief_is_file:
        brief = brief_path.read_text(encoding="utf-8")
    else:
        brief = brief_arg

    # Find config — search from brief file's directory when brief is a file,
    # mirroring how cmd_run/cmd_sprint search relative to their input files.
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

    if getattr(args, "verbose", False):
        runner_set_log_level(LogLevel.VERBOSE)

    # Validate and cap rounds: must be in the inclusive range 1..3.
    if args.rounds < 1 or args.rounds > 3:
        print(
            f"--rounds must be between 1 and 3 (got {args.rounds})",
            file=sys.stderr,
        )
        return 1
    max_rounds = args.rounds

    dry_run: bool = args.dry_run

    # Compute output_path and stories_dir once before calling run_ideation.
    # dry-run → no file written (both None); explicit --output → output_path set;
    # default → stories_dir set so run_ideation derives the slug-based filename.
    run_output_path: Path | None = None
    run_stories_dir: Path | None = None
    if not dry_run:
        if args.output:
            run_output_path = Path(args.output).resolve()
        else:
            run_stories_dir = config.project_root / "stories"

    ideation_started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ideation_start_mono = time.monotonic()

    try:
        result = run_ideation(
            config, brief, run_output_path, stories_dir=run_stories_dir, max_rounds=max_rounds
        )
    except ValueError as exc:
        print(f"Ideation error: {exc}", file=sys.stderr)
        return 1

    duration_seconds = time.monotonic() - ideation_start_mono
    ideation_finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if dry_run:
        if not result.success:
            print(f"Ideation failed: {result.final_synthesis}", file=sys.stderr)
            return 1
        print(result.final_synthesis)
        return 0

    # Write audit record only for real (non-dry-run) runs.
    audit = generate_ideation_audit(
        config,
        brief,
        result,
        started_at=ideation_started_at,
        finished_at=ideation_finished_at,
        duration_seconds=duration_seconds,
    )
    audits_dir = config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "forge_ideation_audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    print(f"[forge] Audit log: {audit_path}", file=sys.stderr)

    return 0 if result.success else 1


def _cmd_audit_ideate(audit: dict) -> int:
    """Print a human-readable summary of an ideation audit record."""
    sep = "=" * 60
    icon = "✓" if audit.get("success") else "✗"
    brief_preview = (audit.get("brief", "") or "")[:80].replace("\n", " ")
    spec_path = audit.get("story_path") or audit.get("spec_path") or "(none)"
    print(sep)
    print(f"{icon} IDEATE  →  {spec_path}")
    print(sep)
    print(f"  Brief:   {brief_preview!r}")

    pool = audit.get("model_pool", [])
    synth = audit.get("synthesis_profile")
    print(f"  Pool:    {'+'.join(pool) or '?'}  synthesis={synth or '—'}")

    if audit.get("human_decision_required"):
        print("  ⚠ Human decisions required")
        for item in audit.get("residual_divergence", []):
            print(f"    - {item}")

    timing = audit.get("timing", {})
    duration = timing.get("duration_seconds")
    started = timing.get("started_at")
    if started or duration is not None:
        print()
        print("  Timing")
        if started:
            print(f"    Started:  {started}")
        if duration is not None:
            mins, secs = divmod(int(duration), 60)
            print(f"    Duration: {mins}m {secs}s ({duration:.1f}s)")

    cost = audit.get("cost", {})
    print()
    print(f"  Cost:  ${cost.get('total_usd', 0):.4f}")

    rounds = audit.get("rounds", [])
    if rounds:
        print()
        print("  Rounds")
        for r in rounds:
            rn = r.get("round_number", "?")
            conv = r.get("converged_count", 0)
            div = r.get("divergent_count", 0)
            print(f"    Round {rn}:  converged={conv}  divergent={div}")
            for item in r.get("divergent_items", []):
                print(f"      ✗ {item}")

    print(sep)
    return 0


_CHECK_PROVIDERS_PROMPT = (
    "Review this change: -    x = 1\n+    x = 2\nVerdict: APPROVE. No findings."
)


def cmd_check_providers(args: argparse.Namespace) -> int:
    """Smoke-test all API-mode profiles in forge.yaml."""
    config_path = getattr(args, "config", None)
    if config_path:
        config_path = Path(config_path)
    else:
        config_path = _find_config()
    if config_path is None:
        print("[check-providers] No forge.yaml found", file=sys.stderr)
        return 1

    config = load_config(config_path)

    # Collect all unique API profiles (by name) across all config slots.
    seen: dict[str, object] = {}
    candidates = [
        config.dev_profile,
        config.preflight_profile,
        *config.review_pool,
    ]
    if config.synthesis_profile is not None:
        candidates.append(config.synthesis_profile)
    if config.plan_agent_review.enabled:
        candidates.extend(config.plan_agent_review.profiles)

    api_profiles = []
    for p in candidates:
        if p.mode == "api" and p.name not in seen:
            seen[p.name] = p
            api_profiles.append(p)

    # Optional single-profile filter.
    profile_filter = getattr(args, "profile", None)
    if profile_filter:
        api_profiles = [p for p in api_profiles if p.name == profile_filter]
        if not api_profiles:
            print(
                f"[check-providers] No API profile named '{profile_filter}' found in forge.yaml",
                file=sys.stderr,
            )
            return 1

    print(f"[check-providers] forge.yaml: {len(api_profiles)} API profile(s) found\n")

    working_dir = config_path.parent

    def _run_one(profile: object) -> tuple:
        # Use single-shot mode by ensuring allowed_tools is empty.
        single_shot = dataclasses.replace(profile, allowed_tools=())
        t0 = time.perf_counter()
        try:
            result: AgentResult = run_api_agent(
                prompt=_CHECK_PROVIDERS_PROMPT,
                profile=single_shot,
                working_dir=working_dir,
                quiet=True,
                secrets=config.secrets,
            )
            elapsed = time.perf_counter() - t0
            return (profile, elapsed, result)
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            return (profile, elapsed, exc)

    results = []
    with ThreadPoolExecutor(max_workers=len(api_profiles) or 1) as pool:
        futures = {pool.submit(_run_one, p): p for p in api_profiles}
        for fut in as_completed(futures):
            results.append(fut.result())

    # Sort results to match original profile order.
    order = {p.name: i for i, p in enumerate(api_profiles)}
    results.sort(key=lambda t: order.get(t[0].name, 999))

    any_failed = False
    for profile, elapsed, outcome in results:
        provider = profile.provider or "?"
        model = profile.model
        name_col = f"{profile.name:<22}"
        prov_col = f"{provider:<10}"
        model_col = f"{model:<28}"

        is_local = profile.base_url and (
            "localhost" in profile.base_url or "127.0.0.1" in profile.base_url
        )
        local_tag = " [local]" if is_local else ""

        if isinstance(outcome, Exception):
            any_failed = True
            print(
                f"  {name_col} {prov_col} {model_col} \u2717  {type(outcome).__name__}: {outcome}"
            )
        elif not outcome.success:
            any_failed = True
            short_err = (outcome.output or "unknown error")[:120]
            print(f"  {name_col} {prov_col} {model_col} \u2717  {short_err}")
        elif outcome.structured_data is None or "verdict" not in outcome.structured_data:
            any_failed = True
            msg = "no valid verdict in structured output"
            print(f"  {name_col} {prov_col} {model_col} \u2717  {msg}")
        else:
            if is_local or outcome.cost_usd == 0.0:
                cost_str = "$0.000"
            elif outcome.cost_usd is not None:
                cost_str = f"${outcome.cost_usd:.3f}"
            else:
                cost_str = "$?.???"
            suffix = f"{elapsed:.1f}s  {cost_str}{local_tag}"
            print(f"  {name_col} {prov_col} {model_col} \u2713  {suffix}")

    passed = sum(
        1
        for _, _, o in results
        if not isinstance(o, Exception)
        and o.success
        and o.structured_data is not None
        and "verdict" in o.structured_data
    )
    total = len(results)
    print(f"\n[check-providers] {passed}/{total} passed")
    return 1 if any_failed else 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Print a human-readable summary of an audit file."""
    audit_path = Path(args.file).resolve()
    if not audit_path.exists():
        print(f"Audit file not found: {audit_path}", file=sys.stderr)
        return 1

    with open(audit_path, encoding="utf-8") as f:
        audit = yaml.safe_load(f) or {}

    # Dispatch to ideation display for ideation audit records.
    if audit.get("type") == "ideate":
        return _cmd_audit_ideate(audit)

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
            dur_str = _fmt_duration(dur) if dur is not None else "—"
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


# ── Hooks scaffolding ─────────────────────────────────────────────────

_POST_RUN_SH = """\
#!/usr/bin/env bash
# Reference post_run hook: file GitHub Issues for P1/P2 findings
# Fires after every forge run; creates one issue per finding on ESCALATE or
# APPROVE-with-findings outcomes.
#
# Requires: gh CLI (authenticated), jq
set -euo pipefail

# Guard: gh not installed → warn and exit cleanly
if ! command -v gh &> /dev/null; then
  echo "[forge hook] gh CLI not found — skipping GitHub issue creation" >&2
  exit 0
fi

# Guard: jq not installed → warn and exit cleanly
if ! command -v jq &> /dev/null; then
  echo "[forge hook] jq not found — skipping GitHub issue creation" >&2
  exit 0
fi

payload=$(cat)

verdict=$(echo "$payload" | jq -r '.verdict')
slug=$(echo "$payload" | jq -r '.slug')
branch=$(echo "$payload" | jq -r '.branch')
summary=$(echo "$payload" | jq -r '.summary')
findings_count=$(echo "$payload" | jq '.findings | length')

# Only act on ESCALATE or APPROVE outcomes (REQUEST_CHANGES = still in review)
if [ "$verdict" != "ESCALATE" ] && [ "$verdict" != "APPROVE" ]; then
  exit 0
fi

# File one GitHub Issue per finding (only when findings exist)
if [ "$findings_count" -gt 0 ]; then
  echo "$payload" | jq -c '.findings[]' | while read -r finding; do
    sev=$(echo "$finding" | jq -r '.severity')
    file=$(echo "$finding" | jq -r '.file')
    line=$(echo "$finding" | jq -r '.line // empty')
    desc=$(echo "$finding" | jq -r '.description')
    suggestion=$(echo "$finding" | jq -r '.suggestion // empty')

    # Title: [P1] slug: description (truncated to 72 chars)
    raw_title="[${sev}] ${slug}: ${desc}"
    title="${raw_title:0:72}"

    location="\\`${file}\\`"
    [ -n "$line" ] && location="${location} line ${line}"

    body="**Story:** \\`${slug}\\` (\\`${branch}\\`)
**Verdict:** ${verdict} — ${summary}
**Location:** ${location}

**Description:** ${desc}"

    if [ -n "$suggestion" ]; then
      body="${body}

**Suggestion:** ${suggestion}"
    fi

    body="${body}

*Filed by theforge post_run hook.*"

    gh issue create \\
      --title "$title" \\
      --body "$body" \\
      --label "forge-finding" \\
      --label "$(echo "$sev" | tr 'A-Z' 'a-z')" || true
  done
fi

# ── PR Review Attribution ─────────────────────────────────────────────
# Opt-in: set FORGE_GH_PR_REVIEWS=1 to enable posting per-reviewer GitHub reviews.
# The gh api calls below use `repos/{owner}/{repo}` — gh CLI resolves these
# automatically to the current repository when run from within the repo.
# See: https://cli.github.com/manual/gh_api (the {owner}/{repo} tokens are
# expanded by gh from the remote origin URL automatically).
[ -z "${FORGE_GH_PR_REVIEWS:-}" ] && exit 0

# Only post reviews on APPROVE
[ "$verdict" != "APPROVE" ] && exit 0

pr_number=$(echo "$payload" | jq -r '.pr_number // "null"')
[ "$pr_number" = "null" ] && exit 0

reviewers_count=$(echo "$payload" | jq '(.reviewers // []) | length')

if [ "$reviewers_count" -le 1 ]; then
  # Single-reviewer (or absent): post one APPROVE with that reviewer's findings
  reviewer_body=$(echo "$payload" | jq -r '
    (.reviewers // []) | if length == 0 then
      "Approved by theforge review pool.\\n\\n*Posted by theforge post_run hook.*"
    else
      .[0] as $r |
      "**Reviewer:** \\($r.name) (`\\($r.model)`)\\n\\n**Summary:** \\($r.summary)\\n\\n" +
      (if ($r.findings | length) > 0 then
        "**Findings:**\\n" +
        ($r.findings | map(
          "- [`\\(.file):\\(.line // "?")`] [\\(.severity)] \\(.description)"
        ) | join("\\n")) + "\\n\\n"
      else "" end) +
      "*Posted by theforge post_run hook.*"
    end
  ')
  jq -n --arg body "$reviewer_body" --arg event "APPROVE" \\
    '{body: $body, event: $event}' | \\
    gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \\
      --method POST \\
      --input - || true
else
  # Multi-reviewer: post one COMMENT per reviewer, then one final APPROVE
  echo "$payload" | jq -c '.reviewers[]' | while read -r reviewer; do
    r_name=$(echo "$reviewer" | jq -r '.name')
    r_model=$(echo "$reviewer" | jq -r '.model')
    r_verdict=$(echo "$reviewer" | jq -r '.verdict')
    r_summary=$(echo "$reviewer" | jq -r '.summary')
    r_findings_count=$(echo "$reviewer" | jq '.findings | length')

    comment_body="**Reviewer:** ${r_name} (\\`${r_model}\\`)
**Verdict:** ${r_verdict}
**Summary:** ${r_summary}"

    if [ "$r_findings_count" -gt 0 ]; then
      findings_text=$(echo "$reviewer" | jq -r '
        .findings | map(
          "- [`\\(.file):\\(.line // "?")`] [\\(.severity)] \\(.description)"
        ) | join("\\n")
      ')
      comment_body="${comment_body}

**Findings:**
${findings_text}"
    fi

    comment_body="${comment_body}

*Posted by theforge post_run hook.*"

    jq -n --arg body "$comment_body" --arg event "COMMENT" \\
      '{body: $body, event: $event}' | \\
      gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \\
        --method POST \\
        --input - || true
  done

  # Final APPROVE with merged summary
  approve_body="**theforge review pool APPROVED** — ${summary}

*Posted by theforge post_run hook.*"

  jq -n --arg body "$approve_body" --arg event "APPROVE" \\
    '{body: $body, event: $event}' | \\
    gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \\
      --method POST \\
      --input - || true
fi
"""

_HOOKS_README = """\
# .forge/hooks

Lifecycle hook scripts for TheForge. Place executable scripts here and
reference them in `forge.yaml` under the `hooks:` key.

## post_run.sh — payload schema

The `post_run` hook receives a JSON object on stdin after every `forge run`:

```json
{
  "event": "post_run",
  "verdict": "APPROVE | REQUEST_CHANGES | ESCALATE",
  "slug": "my-feature",
  "branch": "feat/my-feature",
  "summary": "one-line review summary",
  "pr_number": 42,
  "findings": [
    {
      "severity": "P1 | P2",
      "file": "src/foo.py",
      "line": 42,
      "description": "what is wrong",
      "suggestion": "how to fix"
    }
  ],
  "reviewers": [
    {
      "name": "reviewer-profile-name",
      "model": "claude-sonnet-4-6",
      "verdict": "APPROVE | REQUEST_CHANGES",
      "summary": "one-line summary from this reviewer",
      "findings": [...]
    }
  ]
}
```

`pr_number` is an integer extracted from the merge PR URL, or `null` if the run
did not result in a PR merge. `reviewers` contains per-reviewer attribution from
the last review cycle; empty list if no reviewers ran.

## PR Review Attribution

Set `FORGE_GH_PR_REVIEWS=1` to enable automatic posting of GitHub PR reviews
after an APPROVE outcome. Requires `gh` CLI authenticated and `pr_number` to be
non-null.

## Hook contract

- **stdin**: JSON payload (schema above)
- **exit 0**: success — forge continues normally
- **exit non-zero**: hook failure — forge prints a warning but does not abort

## forge.yaml configuration

```yaml
hooks:
  post_run: .forge/hooks/post_run.sh
  # post_merge: .forge/hooks/post_merge.sh
  # post_sprint: .forge/hooks/post_sprint.sh
  # pre_run: .forge/hooks/pre_run.sh
  timeout_seconds: 30
```
"""


def cmd_init_hooks(args: argparse.Namespace) -> int:
    """Scaffold .forge/hooks/post_run.sh and .forge/hooks/README.md."""
    project_root = Path.cwd()
    hooks_dir = project_root / ".forge" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    sh_path = hooks_dir / "post_run.sh"
    readme_path = hooks_dir / "README.md"
    created: list[str] = []
    skipped: list[str] = []

    if sh_path.exists():
        skipped.append(str(sh_path))
    else:
        sh_path.write_text(_POST_RUN_SH, encoding="utf-8")
        sh_path.chmod(0o755)
        created.append(str(sh_path))

    if readme_path.exists():
        skipped.append(str(readme_path))
    else:
        readme_path.write_text(_HOOKS_README, encoding="utf-8")
        created.append(str(readme_path))

    for path in created:
        print(f"Created {path}")
    for path in skipped:
        print(f"Skipped (already exists): {path}", file=sys.stderr)

    print(
        "\nAdd a hooks: block to forge.yaml to activate:\n\n"
        "  hooks:\n"
        "    post_run: .forge/hooks/post_run.sh\n"
        "    timeout_seconds: 30\n"
    )
    return 0


# ── Telemetry ────────────────────────────────────────────────────────

_PHASE_NAMES = ["preflight", "plan", "plan_review", "dev", "validate", "review"]
_PHASE_LABELS = {
    "preflight": "PREFLIGHT",
    "plan": "PLAN",
    "plan_review": "PLAN_REVIEW",
    "dev": "DEV",
    "validate": "VALIDATE",
    "review": "REVIEW",
}


def cmd_telemetry(args: argparse.Namespace) -> int:
    """Print per-phase cost/duration telemetry from history.jsonl."""
    # Locate project root
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print("Error: forge.yaml not found. Run from a forge project root.", file=sys.stderr)
        return 1
    project_root = config_path.parent
    history_path = project_root / ".forge" / "audits" / "history.jsonl"
    if not history_path.exists():
        print(f"No history found at {history_path}", file=sys.stderr)
        return 1

    # Parse --since date filter
    since_dt: datetime.datetime | None = None
    if args.since:
        try:
            since_dt = datetime.datetime.fromisoformat(args.since).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            print(f"Invalid --since date: {args.since!r} (expected YYYY-MM-DD)", file=sys.stderr)
            return 1

    phase_filter: str | None = getattr(args, "phase", None)

    # Read records
    records: list[dict] = []
    try:
        with open(history_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if since_dt is not None:
                    started = (record.get("timing") or {}).get("started_at")
                    if started:
                        try:
                            ts = datetime.datetime.fromisoformat(started)
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=datetime.timezone.utc)
                            if ts < since_dt:
                                continue
                        except ValueError:
                            pass
                records.append(record)
    except OSError as e:
        print(f"Error reading history: {e}", file=sys.stderr)
        return 1

    if not records:
        print("No records found (try relaxing --since filter).")
        return 0

    # Last 10 by default
    recent = records[-10:]

    # ── Run table ─────────────────────────────────────────────────────
    _fmt_dur_s = lambda s: (  # noqa: E731
        f"{int(s // 60)}m{int(s % 60):02d}s" if s is not None else "—"
    )
    _fmt_cost_s = lambda c: f"${c:.2f}" if c is not None else "—"  # noqa: E731

    if not phase_filter:
        hdr = f"{'Run':<30} {'Cost':>8}  {'Time':>8}  {'Dev':>4}  {'Rev':>4}  {'Outcome'}"
        print(hdr)
        print("-" * len(hdr))
        for rec in recent:
            slug = (rec.get("task") or {}).get("slug", "?")
            slug_short = slug[:29]
            outcome = (rec.get("outcome") or {}).get("final_phase", "?").lower()
            totals = rec.get("totals") or {}
            cost = totals.get("cost_usd") or (rec.get("cost") or {}).get("total_usd")
            dur = totals.get("duration_s") or (rec.get("timing") or {}).get("duration_seconds")
            dev_iters = totals.get("dev_iterations") or (rec.get("iterations") or {}).get(
                "dev_iterations", "?"
            )
            rev_cycles = totals.get("review_cycles") or (rec.get("iterations") or {}).get(
                "review_cycles", "?"
            )
            print(
                f"{slug_short:<30} {_fmt_cost_s(cost):>8}  {_fmt_dur_s(dur):>8}  "
                f"{str(dev_iters):>4}  {str(rev_cycles):>4}  {outcome}"
            )
        print()

    # ── Phase breakdown ───────────────────────────────────────────────
    # Collect per-phase cost and duration across records that have phases block
    phase_costs: dict[str, list[float]] = {p: [] for p in _PHASE_NAMES}
    phase_durations: dict[str, list[float]] = {p: [] for p in _PHASE_NAMES}
    total_cost_all = 0.0
    records_with_phases = 0

    for rec in records:
        phases = rec.get("phases")
        if not phases:
            continue
        records_with_phases += 1
        rec_total = (rec.get("totals") or {}).get("cost_usd") or 0.0
        total_cost_all += rec_total
        for p in _PHASE_NAMES:
            pdata = phases.get(p)
            if not pdata:
                continue
            c = pdata.get("cost_usd")
            d = pdata.get("duration_s")
            if c is not None:
                phase_costs[p].append(c)
            if d is not None:
                phase_durations[p].append(d)

    phases_to_show = [phase_filter] if phase_filter else _PHASE_NAMES

    if records_with_phases == 0:
        print("No phase telemetry data found (runs predating this feature have no phases block).")
        return 0

    print(f"Phase breakdown (last {records_with_phases} run(s) with phase data):")
    avg_total = total_cost_all / records_with_phases if records_with_phases else 0.0
    for p in phases_to_show:
        if p not in _PHASE_NAMES:
            print(f"  Unknown phase: {p!r}", file=sys.stderr)
            continue
        label = _PHASE_LABELS[p]
        costs = phase_costs[p]
        durs = phase_durations[p]
        avg_cost = sum(costs) / len(costs) if costs else 0.0
        avg_dur = sum(durs) / len(durs) if durs else None
        pct = (avg_cost / avg_total * 100) if avg_total > 0 else 0.0
        pct_str = f"  ← {pct:.0f}% of cost" if pct >= 15 else ""
        dur_str = f"avg {_fmt_dur_s(avg_dur):>6}" if avg_dur is not None else "avg      —"
        print(f"  {label:<12} avg ${avg_cost:.2f}   {dur_str}{pct_str}")

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

    # forge secrets-init
    subparsers.add_parser(
        "secrets-init",
        help="Create .forge/secrets.yaml skeleton and update .gitignore",
    )

    # forge init-hooks
    subparsers.add_parser(
        "init-hooks",
        help="Scaffold .forge/hooks/post_run.sh reference script and README",
    )

    # forge run
    run_parser = subparsers.add_parser("run", help="Run dev→review loop for a story")
    run_parser.add_argument("story", help="Path to the story file")
    run_parser.add_argument("--slug", help="Workspace slug (default: story filename stem)")
    run_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
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

    # forge review
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

    # forge sprint
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

    # forge ideate
    ideate_parser = subparsers.add_parser(
        "ideate", help="Run multi-LLM deliberation to generate a story from a brief"
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
        help="Output path for generated story (default: stories/<slug>.md)",
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
        help=(
            "Run deliberation and print synthesized spec to stdout without writing a spec file. "
            "LLM agents are still invoked; only the output spec file is skipped. "
            "An audit record is always written."
        ),
    )
    ideate_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )

    # forge check-providers
    check_providers_parser = subparsers.add_parser(
        "check-providers",
        help="Smoke-test all API-mode profiles in forge.yaml",
    )
    check_providers_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Test only the named profile (default: all API profiles)",
    )
    check_providers_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )

    # forge audit
    audit_parser = subparsers.add_parser("audit", help="Print audit log summary")
    audit_parser.add_argument(
        "file", help="Path to audit file (e.g. .forge/audits/forge_audit.yaml)"
    )

    # forge telemetry
    telemetry_parser = subparsers.add_parser(
        "telemetry", help="Show per-phase cost/duration telemetry across runs"
    )
    telemetry_parser.add_argument(
        "--since",
        metavar="DATE",
        help="Only include runs on or after this date (YYYY-MM-DD)",
    )
    telemetry_parser.add_argument(
        "--phase",
        metavar="PHASE",
        help="Show breakdown for a single phase (preflight|plan|plan_review|dev|validate|review)",
    )
    telemetry_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )

    args = parser.parse_args()

    commands = {
        "init": cmd_init,
        "secrets-init": cmd_secrets_init,
        "init-hooks": cmd_init_hooks,
        "run": cmd_run,
        "review": cmd_review,
        "sprint": cmd_sprint,
        "ideate": cmd_ideate,
        "check-providers": cmd_check_providers,
        "audit": cmd_audit,
        "telemetry": cmd_telemetry,
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
