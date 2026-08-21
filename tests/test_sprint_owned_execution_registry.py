"""Regression tests for #2617: a re-exec must not drop its own in-flight story.

The incident: a sprint landed two of its own stories, pulled the branch those
merges had advanced, and re-exec'd to load the new source. A sibling story was
mid-VALIDATE at that instant — executing in pure Python, with no agent
subprocess to survive ``os.execv`` — so the far side of the exec had nothing to
recognise it by. Its worktree was classified ``active-worktree-collision`` and
the story was dropped: a gate-green, review-approved commit, $3.82 spent,
abandoned by a lifecycle event that said nothing about whether the change was
good.

The fix is an explicit ownership record, written before dispatch and cleared only
after the scheduler settles the story. These tests pin it at each seam it has to
hold at: the registry's identity rules, liveness resolution folding it in, the
launch guard's classification, and the scheduler's write/clear ordering.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sprint_test_helpers import run_sprint_ctx

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
from theforge.sprint.dag import StoryTriage
from theforge.sprint.launch_guard import (
    REASON_ACTIVE_WORKTREE,
    REASON_RECONCILE_PRIOR_DONE,
    acquire_launch_story_locks,
)
from theforge.sprint.live_stories import await_inherited_agents, resolve_liveness
from theforge.sprint.lock import release_story_locks
from theforge.sprint.story_executions import (
    current_process_fingerprint,
    executions_dir,
    register_story_execution,
    scan_story_executions,
    sweep_story_executions,
)

_PATH_PATTERN = ".forge/worktrees/{slug}"


# ── helpers ──────────────────────────────────────────────────────────


def _worktree(project_root: Path, slug: str) -> Path:
    path = project_root / ".forge" / "worktrees" / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_raw_record(project_root: Path, slug: str, payload: object, *, owner_pid: int) -> Path:
    directory = executions_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{owner_pid}-{slug}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _mock_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.project_root = tmp_path
    config.workspace.path_pattern = _PATH_PATTERN
    config.workspace.base_branch = "main"
    config.workspace.branch_pattern = "forge/{slug}"
    return config


def _real_config(tmp_path: Path) -> ForgeConfig:
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


def _requires_process_identity() -> str | None:
    return current_process_fingerprint()


# ── the registry's identity rules ────────────────────────────────────


def test_record_written_by_this_process_is_owned(tmp_path: Path) -> None:
    """The pid survives ``execv`` and so does its start time — that pair is the run."""
    if _requires_process_identity() is None:
        pytest.skip("platform will not describe this process's start time")
    register_story_execution("issue-2593", project_root=tmp_path)

    scan = scan_story_executions(tmp_path)

    assert [r.slug for r in scan.owned] == ["issue-2593"]
    assert scan.unverifiable == ()
    assert scan.failures == ()


def test_record_from_a_dead_run_holding_our_pid_claims_nothing(tmp_path: Path) -> None:
    """A pid is recycled; a start time is not. The fingerprint is what decides."""
    _write_raw_record(
        tmp_path,
        "issue-2593",
        {
            "slug": "issue-2593",
            "owner_pid": os.getpid(),
            "owner_fingerprint": "sysctl:1.000000",
            "worktree": None,
            "run_id": "run-dead",
        },
        owner_pid=os.getpid(),
    )

    scan = scan_story_executions(tmp_path)

    assert scan.owned == ()
    assert scan.unverifiable == ()


def test_record_owned_by_another_process_is_not_this_runs_work(tmp_path: Path) -> None:
    """Someone else's in-flight story is someone else's business."""
    _write_raw_record(
        tmp_path,
        "issue-2593",
        {
            "slug": "issue-2593",
            "owner_pid": os.getpid() + 1,
            "owner_fingerprint": current_process_fingerprint(),
            "worktree": None,
            "run_id": "run-other",
        },
        owner_pid=os.getpid() + 1,
    )

    scan = scan_story_executions(tmp_path)

    assert scan.owned == ()
    assert scan.unverifiable == ()


def test_record_without_a_comparable_identity_is_unverifiable_not_absent(tmp_path: Path) -> None:
    """Neither answer was established, so neither is reported."""
    _write_raw_record(
        tmp_path,
        "issue-2593",
        {
            "slug": "issue-2593",
            "owner_pid": os.getpid(),
            "owner_fingerprint": None,
            "worktree": None,
            "run_id": "run-x",
        },
        owner_pid=os.getpid(),
    )

    scan = scan_story_executions(tmp_path)

    assert scan.owned == ()
    assert [r.slug for r in scan.unverifiable] == ["issue-2593"]


def test_sweep_removes_a_dead_owners_record_but_never_our_own(tmp_path: Path) -> None:
    if _requires_process_identity() is None:
        pytest.skip("platform will not describe this process's start time")
    mine = register_story_execution("issue-2593", project_root=tmp_path)
    theirs = _write_raw_record(
        tmp_path,
        "issue-9999",
        {
            "slug": "issue-9999",
            "owner_pid": 999_999,
            "owner_fingerprint": "sysctl:1.000000",
            "worktree": None,
            "run_id": "run-dead",
        },
        owner_pid=999_999,
    )

    removed = sweep_story_executions(tmp_path)

    assert theirs in removed
    assert mine.exists()


# ── liveness folds ownership in ──────────────────────────────────────


def test_owned_execution_is_live_without_any_agent_sidecar(tmp_path: Path) -> None:
    """The whole point: no subprocess survived the exec, and the story is still ours.

    This is the input the incident could not answer. ``.forge/runs/agents`` is
    empty — the story was executing in-process when ``os.execv`` fired — and the
    only evidence that it is this run's work is the record the scheduler wrote.
    """
    if _requires_process_identity() is None:
        pytest.skip("platform will not describe this process's start time")
    _worktree(tmp_path, "issue-2593")
    _worktree(tmp_path, "issue-2608")
    register_story_execution(
        "issue-2593", project_root=tmp_path, worktree=tmp_path / ".forge/worktrees/issue-2593"
    )

    resolution = resolve_liveness(
        ["issue-2593", "issue-2608"], project_root=tmp_path, path_pattern=_PATH_PATTERN
    )

    assert resolution.live_slugs == {"issue-2593"}
    assert resolution.registered_slugs == {"issue-2593"}
    assert resolution.unresolved_slugs == frozenset()
    assert resolution.resolved is True


def test_stale_and_foreign_records_do_not_make_a_worktree_live(tmp_path: Path) -> None:
    """A record must not be able to shield a genuinely foreign worktree."""
    _worktree(tmp_path, "issue-2593")
    _write_raw_record(
        tmp_path,
        "issue-2593",
        {
            "slug": "issue-2593",
            "owner_pid": os.getpid(),
            "owner_fingerprint": "sysctl:1.000000",
            "worktree": None,
            "run_id": "run-dead",
        },
        owner_pid=os.getpid(),
    )

    resolution = resolve_liveness(
        ["issue-2593"], project_root=tmp_path, path_pattern=_PATH_PATTERN
    )

    assert resolution.live_slugs == frozenset()
    assert resolution.unresolved_slugs == frozenset()


def test_unreadable_execution_record_leaves_existing_worktrees_unresolved(tmp_path: Path) -> None:
    """A registry that cannot be read is not a registry that says "nothing"."""
    _worktree(tmp_path, "issue-2593")
    _write_raw_record(tmp_path, "issue-2593", "{not json", owner_pid=os.getpid())

    resolution = resolve_liveness(
        ["issue-2593", "issue-2608"], project_root=tmp_path, path_pattern=_PATH_PATTERN
    )

    assert resolution.live_slugs == frozenset()
    # Scoped to the worktree that exists — fail-closed must not mean fail-broad.
    assert resolution.unresolved_slugs == {"issue-2593"}
    assert resolution.failures


def test_registry_is_ignored_when_only_agent_groups_are_asked_about(tmp_path: Path) -> None:
    """``include_executions=False`` is the narrower question, and answers it."""
    if _requires_process_identity() is None:
        pytest.skip("platform will not describe this process's start time")
    _worktree(tmp_path, "issue-2593")
    register_story_execution("issue-2593", project_root=tmp_path)

    resolution = resolve_liveness(
        ["issue-2593"],
        project_root=tmp_path,
        path_pattern=_PATH_PATTERN,
        include_executions=False,
    )

    assert resolution.live_slugs == frozenset()
    assert resolution.deferred_slugs == frozenset()


def test_owned_story_with_no_agent_group_reaches_triage_immediately(tmp_path: Path) -> None:
    """The resume path must not wait out its quiesce timeout on its own record.

    ``_run_inherited_story`` waits for inherited *agents*. A story vouched for
    only by an ownership record has none, so the wait has nothing to wait for and
    the story should reach triage at once rather than burning half its worker
    budget.
    """
    if _requires_process_identity() is None:
        pytest.skip("platform will not describe this process's start time")
    _worktree(tmp_path, "issue-2593")
    register_story_execution("issue-2593", project_root=tmp_path)

    started = time.monotonic()
    quiesced = await_inherited_agents(
        "issue-2593",
        project_root=tmp_path,
        path_pattern=_PATH_PATTERN,
        timeout=30.0,
        poll_interval=0.01,
    )

    assert quiesced is True
    assert time.monotonic() - started < 5.0


# ── launch-guard classification ──────────────────────────────────────


def _acquire(tmp_path: Path, **kwargs):
    config = _mock_config(tmp_path)
    completed = MagicMock(returncode=0, stdout="3\n")
    with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
        locked_fds, launch_error, dropped = acquire_launch_story_locks(
            slugs=["issue-2593", "issue-2608"],
            config=config,
            resume=False,
            allow_drop=True,
            **kwargs,
        )
    return locked_fds, launch_error, dropped


def test_truly_unowned_worktree_after_reexec_is_still_a_collision(tmp_path: Path) -> None:
    """The guard still protects a run from an unrelated run's leftovers."""
    _worktree(tmp_path, "issue-2593")

    locked_fds, launch_error, dropped = _acquire(tmp_path)
    release_story_locks(locked_fds)

    assert launch_error is None
    assert dropped["issue-2593"] == REASON_ACTIVE_WORKTREE


