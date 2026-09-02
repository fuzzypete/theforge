"""Claude Code CLI runner.

Invokes `claude -p --output-format stream-json --verbose` as a subprocess,
streams JSONL events, and returns an AgentResult.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from theforge import process_group, process_tree
from theforge.agent_types import (
    COST_ESTIMATED,
    COST_PROVIDER_REPORTED,
    COST_UNKNOWN,
    FAILURE_ENDED_WITHOUT_RESULT,
    FAILURE_KILLED_BEFORE_OUTPUT,
    KILLED_BEFORE_OUTPUT_MARKER,
    AgentResult,
    ModelUsage,
    killed_before_output,
)
from theforge.log_util import _log_line
from theforge.runners.stuck_detection import StuckTracker, build_observation
from theforge.task.handoff_parser import ParseError, extract_dev_handoff
from theforge.workspace_env import build_workspace_env

from ..config import ModelProfile
from .sandbox import SandboxCapabilityError, workspace_effect_sandbox_command

# ── Logging helpers ───────────────────────────────────────────────────


def _log(msg: str) -> None:
    _log_line("[forge]", msg)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL, LogLevel  # noqa: PLC0415

    if _LOG_LEVEL >= LogLevel.VERBOSE:
        _log_line("[forge]", msg)


# ── Argv builder ──────────────────────────────────────────────────────


def _claude_state_write_roots() -> tuple[Path, ...]:
    """Home paths the Claude CLI must write to under sandbox containment.

    Claude persists session transcripts, todos, telemetry, and config under
    ``~/.claude`` (and legacy ``~/.claude.json``). Granting write access to
    just these keeps session persistence working while leaving the worktree the
    only project-side writable root — the containment boundary is not widened to
    the project root or sibling worktrees.
    """
    home = Path.home()
    return (home / ".claude", home / ".claude.json")


def build_argv(
    *,
    profile: ModelProfile,
    session_id: str | None = None,
) -> list[str]:
    """Construct argv for `claude` invocation (initial run or resume).

    .. warning::
       An empty ``profile.allowed_tools`` omits ``--allowedTools`` entirely,
       which grants the CLI's **unrestricted** default tool set — the opposite
       of what an empty allowlist reads like. Every caller treats ``()`` as
       "nothing was requested, apply a default", so a role that reaches dispatch
       with an empty tuple fails *open*.

       Only preflight is defended against this today, by resolving its surface
       to a guaranteed non-empty set before dispatch (see
       ``config.resolve_preflight_tools``). Generalizing that to every role — or
       rejecting an empty ``allowed_tools`` at config load — is the real fix and
       is tracked separately; until then, do not read an empty allowlist here as
       a narrow one.
    """
    cmd: list[str] = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--input-format",
        "stream-json",
        "--verbose",
        "--model",
        profile.model,
    ]
    if profile.allowed_tools:
        cmd.extend(["--allowedTools", " ".join(profile.allowed_tools)])
    if session_id:
        cmd.extend(["--resume", session_id])
    if profile.sandbox_mode != "none":
        cmd.extend(["--permission-mode", "default"])
    return cmd


# ── Claude-specific helpers ───────────────────────────────────────────


def _parse_model_usage(result_json: dict[str, Any]) -> tuple[ModelUsage, ...]:
    """Extract per-model usage breakdown from Claude CLI JSON output."""
    raw_usage = result_json.get("modelUsage", {})
    if not isinstance(raw_usage, dict):
        return ()
    usages = []
    for model_name, data in raw_usage.items():
        if not isinstance(data, dict):
            continue
        usages.append(
            ModelUsage(
                model=model_name,
                input_tokens=int(data.get("inputTokens", 0)),
                output_tokens=int(data.get("outputTokens", 0)),
                cache_read_tokens=int(data.get("cacheReadInputTokens", 0)),
                cache_creation_tokens=int(data.get("cacheCreationInputTokens", 0)),
                cost_usd=float(data.get("costUSD", 0.0)),
                # The CLI's own ``modelUsage`` block — billed, not derived.
                cost_provenance=COST_PROVIDER_REPORTED,
            )
        )
    return tuple(usages)


# ── Partial-cost reconstruction (kill paths) ──────────────────────────
#
# When a run is killed (timeout, stuck-pattern, or missing result event) the
# terminal ``result`` event — which carries ``total_cost_usd`` and the
# aggregated ``modelUsage`` block — never arrives, so the normal cost path
# produces nothing. But every ``assistant`` stream event already captured in
# memory carries a per-message ``usage`` block (Anthropic usage shape). We
# aggregate those and price them from the model catalog's rates for the billed
# name the events report, so a killed run's real spend is attributed rather than
# silently dropped to $0.00. Where no
# usable usage was ever received, cost is recorded as unknown (None) — never a
# fabricated zero, so "unmeasured" stays distinct from "free".

# Anthropic prompt-cache pricing is expressed as multiples of the base input
# rate: cache reads bill at 0.1x and 5-minute cache writes at 1.25x (Anthropic
# pricing docs). A catalog entry that publishes its own cache-hit rate states it
# and is priced from that; otherwise these multipliers price the cached-token
# components off the entry's input rate.
_CACHE_READ_RATE_MULT = 0.1
_CACHE_WRITE_RATE_MULT = 1.25

# How long to let the CLI exit on its own once its stream is finished and stdin
# is closed, before killing it. Distinct from the post-SIGKILL reap window in
# process_group: this one is a genuine grace period for an orderly shutdown, so
# it is generous. What it must not be is unbounded — that is what turns a
# _kill_group() the platform sandbox refused into a wait for the CLI's whole
# natural lifetime (#1959).
_EXIT_GRACE_SECONDS = 10.0

# Models whose kill-path cost could not be reconstructed — warned once each so
# the log makes cost-unknown runs loud rather than silently zero.
_COST_UNMEASURED_WARNED: set[str] = set()


def _anthropic_cli_pricing_names() -> tuple[str, ...]:
    """Anthropic model names priced for the *CLI* transport, most specific first.

    Drawn from the installed rate registry, which is keyed by ``(provider, model,
    transport)`` — so the prefix match below can never resolve a CLI stream event
    onto a price declared for the same model name over the API (#2335). With no
    registry installed (unit tests, non-config code paths) the shipped catalog's
    own rates answer instead.
    """
    from theforge.runners.rate_registry import known_models  # noqa: PLC0415
    from theforge.runners.schema_utils import catalog_rates  # noqa: PLC0415

    # The shipped catalog is unioned in rather than used only as a fallback: the
    # names the CLI reports are concrete *billed* ids (``claude-sonnet-4-6``)
    # that a configuration need not have enabled — the registry may only know the
    # shorthand (``sonnet``) the profile dispatches under. ``catalog_rates`` reads
    # the packaged catalog only, so nothing project-declared for another
    # transport can enter here.
    names = set(known_models("anthropic", "cli"))
    names.update(model for provider, model in catalog_rates() if provider == "anthropic")
    # Longest first so a dated id matches its most specific family entry rather
    # than whichever shorter prefix happened to be enumerated first.
    return tuple(sorted(names, key=len, reverse=True))


def _resolve_anthropic_pricing_key(raw_model: str) -> str | None:
    """Map a stream-event model id to a priced anthropic CLI model name.

    Claude reports the fully-resolved model id (e.g. ``claude-sonnet-4-6`` or a
    dated variant) in each message, while rate cards are keyed on the undated
    family id. Match exactly, then by prefix in either direction so a dated id
    resolves to its family entry. Returns None when unknown.
    """
    if not raw_model:
        return None
    names = _anthropic_cli_pricing_names()
    if raw_model in names:
        return raw_model
    for key in names:
        if raw_model.startswith(key) or key.startswith(raw_model):
            return key
    return None


def _estimate_anthropic_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float | None:
    """Price reconstructed Anthropic token usage; None when model is unpriced.

    Cache tiers are expressed as multiples of the base input rate, except where
    the resolved rate card publishes its own cache-read figure — that is
    preferred over the generic 0.1x multiplier when declared.
    """
    from theforge.runners.schema_utils import catalog_rates, rates_for  # noqa: PLC0415

    key = _resolve_anthropic_pricing_key(model)
    if key is None:
        return None
    rates = rates_for("anthropic", key, "cli")
    if rates is None:
        # Shipped-catalog last resort, for the concrete billed ids above that the
        # loaded configuration does not enable. Deliberately reads the packaged
        # catalog rather than the merged registry: a project-declared price for
        # another transport must never reach this path.
        rates = catalog_rates().get(("anthropic", key))
        if rates is None:
            return None
    in_rate = rates.input_per_mtok
    out_rate = rates.output_per_mtok
    cache_read_rate = (
        rates.cached_input_per_mtok
        if rates.cached_input_per_mtok is not None
        else in_rate * _CACHE_READ_RATE_MULT
    )
    return (
        (input_tokens / 1_000_000) * in_rate
        + (output_tokens / 1_000_000) * out_rate
        + (cache_read_tokens / 1_000_000) * cache_read_rate
        + (cache_creation_tokens / 1_000_000) * in_rate * _CACHE_WRITE_RATE_MULT
    )


def _reconstruct_partial_cost(
    lines: list[str],
    profile: ModelProfile,
) -> tuple[float | None, tuple[ModelUsage, ...]]:
    """Reconstruct spend and per-model usage from partial stream-json events.

    Aggregates ``message.usage`` blocks across all captured ``assistant`` events
    and prices them via the pricing table. Returns ``(cost_usd, model_usage)``.
    ``cost_usd`` is None (cost-unknown, surfaced loudly) when no usable usage was
    observed or no observed model could be priced — never a fabricated ``0.0``.
    """
    per_model: dict[str, dict[str, int]] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message", {})
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        model_name = str(message.get("model") or profile.model or "?")
        acc = per_model.setdefault(
            model_name,
            {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        )
        acc["input"] += int(usage.get("input_tokens", 0) or 0)
        acc["output"] += int(usage.get("output_tokens", 0) or 0)
        acc["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)
        acc["cache_creation"] += int(usage.get("cache_creation_input_tokens", 0) or 0)

    if not per_model:
        return None, ()

    total_cost = 0.0
    any_priced = False
    usages: list[ModelUsage] = []
    for model_name, acc in per_model.items():
        cost = _estimate_anthropic_cost(
            model_name,
            acc["input"],
            acc["output"],
            acc["cache_read"],
            acc["cache_creation"],
        )
        if cost is not None:
            total_cost += cost
            any_priced = True
        usages.append(
            ModelUsage(
                model=model_name,
                input_tokens=acc["input"],
                output_tokens=acc["output"],
                cache_read_tokens=acc["cache_read"],
                cache_creation_tokens=acc["cache_creation"],
                cost_usd=cost,
                # Priced here from the pricing table, not billed by the CLI.
                cost_provenance=COST_ESTIMATED if cost is not None else COST_UNKNOWN,
            )
        )
    return (total_cost if any_priced else None), tuple(usages)


def _partial_cost_or_warn(
    lines: list[str],
    profile: ModelProfile,
    *,
    kill_reason: str,
) -> tuple[float | None, tuple[ModelUsage, ...]]:
    """Reconstruct kill-path cost, logging whether it was measured or unknown."""
    cost, usage = _reconstruct_partial_cost(lines, profile)
    label = profile.name or profile.model or "?"
    if cost is not None:
        _log(
            f"  {label} {kill_reason}: reconstructed partial cost ${cost:.4f} "
            f"from {len(usage)} model(s) of stream usage (result event never arrived)."
        )
    else:
        key = f"{label}:{profile.model}"
        if key not in _COST_UNMEASURED_WARNED:
            _COST_UNMEASURED_WARNED.add(key)
            _log(
                f"  WARNING: {label} {kill_reason} with no priceable stream usage; "
                "recording cost-unknown, NOT $0.00."
            )
    return cost, usage


def _format_tool_input_preview(inp: dict[str, Any]) -> str:
    """Return a short preview string for a tool's input dict."""
    if not inp:
        return ""
    for v in inp.values():
        if isinstance(v, str):
            return v[:120]
    return str(inp)[:120]


def _process_stream_event(line: str, label: str = "", *, label_prefix: str = "") -> None:
    """Process a single JSONL stream event and print tool activity to stderr.

    label: accepted for API compatibility but not used for formatting.
    label_prefix: if non-empty, prepended to tool activity lines
        (e.g. "[reviewer-a] "). Callers set this only in parallel pool mode.
    """
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return

    event_type = event.get("type")

    if event_type == "tool_use_summary":
        summary = event.get("summary", "")
        if summary:
            _log_verbose(f"  ↳ {label_prefix}{summary}")
    elif event_type == "assistant":
        message = event.get("message", {})
        content = message.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_name = item.get("name", "?")
                inp = item.get("input", {})
                preview = _format_tool_input_preview(inp)
                _log_verbose(f"  ↳ {label_prefix}{tool_name}: {preview}")


class _StreamCall:
    """Minimal duck-type for stuck_detection.build_observation()."""

    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: dict | None) -> None:
        self.name = name
        self.arguments = arguments if isinstance(arguments, dict) else {}


