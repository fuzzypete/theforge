"""Tests for config loading."""

from pathlib import Path

import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_WORKSPACE,
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


class TestGenerateDefaultConfig:
    def test_is_valid_yaml(self):
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)
        assert "project" in data
        assert "profiles" in data
        assert "dev" in data["profiles"]
        assert "review" in data["profiles"]
