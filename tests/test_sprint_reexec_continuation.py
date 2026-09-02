"""Seam tests for mid-run re-exec treated as continuation, not re-launch.

Issue #2003: a re-exec (``os.execv`` after the workspace pull observes new
source) re-entered full sprint startup against a *live* sprint. Three startup
behaviours then fired with their preconditions no longer holding: a running
story was reclassified as a foreign active-worktree collision and dropped, its
worktree was swept, and the baseline gate — whose contract is "before any dev
work started" — ran while agents were consuming the host and timed out.

These tests pin the continuation semantics at the seams that produced each
symptom: launch-guard classification, the orphan worktree sweep, and the
runner's baseline-gate decision.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sprint_test_helpers import run_sprint_ctx, stub_resolved

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
from theforge.coordinator.workspace import sweep_orphan_worktrees
from theforge.process_group import group_has_running_members
from theforge.sprint.dag import StoryTriage
from theforge.sprint.launch_guard import (
    REASON_ACTIVE_WORKTREE,
    acquire_launch_story_locks,
)
from theforge.sprint.live_stories import (
    InheritedAgentGroup,
    LivenessResolution,
    await_inherited_agents,
    reclaim_inherited_agents,
    resolve_live_story_slugs,
)

# Beyond any pid this platform hands out, so ``killpg(pgid, 0)`` is reliably
# "no such group" — a group that has certainly finished.
_DEAD_PGID = 999999


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


# ── live-story identity ──────────────────────────────────────────────


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


def test_live_story_slugs_recognises_this_process_own_running_agent(tmp_path: Path) -> None:
    """A sidecar owned by this pid whose group is alive names a live story.

    The pid survives ``os.execv``, so "owner_pid == my pid" is exactly the
    identity relation a re-exec'd process needs to recognise its own work. The
    group used here is this test process's own — genuinely alive, no stubbing.
    """
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    (worktrees / "issue-1992").mkdir(parents=True)

    _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid(),
        pgid=os.getpgrp(),
        sandbox_dir=worktrees / "issue-1945",
    )

    live = resolve_live_story_slugs(
        ["issue-1945", "issue-1992"],
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
    )
    assert live == {"issue-1945"}


def test_live_story_slugs_ignores_other_processes_and_dead_groups(tmp_path: Path) -> None:
    """Foreign owners and dead groups are not this generation's live work."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    (worktrees / "issue-1992").mkdir(parents=True)

    # Another process's sidecar — alive group, but not our sprint.
    _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid() + 1,
        pgid=os.getpgrp(),
        sandbox_dir=worktrees / "issue-1945",
    )
    # Ours, but the group is gone.
    _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid(),
        pgid=os.getpgrp(),
        sandbox_dir=worktrees / "issue-1992",
    )

    live = resolve_live_story_slugs(
        ["issue-1945", "issue-1992"],
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        is_group_alive=lambda _pgid: False,
    )
    assert live == set()


# ── waiting for, and reclaiming, an inherited agent ──────────────────


def test_await_inherited_agents_returns_when_group_exits(tmp_path: Path) -> None:
    """The wait ends when the inherited group does, and clears its record."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=4242, sandbox_dir=worktrees / "issue-1945"
    )

    alive = iter([True, True, False, False])

    quiesced = await_inherited_agents(
        "issue-1945",
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        timeout=30,
        poll_interval=0.01,
        is_group_alive=lambda _pgid: next(alive, False),
    )

    assert quiesced is True
    # The image that registered it is gone, so nothing else would ever clear it.
    assert not sidecar.exists()


def test_await_inherited_agents_treats_an_unreaped_zombie_as_finished(
    tmp_path: Path,
) -> None:
    """A zombie inherited across re-exec is finished work, not running work."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(proc.pid)
    sidecar = _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid(),
        pgid=pgid,
        sandbox_dir=worktrees / "issue-1945",
    )
    logs: list[str] = []
    try:
        os.kill(proc.pid, signal.SIGKILL)
        # SIGKILL is asynchronous: the kernel may leave the child running for a
        # short while after the call returns, and under a loaded gate that window
        # outlasts the first liveness probe. Establish the condition this test is
        # about — an inherited group whose only member is an unreaped corpse —
        # before asserting on how it is treated. Deliberately no proc.wait() here:
        # reaping the child would remove the zombie the assertion depends on.
        deadline = time.monotonic() + 10
        while group_has_running_members(pgid):
            assert time.monotonic() < deadline, "killed child never stopped running"
            time.sleep(0.01)
        started = time.monotonic()

        quiesced = await_inherited_agents(
            "issue-1945",
            project_root=tmp_path,
            path_pattern=".forge/worktrees/{slug}",
            timeout=30,
            poll_interval=0.01,
            log=logs.append,
        )

        assert quiesced is True
        assert time.monotonic() - started < 1.0
        assert logs == []
        assert not sidecar.exists()
    finally:
        proc.wait(timeout=5)


