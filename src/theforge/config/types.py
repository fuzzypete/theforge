"""Dataclasses and type definitions for TheForge configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import AgentDef, AgentSpec, TransportSpec

SUPPORTED_PROVIDERS = {"anthropic", "openai", "google", "deepseek"}


@dataclass(frozen=True)
class AssignmentConfig:
    """Configuration for adaptive model assignment."""

    enabled: bool = False
    min_reviewers: int = 1
    max_reviewers: int = 3
    prefer_cross_provider: bool = True
    max_cost_per_story_usd: float | None = None
    escalation_memory: bool = True
    adaptive_enabled: bool = True


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
class StuckDetectionConfig:
    """Thresholds for progress-aware stuck-agent detection.

    The runner monitors per-iteration tool-call activity for three observable
    stall patterns: repeated identical (name+arguments) tool calls, no file
    modifications across consecutive iterations, and tool-result error loops.
    On detection, the runner injects a one-shot nudge message; if the same
    pattern continues for ``post_nudge_iterations`` more iterations, the run
    is terminated with a structured failure log.

    Detection is gated by profile.phase == "dev" so review/preflight loops
    are unaffected.
    """

    enabled: bool = True
    no_progress_iterations: int = 5  # N — iterations without file modification → stuck
    repeat_threshold: int = 4  # consecutive identical (name+args) tool calls → stuck
    error_threshold: int = 4  # consecutive identical error tool results → stuck
    post_nudge_iterations: int = 3  # M — iterations after nudge before termination
    # Per-complexity multipliers applied to no_progress_iterations and post_nudge_iterations.
    # LARGE/medium stories often need more pre-modification exploration; flat thresholds
    # false-terminate competent dev agents. Scaling is applied in dev_phase.py.
    no_progress_multipliers: dict[str, float] = field(
        default_factory=lambda: {"small": 1.0, "medium": 1.5, "large": 2.5}
    )
    post_nudge_multipliers: dict[str, float] = field(
        default_factory=lambda: {"small": 1.0, "medium": 1.5, "large": 2.0}
    )


@dataclass(frozen=True)
class ModelProfile:
    """Model configuration for a specific agent role (dev or review)."""

    name: str  # "dev", "review", or pool entry name like "opus-reviewer"
    model: str  # primary model identifier (first in preference list)
    budget_usd: float  # cumulative cost ceiling across all invocations
    timeout_seconds: int  # subprocess timeout
    allowed_tools: tuple[str, ...]  # tools the agent may use
    # Transport identity — ``transport`` below is the single source of truth for
    # runtime dispatch. ``cli`` names a CLI binary ("claude"/"codex"/"gemini")
    # and ``provider`` keys auth and pricing ("anthropic"/"openai"/...). Both
    # may coexist; when set without an explicit transport, __post_init__ infers
    # a canonical TransportSpec (cli wins).
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
    fallback_models: tuple[str, ...] = ()  # additional models to try on quota/not-found failure
    sandbox_mode: str = "workspace-write"  # CLI sandbox: "workspace-write" | "read-only" | "none"
    registry_id: str | None = None  # canonical model registry key, when sourced from a registry
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    # Explicit TransportSpec — when set, it is the runtime dispatch source of
    # truth. None preserves the legacy inference from cli/provider for the
    # small number of paths (e.g. raw dataclass constructions in tests) that
    # have not been migrated yet.
    transport: TransportSpec | None = None
    # Per-profile stuck-detection thresholds; None falls back to the
    # ForgeConfig-level default and only fires when phase == "dev".
    stuck_detection: StuckDetectionConfig | None = None

    def __post_init__(self) -> None:
        if self.transport is None and (self.cli or self.provider):
            from .models import infer_transport

            inferred = infer_transport(self.cli, self.provider)
            if inferred is not None:
                object.__setattr__(self, "transport", inferred)

    @property
    def models(self) -> tuple[str, ...]:
        """Full preference list: primary model followed by fallbacks."""
        return (self.model,) + self.fallback_models

    @property
    def mode(self) -> str:
        """Transport kind for runtime dispatch: 'cli' or 'api'.

        Reads TransportSpec.kind — the single source of truth for dispatch.
        Falls back to cli/provider inference only when transport is absent
        (e.g. ad-hoc dataclasses with neither cli nor provider set).
        """
        if self.transport is not None:
            return self.transport.kind
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
    merge_wait_timeout_seconds: int = 3600  # bounded wait for queued merge-pr landing; expiry is
    # fail-closed: story is marked failed and dependents remain blocked


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

    Gate mode: passes if the gate_command exits 0, fails otherwise.
    The coordinator reads no file for the pass/fail decision.
    """

    gate_command: str  # e.g. "make fmt && pytest"
    gate_timeout: int | None = None  # seconds; None = default 600
    gate_output_tail_chars: int = 2000  # chars of gate output to surface on FAIL
    gate_debug_command: str | None = (
        None  # diagnostic command on timeout (e.g. "pytest -x -v -n 0")
    )
    gate_debug_timeout: int | None = None  # seconds; None = same resolved value as gate_timeout
    test_command: str | None = None  # canonical command for intermediate test runs in dev loop
    # Substituted for {test_target} when no task is available (baseline gate) or
    # when a task has no test_target of its own.
    default_test_target: str = "."
    pre_validate_command: str | None = None  # optional command run before dirty check
    # Adaptive gate-timeout scaling under sprint --parallel N. The baseline
    # gate_timeout above is the alone-time budget. When mode == "adaptive"
    # (default), sprint start scales the effective gate_timeout by host CPU
    # contention. "fixed" disables scaling so gate_timeout is a hard ceiling
    # regardless of parallelism.
    gate_cpu_cores: int | None = None  # operator hint for gate CPU demand; None => host_cores
    gate_timeout_scale: str = "adaptive"  # "adaptive" | "fixed"


