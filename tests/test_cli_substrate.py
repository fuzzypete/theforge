"""Substrate provenance + mismatch detection (cli/substrate.py)."""

from __future__ import annotations

from pathlib import Path

from theforge.cli.substrate import (
    Substrate,
    detect_mismatch,
    detect_substrate,
    format_provenance_lines,
)


def _sub(**overrides) -> Substrate:
    base = dict(
        binary="/usr/local/bin/forge",
        package_file="/site-packages/theforge/__init__.py",
        version="0.10.0rc12",
        editable=False,
        source_root=None,
        git_ref=None,
    )
    base.update(overrides)
    return Substrate(**base)


def test_format_installed_substrate_prints_version_and_binary() -> None:
    lines = format_provenance_lines(_sub())
    joined = "\n".join(lines)
    assert "Substrate:" in joined
    assert "installed" in joined
    assert "version=0.10.0rc12" in joined
    assert "/usr/local/bin/forge" in joined


def test_format_editable_substrate_includes_ref_and_source_root() -> None:
    sub = _sub(
        editable=True,
        source_root="/Users/me/src/theforge",
        git_ref="main",
    )
    lines = format_provenance_lines(sub)
    joined = "\n".join(lines)
    assert "editable" in joined
    assert "/Users/me/src/theforge" in joined
    assert "ref=main" in joined


def test_format_editable_with_unknown_ref() -> None:
    sub = _sub(editable=True, source_root="/some/path", git_ref=None)
    lines = format_provenance_lines(sub)
    assert any("ref=unknown" in line for line in lines)


def test_detect_mismatch_returns_none_when_cwd_not_theforge_repo(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "some-other-project"\nversion = "1.0.0"\n'
    )
    assert detect_mismatch(cwd=tmp_path, sub=_sub()) is None


def test_detect_mismatch_returns_none_when_versions_match(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "theforge"\nversion = "0.10.0rc12"\n'
    )
    assert detect_mismatch(cwd=tmp_path, sub=_sub(version="0.10.0rc12")) is None


def test_detect_mismatch_warns_on_version_divergence(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "theforge"\nversion = "0.11.0.dev"\n'
    )
    warning = detect_mismatch(cwd=tmp_path, sub=_sub(version="0.10.0rc12"))
    assert warning is not None
    assert "0.10.0rc12" in warning
    assert "0.11.0.dev" in warning
    assert "--force" in warning


def test_detect_mismatch_returns_none_when_no_pyproject(tmp_path: Path) -> None:
    assert detect_mismatch(cwd=tmp_path, sub=_sub()) is None


def test_detect_substrate_returns_real_runtime_metadata() -> None:
    sub = detect_substrate()
    assert sub.version  # importlib.metadata or fallback
    assert sub.package_file  # theforge.__file__ resolved
    # binary may resolve to sys.executable when forge isn't on PATH; either way
    # the field is non-empty.
    assert sub.binary
