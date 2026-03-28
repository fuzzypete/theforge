"""Tests for the coordinator state machine.

Uses mocked runner to test all state transitions without real agent calls.
"""

import dataclasses
import datetime
import io
import json
import time as _time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    NotificationConfig,
    NtfyConfig,
    PlanAgentReviewConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import (
    Phase,
    generate_audit_log,
    run_from_review,
    run_task,
)
from theforge.coordinator.state import parse_phase_name
from theforge.runners import AgentResult, LogLevel
from theforge.task import TaskStory

# ── Fixtures ─────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config pointing at tmp_path (single reviewer, no synthesis)."""
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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_plan_review_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    mode: str = "blocking",
    timeout_seconds: int = 300,
) -> ForgeConfig:
    """Create a test config with PLAN and PLAN_REVIEW enabled."""
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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
        plan_review=PlanReviewConfig(enabled=enabled, mode=mode, timeout_seconds=timeout_seconds),
        log=LogConfig(enabled=False),
    )


def _make_ntfy_plan_review_config(
    tmp_path: Path,
    *,
    mode: str = "blocking",
    timeout_seconds: int = 10,
) -> ForgeConfig:
    """Create a test config with PLAN, PLAN_REVIEW, and ntfy notifications enabled."""
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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        notifications=NotificationConfig(
            backend="ntfy",
            ntfy=NtfyConfig(url="https://ntfy.sh/test-topic", priority="default"),
        ),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
        plan_review=PlanReviewConfig(enabled=True, mode=mode, timeout_seconds=timeout_seconds),
        log=LogConfig(enabled=False),
    )


def _make_pool_config(
    tmp_path: Path, profiles: list[ModelProfile], synthesis: ModelProfile | None
) -> ForgeConfig:
    """Create a test config with multi-model review pool."""
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
        review_pool=profiles,
        synthesis_profile=synthesis,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path) -> TaskStory:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


def _make_agent_result(
    success: bool = True,
    output: str = "Done.",
    session_id: str | None = "sess-1",
    cost_usd: float | None = 0.50,
    profile_name: str = "",
    structured_data: dict | None = None,
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=session_id,
        cost_usd=cost_usd,
        exit_code=0 if success else 1,
        raw={},
        profile_name=profile_name,
        structured_data=structured_data,
    )


def _make_pool_result(
    outputs: list[str],
    profile_names: list[str],
    success: bool = True,
    cost_usd: float = 0.20,
) -> list[AgentResult]:
    """Build a list of AgentResults as if returned by run_agent_pool."""
    return [
        AgentResult(
            success=success,
            output=out,
            session_id=None,
            cost_usd=cost_usd,
            exit_code=0 if success else 1,
            raw={},
            profile_name=name,
        )
        for out, name in zip(outputs, profile_names)
    ]


_VALID_DEV_NOTES = (
    'summary: "Implemented the feature."\n'
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


def _write_handoff(
    workspace: Path, decision: str = "PASS", handoff_file: str = "handoff.yaml"
) -> None:
    """Write a minimal handoff file in the workspace with valid dev_notes."""
    handoff = {
        "gate_decision": decision,
        "validation": {"make_fmt": {"status": "PASS"}},
        "scope_completed": ["test item"],
        "dev_notes": _VALID_DEV_NOTES,
    }
    handoff_path = workspace / handoff_file
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(yaml.dump(handoff), encoding="utf-8")


# Stale-worktree detection commands need specific responses so that pre-created
# workspaces in existing tests are treated as "fresh" (reused rather than removed).
_RECENT_COMMIT_TS = str(int(_time.time()) - 60)  # 1 minute ago


def _handle_stale_check_cmd(cmd: str) -> tuple[bool, str] | None:
    """Return a mock response for stale-worktree detection git commands, or None if not matched."""
    if "rev-parse --abbrev-ref HEAD" in cmd:
        return (True, "forge/test-task")
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "abc123 a recent commit")
    if "--format=%ct" in cmd:
        return (True, _RECENT_COMMIT_TS)
    return None


def _shell_with_gate(workspace: Path, decisions: list[str] | str = "PASS"):
    """Create a _run_shell side_effect that simulates gate execution via exit code.

    Gate pass/fail is determined by exit code. On PASS, also writes a valid
    handoff.yaml so downstream handoff validation succeeds. On FAIL, returns a
    non-zero exit so the coordinator treats it as a gate failure.

    Also handles stale-worktree detection commands by returning a "fresh" worktree
    (recent commit, commits ahead of base) so pre-created workspaces are reused.
    """
    if isinstance(decisions, str):
        decisions_list = [decisions] * 20
    else:
        decisions_list = list(decisions)
    gate_idx = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if "gate" in cmd:
            d = decisions_list[min(gate_idx["n"], len(decisions_list) - 1)]
            gate_idx["n"] += 1
            if d == "PASS":
                _write_handoff(Path(cwd), d)
                return (True, "OK")
            else:
                return (False, "FAIL: tests failed")
        if "git status --porcelain" in cmd:
            return (True, "")  # clean worktree
        stale_resp = _handle_stale_check_cmd(cmd)
        if stale_resp is not None:
            return stale_resp
        return (True, "OK")

    return side_effect


APPROVE_REVIEW = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""

APPROVE_REVIEW_JSON = {
    "verdict": "APPROVE",
    "summary": "Looks good.",
    "findings": [],
    "story_compliance": {"matches_spec": True},
    "test_coverage": {"adequate": True},
}


REQUEST_CHANGES_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Bug found."
findings:
  - severity: P1
    file: src/foo.py
    line: 10
    description: "Off by one"
    suggestion: "Fix it"
story_compliance:
  matches_spec: false
  mismatches:
    - "Missing batch config"
test_coverage:
  adequate: false
  gaps:
    - "No edge case test"
```
"""

PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Spec requirements are not yet implemented."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

PREFLIGHT_PROCEED_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Spec requirements are not yet implemented."
criteria_checked: []
```
"""

PREFLIGHT_ALREADY_DONE = """\
```yaml
verdict: ALREADY_DONE
reason: "All acceptance criteria are already satisfied."
criteria_checked:
  - criterion: "Feature X"
    satisfied: true
    evidence: "Implemented in coordinator.py:42"
```
"""

PREFLIGHT_BLOCKED = """\
```yaml
verdict: BLOCKED
reason: "Spec references removed_function() which no longer exists."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "removed_function() was deleted in a prior commit"
```
"""


_PREFLIGHT_RESULT = _make_agent_result(
    success=True, output=PREFLIGHT_PROCEED, cost_usd=0.05, profile_name="review"
)


def _preflight_then(*dev_results: AgentResult):
    """Preflight PROCEED on first call, then dev_results."""
    preflight_result = _PREFLIGHT_RESULT
    results = [preflight_result, *dev_results]
    call_idx = {"n": 0}

    def side_effect(**kwargs):
        idx = min(call_idx["n"], len(results) - 1)
        call_idx["n"] += 1
        return results[idx]

    return side_effect


# ── Tests ────────────────────────────────────────────────────────────


class TestCoordinatorHybridRunner:
    @pytest.fixture
    def api_profile(self) -> ModelProfile:
        return ModelProfile(
            name="api-reviewer",
            provider="openai",
            model="o4-mini",
            budget_usd=1.0,
            timeout_seconds=120,
            allowed_tools=(),
        )

    @pytest.fixture
    def cli_profile(self) -> ModelProfile:
        return ModelProfile(
            name="cli-reviewer",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash"),
        )

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_mixed_pool_happy_path(
        self, mock_shell, mock_agent, mock_preflight, tmp_path, api_profile, cli_profile
    ):
        """Test a mixed pool of API and CLI reviewers."""
        config = _make_pool_config(tmp_path, [cli_profile, api_profile], None)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(
                    success=True, output=APPROVE_REVIEW, profile_name="cli-reviewer"
                ),
                _make_agent_result(
                    success=True,
                    output=json.dumps(APPROVE_REVIEW_JSON),
                    profile_name="api-reviewer",
                    structured_data=APPROVE_REVIEW_JSON,
                ),
            ]
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Check that parse_review_json was called
        # (implicitly tested by the fact that the merge succeeds)
        assert len(result.state.review_results) == 1
        merged_review = result.state.review_results[0]
        assert merged_review.verdict == "APPROVE"

    @patch("theforge.coordinator.review_pool.build_review_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_mode_aware_prompt_builder(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_prompt_builder,
        tmp_path,
        api_profile,
    ):
        """Test that the correct mode is passed to the prompt builder."""
        config = _make_pool_config(tmp_path, [api_profile], None)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, structured_data=APPROVE_REVIEW_JSON)
        ]

        run_task(config, task)

        mock_prompt_builder.assert_called()
        # The first call to build_review_prompt is what we want to check
        call_args = mock_prompt_builder.call_args_list[0]
        assert call_args.kwargs["mode"] == "api"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_cost_summation_with_none(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Test that total_cost handles None from CLI reviewers."""
        cli_profile = ModelProfile(
            name="cli-reviewer",
            cli="codex",
            provider=None,
            model="o4-mini",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        config = _make_pool_config(tmp_path, [cli_profile], None)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", cost_usd=None
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, cost_usd=None)
        ]

        result = run_task(config, task)

        assert result.success is True
        # preflight(0.05) + dev(None) + review(None) = 0.05
        assert result.state.total_cost == pytest.approx(0.05)


