"""Tests for configurable worker_timeout_seconds in sprint manifest and forge.yaml."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.test_sprint_parallel import (
    _make_config,
    _make_config_with_sprint,
    _make_spec_file,
)
from theforge.config import SprintConfig
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.util import LARGE_HEADROOM_FACTOR, MEDIUM_HEADROOM_FACTOR
from theforge.sprint import run_sprint
from theforge.sprint.manifest import ResolvedSprint, load_sprint_manifest
from theforge.sprint.runner import derive_worker_timeout


def _make_preflight_state(
    complexity: str | None,
    complexity_score: int | None = None,
) -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_complexity = complexity
    state.preflight_complexity_score = complexity_score
    return state


def _make_done_story_result(success: bool = True) -> CoordinatorResult:
    state = CoordinatorState()
    state.total_cost = 0.0
    return CoordinatorResult(
        success=success,
        phase=Phase.DONE if success else Phase.ESCALATE,
        state=state,
        message="Done." if success else "Failed.",
    )


# ── SprintConfig defaults ─────────────────────────────────────────────────────


def test_sprint_config_default_timeout() -> None:
    """SprintConfig.worker_timeout_seconds defaults to 3600."""
    cfg = SprintConfig()
    assert cfg.worker_timeout_seconds == 3600


def test_sprint_config_custom_timeout() -> None:
    """SprintConfig accepts an explicit worker_timeout_seconds."""
    cfg = SprintConfig(worker_timeout_seconds=7200)
    assert cfg.worker_timeout_seconds == 7200


class TestDeriveWorkerTimeout:
    def test_large_complexity_uses_large_headroom(self) -> None:
        assert derive_worker_timeout(3600, "large") == round(3600 * LARGE_HEADROOM_FACTOR)

    def test_medium_complexity_uses_medium_headroom(self) -> None:
        assert derive_worker_timeout(3600, "medium") == round(3600 * MEDIUM_HEADROOM_FACTOR)

    def test_small_complexity_uses_base(self) -> None:
        assert derive_worker_timeout(3600, "small") == 3600

    def test_none_complexity_uses_base(self) -> None:
        assert derive_worker_timeout(3600, None) == 3600

    def test_score_based_path_matches_string_path(self) -> None:
        assert derive_worker_timeout(3600, "medium", 6) == round(3600 * MEDIUM_HEADROOM_FACTOR)
        assert derive_worker_timeout(3600, "large", 8) == round(3600 * LARGE_HEADROOM_FACTOR)


# ── SprintManifest parsing ────────────────────────────────────────────────────


class TestWorkerTimeoutManifest:
    def test_parses_worker_timeout_seconds(self, tmp_path: Path) -> None:
        """load_sprint_manifest parses worker_timeout_seconds=7200 correctly."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump(
                {
                    "name": "X",
                    "budget_usd": 5.0,
                    "stories": ["a.md"],
                    "worker_timeout_seconds": 7200,
                }
            ),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.worker_timeout_seconds == 7200

    def test_defaults_to_none_when_absent(self, tmp_path: Path) -> None:
        """worker_timeout_seconds is None (sentinel) when not specified in manifest."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"]}),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.worker_timeout_seconds is None

    def test_rejects_zero(self, tmp_path: Path) -> None:
        """worker_timeout_seconds=0 is rejected with ValueError."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump(
                {
                    "name": "X",
                    "budget_usd": 5.0,
                    "stories": ["a.md"],
                    "worker_timeout_seconds": 0,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="worker_timeout_seconds"):
            load_sprint_manifest(path)

    def test_rejects_negative(self, tmp_path: Path) -> None:
        """worker_timeout_seconds=-1 is rejected with ValueError."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump(
                {
                    "name": "X",
                    "budget_usd": 5.0,
                    "stories": ["a.md"],
                    "worker_timeout_seconds": -100,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="worker_timeout_seconds"):
            load_sprint_manifest(path)

    def test_rejects_non_integer(self, tmp_path: Path) -> None:
        """worker_timeout_seconds as float string is rejected."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            'name: X\nbudget_usd: 5.0\nstories: [a.md]\nworker_timeout_seconds: "3600"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="worker_timeout_seconds"):
            load_sprint_manifest(path)


# ── Precedence: manifest vs forge.yaml ───────────────────────────────────────


