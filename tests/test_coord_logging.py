"""Tests for structured logging, per-run log capture, and SIGTERM handling.

Covers: per-run log file tee, project-local log directory, and crash diagnostics.
"""

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    NotificationConfig,
    NtfyConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_from_review, run_task
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.runners import AgentResult
from theforge.task import TaskStory


class TestPerRunLogCapture:
    """Tests for the per-run log file tee (_TeeStderr / _begin_run_log_tee)."""

    def _make_logging_config(self, tmp_path: Path, log_dir: Path) -> ForgeConfig:
        """Create a config with logging enabled, pointing at a tmp log directory."""
        return ForgeConfig(
            project="myproject",
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
            log=LogConfig(
                enabled=True,
                log_file=str(log_dir / "{project}" / "forge.log"),
            ),
        )

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_per_run_log_created(self, mock_shell, mock_agent, mock_preflight, tmp_path):
        """Per-run log file is created at the expected path."""
        import sys

        log_dir = tmp_path / "logs"
        config = self._make_logging_config(tmp_path, log_dir)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        original_stderr = sys.stderr
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task, run_id="abc123xyz")

        assert result.success is True

        # Per-run log file exists at expected path (project-local: .forge/logs/<slug>/run-<id>.log)
        per_run_path = tmp_path / ".forge" / "logs" / "test-task" / "run-abc123xyz.log"
        assert per_run_path.exists(), f"Expected log file not found: {per_run_path}"
        content = per_run_path.read_text(encoding="utf-8")
        assert len(content) > 0, "Per-run log is empty"

        # stderr is restored after run
        assert sys.stderr is original_stderr

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_per_run_log_absent_when_logging_disabled(
        self, mock_shell, mock_agent, mock_preflight, tmp_path
    ):
        """No per-run log file is created when log.enabled is False."""
        log_dir = tmp_path / "logs"
        config = ForgeConfig(
            project="myproject",
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
            log=LogConfig(enabled=False),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task)

        assert result.success is True
        assert not log_dir.exists(), "Log dir should not be created when logging disabled"

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_creates_per_run_log(
        self, mock_shell, mock_agent, mock_preflight, tmp_path
    ):
        """run_from_review() creates a per-run log file."""
        import sys

        log_dir = tmp_path / "logs"
        config = self._make_logging_config(tmp_path, log_dir)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        original_stderr = sys.stderr
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_from_review(config, task, workspace, run_id="reviewrun1")

        assert result.success is True
        per_run_path = tmp_path / ".forge" / "logs" / "test-task" / "run-reviewrun1.log"
        assert per_run_path.exists(), f"Expected log file not found: {per_run_path}"
        assert sys.stderr is original_stderr

    def test_begin_run_log_tee_skipped_in_worker_thread(self, tmp_path):
        """_begin_run_log_tee returns None when called from a non-main thread."""
        import sys
        import threading

        from theforge.coordinator.log_tee import _begin_run_log_tee
        from theforge.coordinator.logging import StructuredLogger

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        config = self._make_logging_config(tmp_path, log_dir)
        logger = StructuredLogger(
            run_id="tee-test",
            project="test",
            task="some-slug",
            log_file=str(tmp_path / "forge.log"),
            enabled=True,
            project_root=tmp_path,
        )

        results: list = []
        original_stderr = sys.stderr

        def worker():
            tee = _begin_run_log_tee(config, logger, "some-slug", log_dir=log_dir)
            results.append(tee)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # Tee must be None (skipped) in worker thread
        assert results[0] is None, "Expected tee to be skipped in worker thread"
        # sys.stderr must be untouched
        assert sys.stderr is original_stderr

    def test_begin_run_log_tee_active_on_main_thread(self, tmp_path):
        """_begin_run_log_tee installs tee when called from the main thread."""
        import sys

        from theforge.coordinator.log_tee import _begin_run_log_tee, _end_run_log_tee
        from theforge.coordinator.logging import StructuredLogger

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        config = self._make_logging_config(tmp_path, log_dir)
        logger = StructuredLogger(
            run_id="tee-main",
            project="test",
            task="main-slug",
            log_file=str(tmp_path / "forge.log"),
            enabled=True,
            project_root=tmp_path,
        )
        original_stderr = sys.stderr

        tee = _begin_run_log_tee(config, logger, "main-slug", log_dir=log_dir)
        try:
            assert tee is not None, "Expected tee to be active on main thread"
            assert sys.stderr is not original_stderr
        finally:
            _end_run_log_tee(tee)

        assert sys.stderr is original_stderr


