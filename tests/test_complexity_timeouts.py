"""Tests for complexity-based timeouts (resolve_timeout, DEV phase, PLAN phase, load_config)."""

import dataclasses
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import _as_detailed, _shell_with_gate, patch_gate_shell

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
    load_config,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.util import (
    LARGE_HEADROOM_FACTOR,
    MEDIUM_HEADROOM_FACTOR,
    resolve_timeout,
    resolve_timeout_with_active,
)
from theforge.runners import AgentResult
from theforge.task import TaskStory

# ── resolve_timeout ────────────────────────────────────────────────────


class TestResolveTimeout:
    def test_small_complexity_uses_base(self):
        assert resolve_timeout(600, 900, 1800, "small") == 600

    def test_same_band_scores_can_route_to_different_timeouts(self):
        assert resolve_timeout(600, 900, 1800, "medium", 5) == 600
        assert resolve_timeout(600, 900, 1800, "medium", 7) == 900

    def test_none_complexity_uses_base(self):
        assert resolve_timeout(600, 900, 1800, None) == 600

    def test_medium_complexity_uses_medium_override(self):
        assert resolve_timeout(600, 900, 1800, "medium") == 900

    def test_large_complexity_uses_large_override(self):
        assert resolve_timeout(600, 900, 1800, "large") == 1800

    def test_medium_override_absent_derives_from_base(self):
        assert resolve_timeout(600, None, 1800, "medium") == round(600 * MEDIUM_HEADROOM_FACTOR)

    def test_large_override_absent_derives_from_base(self):
        assert resolve_timeout(600, 900, None, "large") == round(600 * LARGE_HEADROOM_FACTOR)

    def test_large_score_without_large_override_derives_large_not_medium(self):
        derived = round(600 * LARGE_HEADROOM_FACTOR)
        assert resolve_timeout(600, 900, None, "large", 8) == derived
        assert resolve_timeout(600, 900, None, "large", 10) == derived

    def test_large_score_ignores_medium_override_when_large_override_missing(self):
        assert resolve_timeout(600, 900, None, "medium", 8) == round(600 * LARGE_HEADROOM_FACTOR)

    def test_both_overrides_absent_derives_for_medium_and_large(self):
        assert resolve_timeout(600, None, None, "large") == round(600 * LARGE_HEADROOM_FACTOR)
        assert resolve_timeout(600, None, None, "medium") == round(600 * MEDIUM_HEADROOM_FACTOR)
        assert resolve_timeout(600, None, None, "small") == 600
        assert resolve_timeout(600, None, None, None) == 600

    def test_unknown_complexity_falls_back_to_base(self):
        assert resolve_timeout(600, 900, 1800, "unknown") == 600

    def test_resolve_timeout_with_active_marks_derived_tiers_active(self):
        timeout, override_active = resolve_timeout_with_active(600, None, None, "large", 8)
        assert timeout == round(600 * LARGE_HEADROOM_FACTOR)
        assert override_active is True

        timeout, override_active = resolve_timeout_with_active(600, None, None, "medium", 6)
        assert timeout == round(600 * MEDIUM_HEADROOM_FACTOR)
        assert override_active is True

    def test_resolve_timeout_with_active_keeps_base_for_low_score(self):
        timeout, override_active = resolve_timeout_with_active(600, None, None, "medium", 3)
        assert timeout == 600
        assert override_active is False


# ── Shared test helpers ────────────────────────────────────────────────
#
# Gate/shell dispatch (including handoff writing and stale-worktree responses)
# lives in the single sanctioned seam in ``coord_test_helpers`` — this module
# imports ``_shell_with_gate``/``_as_detailed`` rather than re-defining them, so
# gate command dispatch is owned in exactly one place (see #1737).


