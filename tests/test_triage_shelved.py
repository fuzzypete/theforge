"""Tests for shared ADR-0010 rejection wording helpers."""

from __future__ import annotations

import pytest

from theforge.triage_shelved import (
    TRIAGE_PROPOSALS_SHELVED_EXIT_CODE,
    TriageProposalsShelvedError,
    raise_triage_proposals_shelved,
    triage_proposals_shelved_lines,
    triage_proposals_shelved_message,
)


def test_exit_code_is_the_cli_contract_value() -> None:
    assert TRIAGE_PROPOSALS_SHELVED_EXIT_CODE == 2


def test_lines_render_with_caller_prefixes() -> None:
    lines = triage_proposals_shelved_lines(first_prefix="first: ", continuation_prefix="next: ")

    assert lines[0] == "first: disposition proposals are shelved (ADR-0010)."
    assert lines[-1] == "next: See ADR-0010."


def test_message_joins_lines_verbatim() -> None:
    lines = triage_proposals_shelved_lines()

    assert triage_proposals_shelved_message() == "\n".join(lines)


def test_raise_uses_the_canonical_message() -> None:
    with pytest.raises(TriageProposalsShelvedError) as excinfo:
        raise_triage_proposals_shelved()

    assert str(excinfo.value) == triage_proposals_shelved_message()