def test_await_inherited_agents_logs_resume_only_after_announcing_the_wait(
    tmp_path: Path,
) -> None:
    """A completion log should not appear unless the wait was actually announced."""
    sidecar = tmp_path / "agent-sidecar.json"
    sidecar.write_text("{}", encoding="utf-8")
    group = InheritedAgentGroup(slug="issue-1945", pgid=4242, sidecar=sidecar)
    resolutions = iter(
        [
            LivenessResolution(live_slugs=frozenset({"issue-1945"})),
            LivenessResolution(live_slugs=frozenset({"issue-1945"})),
            LivenessResolution(),
        ]
    )
    calls = iter([True, True, False])
    logs: list[str] = []

    with (
        patch(
            "theforge.sprint.live_stories.resolve_liveness",
            side_effect=lambda *args, **kwargs: next(resolutions),
        ),
        patch(
            "theforge.sprint.live_stories.resolve_inherited_agents",
            side_effect=lambda *args, **kwargs: [group] if next(calls) else [],
        ),
    ):
        quiesced = await_inherited_agents(
            "issue-1945",
            project_root=tmp_path,
            path_pattern=".forge/worktrees/{slug}",
            timeout=0.1,
            poll_interval=0.01,
            log=logs.append,
        )

    assert quiesced is True
    assert logs == [
        "IN-FLIGHT issue-1945: waiting up to 0s for the agent process group(s) 4242 inherited "
        "across the re-exec to finish before resuming",
        "IN-FLIGHT issue-1945: inherited agent finished; resuming the story",
    ]
    assert not sidecar.exists()


def test_await_inherited_agents_reports_overrun_without_killing(tmp_path: Path) -> None:
    """A wait that times out returns False and leaves the group alone.

    Killing is the caller's decision — the wait must not silently destroy work.
    """
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=4242, sandbox_dir=worktrees / "issue-1945"
    )

    quiesced = await_inherited_agents(
        "issue-1945",
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        timeout=0.05,
        poll_interval=0.01,
        is_group_alive=lambda _pgid: True,
    )

    assert quiesced is False
    assert sidecar.exists()


def test_await_inherited_agents_keeps_waiting_when_liveness_is_unresolved(
    tmp_path: Path,
) -> None:
    """An unobservable inherited group is not finished work."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=4242, sandbox_dir=worktrees / "issue-1945"
    )
    logs: list[str] = []

    quiesced = await_inherited_agents(
        "issue-1945",
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        timeout=0.05,
        poll_interval=0.01,
        is_group_alive=lambda _pgid: (_ for _ in ()).throw(OSError("transient scan failure")),
        log=logs.append,
    )

    assert quiesced is False
    assert sidecar.exists()
    assert logs == [
        "IN-FLIGHT issue-1945: waiting up to 0s because inherited agent liveness is temporarily "
        "unobservable; retaining its sidecar until it is confirmed finished or the wait times out"
    ]


def test_await_inherited_agents_logs_unobservable_wait_when_followup_probe_loses_the_group(
    tmp_path: Path,
) -> None:
    """The wait log must not claim blank pgids if the follow-up probe cannot re-read them."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=4242, sandbox_dir=worktrees / "issue-1945"
    )
    calls = iter([True, OSError("transient scan failure"), OSError("transient scan failure")])
    logs: list[str] = []

    def _probe(_pgid: int) -> bool:
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    quiesced = await_inherited_agents(
        "issue-1945",
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        timeout=0.05,
        poll_interval=0.01,
        is_group_alive=_probe,
        log=logs.append,
    )

    assert quiesced is False
    assert sidecar.exists()
    assert logs == [
        "IN-FLIGHT issue-1945: waiting up to 0s because inherited agent liveness is temporarily "
        "unobservable; retaining its sidecar until it is confirmed finished or the wait times out"
    ]