PREFLIGHT_PROCEED_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
complexity_score: 6
reason: "Medium complexity spec."
spec_issues: []
criteria_checked: []
```
"""

PREFLIGHT_PROCEED_MEDIUM_SCORE_5 = """\
```yaml
verdict: PROCEED
complexity: medium
complexity_score: 5
reason: "Medium complexity spec."
spec_issues: []
criteria_checked: []
```
"""

PREFLIGHT_PROCEED_MEDIUM_SCORE_7 = """\
```yaml
verdict: PROCEED
complexity: medium
complexity_score: 7
reason: "Medium complexity spec."
spec_issues: []
criteria_checked: []
```
"""

PREFLIGHT_PROCEED_LARGE = """\
```yaml
verdict: PROCEED
complexity: large
reason: "Large complexity spec."
spec_issues: []
criteria_checked: []
```
"""

APPROVE_REVIEW = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
ac_verification:
  - criterion: "Implementation satisfies the spec"
    status: VERIFIED
    evidence: "diff hunks + tests (fixture default)"
```
"""


def _make_agent_result(
    success: bool = True,
    output: str = "Done.",
    session_id: str | None = "sess-1",
    cost_usd: float = 0.10,
    profile_name: str = "",
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=session_id,
        cost_usd=cost_usd,
        exit_code=0 if success else 1,
        raw={},
        profile_name=profile_name,
    )


def _make_config(
    tmp_path: Path,
    *,
    dev_profile: ModelProfile = DEFAULT_DEV_PROFILE,
    plan: PlanConfig = PlanConfig.of(validate_spec=False),
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
        dev_profile=dev_profile,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            # These tests drive `complexity: large` to exercise timeout derivation,
            # which now also reaches the preflight complexity gate. Disable the gate
            # (threshold above COMPLEXITY_SCORE_MAX) so they keep testing timeouts.
            preflight_complexity_gate_threshold=11,
        ),
        plan=plan,
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


def _get_call_profile(mock_agent, call_idx: int) -> ModelProfile:
    """Extract the 'profile' kwarg from a specific mock_agent call."""
    c = mock_agent.call_args_list[call_idx]
    return c[1]["profile"] if "profile" in c[1] else c[0][1]


# ── DEV phase timeout resolution ───────────────────────────────────────


class TestDevPhaseTimeout:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_uses_large_timeout_for_large_complexity(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """run_agent receives a profile with timeout_large_seconds when complexity=large."""
        dev_profile = dataclasses.replace(
            DEFAULT_DEV_PROFILE,
            timeout_seconds=600,
            timeout_medium_seconds=900,
            timeout_large_seconds=1800,
        )
        config = _make_config(tmp_path, dev_profile=dev_profile)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        dev_profile_used = _get_call_profile(mock_agent, 0)
        assert dev_profile_used.timeout_seconds == 1800

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_uses_medium_timeout_for_medium_complexity(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """run_agent receives a profile with timeout_medium_seconds when complexity=medium."""
        dev_profile = dataclasses.replace(
            DEFAULT_DEV_PROFILE,
            timeout_seconds=600,
            timeout_medium_seconds=900,
            timeout_large_seconds=1800,
        )
        config = _make_config(tmp_path, dev_profile=dev_profile)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        dev_profile_used = _get_call_profile(mock_agent, 0)
        assert dev_profile_used.timeout_seconds == 900

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_derives_large_timeout_when_no_override(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """run_agent derives a large timeout when no complexity overrides are set."""
        config = _make_config(tmp_path)  # DEFAULT_DEV_PROFILE has no medium/large overrides
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        dev_profile_used = _get_call_profile(mock_agent, 0)
        assert dev_profile_used.timeout_seconds == round(
            DEFAULT_DEV_PROFILE.timeout_seconds * LARGE_HEADROOM_FACTOR
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_logs_complexity_suffix_when_override_equals_base(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path, capsys
    ):
        """Complexity suffix appears in log even when override value equals base timeout."""
        base = 600
        dev_profile = dataclasses.replace(
            DEFAULT_DEV_PROFILE,
            timeout_seconds=base,
            timeout_medium_seconds=base,  # same value as base — bug trigger
            timeout_large_seconds=base,
        )
        config = _make_config(tmp_path, dev_profile=dev_profile)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        stderr = capsys.readouterr().err
        assert "Dev timeout: 600s (large complexity)" in stderr


# ── PLAN phase timeout resolution ──────────────────────────────────────


class TestScorePropagationSeam:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_same_band_scores_propagate_to_different_plan_and_dev_timeouts(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Preflight score propagates across phase boundaries and changes PLAN/DEV timeouts."""
        dev_profile = dataclasses.replace(
            DEFAULT_DEV_PROFILE,
            timeout_seconds=600,
            timeout_medium_seconds=900,
            timeout_large_seconds=1800,
        )
        plan = PlanConfig.of(
            enabled=True,
            timeout=600,
            timeout_medium=900,
            timeout_large=1800,
            validate_spec=False,
        )
        config = _make_config(tmp_path, dev_profile=dev_profile, plan=plan)
        task = _make_task(tmp_path)

        def _run_for_preflight_output(preflight_output: str) -> tuple[int, int]:
            workspace = tmp_path / f"test-task-{abs(hash(preflight_output))}"
            workspace.mkdir()
            task_for_run = TaskStory(
                name="Test Task",
                story_path=task.story_path,
                slug=workspace.name,
            )
            mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
            mock_plan_agent.side_effect = mock_agent
            mock_agent.reset_mock()
            mock_plan_agent.reset_mock()
            mock_preflight.reset_mock()
            mock_pool.reset_mock()
            mock_preflight.return_value = _make_agent_result(
                output=preflight_output,
                cost_usd=0.05,
            )
            mock_agent.side_effect = [
                _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
                _make_agent_result(output="Implemented.", cost_usd=0.20),
            ]
            mock_pool.return_value = [
                _make_agent_result(output=APPROVE_REVIEW, profile_name="review")
            ]

            result = run_task(config, task_for_run)

            assert result.success is True
            plan_profile_used = _get_call_profile(mock_agent, 0)
            dev_profile_used = _get_call_profile(mock_agent, 1)
            return plan_profile_used.timeout_seconds, dev_profile_used.timeout_seconds

        plan_timeout_5, dev_timeout_5 = _run_for_preflight_output(PREFLIGHT_PROCEED_MEDIUM_SCORE_5)
        plan_timeout_7, dev_timeout_7 = _run_for_preflight_output(PREFLIGHT_PROCEED_MEDIUM_SCORE_7)

        assert plan_timeout_5 == 600
        assert dev_timeout_5 == 600
        assert plan_timeout_7 == 900
        assert dev_timeout_7 == 900


