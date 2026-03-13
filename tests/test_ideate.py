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

# Helper: build a run_agent side_effect for a 2-model pool.
# Call order per round: P1-A, P1-B, P2-A, P2-B, Synthesis.
_P2_PROMPT_MARKER = "Cross-review"  # appears in phase2 prompt, not phase1


def _multi_model_agent(
    phase1_a: str = "p1 A",
    phase1_b: str = "p1 B",
    phase2_a: str = "p2 A",
    phase2_b: str = "p2 B",
    synth: str = _SYNTHESIS_OUTPUT,
):
    """Returns a run_agent side_effect for a 2-model pool single-round test."""
    outputs = [phase1_a, phase1_b, phase2_a, phase2_b, synth]
    costs = [0.10, 0.10, 0.10, 0.10, 0.10]
    call_count = 0

    def _agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        idx = call_count
        call_count += 1
        out = outputs[idx] if idx < len(outputs) else synth
        return _ok_result(out, profile.name, cost=costs[idx] if idx < len(costs) else 0.10)

    return _agent


def test_phase1_fanout(tmp_path: Path) -> None:
    """Phase 1 prompt must be sent to all models without cross-contamination."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    captured_calls: list[dict] = []

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        captured_calls.append({"prompt": prompt, "profile": profile})
        idx = len(captured_calls) - 1
        outputs = ["phase1 output A", "phase1 output B", "p2 A", "p2 B", _SYNTHESIS_OUTPUT]
        return _ok_result(outputs[idx], profile.name)

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "Test brief", None, max_rounds=1)

    assert result.success
    # Phase 1 calls (calls 0 and 1) must contain the brief but NOT other models' outputs
    p1_call_a = captured_calls[0]
    p1_call_b = captured_calls[1]
    assert "Test brief" in p1_call_a["prompt"]
    assert "Test brief" in p1_call_b["prompt"]
    assert "phase1 output A" not in p1_call_a["prompt"]
    assert "phase1 output B" not in p1_call_a["prompt"]
    assert "phase1 output A" not in p1_call_b["prompt"]
    assert "phase1 output B" not in p1_call_b["prompt"]


def test_phase2_includes_all_phase1_outputs(tmp_path: Path) -> None:
    """Phase 2 prompt must contain all Phase 1 outputs by model name."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    captured_phase2_prompts: list[str] = []
    call_count = 0

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok_result("unique phase1 alpha content", "reviewer-a")
        if call_count == 2:
            return _ok_result("unique phase1 beta content", "reviewer-b")
        if call_count in (3, 4):
            # Phase 2 calls — capture the prompt
            captured_phase2_prompts.append(prompt)
            return _ok_result("phase2 out", profile.name)
        return _ok_result(_SYNTHESIS_OUTPUT, "synthesis")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        run_ideation(config, "Test brief", None, max_rounds=1)

    assert captured_phase2_prompts, "Phase 2 was not called"
    p2 = captured_phase2_prompts[0]
    assert "reviewer-a" in p2
    assert "reviewer-b" in p2
    assert "unique phase1 alpha content" in p2
    assert "unique phase1 beta content" in p2


