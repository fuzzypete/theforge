"""Prompt + parser helpers for intake agent remediation.

This module intentionally owns only the textual contract for intake rewrite
agents: how we ask for a remediation and how we interpret the response. The
runner remains responsible for transport/config wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from .findings import IntakeFinding

_NO_REMEDIATION_PREFIX = "NO_REMEDIATION:"


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


def build_agent_rewrite_prompt(body: str, findings: list[IntakeFinding]) -> str:
    """Build the one-shot intake remediation prompt."""
    lines = [
        "You are remediating a GitHub issue body for TheForge sprint intake.",
        "Rewrite the issue body so it resolves the blocking findings below.",
        "Preserve the issue's intent. Do not add implementation steps, file paths, or test plans.",
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
    lines.extend(
        [
            "",
            "Current issue body:",
            "```markdown",
            body,
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
