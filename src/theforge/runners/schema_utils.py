"""Provider-agnostic schema helpers, pricing, and protocol types."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from theforge.agent_types import ModelUsage
from theforge.schemas import review_json_schema

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
    ("anthropic", "claude-opus-4-6"): (15.00, 75.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    ("google", "gemini-3.1-pro-preview"): (2.00, 12.00),  # ≤200k tokens
    ("google", "gemini-3.1-pro-preview-customtools"): (2.00, 12.00),
    ("google", "gemini-2.5-pro"): (1.25, 10.00),  # ≤200k tokens
    ("google", "gemini-2.5-flash"): (0.30, 2.50),
    ("google", "gemini-2.5-flash-lite"): (0.10, 0.40),
    ("google", "gemini-2.0-flash"): (0.10, 0.40),
    ("google", "gemini-2.0-flash-lite"): (0.075, 0.30),
    ("deepseek", "deepseek-chat"): (0.27, 1.10),  # V3 alias
    ("deepseek", "deepseek-r1"): (0.55, 2.19),
    ("deepseek", "deepseek-v3"): (0.27, 1.10),
    ("deepseek", "deepseek-reasoner"): (0.55, 2.19),  # R1 alias
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


# OpenAI reasoning models that do not support temperature=0.
# These models only accept temperature=1 (the default).
_REASONING_MODEL_RE = re.compile(r"^o\d")


def _is_reasoning_model(model: str) -> bool:
    """Return True for reasoning models that do not support temperature=0."""
    return bool(_REASONING_MODEL_RE.match(model)) or model.startswith(
        ("deepseek-r1", "deepseek-reasoner")
    )


# Submit tool names — loop-internal, not in TOOL_REGISTRY
SUBMIT_REVIEW = "submit_review"
SUBMIT_PLAN_REVIEW = "submit_plan_review"
_SUBMIT_TOOL_NAMES = {SUBMIT_REVIEW, SUBMIT_PLAN_REVIEW}

# Phases that must NOT receive submit tools or review finalizers
_NO_SUBMIT_PHASES = {"preflight", "dev"}

# Max consecutive malformed tool calls before aborting
_MAX_MALFORMED = 3

# Default max loop iterations
_DEFAULT_MAX_ITERATIONS = 50


_MISSING_PRICING_WARNED: set[tuple[str, str]] = set()


def _estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    thinking_tokens: int = 0,
) -> float | None:
    """Estimate cost from pricing table; returns None if model unknown."""
    price = PRICING_TABLE.get((provider, model))
    if price is None:
        key = (provider, model)
        if key not in _MISSING_PRICING_WARNED:
            logger.warning(
                "Missing pricing entry for provider=%s model=%s; cost cannot be estimated. "
                "Add this model to PRICING_TABLE so audit and budget totals stay accurate.",
                provider,
                model,
            )
            _MISSING_PRICING_WARNED.add(key)
        return None
    billable_output_tokens = output_tokens + thinking_tokens
    return ((input_tokens / 1_000_000) * price[0]) + (
        (billable_output_tokens / 1_000_000) * price[1]
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
            "required": ["verdict", "summary", "findings"],
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES"],
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
                            "severity": {"type": "string", "enum": ["P1", "P1-impl", "P2"]},
                            "description": {"type": "string"},
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