class TestCoordinatorHappyPath:
    """Test the golden path: dev succeeds, gate passes, review approves."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_single_pass(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        assert result.state.dev_trace_count == 1
        assert len(result.state.dev_results) == 1
        assert len(result.state.review_results) == 1


class TestAlreadyDoneOverride:
    """ALREADY_DONE guard: commits + no prior APPROVE → override to REVIEW."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=False)
    def test_already_done_with_commits_no_approve_resumes_review(
        self, mock_approve, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Preflight says ALREADY_DONE but commits exist and no audit APPROVE → run REVIEW."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Should proceed to REVIEW and succeed (not short-circuit ALREADY_DONE)
        assert result.success is True
        assert result.state.review_cycle == 1
        assert len(result.state.review_results) == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_already_done_with_prior_approve_honours_already_done(
        self, mock_approve, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Preflight says ALREADY_DONE with prior APPROVE in audit trail → honour ALREADY_DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE
        )

        result = run_task(config, task)

        # ALREADY_DONE is honoured; no review invoked
        assert result.success is True
        assert result.state.preflight_verdict == "ALREADY_DONE"
        assert len(result.state.review_results) == 0
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=False)
    def test_already_done_with_no_commits_ahead_honours_already_done(
        self, mock_approve, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """ALREADY_DONE with 0 commits ahead → honour ALREADY_DONE (no interrupted run)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Use a counter to distinguish stale-check git log (1st call) from ALREADY_DONE
        # guard git log (2nd call). The stale check must see commits (reuse workspace);
        # the ALREADY_DONE guard must see 0 commits (no override).
        git_log_calls = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "git status --porcelain" in cmd:
                return (True, "")
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "forge/test-task")
            if "--oneline" in cmd and "git log" in cmd:
                git_log_calls["n"] += 1
                if git_log_calls["n"] == 1:
                    return (True, "abc123 a commit")  # stale check → reuse workspace
                return (True, "")  # ALREADY_DONE guard → 0 commits → no override
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_verdict == "ALREADY_DONE"
        assert len(result.state.review_results) == 0
        mock_pool.assert_not_called()


class TestPlanReview:
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nOriginal plan."
        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["decision"] == "approve"
        assert audit["plan_review"]["regenerated"] is False
        assert audit["plan_review"]["mode"] == "blocking"
        assert mock_human_review.called

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_edit_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        edited_plan = "# Plan\n\nEdited by human."

        def plan_review_side_effect(*args, **kwargs):
            plan_path = workspace / ".forge" / "plan.md"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(edited_plan, encoding="utf-8")
            return "approve"

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.side_effect = plan_review_side_effect

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == edited_plan
        assert mock_human_review.called

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_regenerate(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        plan_v1 = "# Plan\n\nFirst plan."
        plan_v2 = "# Plan\n\nRegenerated plan."

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output=plan_v1, cost_usd=0.10),
            _make_agent_result(success=True, output=plan_v2, cost_usd=0.15),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.side_effect = ["regenerate", "approve"]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0
        assert result.state.plan_review_decision == "approve"
        assert len(result.state.plan_results) == 2
        assert result.state.plan_output == plan_v2
        assert mock_agent.call_count == 3  # PLAN + PLAN-regen + DEV (preflight mocked separately)
        assert mock_human_review.called

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_abandon(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10)
        ]
        mock_plan_review.return_value = "abandon"

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert "abandoned" in result.message.lower()
        assert workspace.exists()
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_regen_twice_abandons(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = dataclasses.replace(
            _make_plan_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nSecond plan.", cost_usd=0.15),
        ]
        mock_plan_review.side_effect = ["regenerate", "regenerate"]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert "rejected" in result.message.lower()
        assert result.state.plan_review_decision == "abandon"
        assert result.state.plan_regen_count > 0
        assert len(result.state.plan_results) == 2
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_skipped_on_injection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Injected plan\n\nUse this.", encoding="utf-8")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [_make_agent_result(success=True, output="Implemented.")]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        assert result.state.plan_review_decision is None
        mock_plan_review.assert_not_called()
        assert mock_human_review.called

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_skipped_when_disabled(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path, enabled=False)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision is None
        mock_plan_review.assert_not_called()
        assert mock_human_review.called

    def test_plan_review_eof_abandons(self, tmp_path, capsys):
        from theforge.coordinator.engine import CoordinatorState
        from theforge.coordinator.notify import _plan_review_interactive

        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_text = "# Plan\n\nRead me."
        plan_path = workspace / ".forge" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan_text, encoding="utf-8")

        with patch("theforge.coordinator.notify.sys.stdin", io.StringIO("")):
            decision = _plan_review_interactive(CoordinatorState(), plan_text, workspace, task)

        captured = capsys.readouterr()
        assert decision == "abandon"
        assert plan_text in captured.out
        assert f"Plan at: {workspace / '.forge/plan.md'}" in captured.err

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_terminal_used_without_interactive_flag(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        """plan_review.enabled=True + interactive=False + no ntfy → terminal prompt is used."""
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=False)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        mock_plan_review.assert_called_once()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_remote")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_remote_ntfy_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_remote_review,
        mock_human_review,
        tmp_path,
    ):
        """Non-interactive + ntfy configured → _plan_review_remote is called."""
        config = _make_ntfy_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nNtfy plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_remote_review.return_value = "approve"

        result = run_task(config, task, interactive=False, notify=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        mock_remote_review.assert_called_once()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_remote")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_remote_ntfy_abandon(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_remote_review,
        mock_human_review,
        tmp_path,
    ):
        """Non-interactive + ntfy + remote returns abandon → run fails at PLAN_REVIEW."""
        config = _make_ntfy_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nNtfy plan.", cost_usd=0.10)
        ]
        mock_remote_review.return_value = "abandon"

        result = run_task(config, task, interactive=False, notify=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        mock_remote_review.assert_called_once()

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_advisory_without_ntfy_uses_terminal(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        """Advisory mode without ntfy → terminal prompt is used (not auto-approve)."""
        config = _make_plan_review_config(tmp_path, mode="advisory")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nAdvisory plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=False)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        mock_plan_review.assert_called_once()

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_abandon_phase_not_escalate(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10)
        ]
        mock_plan_review.return_value = "abandon"

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.PLAN_REVIEW
        assert result.phase != Phase.ESCALATE

    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_reread_error(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def plan_review_side_effect(*args, **kwargs):
            (workspace / ".forge" / "plan.md").unlink()
            return "approve"

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10)
        ]
        mock_plan_review.side_effect = plan_review_side_effect

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "unreadable after edit" in result.message
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_regen_tracks_both_costs(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nSecond plan.", cost_usd=0.15),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.side_effect = ["regenerate", "approve"]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.total_plan_cost == pytest.approx(0.25)
        assert len(result.state.plan_results) == 2
        assert mock_human_review.called


PREFLIGHT_PROCEED_SMALL = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Spec requirements are not yet implemented."
criteria_checked: []
```
"""


class TestStoryValidation:
    """Tests for spec validation (pre-PLAN quality check)."""

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_spec_validation_pass_continues_to_plan(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        """PASS verdict → run continues to PLAN normally."""
        from theforge.story_validator import StoryValidationResult

        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_validate.return_value = StoryValidationResult(verdict="PASS")
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nThe plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)

        assert result.success is True
        mock_validate.assert_called_once()
        assert result.state.story_validation_result is not None
        assert result.state.story_validation_result.verdict == "PASS"
        # Plan still ran
        assert len(result.state.plan_results) == 1

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_spec_validation_warn_logs_and_continues(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
        capsys,
    ):
        """WARN verdict → findings logged, run continues to PLAN."""
        from theforge.story_validator import StoryValidationFinding, StoryValidationResult

        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_validate.return_value = StoryValidationResult(
            verdict="WARN",
            findings=[
                StoryValidationFinding(
                    category="requirement",
                    description="AC-3 contradicts Requirement-2",
                    split_suggestion=None,
                )
            ],
        )
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nThe plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)

        assert result.success is True
        # Validation ran and returned WARN
        assert result.state.story_validation_result.verdict == "WARN"
        # Run still continued to PLAN
        assert len(result.state.plan_results) == 1
        # Finding was logged
        captured = capsys.readouterr()
        assert "AC-3 contradicts Requirement-2" in captured.err

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_spec_validation_skipped_on_plan_injection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        """validate_story not called when --plan is injected."""
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Injected plan\n\nUse this.", encoding="utf-8")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [_make_agent_result(success=True, output="Implemented.")]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        mock_validate.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_spec_validation_skipped_for_small_complexity(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_human_review,
        tmp_path,
    ):
        """validate_story not called when preflight complexity is small."""
        config = _make_config(tmp_path)  # plan not enabled → small specs skip plan
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_SMALL, cost_usd=0.05
        )
        mock_agent.side_effect = [_make_agent_result(success=True, output="Implemented.")]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        mock_validate.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_spec_validation_warn_scope_appears_in_audit(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        """WARN with scope finding → spec_validation.findings appears in audit log."""
        from theforge.story_validator import StoryValidationFinding, StoryValidationResult

        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        split = {
            "stories": [{"name": "Story A", "acs": ["AC1"]}, {"name": "Story B", "acs": ["AC2"]}]
        }
        mock_validate.return_value = StoryValidationResult(
            verdict="WARN",
            cost_usd=0.005,
            findings=[
                StoryValidationFinding(
                    category="scope",
                    description="Spec covers two independent subsystems",
                    split_suggestion=split,
                )
            ],
        )
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nThe plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)
        audit = generate_audit_log(config, task, result)

        assert result.success is True
        sv = audit["story_validation"]
        assert sv is not None
        assert sv["verdict"] == "WARN"
        assert sv["cost_usd"] == 0.005
        assert len(sv["findings"]) == 1
        f = sv["findings"][0]
        assert f["category"] == "scope"
        assert f["split_suggestion"] == split


