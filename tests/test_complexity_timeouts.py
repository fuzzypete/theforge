"""Tests for complexity-based timeouts (resolve_timeout, DEV phase, PLAN phase, load_config)."""

import dataclasses
import time as _time
from pathlib import Path
from unittest.mock import patch

import yaml

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
from theforge.coordinator.util import resolve_timeout
from theforge.runners import AgentResult
from theforge.task import TaskStory

# ── resolve_timeout ────────────────────────────────────────────────────


class TestResolveTimeout:
    def test_small_complexity_uses_base(self):
        assert resolve_timeout(600, 900, 1800, "small") == 600

    def test_none_complexity_uses_base(self):
        assert resolve_timeout(600, 900, 1800, None) == 600

    def test_medium_complexity_uses_medium_override(self):
        assert resolve_timeout(600, 900, 1800, "medium") == 900

    def test_large_complexity_uses_large_override(self):
        assert resolve_timeout(600, 900, 1800, "large") == 1800

    def test_medium_override_absent_falls_back_to_base(self):
        assert resolve_timeout(600, None, 1800, "medium") == 600

    def test_large_override_absent_falls_back_to_base(self):
        assert resolve_timeout(600, 900, None, "large") == 600

    def test_both_overrides_absent_always_returns_base(self):
        assert resolve_timeout(600, None, None, "large") == 600
        assert resolve_timeout(600, None, None, "medium") == 600
        assert resolve_timeout(600, None, None, "small") == 600
        assert resolve_timeout(600, None, None, None) == 600

    def test_unknown_complexity_falls_back_to_base(self):
        assert resolve_timeout(600, 900, 1800, "unknown") == 600


# ── Shared test helpers ────────────────────────────────────────────────

_VALID_DEV_NOTES = (
    "summary: Implemented the feature.\n"
    "commits:\n"
    '  - sha: "abc1234"\n'
    '    message: "feat: implement"\n'
    "acceptance_criteria:\n"
    '  - criterion: "It works"\n'
    "    status: MET\n"
    '    notes: "tested"\n'
    "spec_deviations: none\n"
    "deferred_items: none\n"
    "gate_result: PASS\n"
)

_RECENT_COMMIT_TS = str(int(_time.time()) - 60)


def _write_handoff(
    workspace: Path, decision: str = "PASS", handoff_file: str = "handoff.yaml"
) -> None:
    handoff = {
        "gate_decision": decision,
        "validation": {"make_fmt": {"status": "PASS"}},
        "scope_completed": ["test item"],
        "dev_notes": _VALID_DEV_NOTES,
    }
    handoff_path = workspace / handoff_file
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(yaml.dump(handoff), encoding="utf-8")


def _handle_stale_check_cmd(cmd: str) -> tuple[bool, str] | None:
    if "rev-parse --abbrev-ref HEAD" in cmd:
        return (True, "forge/test-task")
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "abc123 a recent commit")
    if "--format=%ct" in cmd:
        return (True, _RECENT_COMMIT_TS)
    return None


def _shell_with_gate(workspace: Path, decision: str = "PASS"):
    """Return a _run_shell side_effect that writes a valid handoff on gate pass."""

    def side_effect(cmd, cwd, **kwargs):
        if "gate" in cmd:
            if decision == "PASS":
                _write_handoff(Path(cwd), decision)
                return (True, "OK")
            else:
                return (False, "FAIL: tests failed")
        if "git status --porcelain" in cmd:
            return (True, "")
        stale_resp = _handle_stale_check_cmd(cmd)
        if stale_resp is not None:
            return stale_resp
        return (True, "OK")

    return side_effect


PREFLIGHT_PROCEED_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
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
    plan: PlanConfig = PlanConfig(),
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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_uses_large_timeout_for_large_complexity(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
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

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [
            _make_agent_result(output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        dev_profile_used = _get_call_profile(mock_agent, 1)
        assert dev_profile_used.timeout_seconds == 1800

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_uses_medium_timeout_for_medium_complexity(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
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

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [
            _make_agent_result(output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        dev_profile_used = _get_call_profile(mock_agent, 1)
        assert dev_profile_used.timeout_seconds == 900

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_falls_back_to_base_when_no_override(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
    ):
        """run_agent uses base timeout when no complexity overrides are set."""
        config = _make_config(tmp_path)  # DEFAULT_DEV_PROFILE has no medium/large overrides
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [
            _make_agent_result(output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        dev_profile_used = _get_call_profile(mock_agent, 1)
        assert dev_profile_used.timeout_seconds == DEFAULT_DEV_PROFILE.timeout_seconds

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_logs_complexity_suffix_when_override_equals_base(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path, capsys
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

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [
            _make_agent_result(output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        stderr = capsys.readouterr().err
        assert "Dev timeout: 600s (large complexity)" in stderr


# ── PLAN phase timeout resolution ──────────────────────────────────────


class TestPlanPhaseTimeout:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_uses_large_timeout_for_large_complexity(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
    ):
        """PLAN phase profile uses timeout_large when complexity=large."""
        plan = PlanConfig(enabled=True, timeout=600, timeout_medium=900, timeout_large=1800)
        config = _make_config(tmp_path, plan=plan)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [
            _make_agent_result(output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05),
            _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        # call[0]=preflight, call[1]=plan, call[2]=dev
        plan_profile_used = _get_call_profile(mock_agent, 1)
        assert plan_profile_used.timeout_seconds == 1800

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_falls_back_to_base_when_no_override(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
    ):
        """PLAN phase uses base timeout when no medium/large overrides are configured."""
        plan = PlanConfig(enabled=True, timeout=600)
        config = _make_config(tmp_path, plan=plan)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [
            _make_agent_result(output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05),
            _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        plan_profile_used = _get_call_profile(mock_agent, 1)
        assert plan_profile_used.timeout_seconds == 600

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_logs_complexity_suffix_when_override_equals_base(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path, capsys
    ):
        """Complexity suffix appears in plan log even when override value equals base timeout."""
        base = 600
        plan = PlanConfig(enabled=True, timeout=base, timeout_medium=base, timeout_large=base)
        config = _make_config(tmp_path, plan=plan)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [
            _make_agent_result(output=PREFLIGHT_PROCEED_LARGE, cost_usd=0.05),
            _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)

        assert result.success is True
        stderr = capsys.readouterr().err
        assert "Plan timeout: 600s (large complexity)" in stderr


# ── load_config: new fields parsing ───────────────────────────────────


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


class TestLoadConfigTimeoutFields:
    def test_classic_config_parses_dev_medium_large(self, tmp_path):
        config_path = _write_config(
            {
                "profiles": {
                    "dev": {
                        "timeout_seconds": 600,
                        "timeout_medium_seconds": 900,
                        "timeout_large_seconds": 1800,
                    }
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.timeout_seconds == 600
        assert config.dev_profile.timeout_medium_seconds == 900
        assert config.dev_profile.timeout_large_seconds == 1800

    def test_classic_config_defaults_none_when_absent(self, tmp_path):
        config_path = _write_config({"profiles": {"dev": {"timeout_seconds": 600}}}, tmp_path)
        config = load_config(config_path)
        assert config.dev_profile.timeout_medium_seconds is None
        assert config.dev_profile.timeout_large_seconds is None

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
        """Smart config (models:) path respects timeout_medium_seconds / timeout_large_seconds."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet"],
                "budget_usd": 10.0,
                "profiles": {
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
