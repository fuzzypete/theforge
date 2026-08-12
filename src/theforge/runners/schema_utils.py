"""Provider-agnostic schema helpers, pricing, and protocol types."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, Protocol

from theforge.agent_types import ModelUsage
from theforge.runners.rate_registry import (
    CACHED_INPUT_RATE_MULT,
    PRICING_TABLE,
    AccountingMode,
    ModelRates,
    RateEntry,
    RateSource,
    identity_of,
    make_identity,
)
from theforge.schemas import plan_review_json_schema, review_json_schema

from . import rate_registry as _rate_registry

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


def _sanitize_schema_for_google(schema: dict) -> dict:
    """Strip JSON Schema features unsupported by Google's API.

    Google's response_schema does not support:
    - additionalProperties
    - anyOf / oneOf / allOf
    - $schema, $id, $ref

    This recursively cleans the schema so it can be passed to Gemini.
    """
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("additionalProperties", "$schema", "$id", "$ref"):
            continue
        if key == "anyOf":
            # Simplify anyOf to first non-null type
            for option in value:
                if isinstance(option, dict) and option.get("type") != "null":
                    cleaned.update(_sanitize_schema_for_google(option))
                    break
            else:
                # All null — just use string
                cleaned["type"] = "string"
            continue
        if isinstance(value, dict):
            cleaned[key] = _sanitize_schema_for_google(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _sanitize_schema_for_google(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


# Models we intentionally route to the Responses API (/v1/responses).
# Some current OpenAI models (for example gpt-5.4 and o4-mini) accept both
# Responses and Chat Completions. Keep this set limited to models that are
# Responses-only in practice, so the name does not imply "all supported
# Responses models".
_RESPONSES_ONLY_MODELS: set[str] = {
    "gpt-5.1-codex-mini",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5-codex",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
}


def uses_openai_responses_api(model: str) -> bool:
    """Return True when this OpenAI model should be sent to /v1/responses."""
    return model in _RESPONSES_ONLY_MODELS


# Keep this list limited to models for which issue #2377 observed the provider
# reject tool-bearing Chat Completions requests unless reasoning_effort='none'
# is supplied. Do not broaden it without a concrete failing probe.
_CHAT_COMPLETIONS_TOOL_REASONING_NONE_MODELS: frozenset[str] = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    }
)

# Leave empty until a shipped model is observed to lack both a supported Chat
# Completions tool shape and a Responses path. "unsupported" is fail-closed
# metadata, not something we should guess from a model-family prefix.
_CHAT_COMPLETIONS_TOOL_UNSUPPORTED_MODELS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OpenAIFunctionToolRequestShape:
    """How an OpenAI model can satisfy a required function-tools request.

    ``transport`` is the request surface that can carry function tools for this
    model. ``"responses"`` means the model should be sent to ``/v1/responses``;
    ``"chat"`` means Chat Completions can carry the request, optionally with a
    provider-required override such as ``reasoning_effort='none'``; and
    ``"unsupported"`` means the shipped metadata has no supported tool-bearing
    shape for this model, so the run must fail closed instead of discarding the
    required capability.
    """

    transport: Literal["responses", "chat", "unsupported"]
    chat_reasoning_effort: str | None = None

    def chat_extra_kwargs(self) -> dict[str, Any]:
        """Return Chat Completions kwargs required for a tool-bearing request."""
        if self.chat_reasoning_effort is None:
            return {}
        return {"reasoning_effort": self.chat_reasoning_effort}


def openai_function_tool_request_shape(model: str) -> OpenAIFunctionToolRequestShape:
    """Return the supported OpenAI request shape for required function tools."""
    if uses_openai_responses_api(model):
        return OpenAIFunctionToolRequestShape("responses")
    if model in _CHAT_COMPLETIONS_TOOL_UNSUPPORTED_MODELS:
        return OpenAIFunctionToolRequestShape("unsupported")
    if model in _CHAT_COMPLETIONS_TOOL_REASONING_NONE_MODELS:
        return OpenAIFunctionToolRequestShape("chat", chat_reasoning_effort="none")
    return OpenAIFunctionToolRequestShape("chat")


def sampling_control_kwargs() -> dict[str, Any]:
    """Optional sampling controls to merge into any provider request.

    Empty by design, and deliberately provider- and model-agnostic: no runner
    behaviour depends on a fixed ``temperature``, and providers increasingly
    reject the value outright for current models. Deciding this per call site —
    or from the shape of a model's name — encodes the set of models known when
    the code was written and misclassifies everything released afterwards, so
    every request builder in this subsystem routes through here instead.

    Re-introducing a control requires two things, neither of which is a model
    name: a runner behaviour that demonstrably depends on it, and evidence the
    selected provider accepts it for the selected model.
    """
    return {}


# Submit tool names — loop-internal, not in TOOL_REGISTRY
SUBMIT_REVIEW = "submit_review"
SUBMIT_PLAN_REVIEW = "submit_plan_review"
_SUBMIT_TOOL_NAMES = {SUBMIT_REVIEW, SUBMIT_PLAN_REVIEW}

# Phases that must NOT receive submit tools or review finalizers
_NO_SUBMIT_PHASES = {"preflight", "dev"}

# The phase whose forced-output contract is the plan-review one. Every other
# submit-capable phase reviews code.
PLAN_REVIEW_PHASE = "plan_review"


@dataclass(frozen=True)
class ForcedOutputContract:
    """Schema + instruction pair for a phase's forced-structured-output fallback.

    Forcing a phase into another phase's contract turns a complete answer into an
    unparseable one, so every provider fallback selects its contract through
    :func:`forced_output_contract` rather than hardcoding the code-review shape.
    """

    verdict_kind: str  # "code review" / "plan review"
    schema_name: str  # response_format / json_schema name
    submit_tool: str  # submit tool to force where the provider supports it
    json_schema: dict
    fields: str  # human-readable field list for the instruction

    def instruction(
        self,
        *,
        prefix: str = "Time is up. ",
        form: str | None = "structured JSON",
        suffix: str = "",
    ) -> str:
        """Render the instruction that accompanies the forced schema."""
        as_clause = f" as {form}" if form else ""
        return (
            f"{prefix}Deliver your {self.verdict_kind} verdict now{as_clause}. "
            f"Include {self.fields}.{suffix}"
        )


def forced_output_contract(phase: str | None) -> ForcedOutputContract:
    """Return the forced-output contract belonging to ``phase``.

    Phases in ``_NO_SUBMIT_PHASES`` never reach a forced-output fallback (they use
    ``noop_finalizer``), so only the two review shapes are represented here.
    """
    if phase == PLAN_REVIEW_PHASE:
        return ForcedOutputContract(
            verdict_kind="plan review",
            schema_name="plan_review_output",
            submit_tool=SUBMIT_PLAN_REVIEW,
            json_schema=plan_review_json_schema(),
            fields="verdict, summary, findings, and criteria_coverage",
        )
    return ForcedOutputContract(
        verdict_kind="code review",
        schema_name="review_output",
        submit_tool=SUBMIT_REVIEW,
        json_schema=review_json_schema(),
        fields="verdict, summary, findings, story_compliance, and test_coverage",
    )


# Max consecutive malformed tool calls before aborting
_MAX_MALFORMED = 3

# Default max loop iterations
_DEFAULT_MAX_ITERATIONS = 75


_MISSING_PRICING_WARNED: set[tuple[str, str, str | None]] = set()


# Providers whose reported prompt-token count INCLUDES the tokens served from
# cache. ``_estimate_cost`` documents ``cached_input_tokens`` as a subset of
# ``input_tokens`` and subtracts it, which is right for these and wrong for the
# others: Anthropic reports ``cache_read_input_tokens`` *alongside* an
# ``input_tokens`` that already excludes them, so handing its cache count to the
# estimator would delete real uncached input from the bill.
_CACHE_READS_INSIDE_INPUT: frozenset[str] = frozenset({"openai", "deepseek"})


def cache_reads_are_subset_of_input(provider: str) -> bool:
    """Is this provider's cache-read count part of its reported input count?"""
    return provider in _CACHE_READS_INSIDE_INPUT


