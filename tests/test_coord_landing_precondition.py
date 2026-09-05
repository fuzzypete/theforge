"""Tests for the entry-time landing precondition (dirty project root).

A dirty project root makes ``_merge_branch`` refuse, but that refusal used to
arrive only after dev and review had been paid for (#2048). These tests pin the
condition being evaluated at the points where the spend can still be avoided:

- ``landing_precondition_error``: the predicate itself — only for runs that
  merge into the project-root checkout, naming the offending files
- ``run_task``: refuses in WORKSPACE, before the workspace is created and
  therefore before any dev or review agent is dispatched
- ``_run_resume_coordinator``: same refusal at the resume entry point, which is
  the first point a mid-sprint dirtying can be observed by the next story
- ``run_sprint``: refuses at sprint entry, before the base-branch pull and
  before the baseline gate
- ``_merge_branch``: still refuses on the same condition (parity — the entry
  check must not be able to drift away from the landing check)

And they pin what is *not* dirt (#2775): a sprint's own canonical run audits,
landing evidence and knowledge summaries stand uncommitted in the shared project
root between a story finishing and the next publish, which under
``max_parallel > 1`` can be the rest of the sprint. Refusing over them failed a
sprint's own stories for artifacts no operator can commit, stash or revert.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_config, _make_task
from sprint_test_helpers import run_sprint_ctx
from test_sprint_runner import _make_empty_resolved, _make_runner_config

from theforge.config import ForgeConfig
from theforge.coordinator.engine import _run_resume_coordinator, run_task
from theforge.coordinator.logging import StructuredLogger
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.coordinator.workspace import (
    _merge_branch,
    landing_blocking_dirt,
    landing_precondition_error,
    project_root_dirty_status,
    story_lands_in_project_root,
)

DIRTY = " M forge.yaml"

# The three trees a run writes into the shared checkout as it finishes — the
# exact paths quoted in #2775's refusals, keyed to one landed story's run id.
ARTIFACT_DIRT = "\n".join(
    (
        "?? .forge/audits/landing/817ecdc3d187.landed.json",
        "?? .forge/audits/runs/817ecdc3d187.json",
        "?? .forge/knowledge/summaries/817ecdc3d187.yaml",
    )
)

# A root carrying forge's bookkeeping *and* an operator edit. Still refuses.
MIXED_DIRT = f"{ARTIFACT_DIRT}\n{DIRTY}"


def _merge_config(tmp_path: Path, *, on_approve: str = "merge") -> ForgeConfig:
    """Config whose approvals land in the project-root checkout."""
    config = _make_config(tmp_path)
    (tmp_path / ".git").mkdir(exist_ok=True)
    return replace(config, workspace=replace(config.workspace, on_approve=on_approve))


def _shell(status_out: str):
    """_run_shell stub answering `git status --porcelain` with status_out."""

    def _side_effect(cmd, cwd, **kwargs):
        if "status --porcelain" in cmd:
            return (True, status_out)
        return (True, "")

    return _side_effect


def _split_shell(*, plain: str, uall: tuple[bool, str]):
    """_run_shell stub answering the plain and ``-uall`` status probes apart.

    Attribution asks with ``-uall`` because the default porcelain output
    collapses a wholly-untracked tree to ``?? .forge/``; the two answers are
    separable here so a test can make attribution fail while dirt is visible.
    """

    def _side_effect(cmd, cwd, **kwargs):
        if "status --porcelain -uall" in cmd:
            return uall
        if "status --porcelain" in cmd:
            return (True, plain)
        return (True, "")

    return _side_effect


# ── The predicate ─────────────────────────────────────────────────────


class TestLandingPreconditionError:
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_dirty_root_under_merge_names_the_file(self, mock_shell, tmp_path):
        mock_shell.side_effect = _shell(DIRTY)
        err = landing_precondition_error(_merge_config(tmp_path))
        assert err is not None
        assert "forge.yaml" in err
        assert str(tmp_path) in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_clean_root_passes(self, mock_shell, tmp_path):
        mock_shell.side_effect = _shell("")
        assert landing_precondition_error(_merge_config(tmp_path)) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_untracked_file_refused_like_merge_does(self, mock_shell, tmp_path):
        """Parity with _merge_branch, which blocks on untracked files too."""
        mock_shell.side_effect = _shell("?? scratch.txt")
        err = landing_precondition_error(_merge_config(tmp_path))
        assert err is not None
        assert "scratch.txt" in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_pr_workflow_ignores_dirty_root(self, mock_shell, tmp_path):
        """on_approve: pr never touches the project-root checkout."""
        mock_shell.side_effect = _shell(DIRTY)
        assert landing_precondition_error(_merge_config(tmp_path, on_approve="pr")) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_auto_merge_flag_forces_the_check(self, mock_shell, tmp_path):
        """--auto-merge forces "merge" regardless of configuration."""
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="pr")
        assert landing_precondition_error(config, auto_merge=True) is not None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_non_git_project_root_passes(self, mock_shell, tmp_path):
        mock_shell.side_effect = _shell(DIRTY)
        config = _make_config(tmp_path)
        config = replace(config, workspace=replace(config.workspace, on_approve="merge"))
        assert landing_precondition_error(config) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_unreadable_status_fails_open(self, mock_shell, tmp_path):
        """A root we cannot inspect is not evidence of dirt."""
        mock_shell.return_value = (False, "fatal: not a git repository")
        assert project_root_dirty_status(tmp_path) == ""
        assert landing_precondition_error(_merge_config(tmp_path)) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_caller_supplied_obligation_overrides_the_derivation(self, mock_shell, tmp_path):
        """The scheduler's answer wins over auto_merge + on_approve.

        A parallel dependency parent has auto_merge=False and on_approve="none"
        yet is forced into a local merge after it returns, so the derivation
        alone would let it spend before the refusal.
        """
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="none")

        assert story_lands_in_project_root(config) is False
        assert story_lands_in_project_root(config, lands_in_project_root=True) is True
        assert landing_precondition_error(config) is None
        assert landing_precondition_error(config, lands_in_project_root=True) is not None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_caller_can_waive_a_derived_obligation(self, mock_shell, tmp_path):
        """An explicit False also wins — the override is not an OR."""
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="merge")
        assert landing_precondition_error(config, lands_in_project_root=False) is None


# ── Forge's own bookkeeping is not dirt (#2775) ───────────────────────


class TestStoryRunArtifactDirt:
    """A sprint's own pending artifacts must not refuse that sprint's stories."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_artifact_only_dirt_does_not_block(self, mock_shell, tmp_path):
        mock_shell.side_effect = _shell(ARTIFACT_DIRT)
        assert landing_blocking_dirt(tmp_path) == ""
        assert landing_precondition_error(_merge_config(tmp_path)) is None

    @pytest.mark.parametrize(
        "artifact_path",
        [
            "?? .forge/audits/runs/817ecdc3d187.json",
            "?? .forge/audits/landing/817ecdc3d187.landed.json",
            "?? .forge/knowledge/summaries/817ecdc3d187.yaml",
        ],
    )
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_each_artifact_tree_is_excluded(self, mock_shell, tmp_path, artifact_path):
        """Every tree named in the refusals, not just the set together."""
        mock_shell.side_effect = _shell(artifact_path)
        assert landing_precondition_error(_merge_config(tmp_path)) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_collapsed_untracked_forge_tree_is_still_attributed(self, mock_shell, tmp_path):
        """A first-ever sprint's ``?? .forge/`` expands before it is attributed.

        Plain porcelain collapses a wholly-untracked tree to its top directory,
        which names something broader than the artifact trees; the ``-uall``
        probe is what lets that root be excused rather than refused.
        """
        mock_shell.side_effect = _split_shell(plain="?? .forge/", uall=(True, ARTIFACT_DIRT))
        assert landing_precondition_error(_merge_config(tmp_path)) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_operator_dirt_alongside_artifacts_still_refuses(self, mock_shell, tmp_path):
        """The exclusion is not a blanket waiver — operator dirt is still theirs."""
        mock_shell.side_effect = _shell(MIXED_DIRT)
        err = landing_precondition_error(_merge_config(tmp_path))
        assert err is not None
        assert "forge.yaml" in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_unattributable_status_fails_closed(self, mock_shell, tmp_path):
        """A root git cannot describe in full is not a root to excuse dirt in."""
        mock_shell.side_effect = _split_shell(
            plain=ARTIFACT_DIRT, uall=(False, "fatal: not a git repository")
        )
        assert landing_blocking_dirt(tmp_path) == ARTIFACT_DIRT
        assert landing_precondition_error(_merge_config(tmp_path)) is not None

    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_merge_branch_tolerates_the_same_dirt(self, mock_shell, _mock_log, tmp_path):
        """Parity in the new direction: what entry excuses, landing must too.

        Otherwise the entry check stops being a cheaper form of the landing
        answer and becomes an independent way to fail — here in reverse, paying
        for dev and review and then refusing at the merge anyway.
        """

        def _side_effect(cmd, cwd, **kwargs):
            if "branch --list" in cmd:
                return (True, "  main\n")
            if "status --porcelain" in cmd:
                return (True, ARTIFACT_DIRT)
            if "log main..forge/x" in cmd:
                return (True, "abc1234 work\n")
            return (True, "")

        mock_shell.side_effect = _side_effect

        info = _merge_branch(tmp_path, "main", "forge/x", "x", tmp_path / "wt")

        assert info["error"] is None
        assert info["merged"] is True