# ── Project-local log directory tests ───────────────────────────────


class TestProjectLocalLogDir:
    """Tests for per-story log directory creation and artifact writes."""

    def _make_config(self, tmp_path: Path) -> ForgeConfig:
        return ForgeConfig(
            project="testproj",
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
            log=LogConfig(enabled=True),
        )

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_story_log_dir_created(self, mock_shell, mock_agent, mock_preflight, tmp_path):
        """Per-story log directory created under <project_root>/.forge/logs/<slug>/."""
        config = self._make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task)

        assert result.success is True
        story_log_dir = tmp_path / ".forge" / "logs" / "test-task"
        assert story_log_dir.is_dir(), f"Story log dir not created: {story_log_dir}"
        assert result.state.log_dir == story_log_dir

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_preflight_yaml_written(self, mock_shell, mock_agent, mock_preflight, tmp_path):
        """preflight.yaml written to story log dir after PREFLIGHT phase."""
        config = self._make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task)

        assert result.success is True
        import yaml as _yaml

        preflight_path = tmp_path / ".forge" / "logs" / "test-task" / "preflight.yaml"
        assert preflight_path.exists(), "preflight.yaml not written"
        data = _yaml.safe_load(preflight_path.read_text())
        assert "verdict" in data

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_cycle_artifacts_written(
        self, mock_shell, mock_agent, mock_preflight, tmp_path
    ):
        """Review cycle artifacts written per reviewer and synthesized."""
        config = self._make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(
                    success=True, output=APPROVE_REVIEW, profile_name="claude-reviewer"
                )
            ]
            result = run_task(config, task)

        assert result.success is True
        cycle_dir = tmp_path / ".forge" / "logs" / "test-task" / "review-cycle-1"
        assert cycle_dir.is_dir(), f"review-cycle-1 dir not created: {cycle_dir}"
        synthesized = cycle_dir / "synthesized.yaml"
        assert synthesized.exists(), "synthesized.yaml not written"

    def test_sprint_nesting(self, tmp_path):
        """Sprint passes sprint_name and creates sprint-level log dir + sprint-summary.yaml."""
        import yaml as _yaml

        from theforge.coordinator.state import Phase
        from theforge.sprint import run_sprint

        spec = tmp_path / "story.md"
        spec.write_text("---\nslug: my-story\n---\n# Story", encoding="utf-8")
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            _yaml.dump({"name": "my-sprint", "budget_usd": 10.0, "specs": ["story.md"]}),
            encoding="utf-8",
        )

        config = self._make_config(tmp_path)

        # Mock run_task to return a successful result with a log_dir
        _state = CoordinatorState()
        _state.log_dir = tmp_path / ".forge" / "logs" / "my-sprint" / "my-story"
        _state.log_dir.mkdir(parents=True, exist_ok=True)

        class _FakeResult:
            success = True
            phase = Phase.DONE
            state = _state
            merge = None
            message = "done"

        captured_kwargs: dict = {}

        def _fake_run_task(cfg, tsk, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResult()

        with (
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.coordinator.audit.generate_audit_log", return_value={"task": {}}),
        ):
            run_sprint(config, manifest_path)

        # run_task called with sprint_name="my-sprint"
        assert captured_kwargs.get("sprint_name") == "my-sprint"

        # Sprint-level log dir exists
        sprint_log_dir = tmp_path / ".forge" / "logs" / "my-sprint"
        assert sprint_log_dir.is_dir(), f"Sprint log dir not created: {sprint_log_dir}"

        # sprint-summary.yaml written
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        assert summary_path.exists(), "sprint-summary.yaml not written"
        data = _yaml.safe_load(summary_path.read_text())
        assert data["sprint"]["name"] == "my-sprint"

    def test_parallel_sprint_story_log_dir_still_accepts_artifacts(self, tmp_path):
        """Parallel sprint workers still get per-story log dirs for structured artifacts."""
        import yaml as _yaml

        from theforge.coordinator.log_tee import (
            _begin_run_log_tee,
            _make_story_log_dir,
            _write_log_artifact,
        )
        from theforge.coordinator.logging import StructuredLogger
        from theforge.sprint import run_sprint

        spec_a = tmp_path / "story-a.md"
        spec_a.write_text("---\nslug: story-a\n---\n# Story A", encoding="utf-8")
        spec_b = tmp_path / "story-b.md"
        spec_b.write_text("---\nslug: story-b\n---\n# Story B", encoding="utf-8")
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            _yaml.dump(
                {
                    "name": "parallel-sprint",
                    "budget_usd": 10.0,
                    "specs": ["story-a.md", "story-b.md"],
                    "max_parallel": 2,
                }
            ),
            encoding="utf-8",
        )

        config = self._make_config(tmp_path)
        captured_log_dirs: dict[str, Path] = {}
        tee_results: dict[str, object] = {}

        def _fake_run_task(cfg, tsk, **kwargs):
            sprint_name = kwargs["sprint_name"]
            log_dir = _make_story_log_dir(cfg, tsk.slug, sprint_name=sprint_name)
            assert log_dir is not None
            logger = StructuredLogger(
                run_id="parallel-run",
                project=cfg.project,
                task=tsk.slug,
                log_file=str(tmp_path / "forge.log"),
                enabled=True,
                project_root=tmp_path,
            )
            tee_results[tsk.slug] = _begin_run_log_tee(cfg, logger, tsk.slug, log_dir=log_dir)
            _write_log_artifact(log_dir, "preflight.yaml", f"slug: {tsk.slug}\n")
            captured_log_dirs[tsk.slug] = log_dir

            result_state = CoordinatorState()
            result_state.log_dir = log_dir

            class _FakeResult:
                success = True
                phase = Phase.DONE
                merge = None
                message = "done"

            fake_result = _FakeResult()
            fake_result.state = result_state
            return fake_result

        with (
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.coordinator.audit.generate_audit_log", return_value={"task": {}}),
        ):
            result = run_sprint(config, manifest_path)

        assert result.specs_succeeded == 2
        assert tee_results == {"story-a": None, "story-b": None}
        for slug in ("story-a", "story-b"):
            log_dir = captured_log_dirs[slug]
            assert log_dir == tmp_path / ".forge" / "logs" / "parallel-sprint" / slug
            artifact = log_dir / "preflight.yaml"
            assert artifact.exists(), f"Missing artifact for {slug}"
            assert _yaml.safe_load(artifact.read_text()) == {"slug": slug}
            assert list(log_dir.glob("run-*.log")) == []