class TestCoordinatorGateFailRetry:
    """Test that gate failure retries the dev agent."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_fail_then_pass(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.dev_iteration == 2  # needed a retry

    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_failure_log_contains_tail_not_head(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_dev_prompt, tmp_path
    ):
        """Gate FAIL: the logged gate output is the tail, not the head."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        head_marker = "HEAD_SENTINEL"
        tail_marker = "TAIL_SENTINEL"
        long_output = head_marker + "." * 5000 + tail_marker

        gate_idx = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                gate_idx["n"] += 1
                if gate_idx["n"] == 1:
                    return (False, long_output)
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_dev_prompt.return_value = "dev prompt"

        logged_msgs: list[str] = []
        with patch("theforge.coordinator.util._log") as mock_log:
            mock_log.side_effect = lambda msg: logged_msgs.append(str(msg))
            run_task(config, task)

        gate_fail_logs = [m for m in logged_msgs if "Gate command failed" in m]
        assert gate_fail_logs, "Expected at least one 'Gate command failed' log"
        assert any(tail_marker in m for m in gate_fail_logs), "Tail marker should appear in log"
        assert not any(head_marker in m for m in gate_fail_logs), (
            "Head marker must NOT appear in log (output[:200] head-slice was used instead of tail)"
        )

    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_failure_includes_output_tail_in_dev_feedback(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_dev_prompt, tmp_path
    ):
        """Gate FAIL: human_feedback passed to dev prompt contains gate output tail."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        head_marker = "HEAD_SENTINEL"
        tail_marker = "TAIL_SENTINEL"
        long_output = head_marker + "." * 5000 + tail_marker

        gate_idx = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                gate_idx["n"] += 1
                if gate_idx["n"] == 1:
                    return (False, long_output)
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_dev_prompt.return_value = "dev prompt"

        run_task(config, task)

        # Second build_dev_prompt call is the retry after gate FAIL
        assert mock_dev_prompt.call_count >= 2, "Expected at least two build_dev_prompt calls"
        second_call = mock_dev_prompt.call_args_list[1]
        feedback = second_call.kwargs.get("human_feedback", "")
        assert "Gate output" in feedback, "human_feedback must contain 'Gate output'"
        assert tail_marker in feedback, "human_feedback must contain the tail of gate output"
        assert head_marker not in feedback, (
            "human_feedback must NOT contain the head of gate output ([:200] slice was used)"
        )


class TestCoordinatorEscalation:
    """Test that exhausting retries escalates to human."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_exhaustion(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "FAIL")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 2  # hit max

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_exhaustion(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()


class TestCoordinatorCostTracking:
    """Test that both dev and review costs are tracked."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_total_cost_includes_review(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.75,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.90,
            exit_code=0,
            raw={},
            profile_name="review",
        )

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = dev_result
        mock_pool.return_value = [review_result]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.total_dev_cost == 0.75
        assert result.state.total_review_cost == 0.90
        # total_cost includes preflight ($0.05) + dev + review
        assert result.state.total_cost == pytest.approx(0.05 + 0.75 + 0.90)


def _make_review_profile(name: str, budget_usd: float = 1.0) -> ModelProfile:
    return ModelProfile(
        name=name,
        cli="claude",
        provider=None,
        model="opus",
        budget_usd=budget_usd,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


SYNTHESIS_PROFILE = ModelProfile(
    name="synthesis",
    cli="claude",
    provider=None,
    model="opus",
    budget_usd=1.50,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash"),
)


class TestCoordinatorAuditTiming:
    """Test that audit log includes timing and started_at fields."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_started_at_set_in_state(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """CoordinatorState.started_at is set when run_task() begins."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.started_at is not None
        # Should be a valid ISO timestamp

        dt = datetime.datetime.fromisoformat(result.state.started_at)
        assert dt.tzinfo is not None  # timezone-aware

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_audit_log_timing_fields(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """generate_audit_log() includes started_at, finished_at, duration_seconds."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        timing = audit["timing"]
        assert "started_at" in timing
        assert "finished_at" in timing
        assert "duration_seconds" in timing
        assert timing["started_at"] is not None
        assert timing["finished_at"] is not None
        assert timing["duration_seconds"] is not None
        assert timing["duration_seconds"] >= 0


class TestCoordinatorAuditAgentBreakdown:
    """Test per-agent cost breakdown in audit log."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_cost_agents_list(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        """cost.agents contains one entry per dev and review invocation."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        dev_result_30 = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = dev_result_30
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id="s2",
                cost_usd=0.20,
                exit_code=0,
                raw={},
                profile_name="review",
            )
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        agents = audit["cost"]["agents"]
        assert len(agents) == 2  # 1 dev + 1 review

        dev_entry = next(a for a in agents if a["role"] == "dev")
        assert dev_entry["profile"] == "dev"
        assert dev_entry["cost_usd"] == 0.30
        assert "duration_seconds" in dev_entry
        assert dev_entry["duration_seconds"] is not None
        assert dev_entry["duration_seconds"] >= 0

        review_entry = next(a for a in agents if a["role"] == "review")
        assert review_entry["profile"] == "review"
        assert review_entry["cost_usd"] == 0.20
        assert review_entry["duration_seconds"] is not None
        assert review_entry["duration_seconds"] >= 0


# ── Structured logging tests ──────────────────────────────────────────


class TestVerboseFlagEnablesToolLines:
    """Tool activity is printed in VERBOSE mode and suppressed in PROGRESS mode."""

    def test_verbose_prints_tool_lines(self, capsys):
        import theforge.runners.cli as runner_mod
        import theforge.runners.runner_claude as runner_claude_mod

        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            # Simulate a tool_use assistant event
            tool_event = (
                '{"type": "assistant", "message": {"content": '
                '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
            )
            runner_claude_mod._process_stream_event(tool_event, "test-label")
            captured = capsys.readouterr()
            assert "↳ Read" in captured.err
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)

    def test_progress_suppresses_tool_lines(self, capsys):
        import theforge.runners.cli as runner_mod
        import theforge.runners.runner_claude as runner_claude_mod

        runner_mod.set_log_level(LogLevel.PROGRESS)
        tool_event = (
            '{"type": "assistant", "message": {"content": '
            '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
        )
        runner_claude_mod._process_stream_event(tool_event, "test-label")
        captured = capsys.readouterr()
        assert "↳ Read" not in captured.err


class TestProgressShowsPhaseTransitions:
    """Phase transition lines always appear at PROGRESS level."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_phase_transitions_always_shown(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path, capsys
    ):
        import theforge.coordinator.engine as coord_mod
        import theforge.runners.cli as runner_mod

        # Ensure we are at PROGRESS level
        coord_mod.set_log_level(LogLevel.PROGRESS)
        runner_mod.set_log_level(LogLevel.PROGRESS)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        captured = capsys.readouterr()
        # Phase transitions must appear even at PROGRESS level
        assert "▸ WORKSPACE" in captured.err
        assert "▸ DEV" in captured.err
        assert "▸ VALIDATE" in captured.err
        assert "▸ REVIEW" in captured.err
        assert "✓ DONE" in captured.err


class TestSprintSpecHeaderPrinted:
    """Sprint emits [N/total] slug header before each spec."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_spec_header_emitted(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path, capsys
    ):
        import yaml as _yaml

        from theforge.sprint import run_sprint

        # Write a minimal forge.yaml
        config = _make_config(tmp_path)

        # Write a spec file with frontmatter
        spec_path = tmp_path / "test-spec.md"
        spec_path.write_text(
            "---\nname: Test Spec\nslug: test-spec\n---\n# Test\n",
            encoding="utf-8",
        )

        # Write a sprint manifest
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            _yaml.dump(
                {
                    "name": "test sprint",
                    "budget_usd": 10.0,
                    "specs": ["test-spec.md"],
                }
            ),
            encoding="utf-8",
        )

        workspace = tmp_path / "test-spec"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_sprint(config, manifest_path)

        captured = capsys.readouterr()
        # Header banner for spec [1/1] must appear
        assert "[1/1]" in captured.err
        assert "test-spec" in captured.err


class TestCoordinatorAuditFindings:
    """Test that review findings are included in audit log."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_findings_in_audit(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Audit reviews[] entries include findings list with severity, file, line, description."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert len(audit["reviews"]) == 2

        # First review has findings
        first_rev = audit["reviews"][0]
        assert "findings" in first_rev
        assert first_rev["p1_count"] == 1
        assert len(first_rev["findings"]) == 1
        finding = first_rev["findings"][0]
        assert finding["severity"] == "P1"
        assert finding["file"] == "src/foo.py"
        assert finding["line"] == 10
        assert "Off by one" in finding["description"]

        # Second review (APPROVE) has empty findings
        second_rev = audit["reviews"][1]
        assert "findings" in second_rev
        assert second_rev["findings"] == []
        assert second_rev["p1_count"] == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_approve_review_has_empty_findings(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """APPROVE review in audit has findings: [] (not missing key)."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        rev = audit["reviews"][0]
        assert "findings" in rev
        assert rev["findings"] == []


