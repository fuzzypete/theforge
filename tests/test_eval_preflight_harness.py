"""Unit tests for the preflight evaluation harness and report generator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.agent_types import AgentResult
from theforge.eval.golden_set import GoldenStory, load_golden_set
from theforge.eval.preflight_harness import RunMetrics, run_preflight_for_model
from theforge.eval.preflight_report import generate_report, score_model

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_story(
    story_id: str = "test-1",
    expected_verdict: str = "PROCEED",
    expected_complexity: str | None = "small",
) -> GoldenStory:
    return GoldenStory(
        id=story_id,
        title="Test story",
        story_text="## What\nSome work.\n\n## Acceptance criteria\n- AC1",
        expected_verdict=expected_verdict,
        expected_complexity=expected_complexity,
    )


def _make_profile(model: str = "deepseek-reasoner") -> MagicMock:
    profile = MagicMock()
    profile.model = model
    profile.phase = "preflight"
    return profile


def _make_config() -> MagicMock:
    config = MagicMock()
    config.secrets = {}
    return config


def _make_agent_result(
    success: bool = True,
    output: str = "",
    cost_usd: float | None = None,
    exit_code: int = 0,
    raw: dict | None = None,
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=cost_usd,
        exit_code=exit_code,
        raw=raw or {},
    )


VALID_PROCEED_OUTPUT = """
```yaml
verdict: PROCEED
complexity: small
work_type: feature
contract_change: false
reason: "The feature is not yet implemented."
sufficiency: implementation_ready
spec_issues: []
warnings: []
likely_files: ["src/theforge/foo.py"]
criteria_checked:
  - criterion: "AC1"
    status: NOT_MET
    evidence: "No matching code found in src/theforge/foo.py:42"
```
"""

VALID_ALREADY_DONE_OUTPUT = """
```yaml
verdict: ALREADY_DONE
complexity: small
work_type: feature
contract_change: false
reason: "Already implemented in src/theforge/bar.py"
sufficiency: implementation_ready
spec_issues: []
warnings: []
likely_files: ["src/theforge/bar.py"]
criteria_checked:
  - criterion: "AC1"
    status: MET
    evidence: "Found at src/theforge/bar.py:10"
```
"""

VALID_BLOCKED_OUTPUT = """
```yaml
verdict: BLOCKED
complexity: medium
work_type: feature
contract_change: false
reason: "External dependency not available."
sufficiency: needs_planning
spec_issues: ["Missing external API credentials"]
warnings: []
likely_files: []
criteria_checked:
  - criterion: "AC1"
    status: BLOCKED
    evidence: "Requires external service."
```
"""

GARBAGE_OUTPUT = "not yaml at all $$$ random output"


# ── GoldenStory / load_golden_set tests ──────────────────────────────────────


def test_load_golden_set_valid(tmp_path: Path) -> None:
    yaml_content = """
- id: "story-1"
  title: "Test"
  story_text: "## What\\nDo something."
  expected_verdict: "PROCEED"
  expected_complexity: "small"
  repo_snapshot_ref: "abc123"
"""
    p = tmp_path / "stories.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    stories = load_golden_set(p)
    assert len(stories) == 1
    assert stories[0].id == "story-1"
    assert stories[0].expected_verdict == "PROCEED"


def test_load_golden_set_warns_missing_snapshot(tmp_path: Path) -> None:
    yaml_content = """
- id: "story-1"
  title: "Test"
  story_text: "content"
  expected_verdict: "PROCEED"
"""
    p = tmp_path / "stories.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    with pytest.warns(UserWarning, match="repo_snapshot_ref"):
        load_golden_set(p)


def test_load_golden_set_invalid_verdict(tmp_path: Path) -> None:
    yaml_content = """
- id: "story-1"
  title: "Test"
  story_text: "content"
  expected_verdict: "MAYBE"
  repo_snapshot_ref: "abc123"
