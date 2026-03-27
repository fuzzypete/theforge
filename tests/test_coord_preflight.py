"""Tests for preflight phase, complexity adaptation, and dev model escalation."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_ALREADY_DONE,
    PREFLIGHT_BLOCKED,
    PREFLIGHT_PROCEED_MEDIUM,
    PREFLIGHT_PROCEED_SMALL,
    PREFLIGHT_PROCEED_WITH_WARNINGS,
    REQUEST_CHANGES_REVIEW,
    _make_agent_result,
    _make_config,
    _make_plan_config,
    _make_pool_result,
    _make_task,
    _preflight_then,
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
from theforge.coordinator.engine import (
    Phase,
    _apply_complexity_adaptation,
    _escalate_dev_model,
    _has_persistent_p1,
    _parse_preflight_complexity,
    _parse_preflight_warnings,
    _persistent_p1_descriptions,
    generate_audit_log,
    run_task,
)
from theforge.review import ReviewFinding

# ── Preflight phase tests ─────────────────────────────────────────────


class TestCoordinatorPreflight:
    """Test the PREFLIGHT phase: classify spec before expensive dev cycles."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_proceed_continues_to_dev(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
        """PROCEED verdict → normal dev→validate→review flow."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    @patch("theforge.coordinator.engine.has_review_approve", return_value=True)
    def test_preflight_already_done_skips_dev(
        self, mock_approve, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
    ):
        """ALREADY_DONE verdict with prior APPROVE → DONE immediately, no dev or review."""
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_blocked_escalates(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_agent_failure_proceeds(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
        """If the preflight agent itself fails, fail-open to PROCEED."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        preflight_fail = _make_agent_result(success=False, output="CLI error", cost_usd=0.0)
        dev_ok = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [preflight_fail, dev_ok]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"
        assert "failed" in result.state.preflight_reason.lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_unparseable_proceeds(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = [preflight_garbage, dev_ok]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.preflight_verdict == "PROCEED"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_cost_in_audit(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
        """Preflight cost appears in audit log."""
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_file_path_warning_proceeds(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
    ):
        """Missing file paths in story → warning stored in state, PROCEED continues to dev."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_agent.side_effect = [
            _make_agent_result(
                success=True,
                output=PREFLIGHT_PROCEED_WITH_WARNINGS,
                cost_usd=0.05,
                profile_name="review",
            ),
            _make_agent_result(success=True, output="Implemented."),
        ]
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

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            if profile.name == "preflight":
                return _make_agent_result(output=preflight_large)
            if profile.name == "synthesis":
                # 3-model config: large keeps 2 reviewers, synthesis runs
                return _make_agent_result(output=APPROVE_REVIEW)
            return _make_agent_result()

        pool_names = [p.name for p in config.review_pool]
        with (
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.engine.run_agent", side_effect=fake_run_agent),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
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

        def fake_run_agent(prompt, profile, working_dir, session_id=None, **kwargs):
            if profile.name == "preflight":
                return _make_agent_result(output=preflight_small)
            return _make_agent_result()

        def fake_run_pool(prompt, profiles, working_dir, session_ids=None, **kwargs):
            pool_calls.append([p.name for p in profiles])
            return _make_pool_result([APPROVE_REVIEW], [profiles[0].name])

        with (
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.engine.run_agent", side_effect=fake_run_agent),
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
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=[
                    _make_agent_result(output=preflight_medium),  # preflight
                    _make_agent_result(),  # dev
                ],
            ),
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
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=[
                    _make_agent_result(output=preflight_large),  # preflight
                    _make_agent_result(),  # dev
                ],
            ),
            patch("theforge.coordinator.review_pool.run_agent_pool", side_effect=fake_run_pool),
        ):
            result = run_task(config, task)

        # Complexity captured
        assert result.state.preflight_complexity == "large"
        # But dev model unchanged (classic config not swapped)
        assert result.state.dev_results[0].success  # dev ran normally
        # Pool called with original single reviewer (no synthesis was added)
        assert len(pool_profiles_used) == 1


# ── Dev model escalation tests ────────────────────────────────────────


def _make_review_finding(
    severity: str = "P1",
    file: str = "src/cli.py",
    description: str = "cli.py never wires gate_override into TaskStory",
) -> ReviewFinding:
    return ReviewFinding(
        severity=severity, file=file, line=None, description=description, suggestion=None
    )


# Redefine _make_smart_config for escalation tests (2-model config, different signature)
def _make_smart_config(  # noqa: F811
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


class TestDevModelEscalationIntegration:
    """Integration tests for dev model escalation on persistent P1s."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_swaps_dev_model(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
        """Persistent P1 across 2 cycles → dev model swapped to opus on next iteration."""
        config = _make_smart_config(tmp_path, max_review_cycles=3)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(),  # dev iter 1 (cycle 1)
            _make_agent_result(),  # dev iter 1 (cycle 2, after persistent P1)
            _make_agent_result(),  # dev iter 1 (cycle 3)
        )

        # Track profiles used for dev (non-preflight) calls
        dev_profiles: list[str] = []
        agent_call_count = {"n": 0}
        original_side_effect = mock_agent.side_effect

        def tracking_agent(**kwargs):
            agent_call_count["n"] += 1
            if agent_call_count["n"] > 1:  # skip preflight (call 1)
                dev_profiles.append(kwargs["profile"].model)
            result = original_side_effect(**kwargs)
            return result

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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_max_once(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
        """Escalation happens at most once per run, even with multiple persistent P1 cycles."""
        config = _make_smart_config(tmp_path, max_review_cycles=5)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = _preflight_then(*[_make_agent_result() for _ in range(6)])

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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_only_with_smart_config(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
        """Classic config (no smart_config_models) → no escalation even with persistent P1."""
        config = _make_config(tmp_path)
        # Verify no smart_config_models
        assert config.smart_config_models is None

        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(),  # dev cycle 1
            _make_agent_result(),  # dev cycle 2 (if reached)
        )

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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_skipped_when_budget_exhausted(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
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
        mock_agent.side_effect = _preflight_then(expensive_dev)

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


# ── PLAN phase tests ──────────────────────────────────────────────────


class TestPlanPhase:
    """Tests for the PLAN phase (implementation planning between PREFLIGHT and DEV)."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_runs_for_medium_complexity(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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
        results = [preflight_result, plan_result, dev_result]

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
        # PREFLIGHT + PLAN + DEV = 3 run_agent calls
        assert mock_agent.call_count == 3
        # plan_output is stored on state
        assert result.state.plan_output is not None
        assert "Implementation Plan" in result.state.plan_output
        # .forge/plan.md written to workspace
        assert (workspace / ".forge" / "plan.md").exists()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_skipped_for_small_complexity(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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
        results = [preflight_result, dev_result]

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
        # PREFLIGHT + DEV only (no PLAN) = 2 run_agent calls
        assert mock_agent.call_count == 2
        assert result.state.plan_output is None
        assert not (workspace / ".forge" / "plan.md").exists()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_skipped_when_disabled(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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
        results = [preflight_result, dev_result]

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
        # PREFLIGHT + DEV only (plan disabled) = 2 run_agent calls
        assert mock_agent.call_count == 2
        assert result.state.plan_output is None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_failure_escalates(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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
        results = [preflight_result, plan_result]

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
        # DEV should NOT have run (only preflight + plan = 2 agent calls)
        assert mock_agent.call_count == 2
        # Review pool should NOT have run
        assert mock_pool.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_cost_included_in_total_cost(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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
        results = [preflight_result, plan_result, dev_result]

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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_not_rerun_on_dev_retry(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
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
        results = [preflight_result, plan_result, dev_result, dev_result]

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
        # PREFLIGHT(1) + PLAN(1) + DEV(1) + DEV-retry(1) = 4 calls; no second PLAN
        assert mock_agent.call_count == 4
        # plan_output is still the original plan (from first run)
        assert result.state.plan_output is not None
        assert "Implementation Plan" in result.state.plan_output

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_copies_file_and_skips_agent(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
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
        results = [preflight_result, dev_result]

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
        # PREFLIGHT + DEV only — no plan agent (2 calls)
        assert mock_agent.call_count == 2
        assert result.state.plan_output == plan_content
        assert result.state.plan_results == []
        assert (workspace / ".forge" / "plan.md").read_text(encoding="utf-8") == plan_content

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_missing_file_aborts_before_workspace(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_unreadable_file_aborts_before_workspace(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
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
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_injection_non_utf8_file_aborts_before_workspace(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path
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
        curr = [_make_review_finding(description="Off by one error")]
        prev = [_make_review_finding(description="Missing validation")]
        result = _persistent_p1_descriptions(curr, prev)
        assert result == []

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
        from theforge.coordinator.engine import CoordinatorState
        from theforge.coordinator.phases import _append_cycle_history
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
        from theforge.coordinator.engine import CoordinatorState, CycleHistory
        from theforge.coordinator.phases import _append_cycle_history
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
        from theforge.coordinator.engine import CoordinatorState
        from theforge.coordinator.phases import _append_cycle_history
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
        from theforge.coordinator.engine import CoordinatorState
        from theforge.coordinator.phases import _append_cycle_history
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
        from theforge.coordinator.engine import CoordinatorState
        from theforge.coordinator.phases import _append_cycle_history
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


class TestApprovePathCycleHistory:
    """Integration tests verifying APPROVE path records cycle history."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_approve_records_cycle_in_history(self, mock_shell, mock_agent, mock_plan_agent, mock_pool, tmp_path):
        """Non-interactive APPROVE run records the approved cycle in state.cycle_history."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert len(result.state.cycle_history) == 1
        assert result.state.cycle_history[0].verdict == "APPROVE"
        assert result.state.cycle_history[0].cycle == 1


class TestEscalationNoteOnRejectPath:
    """Integration test: escalation note is delivered on reject-after-escalation path."""

    @patch("theforge.coordinator.engine._human_review")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_note_in_prompt_after_reject(
        self, mock_shell, mock_agent, mock_plan_agent, mock_pool, mock_human_review, tmp_path
    ):
        """Persistent P1 + exhausted cycles + human reject: next dev prompt has escalation note."""
        config = _make_smart_config(tmp_path, max_review_cycles=2)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Capture prompts passed to dev agent (skip preflight call index 0)
        captured_prompts: list[str] = []
        agent_call_count = {"n": 0}

        def agent_side_effect(**kwargs):
            agent_call_count["n"] += 1
            if agent_call_count["n"] > 1:  # skip preflight
                captured_prompts.append(kwargs.get("prompt", ""))
            return _make_agent_result(success=True, output="Done.")

        mock_plan_agent.side_effect = mock_agent
        mock_agent.side_effect = agent_side_effect

        # Cycle 1 + Cycle 2: same P1 → persistent P1 fires on cycle 2, exhausted
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
            # After reject: approve so the run completes
            return [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
            ]

        mock_pool.side_effect = pool_side_effect

        # Cycle 2 exhausted → human review: reject once, then approve
        human_review_call = {"n": 0}

        def human_review_side_effect(*args, **kwargs):
            human_review_call["n"] += 1
            if human_review_call["n"] == 1:
                return ("reject", "Start fresh with the escalated model.")
            return ("approve", None)

        mock_human_review.side_effect = human_review_side_effect

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.dev_escalated is True

        # Flow: prompts[0]=initial dev, prompts[1]=after cycle-1 (build_fix_prompt),
        # prompts[2]=after reject (build_dev_prompt with escalation_note)
        assert len(captured_prompts) >= 3, f"Expected >=3 dev prompts, got {len(captured_prompts)}"
        post_reject_prompt = captured_prompts[2]  # dev call after exhausted+reject
        assert "MODEL ESCALATION" in post_reject_prompt, (
            "Escalation note missing from dev prompt after reject"
        )
        assert "Previous Review Cycles" in post_reject_prompt, (
            "Cycle history missing from dev prompt after reject"
        )
