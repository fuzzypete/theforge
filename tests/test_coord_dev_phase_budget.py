"""Tests for coordinator dev phase: budget enforcement, dev notes, and handoff validation.

Covers: budget limits for dev and review agents, dev notes injection into review
prompts, and structured handoff validation after gate passes.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
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
from theforge.coordinator.engine import run_from_review, run_task
from theforge.coordinator.state import Phase
from theforge.runners import AgentResult
from theforge.task import TaskStory


class TestCoordinatorBudgetEnforcement:
    """Test that budget limits are enforced for dev and review agents."""

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
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_budget_exceeded_first_call(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev agent exceeds budget on first call → ESCALATE with budget error."""
        config = self._make_budget_config(tmp_path, dev_budget=0.40, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")

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
        assert "budget" in result.message.lower()
        assert "0.5000" in result.message
        assert "0.4000" in result.message
        # Only one dev invocation — escalated before retry
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_budget_exceeded_on_retry(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Dev agent exceeds budget on second call (retry) → ESCALATE."""
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
        assert "budget" in result.message.lower()
        # Two dev invocations: $0.30 + $0.30 = $0.60 > $0.50
        assert len(result.state.dev_results) == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
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


# ── Dev notes and handoff validation tests ──────────────────────────────


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

        # Write handoff.yaml with structured dev_notes containing the deviation
        dev_notes_yaml = (
            'summary: "I deviated from spec because it was better."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "It works"\n'
            "    status: MET\n"
            '    notes: "tested"\n'
            "story_deviations:\n"
            '  - description: "I deviated from spec because it was better."\n'
            '    justification: "Better approach"\n'
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )
        handoff = {
            "gate_decision": "PASS",
            "validation": {"make_fmt": {"status": "PASS"}},
            "scope_completed": ["test item"],
            "dev_notes": dev_notes_yaml,
        }
        (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
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
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
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
            if "git diff main --stat" in cmd:
                return (True, " src/foo.py | 2 ++\n tests/test_foo.py | 4 ++++")
            if cmd.strip() == "git diff main":
                return (True, "diff --git a/src/foo.py b/src/foo.py\n+print('ok')")
            if "git show abc1234" in cmd:
                return (True, "commit abc1234\n...diff one...")
            if "git show def5678" in cmd:
                return (True, "commit def5678\n...diff two...")
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
        assert any("## Verified Git Metadata" in p for p in captured_prompts)
        assert any("abc1234 feat: implement" in p for p in captured_prompts)
        assert any("src/foo.py | 2 ++" in p for p in captured_prompts)
        assert any("diff --git a/src/foo.py b/src/foo.py" in p for p in captured_prompts)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
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
            if "git diff main --stat" in cmd:
                return (True, " src/foo.py | 1 +")
            if cmd.strip() == "git diff main":
                return (True, "diff --git a/src/foo.py b/src/foo.py")
            if "git show abc1234" in cmd:
                return (True, "commit abc1234\n...diff...")
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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_legacy_handoff_escalates_when_unfixable(
        self, mock_shell, mock_agent, mock_engine_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Unstructured dev_notes that can't be repaired escalate (hard stop)."""
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
        mock_engine_agent.return_value = _make_agent_result(success=True, output="Unused.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is False
        assert result.state.phase.name == "ESCALATE"
        assert result.state.error_type == "invalid_dev_handoff"

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
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "Feature X works"\n'
            "    status: MET\n"
            '    notes: "Tested in test_x.py"\n'
            "story_deviations: none\n"
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
        spec = tmp_path / "spec.md"
        spec.write_text(
            "# Test Spec\n\n## Acceptance Criteria\n- It works\n",
            encoding="utf-8",
        )
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
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
            "story_deviations: none\n"
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
    def test_invalid_handoff_escalates_after_max_retries(
        self, mock_shell, mock_dev_agent, mock_engine_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Invalid dev handoff escalates after max retries exhausted."""
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

        # Should escalate — invalid handoff is now a hard stop
        assert result.success is False
        assert result.state.phase.name == "ESCALATE"
        assert result.state.error_type == "invalid_dev_handoff"
        # max_handoff_retries (2 by default)
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
        spec = tmp_path / "spec.md"
        spec.write_text(
            "# Test Spec\n\n## Acceptance Criteria\n- It works\n",
            encoding="utf-8",
        )
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
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
            "story_deviations: none\n"
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
        assert any(
            "Write the updated handoff file to disk and stop" in prompt
            for prompt in captured_fix_prompts
        )
        assert not any("git add .forge/handoff.yaml" in prompt for prompt in captured_fix_prompts)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_repaired_handoff_with_wrong_story_content_triggers_retry(
        self, mock_shell, mock_dev_agent, mock_engine_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Schema-valid repaired handoff must still match the active story."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            validation=dataclasses.replace(
                _make_config(tmp_path).validation,
                handoff_file=".forge/handoff.yaml",
            ),
        )
        spec = tmp_path / "spec.md"
        spec.write_text(
            "# Test Spec\n\n## Acceptance Criteria\n- Active criterion\n",
            encoding="utf-8",
        )
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        bad_notes = "just some unstructured text"
        wrong_story_notes = (
            'summary: "Implemented the thing."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "Wrong criterion"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "story_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )
        good_notes = (
            'summary: "Implemented the thing."\n'
            "commits:\n"
            '  - sha: "abc1234"\n'
            '    message: "feat: implement"\n'
            "acceptance_criteria:\n"
            '  - criterion: "Active criterion"\n'
            "    status: MET\n"
            '    notes: "yes"\n'
            "story_deviations: none\n"
            "deferred_items: none\n"
            "gate_result: PASS\n"
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                self._make_structured_handoff(Path(cwd), bad_notes, ".forge/handoff.yaml")
                return (True, "OK")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect

        captured_fix_prompts: list[str] = []
        fix_call_idx = {"n": 0}

        def engine_agent_side_effect(**kwargs):
            fix_call_idx["n"] += 1
            captured_fix_prompts.append(kwargs.get("prompt", ""))
            notes = wrong_story_notes if fix_call_idx["n"] == 1 else good_notes
            self._make_structured_handoff(workspace, notes, ".forge/handoff.yaml")
            return _make_agent_result(success=True, output="Fixed handoff.")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_engine_agent.side_effect = engine_agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert fix_call_idx["n"] == 2
        assert any("## Active Story" in prompt for prompt in captured_fix_prompts)
        assert any("Wrong criterion" in prompt for prompt in captured_fix_prompts)
        assert any("- Active criterion" in prompt for prompt in captured_fix_prompts)
