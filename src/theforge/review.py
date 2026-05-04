"""Review protocol: parse review agent output, extract structured findings.

The review agent is instructed to output only a YAML block.
This module extracts, validates, and converts review data into
structures the coordinator can act on mechanically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from .schemas import repair_review_yaml, validate_review_yaml


def _coerce_line(value: object) -> int | None:
    """Coerce a reviewer-supplied line value to int or None.

    Reviewers sometimes emit line numbers as strings (e.g. "42") or omit them
    entirely.  Any value that cannot be interpreted as a non-negative integer is
    treated as None so downstream arithmetic never crashes.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ACVerification:
    """Per-AC verification entry: did the reviewer verify this criterion?

    For story-type issues, ``criterion`` is the AC text from the spec.
    For bug-type issues, ``criterion`` is the symptom resolution check
    (e.g. "Symptom resolution: Codex 'usage limit' fallback").

    ``evidence`` must point to the diff hunks implementing the criterion AND
    test(s) covering the specific failure mode the issue describes — not a
    generic test that could plausibly cover the category.
    """

    criterion: str
    status: str  # VERIFIED | PARTIAL | NOT_VERIFIED
    evidence: str


@dataclass(frozen=True)
class ReviewFinding:
    """A single review finding with severity, location, and structured prose.

    Findings carry three required prose fields so the post_run hook can render
    a shape-gate-clean bug body without manual grooming:

    * ``observed`` — one sentence, behaviour-only.
    * ``expected`` — a category-level rule in flowing prose that generalises
      beyond the trigger.
    * ``evidence`` — file/line/anchor pointing at the offending code.

    ``suggestion`` is optional and rendered separately as non-binding guidance
    so it never counts as HOW-leakage when the finding becomes a bug story.
    """

    severity: str  # "P1" or "P2"
    file: str
    line: int | None
    observed: str
    suggestion: str | None
    expected: str = ""
    evidence: str = ""
    reviewers: tuple[str, ...] = ()  # reviewers who raised this finding (audit attribution)

    @property
    def description(self) -> str:
        """Backwards-compatible alias for ``observed``.

        Older internal callers (audit serialisation, classifier tokenisation,
        fix prompts) read ``description`` to mean "the issue text"; observed is
        the natural successor. Construction sites must use the structured
        fields directly.
        """
        return self.observed


@dataclass(frozen=True)
class ReviewResult:
    """Parsed and validated review output."""

    verdict: str  # "APPROVE" or "REQUEST_CHANGES"
    summary: str
    findings: list[ReviewFinding]
    story_matches: bool
    story_mismatches: list[str]
    test_adequate: bool
    test_gaps: list[str]
    parse_errors: list[str]  # non-empty if parsing/validation failed
    raw_yaml: dict  # the parsed YAML data
    ac_verification: tuple[ACVerification, ...] = ()  # per-AC verification table
    sanitization_audit: dict[str, dict[str, int]] = field(default_factory=dict)


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


def sanitize_llm_text(text: object, *, field_name: str) -> tuple[str, dict[str, dict[str, int]]]:
    """Strip unsafe control characters from LLM-produced text fields.

    Removes all ASCII control characters except tab/newline/carriage return.
    This prevents subprocess argument construction from failing on embedded NUL
    bytes while keeping normal markdown formatting intact.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)

    removed = 0
    parts: list[str] = []
    for char in text:
        codepoint = ord(char)
        if (0 <= codepoint < 32 and char not in "\t\n\r") or codepoint == 127:
            removed += 1
            continue
        parts.append(char)

    sanitized = "".join(parts)
    if removed == 0:
        return sanitized, {}
    return sanitized, {field_name: {"sanitized_chars": removed}}


def _merge_sanitization_audits(*audits: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Merge field-name keyed sanitization audits by summing replacement counts."""
    merged: dict[str, dict[str, int]] = {}
    for audit in audits:
        for field_name, detail in audit.items():
            entry = merged.setdefault(field_name, {"sanitized_chars": 0})
            entry["sanitized_chars"] += int(detail.get("sanitized_chars", 0))
    return merged


