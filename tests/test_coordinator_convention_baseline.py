"""Tests for src/theforge/coordinator/convention_baseline.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

from theforge.coordinator.convention_baseline import resolve_convention_baseline_ref


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(cwd: Path, name: str) -> str:
    (cwd / name).write_text(f"{name}\n", encoding="utf-8")
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", name)
    return _git(cwd, "rev-parse", "HEAD")


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")


def _clone(origin: Path, work: Path) -> None:
    subprocess.run(["git", "clone", str(origin), str(work)], capture_output=True, check=True)
    _git(work, "config", "user.name", "Test User")
    _git(work, "config", "user.email", "test@example.com")


def test_baseline_is_the_merge_base_with_the_local_base_branch(tmp_path: Path) -> None:
    """With no remote in play the resolution is the plain merge-base."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _commit(repo, "base.txt")
    _git(repo, "checkout", "-b", "feature")
    _commit(repo, "feature.txt")

    assert resolve_convention_baseline_ref(repo, "main") == base_sha


def test_missing_base_branch_fails_closed(tmp_path: Path) -> None:
    """An unresolvable base branch yields no baseline, so the limit is the ceiling."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "base.txt")

    assert resolve_convention_baseline_ref(repo, "does-not-exist") is None


def test_a_stale_local_base_ref_does_not_move_the_branch_point_backwards(
    tmp_path: Path,
) -> None:
    """The workspace's real branch point wins over a local ref nobody advanced.

    Charging this story for every module another story grew since the local ref
    last moved is a false refusal, and the ratchet's ceilings come straight from
    whichever commit is picked here.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    _commit(origin, "base.txt")

    work = tmp_path / "work"
    _clone(origin, work)

    # Other stories land on the remote; the local `main` is never fast-forwarded.
    _commit(origin, "other_story.txt")
    _git(work, "fetch", "origin")
    real_branch_point = _git(work, "rev-parse", "origin/main")

    # The workspace is cut from the commit that actually exists on the remote.
    _git(work, "checkout", "-b", "feature", "origin/main")
    _commit(work, "feature.txt")

    assert _git(work, "rev-parse", "main") != real_branch_point
    assert resolve_convention_baseline_ref(work, "main") == real_branch_point


def test_a_current_local_base_ref_is_used_as_is(tmp_path: Path) -> None:
    """When local and remote agree the substitution is a no-op."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    base_sha = _commit(origin, "base.txt")

    work = tmp_path / "work"
    _clone(origin, work)
    _git(work, "checkout", "-b", "feature")
    _commit(work, "feature.txt")

    assert resolve_convention_baseline_ref(work, "main") == base_sha


def test_a_local_base_ref_ahead_of_the_remote_is_kept(tmp_path: Path) -> None:
    """Only a *behind* local ref defers; local work ahead of origin still counts."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    _commit(origin, "base.txt")

    work = tmp_path / "work"
    _clone(origin, work)
    local_only = _commit(work, "landed_locally.txt")
    _git(work, "checkout", "-b", "feature")
    _commit(work, "feature.txt")

    assert resolve_convention_baseline_ref(work, "main") == local_only


def test_a_base_branch_that_exists_only_on_the_remote_still_resolves(
    tmp_path: Path,
) -> None:
    """A workspace with no local base branch has a branch point all the same."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    base_sha = _commit(origin, "base.txt")

    work = tmp_path / "work"
    _clone(origin, work)
    _git(work, "checkout", "-b", "feature")
    _commit(work, "feature.txt")
    _git(work, "branch", "-D", "main")

    assert resolve_convention_baseline_ref(work, "main") == base_sha