# ── Parity with the landing-time refusal ──────────────────────────────


class TestMergeBranchParity:
    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_merge_branch_still_refuses_a_dirty_root(self, mock_shell, _mock_log, tmp_path):
        def _side_effect(cmd, cwd, **kwargs):
            if "branch --list" in cmd:
                return (True, "  main\n")
            if "status --porcelain" in cmd:
                return (True, DIRTY)
            return (True, "")

        mock_shell.side_effect = _side_effect

        info = _merge_branch(tmp_path, "main", "forge/x", "x", tmp_path / "wt")

        assert info["merged"] is False
        assert "Uncommitted changes in project root" in info["error"]
        assert "forge.yaml" in info["error"]


# ── run_task: refuse before the workspace exists ──────────────────────


class TestRunTaskEntry:
    @patch("theforge.coordinator.engine._create_workspace")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_dirty_root_escalates_before_workspace_creation(
        self, mock_shell, mock_create, tmp_path
    ):
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)

        result = run_task(config, task)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert "forge.yaml" in result.message
        mock_create.assert_not_called()

    @patch("theforge.coordinator.engine._create_workspace")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_scheduler_obligation_escalates_under_on_approve_none(
        self, mock_shell, mock_create, tmp_path
    ):
        """A parallel dependency parent refuses before spend.

        auto_merge is False and on_approve is "none", so only the scheduler's
        obligation reveals that this story will be forced into a local merge.
        """
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="none")
        task = _make_task(tmp_path)

        result = run_task(config, task, auto_merge=False, lands_in_project_root=True)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert "forge.yaml" in result.message
        mock_create.assert_not_called()

    @patch("theforge.coordinator.engine._create_workspace")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_clean_root_proceeds_to_workspace(self, mock_shell, mock_create, tmp_path):
        mock_shell.side_effect = _shell("")
        mock_create.return_value = (None, None, "stop here")
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)

        result = run_task(config, task)

        mock_create.assert_called_once()
        assert "stop here" in result.message

    @patch("theforge.coordinator.engine._create_workspace")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_sibling_run_artifacts_do_not_escalate_at_workspace(
        self, mock_shell, mock_create, tmp_path
    ):
        """The #2775 symptom, at the site that produced it.

        A sibling story landed seconds ago and its own run record, landing
        evidence and knowledge summary stand uncommitted in the shared root.
        This story has nothing to do with them and must reach its workspace.
        """
        mock_shell.side_effect = _shell(ARTIFACT_DIRT)
        mock_create.return_value = (None, None, "stop here")
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)

        result = run_task(config, task)

        mock_create.assert_called_once()
        assert "LANDING PRECONDITION" not in (result.message or "")


