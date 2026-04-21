"""Tests for config loading — TestHybridRunnerConfig, TestLoadConfig, TestAllowedToolsConfig."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import (
    API_PROVIDER_DEFAULT_TOOLS,
    DEFAULT_DEV_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_WORKSPACE,
    ModelProfile,
    load_config,
)
from theforge.config.profiles import (
    _apply_profile_overrides,
    _parse_profile,
)


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


class TestPlanAgentReviewProvider:
    def test_plan_agent_review_provider(self, tmp_path):
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "provider": "openai",
                    "model": "o4-mini",
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        assert config.plan_agent_review.provider == "openai"
        assert config.plan_agent_review.cli is None

    def test_plan_agent_review_provider_missing_sdk_raises(self, tmp_path):
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "provider": "google",
                    "model": "gemini-1.5-pro",
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test"}),
            patch("importlib.import_module", side_effect=ImportError),
        ):
            with pytest.raises(ValueError, match="SDK 'google.genai' is not installed"):
                load_config(config_path)


class TestLoadConfig:
    def test_minimal_config(self, tmp_path):
        config_path = _write_config({"project": "test-project"}, tmp_path)
        config = load_config(config_path)
        assert config.project == "test-project"
        assert config.project_root == tmp_path
        assert config.dev_profile == DEFAULT_DEV_PROFILE
        assert config.review_profile == DEFAULT_REVIEW_PROFILE
        assert config.review_pool == [DEFAULT_REVIEW_PROFILE]
        assert config.synthesis_profile is None

    def test_custom_workspace(self, tmp_path):
        config_path = _write_config(
            {
                "workspace": {
                    "create_command": "my-custom-cmd {slug}",
                    "path_pattern": "workspaces/{slug}",
                    "branch_pattern": "feat/{slug}",
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.workspace.create_command == "my-custom-cmd {slug}"
        assert config.workspace.path_pattern == "workspaces/{slug}"

    def test_custom_retry(self, tmp_path):
        config_path = _write_config(
            {"retry": {"max_dev_iterations": 5, "max_review_cycles": 4}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.retry.max_dev_iterations == 5
        assert config.retry.max_review_cycles == 4

    def test_auto_model_escalation_defaults_false(self, tmp_path):
        config_path = _write_config({"retry": {}}, tmp_path)
        config = load_config(config_path)
        assert config.retry.auto_model_escalation is False

    def test_auto_model_escalation_parsed_true(self, tmp_path):
        config_path = _write_config(
            {"retry": {"auto_model_escalation": True}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.retry.auto_model_escalation is True

    def test_empty_file(self, tmp_path):
        config_path = tmp_path / "forge.yaml"
        config_path.write_text("", encoding="utf-8")
        config = load_config(config_path)
        assert config.project == tmp_path.name  # falls back to dir name
        assert config.workspace == DEFAULT_WORKSPACE

    def test_project_root_derived_from_config_path(self, tmp_path):
        sub = tmp_path / "nested" / "dir"
        sub.mkdir(parents=True)
        config_path = _write_config({"project": "nested"}, sub)
        config = load_config(config_path)
        assert config.project_root == sub

    def test_plan_review_defaults_disabled(self, tmp_path):
        config_path = _write_config({"project": "test-project"}, tmp_path)
        config = load_config(config_path)
        assert config.plan_review.enabled is False

    def test_plan_review_enabled_parsed(self, tmp_path):
        config_path = _write_config({"plan_review": {"enabled": True}}, tmp_path)
        config = load_config(config_path)
        assert config.plan_review.enabled is True

    def test_plan_review_mode_and_timeout_parsed(self, tmp_path):
        config_path = _write_config(
            {"plan_review": {"enabled": True, "mode": "advisory", "timeout_seconds": 120}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan_review.mode == "advisory"
        assert config.plan_review.timeout_seconds == 120

    def test_plan_review_mode_defaults_blocking(self, tmp_path):
        config_path = _write_config({"plan_review": {"enabled": True}}, tmp_path)
        config = load_config(config_path)
        assert config.plan_review.mode == "blocking"
        assert config.plan_review.timeout_seconds == 14400

    def test_plan_cli_field_accepted(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": False, "cli": "claude", "model": "opus"}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan.cli == "claude"
        assert config.plan.model == "opus"

    def test_plan_provider_marks_non_default(self, tmp_path):
        # Regression: plan.provider set without plan.cli/model must not be treated as default.
        config_path = _write_config(
            {
                "plan": {"enabled": False, "provider": "openai", "model": "gpt-4o"},
            },
            tmp_path,
        )
        with patch("theforge.config.load.check_agent_auth", return_value=(True, "")):
            config = load_config(config_path)
        assert config.plan_model_is_default is False
        assert config.plan.provider == "openai"
        assert config.plan.model == "gpt-4o"

    def test_plan_unknown_cli_raises(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": True, "cli": "unknown-cli"}},
            tmp_path,
        )
        with pytest.raises(ValueError, match="Unsupported CLI"):
            load_config(config_path)

    def test_plan_disabled_unknown_cli_ok(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": False, "cli": "unknown-cli"}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan.enabled is False

    def test_plan_cli_and_provider_both_set_ok(self, tmp_path):
        """Both cli and provider may coexist in plan config; transport is dispatch truth."""
        config_path = _write_config(
            {"plan": {"enabled": True, "cli": "claude", "provider": "openai"}},
            tmp_path,
        )
        # load_config must not raise for XOR: the invariant is removed as an
        # operator-enforced constraint. Provider credentials and SDK availability
        # are still validated eagerly by load_config, so supply a stub key and
        # short-circuit the SDK import check (CI installs .[dev] without SDKs).
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        assert config.plan.enabled is True

    def test_plan_agent_review_defaults_disabled(self, tmp_path):
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.plan_agent_review.enabled is False

    def test_plan_agent_review_enabled_parsed(self, tmp_path):
        config_path = _write_config(
            {"plan_agent_review": {"enabled": True, "cli": "claude", "model": "opus"}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan_agent_review.enabled is True
        assert config.plan_agent_review.cli == "claude"
        assert config.plan_agent_review.model == "opus"

    def test_plan_agent_review_unsupported_cli_raises(self, tmp_path):
        config_path = _write_config(
            {"plan_agent_review": {"enabled": True, "cli": "bogus"}},
            tmp_path,
        )
        with pytest.raises(ValueError, match="Unsupported CLI.*bogus.*plan_agent_review"):
            load_config(config_path)

    def test_plan_agent_review_disabled_unsupported_cli_ok(self, tmp_path):
        """Unsupported CLI is not validated when plan_agent_review is disabled."""
        config_path = _write_config(
            {"plan_agent_review": {"enabled": False, "cli": "bogus"}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan_agent_review.enabled is False

    def test_plan_agent_review_pool_format_loads(self, tmp_path):
        """AC-1: pool format loads all entries as ModelProfile objects."""
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {
                            "name": "opus-plan-reviewer",
                            "cli": "claude",
                            "model": "opus",
                            "budget_usd": 2.00,
                            "timeout_seconds": 600,
                            "allowed_tools": ["Read", "Bash", "Glob", "Grep"],
                        },
                        {
                            "name": "sonnet-plan-reviewer",
                            "cli": "claude",
                            "model": "haiku",
                            "budget_usd": 1.00,
                            "timeout_seconds": 300,
                            "allowed_tools": ["Read", "Glob", "Grep"],
                        },
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        par = config.plan_agent_review
        assert par.enabled is True
        assert len(par.pool) == 2
        assert par.pool[0].name == "opus-plan-reviewer"
        assert par.pool[0].model == "opus"
        assert par.pool[0].budget_usd == pytest.approx(2.00)
        assert par.pool[1].name == "sonnet-plan-reviewer"
        assert par.pool[1].model == "haiku"

    def test_plan_agent_review_pool_profiles_property(self, tmp_path):
        """AC-7: profiles property returns pool list when pool is non-empty."""
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {
                            "name": "reviewer-a",
                            "cli": "claude",
                            "model": "opus",
                            "budget_usd": 2.00,
                            "timeout_seconds": 600,
                        },
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        profiles = config.plan_agent_review.profiles
        assert len(profiles) == 1
        assert profiles[0].name == "reviewer-a"
        assert profiles[0].model == "opus"

    def test_plan_agent_review_legacy_format_profiles_property(self, tmp_path):
        """AC-1/AC-7: legacy single-profile format converts to pool of one via profiles."""
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "cli": "claude",
                    "model": "opus",
                    "budget_usd": 2.00,
                    "timeout": 600,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        par = config.plan_agent_review
        assert par.pool == []  # legacy: no pool entries
        profiles = par.profiles
        assert len(profiles) == 1
        assert profiles[0].model == "opus"
        assert profiles[0].budget_usd == pytest.approx(2.00)

    def test_plan_agent_review_pool_provider_entry(self, tmp_path):
        """AC-1: pool entries with provider are accepted."""
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {
                            "name": "openai-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                            "budget_usd": 1.00,
                            "timeout_seconds": 120,
                        },
                    ],
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        par = config.plan_agent_review
        assert len(par.pool) == 1
        assert par.pool[0].provider == "openai"
        assert par.pool[0].cli is None

    def test_plan_agent_review_pool_empty_list_raises(self, tmp_path):
        """pool: [] (empty) is rejected."""
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [],
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="plan_agent_review.pool must be a non-empty list"):
            load_config(config_path)

    def test_plan_agent_review_pool_duplicate_names_raises(self, tmp_path):
        """Duplicate names in pool entries raises ValueError."""
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {"name": "dup", "cli": "claude", "model": "sonnet"},
                        {"name": "dup", "cli": "claude", "model": "opus"},
                    ],
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="Duplicate names in plan_agent_review.pool"):
            load_config(config_path)

    def test_plan_agent_review_pool_rejects_same_model_as_planner(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {"model": "sonnet"},
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {"name": "reviewer-a", "cli": "claude", "model": "sonnet"},
                    ],
                },
            },
            tmp_path,
        )

        with pytest.raises(ValueError, match="model 'sonnet'"):
            load_config(config_path)

    def test_plan_agent_review_legacy_rejects_same_model_as_planner(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {"model": "sonnet"},
                "plan_agent_review": {
                    "enabled": True,
                    "cli": "claude",
                    "model": "sonnet",
                },
            },
            tmp_path,
        )

        with pytest.raises(ValueError, match="model 'sonnet'"):
            load_config(config_path)

    def test_plan_agent_review_allows_different_model_than_planner(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {"model": "sonnet"},
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {"name": "reviewer-a", "cli": "claude", "model": "opus"},
                    ],
                },
            },
            tmp_path,
        )

        config = load_config(config_path)
        assert config.plan_agent_review.pool[0].model == "opus"

    def test_plan_agent_review_legacy_provider_profile_uses_api_default_tools(self, tmp_path):
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "provider": "openai",
                    "model": "o4-mini",
                },
            },
            tmp_path,
        )

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)

        profiles = config.plan_agent_review.profiles
        assert len(profiles) == 1
        assert profiles[0].provider == "openai"
        assert profiles[0].allowed_tools == API_PROVIDER_DEFAULT_TOOLS


class TestAllowedToolsConfig:
    def test_parse_profile_provider_without_allowed_tools_uses_read_only_defaults(self):
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            profile = _parse_profile(
                "api-reviewer",
                {"provider": "openai", "model": "o4-mini"},
                role="review",
            )

        assert profile.allowed_tools == API_PROVIDER_DEFAULT_TOOLS

    def test_apply_profile_overrides_provider_without_allowed_tools_uses_read_only_defaults(self):
        base = ModelProfile(
            name="reviewer",
            cli="codex",
            provider=None,
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )

        overridden = _apply_profile_overrides(
            base,
            {"provider": "openai", "model": "o4-mini"},
        )

        assert overridden.provider == "openai"
        assert overridden.allowed_tools == API_PROVIDER_DEFAULT_TOOLS

    def test_apply_profile_overrides_provider_switch_clears_cli_and_retargets_transport(self):
        """Overriding a CLI base with 'provider' must flip dispatch to API transport."""
        base = ModelProfile(
            name="reviewer",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )
        # Sanity: base is CLI-dispatched
        assert base.transport is not None
        assert base.transport.kind == "cli"

        overridden = _apply_profile_overrides(
            base,
            {"provider": "anthropic", "model": "sonnet"},
        )

        assert overridden.cli is None
        assert overridden.provider == "anthropic"
        assert overridden.transport is not None
        assert overridden.transport.kind == "api"
        assert overridden.transport.runner == "anthropic"

    def test_apply_profile_overrides_cli_switch_clears_provider_and_retargets_transport(self):
        """Overriding an API base with 'cli' must flip dispatch to CLI transport."""
        base = ModelProfile(
            name="reviewer",
            cli=None,
            provider="anthropic",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=API_PROVIDER_DEFAULT_TOOLS,
        )
        assert base.transport is not None
        assert base.transport.kind == "api"

        overridden = _apply_profile_overrides(base, {"cli": "claude", "model": "sonnet"})

        assert overridden.cli == "claude"
        assert overridden.provider is None
        assert overridden.transport is not None
        assert overridden.transport.kind == "cli"
        assert overridden.transport.executable == "claude"

    def test_apply_profile_overrides_cli_profile_keeps_existing_defaults(self):
        base = ModelProfile(
            name="reviewer",
            cli="claude",
            provider=None,
            model="opus",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )

        overridden = _apply_profile_overrides(base, {"model": "sonnet"})

        assert overridden.provider is None
        assert overridden.allowed_tools == DEFAULT_REVIEW_PROFILE.allowed_tools

    def test_parse_profile_reads_thinking_budget(self):
        with (
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            profile = _parse_profile(
                "gemini-reviewer",
                {
                    "provider": "google",
                    "model": "gemini-2.5-pro",
                    "thinking_budget": 2048,
                },
                role="review",
            )

        assert profile.thinking_budget == 2048

    def test_parse_profile_name_with_bracket_raises(self):
        """Profile names containing ']' are rejected at parse time."""
        with pytest.raises(ValueError, match=r"contains '\]'"):
            _parse_profile(
                "bad]name",
                {"cli": "claude", "model": "sonnet"},
                role="review",
            )

    def test_apply_profile_overrides_preserves_explicit_zero_thinking_budget(self):
        base = ModelProfile(
            name="gemini-reviewer",
            cli=None,
            provider="google",
            model="gemini-2.5-pro",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=API_PROVIDER_DEFAULT_TOOLS,
        )

        overridden = _apply_profile_overrides(base, {"thinking_budget": 0})

        assert overridden.thinking_budget == 0


class TestDefaultFlags:
    """Tests for plan_model_is_default and review_pool_is_default flags."""

    def test_plan_model_is_default_when_no_plan_key(self, tmp_path):
        """plan_model_is_default is True when forge.yaml has no plan key."""
        config_path = _write_config({"project": "p"}, tmp_path)
        config = load_config(config_path)
        assert config.plan_model_is_default is True

    def test_plan_model_is_default_false_when_plan_model_set(self, tmp_path):
        """plan_model_is_default is False when plan.model is explicitly configured."""
        config_path = _write_config({"plan": {"model": "claude-opus-4-5"}}, tmp_path)
        config = load_config(config_path)
        assert config.plan_model_is_default is False

    def test_plan_model_is_default_false_when_plan_cli_set(self, tmp_path):
        """plan_model_is_default is False when plan.cli is explicitly configured."""
        config_path = _write_config({"plan": {"cli": "claude"}}, tmp_path)
        config = load_config(config_path)
        assert config.plan_model_is_default is False

    def test_review_pool_is_default_when_no_review_pool(self, tmp_path):
        """review_pool_is_default is True when forge.yaml has no review_pool configured."""
        config_path = _write_config({"project": "p"}, tmp_path)
        config = load_config(config_path)
        assert config.review_pool_is_default is True

    def test_review_pool_is_default_false_when_explicitly_configured(self, tmp_path):
        """review_pool_is_default is False when overrides.review_pool is set."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "overrides": {
                    "review_pool": [{"name": "opus", "model": "opus"}],
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.review_pool_is_default is False


