"""Classification of agent invocations that produced **no model output**.

A phase consumes two very different kinds of event through the same
``AgentResult`` channel:

1. *A model answered.* The output may be an approval, a rejection, an
   escalation, or even malformed prose — but a model formed a judgment about
   the work.
2. *No model answered.* The process died, the credential was rejected, the
   transport dropped, or the wall clock ran out before any text existed. This
   is a statement about the substrate, not about the story.

Historically each phase collapsed (2) into its own worst-case member of (1) —
preflight into ``PROCEED``, plan review into ``REJECT``, dev into
``Phase.ESCALATE`` — and those manufactured verdicts were indistinguishable
downstream from real ones, up to and including durable adaptive memory
(issue #1951). This module owns the one shared distinction so every phase
classifies the same event the same way.

Deliberately stdlib-only (CONVENTIONS §4) so foundational callers — the phase
modules, the audit writer, and the sprint reporter — can import the vocabulary
without pulling in runtime dependencies. It classifies; it never decides what a
phase does with the classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .trust_status import CHECK_FAIL, make_trust_check

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import CoordinatorState

# ── Failure categories ───────────────────────────────────────────────────
# Why no model output was obtained. All four are statements about the
# substrate; none is a statement about the story.
CATEGORY_AUTH = "auth"  # credential rejected (401, revoked token, bad key)
CATEGORY_TRANSPORT = "transport"  # connection dropped, rate limit, provider 5xx
CATEGORY_TIMEOUT = "timeout"  # wall-clock kill before any text existed
CATEGORY_STARTUP = "startup"  # CLI/binary/sandbox could not start at all
CATEGORY_PROCESS = "process"  # non-zero exit / signal with no output
CATEGORY_NO_RESULT = "no_result"  # agent ran but ended without a terminal result

ALLOWED_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        CATEGORY_AUTH,
        CATEGORY_TRANSPORT,
        CATEGORY_TIMEOUT,
        CATEGORY_STARTUP,
        CATEGORY_PROCESS,
        CATEGORY_NO_RESULT,
    }
)

# Name of the trust check this module writes when a run's terminal outcome is
# an infrastructure abort. A failed check taints the run, and the centralized
# ADR-0006 clause 4 gate (``trust_status.is_tainted``) already excludes tainted
# runs from every router-consumed aggregate.
TRUST_CHECK_AGENT_JUDGMENT = "agent_judgment_obtained"

TRUST_CHECK_PRODUCER = "coordinator.agent_failure"

# Machine-readable ``state.error_type`` for a run terminated because no agent
# judgment was obtainable. Distinguishes an infrastructure abort from a genuine
# story ESCALATE in every consumer that reads ``outcome_code``.
ERROR_TYPE_INFRASTRUCTURE_ABORT = "infrastructure_abort"

# Placeholder verdict token for a phase result slot that must exist structurally
# but carries no model judgment. Never a member of any real verdict vocabulary,
# so a consumer that mistakes it for one fails loudly instead of silently
# reading it as PROCEED / REJECT / APPROVE.
NO_JUDGMENT = "NO_JUDGMENT"

# ── Recognized substrate-failure text ────────────────────────────────────
# Provider/credential rejections. Matched case-insensitively against the
# failure text a runner captured (usually provider stderr). Every entry is a
# distinctive phrase: a pattern that can occur in ordinary agent prose would
# invert this module's purpose, reclassifying a real model judgment as a
# substrate failure.
_AUTH_PATTERNS: tuple[str, ...] = (
    "failed to authenticate",
    "oauth access token has been revoked",
    "authentication_error",
    "authenticationerror",
    "invalid api key",
    "invalid_api_key",
    "invalid x-api-key",
    "unauthorized",
    "credit balance is too low",
    "permission_error",
)

# Transport-layer drops and provider-side unavailability.
_TRANSPORT_PATTERNS: tuple[str, ...] = (
    "connection error",
    "connection closed",
    "connection reset",
    "econnreset",
    "socket hang up",
    "network error",
    "fetch failed",
    "rate limit",
    "internal server error",
    "servererror",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "overloaded_error",
    # Provider error banners are emitted with a trailing colon ("API Error:
    # ..."); requiring it keeps the phrase from matching an agent that merely
    # mentions an API error in its narration.
    "api error:",
)

# HTTP status codes are matched ONLY in a status-code-shaped context. A bare
# substring test for "401" / "429" / "500" fires on any output that happens to
# contain those digits — a line number, an issue reference, a diff hunk, a
# duration — and would misclassify a genuine agent judgment as a substrate
# failure. Two shapes count: a status-ish word immediately before the code
# ("http 429", "API Error: 401", "status_code=503"), or the code immediately
# followed by its standard reason phrase ("503 service unavailable").
_STATUS_CODE_CONTEXT = (
    r"\b(?:http|https|status|statuscode|status[ _-]code|error|errors|err|code|response)\b"
    r"[^0-9a-z]{0,8}"
)
_AUTH_STATUS_CODES = r"401|403"
_TRANSPORT_STATUS_CODES = r"429|500|502|503|504"
_AUTH_STATUS_REASONS = r"unauthorized|forbidden|permission denied"
_TRANSPORT_STATUS_REASONS = (
    r"too many requests|internal server error|bad gateway|service unavailable|gateway timeout"
)

_AUTH_CODE_RE = re.compile(
    rf"{_STATUS_CODE_CONTEXT}(?:{_AUTH_STATUS_CODES})\b"
    rf"|\b(?:{_AUTH_STATUS_CODES})\s+(?:{_AUTH_STATUS_REASONS})\b"
)
_TRANSPORT_CODE_RE = re.compile(
    rf"{_STATUS_CODE_CONTEXT}(?:{_TRANSPORT_STATUS_CODES})\b"
    rf"|\b(?:{_TRANSPORT_STATUS_CODES})\s+(?:{_TRANSPORT_STATUS_REASONS})\b"
)

# Runner-emitted markers that stand in for output that never existed.
_NO_OUTPUT_MARKERS: tuple[str, ...] = (
    "timeout: agent exceeded",
    "claude_stream_no_text",
    "sandbox_capability_profile_unsupported",
    "cli not found",
)

# ``failure_code`` values that are substrate statements by construction.
_TRANSPORT_FAILURE_CODES: frozenset[str] = frozenset(
    {"rate_limit", "provider_internal_error", "connection_reset"}
)
_AUTH_FAILURE_CODES: frozenset[str] = frozenset({"auth_error", "authentication_error"})
_MODEL_EXECUTION_FAILURE_CODES: frozenset[str] = frozenset(
    {
        # Keep in sync with the API/CLI loop runners: these codes mean the
        # model executed and the run reached a work-ending condition, even if
        # the final failure text is a runner-authored summary line and the
        # transport billed $0.00 (e.g. a localhost endpoint).
        "agent_ended_without_result",
        "max_iterations_reached",
        "no_submit_completion",
        "stuck_pattern",
    }
)


def _text_of(result: Any) -> str:
    return " ".join(str(getattr(result, "output", "") or "").split()).lower()


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)


def _looks_like_auth_failure(text: str) -> bool:
    """True when ``text`` is a credential rejection (phrase or 401/403 in context)."""
    return _matches(text, _AUTH_PATTERNS) or bool(_AUTH_CODE_RE.search(text))


def _looks_like_transport_failure(text: str) -> bool:
    """True when ``text`` is a transport drop / provider outage (phrase or 5xx/429)."""
    return _matches(text, _TRANSPORT_PATTERNS) or bool(_TRANSPORT_CODE_RE.search(text))


def _has_model_usage_evidence(result: Any) -> bool:
    """True when token/cost telemetry proves a model actually ran."""
    for usage in tuple(getattr(result, "model_usage", ()) or ()):
        for usage_field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "thinking_tokens",
        ):
            value = getattr(usage, usage_field, 0)
            if isinstance(value, int) and value > 0:
                return True
        cost = getattr(usage, "cost_usd", None)
        if isinstance(cost, (int, float)) and cost > 0.0:
            return True
    return False


def _has_runner_artifacts(result: Any) -> bool:
    """True when the runner captured provider/session artifacts for the call."""
    if getattr(result, "session_id", None):
        return True
    raw = getattr(result, "raw", None)
    return isinstance(raw, dict) and bool(raw)


def zero_charge_no_model_artifacts(result: Any) -> bool:
    """True when the failed invocation billed nothing and shows no model artifacts."""
    if result is None or getattr(result, "success", False):
        return False
    exit_code = getattr(result, "exit_code", None)
    if not isinstance(exit_code, int) or exit_code == 0:
        return False
    cost = getattr(result, "cost_usd", None)
    if cost != 0.0:
        return False
    if str(getattr(result, "partial_output", "") or "").strip():
        return False
    if getattr(result, "tool_trace", ()):
        return False
    if getattr(result, "structured_data", None):
        return False
    if getattr(result, "dev_handoff", None):
        return False
    if _has_runner_artifacts(result):
        return False
    if _has_model_usage_evidence(result):
        return False
    failure_code = str(getattr(result, "failure_code", "") or "").lower()
    return failure_code not in _MODEL_EXECUTION_FAILURE_CODES


def carries_agent_text(output: str | None) -> bool:
    """True when *output* is text an agent produced rather than a stand-in marker.

    The runners write a marker (``CLAUDE_STREAM_NO_TEXT: ...``, ``TIMEOUT: Agent
    exceeded ...``) into ``output`` when the stream carried no agent text at all.
    Quoting one back to an operator as "what the agent said" describes a run that
    said nothing as if it had spoken, so callers surfacing captured agent words
    ask here first (#2427).
    """
    text = " ".join(str(output or "").split()).lower()
    return bool(text) and not _matches(text, _NO_OUTPUT_MARKERS)


def produced_model_output(result: Any) -> bool:
    """Return True when this invocation carries evidence a model actually spoke.

    A successful invocation always did. A failed one did when the runner
    salvaged partial text, a tool trace, structured data, or a parsed handoff —
    a timeout that killed an agent mid-work still produced real, paid-for model
    judgment. A failed invocation whose only content is a recognized substrate
    marker (auth rejection, transport drop, runner no-output marker) did not.

    Unrecognized failure text is ordinarily treated as model output. The one
    extra no-judgment heuristic is a failed non-zero process result that billed
    exactly $0.00 and carries no runner/provider/model artifacts at all: that
    shape is a runner/process failure, not a silent model judgment.
    """
    if result is None:
        return False
    if getattr(result, "success", False):
        return True
    if str(getattr(result, "partial_output", "") or "").strip():
        return True
    if getattr(result, "tool_trace", ()):
        return True
    if getattr(result, "structured_data", None):
        return True
    if getattr(result, "dev_handoff", None):
        return True
    text = _text_of(result)
    if not text:
        return False
    if getattr(result, "startup_failure", False):
        return False
    if _matches(text, _NO_OUTPUT_MARKERS):
        return False
    if _has_model_usage_evidence(result):
        return True
    failure_code = str(getattr(result, "failure_code", "") or "").lower()
    if failure_code in _MODEL_EXECUTION_FAILURE_CODES:
        return True
    if _looks_like_auth_failure(text) or _looks_like_transport_failure(text):
        return False
    if _has_runner_artifacts(result):
        return True
    if zero_charge_no_model_artifacts(result):
        return False
    return True


def classify_failure_category(result: Any) -> str:
    """Return the substrate-failure category for a no-model-output invocation."""
    if getattr(result, "startup_failure", False):
        return CATEGORY_STARTUP
    failure_code = str(getattr(result, "failure_code", "") or "").lower()
    if failure_code in _AUTH_FAILURE_CODES:
        return CATEGORY_AUTH
    if failure_code == "timeout":
        return CATEGORY_TIMEOUT
    if failure_code in _TRANSPORT_FAILURE_CODES:
        return CATEGORY_TRANSPORT
    if failure_code == "agent_ended_without_result":
        return CATEGORY_NO_RESULT
    text = _text_of(result)
    if _looks_like_auth_failure(text):
        return CATEGORY_AUTH
    if "timeout: agent exceeded" in text:
        return CATEGORY_TIMEOUT
    if "cli not found" in text or "sandbox_capability_profile_unsupported" in text:
        return CATEGORY_STARTUP
    if _looks_like_transport_failure(text):
        return CATEGORY_TRANSPORT
    return CATEGORY_PROCESS


@dataclass(frozen=True)
class AgentInvocationFailure:
    """One agent invocation that failed without producing any model output.

    Pure data: the phase that made the call, the substrate category, and the
    observed values (exit code, runner failure code, provider text) that drove
    the classification. Rendered verbatim into the audit trail (CONVENTIONS §6)
    so an operator can see the real error rather than reconstruct it.
    """

    phase: str
    category: str
    exit_code: int | None = None
    failure_code: str | None = None
    detail: str = ""
    profile_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in ALLOWED_FAILURE_CATEGORIES:
            raise ValueError(
                f"agent-failure category {self.category!r} not in "
                f"{sorted(ALLOWED_FAILURE_CATEGORIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "category": self.category,
            "exit_code": self.exit_code,
            "failure_code": self.failure_code,
            "detail": self.detail,
            "profile_name": self.profile_name,
            **({"extra": dict(self.extra)} if self.extra else {}),
        }

    def summary(self) -> str:
        """One-line operator-facing rendering."""
        parts = [f"category={self.category}"]
        if self.profile_name:
            parts.insert(0, self.profile_name)
        if self.exit_code is not None:
            parts.append(f"exit={self.exit_code}")
        if self.failure_code:
            parts.append(f"failure_code={self.failure_code}")
        if self.detail:
            parts.append(self.detail[:200])
        return " ".join(parts)


def classify_agent_failure(
    result: Any,
    *,
    phase: str,
    profile_name: str | None = None,
    detail: str | None = None,
) -> AgentInvocationFailure | None:
    """Classify ``result`` as a no-judgment invocation failure, or ``None``.

    Returns ``None`` whenever the invocation produced model output — including
    a failed invocation that still left partial text or a tool trace behind.
    Only a genuine "no model answered" event yields a record.
    """
    if produced_model_output(result):
        return None
    _detail = detail
    if _detail is None:
        _detail = " ".join(str(getattr(result, "output", "") or "").split())[:500]
    return AgentInvocationFailure(
        phase=phase,
        category=classify_failure_category(result),
        exit_code=getattr(result, "exit_code", None),
        failure_code=getattr(result, "failure_code", None),
        detail=_detail,
        profile_name=profile_name or (getattr(result, "profile_name", None) or None),
    )


def record_invocation_failure(
    state: "CoordinatorState",
    failure: AgentInvocationFailure,
) -> None:
    """Append ``failure`` to the run's audit-visible invocation-failure ledger.

    Recording alone never taints the run: a single reviewer losing its transport
    inside an otherwise complete run is degradation, not a fabricated verdict.
    Taint is applied only by :func:`mark_infrastructure_abort`, when the run's
    terminal outcome itself is "no judgment obtained".
    """
    state.agent_invocation_failures.append(failure.to_dict())


def mark_infrastructure_abort(
    state: "CoordinatorState",
    failure: AgentInvocationFailure,
    *,
    message: str,
) -> None:
    """Terminate the run as an infrastructure abort rather than a story verdict.

    Records the structured cause on ``state.infrastructure_failure``, sets the
    machine-readable ``error_type`` every outcome-code consumer reads, and
    writes a **failed** ``agent_judgment_obtained`` trust check. The last piece
    is what makes the run non-teaching: the centralized ADR-0006 clause 4 gate
    already excludes tainted runs from every router-consumed aggregate, so a
    revoked credential can never become durable evidence about a story.
    """
    state.infrastructure_failure = {
        "message": message,
        **failure.to_dict(),
    }
    state.error_type = ERROR_TYPE_INFRASTRUCTURE_ABORT
    state.trust_checks[TRUST_CHECK_AGENT_JUDGMENT] = make_trust_check(
        check=TRUST_CHECK_AGENT_JUDGMENT,
        result=CHECK_FAIL,
        producer=TRUST_CHECK_PRODUCER,
        evidence={"reason": message, **failure.to_dict()},
    )


def is_infrastructure_abort(state: Any) -> bool:
    """True when the run terminated because no agent judgment was obtainable."""
    return bool(getattr(state, "infrastructure_failure", None))


def record_degraded_pool(
    state: "CoordinatorState",
    *,
    phase: str,
    pool_size: int,
    lost: list[str],
    failures: list[AgentInvocationFailure],
) -> None:
    """Record that ``phase`` completed with a pool shrunk by substrate failures.

    A phase that lost participants to infrastructure failure has not produced a
    result of the same kind as one that completed intact (issue #1951), so the
    loss is preserved on run state and rendered into the audit rather than
    disappearing into a smaller-but-equal-looking pool.
    """
    state.degraded_pools.append(
        {
            "phase": phase,
            "pool_size": pool_size,
            "lost": list(lost),
            "remaining": pool_size - len(lost),
            "failures": [f.to_dict() for f in failures],
        }
    )


def classify_launch_failure(result: object) -> str | None:
    """Return a one-line launch reason when an agent never started, else None.

    Reads the runner's own launch signal (``startup_failure`` / a launch
    ``failure_code``) rather than pattern-matching provider text — classification
    of what a subprocess did belongs to the runner; the coordinator only decides
    what the classification means for the phase that asked.

    The distinction is the module's whole subject applied to advisory steps: an
    agent that never reached the model produced no judgment *and* spent nothing,
    which is a defect in the environment forge launched it into rather than an
    investigation that reached no conclusion. Callers that surface a reason to an
    operator need to say which of the two happened.
    """
    if not getattr(result, "startup_failure", False):
        code = str(getattr(result, "failure_code", "") or "").lower()
        if code != "cli_launch_failure":
            return None
    output = str(getattr(result, "output", "") or "")
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if lines:
        return lines[-1][:500]
    return str(getattr(result, "failure_code", "") or "") or "agent process exited at launch"
