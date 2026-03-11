"""Review protocol: parse review agent output, extract structured findings.

The review agent is instructed to output only a YAML block.
This module extracts, validates, and converts review data into
structures the coordinator can act on mechanically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from .schemas import validate_review_yaml


@dataclass(frozen=True)
class ReviewFinding:
    """A single review finding with severity and location."""

    severity: str  # "P1" or "P2"
    file: str
    line: int | None
    description: str
    suggestion: str | None


@dataclass(frozen=True)
class ReviewResult:
    """Parsed and validated review output."""

    verdict: str  # "APPROVE" or "REQUEST_CHANGES"
    summary: str
    findings: list[ReviewFinding]
    spec_matches: bool
    spec_mismatches: list[str]
    test_adequate: bool
    test_gaps: list[str]
    parse_errors: list[str]  # non-empty if parsing/validation failed
    raw_yaml: dict  # the parsed YAML data


def parse_review_output(agent_output: str) -> ReviewResult:
    """Extract and parse review YAML from agent output.

    Strategy:
    1. Look for ```yaml ... ``` fenced block
    2. Fall back to parsing entire output as YAML
    3. If all parsing fails, return REQUEST_CHANGES with parse errors
    """
    # Try to extract YAML from markdown code fences
    yaml_match = re.search(
        r"```ya?ml\s*\n(.*?)```",
        agent_output,
        flags=re.DOTALL,
    )

    yaml_text = yaml_match.group(1) if yaml_match else agent_output

    # Parse YAML
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="PARSE ERROR: Could not parse review output as YAML",
            findings=[],
            spec_matches=False,
            spec_mismatches=[],
            test_adequate=False,
            test_gaps=[],
            parse_errors=[f"YAML parse error: {e}"],
            raw_yaml={},
        )

    if not isinstance(data, dict):
        return ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="PARSE ERROR: Review output root is not a YAML mapping",
            findings=[],
            spec_matches=False,
            spec_mismatches=[],
            test_adequate=False,
            test_gaps=[],
            parse_errors=["Root element is not a mapping"],
            raw_yaml={},
        )

    # Validate schema
    schema_errors = validate_review_yaml(data)

    # Extract findings
    findings: list[ReviewFinding] = []
    for f in data.get("findings", []):
        if isinstance(f, dict):
            findings.append(
                ReviewFinding(
                    severity=f.get("severity", "P2"),
                    file=f.get("file", "unknown"),
                    line=f.get("line"),
                    description=f.get("description", ""),
                    suggestion=f.get("suggestion"),
                )
            )

    # Extract spec compliance
    spec = data.get("spec_compliance", {})
    spec_matches = spec.get("matches_spec", False) if isinstance(spec, dict) else False
    spec_mismatches = spec.get("mismatches", []) if isinstance(spec, dict) else []

    # Extract test coverage
    tests = data.get("test_coverage", {})
    test_adequate = tests.get("adequate", False) if isinstance(tests, dict) else False
    test_gaps = tests.get("gaps", []) if isinstance(tests, dict) else []

    return ReviewResult(
        verdict=data.get("verdict", "REQUEST_CHANGES"),
        summary=data.get("summary", "(no summary)"),
        findings=findings,
        spec_matches=spec_matches,
        spec_mismatches=spec_mismatches if isinstance(spec_mismatches, list) else [],
        test_adequate=test_adequate,
        test_gaps=test_gaps if isinstance(test_gaps, list) else [],
        parse_errors=schema_errors,
        raw_yaml=data,
    )


def findings_to_markdown(findings: list[ReviewFinding]) -> str:
    """Convert review findings to markdown for injection into dev agent prompt.

    This is the mechanical bridge between the review agent's output
    and the dev agent's next iteration input.
    """
    if not findings:
        return "No findings."

    lines: list[str] = []
    for f in findings:
        line_ref = f" (line {f.line})" if f.line else ""
        lines.append(f"### [{f.severity}] `{f.file}`{line_ref}")
        lines.append(f"**Issue:** {f.description}")
        if f.suggestion:
            lines.append(f"**Fix:** {f.suggestion}")
        lines.append("")

    return "\n".join(lines)
