"""Tests for the coordinator state machine.

Uses mocked runner to test all state transitions without real agent calls.
"""

from pathlib import Path
from unittest.mock import patch

import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import Phase, run_task
from theforge.runner import AgentResult
from theforge.task import TaskSpec

# ── Fixtures ─────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config pointing at tmp_path."""
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
        review_profile=DEFAULT_REVIEW_PROFILE,
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
    success: bool = True, output: str = "Done.", session_id: str | None = "sess-1"
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=session_id,
        cost_usd=0.50,
        exit_code=0 if success else 1,
        raw={},
    )


def _write_handoff(workspace: Path, decision: str = "PASS") -> None:
    """Write a minimal handoff.yaml in the workspace."""
    handoff = {
        "gate_decision": decision,
        "validation": {"make_fmt": {"status": "PASS"}},
        "scope_completed": ["test item"],
    }
    (workspace / "handoff.yaml").write_text(yaml.dump(handoff), encoding="utf-8")


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


# ── Tests ────────────────────────────────────────────────────────────


class TestCoordinatorHappyPath:
    """Test the golden path: dev succeeds, gate passes, review approves."""

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_single_pass(self, mock_shell, mock_agent, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Mock: workspace creation succeeds (already exists)
        # Mock: gate command succeeds
        mock_shell.return_value = (True, "OK")

        # Write handoff after "gate" runs
        _write_handoff(workspace, "PASS")

        # Mock agent calls: dev then review
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented."),
            _make_agent_result(success=True, output=APPROVE_REVIEW),
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

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_fail_then_pass(self, mock_shell, mock_agent, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")

        # First dev run: gate returns FAIL, second: PASS
        call_count = {"dev": 0}

        def agent_side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            if "reviewing" in prompt.lower() or "reviewer" in prompt.lower():
                return _make_agent_result(output=APPROVE_REVIEW)
            call_count["dev"] += 1
            if call_count["dev"] == 1:
                _write_handoff(workspace, "FAIL")
            else:
                _write_handoff(workspace, "PASS")
            return _make_agent_result()

        mock_agent.side_effect = agent_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.dev_iteration == 2  # needed a retry


class TestCoordinatorReviewRequestChanges:
    """Test that review REQUEST_CHANGES loops back to dev."""

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_review_then_approve(self, mock_shell, mock_agent, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        _write_handoff(workspace, "PASS")

        # Sequence: dev → review(reject) → dev → review(approve)
        call_count = {"agent": 0}

        def agent_side_effect(**kwargs):
            call_count["agent"] += 1
            prompt = kwargs.get("prompt", "")
            if "reviewing" in prompt.lower() or "reviewer" in prompt.lower():
                if call_count["agent"] <= 2:
                    return _make_agent_result(output=REQUEST_CHANGES_REVIEW)
                return _make_agent_result(output=APPROVE_REVIEW)
            _write_handoff(workspace, "PASS")
            return _make_agent_result()

        mock_agent.side_effect = agent_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.review_cycle == 2


class TestCoordinatorEscalation:
    """Test that exhausting retries escalates to human."""

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_gate_exhaustion(self, mock_shell, mock_agent, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        # Gate always fails
        _write_handoff(workspace, "FAIL")
        mock_agent.return_value = _make_agent_result()

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 2  # hit max

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_review_exhaustion(self, mock_shell, mock_agent, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        _write_handoff(workspace, "PASS")

        def agent_side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            if "reviewing" in prompt.lower() or "reviewer" in prompt.lower():
                return _make_agent_result(output=REQUEST_CHANGES_REVIEW)
            _write_handoff(workspace, "PASS")
            return _make_agent_result()

        mock_agent.side_effect = agent_side_effect

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()


class TestCoordinatorSchemaErrorOverride:
    """Test that APPROVE with schema errors is overridden to REQUEST_CHANGES."""

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_approve_with_schema_errors_triggers_retry(self, mock_shell, mock_agent, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        _write_handoff(workspace, "PASS")

        # Review YAML that says APPROVE but is missing required fields
        # (no spec_compliance or test_coverage → schema errors)
        malformed_approve = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
```
"""
        call_count = {"agent": 0}

        def agent_side_effect(**kwargs):
            call_count["agent"] += 1
            prompt = kwargs.get("prompt", "")
            if "reviewing" in prompt.lower() or "reviewer" in prompt.lower():
                if call_count["agent"] <= 2:
                    # First review: malformed APPROVE (should be overridden)
                    return _make_agent_result(output=malformed_approve)
                # Second review: proper APPROVE
                return _make_agent_result(output=APPROVE_REVIEW)
            _write_handoff(workspace, "PASS")
            return _make_agent_result()

        mock_agent.side_effect = agent_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.review_cycle == 2  # had to retry due to schema override


class TestCoordinatorCostTracking:
    """Test that both dev and review costs are tracked."""

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_total_cost_includes_review(self, mock_shell, mock_agent, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        _write_handoff(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.75,
            exit_code=0,
            raw={},
        )
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.90,  # within default review budget of $1.00
            exit_code=0,
            raw={},
        )

        mock_agent.side_effect = [dev_result, review_result]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.total_dev_cost == 0.75
        assert result.state.total_review_cost == 0.90
        assert result.state.total_cost == 1.65


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
            review_profile=review_profile,
            retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=3),
        )

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_dev_budget_exceeded_first_call(self, mock_shell, mock_agent, tmp_path):
        """Dev agent exceeds budget on first call → ESCALATE with budget error."""
        config = self._make_budget_config(tmp_path, dev_budget=0.40, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")

        # Agent costs $0.50, budget is $0.40 → immediate escalation
        mock_agent.return_value = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.50,
            exit_code=0,
            raw={},
        )

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "0.5000" in result.message
        assert "0.4000" in result.message
        # Only one dev invocation — escalated before retry
        assert len(result.state.dev_results) == 1

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_dev_budget_exceeded_on_retry(self, mock_shell, mock_agent, tmp_path):
        """Dev agent exceeds budget on second call (retry) → ESCALATE."""
        # Budget of $0.60 allows first call ($0.30) but not second ($0.60 cumulative)
        config = self._make_budget_config(tmp_path, dev_budget=0.50, review_budget=1.00)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")

        call_count = {"dev": 0}

        def agent_side_effect(**kwargs):
            call_count["dev"] += 1
            if call_count["dev"] == 1:
                # First call: gate fails so we retry
                _write_handoff(workspace, "FAIL")
            else:
                _write_handoff(workspace, "PASS")
            return AgentResult(
                success=True,
                output="Done.",
                session_id="s1",
                cost_usd=0.30,
                exit_code=0,
                raw={},
            )

        mock_agent.side_effect = agent_side_effect

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        # Two dev invocations: $0.30 + $0.30 = $0.60 > $0.50
        assert len(result.state.dev_results) == 2

    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_review_budget_exceeded(self, mock_shell, mock_agent, tmp_path):
        """Review agent exceeds budget → ESCALATE with budget error."""
        config = self._make_budget_config(tmp_path, dev_budget=2.00, review_budget=0.40)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.return_value = (True, "OK")
        _write_handoff(workspace, "PASS")

        dev_result = AgentResult(
            success=True, output="Done.", session_id="s1", cost_usd=0.10, exit_code=0, raw={}
        )
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.50,  # exceeds $0.40 budget
            exit_code=0,
            raw={},
        )

        mock_agent.side_effect = [dev_result, review_result]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        assert "0.5000" in result.message
        assert "0.4000" in result.message
        assert len(result.state.review_agent_results) == 1


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
