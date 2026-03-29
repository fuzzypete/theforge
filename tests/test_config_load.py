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
            {"profiles": {"dev": {"cli": "claude", "model": "sonnet"}}},
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
                    }
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="cannot have both 'cli' and 'provider'"):
            load_config(config_path)

    def test_neither_cli_nor_provider_uses_default(self, tmp_path):
        config_path = _write_config(
            {"profiles": {"dev": {"model": "sonnet"}}},
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
            {"profiles": {"dev": {"cli": "claude", "model": "sonnet"}}},
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

    def test_missing_api_key_warns(self, tmp_path):
        """Missing API key logs a warning instead of raising (demoted from error)."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {
                            "name": "api-reviewer",
                            "provider": "openai",
                            "model": "o4-mini",
                        }
                    ]
                }
            },
            tmp_path,
        )
        with patch.dict("os.environ", clear=True), patch("importlib.import_module"):
            import logging

            with patch.object(logging.getLogger("theforge.config"), "warning") as mock_warn:
                load_config(config_path)
            mock_warn.assert_called_once()
            assert "OPENAI_API_KEY" in mock_warn.call_args[0][3]

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

    def test_custom_profiles(self, tmp_path):
        config_path = _write_config(
            {
                "project": "custom",
                "profiles": {
                    "dev": {"model": "opus", "budget_usd": 5.0},
                    "review": {"cli": "claude", "model": "haiku", "timeout_seconds": 60},
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

    # ── Plan section field normalization ─────────────────────────────────

    def test_plan_model_name_deprecated_maps_to_model(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": False, "model_name": "opus"}},
            tmp_path,
        )
        import warnings as _w

        with _w.catch_warnings(record=True) as w:
            _w.simplefilter("always")
            config = load_config(config_path)
        assert config.plan.model == "opus"
        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("model_name" in str(x.message) for x in dep_warnings)

    def test_plan_cli_field_accepted(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": False, "cli": "claude", "model": "opus"}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan.cli == "claude"
        assert config.plan.model == "opus"

    def test_plan_model_as_cli_binary_deprecated(self, tmp_path):
        # Old-style: plan.model = "claude" (CLI binary) — should map to plan.cli
        config_path = _write_config(
            {"plan": {"enabled": False, "model": "claude"}},
            tmp_path,
        )
        import warnings as _w

        with _w.catch_warnings(record=True) as w:
            _w.simplefilter("always")
            config = load_config(config_path)
        assert config.plan.cli == "claude"
        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("plan.model" in str(x.message) for x in dep_warnings)

    def test_plan_both_legacy_fields_preserved(self, tmp_path):
        # Regression: model: claude + model_name: opus must yield cli=claude, model=opus
        import warnings as _w

        config_path = _write_config(
            {"plan": {"enabled": False, "model": "claude", "model_name": "opus"}},
            tmp_path,
        )
        with _w.catch_warnings(record=True):
            _w.simplefilter("always")
            config = load_config(config_path)
        assert config.plan.cli == "claude"
        assert config.plan.model == "opus"

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

    def test_plan_cli_and_provider_mutual_exclusion(self, tmp_path):
        config_path = _write_config(
            {"plan": {"enabled": True, "cli": "claude", "provider": "openai"}},
            tmp_path,
        )
        with pytest.raises(ValueError, match="cannot have both"):
            load_config(config_path)

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

    def test_plan_agent_review_rejects_same_model_as_adaptive_planner(self, tmp_path):
        config_path = _write_config(
            {
                "assignment": {"enabled": True},
                "agents": [
                    {"name": "mid-planner", "cli": "claude", "model": "sonnet", "tier": "mid"},
                    {"name": "strong-planner", "cli": "claude", "model": "opus", "tier": "strong"},
                ],
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {"name": "reviewer-a", "cli": "claude", "model": "opus"},
                    ],
                },
            },
            tmp_path,
        )

        with pytest.raises(ValueError, match="model 'opus'"):
            load_config(config_path)

    def test_plan_agent_review_allows_models_distinct_from_adaptive_planner(self, tmp_path):
        config_path = _write_config(
            {
                "assignment": {"enabled": True},
                "agents": [
                    {
                        "name": "mid-planner",
                        "cli": "claude",
                        "model": "sonnet",
                        "tier": "mid",
                    },
                    {
                        "name": "strong-planner",
                        "cli": "claude",
                        "model": "opus",
                        "tier": "strong",
                    },
                ],
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {"name": "reviewer-a", "cli": "claude", "model": "haiku"},
                    ],
                },
            },
            tmp_path,
        )

        config = load_config(config_path)
        assert config.plan_agent_review.pool[0].model == "haiku"

    def test_plan_agent_review_allows_cheap_model_when_no_mid_tier_agents(self, tmp_path):
        """With only cheap and strong agents (no mid tier), the adaptive planner always selects
        the strong agent for all complexity levels (it is the highest-budget fallback when no
        mid-tier agent exists).  A plan reviewer using the cheap model must not be rejected.
        """
        config_path = _write_config(
            {
                "assignment": {"enabled": True},
                "agents": [
                    # Realistic budgets: cheap < strong so the highest-budget fallback picks opus
                    {
                        "name": "cheap-agent",
                        "cli": "claude",
                        "model": "haiku",
                        "tier": "cheap",
                        "budget_usd": 0.5,
                    },
                    {
                        "name": "strong-agent",
                        "cli": "claude",
                        "model": "opus",
                        "tier": "strong",
                        "budget_usd": 5.0,
                    },
                ],
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {"name": "reviewer-a", "cli": "claude", "model": "haiku"},
                    ],
                },
            },
            tmp_path,
        )

        # Should not raise — the planner fallback always picks opus (highest budget), not haiku
        config = load_config(config_path)
        assert config.plan_agent_review.pool[0].model == "haiku"

    def test_plan_agent_review_rejects_strong_model_when_no_mid_tier_agents(self, tmp_path):
        """With only cheap and strong agents, the planner picks opus (highest-budget fallback).
        A plan reviewer using opus must still be rejected.
        """
        config_path = _write_config(
            {
                "assignment": {"enabled": True},
                "agents": [
                    {
                        "name": "cheap-agent",
                        "cli": "claude",
                        "model": "haiku",
                        "tier": "cheap",
                        "budget_usd": 0.5,
                    },
                    {
                        "name": "strong-agent",
                        "cli": "claude",
                        "model": "opus",
                        "tier": "strong",
                        "budget_usd": 5.0,
                    },
                ],
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        {"name": "reviewer-a", "cli": "claude", "model": "opus"},
                    ],
                },
            },
            tmp_path,
        )

        with pytest.raises(ValueError, match="model 'opus'"):
            load_config(config_path)

    def test_plan_agent_review_allows_default_plan_model_when_adaptive_assignment_overrides_it(
        self, tmp_path
    ):
        """When adaptive assignment is enabled and the plan model is still defaulted (sonnet),
        a plan reviewer using sonnet must not be rejected.  At runtime the coordinator uses the
        adaptive planner (which selects opus for all complexity levels given cheap haiku + strong
        opus), so sonnet is never actually the planner.
        """
        config_path = _write_config(
            {
                "assignment": {"enabled": True},
                # No explicit plan.cli/model → _plan_model_is_default = True, default = sonnet
                "agents": [
                    {
                        "name": "cheap-agent",
                        "cli": "claude",
                        "model": "haiku",
                        "tier": "cheap",
                        "budget_usd": 0.5,
                    },
                    {
                        "name": "strong-agent",
                        "cli": "claude",
                        "model": "opus",
                        "tier": "strong",
                        "budget_usd": 5.0,
                    },
                ],
                "plan_agent_review": {
                    "enabled": True,
                    "pool": [
                        # sonnet is the default plan.model but the adaptive planner
                        # will never select it (only haiku/opus are in the pool)
                        {"name": "reviewer-a", "cli": "claude", "model": "sonnet"},
                    ],
                },
            },
            tmp_path,
        )

        # Should not raise — adaptive planner selects opus, not sonnet
        config = load_config(config_path)
        assert config.plan_agent_review.pool[0].model == "sonnet"

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
    def test_empty_allowed_tools_is_empty(self, tmp_path):
        """allowed_tools: [] should produce an empty tuple, not fall back to defaults."""
        config_path = _write_config(
            {"profiles": {"dev": {"allowed_tools": []}}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.allowed_tools == ()

    def test_omitted_allowed_tools_gets_defaults(self, tmp_path):
        """Omitting allowed_tools entirely should fall back to defaults."""
        config_path = _write_config(
            {"profiles": {"dev": {"model": "opus"}}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.allowed_tools == DEFAULT_DEV_PROFILE.allowed_tools

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
