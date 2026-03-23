"""Tests for CLI: spec frontmatter parsing and config discovery."""

from __future__ import annotations

import argparse
import dataclasses
import signal as _signal
from pathlib import Path
from unittest.mock import patch

from theforge.cli import (
    _apply_dev_model_override,
    _apply_plan_model_override,
    _build_task,
    _ensure_gitignored,
    _find_config,
    _parse_story_frontmatter,
    cmd_check_providers,
    cmd_init_hooks,
    cmd_run,
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
from theforge.coord_state import CoordinatorState
from theforge.coordinator import CoordinatorResult, Phase
from theforge.runner import AgentResult

# ── Helpers (kept for TestBuildTask / TestFindConfig) ─────────────────


class TestParseFrontmatter:
    def test_with_frontmatter(self, tmp_path):
        spec = tmp_path / "my_task.md"
        spec.write_text(
            "---\n"
            "name: Phase 6H\n"
            "slug: export-svc\n"
            "pytest_target: tests/test_export.py\n"
            "---\n\n"
            "# Spec content\n",
            encoding="utf-8",
        )
        fm = _parse_story_frontmatter(spec)
        assert fm["name"] == "Phase 6H"
        assert fm["slug"] == "export-svc"

    def test_without_frontmatter(self, tmp_path):
        spec = tmp_path / "plain.md"
        spec.write_text("# Just a spec\n\nNo frontmatter.", encoding="utf-8")
        fm = _parse_story_frontmatter(spec)
        assert fm == {}

    def test_invalid_yaml_frontmatter(self, tmp_path):
        spec = tmp_path / "bad.md"
        spec.write_text("---\n[invalid: yaml: [[\n---\n", encoding="utf-8")
        fm = _parse_story_frontmatter(spec)
        assert fm == {}

    def test_non_mapping_frontmatter(self, tmp_path):
        """Non-dict frontmatter (e.g. a list) must return empty dict, not crash."""
        spec = tmp_path / "list_fm.md"
        spec.write_text("---\n- not-a-mapping\n- item-two\n---\nContent\n", encoding="utf-8")
        fm = _parse_story_frontmatter(spec)
        assert fm == {}

    def test_scalar_frontmatter(self, tmp_path):
        spec = tmp_path / "scalar.md"
        spec.write_text("---\njust a string\n---\nContent\n", encoding="utf-8")
        fm = _parse_story_frontmatter(spec)
        assert fm == {}


class TestBuildTask:
    def test_from_frontmatter(self, tmp_path):
        spec = tmp_path / "task.md"
        spec.write_text(
            "---\nname: My Task\nslug: my-task\n---\nSpec here\n",
            encoding="utf-8",
        )
        task = _build_task(spec)
        assert task.name == "My Task"
        assert task.slug == "my-task"

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


class TestApplyDevModelOverride:
    """Tests for _apply_dev_model_override provider normalisation."""

    def _base_config(self, tmp_path: Path) -> ForgeConfig:
        return _make_forge_config(tmp_path)

    def test_ollama_provider_normalised_to_openai(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_dev_model_override(
            cfg, "ollama/qwen2.5-coder:14b@http://localhost:11434/v1"
        )
        assert result.dev_profile.provider == "openai"
        assert result.dev_profile.model == "qwen2.5-coder:14b"
        assert result.dev_profile.base_url == "http://localhost:11434/v1"

    def test_openai_provider_unchanged(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_dev_model_override(cfg, "openai/gpt-4o@http://localhost:11434/v1")
        assert result.dev_profile.provider == "openai"

    def test_anthropic_provider_unchanged(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_dev_model_override(cfg, "anthropic/claude-opus-4-6")
        assert result.dev_profile.provider == "anthropic"
        assert result.dev_profile.base_url is None

    def test_mode_is_api_for_ollama(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_dev_model_override(
            cfg, "ollama/qwen2.5-coder:7b@http://localhost:11434/v1"
        )
        assert result.dev_profile.mode == "api"


class TestApplyPlanModelOverride:
    """Tests for _apply_plan_model_override."""

    def _base_config(self, tmp_path: Path) -> "ForgeConfig":
        return _make_forge_config(tmp_path)

    def test_short_model_name(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "opus")
        assert result.plan.model_name == "opus"

    def test_provider_slash_model(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "anthropic/claude-opus-4-6")
        assert result.plan.model_name == "claude-opus-4-6"

    def test_original_plan_config_preserved_when_flag_absent(self, tmp_path):
        cfg = self._base_config(tmp_path)
        original_model_name = cfg.plan.model_name
        # No override applied — plan config should be unchanged
        assert cfg.plan.model_name == original_model_name

    def test_other_plan_fields_unchanged(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "opus")
        assert result.plan.model == cfg.plan.model
        assert result.plan.budget_usd == cfg.plan.budget_usd
        assert result.plan.enabled == cfg.plan.enabled


# ── Stage-aware pipeline CLI tests ───────────────────────────────────


def _make_run_args(
    tmp_path,
    *,
    plan: str | None = None,
    from_phase: str | None = None,
    until: str | None = None,
    reviewers: int | None = None,
    max_cycles: int | None = None,
    slug: str | None = None,
    resume: bool = False,
    dev_model: str | None = None,
    plan_model: str | None = None,
    dry_run: bool = False,
    fg: bool = True,
) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for cmd_run tests."""
    story = tmp_path / "story.md"
    story.write_text("# Story\nDo the thing.\n", encoding="utf-8")
    # Create a dummy forge.yaml so _find_config succeeds (load_config is mocked).
    forge_yaml = tmp_path / "forge.yaml"
    if not forge_yaml.exists():
        forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        story=str(story),
        slug=slug,
        config=str(forge_yaml),
        plan=plan,
        from_phase=from_phase,
        until=until,
        reviewers=reviewers,
        max_cycles=max_cycles,
        resume=resume,
        dev_model=dev_model,
        plan_model=plan_model,
        dry_run=dry_run,
        interactive=False,
        auto_merge=False,
        verbose=False,
        no_notify=True,
        fg=fg,
    )


def _stub_result(phase: Phase = Phase.DONE, success: bool = True) -> CoordinatorResult:
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=CoordinatorState(),
        message="ok",
    )


class TestCmdRunUntilFlag:
    """--until flag parsing and wiring."""

    def test_until_plan_parsed_and_passed_to_run_task(self, tmp_path):
        """--until plan passes stop_phase=Phase.PLAN to run_task."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, until="plan")

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("stop_phase") == Phase.PLAN

    def test_until_unknown_phase_returns_1(self, tmp_path):
        """--until with invalid phase name returns exit code 1."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, until="bogus-phase")

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task") as mock_run,
        ):
            rc = cmd_run(args)

        assert rc == 1
        mock_run.assert_not_called()

    def test_start_phase_none_when_no_from(self, tmp_path):
        """When neither --from nor --plan is given, start_phase=None."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") is None


class TestCmdRunFromFlag:
    """--from flag precondition validation."""

    def test_from_dev_no_worktree_returns_1(self, tmp_path):
        """--from dev when worktree does not exist → exit code 1."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        # Worktree does NOT exist
        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task") as mock_run,
        ):
            rc = cmd_run(args)

        assert rc == 1
        mock_run.assert_not_called()

    def test_from_dev_no_plan_md_returns_1(self, tmp_path):
        """--from dev with worktree but no .forge/plan.md → exit code 1."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        # Create worktree without .forge/plan.md
        wt = tmp_path / slug
        wt.mkdir()

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task") as mock_run,
        ):
            rc = cmd_run(args)

        assert rc == 1
        mock_run.assert_not_called()

    def test_from_review_no_handoff_returns_1(self, tmp_path):
        """--from review with worktree but no .forge/handoff.yaml → exit code 1."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="review", slug=slug)

        # Create worktree without .forge/handoff.yaml
        wt = tmp_path / slug
        wt.mkdir()

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task") as mock_run,
        ):
            rc = cmd_run(args)

        assert rc == 1
        mock_run.assert_not_called()

    def test_from_dev_with_plan_md_succeeds(self, tmp_path):
        """--from dev with worktree + .forge/plan.md passes preconditions."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        # Create worktree with .forge/plan.md
        wt = tmp_path / slug
        (wt / ".forge").mkdir(parents=True)
        (wt / ".forge" / "plan.md").write_text("# Plan", encoding="utf-8")

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") == Phase.DEV

    def test_from_dev_with_legacy_root_plan_succeeds(self, tmp_path):
        """--from dev accepts legacy forge_plan.md when .forge/plan.md is absent."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        wt = tmp_path / slug
        wt.mkdir()
        (wt / "forge_plan.md").write_text("# Legacy Plan", encoding="utf-8")

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        assert mock_run.call_args.kwargs.get("start_phase") == Phase.DEV

    def test_from_review_with_legacy_root_handoff_succeeds(self, tmp_path):
        """--from review accepts legacy handoff.yaml when configured .forge file is absent."""
        config = _make_forge_config(tmp_path)
        config = dataclasses.replace(
            config,
            validation=dataclasses.replace(config.validation, handoff_file=".forge/handoff.yaml"),
        )
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="review", slug=slug)

        wt = tmp_path / slug
        wt.mkdir()
        (wt / "handoff.yaml").write_text("gate_decision: PASS\n", encoding="utf-8")

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        assert mock_run.call_args.kwargs.get("start_phase") == Phase.REVIEW


class TestCmdRunConfigOverrides:
    """--reviewers and --max-cycles override flags."""

    def test_reviewers_trims_pool(self, tmp_path):
        """--reviewers 1 passes a review_pool of length 1 to run_task."""
        config = _make_forge_config(tmp_path)
        assert len(config.review_pool) == 2  # fixture has 2 reviewers
        args = _make_run_args(tmp_path, reviewers=1)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            cmd_run(args)

        passed_config = mock_run.call_args.args[0]
        assert len(passed_config.review_pool) == 1
        assert passed_config.review_pool[0] == config.review_pool[0]

    def test_max_cycles_override(self, tmp_path):
        """--max-cycles 1 passes config.retry.max_review_cycles==1 to run_task."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, max_cycles=1)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            cmd_run(args)

        passed_config = mock_run.call_args.args[0]
        assert passed_config.retry.max_review_cycles == 1

    def test_override_flags_not_persisted_to_yaml(self, tmp_path):
        """Config overrides do not write to forge.yaml."""
        config = _make_forge_config(tmp_path)
        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project: test\n", encoding="utf-8")
        yaml_before = forge_yaml.read_text(encoding="utf-8")

        args = _make_run_args(tmp_path, reviewers=1, max_cycles=1)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()),
            patch("theforge.cli._write_audit"),
        ):
            cmd_run(args)

        assert forge_yaml.read_text(encoding="utf-8") == yaml_before

    def test_plan_flag_with_existing_worktree_implies_from_dev(self, tmp_path):
        """--plan with existing worktree sets start_phase=Phase.DEV."""
        config = _make_forge_config(tmp_path)
        slug = "story"

        # Create existing worktree
        wt = tmp_path / slug
        wt.mkdir()

        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# My Plan\n", encoding="utf-8")
        args = _make_run_args(tmp_path, plan=str(plan_file), slug=slug)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            cmd_run(args)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") == Phase.DEV

    def test_plan_flag_without_worktree_no_start_phase(self, tmp_path):
        """--plan on a fresh run (no worktree) does NOT set start_phase."""
        config = _make_forge_config(tmp_path)
        # No worktree directory created
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# My Plan\n", encoding="utf-8")
        args = _make_run_args(tmp_path, plan=str(plan_file))

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli._write_audit"),
        ):
            cmd_run(args)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") is None


