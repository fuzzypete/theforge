"""Tests for AC-blocking net-new P1 gate behaviour.

Covers: net_new_pass must not override P1s that violate acceptance criteria.
- Net-new P1 with matches_spec false → blocks
- Net-new P1 with matches_spec true → passes via net_new_pass
- Audit trail records ac_blocking disposition, not net_new
- AC-blocking finding transitions to fixed in a subsequent clean cycle
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_review_profile,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.state import Phase

# ── Review YAML fixtures ─────────────────────────────────────────────────────

_CYCLE1_P1_REQUEST_CHANGES = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Validation missing."
findings:
  - severity: P1
    file: src/changed.py
    line: 10
    description: "Missing validation in handler"
    suggestion: "Add validation"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE2_AC_BLOCKING = """\
```yaml
verdict: REQUEST_CHANGES
summary: "AC violation found."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 5
    description: "Merge strategy validates for all modes breaking on_approve pr behavior"
    suggestion: "Gate validation on mode"
story_compliance:
  matches_spec: false
  mismatches:
    - "on_approve: pr behavior is broken"
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE2_NET_NEW_ONLY = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Latent issue found."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 5
    description: "Merge strategy validates for all modes breaking on_approve pr behavior"
    suggestion: "Gate validation on mode"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""


def _in_process_worktree_eval(
    workspace_path: Path, command: str, payload: dict, timeout: int = 120
) -> dict:
    """Run subprocess eval commands in-process for testing.

    This replaces the real _run_worktree_eval which spawns a subprocess.
    Running in-process allows test patches (e.g. theforge.finding_classifier._get_changed_files)
    to remain effective during the evaluation.
    """
    from theforge.coordinator._subprocess_eval import _COMMANDS

    return _COMMANDS[command](payload)


class TestNetNewAcBlocking:
    """net_new_pass must not override P1s that violate acceptance criteria."""

    def _two_cycle_pool(self, cycle2_review: str):
        """Return a pool side-effect: cycle 1 → P1 REQUEST_CHANGES, cycle 2 → cycle2_review."""
        call_n = {"n": 0}

        def side_effect(**kwargs):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=_CYCLE1_P1_REQUEST_CHANGES, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=cycle2_review, profile_name="review")]

        return side_effect

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_net_new_p1_with_matches_spec_false_blocks(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Net-new P1 from a reviewer who flagged matches_spec=false must block."""
        config = _make_config(tmp_path)  # max_review_cycles=2
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # src/unchanged.py is not in changed files → P1 there gets net_new disposition
        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_AC_BLOCKING)

        result = run_from_review(config, task, workspace)

        # AC-blocking net-new P1 must block: cycle 2 == max_review_cycles → ESCALATE
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.review_cycle == 2

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_ac_blocking_audit_records_disposition(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Audit trail records AC-blocking findings with disposition ac_blocking, not net_new."""
        config = _make_config(tmp_path)  # max_review_cycles=2
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_AC_BLOCKING)

        result = run_from_review(config, task, workspace)

        audit = generate_audit_log(config, task, result)

        # The cycle-2 P1 must appear in finding_registry with disposition ac_blocking
        registry = audit["finding_registry"]
        ac_blocking = [r for r in registry if r["disposition"] == "ac_blocking"]
        assert len(ac_blocking) == 1
        assert "Merge strategy" in ac_blocking[0]["description"]

        # It must NOT appear in non_blocking_p1s (which only lists disposition: net_new)
        non_blocking = audit["non_blocking_p1s"]
        non_blocking_ids = {r["finding_id"] for r in non_blocking}
        assert ac_blocking[0]["finding_id"] not in non_blocking_ids

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_net_new_p1_with_matches_spec_true_does_not_block(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Net-new P1 where matches_spec=true passes via net_new_pass (no regression)."""
        config = _make_config(tmp_path)  # max_review_cycles=2
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # src/unchanged.py is not in changed files → P1 there gets net_new disposition
        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_NET_NEW_ONLY)

        result = run_from_review(config, task, workspace)

        # Net-new P1 with matches_spec=true → net_new_pass → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 2

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_ac_blocking_finding_transitions_to_fixed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """AC-blocking finding that disappears in a later cycle transitions to fixed."""
        # 3 cycles: cycle 1 (P1 blocks) → cycle 2 (AC-blocking net-new) → cycle 3 (APPROVE)
        # Use a $2.00 reviewer budget so 3 × $0.50 cycles don't exceed the limit.
        base = _make_config(tmp_path)
        config = dataclasses.replace(
            base,
            retry=dataclasses.replace(base.retry, max_review_cycles=3),
            review_pool=[_make_review_profile("review", budget_usd=2.0)],
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT

        call_n = {"n": 0}

        def three_cycle_pool(**kwargs):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=_CYCLE1_P1_REQUEST_CHANGES, profile_name="review"
                    )
                ]
            if call_n["n"] == 2:
                return [
                    _make_agent_result(
                        success=True, output=_CYCLE2_AC_BLOCKING, profile_name="review"
                    )
                ]
            # Cycle 3: clean APPROVE — AC-blocking finding is gone
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = three_cycle_pool

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 3

        # The AC-blocking record must be marked fixed, not ac_blocking
        ac_records = [
            r for r in result.state.finding_registry if "Merge strategy" in r.description
        ]
        assert len(ac_records) == 1
        assert ac_records[0].disposition == "fixed"
