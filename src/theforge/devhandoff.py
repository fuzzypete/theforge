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
    gate_result: str | None  # optional agent value; coordinator state is authoritative
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


def _normalize_story_text(text: str) -> str:
    """Normalize story and handoff text for content-consistency checks."""
    return " ".join(text.casefold().split())


def _extract_story_acceptance_criteria(story_content: str) -> list[str]:
    """Extract acceptance-criteria bullet text from a story document.

    Handles multi-line markdown bullets where continuation lines are indented
    under the ``- `` prefix.  Continuation text is joined with a single space
    so that normalised comparison works regardless of line-wrapping.
    """
    lines = story_content.splitlines()
    in_acceptance_section = False
    criteria: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().casefold()
            if heading == "acceptance criteria":
                in_acceptance_section = True
                continue
            if in_acceptance_section:
                break
            continue

        if not in_acceptance_section:
            continue

        if stripped.startswith(("- ", "* ")):
            criteria.append(stripped[2:].strip())
        elif criteria:
            # Continuation line for the current bullet — append to last criterion
            criteria[-1] = criteria[-1] + " " + stripped

    return criteria


def _extract_story_identity_tokens(story_content: str) -> set[str]:
    """Extract normalized story identifiers that a repaired handoff should reflect."""
    tokens: set[str] = set()

    for line in story_content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading and heading.casefold() != "acceptance criteria":
                tokens.add(_normalize_story_text(heading))
            continue

        if stripped.startswith(">"):
            quoted = stripped.lstrip(">").strip()
            if quoted:
                tokens.add(_normalize_story_text(quoted))

    return {token for token in tokens if token}


def check_handoff_story_consistency(handoff: DevHandoff, story_content: str) -> list[str]:
    """Return content-consistency errors for a schema-valid handoff.

    Reject handoffs whose reported acceptance criteria do not align with the
    active story. A repaired handoff must describe the active story's
    acceptance criteria, not merely contain text that appears somewhere in the
    broader spec. Matching is whitespace- and case-insensitive so repaired
    handoffs are judged on content rather than formatting differences.

    If the story does not declare an explicit acceptance-criteria section, skip
    content validation rather than rejecting otherwise-valid legacy stories.
    """
    if not handoff.acceptance_criteria:
        return []

    extracted_story_criteria = _extract_story_acceptance_criteria(story_content)
    if not extracted_story_criteria:
        return []

    story_criteria = {
        _normalize_story_text(criterion)
        for criterion in extracted_story_criteria
        if criterion.strip()
    }

    missing_criteria: list[str] = []
    for ac in handoff.acceptance_criteria:
        criterion = ac.get("criterion", "").strip()
        normalized_criterion = _normalize_story_text(criterion)
        if criterion and normalized_criterion not in story_criteria:
            missing_criteria.append(criterion)

    if missing_criteria:
        formatted_missing = "; ".join(f"'{criterion}'" for criterion in missing_criteria)
        return [
            "handoff acceptance_criteria appear inconsistent with the active story: "
            f"criterion text not found in story acceptance criteria: {formatted_missing}"
        ]

    return []


def dev_handoff_to_reviewer_text(handoff: DevHandoff) -> str:
    """Format a validated DevHandoff as structured markdown for the reviewer.

    Sections with no content are omitted (spec says: "When dev handoff is
    absent or empty after retries, reviewer prompt is unchanged").
    """
    parts: list[str] = []

    if handoff.summary:
        parts.append(f"**Summary:** {handoff.summary}")

    if handoff.commits:
        parts.append("**Commits:**")
        for c in handoff.commits:
            parts.append(f"- `{c['sha']}` {c['message']}")

    if handoff.acceptance_criteria:
        parts.append("**Acceptance Criteria:**")
        for ac in handoff.acceptance_criteria:
            parts.append(f"- {ac['criterion']} — {ac['status']}: {ac['notes']}")

    if handoff.story_deviations:
        parts.append("**Story Deviations:**")
        for d in handoff.story_deviations:
            parts.append(f"- {d['description']} — {d['justification']}")

    if handoff.deferred_items:
        parts.append("**Deferred Items:**")
        for d in handoff.deferred_items:
            parts.append(f"- {d['description']} — {d['reason']}")

    if handoff.gate_result:
        parts.append(f"**Gate Result:** {handoff.gate_result}")

    if not parts:
        return ""

    return "\n\n".join(parts)
