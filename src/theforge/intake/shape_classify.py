"""Heuristic classifier for `forge shape`.

Given a rough draft (issue title + body + labels), proposes one of TheForge's
typed work-object classifications, a confidence band, and — when confidence
is low — structured ambiguity questions. Stdlib only.

The classifier is intentionally heuristic-first. Per ADR-0001 and project
convention, the coordinator does not delegate process decisions to LLMs;
the LLM-refinement seam used by ``shape_check`` is reserved for fuzzy
verdicts and is not wired into the shape *command* in this slice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from theforge.shape_check.parsing import (
    extract_ac_section,
    extract_section,
    has_heading,
)


class Classification(str, Enum):
    BUG = "bug"
    ENHANCEMENT = "enhancement"
    EPIC = "epic"
    OPERATOR_ACTION = "operator-action"
    DOCUMENTATION = "documentation"
    ADR_CANDIDATE = "adr-candidate"
    DUPLICATE_OR_STALE = "duplicate-or-stale"
    UNRESOLVED = "unresolved"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DiagnosisState(str, Enum):
    NO_DIAGNOSIS = "no-diagnosis"
    DIAGNOSIS_CAUSE_UNKNOWN = "diagnosis-cause-unknown"
    DIAGNOSIS_CONFIRMED_CAUSE = "diagnosis-confirmed-cause"


@dataclass(frozen=True)
class AmbiguityQuestion:
    code: str
    text: str


@dataclass(frozen=True)
class ShapeProposal:
    classification: Classification
    confidence: Confidence
    diagnosis_state: DiagnosisState | None = None
    proposed_labels: tuple[str, ...] = ()
    removed_labels: tuple[str, ...] = ()
    ambiguity_questions: tuple[AmbiguityQuestion, ...] = ()
    proposed_adr_slug: str | None = None
    proposed_adr_title: str | None = None
    proposed_child_stories: tuple[str, ...] = ()
    rationale: str = ""
    proposed_body: str | None = None

    @property
    def kept_as_todo_draft(self) -> bool:
        return self.classification is Classification.UNRESOLVED


_BUG_TITLE_TOKENS = ("bug:", "fix:", "broken", "regression", "fails", "crashes")
_DOC_TITLE_TOKENS = ("doc:", "docs:", "documentation:")
_EPIC_TITLE_TOKENS = ("epic:", "[epic]")
_ADR_TITLE_TOKENS = ("adr:", "[adr]", "architecture decision")
_DUP_TITLE_TOKENS = ("duplicate of", "supersedes", "superseded by")
_OPERATOR_TITLE_TOKENS = (
    "operator:",
    "operator action",
    "human action",
)


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(tok in lowered for tok in tokens)


def _label_set(labels: list[str]) -> set[str]:
    return {label.strip().lower() for label in labels if label and label.strip()}


def _detect_diagnosis_state(body: str) -> DiagnosisState:
    section = extract_section(body, r"diagnosis|root cause")
    if section is None:
        return DiagnosisState.NO_DIAGNOSIS
    lowered = section.lower()
    if "confirmed cause" in lowered or "root cause:" in lowered or "fix:" in lowered:
        return DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE
    if "no diagnosis" in lowered or "not yet" in lowered:
        return DiagnosisState.NO_DIAGNOSIS
    return DiagnosisState.DIAGNOSIS_CAUSE_UNKNOWN


def _looks_like_bug(title: str, body: str, labels: set[str]) -> bool:
    if "bug" in labels:
        return True
    if _has_any(title, _BUG_TITLE_TOKENS):
        return True
    if has_heading(body, r"observed|actual behaviou?r|steps to reproduce"):
        return True
    return False


def _looks_like_enhancement(title: str, body: str, labels: set[str]) -> bool:
    if "enhancement" in labels or "story" in labels or "feature" in labels:
        return True
    if extract_ac_section(body) is not None:
        return True
    return False


def _looks_like_epic(title: str, body: str, labels: set[str]) -> bool:
    if "epic" in labels:
        return True
    if _has_any(title, _EPIC_TITLE_TOKENS):
        return True
    if has_heading(body, r"child (?:stories|issues)|sub[- ]?issues|tasks?\b"):
        # Multiple child bullets ⇒ epic-shaped only if several are present.
        section = extract_section(body, r"child (?:stories|issues)|sub[- ]?issues|tasks?\b") or ""
        bullets = [line for line in section.splitlines() if line.strip().startswith(("-", "*"))]
        if len(bullets) >= 2:
            return True
    return False


def _looks_like_documentation(title: str, body: str, labels: set[str]) -> bool:
    if "documentation" in labels or "docs" in labels:
        return True
    if _has_any(title, _DOC_TITLE_TOKENS):
        return True
    if "docs/" in body or "README" in body and "documentation" in body.lower():
        return True
    return False


def _looks_like_adr_candidate(title: str, body: str, labels: set[str]) -> bool:
    if "adr" in labels or "adr-candidate" in labels:
        return True
    if _has_any(title, _ADR_TITLE_TOKENS):
        return True
    if has_heading(body, r"decision|alternatives considered|trade[- ]?offs?"):
        return True
    return False


def _looks_like_operator_action(title: str, body: str, labels: set[str]) -> bool:
    if "operator-action" in labels:
        return True
    if _has_any(title, _OPERATOR_TITLE_TOKENS):
        return True
    if has_heading(body, r"operator action|manual step"):
        return True
    return False


def _looks_like_duplicate_or_stale(title: str, body: str, labels: set[str]) -> bool:
    if "duplicate" in labels or "stale" in labels or "wontfix" in labels:
        return True
    if _has_any(title, _DUP_TITLE_TOKENS):
        return True
    if _has_any(body, ("duplicate of #", "superseded by #", "closing as stale")):
        return True
    return False


def _slugify(text: str, max_len: int = 60) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:max_len] or "adr"


def _propose_child_stories(body: str) -> tuple[str, ...]:
    section = extract_section(body, r"child (?:stories|issues)|sub[- ]?issues|tasks?\b")
    if section is None:
        return ()
    bullets: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*")):
            bullets.append(stripped.lstrip("-* ").strip())
    return tuple(b for b in bullets if b)


def classify(title: str, body: str, labels: list[str]) -> ShapeProposal:
    """Return a heuristic shape proposal for the given draft.

    The classifier prefers high-confidence single-category matches. When two
    categories present non-trivial signal, the result is downgraded to
    ``UNRESOLVED`` with ambiguity questions so the operator can disambiguate
    rather than being silently force-classified.
    """
    title = title or ""
    body = body or ""
    label_set = _label_set(labels or [])

    matches: list[Classification] = []
    if _looks_like_duplicate_or_stale(title, body, label_set):
        matches.append(Classification.DUPLICATE_OR_STALE)
    if _looks_like_epic(title, body, label_set):
        matches.append(Classification.EPIC)
    if _looks_like_adr_candidate(title, body, label_set):
        matches.append(Classification.ADR_CANDIDATE)
    if _looks_like_bug(title, body, label_set):
        matches.append(Classification.BUG)
    if _looks_like_enhancement(title, body, label_set):
        matches.append(Classification.ENHANCEMENT)
    if _looks_like_documentation(title, body, label_set):
        matches.append(Classification.DOCUMENTATION)
    if _looks_like_operator_action(title, body, label_set):
        matches.append(Classification.OPERATOR_ACTION)

    if not matches:
        # No signal at all → low confidence; ask the operator.
        return ShapeProposal(
            classification=Classification.UNRESOLVED,
            confidence=Confidence.LOW,
            ambiguity_questions=_default_ambiguity_questions(),
            rationale=(
                "No classifying signal in title, body, or labels — the draft is too thin to shape."
            ),
        )

    # Disambiguation rules: enhancement vs operator-action is the most common
    # operator-pain case, so when both fire we keep as todo:draft and ask.
    if Classification.ENHANCEMENT in matches and Classification.OPERATOR_ACTION in matches:
        return ShapeProposal(
            classification=Classification.UNRESOLVED,
            confidence=Confidence.LOW,
            ambiguity_questions=(
                AmbiguityQuestion(
                    code="deliverable_kind",
                    text=("Is the deliverable code behavior, docs, or an operator decision?"),
                ),
                AmbiguityQuestion(
                    code="agent_completable",
                    text=("Should a dev agent be able to complete this without human action?"),
                ),
            ),
            rationale="could be either operator-action or enhancement",
        )

    # Confidence: HIGH when exactly one category fires; MEDIUM when a high-
    # priority category fires alongside a low-priority one; otherwise the
    # caller already handled UNRESOLVED above.
    primary = matches[0]
    confidence = Confidence.HIGH if len(matches) == 1 else Confidence.MEDIUM

    proposed_labels: tuple[str, ...]
    removed_labels: tuple[str, ...] = ("todo:draft",) if "todo:draft" in label_set else ()
    diagnosis_state: DiagnosisState | None = None
    proposed_adr_slug: str | None = None
    proposed_adr_title: str | None = None
    proposed_child_stories: tuple[str, ...] = ()
    rationale = ""

    if primary is Classification.BUG:
        proposed_labels = ("bug",)
        diagnosis_state = _detect_diagnosis_state(body)
        rationale = "title/body/labels match bug shape"
    elif primary is Classification.ENHANCEMENT:
        proposed_labels = ("enhancement",)
        rationale = "feature-shaped: acceptance-criteria heading or enhancement label"
    elif primary is Classification.EPIC:
        proposed_labels = ("epic",)
        proposed_child_stories = _propose_child_stories(body)
        rationale = "epic-shaped: multiple child stories proposed"
    elif primary is Classification.OPERATOR_ACTION:
        proposed_labels = ("operator-action",)
        rationale = "operator-action: deliverable is human action, not dev pipeline"
    elif primary is Classification.DOCUMENTATION:
        proposed_labels = ("documentation",)
        rationale = "docs-shaped change"
    elif primary is Classification.ADR_CANDIDATE:
        proposed_labels = ("adr-candidate",)
        proposed_adr_title = title.strip() or "untitled-design-decision"
        proposed_adr_slug = _slugify(title)
        rationale = "design-decision shape: propose ADR slug + title (file not written)"
    elif primary is Classification.DUPLICATE_OR_STALE:
        proposed_labels = ("duplicate",)
        rationale = "duplicate/stale: close or merge instead of sprinting"
    else:
        proposed_labels = ()

    return ShapeProposal(
        classification=primary,
        confidence=confidence,
        diagnosis_state=diagnosis_state,
        proposed_labels=proposed_labels,
        removed_labels=removed_labels,
        proposed_adr_slug=proposed_adr_slug,
        proposed_adr_title=proposed_adr_title,
        proposed_child_stories=proposed_child_stories,
        rationale=rationale,
    )


def _default_ambiguity_questions() -> tuple[AmbiguityQuestion, ...]:
    return (
        AmbiguityQuestion(
            code="what_kind_of_work",
            text="What kind of work is this — bug, enhancement, docs, or operator action?",
        ),
        AmbiguityQuestion(
            code="observable_outcome",
            text="What is the observable outcome that signals this is done?",
        ),
    )


__all__ = [
    "AmbiguityQuestion",
    "Classification",
    "Confidence",
    "DiagnosisState",
    "ShapeProposal",
    "classify",
]