def _extract_review_payload(
    data: dict,
) -> tuple[str, list[ReviewFinding], dict[str, dict[str, int]]]:
    """Extract sanitized summary/findings payload from parsed review data."""
    summary, summary_audit = sanitize_llm_text(
        data.get("summary", "(no summary)"),
        field_name="summary",
    )

    findings: list[ReviewFinding] = []
    sanitization_audit = dict(summary_audit)
    for index, finding_data in enumerate(data.get("findings", [])):
        if not isinstance(finding_data, dict):
            continue
        observed, observed_audit = sanitize_llm_text(
            finding_data.get("observed", ""),
            field_name=f"findings[{index}].observed",
        )
        expected, expected_audit = sanitize_llm_text(
            finding_data.get("expected", ""),
            field_name=f"findings[{index}].expected",
        )
        evidence, evidence_audit = sanitize_llm_text(
            finding_data.get("evidence", ""),
            field_name=f"findings[{index}].evidence",
        )
        findings.append(
            ReviewFinding(
                severity=finding_data.get("severity", "P2"),
                file=finding_data.get("file", "unknown"),
                line=_coerce_line(finding_data.get("line")),
                observed=observed,
                expected=expected,
                evidence=evidence,
                suggestion=finding_data.get("suggestion"),
            )
        )
        sanitization_audit = _merge_sanitization_audits(
            sanitization_audit, observed_audit, expected_audit, evidence_audit
        )
    return summary, findings, sanitization_audit


def _extract_ac_verification(data: dict) -> tuple[ACVerification, ...]:
    """Extract ac_verification entries from parsed review data."""
    raw = data.get("ac_verification")
    if not isinstance(raw, list):
        return ()
    out: list[ACVerification] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        criterion = entry.get("criterion")
        status = entry.get("status")
        evidence = entry.get("evidence")
        if not isinstance(criterion, str) or not isinstance(status, str):
            continue
        out.append(
            ACVerification(
                criterion=criterion,
                status=status,
                evidence=evidence if isinstance(evidence, str) else "",
            )
        )
    return tuple(out)


def parse_review_json(data: dict) -> ReviewResult:
    """Parse and validate review JSON from API response.

    This path is used for API-based reviewers that return structured JSON.
    The cross-validation rules are the same as the YAML path.
    """
    # Best-effort repair before strict validation
    repair_review_yaml(data)
    schema_errors = validate_review_yaml(data)

    # Extract findings
    summary, findings, sanitization_audit = _extract_review_payload(data)

    # Extract story compliance (accept both story_compliance and spec_compliance for compat)
    spec = data.get("story_compliance") or data.get("spec_compliance") or {}
    story_matches = spec.get("matches_spec", False) if isinstance(spec, dict) else False
    story_mismatches = spec.get("mismatches", []) if isinstance(spec, dict) else []

    # Extract test coverage
    tests = data.get("test_coverage", {})
    test_adequate = tests.get("adequate", False) if isinstance(tests, dict) else False
    test_gaps = tests.get("gaps", []) if isinstance(tests, dict) else []

    return ReviewResult(
        verdict=data.get("verdict", "REQUEST_CHANGES"),
        summary=summary,
        findings=findings,
        story_matches=story_matches,
        story_mismatches=story_mismatches if isinstance(story_mismatches, list) else [],
        test_adequate=test_adequate,
        test_gaps=test_gaps if isinstance(test_gaps, list) else [],
        parse_errors=schema_errors,
        raw_yaml=data,
        ac_verification=_extract_ac_verification(data),
        sanitization_audit=sanitization_audit,
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
            story_matches=False,
            story_mismatches=[],
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
            story_matches=False,
            story_mismatches=[],
            test_adequate=False,
            test_gaps=[],
            parse_errors=["Root element is not a mapping"],
            raw_yaml={},
        )

    # Best-effort repair before strict validation
    repair_review_yaml(data)
    schema_errors = validate_review_yaml(data)

    # Extract findings
    summary, findings, sanitization_audit = _extract_review_payload(data)

    # Extract story compliance (accept both story_compliance and spec_compliance for compat)
    spec = data.get("story_compliance") or data.get("spec_compliance") or {}
    story_matches = spec.get("matches_spec", False) if isinstance(spec, dict) else False
    story_mismatches = spec.get("mismatches", []) if isinstance(spec, dict) else []

    # Extract test coverage
    tests = data.get("test_coverage", {})
    test_adequate = tests.get("adequate", False) if isinstance(tests, dict) else False
    test_gaps = tests.get("gaps", []) if isinstance(tests, dict) else []

    return ReviewResult(
        verdict=data.get("verdict", "REQUEST_CHANGES"),
        summary=summary,
        findings=findings,
        story_matches=story_matches,
        story_mismatches=story_mismatches if isinstance(story_mismatches, list) else [],
        test_adequate=test_adequate,
        test_gaps=test_gaps if isinstance(test_gaps, list) else [],
        parse_errors=schema_errors,
        raw_yaml=data,
        ac_verification=_extract_ac_verification(data),
        sanitization_audit=sanitization_audit,
    )


