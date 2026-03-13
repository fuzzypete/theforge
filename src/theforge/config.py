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

    name: str  # "dev", "review", or pool entry name like "opus-reviewer"
    cli: str  # "claude", "codex", or "gemini"
    model: str  # "sonnet", "opus", "claude-sonnet-4-6"
    budget_usd: float  # cumulative cost ceiling across all invocations
    timeout_seconds: int  # subprocess timeout
    allowed_tools: tuple[str, ...]  # tools the agent may use
    reasoning_effort: str | None = None  # "low" | "medium" | "high"; Codex only


@dataclass(frozen=True)
class WorkspaceConfig:
    """How to create isolated workspaces for agents."""

    create_command: str  # shell command template, {slug} is replaced
    path_pattern: str  # path template, {slug} is replaced
    branch_pattern: str  # branch name template, {slug} is replaced
    base_branch: str = "main"  # base branch for diff comparison
    stale_worktree_days: int = 1  # remove worktrees older than N days; 0 = always remove


@dataclass(frozen=True)
class ValidationConfig:
    """How to validate agent output.

    Two gate modes:
    1. Handoff-based (default for theforge): gate_command writes a handoff file
       with a gate_decision_key. Set handoff_file and gate_decision_key.
    2. Exit-code-based: gate passes if the command exits 0, fails otherwise.
       Set handoff_file to "" (empty) to use this mode.
    """

    gate_command: str  # e.g. "make gate" or "make fmt && pytest"
    handoff_file: str  # e.g. "handoff.yaml", or "" for exit-code mode
    gate_decision_key: str  # YAML key to read for pass/fail
    gate_timeout: int | None = None  # seconds; None = default 600


@dataclass(frozen=True)
class RetryPolicy:
    """Retry limits before escalating to human."""

    max_dev_iterations: int = 3  # retries within a single review cycle
    max_review_cycles: int = 2  # full dev->review loops
    max_review_parse_retries: int = 2  # reviewer retries on parse/schema error per cycle


@dataclass(frozen=True)
class ForgeConfig:
    """Top-level orchestrator configuration loaded from forge.yaml."""

    project: str
    project_root: Path
    workspace: WorkspaceConfig
    validation: ValidationConfig
    dev_profile: ModelProfile
    preflight_profile: ModelProfile  # read-only gap analysis; defaults to sonnet
    review_pool: list[ModelProfile]  # all reviewers; at least 1
    synthesis_profile: ModelProfile | None  # None when pool size <= 1
    retry: RetryPolicy

    @property
    def review_profile(self) -> ModelProfile:
        """Backward-compat: returns review_pool[0]."""
        return self.review_pool[0]


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

DEFAULT_PREFLIGHT_PROFILE = ModelProfile(
    name="preflight",
    cli="claude",
    model="sonnet",
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

# CLIs supported by the runner. Unsupported CLIs are rejected at config load.
SUPPORTED_CLIS: frozenset[str] = frozenset({"claude", "codex", "gemini"})


# ── Loader ────────────────────────────────────────────────────────────


def _parse_profile(name: str, data: dict[str, Any], *, role: str = "review") -> ModelProfile:
    """Parse a model profile from forge.yaml data.

    role controls which defaults to apply: "dev" uses DEFAULT_DEV_PROFILE,
    anything else uses DEFAULT_REVIEW_PROFILE. This prevents pool entries
    named "dev" from accidentally inheriting dev-level tools/timeouts.
    """
    default = DEFAULT_DEV_PROFILE if role == "dev" else DEFAULT_REVIEW_PROFILE
    tools = data.get("allowed_tools")
    reasoning_effort = data.get("reasoning_effort")
    _VALID_REASONING_EFFORTS = {"low", "medium", "high"}
    if reasoning_effort is not None and reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORTS)}, "
            f"got {reasoning_effort!r} in profile {name!r}"
        )
    return ModelProfile(
        name=name,
        cli=data.get("cli", default.cli),
        model=data.get("model", default.model),
        budget_usd=float(data.get("budget_usd", default.budget_usd)),
        timeout_seconds=int(data.get("timeout_seconds", default.timeout_seconds)),
        allowed_tools=tuple(tools) if tools is not None else default.allowed_tools,
        reasoning_effort=reasoning_effort,
    )


