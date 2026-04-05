"""Tests for plan phase, persistent P1 descriptions, cycle history, and already-done override."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED_MEDIUM,
    PREFLIGHT_PROCEED_SMALL,
    REQUEST_CHANGES_REVIEW,
    _make_agent_result,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import (
    _persistent_p1_descriptions,
)
from theforge.coordinator.state import Phase
from theforge.review import ReviewFinding

# ── _make_smart_config for escalation-note tests (2-model config) ─────


def _make_smart_config(
    tmp_path: Path,
    models: list[str] | None = None,
    max_review_cycles: int = 3,
) -> ForgeConfig:
    """Create a ForgeConfig with smart_config_models set (claude/sonnet as dev)."""
    if models is None:
        models = ["claude/sonnet", "claude/opus"]
    dev_profile = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=30.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )
    review_profile = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=10.0,
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
        dev_profile=dev_profile,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[review_profile],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=max_review_cycles),
        smart_config_models=models,
    )


# ── Helper for persistent P1 review ──────────────────────────────────


def _make_review_finding(
    severity: str = "P1",
    file: str = "src/cli.py",
    description: str = "cli.py never wires gate_override into TaskStory",
) -> ReviewFinding:
    return ReviewFinding(
        severity=severity, file=file, line=None, description=description, suggestion=None
    )


_PERSISTENT_P1_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Persistent issue found."
findings:
  - severity: P1
    file: src/cli.py
    line: 42
    description: "cli.py never wires gate_override into TaskStory"
    suggestion: "Wire it"
story_compliance:
  matches_spec: false
  mismatches:
    - "Missing wiring"
test_coverage:
  adequate: false
  gaps:
    - "No test for gate_override"
```
"""


# ── PLAN phase tests ──────────────────────────────────────────────────


