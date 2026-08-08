"""Tests for src/theforge/line_count_conventions.py."""

from __future__ import annotations

from pathlib import Path

from theforge.config.types import HardConventionsConfig
from theforge.convention_types import ConventionViolation
from theforge.line_count_conventions import (
    check_line_counts,
    fail_closed_module_violations,
    module_line_ceiling,
    module_line_counts,
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


class TestLineCountScan:
    def test_line_count_violation(self, tmp_path):
        """File over max_module_lines is reported with blocking=False."""
        py_file = tmp_path / "src" / "theforge" / "big.py"
        _write(py_file, "\n" * 501)  # 501 lines (empty lines count)
        cfg = _make_config(max_module_lines=500)
        violations = check_line_counts(cfg, tmp_path)
        assert any(v.rule == "max_module_lines" and "big.py" in v.file for v in violations)
        loc_violations = [v for v in violations if v.rule == "max_module_lines"]
        assert all(v.blocking is False for v in loc_violations)

    def test_line_count_ok(self, tmp_path):
        """File at exactly the limit is not reported."""
        py_file = tmp_path / "src" / "theforge" / "small.py"
        _write(py_file, "\n" * 500)  # 500 lines
        cfg = _make_config(max_module_lines=500)
        violations = check_line_counts(cfg, tmp_path)
        assert not any(v.rule == "max_module_lines" and "small.py" in v.file for v in violations)

    def test_test_file_line_count_violation(self, tmp_path):
        """Test file over max_test_file_lines is reported with correct rule and blocking=False."""
        py_file = tmp_path / "tests" / "test_big.py"
        _write(py_file, "\n" * 1001)
        cfg = _make_config(max_test_file_lines=1000)
        violations = check_line_counts(cfg, tmp_path)
        assert any(v.rule == "max_test_file_lines" and "test_big.py" in v.file for v in violations)
        test_loc_violations = [v for v in violations if v.rule == "max_test_file_lines"]
        assert all(v.blocking is False for v in test_loc_violations)

    def test_test_file_line_count_ok(self, tmp_path):
        """Test file at limit is not reported."""
        py_file = tmp_path / "tests" / "test_ok.py"
        _write(py_file, "\n" * 1000)
        cfg = _make_config(max_test_file_lines=1000)
        violations = check_line_counts(cfg, tmp_path)
        assert not any(
            v.rule == "max_test_file_lines" and "test_ok.py" in v.file for v in violations
        )

    def test_missing_src_dir_ok(self, tmp_path):
        """No src/ dir → no violations, no crash."""
        cfg = _make_config()
        violations = check_line_counts(cfg, tmp_path)
        assert violations == []

    def test_line_count_under_configured_root_outside_src(self, tmp_path):
        """Oversized file under a configured root outside src/ is reported (AC3)."""
        py_file = tmp_path / "analysis" / "big.py"
        _write(py_file, "\n" * 501)
        cfg = _make_config(max_module_lines=500, package_roots=("analysis",))
        violations = check_line_counts(cfg, tmp_path)
        assert any(
            v.rule == "max_module_lines" and "analysis/big.py" in v.file for v in violations
        )

    def test_line_count_configured_roots_no_double_report(self, tmp_path):
        """Overlapping configured roots don't report the same file twice."""
        py_file = tmp_path / "src" / "pipeline" / "big.py"
        _write(py_file, "\n" * 501)
        cfg = _make_config(max_module_lines=500, package_roots=("src", "src/pipeline"))
        violations = check_line_counts(cfg, tmp_path)
        matches = [v for v in violations if v.rule == "max_module_lines" and "big.py" in v.file]
        assert len(matches) == 1


class TestModuleCeilings:
    def test_ceiling_is_the_baseline_size_for_an_over_limit_module(self):
        """An over-limit module is frozen where it stands (ADR-0008)."""
        assert module_line_ceiling(6953, 600) == 6953

    def test_ceiling_is_the_configured_limit_for_a_module_within_it(self):
        """Nothing licenses growth up to the limit in a smaller module."""
        assert module_line_ceiling(420, 600) == 600
        assert module_line_ceiling(600, 600) == 600

    def test_ceiling_is_the_configured_limit_for_a_module_absent_from_baseline(self):
        """A module added by this change is governed from its first commit."""
        assert module_line_ceiling(None, 600) == 600

    def test_module_line_counts_covers_every_module_not_only_violations(self, tmp_path):
        """Ceilings are derived from the tree, so compliant modules are measured too."""
        _write(tmp_path / "src" / "theforge" / "small.py", "\n" * 10)
        _write(tmp_path / "src" / "theforge" / "big.py", "\n" * 501)
        counts = module_line_counts(_make_config(), tmp_path)
        assert counts["src/theforge/small.py"] == 10
        assert counts["src/theforge/big.py"] == 501


class TestFailClosedWithoutBaseline:
    def test_module_findings_are_promoted_to_blocking(self):
        """No baseline tree means no derived ceiling, so the limit is the ceiling."""
        scan = [
            ConventionViolation("max_module_lines", "src/a.py", "a has 900 lines", blocking=False),
            ConventionViolation(
                "max_test_file_lines", "tests/test_a.py", "big test", blocking=False
            ),
        ]
        promoted = fail_closed_module_violations(scan)
        assert [v.blocking for v in promoted] == [True, False]

    def test_the_scanned_violations_are_left_untouched(self):
        """The advisory view of the same scan must keep its configured-limit reading."""
        scan = [
            ConventionViolation("max_module_lines", "src/a.py", "a has 900 lines", blocking=False)
        ]
        fail_closed_module_violations(scan)
        assert scan[0].blocking is False