def _try_parse_review(output: str, structured_data: dict | None = None) -> ReviewResult | None:
    """Parse review output; return None if any parse errors occurred."""
    if structured_data:
        result = parse_review_json(structured_data)
    else:
        result = parse_review_output(output)
    return None if result.parse_errors else result


def _best_individual_result(results: list[ReviewResult]) -> ReviewResult | None:
    """Return the best individual ReviewResult from a list.

    Priority: first result with P1 findings (→ REQUEST_CHANGES), then first
    APPROVE, then first overall.  Returns None if the list is empty.
    """
    if not results:
        return None
    for r in results:
        if any(f.severity == "P1" for f in r.findings):
            return r
    for r in results:
        if r.verdict == "APPROVE":
            return r
    return results[0]


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
    if verdict == "REQUEST_CHANGES":
        verdict = "REJECT"
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
        if severity in ("P0", "P1"):
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

    # Cross-validation
    if verdict == "REJECT" and not findings:
        errors.append("REJECT verdict without findings — cannot justify rejection")

    # Any parse error on APPROVE → demote to REJECT
    if verdict == "APPROVE" and errors:
        verdict = "REJECT"

    return PlanReviewResult(
        verdict=verdict,
        findings=findings,
        parse_errors=errors,
    )


def merge_plan_review_results(
    results: list[PlanReviewResult],
    names: list[str],
    prior_registry: list | None = None,
    current_attempt: int = 0,
) -> tuple[PlanReviewResult, list[CorroborationDowngrade]]:
    """Merge multiple PlanReviewResults into one without an LLM call.

    Rules:
    - Reviewers with parse_errors are excluded (caller should log a warning).
    - If no valid reviewers remain → REJECT with combined parse errors.
    - Verdict is findings-driven, not reviewer-verdict-driven:
      - Any P0 finding → REJECT (plan is impossible to implement)
      - Any P1 finding → REJECT (real gap that must be addressed before dev)
      - Only P2s remain → APPROVE (pass P2s to dev as context)
    - All findings are prefixed with ``[name]`` for attribution.
    - When ``prior_registry`` is provided, single-reviewer first-occurrence P1s
      are downgraded to P1-impl (advisory) via corroboration check.

    Returns ``(merged_result, corroboration_downgrades)``.
    """
    import logging as _logging

    valid: list[tuple[str, PlanReviewResult]] = []
    parse_error_parts: list[str] = []
    for name, result in zip(names, results):
        if result.parse_errors:
            _logging.getLogger(__name__).warning(
                "PLAN_REVIEW: excluding %s due to parse errors: %s",
                name,
                "; ".join(result.parse_errors),
            )
            for e in result.parse_errors:
                parse_error_parts.append(f"[{name}] {e}")
        else:
            valid.append((name, result))

    if not valid:
        return PlanReviewResult(
            verdict="REJECT",
            findings=[],
            parse_errors=parse_error_parts
            or ["All plan reviewers failed or produced parse errors"],
        ), []

    all_findings: list[PlanReviewFinding] = []
    for name, r in valid:
        for f in r.findings:
            all_findings.append(
                PlanReviewFinding(
                    severity=f.severity,
                    description=f"[{name}] {f.description}",
                    suggestion=f.suggestion,
                )
            )

    # Apply corroboration: downgrade single-reviewer first-occurrence P1s.
    corroborated_findings, downgrades = apply_plan_corroboration(
        all_findings,
        prior_registry=prior_registry,
        current_attempt=current_attempt,
    )

    has_p0_or_p1 = any(f.severity in ("P0", "P1") for f in corroborated_findings)
    verdict = "REJECT" if has_p0_or_p1 else "APPROVE"

    return PlanReviewResult(
        verdict=verdict,
        findings=corroborated_findings,
        parse_errors=[],
    ), downgrades


