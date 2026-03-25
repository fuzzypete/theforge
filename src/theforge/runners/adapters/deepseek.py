"""DeepSeek provider adapter (thin wrapper on OpenAI)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from theforge.agent_types import AgentResult
from theforge.runners.adapters.openai import (
    _make_openai_chat_adapter,
    _run_openai_chat,
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


def _make_deepseek_adapter(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
) -> Any:
    """Build DeepSeek adapter (reuses OpenAI Chat Completions adapter)."""
    client = _deepseek_client(profile, secrets)
    return _make_openai_chat_adapter(profile, secrets, client=client)