def test_this_generations_own_worktree_is_not_dropped_and_keeps_its_lock(
    tmp_path: Path, capsys
) -> None:
    """The incident, at the seam where it happened.

    Same inputs as the unowned case above — an active worktree, no prior
    generation record — except that this run has declared the story its own. The
    story must survive with its launch lock, not be dropped.
    """
    _worktree(tmp_path, "issue-2593")

    locked_fds, launch_error, dropped = _acquire(
        tmp_path,
        live_slugs={"issue-2593"},
        registered_slugs={"issue-2593"},
    )
    lock_count = len(locked_fds)
    lock_file = tmp_path / ".forge" / "locks" / "issue-2593.lock"
    lock_exists = lock_file.exists()
    release_story_locks(locked_fds)

    assert launch_error is None
    assert "issue-2593" not in dropped
    # Both stories kept their launch lock: the in-flight one is scheduled, not
    # dropped, so it must hold the lock that keeps a concurrent forge off it.
    assert lock_count == 2
    assert lock_exists
    err = capsys.readouterr().err
    assert "IN-FLIGHT issue-2593" in err
    # A registry-only story has no process group; the operator must not be told
    # one is running.
    assert "agent process group" not in err


def test_settled_prior_landing_outranks_a_surviving_ownership_record(tmp_path: Path) -> None:
    """A record can outlive the story it describes; a recorded landing cannot.

    The record is cleared only after the scheduler writes the terminal outcome,
    so a crash inside that window leaves one behind for work that already landed.
    Re-dispatching it would pay for finished work twice and bypass #2189's
    reconciliation, so the prior generation's landing wins.
    """
    _worktree(tmp_path, "issue-2593")
    prior = {
        "issue-2593": {
            "outcome": "DONE",
            "landing_status": "landed",
            "landing": {"action": "merge", "merged": True},
        }
    }

    locked_fds, launch_error, dropped = _acquire(
        tmp_path,
        live_slugs={"issue-2593"},
        registered_slugs={"issue-2593"},
        prior_outcomes=prior,
    )
    release_story_locks(locked_fds)

    assert launch_error is None
    assert dropped["issue-2593"] == REASON_RECONCILE_PRIOR_DONE


