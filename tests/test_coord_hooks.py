"""Unit tests for coord_hooks.run_hook() and payload builders."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.coord_hooks import (
    build_post_merge_payload,
    build_post_run_payload,
    build_post_sprint_payload,
    build_pre_run_payload,
    run_hook,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_config(project="test-project", base_branch="main", post_run=None, post_merge=None):
    from theforge.config import (
        DEFAULT_DEV_PROFILE,
        DEFAULT_PREFLIGHT_PROFILE,
        DEFAULT_REVIEW_PROFILE,
        ForgeConfig,
        HooksConfig,
        LogConfig,
        NotificationConfig,
        PlanAgentReviewConfig,
        PlanConfig,
        PlanReviewConfig,
        RetryPolicy,
        ValidationConfig,
        WorkspaceConfig,
    )

    hooks = None
    if post_run or post_merge:
        hooks = HooksConfig(post_run=post_run, post_merge=post_merge)

    return ForgeConfig(
        project=project,
        project_root=Path("/tmp/test"),
        workspace=WorkspaceConfig(
            create_command="git worktree add {slug}",
            path_pattern=".forge/worktrees/{slug}",
            branch_pattern="feat/{slug}",
            base_branch=base_branch,
        ),
        validation=ValidationConfig(
            gate_command="make gate",
            handoff_file="handoff.yaml",
            gate_decision_key="gate_decision",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
        notifications=NotificationConfig(),
        plan=PlanConfig(),
        plan_review=PlanReviewConfig(),
        plan_agent_review=PlanAgentReviewConfig(),
        log=LogConfig(enabled=False),
        hooks=hooks,
    )


def _make_task(slug="my-feature", spec_path=None):
    from theforge.task import TaskSpec

    return TaskSpec(
        name="My Feature",
        spec_path=spec_path or Path("/tmp/specs/my-feature.md"),
        slug=slug,
        file_scope=[],
    )


def _make_state(review_results=None, gate_decisions=None):
    from theforge.coord_state import CoordinatorState

    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.branch_name = "feat/my-feature"
    if review_results is not None:
        state.review_results = review_results
    if gate_decisions is not None:
        state.gate_decisions = gate_decisions
    return state


def _make_result(success=True, state=None):
    from theforge.coord_state import CoordinatorResult, Phase

    if state is None:
        state = _make_state()
    return CoordinatorResult(
        success=success,
        phase=Phase.DONE if success else Phase.ESCALATE,
        state=state,
        message="ok" if success else "escalated",
    )


# ── run_hook tests ───────────────────────────────────────────────────


class TestRunHook:
    def test_success_path(self):
        """Hook exits 0 → HookResult with success=True."""
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "ok\n"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = run_hook("echo hi", {"a": 1}, timeout=30, label="test")

        assert result.success is True
        assert result.exit_code == 0
        assert "ok" in result.output
        assert result.duration_s >= 0.0
        mock_run.assert_called_once()

    def test_nonzero_exit_does_not_raise(self):
        """Non-zero exit returns failure result but does not raise."""
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "error\n"
        with patch("subprocess.run", return_value=proc):
            result = run_hook("false", {}, timeout=30, label="test")

        assert result.success is False
        assert result.exit_code == 1

    def test_timeout_returns_failure(self):
        """TimeoutExpired → HookResult with success=False, no raise."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            result = run_hook("slow-script.sh", {}, timeout=30, label="test")

        assert result.success is False
        assert result.exit_code == -1

    def test_exception_returns_failure(self):
        """Arbitrary exception → HookResult with success=False, no raise."""
        with patch("subprocess.run", side_effect=OSError("oops")):
            result = run_hook("broken", {}, timeout=30, label="test")

        assert result.success is False
        assert result.exit_code == -1

    def test_malformed_command_skipped(self):
        """Malformed command (unmatched quote) → silently skipped (success=True)."""
        with patch("subprocess.run") as mock_run:
            result = run_hook("script.sh 'unmatched", {}, timeout=30, label="test")

        assert result.success is True
        assert result.exit_code == 0
        mock_run.assert_not_called()

    def test_missing_file_skipped(self, tmp_path):
        """Path that doesn't exist → silently skipped (success=True, exit_code=0)."""
        missing = str(tmp_path / "no-such-hook.sh")
        with patch("subprocess.run") as mock_run:
            result = run_hook(missing, {}, timeout=30, label="test")

        assert result.success is True
        assert result.exit_code == 0
        mock_run.assert_not_called()

    def test_non_executable_file_skipped(self, tmp_path):
        """File exists but not executable → silently skipped."""
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\necho hi\n")
        # Do NOT chmod +x
        with patch("subprocess.run") as mock_run:
            result = run_hook(str(script), {}, timeout=30, label="test")

        assert result.success is True
        assert result.exit_code == 0
        mock_run.assert_not_called()

    def test_system_command_not_checked(self):
        """Commands without path sep bypass existence check (e.g. 'python script.py')."""
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "done"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = run_hook("python --version", {}, timeout=30, label="test")

        assert result.success is True
        mock_run.assert_called_once()

    def test_structured_log_event_emitted(self):
        """Structured log event emitted via logger on success."""
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        logger = MagicMock()
        with patch("subprocess.run", return_value=proc):
            run_hook("echo hi", {}, timeout=30, label="post_run", logger=logger)

        logger._safe_emit.assert_called_once()
        call_kwargs = logger._safe_emit.call_args
        assert call_kwargs[0][0] == "hook"
        assert call_kwargs[1]["hook"] == "post_run"
        assert call_kwargs[1]["success"] is True

    def test_structured_log_event_emitted_on_failure(self):
        """Structured log event emitted even on non-zero exit."""
        proc = MagicMock()
        proc.returncode = 2
        proc.stdout = ""
        proc.stderr = "fail"
        logger = MagicMock()
        with patch("subprocess.run", return_value=proc):
            run_hook("bad-hook.sh", {}, timeout=30, label="post_run", logger=logger)

        logger._safe_emit.assert_called_once()
        call_kwargs = logger._safe_emit.call_args
        assert call_kwargs[1]["success"] is False

    def test_payload_serialized_to_stdin(self):
        """Payload dict is JSON-encoded and passed as stdin input."""
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        payload = {"event": "post_run", "slug": "test"}
        with patch("subprocess.run", return_value=proc) as mock_run:
            run_hook("my-hook", payload, timeout=30, label="test")

        call_kwargs = mock_run.call_args
        import json

        assert json.loads(call_kwargs[1]["input"]) == payload


# ── Payload builder tests ─────────────────────────────────────────────


class TestBuildPreRunPayload:
    def test_contains_required_fields(self):
        config = _make_config()
        task = _make_task()
        payload = build_pre_run_payload(task, "run123", config)

        assert payload["event"] == "pre_run"
        assert payload["project"] == "test-project"
        assert payload["slug"] == "my-feature"
        assert payload["run_id"] == "run123"
        assert "spec" in payload


class TestBuildPostRunPayload:
    def test_contains_required_fields(self):
        config = _make_config()
        task = _make_task()
        state = _make_state()
        result = _make_result(state=state)
        payload = build_post_run_payload(state, config, task, result, "run456", 120.5)

        assert payload["event"] == "post_run"
        assert payload["project"] == "test-project"
        assert payload["slug"] == "my-feature"
        assert payload["run_id"] == "run456"
        assert payload["duration_seconds"] == 120.5
        assert payload["outcome"] == "done"

    def test_escalate_outcome(self):
        config = _make_config()
        task = _make_task()
        state = _make_state()
        result = _make_result(success=False, state=state)
        payload = build_post_run_payload(state, config, task, result, "run789", 60.0)

        assert payload["outcome"] == "escalate"

    def test_findings_from_review_results(self):
        from theforge.review import ReviewFinding, ReviewResult

        config = _make_config()
        task = _make_task()
        review = ReviewResult(
            verdict="APPROVE",
            summary="Looks good",
            findings=[
                ReviewFinding(
                    severity="P2",
                    file="src/foo.py",
                    line=10,
                    description="minor issue",
                    suggestion="fix it",
                )
            ],
            spec_matches=True,
            spec_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        state = _make_state(review_results=[review])
        result = _make_result(state=state)
        payload = build_post_run_payload(state, config, task, result, "r1", 10.0)

        assert payload["verdict"] == "APPROVE"
        assert payload["summary"] == "Looks good"
        assert len(payload["findings"]) == 1
        assert payload["findings"][0]["severity"] == "P2"


class TestBuildPostMergePayload:
    def test_contains_required_fields(self):
        config = _make_config()
        payload = build_post_merge_payload("my-slug", "feat/my-slug", "run-xyz", config)

        assert payload["event"] == "post_merge"
        assert payload["project"] == "test-project"
        assert payload["slug"] == "my-slug"
        assert payload["branch"] == "feat/my-slug"
        assert payload["merged_to"] == "main"
        assert payload["run_id"] == "run-xyz"


class TestBuildPostSprintPayload:
    def test_contains_required_fields(self):
        config = _make_config()
        stories = [{"slug": "a", "outcome": "done", "verdict": "APPROVE", "merged": True}]
        payload = build_post_sprint_payload(
            "sprint-1", stories, "sprint-run-1", config, 25.0, 3600.0
        )

        assert payload["event"] == "post_sprint"
        assert payload["project"] == "test-project"
        assert payload["sprint"] == "sprint-1"
        assert payload["run_id"] == "sprint-run-1"
        assert payload["total_cost_usd"] == 25.0
        assert payload["duration_seconds"] == 3600.0
        assert payload["stories"] == stories
