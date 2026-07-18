"""Seam-level integration tests for symptom-verification test escalation (#1560).

Drives the full REVIEW phase boundary via ``run_from_review`` and asserts that a
bug-fix PR whose reviewer flags an absent seam-level test for the closing bug's
symptom path produces a P1 (blocking) verdict — not a P2 that ships silently.

This is the mechanical replay of the #1402 / #1407 failure mode: #1407 shipped
APPROVE with a P2 flagging the missing seam-level driver, and six days later the
symptom recurred on the same code path. Under this rule the finding blocks.
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

# The #1407 finding: reviewer APPROVES but files a P2 noting the seam-level
# integration test for the symptom path is absent.
_APPROVE_WITH_MISSING_SYMPTOM_TEST = """\
```yaml
verdict: APPROVE
summary: "Fix looks correct."
findings:
  - severity: P2
    file: tests/test_sprint_parallel.py
    line: 1539
    observed: "No test drives run_sprint through dependent dispatch."
    expected: "Seam-level integration tests must cover the symptom path."
    evidence: "tests/test_sprint_parallel.py:1539"
    suggestion: "Add a seam-level integration test for the symptom path."
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
ac_verification:
  - criterion: "Symptom resolution: dependent dispatch rebase"
    status: VERIFIED
    evidence: "diff hunks present and unit test covers _poll_queued_pr (test fixture)"
```
"""

# A generic coverage-gap P2 with no seam/symptom-path signal — must NOT escalate.
_APPROVE_WITH_GENERIC_COVERAGE_P2 = """\
```yaml
verdict: APPROVE
summary: "Fix looks correct."
findings:
  - severity: P2
    file: src/theforge/foo.py
    line: 42
    observed: "Test coverage could be higher for the helper function."
    expected: "Additional unit tests would improve confidence."
    evidence: "src/theforge/foo.py:42"
    suggestion: "Consider adding more unit tests."
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
ac_verification:
  - criterion: "Symptom resolution: helper behaviour"
    status: VERIFIED
    evidence: "diff hunks present and tests cover the failure mode (test fixture)"
```
"""


def _in_process_worktree_eval(
    workspace_path: Path, command: str, payload: dict, timeout: int = 120
) -> dict:
    """Run subprocess eval commands in-process for testing."""
    from theforge.coordinator._subprocess_eval import _COMMANDS

    return _COMMANDS[command](payload)


def _bug_task(tmp_path: Path):
    return dataclasses.replace(_make_task(tmp_path), type="bug")


def _single_cycle_config(tmp_path: Path):
    cfg = _make_config(tmp_path)
    return dataclasses.replace(
        cfg,
        retry=dataclasses.replace(cfg.retry, max_review_cycles=1, p2_cleanup_enabled=False),
    )


class TestSymptomTestEscalation:
    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_missing_symptom_test_p2_escalates_to_blocking_p1(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Bug-fix PR: a P2 flagging the missing seam-level symptom test blocks as P1."""
        config = _single_cycle_config(tmp_path)
        task = _bug_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.return_value = [
            _make_agent_result(
                success=True,
                output=_APPROVE_WITH_MISSING_SYMPTOM_TEST,
                profile_name="review",
            )
        ]

        result = run_from_review(config, task, workspace)

        # Escalated to blocking P1 → not DONE despite the reviewer's APPROVE verdict.
        assert result.success is False
        assert result.phase == Phase.ESCALATE

        # The escalation is recorded in state (queryable audit substrate).
        escalations = result.state.symptom_test_escalations
        assert len(escalations) == 1
        assert escalations[0]["original_severity"] == "P2"
        assert escalations[0]["effective_severity"] == "P1"
        assert escalations[0]["file"] == "tests/test_sprint_parallel.py"

        # The finding is recorded in the registry at the escalated P1 severity.
        recs = [
            r for r in result.state.finding_registry if r.file == "tests/test_sprint_parallel.py"
        ]
        assert len(recs) == 1
        assert recs[0].severity == "P1"

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_recorded_in_audit_substrate(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """The classifier's P2→P1 decision is queryable from the audit log (AC4)."""
        config = _single_cycle_config(tmp_path)
        task = _bug_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.return_value = [
            _make_agent_result(
                success=True,
                output=_APPROVE_WITH_MISSING_SYMPTOM_TEST,
                profile_name="review",
            )
        ]

        result = run_from_review(config, task, workspace)
        audit = generate_audit_log(config, task, result)

        escalations = audit["symptom_test_escalations"]
        assert escalations is not None
        assert len(escalations) == 1
        assert escalations[0]["review_cycle"] == 1
        assert escalations[0]["effective_severity"] == "P1"

        # The escalated severity also flows into the per-cycle review record.
        cycle1 = audit["reviews"][0]
        assert cycle1["p1_count"] == 1
        assert cycle1["p2_count"] == 0

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_generic_coverage_p2_not_escalated_on_bug(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """AC3: a generic 'coverage could be higher' P2 is not escalated → APPROVE stands."""
        config = _single_cycle_config(tmp_path)
        task = _bug_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.return_value = [
            _make_agent_result(
                success=True,
                output=_APPROVE_WITH_GENERIC_COVERAGE_P2,
                profile_name="review",
            )
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.symptom_test_escalations == []

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_missing_symptom_test_not_escalated_on_non_bug(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        """Out of scope: the rule is bug-fix-only. A non-bug story's P2 stays P2."""
        config = _single_cycle_config(tmp_path)
        task = _make_task(tmp_path)  # no type → not a bug-class story
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.return_value = [
            _make_agent_result(
                success=True,
                output=_APPROVE_WITH_MISSING_SYMPTOM_TEST,
                profile_name="review",
            )
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.symptom_test_escalations == []
