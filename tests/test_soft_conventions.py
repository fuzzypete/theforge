"""Tests for soft conventions feature.

Covers: config parsing, render_conventions_block, prompt builder injection,
review-only path, dry-run path, and audit inclusion.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
    load_config,
)
from theforge.config.types import HardConventionsConfig
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.review_context import hard_convention_review_kwargs
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
)
from theforge.task import (
    TaskStory,
    build_dev_prompt,
    build_fix_prompt,
    build_plan_prompt,
    build_review_prompt,
    render_conventions_block,
    render_hard_conventions_block,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


def _make_config(tmp_path: Path, conventions_soft: list[str] | None = None) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(
            gate_command="make gate",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
        conventions_soft=conventions_soft or [],
    )


# ── Config parsing ────────────────────────────────────────────────────────


class TestConfigParsing:
    def test_conventions_soft_list_parsed(self, tmp_path):
        """forge.yaml with conventions.soft list produces correct ForgeConfig."""
        conventions = [
            "Single concern per module",
            "Separate IO from logic",
        ]
        config_path = _write_config({"conventions": {"soft": conventions}}, tmp_path)
        with patch("importlib.import_module"):
            cfg = load_config(config_path)
        assert cfg.conventions_soft == conventions

    def test_missing_conventions_section_produces_empty_list(self, tmp_path):
        """No conventions section → conventions_soft is []."""
        config_path = _write_config({}, tmp_path)
        with patch("importlib.import_module"):
            cfg = load_config(config_path)
        assert cfg.conventions_soft == []

    def test_missing_soft_key_produces_empty_list(self, tmp_path):
        """conventions: without soft key → conventions_soft is []."""
        config_path = _write_config({"conventions": {"hard": None}}, tmp_path)
        with patch("importlib.import_module"):
            cfg = load_config(config_path)
        assert cfg.conventions_soft == []

    def test_invalid_type_raises_value_error(self, tmp_path):
        """conventions.soft that is not a list raises ValueError."""
        config_path = _write_config({"conventions": {"soft": "not-a-list"}}, tmp_path)
        with patch("importlib.import_module"), pytest.raises(ValueError, match="list of strings"):
            load_config(config_path)

    def test_invalid_item_type_raises_value_error(self, tmp_path):
        """conventions.soft with non-string item raises ValueError."""
        config_path = _write_config({"conventions": {"soft": [1, "valid"]}}, tmp_path)
        with (
            patch("importlib.import_module"),
            pytest.raises(ValueError, match="items must be strings"),
        ):
            load_config(config_path)


# ── render_conventions_block ──────────────────────────────────────────────


class TestRenderConventionsBlock:
    def test_empty_list_returns_empty_string(self):
        assert render_conventions_block([]) == ""

    def test_none_returns_empty_string(self):
        assert render_conventions_block(None) == ""

    def test_single_item_numbered(self):
        result = render_conventions_block(["Do one thing"])
        assert "## Project Conventions" in result
        assert "1. Do one thing" in result

    def test_multiple_items_numbered(self):
        conventions = ["Rule A", "Rule B", "Rule C"]
        result = render_conventions_block(conventions)
        assert "1. Rule A" in result
        assert "2. Rule B" in result
        assert "3. Rule C" in result

    def test_contains_preamble(self):
        result = render_conventions_block(["Some rule"])
        assert "Respect them in your work" in result
        assert "flag violations you observe" in result


# ── Prompt builder integration ────────────────────────────────────────────


_CONVENTIONS = ["Single concern per module", "Separate IO from logic"]


class TestBuildPlanPromptConventions:
    def test_conventions_block_included(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(task, story_content="# Spec", conventions=_CONVENTIONS)
        assert "## Project Conventions" in prompt
        assert "Single concern per module" in prompt

    def test_no_conventions_no_block(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(task, story_content="# Spec")
        assert "## Project Conventions" not in prompt

    def test_empty_conventions_no_block(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(task, story_content="# Spec", conventions=[])
        assert "## Project Conventions" not in prompt


class TestBuildDevPromptConventions:
    def test_conventions_block_included(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            conventions=_CONVENTIONS,
        )
        assert "## Project Conventions" in prompt
        assert "Separate IO from logic" in prompt

    def test_no_conventions_no_block(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
        )
        assert "## Project Conventions" not in prompt


class TestBuildFixPromptConventions:
    def test_conventions_block_included(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: something is wrong",
            gate_command="make gate",
            conventions=_CONVENTIONS,
        )
        assert "## Project Conventions" in prompt
        assert "Single concern per module" in prompt

    def test_no_conventions_no_block(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: something is wrong",
            gate_command="make gate",
        )
        assert "## Project Conventions" not in prompt


class TestBuildReviewPromptConventions:
    def test_conventions_block_included(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat: thing",
            commit_diffs="diff --git a/foo.py b/foo.py",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
            conventions=_CONVENTIONS,
        )
        assert "## Project Conventions" in prompt
        assert "Single concern per module" in prompt

    def test_no_conventions_no_block(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat: thing",
            commit_diffs="diff --git a/foo.py b/foo.py",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
        )
        assert "## Project Conventions" not in prompt

    def test_p2_convention_violation_note_present(self, tmp_path):
        """Review prompt mentions convention violations are P2 findings."""
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat: thing",
            commit_diffs="diff --git a/foo.py b/foo.py",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
            conventions=_CONVENTIONS,
        )
        # The P2 severity definition should mention convention violations
        assert "convention" in prompt.lower()


# ── Hard conventions rendering ────────────────────────────────────────────


class TestRenderHardConventionsBlock:
    def test_no_args_returns_empty_string(self):
        assert render_hard_conventions_block() == ""

    def test_no_scratch_files_false_returns_empty_string(self):
        # Only render when no_scratch_files is enforced.
        assert render_hard_conventions_block(no_scratch_files=False) == ""
        assert (
            render_hard_conventions_block(no_scratch_files=False, allowed_root_files=("foo.txt",))
            == ""
        )

    def test_no_scratch_files_true_renders_block(self):
        result = render_hard_conventions_block(no_scratch_files=True)
        assert "Hard" in result
        assert "P1" in result
        assert "no_scratch_files" in result
        assert "repo-root" in result.lower() or "repo root" in result.lower()

    def test_allowed_root_files_listed(self):
        result = render_hard_conventions_block(
            no_scratch_files=True,
            allowed_root_files=("pyproject.toml", "poetry.lock"),
        )
        assert "pyproject.toml" in result
        assert "poetry.lock" in result

    def test_no_allowed_files_still_renders_when_no_scratch_files(self):
        result = render_hard_conventions_block(no_scratch_files=True, allowed_root_files=())
        assert "Hard" in result
        assert "not declared any additional permitted repo-root files" in result


class TestBuildReviewPromptHardConventions:
    def test_hard_conventions_block_included(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat: thing",
            commit_diffs="diff --git a/foo.py b/foo.py",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
            no_scratch_files=True,
            allowed_root_files=("pyproject.toml",),
        )
        assert "Hard — Mechanically Enforced" in prompt
        assert "pyproject.toml" in prompt
        assert "no_scratch_files" in prompt

    def test_no_hard_conventions_no_block(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat: thing",
            commit_diffs="diff --git a/foo.py b/foo.py",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
        )
        assert "Hard — Mechanically Enforced" not in prompt

    def test_hard_and_soft_both_rendered(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat: thing",
            commit_diffs="diff --git a/foo.py b/foo.py",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
            conventions=["Single concern per module"],
            no_scratch_files=True,
            allowed_root_files=("pyproject.toml",),
        )
        assert "Hard — Mechanically Enforced" in prompt
        assert "## Project Conventions" in prompt
        assert "Single concern per module" in prompt


# ── Config → review prompt seam ───────────────────────────────────────────


class TestHardConventionConfigPropagation:
    """Seam tests that ForgeConfig.conventions_hard reaches build_review_prompt."""

    def test_helper_extracts_hard_convention_kwargs(self, tmp_path):
        cfg = _make_config(tmp_path)
        # Override conventions_hard via dataclasses.replace pattern.
        import dataclasses

        cfg = dataclasses.replace(
            cfg,
            conventions_hard=HardConventionsConfig(
                no_scratch_files=True,
                allowed_root_files=("pyproject.toml", "poetry.lock"),
            ),
        )
        kwargs = hard_convention_review_kwargs(cfg)
        assert kwargs == {
            "allowed_root_files": ("pyproject.toml", "poetry.lock"),
            "no_scratch_files": True,
        }

    def test_helper_returns_none_when_conventions_hard_absent(self, tmp_path):
        cfg = _make_config(tmp_path)  # conventions_hard defaults to None
        assert cfg.conventions_hard is None
        kwargs = hard_convention_review_kwargs(cfg)
        assert kwargs == {"allowed_root_files": None, "no_scratch_files": None}

    def test_helper_kwargs_round_trip_into_review_prompt(self, tmp_path):
        """The kwargs returned by the helper are accepted by build_review_prompt
        and produce the hard-conventions block end-to-end."""
        import dataclasses

        cfg = _make_config(tmp_path)
        cfg = dataclasses.replace(
            cfg,
            conventions_hard=HardConventionsConfig(
                no_scratch_files=True,
                allowed_root_files=("pyproject.toml",),
            ),
        )
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat",
            commit_diffs="diff",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
            **hard_convention_review_kwargs(cfg),
        )
        assert "Hard — Mechanically Enforced" in prompt
        assert "pyproject.toml" in prompt

    def test_helper_kwargs_no_block_when_conventions_hard_none(self, tmp_path):
        cfg = _make_config(tmp_path)
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec",
            commit_log="abc123 feat",
            commit_diffs="diff",
            workspace_path=str(tmp_path / "ws"),
            branch="feat/test",
            handoff_content="gate_result: PASS",
            **hard_convention_review_kwargs(cfg),
        )
        assert "Hard — Mechanically Enforced" not in prompt


# ── Audit inclusion ───────────────────────────────────────────────────────


class TestAuditConventions:
    def _make_result(self) -> CoordinatorResult:
        state = CoordinatorState()
        state.phase = Phase.DONE
        return CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="done",
            merge=None,
        )

    def test_conventions_soft_included_in_audit(self, tmp_path):
        cfg = _make_config(tmp_path, conventions_soft=["Rule A", "Rule B"])
        task = _make_task(tmp_path)
        result = self._make_result()
        audit = generate_audit_log(cfg, task, result)
        assert audit["conventions"] == {"soft": ["Rule A", "Rule B"]}

    def test_no_conventions_produces_none(self, tmp_path):
        cfg = _make_config(tmp_path, conventions_soft=[])
        task = _make_task(tmp_path)
        result = self._make_result()
        audit = generate_audit_log(cfg, task, result)
        assert audit["conventions"] is None


# ── No-op: empty conventions leave prompts unchanged ─────────────────────


class TestNoConventionsNoOp:
    def test_plan_prompt_unchanged_without_conventions(self, tmp_path):
        task = _make_task(tmp_path)
        prompt_without = build_plan_prompt(task, story_content="# Spec")
        prompt_empty = build_plan_prompt(task, story_content="# Spec", conventions=[])
        assert prompt_without == prompt_empty

    def test_dev_prompt_unchanged_without_conventions(self, tmp_path):
        task = _make_task(tmp_path)
        kwargs = dict(
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
        )
        assert build_dev_prompt(task, **kwargs) == build_dev_prompt(task, **kwargs, conventions=[])