# ── Resume entry: same refusal before re-running dev/review ───────────


class TestResumeEntry:
    @patch("theforge.coordinator.engine._setup_resume_entry")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_dirty_root_escalates_at_resume_entry(self, mock_shell, mock_setup, tmp_path):
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir(exist_ok=True)

        state = CoordinatorState(phase=Phase.REVIEW)
        logger = StructuredLogger(
            run_id="r1",
            project="test",
            task=task.slug,
            log_file=None,
            enabled=False,
            project_root=tmp_path,
        )
        mock_setup.return_value = (state, logger, "forge/test-task", "story", 0.0)

        result = _run_resume_coordinator(
            config,
            task,
            workspace,
            initial_phase=Phase.REVIEW,
            skip_dev_first_iter=True,
            interactive=False,
            auto_merge=False,
            notify=False,
            run_id="r1",
            sprint_name=None,
            state_update_fn=None,
            no_pull=True,
        )

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert "forge.yaml" in result.message

    @patch("theforge.coordinator.engine._make_story_log_dir")
    @patch("theforge.coordinator.engine._setup_resume_entry")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_sibling_run_artifacts_do_not_escalate_at_resume(
        self, mock_shell, mock_setup, mock_log_dir, tmp_path
    ):
        """The resume entry point carries the same exclusion (#2775).

        A resumed story is refused by the identical call; artifacts left
        standing by a run that has already finished refuse it on the same terms
        as a live sibling's, which is why serializing readers alone would not
        close this.
        """
        mock_shell.side_effect = _shell(ARTIFACT_DIRT)
        mock_log_dir.side_effect = RuntimeError("past the landing check")
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir(exist_ok=True)

        state = CoordinatorState(phase=Phase.REVIEW)
        logger = StructuredLogger(
            run_id="r1",
            project="test",
            task=task.slug,
            log_file=None,
            enabled=False,
            project_root=tmp_path,
        )
        mock_setup.return_value = (state, logger, "forge/test-task", "story", 0.0)

        with pytest.raises(RuntimeError, match="past the landing check"):
            _run_resume_coordinator(
                config,
                task,
                workspace,
                initial_phase=Phase.REVIEW,
                skip_dev_first_iter=True,
                interactive=False,
                auto_merge=False,
                notify=False,
                run_id="r1",
                sprint_name=None,
                state_update_fn=None,
                no_pull=True,
            )


