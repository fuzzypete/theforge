"""Tests for the workspace hygiene gate.

Covers the helpers in workspace_hygiene plus the seam-level wiring that
addresses issue #1179: stray files at repo root must not be silently handed to
the dev agent, and non-DEV phases must not mutate the worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from coord_test_helpers import _make_agent_result, _make_config

from theforge.coordinator.dev_phase import _run_dev_phase
from theforge.coordinator.state import CoordinatorState
from theforge.coordinator.workspace_hygiene import (
    check_phase_no_mutation,
    enforce_pre_dev_hygiene,
    ensure_scratch_dir,
    snapshot_porcelain,
    unexpected_entries,
)
from theforge.task import TaskStory


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / ".gitignore").write_text(".forge/\n", encoding="utf-8")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


# ── snapshot_porcelain ────────────────────────────────────────────────


def test_snapshot_porcelain_clean_repo_returns_empty_set(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert snapshot_porcelain(tmp_path) == set()


def test_snapshot_porcelain_lists_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "test_flock.py").write_text("scratch\n", encoding="utf-8")
    snap = snapshot_porcelain(tmp_path)
    paths = {entry[3:] for entry in snap}
    assert "test_flock.py" in paths


def test_snapshot_porcelain_excludes_gitignored_forge_dir(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "tmp.txt").write_text("scratch\n", encoding="utf-8")
    snap = snapshot_porcelain(tmp_path)
    assert all(".forge" not in entry for entry in snap)


def test_snapshot_porcelain_returns_empty_on_non_repo(tmp_path: Path) -> None:
    assert snapshot_porcelain(tmp_path) == set()


# ── unexpected_entries ────────────────────────────────────────────────


def test_unexpected_entries_returns_only_new_paths() -> None:
    before = {"?? a.py"}
    after = {"?? a.py", "?? b.py", " M c.py"}
    new = unexpected_entries(before, after)
    assert [entry[3:] for entry in new] == ["b.py", "c.py"]


# ── check_phase_no_mutation ───────────────────────────────────────────


def test_check_phase_no_mutation_passes_when_clean(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    ok, diag, offending = check_phase_no_mutation(tmp_path, before, "PLAN")
    assert ok is True
    assert diag is None
    assert offending == []


def test_check_phase_no_mutation_rejects_new_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    (tmp_path / "scratch.txt").write_text("oops\n", encoding="utf-8")
    ok, diag, offending = check_phase_no_mutation(tmp_path, before, "REVIEW")
    assert ok is False
    assert "REVIEW" in diag
    assert "scratch.txt" in diag
    assert offending == ["scratch.txt"]


def test_check_phase_no_mutation_rejects_modified_tracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")
    ok, diag, offending = check_phase_no_mutation(tmp_path, before, "PLAN")
    assert ok is False
    assert "README.md" in diag


# ── ensure_scratch_dir ────────────────────────────────────────────────


def test_ensure_scratch_dir_creates_under_forge_tmp(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    scratch = ensure_scratch_dir(tmp_path, "abcd1234")
    assert scratch == tmp_path / ".forge" / "tmp" / "abcd1234"
    assert scratch.is_dir()


# ── enforce_pre_dev_hygiene ───────────────────────────────────────────


def test_enforce_pre_dev_hygiene_passes_on_clean_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ok, diag, audit = enforce_pre_dev_hygiene(tmp_path, "run-1", iteration=1)
    assert ok is True
    assert diag is None
    assert audit["modified"] == []
    assert audit["quarantined"] == []


def test_enforce_pre_dev_hygiene_quarantines_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "test_flock.py").write_text("scratch\n", encoding="utf-8")
    (tmp_path / "test_flock2.py").write_text("scratch\n", encoding="utf-8")

    ok, diag, audit = enforce_pre_dev_hygiene(tmp_path, "run-xyz", iteration=1)

    assert ok is True
    assert diag is None
    # Files moved out of root.
    assert not (tmp_path / "test_flock.py").exists()
    assert not (tmp_path / "test_flock2.py").exists()
    # Quarantine directory holds originals; audit mentions both.
    quarantine_dir = tmp_path / ".forge" / "quarantine" / "run-xyz" / "iter-1"
    assert (quarantine_dir / "test_flock.py").read_text() == "scratch\n"
    assert (quarantine_dir / "test_flock2.py").read_text() == "scratch\n"
    assert sorted(audit["quarantined"]) == ["test_flock.py", "test_flock2.py"]
    assert audit["quarantine_dir"] == ".forge/quarantine/run-xyz/iter-1"


def test_enforce_pre_dev_hygiene_records_but_does_not_quarantine_modified_tracked(
    tmp_path: Path,
) -> None:
    """Modified tracked files are legitimate worktree-reuse state.

    They are audited (so an operator can see them) but NOT silently moved —
    that would qualify as "forge ate my work". Validate-phase's auto-commit
    handles cleanup after DEV runs.
    """
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("worktree-reuse work in progress\n", encoding="utf-8")

    ok, diag, audit = enforce_pre_dev_hygiene(tmp_path, "run-1", iteration=1)

    assert ok is True
    assert diag is None
    assert audit["modified"] == ["README.md"]
    assert audit["quarantined"] == []
    assert (tmp_path / "README.md").exists()


# ── seam: _run_dev_phase quarantines and proceeds ─────────────────────


def test_run_dev_phase_quarantines_untracked_root_scratch(tmp_path: Path) -> None:
    """The #1179 motivating case: stray test_flock*.py at root before DEV.

    Coordinator must quarantine them and let DEV proceed against a clean tree.
    """
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "test_flock.py").write_text("scratch\n", encoding="utf-8")
    (tmp_path / "test_flock2.py").write_text("scratch\n", encoding="utf-8")

    config = _make_config(tmp_path)
    spec_path = tmp_path.parent / f"{tmp_path.name}-spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    task = TaskStory(name="t", slug="t", story_path=spec_path)
    state = CoordinatorState()
    state.run_id = "abcd1234"

    # Successful dev result so we don't bail out on the zero-commits guard.
    def _run_agent(**kwargs):
        # Simulate dev agent committing real work — keeps zero-commits guard happy.
        (tmp_path / "src.py").write_text("ok\n", encoding="utf-8")
        subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat: implement"], cwd=tmp_path, check=True)
        return _make_agent_result(success=True, dev_handoff={"summary": "done"})

    with (
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=_run_agent),
        patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
    ):
        result = _run_dev_phase(
            state, config, task, "# spec\n", tmp_path, "feat/x", notify=False, logger=None
        )

    assert result is None  # no escalation
    assert not (tmp_path / "test_flock.py").exists()
    assert not (tmp_path / "test_flock2.py").exists()
    quarantine_dir = tmp_path / ".forge" / "quarantine" / "abcd1234" / "iter-0"
    assert (quarantine_dir / "test_flock.py").exists()
    # Hygiene audit recorded.
    assert state.workspace_hygiene_audit
    pre_dev_entry = next(e for e in state.workspace_hygiene_audit if e["phase"] == "PRE_DEV")
    assert sorted(pre_dev_entry["quarantined"]) == ["test_flock.py", "test_flock2.py"]


def test_run_dev_phase_skips_hygiene_gate_on_retry_iterations(tmp_path: Path) -> None:
    """Iterations 2+ are not re-gated — validate-phase auto-commit owns cleanup.

    A worktree dirty mid-loop is a legitimate retry handoff; re-gating would
    break the validate→DEV retry path.
    """
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "src.py").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "iter1"], cwd=tmp_path, check=True)
    # Stray scratch file — would normally be quarantined on iter 1, but iter 2+
    # is intentionally not re-gated.
    (tmp_path / "test_flock.py").write_text("scratch\n", encoding="utf-8")

    config = _make_config(tmp_path)
    spec_path = tmp_path.parent / f"{tmp_path.name}-spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    task = TaskStory(name="t", slug="t", story_path=spec_path)
    state = CoordinatorState(dev_iteration=2)
    state.run_id = "abcd1234"

    with (
        patch(
            "theforge.coordinator.dev_phase.run_agent",
            return_value=_make_agent_result(success=True, dev_handoff={"summary": "done"}),
        ),
        patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
    ):
        result = _run_dev_phase(
            state, config, task, "# spec\n", tmp_path, "feat/x", notify=False, logger=None
        )

    assert result is None  # no escalation, no quarantine
    assert (tmp_path / "test_flock.py").exists()
    assert not any(e.get("phase") == "PRE_DEV" for e in state.workspace_hygiene_audit)
