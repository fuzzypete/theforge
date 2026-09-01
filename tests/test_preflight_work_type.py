"""Tests for work-type classification — preflight parsing, state storage,
plan-review gating, and audit log recording."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import (
    _parse_preflight_contract_change,
    _parse_preflight_work_type,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.task import TaskStory, build_plan_prompt, build_preflight_prompt

# ── Shared fixtures ───────────────────────────────────────────────────


def _make_plan_agent_review_config(tmp_path: Path, *, dual_reviewer: bool = False) -> ForgeConfig:
    """Test config with plan + plan_agent_review enabled.

    When ``dual_reviewer=True``, configures two plan review profiles so that
    corroboration can detect cross-reviewer agreement.
    """
    if dual_reviewer:
        _pr_a = ModelProfile(
            name="plan-review-a",
            cli="claude",
            model="sonnet",
            budget_usd=0.50,
            timeout_seconds=300,
            allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
        )
        _pr_b = ModelProfile(
            name="plan-review-b",
            cli="claude",
            model="sonnet",
            budget_usd=0.50,
            timeout_seconds=300,
            allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
        )
        par_config = PlanAgentReviewConfig.of(enabled=True, pool=[_pr_a, _pr_b])
    else:
        par_config = PlanAgentReviewConfig.of(enabled=True, cli="claude", model="sonnet")
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            preflight_complexity_gate_threshold=11,
        ),
        plan=PlanConfig.of(enabled=True, budget_usd=0.50, timeout=300),
        plan_agent_review=par_config,
    )


# ── Parser unit tests ─────────────────────────────────────────────────


class TestParsePreflightWorkType:
    def test_nested_fence_inside_reason_preserves_work_type(self):
        output = """\
```yaml
verdict: PROCEED
reason: |
  Example:
    ```python
    rename("old", "new")
    ```
work_type: refactor
```
"""
        assert _parse_preflight_work_type(output) == "refactor"

    def test_shorter_inner_fence_in_longer_wrapper_does_not_truncate_to_later_work_type(self):
        output = """\
````yaml
verdict: PROCEED
reason: "outer fence must survive inner snippet"
```python
rename("old", "new")
```
work_type: refactor
````
"""
        assert _parse_preflight_work_type(output) == "feature"

    def test_feature(self):
        output = """\
```yaml
verdict: PROCEED
complexity: medium
work_type: feature
reason: "New capability."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_work_type(output) == "feature"

    def test_refactor(self):
        output = """\
```yaml
verdict: PROCEED
complexity: large
work_type: refactor
reason: "Structural reorganization."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_work_type(output) == "refactor"

    def test_mechanical(self):
        output = """\
```yaml
verdict: PROCEED
complexity: small
work_type: mechanical
reason: "Rename only."
sufficiency: implementation_ready
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_work_type(output) == "mechanical"

    def test_bug(self):
        output = """\
```yaml
verdict: PROCEED
complexity: small
work_type: bug
reason: "Fix broken behavior."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_work_type(output) == "bug"

    def test_missing_field_defaults_to_feature(self):
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
        assert _parse_preflight_work_type(output) == "feature"

    def test_unrecognized_value_defaults_to_feature(self):
        output = """\
```yaml
verdict: PROCEED
complexity: medium
work_type: totally_invalid
reason: "Not implemented."
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_work_type(output) == "feature"

    def test_malformed_yaml_defaults_to_feature(self):
        output = "```yaml\n{{{ not valid yaml at all\n```"
        assert _parse_preflight_work_type(output) == "feature"

    def test_no_fences_still_parses(self):
        output = "verdict: PROCEED\nwork_type: refactor\n"
        assert _parse_preflight_work_type(output) == "refactor"


# ── Preflight prompt surface tests ────────────────────────────────────


