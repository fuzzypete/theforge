"""Focused tests for VALIDATE phase helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from theforge.coordinator.validate_phase import _get_convention_baseline_ref


def test_get_convention_baseline_ref_returns_merge_base(tmp_path: Path) -> None:
    """Helper should resolve the merge-base between HEAD and the base branch."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")

    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-b", "feature")
    _write(tmp_path / "feature.txt", "change\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "feature")

    assert _get_convention_baseline_ref(tmp_path, "main") == base_sha


def test_get_convention_baseline_ref_returns_none_when_base_missing(tmp_path: Path) -> None:
    """Helper should fail closed when the requested base branch is unavailable."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    assert _get_convention_baseline_ref(tmp_path, "does-not-exist") is None


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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
