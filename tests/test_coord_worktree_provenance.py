"""Worktree provenance: does the story text that produced this tree still govern? (#2288)

Phase records are refused when the story text that produced them has changed,
but the working tree those phases produced was adopted on identity alone — so a
dev agent inherited an implementation of superseded text as its starting point
and spent its first iteration undoing it.

These tests drive the real `_create_workspace` reuse path over real git rather
than mocks: the defect is in what workspace setup does when it finds a tree, and
the contract is that a story whose text has NOT changed reuses its worktree
exactly as before.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.workspace import _create_workspace
from theforge.coordinator.worktree_provenance import (
    PROVENANCE_CHANGED,
    PROVENANCE_FRESH,
    PROVENANCE_MATCH,
    PROVENANCE_UNKNOWN,
    WorktreeProvenance,
    clear_worktree_provenance,
    evaluate_worktree_provenance,
    inherited_work_note,
    last_worktree_provenance,
    read_worktree_provenance,
    record_worktree_provenance,
    story_content_hash,
    worktree_provenance_path,
)
from theforge.task import TaskStory
from theforge.task.dev_prompts import build_dev_prompt

STORY_V1 = "# story\n\nImplement the thing the old way.\n"
STORY_V2 = "# story\n\nThat diagnosis was wrong. Implement it the other way.\n"


# ── Unit: the judgement itself ───────────────────────────────────────────


def _record(project_root: Path, slug: str, story: str, *, adopted: bool = False) -> None:
    record_worktree_provenance(
        project_root,
        slug,
        evaluate_worktree_provenance(project_root, slug, story, adopted=adopted),
    )


class TestEvaluateWorktreeProvenance:
    def test_fresh_worktree_inherits_nothing(self, tmp_path: Path) -> None:
        prov = evaluate_worktree_provenance(tmp_path, "s", STORY_V1, adopted=False)
        assert prov.status == PROVENANCE_FRESH
        assert prov.adopted is False
        assert prov.inherits_superseded_work is False

    def test_adoption_without_a_record_is_unknown_not_changed(self, tmp_path: Path) -> None:
        """A tree from before provenance existed must not be reported as superseded."""
        prov = evaluate_worktree_provenance(tmp_path, "s", STORY_V1, adopted=True)
        assert prov.status == PROVENANCE_UNKNOWN
        assert prov.inherits_superseded_work is False

    def test_same_story_text_matches(self, tmp_path: Path) -> None:
        _record(tmp_path, "s", STORY_V1)
        prov = evaluate_worktree_provenance(tmp_path, "s", STORY_V1, adopted=True)
        assert prov.status == PROVENANCE_MATCH
        assert prov.recorded_hash == story_content_hash(STORY_V1)
        assert prov.inherits_superseded_work is False

    def test_edited_story_text_is_reported_as_changed(self, tmp_path: Path) -> None:
        _record(tmp_path, "s", STORY_V1)
        prov = evaluate_worktree_provenance(tmp_path, "s", STORY_V2, adopted=True)
        assert prov.status == PROVENANCE_CHANGED
        assert prov.recorded_hash == story_content_hash(STORY_V1)
        assert prov.current_hash == story_content_hash(STORY_V2)
        assert prov.inherits_superseded_work is True

    def test_no_story_text_in_hand_leaves_the_record_alone(self, tmp_path: Path) -> None:
        """Never overwrite a real hash with a null: the next run's judgement depends on it."""
        _record(tmp_path, "s", STORY_V1)
        record_worktree_provenance(
            tmp_path,
            "s",
            evaluate_worktree_provenance(tmp_path, "s", None, adopted=True),
        )
        record = read_worktree_provenance(tmp_path, "s")
        assert record is not None
        assert record["story_content_hash"] == story_content_hash(STORY_V1)

    def test_unreadable_record_degrades_to_unknown(self, tmp_path: Path) -> None:
        path = worktree_provenance_path(tmp_path, "s")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        prov = evaluate_worktree_provenance(tmp_path, "s", STORY_V1, adopted=True)
        assert prov.status == PROVENANCE_UNKNOWN

    def test_clear_forgets_a_removed_trees_provenance(self, tmp_path: Path) -> None:
        _record(tmp_path, "s", STORY_V1)
        clear_worktree_provenance(tmp_path, "s")
        assert read_worktree_provenance(tmp_path, "s") is None
        # Clearing twice is not an error.
        clear_worktree_provenance(tmp_path, "s")


class TestInheritedWorkNote:
    def test_only_a_superseded_tree_gets_a_note(self, tmp_path: Path) -> None:
        assert inherited_work_note(None) is None
        for status in (PROVENANCE_FRESH, PROVENANCE_MATCH, PROVENANCE_UNKNOWN):
            prov = WorktreeProvenance(status=status, adopted=status != PROVENANCE_FRESH)
            assert inherited_work_note(prov) is None

    def test_superseded_note_states_the_condition_and_what_to_do(self) -> None:
        note = inherited_work_note(
            WorktreeProvenance(
                status=PROVENANCE_CHANGED,
                recorded_hash="a" * 64,
                current_hash="b" * 64,
                adopted=True,
            )
        )
        assert note is not None
        assert "earlier attempt" in note
        assert "no longer govern" in note
        assert "handoff" in note


