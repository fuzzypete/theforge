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
                f"findings[{i}].severity must be one of {VALID_SEVERITIES}, got: {severity!r}"
            )
        if severity == "P1":
            p1_count += 1
            # P1 findings should cite a specific line to be actionable
            if not finding.get("line"):
                errors.append(
                    f"findings[{i}].line should be set for P1 findings — "
                    f"vague P1s block the pipeline without clear fix targets"
                )

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


VALID_AC_STATUSES = ("MET", "PARTIAL", "NOT_MET")
VALID_GATE_RESULTS = ("PASS", "FAIL")


def validate_dev_handoff(data: Any) -> list[str]:
    """Validate dev handoff structure. Returns list of errors (empty = valid).

    Required fields:
    - summary: non-empty string describing what was implemented
    - commits: non-empty list of {sha, message}
    - acceptance_criteria: non-empty list of {criterion, status, notes}
    - spec_deviations: list of {description, justification} or the string "none"
    - deferred_items: list of {description, reason} or the string "none"
    - gate_result: "PASS" or "FAIL"
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["dev handoff must be a YAML mapping"]

    # ── summary ────────────────────────────────────────────────────
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")

    # ── commits ────────────────────────────────────────────────────
    commits = data.get("commits")
    if commits is None:
        errors.append("commits is required (non-empty list of {sha, message})")
    elif not isinstance(commits, list):
        errors.append("commits must be a list")
    elif len(commits) == 0:
        errors.append("commits must be non-empty")
    else:
        for i, c in enumerate(commits):
            if not isinstance(c, dict):
                errors.append(f"commits[{i}] must be a mapping")
                continue
            sha = c.get("sha")
            if not isinstance(sha, str) or not sha.strip():
                errors.append(f"commits[{i}].sha must be a non-empty string")
            msg = c.get("message")
            if not isinstance(msg, str) or not msg.strip():
                errors.append(f"commits[{i}].message must be a non-empty string")

    # ── acceptance_criteria ─────────────────────────────────────────
    criteria = data.get("acceptance_criteria")
    if criteria is None:
        errors.append(
            "acceptance_criteria is required (non-empty list of {criterion, status, notes})"
        )
    elif not isinstance(criteria, list):
        errors.append("acceptance_criteria must be a list")
    elif len(criteria) == 0:
        errors.append("acceptance_criteria must be non-empty")
    else:
        for i, ac in enumerate(criteria):
            if not isinstance(ac, dict):
                errors.append(f"acceptance_criteria[{i}] must be a mapping")
                continue
            criterion = ac.get("criterion")
            if not isinstance(criterion, str) or not criterion.strip():
                errors.append(f"acceptance_criteria[{i}].criterion must be a non-empty string")
            status = ac.get("status")
            if not isinstance(status, str) or status not in VALID_AC_STATUSES:
                errors.append(
                    f"acceptance_criteria[{i}].status must be one of "
                    f"{VALID_AC_STATUSES}, got: {status!r}"
                )
            notes = ac.get("notes")
            if not isinstance(notes, str) or not notes.strip():
                errors.append(f"acceptance_criteria[{i}].notes must be a non-empty string")

    # ── spec_deviations ────────────────────────────────────────────
    deviations = data.get("spec_deviations")
    if deviations is None:
        errors.append("spec_deviations is required (list of deviations or 'none')")
    elif isinstance(deviations, str):
        if deviations.strip().lower() != "none":
            errors.append("spec_deviations must be a list or the string 'none'")
    elif isinstance(deviations, list):
        for i, d in enumerate(deviations):
            if not isinstance(d, dict):
                errors.append(f"spec_deviations[{i}] must be a mapping")
                continue
            desc = d.get("description")
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f"spec_deviations[{i}].description must be a non-empty string")
            just = d.get("justification")
            if not isinstance(just, str) or not just.strip():
                errors.append(f"spec_deviations[{i}].justification must be a non-empty string")
    else:
        errors.append("spec_deviations must be a list or the string 'none'")

    # ── deferred_items ─────────────────────────────────────────────
    deferred = data.get("deferred_items")
    if deferred is None:
        errors.append("deferred_items is required (list of items or 'none')")
    elif isinstance(deferred, str):
        if deferred.strip().lower() != "none":
            errors.append("deferred_items must be a list or the string 'none'")
    elif isinstance(deferred, list):
        for i, d in enumerate(deferred):
            if not isinstance(d, dict):
                errors.append(f"deferred_items[{i}] must be a mapping")
                continue
            desc = d.get("description")
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f"deferred_items[{i}].description must be a non-empty string")
            reason = d.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"deferred_items[{i}].reason must be a non-empty string")
    else:
        errors.append("deferred_items must be a list or the string 'none'")

    # ── gate_result ────────────────────────────────────────────────
    gate_result = data.get("gate_result")
    if gate_result not in VALID_GATE_RESULTS:
        errors.append(f"gate_result must be one of {VALID_GATE_RESULTS}, got: {gate_result!r}")

    return errors