# ── Plan review corroboration ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CorroborationDowngrade:
    """Audit record for a P1→P1-impl downgrade."""

    original_severity: str
    effective_severity: str
    description: str


def _extract_reviewer_name(description: str) -> str | None:
    """Extract reviewer name from ``[name] description`` prefix.

    Returns the name string, or None if no prefix found.
    """
    m = re.match(r"^\[([^\]]+)\]\s*", description)
    return m.group(1) if m else None


def apply_plan_corroboration(
    findings: list[PlanReviewFinding],
    prior_registry: list | None = None,
    current_attempt: int = 0,
) -> tuple[list[PlanReviewFinding], list[CorroborationDowngrade]]:
    """Classify P1 findings as corroborated or advisory.

    A P1 is corroborated (stays P1) if:
    - 2+ distinct reviewers raised anchor-overlapping findings, OR
    - the finding matches a prior registry entry from an earlier attempt
      (recurrence).

    Uncorroborated P1s are downgraded to P1-impl.
    P0s are never downgraded.

    Returns (rewritten_findings, downgrade_log).
    """
    from .plan_finding_classifier import extract_anchors, strip_reviewer_prefix

    if prior_registry is None:
        prior_registry = []

    # Identify P1 finding indices.
    p1_indices = [i for i, f in enumerate(findings) if f.severity == "P1"]
    if not p1_indices:
        return findings, []

    # Extract anchors and reviewer names for each P1 finding.
    p1_anchors: dict[int, frozenset] = {}
    p1_reviewers: dict[int, str | None] = {}
    for i in p1_indices:
        stripped = strip_reviewer_prefix(findings[i].description)
        p1_anchors[i] = extract_anchors(stripped)
        p1_reviewers[i] = _extract_reviewer_name(findings[i].description)

    # Group P1 findings by anchor overlap (connected components).
    # Two findings are in the same group if they share ≥1 non-file anchor.
    groups: dict[int, int] = {}  # finding_index → group_id
    next_group = 0
    for i in p1_indices:
        merged_into: int | None = None
        for j in p1_indices:
            if j >= i:
                break
            if j not in groups:
                continue
            shared = p1_anchors[i] & p1_anchors[j]
            non_file_shared = frozenset(a for a in shared if a.kind != "file_path")
            if non_file_shared:
                if merged_into is None:
                    groups[i] = groups[j]
                    merged_into = groups[j]
                elif groups[j] != merged_into:
                    # Merge two groups.
                    old_group = groups[j]
                    for k in list(groups):
                        if groups[k] == old_group:
                            groups[k] = merged_into
        if merged_into is None:
            groups[i] = next_group
            next_group += 1

    # Count distinct reviewers per group.
    group_reviewers: dict[int, set[str]] = {}
    for i in p1_indices:
        gid = groups[i]
        if gid not in group_reviewers:
            group_reviewers[gid] = set()
        reviewer = p1_reviewers[i]
        if reviewer is not None:
            group_reviewers[gid].add(reviewer)

    # Check recurrence against prior registry.
    recurring_indices: set[int] = set()
    if prior_registry and current_attempt > 0:
        prior_clean = [strip_reviewer_prefix(r.description) for r in prior_registry]
        prior_anchor_sets = [extract_anchors(d) for d in prior_clean]

        # Check which prior entries are from earlier attempts.
        prior_from_earlier = []
        for pi, rec in enumerate(prior_registry):
            if hasattr(rec, "cycle_first_seen") and rec.cycle_first_seen < current_attempt:
                prior_from_earlier.append(pi)

        for i in p1_indices:
            for pi in prior_from_earlier:
                shared = p1_anchors[i] & prior_anchor_sets[pi]
                non_file_shared = frozenset(a for a in shared if a.kind != "file_path")
                if non_file_shared:
                    recurring_indices.add(i)
                    break

    # Determine which P1s to downgrade.
    downgrade_indices: set[int] = set()
    for i in p1_indices:
        gid = groups[i]
        multi_reviewer = len(group_reviewers.get(gid, set())) >= 2
        is_recurring = i in recurring_indices
        if not multi_reviewer and not is_recurring:
            downgrade_indices.add(i)

    # Build rewritten findings list (PlanReviewFinding is frozen).
    downgrades: list[CorroborationDowngrade] = []
    rewritten: list[PlanReviewFinding] = []
    for i, f in enumerate(findings):
        if i in downgrade_indices:
            rewritten.append(
                PlanReviewFinding(
                    severity="P1-impl",
                    description=f.description,
                    suggestion=f.suggestion,
                )
            )
            downgrades.append(
                CorroborationDowngrade(
                    original_severity="P1",
                    effective_severity="P1-impl",
                    description=f.description,
                )
            )
        else:
            rewritten.append(f)

    return rewritten, downgrades


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

    if not result.story_matches and result.story_mismatches:
        bullets = "\n".join(f"- {m}" for m in result.story_mismatches)
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


