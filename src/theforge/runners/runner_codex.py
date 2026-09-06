"""Codex (OpenAI) CLI runner.

Invokes `npx @openai/codex@<pinned> exec --json` as a subprocess, captures
agent text via a temp file (`-o`), and returns an AgentResult.

The package spec is **version-pinned** (``CODEX_PACKAGE``). An unpinned ``npx``
spec resolves to whatever npm currently serves, so an upstream release reaches
this project the moment it publishes, with no commit, no review and no gate. A
CLI that removes or renames a flag then fails every Codex-transport agent at
argv parsing — before any model is contacted, so the run costs $0.00 and no
budget signal fires either. That is how 0.147.0's removal of ``--full-auto``
took out the reviewer pool and the escalation advisor mid-sprint. Raising the
pin is a reviewed change like any other, which is the point: the version the
agents run becomes a fact in the tree rather than a property of the day.

``--json`` puts codex in machine-readable event mode, which is the ONLY way its
token usage reaches forge: the human transport prints a bare total that cannot be
priced against an (input, output) table. Recording every run cost-unknown is what
deadlocked multi-story sprints on the fail-closed budget check (#2019). Usage is
parsed from ``turn.completed`` events, priced with cached input discounted, and
salvaged from partial output when a run is killed mid-flight.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from theforge import process_group
from theforge.agent_types import (
    COST_ESTIMATED,
    COST_UNKNOWN,
    FAILURE_KILLED_BEFORE_OUTPUT,
    KILLED_BEFORE_OUTPUT_MARKER,
    AgentResult,
    ModelUsage,
    killed_before_output,
)
from theforge.log_util import _log_line
from theforge.task.handoff_parser import ParseError, extract_dev_handoff
from theforge.workspace_env import build_workspace_env

from ..config import ModelProfile
from .cli import _handle_exception, _run_with_heartbeat
from .schema_utils import _estimate_cost

# Pinned Codex CLI. See the module docstring for why this is not a floating
# spec. Both the fresh and the resume argv build from this one constant so the
# two paths can never drift onto different CLI versions.
CODEX_PACKAGE = "@openai/codex@0.147.0"

# Emit the cost-unmeasured warning at most once per model to avoid log spam.
_COST_UNMEASURED_WARNED: set[str] = set()

# ── Logging helpers ───────────────────────────────────────────────────


def _log(msg: str) -> None:
    _log_line("[forge]", msg)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL, LogLevel  # noqa: PLC0415

    if _LOG_LEVEL >= LogLevel.VERBOSE:
        _log_line("[forge]", msg)


def _try_parse_handoff(output: str) -> dict | None:
    """Best-effort extraction of <forge_handoff> from agent output. Logs on parse error."""
    try:
        return extract_dev_handoff(output)
    except ParseError as exc:
        _log_verbose(f"  handoff parse error (non-fatal): {exc}")
        return None


# ── Argv builders ─────────────────────────────────────────────────────


def build_argv(
    *,
    profile: ModelProfile,
    working_dir: Path,
    output_file: Path,
    prompt: str,
) -> list[str]:
    """Construct argv for a fresh `codex exec` invocation.

    ``--json`` puts codex in machine-readable event mode: it streams JSONL thread
    events on stdout, including a ``turn.completed`` event carrying the turn's
    token usage split. Without it the only usage figure codex prints is a bare
    human-readable total, which cannot be priced against an (input, output) table
    — so every run was recorded cost-unknown and a multi-story sprint deadlocked
    on the fail-closed budget check (#2019). The ``-o`` last-message file is kept
    for agent text; ``--json`` only changes what stdout carries.

    ``--skip-git-repo-check`` is mandatory, not opportunistic. Codex refuses to
    run in any working directory it cannot verify as a trusted git repository
    ("Not inside a trusted directory and --skip-git-repo-check was not
    specified") and exits before contacting the model. Forge legitimately runs
    agents against directories with no ``.git`` at all — the preflight /
    escalation-advisor baseline checkout is built by ``git archive | tar -x``, a
    plain file tree — so without this flag any role adaptive routing assigns to a
    Codex CLI model against that baseline fails to launch 100% of the time
    (#2164). Forge already selects its own containment via ``--sandbox``; the
    trust gate is the CLI's interactive-user heuristic, not a security boundary
    forge relies on.
    """
    cmd: list[str] = [
        "npx",
        CODEX_PACKAGE,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-m",
        profile.model,
    ]
    if profile.reasoning_effort:
        cmd += ["-c", f"model_reasoning_effort={profile.reasoning_effort}"]
    if profile.sandbox_mode != "none":
        cmd += ["--sandbox", profile.sandbox_mode]
    cmd += ["-C", str(working_dir), "-o", str(output_file), prompt]
    return cmd


def build_resume_argv(
    *,
    profile: ModelProfile,
    output_file: Path,
    session_id: str,
) -> list[str]:
    """Construct argv for `codex exec resume` (prompt provided via stdin).

    Sandbox continuity is reasserted explicitly here rather than inherited from
    the resumed session. The fresh `codex exec` run selects its containment with
    ``--sandbox <mode>``, but the resume subcommand rejects that flag, so forge
    restates the SAME native Codex policy via the config override
    ``-c sandbox_mode=<mode>`` — accepted on the resume path — instead of trusting
    the CLI to carry the original policy forward. This keeps containment a
    mechanical guarantee of forge's rather than a documented assumption about CLI
    inheritance.

    ``--strict-config`` makes the reassertion fail closed: today codex silently
    ignores an unrecognized ``-c`` key, so if a future CLI renames or drops the
    ``sandbox_mode`` field the override would be dropped and the resume would run
    under whatever policy the session defaulted to — a silent containment
    downgrade. With ``--strict-config`` codex instead errors out on the unknown
    field, and the run surfaces as a failure rather than a quiet loss of
    containment.

    ``sandbox_mode: none`` opts out of the reassertion (as on the fresh path);
    the ``--full-auto`` alias is intentionally dropped because it is deprecated
    (codex maps it to ``--sandbox workspace-write``) and would contradict an
    explicit ``read-only`` override.

    ``--json`` is reasserted here for the same reason it is set on the fresh path
    (#2019): usage is only machine-readable in event mode, and a resumed dev
    iteration is exactly as billable as the first one. The resume subcommand is
    known to reject some flags the fresh path accepts (``--sandbox``, above), so
    the contract test parametrizes resume alongside fresh — a future CLI that
    stops accepting ``--json`` on resume surfaces there rather than at dogfood
    time.
    """
    cmd: list[str] = [
        "npx",
        CODEX_PACKAGE,
        "exec",
        "resume",
        "--json",
        "-m",
        profile.model,
    ]
    # Reassert the session's sandbox policy explicitly (see docstring). Guarded
    # by --strict-config so a future CLI that stops recognizing sandbox_mode
    # fails loudly instead of silently downgrading containment.
    if profile.sandbox_mode != "none":
        cmd += ["--strict-config", "-c", f"sandbox_mode={profile.sandbox_mode}"]
    if profile.reasoning_effort:
        cmd += ["-c", f"model_reasoning_effort={profile.reasoning_effort}"]
    cmd += ["-o", str(output_file), session_id, "-"]
    return cmd


# ── Codex-specific helpers ────────────────────────────────────────────


def _get_codex_session_id(*, min_mtime: float) -> str | None:
    """Return the newest codex session ID created after min_mtime.

    Scans ~/.codex/session_index.jsonl for entries whose updated_at timestamp
    is strictly after min_mtime (epoch seconds). Same pattern as the Claude
    transcript-file fallback in _get_claude_session_id().
    """
    index_file = Path.home() / ".codex" / "session_index.jsonl"
    try:
        lines = index_file.read_text().splitlines()
    except OSError:
        return None

    best_id: str | None = None
    best_ts: float | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = entry.get("id")
        updated = entry.get("updated_at")
        if not sid or not updated:
            continue
        try:
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except ValueError:
            continue
        if ts > min_mtime and (best_ts is None or ts > best_ts):
            best_ts = ts
            best_id = sid
    return best_id


def _coerce_int(value: Any) -> int | None:
    """Parse an int from a JSON value or a ``"12,345"``-style string; None if not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = value.replace(",", "").strip()
        if digits.isdigit():
            return int(digits)
    return None


# ── JSON event stream (`codex exec --json`) ───────────────────────────


@dataclass(frozen=True)
class _CodexUsage:
    """Token usage recovered from codex ``turn.completed`` events.

    Mirrors codex's own ``TokenUsage`` shape. Two containment relationships hold
    there and are relied on for pricing:

    * ``cached_input_tokens`` is a SUBSET of ``input_tokens`` (codex's own
      ``non_cached_input()`` is ``input - cached``), so cached tokens are
      discounted out of the input total rather than charged on top of it.
    * ``reasoning_output_tokens`` is a SUBSET of ``output_tokens``, so it is
      recorded for visibility but NOT added to billable output — doing so would
      charge reasoning twice and overstate measured spend, which is exactly the
      dishonesty #1992 set out to remove.

    ``cache_write_input_tokens`` is likewise part of ``input_tokens`` and carries
    no surcharge on OpenAI pricing, so it is recorded but priced at the normal
    input rate (i.e. left inside the uncached remainder).
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __add__(self, other: "_CodexUsage") -> "_CodexUsage":
        return _CodexUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_input_tokens=(
                self.cache_write_input_tokens + other.cache_write_input_tokens
            ),
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=(self.reasoning_output_tokens + other.reasoning_output_tokens),
        )


def _iter_codex_events(stdout: str) -> "Iterator[dict[str, Any]]":
    """Yield parsed JSONL event objects from a `codex exec --json` stdout stream.

    Non-JSON lines are skipped rather than treated as an error: codex may
    interleave a plain-text banner or warning, and a partially-written final line
    is normal in the killed-run path.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            yield event


def _first_int(block: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _coerce_int(block.get(key))
        if value is not None:
            return value
    return None


def _parse_usage_block(block: dict[str, Any]) -> _CodexUsage | None:
    """Parse one codex ``usage`` object; None when it carries no token counts."""
    input_tokens = _first_int(block, ("input_tokens", "prompt_tokens"))
    output_tokens = _first_int(block, ("output_tokens", "completion_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    return _CodexUsage(
        input_tokens=input_tokens or 0,
        cached_input_tokens=_first_int(block, ("cached_input_tokens", "cached_tokens")) or 0,
        cache_write_input_tokens=(
            _first_int(block, ("cache_write_input_tokens", "cache_creation_input_tokens")) or 0
        ),
        output_tokens=output_tokens or 0,
        reasoning_output_tokens=(
            _first_int(block, ("reasoning_output_tokens", "reasoning_tokens")) or 0
        ),
    )


def _usage_from_events(stdout: str) -> _CodexUsage | None:
    """Sum the ``usage`` of every ``turn.completed`` event in a codex event stream.

    Each ``turn.completed`` reports the usage of the turn that just finished, so
    turns are summed — the same convention the Claude runner uses for its own
    per-message usage events. A single-turn ``codex exec`` (the common case) makes
    sum and last-event identical; the choice only matters for a run that completes
    several turns in one process, where summing is what "what this process spent"
    means. Returns None when no turn ever reported usage.
    """
    total: _CodexUsage | None = None
    for event in _iter_codex_events(stdout):
        if event.get("type") != "turn.completed":
            continue
        block = event.get("usage")
        if not isinstance(block, dict):
            turn = event.get("turn")
            block = turn.get("usage") if isinstance(turn, dict) else None
        if not isinstance(block, dict):
            continue
        parsed = _parse_usage_block(block)
        if parsed is None:
            continue
        total = parsed if total is None else total + parsed
    return total


def _agent_text_from_events(stdout: str) -> str | None:
    """Reconstruct the agent's visible message text from a codex event stream.

    Only used when the ``-o`` last-message file came back empty. Before ``--json``
    that fallback dumped raw stdout, which under event mode would feed JSONL
    straight into handoff and review parsing.
    """
    texts: list[str] = []
    for event in _iter_codex_events(stdout):
        etype = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if etype in ("item.completed", "item.updated") and item is not None:
            kind = item.get("type") or item.get("item_type")
            if kind in ("agent_message", "assistant_message"):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        elif etype in ("agent_message", "assistant_message"):
            text = event.get("text") or event.get("message")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    if not texts:
        return None
    return "\n\n".join(texts)


def _error_text_from_events(stdout: str) -> str | None:
    """Collect error/failure messages from a codex event stream.

    Needed because ``--json`` can carry a failure entirely on stdout as an
    ``error``/``turn.failed`` event. Without surfacing it into ``AgentResult.output``
    the CLI→API quota fallback classifier, which pattern-matches that text, would
    stop seeing rate-limit and quota errors on this transport.
    """
    messages: list[str] = []
    for event in _iter_codex_events(stdout):
        if event.get("type") not in ("error", "turn.failed", "thread.failed"):
            continue
        for candidate in (event.get("message"), event.get("error"), event.get("detail")):
            if isinstance(candidate, str) and candidate.strip():
                messages.append(candidate.strip())
                break
            if isinstance(candidate, dict):
                nested = candidate.get("message")
                if isinstance(nested, str) and nested.strip():
                    messages.append(nested.strip())
                    break
    if not messages:
        return None
    return "\n".join(messages)


# Event types that prove a turn actually began. ``turn.failed`` counts: the model
# was engaged, so the run is NOT a pre-turn launch failure even though no usage
# was reported for it.
_TURN_ACTIVITY_EVENT_TYPES = frozenset(
    {
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "agent_message",
        "assistant_message",
    }
)

# Stable failure identifier for "the CLI exited before any billable turn began".
LAUNCH_FAILURE_CODE = "cli_launch_failure"


def _exited_before_any_turn(stdout: str) -> bool:
    """True when a codex stdout stream shows no turn ever began.

    Under ``--json`` codex narrates every turn and item as it happens, so the
    absence of ANY turn/item activity event means the process died before the
    model was engaged — the trust-gate refusal, a bad flag, a config error. That
    is a genuinely free run, distinct from a run that executed and whose usage
    could not be parsed.

    Deliberately fails closed on anything it cannot read: a stdout line that is
    not a parseable event object means the process emitted output this parser
    does not understand, so "nothing happened" is no longer an established fact
    and the run keeps its cost-unknown classification. Only a stream that is
    entirely pre-turn events (or empty) proves a zero.
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("{"):
            return False
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(event, dict) or event.get("type") in _TURN_ACTIVITY_EVENT_TYPES:
            return False
    return True


# A provider refusal issued BEFORE generation begins — an HTTP 400
# ``invalid_request_error`` such as "the 'gpt-5.4' model is not supported when
# using Codex with a ChatGPT account" — is a cost of zero: the request was
# declined, not served, so no tokens existed to bill (#2913).
#
# This is deliberately narrower than "the stream reported no usage". A call that
# ran and whose accounting was lost looks identical in that one respect and must
# keep its cost-unknown classification; what separates the two is the provider's
# own explicit statement that the request was never valid. Anything that could
# have consumed tokens before failing — an upstream 5xx, a quota refusal mid
# turn, a timeout — is NOT on this list and stays unmeasured.
_PRE_GENERATION_REFUSAL_STATUS = 400
_PRE_GENERATION_REFUSAL_ERROR_TYPES = frozenset({"invalid_request_error"})

# Stable failure identifier for "the provider refused the request before
# generating anything". Distinct from LAUNCH_FAILURE_CODE: the CLI started fine
# and reached the provider, so this is not a startup failure — only the cost
# conclusion (a measured $0.00) is shared.
PRE_GENERATION_REFUSAL_CODE = "provider_refused_before_generation"


def _refusal_payloads(event: dict[str, Any]) -> "Iterator[dict[str, Any]]":
    """Yield the dicts of an event that may carry provider error fields.

    Codex nests the provider's error differently depending on where the failure
    surfaced: at the top level of an ``error`` event, under ``error`` on a
    ``turn.failed``, or one level deeper as the provider's own response body.
    """
    yield event
    nested = event.get("error")
    if isinstance(nested, dict):
        yield nested
        deeper = nested.get("error")
        if isinstance(deeper, dict):
            yield deeper


def _is_pre_generation_refusal_event(event: dict[str, Any]) -> bool:
    """True when *event* carries an explicit pre-generation provider refusal.

    Requires BOTH signals — the 400 status and the ``invalid_request_error``
    type — because either alone is weaker than the claim being made. A status
    with no error type could be a transport artefact; an error type with no
    status could be a message forge merely relayed.
    """
    if event.get("type") not in ("error", "turn.failed", "thread.failed"):
        return False
    has_status = False
    has_error_type = False
    for payload in _refusal_payloads(event):
        for status_key in ("status", "status_code", "http_status"):
            if _coerce_int(payload.get(status_key)) == _PRE_GENERATION_REFUSAL_STATUS:
                has_status = True
        error_type = payload.get("type")
        if isinstance(error_type, str) and error_type in _PRE_GENERATION_REFUSAL_ERROR_TYPES:
            has_error_type = True
    return has_status and has_error_type


def _classify_pre_generation_refusal(stdout: str) -> str | None:
    """Return the provider's refusal message when nothing was ever generated.

    Fails closed the same way ``_exited_before_any_turn`` does, and for the same
    reason: a line this parser cannot read means output exists that is not
    accounted for, so "nothing was generated" stops being an established fact.
    Any evidence of generation — reported usage, a completed turn, agent text —
    likewise disqualifies the stream, because a refusal alongside real work is a
    run that spent something.
    """
    refusal: str | None = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(event, dict):
            return None
        if event.get("type") == "turn.completed":
            return None
        if refusal is None and _is_pre_generation_refusal_event(event):
            refusal = _refusal_message(event)
    if refusal is None:
        return None
    if _usage_from_events(stdout) is not None or _agent_text_from_events(stdout) is not None:
        return None
    return refusal


def _refusal_message(event: dict[str, Any]) -> str:
    """The most specific human-readable text on a refusal event."""
    for payload in reversed(tuple(_refusal_payloads(event))):
        for key in ("message", "detail"):
            text = payload.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()[:500]
    return "provider refused the request before generating (400 invalid_request_error)"


def _launch_failure_reason(stderr: str, stdout: str, returncode: int) -> str:
    """Distil the CLI's own explanation for a pre-turn exit into one line.

    Codex prefixes deprecation notices and progress chatter before the real
    refusal, so ``warning:``/``Reading additional input`` lines are dropped and
    the last substantive line wins.
    """
    for stream in (stderr, stdout):
        lines = [ln.strip() for ln in (stream or "").splitlines() if ln.strip()]
        substantive = [
            ln
            for ln in lines
            if not ln.lower().startswith(("warning:", "reading additional input"))
            and not ln.startswith("{")
        ]
        candidates = substantive or lines
        if candidates:
            return candidates[-1][:500]
    return f"codex exited {returncode} before starting a turn (no output)"


def _classify_codex_launch_failure(*, returncode: int, stdout: str, stderr: str) -> str | None:
    """Return a launch-failure reason, or None when this was not a pre-turn exit."""
    if returncode == 0:
        return None
    if not _exited_before_any_turn(stdout):
        return None
    return _launch_failure_reason(stderr, stdout, returncode)


def _looks_like_codex_events(stdout: str) -> bool:
    """True when stdout is a codex JSONL event stream rather than human text."""
    return next(_iter_codex_events(stdout), None) is not None


def _usage_from_json(result_json: dict[str, Any]) -> tuple[int, int] | None:
    """Best-effort (input_tokens, output_tokens) from a parsed codex JSON blob.

    Defensive fallback only. The default codex transport writes its last message
    to the ``-o`` file as PLAIN TEXT (``--output-last-message``), not JSON, so
    this path does not fire on a normal run — it exists to opportunistically pick
    up a usage block when codex is invoked in a structured-output mode
    (e.g. ``--output-schema``) that happens to emit one. Tolerates several
    plausible shapes: a ``usage``/``token_usage``/``token_count`` dict keyed by
    ``input_tokens``/``prompt_tokens``/``input`` (and the output analogues).
    Returns None when a usable input+output pair cannot be found.
    """
    if not isinstance(result_json, dict):
        return None
    for key in ("usage", "token_usage", "token_count", "tokens"):
        block = result_json.get(key)
        if not isinstance(block, dict):
            continue
        input_tokens = None
        for in_key in ("input_tokens", "prompt_tokens", "input", "prompt"):
            input_tokens = _coerce_int(block.get(in_key))
            if input_tokens is not None:
                break
        output_tokens = None
        for out_key in ("output_tokens", "completion_tokens", "output", "completion"):
            output_tokens = _coerce_int(block.get(out_key))
            if output_tokens is not None:
                break
        if input_tokens is not None and output_tokens is not None:
            return (input_tokens, output_tokens)
    return None


# Verified against Codex CLI v0.142.x: the human-readable run summary reports
# only a TOTAL token count, on its own line, e.g.::
#     tokens used
#     11,374
# A bare total cannot be priced with the (input, output) pricing table, so the
# default human transport is intentionally recorded cost-unknown rather than
# fabricating a split. This regex therefore matches ONLY a single-line summary
# that both names tokens AND carries an explicit input+output split (as a
# structured/JSON-style summary would). It is deliberately tight:
#   * anchored to a line containing "token" (MULTILINE ``^``), and
#   * confined to one line (``[^\n]`` instead of DOTALL ``.*?``),
# so it cannot fabricate a cost by matching unrelated "input … output" prose
# elsewhere in the agent's stdout.
_USAGE_LINE_RE = re.compile(
    r"^[^\n]*token[^\n]*?input[^0-9]*(?P<input>[\d,]+)[^\n]*?output[^0-9]*(?P<output>[\d,]+)",
    re.IGNORECASE | re.MULTILINE,
)


def _usage_from_text(text: str) -> tuple[int, int] | None:
    """Best-effort (input_tokens, output_tokens) from a codex stdout summary line.

    Scans for a single-line token-usage summary that names both input and output
    token counts (e.g. ``tokens used: input 2,800 output 621``). Returns None
    when no such split is present — including the real CLI's total-only ``tokens
    used`` line — because a bare total cannot be priced honestly.
    """
    if not text:
        return None
    match = _USAGE_LINE_RE.search(text)
    if not match:
        return None
    input_tokens = _coerce_int(match.group("input"))
    output_tokens = _coerce_int(match.group("output"))
    if input_tokens is None or output_tokens is None:
        return None
    return (input_tokens, output_tokens)


def _extract_codex_cost(
    *,
    profile: ModelProfile,
    result_json: dict[str, Any],
    stdout: str,
) -> tuple[float | None, tuple[ModelUsage, ...]]:
    """Recover real cost from codex output; never fabricate a zero.

    Returns ``(cost_usd, model_usage)``. When token usage can be recovered we
    price it via the shared pricing table and populate ``model_usage``; when it
    cannot, ``cost_usd`` is ``None`` (cost-unknown, surfaced loudly) — never
    ``0.0``, so an unmeasured run stays distinct from a genuinely free one.
    """
    event_usage = _usage_from_events(stdout)
    if event_usage is not None:
        return _price_codex_usage(profile=profile, usage=event_usage)

    # Legacy/defensive fallbacks: a structured-output blob in the -o file, or a
    # synthetic single-line stdout summary carrying an explicit input+output
    # split. Neither distinguishes cache tiers, so they price flat — which is why
    # the line-scan is skipped entirely once stdout is known to be an event
    # stream. There the event parser is authoritative, and letting the regex match
    # a JSON usage line instead would silently re-charge cached input at the full
    # rate, reintroducing the overstatement the split exists to avoid.
    usage = _usage_from_json(result_json)
    if usage is None and not _looks_like_codex_events(stdout):
        usage = _usage_from_text(stdout)
    if usage is None:
        model = profile.model or "?"
        if model not in _COST_UNMEASURED_WARNED:
            _COST_UNMEASURED_WARNED.add(model)
            _log(
                f"WARNING: Codex CLI run for model={model} completed cost-unmeasured "
                "(no token usage in output); recording cost-unknown, NOT $0.00."
            )
        return None, ()
    input_tokens, output_tokens = usage
    return _price_codex_usage(
        profile=profile,
        usage=_CodexUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _price_codex_usage(
    *,
    profile: ModelProfile,
    usage: _CodexUsage,
) -> tuple[float | None, tuple[ModelUsage, ...]]:
    """Price a recovered codex usage split into (cost_usd, model_usage).

    Cached input is discounted rather than charged at the uncached rate; reasoning
    output is recorded but not re-billed (it is already inside ``output_tokens``).
    ``cost_usd`` is None when the model has no pricing entry, but the usage split
    is still returned so the audit records the measured tokens.
    """
    cost = _estimate_cost(
        "openai",
        profile.model,
        usage.input_tokens,
        usage.output_tokens,
        # Codex is a CLI transport: its tokens are priced from the codex identity,
        # never from the same model name reached over the OpenAI API (#2335).
        transport="cli",
        cached_input_tokens=usage.cached_input_tokens,
    )
    model_usage = (
        ModelUsage(
            model=profile.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cached_input_tokens,
            cache_creation_tokens=usage.cache_write_input_tokens,
            cost_usd=cost,
            thinking_tokens=usage.reasoning_output_tokens,
            # Codex reports tokens, never a price: this is forge's pricing-table
            # derivation, not a billed figure.
            cost_provenance=COST_ESTIMATED if cost is not None else COST_UNKNOWN,
        ),
    )
    return cost, model_usage


def _partial_codex_cost(
    *,
    profile: ModelProfile,
    stdout: str,
) -> tuple[float | None, tuple[ModelUsage, ...]]:
    """Price the usage a killed codex run emitted before it was terminated.

    A timed-out run still cost real money for every turn it completed. Because
    ``codex exec --json`` emits ``turn.completed`` as each turn lands, the partial
    stdout salvaged from the timeout usually carries priceable usage. Only when no
    turn ever reported usage is the run recorded cost-unknown — the honest answer
    then, but no longer the automatic one.
    """
    usage = _usage_from_events(stdout)
    label = profile.name or profile.model or "?"
    if usage is None:
        key = f"partial:{label}:{profile.model}"
        if key not in _COST_UNMEASURED_WARNED:
            _COST_UNMEASURED_WARNED.add(key)
            _log(
                f"  WARNING: Codex CLI run for {label} was killed before reporting any "
                "token usage; recording cost-unknown, NOT $0.00."
            )
        return None, ()
    cost, model_usage = _price_codex_usage(profile=profile, usage=usage)
    if cost is not None:
        _log(
            f"  {label} killed mid-run: recovered partial cost ${cost:.4f} from "
            "codex turn.completed usage (run did not finish)."
        )
    return cost, model_usage


def _run_codex(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
    quiet: bool = False,
    is_pool: bool = False,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Invoke `npx @openai/codex exec --full-auto` as a subprocess.

    Output is captured via a temp file using `-o <file>`;
    falls back to stdout if the file is empty.

    Session ID extraction scans ~/.codex/session_index.jsonl for the newest
    entry after the run start. This is safe for sequential (single-reviewer)
    runs but not for parallel pools — when is_pool=True we return None to
    avoid misattributing a concurrent invocation's session to this one.
    """
    fd, output_path_str = tempfile.mkstemp(suffix=".txt", prefix="forge_codex_")
    os.close(fd)
    output_file = Path(output_path_str)

    # Resume: `codex exec resume [flags] <id> -` (prompt via stdin).
    # The resume path rejects `--sandbox`, so build_resume_argv reasserts the
    # same containment via `-c sandbox_mode=<mode>` (guarded by --strict-config
    # to fail closed on CLI drift). Fresh runs still take the explicit --sandbox.
    # Fresh start: `codex exec [flags] <prompt>` (prompt as positional arg).
    if session_id:
        cmd: list[str] = build_resume_argv(
            profile=profile, output_file=output_file, session_id=session_id
        )
        stdin_prompt: str | None = prompt
    else:
        cmd = build_argv(
            profile=profile,
            working_dir=working_dir,
            output_file=output_file,
            prompt=prompt,
        )
        stdin_prompt = None

    start_wall = time.time()
    label = profile.name or profile.identity_label
    _codex_env = build_workspace_env(working_dir, extra=secrets)
    # Out-parameter for a forced teardown (#2309). It has to be a channel outside
    # the return value because the timeout path raises rather than returns, and a
    # kill on that path is exactly the fact the run needs to record.
    _teardowns: list[process_group.ProcessTeardown] = []
    outcome, elapsed = _run_with_heartbeat(
        # Group-isolated spawn: subprocess.run's own timeout kill reaches only
        # `npm exec`, leaving node + the codex leaf alive. run_in_process_group
        # killpg-s the whole npm→node→codex tree on timeout/teardown.
        run_fn=lambda: process_group.run_in_process_group(
            cmd,
            input=stdin_prompt,
            capture_output=True,
            text=True,
            timeout=profile.timeout_seconds,
            env=_codex_env,
            cwd=str(working_dir),
            teardown_out=_teardowns,
        ),
        label=label,
        profile=profile,
        cli_name="npx @openai/codex",
        quiet=quiet,
    )

    _teardown = _teardowns[0] if _teardowns else None

    try:
        if outcome.exception:
            result = _handle_exception(
                outcome.exception,
                profile=profile,
                cli_name="npx @openai/codex",
                # Salvage usage from the partial event stream before declaring a
                # killed run unmeasurable (#2019).
                partial_cost_fn=lambda partial: _partial_codex_cost(
                    profile=profile, stdout=partial
                ),
            )
            if result:
                # _handle_exception is transport-agnostic and knows nothing about
                # process groups, so the teardown fact is grafted on here rather
                # than threaded through it.
                return replace(result, process_teardown=_teardown)
            raise outcome.exception

        proc = outcome.proc
        assert proc is not None
        if not quiet:
            _log_verbose(f"  ... {label} done ({elapsed:.0f}s)")

        # Read output file; fall back to stdout then stderr
        output_text = ""
        try:
            content = output_file.read_text(encoding="utf-8").strip()
            if content:
                output_text = content
        except OSError:
            pass
        # Whether anything was produced at all, asked before the fallbacks below
        # substitute a placeholder for it (#2832).
        _produced_output = bool(output_text) or bool((proc.stdout or "").strip())

        if not output_text:
            # Under `--json` stdout is a JSONL event stream, so the old raw-stdout
            # fallback would feed event objects into handoff/review parsing.
            # Reconstruct the agent's message from the events instead; fall back to
            # raw stdout only when it is not an event stream at all.
            raw_stdout = proc.stdout or ""
            reconstructed = _agent_text_from_events(raw_stdout)
            if reconstructed:
                output_text = reconstructed
            elif _looks_like_codex_events(raw_stdout):
                # No agent message means the run failed. Surface the event
                # stream's error text so the CLI→API quota fallback classifier
                # can still see rate-limit/quota strings.
                output_text = (
                    _error_text_from_events(raw_stdout)
                    or proc.stderr
                    or "(no agent message in codex event stream)"
                )
            else:
                output_text = raw_stdout or proc.stderr or "(no output)"

        if killed_before_output(
            exit_code=proc.returncode,
            produced_output=_produced_output,
        ):
            _killed_output = KILLED_BEFORE_OUTPUT_MARKER
            if (proc.stderr or "").strip():
                _killed_output = f"{_killed_output}\n{proc.stderr.strip()}"
            return AgentResult(
                success=False,
                output=_killed_output,
                failure_code=FAILURE_KILLED_BEFORE_OUTPUT,
                session_id=None,
                # Measured $0.00: no turn began, so there is nothing to price
                # and nothing that could have been billed.
                cost_usd=0.0,
                cost_provenance=COST_ESTIMATED,
                exit_code=proc.returncode,
                raw={},
                profile_name=profile.name,
                process_teardown=_teardown,
            )

        # Try JSON parse for structured response
        result_json: dict[str, Any] = {}
        try:
            result_json = json.loads(output_text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Only extract session_id for sequential runs; parallel pools risk
        # picking up a sibling invocation's entry from the global index.
        extracted_sid = None if is_pool else _get_codex_session_id(min_mtime=start_wall)

        # A pre-turn exit (trust gate, bad flag, config error) spent nothing —
        # that is a MEASURED $0.00, not the cost-unknown a completed-but-
        # unparseable run gets. Classify it before pricing so the cost-unmeasured
        # warning is not emitted for a process that never reached the model.
        launch_reason = _classify_codex_launch_failure(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
        # A provider refusal issued before generation began is likewise a
        # measured $0.00 — the request was declined, not served (#2913). Unlike
        # a launch failure the CLI started and reached the provider, so this is
        # NOT a startup failure; only the cost conclusion is shared.
        refusal_reason = (
            None
            if launch_reason is not None or proc.returncode == 0
            else _classify_pre_generation_refusal(proc.stdout or "")
        )
        if launch_reason is not None:
            cost_usd: float | None = 0.0
            model_usage: tuple[ModelUsage, ...] = ()
            _log(
                f"  {label} FAILED TO LAUNCH (exit {proc.returncode}, no turn began): "
                f"{launch_reason} — recording measured $0.00, not cost-unknown."
            )
        elif refusal_reason is not None:
            cost_usd = 0.0
            model_usage = ()
            _log(
                f"  {label} REFUSED BY PROVIDER before generation "
                f"(exit {proc.returncode}, model {profile.model}): {refusal_reason} "
                "— recording measured $0.00, not cost-unknown."
            )
        else:
            # Best-effort real cost from codex output. Unrecoverable → cost-unknown
            # (None), surfaced loudly, never a fabricated $0.00.
            cost_usd, model_usage = _extract_codex_cost(
                profile=profile,
                result_json=result_json,
                stdout=proc.stdout or "",
            )
        _startup_failure = launch_reason is not None
        if launch_reason is not None:
            _failure_code: str | None = LAUNCH_FAILURE_CODE
        elif refusal_reason is not None:
            _failure_code = PRE_GENERATION_REFUSAL_CODE
        else:
            _failure_code = None

        if result_json:
            _json_output = result_json.get("result", output_text)
            return AgentResult(
                success=proc.returncode == 0,
                output=_json_output,
                session_id=extracted_sid,
                cost_usd=cost_usd,
                cost_provenance=(COST_ESTIMATED if cost_usd is not None else COST_UNKNOWN),
                exit_code=proc.returncode,
                raw=result_json,
                profile_name=profile.name,
                model_usage=model_usage,
                dev_handoff=_try_parse_handoff(_json_output),
                startup_failure=_startup_failure,
                failure_code=_failure_code,
                process_teardown=_teardown,
            )

        return AgentResult(
            success=proc.returncode == 0,
            output=output_text,
            session_id=extracted_sid,
            cost_usd=cost_usd,
            cost_provenance=(COST_ESTIMATED if cost_usd is not None else COST_UNKNOWN),
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
            model_usage=model_usage,
            dev_handoff=_try_parse_handoff(output_text),
            startup_failure=_startup_failure,
            failure_code=_failure_code,
            process_teardown=_teardown,
        )
    finally:
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass
