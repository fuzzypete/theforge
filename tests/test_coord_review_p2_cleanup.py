"""Tests for post-APPROVE P2 cleanup iterations (issue #621).

Covers the helper decision logic at the review_phase / engine seam and the
end-to-end loop behaviour when an APPROVE leaves open P2 findings.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_from_review
from theforge.coordinator.review_phase import _maybe_enter_p2_cleanup
from theforge.coordinator.state import CoordinatorState, Phase, RetryReason
from theforge.review import ReviewFinding, ReviewResult

APPROVE_WITH_P2 = """\
```yaml
verdict: APPROVE
summary: "Looks good; some polish opportunities."
findings:
  - severity: P2
    file: src/foo.py
    line: 12
    description: "Stale comment references removed helper"
    suggestion: "Update the docstring"
  - severity: P2
    file: src/bar.py
    line: 7
    description: "Minor redaction gap in log line"
    suggestion: "Use redact() helper"
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
ac_verification:
  - criterion: "Implementation satisfies the spec"
    status: VERIFIED
    evidence: "diff hunks present"
```
"""


def _approve_with_p2_review(n: int = 2) -> ReviewResult:
    findings = [
        ReviewFinding(
            severity="P2",
            file=f"src/file{i}.py",
            line=i,
            description=f"P2 issue {i}",
            suggestion=None,
        )
        for i in range(n)
    ]
    return ReviewResult(
        verdict="APPROVE",
        summary="ok",
        findings=findings,
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _approve_clean_review() -> ReviewResult:
    return ReviewResult(
        verdict="APPROVE",
        summary="clean",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


class TestMaybeEnterP2Cleanup:
    """Helper-level seam tests for the post-APPROVE cleanup decision."""

    def test_enters_cleanup_when_p2s_present_and_budget_remains(self, tmp_path):
        config = _make_config(tmp_path)  # max_dev_iterations=2
        state = CoordinatorState()
        state.budget.max_iterations = config.retry.max_dev_iterations
        # cycle_count=0 → remaining=2

        entered = _maybe_enter_p2_cleanup(state, config, _approve_with_p2_review())

        assert entered is True
        assert state.p2_cleanup_active is True
        assert state.retry_reason == RetryReason.P2_CLEANUP
        assert state.p2_cleanup_iterations == 1
        assert len(state.p2_cleanup_findings) == 2
        assert state.last_review_findings is not None
        assert state.p2_cleanup_audit[-1]["action"] == "enter"

    def test_does_not_enter_when_no_p2_findings(self, tmp_path):
        config = _make_config(tmp_path)
        state = CoordinatorState()
        state.budget.max_iterations = config.retry.max_dev_iterations

        entered = _maybe_enter_p2_cleanup(state, config, _approve_clean_review())

        assert entered is False
        assert state.p2_cleanup_active is False
        assert state.retry_reason is None
        assert state.p2_cleanup_audit[-1]["action"] == "skip_no_p2"

    def test_does_not_enter_when_budget_exhausted(self, tmp_path):
        """AC: budget exhaustion with P2s still open exits DONE, not cleanup."""
        config = _make_config(tmp_path)
        state = CoordinatorState()
        state.budget.max_iterations = 2
        # Consume the full per-cycle budget so remaining()==0.
        state.budget.cycle_count = 2

        entered = _maybe_enter_p2_cleanup(state, config, _approve_with_p2_review())

        assert entered is False
        assert state.p2_cleanup_active is False
        assert state.p2_cleanup_audit[-1]["action"] == "skip_budget"

    def test_does_not_enter_when_disabled(self, tmp_path):
        """AC: operator may disable cleanup via config."""
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, p2_cleanup_enabled=False)
        )
        state = CoordinatorState()
        state.budget.max_iterations = 2

        entered = _maybe_enter_p2_cleanup(state, config, _approve_with_p2_review())

        assert entered is False
        assert state.p2_cleanup_active is False
        assert state.p2_cleanup_audit[-1]["action"] == "skip_disabled"

    def test_default_config_keeps_cleanup_active(self, tmp_path):
        """AC: absent explicit configuration, the feature is active by default."""
        config = _make_config(tmp_path)
        assert config.retry.p2_cleanup_enabled is True

    def test_respects_cap_when_set(self, tmp_path):
        """AC: operator may cap the number of cleanup iterations."""
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, p2_cleanup_max_iterations=1)
        )
        state = CoordinatorState()
        state.budget.max_iterations = 4
        # First cleanup pass already dispatched.
        state.p2_cleanup_iterations = 1
        state.p2_cleanup_active = True

        entered = _maybe_enter_p2_cleanup(state, config, _approve_with_p2_review())

        assert entered is False
        assert state.p2_cleanup_active is False
        assert state.p2_cleanup_audit[-1]["action"] == "skip_cap"


class TestP2CleanupLoop:
    """Integration tests for the cleanup loop through run_from_review."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_approve_with_p2s_triggers_cleanup_pass(
        self, mock_shell, mock_dev, mock_pool, tmp_path
    ):
        """AC: APPROVE + open P2s + remaining budget → at least one cleanup dev iteration."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Polished.")

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(success=True, output=APPROVE_WITH_P2, profile_name="review")
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Exactly one cleanup dev iteration ran (clean re-review exits DONE).
        assert result.state.p2_cleanup_iterations == 1
        assert result.state.dev_trace_count == 1
        assert pool_calls["n"] == 2
        # Cleanup deactivated by clean re-review.
        assert result.state.p2_cleanup_active is False
        # Audit captured both transitions.
        actions = [entry["action"] for entry in result.state.p2_cleanup_audit]
        assert "enter" in actions
        assert "exit_clean" in actions

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_cleanup_disabled_short_circuits_to_done(
        self, mock_shell, mock_dev, mock_pool, tmp_path
    ):
        """AC: with p2_cleanup_enabled=false, APPROVE+P2s exits DONE directly."""
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, p2_cleanup_enabled=False)
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="should not run")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_WITH_P2, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        # No cleanup dev iteration dispatched.
        assert result.state.p2_cleanup_iterations == 0
        assert mock_dev.call_count == 0
        # Audit recorded the skip.
        actions = [entry["action"] for entry in result.state.p2_cleanup_audit]
        assert actions == ["skip_disabled"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_cleanup_budget_exhaustion_exits_done_not_escalate(
        self, mock_shell, mock_dev, mock_pool, tmp_path
    ):
        """AC: budget exhaustion with P2s still open exits DONE, not ESCALATE.

        max_dev_iterations=1 + cleanup cap=1: after one cleanup pass that still
        leaves P2s open, the next review pass cannot start another cleanup
        iteration; the run must terminate as DONE.
        """
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config,
            retry=dataclasses.replace(
                config.retry,
                max_dev_iterations=1,
                p2_cleanup_max_iterations=1,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Polished.")

        # Both reviews return APPROVE+P2 (cleanup did not resolve them).
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_WITH_P2, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.p2_cleanup_iterations == 1
        # Audit shows the second pass refused another cleanup iteration.
        actions = [entry["action"] for entry in result.state.p2_cleanup_audit]
        assert "enter" in actions
        # Either cap or budget exhaustion blocked the second pass.
        assert any(a in {"skip_cap", "skip_budget"} for a in actions)
