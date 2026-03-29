"""Tests for coordinator dev phase: prompt routing and session resume.

Covers: prompt routing (fix vs dev prompt) and session resume/carry-through.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.runners import AgentResult


class TestCoordinatorPromptRouting:
    """Test that the correct prompt builder is called based on retry_reason."""

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_changes_routes_to_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """retry_reason='review_changes' → build_fix_prompt() on second dev call."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

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
        assert mock_fix_prompt.called, "build_fix_prompt should be called on review iteration"
        assert mock_dev_prompt.call_count >= 1, "build_dev_prompt should be called on first run"

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_fail_routes_to_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """retry_reason='gate_fail' → build_dev_prompt() on retry, not build_fix_prompt()."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_iteration == 2
        assert not mock_fix_prompt.called, "build_fix_prompt must NOT be called on gate_fail retry"
        assert mock_dev_prompt.call_count == 2, "build_dev_prompt should be called both times"

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_phase._human_review")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_extend_routes_to_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_human_review,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """retry_reason='extend' → build_fix_prompt() when human extends after APPROVE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        extend_review = """\
```yaml
verdict: APPROVE
summary: "Acceptable but please also add logging."
findings:
  - severity: P2
    file: src/foo.py
    line: 5
    description: "Missing log statement"
    suggestion: "Add logger.info(...)"
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(success=True, output=extend_review, profile_name="review")
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        # First call: human extends; second call: human approves
        human_call = {"n": 0}

        def human_review_side_effect(*args, **kwargs):
            human_call["n"] += 1
            if human_call["n"] == 1:
                return ("extend", "")
            return ("approve", "")

        mock_human_review.side_effect = human_review_side_effect

        run_task(config, task, interactive=True)

        assert mock_fix_prompt.called, "build_fix_prompt should be called after extend"

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_phase._human_review")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_exhausted_cycles_extend_zero_findings_uses_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_human_review,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """Exhausted-cycles extend with empty findings still routes to build_fix_prompt.

        REQUEST_CHANGES with no explicit findings can happen (e.g., reviewer says
        'rewrite the approach' without listing individual findings). When cycles are
        exhausted and the human extends, last_review_findings must be populated
        unconditionally so the next DEV iteration uses fix-prompt, not dev-prompt.
        """
        # max_review_cycles=1 so the first REQUEST_CHANGES exhausts the budget
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
            retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=1),
            log=LogConfig(enabled=False),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        # REQUEST_CHANGES with empty findings list
        empty_findings_rc = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Please rethink the approach entirely."
findings: []
story_compliance:
  matches_spec: false
test_coverage:
  adequate: false
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=empty_findings_rc, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        # First call: human extends (cycles exhausted); second call: human approves
        human_call = {"n": 0}

        def human_review_side_effect(*args, **kwargs):
            human_call["n"] += 1
            if human_call["n"] == 1:
                return ("extend", "")
            return ("approve", "")

        mock_human_review.side_effect = human_review_side_effect

        run_task(config, task, interactive=True)

        assert mock_fix_prompt.called, (
            "build_fix_prompt must be called after exhausted-cycles extend "
            "even when findings list is empty"
        )


class TestCoordinatorSessionResume:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_session_carried_on_timeout(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        timeout_result = AgentResult(
            success=False,
            output="TIMEOUT",
            session_id="sess-timeout",
            cost_usd=0.10,
            exit_code=-9,
            raw={},
            profile_name="dev",
        )
        resumed_result = _make_agent_result(
            success=True, output="Done.", session_id="sess-resumed", profile_name="dev"
        )
        dev_session_ids: list[str | None] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_session_ids.append(session_id)
            if len(dev_session_ids) == 1:
                return timeout_result
            return resumed_result

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert dev_session_ids == [None, "sess-timeout"]
        assert result.state.dev_session_id == "sess-resumed"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_dev_session_carried_across_review_cycles(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dev_session_ids: list[str | None] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_session_ids.append(session_id)
            if len(dev_session_ids) == 1:
                return _make_agent_result(
                    success=True,
                    output="Implemented.",
                    session_id="dev-sess-1",
                    profile_name="dev",
                )
            return _make_agent_result(
                success=True, output="Fixed.", session_id="dev-sess-2", profile_name="dev"
            )

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert dev_session_ids == [None, "dev-sess-1"]
        assert result.state.dev_session_id == "dev-sess-2"

    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_timeout_resume_uses_short_prompt(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_dev_prompt, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "full dev prompt"
        dev_prompts: list[str] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_prompts.append(prompt)
            if len(dev_prompts) == 1:
                return AgentResult(
                    success=False,
                    output="TIMEOUT",
                    session_id="sess-timeout",
                    cost_usd=0.10,
                    exit_code=-9,
                    raw={},
                    profile_name="dev",
                )
            return _make_agent_result(success=True, output="Done.", profile_name="dev")

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert dev_prompts[0] == "full dev prompt"
        assert "You were cut off by a timeout." in dev_prompts[1]
        assert dev_prompts[1] != "full dev prompt"

    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_timeout_resume_dirty_worktree_auto_committed(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_dev_prompt, tmp_path
    ):
        """Timeout on iter 1, success on iter 2, dirty worktree → auto-commit (no retry)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "full dev prompt"
        dev_prompts: list[str] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_prompts.append(prompt)
            if len(dev_prompts) == 1:
                return AgentResult(
                    success=False,
                    output="TIMEOUT",
                    session_id="sess-timeout",
                    cost_usd=0.10,
                    exit_code=-9,
                    raw={},
                    profile_name="dev",
                )
            return _make_agent_result(success=True, output="Done.", profile_name="dev")

        shell_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            shell_cmds.append(cmd)
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/foo.py")
            if "git add" in cmd:
                return (True, "")
            if "git commit" in cmd:
                return (True, "")

            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("theforge.coordinator.validate_phase.subprocess.run"):
            result = run_task(config, task)

        assert result.success is True
        # Timeout iter → gate passes → auto-commit → REVIEW → DONE
        assert mock_dev_prompt.call_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_reviewer_sessions_accumulate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
        ]

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.reviewer_session_ids == {"review": "review-sess-2"}

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_reviewer_sessions_passed_to_pool(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_session_ids: list[list[str | None]] = []

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
        ]

        def pool_side_effect(**kwargs):
            captured_session_ids.append(list(kwargs["session_ids"]))
            if len(captured_session_ids) == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert captured_session_ids == [[None], ["review-sess-1"]]
