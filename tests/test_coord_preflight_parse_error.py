"""Tests for preflight parser degradation (#709).

Covers the three success=True cases where _parse_preflight_verdict previously
returned BLOCKED but should now return PROCEED with degraded=True:
  1. YAML parse failure
  2. Output is not a dict
  3. Unknown verdict string
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import _parse_preflight_verdict
from theforge.coordinator.state import Phase

# ── Unit tests for _parse_preflight_verdict ───────────────────────────────────


class TestParsePreflightVerdictUnit:
    def test_yaml_parse_failure_returns_proceed_degraded(self):
        """YAML parse error → PROCEED + degraded=True, not BLOCKED."""
        output = "```yaml\n: invalid: yaml: [\n```"
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "PROCEED"
        assert degraded is True
        assert "parse" in reason.lower() or "yaml" in reason.lower()

    def test_non_dict_output_returns_proceed_degraded(self):
        """Non-dict parsed YAML → PROCEED + degraded=True, not BLOCKED."""
        output = "```yaml\n- item1\n- item2\n```"
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "PROCEED"
        assert degraded is True
        assert "not a dict" in reason.lower() or "dict" in reason.lower()

    def test_unknown_verdict_returns_proceed_degraded(self):
        """Unknown verdict string → PROCEED + degraded=True, not BLOCKED."""
        output = "```yaml\nverdict: SKIP\nreason: 'not a valid verdict'\n```"
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "PROCEED"
        assert degraded is True
        assert "skip" in reason.lower() or "unknown" in reason.lower()

    def test_valid_proceed_not_degraded(self):
        """Valid PROCEED output → not degraded."""
        output = "```yaml\nverdict: PROCEED\nreason: 'Go ahead'\n```"
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "PROCEED"
        assert degraded is False

    def test_valid_blocked_not_degraded(self):
        """Valid BLOCKED output → not degraded (classifier made a real decision)."""
        output = "```yaml\nverdict: BLOCKED\nreason: 'Dependency missing'\n```"
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "BLOCKED"
        assert degraded is False

    def test_valid_already_done_not_degraded(self):
        """Valid ALREADY_DONE output → not degraded."""
        output = "```yaml\nverdict: ALREADY_DONE\nreason: 'All criteria met'\n```"
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "ALREADY_DONE"
        assert degraded is False

    def test_empty_output_returns_proceed_degraded(self):
        """Empty output fails YAML parse → PROCEED + degraded=True."""
        verdict, reason, degraded = _parse_preflight_verdict("")
        # yaml.safe_load("") returns None, which is not a dict
        assert verdict == "PROCEED"
        assert degraded is True

    def test_plain_text_no_fence_non_dict(self):
        """Plain text that parses as a scalar → PROCEED + degraded=True."""
        output = "just some text that is not yaml at all!!! @@@"
        verdict, reason, degraded = _parse_preflight_verdict(output)
        # yaml.safe_load returns a string (scalar), not a dict
        assert verdict == "PROCEED"
        assert degraded is True


# ── Integration tests via run_task ────────────────────────────────────────────


PREFLIGHT_MALFORMED_YAML = "```yaml\n: bad: yaml: [\n```"

PREFLIGHT_NON_DICT = "```yaml\n- item1\n- item2\n```"

PREFLIGHT_UNKNOWN_VERDICT = """\
```yaml
verdict: SKIP
reason: "Not a valid verdict string"
complexity: medium
sufficiency: needs_planning
work_type: feature
```
"""


class TestPreflightParseErrorIntegration:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_yaml_parse_failure_falls_back_to_proceed(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """YAML parse error: success=True + malformed → degraded PROCEED, not BLOCKED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_MALFORMED_YAML, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "parse_error"
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_non_dict_output_falls_back_to_proceed(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Non-dict YAML: success=True + list output → degraded PROCEED, not BLOCKED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_NON_DICT, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "parse_error"
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_unknown_verdict_falls_back_to_proceed(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Unknown verdict: success=True + garbage verdict → degraded PROCEED, not BLOCKED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_UNKNOWN_VERDICT, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "parse_error"
        assert result.phase == Phase.DONE
        assert result.success is True
