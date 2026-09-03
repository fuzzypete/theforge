"""Structured output parsing for semantic evaluation."""

from __future__ import annotations

import json

import yaml

from theforge.eval.semantic_types import (
    OUTCOME_FINDINGS,
    OUTCOME_NO_FINDINGS,
    SEVERITY_VALUES,
    SemanticFinding,
    SemanticParsedOutcome,
)

_TOP_LEVEL_KEYS = frozenset({"outcome", "findings"})
_FINDING_KEYS = frozenset({"summary", "rationale", "evidence", "severity"})


class SemanticOutputParseError(ValueError):
    """Raised when a semantic-evaluation response does not satisfy the contract."""


def _extract_structured_block(output: str) -> str:
    text = output.strip()
    if not text:
        raise SemanticOutputParseError("empty output")
    if not text.startswith("```"):
        return text
    newline = text.find("\n")
    if newline == -1:
        raise SemanticOutputParseError("unterminated fenced block")
    closing = text.rfind("```")
    if closing <= newline:
        raise SemanticOutputParseError("unterminated fenced block")
    inner = text[newline + 1 : closing].strip()
    if not inner:
        raise SemanticOutputParseError("empty fenced block")
    return inner


def _load_mapping(text: str) -> dict[str, object]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SemanticOutputParseError(f"not valid JSON or YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SemanticOutputParseError("structured output must be a mapping")
    unknown = set(parsed) - _TOP_LEVEL_KEYS
    if unknown:
        raise SemanticOutputParseError(f"structured output has unknown keys: {sorted(unknown)}")
    return parsed


def _parse_finding(entry: object) -> SemanticFinding:
    if not isinstance(entry, dict):
        raise SemanticOutputParseError("each finding must be a mapping")
    unknown = set(entry) - _FINDING_KEYS
    if unknown:
        raise SemanticOutputParseError(f"finding has unknown keys: {sorted(unknown)}")
    summary = entry.get("summary")
    rationale = entry.get("rationale")
    if not isinstance(summary, str) or not summary.strip():
        raise SemanticOutputParseError("finding.summary must be a non-empty string")
    if not isinstance(rationale, str) or not rationale.strip():
        raise SemanticOutputParseError("finding.rationale must be a non-empty string")
    evidence_raw = entry.get("evidence")
    evidence = None
    if evidence_raw not in (None, ""):
        if not isinstance(evidence_raw, str):
            raise SemanticOutputParseError("finding.evidence must be a string when present")
        evidence = evidence_raw.strip() or None
    severity_raw = entry.get("severity")
    severity = None
    if severity_raw not in (None, ""):
        if not isinstance(severity_raw, str):
            raise SemanticOutputParseError("finding.severity must be a string when present")
        severity = severity_raw.strip().lower()
        if severity not in SEVERITY_VALUES:
            raise SemanticOutputParseError(
                f"finding.severity must be one of {sorted(SEVERITY_VALUES)}"
            )
    return SemanticFinding(
        summary=summary.strip(),
        rationale=rationale.strip(),
        evidence=evidence,
        severity=severity,
    )


def parse_semantic_review_output(output: str) -> SemanticParsedOutcome:
    parsed = _load_mapping(_extract_structured_block(output))
    outcome_raw = parsed.get("outcome")
    if not isinstance(outcome_raw, str):
        raise SemanticOutputParseError("outcome must be a string")
    outcome = outcome_raw.strip().upper()
    findings_raw = parsed.get("findings")

    if outcome == OUTCOME_NO_FINDINGS:
        if findings_raw not in (None, [], ()):
            raise SemanticOutputParseError("NO_FINDINGS output must not carry findings")
        return SemanticParsedOutcome(outcome=OUTCOME_NO_FINDINGS, findings=())

    if outcome != OUTCOME_FINDINGS:
        raise SemanticOutputParseError(
            f"outcome must be {OUTCOME_FINDINGS!r} or {OUTCOME_NO_FINDINGS!r}"
        )
    if not isinstance(findings_raw, list) or not findings_raw:
        raise SemanticOutputParseError("FINDINGS output must include a non-empty findings list")
    findings = tuple(_parse_finding(item) for item in findings_raw)
    return SemanticParsedOutcome(outcome=OUTCOME_FINDINGS, findings=findings)
