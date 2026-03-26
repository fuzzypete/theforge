"""Anthropic provider: single-shot runner, message translator, adapter, finalizer, loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from theforge.agent_types import AgentResult, ModelUsage
from theforge.runners.loop_manager import (
    SUBMIT_REVIEW,
    AgentLoopManager,
    Finalizer,
    LoopTurn,
    ProviderAdapter,
    ToolCallRequest,
    _estimate_cost,
)
from theforge.schemas import review_json_schema

if TYPE_CHECKING:
    from theforge.config import ModelProfile


# ── Single-shot runner (no tools) ────────────────────────────────────


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


# ── Message translator ────────────────────────────────────────────────


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


# ── Provider adapter ──────────────────────────────────────────────────


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


# ── Finalizer ─────────────────────────────────────────────────────────


def _make_anthropic_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> Finalizer:
    """Build an Anthropic finalizer using tool_choice to force submit_review."""
    import anthropic

    from theforge.runners.submit_tools import _build_submit_tools_anthropic

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


# ── Loop-mode entry point ─────────────────────────────────────────────


def _run_loop_anthropic(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run Anthropic provider in agent loop mode."""
    from theforge.runners.submit_tools import _build_registry_tools, _build_submit_tools_anthropic

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
