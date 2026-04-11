"""Tests for merge step-state persistence and crash-resume in _merge_pr."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.coordinator.completion import (
    _merge_pr,
    _step_await_merge_state,
    _step_fetch_rebase,
    _step_force_push,
    _step_merge,
)
from theforge.coordinator.run_setup import (
    delete_merge_state,
    load_merge_state,
    save_merge_state,
)
from theforge.coordinator.state import MergeStepState

# ── MergeStepState persistence helpers ────────────────────────────────


class TestMergeStatePersistence:
    def test_load_returns_fresh_state_when_no_sidecar(self, tmp_path: Path) -> None:
        state = load_merge_state(tmp_path)
        assert state.completed_steps == []
        assert state.pr_url is None
        assert state.merge_queued is False
        assert state.auto_merge_queued is False
        assert state.error is None

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        ms = MergeStepState(
            completed_steps=["create_pr", "merge"],
            pr_url="https://github.com/owner/repo/pull/99",
            merge_queued=True,
            auto_merge_queued=True,
            error=None,
        )
        save_merge_state(tmp_path, ms)
        loaded = load_merge_state(tmp_path)
        assert loaded.completed_steps == ["create_pr", "merge"]
        assert loaded.pr_url == "https://github.com/owner/repo/pull/99"
        assert loaded.merge_queued is True
        assert loaded.auto_merge_queued is True
        assert loaded.error is None

    def test_save_writes_last_updated(self, tmp_path: Path) -> None:
        import yaml

        ms = MergeStepState(completed_steps=["create_pr"])
        save_merge_state(tmp_path, ms)
        sidecar = tmp_path / ".forge" / "merge_state.yaml"
        data = yaml.safe_load(sidecar.read_text())
        assert "last_updated" in data
        assert data["last_updated"]  # non-empty ISO timestamp

    def test_delete_removes_sidecar(self, tmp_path: Path) -> None:
        save_merge_state(tmp_path, MergeStepState(completed_steps=["create_pr"]))
        sidecar = tmp_path / ".forge" / "merge_state.yaml"
        assert sidecar.exists()
        delete_merge_state(tmp_path)
        assert not sidecar.exists()

    def test_delete_is_idempotent_when_no_sidecar(self, tmp_path: Path) -> None:
        # Should not raise even if file doesn't exist.
        delete_merge_state(tmp_path)

    def test_load_returns_fresh_state_on_corrupt_sidecar(self, tmp_path: Path) -> None:
        sidecar = tmp_path / ".forge" / "merge_state.yaml"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("not: valid: yaml: [[[", encoding="utf-8")
        state = load_merge_state(tmp_path)
        assert state.completed_steps == []

    def test_load_returns_fresh_state_on_empty_sidecar(self, tmp_path: Path) -> None:
        sidecar = tmp_path / ".forge" / "merge_state.yaml"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("", encoding="utf-8")
        state = load_merge_state(tmp_path)
        assert state.completed_steps == []

    def test_load_handles_non_dict_sidecar(self, tmp_path: Path) -> None:
        sidecar = tmp_path / ".forge" / "merge_state.yaml"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("- a\n- b\n", encoding="utf-8")
        state = load_merge_state(tmp_path)
        assert state.completed_steps == []

    def test_load_handles_invalid_completed_steps(self, tmp_path: Path) -> None:
        import yaml

        sidecar = tmp_path / ".forge" / "merge_state.yaml"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(yaml.dump({"completed_steps": "not-a-list"}), encoding="utf-8")
        state = load_merge_state(tmp_path)
        assert state.completed_steps == []


# ── Step function unit tests ──────────────────────────────────────────


def _ok(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestStepFetchRebase:
    def test_success(self, tmp_path: Path) -> None:
        with patch("theforge.coordinator.completion.subprocess.run", return_value=_ok()):
            with patch("theforge.coordinator.completion._deindex_forge_artifacts"):
                result = _step_fetch_rebase(tmp_path, "main")
        assert result["success"] is True

    def test_fetch_failure(self, tmp_path: Path) -> None:
        fetch_fail = _ok(1, stderr="network error")

        def fake_run(cmd, **kw):
            if "fetch" in cmd:
                return fetch_fail
            return _ok()

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run):
            with patch("theforge.coordinator.completion._deindex_forge_artifacts"):
                result = _step_fetch_rebase(tmp_path, "main")
        assert result["success"] is False
        assert "fetch" in result["error"].lower()

    def test_rebase_failure_aborts_and_returns_error(self, tmp_path: Path) -> None:
        def fake_run(cmd, **kw):
            if "rebase" in cmd and "--abort" not in cmd:
                return _ok(1, stderr="conflict!")
            return _ok()

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run):
            with patch("theforge.coordinator.completion._deindex_forge_artifacts"):
                result = _step_fetch_rebase(tmp_path, "main")
        assert result["success"] is False
        assert "rebase" in result["error"].lower() or "escalat" in result["error"].lower()


class TestStepForcePush:
    def test_success(self, tmp_path: Path) -> None:
        with patch("theforge.coordinator.completion.subprocess.run", return_value=_ok()):
            result = _step_force_push(tmp_path, "feat/my-branch")
        assert result["success"] is True

    def test_missing_refspec_is_success(self, tmp_path: Path) -> None:
        gone = _ok(1, stderr="error: src refspec feat/my-branch does not match any")
        with patch("theforge.coordinator.completion.subprocess.run", return_value=gone):
            result = _step_force_push(tmp_path, "feat/my-branch")
        assert result["success"] is True

    def test_push_failure(self, tmp_path: Path) -> None:
        with patch(
            "theforge.coordinator.completion.subprocess.run", return_value=_ok(1, stderr="denied")
        ):
            result = _step_force_push(tmp_path, "feat/my-branch")
        assert result["success"] is False
        assert "push" in result["error"].lower()


class TestStepMerge:
    def test_success(self, tmp_path: Path) -> None:
        with patch("theforge.coordinator.completion.subprocess.run", return_value=_ok()):
            result = _step_merge(tmp_path, "https://github.com/o/r/pull/1", "squash")
        assert result["success"] is True
        assert "retryable" not in result

    def test_retryable_error(self, tmp_path: Path) -> None:
        err = _ok(1, stderr="GraphQL: Base branch was modified. Review and try the merge again.")
        with patch("theforge.coordinator.completion.subprocess.run", return_value=err):
            result = _step_merge(tmp_path, "https://github.com/o/r/pull/1", "squash")
        assert result["success"] is False
        assert result["retryable"] is True

    def test_non_retryable_error(self, tmp_path: Path) -> None:
        err = _ok(1, stderr="some other unrelated merge error")
        with patch("theforge.coordinator.completion.subprocess.run", return_value=err):
            result = _step_merge(tmp_path, "https://github.com/o/r/pull/1", "squash")
        assert result["success"] is False
        assert result["retryable"] is False


class TestStepAwaitMergeState:
    def test_merged_immediately(self, tmp_path: Path) -> None:
        with patch(
            "theforge.coordinator.completion.subprocess.run", return_value=_ok(0, stdout="MERGED")
        ):
            result = _step_await_merge_state(tmp_path, "https://github.com/o/r/pull/1")
        assert result["success"] is True
        assert result["merge_queued"] is False
        assert result["auto_merge_queued"] is False

    def test_queued_for_checks(self, tmp_path: Path) -> None:
        with patch(
            "theforge.coordinator.completion.subprocess.run", return_value=_ok(0, stdout="OPEN")
        ):
            result = _step_await_merge_state(tmp_path, "https://github.com/o/r/pull/1")
        assert result["success"] is True
        assert result["merge_queued"] is True
        assert result["auto_merge_queued"] is True

    def test_gh_view_failure(self, tmp_path: Path) -> None:
        with patch(
            "theforge.coordinator.completion.subprocess.run",
            return_value=_ok(1, stderr="not found"),
        ):
            result = _step_await_merge_state(tmp_path, "https://github.com/o/r/pull/1")
        assert result["success"] is False
        assert "error" in result


# ── _merge_pr crash-resume tests ─────────────────────────────────────

PR_URL = "https://github.com/fuzzypete/theforge/pull/42"


def _make_config(tmp_path: Path):
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

    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            on_approve="merge-pr",
            auto_push=True,
            merge_strategy="squash",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path):
    from theforge.task import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Test", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


def _make_review():
    from theforge.review import ReviewResult

    return ReviewResult(
        verdict="APPROVE",
        summary="Looks good.",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


class TestMergePrStepStateResume:
    """Tests that _merge_pr correctly resumes from persisted step state."""

    def _worktree_path(self, tmp_path: Path) -> Path:
        return tmp_path / "test-task"

    def _write_state(self, tmp_path: Path, **kwargs) -> None:
        ms = MergeStepState(**kwargs)
        save_merge_state(self._worktree_path(tmp_path), ms)

    def _patches(self):
        """Return context managers that stub out subprocess and _create_pr."""
        return (
            patch(
                "theforge.coordinator.completion.subprocess.run",
                return_value=_ok(0, stdout="MERGED"),
            ),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={"action": "pr", "pr_url": PR_URL, "success": True, "error": None},
            ),
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        )

    def _run(self, tmp_path: Path, *, gh_list_stdout: str = "[]"):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout=gh_list_stdout)
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={"action": "pr", "pr_url": PR_URL, "success": True, "error": None},
            ) as mock_create_pr,
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        return result, mock_create_pr

    def test_fresh_run_writes_and_deletes_sidecar(self, tmp_path: Path) -> None:
        result, _ = self._run(tmp_path)
        assert result["success"] is True
        # Sidecar should be cleaned up on success.
        sidecar = self._worktree_path(tmp_path) / ".forge" / "merge_state.yaml"
        assert not sidecar.exists()

    def test_resume_skips_create_pr_when_already_completed(self, tmp_path: Path) -> None:
        # Pre-populate state as if create_pr already succeeded.
        self._write_state(
            tmp_path,
            completed_steps=["create_pr"],
            pr_url=PR_URL,
        )

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch("theforge.coordinator.completion._create_pr") as mock_create_pr,
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert result["pr_url"] == PR_URL
        # _create_pr must NOT be called again on resume.
        mock_create_pr.assert_not_called()

    def test_resume_skips_merge_when_already_completed(self, tmp_path: Path) -> None:
        # Pre-populate state as if merge already succeeded.
        self._write_state(
            tmp_path,
            completed_steps=["create_pr", "merge"],
            pr_url=PR_URL,
        )

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        merge_calls: list = []

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                merge_calls.append(cmd)
                return _ok(0)
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch("theforge.coordinator.completion._create_pr") as mock_create_pr,
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        # merge command must NOT be called again.
        assert merge_calls == []
        mock_create_pr.assert_not_called()

    def test_resume_skips_await_merge_state_when_already_completed(self, tmp_path: Path) -> None:
        # Pre-populate state as if await_merge_state already ran (queued).
        self._write_state(
            tmp_path,
            completed_steps=["create_pr", "merge", "await_merge_state"],
            pr_url=PR_URL,
            merge_queued=True,
            auto_merge_queued=True,
        )

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        view_calls: list = []

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "view"]:
                view_calls.append(cmd)
                return _ok(0, stdout="MERGED")
            return _ok(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch("theforge.coordinator.completion._create_pr") as mock_create_pr,
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        # merge_queued restored from state, not from a fresh gh pr view call.
        assert result["merge_queued"] is True
        assert view_calls == []
        mock_create_pr.assert_not_called()

    def test_resume_skips_cleanup_when_already_completed(self, tmp_path: Path) -> None:
        # Pre-populate state as if cleanup already ran.
        self._write_state(
            tmp_path,
            completed_steps=["create_pr", "merge", "await_merge_state", "cleanup"],
            pr_url=PR_URL,
            merge_queued=False,
            auto_merge_queued=False,
        )

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        cleanup_related: list = []

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            if isinstance(cmd, list) and "worktree" in cmd:
                cleanup_related.append("worktree")
            if isinstance(cmd, list) and cmd[:3] == ["git", "branch", "-D"]:
                cleanup_related.append("branch-delete")
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch("theforge.coordinator.completion._create_pr") as mock_create_pr,
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert cleanup_related == []  # cleanup was skipped
        mock_create_pr.assert_not_called()

    def test_failure_persists_error_to_sidecar(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            if isinstance(cmd, list) and "fetch" in cmd:
                return _ok(1, stderr="network failure")
            return _ok(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch("theforge.coordinator.completion._create_pr"),
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is False
        # Error should be persisted so humans can inspect mid-merge state.
        worktree_path = tmp_path / "test-task"
        loaded = load_merge_state(worktree_path)
        assert loaded.error is not None
        assert "fetch" in loaded.error.lower() or "network" in loaded.error.lower()

    def test_step_outcomes_logged_via_pr_log(self, tmp_path: Path) -> None:
        """Each step should log its outcome via _pr_log.info for independent auditability."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        logged_steps: list[str] = []

        def capture_info(msg, *args, **kw):
            if "merge step" in str(msg):
                # Render the format string so we see the actual step name.
                try:
                    rendered = str(msg) % args if args else str(msg)
                except Exception:
                    rendered = str(msg)
                parts = rendered.split()
                if len(parts) >= 3:
                    logged_steps.append(parts[2])

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={"action": "pr", "pr_url": PR_URL, "success": True, "error": None},
            ),
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
            patch("theforge.coordinator.completion._pr_log") as mock_log,
        ):
            mock_log.info.side_effect = capture_info
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        # Each major step should log its outcome.
        assert "fetch_rebase" in logged_steps
        assert "force_push" in logged_steps
        assert "create_pr" in logged_steps
        assert "merge" in logged_steps
        assert "await_merge_state" in logged_steps
        assert "cleanup" in logged_steps

    def test_zero_delta_branch_returns_success_without_merge(self, tmp_path: Path) -> None:
        """Zero-delta skip path: return success immediately, do not persist pr_url=None."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        zero_delta_pr_result = {
            "action": "pr",
            "pr_url": None,
            "success": True,
            "error": None,
            "skipped": True,
            "skip_reason": "zero-delta branch",
            "ahead_commit_count": 0,
        }

        merge_calls: list = []

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                merge_calls.append(cmd)
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value=zero_delta_pr_result,
            ),
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        # Zero-delta: success but no merge attempted.
        assert result["success"] is True
        assert result["pr_url"] is None
        assert result.get("skipped") is True
        assert result.get("skip_reason") == "zero-delta branch"
        assert merge_calls == []

        # merge_state.yaml must be cleaned up (not persisted with pr_url=None).
        sidecar = self._worktree_path(tmp_path) / ".forge" / "merge_state.yaml"
        assert not sidecar.exists()

    def test_resume_after_merge_skips_fetch_rebase_and_force_push(self, tmp_path: Path) -> None:
        """Resuming after merge is complete must not force-push, which could cancel auto-merge."""
        self._write_state(
            tmp_path,
            completed_steps=["create_pr", "merge"],
            pr_url=PR_URL,
            merge_queued=False,
            auto_merge_queued=False,
        )

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        git_push_calls: list = []
        git_fetch_rebase_calls: list = []

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            if isinstance(cmd, list) and cmd[:4] == ["git", "push", "-f", "origin"]:
                git_push_calls.append(cmd)
            if isinstance(cmd, list) and "rebase" in cmd:
                git_fetch_rebase_calls.append(cmd)
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch("theforge.coordinator.completion._create_pr") as mock_create_pr,
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        # force-push must NOT run after merge is already complete.
        assert git_push_calls == [], "force-push must not run when resuming after merge"
        assert git_fetch_rebase_calls == [], "rebase must not run when resuming after merge"
        mock_create_pr.assert_not_called()
