"""Tests for ``sprint/audit_publish.py``: terminal sprint audits and their publish.

The module owns one responsibility end to end — build the terminal audit and
summary inputs, write the run's artifacts, then commit and push the canonical
per-run audit JSON — and every test here calls it directly, with no sprint
running, no worktree and no agent invoked (#2402).

The publish half pushes the audit commit to the base branch. Because the
sprint's own merges are what usually advance that branch, a non-fast-forward
rejection is the ordinary case for a run that landed stories — it must be
reconciled and retried rather than raised as terminal. Those tests drive the
real git plumbing (a bare "origin" plus two clones) so the reconcile is
exercised end to end, and assert the recorded publish end state.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from coord_test_helpers import _make_config

from theforge.sprint.audit_publish import (
    _STORY_RUN_AUDIT_PUBLISH_STATE_PATH,
    AUDIT_PUBLISH_BRANCH_MISMATCH,
    AUDIT_PUBLISH_CLEAN,
    AUDIT_PUBLISH_LOCAL_ONLY,
    AUDIT_PUBLISH_PUBLISHED,
    AUDIT_PUBLISH_PUSH_REFUSED,
    AUDIT_PUBLISH_RECONCILE_FAILED,
    StoryRunAuditPublishError,
    _commit_story_run_audits,
    publish_story_run_audits,
    write_terminal_sprint_audits,
)
from theforge.sprint.dag import StoryTriage
from theforge.sprint.manifest import ResolvedSprint, SprintResult
from theforge.sprint.runner import SprintExecutionState, SprintRunContext
from theforge.task import TaskStory

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


def _write_summary(repo: Path, run_id: str) -> None:
    summaries_dir = repo / ".forge" / "knowledge" / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    (summaries_dir / f"{run_id}.yaml").write_text(
        yaml.safe_dump({"run_id": run_id}, sort_keys=False),
        encoding="utf-8",
    )


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


def _tree_names(repo: Path) -> str:
    return _git(repo, "ls-tree", "-r", "--name-only", BASE)


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
    _write_summary(clone, "run-a")

    _commit_story_run_audits(clone, BASE, publish=True)

    tree = _tree_names(origin)
    assert "run-a.json" in tree
    assert ".forge/knowledge/summaries/run-a.yaml" in tree
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_PUBLISHED
    assert _git(clone, "status", "--short", "--", ".forge/knowledge/summaries") == ""


def test_publish_reconciles_when_the_base_branch_advanced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    origin_and_clone: tuple[Path, Path],
) -> None:
    """The sprint's own merge landing mid-run must not strand the audit commit."""
    origin, clone = origin_and_clone
    _write_audit(clone, "run-b.json")
    _write_summary(clone, "run-b")
    # The base branch moves after the clone's last fetch — the merge of the PR
    # for the story this sprint just landed.
    _advance_origin(tmp_path, origin, "story-merge")
    push_shas: list[str] = []

    from theforge.coordinator import util as _cu

    real_run_shell = _cu._run_shell

    def tracing_run_shell(cmd: str, cwd: Path, *args: object, **kwargs: object):
        if cmd.startswith("git push origin"):
            push_shas.append(_git(clone, "rev-parse", BASE))
        return real_run_shell(cmd, cwd, *args, **kwargs)

    monkeypatch.setattr(_cu, "_run_shell", tracing_run_shell)

    _commit_story_run_audits(clone, BASE, publish=True)

    tree = _git(origin, "ls-tree", "-r", "--name-only", BASE)
    assert "run-b.json" in tree
    assert ".forge/knowledge/summaries/run-b.yaml" in tree
    # The reconcile rebased onto the mover rather than discarding it.
    assert "story-merge.txt" in tree
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_PUBLISHED
    assert _git(clone, "rev-list", "--count", f"origin/{BASE}..{BASE}") == "0"
    assert len(push_shas) == 2
    assert push_shas[0] != push_shas[1]


