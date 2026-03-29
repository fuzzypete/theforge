"""Shared helpers for the forge CLI."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from theforge.artifacts import (
    AUDIT_PATH,
    ensure_parent_dir,
)
from theforge.config import (
    ForgeConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import CoordinatorResult
from theforge.task import TaskStory, build_dev_prompt, build_review_prompt, load_story

_SECRETS_FILE = ".forge/.env"


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


def _write_audit(result: CoordinatorResult, config: ForgeConfig, task: TaskStory) -> Path:
    """Write the audit log to .forge/audits/forge_audit.yaml and .forge/audit.yaml."""
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
        worktree_audit_path = result.state.workspace_path / AUDIT_PATH
        ensure_parent_dir(worktree_audit_path)
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


def _apply_dev_model_override(config: "ForgeConfig", spec: str) -> "ForgeConfig":
    """Override the dev profile with a --dev-model spec.

    Format: provider/model@base_url
    Examples:
        ollama/qwen2.5-coder:14b@http://localhost:11434/v1
        openai/qwen2.5-coder:7b@http://localhost:11434/v1
        anthropic/claude-opus-4-6

    The "ollama" provider alias is normalised to "openai" because Ollama exposes
    an OpenAI-compatible API.  Pass the Ollama base URL via the @url suffix.
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

    # Ollama exposes an OpenAI-compatible API — normalise so it routes through
    # the existing OpenAI runner (which already passes base_url to the client).
    if provider == "ollama":
        provider = "openai"

    new_dev = replace(
        config.dev_profile,
        cli=None,
        provider=provider,
        model=model,
        base_url=base_url,
        budget_usd=config.dev_profile.budget_usd,
    )
    return replace(config, dev_profile=new_dev)


def _apply_plan_model_override(config: "ForgeConfig", spec: str) -> "ForgeConfig":
    """Override the plan profile with a --plan-model spec.

    Format: provider/model  (sets API transport, clears CLI)
            bare-model-name (updates model identifier only, preserves transport)
    Examples:
        opus
        anthropic/claude-opus-4-6
    """
    from dataclasses import replace

    if "/" in spec:
        provider, model = spec.split("/", 1)
        new_plan = replace(config.plan, provider=provider, model=model, cli=None)
    else:
        new_plan = replace(config.plan, model=spec)

    return replace(config, plan=new_plan, plan_model_is_default=False)
