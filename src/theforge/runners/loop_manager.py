"""Agent loop manager: multi-turn tool-use loop for API-mode agents.

Owns the AgentLoopManager class, provider-agnostic intermediate types
(LoopTurn, ToolCallRequest), and the loop execution infrastructure
including rate-limit retry, tool dispatch, and iteration nudges.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from theforge.agent_types import AgentResult, ModelUsage
from theforge.runners.cli import _log, _log_verbose
from theforge.runners.tool_runtime import ToolDef

if TYPE_CHECKING:
    from theforge.config import ModelProfile


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
    ("deepseek", "deepseek-chat"): (0.27, 1.10),  # V3 alias
    ("deepseek", "deepseek-r1"): (0.55, 2.19),
    ("deepseek", "deepseek-v3"): (0.27, 1.10),
    ("deepseek", "deepseek-reasoner"): (0.55, 2.19),  # R1 alias
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
    """Return True for reasoning models that do not support temperature=0."""
    return bool(_REASONING_MODEL_RE.match(model)) or model.startswith("deepseek-r1")


# Submit tool names — loop-internal, not in TOOL_REGISTRY
SUBMIT_REVIEW = "submit_review"
SUBMIT_PLAN_REVIEW = "submit_plan_review"
_SUBMIT_TOOL_NAMES = {SUBMIT_REVIEW, SUBMIT_PLAN_REVIEW}

# Max consecutive malformed tool calls before aborting
_MAX_MALFORMED = 3

# Default max loop iterations
_DEFAULT_MAX_ITERATIONS = 50


def _estimate_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """Estimate cost from pricing table; returns None if model unknown."""
    price = PRICING_TABLE.get((provider, model))
    if price is None:
        return None
    return ((input_tokens / 1_000_000) * price[0]) + ((output_tokens / 1_000_000) * price[1])


def _is_local_endpoint(base_url: str | None) -> bool:
    """Return True if *base_url* points to a local machine (ollama/vllm etc.).

    Uses urlparse to inspect only the hostname, avoiding false positives from
    'localhost' appearing in URL paths or query parameters.
    """
    if not base_url:
        return False
    from urllib.parse import urlparse

    try:
        hostname = urlparse(base_url).hostname or ""
    except Exception:
        return False
    return hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


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


# ── Usage accumulator ────────────────────────────────────────────────


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
        _tool_call_counts: dict[str, int] = {}

        label = self._profile.name or f"{self._provider}/{self._profile.model}"
        tool_names = [s.get("function", {}).get("name") or s.get("name") for s in tool_schemas]
        tool_names = [n for n in tool_names if n]
        _log_verbose(f"  [{label}] loop start: {len(tool_names)} tools: {tool_names}")

        while iterations < self._max_iterations:
            if self._timed_out():
                return self._finalize_or_timeout(messages, iterations, "wall-clock timeout")

            try:
                turn = self._call_with_retry(messages, tool_schemas)
            except Exception as exc:
                # Re-raise HTTP 400 errors so callers (e.g. _run_loop_openai) can
                # implement provider-specific fallbacks (e.g. tool-not-supported).
                if getattr(exc, "status_code", None) == 400:
                    raise
                _log_verbose(traceback.format_exc())
                return self._failure_result(f"Provider API error: {exc}")

            self._usage.add(turn.usage)
            iterations += 1

            # Log per-turn tool calls at verbose level
            if turn.tool_calls:
                turn_call_names = [c.name for c in turn.tool_calls]
                _log_verbose(
                    f"  [{label}] iter {iterations}: "
                    f"{len(turn_call_names)} call(s): {turn_call_names}"
                )
                for name in turn_call_names:
                    _tool_call_counts[name] = _tool_call_counts.get(name, 0) + 1

            # Log text reasoning snippet at verbose level
            if turn.text_output and turn.text_output.strip():
                snippet = turn.text_output.strip()[:200]
                _log_verbose(f'  [{label}] iter {iterations} reasoning: "{snippet}"')

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
                    _log(
                        f"  ⚠ {label} approaching iteration limit "
                        f"({iterations}/{self._max_iterations}) — nudge sent"
                    )

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
                        _log_verbose(f"  ⚠ {label} time nudge sent ({remaining_secs}s remaining)")

        # Log iteration summary: tool call counts and whether submit was ever attempted
        submit_names = _SUBMIT_TOOL_NAMES
        submit_called = any(n in submit_names for n in _tool_call_counts)
        submit_status = "submit never called" if not submit_called else "submit called"
        counts_detail = ", ".join(f"{n}:{c}" for n, c in sorted(_tool_call_counts.items()))
        total_calls = sum(_tool_call_counts.values())
        _log(
            f"  ⚠ {label} max iterations ({iterations}): "
            f"{total_calls} tool calls [{counts_detail}], {submit_status}"
        )

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
            _log(f"  ... {label} finalization failed: {exc}")
            _log_verbose(f"  [finalization traceback]\n{traceback.format_exc()}")

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

    def _zero_cost_if_local(self, usage: ModelUsage) -> tuple[ModelUsage, float | None]:
        """Return (usage, cost) with costs zeroed for local endpoints."""
        if _is_local_endpoint(self._profile.base_url):
            return dataclasses.replace(usage, cost_usd=0.0), 0.0
        return usage, usage.cost_usd

    def _success_result(self, *, output: str, structured_data: dict | None) -> AgentResult:
        usage = self._usage.to_model_usage(self._profile.model, self._provider)
        usage, cost = self._zero_cost_if_local(usage)
        return AgentResult(
            success=True,
            output=output,
            session_id=None,
            cost_usd=cost,
            exit_code=0,
            raw={},
            profile_name=self._profile.name,
            model_usage=(usage,),
            structured_data=structured_data,
        )

    def _failure_result(self, reason: str) -> AgentResult:
        usage = self._usage.to_model_usage(self._profile.model, self._provider)
        usage, cost = self._zero_cost_if_local(usage)
        return AgentResult(
            success=False,
            output=reason,
            session_id=None,
            cost_usd=cost,
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