def test_synthesis_writes_spec_file(tmp_path: Path) -> None:
    """Synthesis output (valid frontmatter) should be written to output_path."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    output_path = tmp_path / "specs" / "test-feature.md"

    with patch(
        "theforge.ideate.run_agent", side_effect=_multi_model_agent(synth=_SYNTHESIS_OUTPUT)
    ):
        result = run_ideation(config, "Build a feature", output_path, max_rounds=1)

    assert result.spec_path == output_path
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "Test Feature" in content
    assert "---" in content


def test_single_model_pool_skips_crossreview(tmp_path: Path) -> None:
    """Pool of 1 → Phase 2 skipped; synthesis runs with lone model to produce valid spec."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)

    agent_call_prompts: list[str] = []

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        agent_call_prompts.append(prompt)
        if len(agent_call_prompts) == 1:
            # Phase 1 call
            return _ok_result("single model phase1 output")
        # Synthesis call
        return _ok_result(_SYNTHESIS_OUTPUT, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    # Phase 1 prompt should NOT include cross-review content
    assert "single model phase1 output" not in agent_call_prompts[0]
    # Synthesis was called (second call)
    assert len(agent_call_prompts) == 2
    assert result.success
    # rounds has phase2_outputs as empty dict (no cross-review)
    assert result.rounds[0].phase2_outputs == {}
    # Final synthesis is valid spec from synthesis step
    assert "---" in result.final_synthesis


def test_single_model_synthesis_produces_valid_spec(tmp_path: Path) -> None:
    """Single-model synthesis output must contain valid YAML frontmatter."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)
    output_path = tmp_path / "specs" / "solo-spec.md"

    call_count = 0

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok_result("## Core Ideas\nIdea A\nIdea B")
        return _ok_result(_SYNTHESIS_OUTPUT, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", output_path, max_rounds=1)

    assert result.success
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "---" in content
    assert "Test Feature" in content


def test_max_rounds_respected(tmp_path: Path) -> None:
    """max_rounds=1 with divergence → human_decision_required=True, spec still written."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    output_path = tmp_path / "specs" / "my-spec.md"

    # _SYNTHESIS_OUTPUT has divergent items; max_rounds=1 stops here
    with patch(
        "theforge.ideate.run_agent", side_effect=_multi_model_agent(synth=_SYNTHESIS_OUTPUT)
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

    with patch(
        "theforge.ideate.run_agent", side_effect=_multi_model_agent(synth=_SYNTHESIS_OUTPUT)
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
    """total_cost_usd sums across all agent invocations (2 phase1 + 2 phase2 + synthesis)."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    # Call order: P1-A(0.10), P1-B(0.20), P2-A(0.10), P2-B(0.20), Synthesis(0.50)
    costs = [0.10, 0.20, 0.10, 0.20, 0.50]
    call_count = 0

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        cost = costs[call_count]
        call_count += 1
        output = _SYNTHESIS_OUTPUT if call_count == 5 else "output"
        return _ok_result(output, profile.name, cost=cost)

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "Build a feature", None, max_rounds=1)

    # 0.10 + 0.20 + 0.10 + 0.20 + 0.50 = 1.10
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

    # Round 1: 5 calls (P1-A, P1-B, P2-A, P2-B, Synth divergent)
    # Round 2: 5 calls (P1-A, P1-B, P2-A, P2-B, Synth clean)
    synth_outputs = iter([_SYNTHESIS_OUTPUT, clean_synthesis])
    call_count = 0

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        call_count += 1
        # Every 5th call within a round is synthesis
        if call_count % 5 == 0:
            return _ok_result(next(synth_outputs), "synthesis")
        return _ok_result("output", profile.name)

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "Build feature", None, max_rounds=2)

    assert len(result.rounds) == 2
    assert result.human_decision_required is False
    assert result.residual_divergence == []


def test_phase1_agent_failure_returns_failed_result(tmp_path: Path) -> None:
    """A failed Phase 1 agent should return a failed IdeationResult immediately."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        # First call (Phase 1, reviewer-a) fails
        return AgentResult(
            success=False,
            output="TIMEOUT: exceeded limit",
            session_id=None,
            cost_usd=0.0,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )

    with patch("theforge.ideate.run_agent", side_effect=mock_agent) as mock_run:
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert "reviewer-a" in result.final_synthesis
    # Should stop after the first failure
    assert mock_run.call_count == 1


def test_synthesis_failure_returns_failed_result(tmp_path: Path) -> None:
    """A failed synthesis agent should return a failed IdeationResult."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    call_count = 0

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        call_count += 1
        if call_count < 5:
            return _ok_result("output", profile.name)
        # 5th call = synthesis — fail it
        return AgentResult(
            success=False,
            output="ERROR: synthesis agent crashed",
            session_id=None,
            cost_usd=0.0,
            exit_code=1,
            raw={},
            profile_name="synthesis",
        )

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert "synthesis" in result.final_synthesis.lower()


def test_single_model_no_pool_calls(tmp_path: Path) -> None:
    """Single-model pool uses run_agent directly for all phases."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)

    call_count = 0

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok_result("phase1 output")
        return _ok_result(_SYNTHESIS_OUTPUT, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success
    assert call_count == 2  # Phase 1 + synthesis


def test_max_rounds_zero_clamped_to_one(tmp_path: Path) -> None:
    """max_rounds=0 is clamped to 1 defensively — at least one round must run."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    with patch(
        "theforge.ideate.run_agent", side_effect=_multi_model_agent(synth=_SYNTHESIS_OUTPUT)
    ):
        result = run_ideation(config, "A brief", None, max_rounds=0)

    # Should complete exactly one round (clamped from 0 to 1)
    assert len(result.rounds) == 1
    assert result.success


def test_specs_dir_writes_slug_based_file(tmp_path: Path) -> None:
    """When specs_dir is provided and output_path is None, spec is written to specs_dir/slug.md."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    specs_dir = tmp_path / "specs"

    with patch(
        "theforge.ideate.run_agent", side_effect=_multi_model_agent(synth=_SYNTHESIS_OUTPUT)
    ):
        result = run_ideation(config, "Build a feature", None, specs_dir=specs_dir, max_rounds=1)

    assert result.success
    assert result.spec_path is not None
    assert result.spec_path.parent == specs_dir
    assert result.spec_path.name == "test-feature.md"  # slug from _VALID_SPEC
    assert result.spec_path.exists()


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

    with patch(
        "theforge.ideate.run_agent",
        side_effect=_multi_model_agent(synth=malformed_synthesis),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert result.spec_path is None