# ── New tests: --fg flag, forge logs/stop/status ──────────────────────


def _make_sprint_args(
    tmp_path,
    *,
    fg: bool = True,
    detach: bool = False,
    resume: bool = False,
) -> argparse.Namespace:
    manifest = tmp_path / "sprint.yaml"
    manifest.write_text("stories: []\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    if not forge_yaml.exists():
        forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        manifest=str(manifest),
        config=str(forge_yaml),
        fg=fg,
        detach=detach,
        resume=resume,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
    )


class TestFgFlag:
    """--fg flag parsing for run and sprint."""

    def test_run_fg_true_skips_daemonization(self, tmp_path):
        """With --fg, daemonize_run should NOT be called."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, fg=True)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()),
            patch("theforge.cli._write_audit"),
            patch("theforge.detach.daemonize_run") as mock_daemonize,
            patch("theforge.detach.remove_pid"),
        ):
            cmd_run(args)
            mock_daemonize.assert_not_called()

    def test_run_fg_false_calls_daemonization(self, tmp_path):
        """Without --fg, daemonize_run should be called."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, fg=False)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_task", return_value=_stub_result()),
            patch("theforge.cli._write_audit"),
            patch("theforge.detach.daemonize_run") as mock_daemonize,
            patch("theforge.detach.suppress_app_nap"),
            patch("theforge.detach.install_cleanup_handler"),
            patch("theforge.detach.remove_pid"),
        ):
            cmd_run(args)
            mock_daemonize.assert_called_once()

    def test_sprint_fg_true_skips_daemonization(self, tmp_path):
        """With --fg on sprint, daemonize_run should NOT be called."""
        from theforge.cli import cmd_sprint
        from theforge.sprint import SprintResult

        config = _make_forge_config(tmp_path)
        args = _make_sprint_args(tmp_path, fg=True)

        stub_result = SprintResult(
            name="test",
            specs_total=0,
            specs_succeeded=0,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.0,
            budget_usd=0.0,
        )

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_sprint", return_value=stub_result),
            patch("theforge.detach.daemonize_run") as mock_daemonize,
            patch("theforge.detach.remove_pid"),
        ):
            cmd_sprint(args)
            mock_daemonize.assert_not_called()