class TestCoordinatorReviewCycleMetadata:
    """Test that review cycle metadata is populated correctly."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_metadata_present_on_approve(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Audit metadata is populated after successful pool merge."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", profile_name="dev"
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        result = run_task(config, task)

        assert len(result.state.review_cycle_metadata) == 1
        meta = result.state.review_cycle_metadata[0]
        assert meta.pool_models == ["r1", "r2"]
        assert meta.successful == ["r1", "r2"]
        assert meta.failed == []
        assert meta.synthesized is False

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_metadata_present_on_all_reviewers_fail(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Metadata is populated even when all reviewers fail (P2 fix)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=False, output="FAIL", profile_name="r1"),
            _make_agent_result(success=False, output="FAIL", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.phase == Phase.ESCALATE
        # Metadata must be present even though we escalated early
        assert len(result.state.review_cycle_metadata) == 1
        meta = result.state.review_cycle_metadata[0]
        assert meta.failed == ["r1", "r2"]
        assert meta.successful == []
        assert meta.synthesized is False

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_audit_log_contains_pool_metadata(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """generate_audit_log includes pool_models, synthesized, successful, failed."""

        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", profile_name="dev"
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert len(audit["reviews"]) == 1
        rev = audit["reviews"][0]
        assert rev["cycle"] == 1
        assert rev["pool_models"] == ["r1", "r2"]
        assert rev["successful"] == ["r1", "r2"]
        assert rev["failed"] == []
        assert rev["synthesized"] is False
        assert rev["verdict"] == "APPROVE"


class TestCoordinatorDevNotes:
    """Test that coordinator reads dev_notes from handoff.yaml and passes to review prompt."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_notes_passed_to_review_prompt(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Coordinator reads dev_notes from handoff.yaml and injects into review prompt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Write handoff.yaml with dev_notes
        handoff = {
            "gate_decision": "PASS",
            "validation": {"make_fmt": {"status": "PASS"}},
            "scope_completed": ["test item"],
            "dev_notes": "I deviated from spec because it was better.",
        }
        (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                # handoff.yaml already exists — gate re-writes it
                (Path(cwd) / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        captured_prompts: list[str] = []

        def pool_side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            if isinstance(prompt, list):
                captured_prompts.extend(prompt)
            elif isinstance(prompt, str):
                captured_prompts.append(prompt)
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # At least one review prompt should contain the dev_notes content
        assert any("I deviated from spec because it was better." in p for p in captured_prompts), (
            f"dev_notes not found in any review prompt. Captured: {captured_prompts[:1]}"
        )
        assert any("## Developer Notes" in p for p in captured_prompts)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_notes_fall_back_to_legacy_handoff(
        self, mock_shell, mock_agent, mock_engine_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Coordinator falls back to legacy handoff.yaml when configured .forge file is absent."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            validation=dataclasses.replace(
                _make_config(tmp_path).validation,
                handoff_file=".forge/handoff.yaml",
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        handoff = {
            "gate_decision": "PASS",
            "validation": {"make_fmt": {"status": "PASS"}},
            "scope_completed": ["test item"],
            "dev_notes": "Legacy root handoff content.",
        }
        (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")

        def shell_side_effect(cmd, cwd, **kwargs):
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Unused.")
        # handoff fix uses engine.run_agent (unstructured dev_notes trigger fix attempts)
        mock_engine_agent.return_value = _make_agent_result(success=True, output="Unused.")

        captured_prompts: list[str] = []

        def pool_side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            if isinstance(prompt, list):
                captured_prompts.extend(prompt)
            elif isinstance(prompt, str):
                captured_prompts.append(prompt)
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert any("Legacy root handoff content." in p for p in captured_prompts)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_to_dev_handoff_used_on_request_changes(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Coordinator uses review_to_dev_handoff (not findings_to_markdown) on REQUEST_CHANGES."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # last_review_findings should contain the richer format from review_to_dev_handoff
        assert result.state.last_review_findings is not None
        assert "## Review Summary" in result.state.last_review_findings


# ── Dev handoff validation tests ──────────────────────────────────────


class TestCoordinatorDevHandoffValidation:
    """Test that coordinator validates structured dev handoff after gate passes."""

    def _make_structured_handoff(
        self, workspace: Path, dev_notes: str, handoff_file: str = "handoff.yaml"
    ) -> None:
        """Write handoff.yaml with structured dev_notes."""
        handoff = {
            "gate_decision": "PASS",
            "validation": {"make_fmt": {"status": "PASS"}},
            "scope_completed": ["test item"],
            "dev_notes": dev_notes,
        }
        handoff_path = workspace / handoff_file
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_path.write_text(yaml.dump(handoff), encoding="utf-8")

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_valid_structured_handoff_passes_to_review(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Valid structured dev handoff is formatted and passed to reviewer."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        structured_notes = (
            'summary: "Implemented feature X with full test coverage."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat(x): implement feature X"\n'
            "acceptance_criteria:\n"
            '  - criterion: "Feature X works"\n'
            "    status: MET\n"
            '    notes: "Tested in test_x.py"\n'
            "spec_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                self._make_structured_handoff(Path(cwd), structured_notes)
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        captured_prompts: list[str] = []

        def pool_side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            if isinstance(prompt, list):
                captured_prompts.extend(prompt)
            elif isinstance(prompt, str):
                captured_prompts.append(prompt)
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # Structured summary should appear in review prompt
        assert any("Implemented feature X" in p for p in captured_prompts)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_invalid_handoff_triggers_retry(
        self, mock_shell, mock_dev_agent, mock_engine_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Invalid dev handoff triggers a handoff fix retry."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # First handoff is malformed (missing required fields)
        bad_notes = "just some unstructured text"
        good_notes = (
            'summary: "Implemented the thing."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "It works"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "spec_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        fix_call_idx = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                self._make_structured_handoff(Path(cwd), bad_notes)
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        # handoff fix agent writes valid handoff on first attempt
        def engine_agent_side_effect(**kwargs):
            fix_call_idx["n"] += 1
            self._make_structured_handoff(workspace, good_notes)
            return _make_agent_result(success=True, output="Fixed handoff.")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_engine_agent.side_effect = engine_agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # Should have called handoff fix once
        assert fix_call_idx["n"] == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_invalid_handoff_proceeds_after_max_retries(
        self, mock_shell, mock_dev_agent, mock_engine_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Invalid dev handoff proceeds to review after max retries exhausted."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        bad_notes = "just some unstructured text"

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                self._make_structured_handoff(Path(cwd), bad_notes)
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        hf_call_idx = {"n": 0}

        def engine_agent_side_effect(**kwargs):
            hf_call_idx["n"] += 1
            # Failed handoff fixes (don't write valid handoff)
            return _make_agent_result(success=True, output="Done.")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_engine_agent.side_effect = engine_agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Should still succeed (proceeds to review even with invalid handoff)
        assert result.success is True
        # max_handoff_retries (2 by default) — dev and preflight mocked separately
        assert hf_call_idx["n"] == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_missing_dev_notes_triggers_retry(
        self, mock_shell, mock_dev_agent, mock_engine_agent, mock_preflight, mock_pool, tmp_path
    ):
        """PASS gate with no dev_notes triggers the handoff retry path."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            validation=dataclasses.replace(
                _make_config(tmp_path).validation,
                handoff_file=".forge/handoff.yaml",
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        good_notes = (
            'summary: "Implemented the thing."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "It works"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "spec_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        def _write_handoff_no_dev_notes(ws: Path) -> None:
            handoff = {
                "gate_decision": "PASS",
                "validation": {"make_fmt": {"status": "PASS"}},
                "scope_completed": ["test item"],
            }
            handoff_path = ws / ".forge" / "handoff.yaml"
            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            handoff_path.write_text(yaml.dump(handoff), encoding="utf-8")

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                # Write handoff.yaml WITHOUT dev_notes
                _write_handoff_no_dev_notes(Path(cwd))
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        captured_fix_prompts: list[str] = []

        def engine_agent_side_effect(**kwargs):
            # handoff fix agent — write valid handoff with dev_notes
            captured_fix_prompts.append(kwargs.get("prompt", ""))
            self._make_structured_handoff(workspace, good_notes, ".forge/handoff.yaml")
            return _make_agent_result(success=True, output="Fixed handoff.")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_engine_agent.side_effect = engine_agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # handoff fix called once
        assert len(captured_fix_prompts) == 1
        assert any(".forge/handoff.yaml" in prompt for prompt in captured_fix_prompts)
        assert any(
            "dev_notes field is missing or blank in .forge/handoff.yaml" in prompt
            for prompt in captured_fix_prompts
        )
        assert any("git add .forge/handoff.yaml" in prompt for prompt in captured_fix_prompts)


# ── _has_persistent_p1 unit tests ────────────────────────────────────


PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""

PLAN_AGENT_REJECT_P1 = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "Plan references nonexistent function parse_config()"
    suggestion: "Use load_config() from config.py instead"
```
"""

PLAN_AGENT_REJECT_P0 = """\
```yaml
verdict: REJECT
findings:
  - severity: P0
    description: "Plan is architecturally broken — wrong module entirely"
    suggestion: "Rethink the approach"
```
"""


def _make_plan_agent_review_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config with PLAN and plan_agent_review enabled."""
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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
        plan_agent_review=PlanAgentReviewConfig(enabled=True, cli="claude", model="sonnet"),
        log=LogConfig(enabled=False),
    )


class TestPlanAgentReview:
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        tmp_path,
    ):
        """Agent returns APPROVE, pipeline continues to DEV."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # mock_pool: first call = plan review pool, second = code review pool
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_review_mode == "agent"
        assert len(result.state.plan_review_results) == 1
        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["reviewer"] == "agent"
        assert audit["plan_review"]["decision"] == "approve"
        assert audit["plan_review"]["cost_usd"] == pytest.approx(0.08)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_p1_blocking_triggers_regen(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """P1 findings block — plan regenerated, second review APPROVE."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review calls (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review",
                )
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0  # regen triggered by P1
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2  # two plan attempts

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_p0_reject_then_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """P0 finding blocks — plan regenerated, second review APPROVE."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review calls (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2
        assert len(result.state.plan_review_results) == 2

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_double_p0_reject_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Two P0 REJECTs, run escalates with findings."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nStill bad.", cost_usd=0.12),
        ]
        # Two plan review pool calls (both REJECT); code review pool never reached
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "rejected" in result.message.lower()
        assert result.state.plan_regen_count > 0
        # plan review pool called twice; code review never reached
        assert mock_pool.call_count == 2

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_model_escalation_on_repeated_rejection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        tmp_path,
    ):
        """After 2 plan rejections, planner model escalates sonnet→opus; 3rd review approves."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=3,
                plan_escalation_threshold=2,
            ),
            smart_config_models=["claude/sonnet", "claude/opus"],
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            # initial plan (sonnet)
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            # 1st regen (sonnet — rejection 1, below threshold)
            _make_agent_result(success=True, output="# Plan\n\nStill bad.", cost_usd=0.12),
            # 2nd regen (opus — rejection 2, escalation fires before this call)
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.20),
            # dev
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.side_effect = [
            # 1st plan review → REJECT (rejection 1)
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            # 2nd plan review → REJECT (rejection 2, triggers escalation)
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            # 3rd plan review → APPROVE (after escalation)
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review",
                )
            ],
            # code review
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_escalated is True
        assert result.state.plan_regen_count == 2
        assert result.state.plan_escalation_note is not None
        assert "MODEL ESCALATION" in result.state.plan_escalation_note

        # The 3rd run_agent call (index 2) is the 2nd regen — should use opus
        # (call[0]=plan, call[1]=1st regen, call[2]=2nd regen, call[3]=dev;
        # preflight mocked separately)
        regen_call = mock_agent.call_args_list[2]
        regen_profile = regen_call.kwargs.get("profile") or regen_call[1].get("profile")
        assert regen_profile.model == "opus", (
            f"Expected opus model after escalation, got {regen_profile.model}"
        )

        # The regen prompt should contain the escalation note
        regen_prompt = regen_call.kwargs.get("prompt") or regen_call[1].get("prompt")
        assert "MODEL ESCALATION" in regen_prompt

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_disabled_by_default(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        tmp_path,
    ):
        """Config without plan_agent_review section — PLAN_REVIEW is skipped."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision is None

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_skipped_on_plan_injection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        tmp_path,
    ):
        """`--plan` flag skips agent review."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Injected plan\n\nUse this.", encoding="utf-8")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [_make_agent_result(success=True, output="Implemented.")]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        assert result.state.plan_review_decision is None
        assert len(result.state.plan_review_results) == 0

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_parse_failure(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Agent produces garbage — treated as REJECT, escalates after max retries."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nBetter plan.", cost_usd=0.12),
        ]
        # Plan review pool returns garbage → parse error → REJECT each time
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output="I think the plan looks okay!",
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output="Still looks fine to me",
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # plan review pool called twice; code review never reached
        assert mock_pool.call_count == 2

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_cost_in_audit(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        tmp_path,
    ):
        """Plan review cost appears in audit log."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.25,
                    profile_name="plan-review",
                )
            ],
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        result = run_task(config, task, interactive=True)
        audit = generate_audit_log(config, task, result)

        assert audit["plan_review"]["cost_usd"] == pytest.approx(0.25)
        assert result.state.total_plan_review_cost == pytest.approx(0.25)
        assert result.state.total_cost >= 0.25

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_regen_receives_rejection_findings(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Regenerated plan prompt includes rejection findings from P0 review."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review calls (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        # Plan review via pool; regen at index 1
        # (bad_plan=0, regen=1, dev=2; preflight mocked separately)
        regen_call = mock_agent.call_args_list[1]
        regen_prompt = regen_call.kwargs.get(
            "prompt", regen_call.args[0] if regen_call.args else ""
        )
        assert "Previous Plan Review Findings" in regen_prompt
        assert "architecturally broken" in regen_prompt.lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_pool_p0_from_one_reviewer_rejects(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Pool: P0 from one reviewer + APPROVE from another -> merged REJECT."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        cli="claude",
                        model="opus",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob", "Grep"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        cli="claude",
                        model="sonnet",
                        budget_usd=1.00,
                        timeout_seconds=300,
                        allowed_tools=("Read", "Glob"),
                    ),
                ],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_regen_count == 1
        assert len(result.state.plan_review_results) == 4  # 2 reviewers x 2 rounds

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_pool_all_fail_rejects(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Pool: all reviewers fail (exit_code != 0) -> REJECT."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        cli="claude",
                        model="opus",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        cli="claude",
                        model="sonnet",
                        budget_usd=1.00,
                        timeout_seconds=300,
                        allowed_tools=("Read", "Glob"),
                    ),
                ],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nRetried plan.", cost_usd=0.10),
        ]
        # Both reviewers fail each round
        mock_pool.side_effect = [
            [
                _make_agent_result(success=False, output="", profile_name="reviewer-a"),
                _make_agent_result(success=False, output="", profile_name="reviewer-b"),
            ],
            [
                _make_agent_result(success=False, output="", profile_name="reviewer-a"),
                _make_agent_result(success=False, output="", profile_name="reviewer-b"),
            ],
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_pool_p1_blocking_triggers_regen(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Pool: P1s from multiple reviewers block — regen triggered, findings attributed."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        cli="claude",
                        model="opus",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        cli="claude",
                        model="sonnet",
                        budget_usd=1.00,
                        timeout_seconds=300,
                        allowed_tools=("Read", "Glob"),
                    ),
                ],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: both reviewers REJECT(P1), then both APPROVE
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.03,
                    profile_name="reviewer-b",
                ),
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_regen_count > 0  # regen triggered by P1 pool findings
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2


# ── Structured logging tests ──────────────────────────────────────────


class TestPerRunLogCapture:
    """Tests for the per-run log file tee (_TeeStderr / _begin_run_log_tee)."""

    def _make_logging_config(self, tmp_path: Path, log_dir: Path) -> ForgeConfig:
        """Create a config with logging enabled, pointing at a tmp log directory."""
        return ForgeConfig(
            project="myproject",
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
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
            log=LogConfig(
                enabled=True,
                log_file=str(log_dir / "{project}" / "forge.log"),
            ),
        )

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_per_run_log_created(self, mock_shell, mock_agent, mock_preflight, tmp_path):
        """Per-run log file is created at the expected path."""
        import sys

        log_dir = tmp_path / "logs"
        config = self._make_logging_config(tmp_path, log_dir)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        original_stderr = sys.stderr
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task, run_id="abc123xyz")

        assert result.success is True

        # Per-run log file exists at expected path (project-local: .forge/logs/<slug>/run-<id>.log)
        per_run_path = tmp_path / ".forge" / "logs" / "test-task" / "run-abc123xyz.log"
        assert per_run_path.exists(), f"Expected log file not found: {per_run_path}"
        content = per_run_path.read_text(encoding="utf-8")
        assert len(content) > 0, "Per-run log is empty"

        # stderr is restored after run
        assert sys.stderr is original_stderr

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_per_run_log_absent_when_logging_disabled(
        self, mock_shell, mock_agent, mock_preflight, tmp_path
    ):
        """No per-run log file is created when log.enabled is False."""
        log_dir = tmp_path / "logs"
        config = ForgeConfig(
            project="myproject",
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
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
            log=LogConfig(enabled=False),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task)

        assert result.success is True
        assert not log_dir.exists(), "Log dir should not be created when logging disabled"

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_creates_per_run_log(
        self, mock_shell, mock_agent, mock_preflight, tmp_path
    ):
        """run_from_review() creates a per-run log file."""
        import sys

        log_dir = tmp_path / "logs"
        config = self._make_logging_config(tmp_path, log_dir)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        original_stderr = sys.stderr
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_from_review(config, task, workspace, run_id="reviewrun1")

        assert result.success is True
        per_run_path = tmp_path / ".forge" / "logs" / "test-task" / "run-reviewrun1.log"
        assert per_run_path.exists(), f"Expected log file not found: {per_run_path}"
        assert sys.stderr is original_stderr

    def test_begin_run_log_tee_skipped_in_worker_thread(self, tmp_path):
        """_begin_run_log_tee returns None when called from a non-main thread."""
        import sys
        import threading

        from theforge.coordinator.log_tee import _begin_run_log_tee
        from theforge.coordinator.logging import StructuredLogger

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        config = self._make_logging_config(tmp_path, log_dir)
        logger = StructuredLogger(
            run_id="tee-test",
            project="test",
            task="some-slug",
            log_file=str(tmp_path / "forge.log"),
            enabled=True,
            project_root=tmp_path,
        )

        results: list = []
        original_stderr = sys.stderr

        def worker():
            tee = _begin_run_log_tee(config, logger, "some-slug", log_dir=log_dir)
            results.append(tee)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # Tee must be None (skipped) in worker thread
        assert results[0] is None, "Expected tee to be skipped in worker thread"
        # sys.stderr must be untouched
        assert sys.stderr is original_stderr

    def test_begin_run_log_tee_active_on_main_thread(self, tmp_path):
        """_begin_run_log_tee installs tee when called from the main thread."""
        import sys

        from theforge.coordinator.log_tee import _begin_run_log_tee, _end_run_log_tee
        from theforge.coordinator.logging import StructuredLogger

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        config = self._make_logging_config(tmp_path, log_dir)
        logger = StructuredLogger(
            run_id="tee-main",
            project="test",
            task="main-slug",
            log_file=str(tmp_path / "forge.log"),
            enabled=True,
            project_root=tmp_path,
        )
        original_stderr = sys.stderr

        tee = _begin_run_log_tee(config, logger, "main-slug", log_dir=log_dir)
        try:
            assert tee is not None, "Expected tee to be active on main thread"
            assert sys.stderr is not original_stderr
        finally:
            _end_run_log_tee(tee)

        assert sys.stderr is original_stderr


