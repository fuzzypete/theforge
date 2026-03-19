"""Orchestrator configuration: forge.yaml loader and typed dataclasses.

TheForge is project-agnostic. All project-specific details (workspace commands,
validation commands, model selection) live in forge.yaml in the consuming project.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

log = logging.getLogger(__name__)

# ── Model registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelInfo:
    """Built-in metadata for a known model."""

    cli: str  # "claude", "codex", "gemini"
    model: str  # model identifier for the CLI
    tier: str  # "fast" or "strong"
    capability: int  # relative capability score (1-10)
    cost_rank: int  # 1=cheap, 2=moderate, 3=expensive
    dev_capable: bool = True  # False for models whose CLI doesn't support dev tools


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
        cli="gemini",
        model="gemini-2.5-pro",
        tier="strong",
        capability=8,
        cost_rank=2,
        dev_capable=False,
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


SUPPORTED_PROVIDERS = {"anthropic", "openai", "google", "deepseek"}


@dataclass(frozen=True)
class ModelProfile:
    """Model configuration for a specific agent role (dev or review)."""

    name: str  # "dev", "review", or pool entry name like "opus-reviewer"
    model: str  # "sonnet", "opus", "claude-sonnet-4-6"
    budget_usd: float  # cumulative cost ceiling across all invocations
    timeout_seconds: int  # subprocess timeout
    allowed_tools: tuple[str, ...]  # tools the agent may use
    # Transport — exactly one of cli/provider is set
    cli: str | None = None  # "claude", "codex", "gemini"
    provider: str | None = None  # "anthropic", "openai", "google"
    # Optional
    timeout_medium_seconds: int | None = None  # override for medium complexity
    timeout_large_seconds: int | None = None  # override for large complexity
    reasoning_effort: str | None = None  # "low" | "medium" | "high"; Codex only
    review_role: str | None = None  # "correctness" | "patterns" | "edge-cases"
    base_url: str | None = None  # overrides provider's default API endpoint (Ollama etc.)
    max_tool_output_bytes: int = 51200  # cap for tool output (50KB default)
    max_iterations: int | None = (
        None  # override default agent loop iterations (None = use default)
    )

    @property
    def mode(self) -> str:
        return "api" if self.provider else "cli"


@dataclass(frozen=True)
class WorkspaceConfig:
    """How to create isolated workspaces for agents."""

    create_command: str  # shell command template, {slug} is replaced
    path_pattern: str  # path template, {slug} is replaced
    branch_pattern: str  # branch name template, {slug} is replaced
    base_branch: str = "main"  # base branch for diff comparison
    stale_worktree_days: int = 1  # remove worktrees older than N days; 0 = always remove
    auto_push: bool = False  # push base_branch to origin after successful auto-merge
    setup_command: str | None = None  # optional command run once after workspace creation
    on_approve: str = "none"  # "merge" (auto-merge) | "pr" (create GitHub PR) | "none" (skip)
    pr_labels: tuple[str, ...] = ()  # labels to apply when on_approve="pr"
    pr_draft: bool = False  # create PR as draft when on_approve="pr"


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
    gate_output_tail_chars: int = 2000  # chars of gate output to surface on FAIL
    pre_validate_command: str | None = None  # optional command run before dirty check


@dataclass(frozen=True)
class RetryPolicy:
    """Retry limits before escalating to human."""

    max_dev_iterations: int = 3  # retries within a single review cycle
    max_review_cycles: int = 2  # full dev->review loops
    max_review_parse_retries: int = 2  # reviewer retries on parse/schema error per cycle
    max_handoff_retries: int = 2  # dev handoff rewrite retries after gate passes
    max_plan_regen_attempts: int = 3  # plan review rejection → regen cycles before escalating


@dataclass(frozen=True)
class PlanConfig:
    """Configuration for the PLAN phase (pre-DEV implementation planning).

    Disabled by default; forge.yaml sets enabled: true to opt in.
    This keeps existing test configurations unaffected.
    """

    enabled: bool = False
    model: str = "claude"  # CLI name (the CLI binary, e.g. "claude")
    model_name: str = "sonnet"  # model identifier passed to the CLI
    budget_usd: float = 0.50
    timeout: int = 600
    timeout_medium: int | None = None  # override for medium complexity
    timeout_large: int | None = None  # override for large complexity


@dataclass(frozen=True)
class PlanReviewConfig:
    """Configuration for the PLAN_REVIEW gate."""

    enabled: bool = False
    mode: str = "blocking"  # "blocking" | "advisory"
    timeout_seconds: int = 14400  # advisory: auto-approve after this many seconds (4 h)


@dataclass(frozen=True)
class PlanAgentReviewConfig:
    """Configuration for automated agent review of plans before dev.

    When enabled, takes precedence over PlanReviewConfig (human review).
    They are mutually exclusive — agent review replaces human review.

    Supports both legacy single-profile format (scalar fields) and a new
    pool format (list of ModelProfile objects). Use the ``profiles`` property
    to get the canonical pool regardless of which format was used.
    """

    enabled: bool = False
    # Legacy single-profile fields — present only when pool is empty.
    cli: str | None = "claude"
    provider: str | None = None
    model: str = "sonnet"
    budget_usd: float = 0.50
    timeout: int = 300
    # Pool format — populated by load_forge_yaml when pool: key is present.
    pool: list[ModelProfile] = field(default_factory=list)

    @property
    def profiles(self) -> list[ModelProfile]:
        """Return pool list, or a single-profile pool from legacy scalar fields."""
        if self.pool:
            return self.pool
        # Legacy single-profile: construct from scalar fields.
        # Use DEFAULT_PREFLIGHT_PROFILE.allowed_tools as the standard plan-review tool set.
        return [
            ModelProfile(
                name="plan-review",
                cli=self.cli,
                provider=self.provider,
                model=self.model or "sonnet",
                budget_usd=self.budget_usd,
                timeout_seconds=self.timeout,
                allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
            )
        ]


@dataclass(frozen=True)
class LogConfig:
    """Configuration for persistent structured logging."""

    log_file: str = "~/.forge/logs/{project}/forge.log"
    enabled: bool = True


@dataclass(frozen=True)
class HooksConfig:
    """Lifecycle hook commands fired at key forge events."""

    post_run: str | None = None
    post_merge: str | None = None
    post_sprint: str | None = None
    pre_run: str | None = None
    timeout_seconds: int = 30


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
    plan: PlanConfig = field(default_factory=PlanConfig)
    plan_review: PlanReviewConfig = field(default_factory=PlanReviewConfig)
    plan_agent_review: PlanAgentReviewConfig = field(default_factory=PlanAgentReviewConfig)
    log: LogConfig = field(default_factory=LogConfig)
    hooks: HooksConfig | None = None
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def review_profile(self) -> ModelProfile:
        """Backward-compat: returns review_pool[0]."""
        return self.review_pool[0]


# ── Defaults ──────────────────────────────────────────────────────────


DEFAULT_DEV_PROFILE = ModelProfile(
    name="dev",
    cli="claude",
    provider=None,
    model="sonnet",
    budget_usd=2.00,
    timeout_seconds=900,
    allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
)

DEFAULT_REVIEW_PROFILE = ModelProfile(
    name="review",
    cli="claude",
    provider=None,
    model="opus",
    budget_usd=1.00,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash", "Glob", "Grep"),
)

DEFAULT_PREFLIGHT_PROFILE = ModelProfile(
    name="preflight",
    cli="claude",
    provider=None,
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
PROVIDER_SDK_MAP = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google.genai",
    "deepseek": "openai",
}
PROVIDER_API_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


def _resolve_secret(key: str, secrets: dict[str, str]) -> str | None:
    """Check secrets dict first, then fall back to os.environ."""
    return secrets.get(key) or os.getenv(key)


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
    timeout_medium_raw = data.get("timeout_medium_seconds", base.timeout_medium_seconds)
    timeout_large_raw = data.get("timeout_large_seconds", base.timeout_large_seconds)
    return ModelProfile(
        name=base.name,
        cli=data.get("cli", base.cli),
        provider=data.get("provider", base.provider),
        model=data.get("model", base.model),
        budget_usd=float(data.get("budget_usd", base.budget_usd)),
        timeout_seconds=int(data.get("timeout_seconds", base.timeout_seconds)),
        timeout_medium_seconds=int(timeout_medium_raw) if timeout_medium_raw is not None else None,
        timeout_large_seconds=int(timeout_large_raw) if timeout_large_raw is not None else None,
        allowed_tools=tuple(tools) if tools is not None else base.allowed_tools,
        reasoning_effort=reasoning_effort,
        base_url=data.get("base_url", base.base_url),
        max_iterations=int(max_iter_raw)
        if (max_iter_raw := data.get("max_iterations", base.max_iterations)) is not None
        else None,
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
        provider=None,
        model=dev_info.model,
        budget_usd=dev_budget,
        timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
    )
    preflight_profile = ModelProfile(
        name="preflight",
        cli=preflight_info.cli,
        provider=None,
        model=preflight_info.model,
        budget_usd=preflight_budget,
        timeout_seconds=DEFAULT_PREFLIGHT_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
    )
    review_pool = [
        ModelProfile(
            name=k.replace("/", "-"),
            cli=i.cli,
            provider=None,
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
            provider=None,
            model=synth_info.model,
            budget_usd=synthesis_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )

    return dev_profile, preflight_profile, review_pool, synthesis_profile


# ── Loader ────────────────────────────────────────────────────────────


def _parse_profile(
    name: str,
    data: dict[str, Any],
    *,
    role: str = "review",
    secrets: dict[str, str] | None = None,
) -> ModelProfile:
    """Parse a model profile from forge.yaml data.

    role controls which defaults to apply: "dev" uses DEFAULT_DEV_PROFILE,
    anything else uses DEFAULT_REVIEW_PROFILE. This prevents pool entries
    named "dev" from accidentally inheriting dev-level tools/timeouts.
    """
    default = DEFAULT_DEV_PROFILE if role == "dev" else DEFAULT_REVIEW_PROFILE
    cli = data.get("cli")
    provider = data.get("provider")

    if cli and provider:
        raise ValueError(f"Profile {name!r} cannot have both 'cli' and 'provider' set. Use one.")
    if not cli and not provider:
        # Fallback to default if neither is specified
        cli = default.cli
        provider = default.provider

    if cli and cli not in SUPPORTED_CLIS:
        raise ValueError(
            f"Unsupported CLI {cli!r} in profile {name!r}. Supported: {sorted(SUPPORTED_CLIS)}"
        )
    if provider:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported provider {provider!r} in profile {name!r}. "
                f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
            )
        # Eagerly validate provider readiness
        sdk = PROVIDER_SDK_MAP.get(provider)
        if sdk:
            try:
                importlib.import_module(sdk)
            except ImportError:
                raise ValueError(
                    f"Profile {name!r} uses provider '{provider}' but the required "
                    f"SDK '{sdk}' is not installed. Please install it."
                )
        base_url_early = data.get("base_url")
        _is_local = base_url_early and any(
            base_url_early.startswith(p) for p in ("http://localhost", "http://127.0.0.1")
        )
        api_key_var = PROVIDER_API_KEY_MAP.get(provider)
        if api_key_var and not _resolve_secret(api_key_var, secrets or {}) and not _is_local:
            raise ValueError(
                f"Profile {name!r} uses provider '{provider}' but the required "
                f"environment variable ${api_key_var} is not set."
            )

    tools = data.get("allowed_tools")
    reasoning_effort = data.get("reasoning_effort")
    _VALID_REASONING_EFFORTS = {"low", "medium", "high"}
    if reasoning_effort is not None and reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORTS)}, "
            f"got {reasoning_effort!r} in profile {name!r}"
        )
    timeout_medium_raw = data.get("timeout_medium_seconds")
    timeout_large_raw = data.get("timeout_large_seconds")

    # Build allowed_tools tuple. For API profiles, normalize capitalized names to canonical names.
    if tools is not None:
        if provider:
            from .tool_runtime import TOOL_NAME_MAP

            allowed_tools_tuple = tuple(TOOL_NAME_MAP.get(t, t) for t in tools)
        else:
            allowed_tools_tuple = tuple(tools)
    elif provider:
        allowed_tools_tuple = ()
    else:
        allowed_tools_tuple = default.allowed_tools

    return ModelProfile(
        name=name,
        cli=cli,
        provider=provider,
        model=data.get("model", default.model),
        budget_usd=float(data.get("budget_usd", default.budget_usd)),
        timeout_seconds=int(data.get("timeout_seconds", default.timeout_seconds)),
        timeout_medium_seconds=int(timeout_medium_raw) if timeout_medium_raw is not None else None,
        timeout_large_seconds=int(timeout_large_raw) if timeout_large_raw is not None else None,
        allowed_tools=allowed_tools_tuple,
        reasoning_effort=reasoning_effort,
        review_role=data.get("review_role"),
        base_url=data.get("base_url"),
        max_iterations=int(max_iter_raw)
        if (max_iter_raw := data.get("max_iterations")) is not None
        else None,
    )


def load_config(config_path: Path) -> ForgeConfig:
    """Load forge.yaml and return a typed ForgeConfig.

    The config file path is used to derive the project root (its parent directory).
    Missing sections fall back to sensible defaults.

    Raises ValueError for invalid configurations (empty pool, duplicate names,
    unsupported CLI, missing synthesis profile when pool size > 1).
    """
    project_root = config_path.parent.resolve()

    # Load project-scoped secrets before profile validation so _resolve_secret() works.
    env_path = project_root / ".forge" / ".env"
    secrets_yaml_path = project_root / ".forge" / "secrets.yaml"
    secrets: dict[str, str] = {}
    if env_path.exists():
        raw = dotenv_values(env_path)
        if any(v is None for v in raw.values()):
            raise ValueError(f"{env_path}: malformed .env")
        secrets = {k: v for k, v in raw.items() if v is not None}
    elif secrets_yaml_path.exists():
        log.warning(
            "⚠ .forge/secrets.yaml detected — migrate to .forge/.env (see .forge/.env.example)"
        )

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
        auto_push=bool(ws_data.get("auto_push", DEFAULT_WORKSPACE.auto_push)),
        setup_command=ws_data.get("setup_command", DEFAULT_WORKSPACE.setup_command),
        on_approve=str(ws_data.get("on_approve", "none")),
        pr_labels=tuple(ws_data.get("pr_labels", [])),
        pr_draft=bool(ws_data.get("pr_draft", False)),
    )

    # Validation
    val_data = raw.get("validation", {})
    validation = ValidationConfig(
        gate_command=val_data.get("gate_command", DEFAULT_VALIDATION.gate_command),
        handoff_file=val_data.get("handoff_file", DEFAULT_VALIDATION.handoff_file),
        gate_decision_key=val_data.get("gate_decision_key", DEFAULT_VALIDATION.gate_decision_key),
        gate_timeout=val_data.get("gate_timeout"),
        gate_output_tail_chars=int(
            val_data.get("gate_output_tail_chars", DEFAULT_VALIDATION.gate_output_tail_chars)
        ),
        pre_validate_command=val_data.get("pre_validate_command"),
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
            _parse_profile("dev", profiles["dev"], role="dev", secrets=secrets)
            if "dev" in profiles
            else DEFAULT_DEV_PROFILE
        )
        preflight_profile = (
            _parse_profile("preflight", profiles["preflight"], role="review", secrets=secrets)
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
            review_pool = [
                _parse_profile(e["name"], e, role="review", secrets=secrets) for e in pool_data
            ]
            # synthesis is optional — multiple reviewers are merged deterministically
            if "synthesis" in profiles:
                synth_data = profiles["synthesis"]
                synthesis_profile: ModelProfile | None = _parse_profile(
                    "synthesis", synth_data, role="review", secrets=secrets
                )
            else:
                synthesis_profile = None

        elif "review" in profiles:
            # Backward compat: single review dict wrapped into a pool of one.
            review_data = profiles["review"]
            review_pool = [_parse_profile("review", review_data, role="review", secrets=secrets)]
            synthesis_profile = None

        else:
            review_pool = [DEFAULT_REVIEW_PROFILE]
            synthesis_profile = None

    # smart_config_models — escalation chain; works alongside explicit profiles
    if smart_config_models is None and "smart_config_models" in raw:
        models_raw = raw["smart_config_models"]
        if isinstance(models_raw, list) and models_raw:
            smart_config_models = [str(m) for m in models_raw]

    # Retry
    retry_data = raw.get("retry", {})
    retry = RetryPolicy(
        max_dev_iterations=int(retry_data.get("max_dev_iterations", 3)),
        max_review_cycles=int(retry_data.get("max_review_cycles", 2)),
        max_review_parse_retries=int(retry_data.get("max_review_parse_retries", 2)),
        max_handoff_retries=int(retry_data.get("max_handoff_retries", 2)),
        max_plan_regen_attempts=int(retry_data.get("max_plan_regen_attempts", 3)),
    )

    # Notifications
    notif_data = raw.get("notifications", {})
    notif_backend = notif_data.get("backend", "none")
    ntfy_config: NtfyConfig | None = None
    if "ntfy" in notif_data:
        ntfy_data = notif_data["ntfy"]
        ntfy_url = ntfy_data.get("url") or secrets.get("NTFY_URL") or os.getenv("NTFY_URL") or ""
        if ntfy_url:
            ntfy_config = NtfyConfig(
                url=ntfy_url,
                priority=ntfy_data.get("priority", "high"),
            )
        elif notif_backend == "ntfy":
            log.warning("ntfy backend enabled but no URL configured — notifications disabled")
    elif notif_backend == "ntfy":
        ntfy_url = secrets.get("NTFY_URL") or os.getenv("NTFY_URL") or ""
        if ntfy_url:
            ntfy_config = NtfyConfig(url=ntfy_url, priority="high")
        else:
            log.warning("ntfy backend enabled but no URL configured — notifications disabled")
    notifications = NotificationConfig(
        backend=notif_backend,
        ntfy=ntfy_config,
        script=notif_data.get("script"),
        human_review_timeout_seconds=int(notif_data.get("human_review_timeout_seconds", 14400)),
    )

    # Plan
    plan_data = raw.get("plan", {})
    plan_timeout_medium_raw = plan_data.get("timeout_medium")
    plan_timeout_large_raw = plan_data.get("timeout_large")
    plan_cfg = PlanConfig(
        enabled=bool(plan_data.get("enabled", False)),
        model=str(plan_data.get("model", "claude")),
        model_name=str(plan_data.get("model_name", "sonnet")),
        budget_usd=float(plan_data.get("budget_usd", 0.50)),
        timeout=int(plan_data.get("timeout", 600)),
        timeout_medium=int(plan_timeout_medium_raw)
        if plan_timeout_medium_raw is not None
        else None,
        timeout_large=int(plan_timeout_large_raw) if plan_timeout_large_raw is not None else None,
    )

    # Plan review
    plan_review_data = raw.get("plan_review", {})
    plan_review_cfg = PlanReviewConfig(
        enabled=bool(plan_review_data.get("enabled", False)),
        mode=str(plan_review_data.get("mode", "blocking")),
        timeout_seconds=int(plan_review_data.get("timeout_seconds", 14400)),
    )

    # Plan agent review
    par_data = raw.get("plan_agent_review", {})
    par_enabled = bool(par_data.get("enabled", False))
    par_cli = par_data.get("cli")
    par_provider = par_data.get("provider")

    if par_enabled:
        if par_cli and par_provider:
            raise ValueError(
                "plan_agent_review cannot have both 'cli' and 'provider' set. Use one."
            )
        if not par_cli and not par_provider:
            # Default to cli: claude if neither is set
            par_cli = "claude"

        if par_cli and par_cli not in SUPPORTED_CLIS:
            raise ValueError(
                f"Unsupported CLI {par_cli!r} in plan_agent_review. "
                f"Supported: {sorted(SUPPORTED_CLIS)}"
            )
        if par_provider:
            if par_provider not in SUPPORTED_PROVIDERS:
                raise ValueError(
                    f"Unsupported provider {par_provider!r} in plan_agent_review. "
                    f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
                )
            # Eagerly validate provider readiness
            sdk = PROVIDER_SDK_MAP.get(par_provider)
            if sdk:
                try:
                    importlib.import_module(sdk)
                except ImportError:
                    raise ValueError(
                        f"plan_agent_review uses provider '{par_provider}' but the required "
                        f"SDK '{sdk}' is not installed. Please install it."
                    )
            api_key_var = PROVIDER_API_KEY_MAP.get(par_provider)
            if api_key_var and not _resolve_secret(api_key_var, secrets):
                raise ValueError(
                    f"plan_agent_review uses provider '{par_provider}' but the required "
                    f"environment variable ${api_key_var} is not set."
                )

    # Parse pool entries if present (new format)
    par_pool: list[ModelProfile] = []
    if "pool" in par_data:
        pool_data = par_data["pool"]
        if not isinstance(pool_data, list) or len(pool_data) == 0:
            raise ValueError("plan_agent_review.pool must be a non-empty list")
        pool_names = [e.get("name") for e in pool_data]
        if any(n is None for n in pool_names):
            raise ValueError("Each plan_agent_review.pool entry must have a 'name' field")
        if len(pool_names) != len(set(pool_names)):
            raise ValueError(f"Duplicate names in plan_agent_review.pool: {pool_names}")
        par_pool = [
            _parse_profile(e["name"], e, role="review", secrets=secrets) for e in pool_data
        ]

    plan_agent_review_cfg = PlanAgentReviewConfig(
        enabled=par_enabled,
        cli=par_cli,
        provider=par_provider,
        model=str(par_data.get("model", "sonnet")),
        budget_usd=float(par_data.get("budget_usd", 0.50)),
        timeout=int(par_data.get("timeout", 300)),
        pool=par_pool,
    )

    # Logging
    log_data = raw.get("logging", {})
    log_cfg = LogConfig(
        log_file=str(log_data.get("log_file", LogConfig.log_file)),
        enabled=bool(log_data.get("enabled", True)),
    )

    # Hooks
    hooks_data = raw.get("hooks")
    hooks_cfg: HooksConfig | None = None
    if hooks_data:
        hooks_cfg = HooksConfig(
            post_run=hooks_data.get("post_run"),
            post_merge=hooks_data.get("post_merge"),
            post_sprint=hooks_data.get("post_sprint"),
            pre_run=hooks_data.get("pre_run"),
            timeout_seconds=int(hooks_data.get("timeout_seconds", 30)),
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
        plan=plan_cfg,
        plan_review=plan_review_cfg,
        plan_agent_review=plan_agent_review_cfg,
        log=log_cfg,
        hooks=hooks_cfg,
        secrets=secrets,
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
  # Optional: run once in the new workspace after creation (e.g. install deps).
  # setup_command: "pip install -e ."
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
