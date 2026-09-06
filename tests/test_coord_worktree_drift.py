"""Tests for issue #1993: preserved worktrees that go stale across a merge.

Forge preserves an escalated story's worktree on purpose. Nothing reconciles it
against a base branch that advances afterwards, so the next resume rebases it
onto a base that may have rewritten the same files. That condition used to
surface as raw ``git rebase`` stderr — including git's own "resolve manually /
git rebase --continue" hints, which are not a supported operator action inside a
story worktree.

These tests pin the classification at each surface it crosses:

- ``classify_rebase_conflict`` / ``collect_drift``: the classification itself,
  against real git repositories — names the overlapping files and the
  base-branch commits responsible, and drops git's hints
- ``_create_workspace``: the reuse path returns the classification instead of
  the relayed tool failure, and still returns the relayed failure when drift
  cannot be established
- ``run_task``: the WORKSPACE→ESCALATE seam forwards a classification verbatim
  rather than re-framing it as "Workspace creation failed"
- ``run_batch_preflight``: the operator-facing sprint log renders it line by
  line instead of burying it inside one WARNING line

Issue #2908 is the neighbouring failure at the same seam: a workspace condition
the result object carried but nothing wrote to the operator's console, so the
story just failed with no stated reason. Those tests capture the log stream and
assert the ESCALATE line names the reason — at each entry point that reaches an
ESCALATE before any agent runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task, patch_gate_shell

from theforge.artifacts import ESCALATED_MARKER_PATH
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
from theforge.coordinator.engine import (
    run_from_dev,
    run_from_review,
    run_review_only,
    run_task,
)
from theforge.coordinator.state import Phase
from theforge.coordinator.workspace import _create_workspace
from theforge.coordinator.worktree_drift import (
    DRIFT_HEADER,
    classify_rebase_conflict,
    collect_drift,
    is_drift_classification,
)
from theforge.task import TaskStory

REBASE_STDERR = (
    "error: could not apply 231964a... fix(gate): align story gate with ci python matrix\n"
    "hint: Resolve all conflicts manually, mark them as resolved with\n"
    'hint: "git add/rm <conflicted_files>", then run "git rebase --continue".\n'
    'hint: You can instead skip this commit: run "git rebase --skip".\n'
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _drifted_repo(tmp_path: Path, slug: str, *, escalated: bool) -> tuple[Path, Path, str]:
    """Origin + clone + a story worktree whose commits conflict with later base commits.

    Mirrors #1945: the story worktree holds commits touching several files, then
    base-branch commits land that rewrite the same files.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _git(project_root, "init", "--initial-branch=main")
    _git(project_root, "config", "user.email", "test@example.com")
    _git(project_root, "config", "user.name", "Test")
    _git(project_root, "remote", "add", "origin", str(origin))

    for name in ("gate.py", "types.py", "untouched.py"):
        _write(project_root / "src" / name, f"# base {name}\n")
    _git(project_root, "add", "src")
    _git(project_root, "commit", "-m", "initial")
    _git(project_root, "push", "-u", "origin", "main")

    branch = f"feat/{slug}"
    workspace_path = project_root / slug
    _git(project_root, "worktree", "add", "-b", branch, str(workspace_path), "main")
    _git(workspace_path, "config", "user.email", "test@example.com")
    _git(workspace_path, "config", "user.name", "Test")

    # Story work: two commits over gate.py and types.py.
    _write(workspace_path / "src" / "gate.py", "# story gate change\n")
    _git(workspace_path, "add", "src/gate.py")
    _git(workspace_path, "commit", "-m", "story: rework gate")
    _write(workspace_path / "src" / "types.py", "# story types change\n")
    _git(workspace_path, "add", "src/types.py")
    _git(workspace_path, "commit", "-m", "story: rework types")

    if escalated:
        marker = workspace_path / ESCALATED_MARKER_PATH
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("escalated\n", encoding="utf-8")

    # Intervening merge on the base: one commit rewrites the same two files,
    # another touches a file the story never saw.
    _write(project_root / "src" / "gate.py", "# base gate rewrite\n")
    _write(project_root / "src" / "types.py", "# base types rewrite\n")
    _git(project_root, "add", "src")
    _git(project_root, "commit", "-m", "fix(gate): align story gate with ci python matrix")
    _write(project_root / "src" / "untouched.py", "# unrelated base change\n")
    _git(project_root, "add", "src")
    _git(project_root, "commit", "-m", "chore: unrelated change")
    _git(project_root, "push", "origin", "main")

    return project_root, workspace_path, branch


