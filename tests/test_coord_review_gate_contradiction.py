"""Tests for gate-contradiction P1 downgrade behaviour.

Covers: P1 findings claiming test/build/lint failures are non-blocking when the
most recent gate decision was PASS.

- Gate-verifiable P1 on gate PASS → disposition gate_contradicted → does not block
- Subjective P1 on gate PASS → NOT downgraded → still blocks
- No downgrade when gate decision is absent (cycle 1 via run_from_review)
- Multiple gate-verifiable patterns (test failure, build error, lint error)
- Audit trail records gate_contradicted at original P1 severity
- Fallback (synthetic P1 injection) is unaffected when all P1s are gate_contradicted
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.state import Phase

# ── Review YAML fixtures ─────────────────────────────────────────────────────

_CYCLE1_P1_CHANGED_FILE = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Bug in handler."
findings:
  - severity: P1
    file: src/changed.py
    line: 10
    observed: "Missing null check in handler"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Add null guard"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE2_TEST_FAILURE_P1 = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Tests are failing."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 5
    observed: "Test failure: 435 tests fail with document is not defined"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix jsdom setup"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE2_BUILD_ERROR_P1 = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Build is broken."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 5
    observed: "Build error in compilation step prevents deployment"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix import"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE2_LINT_FAIL_P1 = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Lint fails."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 5
    observed: "Lint fail: unused import in module"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Remove import"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE2_SUBJECTIVE_P1 = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Design issue found."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 5
    observed: "Missing validation in the data processing pipeline"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Add validation guard"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE1_TEST_FAILURE_P1 = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Tests are failing."
findings:
  - severity: P1
    file: src/changed.py
    line: 10
    observed: "Test failure: import error prevents test collection"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix the import"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

# Observed trail (story #1425): the finding mentions "test suite" — a substring the
# old keyword list would downgrade on — but asserts that acceptance evidence was
# never produced (inadequate coverage), which a green gate cannot contradict.
# The reviewer also returned matches_spec=false.
_CYCLE2_INADEQUATE_ACCEPTANCE_AC_FALSE = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Acceptance evidence missing."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 438
    observed: "test suite only exercises mocked diagnose output; no run shown within budget"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Demonstrate a live diagnose run"
story_compliance:
  matches_spec: false
  mismatches:
    - "AC3: live sparse-body diagnose run never demonstrated"
test_coverage:
  adequate: false
  gaps:
    - "no live diagnose run exercised"
```
"""

# Same inadequate-coverage finding, but the reviewer returned matches_spec=true.
# The old keyword list ("test suite") would still have downgraded this to
# gate_contradicted → net_new_pass → DONE. Assertion-based classification must
# refuse the downgrade so it keeps blocking on its own merits.
_CYCLE2_INADEQUATE_ACCEPTANCE_AC_TRUE = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Acceptance evidence missing."
findings:
  - severity: P1
    file: src/unchanged.py
    line: 438
    observed: "test suite only exercises mocked diagnose output; no run shown within budget"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Demonstrate a live diagnose run"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: false
  gaps:
    - "no live diagnose run exercised"