# ── Sprint entry: refuse before pull and before the baseline gate ─────


def _sprint_config(tmp_path: Path) -> ForgeConfig:
    config = _make_runner_config(tmp_path)
    (tmp_path / ".git").mkdir(exist_ok=True)
    return replace(config, workspace=replace(config.workspace, on_approve="merge"))


def test_run_sprint_dirty_root_aborts_before_pull_and_baseline(tmp_path: Path) -> None:
    config = _sprint_config(tmp_path)
    resolved = _make_empty_resolved()

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=_shell(DIRTY)),
        patch("theforge.coordinator.workspace.pull_base_branch") as mock_pull,
        patch("theforge.sprint.runner._run_baseline_gate") as mock_baseline,
    ):
        with pytest.raises(RuntimeError, match="forge.yaml"):
            run_sprint_ctx(config, resolved)

    mock_pull.assert_not_called()
    mock_baseline.assert_not_called()


@pytest.mark.parametrize("root_status", ["", ARTIFACT_DIRT], ids=["clean", "leftover-artifacts"])
def test_run_sprint_clean_root_proceeds(tmp_path: Path, root_status: str) -> None:
    """The entry check must not block a sprint whose project root is clean.

    ``leftover-artifacts`` is the same requirement one step further out (#2775):
    a prior run's own bookkeeping, still untracked at rest when nothing is
    running, refused the *first* story of the next sprint before it started.
    Excluding it is what makes admissibility independent of what an earlier run
    happened to leave behind.
    """
    config = _sprint_config(tmp_path)
    resolved = _make_empty_resolved()

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=_shell(root_status)),
        patch("theforge.coordinator.workspace.pull_base_branch") as mock_pull,
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=set()),
        patch(
            "theforge.sprint.runner.normalize_dependency_plan",
            return_value=SimpleNamespace(tasks=[], blocked={}),
        ),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            side_effect=RuntimeError("stop after baseline"),
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint_ctx(config, resolved)

    mock_pull.assert_called_once()


