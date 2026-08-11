"""OpenAI provider adapter (Chat Completions and Responses API)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from theforge.agent_types import AgentResult, ModelUsage
from theforge.runners.schema_utils import (
    LoopTurn,
    ProviderAdapter,
    ToolCallRequest,
    _estimate_cost,
    sampling_control_kwargs,
    uses_openai_responses_api,
)

if TYPE_CHECKING:
    from theforge.config import ModelProfile


def _is_local_endpoint(base_url: str | None) -> bool:
    """Return True if *base_url* points to a local machine (ollama/vllm etc.)."""
    if not base_url:
        return False
    from urllib.parse import urlparse

    try:
        hostname = urlparse(base_url).hostname or ""
    except Exception:
        return False
    return hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


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


@dataclass(frozen=True)
class _ChatUsage:
    """Token counts read off an OpenAI-compatible ``usage`` object.

    Normalized to forge's convention, which differs from the wire format in one
    place that matters for billing: ``output_tokens`` here EXCLUDES reasoning,
    because ``_estimate_cost`` bills ``output_tokens + thinking_tokens``. On the
    wire ``completion_tokens`` already *includes* ``reasoning_tokens``, so
    carrying both through unchanged would bill the reasoning twice.

    ``cache_read_tokens`` needs no such adjustment: it is a subset of
    ``input_tokens`` on this wire format, which is exactly what
    ``_estimate_cost`` expects of ``cached_input_tokens``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_tokens: int = 0


def _read_chat_usage(usage: Any) -> _ChatUsage:
    """Read a Chat Completions ``usage`` object, including its detail fields.

    Two spellings have to be read for the cache tier because neither is
    universal: ``prompt_cache_hit_tokens`` (DeepSeek) and
    ``prompt_tokens_details.cached_tokens`` (OpenAI). Reasoning output arrives as
    ``completion_tokens_details.reasoning_tokens`` on both.

    Reading these is what makes a separately-billed cache tier and reasoning
    spend accountable at all: discarding them, as this path did before #2352,
    priced every input token at the uncached rate and reported thinking spend as
    zero on a model routed *for* its reasoning.
    """
    if usage is None:
        return _ChatUsage()

    def _int(value: Any) -> int:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    input_tokens = _int(getattr(usage, "prompt_tokens", None)) or _int(
        getattr(usage, "input_tokens", None)
    )
    completion_tokens = _int(getattr(usage, "completion_tokens", None)) or _int(
        getattr(usage, "output_tokens", None)
    )
    cache_read = _int(getattr(usage, "prompt_cache_hit_tokens", None))
    if not cache_read:
        details = getattr(usage, "prompt_tokens_details", None)
        cache_read = _int(getattr(details, "cached_tokens", None)) if details else 0
    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning = _int(getattr(completion_details, "reasoning_tokens", None))
    return _ChatUsage(
        input_tokens=input_tokens,
        output_tokens=max(0, completion_tokens - reasoning),
        cache_read_tokens=min(cache_read, input_tokens) if input_tokens else 0,
        thinking_tokens=reasoning,
    )


