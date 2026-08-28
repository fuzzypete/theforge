"""Shared ADR-0010 rejection wording for shelved triage disposition proposals."""

from __future__ import annotations

from typing import NoReturn

TRIAGE_PROPOSALS_SHELVED_EXIT_CODE = 2

_BODY_LINES = (
    "disposition proposals are shelved (ADR-0010).",
    "A full pass returned 29 needs_verification of 30 findings: the proposer's",
    "evidence is artifact presence and churn, which cannot establish whether a",
    "behavioral claim still holds.",
    "To verify findings instead: forge diagnose --dry-run --issue A,B --parallel 2",
    "Still supported: report generation (no flag, interactive), --ratify, --discard.",
    "See ADR-0010.",
)


class TriageProposalsShelvedError(RuntimeError):
    """Raised when a shelved triage proposal path is invoked."""


def triage_proposals_shelved_lines(
    *, first_prefix: str = "[forge] triage: ", continuation_prefix: str = "        "
) -> tuple[str, ...]:
    """Render the canonical ADR-0010 shelf message with caller-chosen prefixes."""

    first, *rest = _BODY_LINES
    return (f"{first_prefix}{first}", *(f"{continuation_prefix}{line}" for line in rest))


def triage_proposals_shelved_message(
    *, first_prefix: str = "[forge] triage: ", continuation_prefix: str = "        "
) -> str:
    """Return the canonical ADR-0010 shelf message as one printable string."""

    return "\n".join(
        triage_proposals_shelved_lines(
            first_prefix=first_prefix,
            continuation_prefix=continuation_prefix,
        )
    )


def raise_triage_proposals_shelved(
    *, first_prefix: str = "[forge] triage: ", continuation_prefix: str = "        "
) -> NoReturn:
    """Raise the canonical ADR-0010 shelf refusal for proposal-dispatch paths."""

    raise TriageProposalsShelvedError(
        triage_proposals_shelved_message(
            first_prefix=first_prefix,
            continuation_prefix=continuation_prefix,
        )
    )
