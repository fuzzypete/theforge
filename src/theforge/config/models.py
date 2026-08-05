"""Model registry: AgentSpec / TransportSpec / ModelInfo and model-related helpers.

The registry is the single source of truth for what agents the system knows about.
Each registry entry is expressed as an `AgentSpec` — a canonical identity
(``provider`` + ``model`` + ``TransportSpec.kind``) paired with a separate
:class:`RoutingPolicy` holding the routing/selection knobs (tier, capability,
cost band, dev capability, phase eligibility). `ModelInfo` is retained as a
legacy flat view derived from these specs so existing callers continue to work
while the codebase migrates.

Transport is first class: ``cli``/``api`` is never encoded in a provider-like
prefix (``openai-api/``, ``gemini-cli/``). Those spellings survive only as raw
input aliases (see :data:`_LEGACY_MODEL_KEY_ALIASES` and
:func:`normalize_model_key`) and are normalized to canonical identities the
moment they are read.

Per-MTok pricing on a registry entry is a *routing input*: it sets the cost band
and breaks equal-band ties. A price may only act as one when it can be attributed
to the identity it describes — see :attr:`AgentSpec.pricing_provenance` and
:func:`price_tiebreak_signal_for`. An entry whose identity is a vendor shorthand
that the vendor's own tooling resolves to some other concrete version at
invocation time carries no attribution, and its literal price is treated as
unknown rather than trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, TypeVar, overload

from .pricing import (
    COST_BAND_BASIS_DECLARED_POLICY,
    COST_BAND_BASIS_VENDOR_TIER,
    PRICING_PROVENANCE_LOCAL_ENDPOINT,
    AttributablePricing,
    price_tiebreak_signal_for,
    resolve_cost_band_basis,
)

# Re-exported: these moved to config/pricing.py, but callers (and the #1617
# regression suite) import them from the registry module.
from .pricing import custom_model_cost_rank as custom_model_cost_rank
from .pricing import price_tiebreak_signal as price_tiebreak_signal
from .types import (
    AssignmentConfig,
    ExplorationConfig,
    ModelProfile,
    ModelRef,
    PlanConfig,
    ProviderReasoningEffortConfig,
    ReasoningEffortConfig,
    RecencyConfig,
    TransportFallbackConfig,
)

_ProfileT = TypeVar("_ProfileT", ModelProfile, ModelRef, PlanConfig)

TRANSPORT_KINDS: frozenset[str] = frozenset({"cli", "api"})


@dataclass(frozen=True)
class TransportSpec:
    """How to execute an agent.

    kind: "cli" — invoke via a locally installed binary (Claude/Codex/Gemini CLI)
          "api" — invoke via a provider SDK (Anthropic/OpenAI/Google/DeepSeek)
    runner: the logical runner module key (e.g. "claude", "codex", "gemini",
            "anthropic", "openai", "google", "deepseek"). For CLI transports this
            identifies both the runner and the binary; for API transports this
            identifies the adapter to dispatch to. It is *derived* from
            ``(provider, kind)`` by :func:`transport_for` wherever that tuple has
            exactly one valid executor; only genuinely ambiguous tuples require an
            explicit runner.
    executable: only meaningful for kind="cli" — the binary name on PATH.
    """

    kind: str  # "cli" | "api"
    runner: str
    executable: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in TRANSPORT_KINDS:
            raise ValueError(f"TransportSpec.kind must be 'cli' or 'api', got {self.kind!r}")
        if self.kind == "cli" and not self.executable:
            raise ValueError("TransportSpec(kind='cli') requires an executable name")
        if self.kind == "api" and self.executable is not None:
            raise ValueError("TransportSpec(kind='api') must not set an executable")


_DEFAULT_PHASE_ELIGIBILITY: frozenset[str] = frozenset({"preflight", "dev", "plan", "review"})


@dataclass(frozen=True)
class RoutingPolicy:
    """Routing/selection policy for a model — deliberately *not* its identity.

    Two registry entries with the same ``(provider, model, transport.kind)`` are
    the same model no matter how their routing policy differs. Keeping tier,
    capability, cost band, dev capability and phase eligibility here (rather than
    inline on :class:`AgentSpec`) is what makes that separation checkable.
    """

    tier: str  # "cheap" | "fast" | "strong" (semantic speed/latency band)
    capability: int  # 1-10 relative capability score
    cost_rank: int  # 1=cheap, 2=moderate, 3=expensive
    dev_capable: bool = True  # whether this agent is allowed to own the dev role
    phase_eligibility: frozenset[str] = _DEFAULT_PHASE_ELIGIBILITY
    # What ``cost_rank`` is derived from: ``price:<provenance>`` when the band is
    # the entry's own attributable price band, otherwise a COST_BAND_BASIS_*
    # marker naming a non-price basis. The band is a routing input in its own
    # right, so it may not be a bare number copied off an untraceable literal —
    # see config/pricing.resolve_cost_band_basis.
    cost_rank_basis: str | None = None


# ── Runner derivation from (provider, transport.kind) ────────────────────
#
# The (provider, kind) tuple determines the executor wherever it is unambiguous.
# Only tuples with more than one valid executor need an explicit runner — today
# that is the gh-aw backend (ADR-0004), which reaches a remote agent through the
# `gh` binary and therefore collides with the provider's own native CLI.
_CLI_RUNNER_BY_PROVIDER: dict[str, tuple[str, str]] = {
    # provider -> (runner, executable)
    "anthropic": ("claude", "claude"),
    "openai": ("codex", "codex"),
    "google": ("gemini", "gemini"),
}
_API_RUNNER_BY_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "deepseek": "deepseek",
}
# Runners that must be named explicitly because (provider, kind) alone does not
# select them: runner -> executable.
_EXPLICIT_CLI_RUNNERS: dict[str, str] = {
    "ghaw": "gh",  # gh-aw (GitHub Agentic Workflows) — dispatched via the `gh` binary
}


def transport_for(provider: str, kind: str, runner: str | None = None) -> TransportSpec:
    """Build the canonical TransportSpec for a ``(provider, kind)`` identity.

    The runner/executor is derived from ``(provider, kind)`` when that tuple has
    exactly one valid executor. ``runner`` only needs to be supplied for tuples
    that admit more than one (currently the gh-aw backend).
    """
    if kind not in TRANSPORT_KINDS:
        raise ValueError(f"transport kind must be 'cli' or 'api', got {kind!r}")
    if runner is not None and runner in _EXPLICIT_CLI_RUNNERS:
        if kind != "cli":
            raise ValueError(f"runner {runner!r} is a CLI runner but kind is {kind!r}")
        return TransportSpec(kind="cli", runner=runner, executable=_EXPLICIT_CLI_RUNNERS[runner])
    if kind == "cli":
        derived = _CLI_RUNNER_BY_PROVIDER.get(provider)
        if derived is None:
            raise ValueError(
                f"No CLI runner for provider {provider!r}. "
                f"Providers with a CLI: {sorted(_CLI_RUNNER_BY_PROVIDER)}"
            )
        cli_runner, executable = derived
        if runner is not None and runner != cli_runner:
            raise ValueError(
                f"runner {runner!r} is not valid for ({provider!r}, 'cli'); "
                f"expected {cli_runner!r}"
            )
        return TransportSpec(kind="cli", runner=cli_runner, executable=executable)
    api_runner = _API_RUNNER_BY_PROVIDER.get(provider)
    if api_runner is None:
        raise ValueError(
            f"No API adapter for provider {provider!r}. "
            f"Providers with an API adapter: {sorted(_API_RUNNER_BY_PROVIDER)}"
        )
    if runner is not None and runner != api_runner:
        raise ValueError(
            f"runner {runner!r} is not valid for ({provider!r}, 'api'); expected {api_runner!r}"
        )
    return TransportSpec(kind="api", runner=api_runner)


# Canonical transport objects — referenced by AGENT_REGISTRY entries.
_TRANSPORT_CLAUDE_CLI = transport_for("anthropic", "cli")
_TRANSPORT_CODEX_CLI = transport_for("openai", "cli")
_TRANSPORT_GEMINI_CLI = transport_for("google", "cli")
_TRANSPORT_GHAW_CLI = transport_for("anthropic", "cli", runner="ghaw")
_TRANSPORT_ANTHROPIC_API = transport_for("anthropic", "api")
_TRANSPORT_OPENAI_API = transport_for("openai", "api")
_TRANSPORT_GOOGLE_API = transport_for("google", "api")
_TRANSPORT_DEEPSEEK_API = transport_for("deepseek", "api")


@dataclass(frozen=True)
class AgentSpec(AttributablePricing):
    """First-class description of an agent: canonical identity + routing policy.

    Identity is exactly ``(provider, model, transport.kind)`` — see
    :func:`canonical_id_for_spec`. ``base_url`` is endpoint *metadata* on that
    identity (this is how a local OpenAI-compatible model is expressed: an API
    transport pointed at a localhost endpoint), and pricing is accounting
    metadata. Neither participates in identity. Routing knobs live in
    :attr:`routing`; the flat properties below are read-only conveniences for
    callers that only need one of them.
    """

    provider: str  # "anthropic" | "openai" | "google" | "deepseek"
    model: str  # model identifier (e.g. "sonnet", "gpt-5.4", "deepseek-reasoner")
    transport: TransportSpec
    routing: RoutingPolicy
    base_url: str | None = None  # endpoint metadata (local/OpenAI-compatible servers)
    tool_mode: str = "auto"  # "auto" = follow transport default; reserved for future use
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    # What the prices above are attributed to (concrete billed identity or a
    # PRICING_PROVENANCE_* marker). None = unattributed: routing treats the
    # figures as unknown. See _AttributablePricing.
    pricing_provenance: str | None = None

    @property
    def tier(self) -> str:
        return self.routing.tier

    @property
    def capability(self) -> int:
        return self.routing.capability

    @property
    def cost_rank(self) -> int:
        return self.routing.cost_rank

    @property
    def dev_capable(self) -> bool:
        return self.routing.dev_capable

    @property
    def phase_eligibility(self) -> frozenset[str]:
        return self.routing.phase_eligibility


# ── Raw-input alias boundary ─────────────────────────────────────────────
#
# Everything below this comment exists only to translate *raw* operator input
# into a canonical identity. Nothing downstream of config loading may consult it.

# Provider-like tokens accepted in raw `models.custom` declarations. These are
# aliases, not providers: each maps to a real provider family plus a transport
# kind, and is normalized away immediately.
_MODEL_OVERLAY_PROVIDER_MAP: dict[str, tuple[str, str]] = {
    # alias token -> (provider family, transport kind)
    "anthropic": ("anthropic", "cli"),
    "claude": ("anthropic", "cli"),
    "openai": ("openai", "cli"),
    "openai-api": ("openai", "api"),
    "deepseek": ("deepseek", "api"),
    "google": ("google", "api"),
    "gemini": ("google", "api"),
    "gemini-cli": ("google", "cli"),
}

# Legacy CLI binary names accepted in raw profile/plan blocks, mapped to the
# provider family whose CLI they are. Used only to migrate a raw ``cli:`` key
# into a canonical transport at parse time.
_LEGACY_CLI_TO_PROVIDER: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
    "ghaw": "anthropic",
}


def provider_for_cli_runner(runner: str | None) -> str | None:
    """Map a CLI runner name (``claude``/``codex``/``gemini``) to its provider family."""
    if not runner:
        return None
    return _LEGACY_CLI_TO_PROVIDER.get(runner)


def provider_for_transport(transport: TransportSpec) -> str | None:
    """Return the provider family a TransportSpec belongs to.

    The inverse of :func:`transport_for`. Telemetry uses it to recover the
    identity half (provider) from a CLI transport, where ``ModelProfile.provider``
    is conventionally left unset.
    """
    if transport.kind == "api":
        return next(
            (p for p, runner in _API_RUNNER_BY_PROVIDER.items() if runner == transport.runner),
            None,
        )
    for provider, (runner, _executable) in _CLI_RUNNER_BY_PROVIDER.items():
        if runner == transport.runner:
            return provider
    return _LEGACY_CLI_TO_PROVIDER.get(transport.runner)


def mirror_fields_for_transport(
    transport: TransportSpec | None,
    cli: str | None,
    provider: str | None,
) -> tuple[str | None, str | None]:
    """Return the ``(cli, provider)`` pair that mirrors ``transport``.

    Once a parse-boundary helper has resolved which transport an override lands
    on, the legacy spelling has to be rewritten to match it — otherwise a stale
    ``cli`` inherited from the profile being overridden survives into the
    constructor and :func:`types._normalize_transport` re-derives the *old*
    transport from it, silently discarding the switch.

    Following the ``ModelProfile`` convention: a CLI transport mirrors as
    ``(runner, None)`` and an API transport as ``(None, provider_family)``. When
    the transport is unresolvable the raw pair is returned untouched so the
    unresolved value still surfaces in error messages.
    """
    if transport is None:
        return cli, provider
    if transport.kind == "cli":
        return transport.runner, None
    return None, provider_for_transport(transport)


def transport_from_raw_fields(
    cli: str | None,
    provider: str | None,
) -> TransportSpec | None:
    """Normalize a raw ``cli``/``provider`` pair into a canonical TransportSpec.

    **Migration only.** ``cli`` and ``provider`` are raw-input spellings; runtime
    dispatch reads :class:`TransportSpec` and never this pair. This helper exists
    so the parsing boundary can turn old config (and legacy in-process
    constructions) into a canonical transport once, at the edge.

    ``cli`` wins when both are supplied — a declaration that names a CLI binary
    is dispatched via that binary.
    """
    if cli and cli in _LEGACY_CLI_TO_PROVIDER:
        runner = cli if cli in _EXPLICIT_CLI_RUNNERS else None
        return transport_for(_LEGACY_CLI_TO_PROVIDER[cli], "cli", runner=runner)
    if provider and provider in _API_RUNNER_BY_PROVIDER:
        return transport_for(provider, "api")
    return None


@dataclass(frozen=True)
class ModelInfo(AttributablePricing):
    """Legacy flat view derived from an AgentSpec.

    Retained for backward compatibility: existing callers read `.cli`, `.provider`,
    `.model`, `.tier`, `.capability`, `.cost_rank`, `.dev_capable`. New code should
    prefer `AgentSpec` + `TransportSpec` directly.

    `phase_eligibility` and `transport` are carried through the projection so role
    derivation can filter candidates per-role and so runtime dispatch can read the
    explicit TransportSpec rather than inferring transport from cli/provider.
    """

    cli: str | None  # "claude", "codex", "gemini"; None for API-backed providers
    model: str  # model identifier for the CLI
    tier: str  # "fast" or "strong"
    capability: int  # relative capability score (1-10)
    cost_rank: int  # 1=cheap, 2=moderate, 3=expensive
    dev_capable: bool = True  # False for models whose CLI doesn't support dev tools
    provider: str | None = None  # API transport, mutually exclusive with cli
    phase_eligibility: frozenset[str] = _DEFAULT_PHASE_ELIGIBILITY
    registry_id: str | None = None  # canonical model registry key
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    pricing_provenance: str | None = None  # carried from the spec; None = unattributed
    transport: TransportSpec | None = None  # the canonical TransportSpec this view was built from
    # Provider family for *both* transports — unlike ``provider`` (which stays
    # None for CLI entries so legacy consumers keep their meaning), this is the
    # identity half of the (provider, model, transport.kind) tuple.
    provider_family: str | None = None
    base_url: str | None = None  # endpoint metadata carried from the registry entry


_CLI_TO_PROVIDER: dict[str, str] = dict(_LEGACY_CLI_TO_PROVIDER)


@dataclass(frozen=True)
class AgentDef(AttributablePricing):
    """An agent available in the pool for adaptive model assignment."""

    name: str
    provider: str | None  # "anthropic", "openai", etc. — None for CLI agents
    model: str
    budget_usd: float
    timeout_seconds: int
    tier: str  # "cheap" | "mid" | "strong"
    cli: str | None = None  # "claude", "codex", etc. — set for CLI agents
    api_fallback: TransportFallbackConfig | None = None
    strengths: tuple[str, ...] = ()
    # Canonical transport carried from the registry so profiles built from this
    # pool entry never have to re-derive dispatch from the cli/provider pair.
    transport: TransportSpec | None = None
    base_url: str | None = None  # endpoint metadata (local OpenAI-compatible servers)
    registry_id: str | None = None
    registry_source: str = "builtin"
    # Per-MTok pricing carried through from the registry so ranking helpers can
    # break equal-tier ties by real cost rather than pool list order. See
    # price_tiebreak_signal_for and issue #1617. ``pricing_provenance`` travels
    # with the figures so the tie-break can tell a traceable price from a
    # literal nothing can vouch for.
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    pricing_provenance: str | None = None

    @property
    def effective_provider(self) -> str | None:
        """Return the agent's provider for routing/display purposes.

        For API agents this is the explicit ``provider`` field. For CLI-only
        agents (where ``provider`` is None), this derives the provider from the
        ``cli`` binary: ``claude`` → ``anthropic``, ``codex`` → ``openai``,
        ``gemini`` → ``google``. The raw ``provider`` field stays None for CLI
        agents so dispatch (which reads ``ModelProfile.provider`` to choose
        between CLI and API runners) is unaffected.
        """
        if self.provider is not None:
            return self.provider
        if self.cli is not None:
            return _CLI_TO_PROVIDER.get(self.cli)
        return None

    def to_model_profile(self, *, allowed_tools: tuple[str, ...] = ()) -> ModelProfile:
        """Convert to a ModelProfile for use in coordinator config."""
        return ModelProfile(
            name=self.name,
            cli=self.cli,
            provider=self.provider,
            transport=self.transport,
            base_url=self.base_url,
            model=self.model,
            budget_usd=self.budget_usd,
            timeout_seconds=self.timeout_seconds,
            allowed_tools=allowed_tools,
            api_fallback=self.api_fallback,
            registry_id=self.registry_id,
            registry_source=self.registry_source,
        )


# ── Canonical model identity ─────────────────────────────────────────
#
# The canonical model ID is the single key under which all profile data,
# assignment history and audit records accumulate. Two registrations resolve
# to the same canonical ID iff they refer to the same actual provider, model
# and transport.kind. Format: ``<provider>/<model>/<transport.kind>``
# (e.g. ``anthropic/sonnet/cli``, ``openai/gpt-5.4/api``).


def canonical_model_id(provider: str, model: str, transport_kind: str) -> str:
    """Build the canonical ID from its three constituent parts."""
    return f"{provider}/{model}/{transport_kind}"


def canonical_id_for_spec(spec: AgentSpec) -> str:
    """Return the canonical ID for an :class:`AgentSpec`."""
    return canonical_model_id(spec.provider, spec.model, spec.transport.kind)


def is_canonical_model_id(value: str | None) -> bool:
    """Return True if ``value`` already follows the canonical format."""
    if not value or not isinstance(value, str):
        return False
    parts = value.split("/")
    return len(parts) == 3 and bool(parts[0]) and bool(parts[1]) and parts[2] in ("cli", "api")


# ── Agent registry ────────────────────────────────────────────────────
#
# Keys are canonical model identities: ``<provider>/<model>/<transport.kind>``.
# CLI-vs-API is carried by the transport half of that key, never by a
# provider-like prefix. Adding a model is a single entry here — no
# role-derivation, profile, or runner-dispatch edits required.

# Default endpoint for OpenAI-compatible models served locally (Ollama et al).
# Locality is endpoint metadata on an ordinary API transport; there is no
# ``local`` provider and no ``local`` transport kind.
LOCAL_OPENAI_COMPATIBLE_BASE_URL = "http://localhost:11434/v1"


def _entry(
    provider: str,
    model: str,
    kind: str,
    *,
    tier: str,
    capability: int,
    cost_rank: int,
    dev_capable: bool = True,
    phases: frozenset[str] = _DEFAULT_PHASE_ELIGIBILITY,
    runner: str | None = None,
    base_url: str | None = None,
    input_cost_per_mtok: float | None = None,
    output_cost_per_mtok: float | None = None,
    pricing_provenance: str | None = None,
    cost_rank_basis: str | None = None,
) -> tuple[str, AgentSpec]:
    """Build a ``(canonical_id, AgentSpec)`` registry pair.

    ``pricing_provenance`` names the concrete billed identity the price figures
    were recorded for. Omitting it marks the figures unattributed, which is what
    entries identified by a vendor shorthand must do: the shorthand resolves to
    some other concrete version at invocation time, so nothing ties the stored
    literal to what is actually billed.

    ``cost_rank`` is a routing input too, so it is held to the same standard:
    omit ``cost_rank_basis`` only when the band *is* this entry's attributable
    price band (it is then attributed to that price automatically). Every other
    band — including every band on an entry whose price is unattributed — must
    name its non-price basis, or construction raises.
    """
    transport = transport_for(provider, kind, runner=runner)
    basis = resolve_cost_band_basis(
        cost_rank,
        input_cost_per_mtok=input_cost_per_mtok,
        output_cost_per_mtok=output_cost_per_mtok,
        pricing_provenance=pricing_provenance,
        declared_basis=cost_rank_basis,
    )
    spec = AgentSpec(
        provider=provider,
        model=model,
        transport=transport,
        routing=RoutingPolicy(
            tier=tier,
            capability=capability,
            cost_rank=cost_rank,
            dev_capable=dev_capable,
            phase_eligibility=phases,
            cost_rank_basis=basis,
        ),
        base_url=base_url,
        input_cost_per_mtok=input_cost_per_mtok,
        output_cost_per_mtok=output_cost_per_mtok,
        pricing_provenance=pricing_provenance,
    )
    return canonical_id_for_spec(spec), spec


AGENT_REGISTRY: dict[str, AgentSpec] = dict(
    [
        # ── Anthropic ────────────────────────────────────────────────
        #
        # ``sonnet``/``opus`` are Claude-CLI shorthands, not billed identities:
        # the CLI resolves each to whichever concrete version it currently ships
        # (the vendor bills ``claude-sonnet-4-6`` / ``claude-opus-4-6``-style
        # names — see runners/schema_utils.PRICING_TABLE). The entry identity can
        # therefore move while the literal below does not, so these figures carry
        # no pricing_provenance: they are indicative only and routing ignores
        # them. Re-attributing them requires pinning the entry to the concrete
        # version the price is true of.
        #
        # Their cost bands are not derived from those literals either — they
        # restate the vendor's own tier naming, which is what the shorthand
        # *means* and stays true when it resolves to a new version: ``opus`` is
        # whatever Anthropic currently sells as its flagship (strong band),
        # ``sonnet`` whatever it sells as the mid-priced workhorse (cheap band,
        # as the fleet's baseline). Note the bands already disagree with the
        # literals — 3.00/15.00 would band sonnet at 2 — so the figures could
        # move to any value without moving either band.
        _entry(
            "anthropic",
            "sonnet",
            "cli",
            tier="fast",
            capability=7,
            cost_rank=1,
            input_cost_per_mtok=3.00,
            output_cost_per_mtok=15.00,
            cost_rank_basis=COST_BAND_BASIS_VENDOR_TIER,
        ),
        _entry(
            "anthropic",
            "opus",
            "cli",
            tier="strong",
            capability=10,
            cost_rank=3,
            input_cost_per_mtok=15.00,
            output_cost_per_mtok=75.00,
            cost_rank_basis=COST_BAND_BASIS_VENDOR_TIER,
        ),
        # ── OpenAI (Codex CLI) ───────────────────────────────────────
        #
        # Unlike the Claude-CLI shorthands above, these model strings are the
        # identity the vendor bills under — they are passed through verbatim and
        # priced under the same name (runners/schema_utils.PRICING_TABLE keys
        # them identically), so the figures are attributable to the entry.
        _entry(
            "openai",
            "gpt-5.4",
            "cli",
            tier="strong",
            capability=9,
            cost_rank=2,
            input_cost_per_mtok=1.25,
            output_cost_per_mtok=10.00,
            pricing_provenance="gpt-5.4",
        ),
        _entry(
            "openai",
            "gpt-5.4-mini",
            "cli",
            tier="cheap",
            capability=7,
            cost_rank=1,
            input_cost_per_mtok=0.25,
            output_cost_per_mtok=2.00,
            pricing_provenance="gpt-5.4-mini",
        ),
        _entry(
            "openai",
            "gpt-5.4-pro",
            "cli",
            tier="strong",
            capability=10,
            cost_rank=3,
            input_cost_per_mtok=15.00,
            output_cost_per_mtok=120.00,
            pricing_provenance="gpt-5.4-pro",
        ),
        # ── OpenAI (API) ─────────────────────────────────────────────
        _entry(
            "openai",
            "gpt-5.4",
            "api",
            tier="strong",
            capability=9,
            cost_rank=2,
            input_cost_per_mtok=1.25,
            output_cost_per_mtok=10.00,
            pricing_provenance="gpt-5.4",
        ),
        _entry(
            "openai",
            "gpt-5.4-mini",
            "api",
            tier="cheap",
            capability=7,
            cost_rank=1,
            input_cost_per_mtok=0.25,
            output_cost_per_mtok=2.00,
            pricing_provenance="gpt-5.4-mini",
        ),
        _entry(
            "openai",
            "gpt-5.4-pro",
            "api",
            tier="strong",
            capability=10,
            cost_rank=3,
            input_cost_per_mtok=15.00,
            output_cost_per_mtok=120.00,
            pricing_provenance="gpt-5.4-pro",
            # Reasoning-heavy — intentionally excluded from the preflight role.
            phases=frozenset({"dev", "plan", "review"}),
        ),
        # ── DeepSeek (API) ───────────────────────────────────────────
        _entry(
            "deepseek",
            "deepseek-reasoner",
            "api",
            tier="strong",
            capability=9,
            cost_rank=2,
            input_cost_per_mtok=0.55,
            output_cost_per_mtok=2.19,
            pricing_provenance="deepseek-reasoner",
            # Banded a step above its per-MTok rate on purpose: a reasoning model
            # spends far more output tokens per task, so the rate alone (band 1)
            # understates what filling a role with it costs.
            cost_rank_basis=COST_BAND_BASIS_DECLARED_POLICY,
        ),
        _entry(
            "deepseek",
            "deepseek-chat",
            "api",
            tier="fast",
            capability=7,
            cost_rank=1,
            input_cost_per_mtok=0.27,
            output_cost_per_mtok=1.10,
            pricing_provenance="deepseek-chat",
        ),
        # ── Google (API) ─────────────────────────────────────────────
        _entry(
            "google",
            "gemini-3-flash-preview",
            "api",
            tier="cheap",
            capability=7,
            cost_rank=1,
        ),
        _entry(
            "google",
            "gemini-3.1-pro-preview",
            "api",
            tier="strong",
            capability=9,
            cost_rank=2,
            input_cost_per_mtok=2.00,
            output_cost_per_mtok=12.00,
            pricing_provenance="gemini-3.1-pro-preview",
        ),
        _entry(
            "google",
            "gemini-2.5-pro",
            "api",
            tier="strong",
            capability=8,
            cost_rank=2,
            input_cost_per_mtok=1.25,
            output_cost_per_mtok=10.00,
            pricing_provenance="gemini-2.5-pro",
        ),
        # ── Google (Gemini CLI — explicit opt-in) ────────────────────
        _entry(
            "google",
            "gemini-2.5-pro",
            "cli",
            tier="strong",
            capability=8,
            cost_rank=2,
            dev_capable=False,
            input_cost_per_mtok=1.25,
            output_cost_per_mtok=10.00,
            pricing_provenance="gemini-2.5-pro",
        ),
        _entry(
            "google",
            "gemini-3-flash-preview",
            "cli",
            tier="cheap",
            capability=7,
            cost_rank=1,
            dev_capable=False,
        ),
        _entry(
            "google",
            "gemini-3.1-pro-preview",
            "cli",
            tier="strong",
            capability=9,
            cost_rank=2,
            dev_capable=False,
            input_cost_per_mtok=2.00,
            output_cost_per_mtok=12.00,
            pricing_provenance="gemini-3.1-pro-preview",
        ),
        # ── Local OpenAI-compatible models ───────────────────────────
        # API transport + a localhost base_url. Not a provider prefix, not a
        # transport kind — just an endpoint.
        _entry(
            "openai",
            "codestral",
            "api",
            tier="fast",
            capability=7,
            cost_rank=1,
            base_url=LOCAL_OPENAI_COMPATIBLE_BASE_URL,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            pricing_provenance=PRICING_PROVENANCE_LOCAL_ENDPOINT,
        ),
        _entry(
            "openai",
            "deepseek-coder",
            "api",
            tier="fast",
            capability=7,
            cost_rank=1,
            base_url=LOCAL_OPENAI_COMPATIBLE_BASE_URL,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            pricing_provenance=PRICING_PROVENANCE_LOCAL_ENDPOINT,
        ),
        _entry(
            "openai",
            "llama3.1",
            "api",
            tier="fast",
            capability=6,
            cost_rank=1,
            base_url=LOCAL_OPENAI_COMPATIBLE_BASE_URL,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            pricing_provenance=PRICING_PROVENANCE_LOCAL_ENDPOINT,
        ),
        _entry(
            "openai",
            "qwen2.5-coder",
            "api",
            tier="fast",
            capability=7,
            cost_rank=1,
            base_url=LOCAL_OPENAI_COMPATIBLE_BASE_URL,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            pricing_provenance=PRICING_PROVENANCE_LOCAL_ENDPOINT,
        ),
    ]
)


# Raw-input aliases for the pre-transport key spellings. These are accepted only
# at the lookup/parse boundary (:func:`normalize_model_key`) and are translated
# to canonical identities immediately; nothing downstream ever sees them.
_LEGACY_MODEL_KEY_ALIASES: dict[str, str] = {
    "claude/sonnet": "anthropic/sonnet/cli",
    "claude/opus": "anthropic/opus/cli",
    "anthropic/sonnet": "anthropic/sonnet/cli",
    "anthropic/opus": "anthropic/opus/cli",
    "openai/gpt-5.4": "openai/gpt-5.4/cli",
    "openai/gpt-5.4-mini": "openai/gpt-5.4-mini/cli",
    "openai/gpt-5.4-pro": "openai/gpt-5.4-pro/cli",
    "openai-api/gpt-5.4": "openai/gpt-5.4/api",
    "openai-api/gpt-5.4-mini": "openai/gpt-5.4-mini/api",
    "openai-api/gpt-5.4-pro": "openai/gpt-5.4-pro/api",
    "deepseek/deepseek-reasoner": "deepseek/deepseek-reasoner/api",
    "deepseek/deepseek-chat": "deepseek/deepseek-chat/api",
    "google/gemini-3-flash-preview": "google/gemini-3-flash-preview/api",
    "google/gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview/api",
    "google/gemini-2.5-pro": "google/gemini-2.5-pro/api",
    "gemini-cli/gemini-2.5-pro": "google/gemini-2.5-pro/cli",
    "gemini-cli/gemini-3-flash-preview": "google/gemini-3-flash-preview/cli",
    "gemini-cli/gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview/cli",
    "openai/codestral": "openai/codestral/api",
    "openai/deepseek-coder": "openai/deepseek-coder/api",
    "openai/llama3.1": "openai/llama3.1/api",
    "openai/qwen2.5-coder": "openai/qwen2.5-coder/api",
}


def known_raw_model_key_prefixes() -> frozenset[str]:
    """Provider-like leading segments accepted in *raw* model keys.

    Union of the canonical identities' provider families and the legacy alias
    spellings (``claude``, ``openai-api``, ``gemini-cli``). Callers that have to
    interpret an operator- or history-supplied key — never a runtime dispatch
    path — use this to know which prefixes are worth trying.
    """
    return frozenset(
        {key.split("/", 1)[0] for key in AGENT_REGISTRY}
        | {key.split("/", 1)[0] for key in _LEGACY_MODEL_KEY_ALIASES}
    )


def normalize_model_key(model_key: str) -> str:
    """Translate a raw model key into its canonical ``provider/model/kind`` id.

    Raw-input boundary only. Canonical ids pass through unchanged; the legacy
    provider-prefix spellings (``openai-api/…``, ``gemini-cli/…``,
    ``claude/…``) are rewritten here and never propagate further.
    """
    if is_canonical_model_id(model_key):
        return model_key
    return _LEGACY_MODEL_KEY_ALIASES.get(model_key, model_key)


def known_model_overlay_providers() -> tuple[str, ...]:
    """Return accepted provider tokens for raw ``models.custom`` declarations."""
    return tuple(sorted(_MODEL_OVERLAY_PROVIDER_MAP))


def overlay_transport(
    provider: str, transport_kind: str | None = None
) -> tuple[str, TransportSpec]:
    """Normalize a raw ``models.custom`` provider token into (provider, transport).

    Raw-input boundary only. An explicit ``transport_kind`` (the canonical
    spelling) wins; the provider-like alias tokens (``openai-api``,
    ``gemini-cli``) are accepted purely for migration and carry an implied kind.
    """
    if provider not in _MODEL_OVERLAY_PROVIDER_MAP:
        known = ", ".join(sorted(_MODEL_OVERLAY_PROVIDER_MAP))
        raise ValueError(
            f"Unknown provider {provider!r} in models.custom. Known providers/adapters: {known}"
        )
    family, implied_kind = _MODEL_OVERLAY_PROVIDER_MAP[provider]
    kind = transport_kind or implied_kind
    return family, transport_for(family, kind)


def custom_model_capability(tier: str) -> int:
    """Return the default capability score for a custom model tier."""
    by_tier = {"cheap": 6, "fast": 7, "strong": 9}
    if tier not in by_tier:
        raise ValueError(f"models.custom tier must be one of {sorted(by_tier)}, got {tier!r}")
    return by_tier[tier]


def custom_model_dev_capable(transport: TransportSpec) -> bool:
    """Return whether a custom model transport can own the dev role."""
    return not (transport.kind == "cli" and transport.runner == "gemini")


def _spec_to_model_info(
    model_key: str | AgentSpec,
    spec: AgentSpec | None = None,
) -> ModelInfo:
    """Project an AgentSpec down to the legacy ModelInfo view.

    Carries `phase_eligibility` and the underlying `TransportSpec` through the
    projection so role derivation can filter by phase and runtime dispatch can
    read the explicit transport rather than re-inferring it from cli/provider.
    """
    if spec is None:
        spec = model_key
        assert isinstance(spec, AgentSpec)
        model_key = spec.model

    assert isinstance(model_key, str)
    is_cli = spec.transport.kind == "cli"
    return ModelInfo(
        cli=spec.transport.runner if is_cli else None,
        model=spec.model,
        tier=spec.tier,
        capability=spec.capability,
        cost_rank=spec.cost_rank,
        dev_capable=spec.dev_capable,
        provider=None if is_cli else spec.provider,
        provider_family=spec.provider,
        base_url=spec.base_url,
        phase_eligibility=spec.phase_eligibility,
        registry_id=model_key,
        registry_source=spec.registry_source,
        input_cost_per_mtok=spec.input_cost_per_mtok,
        output_cost_per_mtok=spec.output_cost_per_mtok,
        pricing_provenance=spec.pricing_provenance,
        transport=spec.transport,
    )


# Derived legacy view — exactly mirrors AGENT_REGISTRY.
MODEL_REGISTRY: dict[str, ModelInfo] = {
    k: _spec_to_model_info(k, v) for k, v in AGENT_REGISTRY.items()
}


def model_info_view(
    registry: dict[str, AgentSpec] | None = None,
) -> dict[str, ModelInfo]:
    """Return a {model_key: ModelInfo} view of an AgentSpec registry.

    With ``registry=None`` returns the built-in MODEL_REGISTRY. Pass
    ``ForgeConfig.model_registry`` (the merged built-in + forge.yaml overlay)
    so consumers see user-declared custom models as first-class entries.

    An explicitly empty ``{}`` is honored as-is and returns an empty view — it is
    not treated as "absent" (only ``None`` selects the built-in default).
    """
    if registry is None:
        return MODEL_REGISTRY
    return {k: _spec_to_model_info(k, v) for k, v in registry.items()}


@overload
def apply_model_info(profile: ModelProfile, info: ModelInfo) -> ModelProfile: ...


@overload
def apply_model_info(profile: ModelRef, info: ModelInfo) -> ModelRef: ...


@overload
def apply_model_info(profile: PlanConfig, info: ModelInfo) -> PlanConfig: ...


def apply_model_info(profile: _ProfileT, info: ModelInfo) -> _ProfileT:
    """Return profile with model identity and dispatch transport from ``info``.

    Mid-run model swaps must update every field that defines dispatch together,
    ``transport`` first: it is the dispatch source of truth, and leaving a stale
    TransportSpec behind while the model name changes is exactly the divergence
    this helper exists to prevent. ``base_url`` travels with the identity so a
    swap onto a locally served model reaches the right endpoint.

    A PlanConfig holds no transport of its own — it composes a ModelRef — so the
    swap is applied to that ref and the wrapper is rebuilt around it.
    """
    if isinstance(profile, PlanConfig):
        return replace(profile, ref=apply_model_info(profile.ref, info))
    profile_fields = {f.name for f in fields(profile)}
    updates: dict[str, object] = {
        "cli": info.cli,
        "model": info.model,
        "provider": info.provider,
    }
    if "registry_id" in profile_fields:
        updates["registry_id"] = info.registry_id
    if "registry_source" in profile_fields:
        updates["registry_source"] = info.registry_source
    if "transport" in profile_fields:
        updates["transport"] = info.transport
    if "base_url" in profile_fields:
        updates["base_url"] = info.base_url
    return replace(profile, **updates)


def resolve_agent_spec(
    model_key: str,
    registry: dict[str, AgentSpec] | None = None,
) -> AgentSpec:
    """Resolve a model key to its AgentSpec.

    This is the raw-lookup boundary: a legacy provider-prefix spelling
    (``openai-api/gpt-5.4``, ``gemini-cli/gemini-2.5-pro``, ``claude/opus``) is
    normalized to its canonical ``provider/model/kind`` identity here, and only
    the canonical form exists past this point. Raises ValueError for any key
    that is neither canonical nor a known alias — there is no
    provider-prefix-to-CLI guessing. To support a new model, add an explicit
    registry entry.

    ``registry=None`` means "no registry supplied — use the built-in default."
    An explicitly empty ``{}`` is honored as-is: every key is unknown, so this
    raises ValueError rather than silently substituting the built-in registry.
    """
    effective_registry = AGENT_REGISTRY if registry is None else registry
    lookup = model_key if model_key in effective_registry else normalize_model_key(model_key)
    if lookup not in effective_registry:
        known = sorted(effective_registry)
        raise ValueError(
            f"Unknown model {model_key!r}: not in AGENT_REGISTRY. "
            f"Add an explicit registry entry. Known models: {known}"
        )
    return effective_registry[lookup]


def _resolve_model_info(
    model_key: str,
    registry: dict[str, AgentSpec] | None = None,
) -> ModelInfo:
    """Resolve a model key to its legacy ModelInfo view.

    Delegates to resolve_agent_spec(): unknown keys raise ValueError rather than
    falling back to a prefix-derived CLI. The resulting ``registry_id`` is always
    the canonical identity, never the alias the caller happened to pass.
    """
    spec = resolve_agent_spec(model_key, registry=registry)
    effective_registry = AGENT_REGISTRY if registry is None else registry
    canonical_key = (
        model_key if model_key in effective_registry else normalize_model_key(model_key)
    )
    return _spec_to_model_info(canonical_key, spec)


def _planner_candidate_models(agents: list[AgentDef]) -> set[str]:
    """Return model names the adaptive planner can select at runtime.

    Mirrors assign_models planner selection logic (PHASE_TIER["plan"]):
      LOW → mid tier (fallback: highest-budget agent if no mid agents)
      MEDIUM/HIGH → strong tier (fallback: highest-budget agent if no strong agents)

    Auth filtering is intentionally omitted: auth state can change at runtime,
    so the static check only considers structural availability.
    """
    if not agents:
        return set()

    # PHASE_TIER["plan"] = {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"}
    planner_tiers = {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"}
    highest_budget = sorted(agents, key=lambda a: -a.budget_usd)[0]

    candidate_models: set[str] = set()
    for tier in planner_tiers.values():
        # Mirror _agents_by_tier: equal-budget ties break on real per-MTok price
        # so the predicted candidate matches the model assign_models actually picks.
        tier_agents = sorted(
            [a for a in agents if a.tier == tier],
            key=lambda a: (
                a.budget_usd,
                price_tiebreak_signal_for(a),
            ),
        )
        if tier_agents:
            candidate_models.add(tier_agents[0].model)
        else:
            # Mirror assign_models fallback: pick highest-budget agent
            candidate_models.add(highest_budget.model)

    return candidate_models


def _parse_recency(recency_raw: Any) -> RecencyConfig:
    """Parse and validate the ``assignment.recency`` block (#1392).

    Config loading is an integrity boundary: reject invalid values with a clear
    error rather than silently falling back to a default that hides bad config.
    """
    if recency_raw is None:
        return RecencyConfig()
    if not isinstance(recency_raw, dict):
        raise ValueError(f"assignment.recency must be a mapping, got {type(recency_raw).__name__}")
    defaults = RecencyConfig()
    mode = str(recency_raw.get("mode", defaults.mode)).strip().lower()
    if mode not in ("exponential", "window", "off"):
        raise ValueError(
            "assignment.recency.mode must be one of 'exponential', 'window', 'off', "
            f"got {recency_raw.get('mode')!r}"
        )
    try:
        half_life_runs = float(recency_raw.get("half_life_runs", defaults.half_life_runs))
    except (TypeError, ValueError):
        raise ValueError(
            f"assignment.recency.half_life_runs must be a number, "
            f"got {recency_raw.get('half_life_runs')!r}"
        ) from None
    if mode == "exponential" and half_life_runs <= 0:
        raise ValueError(
            "assignment.recency.half_life_runs must be > 0 for exponential mode, "
            f"got {half_life_runs}"
        )
    try:
        window = int(recency_raw.get("window", defaults.window))
    except (TypeError, ValueError):
        raise ValueError(
            f"assignment.recency.window must be an integer, got {recency_raw.get('window')!r}"
        ) from None
    if window <= 0:
        raise ValueError(f"assignment.recency.window must be > 0, got {window}")
    return RecencyConfig(mode=mode, half_life_runs=half_life_runs, window=window)


def _parse_unit_float(value: Any, *, key: str, default: float) -> float:
    """Validate a probability threshold in ``[0.0, 1.0]`` at the config boundary.

    Config loading is an integrity boundary (ADR-0006 clause 1): reject an
    out-of-range or non-numeric threshold with a clear error rather than silently
    clamping, which would hide bad config and let a nonsense promotion threshold
    swing routing. ``bool`` is rejected explicitly (a YAML ``true`` is an ``int``
    subtype but a nonsense rate).
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number in [0.0, 1.0], got {value!r}")
    coerced = float(value)
    if not 0.0 <= coerced <= 1.0:
        raise ValueError(f"{key} must be in [0.0, 1.0], got {coerced}")
    return coerced


def _parse_strict_bool(value: Any, *, key: str, default: bool) -> bool:
    """Validate an opt-in flag at the config integrity boundary.

    Bare ``bool()`` coercion makes every non-empty scalar truthy, so a typo like
    ``enabled: flase`` or ``enabled: "no"`` silently turns a routing mechanism ON.
    Config loading is an integrity boundary (ADR-0006 clause 1): a value that is
    not an actual boolean is a hard error, not a truthiness question.
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean, got {value!r}")
    return value


def _parse_positive_int(value: Any, *, key: str, minimum: int, default: int) -> int:
    """Coerce/validate a bounded integer at the config integrity boundary.

    ``bool`` is rejected explicitly (a YAML ``true`` is an ``int`` subtype but a
    nonsense cadence), and values below ``minimum`` are a hard error rather than
    a silent clamp that would hide bad config.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}, got {value}")
    return value


