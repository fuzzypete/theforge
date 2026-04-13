"""Tests for preflight ALREADY_DONE AC-evidence downgrade logic (issue #705).

Acceptance criteria:
- All ACs evidenced (files_checked non-empty, satisfied=true) → ALREADY_DONE honored
- Any AC lacking files_checked evidence → verdict downgraded to PROCEED
- Missing/empty criteria_checked → verdict downgraded to PROCEED
- criteria_checked evidence map persists in the preflight.yaml artifact
"""

from __future__ import annotations

from unittest.mock import patch

import yaml as _yaml
from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# ── Test fixtures ─────────────────────────────────────────────────────────────

# All ACs satisfied with concrete file evidence — ALREADY_DONE should be honored.
PREFLIGHT_ALREADY_DONE_ALL_EVIDENCED = """\
```yaml
verdict: ALREADY_DONE
reason: "All acceptance criteria are satisfied with file evidence."
complexity: small
sufficiency: implementation_ready
work_type: feature
criteria_checked:
  - criterion: "Feature X is implemented"
    satisfied: true
    files_checked:
      - "src/theforge/coordinator/engine.py"
    evidence: "Found implementation at engine.py:42"
  - criterion: "Feature X has tests"
    satisfied: true
    files_checked:
      - "tests/test_coord_engine.py"
    evidence: "Test coverage confirmed at test_coord_engine.py:100"
```
"""

# One AC has empty files_checked — should be downgraded to PROCEED.
PREFLIGHT_ALREADY_DONE_PARTIAL_EVIDENCE = """\
```yaml
verdict: ALREADY_DONE
reason: "Some criteria lack file evidence."
complexity: small
sufficiency: implementation_ready
work_type: feature
criteria_checked:
  - criterion: "Feature X is implemented"
    satisfied: true
    files_checked:
      - "src/theforge/coordinator/engine.py"
    evidence: "Confirmed at engine.py:42"
  - criterion: "Feature Y is implemented"
    satisfied: true
    files_checked: []
    evidence: "Believed to be done but no file checked"
```
"""

# No criteria_checked field at all — should be downgraded to PROCEED.
PREFLIGHT_ALREADY_DONE_NO_CRITERIA = """\
```yaml
verdict: ALREADY_DONE
reason: "Looks done."
complexity: small
sufficiency: implementation_ready
work_type: feature
```
"""

# AC marked satisfied=false — should be downgraded to PROCEED.
PREFLIGHT_ALREADY_DONE_UNSATISFIED_AC = """\
```yaml
verdict: ALREADY_DONE
reason: "One AC not satisfied."
complexity: small
sufficiency: implementation_ready
work_type: feature
criteria_checked:
  - criterion: "Feature X is implemented"
    satisfied: false
    files_checked:
      - "src/theforge/coordinator/engine.py"
    evidence: "Found partial implementation"
```
"""


class TestPreflightAlreadyDoneVerification:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_all_acs_evidenced_already_done_honored(
        self,
        mock_approve,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """All ACs have non-empty files_checked and satisfied=true → ALREADY_DONE honored."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE_ALL_EVIDENCED, cost_usd=0.05
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "ALREADY_DONE"
        # No dev or review should have run
        mock_dev.assert_not_called()
        mock_pool.assert_not_called()
        # Criteria evidence preserved on state
        assert len(result.state.preflight_criteria_checked) == 2
        assert (
            result.state.preflight_criteria_checked[0]["criterion"] == "Feature X is implemented"
        )
        assert result.state.preflight_criteria_checked[0]["satisfied"] is True
        assert (
            "src/theforge/coordinator/engine.py"
            in result.state.preflight_criteria_checked[0]["files_checked"]
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_partial_evidence_downgraded_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """One AC with files_checked=[] → ALREADY_DONE downgraded to PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE_PARTIAL_EVIDENCE, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_verdict == "PROCEED"
        # Warning about the missing evidence should appear
        assert any("Feature Y" in w for w in (result.state.preflight_warnings or []))
        # Run should have proceeded through dev
        mock_dev.assert_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_no_criteria_checked_downgraded_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """Missing criteria_checked on ALREADY_DONE → downgraded to PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE_NO_CRITERIA, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert any("criteria_checked absent" in w for w in (result.state.preflight_warnings or []))
        mock_dev.assert_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_unsatisfied_ac_downgraded_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """AC with satisfied=false → ALREADY_DONE downgraded to PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE_UNSATISFIED_AC, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert any("satisfied=false" in w for w in (result.state.preflight_warnings or []))

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_criteria_checked_preserved_in_artifact(
        self,
        mock_approve,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """criteria_checked evidence map is written to the preflight.yaml artifact."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE_ALL_EVIDENCED, cost_usd=0.05
        )

        result = run_task(config, task)

        assert result.success is True
        log_dir = result.state.log_dir
        assert log_dir is not None
        preflight_yaml = log_dir / "preflight.yaml"
        assert preflight_yaml.exists(), f"preflight.yaml not found in {log_dir}"
        artifact = _yaml.safe_load(preflight_yaml.read_text(encoding="utf-8"))
        assert "criteria_checked" in artifact
        criteria = artifact["criteria_checked"]
        assert isinstance(criteria, list)
        assert len(criteria) == 2
        assert criteria[0]["criterion"] == "Feature X is implemented"
        assert "src/theforge/coordinator/engine.py" in criteria[0]["files_checked"]
