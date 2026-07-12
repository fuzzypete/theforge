"""API-based agent runners for text-judgment agents."""

from __future__ import annotations

import dataclasses
import json
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from theforge.agent_types import AgentResult, ModelUsage
from theforge.runners.adapters.anthropic import (
    _make_anthropic_adapter,
    _run_anthropic,
)
from theforge.runners.adapters.deepseek import (
    _deepseek_client,
    _run_deepseek,
)
from theforge.runners.adapters.google import (
    _make_google_adapter,
    _run_google,
)
from theforge.runners.adapters.openai import (
    _is_local_endpoint,
    _make_openai_chat_adapter,
    _make_openai_responses_adapter,
    _run_openai,
)
from theforge.runners.cli import _log, _log_verbose
from theforge.runners.finalizers import (
    _make_anthropic_finalizer,
    _make_deepseek_finalizer,
    _make_google_finalizer,
    _make_openai_chat_finalizer,
    _make_openai_responses_finalizer,
)
from theforge.runners.schema_utils import (
    _DEFAULT_MAX_ITERATIONS,
    _MAX_MALFORMED,
    _NO_SUBMIT_PHASES,
    _SUBMIT_TOOL_NAMES,
    Finalizer,
    LoopTurn,
    ProviderAdapter,
    ToolCallRequest,
    _build_submit_tools_anthropic,
    _build_submit_tools_google,
    _build_submit_tools_openai,
    _estimate_cost,
    noop_finalizer,
    uses_openai_responses_api,
)
from theforge.runners.stuck_detection import StuckTracker, build_observation
from theforge.runners.tool_runtime import TOOL_REGISTRY, ToolDef

if TYPE_CHECKING:
    from theforge.config import ModelProfile


# Max 429 retries per loop turn; backoff: 30s, 60s, 120s, 240s …
_MAX_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_BACKOFF_BASE = 30  # seconds

# Argument names that may carry large or sensitive content (file bodies, edit
# payloads, stdin data).  These are redacted in any diagnostic log or trace
# artifact that serialises ToolCallRequest objects — they are NOT removed from
# the live conversation history sent to the provider, which needs them for
# context.
_SENSITIVE_ARG_NAMES: frozenset[str] = frozenset(
    {"content", "new_content", "old_string", "new_string", "input"}
)


def _redact_tool_call_arguments(arguments: dict) -> dict:
    """Return a copy of *arguments* with sensitive fields replaced by a placeholder.

    Only keys in _SENSITIVE_ARG_NAMES are redacted.  All other arguments (paths,
    patterns, commands) are preserved so the diagnostic output is still useful.
    """
    return {k: "<redacted>" if k in _SENSITIVE_ARG_NAMES else v for k, v in arguments.items()}