def _parse_exploration(exploration_raw: Any) -> ExplorationConfig:
    """Parse and validate the ``assignment.exploration`` block (#325).

    Config loading is an integrity boundary (ADR-0006 clause 8: exploration must
    stay bounded); reject invalid cadence/floor/cap values with a clear error.
    """
    if exploration_raw is None:
        return ExplorationConfig()
    if not isinstance(exploration_raw, dict):
        raise ValueError(
            f"assignment.exploration must be a mapping, got {type(exploration_raw).__name__}"
        )
    defaults = ExplorationConfig()
    explore_every_n = _parse_positive_int(
        exploration_raw.get("explore_every_n"),
        key="assignment.exploration.explore_every_n",
        minimum=1,
        default=defaults.explore_every_n,
    )
    min_sample_size = _parse_positive_int(
        exploration_raw.get("min_sample_size"),
        key="assignment.exploration.min_sample_size",
        minimum=1,
        default=defaults.min_sample_size,
    )
    per_sprint_cap = _parse_positive_int(
        exploration_raw.get("per_sprint_cap"),
        key="assignment.exploration.per_sprint_cap",
        minimum=0,
        default=defaults.per_sprint_cap,
    )
    cache_path_raw = exploration_raw.get("performance_cache_path")
    if cache_path_raw is None:
        performance_cache_path = defaults.performance_cache_path
    elif isinstance(cache_path_raw, str) and cache_path_raw.strip():
        performance_cache_path = cache_path_raw.strip()
    else:
        raise ValueError(
            "assignment.exploration.performance_cache_path must be a non-empty string, "
            f"got {cache_path_raw!r}"
        )
    return ExplorationConfig(
        explore_every_n=explore_every_n,
        min_sample_size=min_sample_size,
        per_sprint_cap=per_sprint_cap,
        performance_cache_path=performance_cache_path,
    )


