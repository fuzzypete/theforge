"""API-based agent runners for text-judgment agents.

Public entry point: run_api_agent()

This module owns run_api_agent() — the top-level dispatch.

Submit tool schema builders and tool-registry mapping live in submit_tools.py.
Provider adapters, finalizers, and loop entry points live in runner_<provider>.py.
The AgentLoopManager and loop infrastructure live in loop_manager.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from theforge.runners.cli import _log, _log_verbose
from theforge.runners.loop_runners import _LOOP_RUNNERS, PROVIDER_RUNNERS

if TYPE_CHECKING:
    from theforge.agent_types import AgentResult
    from theforge.config import ModelProfile


# ── Public entry point ────────────────────────────────────────────────


def run_api_agent(
    *,
    prompt: str,
    profile: "ModelProfile",
    working_dir: Path,
    quiet: bool = False,
    secrets: dict[str, str] | None = None,
    plain_text: bool = False,
) -> "AgentResult":
    """Run a text-judgment agent via API.

    When profile.allowed_tools is non-empty, drives an agent loop where the model
    can call tools. When empty, falls back to a single-shot stateless call.
    """
    from theforge.agent_types import AgentResult

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
        if profile.provider == "google" and plain_text:
            result = runner_fn(prompt, profile, secrets, plain_text=True)
        else:
            result = runner_fn(prompt, profile, secrets)

    if not quiet:
        status = "OK" if result.success else "FAIL"
        cost_str = f"${result.cost_usd:.3f}" if result.cost_usd is not None else "unknown"
        _log_verbose(f"  ... {label} done | {status} | cost={cost_str}")

    return result
