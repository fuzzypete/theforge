"""Tests for ideate prompt-building, prohibited-content detection, and validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.ideate import (
    _SPEC_LINE_LIMIT,
    _build_phase1_prompt,
    _build_phase2_prompt,
    _build_single_model_prompt,
    _build_synthesis_prompt,
    _has_prohibited_content,
    _parse_synthesis_output,
    _validate_frontmatter,
    run_ideation,
)
from theforge.runners import AgentResult

# ── Shared model profiles ─────────────────────────────────────────────

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


# ── Small fixtures ────────────────────────────────────────────────────


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
test_target: tests/
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
    # Lean output instructions
    assert "observable behavior" in prompt
    assert "Language-specific signatures" in prompt
    assert "Code snippets" in prompt
    assert "150 lines" in prompt


def test_single_model_prompt_includes_lean_constraints() -> None:
    brief = "Build a caching layer."
    prompt = _build_single_model_prompt(brief)
    assert "observable behavior" in prompt
    assert "Language-specific signatures" in prompt
    assert "Code snippets" in prompt
    assert "150 lines" in prompt


# ── Round-trip test ──────────────────────────────────────────────────


def test_round_trip_ideate_to_dev_prompt(tmp_path: Path) -> None:
    """Spec produced by run_ideation parses into a non-empty dev prompt."""
    from theforge.task import TaskStory, build_dev_prompt, parse_spec_frontmatter

    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)
    output_path = tmp_path / "specs" / "test-feature.md"

    def mock_agent(*, prompt: str, profile, working_dir: Path, **kwargs) -> AgentResult:
        return _ok_result(_SYNTHESIS_OUTPUT, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "Build a feature", output_path, max_rounds=1)

    assert result.success
    assert output_path.exists()

    fm = parse_spec_frontmatter(output_path)
    task = TaskStory(
        name=fm.get("name", "test"),
        story_path=output_path,
        slug=fm.get("slug", "test"),
        test_target=fm.get("test_target", "tests/"),
    )
    spec_content = output_path.read_text(encoding="utf-8")
    dev_prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "workspace",
        branch_name="feat/test-feature",
        story_content=spec_content,
        gate_command="make gate",
    )
    assert len(dev_prompt) > 0
    assert "Test Feature" in dev_prompt


# ── Line-limit enforcement tests ─────────────────────────────────────


def _make_long_spec(line_count: int = 160) -> str:
    """Build a spec with valid frontmatter but more than 150 lines total."""
    padding = "\n".join(f"- AC item {i}" for i in range(line_count))
    return f"""\
---
name: "Test Feature"
slug: test-feature
test_target: tests/
---

# Test Feature

## Problem
A test problem.