def _parse_effort_buckets(raw: Any, *, key: str) -> dict[str, tuple[tuple[int, str], ...]]:
    """Parse a ``{phase: [{max_score, effort}, ...]}`` override table (#1108).

    Config loading is an integrity boundary: an unknown phase, a non-``low``/
    ``medium``/``high`` effort, an out-of-range or non-ascending ``max_score``,
    or a band table that never reaches 10 is a hard error rather than a silently
    ignored override that would leave the operator's intent unapplied.
    """
    from ..routing import REASONING_EFFORT_PHASES, VALID_REASONING_EFFORTS  # noqa: PLC0415

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{key} must be a mapping of phase to bands, got {type(raw).__name__}")
    parsed: dict[str, tuple[tuple[int, str], ...]] = {}
    for phase, bands_raw in raw.items():
        if phase not in REASONING_EFFORT_PHASES:
            raise ValueError(
                f"{key} phase must be one of {list(REASONING_EFFORT_PHASES)}, got {phase!r}"
            )
        if not isinstance(bands_raw, list) or not bands_raw:
            raise ValueError(f"{key}.{phase} must be a non-empty list of bands, got {bands_raw!r}")
        bands: list[tuple[int, str]] = []
        previous = 0
        for band in bands_raw:
            if not isinstance(band, dict):
                raise ValueError(f"{key}.{phase} band must be a mapping, got {band!r}")
            max_score = band.get("max_score")
            effort = band.get("effort")
            if isinstance(max_score, bool) or not isinstance(max_score, int):
                raise ValueError(
                    f"{key}.{phase} band max_score must be an integer, got {max_score!r}"
                )
            if not previous < max_score <= 10:
                raise ValueError(
                    f"{key}.{phase} band max_score must ascend within 1-10, "
                    f"got {max_score} after {previous}"
                )
            if effort not in VALID_REASONING_EFFORTS:
                raise ValueError(
                    f"{key}.{phase} band effort must be one of "
                    f"{list(VALID_REASONING_EFFORTS)}, got {effort!r}"
                )
            bands.append((max_score, effort))
            previous = max_score
        if previous != 10:
            raise ValueError(f"{key}.{phase} bands must cover scores through 10, got {previous}")
        parsed[phase] = tuple(bands)
    return parsed


