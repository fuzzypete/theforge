"""Canonical model identity: transport, routing policy, and the AgentSpec.

The identity half of the model registry, kept in its own low-dependency module
so both the registry (:mod:`theforge.config.models`) and the definition parser
(:mod:`theforge.config.model_catalog`) can build on it without importing each
other. A model is exactly ``(provider, model, transport.kind)``; everything that
turns a raw operator spelling into that triple, and the routing policy attached
to it, lives here.

Transport is first class: ``cli``/``api`` is never encoded in a provider-like
prefix (``openai-api/``, ``gemini-cli/``). Those spellings survive only as raw
input aliases below and are normalized to canonical identities the moment they
are read.

Depends only on :mod:`theforge.config.pricing` (stdlib-only) and the standard
library, so it stays importable from anywhere in the config package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .pricing import AttributablePricing

TRANSPORT_KINDS: frozenset[str] = frozenset({"cli", "api"})


# ── Upstream identifier status ───────────────────────────────────────────
#
# A model identifier is a claim about something outside this repository: that
# the provider still serves that name, and that it still designates the model
# whose capability, tier and price were recorded against it. Providers retire
# and re-point identifiers, and — the dangerous case — a retired identifier
# often keeps *resolving*, so a run against it completes with exit 0 and real
# token counts while the declaration describes a model that is no longer there.
#
# So the claim is written down rather than assumed. Every catalog entry may
# state when its identifier was last checked against the provider's published
# model list, and an entry the provider has retired says so explicitly instead
# of quietly staying selectable (#2352).

IDENTITY_STATUS_SERVED = "served"
IDENTITY_STATUS_RETIRED = "retired"
IDENTITY_STATUSES: frozenset[str] = frozenset({IDENTITY_STATUS_SERVED, IDENTITY_STATUS_RETIRED})

# How long a recorded verification stays load-bearing. A check made against the
# provider's model list is evidence about the day it was made, not a permanent
# property: past this window the entry reverts to *unconfirmed*, which is the
# same state an entry that never declared a check is in. Deliberately not a
# load error — an expired check is not bad configuration, it is configuration
# whose supporting evidence has aged out, and the operator is the one who can
# refresh it.
IDENTITY_VERIFICATION_MAX_AGE_DAYS = 180


@dataclass(frozen=True)
class IdentityVerification:
    """What is known about an entry's upstream identifier, and when.

    ``status`` is what the provider is understood to do with the name today.
    ``verified_against`` names the source the check was made against (a
    published model list, a live probe) and ``verified_on`` the day it was made;
    together they are what makes :meth:`confirmed_on` answerable rather than a
    matter of belief. ``retired_reason`` is required of a retired entry — a name
    withdrawn without a stated reason tells an operator nothing they can act on.
    """

    status: str = IDENTITY_STATUS_SERVED
    verified_against: str | None = None
    verified_on: date | None = None
    retired_reason: str | None = None

    @property
    def retired(self) -> bool:
        return self.status == IDENTITY_STATUS_RETIRED

    def confirmed_on(self, today: date) -> bool:
        """True when a check exists for this identifier and has not aged out."""
        if self.retired or self.verified_on is None or not self.verified_against:
            return False
        return today - self.verified_on <= timedelta(days=IDENTITY_VERIFICATION_MAX_AGE_DAYS)

    def describe(self, today: date) -> str:
        """One-line statement of what is known, for operator-facing reports."""
        if self.retired:
            return f"retired upstream — {self.retired_reason or 'no reason recorded'}"
        if self.verified_on is None or not self.verified_against:
            return "never checked against the provider's published model list"
        age = (today - self.verified_on).days
        if age > IDENTITY_VERIFICATION_MAX_AGE_DAYS:
            return (
                f"last checked {self.verified_on.isoformat()} against {self.verified_against} "
                f"({age}d ago, over the {IDENTITY_VERIFICATION_MAX_AGE_DAYS}d window)"
            )
        return f"checked {self.verified_on.isoformat()} against {self.verified_against}"


# The state of an entry that says nothing about its identifier: the provider may
# well still serve it, but nothing here establishes that.
UNCONFIRMED_IDENTITY = IdentityVerification()


# ── Provider invocation controls ─────────────────────────────────────────
#
# Where a provider expresses a behavioural mode as a *request parameter* rather
# than as a distinct model name, banding an entry for that behaviour is only
# meaningful if the request actually asks for it. ``reasoning_mode`` is that
# declaration: it says what the entry's routing band was recorded about, in a
# form the adapter can forward.
REASONING_MODE_ENABLED = "enabled"
REASONING_MODE_DISABLED = "disabled"
REASONING_MODES: frozenset[str] = frozenset({REASONING_MODE_ENABLED, REASONING_MODE_DISABLED})


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

# Domains of the routing fields, kept next to the policy they constrain so both
# declaration surfaces check the same thing. Each is bounded by what actually
# consumes it, not by taste:
#
# - ``MODEL_TIERS``: the tiers ``custom_model_capability`` below can score.
# - ``CAPABILITY_RANGE``: the 1-10 scale ``RoutingPolicy.capability`` documents.
# - ``COST_RANK_RANGE``: the bands role selection reads (1=cheap, 2=mid,
#   3=strong) — see ``config/pricing.py``.
# - ``KNOWN_PHASES``: the only phases ever queried, by ``derive_roles()`` in
#   ``role_derivation.py``. It equals the default set today because every known
#   phase is eligible by default; they are separate names because a future phase
#   that is *not* default-eligible would make them diverge.
#
# An unrecognized phase is the sharpest of these: ``_phase_candidates`` falls
# back to the whole list when filtering empties it, so ``[reviewer]`` for
# ``[review]`` does not fail — it quietly drops the model from the pool it was
# declared for, which is the failure mode that is impossible to see from config.
MODEL_TIERS: frozenset[str] = frozenset({"cheap", "fast", "strong"})
CAPABILITY_RANGE: tuple[int, int] = (1, 10)
COST_RANK_RANGE: tuple[int, int] = (1, 3)
KNOWN_PHASES: frozenset[str] = frozenset({"preflight", "dev", "plan", "review"})


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


def model_fallback_transport(provider: str | None) -> TransportSpec | None:
    """The transport a ``fallback_models`` entry actually dispatches on.

    Always the provider's API transport, whichever transport the *primary* uses,
    and both dispatch paths agree on that:

    - An API profile retries the next model on its own API transport
      (``runners/api.py`` replaces only ``model``).
    - A CLI profile's fallback entries are sent through the provider's API
      adapter (``runners/cli.py:_build_cli_fallback_api_profile``). A CLI failure
      that triggers a model fallback is a quota or model-not-found refusal, so
      retrying on the CLI that just refused would reproduce it.

    Returns ``None`` when the provider has no API adapter — no fallback can be
    attempted at all, so that entry names no reachable identity.

    This function exists so the runner that builds the fallback profile and the
    load-time enumeration that prices it read ONE definition of the rule. They
    disagreed before: load reported a CLI profile's fallback entries as CLI
    identities while the runner dispatched them on the API, so the identity that
    could actually run unpriced was never the one named (#2335).
    """
    if not provider:
        return None
    try:
        return transport_for(provider, "api")
    except ValueError:
        return None


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
    model: str  # model identifier (e.g. "sonnet", "gpt-5.4", "deepseek-v4-pro")
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
    # A provider that bills prompt-cache hits at its own published rate rather
    # than as a fixed fraction of the uncached rate states that rate here. None
    # means "no separately-billed tier declared" and the generic cache discount
    # in runners/schema_utils applies.
    cached_input_cost_per_mtok: float | None = None
    # What is known about the upstream identifier this entry names (#2352).
    identity: IdentityVerification = UNCONFIRMED_IDENTITY
    # Request-level behavioural mode to ask for at invocation, where the provider
    # expresses one. None = send nothing and take the provider's default.
    reasoning_mode: str | None = None

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


# ── Routing defaults for a declaration that states no policy ─────────────
#
# A definition that names only a tier still has to land on a full routing
# policy. These supply the two knobs that can be derived from what it did say,
# and are used only when there is no built-in entry of the same identity to
# inherit from — see config/model_catalog.resolve_project.


def custom_model_capability(tier: str) -> int:
    """Return the default capability score for a custom model tier."""
    by_tier = {"cheap": 6, "fast": 7, "strong": 9}
    if tier not in by_tier:
        raise ValueError(f"models.custom tier must be one of {sorted(by_tier)}, got {tier!r}")
    return by_tier[tier]


def custom_model_dev_capable(transport: TransportSpec) -> bool:
    """Return whether a custom model transport can own the dev role."""
    return not (transport.kind == "cli" and transport.runner == "gemini")


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
