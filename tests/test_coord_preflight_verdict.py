"""Tests for preflight verdict, complexity parsing, and complexity adaptation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    PREFLIGHT_ALREADY_DONE,
    PREFLIGHT_BLOCKED,
    PREFLIGHT_PROCEED_WITH_WARNINGS,
    _make_agent_result,
    _make_config,
    _make_pool_result,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import (
    _apply_complexity_adaptation,
    _parse_preflight_complexity,
    _parse_preflight_warnings,
)
from theforge.coordinator.preflight_cache import _story_content_hash
from theforge.coordinator.state import CoordinatorState, Phase

# ── Preflight phase tests ─────────────────────────────────────────────


class TestCoordinatorPreflight:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_already_done_uses_clean_baseline_not_resumed_worktree(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Resumed-worktree-only implementation must not trigger ALREADY_DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        (workspace / "resumed_only.py").write_text("implemented = True\n", encoding="utf-8")

        captured = {}

        def preflight_side_effect(**kwargs):
            working_dir = Path(kwargs["working_dir"])
            captured["working_dir"] = working_dir
            captured["has_resumed_only"] = (working_dir / "resumed_only.py").exists()
            return _make_agent_result(success=True, output=_PREFLIGHT_RESULT.output, cost_usd=0.08)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.side_effect = preflight_side_effect
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_verdict == "PROCEED"
        assert captured["working_dir"] != workspace
        assert captured["has_resumed_only"] is False
        mock_pool.assert_called_once()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_preflight_already_done_allows_clean_baseline_match(
        self,
        mock_approve,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """Baseline content satisfying the story may still return ALREADY_DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured = {}

        def preflight_side_effect(**kwargs):
            working_dir = Path(kwargs["working_dir"])
            captured["working_dir"] = working_dir
            captured["exists"] = working_dir.exists()
            return _make_agent_result(success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.08)

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.side_effect = preflight_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "ALREADY_DONE"
        assert captured["working_dir"] != workspace
        assert captured["exists"] is True
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_proceed_continues_to_dev(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """PROCEED verdict → normal dev→validate→review flow."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_preflight_already_done_skips_dev(
        self,
        mock_approve,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """ALREADY_DONE verdict with prior APPROVE → DONE immediately, no dev or review."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.08, profile_name="review"
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "ALREADY_DONE"
        assert "already" in result.message.lower()
        assert len(result.state.dev_results) == 0
        assert len(result.state.review_results) == 0
        assert mock_preflight.call_count == 1
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_blocked_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """BLOCKED verdict → ESCALATE with reason."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED, cost_usd=0.08, profile_name="review"
        )

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.preflight_verdict == "BLOCKED"
        assert "blocked" in result.message.lower()
        assert "removed_function" in result.message
        assert len(result.state.dev_results) == 0
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_agent_failure_falls_back_to_degraded_proceed(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """If the preflight agent itself fails, coordinator falls back to degraded PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        preflight_fail = _make_agent_result(success=False, output="CLI error", cost_usd=0.0)
        dev_ok = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = preflight_fail
        mock_agent.return_value = dev_ok
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Fallback PROCEED — not BLOCKED/ESCALATE
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "timeout_no_verdict"
        assert "failed" in result.state.preflight_reason.lower()
        # Conservative classification applied
        assert result.state.preflight_sufficiency == "needs_planning"
        assert result.state.preflight_complexity == "large"
        # Run continues to dev
        assert len(result.state.dev_results) >= 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_unparseable_falls_back_to_proceed(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Unparseable preflight output falls back to degraded PROCEED, not BLOCKED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        preflight_garbage = _make_agent_result(
            success=True, output="I don't know what to do", cost_usd=0.05
        )
        dev_ok = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = preflight_garbage
        mock_agent.return_value = dev_ok
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "parse_error"
        assert len(result.state.dev_results) >= 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_unknown_verdict_falls_back_to_proceed(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Unknown preflight verdicts fall back to degraded PROCEED, not hard failure."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="""```yaml
verdict: APPROVE
complexity: small
reason: \"Model returned a review verdict during preflight.\"
likely_files:
  - src/theforge/coordinator/preflight_flow.py
```
""",
            cost_usd=0.05,
            profile_name="review",
        )
        dev_ok = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.return_value = dev_ok
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert result.state.preflight_degraded is True
        assert result.state.preflight_degraded_reason == "parse_error"
        assert "unknown preflight verdict" in result.state.preflight_reason.lower()
        # likely_files still parsed from the valid YAML even when verdict is unknown
        assert result.state.preflight_likely_files == [
            "src/theforge/coordinator/preflight_flow.py"
        ]
        assert len(result.state.dev_results) >= 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_concurrency_multiphase_story_upgrades_medium_preflight_to_large_for_routing(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Concurrency-sensitive coordinator surgery is force-sized to LARGE."""
        config = _make_smart_config(tmp_path)
        task = _make_task(tmp_path)
        task.story_path.write_text(
            """# Test Spec

Thread a per-story cancellation signal through the sprint runner, coordinator loop,
and every phase function. Insert cancellation checks at each phase boundary and
update the state machine handoff so cleanup does not emit duplicate terminal records.
""",
            encoding="utf-8",
        )
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="""```yaml
verdict: PROCEED
complexity: medium
complexity_score: 5
work_type: feature
contract_change: false
reason: "Looks like a medium multi-file coordinator fix."
sufficiency: implementation_ready
sufficiency_reason: "Spec is detailed."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Cancellation signal reaches every phase boundary"
    satisfied: false
    evidence: "Not found in codebase"
```""",
        )
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_complexity == "large"
        assert result.state.preflight_complexity_score == 8
        assert any(
            "coordinator override: upgraded complexity to large" in warning
            for warning in result.state.preflight_warnings
        )
        audit = result.state.complexity_routing_audit
        assert audit is not None
        assert audit["complexity"] == "large"
        assert audit["complexity_score"] == 8
        assert audit["derived_dev_model"] == "opus"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_bug_story_with_lifecycle_vocabulary_not_upgraded_to_large(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Bug stories that mention lifecycle/cancellation context must not be upgraded.

        Regression for issue #1150: the coordinator override was firing on bug *context*
        (what the bug is about) rather than fix *shape* (what change is required). A
        display fix in cli/status.py that describes 'prior terminal story state during
        resumed run' should stay at MEDIUM — the preflight LLM rates the fix correctly
        from the codebase; the keyword override must not re-classify it.
        """
        config = _make_smart_config(tmp_path)
        task = _make_task(tmp_path)
        task.story_path.write_text(
            """# forge status MODEL column shows panel(N) during resumed run

## What happened

The MODEL column in forge status shows panel(N) instead of the model name when
the sprint is resumed and reuses prior terminal story state from a previous run.
The coordinator loop carries prior lifecycle state across phases, so the resumed
run displays stale panel references from the earlier lifecycle/cancellation flow.

## What was expected

The MODEL column should always show the current model name from the active run,
not a panel reference inherited from prior terminal state.
""",
            encoding="utf-8",
        )
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="""```yaml
verdict: PROCEED
complexity: medium
complexity_score: 5
work_type: bug
contract_change: false
reason: "Single-file display fix in cli/status.py."
sufficiency: implementation_ready
sufficiency_reason: "Fix is localised to status rendering."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "MODEL column shows model name"
    satisfied: false
    evidence: "Wrong value returned in status render path"
```""",
        )
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_complexity == "medium"
        assert result.state.preflight_complexity_score == 5
        assert not any(
            "coordinator override: upgraded complexity to large" in warning
            for warning in (result.state.preflight_warnings or [])
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_cost_in_audit(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Preflight cost appears in audit log."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.08, profile_name="review"
        )

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert audit["preflight"] is not None
        assert audit["preflight"]["verdict"] == "ALREADY_DONE"
        assert audit["preflight"]["cost_usd"] == 0.08
        assert "already" in audit["preflight"]["reason"].lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_file_path_warning_proceeds(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Missing file paths in story → warning stored in state, PROCEED continues to dev."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output=PREFLIGHT_PROCEED_WITH_WARNINGS,
            cost_usd=0.05,
            profile_name="review",
        )
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert len(result.state.preflight_warnings) == 2
        assert "src/theforge/old_module.py" in result.state.preflight_warnings[0]
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    @patch("theforge.coordinator.engine._fire_post_run_hook")
    def test_post_run_hook_fires_on_already_done(
        self,
        mock_hook,
        mock_approve,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """ALREADY_DONE verdict → post_run hook fires exactly once."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.08, profile_name="review"
        )

        result = run_task(config, task)

        assert result.state.preflight_verdict == "ALREADY_DONE"
        mock_hook.assert_called_once()
        called_result = mock_hook.call_args[0][3]
        assert called_result is result

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.engine._fire_post_run_hook")
    def test_post_run_hook_fires_on_blocked(
        self,
        mock_hook,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """BLOCKED verdict → post_run hook fires exactly once."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED, cost_usd=0.08, profile_name="review"
        )

        result = run_task(config, task)

        assert result.state.preflight_verdict == "BLOCKED"
        mock_hook.assert_called_once()
        called_result = mock_hook.call_args[0][3]
        assert called_result is result

    def test_parse_preflight_likely_files_requires_explicit_field(self):
        """Missing likely_files stays unknown instead of defaulting to zero-footprint."""
        from coord_test_helpers import PREFLIGHT_PROCEED

        from theforge.coordinator.preflight import _parse_preflight_likely_files

        assert _parse_preflight_likely_files(PREFLIGHT_PROCEED) is None

    def test_parse_preflight_likely_files_allows_explicit_zero_footprint(self):
        """An explicit empty likely_files list is preserved as zero-footprint."""
        from theforge.coordinator.preflight import _parse_preflight_likely_files

        output = """```yaml
verdict: PROCEED
reason: \"No file edits expected.\"
likely_files: []
```
"""

        assert _parse_preflight_likely_files(output) == []

    def test_parse_preflight_warnings_extracts_paths(self):
        """_parse_preflight_warnings returns the warnings list from YAML."""
        warnings = _parse_preflight_warnings(PREFLIGHT_PROCEED_WITH_WARNINGS)
        assert len(warnings) == 2
        assert "src/theforge/old_module.py does not exist on disk" in warnings
        assert "src/theforge/another_missing.py does not exist on disk" in warnings

    def test_parse_preflight_warnings_empty_when_absent(self):
        """_parse_preflight_warnings returns [] when warnings field is missing."""
        from coord_test_helpers import PREFLIGHT_PROCEED

        warnings = _parse_preflight_warnings(PREFLIGHT_PROCEED)
        assert warnings == []

    def test_parse_preflight_warnings_empty_on_garbage(self):
        """_parse_preflight_warnings returns [] on unparseable input."""
        warnings = _parse_preflight_warnings("not yaml at all")
        assert warnings == []


# ── Complexity parsing tests ──────────────────────────────────────────


_PREFLIGHT_PROCEED_SMALL = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Single-file config change."
criteria_checked: []
```
"""

_PREFLIGHT_PROCEED_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Multi-file feature with tests."
criteria_checked: []
```
"""

_PREFLIGHT_PROCEED_LARGE = """\
```yaml
verdict: PROCEED
complexity: large
reason: "Cross-cutting refactor."
criteria_checked: []
```
"""

_PREFLIGHT_NO_COMPLEXITY = """\
```yaml
verdict: PROCEED
reason: "No complexity field."
criteria_checked: []
```
"""


class TestParsePreflightComplexity:
    def test_complexity_with_nested_fence_in_reason(self):
        output = """```yaml
verdict: PROCEED
reason: |
  Example:
    ```text
    nested fence
    ```
complexity: large
```"""
        assert _parse_preflight_complexity(output) == "large"

    def test_complexity_parsed_small(self):
        assert _parse_preflight_complexity(_PREFLIGHT_PROCEED_SMALL) == "small"

    def test_complexity_parsed_medium(self):
        assert _parse_preflight_complexity(_PREFLIGHT_PROCEED_MEDIUM) == "medium"

    def test_complexity_parsed_large(self):
        assert _parse_preflight_complexity(_PREFLIGHT_PROCEED_LARGE) == "large"

    def test_complexity_default_medium(self):
        """Missing complexity line → medium."""
        assert _parse_preflight_complexity(_PREFLIGHT_NO_COMPLEXITY) == "medium"

    def test_complexity_default_on_invalid_yaml(self):
        """Malformed YAML → medium."""
        assert _parse_preflight_complexity("```yaml\n{bad: [yaml\n```") == "medium"

    def test_complexity_default_on_empty(self):
        assert _parse_preflight_complexity("") == "medium"

    def test_complexity_case_insensitive(self):
        output = "```yaml\nverdict: PROCEED\ncomplexity: LARGE\n```"
        assert _parse_preflight_complexity(output) == "large"

    def test_complexity_invalid_value_defaults_medium(self):
        output = "```yaml\nverdict: PROCEED\ncomplexity: huge\n```"
        assert _parse_preflight_complexity(output) == "medium"


# ── Complexity-adaptive model swapping tests ──────────────────────────


def _make_smart_config(tmp_path: Path) -> ForgeConfig:
    """Build a ForgeConfig that mimics a 3-model smart config."""
    sonnet = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=30.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )
    preflight = ModelProfile(
        name="preflight",
        cli="claude",
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
    opus_reviewer = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
    gpt_reviewer = ModelProfile(
        name="openai-gpt-5.4",
        cli="codex",
        model="gpt-5.4",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
    synthesis = ModelProfile(
        name="synthesis",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=sonnet,
        preflight_profile=preflight,
        review_pool=[opus_reviewer, gpt_reviewer],
        synthesis_profile=synthesis,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            # `complexity: large` fixtures here assert routing/state, not the
            # preflight complexity gate. Threshold above COMPLEXITY_SCORE_MAX
            # disables the gate so those assertions stay the subject.
            preflight_complexity_gate_threshold=11,
        ),
        models=["claude/sonnet", "claude/opus", "openai/gpt-5.4"],
        plan_model_is_default=True,
        dev_profile_is_default=True,
        review_pool_is_default=True,
    )


class TestComplexityAdaptation:
    def test_medium_routes_dev_to_mid_tier(self, tmp_path):
        """medium complexity → dev_profile uses mid-tier model (AC: dev medium→mid)."""
        config = _make_smart_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "medium")
        # Pool is [sonnet (cheap), opus (strong), gpt-5.4 (mid)] → mid=gpt-5.4
        assert adapted.dev_profile.model == "gpt-5.4"

    def test_small_reduces_review_pool(self, tmp_path):
        """small complexity → single cheapest reviewer, no synthesis."""
        config = _make_smart_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "small")
        assert len(adapted.review_pool) == 1
        assert adapted.synthesis_profile is None

    def test_large_upgrades_dev(self, tmp_path):
        """large complexity → dev uses strongest model (opus)."""
        config = _make_smart_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "large")
        assert adapted.dev_profile.model == "opus"
        assert adapted.dev_profile.cli == "claude"

    def test_large_api_dev_uses_provider_model_registry_match(self, tmp_path):
        """large complexity ranks API profiles by provider/model instead of defaulting."""
        config = _make_smart_config(tmp_path)
        api_dev = replace(
            config.dev_profile,
            cli=None,
            provider="openai",
            model="gpt-5.4",
        )
        api_review = replace(
            config.review_pool[0],
            cli=None,
            provider="anthropic",
            model="opus",
        )
        reviewer = replace(
            config.review_pool[1],
            cli=None,
            provider="openai",
            model="gpt-5.4",
        )
        api_config = replace(config, dev_profile=api_dev, review_pool=[api_review, reviewer])

        adapted = _apply_complexity_adaptation(api_config, "large")

        assert adapted.dev_profile.model == "opus"
        assert adapted.dev_profile.cli == "claude"

    def test_complexity_ignored_with_explicit_profiles(self, tmp_path):
        """No models → complexity is a no-op."""
        config = _make_config(tmp_path)  # classic config, models=None
        adapted = _apply_complexity_adaptation(config, "small")
        assert adapted is config  # unchanged

    def test_small_single_pool_drops_synthesis_only(self, tmp_path):
        """small with pool of 1 → just drops synthesis (no model change)."""
        config = _make_smart_config(tmp_path)
        one_pool = replace(config, review_pool=[config.review_pool[0]])
        adapted = _apply_complexity_adaptation(one_pool, "small")
        assert len(adapted.review_pool) == 1
        assert adapted.synthesis_profile is None

    def test_large_already_strongest_no_change(self, tmp_path):
        """large complexity when dev is already strongest → config unchanged."""
        config = _make_smart_config(tmp_path)
        opus_dev = replace(config.dev_profile, model="opus", cli="claude")
        strong_config = replace(config, dev_profile=opus_dev)
        adapted = _apply_complexity_adaptation(strong_config, "large")
        assert adapted.dev_profile.model == "opus"


class TestComplexityIntegration:
    """Integration tests: complexity flows through run_task with smart config."""

    def test_complexity_stored_in_state(self, tmp_path):
        """Complexity parsed from preflight is stored in CoordinatorState."""
        config = _make_smart_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        preflight_large = """\
```yaml
verdict: PROCEED
complexity: large
reason: "Big change."
criteria_checked: []
```
"""

        def fake_preflight_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            return _make_agent_result(output=preflight_large)

        def fake_dev_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            if profile.name == "synthesis":
                # 3-model config: large keeps 2 reviewers, synthesis runs
                return _make_agent_result(output=APPROVE_REVIEW)
            return _make_agent_result()

        pool_names = [p.name for p in config.review_pool]
        with (
            patch_gate_shell(
                side_effect=_shell_with_gate(workspace),
            ),
            patch(
                "theforge.coordinator.preflight_flow.run_agent", side_effect=fake_preflight_agent
            ),
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=fake_dev_agent),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW, APPROVE_REVIEW], pool_names),
            ),
        ):
            result = run_task(config, task)

        assert result.state.preflight_complexity == "large"

    def test_cached_preflight_large_reuses_complexity_adaptation(self, tmp_path):
        """Cached preflight state should still apply smart-config model adaptation."""
        config = _make_smart_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()
        cached_state = replace(
            CoordinatorState(),
            preflight_verdict="PROCEED",
            preflight_reason="Already classified in sprint preflight.",
            preflight_complexity="large",
            preflight_sufficiency="implementation_ready",
            preflight_work_type="feature",
        )
        cached_state.preflight_cache_snapshot = {
            "worktree_head": "OK",
            "evaluation_base_branch": "main",
            "evaluation_base_branch_head": "OK",
            "story_content_hash": _story_content_hash(task.story_path.read_text(encoding="utf-8")),
        }

        with (
            patch_gate_shell(
                side_effect=_shell_with_gate(workspace),
            ),
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
        ):
            mock_dev.return_value = _make_agent_result(output="Implemented.")
            result = run_task(
                config,
                task,
                cached_preflight_state=cached_state,
                stop_phase=Phase.DEV,
            )

        assert result.success is True
        assert mock_preflight.call_count == 0
        assert mock_dev.call_args.kwargs["profile"].model == "opus"

    def test_complexity_small_skips_synthesis_in_run(self, tmp_path):
        """small complexity causes pool to be reduced to 1 reviewer."""
        config = _make_smart_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        preflight_small = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Tiny fix."
criteria_checked: []
```
"""

        pool_calls: list[list[str]] = []

        def fake_preflight_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            return _make_agent_result(output=preflight_small)

        def fake_run_pool(prompt, profiles, working_dir, session_ids=None, **kwargs):
            pool_calls.append([p.name for p in profiles])
            return _make_pool_result([APPROVE_REVIEW], [profiles[0].name])

        with (
            patch_gate_shell(
                side_effect=_shell_with_gate(workspace),
            ),
            patch(
                "theforge.coordinator.preflight_flow.run_agent", side_effect=fake_preflight_agent
            ),
            patch("theforge.coordinator.dev_phase.run_agent", return_value=_make_agent_result()),
            patch("theforge.coordinator.review_pool.run_agent_pool", side_effect=fake_run_pool),
        ):
            run_task(config, task)

        # Pool should have been called with only 1 reviewer
        assert len(pool_calls) == 1
        assert len(pool_calls[0]) == 1


class TestLargeComplexitySynthesisP1:
    """P1 fix: large complexity must materialize synthesis even for 2-model pool."""

    def test_large_2_model_pool_creates_synthesis(self, tmp_path):
        """large with 2-model config (synthesis=None) → synthesis is created."""
        config = _make_smart_config(tmp_path)
        # Simulate 2-model auto-assign: single reviewer, no synthesis
        two_model = replace(
            config,
            review_pool=[config.review_pool[0]],
            synthesis_profile=None,
        )
        adapted = _apply_complexity_adaptation(two_model, "large")
        assert adapted.synthesis_profile is not None
        assert adapted.synthesis_profile.name == "synthesis"

    def test_large_2_model_synthesis_uses_strongest(self, tmp_path):
        """For large complexity with 2 models, synthesis is set to the strongest model."""
        config = _make_smart_config(tmp_path)
        two_model = replace(
            config,
            review_pool=[config.review_pool[0]],  # opus reviewer
            synthesis_profile=None,
        )
        adapted = _apply_complexity_adaptation(two_model, "large")
        assert adapted.synthesis_profile is not None
        # Strongest is opus (cap=10)
        assert adapted.synthesis_profile.model == "opus"
        assert adapted.synthesis_profile.cli == "claude"

    def test_large_3_model_pool_synthesis_preserved(self, tmp_path):
        """large with existing synthesis → synthesis is preserved (not recreated)."""
        config = _make_smart_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "large")
        assert adapted.synthesis_profile is not None
        assert adapted.synthesis_profile.model == "opus"


class TestComplexityParsedForAllPreflightsP1:
    """P1 fix: complexity parsed on all successful preflights, not just smart config."""

    def test_complexity_stored_for_classic_config(self, tmp_path):
        """Complexity stored in preflight_complexity even when models is None."""
        config = _make_config(tmp_path)  # classic config, no models
        assert config.models is None
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        preflight_medium = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Multi-file feature."
criteria_checked: []
```
"""

        with (
            patch_gate_shell(
                side_effect=_shell_with_gate(workspace),
            ),
            patch(
                "theforge.coordinator.preflight_flow.run_agent",
                return_value=_make_agent_result(output=preflight_medium),
            ),
            patch("theforge.coordinator.dev_phase.run_agent", return_value=_make_agent_result()),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], [config.review_pool[0].name]),
            ),
        ):
            result = run_task(config, task)

        assert result.state.preflight_complexity == "medium"

    def test_classic_config_complexity_does_not_swap_models(self, tmp_path):
        """Classic config: complexity is parsed but does NOT change model assignments."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        preflight_large = """\
```yaml
verdict: PROCEED
complexity: large
reason: "Big refactor."
criteria_checked: []
```
"""

        pool_profiles_used: list[str] = []

        def fake_run_pool(prompt, profiles, working_dir, session_ids=None, **kwargs):
            pool_profiles_used.extend(p.name for p in profiles)
            return _make_pool_result([APPROVE_REVIEW], [profiles[0].name])

        with (
            patch_gate_shell(
                side_effect=_shell_with_gate(workspace),
            ),
            patch(
                "theforge.coordinator.preflight_flow.run_agent",
                return_value=_make_agent_result(output=preflight_large),
            ),
            patch("theforge.coordinator.dev_phase.run_agent", return_value=_make_agent_result()),
            patch("theforge.coordinator.review_pool.run_agent_pool", side_effect=fake_run_pool),
        ):
            result = run_task(config, task)

        # Complexity captured
        assert result.state.preflight_complexity == "large"
        # But dev model unchanged (classic config not swapped)
        assert result.state.dev_results[0].success  # dev ran normally
        # Pool called with original single reviewer (no synthesis was added)
        assert len(pool_profiles_used) == 1


def test_preflight_missing_likely_files_preserved_as_unknown(tmp_path):
    from theforge.sprint.collision import build_bundle_hint

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    with (
        patch_gate_shell() as mock_shell,
        patch("theforge.coordinator.dev_phase.run_agent") as mock_agent,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="""```yaml
verdict: PROCEED
reason: \"Need planning before implementation.\"
complexity: small
work_type: bug
bundle_candidate: true
criteria_checked: []
```\n""",
            cost_usd=0.05,
            profile_name="review",
        )
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

    assert result.success is True
    assert result.state.preflight_likely_files is None

    hint = build_bundle_hint(task, result.state)
    assert hint.likely_files is None


def test_collision_unknown_likely_files_not_treated_as_zero_footprint(tmp_path):
    from theforge.coordinator.state import CoordinatorState
    from theforge.sprint.collision import build_bundle_hint

    task = _make_task(tmp_path)
    unknown_state = CoordinatorState(
        preflight_work_type="bug",
        preflight_complexity="small",
        preflight_bundle_candidate=True,
        preflight_likely_files=None,
    )
    zero_state = CoordinatorState(
        preflight_work_type="bug",
        preflight_complexity="small",
        preflight_bundle_candidate=True,
        preflight_likely_files=[],
    )

    unknown_hint = build_bundle_hint(task, unknown_state)
    zero_hint = build_bundle_hint(task, zero_state)

    assert unknown_hint.likely_files is None
    assert zero_hint.likely_files == ()
