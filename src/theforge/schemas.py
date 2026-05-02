"""YAML schema validation for review output.

The review agent produces structured YAML. This module validates it
mechanically — no LLM interprets the review. If the schema is violated,
the orchestrator treats it as REQUEST_CHANGES.
"""

from __future__ import annotations

from typing import Any

VALID_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
VALID_SEVERITIES = ("P1", "P2")
VALID_AC_VERIFICATION_STATUSES = ("VERIFIED", "PARTIAL", "NOT_VERIFIED")


def repair_review_yaml(data: Any) -> Any:
    """Best-effort repair of common review YAML issues before validation.

    Fixes predictable errors from models that struggle with the schema
    (especially DeepSeek): missing sections and findings as string instead of
    list. Cross-field verdict/finding contradictions are schema violations and
    must survive to validation so the reviewer output is retried instead of
    silently rewritten. Mutates and returns the dict.
    """
    if not isinstance(data, dict):
        return data

    # ── findings: string → wrap in list ─────────────────────────
    findings = data.get("findings")
    if isinstance(findings, str):
        data["findings"] = []
    elif findings is None:
        data["findings"] = []

    # ── story_compliance / spec_compliance: fill if missing ─────
    if "story_compliance" not in data and "spec_compliance" not in data:
        verdict = data.get("verdict", "")
        data["story_compliance"] = {
            "matches_spec": verdict == "APPROVE",
            "mismatches": [],
        }

    # ── test_coverage: fill if missing ──────────────────────────
    if "test_coverage" not in data:
        data["test_coverage"] = {"adequate": True, "gaps": []}

    # ── ac_verification: ensure list ─────────────────────────────
    # Missing or non-list → empty list. Cross-validation will reject
    # APPROVE with empty/non-VERIFIED ac_verification, forcing a retry.
    ac_v = data.get("ac_verification")
    if ac_v is None or not isinstance(ac_v, list):
        data["ac_verification"] = []

    return data


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
            file_val = finding.get("file")  # None = null (architectural), "" = empty (error)

            # P1 with file set must have a non-empty file path.
            # P1 with file: null is an architectural finding — no file required.
            if file_val is not None and not file_val:
                errors.append(f"findings[{i}].file must be non-empty for P1 findings")

            # P1 with file set must also cite a specific line to be actionable.
            # P1 with file: null (architectural finding) does not require a line.
            if file_val and not finding.get("line"):
                errors.append(
                    f"findings[{i}].line must be set for P1 findings with a file — "
                    f"vague P1s block the pipeline without clear fix targets"
                )

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

    # ── story_compliance (accept spec_compliance for backward compat) ──
    spec = data.get("story_compliance") or data.get("spec_compliance")
    if spec is None:
        errors.append("story_compliance section is required")
    elif not isinstance(spec, dict):
        errors.append("story_compliance must be a mapping")
    elif "matches_spec" not in spec:
        errors.append("story_compliance.matches_spec is required (true/false)")

    # ── test_coverage ─────────────────────────────────────────────
    tests = data.get("test_coverage")
    if tests is None:
        errors.append("test_coverage section is required")
    elif not isinstance(tests, dict):
        errors.append("test_coverage must be a mapping")
    elif "adequate" not in tests:
        errors.append("test_coverage.adequate is required (true/false)")

    # ── ac_verification ───────────────────────────────────────────
    # Per-AC verification table. Each entry must declare the criterion text
    # (or "Symptom resolution" for bug-type issues), a status, and evidence.
    # Cross-validation: APPROVE requires a non-empty table whose entries
    # are all VERIFIED. PARTIAL or NOT_VERIFIED entries on an APPROVE verdict
    # are a structural contradiction — same kind of rule as APPROVE+P1.
    ac_v = data.get("ac_verification")
    non_verified_count = 0
    if ac_v is None:
        ac_v = []
    if not isinstance(ac_v, list):
        errors.append("ac_verification must be a list")
        ac_v = []
    for i, entry in enumerate(ac_v):
        if not isinstance(entry, dict):
            errors.append(f"ac_verification[{i}] must be a mapping")
            continue
        criterion = entry.get("criterion")
        if not isinstance(criterion, str) or not criterion.strip():
            errors.append(f"ac_verification[{i}].criterion must be a non-empty string")
        status = entry.get("status")
        if status not in VALID_AC_VERIFICATION_STATUSES:
            errors.append(
                f"ac_verification[{i}].status must be one of "
                f"{VALID_AC_VERIFICATION_STATUSES}, got: {status!r}"
            )
        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append(
                f"ac_verification[{i}].evidence must be a non-empty string "
                f"(diff hunks + test pointers for VERIFIED, reason otherwise)"
            )
        if status in ("PARTIAL", "NOT_VERIFIED"):
            non_verified_count += 1

    if verdict == "APPROVE":
        if not ac_v:
            errors.append(
                "verdict is APPROVE but ac_verification is empty — "
                "reviewers must enumerate each acceptance criterion (or symptom for bugs) "
                "and mark it VERIFIED with evidence pointers"
            )
        elif non_verified_count > 0:
            errors.append(
                f"verdict is APPROVE but {non_verified_count} ac_verification entry(ies) "
                f"are PARTIAL or NOT_VERIFIED — cannot approve when any acceptance "
                f"criterion is unverified"
            )

    return errors