class _ClaudeStreamMonitor:
    """Group Claude stream events into iterations and feed a StuckTracker.

    Claude emits ``assistant`` events with one or more ``tool_use`` items per
    LLM turn, then ``user`` events with the matching ``tool_result`` items.
    We treat one assistant turn (with its tool_results) as one agent
    iteration. The monitor calls ``tracker.observe`` once per iteration.

    On a stuck-pattern termination the monitor records a reason and the
    runner kills the subprocess; the runner translates the reason into a
    failure ``AgentResult`` (exit_code -2) so the coordinator can attribute
    the early termination correctly.
    """

    def __init__(self, profile: ModelProfile) -> None:
        self._tracker = StuckTracker(profile)
        self._enabled = self._tracker.enabled
        self._tool_name_by_id: dict[str, str] = {}
        self._pending_calls: list[_StreamCall] = []
        self._pending_results: list[dict] = []
        self.terminate_reason: str | None = None
        self.terminate_pattern: str | None = None
        self.nudge_pattern: str | None = None  # last nudge pattern (for logging)
        self._pending_nudge: str | None = None  # nudge text awaiting delivery
        self.iteration_count = 0

    def consume_pending_nudge(self) -> str | None:
        """Return and clear the nudge message awaiting delivery, if any."""
        msg = self._pending_nudge
        self._pending_nudge = None
        return msg

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def should_terminate(self) -> bool:
        return self.terminate_reason is not None

    def ingest(self, line: str) -> None:
        if not self._enabled or not line:
            return
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        et = event.get("type")
        if et == "assistant":
            self._on_assistant(event)
        elif et == "user":
            self._on_user(event)
        elif et == "result":
            self._flush()

    def finalize(self) -> None:
        """Flush any unprocessed pending iteration at end of stream."""
        if self._enabled:
            self._flush()

    def _on_assistant(self, event: dict) -> None:
        msg = event.get("message", {}) or {}
        content = msg.get("content", []) or []
        new_calls: list[_StreamCall] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tid = item.get("id", "")
                name = item.get("name", "") or ""
                args = item.get("input", {})
                if tid:
                    self._tool_name_by_id[tid] = name
                new_calls.append(_StreamCall(name, args))
        if not new_calls:
            return
        # If a previous iteration's results never arrived, flush what we have.
        if self._pending_calls:
            self._flush()
        self._pending_calls = new_calls

    def _on_user(self, event: dict) -> None:
        msg = event.get("message", {}) or {}
        content = msg.get("content", []) or []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            tid = item.get("tool_use_id", "")
            tool_name = self._tool_name_by_id.get(tid, "")
            raw_content = item.get("content", "")
            if isinstance(raw_content, list):
                parts = []
                for piece in raw_content:
                    if isinstance(piece, dict) and "text" in piece:
                        parts.append(str(piece.get("text", "")))
                    else:
                        parts.append(str(piece))
                text = " ".join(parts)
            else:
                text = str(raw_content)
            is_error = bool(item.get("is_error"))
            content_str = f"Error: {text}" if is_error and not text.startswith("Error") else text
            self._pending_results.append({"name": tool_name, "content": content_str})
        if self._pending_calls and len(self._pending_results) >= len(self._pending_calls):
            self._flush()

    def _flush(self) -> None:
        if not self._pending_calls and not self._pending_results:
            return
        obs = build_observation(self._pending_calls, self._pending_results)
        nudge_msg, terminate_reason, pattern = self._tracker.observe(obs)
        self.iteration_count += 1
        if nudge_msg is not None:
            self.nudge_pattern = pattern
            self._pending_nudge = nudge_msg
        if terminate_reason is not None and self.terminate_reason is None:
            self.terminate_reason = terminate_reason
            self.terminate_pattern = pattern
        self._pending_calls = []
        self._pending_results = []