class TestConventionsConfig:
    def test_conventions_hard_parsed(self, tmp_path):
        """forge.yaml with conventions.hard section parses into HardConventionsConfig."""
        from theforge.config.types import HardConventionsConfig

        config_path = _write_config(
            {
                "conventions": {
                    "hard": {
                        "max_module_lines": 300,
                        "max_test_file_lines": 800,
                        "no_circular_imports": True,
                        "test_mirrors_source": False,
                    }
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert isinstance(config.conventions_hard, HardConventionsConfig)
        assert config.conventions_hard.max_module_lines == 300
        assert config.conventions_hard.max_test_file_lines == 800
        assert config.conventions_hard.no_circular_imports is True
        assert config.conventions_hard.test_mirrors_source is False

    def test_conventions_absent_is_none(self, tmp_path):
        """forge.yaml without conventions section yields conventions_hard=None."""
        config_path = _write_config({}, tmp_path)
        config = load_config(config_path)
        assert config.conventions_hard is None

    def test_conventions_hard_defaults(self, tmp_path):
        """conventions.hard with empty dict uses sensible defaults."""
        from theforge.config.types import HardConventionsConfig

        config_path = _write_config({"conventions": {"hard": {}}}, tmp_path)
        config = load_config(config_path)
        assert isinstance(config.conventions_hard, HardConventionsConfig)
        assert config.conventions_hard.max_module_lines == 500
        assert config.conventions_hard.max_test_file_lines == 1000
        assert config.conventions_hard.no_circular_imports is True
        assert config.conventions_hard.test_mirrors_source is True

    def test_conventions_hard_invalid_type_raises(self, tmp_path):
        """conventions.hard with wrong type raises ValueError."""
        config_path = _write_config(
            {"conventions": {"hard": {"max_module_lines": "not-an-int"}}},
            tmp_path,
        )
        with pytest.raises(ValueError, match="max_module_lines"):
            load_config(config_path)


class TestValidationTestCommand:
    """Tests for validation.test_command config field."""

    def test_test_command_parsed_when_present(self, tmp_path):
        config_path = _write_config(
            {
                "validation": {
                    "gate_command": "make gate",
                    "test_command": "pytest tests/ -v -n auto",
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.validation.test_command == "pytest tests/ -v -n auto"

    def test_test_command_defaults_to_none_when_absent(self, tmp_path):
        config_path = _write_config(
            {
                "validation": {
                    "gate_command": "make gate",
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.validation.test_command is None

    def test_gate_debug_timeout_parsed_when_present(self, tmp_path):
        config_path = _write_config(
            {
                "validation": {
                    "gate_command": "make gate",
                    "gate_debug_command": "pytest -x -v -n 0",
                    "gate_debug_timeout": 90,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.validation.gate_debug_command == "pytest -x -v -n 0"
        assert config.validation.gate_debug_timeout == 90

    def test_handoff_file_raises_value_error(self, tmp_path):
        """handoff_file and gate_decision_key are removed in v0.8 and must raise ValueError."""
        config_path = _write_config(
            {
                "validation": {
                    "gate_command": "make gate",
                    "handoff_file": "handoff.yaml",
                    "gate_decision_key": "gate_decision",
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="v0.8"):
            load_config(config_path)
