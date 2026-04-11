"""Tests for src/theforge/conventions.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

from theforge.config.types import HardConventionsConfig
from theforge.conventions import (
    ConventionViolation,
    _check_circular_imports,
    _check_hard_conventions_at_git_ref,
    _check_line_counts,
    _check_no_scratch_files,
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
        """File over max_module_lines is reported."""
        py_file = tmp_path / "src" / "theforge" / "big.py"
        _write(py_file, "\n" * 501)  # 501 lines (empty lines count)
        cfg = _make_config(max_module_lines=500)
        violations = _check_line_counts(cfg, tmp_path)
        assert any(v.rule == "max_module_lines" and "big.py" in v.file for v in violations)

    def test_line_count_ok(self, tmp_path):
        """File at exactly the limit is not reported."""
        py_file = tmp_path / "src" / "theforge" / "small.py"
        _write(py_file, "\n" * 500)  # 500 lines
        cfg = _make_config(max_module_lines=500)
        violations = _check_line_counts(cfg, tmp_path)
        assert not any(v.rule == "max_module_lines" and "small.py" in v.file for v in violations)

    def test_test_file_line_count_violation(self, tmp_path):
        """Test file over max_test_file_lines is reported with correct rule."""
        py_file = tmp_path / "tests" / "test_big.py"
        _write(py_file, "\n" * 1001)
        cfg = _make_config(max_test_file_lines=1000)
        violations = _check_line_counts(cfg, tmp_path)
        assert any(v.rule == "max_test_file_lines" and "test_big.py" in v.file for v in violations)

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
        """Two files that import each other → no_circular_imports violation."""
        pkg = tmp_path / "src" / "theforge"
        pkg.mkdir(parents=True)
        _write(pkg / "a.py", "from theforge import b\n")
        _write(pkg / "b.py", "from theforge import a\n")
        violations = _check_circular_imports(tmp_path)
        assert any(v.rule == "no_circular_imports" for v in violations)

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
        """src/theforge/bar.py with no tests/test_bar.py → violation."""
        _write(tmp_path / "src" / "theforge" / "bar.py", "# module\n")
        (tmp_path / "tests").mkdir(parents=True)
        violations = _check_test_mirrors(tmp_path)
        assert any(v.rule == "test_mirrors_source" and "bar.py" in v.file for v in violations)

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
        """ConventionViolation has correct fields."""
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
        assert v.blocking is True


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

    def test_multiple_root_py_files_all_reported(self, tmp_path):
        """Multiple .py files in root are all reported."""
        _write(tmp_path / "test_exec.py", "# scratch\n")
        _write(tmp_path / "fake_forge.py", "# scratch\n")
        violations = _check_no_scratch_files(tmp_path)
        files = {v.file for v in violations if v.rule == "no_scratch_files"}
        assert "test_exec.py" in files
        assert "fake_forge.py" in files

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

    def test_no_py_files_ok(self, tmp_path):
        """Empty root directory produces no violations."""
        violations = _check_no_scratch_files(tmp_path)
        assert violations == []

    def test_violation_is_blocking(self, tmp_path):
        """no_scratch_files violations are blocking by default."""
        _write(tmp_path / "oops.py", "# scratch\n")
        violations = _check_no_scratch_files(tmp_path)
        assert all(v.blocking for v in violations if v.rule == "no_scratch_files")

    def test_check_hard_conventions_respects_no_scratch_files_flag(self, tmp_path):
        """no_scratch_files=False disables the check."""
        _write(tmp_path / "scratch.py", "# scratch\n")
        cfg = _make_config(
            no_scratch_files=False, no_circular_imports=False, test_mirrors_source=False
        )
        violations = check_hard_conventions(cfg, tmp_path)
        assert not any(v.rule == "no_scratch_files" for v in violations)

    def test_check_hard_conventions_includes_scratch_check_when_enabled(self, tmp_path):
        """no_scratch_files=True (default) surfaces root .py files via check_hard_conventions."""
        _write(tmp_path / "scratch.py", "# scratch\n")
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
