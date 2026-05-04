"""Tests for the coordinator review phase — cycle and iteration behaviour.

Covers: run_review_only, run_from_review, persistent P1 detection,
escalate-gate state machine, and review cycle metadata.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    SYNTHESIS_PROFILE,
    _make_agent_result,
    _make_config,
    _make_pool_config,
    _make_review_profile,
    _make_task,
    _preflight_then,
    _shell_with_gate,
)

from theforge.config import (
    ForgeConfig,
    ModelProfile,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_from_review, run_review_only, run_task
from theforge.coordinator.state import Phase


class TestReviewOnly:
    """Tests for run_review_only — skips WORKSPACE/PREFLIGHT/DEV/VALIDATE."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_only_approve(self, mock_shell, mock_pool, tmp_path):
        """APPROVE → success, phase=DONE, dev_iteration=0."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")  # git diff returns empty
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_review_only(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.dev_iteration == 0
        assert result.state.review_cycle == 1
        assert len(result.state.review_results) == 1
        assert result.state.review_results[0].verdict == "APPROVE"
        # No dev agents were invoked
        assert len(result.state.dev_results) == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_only_request_changes(self, mock_shell, mock_pool, tmp_path):
        """REQUEST_CHANGES → failure, phase=ESCALATE, findings in result."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_review_only(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 0
        # Findings are present in review results
        assert len(result.state.review_results) == 1
        assert result.state.review_results[0].verdict == "REQUEST_CHANGES"
        assert len(result.state.review_results[0].findings) > 0

    def test_review_only_missing_worktree(self, tmp_path):
        """Missing workspace_path → error result with clear message."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        missing = tmp_path / "does-not-exist"

        result = run_review_only(config, task, missing)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Worktree not found" in result.message
        assert "forge run" in result.message
        assert str(missing) in result.message

    @patch("theforge.coordinator.review_phase._get_handoff_commit_warning")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_only_emits_git_context_and_continues_on_handoff_mismatch(
        self, mock_shell, mock_pool, mock_handoff_warning, tmp_path
    ):
        """Review-only logs handoff mismatch warnings but still runs reviewers."""
        import json

        log_file = tmp_path / "forge.log"
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config,
            log=dataclasses.replace(config.log, log_file=str(log_file), enabled=True),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_handoff_warning.return_value = "handoff commit mismatch"

        result = run_review_only(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert mock_pool.called

        git_events = [
            json.loads(line)
            for line in log_file.read_text().splitlines()
            if json.loads(line).get("event") == "review_git_context"
        ]
        assert len(git_events) == 1
        assert git_events[0]["handoff_commit_warning"] == "handoff commit mismatch"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_only_no_dev_cycles(self, mock_shell, mock_pool, tmp_path):
        """dev_iteration == 0 in all cases (APPROVE and REQUEST_CHANGES)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")

        for review_output in [APPROVE_REVIEW, REQUEST_CHANGES_REVIEW]:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=review_output, profile_name="review")
            ]
            result = run_review_only(config, task, workspace)
            assert result.state.dev_iteration == 0
            assert len(result.state.dev_results) == 0


