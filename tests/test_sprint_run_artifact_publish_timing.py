"""A story's own run artifacts must not refuse its successor (#2595).

A sprint writes each story's canonical run record and knowledge summary into the
project-root checkout as that story finishes, and both paths are *tracked* — the
generated ``.gitignore`` denies ``.forge/**`` and re-includes them as project
memory. The landing precondition is re-evaluated at every story's WORKSPACE
entry and refuses on any project-root dirt, untracked files included. Publishing
those artifacts only at sprint exit therefore left story 1's own success
standing as the reason story 2 was refused, before any agent was dispatched.

These are seam tests over the real ``run_sprint`` scheduler: only ``run_task``
is faked, and the fake does what a story does — observe the precondition it
would be refused by, then write its artifacts into the project root. What is
asserted is the ordering between those two, which is a property of the
scheduler, not of either mechanism on its own.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sprint_test_helpers import run_sprint_ctx

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.workspace import landing_precondition_error

BASE = "main"

# Mirrors the ``forge init`` output: ``.forge/**`` denied, the two project-memory
# trees re-included. Without it the artifacts would be ignored and no landing
# precondition would ever see them — the condition under test would not exist.
_FORGE_GITIGNORE = """\
.forge/**
!.forge/audits/
!.forge/audits/runs/
!.forge/audits/runs/**
!.forge/knowledge/
!.forge/knowledge/summaries/
!.forge/knowledge/summaries/**
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A clean git checkout on ``BASE`` with forge's own .gitignore in place."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "--initial-branch", BASE)
    _git(root, "config", "user.email", "forge@example.com")
    _git(root, "config", "user.name", "Forge Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / ".gitignore").write_text(_FORGE_GITIGNORE, encoding="utf-8")
    _git(root, "add", "README.md", ".gitignore")
    _git(root, "commit", "-m", "seed")
    # A real (bare) origin: the sprint pulls the base branch before its first
    # story, and a checkout with no remote fails that step for reasons that have
    # nothing to do with what is under test.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", BASE)
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-u", "origin", BASE)
    return root


def _make_config(root: Path) -> ForgeConfig:
    """A landing workflow: ``on_approve: merge`` with no origin to push to.

    ``auto_push`` stays at its default (off), which is the configuration the
    reported sprint ran under — the publish commits locally and does not push,
    and a local commit is still what cleans the checkout.
    """
    return ForgeConfig(
        project="test",
        project_root=root,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch=BASE,
            on_approve="merge",
        ),
        # A gate that passes: the sprint runs its baseline gate on the merge base
        # before dispatching, and this file is about what happens after that.
        validation=ValidationConfig(gate_command="true"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_manifest(root: Path, slugs: list[str], *, max_parallel: int = 1) -> Path:
    for slug in slugs:
        (root / f"{slug}.md").write_text(
            f"---\nname: {slug}\nslug: {slug}\n---\n# {slug}\nDo the thing.",
            encoding="utf-8",
        )
    manifest_path = root / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump(
            {
                "name": "Landing Sprint",
                "budget_usd": 30.0,
                "stories": [f"{slug}.md" for slug in slugs],
                "max_parallel": max_parallel,
            }
        ),
        encoding="utf-8",
    )
    # The spec files and the manifest are project-root dirt of the ordinary
    # kind; commit them so the sprint-entry precondition passes and the only
    # dirt the run can meet afterwards is the dirt it makes itself.
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "sprint stories")
    _git(root, "push", "origin", BASE)
    return manifest_path


def _result(cost: float = 1.0) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    preflight = MagicMock()
    preflight.cost_usd = cost
    state.preflight_result = preflight
    return CoordinatorResult(
        success=True, phase=Phase.DONE, state=state, message="Done.", merge={"merged": True}
    )


def _write_run_artifacts(root: Path, run_id: str) -> None:
    """What a finished story leaves behind in the project-root checkout."""
    runs = root / ".forge" / "audits" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
    summaries = root / ".forge" / "knowledge" / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / f"{run_id}.yaml").write_text(
        yaml.safe_dump({"run_id": run_id}, sort_keys=False), encoding="utf-8"
    )


def test_a_story_run_artifact_does_not_refuse_the_next_story(project_root: Path) -> None:
    """Three sequential landing stories, each leaving only its own artifacts."""
    config = _make_config(project_root)
    manifest_path = _make_manifest(project_root, ["story-a", "story-b", "story-c"])

    observed: list[tuple[str, str | None]] = []

    def fake_run_task(_config, task, *args, **kwargs):
        # The refusal the story would meet at its WORKSPACE entry, evaluated
        # against the checkout as the scheduler left it.
        observed.append(
            (task.slug, landing_precondition_error(config, lands_in_project_root=True))
        )
        _write_run_artifacts(project_root, f"run-{task.slug}")
        return _result()

    with patch("theforge.sprint.runner.run_task", side_effect=fake_run_task):
        sprint = run_sprint_ctx(config, manifest_path)

    assert [slug for slug, _ in observed] == ["story-a", "story-b", "story-c"]
    refused = [(slug, error) for slug, error in observed if error is not None]
    assert refused == [], f"stories refused by forge's own artifacts: {refused}"
    assert sprint.specs_succeeded == 3
    assert sprint.specs_failed == 0

    # Every story's artifacts are on the base branch by the time the run ends —
    # publishing early is a change of timing, not of what gets published.
    tree = _git(project_root, "ls-tree", "-r", "--name-only", BASE)
    for slug in ("story-a", "story-b", "story-c"):
        assert f".forge/audits/runs/run-{slug}.json" in tree
        assert f".forge/knowledge/summaries/run-{slug}.yaml" in tree


def test_no_story_is_dispatched_between_an_artifact_write_and_its_publish(
    project_root: Path,
) -> None:
    """The ordering, asserted as a sequence rather than through its symptom.

    A publish that merely happened *sometime* before the next entry would pass
    the test above by luck of scheduling. What the fix relies on is that the
    scheduler cannot dispatch between the two, so the interleaving itself is
    what is recorded here.
    """
    config = _make_config(project_root)
    manifest_path = _make_manifest(project_root, ["story-a", "story-b"])
    events: list[str] = []

    def fake_run_task(_config, task, *args, **kwargs):
        events.append(f"dispatch:{task.slug}")
        _write_run_artifacts(project_root, f"run-{task.slug}")
        events.append(f"artifacts:{task.slug}")
        return _result()

    from theforge.sprint import runner as _runner

    real_publish = _runner.publish_pending_story_run_audits

    def traced_publish(state, **kwargs):
        events.append("publish")
        return real_publish(state, **kwargs)

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("theforge.sprint.runner.publish_pending_story_run_audits", traced_publish),
    ):
        run_sprint_ctx(config, manifest_path)

    # Between story-a's artifact write and story-b's dispatch there is a publish
    # and nothing else — no dispatch of any story in the window.
    write_idx = events.index("artifacts:story-a")
    dispatch_idx = events.index("dispatch:story-b")
    assert write_idx < dispatch_idx
    between = events[write_idx + 1 : dispatch_idx]
    assert "publish" in between
    assert not [e for e in between if e.startswith("dispatch:")]


def test_operator_dirt_still_refuses_the_next_story(project_root: Path) -> None:
    """The gate is not weakened: only forge's own tracked artifacts are committed.

    ``.forge/audit-publish-state.json`` — written by the publish itself — is
    denied by the same .gitignore, so it is not dirt either. An unrelated file
    left in the project root is, and must still be refused.
    """
    config = _make_config(project_root)
    manifest_path = _make_manifest(project_root, ["story-a", "story-b"])

    observed: dict[str, str | None] = {}

    def fake_run_task(_config, task, *args, **kwargs):
        observed[task.slug] = landing_precondition_error(config, lands_in_project_root=True)
        _write_run_artifacts(project_root, f"run-{task.slug}")
        if task.slug == "story-a":
            (project_root / "operator-edit.txt").write_text("unrelated\n", encoding="utf-8")
        return _result()

    with patch("theforge.sprint.runner.run_task", side_effect=fake_run_task):
        run_sprint_ctx(config, manifest_path)

    assert observed["story-a"] is None
    assert observed["story-b"] is not None
    assert "operator-edit.txt" in observed["story-b"]
    # The refusal names the operator's file and nothing forge produced.
    assert ".forge/audits/runs" not in observed["story-b"]
    assert ".forge/knowledge/summaries" not in observed["story-b"]
    assert "audit-publish-state" not in observed["story-b"]


def test_a_rewritten_run_record_is_published_before_the_next_entry(
    project_root: Path,
) -> None:
    """The record a finished story is written more than once still lands in time.

    ``_write_native_story_record`` is called again with ``force_replace=True``
    after the knowledge summary is generated, and the landing status is
    re-persisted when a pending integration resolves. The publish has to come
    after the *last* of those rewrites, not the first.
    """
    config = _make_config(project_root)
    manifest_path = _make_manifest(project_root, ["story-a", "story-b"])
    observed: dict[str, str | None] = {}

    def fake_run_task(_config, task, *args, **kwargs):
        observed[task.slug] = landing_precondition_error(config, lands_in_project_root=True)
        _write_run_artifacts(project_root, f"run-{task.slug}")
        # A second, differing write of the same record — the rewrite the sprint
        # performs once landing resolves.
        record = project_root / ".forge" / "audits" / "runs" / f"run-{task.slug}.json"
        record.write_text(
            json.dumps({"run_id": f"run-{task.slug}", "landing_status": "landed"}) + "\n",
            encoding="utf-8",
        )
        return _result()

    with patch("theforge.sprint.runner.run_task", side_effect=fake_run_task):
        run_sprint_ctx(config, manifest_path)

    assert observed["story-b"] is None
    committed = json.loads(
        _git(project_root, "show", f"{BASE}:.forge/audits/runs/run-story-a.json")
    )
    assert committed["landing_status"] == "landed"


def _refused_result() -> CoordinatorResult:
    """A story refused at preflight: no commit, no landing, still a run record."""
    state = CoordinatorState()
    state.preflight_verdict = "BLOCKED"
    preflight = MagicMock()
    preflight.cost_usd = 0.1
    state.preflight_result = preflight
    return CoordinatorResult(
        success=False,
        phase=Phase.PREFLIGHT,
        state=state,
        message="Preflight: spec is blocked.",
    )


def test_a_story_refused_before_integration_does_not_refuse_its_siblings(
    project_root: Path,
) -> None:
    """#2755: a refusal's own record must not stand as dirt while workers run.

    ``story-a`` is refused at preflight — it never reaches ``_attempt_integration``
    and so never takes the pre-merge publish — while ``story-b`` is deliberately
    held open across ``story-c``'s admission. Before the fix, the pass-level
    publish was gated on a quiescent pass that ``story-b`` denied, so ``story-a``'s
    run record stood untracked in the project root and refused ``story-c`` at
    WORKSPACE entry with a LANDING PRECONDITION naming forge's own artifact.
    """
    import threading

    config = _make_config(project_root)
    manifest_path = _make_manifest(project_root, ["story-a", "story-b", "story-c"], max_parallel=2)

    observed: dict[str, str | None] = {}
    events: list[str] = []
    events_lock = threading.Lock()
    c_dispatched = threading.Event()

    def fake_run_task(_config, task, *args, **kwargs):
        with events_lock:
            events.append(f"dispatch:{task.slug}")
            observed[task.slug] = landing_precondition_error(config, lands_in_project_root=True)
        _write_run_artifacts(project_root, f"run-{task.slug}")
        if task.slug == "story-b":
            # Stay active across story-c's admission: the sprint is never
            # quiescent between story-a's refusal and story-c's entry check.
            c_dispatched.wait(timeout=60)
            return _result()
        if task.slug == "story-c":
            c_dispatched.set()
            return _result()
        return _refused_result()

    from theforge.sprint import runner as _runner

    real_publish = _runner.publish_pending_story_run_audits

    def traced_publish(state, **kwargs):
        with events_lock:
            events.append("publish")
        return real_publish(state, **kwargs)

    try:
        with (
            patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
            patch("theforge.sprint.runner.publish_pending_story_run_audits", traced_publish),
        ):
            run_sprint_ctx(config, manifest_path)
    finally:
        c_dispatched.set()

    assert observed.get("story-c") is None, (
        f"story-c was refused at WORKSPACE entry: {observed.get('story-c')}"
    )
    for slug, error in observed.items():
        if error is None:
            continue
        assert ".forge/audits/runs" not in error, f"{slug} refused by a forge artifact: {error}"
        assert ".forge/knowledge/summaries" not in error, (
            f"{slug} refused by a forge artifact: {error}"
        )

    # The publish is what closes the window, and it ran between the refusal and
    # the next story's admission rather than at some later quiescent pass.
    assert "dispatch:story-c" in events
    before_c = events[: events.index("dispatch:story-c")]
    assert "publish" in before_c, f"no publish before story-c was admitted: {events}"

    # The refused story's record is on the base branch, not left as dirt.
    tree = _git(project_root, "ls-tree", "-r", "--name-only", BASE)
    assert ".forge/audits/runs/run-story-a.json" in tree


def test_repeated_terminal_publishes_leave_no_dirt_and_no_empty_commits(
    project_root: Path,
) -> None:
    """The publish is called once per terminal outcome, and is idempotent.

    Every story here is refused, so the helper runs on each of them — including
    the last, by which point there is nothing pending. That must not produce an
    empty commit, a failure, or leftover pending state.
    """
    config = _make_config(project_root)
    manifest_path = _make_manifest(project_root, ["story-a", "story-b", "story-c"])

    def fake_run_task(_config, task, *args, **kwargs):
        _write_run_artifacts(project_root, f"run-{task.slug}")
        if task.slug == "story-c":
            # A terminal outcome whose artifacts a prior publish already took:
            # rewrite nothing new, so this story's publish has nothing to do.
            pass
        return _refused_result()

    with patch("theforge.sprint.runner.run_task", side_effect=fake_run_task):
        run_sprint_ctx(config, manifest_path)

    # No forge-authored dirt survives the run.
    status = _git(project_root, "status", "--porcelain")
    assert ".forge/audits/runs" not in status
    assert ".forge/knowledge/summaries" not in status

    # Every audit commit the run made carries a change; none is an empty commit.
    shas = _git(
        project_root, "log", "--format=%H", "--grep", "record sprint run audits", BASE
    ).split()
    assert shas, "the run published nothing"
    for sha in shas:
        changed = _git(project_root, "show", "--name-only", "--format=", sha)
        assert changed.strip(), f"empty audit commit {sha}"


def test_a_non_landing_workflow_is_unaffected(project_root: Path) -> None:
    """Nothing changes for a run whose stories never touch the project root."""
    config = dataclasses.replace(
        _make_config(project_root),
        workspace=dataclasses.replace(_make_config(project_root).workspace, on_approve="pr"),
    )
    manifest_path = _make_manifest(project_root, ["story-a", "story-b"])

    def fake_run_task(_config, task, *args, **kwargs):
        _write_run_artifacts(project_root, f"run-{task.slug}")
        return _result()

    with patch("theforge.sprint.runner.run_task", side_effect=fake_run_task):
        sprint = run_sprint_ctx(config, manifest_path)

    assert sprint.specs_succeeded == 2
