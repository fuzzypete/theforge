"""Dataclasses and type definitions for TheForge configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import AgentDef

SUPPORTED_PROVIDERS = {"anthropic", "openai", "google", "deepseek"}


@dataclass(frozen=True)
class AssignmentConfig:
    """Configuration for adaptive model assignment."""

    enabled: bool = False
    min_reviewers: int = 1
    max_reviewers: int = 3
    prefer_cross_provider: bool = True
    budget_per_story_usd: float = 15.0
    escalation_memory: bool = True


@dataclass(frozen=True)
class NtfyConfig:
    """Configuration for ntfy.sh push notifications."""

    url: str  # e.g. "https://ntfy.sh/my-topic"
    priority: str = "high"  # ntfy priority: min, low, default, high, urgent


@dataclass(frozen=True)
class SlackConfig:
    """Configuration for Slack webhook notifications."""

    webhook_url_env: str = "SLACK_WEBHOOK_URL"  # env var name holding the webhook URL
    channel: str | None = None  # optional channel override (e.g. "#theforge")
    mention_on_escalate: str | None = None  # optional mention (e.g. "@here")


@dataclass(frozen=True)
class EmailConfig:
    """Stub for future email notification support (not yet implemented)."""

    pass


@dataclass(frozen=True)
class BackendConfig:
    """Configuration for a single notification backend."""

    type: str  # "terminal", "ntfy", "webhook", "slack"
    url: str | None = None
    priority: str | None = None
    webhook_url_env: str | None = None  # Slack: env var name for webhook URL
    channel: str | None = None  # Slack: optional channel override
    mention_on_escalate: str | None = None  # Slack: optional mention on escalations


@dataclass(frozen=True)
class NotificationConfig:
    """Notification backend configuration."""

    backend: str = "none"  # "none", "ntfy", "slack", "osascript"
    ntfy: NtfyConfig | None = None
    slack: SlackConfig | None = None
    email: EmailConfig | None = None  # reserved for future use
    script: str | None = None  # path to custom notification script
    human_review_timeout_seconds: int = 600  # 10 minutes — never block indefinitely
    backends: tuple[BackendConfig, ...] = ()  # pluggable backend list


@dataclass(frozen=True)
class GithubConfig:
    """Native GitHub integration settings.

    When enabled, the coordinator posts PR comments and assigns configured
    GitHub reviewers after creating a PR. Reviewer assignment uses optional
    ModelProfile.github_handle values when present.
    """

    enabled: bool = False


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
    thinking_budget: int | None = None  # Google Gemini ThinkingConfig token budget
    review_role: str | None = None  # "correctness" | "patterns" | "edge-cases"
    phase: str | None = None  # "dev" | "preflight" | "review" | "plan_review" — set by coordinator
    base_url: str | None = None  # overrides provider's default API endpoint (Ollama etc.)
    max_tool_output_bytes: int = 51200  # cap for tool output (50KB default)
    max_iterations: int | None = (
        None  # override default agent loop iterations (None = use default)
    )
    api_fallback: ApiFallbackConfig | None = None  # CLI-only fallback to same-provider API
    github_handle: str | None = None  # optional GitHub username for reviewer assignment

    @property
    def mode(self) -> str:
        return "api" if self.provider else "cli"


@dataclass(frozen=True)
class WorkspaceConfig:
    """How to create isolated workspaces for agents."""

    create_command: str  # shell command template, {slug} and {base_branch} are replaced
    path_pattern: str  # path template, {slug} is replaced
    branch_pattern: str  # branch name template, {slug} is replaced
    base_branch: str = "main"  # base branch for diff comparison
    stale_worktree_days: int = 1  # remove worktrees older than N days; 0 = always remove
    auto_push: bool = False  # push base_branch to origin after successful auto-merge
    setup_command: str | None = None  # optional command run once after workspace creation
    on_approve: str = "none"  # "merge" | "pr" | "merge-pr" | "none"; alias: "ask" → "pr"
    merge_strategy: str = "squash"  # merge | squash | rebase (used by on_approve="merge-pr")
    pr_labels: tuple[str, ...] = ()  # labels to apply when on_approve="pr" or "merge-pr"
    pr_draft: bool = False  # create PR as draft when on_approve="pr"
    ci_check_timeout_seconds: int = 300  # bounded wait for required CI checks after merge


@dataclass(frozen=True)
class ApiFallbackConfig:
    """Fallback API transport for a CLI profile of the same provider."""

    provider: str
    model: str
    timeout_seconds: int | None = None
    reasoning_effort: str | None = None
    thinking_budget: int | None = None
    base_url: str | None = None
    max_iterations: int | None = None


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
    demotion_threshold: int = 2  # parse failures per reviewer per run before exclusion; 0 disables
    plan_escalation_threshold: int = (
        2  # consecutive plan rejections before escalating planner model
    )
    escalate_policy: str = "prompt"  # "prompt" | "auto_approve" | "reject"


@dataclass(frozen=True)
class PlanConfig:
    """Configuration for the PLAN phase (pre-DEV implementation planning).

    Disabled by default; forge.yaml sets enabled: true to opt in.
    This keeps existing test configurations unaffected.

    Transport — exactly one of cli/provider should be set (cli is the default).
    Field semantics match ModelProfile: cli=binary name, model=identifier, provider=API transport.
    """

    enabled: bool = False
    cli: str | None = "claude"  # CLI binary name (e.g. "claude", "codex", "gemini")
    model: str = "sonnet"  # model identifier passed to the CLI or API
    provider: str | None = (
        None  # API transport (e.g. "anthropic", "openai"); mutually exclusive with cli
    )
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
    min_reviewers: int = 1

    @property
    def profiles(self) -> list[ModelProfile]:
        """Return pool list, or a single-profile pool from legacy scalar fields."""
        if self.pool:
            return self.pool
        # Legacy single-profile: construct from scalar fields.
        from .defaults import API_PROVIDER_DEFAULT_TOOLS, DEFAULT_PREFLIGHT_PROFILE

        allowed_tools = (
            API_PROVIDER_DEFAULT_TOOLS
            if self.provider and self.provider in SUPPORTED_PROVIDERS
            else DEFAULT_PREFLIGHT_PROFILE.allowed_tools
        )
        return [
            ModelProfile(
                name="plan-review",
                cli=self.cli,
                provider=self.provider,
                model=self.model or "sonnet",
                budget_usd=self.budget_usd,
                timeout_seconds=self.timeout,
                allowed_tools=allowed_tools,
            )
        ]


@dataclass(frozen=True)
class LogConfig:
    """Configuration for persistent structured logging."""

    log_file: str = ".forge/logs/forge.log"
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
class ContextConfig:
    """Default line budgets for phase-aware context assembly."""

    preflight_budget: int = 200
    plan_budget: int = 120
    dev_budget: int = 80


@dataclass(frozen=True)
class SprintConfig:
    """Project-level sprint defaults from forge.yaml."""

    max_parallel: int = 1


@dataclass(frozen=True)
class HardConventionsConfig:
    """Mechanically enforced code structure rules."""

    max_module_lines: int = 500
    max_test_file_lines: int = 1000
    no_circular_imports: bool = True
    test_mirrors_source: bool = True


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
    github: GithubConfig = field(default_factory=GithubConfig)
    smart_config_models: list[str] | None = None  # None = classic config; list = smart config
    plan: PlanConfig = field(default_factory=PlanConfig)
    plan_review: PlanReviewConfig = field(default_factory=PlanReviewConfig)
    plan_agent_review: PlanAgentReviewConfig = field(default_factory=PlanAgentReviewConfig)
    log: LogConfig = field(default_factory=LogConfig)
    hooks: HooksConfig | None = None
    sprint: SprintConfig = field(default_factory=SprintConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    secrets: dict[str, str] = field(default_factory=dict)
    agents: list[AgentDef] = field(default_factory=list)
    assignment: AssignmentConfig = field(default_factory=AssignmentConfig)
    provider_fallbacks: dict[str, ApiFallbackConfig] = field(default_factory=dict)
    review_pool_is_default: bool = False  # True when review_pool was not explicitly configured
    plan_model_is_default: bool = False  # True when plan.cli/model were not explicitly configured
    conventions_hard: HardConventionsConfig | None = None  # None = no section = no checks
    conventions_soft: list[str] = field(default_factory=list)  # [] = no soft conventions

    @property
    def review_profile(self) -> ModelProfile:
        """Backward-compat: returns review_pool[0]."""
        return self.review_pool[0]
