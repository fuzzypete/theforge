"""Shared agent result types used by both the runners and coordinator packages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── Cost provenance (#2205) ───────────────────────────────────────────────
# An observed cost is not self-describing: a number the provider billed and a
# number forge derived from a token count and a pricing table are different
# kinds of evidence, and a consumer attributing spend to a model has to be able
# to tell them apart. Recorded next to every cost rather than inferred from the
# runner, because the same runner produces both (a Claude CLI result event
# reports cost; the same runner's kill path reconstructs one).
COST_PROVIDER_REPORTED = "provider_reported"
"""The provider returned this cost; forge did not compute it."""

COST_ESTIMATED = "estimated"
"""Derived by forge from observed token counts and a pricing table."""

COST_UNKNOWN = "unknown"
"""No cost was observed. Always paired with ``cost_usd is None``."""

COST_PROVENANCE_VALUES = (COST_PROVIDER_REPORTED, COST_ESTIMATED, COST_UNKNOWN)


@dataclass(frozen=True)
class ModelUsage:
    """Per-model token and cost breakdown from a single agent invocation."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None
    thinking_tokens: int = 0
    # Whether this component's cost was reported by the provider or estimated
    # by forge. Defaults to "unknown" so a legacy constructor that predates the
    # field says "not stated" rather than claiming provider authority (#2205).
    cost_provenance: str = COST_UNKNOWN


@dataclass(frozen=True)
class AgentResult:
    """Structured result from an agent invocation."""

    success: bool  # subprocess returned 0
    output: str  # agent's text response
    session_id: str | None  # for --resume on follow-up
    cost_usd: float | None  # total invocation cost
    exit_code: int  # raw exit code
    raw: dict[str, Any]  # full parsed JSON (if available)
    profile_name: str = ""  # identifies which profile produced this result
    model_usage: tuple[ModelUsage, ...] = ()  # per-model breakdown (Claude only)
    structured_data: dict | None = None  # parsed JSON for API reviewers
    startup_failure: bool = False  # True when the agent couldn't start at all
    model_config: tuple[
        str, ...
    ] = ()  # configured preference list (non-empty when fallback fired)
    model_used: str | None = (
        None  # model that actually ran (set by runners; None for legacy results)
    )
    dev_handoff: dict | None = None  # parsed <forge_handoff> block from agent output
    failure_code: str | None = None  # stable machine-readable failure identifier
    cli_quota_error_observed: bool = False  # CLI reported quota/rate-limit exhaustion
    transport_fallback_fired: bool = False  # invocation switched transport mid-attempt
    transport_fallback_reason: str | None = None  # classifier reason that triggered fallback
    transport_used: str | None = None  # transport that actually ran last: "cli" or "api"
    # Why an *eligible* transport fallback was never attempted. A recorded
    # ``transport_fallback_reason`` with ``fired=False`` states an intention;
    # this field states the outcome, so the audit says why nothing happened
    # rather than leaving the operator to infer it (#2298). None whenever a
    # fallback fired, or when the failure was never fallback-eligible.
    transport_fallback_not_applied_reason: str | None = None
    # The reset time the provider stated alongside a quota refusal, verbatim
    # (e.g. "Aug 8th, 2026 7:59 AM"). Present only when the provider named one:
    # that is the difference between a failure worth retrying and one that is
    # certain to repeat until the stated time.
    provider_quota_reset_at: str | None = None
    # Best-effort observed tool calls, retained even when the run crashes/times
    # out so downstream phases can salvage the exploration a failed agent did.
    # Each entry is {"tool": <name>, "target": <path/pattern or None>}. Empty
    # for providers/paths that do not stream tool activity (e.g. single-shot
    # CLIs). Consumed by the preflight partial-evidence artifact (issue #706).
    tool_trace: tuple[dict[str, Any], ...] = ()
    # Last substantive agent text captured from a killed/crashed run whose
    # ``output`` field holds only a failure marker (timeout/stuck/no-result).
    # None on clean successes (``output`` already carries the text) and on
    # providers that do not stream partial output.
    partial_output: str | None = None
    # ── Invocation ledger identity (#2205) ────────────────────────────────
    # ``model_used``/``transport_used`` above are the *resolved primary*
    # identity: the concrete model that served this invocation. The two fields
    # below are the *configured* identity: the spelling the operator selected
    # in the profile, before any alias resolution, preference-list fallback, or
    # transport fallback rewrote it. They are separate values because a single
    # invocation genuinely has a different answer for each, and collapsing them
    # forces a choice that discards the rest.
    configured_model: str | None = None
    configured_transport: str | None = None  # "cli" | "api" as configured
    # Reasoning effort the profile requested for this invocation, when the
    # transport supports one. None means the profile did not request one.
    reasoning_effort: str | None = None
    # Whether ``cost_usd`` was reported by the provider or estimated by forge.
    # Always ``COST_UNKNOWN`` when ``cost_usd`` is None.
    cost_provenance: str = COST_UNKNOWN