def test_publish_refuses_when_a_different_branch_is_checked_out(
    origin_and_clone: tuple[Path, Path],
) -> None:
    _origin, clone = origin_and_clone
    _git(clone, "checkout", "-b", "feature/wrong-branch")
    head_before = _git(clone, "rev-parse", "HEAD")
    base_before = _git(clone, "rev-parse", BASE)
    _write_audit(clone, "run-g.json")

    with pytest.raises(StoryRunAuditPublishError) as excinfo:
        _commit_story_run_audits(clone, BASE, publish=True)

    assert excinfo.value.state == AUDIT_PUBLISH_BRANCH_MISMATCH
    assert BASE in str(excinfo.value)
    assert "feature/wrong-branch" in str(excinfo.value)
    assert _git(clone, "rev-parse", "HEAD") == head_before
    assert _git(clone, "rev-parse", BASE) == base_before
    assert _git(clone, "diff", "--cached", "--name-only") == ""
    state = _read_state(clone)
    assert state["state"] == AUDIT_PUBLISH_BRANCH_MISMATCH
    assert BASE in state["detail"]
    assert "feature/wrong-branch" in state["detail"]


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
    _write_summary(clone, "run-f")

    _commit_story_run_audits(clone, BASE, publish=False)

    tree = _tree_names(origin)
    assert "run-f.json" not in tree
    assert ".forge/knowledge/summaries/run-f.yaml" not in tree
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_LOCAL_ONLY


def test_publish_records_clean_state_when_no_audits_are_pending(
    origin_and_clone: tuple[Path, Path],
) -> None:
    """A stale marker from an earlier run must not be read as this run's outcome."""
    _origin, clone = origin_and_clone

    _commit_story_run_audits(clone, BASE, publish=True)

    assert _read_state(clone)["state"] == AUDIT_PUBLISH_CLEAN


# ── the write half: terminal audit + summary, called directly ─────────────
#
# No sprint runs in any of these. The state a run would hold is constructed
# here and handed to the module, which is the property the move was for: the
# responsibility is exercisable on its own (#2402).


def _make_state(
    tmp_path: Path,
    *,
    base_branch: str = "main",
    auto_push: bool = True,
) -> SprintExecutionState:
    """The execution state a one-story sprint would hold at its terminal write."""
    import dataclasses

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        workspace=dataclasses.replace(
            config.workspace, base_branch=base_branch, auto_push=auto_push
        ),
    )
    task = TaskStory(name="Export service", slug="export-service", github_issue=42)
    resolved = ResolvedSprint(
        name="audit-move",
        budget_usd=10.0,
        stories=[(task, None, "issue:42")],
        max_parallel=1,
    )
    context = SprintRunContext(
        config=config,
        resolved=resolved,
        sprint_id="sprint-abc",
        run_id="run-xyz",
    )
    return SprintExecutionState.for_run(context)


def _make_result() -> SprintResult:
    return SprintResult(
        name="audit-move",
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=1.25,
        budget_usd=10.0,
        results=[],
    )


def test_terminal_write_emits_audit_and_summary_from_the_state(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    started = datetime.datetime(2026, 8, 13, 9, 0, tzinfo=datetime.timezone.utc)
    finished = started + datetime.timedelta(minutes=12)
    state.story_times["export-service"] = (started, finished)
    state.batch_assignments["export-service"] = 1
    state.current_story_entries_by_ref["issue:42"] = {
        "slug": "export-service",
        "outcome": "done",
        "cost_usd": 1.25,
    }
    log_dir = tmp_path / ".forge" / "logs" / "audit-move"

    write_terminal_sprint_audits(
        state,
        result=_make_result(),
        started_at=started,
        finished_at=finished,
        duration=720.0,
        sprint_log_dir=log_dir,
        dropped_slugs={},
        triages={"issue:42": StoryTriage(story_path="issue:42", action="full", reason="new")},
    )

    audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )
    assert audit["sprint"]["name"] == "audit-move"
    assert audit["sprint"]["sprint_id"] == "sprint-abc"
    assert audit["sprint"]["duration_seconds"] == 720.0
    # The ref → slug map and the tasks-by-slug map are built inside the module
    # from the run context, so the story is identifiable in the record without
    # the caller having assembled either.
    assert [spec["slug"] for spec in audit["specs"]] == ["export-service"]

    summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text(encoding="utf-8"))
    assert summary["sprint"]["name"] == "audit-move"
    assert [story["slug"] for story in summary["stories"]] == ["export-service"]
    # Both artifacts describe the same run: one derivation, two writers.
    assert summary["sprint"]["run_id"] == "run-xyz"
    assert summary["sprint"]["sprint_id"] == audit["sprint"]["sprint_id"]


