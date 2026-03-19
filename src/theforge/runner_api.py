"""API-based agent runners for text-judgment agents."""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from .runner import AgentResult, ModelUsage, _log, _log_verbose
from .schemas import review_json_schema
from .tool_runtime import TOOL_REGISTRY, ToolDef

if TYPE_CHECKING:
    from .config import ModelProfile


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


# ── Pricing table (per 1M tokens) ──────────────────────────────────────

# Fallback for when API response doesn't include cost.
# Key: (provider, model_name)
# Value: (input_cost_per_mtok, output_cost_per_mtok)
PRICING_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "o4-mini"): (1.10, 4.40),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-5.1-codex-mini"): (1.50, 6.00),
    ("openai", "gpt-5.1-codex"): (3.00, 12.00),
    ("openai", "gpt-5.1-codex-max"): (6.00, 24.00),
    ("anthropic", "claude-opus-4-6"): (15.00, 75.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    ("google", "gemini-2.5-pro"): (3.50, 10.50),
    ("google", "gemini-2.5-flash"): (0.15, 0.60),
    ("google", "gemini-2.0-flash"): (0.10, 0.40),
}

# Models that use the Responses API (/v1/responses) instead of Chat Completions.
# Codex models are agentic and only available on this newer endpoint.
_RESPONSES_API_MODELS: set[str] = {
    "gpt-5.1-codex-mini",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5-codex",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
}

# OpenAI reasoning models that do not support temperature=0.
# These models only accept temperature=1 (the default).
_REASONING_MODEL_RE = re.compile(r"^o\d")


def _is_reasoning_model(model: str) -> bool:
    """Return True for OpenAI reasoning models (o1, o3, o4-mini, etc.)."""
    return bool(_REASONING_MODEL_RE.match(model))


# Submit tool names — loop-internal, not in TOOL_REGISTRY
SUBMIT_REVIEW = "submit_review"
SUBMIT_PLAN_REVIEW = "submit_plan_review"
_SUBMIT_TOOL_NAMES = {SUBMIT_REVIEW, SUBMIT_PLAN_REVIEW}

# Max consecutive malformed tool calls before aborting
_MAX_MALFORMED = 3

# Default max loop iterations
_DEFAULT_MAX_ITERATIONS = 25


def _estimate_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """Estimate cost from pricing table; returns None if model unknown."""
    price = PRICING_TABLE.get((provider, model))
    if price is None:
        return None
    return ((input_tokens / 1_000_000) * price[0]) + ((output_tokens / 1_000_000) * price[1])


# ── Provider-agnostic intermediate types ──────────────────────────────


@dataclass
class ToolCallRequest:
    """Provider-agnostic representation of a tool call from the model."""

    id: str
    name: str
    arguments: dict


@dataclass
class LoopTurn:
    """Unified result of one API call, regardless of provider."""

    tool_calls: list[ToolCallRequest]  # empty = model is done
    text_output: str | None  # final text when no tool calls
    structured_data: dict | None  # final structured output when available
    usage: ModelUsage | None  # token usage for this turn