class TestSigtermHandler:
    """Tests for _make_sigterm_handler crash diagnostics."""

    def _make_task(self, tmp_path: Path) -> "TaskStory":
        spec = tmp_path / "spec.md"
        spec.write_text("# spec")
        return TaskStory(name="Test Task", slug="test-task", story_path=spec)

    def _make_config_no_ntfy(self, tmp_path: Path) -> ForgeConfig:
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
            log=LogConfig(enabled=True, log_file=str(tmp_path / "forge.log")),
        )

    def _make_config_with_ntfy(self, tmp_path: Path) -> ForgeConfig:
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
            log=LogConfig(enabled=True, log_file=str(tmp_path / "forge.log")),
            notifications=NotificationConfig(
                backend="ntfy",
                ntfy=NtfyConfig(url="https://ntfy.sh/test-topic", priority="default"),
            ),
        )

    def test_crash_handler_emits_all_fields(self, tmp_path: Path) -> None:
        """Handler emits run_end:crashed with all required context fields."""
        import signal as _signal
        import time

        from theforge.coordinator.logging import StructuredLogger
        from theforge.coordinator.signals import _make_sigterm_handler

        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="test-run",
            project="test",
            task="test-task",
            log_file=str(log_file),
            enabled=True,
            project_root=tmp_path,
        )
        # Emit one event so last_event is non-empty
        logger.emit("phase_start", phase="DEV")

        state = CoordinatorState()
        state.phase = Phase.DEV
        state.dev_iteration = 2
        # total_cost is a computed property over dev_results
        state.dev_results.append(
            AgentResult(
                success=True,
                output="",
                session_id=None,
                cost_usd=0.57,
                exit_code=0,
                raw={},
                profile_name="dev",
            )
        )

        task = self._make_task(tmp_path)
        config = self._make_config_no_ntfy(tmp_path)
        task_start = time.monotonic() - 10.0  # pretend 10s have elapsed

        captured: list[dict] = []
        original_safe_emit = logger._safe_emit

        def _capture_safe_emit(event: str, **fields: object) -> None:
            captured.append({"event": event, **fields})
            original_safe_emit(event, **fields)

        logger._safe_emit = _capture_safe_emit  # type: ignore[method-assign]

        handler = _make_sigterm_handler(
            logger,
            None,
            _signal.SIG_DFL,
            state=state,
            task_start=task_start,
            task=task,
            config=config,
        )

        with patch("os.kill"):
            handler(_signal.SIGTERM, None)

        assert len(captured) == 1
        ev = captured[0]
        assert ev["event"] == "run_end"
        assert ev["outcome"] == "crashed"
        assert ev["signal"] == _signal.SIGTERM
        assert ev["signal_name"] == "SIGTERM"
        assert ev["phase_at_crash"] == "DEV"
        assert ev["iteration_at_crash"] == 2
        assert ev["cost_at_crash"] == round(0.57, 6)
        assert ev["last_event"] == "phase_start"
        assert ev["uptime_seconds"] >= 9.0  # at least 9s given 10s offset

    def test_crash_handler_calls_ntfy_when_configured(self, tmp_path: Path) -> None:
        """Handler calls _ntfy_crash_notify when ntfy is configured."""
        import signal as _signal

        from theforge.coordinator.logging import StructuredLogger
        from theforge.coordinator.signals import _make_sigterm_handler

        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="test-run",
            project="test",
            task="test-task",
            log_file=str(log_file),
            enabled=True,
            project_root=tmp_path,
        )

        state = CoordinatorState()
        state.phase = Phase.PLAN_REVIEW
        state.dev_iteration = 0

        task = self._make_task(tmp_path)
        config = self._make_config_with_ntfy(tmp_path)

        handler = _make_sigterm_handler(
            logger,
            None,
            _signal.SIG_DFL,
            state=state,
            task_start=0.0,
            task=task,
            config=config,
        )

        with (
            patch("os.kill"),
            patch("theforge.coordinator.notify._ntfy_crash_notify") as mock_crash_notify,
        ):
            handler(_signal.SIGTERM, None)

        mock_crash_notify.assert_called_once()
        call_kwargs = mock_crash_notify.call_args
        assert call_kwargs[0][0] is task
        assert call_kwargs[0][1] is state
        assert call_kwargs[0][2] is config

    def test_crash_handler_no_ntfy_when_not_configured(self, tmp_path: Path) -> None:
        """_ntfy_publish is never called end-to-end when ntfy is not configured."""
        import signal as _signal

        from theforge.coordinator.logging import StructuredLogger
        from theforge.coordinator.signals import _make_sigterm_handler

        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="test-run",
            project="test",
            task="test-task",
            log_file=str(log_file),
            enabled=True,
            project_root=tmp_path,
        )

        state = CoordinatorState()
        state.phase = Phase.DEV
        task = self._make_task(tmp_path)
        config = self._make_config_no_ntfy(tmp_path)

        handler = _make_sigterm_handler(
            logger,
            None,
            _signal.SIG_DFL,
            state=state,
            task_start=0.0,
            task=task,
            config=config,
        )

        with patch("os.kill"), patch("theforge.coordinator.notify._ntfy_publish") as mock_publish:
            handler(_signal.SIGTERM, None)

        # When ntfy is not configured, _ntfy_crash_notify guards internally and
        # _ntfy_publish must never be called.
        mock_publish.assert_not_called()


