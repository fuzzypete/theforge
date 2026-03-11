"""Orchestrator configuration: forge.yaml loader and typed dataclasses.

TheForge is project-agnostic. All project-specific details (workspace commands,
validation commands, model selection) live in forge.yaml in the consuming project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelProfile:
    """Model configuration for a specific agent role (dev or review)."""

    name: str  # "dev", "review"
    cli: str  # "claude" (future: "codex", "gemini")
    model: str  # "sonnet", "opus", "claude-sonnet-4-6"
    budget_usd: float  # per-invocation cost ceiling
    timeout_seconds: int  # subprocess timeout
    allowed_tools: tuple[str, ...]  # tools the agent may use


@dataclass(frozen=True)
class WorkspaceConfig:
    """How to create isolated workspaces for agents."""

    create_command: str  # shell command template, {slug} is replaced
    path_pattern: str  # path template, {slug} is replaced
    branch_pattern: str  # branch name template, {slug} is replaced


@dataclass(frozen=True)
class ValidationConfig:
    """How to validate agent output."""

    gate_command: str  # e.g. "make gate"
    handoff_file: str  # e.g. "handoff.yaml"
    gate_decision_key: str  # YAML key to read for pass/fail


@dataclass(frozen=True)
class RetryPolicy:
    """Retry limits before escalating to human."""

    max_dev_iterations: int = 3  # retries within a single review cycle
    max_review_cycles: int = 2  # full dev->review loops


@dataclass(frozen=True)
class ForgeConfig:
    """Top-level orchestrator configuration loaded from forge.yaml."""

    project: str
    project_root: Path
    workspace: WorkspaceConfig
    validation: ValidationConfig
    dev_profile: ModelProfile
    review_profile: ModelProfile
    retry: RetryPolicy


# ── Defaults ──────────────────────────────────────────────────────────


DEFAULT_DEV_PROFILE = ModelProfile(
    name="dev",
    cli="claude",
    model="sonnet",
    budget_usd=2.00,
    timeout_seconds=900,
    allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
)

DEFAULT_REVIEW_PROFILE = ModelProfile(
    name="review",
    cli="claude",
    model="opus",
    budget_usd=1.00,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash", "Glob", "Grep"),
)

DEFAULT_WORKSPACE = WorkspaceConfig(
    create_command="git worktree add .forge/worktrees/{slug} -b forge/{slug} main",
    path_pattern=".forge/worktrees/{slug}",
    branch_pattern="forge/{slug}",
)

DEFAULT_VALIDATION = ValidationConfig(
    gate_command="make gate",
    handoff_file="handoff.yaml",
    gate_decision_key="gate_decision",
)


# ── Loader ────────────────────────────────────────────────────────────


def _parse_profile(name: str, data: dict[str, Any]) -> ModelProfile:
    """Parse a model profile from forge.yaml data."""
    tools = data.get("allowed_tools", [])
    return ModelProfile(
        name=name,
        cli=data.get("cli", "claude"),
        model=data.get("model", "sonnet" if name == "dev" else "opus"),
        budget_usd=float(data.get("budget_usd", 2.0 if name == "dev" else 1.0)),
        timeout_seconds=int(data.get("timeout_seconds", 900 if name == "dev" else 300)),
        allowed_tools=tuple(tools) if tools else DEFAULT_DEV_PROFILE.allowed_tools,
    )


def load_config(config_path: Path) -> ForgeConfig:
    """Load forge.yaml and return a typed ForgeConfig.

    The config file path is used to derive the project root (its parent directory).
    Missing sections fall back to sensible defaults.
    """
    project_root = config_path.parent.resolve()

    with open(config_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    # Workspace
    ws_data = raw.get("workspace", {})
    workspace = WorkspaceConfig(
        create_command=ws_data.get("create_command", DEFAULT_WORKSPACE.create_command),
        path_pattern=ws_data.get("path_pattern", DEFAULT_WORKSPACE.path_pattern),
        branch_pattern=ws_data.get("branch_pattern", DEFAULT_WORKSPACE.branch_pattern),
    )

    # Validation
    val_data = raw.get("validation", {})
    validation = ValidationConfig(
        gate_command=val_data.get("gate_command", DEFAULT_VALIDATION.gate_command),
        handoff_file=val_data.get("handoff_file", DEFAULT_VALIDATION.handoff_file),
        gate_decision_key=val_data.get(
            "gate_decision_key", DEFAULT_VALIDATION.gate_decision_key
        ),
    )

    # Profiles
    profiles = raw.get("profiles", {})
    dev_profile = (
        _parse_profile("dev", profiles["dev"])
        if "dev" in profiles
        else DEFAULT_DEV_PROFILE
    )
    review_profile = (
        _parse_profile("review", profiles["review"])
        if "review" in profiles
        else DEFAULT_REVIEW_PROFILE
    )

    # Retry
    retry_data = raw.get("retry", {})
    retry = RetryPolicy(
        max_dev_iterations=int(retry_data.get("max_dev_iterations", 3)),
        max_review_cycles=int(retry_data.get("max_review_cycles", 2)),
    )

    return ForgeConfig(
        project=raw.get("project", project_root.name),
        project_root=project_root,
        workspace=workspace,
        validation=validation,
        dev_profile=dev_profile,
        review_profile=review_profile,
        retry=retry,
    )


def generate_default_config() -> str:
    """Generate a starter forge.yaml content string."""
    return """\
# forge.yaml — TheForge project configuration
# See https://github.com/your-org/theforge for documentation.

project: my-project

workspace:
  # Shell command to create an isolated workspace. {slug} is replaced.
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} main"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  # Command to run all validation checks and produce a gate artifact.
  gate_command: "make gate"
  handoff_file: "handoff.yaml"
  gate_decision_key: "gate_decision"  # YAML key in handoff_file: PASS | FAIL | BLOCKED

profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 2.00
    timeout_seconds: 900
    allowed_tools: ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
  review:
    cli: claude
    model: opus
    budget_usd: 1.00
    timeout_seconds: 300
    allowed_tools: ["Read", "Bash", "Glob", "Grep"]

retry:
  max_dev_iterations: 3    # retries within a review cycle
  max_review_cycles: 2     # full dev→review loops before escalation
"""
