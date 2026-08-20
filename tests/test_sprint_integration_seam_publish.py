"""Sibling run artifacts are published before a story lands (#2602).

Each story writes its canonical run record and knowledge summary directly into
the shared project-root checkout as it completes. Under ``max_parallel > 1`` a
sibling can do that between an approved story's entry-time landing precondition
check and its own merge, and ``_merge_branch`` then refuses the approved story
for dirt it did not cause — after dev and review have been paid for.

These tests pin the seam that closes that: ``_attempt_integration`` publishes
pending story-run artifacts inside ``integration_lock``, immediately before the
merge, so ``land_story`` always observes a root free of sibling artifacts. They
also pin what must *not* change — operator dirt is still dirt, and unrelated
files are never swept into the artifact commit.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_config

from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.workspace import project_root_dirty_status
from theforge.sprint.audit_publish import (
    project_root_dirt_is_story_run_artifacts_only,
    story_run_artifact_dirt_only,
)
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import (
    SprintExecutionState,
    SprintRunContext,
    _attempt_integration,
)
from theforge.task import TaskStory

SIBLING_RUN = "13a56e534cb5"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "forge@example.com")
    _git(root, "config", "user.name", "Forge Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    # Mirrors the real deny-all-then-allow-the-artifact-trees rule: everything
    # forge writes under .forge/ is ignored except the two trees that are the
    # canonical record, which are exactly the ones that dirty the root.
    (root / ".gitignore").write_text(
        "\n".join(
            [
                ".forge/**",
                "!.forge/audits/",
                "!.forge/audits/runs/",
                "!.forge/audits/runs/**",
                "!.forge/knowledge/",
                "!.forge/knowledge/summaries/",
                "!.forge/knowledge/summaries/**",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(root, "add", "README.md", ".gitignore")
    _git(root, "commit", "-m", "seed")
    return root


def _write_sibling_artifacts(root: Path, run_id: str = SIBLING_RUN) -> list[str]:
    """The artifacts a sibling story leaves behind when it completes."""
    runs = root / ".forge" / "audits" / "runs"
    summaries = root / ".forge" / "knowledge" / "summaries"
    runs.mkdir(parents=True, exist_ok=True)
    summaries.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
    (summaries / f"{run_id}.yaml").write_text(f"run_id: {run_id}\n", encoding="utf-8")
    return [
        f".forge/audits/runs/{run_id}.json",
        f".forge/knowledge/summaries/{run_id}.yaml",
    ]


def _state(root: Path, *, on_approve: str = "merge") -> tuple[SprintExecutionState, TaskStory]:
    config = _make_config(root)
    config = dataclasses.replace(
        config,
        workspace=dataclasses.replace(
            config.workspace,
            on_approve=on_approve,
            base_branch="main",
            # auto_push off keeps the publish local: the seam's job here is the
            # commit, and a push would need a remote this test has no business
            # standing up.
            auto_push=False,
        ),
    )
    task = TaskStory(name="Issue 339", slug="issue-339", github_issue=339)
    resolved = ResolvedSprint(
        name="issues-339,340",
        budget_usd=50.0,
        stories=[(task, None, "issue:339")],
        max_parallel=3,
    )
    context = SprintRunContext(
        config=config,
        resolved=resolved,
        sprint_id="sprint-2602",
        run_id="run-2602",
    )
    state = SprintExecutionState.for_run(context)
    state.dag = SimpleNamespace(mark_complete=lambda slug: None)
    return state, task


def _approved_result(task: TaskStory) -> CoordinatorResult:
    coord_state = CoordinatorState()
    coord_state.run_id = "aaaabbbbcccc"
    coord_state.phase = Phase.DONE
    return CoordinatorResult(
        success=True,
        phase=Phase.DONE,
        state=coord_state,
        message="approved",
        merge={"action": "merge"},
        landing_status="pending_integration",
    )


def _run_integration(state, task, result, land_story):
    """Run the seam with only ``land_story`` stubbed; the publish path is real.

    The audit/landing bookkeeping either side of the merge is silenced because
    it writes into the very artifact trees under test — the seam, not the
    record-keeping, is what these tests are about.
    """
    with ExitStack() as stack:
        stack.enter_context(patch("theforge.coordinator.completion.land_story", land_story))
        for target in (
            "_write_story_audit",
            "_persist_story_landing",
            "_resolve_batch_leader_landing",
        ):
            stack.enter_context(patch(f"theforge.sprint.runner.{target}"))
        return _attempt_integration(state, task.slug, task, result)


# ── The predicate ──────────────────────────────────────────────────────


class TestStoryRunArtifactDirtOnly:
    def test_sibling_artifacts_are_attributed(self):
        status = (
            "?? .forge/audits/runs/13a56e534cb5.json\n"
            "?? .forge/knowledge/summaries/13a56e534cb5.yaml"
        )
        assert story_run_artifact_dirt_only(status) is True

    def test_operator_dirt_is_not(self):
        assert story_run_artifact_dirt_only(" M forge.yaml") is False

    def test_mixed_dirt_is_not(self):
        status = "?? .forge/audits/runs/13a56e534cb5.json\n M forge.yaml"
        assert story_run_artifact_dirt_only(status) is False

    def test_clean_is_not_artifact_dirt(self):
        assert story_run_artifact_dirt_only("") is False

    def test_untracked_artifact_directory_is_attributed(self):
        assert story_run_artifact_dirt_only("?? .forge/audits/runs/") is True

    def test_collapsed_parent_directory_is_not_attributed(self):
        # ``?? .forge/`` names more than the artifact trees; only the expanded
        # form can be attributed.
        assert story_run_artifact_dirt_only("?? .forge/") is False


class TestProjectRootDirtIsStoryRunArtifactsOnly:
    def test_wholly_untracked_forge_tree_is_expanded_and_attributed(self, tmp_path: Path) -> None:
        """A first-ever sprint's collapsed ``?? .forge/`` still attributes."""
        root = _repo(tmp_path)
        _write_sibling_artifacts(root)
        assert project_root_dirty_status(root) == "?? .forge/"
        assert project_root_dirt_is_story_run_artifacts_only(root) is True

    def test_operator_dirt_alongside_artifacts_does_not_attribute(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        _write_sibling_artifacts(root)
        (root / "forge.yaml").write_text("project: test\n", encoding="utf-8")
        assert project_root_dirt_is_story_run_artifacts_only(root) is False

    def test_clean_root_does_not_attribute(self, tmp_path: Path) -> None:
        assert project_root_dirt_is_story_run_artifacts_only(_repo(tmp_path)) is False


# ── The seam ───────────────────────────────────────────────────────────


def test_sibling_artifacts_are_committed_before_land_story_runs(tmp_path: Path) -> None:
    """The approved story's merge sees a clean root despite a sibling's artifacts."""
    root = _repo(tmp_path)
    artifacts = _write_sibling_artifacts(root)
    assert project_root_dirty_status(root) != ""

    state, task = _state(root)
    result = _approved_result(task)
    observed: dict = {}

    def _land_story(config, task_, branch, wt, review, st, on_approve, **kwargs):
        observed["dirty"] = project_root_dirty_status(config.project_root)
        return {"attempted": True, "merged": True, "base_branch": "main", "error": None}, "merged"

    assert _run_integration(state, task, result, _land_story) is True

    assert observed["dirty"] == "", (
        "land_story must run against a root the sibling's artifacts have been "
        f"committed out of; saw: {observed['dirty']}"
    )
    assert result.landing_status == "merged"
    assert task.slug in state.merged_slugs

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(committed) == sorted(artifacts)


def test_a_sibling_landing_after_the_publish_is_retried_not_refused(tmp_path: Path) -> None:
    """A sibling arriving in the gap costs a republish, not the approved story."""
    root = _repo(tmp_path)
    state, task = _state(root)
    result = _approved_result(task)
    attempts: list[str] = []

    def _land_story(config, task_, branch, wt, review, st, on_approve, **kwargs):
        dirty = project_root_dirty_status(config.project_root)
        attempts.append(dirty)
        if len(attempts) == 1:
            # The sibling finished between the publish and this check.
            _write_sibling_artifacts(config.project_root, run_id="latecomer0001")
            return (
                {
                    "attempted": True,
                    "merged": False,
                    "base_branch": "main",
                    "error": "Uncommitted changes in project root: ?? .forge/audits/runs/x.json",
                },
                "failed",
            )
        return {"attempted": True, "merged": True, "base_branch": "main", "error": None}, "merged"

    assert _run_integration(state, task, result, _land_story) is True

    assert len(attempts) == 2, "the seam must republish and retry once"
    assert attempts[1] == "", "the retry must see the late sibling's artifacts committed"
    assert result.landing_status == "merged"


def test_operator_dirt_still_refuses_the_landing(tmp_path: Path) -> None:
    """The publish never launders unrelated dirt into a landable root."""
    root = _repo(tmp_path)
    (root / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    _write_sibling_artifacts(root)

    state, task = _state(root)
    result = _approved_result(task)
    attempts: list[str] = []

    def _land_story(config, task_, branch, wt, review, st, on_approve, **kwargs):
        dirty = project_root_dirty_status(config.project_root)
        attempts.append(dirty)
        return (
            {
                "attempted": True,
                "merged": False,
                "base_branch": "main",
                "error": f"Uncommitted changes in project root: {dirty}",
            },
            "failed",
        )

    assert _run_integration(state, task, result, _land_story) is True

    assert len(attempts) == 1, "operator dirt is not a republish-and-retry condition"
    assert "forge.yaml" in attempts[0]
    assert result.landing_status == "failed"
    # The artifact commit stages only the artifact trees; forge.yaml is still
    # the operator's uncommitted file.
    assert "forge.yaml" in project_root_dirty_status(root)


def test_non_local_landing_does_not_publish(tmp_path: Path) -> None:
    """Under ``on_approve: pr`` nothing lands in the root, so nothing is committed."""
    root = _repo(tmp_path)
    _write_sibling_artifacts(root)
    before = project_root_dirty_status(root)

    state, task = _state(root, on_approve="pr")
    result = _approved_result(task)
    result.merge = {"action": "pr"}

    def _land_story(config, task_, branch, wt, review, st, on_approve, **kwargs):
        return {"attempted": True, "merged": True, "base_branch": "main", "error": None}, "merged"

    assert _run_integration(state, task, result, _land_story) is True
    assert project_root_dirty_status(root) == before


def test_publish_failure_is_attributed_on_the_merge_record(tmp_path: Path) -> None:
    """A swallowed publish failure is distinguishable from ordinary sibling dirt."""
    root = _repo(tmp_path)
    _write_sibling_artifacts(root)
    # A checkout on the wrong branch is a publish health failure: the publish
    # refuses, the root stays dirty, and the landing fails for that reason.
    _git(root, "checkout", "-b", "some-other-branch")

    state, task = _state(root)
    result = _approved_result(task)

    def _land_story(config, task_, branch, wt, review, st, on_approve, **kwargs):
        dirty = project_root_dirty_status(config.project_root)
        return (
            {
                "attempted": True,
                "merged": False,
                "base_branch": "main",
                "error": f"Uncommitted changes in project root: {dirty}",
            },
            "failed",
        )

    assert _run_integration(state, task, result, _land_story) is True
    assert result.merge is not None
    assert "story_run_audit_publish_state" in result.merge
    assert result.merge["story_run_audit_publish_state"] == "branch_mismatch"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
