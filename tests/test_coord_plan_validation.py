"""Integration tests: plan validation is invoked from coordinator/plan_flow.py.

Verifies that the mechanical plan validation (pure Python, no LLM) is wired
into the coordinator pipeline:

  - validate_plan is called when a structured YAML plan is parsed
  - findings are stored on state.plan_validation_findings
  - freeform markdown plans are silently skipped (no findings recorded)
  - validation is advisory — never blocks DEV
  - AC coverage findings appear for criteria missing from criteria_mapping
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# ── Structured plan fixtures ──────────────────────────────────────��──────────

_STRUCTURED_PLAN = """\
plan:
  approach: "Implement the feature"
  steps:
    - id: 1
      description: "Add validator module"
      files:
        - src/theforge/plan_validator.py
      action: create
      details: "Create the validator with four checks"
  criteria_mapping:
    - criterion: "AC one"
      steps: [1]
"""

_FREEFORM_PLAN = """\
# Implementation Plan

## Overview

This is a freeform markdown plan without structured YAML.

1. Implement feature
2. Add tests
3. Run gate
"""

_STRUCTURED_PLAN_NO_CRITERIA_MAPPING = """\
plan:
  approach: "Implement the feature"
  steps:
    - id: 1
      description: "Add validator module"
      files:
        - src/theforge/plan_validator.py
      action: create
      details: "Create the validator"
"""


def _make_task_with_ac(tmp_path: Path) -> object:
    """Create a task whose spec has an Acceptance Criteria section."""
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# Story\n\n## Acceptance Criteria\n\n- [ ] AC one\n- [ ] AC two unmapped\n",
        encoding="utf-8",
    )
    from theforge.task import TaskStory

    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


# ── validate_plan is called for structured plans ──────────────────────────────


@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_validation_runs_for_structured_plan(
    mock_validate_story,
    mock_shell,
    mock_preflight,
    mock_plan_agent,
    tmp_path: Path,
) -> None:
    """validate_plan is called when plan output parses as structured YAML."""
    from theforge.story_validator import StoryValidationResult

    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
    )
    mock_plan_agent.return_value = _make_agent_result(
        success=True, output=_STRUCTURED_PLAN, cost_usd=0.10
    )

    with patch("theforge.plan_validator.validate_plan") as mock_validate:
        mock_validate.return_value = []
        result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is True
    mock_validate.assert_called_once()


# ── freeform plan → skipped ───────────────────────────────────────────────────


@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_validation_skipped_for_freeform_plan(
    mock_validate_story,
    mock_shell,
    mock_preflight,
    mock_plan_agent,
    tmp_path: Path,
) -> None:
    """validate_plan is NOT called when plan output is freeform markdown."""
    from theforge.story_validator import StoryValidationResult

    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
    )
    mock_plan_agent.return_value = _make_agent_result(
        success=True, output=_FREEFORM_PLAN, cost_usd=0.10
    )

    with patch("theforge.plan_validator.validate_plan") as mock_validate:
        result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is True
    mock_validate.assert_not_called()
    # Freeform plan → plan_structured is None → no validation findings stored
    assert result.state.plan_validation_findings == []


# ── findings stored on state ──────────────────────────────────────────────────


@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_validation_findings_stored_on_state(
    mock_validate_story,
    mock_shell,
    mock_preflight,
    mock_plan_agent,
    tmp_path: Path,
) -> None:
    """Findings from validate_plan are stored on state.plan_validation_findings."""
    from theforge.story_validator import StoryValidationResult

    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
    )
    mock_plan_agent.return_value = _make_agent_result(
        success=True, output=_STRUCTURED_PLAN, cost_usd=0.10
    )

    sentinel_finding = {"check": "ac_coverage", "message": "AC unmapped"}

    with patch("theforge.plan_validator.validate_plan") as mock_validate:
        mock_validate.return_value = [sentinel_finding]
        result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is True
    assert result.state.plan_validation_findings == [sentinel_finding]


# ── advisory — never blocks DEV ───────────────────────────────────────────────


@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_validation_never_blocks_dev(
    mock_validate_story,
    mock_shell,
    mock_preflight,
    mock_plan_agent,
    tmp_path: Path,
) -> None:
    """Plan validation findings are advisory — run continues to DEV even with findings."""
    from theforge.story_validator import StoryValidationResult

    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
    )
    mock_plan_agent.return_value = _make_agent_result(
        success=True, output=_STRUCTURED_PLAN, cost_usd=0.10
    )

    # Many findings — validation should still not block
    many_findings = [
        {"check": "ac_coverage", "message": "AC 1 unmapped"},
        {"check": "ac_coverage", "message": "AC 2 unmapped"},
        {"check": "step_completeness", "message": "Step 1: missing description"},
        {"check": "file_validity", "message": "Step 1: nonexistent.py not found"},
        {"check": "dependency_ordering", "message": "Step 1: depends_on references unknown 99"},
    ]

    with patch("theforge.plan_validator.validate_plan") as mock_validate:
        mock_validate.return_value = many_findings
        # Stop at PLAN: if plan validation blocked, success would be False
        result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is True
    assert result.phase == Phase.PLAN
    assert len(result.state.plan_validation_findings) == 5


# ── AC coverage check via real validator ─────────────────────────────────────


@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_validation_ac_coverage_finding_for_unmapped_criteria(
    mock_validate_story,
    mock_shell,
    mock_preflight,
    mock_plan_agent,
    tmp_path: Path,
) -> None:
    """AC coverage check emits a finding for criteria not in criteria_mapping."""
    from theforge.story_validator import StoryValidationResult

    config = _make_plan_config(tmp_path)
    task = _make_task_with_ac(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
    )
    # Plan maps "AC one" but not "AC two unmapped"
    mock_plan_agent.return_value = _make_agent_result(
        success=True, output=_STRUCTURED_PLAN, cost_usd=0.10
    )

    result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is True
    ac_findings = [f for f in result.state.plan_validation_findings if f["check"] == "ac_coverage"]
    assert len(ac_findings) == 1
    assert "AC two unmapped" in ac_findings[0]["message"]