# ── Scheduler seam: the per-story landing obligation reaches the worker ──


class _StopDispatch(RuntimeError):
    """Raised from the fake executor to end the sprint at first submit."""


def _capturing_executor(captured: dict):
    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args, **kwargs):
            # _run_single_story positionals: config, task, triage, run_id,
            # sprint_name, interactive, notify, resume, effective_auto_merge, ...
            captured[args[1].slug] = {
                "lands_in_project_root": kwargs.get("lands_in_project_root"),
                "effective_auto_merge": args[8],
            }
            raise _StopDispatch(args[1].slug)

    return _FakeExecutor


def test_parallel_dependency_parent_dispatched_with_landing_obligation(tmp_path: Path) -> None:
    """A parallel dependency parent must reach its worker knowing it lands locally.

    on_approve is "none" and --auto-merge is off, so ``effective_am`` is False
    and the story's own flags reveal no landing. The scheduler nevertheless
    rewrites its result to a local merge once it returns, so without the
    threaded obligation the dirty-root refusal would land after dev and review
    (#2048 review iteration 1).
    """
    from tests.test_sprint_resume import _make_config as _make_sprint_config
    from tests.test_sprint_resume import _make_spec_file

    config = _make_sprint_config(tmp_path)
    assert config.workspace.on_approve == "none"

    parent = _make_spec_file(tmp_path, "Parent", "parent")
    child = _make_spec_file(tmp_path, "Child", "child", depends_on=["parent"])
    manifest = tmp_path / "sprint.yaml"
    manifest.write_text(
        "name: Test Sprint\nbudget_usd: 10.0\nmax_parallel: 2\nspecs:\n"
        f"  - {parent.name}\n  - {child.name}\n",
        encoding="utf-8",
    )

    captured: dict[str, bool | None] = {}

    with (
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _capturing_executor(captured)),
    ):
        with pytest.raises(_StopDispatch):
            run_sprint_ctx(config, manifest)

    parent_kwargs = captured.get("parent")
    assert parent_kwargs is not None, "dependency parent was never dispatched"
    # Proves the parallel path: the story's own auto-merge flag says no landing.
    assert parent_kwargs["effective_auto_merge"] is False
    assert parent_kwargs["lands_in_project_root"] is True, (
        "dependency parent dispatched without its scheduler-forced landing obligation"
    )


# ── Sprint entry: the obligation follows in-manifest dependency parents ──


def _dependency_sprint(
    tmp_path: Path,
    *,
    on_approve: str,
    dep: str,
    in_manifest_parent: bool,
    max_parallel: int = 1,
) -> tuple[ForgeConfig, Path]:
    """A dirty-rooted sprint whose only dependency edge is `dep`."""
    from tests.test_sprint_resume import _make_config as _make_sprint_config
    from tests.test_sprint_resume import _make_spec_file

    config = _make_sprint_config(tmp_path)
    config = replace(config, workspace=replace(config.workspace, on_approve=on_approve))
    (tmp_path / ".git").mkdir(exist_ok=True)

    specs = []
    if in_manifest_parent:
        specs.append(_make_spec_file(tmp_path, "Parent", dep).name)
    specs.append(_make_spec_file(tmp_path, "Child", "child", depends_on=[dep]).name)

    manifest = tmp_path / "sprint.yaml"
    manifest.write_text(
        f"name: Test Sprint\nbudget_usd: 10.0\nmax_parallel: {max_parallel}\nspecs:\n"
        + "".join(f"  - {s}\n" for s in specs),
        encoding="utf-8",
    )
    return config, manifest


