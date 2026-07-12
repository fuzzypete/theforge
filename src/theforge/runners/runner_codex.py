"""Codex (OpenAI) CLI runner.

Invokes `npx @openai/codex exec --full-auto` as a subprocess,
captures output via a temp file, and returns an AgentResult.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from theforge.agent_types import AgentResult, ModelUsage
from theforge.log_util import _log_line
from theforge.task.handoff_parser import ParseError, extract_dev_handoff
from theforge.workspace_env import build_workspace_env

from ..config import ModelProfile
from .cli import _handle_exception, _run_with_heartbeat
from .schema_utils import _estimate_cost

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
    """Construct argv for a fresh `codex exec` invocation."""
    cmd: list[str] = [
        "npx",
        "@openai/codex",
        "exec",
        "--full-auto",
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
    """Construct argv for `codex exec resume` (prompt provided via stdin)."""
    cmd: list[str] = [
        "npx",
        "@openai/codex",
        "exec",
        "resume",
        "--full-auto",
        "-m",
        profile.model,
    ]
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


def _usage_from_json(result_json: dict[str, Any]) -> tuple[int, int] | None:
    """Best-effort (input_tokens, output_tokens) from a parsed codex JSON blob.

    Tolerates several plausible shapes: a ``usage``/``token_usage``/``token_count``
    dict keyed by ``input_tokens``/``prompt_tokens``/``input`` (and the output
    analogues). Returns None when a usable input+output pair cannot be found.
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


_USAGE_LINE_RE = re.compile(
    r"input[^0-9]*(?P<input>[\d,]+).*?output[^0-9]*(?P<output>[\d,]+)",
    re.IGNORECASE | re.DOTALL,
)


def _usage_from_text(text: str) -> tuple[int, int] | None:
    """Best-effort (input_tokens, output_tokens) from a codex stdout summary line.

    Scans for a token-usage summary that names both input and output token
    counts (e.g. ``tokens used: input 2,800 output 621``). Returns None when no
    such split is present — a bare total is not enough to price honestly.
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
    usage = _usage_from_json(result_json) or _usage_from_text(stdout)
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
    cost = _estimate_cost("openai", profile.model, input_tokens, output_tokens)
    model_usage = (
        ModelUsage(
            model=profile.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=cost,
        ),
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
    # Current Codex CLI exposes `--full-auto` on resume but not `--sandbox`.
    # Fresh runs still accept the explicit sandbox flag.
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
    label = profile.name or f"{profile.cli or profile.provider}/{profile.model}"
    _codex_env = build_workspace_env(working_dir, extra=secrets)
    outcome, elapsed = _run_with_heartbeat(
        run_fn=lambda: subprocess.run(
            cmd,
            input=stdin_prompt,
            capture_output=True,
            text=True,
            timeout=profile.timeout_seconds,
            env=_codex_env,
            cwd=str(working_dir),
        ),
        label=label,
        profile=profile,
        cli_name="npx @openai/codex",
        quiet=quiet,
    )

    try:
        if outcome.exception:
            result = _handle_exception(
                outcome.exception, profile=profile, cli_name="npx @openai/codex"
            )
            if result:
                return result
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

        if not output_text:
            output_text = proc.stdout or proc.stderr or "(no output)"

        # Try JSON parse for structured response
        result_json: dict[str, Any] = {}
        try:
            result_json = json.loads(output_text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Only extract session_id for sequential runs; parallel pools risk
        # picking up a sibling invocation's entry from the global index.
        extracted_sid = None if is_pool else _get_codex_session_id(min_mtime=start_wall)

        # Best-effort real cost from codex output. Unrecoverable → cost-unknown
        # (None), surfaced loudly, never a fabricated $0.00.
        cost_usd, model_usage = _extract_codex_cost(
            profile=profile,
            result_json=result_json,
            stdout=proc.stdout or "",
        )

        if result_json:
            _json_output = result_json.get("result", output_text)
            return AgentResult(
                success=proc.returncode == 0,
                output=_json_output,
                session_id=extracted_sid,
                cost_usd=cost_usd,
                exit_code=proc.returncode,
                raw=result_json,
                profile_name=profile.name,
                model_usage=model_usage,
                dev_handoff=_try_parse_handoff(_json_output),
            )

        return AgentResult(
            success=proc.returncode == 0,
            output=output_text,
            session_id=extracted_sid,
            cost_usd=cost_usd,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
            model_usage=model_usage,
            dev_handoff=_try_parse_handoff(output_text),
        )
    finally:
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass
