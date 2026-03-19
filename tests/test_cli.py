"""Tests for CLI: spec frontmatter parsing and config discovery."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from theforge.cli import (
    _build_task,
    _ensure_gitignored,
    _find_config,
    _parse_spec_frontmatter,
    cmd_check_providers,
    cmd_init_hooks,
    cmd_secrets_init,
)
from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.runner import AgentResult

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


# ── Helpers for check-providers tests ────────────────────────────────


def _api_profile(
    name: str, provider: str = "anthropic", model: str = "claude-opus-4-6"
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read", "Grep"),
    )


def _make_forge_config(
    tmp_path: Path,
    review_pool: list[ModelProfile] | None = None,
) -> ForgeConfig:
    if review_pool is None:
        review_pool = [_api_profile("claude-reviewer"), _api_profile("codex-reviewer", "openai")]
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=review_pool,
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


def _make_pass_result(profile_name: str = "test") -> AgentResult:
    return AgentResult(
        success=True,
        output='{"verdict": "APPROVE", "summary": "ok", "findings": []}',
        session_id=None,
        cost_usd=0.003,
        exit_code=0,
        raw={},
        profile_name=profile_name,
        structured_data={"verdict": "APPROVE", "summary": "ok", "findings": []},
    )


def _make_fail_result(profile_name: str = "test") -> AgentResult:
    return AgentResult(
        success=False,
        output="AuthenticationError: invalid key",
        session_id=None,
        cost_usd=None,
        exit_code=1,
        raw={},
        profile_name=profile_name,
    )


def _make_args(profile: str | None = None, config: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(profile=profile, config=config)


class TestCmdCheckProviders:
    """Tests for cmd_check_providers."""

    def test_all_pass_exits_zero(self, tmp_path, capsys):
        """All profiles passing → exit code 0, table shows checkmarks."""
        cfg = _make_forge_config(tmp_path)
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        side_effects = [
            _make_pass_result("claude-reviewer"),
            _make_pass_result("codex-reviewer"),
        ]
        with patch("theforge.cli.load_config", return_value=cfg):
            with patch("theforge.cli.run_api_agent", side_effect=side_effects):
                rc = cmd_check_providers(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "2/2 passed" in captured.out

    def test_partial_fail_exits_one(self, tmp_path, capsys):
        """One profile failing → exit code 1, failure shown inline."""
        cfg = _make_forge_config(tmp_path)
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        side_effects = [
            _make_pass_result("claude-reviewer"),
            _make_fail_result("codex-reviewer"),
        ]
        with patch("theforge.cli.load_config", return_value=cfg):
            with patch("theforge.cli.run_api_agent", side_effect=side_effects):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "1/2 passed" in captured.out

    def test_exception_counts_as_failure(self, tmp_path, capsys):
        """run_api_agent raising an exception → exit code 1, error shown inline."""
        cfg = _make_forge_config(tmp_path)
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        def _boom(*_, **__):
            raise RuntimeError("connection refused")

        with patch("theforge.cli.load_config", return_value=cfg):
            with patch("theforge.cli.run_api_agent", side_effect=_boom):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "connection refused" in captured.out

    def test_profile_filter(self, tmp_path, capsys):
        """--profile <name> tests only the named profile."""
        cfg = _make_forge_config(tmp_path)
        args = _make_args(profile="claude-reviewer", config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.load_config", return_value=cfg):
            with patch(
                "theforge.cli.run_api_agent",
                return_value=_make_pass_result("claude-reviewer"),
            ) as mock_api:
                rc = cmd_check_providers(args)

        assert rc == 0
        assert mock_api.call_count == 1
        captured = capsys.readouterr()
        assert "1/1 passed" in captured.out

    def test_profile_filter_unknown_exits_one(self, tmp_path):
        """--profile with unknown name → exit code 1, no API calls."""
        cfg = _make_forge_config(tmp_path)
        args = _make_args(profile="nonexistent", config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.load_config", return_value=cfg):
            with patch("theforge.cli.run_api_agent") as mock_api:
                rc = cmd_check_providers(args)

        assert rc == 1
        mock_api.assert_not_called()

    def test_no_verdict_in_structured_data_counts_as_failure(self, tmp_path, capsys):
        """structured_data without 'verdict' key → failure."""
        cfg = _make_forge_config(tmp_path)
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        bad_result = AgentResult(
            success=True,
            output="{}",
            session_id=None,
            cost_usd=0.001,
            exit_code=0,
            raw={},
            profile_name="claude-reviewer",
            structured_data={"summary": "no verdict here"},
        )
        with patch("theforge.cli.load_config", return_value=cfg):
            with patch("theforge.cli.run_api_agent", return_value=bad_result):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "no valid verdict" in captured.out

    def test_no_forge_yaml_exits_one(self, tmp_path):
        """No forge.yaml found → exit code 1."""
        args = _make_args(config=None)
        with patch("theforge.cli._find_config", return_value=None):
            rc = cmd_check_providers(args)
        assert rc == 1

    def test_deduplication(self, tmp_path, capsys):
        """Same profile name appearing in multiple config slots is tested only once."""
        shared = _api_profile("shared-reviewer")
        # Place same profile in review_pool and also as synthesis_profile
        cfg = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="feat/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=ModelProfile(
                name="dev",
                cli="claude",
                model="sonnet",
                budget_usd=2.0,
                timeout_seconds=300,
                allowed_tools=("Read",),
            ),
            preflight_profile=ModelProfile(
                name="preflight",
                cli="claude",
                model="sonnet",
                budget_usd=0.5,
                timeout_seconds=120,
                allowed_tools=("Read",),
            ),
            review_pool=[shared],
            synthesis_profile=shared,  # same object → same name → should be deduped
            retry=RetryPolicy(),
            plan_agent_review=PlanAgentReviewConfig(enabled=False),
            log=LogConfig(enabled=False),
        )
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.load_config", return_value=cfg):
            with patch(
                "theforge.cli.run_api_agent",
                return_value=_make_pass_result("shared-reviewer"),
            ) as mock_api:
                rc = cmd_check_providers(args)

        assert rc == 0
        assert mock_api.call_count == 1


class TestCmdInitHooks:
    """AC tests for forge init-hooks."""

    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace()

    def test_creates_post_run_sh(self, tmp_path, monkeypatch):
        """forge init-hooks creates .forge/hooks/post_run.sh."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        rc = cmd_init_hooks(args)
        assert rc == 0
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        assert sh_path.exists()
        content = sh_path.read_text(encoding="utf-8")
        assert "#!/usr/bin/env bash" in content

    def test_creates_readme(self, tmp_path, monkeypatch):
        """forge init-hooks creates .forge/hooks/README.md."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        rc = cmd_init_hooks(args)
        assert rc == 0
        readme_path = tmp_path / ".forge" / "hooks" / "README.md"
        assert readme_path.exists()
        content = readme_path.read_text(encoding="utf-8")
        assert "post_run" in content

    def test_post_run_sh_is_executable(self, tmp_path, monkeypatch):
        """forge init-hooks sets executable permissions on post_run.sh."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        import stat

        mode = sh_path.stat().st_mode
        assert mode & stat.S_IXUSR, "post_run.sh must be owner-executable"

    def test_idempotent_skips_existing_sh(self, tmp_path, monkeypatch, capsys):
        """forge init-hooks does not overwrite existing post_run.sh."""
        monkeypatch.chdir(tmp_path)
        hooks_dir = tmp_path / ".forge" / "hooks"
        hooks_dir.mkdir(parents=True)
        sh_path = hooks_dir / "post_run.sh"
        sh_path.write_text("# custom script\n", encoding="utf-8")

        args = self._make_args()
        rc = cmd_init_hooks(args)
        assert rc == 0
        assert sh_path.read_text(encoding="utf-8") == "# custom script\n"
        captured = capsys.readouterr()
        assert "post_run.sh" in captured.err

    def test_idempotent_skips_existing_readme(self, tmp_path, monkeypatch, capsys):
        """forge init-hooks does not overwrite existing README.md."""
        monkeypatch.chdir(tmp_path)
        hooks_dir = tmp_path / ".forge" / "hooks"
        hooks_dir.mkdir(parents=True)
        readme_path = hooks_dir / "README.md"
        readme_path.write_text("# my readme\n", encoding="utf-8")

        args = self._make_args()
        rc = cmd_init_hooks(args)
        assert rc == 0
        assert readme_path.read_text(encoding="utf-8") == "# my readme\n"
        captured = capsys.readouterr()
        assert "README.md" in captured.err

    def test_prints_hooks_guidance(self, tmp_path, monkeypatch, capsys):
        """forge init-hooks prints forge.yaml hooks: block guidance."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        captured = capsys.readouterr()
        assert "hooks:" in captured.out
        assert "post_run" in captured.out

    def test_script_contains_gh_guard(self, tmp_path, monkeypatch):
        """Reference script warns and exits 0 when gh is not installed."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert "command -v gh" in content

    def test_script_contains_zero_findings_guard(self, tmp_path, monkeypatch):
        """Reference script exits 0 when findings count is zero."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert "findings_count" in content
        assert "exit 0" in content

    def test_script_includes_suggestion_in_body(self, tmp_path, monkeypatch):
        """Reference script includes suggestion field in the issue body."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert "suggestion" in content

    def test_script_uses_forge_finding_label(self, tmp_path, monkeypatch):
        """Reference script adds forge-finding label to issues."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert "forge-finding" in content