"""
    p = tmp_path / "stories.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    with pytest.raises(ValueError, match="expected_verdict"):
        load_golden_set(p)


# ── run_preflight_for_model tests ─────────────────────────────────────────────


def test_submitted_false_on_runner_failure() -> None:
    """submitted=False when exit_code != 0 (non-success result)."""
    story = _make_story()
    profile = _make_profile()
    config = _make_config()

    failed_result = _make_agent_result(success=False, output="", exit_code=1)

    with (
        patch("theforge.eval.preflight_harness.run_agent", return_value=failed_result),
        patch(
            "theforge.eval.preflight_harness.build_preflight_prompt",
            return_value="prompt",
        ),
    ):
        metrics = run_preflight_for_model(story, profile, Path("/tmp"), config)

    assert metrics.submitted is False
    assert metrics.valid_yaml is False
    assert metrics.verdict is None


def test_submitted_false_on_empty_output() -> None:
    """submitted=False when agent returns success but empty output."""
    story = _make_story()
    profile = _make_profile()
    config = _make_config()

    empty_result = _make_agent_result(success=True, output="  ", exit_code=0)

    with (
        patch("theforge.eval.preflight_harness.run_agent", return_value=empty_result),
        patch(
            "theforge.eval.preflight_harness.build_preflight_prompt",
            return_value="prompt",
        ),
    ):
        metrics = run_preflight_for_model(story, profile, Path("/tmp"), config)

    assert metrics.submitted is False


def test_valid_yaml_false_on_garbage_output() -> None:
    """valid_yaml=False when output is garbage that cannot be parsed."""
    story = _make_story()
    profile = _make_profile()
    config = _make_config()

    garbage_result = _make_agent_result(success=True, output=GARBAGE_OUTPUT, exit_code=0)

    with (
        patch("theforge.eval.preflight_harness.run_agent", return_value=garbage_result),
        patch(
            "theforge.eval.preflight_harness.build_preflight_prompt",
            return_value="prompt",
        ),
    ):
        metrics = run_preflight_for_model(story, profile, Path("/tmp"), config)

    assert metrics.submitted is True
    assert metrics.valid_yaml is False
    assert metrics.verdict is None


def test_proceed_verdict_parsed_correctly() -> None:
    """Correct PROCEED verdict is parsed from valid agent output."""
    story = _make_story(expected_verdict="PROCEED")
    profile = _make_profile()
    config = _make_config()

    ok_result = _make_agent_result(
        success=True, output=VALID_PROCEED_OUTPUT, exit_code=0, cost_usd=0.01
    )

    with (
        patch("theforge.eval.preflight_harness.run_agent", return_value=ok_result),
        patch(
            "theforge.eval.preflight_harness.build_preflight_prompt",
            return_value="prompt",
        ),
    ):
        metrics = run_preflight_for_model(story, profile, Path("/tmp"), config)

    assert metrics.submitted is True
    assert metrics.valid_yaml is True
    assert metrics.verdict == "PROCEED"
    assert metrics.complexity == "small"
    assert metrics.cost_usd == 0.01


def test_tool_iterations_extracted_from_raw() -> None:
    """tool_iterations is read from result.raw['num_turns'] when present."""
    story = _make_story()
    profile = _make_profile()
    config = _make_config()

    result_with_turns = _make_agent_result(
        success=True,
        output=VALID_PROCEED_OUTPUT,
        exit_code=0,
        raw={"num_turns": 7},
    )

    with (
        patch("theforge.eval.preflight_harness.run_agent", return_value=result_with_turns),
        patch(
            "theforge.eval.preflight_harness.build_preflight_prompt",
            return_value="prompt",
        ),
    ):
        metrics = run_preflight_for_model(story, profile, Path("/tmp"), config)

    assert metrics.tool_iterations == 7


def test_rationale_cites_code_true_when_file_path_in_output() -> None:
    """rationale_cites_code=True when output contains a .py file reference."""
    story = _make_story()
    profile = _make_profile()
    config = _make_config()

    # VALID_PROCEED_OUTPUT contains "src/theforge/foo.py"
    ok_result = _make_agent_result(success=True, output=VALID_PROCEED_OUTPUT, exit_code=0)

    with (
        patch("theforge.eval.preflight_harness.run_agent", return_value=ok_result),
        patch(
            "theforge.eval.preflight_harness.build_preflight_prompt",
            return_value="prompt",
        ),
    ):
        metrics = run_preflight_for_model(story, profile, Path("/tmp"), config)

    assert metrics.rationale_cites_code is True


# ── score_model tests ─────────────────────────────────────────────────────────


def _make_metrics(
    story_id: str,
    model: str,
    expected: str,
    verdict: str | None,
    submitted: bool = True,
    valid_yaml: bool = True,
    complexity: str | None = None,
    expected_complexity: str | None = None,
    rationale_cites_code: bool = False,
    cost_usd: float | None = None,
    duration_s: float | None = None,
    tool_iterations: int | None = None,
) -> RunMetrics:
    return RunMetrics(
        story_id=story_id,
        model_name=model,
        verdict=verdict,
        expected_verdict=expected,
        complexity=complexity,
        expected_complexity=expected_complexity,
        cost_usd=cost_usd,
        duration_s=duration_s,
        tool_iterations=tool_iterations,
        submitted=submitted,
        valid_yaml=valid_yaml,
        rationale_cites_code=rationale_cites_code,
        raw_output="",
    )


def test_timeout_rate() -> None:
    """submitted=False runs contribute to timeout_rate."""
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", "PROCEED", submitted=True),
        _make_metrics("s2", "m1", "PROCEED", None, submitted=False),
    ]
    scores = score_model(metrics)
    assert scores.timeout_rate == pytest.approx(0.5)


def test_invalid_yaml_rate() -> None:
    """valid_yaml=False (among submitted) contributes to invalid_yaml_rate."""
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", None, submitted=True, valid_yaml=False),
        _make_metrics("s2", "m1", "PROCEED", "PROCEED", submitted=True, valid_yaml=True),
    ]
    scores = score_model(metrics)
    assert scores.invalid_yaml_rate == pytest.approx(0.5)


def test_false_already_done_rate() -> None:
    """verdict=ALREADY_DONE when expected=PROCEED counted as false_already_done."""
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", "ALREADY_DONE"),
        _make_metrics("s2", "m1", "PROCEED", "PROCEED"),
    ]
    scores = score_model(metrics)
    assert scores.false_already_done_rate == pytest.approx(0.5)


def test_false_blocked_rate() -> None:
    """verdict=BLOCKED when expected=PROCEED counted as false_blocked."""
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", "BLOCKED"),
        _make_metrics("s2", "m1", "PROCEED", "PROCEED"),
    ]
    scores = score_model(metrics)
    assert scores.false_blocked_rate == pytest.approx(0.5)


def test_composite_error_rate_is_sum() -> None:
    """composite_error_rate = timeout + false_AD + false_BLK rates."""
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", None, submitted=False),  # timeout
        _make_metrics("s2", "m1", "PROCEED", "ALREADY_DONE"),  # false AD
        _make_metrics("s3", "m1", "PROCEED", "BLOCKED"),  # false BLK
        _make_metrics("s4", "m1", "PROCEED", "PROCEED"),  # correct
    ]
    scores = score_model(metrics)
    # timeout=0.25, false_AD=1/3≈0.333, false_BLK=1/3≈0.333
    expected = scores.timeout_rate + scores.false_already_done_rate + scores.false_blocked_rate
    assert scores.composite_error_rate == pytest.approx(expected)


def test_complexity_agreement_rate() -> None:
    """complexity_agreement_rate counts matching parsed vs expected complexity."""
    metrics = [
        _make_metrics(
            "s1", "m1", "PROCEED", "PROCEED", complexity="small", expected_complexity="small"
        ),
        _make_metrics(
            "s2", "m1", "PROCEED", "PROCEED", complexity="large", expected_complexity="small"
        ),
    ]
    scores = score_model(metrics)
    assert scores.complexity_agreement_rate == pytest.approx(0.5)


def test_complexity_agreement_skips_unknown() -> None:
    """complexity_agreement_rate excludes entries where either side is None."""
    metrics = [
        _make_metrics(
            "s1", "m1", "PROCEED", "PROCEED", complexity="small", expected_complexity="small"
        ),
        _make_metrics(
            "s2",
            "m1",
            "PROCEED",
            "PROCEED",
            complexity=None,
            expected_complexity="small",  # parsed unknown
        ),
    ]
    scores = score_model(metrics)
    # Only s1 contributes: 1/1 = 1.0
    assert scores.complexity_agreement_rate == pytest.approx(1.0)


def test_avg_cost_usd() -> None:
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", "PROCEED", cost_usd=0.10),
        _make_metrics("s2", "m1", "PROCEED", "PROCEED", cost_usd=0.20),
    ]
    scores = score_model(metrics)
    assert scores.avg_cost_usd == pytest.approx(0.15)


def test_avg_cost_usd_none_when_no_data() -> None:
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", "PROCEED", cost_usd=None),
    ]
    scores = score_model(metrics)
    assert scores.avg_cost_usd is None


# ── generate_report tests ─────────────────────────────────────────────────────


def test_generate_report_contains_model_names() -> None:
    """generate_report output contains all model names."""
    metrics = [
        _make_metrics("s1", "model-a", "PROCEED", "PROCEED"),
        _make_metrics("s1", "model-b", "PROCEED", "PROCEED"),
    ]
    report = generate_report(metrics)
    assert "model-a" in report
    assert "model-b" in report


def test_generate_report_empty_metrics() -> None:
    """generate_report handles empty input gracefully."""
    report = generate_report([])
    assert "No metrics" in report


def test_generate_report_contains_story_ids() -> None:
    """Per-story breakdown includes story IDs."""
    metrics = [
        _make_metrics("gh-999", "model-a", "PROCEED", "PROCEED"),
    ]
    report = generate_report(metrics)
    assert "gh-999" in report


def test_generate_report_verdict_match_symbol() -> None:
    """✓ appears when verdict matches expected, ✗ when it doesn't."""
    metrics = [
        _make_metrics("s1", "m1", "PROCEED", "PROCEED"),  # correct
        _make_metrics("s2", "m1", "PROCEED", "ALREADY_DONE"),  # wrong
    ]
    report = generate_report(metrics)
    assert "✓" in report
    assert "✗" in report
