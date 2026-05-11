"""Reusable clarification Q&A primitives.

Centralizes the structured ambiguity-question records emitted when a
producer command (today `forge shape`; later `forge story`/#367 and any
other refusal-capable surface) needs the operator to disambiguate before
classification can proceed.

The module is intentionally tiny: a typed record, a registry of canonical
question codes, and a lookup that returns the questions for a named
situation. Every callable surface that asks the operator a clarification
question routes through this registry so the question vocabulary stays
inspectable in one place instead of accreting silently across commands.

Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClarificationQuestion:
    """A single structured question the operator must answer to proceed.

    ``code`` is the stable identifier audit consumers can group on (so
    repeat occurrences of the same disambiguation surface are countable);
    ``text`` is the human-facing wording shown in the CLI.
    """

    code: str
    text: str


# Canonical question vocabulary. Add new entries here so a single grep
# enumerates every clarification the system can raise.
QUESTIONS: dict[str, str] = {
    "what_kind_of_work": (
        "What kind of work is this — bug, enhancement, docs, or operator action?"
    ),
    "observable_outcome": ("What is the observable outcome that signals this is done?"),
    "deliverable_kind": ("Is the deliverable code behavior, docs, or an operator decision?"),
    "agent_completable": ("Should a dev agent be able to complete this without human action?"),
}


# Named situations consumers can request a question set for. Keeps the
# decision of *which* questions to ask out of the call sites.
SITUATIONS: dict[str, tuple[str, ...]] = {
    "no_signal": ("what_kind_of_work", "observable_outcome"),
    "enhancement_vs_operator_action": ("deliverable_kind", "agent_completable"),
}


def build(codes: tuple[str, ...]) -> tuple[ClarificationQuestion, ...]:
    """Return ClarificationQuestion records for the given codes.

    Unknown codes raise ``KeyError`` — this is intentional. A typo in a
    code should surface loudly rather than silently dropping a question.
    """
    return tuple(ClarificationQuestion(code=code, text=QUESTIONS[code]) for code in codes)


def for_situation(name: str) -> tuple[ClarificationQuestion, ...]:
    """Return the canonical question set for a named disambiguation situation."""
    return build(SITUATIONS[name])


__all__ = ["ClarificationQuestion", "QUESTIONS", "SITUATIONS", "build", "for_situation"]