class TestPlanPhaseTimeout:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_uses_large_timeout_for_large_complexity(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """PLAN phase profile uses timeout_large when complexity=large."""
        plan = PlanConfig.of(
            enabled=True,
            timeout=600,
            timeout_medium=900,
            timeout_large=1800,
            validate_spec=False,
        )
        config = _make_config(tmp_path, plan=plan)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        # call[0]=plan, call[1]=dev (preflight now handled by mock_preflight)
        plan_profile_used = _get_call_profile(mock_agent, 0)
        assert plan_profile_used.timeout_seconds == 1800

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_derives_large_timeout_when_no_override(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
        capsys,
    ):
        """PLAN phase derives a large timeout when no medium/large overrides are configured."""
        plan = PlanConfig.of(enabled=True, timeout=600, validate_spec=False)
        config = _make_config(tmp_path, plan=plan)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        # call[0]=plan (preflight now handled by mock_preflight)
        plan_profile_used = _get_call_profile(mock_agent, 0)
        assert plan_profile_used.timeout_seconds == round(600 * LARGE_HEADROOM_FACTOR)
        stderr = capsys.readouterr().err
        assert (
            f"Plan timeout: {round(600 * LARGE_HEADROOM_FACTOR)}s (large complexity, derived)"
            in stderr
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_logs_complexity_suffix_when_override_equals_base(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path, capsys
    ):
        """Complexity suffix appears in plan log even when override value equals base timeout."""
        base = 600
        plan = PlanConfig.of(
            enabled=True,
            timeout=base,
            timeout_medium=base,
            timeout_large=base,
            validate_spec=False,
        )
        config = _make_config(tmp_path, plan=plan)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        stderr = capsys.readouterr().err
        assert "Plan timeout: 600s (large complexity, explicit override)" in stderr


# ── load_config: new fields parsing ───────────────────────────────────


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


class TestLoadConfigTimeoutFields:
    def test_plan_config_parses_medium_large(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {
                    "enabled": True,
                    "timeout": 600,
                    "timeout_medium": 900,
                    "timeout_large": 1800,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan.timeout == 600
        assert config.plan.timeout_medium == 900
        assert config.plan.timeout_large == 1800

    def test_plan_config_defaults_none_when_absent(self, tmp_path):
        config_path = _write_config({"plan": {"enabled": True, "timeout": 600}}, tmp_path)
        config = load_config(config_path)
        assert config.plan.timeout_medium is None
        assert config.plan.timeout_large is None

    def test_smart_config_applies_dev_timeout_overrides(self, tmp_path):
        """Smart config (models:) path respects timeout overrides via overrides: key."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet"],
                "budget_usd": 10.0,
                "overrides": {
                    "dev": {
                        "timeout_seconds": 600,
                        "timeout_medium_seconds": 900,
                        "timeout_large_seconds": 1800,
                    }
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.timeout_seconds == 600
        assert config.dev_profile.timeout_medium_seconds == 900
        assert config.dev_profile.timeout_large_seconds == 1800

    def test_smart_config_defaults_none_when_absent(self, tmp_path):
        config_path = _write_config(
            {"models": ["claude/sonnet"], "budget_usd": 10.0},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.timeout_medium_seconds is None
        assert config.dev_profile.timeout_large_seconds is None

    def test_plan_timeout_defaults_to_600_when_omitted(self, tmp_path):
        """plan.timeout falls back to 600s when not specified in forge.yaml."""
        config_path = _write_config({"plan": {"enabled": True}}, tmp_path)
        config = load_config(config_path)
        assert config.plan.timeout == 600
