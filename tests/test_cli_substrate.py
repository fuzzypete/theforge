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


def test_detect_mismatch_warns_on_editable_version_divergence(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "theforge"\nversion = "0.11.0.dev"\n'
    )
    sub = _sub(
        version="0.10.0rc12",
        editable=True,
        source_root="/some/other/theforge",
    )
    warning = detect_mismatch(cwd=tmp_path, sub=sub)
    assert warning is not None
    assert "0.10.0rc12" in warning
    assert "0.11.0.dev" in warning
    assert "--force" in warning
    # Remediation must not nudge the operator toward editable-main, which
    # contradicts the dogfood model.
    assert "pip install -e" not in warning


def test_detect_mismatch_silent_for_released_substrate_on_newer_checkout(
    tmp_path: Path,
) -> None:
    """Documented dogfood happy path: released RC orchestrating a newer
    checkout. A non-editable substrate cannot import the patient's source, so
    a version-string difference is not incoherence and must not warn.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "theforge"\nversion = "0.10.0.dev0"\n'
    )
    sub = _sub(version="0.10.0rc14", editable=False)
    assert detect_mismatch(cwd=tmp_path, sub=sub) is None


def test_detect_mismatch_returns_none_when_no_pyproject(tmp_path: Path) -> None:
    assert detect_mismatch(cwd=tmp_path, sub=_sub()) is None


def test_detect_substrate_returns_real_runtime_metadata() -> None:
    sub = detect_substrate()
    assert sub.version  # importlib.metadata or fallback
    assert sub.package_file  # theforge.__file__ resolved
    # binary may resolve to sys.executable when forge isn't on PATH; either way
    # the field is non-empty.
    assert sub.binary


def test_resolve_running_binary_prefers_argv0_over_path(tmp_path: Path, monkeypatch) -> None:
    """Operator workflow: dogfood test ladder runs `.forge/rc-envs/<tag>/bin/forge`
    while PATH still has a different shell-default forge. The provenance line
    must name the path-qualified binary actually executing, not whatever forge
    PATH resolves to first.
    """
    import sys as _sys

    from theforge.cli import substrate as _sub

    # Path-qualified binary the operator actually invoked (mimics the test
    # ladder's `.forge/rc-envs/v.../bin/forge`).
    isolated_dir = tmp_path / "rc-envs" / "v0.10.0rc99" / "bin"
    isolated_dir.mkdir(parents=True)
    isolated = isolated_dir / "forge"
    isolated.write_text("#!/bin/sh\necho isolated\n")
    isolated.chmod(0o755)

    # A different forge sitting on PATH (mimics the operator's shell-default).
    path_dir = tmp_path / "default_path"
    path_dir.mkdir()
    default_forge = path_dir / "forge"
    default_forge.write_text("#!/bin/sh\necho default\n")
    default_forge.chmod(0o755)

    monkeypatch.setattr(_sys, "argv", [str(isolated), "sprint", "--issues", "1"])
    monkeypatch.setenv("PATH", str(path_dir))

    binary = _sub._resolve_running_binary()

    assert binary == str(isolated.resolve())
    assert binary != str(default_forge)


def test_resolve_running_binary_falls_back_to_path_when_argv0_unusable(
    tmp_path: Path, monkeypatch
) -> None:
    import sys as _sys

    from theforge.cli import substrate as _sub

    path_dir = tmp_path / "p"
    path_dir.mkdir()
    forge_bin = path_dir / "forge"
    forge_bin.write_text("#!/bin/sh\n")
    forge_bin.chmod(0o755)

    monkeypatch.setattr(_sys, "argv", [""])
    monkeypatch.setenv("PATH", str(path_dir))

    assert _sub._resolve_running_binary() == str(forge_bin)