class ProviderAdapter(Protocol):
    """Protocol for provider adapters used by AgentLoopManager."""

    def __call__(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> LoopTurn: ...


class Finalizer(Protocol):
    """Protocol for forced-output finalization when the loop runs out of budget.

    Called with the full conversation history when the agent hits a wall-clock
    or iteration timeout. Returns a LoopTurn with structured_data extracted
    via provider-specific constrained output (response_format, tool_choice,
    response_schema).
    """

    def __call__(self, messages: list[dict]) -> LoopTurn: ...


# ── Submit tool schemas ───────────────────────────────────────────────


def _submit_review_schema() -> dict:
    """Schema for submit_review tool — matches review_json_schema()."""
    return {
        "name": SUBMIT_REVIEW,
        "description": (
            "Submit the final structured code review. Call this when you have finished "
            "inspecting the codebase and are ready to deliver your verdict."
        ),
        "parameters": review_json_schema(),
    }


def _submit_plan_review_schema() -> dict:
    """Schema for submit_plan_review tool — for plan agent review."""
    return {
        "name": SUBMIT_PLAN_REVIEW,
        "description": (
            "Submit the final plan review verdict. Call this when you have finished "
            "verifying the implementation plan against the codebase."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdict", "summary", "findings"],
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES"],
                    "description": "Overall verdict on the plan.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-line summary of the review.",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["severity", "description"],
                        "properties": {
                            "severity": {"type": "string", "enum": ["P1", "P2"]},
                            "description": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def _build_submit_tools_openai(responses_api: bool = False) -> list[dict]:
    """Build submit tool schemas in OpenAI function format.

    When responses_api=True, uses the flat Responses API format (Codex models).
    Otherwise uses the nested Chat Completions format.
    """
    result = []
    for schema_fn in (_submit_review_schema, _submit_plan_review_schema):
        s = schema_fn()
        if responses_api:
            result.append(
                {
                    "type": "function",
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["parameters"],
                }
            )
        else:
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "description": s["description"],
                        "parameters": s["parameters"],
                    },
                }
            )
    return result


def _build_submit_tools_anthropic() -> list[dict]:
    """Build submit tool schemas in Anthropic tool format."""
    result = []
    for schema_fn in (_submit_review_schema, _submit_plan_review_schema):
        s = schema_fn()
        result.append(
            {
                "name": s["name"],
                "description": s["description"],
                "input_schema": s["parameters"],
            }
        )
    return result


def _build_submit_tools_google() -> list[dict]:
    """Build submit tool function declarations for Google.

    Sanitizes parameters to strip additionalProperties and other unsupported
    JSON Schema features.
    """
    result = []
    for schema_fn in (_submit_review_schema, _submit_plan_review_schema):
        s = schema_fn()
        result.append(
            {
                "name": s["name"],
                "description": s["description"],
                "parameters": _sanitize_schema_for_google(s["parameters"]),
            }
        )
    return result


# ── Agent loop manager ────────────────────────────────────────────────


@dataclass
class _UsageAccumulator:
    """Accumulate token usage across loop iterations."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, usage: ModelUsage | None) -> None:
        if usage is None:
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_creation_tokens += usage.cache_creation_tokens

    def to_model_usage(self, model: str, provider: str) -> ModelUsage:
        cost = _estimate_cost(provider, model, self.input_tokens, self.output_tokens)
        return ModelUsage(
            model=model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
            cost_usd=cost,
        )


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if *exc* is a provider 429 / quota-exhausted error."""
    type_name = type(exc).__name__
    if type_name in ("RateLimitError", "ResourceExhausted", "TooManyRequestsError"):
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "resource_exhausted" in msg or "quota" in msg


# Max 429 retries per loop turn; backoff: 30s, 60s, 120s, 240s …
_MAX_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_BACKOFF_BASE = 30  # seconds


class AgentLoopManager:
    """Drives the multi-turn tool-use loop for API-mode agents.

    The loop:
    1. Sends the message history + tool schemas to the provider
    2. If the model requests tool calls, executes them (in parallel) and loops
    3. If the model calls a submit tool, extracts structured data and returns
    4. If the model emits plain text, returns it as the final output
    5. Terminates on timeout, max iterations, or consecutive malformed calls
    """

    def __init__(
        self,
        *,
        profile: "ModelProfile",
        provider: str,
        working_dir: Path,
        tools: list[ToolDef],
        provider_adapter: ProviderAdapter,
        finalizer: Finalizer | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self._profile = profile
        self._provider = provider
        self._working_dir = working_dir
        self._tools = {t.name: t for t in tools}
        self._adapter = provider_adapter
        self._finalizer = finalizer
        # Per-profile max_iterations takes precedence over the constructor default
        self._max_iterations = profile.max_iterations or max_iterations
        self._usage = _UsageAccumulator()
        self._total_tool_calls = 0
        self._deadline = time.monotonic() + profile.timeout_seconds
        self._iter_nudge_sent = False
        self._time_nudge_sent = False

    def _timed_out(self) -> bool:
        return time.monotonic() > self._deadline

    def _call_with_retry(self, messages: list[dict], tool_schemas: list[dict]) -> LoopTurn:
        """Call the provider adapter, retrying on 429 rate-limit errors.

        Uses exponential backoff starting at _RATE_LIMIT_BACKOFF_BASE seconds.
        Raises the final exception if all retries are exhausted or the deadline
        would be exceeded before the next retry sleep completes.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return self._adapter(messages, tool_schemas)
            except Exception as exc:
                if not _is_rate_limit_error(exc):
                    raise  # non-429 errors propagate immediately
                last_exc = exc
                if attempt >= _MAX_RATE_LIMIT_RETRIES:
                    break
                wait = _RATE_LIMIT_BACKOFF_BASE * (2**attempt)
                label = self._profile.name or f"{self._provider}/{self._profile.model}"
                remaining = self._deadline - time.monotonic()
                if remaining <= wait:
                    _log(
                        f"  ⚠ {label} rate-limited; {remaining:.0f}s left < "
                        f"{wait}s backoff — giving up"
                    )
                    break
                _log(
                    f"  ⚠ {label} rate-limited (429); retrying in {wait}s "
                    f"(attempt {attempt + 1}/{_MAX_RATE_LIMIT_RETRIES})"
                )
                time.sleep(wait)
        raise last_exc  # type: ignore[misc]

    def _execute_tools(self, calls: list[ToolCallRequest]) -> list[dict]:
        """Execute tool calls in parallel; always return string results."""
        max_bytes = self._profile.max_tool_output_bytes

        def _run_one(call: ToolCallRequest) -> dict:
            t0 = time.monotonic()
            if self._timed_out():
                content = "Error: global timeout reached before tool could execute"
                duration_ms = 0
            elif call.name in _SUBMIT_TOOL_NAMES:
                # Submit tools are handled by the loop itself, not dispatched here
                content = ""
                duration_ms = 0
            elif call.name not in self._tools:
                available = ", ".join(sorted(self._tools.keys()))
                content = f"Error: unknown tool '{call.name}' — available tools: {available}"
                duration_ms = int((time.monotonic() - t0) * 1000)
            else:
                tool = self._tools[call.name]
                try:
                    # Validate required args from schema
                    required = tool.parameters.get("required", [])
                    missing = [r for r in required if r not in call.arguments]
                    if missing:
                        content = (
                            f"Error: {call.name} requires {missing!r} but got: {call.arguments}"
                        )
                    else:
                        content = tool.handler(
                            working_dir=self._working_dir,
                            max_bytes=max_bytes,
                            **call.arguments,
                        )
                except Exception as exc:
                    content = f"Error: {type(exc).__name__} — {exc}"
                duration_ms = int((time.monotonic() - t0) * 1000)

            # Brief summary for logging
            if call.name == "read_file":
                summary = call.arguments.get("path", "")
                sl = call.arguments.get("start_line")
                el = call.arguments.get("end_line")
                if sl or el:
                    summary = f"{summary}:{sl or ''}-{el or ''}"
            elif call.name == "bash":
                cmd = call.arguments.get("command", "")
                summary = cmd[:60] + ("..." if len(cmd) > 60 else "")
            elif call.name in ("grep", "glob"):
                summary = call.arguments.get("pattern", "")
            else:
                summary = str(call.arguments)[:60]

            _log_verbose(f"  ↳ {call.name}: {summary} ({duration_ms}ms)")
            return {"id": call.id, "name": call.name, "content": content}

        with ThreadPoolExecutor(max_workers=min(len(calls), 8)) as pool:
            futures = {pool.submit(_run_one, call): call for call in calls}
            results = []
            for future in as_completed(futures):
                results.append(future.result())
        # Preserve original call ordering
        order = {call.id: i for i, call in enumerate(calls)}
        results.sort(key=lambda r: order.get(r["id"], 0))
        return results

    def run(
        self,
        *,
        initial_messages: list[dict],
        tool_schemas: list[dict],
    ) -> AgentResult:
        """Run the agent loop. Returns AgentResult with accumulated cost."""
        messages = list(initial_messages)
        iterations = 0
        consecutive_malformed = 0

        while iterations < self._max_iterations:
            if self._timed_out():
                return self._finalize_or_timeout(messages, iterations, "wall-clock timeout")

            try:
                turn = self._call_with_retry(messages, tool_schemas)
            except Exception as exc:
                _log_verbose(traceback.format_exc())
                return self._failure_result(f"Provider API error: {exc}")

            self._usage.add(turn.usage)
            iterations += 1

            # Check for submit tool call
            for call in turn.tool_calls:
                if call.name in _SUBMIT_TOOL_NAMES:
                    self._total_tool_calls += len(turn.tool_calls)
                    label = self._profile.name or f"{self._provider}/{self._profile.model}"
                    _log(
                        f"  ... {label} done "
                        f"({iterations} iter, {self._total_tool_calls} tool calls)"
                    )
                    return self._success_result(
                        output=json.dumps(call.arguments, indent=2),
                        structured_data=call.arguments,
                    )

            # No tool calls → model is done, return text output
            if not turn.tool_calls:
                label = self._profile.name or f"{self._provider}/{self._profile.model}"
                output = turn.text_output or ""
                # If the model stopped without calling a submit tool and produced
                # no output, treat it as a failure — the review was not delivered.
                if not output.strip() and turn.structured_data is None:
                    _log(
                        f"  ... {label} done ({iterations} iter, "
                        f"{self._total_tool_calls} tool calls) — empty output"
                    )
                    return self._failure_result(
                        "Agent finished without calling submit tool and produced no output"
                    )
                _log(
                    f"  ... {label} done ({iterations} iter, {self._total_tool_calls} tool calls)"
                )
                return self._success_result(
                    output=output,
                    structured_data=turn.structured_data,
                )

            # Validate all calls in this turn and collect results for the full set.
            # We must call _append_tool_results ONCE with all of turn.tool_calls so
            # that every tool_call/tool_use in the assistant message has a matching
            # result — all three providers require this.
            turn_results: list[dict] = []
            has_malformed = False
            valid_calls: list[ToolCallRequest] = []

            for call in turn.tool_calls:
                if not call.name or not isinstance(call.arguments, dict):
                    has_malformed = True
                    consecutive_malformed += 1
                    turn_results.append(
                        {
                            "id": call.id,
                            "name": call.name or "unknown",
                            "content": (
                                f"Error: malformed tool call — "
                                f"name={call.name!r}, arguments={call.arguments!r}"
                            ),
                        }
                    )
                else:
                    valid_calls.append(call)

            if has_malformed and consecutive_malformed >= _MAX_MALFORMED:
                return self._failure_result(
                    f"Aborted after {_MAX_MALFORMED} consecutive malformed tool calls"
                )

            # Execute valid calls in parallel and merge results in original order
            if valid_calls:
                consecutive_malformed = 0
                self._total_tool_calls += len(valid_calls)
                executed = self._execute_tools(valid_calls)
                # executed is ordered by valid_calls; turn_results holds errors so far;
                # rebuild in original turn order
                executed_by_id = {r["id"]: r for r in executed}
                turn_results = [
                    executed_by_id[call.id]
                    if call.id in executed_by_id
                    else next(r for r in turn_results if r["id"] == call.id)
                    for call in turn.tool_calls
                ]
            elif not has_malformed:
                # All calls were skipped (shouldn't happen), reset counter
                consecutive_malformed = 0

            # Append assistant turn + all tool results in a single history entry
            messages = self._append_tool_results(messages, turn.tool_calls, turn_results)

            # Nudge: when approaching the iteration limit, tell the model to wrap up
            if not self._iter_nudge_sent:
                nudge_threshold = int(self._max_iterations * 0.8)
                remaining_iter = self._max_iterations - iterations
                if iterations >= nudge_threshold and remaining_iter > 0:
                    self._iter_nudge_sent = True
                    nudge_msg = (
                        f"[SYSTEM] You have {remaining_iter} iterations remaining before "
                        f"this session terminates. Finish your analysis and submit "
                        f"your response now using the submit tool."
                    )
                    messages = list(messages)
                    messages.append({"role": "user", "content": nudge_msg})
                    label = self._profile.name or f"{self._provider}/{self._profile.model}"
                    _log_verbose(f"  ⚠ {label} nudge sent ({remaining_iter} iterations remaining)")

            # Time-based nudge: when approaching wall-clock deadline
            if not self._time_nudge_sent:
                elapsed = time.monotonic() - (self._deadline - self._profile.timeout_seconds)
                time_fraction = elapsed / self._profile.timeout_seconds
                if time_fraction >= 0.8:
                    self._time_nudge_sent = True
                    remaining_secs = int(self._deadline - time.monotonic())
                    if remaining_secs > 0:
                        nudge_msg = (
                            f"[SYSTEM] You have approximately {remaining_secs} seconds "
                            f"remaining before timeout. Finish your analysis and submit "
                            f"your response now using the submit tool."
                        )
                        messages = list(messages)
                        messages.append({"role": "user", "content": nudge_msg})
                        label = self._profile.name or f"{self._provider}/{self._profile.model}"
                        _log_verbose(f"  ⚠ {label} time nudge sent ({remaining_secs}s remaining)")

        return self._finalize_or_timeout(messages, iterations, "max iterations reached")

    def _finalize_or_timeout(
        self, messages: list[dict], iterations: int, reason: str
    ) -> AgentResult:
        """Attempt finalization via constrained output; fall back to timeout failure."""
        if self._finalizer is None:
            return self._timeout_result(iterations, reason=reason)

        label = self._profile.name or f"{self._provider}/{self._profile.model}"
        _log(f"  ⚠ {label} {reason} after {iterations} iterations — attempting finalization")

        try:
            turn = self._finalizer(messages)
            self._usage.add(turn.usage)

            if turn.structured_data:
                _log(
                    f"  ... {label} finalized "
                    f"({iterations} iter, {self._total_tool_calls} tool calls)"
                )
                return self._success_result(
                    output=json.dumps(turn.structured_data, indent=2),
                    structured_data=turn.structured_data,
                )

            # Finalizer returned but no structured data — try parsing text
            if turn.text_output and turn.text_output.strip():
                try:
                    data = json.loads(turn.text_output)
                    _log(
                        f"  ... {label} finalized from text "
                        f"({iterations} iter, {self._total_tool_calls} tool calls)"
                    )
                    return self._success_result(
                        output=turn.text_output,
                        structured_data=data,
                    )
                except json.JSONDecodeError:
                    pass

            _log(f"  ... {label} finalization produced no structured output")
        except Exception as exc:
            _log_verbose(f"  ... {label} finalization failed: {exc}")
            _log_verbose(traceback.format_exc())

        return self._timeout_result(iterations, reason=reason)

    def _append_tool_results(
        self,
        messages: list[dict],
        calls: list[ToolCallRequest],
        results: list[dict],
    ) -> list[dict]:
        """Add assistant tool-call turn + tool result turn to messages."""
        new_messages = list(messages)
        new_messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": calls,
            }
        )
        new_messages.append(
            {
                "role": "tool_results",
                "results": results,
            }
        )
        return new_messages

    def _success_result(self, *, output: str, structured_data: dict | None) -> AgentResult:
        usage = self._usage.to_model_usage(self._profile.model, self._provider)
        return AgentResult(
            success=True,
            output=output,
            session_id=None,
            cost_usd=usage.cost_usd,
            exit_code=0,
            raw={},
            profile_name=self._profile.name,
            model_usage=(usage,),
            structured_data=structured_data,
        )

    def _failure_result(self, reason: str) -> AgentResult:
        usage = self._usage.to_model_usage(self._profile.model, self._provider)
        return AgentResult(
            success=False,
            output=reason,
            session_id=None,
            cost_usd=usage.cost_usd,
            exit_code=1,
            raw={},
            profile_name=self._profile.name,
            model_usage=(usage,),
        )

    def _timeout_result(self, iterations: int, reason: str = "wall-clock timeout") -> AgentResult:
        label = self._profile.name or f"{self._provider}/{self._profile.model}"
        _log(f"  ... {label} FAILED — {reason} after {iterations} iterations")
        return self._failure_result(
            f"Agent loop terminated: {reason} after {iterations} iterations. "
            f"Accumulated cost reported."
        )


# ── OpenAI client helpers ─────────────────────────────────────────────


def _openai_client(profile: "ModelProfile", secrets: dict[str, str] | None = None):  # type: ignore[return]
    """Build an OpenAI client from profile + secrets."""
    import openai

    merged = {**os.environ, **(secrets or {})}
    kwargs: dict[str, Any] = {"api_key": merged.get("OPENAI_API_KEY") or "local"}
    if profile.base_url:
        kwargs["base_url"] = profile.base_url
    return openai.OpenAI(**kwargs)


def _openai_result(
    profile: "ModelProfile",
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    raw: dict,
) -> AgentResult:
    """Build AgentResult from parsed OpenAI response fields."""
    cost = _estimate_cost("openai", profile.model, input_tokens, output_tokens)
    if profile.base_url:
        cost = None
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
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Run via OpenAI Chat Completions (/v1/chat/completions)."""
    client = _openai_client(profile, secrets)
    schema = review_json_schema()
    try:
        create_kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "review_output", "schema": schema, "strict": True},
            },
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


