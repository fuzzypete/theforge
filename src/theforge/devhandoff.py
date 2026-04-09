"""Dev handoff: parse and validate structured dev→reviewer handoff.

The dev agent writes structured YAML into the dev_notes field of the handoff
file. This module extracts, validates, and converts that data into structured
content the reviewer can act on, mirroring review.py for review→dev handoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from .schemas import validate_dev_handoff


@dataclass(frozen=True)
class DevHandoff:
    """Parsed and validated dev handoff output."""

    summary: str
    commits: list[dict[str, str]]  # [{sha, message}]
    acceptance_criteria: list[dict[str, str]]  # [{criterion, status, notes}]
    story_deviations: list[dict[str, str]]  # [{description, justification}]
    deferred_items: list[dict[str, str]]  # [{description, reason}]
    gate_result: str | None  # coordinator gate decision when available
    parse_errors: list[str]  # non-empty if parsing/validation failed
    raw: dict  # the parsed YAML data


def parse_dev_handoff(dev_notes: str) -> DevHandoff:
    """Parse dev_notes string into a validated DevHandoff.

    Strategy (mirrors parse_review_output):
    1. Look for ```yaml ... ``` fenced block
    2. Fall back to parsing entire string as YAML
    3. If all parsing fails, return DevHandoff with parse errors
    """
    _empty = DevHandoff(
        summary="",
        commits=[],
        acceptance_criteria=[],
        story_deviations=[],
        deferred_items=[],
        gate_result=None,
        parse_errors=[],
        raw={},
    )

    # Try to extract YAML from markdown code fences
    yaml_match = re.search(
        r"```ya?ml\s*\n(.*?)```",
        dev_notes,
        flags=re.DOTALL,
    )
    yaml_text = yaml_match.group(1) if yaml_match else dev_notes

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return DevHandoff(**{**_empty.__dict__, "parse_errors": [f"YAML parse error: {e}"]})

    if not isinstance(data, dict):
        return DevHandoff(
            **{**_empty.__dict__, "parse_errors": ["dev handoff must be a YAML mapping"]}
        )

    schema_errors = validate_dev_handoff(data)

    # Extract commits
    raw_commits = data.get("commits")
    commits: list[dict[str, str]] = []
    if isinstance(raw_commits, list):
        for c in raw_commits:
            if isinstance(c, dict):
                commits.append(
                    {
                        "sha": str(c.get("sha", "")),
                        "message": str(c.get("message", "")),
                    }
                )

    # Extract acceptance_criteria
    raw_criteria = data.get("acceptance_criteria")
    acceptance_criteria: list[dict[str, str]] = []
    if isinstance(raw_criteria, list):
        for ac in raw_criteria:
            if isinstance(ac, dict):
                acceptance_criteria.append(
                    {
                        "criterion": str(ac.get("criterion", "")),
                        "status": str(ac.get("status", "")),
                        "notes": str(ac.get("notes", "")),
                    }
                )

    # Extract story_deviations (accept spec_deviations for backward compat)
    raw_deviations = (
        data.get("story_deviations") if "story_deviations" in data else data.get("spec_deviations")
    )
    story_deviations: list[dict[str, str]] = []
    if isinstance(raw_deviations, list):
        for d in raw_deviations:
            if isinstance(d, dict):
                story_deviations.append(
                    {
                        "description": d.get("description", ""),
                        "justification": d.get("justification", ""),
                    }
                )

    # Extract deferred_items
    raw_deferred = data.get("deferred_items")
    deferred_items: list[dict[str, str]] = []
    if isinstance(raw_deferred, list):
        for d in raw_deferred:
            if isinstance(d, dict):
                deferred_items.append(
                    {
                        "description": d.get("description", ""),
                        "reason": d.get("reason", ""),
                    }
                )

    return DevHandoff(
        summary=data.get("summary", ""),
        commits=commits,
        acceptance_criteria=acceptance_criteria,
        story_deviations=story_deviations,
        deferred_items=deferred_items,
        gate_result=(str(data["gate_result"]) if "gate_result" in data else None),
        parse_errors=schema_errors,
        raw=data,
    )


def dev_handoff_to_reviewer_text(handoff: DevHandoff) -> str:
    """Format a validated DevHandoff as structured markdown for the reviewer.

    Sections with no content are omitted (spec says: "When dev handoff is
    absent or empty after retries, reviewer prompt is unchanged").
    """
    parts: list[str] = []

    if handoff.summary:
        parts.append(f"**Summary:** {handoff.summary}")

    if handoff.commits:
        lines = ["**Commits:**"]
        for c in handoff.commits:
            lines.append(f"- `{c['sha']}` {c['message']}")
        parts.append("\n".join(lines))

    if handoff.acceptance_criteria:
        lines = ["**Acceptance Criteria:**"]
        for ac in handoff.acceptance_criteria:
            lines.append(f"- **[{ac['status']}]** {ac['criterion']}")
            if ac["notes"]:
                lines.append(f"  {ac['notes']}")
        parts.append("\n".join(lines))

    if handoff.story_deviations:
        lines = ["**Story Deviations:**"]
        for d in handoff.story_deviations:
            lines.append(f"- {d['description']}")
            lines.append(f"  *Justification:* {d['justification']}")
        parts.append("\n".join(lines))

    if handoff.deferred_items:
        lines = ["**Deferred Items:**"]
        for d in handoff.deferred_items:
            lines.append(f"- {d['description']}")
            lines.append(f"  *Reason:* {d['reason']}")
        parts.append("\n".join(lines))

    if handoff.gate_result:
        parts.append(f"**Gate Result:** {handoff.gate_result}")

    if not parts:
        return ""

    return "\n\n".join(parts)
