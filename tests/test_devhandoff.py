"""Tests for dev handoff schema validation, parsing, and formatting."""

from theforge.devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff
from theforge.schemas import validate_dev_handoff

# ── Schema validation ─────────────────────────────────────────────────


def _valid_handoff() -> dict:
    """Return a minimal valid dev handoff structure."""
    return {
        "summary": "Implemented feature X with tests.",
        "commits": [
            {"sha": "abc1234", "message": "feat(x): implement feature X"},
        ],
        "acceptance_criteria": [
            {"criterion": "Feature X works", "status": "MET", "notes": "Tested in test_x.py"},
        ],
        "story_deviations": "none",
        "deferred_items": "none",
        "gate_result": "PASS",
    }


class TestValidateDevHandoff:
    def test_valid_minimal(self):
        errors = validate_dev_handoff(_valid_handoff())
        assert errors == []

    def test_valid_with_deviations_list(self):
        data = _valid_handoff()
        data["story_deviations"] = [
            {
                "description": "Used coord_util instead of coordinator",
                "justification": "Shared by 4 modules",
            }
        ]
        errors = validate_dev_handoff(data)
        assert errors == []

    def test_valid_with_deferred_list(self):
        data = _valid_handoff()
        data["deferred_items"] = [
            {
                "description": "Caching layer",
                "reason": "Not needed for MVP",
            }
        ]
        errors = validate_dev_handoff(data)
        assert errors == []

    def test_valid_multiple_commits(self):
        data = _valid_handoff()
        data["commits"] = [
            {"sha": "abc1234", "message": "feat(x): implement feature X"},
            {"sha": "def5678", "message": "test(x): add tests for feature X"},
        ]
        errors = validate_dev_handoff(data)
        assert errors == []

    def test_valid_multiple_acceptance_criteria(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = [
            {"criterion": "AC 1", "status": "MET", "notes": "done"},
            {"criterion": "AC 2", "status": "PARTIAL", "notes": "mostly done"},
            {"criterion": "AC 3", "status": "NOT_MET", "notes": "deferred"},
        ]
        errors = validate_dev_handoff(data)
        assert errors == []

    def test_valid_gate_result_fail(self):
        data = _valid_handoff()
        data["gate_result"] = "FAIL"
        errors = validate_dev_handoff(data)
        assert errors == []

    # ── missing fields ─────────────────────────────────────────────

    def test_missing_summary(self):
        data = _valid_handoff()
        del data["summary"]
        errors = validate_dev_handoff(data)
        assert any("summary" in e for e in errors)

    def test_empty_summary(self):
        data = _valid_handoff()
        data["summary"] = "   "
        errors = validate_dev_handoff(data)
        assert any("summary" in e for e in errors)

    def test_missing_commits(self):
        data = _valid_handoff()
        del data["commits"]
        errors = validate_dev_handoff(data)
        assert any("commits" in e for e in errors)

    def test_empty_commits(self):
        data = _valid_handoff()
        data["commits"] = []
        errors = validate_dev_handoff(data)
        assert any("commits" in e and "non-empty" in e for e in errors)

    def test_commits_not_list(self):
        data = _valid_handoff()
        data["commits"] = "abc1234"
        errors = validate_dev_handoff(data)
        assert any("commits" in e and "list" in e for e in errors)

    def test_commit_missing_sha(self):
        data = _valid_handoff()
        data["commits"] = [{"message": "feat: thing"}]
        errors = validate_dev_handoff(data)
        assert any("sha" in e for e in errors)

    def test_commit_missing_message(self):
        data = _valid_handoff()
        data["commits"] = [{"sha": "abc1234"}]
        errors = validate_dev_handoff(data)
        assert any("message" in e for e in errors)

    def test_commit_non_dict(self):
        data = _valid_handoff()
        data["commits"] = ["abc1234 feat: thing"]
        errors = validate_dev_handoff(data)
        assert any("mapping" in e for e in errors)

    def test_missing_acceptance_criteria(self):
        data = _valid_handoff()
        del data["acceptance_criteria"]
        errors = validate_dev_handoff(data)
        assert any("acceptance_criteria" in e for e in errors)

    def test_empty_acceptance_criteria(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = []
        errors = validate_dev_handoff(data)
        assert any("acceptance_criteria" in e and "non-empty" in e for e in errors)

    def test_acceptance_criteria_not_list(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = "all met"
        errors = validate_dev_handoff(data)
        assert any("acceptance_criteria" in e and "list" in e for e in errors)

    def test_ac_missing_criterion(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = [{"status": "MET", "notes": "done"}]
        errors = validate_dev_handoff(data)
        assert any("criterion" in e for e in errors)

    def test_ac_invalid_status(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = [{"criterion": "AC 1", "status": "DONE", "notes": "done"}]
        errors = validate_dev_handoff(data)
        assert any("status" in e for e in errors)

    def test_ac_missing_notes(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = [{"criterion": "AC 1", "status": "MET"}]
        errors = validate_dev_handoff(data)
        assert any("notes" in e for e in errors)

    def test_ac_non_dict(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = ["AC 1: MET"]
        errors = validate_dev_handoff(data)
        assert any("mapping" in e for e in errors)

    def test_missing_gate_result(self):
        data = _valid_handoff()
        del data["gate_result"]
        errors = validate_dev_handoff(data)
        assert errors == []

    def test_invalid_gate_result(self):
        data = _valid_handoff()
        data["gate_result"] = "SUCCESS"
        errors = validate_dev_handoff(data)
        assert any("gate_result" in e for e in errors)

    def test_missing_story_deviations(self):
        data = _valid_handoff()
        del data["story_deviations"]
        errors = validate_dev_handoff(data)
        assert any("story_deviations" in e for e in errors)

    def test_missing_deferred_items(self):
        data = _valid_handoff()
        del data["deferred_items"]
        errors = validate_dev_handoff(data)
        assert any("deferred_items" in e for e in errors)

    # ── story_deviations / deferred_items existing behavior ─────────

    def test_story_deviations_invalid_string(self):
        data = _valid_handoff()
        data["story_deviations"] = "some random text"
        errors = validate_dev_handoff(data)
        assert any("story_deviations" in e for e in errors)

    def test_deferred_items_invalid_string(self):
        data = _valid_handoff()
        data["deferred_items"] = "some random text"
        errors = validate_dev_handoff(data)
        assert any("deferred_items" in e for e in errors)

    def test_story_deviations_invalid_type(self):
        data = _valid_handoff()
        data["story_deviations"] = 42
        errors = validate_dev_handoff(data)
        assert any("story_deviations" in e for e in errors)

    def test_deferred_items_invalid_type(self):
        data = _valid_handoff()
        data["deferred_items"] = True
        errors = validate_dev_handoff(data)
        assert any("deferred_items" in e for e in errors)

    def test_deviation_missing_description(self):
        data = _valid_handoff()
        data["story_deviations"] = [{"justification": "reason"}]
        errors = validate_dev_handoff(data)
        assert any("description" in e for e in errors)

    def test_deviation_missing_justification(self):
        data = _valid_handoff()
        data["story_deviations"] = [{"description": "what"}]
        errors = validate_dev_handoff(data)
        assert any("justification" in e for e in errors)

    def test_deferred_missing_description(self):
        data = _valid_handoff()
        data["deferred_items"] = [{"reason": "why"}]
        errors = validate_dev_handoff(data)
        assert any("description" in e for e in errors)

    def test_deferred_missing_reason(self):
        data = _valid_handoff()
        data["deferred_items"] = [{"description": "what"}]
        errors = validate_dev_handoff(data)
        assert any("reason" in e for e in errors)

    def test_non_dict_root(self):
        errors = validate_dev_handoff("just a string")
        assert len(errors) == 1
        assert "mapping" in errors[0]

    def test_non_dict_deviation_item(self):
        data = _valid_handoff()
        data["story_deviations"] = ["not a dict"]
        errors = validate_dev_handoff(data)
        assert any("mapping" in e for e in errors)

    def test_non_dict_deferred_item(self):
        data = _valid_handoff()
        data["deferred_items"] = ["not a dict"]
        errors = validate_dev_handoff(data)
        assert any("mapping" in e for e in errors)

    def test_story_deviations_none_case_insensitive(self):
        data = _valid_handoff()
        data["story_deviations"] = "None"
        errors = validate_dev_handoff(data)
        assert errors == []

    def test_deferred_items_none_case_insensitive(self):
        data = _valid_handoff()
        data["deferred_items"] = "NONE"
        errors = validate_dev_handoff(data)
        assert errors == []

    # ── non-string leaf values rejected ────────────────────────────

    def test_commit_sha_non_string_rejected(self):
        data = _valid_handoff()
        data["commits"] = [{"sha": 123, "message": "feat: thing"}]
        errors = validate_dev_handoff(data)
        assert any("sha" in e and "string" in e for e in errors)

    def test_commit_message_non_string_rejected(self):
        data = _valid_handoff()
        data["commits"] = [{"sha": "abc", "message": True}]
        errors = validate_dev_handoff(data)
        assert any("message" in e and "string" in e for e in errors)

    def test_ac_criterion_non_string_rejected(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = [{"criterion": 99, "status": "MET", "notes": "ok"}]
        errors = validate_dev_handoff(data)
        assert any("criterion" in e and "string" in e for e in errors)

    def test_ac_notes_non_string_rejected(self):
        data = _valid_handoff()
        data["acceptance_criteria"] = [{"criterion": "AC 1", "status": "MET", "notes": 42}]
        errors = validate_dev_handoff(data)
        assert any("notes" in e and "string" in e for e in errors)

    def test_deviation_description_non_string_rejected(self):
        data = _valid_handoff()
        data["story_deviations"] = [{"description": 123, "justification": "reason"}]
        errors = validate_dev_handoff(data)
        assert any("description" in e and "string" in e for e in errors)

    def test_deviation_justification_non_string_rejected(self):
        data = _valid_handoff()
        data["story_deviations"] = [{"description": "what", "justification": False}]
        errors = validate_dev_handoff(data)
        assert any("justification" in e and "string" in e for e in errors)

    def test_deferred_description_non_string_rejected(self):
        data = _valid_handoff()
        data["deferred_items"] = [{"description": 0, "reason": "why"}]
        errors = validate_dev_handoff(data)
        assert any("description" in e and "string" in e for e in errors)

    def test_deferred_reason_non_string_rejected(self):
        data = _valid_handoff()
        data["deferred_items"] = [{"description": "what", "reason": 99}]
        errors = validate_dev_handoff(data)
        assert any("reason" in e and "string" in e for e in errors)


# ── Parsing ───────────────────────────────────────────────────────────

_VALID_YAML = (
    'summary: "Implemented the thing."\n'
    "commits:\n"
    '  - sha: "abc1234"\n'
    '    message: "feat(x): implement the thing"\n'
    "acceptance_criteria:\n"
    '  - criterion: "It works"\n'
    "    status: MET\n"
    '    notes: "Tested"\n'
    "spec_deviations: none\n"
    "deferred_items: none\n"
    "gate_result: PASS\n"
)


class TestParseDevHandoff:
    def test_parse_valid_yaml(self):
        result = parse_dev_handoff(_VALID_YAML)
        assert result.parse_errors == []
        assert result.summary == "Implemented the thing."
        assert len(result.commits) == 1
        assert result.commits[0]["sha"] == "abc1234"
        assert result.commits[0]["message"] == "feat(x): implement the thing"
        assert len(result.acceptance_criteria) == 1
        assert result.acceptance_criteria[0]["criterion"] == "It works"
        assert result.acceptance_criteria[0]["status"] == "MET"
        assert result.story_deviations == []
        assert result.deferred_items == []
        assert result.gate_result == "PASS"

    def test_parse_fenced_yaml(self):
        text = f"```yaml\n{_VALID_YAML}```\n"
        result = parse_dev_handoff(text)
        assert result.parse_errors == []
        assert result.summary == "Implemented the thing."

    def test_parse_with_deviations(self):
        text = (
            'summary: "Implemented feature."\n'
            "commits:\n"
            '  - sha: "a1b2c3"\n'
            '    message: "feat: thing"\n'
            "acceptance_criteria:\n"
            '  - criterion: "Works"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "spec_deviations:\n"
            '  - description: "Used different module"\n'
            '    justification: "Better encapsulation"\n'
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )
        result = parse_dev_handoff(text)
        assert result.parse_errors == []
        assert len(result.story_deviations) == 1
        assert result.story_deviations[0]["description"] == "Used different module"
        assert result.story_deviations[0]["justification"] == "Better encapsulation"

    def test_parse_with_deferred(self):
        text = (
            'summary: "Implemented feature."\n'
            "commits:\n"
            '  - sha: "a1b2c3"\n'
            '    message: "feat: thing"\n'
            "acceptance_criteria:\n"
            '  - criterion: "Works"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "spec_deviations: none\n"
            "deferred_items:\n"
            '  - description: "Caching"\n'
            '    reason: "Not in scope"\n'
            "gate_result: PASS\n"
        )
        result = parse_dev_handoff(text)
        assert result.parse_errors == []
        assert len(result.deferred_items) == 1
        assert result.deferred_items[0]["description"] == "Caching"
        assert result.deferred_items[0]["reason"] == "Not in scope"

    def test_parse_garbage(self):
        result = parse_dev_handoff("this is not yaml: [[[")
        assert len(result.parse_errors) > 0

    def test_parse_non_dict(self):
        result = parse_dev_handoff("- just\n- a\n- list\n")
        assert len(result.parse_errors) > 0
        assert "mapping" in result.parse_errors[0]

    def test_parse_missing_required_fields(self):
        text = 'summary: "ok"\n'
        result = parse_dev_handoff(text)
        assert len(result.parse_errors) > 0
        assert any("commits" in e for e in result.parse_errors)
        assert any("acceptance_criteria" in e for e in result.parse_errors)

    def test_parse_partial_criteria(self):
        text = (
            'summary: "Partial work."\n'
            "commits:\n"
            '  - sha: "abc"\n'
            '    message: "wip"\n'
            "acceptance_criteria:\n"
            '  - criterion: "AC 1"\n'
            "    status: MET\n"
            '    notes: "done"\n'
            '  - criterion: "AC 2"\n'
            "    status: NOT_MET\n"
            '    notes: "blocked"\n'
            "spec_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: FAIL\n"
        )
        result = parse_dev_handoff(text)
        assert result.parse_errors == []
        assert result.gate_result == "FAIL"
        assert len(result.acceptance_criteria) == 2
        assert result.acceptance_criteria[1]["status"] == "NOT_MET"


# ── Formatting ────────────────────────────────────────────────────────


class TestDevHandoffToReviewerText:
    def test_full_handoff(self):
        handoff = DevHandoff(
            summary="Implemented X.",
            commits=[{"sha": "abc1234", "message": "feat(x): implement X"}],
            acceptance_criteria=[
                {"criterion": "X works", "status": "MET", "notes": "Tested"},
            ],
            story_deviations=[
                {"description": "Moved to util", "justification": "Shared by 3 modules"}
            ],
            deferred_items=[{"description": "Caching", "reason": "Out of scope"}],
            gate_result="PASS",
            parse_errors=[],
            raw={},
        )
        text = dev_handoff_to_reviewer_text(handoff)
        assert "Implemented X." in text
        assert "Gate Result:** PASS" in text
        assert "`abc1234`" in text
        assert "feat(x): implement X" in text
        assert "**[MET]** X works" in text
        assert "Tested" in text
        assert "Story Deviations" in text
        assert "Moved to util" in text
        assert "Shared by 3 modules" in text
        assert "Deferred Items" in text
        assert "Caching" in text
        assert "Out of scope" in text

    def test_no_deviations_no_deferred(self):
        handoff = DevHandoff(
            summary="Implemented the thing.",
            commits=[{"sha": "abc", "message": "feat: thing"}],
            acceptance_criteria=[
                {"criterion": "Works", "status": "MET", "notes": "yes"},
            ],
            story_deviations=[],
            deferred_items=[],
            gate_result="PASS",
            parse_errors=[],
            raw={},
        )
        text = dev_handoff_to_reviewer_text(handoff)
        assert "Implemented the thing." in text
        assert "Commits" in text
        assert "Acceptance Criteria" in text
        assert "Story Deviations" not in text
        assert "Deferred Items" not in text

    def test_empty_handoff_returns_empty(self):
        handoff = DevHandoff(
            summary="",
            commits=[],
            acceptance_criteria=[],
            story_deviations=[],
            deferred_items=[],
            gate_result="",
            parse_errors=[],
            raw={},
        )
        text = dev_handoff_to_reviewer_text(handoff)
        assert text == ""

    def test_multiple_criteria_statuses(self):
        handoff = DevHandoff(
            summary="Partial implementation.",
            commits=[{"sha": "abc", "message": "wip"}],
            acceptance_criteria=[
                {"criterion": "AC 1", "status": "MET", "notes": "done"},
                {"criterion": "AC 2", "status": "PARTIAL", "notes": "mostly"},
                {"criterion": "AC 3", "status": "NOT_MET", "notes": "deferred"},
            ],
            story_deviations=[],
            deferred_items=[],
            gate_result="FAIL",
            parse_errors=[],
            raw={},
        )
        text = dev_handoff_to_reviewer_text(handoff)
        assert "**[MET]** AC 1" in text
        assert "**[PARTIAL]** AC 2" in text
        assert "**[NOT_MET]** AC 3" in text
        assert "Gate Result:** FAIL" in text