def test_reclaim_inherited_agents_kills_survivor_and_clears_record(tmp_path: Path) -> None:
    """An agent that overruns is terminated, so no second agent shares its
    worktree and no later reaper inherits the pgid."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=4242, sandbox_dir=worktrees / "issue-1945"
    )
    killed: list[int] = []

    reclaimed = reclaim_inherited_agents(
        "issue-1945",
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        is_group_alive=lambda _pgid: True,
        kill_group=lambda pgid: (killed.append(pgid), True)[1],
    )

    assert reclaimed == [4242]
    assert killed == [4242]
    assert not sidecar.exists()


def test_reclaim_inherited_agents_kills_unresolved_group_and_clears_record(tmp_path: Path) -> None:
    """A timed-out inherited group that stays unobservable is still reclaimed."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=4242, sandbox_dir=worktrees / "issue-1945"
    )
    killed: list[int] = []

    reclaimed = reclaim_inherited_agents(
        "issue-1945",
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        is_group_alive=lambda _pgid: (_ for _ in ()).throw(OSError("transient scan failure")),
        kill_group=lambda pgid: (killed.append(pgid), True)[1],
    )

    assert reclaimed == [4242]
    assert killed == [4242]
    assert not sidecar.exists()


def test_reclaim_inherited_agents_keeps_sidecar_when_unresolved_kill_is_refused(
    tmp_path: Path,
) -> None:
    """A possibly-live inherited group must stay registered if the reclaim kill fails."""
    worktrees = tmp_path / ".forge" / "worktrees"
    (worktrees / "issue-1945").mkdir(parents=True)
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=4242, sandbox_dir=worktrees / "issue-1945"
    )
    killed: list[int] = []

    reclaimed = reclaim_inherited_agents(
        "issue-1945",
        project_root=tmp_path,
        path_pattern=".forge/worktrees/{slug}",
        is_group_alive=lambda _pgid: (_ for _ in ()).throw(OSError("transient scan failure")),
        kill_group=lambda pgid: (killed.append(pgid), False)[1],
    )

    assert reclaimed == []
    assert killed == [4242]
    assert sidecar.exists()


# ── launch-guard classification ──────────────────────────────────────


def test_reexec_keeps_own_live_worktree_scheduled_not_dropped(
    tmp_path: Path,
) -> None:
    """The issue-1945 symptom: a live story must not become a collision drop.

    Its worktree is active (commits ahead of base) and it has no prior-generation
    recorded outcome — the exact combination that used to fall through to
    ``REASON_ACTIVE_WORKTREE``, print "DROPPED … continuing", and leave the
    worktree unclaimed for the sweep to delete. It must come back *scheduled*
    (with its launch lock held): only this process can finish it, so dropping it
    would strand the story just as surely as deleting the worktree.
    """
    config = _make_config(tmp_path)
    lock_dir = tmp_path / ".forge" / "locks"
    lock_dir.mkdir(parents=True)
    live_lock = lock_dir / "issue-1945.lock"
    live_lock.write_text("999999|Mon|fingerprint", encoding="utf-8")

    with (
        patch(
            "theforge.sprint.launch_guard.check_active_worktrees",
            return_value=["issue-1945"],
        ) as mock_active,
        patch("theforge.sprint.launch_guard.check_escalated_worktrees", return_value=[]),
    ):
        locked_fds, exit_code, dropped = acquire_launch_story_locks(
            slugs=["issue-1945", "issue-1992"],
            config=config,
            resume=False,
            allow_drop=True,
            live_slugs={"issue-1945"},
        )

    try:
        assert exit_code is None
        # Not dropped at all — for any reason, least of all a self-collision.
        assert "issue-1945" not in dropped
        assert REASON_ACTIVE_WORKTREE not in dropped.values()
        # The live slug is never even offered to the worktree collision check.
        assert "issue-1945" not in mock_active.call_args.args[0]
        # Both stories are scheduled and hold launch locks.
        assert live_lock.exists()
        assert "issue-1992" not in dropped
        assert len(locked_fds) == 2
    finally:
        from theforge.sprint.lock import release_story_locks

        release_story_locks(locked_fds)