def _write_user_message(stdin: Any, text: str) -> None:
    """Write a single user message to claude's stream-json input pipe.

    Claude's ``--input-format stream-json`` reads one JSON object per line; each
    line that is a ``user`` message is processed as another conversation turn.
    Used both for the initial prompt and for stuck-detection nudges.
    """
    if stdin is None:
        return
    payload = {"type": "user", "message": {"role": "user", "content": text}}
    try:
        stdin.write(json.dumps(payload) + "\n")
        stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        # Subprocess closed stdin (e.g. after kill); silently ignore.
        pass


def _try_parse_handoff(output: str) -> dict | None:
    """Best-effort extraction of <forge_handoff> from agent output. Logs on parse error."""
    try:
        return extract_dev_handoff(output)
    except ParseError as exc:
        _log_verbose(f"  handoff parse error (non-fatal): {exc}")
        return None


def _extract_assistant_text(event: dict[str, Any]) -> str:
    """Return visible assistant-authored text from a Claude stream event."""
    if event.get("type") != "assistant":
        return ""
    message = event.get("message", {})
    if not isinstance(message, dict):
        return ""
    content = message.get("content", [])
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text", "")).strip()
        else:
            continue
        if text:
            parts.append(text)
    return "\n".join(parts)


def _extract_stream_output(lines: list[str]) -> str | None:
    """Prefer assistant text from stream-json events; otherwise preserve plain-text streams."""
    last_assistant_text = ""
    saw_json_event = False
    raw_text_parts: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            raw_text_parts.append(line)
            continue
        if isinstance(event, dict):
            saw_json_event = True
            assistant_text = _extract_assistant_text(event)
            if assistant_text:
                last_assistant_text = assistant_text

    if last_assistant_text:
        return last_assistant_text
    if raw_text_parts:
        return "".join(raw_text_parts).strip()
    if saw_json_event:
        return None
    return "".join(lines).strip() or None


