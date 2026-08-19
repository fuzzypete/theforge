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
    patch_gate_shell,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import _parse_preflight_verdict
from theforge.coordinator.state import Phase

# ── Unit tests for _parse_preflight_verdict ───────────────────────────────────


class TestParsePreflightVerdictUnit:
    def test_prefers_yaml_fence_when_another_fence_comes_first(self):
        output = """```python
print("quoted offending snippet")
```
```yaml
verdict: PROCEED
reason: "structured classification"
work_type: bug
domains:
  - testing
```"""
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "PROCEED"
        assert degraded is False
        assert reason == "structured classification"

    def test_nested_fence_inside_yaml_scalar_keeps_outer_payload(self):
        output = """```yaml
verdict: PROCEED
reason: |
  Example:
    ```python
    print("nested")
    ```
```"""
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "PROCEED"
        assert degraded is False
        assert "Example:" in reason

    def test_four_backtick_outer_fence_keeps_yaml_classification(self):
        output = """````yaml
verdict: PROCEED
reason: "four-backtick wrapper"
```python
print("inner snippet")
```
````
"""
        verdict, reason, degraded = _parse_preflight_verdict(output)
        assert verdict == "PROCEED"
        assert degraded is False
        assert reason == "four-backtick wrapper"

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
    @patch_gate_shell()
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
    @patch_gate_shell()
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
        # #1773: an unparseable first output is re-requested against the same
        # profile before falling through to degraded PROCEED.
        assert mock_preflight.call_count == 2
        assert [c.kwargs["profile"].name for c in mock_preflight.call_args_list] == [
            config.preflight_profile.name,
            config.preflight_profile.name,
        ]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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


# ── Same-profile parse-error retry (#1773) ─────────────────────────────────────

# The conversational narration observed in #1453 / #1735: sonnet narrated a
# hand-off to a non-existent "investigation agent" in place of the structured
# dict. yaml.safe_load parses this scalar prose to a string, so the parser
# reports parse_error.
PREFLIGHT_NARRATION = "I'll pause here until the investigation agent's findings come back."

PREFLIGHT_PROCEED_WITH_FILES = """\
```yaml
verdict: PROCEED
reason: "Needs implementation."
complexity: medium
sufficiency: needs_planning
work_type: feature
likely_files:
  - "src/theforge/coordinator/preflight_flow.py"
```
"""


class TestPreflightParseErrorRetry:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_parse_error_retried_same_profile_recovers_metadata(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """First output is narration (parse_error); same-profile retry recovers a
        structured result, so the run is NOT degraded and agent-derived metadata
        (likely_files) is preserved."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_NARRATION, cost_usd=0.05),
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_WITH_FILES, cost_usd=0.05),
        ]
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert mock_preflight.call_count == 2
        # Both attempts used the SAME (primary) profile — no fallback involved.
        assert [c.kwargs["profile"].name for c in mock_preflight.call_args_list] == [
            config.preflight_profile.name,
            config.preflight_profile.name,
        ]
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is False
        assert result.state.preflight_degraded_reason is None
        # Recovered structured metadata — not the silent-None of a discarded run.
        assert result.state.preflight_likely_files == [
            "src/theforge/coordinator/preflight_flow.py"
        ]
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_parse_error_retry_exhausted_marks_degraded(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """When the same-profile retry is also unparseable, the run is marked
        degraded (parse_error) with None likely_files so collision serialization
        treats the footprint as unknown-and-risky, not known-empty."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_NARRATION, cost_usd=0.05),
            _make_agent_result(success=True, output=PREFLIGHT_NARRATION, cost_usd=0.05),
        ]
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert mock_preflight.call_count == 2
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "parse_error"
        assert result.state.preflight_likely_files is None
        # Both attempts recorded in the audit trail for traceability.
        assert len(result.state.preflight_result.raw["attempts"]) == 2
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_parse_error_retry_precedes_fallback_profile(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """With a fallback profile configured, the same-profile retry fires FIRST;
        only when it is still unparseable does the fallback profile run."""
        from theforge.config.types import ModelProfile

        config = _make_config(tmp_path)
        fallback = ModelProfile(
            name="preflight_fallback",
            cli="gemini",
            model="gemini-2.5-pro",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash", "Glob", "Grep"),
            phase="preflight",
        )
        config = config.__class__(**{**config.__dict__, "preflight_fallback_profile": fallback})
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.side_effect = [
            _make_agent_result(success=True, output=PREFLIGHT_NARRATION, cost_usd=0.05),
            _make_agent_result(success=True, output=PREFLIGHT_NARRATION, cost_usd=0.05),
            _make_agent_result(success=True, output=PREFLIGHT_PROCEED_WITH_FILES, cost_usd=0.05),
        ]
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert mock_preflight.call_count == 3
        assert [c.kwargs["profile"].name for c in mock_preflight.call_args_list] == [
            config.preflight_profile.name,
            config.preflight_profile.name,
            "preflight_fallback",
        ]
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is False
        assert result.state.preflight_likely_files == [
            "src/theforge/coordinator/preflight_flow.py"
        ]
        assert result.phase == Phase.DONE
        assert result.success is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_non_yaml_fence_before_yaml_block_keeps_real_classification(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="""```python
print("quoted offending snippet")
```
```yaml
verdict: PROCEED
reason: "classification after quoted code"
complexity: small
sufficiency: implementation_ready
work_type: bug
domains:
  - testing
likely_files:
  - "src/theforge/coordinator/preflight.py"
```""",
            cost_usd=0.05,
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_degraded is False
        assert result.state.preflight_work_type == "bug"
        assert result.state.preflight_domains == ["testing"]
        assert result.state.preflight_likely_files == ["src/theforge/coordinator/preflight.py"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_four_backtick_outer_fence_keeps_real_classification(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="""````yaml
verdict: PROCEED
reason: |
  Classification after a wrapped snippet.
  ```python
  print("inner snippet")
  ```
complexity: small
sufficiency: implementation_ready
work_type: bug
domains:
  - testing
likely_files:
  - "src/theforge/coordinator/preflight.py"
````
""",
            cost_usd=0.05,
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_degraded is False
        assert result.state.preflight_reason.startswith("Classification after a wrapped snippet.")
        assert result.state.preflight_work_type == "bug"
        assert result.state.preflight_domains == ["testing"]
        assert result.state.preflight_likely_files == ["src/theforge/coordinator/preflight.py"]
