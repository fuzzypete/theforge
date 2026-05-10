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
from theforge.coordinator.review_phase import _ReviewOutcome, _run_review_phase
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.coordinator.workspace_hygiene import (
    check_phase_no_mutation,
    enforce_pre_dev_hygiene,
    enforce_pre_review_hygiene,
    ensure_scratch_dir,
    reconcile_post_review_mutations,
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


# ── enforce_pre_review_hygiene ────────────────────────────────────────


def test_enforce_pre_review_hygiene_passes_on_clean_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ok, diag, audit = enforce_pre_review_hygiene(tmp_path, "run-1", cycle=0)
    assert ok is True
    assert diag is None
    assert audit["modified"] == []
    assert audit["quarantined"] == []


def test_enforce_pre_review_hygiene_quarantines_untracked(tmp_path: Path) -> None:
    """The #1501 motivating case: stray paths inherited at REVIEW entry."""
    _init_repo(tmp_path)
    (tmp_path / "test_script.sh").write_text("oops\n", encoding="utf-8")

    ok, diag, audit = enforce_pre_review_hygiene(tmp_path, "run-xyz", cycle=2)

    assert ok is True
    assert diag is None
    assert not (tmp_path / "test_script.sh").exists()
    quarantine_dir = tmp_path / ".forge" / "quarantine" / "run-xyz" / "review-2"
    assert (quarantine_dir / "test_script.sh").read_text() == "oops\n"
    assert audit["quarantined"] == ["test_script.sh"]
    assert audit["quarantine_dir"] == ".forge/quarantine/run-xyz/review-2"


def test_enforce_pre_review_hygiene_records_but_does_not_quarantine_modified(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("review-time WIP\n", encoding="utf-8")

    ok, diag, audit = enforce_pre_review_hygiene(tmp_path, "run-1", cycle=0)

    assert ok is True
    assert diag is None
    assert audit["modified"] == ["README.md"]
    assert audit["quarantined"] == []
    assert (tmp_path / "README.md").exists()


# ── seam: _run_review_phase quarantines stray paths before reviewers ──


def test_run_review_phase_quarantines_stray_paths_before_reviewers(tmp_path: Path) -> None:
    """The #1501 seam contract: stray untracked paths inherited at REVIEW
    entry are quarantined BEFORE any reviewer observes the tree.

    We patch ``_run_review_pool`` to a side-effect that asserts the worktree
    was clean when the pool was called, proving the hygiene gate ran first.
    """
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    # Simulate a real dev commit so REVIEW has something to review.
    (tmp_path / "src.py").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: implement"], cwd=tmp_path, check=True)
    # Stray untracked file the reviewer must NEVER see.
    (tmp_path / "test_script.sh").write_text("stale scratch\n", encoding="utf-8")

    config = _make_config(tmp_path)
    spec_path = tmp_path.parent / f"{tmp_path.name}-spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    task = TaskStory(name="t", slug="t", story_path=spec_path)
    state = CoordinatorState()
    state.run_id = "abcd1234"

    seen_porcelain: list[set[str]] = []

    def _fake_pool(*args, **kwargs):
        # Record the porcelain set the reviewers would observe.
        seen_porcelain.append(snapshot_porcelain(tmp_path))
        # Return all-empty so phase escalates without needing the full
        # review/synthesis machinery.
        return ([], [], None, [], [])

    with (
        patch("theforge.coordinator.review_phase._run_review_pool", side_effect=_fake_pool),
        patch(
            "theforge.coordinator.review_phase._run_escalate_gate",
            return_value=None,
        ),
    ):
        outcome, _result, _config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "feat/x",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    # Stray file is gone from the worktree.
    assert not (tmp_path / "test_script.sh").exists()
    # Quarantine directory holds the original.
    quarantine_dir = tmp_path / ".forge" / "quarantine" / "abcd1234" / "review-0"
    assert (quarantine_dir / "test_script.sh").read_text() == "stale scratch\n"
    # The reviewer-observed worktree was clean.
    assert seen_porcelain, "review pool was never invoked"
    assert seen_porcelain[0] == set(), f"reviewers observed stray pollution: {seen_porcelain[0]!r}"
    # Audit recorded under PRE_REVIEW.
    pre_review_entry = next(e for e in state.workspace_hygiene_audit if e["phase"] == "PRE_REVIEW")
    assert pre_review_entry["quarantined"] == ["test_script.sh"]
    # Outcome is unrelated escalate (pool returned empty); it is not the
    # hygiene-failure ESCALATE because quarantine succeeded.
    assert outcome in (_ReviewOutcome.ESCALATE, _ReviewOutcome.RETRY_DEV)


def test_run_review_phase_escalates_when_quarantine_fails(tmp_path: Path) -> None:
    """If hygiene gate cannot quarantine, REVIEW escalates BEFORE invoking
    reviewers — operator-visible failure, not a silent stale-tree review."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "src.py").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: implement"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    spec_path = tmp_path.parent / f"{tmp_path.name}-spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    task = TaskStory(name="t", slug="t", story_path=spec_path)
    state = CoordinatorState()
    state.run_id = "abcd1234"

    pool_called = []

    def _fail_hygiene(*args, **kwargs):
        return (
            False,
            "quarantine refused: stray.txt",
            {
                "snapshot": ["?? stray.txt"],
                "modified": [],
                "quarantined": [],
                "quarantine_dir": ".forge/quarantine/abcd1234/review-0",
            },
        )

    with (
        patch(
            "theforge.coordinator.workspace_hygiene.enforce_pre_review_hygiene",
            side_effect=_fail_hygiene,
        ),
        patch(
            "theforge.coordinator.review_phase._run_review_pool",
            side_effect=lambda *a, **kw: pool_called.append(True) or ([], [], None, [], []),
        ),
    ):
        outcome, result, _config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "feat/x",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert outcome == _ReviewOutcome.ESCALATE
    assert result is not None
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    assert "quarantine refused" in (state.error or "")
    # Reviewers were never invoked.
    assert pool_called == []


# ── reconcile_post_review_mutations (issue #1497) ─────────────────────


def test_reconcile_post_review_no_mutation_returns_ok(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    ok, diag, offending, audit = reconcile_post_review_mutations(
        tmp_path, before, "run-1", cycle=0
    )
    assert ok is True
    assert diag is None
    assert offending == []
    assert audit["quarantined"] == []
    assert audit["tracked_changes"] == []


def test_reconcile_post_review_quarantines_untracked_reviewer_scratch(tmp_path: Path) -> None:
    """The #1497 motivating case: reviewer wrote test_script.sh via shell redirect."""
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    (tmp_path / "test_script.sh").write_text("MANAGED_LAUNCHER=...\n", encoding="utf-8")

    ok, diag, offending, audit = reconcile_post_review_mutations(
        tmp_path, before, "run-xyz", cycle=2
    )

    assert ok is True
    assert diag is None
    assert offending == []
    assert not (tmp_path / "test_script.sh").exists()
    quarantine_dir = tmp_path / ".forge" / "quarantine" / "run-xyz" / "review-2-post"
    assert (quarantine_dir / "test_script.sh").read_text() == "MANAGED_LAUNCHER=...\n"
    assert audit["quarantined"] == ["test_script.sh"]
    assert audit["quarantine_dir"] == ".forge/quarantine/run-xyz/review-2-post"
    assert audit["tracked_changes"] == []


def test_reconcile_post_review_reverts_tracked_modification(tmp_path: Path) -> None:
    """Reviewer-side modification to a tracked file is reverted to HEAD content,
    so an otherwise-APPROVE verdict is not invalidated by reviewer side effects."""
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    (tmp_path / "README.md").write_text("seed\nreviewer-edit\n", encoding="utf-8")
    # Untracked scratch is reconciled in the same pass.
    (tmp_path / "test_script.sh").write_text("scratch\n", encoding="utf-8")

    ok, diag, offending, audit = reconcile_post_review_mutations(
        tmp_path, before, "run-1", cycle=0
    )

    assert ok is True
    assert diag is None
    assert offending == []
    # README.md restored to HEAD content; scratch quarantined.
    assert (tmp_path / "README.md").read_text() == "seed\n"
    assert not (tmp_path / "test_script.sh").exists()
    assert audit["tracked_changes"] == ["README.md"]
    assert audit["reverted"] == ["README.md"]
    assert audit["quarantined"] == ["test_script.sh"]


def test_reconcile_post_review_reverts_tracked_deletion(tmp_path: Path) -> None:
    """Reviewer deleting a tracked file is reverted; file restored from HEAD."""
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    (tmp_path / "README.md").unlink()

    ok, _diag, offending, audit = reconcile_post_review_mutations(
        tmp_path, before, "run-1", cycle=0
    )

    assert ok is True
    assert offending == []
    assert (tmp_path / "README.md").read_text() == "seed\n"
    assert audit["reverted"] == ["README.md"]


def test_reconcile_post_review_escalates_when_revert_fails(tmp_path: Path, monkeypatch) -> None:
    """If git revert itself fails, escalation fires so the consensus-capture +
    resume-replay machinery has a real failsafe (#1499) to ride on."""
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    (tmp_path / "README.md").write_text("seed\nreviewer-edit\n", encoding="utf-8")

    from theforge.coordinator import workspace_hygiene as wh

    monkeypatch.setattr(wh, "_revert_tracked_path", lambda *a, **kw: False)

    ok, diag, offending, audit = reconcile_post_review_mutations(
        tmp_path, before, "run-1", cycle=0
    )

    assert ok is False
    assert diag is not None
    assert "README.md" in diag
    assert "could not be reverted" in diag
    assert offending == ["README.md"]
    assert audit["reverted"] == []


def test_reconcile_post_review_escalates_when_quarantine_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """If quarantine_paths fails for any untracked path, the review escalates
    rather than silently treating the dirty tree as valid."""
    _init_repo(tmp_path)
    before = snapshot_porcelain(tmp_path)
    (tmp_path / "test_script.sh").write_text("scratch\n", encoding="utf-8")

    from theforge.coordinator import workspace_hygiene as wh

    def _fail_quarantine(workspace_path, paths, quarantine_root):
        return ([], list(paths))

    monkeypatch.setattr(wh, "quarantine_paths", _fail_quarantine)

    ok, diag, offending, audit = reconcile_post_review_mutations(
        tmp_path, before, "run-1", cycle=0
    )

    assert ok is False
    assert diag is not None
    assert "test_script.sh" in diag
    assert offending == ["test_script.sh"]


# ── seam: post-REVIEW reviewer scratch is auto-quarantined (issue #1497) ─


def test_run_review_phase_quarantines_reviewer_scratch_after_review(tmp_path: Path) -> None:
    """Issue #1497: a reviewer writing test_script.sh via shell during REVIEW
    must not escalate an otherwise-APPROVE review."""
    from coord_test_helpers import _make_task  # noqa: PLC0415

    from theforge.review import ReviewResult  # noqa: PLC0415

    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "src.py").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: implement"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    state.workspace_path = tmp_path
    state.branch_name = "feat/x"
    state.run_id = "abcd1234"
    state.adaptive_review_max = 2

    def _approve_pool(*args, **kwargs):
        # Reviewer writes a scratch file mid-call.
        (tmp_path / "test_script.sh").write_text("MANAGED_LAUNCHER=...\n", encoding="utf-8")
        candidate = ReviewResult(
            verdict="APPROVE",
            summary="ok",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={"verdict": "APPROVE"},
        )
        return ([], [], candidate, [candidate], [("reviewer_a", candidate)])

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_approve_pool,
    ):
        outcome, _result, _config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "feat/x",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    # Did NOT escalate on hygiene grounds — the reviewer scratch was quarantined.
    assert state.escalate_kind != "hygiene"
    # The scratch file is gone from the worktree.
    assert not (tmp_path / "test_script.sh").exists()
    # And lives under the post-review quarantine subdir.
    quarantine_dir = tmp_path / ".forge" / "quarantine" / "abcd1234" / "review-0-post"
    assert (quarantine_dir / "test_script.sh").read_text() == "MANAGED_LAUNCHER=...\n"
    # Audit recorded the quarantined path under the REVIEW post-phase entry.
    review_entry = next(e for e in state.workspace_hygiene_audit if e.get("phase") == "REVIEW")
    assert review_entry["ok"] is True
    assert review_entry["quarantined"] == ["test_script.sh"]
    # Outcome reflects the APPROVE consensus, not a hygiene escalation.
    assert outcome != _ReviewOutcome.ESCALATE or state.escalate_kind != "hygiene"