def test_a_live_agent_group_is_not_superseded_by_a_prior_landing(tmp_path: Path) -> None:
    """Precedence is scoped to records. A running process outranks a past outcome."""
    _worktree(tmp_path, "issue-2593")
    prior = {
        "issue-2593": {
            "outcome": "DONE",
            "landing_status": "landed",
            "landing": {"action": "merge", "merged": True},
        }
    }

    locked_fds, launch_error, dropped = _acquire(
        tmp_path,
        live_slugs={"issue-2593"},
        registered_slugs=set(),
        prior_outcomes=prior,
    )
    release_story_locks(locked_fds)

    assert launch_error is None
    assert "issue-2593" not in dropped


# ── the scheduler's write/clear ordering ─────────────────────────────


def _run_one_story_sprint(tmp_path: Path, *, slugs: list[str], **ctx_kwargs):
    for slug in slugs:
        _make_spec_file(tmp_path, slug, slug)
    manifest_path = _make_manifest(tmp_path, [f"{slug}.md" for slug in slugs])
    return _real_config(tmp_path), manifest_path


def test_dispatch_records_ownership_before_the_worker_runs(tmp_path: Path) -> None:
    """No story reaches the pool unowned — checked from inside the worker."""
    config, manifest_path = _run_one_story_sprint(tmp_path, slugs=["issue-2593"])
    owned_at_worker_time: list[list[str]] = []

    def _observe(*_args, **_kwargs):
        scan = scan_story_executions(tmp_path)
        owned_at_worker_time.append(sorted(r.slug for r in scan.owned))
        return _make_coordinator_result()

    with (
        patch("theforge.sprint.runner._run_baseline_gate", return_value={"passed": True}),
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", side_effect=_observe),
    ):
        result = run_sprint_ctx(config, manifest_path)

    assert result.specs_succeeded == 1
    assert owned_at_worker_time == [["issue-2593"]]
    # And nothing this run declared outlives it.
    assert scan_story_executions(tmp_path).owned == ()


