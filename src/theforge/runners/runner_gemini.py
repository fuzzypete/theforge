"""Gemini (Google) CLI runner.

Invokes `npx @google/gemini-cli -p <prompt> --yolo -m <model> -o json`
as a subprocess and returns an AgentResult.
"""

from __future__ import annotations

import json
from dataclasses import replace
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
from .rate_registry import CACHED_INPUT_RATE_MULT as _CACHED_INPUT_RATE_MULT
from .sandbox import SandboxCapabilityError, workspace_effect_sandbox_command

# ── Logging helpers ───────────────────────────────────────────────────


def _log(msg: str) -> None:
    _log_line("[forge]", msg)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL, LogLevel  # noqa: PLC0415

    if _LOG_LEVEL >= LogLevel.VERBOSE:
        _log_line("[forge]", msg)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_gemini_usage(
    result_json: dict[str, Any],
    profile: "ModelProfile",
) -> tuple[float | None, tuple[ModelUsage, ...]]:
    """Price whatever token usage the Gemini CLI reported, or return nothing.

    The Gemini CLI reports usage in a ``stats.models`` block on the invocations
    that emit one and reports nothing on the others (parse failures, sandbox
    refusals, error exits). Where a count exists it is priced from the rate card
    for the *gemini CLI* identity specifically — never from a gemini API
    identity's rates, which the transport-keyed lookup makes structural rather
    than a matter of care (#2335). Where no count exists this returns
    ``(None, ())`` and the caller records cost-unknown: an estimate is never
    fabricated from an absent measurement.
    """
    from .schema_utils import rates_for  # noqa: PLC0415

    stats = result_json.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if not isinstance(models, dict) or not models:
        return None, ()

    total = 0.0
    any_priced = False
    usages: list[ModelUsage] = []
    for model_name, block in models.items():
        tokens = block.get("tokens") if isinstance(block, dict) else None
        if not isinstance(tokens, dict):
            continue
        cached = _int(tokens.get("cached"))
        # ``prompt`` is the full prompt count and includes the cached portion,
        # which is what _estimate_cost documents ``cached_input_tokens`` to be.
        input_tokens = _int(tokens.get("prompt"))
        output_tokens = _int(tokens.get("candidates"))
        thinking_tokens = _int(tokens.get("thoughts"))
        if not (input_tokens or output_tokens or thinking_tokens):
            continue
        rates = rates_for("google", str(model_name), "cli")
        cost: float | None = None
        if rates is not None:
            uncached = max(0, input_tokens - min(cached, input_tokens))
            cached_rate = (
                rates.cached_input_per_mtok
                if rates.cached_input_per_mtok is not None
                else rates.input_per_mtok * _CACHED_INPUT_RATE_MULT
            )
            cost = (
                (uncached / 1_000_000) * rates.input_per_mtok
                + (min(cached, input_tokens) / 1_000_000) * cached_rate
                + ((output_tokens + thinking_tokens) / 1_000_000) * rates.output_per_mtok
            )
            total += cost
            any_priced = True
        usages.append(
            ModelUsage(
                model=str(model_name),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=min(cached, input_tokens),
                cache_creation_tokens=0,
                cost_usd=cost,
                thinking_tokens=thinking_tokens,
                # Forge multiplied tokens by a rate card; the CLI billed nothing.
                cost_provenance=COST_ESTIMATED if cost is not None else COST_UNKNOWN,
            )
        )
    if not usages:
        return None, ()
    return (total if any_priced else None), tuple(usages)


def _try_parse_handoff(output: str) -> dict | None:
    """Best-effort extraction of <forge_handoff> from agent output. Logs on parse error."""
    try:
        return extract_dev_handoff(output)
    except ParseError as exc:
        _log_verbose(f"  handoff parse error (non-fatal): {exc}")
        return None


# ── Argv builder ──────────────────────────────────────────────────────