# ── Seam: workspace setup over real git ──────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


def _init_repo_with_worktree(tmp_path: Path, slug: str) -> tuple[Path, Path]:
    """A repo whose story worktree already holds an earlier attempt's uncommitted work."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _git(project_root, "init", "--initial-branch=main")
    _git(project_root, "config", "user.email", "test@example.com")
    _git(project_root, "config", "user.name", "Test")
    _git(project_root, "remote", "add", "origin", str(origin))
    (project_root / "shared.txt").write_text("base content\n", encoding="utf-8")
    _git(project_root, "add", "shared.txt")
    _git(project_root, "commit", "-m", "initial")
    _git(project_root, "push", "-u", "origin", "main")

    workspace_path = project_root / slug
    _git(project_root, "worktree", "add", "-b", f"feat/{slug}", str(workspace_path), "main")
    _git(workspace_path, "config", "user.email", "test@example.com")
    _git(workspace_path, "config", "user.name", "Test")
    # The earlier attempt's work, uncommitted — exactly the shape #2288 reports.
    (workspace_path / "prior_attempt.txt").write_text("superseded diagnosis\n", encoding="utf-8")

    return project_root, workspace_path


def _make_config(project_root: Path, slug: str) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=project_root,
        workspace=WorkspaceConfig(
            create_command="git worktree add -b feat/{slug} {slug} {base_branch}",
            path_pattern="{slug}",
            branch_pattern=f"feat/{slug}",
            base_branch="main",
            setup_command=None,
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(project_root: Path, slug: str) -> TaskStory:
    story = project_root / f"{slug}.md"
    story.write_text("# task\n", encoding="utf-8")
    return TaskStory(name="task", story_path=story, slug=slug)


def _create(config: ForgeConfig, task: TaskStory, story: str | None) -> tuple:
    """Run workspace setup, returning (result, logged lines)."""
    with patch("theforge.coordinator.util._log") as mock_log:
        result = _create_workspace(config, task, no_pull=True, story_content=story)
    lines = [str(call.args[0]) if call.args else "" for call in mock_log.call_args_list]
    return result, lines


class TestWorkspaceAdoptionProvenance:
    def test_first_adoption_records_the_story_text_that_now_governs(self, tmp_path: Path) -> None:
        slug = "prov-first"
        project_root, workspace_path = _init_repo_with_worktree(tmp_path, slug)
        config, task = _make_config(project_root, slug), _make_task(project_root, slug)

        (path, _branch, error), lines = _create(config, task, STORY_V1)

        assert error is None
        assert path == workspace_path
        prov = last_worktree_provenance(project_root, slug)
        assert prov is not None
        # No prior record existed, so the tree's origin cannot be asserted...
        assert prov.status == PROVENANCE_UNKNOWN
        assert any("provenance unknown" in line for line in lines)
        # ...but the text now governing is recorded for the next run to compare.
        assert prov.current_hash == story_content_hash(STORY_V1)

    def test_unchanged_story_reuses_its_worktree_exactly_as_before(self, tmp_path: Path) -> None:
        """The non-regression constraint: nothing about a matching story changes."""
        slug = "prov-match"
        project_root, workspace_path = _init_repo_with_worktree(tmp_path, slug)
        config, task = _make_config(project_root, slug), _make_task(project_root, slug)

        _create(config, task, STORY_V1)
        (path, branch, error), lines = _create(config, task, STORY_V1)

        assert error is None
        assert path == workspace_path
        assert branch == f"feat/{slug}"
        assert (workspace_path / "prior_attempt.txt").exists(), "prior work must be preserved"
        assert any("reusing existing worktree" in line for line in lines)
        assert not any("story text changed" in line for line in lines)
        prov = last_worktree_provenance(project_root, slug)
        assert prov is not None and prov.status == PROVENANCE_MATCH
        assert inherited_work_note(prov) is None

    def test_edited_story_keeps_the_tree_and_says_so(self, tmp_path: Path) -> None:
        slug = "prov-changed"
        project_root, workspace_path = _init_repo_with_worktree(tmp_path, slug)
        config, task = _make_config(project_root, slug), _make_task(project_root, slug)

        _create(config, task, STORY_V1)
        (path, _branch, error), lines = _create(config, task, STORY_V2)

        assert error is None
        assert path == workspace_path
        # Discarding the work is not automatically right — the tree survives.
        assert (workspace_path / "prior_attempt.txt").exists()
        assert any("story text changed" in line for line in lines)
        prov = last_worktree_provenance(project_root, slug)
        assert prov is not None
        assert prov.status == PROVENANCE_CHANGED
        assert prov.recorded_hash == story_content_hash(STORY_V1)
        assert prov.current_hash == story_content_hash(STORY_V2)
        assert prov.inherits_superseded_work is True

    def test_a_freshly_created_worktree_inherits_nothing(self, tmp_path: Path) -> None:
        slug = "prov-fresh"
        project_root, workspace_path = _init_repo_with_worktree(tmp_path, slug)
        config, task = _make_config(project_root, slug), _make_task(project_root, slug)
        _create(config, task, STORY_V1)

        # Remove the worktree the way an operator would, then run again.
        _git(project_root, "worktree", "remove", "--force", str(workspace_path))
        _git(project_root, "branch", "-D", f"feat/{slug}")

        (path, _branch, error), _lines = _create(config, task, STORY_V2)

        assert error is None and path is not None
        prov = last_worktree_provenance(project_root, slug)
        assert prov is not None
        assert prov.status == PROVENANCE_FRESH
        assert inherited_work_note(prov) is None

    def test_provenance_is_recorded_even_without_story_text_available(
        self, tmp_path: Path
    ) -> None:
        """A caller with no story text must not corrupt the record for the next run."""
        slug = "prov-nostory"
        project_root, _workspace_path = _init_repo_with_worktree(tmp_path, slug)
        config, task = _make_config(project_root, slug), _make_task(project_root, slug)

        _create(config, task, STORY_V1)
        (_path, _branch, error), _lines = _create(config, task, None)

        assert error is None
        record = read_worktree_provenance(project_root, slug)
        assert record is not None
        assert record["story_content_hash"] == story_content_hash(STORY_V1)


# ── Seam: the judgement reaches the agent and the audit ──────────────────


class TestInheritedWorkReachesTheDevAgent:
    def test_dev_prompt_carries_the_warning_when_a_note_is_supplied(self, tmp_path: Path) -> None:
        note = inherited_work_note(
            WorktreeProvenance(
                status=PROVENANCE_CHANGED,
                recorded_hash="a" * 64,
                current_hash="b" * 64,
                adopted=True,
            )
        )
        prompt = build_dev_prompt(
            TaskStory(name="t", slug="t"),
            workspace_path=tmp_path,
            branch_name="feat/t",
            story_content=STORY_V2,
            gate_command="make gate",
            inherited_work_note=note,
        )
        assert "## ⚠ Inherited Working Tree — Story Text Has Changed Since" in prompt
        assert "earlier attempt" in prompt

    def test_dev_prompt_says_nothing_when_there_is_nothing_to_say(self, tmp_path: Path) -> None:
        prompt = build_dev_prompt(
            TaskStory(name="t", slug="t"),
            workspace_path=tmp_path,
            branch_name="feat/t",
            story_content=STORY_V1,
            gate_command="make gate",
        )
        assert "Inherited Working Tree" not in prompt

    def test_dev_phase_injects_the_note_once_and_consumes_it(self, tmp_path: Path) -> None:
        """Seam: the WORKSPACE judgement carried on state reaches the dev prompt."""
        from unittest.mock import MagicMock

        from theforge.coordinator.dev_phase import _run_dev_phase
        from theforge.runners import AgentResult

        _git(tmp_path, "init", "--initial-branch=main")
        _git(tmp_path, "config", "user.email", "test@example.com")
        _git(tmp_path, "config", "user.name", "Test")
        (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "initial")
        _git(tmp_path, "checkout", "-q", "-b", "feat/t")

        config = _make_config(tmp_path, "t")
        task = TaskStory(name="t", slug="t")
        state = CoordinatorState()
        state.workspace_provenance_status = PROVENANCE_CHANGED
        state.workspace_inherited_work_note = inherited_work_note(
            WorktreeProvenance(
                status=PROVENANCE_CHANGED,
                recorded_hash="a" * 64,
                current_hash="b" * 64,
                adopted=True,
            )
        )
        failed = AgentResult(
            success=False,
            output="",
            session_id=None,
            cost_usd=0.0,
            exit_code=-2,
            raw={},
            profile_name="dev",
        )

        with (
            patch("theforge.coordinator.dev_phase.run_agent", return_value=failed) as mock_agent,
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
        ):
            _run_dev_phase(
                state, config, task, STORY_V2, tmp_path, "feat/t", notify=False, logger=None
            )

        prompt = mock_agent.call_args.kwargs["prompt"]
        assert "## ⚠ Inherited Working Tree — Story Text Has Changed Since" in prompt
        # Consumed: a later iteration is looking at its own work, not the inheritance.
        assert state.workspace_inherited_work_note is None


class TestProvenanceReachesTheAudit:
    def _audit(self, tmp_path: Path, state: CoordinatorState) -> dict:
        config = _make_config(tmp_path, "t")
        task = TaskStory(name="t", slug="t")
        result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")
        return generate_audit_log(config, task, result)

    def test_superseded_adoption_is_recorded(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.workspace_provenance_status = PROVENANCE_CHANGED
        state.workspace_inherited_work_note = "note"
        workspace = self._audit(tmp_path, state)["workspace"]
        assert workspace["story_provenance"] == PROVENANCE_CHANGED
        assert workspace["inherited_superseded_work"] is True

    def test_ordinary_run_records_the_judgement_it_made(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.workspace_provenance_status = PROVENANCE_MATCH
        workspace = self._audit(tmp_path, state)["workspace"]
        assert workspace["story_provenance"] == PROVENANCE_MATCH
        assert workspace["inherited_superseded_work"] is False
