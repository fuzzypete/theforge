"""Forced-output finalizers for each provider (timeout recovery)."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from theforge.agent_types import ModelUsage
from theforge.runners.schema_utils import (
    Finalizer,
    LoopTurn,
    _build_submit_tools_anthropic,
    _sanitize_schema_for_google,
    forced_output_contract,
    sampling_control_kwargs,
)

if TYPE_CHECKING:
    from theforge.config import ModelProfile


def _make_openai_chat_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
    client: Any = None,
) -> Finalizer:
    """Build a Chat Completions finalizer using response_format: json_schema."""
    from theforge.runners.adapters.openai import (
        _make_openai_usage,
        _openai_client,
        _translate_messages_openai_chat,
    )

    if client is None:
        client = _openai_client(profile, secrets)

    contract = forced_output_contract(profile.phase)
    schema = contract.json_schema
    schema_name = contract.schema_name
    time_up_content = contract.instruction()

    def finalizer(messages: list[dict]) -> LoopTurn:
        oai_messages = _translate_messages_openai_chat(messages)
        oai_messages.append({"role": "user", "content": time_up_content})
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": oai_messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
            **sampling_control_kwargs(),
        }

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
    from theforge.runners.adapters.deepseek import _deepseek_client
    from theforge.runners.adapters.openai import (
        _make_openai_usage,
        _translate_messages_openai_chat,
    )

    if client is None:
        client = _deepseek_client(profile, secrets)

    # DeepSeek has no json_schema mode, so the phase's contract reaches the model
    # through the instruction text alone — it still must be the right one.
    time_up_content = forced_output_contract(profile.phase).instruction(
        form="JSON",
        suffix=" Output only valid JSON with no markdown fences.",
    )

    def finalizer(messages: list[dict]) -> LoopTurn:
        oai_messages = _translate_messages_openai_chat(messages)
        oai_messages.append({"role": "user", "content": time_up_content})
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": oai_messages,
            "response_format": {"type": "json_object"},
            **sampling_control_kwargs(),
        }

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
    from theforge.runners.adapters.openai import (
        _openai_client,
        _translate_messages_openai_responses,
    )

    client = _openai_client(profile, secrets)
    contract = forced_output_contract(profile.phase)
    schema = contract.json_schema

    def finalizer(messages: list[dict]) -> LoopTurn:
        input_items = _translate_messages_openai_responses(messages)
        input_items.append({"role": "user", "content": contract.instruction()})
        response = client.responses.create(
            model=profile.model,
            input=input_items,
            text={
                "format": {
                    "type": "json_schema",
                    "name": contract.schema_name,
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

    from theforge.runners.adapters.anthropic import _translate_messages_anthropic

    merged = {**os.environ, **(secrets or {})}
    client = anthropic.Anthropic(
        api_key=merged.get("ANTHROPIC_API_KEY"),
        timeout=profile.timeout_seconds,
    )
    contract = forced_output_contract(profile.phase)

    def finalizer(messages: list[dict]) -> LoopTurn:
        anth_messages = _translate_messages_anthropic(messages)
        anth_messages.append({"role": "user", "content": contract.instruction(form=None)})
        submit_tool = next(
            t for t in _build_submit_tools_anthropic() if t["name"] == contract.submit_tool
        )
        response = client.messages.create(
            model=profile.model,
            max_tokens=8192,
            messages=anth_messages,
            tools=[submit_tool],
            **sampling_control_kwargs(),
            tool_choice={"type": "tool", "name": contract.submit_tool},
        )

        structured_data = None
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == contract.submit_tool:
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

    from theforge.runners.adapters.google import (
        _make_google_generate_config,
        _make_google_usage,
        _translate_messages_google,
    )

    merged = {**os.environ, **(secrets or {})}
    client = genai.Client(api_key=merged.get("GOOGLE_API_KEY") or merged.get("GEMINI_API_KEY"))
    contract = forced_output_contract(profile.phase)
    finalize_schema = _sanitize_schema_for_google(contract.json_schema)

    def finalizer(messages: list[dict]) -> LoopTurn:
        contents = _translate_messages_google(messages)
        contents.append({"role": "user", "parts": [{"text": contract.instruction()}]})
        config = _make_google_generate_config(
            genai_types,
            profile,
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

        usage = _make_google_usage(profile, response.usage_metadata)

        return LoopTurn(
            tool_calls=[],
            text_output=output_text,
            structured_data=structured_data,
            usage=usage,
        )

    return finalizer