# ── orphan worktree sweep ────────────────────────────────────────────


def test_orphan_sweep_preserves_protected_live_worktree(tmp_path: Path) -> None:
    """A live sprint-owned worktree is skipped by the sweep; others are not.

    The directory here holds only Forge-owned artifacts, which is the shape the
    sweep removes unconditionally — so surviving is attributable to the
    protection, not to some other guard.
    """
    config = _make_config(tmp_path)
    worktrees = tmp_path / ".forge" / "worktrees"
    for slug in ("issue-1945", "issue-1899"):
        (worktrees / slug / ".forge").mkdir(parents=True)

    sweep_orphan_worktrees(tmp_path, config, protected_slugs={"issue-1945"})

    assert (worktrees / "issue-1945").exists()
    assert not (worktrees / "issue-1899").exists()


def test_runner_passes_live_slugs_to_orphan_sweep(tmp_path: Path) -> None:
    """The runner must hand its live-story set to the sweep, not just to itself."""
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-b.md"])
    config = _make_config(tmp_path)

    with (
        patch("theforge.sprint.runner.sweep_orphan_worktrees") as mock_sweep,
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            live_story_slugs={"feature-a"},
        )

    assert mock_sweep.call_args.kwargs["protected_slugs"] == {"feature-a"}


# ── baseline gate: a startup-only check ──────────────────────────────


