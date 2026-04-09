from __future__ import annotations

from theforge.devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff
from theforge.schemas import validate_dev_handoff


_VALID_YAML = (
    'summary: "Implemented the thing."\n'
    "commits:\n"
    '  - sha: "abc1234"\n'
    '    message: "feat(x): implement the thing"\n'
    "acceptance_criteria:\n"
    '  - criterion: "It works"\n'
    "    status: MET\n"
    '    notes: "Tested"\n'
    "story_deviations: none\n"
    "deferred_items: none\n"
)


def test_validate_dev_handoff_allows_missing_gate_result() -> None:
    data = {
        "summary": "Implemented feature X with tests.",
        "commits": [{"sha": "abc1234", "message": "feat(x): implement feature X"}],
        "acceptance_criteria": [
            {"criterion": "Feature X works", "status": "MET", "notes": "Tested in test_x.py"}
        ],
        "story_deviations": "none",
        "deferred_items": "none",
    }

    assert validate_dev_handoff(data) == []


def test_parse_dev_handoff_accepts_missing_gate_result() -> None:
    result = parse_dev_handoff(_VALID_YAML)

    assert result.parse_errors == []
    assert result.gate_result is None


def test_reviewer_text_omits_gate_result_section() -> None:
    handoff = DevHandoff(
        summary="Implemented X.",
        commits=[{"sha": "abc1234", "message": "feat(x): implement X"}],
        acceptance_criteria=[{"criterion": "X works", "status": "MET", "notes": "Tested"}],
        story_deviations=[],
        deferred_items=[],
        gate_result=None,
        parse_errors=[],
        raw={},
    )

    text = dev_handoff_to_reviewer_text(handoff)
    assert "Gate Result" not in text
