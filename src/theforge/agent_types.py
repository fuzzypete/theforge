"""Shared agent result types used by both the runners and coordinator packages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from theforge.process_group import ProcessTeardown

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
"""Derived by forge from observed token counts and a rate card whose model
identity is currently confirmed against the provider."""

COST_ESTIMATED_UNCONFIRMED = "estimated_unconfirmed"
"""Derived by forge, but from a rate card that may no longer apply (#2352).

The rate is real and the arithmetic is right; what is missing is any current
evidence that the identifier it was recorded for still designates the model that
was invoked. That is the state every DeepSeek estimate was in while the retired
identifiers were still routable — indistinguishable, at the time, from an
estimate off a rate card in force. A consumer deciding on price can tell how much
the price is worth; see ``config/model_identity.IdentityVerification``.
"""

COST_UNKNOWN = "unknown"
"""No cost was observed. Always paired with ``cost_usd is None``."""

COST_PROVENANCE_VALUES = (
    COST_PROVIDER_REPORTED,
    COST_ESTIMATED,
    COST_ESTIMATED_UNCONFIRMED,
    COST_UNKNOWN,
)

# Every provenance that means "forge computed this", for consumers that care
# about computed-vs-billed rather than about the confidence within computed.
COST_ESTIMATED_VALUES = (COST_ESTIMATED, COST_ESTIMATED_UNCONFIRMED)

# ── Failure codes (#2427) ─────────────────────────────────────────────────
FAILURE_ENDED_WITHOUT_RESULT = "agent_ended_without_result"
"""The agent process ended before emitting its terminal result event.

The shape this names: an agent stops producing output — a dev agent that
delegates to a subagent and then announces it will wait to be notified is the
observed case — and its process is later killed or exits nonzero without ever
reporting a result. The stream it did produce is captured, so the run holds the
agent's own last words about how it ended. Naming the ending gives every
consumer downstream something to receive: without a code, the only fact that
survived was the signal number, which records what was done to the process and
not what went wrong, and a run whose cause was sitting in its own log was
reported as unexplained.
"""

FAILURE_KILLED_BEFORE_OUTPUT = "killed_before_output"
"""The invocation died on a signal having produced nothing at all — it never ran.

Distinct from `FAILURE_ENDED_WITHOUT_RESULT`, and the distinction is the whole
point (#2832). That code names an agent that *ran*: it streamed, it spoke, and
its process then ended before the terminal result event, so the run holds the
agent's own last words. This one names an invocation that never got that far —
not one stream event, no text, no token usage, no session, no tool call. Nothing
about the story was attempted, so the attempt is evidence about the host and not
about the work, and the story's retry allowance must not be charged for it.

The confirmed mechanism, measured against `tests/fake_bin/claude`'s
``silent_stdout_close`` mode and matching all four occurrences in #2832: the
CLI's stdout reaches EOF without a single event, the runner closes stdin and
waits out its short post-stream exit grace, the process is still alive at the
end of it, and the runner ``SIGKILL``s its own process group — which is what
writes ``exit=-9``, ``$0.000`` and ``0 chars`` into the record. Before this code
existed that shape was reported as `FAILURE_ENDED_WITHOUT_RESULT`, identical to
an agent that had worked for an hour and gone quiet.

Who *sent* the signal is deliberately not part of the definition. The observed
kills are forge's own supervision and an external signal is equally possible;
what the record needs to carry, and what the retry decision turns on, is that
the invocation produced no execution artifacts — which is a fact the runner can
prove rather than a guess about signal provenance. The endings that *are* about
the work — a timeout that spent the story's whole allowance, a stuck-pattern
terminate, an operator cancellation — are named by their own codes and are
excluded before this one is ever considered.
"""

KILLED_BEFORE_OUTPUT_MARKER = (
    "KILLED_BEFORE_OUTPUT: the invocation was killed before it produced any "
    "output; nothing about the story was attempted"
)
"""Stand-in text for output that never existed, for the code above.

Every runner writes the same string, so a consumer classifying by text sees one
shape across providers rather than three provider-specific phrasings. Registered
with the coordinator's no-output markers (`coordinator.agent_failure`).
"""


def killed_before_output(*, exit_code: object, produced_output: bool) -> bool:
    """True when this exit is the `FAILURE_KILLED_BEFORE_OUTPUT` shape.

    Shared by every CLI runner so the three of them classify one signal/no-output
    shape identically rather than each growing its own near-miss of it. Kept here,
    beside the constant, because there is no common result-construction boundary
    to hang it on — the runners build their `AgentResult`s at separate sites.

    Two facts, both provable from the invocation itself. A negative ``exit_code``
    is POSIX for "died on signal N", so the process did not choose its own ending;
    ``produced_output`` False says it had emitted nothing when that happened.

    There is deliberately no "was the kill ours?" parameter. Every runner-owned
    ending that *did* let the agent run — timeout, stuck-pattern, cancellation —
    returns its own coded result before reaching any call site of this, so asking
    the question here would only invite a caller to answer it wrongly: the kill in
    the #2832 occurrences *is* forge's own, and gating on provenance would have
    classified the bug as ordinary agent failure.
    """
    return (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code < 0
        and not produced_output
    )


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
    # Set when this invocation's process group did not end on its own and had to
    # be torn down — an agent that shelled out to a long-running command and
    # returned before it did (#2309). None is the ordinary case: the group was
    # empty when the invocation released it. A leaked process leaves no artifact
    # and no cost of its own, so unless the forced kill is carried out of the
    # runner here, the run that spawned the work has no way to say it happened.
    process_teardown: ProcessTeardown | None = None