def convention_violations_to_review_findings(violations: list[dict] | None) -> list[ReviewFinding]:
    """Convert blocking hard convention violations into actionable review-style findings."""
    findings: list[ReviewFinding] = []
    for violation in violations or []:
        if not violation.get("blocking", False):
            continue
        file = str(violation.get("file") or "unknown")
        rule = str(violation.get("rule") or "unknown")
        detail = str(violation.get("detail") or "No details recorded.")
        findings.append(
            ReviewFinding(
                severity="P1",
                file=file,
                line=None,
                observed=f"Hard convention violation [{rule}] in {file}: {detail}",
                expected=(
                    "Code merged to main must conform to the project's hard conventions; "
                    "any violation flagged as blocking must be resolved before approval, "
                    "regardless of the originating rule."
                ),
                evidence=f"{file} (rule: {rule})",
                suggestion=f"Resolve the [{rule}] convention violation in {file}.",
            )
        )
    return findings


def append_convention_retry_findings(
    existing_feedback: str | None, violations: list[dict] | None
) -> str | None:
    """Append blocking convention violations to dev retry findings in review format."""
    convention_findings = convention_violations_to_review_findings(violations)
    if not convention_findings:
        return existing_feedback

    convention_block = "\n".join(
        [
            "## Blocking Convention Violations",
            "",
            findings_to_markdown(convention_findings),
        ]
    )
    if existing_feedback:
        return f"{existing_feedback}\n\n{convention_block}"
    return convention_block


def _normalize_description(description: str) -> str:
    """Normalize a finding description for duplicate detection: lowercase + stripped."""
    return description.strip().lower()


def _dedup_findings(reviewer_findings: list[tuple[str, ReviewFinding]]) -> list[ReviewFinding]:
    """Deduplicate findings by (file, line, normalized description).

    First occurrence of each unique (file, line, normalized_description) key wins
    for all fields.  All reviewers who raised the same finding are collected into
    the ``reviewers`` attribution tuple on the canonical finding.
    """
    seen_reviewers: dict[tuple[str, int | None, str], list[str]] = {}
    canonical: dict[tuple[str, int | None, str], ReviewFinding] = {}

    for reviewer_name, finding in reviewer_findings:
        key = (finding.file, finding.line, _normalize_description(finding.description))
        if key not in canonical:
            canonical[key] = finding
            seen_reviewers[key] = []
        seen_reviewers[key].append(reviewer_name)

    result: list[ReviewFinding] = []
    for key, finding in canonical.items():
        result.append(
            ReviewFinding(
                severity=finding.severity,
                file=finding.file,
                line=finding.line,
                observed=finding.observed,
                expected=finding.expected,
                evidence=finding.evidence,
                suggestion=finding.suggestion,
                reviewers=tuple(seen_reviewers[key]),
            )
        )
    return result


