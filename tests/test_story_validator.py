"""Unit tests for story_validator.py."""

from unittest.mock import patch

from theforge.config import DEFAULT_DEV_PROFILE
from theforge.runners import AgentResult
from theforge.story_validator import (
    StoryValidationFinding,
    StoryValidationResult,
    _extract_yaml_block,
    _make_fast_profile,
    _parse_validation_output,
    validate_story,
)

# ── Helper ────────────────────────────────────────────────────────────


def _make_agent_result(output: str, cost_usd: float = 0.01) -> AgentResult:
    return AgentResult(
        success=True,
        output=output,
        session_id=None,
        cost_usd=cost_usd,
        exit_code=0,
        raw={},
        profile_name="spec-validator",
    )


# ── Tests: validate_story ─────────────────────────────────────────────


def test_validate_story_pass():
    output = "```yaml\nverdict: PASS\nfindings: []\n```"
    with patch("theforge.story_validator.run_agent") as mock_run:
        mock_run.return_value = _make_agent_result(output, cost_usd=0.005)
        result = validate_story("story content", DEFAULT_DEV_PROFILE, "working_dir")
        assert result.verdict == "PASS"
        assert len(result.findings) == 0
        assert result.cost_usd == 0.005
        assert result.duration_s is not None
        mock_run.assert_called_once()


def test_validate_story_warn():
    output = """
```yaml
verdict: WARN
findings:
  - category: contradiction
    description: "Requirement A contradicts AC 1"
```
"""
    with patch("theforge.story_validator.run_agent") as mock_run:
        mock_run.return_value = _make_agent_result(output)
        result = validate_story("story content", DEFAULT_DEV_PROFILE, "working_dir")
        assert result.verdict == "WARN"
        assert len(result.findings) == 1
        assert result.findings[0].category == "requirement"


def test_validate_story_fail_safe_on_exception():
    with patch("theforge.story_validator.run_agent") as mock_run:
        mock_run.side_effect = Exception("API error")
        result = validate_story("story content", DEFAULT_DEV_PROFILE, "working_dir")
        assert result.verdict == "PASS"
        assert len(result.findings) == 0


# ── Tests: _extract_yaml_block ───────────────────────────────────────


def test_extract_yaml_block_found():
    text = "Some text before\n```yaml\nkey: value\n```\nSome text after"
    assert _extract_yaml_block(text) == "key: value"


def test_extract_yaml_block_not_found():
    assert _extract_yaml_block("No block here") is None


def test_extract_yaml_block_case_insensitive():
    text = "```YAML\nverdict: PASS\nfindings: []\n```"
    result = _extract_yaml_block(text)
    assert result is not None


# ── Tests: _parse_validation_output ──────────────────────────────────


def test_parse_valid_yaml():
    output = "```yaml\nverdict: PASS\nfindings: []\n```"
    result = _parse_validation_output(output)
    assert result.verdict == "PASS"
    assert len(result.findings) == 0


def test_parse_findings():
    output = """
```yaml
verdict: WARN
findings:
  - category: scope
    description: "Too many ACs"
    split_suggestion:
      stories:
        - name: "Part 1"
          description: "Desc 1"
```
"""
    result = _parse_validation_output(output)
    assert result.verdict == "WARN"
    assert len(result.findings) == 1
    assert result.findings[0].category == "scope"
    assert result.findings[0].split_suggestion["stories"][0]["name"] == "Part 1"


def test_parse_garbage_falls_back_to_pass():
    result = _parse_validation_output("This is not YAML at all.")
    assert result.verdict == "PASS"


def test_parse_empty_string_falls_back_to_pass():
    result = _parse_validation_output("")
    assert result.verdict == "PASS"


def test_parse_invalid_verdict_falls_back_to_pass():
    output = "```yaml\nverdict: MAYBE\nfindings: []\n```"
    result = _parse_validation_output(output)
    assert result.verdict == "PASS"


def test_parse_fenced_non_dict_yaml_falls_back_to_pass():
    output = "```yaml\n- not\n- a dict\n```"
    result = _parse_validation_output(output)
    assert result.verdict == "PASS"


def test_parse_lowercase_verdict_normalized():
    # [P1] Regression test
    output = "```yaml\nverdict: warn\nfindings: []\n```"
    result = _parse_validation_output(output)
    assert result.verdict == "WARN"


