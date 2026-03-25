"""API-based agent runners for text-judgment agents.

Public entry point: run_api_agent()

This module owns:
  - Submit tool schema builders (the review/plan-review contract)
  - _build_registry_tools / _CLI_TO_REGISTRY (tool selection)
  - run_api_agent() — top-level dispatch

Provider adapters, finalizers, and loop entry points live in loop_runners.py.
The AgentLoopManager and loop infrastructure live in loop_manager.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from theforge.runners.cli import _log, _log_verbose
from theforge.runners.loop_manager import (  # noqa: F401 — re-exported for tests/compat
    _RESPONSES_API_MODELS,
    PRICING_TABLE,
    SUBMIT_PLAN_REVIEW,
    SUBMIT_REVIEW,
    AgentLoopManager,
    Finalizer,
    LoopTurn,
    ProviderAdapter,
    ToolCallRequest,
    _estimate_cost,
    _is_local_endpoint,
    _is_reasoning_model,
)
from theforge.runners.loop_runners import (  # noqa: F401 — re-exported for tests/compat
    _LOOP_RUNNERS,
    PROVIDER_RUNNERS,
    _deepseek_client,
    _make_google_adapter,
    _openai_result,
    _run_deepseek,
    _run_loop_openai,
)
from theforge.runners.tool_runtime import TOOL_REGISTRY
from theforge.schemas import review_json_schema

if TYPE_CHECKING:
    from theforge.agent_types import AgentResult
    from theforge.config import ModelProfile


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
    from theforge.runners.loop_runners import _sanitize_schema_for_google

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


# ── Tool selection ────────────────────────────────────────────────────

_CLI_TO_REGISTRY: dict[str, str] = {
    # Claude CLI tool names → API registry keys
    "Read": "read_file",
    "Edit": "edit_file",
    "Write": "write_file",
    "Bash": "bash",
    "Glob": "glob",
    "Grep": "grep",
}


def _build_registry_tools(profile: "ModelProfile") -> list:
    """Build the list of ToolDef objects from profile.allowed_tools.

    Normalises Claude CLI tool names (Read, Edit, Write, Bash, Glob, Grep) to
    their API registry equivalents so that forge.yaml profiles work for both
    CLI runners and API runners (e.g. Ollama via --dev-model).
    """
    result = []
    for name in profile.allowed_tools:
        key = _CLI_TO_REGISTRY.get(
            name, name
        )  # normalise CLI name; fall through if already API name
        if key in TOOL_REGISTRY:
            result.append(TOOL_REGISTRY[key])
    return result


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
