"""Shared helpers for the forge CLI."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from theforge.artifacts import (
    AUDIT_PATH,
    ESCALATED_MARKER_PATH,
    ensure_parent_dir,
)
from theforge.config import (
    ForgeConfig,
    _validate_plan_provider,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.redact import redact
from theforge.coordinator.review_context import hard_convention_review_kwargs
from theforge.coordinator.state import CoordinatorResult
from theforge.task import (
    TaskStory,
    build_dev_prompt,
    build_review_prompt,
    frontmatter_allows_forge_yaml_mutation,
    load_story,
    parse_story_frontmatter,
)

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
    """Backward-compatible wrapper around the shared story frontmatter parser."""
    return parse_story_frontmatter(story_path)


def _build_task(story_path: Path, slug: str | None = None) -> TaskStory:
    """Build a TaskStory from a story file, using frontmatter if available."""
    fm = _parse_story_frontmatter(story_path)

    # Slug: CLI arg > frontmatter > filename stem
    resolved_slug = slug or fm.get("slug") or story_path.stem

    raw_issue = fm.get("github_issue")
    try:
        github_issue = int(raw_issue) if raw_issue is not None else None
    except (ValueError, TypeError):
        github_issue = None
    return TaskStory(
        name=fm.get("name", story_path.stem.replace("_", " ").replace("-", " ").title()),
        story_path=story_path.resolve(),
        slug=resolved_slug,
        test_target=fm.get("test_target"),
        gate_override=fm.get("gate"),
        github_issue=github_issue,
        allow_mutate_forge_yaml=frontmatter_allows_forge_yaml_mutation(fm),
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
    """Write the canonical audit log and preserve minimal worktree state on ESCALATE."""
    audit = generate_audit_log(config, task, result)
    audits_dir = config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "forge_audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Append to history log (JSONL, never overwritten).
    _append_history(audits_dir, audit)
    final_phase = result.phase.name
    if (
        final_phase == "ESCALATE"
        and result.state.workspace_path
        and result.state.workspace_path.exists()
    ):
        worktree_audit_path = result.state.workspace_path / AUDIT_PATH
        ensure_parent_dir(worktree_audit_path)
        with open(worktree_audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
        marker_path = result.state.workspace_path / ESCALATED_MARKER_PATH
        ensure_parent_dir(marker_path)
        timestamp = audit.get("ended_at") or audit.get("started_at") or ""
        marker_path.write_text(
            f"slug: {task.slug}\nfinal_phase: {final_phase}\ntimestamp: {timestamp}\n",
            encoding="utf-8",
        )
    # Copy to durable per-story log dir (survives worktree cleanup)
    if result.state.log_dir is not None:
        try:
            log_audit_path = result.state.log_dir / "audit.yaml"
            log_audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_audit_path, "w", encoding="utf-8") as f:
                yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass  # best-effort
    # Write per-run JSON record (Phase A dual-write).
    _write_per_run_record(result, config, audit, audits_dir)
    return audit_path


def _write_per_run_record(
    result: CoordinatorResult,
    config: ForgeConfig,
    audit: dict,
    audits_dir: Path,
) -> None:
    """Write a per-run JSON record to .forge/audits/runs/{run_id}.json.

    The record is written exactly once at run termination, carries schema_version,
    run_id, and parent_run_id (null for Phase A — resume lineage is not yet tracked),
    and is scrubbed by a best-effort redaction pass before hitting disk.

    Missing run_id (e.g. very old coordinator path) silently skips the write so
    existing behaviour is unchanged.
    """
    run_id = result.state.run_id
    if not run_id:
        return

    try:
        runs_dir = audits_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_file = runs_dir / f"{run_id}.json"
        # Don't overwrite an already-written record (immutability contract).
        if run_file.exists():
            return

        record: dict = {
            "schema_version": 1,
            "run_id": run_id,
            "parent_run_id": None,
            "forge_version": audit.get("forge_version"),
        }
        record.update(audit)
        # Ensure the envelope fields stay at the top (dict insertion order is preserved).
        # Re-insert them so they shadow any same-named keys from audit.
        record["schema_version"] = 1
        record["run_id"] = run_id
        record["parent_run_id"] = None
        record["forge_version"] = audit.get("forge_version")

        env_file = config.project_root / _SECRETS_FILE
        redacted = redact(record, env_file if env_file.exists() else None)

        with open(run_file, "w", encoding="utf-8") as f:
            json.dump(redacted, f, default=str, indent=2)
    except Exception:
        pass  # best-effort — never block a run on audit write failure
        return

    # Mirror the per-run record into the SQLite audit substrate. The
    # per-run JSON is canonical; substrate write failure is a logged
    # warning, not a hard fail — `forge audits rebuild` recovers.
    try:
        from theforge.coordinator import audit_substrate

        conn = audit_substrate.create_or_open(config.project_root)
        try:
            stat = run_file.stat()
            audit_substrate.upsert_run_record(
                conn,
                redacted,
                provenance="native",
                source_path=str(run_file.relative_to(config.project_root)),
                source_mtime=stat.st_mtime,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        import sys as _sys

        print(
            f"[forge] warning: failed to update audit substrate: {exc}",
            file=_sys.stderr,
        )


def _cmd_dry_run(config: ForgeConfig, task: TaskStory, story_path: Path) -> int:
    """Print what would happen without invoking any agents."""
    story_content = load_story(story_path)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    branch_name = config.workspace.branch_pattern.format(slug=task.slug)

    dev_prompt = build_dev_prompt(
        task,
        workspace_path=workspace_path,
        branch_name=branch_name,
        allowed_tools=config.dev_profile.allowed_tools,
        story_content=story_content,
        gate_command=config.validation.gate_command,
        conventions=config.conventions_soft,
    )
    review_prompt = build_review_prompt(
        task,
        story_content=story_content,
        commit_log="(dry run — no commits available)",
        workspace_path=str(workspace_path),
        branch=branch_name,
        handoff_content="(dry run — no handoff available)",
        conventions=config.conventions_soft,
        **hard_convention_review_kwargs(config),
    )

    sep = "=" * 60
    print(f"{sep}")
    print("DRY RUN — no agents will be invoked")
    print(f"{sep}\n")

    workspace_command = config.workspace.create_command.format(
        slug=task.slug,
        base_branch=config.workspace.base_branch,
    )
    print(f"Workspace command: {workspace_command}")
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
        # Clear transport so ModelProfile.__post_init__ re-infers it from the
        # new cli/provider pair. Without this, the prior transport (e.g.
        # claude CLI) would persist and dispatch would still go CLI.
        transport=None,
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
        if new_plan.enabled:
            _validate_plan_provider(new_plan, config.secrets)
    else:
        new_plan = replace(config.plan, model=spec)

    return replace(config, plan=new_plan, plan_model_is_default=False)