def _catalog_rates() -> dict[tuple[str, str], ModelRates]:
    """Rate cards carried by the shipped model catalog, keyed like PRICING_TABLE.

    The catalog is where a model's price is *declared alongside the identity it
    was recorded for* — a price with no ``pricing_provenance`` is a literal
    nothing can vouch for (see config/pricing.py), so those entries are skipped
    and fall through to the table below. Where two catalog entries share a
    ``(provider, model)`` and disagree on price (the same model offered over both
    CLI and API), the pair is dropped rather than arbitrated: an ambiguous rate
    is not a better answer than the explicit table.

    Imported lazily so this module keeps its light import graph, and recomputed
    per call is avoided by the module-level cache below.
    """
    from theforge.config.models import AGENT_REGISTRY  # noqa: PLC0415
    from theforge.config.pricing import PRICING_PROVENANCE_LOCAL_ENDPOINT  # noqa: PLC0415

    rates: dict[tuple[str, str], ModelRates] = {}
    ambiguous: set[tuple[str, str]] = set()
    for spec in AGENT_REGISTRY.values():
        if spec.input_cost_per_mtok is None or spec.output_cost_per_mtok is None:
            continue
        if not spec.pricing_attributable:
            continue
        if spec.pricing_provenance == PRICING_PROVENANCE_LOCAL_ENDPOINT:
            # A local endpoint's 0.00 is true of the *endpoint*, not of the model
            # name — the same name reached over a vendor's API is billed. The
            # runners already zero a genuinely local invocation by base_url, so
            # importing the figure here would only mis-price the other case.
            continue
        key = (spec.provider, spec.model)
        entry = ModelRates(
            input_per_mtok=spec.input_cost_per_mtok,
            output_per_mtok=spec.output_cost_per_mtok,
            cached_input_per_mtok=spec.cached_input_cost_per_mtok,
        )
        existing = rates.get(key)
        if existing is not None and existing != entry:
            ambiguous.add(key)
            continue
        rates[key] = entry
    for key in ambiguous:
        rates.pop(key, None)
    return rates


