"""Tests for coordinator dev phase: prompt routing and session resume.

Covers: prompt routing (fix vs dev prompt) and session resume/carry-through.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _as_detailed,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    _write_handoff,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.types import HardConventionsConfig
from theforge.coordinator.dev_phase import (
    _current_cycle_p1s_for_dev_prompt,
    _prior_open_p1s_for_dev_prompt,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorState, FindingRecord
from theforge.runners import AgentResult


class TestCoordinatorPromptRouting:
    """Test that the correct prompt builder is called based on retry_reason."""

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_review_changes_routes_to_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """retry_reason='review_changes' → build_fix_prompt() on second dev call."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert mock_fix_prompt.called, "build_fix_prompt should be called on review iteration"
        assert mock_dev_prompt.call_count >= 1, "build_dev_prompt should be called on first run"
        assert mock_fix_prompt.call_args.kwargs["prior_open_p1s"] is None
        classified = mock_fix_prompt.call_args.kwargs["classified_p1s"]
        assert len(classified) == 1
        assert classified[0].description == "Off by one"
        assert result.state.dev_prompt_injected_finding_ids == [[], [classified[0].finding_id]]

    def test_carry_forward_excludes_findings_first_seen_in_current_review_cycle(self):
        """Only unresolved P1s from earlier cycles are injected as carry-forward context."""
        state = CoordinatorState(review_cycle=2)
        older_open = FindingRecord(
            finding_id="older-open",
            cycle_first_seen=1,
            cycle_last_seen=2,
            file="src/older.py",
            line=9,
            severity="P1",
            description="Older unresolved issue",
            reporter="reviewer-a",
            disposition="unresolved",
        )
        current_cycle = FindingRecord(
            finding_id="current-cycle",
            cycle_first_seen=2,
            cycle_last_seen=2,
            file="src/current.py",
            line=11,
            severity="P1",
            description="Current cycle issue",
            reporter="reviewer-b",
            disposition="regression",
        )
        already_fixed = FindingRecord(
            finding_id="fixed-prior",
            cycle_first_seen=1,
            cycle_last_seen=1,
            file="src/fixed.py",
            line=7,
            severity="P1",
            description="Already fixed issue",
            reporter="reviewer-c",
            disposition="fixed",
        )
        state.finding_registry.extend([older_open, current_cycle, already_fixed])

        assert _prior_open_p1s_for_dev_prompt(state) == [older_open]
        assert _current_cycle_p1s_for_dev_prompt(state) == [older_open, current_cycle]

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_prompt_injection_audit_tracks_fix_prompt_finding_ids(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_fix_prompt,
        tmp_path,
    ):
        """Fix-prompt audit records all finding IDs passed to the dev agent."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_fix_prompt.return_value = "fix prompt"

        cycle_one_review = REQUEST_CHANGES_REVIEW
        cycle_two_review = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Still needs work"
findings:
  - severity: P1
    file: src/foo.py
    line: 10
    observed: "Off by one"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix the boundary condition"
  - severity: P1
    file: src/bar.py
    line: 20
    observed: "Missing validation"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Validate the input"
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""
        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=cycle_one_review, profile_name="review"
                    )
                ]
            return [
                _make_agent_result(success=True, output=cycle_two_review, profile_name="review")
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is False
        injected_ids = result.state.dev_prompt_injected_finding_ids
        assert injected_ids[0] == []
        assert len(injected_ids[1]) == 1
        assert injected_ids[1][0] == result.state.finding_registry[0].finding_id

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_gate_fail_routes_to_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """retry_reason='gate_fail' → build_dev_prompt() on retry, not build_fix_prompt()."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_iteration == 2
        assert not mock_fix_prompt.called, "build_fix_prompt must NOT be called on gate_fail retry"
        assert mock_dev_prompt.call_count == 2, "build_dev_prompt should be called both times"

    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_convention_violation_retry_injects_actionable_findings_into_dev_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        mock_check_conventions,
        tmp_path,
    ):
        """Blocking convention violations are propagated into the next dev retry prompt."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        violation = type(
            "Violation",
            (),
            {
                "rule": "no_scratch_files",
                "file": "test_resolution_commentary.py",
                "detail": "Unexpected root-level scratch file",
                "blocking": True,
            },
        )()

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_check_conventions.side_effect = [
            ([violation], [violation]),
            ([], []),
        ]
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert mock_dev_prompt.call_count == 2
        retry_findings = mock_dev_prompt.call_args_list[1].kwargs["review_findings"]
        assert retry_findings is not None
        assert "## Blocking Convention Violations" in retry_findings
        assert "[P1]" in retry_findings
        assert "test_resolution_commentary.py" in retry_findings
        assert "no_scratch_files" in retry_findings
        assert "Resolve the [no_scratch_files] convention violation" in retry_findings
        assert not mock_fix_prompt.called

    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_convention_violation_retry_does_not_duplicate_or_leak_stale_findings(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        mock_check_conventions,
        tmp_path,
    ):
        """Retry prompts derive convention findings from the current validate result only."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            retry=RetryPolicy(max_dev_iterations=4, max_review_cycles=2),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        violation = type(
            "Violation",
            (),
            {
                "rule": "no_scratch_files",
                "file": "test_resolution_commentary.py",
                "detail": "Unexpected root-level scratch file",
                "blocking": True,
            },
        )()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "FAIL", "FAIL", "PASS"])
        mock_check_conventions.side_effect = [
            ([violation], [violation]),
            ([violation], [violation]),
            ([], []),
            ([], []),
        ]
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(),
            _make_agent_result(),
            _make_agent_result(),
            _make_agent_result(),
        ]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert mock_dev_prompt.call_count == 4
        first_retry_findings = mock_dev_prompt.call_args_list[1].kwargs["review_findings"]
        second_retry_findings = mock_dev_prompt.call_args_list[2].kwargs["review_findings"]
        third_retry_findings = mock_dev_prompt.call_args_list[3].kwargs["review_findings"]
        assert first_retry_findings is not None
        assert first_retry_findings.count("## Blocking Convention Violations") == 1
        assert second_retry_findings is not None
        assert second_retry_findings.count("## Blocking Convention Violations") == 1
        assert third_retry_findings is None
        assert not mock_fix_prompt.called

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_prompt_injection_audit_tracks_carry_forward_and_current_cycle_findings(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """Audit trail records both carry-forward and current-cycle P1 finding IDs."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        first_review = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Need fixes"
findings:
  - id: carry-forward-p1
    severity: P1
    file: src/older.py
    line: 9
    observed: "Older unresolved issue"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix older issue"
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""
        second_review = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Still needs fixes"
findings:
  - id: carry-forward-p1
    severity: P1
    file: src/older.py
    line: 9
    observed: "Older unresolved issue"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix older issue"
  - id: current-cycle-p1
    severity: P1
    file: src/current.py
    line: 11
    observed: "Current cycle issue"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix current issue"
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(success=True, output=first_review, profile_name="review")
                ]
            if call_count["pool"] == 2:
                return [
                    _make_agent_result(success=True, output=second_review, profile_name="review")
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        config = ForgeConfig(
            project=config.project,
            project_root=config.project_root,
            workspace=config.workspace,
            validation=config.validation,
            dev_profile=config.dev_profile,
            preflight_profile=config.preflight_profile,
            review_pool=[
                DEFAULT_REVIEW_PROFILE.__class__(
                    **{**DEFAULT_REVIEW_PROFILE.__dict__, "budget_usd": 5.0}
                )
            ],
            synthesis_profile=config.synthesis_profile,
            retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=3),
            log=config.log,
        )

        result = run_task(config, task)

        assert result.success is True
        injected = result.state.dev_prompt_injected_finding_ids
        assert injected[0] == []
        assert len(injected[1]) == 1
        assert len(injected[2]) == 2
        assert set(injected[1]).issubset(set(injected[2]))

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_phase._human_review")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_extend_routes_to_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_human_review,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """retry_reason='extend' → build_fix_prompt() when human extends after APPROVE."""
        config = _make_config(tmp_path)
        # Disable post-APPROVE P2 cleanup so this test exercises the original
        # extend-after-APPROVE+P2 path without an intervening cleanup pass.
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, p2_cleanup_enabled=False)
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result(), _make_agent_result()]
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        extend_review = """\
```yaml
verdict: APPROVE
summary: "Acceptable but please also add logging."
findings:
  - severity: P2
    file: src/foo.py
    line: 5
    observed: "Missing log statement"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Add logger.info(...)"
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
ac_verification:
  - criterion: "Implementation satisfies the spec"
    status: VERIFIED
    evidence: "diff hunks present and tests cover the failure mode (test fixture default)"
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(success=True, output=extend_review, profile_name="review")
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        # First call: human extends; second call: human approves
        human_call = {"n": 0}

        def human_review_side_effect(*args, **kwargs):
            human_call["n"] += 1
            if human_call["n"] == 1:
                return ("extend", "")
            return ("approve", "")

        mock_human_review.side_effect = human_review_side_effect

        run_task(config, task, interactive=True)

        assert mock_fix_prompt.called, "build_fix_prompt should be called after extend"

    @patch("theforge.coordinator.dev_phase.build_fix_prompt", wraps=None)
    @patch("theforge.coordinator.dev_phase.build_dev_prompt", wraps=None)
    @patch("theforge.coordinator.review_phase._human_review")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_exhausted_cycles_extend_zero_findings_uses_fix_prompt(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_review_agent,
        mock_human_review,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """Exhausted-cycles extend with empty findings still routes to build_fix_prompt.

        REQUEST_CHANGES with no explicit findings can happen (e.g., reviewer says
        'rewrite the approach' without listing individual findings). When cycles are
        exhausted and the human extends, last_review_findings must be populated
        unconditionally so the next DEV iteration uses fix-prompt, not dev-prompt.
        """
        # max_review_cycles=1 so the first REQUEST_CHANGES exhausts the budget
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
            retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=1),
            log=LogConfig(enabled=False),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_dev_prompt.return_value = "full dev prompt"
        mock_fix_prompt.return_value = "fix prompt"

        # First pooled response is malformed (no P1), which should trigger the
        # per-reviewer retry path. That retry returns a valid REQUEST_CHANGES
        # review so the exhausted-cycle extend still exercises fix-prompt routing.
        # This keeps the test hermetic across both review entry points.
        empty_findings_rc = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Please rethink the approach entirely."
findings: []
story_compliance:
  matches_spec: false
test_coverage:
  adequate: false
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=empty_findings_rc, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect
        mock_review_agent.return_value = _make_agent_result(
            success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
        )

        # First call: human extends (cycles exhausted); second call: human approves
        human_call = {"n": 0}

        def human_review_side_effect(*args, **kwargs):
            human_call["n"] += 1
            if human_call["n"] == 1:
                return ("extend", "")
            return ("approve", "")

        mock_human_review.side_effect = human_review_side_effect

        run_task(config, task, interactive=True)

        assert mock_fix_prompt.called, (
            "build_fix_prompt must be called after exhausted-cycles extend "
            "even when findings list is empty"
        )


class TestCoordinatorSessionResume:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_session_carried_on_timeout(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        timeout_result = AgentResult(
            success=False,
            output="TIMEOUT",
            session_id="sess-timeout",
            cost_usd=0.10,
            exit_code=-9,
            raw={},
            profile_name="dev",
        )
        resumed_result = _make_agent_result(
            success=True, output="Done.", session_id="sess-resumed", profile_name="dev"
        )
        dev_session_ids: list[str | None] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_session_ids.append(session_id)
            if len(dev_session_ids) == 1:
                return timeout_result
            return resumed_result

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert dev_session_ids == [None, "sess-timeout"]
        assert result.state.dev_session_id == "sess-resumed"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_session_carried_across_review_cycles(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dev_session_ids: list[str | None] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_session_ids.append(session_id)
            if len(dev_session_ids) == 1:
                return _make_agent_result(
                    success=True,
                    output="Implemented.",
                    session_id="dev-sess-1",
                    profile_name="dev",
                )
            return _make_agent_result(
                success=True, output="Fixed.", session_id="dev-sess-2", profile_name="dev"
            )

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert dev_session_ids == [None, "dev-sess-1"]
        assert result.state.dev_session_id == "dev-sess-2"

    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_timeout_resume_uses_short_prompt(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_dev_prompt, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "full dev prompt"
        dev_prompts: list[str] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_prompts.append(prompt)
            if len(dev_prompts) == 1:
                return AgentResult(
                    success=False,
                    output="TIMEOUT",
                    session_id="sess-timeout",
                    cost_usd=0.10,
                    exit_code=-9,
                    raw={},
                    profile_name="dev",
                )
            return _make_agent_result(success=True, output="Done.", profile_name="dev")

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert dev_prompts[0] == "full dev prompt"
        assert "You were cut off by a timeout." in dev_prompts[1]
        assert dev_prompts[1] != "full dev prompt"
        assert result.state.dev_prompt_injected_finding_ids == [[], []]
        assert len(result.state.dev_prompt_injected_finding_ids) == len(result.state.dev_results)

    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_timeout_resume_dirty_worktree_auto_committed(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_dev_prompt, tmp_path
    ):
        """Timeout on iter 1, success on iter 2, dirty worktree → auto-commit (no retry)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "full dev prompt"
        dev_prompts: list[str] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            dev_prompts.append(prompt)
            if len(dev_prompts) == 1:
                return AgentResult(
                    success=False,
                    output="TIMEOUT",
                    session_id="sess-timeout",
                    cost_usd=0.10,
                    exit_code=-9,
                    raw={},
                    profile_name="dev",
                )
            return _make_agent_result(success=True, output="Done.", profile_name="dev")

        shell_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            shell_cmds.append(cmd)
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/foo.py")
            if "git add" in cmd:
                return (True, "")
            if "git commit" in cmd:
                return (True, "")

            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = fake_run_agent
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("theforge.coordinator.validate_phase.subprocess.run"):
            result = run_task(config, task)

        assert result.success is True
        # Timeout iter → gate passes → auto-commit → REVIEW → DONE
        assert mock_dev_prompt.call_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_reviewer_sessions_accumulate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
        ]

        pool_calls = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_calls["n"] += 1
            if pool_calls["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.reviewer_session_ids == {"review": "review-sess-2"}

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_reviewer_sessions_passed_to_pool(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_session_ids: list[list[str | None]] = []

        mock_shell.side_effect = _shell_with_gate(workspace, ["PASS", "PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
        ]

        def pool_side_effect(**kwargs):
            captured_session_ids.append(list(kwargs["session_ids"]))
            if len(captured_session_ids) == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        session_id="review-sess-1",
                        profile_name="review",
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id="review-sess-2",
                    profile_name="review",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert captured_session_ids == [[None], ["review-sess-1"]]