def _parse_token_budgets(raw: Any, *, key: str) -> dict[str, int]:
    """Parse an ``{effort: token_budget}`` override map (#1108)."""
    from ..routing import VALID_REASONING_EFFORTS  # noqa: PLC0415

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{key} must be a mapping of effort to token budget, got {raw!r}")
    parsed: dict[str, int] = {}
    for effort, budget in raw.items():
        if effort not in VALID_REASONING_EFFORTS:
            raise ValueError(
                f"{key} effort must be one of {list(VALID_REASONING_EFFORTS)}, got {effort!r}"
            )
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError(f"{key}.{effort} must be a non-negative integer, got {budget!r}")
        parsed[effort] = budget
    return parsed


def _parse_reasoning_effort(raw: Any) -> ReasoningEffortConfig:
    """Parse and validate ``assignment.reasoning_effort`` (#1108)."""
    if raw is None:
        return ReasoningEffortConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"assignment.reasoning_effort must be a mapping, got {type(raw).__name__}"
        )
    base = "assignment.reasoning_effort"
    providers_raw = raw.get("providers")
    providers: dict[str, ProviderReasoningEffortConfig] = {}
    if providers_raw is not None:
        if not isinstance(providers_raw, dict):
            raise ValueError(f"{base}.providers must be a mapping, got {providers_raw!r}")
        for provider, entry in providers_raw.items():
            if not isinstance(entry, dict):
                raise ValueError(f"{base}.providers.{provider} must be a mapping, got {entry!r}")
            providers[str(provider)] = ProviderReasoningEffortConfig(
                phase_buckets=_parse_effort_buckets(
                    entry.get("phases"), key=f"{base}.providers.{provider}.phases"
                ),
                token_budgets=_parse_token_budgets(
                    entry.get("token_budgets"),
                    key=f"{base}.providers.{provider}.token_budgets",
                ),
            )
    return ReasoningEffortConfig(
        enabled=bool(raw.get("enabled", True)),
        phase_buckets=_parse_effort_buckets(raw.get("phases"), key=f"{base}.phases"),
        token_budgets=_parse_token_budgets(raw.get("token_budgets"), key=f"{base}.token_budgets"),
        providers=providers,
    )