def test_parse_tolerant_fence_trailing_space():
    # [P2] Regression test
    output = "```yaml  \nverdict: WARN\nfindings: []\n```"
    result = _parse_validation_output(output)
    assert result.verdict == "WARN"


def test_parse_bare_yaml_fallback():
    # [P2] Bare YAML fallback test
    output = "verdict: WARN\nfindings: []"
    result = _parse_validation_output(output)
    assert result.verdict == "WARN"


def test_parse_verdict_pattern_fallback():
    # [P2] Last-resort pattern fallback
    output = "Some chatter...\nverdict: warn\nMore chatter..."
    result = _parse_validation_output(output)
    assert result.verdict == "WARN"


def test_parse_unknown_category_coerced_to_requirement():
    output = """\
```yaml
verdict: WARN
findings:
  - category: unknown_type
    description: "Some issue"
    split_suggestion: null
```
"""
    result = _parse_validation_output(output)
    assert result.verdict == "WARN"
    assert result.findings[0].category == "requirement"


def test_parse_category_normalization():
    # [P2] Regression test
    output = """\
```yaml
verdict: WARN
findings:
  - category: Scope
    description: "Case test"
```
"""
    result = _parse_validation_output(output)
    assert result.findings[0].category == "scope"


def test_parse_expanded_requirement_category_maps_to_requirement():
    output = """\
```yaml
verdict: WARN
findings:
  - category: Contradiction
    description: "Case test"
```
"""
    result = _parse_validation_output(output)
    assert result.findings[0].category == "requirement"


def test_parse_malformed_split_suggestion_handled():
    # [P1] Regression test: scalar instead of dict
    output = """\
```yaml
verdict: WARN
findings:
  - category: scope
    description: "Malformed split"
    split_suggestion: "should be a dict"
```
"""
    result = _parse_validation_output(output)
    assert result.findings[0].split_suggestion is None


def test_story_validation_finding_normalizes_invalid_fields():
    finding = StoryValidationFinding(
        category="UNKNOWN",
        description=123,
        split_suggestion=["not", "a", "dict"],
    )

    assert finding.category == "requirement"
    assert finding.description == "123"
    assert finding.split_suggestion is None


def test_story_validation_result_coerces_dict_findings():
    result = StoryValidationResult(
        verdict="warn",
        findings=[
            {
                "category": "scope",
                "description": "Split this story",
                "split_suggestion": {"stories": [{"name": "A"}]},
            }
        ],
    )

    assert result.verdict == "WARN"
    assert len(result.findings) == 1
    assert isinstance(result.findings[0], StoryValidationFinding)
    assert result.findings[0].category == "scope"


@patch("theforge.story_validator.run_agent")
def test_validate_story_uses_fast_profile_for_opus(mock_run, tmp_path):
    """Opus dev profile should be replaced with sonnet before invoking the runner."""
    import dataclasses

    mock_run.return_value = _make_agent_result("```yaml\nverdict: PASS\nfindings: []\n```")
    opus_profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="opus")

    validate_story(
        story_content="# Spec",
        profile=opus_profile,
        working_dir=tmp_path,
    )

    called_profile = mock_run.call_args.kwargs["profile"]
    assert called_profile.model == "sonnet"


def test_make_fast_profile_opus_full_id_substituted():
    """Full model IDs containing opus should be replaced with sonnet."""
    import dataclasses

    profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="claude-opus-4-6")
    fast = _make_fast_profile(profile)
    assert fast.model == "sonnet"


def test_make_fast_profile_does_not_mutate_original():
    """Original profile must be preserved."""
    import dataclasses

    profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="opus")
    fast = _make_fast_profile(profile)
    assert profile.model == "opus"
    assert fast.model == "sonnet"


# ── Tests: _make_fast_profile ─────────────────────────────────────────


def test_make_fast_profile_opus_substituted():
    """opus model should be replaced with sonnet."""
    from theforge.config import ModelProfile

    profile = ModelProfile(
        name="test",
        cli="claude",
        model="opus-something",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=(),
    )
    fast = _make_fast_profile(profile)
    assert "sonnet" in fast.model


def test_make_fast_profile_non_opus_preserved():
    """non-opus models should be left alone."""
    from theforge.config import ModelProfile

    profile = ModelProfile(
        name="test",
        cli="claude",
        model="sonnet-something",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=(),
    )
    fast = _make_fast_profile(profile)
    assert fast.model == "sonnet-something"
