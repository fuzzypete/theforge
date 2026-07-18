from __future__ import annotations

from theforge.devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff
from theforge.schemas import validate_dev_handoff

# gate_result stays optional for a NON-completion handoff (no acceptance
# criterion marked MET), but a completion claim (any AC MET) now requires
# gate_result: PASS as evidence. See issue #927.

_NON_COMPLETION_YAML = (
    'summary: "Work in progress."\n'
    "commits:\n"
    '  - sha: "abc1234"\n'
    '    message: "feat(x): partial work"\n'
    "acceptance_criteria:\n"
    '  - criterion: "It works"\n'
    "    status: PARTIAL\n"
    '    notes: "not finished"\n'
    "story_deviations: none\n"
    "deferred_items: none\n"
)


def test_validate_dev_handoff_allows_missing_gate_result_for_non_completion() -> None:
    # No AC marked MET → no completion claim → gate_result stays optional.
    data = {
        "summary": "Work in progress on feature X.",
        "commits": [{"sha": "abc1234", "message": "feat(x): partial work"}],
        "acceptance_criteria": [
            {"criterion": "Feature X works", "status": "PARTIAL", "notes": "still working"}
        ],
        "story_deviations": "none",
        "deferred_items": "none",
    }

    assert validate_dev_handoff(data) == []


def test_validate_dev_handoff_completion_requires_gate_pass() -> None:
    # An AC marked MET without gate_result: PASS is a completion claim without
    # evidence and must be rejected.
    data = {
        "summary": "Implemented feature X with tests.",
        "commits": [{"sha": "abc1234", "message": "feat(x): implement feature X"}],
        "acceptance_criteria": [
            {"criterion": "Feature X works", "status": "MET", "notes": "Tested in test_x.py"}
        ],
        "story_deviations": "none",
        "deferred_items": "none",
    }

    errors = validate_dev_handoff(data)
    assert any("MET but gate_result is not PASS" in e for e in errors)


def test_validate_dev_handoff_completion_with_gate_pass_is_valid() -> None:
    data = {
        "summary": "Implemented feature X with tests.",
        "commits": [{"sha": "abc1234", "message": "feat(x): implement feature X"}],
        "acceptance_criteria": [
            {"criterion": "Feature X works", "status": "MET", "notes": "Tested in test_x.py"}
        ],
        "gate_result": "PASS",
        "story_deviations": "none",
        "deferred_items": "none",
    }

    assert validate_dev_handoff(data) == []


def test_parse_dev_handoff_accepts_missing_gate_result_for_non_completion() -> None:
    result = parse_dev_handoff(_NON_COMPLETION_YAML)

    assert result.parse_errors == []
    assert result.gate_result is None


def test_reviewer_text_omits_gate_result_section() -> None:
    handoff = DevHandoff(
        summary="Implemented X.",
        commits=[{"sha": "abc1234", "message": "feat(x): implement X"}],
        acceptance_criteria=[{"criterion": "X works", "status": "PARTIAL", "notes": "wip"}],
        story_deviations=[],
        deferred_items=[],
        gate_result=None,
        parse_errors=[],
        raw={},
    )

    text = dev_handoff_to_reviewer_text(handoff)
    assert "Gate Result" not in text
