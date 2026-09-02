"""Dataclasses and type definitions for TheForge configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .provenance import ConfigProvenance

if TYPE_CHECKING:
    from .model_duplicates import DuplicateDeclaration
    from .models import AgentDef, AgentSpec, TransportSpec

SUPPORTED_PROVIDERS = {"anthropic", "openai", "google", "deepseek"}

# Auto-approve window for a human-in-the-loop gate (4 h). Declared once so the
# typed default and the loader default (secrets.py) cannot drift apart: a
# mismatch silently shortens live gate windows for anyone who never writes the
# key into forge.yaml.
DEFAULT_HITL_TIMEOUT_SECONDS = 14400


def _normalize_transport(
    cli: str | None,
    provider: str | None,
    transport: "TransportSpec | None",
) -> "TransportSpec | None":
    """Resolve the canonical transport for a construction that may mix spellings.

    Shared by every runtime type that carries a transport (ModelProfile,
    PlanConfig, ModelRef). Three cases:

    - no transport → normalize the raw ``cli``/``provider`` pair into one;
    - a transport that already agrees with ``cli`` → keep it;
    - a transport whose CLI runner *disagrees* with ``cli`` → the caller passed a
      different raw ``cli``, so re-derive. Because ``cli`` always mirrors the
      transport after construction, a disagreement can only mean the caller
      changed it. This is what makes ``dataclasses.replace(profile,
      cli="codex")`` — or ``replace(plan, cli=None, provider="openai")`` —
      mean what it reads as, instead of silently keeping the old transport (and
      therefore dispatching to the runner the caller just replaced).

    ``cli`` wins on conflict, matching ``transport_from_raw_fields``. A
    disagreement the raw pair cannot resolve (both cleared) leaves the existing
    transport in place rather than dropping dispatch on the floor.
    """
    from .models import transport_from_raw_fields

    if transport is None:
        if not (cli or provider):
            return None
        return transport_from_raw_fields(cli, provider)
    mirrored = transport.runner if transport.kind == "cli" else None
    if cli != mirrored:
        rederived = transport_from_raw_fields(cli, provider)
        if rederived is not None:
            return rederived
    return transport


@dataclass(frozen=True)
class RecencyConfig:
    """Recency-weighting parameters for profile-derived routing rates (#1392).

    Governs how the adaptive router weights historical dev (and, once landed,
    reviewer) run outcomes when ranking models: newer runs count for more than
    stale ones so a model's *current* behavior drives routing, not a lifetime
    cumulative average (ADR-0006 clause 2.4). Consumed by
    :func:`theforge.model_profiles_read_model.get_dev_signal` via a duck-typed read of these
    fields; defaults mirror the module defaults there.

    - ``mode``: ``"exponential"`` (run-position decay), ``"window"`` (unweighted
      mean over the last ``window`` runs), or ``"off"`` (no recency weighting —
      routing falls back to the lifetime cumulative rate).
    - ``half_life_runs``: runs after which a run's weight halves (``exponential``
      mode only). Larger = longer memory.
    - ``window``: max recent outcomes consulted; also caps the ``window`` mode.
    """

    mode: str = "exponential"
    half_life_runs: float = 50.0
    window: int = 200


@dataclass(frozen=True)
class ExplorationConfig:
    """Challenger-sampling exploration parameters (#325, ADR-0006 clause 8).

    Governs the single sanctioned deviation from deterministic routing: every
    Nth run for a routing key (phase + domain + complexity band) the router may
    run a *challenger* model instead of the cached winner and learn from the
    race (the MongoDB query-planner analogy). All fields are bounded at the
    config boundary — exploration that consumes cross-run evidence must stay
    labeled, bounded, and reconstructable.

    - ``explore_every_n``: per-routing-key challenger cadence. Every Nth run for
      a key is a challenger race (subject to the per-sprint cap). Must be >= 1.
    - ``min_sample_size``: sample floor. A winner is declared for a key only
      after this many *admissible* (non-tainted) runs; below it the slot stays
      in cold-start/exploring mode routed via the static tier table. Must be
      >= 1.
    - ``per_sprint_cap``: at most this many exploration runs *per sprint* across
      ALL routing keys (not one-per-key unbounded). Must be >= 0; 0 disables
      exploration entirely.
    - ``reliability_floor``: the minimum recency-weighted success rate a
      candidate must clear before it is eligible to be *exploited* as the
      winner for a key (#2392). Winner ranking is expected-cost-to-trusted-
      completion first, so without a floor the cheapest model would win
      regardless of how often it fails. A candidate below the floor is excluded
      from exploitation no matter how cheap it is; it remains eligible for
      bounded challenger exploration. In ``[0.0, 1.0]``.
    - ``challenger_rotation``: how a challenger is drawn from the eligible
      non-winner pool. ``least_sampled`` (default) draws from the candidates
      with the FEWEST admissible runs for the key (ties broken by the injected
      RNG), which bounds each alternative's evidence-accumulation rate from
      *below* — every eligible alternative gets a race within
      ``len(pool) - 1`` cadence hits, so an incumbent's historical volume
      cannot make its own displacement impossible. ``random`` restores the
      uniform draw over the whole non-winner pool.
    - ``performance_cache_path``: a rebuildable, gitignored **derived view** of
      the per-key aggregates for operator inspection only. Never read as
      authoritative — the router always consults the native audit substrate.
    """

    explore_every_n: int = 5
    min_sample_size: int = 3
    per_sprint_cap: int = 1
    reliability_floor: float = 0.7
    challenger_rotation: str = "least_sampled"
    performance_cache_path: str = ".forge/performance_table.yaml"


@dataclass(frozen=True)
class ProviderReasoningEffortConfig:
    """Per-provider overrides for the score-driven reasoning-effort axis (#1108).

    Providers differ in how they express and honor reasoning effort, so both the
    score→effort table and the effort→token-budget map are overridable per
    provider. Keys are transport runner names (``codex``, ``google``,
    ``claude``, ``gemini``, ``anthropic``, ``openai``, ``deepseek``, ``ghaw``).
    An empty mapping means "inherit the sprint-wide table".
    """

    phase_buckets: dict[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)
    token_budgets: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ReasoningEffortConfig:
    """Score-driven reasoning-effort policy (#1108).

    The default score→effort table lives in :mod:`theforge.routing` (the routing
    policy SSOT); everything here is operator override. ``phase_buckets`` and
    ``token_budgets`` are sprint-wide defaults, ``providers`` narrows them per
    transport. Set ``enabled: false`` to leave the axis flat — the
    ``routing_decision`` block then records the axis as disabled rather than
    silently omitting it.
    """

    enabled: bool = True
    phase_buckets: dict[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)
    token_budgets: dict[str, int] = field(default_factory=dict)
    providers: dict[str, ProviderReasoningEffortConfig] = field(default_factory=dict)

    def buckets_for_provider(self, provider: str | None) -> dict[str, tuple[tuple[int, str], ...]]:
        """Return the effective per-phase bucket overrides for ``provider``.

        Provider-specific entries win per *phase*, so a provider may override
        just the dev band and inherit the sprint-wide plan/review bands.
        """
        merged = dict(self.phase_buckets)
        override = self.providers.get(provider or "")
        if override is not None:
            merged.update(override.phase_buckets)
        return merged

    def token_budget_for(self, provider: str | None, effort: str) -> int:
        """Resolve ``effort`` to a thinking-token budget for ``provider``."""
        from ..routing import DEFAULT_EFFORT_TOKEN_BUDGETS  # noqa: PLC0415

        override = self.providers.get(provider or "")
        if override is not None and effort in override.token_budgets:
            return override.token_budgets[effort]
        if effort in self.token_budgets:
            return self.token_budgets[effort]
        return DEFAULT_EFFORT_TOKEN_BUDGETS[effort]


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
    recency: RecencyConfig = field(default_factory=RecencyConfig)
    exploration: ExplorationConfig = field(default_factory=ExplorationConfig)
    # Reviewer attempt-completion routing (#1388). A reviewer whose recency-weighted
    # completion rate (did it return a parseable verdict at all?) falls below
    # ``reviewer_completion_threshold`` is sorted *after* higher-completion
    # candidates within the existing reviewer pool — but only once it has
    # accumulated ``reviewer_completion_min_runs`` attempts, so cold-start
    # reviewers are never locked out. This is a sort-after, not a filter-out: a
    # low-completion reviewer is still selected when no better candidate exists.
    reviewer_completion_threshold: float = 0.5
    reviewer_completion_min_runs: int = 5
    # Plan-reviewer mechanical value routing (#1443, ADR-0006 clause 2). Opt-in
    # (default off): only when the operator sets ``reviewer_value_enabled: true``
    # may the router consult a plan reviewer's recency-weighted P1-uniqueness rate
    # to adjust ordering. A reviewer whose admissible uniqueness rate (fraction of
    # its blocking findings no peer corroborated) falls below
    # ``reviewer_value_uniqueness_threshold`` — once it has ``reviewer_value_min_runs``
    # admissible, non-tainted, P1-bearing samples at the story's complexity band —
    # is sorted *after* higher-value candidates within the already-eligible pool.
    # Sort-after, not filter-out; explicit operator profile pins still win outright
    # (they bypass adaptive reviewer selection entirely). Latency-per-P1 is recorded
    # alongside for explainability. Below the sample floor no reordering fires and
    # ordering falls through to the existing tier / completion / health selection.
    reviewer_value_enabled: bool = False
    reviewer_value_uniqueness_threshold: float = 0.34
    reviewer_value_min_runs: int = 5
    # Code-reviewer mechanical value routing (#2156). The same mechanism as the
    # plan-review fields above, over the independently accumulated
    # ``code_review_value`` profile section, with its OWN enablement/threshold/
    # floor so the two phases can be tuned (and turned on) separately: code review
    # is where reviewer spend concentrates and where blocking findings are raised,
    # so its uniqueness distribution is not the plan-review one. Same defaults as
    # the plan-review fields — opt-in, and no reordering below the sample floor.
    code_review_value_enabled: bool = False
    code_review_value_uniqueness_threshold: float = 0.34
    code_review_value_min_runs: int = 5
    # Dev pre-promotion from cross-run capability profiles (#158, ADR-0006
    # clauses 1/2.3/2.4/4/5/7). Before the first iteration the router reads the
    # selected dev model's **recency-weighted** success rate at the story's
    # complexity band (the shared #1392 mechanism — not a lifetime cumulative
    # average). When that rate is below ``dev_promotion_threshold`` over at least
    # ``dev_promotion_min_runs`` admissible (non-tainted) runs the dev tier is
    # pre-promoted one step, before any failure. Below the sample floor no
    # promotion fires and routing falls through to the static tier. The paired
    # passive return path is recency recovery (clause 2.4/5): as old failures age
    # out the weighted rate climbs back to/above the threshold and pre-promotion
    # stops firing. Explicit forge.yaml overrides bypass the check (clause 1).
    dev_promotion_threshold: float = 0.60
    dev_promotion_min_runs: int = 5
    # Post-plan dev-tier checkpoint (#1387, absorbs #1109). When True (default), a
    # clean plan-review on a medium-complexity story may step the preflight-assigned
    # dev tier down by exactly one level (strong→mid, mid→cheap), counteracting
    # promotion creep once the plan phase has resolved the uncertainty that drove
    # the promotion. Set False for conservative routing that always honors the
    # preflight-assigned dev tier. Only the dev role is re-evaluated; explicit dev
    # overrides and locked roles always bypass the checkpoint.
    plan_tier_reduction: bool = True
    # Score-driven reasoning effort (#1108). Defaults come from the routing SSOT;
    # this block only carries operator overrides.
    reasoning_effort: ReasoningEffortConfig = field(default_factory=ReasoningEffortConfig)


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
    script: str | None = None  # path to custom notification script
    human_review_timeout_seconds: int = DEFAULT_HITL_TIMEOUT_SECONDS
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
    # ``transport`` below is the ONLY source of truth for runtime dispatch.
    # ``cli`` and ``provider`` are raw-input spellings accepted at construction
    # so callers can declare a profile the old way; __post_init__ normalizes them
    # into a canonical TransportSpec once (cli wins) and then rewrites ``cli`` to
    # mirror that transport, so the two can never disagree. Nothing downstream
    # may dispatch on the ``cli``/``provider`` pair.
    # Convention: ``provider`` is set only on API-transport profiles; on a CLI
    # profile it stays None and the provider identity is read from
    # ``provider_family``. Setting ``provider`` therefore *declares* an API
    # transport, which is what makes ``replace(profile, cli=None,
    # provider="openai")`` a complete transport switch.
    cli: str | None = None  # derived mirror of transport (CLI runner name)
    provider: str | None = None  # "anthropic", "openai", "google" (API transports)
    # Optional
    timeout_medium_seconds: int | None = None  # override for medium complexity
    timeout_large_seconds: int | None = None  # override for large complexity
    reasoning_effort: str | None = None  # "low" | "medium" | "high"; Codex/DeepSeek
    thinking_budget: int | None = None  # Google Gemini ThinkingConfig token budget
    # Whether to ask the provider for reasoning at all, where it expresses that
    # as a request parameter rather than as a distinct model name. Carried from
    # the registry entry (``invocation.reasoning_mode``) so a model banded as a
    # reasoning model is invoked in a way that actually requests reasoning
    # (#2352). None = send nothing and take the provider's default.
    reasoning_mode: str | None = None  # "enabled" | "disabled"
    review_role: str | None = None  # "correctness" | "patterns" | "edge-cases"
    phase: str | None = None  # "dev" | "preflight" | "review" | "plan_review" — set by coordinator
    base_url: str | None = None  # overrides provider's default API endpoint (Ollama etc.)
    max_tool_output_bytes: int = 51200  # cap for tool output (50KB default)
    max_iterations: int | None = (
        None  # override default agent loop iterations (None = use default)
    )
    api_fallback: TransportFallbackConfig | None = None  # CLI-only fallback to same-provider API
    github_handle: str | None = None  # optional GitHub username for reviewer assignment
    fallback_models: tuple[str, ...] = ()  # additional models to try on quota/not-found failure
    sandbox_mode: str = "workspace-write"  # CLI sandbox: "workspace-write" | "read-only" | "none"
    # Forge-owned sandbox capability preset (see config.sandbox_capabilities)
    # widening the host sandbox for stacks that cannot build inside the default
    # containment. Stamped by the coordinator from ForgeConfig.sandbox; None
    # means default containment (#1947).
    sandbox_capability_profile: str | None = None
    # Project-declared additive grants, stamped from ForgeConfig.sandbox next to
    # the preset above. Empty tuples mean "preset only", so a profile built
    # anywhere else keeps exactly today's capability set (#2038).
    sandbox_write_roots: tuple[str, ...] = ()
    sandbox_mach_services: tuple[str, ...] = ()
    registry_id: str | None = None  # canonical model registry key, when sourced from a registry
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    # The canonical TransportSpec. Populated at construction (explicitly, or
    # normalized once from the raw cli/provider spelling) and read by every
    # dispatch site thereafter.
    transport: TransportSpec | None = None
    # Per-profile stuck-detection thresholds; None falls back to the
    # ForgeConfig-level default and only fires when phase == "dev".
    stuck_detection: StuckDetectionConfig | None = None

    def __post_init__(self) -> None:
        """Normalize the raw cli/provider spelling into a canonical transport.

        This runs once, at the construction boundary — it is migration, not
        dispatch. Afterwards ``cli`` is a derived mirror of ``transport`` so a
        profile can never carry a CLI name that disagrees with the transport it
        actually dispatches through.
        """
        transport = _normalize_transport(self.cli, self.provider, self.transport)
        if transport is not self.transport:
            object.__setattr__(self, "transport", transport)
        if transport is not None:
            mirrored = transport.runner if transport.kind == "cli" else None
            if mirrored != self.cli:
                object.__setattr__(self, "cli", mirrored)

    @property
    def models(self) -> tuple[str, ...]:
        """Full preference list: primary model followed by fallbacks."""
        return (self.model,) + self.fallback_models

    @property
    def mode(self) -> str:
        """Transport kind for runtime dispatch: 'cli' or 'api'.

        Reads TransportSpec.kind — the single source of truth for dispatch. A
        profile with no transport at all (neither cli nor provider declared)
        defaults to CLI, matching the historical default.
        """
        if self.transport is not None:
            return self.transport.kind
        return "cli"

    @property
    def transport_kind(self) -> str:
        """Alias of :attr:`mode` that names what it actually reads."""
        return self.mode

    @property
    def identity_label(self) -> str:
        """``provider/model (transport)`` — the canonical identity, printable.

        Identity and transport stay visibly separate so a log line never
        collapses "OpenAI over the API" and "OpenAI over Codex" into one token.
        """
        return f"{self.provider_family or '?'}/{self.model} ({self.mode})"

    @property
    def provider_family(self) -> str | None:
        """Provider identity for *both* transports — e.g. ``anthropic`` for Claude CLI.

        ``provider`` stays None on CLI profiles (legacy consumers read it as
        "is this an API profile?"), so telemetry that wants the identity half of
        the (provider, model, transport) tuple reads this instead.
        """
        if self.provider is not None:
            return self.provider
        if self.transport is not None:
            from .models import provider_for_transport

            return provider_for_transport(self.transport)
        return None


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
    setup_timeout: int = 120  # seconds; sprint start may scale this under host contention
    # Interpreter the patient project develops against, e.g. "python3.12" or an
    # absolute path. Substituted for {forge_python} in setup_command and used to
    # validate worktree virtualenvs. Deliberately has no default: falling back to
    # the orchestrator's own interpreter is the defect this field exists to close.
    python_interpreter: str | None = None
    on_approve: str = "none"  # "merge" | "pr" | "merge-pr" | "none"; alias: "ask" → "pr"
    merge_strategy: str = "squash"  # merge | squash | rebase (used by on_approve="merge-pr")
    pr_labels: tuple[str, ...] = ()  # labels to apply when on_approve="pr" or "merge-pr"
    pr_draft: bool = False  # create PR as draft when on_approve="pr"
    ci_check_timeout_seconds: int = 300  # bounded wait for required CI checks after merge
    merge_wait_timeout_seconds: int = 3600  # bounded wait for queued merge-pr landing; expiry is
    # fail-closed: story is marked failed and dependents remain blocked


@dataclass(frozen=True)
class TransportFallbackConfig:
    """A CLI→API *transport* fallback for one provider family.

    This is not a provider-family fallback: the provider never changes. When a
    CLI transport fails in a retryable way (quota exhaustion, model-not-found),
    the run continues on the same provider's API transport with the model named
    here. :meth:`transport` returns the canonical TransportSpec that fallback
    dispatches through, so a fallback carries the same normalized identity shape
    (provider + model + transport.kind) as any other model.
    """

    provider: str
    model: str
    timeout_seconds: int | None = None
    reasoning_effort: str | None = None
    thinking_budget: int | None = None
    base_url: str | None = None
    max_iterations: int | None = None

    def transport(self) -> "TransportSpec":
        """Return the canonical API TransportSpec this fallback dispatches through."""
        from .models import transport_for

        return transport_for(self.provider, "api")


@dataclass(frozen=True)
class ModelRef:
    """Transport/model atom — reusable core of every model-backed phase config.

    Dispatch reads the embedded TransportSpec, and only that. ``cli`` and
    ``provider`` are raw-input spellings normalized into a transport once at
    construction; ``cli`` is then a derived mirror of that transport so the two
    can never disagree.

    Adding a new model-backed phase requires only a new wrapper dataclass that
    embeds a ModelRef — no transport fields are copy-pasted. This lives in
    ``types.py`` rather than ``schema.py`` because both the config-domain role
    types (``schema.py``) and the coordinator runtime types below compose it;
    ``schema.py`` re-exports it for the config-domain spelling.

    Note that :class:`TransportFallbackConfig` above deliberately does *not*
    compose a ModelRef: a ModelRef *contains* one (``api_fallback``), so the
    composition would be circular. The fallback is the leaf transport atom —
    provider + model + timeout, no cli and no budget — not a duplicated phase
    config.
    """

    model: str
    budget_usd: float
    timeout_seconds: int
    # Raw-input transport spelling; normalized into ``transport`` below.
    cli: str | None = None
    provider: str | None = None
    # Optional: preference fallbacks
    fallback_models: tuple[str, ...] = ()
    # Optional: complexity-dependent timeout overrides
    timeout_medium_seconds: int | None = None
    timeout_large_seconds: int | None = None
    # Optional: provider-specific runtime controls
    reasoning_effort: str | None = None  # "low" | "medium" | "high"; Codex only
    thinking_budget: int | None = None  # Gemini ThinkingConfig token budget
    base_url: str | None = None  # override provider endpoint (Ollama, etc.)
    max_iterations: int | None = None  # override default agent loop iterations
    max_tool_output_bytes: int = 51200  # cap for tool output (50 KB default)
    api_fallback: TransportFallbackConfig | None = None  # CLI-only same-provider API fallback
    registry_id: str | None = None  # canonical model registry key, when sourced from a registry
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    # Explicit TransportSpec — carried through derive_roles → bridge → ModelProfile
    # so runtime dispatch reads TransportSpec.kind rather than inferring from cli/provider.
    transport: TransportSpec | None = None

    def __post_init__(self) -> None:
        """Normalize the raw cli/provider spelling into a canonical transport."""
        transport = _normalize_transport(self.cli, self.provider, self.transport)
        if transport is not self.transport:
            object.__setattr__(self, "transport", transport)
        if transport is not None:
            mirrored = transport.runner if transport.kind == "cli" else None
            if mirrored != self.cli:
                object.__setattr__(self, "cli", mirrored)

    @property
    def mode(self) -> str:
        """Transport kind for dispatch: 'cli' or 'api'."""
        return self.transport.kind if self.transport is not None else "cli"

    @property
    def provider_family(self) -> str | None:
        """Provider identity for both transports (see ModelProfile.provider_family)."""
        if self.provider is not None:
            return self.provider
        if self.transport is not None:
            from .models import provider_for_transport

            return provider_for_transport(self.transport)
        return None


@dataclass(frozen=True)
class DevVerificationCommand:
    """One project-declared verification command the dev agent may request by name.

    ADR-0007: a project whose inner-loop toolchain the host sandbox cannot host
    (SwiftPM invoking ``sandbox-exec`` inside a ``sandbox-exec`` confinement, say)
    declares whole named commands here. The dev agent never gains unconfined
    execution — it requests a *name*, and the coordinator runs the command
    outside the sandbox on its behalf.

    The declared unit is deliberately a **whole command**, not a binary: fixing
    argv in project configuration is what keeps an agent-authored build input
    (``Package.swift``, a run-script phase) from being reachable through an
    arbitrary invocation the agent composes.
    """

    name: str  # the token the agent requests, e.g. "verify-watch"
    command: str  # the whole shell command run in the worktree, outside the sandbox
    timeout: int = 600  # seconds; hard wall-clock cap for one run
    output_tail_chars: int = 4000  # chars of output returned to the agent


#: Authority a validation profile's result carries. ``merge`` is the only
#: standing that establishes a gate verdict; every other run is ``advisory``
#: — a signal a phase may act on, never a verdict a merge may rest on (#2358).
VALIDATION_AUTHORITY_MERGE = "merge"
VALIDATION_AUTHORITY_ADVISORY = "advisory"
VALIDATION_AUTHORITIES: tuple[str, ...] = (
    VALIDATION_AUTHORITY_MERGE,
    VALIDATION_AUTHORITY_ADVISORY,
)

#: The closed vocabulary of declarable profile names. Closed on purpose: forge
#: selects a profile *by meaning* (complete / cheap-and-broad / scoped), so a
#: name it does not recognise is a profile it could never select. Accepting one
#: would load cleanly and then silently never run — the failure mode the
#: reviewer of this change flagged. Rejecting it at load time is the config
#: integrity boundary doing its job.
VALIDATION_PROFILE_COMPLETE = "complete"
VALIDATION_PROFILE_FAST = "fast"
VALIDATION_PROFILE_TARGETED = "targeted"
VALIDATION_PROFILE_NAMES: tuple[str, ...] = (
    VALIDATION_PROFILE_COMPLETE,
    VALIDATION_PROFILE_FAST,
    VALIDATION_PROFILE_TARGETED,
)


@dataclass(frozen=True)
class ValidationProfile:
    """One named validation profile a project declares, and what it is worth.

    The command is the project's own — forge substitutes the scoping context it
    has (``{test_target}``, ``{slug}``) and runs it. It never infers test
    framework syntax, source-to-test mappings, or package layout: what a scoped
    run means is the declared command's decision, not forge's.
    """

    name: str  # one of VALIDATION_PROFILE_NAMES
    command: str  # shell command, run in the worktree
    authority: str = VALIDATION_AUTHORITY_ADVISORY  # one of VALIDATION_AUTHORITIES

    @property
    def is_merge_authority(self) -> bool:
        """True when a result from this profile can establish a gate verdict."""
        return self.authority == VALIDATION_AUTHORITY_MERGE


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
    # ── Gate-timeout diagnostic re-run (issue #1217) ──────────────────────────
    # On a gate timeout that routes back to dev, run a serialized diagnostic
    # command with a hard per-test timeout to isolate the hanging test and
    # capture its stack trace. The built-in default command and its output
    # parsing live in theforge.gate_diagnostics (outside the stack-neutral core);
    # operators override the command via gate_diagnostic_command in forge.yaml.
    gate_diagnostic_enabled: bool = True  # run the diagnostic pass on gate timeout
    gate_diagnostic_command: str | None = None  # override; None = built-in default (see module)
    gate_diagnostic_per_test_timeout: int = 10  # seconds; per-test hard timeout in the pass
    gate_diagnostic_budget: int = 60  # seconds; hard wall-clock cap for the whole pass
    test_command: str | None = None  # canonical command for intermediate test runs in dev loop
    # Substituted for {test_target} when no task is available (baseline gate) or
    # when a task has no test_target of its own.
    default_test_target: str = "."
    pre_validate_command: str | None = None  # optional command run before dirty check
    # ── Failing-test extraction seam (issue #1738) ────────────────────────────
    # Core's built-in extractor of failing-test identifiers from gate output
    # only understands pytest's summary grammar. A project whose gate speaks a
    # different toolchain (xcodebuild, go test, cargo, …) can declare a regex
    # here so gate-failure retries are pointed at the exact failing tests its
    # gate names. The identifier is taken from a named group ``test`` if present,
    # else capture group 1, else the whole match. When unset, core falls back to
    # its pytest grammar and, if that does not match, records visibly (log +
    # audit telemetry) that extraction did not apply rather than returning a
    # silent empty list. Example: r"^\s*Test Case .* (?P<test>\w+)\(\) failed".
    failed_test_pattern: str | None = None
    # Adaptive gate-timeout scaling under sprint --parallel N. The baseline
    # gate_timeout above is the alone-time budget. When mode == "adaptive"
    # (default), sprint start scales the effective gate_timeout by host CPU
    # contention. "fixed" disables scaling so gate_timeout is a hard ceiling
    # regardless of parallelism.
    gate_cpu_cores: int | None = None  # operator hint for gate CPU demand; None => host_cores
    gate_timeout_scale: str = "adaptive"  # "adaptive" | "fixed"
    # ── Dev-phase verification capability (ADR-0007 / issue #2050) ────────────
    # Named commands the coordinator will run outside the dev sandbox when the
    # dev agent requests them by name. Empty (the default) means the capability
    # is not offered at all: no request channel is created and the dev prompt
    # says nothing about it. Declared as a tuple so ValidationConfig stays a
    # frozen, hashable value; look entries up with ``dev_verification_command``.
    dev_verification_commands: tuple[DevVerificationCommand, ...] = ()
    # Fail-closed per-iteration budget. Every request the broker handles counts,
    # accepted or refused, so a loop of malformed requests cannot buy unbounded
    # coordinator execution. Does not reset across dev transport retries — the
    # budget belongs to the iteration, not to one run_agent attempt.
    dev_verification_max_requests: int = 10
    # ── Declared validation profiles (issue #2358) ────────────────────────────
    # Empty (the default) means the project declared nothing new and keeps the
    # legacy two-slot behaviour exactly: gate_command is the authoritative
    # complete run, test_command the advisory inner-loop one. When profiles are
    # declared they are the whole vocabulary — exactly one carries merge
    # authority, and selection runs through theforge.validation_profiles.
    profiles: tuple[ValidationProfile, ...] = ()

    def dev_verification_command(self, name: str) -> DevVerificationCommand | None:
        """Return the declared verification command named ``name``, else None."""
        for entry in self.dev_verification_commands:
            if entry.name == name:
                return entry
        return None

    def profile(self, name: str) -> ValidationProfile | None:
        """Return the declared profile named ``name``, else None."""
        for entry in self.profiles:
            if entry.name == name:
                return entry
        return None

    def declared_merge_profile(self) -> ValidationProfile | None:
        """Return the declared merge-authority profile, else None.

        None means nothing was declared: the loader guarantees that a non-empty
        ``profiles`` contains exactly one merge-authority entry, so this can
        only be None on the legacy path.
        """
        for entry in self.profiles:
            if entry.is_merge_authority:
                return entry
        return None


#: ``retry.escalate_timeout_policy`` — what an *expired* escalate gate means.
#: ``preserve`` keeps today's behaviour (wait for an operator); ``apply_advice``
#: opts in to applying the advisory recommendation on expiry (#2279).
ESCALATE_TIMEOUT_PRESERVE = "preserve"
ESCALATE_TIMEOUT_APPLY_ADVICE = "apply_advice"
ESCALATE_TIMEOUT_POLICIES: tuple[str, ...] = (
    ESCALATE_TIMEOUT_PRESERVE,
    ESCALATE_TIMEOUT_APPLY_ADVICE,
)


#: The two actions the preflight complexity gate offers, and the only two an
#: operator may select or a project may configure as the no-decision action
#: (#2681). ``approve`` plans and implements the story as scoped; ``decompose``
#: returns it to be split before anything past preflight is spent on it.
PREFLIGHT_GATE_APPROVE = "approve"
PREFLIGHT_GATE_DECOMPOSE = "decompose"
PREFLIGHT_GATE_ACTIONS: tuple[str, ...] = (
    PREFLIGHT_GATE_APPROVE,
    PREFLIGHT_GATE_DECOMPOSE,
)

#: Shipped threshold for the preflight complexity gate. One below the ceiling
#: (``COMPLEXITY_SCORE_MAX`` = 10) that #2680's ``scope_exceeded`` signal already
#: flags, so the two signals do not collapse onto the same set of stories: 10
#: says "preflight judged this over scope", 9 says "preflight judged this at the
#: top of what one story should hold, so ask before spending on it".
DEFAULT_PREFLIGHT_COMPLEXITY_GATE_THRESHOLD = 9


def normalize_preflight_gate_no_decision(raw: object) -> tuple[str, str | None]:
    """Resolve ``retry.preflight_complexity_gate_no_decision`` fail-closed.

    Returns ``(action, fallback_reason)``. ``fallback_reason`` is None when the
    configured value was one of :data:`PREFLIGHT_GATE_ACTIONS`; otherwise it says
    why the value was not usable and the action is ``decompose``.

    Absent, empty, and unrecognised values all resolve to ``decompose`` rather
    than raising at config load. The gate exists to stop unapproved spend, so a
    typo must not be able to turn it into an auto-approval — and refusing to load
    the project's config at all would be a worse failure than taking the safe
    action and recording that a fallback was applied.
    """
    if raw is None:
        return PREFLIGHT_GATE_DECOMPOSE, "no value configured"
    text = str(raw).strip().lower()
    if not text:
        return PREFLIGHT_GATE_DECOMPOSE, "configured value was empty"
    if text not in PREFLIGHT_GATE_ACTIONS:
        return (
            PREFLIGHT_GATE_DECOMPOSE,
            f"configured value {text!r} is not one of {', '.join(PREFLIGHT_GATE_ACTIONS)}",
        )
    return text, None


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
    # Finite allowance of specification-gap pauses per run (#2122). A dev agent
    # that raises a gap past this bound proceeds under its own recorded
    # assumption instead of pausing again. Conservative by default: the pause is
    # cheap relative to a wasted review budget, but an unbounded one turns every
    # underspecified criterion into an operator interrupt. 0 disables the
    # channel entirely.
    max_spec_gap_pauses: int = 1
    # Preflight complexity gate (#2681). A PROCEED verdict whose complexity score
    # reaches this threshold pauses at the end of PREFLIGHT and asks the operator
    # to approve the story as scoped or return it for decomposition — before any
    # cost-bearing phase that follows preflight. Active by default; raising the
    # threshold above the highest score preflight can assign (10) disables it,
    # which is why there is no separate enable switch.
    preflight_complexity_gate_threshold: int = DEFAULT_PREFLIGHT_COMPLEXITY_GATE_THRESHOLD
    # What an *expired* gate does, as configured. Recorded raw (not normalised)
    # so the audit can name what the project actually wrote; resolve it through
    # ``normalize_preflight_gate_no_decision``, which fails closed to
    # ``decompose`` for anything absent, empty, or unrecognised.
    preflight_complexity_gate_no_decision: str = PREFLIGHT_GATE_DECOMPOSE
    max_review_cycles: int = 2  # full dev->review loops
    max_review_parse_retries: int = 2  # reviewer retries on parse/schema error per cycle
    max_diagnose_parse_retries: int = (
        2  # reformat-only retries when the diagnose agent completes its
        # investigation but emits unparseable YAML; 0 disables
    )
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
    # What an escalate gate does when it expires with no operator selection.
    #
    #   "preserve"     (default) — keep the pending checkpoint and wait for an
    #                  operator. Unchanged from the behaviour every project has
    #                  today; a project that has not opted in sees no change.
    #   "apply_advice" — apply the advisory recommendation the gate just paid for,
    #                  as if an operator had selected it deliberately. Only a
    #                  valid report with a non-elevate recommendation this run can
    #                  actually perform is applied; `elevate`, an absent
    #                  recommendation, and an advisor that never produced one all
    #                  fall back to "preserve" (#2279).
    #
    # An operator selection that arrives before expiry always governs — this
    # field only decides what an *expiry* means.
    escalate_timeout_policy: str = "preserve"  # "preserve" | "apply_advice"
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


def _default_plan_ref() -> ModelRef:
    """The PLAN phase's default transport/model atom (claude CLI, sonnet)."""
    return ModelRef(cli="claude", model="sonnet", budget_usd=0.50, timeout_seconds=600)


@dataclass(frozen=True)
class PlanConfig:
    """Configuration for the PLAN phase (pre-DEV implementation planning).

    Disabled by default; forge.yaml sets enabled: true to opt in.
    This keeps existing test configurations unaffected.

    Transport, model identity, budget and timeouts live in the composed
    :class:`ModelRef` — this type owns only what is *specific to planning*
    (``enabled``, ``validate_spec``). The scalar accessors below are read-only
    views onto ``ref``, so there is exactly one place a plan's transport can be
    written and no way for two copies to disagree. To change transport, replace
    the ref: ``replace(plan, ref=replace(plan.ref, cli=None, provider="openai"))``.
    """

    enabled: bool = False
    ref: ModelRef = field(default_factory=_default_plan_ref)
    validate_spec: bool = True  # whether to run story_validator before planning

    @classmethod
    def of(
        cls,
        *,
        enabled: bool = False,
        cli: str | None = "claude",
        model: str = "sonnet",
        provider: str | None = None,
        budget_usd: float = 0.50,
        timeout: int = 600,
        timeout_medium: int | None = None,
        timeout_large: int | None = None,
        validate_spec: bool = True,
        api_fallback: TransportFallbackConfig | None = None,
        transport: TransportSpec | None = None,
    ) -> PlanConfig:
        """Build a PlanConfig from the flat ``plan:`` spelling in forge.yaml.

        forge.yaml declares planning transport as flat scalar keys; this is the
        one place they are folded into a ModelRef, mirroring how
        ``_normalize_transport`` folds cli/provider into a TransportSpec.
        """
        return cls(
            enabled=enabled,
            ref=ModelRef(
                cli=cli,
                provider=provider,
                model=model,
                budget_usd=budget_usd,
                timeout_seconds=timeout,
                timeout_medium_seconds=timeout_medium,
                timeout_large_seconds=timeout_large,
                api_fallback=api_fallback,
                transport=transport,
            ),
            validate_spec=validate_spec,
        )

    @property
    def cli(self) -> str | None:
        """Derived mirror of the ref's transport (CLI binary name)."""
        return self.ref.cli

    @property
    def model(self) -> str:
        return self.ref.model

    @property
    def provider(self) -> str | None:
        return self.ref.provider

    @property
    def budget_usd(self) -> float:
        return self.ref.budget_usd

    @property
    def timeout(self) -> int:
        return self.ref.timeout_seconds

    @property
    def timeout_medium(self) -> int | None:
        return self.ref.timeout_medium_seconds

    @property
    def timeout_large(self) -> int | None:
        return self.ref.timeout_large_seconds

    @property
    def api_fallback(self) -> TransportFallbackConfig | None:
        return self.ref.api_fallback

    @property
    def transport(self) -> TransportSpec | None:
        """Canonical transport; dispatch reads this."""
        return self.ref.transport

    @property
    def mode(self) -> str:
        """Transport kind for dispatch: 'cli' or 'api'."""
        return self.ref.mode

    @property
    def provider_family(self) -> str | None:
        """Provider identity for both transports (see ModelProfile.provider_family)."""
        return self.ref.provider_family


@dataclass(frozen=True)
class PlanReviewConfig:
    """Configuration for the PLAN_REVIEW gate."""

    enabled: bool = False
    mode: str = "blocking"  # "blocking" | "advisory"
    timeout_seconds: int = 14400  # advisory: auto-approve after this many seconds (4 h)


def _default_plan_review_ref() -> ModelRef:
    """The plan-agent-review default transport/model atom (claude CLI, sonnet)."""
    return ModelRef(cli="claude", model="sonnet", budget_usd=0.50, timeout_seconds=300)


@dataclass(frozen=True)
class PlanAgentReviewConfig:
    """Configuration for automated agent review of plans before dev.

    When enabled, takes precedence over PlanReviewConfig (human review).
    They are mutually exclusive — agent review replaces human review.

    Supports both a single-profile format (the composed ``ref``) and a pool
    format (list of ModelProfile objects). Use the ``profiles`` property to get
    the canonical pool regardless of which format was used.

    As with :class:`PlanConfig`, the single-profile transport is not duplicated
    here: it lives in the composed :class:`ModelRef` and the scalar accessors
    below are read-only views onto it.
    """

    enabled: bool = False
    # Single-profile transport/model atom — used only when ``pool`` is empty.
    ref: ModelRef | None = field(default_factory=_default_plan_review_ref)
    # Pool format — populated by load_forge_yaml when pool: key is present.
    pool: list[ModelProfile] = field(default_factory=list)
    min_reviewers: int = 1

    @classmethod
    def of(
        cls,
        *,
        enabled: bool = False,
        cli: str | None = "claude",
        provider: str | None = None,
        model: str = "sonnet",
        budget_usd: float = 0.50,
        timeout: int = 300,
        pool: list[ModelProfile] | None = None,
        api_fallback: TransportFallbackConfig | None = None,
        min_reviewers: int = 1,
    ) -> PlanAgentReviewConfig:
        """Build from the flat ``plan_agent_review:`` spelling in forge.yaml."""
        return cls(
            enabled=enabled,
            ref=ModelRef(
                cli=cli,
                provider=provider,
                model=model,
                budget_usd=budget_usd,
                timeout_seconds=timeout,
                api_fallback=api_fallback,
            ),
            pool=list(pool) if pool else [],
            min_reviewers=min_reviewers,
        )

    @property
    def cli(self) -> str | None:
        return self.ref.cli if self.ref is not None else None

    @property
    def provider(self) -> str | None:
        return self.ref.provider if self.ref is not None else None

    @property
    def model(self) -> str:
        return self.ref.model if self.ref is not None else "sonnet"

    @property
    def budget_usd(self) -> float:
        return self.ref.budget_usd if self.ref is not None else 0.50

    @property
    def timeout(self) -> int:
        return self.ref.timeout_seconds if self.ref is not None else 300

    @property
    def api_fallback(self) -> TransportFallbackConfig | None:
        return self.ref.api_fallback if self.ref is not None else None

    @property
    def profiles(self) -> list[ModelProfile]:
        """Return pool list, or a single-profile pool built from ``ref``."""
        if self.pool:
            return self.pool
        ref = self.ref
        if ref is None or (not ref.cli and not ref.provider):
            return []
        from .defaults import API_PROVIDER_DEFAULT_TOOLS, DEFAULT_INVESTIGATION_TOOLS
        from .model_identity import PHASE_PLAN_REVIEW

        allowed_tools = (
            API_PROVIDER_DEFAULT_TOOLS
            if ref.provider and ref.provider in SUPPORTED_PROVIDERS
            else DEFAULT_INVESTIGATION_TOOLS
        )
        return [
            ModelProfile(
                name="plan-review",
                cli=ref.cli,
                provider=ref.provider,
                model=ref.model or "sonnet",
                budget_usd=ref.budget_usd,
                timeout_seconds=ref.timeout_seconds,
                allowed_tools=allowed_tools,
                api_fallback=ref.api_fallback,
                phase=PHASE_PLAN_REVIEW,
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
class DevConfig:
    """Non-model behavior controls for the DEV phase."""

    p2_policy: str = "in_scope"  # "in_scope" | "all" | "p1_only"


@dataclass(frozen=True)
class SandboxConfig:
    """The project's sandbox capability declaration.

    ``capability_profile`` selects a forge-owned preset by name. ``write_roots``
    and ``mach_services`` are project-authored grants **added** to whatever that
    preset grants (or granted on their own, with no preset selected), because
    the shipped presets cannot anticipate every toolchain (#2038). There is no
    field here that subtracts from a preset, disables containment, or grants
    allow-default — the declaration only ever enumerates.
    """

    capability_profile: str | None = None
    write_roots: tuple[str, ...] = ()
    mach_services: tuple[str, ...] = ()


@dataclass(frozen=True)
class SprintBatchConfig:
    """Cost-aware batch-group limits (``sprint.batch`` in forge.yaml).

    Batch groups pack small *independent* stories into a single dev assignment
    to amortise per-story orchestration overhead. They are a separate primitive
    from conflict bundles (which exist to avoid merge pain) and from DAG
    dependencies (which encode ordering).

    Batching is **off by default**. It is not automatically cheaper — a combined
    prompt can exceed what one dev agent holds well, tests can broaden, and
    review can get harder — so a project opts in rather than discovering a
    changed dispatch shape mid-sprint. ``max_stories: 1`` is the off switch (a
    group of one is not a batch); set it to 2 or more to enable::

        sprint:
          batch:
            max_stories: 2
            max_complexity_budget: 2
            max_touched_files: 6
    """

    max_stories: int = 1
    max_complexity_budget: int = 2
    #: Upper bound on the *combined* distinct likely_files a group may touch.
    #: A group whose members collectively claim a wide footprint is not a set of
    #: small independent fixes, whatever preflight labelled the complexity.
    max_touched_files: int = 6


@dataclass(frozen=True)
class SprintConfig:
    """Project-level sprint defaults from forge.yaml.

    ``post_sprint_triage`` opts a project into a headless ``forge triage``
    proposal pass after a sprint reaches its terminal result. It is **off by
    default** and stays a proposal: the pass writes a pending operator decision
    and never ratifies, so nothing is applied to a tracker without a person. A
    failure in the pass is reported and never changes the sprint's outcome.
    """

    max_parallel: int = 1
    worker_timeout_seconds: int = 3600
    batch: SprintBatchConfig = field(default_factory=SprintBatchConfig)
    post_sprint_triage: bool = False


@dataclass(frozen=True)
class ShapeCheckConfig:
    """Shared shape_check settings used by the #811 Action and sprint entry.

    ``classifier`` selects the shape-check classifier mode: ``heuristic`` for
    deterministic stdlib-only checks, or a provider name (e.g. ``claude``)
    when an LLM-assisted classifier should be used. Sprint entry falls back
    to ``heuristic`` when the configured provider is unavailable at sprint
    time (e.g. no credentials, no SDK installed, no network).

    ``stuck_issue_threshold`` is the number of times an issue may be blocked by
    the same shape-gate skip code across runs before the sprint postmortem flags
    it as a stuck-issue pattern (issue #1453). Default 3 would have surfaced
    #1135 and #1405 weeks before manual log-walking did.
    """

    classifier: str = "heuristic"
    stuck_issue_threshold: int = 3


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
class KnowledgeConfig:
    """Gates for the run-knowledge pipeline (docs/plans/knowledge-capture.md).

    Generation and consumption are separate knobs with separate readiness
    points, and both ship disabled. ``run_summaries`` turns on the post-DONE
    evidence-backed run summary (Layer 2), so a corpus can accumulate while
    nothing consumes it.

    ``prior_run_context`` gates Layer 3 injection of those summaries into future
    agent prompts. When on, ``ContextAssembler`` offers indexed summaries to the
    plan, dev, and review phases as advisory, droppable context items — never to
    preflight, whose output drives coordinator control flow (ADR-0002 clause 5),
    and never without an admissible verdict in the knowledge index. It is
    deliberately not advertised in the generated starter config: it is only
    useful once a project has a corpus of summaries to draw on.

    ``invariant_context`` gates the #1875 invariant-index spike: when on,
    ``ContextAssembler`` offers project invariants marked in the project's own
    authoritative Markdown to the plan, dev, and review phases — never to
    preflight, for the same ADR-0002 clause 5 reason. ``invariant_sources`` is
    the list of Markdown globs the deterministic extractor scans; it holds
    *source locations only*, never invariant prose, because the marked document
    stays the single source of truth.

    The default glob is every Markdown file in the repository, deliberately: a
    default naming particular directories would presume one project's layout,
    and this feature has to work on any target project that marks its own rules.
    The extractor skips derived and vendored trees and only parses files that
    actually contain a marker, so the broad default costs a substring scan.
    Projects with a settled documentation layout can narrow it here.

    ``ref`` directly names the tool-free API model that authors durable
    knowledge summaries. When omitted, summaries inherit ``plan.ref`` only if
    the planner already dispatches over API transport. CLI planner refs do not
    consult ``transport_fallback`` for summary authoring: configure
    ``knowledge.ref`` explicitly instead so the role's model is visible at the
    key whose job is to declare it.
    """

    run_summaries: bool = False
    prior_run_context: bool = False
    ref: ModelRef | None = None
    invariant_context: bool = False
    invariant_sources: tuple[str, ...] = ("**/*.md",)


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
                           (default behavior for bug-format issues: the sprint
                           shape gate reads the issue body for the diagnosis
                           artifact, so this is the destination that leaves a
                           bug fix-ready after diagnose)
      - ``comment``      — post the artifact as a new GitHub issue comment
                           (default behavior for non-bug issues when the
                           configured/default destination is ``body_section``,
                           so diagnose does not introduce plan-in-body blockers)
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
    dev: DevConfig = field(default_factory=DevConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    sprint: SprintConfig = field(default_factory=SprintConfig)
    shape_check: ShapeCheckConfig = field(default_factory=ShapeCheckConfig)
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    secrets: dict[str, str] = field(default_factory=dict)
    agents: list[AgentDef] = field(default_factory=list)
    assignment: AssignmentConfig = field(default_factory=AssignmentConfig)
    transport_fallbacks: dict[str, TransportFallbackConfig] = field(default_factory=dict)
    auto_transport_fallback: bool = True
    review_pool_is_default: bool = False  # True when review_pool was not explicitly configured
    plan_model_is_default: bool = False  # True when plan.cli/model were not explicitly configured
    dev_profile_is_default: bool = (
        # True when dev routing is active: dev_profile was auto-derived from
        # models: and no model-constraining overrides.dev pinned it. A
        # resource-only overrides.dev (timeouts/budget) leaves this True. See #1764.
        False
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
    # Per-field provenance for the merged registry: {canonical_id: {field:
    # "builtin" | "forge.yaml"}}. Entry-level ``model_registry_sources`` above
    # says which file an entry came from; this says which file supplied each
    # field of it, which is what makes a project declaration that only refines
    # part of a shipped definition readable rather than opaque. Empty for a
    # directly-constructed config that never went through load_config().
    model_registry_field_sources: dict[str, dict[str, str]] = field(default_factory=dict)
    # Canonical identities defined in *both* the shipped catalog and forge.yaml,
    # with how the two definitions differ. A duplicate declaration is not assumed
    # inert: a project declaration re-derives its own pricing attribution, and
    # attribution gates the prices routing reads, so an apparently redundant
    # declaration can still change selection. Recorded here (and therefore in the
    # resolved-config digest) so the difference is a stated fact rather than
    # something an operator discovers by deleting the declaration.
    model_registry_duplicates: tuple[DuplicateDeclaration, ...] = ()
    custom_models: tuple[str, ...] = ()
    diagnose: DiagnoseConfig = field(default_factory=DiagnoseConfig)
    # Identity of the configuration this object represents (#2056). Populated by
    # load_config(); None for a directly-constructed config that never came from
    # a file, which is exactly the "identity unknown" case the audit record must
    # be able to state rather than fabricate.
    provenance: ConfigProvenance | None = None

    def __post_init__(self) -> None:
        """Canonicalize the selected model list at the runtime type boundary.

        ``models`` holds model identities, and every consumer (role derivation,
        escalation chains, the adaptive pool) looks them up in the registry.
        Normalizing here means a legacy provider-prefix spelling cannot reach a
        consumer regardless of whether this config came from ``load_config`` or
        was constructed directly.
        """
        if self.models is not None:
            from .models import normalize_model_key

            registry = self.model_registry
            normalized = [
                key if registry is not None and key in registry else normalize_model_key(key)
                for key in self.models
            ]
            if normalized != self.models:
                object.__setattr__(self, "models", normalized)

    @property
    def review_profile(self) -> ModelProfile:
        """Backward-compat: returns review_pool[0]."""
        return self.review_pool[0]
