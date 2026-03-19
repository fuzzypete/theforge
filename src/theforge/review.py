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


def _sanitize_yaml_text(yaml_text: str) -> str:
    """Sanitize reviewer output text to survive YAML parsing.

    Fixes:
    1. Backslash-escaped quotes (\\") → apostrophe. Reviewers sometimes write
       descriptions ending with r\\" thinking it closes the string.
    2. Inline backtick code (`` `foo.bar` ``) → plain text. Backtick-wrapped
       code containing colons (e.g., `candidate.content.parts`) causes YAML
       to interpret the colon as a mapping separator.
    """
    text = yaml_text.replace('\\"', "'")
    # Strip inline backticks — they're markdown formatting, not meaningful in YAML.
    # This prevents `foo: bar` from being parsed as a YAML mapping key.
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return text


def parse_review_json(data: dict) -> ReviewResult:
    """Parse and validate review JSON from API response.

    This path is used for API-based reviewers that return structured JSON.
    The cross-validation rules are the same as the YAML path.
    """
    # Validate schema (re-using the YAML validator which works on dicts)
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


def parse_review_output(agent_output: str) -> ReviewResult:
    """Extract and parse review YAML from agent output.

    Strategy:
    1. Look for ```yaml ... ``` fenced block
    2. Fall back to parsing entire output as YAML
    3. If all parsing fails, return REQUEST_CHANGES with parse errors
    """
    # Try to extract YAML from markdown code fences
    yaml_match = re.search(
        r"```ya?ml\s*(.*?)```",
        agent_output,
        flags=re.DOTALL,
    )

    yaml_text = yaml_match.group(1) if yaml_match else agent_output
    yaml_text = _sanitize_yaml_text(yaml_text)

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


@dataclass(frozen=True)
class PlanReviewFinding:
    """A single finding from plan review."""

    severity: str  # "P1"
    description: str
    suggestion: str | None


@dataclass(frozen=True)
class PlanReviewResult:
    """Parsed plan review verdict."""

    verdict: str  # "APPROVE" or "REJECT"
    findings: list[PlanReviewFinding]
    parse_errors: list[str]


def parse_plan_review_output(agent_output: str) -> PlanReviewResult:
    """Extract and parse plan review YAML from agent output.

    Reuses the same YAML extraction strategy as code review parsing.
    REJECT without findings is treated as a parse error.
    Unparseable output is treated as REJECT.
    """
    yaml_match = re.search(
        r"```ya?ml\s*(.*?)```",
        agent_output,
        flags=re.DOTALL,
    )
    yaml_text = yaml_match.group(1) if yaml_match else agent_output
    yaml_text = _sanitize_yaml_text(yaml_text)

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return PlanReviewResult(
            verdict="REJECT",
            findings=[],
            parse_errors=[f"YAML parse error: {e}"],
        )

    if not isinstance(data, dict):
        return PlanReviewResult(
            verdict="REJECT",
            findings=[],
            parse_errors=["Plan review output root is not a YAML mapping"],
        )

    verdict = data.get("verdict", "").upper()
    if verdict not in ("APPROVE", "REJECT"):
        return PlanReviewResult(
            verdict="REJECT",
            findings=[],
            parse_errors=[f"verdict must be APPROVE or REJECT, got: {verdict!r}"],
        )

    errors: list[str] = []

    # Validate findings structure
    raw_findings = data.get("findings")
    if raw_findings is not None and not isinstance(raw_findings, list):
        errors.append(f"findings must be a list, got: {type(raw_findings).__name__}")
        raw_findings = []

    findings: list[PlanReviewFinding] = []
    blocking_count = 0
    for i, f in enumerate(raw_findings or []):
        if not isinstance(f, dict):
            errors.append(f"findings[{i}] must be a mapping")
            continue
        severity = f.get("severity", "P1")
        if severity == "P0":
            blocking_count += 1
        desc = f.get("description", "")
        if not desc:
            errors.append(f"findings[{i}].description must be non-empty")
        findings.append(
            PlanReviewFinding(
                severity=severity,
                description=desc,
                suggestion=f.get("suggestion"),
            )
        )

    # Cross-validation: same principle as code review schema
    if verdict == "REJECT" and not findings:
        errors.append("REJECT verdict without findings — cannot justify rejection")
    if verdict == "APPROVE" and blocking_count > 0:
        errors.append(
            f"verdict is APPROVE but {blocking_count} P0 finding(s) exist — "
            "cannot approve with blocking findings"
        )

    # Any parse error on APPROVE → demote to REJECT
    if verdict == "APPROVE" and errors:
        verdict = "REJECT"

    return PlanReviewResult(
        verdict=verdict,
        findings=findings,
        parse_errors=errors,
    )


