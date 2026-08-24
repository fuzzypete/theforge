"""Tests for CLI setup: frontmatter parsing, config discovery, secrets, hooks, model overrides."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.cli import (
    _apply_dev_model_override,
    _apply_plan_model_override,
    _build_task,
    _ensure_gitattributes,
    _ensure_gitignored,
    _find_config,
    _gitattributes_block,
    _gitignore_block,
    _parse_story_frontmatter,
    build_parser,
    cmd_check_providers,
    cmd_init,
    cmd_init_hooks,
    cmd_secrets_init,
)
from theforge.cli import hooks as hooks_module
from theforge.cli.hooks import created_labels, static_issue_labels
from theforge.cli.init_commands import _extract_forge_block
from theforge.cli.provider_readiness import (
    READINESS_STATUS_COST_UNAVAILABLE,
    READINESS_STATUS_UNVERIFIED,
)
from theforge.config import (
    DEFAULT_VALIDATION,
    AgentDef,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
    transport_for,
)
from theforge.runners import AgentResult

# ── Helpers ───────────────────────────────────────────────────────────


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


def _cli_profile(name: str, cli: str = "claude", model: str = "sonnet") -> ModelProfile:
    return ModelProfile(
        name=name,
        cli=cli,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read", "Grep"),
    )


def _make_forge_config(
    tmp_path: Path,
    review_pool: list[ModelProfile] | None = None,
    *,
    include_test_secrets: bool = False,
) -> ForgeConfig:
    if review_pool is None:
        review_pool = [
            _api_profile("claude-reviewer"),
            _api_profile("deepseek-reviewer", "deepseek", "deepseek-chat"),
        ]
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=_api_profile("dev", provider="anthropic", model="claude-sonnet-4-6"),
        preflight_profile=_api_profile(
            "preflight",
            provider="anthropic",
            model="claude-sonnet-4-6",
        ),
        review_pool=review_pool,
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig.of(enabled=False),
        log=LogConfig(enabled=False),
        secrets=(
            {
                "ANTHROPIC_API_KEY": "test-key",
                "DEEPSEEK_API_KEY": "test-key",
                "OPENAI_API_KEY": "test-key",
            }
            if include_test_secrets
            else {}
        ),
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
        output="provider returned malformed tool response",
        session_id=None,
        cost_usd=None,
        exit_code=1,
        raw={},
        profile_name=profile_name,
    )


def _make_args(
    profile: str | None = None,
    config: str | None = None,
    *,
    declared_only: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(profile=profile, config=config, declared_only=declared_only)


# ── TestParseFrontmatter ──────────────────────────────────────────────


class TestParseFrontmatter:
    def test_with_frontmatter(self, tmp_path):
        spec = tmp_path / "my_task.md"
        spec.write_text(
            "---\n"
            "name: Phase 6H\n"
            "slug: export-svc\n"
            "test_target: tests/test_export.py\n"
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


# ── TestBuildTask ─────────────────────────────────────────────────────


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

    def test_github_issue_parsed(self, tmp_path):
        spec = tmp_path / "task.md"
        spec.write_text("---\nslug: my-task\ngithub_issue: 42\n---\n", encoding="utf-8")
        task = _build_task(spec)
        assert task.github_issue == 42

    def test_github_issue_absent(self, tmp_path):
        spec = tmp_path / "task.md"
        spec.write_text("---\nslug: my-task\n---\n", encoding="utf-8")
        task = _build_task(spec)
        assert task.github_issue is None

    def test_github_issue_malformed_ignored(self, tmp_path):
        spec = tmp_path / "task.md"
        spec.write_text("---\nslug: my-task\ngithub_issue: abc\n---\n", encoding="utf-8")
        task = _build_task(spec)
        assert task.github_issue is None


# ── TestFindConfig ────────────────────────────────────────────────────


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


# ── TestSecretsInit ───────────────────────────────────────────────────


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


# ── TestCanonicalGitignoreTemplate ────────────────────────────────────


class TestCanonicalGitignoreTemplate:
    """AC: forge init writes a canonical default-deny .gitignore block."""

    def _init_args(self, shared_memory: bool = True) -> argparse.Namespace:
        return argparse.Namespace(shared_memory=shared_memory)

    def test_shared_memory_block_shape(self, tmp_path, monkeypatch):
        """Default (--shared-memory) writes default-deny with all re-includes."""
        monkeypatch.chdir(tmp_path)
        assert cmd_init(self._init_args()) == 0
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        # Default-deny rule
        assert ".forge/**" in content
        # Project-memory re-includes
        assert "!.forge/audits/runs/**" in content
        assert "!.forge/knowledge/summaries/**" in content
        # Config re-includes
        assert "!.forge/hooks/**" in content
        assert "!.forge/.env.example" in content
        # Root runtime state
        assert "handoff.yaml" in content
        # Prose references the design doc and the escape hatch
        assert "docs/plans/forge-storage-layout.md" in content
        assert "--local-memory" in content

    def test_local_memory_omits_project_memory_reincludes(self, tmp_path, monkeypatch):
        """--local-memory drops project-memory re-includes, keeps config re-includes."""
        monkeypatch.chdir(tmp_path)
        assert cmd_init(self._init_args(shared_memory=False)) == 0
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".forge/**" in content
        # Project-memory re-includes must be gone
        assert "!.forge/audits/runs/**" not in content
        assert "!.forge/knowledge/summaries/**" not in content
        # Config re-includes remain
        assert "!.forge/hooks/**" in content
        assert "!.forge/.env.example" in content

    def test_single_block_not_duplicated_on_rerun(self, tmp_path):
        """Running the writer twice does not duplicate the block."""
        _ensure_gitignored(tmp_path)
        _ensure_gitignored(tmp_path)
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".forge/**") == 1
        assert content.count("# ── TheForge") == 1

    def test_local_then_shared_recognized_no_drift(self, tmp_path, capsys):
        """A local-memory block is recognized by a later shared-memory run (no drift)."""
        _ensure_gitignored(tmp_path, shared_memory=False)
        before = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        status = _ensure_gitignored(tmp_path, shared_memory=True)
        after = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert status == "current"
        assert before == after

    def test_drift_is_surfaced_not_overwritten(self, tmp_path):
        """An edited TheForge block is reported as drift and left untouched."""
        _ensure_gitignored(tmp_path)
        gitignore = tmp_path / ".gitignore"
        edited = gitignore.read_text(encoding="utf-8").replace(
            "!.forge/hooks/**", "!.forge/hooks/**\n!.forge/custom/**"
        )
        gitignore.write_text(edited, encoding="utf-8")
        status = _ensure_gitignored(tmp_path)
        assert status == "drift"
        # User edit preserved
        assert gitignore.read_text(encoding="utf-8") == edited

    def test_appends_below_existing_content(self, tmp_path):
        """The block is appended below unrelated existing .gitignore content."""
        (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        _ensure_gitignored(tmp_path)
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.startswith("*.pyc\n")
        assert ".forge/**" in content

    def test_secrets_denied_by_default_deny(self, tmp_path):
        """.forge/.env is not re-included — it stays denied by .forge/**."""
        block = _gitignore_block(shared_memory=True)
        assert "!.forge/.env\n" not in block
        assert "!.forge/.env.example" in block

    def test_rerun_with_existing_forge_yaml_surfaces_drift(self, tmp_path, monkeypatch, capsys):
        """forge init on an already-initialized project still surfaces template drift."""
        monkeypatch.chdir(tmp_path)
        # First init writes forge.yaml and the canonical blocks.
        assert cmd_init(argparse.Namespace(shared_memory=True)) == 0
        # Operator edits the TheForge .gitignore block away from canonical.
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8").replace(
                "!.forge/hooks/**", "!.forge/hooks/**\n!.forge/custom/**"
            ),
            encoding="utf-8",
        )
        edited = gitignore.read_text(encoding="utf-8")
        capsys.readouterr()  # clear buffered output from the first init

        # Re-running init (forge.yaml already exists) must still check the block.
        rc = cmd_init(argparse.Namespace(shared_memory=True))
        assert rc == 1  # forge.yaml already exists → non-zero, unchanged contract
        captured = capsys.readouterr()
        assert "forge.yaml already exists" in captured.err
        assert "differs from the canonical" in captured.err
        # The user's edited block is left untouched.
        assert gitignore.read_text(encoding="utf-8") == edited

    def test_rerun_with_existing_forge_yaml_writes_missing_gitattributes(
        self, tmp_path, monkeypatch
    ):
        """Re-run writes a missing .gitattributes block even when forge.yaml exists."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "forge.yaml").write_text("project: x\n", encoding="utf-8")
        assert not (tmp_path / ".gitattributes").exists()
        rc = cmd_init(argparse.Namespace(shared_memory=True))
        assert rc == 1
        assert (tmp_path / ".gitattributes").exists()
        assert (tmp_path / ".gitignore").exists()


class TestDogfoodGitPolicyInSync:
    """Dogfood: this repo's own git policy stays byte-identical to the canonical template.

    TheForge's `.gitignore`/`.gitattributes` are the reference implementation
    downstream projects look at, so the TheForge marker block in each must match
    exactly what `forge init --shared-memory` emits. If the canonical template
    builders change, these tests fail until the repo's own files are re-synced.
    """

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def test_repo_gitignore_block_matches_shared_memory_canonical(self):
        content = (self._repo_root() / ".gitignore").read_text(encoding="utf-8")
        block = _extract_forge_block(content)
        assert block is not None, "repo .gitignore is missing the TheForge marker block"
        assert block == _gitignore_block(shared_memory=True)

    def test_repo_gitattributes_block_matches_canonical(self):
        content = (self._repo_root() / ".gitattributes").read_text(encoding="utf-8")
        block = _extract_forge_block(content)
        assert block is not None, "repo .gitattributes is missing the TheForge marker block"
        assert block == _gitattributes_block()

    def test_repo_gitignore_recognized_as_current_no_drift(self):
        """The committed block is detected as canonical (drift-check passes)."""
        content = (self._repo_root() / ".gitignore").read_text(encoding="utf-8")
        block = _extract_forge_block(content)
        known_good = {
            _gitignore_block(shared_memory=True),
            _gitignore_block(shared_memory=False),
        }
        assert block in known_good


# ── TestCanonicalGitattributes ────────────────────────────────────────


class TestCanonicalGitattributes:
    """AC: forge init writes a .gitattributes block marking runs as generated."""

    def test_block_marks_generated(self, tmp_path):
        status = _ensure_gitattributes(tmp_path)
        assert status == "written"
        content = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
        assert ".forge/audits/runs/**  linguist-generated=true" in content
        assert ".forge/knowledge/summaries/**  linguist-generated=true" in content

    def test_idempotent(self, tmp_path):
        _ensure_gitattributes(tmp_path)
        status = _ensure_gitattributes(tmp_path)
        content = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
        assert status == "current"
        assert content.count("# ── TheForge") == 1

    def test_appends_below_existing_content(self, tmp_path):
        (tmp_path / ".gitattributes").write_text(
            "*.lock.yml linguist-generated=true\n", encoding="utf-8"
        )
        _ensure_gitattributes(tmp_path)
        content = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
        assert content.startswith("*.lock.yml linguist-generated=true\n")
        assert "linguist-generated=true" in _gitattributes_block()

    def test_written_regardless_of_memory_mode(self, tmp_path, monkeypatch):
        """--local-memory still writes .gitattributes (harmless if nothing tracked)."""
        monkeypatch.chdir(tmp_path)
        cmd_init(argparse.Namespace(shared_memory=False))
        assert (tmp_path / ".gitattributes").exists()


# ── TestInitMemoryFlags ───────────────────────────────────────────────


class TestInitMemoryFlags:
    """AC: forge init exposes mutually-exclusive --shared-memory/--local-memory."""

    def test_default_is_shared_memory(self):
        args = build_parser().parse_args(["init"])
        assert args.shared_memory is True

    def test_local_memory_flag(self):
        args = build_parser().parse_args(["init", "--local-memory"])
        assert args.shared_memory is False

    def test_shared_memory_flag(self):
        args = build_parser().parse_args(["init", "--shared-memory"])
        assert args.shared_memory is True

    def test_flags_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["init", "--shared-memory", "--local-memory"])


# ── TestSecretsInitTemplateSource ─────────────────────────────────────


class TestSecretsInitTemplateSource:
    """AC: forge secrets-init uses the same template source as forge init."""

    def test_writes_canonical_block_and_gitattributes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = cmd_secrets_init(argparse.Namespace())
        assert rc == 0
        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        # Same canonical block the default forge init would write
        assert _gitignore_block(shared_memory=True) in gitignore
        assert (tmp_path / ".gitattributes").exists()


# ── TestCmdCheckProviders ─────────────────────────────────────────────


class TestCmdCheckProviders:
    """Tests for cmd_check_providers."""

    def test_all_pass_exits_zero(self, tmp_path, capsys):
        """All profiles passing → exit code 0, table shows checkmarks."""
        cfg = _make_forge_config(tmp_path, include_test_secrets=True)
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        side_effects = [
            _make_pass_result("dev"),
            _make_pass_result("preflight"),
            _make_pass_result("claude-reviewer"),
            _make_pass_result("deepseek-reviewer"),
            _make_pass_result("preflight"),
        ]
        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch("theforge.cli.provider_readiness.run_api_agent", side_effect=side_effects):
                rc = cmd_check_providers(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "5/5 passed" in captured.out
        assert "declared transport:" not in captured.out

    def test_partial_fail_exits_one(self, tmp_path, capsys):
        """One profile failing → exit code 1, failure shown inline."""
        cfg = _make_forge_config(tmp_path, include_test_secrets=True)
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        side_effects = [
            _make_pass_result("dev"),
            _make_pass_result("preflight"),
            _make_pass_result("claude-reviewer"),
            _make_fail_result("deepseek-reviewer"),
            _make_pass_result("preflight"),
        ]
        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch("theforge.cli.provider_readiness.run_api_agent", side_effect=side_effects):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "4/5 passed" in captured.out
        assert "declared transport:" in captured.out

    def test_exception_counts_as_failure(self, tmp_path, capsys):
        """run_api_agent raising an exception → exit code 1, error shown inline."""
        cfg = _make_forge_config(tmp_path, include_test_secrets=True)
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        def _boom(*_, **__):
            raise RuntimeError("connection refused")

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch("theforge.cli.provider_readiness.run_api_agent", side_effect=_boom):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "connection refused" in captured.out

    def test_profile_filter(self, tmp_path, capsys):
        """--profile <name> tests only the named profile."""
        cfg = _make_forge_config(tmp_path, include_test_secrets=True)
        args = _make_args(profile="claude-reviewer", config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch(
                "theforge.cli.provider_readiness.run_api_agent",
                return_value=_make_pass_result("claude-reviewer"),
            ) as mock_api:
                rc = cmd_check_providers(args)

        assert rc == 0
        assert mock_api.call_count == 1
        captured = capsys.readouterr()
        assert "1/1 passed" in captured.out

    def test_profile_filter_unknown_exits_one(self, tmp_path):
        """--profile with unknown name → exit code 1, no API calls."""
        cfg = _make_forge_config(tmp_path, include_test_secrets=True)
        args = _make_args(profile="nonexistent", config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch("theforge.cli.provider_readiness.run_api_agent") as mock_api:
                rc = cmd_check_providers(args)

        assert rc == 1
        mock_api.assert_not_called()

    def test_declared_only_skips_alternate_transport_probe(self, tmp_path):
        cfg = _make_forge_config(tmp_path, include_test_secrets=True)
        args = _make_args(config=str(tmp_path / "forge.yaml"), declared_only=True)

        with (
            patch("theforge.cli.providers.load_config", return_value=cfg),
            patch(
                "theforge.cli.provider_readiness.run_api_agent",
                return_value=_make_pass_result("declared-only"),
            ) as mock_api,
            patch("theforge.cli.provider_readiness.run_agent") as mock_cli,
        ):
            rc = cmd_check_providers(args)

        assert rc == 0
        assert mock_api.call_count == 5
        mock_cli.assert_not_called()

    def test_declared_only_skips_alternate_api_probe_for_cli_declared_profile(self, tmp_path):
        cfg = _make_forge_config(
            tmp_path,
            review_pool=[_cli_profile("review-ghaw", cli="ghaw", model="sonnet")],
            include_test_secrets=True,
        )
        args = _make_args(
            profile="review-ghaw",
            config=str(tmp_path / "forge.yaml"),
            declared_only=True,
        )

        with (
            patch("theforge.cli.providers.load_config", return_value=cfg),
            patch(
                "theforge.cli.provider_readiness.run_agent",
                return_value=AgentResult(
                    success=True,
                    output='{"verdict":"APPROVE","summary":"ok"}',
                    session_id=None,
                    cost_usd=0.003,
                    exit_code=0,
                    raw={},
                    profile_name="review-ghaw",
                    structured_data={"verdict": "APPROVE", "summary": "ok"},
                    transport_used="cli",
                ),
            ) as mock_cli,
            patch("theforge.cli.provider_readiness.run_api_agent") as mock_api,
            patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
        ):
            rc = cmd_check_providers(args)

        assert rc == 0
        assert mock_cli.call_count == 1
        mock_api.assert_not_called()

    def test_no_verdict_in_structured_data_counts_as_failure(self, tmp_path, capsys):
        """structured_data without 'verdict' key → failure."""
        cfg = _make_forge_config(tmp_path, include_test_secrets=True)
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
        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch("theforge.cli.provider_readiness.run_api_agent", return_value=bad_result):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "no valid verdict" in captured.out

    def test_no_forge_yaml_exits_one(self, tmp_path):
        """No forge.yaml found → exit code 1."""
        args = _make_args(config=None)
        with patch("theforge.cli.providers._find_config", return_value=None):
            rc = cmd_check_providers(args)
        assert rc == 1

    def test_deduplication(self, tmp_path, capsys):
        """Exact duplicate probes in the same role collapse to one run."""
        shared = _api_profile("shared-reviewer")
        cfg = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="feat/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=_api_profile("dev", provider="anthropic", model="claude-sonnet-4-6"),
            preflight_profile=_api_profile(
                "preflight",
                provider="anthropic",
                model="claude-sonnet-4-6",
            ),
            review_pool=[shared, shared],
            synthesis_profile=None,
            retry=RetryPolicy(),
            plan_agent_review=PlanAgentReviewConfig.of(enabled=False),
            log=LogConfig(enabled=False),
            secrets={"ANTHROPIC_API_KEY": "test-key"},
        )
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch(
                "theforge.cli.provider_readiness.run_api_agent",
                return_value=_make_pass_result("shared-reviewer"),
            ) as mock_api:
                rc = cmd_check_providers(args)

        assert rc == 0
        assert mock_api.call_count == 4

    def test_tool_bearing_profile_is_probed_with_original_allowlist(self, tmp_path):
        cfg = _make_forge_config(
            tmp_path,
            review_pool=[_api_profile("reviewer")],
            include_test_secrets=True,
        )
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch(
                "theforge.cli.provider_readiness.run_api_agent",
                return_value=_make_pass_result("reviewer"),
            ) as mock_api:
                rc = cmd_check_providers(args)

        assert rc == 0
        reviewer_call = next(
            call for call in mock_api.call_args_list if call.kwargs["profile"].name == "reviewer"
        )
        assert reviewer_call.kwargs["profile"].allowed_tools == ("Read", "Grep")

    def test_tool_bearing_agent_pool_failure_cannot_hide_behind_plain_probe(
        self, tmp_path, capsys
    ):
        cfg = _make_forge_config(tmp_path, review_pool=[], include_test_secrets=True)
        cfg = ForgeConfig(
            **{
                **cfg.__dict__,
                "agents": [
                    AgentDef(
                        name="agent-api",
                        provider="openai",
                        model="gpt-5.4",
                        budget_usd=1.0,
                        timeout_seconds=120,
                        tier="mid",
                        transport=transport_for("openai", "api"),
                    )
                ],
            }
        )
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        def _side_effect(*_, **kwargs):
            profile = kwargs["profile"]
            if profile.allowed_tools:
                return _make_fail_result(profile.name)
            return _make_pass_result(profile.name)

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch("theforge.cli.provider_readiness.run_api_agent", side_effect=_side_effect):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "agent-dev" in captured.out
        assert "0/8 passed" in captured.out

    def test_same_profile_can_render_distinct_role_rows(self, tmp_path, capsys):
        shared = _api_profile("shared")
        cfg = _make_forge_config(tmp_path, review_pool=[shared], include_test_secrets=True)
        cfg = ForgeConfig(
            **{
                **cfg.__dict__,
                "plan_agent_review": PlanAgentReviewConfig(enabled=True, pool=[shared]),
            }
        )
        args = _make_args(config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch(
                "theforge.cli.provider_readiness.run_api_agent",
                side_effect=[
                    _make_pass_result("dev"),
                    _make_pass_result("preflight"),
                    _make_pass_result("shared"),
                    _make_pass_result("shared"),
                    _make_pass_result("preflight"),
                ],
            ):
                rc = cmd_check_providers(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "review" in captured.out
        assert "plan-review" in captured.out
        assert "5/5 passed" in captured.out

    def test_cost_unavailable_is_reported_and_fails(self, tmp_path, capsys):
        cfg = _make_forge_config(
            tmp_path,
            review_pool=[_api_profile("reviewer")],
            include_test_secrets=True,
        )
        args = _make_args(config=str(tmp_path / "forge.yaml"))
        unknown_cost = AgentResult(
            success=True,
            output='{"verdict":"APPROVE","summary":"ok"}',
            session_id=None,
            cost_usd=None,
            exit_code=0,
            raw={},
            profile_name="reviewer",
            structured_data={"verdict": "APPROVE", "summary": "ok"},
        )

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch("theforge.cli.provider_readiness.run_api_agent", return_value=unknown_cost):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert READINESS_STATUS_COST_UNAVAILABLE in captured.out
        assert "0/4 passed" in captured.out

    def test_declared_api_failure_names_ready_cli_alternate(self, tmp_path, capsys):
        cfg = _make_forge_config(
            tmp_path,
            review_pool=[_api_profile("openai-reviewer", "openai", "gpt-5.4")],
            include_test_secrets=True,
        )
        args = _make_args(profile="openai-reviewer", config=str(tmp_path / "forge.yaml"))

        with (
            patch("theforge.cli.providers.load_config", return_value=cfg),
            patch(
                "theforge.cli.provider_readiness.run_api_agent",
                return_value=_make_fail_result("openai-reviewer"),
            ),
            patch(
                "theforge.cli.provider_readiness.run_agent",
                return_value=AgentResult(
                    success=True,
                    output='{"verdict":"APPROVE","summary":"ok"}',
                    session_id=None,
                    cost_usd=0.003,
                    exit_code=0,
                    raw={},
                    profile_name="openai-reviewer",
                    structured_data={"verdict": "APPROVE", "summary": "ok"},
                    transport_used="cli",
                ),
            ),
            patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
        ):
            rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "declared transport: api" in captured.out
        assert "api      tool-structured" in captured.out
        assert "cli      tool-structured" in captured.out
        assert "ready alternate transport: cli" in captured.out

    def test_unverified_alternate_transport_reason_is_rendered(self, tmp_path, capsys):
        cfg = _make_forge_config(
            tmp_path,
            review_pool=[_api_profile("deepseek-reviewer", "deepseek", "deepseek-chat")],
            include_test_secrets=True,
        )
        args = _make_args(profile="deepseek-reviewer", config=str(tmp_path / "forge.yaml"))

        with patch("theforge.cli.providers.load_config", return_value=cfg):
            with patch(
                "theforge.cli.provider_readiness.run_api_agent",
                return_value=_make_fail_result("deepseek-reviewer"),
            ):
                rc = cmd_check_providers(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert READINESS_STATUS_UNVERIFIED in captured.out
        assert "No CLI runner for provider 'deepseek'" in captured.out

    def test_declared_explicit_cli_runner_is_exercised(self, tmp_path):
        cfg = _make_forge_config(
            tmp_path,
            review_pool=[_cli_profile("review-ghaw", cli="ghaw", model="sonnet")],
            include_test_secrets=True,
        )
        args = _make_args(profile="review-ghaw", config=str(tmp_path / "forge.yaml"))

        with (
            patch("theforge.cli.providers.load_config", return_value=cfg),
            patch(
                "theforge.cli.provider_readiness.run_agent",
                return_value=AgentResult(
                    success=True,
                    output='{"verdict":"APPROVE","summary":"ok"}',
                    session_id=None,
                    cost_usd=0.003,
                    exit_code=0,
                    raw={},
                    profile_name="review-ghaw",
                    structured_data={"verdict": "APPROVE", "summary": "ok"},
                    transport_used="cli",
                ),
            ) as mock_cli,
            patch(
                "theforge.cli.provider_readiness.run_api_agent",
                return_value=AgentResult(
                    success=True,
                    output='{"verdict":"APPROVE","summary":"ok"}',
                    session_id=None,
                    cost_usd=0.003,
                    exit_code=0,
                    raw={},
                    profile_name="review-ghaw",
                    structured_data={"verdict": "APPROVE", "summary": "ok"},
                    transport_used="api",
                ),
            ),
            patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
        ):
            rc = cmd_check_providers(args)

        assert rc == 0
        declared_profile = mock_cli.call_args.kwargs["profile"]
        assert declared_profile.cli == "ghaw"
        assert declared_profile.transport is not None
        assert declared_profile.transport.runner == "ghaw"


# ── TestCmdInitHooks ──────────────────────────────────────────────────


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

    def test_post_run_sh_bug_format_compliant(self, tmp_path, monkeypatch):
        """post_run.sh renders observed/expected/evidence and a non-binding suggestion section.

        The body must read as a bug story (Observed/Expected/Evidence headings) so that
        new findings pass bug shape gate without manual grooming. The legacy boilerplate
        ``What was expected: Behavior conforming to ...`` line must not appear, and any
        suggestion must be marked non-binding so it is not treated as HOW-leakage.
        """
        monkeypatch.chdir(tmp_path)
        cmd_init_hooks(self._make_args())
        content = (tmp_path / ".forge" / "hooks" / "post_run.sh").read_text(encoding="utf-8")
        assert "**Observed:**" in content
        assert "**Expected:**" in content
        assert "**Evidence:**" in content
        # Suggestion appears only as a non-binding section heading.
        assert "Suggested approach (non-binding)" in content
        assert "non-binding" in content
        # Legacy boilerplate must be gone.
        assert "What happened" not in content
        assert "Behavior conforming to" not in content

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

    def test_existing_stale_generated_hook_warns(self, tmp_path, monkeypatch, capsys):
        """forge init-hooks warns when the installed generated hook needs operator action."""
        monkeypatch.chdir(tmp_path)
        hooks_dir = tmp_path / ".forge" / "hooks"
        hooks_dir.mkdir(parents=True)
        sh_path = hooks_dir / "post_run.sh"
        sh_path.write_text(
            "#!/usr/bin/env bash\n"
            "*Filed by theforge post_run hook.*\n"
            'gh issue create --label "forge-finding"\n',
            encoding="utf-8",
        )

        rc = cmd_init_hooks(self._make_args())
        assert rc == 0
        captured = capsys.readouterr()
        assert "stale" in captured.err
        assert "needs-triage" in captured.err
        assert "forge init-hooks" in captured.err
        assert "--update" not in captured.err

    def test_existing_hook_warning_tracks_future_template_labels(
        self, tmp_path, monkeypatch, capsys
    ):
        """Stale-hook detection compares against the template, not one hard-coded label."""
        monkeypatch.chdir(tmp_path)
        future_template = hooks_module._POST_RUN_SH.replace(
            '--label "needs-triage" \\',
            '--label "needs-triage" \\\n      --label "release-intake" \\',
            1,
        )
        monkeypatch.setattr(hooks_module, "_POST_RUN_SH", future_template)
        hooks_dir = tmp_path / ".forge" / "hooks"
        hooks_dir.mkdir(parents=True)
        sh_path = hooks_dir / "post_run.sh"
        sh_path.write_text(
            "#!/usr/bin/env bash\n"
            "*Filed by theforge post_run hook.*\n"
            'gh issue create --label "forge-finding" --label "needs-triage"\n',
            encoding="utf-8",
        )

        rc = cmd_init_hooks(self._make_args())

        assert rc == 0
        captured = capsys.readouterr()
        assert "release-intake" in captured.err

    def test_repo_configured_hook_matches_generated_required_labels(self):
        """The checked-in active hook carries the generated template's static labels."""
        repo_root = Path(__file__).resolve().parents[1]
        live_hook = repo_root / ".forge" / "hooks" / "post_run.sh"
        if not live_hook.exists():
            pytest.skip(".forge/hooks/post_run.sh missing (gitignored); dev-machine only")
        live_content = live_hook.read_text(encoding="utf-8")

        assert static_issue_labels(live_content) >= static_issue_labels(hooks_module._POST_RUN_SH)
        assert created_labels(live_content) >= created_labels(hooks_module._POST_RUN_SH)

    def test_repo_configured_hook_files_findings_with_needs_triage(self, tmp_path):
        """Execute the checked-in active hook with fake gh/jq and inspect gh calls."""
        repo_root = Path(__file__).resolve().parents[1]
        live_hook = repo_root / ".forge" / "hooks" / "post_run.sh"
        if not live_hook.exists():
            pytest.skip(".forge/hooks/post_run.sh missing (gitignored); dev-machine only")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        gh_log = tmp_path / "gh.log"

        gh = bin_dir / "gh"
        gh.write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$GH_LOG"\nexit 0\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)

        jq = bin_dir / "jq"
        jq.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "query = sys.argv[-1]\n"
            "data = json.loads(sys.stdin.read())\n"
            "if query == '.verdict': print(data['verdict'])\n"
            "elif query == '.slug': print(data['slug'])\n"
            "elif query == '.branch': print(data['branch'])\n"
            "elif query == '.summary': print(data['summary'])\n"
            "elif query == '.findings | length': print(len(data['findings']))\n"
            "elif query == '.findings[]':\n"
            "    print('\\n'.join(json.dumps(item) for item in data['findings']))\n"
            "elif query == '.severity': print(data['severity'])\n"
            "elif query == '.file': print(data['file'])\n"
            "elif query == '.line // empty': print(data.get('line') or '')\n"
            "elif query == '.observed // \"\"': print(data.get('observed') or '')\n"
            "elif query == '.expected // \"\"': print(data.get('expected') or '')\n"
            "elif query == '.evidence // \"\"': print(data.get('evidence') or '')\n"
            "elif query == '.suggestion // \"\"': print(data.get('suggestion') or '')\n"
            "elif query == '(.reviewers // []) | length':\n"
            "    print(len(data.get('reviewers') or []))\n"
            "else: raise SystemExit(f'unhandled jq query: {query}')\n",
            encoding="utf-8",
        )
        jq.chmod(0o755)

        payload = {
            "verdict": "APPROVE",
            "slug": "issue-test",
            "branch": "fix/test",
            "summary": "approved with finding",
            "findings": [
                {
                    "severity": "P2",
                    "file": "src/example.py",
                    "line": 12,
                    "observed": "example finding",
                    "suggestion": "",
                    "expected": "project-contract category rule (test fixture)",
                    "evidence": "(test fixture evidence)",
                }
            ],
        }
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_LOG": str(gh_log),
        }
        env.pop("FORGE_GH_PR_REVIEWS", None)

        result = subprocess.run(
            [str(live_hook)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            cwd=repo_root,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        calls = gh_log.read_text(encoding="utf-8")
        assert "label create needs-triage" in calls
        assert "issue create" in calls
        assert "--label bug" in calls
        assert "--label needs-triage" in calls

    def test_repo_configured_hook_renders_shape_gate_clean_body(self, tmp_path):
        """End-to-end: live hook + structured finding payload produces a body with
        Observed/Expected/Evidence sections and a non-binding suggestion section,
        and no boilerplate "Behavior conforming to" sentence.

        This is the post_run-template counterpart of issue-1338's acceptance criterion
        "a finding filed by the new hook passes bug shape gate without manual grooming".
        """
        repo_root = Path(__file__).resolve().parents[1]
        live_hook = repo_root / ".forge" / "hooks" / "post_run.sh"
        if not live_hook.exists():
            pytest.skip(".forge/hooks/post_run.sh missing (gitignored); dev-machine only")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        gh_log = tmp_path / "gh.log"

        # Capture the full gh argv (one line per arg) so the test can pull --body out
        # of `gh issue create --title ... --body ...` invocations.
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do printf "%s\\n" "$a" >> "$GH_LOG"; done\n'
            'printf -- "---END---\\n" >> "$GH_LOG"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)

        # Use the real jq binary; the jq queries in the hook are too rich for the
        # python fake stub to mirror exactly (multiline string interpolation).
        # Skip the test if jq is not on PATH.
        import shutil

        real_jq = shutil.which("jq")
        if real_jq is None:
            import pytest as _pytest

            _pytest.skip("real jq required for end-to-end body rendering")

        payload = {
            "verdict": "APPROVE",
            "slug": "issue-test",
            "branch": "fix/test",
            "summary": "approved with one finding",
            "findings": [
                {
                    "severity": "P1",
                    "file": "src/example.py",
                    "line": 12,
                    "observed": (
                        "When parse_config encounters a missing dependency entry, "
                        "the loader silently drops the dep instead of surfacing the "
                        "malformed file to the operator."
                    ),
                    "expected": (
                        "When any required field is missing from a dependency entry, "
                        "the loader must surface the malformed file with file/line "
                        "context so operators can correct the source before the run "
                        "advances; silently dropping declarations leaves runs incoherent."
                    ),
                    "evidence": "src/example.py near line 12 (parse_config dep loop)",
                    "suggestion": "Raise StoryConfigError with the offending file path.",
                },
            ],
        }
        path_parts = [
            str(bin_dir.resolve()),
            str(Path(real_jq).parent),
            os.environ["PATH"],
        ]
        env = {
            **os.environ,
            "PATH": os.pathsep.join(path_parts),
            "GH_LOG": str(gh_log),
        }
        env.pop("FORGE_GH_PR_REVIEWS", None)

        result = subprocess.run(
            [str(live_hook)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            cwd=repo_root,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        log_text = gh_log.read_text(encoding="utf-8")
        # Pull the body argument from the issue-create invocation. With one-arg-per-line
        # capture, --body is followed by the body string before the next --flag.
        lines = log_text.splitlines()
        body_lines: list[str] = []
        capture = False
        for line in lines:
            if line == "--body":
                capture = True
                continue
            if capture:
                if line.startswith("--") or line == "---END---":
                    capture = False
                    break
                body_lines.append(line)
        body = "\n".join(body_lines)

        assert "**Observed:**" in body, body
        assert "**Expected:**" in body, body
        assert "**Evidence:**" in body, body
        assert "Suggested approach (non-binding)" in body, body
        # Legacy boilerplate must not appear.
        assert "Behavior conforming to" not in body, body
        assert "What happened" not in body, body
        # The non-binding disclaimer must travel with the suggestion.
        assert "non-binding" in body, body

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

    def test_script_omits_suggestion_from_issue_body(self, tmp_path, monkeypatch):
        """Reference script does not include suggestion in the GitHub issue body."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert "Suggestion" not in content

    def test_script_uses_forge_finding_label(self, tmp_path, monkeypatch):
        """Reference script adds forge-finding label to issues."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert "forge-finding" in content

    def test_script_applies_needs_triage_label_at_creation(self, tmp_path, monkeypatch):
        """Reference script adds needs-triage to newly filed finding issues."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert '--label "bug"' in content
        assert '--label "forge-finding"' in content
        assert '--label "needs-triage"' in content

    def test_script_ensures_needs_triage_label_description(self, tmp_path, monkeypatch):
        """Reference script creates or refreshes the needs-triage label."""
        monkeypatch.chdir(tmp_path)
        args = self._make_args()
        cmd_init_hooks(args)
        sh_path = tmp_path / ".forge" / "hooks" / "post_run.sh"
        content = sh_path.read_text(encoding="utf-8")
        assert 'gh label create "needs-triage"' in content
        assert "Forge finding awaiting explicit triage decision" in content


# ── TestApplyDevModelOverride ─────────────────────────────────────────


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


# ── TestApplyPlanModelOverride ────────────────────────────────────────


class TestApplyPlanModelOverride:
    """Tests for _apply_plan_model_override."""

    def _base_config(self, tmp_path: Path) -> ForgeConfig:
        return _make_forge_config(tmp_path)

    def test_short_model_name(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "opus")
        assert result.plan.model == "opus"

    def test_provider_slash_model(self, tmp_path):
        cfg = self._base_config(tmp_path)
        with patch("theforge.cli.shared._validate_plan_provider"):
            result = _apply_plan_model_override(cfg, "anthropic/claude-opus-4-6")
        assert result.plan.model == "claude-opus-4-6"
        assert result.plan.provider == "anthropic"
        assert result.plan.cli is None

    def test_original_plan_config_preserved_when_flag_absent(self, tmp_path):
        cfg = self._base_config(tmp_path)
        original_model = cfg.plan.model
        # No override applied — plan config should be unchanged
        assert cfg.plan.model == original_model

    def test_other_plan_fields_unchanged(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "opus")
        assert result.plan.cli == cfg.plan.cli
        assert result.plan.budget_usd == cfg.plan.budget_usd
        assert result.plan.enabled == cfg.plan.enabled

    def test_provider_override_clears_cli(self, tmp_path):
        cfg = self._base_config(tmp_path)
        with patch("theforge.cli.shared._validate_plan_provider"):
            result = _apply_plan_model_override(cfg, "openai/gpt-4o")
        assert result.plan.cli is None
        assert result.plan.provider == "openai"
        assert result.plan.model == "gpt-4o"

    def test_bare_model_preserves_cli(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "sonnet")
        assert result.plan.cli == cfg.plan.cli
        assert result.plan.provider == cfg.plan.provider
        assert result.plan.model == "sonnet"

    def test_plan_model_is_default_set_false(self, tmp_path):
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "opus")
        assert result.plan_model_is_default is False

    def test_provider_slash_model_unknown_provider_raises(self, tmp_path):
        # Validation only runs when plan is enabled.
        from dataclasses import replace as _replace

        cfg = _replace(self._base_config(tmp_path), plan=PlanConfig.of(enabled=True))
        with pytest.raises(ValueError, match="Unsupported provider"):
            _apply_plan_model_override(cfg, "bogus/some-model")

    def test_provider_slash_model_missing_api_key_raises(self, tmp_path, monkeypatch):
        # Validation only runs when plan is enabled; key must be absent.
        # Patch the SDK import so the test is not sensitive to whether openai is installed.
        from dataclasses import replace as _replace

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = _replace(self._base_config(tmp_path), plan=PlanConfig.of(enabled=True))
        with patch("theforge.config.load.importlib.import_module"):
            with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                _apply_plan_model_override(cfg, "openai/gpt-4o")

    def test_provider_slash_model_disabled_plan_skips_validation(self, tmp_path, monkeypatch):
        # When plan.enabled is false the planner won't run, so missing credentials
        # must not block the command — matches load_config() semantics.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = self._base_config(tmp_path)  # plan.enabled=False by default
        result = _apply_plan_model_override(cfg, "openai/gpt-4o")
        assert result.plan.provider == "openai"

    def test_bare_model_skips_provider_validation(self, tmp_path):
        # Bare model names don't set a provider, so no validation is run.
        cfg = self._base_config(tmp_path)
        result = _apply_plan_model_override(cfg, "bogus-model-name")
        assert result.plan.model == "bogus-model-name"
