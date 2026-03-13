"""Tests for the ideate module — multi-LLM deliberation for spec generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.ideate import (
    _build_phase1_prompt,
    _build_phase2_prompt,
    _build_synthesis_prompt,
    _parse_synthesis_output,
    _validate_frontmatter,
    run_ideation,
)
from theforge.runner import AgentResult

# ── Fixtures ─────────────────────────────────────────────────────────

_SYNTH_PROFILE = ModelProfile(
    name="synthesis",
    cli="claude",
    model="opus",
    budget_usd=1.0,
    timeout_seconds=300,
    allowed_tools=("Read",),
)

_REVIEWER_A = ModelProfile(
    name="reviewer-a",
    cli="claude",
    model="opus",
    budget_usd=1.0,
    timeout_seconds=300,
    allowed_tools=("Read",),
)

_REVIEWER_B = ModelProfile(
    name="reviewer-b",
    cli="claude",
    model="sonnet",
    budget_usd=1.0,
    timeout_seconds=300,
    allowed_tools=("Read",),
)

_SINGLE_REVIEWER = ModelProfile(
    name="solo",
    cli="claude",
    model="sonnet",
    budget_usd=1.0,
    timeout_seconds=300,
    allowed_tools=("Read",),
)


def _make_config(
    tmp_path: Path,
    pool: list[ModelProfile],
    synthesis: ModelProfile | None,
) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=_SINGLE_REVIEWER,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=pool,
        synthesis_profile=synthesis,
        retry=RetryPolicy(),
    )


def _ok_result(output: str, profile_name: str = "test", cost: float = 0.10) -> AgentResult:
    return AgentResult(
        success=True,
        output=output,
        session_id=None,
        cost_usd=cost,
        exit_code=0,
        raw={},
        profile_name=profile_name,
    )


_VALID_SPEC = """\
---
name: "Test Feature"
slug: test-feature
file_scope: []
pytest_target: tests/
---

# Test Feature

## Problem
A test problem.

## Requirements
- Do the thing.

## Acceptance Criteria
- [ ] Thing is done.
"""

_SYNTHESIS_OUTPUT = f"""CONVERGED_ITEMS:
- Use async IO
- Add unit tests

DIVERGENT_ITEMS:
- Whether to use Redis or in-memory cache

SPEC:
{_VALID_SPEC}"""


# ── Prompt builder tests ─────────────────────────────────────────────


def test_phase1_prompt_contains_brief() -> None:
    brief = "Build a caching layer for the API."
    prompt = _build_phase1_prompt(brief)
    assert brief in prompt
    assert "## Core Ideas" in prompt
    assert "## Key Constraints" in prompt
    assert "## Risks and Blind Spots" in prompt
    assert "## Recommended Approach" in prompt


def test_phase2_prompt_includes_all_phase1_outputs() -> None:
    brief = "Build a caching layer."
    phase1 = {
        "reviewer-a": "Idea A content here",
        "reviewer-b": "Idea B content here",
    }
    prompt = _build_phase2_prompt(brief, phase1)
    assert brief in prompt
    assert "reviewer-a" in prompt
    assert "reviewer-b" in prompt
    assert "Idea A content here" in prompt
    assert "Idea B content here" in prompt
    assert "## Agreements" in prompt
    assert "## Disagreements" in prompt


def test_phase2_prompt_no_cross_contamination_in_phase1() -> None:
    """Phase 1 prompt must NOT include other models' outputs."""
    brief = "Build something."
    phase1_prompt = _build_phase1_prompt(brief)
    assert "reviewer-a" not in phase1_prompt
    assert "reviewer-b" not in phase1_prompt
    # No other model's content should appear
    assert "Idea A" not in phase1_prompt
    assert "Idea B" not in phase1_prompt


def test_synthesis_prompt_includes_all_outputs() -> None:
    brief = "Build a feature."
    phase1 = {"model-a": "phase1 output a", "model-b": "phase1 output b"}
    phase2 = {"model-a": "phase2 review a", "model-b": "phase2 review b"}
    prompt = _build_synthesis_prompt(brief, phase1, phase2)
    assert "phase1 output a" in prompt
    assert "phase1 output b" in prompt
    assert "phase2 review a" in prompt
    assert "phase2 review b" in prompt
    assert "CONVERGED_ITEMS" in prompt
    assert "DIVERGENT_ITEMS" in prompt
    assert "SPEC:" in prompt


# ── Synthesis parsing tests ──────────────────────────────────────────


