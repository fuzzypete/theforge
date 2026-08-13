"""Tests for post-APPROVE P2 cleanup iterations (issue #621).

Covers the helper decision logic at the review_phase / engine seam and the
end-to-end loop behaviour when an APPROVE leaves open P2 findings.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import load_config
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.review_phase import _maybe_enter_p2_cleanup
from theforge.coordinator.state import CoordinatorState, Phase, RetryReason
from theforge.coordinator.validate_phase import _ValidateOutcome, _blocking_finding_route
from theforge.review import ReviewFinding, ReviewResult

APPROVE_WITH_P2 = """\
```yaml
verdict: APPROVE
summary: "Looks good; some polish opportunities."
findings:
  - severity: P2
    file: src/foo.py
    line: 12
    observed: "Stale comment references removed helper"
    expected: "Comments must track the code they describe"
    evidence: "src/foo.py:12 docstring names a deleted helper"
    suggestion: "Update the docstring"
  - severity: P2
    file: src/bar.py
    line: 7
    observed: "Minor redaction gap in log line"
    expected: "Log lines must pass through the redaction pass"
    evidence: "src/bar.py:7 logs an unredacted value"
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
            observed=f"P2 issue {i}",
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
        state.budget.max_iterations = 3
        state.budget.cycle_count = 1
        # remaining=2: one iteration for cleanup, one reserved for repair

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

    def test_does_not_enter_when_only_repair_reserve_remains(self, tmp_path):
        """Cleanup must not spend the final iteration reserved for repair."""
        config = _make_config(tmp_path)
        state = CoordinatorState()
        state.budget.max_iterations = 2
        state.budget.cycle_count = 1
        # remaining()==1: enough for one more dev call, but not for cleanup plus repair.

        entered = _maybe_enter_p2_cleanup(state, config, _approve_with_p2_review())

        assert entered is False
        assert state.p2_cleanup_active is False
        assert state.p2_cleanup_audit[-1]["action"] == "skip_budget_reserve"

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

    def test_new_p2_after_carry_does_not_extend_loop(self, tmp_path):
        """Loop is bound to the carried P2 set; new P2s do not extend it.

        Reviewer no longer raises the original carried P2s, but raises a
        brand-new one. The cleanup loop must exit (action=exit_clean)
        rather than follow the new advisory finding.
        """
        config = _make_config(tmp_path)
        state = CoordinatorState()
        state.budget.max_iterations = 4
        state.p2_cleanup_iterations = 1
        state.p2_cleanup_active = True
        # Carry set captured at first entry — none of these descriptions
        # appear in the next review.
        state.p2_cleanup_carry_keys = [
            ["src/old.py", 1, "Original P2 A"],
            ["src/old.py", 2, "Original P2 B"],
        ]

        # The new review surfaces an entirely different P2 (new finding).
        new_review = ReviewResult(
            verdict="APPROVE",
            summary="ok",
            findings=[
                ReviewFinding(
                    severity="P2",
                    file="src/new.py",
                    line=99,
                    observed="Brand new advisory",
                    suggestion=None,
                )
            ],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )

        entered = _maybe_enter_p2_cleanup(state, config, new_review)

        assert entered is False
        assert state.p2_cleanup_active is False
        assert state.p2_cleanup_audit[-1]["action"] == "exit_clean"

    def test_carry_keys_capture_only_initial_p2_set(self, tmp_path):
        """First entry captures the carry set; subsequent passes do not extend it."""
        config = _make_config(tmp_path)
        state = CoordinatorState()
        state.budget.max_iterations = 4

        first = _approve_with_p2_review(n=2)
        _maybe_enter_p2_cleanup(state, config, first)
        captured = list(state.p2_cleanup_carry_keys)
        assert len(captured) == 2

        # Subsequent pass surfaces the original set PLUS a new P2;
        # carry keys must not grow.
        second = ReviewResult(
            verdict="APPROVE",
            summary="ok",
            findings=[
                *first.findings,
                ReviewFinding(
                    severity="P2",
                    file="src/extra.py",
                    line=42,
                    observed="Late-breaking advisory",
                    suggestion=None,
                ),
            ],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        _maybe_enter_p2_cleanup(state, config, second)
        assert list(state.p2_cleanup_carry_keys) == captured

    def test_respects_cap_when_set(self, tmp_path):
        """AC: operator may cap the number of cleanup iterations."""
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, p2_cleanup_max_iterations=1)
        )
        state = CoordinatorState()
        state.budget.max_iterations = 4
        # First cleanup pass already dispatched with the same P2 set still
        # outstanding (carry_keys mirror the review we're about to evaluate).
        state.p2_cleanup_iterations = 1
        state.p2_cleanup_active = True
        state.p2_cleanup_carry_keys = [
            ["src/file0.py", 0, "P2 issue 0"],
            ["src/file1.py", 1, "P2 issue 1"],
        ]

        entered = _maybe_enter_p2_cleanup(state, config, _approve_with_p2_review())

        assert entered is False
        assert state.p2_cleanup_active is False
        assert state.p2_cleanup_audit[-1]["action"] == "skip_cap"


class TestP2CleanupConfigLoading:
    """forge.yaml retry.p2_cleanup_* keys must reach RetryPolicy via load_config."""

    def _write_yaml(self, tmp_path, retry_data: dict):
        config_path = tmp_path / "forge.yaml"
        config_path.write_text(yaml.dump({"retry": retry_data}), encoding="utf-8")
        return config_path

    def test_defaults_when_keys_absent(self, tmp_path):
        config_path = self._write_yaml(tmp_path, {})
        with patch("importlib.import_module"):
            cfg = load_config(config_path)
        assert cfg.retry.p2_cleanup_enabled is True
        assert cfg.retry.p2_cleanup_max_iterations == 0

    def test_yaml_disables_cleanup(self, tmp_path):
        config_path = self._write_yaml(tmp_path, {"p2_cleanup_enabled": False})
        with patch("importlib.import_module"):
            cfg = load_config(config_path)
        assert cfg.retry.p2_cleanup_enabled is False

    def test_yaml_caps_cleanup_iterations(self, tmp_path):
        config_path = self._write_yaml(tmp_path, {"p2_cleanup_max_iterations": 3})
        with patch("importlib.import_module"):
            cfg = load_config(config_path)
        assert cfg.retry.p2_cleanup_max_iterations == 3


class TestValidateCleanupRouting:
    """Validate-phase seam tests for cleanup reserve and repair routing."""

    def test_cleanup_breakage_uses_reserved_repair_iteration_when_budget_remains(
        self, tmp_path
    ):
        config = _make_config(tmp_path)
        state = CoordinatorState()
        state.p2_cleanup_active = True
        state.budget.max_iterations = 3
        state.budget.cycle_count = 2
        # remaining()==1: cleanup is active, but one in-cycle repair attempt remains.

        route = _blocking_finding_route(state, config)

        assert route.outcome == _ValidateOutcome.RETRY_DEV
        assert route.reason == "dev_budget_remains"

    def test_cleanup_breakage_escalates_when_reserved_repair_iteration_is_spent(
        self, tmp_path
    ):
        config = _make_config(tmp_path)
        state = CoordinatorState()
        state.p2_cleanup_active = True
        state.budget.max_iterations = 3
        state.budget.cycle_count = 3
        # remaining()==0: cleanup may not buy a new cycle once the reserved repair is gone.

        route = _blocking_finding_route(state, config)

        assert route.outcome == _ValidateOutcome.ESCALATE
        assert route.reason == "p2_cleanup"


class TestP2CleanupLoop:
    """Integration tests for the cleanup loop through run_from_review."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
    @patch_gate_shell()
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

    @patch("theforge.coordinator.review_phase._human_review")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_interactive_mode_runs_cleanup_before_hitl(
        self, mock_shell, mock_dev, mock_pool, mock_human_review, tmp_path
    ):
        """Interactive APPROVE with open P2s + remaining budget runs cleanup,
        not the HITL prompt (the human is asked only when cleanup terminates).
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Polished.")

        # Reviewer flips clean after the cleanup dev pass.
        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(success=True, output=APPROVE_WITH_P2, profile_name="review")
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        # When cleanup completes cleanly the interactive flow surfaces to the
        # operator on the *second* review; HITL must not run before cleanup.
        mock_human_review.return_value = ("approve", None)

        result = run_from_review(config, task, workspace, interactive=True)

        assert result.success is True
        assert result.state.p2_cleanup_iterations == 1
        # Exactly one HITL call — and it followed the cleanup dev pass.
        assert mock_human_review.call_count == 1
        # First REVIEW (with P2s) must NOT have called HITL — verified
        # implicitly by cleanup dispatching a dev iteration before HITL ran.
        assert mock_dev.call_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_cleanup_reserve_decline_exits_done_not_escalate(
        self, mock_shell, mock_dev, mock_pool, tmp_path
    ):
        """AC: reserve protection declines cleanup and exits DONE, not ESCALATE."""
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config,
            retry=dataclasses.replace(
                config.retry,
                max_dev_iterations=1,
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
        assert result.state.p2_cleanup_iterations == 0
        assert mock_dev.call_count == 0
        actions = [entry["action"] for entry in result.state.p2_cleanup_audit]
        assert actions == ["skip_budget_reserve"]

    @patch("theforge.coordinator.engine._run_validate_phase")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_cleanup_breakage_uses_reserved_repair_iteration_before_rereview(
        self, mock_shell, mock_dev, mock_pool, mock_validate, tmp_path
    ):
        """A cleanup regression spends the reserved repair attempt instead of escalating."""
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config,
            retry=dataclasses.replace(
                config.retry,
                max_dev_iterations=3,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Polished.")
        mock_validate.side_effect = [
            (_ValidateOutcome.RETRY_DEV, None),
            (_ValidateOutcome.PASS, None),
        ]

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
        assert result.state.p2_cleanup_iterations == 1
        assert result.state.p2_cleanup_active is False
        assert mock_dev.call_count == 2
        assert mock_validate.call_count == 2
        assert pool_calls["n"] == 2
        actions = [entry["action"] for entry in result.state.p2_cleanup_audit]
        assert actions == ["enter", "exit_clean"]