VALID_AC_STATUSES = ("MET", "PARTIAL", "NOT_MET")
VALID_GATE_RESULTS = ("PASS", "FAIL")


def validate_dev_handoff(data: Any) -> list[str]:
    """Validate dev handoff structure. Returns list of errors (empty = valid).

    Required fields:
    - summary: non-empty string describing what was implemented
    - commits: non-empty list of {sha, message}
    - acceptance_criteria: non-empty list of {criterion, status, notes}
    - story_deviations: list of {description, justification} or the string "none"
    - deferred_items: list of {description, reason} or the string "none"
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

    # ── gate_result (optional; coordinator is source of truth) ───────
    gate_result = data.get("gate_result")
    if gate_result is not None and (
        not isinstance(gate_result, str) or gate_result not in VALID_GATE_RESULTS
    ):
        errors.append(
            f"gate_result must be one of {VALID_GATE_RESULTS} when provided, got: {gate_result!r}"
        )

    # ── story_deviations (accept spec_deviations for backward compat) ─
    deviations = (
        data.get("story_deviations") if "story_deviations" in data else data.get("spec_deviations")
    )
    if deviations is None:
        errors.append("story_deviations is required (list of deviations or 'none')")
    elif isinstance(deviations, str):
        if deviations.strip().lower() != "none":
            errors.append("story_deviations must be a list or the string 'none'")
    elif isinstance(deviations, list):
        for i, d in enumerate(deviations):
            if not isinstance(d, dict):
                errors.append(f"story_deviations[{i}] must be a mapping")
                continue
            desc = d.get("description")
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f"story_deviations[{i}].description must be a non-empty string")
            just = d.get("justification")
            if not isinstance(just, str) or not just.strip():
                errors.append(f"story_deviations[{i}].justification must be a non-empty string")
    else:
        errors.append("story_deviations must be a list or the string 'none'")

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

    return errors


def review_json_schema() -> dict:
    """Export the review schema as a JSON Schema dict.

    Conforms to OpenAI strict JSON Schema requirements:
    - additionalProperties: false on every object
    - no type arrays (use anyOf/oneOf for nullable fields)
    - all object properties listed in required
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "summary",
            "findings",
            "story_compliance",
            "test_coverage",
            "ac_verification",
        ],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(VALID_VERDICTS),
                "description": "The overall verdict of the review.",
            },
            "summary": {
                "type": "string",
                "description": "A one-line summary of the review.",
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["severity", "file", "line", "description", "suggestion"],
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": list(VALID_SEVERITIES),
                        },
                        "file": {"type": "string"},
                        "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "description": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                },
            },
            "story_compliance": {
                "type": "object",
                "additionalProperties": False,
                "required": ["matches_spec", "mismatches"],
                "properties": {
                    "matches_spec": {"type": "boolean"},
                    "mismatches": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "test_coverage": {
                "type": "object",
                "additionalProperties": False,
                "required": ["adequate", "gaps"],
                "properties": {
                    "adequate": {"type": "boolean"},
                    "gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "ac_verification": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "status", "evidence"],
                    "properties": {
                        "criterion": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": list(VALID_AC_VERIFICATION_STATUSES),
                        },
                        "evidence": {"type": "string"},
                    },
                },
            },
        },
    }


def plan_review_json_schema() -> dict:
    """Export the plan review schema as a JSON Schema dict.

    Mirrors the submit_plan_review tool parameters in schema_utils.py.
    Conforms to OpenAI strict JSON Schema requirements:
    - additionalProperties: false on every object
    - all object properties listed in required
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "summary", "findings", "criteria_coverage"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["APPROVE", "REJECT"],
                "description": "Overall verdict on the plan.",
            },
            "summary": {
                "type": "string",
                "description": "One-line summary of the review.",
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["severity", "description"],
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": ["P0", "P1", "P1-impl", "P2"],
                        },
                        "description": {"type": "string"},
                    },
                },
            },
            "criteria_coverage": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "covered", "plan_section"],
                    "properties": {
                        "criterion": {"type": "string"},
                        "covered": {"type": "boolean"},
                        "plan_section": {"type": "string"},
                    },
                },
            },
        },
    }