_CATALOG_RATES_CACHE: dict[tuple[str, str], ModelRates] | None = None


def catalog_rates() -> dict[tuple[str, str], ModelRates]:
    """Memoized :func:`_catalog_rates`. ``AGENT_REGISTRY`` is built once at import."""
    global _CATALOG_RATES_CACHE
    if _CATALOG_RATES_CACHE is None:
        _CATALOG_RATES_CACHE = _catalog_rates()
    return _CATALOG_RATES_CACHE


def pricing_for(provider: str, model: str) -> ModelRates | None:
    """Return the rate card for ``(provider, model)``, or None if none is known.

    **Transport-blind, and therefore the no-registry baseline only.** Accounting
    resolves through :func:`rates_for`, which is keyed by
    ``(provider, model, transport)``; this function answers when no registry is
    installed, which is the case for unit tests and for any process that never
    loads a configuration. Its behaviour is deliberately unchanged so that
    baseline is bit-for-bit what it was before #2335.

    Catalog first, :data:`PRICING_TABLE` second. The catalog is the surface where
    a rate is declared next to the identity it was recorded for and the date that
    identity was last checked against the provider, so it is the one that gets to
    answer when both do. The table remains for identities the catalog does not
    describe — vendor CLI shorthands resolve to concrete billed names that are
    never catalog entries in their own right (``claude-sonnet-4-6``), and stream
    events report those names directly.
    """
    catalog = catalog_rates().get((provider, model))
    if catalog is not None:
        return catalog
    price = PRICING_TABLE.get((provider, model))
    if price is None:
        return None
    return ModelRates(input_per_mtok=price[0], output_per_mtok=price[1])


def rate_card_confirmed(provider: str, model: str, *, today: date | None = None) -> bool:
    """Is the rate card used for ``(provider, model)`` attached to a checked identity?

    True only when the figures came from a catalog entry whose upstream
    identifier has been checked against the provider inside the verification
    window. A rate read from :data:`PRICING_TABLE` is never confirmed: that table
    records no identity and no date, which is exactly how it carried DeepSeek's
    superseded rates across two revisions of the provider's pricing without
    anything noticing.

    Consumed by the API cost-provenance stamp so an estimate off a rate card that
    may no longer apply is distinguishable from one that is current (#2352).
    """
    from theforge.config.models import AGENT_REGISTRY  # noqa: PLC0415

    if catalog_rates().get((provider, model)) is None:
        return False
    reference = today or date.today()
    return any(
        spec.identity.confirmed_on(reference)
        for spec in AGENT_REGISTRY.values()
        if (spec.provider, spec.model) == (provider, model)
    )


