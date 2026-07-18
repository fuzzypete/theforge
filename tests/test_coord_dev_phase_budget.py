"""Tests for coordinator dev phase: cost governance and dev notes.

Covers: per-story dev cost estimates (surfaced, not enforced), review-agent
budget ceilings, and dev notes injection into review prompts.

Per-story dollar values are historical-cost estimates, not enforced budgets.
When a dev attempt exceeds its estimate the outcome depends on whether it
produced usable output: committed work proceeds (the estimate was low), while a
no-commit attempt escalates on unproductive-attempt semantics — it names that no
usable output was produced, not a dollar overrun. Real post-hoc dollar
governance lives at the sprint level (forge.yaml budget_usd).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _as_detailed,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.runners import AgentResult


class TestCoordinatorBudgetEnforcement:
    """Per-story dev cost is an estimate (not enforced); review budgets are ceilings."""

    def _make_budget_config(
        self, tmp_path: Path, dev_budget: float, review_budget: float
    ) -> ForgeConfig:
        """Create a config with tight budgets for testing."""
        dev_profile = ModelProfile(
            name=DEFAULT_DEV_PROFILE.name,
            cli=DEFAULT_DEV_PROFILE.cli,
            provider=None,
            model=DEFAULT_DEV_PROFILE.model,
            budget_usd=dev_budget,
            timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
        )
        review_profile = ModelProfile(
            name=DEFAULT_REVIEW_PROFILE.name,
            cli=DEFAULT_REVIEW_PROFILE.cli,
            provider=None,
            model=DEFAULT_REVIEW_PROFILE.model,
            budget_usd=review_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
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
            retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=3),
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_dev_estimate_exceeded_no_commits_first_call(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev exceeds estimate with no commits on first call → ESCALATE (unproductive)."""
        config = self._make_budget_config(tmp_path, dev_budget=0.40, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)

        # Agent costs $0.50, budget is $0.40 → immediate escalation
        expensive_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = expensive_result

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # No commits + high spend → unproductive-attempt framing, not a dollar overrun.
        assert "no usable output" in result.message.lower()
        assert "no commits" in result.message.lower()
        assert "budget" not in result.message.lower()
        assert "0.5000" in result.message
        # Only one dev invocation — escalated before retry
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_dev_estimate_exceeded_no_commits_on_retry(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev exceeds estimate with no commits on second call (retry) → ESCALATE."""
        config = self._make_budget_config(tmp_path, dev_budget=0.50, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])

        retry_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = retry_result

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "no usable output" in result.message.lower()
        assert "budget" not in result.message.lower()
        # Two dev invocations: $0.30 + $0.30 = $0.60 > $0.50
        assert len(result.state.dev_results) == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_review_budget_all_exceeded(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """All reviewers over budget → ESCALATE (no reviews to synthesize)."""
        config = self._make_budget_config(tmp_path, dev_budget=2.00, review_budget=0.40)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.10,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        # Profile name must match pool entry name for per-profile enforcement
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="review",
        )

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = dev_result
        mock_pool.return_value = [review_result]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "review" in result.message
        assert len(result.state.review_agent_results) == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    @patch("theforge.coordinator.dev_phase._commits_exist_strict", return_value=True)
    def test_dev_budget_exceeded_but_commits_present_proceeds(
        self, mock_commits, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev exceeds budget but committed work → honour the result, proceed to DONE."""
        config = self._make_budget_config(tmp_path, dev_budget=0.40, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        expensive_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = expensive_result
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True, f"Expected DONE but got ESCALATE: {result.message}"
        assert result.phase == Phase.DONE
        mock_commits.assert_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    @patch("theforge.coordinator.dev_phase._commits_exist_strict", return_value=False)
    def test_dev_estimate_exceeded_no_commits_escalates_unproductive(
        self, mock_commits, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev exceeds estimate with no commits → ESCALATE as unproductive (no work to salvage)."""
        config = self._make_budget_config(tmp_path, dev_budget=0.40, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)

        expensive_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = expensive_result

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "no usable output" in result.message.lower()
        assert "no commits" in result.message.lower()
        assert "budget" not in result.message.lower()


# ── Dev notes and handoff validation tests ──────────────────────────────


class TestCoordinatorDevNotes:
    """Test that coordinator reads dev_notes from handoff.yaml and passes to review prompt."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_dev_notes_passed_to_review_prompt(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Coordinator injects dev_notes from forge handoff artifact into review prompt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # dev_handoff dict simulates the parsed <forge_handoff> block from the agent output
        dev_handoff_dict = {
            "summary": "I deviated from spec because it was better.",
            "commits": [{"sha": "abc1234", "message": "feat: implement"}],
            "acceptance_criteria": [{"criterion": "It works", "status": "MET", "notes": "tested"}],
            "story_deviations": [
                {
                    "description": "I deviated from spec because it was better.",
                    "justification": "Better approach",
                }
            ],
            "deferred_items": "none",
            "gate_result": "PASS",
        }

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", dev_handoff=dev_handoff_dict
        )

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
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_review_prompt_includes_verified_git_metadata(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Review prompt includes verified git log, diff stat, and diff content sections."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git log" in cmd and "--oneline --reverse" in cmd:
                return (True, "abc1234 feat: implement\ndef5678 test: add coverage")
            if "git diff main...HEAD --stat" in cmd:
                return (True, " src/foo.py | 2 ++\n tests/test_foo.py | 4 ++++")
            if cmd.strip() == "git diff main...HEAD":
                return (True, "diff --git a/src/foo.py b/src/foo.py\n+print('ok')")
            if "git show abc1234" in cmd:
                return (True, "commit abc1234\n...diff one...")
            if "git show def5678" in cmd:
                return (True, "commit def5678\n...diff two...")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
        assert any("## Verified Git Metadata" in p for p in captured_prompts)
        assert any("abc1234 feat: implement" in p for p in captured_prompts)
        assert any("src/foo.py | 2 ++" in p for p in captured_prompts)
        assert any("diff --git a/src/foo.py b/src/foo.py" in p for p in captured_prompts)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_review_prompt_warns_on_handoff_commit_mismatch(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Review phase fails closed when handoff commit list disagrees with git log."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mismatched_notes = (
            'summary: "Implemented the feature."\n'
            "commits:\n"
            '  - sha: "deadbee"\n'
            '    message: "feat: imaginary commit"\n'
            "acceptance_criteria:\n"
            '  - criterion: "It works"\n'
            "    status: MET\n"
            '    notes: "tested"\n'
            "story_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                handoff = {
                    "gate_decision": "PASS",
                    "validation": {"make_fmt": {"status": "PASS"}},
                    "scope_completed": ["test item"],
                    "dev_notes": mismatched_notes,
                }
                (Path(cwd) / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git log" in cmd and "--oneline --reverse" in cmd:
                return (True, "abc1234 feat: real commit")
            if "git diff main...HEAD --stat" in cmd:
                return (True, " src/foo.py | 1 +")
            if cmd.strip() == "git diff main...HEAD":
                return (True, "diff --git a/src/foo.py b/src/foo.py")
            if "git show abc1234" in cmd:
                return (True, "commit abc1234\n...diff...")
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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

        with patch(
            "theforge.coordinator.review_pool._get_handoff_commit_warning",
            return_value=(
                "⚠ WARNING: Dev handoff commit list does not match verified git history.\n"
                "Claims not found on branch:\n"
                "- deadbee feat: imaginary commit"
            ),
        ):
            result = run_task(config, task)

        assert result.success is True
        assert captured_prompts
        assert any(
            "Dev handoff commit list does not match verified git history" in prompt
            for prompt in captured_prompts
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
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
