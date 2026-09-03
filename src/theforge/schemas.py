"""YAML schema validation for review output.

The review agent produces structured YAML. This module validates it
mechanically — no LLM interprets the review. If the schema is violated,
the orchestrator treats it as REQUEST_CHANGES.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from theforge.knowledge_receipts import CLOSED_DISPOSITIONS

VALID_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
VALID_SEVERITIES = ("P1", "P2")
VALID_AC_VERIFICATION_STATUSES = ("VERIFIED", "PARTIAL", "NOT_VERIFIED")

# Audit-only prior-run knowledge receipt (#2866). Imported from the verifier
# rather than restated so a transport schema cannot advertise a disposition the
# verifier would then record as unrecognised. Nothing in review validation reads
# this field — a malformed debrief never rejects a verdict.
VALID_KNOWLEDGE_DISPOSITIONS = tuple(sorted(CLOSED_DISPOSITIONS))

# ── Parse-error taxonomy ─────────────────────────────────────────────
# Operator-facing logs and audit must distinguish which validation stage
# rejected an agent's output. The stage identifies the remediation surface:
# YAML_SYNTAX → parser/sanitizer; SCHEMA_VALIDATION → field shape/type;
# CONTRACT_CROSS_VALIDATION → verdict-vs-findings or AC contract rules;
# STRUCTURE → preconditions before schema validation (root is a mapping).

YAML_SYNTAX = "YAML_SYNTAX"
STRUCTURE = "STRUCTURE"
SCHEMA_VALIDATION = "SCHEMA_VALIDATION"
CONTRACT_CROSS_VALIDATION = "CONTRACT_CROSS_VALIDATION"

VALID_PARSE_ERROR_STAGES = (
    YAML_SYNTAX,
    STRUCTURE,
    SCHEMA_VALIDATION,
    CONTRACT_CROSS_VALIDATION,
)


@dataclass(frozen=True)
class ParseError:
    """A single rejection reason tagged with the validation stage that produced it.

    ``stage`` names the remediation surface — operators consume the stage to
    decide whether to fix the parser (YAML_SYNTAX), the prompt/schema description
    (SCHEMA_VALIDATION), or the contract rules themselves
    (CONTRACT_CROSS_VALIDATION). ``message`` is the human-readable detail.
    """

    stage: str
    message: str

    def __str__(self) -> str:
        return f"[{self.stage}] {self.message}"

    def __contains__(self, item: object) -> bool:
        # Substring checks against the rendered form so legacy assertions like
        # ``"APPROVE" in error`` continue to work after the structural refactor.
        return isinstance(item, str) and item in str(self)


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


def validate_review_yaml(data: Any) -> list[ParseError]:
    """Validate review YAML structure. Returns list of errors (empty = valid).

    Each returned :class:`ParseError` carries a ``stage`` tag identifying the
    validation layer that produced it (``STRUCTURE``, ``SCHEMA_VALIDATION``, or
    ``CONTRACT_CROSS_VALIDATION``) so operator-facing logs and audit can
    distinguish parser/schema failures from contract-cross-check failures
    without parsing the message string.

    Cross-validation rules:
    - APPROVE + any P1 → error (can't approve with blocking findings)
    - REQUEST_CHANGES + zero P1s → error (must justify the request)
    - APPROVE + empty ac_verification → error, UNLESS the reviewer declares
      ``criteria_enumerable: false`` with a non-empty
      ``criteria_enumerable_rationale`` (escape valve for issues with no
      enumerable acceptance criteria).
    """
    errors: list[ParseError] = []

    if not isinstance(data, dict):
        return [ParseError(stage=STRUCTURE, message="review output root must be a YAML mapping")]

    def _schema(msg: str) -> None:
        errors.append(ParseError(stage=SCHEMA_VALIDATION, message=msg))

    def _cross(msg: str) -> None:
        errors.append(ParseError(stage=CONTRACT_CROSS_VALIDATION, message=msg))

    # ── verdict ───────────────────────────────────────────────────
    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        _schema(f"verdict must be one of {VALID_VERDICTS}, got: {verdict!r}")

    # ── summary ───────────────────────────────────────────────────
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        _schema("summary must be a non-empty string")

    # ── findings ──────────────────────────────────────────────────
    findings = data.get("findings")
    if findings is None:
        findings = []
    if not isinstance(findings, list):
        _schema("findings must be a list")
        findings = []

    p1_count = 0
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            _schema(f"findings[{i}] must be a mapping")
            continue

        severity = finding.get("severity")
        if severity not in VALID_SEVERITIES:
            _schema(f"findings[{i}].severity must be one of {VALID_SEVERITIES}, got: {severity!r}")
        if severity == "P1":
            p1_count += 1
            file_val = finding.get("file")  # None = null (architectural), "" = empty (error)

            # P1 with file set must have a non-empty file path.
            # P1 with file: null is an architectural finding — no file required.
            if file_val is not None and not file_val:
                _schema(f"findings[{i}].file must be non-empty for P1 findings")

            # line may be null even when file is set: file-scope findings
            # (file existence, mode, whole-file state, structural hygiene) target
            # the path itself, not a specific source line. Architectural findings
            # (file: null) also do not require a line.

        for prose_field in ("observed", "expected", "evidence"):
            value = finding.get(prose_field)
            if not isinstance(value, str) or not value.strip():
                _schema(f"findings[{i}].{prose_field} must be a non-empty string")

    # ── Cross-validation: verdict vs findings ─────────────────────
    if verdict == "APPROVE" and p1_count > 0:
        _cross(
            f"verdict is APPROVE but {p1_count} P1 finding(s) exist — "
            f"cannot approve with blocking findings"
        )
    if verdict == "REQUEST_CHANGES" and p1_count == 0:
        _cross(
            "verdict is REQUEST_CHANGES but no P1 findings exist — "
            "must have at least one P1 to justify REQUEST_CHANGES"
        )

    # ── story_compliance (accept spec_compliance for backward compat) ──
    spec = data.get("story_compliance") or data.get("spec_compliance")
    if spec is None:
        _schema("story_compliance section is required")
    elif not isinstance(spec, dict):
        _schema("story_compliance must be a mapping")
    elif "matches_spec" not in spec:
        _schema("story_compliance.matches_spec is required (true/false)")

    # ── test_coverage ─────────────────────────────────────────────
    tests = data.get("test_coverage")
    if tests is None:
        _schema("test_coverage section is required")
    elif not isinstance(tests, dict):
        _schema("test_coverage must be a mapping")
    elif "adequate" not in tests:
        _schema("test_coverage.adequate is required (true/false)")

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
        _schema("ac_verification must be a list")
        ac_v = []
    for i, entry in enumerate(ac_v):
        if not isinstance(entry, dict):
            _schema(f"ac_verification[{i}] must be a mapping")
            continue
        criterion = entry.get("criterion")
        if not isinstance(criterion, str) or not criterion.strip():
            _schema(f"ac_verification[{i}].criterion must be a non-empty string")
        status = entry.get("status")
        if status not in VALID_AC_VERIFICATION_STATUSES:
            _schema(
                f"ac_verification[{i}].status must be one of "
                f"{VALID_AC_VERIFICATION_STATUSES}, got: {status!r}"
            )
        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            _schema(
                f"ac_verification[{i}].evidence must be a non-empty string "
                f"(diff hunks + test pointers for VERIFIED, reason otherwise)"
            )
        if status in ("PARTIAL", "NOT_VERIFIED"):
            non_verified_count += 1

    # ── criteria_enumerable escape valve ──────────────────────────
    # A reviewer who has reviewed the code, approves, and finds the issue has
    # no enumerable acceptance criteria (some bug fixes, chores) previously had
    # no legal move: APPROVE demands a non-empty ac_verification table. Rather
    # than force the reviewer to manufacture criteria — or oscillate between
    # APPROVE (empty table) and REQUEST_CHANGES (no P1) to dodge the rules —
    # this flag lets the reviewer declare the degenerate state explicitly. It
    # is not a free bypass: criteria_enumerable=false requires a non-empty
    # rationale so the assertion is deliberate and auditable.
    criteria_enumerable = data.get("criteria_enumerable", True)
    if not isinstance(criteria_enumerable, bool):
        _schema("criteria_enumerable must be a boolean when provided (true/false)")
        criteria_enumerable = True
    rationale = data.get("criteria_enumerable_rationale")
    if rationale is not None and not isinstance(rationale, str):
        _schema("criteria_enumerable_rationale must be a string when provided")
        rationale = None

    if verdict == "APPROVE":
        if not ac_v:
            if criteria_enumerable is False:
                # Legal degenerate state — require the reviewer to say why.
                if not isinstance(rationale, str) or not rationale.strip():
                    _cross(
                        "verdict is APPROVE with criteria_enumerable: false but "
                        "criteria_enumerable_rationale is empty — the reviewer must "
                        "state why the issue has no enumerable acceptance criteria"
                    )
            else:
                _cross(
                    "verdict is APPROVE but ac_verification is empty — "
                    "reviewers must enumerate each acceptance criterion (or symptom for bugs) "
                    "and mark it VERIFIED with evidence pointers, or set "
                    "criteria_enumerable: false with a rationale if the issue has none"
                )
        elif non_verified_count > 0:
            _cross(
                f"verdict is APPROVE but {non_verified_count} ac_verification entry(ies) "
                f"are PARTIAL or NOT_VERIFIED — cannot approve when any acceptance "
                f"criterion is unverified"
            )

    return errors


VALID_AC_STATUSES = ("MET", "PARTIAL", "NOT_MET")
VALID_GATE_RESULTS = ("PASS", "FAIL", "BLOCKED")


def dev_handoff_claims_unproven_completion(
    data: dict, *, honor_gate_delegation: bool = True
) -> bool:
    """Return True when a dev handoff claims completion without gate evidence.

    A completion claim is any acceptance_criteria entry marked ``status: MET``.
    Such a claim is normally only proven when ``gate_result`` is exactly
    ``"PASS"``. A missing gate_result, ``"FAIL"``, or ``"BLOCKED"`` all leave the
    completion unproven — the gate was never shown to pass, so the claim lacks
    evidence.

    Handoffs that make no completion claim (all criteria PARTIAL/NOT_MET) never
    trip this check: gate_result stays optional for them.

    Gate delegation exception: a review-fix iteration delegates gate execution to
    the coordinator (see ``task.fix_prompts.build_fix_prompt``), which runs the
    authoritative gate itself after the dev completes. Such a handoff legitimately
    lacks a self-reported PASS. When ``honor_gate_delegation`` is set and the
    handoff explicitly marks ``gate_delegated: true`` (strictly boolean ``True`` —
    a missing, string, or otherwise malformed value is NOT delegation), the
    completion claim is not treated as unproven. Callers that own authoritative
    knowledge of whether the gate was actually delegated (e.g. the coordinator
    dev-phase guard) pass ``honor_gate_delegation=False`` and gate on that
    knowledge instead, so an ordinary iteration cannot bypass the check by
    self-reporting the flag.
    """
    if not isinstance(data, dict):
        return False
    criteria = data.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return False
    claims_met = any(isinstance(ac, dict) and ac.get("status") == "MET" for ac in criteria)
    if not claims_met:
        return False
    if data.get("gate_result") == "PASS":
        return False
    if honor_gate_delegation and data.get("gate_delegated") is True:
        return False
    return True


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

    # ── gate_result ─────────────────────────────────────────────────
    # Optional for a non-completion handoff, but load-bearing for a completion
    # claim: an acceptance criterion marked MET requires gate_result: PASS as
    # evidence (see the cross-field check below).
    gate_result = data.get("gate_result")
    if gate_result is not None and (
        not isinstance(gate_result, str) or gate_result not in VALID_GATE_RESULTS
    ):
        errors.append(
            f"gate_result must be one of {VALID_GATE_RESULTS} when provided, got: {gate_result!r}"
        )

    # ── cross-field: completion claim requires gate evidence ─────────
    # A MET acceptance criterion is a completion claim; it is only proven when
    # the gate actually passed. Reject a completion claim whose gate_result is
    # absent, FAIL, or BLOCKED — an unrun or failing gate is a blocking failure,
    # not completion.
    if dev_handoff_claims_unproven_completion(data):
        errors.append(
            "acceptance_criteria mark MET but gate_result is not PASS — a completion "
            "claim requires gate evidence (set gate_result: PASS after the gate passes, "
            "or gate_result: BLOCKED and do not mark criteria MET if the gate could not "
            "be run)"
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
            "criteria_enumerable",
            "criteria_enumerable_rationale",
            "knowledge_debrief",
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
                    "required": [
                        "severity",
                        "file",
                        "line",
                        "observed",
                        "expected",
                        "evidence",
                        "suggestion",
                    ],
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": list(VALID_SEVERITIES),
                        },
                        "file": {"type": "string"},
                        "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "observed": {
                            "type": "string",
                            "description": (
                                "One sentence describing the observed behaviour, "
                                "without fix theory or verdict prose."
                            ),
                        },
                        "expected": {
                            "type": "string",
                            "description": (
                                "Category-level rule in flowing prose that "
                                "generalises beyond this single trigger."
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "File path, line, or anchor pointing at the offending code."
                            ),
                        },
                        "suggestion": {
                            "type": "string",
                            "description": (
                                "Optional non-binding fix guidance. Use empty string "
                                "when no suggestion is offered."
                            ),
                        },
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
            "criteria_enumerable": {
                "type": "boolean",
                "description": (
                    "Whether the issue has enumerable acceptance criteria to verify. "
                    "Set true in the normal case. Set false ONLY when the issue "
                    "genuinely has none to enumerate (e.g. some bug fixes or chores); "
                    "then ac_verification may be empty and you MUST supply a non-empty "
                    "criteria_enumerable_rationale. This is not a shortcut to avoid "
                    "verifying criteria that do exist."
                ),
            },
            "criteria_enumerable_rationale": {
                "type": "string",
                "description": (
                    "Required (non-empty) when criteria_enumerable is false: one "
                    "sentence stating why the issue has no enumerable acceptance "
                    "criteria. Empty string when criteria_enumerable is true."
                ),
            },
            # Audit-only receipt on injected prior-run claims (#2866). Declared
            # required-and-nullable rather than optional because this schema is
            # submitted with strict=true, where every property must appear in
            # `required`; a reviewer shown no prior-run claims sends null. It is
            # never read by verdict validation or by any coordinator decision.
            "knowledge_debrief": {
                "description": (
                    "Audit-only. One entry per prior-run claim reference shown in the "
                    "Repository Context Pack. Null when the pack carried no claims. "
                    "Never affects the verdict."
                ),
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["claim_ref", "disposition", "did", "evidence"],
                            "properties": {
                                "claim_ref": {"type": "string"},
                                "disposition": {
                                    "type": "string",
                                    "enum": list(VALID_KNOWLEDGE_DISPOSITIONS),
                                },
                                "did": {"type": "string"},
                                "evidence": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                ],
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
