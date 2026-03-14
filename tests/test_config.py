"""Tests for config loading."""

from pathlib import Path

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_WORKSPACE,
    SUPPORTED_CLIS,
    generate_default_config,
    load_config,
)


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


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
                    "review": {"model": "haiku", "timeout_seconds": 60},
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


class TestReviewPool:
    """Tests for multi-model review_pool configuration."""

    def test_review_pool_list(self, tmp_path):
        """review_pool produces correct review_pool and synthesis_profile."""
        config_path = _write_config(
            {
                "profiles": {
                    "dev": {"model": "sonnet"},
                    "review_pool": [
                        {"name": "opus-reviewer", "cli": "claude", "model": "opus"},
                        {"name": "sonnet-reviewer", "cli": "claude", "model": "sonnet"},
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
                    "review": {"model": "haiku", "budget_usd": 0.50},
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
                        {"name": "primary", "cli": "claude", "model": "opus"},
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
                    "review": {"model": "haiku"},
                    "review_pool": [
                        {"name": "pool-member", "cli": "claude", "model": "opus"},
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
                        {"name": "solo", "cli": "claude", "model": "opus"},
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
                        {"name": "dup", "cli": "claude", "model": "opus"},
                        {"name": "dup", "cli": "claude", "model": "sonnet"},
                    ],
                    "synthesis": {"cli": "claude", "model": "opus"},
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
                        {"cli": "claude", "model": "opus"},  # no name
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
                        {"name": "llama-reviewer", "cli": "llama", "model": "llama3"},
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
                    "review": {"cli": "llama", "model": "llama3"},
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
                        {"name": "codex-reviewer", "cli": "codex", "model": "o4-mini"},
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
                            "model": "gemini-2.5-pro",
                        },
                    ],
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.review_pool[0].cli == "gemini"

    def test_pool_gt1_missing_synthesis_raises(self, tmp_path):
        """Pool with >1 entries but no synthesis profile → ValueError."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        {"name": "a", "cli": "claude", "model": "opus"},
                        {"name": "b", "cli": "claude", "model": "sonnet"},
                    ],
                    # no synthesis
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="synthesis"):
            load_config(config_path)

    def test_pool_entry_uses_review_defaults_not_dev(self, tmp_path):
        """Pool entry named 'dev' must NOT get dev-level defaults (P1 fix)."""
        config_path = _write_config(
            {
                "profiles": {
                    "review_pool": [
                        # entry named "dev" — should still get review-profile defaults
                        {"name": "dev", "cli": "claude", "model": "opus"},
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
                        {"name": "a", "cli": "claude", "model": "opus"},
                        {"name": "b", "cli": "claude", "model": "sonnet"},
                    ],
                    "synthesis": {"cli": "llama", "model": "llama3"},
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
                        {"name": "claude-reviewer", "cli": "claude", "model": "opus"},
                        {"name": "codex-reviewer", "cli": "codex", "model": "o4-mini"},
                        {
                            "name": "gemini-reviewer",
                            "cli": "gemini",
                            "model": "gemini-2.5-pro",
                        },
                    ],
                    "synthesis": {"cli": "claude", "model": "opus"},
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
