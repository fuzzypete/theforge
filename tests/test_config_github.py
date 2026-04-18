from __future__ import annotations

from theforge.config import load_config


def test_load_config_parses_github_enabled(tmp_path):
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(
        """
project: test
github:
  enabled: true
models:
  - claude/sonnet
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.github.enabled is True


def test_load_config_defaults_github_disabled_when_absent(tmp_path):
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(
        """
project: test
models:
  - claude/sonnet
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.github.enabled is False


def test_load_config_parses_review_pool_github_handle(tmp_path):
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(
        """
project: test
profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 1
    timeout_seconds: 30
  review_pool:
    - name: reviewer-a
      cli: claude
      model: sonnet
      budget_usd: 1
      timeout_seconds: 30
      github_handle: reviewer-a-gh
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.review_pool[0].github_handle == "reviewer-a-gh"
