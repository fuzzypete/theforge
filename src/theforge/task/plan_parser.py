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


def _extract_plan_block(text: str) -> str | None:
    """Return the rooted ``plan:`` YAML block from mixed agent output."""

    lines = text.strip().splitlines()
    start_index: int | None = None
    base_indent = 0

    for index, line in enumerate(lines):
        if line.lstrip().startswith("plan:"):
            start_index = index
            base_indent = len(line) - len(line.lstrip())
            break

    if start_index is None:
        return None

    block: list[str] = []
    for index, line in enumerate(lines[start_index:], start=start_index):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if index > start_index and stripped and indent <= base_indent:
            break
        block.append(line[base_indent:] if len(line) >= base_indent else line)

    return "\n".join(block).strip() or None


def parse_plan_output(text: str) -> PlanData | None:
    """Parse structured YAML plan output from a plan agent.

    Returns a PlanData dict on success, or None if the text is not valid
    structured plan YAML (e.g. freeform markdown fallback).
    """
    stripped = text.strip()
    if not stripped:
        return None

    stripped = _extract_plan_block(stripped)
    if stripped is None:
        return None

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