# Patterns in result.output that indicate a model-preference fallback should fire.
# Only usage-exhaustion and model-not-found errors trigger fallback; runtime errors
# (bad code, schema violations) propagate immediately.
_MODEL_FALLBACK_PATTERNS = (
    "429",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "resource exhausted",
    "quota exceeded",
    "quota_exceeded",
    "overloaded",
    "try again later",
    "model not found",
    "model_not_found",
    "no such model",
    "invalid model",
    "model does not exist",
    "model is deprecated",
    "model has been deprecated",
)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if *exc* is a provider 429 / quota-exhausted error."""
    type_name = type(exc).__name__
    if type_name in ("RateLimitError", "ResourceExhausted", "TooManyRequestsError"):
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "resource_exhausted" in msg or "quota" in msg


def _classify_api_model_fallback(result: AgentResult) -> str | None:
    """Return a reason string if *result* should trigger model-preference fallback.

    Only quota-exhaustion and model-not-found errors trigger fallback.
    Runtime errors (bad code, schema violations, timeouts) return None.
    """
    if result.success:
        return None
    output_lower = result.output.lower()
    for pattern in _MODEL_FALLBACK_PATTERNS:
        if pattern in output_lower:
            return f"matched {pattern!r}"
    return None


@dataclass
class _UsageAccumulator:
    """Accumulate token usage across loop iterations."""

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, usage: ModelUsage | None) -> None:
        if usage is None:
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.thinking_tokens += usage.thinking_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_creation_tokens += usage.cache_creation_tokens

    def to_model_usage(self, model: str, provider: str) -> ModelUsage:
        cost = _estimate_cost(
            provider,
            model,
            self.input_tokens,
            self.output_tokens,
            thinking_tokens=self.thinking_tokens,
        )
        return ModelUsage(
            model=model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
            cost_usd=cost,
            thinking_tokens=self.thinking_tokens,
        )


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
        self._stuck = StuckTracker(profile)

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

            # Brief summary for logging — never include sensitive argument values.
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
            elif call.name in ("write_file", "edit_file"):
                # content/old_string/new_string can be large — log only the path.
                summary = call.arguments.get("path", "")
            else:
                summary = str(_redact_tool_call_arguments(call.arguments))[:60]

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
                executed_by_id = {r["id"]: r for r in executed}
                turn_results = [
                    executed_by_id[call.id]
                    if call.id in executed_by_id
                    else next(r for r in turn_results if r["id"] == call.id)
                    for call in turn.tool_calls
                ]
            elif not has_malformed:
                consecutive_malformed = 0

            # Append assistant turn + all tool results in a single history entry
            messages = self._append_tool_results(messages, turn.tool_calls, turn_results)

            # Progress-aware stuck detection: observe this iteration's calls/results.
            # On detection, inject a nudge; if the pattern persists after the nudge,
            # terminate with a structured failure log.
            stuck_nudge, stuck_terminate, stuck_pattern = self._stuck.observe(
                build_observation(turn.tool_calls, turn_results)
            )
            if stuck_terminate is not None:
                _log(
                    f"  ⚠ {label} stuck-detection terminate after {iterations} iterations "
                    f"({self._total_tool_calls} tool calls): {stuck_pattern}"
                )
                return self._failure_result(
                    f"Agent loop terminated: {stuck_terminate} "
                    f"(at iteration {iterations}, {self._total_tool_calls} tool calls)"
                )
            if stuck_nudge is not None:
                messages = list(messages)
                messages.append({"role": "user", "content": stuck_nudge})
                _log(
                    f"  ⚠ {label} stuck-detection nudge at iteration {iterations}: {stuck_pattern}"
                )

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

        # Log iteration summary
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
        """Attempt finalization via constrained output; fall back to timeout failure.

        *messages* contains the full conversation history, including assistant turns
        with ToolCallRequest objects whose arguments may hold sensitive data (file
        contents, edit payloads, etc.).  The history is passed to the provider as-is
        so the model has full context for producing its final output.

        If this method ever needs to write *messages* to a diagnostic artifact (log
        file, trace, etc.), use _redact_tool_call_arguments() on each call's
        arguments before serialising to avoid leaking sensitive content.
        """
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
        failure_code = None
        if reason.startswith("Agent loop terminated: max iterations reached"):
            failure_code = "max_iterations_reached"
        elif reason.startswith("Agent finished without calling submit tool"):
            # The agent completed its turn cleanly but delivered no verdict —
            # a non-verdict/behavioral completion, distinct from a transport
            # crash. Tagged so the review pool can recognise and recover from
            # it (degrade to surviving verdicts) instead of failing the story.
            failure_code = "no_submit_completion"
        return AgentResult(
            success=False,
            output=reason,
            session_id=None,
            cost_usd=cost,
            exit_code=1,
            raw={},
            profile_name=self._profile.name,
            model_usage=(usage,),
            failure_code=failure_code,
        )

    def _timeout_result(self, iterations: int, reason: str = "wall-clock timeout") -> AgentResult:
        label = self._profile.name or f"{self._provider}/{self._profile.model}"
        _log(f"  ... {label} FAILED — {reason} after {iterations} iterations")
        return self._failure_result(
            f"Agent loop terminated: {reason} after {iterations} iterations. "
            f"Accumulated cost reported."
        )


# ── Provider runners (single-shot) ────────────────────────────────────

PROVIDER_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_openai,
    "anthropic": _run_anthropic,
    "google": _run_google,
    "deepseek": _run_deepseek,
}


# ── Tool schema builder ───────────────────────────────────────────────

_CLI_TO_REGISTRY: dict[str, str] = {
    # Claude CLI tool names → API registry keys
    "Read": "read_file",
    "Edit": "edit_file",
    "Write": "write_file",
    "Bash": "bash",
    "Glob": "glob",
    "Grep": "grep",
}


def _build_registry_tools(profile: "ModelProfile") -> list[ToolDef]:
    """Build the list of ToolDef objects from profile.allowed_tools."""
    result = []
    for name in profile.allowed_tools:
        key = _CLI_TO_REGISTRY.get(name, name)
        if key in TOOL_REGISTRY:
            result.append(TOOL_REGISTRY[key])
    return result


# ── Loop-mode entry points ────────────────────────────────────────────


def _run_loop_openai(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run OpenAI provider in agent loop mode."""
    import openai

    tools = _build_registry_tools(profile)
    is_responses = uses_openai_responses_api(profile.model)
    is_review = profile.phase not in _NO_SUBMIT_PHASES

    if is_responses:
        tool_schemas = [t.to_openai_responses_function() for t in tools] + (
            _build_submit_tools_openai(responses_api=True) if is_review else []
        )
        adapter = _make_openai_responses_adapter(profile, secrets)
        finalizer = (
            _make_openai_responses_finalizer(profile, secrets) if is_review else noop_finalizer
        )
    else:
        tool_schemas = [t.to_openai_function() for t in tools] + (
            _build_submit_tools_openai(responses_api=False) if is_review else []
        )
        adapter = _make_openai_chat_adapter(profile, secrets)
        finalizer = _make_openai_chat_finalizer(profile, secrets) if is_review else noop_finalizer

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
            _log(
                f"  ⚠ {profile.name or profile.model} tool-call 400 — "
                "falling back to single-shot text mode"
            )
            fallback_prompt = (
                prompt
                + "\n\n[SYSTEM] Respond with a JSON object matching the review output schema. "
                "Do not use tool calls."
            )
            return PROVIDER_RUNNERS["openai"](fallback_prompt, profile, secrets)
        raise

    if _is_local_endpoint(profile.base_url):
        zeroed_usage = tuple(
            dataclasses.replace(u, cost_usd=0.0) for u in (result.model_usage or ())
        )
        result = dataclasses.replace(result, cost_usd=0.0, model_usage=zeroed_usage)

    return result