def _repo_with_residue(tmp_path: Path, slug: str) -> tuple[Path, Path]:
    """A real repo whose story worktree path holds a directory git never registered.

    Mirrors #2908: an operator (or a crashed run) leaves non-Forge contents at the
    managed worktree path. ``git worktree list`` does not name it, and because the
    contents are not Forge-owned they are preserved rather than deleted — so the
    workspace phase has nothing it can safely reuse or remove.
    """
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _git(project_root, "init", "--initial-branch=main")
    _git(project_root, "config", "user.email", "test@example.com")
    _git(project_root, "config", "user.name", "Test")
    _write(project_root / "src" / "app.py", "# base\n")
    _git(project_root, "add", "src")
    _git(project_root, "commit", "-m", "initial")

    residue = project_root / slug
    _write(residue / "leftover.txt", "hand-written contents forge never produced\n")

    return project_root, residue


def _real_config(project_root: Path, slug: str) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=project_root,
        workspace=WorkspaceConfig(
            create_command="git worktree add {slug} {base_branch}",
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


def _real_task(slug: str, project_root: Path) -> TaskStory:
    spec = project_root / f"{slug}.md"
    spec.write_text("# task\n", encoding="utf-8")
    return TaskStory(name="task", story_path=spec, slug=slug)


# ── The classification itself ─────────────────────────────────────────


class TestCollectDrift:
    def test_names_overlapping_files_and_responsible_commits(self, tmp_path):
        _root, workspace_path, _branch = _drifted_repo(tmp_path, "drift", escalated=True)
        _git(workspace_path, "fetch", "origin", "main")

        drift = collect_drift(workspace_path, "main")

        assert drift.overlapping_files == ["src/gate.py", "src/types.py"]
        assert drift.worktree_commits == 2
        assert drift.base_commits == 2
        assert drift.escalated is True
        assert drift.base_ref == "origin/main"
        subjects = " ".join(drift.responsible_commits)
        assert "align story gate with ci python matrix" in subjects
        # The base commit that touched only untouched.py is not responsible.
        assert "unrelated change" not in subjects

    def test_file_the_story_never_touched_is_not_reported(self, tmp_path):
        _root, workspace_path, _branch = _drifted_repo(tmp_path, "drift2", escalated=True)
        _git(workspace_path, "fetch", "origin", "main")

        drift = collect_drift(workspace_path, "main")

        assert "src/untouched.py" not in drift.overlapping_files

    def test_marker_absence_is_recorded(self, tmp_path):
        _root, workspace_path, _branch = _drifted_repo(tmp_path, "drift3", escalated=False)
        _git(workspace_path, "fetch", "origin", "main")

        assert collect_drift(workspace_path, "main").escalated is False


class TestClassifyRebaseConflict:
    def test_message_states_condition_files_commits_and_resolutions(self, tmp_path):
        _root, workspace_path, _branch = _drifted_repo(tmp_path, "classify", escalated=True)
        _git(workspace_path, "fetch", "origin", "main")

        message = classify_rebase_conflict(workspace_path, "main", REBASE_STDERR)

        assert message is not None
        assert is_drift_classification(message)
        assert message.startswith(DRIFT_HEADER)
        assert "src/gate.py" in message
        assert "src/types.py" in message
        assert "align story gate with ci python matrix" in message
        assert "Available resolutions:" in message
        assert str(workspace_path) in message

    def test_git_hint_lines_are_dropped(self, tmp_path):
        _root, workspace_path, _branch = _drifted_repo(tmp_path, "hints", escalated=True)
        _git(workspace_path, "fetch", "origin", "main")

        message = classify_rebase_conflict(workspace_path, "main", REBASE_STDERR)

        assert message is not None
        assert "hint:" not in message
        assert "git add/rm" not in message
        # git's factual first line may still be quoted for reference, but it is
        # no longer the entirety of what the operator is shown.
        assert message.splitlines()[-1].startswith("git reported:")
        assert len(message.splitlines()) > 10

    def test_returns_none_when_no_overlap_can_be_established(self, tmp_path):
        """A rebase can fail for reasons unrelated to drift — do not mislabel those."""
        project_root = tmp_path / "repo"
        project_root.mkdir()
        _git(project_root, "init", "--initial-branch=main")
        _git(project_root, "config", "user.email", "test@example.com")
        _git(project_root, "config", "user.name", "Test")
        _write(project_root / "a.txt", "a\n")
        _git(project_root, "add", "a.txt")
        _git(project_root, "commit", "-m", "initial")

        assert classify_rebase_conflict(project_root, "main", "fatal: unable to access") is None

    def test_no_git_repository_degrades_to_none(self, tmp_path):
        assert classify_rebase_conflict(tmp_path, "main", "boom") is None


# ── The workspace reuse path ──────────────────────────────────────────


class TestCreateWorkspaceReuse:
    def test_preserved_worktree_reports_classification_not_git_output(self, tmp_path):
        slug = "reuse-drift"
        project_root, workspace_path, _branch = _drifted_repo(tmp_path, slug, escalated=True)
        config = _real_config(project_root, slug)
        task = _real_task(slug, project_root)

        path, branch_name, error = _create_workspace(config, task, no_pull=True)

        assert path is None
        assert branch_name is None
        assert error is not None
        assert is_drift_classification(error)
        assert "src/gate.py" in error
        assert "src/types.py" in error
        assert "align story gate with ci python matrix" in error
        assert "hint:" not in error
        # The worktree survives the diagnosis — nothing is left mid-rebase.
        assert workspace_path.exists()
        assert not (workspace_path / ".git" / "rebase-merge").exists()
        assert not (workspace_path / ".git" / "rebase-apply").exists()
        assert (workspace_path / ESCALATED_MARKER_PATH).exists()

    def test_unclassifiable_rebase_failure_still_relays_the_tool_error(self, tmp_path):
        slug = "reuse-opaque"
        project_root, _workspace_path, _branch = _drifted_repo(tmp_path, slug, escalated=True)
        config = _real_config(project_root, slug)
        task = _real_task(slug, project_root)

        with patch("theforge.coordinator.workspace.classify_rebase_conflict", return_value=None):
            _path, _branch_name, error = _create_workspace(config, task, no_pull=True)

        assert error is not None
        assert not is_drift_classification(error)
        assert "pre-dev rebase onto main failed" in error


# ── The WORKSPACE → ESCALATE seam ─────────────────────────────────────


class TestRunTaskSeam:
    @patch("theforge.coordinator.engine._create_workspace")
    def test_classification_reaches_the_result_unwrapped(self, mock_create, tmp_path):
        classification = f"{DRIFT_HEADER} on release/v0.13\n\nsrc/foo.py"
        mock_create.return_value = (None, None, classification)
        config = _make_config(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)

        result = run_task(config, _make_task(tmp_path))

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert result.message == classification
        assert "Workspace creation failed" not in result.message
        assert result.state.error == classification

    @patch("theforge.coordinator.engine._create_workspace")
    def test_unclassified_workspace_error_keeps_its_prefix(self, mock_create, tmp_path):
        mock_create.return_value = (None, None, "Failed to create workspace: boom")
        config = _make_config(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)

        result = run_task(config, _make_task(tmp_path))

        assert result.message.startswith("Workspace creation failed:")

    def test_unregistered_residue_states_its_reason_on_the_operator_log(self, tmp_path):
        """#2908: the story used to fail with no reason on the console.

        The result object carried the error, but nothing wrote it to the stream
        the operator actually watches, so a sprint reported a failed story and
        said nothing about why. Driven end to end from a real repo so the
        residue condition is produced by the workspace phase, not asserted from
        a mock of it.
        """
        slug = "residue-story"
        project_root, residue = _repo_with_residue(tmp_path, slug)
        config = _real_config(project_root, slug)
        task = _real_task(slug, project_root)

        logged: list[str] = []
        with patch("theforge.coordinator.engine._log", side_effect=logged.append):
            result = run_task(config, task, no_pull=True, lands_in_project_root=False)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert "does not register it as a worktree" in result.state.error
        # The residue is preserved, not deleted — it is not Forge-owned.
        assert (residue / "leftover.txt").exists()

        escalate_lines = [line for line in logged if "ESCALATE" in line]
        assert escalate_lines, f"no ESCALATE line on the operator log: {logged}"
        assert result.state.error in escalate_lines[0]
        assert str(residue) in escalate_lines[0]


# ── The resume rebase seam ────────────────────────────────────────────


def _resume_shell(cmd: str, cwd, **kwargs):
    if "git rev-parse --abbrev-ref HEAD" in cmd:
        return (True, "forge/test-task", 0, False)
    return (True, "", 0, False)


class TestResumeRebaseSeam:
    @patch("theforge.coordinator.engine.classify_rebase_conflict")
    @patch("theforge.coordinator.engine._rebase_onto_main", return_value=(False, REBASE_STDERR))
    @patch_gate_shell(side_effect=_resume_shell)
    def test_run_from_review_forwards_classification(
        self, _mock_shell, _mock_rebase, mock_classify, tmp_path
    ):
        classification = f"{DRIFT_HEADER} on main\n\nFiles changed on both sides:\n  - src/foo.py"
        mock_classify.return_value = classification
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        result = run_from_review(config, task, workspace, no_pull=True)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert result.message == classification
        assert result.state.error == classification
        mock_classify.assert_called_once_with(
            workspace,
            config.workspace.base_branch,
            REBASE_STDERR,
        )

    @patch("theforge.coordinator.engine.classify_rebase_conflict")
    @patch("theforge.coordinator.engine._rebase_onto_main", return_value=(False, REBASE_STDERR))
    @patch_gate_shell(side_effect=_resume_shell)
    def test_run_from_dev_forwards_classification(
        self, _mock_shell, _mock_rebase, mock_classify, tmp_path
    ):
        classification = f"{DRIFT_HEADER} on main\n\nFiles changed on both sides:\n  - src/foo.py"
        mock_classify.return_value = classification
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        result = run_from_dev(config, task, workspace, no_pull=True)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert result.message == classification
        assert result.state.error == classification
        mock_classify.assert_called_once_with(
            workspace,
            config.workspace.base_branch,
            REBASE_STDERR,
        )

    @patch("theforge.coordinator.engine.classify_rebase_conflict", return_value=None)
    @patch("theforge.coordinator.engine._rebase_onto_main", return_value=(False, REBASE_STDERR))
    @patch_gate_shell(side_effect=_resume_shell)
    def test_resume_rebase_falls_back_to_raw_error_when_unclassifiable(
        self, _mock_shell, _mock_rebase, _mock_classify, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        result = run_from_review(config, task, workspace, no_pull=True)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert result.message.startswith("pre-dev rebase onto main failed")
        assert not is_drift_classification(result.message)

    @patch("theforge.coordinator.engine.classify_rebase_conflict", return_value=None)
    @patch("theforge.coordinator.engine._rebase_onto_main", return_value=(False, REBASE_STDERR))
    @patch_gate_shell(side_effect=_resume_shell)
    def test_resume_rebase_failure_states_its_reason_on_the_operator_log(
        self, _mock_shell, _mock_rebase, _mock_classify, tmp_path
    ):
        """#2908, same shape on the resume path: the audit had it, the console did not."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        logged: list[str] = []
        with patch("theforge.coordinator.engine._log", side_effect=logged.append):
            result = run_from_review(config, task, workspace, no_pull=True)

        assert result.phase is Phase.ESCALATE
        escalate_lines = [line for line in logged if "ESCALATE" in line]
        assert escalate_lines, f"no ESCALATE line on the operator log: {logged}"
        assert result.state.error in escalate_lines[0]

    @patch_gate_shell(side_effect=_resume_shell)
    def test_missing_worktree_states_its_reason_on_the_operator_log(self, _mock_shell, tmp_path):
        """#2908, same shape: resuming against a worktree that is simply gone."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug  # deliberately never created

        logged: list[str] = []
        # This guard lives in run_setup, which resolves ``_log`` through the util
        # module at call time — the same operator stream, a different binding.
        with patch("theforge.coordinator.util._log", side_effect=logged.append):
            result = run_from_review(config, task, workspace, no_pull=True)

        assert result.phase is Phase.ESCALATE
        assert "Worktree not found" in result.state.error
        escalate_lines = [line for line in logged if "ESCALATE" in line]
        assert escalate_lines, f"no ESCALATE line on the operator log: {logged}"
        assert result.state.error in escalate_lines[0]

    def test_review_only_missing_worktree_states_its_reason_on_the_operator_log(self, tmp_path):
        """#2908, same shape: `forge review` against a worktree that is gone."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug  # deliberately never created

        logged: list[str] = []
        with patch("theforge.coordinator.engine._log", side_effect=logged.append):
            result = run_review_only(config, task, workspace)

        assert result.phase is Phase.ESCALATE
        assert "Worktree not found" in result.state.error
        escalate_lines = [line for line in logged if "ESCALATE" in line]
        assert escalate_lines, f"no ESCALATE line on the operator log: {logged}"
        assert result.state.error in escalate_lines[0]


# ── The sprint-log surface ────────────────────────────────────────────


class TestBatchPreflightLogging:
    def _run_batch(self, message: str, tmp_path: Path) -> list[str]:
        from theforge.sprint import collision

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = type("R", (), {"success": False, "message": message, "state": None})()
        logged: list[str] = []
        with (
            patch.object(collision, "run_task", return_value=result),
            patch.object(collision, "_log", side_effect=logged.append),
        ):
            collision.run_batch_preflight(
                [task], config, sprint_name="s", no_pull=True, max_parallel=1
            )
        return logged

    def test_classification_is_logged_line_by_line(self, tmp_path):
        message = "\n".join(
            [
                f"{DRIFT_HEADER} on release/v0.13",
                "",
                "Files changed on both sides:",
                "  - src/theforge/coordinator/gate.py",
                "",
                "Available resolutions:",
                "  - Discard the preserved work",
            ]
        )

        logged = self._run_batch(message, tmp_path)

        assert any("excluded from collision detection" in ln for ln in logged)
        assert any("src/theforge/coordinator/gate.py" in ln for ln in logged)
        assert any("Available resolutions:" in ln for ln in logged)
        # Not squeezed into a single trailing clause.
        assert not any(ln.count("\n") for ln in logged)

    def test_unclassified_failure_keeps_the_single_warning_line(self, tmp_path):
        logged = self._run_batch("Workspace setup command failed: boom", tmp_path)

        assert any("batch preflight returned failure" in ln for ln in logged)