class TestPlanPhase:
    """Tests for the PLAN phase (implementation planning between PREFLIGHT and DEV)."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_runs_for_medium_complexity(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """PLAN phase runs when preflight complexity is medium."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Implementation Plan\n\nStep 1: implement feature.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # PLAN + DEV = 2 run_agent calls (preflight now mocked separately)
        assert mock_agent.call_count == 2
        # plan_output is stored on state
        assert result.state.plan_output is not None
        assert "Implementation Plan" in result.state.plan_output
        # .forge/plan.md written to workspace
        assert (workspace / ".forge" / "plan.md").exists()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_skipped_for_small_complexity(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """PLAN phase is skipped when preflight complexity is small."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_SMALL, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # DEV only (no PLAN, preflight mocked separately) = 1 run_agent call
        assert mock_agent.call_count == 1
        assert result.state.plan_output is None
        assert not (workspace / ".forge" / "plan.md").exists()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_skipped_when_disabled(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """PLAN phase is skipped when plan.enabled is False."""
        config = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
            plan=PlanConfig(enabled=False),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)
        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        # DEV only (plan disabled, preflight mocked separately) = 1 run_agent call
        assert mock_agent.call_count == 1
        assert result.state.plan_output is None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_failure_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """When PLAN agent fails, the run escalates (does not proceed blind)."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        plan_result = _make_agent_result(
            success=False,
            output="Error: plan agent crashed.",
            cost_usd=0.01,
        )

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect

        result = run_task(config, task)

        # PLAN failure should escalate, not proceed to DEV
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "PLAN phase failed" in result.message
        # plan_output is None (plan failed, no output stored)
        assert result.state.plan_output is None
        # plan result is stored
        assert result.state.plan_results
        assert result.state.plan_results[-1].success is False
        # DEV should NOT have run (only plan = 1 agent call; preflight mocked separately)
        assert mock_agent.call_count == 1
        # Review pool should NOT have run
        assert mock_pool.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_cost_included_in_total_cost(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """total_cost includes plan cost."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Implementation Plan\n\nStep 1.",
            cost_usd=0.20,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review"),
        ]

        result = run_task(config, task)

        assert result.success is True
        state = result.state
        assert state.total_plan_cost == pytest.approx(0.20)
        # total_cost = dev(0.50) + review(0.50) + preflight(0.05) + plan(0.20) + story_validation
        assert state.total_cost == pytest.approx(
            0.50 + 0.50 + 0.05 + 0.20 + state.total_story_validation_cost
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_not_rerun_on_dev_retry(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """On DEV retry (review sends REQUEST_CHANGES), PLAN does not re-run."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        plan_result = _make_agent_result(
            success=True,
            output="# Implementation Plan\n\nStep 1.",
            cost_usd=0.10,
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [plan_result, dev_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect
        # First review cycle: REQUEST_CHANGES; second: APPROVE
        mock_pool.side_effect = [
            [_make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="r")],
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r")],
        ]

        result = run_task(config, task)

        assert result.success is True
        # PLAN(1) + DEV(1) + DEV-retry(1) = 3 calls; no second PLAN (preflight mocked separately)
        assert mock_agent.call_count == 3
        # plan_output is still the original plan (from first run)
        assert result.state.plan_output is not None
        assert "Implementation Plan" in result.state.plan_output

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_copies_file_and_skips_agent(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """--plan copies the file into worktree, sets plan_output, and skips the PLAN agent."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        plan_content = "# Implementation Plan\n\nDo the thing."
        plan_file = tmp_path / "my_plan.md"
        plan_file.write_text(plan_content, encoding="utf-8")

        preflight_result = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)

        call_idx = {"n": 0}
        mock_preflight.return_value = preflight_result
        results = [dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, plan_path=plan_file)

        assert result.success is True
        # DEV only — no plan agent (1 call; preflight mocked separately)
        assert mock_agent.call_count == 1
        assert result.state.plan_output == plan_content
        assert result.state.plan_results == []
        assert (workspace / ".forge" / "plan.md").read_text(encoding="utf-8") == plan_content

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_missing_file_aborts_before_workspace(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """--plan with missing file aborts before WORKSPACE runs."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)

        result = run_task(config, task, plan_path=tmp_path / "nonexistent.md")

        assert result.success is False
        assert result.phase == Phase.INIT
        assert "does not exist" in result.message
        # No agents or shell commands ran
        assert mock_agent.call_count == 0
        assert mock_shell.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_unreadable_file_aborts_before_workspace(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """--plan with an existing but unreadable file aborts before WORKSPACE runs."""
        import os

        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)

        plan_file = tmp_path / "unreadable_plan.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        os.chmod(plan_file, 0o000)

        try:
            result = run_task(config, task, plan_path=plan_file)
        finally:
            os.chmod(plan_file, 0o644)

        assert result.success is False
        assert result.phase == Phase.INIT
        assert "not readable" in result.message
        assert mock_agent.call_count == 0
        assert mock_shell.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_non_utf8_file_aborts_before_workspace(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """--plan with a file that exists but is not valid UTF-8 aborts before WORKSPACE runs."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)

        plan_file = tmp_path / "binary_plan.md"
        plan_file.write_bytes(b"\xff\xfe invalid utf-8 \x80\x81")

        result = run_task(config, task, plan_path=plan_file)

        assert result.success is False
        assert result.phase == Phase.INIT
        assert "not readable" in result.message
        assert mock_agent.call_count == 0
        assert mock_shell.call_count == 0


class TestPersistentP1Descriptions:
    """Tests for _persistent_p1_descriptions() helper."""

    def test_returns_matched_descriptions(self):
        """Returns current P1 description strings that match previous P1s."""
        curr = [_make_review_finding(description="null check missing in foo.py")]
        prev = [_make_review_finding(description="null check missing in foo.py")]
        result = _persistent_p1_descriptions(curr, prev)
        assert result == ["null check missing in foo.py"]

    def test_returns_empty_when_no_match(self):
        """Returns empty list when no current P1 matches any previous P1."""
        curr = [_make_review_finding(file="src/foo.py", description="Off by one error")]
        prev = [_make_review_finding(file="src/bar.py", description="Missing validation")]
        result = _persistent_p1_descriptions(curr, prev)
        assert result == []

    def test_returns_match_for_same_file_cascading_p1(self):
        """Different descriptions in the same file are reported as persistent."""
        curr = [
            _make_review_finding(file="src/plan_flow.py", description="skip branch drops abandon")
        ]
        prev = [
            _make_review_finding(
                file="src/plan_flow.py",
                description="skip branch wrong for refactor",
            )
        ]
        result = _persistent_p1_descriptions(curr, prev)
        assert result == ["skip branch drops abandon"]

    def test_returns_empty_when_no_current_p1s(self):
        """Returns empty list when there are no current P1 findings."""
        curr = [_make_review_finding(severity="P2", description="style issue")]
        prev = [_make_review_finding(description="null check missing")]
        result = _persistent_p1_descriptions(curr, prev)
        assert result == []

    def test_returns_empty_when_no_previous_p1s(self):
        """Returns empty list when there are no previous P1 findings."""
        curr = [_make_review_finding(description="null check missing")]
        prev = [_make_review_finding(severity="P2", description="null check missing")]
        result = _persistent_p1_descriptions(curr, prev)
        assert result == []

    def test_substring_containment_matches(self):
        """Substring containment triggers a match."""
        curr = [_make_review_finding(description="gate_override never wired")]
        prev = [_make_review_finding(description="gate_override never wired into TaskStory")]
        result = _persistent_p1_descriptions(curr, prev)
        assert "gate_override never wired" in result

    def test_token_overlap_matches(self):
        """>=60% token overlap triggers a match."""
        curr = [_make_review_finding(description="missing batch configuration")]
        prev = [_make_review_finding(description="batch configuration is missing")]
        result = _persistent_p1_descriptions(curr, prev)
        assert len(result) == 1

    def test_descriptions_truncated_at_200_chars(self):
        """Returns descriptions truncated to 200 characters."""
        long_desc = "x" * 300
        curr = [_make_review_finding(description=long_desc)]
        prev = [_make_review_finding(description=long_desc)]
        result = _persistent_p1_descriptions(curr, prev)
        assert len(result) == 1
        assert len(result[0]) <= 200

    def test_multiple_matches_returns_all(self):
        """Multiple matching P1s are all returned."""
        curr = [
            _make_review_finding(description="alpha issue"),
            _make_review_finding(description="beta issue"),
        ]
        prev = [
            _make_review_finding(description="alpha issue"),
            _make_review_finding(description="beta issue"),
        ]
        result = _persistent_p1_descriptions(curr, prev)
        assert len(result) == 2


class TestCycleHistoryAccumulation:
    """Tests for CycleHistory accumulation in _append_cycle_history."""

    def test_append_cycle_history_adds_entry(self):
        """_append_cycle_history appends a CycleHistory entry to state."""
        from theforge.coordinator.completion import _append_cycle_history
        from theforge.coordinator.state import CoordinatorState
        from theforge.review import ReviewFinding, ReviewResult

        state = CoordinatorState()
        parsed_review = ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="Found issues",
            findings=[
                ReviewFinding(
                    severity="P1",
                    file="src/foo.py",
                    line=None,
                    description="Null check missing",
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
        _append_cycle_history(state, parsed_review)

        assert len(state.cycle_history) == 1
        entry = state.cycle_history[0]
        assert entry.cycle == 1
        assert entry.verdict == "REQUEST_CHANGES"
        assert entry.summary == "Found issues"
        assert entry.p1_findings == ["Null check missing"]

    def test_cycle_history_capped_at_3(self):
        """History is capped at 3 entries; oldest is dropped."""
        from theforge.coordinator.completion import _append_cycle_history
        from theforge.coordinator.state import CoordinatorState, CycleHistory
        from theforge.review import ReviewResult

        state = CoordinatorState()
        # Pre-populate with 3 entries (also set total counter to match)
        state.cycle_history = [
            CycleHistory(cycle=1, verdict="REQUEST_CHANGES", summary="s1", p1_findings=["a"]),
            CycleHistory(cycle=2, verdict="REQUEST_CHANGES", summary="s2", p1_findings=["b"]),
            CycleHistory(cycle=3, verdict="REQUEST_CHANGES", summary="s3", p1_findings=["c"]),
        ]
        state.cycle_history_total = 3
        parsed_review = ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="fourth",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        _append_cycle_history(state, parsed_review)

        assert len(state.cycle_history) == 3
        assert state.cycle_history[0].summary == "s2"  # oldest (s1) dropped
        assert state.cycle_history[-1].summary == "fourth"
        assert state.cycle_history[-1].cycle == 4  # monotonically increasing

    def test_cycle_numbers_monotonic_after_cap(self):
        """Cycle numbers remain monotonically increasing even after trimming."""
        from theforge.coordinator.completion import _append_cycle_history
        from theforge.coordinator.state import CoordinatorState
        from theforge.review import ReviewResult

        state = CoordinatorState()
        parsed_review = ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="s",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        # Append 5 cycles — cap fires after 3, but numbers must never repeat
        for _ in range(5):
            _append_cycle_history(state, parsed_review)

        assert len(state.cycle_history) == 3
        cycles = [h.cycle for h in state.cycle_history]
        assert cycles == [3, 4, 5]  # oldest trimmed, no duplicates

    def test_cycle_numbers_monotonically_increase(self):
        """Cycle numbers use a counter independent of list length."""
        from theforge.coordinator.completion import _append_cycle_history
        from theforge.coordinator.state import CoordinatorState
        from theforge.review import ReviewResult

        state = CoordinatorState()
        parsed_review = ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="s",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        _append_cycle_history(state, parsed_review)
        _append_cycle_history(state, parsed_review)
        assert state.cycle_history[0].cycle == 1
        assert state.cycle_history[1].cycle == 2

    def test_p1_descriptions_truncated(self):
        """P1 finding descriptions in history are truncated to 200 chars."""
        from theforge.coordinator.completion import _append_cycle_history
        from theforge.coordinator.state import CoordinatorState
        from theforge.review import ReviewFinding, ReviewResult

        state = CoordinatorState()
        long_desc = "z" * 300
        parsed_review = ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="s",
            findings=[
                ReviewFinding(
                    severity="P1",
                    file="src/foo.py",
                    line=None,
                    description=long_desc,
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
        _append_cycle_history(state, parsed_review)
        assert len(state.cycle_history[0].p1_findings[0]) <= 200
