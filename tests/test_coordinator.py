"""Tests for the coordinator state machine.

Uses mocked runner to test all state transitions without real agent calls.
"""

import datetime
import time as _time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    NotificationConfig,
    NtfyConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import (
    Phase,
    _apply_complexity_adaptation,
    _fmt_duration,
    _is_remote_mode,
    _is_stale_worktree,
    _ntfy_poll_reply,
    _ntfy_reply_url,
    _parse_preflight_complexity,
    _remove_worktree,
    generate_audit_log,
    run_from_review,
    run_review_only,
    run_task,
)
from theforge.runner import AgentResult, LogLevel
from theforge.task import TaskSpec

# ── Fixtures ─────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config pointing at tmp_path (single reviewer, no synthesis)."""
    return ForgeConfig(
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
    )


def _make_pool_config(
    tmp_path: Path, profiles: list[ModelProfile], synthesis: ModelProfile
) -> ForgeConfig:
    """Create a test config with multi-model review pool."""
    return ForgeConfig(
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
        review_pool=profiles,
        synthesis_profile=synthesis,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_task(tmp_path: Path) -> TaskSpec:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskSpec(
        name="Test Task",
        spec_path=spec,
        slug="test-task",
        file_scope=["src/"],
    )


def _make_agent_result(
    success: bool = True,
    output: str = "Done.",
    session_id: str | None = "sess-1",
    cost_usd: float = 0.50,
    profile_name: str = "",
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=session_id,
        cost_usd=cost_usd,
        exit_code=0 if success else 1,
        raw={},
        profile_name=profile_name,
    )


def _make_pool_result(
    outputs: list[str],
    profile_names: list[str],
    success: bool = True,
    cost_usd: float = 0.20,
) -> list[AgentResult]:
    """Build a list of AgentResults as if returned by run_agent_pool."""
    return [
        AgentResult(
            success=success,
            output=out,
            session_id=None,
            cost_usd=cost_usd,
            exit_code=0 if success else 1,
            raw={},
            profile_name=name,
        )
        for out, name in zip(outputs, profile_names)
    ]


def _write_handoff(workspace: Path, decision: str = "PASS") -> None:
    """Write a minimal handoff.yaml in the workspace."""
    handoff = {
        "gate_decision": decision,
        "validation": {"make_fmt": {"status": "PASS"}},
        "scope_completed": ["test item"],
    }
    (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")


# Stale-worktree detection commands need specific responses so that pre-created
# workspaces in existing tests are treated as "fresh" (reused rather than removed).
_RECENT_COMMIT_TS = str(int(_time.time()) - 60)  # 1 minute ago


def _handle_stale_check_cmd(cmd: str) -> tuple[bool, str] | None:
    """Return a mock response for stale-worktree detection git commands, or None if not matched."""
    if "rev-parse --abbrev-ref HEAD" in cmd:
        return (True, "forge/test-task")
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "abc123 a recent commit")
    if "--format=%ct" in cmd:
        return (True, _RECENT_COMMIT_TS)
    return None


def _shell_with_gate(workspace: Path, decisions: list[str] | str = "PASS"):
    """Create a _run_shell side_effect that writes handoff.yaml during gate execution.

    With the stale-handoff fix, _run_gate() deletes handoff before running the gate
    command, so tests must produce handoff *during* (not before) gate execution.

    Also handles stale-worktree detection commands by returning a "fresh" worktree
    (recent commit, commits ahead of base) so pre-created workspaces are reused.
    """
    if isinstance(decisions, str):
        decisions_list = [decisions] * 20
    else:
        decisions_list = list(decisions)
    gate_idx = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if "gate" in cmd:
            d = decisions_list[min(gate_idx["n"], len(decisions_list) - 1)]
            gate_idx["n"] += 1
            _write_handoff(Path(cwd), d)
            return (True, "OK")
        if "git status --porcelain" in cmd:
            return (True, "")  # clean worktree
        stale_resp = _handle_stale_check_cmd(cmd)
        if stale_resp is not None:
            return stale_resp
        return (True, "OK")

    return side_effect


APPROVE_REVIEW = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
spec_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""

REQUEST_CHANGES_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Bug found."
findings:
  - severity: P1
    file: src/foo.py
    line: 10
    description: "Off by one"
    suggestion: "Fix it"
spec_compliance:
  matches_spec: false
  mismatches:
    - "Missing batch config"
test_coverage:
  adequate: false
  gaps:
    - "No edge case test"
```
"""

PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Spec requirements are not yet implemented."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

PREFLIGHT_ALREADY_DONE = """\
```yaml
verdict: ALREADY_DONE
reason: "All acceptance criteria are already satisfied."
criteria_checked:
  - criterion: "Feature X"
    satisfied: true
    evidence: "Implemented in coordinator.py:42"
```
"""

PREFLIGHT_BLOCKED = """\
```yaml
verdict: BLOCKED
reason: "Spec references removed_function() which no longer exists."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "removed_function() was deleted in a prior commit"
```
"""


_PREFLIGHT_RESULT = _make_agent_result(
    success=True, output=PREFLIGHT_PROCEED, cost_usd=0.05, profile_name="review"
)


def _preflight_then(*dev_results: AgentResult):
    """Preflight PROCEED on first call, then dev_results."""
    preflight_result = _PREFLIGHT_RESULT
    results = [preflight_result, *dev_results]
    call_idx = {"n": 0}

    def side_effect(**kwargs):
        idx = min(call_idx["n"], len(results) - 1)
        call_idx["n"] += 1
        return results[idx]

    return side_effect


# ── Tests ────────────────────────────────────────────────────────────


class TestCoordinatorHappyPath:
    """Test the golden path: dev succeeds, gate passes, review approves."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_single_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        assert result.state.dev_iteration == 1
        assert len(result.state.dev_results) == 1
        assert len(result.state.review_results) == 1


class TestCoordinatorGateFailRetry:
    """Test that gate failure retries the dev agent."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_fail_then_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.dev_iteration == 2  # needed a retry


class TestCoordinatorReviewRequestChanges:
    """Test that review REQUEST_CHANGES loops back to dev."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_review_then_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

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
        assert result.state.review_cycle == 2


class TestCoordinatorEscalation:
    """Test that exhausting retries escalates to human."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_exhaustion(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "FAIL")
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 2  # hit max

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_review_exhaustion(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()


class TestCoordinatorSchemaErrorOverride:
    """Test that APPROVE with schema errors triggers reviewer retry (not a full dev cycle)."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_approve_with_schema_errors_triggers_retry(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        # Review YAML that says APPROVE but is missing required fields
        malformed_approve = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=malformed_approve, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        # Schema error triggers reviewer retry (not a dev cycle increment)
        assert result.state.review_cycle == 1
        # Parse retry was tracked in cycle metadata
        assert result.state.review_cycle_metadata[0].parse_retries == 1


class TestCoordinatorCostTracking:
    """Test that both dev and review costs are tracked."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_total_cost_includes_review(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.75,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.90,
            exit_code=0,
            raw={},
            profile_name="review",
        )

        mock_agent.side_effect = _preflight_then(dev_result)
        mock_pool.return_value = [review_result]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.total_dev_cost == 0.75
        assert result.state.total_review_cost == 0.90
        # total_cost includes preflight ($0.05) + dev + review
        assert result.state.total_cost == pytest.approx(0.05 + 0.75 + 0.90)


class TestCoordinatorBudgetEnforcement:
    """Test that budget limits are enforced for dev and review agents."""

    def _make_budget_config(
        self, tmp_path: Path, dev_budget: float, review_budget: float
    ) -> ForgeConfig:
        """Create a config with tight budgets for testing."""
        dev_profile = ModelProfile(
            name=DEFAULT_DEV_PROFILE.name,
            cli=DEFAULT_DEV_PROFILE.cli,
            model=DEFAULT_DEV_PROFILE.model,
            budget_usd=dev_budget,
            timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
        )
        review_profile = ModelProfile(
            name=DEFAULT_REVIEW_PROFILE.name,
            cli=DEFAULT_REVIEW_PROFILE.cli,
            model=DEFAULT_REVIEW_PROFILE.model,
            budget_usd=review_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
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
            retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=3),
        )

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_dev_budget_exceeded_first_call(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Dev agent exceeds budget on first call → ESCALATE with budget error."""
        config = self._make_budget_config(tmp_path, dev_budget=0.40, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")

        # Agent costs $0.50, budget is $0.40 → immediate escalation
        expensive_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_agent.side_effect = _preflight_then(expensive_result)

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "0.5000" in result.message
        assert "0.4000" in result.message
        # Only one dev invocation — escalated before retry
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_dev_budget_exceeded_on_retry(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Dev agent exceeds budget on second call (retry) → ESCALATE."""
        config = self._make_budget_config(tmp_path, dev_budget=0.50, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])

        retry_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_agent.side_effect = _preflight_then(retry_result)

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        # Two dev invocations: $0.30 + $0.30 = $0.60 > $0.50
        assert len(result.state.dev_results) == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_review_budget_exceeded(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Review agent exceeds per-profile budget → ESCALATE."""
        config = self._make_budget_config(tmp_path, dev_budget=2.00, review_budget=0.40)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.10,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        # Profile name must match pool entry name for per-profile enforcement
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="review",
        )

        mock_agent.side_effect = _preflight_then(dev_result)
        mock_pool.return_value = [review_result]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "0.5000" in result.message
        assert "0.4000" in result.message
        assert len(result.state.review_agent_results) == 1


class TestCoordinatorStaleHandoff:
    """Test that stale handoff.yaml is deleted before running the gate."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_stale_handoff_not_reused(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """A PASS from a prior gate run must not leak through on gate failure."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Pre-plant a stale PASS handoff from a prior run
        _write_handoff(workspace, "PASS")

        call_count = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            call_count["n"] += 1
            if "mkdir" in cmd:
                return (True, "OK")
            # Gate command fails (e.g. tests fail)
            if "gate" in cmd.lower() or "pytest" in cmd.lower():
                return (False, "FAIL: 1 test failed")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Gate failed and stale handoff was deleted → should escalate, not PASS
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Gate" in result.message or "gate" in result.message


class TestCoordinatorStaleHandoffUnlinkFailure:
    """Test that unlink failure on stale handoff is handled gracefully."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_handoff_is_directory(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """If handoff.yaml is a directory, unlink fails → gate error → retry/escalate."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Create handoff.yaml as a directory (pathological case)
        (workspace / "handoff.yaml").mkdir()

        mock_shell.return_value = (True, "OK")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Should mention stale handoff or gate failure
        assert "handoff" in result.message.lower() or "gate" in result.message.lower()


class TestCoordinatorSchemaErrorOnRequestChanges:
    """Test that malformed REQUEST_CHANGES triggers reviewer retry (not a full dev cycle)."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_malformed_request_changes_triggers_retry(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))

        # Malformed REQUEST_CHANGES: has P1 finding but spec_compliance missing fields
        malformed_review = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Needs work"
findings:
  - severity: P1
    file: src/foo.py
    description: "Bug"
    suggestion: "Fix"
test_coverage:
  adequate: false
```
"""
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=malformed_review, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        # Schema error on REQUEST_CHANGES triggers retry — task completes on second attempt
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        # Parse retry was tracked
        assert result.state.review_cycle_metadata[0].parse_retries == 1


class TestCoordinatorWorkspaceFailure:
    """Test that workspace creation failure escalates immediately."""

    @patch("theforge.coordinator._run_shell")
    def test_workspace_creation_fails(self, mock_shell, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        # Workspace creation command fails
        mock_shell.return_value = (False, "fatal: branch already exists")

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "workspace" in result.message.lower() or "Workspace" in result.message


# ── Multi-model review tests ─────────────────────────────────────────


def _make_review_profile(name: str, budget_usd: float = 1.0) -> ModelProfile:
    return ModelProfile(
        name=name,
        cli="claude",
        model="opus",
        budget_usd=budget_usd,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


SYNTHESIS_PROFILE = ModelProfile(
    name="synthesis",
    cli="claude",
    model="opus",
    budget_usd=1.50,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash"),
)


class TestCoordinatorMultiModelReview:
    """Tests for pool of 2+ reviewers with synthesis."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_pool_of_2_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Pool of 2 reviews → synthesis → APPROVE."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent: DEV (first call), SYNTHESIS (second call)
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="synthesis"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="Reviewer 1 output", profile_name="r1"),
            _make_agent_result(success=True, output="Reviewer 2 output", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_pool_of_2_request_changes(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Pool of 2 reviews → synthesis → REQUEST_CHANGES → ESCALATE after max cycles."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent calls: DEV (cycle 1), SYNTHESIS (cycle 1), DEV (cycle 2), SYNTHESIS (cycle 2)
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(
                success=True, output=REQUEST_CHANGES_REVIEW, profile_name="synthesis"
            ),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
            _make_agent_result(
                success=True, output=REQUEST_CHANGES_REVIEW, profile_name="synthesis"
            ),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1 out", profile_name="r1"),
            _make_agent_result(success=True, output="R2 out", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_pool_of_1_skips_synthesis(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Pool of 1 → uses output directly, no synthesis call."""
        # synthesis_profile is set but pool has only 1 entry
        single_profile = _make_review_profile("solo")
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
            review_pool=[single_profile],
            synthesis_profile=None,  # pool of 1 → no synthesis
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="solo"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # run_agent called for PREFLIGHT + DEV (no synthesis for pool of 1)
        assert mock_agent.call_count == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_mixed_success_failure_degrades_to_single_no_synthesis(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """1 of 2 reviewers succeeds → single output used directly (no synthesis)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent: only DEV — no synthesis since we degrade to 1 successful
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=False, output="TIMEOUT", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # PREFLIGHT + DEV called run_agent; synthesis was skipped
        assert mock_agent.call_count == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_all_reviewers_fail_escalates(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """All reviewers fail → ESCALATE."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=False, output="TIMEOUT", profile_name="r1"),
            _make_agent_result(success=False, output="CRASH", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "failed" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_synthesis_failure_escalates(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Synthesis agent failure → ESCALATE (no fallback)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent: DEV then SYNTHESIS (fails)
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=False, output="CRASH", profile_name="synthesis"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="r1"),
            _make_agent_result(success=True, output="R2", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "synthesis" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_per_profile_budget_enforcement(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """One pool profile over budget → ESCALATE."""
        tight_profile = _make_review_profile("tight", budget_usd=0.10)
        normal_profile = _make_review_profile("normal", budget_usd=5.00)
        config = _make_pool_config(tmp_path, [tight_profile, normal_profile], SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        # tight profile costs $0.50 which exceeds its $0.10 budget
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="tight", cost_usd=0.50),
            _make_agent_result(success=True, output="R2", profile_name="normal", cost_usd=0.10),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "tight" in result.message

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_synthesis_budget_enforcement(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Synthesis agent over budget → ESCALATE."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        tight_synthesis = ModelProfile(
            name="synthesis",
            cli="claude",
            model="opus",
            budget_usd=0.10,  # very tight
            timeout_seconds=300,
            allowed_tools=(),
        )
        config = _make_pool_config(tmp_path, profiles, tight_synthesis)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            # synthesis costs $0.50 > budget $0.10
            _make_agent_result(
                success=True,
                output=APPROVE_REVIEW,
                profile_name="synthesis",
                cost_usd=0.50,
            ),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="r1"),
            _make_agent_result(success=True, output="R2", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "synthesis" in result.message.lower()


class TestCoordinatorAuditTiming:
    """Test that audit log includes timing and started_at fields."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_started_at_set_in_state(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """CoordinatorState.started_at is set when run_task() begins."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.started_at is not None
        # Should be a valid ISO timestamp
        import datetime

        dt = datetime.datetime.fromisoformat(result.state.started_at)
        assert dt.tzinfo is not None  # timezone-aware

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_audit_log_timing_fields(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """generate_audit_log() includes started_at, finished_at, duration_seconds."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        timing = audit["timing"]
        assert "started_at" in timing
        assert "finished_at" in timing
        assert "duration_seconds" in timing
        assert timing["started_at"] is not None
        assert timing["finished_at"] is not None
        assert timing["duration_seconds"] is not None
        assert timing["duration_seconds"] >= 0


class TestCoordinatorAuditAgentBreakdown:
    """Test per-agent cost breakdown in audit log."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_cost_agents_list(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """cost.agents contains one entry per dev and review invocation."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        dev_result_30 = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_agent.side_effect = _preflight_then(dev_result_30)
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id="s2",
                cost_usd=0.20,
                exit_code=0,
                raw={},
                profile_name="review",
            )
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        agents = audit["cost"]["agents"]
        assert len(agents) == 2  # 1 dev + 1 review

        dev_entry = next(a for a in agents if a["role"] == "dev")
        assert dev_entry["profile"] == "dev"
        assert dev_entry["cost_usd"] == 0.30
        assert "duration_seconds" in dev_entry
        assert dev_entry["duration_seconds"] is not None
        assert dev_entry["duration_seconds"] >= 0

        review_entry = next(a for a in agents if a["role"] == "review")
        assert review_entry["profile"] == "review"
        assert review_entry["cost_usd"] == 0.20
        assert review_entry["duration_seconds"] is not None
        assert review_entry["duration_seconds"] >= 0

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_synthesis_agent_tagged_correctly(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Synthesis agent entry has role='synthesis' in agents list."""
        from theforge.coordinator import generate_audit_log

        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="synthesis"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="r1"),
            _make_agent_result(success=True, output="R2", profile_name="r2"),
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        agents = audit["cost"]["agents"]
        roles = [a["role"] for a in agents]
        assert "synthesis" in roles

        synth_entry = next(a for a in agents if a["role"] == "synthesis")
        assert synth_entry["profile"] == "synthesis"


# ── Structured logging tests ──────────────────────────────────────────


class TestVerboseFlagEnablesToolLines:
    """Tool activity is printed in VERBOSE mode and suppressed in PROGRESS mode."""

    def test_verbose_prints_tool_lines(self, capsys):
        import theforge.runner as runner_mod

        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            # Simulate a tool_use assistant event
            tool_event = (
                '{"type": "assistant", "message": {"content": '
                '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
            )
            runner_mod._process_stream_event(tool_event, "test-label")
            captured = capsys.readouterr()
            assert "↳ Read" in captured.err
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)

    def test_progress_suppresses_tool_lines(self, capsys):
        import theforge.runner as runner_mod

        runner_mod.set_log_level(LogLevel.PROGRESS)
        tool_event = (
            '{"type": "assistant", "message": {"content": '
            '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
        )
        runner_mod._process_stream_event(tool_event, "test-label")
        captured = capsys.readouterr()
        assert "↳ Read" not in captured.err


class TestProgressShowsPhaseTransitions:
    """Phase transition lines always appear at PROGRESS level."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_phase_transitions_always_shown(
        self, mock_shell, mock_agent, mock_pool, tmp_path, capsys
    ):
        import theforge.coordinator as coord_mod
        import theforge.runner as runner_mod

        # Ensure we are at PROGRESS level
        coord_mod.set_log_level(LogLevel.PROGRESS)
        runner_mod.set_log_level(LogLevel.PROGRESS)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        captured = capsys.readouterr()
        # Phase transitions must appear even at PROGRESS level
        assert "▸ WORKSPACE" in captured.err
        assert "▸ DEV" in captured.err
        assert "▸ VALIDATE" in captured.err
        assert "▸ REVIEW" in captured.err
        assert "✓ DONE" in captured.err


class TestCampaignSpecHeaderPrinted:
    """Campaign emits [N/total] slug header before each spec."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_spec_header_emitted(self, mock_shell, mock_agent, mock_pool, tmp_path, capsys):
        import yaml as _yaml

        from theforge.sprint import run_sprint

        # Write a minimal forge.yaml
        config = _make_config(tmp_path)

        # Write a spec file with frontmatter
        spec_path = tmp_path / "test-spec.md"
        spec_path.write_text(
            "---\nname: Test Spec\nslug: test-spec\n---\n# Test\n",
            encoding="utf-8",
        )

        # Write a campaign manifest
        manifest_path = tmp_path / "campaign.yaml"
        manifest_path.write_text(
            _yaml.dump(
                {
                    "name": "test campaign",
                    "budget_usd": 10.0,
                    "specs": ["test-spec.md"],
                }
            ),
            encoding="utf-8",
        )

        workspace = tmp_path / "test-spec"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_sprint(config, manifest_path)

        captured = capsys.readouterr()
        # Header banner for spec [1/1] must appear
        assert "[1/1]" in captured.err
        assert "test-spec" in captured.err


class TestCoordinatorAuditFindings:
    """Test that review findings are included in audit log."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_review_findings_in_audit(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Audit reviews[] entries include findings list with severity, file, line, description."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

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
        audit = generate_audit_log(config, task, result)

        assert len(audit["reviews"]) == 2

        # First review has findings
        first_rev = audit["reviews"][0]
        assert "findings" in first_rev
        assert first_rev["p1_count"] == 1
        assert len(first_rev["findings"]) == 1
        finding = first_rev["findings"][0]
        assert finding["severity"] == "P1"
        assert finding["file"] == "src/foo.py"
        assert finding["line"] == 10
        assert "Off by one" in finding["description"]

        # Second review (APPROVE) has empty findings
        second_rev = audit["reviews"][1]
        assert "findings" in second_rev
        assert second_rev["findings"] == []
        assert second_rev["p1_count"] == 0

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_approve_review_has_empty_findings(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """APPROVE review in audit has findings: [] (not missing key)."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        rev = audit["reviews"][0]
        assert "findings" in rev
        assert rev["findings"] == []


class TestCoordinatorReviewCycleMetadata:
    """Test that review cycle metadata is populated correctly."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_metadata_present_on_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Audit metadata is populated after successful pool+synthesis."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="synthesis"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="r1"),
            _make_agent_result(success=True, output="R2", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert len(result.state.review_cycle_metadata) == 1
        meta = result.state.review_cycle_metadata[0]
        assert meta.pool_models == ["r1", "r2"]
        assert meta.successful == ["r1", "r2"]
        assert meta.failed == []
        assert meta.synthesized is True

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_metadata_present_on_all_reviewers_fail(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Metadata is populated even when all reviewers fail (P2 fix)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=False, output="FAIL", profile_name="r1"),
            _make_agent_result(success=False, output="FAIL", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.phase == Phase.ESCALATE
        # Metadata must be present even though we escalated early
        assert len(result.state.review_cycle_metadata) == 1
        meta = result.state.review_cycle_metadata[0]
        assert meta.failed == ["r1", "r2"]
        assert meta.successful == []
        assert meta.synthesized is False

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_metadata_present_on_synthesis_failure(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Metadata is populated even when synthesis fails (P2 fix)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=False, output="CRASH", profile_name="synthesis"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="r1"),
            _make_agent_result(success=True, output="R2", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.phase == Phase.ESCALATE
        # Metadata must be present despite synthesis failure
        assert len(result.state.review_cycle_metadata) == 1
        meta = result.state.review_cycle_metadata[0]
        assert meta.successful == ["r1", "r2"]
        assert meta.synthesized is True  # synthesis was attempted

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_audit_log_contains_pool_metadata(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """generate_audit_log includes pool_models, synthesized, successful, failed."""
        from theforge.coordinator import generate_audit_log

        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = [
            _PREFLIGHT_RESULT,
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="synthesis"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output="R1", profile_name="r1"),
            _make_agent_result(success=True, output="R2", profile_name="r2"),
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert len(audit["reviews"]) == 1
        rev = audit["reviews"][0]
        assert rev["cycle"] == 1
        assert rev["pool_models"] == ["r1", "r2"]
        assert rev["successful"] == ["r1", "r2"]
        assert rev["failed"] == []
        assert rev["synthesized"] is True
        assert rev["verdict"] == "APPROVE"


class TestCoordinatorDirtyWorktree:
    """Test that the coordinator catches uncommitted changes after gate PASS."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_dirty_worktree_retries_dev(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Dirty worktree after gate PASS sends dev back with process violation feedback."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        call_count = {"gate": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                call_count["gate"] += 1
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # First call: dirty; second call: clean (dev fixed it)
                if call_count["gate"] == 1:
                    return (True, " M src/theforge/runner.py\n M src/theforge/config.py")
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Dev was retried (dirty first, clean second) → should succeed
        assert result.success is True
        assert result.phase == Phase.DONE
        # PREFLIGHT + 2 DEV calls (once dirty, once clean)
        assert mock_agent.call_count == 3

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_dirty_worktree_escalates_after_max_retries(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Dirty worktree with no retries left escalates."""
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
            retry=RetryPolicy(max_dev_iterations=1, max_review_cycles=2),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/runner.py")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "uncommitted" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_handoff_file_not_flagged_as_dirty(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """handoff.yaml in git status output is excluded from dirty check."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # Only handoff.yaml is dirty — that's expected
                return (True, "?? handoff.yaml")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # handoff.yaml is filtered out → clean worktree → proceeds to review
        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_handoff_dirty_worktree_unchanged(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Regression guard: handoff mode still filters handoff.yaml from dirty check."""
        config = _make_config(tmp_path)  # handoff_file="handoff.yaml"
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                # handoff.yaml is the only dirty file — should be filtered out
                return (True, "?? handoff.yaml")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # handoff.yaml filtered out → worktree clean → proceeds to DONE
        assert result.success is True
        assert result.phase == Phase.DONE


# ── Human Review Tests ────────────────────────────────────────────────


class TestCoordinatorHumanReview:
    """Tests for the HUMAN_REVIEW phase (R7 from the spec)."""

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _make_interactive_base(tmp_path):
        """Return (config, task, workspace) with workspace already created."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        return config, task, workspace

    @staticmethod
    def _shell_side_effect(workspace):
        """Standard shell mock: gate writes PASS handoff, git status is clean."""

        def side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(workspace, "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        return side_effect

    # ── test_interactive_approve ──────────────────────────────────────

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_interactive_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Human enters 'a' → DONE."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        import io

        with patch("sys.stdin", io.StringIO("a\n")):
            result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.human_review_decision == "approve"
        assert result.state.human_review_feedback is None

    # ── test_interactive_reject_loops_back ────────────────────────────

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_interactive_reject_loops_back(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Human enters 'r' + findings → dev called again with human_feedback, then approves."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))

        # First review cycle: APPROVE → human rejects; second cycle: APPROVE → human approves
        approve_result = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_pool.return_value = approve_result  # same list each call (pool is called twice)

        # Use side_effect list so first call triggers reject path, second triggers approve
        import io

        stdin_input = "r\nfix the bug\n\na\n"
        with patch("sys.stdin", io.StringIO(stdin_input)):
            result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        # dev agent was called at least twice (original + after rejection)
        assert len(result.state.dev_results) >= 2
        # The human_review_decision records the final decision
        assert result.state.human_review_decision == "approve"

    # ── test_interactive_escalate ─────────────────────────────────────

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_interactive_escalate(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Human enters 'e' → ESCALATE."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        import io

        with patch("sys.stdin", io.StringIO("e\n")):
            result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "escalate"

    # ── test_auto_mode_skips_human_review ─────────────────────────────

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_mode_skips_human_review(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """interactive=False never enters HUMAN_REVIEW."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=False)

        assert result.success is True
        assert result.phase == Phase.DONE
        # HUMAN_REVIEW phase was never set
        assert result.state.human_review_decision is None
        # The phase stored in state at completion is DONE (not HUMAN_REVIEW)
        assert result.state.phase == Phase.DONE

    # ── test_interactive_on_exhausted_cycles ─────────────────────────

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_interactive_on_exhausted_cycles(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """When review cycles exhaust with REQUEST_CHANGES, human can still choose."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        # Always REQUEST_CHANGES → cycles exhaust → HUMAN_REVIEW
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        import io

        # Human escalates at the HUMAN_REVIEW prompt
        with patch("sys.stdin", io.StringIO("e\n")):
            result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "escalate"


# ── Preflight tests ──────────────────────────────────────────────────


class TestCoordinatorPreflight:
    """Test the PREFLIGHT phase: classify spec before expensive dev cycles."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_preflight_proceed_continues_to_dev(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """PROCEED verdict → normal dev→validate→review flow."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_preflight_already_done_skips_dev(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """ALREADY_DONE verdict → DONE immediately, no dev or review cycles."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        mock_agent.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.08, profile_name="review"
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "ALREADY_DONE"
        assert "already" in result.message.lower()
        assert len(result.state.dev_results) == 0
        assert len(result.state.review_results) == 0
        assert mock_agent.call_count == 1
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_preflight_blocked_escalates(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """BLOCKED verdict → ESCALATE with reason."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        mock_agent.return_value = _make_agent_result(
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

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_preflight_agent_failure_proceeds(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """If the preflight agent itself fails, fail-open to PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        preflight_fail = _make_agent_result(success=False, output="CLI error", cost_usd=0.0)
        dev_ok = _make_agent_result(success=True, output="Implemented.")
        mock_agent.side_effect = [preflight_fail, dev_ok]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert "failed" in result.state.preflight_reason.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_preflight_unparseable_proceeds(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """If preflight output is not valid YAML, fail-open to PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        preflight_garbage = _make_agent_result(
            success=True, output="I don't know what to do", cost_usd=0.05
        )
        dev_ok = _make_agent_result(success=True, output="Implemented.")
        mock_agent.side_effect = [preflight_garbage, dev_ok]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_preflight_cost_in_audit(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Preflight cost appears in audit log."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        mock_agent.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.08, profile_name="review"
        )

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert audit["preflight"] is not None
        assert audit["preflight"]["verdict"] == "ALREADY_DONE"
        assert audit["preflight"]["cost_usd"] == 0.08
        assert "already" in audit["preflight"]["reason"].lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_preflight_reads_file_scope(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Preflight prompt includes current file contents from file_scope."""
        config = _make_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        task = TaskSpec(
            name="Scoped Task",
            spec_path=spec,
            slug="test-task",
            file_scope=["src/foo.py"],
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "foo.py").write_text("def foo(): pass\n", encoding="utf-8")

        mock_shell.return_value = (True, "OK")
        mock_agent.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.05
        )

        run_task(config, task)

        preflight_call = mock_agent.call_args_list[0]
        prompt = preflight_call.kwargs["prompt"]
        assert "def foo(): pass" in prompt
        assert "src/foo.py" in prompt


# ── Auto-merge Tests ──────────────────────────────────────────────────


class TestCoordinatorAutoMerge:
    """Tests for the auto_merge=True path."""

    def _shell_with_gate_and_merge(self, workspace: "Path", merge_succeeds: bool = True):
        """Shell side_effect: handles gate, git status, and merge-related commands."""

        def side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")  # clean
            # Stale-worktree detection commands (must come before generic git log checks)
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git branch --list" in cmd:
                return (True, "main")  # base branch exists
            if "git log" in cmd and ".." in cmd:
                return (True, "abc123 feat: implement thing")  # has commits ahead
            if "git checkout" in cmd:
                return (True, "Switched to branch 'main'")
            if "git merge --ff-only" in cmd:
                if merge_succeeds:
                    return (True, "Fast-forward")
                return (False, "fatal: Not possible to fast-forward")
            if "git merge --no-edit" in cmd:
                return (True, "Merge made by 'ort'")
            if "git worktree remove" in cmd:
                return (True, "OK")
            return (True, "OK")

        return side_effect

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_success_on_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: merge occurs after APPROVE, result.merge.merged is True."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=True)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["attempted"] is True
        assert result.merge["merged"] is True
        assert result.merge["base_branch"] == "main"
        assert result.merge["error"] is None
        assert "Merged." in result.message

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_false_no_merge(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=False (default): no merge, result.merge is None."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=False)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is None

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_no_merge_on_escalate(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: no merge when result is ESCALATE (not APPROVE)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented."),
            _make_agent_result(success=True, output="Fixed."),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.merge is None  # no merge attempted on ESCALATE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_ff_fails_falls_back(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: non-ff fallback used when ff-only fails."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=False)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is True  # fell back to --no-edit and succeeded

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_safety_no_base_branch(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: skips merge if base branch doesn't exist."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git branch --list" in cmd:
                return (True, "")  # base branch not found
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # run still succeeds
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None
        assert "not found" in result.merge["error"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_safety_dirty_project_root(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """auto_merge=True: skips merge if project root has uncommitted changes."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dirty_seen = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                dirty_seen["n"] += 1
                # First call is from validate (worktree clean check); second is safety check
                if dirty_seen["n"] == 1:
                    return (True, "")  # worktree clean → proceed to review
                return (True, " M some_file.py")  # project root dirty → skip merge
            if "git branch --list" in cmd:
                return (True, "main")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # run still succeeds
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None
        assert "Uncommitted" in result.merge["error"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_safety_no_commits_ahead(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: skips merge if branch has no commits ahead of base."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git branch --list" in cmd:
                return (True, "main")
            if "git log" in cmd and ".." in cmd:
                return (True, "")  # no commits ahead
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert "no commits" in result.merge["error"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_merge_info_in_audit(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Merge info appears in audit log under 'merge' key."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=True)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)
        audit = generate_audit_log(config, task, result)

        assert "merge" in audit
        merge = audit["merge"]
        assert merge["attempted"] is True
        assert merge["merged"] is True
        assert merge["base_branch"] == "main"
        assert merge["error"] is None

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_auto_merge_false_no_merge_key_in_audit(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Without auto_merge, audit 'merge' key is None."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=False)
        audit = generate_audit_log(config, task, result)

        assert audit["merge"] is None


# ── Exit-code gate mode + pytest_target substitution ─────────────


def _make_exit_code_config(tmp_path: Path) -> ForgeConfig:
    """Config with exit-code gate mode (empty handoff_file)."""
    from theforge.config import ValidationConfig

    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(
            gate_command="pytest {pytest_target} -q",
            handoff_file="",
            gate_decision_key="",
            gate_timeout=120,
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _shell_exit_code(pass_on_call: int | None = None, gate_marker: str = "pytest"):
    """Shell side_effect for exit-code gate mode.

    If pass_on_call is None, all gate calls pass.
    If pass_on_call is N, gate fails until the Nth call.
    gate_marker: string to detect which shell command is the gate command.
    """
    gate_idx = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if gate_marker in cmd:
            gate_idx["n"] += 1
            if pass_on_call is not None and gate_idx["n"] < pass_on_call:
                return (False, "FAILED: 1 error")
            return (True, "passed")
        if "git status --porcelain" in cmd:
            return (True, "")
        stale_resp = _handle_stale_check_cmd(cmd)
        if stale_resp is not None:
            return stale_resp
        return (True, "OK")

    return side_effect


class TestExitCodeGateMode:
    """Test gate validation using exit code instead of handoff file."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_exit_code_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Exit code 0 → PASS in exit-code mode."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_exit_code()
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_exit_code_fail_then_pass(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Exit code non-zero → FAIL, then 0 → PASS on retry."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_exit_code(pass_on_call=2)
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_iteration == 2  # needed a retry
        assert result.state.gate_decisions == ["FAIL", "PASS"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_exit_code_exhaustion(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Gate always fails → ESCALATE after max iterations."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_exit_code(pass_on_call=999)
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_infrastructure_failure_escalates_immediately(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """TIMEOUT/ERROR in exit-code mode escalates immediately (not retried as FAIL)."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (False, "TIMEOUT after 120s: pytest tests/ -q")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "infrastructure" in result.message.lower() or "timeout" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_dirty_worktree_blocked_in_exit_code_mode(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Dirty worktree is still caught in exit-code mode (empty handoff_file)."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/coordinator.py\n M tests/test_something.py")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase != Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_exit_code_dirty_worktree_detected(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Exit-code mode: dirty files detected (empty handoff_file must not cause false-clean)."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, " M src/theforge/coordinator.py\n M tests/test_foo.py")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase != Phase.DONE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_exit_code_gate_timeout_is_error(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Timeout in exit-code mode returns error message (not FAIL), escalates immediately."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (False, "TIMEOUT after 120s: pytest tests/ -q")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Message must mention timeout and hint to increase gate_timeout
        msg = result.message.lower()
        assert "timed out" in msg or "gate_timeout" in msg

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_exit_code_infrastructure_error_is_error(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """ERROR: prefix in exit-code mode returns error (not FAIL), escalates immediately."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pytest" in cmd:
                return (False, "ERROR: [Errno 2] No such file or directory: 'pytest'")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Escalated as infrastructure error, not retried as FAIL
        assert result.state.dev_iteration == 1  # no retries consumed

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_exit_code_test_failure_is_fail(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Normal non-zero exit (tests failing) returns FAIL and is retried."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # First gate call fails with normal test output; second passes
        mock_shell.side_effect = _shell_exit_code(pass_on_call=2)
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.gate_decisions == ["FAIL", "PASS"]
        assert result.state.dev_iteration == 2  # was retried


class TestPytestTargetSubstitution:
    """Test that {pytest_target} in gate_command is replaced from TaskSpec."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_pytest_target_substituted(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Gate command should contain the task's pytest_target, not the placeholder."""
        config = _make_exit_code_config(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("# Test", encoding="utf-8")
        task = TaskSpec(
            name="Test",
            spec_path=spec,
            slug="test-task",
            file_scope=[],
            pytest_target="tests/test_specific.py",
        )
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_cmds = []

        def shell_side_effect(cmd, cwd, **kwargs):
            captured_cmds.append(cmd)
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "pytest" in cmd and "worktree" not in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        # Find the gate command: contains pytest but is NOT a git worktree command
        gate_cmds = [c for c in captured_cmds if "pytest" in c and "worktree" not in c]
        assert gate_cmds, "No gate command captured"
        assert "tests/test_specific.py" in gate_cmds[0]
        assert "{pytest_target}" not in gate_cmds[0]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_pytest_target_defaults_to_tests(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """When pytest_target is None, defaults to 'tests/'."""
        config = _make_exit_code_config(tmp_path)
        task = _make_task(tmp_path)  # pytest_target=None by default
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        captured_cmds = []

        def shell_side_effect(cmd, cwd, **kwargs):
            captured_cmds.append(cmd)
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "pytest" in cmd and "worktree" not in cmd:
                return (True, "passed")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(_make_agent_result())
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        gate_cmds = [c for c in captured_cmds if "pytest" in c and "worktree" not in c]
        assert gate_cmds
        assert "tests/" in gate_cmds[0]


# ── Parse Retry Tests ─────────────────────────────────────────────────


PARSE_ERROR_OUTPUT = "this is not valid yaml: {{{ completely broken"

SCHEMA_ERROR_OUTPUT = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
```
"""
# Missing spec_compliance and test_coverage → schema errors


class TestReviewParseRetry:
    """Tests for reviewer retry on parse/schema errors (spec: review-parse-retry)."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_parse_error_does_not_increment_cycle(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Parse error on first review attempt → retry → APPROVE: review_cycle == 1."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=PARSE_ERROR_OUTPUT, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Parse error did NOT increment review_cycle — only the valid APPROVE did
        assert result.state.review_cycle == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_parse_error_then_request_changes(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Parse error then real REQUEST_CHANGES → cycle increments once, DEV retried."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(),  # dev cycle 1
            _make_agent_result(),  # dev cycle 2 (after REQUEST_CHANGES)
        )

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                # Parse error — triggers retry, NOT a cycle
                return [
                    _make_agent_result(
                        success=True,
                        output=PARSE_ERROR_OUTPUT,
                        profile_name="review",
                        cost_usd=0.1,
                    )
                ]
            if call_count["pool"] == 2:
                # Real REQUEST_CHANGES — increments cycle to 1, DEV reruns
                return [
                    _make_agent_result(
                        success=True,
                        output=REQUEST_CHANGES_REVIEW,
                        profile_name="review",
                        cost_usd=0.1,
                    )
                ]
            # Cycle 2: APPROVE
            return [
                _make_agent_result(
                    success=True, output=APPROVE_REVIEW, profile_name="review", cost_usd=0.1
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # review_cycle == 2: cycle 1 (parse error + REQUEST_CHANGES), cycle 2 (APPROVE)
        assert result.state.review_cycle == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_all_parse_retries_exhausted(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """All parse retries exhausted → ESCALATE with 'unreliable' in message."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        # Always return parse errors (default max_parse_retries=2 → 3 total attempts)
        # Use low cost_usd to avoid hitting the review budget before exhausting retries
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=PARSE_ERROR_OUTPUT, profile_name="review", cost_usd=0.1
            )
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "unreliable" in result.message.lower()

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_parse_retry_count_in_audit(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Audit log records parse_retries: 1 when one retry occurred."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=PARSE_ERROR_OUTPUT, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert result.success is True
        assert len(audit["reviews"]) == 1
        assert audit["reviews"][0]["parse_retries"] == 1

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_schema_error_also_retried(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Schema validation error (not just YAML parse error) also triggers retry."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result())

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                # Valid YAML but invalid schema (missing spec_compliance, test_coverage)
                return [
                    _make_agent_result(
                        success=True, output=SCHEMA_ERROR_OUTPUT, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Schema error triggered retry — only 1 review cycle
        assert result.state.review_cycle == 1
        # parse_retries tracked in metadata
        assert result.state.review_cycle_metadata[0].parse_retries == 1


# ── Tests: run_review_only ────────────────────────────────────────────


class TestReviewOnly:
    """Tests for run_review_only — skips WORKSPACE/PREFLIGHT/DEV/VALIDATE."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator._run_shell")
    def test_review_only_approve(self, mock_shell, mock_pool, tmp_path):
        """APPROVE → success, phase=DONE, dev_iteration=0."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")  # git diff returns empty
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_review_only(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.dev_iteration == 0
        assert result.state.review_cycle == 1
        assert len(result.state.review_results) == 1
        assert result.state.review_results[0].verdict == "APPROVE"
        # No dev agents were invoked
        assert len(result.state.dev_results) == 0

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator._run_shell")
    def test_review_only_request_changes(self, mock_shell, mock_pool, tmp_path):
        """REQUEST_CHANGES → failure, phase=ESCALATE, findings in result."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_review_only(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 0
        # Findings are present in review results
        assert len(result.state.review_results) == 1
        assert result.state.review_results[0].verdict == "REQUEST_CHANGES"
        assert len(result.state.review_results[0].findings) > 0

    def test_review_only_missing_worktree(self, tmp_path):
        """Missing workspace_path → error result with clear message."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        missing = tmp_path / "does-not-exist"

        result = run_review_only(config, task, missing)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Worktree not found" in result.message
        assert "forge run" in result.message
        assert str(missing) in result.message

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator._run_shell")
    def test_review_only_no_dev_cycles(self, mock_shell, mock_pool, tmp_path):
        """dev_iteration == 0 in all cases (APPROVE and REQUEST_CHANGES)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")

        for review_output in [APPROVE_REVIEW, REQUEST_CHANGES_REVIEW]:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=review_output, profile_name="review")
            ]
            result = run_review_only(config, task, workspace)
            assert result.state.dev_iteration == 0
            assert len(result.state.dev_results) == 0


class TestAuditReviewPoolFields:
    """Tests for generate_audit_log() review pool field serialization."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_audit_review_pool_fields_populated(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """When pool has 2 reviewers and one fails, audit has correct pool/successful/failed."""
        config = _make_pool_config(
            tmp_path,
            profiles=[
                ModelProfile(
                    name="opus",
                    cli="claude",
                    model="claude-opus-4-6",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
                ModelProfile(
                    name="codex",
                    cli="codex",
                    model="codex",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
            ],
            synthesis=ModelProfile(
                name="synthesis",
                cli="claude",
                model="claude-sonnet-4-6",
                budget_usd=5.0,
                timeout_seconds=300,
                allowed_tools=[],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        # opus succeeds, codex fails
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id=None,
                cost_usd=0.10,
                exit_code=0,
                raw={},
                profile_name="opus",
            ),
            AgentResult(
                success=False,
                output="",
                session_id=None,
                cost_usd=0.0,
                exit_code=1,
                raw={},
                profile_name="codex",
            ),
        ]

        result = run_task(config, task)

        audit = generate_audit_log(config, task, result)
        reviews = audit["reviews"]
        assert len(reviews) == 1
        cycle = reviews[0]
        assert set(cycle["pool_models"]) == {"opus", "codex"}
        assert cycle["successful"] == ["opus"]
        assert cycle["failed"] == ["codex"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_audit_failed_reviewer_detail(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Failed reviewer includes exit code in failed_detail."""
        config = _make_pool_config(
            tmp_path,
            profiles=[
                ModelProfile(
                    name="opus",
                    cli="claude",
                    model="claude-opus-4-6",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
                ModelProfile(
                    name="codex",
                    cli="codex",
                    model="codex",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
            ],
            synthesis=ModelProfile(
                name="synthesis",
                cli="claude",
                model="claude-sonnet-4-6",
                budget_usd=5.0,
                timeout_seconds=300,
                allowed_tools=[],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id=None,
                cost_usd=0.10,
                exit_code=0,
                raw={},
                profile_name="opus",
            ),
            AgentResult(
                success=False,
                output="",
                session_id=None,
                cost_usd=0.0,
                exit_code=1,
                raw={},
                profile_name="codex",
            ),
        ]

        result = run_task(config, task)

        audit = generate_audit_log(config, task, result)
        cycle = audit["reviews"][0]
        assert "failed_detail" in cycle
        assert "codex" in cycle["failed_detail"]
        assert "exit=1" in cycle["failed_detail"]["codex"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_audit_synthesized_flag_true(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """synthesized=True when synthesis agent ran (both reviewers succeeded)."""
        synthesis_profile = ModelProfile(
            name="synthesis",
            cli="claude",
            model="claude-sonnet-4-6",
            budget_usd=5.0,
            timeout_seconds=300,
            allowed_tools=[],
        )
        config = _make_pool_config(
            tmp_path,
            profiles=[
                ModelProfile(
                    name="opus",
                    cli="claude",
                    model="claude-opus-4-6",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
                ModelProfile(
                    name="gemini",
                    cli="gemini",
                    model="gemini-pro",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
            ],
            synthesis=synthesis_profile,
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        synthesis_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id=None,
            cost_usd=0.05,
            exit_code=0,
            raw={},
            profile_name="synthesis",
        )

        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        # Both reviewers succeed → synthesis runs
        mock_pool.side_effect = [
            # First call: review pool
            [
                AgentResult(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id=None,
                    cost_usd=0.10,
                    exit_code=0,
                    raw={},
                    profile_name="opus",
                ),
                AgentResult(
                    success=True,
                    output=APPROVE_REVIEW,
                    session_id=None,
                    cost_usd=0.10,
                    exit_code=0,
                    raw={},
                    profile_name="gemini",
                ),
            ],
        ]

        # Intercept the synthesis agent call
        preflight_result = _PREFLIGHT_RESULT
        dev_result = _make_agent_result(success=True, output="Done.")
        call_idx = {"n": 0}
        call_order = [preflight_result, dev_result, synthesis_result]

        def ordered_agent(**kwargs):
            idx = min(call_idx["n"], len(call_order) - 1)
            call_idx["n"] += 1
            return call_order[idx]

        mock_agent.side_effect = ordered_agent

        result = run_task(config, task)

        audit = generate_audit_log(config, task, result)
        assert len(audit["reviews"]) == 1
        assert audit["reviews"][0]["synthesized"] is True

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_audit_synthesized_flag_false_degraded(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """synthesized=False when degraded to single reviewer (one failed)."""
        config = _make_pool_config(
            tmp_path,
            profiles=[
                ModelProfile(
                    name="opus",
                    cli="claude",
                    model="claude-opus-4-6",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
                ModelProfile(
                    name="codex",
                    cli="codex",
                    model="codex",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
            ],
            synthesis=ModelProfile(
                name="synthesis",
                cli="claude",
                model="claude-sonnet-4-6",
                budget_usd=5.0,
                timeout_seconds=300,
                allowed_tools=[],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        # One fails → degraded, no synthesis
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id=None,
                cost_usd=0.10,
                exit_code=0,
                raw={},
                profile_name="opus",
            ),
            AgentResult(
                success=False,
                output="",
                session_id=None,
                cost_usd=0.0,
                exit_code=1,
                raw={},
                profile_name="codex",
            ),
        ]

        result = run_task(config, task)

        audit = generate_audit_log(config, task, result)
        assert audit["reviews"][0]["synthesized"] is False


class TestCampaignAuditWrites:
    """Tests for campaign audit writing behavior.

    These tests cover campaign.py behavior within the allowed file scope
    (tests/test_coordinator.py). Campaign-specific tests for run_sprint()
    audit writes are placed here since tests/test_campaign.py is out of scope.
    """

    def _make_manifest(self, tmp_path: Path, spec_rel_paths: list[str]) -> Path:
        """Create a campaign manifest YAML in tmp_path."""
        manifest = {
            "name": "Test Campaign",
            "budget_usd": 100.0,
            "specs": spec_rel_paths,
        }
        manifest_path = tmp_path / "campaign.yaml"
        manifest_path.write_text(yaml.dump(manifest), encoding="utf-8")
        return manifest_path

    def _make_fake_result(self, tmp_path: Path) -> object:
        """Build a fake CoordinatorResult with one APPROVE review cycle."""
        from theforge.coordinator import CoordinatorResult, CoordinatorState, ReviewCycleMetadata
        from theforge.review import ReviewResult

        state = CoordinatorState()
        meta = ReviewCycleMetadata(
            pool_models=["opus", "codex"],
            successful=["opus"],
            failed=["codex"],
            synthesized=False,
            failed_detail={"codex": "exit=1"},
        )
        state.review_cycle_metadata.append(meta)
        state.review_cycle = 1
        rr = ReviewResult(
            verdict="APPROVE",
            summary="Looks good.",
            findings=[],
            spec_matches=True,
            spec_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        state.review_results.append(rr)
        return CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done",
        )

    @patch("theforge.sprint.run_task")
    def test_campaign_writes_worktree_audit(self, mock_run_task, tmp_path):
        """After run_sprint(), the spec worktree contains forge_audit.yaml."""
        from theforge.sprint import run_sprint

        config = _make_config(tmp_path)

        # Create a spec file
        spec = tmp_path / "spec.md"
        spec.write_text("---\nslug: my-spec\nname: My Spec\n---\n# Spec", encoding="utf-8")

        # Pre-create the workspace (simulates coordinator having created it)
        workspace = tmp_path / "my-spec"
        workspace.mkdir()

        fake_result = self._make_fake_result(tmp_path)
        mock_run_task.return_value = fake_result

        manifest_path = self._make_manifest(tmp_path, ["spec.md"])
        run_sprint(config, manifest_path)

        audit_path = workspace / "forge_audit.yaml"
        assert audit_path.exists(), "forge_audit.yaml not written to worktree"
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8")) or {}
        assert "reviews" in audit
        assert len(audit["reviews"]) == 1
        assert audit["reviews"][0]["failed"] == ["codex"]
        assert audit["reviews"][0].get("failed_detail", {}).get("codex") == "exit=1"

    @patch("theforge.sprint.run_task")
    def test_sprint_audit_includes_review_summary(self, mock_run_task, tmp_path):
        """sprint-audit.yaml has reviews list per spec with pool/successful/failed fields."""
        from theforge.sprint import run_sprint

        config = _make_config(tmp_path)

        spec = tmp_path / "spec.md"
        spec.write_text("---\nslug: my-spec\nname: My Spec\n---\n# Spec", encoding="utf-8")

        workspace = tmp_path / "my-spec"
        workspace.mkdir()

        fake_result = self._make_fake_result(tmp_path)
        mock_run_task.return_value = fake_result

        manifest_path = self._make_manifest(tmp_path, ["spec.md"])
        run_sprint(config, manifest_path)

        sprint_audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        assert sprint_audit_path.exists(), "sprint-audit.yaml not written"
        audit = yaml.safe_load(sprint_audit_path.read_text(encoding="utf-8")) or {}
        specs = audit.get("specs", [])
        assert len(specs) == 1
        spec_entry = specs[0]
        assert "reviews" in spec_entry
        reviews = spec_entry["reviews"]
        assert len(reviews) == 1
        cycle = reviews[0]
        assert set(cycle["pool"]) == {"opus", "codex"}
        assert cycle["successful"] == ["opus"]
        assert cycle["failed"] == ["codex"]
        assert cycle["verdict"] == "APPROVE"
        assert cycle["p1_count"] == 0
        assert cycle["p2_count"] == 0

    @patch("theforge.sprint.run_task")
    def test_campaign_already_done_no_worktree_audit(self, mock_run_task, tmp_path):
        """ALREADY_DONE specs do not write a worktree audit (no worktree was created)."""
        from theforge.coordinator import CoordinatorResult, CoordinatorState
        from theforge.sprint import run_sprint

        config = _make_config(tmp_path)

        spec = tmp_path / "spec.md"
        spec.write_text("---\nslug: done-spec\nname: Done Spec\n---\n# Spec", encoding="utf-8")

        # No workspace created — ALREADY_DONE path
        state = CoordinatorState()
        state.preflight_verdict = "ALREADY_DONE"
        fake_result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Already done",
        )
        mock_run_task.return_value = fake_result

        manifest_path = self._make_manifest(tmp_path, ["spec.md"])
        run_sprint(config, manifest_path)

        # No workspace dir → no forge_audit.yaml written there
        workspace = tmp_path / "done-spec"
        assert not workspace.exists() or not (workspace / "forge_audit.yaml").exists()


# ── run_from_review tests ─────────────────────────────────────────────


class TestRunFromReview:
    """Tests for the run_from_review() full iteration loop entry point."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator._run_shell")
    def test_run_from_review_approve_merges(self, mock_shell, mock_pool, tmp_path):
        """APPROVE on first review → DONE; auto_merge triggers merge attempt."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # _run_shell: git diff (OK), git status --porcelain (clean), merge safety checks
        def shell_side_effect(cmd, cwd, **kwargs):
            if "git branch --list" in cmd:
                return (True, "main")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git log" in cmd:
                return (True, "abc123 feat: something")
            if "git checkout" in cmd or "git merge" in cmd or "git worktree" in cmd:
                return (True, "OK")
            return (True, "")

        mock_shell.side_effect = shell_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, auto_merge=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        assert result.state.dev_iteration == 0
        # No dev agent invoked
        assert len(result.state.dev_results) == 0
        # auto_merge attempted
        assert result.merge is not None
        assert result.merge["attempted"] is True

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_run_from_review_request_changes_iterates(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """REQUEST_CHANGES → dev cycle → re-review → APPROVE → DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Fixed.")

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 2
        # One dev iteration ran
        assert result.state.dev_iteration == 1
        assert len(result.state.dev_results) == 1
        # preflight was skipped
        assert result.state.preflight_verdict == "SKIPPED"
        assert result.state.preflight_result is None

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_run_from_review_exhausts_cycles(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """REQUEST_CHANGES × max_review_cycles → ESCALATE."""
        config = _make_config(tmp_path)  # max_review_cycles=2
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Attempted fix.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()
        assert result.state.review_cycle == config.retry.max_review_cycles

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator._run_shell")
    def test_run_from_review_skips_preflight(self, mock_shell, mock_pool, tmp_path):
        """preflight_verdict is 'SKIPPED' and no preflight agent is ever invoked."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace)

        assert result.state.preflight_verdict == "SKIPPED"
        assert result.state.preflight_result is None
        # run_agent was never called (no preflight, no dev)
        # We can verify by checking no dev results
        assert len(result.state.dev_results) == 0

        # Spec requires: audit log records preflight_verdict as 'SKIPPED' with cost 0.0
        from theforge.coordinator import generate_audit_log

        audit = generate_audit_log(config, task, result)
        assert audit["preflight"] is not None
        assert audit["preflight"]["verdict"] == "SKIPPED"
        assert audit["preflight"]["cost_usd"] == 0.0


# ── _fmt_duration ─────────────────────────────────────────────────────


def test_fmt_duration_seconds():
    assert _fmt_duration(0) == "0s"
    assert _fmt_duration(1) == "1s"
    assert _fmt_duration(59) == "59s"
    assert _fmt_duration(59.9) == "59s"


def test_fmt_duration_minutes():
    assert _fmt_duration(60) == "1m 0s"
    assert _fmt_duration(61) == "1m 1s"
    assert _fmt_duration(90) == "1m 30s"
    assert _fmt_duration(3599) == "59m 59s"


def test_fmt_duration_hours():
    assert _fmt_duration(3600) == "1h 0m 0s"
    assert _fmt_duration(3661) == "1h 1m 1s"
    assert _fmt_duration(7384) == "2h 3m 4s"


# ── TestStaleWorktree ─────────────────────────────────────────────────


def _make_stale_config(tmp_path: Path, stale_worktree_days: int = 1) -> ForgeConfig:
    """Create a test ForgeConfig with a WorkspaceConfig that has stale_worktree_days."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            base_branch="main",
            stale_worktree_days=stale_worktree_days,
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


class TestStaleWorktree:
    """Tests for stale worktree detection (R1–R6 from spec)."""

    # ── _is_stale_worktree unit tests ────────────────────────────────

    @patch("theforge.coordinator._run_shell")
    def test_stale_zero_commits_ahead(self, mock_shell, tmp_path):
        """Branch has 0 commits ahead of base → stale."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "git log main..feat/my-spec --oneline" in cmd:
                return (True, "")  # empty → 0 commits ahead
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True
        assert "0 commits ahead" in info

    @patch("theforge.coordinator._run_shell")
    def test_stale_old_commit(self, mock_shell, tmp_path):
        """Branch has commits but last commit is >1 day old → stale."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        # Timestamp 3 days ago
        old_ts = int(
            (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).timestamp()
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "--oneline" in cmd:
                return (True, "abc123 some commit")
            if "--format=%ct" in cmd:
                return (True, str(old_ts))
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True
        assert "stale" in info

    @patch("theforge.coordinator._run_shell")
    def test_fresh_worktree_reused(self, mock_shell, tmp_path):
        """Branch has commits ahead, last commit recent → not stale (safe to reuse)."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        # Timestamp 12 minutes ago
        recent_ts = int(
            (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=12)
            ).timestamp()
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "--oneline" in cmd:
                return (True, "abc123 commit one\ndef456 commit two\nghi789 commit three")
            if "--format=%ct" in cmd:
                return (True, str(recent_ts))
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "3 commits ahead" in info
        assert "stale" not in info

    @patch("theforge.coordinator._run_shell")
    def test_stale_worktree_days_zero_always_removes(self, mock_shell, tmp_path):
        """stale_worktree_days=0 → always stale regardless of commit state."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=0)

        # Even with a recent commit, stale_days=0 means always remove
        recent_ts = int(
            (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
            ).timestamp()
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "--oneline" in cmd:
                return (True, "abc123 recent commit")
            if "--format=%ct" in cmd:
                return (True, str(recent_ts))
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True
        assert "stale_worktree_days=0" in info

    @patch("theforge.coordinator._run_shell")
    def test_stale_branch_not_found(self, mock_shell, tmp_path):
        """Worktree dir exists but branch is gone (corrupted state) → stale."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        mock_shell.return_value = (False, "fatal: not a git repository")

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True

    # ── _remove_worktree unit tests ──────────────────────────────────

    @patch("theforge.coordinator._run_shell")
    def test_remove_worktree_logs_warning(self, mock_shell, tmp_path, capsys):
        """Warning is logged before removal."""
        mock_shell.return_value = (True, "")

        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "stale worktree detected" in captured.err
        assert "feat/my-spec" in captured.err

    @patch("theforge.coordinator._run_shell")
    def test_remove_failure_does_not_raise(self, mock_shell, tmp_path, capsys):
        """git worktree remove failure is logged but does not raise."""
        mock_shell.return_value = (False, "error: not a git worktree")

        # Must not raise
        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "Warning" in captured.err

    # ── Integration: _create_workspace stale detection ───────────────

    @patch("theforge.coordinator._run_shell")
    def test_no_existing_worktree(self, mock_shell, tmp_path):
        """Path doesn't exist → no staleness check, normal workspace creation."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug

        # Workspace does NOT exist initially
        assert not workspace.exists()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "mkdir" in cmd:
                workspace.mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        from theforge.coordinator import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace
        # rev-parse should NOT have been called (no stale check needed)
        for call in mock_shell.call_args_list:
            cmd_arg = call[0][0]
            assert "rev-parse" not in cmd_arg

    @patch("theforge.coordinator._run_shell")
    def test_stale_worktree_removed_on_create(self, mock_shell, tmp_path):
        """Stale worktree (0 commits ahead) is removed and workspace recreated."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()  # Pre-existing stale worktree

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/test-task")
            if "--oneline" in cmd:
                return (True, "")  # 0 commits ahead
            if "worktree remove" in cmd:
                # Simulate removal
                import shutil

                if workspace.exists():
                    shutil.rmtree(workspace)
                return (True, "")
            if "branch -D" in cmd:
                return (True, "")
            if "mkdir" in cmd:
                workspace.mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        from theforge.coordinator import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace

        # Verify git worktree remove was called
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert any("worktree remove" in c for c in calls)

    @patch("theforge.coordinator._run_shell")
    def test_fresh_worktree_not_removed(self, mock_shell, tmp_path):
        """Fresh worktree (recent commits ahead) is reused without removal."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()  # Pre-existing fresh worktree

        recent_ts = int(
            (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
            ).timestamp()
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/test-task")
            if "--oneline" in cmd:
                return (True, "abc123 a commit")
            if "--format=%ct" in cmd:
                return (True, str(recent_ts))
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        from theforge.coordinator import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace

        # Verify git worktree remove was NOT called
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert not any("worktree remove" in c for c in calls)
        assert not any("branch -D" in c for c in calls)

    def test_stale_worktree_days_parsed_from_forge_yaml(self, tmp_path):
        """stale_worktree_days in forge.yaml is parsed into WorkspaceConfig."""
        from theforge.config import load_config

        config_file = tmp_path / "forge.yaml"
        config_file.write_text(
            """\
project: myproject
workspace:
  create_command: "git worktree add {slug}"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "feat/{slug}"
  stale_worktree_days: 3
""",
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert cfg.workspace.stale_worktree_days == 3

    def test_stale_worktree_days_defaults_to_1(self, tmp_path):
        """stale_worktree_days defaults to 1 when not set in forge.yaml."""
        from theforge.config import load_config

        config_file = tmp_path / "forge.yaml"
        config_file.write_text("project: myproject\n", encoding="utf-8")
        cfg = load_config(config_file)
        assert cfg.workspace.stale_worktree_days == 1


# ── Remote HITL helpers ───────────────────────────────────────────────


def _make_ntfy_config(
    tmp_path: Path,
    url: str = "https://ntfy.sh/test-topic",
    timeout: int = 60,
) -> ForgeConfig:
    """Create a ForgeConfig with ntfy notifications enabled."""
    return ForgeConfig(
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
        notifications=NotificationConfig(
            backend="ntfy",
            ntfy=NtfyConfig(url=url, priority="high"),
            human_review_timeout_seconds=timeout,
        ),
    )


class TestRemoteHumanReview:
    """Remote async HITL via ntfy action buttons."""

    def test_ntfy_reply_url(self):
        assert _ntfy_reply_url("https://ntfy.sh/my-topic") == "https://ntfy.sh/my-topic-reply"
        assert _ntfy_reply_url("https://ntfy.sh/my-topic/") == "https://ntfy.sh/my-topic-reply"

    def test_remote_mode_not_activated_without_notify(self, tmp_path):
        """notify=False → remote mode is off even with ntfy configured."""
        config = _make_ntfy_config(tmp_path)
        assert not _is_remote_mode(False, config)

    def test_remote_mode_not_activated_without_ntfy(self, tmp_path):
        """Non-ntfy backend → remote mode is off."""
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
            retry=RetryPolicy(),
            notifications=NotificationConfig(backend="none"),
        )
        assert not _is_remote_mode(True, config)

    def test_remote_mode_activated_with_ntfy(self, tmp_path):
        """notify=True + ntfy backend + NtfyConfig → remote mode is on."""
        config = _make_ntfy_config(tmp_path)
        assert _is_remote_mode(True, config)

    def test_remote_approve(self, tmp_path):
        """ntfy poll returns 'approve' → task reaches DONE."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch(
                "theforge.coordinator.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator._ntfy_publish"),
            patch(
                "theforge.coordinator._ntfy_poll_reply",
                return_value=("approve", None),
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        assert result.success
        assert result.phase == Phase.DONE
        assert result.state.human_review_decision == "approve"
        assert result.state.human_review_mode == "remote"

    def test_remote_escalate(self, tmp_path):
        """ntfy poll returns 'escalate' → task reaches ESCALATE."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch(
                "theforge.coordinator.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator._ntfy_publish"),
            patch(
                "theforge.coordinator._ntfy_poll_reply",
                return_value=("escalate", None),
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        assert not result.success
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "escalate"

    def test_remote_timeout(self, tmp_path):
        """ntfy poll times out → auto-escalate + timeout notification."""
        config = _make_ntfy_config(tmp_path, timeout=60)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        ntfy_calls: list[tuple] = []

        def capture_ntfy(url, title, body, **kwargs):
            ntfy_calls.append((url, title, body))

        with (
            patch(
                "theforge.coordinator.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator._ntfy_publish", side_effect=capture_ntfy),
            patch(
                "theforge.coordinator._ntfy_poll_reply",
                return_value=("timeout", None),
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        assert not result.success
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "timeout"
        # Timeout notification should have been sent
        timeout_notifs = [c for c in ntfy_calls if "timed out" in c[1].lower()]
        assert len(timeout_notifs) >= 1

    def test_remote_reject_with_findings(self, tmp_path):
        """ntfy poll returns reject with findings → findings fed back to dev, then approve."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dev_prompts: list[str] = []

        def dev_side_effect(**kwargs):
            dev_prompts.append(kwargs.get("prompt", ""))
            # call 1: preflight; call 2: dev (first run); call 3: dev after reject
            if len(dev_prompts) == 1:
                return _make_agent_result(output=PREFLIGHT_PROCEED, cost_usd=0.05)
            return _make_agent_result(output="Done.")

        approve_result = _make_pool_result([APPROVE_REVIEW], ["review"])

        poll_calls: list[int] = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            poll_calls.append(1)
            if len(poll_calls) == 1:
                return ("reject", "fix the error handling")
            return ("approve", None)  # second human review approves

        with (
            patch("theforge.coordinator.run_agent", side_effect=dev_side_effect),
            patch("theforge.coordinator.run_agent_pool", return_value=approve_result),
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator._ntfy_publish"),
            patch("theforge.coordinator._ntfy_poll_reply", side_effect=poll_side_effect),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        # Flow: preflight → dev1 → review(APPROVE) → human(reject)
        #       → dev2 → review(APPROVE) → human(approve) → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.human_review_decision == "approve"
        # dev ran at least 3 times: preflight + dev1 + dev-after-reject
        assert len(dev_prompts) >= 3
        # Rejection text "fix the error handling" must appear in the post-reject dev prompt
        post_reject_prompts = " ".join(dev_prompts[2:])
        assert "fix the error handling" in post_reject_prompts

    def test_remote_extend_grants_cycle(self, tmp_path):
        """ntfy poll returns 'extend' → fresh dev+review budget granted."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        poll_calls = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            poll_calls.append(1)
            if len(poll_calls) == 1:
                return ("extend", None)
            return ("approve", None)

        dev_calls = []

        def dev_side_effect(**kwargs):
            dev_calls.append(1)
            if len(dev_calls) == 1:
                return _make_agent_result(output=PREFLIGHT_PROCEED, cost_usd=0.05)
            return _make_agent_result(output="Done.")

        with (
            patch("theforge.coordinator.run_agent", side_effect=dev_side_effect),
            patch(
                "theforge.coordinator.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator._ntfy_publish"),
            patch("theforge.coordinator._ntfy_poll_reply", side_effect=poll_side_effect),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        # extend → extra_cycles incremented
        assert result.state.human_review_extra_cycles >= 1
        assert result.state.human_review_mode == "remote"


class TestNtfyPollReply:
    """Unit tests for _ntfy_poll_reply() — mock urlopen/time to avoid real I/O."""

    def _make_resp(self, lines: list[str]):
        """Return a fake context-manager response whose read() returns the given lines."""
        content = "\n".join(lines).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = content
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_poll_returns_approve_immediately(self):
        """Single 'approve' message in response → returns ('approve', None)."""
        resp = self._make_resp(['{"event":"message","message":"approve"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("approve", None)

    def test_poll_returns_extend(self):
        """'extend' message → returns ('extend', None)."""
        resp = self._make_resp(['{"event":"message","message":"extend"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("extend", None)

    def test_poll_returns_escalate(self):
        """'escalate' message → returns ('escalate', None)."""
        resp = self._make_resp(['{"event":"message","message":"escalate"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("escalate", None)

    def test_poll_reject_with_findings(self):
        """'reject: fix the bug' → returns ('reject', 'fix the bug')."""
        resp = self._make_resp(['{"event":"message","message":"reject: fix the bug"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("reject", "fix the bug")

    def test_poll_reject_empty_findings(self):
        """'reject:' with no trailing text → findings is None."""
        resp = self._make_resp(['{"event":"message","message":"reject:"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("reject", None)

    def test_poll_uses_since_parameter(self):
        """Verify the URL contains poll=1&since=<ts>."""
        captured_urls: list[str] = []

        resp = self._make_resp(['{"event":"message","message":"approve"}'])

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return resp

        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", side_effect=fake_urlopen),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)

        assert len(captured_urls) >= 1
        assert "poll=1" in captured_urls[0]
        assert "since=1700000000" in captured_urls[0]

    def test_poll_ignores_unknown_messages(self):
        """Unknown message on first response, valid on second → returns valid decision."""
        resp1 = self._make_resp(['{"event":"message","message":"unknown-action"}'])
        resp2 = self._make_resp(['{"event":"message","message":"approve"}'])
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            return resp1 if call_count == 1 else resp2

        # deadline=60s; first poll at t=0 < 60; sleep; second poll at t=1 < 60; returns
        monotonic_vals = iter([0.0, 0.0, 1.0, 1.0, 1.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", side_effect=fake_urlopen),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)

        assert result == ("approve", None)
        assert call_count == 2

    def test_poll_timeout_when_no_reply(self):
        """monotonic advances past deadline → returns ('timeout', None)."""
        # call 1: deadline = monotonic() + 60 → deadline=60
        # call 2: while monotonic() < 60 → True, enter loop
        # urlopen raises → sleep calc: call 3 (returns 10, so sleep(10))
        # call 4: while monotonic() < 60 → 61 >= 60 → exit loop
        monotonic_vals = iter([0.0, 0.0, 10.0, 61.0])
        with (
            patch(
                "theforge.coordinator.urllib.request.urlopen",
                side_effect=Exception("no data"),
            ),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("timeout", None)

    def test_poll_sleeps_10_seconds_between_polls(self):
        """time.sleep is called with ~10 seconds when deadline is far away."""
        resp_empty = self._make_resp([""])
        resp_approve = self._make_resp(['{"event":"message","message":"approve"}'])
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            return resp_empty if call_count == 1 else resp_approve

        sleep_args: list[float] = []

        # t=0 (deadline check), t=0 (after failed parse, compute sleep), t=1 (loop check), t=1, t=1
        monotonic_vals = iter([0.0, 0.0, 1.0, 1.0, 1.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", side_effect=fake_urlopen),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep", side_effect=lambda s: sleep_args.append(s)),
        ):
            _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)

        assert len(sleep_args) >= 1
        # sleep should be capped at 10s; with deadline=60 and t=0, remaining=60 → sleep=10
        assert sleep_args[0] == pytest.approx(10.0)

    def test_poll_skips_non_message_events(self):
        """ntfy keepalive/open events (event != 'message') are ignored."""
        resp = self._make_resp(
            [
                '{"event":"open","message":""}',
                '{"event":"keepalive","message":""}',
                '{"event":"message","message":"approve"}',
            ]
        )
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("approve", None)


# ── Gate Override Tests ───────────────────────────────────────────────


def _make_task_with_gate_override(tmp_path: Path, gate_override: str | None) -> TaskSpec:
    """Create a test task with a gate_override set."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskSpec(
        name="Test Task",
        spec_path=spec,
        slug="test-task",
        file_scope=["src/"],
        gate_override=gate_override,
    )


class TestGateOverride:
    """Tests for spec-level gate override feature."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_override_none_skips_validation(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """gate_override='none' skips validation; no gate subprocess is run."""
        config = _make_config(tmp_path)
        task = _make_task_with_gate_override(tmp_path, "none")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        gate_calls: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            # Track any gate-related shell calls
            if "gate" in cmd:
                gate_calls.append(cmd)
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # No gate command should have been run
        assert gate_calls == [], f"Gate was called unexpectedly: {gate_calls}"
        # PASS should have been recorded
        assert "PASS" in result.state.gate_decisions

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_override_custom_command(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """gate_override='make lint' runs that command instead of global gate."""
        config = _make_config(tmp_path)
        task = _make_task_with_gate_override(tmp_path, "make lint")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        called_cmds: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            called_cmds.append(cmd)
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            # Custom gate succeeds with exit 0 (exit-code mode)
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # "make lint" should have been called
        assert any("make lint" in c for c in called_cmds), (
            f"make lint not called; cmds={called_cmds}"
        )
        # Global gate_command ("make gate") should NOT have been called
        assert not any("make gate" in c for c in called_cmds), (
            f"Global gate was called unexpectedly: {called_cmds}"
        )

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_override_custom_command_fail(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Custom gate command returning non-zero exit code produces FAIL and triggers retry."""
        config = _make_config(tmp_path)
        task = _make_task_with_gate_override(tmp_path, "make lint")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "make lint" in cmd:
                # Simulate lint failure (non-zero exit → FAIL in exit-code mode)
                return (False, "lint error: style violations found")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented."),
            _make_agent_result(success=True, output="Fixed lint."),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # Gate always fails → dev retried → max_dev_iterations exhausted → ESCALATE
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert any(d == "FAIL" for d in result.state.gate_decisions)

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_override_absent_uses_global(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """No gate_override → uses config.validation.gate_command (backward compat)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)  # no gate_override (None by default)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        assert task.gate_override is None

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE

    def test_gate_override_parsed_from_frontmatter(self, tmp_path):
        """parse_spec_frontmatter reads 'gate' key and it maps to gate_override on TaskSpec."""
        from theforge.task import parse_spec_frontmatter

        spec = tmp_path / "spec.md"
        spec.write_text(
            "---\nname: My Spec\nslug: my-spec\ngate: none\n---\n\n# Body",
            encoding="utf-8",
        )

        fm = parse_spec_frontmatter(spec)
        assert fm.get("gate") == "none"

        # Build TaskSpec with the parsed gate value
        task = TaskSpec(
            name=fm.get("name", "My Spec"),
            spec_path=spec,
            slug=fm.get("slug", "my-spec"),
            file_scope=fm.get("file_scope", []),
            gate_override=fm.get("gate"),
        )
        assert task.gate_override == "none"

    def test_gate_override_non_string_stripped_from_frontmatter(self, tmp_path):
        """R3: non-string gate values are stripped by parse_spec_frontmatter (type safety)."""
        from theforge.task import parse_spec_frontmatter

        spec = tmp_path / "spec.md"
        # gate: 123 is a YAML integer, not a string
        spec.write_text(
            "---\nname: My Spec\nslug: my-spec\ngate: 123\n---\n\n# Body",
            encoding="utf-8",
        )

        fm = parse_spec_frontmatter(spec)
        # Non-string gate must be stripped to avoid AttributeError in _is_gate_skip
        assert "gate" not in fm

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_override_none_case_insensitive(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """gate_override='None' and 'NONE' both trigger skip mode."""
        for override_value in ("None", "NONE"):
            config = _make_config(tmp_path)
            task = _make_task_with_gate_override(tmp_path, override_value)
            workspace = tmp_path / "test-task"
            workspace.mkdir(exist_ok=True)

            gate_calls: list[str] = []

            def shell_side_effect(cmd, cwd, **kwargs):
                if "gate" in cmd:
                    gate_calls.append(cmd)
                    _write_handoff(Path(cwd), "PASS")
                    return (True, "OK")
                if "git status --porcelain" in cmd:
                    return (True, "")
                stale_resp = _handle_stale_check_cmd(cmd)
                if stale_resp is not None:
                    return stale_resp
                return (True, "OK")

            mock_shell.side_effect = shell_side_effect
            mock_agent.side_effect = _preflight_then(
                _make_agent_result(success=True, output="Implemented.")
            )
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]

            result = run_task(config, task)

            assert result.success is True, f"Failed for gate_override={override_value!r}"
            assert gate_calls == [], (
                f"Gate was called for override={override_value!r}: {gate_calls}"
            )
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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        smart_config_models=["claude/sonnet", "claude/opus", "openai/gpt-5.4"],
    )


class TestComplexityAdaptation:
    def test_medium_no_change(self, tmp_path):
        """medium complexity → config unchanged."""
        config = _make_smart_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "medium")
        assert adapted is config

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

    def test_complexity_ignored_with_explicit_profiles(self, tmp_path):
        """No smart_config_models → complexity is a no-op."""
        config = _make_config(tmp_path)  # classic config, smart_config_models=None
        adapted = _apply_complexity_adaptation(config, "small")
        assert adapted is config  # unchanged

    def test_small_single_pool_drops_synthesis_only(self, tmp_path):
        """small with pool of 1 → just drops synthesis (no model change)."""
        from dataclasses import replace

        config = _make_smart_config(tmp_path)
        one_pool = replace(config, review_pool=[config.review_pool[0]])
        adapted = _apply_complexity_adaptation(one_pool, "small")
        assert len(adapted.review_pool) == 1
        assert adapted.synthesis_profile is None

    def test_large_already_strongest_no_change(self, tmp_path):
        """large complexity when dev is already strongest → config unchanged."""
        from dataclasses import replace

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

        def fake_run_agent(prompt, profile, working_dir, session_id=None):
            if profile.name == "preflight":
                return _make_agent_result(output=preflight_large)
            if profile.name == "synthesis":
                # 3-model config: large keeps 2 reviewers, synthesis runs
                return _make_agent_result(output=APPROVE_REVIEW)
            return _make_agent_result()

        pool_names = [p.name for p in config.review_pool]
        with (
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.run_agent", side_effect=fake_run_agent),
            patch(
                "theforge.coordinator.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW, APPROVE_REVIEW], pool_names),
            ),
        ):
            result = run_task(config, task)

        assert result.state.preflight_complexity == "large"

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

        def fake_run_agent(prompt, profile, working_dir, session_id=None):
            if profile.name == "preflight":
                return _make_agent_result(output=preflight_small)
            return _make_agent_result()

        def fake_run_pool(prompt, profiles, working_dir):
            pool_calls.append([p.name for p in profiles])
            return _make_pool_result([APPROVE_REVIEW], [profiles[0].name])

        with (
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.run_agent", side_effect=fake_run_agent),
            patch("theforge.coordinator.run_agent_pool", side_effect=fake_run_pool),
        ):
            run_task(config, task)

        # Pool should have been called with only 1 reviewer
        assert len(pool_calls) == 1
        assert len(pool_calls[0]) == 1


class TestLargeComplexitySynthesisP1:
    """P1 fix: large complexity must materialize synthesis even for 2-model pool."""

    def test_large_2_model_pool_creates_synthesis(self, tmp_path):
        """large with 2-model config (synthesis=None) → synthesis is created."""
        from dataclasses import replace

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
        from dataclasses import replace

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

    def test_large_2_model_synthesis_runs_in_coordinator(self, tmp_path):
        """Synthesis gate must not skip synthesis for 1-reviewer large-complexity pool.

        When large complexity materializes synthesis_profile for a 2-model config,
        the coordinator must invoke synthesis (not skip due to pool_size == 1).
        """
        from dataclasses import replace

        config = _make_smart_config(tmp_path)
        # Simulate 2-model smart-config after large-complexity adaptation:
        # review_pool has 1 reviewer but synthesis_profile is set
        two_model_large = replace(
            config,
            review_pool=[config.review_pool[0]],
            synthesis_profile=config.synthesis_profile,
        )
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
        synthesis_called: list[bool] = []

        def fake_run_agent(prompt, profile, working_dir, session_id=None):
            if profile.name == "preflight":
                return _make_agent_result(output=preflight_large)
            if profile.name == "synthesis":
                synthesis_called.append(True)
                return _make_agent_result(output=APPROVE_REVIEW)
            return _make_agent_result()

        with (
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.run_agent", side_effect=fake_run_agent),
            patch(
                "theforge.coordinator.run_agent_pool",
                return_value=_make_pool_result(
                    [APPROVE_REVIEW], [two_model_large.review_pool[0].name]
                ),
            ),
        ):
            run_task(two_model_large, task)

        assert synthesis_called, "synthesis must run for 1-reviewer pool with synthesis_profile set"  # noqa: E501


class TestComplexityParsedForAllPreflightsP1:
    """P1 fix: complexity parsed on all successful preflights, not just smart config."""

    def test_complexity_stored_for_classic_config(self, tmp_path):
        """Complexity stored in preflight_complexity even when smart_config_models is None."""
        config = _make_config(tmp_path)  # classic config, no smart_config_models
        assert config.smart_config_models is None
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
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch(
                "theforge.coordinator.run_agent",
                side_effect=[
                    _make_agent_result(output=preflight_medium),  # preflight
                    _make_agent_result(),  # dev
                ],
            ),
            patch(
                "theforge.coordinator.run_agent_pool",
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

        def fake_run_pool(prompt, profiles, working_dir):
            pool_profiles_used.extend(p.name for p in profiles)
            return _make_pool_result([APPROVE_REVIEW], [profiles[0].name])

        with (
            patch("theforge.coordinator._run_shell", side_effect=_shell_with_gate(workspace)),
            patch(
                "theforge.coordinator.run_agent",
                side_effect=[
                    _make_agent_result(output=preflight_large),  # preflight
                    _make_agent_result(),  # dev
                ],
            ),
            patch("theforge.coordinator.run_agent_pool", side_effect=fake_run_pool),
        ):
            result = run_task(config, task)

        # Complexity captured
        assert result.state.preflight_complexity == "large"
        # But dev model unchanged (classic config not swapped)
        assert result.state.dev_results[0].success  # dev ran normally
        # Pool called with original single reviewer (no synthesis was added)
        assert len(pool_profiles_used) == 1
