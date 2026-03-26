"""OpenAI provider: client, single-shot runners, message translators, adapter, finalizer, loop."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from theforge.agent_types import AgentResult, ModelUsage
from theforge.runners.cli import _log, _log_verbose
from theforge.runners.loop_manager import (
    _RESPONSES_API_MODELS,
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


# ── Message translators ───────────────────────────────────────────────


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


# ── Provider adapters ─────────────────────────────────────────────────


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


# ── Finalizers ────────────────────────────────────────────────────────


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


# ── Loop-mode entry point ─────────────────────────────────────────────


def _run_loop_openai(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run OpenAI provider in agent loop mode."""
    import openai

    from theforge.runners.submit_tools import _build_registry_tools, _build_submit_tools_openai

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
            from theforge.runners.loop_runners import PROVIDER_RUNNERS

            return PROVIDER_RUNNERS["openai"](fallback_prompt, profile, secrets)
        raise

    # Zero cost for local endpoints — token counts are meaningless for self-hosted models.
    if _is_local_endpoint(profile.base_url):
        zeroed_usage = tuple(
            dataclasses.replace(u, cost_usd=0.0) for u in (result.model_usage or ())
        )
        result = dataclasses.replace(result, cost_usd=0.0, model_usage=zeroed_usage)

    return result