def _openai_result(
    profile: "ModelProfile",
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    raw: dict,
    provider: str = "openai",
    *,
    cache_read_tokens: int = 0,
    thinking_tokens: int = 0,
) -> AgentResult:
    """Build AgentResult from parsed OpenAI-compatible response fields."""
    cost = _estimate_cost(
        provider,
        profile.model,
        input_tokens,
        output_tokens,
        thinking_tokens=thinking_tokens,
        cached_input_tokens=cache_read_tokens,
    )
    if _is_local_endpoint(profile.base_url):
        cost = 0.0
    model_usage = ModelUsage(
        model=profile.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=0,
        cost_usd=cost,
        thinking_tokens=thinking_tokens,
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


def _run_openai_chat(
    prompt: str,
    profile: "ModelProfile",
    secrets: dict[str, str] | None = None,
    client: Any = None,
    provider: str = "openai",
    response_format: dict[str, Any] | None = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> AgentResult:
    """Run via OpenAI Chat Completions (/v1/chat/completions).

    ``extra_kwargs`` carries provider-specific request controls the caller has
    already resolved (DeepSeek's ``thinking`` block, for one). It is merged last
    so a provider wrapper can state a control this generic path knows nothing of,
    without that knowledge leaking back into the shared adapter.
    """
    from theforge.schemas import review_json_schema

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
            **sampling_control_kwargs(),
            **(extra_kwargs or {}),
        }
        response = client.chat.completions.create(**create_kwargs)
        output_text = response.choices[0].message.content or ""
        chat_usage = _read_chat_usage(response.usage)
        return _openai_result(
            profile,
            output_text,
            chat_usage.input_tokens,
            chat_usage.output_tokens,
            response.model_dump(),
            provider=provider,
            cache_read_tokens=chat_usage.cache_read_tokens,
            thinking_tokens=chat_usage.thinking_tokens,
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
    from theforge.schemas import review_json_schema

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
    if uses_openai_responses_api(profile.model):
        return _run_openai_responses(prompt, profile, secrets)
    return _run_openai_chat(prompt, profile, secrets)


def _make_openai_usage(usage: Any, model: str) -> ModelUsage | None:
    if usage is None:
        return None
    chat_usage = _read_chat_usage(usage)
    return ModelUsage(
        model=model,
        input_tokens=chat_usage.input_tokens,
        output_tokens=chat_usage.output_tokens,
        cache_read_tokens=chat_usage.cache_read_tokens,
        cache_creation_tokens=0,
        cost_usd=None,
        thinking_tokens=chat_usage.thinking_tokens,
    )


def _translate_messages_openai_chat(messages: list[dict]) -> list[dict]:
    """Translate loop-internal messages to OpenAI Chat Completions format.

    ``reasoning_content`` round-trips: an assistant turn is replayed carrying
    whatever chain of thought the provider reported for it. DeepSeek's thinking
    mode *requires* this once tools are in play — "the intermediate assistant's
    ``reasoning_content`` must participate in the context concatenation and must
    be passed back to the API in all subsequent user interaction turns… if your
    code does not correctly pass back ``reasoning_content``, the API will return
    a 400 error" (api-docs.deepseek.com, thinking-mode guide, retrieved
    2026-08-10). Without it the first tool call succeeds and every continuation
    after it is rejected, which makes a routed reviewer unable to finish a review.

    The rule is *narrower* than "always send" and *wider* than "never send", and
    that is why this is data-driven rather than a provider flag: the field is
    replayed exactly when the provider produced one. OpenAI's Chat Completions
    never returns ``reasoning_content``, so an OpenAI history never carries the
    key and its request bodies are unchanged; DeepSeek returns it on every
    thinking-mode turn, so its histories round-trip it. No branch on provider is
    needed, and no shared code has to hold an opinion about either.

    (For a DeepSeek conversation with no tool calls the same guide says a
    replayed ``reasoning_content`` "will be ignored", so replaying it there is
    harmless. The failing case is only the tool-call one.)
    """
    result = []
    for msg in messages:
        role = msg["role"]
        if role in ("user", "system"):
            result.append({"role": role, "content": msg.get("content", "")})
        elif role == "assistant":
            calls = msg.get("tool_calls", [])
            m: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
            reasoning = msg.get("reasoning_content")
            if reasoning:
                m["reasoning_content"] = reasoning
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
    response_format: dict[str, Any] | None = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> ProviderAdapter:
    """Build OpenAI Chat Completions adapter for AgentLoopManager.

    ``extra_kwargs`` carries provider-specific request controls resolved by the
    caller — see :func:`_run_openai_chat` for why they arrive that way.
    """
    if client is None:
        client = _openai_client(profile, secrets)

    def adapter(messages: list[dict], tools: list[dict]) -> LoopTurn:
        oai_messages = _translate_messages_openai_chat(messages)
        kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": oai_messages,
            **sampling_control_kwargs(),
            **(extra_kwargs or {}),
        }
        if tools:
            kwargs["tools"] = tools
        if response_format is not None:
            kwargs["response_format"] = response_format

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
        # DeepSeek returns the chain of thought as its own field beside
        # ``content``. Reading it here is what stops a reasoning model's
        # reasoning from being invisible to everything downstream — the loop
        # previously kept only ``content``, so a thinking-mode turn that made a
        # tool call left no trace of the thinking at all.
        reasoning = getattr(msg, "reasoning_content", None)
        return LoopTurn(
            tool_calls=tool_calls,
            text_output=text,
            structured_data=None,
            usage=usage,
            reasoning_content=reasoning if isinstance(reasoning, str) and reasoning else None,
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
