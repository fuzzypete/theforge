"""Regression tests for issue #835: re-exec must not silently drop stories.

Covers two behaviors:

1. Escalated worktrees are recognized by their state metadata and are not
   treated as active-worktree collisions during re-exec.
2. A genuine active-worktree collision after re-exec does not abort the whole
   sprint — the conflicted story is marked failed, visibly, and the remaining
   stories continue.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.artifacts import ESCALATED_MARKER_PATH
from theforge.sprint.launch_guard import acquire_launch_story_locks
from theforge.sprint.preserved_resume import preserved_escalated_detail


def _mock_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.project_root = tmp_path
    config.workspace.path_pattern = ".forge/worktrees/{slug}"
    config.workspace.base_branch = "main"
    config.workspace.branch_pattern = "forge/{slug}"
    return config


def _make_worktree_with_audit(tmp_path: Path, slug: str, final_phase: str | None = None) -> Path:
    worktree = tmp_path / ".forge" / "worktrees" / slug
    worktree.mkdir(parents=True)
    if final_phase == "ESCALATE":
        marker = worktree / ESCALATED_MARKER_PATH
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("final_phase: ESCALATE\n", encoding="utf-8")
    return worktree


class TestEscalatedWorktreeNotTreatedAsCollision:
    def test_empty_escalated_worktree_is_cleaned_up_and_rescheduled(self, tmp_path: Path) -> None:
        """An escalated-but-empty worktree is removed instead of being preserved."""
        worktree = _make_worktree_with_audit(tmp_path, "issue-829", final_phase="ESCALATE")
        config = _mock_config(tmp_path)

        git_results = [
            MagicMock(returncode=0, stdout="0\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="Deleted branch forge/issue-829\n"),
        ]
        with (
            patch("theforge.sprint.lock.subprocess.run", side_effect=git_results) as mock_git,
            patch("theforge.sprint.lock._current_process_fingerprint", return_value="test-fp"),
        ):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829"],
                config=config,
                resume=False,
                allow_drop=False,
            )

        try:
            assert launch_error is None
            assert dropped == {}
            assert len(locked_fds) == 1
            assert mock_git.call_args_list[0].args[0] == [
                "git",
                "-C",
                str(worktree),
                "rev-list",
                "--count",
                "main..HEAD",
            ]
            assert mock_git.call_args_list[1].args[0] == [
                "git",
                "-C",
                str(worktree),
                "status",
                "--porcelain",
            ]
            assert mock_git.call_args_list[2].args[0][:4] == [
                "git",
                "worktree",
                "remove",
                "--force",
            ]
            assert mock_git.call_args_list[3].args[0] == ["git", "branch", "-D", "forge/issue-829"]
            assert not worktree.exists()
        finally:
            from theforge.sprint.lock import release_story_locks

            release_story_locks(locked_fds)

    def test_escalated_worktree_is_preserved_not_scheduled(self, tmp_path: Path, capsys) -> None:
        """An escalated worktree is excluded from scheduling — no lock, no rerun."""
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase="ESCALATE")
        config = _mock_config(tmp_path)

        git_results = [
            MagicMock(returncode=0, stdout="7\n"),
            MagicMock(returncode=0, stdout=""),
        ]
        with patch("theforge.sprint.lock.subprocess.run", side_effect=git_results):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829"],
                config=config,
                resume=False,
                allow_drop=False,
            )

        assert launch_error is None
        assert dropped == {"issue-829": "preserved-escalated"}
        # No lock is held for the preserved escalated worktree.
        assert locked_fds == []
        assert preserved_escalated_detail(slug="issue-829") in capsys.readouterr().err

    def test_multiple_preserved_escalated_worktrees_each_name_runnable_command(
        self, tmp_path: Path, capsys
    ) -> None:
        """Each preserved slug should render its own runnable review command."""
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase="ESCALATE")
        _make_worktree_with_audit(tmp_path, "issue-830", final_phase="ESCALATE")
        config = _mock_config(tmp_path)

        git_results = [
            MagicMock(returncode=0, stdout="7\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="5\n"),
            MagicMock(returncode=0, stdout=""),
        ]
        with patch("theforge.sprint.lock.subprocess.run", side_effect=git_results):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-830"],
                config=config,
                resume=False,
                allow_drop=False,
            )

        assert launch_error is None
        assert dropped == {
            "issue-829": "preserved-escalated",
            "issue-830": "preserved-escalated",
        }
        assert locked_fds == []
        err = capsys.readouterr().err
        assert preserved_escalated_detail(slug="issue-829") in err
        assert preserved_escalated_detail(slug="issue-830") in err
        assert "PRESERVED issue-829, issue-830" not in err
        assert "resolve with `forge review`" not in err

    def test_reexec_with_escalated_and_pending_story(self, tmp_path: Path) -> None:
        """Scenario from the bug: three stories, middle escalated, third must run.

        Simulates the post-re-exec launch guard path: ``slugs`` is the full
        sprint slug list; the escalated slug is returned as preserved, the
        remaining slugs acquire locks and proceed.
        """
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase="ESCALATE")
        config = _mock_config(tmp_path)

        # issue-267 and issue-268 have no worktree; issue-829 is escalated.
        git_results = [
            MagicMock(returncode=0, stdout="0\n"),
            MagicMock(returncode=0, stdout=" M keep-me.py\n"),
            MagicMock(returncode=0, stdout="0\n"),
            MagicMock(returncode=0, stdout="0\n"),
        ]
        with patch("theforge.sprint.lock.subprocess.run", side_effect=git_results):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267", "issue-268"],
                config=config,
                resume=False,
                allow_drop=True,
            )

        try:
            assert launch_error is None, "re-exec must not abort when only collision is escalated"
            assert dropped == {"issue-829": "preserved-escalated"}
            # Two live stories got locks; the preserved one did not.
            assert len(locked_fds) == 2
        finally:
            from theforge.sprint.lock import release_story_locks

            release_story_locks(locked_fds)


class TestGenuineCollisionDuringReexec:
    def test_reexec_active_worktree_drops_only_conflicted_story(
        self, tmp_path: Path, capsys
    ) -> None:
        """A truly active (non-escalated) worktree is dropped, not an abort."""
        # issue-829 has no audit.yaml -> treated as active (ahead commits).
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase=None)
        config = _mock_config(tmp_path)

        completed = MagicMock(returncode=0, stdout="3\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267", "issue-268"],
                config=config,
                resume=False,
                allow_drop=True,
            )

        try:
            assert launch_error is None, "re-exec must not abort entire sprint"
            assert "issue-829" in dropped
            assert "issue-267" not in dropped
            assert "issue-268" not in dropped
            # Remaining stories acquired locks.
            assert len(locked_fds) == 2

            # Visibility: the drop is loud, not silent.
            captured = capsys.readouterr()
            assert "DROPPED" in captured.err
            assert "issue-829" in captured.err
        finally:
            from theforge.sprint.lock import release_story_locks

            release_story_locks(locked_fds)

    def test_reexec_live_lock_holder_drops_only_conflicted_and_keeps_locks(
        self, tmp_path: Path
    ) -> None:
        """A real process holding a story lock results in only that slug dropped.

        The remaining stories must still hold real locks after the launch guard
        returns — otherwise a concurrent forge invocation could race them.
        """
        import fcntl
        import multiprocessing as _mp_mod
        import os

        from theforge.sprint.lock import acquire_story_locks, release_story_locks

        ctx = _mp_mod.get_context("fork")
        config = _mock_config(tmp_path)

        ready = ctx.Event()
        release = ctx.Event()

        def _hold_lock(root: str, slug: str, r: object, rel: object) -> None:
            fds, _ = acquire_story_locks([slug], Path(root))
            r.set()
            rel.wait(timeout=10)
            release_story_locks(fds)

        proc = ctx.Process(
            target=_hold_lock,
            args=(str(tmp_path), "issue-829", ready, release),
        )
        proc.start()
        try:
            assert ready.wait(timeout=5)

            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267", "issue-268"],
                config=config,
                resume=False,
                allow_drop=True,
            )

            try:
                assert launch_error is None
                assert dropped == {"issue-829": "story-lock-held-by-other-process"}
                # Two non-conflicted stories actually hold their flocks.
                assert len(locked_fds) == 2

                # Try to re-acquire one of the live locks from this process —
                # it must fail because acquire_launch_story_locks still holds it.
                live_lock = tmp_path / ".forge" / "locks" / "issue-267.lock"
                fd = open(live_lock, "a+")
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    fd.close()
            finally:
                release_story_locks(locked_fds)
        finally:
            release.set()
            proc.join(timeout=5)
            if proc.is_alive():
                os.kill(proc.pid, 9)
                proc.join(timeout=2)

    def test_initial_launch_still_aborts_on_active_worktree(self, tmp_path: Path) -> None:
        """Without re-exec (allow_drop=False) the legacy abort behavior is preserved."""
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase=None)
        config = _mock_config(tmp_path)

        completed = MagicMock(returncode=0, stdout="3\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267"],
                config=config,
                resume=False,
                allow_drop=False,
            )

        assert launch_error == 1
        assert locked_fds == []
        assert dropped == {}


class TestReexecPriorOutcomeClassification:
    """Issue #1838: an active worktree after re-exec is classified against the
    prior generation's recorded outcomes — a 3-way split so a completed story is
    reconciled, an unfinished prior story is reported as stranded, and only a
    worktree with no prior record remains a genuine fresh collision."""

    def _drop_for(
        self, tmp_path: Path, capsys, prior_outcomes: dict[str, str]
    ) -> tuple[dict[str, str], str]:
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase=None)
        config = _mock_config(tmp_path)
        completed = MagicMock(returncode=0, stdout="3\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267"],
                config=config,
                resume=False,
                allow_drop=True,
                prior_outcomes=prior_outcomes,
            )
        from theforge.sprint.lock import release_story_locks

        release_story_locks(locked_fds)
        assert launch_error is None
        return dropped, capsys.readouterr().err

    def test_prior_done_worktree_is_reconciled_not_collision(self, tmp_path, capsys) -> None:
        from theforge.sprint.launch_guard import (
            REASON_ACTIVE_WORKTREE,
            REASON_RECONCILE_PRIOR_DONE,
        )

        dropped, err = self._drop_for(tmp_path, capsys, {"issue-829": "DONE"})
        assert dropped["issue-829"] == REASON_RECONCILE_PRIOR_DONE
        assert dropped["issue-829"] != REASON_ACTIVE_WORKTREE
        assert "issue-267" not in dropped
        assert "RECONCILED" in err

    def test_prior_already_done_worktree_is_reconciled(self, tmp_path, capsys) -> None:
        from theforge.sprint.launch_guard import REASON_RECONCILE_PRIOR_DONE

        dropped, _err = self._drop_for(tmp_path, capsys, {"issue-829": "ALREADY_DONE"})
        assert dropped["issue-829"] == REASON_RECONCILE_PRIOR_DONE

    def test_prior_unfinished_worktree_is_stranded(self, tmp_path, capsys) -> None:
        from theforge.sprint.launch_guard import (
            REASON_ACTIVE_WORKTREE,
            REASON_STRANDED_WORKTREE,
        )

        dropped, err = self._drop_for(tmp_path, capsys, {"issue-829": "FAILED"})
        assert dropped["issue-829"] == REASON_STRANDED_WORKTREE
        assert dropped["issue-829"] != REASON_ACTIVE_WORKTREE
        assert "STRANDED" in err

    def test_no_prior_record_is_unchanged_active_collision(self, tmp_path, capsys) -> None:
        from theforge.sprint.launch_guard import REASON_ACTIVE_WORKTREE

        dropped, err = self._drop_for(tmp_path, capsys, {})
        assert dropped["issue-829"] == REASON_ACTIVE_WORKTREE
        assert "DROPPED" in err

    def test_prior_outcomes_none_degrades_to_active_collision(self, tmp_path) -> None:
        """Passing no prior_outcomes (today's default) keeps the collision path."""
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase=None)
        config = _mock_config(tmp_path)
        completed = MagicMock(returncode=0, stdout="3\n")
        from theforge.sprint.launch_guard import REASON_ACTIVE_WORKTREE

        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829"],
                config=config,
                resume=False,
                allow_drop=True,
            )
        from theforge.sprint.lock import release_story_locks

        release_story_locks(locked_fds)
        assert launch_error is None
        assert dropped == {"issue-829": REASON_ACTIVE_WORKTREE}