def test_run_review_phase_reverts_reviewer_tracked_mutation(
    tmp_path: Path,
) -> None:
    """Issue #1497: a reviewer modifying a tracked file mid-review must NOT
    escalate an otherwise-APPROVE consensus. The post-review reconciler
    reverts the mutation back to HEAD and the sprint proceeds."""
    from coord_test_helpers import _make_task  # noqa: PLC0415

    from theforge.review import ReviewResult  # noqa: PLC0415

    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "src.py").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: implement"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    state.workspace_path = tmp_path
    state.branch_name = "feat/x"
    state.run_id = "abcd1234"
    state.adaptive_review_max = 2

    def _mutating_pool(*args, **kwargs):
        (tmp_path / "src.py").write_text("ok\nreviewer-edit\n", encoding="utf-8")
        candidate = ReviewResult(
            verdict="APPROVE",
            summary="ok",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={"verdict": "APPROVE"},
        )
        return ([], [], candidate, [candidate], [("reviewer_a", candidate)])

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_mutating_pool,
    ):
        outcome, _result, _config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "feat/x",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert state.escalate_kind != "hygiene"
    # The tracked file was restored to HEAD content.
    assert (tmp_path / "src.py").read_text() == "ok\n"
    review_entry = next(e for e in state.workspace_hygiene_audit if e.get("phase") == "REVIEW")
    assert review_entry["ok"] is True
    assert review_entry["reverted"] == ["src.py"]
    # Outcome did not escalate on hygiene grounds (may still escalate on
    # downstream guards, e.g. zero-commits-ahead — that is unrelated).
    assert outcome != _ReviewOutcome.ESCALATE or state.escalate_kind != "hygiene"
    # Suppress unused-variable warning from outcome capture.
    _ = outcome