def _parse_assignment(assignment_raw: dict[str, Any]) -> AssignmentConfig:
    """Parse assignment config from raw YAML dict."""
    return AssignmentConfig(
        enabled=bool(assignment_raw.get("enabled", False)),
        min_reviewers=int(assignment_raw.get("min_reviewers", 1)),
        max_reviewers=int(assignment_raw.get("max_reviewers", 3)),
        prefer_cross_provider=bool(assignment_raw.get("prefer_cross_provider", True)),
        max_cost_per_story_usd=(
            float(assignment_raw["max_cost_per_story_usd"])
            if assignment_raw.get("max_cost_per_story_usd") is not None
            else None
        ),
        escalation_memory=bool(assignment_raw.get("escalation_memory", True)),
        adaptive_enabled=bool(assignment_raw.get("adaptive_enabled", True)),
        recency=_parse_recency(assignment_raw.get("recency")),
        exploration=_parse_exploration(assignment_raw.get("exploration")),
        reviewer_completion_threshold=float(
            assignment_raw.get("reviewer_completion_threshold", 0.5)
        ),
        reviewer_completion_min_runs=int(assignment_raw.get("reviewer_completion_min_runs", 5)),
        # Both reviewer-value phases validate strictly: these three fields decide
        # whether a mechanical signal is allowed to reorder reviewers and how much
        # evidence it needs first, so a malformed scalar must fail loudly rather
        # than coerce into an active routing setting.
        reviewer_value_enabled=_parse_strict_bool(
            assignment_raw.get("reviewer_value_enabled"),
            key="assignment.reviewer_value_enabled",
            default=False,
        ),
        reviewer_value_uniqueness_threshold=_parse_unit_float(
            assignment_raw.get("reviewer_value_uniqueness_threshold"),
            key="assignment.reviewer_value_uniqueness_threshold",
            default=0.34,
        ),
        reviewer_value_min_runs=_parse_positive_int(
            assignment_raw.get("reviewer_value_min_runs"),
            key="assignment.reviewer_value_min_runs",
            minimum=1,
            default=5,
        ),
        code_review_value_enabled=_parse_strict_bool(
            assignment_raw.get("code_review_value_enabled"),
            key="assignment.code_review_value_enabled",
            default=False,
        ),
        code_review_value_uniqueness_threshold=_parse_unit_float(
            assignment_raw.get("code_review_value_uniqueness_threshold"),
            key="assignment.code_review_value_uniqueness_threshold",
            default=0.34,
        ),
        code_review_value_min_runs=_parse_positive_int(
            assignment_raw.get("code_review_value_min_runs"),
            key="assignment.code_review_value_min_runs",
            minimum=1,
            default=5,
        ),
        dev_promotion_threshold=_parse_unit_float(
            assignment_raw.get("dev_promotion_threshold"),
            key="assignment.dev_promotion_threshold",
            default=0.60,
        ),
        dev_promotion_min_runs=_parse_positive_int(
            assignment_raw.get("dev_promotion_min_runs"),
            key="assignment.dev_promotion_min_runs",
            minimum=1,
            default=5,
        ),
        plan_tier_reduction=bool(assignment_raw.get("plan_tier_reduction", True)),
        reasoning_effort=_parse_reasoning_effort(assignment_raw.get("reasoning_effort")),
    )