def build_argv(
    *,
    profile: ModelProfile,
    prompt: str,
    session_id: str | None = None,
) -> list[str]:
    """Construct argv for `npx @google/gemini-cli` invocation."""
    cmd: list[str] = ["npx", "@google/gemini-cli"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += [
        "-p",
        prompt,
        "--yolo",
        "-m",
        profile.model,
        "-o",
        "json",
    ]
    return cmd


# ── Gemini runner ─────────────────────────────────────────────────────


def _run_gemini(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
    quiet: bool = False,
    is_pool: bool = False,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Invoke `npx @google/gemini-cli -p <prompt> --yolo -m <model> -o json` as a subprocess.

    Session resume: gemini --resume accepts "latest", an index number, or a UUID.
    Sessions are scoped to the current working directory. We return "latest" so the
    next sequential call resumes the same project session. This is only safe for
    single-reviewer runs — parallel pools get session_id=None because "--resume latest"
    is not invocation-scoped and two concurrent gemini reviewers would trample each
    other's context.
    """
    cmd: list[str] = build_argv(profile=profile, prompt=prompt, session_id=session_id)

    # NOTE: Gemini CLI has no --config flag for thinking config, so the model
    # uses its default thinking level. This is declared to the router as
    # KNOB_NONE in routing.REASONING_EFFORT_TRANSPORT_SUPPORT, so score-driven
    # reasoning effort (#1108) is never *set* on a gemini-CLI profile — the
    # routing_decision records provider_unsupported instead of silently
    # dropping a value here.

    # Gemini CLI has no native sandbox flag. When sandboxing is requested, wrap the
    # command with the platform sandbox (macOS Seatbelt / Linux bwrap). This is the
    # correct approach for Gemini — unlike #624 (which double-sandboxed claude-within-
    # claude), wrapping the Gemini binary is safe because Gemini has no native sandbox
    # of its own.
    # Fail closed: if the platform sandbox is unavailable and sandbox_mode is not
    # "none", return a failure result rather than running unsandboxed. The sibling
    # detector has been removed, so running without containment would leave writes
    # undetected. Set sandbox_mode: none explicitly to opt out of containment.
    if profile.sandbox_mode != "none":
        try:
            sandboxed_cmd = workspace_effect_sandbox_command(
                cmd,
                working_dir,
                capability_profile=profile.sandbox_capability_profile,
                capability_write_roots=profile.sandbox_write_roots,
                capability_mach_services=profile.sandbox_mach_services,
            )
        except SandboxCapabilityError as exc:
            # Fail closed: a declared capability profile this host cannot express
            # must refuse the run, never degrade to default containment (#1947).
            _log(f"✗ gemini: {exc}")
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
                f"✗ gemini: sandbox_mode={profile.sandbox_mode!r} requested but platform "
                "sandbox (sandbox-exec/bwrap) is unavailable — refusing to run unsandboxed. "
                "Set sandbox_mode: none to explicitly opt out of write containment."
            )
            return AgentResult(
                success=False,
                output=(
                    f"SANDBOX_UNAVAILABLE: sandbox_mode={profile.sandbox_mode!r} is set but "
                    "the platform sandbox (sandbox-exec on macOS, bwrap on Linux) is not "
                    "available on this host. Gemini CLI has no native sandbox flag. "
                    "Set sandbox_mode: none to run without write containment."
                ),
                session_id=None,
                cost_usd=None,
                exit_code=-1,
                raw={},
                profile_name=profile.name,
                startup_failure=True,
            )
        cmd = sandboxed_cmd

    label = profile.name or profile.identity_label
    _gemini_env = build_workspace_env(working_dir, extra=secrets)
    # Out-parameter carrying a forced teardown back from the spawn (#2309); see
    # run_in_process_group. Populated only when the group outlived the CLI.
    _teardowns: list[process_group.ProcessTeardown] = []
    outcome, elapsed = _run_with_heartbeat(
        # Group-isolated spawn, same as codex: a bare subprocess.run leaves the
        # npm→node→gemini tree (and anything the agent started) alive on timeout,
        # and its clean-exit path never checks whether the group emptied at all.
        run_fn=lambda: process_group.run_in_process_group(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(working_dir),
            timeout=profile.timeout_seconds,
            env=_gemini_env,
            teardown_out=_teardowns,
        ),
        label=label,
        profile=profile,
        cli_name="gemini",
        quiet=quiet,
    )
    _teardown = _teardowns[0] if _teardowns else None

    if outcome.exception:
        result = _handle_exception(outcome.exception, profile=profile, cli_name="gemini")
        if result:
            return replace(result, process_teardown=_teardown)
        raise outcome.exception

    proc = outcome.proc
    assert proc is not None
    if not quiet:
        _log_verbose(f"  ... {label} done ({elapsed:.0f}s)")

    if killed_before_output(
        exit_code=proc.returncode,
        produced_output=bool((proc.stdout or "").strip()),
    ):
        _killed_output = KILLED_BEFORE_OUTPUT_MARKER
        if (proc.stderr or "").strip():
            _killed_output = f"{_killed_output}\n{proc.stderr.strip()}"
        return AgentResult(
            success=False,
            output=_killed_output,
            failure_code=FAILURE_KILLED_BEFORE_OUTPUT,
            session_id=None,
            # Measured $0.00 — nothing streamed, so nothing was billed. Unlike
            # the no-JSON return below, this shape knows its spend.
            cost_usd=0.0,
            cost_provenance=COST_ESTIMATED,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
            process_teardown=_teardown,
        )

    # Parse JSON output (-o json requests structured response).
    # The gemini CLI emits preamble lines (e.g. "YOLO mode is enabled.") to
    # stdout before the JSON object, so find the first '{' and parse from there.
    result_json: dict[str, Any] = {}
    json_start = proc.stdout.find("{")
    json_candidate = proc.stdout[json_start:] if json_start != -1 else proc.stdout
    try:
        result_json = json.loads(json_candidate)
    except (json.JSONDecodeError, ValueError):
        # Don't return "latest" on parse failure: the CLI may have exited before
        # creating a resumable session, so resuming would attach to stale context.
        _nojson_output = proc.stdout or proc.stderr or "(no output)"
        return AgentResult(
            success=proc.returncode == 0,
            output=_nojson_output,
            session_id=None,
            cost_usd=None,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
            dev_handoff=_try_parse_handoff(_nojson_output),
            process_teardown=_teardown,
        )

    # "latest" is only safe for sequential single-reviewer runs; parallel pools
    # would trample each other since --resume latest is not invocation-scoped.
    resume_sid = None if is_pool else "latest"
    _gemini_output = result_json.get("response", result_json.get("result", proc.stdout))
    # Only this path — the one that parsed a result — can carry usage. The
    # error, sandbox-refusal and no-JSON returns above keep cost_usd=None, so an
    # error's cost never changes from unknown to an estimate.
    _cost, _usage = _parse_gemini_usage(result_json, profile)
    return AgentResult(
        success=proc.returncode == 0,
        output=_gemini_output,
        session_id=resume_sid,
        cost_usd=_cost,
        exit_code=proc.returncode,
        raw=result_json,
        profile_name=profile.name,
        dev_handoff=_try_parse_handoff(_gemini_output),
        process_teardown=_teardown,
        model_usage=_usage,
        cost_provenance=COST_ESTIMATED if _cost is not None else COST_UNKNOWN,
    )
