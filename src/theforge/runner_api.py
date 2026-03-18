"""API-based agent runners for text-judgment agents."""

from __future__ import annotations

import dataclasses
import json
import os
from typing import TYPE_CHECKING, Any, Callable

from .runner import AgentResult, ModelUsage, _log, _log_verbose
from .schemas import review_json_schema

if TYPE_CHECKING:
    from .config import ModelProfile

# ── Pricing table (per 1M tokens) ──────────────────────────────────────

# Fallback for when API response doesn't include cost.
# Key: (provider, model_name)
# Value: (input_cost_per_mtok, output_cost_per_mtok)
PRICING_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "o4-mini"): (1.10, 4.40),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("anthropic", "claude-opus-4-6"): (15.00, 75.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    ("google", "gemini-2.5-pro"): (3.50, 10.50),
    ("google", "gemini-2.0-flash"): (0.10, 0.40),
}


def _estimate_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """Estimate cost from pricing table; returns None if model unknown."""
    price = PRICING_TABLE.get((provider, model))
    if price is None:
        return None
    return ((input_tokens / 1_000_000) * price[0]) + ((output_tokens / 1_000_000) * price[1])


def _run_openai(prompt: str, profile: ModelProfile) -> AgentResult:
    """Run agent via OpenAI API."""
    import openai

    client_kwargs: dict[str, Any] = {"api_key": os.getenv("OPENAI_API_KEY") or "local"}
    if profile.base_url:
        client_kwargs["base_url"] = profile.base_url
    client = openai.OpenAI(**client_kwargs)

    schema = review_json_schema()
    messages = [{"role": "user", "content": prompt}]

    try:
        response = client.chat.completions.create(
            model=profile.model,
            messages=messages,
            temperature=0,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "review_output", "schema": schema, "strict": True},
            },
        )
        output_text = response.choices[0].message.content or ""
        structured_data = json.loads(output_text)
        usage = response.usage

        model_usage: ModelUsage | None = None
        cost: float | None = None
        if usage:
            cost = _estimate_cost(
                "openai", profile.model, usage.prompt_tokens, usage.completion_tokens
            )
            # Local models have no metered cost
            if profile.base_url:
                cost = None
            model_usage = ModelUsage(
                model=profile.model,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=cost,
            )

        return AgentResult(
            success=True,
            output=output_text,
            session_id=None,
            cost_usd=cost,
            exit_code=0,
            raw=response.model_dump(),
            profile_name=profile.name,
            model_usage=(model_usage,) if model_usage else (),
            structured_data=structured_data,
        )
    except Exception as e:
        return AgentResult(
            success=False,
            output=f"OpenAI API error: {e}",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name=profile.name,
        )


def _run_anthropic(prompt: str, profile: ModelProfile) -> AgentResult:
    """Run agent via Anthropic API."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    schema = review_json_schema()

    try:
        response = client.messages.create(
            model=profile.model,
            max_tokens=4096,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "name": "review_output",
                    "description": "Structured output for code review.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": "review_output"},
        )

        output_text = ""
        structured_data = None
        for block in response.content:
            if block.type == "text":
                output_text += block.text
            elif block.type == "tool_use" and block.name == "review_output":
                structured_data = block.input
                output_text += json.dumps(structured_data, indent=2)

        if structured_data is None:
            raise ValueError("Anthropic API did not return structured data in tool_use block.")

        cost = _estimate_cost(
            "anthropic",
            profile.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        model_usage = ModelUsage(
            model=profile.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=cost,
        )

        return AgentResult(
            success=True,
            output=output_text,
            session_id=None,
            cost_usd=cost,
            exit_code=0,
            raw=response.model_dump(),
            profile_name=profile.name,
            model_usage=(model_usage,),
            structured_data=structured_data,
        )
    except Exception as e:
        return AgentResult(
            success=False,
            output=f"Anthropic API error: {e}",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name=profile.name,
        )


def _run_google(prompt: str, profile: ModelProfile) -> AgentResult:
    """Run agent via Google Gemini API."""
    import google.genai as genai
    import google.genai.types as genai_types

    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    schema = review_json_schema()

    try:
        response = client.models.generate_content(
            model=profile.model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0,
            ),
        )
        output_text = response.text
        structured_data = json.loads(output_text)

        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count if usage else 0
        output_tokens = usage.candidates_token_count if usage else 0

        cost = _estimate_cost("google", profile.model, input_tokens, output_tokens)
        model_usage: ModelUsage | None = None
        if input_tokens or output_tokens:
            model_usage = ModelUsage(
                model=profile.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=cost,
            )

        return AgentResult(
            success=True,
            output=output_text,
            session_id=None,
            cost_usd=cost,
            exit_code=0,
            raw={},
            profile_name=profile.name,
            model_usage=(model_usage,) if model_usage else (),
            structured_data=structured_data,
        )
    except Exception as e:
        return AgentResult(
            success=False,
            output=f"Google Gemini API error: {e}",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name=profile.name,
        )


PROVIDER_RUNNERS: dict[str, Callable[[str, ModelProfile], AgentResult]] = {
    "openai": _run_openai,
    "anthropic": _run_anthropic,
    "google": _run_google,
}


def run_api_agent(
    *,
    prompt: str,
    profile: ModelProfile,
    quiet: bool = False,
) -> AgentResult:
    """Run a text-judgment agent via API.

    Dispatches to the appropriate provider adapter. Signature is minimal —
    no working_dir, session_id, or is_pool because API calls are stateless
    and inherently concurrent-safe.
    """
    if not profile.provider:
        return AgentResult(
            success=False,
            output=f"Profile '{profile.name}' is not an API profile.",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name=profile.name,
        )

    runner_fn = PROVIDER_RUNNERS.get(profile.provider)
    if not runner_fn:
        return AgentResult(
            success=False,
            output=f"Unknown API provider: {profile.provider}",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name=profile.name,
        )

    label = profile.name or f"{profile.provider}/{profile.model}"
    if not quiet:
        _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    result = runner_fn(prompt, profile)

    if not quiet:
        status = "OK" if result.success else "FAIL"
        cost_str = f"${result.cost_usd:.3f}" if result.cost_usd is not None else "unknown"
        _log_verbose(f"  ... {label} done | {status} | cost={cost_str}")

    return result
