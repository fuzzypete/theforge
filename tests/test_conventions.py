"""Tests for src/theforge/conventions.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

from theforge.config.types import HardConventionsConfig
from theforge.conventions import (
    _ALLOWED_ROOT_FILES,
    ConventionViolation,
    _check_circular_imports,
    _check_hard_conventions_at_git_ref,
    _check_line_counts,
    _check_no_scratch_files,
    _check_stack_neutrality,
    _check_test_mirrors,
    check_hard_conventions,
    new_hard_convention_violations_since_ref,
)


def _make_config(**kwargs) -> HardConventionsConfig:
    defaults = dict(
        max_module_lines=500,
        max_test_file_lines=1000,
        no_circular_imports=True,
        test_mirrors_source=True,
        no_scratch_files=True,
    )
    defaults.update(kwargs)
    return HardConventionsConfig(**defaults)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── Line count tests ──────────────────────────────────────────────────


class TestLineCountCheck:
    def test_line_count_violation(self, tmp_path):
        """File over max_module_lines is reported with blocking=False."""
        py_file = tmp_path / "src" / "theforge" / "big.py"
        _write(py_file, "\n" * 501)  # 501 lines (empty lines count)
        cfg = _make_config(max_module_lines=500)
        violations = _check_line_counts(cfg, tmp_path)
        assert any(v.rule == "max_module_lines" and "big.py" in v.file for v in violations)
        loc_violations = [v for v in violations if v.rule == "max_module_lines"]
        assert all(v.blocking is False for v in loc_violations)

    def test_line_count_ok(self, tmp_path):
        """File at exactly the limit is not reported."""
        py_file = tmp_path / "src" / "theforge" / "small.py"
        _write(py_file, "\n" * 500)  # 500 lines
        cfg = _make_config(max_module_lines=500)
        violations = _check_line_counts(cfg, tmp_path)
        assert not any(v.rule == "max_module_lines" and "small.py" in v.file for v in violations)

    def test_test_file_line_count_violation(self, tmp_path):
        """Test file over max_test_file_lines is reported with correct rule and blocking=False."""
        py_file = tmp_path / "tests" / "test_big.py"
        _write(py_file, "\n" * 1001)
        cfg = _make_config(max_test_file_lines=1000)
        violations = _check_line_counts(cfg, tmp_path)
        assert any(v.rule == "max_test_file_lines" and "test_big.py" in v.file for v in violations)
        test_loc_violations = [v for v in violations if v.rule == "max_test_file_lines"]
        assert all(v.blocking is False for v in test_loc_violations)

    def test_test_file_line_count_ok(self, tmp_path):
        """Test file at limit is not reported."""
        py_file = tmp_path / "tests" / "test_ok.py"
        _write(py_file, "\n" * 1000)
        cfg = _make_config(max_test_file_lines=1000)
        violations = _check_line_counts(cfg, tmp_path)
        assert not any(
            v.rule == "max_test_file_lines" and "test_ok.py" in v.file for v in violations
        )

    def test_missing_src_dir_ok(self, tmp_path):
        """No src/ dir → no violations, no crash."""
        cfg = _make_config()
        violations = _check_line_counts(cfg, tmp_path)
        assert violations == []


# ── Circular import tests ─────────────────────────────────────────────


class TestCircularImportCheck:
    def test_circular_import_detected(self, tmp_path):
        """Two files that import each other → no_circular_imports violation (blocking=True)."""
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        _write(pkg / "a.py", "from theforge import b\n")
        _write(pkg / "b.py", "from theforge import a\n")
        violations = _check_circular_imports(tmp_path)
        assert any(v.rule == "no_circular_imports" for v in violations)
        circular_violations = [v for v in violations if v.rule == "no_circular_imports"]
        assert all(v.blocking is True for v in circular_violations)

    def test_no_circular_imports(self, tmp_path):
        """Linear imports → no violation."""
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        _write(pkg / "a.py", "# standalone\n")
        _write(pkg / "b.py", "from theforge import a\n")
        _write(pkg / "c.py", "from theforge import b\n")
        violations = _check_circular_imports(tmp_path)
        assert not any(v.rule == "no_circular_imports" for v in violations)

    def test_missing_src_dir_ok(self, tmp_path):
        """No src/ dir → no violations."""
        violations = _check_circular_imports(tmp_path)
        assert violations == []

    def test_syntax_error_skipped(self, tmp_path):
        """Syntax-error files are skipped without crashing."""
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        _write(pkg / "bad.py", "def foo(\n")  # syntax error
        violations = _check_circular_imports(tmp_path)
        # no crash; bad.py has no imports to form a cycle
        assert not any(v.rule == "no_circular_imports" for v in violations)

    def test_cycle_through_init(self, tmp_path):
        """Cycle via package __init__.py is detected (init normalised to pkg name)."""
        pkg = tmp_path / "src" / "theforge"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        # a.py imports the sub package; sub/__init__.py imports a
        _write(pkg / "a.py", "from theforge import sub\n")
        _write(sub / "__init__.py", "from theforge import a\n")
        violations = _check_circular_imports(tmp_path)
        assert any(v.rule == "no_circular_imports" for v in violations)

    def test_init_relative_import_no_false_positive(self, tmp_path):
        """from . import a in sub/__init__.py resolves to sub.a, not top-level a."""
        # Layout: a.py → theforge.sub (package), sub/__init__.py → .a (=sub.a), sub/a.py
        # This is acyclic: a → sub → sub.a (no cycle back to a)
        pkg = tmp_path / "src" / "theforge"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        _write(pkg / "a.py", "from theforge import sub\n")
        _write(sub / "__init__.py", "from . import a\n")
        _write(sub / "a.py", "# leaf\n")
        violations = _check_circular_imports(tmp_path)
        assert not any(v.rule == "no_circular_imports" for v in violations)

    def test_external_src_package_ignored(self, tmp_path):
        """Cycles in src/other_pkg are not reported (spec scopes to src/theforge)."""
        # Create a cycle in an unrelated package under src/
        other = tmp_path / "src" / "other_pkg"
        other.mkdir(parents=True)
        _write(other / "x.py", "from other_pkg import y\n")
        _write(other / "y.py", "from other_pkg import x\n")
        # Ensure src/theforge exists but has no cycle
        (tmp_path / "src" / "theforge").mkdir(parents=True)
        violations = _check_circular_imports(tmp_path)
        assert not any(v.rule == "no_circular_imports" for v in violations)


# ── Test mirror tests ─────────────────────────────────────────────────


class TestTestMirrorCheck:
    def test_missing_test_mirror(self, tmp_path):
        """src/theforge/bar.py with no tests/test_bar.py → violation (blocking=True)."""
        _write(tmp_path / "src" / "theforge" / "bar.py", "# module\n")
        (tmp_path / "tests").mkdir(parents=True)
        violations = _check_test_mirrors(tmp_path)
        assert any(v.rule == "test_mirrors_source" and "bar.py" in v.file for v in violations)
        mirror_violations = [v for v in violations if v.rule == "test_mirrors_source"]
        assert all(v.blocking is True for v in mirror_violations)

    def test_test_mirror_ok(self, tmp_path):
        """Mirror exists → no violation."""
        _write(tmp_path / "src" / "theforge" / "bar.py", "# module\n")
        _write(tmp_path / "tests" / "test_bar.py", "# tests\n")
        violations = _check_test_mirrors(tmp_path)
        assert not any(v.rule == "test_mirrors_source" and "bar.py" in v.file for v in violations)

    def test_package_mirror_glob(self, tmp_path):
        """src/theforge/pkg/ with tests/test_pkg_foo.py → no violation."""
        (tmp_path / "src" / "theforge" / "pkg").mkdir(parents=True)
        _write(tmp_path / "tests" / "test_pkg_foo.py", "# tests\n")
        violations = _check_test_mirrors(tmp_path)
        assert not any(v.rule == "test_mirrors_source" and "pkg" in v.file for v in violations)

    def test_package_mirror_dir(self, tmp_path):
        """src/theforge/pkg/ with tests/test_pkg/ → no violation."""
        (tmp_path / "src" / "theforge" / "pkg").mkdir(parents=True)
        (tmp_path / "tests" / "test_pkg").mkdir(parents=True)
        violations = _check_test_mirrors(tmp_path)
        assert not any(v.rule == "test_mirrors_source" and "pkg" in v.file for v in violations)

    def test_package_missing_mirror(self, tmp_path):
        """src/theforge/pkg/ with no tests/ mirror → violation."""
        (tmp_path / "src" / "theforge" / "pkg").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)
        violations = _check_test_mirrors(tmp_path)
        assert any(v.rule == "test_mirrors_source" and "pkg" in v.file for v in violations)

    def test_dunder_skipped(self, tmp_path):
        """__init__.py and __pycache__ are not checked."""
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        _write(pkg / "__init__.py", "# init\n")
        (pkg / "__pycache__").mkdir()
        (tmp_path / "tests").mkdir(parents=True)
        violations = _check_test_mirrors(tmp_path)
        assert violations == []

    def test_underscore_module_requires_mirror(self, tmp_path):
        """src/theforge/_secret.py with no mirror → violation (only __init__.py exempt)."""
        _write(tmp_path / "src" / "theforge" / "_secret.py", "# private\n")
        (tmp_path / "tests").mkdir(parents=True)
        violations = _check_test_mirrors(tmp_path)
        assert any(v.rule == "test_mirrors_source" and "_secret.py" in v.file for v in violations)

    def test_missing_src_or_tests_ok(self, tmp_path):
        """Missing src or tests dir → no crash, no violations."""
        violations = _check_test_mirrors(tmp_path)
        assert violations == []


# ── check_hard_conventions integration ───────────────────────────────


def test_allowed_root_files_whitelist_is_stack_neutral() -> None:
    assert "conftest.py" not in _ALLOWED_ROOT_FILES
    assert "tox.ini" not in _ALLOWED_ROOT_FILES
    assert "pyproject.toml" not in _ALLOWED_ROOT_FILES
    assert "setup.py" not in _ALLOWED_ROOT_FILES
    assert "setup.cfg" not in _ALLOWED_ROOT_FILES
    assert "package-lock.json" not in _ALLOWED_ROOT_FILES
    assert "yarn.lock" not in _ALLOWED_ROOT_FILES
    assert "pnpm-lock.yaml" not in _ALLOWED_ROOT_FILES
    assert "bun.lockb" not in _ALLOWED_ROOT_FILES


class TestStackNeutralityCheck:
    def test_shared_model_prefix_smell_reports_file_line_and_rule(self, tmp_path):
        _write(
            tmp_path / "src" / "theforge" / "config" / "types.py",
            "pytest_target = None\n",
        )
        violations = _check_stack_neutrality(tmp_path)
        assert len(violations) == 1
        violation = violations[0]
        assert violation.rule == "stack_neutrality_shared_model_prefix"
        assert violation.file == "src/theforge/config/types.py"
        assert "line 1" in violation.detail
        assert "pytest_target" in violation.detail
        assert "shared schemas may not encode stack-specific concepts" in violation.detail

    def test_literal_tool_command_smell_reports_prompt_rule(self, tmp_path):
        _write(
            tmp_path / "src" / "theforge" / "task" / "dev_prompts.py",
            'prompt = "run pytest before finishing"\n',
        )
        violations = _check_stack_neutrality(tmp_path)
        assert len(violations) == 1
        violation = violations[0]
        assert violation.rule == "stack_neutrality_literal_tool_command"
        assert violation.file == "src/theforge/task/dev_prompts.py"
        assert "configured commands" in violation.detail

    def test_hardcoded_layout_smell_reports_generic_scaffolding_rule(self, tmp_path):
        _write(
            tmp_path / "src" / "theforge" / "task" / "plan_prompts.py",
            'template = "put tests in tests/"\n',
        )
        violations = _check_stack_neutrality(tmp_path)
        assert len(violations) == 1
        violation = violations[0]
        assert violation.rule == "stack_neutrality_hardcoded_layout"
        assert "generated scaffolding must use generic names" in violation.detail

    def test_provider_adapter_code_is_out_of_scope(self, tmp_path):
        _write(
            tmp_path / "src" / "theforge" / "runners" / "adapters" / "openai.py",
            'cmd = "pytest"\n',
        )
        violations = _check_stack_neutrality(tmp_path)
        assert violations == []

    def test_forge_yaml_is_exempt(self, tmp_path):
        _write(tmp_path / "forge.yaml", 'validation:\n  gate_command: "pytest tests/ -v"\n')
        violations = _check_stack_neutrality(tmp_path)
        assert violations == []

    def test_python_specific_example_marker_is_exempt(self, tmp_path):
        _write(
            tmp_path / "src" / "theforge" / "config" / "python_example.py",
            '# Python-specific example\ncmd = "pytest tests/ -v"\n',
        )
        violations = _check_stack_neutrality(tmp_path)
        assert violations == []


class TestCheckHardConventions:
    def test_all_checks_run(self, tmp_path):
        """check_hard_conventions calls all enabled checks."""
        # Set up a violation in each category
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        # line count violation
        _write(pkg / "big.py", "\n" * 6)
        # missing test mirror
        (tmp_path / "tests").mkdir(parents=True)
        cfg = _make_config(max_module_lines=5, no_circular_imports=False, test_mirrors_source=True)
        violations = check_hard_conventions(cfg, tmp_path)
        rules = {v.rule for v in violations}
        assert "max_module_lines" in rules
        assert "test_mirrors_source" in rules

    def test_disabled_checks_not_run(self, tmp_path):
        """no_circular_imports=False and test_mirrors_source=False → those checks skipped."""
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        _write(pkg / "a.py", "from theforge import b\n")
        _write(pkg / "b.py", "from theforge import a\n")
        (tmp_path / "tests").mkdir(parents=True)
        cfg = _make_config(no_circular_imports=False, test_mirrors_source=False)
        violations = check_hard_conventions(cfg, tmp_path)
        assert not any(v.rule == "no_circular_imports" for v in violations)
        assert not any(v.rule == "test_mirrors_source" for v in violations)

    def test_violations_cleared_on_clean_pass(self, tmp_path):
        """Violations list is [] when all checks pass (stale state not returned)."""
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        _write(pkg / "ok.py", "# fine\n")
        _write(tmp_path / "tests" / "test_ok.py", "# fine\n")
        cfg = _make_config()
        violations = check_hard_conventions(cfg, tmp_path)
        assert violations == []

    def test_violation_dataclass_fields(self, tmp_path):
        """ConventionViolation has correct fields; line-count violations are non-blocking."""
        _write(tmp_path / "src" / "theforge" / "over.py", "\n" * 6)
        (tmp_path / "tests").mkdir(parents=True)
        cfg = _make_config(
            max_module_lines=5, no_circular_imports=False, test_mirrors_source=False
        )
        violations = check_hard_conventions(cfg, tmp_path)
        assert len(violations) >= 1
        v = violations[0]
        assert isinstance(v, ConventionViolation)
        assert v.rule
        assert v.file
        assert v.detail
        # max_module_lines is a hygiene/follow-up rule — non-blocking
        assert v.blocking is False


class TestConventionBaseline:
    def test_check_hard_conventions_at_git_ref_reads_baseline_tree(self, tmp_path):
        """Baseline checks run against the requested git snapshot, not the worktree."""
        _init_git_repo(tmp_path)
        _write(tmp_path / "src" / "theforge" / "ok.py", "# fine\n")
        _write(tmp_path / "tests" / "test_ok.py", "# fine\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "baseline")
        baseline_ref = _git(tmp_path, "rev-parse", "HEAD")

        _write(tmp_path / "src" / "theforge" / "ok.py", "\n" * 501)

        cfg = _make_config(no_circular_imports=False, test_mirrors_source=False)
        violations = _check_hard_conventions_at_git_ref(cfg, tmp_path, baseline_ref)
        assert violations == []

    def test_check_hard_conventions_at_git_ref_handles_missing_tests_dir(self, tmp_path):
        """Baseline archive should not fail when tests/ only exists on the branch."""
        _init_git_repo(tmp_path)
        _write(tmp_path / "src" / "theforge" / "ok.py", "# fine\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "baseline without tests")
        baseline_ref = _git(tmp_path, "rev-parse", "HEAD")

        _write(tmp_path / "tests" / "test_ok.py", "# added later\n")

        cfg = _make_config(no_circular_imports=False, test_mirrors_source=False)
        violations = _check_hard_conventions_at_git_ref(cfg, tmp_path, baseline_ref)
        assert violations == []

    def test_new_hard_convention_violations_since_ref_only_returns_net_new(self, tmp_path):
        """Pre-existing debt at the branch point should not be treated as blocking."""
        _init_git_repo(tmp_path)
        _write(tmp_path / "src" / "theforge" / "legacy.py", "\n" * 501)
        _write(tmp_path / "tests" / "test_legacy.py", "# mirror\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "baseline debt")
        baseline_ref = _git(tmp_path, "rev-parse", "HEAD")

        _write(tmp_path / "src" / "theforge" / "new_hotness.py", "\n" * 501)

        cfg = _make_config(no_circular_imports=False, test_mirrors_source=False)
        current, net_new = new_hard_convention_violations_since_ref(cfg, tmp_path, baseline_ref)

        assert any("legacy.py" in v.file for v in current)
        assert any("new_hotness.py" in v.file for v in current)
        assert not any("legacy.py" in v.file for v in net_new)
        assert any("new_hotness.py" in v.file for v in net_new)

    def test_worsened_line_count_violation_is_not_treated_as_net_new(self, tmp_path):
        """A pre-existing line-count violation remains existing debt if it grows."""
        _init_git_repo(tmp_path)
        _write(tmp_path / "src" / "theforge" / "legacy.py", "\n" * 501)
        _write(tmp_path / "tests" / "test_legacy.py", "# mirror\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "baseline debt")
        baseline_ref = _git(tmp_path, "rev-parse", "HEAD")

        _write(tmp_path / "src" / "theforge" / "legacy.py", "\n" * 520)

        cfg = _make_config(no_circular_imports=False, test_mirrors_source=False)
        current, net_new = new_hard_convention_violations_since_ref(cfg, tmp_path, baseline_ref)

        assert any("legacy.py" in v.file for v in current)
        assert not any("legacy.py" in v.file for v in net_new)


# ── Scratch file check tests ──────────────────────────────────────────


class TestNoScratchFilesCheck:
    def test_py_file_in_root_is_violation(self, tmp_path):
        """A .py file directly in the project root is a violation."""
        _write(tmp_path / "scratch.py", "# oops\n")
        violations = _check_no_scratch_files(tmp_path)
        assert any(v.rule == "no_scratch_files" and "scratch.py" in v.file for v in violations)

    def test_extensionless_script_in_root_is_violation(self, tmp_path):
        """Scripts without extension (e.g. bin_script, fake_forge) are flagged."""
        (tmp_path / "bin_script").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
        (tmp_path / "fake_forge").write_text("#!/usr/bin/env python\n", encoding="utf-8")
        violations = _check_no_scratch_files(tmp_path)
        files = {v.file for v in violations if v.rule == "no_scratch_files"}
        assert "bin_script" in files
        assert "fake_forge" in files

    def test_multiple_root_files_all_reported(self, tmp_path):
        """Multiple unlisted files in root are all reported."""
        _write(tmp_path / "test_exec.py", "# scratch\n")
        (tmp_path / "bin_script2").write_text("#!/bin/bash\n", encoding="utf-8")
        violations = _check_no_scratch_files(tmp_path)
        files = {v.file for v in violations if v.rule == "no_scratch_files"}
        assert "test_exec.py" in files
        assert "bin_script2" in files

    def test_allowed_root_files_not_flagged(self, tmp_path):
        """Known-good stack-neutral root files are not flagged."""
        (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# Readme\n", encoding="utf-8")
        violations = _check_no_scratch_files(tmp_path)
        assert violations == []

    def test_stack_specific_root_files_are_not_universally_allowed(self, tmp_path):
        _write(tmp_path / "pyproject.toml", "[project]\n")
        _write(tmp_path / "conftest.py", "# root conftest\n")
        _write(tmp_path / "tox.ini", "[tox]\n")
        _write(tmp_path / "package-lock.json", "{}\n")
        _write(tmp_path / "yarn.lock", "# yarn lockfile\n")
        violations = _check_no_scratch_files(tmp_path)
        files = {v.file for v in violations if v.rule == "no_scratch_files"}
        assert "pyproject.toml" in files
        assert "conftest.py" in files
        assert "tox.ini" in files
        assert "package-lock.json" in files
        assert "yarn.lock" in files

    def test_project_local_allowed_root_files_extend_whitelist(self, tmp_path):
        _write(tmp_path / "pyproject.toml", "[project]\n")
        violations = _check_no_scratch_files(tmp_path, ("pyproject.toml",))
        assert violations == []

    def test_dotfiles_not_flagged(self, tmp_path):
        """Hidden files (dotfiles) are always allowed."""
        (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        (tmp_path / ".env").write_text("KEY=val\n", encoding="utf-8")
        violations = _check_no_scratch_files(tmp_path)
        assert violations == []

    def test_conftest_py_requires_project_local_allowance(self, tmp_path):
        """conftest.py at root is stack-specific and must come from project config."""
        _write(tmp_path / "conftest.py", "# root conftest\n")
        violations = _check_no_scratch_files(tmp_path)
        assert any(v.rule == "no_scratch_files" for v in violations)

    def test_py_in_src_is_ok(self, tmp_path):
        """Python files under src/ are not flagged."""
        _write(tmp_path / "src" / "theforge" / "module.py", "# fine\n")
        violations = _check_no_scratch_files(tmp_path)
        assert violations == []

    def test_py_in_tests_is_ok(self, tmp_path):
        """Python files under tests/ are not flagged."""
        _write(tmp_path / "tests" / "test_module.py", "# fine\n")
        violations = _check_no_scratch_files(tmp_path)
        assert violations == []

    def test_directories_not_flagged(self, tmp_path):
        """Directories in the root are never flagged (only files are checked)."""
        (tmp_path / "scratch_dir").mkdir()
        violations = _check_no_scratch_files(tmp_path)
        assert violations == []

    def test_empty_root_ok(self, tmp_path):
        """Empty root directory produces no violations."""
        violations = _check_no_scratch_files(tmp_path)
        assert violations == []

    def test_violation_is_blocking(self, tmp_path):
        """no_scratch_files violations are blocking by default."""
        (tmp_path / "oops").write_text("scratch\n", encoding="utf-8")
        violations = _check_no_scratch_files(tmp_path)
        assert all(v.blocking for v in violations if v.rule == "no_scratch_files")

    def test_check_hard_conventions_respects_no_scratch_files_flag(self, tmp_path):
        """no_scratch_files=False disables the check."""
        (tmp_path / "bin_script").write_text("#!/bin/bash\n", encoding="utf-8")
        cfg = _make_config(
            no_scratch_files=False, no_circular_imports=False, test_mirrors_source=False
        )
        violations = check_hard_conventions(cfg, tmp_path)
        assert not any(v.rule == "no_scratch_files" for v in violations)

    def test_check_hard_conventions_includes_scratch_check_when_enabled(self, tmp_path):
        """no_scratch_files=True surfaces unlisted root files via check_hard_conventions."""
        (tmp_path / "bin_script").write_text("#!/bin/bash\n", encoding="utf-8")
        (tmp_path / "src" / "theforge").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)
        cfg = _make_config(no_circular_imports=False, test_mirrors_source=False)
        violations = check_hard_conventions(cfg, tmp_path)
        assert any(v.rule == "no_scratch_files" for v in violations)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout.strip()


def _init_git_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
