"""Submit tool schema builders and tool-registry mapping for API runners.

Owns:
  - _submit_review_schema / _submit_plan_review_schema — review/plan-review tool contracts
  - _build_submit_tools_openai / _anthropic / _google — provider-specific shaping
  - _CLI_TO_REGISTRY / _build_registry_tools — tool selection from forge.yaml profiles
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from theforge.runners.loop_manager import SUBMIT_PLAN_REVIEW, SUBMIT_REVIEW
from theforge.runners.tool_runtime import TOOL_REGISTRY
from theforge.schemas import review_json_schema

if TYPE_CHECKING:
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
    from theforge.runners.runner_google import _sanitize_schema_for_google

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
