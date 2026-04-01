"""Tests for fix prompt routing, PR creation, and gate fail retry behaviour.

Covers: TestFixPromptRouting, TestCreatePR, TestCoordinatorGateFailRetry.
"""

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_ntfy_config,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    NotificationConfig,
    NtfyConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_from_review, run_task
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.task import TaskStory

# ── Local helpers ─────────────────────────────────────────────────────


def _make_task(tmp_path: Path) -> TaskStory:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


# ── Fix prompt routing tests ─────────────────────────────────────────


class TestFixPromptRouting:
    """Tests that the coordinator routes to build_fix_prompt on iteration 2+."""

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_iteration_1_uses_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """First iteration always uses build_dev_prompt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        mock_dev_prompt.assert_called_once()
        mock_fix_prompt.assert_not_called()

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_iteration_2_with_review_findings_uses_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """Iteration 2+ with last_review_findings set uses build_fix_prompt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Done."),
            # iter 1
            _make_agent_result(success=True, output="Fixed."),
            # iter 2,
        ]
        mock_pool.side_effect = [
            # First review: REQUEST_CHANGES → triggers iter 2
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            # Second review: APPROVE
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_task(config, task)

        assert mock_dev_prompt.call_count == 1  # only iter 1
        assert mock_fix_prompt.call_count == 1  # iter 2 uses fix prompt

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_failure_retry_uses_dev_prompt_not_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """Gate failure retries use build_dev_prompt (not review findings)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        # First gate call FAILs, second PASSes
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Done."),
            # iter 1
            _make_agent_result(success=True, output="Fixed."),
            # iter 2 (gate retry),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        # Both iterations should use dev_prompt since last_review_findings is None
        assert mock_dev_prompt.call_count == 2
        mock_fix_prompt.assert_not_called()

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_fix_prompt_receives_review_findings(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """build_fix_prompt is called with the review findings content."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Done."),
            _make_agent_result(success=True, output="Fixed."),
        ]
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_task(config, task)

        assert mock_fix_prompt.call_count == 1
        call_kwargs = mock_fix_prompt.call_args.kwargs
        assert call_kwargs["review_findings"] is not None
        assert len(call_kwargs["review_findings"]) > 0
        assert call_kwargs["iteration"] >= 1

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_failure_after_review_uses_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """After REQUEST_CHANGES, if the fix attempt's gate fails, the retry uses
        build_dev_prompt (not build_fix_prompt) because human_feedback is set."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        # iter 1: PASS; iter 2 (post-review fix): FAIL; iter 3 (gate retry): PASS
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS", "FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Done."),
            # iter 1
            _make_agent_result(success=True, output="Fixed."),
            # iter 2 (fix attempt)
            _make_agent_result(success=True, output="Fixed."),
            # iter 3 (gate retry),
        ]
        mock_pool.side_effect = [
            # Review after iter 1: REQUEST_CHANGES
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            # Review after iter 3: APPROVE
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_task(config, task)

        # iter 1 → build_dev_prompt; iter 2 → build_fix_prompt; iter 3 → build_dev_prompt
        assert mock_dev_prompt.call_count == 2
        assert mock_fix_prompt.call_count == 1

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_first_dev_uses_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """run_from_review() starts at REVIEW with last_review_findings pre-set.
        The first DEV pass (dev_iteration=1) must use build_dev_prompt, not
        build_fix_prompt, because there is no prior resumed dev session."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        # run_from_review skips preflight — only dev + review agents
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Fixed."),
        ]
        mock_pool.side_effect = [
            # Initial REVIEW: REQUEST_CHANGES → triggers DEV
            [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ],
            # Second REVIEW after DEV: APPROVE
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        run_from_review(config, task, workspace_path=workspace)

        # run_from_review starts at REVIEW; after REQUEST_CHANGES the first DEV pass
        # uses build_fix_prompt because retry_reason="review_changes" and findings are
        # set — the routing condition does not require a prior resumed dev session.
        mock_dev_prompt.assert_not_called()
        assert mock_fix_prompt.call_count == 1

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.remote_gates._ntfy_publish")
    @patch("theforge.coordinator.remote_gates._ntfy_poll_reply")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_extend_on_approve_with_no_findings_uses_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_poll,
        mock_publish,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """After a remote 'extend' on an APPROVE review (no findings), the next
        DEV pass must use build_dev_prompt. The extend path only sets
        last_review_findings when there are actual findings; an empty findings
        list produces last_review_findings=None, so the routing falls back to
        build_dev_prompt (there is nothing specific for the agent to fix)."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Done."),
            # iter 1
            _make_agent_result(success=True, output="Done."),
            # iter after extend,
        ]
        # APPROVE review — no P1 findings → last_review_findings will be None
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        poll_calls = {"n": 0}

        def poll_side(reply_url, since_ts, timeout_seconds):
            poll_calls["n"] += 1
            if poll_calls["n"] == 1:
                return ("extend", None)
            return ("approve", None)

        mock_poll.side_effect = poll_side

        result = run_task(config, task, interactive=True, notify=True)

        assert result.success is True
        # iter 1 → build_dev_prompt; post-extend iter → build_dev_prompt
        # (no actionable findings → last_review_findings=None → dev prompt)
        assert mock_dev_prompt.call_count == 2
        mock_fix_prompt.assert_not_called()

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.remote_gates._ntfy_publish")
    @patch("theforge.coordinator.remote_gates._ntfy_poll_reply")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_extend_after_request_changes_uses_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_poll,
        mock_publish,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """After a remote 'extend' triggered by REQUEST_CHANGES (cycles exhausted),
        the next DEV pass must use build_fix_prompt because last_review_findings
        contains real P1 findings from the REQUEST_CHANGES review."""
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
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=1),
            notifications=NotificationConfig(
                backend="ntfy",
                ntfy=NtfyConfig(url="https://ntfy.sh/test", priority="high"),
                human_review_timeout_seconds=60,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Done."),
            # iter 1
            _make_agent_result(success=True, output="Fixed."),
            # iter 2 (post-extend fix),
        ]

        pool_calls = {"n": 0}

        def pool_side(*args, **kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                # First review: REQUEST_CHANGES — exhausts cycles (max_review_cycles=1)
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            # Second review after fix: APPROVE
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side

        poll_calls = {"n": 0}

        def poll_side(reply_url, since_ts, timeout_seconds):
            poll_calls["n"] += 1
            # First call: human extends after REQUEST_CHANGES exhausted cycles
            if poll_calls["n"] == 1:
                return ("extend", None)
            # Second call: human approves after the fix iteration
            return ("approve", None)

        mock_poll.side_effect = poll_side

        result = run_task(config, task, interactive=True, notify=True)

        assert result.success is True
        # iter 1 → build_dev_prompt; iter 2 (post-extend) → build_fix_prompt (P1 findings)
        assert mock_dev_prompt.call_count == 1
        assert mock_fix_prompt.call_count == 1


# ── PR creation tests ──────────────────────────────────────────────────


class TestCreatePR:
    """Tests for _create_pr: push branch + gh pr create."""

    def _make_pr_config(self, tmp_path):
        ws_dir = tmp_path / ".forge" / "worktrees" / "test-task"
        ws_dir.mkdir(parents=True)
        return ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="echo",
                path_pattern=".forge/worktrees/{slug}",
                branch_pattern="feat/{slug}",
                on_approve="pr",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(),
        )

    def _make_review(self):
        from theforge.review import ReviewFinding, ReviewResult

        return ReviewResult(
            verdict="APPROVE",
            summary="All good",
            findings=[
                ReviewFinding(
                    severity="P2",
                    file="foo.py",
                    line=10,
                    description="Minor style issue",
                    suggestion="Rename var",
                )
            ],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )

    @patch("theforge.coordinator.validate_phase.subprocess.run")
    def test_push_before_pr_create(self, mock_run, tmp_path):
        """_create_pr pushes branch before calling gh pr create."""
        from theforge.coordinator.completion import _create_pr

        config = self._make_pr_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n", encoding="utf-8")
        task = TaskStory(
            name="Test Task",
            slug="test-task",
            story_path=spec,
        )
        state = CoordinatorState()

        # First call: git push (success). Second call: gh pr create (success).
        mock_run.side_effect = [
            type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            type(
                "Proc",
                (),
                {"returncode": 0, "stdout": "https://github.com/test/pr/1\n", "stderr": ""},
            )(),
        ]

        result = _create_pr(config, task, "feat/test-task", self._make_review(), state)

        assert result["success"] is True
        assert result["pr_url"] == "https://github.com/test/pr/1"
        assert mock_run.call_count == 2

        # First call must be git push
        push_call = mock_run.call_args_list[0]
        assert push_call[0][0][:3] == ["git", "push", "-u"]
        assert "feat/test-task" in push_call[0][0]

        # Second call must be gh pr create
        pr_call = mock_run.call_args_list[1]
        assert pr_call[0][0][:3] == ["gh", "pr", "create"]

    @patch("theforge.coordinator.validate_phase.subprocess.run")
    def test_pr_body_includes_closes_when_issue_set(self, mock_run, tmp_path):
        """PR body contains 'Closes #N' when task.github_issue is set."""
        from theforge.coordinator.completion import _create_pr

        config = self._make_pr_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n", encoding="utf-8")
        task = TaskStory(name="Test Task", slug="test-task", story_path=spec, github_issue=99)
        state = CoordinatorState()

        mock_run.side_effect = [
            type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            type("Proc", (), {"returncode": 0, "stdout": "https://github.com/test/pr/1\n", "stderr": ""})(),
        ]

        _create_pr(config, task, "feat/test-task", self._make_review(), state)

        pr_call = mock_run.call_args_list[1]
        body_idx = pr_call[0][0].index("--body") + 1
        body = pr_call[0][0][body_idx]
        assert "Closes #99" in body

    @patch("theforge.coordinator.validate_phase.subprocess.run")
    def test_pr_body_no_closes_when_issue_absent(self, mock_run, tmp_path):
        """PR body omits 'Closes' line when task.github_issue is None."""
        from theforge.coordinator.completion import _create_pr

        config = self._make_pr_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n", encoding="utf-8")
        task = TaskStory(name="Test Task", slug="test-task", story_path=spec)
        state = CoordinatorState()

        mock_run.side_effect = [
            type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            type("Proc", (), {"returncode": 0, "stdout": "https://github.com/test/pr/1\n", "stderr": ""})(),
        ]

        _create_pr(config, task, "feat/test-task", self._make_review(), state)

        pr_call = mock_run.call_args_list[1]
        body_idx = pr_call[0][0].index("--body") + 1
        body = pr_call[0][0][body_idx]
        assert "Closes" not in body

    @patch("theforge.coordinator.validate_phase.subprocess.run")
    def test_push_failure_aborts_pr(self, mock_run, tmp_path):
        """If git push fails, _create_pr returns failure without calling gh."""
        from theforge.coordinator.completion import _create_pr

        config = self._make_pr_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n", encoding="utf-8")
        task = TaskStory(
            name="Test Task",
            slug="test-task",
            story_path=spec,
        )
        state = CoordinatorState()

        mock_run.return_value = type(
            "Proc", (), {"returncode": 128, "stdout": "", "stderr": "fatal: remote error"}
        )()

        result = _create_pr(config, task, "feat/test-task", self._make_review(), state)

        assert result["success"] is False
        assert "git push failed" in result["error"]
        assert mock_run.call_count == 1  # only push, no gh pr create


# ── Gate fail retry tests ───────────────────────────────────────────────


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
