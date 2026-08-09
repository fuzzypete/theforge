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
from theforge.agent_types import AgentResult
from theforge.log_util import _log_line
from theforge.task.handoff_parser import ParseError, extract_dev_handoff
from theforge.workspace_env import build_workspace_env

from ..config import ModelProfile
from .cli import _handle_exception, _run_with_heartbeat
from .sandbox import SandboxCapabilityError, workspace_effect_sandbox_command

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
    return AgentResult(
        success=proc.returncode == 0,
        output=_gemini_output,
        session_id=resume_sid,
        cost_usd=None,
        exit_code=proc.returncode,
        raw=result_json,
        profile_name=profile.name,
        dev_handoff=_try_parse_handoff(_gemini_output),
        process_teardown=_teardown,
    )
