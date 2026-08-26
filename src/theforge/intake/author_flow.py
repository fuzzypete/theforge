"""Interactive pre-submission issue authoring flow.

Owns the deterministic part of ``forge author``: resolving the selected issue
type through the shared typed specification, collecting any required content
that is still missing or placeholder-only, rendering the result through the
typed document round trip, and validating the finished body with the same
``shape_check.check`` entry point every other surface uses.

The CLI remains a thin shell around this module: terminal prompts, GitHub I/O,
and file I/O live there; collection state, prompt derivation, section merging,
and shape-gate validation live here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from theforge.shape_check import Reason, Severity
from theforge.shape_check import check as shape_check
from theforge.shape_check.diagnosis_spec import REQUIRED_DIAGNOSIS_COMPONENTS
from theforge.shape_check.document import (
    IssueDocument,
    parse_issue_document,
    render_issue_document,
    replace_section,
    with_section,
)
from theforge.shape_check.issue_spec import (
    ACCEPTANCE_CRITERIA_SECTION,
    ISSUE_TYPES,
    IssueTypeSpec,
    Presence,
    SectionSpec,
    spec_for_label,
)
from theforge.shape_check.parsing import extract_bullets
from theforge.shape_check.placeholders import is_placeholder_only

_TITLE_KEY = "title"
_DIAGNOSIS_KEY = "diagnosis"
_INCOMPLETE_STATUS_PREFIX = "> Status: incomplete draft"
TODO_DRAFT_LABEL = "todo:draft"


class AuthoringStatus(str, Enum):
    RUNNABLE = "runnable"
    DRAFT = "draft"


@dataclass(frozen=True)
class AuthorPrompt:
    """One piece of author input the flow needs to collect."""

    key: str
    label: str
    prompt: str
    constraint: str
    existing: str = ""
    multiline: bool = False
    required: bool = True


@dataclass(frozen=True)
class MissingPart:
    """A required part the author declined or left structurally short."""

    key: str
    label: str
    detail: str


@dataclass(frozen=True)
class AuthorResult:
    """Result of running the authoring flow."""

    title: str
    labels: tuple[str, ...]
    body: str
    type_spec: IssueTypeSpec
    status: AuthoringStatus
    reasons: tuple[Reason, ...]
    missing_parts: tuple[MissingPart, ...]

    @property
    def runnable(self) -> bool:
        return self.status is AuthoringStatus.RUNNABLE

    def body_for_storage(self) -> str:
        """Return ``body`` with the generated draft marker normalized."""
        body = _strip_incomplete_marker(self.body)
        if self.runnable:
            return body
        labels = ", ".join(part.label for part in self.missing_parts)
        labels = labels or "shape-gate findings remain"
        status = (
            f"{_INCOMPLETE_STATUS_PREFIX} — do not submit yet. "
            f"Missing before submission: {labels}."
        )
        if not body:
            return status + "\n"
        return status + "\n\n" + body.lstrip("\n")


AnswerSource = Callable[[AuthorPrompt], str | None]


def run_author_flow(
    *,
    title: str,
    selected_type_label: str,
    existing_body: str = "",
    existing_labels: Iterable[str] = (),
    answer_source: AnswerSource,
) -> AuthorResult:
    """Collect required issue parts and return a runnable body or honest draft."""
    type_spec = spec_for_label(selected_type_label)
    if type_spec is None:
        raise ValueError(f"unrecognized issue type: {selected_type_label!r}")

    working_title = title.strip()
    document = parse_issue_document(existing_body or "", type_spec=type_spec)

    if not working_title:
        prompt = AuthorPrompt(
            key=_TITLE_KEY,
            label="Title",
            prompt="Issue title",
            constraint="name the work, not the implementation plan",
            multiline=False,
        )
        response = _clean_answer(answer_source(prompt))
        if response:
            working_title = response

    missing_parts: list[MissingPart] = []
    if not working_title:
        missing_parts.append(
            MissingPart(
                key=_TITLE_KEY,
                label="Title",
                detail="issue title missing",
            )
        )

    for section in type_spec.sections():
        if type_spec.presence_of(section.key) is not Presence.REQUIRED:
            continue
        if section.key == _DIAGNOSIS_KEY:
            document, missing = _collect_diagnosis(document, section, answer_source, type_spec)
        else:
            document, missing = _collect_required_section(
                document,
                section,
                answer_source,
                type_spec,
            )
        missing_parts.extend(missing)

    body = render_issue_document(document)
    gate = shape_check(
        working_title,
        _strip_incomplete_marker(body),
        _labels_for_status(existing_labels, type_spec, runnable=True),
    )

    runnable = bool(working_title) and not missing_parts and gate.admits_implementation_sprint
    status = AuthoringStatus.RUNNABLE if runnable else AuthoringStatus.DRAFT
    labels = _labels_for_status(existing_labels, type_spec, runnable=runnable)

    if not runnable and not missing_parts:
        missing_parts.extend(_missing_parts_from_reasons(gate.reasons, type_spec))

    return AuthorResult(
        title=working_title,
        labels=labels,
        body=_strip_incomplete_marker(body),
        type_spec=type_spec,
        status=status,
        reasons=gate.reasons,
        missing_parts=tuple(_dedupe_missing_parts(missing_parts)),
    )


def available_type_labels() -> tuple[str, ...]:
    """Return the issue-type labels an operator may select from."""
    return tuple(spec.label for spec in ISSUE_TYPES if spec.dispatchable and spec.declares_type)


def _collect_required_section(
    document: IssueDocument,
    section: SectionSpec,
    answer_source: AnswerSource,
    type_spec: IssueTypeSpec,
) -> tuple[IssueDocument, list[MissingPart]]:
    existing = _section_text(document, section.key)
    if not _section_needs_input(section, existing):
        return document, []

    prompt = AuthorPrompt(
        key=section.key,
        label=section.canonical_heading,
        prompt=section.summary,
        constraint=_section_constraint(section),
        existing=existing,
        multiline=True,
    )
    response = _clean_answer(answer_source(prompt))
    if not response:
        return document, [_missing_section(section)]
    document = _set_section(document, section.key, response, type_spec=type_spec)
    return document, []


def _collect_diagnosis(
    document: IssueDocument,
    section: SectionSpec,
    answer_source: AnswerSource,
    type_spec: IssueTypeSpec,
) -> tuple[IssueDocument, list[MissingPart]]:
    existing = _section_text(document, section.key)
    existing_values = _extract_field_values(existing, section)
    prefix = _diagnosis_prefix(existing, section)

    missing_fields = [
        field
        for field in section.fields
        if not _field_value_present(existing_values.get(field.key, ""))
    ]
    if not missing_fields and existing and not is_placeholder_only(existing):
        return document, []

    for field in missing_fields:
        prompt = AuthorPrompt(
            key=f"{section.key}.{field.key}",
            label=field.label,
            prompt=section.summary,
            constraint=f"{field.satisfies}. Example: {field.bullet()}",
            existing=existing_values.get(field.key, ""),
            multiline=False,
        )
        response = _clean_answer(answer_source(prompt))
        if not response:
            body = _render_diagnosis_body(prefix, section, existing_values)
            document = _set_section(document, section.key, body, type_spec=type_spec)
            remaining = missing_fields[missing_fields.index(field) :]
            return document, [_missing_field(item) for item in remaining]
        existing_values[field.key] = response

    body = _render_diagnosis_body(prefix, section, existing_values)
    document = _set_section(document, section.key, body, type_spec=type_spec)
    return document, []


def _section_text(document: IssueDocument, key: str) -> str:
    section = document.section(key)
    if section is None:
        return ""
    return section.body.strip("\n")


def _section_needs_input(section: SectionSpec, existing: str) -> bool:
    if not existing:
        return True
    if is_placeholder_only(existing):
        return True
    if section.key == ACCEPTANCE_CRITERIA_SECTION.key:
        return not extract_bullets(existing)
    return False


def _section_constraint(section: SectionSpec) -> str:
    if section.key == ACCEPTANCE_CRITERIA_SECTION.key:
        return (
            "write one or more Markdown bullets stating reviewer-checkable outcomes. "
            "Implementation steps, file paths, call sequences, and design plans belong later."
        )
    return section.summary


def _missing_section(section: SectionSpec) -> MissingPart:
    return MissingPart(
        key=section.key,
        label=section.canonical_heading,
        detail=f"{section.canonical_heading} missing or still too short",
    )


def _missing_field(field) -> MissingPart:
    return MissingPart(
        key=field.key,
        label=field.label,
        detail=f"{field.label} missing",
    )


def _set_section(
    document: IssueDocument,
    key: str,
    body: str,
    *,
    type_spec: IssueTypeSpec,
) -> IssueDocument:
    if document.has_section(key):
        return replace_section(document, key, body)
    return with_section(document, key, body, type_spec=type_spec)


def _extract_field_values(section_text: str, section: SectionSpec) -> dict[str, str]:
    return {
        field.key: _extract_field_value(section_text, field.label) or ""
        for field in section.fields
    }


def _extract_field_value(section_text: str, label: str) -> str | None:
    if not section_text:
        return None
    label_pattern = r"\s+".join(re.escape(word) for word in label.split())
    lines = section_text.splitlines()
    for index, line in enumerate(lines):
        cleaned = re.sub(r"^\s*#+\s*", "", line)
        cleaned = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", cleaned)
        cleaned = re.sub(r"^\*+\s*", "", cleaned)
        match = re.match(label_pattern + r"\b", cleaned, re.IGNORECASE)
        if match is None:
            continue
        rest = cleaned[match.end() :]
        rest = re.sub(r"^[\s*:.\-—]+", "", rest)
        value = re.sub(r"\*+\s*$", "", rest).strip()
        if value:
            return value
        for continuation in lines[index + 1 :]:
            stripped = continuation.strip()
            if not stripped:
                continue
            if _looks_like_field_line(stripped, REQUIRED_DIAGNOSIS_COMPONENTS):
                break
            return stripped
        return ""
    return None


def _diagnosis_prefix(section_text: str, section: SectionSpec) -> str:
    if not section_text:
        return ""
    lines = section_text.splitlines()
    prefix: list[str] = []
    for line in lines:
        stripped = line.strip()
        if _looks_like_field_line(stripped, section.fields):
            break
        prefix.append(line.rstrip())
    text = "\n".join(prefix).strip("\n")
    return "" if is_placeholder_only(text) else text


def _looks_like_field_line(stripped: str, fields) -> bool:
    for field in fields:
        label_pattern = r"\s+".join(re.escape(word) for word in field.label.split())
        cleaned = re.sub(r"^\s*#+\s*", "", stripped)
        cleaned = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", cleaned)
        cleaned = re.sub(r"^\*+\s*", "", cleaned)
        if re.match(label_pattern + r"\b", cleaned, re.IGNORECASE):
            return True
    return False


def _field_value_present(value: str) -> bool:
    value = value.strip()
    return bool(value) and not is_placeholder_only(value)


def _render_diagnosis_body(prefix: str, section: SectionSpec, values: dict[str, str]) -> str:
    lines: list[str] = []
    if prefix:
        lines.append(prefix)
        lines.append("")
    for field in section.fields:
        value = values.get(field.key, "").strip()
        lines.append(f"- **{field.label}:** {value}")
    return "\n".join(lines).rstrip() + "\n"


def _labels_for_status(
    existing_labels: Iterable[str],
    type_spec: IssueTypeSpec,
    *,
    runnable: bool,
) -> tuple[str, ...]:
    type_labels = {spec.label for spec in ISSUE_TYPES}
    labels: list[str] = []
    seen: set[str] = set()
    for raw in existing_labels:
        label = str(raw).strip()
        if not label:
            continue
        lowered = label.lower()
        if lowered in type_labels or lowered == TODO_DRAFT_LABEL:
            continue
        if lowered in seen:
            continue
        labels.append(label)
        seen.add(lowered)

    labels.append(type_spec.label)
    if not runnable:
        labels.append(TODO_DRAFT_LABEL)
    return tuple(labels)


def _missing_parts_from_reasons(
    reasons: Iterable[Reason],
    type_spec: IssueTypeSpec,
) -> list[MissingPart]:
    missing: list[MissingPart] = []
    for reason in reasons:
        if reason.code in {"missing_acceptance_criteria", "no_observable_done_state"}:
            missing.append(_missing_section(ACCEPTANCE_CRITERIA_SECTION))
            continue
        if reason.code == "needs_diagnosis":
            for field in REQUIRED_DIAGNOSIS_COMPONENTS:
                if field.label not in {part.label for part in missing}:
                    missing.append(_missing_field(field))
            continue
        if reason.code == "diagnosis_cause_unknown":
            missing.append(
                MissingPart(
                    key="diagnosis.confirmed_cause",
                    label="Confirmed cause",
                    detail=reason.detail,
                )
            )
            continue
        if reason.code in {
            state.refusal_code for state in type_spec.lifecycle_states if state.refusal_code
        }:
            missing.append(
                MissingPart(
                    key=reason.code,
                    label=type_spec.label,
                    detail=reason.detail,
                )
            )
            continue
        if reason.severity is not Severity.BLOCKING:
            continue
        missing.append(
            MissingPart(
                key=reason.code,
                label=_reason_label(reason.code),
                detail=reason.detail,
            )
        )
    return missing


def _dedupe_missing_parts(parts: Iterable[MissingPart]) -> list[MissingPart]:
    deduped: list[MissingPart] = []
    seen: set[tuple[str, str]] = set()
    for part in parts:
        marker = (part.key, part.label)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(part)
    return deduped


def _clean_answer(answer: str | None) -> str:
    return (answer or "").strip()


def _strip_incomplete_marker(body: str) -> str:
    body = body or ""
    while True:
        match = re.match(
            rf"^\s*{re.escape(_INCOMPLETE_STATUS_PREFIX)}[^\n]*(?:\n(?:[ \t]*\n)*)?",
            body,
        )
        if match is None:
            return body
        body = body[match.end() :].lstrip("\n")


def _reason_label(code: str) -> str:
    words = re.split(r"[_-]+", code.strip())
    return " ".join(word.capitalize() for word in words if word)