## Acceptance Criteria
{padding}
"""


def test_single_model_overlong_spec_returns_failed(tmp_path: Path) -> None:
    """Single-model output exceeding _SPEC_LINE_LIMIT → failed IdeationResult."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)
    long_output = f"SPEC:\n{_make_long_spec(line_count=_SPEC_LINE_LIMIT + 10)}"

    def mock_agent(*, prompt: str, profile, working_dir: Path, **kwargs) -> AgentResult:
        return _ok_result(long_output, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert str(_SPEC_LINE_LIMIT) in result.final_synthesis


def test_synthesis_overlong_spec_returns_failed(tmp_path: Path) -> None:
    """Multi-model synthesis output exceeding _SPEC_LINE_LIMIT → failed IdeationResult."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    long_spec = _make_long_spec(line_count=_SPEC_LINE_LIMIT + 10)
    long_synthesis = f"CONVERGED_ITEMS:\n- item\n\nDIVERGENT_ITEMS:\n\nSPEC:\n{long_spec}"

    def _pool(*, prompt, profiles, working_dir, **kwargs):
        return [_ok_result(f"out {p.name}", p.name) for p in profiles]

    def _synth(*, prompt, profile, working_dir, **kwargs):
        return _ok_result(long_synthesis, profile.name)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_pool),
        patch("theforge.ideate.run_agent", side_effect=_synth),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert str(_SPEC_LINE_LIMIT) in result.final_synthesis


# ── Prohibited-content detection unit tests ──────────────────────────


def test_has_prohibited_content_clean_spec() -> None:
    """Clean spec with no code blocks or signatures returns (False, '')."""
    found, reason = _has_prohibited_content(_VALID_SPEC)
    assert found is False
    assert reason == ""


def test_has_prohibited_content_code_block() -> None:
    spec = _VALID_SPEC + "\n```python\nprint('hi')\n```\n"
    found, reason = _has_prohibited_content(spec)
    assert found is True
    assert "code block" in reason


def test_has_prohibited_content_function_def_in_prose_is_allowed() -> None:
    spec = _VALID_SPEC + "\nMention examples like def my_func(arg1, arg2): in prose only.\n"
    found, reason = _has_prohibited_content(spec)
    assert found is False
    assert reason == ""


def test_has_prohibited_content_class_def_in_prose_is_allowed() -> None:
    spec = _VALID_SPEC + "\nMention examples like class MyClass: in prose only.\n"
    found, reason = _has_prohibited_content(spec)
    assert found is False
    assert reason == ""


def test_has_prohibited_content_dataclass_marker_in_prose_is_allowed() -> None:
    spec = _VALID_SPEC + "\nMention examples like @dataclass in prose only.\n"
    found, reason = _has_prohibited_content(spec)
    assert found is False
    assert reason == ""


def test_has_prohibited_content_bare_signature_in_prose_is_allowed() -> None:
    spec = _VALID_SPEC + "\nMention examples like my_func(a: int) -> bool in prose only.\n"
    found, reason = _has_prohibited_content(spec)
    assert found is False
    assert reason == ""


def test_has_prohibited_content_prose_with_parenthetical() -> None:
    """Prose lines with parenthetical text (e.g. in Context/Background) are not flagged."""
    extra = "\n## Context\nBackground (current state): slow.\nContext (as-is): no cache.\n"
    spec = _VALID_SPEC + extra
    found, reason = _has_prohibited_content(spec)
    assert found is False
    assert reason == ""


# ── Prohibited-content enforcement integration tests ──────────────────


def _make_spec_with_code_block() -> str:
    return (
        _VALID_SPEC + "\n## Implementation\n```python\ndef cache_get(key):\n    return None\n```\n"
    )


def test_single_model_spec_with_code_block_returns_failed(tmp_path: Path) -> None:
    """Single-model output with a fenced code block → failed IdeationResult."""
    config = _make_config(tmp_path, [_SINGLE_REVIEWER], None)
    spec_with_code = _make_spec_with_code_block()
    output = f"SPEC:\n{spec_with_code}"

    def mock_agent(*, prompt: str, profile, working_dir: Path, **kwargs) -> AgentResult:
        return _ok_result(output, "solo")

    with patch("theforge.ideate.run_agent", side_effect=mock_agent):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is False
    assert "prohibited" in result.final_synthesis.lower()


def test_synthesis_spec_with_function_def_in_prose_is_allowed(tmp_path: Path) -> None:
    """Language-specific code-shape text in prose no longer blocks ideation."""
    config = _make_config(tmp_path, [_REVIEWER_A, _REVIEWER_B], _SYNTH_PROFILE)
    spec_with_func = (
        _VALID_SPEC
        + "\nMention examples like def build_cache(ttl: int) -> Cache:"
        + " in prose only.\n"
    )
    synthesis = f"CONVERGED_ITEMS:\n- item\n\nDIVERGENT_ITEMS:\n\nSPEC:\n{spec_with_func}"

    def _pool(*, prompt, profiles, working_dir, **kwargs):
        return [_ok_result(f"out {p.name}", p.name) for p in profiles]

    def _synth(*, prompt, profile, working_dir, **kwargs):
        return _ok_result(synthesis, profile.name)

    with (
        patch("theforge.ideate.run_agent_pool", side_effect=_pool),
        patch("theforge.ideate.run_agent", side_effect=_synth),
    ):
        result = run_ideation(config, "A brief", None, max_rounds=1)

    assert result.success is True


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