# ── Project-local log directory tests ───────────────────────────────


class TestProjectLocalLogDir:
    """Tests for per-story log directory creation and artifact writes."""

    def _make_config(self, tmp_path: Path) -> ForgeConfig:
        return ForgeConfig(
            project="testproj",
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
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
            log=LogConfig(enabled=True),
        )

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_story_log_dir_created(self, mock_shell, mock_agent, mock_preflight, tmp_path):
        """Per-story log directory created under <project_root>/.forge/logs/<slug>/."""
        config = self._make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task)

        assert result.success is True
        story_log_dir = tmp_path / ".forge" / "logs" / "test-task"
        assert story_log_dir.is_dir(), f"Story log dir not created: {story_log_dir}"
        assert result.state.log_dir == story_log_dir

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_yaml_written(self, mock_shell, mock_agent, mock_preflight, tmp_path):
        """preflight.yaml written to story log dir after PREFLIGHT phase."""
        config = self._make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task)

        assert result.success is True
        import yaml as _yaml

        preflight_path = tmp_path / ".forge" / "logs" / "test-task" / "preflight.yaml"
        assert preflight_path.exists(), "preflight.yaml not written"
        data = _yaml.safe_load(preflight_path.read_text())
        assert "verdict" in data

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_cycle_artifacts_written(
        self, mock_shell, mock_agent, mock_preflight, tmp_path
    ):
        """Review cycle artifacts written per reviewer and synthesized."""
        config = self._make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(
                    success=True, output=APPROVE_REVIEW, profile_name="claude-reviewer"
                )
            ]
            result = run_task(config, task)

        assert result.success is True
        cycle_dir = tmp_path / ".forge" / "logs" / "test-task" / "review-cycle-1"
        assert cycle_dir.is_dir(), f"review-cycle-1 dir not created: {cycle_dir}"
        synthesized = cycle_dir / "synthesized.yaml"
        assert synthesized.exists(), "synthesized.yaml not written"

    def test_sprint_nesting(self, tmp_path):
        """Sprint passes sprint_name and creates sprint-level log dir + sprint-summary.yaml."""
        import yaml as _yaml

        from theforge.coordinator.state import CoordinatorState, Phase
        from theforge.sprint import run_sprint

        spec = tmp_path / "story.md"
        spec.write_text("---\nslug: my-story\n---\n# Story", encoding="utf-8")
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            _yaml.dump({"name": "my-sprint", "budget_usd": 10.0, "specs": ["story.md"]}),
            encoding="utf-8",
        )

        config = self._make_config(tmp_path)

        # Mock run_task to return a successful result with a log_dir
        _state = CoordinatorState()
        _state.log_dir = tmp_path / ".forge" / "logs" / "my-sprint" / "my-story"
        _state.log_dir.mkdir(parents=True, exist_ok=True)

        class _FakeResult:
            success = True
            phase = Phase.DONE
            state = _state
            merge = None
            message = "done"

        captured_kwargs: dict = {}

        def _fake_run_task(cfg, tsk, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResult()

        with (
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.coordinator.audit.generate_audit_log", return_value={"task": {}}),
        ):
            run_sprint(config, manifest_path)

        # run_task called with sprint_name="my-sprint"
        assert captured_kwargs.get("sprint_name") == "my-sprint"

        # Sprint-level log dir exists
        sprint_log_dir = tmp_path / ".forge" / "logs" / "my-sprint"
        assert sprint_log_dir.is_dir(), f"Sprint log dir not created: {sprint_log_dir}"

        # sprint-summary.yaml written
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        assert summary_path.exists(), "sprint-summary.yaml not written"
        data = _yaml.safe_load(summary_path.read_text())
        assert data["sprint"]["name"] == "my-sprint"


class TestSigtermHandler:
    """Tests for _make_sigterm_handler crash diagnostics."""

    def _make_task(self, tmp_path: Path) -> "TaskStory":
        spec = tmp_path / "spec.md"
        spec.write_text("# spec")
        return TaskStory(name="Test Task", slug="test-task", story_path=spec)

    def _make_config_no_ntfy(self, tmp_path: Path) -> ForgeConfig:
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
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
            log=LogConfig(enabled=True, log_file=str(tmp_path / "forge.log")),
        )

    def _make_config_with_ntfy(self, tmp_path: Path) -> ForgeConfig:
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
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
            log=LogConfig(enabled=True, log_file=str(tmp_path / "forge.log")),
            notifications=NotificationConfig(
                backend="ntfy",
                ntfy=NtfyConfig(url="https://ntfy.sh/test-topic", priority="default"),
            ),
        )

    def test_crash_handler_emits_all_fields(self, tmp_path: Path) -> None:
        """Handler emits run_end:crashed with all required context fields."""
        import signal as _signal
        import time

        from theforge.coordinator.engine import _make_sigterm_handler
        from theforge.coordinator.logging import StructuredLogger
        from theforge.coordinator.state import CoordinatorState

        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="test-run",
            project="test",
            task="test-task",
            log_file=str(log_file),
            enabled=True,
            project_root=tmp_path,
        )
        # Emit one event so last_event is non-empty
        logger.emit("phase_start", phase="DEV")

        state = CoordinatorState()
        state.phase = Phase.DEV
        state.dev_iteration = 2
        # total_cost is a computed property over dev_results
        state.dev_results.append(
            AgentResult(
                success=True,
                output="",
                session_id=None,
                cost_usd=0.57,
                exit_code=0,
                raw={},
                profile_name="dev",
            )
        )

        task = self._make_task(tmp_path)
        config = self._make_config_no_ntfy(tmp_path)
        task_start = time.monotonic() - 10.0  # pretend 10s have elapsed

        captured: list[dict] = []
        original_safe_emit = logger._safe_emit

        def _capture_safe_emit(event: str, **fields: object) -> None:
            captured.append({"event": event, **fields})
            original_safe_emit(event, **fields)

        logger._safe_emit = _capture_safe_emit  # type: ignore[method-assign]

        handler = _make_sigterm_handler(
            logger,
            None,
            _signal.SIG_DFL,
            state=state,
            task_start=task_start,
            task=task,
            config=config,
        )

        with patch("os.kill"):
            handler(_signal.SIGTERM, None)

        assert len(captured) == 1
        ev = captured[0]
        assert ev["event"] == "run_end"
        assert ev["outcome"] == "crashed"
        assert ev["signal"] == _signal.SIGTERM
        assert ev["signal_name"] == "SIGTERM"
        assert ev["phase_at_crash"] == "DEV"
        assert ev["iteration_at_crash"] == 2
        assert ev["cost_at_crash"] == round(0.57, 6)
        assert ev["last_event"] == "phase_start"
        assert ev["uptime_seconds"] >= 9.0  # at least 9s given 10s offset

    def test_crash_handler_calls_ntfy_when_configured(self, tmp_path: Path) -> None:
        """Handler calls _ntfy_crash_notify when ntfy is configured."""
        import signal as _signal

        from theforge.coordinator.engine import _make_sigterm_handler
        from theforge.coordinator.logging import StructuredLogger
        from theforge.coordinator.state import CoordinatorState

        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="test-run",
            project="test",
            task="test-task",
            log_file=str(log_file),
            enabled=True,
            project_root=tmp_path,
        )

        state = CoordinatorState()
        state.phase = Phase.PLAN_REVIEW
        state.dev_iteration = 0

        task = self._make_task(tmp_path)
        config = self._make_config_with_ntfy(tmp_path)

        handler = _make_sigterm_handler(
            logger,
            None,
            _signal.SIG_DFL,
            state=state,
            task_start=0.0,
            task=task,
            config=config,
        )

        with (
            patch("os.kill"),
            patch("theforge.coordinator.notify._ntfy_crash_notify") as mock_crash_notify,
        ):
            handler(_signal.SIGTERM, None)

        mock_crash_notify.assert_called_once()
        call_kwargs = mock_crash_notify.call_args
        assert call_kwargs[0][0] is task
        assert call_kwargs[0][1] is state
        assert call_kwargs[0][2] is config

    def test_crash_handler_no_ntfy_when_not_configured(self, tmp_path: Path) -> None:
        """_ntfy_publish is never called end-to-end when ntfy is not configured."""
        import signal as _signal

        from theforge.coordinator.engine import _make_sigterm_handler
        from theforge.coordinator.logging import StructuredLogger
        from theforge.coordinator.state import CoordinatorState

        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="test-run",
            project="test",
            task="test-task",
            log_file=str(log_file),
            enabled=True,
            project_root=tmp_path,
        )

        state = CoordinatorState()
        state.phase = Phase.DEV
        task = self._make_task(tmp_path)
        config = self._make_config_no_ntfy(tmp_path)

        handler = _make_sigterm_handler(
            logger,
            None,
            _signal.SIG_DFL,
            state=state,
            task_start=0.0,
            task=task,
            config=config,
        )

        with patch("os.kill"), patch("theforge.coordinator.notify._ntfy_publish") as mock_publish:
            handler(_signal.SIGTERM, None)

        # When ntfy is not configured, _ntfy_crash_notify guards internally and
        # _ntfy_publish must never be called.
        mock_publish.assert_not_called()


