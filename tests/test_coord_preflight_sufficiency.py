"""Tests for preflight spec sufficiency classification and pipeline behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import _parse_preflight_sufficiency
from theforge.task import TaskStory, build_preflight_prompt

# ── Parser unit tests ─────────────────────────────────────────────────


class TestParsePreflightSufficiency:
    def test_parse_sufficiency_implementation_ready(self):
        """YAML with sufficiency: implementation_ready returns 'implementation_ready'."""
        output = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Not yet implemented."
sufficiency: implementation_ready
sufficiency_reason: "Spec has detailed Notes with file paths and function names."
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_sufficiency(output) == "implementation_ready"

    def test_parse_sufficiency_needs_planning(self):
        """YAML with sufficiency: needs_planning returns 'needs_planning'."""
        output = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Not yet implemented."
sufficiency: needs_planning
sufficiency_reason: "Spec is vague — no Notes, minimal ACs."
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_sufficiency(output) == "needs_planning"

    def test_parse_sufficiency_missing_defaults_to_needs_planning(self):
        """YAML without a sufficiency field defaults to 'needs_planning'."""
        output = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Not yet implemented."
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_sufficiency(output) == "needs_planning"

    def test_parse_sufficiency_invalid_defaults_to_needs_planning(self):
        """YAML with an invalid sufficiency value defaults to 'needs_planning'."""
        output = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Not yet implemented."
sufficiency: totally_invalid_value
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_sufficiency(output) == "needs_planning"

    def test_parse_sufficiency_broken_yaml_defaults_to_needs_planning(self):
        """Broken YAML defaults to 'needs_planning'."""
        output = "```yaml\n{{{ not valid yaml at all\n```"
        assert _parse_preflight_sufficiency(output) == "needs_planning"

    def test_parse_sufficiency_no_fences_still_parses(self):
        """YAML without markdown fences is still parsed."""
        output = "verdict: PROCEED\nsufficiency: implementation_ready\n"
        assert _parse_preflight_sufficiency(output) == "implementation_ready"


# ── Preflight prompt surface test ─────────────────────────────────────


class TestPreflightPromptSufficiency:
    def test_prompt_includes_sufficiency_instructions(self, tmp_path: Path):
        """build_preflight_prompt includes sufficiency classification instructions."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_preflight_prompt(task, story_content="# Test\n\nDo something.")
        assert "sufficiency" in prompt
        assert "implementation_ready" in prompt
        assert "needs_planning" in prompt
        assert "Spec Sufficiency Classification" in prompt


# ── Pipeline behavior tests ───────────────────────────────────────────

_PREFLIGHT_IMPL_READY_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Spec requirements are not yet implemented."
sufficiency: implementation_ready
sufficiency_reason: "Spec has comprehensive Notes with file paths and function names."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

_PREFLIGHT_NEEDS_PLANNING_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Spec requirements are not yet implemented."
sufficiency: needs_planning
sufficiency_reason: "Spec lacks Notes and has vague acceptance criteria."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

_PREFLIGHT_FAILED_NO_SUFFICIENCY = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Spec requirements are not yet implemented."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""


