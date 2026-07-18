"""Shared agent result types used by both the runners and coordinator packages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
