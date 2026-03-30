"""Google Gemini provider adapter."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from theforge.agent_types import AgentResult, ModelUsage
from theforge.runners.cli import _log_verbose
from theforge.runners.schema_utils import (
    LoopTurn,
    ProviderAdapter,
    ToolCallRequest,
    _estimate_cost,
    _sanitize_schema_for_google,
)

if TYPE_CHECKING:
    from theforge.config import ModelProfile


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

    from theforge.schemas import review_json_schema

    merged = {**os.environ, **(secrets or {})}
    api_key = merged.get("GOOGLE_API_KEY") or merged.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

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

    from theforge.schemas import review_json_schema

    merged = {**os.environ, **(secrets or {})}
    api_key = merged.get("GOOGLE_API_KEY") or merged.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    # Pre-build the sanitized schema for finalization calls
    _finalize_schema = _sanitize_schema_for_google(review_json_schema())

    def _needs_finalization(response: Any) -> bool:
        """Check if the response indicates the model is done exploring
        but failed to call submit_review."""
        for candidate in response.candidates or []:
            fr = str(getattr(candidate, "finish_reason", ""))
            if "MALFORMED" in fr:
                return True
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