# ── Stage-aware pipeline tests ────────────────────────────────────────


class TestParsePhaseNameUtility:
    """Unit tests for parse_phase_name()."""

    def test_all_valid_names(self):
        expected = {
            "init": Phase.INIT,
            "workspace": Phase.WORKSPACE,
            "preflight": Phase.PREFLIGHT,
            "plan": Phase.PLAN,
            "plan-review": Phase.PLAN_REVIEW,
            "dev": Phase.DEV,
            "validate": Phase.VALIDATE,
            "review": Phase.REVIEW,
            "human-review": Phase.HUMAN_REVIEW,
        }
        for name, phase in expected.items():
            assert parse_phase_name(name) == phase

    def test_case_insensitive(self):
        assert parse_phase_name("DEV") == Phase.DEV
        assert parse_phase_name("Plan-Review") == Phase.PLAN_REVIEW

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown phase name"):
            parse_phase_name("unknown-phase")

    def test_phase_ordering(self):
        """Phase enum values must be ordered INIT < WORKSPACE < ... < REVIEW."""
        phases = [
            Phase.INIT,
            Phase.WORKSPACE,
            Phase.PREFLIGHT,
            Phase.PLAN,
            Phase.PLAN_REVIEW,
            Phase.DEV,
            Phase.VALIDATE,
            Phase.REVIEW,
        ]
        for i in range(len(phases) - 1):
            assert phases[i].value < phases[i + 1].value, (
                f"{phases[i].name}.value ({phases[i].value}) should be < "
                f"{phases[i + 1].name}.value ({phases[i + 1].value})"
            )