class TestAuditVisibilityForDroppedStories:
    def test_sprint_audit_records_dropped_and_preserved(self, tmp_path: Path) -> None:
        """_write_sprint_audit emits distinct DROPPED / PRESERVED outcomes with reasons."""
        import datetime

        from theforge.sprint.audit import _write_sprint_audit
        from theforge.sprint.manifest import SprintResult

        manifest = MagicMock()
        manifest.name = "test-sprint"
        manifest.budget_usd = 1.0
        manifest.max_parallel = 1

        sprint_result = SprintResult(
            name="test-sprint",
            specs_total=3,
            specs_succeeded=0,
            specs_failed=1,
            specs_skipped=2,
            total_cost_usd=0.0,
            budget_usd=1.0,
            results=[],
        )

        now = datetime.datetime(2026, 4, 18, tzinfo=datetime.timezone.utc)
        _write_sprint_audit(
            manifest=manifest,
            result=sprint_result,
            canonical_refs=["issue:829", "issue:267", "issue:268"],
            started_at=now,
            finished_at=now,
            duration=1.0,
            project_root=tmp_path,
            slug_map={
                "issue:829": "issue-829",
                "issue:267": "issue-267",
                "issue:268": "issue-268",
            },
            dropped_slugs={
                "issue-829": "preserved-escalated",
                "issue-267": "active-worktree-collision",
            },
        )

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        data = yaml.safe_load(audit_path.read_text())
        specs_by_path = {e["path"]: e for e in data["specs"]}

        assert specs_by_path["Issue #829"]["outcome"] == "PRESERVED"
        assert specs_by_path["Issue #829"]["drop_reason"] == "preserved-escalated"
        assert specs_by_path["Issue #267"]["outcome"] == "DROPPED"
        assert specs_by_path["Issue #267"]["drop_reason"] == "active-worktree-collision"
        # Unmentioned story is still a plain SKIPPED (no drop_reason field).
        assert specs_by_path["Issue #268"]["outcome"] == "SKIPPED"
        assert "drop_reason" not in specs_by_path["Issue #268"]


class TestCliReexecDetection:
    def test_reexec_detected_when_prev_run_id_env_set(self, monkeypatch) -> None:
        from theforge.cli.sprint import _is_reexec

        monkeypatch.setenv("FORGE_PREV_RUN_ID", "run-prev-123")
        assert _is_reexec() is True

    def test_reexec_not_detected_without_env(self, monkeypatch) -> None:
        from theforge.cli.sprint import _is_reexec

        monkeypatch.delenv("FORGE_PREV_RUN_ID", raising=False)
        assert _is_reexec() is False
