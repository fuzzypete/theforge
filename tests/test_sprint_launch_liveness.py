"""Seam tests for issue #2079: a sprint must not drop its own running stories.

The incident: a mid-run re-exec could not resolve which of its own agent groups
were still alive, read the empty result as "nothing is running", and classified
two of its own executing stories as foreign ``active-worktree-collision`` drops.
Downstream, each symptom followed from that one misclassification — the stories
were recorded at ``cost_usd: 0.0`` with no evidence, the operator was advised to
delete the worktrees holding the only copy of five unmerged commits, and the
inherited agent group survived the sprint to be reaped hours later.

These tests pin each link in that chain at its own seam: liveness resolution
(unresolved ≠ not-live), launch-guard classification, the audit record for a drop
that abandoned work, RCA remediation gating, and inherited-group cleanup.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint import run_sprint
from theforge.sprint.dag import StoryTriage
from theforge.sprint.dropped_work import describe_worktree_work, inspect_worktree_work
from theforge.sprint.launch_guard import (
    REASON_ACTIVE_WORKTREE,
    REASON_IN_FLIGHT_UNRESOLVED,
    acquire_launch_story_locks,
)
from theforge.sprint.live_stories import resolve_liveness, unresolved_liveness

_DEAD_PGID = 999999


# ── helpers ──────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_spec_file(tmp_path: Path, name: str, slug: str) -> Path:
    spec = tmp_path / f"{slug}.md"
    spec.write_text(
        f"---\nname: {name}\nslug: {slug}\n---\n# {name}\nDo the thing.",
        encoding="utf-8",
    )
    return spec


def _make_manifest(tmp_path: Path, specs: list[str], budget: float = 10.0) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump({"name": "Test Sprint", "budget_usd": budget, "specs": specs}),
        encoding="utf-8",
    )
    return manifest_path


def _make_coordinator_result(cost: float = 1.0) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    mock_preflight = MagicMock()
    mock_preflight.cost_usd = cost
    state.preflight_result = mock_preflight
    return CoordinatorResult(
        success=True,
        phase=Phase.DONE,
        state=state,
        message="Done.",
        landing_status="landed",
    )


def _triage_full(spec_path, config, project_root, *, task=None, **_progress):
    return StoryTriage(
        story_path=spec_path,
        action="full",
        reason="x",
        worktree_path=None,
        slug=Path(spec_path).stem,
    )


def _write_agent_sidecar(
    project_root: Path,
    *,
    owner_pid: int,
    pgid: int,
    sandbox_dir: Path | None,
) -> Path:
    agents_dir = project_root / ".forge" / "runs" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{owner_pid}-{pgid}.json"
    path.write_text(
        json.dumps(
            {
                "owner_pid": owner_pid,
                "pgid": pgid,
                "run_id": "run-prev",
                "sandbox_dir": str(sandbox_dir) if sandbox_dir else None,
            }
        ),
        encoding="utf-8",
    )
    return path


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _worktree_with_commit(root: Path, slug: str, *, base_branch: str = "main") -> Path:
    """A story worktree whose branch carries one commit ahead of the base."""
    worktree = root / slug
    worktree.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", f"--initial-branch={base_branch}"], worktree)
    _git(["config", "user.email", "test@example.com"], worktree)
    _git(["config", "user.name", "Test"], worktree)
    (worktree / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], worktree)
    _git(["commit", "-qm", "base"], worktree)
    _git(["checkout", "-qb", f"forge/{slug}"], worktree)
    (worktree / "work.py").write_text("# the only copy of the work\n", encoding="utf-8")
    _git(["add", "-A"], worktree)
    _git(["commit", "-qm", "fix(scope): the work this story was dispatched to write"], worktree)
    return worktree


# ── liveness resolution: unresolved is not "not live" ────────────────


def test_unreadable_sidecar_leaves_liveness_unresolved(tmp_path: Path) -> None:
    """A sidecar that cannot be parsed names no slug — so it taints them all.

    This is the exact input the incident could not recover: the empty
    ``live_slugs`` that followed is indistinguishable from "nothing is running"
    unless the failure itself is reported.
    """
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-2048").mkdir(parents=True)
    (worktrees / "issue-2060").mkdir(parents=True)
    agents_dir = tmp_path / ".forge" / "runs" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")

    resolution = resolve_liveness(
        ["issue-2048", "issue-2060"],
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
    )

    assert resolution.live_slugs == frozenset()
    assert resolution.unresolved_slugs == {"issue-2048", "issue-2060"}
    assert resolution.resolved is False
    assert resolution.failures


def test_unresolved_scope_excludes_slugs_with_no_worktree(tmp_path: Path) -> None:
    """A slug with no worktree has nothing that could be live.

    Fail-closed must not mean fail-broad: a story with no worktree keeps its full
    intake/preflight path even when the sidecar scan is broken.
    """
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-2048").mkdir(parents=True)
    agents_dir = tmp_path / ".forge" / "runs" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "broken.json").write_text("[]", encoding="utf-8")

    resolution = resolve_liveness(
        ["issue-2048", "issue-2060"],
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
    )

    assert resolution.unresolved_slugs == {"issue-2048"}


def test_clean_scan_resolves_confirmed_live_and_nothing_else(tmp_path: Path) -> None:
    """A readable scan still answers precisely: live is live, absent is absent."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-2048").mkdir(parents=True)
    (worktrees / "issue-2060").mkdir(parents=True)
    _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid(),
        pgid=os.getpgrp(),
        sandbox_dir=worktrees / "issue-2048",
    )

    resolution = resolve_liveness(
        ["issue-2048", "issue-2060"],
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
    )

    assert resolution.live_slugs == {"issue-2048"}
    assert resolution.unresolved_slugs == frozenset()
    assert resolution.resolved is True