class TestUntilPhaseStop:
    """Tests for --until phase stop behaviour."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_preflight_stops_after_preflight(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until preflight: run PREFLIGHT, then stop without entering DEV."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(success=True, output=PREFLIGHT_PROCEED)

        result = run_task(config, task, stop_phase=Phase.PREFLIGHT)

        assert result.success is True
        assert result.message == "Stopped at --until preflight"
        assert result.phase == Phase.PREFLIGHT
        # DEV agent should NOT have been called (only preflight)
        assert result.state.dev_iteration == 0
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_validate_stops_after_validate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until validate: run DEV+VALIDATE, then stop without REVIEW."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        result = run_task(config, task, stop_phase=Phase.VALIDATE)

        assert result.success is True
        assert result.message == "Stopped at --until validate"
        assert result.phase == Phase.VALIDATE
        assert result.state.dev_trace_count == 1
        # REVIEW should NOT have been called
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_review_stops_after_first_review(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until review: run DEV+VALIDATE+REVIEW, then stop (no retry on REQUEST_CHANGES)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        # Return REQUEST_CHANGES — without --until, this would retry DEV
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, stop_phase=Phase.REVIEW)

        assert result.success is True
        assert result.message == "Stopped at --until review"
        assert result.phase == Phase.REVIEW
        # Only one DEV call — did not loop back (dev_iteration resets after review)
        assert result.state.dev_trace_count == 1


class TestFromPhaseSkip:
    """Tests for --from phase skip behaviour."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_from_dev_skips_preflight_and_plan(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--from dev: WORKSPACE is reused, PREFLIGHT/PLAN skipped, DEV runs."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        # Put .forge/plan.md so the coordinator doesn't complain
        plan_path = workspace / ".forge" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("# Plan\n- step 1", encoding="utf-8")

        def shell_side(cmd, cwd=None, **kw):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "forge/test-task")
            return _shell_with_gate(workspace, "PASS")(cmd, cwd, **kw)

        mock_shell.side_effect = shell_side
        # Only dev agent and review pool should be called
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, start_phase=Phase.DEV)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Preflight verdict should be SKIPPED
        assert result.state.preflight_verdict == "SKIPPED"
        # run_agent called only once (for dev, not preflight)
        assert mock_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_from_dev_until_validate_combined(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--from dev --until validate: DEV→VALIDATE then stop."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_path = workspace / ".forge" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("# Plan\n", encoding="utf-8")

        def shell_side(cmd, cwd=None, **kw):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "forge/test-task")
            return _shell_with_gate(workspace, "PASS")(cmd, cwd, **kw)

        mock_shell.side_effect = shell_side
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        result = run_task(config, task, start_phase=Phase.DEV, stop_phase=Phase.VALIDATE)

        assert result.success is True
        assert result.message == "Stopped at --until validate"
        assert result.phase == Phase.VALIDATE
        assert result.state.preflight_verdict == "SKIPPED"
        mock_pool.assert_not_called()


class TestAuditStartStopPhase:
    """Audit log records start/stop phases."""

    def test_audit_records_start_stop_phase(self, tmp_path):
        from theforge.coordinator.state import CoordinatorState

        state = CoordinatorState()
        state.start_phase = Phase.DEV
        state.stop_phase = Phase.VALIDATE

        from theforge.coordinator.audit import generate_audit_log
        from theforge.coordinator.engine import CoordinatorResult

        result = CoordinatorResult(
            success=True,
            phase=Phase.VALIDATE,
            state=state,
            message="Stopped at --until validate",
        )
        task = _make_task(tmp_path)
        audit = generate_audit_log(_make_config(tmp_path), task, result)

        assert audit["outcome"]["start_phase"] == "DEV"
        assert audit["outcome"]["stop_phase"] == "VALIDATE"

    def test_audit_none_start_stop_when_unset(self, tmp_path):
        from theforge.coordinator.state import CoordinatorState

        state = CoordinatorState()

        from theforge.coordinator.audit import generate_audit_log
        from theforge.coordinator.engine import CoordinatorResult

        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
        )
        task = _make_task(tmp_path)
        audit = generate_audit_log(_make_config(tmp_path), task, result)

        assert audit["outcome"]["start_phase"] is None
        assert audit["outcome"]["stop_phase"] is None
