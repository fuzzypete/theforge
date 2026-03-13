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
    generate_ideation_audit,
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


def _fail_result(output: str, profile_name: str = "test") -> AgentResult:
    return AgentResult(
        success=False,
        output=output,
        session_id=None,
        cost_usd=0.0,
        exit_code=1,
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


# ── Mock helpers ─────────────────────────────────────────────────────

# With run_agent_pool for Phase 1 and Phase 2, and run_agent for synthesis,
# tests patch both "theforge.ideate.run_agent_pool" and "theforge.ideate.run_agent".


def _make_pool_side_effect(
    phase1_outputs: list[str] | None = None,
    phase2_outputs: list[str] | None = None,
    phase1_cost: float = 0.10,
    phase2_cost: float = 0.10,
):
    """Returns a run_agent_pool side_effect for 2-model pool tests.

    Call 1 → Phase 1 results, Call 2 → Phase 2 results.
    """
    p1 = phase1_outputs or ["p1 A", "p1 B"]
    p2 = phase2_outputs or ["p2 A", "p2 B"]
    call_count = 0

    def _pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        nonlocal call_count
        call_count += 1
        if call_count % 2 == 1:  # odd calls = Phase 1 (per round)
            return [_ok_result(out, prof.name, phase1_cost) for prof, out in zip(profiles, p1)]
        else:  # even calls = Phase 2 (per round)
            return [_ok_result(out, prof.name, phase2_cost) for prof, out in zip(profiles, p2)]

    return _pool


def _make_synth_side_effect(synth_output: str = _SYNTHESIS_OUTPUT, cost: float = 0.10):
    """Returns a run_agent side_effect for synthesis calls."""

    def _synth(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        return _ok_result(synth_output, profile.name, cost)

    return _synth


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

    captured_pool_calls: list[dict] = []

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        captured_pool_calls.append({"prompt": prompt, "profiles": profiles})
        return [_ok_result(f"phase1 output {p.name}", p.name) for p in profiles]

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        result = run_ideation(config, "Test brief", None, max_rounds=1)

    assert result.success
    # First pool call is Phase 1: prompt must contain the brief but no other model's output
    p1_call = captured_pool_calls[0]
    assert "Test brief" in p1_call["prompt"]
    # Phase 1 prompt must NOT contain any model outputs (structural guarantee, not content-based)
    assert "phase1 output" not in p1_call["prompt"]


def test_phase2_includes_all_phase1_outputs(tmp_path: Path) -> None:
    """Phase 2 prompt must contain all Phase 1 outputs by model name."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    captured_prompts: list[str] = []
    pool_call_count = 0

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        nonlocal pool_call_count
        pool_call_count += 1
        captured_prompts.append(prompt)
        if pool_call_count == 1:  # Phase 1
            return [
                _ok_result("unique phase1 alpha content", "reviewer-a"),
                _ok_result("unique phase1 beta content", "reviewer-b"),
            ]
        return [_ok_result("phase2 out", p.name) for p in profiles]

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        run_ideation(config, "Test brief", None, max_rounds=1)

    assert len(captured_prompts) >= 2, "Phase 2 was not called"
    p2_prompt = captured_prompts[1]
    assert "reviewer-a" in p2_prompt
    assert "reviewer-b" in p2_prompt
    assert "unique phase1 alpha content" in p2_prompt
    assert "unique phase1 beta content" in p2_prompt


def test_synthesis_writes_spec_file(tmp_path: Path) -> None:
    """Synthesis output (valid frontmatter) should be written to output_path."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    output_path = tmp_path / "specs" / "test-feature.md"

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        result = run_ideation(config, "Build a feature", output_path, max_rounds=1)

    assert result.spec_path == output_path
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "Test Feature" in content
    assert "---" in content


def test_single_model_pool_skips_crossreview(tmp_path: Path) -> None:
    """Pool of 1 → single combined call; no Phase 2, no separate synthesis."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)

    agent_call_prompts: list[str] = []

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        agent_call_prompts.append(prompt)
        return _ok_result(_SYNTHESIS_OUTPUT, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    # Exactly one agent call — no Phase 2 and no separate synthesis
    assert len(agent_call_prompts) == 1
    assert result.success
    # Round has no phase2_outputs (no cross-review for single-model)
    assert result.rounds[0].phase2_outputs == {}
    # Final synthesis is valid spec extracted from the single call
    assert "---" in result.final_synthesis


def test_single_model_synthesis_produces_valid_spec(tmp_path: Path) -> None:
    """Single-model combined call must produce a spec with valid YAML frontmatter."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)
    output_path = tmp_path / "specs" / "solo-spec.md"

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
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
    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        result = run_ideation(config, "Build a feature", output_path, max_rounds=1)

    assert result.human_decision_required is True
    assert len(result.residual_divergence) > 0
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "Human Decisions Required" in content
    assert len(result.rounds) == 1


def test_human_decisions_section_overrides_synthesis_version(tmp_path: Path) -> None:
    """Residual divergence rewrites Human Decisions Required, overriding synthesis content."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    # Synthesis emits a Human Decisions Required section with wrong/extra content.
    synthesis_with_bad_section = f"""\
CONVERGED_ITEMS:
- Use async IO

DIVERGENT_ITEMS:
- Whether to use Redis or in-memory cache

SPEC:
{_VALID_SPEC}
## Human Decisions Required
- stale item that should be replaced
- another stale entry
"""

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch(
            "theforge.ideate.run_agent",
            side_effect=_make_synth_side_effect(synthesis_with_bad_section),
        ),
    ):
        result = run_ideation(config, "Build a feature", None, max_rounds=1)

    assert result.human_decision_required is True
    assert "Whether to use Redis or in-memory cache" in result.residual_divergence
    # The stale section content should be stripped and rewritten from residual_divergence
    assert "stale item that should be replaced" not in result.final_synthesis
    assert "Whether to use Redis or in-memory cache" in result.final_synthesis
    assert result.final_synthesis.count("## Human Decisions Required") == 1


def test_no_divergence_strips_human_decisions_section(tmp_path: Path) -> None:
    """No divergent items → spurious Human Decisions Required section is stripped."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    # Synthesis has no divergent items but erroneously emits the section.
    clean_synthesis_with_spurious_section = f"""\
CONVERGED_ITEMS:
- Use async IO
- Add unit tests

DIVERGENT_ITEMS:

SPEC:
{_VALID_SPEC}
## Human Decisions Required
- spurious item that should disappear
"""

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch(
            "theforge.ideate.run_agent",
            side_effect=_make_synth_side_effect(clean_synthesis_with_spurious_section),
        ),
    ):
        result = run_ideation(config, "Build a feature", None, max_rounds=1)

    assert result.human_decision_required is False
    assert "Human Decisions Required" not in result.final_synthesis
    assert "spurious item" not in result.final_synthesis


def test_dry_run_no_file_written(tmp_path: Path) -> None:
    """output_path=None → spec printed to stdout, no file created."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
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

    # Phase 1 pool: 2 models × 0.15 = 0.30
    # Phase 2 pool: 2 models × 0.20 = 0.40
    # Synthesis: 0.50
    # Total: 1.20
    with (
        patch(
            "theforge.ideate.run_agent_pool",
            side_effect=_make_pool_side_effect(phase1_cost=0.15, phase2_cost=0.20),
        ),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect(cost=0.50)),
    ):
        result = run_ideation(config, "Build a feature", None, max_rounds=1)

    assert abs(result.total_cost_usd - 1.20) < 1e-6


def test_multiple_rounds_loop(tmp_path: Path) -> None:
    """With max_rounds=2 and divergence after round 1, round 2 should run with narrowed brief."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    # Round 2 synthesis has no divergent items
    clean_synthesis = f"""CONVERGED_ITEMS:
- Use async IO
- Add unit tests
- Resolved the cache debate

DIVERGENT_ITEMS:

SPEC:
{_VALID_SPEC}"""

    synth_outputs = iter([_SYNTHESIS_OUTPUT, clean_synthesis])
    captured_pool_prompts: list[str] = []

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        captured_pool_prompts.append(prompt)
        return [_ok_result("output", p.name) for p in profiles]

    def mock_synth(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        return _ok_result(next(synth_outputs), profile.name)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=mock_pool),
        patch("theforge.ideate.run_agent", side_effect=mock_synth),
    ):
        result = run_ideation(config, "Build feature", None, max_rounds=2)

    assert len(result.rounds) == 2
    assert result.human_decision_required is False
    assert result.residual_divergence == []

    # Round 2 Phase 1 prompt (3rd pool call, index 2) must be narrowed to divergent items only.
    # The original brief "Build feature" must NOT appear — only the divergent items.
    round2_phase1_prompt = captured_pool_prompts[2]
    assert "Build feature" not in round2_phase1_prompt
    assert "Redis or in-memory cache" in round2_phase1_prompt
    assert "Focus exclusively" in round2_phase1_prompt