```
"""


def _in_process_worktree_eval(
    workspace_path: Path, command: str, payload: dict, timeout: int = 120
) -> dict:
    """Run subprocess eval commands in-process for testing."""
    from theforge.coordinator._subprocess_eval import _COMMANDS

    return _COMMANDS[command](payload)


class TestGateContradiction:
    """Gate-verifiable P1 findings must not block when the gate already passed."""

    def _two_cycle_pool(self, cycle2_review: str):
        """Cycle 1 → blocking P1, cycle 2 → cycle2_review."""
        call_n = {"n": 0}

        def side_effect(**kwargs):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=_CYCLE1_P1_CHANGED_FILE,
                        profile_name="review",
                    )
                ]
            return [_make_agent_result(success=True, output=cycle2_review, profile_name="review")]

        return side_effect

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_test_failure_p1_does_not_block_on_gate_pass(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """P1 claiming test failure is non-blocking when the gate already passed."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # src/unchanged.py is not in changed files → P1 gets net_new from classifier
        # Gate contradiction then downgrades it to gate_contradicted
        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_TEST_FAILURE_P1)

        result = run_from_review(config, task, workspace)

        # gate_contradicted P1 must not block: cycle 2 passes despite REQUEST_CHANGES
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 2

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_build_error_p1_does_not_block_on_gate_pass(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """P1 claiming build error is non-blocking when the gate already passed."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_BUILD_ERROR_P1)

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_lint_fail_p1_does_not_block_on_gate_pass(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """P1 claiming lint failure is non-blocking when the gate already passed."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_LINT_FAIL_P1)

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_gate_contradicted_audit_trail(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """gate_contradicted findings appear in finding_registry and are legible as suppressed."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_TEST_FAILURE_P1)

        result = run_from_review(config, task, workspace)

        audit = generate_audit_log(config, task, result)

        # Finding must appear in registry with original P1 severity and gate_contradicted
        registry = audit["finding_registry"]
        downgraded = [r for r in registry if r["disposition"] == "gate_contradicted"]
        assert len(downgraded) == 1
        assert downgraded[0]["severity"] == "P1"
        assert "test failure" in downgraded[0]["description"].lower()

        # Suppression must be legible in the audit trail: the gate_contradicted P1
        # appears in non_blocking_p1s (where waived findings are looked for) with
        # its real disposition preserved, not flattened to net_new or omitted.
        non_blocking = audit["non_blocking_p1s"]
        suppressed = [r for r in non_blocking if r["finding_id"] == downgraded[0]["finding_id"]]
        assert len(suppressed) == 1
        assert suppressed[0]["disposition"] == "gate_contradicted"

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_subjective_p1_not_downgraded_on_gate_pass(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Subjective P1 (design/validation issue) is not downgraded even when gate passes."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_SUBJECTIVE_P1)

        result = run_from_review(config, task, workspace)

        # Subjective P1 must still block: cycle 2 == max_review_cycles → ESCALATE
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.review_cycle == 2

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_no_downgrade_when_gate_decision_absent(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """No downgrade when no gate decision has been recorded (safe default)."""
        # run_from_review enters at REVIEW directly without running a gate first.
        # state.gate_decisions is empty on the initial review → no downgrade.
        config = dataclasses.replace(
            _make_config(tmp_path),
            # Single cycle: one REQUEST_CHANGES exhausts max_review_cycles
            retry=dataclasses.replace(_make_config(tmp_path).retry, max_review_cycles=1),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        # Shell returns PASS for gate, but it only runs after first dev cycle
        # which doesn't happen here (review_cycle=1 immediately escalates)
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT

        # Single cycle: P1 with test failure description
        mock_pool.return_value = [
            _make_agent_result(success=True, output=_CYCLE1_TEST_FAILURE_P1, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        # No gate decision → no downgrade → P1 blocks → ESCALATE (max cycles hit)
        assert result.success is False
        assert result.phase == Phase.ESCALATE

        # Finding must NOT have gate_contradicted disposition
        contradicted = [
            r for r in result.state.finding_registry if r.disposition == "gate_contradicted"
        ]
        assert len(contradicted) == 0

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_inadequate_acceptance_p1_blocks_despite_gate_pass_and_keyword(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Replay of the observed trail: a P1 mentioning "test suite" but asserting
        inadequate acceptance evidence, with reviewer matches_spec=false and a PASS
        gate, must NOT be suppressed — it blocks and the story escalates, not DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_INADEQUATE_ACCEPTANCE_AC_FALSE)

        result = run_from_review(config, task, workspace)

        # The finding is not gate-contradictable: story must escalate, not reach DONE.
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.review_cycle == 2

        # The finding must be recorded as blocking (ac_blocking), never gate_contradicted.
        recs = [r for r in result.state.finding_registry if "test suite" in r.description]
        assert len(recs) == 1
        assert recs[0].disposition == "ac_blocking"

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_inadequate_acceptance_keyword_not_downgraded_even_when_ac_true(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Even with matches_spec=true, a "test suite"-mentioning inadequate-coverage
        P1 must not be downgraded to gate_contradicted on a PASS gate. It keeps
        blocking on its own merits (net-new bypass disabled by default)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_INADEQUATE_ACCEPTANCE_AC_TRUE)

        result = run_from_review(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

        # The keyword-matching finding must NOT be suppressed as gate_contradicted.
        recs = [r for r in result.state.finding_registry if "test suite" in r.description]
        assert len(recs) == 1
        assert recs[0].disposition != "gate_contradicted"