@dataclass(frozen=True)
class RetryPolicy:
    """Retry limits before escalating to human."""

    max_dev_iterations: int = 3  # retries within a single review cycle
    max_dev_transport_retries: int = (
        1  # per-iteration retries on transient dev transport/provider failure
    )
    max_plan_transport_retries: int = (
        2  # per-attempt retries on transient plan draft/regen transport/provider failure
    )
    max_review_cycles: int = 2  # full dev->review loops
    max_review_parse_retries: int = 2  # reviewer retries on parse/schema error per cycle
    max_plan_review_transport_retries: int = (
        2  # per-reviewer retries on transient plan-review transport/provider failure
    )
    max_plan_review_parse_retries: int = (
        2  # per-reviewer fresh-session retries when a plan reviewer completes but
        # emits unparseable output (prose / non-mapping YAML root); 0 disables
    )
    # Per-reviewer retries on transient review-pool transport/provider failure.
    max_review_transport_retries: int = 2
    # Initial backoff (seconds) for review-pool transient retry; doubled per retry.
    review_transport_retry_backoff_seconds: float = 8.0
    # Minimum successful reviewers required to proceed to synthesis without
    # escalating; collapses to 1 when the panel size is 1.
    review_quorum_threshold: int = 2
    # When True, a review cycle whose quorum shortfall is caused entirely by
    # non-verdict reviewer failures (a reviewer that finished without emitting a
    # submit call) degrades to the surviving verdict(s) with an explicit audit
    # warning instead of failing the story — provided at least one reviewer
    # delivered a verdict. Story failure is reserved for a total quorum collapse
    # (zero survivors) or a genuine hard failure blocking the quorum.
    review_degrade_on_infra_failure: bool = True
    # Failure-code identifiers (from AgentResult.failure_code) that mark a
    # review-pool failure as transient/retryable.
    review_transient_failure_codes: tuple[str, ...] = (
        "rate_limit",
        "provider_internal_error",
        "connection_reset",
    )
    # Substrings (matched case-insensitively against agent output) treated as
    # transient signatures for the review pool.
    review_transient_output_patterns: tuple[str, ...] = (
        "429",
        "rate limit",
        "rate-limited",
        "resource_exhausted",
        "resource exhausted",
        "quota exceeded",
        "quota_exceeded",
        "500",
        "502",
        "503",
        "504",
        "internal error",
        "server error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "connection reset",
        "connection-reset",
        "econnreset",
        "connection aborted",
        "connection refused",
        "peer closed connection",
        "temporarily unavailable",
        "try again later",
        "timeout awaiting headers",
    )
    max_plan_regen_attempts: int = 3  # plan review rejection → regen cycles before escalating
    demotion_threshold: int = 2  # parse failures per reviewer per run before exclusion; 0 disables
    plan_escalation_threshold: int = (
        2  # consecutive plan rejections before escalating planner model
    )
    escalate_policy: str = "prompt"  # "prompt" | "auto_approve" | "reject"
    auto_model_escalation: bool = False  # escalate dev model on persistent P1; disabled by default
    # Adaptive iteration limits: scale max_dev_iterations / max_review_cycles
    # per-story from preflight complexity_score and historical usage. The
    # `max_dev_iterations`/`max_review_cycles` fields act as the floor (minimum
    # grant) when adaptive is enabled; the `*_cap` fields are the hard ceiling.
    # Caps default to 0 (meaning "same as floor" — no adaptive growth); set
    # them explicitly in forge.yaml to opt into scaling.
    adaptive_iterations: bool = True
    max_dev_iterations_cap: int = 0
    max_review_cycles_cap: int = 0
    # Stop the review loop early when this many consecutive iterations produce
    # zero new findings. 0 disables early termination.
    review_zero_findings_stop: int = 0
    # After APPROVE with open P2 findings, the coordinator re-enters DEV to
    # address them as advisory cleanup until the dev iteration budget is
    # exhausted. Enabled by default. p2_cleanup_max_iterations=0 means
    # "no separate cap — use whatever remains of the per-cycle dev budget".
    p2_cleanup_enabled: bool = True
    p2_cleanup_max_iterations: int = 0