def test_failed_liveness_probe_is_unresolved_not_dead(tmp_path: Path) -> None:
    """A probe that raises says nothing about the group — least of all that it exited."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-2048").mkdir(parents=True)
    _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid(),
        pgid=4242,
        sandbox_dir=worktrees / "issue-2048",
    )

    def _boom(_pgid: int) -> bool:
        raise OSError("cannot signal")

    resolution = resolve_liveness(
        ["issue-2048"],
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        is_group_alive=_boom,
    )

    assert resolution.unresolved_slugs == {"issue-2048"}
    assert resolution.live_slugs == frozenset()


def test_unresolved_liveness_factory_taints_every_slug() -> None:
    """A lookup that could not run at all resolves nothing, for anyone."""
    resolution = unresolved_liveness(["a", "b"], reason="config unavailable")

    assert resolution.deferred_slugs == {"a", "b"}
    assert resolution.live_slugs == frozenset()


def test_cli_liveness_seam_fails_closed(tmp_path: Path) -> None:
    """The CLI seam degrades to *unresolved*, never to an empty live set."""
    from theforge.cli.sprint import _resolve_story_liveness

    broken_config = MagicMock()
    broken_config.project_root = tmp_path
    type(broken_config).workspace = property(
        lambda _self: (_ for _ in ()).throw(RuntimeError("no workspace"))
    )

    resolution = _resolve_story_liveness(broken_config, ["issue-2048"])

    assert resolution.unresolved_slugs == {"issue-2048"}
    assert resolution.live_slugs == frozenset()


# ── launch-guard classification ──────────────────────────────────────


def test_unresolved_liveness_is_never_an_active_worktree_collision(tmp_path: Path) -> None:
    """The incident's classification, at its seam.

    An unresolved story's worktree is active and has no prior-generation record —
    the combination that fell through to ``REASON_ACTIVE_WORKTREE``. It must come
    back scheduled and lock-holding, never dropped.
    """
    config = _make_config(tmp_path)

    with (
        patch(
            "theforge.sprint.launch_guard.check_active_worktrees",
            return_value=["issue-2048"],
        ) as mock_active,
        patch("theforge.sprint.launch_guard.check_escalated_worktrees", return_value=[]),
    ):
        locked_fds, exit_code, dropped = acquire_launch_story_locks(
            slugs=["issue-2048", "issue-2060"],
            config=config,
            resume=False,
            allow_drop=True,
            live_slugs=set(),
            unresolved_slugs={"issue-2048"},
        )

    try:
        assert exit_code is None
        assert "issue-2048" not in dropped
        assert REASON_ACTIVE_WORKTREE not in dropped.values()
        # Never even offered to the collision classifier.
        assert "issue-2048" not in mock_active.call_args.args[0]
        assert len(locked_fds) == 2
    finally:
        from theforge.sprint.lock import release_story_locks

        release_story_locks(locked_fds)


def test_unresolved_liveness_survives_a_prior_generation_record(tmp_path: Path) -> None:
    """Deferral outranks reconciliation: a possibly-live story is not prior state.

    The prior-generation classifications (#1838) only make sense for a story that
    is *not* running now; an unresolved one may be, and re-consuming its outcome
    while an agent writes to the worktree is the same mistake in another branch.
    """
    config = _make_config(tmp_path)

    with (
        patch(
            "theforge.sprint.launch_guard.check_active_worktrees",
            return_value=["issue-2048"],
        ) as mock_active,
        patch("theforge.sprint.launch_guard.check_escalated_worktrees", return_value=[]),
    ):
        locked_fds, exit_code, dropped = acquire_launch_story_locks(
            slugs=["issue-2048"],
            config=config,
            resume=False,
            allow_drop=True,
            prior_outcomes={"issue-2048": "FAILED"},
            unresolved_slugs={"issue-2048"},
        )
    assert "issue-2048" not in mock_active.call_args.args[0]

    try:
        assert exit_code is None
        assert dropped == {}
        assert len(locked_fds) == 1
    finally:
        from theforge.sprint.lock import release_story_locks

        release_story_locks(locked_fds)


def test_unresolved_liveness_is_announced_distinctly(tmp_path: Path, capsys) -> None:
    """The operator is told the story was deferred *because nothing could be resolved*."""
    config = _make_config(tmp_path)

    with (
        patch("theforge.sprint.launch_guard.check_active_worktrees", return_value=[]),
        patch("theforge.sprint.launch_guard.check_escalated_worktrees", return_value=[]),
    ):
        locked_fds, _exit_code, _dropped = acquire_launch_story_locks(
            slugs=["issue-2048"],
            config=config,
            resume=False,
            allow_drop=True,
            unresolved_slugs={"issue-2048"},
        )
    from theforge.sprint.lock import release_story_locks

    release_story_locks(locked_fds)

    err = capsys.readouterr().err
    assert "IN-FLIGHT issue-2048" in err
    assert "could not be resolved" in err


# ── work left behind by a drop ───────────────────────────────────────


def test_worktree_work_reports_commits_ahead_of_base(tmp_path: Path) -> None:
    worktree = _worktree_with_commit(tmp_path, "issue-2048")

    work = inspect_worktree_work(
        "issue-2048",
        project_root=tmp_path,
        path_pattern="{slug}",
        base_branch="main",
        branch_pattern="forge/{slug}",
    )

    assert work.path == str(worktree.resolve())
    assert work.commits_ahead == 1
    assert work.has_work is True
    assert "1 unmerged commit(s)" in (describe_worktree_work(work) or "")


def test_unreadable_worktree_state_is_treated_as_holding_work(tmp_path: Path) -> None:
    """Fail closed: a directory git cannot answer for is not a directory known empty."""
    (tmp_path / "issue-2048").mkdir()  # present, but not a git repo

    work = inspect_worktree_work(
        "issue-2048",
        project_root=tmp_path,
        path_pattern="{slug}",
        base_branch="main",
    )

    assert work.determined is False
    assert work.may_have_work is True
    assert describe_worktree_work(work)


def test_absent_worktree_is_determined_empty(tmp_path: Path) -> None:
    """A story that never got a worktree is a genuinely free drop."""
    work = inspect_worktree_work(
        "issue-2048",
        project_root=tmp_path,
        path_pattern="{slug}",
        base_branch="main",
    )

    assert work.determined is True
    assert work.may_have_work is False
    assert describe_worktree_work(work) is None


# ── audit record for a drop that abandoned work ──────────────────────


def test_dropped_story_with_commits_is_unmeasured_and_evidenced(tmp_path: Path) -> None:
    """A drop that abandoned committed work is never recorded as free and silent.

    The run that produced commits is precisely the run an operator needs evidence
    for; ``cost_usd: 0.0`` plus no detail is the record that hid it.
    """
    _make_spec_file(tmp_path, "Issue 2048", "issue-2048")
    _make_spec_file(tmp_path, "Issue 2060", "issue-2060")
    manifest_path = _make_manifest(tmp_path, ["issue-2048.md", "issue-2060.md"])
    config = _make_config(tmp_path)
    _worktree_with_commit(tmp_path, "issue-2048")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        result = run_sprint(
            config,
            manifest_path,
            run_id="run-2079",
            dropped_slugs={"issue-2048": REASON_ACTIVE_WORKTREE},
        )

    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    story = {s["slug"]: s for s in summary["stories"]}["issue-2048"]

    assert story["outcome"] == "DROPPED"
    # Not free: the spend happened, it just cannot be recovered here.
    assert story["cost_usd"] is None
    assert story["unmerged_commits"] == 1
    assert story["unmerged_work_determined"] is True
    assert story["branch"] == "forge/issue-2048"
    # The recorded detail names the abandoned work, not an unrelated fragment.
    assert "unmerged commit" in story["error"]
    assert REASON_ACTIVE_WORKTREE in story["error"]
    # The sprint total is a lower bound, and says so.
    assert result.cost_complete is False
    assert "dropped-with-work:issue-2048" in result.unmeasured_spend_sources

    # The same evidence reaches the audit trail, which is where an operator
    # reconstructing the run actually looks.
    audit = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    spec = {s["path"]: s for s in audit["specs"]}["issue-2048.md"]
    assert spec["outcome"] == "DROPPED"
    assert spec["cost_usd"] is None
    assert spec["unmerged_commits"] == 1
    assert "unmerged commit" in spec["error"]
    assert audit["sprint"]["total_cost_usd"] is None


def test_dropped_story_without_a_worktree_stays_a_free_drop(tmp_path: Path) -> None:
    """No worktree, no work: the ordinary drop record is unchanged."""
    _make_spec_file(tmp_path, "Issue 2048", "issue-2048")
    _make_spec_file(tmp_path, "Issue 2060", "issue-2060")
    manifest_path = _make_manifest(tmp_path, ["issue-2048.md", "issue-2060.md"])
    config = _make_config(tmp_path)

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        result = run_sprint(
            config,
            manifest_path,
            run_id="run-2079",
            dropped_slugs={"issue-2048": REASON_ACTIVE_WORKTREE},
        )

    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    story = {s["slug"]: s for s in summary["stories"]}["issue-2048"]

    assert story["outcome"] == "DROPPED"
    assert story["cost_usd"] == 0.0
    assert story["error"] == REASON_ACTIVE_WORKTREE
    assert result.cost_complete is True


def test_dropped_story_inherited_group_is_settled_by_this_sprint(tmp_path: Path) -> None:
    """No agent record belonging to the sprint outlives it.

    The observed orphan (``pgid=22830``, reaped four hours later by an unrelated
    ``forge status``) existed because a dropped story's inherited group was never
    handed to the wait/reclaim path. A drop settles it now.
    """
    _make_spec_file(tmp_path, "Issue 2048", "issue-2048")
    _make_spec_file(tmp_path, "Issue 2060", "issue-2060")
    manifest_path = _make_manifest(tmp_path, ["issue-2048.md", "issue-2060.md"])
    config = _make_config(tmp_path)
    (tmp_path / "issue-2048").mkdir()
    sidecar = _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid(),
        pgid=_DEAD_PGID,
        sandbox_dir=tmp_path / "issue-2048",
    )

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint(
            config,
            manifest_path,
            run_id="run-2079",
            dropped_slugs={"issue-2048": REASON_ACTIVE_WORKTREE},
        )

    assert not sidecar.exists()


# ── the whole chain: an unresolved re-exec ───────────────────────────


def test_reexec_with_unresolved_liveness_defers_and_resumes(tmp_path: Path) -> None:
    """End to end on the incident's inputs, with liveness unresolvable.

    The story is not dropped, not reported free, and reaches a terminal outcome in
    this run — and the baseline gate, whose precondition ("no dev work started")
    cannot be established, is skipped with that stated as its evidence.
    """
    _make_spec_file(tmp_path, "Issue 2048", "issue-2048")
    _make_spec_file(tmp_path, "Issue 2060", "issue-2060")
    manifest_path = _make_manifest(tmp_path, ["issue-2048.md", "issue-2060.md"])
    config = _make_config(tmp_path)
    (tmp_path / "issue-2048").mkdir()

    with (
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch(
            "theforge.sprint.runner.run_task", return_value=_make_coordinator_result()
        ) as mock_run_task,
    ):
        result = run_sprint(
            config,
            manifest_path,
            reexec=True,
            unresolved_live_slugs={"issue-2048"},
        )

    mock_gate.assert_not_called()
    dispatched = sorted(c.args[1].slug for c in mock_run_task.call_args_list)
    assert dispatched == ["issue-2048", "issue-2060"]
    assert result.specs_succeeded == 2

    audit = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    baseline = audit["baseline_check"]
    assert baseline["status"] == "skipped"
    assert "issue-2048" in baseline["skip_evidence"]
    assert "unresolved" in baseline["skip_evidence"]


def test_reexec_state_names_unresolved_deferral_distinctly(tmp_path: Path) -> None:
    """The live status row distinguishes 'waiting on a known agent' from 'unknown'."""
    _make_spec_file(tmp_path, "Issue 2048", "issue-2048")
    manifest_path = _make_manifest(tmp_path, ["issue-2048.md"])
    config = _make_config(tmp_path)
    (tmp_path / "issue-2048").mkdir()

    seen: list[dict] = []
    real_init = None

    from theforge.sprint.state_writer import SprintStateWriter

    real_init = SprintStateWriter.init

    def _capture_init(self, stories):
        seen.extend(stories)
        return real_init(self, stories)

    with (
        patch("theforge.sprint.runner._run_baseline_gate"),
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch.object(SprintStateWriter, "init", _capture_init),
    ):
        run_sprint(
            config,
            manifest_path,
            run_id="run-2079",
            reexec=True,
            unresolved_live_slugs={"issue-2048"},
        )

    row = {s["slug"]: s for s in seen}["issue-2048"]
    assert row["status"] == "waiting"
    assert row["detail"]["in_flight_reason"] == REASON_IN_FLIGHT_UNRESOLVED


# ── RCA remediation gating ───────────────────────────────────────────


def test_launch_collision_remediation_preserves_unmerged_work() -> None:
    """Advice that deletes a worktree is not emitted over unmerged commits."""
    from theforge.sprint.rca import _recommend_actions

    actions = _recommend_actions(
        "launch_collision",
        [],
        {
            "slug": "issue-2048",
            "unmerged_commits": 4,
            "unmerged_work_determined": True,
            "branch": "forge/issue-2048",
            "worktree": "/repo/.forge/worktrees/issue-2048",
        },
    )

    assert "clear the active worktree" not in actions[0]
    assert "preserve the worktree" in actions[0]
    assert "4 unmerged commit(s)" in actions[0]


def test_launch_collision_remediation_is_conservative_when_undetermined() -> None:
    """No evidence either way is not evidence of an empty worktree."""
    from theforge.sprint.rca import _recommend_actions

    actions = _recommend_actions("launch_collision", [], {"slug": "issue-2048"})

    assert "clear the active worktree" not in actions[0]
    assert "inspect the worktree" in actions[0]


def test_launch_collision_remediation_clears_only_a_confirmed_empty_worktree() -> None:
    """With absence of work established, the direct remediation stands."""
    from theforge.sprint.rca import _recommend_actions

    actions = _recommend_actions(
        "launch_collision",
        [],
        {
            "slug": "issue-2048",
            "unmerged_commits": 0,
            "unmerged_work_determined": True,
            "worktree_dirty": False,
        },
    )

    assert "clear the active worktree/lock" in actions[0]
