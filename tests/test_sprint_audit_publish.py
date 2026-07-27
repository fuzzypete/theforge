"""Tests for publishing canonical story run audit records at end of sprint.

The publish step pushes the audit commit to the base branch. Because the
sprint's own merges are what usually advance that branch, a non-fast-forward
rejection is the ordinary case for a run that landed stories — it must be
reconciled and retried rather than raised as terminal. These tests drive the
real git plumbing (a bare "origin" plus two clones) so the reconcile is
exercised end to end, and assert the recorded publish end state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from theforge.sprint.runner import (
    _STORY_RUN_AUDIT_PUBLISH_STATE_PATH,
    AUDIT_PUBLISH_CLEAN,
    AUDIT_PUBLISH_LOCAL_ONLY,
    AUDIT_PUBLISH_PUBLISHED,
    AUDIT_PUBLISH_PUSH_REFUSED,
    AUDIT_PUBLISH_RECONCILE_FAILED,
    StoryRunAuditPublishError,
    _commit_story_run_audits,
)

BASE = "release/v0.13"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "forge@example.com")
    _git(repo, "config", "user.name", "Forge Test")


def _write_audit(repo: Path, name: str) -> None:
    audit_dir = repo / ".forge" / "audits" / "runs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / name).write_text(json.dumps({"run": name}) + "\n", encoding="utf-8")


@pytest.fixture()
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin with ``BASE`` checked out in a clone, audits unignored."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", BASE)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch", BASE)
    _configure(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", BASE)

    clone = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(clone))
    _configure(clone)
    return origin, clone


def _read_state(project_root: Path) -> dict:
    return json.loads(
        (project_root / _STORY_RUN_AUDIT_PUBLISH_STATE_PATH).read_text(encoding="utf-8")
    )


def _advance_origin(tmp_path: Path, origin: Path, message: str) -> None:
    """Land an unrelated commit on origin's base branch, as a merged PR would."""
    other = tmp_path / f"other-{message}"
    _git(tmp_path, "clone", str(origin), str(other))
    _configure(other)
    (other / f"{message}.txt").write_text(message + "\n", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-m", message)
    _git(other, "push", "origin", BASE)


def test_publish_pushes_audits_when_remote_is_unchanged(
    origin_and_clone: tuple[Path, Path],
) -> None:
    origin, clone = origin_and_clone
    _write_audit(clone, "run-a.json")

    _commit_story_run_audits(clone, BASE, publish=True)

    assert "run-a.json" in _git(origin, "ls-tree", "-r", "--name-only", BASE)
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_PUBLISHED


def test_publish_reconciles_when_the_base_branch_advanced(
    tmp_path: Path, origin_and_clone: tuple[Path, Path]
) -> None:
    """The sprint's own merge landing mid-run must not strand the audit commit."""
    origin, clone = origin_and_clone
    _write_audit(clone, "run-b.json")
    # The base branch moves after the clone's last fetch — the merge of the PR
    # for the story this sprint just landed.
    _advance_origin(tmp_path, origin, "story-merge")

    _commit_story_run_audits(clone, BASE, publish=True)

    tree = _git(origin, "ls-tree", "-r", "--name-only", BASE)
    assert "run-b.json" in tree
    # The reconcile rebased onto the mover rather than discarding it.
    assert "story-merge.txt" in tree
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_PUBLISHED
    assert _git(clone, "rev-list", "--count", f"origin/{BASE}..{BASE}") == "0"


def test_publish_raises_with_push_refused_state_when_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch, origin_and_clone: tuple[Path, Path]
) -> None:
    origin, clone = origin_and_clone
    _write_audit(clone, "run-c.json")

    from theforge.coordinator import util as _cu

    real_run_shell = _cu._run_shell

    def fake_run_shell(cmd: str, cwd: Path, *args: object, **kwargs: object):
        if cmd.startswith("git push origin"):
            return False, "! [rejected] (fetch first)"
        return real_run_shell(cmd, cwd, *args, **kwargs)

    monkeypatch.setattr(_cu, "_run_shell", fake_run_shell)

    with pytest.raises(StoryRunAuditPublishError) as excinfo:
        _commit_story_run_audits(clone, BASE, publish=True)

    assert excinfo.value.state == AUDIT_PUBLISH_PUSH_REFUSED
    assert "3 attempts" in str(excinfo.value)
    state = _read_state(clone)
    assert state["state"] == AUDIT_PUBLISH_PUSH_REFUSED
    assert "rejected" in state["detail"]
    # The audit commit is still local — the caller must exit nonzero.
    assert _git(clone, "log", "-1", "--pretty=%s") == "chore(audit): record sprint run audits"


def test_publish_reports_reconcile_failure_distinctly(
    monkeypatch: pytest.MonkeyPatch, origin_and_clone: tuple[Path, Path]
) -> None:
    origin, clone = origin_and_clone
    _write_audit(clone, "run-d.json")

    from theforge.coordinator import util as _cu

    real_run_shell = _cu._run_shell

    def fake_run_shell(cmd: str, cwd: Path, *args: object, **kwargs: object):
        if cmd.startswith("git push origin"):
            return False, "! [rejected] (fetch first)"
        if cmd.startswith("git fetch origin"):
            return False, "fatal: could not read from remote repository"
        return real_run_shell(cmd, cwd, *args, **kwargs)

    monkeypatch.setattr(_cu, "_run_shell", fake_run_shell)

    with pytest.raises(StoryRunAuditPublishError) as excinfo:
        _commit_story_run_audits(clone, BASE, publish=True)

    assert excinfo.value.state == AUDIT_PUBLISH_RECONCILE_FAILED
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_RECONCILE_FAILED


def test_publish_aborts_a_conflicted_rebase_before_raising(
    tmp_path: Path, origin_and_clone: tuple[Path, Path]
) -> None:
    """A conflicting reconcile leaves no half-finished rebase in the checkout."""
    origin, clone = origin_and_clone
    _write_audit(clone, "run-e.json")

    other = tmp_path / "conflicting"
    _git(tmp_path, "clone", str(origin), str(other))
    _configure(other)
    _write_audit(other, "run-e.json")
    (other / ".forge" / "audits" / "runs" / "run-e.json").write_text(
        '{"run": "different"}\n', encoding="utf-8"
    )
    _git(other, "add", "--", ".forge/audits/runs")
    _git(other, "commit", "-m", "conflicting audit")
    _git(other, "push", "origin", BASE)

    with pytest.raises(StoryRunAuditPublishError) as excinfo:
        _commit_story_run_audits(clone, BASE, publish=True)

    assert excinfo.value.state == AUDIT_PUBLISH_RECONCILE_FAILED
    assert not (clone / ".git" / "rebase-merge").exists()
    assert not (clone / ".git" / "rebase-apply").exists()


def test_publish_disabled_records_local_only_state(
    origin_and_clone: tuple[Path, Path],
) -> None:
    origin, clone = origin_and_clone
    _write_audit(clone, "run-f.json")

    _commit_story_run_audits(clone, BASE, publish=False)

    assert "run-f.json" not in _git(origin, "ls-tree", "-r", "--name-only", BASE)
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_LOCAL_ONLY


def test_publish_records_clean_state_when_no_audits_are_pending(
    origin_and_clone: tuple[Path, Path],
) -> None:
    """A stale marker from an earlier run must not be read as this run's outcome."""
    _origin, clone = origin_and_clone

    _commit_story_run_audits(clone, BASE, publish=True)

    assert _read_state(clone)["state"] == AUDIT_PUBLISH_CLEAN
