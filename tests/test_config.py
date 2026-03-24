"""Tests for config loading."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_WORKSPACE,
    MODEL_REGISTRY,
    SUPPORTED_CLIS,
    ConfigError,
    _auto_assign_models,
    _resolve_model_info,
    generate_default_config,
    load_config,
)


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


class TestHybridRunnerConfig:
    def test_provider_profile_loads(self, tmp_path):
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "api-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        profile = config.review_pool[0]
        assert profile.provider == "openai"
        assert profile.cli is None
        assert profile.mode == "api"

    def test_deepseek_profile_parses(self, tmp_path):
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "deepseek-reviewer",
                            "provider": "deepseek",
                            "model": "deepseek-r1",
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        profile = config.review_pool[0]
        assert profile.provider == "deepseek"
        assert profile.model == "deepseek-r1"
        assert profile.cli is None
        assert profile.mode == "api"

    def test_deepseek_local_base_url_no_api_key_no_error(self, tmp_path):
        """A local base_url override should not require DEEPSEEK_API_KEY."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "deepseek-local",
                            "provider": "deepseek",
                            "model": "deepseek-r1",
                            "base_url": "http://localhost:11434",
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        profile = config.review_pool[0]
        assert profile.provider == "deepseek"
        assert profile.base_url == "http://localhost:11434"

    def test_cli_profile_loads(self, tmp_path):
        config_path = _write_config(
            {"profiles": {"dev": {"cli": "claude", "model": "sonnet", "budget_usd": 2.0}}},
            tmp_path,
        )
        config = load_config(config_path)
        profile = config.dev_profile
        assert profile.cli == "claude"
        assert profile.provider is None
        assert profile.mode == "cli"

    def test_mutual_exclusion_cli_provider_raises(self, tmp_path):
        config_path = _write_config(
            {
                "profiles": {
                    "dev": {
                        "cli": "claude",
                        "provider": "openai",
                        "model": "sonnet",
                        "budget_usd": 2.0,
                    }
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="cannot have both 'cli' and 'provider'"):
            load_config(config_path)

    def test_neither_cli_nor_provider_uses_default(self, tmp_path):
        config_path = _write_config(
            {"profiles": {"dev": {"model": "sonnet", "budget_usd": 2.0}}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.cli == "claude"
        assert config.dev_profile.provider is None

    def test_allowed_tools_on_api_profile_now_passes(self, tmp_path):
        """API profiles with allowed_tools are now valid — surfaced via the agent loop."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "api-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                            "allowed_tools": ["Read"],
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        profile = config.review_pool[0]
        assert profile.provider == "openai"
        # Normalized at parse time: "Read" → "read_file"
        assert "read_file" in profile.allowed_tools
        assert "Read" not in profile.allowed_tools

    def test_allowed_tools_normalized_for_api_profiles(self, tmp_path):
        """API profiles normalize capitalized tool names to canonical internal names."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "api-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                            "allowed_tools": ["Read", "Bash", "Grep", "Glob"],
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        profile = config.review_pool[0]
        assert set(profile.allowed_tools) == {"read_file", "bash", "grep", "glob"}

    def test_allowed_tools_not_normalized_for_cli_profiles(self, tmp_path):
        """CLI profiles keep capitalized tool names so --allowedTools argument stays correct."""
        config_path = _write_config(
            {"profiles": {"dev": {"cli": "claude", "model": "sonnet", "budget_usd": 2.0}}},
            tmp_path,
        )
        config = load_config(config_path)
        # Default CLI dev profile uses capitalized names
        assert "Read" in config.dev_profile.allowed_tools
        assert "read_file" not in config.dev_profile.allowed_tools

    def test_missing_sdk_raises(self, tmp_path):
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "api-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch("importlib.import_module", side_effect=ImportError),
        ):
            with pytest.raises(ValueError, match="SDK 'openai' is not installed"):
                load_config(config_path)

    def test_missing_api_key_raises(self, tmp_path):
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "api-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with patch.dict("os.environ", clear=True), patch("importlib.import_module"):
            with pytest.raises(ValueError, match=r"\$OPENAI_API_KEY is not set"):
                load_config(config_path)

    def test_plan_agent_review_provider(self, tmp_path):
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "provider": "openai",
                    "model": "o4-mini",
                    "budget_usd": 1.0,
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
                    "budget_usd": 1.0,
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

    def test_custom_profiles(self, tmp_path):
        config_path = _write_config(
            {
                "project": "custom",
                "profiles": {
                    "dev": {"model": "opus", "budget_usd": 5.0},
                    "review": {
                        "cli": "claude",
                        "model": "haiku",
                        "timeout_seconds": 60,
                        "budget_usd": 1.0,
                    },
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.model == "opus"
        assert config.dev_profile.budget_usd == 5.0
        assert config.review_profile.model == "haiku"
        assert config.review_profile.timeout_seconds == 60

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

    def test_plan_cli_and_model_use_normalized_fields(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {
                    "enabled": True,
                    "cli": "claude",
                    "model": "opus",
                    "budget_usd": 3.0,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan.cli == "claude"
        assert config.plan.model == "opus"

    def test_plan_model_name_deprecated_but_normalized(self, tmp_path, caplog):
        config_path = _write_config(
            {
                "plan": {
                    "enabled": True,
                    "model": "claude",
                    "model_name": "claude-opus-4-6",
                    "budget_usd": 3.0,
                }
            },
            tmp_path,
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            config = load_config(config_path)
        assert config.plan.cli == "claude"
        assert config.plan.model == "claude-opus-4-6"
        assert any("plan.model_name is deprecated" in r.message for r in caplog.records)

    def test_plan_unknown_cli_raises_config_error(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": True, "cli": "bogus", "model": "opus", "budget_usd": 1.0}},
            tmp_path,
        )
        with pytest.raises(ConfigError, match="Unsupported CLI.*plan"):
            load_config(config_path)

    def test_plan_missing_budget_raises_config_error(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": True, "cli": "claude", "model": "opus"}},
            tmp_path,
        )
        with pytest.raises(ConfigError, match="plan must set budget_usd explicitly"):
            load_config(config_path)

    def test_plan_agent_review_defaults_disabled(self, tmp_path):
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.plan_agent_review.enabled is False

    def test_plan_agent_review_enabled_parsed(self, tmp_path):
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "cli": "claude",
                    "model": "sonnet",
                    "budget_usd": 1.0,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan_agent_review.enabled is True
        assert config.plan_agent_review.cli == "claude"
        assert config.plan_agent_review.model == "sonnet"

    def test_plan_agent_review_unsupported_cli_raises(self, tmp_path):
        config_path = _write_config(
            {"plan_agent_review": {"enabled": True, "cli": "bogus", "budget_usd": 1.0}},
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
                            "model": "sonnet",
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
        assert par.pool[1].model == "sonnet"

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


class TestAllowedToolsConfig:
    def test_empty_allowed_tools_is_empty(self, tmp_path):
        """allowed_tools: [] should produce an empty tuple, not fall back to defaults."""
        config_path = _write_config(
            {"profiles": {"dev": {"allowed_tools": [], "budget_usd": 2.0}}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.allowed_tools == ()

    def test_omitted_allowed_tools_gets_defaults(self, tmp_path):
        """Omitting allowed_tools entirely should fall back to defaults."""
        config_path = _write_config(
            {"profiles": {"dev": {"model": "opus", "budget_usd": 2.0}}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.allowed_tools == DEFAULT_DEV_PROFILE.allowed_tools


class TestReviewPool:
    """Tests for multi-model review_pool configuration."""

    def test_review_pool_list(self, tmp_path):
        """review_pool produces correct review_pool and synthesis_profile."""
        config_path = _write_config(
            {
                "profiles": {
                    "dev": {"model": "sonnet", "budget_usd": 2.0},
                    "review_pool": [
                        {
                            "name": "opus-reviewer",
                            "cli": "claude",
                            "model": "opus",
                            "budget_usd": 1.0,
                        },
                        {
                            "name": "sonnet-reviewer",
                            "cli": "claude",
                            "model": "sonnet",
                            "budget_usd": 1.0,
                        },
                    ],
                    "synthesis": {"cli": "claude", "model": "opus", "budget_usd": 1.50},
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 2
        assert config.review_pool[0].name == "opus-reviewer"
        assert config.review_pool[1].name == "sonnet-reviewer"
        assert config.synthesis_profile is not None
        assert config.synthesis_profile.name == "synthesis"
        assert config.synthesis_profile.model == "opus"
        # review_profile property returns pool[0]
        assert config.review_profile is config.review_pool[0]

    def test_backward_compat_review_dict(self, tmp_path):
        """Single review dict → pool of one, synthesis_profile is None."""
        config_path = _write_config(
            {
                "profiles": {
                    "review": {"cli": "claude", "model": "haiku", "budget_usd": 0.50},
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 1
        assert config.review_pool[0].name == "review"
        assert config.review_pool[0].model == "haiku"
        assert config.synthesis_profile is None

    def test_review_profile_property_returns_pool_zero(self, tmp_path):
        """review_profile property always returns review_pool[0]."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {"name": "primary", "cli": "claude", "model": "opus", "budget_usd": 1.0},
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.review_profile is config.review_pool[0]
        assert config.review_profile.name == "primary"

    def test_review_pool_wins_over_review(self, tmp_path):
        """If both review and review_pool present, review_pool wins."""
        config_path = _write_config(
            {
                "profiles": {
                    "review": {"model": "haiku", "budget_usd": 0.5},
                    "review_pool": [
                        {
                            "name": "pool-member",
                            "cli": "claude",
                            "model": "opus",
                            "budget_usd": 1.0,
                        },
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 1
        assert config.review_pool[0].name == "pool-member"
        assert config.review_pool[0].model == "opus"

    def test_pool_of_one_no_synthesis_required(self, tmp_path):
        """Pool with 1 entry does not require synthesis profile."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {"name": "solo", "cli": "claude", "model": "opus", "budget_usd": 1.0},
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 1
        assert config.synthesis_profile is None

    def test_empty_review_pool_raises(self, tmp_path):
        """Empty review_pool → ValueError."""
        config_path = _write_config(
            {"profiles": {"review_pool": []}},
            tmp_path,
        )
        with pytest.raises(ValueError, match="non-empty"):
            load_config(config_path)

    def test_duplicate_pool_names_raises(self, tmp_path):
        """Duplicate names in review_pool → ValueError."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {"name": "dup", "cli": "claude", "model": "opus", "budget_usd": 1.0},
                        {"name": "dup", "cli": "claude", "model": "sonnet", "budget_usd": 1.0},
                    ],
                    "synthesis": {"cli": "claude", "model": "opus", "budget_usd": 1.0},
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="Duplicate"):
            load_config(config_path)

    def test_missing_name_in_pool_entry_raises(self, tmp_path):
        """Pool entry without 'name' → ValueError."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {"cli": "claude", "model": "opus", "budget_usd": 1.0},  # no name
                    ],
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="name"):
            load_config(config_path)

    def test_unsupported_cli_in_pool_raises(self, tmp_path):
        """Unsupported CLI in pool entry → ValueError at load time."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "llama-reviewer",
                            "cli": "llama",
                            "model": "llama3",
                            "budget_usd": 1.0,
                        },
                    ],
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="Unsupported CLI"):
            load_config(config_path)

    def test_unsupported_cli_in_review_dict_raises(self, tmp_path):
        """Unsupported CLI in backward-compat profiles.review → ValueError (P1 fix)."""
        config_path = _write_config(
            {
                "profiles": {
                    "review": {"cli": "llama", "model": "llama3", "budget_usd": 1.0},
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="Unsupported CLI"):
            load_config(config_path)

    def test_codex_cli_in_pool_accepted(self, tmp_path):
        """codex CLI in review_pool is valid and loads without error."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "codex-reviewer",
                            "cli": "codex",
                            "model": "o4-mini",
                            "budget_usd": 1.0,
                        },
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.review_pool[0].cli == "codex"

    def test_gemini_cli_in_pool_accepted(self, tmp_path):
        """gemini CLI in review_pool is valid and loads without error."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "gemini-reviewer",
                            "cli": "gemini",
                            "budget_usd": 1.0,
                            "model": "gemini-2.5-pro",
                        },
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.review_pool[0].cli == "gemini"

    def test_pool_gt1_without_synthesis_loads_ok(self, tmp_path):
        """Pool with >1 entries and no synthesis profile is now valid — merge is deterministic."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {"name": "a", "cli": "claude", "model": "opus", "budget_usd": 1.0},
                        {"name": "b", "cli": "claude", "model": "sonnet", "budget_usd": 1.0},
                    ],
                    # no synthesis — fine
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 2
        assert config.synthesis_profile is None

    def test_pool_entry_uses_review_defaults_not_dev(self, tmp_path):
        """Pool entry named 'dev' must NOT get dev-level defaults (P1 fix)."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        # entry named "dev" — should still get review-profile defaults
                        {"name": "dev", "cli": "claude", "model": "opus", "budget_usd": 1.0},
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        pool_entry = config.review_pool[0]
        assert pool_entry.name == "dev"
        # Must use review defaults, NOT dev defaults
        assert pool_entry.timeout_seconds == DEFAULT_REVIEW_PROFILE.timeout_seconds
        assert pool_entry.allowed_tools == DEFAULT_REVIEW_PROFILE.allowed_tools
        # Dev profile has Edit/Write; review profile does not
        assert "Edit" not in pool_entry.allowed_tools
        assert "Write" not in pool_entry.allowed_tools


class TestGenerateDefaultConfig:
    def test_is_valid_yaml(self):
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)
        assert "project" in data
        assert "profiles" in data
        assert "dev" in data["profiles"]
        assert "review" in data["profiles"]

    def test_loadable(self, tmp_path):
        """Default config should load without errors."""
        config_path = tmp_path / "forge.yaml"
        config_path.write_text(generate_default_config(), encoding="utf-8")
        config = load_config(config_path)
        assert config.review_pool == [config.review_profile]
        assert config.synthesis_profile is None


class TestSupportedClis:
    def test_supported_clis_contains_claude(self):
        assert "claude" in SUPPORTED_CLIS

    def test_supported_clis_contains_codex(self):
        assert "codex" in SUPPORTED_CLIS

    def test_supported_clis_contains_gemini(self):
        assert "gemini" in SUPPORTED_CLIS

    def test_unsupported_cli_in_synthesis_raises(self, tmp_path):
        """Unsupported CLI in synthesis profile → ValueError."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {"name": "a", "cli": "claude", "model": "opus", "budget_usd": 1.0},
                        {"name": "b", "cli": "claude", "model": "sonnet", "budget_usd": 1.0},
                    ],
                    "synthesis": {"cli": "llama", "model": "llama3", "budget_usd": 1.0},
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="Unsupported CLI"):
            load_config(config_path)

    def test_mixed_cli_pool_with_synthesis_loads(self, tmp_path):
        """Pool with claude + codex + gemini entries loads successfully."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "claude-reviewer",
                            "cli": "claude",
                            "model": "opus",
                            "budget_usd": 1.0,
                        },
                        {
                            "name": "codex-reviewer",
                            "cli": "codex",
                            "model": "o4-mini",
                            "budget_usd": 1.0,
                        },
                        {
                            "name": "gemini-reviewer",
                            "cli": "gemini",
                            "model": "gemini-2.5-pro",
                            "budget_usd": 1.0,
                        },
                    ],
                    "synthesis": {"cli": "claude", "model": "opus", "budget_usd": 1.0},
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 3
        clis = [p.cli for p in config.review_pool]
        assert "claude" in clis
        assert "codex" in clis
        assert "gemini" in clis


class TestNotificationConfig:
    def test_notifications_default_when_absent(self, tmp_path):
        """Missing notifications section → default NotificationConfig."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.notifications.backend == "none"
        assert config.notifications.ntfy is None
        assert config.notifications.human_review_timeout_seconds == 14400

    def test_human_review_timeout_parsed(self, tmp_path):
        """human_review_timeout_seconds is read from forge.yaml."""
        config_path = _write_config(
            {"notifications": {"backend": "ntfy", "human_review_timeout_seconds": 7200}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.human_review_timeout_seconds == 7200

    def test_human_review_timeout_default(self, tmp_path):
        """Absent human_review_timeout_seconds defaults to 14400."""
        config_path = _write_config(
            {"notifications": {"backend": "ntfy"}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.human_review_timeout_seconds == 14400

    def test_ntfy_config_parsed(self, tmp_path):
        """ntfy url and priority are parsed correctly."""
        config_path = _write_config(
            {
                "notifications": {
                    "backend": "ntfy",
                    "ntfy": {
                        "url": "https://ntfy.sh/my-topic",
                        "priority": "urgent",
                    },
                    "human_review_timeout_seconds": 3600,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.backend == "ntfy"
        assert config.notifications.ntfy is not None
        assert config.notifications.ntfy.url == "https://ntfy.sh/my-topic"
        assert config.notifications.ntfy.priority == "urgent"
        assert config.notifications.human_review_timeout_seconds == 3600

    def test_ntfy_default_priority(self, tmp_path):
        """ntfy priority defaults to 'high'."""
        config_path = _write_config(
            {
                "notifications": {
                    "backend": "ntfy",
                    "ntfy": {"url": "https://ntfy.sh/topic"},
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.ntfy is not None
        assert config.notifications.ntfy.priority == "high"


# ── Smart config tests ────────────────────────────────────────────────


class TestAutoAssignModels:
    """Tests for the _auto_assign_models() function."""

    def test_auto_assign_4_models(self):
        """4 models → correct dev/preflight/pool/synthesis assignment."""
        models = [
            "claude/sonnet",
            "claude/opus",
            "openai/gpt-5.4",
            "google/gemini-2.5-pro",
        ]
        dev, preflight, pool, synthesis = _auto_assign_models(models, 50.0)

        # dev = cheapest (sonnet, cost_rank=1)
        assert dev.cli == "claude"
        assert dev.model == "sonnet"

        # preflight = first fast-tier model (sonnet)
        assert preflight.cli == "claude"
        assert preflight.model == "sonnet"

        # review_pool = all except dev (3 models)
        assert len(pool) == 3
        pool_models = {p.model for p in pool}
        assert "opus" in pool_models
        assert "gpt-5.4" in pool_models
        assert "gemini-2.5-pro" in pool_models

        # synthesis = highest capability from pool (opus, cap=10)
        assert synthesis is not None
        assert synthesis.model == "opus"

    def test_auto_assign_2_models(self):
        """2 models → cheaper dev, single reviewer, no synthesis."""
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus"], 50.0
        )
        assert dev.model == "sonnet"
        assert preflight.model == "sonnet"
        assert len(pool) == 1
        assert pool[0].model == "opus"
        assert synthesis is None

    def test_auto_assign_1_model(self):
        """1 model → used for everything."""
        dev, preflight, pool, synthesis = _auto_assign_models(["claude/sonnet"], 50.0)
        assert dev.model == "sonnet"
        assert preflight.model == "sonnet"
        assert len(pool) == 1
        assert pool[0].model == "sonnet"
        assert synthesis is None

    def test_auto_assign_budget_distribution(self):
        """Verify budget shares are allocated correctly."""
        budget = 100.0
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus", "openai/gpt-5.4"], budget
        )
        # dev = 60%
        assert abs(dev.budget_usd - 60.0) < 0.01
        # preflight = max(2%, $1) = max($2, $1) = $2
        assert abs(preflight.budget_usd - 2.0) < 0.01
        # synthesis = max(2%, $1) = $2
        assert synthesis is not None
        assert abs(synthesis.budget_usd - 2.0) < 0.01
        # remaining = 100 - 60 - 2 - 2 = 36; 2 reviewers → $18 each
        assert len(pool) == 2
        for p in pool:
            assert abs(p.budget_usd - 18.0) < 0.01


class TestModelsKeyConfig:
    """Tests for forge.yaml 'models' key loading."""

    def test_models_key_loads_config(self, tmp_path):
        """forge.yaml with models key produces valid ForgeConfig."""
        config_path = _write_config(
            {
                "project": "smart-test",
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.project == "smart-test"
        assert config.smart_config_models == ["claude/sonnet", "claude/opus"]
        assert config.dev_profile.model == "sonnet"
        assert len(config.review_pool) == 1
        assert config.review_pool[0].model == "opus"
        assert config.synthesis_profile is None

    def test_models_with_profile_override(self, tmp_path):
        """Explicit profiles overlay auto-assigned values."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "profiles": {
                    "dev": {"budget_usd": 100.0},
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.model == "sonnet"  # still auto-assigned
        assert config.dev_profile.budget_usd == 100.0  # overridden

    def test_models_key_absent_backward_compat(self, tmp_path):
        """No models key → existing behavior unchanged."""
        config_path = _write_config({"project": "classic"}, tmp_path)
        config = load_config(config_path)
        assert config.smart_config_models is None
        assert config.dev_profile == DEFAULT_DEV_PROFILE
        assert config.review_pool == [DEFAULT_REVIEW_PROFILE]

    def test_unknown_model_gets_defaults(self, tmp_path):
        """Model not in registry is accepted with default metadata."""
        config_path = _write_config(
            {
                "models": ["claude/future-model"],
                "budget_usd": 10.0,
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.cli == "claude"
        assert config.dev_profile.model == "future-model"

    def test_models_empty_raises(self, tmp_path):
        """Empty models list → ValueError."""
        config_path = _write_config({"models": []}, tmp_path)
        with pytest.raises(ValueError, match="non-empty"):
            load_config(config_path)

    def test_models_invalid_format_raises(self, tmp_path):
        """Model without '/' → ValueError."""
        config_path = _write_config({"models": ["badformat"]}, tmp_path)
        with pytest.raises(ValueError, match="provider/model"):
            load_config(config_path)

    def test_models_unknown_provider_raises(self, tmp_path):
        """Unknown provider with no registry entry → ValueError."""
        config_path = _write_config({"models": ["llama/llama3"]}, tmp_path)
        with pytest.raises(ValueError, match="Unknown provider"):
            load_config(config_path)

    def test_budget_negative_raises(self, tmp_path):
        """Negative budget_usd → ValueError."""
        config_path = _write_config({"models": ["claude/sonnet"], "budget_usd": -1.0}, tmp_path)
        with pytest.raises(ValueError, match="positive"):
            load_config(config_path)

    def test_4_model_auto_assign_via_load_config(self, tmp_path):
        """load_config with 4 models produces correct pool and synthesis."""
        config_path = _write_config(
            {
                "models": [
                    "claude/sonnet",
                    "claude/opus",
                    "openai/gpt-5.4",
                    "google/gemini-2.5-pro",
                ],
                "budget_usd": 50.0,
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 3
        assert config.synthesis_profile is not None
        assert config.synthesis_profile.model == "opus"


class TestAutoAssignBudgetClamping:
    """Tests for budget clamping (P1 fix: reviewer budget must not exceed total)."""

    def test_tight_budget_reviewer_does_not_exceed_total(self):
        """When dev+preflight+synthesis consume the budget, reviewers get $0 not $0.50.

        Old bug: max(remaining / pool_size, 0.5) would give each reviewer $0.50
        even when remaining=0, pushing total above budget_usd.
        """
        # With $5 and 3 models: dev=$3, preflight=$1, synthesis=$1 → remaining=$0
        budget = 5.0
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus", "openai/gpt-5.4"], budget
        )
        total = dev.budget_usd + preflight.budget_usd + sum(p.budget_usd for p in pool)
        if synthesis:
            total += synthesis.budget_usd
        assert total <= budget + 0.01  # allow tiny float rounding
        for p in pool:
            assert p.budget_usd >= 0.0  # reviewer budget never negative

    def test_reviewer_budget_never_negative(self):
        """Reviewer budget is never negative even when fixed costs eat the whole budget."""
        dev, preflight, pool, synthesis = _auto_assign_models(["claude/sonnet"], 1.0)
        for p in pool:
            assert p.budget_usd >= 0.0

    def test_normal_budget_reviewer_gets_remaining_share(self):
        """With adequate budget, reviewers split remaining after dev+preflight+synthesis."""
        budget = 50.0
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus", "openai/gpt-5.4"], budget
        )
        assert synthesis is not None
        remaining = budget - dev.budget_usd - preflight.budget_usd - synthesis.budget_usd
        expected_per_reviewer = remaining / len(pool)
        for p in pool:
            assert abs(p.budget_usd - expected_per_reviewer) < 0.01


class TestModelsKeyReviewPoolOverride:
    """Tests for review_pool override in smart-config mode (P1 fix)."""

    def test_review_pool_override_by_name(self, tmp_path):
        """profiles.review_pool entries are matched by name and override auto-assigned values."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "profiles": {
                    "review_pool": [
                        {"name": "claude-opus", "budget_usd": 99.0},
                    ],
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 1
        assert config.review_pool[0].model == "opus"
        assert abs(config.review_pool[0].budget_usd - 99.0) < 0.01

    def test_review_pool_override_partial(self, tmp_path):
        """Overriding only budget_usd preserves auto-assigned cli/model."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "profiles": {
                    "review_pool": [
                        {"name": "claude-opus", "budget_usd": 9.0, "timeout_seconds": 600},
                    ],
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.review_pool[0].cli == "claude"
        assert config.review_pool[0].model == "opus"
        assert config.review_pool[0].timeout_seconds == 600

    def test_review_pool_override_unknown_name_ignored(self, tmp_path):
        """Override with name not in auto-assigned pool is silently ignored."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "profiles": {
                    "review_pool": [
                        {"name": "nonexistent", "budget_usd": 1.0},
                    ],
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        # Pool still has auto-assigned opus reviewer
        assert len(config.review_pool) == 1
        assert config.review_pool[0].model == "opus"

    def test_review_pool_override_missing_budget_raises(self, tmp_path):
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "profiles": {
                    "review_pool": [
                        {"name": "claude-opus", "timeout_seconds": 600},
                    ],
                },
            },
            tmp_path,
        )
        with pytest.raises(ConfigError, match="must set budget_usd explicitly"):
            load_config(config_path)


class TestConfigValidationWarnings:
    def test_provider_agent_missing_api_key_raises_config_error(self, tmp_path):
        config_path = _write_config(
            {
                "agents": [
                    {
                        "name": "openai-dev",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "budget_usd": 2.0,
                    }
                ]
            },
            tmp_path,
        )
        with patch.dict("os.environ", {}, clear=True), patch("importlib.import_module"):
            with pytest.raises(ConfigError, match=r"\$OPENAI_API_KEY is not set"):
                load_config(config_path)

    def test_agent_missing_budget_raises_config_error(self, tmp_path):
        config_path = _write_config(
            {"agents": [{"name": "planner", "cli": "claude", "model": "opus"}]},
            tmp_path,
        )
        with pytest.raises(ConfigError, match="Agent 'planner' must set budget_usd explicitly"):
            load_config(config_path)

    def test_assignment_enabled_requires_agents(self, tmp_path):
        config_path = _write_config({"assignment": {"enabled": True}}, tmp_path)
        with pytest.raises(ConfigError, match="non-empty agents list"):
            load_config(config_path)

    def test_plan_agent_review_same_as_planner_raises(self, tmp_path):
        config_path = _write_config(
            {
                "plan": {"enabled": True, "cli": "claude", "model": "opus", "budget_usd": 2.0},
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {
                            "name": "same-as-planner",
                            "cli": "claude",
                            "model": "opus",
                            "budget_usd": 1.0,
                        }
                    ],
                },
            },
            tmp_path,
        )
        with pytest.raises(ConfigError, match="same cli\\+model as the planner"):
            load_config(config_path)

    def test_smart_config_models_warns_when_explicit_roles_present(self, tmp_path, caplog):
        config_path = _write_config(
            {
                "project": "smart-test",
                "smart_config_models": ["claude/sonnet", "claude/opus"],
                "profiles": {"dev": {"cli": "claude", "model": "sonnet", "budget_usd": 3.0}},
            },
            tmp_path,
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            load_config(config_path)
        assert any(
            "smart_config_models is configured alongside explicit role config" in r.message
            for r in caplog.records
        )

    def test_max_iterations_over_ceiling_warns(self, tmp_path, caplog):
        config_path = _write_config(
            {
                "profiles": {
                    "dev": {
                        "cli": "claude",
                        "model": "sonnet",
                        "budget_usd": 2.0,
                        "max_iterations": 51,
                    }
                }
            },
            tmp_path,
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            load_config(config_path)
        assert any("max_iterations=51" in r.message for r in caplog.records)


class TestResolveModelInfo:
    """Tests for _resolve_model_info()."""

    def test_known_model_from_registry(self):
        info = _resolve_model_info("claude/sonnet")
        assert info.cli == "claude"
        assert info.model == "sonnet"
        assert info.tier == "fast"
        assert info.cost_rank == 1

    def test_unknown_model_defaults(self):
        info = _resolve_model_info("claude/future-model")
        assert info.cli == "claude"
        assert info.model == "future-model"
        assert info.tier == "strong"
        assert info.capability == 5
        assert info.cost_rank == 2


class TestAutoPushConfig:
    """Tests for workspace.auto_push config field."""

    def test_auto_push_config_parsed(self, tmp_path):
        """forge.yaml workspace.auto_push: true → config.workspace.auto_push is True."""
        config_path = _write_config(
            {"project": "test", "workspace": {"auto_push": True}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.workspace.auto_push is True

    def test_auto_push_default_false(self, tmp_path):
        """auto_push absent from forge.yaml → defaults to False."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.workspace.auto_push is False

    def test_auto_push_explicit_false(self, tmp_path):
        """workspace.auto_push: false → config.workspace.auto_push is False."""
        config_path = _write_config(
            {"project": "test", "workspace": {"auto_push": False}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.workspace.auto_push is False


class TestProjectSecrets:
    """Tests for project-scoped secrets loading (AC-1, AC-2)."""

    def _make_forge_dir(self, tmp_path: Path) -> Path:
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        return forge_dir

    def test_secrets_loaded_into_config(self, tmp_path):
        """AC-1: .env file is loaded and stored on ForgeConfig.secrets."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\nOPENAI_API_KEY=sk-openai-test\n",
            encoding="utf-8",
        )
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.secrets == {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-openai-test",
        }

    def test_missing_secrets_file_defaults_to_empty(self, tmp_path):
        """AC-2: absent .forge/.env → secrets defaults to {}."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.secrets == {}

    def test_empty_secrets_file_defaults_to_empty(self, tmp_path):
        """AC-2: empty .forge/.env → secrets defaults to {}."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text("", encoding="utf-8")
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.secrets == {}

    def test_malformed_secrets_raises_value_error(self, tmp_path):
        """AC-2: malformed .env raises ValueError with file path."""
        forge_dir = self._make_forge_dir(tmp_path)
        env_path = forge_dir / ".env"
        # dotenv_values produces None value when a key has no value (bare key with no =)
        env_path.write_text("BARE_KEY_NO_EQUALS\n", encoding="utf-8")
        config_path = _write_config({"project": "test"}, tmp_path)
        with pytest.raises(ValueError, match=r"malformed \.env"):
            load_config(config_path)

    def test_secrets_yaml_triggers_migration_warning(self, tmp_path, caplog):
        """AC-7: .forge/secrets.yaml present without .env triggers a warning."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / "secrets.yaml").write_text(
            "ANTHROPIC_API_KEY: sk-ant-test\n", encoding="utf-8"
        )
        config_path = _write_config({"project": "test"}, tmp_path)
        import logging

        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            config = load_config(config_path)
        assert any("secrets.yaml" in r.message for r in caplog.records)
        # secrets.yaml is NOT loaded — secrets defaults to {}
        assert config.secrets == {}

    def test_secret_satisfies_provider_api_key_validation(self, tmp_path):
        """AC-2: key in .env satisfies provider API key check even when not in os.environ."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text("OPENAI_API_KEY=sk-from-secrets\n", encoding="utf-8")
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "api-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                            "budget_usd": 1.0,
                        }
                    ]
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("importlib.import_module"),
        ):
            # Should NOT raise even though OPENAI_API_KEY is not in os.environ
            config = load_config(config_path)
        assert config.review_pool[0].provider == "openai"

    def test_plan_agent_review_secret_satisfies_validation(self, tmp_path):
        """AC-2: plan_agent_review provider key in .env satisfies validation."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-ant-from-secrets\n", encoding="utf-8"
        )
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        assert config.plan_agent_review.provider == "anthropic"

    def test_ntfy_url_from_env_when_forge_yaml_omits_url(self, tmp_path):
        """AC-4: NTFY_URL in .forge/.env is used when forge.yaml ntfy block has no url."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text("NTFY_URL=https://ntfy.sh/my-topic\n", encoding="utf-8")
        config_path = _write_config(
            {
                "project": "test",
                "notifications": {"backend": "ntfy", "ntfy": {"priority": "default"}},
            },
            tmp_path,
        )
        with patch.dict("os.environ", {}, clear=True):
            config = load_config(config_path)
        assert config.notifications.ntfy is not None
        assert config.notifications.ntfy.url == "https://ntfy.sh/my-topic"

    def test_ntfy_url_missing_warns_and_disables(self, tmp_path, caplog):
        """AC-4: ntfy backend with no URL in forge.yaml, .env, or os.environ warns and disables."""
        self._make_forge_dir(tmp_path)
        config_path = _write_config(
            {
                "project": "test",
                "notifications": {"backend": "ntfy", "ntfy": {"priority": "high"}},
            },
            tmp_path,
        )
        import logging

        with (
            patch.dict("os.environ", {}, clear=True),
            caplog.at_level(logging.WARNING, logger="theforge.config"),
        ):
            config = load_config(config_path)
        assert config.notifications.ntfy is None
        assert any("no URL" in r.message for r in caplog.records)


class TestLocalModelRegistry:
    """The four local model keys must be present in MODEL_REGISTRY with correct metadata."""

    LOCAL_KEYS = [
        "openai/codestral",
        "openai/deepseek-coder",
        "openai/llama3.1",
        "openai/qwen2.5-coder",
    ]

    def test_all_local_keys_present(self):
        for key in self.LOCAL_KEYS:
            assert key in MODEL_REGISTRY, f"Missing local model key: {key}"

    def test_local_models_cost_rank_one(self):
        for key in self.LOCAL_KEYS:
            assert MODEL_REGISTRY[key].cost_rank == 1, f"{key} should have cost_rank=1"

    def test_local_models_dev_capable(self):
        for key in self.LOCAL_KEYS:
            assert MODEL_REGISTRY[key].dev_capable is True, f"{key} should be dev_capable"

    def test_local_models_provider_prefix_is_openai(self):
        """'openai/' prefix ensures routing through the existing OpenAI adapter."""
        for key in self.LOCAL_KEYS:
            assert key.startswith("openai/"), f"{key} must use 'openai/' prefix"


class TestSprintConfig:
    """Tests for forge.yaml sprint.max_parallel parsing."""

    def test_sprint_max_parallel_parsed(self, tmp_path):
        """forge.yaml with sprint.max_parallel: 3 → config.sprint.max_parallel == 3."""
        config_path = _write_config({"project": "test", "sprint": {"max_parallel": 3}}, tmp_path)
        config = load_config(config_path)
        assert config.sprint.max_parallel == 3

    def test_sprint_section_absent_defaults_to_1(self, tmp_path):
        """No sprint section → config.sprint.max_parallel defaults to 1."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.sprint.max_parallel == 1

    def test_sprint_max_parallel_zero_raises(self, tmp_path):
        """sprint.max_parallel: 0 → raises ValueError."""
        config_path = _write_config({"project": "test", "sprint": {"max_parallel": 0}}, tmp_path)
        with pytest.raises(ValueError, match="max_parallel"):
            load_config(config_path)

    def test_sprint_max_parallel_non_integer_raises(self, tmp_path):
        """sprint.max_parallel: 'abc' → raises ValueError."""
        config_path = _write_config(
            {"project": "test", "sprint": {"max_parallel": "abc"}}, tmp_path
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_config(config_path)
