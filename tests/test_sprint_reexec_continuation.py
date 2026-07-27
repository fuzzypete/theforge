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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
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
from theforge.coordinator.workspace import sweep_orphan_worktrees
from theforge.sprint import run_sprint
from theforge.sprint.dag import StoryTriage
from theforge.sprint.launch_guard import (
    REASON_ACTIVE_WORKTREE,
    REASON_IN_FLIGHT,
    acquire_launch_story_locks,
)
from theforge.sprint.live_stories import resolve_live_story_slugs


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


def _triage_full(spec_path, config, project_root, *, task=None):
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


# ── launch-guard classification ──────────────────────────────────────


def test_reexec_classifies_own_live_worktree_as_in_flight_not_collision(
    tmp_path: Path,
) -> None:
    """The issue-1945 symptom: a live story must not become a collision drop.

    Its worktree is active (commits ahead of base) and it has no prior-generation
    recorded outcome — the exact combination that used to fall through to
    ``REASON_ACTIVE_WORKTREE``, print "DROPPED … continuing", and leave the
    worktree unclaimed for the sweep to delete.
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
        assert dropped["issue-1945"] == REASON_IN_FLIGHT
        assert REASON_ACTIVE_WORKTREE not in dropped.values()
        # The live slug is never even offered to the worktree collision check.
        assert "issue-1945" not in mock_active.call_args.args[0]
        # Its lock file survives the launch sweep — that file is what tells the
        # worktree sweep the directory is claimed.
        assert live_lock.exists()
        # The remaining story is scheduled normally.
        assert "issue-1992" not in dropped
        assert len(locked_fds) == 1
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
        run_sprint(
            config,
            manifest_path,
            reexec=True,
            live_story_slugs={"feature-a"},
            dropped_slugs={"feature-a": REASON_IN_FLIGHT},
        )

    assert mock_sweep.call_args.kwargs["protected_slugs"] == {"feature-a"}


# ── baseline gate: a startup-only check ──────────────────────────────


def test_reexec_with_live_story_skips_baseline_gate_and_continues(tmp_path: Path) -> None:
    """The whole failure, end to end: live story preserved, gate not re-run.

    Given the incident's inputs — one story still running, one still to do — the
    re-exec must not run the baseline gate (its precondition, "no dev work
    started", is false), must not drop the live story, and must carry on
    scheduling the remaining work.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    with (
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch(
            "theforge.sprint.runner.run_batch_preflight", return_value={}
        ) as mock_batch_preflight,
        patch(
            "theforge.sprint.runner.run_task", return_value=_make_coordinator_result()
        ) as mock_run_task,
    ):
        result = run_sprint(
            config,
            manifest_path,
            reexec=True,
            live_story_slugs={"feature-a"},
            dropped_slugs={"feature-a": REASON_IN_FLIGHT},
        )

    # (a) The startup-only check did not run against a live sprint.
    mock_gate.assert_not_called()

    # (b) The live story was neither re-dispatched (two agents, one worktree) nor
    # counted as a failure.
    dispatched = [c.args[1].slug for c in mock_run_task.call_args_list]
    assert dispatched == ["feature-b"]
    assert "feature-a" not in [t.slug for t in mock_batch_preflight.call_args.args[0]]
    assert result.specs_failed == 0
    assert result.specs_skipped == 1

    # (c) The sprint continued and finished the remaining work.
    assert result.specs_succeeded == 1

    # (d) The skip and its evidence are recorded, not silent.
    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    by_slug = {s["slug"]: s for s in summary["stories"]}
    assert by_slug["feature-a"]["outcome"] == "PRESERVED"
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
        run_sprint(config, manifest_path, reexec=True)

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
        run_sprint(config, manifest_path)

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
            return_value=([], None, {"issue-1945": REASON_IN_FLIGHT}),
        ) as mock_locks,
        patch("theforge.cli.sprint.reacquire_story_locks_in_daemon", return_value=[]),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=fake_result) as mock_run_sprint,
        patch.object(_detach_mod, "install_cleanup_handler"),
        patch.object(_detach_mod, "write_run_ended"),
        patch.object(_detach_mod, "remove_pid"),
    ):
        rc = sprint_cli.cmd_sprint(args)

    assert rc == 0
    assert mock_locks.call_args.kwargs["live_slugs"] == {"issue-1945"}
    assert mock_run_sprint.call_args.kwargs["live_story_slugs"] == {"issue-1945"}
