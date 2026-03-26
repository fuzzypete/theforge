"""DeepSeek provider: thin OpenAI-compatible wrapper, finalizer, loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from theforge.agent_types import AgentResult
from theforge.runners.loop_manager import (
    AgentLoopManager,
    Finalizer,
    LoopTurn,
    _is_reasoning_model,
)

if TYPE_CHECKING:
    from theforge.config import ModelProfile


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


# ── Single-shot runner (no tools) ────────────────────────────────────


def _run_deepseek(
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Run via DeepSeek API (OpenAI-compatible Chat Completions).

    DeepSeek supports json_object but not json_schema structured output.
    """
    from theforge.runners.runner_openai import _run_openai_chat

    client = _deepseek_client(profile, secrets)
    return _run_openai_chat(
        prompt,
        profile,
        secrets,
        client=client,
        provider="deepseek",
        response_format={"type": "json_object"},
    )


# ── Finalizer ─────────────────────────────────────────────────────────


def _make_deepseek_finalizer(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
    client: Any = None,
) -> Finalizer:
    """Build a finalizer for DeepSeek using response_format: json_object.

    DeepSeek's Chat Completions API supports JSON mode (json_object) but not
    structured output (json_schema).  Using json_schema returns HTTP 400.
    """
    from theforge.agent_types import ModelUsage
    from theforge.runners.runner_openai import (
        _make_openai_usage,
        _translate_messages_openai_chat,
    )

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


# ── Loop-mode entry point ─────────────────────────────────────────────


def _run_loop_deepseek(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run DeepSeek provider in agent loop mode (OpenAI Chat Completions)."""
    from theforge.runners.submit_tools import _build_registry_tools, _build_submit_tools_openai
    from theforge.runners.runner_openai import _make_openai_chat_adapter

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