def _entry_patches(satisfied: set[str]):
    return (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=_shell(DIRTY)),
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=satisfied),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            side_effect=RuntimeError("stop after baseline"),
        ),
    )


def test_external_satisfied_dependency_does_not_impose_a_landing_obligation(
    tmp_path: Path,
) -> None:
    """A PR-landing sprint whose only edge points outside the manifest must run.

    The dependency target is an already-satisfied external issue: no story here
    merges into the project-root checkout, so its dirtiness is not forge's
    business and refusing would block a workflow that cannot hit the landing
    failure (#2048 review iteration 2).
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="pr", dep="external-issue", in_manifest_parent=False
    )

    with ExitStack() as stack:
        for cm in _entry_patches({"external-issue"}):
            stack.enter_context(cm)
        # Reaching the baseline gate proves the entry check did not refuse.
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint_ctx(config, manifest)


def test_in_manifest_dependency_parent_still_imposes_a_landing_obligation(
    tmp_path: Path,
) -> None:
    """A parent carried by this sprint is merged locally to unblock its child.

    on_approve is "pr" and --auto-merge is off, so only the dependency edge
    reveals the landing — the sprint must still refuse a dirty root, and must
    do so before batch preflight, the first agent spend.
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="pr", dep="parent", in_manifest_parent=True
    )

    with ExitStack() as stack:
        mocks = [stack.enter_context(cm) for cm in _entry_patches(set())]
        mock_preflight = mocks[-1]
        with pytest.raises(RuntimeError, match="forge.yaml"):
            run_sprint_ctx(config, manifest)

    mock_preflight.assert_not_called()


def test_satisfied_in_manifest_parent_imposes_no_landing_obligation(tmp_path: Path) -> None:
    """A parent already merged is never dispatched, so it owes no landing.

    Same shape as the test above — an in-manifest dependency parent under
    on_approve "pr" — except dependency resolution reports it satisfied. Nothing
    in this sprint can merge into the project-root checkout, so the dirty root
    must not refuse it (#2048 review iteration 3). Sprint entry cannot know
    this, which is why the dependency-derived term is asserted only after the
    satisfied and resume-triage sets are resolved.
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="pr", dep="parent", in_manifest_parent=True
    )

    with ExitStack() as stack:
        for cm in _entry_patches({"parent"}):
            stack.enter_context(cm)
        # Reaching batch preflight proves neither pass refused.
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint_ctx(config, manifest)


def test_parallel_merge_pr_dependency_parent_imposes_no_landing_obligation(
    tmp_path: Path,
) -> None:
    """A parallel merge-pr parent lands through its own PR, not the local checkout.

    ``_finalize_approve`` gives a merge-pr story landing_status
    "pending_integration" with action "merge-pr", so the scheduler's
    pending_integration conversion — which only fires for a story that produced
    no landing of its own — never rewrites it to a local merge. Nothing here
    touches the project root, so a dirty one must not refuse the sprint
    (#2048 review iteration 4).
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="merge-pr", dep="parent", in_manifest_parent=True, max_parallel=2
    )

    with ExitStack() as stack:
        for cm in _entry_patches(set()):
            stack.enter_context(cm)
        # Reaching batch preflight proves neither pass refused.
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint_ctx(config, manifest)


