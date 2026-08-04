"""Phase configs compose ModelRef instead of duplicating transport fields (#751).

Covers the internal contract change end to end: the type shape itself, the
loader that normalizes flat forge.yaml keys into a ref, the notification-timeout
default parity that used to diverge silently, and the coordinator phase boundary
that projects the ref into the ModelProfile the runners dispatch on.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    _as_detailed,
    _make_agent_result,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelRef,
    RetryPolicy,
    WorkspaceConfig,
    load_config,
)
from theforge.config.bridge import model_ref_to_profile
from theforge.config.profiles import iter_plan_phase_profiles
from theforge.config.secrets import _parse_notifications
from theforge.config.types import (
    DEFAULT_HITL_TIMEOUT_SECONDS,
    NotificationConfig,
    PlanAgentReviewConfig,
    PlanConfig,
    TransportFallbackConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.util import resolve_timeout
from theforge.task import TaskStory

PREFLIGHT_PROCEED_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
complexity_score: 6
reason: "Medium spec."
spec_issues: []
criteria_checked: []
```
"""

APPROVE_REVIEW = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
ac_verification:
  - criterion: "Implementation satisfies the spec"
    status: VERIFIED
    evidence: "diff hunks + tests (fixture default)"
```
"""

# Transport settings that must live in the composed ModelRef, never as
# duplicated fields on the phase configs that wrap it.
_TRANSPORT_FIELDS = {"cli", "provider", "model", "budget_usd", "timeout", "api_fallback"}


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


def _field_names(obj) -> set[str]:
    return {f.name for f in dataclasses.fields(obj)}


class TestComposedShape:
    def test_plan_config_carries_no_transport_fields_of_its_own(self):
        names = _field_names(PlanConfig())
        assert names & _TRANSPORT_FIELDS == set()
        assert "ref" in names
        assert isinstance(PlanConfig().ref, ModelRef)

    def test_plan_agent_review_carries_no_transport_fields_of_its_own(self):
        names = _field_names(PlanAgentReviewConfig())
        assert names & _TRANSPORT_FIELDS == set()
        assert "ref" in names
        assert isinstance(PlanAgentReviewConfig().ref, ModelRef)

    def test_scalar_accessors_are_read_only_views_onto_the_ref(self):
        plan = PlanConfig.of(cli="claude", model="opus", budget_usd=3.0, timeout=900)
        assert (plan.cli, plan.model, plan.budget_usd, plan.timeout) == (
            "claude",
            "opus",
            3.0,
            900,
        )
        # There is one storage location, so replacing the ref moves every view.
        swapped = dataclasses.replace(
            plan, ref=dataclasses.replace(plan.ref, cli=None, provider="openai", model="gpt-5.4")
        )
        assert swapped.cli is None
        assert swapped.provider == "openai"
        assert swapped.model == "gpt-5.4"
        # …including the derived dispatch view, which cannot lag behind.
        assert swapped.mode == "api"
        assert swapped.transport is not None
        assert swapped.transport.kind == "api"

    def test_transport_fallback_is_the_leaf_atom_a_ref_contains(self):
        """TransportFallbackConfig cannot compose ModelRef — ModelRef contains it."""
        fallback_fields = _field_names(TransportFallbackConfig(provider="anthropic", model="opus"))
        assert "cli" not in fallback_fields
        assert "budget_usd" not in fallback_fields
        assert "api_fallback" in _field_names(
            ModelRef(model="opus", budget_usd=1.0, timeout_seconds=60)
        )


class TestLoaderNormalization:
    def test_flat_plan_keys_land_in_the_ref(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {
                    "enabled": True,
                    "cli": "claude",
                    "model": "opus",
                    "budget_usd": 2.5,
                    "timeout": 1200,
                    "timeout_medium": 1500,
                    "timeout_large": 2400,
                    "validate_spec": False,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        ref = config.plan.ref
        assert (ref.cli, ref.model, ref.budget_usd, ref.timeout_seconds) == (
            "claude",
            "opus",
            2.5,
            1200,
        )
        assert ref.timeout_medium_seconds == 1500
        assert ref.timeout_large_seconds == 2400
        assert ref.transport is not None and ref.transport.kind == "cli"
        # validate_spec is plan-specific, so it stays on the wrapper.
        assert config.plan.validate_spec is False
        assert "validate_spec" not in _field_names(ref)

    def test_flat_plan_agent_review_keys_land_in_the_ref(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {"enabled": True, "model": "opus"},
                "plan_agent_review": {
                    "enabled": True,
                    "cli": "claude",
                    "model": "sonnet",
                    "budget_usd": 1.25,
                    "timeout": 450,
                    "min_reviewers": 1,
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        ref = config.plan_agent_review.ref
        assert ref is not None
        assert (ref.cli, ref.model, ref.budget_usd, ref.timeout_seconds) == (
            "claude",
            "sonnet",
            1.25,
            450,
        )
        # The profiles view is built from the ref, not from a second copy.
        (profile,) = config.plan_agent_review.profiles
        assert (profile.cli, profile.model, profile.budget_usd, profile.timeout_seconds) == (
            "claude",
            "sonnet",
            1.25,
            450,
        )

    def test_auto_api_fallback_is_recorded_on_the_plan_ref(self, tmp_path):
        config_path = _write_config(
            {
                "models": ["openai/gpt-5.4/cli"],
                "plan": {"enabled": True, "cli": "codex", "model": "gpt-5.4"},
            },
            tmp_path,
        )
        with (
            patch("theforge.config.load.check_agent_auth", return_value=(True, "")),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        assert config.plan.ref.api_fallback is not None
        assert config.plan.ref.api_fallback.provider == "openai"
        # The accessor reads the same object — no second copy to fall stale.
        assert config.plan.api_fallback is config.plan.ref.api_fallback

    def test_plan_escalation_threshold_is_reachable_from_forge_yaml(self, tmp_path):
        """It drives escalation in plan_flow, so it must be operator-settable."""
        assert load_config(_write_config({}, tmp_path)).retry.plan_escalation_threshold == 2
        config_path = _write_config({"retry": {"plan_escalation_threshold": 5}}, tmp_path)
        assert load_config(config_path).retry.plan_escalation_threshold == 5


class TestNotificationTimeoutParity:
    def test_typed_default_matches_loader_default(self):
        """A default NotificationConfig and an unconfigured load must agree.

        These drifted (600 vs 14400): anyone reading the typed default saw a
        10-minute HITL window while live gates actually waited 4 hours.
        """
        loaded = _parse_notifications({}, {})
        assert (
            NotificationConfig().human_review_timeout_seconds
            == loaded.human_review_timeout_seconds
            == DEFAULT_HITL_TIMEOUT_SECONDS
        )

    def test_explicit_value_still_wins(self, tmp_path):
        config_path = _write_config(
            {"notifications": {"hitl_timeout_seconds": 900}},
            tmp_path,
        )
        assert load_config(config_path).notifications.human_review_timeout_seconds == 900


class TestPhaseBoundaryProjection:
    """The PLAN/PLAN_REVIEW seam: ref → ModelProfile is what actually dispatches."""

    def test_model_ref_to_profile_carries_the_full_transport(self):
        ref = ModelRef(
            cli="claude",
            model="opus",
            budget_usd=2.0,
            timeout_seconds=600,
            timeout_medium_seconds=900,
            timeout_large_seconds=1800,
            api_fallback=TransportFallbackConfig(provider="anthropic", model="claude-opus-4-6"),
            max_iterations=42,
        )
        profile = model_ref_to_profile("plan", ref, allowed_tools=("Read",), phase="plan")
        assert profile.name == "plan"
        assert profile.model == "opus"
        assert profile.budget_usd == 2.0
        assert profile.timeout_seconds == 600
        assert profile.timeout_medium_seconds == 900
        assert profile.timeout_large_seconds == 1800
        assert profile.max_iterations == 42
        assert profile.api_fallback is ref.api_fallback
        assert profile.transport is ref.transport
        assert profile.allowed_tools == ("Read",)
        assert profile.phase == "plan"

    def test_resolved_timeout_overrides_only_the_base_timeout(self):
        ref = ModelRef(cli="claude", model="opus", budget_usd=2.0, timeout_seconds=600)
        profile = model_ref_to_profile("plan", ref, timeout_seconds=1800)
        assert profile.timeout_seconds == 1800
        assert profile.budget_usd == 2.0

    def test_plan_dispatch_profile_follows_a_transport_swap_on_the_ref(self, tmp_path):
        """Swapping the ref's transport must reach the projected dispatch profile."""
        config_path = _write_config(
            {"plan": {"enabled": True, "cli": "claude", "model": "opus"}}, tmp_path
        )
        config = load_config(config_path)
        (_, before) = next(iter_plan_phase_profiles(config))
        assert before.mode == "cli"

        swapped = dataclasses.replace(
            config,
            plan=dataclasses.replace(
                config.plan,
                ref=dataclasses.replace(
                    config.plan.ref, cli=None, provider="openai", model="gpt-5.4"
                ),
            ),
        )
        (_, after) = next(iter_plan_phase_profiles(swapped))
        assert after.mode == "api"
        assert after.provider == "openai"
        assert after.model == "gpt-5.4"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_phase_dispatches_the_profile_projected_from_the_ref(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Seam: the profile PLAN actually dispatches is built from config.plan.ref.

        A transport swap written to the ref has to survive the preflight → PLAN
        handoff, or the coordinator would keep planning on the model the
        operator just replaced.
        """
        plan = PlanConfig.of(
            enabled=True,
            cli="claude",
            model="sonnet",
            budget_usd=0.50,
            timeout=300,
            validate_spec=False,
        )
        # Swap the transport by replacing the ref — the only writable location.
        plan = dataclasses.replace(
            plan,
            ref=dataclasses.replace(
                plan.ref, cli="codex", model="gpt-5.4-codex", budget_usd=3.0, timeout_seconds=600
            ),
        )
        config = _make_plan_seam_config(tmp_path, plan=plan)
        task = _make_seam_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(_shell_with_gate(workspace, "PASS"))
        mock_plan_agent.side_effect = mock_dev_agent
        mock_preflight.return_value = _make_agent_result(
            output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_dev_agent.side_effect = [
            _make_agent_result(output="# Plan\n\nDo it.", cost_usd=0.10),
            _make_agent_result(output="Implemented.", cost_usd=0.20),
        ]
        mock_pool.return_value = [_make_agent_result(output=APPROVE_REVIEW, profile_name="review")]

        result = run_task(config, task)
        assert result.success is True

        call = mock_dev_agent.call_args_list[0]
        dispatched = call[1]["profile"] if "profile" in call[1] else call[0][1]
        assert dispatched.model == "gpt-5.4-codex"
        assert dispatched.cli == "codex"
        assert dispatched.transport is not None
        assert dispatched.transport.runner == "codex"
        assert dispatched.budget_usd == 3.0
        # The ref's base timeout (600) is still what complexity scaling starts
        # from — medium applies the standard headroom factor on top of it.
        assert dispatched.timeout_seconds == resolve_timeout(
            config.plan.ref.timeout_seconds,
            config.plan.ref.timeout_medium_seconds,
            config.plan.ref.timeout_large_seconds,
            "medium",
            6,
        )
        assert dispatched.timeout_seconds != config.plan.ref.timeout_seconds


def _make_plan_seam_config(tmp_path: Path, *, plan: PlanConfig) -> ForgeConfig:
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
        plan=plan,
        log=LogConfig(enabled=False),
    )


def _make_seam_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")
