"""Provider-agnostic schema helpers, pricing, and protocol types."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

from theforge.agent_types import ModelUsage
from theforge.schemas import plan_review_json_schema, review_json_schema

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


# ── Pricing table (per 1M tokens) ──────────────────────────────────────

# Fallback for when API response doesn't include cost.
# Key: (provider, model_name)
# Value: (input_cost_per_mtok, output_cost_per_mtok)
PRICING_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "o4-mini"): (1.10, 4.40),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-5.1-codex-mini"): (1.50, 6.00),
    ("openai", "gpt-5.1-codex"): (3.00, 12.00),
    ("openai", "gpt-5.1-codex-max"): (6.00, 24.00),
    ("openai", "gpt-5.4-mini"): (0.25, 2.00),
    ("openai", "gpt-5.4"): (1.25, 10.00),
    ("openai", "gpt-5.4-pro"): (15.00, 120.00),
    # Mirrors the input/output_cost_per_mtok declared for this model in
    # forge.yaml. Routing already reads those config values; accounting reads
    # this table, so a model priced in one and absent from the other routes
    # fine and then records cost-unknown, which fails the budget check closed.
    ("openai", "gpt-5.5"): (5.00, 30.00),
    ("anthropic", "claude-opus-4-6"): (15.00, 75.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    # Mirrors the figures the `haiku` shorthand carries and the pinned
    # `anthropic/claude-haiku-4-5/cli` catalog entry attributes to this name.
    ("anthropic", "claude-haiku-4-5"): (1.00, 5.00),
    ("google", "gemini-3.5-flash"): (1.50, 9.00),  # mirrors forge.yaml overlay
    ("google", "gemini-3.1-pro-preview"): (2.00, 12.00),  # ≤200k tokens
    ("google", "gemini-3.1-pro-preview-customtools"): (2.00, 12.00),
    ("google", "gemini-2.5-pro"): (1.25, 10.00),  # ≤200k tokens
    ("google", "gemini-2.5-flash"): (0.30, 2.50),
    ("google", "gemini-2.5-flash-lite"): (0.10, 0.40),
    ("google", "gemini-2.0-flash"): (0.10, 0.40),
    ("google", "gemini-2.0-flash-lite"): (0.075, 0.30),
    # DeepSeek is deliberately absent. Its rates — including the cache-hit tier
    # it bills separately, which this table has no column for — are declared on
    # the catalog entries in config/data/models.yaml and read through
    # :func:`pricing_for`. The rows that used to sit here were the third
    # hand-maintained copy of a rate card and outlived two of its revisions
    # (#2352); the retired identifiers they priced now record no price at all.
}

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


_MISSING_PRICING_WARNED: set[tuple[str, str]] = set()

# Fraction of the uncached input rate charged for a cache HIT, for providers that
# express their cache tier that way. OpenAI and Anthropic both bill cached input
# at 10% of the normal input rate. A provider that publishes an independent
# cache-hit rate instead (DeepSeek bills roughly 2%, which no fixed fraction of
# the uncached rate approximates) states it on its catalog entry and is priced
# from that figure rather than from this multiplier — see :class:`ModelRates`.
CACHED_INPUT_RATE_MULT = 0.1


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


@dataclass(frozen=True)
class ModelRates:
    """The rate card in force for one ``(provider, model)``.

    ``cached_input_per_mtok`` is ``None`` for a provider that has no separately
    published cache tier; the generic :data:`CACHED_INPUT_RATE_MULT` discount
    applies in that case. Zero is a real rate and is honoured as one.
    """

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None


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


def _estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    thinking_tokens: int = 0,
    cached_input_tokens: int = 0,
    cached_input_rate_mult: float = CACHED_INPUT_RATE_MULT,
) -> float | None:
    """Estimate cost from the rate card in force; returns None if model unknown.

    ``cached_input_tokens`` is the SUBSET of ``input_tokens`` that was served from
    the provider's prompt cache. It is billed at the provider's own published
    cache rate when the catalog entry declares one, and otherwise at
    ``cached_input_rate_mult`` of the input rate. Callers that cannot distinguish
    the cache tier leave it at 0 and get the flat-rate behaviour unchanged.
    """
    rates = pricing_for(provider, model)
    if rates is None:
        key = (provider, model)
        if key not in _MISSING_PRICING_WARNED:
            logger.warning(
                "Missing pricing entry for provider=%s model=%s; cost cannot be estimated. "
                "Declare a cost block on the model's catalog entry (or add it to "
                "PRICING_TABLE) so audit and budget totals stay accurate.",
                provider,
                model,
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
