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


# DeepSeek expresses reasoning effort on three levels where forge's routing axis
# has three of its own. The mapping is stated rather than passed through because
# the two vocabularies only *look* alike: DeepSeek has no "medium", and sending a
# level it does not define would either 400 or be silently remapped by the
# provider — in which case the effort forge recorded is not the effort that ran.
_REASONING_EFFORT_TO_DEEPSEEK: dict[str, str] = {
    "low": "low",
    "medium": "high",
    "high": "max",
}


def deepseek_request_kwargs(profile: "ModelProfile") -> dict[str, Any]:
    """Build the request-level controls for one DeepSeek invocation.

    DeepSeek moved reasoning from a model-name distinction to a request
    parameter, so an entry banded as a reasoning model is only *actually* a
    reasoning model if the request says so. ``profile.reasoning_mode`` carries
    that declaration from the catalog entry (``invocation.reasoning_mode``) and
    ``profile.reasoning_effort`` carries the score-driven level the router
    resolved for this phase.

    Returns an empty mapping when the profile declares neither, so a profile
    constructed outside the registry path keeps the provider's own defaults
    rather than having a mode invented for it. Every DeepSeek request — single
    shot, tool loop and finalizer — is built through here so the three cannot
    drift apart.

    The control travels under ``extra_body``, not as a top-level keyword.
    ``thinking`` is DeepSeek's own extension to the Chat Completions body; the
    OpenAI SDK's ``create()`` declares an explicit parameter list with no
    ``**kwargs``, so passing it directly raises ``TypeError`` in the client
    before a request is ever sent — a mode that is never requested and an
    invocation that never happens. ``extra_body`` is the SDK's supported channel
    for exactly this: merged into the JSON body verbatim.

    ``reasoning_effort`` deliberately goes inside the ``thinking`` object rather
    than into the SDK's own top-level ``reasoning_effort`` parameter. They are
    different fields with different vocabularies — DeepSeek nests its own and
    accepts ``max``, which OpenAI's does not define.
    """
    thinking: dict[str, Any] = {}
    if profile.reasoning_mode is not None:
        thinking["type"] = profile.reasoning_mode
    effort = _REASONING_EFFORT_TO_DEEPSEEK.get(profile.reasoning_effort or "")
    if effort is not None:
        thinking["reasoning_effort"] = effort
    if not thinking:
        return {}
    return {"extra_body": {"thinking": thinking}}


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
        extra_kwargs=deepseek_request_kwargs(profile),
    )


def _make_deepseek_adapter(
    profile: "ModelProfile",
    secrets: dict[str, str] | None,
    client: Any = None,
) -> Any:
    """Build DeepSeek adapter (reuses OpenAI Chat Completions adapter).

    Forces response_format=json_object so DeepSeek's tool call submissions
    are always valid JSON — prevents the APPROVE+P1 YAML contradiction bug
    that causes schema validation failures in both plan review and code review.
    """
    if client is None:
        client = _deepseek_client(profile, secrets)
    return _make_openai_chat_adapter(
        profile,
        secrets,
        client=client,
        response_format={"type": "json_object"},
        extra_kwargs=deepseek_request_kwargs(profile),
    )
