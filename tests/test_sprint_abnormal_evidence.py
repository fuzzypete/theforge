"""Seam tests for issue #2030: an abnormal story exit must leave primary evidence.

Three exits never hand the scheduler a ``CoordinatorResult`` — a launch-guard
drop, a worker exception, a worker timeout — and the audit record is finalized
from that result. So the runs with the least recoverable context were exactly
the ones that produced no record at all, leaving the agent's own prose (or a
sprint-summary line a later resume would overwrite) as the only account of the
failure.

These tests pin both halves: the record exists and names the primary cause, and
a second attempt at the same story adds to the recorded causes instead of
replacing them.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from sprint_test_helpers import run_sprint_ctx

from tests.test_sprint_launch_liveness import (
    _make_config,
    _make_coordinator_result,
    _make_manifest,
    _make_spec_file,
    _triage_full,
)
from theforge.coordinator import audit_substrate
from theforge.sprint.abnormal import (
    ABNORMAL_LAUNCH_GUARD_DROP,
    ABNORMAL_WORKER_EXCEPTION,
    ABNORMAL_WORKER_TIMEOUT,
    accumulate_failure_history,
    build_abnormal_cause,
    derive_failure_cause,
)
from theforge.sprint.launch_guard import REASON_ACTIVE_WORKTREE, REASON_STRANDED_WORKTREE
from theforge.sprint.story_state import SprintStoryState, StoryOutcome

# ── helpers ──────────────────────────────────────────────────────────


def _story_audits(project_root: Path) -> dict[str, dict]:
    """Every per-run audit record in the substrate, keyed by story slug."""
    conn = audit_substrate.require_substrate(project_root)
    try:
        records = list(audit_substrate.iter_records(conn))
    finally:
        conn.close()
    return {(rec.get("task") or {}).get("slug"): rec for rec in records}


def _accumulated_story(tmp_path: Path, slug: str) -> dict:
    """The story's row in the sprint's accumulated state file."""
    state_file = next((tmp_path / ".forge" / "sprints").glob("*/state.yaml"))
    stories = yaml.safe_load(state_file.read_text())["stories"]
    return {s["slug"]: s for s in stories}[slug]


def _run_sprint_landing(tmp_path: Path, manifest_path: Path, *, run_id: str) -> None:
    """Run the sprint again with every story dispatched and landing."""
    config = _make_config(tmp_path)
    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint_ctx(config, manifest_path, run_id=run_id)


def _run_sprint_with_drop(tmp_path: Path, *, reason: str, run_id: str = "run-2030"):
    _make_spec_file(tmp_path, "Issue 2048", "issue-2048")
    _make_spec_file(tmp_path, "Issue 2060", "issue-2060")
    manifest_path = _make_manifest(tmp_path, ["issue-2048.md", "issue-2060.md"])
    config = _make_config(tmp_path)

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        return run_sprint_ctx(
            config,
            manifest_path,
            run_id=run_id,
            dropped_slugs={"issue-2048": reason},
        )


# ── launch-guard drops leave a record ────────────────────────────────


def test_launch_guard_drop_writes_a_run_audit_naming_the_reason(tmp_path: Path) -> None:
    """A story dropped before dispatch gets its own audit record, not silence."""
    _run_sprint_with_drop(tmp_path, reason=REASON_ACTIVE_WORKTREE)

    audits = _story_audits(tmp_path)
    assert "issue-2048" in audits, "the dropped story produced no audit record at all"

    record = audits["issue-2048"]
    abnormal = record.get("abnormal_termination")
    assert isinstance(abnormal, dict)
    assert abnormal["kind"] == ABNORMAL_LAUNCH_GUARD_DROP
    assert REASON_ACTIVE_WORKTREE in abnormal["cause"]
    # The source names the code path that observed the cause, so a reader can
    # tell captured evidence from a reconstruction.
    assert abnormal["source"]
    assert abnormal["run_id"] == record["run_id"]
    # The cause is also readable from the ordinary error fields, which is where
    # an operator (and every existing audit reader) looks first.
    assert REASON_ACTIVE_WORKTREE in record["error"]
    assert record["error_type"] == "LaunchGuardDrop"


def test_launch_guard_drop_record_is_addressable_as_a_run(tmp_path: Path) -> None:
    """The drop record carries a run id and lands as a per-run JSON file."""
    _run_sprint_with_drop(tmp_path, reason=REASON_ACTIVE_WORKTREE)

    record = _story_audits(tmp_path)["issue-2048"]
    run_id = record["run_id"]
    assert isinstance(run_id, str) and run_id

    run_file = audit_substrate.runs_dir(tmp_path) / f"{run_id}.json"
    assert run_file.exists(), "drop record is not addressable as a run"


def test_stranded_worktree_drop_also_leaves_a_record(tmp_path: Path) -> None:
    """The stranded-prior-state drop is a drop too, with its own distinct reason."""
    _run_sprint_with_drop(tmp_path, reason=REASON_STRANDED_WORKTREE)

    abnormal = _story_audits(tmp_path)["issue-2048"]["abnormal_termination"]
    assert abnormal["kind"] == ABNORMAL_LAUNCH_GUARD_DROP
    assert REASON_STRANDED_WORKTREE in abnormal["cause"]


def test_drop_record_never_overwrites_an_existing_story_audit(tmp_path: Path) -> None:
    """A drop must not replace the audit of the generation that actually ran.

    The dropped story shares its log directory with the run whose worktree
    collided. That run's audit.yaml is the primary evidence the drop exists to
    point at — overwriting it with a synthetic no-op record destroys exactly what
    is being looked for.
    """
    log_dir = tmp_path / ".forge" / "logs" / "Test Sprint" / "issue-2048"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "audit.yaml").write_text(
        yaml.dump({"run_id": "prior-generation", "iterations": {"dev_attempts_total": 3}}),
        encoding="utf-8",
    )

    _run_sprint_with_drop(tmp_path, reason=REASON_ACTIVE_WORKTREE)

    preserved = yaml.safe_load((log_dir / "audit.yaml").read_text())
    assert preserved["run_id"] == "prior-generation"
    # And the drop's own record is written beside it rather than discarded.
    drop_audits = list(log_dir.glob("audit-abnormal-*.yaml"))
    assert len(drop_audits) == 1
    drop_record = yaml.safe_load(drop_audits[0].read_text())
    assert drop_record["abnormal_termination"]["kind"] == ABNORMAL_LAUNCH_GUARD_DROP


def test_drop_does_not_mark_the_story_escalated(tmp_path: Path) -> None:
    """A story that never ran did not escalate; its worktree must not say so."""
    _run_sprint_with_drop(tmp_path, reason=REASON_ACTIVE_WORKTREE)

    record = _story_audits(tmp_path)["issue-2048"]
    assert record["outcome"]["final_phase"] != "ESCALATE"
    assert not (tmp_path / "issue-2048" / ".forge" / "escalated").exists()


# ── worker exception and timeout records stay intact ─────────────────


def test_worker_exception_audit_names_the_exception(tmp_path: Path) -> None:
    """The pre-existing worker-exception record keeps working, and gains a kind."""
    _make_spec_file(tmp_path, "Issue 2054", "issue-2054")
    manifest_path = _make_manifest(tmp_path, ["issue-2054.md"])
    config = _make_config(tmp_path)

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("advisory artifact missing")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", side_effect=_boom),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint_ctx(config, manifest_path, run_id="run-2107")

    record = _story_audits(tmp_path)["issue-2054"]
    assert "advisory artifact missing" in record["error"]
    assert record["error_type"] == "FileNotFoundError"
    assert record["abnormal_termination"]["kind"] == ABNORMAL_WORKER_EXCEPTION
    assert "advisory artifact missing" in record["abnormal_termination"]["cause"]

    # The same cause is retained in the sprint's accumulated state, which is
    # where a later generation would otherwise overwrite it.
    history = _accumulated_story(tmp_path, "issue-2054")["failure_history"]
    # One attempt, one cause: the row is written by several writers in one
    # generation and they must not each append their own copy.
    assert len(history) == 1
    assert history[0]["kind"] == ABNORMAL_WORKER_EXCEPTION
    assert history[0]["attempt"] == 1
    assert "advisory artifact missing" in history[0]["cause"]


def test_worker_exception_cause_survives_a_later_successful_generation(tmp_path: Path) -> None:
    """#2107's exact shape: the resume after a worker exception must not erase it.

    The exception text lived only in the sprint state file, and the resume that
    followed rewrote it — so the record the issue was filed from no longer
    existed on disk by the time anyone looked.
    """
    _make_spec_file(tmp_path, "Issue 2054", "issue-2054")
    manifest_path = _make_manifest(tmp_path, ["issue-2054.md"])
    config = _make_config(tmp_path)

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("advisory artifact missing")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", side_effect=_boom),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint_ctx(config, manifest_path, run_id="run-first")

    _run_sprint_landing(tmp_path, manifest_path, run_id="run-second")

    after = _accumulated_story(tmp_path, "issue-2054")
    assert after["outcome"] in {"DONE", "ALREADY_DONE"}
    assert after["failure_history"][0]["kind"] == ABNORMAL_WORKER_EXCEPTION, (
        "the successful generation erased the worker exception's recorded cause"
    )
    assert "advisory artifact missing" in after["failure_history"][0]["cause"]


def _run_sprint_to_worker_timeout(tmp_path: Path, manifest_path: Path) -> None:
    """Run the sprint with a worker that never returns, so its deadline expires."""
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
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", return_value=(set(), set())),
        patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 4000.0, 4000.0]),
    ):
        run_sprint_ctx(config, manifest_path)


def test_worker_timeout_audit_names_the_timeout(tmp_path: Path) -> None:
    """The pre-existing worker-timeout record keeps working, and gains a kind."""
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md"])

    _run_sprint_to_worker_timeout(tmp_path, manifest_path)

    record = _story_audits(tmp_path)["feature-a"]
    assert record["abnormal_termination"]["kind"] == ABNORMAL_WORKER_TIMEOUT
    # The cause names deadline exhaustion, so an operator reads a wall-clock
    # outcome rather than a verdict on the work (#2333).
    assert "Story deadline exhausted" in record["abnormal_termination"]["cause"]

    history = _accumulated_story(tmp_path, "feature-a")["failure_history"]
    assert history[0]["kind"] == ABNORMAL_WORKER_TIMEOUT
    assert "Story deadline exhausted" in history[0]["cause"]


def test_worker_timeout_cause_survives_a_later_successful_generation(tmp_path: Path) -> None:
    """A story that later succeeds keeps the record of the attempt that timed out."""
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md"])

    _run_sprint_to_worker_timeout(tmp_path, manifest_path)
    _run_sprint_landing(tmp_path, manifest_path, run_id="run-second")

    after = _accumulated_story(tmp_path, "feature-a")
    assert after["outcome"] in {"DONE", "ALREADY_DONE"}
    assert after["failure_history"][0]["kind"] == ABNORMAL_WORKER_TIMEOUT, (
        "the successful generation erased the timed-out attempt's recorded cause"
    )


# ── a later attempt must not erase an earlier cause ──────────────────


def test_resume_keeps_the_prior_attempts_cause(tmp_path: Path) -> None:
    """The sprint's accumulated state retains every attempt's cause, not the last.

    Sprint ``7272950c8b34`` lost the only record of a worker exception because
    the resume rewrote ``.forge/sprints/<id>/state.yaml`` in place.
    """
    prior = {
        "canonical_ref": "spec:issue-2054.md",
        "slug": "issue-2054",
        "outcome": "FAILED",
        "error": "Worker exception during phase DEV: FileNotFoundError(advisory)",
        "error_type": "FileNotFoundError",
        "story_run_id": "run-first",
    }
    current = {
        "canonical_ref": "spec:issue-2054.md",
        "slug": "issue-2054",
        "outcome": "DROPPED",
        "error": "active-worktree-collision",
        "error_type": "dropped",
        "story_run_id": "run-second",
    }

    history = accumulate_failure_history(prior, current)

    causes = [entry["cause"] for entry in history]
    assert any("FileNotFoundError(advisory)" in cause for cause in causes), (
        "the resume erased the earlier attempt's cause"
    )
    assert any("active-worktree-collision" in cause for cause in causes)
    assert [entry["attempt"] for entry in history] == [1, 2]
    assert [entry["run_id"] for entry in history] == ["run-first", "run-second"]


def test_accumulated_history_is_idempotent_across_rewrites(tmp_path: Path) -> None:
    """Re-persisting the same generation does not inflate the attempt count."""
    first = {
        "outcome": "FAILED",
        "error": "Worker timeout (>3600s) during phase DEV",
        "error_type": "TimeoutError",
        "story_run_id": "run-a",
    }
    once = accumulate_failure_history(None, first)
    twice = accumulate_failure_history({**first, "failure_history": once}, first)

    assert len(once) == 1
    assert len(twice) == 1


def test_success_entries_contribute_no_failure_cause() -> None:
    """A story that succeeded has no cause to retain."""
    assert derive_failure_cause({"outcome": "DONE", "error": None}) is None
    assert derive_failure_cause({"outcome": "DONE", "error": "stale prose"}) is None
    assert derive_failure_cause({"outcome": "FAILED", "error": "real cause"}) is not None


def test_story_state_appends_attempt_scoped_causes() -> None:
    """A retry adds a cause row; it does not overwrite the first one."""
    state = SprintStoryState()
    state.register("issue-2054", "Issue #2054")

    first = build_abnormal_cause(
        kind=ABNORMAL_WORKER_EXCEPTION,
        cause="Worker exception during phase DEV: boom",
        run_id="run-first",
    )
    second = build_abnormal_cause(
        kind=ABNORMAL_LAUNCH_GUARD_DROP,
        cause="active-worktree-collision",
        run_id="run-second",
    )
    state.transition("issue-2054", outcome=StoryOutcome.FAILED, failure_cause=first)
    state.transition("issue-2054", outcome=StoryOutcome.DROPPED, failure_cause=second)

    history = state.get("issue-2054").failure_history
    assert [entry["attempt"] for entry in history] == [1, 2]
    assert history[0]["cause"] == "Worker exception during phase DEV: boom"

    # And it survives a serialize/reload round trip, which is how a later
    # generation reads what the earlier one recorded.
    reloaded = SprintStoryState.from_dict(state.as_dict())
    assert reloaded.get("issue-2054").failure_history == history


def test_story_state_omits_failure_history_when_there_is_none() -> None:
    """Stories that never failed do not grow an empty evidence key."""
    state = SprintStoryState()
    state.register("issue-2054", "Issue #2054")
    state.transition("issue-2054", outcome=StoryOutcome.DONE)

    assert "failure_history" not in state.as_dict()[0]
    # A caller with nothing to record must not leave a null behind either.
    state.transition("issue-2054", failure_cause=None)
    assert "failure_cause" not in state.as_dict()[0]


def test_second_generation_does_not_erase_the_first_generations_cause(tmp_path: Path) -> None:
    """Seam test: a re-run of the same sprint keeps the earlier attempt's cause.

    The accumulated sprint state is rewritten wholesale by each generation, so
    this is the boundary where the record of a failed attempt was destroyed by
    the attempt that followed it.
    """
    _run_sprint_with_drop(tmp_path, reason=REASON_ACTIVE_WORKTREE, run_id="run-first")

    def _state() -> dict:
        state_file = next((tmp_path / ".forge" / "sprints").glob("*/state.yaml"))
        stories = yaml.safe_load(state_file.read_text())["stories"]
        return {s["slug"]: s for s in stories}["issue-2048"]

    assert _state()["failure_history"][0]["kind"] == ABNORMAL_LAUNCH_GUARD_DROP

    # Second generation: the same story is dispatched and lands.
    manifest_path = tmp_path / "sprint.yaml"
    config = _make_config(tmp_path)
    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        run_sprint_ctx(config, manifest_path, run_id="run-second")

    after = _state()
    assert after["outcome"] in {"DONE", "ALREADY_DONE"}
    assert after["failure_history"][0]["kind"] == ABNORMAL_LAUNCH_GUARD_DROP, (
        "the second generation erased the first generation's recorded cause"
    )
    assert REASON_ACTIVE_WORKTREE in after["failure_history"][0]["cause"]


def test_sprint_state_file_records_the_drop_cause(tmp_path: Path) -> None:
    """The dropped story's cause reaches the sprint's accumulated state file."""
    _run_sprint_with_drop(tmp_path, reason=REASON_ACTIVE_WORKTREE)

    state_files = list((tmp_path / ".forge" / "sprints").glob("*/state.yaml"))
    assert state_files, "no accumulated sprint state was persisted"
    stories = yaml.safe_load(state_files[0].read_text())["stories"]
    dropped = {s["slug"]: s for s in stories}["issue-2048"]

    history = dropped["failure_history"]
    # One attempt, one cause: the structured record must not be double-counted
    # against the same failure's error prose.
    assert len(history) == 1
    assert history[0]["kind"] == ABNORMAL_LAUNCH_GUARD_DROP
    assert REASON_ACTIVE_WORKTREE in history[0]["cause"]
    assert history[0]["attempt"] == 1
