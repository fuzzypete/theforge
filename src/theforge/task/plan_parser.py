from __future__ import annotations

from typing import TypedDict

import yaml


class _PlanStepRequired(TypedDict):
    id: int
    description: str
    files: list[str]
    action: str  # "modify" | "create" | "delete"
    details: str


class PlanStep(_PlanStepRequired, total=False):
    """A single step in a structured plan. depends_on is optional."""

    depends_on: list[int]


class _PlanDataRequired(TypedDict):
    approach: str
    steps: list[PlanStep]


class PlanData(_PlanDataRequired, total=False):
    """Structured plan output parsed from YAML."""

    criteria_mapping: list[dict]
    risks: list[dict]


def parse_plan_output(text: str) -> PlanData | None:
    """Parse structured YAML plan output from a plan agent.

    Returns a PlanData dict on success, or None if the text is not valid
    structured plan YAML (e.g. freeform markdown fallback).
    """
    stripped = text.strip()

    # Strip fenced code block if present (```yaml ... ```)
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove first line (```yaml or ```) and last line (```)
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        stripped = "\n".join(inner).strip()

    # Detect YAML plan: starts with 'plan:' or '---' followed by 'plan:'
    if not (stripped.startswith("plan:") or stripped.startswith("---")):
        return None

    # Strip YAML document markers if present
    if stripped.startswith("---"):
        stripped = stripped[3:].strip()

    try:
        data = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict) or "plan" not in data:
        return None

    plan = data["plan"]
    if not isinstance(plan, dict):
        return None

    # Validate required top-level keys
    if "approach" not in plan or "steps" not in plan:
        return None

    if not isinstance(plan["steps"], list):
        return None

    # Validate each step has required fields
    for step in plan["steps"]:
        if not isinstance(step, dict):
            return None
        for required_field in ("id", "description", "files", "action", "details"):
            if required_field not in step:
                return None

    result: PlanData = {
        "approach": str(plan["approach"]),
        "steps": plan["steps"],
    }
    if "criteria_mapping" in plan and isinstance(plan["criteria_mapping"], list):
        result["criteria_mapping"] = plan["criteria_mapping"]
    if "risks" in plan and isinstance(plan["risks"], list):
        result["risks"] = plan["risks"]

    return result