def test_sequential_merge_pr_dependency_parent_still_imposes_a_landing_obligation(
    tmp_path: Path,
) -> None:
    """Sequential mode eager-merges the parent, so merge-pr is not a blanket waiver.

    ``effective_am`` is True for a dependency parent in sequential mode, which
    forces effective on_approve to "merge" — a real local merge whatever the
    configured landing path says.
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="merge-pr", dep="parent", in_manifest_parent=True, max_parallel=1
    )

    with ExitStack() as stack:
        mocks = [stack.enter_context(cm) for cm in _entry_patches(set())]
        with pytest.raises(RuntimeError, match="forge.yaml"):
            run_sprint_ctx(config, manifest)

    mocks[-1].assert_not_called()


def test_parallel_pr_dependency_parent_still_imposes_a_landing_obligation(
    tmp_path: Path,
) -> None:
    """The merge-pr carve-out must not swallow the parallel "pr" case.

    A "pr" story leaves landing_status None, so the scheduler's conversion does
    rewrite the parent to a local merge — the very path review iteration 1 found.
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="pr", dep="parent", in_manifest_parent=True, max_parallel=2
    )

    with ExitStack() as stack:
        mocks = [stack.enter_context(cm) for cm in _entry_patches(set())]
        with pytest.raises(RuntimeError, match="forge.yaml"):
            run_sprint_ctx(config, manifest)

    mocks[-1].assert_not_called()


def test_parallel_merge_pr_parent_dispatched_without_landing_obligation(tmp_path: Path) -> None:
    """The worker-side obligation carries the same merge-pr carve-out.

    Mirror of test_parallel_dependency_parent_dispatched_with_landing_obligation:
    same parallel dependency parent, but under merge-pr it does not land in the
    project root, so the worker must not be told it does — otherwise a dirty
    root would escalate a story that could have run.
    """
    from tests.test_sprint_resume import _make_config as _make_sprint_config
    from tests.test_sprint_resume import _make_spec_file

    config = _make_sprint_config(tmp_path)
    config = replace(config, workspace=replace(config.workspace, on_approve="merge-pr"))

    parent = _make_spec_file(tmp_path, "Parent", "parent")
    child = _make_spec_file(tmp_path, "Child", "child", depends_on=["parent"])
    manifest = tmp_path / "sprint.yaml"
    manifest.write_text(
        "name: Test Sprint\nbudget_usd: 10.0\nmax_parallel: 2\nspecs:\n"
        f"  - {parent.name}\n  - {child.name}\n",
        encoding="utf-8",
    )

    captured: dict[str, dict] = {}

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _capturing_executor(captured)),
    ):
        with pytest.raises(_StopDispatch):
            run_sprint_ctx(config, manifest)

    parent_kwargs = captured.get("parent")
    assert parent_kwargs is not None, "dependency parent was never dispatched"
    assert parent_kwargs["effective_auto_merge"] is False
    assert parent_kwargs["lands_in_project_root"] is False, (
        "merge-pr parent told it lands in the project root, which it does not"
    )


def test_auto_merge_refuses_in_sequential_mode(tmp_path: Path) -> None:
    """--auto-merge reaches the worker in sequential mode and forces a local merge."""
    config, manifest = _dependency_sprint(
        tmp_path,
        on_approve="pr",
        dep="external-issue",
        in_manifest_parent=False,
        max_parallel=1,
    )

    with ExitStack() as stack:
        mocks = [stack.enter_context(cm) for cm in _entry_patches({"external-issue"})]
        with pytest.raises(RuntimeError, match="forge.yaml"):
            run_sprint_ctx(config, manifest, auto_merge=True)

    mocks[-1].assert_not_called()


def test_auto_merge_in_parallel_mode_imposes_no_landing_obligation(tmp_path: Path) -> None:
    """--auto-merge is dropped in parallel mode, so it cannot make a story land.

    ``effective_am`` is hard-False when max_parallel > 1, so the flag never
    reaches ``_finalize_approve`` and the configured "pr" landing stands. The
    dirty root must not refuse a sprint whose merges will not happen.
    """
    config, manifest = _dependency_sprint(
        tmp_path,
        on_approve="pr",
        dep="external-issue",
        in_manifest_parent=False,
        max_parallel=2,
    )

    with ExitStack() as stack:
        for cm in _entry_patches({"external-issue"}):
            stack.enter_context(cm)
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint_ctx(config, manifest, auto_merge=True)