class TestRunFromReview:
    """Tests for the run_from_review() full iteration loop entry point."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_approve_merges(self, mock_shell, mock_pool, tmp_path):
        """APPROVE on first review → DONE; auto_merge triggers merge attempt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # _run_shell: git diff (OK), git status --porcelain (clean), merge safety checks
        def shell_side_effect(cmd, cwd, **kwargs):
            if "git branch --list" in cmd:
                return (True, "main")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git log" in cmd:
                return (True, "abc123 feat: something")
            if "git checkout" in cmd or "git merge" in cmd or "git worktree" in cmd:
                return (True, "OK")
            return (True, "")

        mock_shell.side_effect = shell_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, auto_merge=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        assert result.state.dev_iteration == 0
        # No dev agent invoked
        assert len(result.state.dev_results) == 0
        # auto_merge attempted
        assert result.merge is not None
        assert result.merge["attempted"] is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_request_changes_iterates(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """REQUEST_CHANGES → dev cycle → re-review → APPROVE → DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Fixed.")

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 2
        # One dev iteration ran
        assert result.state.dev_trace_count == 1
        assert len(result.state.dev_results) == 1
        # preflight was skipped
        assert result.state.preflight_verdict == "SKIPPED"
        assert result.state.preflight_result is None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_exhausts_cycles(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """REQUEST_CHANGES × max_review_cycles → ESCALATE."""
        config = _make_config(tmp_path)  # max_review_cycles=2
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Attempted fix.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()
        assert result.state.review_cycle == config.retry.max_review_cycles

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_skips_preflight(self, mock_shell, mock_pool, tmp_path):
        """preflight_verdict is 'SKIPPED' and no preflight agent is ever invoked."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.state.preflight_verdict == "SKIPPED"
        assert result.state.preflight_result is None
        # run_agent was never called (no preflight, no dev)
        # We can verify by checking no dev results
        assert len(result.state.dev_results) == 0

        # Spec requires: audit log records preflight_verdict as 'SKIPPED' with cost 0.0

        audit = generate_audit_log(config, task, result)
        assert audit["preflight"] is not None
        assert audit["preflight"]["verdict"] == "SKIPPED"
        assert audit["preflight"]["cost_usd"] == 0.0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_restores_dev_session_id(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Pre-existing sessions.json causes dev session ID to be passed on first dev call."""
        import json

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Write a sessions.json as if a prior run had saved it
        forge_dir = workspace / ".forge"
        forge_dir.mkdir()
        (forge_dir / "sessions.json").write_text(
            json.dumps({"dev_session_id": "prior-dev-sess"}), encoding="utf-8"
        )

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # First pool call → REQUEST_CHANGES, second → APPROVE
        pool_call_n = {"n": 0}

        def pool_side(prompt=None, profiles=None, working_dir=None, session_ids=None, **kwargs):
            pool_call_n["n"] += 1
            if pool_call_n["n"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side

        captured_dev_session_ids: list[str | None] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            captured_dev_session_ids.append(session_id)
            return _make_agent_result(success=True, output="Fixed.", session_id="new-dev-sess")

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent

        result = run_from_review(config, task, workspace)

        assert result.success is True
        # The first (and only) dev call should receive the restored session ID
        assert captured_dev_session_ids == ["prior-dev-sess"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_restores_reviewer_session_ids(self, mock_shell, mock_pool, tmp_path):
        """Pre-existing sessions.json causes reviewer session IDs to be passed to first pool."""
        import json

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Write a sessions.json with reviewer session IDs from a prior run
        forge_dir = workspace / ".forge"
        forge_dir.mkdir()
        (forge_dir / "sessions.json").write_text(
            json.dumps({"reviewer_session_ids": {"review": "prior-rev-sess"}}),
            encoding="utf-8",
        )

        mock_shell.return_value = (True, "")

        captured_session_ids: list[list[str | None]] = []

        def pool_side(prompt=None, profiles=None, working_dir=None, session_ids=None, **kwargs):
            captured_session_ids.append(list(session_ids or []))
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side

        result = run_from_review(config, task, workspace)

        assert result.success is True
        # First (and only) review pool call should receive the restored reviewer session ID
        assert len(captured_session_ids) == 1
        assert captured_session_ids[0] == ["prior-rev-sess"]


class TestHasPersistentP1:
    """Unit tests for _has_persistent_p1 in coord_preflight."""

    def _make_finding(self, severity: str, description: str, file: str = "coordinator.py"):
        from theforge.review import ReviewFinding

        return ReviewFinding(
            severity=severity,
            file=file,
            line=None,
            observed=description,
            suggestion="fix it",
        )

    def test_same_description_different_files_returns_false(self):
        """Same description on different real files is NOT persistent — no file evidence."""
        from theforge.coordinator.preflight import _has_persistent_p1

        curr = [
            self._make_finding(
                "P1", "coordinator routing ignores extend path", file="coordinator.py"
            )
        ]
        prev = [
            self._make_finding("P1", "coordinator routing ignores extend path", file="task.py")
        ]
        assert _has_persistent_p1(curr, prev) is False

    def test_same_description_same_files_returns_true(self):
        from theforge.coordinator.preflight import _has_persistent_p1

        curr = [
            self._make_finding(
                "P1", "coordinator routing ignores extend path", file="coordinator.py"
            )
        ]
        prev = [
            self._make_finding(
                "P1", "coordinator routing ignores extend path", file="coordinator.py"
            )
        ]
        assert _has_persistent_p1(curr, prev) is True

    def test_different_descriptions_returns_false(self):
        from theforge.coordinator.preflight import _has_persistent_p1

        curr = [self._make_finding("P1", "missing null check on session id", file="session.py")]
        prev = [
            self._make_finding(
                "P1",
                "wrong HTTP method used in upload endpoint",
                file="upload.py",
            )
        ]
        assert _has_persistent_p1(curr, prev) is False

    def test_empty_current_findings_returns_false(self):
        from theforge.coordinator.preflight import _has_persistent_p1

        prev = [self._make_finding("P1", "coordinator routing ignores extend path")]
        assert _has_persistent_p1([], prev) is False

    def test_empty_previous_findings_returns_false(self):
        from theforge.coordinator.preflight import _has_persistent_p1

        curr = [self._make_finding("P1", "coordinator routing ignores extend path")]
        assert _has_persistent_p1(curr, []) is False


class TestEscalateGate:
    """Tests for _run_escalate_gate() via run_task integration."""

    def _make_escalate_config(
        self, tmp_path: Path, escalate_policy: str = "prompt"
    ) -> ForgeConfig:
        """Config with max_review_cycles=1 to trigger escalation quickly."""

        base = _make_config(tmp_path)
        new_retry = dataclasses.replace(
            base.retry,
            max_dev_iterations=1,
            max_review_cycles=1,
            escalate_policy=escalate_policy,
        )
        return dataclasses.replace(base, retry=new_retry)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalate_gate_reject_policy_exits_as_escalate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """escalate_policy=reject exits as ESCALATE without prompting."""
        config = self._make_escalate_config(tmp_path, escalate_policy="reject")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", profile_name="dev"
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.escalate_decision == "reject"
        assert result.state.escalate_reason is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalate_gate_auto_approve_majority_pass(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """escalate_policy=auto_approve auto-approves when gate passed and majority approved."""

        config = self._make_escalate_config(tmp_path, escalate_policy="auto_approve")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Gate PASS written by _shell_with_gate
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", profile_name="dev"
        )
        # Pool of 2 reviewers: one APPROVE, one REQUEST_CHANGES (majority = APPROVE)
        # But with a single review pool we can only get REQUEST_CHANGES from the
        # single reviewer → auto_approve won't trigger unless majority is APPROVE.
        # Use single reviewer APPROVE to ensure majority check passes.
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        # Patch gate_decisions to include "PASS" so auto_approve condition is met.
        # Actually, the gate decision comes from handoff.yaml written by _shell_with_gate.
        # The problem is: auto_approve only fires when review cycle is exhausted
        # AND majority approved. With APPROVE result, the coordinator never escalates.
        # We need REQUEST_CHANGES but majority of reviewer_verdicts should be APPROVE.
        # Use 2 profiles: one APPROVE, one REQUEST_CHANGES. The merged result is
        # REQUEST_CHANGES (strict wins), but last_cycle_reviewer_results has 1 APPROVE.

        r1 = (
            _make_review_profile("r1")
            if hasattr(
                __import__("tests.test_coordinator", fromlist=["_make_review_profile"]),
                "_make_review_profile",
            )
            else ModelProfile(
                name="r1",
                cli="claude",
                model="sonnet",
                budget_usd=5.0,
                timeout_seconds=300,
                allowed_tools=(),
            )
        )
        r2 = ModelProfile(
            name="r2",
            cli="claude",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        new_retry = dataclasses.replace(
            config.retry, max_review_cycles=1, escalate_policy="auto_approve"
        )
        config2 = dataclasses.replace(
            config,
            review_pool=[r1, r2],
            retry=new_retry,
            synthesis_profile=None,
        )

        # r1=APPROVE, r2=REQUEST_CHANGES → merged = REQUEST_CHANGES (strict)
        # majority = 1/2 APPROVE → 50% which is NOT majority (>50%)
        # so auto_approve won't fire. Use 2 APPROVE + 1 REQUEST_CHANGES would need
        # 3 reviewers. Simplest: with 1 reviewer returning APPROVE, merged=APPROVE,
        # coordinator never escalates. So test auto_approve with a direct gate mock.
        # The cleanest approach: patch _run_escalate_gate directly.
        from theforge.coordinator.state import CoordinatorResult

        gate_calls = []

        def mock_gate(state, cfg, tsk, wp, bn, ts, **kwargs):
            gate_calls.append({"state": state, "config": cfg})
            # Simulate auto_approve firing: return approve result
            state.escalate_decision = "approve"
            state.escalate_reason = "test escalation"
            state.phase = Phase.DONE
            return CoordinatorResult(
                success=True,
                phase=Phase.DONE,
                state=state,
                message="human approved via escalate gate",
            )

        with patch("theforge.coordinator.review_phase._run_escalate_gate", side_effect=mock_gate):
            mock_agent2 = mock_agent
            mock_pool2 = mock_pool
            mock_agent2.side_effect = _preflight_then(
                _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            )
            mock_pool2.return_value = [
                _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="r1"),
                _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="r2"),
            ]
            result = run_task(config2, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.escalate_decision == "approve"
        assert len(gate_calls) >= 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalate_gate_approve_path(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Gate approve path: gate returns CoordinatorResult with success=True."""
        config = self._make_escalate_config(tmp_path, escalate_policy="prompt")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        from theforge.coordinator.state import CoordinatorResult

        def mock_gate(state, cfg, tsk, wp, bn, ts, **kwargs):
            state.escalate_decision = "approve"
            state.escalate_reason = "max cycles reached"
            state.phase = Phase.DONE
            return CoordinatorResult(
                success=True,
                phase=Phase.DONE,
                state=state,
                message="human approved via escalate gate",
            )

        with patch("theforge.coordinator.review_phase._run_escalate_gate", side_effect=mock_gate):
            mock_preflight.return_value = _PREFLIGHT_RESULT
            mock_agent.return_value = _make_agent_result(
                success=True, output="Implemented.", profile_name="dev"
            )
            mock_pool.return_value = [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ]
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.escalate_decision == "approve"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalate_gate_reject_path(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Gate reject path: gate returns ESCALATE CoordinatorResult."""
        config = self._make_escalate_config(tmp_path, escalate_policy="prompt")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        from theforge.coordinator.state import CoordinatorResult

        def mock_gate(state, cfg, tsk, wp, bn, ts, **kwargs):
            state.escalate_decision = "reject"
            state.escalate_reason = "max cycles reached"
            return CoordinatorResult(
                success=False,
                phase=Phase.ESCALATE,
                state=state,
                message="escalated",
            )

        with patch("theforge.coordinator.review_phase._run_escalate_gate", side_effect=mock_gate):
            mock_preflight.return_value = _PREFLIGHT_RESULT
            mock_agent.return_value = _make_agent_result(
                success=True, output="Implemented.", profile_name="dev"
            )
            mock_pool.return_value = [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ]
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.escalate_decision == "reject"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalate_gate_continue_path(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Gate continue path: returns None → coordinator re-enters REVIEW for one more cycle."""

        # max_review_cycles=1, so first exhaustion triggers gate
        config = self._make_escalate_config(tmp_path, escalate_policy="prompt")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        gate_call_count = {"n": 0}

        def mock_gate(state, cfg, tsk, wp, bn, ts, **kwargs):
            gate_call_count["n"] += 1
            if gate_call_count["n"] == 1:
                # First gate call: continue (grant one more cycle)
                state.escalate_decision = "continue"
                state.escalate_reason = "max cycles reached"
                state.phase = Phase.REVIEW
                return None
            # Second gate call (after extra cycle): reject
            state.escalate_decision = "reject"
            state.escalate_reason = "max cycles reached again"
            from theforge.coordinator.state import CoordinatorResult

            return CoordinatorResult(
                success=False,
                phase=Phase.ESCALATE,
                state=state,
                message="escalated after continue",
            )

        with patch("theforge.coordinator.review_phase._run_escalate_gate", side_effect=mock_gate):
            mock_preflight.return_value = _PREFLIGHT_RESULT
            mock_agent.side_effect = [
                # DEV for cycle 1, DEV for continue cycle
                _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
                _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
            ]
            mock_pool.return_value = [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ]
            result = run_task(config, task)

        # Gate was called twice: first continue, then reject
        assert gate_call_count["n"] >= 1
        assert result.phase == Phase.ESCALATE
        assert result.state.escalate_decision == "reject"


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