def test_phase1_agent_failure_returns_failed_result(tmp_path: Path) -> None:
    """A failed Phase 1 pool result should return a failed IdeationResult immediately."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    def mock_pool(*, prompt: str, profiles, working_dir: Path) -> list[AgentResult]:
        # Phase 1 pool returns a failed result for reviewer-a
        return [
            _fail_result("TIMEOUT: exceeded limit", profiles[0].name),
            _ok_result("output", profiles[1].name),
        ]

    with patch("theforge.ideate.run_agent_pool", side_effect=mock_pool) as mock_pool_obj:
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert "reviewer-a" in result.final_synthesis
    # Should stop after the first pool call (Phase 1 failure)
    assert mock_pool_obj.call_count == 1


def test_synthesis_failure_returns_failed_result(tmp_path: Path) -> None:
    """A failed synthesis agent should return a failed IdeationResult."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    def mock_synth(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        return _fail_result("ERROR: synthesis agent crashed", "synthesis")

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=mock_synth),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert "synthesis" in result.final_synthesis.lower()


def test_single_model_no_pool_calls(tmp_path: Path) -> None:
    """Single-model pool makes exactly one run_agent call (combined prompt)."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)

    call_count = 0

    def mock_agent(*, prompt: str, profile, working_dir: Path) -> AgentResult:
        nonlocal call_count
        call_count += 1
        return _ok_result(_SYNTHESIS_OUTPUT, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success
    assert call_count == 1  # single combined call, no Phase 2 or synthesis


def test_max_rounds_zero_clamped_to_one(tmp_path: Path) -> None:
    """max_rounds=0 is clamped to 1 defensively — at least one round must run."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=0)

    # Should complete exactly one round (clamped from 0 to 1)
    assert len(result.rounds) == 1
    assert result.success