def _run_loop_anthropic(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run Anthropic provider in agent loop mode."""
    tools = _build_registry_tools(profile)
    is_review = profile.phase not in _NO_SUBMIT_PHASES
    tool_schemas = [t.to_anthropic_tool() for t in tools] + (
        _build_submit_tools_anthropic() if is_review else []
    )
    adapter = _make_anthropic_adapter(profile, secrets)
    finalizer = _make_anthropic_finalizer(profile, secrets) if is_review else noop_finalizer

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
    is_review = profile.phase not in _NO_SUBMIT_PHASES
    tool_schemas = [t.to_google_declaration() for t in tools] + (
        _build_submit_tools_google() if is_review else []
    )
    adapter = _make_google_adapter(profile, secrets)
    finalizer = _make_google_finalizer(profile, secrets) if is_review else noop_finalizer

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


def _run_loop_deepseek(
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Run DeepSeek provider in agent loop mode (OpenAI Chat Completions)."""
    client = _deepseek_client(profile, secrets)
    tools = _build_registry_tools(profile)
    is_review = profile.phase not in _NO_SUBMIT_PHASES
    tool_schemas = [t.to_openai_function() for t in tools] + (
        _build_submit_tools_openai(responses_api=False) if is_review else []
    )
    adapter = _make_openai_chat_adapter(profile, secrets, client=client)
    finalizer = (
        _make_deepseek_finalizer(profile, secrets, client=client) if is_review else noop_finalizer
    )

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


_LOOP_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_loop_openai,
    "anthropic": _run_loop_anthropic,
    "google": _run_loop_google,
    "deepseek": _run_loop_deepseek,
}