def _tool_call_target(inp: dict[str, Any]) -> str | None:
    """Return the file/path/pattern a tool call operated on, or None.

    Prefers the concrete file argument shared by the read/edit/write tools,
    then falls back to search targets (``path``/``pattern``). Kept
    stack-neutral so it works for any tool the agent happens to invoke.
    """
    if not isinstance(inp, dict):
        return None
    for key in ("file_path", "notebook_path", "path", "pattern"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_tool_trace(lines: list[str]) -> tuple[dict[str, Any], ...]:
    """Extract observed tool calls from accumulated Claude stream-json lines.

    Claude emits ``assistant`` events whose ``message.content`` holds one or
    more ``tool_use`` items (``name`` + ``input``). This retains the ordered
    sequence of calls — ``{"tool": <name>, "target": <path/pattern or None>}``
    — so a crashed run's exploration is not lost. Best-effort: malformed lines
    are skipped, never raising.
    """
    trace: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                trace.append(
                    {
                        "tool": str(item.get("name", "?")),
                        "target": _tool_call_target(item.get("input", {})),
                    }
                )
    return tuple(trace)


def _build_no_text_marker(reason: str, *, subtype: str | None = None) -> str:
    """Return a machine-readable marker for streams that contained no text output."""
    marker = f"CLAUDE_STREAM_NO_TEXT: reason={reason}"
    if subtype:
        marker += f" subtype={subtype}"
    return marker


def _get_claude_session_id(
    output: str,
    cwd: Path,
    *,
    fallback_to_file: bool = True,
    min_mtime: float | None = None,
) -> str | None:
    """Extract a Claude session id from stream output or transcript files."""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            return sid

    if not fallback_to_file:
        return None

    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.is_dir():
        return None

    try:
        project_slug = str(cwd.resolve()).replace("/", "-")
        project_dir = claude_projects / project_slug
        if not project_dir.is_dir():
            return None

        candidates = []
        for path in project_dir.glob("*.jsonl"):
            mtime = path.stat().st_mtime
            if min_mtime is not None and mtime <= min_mtime:
                continue
            candidates.append((mtime, path))
    except OSError:
        return None

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1].stem


