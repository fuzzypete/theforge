"""Concurrent-writer regressions for the rolling advisory artifact (#2107).

Two stories of one ``--parallel`` sprint write the same artifact through the
same project root. Before this fix the scratch path was a deterministic
function of the destination, so writers collided on it (crashing one of them
with ``FileNotFoundError`` on the rename, or — worse — silently installing one
writer's document under the other's name), and the read-merge-write let the
later writer delete observations it had never seen.
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from coord_test_helpers import _make_config

from theforge.advisory_conventions import (
    AdvisoryArtifactError,
    _write_yaml_atomic,
    advisory_artifact_path,
    load_advisory_summary,
    update_advisory_violations,
)

OBSERVED_AT = dt.datetime(2026, 8, 1, 5, 31, tzinfo=dt.timezone.utc)


def _violation(file: str, *, line_count: int = 900, limit: int = 600) -> dict[str, object]:
    return {
        "rule": "max_module_lines",
        "file": file,
        "detail": f"{file} has {line_count} lines (limit {limit})",
        "blocking": False,
    }


def _files(config) -> set[str]:
    return {
        entry["file"]
        for entry in load_advisory_summary(config)["entries"].values()
        if isinstance(entry, dict)
    }


def test_scratch_path_is_unique_per_writer(tmp_path: Path) -> None:
    """Two writers of one destination must not stage through one scratch path."""
    destination = tmp_path / "conventions" / "advisory.yaml"
    staged: list[Path] = []
    real_replace = Path.replace

    def record_and_replace(self: Path, target):  # type: ignore[no-untyped-def]
        staged.append(Path(self))
        return real_replace(self, target)

    with patch.object(Path, "replace", record_and_replace):
        _write_yaml_atomic(destination, {"entries": {"a": 1}})
        _write_yaml_atomic(destination, {"entries": {"b": 1}})

    assert len(staged) == 2
    assert staged[0] != staged[1], "scratch path is deterministic — writers will collide"
    # The pre-fix scratch path was exactly destination + ".tmp".
    legacy = destination.with_suffix(destination.suffix + ".tmp")
    assert legacy not in staged
    assert not legacy.exists()
    # No scratch file survives a successful write.
    assert sorted(p.name for p in destination.parent.iterdir()) == ["advisory.yaml"]


def test_interleaved_writers_do_not_raise_on_rename(tmp_path: Path) -> None:
    """Replay the reported interleaving: a peer completes its write mid-flight.

    Writer A has staged its scratch file; writer B stages, renames, and finishes
    before A renames. With a shared scratch path A's rename source is gone and A
    dies with ``FileNotFoundError``; with a unique one A is untouched.
    """
    destination = tmp_path / "advisory.yaml"
    real_replace = Path.replace
    replaced: list[Path] = []

    def peer_writes_first(self: Path, target):  # type: ignore[no-untyped-def]
        replaced.append(Path(self))
        if len(replaced) == 1:
            # Writer B runs to completion inside writer A's rename window.
            _write_yaml_atomic(Path(target), {"entries": {"peer": True}})
        return real_replace(self, target)

    with patch.object(Path, "replace", peer_writes_first):
        _write_yaml_atomic(destination, {"entries": {"mine": True}})

    assert yaml.safe_load(destination.read_text(encoding="utf-8")) == {"entries": {"mine": True}}


def test_concurrent_updates_retain_every_writer_observations(tmp_path: Path) -> None:
    """Threads writing one project root keep both stories' observations.

    Real ``fcntl.flock`` acquisitions here are held only across a local
    read-merge-write, so the threads contend for microseconds; the joins are
    bounded so a regression that reintroduces a deadlock fails the test instead
    of hanging the suite.
    """
    config = _make_config(tmp_path)
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def worker(story: str, file: str) -> None:
        try:
            start.wait(timeout=10)
            for i in range(15):
                update_advisory_violations(
                    config,
                    [_violation(file, line_count=900 + i)],
                    observed_at=OBSERVED_AT + dt.timedelta(seconds=i),
                    run_id=f"run-{story}",
                    story_slug=story,
                )
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("issue-2054", "src/theforge/a.py"), daemon=True),
        threading.Thread(target=worker, args=("issue-2057", "src/theforge/b.py"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "advisory artifact write deadlocked"

    assert errors == [], f"concurrent writers raised: {errors!r}"
    assert _files(config) == {"src/theforge/a.py", "src/theforge/b.py"}
    # No scratch file left behind in the artifact directory.
    artifact_dir = advisory_artifact_path(config).parent
    assert sorted(p.name for p in artifact_dir.iterdir()) == ["advisory.yaml"]


def test_later_scan_does_not_delete_a_peer_observation(tmp_path: Path) -> None:
    """A worker that did not observe a peer's entry must not remove it."""
    config = _make_config(tmp_path)
    update_advisory_violations(
        config,
        [_violation("src/theforge/a.py")],
        observed_at=OBSERVED_AT,
        run_id="run-a",
        story_slug="issue-2054",
    )
    result = update_advisory_violations(
        config,
        [_violation("src/theforge/b.py")],
        observed_at=OBSERVED_AT + dt.timedelta(minutes=9),
        run_id="run-b",
        story_slug="issue-2057",
    )

    assert _files(config) == {"src/theforge/a.py", "src/theforge/b.py"}
    assert result["entry_count"] == 2
    # The caller still learns what *it* observed, not the merged set.
    assert {entry["file"] for entry in result["entries"].values()} == {"src/theforge/b.py"}


def test_lock_files_live_outside_the_working_tree(tmp_path: Path) -> None:
    """Lock files stay out of the working tree and out of the story-lock sweep.

    ``sprint.lock.sweep_story_locks`` deletes every unheld ``.forge/locks/*.lock``
    at sprint launch; artifact locks belong to no story and live one level down.
    """
    config = _make_config(tmp_path)
    update_advisory_violations(
        config,
        [_violation("src/theforge/a.py")],
        observed_at=OBSERVED_AT,
        run_id="run-a",
        story_slug="issue-2054",
    )
    lock_dir = tmp_path / ".forge" / "locks" / "advisory"
    locks = sorted(p.name for p in lock_dir.iterdir())
    assert locks and all(name.startswith("advisory.yaml-") for name in locks)
    # Nothing the story-lock sweeper's non-recursive glob would collect.
    assert list((tmp_path / ".forge" / "locks").glob("*.lock")) == []


def test_write_failure_raises_advisory_artifact_error(tmp_path: Path) -> None:
    """A persistence failure names the artifact, keeps the errno, and cleans up."""
    config = _make_config(tmp_path)
    destination = advisory_artifact_path(config)
    staged: list[Path] = []

    def failing_replace(self: Path, target):  # type: ignore[no-untyped-def]
        staged.append(Path(self))
        raise FileNotFoundError(2, "No such file or directory")

    with patch.object(Path, "replace", failing_replace):
        with pytest.raises(AdvisoryArtifactError) as excinfo:
            update_advisory_violations(
                config,
                [_violation("src/theforge/a.py")],
                observed_at=OBSERVED_AT,
                run_id="run-a",
                story_slug="issue-2054",
            )

    error = excinfo.value
    assert error.path == destination
    assert isinstance(error.cause, FileNotFoundError)
    assert "No such file or directory" in str(error)
    # The writer removed its own scratch file and nobody else's.
    assert staged and not any(path.exists() for path in staged)