def merge_review_results(results: list[ReviewResult], names: list[str]) -> ReviewResult:
    """Merge multiple ReviewResults into one without an LLM call.

    - Reviewers with parse_errors are **excluded** (degraded, not poison).
      If ALL reviewers have parse errors, the merged result carries the errors
      so the parse-retry loop in coord_phases can fire.
    - Verdict: REQUEST_CHANGES if any valid reviewer says so, else APPROVE
    - Summary: one line per valid reviewer labelled by name
    - Findings: union of all valid findings, deduplicated by (file, line, description)
    - story_matches: False if any valid reviewer says False
    - test_adequate: False if any valid reviewer says False
    """
    import logging as _logging

    valid: list[tuple[str, ReviewResult]] = []
    excluded_errors: list[str] = []
    for name, r in zip(names, results):
        if r.parse_errors:
            _logging.getLogger(__name__).warning(
                "REVIEW merge: excluding %s due to parse errors: %s",
                name,
                "; ".join(r.parse_errors),
            )
            for e in r.parse_errors:
                excluded_errors.append(f"[{name}] {e}")
        else:
            valid.append((name, r))

    # If ALL reviewers had parse errors, propagate so the retry loop can fire.
    if not valid:
        return ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="All reviewers produced unparseable output",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=excluded_errors or ["All reviewers failed to produce valid output"],
            raw_yaml={},
        )

    verdict = (
        "REQUEST_CHANGES" if any(r.verdict == "REQUEST_CHANGES" for _, r in valid) else "APPROVE"
    )
    summary_parts = [f"[{name}] {r.summary}" for name, r in valid]
    summary = " | ".join(summary_parts)

    reviewer_findings: list[tuple[str, ReviewFinding]] = []
    for name, r in valid:
        for f in r.findings:
            reviewer_findings.append((name, f))
    all_findings = _dedup_findings(reviewer_findings)

    story_matches = all(r.story_matches for _, r in valid)
    story_mismatches: list[str] = []
    for name, r in valid:
        for m in r.story_mismatches:
            story_mismatches.append(f"[{name}] {m}")

    test_adequate = all(r.test_adequate for _, r in valid)
    test_gaps: list[str] = []
    for name, r in valid:
        for g in r.test_gaps:
            test_gaps.append(f"[{name}] {g}")

    merged_ac = _merge_ac_verification([r.ac_verification for _, r in valid])
    merged_sanitization_audit = _merge_sanitization_audits(
        *(r.sanitization_audit for _, r in valid)
    )

    return ReviewResult(
        verdict=verdict,
        summary=summary,
        findings=all_findings,
        story_matches=story_matches,
        story_mismatches=story_mismatches,
        test_adequate=test_adequate,
        test_gaps=test_gaps,
        parse_errors=[],  # valid reviewers had no errors
        raw_yaml={},
        ac_verification=merged_ac,
        sanitization_audit=merged_sanitization_audit,
    )


def _merge_ac_verification(
    per_reviewer: list[tuple[ACVerification, ...]],
) -> tuple[ACVerification, ...]:
    """Combine per-reviewer ac_verification tables into one canonical table.

    Conservative rule: for the same criterion (case-insensitive normalized),
    the merged status is the *worst* across reviewers
    (NOT_VERIFIED > PARTIAL > VERIFIED). Evidence strings are joined by `;`.
    Order follows first-appearance in the reviewer list.
    """
    rank = {"VERIFIED": 0, "PARTIAL": 1, "NOT_VERIFIED": 2}
    by_key: dict[str, ACVerification] = {}
    order: list[str] = []
    for table in per_reviewer:
        for entry in table:
            key = entry.criterion.strip().lower()
            if key not in by_key:
                by_key[key] = entry
                order.append(key)
                continue
            existing = by_key[key]
            if rank.get(entry.status, 0) > rank.get(existing.status, 0):
                merged_evidence = entry.evidence
            else:
                merged_evidence = existing.evidence
            if existing.evidence and entry.evidence and existing.evidence != entry.evidence:
                merged_evidence = f"{existing.evidence}; {entry.evidence}"
            worst_status = (
                entry.status
                if rank.get(entry.status, 0) > rank.get(existing.status, 0)
                else existing.status
            )
            by_key[key] = ACVerification(
                criterion=existing.criterion,
                status=worst_status,
                evidence=merged_evidence,
            )
    return tuple(by_key[k] for k in order)


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