def plan_review_findings_to_text(result: PlanReviewResult) -> str:
    """Convert plan review findings to text for feeding back into plan regeneration."""
    if not result.findings:
        return "No specific findings provided."
    lines: list[str] = []
    for f in result.findings:
        lines.append(f"- [{f.severity}] {f.description}")
        if f.suggestion:
            lines.append(f"  Suggestion: {f.suggestion}")
    return "\n".join(lines)


def review_to_dev_handoff(result: ReviewResult) -> str:
    """Convert a ReviewResult to a rich action-oriented markdown block for the dev agent.

    Includes summary, spec compliance issues (if any), test gaps (if any),
    and findings. Sections with no content are omitted.
    """
    parts: list[str] = []

    parts.append(f"## Review Summary\n{result.summary}")

    if not result.spec_matches and result.spec_mismatches:
        bullets = "\n".join(f"- {m}" for m in result.spec_mismatches)
        parts.append(f"## Spec Compliance Issues\n{bullets}")

    if not result.test_adequate and result.test_gaps:
        bullets = "\n".join(f"- {g}" for g in result.test_gaps)
        parts.append(f"## Missing Test Coverage\n{bullets}")

    if not result.findings:
        parts.append("## Findings\n\nNo findings.")
    else:
        finding_lines: list[str] = ["## Findings"]
        for f in result.findings:
            line_ref = f" (line {f.line})" if f.line is not None else ""
            finding_lines.append(f"\n### [{f.severity}] `{f.file}`{line_ref}")
            finding_lines.append(f"**Issue:** {f.description}")
            if f.suggestion:
                finding_lines.append(f"**Fix:** {f.suggestion}")
        parts.append("\n".join(finding_lines))

    return "\n\n".join(parts)


def merge_review_results(results: list[ReviewResult], names: list[str]) -> ReviewResult:
    """Merge multiple ReviewResults into one without an LLM call.

    - Verdict: REQUEST_CHANGES if any reviewer says so, else APPROVE
    - Summary: one line per reviewer labelled by name
    - Findings: union of all findings (preserves duplicates — dev sees full picture)
    - spec_matches: False if any reviewer says False
    - test_adequate: False if any reviewer says False
    """
    verdict = (
        "REQUEST_CHANGES" if any(r.verdict == "REQUEST_CHANGES" for r in results) else "APPROVE"
    )
    summary_parts = [f"[{name}] {r.summary}" for name, r in zip(names, results)]
    summary = " | ".join(summary_parts)

    all_findings: list[ReviewFinding] = []
    for r in results:
        all_findings.extend(r.findings)

    spec_matches = all(r.spec_matches for r in results)
    spec_mismatches: list[str] = []
    for name, r in zip(names, results):
        for m in r.spec_mismatches:
            spec_mismatches.append(f"[{name}] {m}")

    test_adequate = all(r.test_adequate for r in results)
    test_gaps: list[str] = []
    for name, r in zip(names, results):
        for g in r.test_gaps:
            test_gaps.append(f"[{name}] {g}")

    # Propagate parse errors from any reviewer so the parse-retry loop in
    # _run_review_phase() can fire instead of treating malformed output as a
    # real REQUEST_CHANGES verdict.
    all_parse_errors: list[str] = []
    for name, r in zip(names, results):
        for e in r.parse_errors:
            all_parse_errors.append(f"[{name}] {e}")

    return ReviewResult(
        verdict=verdict,
        summary=summary,
        findings=all_findings,
        spec_matches=spec_matches,
        spec_mismatches=spec_mismatches,
        test_adequate=test_adequate,
        test_gaps=test_gaps,
        parse_errors=all_parse_errors,
        raw_yaml={},
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