# ── Public entry point ────────────────────────────────────────────────


def _run_single_api_model(
    *,
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    secrets: dict[str, str] | None,
    plain_text: bool,
) -> AgentResult:
    """Invoke one model (profile.model) via the provider's runner.

    Does not handle model-preference iteration — that lives in run_api_agent.
    """
    if profile.provider == "google" and plain_text:
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
        return runner_fn(prompt, profile, secrets, plain_text=True)
    elif profile.allowed_tools:
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
        return loop_runner(prompt, profile, working_dir, secrets)
    else:
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
        return runner_fn(prompt, profile, secrets)


def run_api_agent(
    *,
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    quiet: bool = False,
    secrets: dict[str, str] | None = None,
    plain_text: bool = False,
) -> AgentResult:
    """Run a text-judgment agent via API, trying models in preference-list order.

    When profile.allowed_tools is non-empty, drives an agent loop where the model
    can call tools. When empty, falls back to a single-shot stateless call.

    Google Gemini plain-text calls intentionally bypass the tool loop even when
    the profile allows tools. Ideation expects raw markdown/text output, and the
    Google loop's review finalization path can otherwise force response_schema
    JSON output that does not match the ideation prompt.

    If profile.fallback_models is non-empty, forge tries each model in
    (profile.model, *profile.fallback_models) order. Only quota-exhaustion and
    model-not-found errors trigger fallback; runtime errors propagate immediately.
    The model actually used is recorded in AgentResult.model_usage[*].model and,
    when a fallback fired, in AgentResult.model_config (the full preference list).
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

    models = profile.models  # preference list: (primary, *fallbacks)
    label = profile.name or f"{profile.provider}/{profile.model}"
    if not quiet:
        _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    # model_config is set in the result only when the preference list has >1 entry,
    # so auditors can detect when a fallback was configured (whether or not it fired).
    model_config: tuple[str, ...] = models if len(models) > 1 else ()

    last_result: AgentResult | None = None
    for idx, model in enumerate(models):
        is_last = idx == len(models) - 1
        # Build a single-model profile for this attempt
        current_profile = dataclasses.replace(profile, model=model, fallback_models=())

        result = _run_single_api_model(
            prompt=prompt,
            profile=current_profile,
            working_dir=working_dir,
            secrets=secrets,
            plain_text=plain_text,
        )

        if result.success:
            if idx > 0:
                _log(
                    f"  ✓ {label} fallback to {model!r} succeeded (skipped: {list(models[:idx])})"
                )
            if not quiet:
                status = "OK"
                cost_str = f"${result.cost_usd:.3f}" if result.cost_usd is not None else "unknown"
                _log_verbose(f"  ... {label} done | {status} | cost={cost_str}")
            # Annotate with model_config and model_used only when a preference list is
            # configured. Skip replace() for single-model profiles to avoid breaking
            # callers that return non-dataclass objects (e.g., mocks in tests).
            if model_config:
                return dataclasses.replace(result, model_config=model_config, model_used=model)
            return result

        last_result = result

        if not is_last:
            fallback_reason = _classify_api_model_fallback(result)
            if fallback_reason:
                next_model = models[idx + 1]
                _log(
                    f"  ⚠ {label} model {model!r} failed ({fallback_reason}); "
                    f"trying {next_model!r}..."
                )
                continue

        # Not a fallback-eligible error, or we are at the last model — stop here.
        break

    assert last_result is not None
    if not quiet:
        status = "FAIL"
        cost_str = (
            f"${last_result.cost_usd:.3f}" if last_result.cost_usd is not None else "unknown"
        )
        _log_verbose(f"  ... {label} done | {status} | cost={cost_str}")
    if model_config:
        return dataclasses.replace(last_result, model_config=model_config, model_used=model)
    return last_result