def entry_for(
    provider: str | None,
    model: str | None,
    transport: str | None,
) -> RateEntry:
    """Resolve one dispatched identity to its accounting entry.

    This is the seam between the two halves of the fix, and it lives here rather
    than in :mod:`rate_registry` because it is the only function that needs
    both: the strict, transport-respecting registry lookup, and the
    transport-blind :func:`pricing_for` baseline that answers when no
    configuration has been loaded. Keeping the baseline on this side is what lets
    ``rate_registry`` stay an import leaf that configuration load can consume.

    With a registry installed the answer is an exact ``(provider, model,
    transport)`` match and nothing else — a miss is unpriced, and no other
    transport's rate is borrowed. With nothing installed the answer is exactly
    what ``pricing_for`` returned before #2335.
    """
    identity = make_identity(provider, model, transport)
    if identity is not None:
        entry = _rate_registry.resolve(identity)
        if entry is not None:
            return entry
    # Either nothing is installed, or the caller could not name a transport and
    # so has no concrete key to look up. Both take today's transport-blind
    # baseline, unchanged.
    if not provider or not model:
        return RateEntry(identity=identity)
    rates = pricing_for(provider, model)
    return RateEntry(
        identity=identity,
        rates=rates,
        mode=AccountingMode.UNKNOWN,
        source=RateSource.BASELINE if rates is not None else RateSource.NONE,
        origin="no-registry-baseline",
    )


def rates_for(
    provider: str | None,
    model: str | None,
    transport: str | None,
) -> ModelRates | None:
    """Rate card for the identity that ran, or None when it is not priced."""
    return entry_for(provider, model, transport).rates


def entry_for_profile(profile: Any) -> RateEntry:
    """Resolve the entry for the identity ``profile`` would dispatch on.

    ``identity_of`` returns None for a profile carrying neither a provider nor a
    transport; that resolves to an explicitly unpriced entry rather than a
    partial key that could never match.
    """
    identity = identity_of(profile)
    if identity is None:
        return RateEntry(identity=None)
    return entry_for(identity.provider, identity.model, identity.transport)


def _estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    transport: str | None = None,
    thinking_tokens: int = 0,
    cached_input_tokens: int = 0,
    cached_input_rate_mult: float = CACHED_INPUT_RATE_MULT,
) -> float | None:
    """Estimate cost from the rate card in force; returns None if model unknown.

    ``transport`` names the transport kind (``"cli"`` / ``"api"``) the tokens
    being priced were actually spent on. It is what makes the lookup identify
    the thing that *dispatched* rather than a model name two transports can
    share (#2335). It defaults to None purely so an unconverted caller still
    compiles; None takes the transport-blind :func:`pricing_for` baseline. Every
    call site in this repository passes it.

    ``cached_input_tokens`` is the SUBSET of ``input_tokens`` that was served from
    the provider's prompt cache. It is billed at the provider's own published
    cache rate when the catalog entry declares one, and otherwise at
    ``cached_input_rate_mult`` of the input rate. Callers that cannot distinguish
    the cache tier leave it at 0 and get the flat-rate behaviour unchanged.
    """
    rates = rates_for(provider, model, transport)
    if rates is None:
        # Keyed on the transport too, so the same model name unpriced over two
        # transports warns about each rather than reporting one and hiding one.
        key = (provider, model, transport)
        if key not in _MISSING_PRICING_WARNED:
            logger.warning(
                "Missing pricing entry for provider=%s model=%s transport=%s; cost cannot "
                "be estimated. Declare a cost block on the model's catalog entry (or add "
                "it to PRICING_TABLE) so audit and budget totals stay accurate.",
                provider,
                model,
                transport or "unspecified",
            )
            _MISSING_PRICING_WARNED.add(key)
        return None
    # Clamp rather than trust: a provider reporting more cached tokens than input
    # tokens would otherwise produce a negative uncached count and understate spend.
    cached = max(0, min(cached_input_tokens, input_tokens))
    uncached_input_tokens = input_tokens - cached
    billable_output_tokens = output_tokens + thinking_tokens
    cached_rate = (
        rates.cached_input_per_mtok
        if rates.cached_input_per_mtok is not None
        else rates.input_per_mtok * cached_input_rate_mult
    )
    return (
        ((uncached_input_tokens / 1_000_000) * rates.input_per_mtok)
        + ((cached / 1_000_000) * cached_rate)
        + ((billable_output_tokens / 1_000_000) * rates.output_per_mtok)
    )


# ── Provider-agnostic intermediate types ──────────────────────────────


