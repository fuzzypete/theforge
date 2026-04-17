"""Unit tests for theforge.task.handoff_parser."""

import pytest

from theforge.task.handoff_parser import ParseError, extract_dev_handoff

VALID_BLOCK = """\
<forge_handoff>
summary: "Implemented the feature."
commits:
  - sha: "abc1234"
    message: "feat: implement it"
acceptance_criteria:
  - criterion: "Does the thing"
    status: MET
    notes: "verified"
story_deviations: none
deferred_items: none
</forge_handoff>"""


def _make_output(block: str, before: str = "", after: str = "") -> str:
    return f"{before}\n{block}\n{after}"


# ── Happy path ────────────────────────────────────────────────────────


def test_valid_block_with_prose_before_and_after():
    text = _make_output(VALID_BLOCK, before="Some prose here.", after="More text after.")
    result = extract_dev_handoff(text)
    assert result is not None
    assert result["summary"] == "Implemented the feature."
    assert isinstance(result["commits"], list)
    assert result["commits"][0]["sha"] == "abc1234"
    assert isinstance(result["acceptance_criteria"], list)
    assert result["story_deviations"] == "none"
    assert result["deferred_items"] == "none"


def test_no_block_returns_none():
    text = "There is no forge_handoff block here."
    result = extract_dev_handoff(text)
    assert result is None


def test_empty_text_returns_none():
    assert extract_dev_handoff("") is None


# ── Delimiter in fenced code block is ignored ─────────────────────────


def test_delimiter_inside_code_fence_is_ignored():
    text = (
        "The spec mentions the format:\n"
        "```\n"
        "<forge_handoff>\n"
        "summary: quoted example\n"
        "commits: []\n"
        "acceptance_criteria: []\n"
        "story_deviations: none\n"
        "deferred_items: none\n"
        "</forge_handoff>\n"
        "```\n"
        "That was just an example. No real block here."
    )
    result = extract_dev_handoff(text)
    assert result is None


def test_real_block_after_code_fence_example():
    text = (
        "Here's how it looks:\n```\n<forge_handoff>ignore me</forge_handoff>\n```\n" + VALID_BLOCK
    )
    result = extract_dev_handoff(text)
    assert result is not None
    assert result["summary"] == "Implemented the feature."


# ── Parse errors ──────────────────────────────────────────────────────


def test_malformed_yaml_raises_parse_error():
    text = "<forge_handoff>\n{invalid: yaml: :\n</forge_handoff>"
    with pytest.raises(ParseError, match="YAML parse error"):
        extract_dev_handoff(text)


def test_truncated_block_no_closing_tag():
    text = "<forge_handoff>\nsummary: incomplete"
    with pytest.raises(ParseError, match="without closing"):
        extract_dev_handoff(text)


def test_closing_tag_without_opening():
    text = "some text </forge_handoff>"
    with pytest.raises(ParseError, match="without opening"):
        extract_dev_handoff(text)


def test_missing_required_key_raises_parse_error():
    text = (
        "<forge_handoff>\n"
        "summary: 'Missing commits and other keys.'\n"
        "commits: []\n"
        # acceptance_criteria missing
        "story_deviations: none\n"
        "deferred_items: none\n"
        "</forge_handoff>"
    )
    with pytest.raises(ParseError, match="missing required keys"):
        extract_dev_handoff(text)


def test_wrong_type_commits_string_raises_parse_error():
    text = (
        "<forge_handoff>\n"
        "summary: 'OK'\n"
        "commits: 'not a list'\n"
        "acceptance_criteria: []\n"
        "story_deviations: none\n"
        "deferred_items: none\n"
        "</forge_handoff>"
    )
    with pytest.raises(ParseError, match="commits must be a list"):
        extract_dev_handoff(text)


def test_wrong_type_acceptance_criteria_string_raises_parse_error():
    text = (
        "<forge_handoff>\n"
        "summary: 'OK'\n"
        "commits: []\n"
        "acceptance_criteria: 'should be a list'\n"
        "story_deviations: none\n"
        "deferred_items: none\n"
        "</forge_handoff>"
    )
    with pytest.raises(ParseError, match="acceptance_criteria must be a list"):
        extract_dev_handoff(text)


def test_wrong_type_summary_not_string_raises_parse_error():
    text = (
        "<forge_handoff>\n"
        "summary: 42\n"
        "commits: []\n"
        "acceptance_criteria: []\n"
        "story_deviations: none\n"
        "deferred_items: none\n"
        "</forge_handoff>"
    )
    with pytest.raises(ParseError, match="summary must be a string"):
        extract_dev_handoff(text)


def test_multiple_blocks_raises_parse_error():
    block2 = VALID_BLOCK
    text = VALID_BLOCK + "\n\nSome text\n\n" + block2
    with pytest.raises(ParseError, match="2 <forge_handoff>"):
        extract_dev_handoff(text)


def test_empty_block_raises_parse_error():
    text = "<forge_handoff>\n</forge_handoff>"
    with pytest.raises(ParseError, match="empty"):
        extract_dev_handoff(text)


def test_empty_block_whitespace_only_raises_parse_error():
    text = "<forge_handoff>\n   \n</forge_handoff>"
    with pytest.raises(ParseError, match="empty"):
        extract_dev_handoff(text)


def test_non_mapping_yaml_raises_parse_error():
    text = "<forge_handoff>\n- item1\n- item2\n</forge_handoff>"
    with pytest.raises(ParseError, match="must be a YAML mapping"):
        extract_dev_handoff(text)


# ── Edge cases ────────────────────────────────────────────────────────


def test_empty_lists_are_valid():
    text = (
        "<forge_handoff>\n"
        "summary: 'Minimal valid block.'\n"
        "commits: []\n"
        "acceptance_criteria: []\n"
        "story_deviations: none\n"
        "deferred_items: none\n"
        "</forge_handoff>"
    )
    result = extract_dev_handoff(text)
    assert result is not None
    assert result["commits"] == []
    assert result["acceptance_criteria"] == []


def test_extra_keys_are_allowed():
    text = (
        "<forge_handoff>\n"
        "summary: 'Has extra fields.'\n"
        "commits: []\n"
        "acceptance_criteria: []\n"
        "story_deviations: none\n"
        "deferred_items: none\n"
        "extra_field: allowed\n"
        "</forge_handoff>"
    )
    result = extract_dev_handoff(text)
    assert result is not None
    assert result["extra_field"] == "allowed"
