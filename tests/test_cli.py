"""Tests for CLI: spec frontmatter parsing and config discovery."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli import _build_task, _find_config, _parse_spec_frontmatter, cmd_ideate
from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.ideate import IdeationResult, IdeationRound

# ── Helpers for cmd_ideate tests ─────────────────────────────────────

_SOLO_PROFILE = ModelProfile(
    name="solo",
    cli="claude",
    model="sonnet",
    budget_usd=1.0,
    timeout_seconds=300,
    allowed_tools=("Read",),
)

_VALID_SPEC = """\
---
name: "Test Feature"
slug: test-feature
file_scope: []
pytest_target: tests/
---

# Test Feature

## Problem
A test problem.
"""


def _make_forge_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=_SOLO_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[_SOLO_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _make_ideation_result(
    tmp_path: Path,
    *,
    write_spec: bool = True,
    human_decision_required: bool = False,
    residual_divergence: list[str] | None = None,
) -> IdeationResult:
    spec_path: Path | None = None
    if write_spec:
        spec_path = tmp_path / "specs" / "test-feature.md"
    round_ = IdeationRound(
        round_number=1,
        phase1_outputs={"solo": "ideas"},
        phase2_outputs={},
        converged_items=["item1"],
        divergent_items=[],
        synthesis_output=_VALID_SPEC,
    )
    return IdeationResult(
        success=True,
        spec_path=spec_path,
        rounds=[round_],
        final_synthesis=_VALID_SPEC,
        residual_divergence=residual_divergence or [],
        total_cost_usd=0.42,
        human_decision_required=human_decision_required,
    )


def _make_args(
    brief: str = "build a thing",
    *,
    output: str | None = None,
    rounds: int = 2,
    dry_run: bool = False,
    config: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        brief=brief,
        output=output,
        rounds=rounds,
        dry_run=dry_run,
        config=config,
    )


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


# ── cmd_ideate CLI integration tests ─────────────────────────────────


class TestCmdIdeate:
    """CLI-level tests for the `forge ideate` command."""

    def _run(
        self,
        tmp_path: Path,
        args: argparse.Namespace,
        ideation_result: IdeationResult | None = None,
    ) -> tuple[int, MagicMock]:
        """Run cmd_ideate with mocked config loading and run_ideation."""
        config = _make_forge_config(tmp_path)
        if ideation_result is None:
            ideation_result = _make_ideation_result(tmp_path)

        config_file = tmp_path / "forge.yaml"
        config_file.write_text("project: test\n", encoding="utf-8")
        args.config = str(config_file)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_ideation", return_value=ideation_result) as mock_run,
            patch("theforge.cli._find_config", return_value=config_file),
        ):
            rc = cmd_ideate(args)

        return rc, mock_run

    def test_dry_run_writes_audit_but_not_spec(self, tmp_path, capsys):
        """--dry-run: audit is always written; no spec file; synthesis printed to stdout."""
        result = _make_ideation_result(tmp_path, write_spec=False)
        args = _make_args(dry_run=True)
        rc, mock_run = self._run(tmp_path, args, ideation_result=result)

        assert rc == 0
        # Synthesis printed to stdout
        captured = capsys.readouterr()
        assert "Test Feature" in captured.out

        # Audit file is written even on dry-run
        audit_path = tmp_path / "forge_ideation_audit.yaml"
        assert audit_path.exists(), "Audit file must be written even for --dry-run"

        # run_ideation called with no output path (dry-run passes None)
        call_kwargs = mock_run.call_args
        assert call_kwargs.args[2] is None  # output_path is None
        assert call_kwargs.kwargs.get("specs_dir") is None  # no specs_dir either

    def test_normal_run_writes_audit_and_invokes_with_specs_dir(self, tmp_path):
        """Normal run: audit written; run_ideation given specs_dir."""
        result = _make_ideation_result(tmp_path)
        args = _make_args()
        rc, mock_run = self._run(tmp_path, args, ideation_result=result)

        assert rc == 0
        audit_path = tmp_path / "forge_ideation_audit.yaml"
        assert audit_path.exists()

        # run_ideation receives specs_dir (not explicit output_path)
        call_kwargs = mock_run.call_args
        assert call_kwargs.args[2] is None  # explicit output_path is None
        assert call_kwargs.kwargs.get("specs_dir") == tmp_path / "specs"

    def test_output_flag_passes_explicit_path(self, tmp_path):
        """--output passes the resolved path as output_path to run_ideation."""
        out = str(tmp_path / "my-spec.md")
        args = _make_args(output=out)
        rc, mock_run = self._run(tmp_path, args)

        assert rc == 0
        call_kwargs = mock_run.call_args
        assert call_kwargs.args[2] == Path(out).resolve()
        assert call_kwargs.kwargs.get("specs_dir") is None

    def test_brief_file_input(self, tmp_path):
        """Brief given as a .md file path: content is read and passed to run_ideation."""
        brief_file = tmp_path / "brief.md"
        brief_file.write_text("# My Brief\nBuild something great.", encoding="utf-8")
        args = _make_args(brief=str(brief_file))

        config = _make_forge_config(tmp_path)
        result = _make_ideation_result(tmp_path)
        config_file = tmp_path / "forge.yaml"
        config_file.write_text("project: test\n", encoding="utf-8")
        args.config = str(config_file)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli.run_ideation", return_value=result) as mock_run,
            patch("theforge.cli._find_config", return_value=config_file),
        ):
            rc = cmd_ideate(args)

        assert rc == 0
        passed_brief = mock_run.call_args.args[1]
        assert "Build something great." in passed_brief

    def test_invalid_rounds_returns_nonzero(self, tmp_path):
        """--rounds out of range returns error code 1."""
        config = _make_forge_config(tmp_path)
        config_file = tmp_path / "forge.yaml"
        config_file.write_text("project: test\n", encoding="utf-8")
        args = _make_args(rounds=5)
        args.config = str(config_file)

        with (
            patch("theforge.cli.load_config", return_value=config),
            patch("theforge.cli._find_config", return_value=config_file),
        ):
            rc = cmd_ideate(args)

        assert rc == 1
