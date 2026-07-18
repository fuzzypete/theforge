"""Prompt + parser helpers for intake agent remediation.

This module intentionally owns only the textual contract for intake rewrite
agents: how we ask for a remediation and how we interpret the response. The
runner remains responsible for transport/config wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..shape_check.diagnosis_spec import render_diagnosis_requirements_block
from .findings import IntakeFinding

_NO_REMEDIATION_PREFIX = "NO_REMEDIATION:"

# Finding codes whose remediation requires a complete bug Diagnosis section.
# When present, the prompt embeds the diagnosis spec's labels + examples so the
# agent aims at exactly the target the validator checks (#1629).
_DIAGNOSIS_FINDING_CODES: frozenset[str] = frozenset({"needs_diagnosis"})


@dataclass(frozen=True)
class AgentRewriteResult:
    """Structured result from an intake remediation agent call."""

    replacement: str | None
    detail: str = ""
    attempted: bool = False
    profile_name: str | None = None
    model_used: str | None = None
    cost_usd: float | None = None
    transport_used: str | None = None


def build_agent_rewrite_prompt(
    body: str,
    findings: list[IntakeFinding],
    comments: list[str] | None = None,
) -> str:
    """Build the one-shot intake remediation prompt.

    ``comments`` is the issue's existing comment thread in chronological
    order. When the body lacks a structural section (e.g. ``## Diagnosis``)
    that is present in raw form in a comment, the agent should synthesize
    the section into the replacement body from that comment material.
    """
    lines = [
        "You are remediating a GitHub issue body for TheForge sprint intake.",
        "Rewrite the issue body so it resolves the blocking findings below.",
        "Preserve the issue's intent. Do not add implementation steps, file paths, or test plans.",
        "If a required structural section is missing from the body but the material exists",
        "in the comment thread below, synthesize that section into the replacement body.",
        "",
        "Output rules:",
        "1. If you can fix the issue, return only the full replacement Markdown body.",
        "2. If you cannot produce a compliant replacement, return exactly one line starting with",
        f"   {_NO_REMEDIATION_PREFIX}",
        "3. Do not include explanations, bullets outside the issue body, or surrounding prose.",
        "",
        "Blocking findings:",
    ]
    for finding in findings:
        lines.append(f"- `{finding.code}` at `{finding.location}`: {finding.problem}")
    if any(finding.code in _DIAGNOSIS_FINDING_CODES for finding in findings):
        lines.extend(
            [
                "",
                "Diagnosis section requirements:",
                render_diagnosis_requirements_block(),
            ]
        )
    lines.extend(
        [
            "",
            "Current issue body:",
            "```markdown",
            body,
            "```",
        ]
    )
    if comments:
        lines.extend(["", "Existing issue comments (chronological):"])
        for idx, comment in enumerate(comments, start=1):
            lines.extend(
                [
                    "",
                    f"Comment {idx}:",
                    "```markdown",
                    comment,
                    "```",
                ]
            )
    return "\n".join(lines)


def parse_agent_rewrite_output(output: str) -> AgentRewriteResult:
    """Parse the intake rewrite agent output into a structured result."""
    text = output.strip()
    if not text:
        return AgentRewriteResult(
            replacement=None,
            detail="agent returned empty output",
            attempted=True,
        )

    if text.startswith(_NO_REMEDIATION_PREFIX):
        reason = text[len(_NO_REMEDIATION_PREFIX) :].strip() or "agent declined remediation"
        return AgentRewriteResult(replacement=None, detail=reason, attempted=True)

    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()

    text = text.strip()
    if not text:
        return AgentRewriteResult(
            replacement=None,
            detail="agent returned an empty fenced body",
            attempted=True,
        )
    return AgentRewriteResult(
        replacement=text,
        detail="agent produced replacement",
        attempted=True,
    )