def test_parse_synthesis_output_extracts_sections() -> None:
    converged, divergent, spec_text = _parse_synthesis_output(_SYNTHESIS_OUTPUT)
    assert "Use async IO" in converged
    assert "Add unit tests" in converged
    assert "Whether to use Redis or in-memory cache" in divergent
    assert "---" in spec_text
    assert "Test Feature" in spec_text


def test_validate_frontmatter_valid() -> None:
    assert _validate_frontmatter(_VALID_SPEC) is True


def test_validate_frontmatter_invalid() -> None:
    assert _validate_frontmatter("# Just markdown\nNo frontmatter here.") is False


# ── run_ideation tests ────────────────────────────────────────────────


def test_phase1_fanout(tmp_path: Path) -> None:
    """Phase 1 prompt must be sent to all models without cross-contamination."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    pool_results = [
        _ok_result("phase1 output A", "reviewer-a"),
        _ok_result("phase1 output B", "reviewer-b"),
    ]
    synth_result = _ok_result(_SYNTHESIS_OUTPUT, "synthesis")

    captured_pool_calls: list[dict] = []

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        captured_pool_calls.append({"prompt": prompt, "profiles": profiles})
        if captured_pool_calls and len(captured_pool_calls) == 1:
            return pool_results
        return [_ok_result("phase2 out A"), _ok_result("phase2 out B")]

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=synth_result),
    ):
        result = run_ideation(config, "Test brief", None, max_rounds=1)

    assert result.success
    # Phase 1 call is first
    assert len(captured_pool_calls) >= 1
    phase1_call = captured_pool_calls[0]
    # Phase 1 prompt should contain the brief but NOT other models' outputs
    assert "Test brief" in phase1_call["prompt"]
    assert "phase1 output A" not in phase1_call["prompt"]
    assert "phase1 output B" not in phase1_call["prompt"]
    # Both profiles in the pool
    assert len(phase1_call["profiles"]) == 2


def test_phase2_includes_all_phase1_outputs(tmp_path: Path) -> None:
    """Phase 2 prompt must contain all Phase 1 outputs by model name."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    pool_call_count = 0
    captured_phase2_prompt: list[str] = []

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        nonlocal pool_call_count
        pool_call_count += 1
        if pool_call_count == 1:
            # Phase 1
            return [
                _ok_result("unique phase1 alpha content", "reviewer-a"),
                _ok_result("unique phase1 beta content", "reviewer-b"),
            ]
        else:
            # Phase 2
            captured_phase2_prompt.append(prompt)
            return [_ok_result("phase2 out A"), _ok_result("phase2 out B")]

    synth_result = _ok_result(_SYNTHESIS_OUTPUT, "synthesis")

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=synth_result),
    ):
        run_ideation(config, "Test brief", None, max_rounds=1)

    assert captured_phase2_prompt, "Phase 2 was not called"
    p2 = captured_phase2_prompt[0]
    assert "reviewer-a" in p2
    assert "reviewer-b" in p2
    assert "unique phase1 alpha content" in p2
    assert "unique phase1 beta content" in p2


def test_synthesis_writes_spec_file(tmp_path: Path) -> None:
    """Synthesis output (valid frontmatter) should be written to output_path."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    output_path = tmp_path / "specs" / "test-feature.md"

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [_ok_result("phase output"), _ok_result("phase output 2")]

    synth_result = _ok_result(_SYNTHESIS_OUTPUT, "synthesis")

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=synth_result),
    ):
        result = run_ideation(config, "Build a feature", output_path, max_rounds=1)

    assert result.spec_path == output_path
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "Test Feature" in content
    assert "---" in content


def test_single_model_pool_skips_crossreview(tmp_path: Path) -> None:
    """Pool of 1 → Phase 2 and synthesis both skipped; Phase 1 output is the result."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)

    pool_call_count = 0

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        nonlocal pool_call_count
        pool_call_count += 1
        return [_ok_result("single model phase1 output")]

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent") as mock_synth,
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    # Only Phase 1 pool call; no Phase 2, no synthesis
    assert pool_call_count == 1
    mock_synth.assert_not_called()
    assert result.success
    # rounds has phase2_outputs as empty dict
    assert result.rounds[0].phase2_outputs == {}
    # Phase 1 output is used as the spec text
    assert "single model phase1 output" in result.final_synthesis


