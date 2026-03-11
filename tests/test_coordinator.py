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


def _shell_with_gate(workspace: Path, decisions: list[str] | str = "PASS"):
    """Create a _run_shell side_effect that writes handoff.yaml during gate execution.

    With the stale-handoff fix, _run_gate() deletes handoff before running the gate
    command, so tests must produce handoff *during* (not before) gate execution.
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
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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
        mock_agent.return_value = _make_agent_result()
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
        mock_agent.return_value = _make_agent_result()

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
        mock_agent.return_value = _make_agent_result()
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
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()


class TestCoordinatorSchemaErrorOverride:
    """Test that APPROVE with schema errors is overridden to REQUEST_CHANGES."""

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
        mock_agent.return_value = _make_agent_result()

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
        assert result.state.review_cycle == 2  # had to retry due to schema override


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

        mock_agent.return_value = dev_result
        mock_pool.return_value = [review_result]

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
        mock_agent.return_value = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.50,
            exit_code=0,
            raw={},
            profile_name="dev",
        )

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

        mock_agent.return_value = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )

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

        mock_agent.return_value = dev_result
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
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
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
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Should mention stale handoff or gate failure
        assert "handoff" in result.message.lower() or "gate" in result.message.lower()


class TestCoordinatorSchemaErrorOnRequestChanges:
    """Test that malformed REQUEST_CHANGES also gets flagged as parse error."""

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coordinator._run_shell")
    def test_malformed_request_changes_flagged(self, mock_shell, mock_agent, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")

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

        # The first review had parse errors — its summary should be prefixed
        first_review = result.state.review_results[0]
        assert first_review.summary.startswith("PARSE ERROR:")


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
            review_pool=[single_profile],
            synthesis_profile=None,  # pool of 1 → no synthesis
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="solo"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # run_agent (synthesis) should NOT have been called (only DEV was)
        assert mock_agent.call_count == 1

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
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=False, output="TIMEOUT", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # Only DEV called run_agent; synthesis was skipped
        assert mock_agent.call_count == 1

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
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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
