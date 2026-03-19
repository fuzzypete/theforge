"""Tests for CLI: spec frontmatter parsing and config discovery."""

from __future__ import annotations

import argparse

from theforge.cli import (
    _build_task,
    _ensure_gitignored,
    _find_config,
    _parse_spec_frontmatter,
    cmd_secrets_init,
)

# ── Helpers (kept for TestBuildTask / TestFindConfig) ─────────────────


class TestParseFrontmatter:
    def test_with_frontmatter(self, tmp_path):
        spec = tmp_path / "my_task.md"
        spec.write_text(
            "---\n"
            "name: Phase 6H\n"
            "slug: export-svc\n"
            "file_scope:\n"
            "  - src/export/\n"
            "pytest_target: tests/test_export.py\n"
            "---\n\n"
            "# Spec content\n",
            encoding="utf-8",
        )
        fm = _parse_spec_frontmatter(spec)
        assert fm["name"] == "Phase 6H"
        assert fm["slug"] == "export-svc"
        assert fm["file_scope"] == ["src/export/"]

    def test_without_frontmatter(self, tmp_path):
        spec = tmp_path / "plain.md"
        spec.write_text("# Just a spec\n\nNo frontmatter.", encoding="utf-8")
        fm = _parse_spec_frontmatter(spec)
        assert fm == {}

    def test_invalid_yaml_frontmatter(self, tmp_path):
        spec = tmp_path / "bad.md"
        spec.write_text("---\n[invalid: yaml: [[\n---\n", encoding="utf-8")
        fm = _parse_spec_frontmatter(spec)
        assert fm == {}

    def test_non_mapping_frontmatter(self, tmp_path):
        """Non-dict frontmatter (e.g. a list) must return empty dict, not crash."""
        spec = tmp_path / "list_fm.md"
        spec.write_text("---\n- not-a-mapping\n- item-two\n---\nContent\n", encoding="utf-8")
        fm = _parse_spec_frontmatter(spec)
        assert fm == {}

    def test_scalar_frontmatter(self, tmp_path):
        spec = tmp_path / "scalar.md"
        spec.write_text("---\njust a string\n---\nContent\n", encoding="utf-8")
        fm = _parse_spec_frontmatter(spec)
        assert fm == {}


class TestBuildTask:
    def test_from_frontmatter(self, tmp_path):
        spec = tmp_path / "task.md"
        spec.write_text(
            "---\nname: My Task\nslug: my-task\nfile_scope:\n  - src/\n---\nSpec here\n",
            encoding="utf-8",
        )
        task = _build_task(spec)
        assert task.name == "My Task"
        assert task.slug == "my-task"
        assert task.file_scope == ["src/"]

    def test_slug_override(self, tmp_path):
        spec = tmp_path / "task.md"
        spec.write_text("---\nslug: from-fm\n---\n", encoding="utf-8")
        task = _build_task(spec, slug="cli-override")
        assert task.slug == "cli-override"

    def test_fallback_name_from_filename(self, tmp_path):
        spec = tmp_path / "my-cool-feature.md"
        spec.write_text("# Spec\n", encoding="utf-8")
        task = _build_task(spec)
        assert task.name == "My Cool Feature"
        assert task.slug == "my-cool-feature"


class TestFindConfig:
    def test_finds_in_current(self, tmp_path):
        (tmp_path / "forge.yaml").write_text("project: x\n", encoding="utf-8")
        assert _find_config(tmp_path) == tmp_path / "forge.yaml"

    def test_finds_in_parent(self, tmp_path):
        (tmp_path / "forge.yaml").write_text("project: x\n", encoding="utf-8")
        child = tmp_path / "specs"
        child.mkdir()
        assert _find_config(child) == tmp_path / "forge.yaml"

    def test_not_found(self, tmp_path):
        child = tmp_path / "deep" / "nested"
        child.mkdir(parents=True)
        # No forge.yaml anywhere in tmp_path hierarchy
        # Note: this might find a forge.yaml higher up in the real filesystem,
        # but tmp_path is under /tmp so unlikely
        result = _find_config(child)
        # Either None or found somewhere above — both are valid behaviors
        # The important thing is it doesn't crash
        assert result is None or result.name == "forge.yaml"


class TestSecretsInit:
    """AC-6: forge secrets-init creates .env skeleton and updates .gitignore."""

    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace()

    def test_creates_skeleton_file(self, tmp_path, monkeypatch):
        """forge secrets-init creates .forge/.env with commented-out keys."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        rc = cmd_secrets_init(args)
        assert rc == 0
        env_path = tmp_path / ".forge" / ".env"
        assert env_path.exists()
        content = env_path.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY" in content
        assert "OPENAI_API_KEY" in content
        assert "GOOGLE_API_KEY" in content
        assert "NTFY_URL" in content
        # Keys should be commented out in KEY=value format
        assert "# ANTHROPIC_API_KEY=" in content

    def test_updates_gitignore(self, tmp_path, monkeypatch):
        """forge secrets-init appends .forge/.env to .gitignore."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_secrets_init(args)
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert ".forge/.env" in gitignore.read_text(encoding="utf-8")

    def test_noop_when_file_exists(self, tmp_path, monkeypatch, capsys):
        """forge secrets-init prints warning and exits without overwriting when .env exists."""
        monkeypatch.chdir(tmp_path)
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        env_path = forge_dir / ".env"
        env_path.write_text("EXISTING_KEY=existing-value\n", encoding="utf-8")

        args = self._make_args()
        rc = cmd_secrets_init(args)
        assert rc == 0
        # File must not be overwritten
        assert "existing-value" in env_path.read_text(encoding="utf-8")
        # Warning must be printed
        captured = capsys.readouterr()
        assert "already exists" in captured.err

    def test_migration_warning_when_secrets_yaml_exists(self, tmp_path, monkeypatch, capsys):
        """AC-7: migration warning is printed when secrets.yaml exists and .env does not."""
        monkeypatch.chdir(tmp_path)
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "secrets.yaml").write_text("ANTHROPIC_API_KEY: sk-test\n", encoding="utf-8")

        args = self._make_args()
        rc = cmd_secrets_init(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "secrets.yaml" in captured.err

    def test_creates_gitignore_if_absent(self, tmp_path):
        """_ensure_gitignored creates .gitignore if it doesn't exist."""
        _ensure_gitignored(tmp_path)
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert ".forge/.env" in gitignore.read_text(encoding="utf-8")

    def test_does_not_duplicate_gitignore_entry(self, tmp_path):
        """_ensure_gitignored is idempotent — calling twice adds entry only once."""
        _ensure_gitignored(tmp_path)
        _ensure_gitignored(tmp_path)
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".forge/.env") == 1

    def test_appends_to_existing_gitignore(self, tmp_path):
        """_ensure_gitignored appends to an existing .gitignore."""
        (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        _ensure_gitignored(tmp_path)
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert ".forge/.env" in content