class TestPreflightPromptWorkType:
    def test_prompt_includes_work_type_instructions(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_preflight_prompt(task, story_content="# Test\n\nDo something.")
        assert "work_type" in prompt
        assert "feature" in prompt
        assert "refactor" in prompt
        assert "mechanical" in prompt
        assert "bug" in prompt
        assert "Work-Type Classification" in prompt

    def test_prompt_output_format_includes_work_type_field(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_preflight_prompt(task, story_content="# Test\n\nDo something.")
        # The output format YAML template should list the field
        assert "work_type: feature | refactor | mechanical | bug" in prompt


# ── Plan prompt depth tests ────────────────────────────────────────────


class TestPlanPromptDepth:
    def test_refactor_injects_high_level_constraint(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_plan_prompt(
            task, story_content="# Test\n\nDo something.", work_type="refactor"
        )
        assert "high-level file" in prompt
        assert "Plan Depth Constraint" in prompt

    def test_mechanical_injects_high_level_constraint(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_plan_prompt(
            task, story_content="# Test\n\nDo something.", work_type="mechanical"
        )
        assert "high-level file" in prompt
        assert "Plan Depth Constraint" in prompt

    def test_feature_no_depth_constraint(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_plan_prompt(
            task, story_content="# Test\n\nDo something.", work_type="feature"
        )
        assert "Plan Depth Constraint" not in prompt

    def test_bug_no_depth_constraint(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_plan_prompt(task, story_content="# Test\n\nDo something.", work_type="bug")
        assert "Plan Depth Constraint" not in prompt

    def test_none_work_type_no_depth_constraint(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_plan_prompt(task, story_content="# Test\n\nDo something.", work_type=None)
        assert "Plan Depth Constraint" not in prompt


# ── Preflight fixtures for integration tests ─────────────────────────

PREFLIGHT_MECHANICAL = """\
```yaml
verdict: PROCEED
complexity: small
work_type: mechanical
reason: "Rename only — zero judgment required."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not renamed yet"
```
"""

PREFLIGHT_REFACTOR = """\
```yaml
verdict: PROCEED
complexity: large
work_type: refactor
reason: "Structural reorganization — no behavior change."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not reorganized yet"
```
"""

PREFLIGHT_FEATURE = """\
```yaml
verdict: PROCEED
complexity: medium
work_type: feature
reason: "New capability needed."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not implemented yet"
```
"""

PLAN_AGENT_REJECT = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "Plan references nonexistent function parse_config()"
    suggestion: "Use load_config() from config.py instead"
```
"""

PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""


# ── Plan review gating tests ─────────────────────────────────────────


class TestMechanicalSkipsPlanReview:
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_mechanical_skips_plan_review(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_review_pool,
        mock_review_pool,
        mock_human_review,
        tmp_path,
    ):
        """For mechanical work type, plan review pool is NOT invoked."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_MECHANICAL, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: rename the file.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_review_pool.return_value = []
        mock_review_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # plan_review_pool should NOT have been called (mechanical skips review)
        assert mock_plan_review_pool.call_count == 0
        # work_type stored on state
        assert result.state.preflight_work_type == "mechanical"
        # plan_review_decision should remain None (not set to "approve")
        assert result.state.plan_review_decision is None


class TestRefactorAdvisoryPlanReview:
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_refactor_advisory_does_not_reject(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_review_pool,
        mock_review_pool,
        mock_human_review,
        tmp_path,
    ):
        """For refactor work type, even a P1 REJECT from the reviewer does not block."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_REFACTOR, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: reorganize modules.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # Plan reviewer returns REJECT with P1 — but advisory mode should ignore it
        mock_plan_review_pool.return_value = [
            _make_agent_result(success=True, output=PLAN_AGENT_REJECT, profile_name="reviewer")
        ]
        mock_review_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Should succeed even though plan reviewer rejected
        assert result.success is True
        # Plan review pool WAS called
        assert mock_plan_review_pool.call_count == 1
        # Dev ran exactly once (no regen loop)
        assert mock_dev_agent.call_count == 1
        assert result.state.preflight_work_type == "refactor"

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_refactor_regen_loop_not_entered(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_review_pool,
        mock_review_pool,
        mock_human_review,
        tmp_path,
    ):
        """Refactor advisory: plan review runs once only, no regeneration."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_REFACTOR, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: reorganize.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_review_pool.return_value = [
            _make_agent_result(success=True, output=PLAN_AGENT_REJECT, profile_name="reviewer")
        ]
        mock_review_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Plan regen count should be 0 — advisory never triggers regen
        assert result.state.plan_regen_count == 0
        # Reviewer ran once
        assert mock_plan_review_pool.call_count == 1


class TestFeatureBugFullPlanReview:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_feature_reviewer_reject_triggers_regen(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_review_pool,
        mock_review_pool,
        tmp_path,
    ):
        """Corroborated REJECT (2 reviewers) triggers plan regeneration."""
        config = _make_plan_agent_review_config(tmp_path, dual_reviewer=True)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_FEATURE, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: implement feature.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # First review: two reviewers raise same P1 (corroborated → REJECT); second: APPROVE
        mock_plan_review_pool.side_effect = [
            [
                _make_agent_result(
                    success=True, output=PLAN_AGENT_REJECT, profile_name="plan-review-a"
                ),
                _make_agent_result(
                    success=True, output=PLAN_AGENT_REJECT, profile_name="plan-review-b"
                ),
            ],
            [
                _make_agent_result(
                    success=True, output=PLAN_AGENT_APPROVE, profile_name="plan-review-a"
                ),
                _make_agent_result(
                    success=True, output=PLAN_AGENT_APPROVE, profile_name="plan-review-b"
                ),
            ],
        ]
        mock_review_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Plan review pool was called twice (corroborated reject → regen → approve)
        assert mock_plan_review_pool.call_count == 2
        # Plan was regenerated once
        assert result.state.plan_regen_count == 1


# ── State storage test ────────────────────────────────────────────────


class TestWorkTypeStoredOnState:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_work_type_stored_on_state(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """preflight_work_type is stored on CoordinatorState after preflight."""
        from coord_test_helpers import _make_plan_config

        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_FEATURE, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: implement feature.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_work_type == "feature"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_failure_defaults_to_feature(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """When preflight agent fails, preflight_work_type defaults to 'feature'."""
        from coord_test_helpers import _make_plan_config

        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=False, output="Error: agent crashed.", cost_usd=0.01
        )
        plan_result = _make_agent_result(success=True, output="# Plan\n\nStep 1.", cost_usd=0.10)
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        results = [plan_result, dev_result]

        def side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_preflight.return_value = preflight_result
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = side_effect
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_work_type == "feature"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_nested_fence_in_preflight_reason_reaches_state(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """Quoted fenced examples in preflight YAML must not truncate later fields."""
        from coord_test_helpers import _make_plan_config

        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_output = """\
```yaml
verdict: PROCEED
reason: |
  Example:
    ```python
    rename("old", "new")
    ```
work_type: refactor
contract_change: true
complexity: small
sufficiency: implementation_ready
```
"""
        preflight_result = _make_agent_result(success=True, output=preflight_output, cost_usd=0.02)
        plan_result = _make_agent_result(success=True, output="# Plan\n\nStep 1.", cost_usd=0.10)
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        call_idx = {"n": 0}
        results = [plan_result, dev_result]

        def side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_preflight.return_value = preflight_result
        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = side_effect
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_work_type == "refactor"
        assert result.state.preflight_contract_change is True
        assert result.state.preflight_complexity == "medium"
        assert result.state.preflight_sufficiency == "needs_planning"


# ── Refactor + human plan review (advisory) ──────────────────────────


def _make_human_plan_review_config(tmp_path: Path) -> ForgeConfig:
    """Test config with plan + human plan_review enabled (no agent review)."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            preflight_complexity_gate_threshold=11,
        ),
        plan=PlanConfig.of(enabled=True, budget_usd=0.50, timeout=300),
        plan_review=PlanReviewConfig(enabled=True, mode="blocking", timeout_seconds=300),
    )


class TestRefactorHumanPlanReviewAdvisory:
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_refactor_human_review_runs_advisory(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_interactive,
        mock_human_review,
        tmp_path,
    ):
        """Refactor + human plan review: review IS run (not skipped), continues to DEV."""
        config = _make_human_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_REFACTOR, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: reorganize modules.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_interactive.return_value = "approve"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Human plan review was invoked (not skipped)
        assert mock_interactive.called
        assert result.state.preflight_work_type == "refactor"

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_refactor_human_review_rejection_not_blocking(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_interactive,
        mock_human_review,
        tmp_path,
    ):
        """Refactor + human plan review: human rejection is advisory — does not stop pipeline."""
        config = _make_human_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_REFACTOR, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: reorganize modules.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # Human says "regenerate" repeatedly until max attempts exhausted → abandon
        mock_interactive.return_value = "regenerate"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Pipeline should continue to DEV despite human rejection (advisory mode)
        assert result.success is True
        # Human review was invoked
        assert mock_interactive.called
        assert result.state.preflight_work_type == "refactor"

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_refactor_human_explicit_abandon_is_terminal(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_interactive,
        mock_human_review,
        tmp_path,
    ):
        """Refactor + human plan review: explicit 'abandon' stops the pipeline."""
        config = _make_human_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_REFACTOR, cost_usd=0.02
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: reorganize modules.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

        mock_preflight.return_value = preflight_result
        mock_plan_agent.return_value = plan_result
        mock_dev_agent.return_value = dev_result
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # Human explicitly abandons — this should stop the run even in advisory mode
        mock_interactive.return_value = "abandon"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Explicit abandon must stop the pipeline — not advisory
        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert "abandoned by human" in result.message
        # Dev agent should NOT have run
        assert mock_dev_agent.call_count == 0


# ── Audit log test ────────────────────────────────────────────────────


class TestAuditLogIncludesWorkType:
    def test_audit_log_includes_work_type(self, tmp_path: Path):
        """generate_audit_log includes work_type in the preflight section."""
        from theforge.config import (
            DEFAULT_DEV_PROFILE,
            DEFAULT_PREFLIGHT_PROFILE,
            DEFAULT_REVIEW_PROFILE,
            WorkspaceConfig,
        )

        spec_path = tmp_path / "spec.md"
        spec_path.write_text("# Test spec", encoding="utf-8")
        task = TaskStory(name="Test Task", slug="test-task", story_path=spec_path)

        config = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(),
        )

        state = CoordinatorState(
            preflight_verdict="PROCEED",
            preflight_reason="Not implemented.",
            preflight_work_type="refactor",
            preflight_result=_make_agent_result(success=True, output="...", cost_usd=0.01),
        )
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
        )

        audit = generate_audit_log(config, task, result)

        assert audit["preflight"] is not None
        assert "work_type" in audit["preflight"]
        assert audit["preflight"]["work_type"] == "refactor"

    def test_audit_log_work_type_none_when_preflight_absent(self, tmp_path: Path):
        """When preflight_verdict is None, the entire preflight section is None."""
        from theforge.config import (
            DEFAULT_DEV_PROFILE,
            DEFAULT_PREFLIGHT_PROFILE,
            DEFAULT_REVIEW_PROFILE,
            WorkspaceConfig,
        )

        spec_path = tmp_path / "spec.md"
        spec_path.write_text("# Test spec", encoding="utf-8")
        task = TaskStory(name="Test Task", slug="test-task", story_path=spec_path)

        config = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(),
        )

        state = CoordinatorState()  # no preflight at all
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
        )

        audit = generate_audit_log(config, task, result)

        assert audit["preflight"] is None


# ── contract_change parser tests ─────────────────────────────────────


class TestParsePreflightContractChange:
    def test_nested_fence_inside_reason_preserves_contract_change(self):
        output = """\
```yaml
verdict: PROCEED
reason: |
  Example:
    ```python
    print("nested")
    ```
contract_change: true
```
"""
        assert _parse_preflight_contract_change(output) is True

    def test_true_when_set(self):
        output = """\
```yaml
verdict: PROCEED
complexity: small
work_type: bug
contract_change: true
reason: "Behavioral contract change."
sufficiency: implementation_ready
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_contract_change(output) is True

    def test_false_when_explicitly_false(self):
        output = """\
```yaml
verdict: PROCEED
complexity: medium
work_type: feature
contract_change: false
reason: "New capability."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_contract_change(output) is False

    def test_false_when_field_absent(self):
        output = """\
```yaml
verdict: PROCEED
complexity: medium
work_type: feature
reason: "New capability."
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked: []
```
"""
        assert _parse_preflight_contract_change(output) is False

    def test_false_on_malformed_yaml(self):
        assert _parse_preflight_contract_change("```yaml\n{{{ not valid\n```") is False

    def test_no_fences_still_parses(self):
        output = "verdict: PROCEED\ncontract_change: true\n"
        assert _parse_preflight_contract_change(output) is True

    def test_string_false_does_not_become_true(self):
        """Quoted 'false' must not be treated as truthy via bool()."""
        output = 'contract_change: "false"\n'
        assert _parse_preflight_contract_change(output) is False

    def test_string_zero_does_not_become_true(self):
        """Quoted '0' must not be treated as truthy via bool()."""
        output = 'contract_change: "0"\n'
        assert _parse_preflight_contract_change(output) is False

    def test_string_true_is_accepted(self):
        """Quoted 'true' (unusual but possible) should be accepted."""
        output = 'contract_change: "true"\n'
        assert _parse_preflight_contract_change(output) is True

    def test_integer_one_does_not_become_true(self):
        """Numeric 1 should not be accepted — only boolean True."""
        output = "contract_change: 1\n"
        assert _parse_preflight_contract_change(output) is False


# ── Preflight prompt includes contract_change field ───────────────────


class TestPreflightPromptContractChange:
    def test_prompt_includes_contract_change_field(self, tmp_path: Path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo something.", encoding="utf-8")
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        prompt = build_preflight_prompt(task, story_content="# Test\n\nDo something.")
        assert "contract_change" in prompt

    def test_audit_log_includes_contract_change(self, tmp_path: Path):
        """generate_audit_log includes contract_change in the preflight section."""
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("# Test spec", encoding="utf-8")
        task = TaskStory(name="Test Task", slug="test-task", story_path=spec_path)

        config = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(),
        )

        state = CoordinatorState(
            preflight_verdict="PROCEED",
            preflight_reason="Contract changed.",
            preflight_work_type="bug",
            preflight_contract_change=True,
            preflight_result=_make_agent_result(success=True, output="...", cost_usd=0.01),
        )
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
        )

        audit = generate_audit_log(config, task, result)

        assert audit["preflight"] is not None
        assert "contract_change" in audit["preflight"]
        assert audit["preflight"]["contract_change"] is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_contract_change_in_preflight_artifact(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """preflight.yaml artifact includes contract_change field."""
        import dataclasses

        import yaml as _yaml
        from coord_test_helpers import _make_plan_config

        from theforge.config import LogConfig

        config = _make_plan_config(tmp_path)
        config = dataclasses.replace(config, log=LogConfig(enabled=True))
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        _preflight_output = """\
```yaml
verdict: PROCEED
complexity: small
work_type: bug
contract_change: true
reason: "Changes the error-to-escalation contract."
sufficiency: implementation_ready
sufficiency_reason: "Spec is concrete."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "ESCALATE instead of error"
    satisfied: false
    evidence: "Still returns error"
```
"""
        preflight_result = _make_agent_result(
            success=True, output=_preflight_output, cost_usd=0.03
        )
        # contract_change=true forces complexity→medium and sufficiency→needs_planning,
        # so the plan phase runs. Provide a plan result so the mock chain completes.
        plan_result = _make_agent_result(
            success=True,
            output="# Plan\n\nStep 1: update all references.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.30)

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
        log_dir = result.state.log_dir
        assert log_dir is not None
        preflight_yaml = log_dir / "preflight.yaml"
        assert preflight_yaml.exists(), f"preflight.yaml not found in {log_dir}"
        artifact = _yaml.safe_load(preflight_yaml.read_text(encoding="utf-8"))
        assert "contract_change" in artifact
        assert artifact["contract_change"] is True
