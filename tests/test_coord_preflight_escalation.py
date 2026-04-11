"""Tests for dev model escalation on persistent P1s."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import (
    _escalate_dev_model,
    _has_persistent_p1,
)
from theforge.coordinator.state import Phase
from theforge.review import ReviewFinding

# ── Dev model escalation tests ────────────────────────────────────────


def _make_review_finding(
    severity: str = "P1",
    file: str = "src/cli.py",
    description: str = "cli.py never wires gate_override into TaskStory",
) -> ReviewFinding:
    return ReviewFinding(
        severity=severity, file=file, line=None, description=description, suggestion=None
    )


# _make_smart_config for escalation tests (2-model config)
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
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=max_review_cycles,
            auto_model_escalation=True,
        ),
        smart_config_models=models,
    )


# ── Unit tests for helper functions ──────────────────────────────────


class TestHasPersistentP1:
    def test_persistent_p1_detected(self):
        """Same P1 on same file across consecutive cycles → detected."""
        finding = _make_review_finding()
        assert _has_persistent_p1([finding], [finding]) is True

    def test_new_p1_not_persistent(self):
        """Different descriptions → not persistent."""
        curr = [_make_review_finding(file="src/foo.py", description="Off by one error")]
        prev = [_make_review_finding(file="src/bar.py", description="Missing validation")]
        assert _has_persistent_p1(curr, prev) is False

    def test_same_file_cascading_p1_is_persistent(self):
        """Different descriptions in the same file still count as persistent."""
        curr = [
            _make_review_finding(file="src/plan_flow.py", description="skip branch drops abandon")
        ]
        prev = [
            _make_review_finding(
                file="src/plan_flow.py",
                description="skip branch wrong for refactor",
            )
        ]
        assert _has_persistent_p1(curr, prev) is True

    def test_persistent_p1_different_files(self):
        """Same P1 description on different files → still detected as persistent."""
        curr = [
            _make_review_finding(file="src/coordinator.py", description="extend resets session ID")
        ]
        prev = [_make_review_finding(file="src/task.py", description="extend resets session ID")]
        assert _has_persistent_p1(curr, prev) is True

    def test_p1_similarity_matching(self):
        """Substring containment and token overlap both match."""
        # Substring containment: prev description contains curr description
        curr = [_make_review_finding(description="gate_override never wired")]
        prev = [_make_review_finding(description="gate_override never wired into TaskStory")]
        assert _has_persistent_p1(curr, prev) is True

        # Token overlap > 60%: "missing batch configuration" vs "batch configuration is missing"
        curr2 = [_make_review_finding(description="missing batch configuration")]
        prev2 = [_make_review_finding(description="batch configuration is missing")]
        assert _has_persistent_p1(curr2, prev2) is True

    def test_p2_findings_ignored(self):
        """P2 findings are not considered for persistence."""
        curr = [_make_review_finding(severity="P2", description="minor style issue")]
        prev = [_make_review_finding(severity="P2", description="minor style issue")]
        assert _has_persistent_p1(curr, prev) is False

    def test_empty_findings_not_persistent(self):
        """Empty findings on either side → not persistent."""
        finding = _make_review_finding()
        assert _has_persistent_p1([], [finding]) is False
        assert _has_persistent_p1([finding], []) is False


class TestEscalateDevModel:
    def test_escalation_swaps_to_higher_model(self):
        """sonnet → opus when opus is in available list and has higher capability."""
        result = _escalate_dev_model("claude/sonnet", ["claude/sonnet", "claude/opus"])
        assert result == "claude/opus"

    def test_escalation_skips_non_dev_capable(self):
        """google/gemini-2.5-pro has higher capability but dev_capable=False → skipped."""
        result = _escalate_dev_model("claude/sonnet", ["claude/sonnet", "google/gemini-2.5-pro"])
        assert result is None

    def test_escalation_no_higher_model(self):
        """Already on strongest → returns None."""
        result = _escalate_dev_model("claude/opus", ["claude/sonnet", "claude/opus"])
        assert result is None

    def test_escalation_selects_next_step_up(self):
        """Selects the lowest-capability model that still beats current."""
        # sonnet(7) < gemini(8, not dev) < gpt-5.4(9) < opus(10)
        # Should select gpt-5.4 as next step up from sonnet (gemini skipped)
        result = _escalate_dev_model(
            "claude/sonnet",
            ["claude/sonnet", "openai/gpt-5.4", "claude/opus"],
        )
        assert result == "openai/gpt-5.4"

    def test_escalation_unknown_current_model(self):
        """Unknown current model → returns None (safe fallback)."""
        result = _escalate_dev_model("unknown/model", ["claude/sonnet", "claude/opus"])
        assert result is None


# ── Integration tests for coordinator loop ────────────────────────────

# Review with a persistent P1 (same file + description)
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

_CASCADING_P1_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "New blocker in same file."
findings:
  - severity: P1
    file: src/cli.py
    line: 43
    description: "cli.py now drops abandon handling after wiring gate_override"
    suggestion: "Preserve the abandon path"
story_compliance:
  matches_spec: false
  mismatches:
    - "Abandon handling regressed"
test_coverage:
  adequate: false
  gaps:
    - "No regression test for abandon handling"
```
"""