@dataclass
class ToolCallRequest:
    """Provider-agnostic representation of a tool call from the model."""

    id: str
    name: str
    arguments: dict
    # Google Gemini: required for -customtools variants.
    thought_signature: bytes | str | None = None


@dataclass
class LoopTurn:
    """Unified result of one API call, regardless of provider."""

    tool_calls: list[ToolCallRequest]  # empty = model is done
    text_output: str | None  # final text when no tool calls
    structured_data: dict | None  # final structured output when available
    usage: ModelUsage | None  # token usage for this turn
    # Provider-reported chain of thought, where the provider returns one as a
    # field separate from ``text_output`` (DeepSeek's ``reasoning_content``).
    # Load-bearing, not merely observability: a provider that reports one may
    # require it back on the next request to continue a tool-calling
    # conversation, so the loop records it and the translators replay it.
    reasoning_content: str | None = None


class ProviderAdapter(Protocol):
    """Protocol for provider adapters used by AgentLoopManager."""

    def __call__(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> LoopTurn: ...


class Finalizer(Protocol):
    """Protocol for forced-output finalization when the loop runs out of budget.

    Called with the full conversation history when the agent hits a wall-clock
    or iteration timeout. Returns a LoopTurn with structured_data extracted
    via provider-specific constrained output (response_format, tool_choice,
    response_schema).
    """

    def __call__(self, messages: list[dict]) -> LoopTurn: ...


def noop_finalizer(messages: list[dict]) -> LoopTurn:
    """No-op finalizer for non-review phases (dev, preflight).

    On timeout, these phases don't produce structured review output — the
    coordinator handles their timeouts via exit-code / output parsing. This
    finalizer just signals loop termination without coercing review-shaped JSON.
    """
    return LoopTurn(tool_calls=[], text_output=None, structured_data=None, usage=None)


# ── Submit tool schemas ───────────────────────────────────────────────


def _submit_review_schema() -> dict:
    """Schema for submit_review tool — matches review_json_schema()."""
    return {
        "name": SUBMIT_REVIEW,
        "description": (
            "Submit the final structured code review. Call this when you have finished "
            "inspecting the codebase and are ready to deliver your verdict."
        ),
        "parameters": review_json_schema(),
    }


def _submit_plan_review_schema() -> dict:
    """Schema for submit_plan_review tool — for plan agent review."""
    return {
        "name": SUBMIT_PLAN_REVIEW,
        "description": (
            "Submit the final plan review verdict. Call this when you have finished "
            "verifying the implementation plan against the codebase."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdict", "summary", "findings", "criteria_coverage"],
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["APPROVE", "REJECT"],
                    "description": "Overall verdict on the plan.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-line summary of the review.",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["severity", "description"],
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["P0", "P1", "P1-impl", "P2"],
                            },
                            "description": {"type": "string"},
                        },
                    },
                },
                "criteria_coverage": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["criterion", "covered", "plan_section"],
                        "properties": {
                            "criterion": {"type": "string"},
                            "covered": {"type": "boolean"},
                            "plan_section": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def _build_submit_tools_openai(responses_api: bool = False) -> list[dict]:
    """Build submit tool schemas in OpenAI function format.

    When responses_api=True, uses the flat Responses API format (Codex models).
    Otherwise uses the nested Chat Completions format.
    """
    result = []
    for schema_fn in (_submit_review_schema, _submit_plan_review_schema):
        s = schema_fn()
        if responses_api:
            result.append(
                {
                    "type": "function",
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["parameters"],
                }
            )
        else:
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "description": s["description"],
                        "parameters": s["parameters"],
                    },
                }
            )
    return result


def _build_submit_tools_anthropic() -> list[dict]:
    """Build submit tool schemas in Anthropic tool format."""
    result = []
    for schema_fn in (_submit_review_schema, _submit_plan_review_schema):
        s = schema_fn()
        result.append(
            {
                "name": s["name"],
                "description": s["description"],
                "input_schema": s["parameters"],
            }
        )
    return result


def _build_submit_tools_google() -> list[dict]:
    """Build submit tool function declarations for Google.

    Sanitizes parameters to strip additionalProperties and other unsupported
    JSON Schema features.
    """
    result = []
    for schema_fn in (_submit_review_schema, _submit_plan_review_schema):
        s = schema_fn()
        result.append(
            {
                "name": s["name"],
                "description": s["description"],
                "parameters": _sanitize_schema_for_google(s["parameters"]),
            }
        )
    return result