def test_terminal_write_skips_log_dir_artifacts_when_there_is_no_log_dir(
    tmp_path: Path,
) -> None:
    """A run that could not create its log directory still records the audit."""
    state = _make_state(tmp_path)
    now = datetime.datetime.now(datetime.timezone.utc)

    write_terminal_sprint_audits(
        state,
        result=_make_result(),
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=None,
        dropped_slugs={},
        triages={},
    )

    assert (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").exists()
    assert not (tmp_path / ".forge" / "logs").exists()


def test_terminal_write_records_a_dropped_story_reason(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    now = datetime.datetime.now(datetime.timezone.utc)

    write_terminal_sprint_audits(
        state,
        result=_make_result(),
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=None,
        dropped_slugs={"export-service": "collision with sibling"},
        triages={},
    )

    audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )
    spec = audit["specs"][0]
    assert spec["outcome"] == "DROPPED"
    assert spec["drop_reason"] == "collision with sibling"


# ── the publish half, entered as the runner enters it ─────────────────────


def test_publish_entry_point_publishes_for_the_state_it_is_given(
    origin_and_clone: tuple[Path, Path],
) -> None:
    origin, clone = origin_and_clone
    _write_audit(clone, "run-h.json")
    _write_summary(clone, "run-h")
    state = _make_state(clone, base_branch=BASE)

    publish_story_run_audits(state, lands_locally=False)

    tree = _tree_names(origin)
    assert "run-h.json" in tree
    assert ".forge/knowledge/summaries/run-h.yaml" in tree
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_PUBLISHED


def test_publish_entry_point_keeps_the_commit_local_when_pushing_would_publish_merges(
    origin_and_clone: tuple[Path, Path],
) -> None:
    """auto_push off on a run that lands locally: commit, do not push, say so."""
    origin, clone = origin_and_clone
    _write_audit(clone, "run-i.json")
    _write_summary(clone, "run-i")
    state = _make_state(clone, base_branch=BASE, auto_push=False)

    publish_story_run_audits(state, lands_locally=True)

    tree = _tree_names(origin)
    assert "run-i.json" not in tree
    assert ".forge/knowledge/summaries/run-i.yaml" not in tree
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_LOCAL_ONLY


def test_publish_entry_point_reports_the_failure_and_re_raises(
    origin_and_clone: tuple[Path, Path],
) -> None:
    """Reporting is part of the responsibility — and the sprint still exits nonzero."""
    _origin, clone = origin_and_clone
    _git(clone, "checkout", "-b", "feature/wrong-branch")
    _write_audit(clone, "run-j.json")
    state = _make_state(clone, base_branch=BASE)

    with pytest.raises(StoryRunAuditPublishError) as excinfo:
        publish_story_run_audits(state, lands_locally=False)

    assert excinfo.value.state == AUDIT_PUBLISH_BRANCH_MISMATCH
    assert _read_state(clone)["state"] == AUDIT_PUBLISH_BRANCH_MISMATCH


def test_the_module_does_not_import_the_sprint_runner() -> None:
    """Separation is the point of the move — a dependency back would undo it.

    Two stories changing unrelated parts of a sprint run should claim different
    files. An import of ``sprint.runner`` here — including one hidden inside a
    function or behind ``TYPE_CHECKING`` — puts this module back in the runner's
    dependency graph and makes the move a relocation (#2402).
    """
    import ast

    from theforge.sprint import audit_publish

    tree = ast.parse(Path(audit_publish.__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "runner" or module.endswith(".runner"):
                offenders.append(f"line {node.lineno}: from {'.' * node.level}{module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("sprint.runner"):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
    assert not offenders, "sprint/audit_publish.py imports the sprint runner: " + "; ".join(
        offenders
    )