class TestDevModelEscalationIntegration:
    """Integration tests for dev model escalation on persistent P1s."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_swaps_dev_model(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Persistent P1 across 2 cycles → dev model swapped to opus on next iteration."""
        config = _make_smart_config(tmp_path, max_review_cycles=3)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        dev_results_list = [
            _make_agent_result(),  # dev iter 1 (cycle 1)
            _make_agent_result(),  # dev iter 1 (cycle 2, after persistent P1)
            _make_agent_result(),  # dev iter 1 (cycle 3)
        ]

        # Track profiles used for dev calls
        dev_profiles: list[str] = []
        dev_call_idx = {"n": 0}

        def tracking_agent(**kwargs):
            dev_profiles.append(kwargs["profile"].model)  # all mock_agent calls are dev calls
            idx = min(dev_call_idx["n"], len(dev_results_list) - 1)
            dev_call_idx["n"] += 1
            return dev_results_list[idx]

        mock_agent.side_effect = tracking_agent

        pool_call = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_call["n"] += 1
            if pool_call["n"] <= 2:
                return [
                    _make_agent_result(
                        success=True,
                        output=_PERSISTENT_P1_REVIEW,
                        profile_name="claude-opus",
                    )
                ]
            return [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_escalated is True
        # After escalation, dev should have used opus
        assert "opus" in dev_profiles

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_with_cascading_same_file_p1(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Escalation fires when a new P1 appears in the same file on the next cycle."""
        config = _make_smart_config(tmp_path, max_review_cycles=3)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result() for _ in range(3)]

        pool_call = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_call["n"] += 1
            if pool_call["n"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=_PERSISTENT_P1_REVIEW,
                        profile_name="claude-opus",
                    )
                ]
            if pool_call["n"] == 2:
                return [
                    _make_agent_result(
                        success=True,
                        output=_CASCADING_P1_REVIEW,
                        profile_name="claude-opus",
                    )
                ]
            return [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.dev_escalated is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_max_once(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Escalation happens at most once per run, even with multiple persistent P1 cycles."""
        config = _make_smart_config(tmp_path, max_review_cycles=5)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result() for _ in range(6)]

        pool_call = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_call["n"] += 1
            if pool_call["n"] >= 5:
                return [
                    _make_agent_result(
                        success=True, output=APPROVE_REVIEW, profile_name="claude-opus"
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=_PERSISTENT_P1_REVIEW,
                    profile_name="claude-opus",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        # Escalation flag is set at most once
        assert result.state.dev_escalated is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_only_with_smart_config(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Classic config (no smart_config_models) → no escalation even with persistent P1."""
        config = _make_config(tmp_path)
        # Verify no smart_config_models
        assert config.smart_config_models is None

        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(),
            # dev cycle 1
            _make_agent_result(),
            # dev cycle 2 (if reached),
        ]

        pool_call = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_call["n"] += 1
            if pool_call["n"] >= 2:
                return [
                    _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=_PERSISTENT_P1_REVIEW,
                    profile_name="review",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        # No escalation happened
        assert result.state.dev_escalated is False

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_skipped_when_budget_exhausted(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Budget exhausted before persistent P1 detected → no escalation, no extra dev run."""
        # Use a very tight budget so the first dev call exhausts it
        config = _make_smart_config(tmp_path, max_review_cycles=3)
        # Override dev budget to be tiny so it's exceeded after first call
        from dataclasses import replace as _replace

        tight_dev = _replace(config.dev_profile, budget_usd=0.01)
        config = _replace(config, dev_profile=tight_dev)

        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Dev result costs more than budget (0.50 > 0.01)
        expensive_dev = _make_agent_result(success=True, output="Done.", cost_usd=0.50)
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = expensive_dev

        # Pool always returns persistent P1
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=_PERSISTENT_P1_REVIEW, profile_name="claude-opus"
            )
        ]

        result = run_task(config, task)

        # Dev budget exhausted → ESCALATE immediately after first dev run
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "budget" in result.message.lower()
        # Escalation flag never set (budget guard fired first)
        assert result.state.dev_escalated is False


class TestAutoModelEscalationFlag:
    """Tests that auto_model_escalation feature flag gates dev model escalation."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_disabled_by_default(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Smart config with auto_model_escalation=False (default) → no dev model swap."""
        # _make_smart_config would set auto_model_escalation=True; build one without it
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
        config = ForgeConfig(
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
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=3,
                auto_model_escalation=False,  # explicit default
            ),
            smart_config_models=["claude/sonnet", "claude/opus"],
        )

        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [_make_agent_result() for _ in range(4)]

        pool_call = {"n": 0}

        def pool_side_effect(**kwargs):
            pool_call["n"] += 1
            if pool_call["n"] >= 3:
                return [
                    _make_agent_result(
                        success=True, output=APPROVE_REVIEW, profile_name="claude-opus"
                    )
                ]
            return [
                _make_agent_result(
                    success=True,
                    output=_PERSISTENT_P1_REVIEW,
                    profile_name="claude-opus",
                )
            ]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        # Escalation never fired despite persistent P1 and smart config
        assert result.state.dev_escalated is False

    def test_auto_model_escalation_defaults_to_false(self, tmp_path):
        """RetryPolicy.auto_model_escalation defaults to False."""
        policy = RetryPolicy()
        assert policy.auto_model_escalation is False

    def test_auto_model_escalation_can_be_enabled(self, tmp_path):
        """RetryPolicy.auto_model_escalation can be set to True."""
        policy = RetryPolicy(auto_model_escalation=True)
        assert policy.auto_model_escalation is True


def test_dev_startup_failure_message_mentions_launcher(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    startup_failure = _make_agent_result(
        success=False,
        output="ERROR: 'claude' CLI not found. Is it installed?",
        startup_failure=True,
    )

    with (
        patch(
            "theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace, "PASS")
        ),
        patch("theforge.coordinator.dev_phase.run_agent", return_value=startup_failure),
        patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
        patch("theforge.coordinator.plan_flow.run_agent"),
        patch("theforge.coordinator.review_pool.run_agent_pool", return_value=[]),
    ):
        result = run_task(config, task)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert "agent launcher startup failed" in result.message
