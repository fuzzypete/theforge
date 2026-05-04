"""Tests for approve-path cycle history, escalation note, and already-done override."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    PREFLIGHT_ALREADY_DONE,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase


def _make_smart_config(
    tmp_path: Path,
    models: list[str] | None = None,
    max_review_cycles: int = 3,
) -> ForgeConfig:
    """Create a ForgeConfig with models set (claude/sonnet as dev)."""
    if models is None:
        models = ["claude/sonnet", "claude/opus"]
    dev_profile = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=30.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )
    review_profile = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=10.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
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
        review_pool=[review_profile],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=max_review_cycles,
            auto_model_escalation=True,
        ),
        models=models,
    )


_PERSISTENT_P1_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Persistent issue found."
findings:
  - severity: P1
    file: src/cli.py
    line: 42
    observed: "cli.py never wires gate_override into TaskStory"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Wire it"
story_compliance:
  matches_spec: false
  mismatches:
    - "Missing wiring"
test_coverage:
  adequate: false
  gaps:
    - "No test for gate_override"
```
"""


class TestApprovePathCycleHistory:
    """Integration tests verifying APPROVE path records cycle history."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_approve_records_cycle_in_history(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Non-interactive APPROVE run records the approved cycle in state.cycle_history."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert len(result.state.cycle_history) == 1
        assert result.state.cycle_history[0].verdict == "APPROVE"
        assert result.state.cycle_history[0].cycle == 1


class TestEscalationNoteOnRejectPath:
    """Integration test: escalation note is delivered on reject-after-escalation path."""

    @patch("theforge.coordinator.review_phase._human_review")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_note_in_prompt_after_reject(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        tmp_path,
    ):
        """Persistent P1 + exhausted cycles + human reject: next dev prompt has escalation note."""
        config = _make_smart_config(tmp_path, max_review_cycles=2)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Capture prompts passed to dev agent
        captured_prompts: list[str] = []

        def agent_side_effect(**kwargs):
            captured_prompts.append(kwargs.get("prompt", ""))
            return _make_agent_result(success=True, output="Done.")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect

        # Cycle 1 + Cycle 2: same P1 → persistent P1 fires on cycle 2, exhausted
        pool_call = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_call["n"] += 1
            if pool_call["n"] <= 2:
                return [
                    _make_agent_result(
                        success=True,
                        output=_PERSISTENT_P1_REVIEW,
                        profile_name="claude-opus",
                    )
                ]
            # After reject: approve so the run completes
            return [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
            ]

        mock_pool.side_effect = pool_side_effect

        # Cycle 2 exhausted → human review: reject once, then approve
        human_review_call = {"n": 0}

        def human_review_side_effect(*args, **kwargs):
            human_review_call["n"] += 1
            if human_review_call["n"] == 1:
                return ("reject", "Start fresh with the escalated model.")
            return ("approve", None)

        mock_human_review.side_effect = human_review_side_effect

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.dev_escalated is True

        # Flow: prompts[0]=initial dev, prompts[1]=after cycle-1 (build_fix_prompt),
        # prompts[2]=after reject (build_dev_prompt with escalation_note)
        assert len(captured_prompts) >= 3, f"Expected >=3 dev prompts, got {len(captured_prompts)}"
        post_reject_prompt = captured_prompts[2]  # dev call after exhausted+reject
        assert "MODEL ESCALATION" in post_reject_prompt, (
            "Escalation note missing from dev prompt after reject"
        )
        assert "Previous Review Cycles" in post_reject_prompt, (
            "Cycle history missing from dev prompt after reject"
        )


# ── Already-done override tests ─────────────────────────────────────────


class TestAlreadyDoneOverride:
    """ALREADY_DONE guard: commits + no prior APPROVE → override to REVIEW."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=False)
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=False)
    def test_already_done_with_commits_no_approve_resumes_review(
        self,
        mock_approve,
        mock_merged,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        tmp_path,
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
    @patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=True)
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=False)
    def test_already_done_with_commits_on_merged_branch_honours_already_done(
        self,
        mock_approve,
        mock_merged,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        tmp_path,
    ):
        """ALREADY_DONE with commits ahead on an already-merged branch stays DONE."""
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
            if "--oneline" in cmd and "git log" in cmd:
                return (True, "abc123 a commit")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "ALREADY_DONE"
        assert len(result.state.review_results) == 0
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=False)
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=False)
    def test_already_done_with_no_commits_ahead_honours_already_done(
        self,
        mock_approve,
        mock_merged,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        tmp_path,
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
