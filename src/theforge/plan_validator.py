"""Mechanical plan structure validation — advisory checks run before DEV.

All checks are pure Python (no LLM). Returns findings as a list of dicts.
Validation is advisory — findings are logged and recorded but never block DEV.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def validate_plan(
    plan: dict,
    story_criteria: list[str],
    workspace_path: Path,
) -> list[dict]:
    """Run four mechanical validation checks on a structured plan.

    Args:
        plan: A PlanData dict (from parse_plan_output).
        story_criteria: Acceptance criterion strings from the story.
        workspace_path: Root of the workspace for file-existence checks.

    Returns:
        List of finding dicts: {"check": str, "message": str}.
        Empty list means all checks passed.
    """
    findings: list[dict] = []
    steps: list[dict] = plan.get("steps", [])
    criteria_mapping: list[dict] = plan.get("criteria_mapping", [])

    # ── 1. AC coverage ────────────────────────────────────────────────
    mapped_criteria = {entry.get("criterion", "") for entry in criteria_mapping}
    for criterion in story_criteria:
        if criterion and criterion not in mapped_criteria:
            findings.append(
                {
                    "check": "ac_coverage",
                    "message": f"Acceptance criterion not mapped in plan: {criterion!r}",
                }
            )

    # ── 2. Step completeness ──────────────────────────────────────────
    for step in steps:
        step_id = step.get("id", "?")
        if not step.get("description"):
            findings.append(
                {
                    "check": "step_completeness",
                    "message": f"Step {step_id}: missing or empty 'description'",
                }
            )
        if not step.get("files"):
            findings.append(
                {
                    "check": "step_completeness",
                    "message": f"Step {step_id}: missing or empty 'files'",
                }
            )
        if not step.get("action"):
            findings.append(
                {
                    "check": "step_completeness",
                    "message": f"Step {step_id}: missing or empty 'action'",
                }
            )

    # ── 3. File validity ──────────────────────────────────────────────
    for step in steps:
        action = step.get("action", "")
        if action not in ("modify", "delete"):
            continue
        step_id = step.get("id", "?")
        for filepath in step.get("files", []):
            target = workspace_path / filepath
            if not target.exists():
                findings.append(
                    {
                        "check": "file_validity",
                        "message": (
                            f"Step {step_id}: file {filepath!r} referenced with"
                            f" action={action!r} does not exist in workspace"
                        ),
                    }
                )

    # ── 4. Dependency ordering ────────────────────────────────────────
    all_ids: set[int] = {step.get("id") for step in steps if "id" in step}

    # Build adjacency list and check for unknown refs
    adjacency: dict[int, list[int]] = {}
    for step in steps:
        step_id = step.get("id")
        if step_id is None:
            continue
        deps: list[int] = step.get("depends_on", [])
        adjacency[step_id] = list(deps)
        for dep in deps:
            if dep not in all_ids:
                findings.append(
                    {
                        "check": "dependency_ordering",
                        "message": (
                            f"Step {step_id}: depends_on references unknown step id {dep}"
                        ),
                    }
                )

    # Cycle detection via iterative DFS
    visited: set[int] = set()
    in_stack: set[int] = set()
    cycle_found = False

    def _dfs(node: int) -> bool:
        visited.add(node)
        in_stack.add(node)
        for neighbor in adjacency.get(node, []):
            if neighbor not in visited:
                if _dfs(neighbor):
                    return True
            elif neighbor in in_stack:
                return True
        in_stack.discard(node)
        return False

    for node in all_ids:
        if not cycle_found and node not in visited:
            if _dfs(node):
                cycle_found = True
                findings.append(
                    {
                        "check": "dependency_ordering",
                        "message": "Circular dependency detected in plan steps",
                    }
                )

    return findings


def extract_story_criteria(story_content: str) -> list[str]:
    """Extract acceptance criteria lines from a story's markdown body.

    Looks for lines matching '- [ ] <criterion>' under any heading that
    contains 'acceptance criteria' (case-insensitive).

    Returns a list of criterion strings (without the checkbox prefix).
    Returns empty list when no AC section is found.
    """
    criteria: list[str] = []
    in_ac_section = False

    for line in story_content.splitlines():
        stripped = line.strip()
        # Detect AC section header
        if stripped.startswith("#") and "acceptance criteria" in stripped.lower():
            in_ac_section = True
            continue
        # Exit AC section on next heading
        if in_ac_section and stripped.startswith("#"):
            break
        if in_ac_section:
            # Match "- [ ] text" or "- [x] text"
            if stripped.startswith("- [") and "]" in stripped:
                bracket_end = stripped.index("]")
                criterion = stripped[bracket_end + 1 :].strip()
                if criterion:
                    criteria.append(criterion)

    return criteria


# Backward-compat alias
extract_spec_criteria = extract_story_criteria
