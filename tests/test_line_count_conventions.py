"""Tests for src/theforge/line_count_conventions.py."""

from __future__ import annotations

from pathlib import Path

from theforge.config.types import HardConventionsConfig
from theforge.line_count_conventions import (
    check_line_counts,
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


class TestModuleLineCounts:
    def test_module_line_counts_covers_every_module_not_only_violations(self, tmp_path):
        """The scan measures the whole tree, so compliant modules are counted too."""
        _write(tmp_path / "src" / "theforge" / "small.py", "\n" * 10)
        _write(tmp_path / "src" / "theforge" / "big.py", "\n" * 501)
        counts = module_line_counts(_make_config(), tmp_path)
        assert counts["src/theforge/small.py"] == 10
        assert counts["src/theforge/big.py"] == 501