def _run_anthropic(
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Run agent via Anthropic API."""
    import anthropic

    merged = {**os.environ, **(secrets or {})}
    client = anthropic.Anthropic(api_key=merged.get("ANTHROPIC_API_KEY"))
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
    prompt: str, profile: "ModelProfile", secrets: dict[str, str] | None = None
) -> AgentResult:
    """Run agent via Google Gemini API."""
    import google.genai as genai
    import google.genai.types as genai_types

    merged = {**os.environ, **(secrets or {})}
    client = genai.Client(api_key=merged.get("GOOGLE_API_KEY"))
    schema = _sanitize_schema_for_google(review_json_schema())

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


PROVIDER_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_openai,
    "anthropic": _run_anthropic,
    "google": _run_google,
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
) -> ProviderAdapter:
    """Build OpenAI Chat Completions adapter for AgentLoopManager."""
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
    client = anthropic.Anthropic(api_key=merged.get("ANTHROPIC_API_KEY"))

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
                            "Include verdict, summary, findings, spec_compliance, "
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
) -> Finalizer:
    """Build a Chat Completions finalizer using response_format: json_schema."""
    client = _openai_client(profile, secrets)
    schema = review_json_schema()

    def finalizer(messages: list[dict]) -> LoopTurn:
        oai_messages = _translate_messages_openai_chat(messages)
        oai_messages.append(
            {
                "role": "user",
                "content": (
                    "Time is up. Deliver your code review verdict now as structured JSON. "
                    "Include verdict, summary, findings, spec_compliance, and test_coverage."
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
                    "Include verdict, summary, findings, spec_compliance, and test_coverage."
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

    merged = {**os.environ, **(secrets or {})}
    client = anthropic.Anthropic(api_key=merged.get("ANTHROPIC_API_KEY"))

    def finalizer(messages: list[dict]) -> LoopTurn:
        anth_messages = _translate_messages_anthropic(messages)
        anth_messages.append(
            {
                "role": "user",
                "content": (
                    "Time is up. Deliver your code review verdict now. "
                    "Include verdict, summary, findings, spec_compliance, and test_coverage."
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
                            "findings, spec_compliance, and test_coverage."
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


def _build_registry_tools(profile: "ModelProfile") -> list[ToolDef]:
    """Build the list of ToolDef objects from profile.allowed_tools."""
    return [TOOL_REGISTRY[name] for name in profile.allowed_tools if name in TOOL_REGISTRY]


def _run_loop_openai(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run OpenAI provider in agent loop mode."""
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
    return manager.run(
        initial_messages=[{"role": "user", "content": prompt}],
        tool_schemas=tool_schemas,
    )


def _run_loop_anthropic(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run Anthropic provider in agent loop mode."""
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


_LOOP_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_loop_openai,
    "anthropic": _run_loop_anthropic,
    "google": _run_loop_google,
}


# ── Public entry point ────────────────────────────────────────────────


def run_api_agent(
    *,
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    quiet: bool = False,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run a text-judgment agent via API.

    When profile.allowed_tools is non-empty, drives an agent loop where the model
    can call tools. When empty, falls back to a single-shot stateless call.
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

    label = profile.name or f"{profile.provider}/{profile.model}"
    if not quiet:
        _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    if profile.allowed_tools:
        # Loop mode — tools are available
        loop_runner = _LOOP_RUNNERS.get(profile.provider)
        if not loop_runner:
            return AgentResult(
                success=False,
                output=f"Unknown API provider: {profile.provider}",
                session_id=None,
                cost_usd=None,
                exit_code=1,
                raw={},
                profile_name=profile.name,
            )
        result = loop_runner(prompt, profile, working_dir, secrets)
    else:
        # Single-shot stateless mode — existing behavior
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
        result = runner_fn(prompt, profile, secrets)

    if not quiet:
        status = "OK" if result.success else "FAIL"
        cost_str = f"${result.cost_usd:.3f}" if result.cost_usd is not None else "unknown"
        _log_verbose(f"  ... {label} done | {status} | cost={cost_str}")

    return result
