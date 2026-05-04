"""Coordinator review-cycle handling for resolution commentary findings.

Covers the review-phase seam where finding classification runs through the
worktree eval bridge and must not treat closure commentary as a surviving prior
finding.
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

from theforge.config import FindingClassifierConfig
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.state import Phase

_CYCLE1_REQUEST_CHANGES = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Validation missing."
findings:
  - severity: P1
    file: src/changed.py
    line: 10
    observed: "Missing null handling in runtime path"
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

_CYCLE2_RESOLUTION_COMMENTARY = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Prior issue confirmed fixed."
findings:
  - severity: P1
    file: src/changed.py
    line: 10
    observed: "Previous finding fixed: missing null handling in runtime path"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "None"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""

_CYCLE2_UNRESOLVED_WORDING = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Issue still open."
findings:
  - severity: P1
    file: src/changed.py
    line: 10
    observed: "The prior finding is unresolved: missing null handling in runtime path"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Still needs a null guard"
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
    """Run subprocess-eval commands in-process so test patches stay active."""
    from theforge.coordinator._subprocess_eval import _COMMANDS

    return _COMMANDS[command](payload)


class TestResolutionCommentaryBridge:
    def _two_cycle_pool(self, cycle2_review: str):
        call_n = {"n": 0}

        def side_effect(**kwargs):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=_CYCLE1_REQUEST_CHANGES,
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=cycle2_review,
                    profile_name="review",
                )
            ]

        return side_effect

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_resolution_commentary_does_not_revive_prior_finding_across_cycles(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        base = _make_config(tmp_path)
        config = dataclasses.replace(
            base,
            finding_classifier=FindingClassifierConfig(allow_net_new_bypass=True),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_RESOLUTION_COMMENTARY)

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 2

        prior = next(
            r
            for r in result.state.finding_registry
            if r.description == "Missing null handling in runtime path"
        )
        commentary = next(
            r
            for r in result.state.finding_registry
            if r.description == "Previous finding fixed: missing null handling in runtime path"
        )
        assert prior.disposition == "fixed"
        assert prior.cycle_last_seen == 1
        assert commentary.disposition == "net_new"
        assert commentary.cycle_first_seen == 2

    @patch("theforge.coordinator.util._run_worktree_eval", side_effect=_in_process_worktree_eval)
    @patch("theforge.finding_classifier._get_changed_files")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_unresolved_wording_still_tracks_prior_finding_across_cycles(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_changed_files,
        mock_eval,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_changed_files.return_value = frozenset(["src/changed.py"])
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_pool.side_effect = self._two_cycle_pool(_CYCLE2_UNRESOLVED_WORDING)

        result = run_from_review(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.review_cycle == 2
        assert len(result.state.finding_registry) == 1

        prior = result.state.finding_registry[0]
        assert prior.description == "Missing null handling in runtime path"
        assert prior.disposition == "unresolved"
        assert prior.cycle_last_seen == 2