class TestWorkerSlugContext:
    def test_get_worker_slug_default_empty(self) -> None:
        from theforge.coordinator.log_tee import get_worker_slug, set_worker_slug

        set_worker_slug("")
        assert get_worker_slug() == ""

    def test_set_and_get_worker_slug(self) -> None:
        from theforge.coordinator.log_tee import get_worker_slug, set_worker_slug

        set_worker_slug("issue-99")
        assert get_worker_slug() == "issue-99"
        set_worker_slug("")

    def test_worker_slug_is_thread_local(self) -> None:
        import threading

        from theforge.coordinator.log_tee import get_worker_slug, set_worker_slug

        results: dict[str, str] = {}

        def worker_a() -> None:
            set_worker_slug("issue-99")
            results["a"] = get_worker_slug()

        def worker_b() -> None:
            results["b"] = get_worker_slug()

        thread_a = threading.Thread(target=worker_a)
        thread_b = threading.Thread(target=worker_b)
        thread_a.start()
        thread_a.join()
        thread_b.start()
        thread_b.join()

        assert results == {"a": "issue-99", "b": ""}

    def test_runner_log_includes_slug(self, monkeypatch, capsys) -> None:
        from theforge.sprint import runner

        monkeypatch.setattr(runner, "get_worker_slug", lambda: "my-story")
        runner._log("hello")

        assert "[sprint] [my-story] hello" in capsys.readouterr().err

    def test_util_log_includes_slug(self, monkeypatch, capsys) -> None:
        from theforge.coordinator import util

        monkeypatch.setattr(util, "get_worker_slug", lambda: "my-story")
        util._log("hello")

        assert "[forge] [my-story] hello" in capsys.readouterr().err