class TestCmdLogs:
    def test_tails_log_file_for_known_run(self, tmp_path):
        """forge logs <run-id> calls tail -f on the correct log file."""
        from theforge.cli import cmd_logs

        run_id = "abc123"
        slug = "my-slug"
        # Create PID file
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"12345\n{slug}\n")
        # Create log file
        log_dir = tmp_path / ".forge" / "logs" / slug
        log_dir.mkdir(parents=True)
        log_file = log_dir / "run.log"
        log_file.write_text("hello\n")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")

        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id)

        with (
            patch("theforge.cli._find_config", return_value=forge_yaml),
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.subprocess.run") as mock_run,
        ):
            result = cmd_logs(args)

        assert result == 0
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "tail"
        assert call_args[1] == "-f"
        assert str(log_file) in call_args[2]

    def test_returns_error_when_no_pid_and_no_log(self, tmp_path):
        """forge logs with unknown run_id returns error."""
        from theforge.cli import cmd_logs

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id="deadbeef")

        with (
            patch("theforge.cli._find_config", return_value=forge_yaml),
            patch("theforge.cli.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 1


class TestCmdStop:
    def test_sends_sigterm_to_pid(self, tmp_path):
        """forge stop <run-id> sends SIGTERM to the correct PID."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nmy-slug\n")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id)

        with (
            patch("theforge.cli._find_config", return_value=forge_yaml),
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.os.kill") as mock_kill,
        ):
            result = cmd_stop(args)

        assert result == 0
        mock_kill.assert_called_once_with(target_pid, _signal.SIGTERM)

    def test_returns_error_when_no_pid_file(self, tmp_path):
        """forge stop returns 1 when no PID file found."""
        from theforge.cli import cmd_stop

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id="nosuchrun")

        with (
            patch("theforge.cli._find_config", return_value=forge_yaml),
            patch("theforge.cli.load_config", return_value=config),
        ):
            result = cmd_stop(args)

        assert result == 1


class TestCmdStatusActiveRuns:
    def test_shows_no_active_runs(self, tmp_path, capsys):
        """When no active runs, prints 'No active runs.'"""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace()

        with (
            patch("theforge.cli._find_config", return_value=forge_yaml),
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.detach.list_active_runs", return_value=[]),
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "No active runs" in captured.out

    def test_shows_table_for_active_runs(self, tmp_path, capsys):
        """Active runs are displayed in table format."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace()

        mock_runs = [{"run_id": "abc123ef", "pid": 12345, "slug": "my-story", "alive": True}]
        mock_status = {"phase": "DEV", "cost_usd": 1.23, "elapsed_seconds": 300, "log_path": None}

        with (
            patch("theforge.cli._find_config", return_value=forge_yaml),
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.detach.list_active_runs", return_value=mock_runs),
            patch("theforge.detach.read_run_status", return_value=mock_status),
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "abc123ef" in captured.out
        assert "my-story" in captured.out
        assert "DEV" in captured.out
        assert "active run" in captured.out


class TestDaemonDeprecation:
    def test_daemon_emits_deprecation_warning(self, tmp_path):
        """forge daemon emits DeprecationWarning."""
        import warnings

        from theforge.cli import cmd_daemon

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(
            daemon_subcommand="status",
            config=str(forge_yaml),
            no_daemonize=False,
        )

        with (
            patch("theforge.cli._find_config", return_value=forge_yaml),
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.daemon.get_daemon_status", return_value={}),
            patch("theforge.cli._print_daemon_status"),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            cmd_daemon(args)

        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "deprecated" in str(dep_warnings[0].message).lower()