class TestPlanSkipWhenImplementationReady:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_skipped_when_implementation_ready(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Plan agent is NOT invoked when spec is classified as implementation_ready."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_IMPL_READY_MEDIUM, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        mock_preflight.return_value = preflight_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Plan agent should NOT have been called
        assert mock_plan_agent.call_count == 0
        # Dev agent should have run
        assert mock_dev_agent.call_count == 1
        # Plan output should be None (plan was skipped)
        assert result.state.plan_output is None
        # Sufficiency stored on state
        assert result.state.preflight_sufficiency == "implementation_ready"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_runs_when_needs_planning(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Plan agent IS invoked when spec is classified as needs_planning."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_NEEDS_PLANNING_MEDIUM, cost_usd=0.05
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Implementation Plan\n\nStep 1: do the thing.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Plan agent + Dev agent = 2 calls
        assert mock_dev_agent.call_count == 2
        assert result.state.plan_output is not None
        assert result.state.preflight_sufficiency == "needs_planning"


class TestSufficiencyInPreflightArtifact:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_sufficiency_in_preflight_artifact(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Preflight.yaml artifact written to log dir includes the sufficiency field."""
        import dataclasses

        import yaml as _yaml

        from theforge.config import LogConfig

        config = _make_plan_config(tmp_path)
        config = dataclasses.replace(config, log=LogConfig(enabled=True))
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_IMPL_READY_MEDIUM, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        mock_preflight.return_value = preflight_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Find the preflight.yaml in the log dir
        log_dir = result.state.log_dir
        assert log_dir is not None
        preflight_yaml = log_dir / "preflight.yaml"
        assert preflight_yaml.exists(), f"preflight.yaml not found in {log_dir}"
        artifact = _yaml.safe_load(preflight_yaml.read_text(encoding="utf-8"))
        assert "sufficiency" in artifact
        assert artifact["sufficiency"] == "implementation_ready"


class TestPreflightFailureSetsNeedsPlanning:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_failure_sets_needs_planning(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """When preflight agent fails, state.preflight_sufficiency is 'needs_planning'."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Preflight fails
        preflight_result = _make_agent_result(
            success=False, output="Error: agent crashed.", cost_usd=0.01
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        # After failed preflight + fallback, the plan agent runs (complexity="large")
        # then dev runs
        plan_result = _make_agent_result(
            success=True,
            output="# Implementation Plan\n\nStep 1.",
            cost_usd=0.10,
        )
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Sufficiency should default to needs_planning on preflight failure
        assert result.state.preflight_sufficiency == "needs_planning"


# ── Contract-change override tests ────────────────────────────────────

_PREFLIGHT_CONTRACT_CHANGE_SMALL_IMPL_READY = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Remove gate_result field from dev prompt output."
sufficiency: implementation_ready
sufficiency_reason: "Spec has detailed Notes with file paths."
contract_change: true
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "gate_result removed from output"
    satisfied: false
    evidence: "Field still present"
```
"""

_PREFLIGHT_NO_CONTRACT_CHANGE_IMPL_READY = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Add a log message."
sufficiency: implementation_ready
sufficiency_reason: "Spec is very clear and narrow."
contract_change: false
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Log message added"
    satisfied: false
    evidence: "Not found"
```
"""

_PREFLIGHT_CONTRACT_CHANGE_MEDIUM_NEEDS_PLANNING = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Rename shared field across coordinator."
sufficiency: needs_planning
sufficiency_reason: "No Notes, approach unclear."
contract_change: true
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Field renamed"
    satisfied: false
    evidence: "Not yet done"
```
"""


class TestContractChangeOverridesState:
    """Coordinator enforces needs_planning and medium complexity for contract changes."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_contract_change_impl_ready_overridden_to_needs_planning(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """contract_change=true + implementation_ready → coordinator forces needs_planning."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_CONTRACT_CHANGE_SMALL_IMPL_READY, cost_usd=0.05
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: update all references.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Coordinator must have overridden to needs_planning
        assert result.state.preflight_sufficiency == "needs_planning"
        # Coordinator must have upgraded complexity so the plan phase can run
        assert result.state.preflight_complexity == "medium"
        # Plan agent must have run (contract change forces planning)
        assert mock_dev_agent.call_count == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_no_contract_change_impl_ready_not_overridden(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """contract_change=false + implementation_ready → no override; plan stays skipped."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_NO_CONTRACT_CHANGE_IMPL_READY, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        mock_preflight.return_value = preflight_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # No override — stays implementation_ready
        assert result.state.preflight_sufficiency == "implementation_ready"
        # Complexity unchanged
        assert result.state.preflight_complexity == "small"
        # Plan agent NOT called
        assert mock_plan_agent.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_contract_change_needs_planning_already_unchanged(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """contract_change=true + needs_planning already → no state change; plan runs."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True,
            output=_PREFLIGHT_CONTRACT_CHANGE_MEDIUM_NEEDS_PLANNING,
            cost_usd=0.05,
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: enumerate references.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_sufficiency == "needs_planning"
        assert result.state.preflight_complexity == "medium"
        assert mock_dev_agent.call_count == 2


class TestPreflightPromptContractChangeGuidance:
    """Preflight prompt includes cross-cutting contract change guidance."""

    def test_prompt_includes_contract_change_needs_planning_criteria(self, tmp_path: Path):
        """build_preflight_prompt includes contract change as a needs_planning trigger."""
        from theforge.task import build_preflight_prompt
        from theforge.task.story import TaskStory

        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_preflight_prompt(task, story_content="# Test\n\nDo something.")
        assert "contract change" in prompt.lower()
        assert "blast radius" in prompt.lower()
        assert "exact-string" in prompt.lower() or "exact-string" in prompt


# ── Bounded-bug planning skip tests ───────────────────────────────────

_PREFLIGHT_BUG_SMALL_NEEDS_PLANNING = """\
```yaml
verdict: PROCEED
complexity: small
complexity_score: 3
reason: "Status display reads wrong field — single-area bug fix."
work_type: bug
sufficiency: needs_planning
sufficiency_reason: "Spec lacks Notes section."
contract_change: false
spec_issues: []
warnings: []
likely_files:
  - src/foo.py
criteria_checked:
  - criterion: "Status displays the right field"
    satisfied: false
    evidence: "Reads stale value"
```
"""

_PREFLIGHT_FEATURE_SMALL_NEEDS_PLANNING = """\
```yaml
verdict: PROCEED
complexity: small
complexity_score: 3
reason: "New small feature."
work_type: feature
sufficiency: needs_planning
sufficiency_reason: "Spec lacks Notes section."
contract_change: false
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature added"
    satisfied: false
    evidence: "Not present"
```
"""

_PREFLIGHT_BUG_MEDIUM_NEEDS_PLANNING = """\
```yaml
verdict: PROCEED
complexity: medium
complexity_score: 6
reason: "Multi-module bug fix."
work_type: bug
sufficiency: needs_planning
sufficiency_reason: "Multiple modules involved."
contract_change: false
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Fix applied"
    satisfied: false
    evidence: "Not yet"
```
"""


class TestBoundedBugPlanSkip:
    """Coordinator skips planning for bounded bugs (work_type=bug, small, not contract_change)."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_bounded_bug_overrides_to_implementation_ready(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """work_type=bug + complexity=small + not contract_change → implementation_ready."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_BUG_SMALL_NEEDS_PLANNING, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        mock_preflight.return_value = preflight_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Coordinator overrode needs_planning → implementation_ready
        assert result.state.preflight_sufficiency == "implementation_ready"
        # Plan agent must NOT have run
        assert mock_plan_agent.call_count == 0
        # Dev runs once
        assert mock_dev_agent.call_count == 1
        assert result.state.plan_output is None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_feature_small_not_overridden(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """work_type=feature + small + needs_planning → no override; plan still runs."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_FEATURE_SMALL_NEEDS_PLANNING, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        mock_preflight.return_value = preflight_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # No bounded-bug override (work_type != bug). Sufficiency stays needs_planning.
        assert result.state.preflight_sufficiency == "needs_planning"
        # complexity=small means plan_flow gates plan off (gate is medium/large only).
        # The new override only fires for bugs; features keep their original behavior.
        assert mock_plan_agent.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_medium_bug_not_overridden(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """work_type=bug + complexity=medium → no override; plan runs."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=_PREFLIGHT_BUG_MEDIUM_NEEDS_PLANNING, cost_usd=0.05
        )
        plan_result = _make_agent_result(success=True, output="# Plan\n\nStep 1.", cost_usd=0.10)
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Medium bugs may genuinely need decomposition — no override.
        assert result.state.preflight_sufficiency == "needs_planning"
        assert mock_dev_agent.call_count == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_contract_change_bug_keeps_planning(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """contract_change=true beats the bounded-bug skip — plan runs."""
        contract_bug_yaml = """\
```yaml
verdict: PROCEED
complexity: small
complexity_score: 3
reason: "Bug fix that renames a shared field."
work_type: bug
sufficiency: needs_planning
sufficiency_reason: "Field rename across coordinator."
contract_change: true
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Field renamed"
    satisfied: false
    evidence: "Old name still in use"
```
"""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=contract_bug_yaml, cost_usd=0.05
        )
        plan_result = _make_agent_result(
            success=True, output="# Plan\n\nStep 1: enumerate.", cost_usd=0.10
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # contract_change forces needs_planning AND upgrades complexity to medium.
        # The bounded-bug override does not fire because complexity is no longer small.
        assert result.state.preflight_sufficiency == "needs_planning"
        assert result.state.preflight_complexity == "medium"
        assert mock_dev_agent.call_count == 2


class TestPreflightPromptDecompositionAnchor:
    """Preflight prompt anchors sufficiency on fix-shape decomposition, not spec quality."""

    def test_prompt_emphasises_decomposition_signal(self, tmp_path: Path):
        """The prompt must signal that decomposition need (not Notes density) drives the call."""
        from theforge.task import build_preflight_prompt
        from theforge.task.story import TaskStory

        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_preflight_prompt(task, story_content="# Test\n\nDo something.")
        lower = prompt.lower()
        assert "decomposition" in lower
        assert "fix-shape" in lower or "fix shape" in lower
        # The prompt must explicitly state that sparse Notes alone is not grounds.
        assert "sparse" in lower or "terse" in lower
        # implementation_ready must be presented as the default for bounded work.
        assert "default" in lower