class TestWorkerTimeoutPrecedence:
    """Manifest worker_timeout_seconds overrides forge.yaml default; absent means use config."""

    def _make_manifest(self, tmp_path: Path, timeout: int | None = None) -> Path:
        data: dict = {"name": "X", "budget_usd": 5.0, "stories": ["story-a.md"]}
        if timeout is not None:
            data["worker_timeout_seconds"] = timeout
        path = tmp_path / "sprint.yaml"
        path.write_text(yaml.dump(data), encoding="utf-8")
        return path

    def test_manifest_wins_over_config_default(self, tmp_path: Path) -> None:
        """Manifest worker_timeout_seconds=120 overrides config default of 3600."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, timeout=120)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=1)

        wait_calls: list[float] = []

        class _NeverDoneFuture:
            def cancel(self):
                return True

            def result(self):
                raise AssertionError("should not be called")

        class _FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def submit(self, _fn, _config, task, *args, **kwargs):
                return _NeverDoneFuture()

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return (set(), set())

        with (
            patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
            patch(
                "theforge.sprint.runner._run_baseline_gate",
                return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
            ),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
            patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 0.0, 121.0]),
        ):
            run_sprint(config, manifest_path)

        assert any(t == pytest.approx(120.0) for t in wait_calls), (
            f"Expected a wait() call with timeout=120.0 but got: {wait_calls}"
        )

    def test_config_default_used_when_manifest_omits(self, tmp_path: Path) -> None:
        """When manifest omits worker_timeout_seconds, config default is used."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, timeout=None)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=1)
        # config.sprint.worker_timeout_seconds defaults to 3600

        wait_calls: list[float] = []

        class _NeverDoneFuture:
            def cancel(self):
                return True

            def result(self):
                raise AssertionError("should not be called")

        class _FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def submit(self, _fn, _config, task, *args, **kwargs):
                return _NeverDoneFuture()

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return (set(), set())

        with (
            patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
            patch(
                "theforge.sprint.runner._run_baseline_gate",
                return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
            ),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
            patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 0.0, 3601.0]),
        ):
            run_sprint(config, manifest_path)

        assert any(t == pytest.approx(3600.0) for t in wait_calls), (
            f"Expected a wait() call with timeout=3600.0 but got: {wait_calls}"
        )

    def test_large_preflight_derives_timeout_when_manifest_omits(self, tmp_path: Path) -> None:
        """A large story derives a larger worker timeout when no explicit override exists."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, timeout=None)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=1)
        wait_calls: list[float] = []

        class _DoneFuture:
            def __init__(self, task):
                self._task = task

            def cancel(self):
                return False

            def result(self):
                t0 = t1 = datetime.datetime.now(datetime.timezone.utc)
                return (self._task, _make_done_story_result(), 0.0, t0, t1)

        class _FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def submit(self, _fn, _config, task, *args, **kwargs):
                return _DoneFuture(task)

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return ({next(iter(futs))}, set())

        with (
            patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
            patch(
                "theforge.sprint.runner._run_baseline_gate",
                return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
            ),
            patch(
                "theforge.sprint.runner.run_batch_preflight",
                return_value={"story-a": _make_preflight_state("large", 8)},
            ),
            patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
        ):
            run_sprint(config, manifest_path)

        assert any(t == pytest.approx(round(3600 * LARGE_HEADROOM_FACTOR)) for t in wait_calls), (
            f"Expected wait() to use the derived large-story timeout, got: {wait_calls}"
        )

    def test_resolved_sprint_timeout_overrides_config(self, tmp_path: Path) -> None:
        """ResolvedSprint with worker_timeout_seconds=60 overrides config default."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        spec_path = tmp_path / "story-a.md"
        from theforge.sprint.manifest import _build_task_from_story
        from theforge.sprint.sources import FileSource

        task = _build_task_from_story(spec_path)
        source = FileSource()
        resolved = ResolvedSprint(
            name="Direct",
            budget_usd=5.0,
            stories=[(task, source, "story-a.md")],
            max_parallel=1,
            worker_timeout_seconds=60,
        )
        config = _make_config(tmp_path)

        wait_calls: list[float] = []

        class _NeverDoneFuture:
            def cancel(self):
                return True

            def result(self):
                raise AssertionError("should not be called")

        class _FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def submit(self, _fn, _config, task, *args, **kwargs):
                return _NeverDoneFuture()

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return (set(), set())

        with (
            patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
            patch(
                "theforge.sprint.runner._run_baseline_gate",
                return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
            ),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
            patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 0.0, 61.0]),
        ):
            run_sprint(config, resolved)

        assert any(t == pytest.approx(60.0) for t in wait_calls), (
            f"Expected a wait() call with timeout=60.0 but got: {wait_calls}"
        )

    def test_manifest_timeout_wins_over_complexity_derivation(self, tmp_path: Path) -> None:
        """A manifest timeout override wins even when preflight marks the story as large."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, timeout=7200)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=1)
        wait_calls: list[float] = []

        class _DoneFuture:
            def __init__(self, task):
                self._task = task

            def cancel(self):
                return False

            def result(self):
                t0 = t1 = datetime.datetime.now(datetime.timezone.utc)
                return (self._task, _make_done_story_result(), 0.0, t0, t1)

        class _FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def submit(self, _fn, _config, task, *args, **kwargs):
                return _DoneFuture(task)

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return ({next(iter(futs))}, set())

        with (
            patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
            patch(
                "theforge.sprint.runner._run_baseline_gate",
                return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
            ),
            patch(
                "theforge.sprint.runner.run_batch_preflight",
                return_value={"story-a": _make_preflight_state("large", 8)},
            ),
            patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
        ):
            run_sprint(config, manifest_path)

        assert any(t == pytest.approx(7200.0) for t in wait_calls), (
            f"Expected manifest override timeout=7200.0 but got: {wait_calls}"
        )

    def test_manifest_none_is_sentinel_for_config_fallback(self, tmp_path: Path) -> None:
        """After load_sprint_manifest without timeout, worker_timeout_seconds is None."""
        manifest_path = self._make_manifest(tmp_path, timeout=None)
        manifest = load_sprint_manifest(manifest_path)
        assert manifest.worker_timeout_seconds is None


# ── Timeout error message uses configured value ───────────────────────────────


def test_timeout_error_message_uses_configured_value(tmp_path: Path) -> None:
    """When timeout fires, error messages reference the configured timeout (not 3600)."""
    from tests.test_sprint_resume import _make_manifest as _make_basic_manifest
    from tests.test_sprint_resume import _make_spec_file

    spec = _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest = _make_basic_manifest(tmp_path, [spec.name])
    # Write a custom timeout into the manifest
    manifest_data = yaml.safe_load(manifest.read_text())
    manifest_data["worker_timeout_seconds"] = 42
    manifest.write_text(yaml.dump(manifest_data), encoding="utf-8")

    config = _make_config(tmp_path)

    class _NeverDoneFuture:
        def __init__(self):
            self.cancel_called = False

        def cancel(self):
            self.cancel_called = True
            return True

        def result(self):
            raise AssertionError("should not be called")

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, *args, **kwargs):
            return _NeverDoneFuture()

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", return_value=(set(), set())),
        patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 50.0, 50.0]),
    ):
        result = run_sprint(config, manifest)

    assert result.results
    _, story_result = result.results[0]
    assert "42" in story_result.message, (
        f"Expected '42' in story result message, got: {story_result.message!r}"
    )


def test_per_story_deadline_expires_only_the_elapsed_future(tmp_path: Path, capsys) -> None:
    """A timed-out story is cancelled independently while other active workers continue."""
    spec_a = _make_spec_file(tmp_path, "Story A", "story-a")
    spec_b = _make_spec_file(tmp_path, "Story B", "story-b")
    manifest = tmp_path / "sprint.yaml"
    manifest.write_text(
        yaml.dump(
            {
                "name": "Parallel",
                "budget_usd": 5.0,
                "stories": [spec_a.name, spec_b.name],
                "max_parallel": 2,
            }
        ),
        encoding="utf-8",
    )
    config = _make_config_with_sprint(tmp_path, sprint_max_parallel=2)

    class _NeverDoneFuture:
        def __init__(self):
            self.cancel_called = False

        def cancel(self):
            self.cancel_called = True
            return True

        def result(self):
            raise AssertionError("should not be called")

    class _DoneFuture:
        def __init__(self, task):
            self._task = task
            self.cancel_called = False

        def cancel(self):
            self.cancel_called = True
            return False

        def result(self):
            t0 = t1 = datetime.datetime.now(datetime.timezone.utc)
            return (self._task, _make_done_story_result(), 0.0, t0, t1)

    hanging_future = _NeverDoneFuture()
    completed_future = None

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            self._submitted = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, _fn, _config, task, *args, **kwargs):
            nonlocal completed_future
            self._submitted += 1
            if self._submitted == 1:
                return hanging_future
            completed_future = _DoneFuture(task)
            return completed_future

    wait_calls = 0

    def _fake_wait(futs, *, return_when, timeout):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            return (set(), set())
        return ({completed_future}, set())

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            return_value={
                "story-a": _make_preflight_state("small", 3),
                "story-b": _make_preflight_state("large", 8),
            },
        ),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
        patch(
            "theforge.sprint.runner.time.monotonic",
            side_effect=[0.0, 0.0, 4000.0, 4000.0, 4001.0, 4001.0],
        ),
    ):
        result = run_sprint(config, manifest)

    stderr = capsys.readouterr().err
    assert "TIMEOUT story-a (worker unresponsive after 3600s — marking as failed)" in stderr
    assert hanging_future.cancel_called is True
    assert completed_future is not None
    assert completed_future.cancel_called is False
    assert len(result.results) == 2
    assert {spec for spec, _res in result.results} == {"story-a.md", "story-b.md"}
    by_spec = dict(result.results)
    assert by_spec["story-a.md"].success is False
    assert by_spec["story-b.md"].success is True