def test_specs_dir_writes_slug_based_file(tmp_path: Path) -> None:
    """When specs_dir is provided and output_path is None, spec is written to specs_dir/slug.md."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    specs_dir = tmp_path / "specs"

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
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

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch(
            "theforge.ideate.run_agent",
            side_effect=_make_synth_side_effect(malformed_synthesis),
        ),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert result.spec_path is None


# ── Audit tests ───────────────────────────────────────────────────────


def test_generate_ideation_audit_structure(tmp_path: Path) -> None:
    """generate_ideation_audit returns a dict with type=ideate and expected fields."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        result = run_ideation(config, "Build a feature", None, max_rounds=1)

    audit = generate_ideation_audit(
        config,
        "Build a feature",
        result,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
        duration_seconds=60.0,
    )

    assert audit["type"] == "ideate"
    assert audit["brief"] == "Build a feature"
    assert audit["success"] == result.success
    assert audit["human_decision_required"] == result.human_decision_required
    assert "rounds" in audit
    assert "cost" in audit
    assert "timing" in audit
    assert audit["timing"]["started_at"] == "2026-01-01T00:00:00+00:00"
    assert audit["timing"]["duration_seconds"] == 60.0
    assert "reviewer-a" in audit["model_pool"]
    assert "reviewer-b" in audit["model_pool"]
    assert audit["synthesis_profile"] == "synthesis"


def test_generate_ideation_audit_rounds_data(tmp_path: Path) -> None:
    """Rounds data in audit contains converged/divergent counts and items."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        result = run_ideation(config, "Build a feature", None, max_rounds=1)

    audit = generate_ideation_audit(config, "Build a feature", result)

    assert len(audit["rounds"]) == 1
    r = audit["rounds"][0]
    assert r["round_number"] == 1
    assert r["converged_count"] == 2  # "Use async IO" + "Add unit tests"
    assert r["divergent_count"] == 1  # "Whether to use Redis or in-memory cache"
    assert "Whether to use Redis or in-memory cache" in r["divergent_items"]


def test_generate_ideation_audit_brief_truncated(tmp_path: Path) -> None:
    """Briefs longer than 500 chars are truncated in the audit record."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_make_pool_side_effect()),
        patch("theforge.ideate.run_agent", side_effect=_make_synth_side_effect()),
    ):
        result = run_ideation(config, "x" * 600, None, max_rounds=1)

    audit = generate_ideation_audit(config, "x" * 600, result)
    assert len(audit["brief"]) == 500


def test_parse_synthesis_output_lstrip_safe(tmp_path: Path) -> None:
    """Items starting with '-- ' or '-verbose' are not over-stripped."""
    synthesis = """CONVERGED_ITEMS:
- --verbose flag should be added
- normal item

DIVERGENT_ITEMS:
- --debug mode

SPEC:
---
name: "Test"
slug: test
file_scope: []
pytest_target: tests/
---
# Test
"""
    converged, divergent, _ = _parse_synthesis_output(synthesis)
    # With removeprefix("- "), "- --verbose flag" becomes "--verbose flag" (correct)
    # With the old lstrip("- "), it would have become "verbose flag" (wrong)
    assert any("--verbose" in item for item in converged)
    assert any("--debug" in item for item in divergent)
