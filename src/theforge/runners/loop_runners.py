"""Provider-specific adapters, finalizers, and loop entry points.

Each provider (OpenAI, Anthropic, Google, DeepSeek) has:
  - A message translator: internal format → provider wire format
  - A provider adapter: callable that makes one API turn
  - A finalizer: forced structured-output call for timeout/budget recovery
  - A loop entry point: wires the adapter+finalizer into AgentLoopManager

Single-shot runners (no tools) also live here for providers that support them.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from theforge.agent_types import AgentResult, ModelUsage
from theforge.runners.cli import _log, _log_verbose
from theforge.runners.loop_manager import (
    _RESPONSES_API_MODELS,
    SUBMIT_REVIEW,
    AgentLoopManager,
    Finalizer,
    LoopTurn,
    ProviderAdapter,
    ToolCallRequest,
    _estimate_cost,
    _is_local_endpoint,
    _is_reasoning_model,
)
from theforge.schemas import review_json_schema

if TYPE_CHECKING:
    from theforge.config import ModelProfile


# ── Google schema sanitizer ───────────────────────────────────────────


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


# ── OpenAI client helpers ─────────────────────────────────────────────


def _openai_client(profile: "ModelProfile", secrets: dict[str, str] | None = None):  # type: ignore[return]
    """Build an OpenAI client from profile + secrets."""
    import httpx
    import openai

    merged = {**os.environ, **(secrets or {})}
    kwargs: dict[str, Any] = {
        "api_key": merged.get("OPENAI_API_KEY") or "local",
        "http_client": httpx.Client(timeout=httpx.Timeout(profile.timeout_seconds)),
    }
    if profile.base_url:
        kwargs["base_url"] = profile.base_url
    return openai.OpenAI(**kwargs)


def _deepseek_client(profile: "ModelProfile", secrets: dict[str, str] | None = None):  # type: ignore[return]
    """Build an OpenAI-compatible client for the DeepSeek API."""
    import httpx
    import openai

    merged = {**os.environ, **(secrets or {})}
    kwargs: dict[str, Any] = {
        "api_key": merged.get("DEEPSEEK_API_KEY") or "local",
        "base_url": profile.base_url or "https://api.deepseek.com",
        "http_client": httpx.Client(timeout=httpx.Timeout(profile.timeout_seconds)),
    }
    return openai.OpenAI(**kwargs)


def _openai_result(
    profile: "ModelProfile",
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    raw: dict,
    provider: str = "openai",
) -> AgentResult:
    """Build AgentResult from parsed OpenAI-compatible response fields."""
    cost = _estimate_cost(provider, profile.model, input_tokens, output_tokens)
    if _is_local_endpoint(profile.base_url):
        cost = 0.0
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
        raw=raw,
        profile_name=profile.name,
        model_usage=(model_usage,),
        structured_data=json.loads(output_text),
    )


# ── Single-shot runners (no tools) ───────────────────────────────────


def _run_openai_chat(
    prompt: str,
    profile: "ModelProfile",
    secrets: dict[str, str] | None = None,
    client: Any = None,
    provider: str = "openai",
    response_format: dict[str, Any] | None = None,
) -> AgentResult:
    """Run via OpenAI Chat Completions (/v1/chat/completions).

    Args:
        response_format: Override the default response_format. Pass
            ``{"type": "json_object"}`` for providers that don't support
            ``json_schema`` (e.g. DeepSeek).
    """
    if client is None:
        client = _openai_client(profile, secrets)
    schema = review_json_schema()
    if response_format is None:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "review_output", "schema": schema, "strict": True},
        }
    try:
        create_kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": response_format,
        }
        if not _is_reasoning_model(profile.model):
            create_kwargs["temperature"] = 0
        response = client.chat.completions.create(**create_kwargs)
        output_text = response.choices[0].message.content or ""
        usage = response.usage
        return _openai_result(
            profile,
            output_text,
            usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0,
            response.model_dump(),
            provider=provider,
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


def _run_openai_responses(
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Run via OpenAI Responses API (/v1/responses) — required for Codex models."""
    client = _openai_client(profile, secrets)
    schema = review_json_schema()
    try:
        response = client.responses.create(
            model=profile.model,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "review_output",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
        output_text = response.output_text
        usage = response.usage
        return _openai_result(
            profile,
            output_text,
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
            {},
        )
    except Exception as e:
        return AgentResult(
            success=False,
            output=f"OpenAI Responses API error: {e}",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name=profile.name,
        )


def _run_openai(
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Dispatch to Chat Completions or Responses API based on model."""
    if profile.model in _RESPONSES_API_MODELS:
        return _run_openai_responses(prompt, profile, secrets)
    return _run_openai_chat(prompt, profile, secrets)


def _run_deepseek(
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Run via DeepSeek API (OpenAI-compatible Chat Completions).

    DeepSeek supports json_object but not json_schema structured output.
    """
    client = _deepseek_client(profile, secrets)
    return _run_openai_chat(
        prompt,
        profile,
        secrets,
        client=client,
        provider="deepseek",
        response_format={"type": "json_object"},
    )


def _run_anthropic(
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Run agent via Anthropic API."""
    import anthropic

    merged = {**os.environ, **(secrets or {})}
    client = anthropic.Anthropic(
        api_key=merged.get("ANTHROPIC_API_KEY"),
        timeout=profile.timeout_seconds,
    )
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


def _run_google(
    prompt: str,
    profile: "ModelProfile",
    secrets: dict[str, str] | None = None,
    *,
    plain_text: bool = False,
) -> AgentResult:
    """Run agent via Google Gemini API."""
    import google.genai as genai
    import google.genai.types as genai_types

    merged = {**os.environ, **(secrets or {})}
    client = genai.Client(api_key=merged.get("GOOGLE_API_KEY"))

    try:
        if plain_text:
            response = client.models.generate_content(
                model=profile.model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0),
            )
            output_text = response.text
            structured_data = None
        else:
            schema = _sanitize_schema_for_google(review_json_schema())
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


PROVIDER_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_openai,
    "anthropic": _run_anthropic,
    "google": _run_google,
    "deepseek": _run_deepseek,
}


# ── Provider adapters for the agent loop ─────────────────────────────


def _make_openai_usage(usage: Any, model: str) -> ModelUsage | None:
    if usage is None:
        return None
    return ModelUsage(
        model=model,
        input_tokens=getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)),
        output_tokens=getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)),
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=None,
    )


def _translate_messages_openai_chat(messages: list[dict]) -> list[dict]:
    """Translate loop-internal messages to OpenAI Chat Completions format."""
    result = []
    for msg in messages:
        role = msg["role"]
        if role in ("user", "system"):
            result.append({"role": role, "content": msg.get("content", "")})
        elif role == "assistant":
            calls = msg.get("tool_calls", [])
            m: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
            if calls:
                m["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in calls
                ]
            result.append(m)
        elif role == "tool_results":
            for r in msg.get("results", []):
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": r["id"],
                        "content": r["content"],
                    }
                )
    return result


def _make_openai_chat_adapter(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
    client: Any = None,
) -> ProviderAdapter:
    """Build OpenAI Chat Completions adapter for AgentLoopManager."""
    if client is None:
        client = _openai_client(profile, secrets)

    def adapter(messages: list[dict], tools: list[dict]) -> LoopTurn:
        oai_messages = _translate_messages_openai_chat(messages)
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": oai_messages,
        }
        if not _is_reasoning_model(profile.model):
            kwargs["temperature"] = 0
        if tools:
            kwargs["tools"] = tools

        response = client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        usage = _make_openai_usage(response.usage, profile.model)

        tool_calls: list[ToolCallRequest] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, AttributeError):
                    args = {}
                tool_calls.append(
                    ToolCallRequest(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=args,
                    )
                )

        text = msg.content or None
        return LoopTurn(
            tool_calls=tool_calls,
            text_output=text,
            structured_data=None,
            usage=usage,
        )

    return adapter


def _translate_messages_openai_responses(messages: list[dict]) -> list[Any]:
    """Translate loop-internal messages to OpenAI Responses API input format."""
    result: list[Any] = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            result.append({"role": "user", "content": msg.get("content", "")})
        elif role == "assistant":
            calls = msg.get("tool_calls", [])
            for c in calls:
                result.append(
                    {
                        "type": "function_call",
                        "name": c.name,
                        "arguments": json.dumps(c.arguments),
                        "call_id": c.id,
                    }
                )
            text = msg.get("content")
            if text:
                result.append({"role": "assistant", "content": text})
        elif role == "tool_results":
            for r in msg.get("results", []):
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": r["id"],
                        "output": r["content"],
                    }
                )
    return result


def _make_openai_responses_adapter(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> ProviderAdapter:
    """Build OpenAI Responses API adapter for AgentLoopManager."""
    client = _openai_client(profile, secrets)

    def adapter(messages: list[dict], tools: list[dict]) -> LoopTurn:
        input_items = _translate_messages_openai_responses(messages)
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "input": input_items,
        }
        if tools:
            kwargs["tools"] = tools

        response = client.responses.create(**kwargs)

        tool_calls: list[ToolCallRequest] = []
        text_parts: list[str] = []
        for item in response.output:
            item_type = getattr(item, "type", None)
            if item_type == "function_call":
                try:
                    args = json.loads(item.arguments)
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, AttributeError):
                    args = {}
                tool_calls.append(
                    ToolCallRequest(
                        id=getattr(item, "call_id", getattr(item, "id", str(len(tool_calls)))),
                        name=item.name,
                        arguments=args,
                    )
                )
            elif item_type == "message":
                # message items contain content parts
                content = getattr(item, "content", [])
                if isinstance(content, list):
                    for part in content:
                        if getattr(part, "type", None) == "output_text":
                            text_parts.append(getattr(part, "text", ""))
                elif isinstance(content, str):
                    text_parts.append(content)

        usage_obj = getattr(response, "usage", None)
        usage: ModelUsage | None = None
        if usage_obj:
            usage = ModelUsage(
                model=profile.model,
                input_tokens=getattr(usage_obj, "input_tokens", 0),
                output_tokens=getattr(usage_obj, "output_tokens", 0),
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=None,
            )

        text = "\n".join(text_parts) or None
        return LoopTurn(
            tool_calls=tool_calls,
            text_output=text,
            structured_data=None,
            usage=usage,
        )

    return adapter


def _translate_messages_anthropic(messages: list[dict]) -> list[dict]:
    """Translate loop-internal messages to Anthropic API format."""
    result: list[dict] = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            result.append({"role": "user", "content": msg.get("content", "")})
        elif role == "assistant":
            calls = msg.get("tool_calls", [])
            content: list[Any] = []
            text = msg.get("content")
            if text:
                content.append({"type": "text", "text": text})
            for c in calls:
                content.append(
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.name,
                        "input": c.arguments,
                    }
                )
            result.append(
                {"role": "assistant", "content": content or [{"type": "text", "text": ""}]}
            )
        elif role == "tool_results":
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": r["id"],
                    "content": r["content"],
                }
                for r in msg.get("results", [])
            ]
            result.append({"role": "user", "content": tool_results})
    return result


def _make_anthropic_adapter(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> ProviderAdapter:
    """Build Anthropic adapter for AgentLoopManager."""
    import anthropic

    merged = {**os.environ, **(secrets or {})}
    client = anthropic.Anthropic(
        api_key=merged.get("ANTHROPIC_API_KEY"),
        timeout=profile.timeout_seconds,
    )

    def adapter(messages: list[dict], tools: list[dict]) -> LoopTurn:
        anth_messages = _translate_messages_anthropic(messages)
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "max_tokens": 8192,
            "temperature": 0,
            "messages": anth_messages,
        }
        if tools:
            kwargs["tools"] = tools
            # No forced tool_choice — model is free to call any registered tool

        response = client.messages.create(**kwargs)

        tool_calls: list[ToolCallRequest] = []
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCallRequest(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        usage = ModelUsage(
            model=profile.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            cost_usd=None,
        )

        text = "\n".join(text_parts) or None
        return LoopTurn(
            tool_calls=tool_calls,
            text_output=text,
            structured_data=None,
            usage=usage,
        )

    return adapter


def _translate_messages_google(messages: list[dict]) -> list[dict]:
    """Translate loop-internal messages to Google Gemini contents format."""
    result: list[dict] = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            result.append({"role": "user", "parts": [{"text": msg.get("content", "")}]})
        elif role == "assistant":
            calls = msg.get("tool_calls", [])
            parts: list[dict] = []
            text = msg.get("content")
            if text:
                parts.append({"text": text})
            for c in calls:
                parts.append({"function_call": {"name": c.name, "args": c.arguments}})
            result.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif role == "tool_results":
            parts = [
                {
                    "function_response": {
                        "name": r["name"],
                        "response": {"output": r["content"]},
                    }
                }
                for r in msg.get("results", [])
            ]
            if parts:
                result.append({"role": "user", "parts": parts})
    return result


def _make_google_adapter(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> ProviderAdapter:
    """Build Google Gemini adapter for AgentLoopManager.

    Gemini struggles to produce valid function calls for complex schemas
    (nested objects, many required fields) after large context accumulation.
    When the model stops calling tools or produces a MALFORMED_FUNCTION_CALL,
    we make a finalization call using response_schema to force valid structured
    JSON output — the same mechanism used by the single-shot _run_google path.
    """
    import google.genai as genai
    import google.genai.types as genai_types

    merged = {**os.environ, **(secrets or {})}
    client = genai.Client(api_key=merged.get("GOOGLE_API_KEY"))

    # Pre-build the sanitized schema for finalization calls
    _finalize_schema = _sanitize_schema_for_google(review_json_schema())

    def _needs_finalization(response: Any) -> bool:
        """Check if the response indicates the model is done exploring
        but failed to call submit_review."""
        for candidate in response.candidates or []:
            fr = str(getattr(candidate, "finish_reason", ""))
            if "MALFORMED" in fr:
                return True
        for candidate in response.candidates or []:
            parts = (candidate.content.parts if candidate.content else None) or []
            # Model returned text instead of a tool call — it's trying to
            # deliver the review as prose instead of via submit_review.
            has_text = any(hasattr(p, "text") and p.text for p in parts)
            has_tool = any(hasattr(p, "function_call") and p.function_call for p in parts)
            if has_text and not has_tool:
                return True
        return False

    def _finalize(contents: list[dict], usage_so_far: "ModelUsage | None") -> LoopTurn:
        """Make a constrained-output call to extract the structured review."""
        _log_verbose("  ⚠ Gemini finalization — switching to response_schema")
        # Append instruction to submit
        finalize_contents = list(contents)
        finalize_contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Now deliver your code review verdict as structured JSON. "
                            "Include verdict, summary, findings, story_compliance, "
                            "and test_coverage."
                        )
                    }
                ],
            }
        )
        config = genai_types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_finalize_schema,
        )
        response = client.models.generate_content(
            model=profile.model,
            contents=finalize_contents,
            config=config,
        )
        output_text = response.text or ""
        structured_data = None
        if output_text.strip():
            try:
                structured_data = json.loads(output_text)
            except json.JSONDecodeError:
                pass

        usage_meta = response.usage_metadata
        usage: ModelUsage | None = None
        if usage_meta:
            usage = ModelUsage(
                model=profile.model,
                input_tokens=usage_meta.prompt_token_count or 0,
                output_tokens=usage_meta.candidates_token_count or 0,
                cache_read_tokens=getattr(usage_meta, "cached_content_token_count", 0) or 0,
                cache_creation_tokens=0,
                cost_usd=None,
            )

        return LoopTurn(
            tool_calls=[],
            text_output=output_text,
            structured_data=structured_data,
            usage=usage,
        )

    def adapter(messages: list[dict], tools: list[dict]) -> LoopTurn:
        contents = _translate_messages_google(messages)
        google_tools = None
        if tools:
            google_tools = [genai_types.Tool(function_declarations=tools)]

        config = genai_types.GenerateContentConfig(
            temperature=0,
            tools=google_tools,
        )
        response = client.models.generate_content(
            model=profile.model,
            contents=contents,
            config=config,
        )

        # Check if model is done exploring but didn't call submit_review.
        # If so, make a finalization call with response_schema to force
        # valid structured output.
        if _needs_finalization(response):
            return _finalize(contents, None)

        tool_calls: list[ToolCallRequest] = []
        text_parts: list[str] = []

        for candidate in response.candidates or []:
            parts = (candidate.content.parts if candidate.content else None) or []
            for part in parts:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    try:
                        args = dict(fc.args) if fc.args is not None else {}
                    except (TypeError, AttributeError):
                        args = {}
                    tool_calls.append(
                        ToolCallRequest(
                            id=f"call_{len(tool_calls)}",
                            name=fc.name,
                            arguments=args,
                        )
                    )
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)

        if not tool_calls and not text_parts:
            feedback = getattr(response, "prompt_feedback", None)
            if feedback:
                block_reason = getattr(feedback, "block_reason", None)
                if block_reason:
                    text_parts.append(f"[Blocked: {block_reason}]")

        usage_meta = response.usage_metadata
        usage: ModelUsage | None = None
        if usage_meta:
            usage = ModelUsage(
                model=profile.model,
                input_tokens=usage_meta.prompt_token_count or 0,
                output_tokens=usage_meta.candidates_token_count or 0,
                cache_read_tokens=getattr(usage_meta, "cached_content_token_count", 0) or 0,
                cache_creation_tokens=0,
                cost_usd=None,
            )

        text = "\n".join(text_parts) or None
        return LoopTurn(
            tool_calls=tool_calls,
            text_output=text,
            structured_data=None,
            usage=usage,
        )

    return adapter


# ── Finalizers (forced structured output on timeout) ──────────────────


def _make_openai_chat_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
    client: Any = None,
) -> Finalizer:
    """Build a Chat Completions finalizer using response_format: json_schema."""
    if client is None:
        client = _openai_client(profile, secrets)
    schema = review_json_schema()

    def finalizer(messages: list[dict]) -> LoopTurn:
        oai_messages = _translate_messages_openai_chat(messages)
        oai_messages.append(
            {
                "role": "user",
                "content": (
                    "Time is up. Deliver your code review verdict now as structured JSON. "
                    "Include verdict, summary, findings, story_compliance, and test_coverage."
                ),
            }
        )
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": oai_messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "review_output", "schema": schema, "strict": True},
            },
        }
        if not _is_reasoning_model(profile.model):
            kwargs["temperature"] = 0

        response = client.chat.completions.create(**kwargs)
        output_text = response.choices[0].message.content or ""
        usage = _make_openai_usage(response.usage, profile.model)

        structured_data = None
        if output_text.strip():
            try:
                structured_data = json.loads(output_text)
            except json.JSONDecodeError:
                pass

        return LoopTurn(
            tool_calls=[],
            text_output=output_text,
            structured_data=structured_data,
            usage=usage,
        )

    return finalizer


def _make_deepseek_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
    client: Any = None,
) -> Finalizer:
    """Build a finalizer for DeepSeek using response_format: json_object.

    DeepSeek's Chat Completions API supports JSON mode (json_object) but not
    structured output (json_schema).  Using json_schema returns HTTP 400.
    """
    if client is None:
        client = _deepseek_client(profile, secrets)

    def finalizer(messages: list[dict]) -> LoopTurn:
        oai_messages = _translate_messages_openai_chat(messages)
        oai_messages.append(
            {
                "role": "user",
                "content": (
                    "Time is up. Deliver your code review verdict now as JSON. "
                    "Include verdict, summary, findings, story_compliance, and test_coverage. "
                    "Output only valid JSON with no markdown fences."
                ),
            }
        )
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": oai_messages,
            "response_format": {"type": "json_object"},
        }
        if not _is_reasoning_model(profile.model):
            kwargs["temperature"] = 0

        response = client.chat.completions.create(**kwargs)
        output_text = response.choices[0].message.content or ""
        usage = _make_openai_usage(response.usage, profile.model)

        structured_data = None
        if output_text.strip():
            try:
                structured_data = json.loads(output_text)
            except json.JSONDecodeError:
                pass

        return LoopTurn(
            tool_calls=[],
            text_output=output_text,
            structured_data=structured_data,
            usage=usage,
        )

    return finalizer


def _make_openai_responses_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> Finalizer:
    """Build a Responses API finalizer using text.format: json_schema."""
    client = _openai_client(profile, secrets)
    schema = review_json_schema()

    def finalizer(messages: list[dict]) -> LoopTurn:
        input_items = _translate_messages_openai_responses(messages)
        input_items.append(
            {
                "role": "user",
                "content": (
                    "Time is up. Deliver your code review verdict now as structured JSON. "
                    "Include verdict, summary, findings, story_compliance, and test_coverage."
                ),
            }
        )
        response = client.responses.create(
            model=profile.model,
            input=input_items,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "review_output",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
        output_text = response.output_text or ""
        usage_obj = getattr(response, "usage", None)
        usage: ModelUsage | None = None
        if usage_obj:
            usage = ModelUsage(
                model=profile.model,
                input_tokens=getattr(usage_obj, "input_tokens", 0),
                output_tokens=getattr(usage_obj, "output_tokens", 0),
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=None,
            )

        structured_data = None
        if output_text.strip():
            try:
                structured_data = json.loads(output_text)
            except json.JSONDecodeError:
                pass

        return LoopTurn(
            tool_calls=[],
            text_output=output_text,
            structured_data=structured_data,
            usage=usage,
        )

    return finalizer


def _make_anthropic_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> Finalizer:
    """Build an Anthropic finalizer using tool_choice to force submit_review."""
    import anthropic

    from theforge.runners.api import _build_submit_tools_anthropic

    merged = {**os.environ, **(secrets or {})}
    client = anthropic.Anthropic(
        api_key=merged.get("ANTHROPIC_API_KEY"),
        timeout=profile.timeout_seconds,
    )

    def finalizer(messages: list[dict]) -> LoopTurn:
        anth_messages = _translate_messages_anthropic(messages)
        anth_messages.append(
            {
                "role": "user",
                "content": (
                    "Time is up. Deliver your code review verdict now. "
                    "Include verdict, summary, findings, story_compliance, and test_coverage."
                ),
            }
        )
        submit_tool = _build_submit_tools_anthropic()[0]  # submit_review
        response = client.messages.create(
            model=profile.model,
            max_tokens=8192,
            temperature=0,
            messages=anth_messages,
            tools=[submit_tool],
            tool_choice={"type": "tool", "name": SUBMIT_REVIEW},
        )

        structured_data = None
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == SUBMIT_REVIEW:
                structured_data = block.input if isinstance(block.input, dict) else {}

        usage = ModelUsage(
            model=profile.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            cost_usd=None,
        )

        output = (
            json.dumps(structured_data, indent=2) if structured_data else "\n".join(text_parts)
        )
        return LoopTurn(
            tool_calls=[],
            text_output=output,
            structured_data=structured_data,
            usage=usage,
        )

    return finalizer


def _make_google_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> Finalizer:
    """Build a Google Gemini finalizer using response_schema."""
    import google.genai as genai
    import google.genai.types as genai_types

    merged = {**os.environ, **(secrets or {})}
    client = genai.Client(api_key=merged.get("GOOGLE_API_KEY"))
    finalize_schema = _sanitize_schema_for_google(review_json_schema())

    def finalizer(messages: list[dict]) -> LoopTurn:
        contents = _translate_messages_google(messages)
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Time is up. Deliver your code review verdict now "
                            "as structured JSON. Include verdict, summary, "
                            "findings, story_compliance, and test_coverage."
                        )
                    }
                ],
            }
        )
        config = genai_types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=finalize_schema,
        )
        response = client.models.generate_content(
            model=profile.model,
            contents=contents,
            config=config,
        )
        output_text = response.text or ""
        structured_data = None
        if output_text.strip():
            try:
                structured_data = json.loads(output_text)
            except json.JSONDecodeError:
                pass

        usage_meta = response.usage_metadata
        usage: ModelUsage | None = None
        if usage_meta:
            usage = ModelUsage(
                model=profile.model,
                input_tokens=usage_meta.prompt_token_count or 0,
                output_tokens=usage_meta.candidates_token_count or 0,
                cache_read_tokens=getattr(usage_meta, "cached_content_token_count", 0) or 0,
                cache_creation_tokens=0,
                cost_usd=None,
            )

        return LoopTurn(
            tool_calls=[],
            text_output=output_text,
            structured_data=structured_data,
            usage=usage,
        )

    return finalizer


# ── Loop-mode entry points ────────────────────────────────────────────


def _run_loop_openai(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run OpenAI provider in agent loop mode."""
    import openai

    from theforge.runners.api import _build_registry_tools, _build_submit_tools_openai

    tools = _build_registry_tools(profile)
    is_responses = profile.model in _RESPONSES_API_MODELS

    if is_responses:
        tool_schemas = [
            t.to_openai_responses_function() for t in tools
        ] + _build_submit_tools_openai(responses_api=True)
        adapter = _make_openai_responses_adapter(profile, secrets)
        finalizer = _make_openai_responses_finalizer(profile, secrets)
    else:
        tool_schemas = [t.to_openai_function() for t in tools] + _build_submit_tools_openai(
            responses_api=False
        )
        adapter = _make_openai_chat_adapter(profile, secrets)
        finalizer = _make_openai_chat_finalizer(profile, secrets)

    manager = AgentLoopManager(
        profile=profile,
        provider="openai",
        working_dir=working_dir,
        tools=tools,
        provider_adapter=adapter,
        finalizer=finalizer,
    )
    try:
        result = manager.run(
            initial_messages=[{"role": "user", "content": prompt}],
            tool_schemas=tool_schemas,
        )
    except Exception as exc:
        if isinstance(exc, openai.BadRequestError) and "tool" in str(exc).lower():
            # Local model doesn't support tool calling — fall back to single-shot text mode.
            _log(
                f"  ⚠ {profile.name or profile.model} tool-call 400 — "
                "falling back to single-shot text mode"
            )
            fallback_prompt = (
                prompt
                + "\n\n[SYSTEM] Respond with a JSON object matching the review output schema. "
                "Do not use tool calls."
            )
            return PROVIDER_RUNNERS["openai"](fallback_prompt, profile, secrets)
        raise

    # Zero cost for local endpoints — token counts are meaningless for self-hosted models.
    if _is_local_endpoint(profile.base_url):
        zeroed_usage = tuple(
            dataclasses.replace(u, cost_usd=0.0) for u in (result.model_usage or ())
        )
        result = dataclasses.replace(result, cost_usd=0.0, model_usage=zeroed_usage)

    return result


def _run_loop_anthropic(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run Anthropic provider in agent loop mode."""
    from theforge.runners.api import _build_registry_tools, _build_submit_tools_anthropic

    tools = _build_registry_tools(profile)
    tool_schemas = [t.to_anthropic_tool() for t in tools] + _build_submit_tools_anthropic()
    adapter = _make_anthropic_adapter(profile, secrets)
    finalizer = _make_anthropic_finalizer(profile, secrets)

    manager = AgentLoopManager(
        profile=profile,
        provider="anthropic",
        working_dir=working_dir,
        tools=tools,
        provider_adapter=adapter,
        finalizer=finalizer,
    )
    return manager.run(
        initial_messages=[{"role": "user", "content": prompt}],
        tool_schemas=tool_schemas,
    )


def _run_loop_google(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run Google provider in agent loop mode."""
    from theforge.runners.api import _build_registry_tools, _build_submit_tools_google

    tools = _build_registry_tools(profile)
    # Google adapter wraps declarations into genai_types.Tool objects internally
    tool_schemas = [t.to_google_declaration() for t in tools] + _build_submit_tools_google()
    adapter = _make_google_adapter(profile, secrets)
    finalizer = _make_google_finalizer(profile, secrets)

    manager = AgentLoopManager(
        profile=profile,
        provider="google",
        working_dir=working_dir,
        tools=tools,
        provider_adapter=adapter,
        finalizer=finalizer,
    )
    return manager.run(
        initial_messages=[{"role": "user", "content": prompt}],
        tool_schemas=tool_schemas,
    )


def _run_loop_deepseek(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run DeepSeek provider in agent loop mode (OpenAI Chat Completions)."""
    from theforge.runners.api import _build_registry_tools, _build_submit_tools_openai

    client = _deepseek_client(profile, secrets)
    tools = _build_registry_tools(profile)
    tool_schemas = [t.to_openai_function() for t in tools] + _build_submit_tools_openai(
        responses_api=False
    )
    adapter = _make_openai_chat_adapter(profile, secrets, client=client)
    finalizer = _make_deepseek_finalizer(profile, secrets, client=client)

    manager = AgentLoopManager(
        profile=profile,
        provider="deepseek",
        working_dir=working_dir,
        tools=tools,
        provider_adapter=adapter,
        finalizer=finalizer,
    )
    return manager.run(
        initial_messages=[{"role": "user", "content": prompt}],
        tool_schemas=tool_schemas,
    )


_LOOP_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_loop_openai,
    "anthropic": _run_loop_anthropic,
    "google": _run_loop_google,
    "deepseek": _run_loop_deepseek,
}
