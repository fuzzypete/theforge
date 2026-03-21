"""Unit tests for story_validator.py."""

from unittest.mock import patch

from theforge.config import DEFAULT_DEV_PROFILE
from theforge.runner import AgentResult
from theforge.story_validator import (
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


PASS_OUTPUT = """\
```yaml
verdict: PASS
findings: []
```
"""

WARN_REQUIREMENT_OUTPUT = """\
```yaml
verdict: WARN
findings:
  - category: requirement
    description: "Requirement 3 says no overwrites but AC says plan.txt (fixed name)"
    split_suggestion: null
```
"""

WARN_SCOPE_OUTPUT = """\
```yaml
verdict: WARN
findings:
  - category: scope
    description: "Spec covers iOS native, React UI, and watchOS — three independent subsystems"
    split_suggestion:
      stories:
        - name: "Story: iOS native workout view"
          acs:
            - "User can start a workout from the iPhone app"
        - name: "Story: React UI dashboard"
          acs:
            - "User can view workout history in the web dashboard"
        - name: "Story: watchOS timer"
          acs:
            - "User can start a rest timer from Apple Watch"
```
"""

MALFORMED_OUTPUT = """\
Here is my analysis of the spec. It looks fine overall but I noticed some things.
The requirements seem consistent with the acceptance criteria.
"""


# ── Tests: _extract_yaml_block ────────────────────────────────────────


def test_extract_yaml_block_standard():
    text = "Some preamble\n```yaml\nverdict: PASS\nfindings: []\n```\nSome postamble"
    result = _extract_yaml_block(text)
    assert result == "verdict: PASS\nfindings: []"


def test_extract_yaml_block_case_insensitive():
    text = "```YAML\nverdict: PASS\nfindings: []\n```"
    result = _extract_yaml_block(text)
    assert result is not None


def test_extract_yaml_block_none_on_no_match():
    result = _extract_yaml_block("No YAML here at all.")
    assert result is None


# ── Tests: _parse_validation_output ──────────────────────────────────


def test_parse_pass_verdict():
    result = _parse_validation_output(PASS_OUTPUT)
    assert result.verdict == "PASS"
    assert result.findings == []


def test_parse_warn_requirement_finding():
    result = _parse_validation_output(WARN_REQUIREMENT_OUTPUT)
    assert result.verdict == "WARN"
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.category == "requirement"
    assert "Requirement 3" in f.description
    assert f.split_suggestion is None


def test_parse_warn_scope_finding_with_split():
    result = _parse_validation_output(WARN_SCOPE_OUTPUT)
    assert result.verdict == "WARN"
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.category == "scope"
    assert f.split_suggestion is not None
    stories = f.split_suggestion["stories"]
    assert len(stories) == 3
    assert stories[0]["name"] == "Story: iOS native workout view"


def test_parse_malformed_output_falls_back_to_pass():
    """Any parse failure must be fail-safe: return PASS."""
    result = _parse_validation_output(MALFORMED_OUTPUT)
    assert result.verdict == "PASS"
    assert result.findings == []


def test_parse_empty_string_falls_back_to_pass():
    result = _parse_validation_output("")
    assert result.verdict == "PASS"


def test_parse_invalid_verdict_falls_back_to_pass():
    output = "```yaml\nverdict: MAYBE\nfindings: []\n```"
    result = _parse_validation_output(output)
    assert result.verdict == "PASS"


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


# ── Tests: _make_fast_profile ─────────────────────────────────────────


def test_make_fast_profile_opus_substituted():
    """opus model should be replaced with sonnet."""
    import dataclasses

    profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="opus")
    fast = _make_fast_profile(profile)
    assert fast.model == "sonnet"


def test_make_fast_profile_opus_full_id_substituted():
    """Full model ID containing 'opus' should be replaced with sonnet."""
    import dataclasses

    profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="claude-opus-4-6")
    fast = _make_fast_profile(profile)
    assert fast.model == "sonnet"


def test_make_fast_profile_sonnet_passthrough():
    """Non-opus model should pass through unchanged."""
    import dataclasses

    profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="sonnet")
    fast = _make_fast_profile(profile)
    assert fast.model == "sonnet"


