"""YAML schema validation for review output.

The review agent produces structured YAML. This module validates it
mechanically — no LLM interprets the review. If the schema is violated,
the orchestrator treats it as REQUEST_CHANGES.
"""

from __future__ import annotations

from typing import Any

VALID_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
VALID_SEVERITIES = ("P1", "P2")


def validate_review_yaml(data: Any) -> list[str]:
    """Validate review YAML structure. Returns list of errors (empty = valid).

    Cross-validation rules:
    - APPROVE + any P1 → error (can't approve with blocking findings)
    - REQUEST_CHANGES + zero P1s → error (must justify the request)
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["review output root must be a YAML mapping"]

    # ── verdict ───────────────────────────────────────────────────
    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        errors.append(f"verdict must be one of {VALID_VERDICTS}, got: {verdict!r}")

    # ── summary ───────────────────────────────────────────────────
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")

    # ── findings ──────────────────────────────────────────────────
    findings = data.get("findings")
    if findings is None:
        findings = []
    if not isinstance(findings, list):
        errors.append("findings must be a list")
        findings = []

    p1_count = 0
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"findings[{i}] must be a mapping")
            continue

        severity = finding.get("severity")
        if severity not in VALID_SEVERITIES:
            errors.append(
                f"findings[{i}].severity must be one of {VALID_SEVERITIES}, "
                f"got: {severity!r}"
            )
        if severity == "P1":
            p1_count += 1

        if not finding.get("file"):
            errors.append(f"findings[{i}].file must be non-empty")

        if not finding.get("description"):
            errors.append(f"findings[{i}].description must be non-empty")

    # ── Cross-validation: verdict vs findings ─────────────────────
    if verdict == "APPROVE" and p1_count > 0:
        errors.append(
            f"verdict is APPROVE but {p1_count} P1 finding(s) exist — "
            f"cannot approve with blocking findings"
        )
    if verdict == "REQUEST_CHANGES" and p1_count == 0:
        errors.append(
            "verdict is REQUEST_CHANGES but no P1 findings exist — "
            "must have at least one P1 to justify REQUEST_CHANGES"
        )

    # ── spec_compliance ───────────────────────────────────────────
    spec = data.get("spec_compliance")
    if spec is None:
        errors.append("spec_compliance section is required")
    elif not isinstance(spec, dict):
        errors.append("spec_compliance must be a mapping")
    elif "matches_spec" not in spec:
        errors.append("spec_compliance.matches_spec is required (true/false)")

    # ── test_coverage ─────────────────────────────────────────────
    tests = data.get("test_coverage")
    if tests is None:
        errors.append("test_coverage section is required")
    elif not isinstance(tests, dict):
        errors.append("test_coverage must be a mapping")
    elif "adequate" not in tests:
        errors.append("test_coverage.adequate is required (true/false)")

    return errors