def load_config(config_path: Path) -> ForgeConfig:
    """Load forge.yaml and return a typed ForgeConfig.

    The config file path is used to derive the project root (its parent directory).
    Missing sections fall back to sensible defaults.

    Raises ValueError for invalid configurations (empty pool, duplicate names,
    unsupported CLI, missing synthesis profile when pool size > 1).
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
        base_branch=ws_data.get("base_branch", DEFAULT_WORKSPACE.base_branch),
        stale_worktree_days=ws_data.get(
            "stale_worktree_days", DEFAULT_WORKSPACE.stale_worktree_days
        ),
    )

    # Validation
    val_data = raw.get("validation", {})
    validation = ValidationConfig(
        gate_command=val_data.get("gate_command", DEFAULT_VALIDATION.gate_command),
        handoff_file=val_data.get("handoff_file", DEFAULT_VALIDATION.handoff_file),
        gate_decision_key=val_data.get("gate_decision_key", DEFAULT_VALIDATION.gate_decision_key),
        gate_timeout=val_data.get("gate_timeout"),
    )

    # Profiles
    profiles = raw.get("profiles", {})
    dev_profile = (
        _parse_profile("dev", profiles["dev"], role="dev")
        if "dev" in profiles
        else DEFAULT_DEV_PROFILE
    )
    preflight_profile = (
        _parse_profile("preflight", profiles["preflight"], role="review")
        if "preflight" in profiles
        else DEFAULT_PREFLIGHT_PROFILE
    )

    # review_pool precedence: review_pool > review > default
    if "review_pool" in profiles:
        pool_data = profiles["review_pool"]
        if not isinstance(pool_data, list) or len(pool_data) == 0:
            raise ValueError("profiles.review_pool must be a non-empty list")
        names = [e.get("name") for e in pool_data]
        if any(n is None for n in names):
            raise ValueError("Each profiles.review_pool entry must have a 'name' field")
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate names in profiles.review_pool: {names}")
        for entry in pool_data:
            cli = entry.get("cli", DEFAULT_REVIEW_PROFILE.cli)
            if cli not in SUPPORTED_CLIS:
                raise ValueError(
                    f"Unsupported CLI {cli!r} in review_pool entry {entry['name']!r}. "
                    f"Supported: {sorted(SUPPORTED_CLIS)}"
                )
        review_pool = [_parse_profile(e["name"], e, role="review") for e in pool_data]
        if len(review_pool) > 1:
            if "synthesis" not in profiles:
                raise ValueError(
                    "profiles.synthesis is required when review_pool has more than 1 entry"
                )
            synth_data = profiles["synthesis"]
            synth_cli = synth_data.get("cli", DEFAULT_REVIEW_PROFILE.cli)
            if synth_cli not in SUPPORTED_CLIS:
                raise ValueError(
                    f"Unsupported CLI {synth_cli!r} in profiles.synthesis. "
                    f"Supported: {sorted(SUPPORTED_CLIS)}"
                )
            synthesis_profile: ModelProfile | None = _parse_profile(
                "synthesis", synth_data, role="review"
            )
        else:
            synthesis_profile = None

    elif "review" in profiles:
        # Backward compat: single review dict wrapped into a pool of one.
        # CLI validation applies here too (P1 fix).
        review_data = profiles["review"]
        cli = review_data.get("cli", DEFAULT_REVIEW_PROFILE.cli)
        if cli not in SUPPORTED_CLIS:
            raise ValueError(
                f"Unsupported CLI {cli!r} in profiles.review. Supported: {sorted(SUPPORTED_CLIS)}"
            )
        review_pool = [_parse_profile("review", review_data, role="review")]
        synthesis_profile = None

    else:
        review_pool = [DEFAULT_REVIEW_PROFILE]
        synthesis_profile = None

    # Retry
    retry_data = raw.get("retry", {})
    retry = RetryPolicy(
        max_dev_iterations=int(retry_data.get("max_dev_iterations", 3)),
        max_review_cycles=int(retry_data.get("max_review_cycles", 2)),
        max_review_parse_retries=int(retry_data.get("max_review_parse_retries", 2)),
    )

    return ForgeConfig(
        project=raw.get("project", project_root.name),
        project_root=project_root,
        workspace=workspace,
        validation=validation,
        dev_profile=dev_profile,
        preflight_profile=preflight_profile,
        review_pool=review_pool,
        synthesis_profile=synthesis_profile,
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

# Multi-CLI review pool example:
# review_pool:
#   - name: claude-reviewer
#     cli: claude
#     model: opus
#     budget_usd: 1.00
#   - name: codex-reviewer
#     cli: codex
#     model: o4-mini
#     budget_usd: 1.00
#   - name: gemini-reviewer
#     cli: gemini
#     model: gemini-2.5-pro
#     budget_usd: 1.00
# synthesis:
#   cli: claude
#   model: opus
#   budget_usd: 1.00
"""