def test_ownership_survives_the_worker_and_is_cleared_only_once_settled(tmp_path: Path) -> None:
    """The clear point is the scheduler's reconciliation, not the worker's return.

    Asserted on the helper directly, because the window it protects — worker
    returned, terminal outcome not yet written — exists for microseconds inside a
    real run and is exactly where the re-exec landed.
    """
    from sprint_test_helpers import make_run_context

    from theforge.sprint.runner import (
        SprintExecutionState,
        _release_settled_story_executions,
        _set_outcome,
    )
    from theforge.sprint.story_state import StoryOutcome

    config, manifest_path = _run_one_story_sprint(tmp_path, slugs=["issue-2593"])
    state = SprintExecutionState.for_run(make_run_context(config, manifest_path))
    register_story_execution("issue-2593", project_root=tmp_path)
    state.owned_story_executions.add("issue-2593")

    # Still dispatched: nothing to clear.
    state.active["issue-2593"] = MagicMock()
    _release_settled_story_executions(state)
    assert state.owned_story_executions == {"issue-2593"}

    # Worker returned, outcome not yet recorded — the window the record exists
    # for. Still nothing to clear.
    del state.active["issue-2593"]
    _release_settled_story_executions(state)
    assert state.owned_story_executions == {"issue-2593"}

    # Terminal outcome on disk: the prior-generation reconciliation can speak for
    # the story now, so the record goes.
    _set_outcome(state, "issue-2593", StoryOutcome.DONE)
    _release_settled_story_executions(state)
    assert state.owned_story_executions == set()
    assert scan_story_executions(tmp_path).owned == ()


def test_batch_members_are_each_owned_before_the_group_is_dispatched(tmp_path: Path) -> None:
    """A shared worker is still several stories, and each is owned in its own right.

    A batch group runs on one future, so a re-exec that hit it would have to
    recognise every member — the group's leader is a dispatch detail, not the
    unit responsibility is recorded in.
    """
    config, manifest_path = _run_one_story_sprint(tmp_path, slugs=["issue-2593", "issue-2608"])
    owned_at_worker_time: list[list[str]] = []

    def _observe(*_args, **_kwargs):
        scan = scan_story_executions(tmp_path)
        owned_at_worker_time.append(sorted(r.slug for r in scan.owned))
        return _make_coordinator_result()

    with (
        patch("theforge.sprint.runner._run_baseline_gate", return_value={"passed": True}),
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch(
            "theforge.sprint.runner.compute_batch_groups",
            return_value=[["issue-2593", "issue-2608"]],
        ),
        patch("theforge.sprint.runner.run_task", side_effect=_observe),
    ):
        run_sprint_ctx(config, manifest_path)

    assert owned_at_worker_time == [["issue-2593", "issue-2608"]]
    assert scan_story_executions(tmp_path).owned == ()


def test_a_registry_that_cannot_be_written_stops_the_dispatch(tmp_path: Path) -> None:
    """Fail closed: unowned work is work the next re-exec may throw away.

    The guard is the whole reason the registry is trustworthy. Without this the
    write could silently degrade to best-effort and the fix would regress to the
    behaviour it replaced.
    """
    config, manifest_path = _run_one_story_sprint(tmp_path, slugs=["issue-2593"])

    def _refuse(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    with (
        patch("theforge.sprint.runner._run_baseline_gate", return_value={"passed": True}),
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", side_effect=_observe_never) as mock_run_task,
        patch("theforge.sprint.story_executions.register_story_execution", side_effect=_refuse),
    ):
        result = run_sprint_ctx(config, manifest_path, run_id="run-2617")

    mock_run_task.assert_not_called()
    assert result.specs_succeeded == 0
    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    story = {s["slug"]: s for s in summary["stories"]}["issue-2593"]
    assert "ownership" in (story.get("error") or "").lower()


def _observe_never(*_args, **_kwargs):  # pragma: no cover - asserted not to run
    raise AssertionError("a story was dispatched without a durable ownership record")