def test_reexec_with_live_story_skips_baseline_gate_and_resumes_it(tmp_path: Path) -> None:
    """The whole failure, end to end: gate not re-run, live story resumed.

    Given the incident's inputs — one story still running, one still to do — the
    re-exec must not run the baseline gate (its precondition, "no dev work
    started", is false), must not drop the live story, and must carry the live
    story through to a terminal outcome in this run rather than abandoning it.
    The sidecar names this pid as owner, so nothing else ever could: once this
    process exits, the next invocation's orphan reaper kills the group.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    # feature-a's inherited agent has already exited (pgid beyond any real pid),
    # so the deferred dispatch waits, observes it gone, and resumes immediately.
    (tmp_path / "feature-a").mkdir()
    sidecar = _write_agent_sidecar(
        tmp_path, owner_pid=os.getpid(), pgid=_DEAD_PGID, sandbox_dir=tmp_path / "feature-a"
    )

    with (
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full) as mock_triage,
        patch(
            "theforge.sprint.runner.run_batch_preflight", return_value={}
        ) as mock_batch_preflight,
        patch(
            "theforge.sprint.runner.run_task", return_value=_make_coordinator_result()
        ) as mock_run_task,
    ):
        result = run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            live_story_slugs={"feature-a"},
        )

    # (a) The startup-only check did not run against a live sprint.
    mock_gate.assert_not_called()

    # (b) The live story was not dropped: it ran, and it reached a terminal
    # success like any other story. Both stories are accounted for.
    dispatched = [c.args[1].slug for c in mock_run_task.call_args_list]
    assert sorted(dispatched) == ["feature-a", "feature-b"]
    assert result.specs_succeeded == 2
    assert result.specs_failed == 0
    assert result.specs_skipped == 0

    # (c) It did not consume the passes that would have collided with the agent
    # still writing to its worktree.
    assert "feature-a" not in [t.slug for t in mock_batch_preflight.call_args.args[0]]
    # Its triage happened at dispatch (after the wait), not in the startup sweep
    # that ran while the agent was still writing.
    triaged_refs = [c.args[0] for c in mock_triage.call_args_list]
    assert triaged_refs[0] == "feature-b.md"
    assert triaged_refs.count("feature-a.md") == 1

    # (d) The inherited group's record is cleaned up, so no later reaper acts on
    # a pgid this run has already settled.
    assert not sidecar.exists()

    # (e) The gate skip and its evidence are recorded, not silent.
    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    by_slug = {s["slug"]: s for s in summary["stories"]}
    assert by_slug["feature-a"]["outcome"] == "DONE"
    audit = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    baseline = audit["baseline_check"]
    assert baseline["status"] == "skipped"
    assert baseline["skip_reason"] == "reexec_continuation"
    assert "feature-a" in baseline["skip_evidence"]


def test_reexec_before_any_work_still_runs_baseline_gate(tmp_path: Path) -> None:
    """A re-exec is not by itself proof that work started.

    The sprint's own first pull can trigger the re-exec before any story runs;
    that launch is still a genuine start, and skipping the baseline gate there
    would lose the check entirely rather than defer it.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
    config = _make_config(tmp_path)

    with (
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint_ctx(config, manifest_path, reexec=True)

    mock_gate.assert_called_once()


def test_fresh_start_still_runs_baseline_gate(tmp_path: Path) -> None:
    """Non-re-exec launches are untouched by the continuation carve-out."""
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
    config = _make_config(tmp_path)

    with (
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint_ctx(config, manifest_path)

    mock_gate.assert_called_once()


# ── CLI wiring ───────────────────────────────────────────────────────


def test_cli_threads_live_story_slugs_into_launch_and_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI resolves live stories once on re-exec and hands them to both
    consumers — the launch guard (classification) and run_sprint (sweep
    protection, gate decision)."""
    import argparse

    from theforge import detach as _detach_mod
    from theforge.cli import sprint as sprint_cli

    manifest_path = _make_manifest(tmp_path, ["issue-1945.md"])
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")

    fake_config = SimpleNamespace(
        project_root=tmp_path,
        project="test",
        workspace=SimpleNamespace(path_pattern=".forge/worktrees/{slug}"),
    )

    worktree = tmp_path / ".forge" / "worktrees" / "issue-1945"
    worktree.mkdir(parents=True)
    _write_agent_sidecar(
        tmp_path,
        owner_pid=os.getpid(),
        pgid=os.getpgrp(),
        sandbox_dir=worktree,
    )

    args = argparse.Namespace(
        manifest=str(manifest_path),
        milestone=None,
        label=None,
        issues=None,
        config=str(config_path),
        base_branch=None,
        name=None,
        fg=False,
        detach=False,
        dry_run=False,
        verbose=False,
        auto_merge=False,
        interactive=False,
        resume=False,
        no_pull=False,
        parallel=None,
        force=False,
        no_notify=True,
    )

    monkeypatch.setenv("FORGE_DETACHED", "1")
    monkeypatch.setenv("FORGE_DETACHED_RUN_ID", "child-run-id")
    monkeypatch.setenv("FORGE_PREV_RUN_ID", "prev-run-id")

    fake_result = MagicMock()
    fake_result.specs_failed = 0

    with (
        patch("theforge.cli.sprint.load_config", return_value=fake_config),
        patch("theforge.cli.sprint.apply_base_branch_override", return_value=fake_config),
        patch("theforge.cli.sprint._find_config", return_value=config_path),
        patch("theforge.cli.sprint.parse_manifest_slugs", return_value=["issue-1945"]),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            # The guard keeps an in-flight story scheduled: nothing is dropped.
            return_value=([], None, {}),
        ) as mock_locks,
        patch("theforge.cli.sprint.reacquire_story_locks_in_daemon", return_value=[]),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=fake_result) as mock_run_sprint,
        patch("theforge.sprint.runner.resolve_from_manifest", return_value=stub_resolved()),
        patch.object(_detach_mod, "install_cleanup_handler"),
        patch.object(_detach_mod, "write_run_ended"),
        patch.object(_detach_mod, "remove_pid"),
    ):
        rc = sprint_cli.cmd_sprint(args)

    assert rc == 0
    assert mock_locks.call_args.kwargs["live_slugs"] == {"issue-1945"}
    assert mock_run_sprint.call_args.args[0].live_story_slugs == frozenset({"issue-1945"})
