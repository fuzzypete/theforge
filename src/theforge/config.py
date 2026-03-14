"""Orchestrator configuration: forge.yaml loader and typed dataclasses.

TheForge is project-agnostic. All project-specific details (workspace commands,
validation commands, model selection) live in forge.yaml in the consuming project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# ── Model registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelInfo:
    """Built-in metadata for a known model."""

    cli: str  # "claude", "codex", "gemini"
    model: str  # model identifier for the CLI
    tier: str  # "fast" or "strong"
    capability: int  # relative capability score (1-10)
    cost_rank: int  # 1=cheap, 2=moderate, 3=expensive


MODEL_REGISTRY: dict[str, ModelInfo] = {
    "claude/sonnet": ModelInfo(
        cli="claude", model="sonnet", tier="fast", capability=7, cost_rank=1
    ),
    "claude/opus": ModelInfo(
        cli="claude", model="opus", tier="strong", capability=10, cost_rank=3
    ),
    "openai/gpt-5.4": ModelInfo(
        cli="codex", model="gpt-5.4", tier="strong", capability=9, cost_rank=2
    ),
    "google/gemini-2.5-pro": ModelInfo(
        cli="gemini", model="gemini-2.5-pro", tier="strong", capability=8, cost_rank=2
    ),
}

# Maps provider prefix → CLI name
_PROVIDER_CLI_MAP: dict[str, str] = {
    "claude": "claude",
    "openai": "codex",
    "google": "gemini",
}


@dataclass(frozen=True)
class NtfyConfig:
    """Configuration for ntfy.sh push notifications."""

    url: str  # e.g. "https://ntfy.sh/my-topic"
    priority: str = "high"  # ntfy priority: min, low, default, high, urgent


@dataclass(frozen=True)
class EmailConfig:
    """Stub for future email notification support (not yet implemented)."""

    pass


@dataclass(frozen=True)
class NotificationConfig:
    """Notification backend configuration."""

    backend: str = "none"  # "none", "ntfy", "osascript"
    ntfy: NtfyConfig | None = None
    email: EmailConfig | None = None  # reserved for future use
    script: str | None = None  # path to custom notification script
    human_review_timeout_seconds: int = 14400  # 4 hours


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
    notifications: NotificationConfig = NotificationConfig()
    smart_config_models: list[str] | None = None  # None = classic config; list = smart config

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


# ── Smart config helpers ───────────────────────────────────────────────


def _resolve_model_info(model_key: str) -> ModelInfo:
    """Resolve a model key to ModelInfo; unknown models get sensible defaults."""
    if model_key in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_key]
    parts = model_key.split("/", 1)
    cli = _PROVIDER_CLI_MAP.get(parts[0], parts[0]) if len(parts) == 2 else model_key
    model = parts[1] if len(parts) == 2 else model_key
    return ModelInfo(cli=cli, model=model, tier="strong", capability=5, cost_rank=2)


def _apply_profile_overrides(base: ModelProfile, data: dict[str, Any]) -> ModelProfile:
    """Apply partial forge.yaml profile overrides on top of an auto-assigned profile."""
    tools = data.get("allowed_tools")
    reasoning_effort = data.get("reasoning_effort", base.reasoning_effort)
    _VALID_REASONING_EFFORTS = {"low", "medium", "high"}
    if reasoning_effort is not None and reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORTS)}, "
            f"got {reasoning_effort!r} in profile {base.name!r}"
        )
    return ModelProfile(
        name=base.name,
        cli=data.get("cli", base.cli),
        model=data.get("model", base.model),
        budget_usd=float(data.get("budget_usd", base.budget_usd)),
        timeout_seconds=int(data.get("timeout_seconds", base.timeout_seconds)),
        allowed_tools=tuple(tools) if tools is not None else base.allowed_tools,
        reasoning_effort=reasoning_effort,
    )


def _auto_assign_models(
    models: list[str],
    budget_usd: float,
) -> tuple[ModelProfile, ModelProfile, list[ModelProfile], ModelProfile | None]:
    """Auto-assign models to stages from a declarative pool.

    Assignment algorithm:
    1. Sort by cost_rank asc, capability desc
    2. dev = cheapest capable model
    3. preflight = cheapest "fast" tier, else same as dev
    4. review_pool = all models except dev (if only 1, pool = [dev])
    5. synthesis = highest-capability model from review_pool (skip if pool <= 1)

    Budget distribution:
    - dev: 60% of total
    - preflight: max(2%, $1)
    - synthesis: max(2%, $1) when pool > 1
    - each reviewer: remaining / pool_size
    """
    infos = [(m, _resolve_model_info(m)) for m in models]
    sorted_models = sorted(infos, key=lambda x: (x[1].cost_rank, -x[1].capability))

    dev_key, dev_info = sorted_models[0]

    fast = [(k, i) for k, i in sorted_models if i.tier == "fast"]
    preflight_key, preflight_info = fast[0] if fast else sorted_models[0]

    review_pairs = [(k, i) for k, i in sorted_models if k != dev_key]
    if not review_pairs:
        review_pairs = [(dev_key, dev_info)]

    has_synthesis = len(review_pairs) > 1

    preflight_budget = max(budget_usd * 0.02, 1.0)
    dev_budget = budget_usd * 0.60
    synthesis_budget = max(budget_usd * 0.02, 1.0) if has_synthesis else 0.0
    remaining = max(budget_usd - dev_budget - preflight_budget - synthesis_budget, 0.0)
    reviewer_budget = remaining / len(review_pairs)

    dev_profile = ModelProfile(
        name="dev",
        cli=dev_info.cli,
        model=dev_info.model,
        budget_usd=dev_budget,
        timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
    )
    preflight_profile = ModelProfile(
        name="preflight",
        cli=preflight_info.cli,
        model=preflight_info.model,
        budget_usd=preflight_budget,
        timeout_seconds=DEFAULT_PREFLIGHT_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
    )
    review_pool = [
        ModelProfile(
            name=k.replace("/", "-"),
            cli=i.cli,
            model=i.model,
            budget_usd=reviewer_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )
        for k, i in review_pairs
    ]

    synthesis_profile: ModelProfile | None = None
    if has_synthesis:
        synth_key, synth_info = max(review_pairs, key=lambda x: x[1].capability)
        synthesis_profile = ModelProfile(
            name="synthesis",
            cli=synth_info.cli,
            model=synth_info.model,
            budget_usd=synthesis_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )

    return dev_profile, preflight_profile, review_pool, synthesis_profile


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

    # ── Smart config: models key ──────────────────────────────────────
    smart_config_models: list[str] | None = None

    if "models" in raw:
        models_list = raw["models"]
        if not isinstance(models_list, list) or len(models_list) == 0:
            raise ValueError("'models' must be a non-empty list")
        for m in models_list:
            if "/" not in str(m):
                raise ValueError(
                    f"Model entry {m!r} must be in 'provider/model' format (contains '/')"
                )
            provider = str(m).split("/", 1)[0]
            if str(m) not in MODEL_REGISTRY and provider not in _PROVIDER_CLI_MAP:
                raise ValueError(
                    f"Unknown provider {provider!r} in model {m!r}. "
                    f"Supported providers: {sorted(_PROVIDER_CLI_MAP)}. "
                    "Or add the model to MODEL_REGISTRY."
                )
        budget_usd_raw = raw.get("budget_usd", 50.0)
        budget_usd_val = float(budget_usd_raw)
        if budget_usd_val <= 0:
            raise ValueError("budget_usd must be positive")

        dev_profile, preflight_profile, review_pool, synthesis_profile = _auto_assign_models(
            [str(m) for m in models_list], budget_usd_val
        )

        # Apply explicit profile overrides (partial override supported)
        profiles = raw.get("profiles", {})
        if "dev" in profiles:
            dev_profile = _apply_profile_overrides(dev_profile, profiles["dev"])
        if "preflight" in profiles:
            preflight_profile = _apply_profile_overrides(preflight_profile, profiles["preflight"])
        if synthesis_profile is not None and "synthesis" in profiles:
            synthesis_profile = _apply_profile_overrides(synthesis_profile, profiles["synthesis"])
        # Apply per-reviewer overrides matched by name
        # (e.g. profiles.review_pool[{name: claude-opus}])
        if "review_pool" in profiles:
            pool_overrides = profiles["review_pool"]
            if isinstance(pool_overrides, list):
                override_by_name: dict[str, dict[str, Any]] = {
                    e["name"]: e for e in pool_overrides if isinstance(e, dict) and "name" in e
                }
                review_pool = [
                    _apply_profile_overrides(p, override_by_name[p.name])
                    if p.name in override_by_name
                    else p
                    for p in review_pool
                ]

        smart_config_models = [str(m) for m in models_list]

    else:
        # ── Classic config: profiles key ──────────────────────────────────
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
                    f"Unsupported CLI {cli!r} in profiles.review. "
                    f"Supported: {sorted(SUPPORTED_CLIS)}"
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

    # Notifications
    notif_data = raw.get("notifications", {})
    notif_backend = notif_data.get("backend", "none")
    ntfy_config: NtfyConfig | None = None
    if "ntfy" in notif_data:
        ntfy_data = notif_data["ntfy"]
        ntfy_url = ntfy_data.get("url", "")
        if ntfy_url:
            ntfy_config = NtfyConfig(
                url=ntfy_url,
                priority=ntfy_data.get("priority", "high"),
            )
    notifications = NotificationConfig(
        backend=notif_backend,
        ntfy=ntfy_config,
        script=notif_data.get("script"),
        human_review_timeout_seconds=int(notif_data.get("human_review_timeout_seconds", 14400)),
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
        notifications=notifications,
        smart_config_models=smart_config_models,
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