def test_make_fast_profile_does_not_mutate_original():
    """Original profile must be unchanged (frozen dataclass)."""
    import dataclasses

    profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="opus")
    fast = _make_fast_profile(profile)
    assert profile.model == "opus"
    assert fast.model == "sonnet"


# ── Tests: validate_story (integration with mocked run_agent) ──────────


@patch("theforge.story_validator.run_agent")
def test_validate_story_pass(mock_run_agent, tmp_path):
    """PASS verdict from model → result.verdict == PASS with no findings."""
    mock_run_agent.return_value = _make_agent_result(PASS_OUTPUT, cost_usd=0.005)

    result = validate_story(
        story_content="# Spec\n\n## AC\n- [ ] Feature works",
        profile=DEFAULT_DEV_PROFILE,
        working_dir=tmp_path,
    )

    assert result.verdict == "PASS"
    assert result.findings == []
    assert result.cost_usd == 0.005
    assert result.duration_s is not None
    mock_run_agent.assert_called_once()


@patch("theforge.story_validator.run_agent")
def test_validate_story_warn_requirement(mock_run_agent, tmp_path):
    """WARN with requirement finding → finding propagated."""
    mock_run_agent.return_value = _make_agent_result(WARN_REQUIREMENT_OUTPUT, cost_usd=0.008)

    result = validate_story(
        story_content="# Spec\n\nSome spec content",
        profile=DEFAULT_DEV_PROFILE,
        working_dir=tmp_path,
    )

    assert result.verdict == "WARN"
    assert len(result.findings) == 1
    assert result.findings[0].category == "requirement"


@patch("theforge.story_validator.run_agent")
def test_validate_story_warn_scope_with_split(mock_run_agent, tmp_path):
    """WARN with scope finding including split_suggestion."""
    mock_run_agent.return_value = _make_agent_result(WARN_SCOPE_OUTPUT, cost_usd=0.010)

    result = validate_story(
        story_content="# Big Spec\n\nLots of ACs",
        profile=DEFAULT_DEV_PROFILE,
        working_dir=tmp_path,
    )

    assert result.verdict == "WARN"
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.category == "scope"
    assert f.split_suggestion is not None
    assert len(f.split_suggestion["stories"]) == 3


@patch("theforge.story_validator.run_agent")
def test_validate_story_malformed_output_is_pass(mock_run_agent, tmp_path):
    """Malformed model output → fail-safe PASS, run is not blocked."""
    mock_run_agent.return_value = _make_agent_result(MALFORMED_OUTPUT)

    result = validate_story(
        story_content="# Spec",
        profile=DEFAULT_DEV_PROFILE,
        working_dir=tmp_path,
    )

    assert result.verdict == "PASS"
    assert result.findings == []


@patch("theforge.story_validator.run_agent")
def test_validate_story_agent_exception_is_pass(mock_run_agent, tmp_path):
    """Exception from run_agent → fail-safe PASS."""
    mock_run_agent.side_effect = RuntimeError("agent failed")

    result = validate_story(
        story_content="# Spec",
        profile=DEFAULT_DEV_PROFILE,
        working_dir=tmp_path,
    )

    assert result.verdict == "PASS"


@patch("theforge.story_validator.run_agent")
def test_validate_story_uses_fast_profile_for_opus(mock_run_agent, tmp_path):
    """Opus dev profile → model substituted to sonnet before calling run_agent."""
    import dataclasses

    mock_run_agent.return_value = _make_agent_result(PASS_OUTPUT)
    opus_profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="opus")

    validate_story(
        story_content="# Spec",
        profile=opus_profile,
        working_dir=tmp_path,
    )

    called_profile = mock_run_agent.call_args.kwargs["profile"]
    assert called_profile.model == "sonnet"


@patch("theforge.story_validator.run_agent")
def test_validate_story_passthrough_non_opus(mock_run_agent, tmp_path):
    """Non-opus profile model is passed through unchanged."""
    import dataclasses

    mock_run_agent.return_value = _make_agent_result(PASS_OUTPUT)
    sonnet_profile = dataclasses.replace(DEFAULT_DEV_PROFILE, model="sonnet")

    validate_story(
        story_content="# Spec",
        profile=sonnet_profile,
        working_dir=tmp_path,
    )

    called_profile = mock_run_agent.call_args.kwargs["profile"]
    assert called_profile.model == "sonnet"
