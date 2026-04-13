"""Tests for configurable worker_timeout_seconds in sprint manifest and forge.yaml."""

from __future__ import annotations

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
from theforge.sprint import run_sprint
from theforge.sprint.manifest import ResolvedSprint, load_sprint_manifest

# ── SprintConfig defaults ─────────────────────────────────────────────────────


def test_sprint_config_default_timeout() -> None:
    """SprintConfig.worker_timeout_seconds defaults to 3600."""
    cfg = SprintConfig()
    assert cfg.worker_timeout_seconds == 3600


def test_sprint_config_custom_timeout() -> None:
    """SprintConfig accepts an explicit worker_timeout_seconds."""
    cfg = SprintConfig(worker_timeout_seconds=7200)
    assert cfg.worker_timeout_seconds == 7200


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
        # Config uses the default 3600 but manifest says 120.
        config = _make_config(tmp_path)

        wait_calls: list[float] = []

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return (set(), set())

        with (
            patch("theforge.sprint.runner.pull_base_branch", return_value=True),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
        ):
            run_sprint(config, manifest_path)

        # The poll loop uses the configured timeout as its ceiling.
        # At least one wait() call should use 120.0 (not 3600.0) as the timeout.
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

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return (set(), set())

        with (
            patch("theforge.sprint.runner.pull_base_branch", return_value=True),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
        ):
            run_sprint(config, manifest_path)

        assert any(t == pytest.approx(3600.0) for t in wait_calls), (
            f"Expected a wait() call with timeout=3600.0 but got: {wait_calls}"
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

        def _fake_wait(futs, *, return_when, timeout):
            wait_calls.append(timeout)
            return (set(), set())

        with (
            patch("theforge.sprint.runner.pull_base_branch", return_value=True),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
        ):
            run_sprint(config, resolved)

        assert any(t == pytest.approx(60.0) for t in wait_calls), (
            f"Expected a wait() call with timeout=60.0 but got: {wait_calls}"
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

        def submit(self, *args, **kwargs):
            return _NeverDoneFuture()

    with (
        patch("theforge.sprint.runner.pull_base_branch", return_value=True),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", return_value=(set(), set())),
    ):
        result = run_sprint(config, manifest)

    assert result.stopped_reason is not None
    assert "42" in result.stopped_reason, (
        f"Expected '42' in stopped_reason, got: {result.stopped_reason!r}"
    )
    # Verify story result also has timeout message with configured value
    assert result.results
    _, story_result = result.results[0]
    assert "42" in story_result.message, (
        f"Expected '42' in story result message, got: {story_result.message!r}"
    )