def _run_claude(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
    fallback_to_file: bool = True,
    quiet: bool = False,
    secrets: dict[str, str] | None = None,
    stop_event: "threading.Event | None" = None,
) -> AgentResult:
    """Invoke `claude -p --output-format stream-json --verbose` as a subprocess."""
    # NOTE: Claude CLI has no mechanically-enforced read-only mode. When
    # sandbox_mode == "read-only" the host wrapper still confines *writes* to
    # the worktree (see below), but it does not enforce read-only; we log a
    # warning so operators know that constraint is not syscall-enforced.
    if profile.sandbox_mode == "read-only":
        _log(
            "  WARNING: sandbox_mode=read-only is not mechanically enforced by Claude CLI; "
            "writes are contained to the worktree but reads are not restricted. "
            "Use a provider/API profile for true read-only enforcement."
        )
    cmd: list[str] = build_argv(profile=profile, session_id=session_id)

    # Mechanical write containment: wrap the Claude CLI in the host sandbox
    # (macOS sandbox-exec / Linux bwrap) so absolute-path writes and commits
    # cannot escape the story worktree (#1907). Claude's native --permission-mode
    # (added by build_argv) is retained as cooperative defense-in-depth, but it
    # is NOT the containment boundary — a dev agent bypasses it with absolute
    # paths, which is exactly how #1443 committed to the release checkout.
    #
    # Fail closed like runner_gemini: if the host sandbox is unavailable and
    # sandbox_mode is not "none", refuse to run rather than fall back to
    # prompt-only discipline. The earlier attempt (#920) was reverted (#925)
    # because the sandbox blocked Claude's macOS Keychain auth ("Not logged
    # in"); allow_credential_services now grants the securityd mach-lookup +
    # keychain reads, and ~/.claude stays writable, so auth and session
    # persistence survive containment.
    if profile.sandbox_mode != "none":
        try:
            sandboxed_cmd = workspace_effect_sandbox_command(
                cmd,
                working_dir,
                extra_write_roots=_claude_state_write_roots(),
                allow_credential_services=True,
                capability_profile=profile.sandbox_capability_profile,
                capability_write_roots=profile.sandbox_write_roots,
                capability_mach_services=profile.sandbox_mach_services,
            )
        except SandboxCapabilityError as exc:
            # Fail closed: a declared capability profile this host cannot express
            # must refuse the run, never degrade to default containment (#1947).
            _log(f"✗ claude: {exc}")
            return AgentResult(
                success=False,
                output=f"SANDBOX_CAPABILITY_PROFILE_UNSUPPORTED: {exc}",
                session_id=None,
                cost_usd=None,
                exit_code=-1,
                raw={},
                profile_name=profile.name,
                startup_failure=True,
            )
        if sandboxed_cmd[0] == cmd[0]:
            _log(
                f"✗ claude: sandbox_mode={profile.sandbox_mode!r} requested but platform "
                "sandbox (sandbox-exec/bwrap) is unavailable — refusing to run unsandboxed. "
                "Set sandbox_mode: none to explicitly opt out of write containment."
            )
            return AgentResult(
                success=False,
                output=(
                    f"SANDBOX_UNAVAILABLE: sandbox_mode={profile.sandbox_mode!r} is set but "
                    "the platform sandbox (sandbox-exec on macOS, bwrap on Linux) is not "
                    "available on this host. Claude CLI relies on OS sandboxing for "
                    "mechanical write containment. Set sandbox_mode: none to run without "
                    "write containment."
                ),
                session_id=None,
                cost_usd=None,
                exit_code=-1,
                raw={},
                profile_name=profile.name,
                startup_failure=True,
            )
        cmd = sandboxed_cmd

    # Unset CLAUDECODE so the subprocess isn't blocked by the nested-session check
    env = build_workspace_env(working_dir, extra=secrets)
    env.pop("CLAUDECODE", None)
    # Stamp the spawn's lease into the env every descendant inherits, so teardown
    # can still reach a tool the agent started that left the process group by
    # calling setsid — the escape group isolation alone cannot cover (#2309).
    env, lease = process_group.open_process_lease(env)
    # Watches the spawn's descendants for as long as it runs. The lease alone
    # cannot see one whose environment is unreadable (a SIP-protected platform
    # binary on macOS); this reads ppid/start-time, which always are.
    tracker: process_tree.DescendantTracker | None = None

    label = profile.name or profile.identity_label
    if not quiet:
        _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    start_wall = time.time()
    start = time.monotonic()
    deadline = start + profile.timeout_seconds
    timed_out = False
    stuck_monitor = _ClaudeStreamMonitor(profile)
    # Track the spawned process group so every teardown branch kills the whole
    # node/tool grandchild tree (not just the direct child) and the reaper can
    # clean up if the sprint is SIGKILL-ed mid-run. Defined before the try so the
    # finally can unregister even if Popen itself raises.
    pgid: int | None = None
    # Set by the finally when something did not end on its own, so every result
    # this function can return says whether the invocation left processes behind
    # and what had to be done about them (#2309).
    teardown: process_group.ProcessTeardown | None = None
    try:
        # start_new_session=True isolates the CLI (and its node/tool children)
        # into their own process group, making the whole tree killable at once.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(working_dir),
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return AgentResult(
            success=False,
            output="ERROR: 'claude' CLI not found. Is it installed?",
            session_id=None,
            cost_usd=None,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
            startup_failure=True,
        )

    # Set whenever a kill fails to reach the whole group, so the finally below
    # knows the tree may have outlived us and keeps the reaper's record. An Event
    # rather than a bool because _kill_group runs on the watchdog thread as well
    # as this one; Event carries its own lock, so the flag needs no reasoning
    # about which writes are atomic.
    group_kill_failed = threading.Event()
    # Set when the *watchdog* thread ends the run — the deadline passed, or a
    # stop_event cancelled it. Both are endings the in-loop checks already
    # record as `timed_out`, but the watchdog fires when the stream loop is
    # blocked and so cannot reach those checks, and the elapsed-time fallback
    # below only catches the deadline case once the run is 5% past its limit.
    # A watchdog kill landing exactly on the deadline therefore reached the
    # no-result branch unnamed — and once `killed_before_output` exists, an
    # agent that was given its full allowance and used it would be recorded as
    # an invocation that never ran, and refunded a retry it did spend (#2832).
    # An Event for the same reason as the flag above: the watchdog sets it.
    watchdog_killed = threading.Event()

    def _kill_group() -> None:
        # Kill the whole process group so node/tool grandchildren die too — a
        # bare proc.kill() reaches only the direct child. Fall back to the
        # direct child if the pgid is unknown or already gone.
        if pgid is not None:
            if not process_group.kill_agent_group(pgid):
                group_kill_failed.set()
        else:
            group_kill_failed.set()
            try:
                proc.kill()
            except OSError:
                pass

    # Any exception raised after spawn — the SystemExit that detach.py's SIGTERM
    # handler raises while this thread is blocked reading stdout, a
    # KeyboardInterrupt, or any error — must kill the whole group before the
    # finally drops the sidecar; otherwise the agent tree reparents to init still
    # holding its workspace-write sandbox, with no record left for the reaper.
    try:
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, TypeError):
            # OSError: child already gone. TypeError: non-int pid (test doubles).
            pgid = None
        if pgid is not None:
            process_group.register_agent_group(pgid, sandbox_dir=working_dir, lease=lease)
        tracker = process_group.descendant_tracker(root_pid=proc.pid, pgid=pgid)
        tracker.start()
        assert proc.stdin is not None
        # Send the initial prompt as a stream-json user message. stdin is kept
        # open so stuck-detection nudges can be injected as additional user
        # messages mid-run; it is closed only after the stream completes
        # (or the run is killed).
        _write_user_message(proc.stdin, prompt)

        lines: list[str] = []
        assert proc.stdout is not None

        # Enforce wall-clock timeout on the streaming loop via a watchdog thread.
        # proc.wait(timeout=...) only fires after stdout is drained, which never
        # happens if the agent streams indefinitely.
        def _watchdog() -> None:
            # Wake periodically so a stop_event signal cancels the in-flight
            # subprocess immediately rather than waiting for the deadline.
            while True:
                if proc.poll() is not None:
                    return
                if stop_event is not None and stop_event.is_set():
                    watchdog_killed.set()
                    _kill_group()
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if proc.poll() is None:
                        watchdog_killed.set()
                        _kill_group()
                    return
                time.sleep(min(0.5, remaining))

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        lp = f"[{label}] " if quiet else ""
        for line in proc.stdout:
            lines.append(line)
            stripped = line.strip()
            _process_stream_event(stripped, label_prefix=lp)
            stuck_monitor.ingest(stripped)
            pending_nudge = stuck_monitor.consume_pending_nudge()
            if pending_nudge is not None:
                _write_user_message(proc.stdin, pending_nudge)
                _log(f"  ⚠ {label} stuck-detection nudge sent: {stuck_monitor.nudge_pattern}")
            if stuck_monitor.should_terminate:
                _log(
                    f"  ⚠ {label} stuck-detection terminate after "
                    f"{stuck_monitor.iteration_count} iterations: "
                    f"{stuck_monitor.terminate_pattern}"
                )
                _kill_group()
                break
            if stop_event is not None and stop_event.is_set():
                _kill_group()
                timed_out = True
                break
            if time.monotonic() > deadline:
                _kill_group()
                timed_out = True
                break
            # Break as soon as the result event arrives — the stream is complete.
            # Closing stdin immediately lets the subprocess exit cleanly rather
            # than blocking until the watchdog fires (#1054).
            try:
                if stripped and json.loads(stripped).get("type") == "result":
                    break
            except (json.JSONDecodeError, ValueError):
                pass

        stuck_monitor.finalize()
        # Close stdin now that the stream is done (or the proc was killed) so
        # any reader cleanup completes; ignore errors if stdin is already
        # closed by the kill path.
        try:
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
        # Bounded. The stream is finished (result event, timeout, stop, or stuck
        # kill) and stdin is closed, so a CLI that has not exited by now is not
        # going to produce anything more. Waiting without a bound here is how a
        # _kill_group() the platform sandbox refused turns an enforced timeout
        # into a block for the CLI's full natural lifetime (#1959).
        try:
            proc.wait(timeout=_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            if not process_group.terminate_process_group(proc):
                group_kill_failed.set()
    except BaseException:
        # SIGTERM→SystemExit, KeyboardInterrupt, or any post-spawn error: kill the
        # whole group so it cannot outlive this process, then re-raise.
        _kill_group()
        # Reap the CLI before the finally inspects the group. An unwaited child
        # lingers as a zombie, which still counts as a group member — release
        # would then read our own corpse as a survivor and report a teardown
        # that never happened. Bounded, for the reason every wait here is (#1959).
        try:
            proc.wait(timeout=process_group.KILL_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise
    finally:
        # Normally the group is dead by now (clean exit, an in-loop _kill_group,
        # or the except above) and dropping the sidecar keeps the reaper from
        # chasing a dead pgid. But when a kill was refused the tree can outlive
        # us, and the sidecar is the only thing that can still reach it — so
        # release the record only once the group is actually gone. A clean exit
        # is no longer taken as proof the group went with the child: an agent
        # that started a long-running command and returned first leaves it
        # running, and release kills it rather than letting it outlive the story.
        teardown = process_group.release_group_record(
            pgid,
            group_killed=not group_kill_failed.is_set(),
            sandbox_dir=working_dir,
            lease=lease,
            tracker=tracker,
        )

    if stuck_monitor.should_terminate:
        partial_output = "".join(lines)
        _stuck_cost, _stuck_usage = _partial_cost_or_warn(
            lines, profile, kill_reason="killed on stuck-pattern"
        )
        return AgentResult(
            success=False,
            output=(
                f"Agent loop terminated: {stuck_monitor.terminate_reason} "
                f"(at iteration {stuck_monitor.iteration_count})"
            ),
            session_id=_get_claude_session_id(
                partial_output,
                working_dir,
                fallback_to_file=fallback_to_file,
                min_mtime=start_wall,
            ),
            cost_usd=_stuck_cost,
            cost_provenance=(COST_ESTIMATED if _stuck_cost is not None else COST_UNKNOWN),
            exit_code=-2,
            raw={},
            profile_name=profile.name,
            model_usage=_stuck_usage,
            failure_code="stuck_pattern",
            dev_handoff=_try_parse_handoff(partial_output),
            tool_trace=extract_tool_trace(lines),
            process_teardown=teardown,
            partial_output=_extract_stream_output(lines),
        )

    if (
        timed_out
        or watchdog_killed.is_set()
        or (time.monotonic() - start) >= profile.timeout_seconds * 1.05
    ):
        timed_out = True

    if timed_out:
        partial_output = "".join(lines)
        _timeout_output = f"TIMEOUT: Agent exceeded {profile.timeout_seconds}s limit"
        _timeout_cost, _timeout_usage = _partial_cost_or_warn(
            lines, profile, kill_reason="killed at timeout"
        )
        return AgentResult(
            success=False,
            output=_timeout_output,
            session_id=_get_claude_session_id(
                partial_output,
                working_dir,
                fallback_to_file=fallback_to_file,
                min_mtime=start_wall,
            ),
            cost_usd=_timeout_cost,
            cost_provenance=(COST_ESTIMATED if _timeout_cost is not None else COST_UNKNOWN),
            exit_code=-9,
            raw={},
            profile_name=profile.name,
            model_usage=_timeout_usage,
            failure_code="timeout",
            dev_handoff=_try_parse_handoff(partial_output),
            tool_trace=extract_tool_trace(lines),
            process_teardown=teardown,
            partial_output=_extract_stream_output(lines),
        )

    elapsed = time.monotonic() - start
    if not quiet:
        _log_verbose(f"  ... {label} done ({elapsed:.0f}s)")

    # Find the result line (type=result) in the JSONL stream
    result_json: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
            if event.get("type") == "result":
                result_json = event
                break
        except (json.JSONDecodeError, ValueError):
            continue

    if not result_json:
        extracted_output = _extract_stream_output(lines)
        stderr_text = ""
        if proc.stderr:
            try:
                stderr_text = proc.stderr.read()
            except Exception:
                pass
        if killed_before_output(
            exit_code=proc.returncode,
            # ``lines`` is every stream event the CLI emitted, so empty means it
            # emitted none — not one init, assistant, tool or usage event. The
            # timeout and stuck-pattern endings returned above, so what is left
            # here with a signal exit and an empty stream is the #2832 shape: the
            # stream closed with nothing and the process had to be killed at the
            # exit grace. ``extracted_output`` is checked too and is None when
            # there was nothing to extract, which is why it is not dereferenced.
            produced_output=bool(lines) or bool((extracted_output or "").strip()),
        ):
            # Nothing streamed before the signal landed, so there is no partial
            # cost to salvage and no session to resume: the $0.00 here is
            # measured, not unknown, which is what lets the coordinator tell this
            # apart from a run whose spend could not be read (#2832).
            _killed_output = KILLED_BEFORE_OUTPUT_MARKER
            if stderr_text.strip():
                _killed_output = f"{_killed_output}\n{stderr_text.strip()}"
            return AgentResult(
                success=False,
                output=_killed_output,
                failure_code=FAILURE_KILLED_BEFORE_OUTPUT,
                session_id=None,
                cost_usd=0.0,
                cost_provenance=COST_ESTIMATED,
                exit_code=proc.returncode,
                raw={},
                profile_name=profile.name,
                process_teardown=teardown,
            )
        _noresult_output = (
            extracted_output or stderr_text or _build_no_text_marker("missing_result_event")
        )
        _noresult_cost, _noresult_usage = _partial_cost_or_warn(
            lines, profile, kill_reason="ended without a result event"
        )
        # Name the ending, the way the timeout and stuck-pattern branches above
        # already do (#2427). Without a failure_code the only fact that reached
        # the coordinator was the exit status — a signal number records what was
        # done to the process, not what went wrong — so the agent's captured last
        # words were dropped and a run that had stated its own cause was reported
        # as unexplained. A clean exit that merely lacked a result event is not a
        # failure and gets no code.
        return AgentResult(
            success=proc.returncode == 0,
            output=_noresult_output,
            failure_code=(None if proc.returncode == 0 else FAILURE_ENDED_WITHOUT_RESULT),
            session_id=_get_claude_session_id(
                "".join(lines) or stderr_text,
                working_dir,
                fallback_to_file=fallback_to_file,
                min_mtime=start_wall,
            ),
            cost_usd=_noresult_cost,
            cost_provenance=(COST_ESTIMATED if _noresult_cost is not None else COST_UNKNOWN),
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
            model_usage=_noresult_usage,
            dev_handoff=_try_parse_handoff(_noresult_output),
            tool_trace=extract_tool_trace(lines),
            process_teardown=teardown,
            partial_output=None if proc.returncode == 0 else _extract_stream_output(lines),
        )

    try:
        raw_cost = result_json.get("total_cost_usd")
        cost = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        cost = None

    _success_output = result_json.get("result")
    if _success_output is None:
        _success_output = _extract_stream_output(lines) or _build_no_text_marker(
            "result_missing_text", subtype=str(result_json.get("subtype", "unknown"))
        )
    return AgentResult(
        success=proc.returncode == 0,
        output=_success_output,
        session_id=result_json.get("session_id"),
        cost_usd=cost,
        # ``total_cost_usd`` from the CLI's terminal result event.
        cost_provenance=(COST_PROVIDER_REPORTED if cost is not None else COST_UNKNOWN),
        exit_code=proc.returncode,
        raw=result_json,
        profile_name=profile.name,
        model_usage=_parse_model_usage(result_json),
        dev_handoff=_try_parse_handoff(_success_output),
        tool_trace=extract_tool_trace(lines),
        process_teardown=teardown,
    )