@dataclass(frozen=True)
class PlanConfig:
    """Configuration for the PLAN phase (pre-DEV implementation planning).

    Disabled by default; forge.yaml sets enabled: true to opt in.
    This keeps existing test configurations unaffected.

    Field semantics match ModelProfile: cli names a CLI binary, provider keys
    auth/pricing, model is the identifier. Either or both may be set; when both
    are supplied the coordinator resolves plan dispatch via the same transport
    inference as ModelProfile.
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
    validate_spec: bool = True  # whether to run story_validator before planning
    api_fallback: ApiFallbackConfig | None = None  # CLI-only fallback to same-provider API


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
    api_fallback: ApiFallbackConfig | None = None  # legacy scalar profile fallback
    min_reviewers: int = 1

    @property
    def profiles(self) -> list[ModelProfile]:
        """Return pool list, or a single-profile pool from legacy scalar fields."""
        if self.pool:
            return self.pool
        if not self.cli and not self.provider:
            return []
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
                api_fallback=self.api_fallback,
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
    review_budget: int = 80


@dataclass(frozen=True)
class FindingClassifierConfig:
    """Configuration for the finding classifier heuristics."""

    allow_net_new_bypass: bool = False


@dataclass(frozen=True)
class SprintConfig:
    """Project-level sprint defaults from forge.yaml."""

    max_parallel: int = 1
    worker_timeout_seconds: int = 3600


@dataclass(frozen=True)
class ShapeCheckConfig:
    """Shared shape_check settings used by the #811 Action and sprint entry.

    ``classifier`` selects the shape-check classifier mode: ``heuristic`` for
    deterministic stdlib-only checks, or a provider name (e.g. ``claude``)
    when an LLM-assisted classifier should be used. Sprint entry falls back
    to ``heuristic`` when the configured provider is unavailable at sprint
    time (e.g. no credentials, no SDK installed, no network).
    """

    classifier: str = "heuristic"


@dataclass(frozen=True)
class IntakeConfig:
    """Sprint-intake remediation settings.

    Controls the auto-fix gate that runs between dependency normalization
    and batch preflight. ``grooming`` enables the text-only semantic
    grooming check. ``auto_fix`` enables the one-pass agent remediation on
    failure. ``auto_fix_mode`` selects the output mode: ``comment`` posts
    the proposed replacement and drops the story; ``edit`` updates the
    issue body in place and reruns the gate once.
    """

    grooming: bool = False
    auto_fix: bool = False
    auto_fix_mode: str = "comment"  # "comment" | "edit"


@dataclass(frozen=True)
class HardConventionsConfig:
    """Mechanically enforced code structure rules."""

    max_module_lines: int = 500
    max_test_file_lines: int = 1000
    no_circular_imports: bool = True
    test_mirrors_source: bool = True
    no_scratch_files: bool = True
    stack: tuple[str, ...] = ()
    allowed_root_files: tuple[str, ...] = ()
    # Repo-relative package directories scanned by the circular-import,
    # test-mirror, and line-count checks. Empty preserves the legacy
    # src/theforge scope; consumer projects list their own package roots
    # (e.g. ("src/pipeline", "analysis", "api")).
    package_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdvisoryIssueFilingConfig:
    """Opt-in GitHub issue filing for advisory convention debt."""

    enabled: bool = False
    threshold_percent: float = 25.0
    label: str = "refactor-debt"
    milestone: str | None = None


@dataclass(frozen=True)
class AdvisoryConventionsConfig:
    """Aggregation and surfacing settings for non-blocking convention debt."""

    artifact_path: str = ".forge/conventions/advisory.yaml"
    summary_top_n: int = 10
    noteworthy_threshold_percent: float = 10.0
    commit_shared_artifact: bool = False
    shared_artifact_path: str | None = None
    issue_filing: AdvisoryIssueFilingConfig = field(default_factory=AdvisoryIssueFilingConfig)


@dataclass(frozen=True)
class DiagnoseConfig:
    """Configuration for the ``forge diagnose`` flow.

    The diagnose flow is intentionally separate from the sprint pipeline:
    different state machine, different prompts, different success criterion.
    Its budget and timeout are independent so a long-running cause hunt
    cannot consume sprint budget — and a sprint cannot starve diagnosis.

    ``output_destination`` controls where the diagnosis artifact lands:
      - ``body_section`` — upsert a ``## Diagnosis`` section in the issue body
                           (default: the sprint shape gate reads the issue body
                           for the diagnosis artifact, so this is the destination
                           that leaves the issue fix-ready after diagnose)
      - ``comment``      — post the artifact as a new GitHub issue comment
      - ``pr_to_body``   — write the artifact to ``.forge/diagnoses/issue-N.md``
                           so the operator can open a body-edit PR manually
    """

    output_destination: str = "body_section"
    budget_usd: float = 1.50
    timeout_seconds: int = 600
    autonomous_default: bool = True  # default mode when --interactive is not passed


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
    preflight_fallback_profile: ModelProfile | None = None  # optional one-shot retry profile
    notifications: NotificationConfig = NotificationConfig()
    github: GithubConfig = field(default_factory=GithubConfig)
    models: list[str] | None = None  # raw v0.8 `models:` list; None = not set (uses defaults)
    plan: PlanConfig = field(default_factory=PlanConfig)
    plan_review: PlanReviewConfig = field(default_factory=PlanReviewConfig)
    plan_agent_review: PlanAgentReviewConfig = field(default_factory=PlanAgentReviewConfig)
    log: LogConfig = field(default_factory=LogConfig)
    hooks: HooksConfig | None = None
    sprint: SprintConfig = field(default_factory=SprintConfig)
    shape_check: ShapeCheckConfig = field(default_factory=ShapeCheckConfig)
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    secrets: dict[str, str] = field(default_factory=dict)
    agents: list[AgentDef] = field(default_factory=list)
    assignment: AssignmentConfig = field(default_factory=AssignmentConfig)
    provider_fallbacks: dict[str, ApiFallbackConfig] = field(default_factory=dict)
    auto_api_fallback: bool = True
    review_pool_is_default: bool = False  # True when review_pool was not explicitly configured
    plan_model_is_default: bool = False  # True when plan.cli/model were not explicitly configured
    dev_profile_is_default: bool = (
        False  # True when dev_profile was auto-derived from models: (no overrides.dev)
    )
    conventions_hard: HardConventionsConfig | None = None  # None = no section = no checks
    conventions_soft: list[str] = field(default_factory=list)  # [] = no soft conventions
    conventions_advisory: AdvisoryConventionsConfig = field(
        default_factory=AdvisoryConventionsConfig
    )
    finding_classifier: FindingClassifierConfig = field(default_factory=FindingClassifierConfig)
    stuck_detection: StuckDetectionConfig = field(default_factory=StuckDetectionConfig)
    models_budget_usd: float | None = None  # set when models: key is used (v0.8 path)
    models_overrides: dict[str, Any] | None = None  # raw overrides: dict from v0.8 YAML
    # None = no registry supplied (a directly-constructed config that never
    # populated one) → consumers fall back to the built-in default registry.
    # An explicit {} is an *intentional* empty registry and is honored as-is
    # (every model key is unknown → resolution fails clearly). Keeping this
    # Optional is what lets the two cases be told apart; the load path always
    # sets a populated dict (built-in + forge.yaml overlay).
    model_registry: dict[str, AgentSpec] | None = None
    model_registry_sources: dict[str, str] = field(default_factory=dict)
    custom_models: tuple[str, ...] = ()
    diagnose: DiagnoseConfig = field(default_factory=DiagnoseConfig)

    @property
    def review_profile(self) -> ModelProfile:
        """Backward-compat: returns review_pool[0]."""
        return self.review_pool[0]