def test_max_rounds_respected(tmp_path: Path) -> None:
    """max_rounds=1 with divergence → human_decision_required=True, spec still written."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    output_path = tmp_path / "specs" / "my-spec.md"

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [_ok_result("pool output A"), _ok_result("pool output B")]

    synth_result = _ok_result(_SYNTHESIS_OUTPUT, "synthesis")  # has divergent items

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=synth_result),
    ):
        result = run_ideation(config, "Build a feature", output_path, max_rounds=1)

    assert result.human_decision_required is True
    assert len(result.residual_divergence) > 0
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "Human Decisions Required" in content
    assert len(result.rounds) == 1


def test_dry_run_no_file_written(tmp_path: Path) -> None:
    """output_path=None → spec printed to stdout, no file created."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [_ok_result("pool output A"), _ok_result("pool output B")]

    synth_result = _ok_result(_SYNTHESIS_OUTPUT, "synthesis")

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=synth_result),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.spec_path is None
    # No spec files created
    spec_files = list(tmp_path.rglob("*.md"))
    assert spec_files == []


def test_no_synthesis_profile_raises(tmp_path: Path) -> None:
    """Pool > 1 without synthesis profile → ValueError."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], None)

    with pytest.raises(ValueError, match="synthesis profile is required"):
        run_ideation(config, "A brief", None)


def test_ideation_result_cost_accumulates(tmp_path: Path) -> None:
    """total_cost_usd sums across all pool invocations and synthesis."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [_ok_result("output A", cost=0.10), _ok_result("output B", cost=0.20)]

    synth_result = _ok_result(_SYNTHESIS_OUTPUT, "synthesis", cost=0.50)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=synth_result),
    ):
        result = run_ideation(config, "Build a feature", None, max_rounds=1)

    # Phase 1: 0.10 + 0.20 = 0.30
    # Phase 2: 0.10 + 0.20 = 0.30
    # Synthesis: 0.50
    # Total: 1.10
    assert abs(result.total_cost_usd - 1.10) < 1e-6


def test_multiple_rounds_loop(tmp_path: Path) -> None:
    """With max_rounds=2 and divergence after round 1, round 2 should run."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    # Round 2 synthesis has no divergent items
    clean_synthesis = f"""CONVERGED_ITEMS:
- Use async IO
- Add unit tests
- Resolved the cache debate

DIVERGENT_ITEMS:

SPEC:
{_VALID_SPEC}"""

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [_ok_result("output"), _ok_result("output")]

    synth_results = [
        _ok_result(_SYNTHESIS_OUTPUT, "synthesis"),  # round 1 — has divergence
        _ok_result(clean_synthesis, "synthesis"),  # round 2 — no divergence
    ]
    synth_iter = iter(synth_results)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", side_effect=lambda **kw: next(synth_iter)),
    ):
        result = run_ideation(config, "Build feature", None, max_rounds=2)

    assert len(result.rounds) == 2
    assert result.human_decision_required is False
    assert result.residual_divergence == []


def test_phase1_agent_failure_returns_failed_result(tmp_path: Path) -> None:
    """A failed Phase 1 agent should return a failed IdeationResult immediately."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [
            AgentResult(
                success=False,
                output="TIMEOUT: exceeded limit",
                session_id=None,
                cost_usd=0.0,
                exit_code=-1,
                raw={},
                profile_name="reviewer-a",
            ),
            _ok_result("phase1 B"),
        ]

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent") as mock_synth,
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert "reviewer-a" in result.final_synthesis
    mock_synth.assert_not_called()


def test_synthesis_failure_returns_failed_result(tmp_path: Path) -> None:
    """A failed synthesis agent should return a failed IdeationResult."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [_ok_result("pool output A"), _ok_result("pool output B")]

    failed_synth = AgentResult(
        success=False,
        output="ERROR: synthesis agent crashed",
        session_id=None,
        cost_usd=0.0,
        exit_code=1,
        raw={},
        profile_name="synthesis",
    )

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=failed_synth),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert "synthesis" in result.final_synthesis.lower()


def test_synthesis_invalid_frontmatter_returns_failed_result(tmp_path: Path) -> None:
    """Synthesis output without valid frontmatter should return a failed IdeationResult."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    malformed_synthesis = """CONVERGED_ITEMS:
- item one

DIVERGENT_ITEMS:

SPEC:
This is just prose with no YAML frontmatter at all.
No triple-dash delimiters here.
"""

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        return [_ok_result("pool output A"), _ok_result("pool output B")]

    synth_result = _ok_result(malformed_synthesis, "synthesis")

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", return_value=synth_result),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert result.spec_path is None
