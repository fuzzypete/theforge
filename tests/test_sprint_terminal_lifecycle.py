"""Terminal-state consistency for sprints that time out, fail, or are stopped (#2013).

A sprint that dies mid-VALIDATE used to leave every operator surface disagreeing
with every other: the ``.state`` file carried a terminal story status next to a
running sprint phase and a running gate, ``forge stop`` reported success while the
gate's subprocess tree kept running, the timeout audit lost the dev/gate history
that had actually happened, and ``forge logs --story <slug>`` could not open a
single slug it had just listed.

These tests pin each of those seams independently, because they live in different
modules and fail in different ways.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import yaml

from theforge.coordinator import util as coord_util
from theforge.coordinator.live_state import (
    register_live_state,
    release_live_state,
    snapshot_live_state,
)
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.process_group import reap_orphan_agents
from theforge.sprint.state_writer import (
    SPRINT_PHASE_STOPPED,
    SprintStateWriter,
    is_terminal_sprint_phase,
    terminalize_state_file,
)
from theforge.sprint.story_state import SprintStoryState, StoryOutcome


def _read_state(project_root: Path, run_id: str) -> dict:
    with open(project_root / ".forge" / "runs" / f"{run_id}.state", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _story(state: dict, slug: str) -> dict:
    return next(s for s in state["stories"] if s["slug"] == slug)


# ── Canonical structure: a terminal story cannot carry a running gate ──


def test_terminal_story_never_serializes_a_running_gate() -> None:
    state = SprintStoryState()
    state.register("issue-50", "Issue #50", detail={"gate_status": "running"})
    state.transition("issue-50", outcome=StoryOutcome.FAILED)

    serialized = state.as_dict()[0]
    assert serialized["status"] == "failed"
    assert serialized["detail"]["gate_status"] == "incomplete"


def test_nonterminal_story_keeps_its_running_gate() -> None:
    """The guard is about terminal rows only — a live VALIDATE still reads running."""
    state = SprintStoryState()
    state.register("issue-50", "Issue #50", detail={"gate_status": "running"})
    state.transition("issue-50", outcome=StoryOutcome.RUNNING, phase="VALIDATE")

    assert state.as_dict()[0]["detail"]["gate_status"] == "running"


def test_detail_updates_merges_instead_of_replacing() -> None:
    state = SprintStoryState()
    state.register("issue-50", "Issue #50", detail={"gate_status": "running", "pr": "#7"})
    state.transition("issue-50", detail_updates={"gate_status": "timeout"})

    entry = state.get("issue-50")
    assert entry is not None
    assert entry.detail == {"gate_status": "timeout", "pr": "#7"}


# ── State writer: stranded stories are terminalized ────────────────────


def test_terminalize_stories_moves_only_stranded_rows(tmp_path: Path) -> None:
    writer = SprintStateWriter("run1", tmp_path, "sprint-a")
    writer.init(
        [
            {"slug": "issue-50", "path": "Issue #50", "status": "running"},
            {"slug": "issue-230", "path": "Issue #230", "status": "done"},
        ]
    )
    writer.update("issue-50", phase="VALIDATE", detail={"gate_status": "running"})

    stranded = writer.terminalize_stories(reason="sprint ended")

    assert stranded == ["issue-50"]
    state = _read_state(tmp_path, "run1")
    stalled = _story(state, "issue-50")
    assert stalled["status"] == "failed"
    assert stalled["outcome"] == "failed"
    assert stalled["detail"]["gate_status"] == "incomplete"
    assert stalled["reason"] == "sprint ended"
    assert stalled["finished_at"]
    # An already-terminal story is untouched.
    assert _story(state, "issue-230")["status"] == "done"


def test_terminal_sprint_phase_is_written_to_disk(tmp_path: Path) -> None:
    writer = SprintStateWriter("run1", tmp_path, "sprint-a")
    writer.init([{"slug": "issue-50", "path": "Issue #50", "status": "failed"}])
    writer.set_phase("running")
    assert not is_terminal_sprint_phase(_read_state(tmp_path, "run1")["sprint_phase"])

    writer.set_phase("failed")

    assert is_terminal_sprint_phase(_read_state(tmp_path, "run1")["sprint_phase"])


# ── forge stop: the surviving state file is made terminal ──────────────


def test_terminalize_state_file_marks_sprint_and_stories_stopped(tmp_path: Path) -> None:
    writer = SprintStateWriter("run1", tmp_path, "sprint-a")
    writer.init(
        [
            {"slug": "issue-50", "path": "Issue #50", "status": "running"},
            {"slug": "issue-230", "path": "Issue #230", "status": "done"},
        ]
    )
    writer.update("issue-50", phase="VALIDATE", detail={"gate_status": "running"})
    writer.set_phase("running")

    stranded = terminalize_state_file("run1", tmp_path)

    assert stranded == ["issue-50"]
    state = _read_state(tmp_path, "run1")
    assert state["sprint_phase"] == SPRINT_PHASE_STOPPED
    stopped = _story(state, "issue-50")
    assert stopped["status"] == "failed"
    assert stopped["phase"] == "STOPPED"
    assert stopped["reason"] == "stopped"
    assert stopped["detail"]["gate_status"] == "stopped"
    assert _story(state, "issue-230")["status"] == "done"


def test_terminalize_state_file_is_a_noop_without_a_state_file(tmp_path: Path) -> None:
    assert terminalize_state_file("missing", tmp_path) == []


def test_cleanup_stopped_run_terminalizes_before_reaping(tmp_path: Path) -> None:
    from theforge.cli import status as cli_status

    writer = SprintStateWriter("run1", tmp_path, "sprint-a")
    writer.init([{"slug": "issue-50", "path": "Issue #50", "status": "running"}])
    writer.set_phase("running")

    order: list[str] = []

    def _fake_reap(project_root: Path) -> int:
        order.append("reap")
        state = _read_state(tmp_path, "run1")
        # Terminal state must already be on disk when the reaper runs, so an
        # operator reading .state right after stop never sees a live sprint.
        assert state["sprint_phase"] == SPRINT_PHASE_STOPPED
        return 0

    with (
        patch("theforge.process_group.reap_orphan_agents", side_effect=_fake_reap),
        patch("theforge.sprint.lock.cleanup_story_locks", side_effect=lambda *a, **k: None),
    ):
        cli_status._cleanup_stopped_run("run1", tmp_path, "issue-50", pid=424242)

    assert order == ["reap"]
    state = _read_state(tmp_path, "run1")
    assert _story(state, "issue-50")["status"] == "failed"
    assert _story(state, "issue-50")["detail"]["gate_status"] == "stopped"


# ── Gate subprocesses are reachable by the orphan reaper ───────────────


def test_gate_subprocess_registers_its_process_group(tmp_path: Path, monkeypatch) -> None:
    """The gate's own session must leave a sidecar the reaper can kill it by."""
    monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
    agents_dir = tmp_path / ".forge" / "runs" / "agents"
    seen: list[dict] = []

    real_register = coord_util.register_agent_group

    def _record(pgid: int, *, sandbox_dir=None) -> None:
        real_register(pgid, sandbox_dir=sandbox_dir)
        payload = {"pgid": pgid, "sandbox_dir": sandbox_dir}
        payload["sidecar_written"] = any(agents_dir.glob("*.json"))
        seen.append(payload)

    monkeypatch.setattr(coord_util, "register_agent_group", _record)

    ok, _output = coord_util._run_shell("echo hello", tmp_path, timeout=30)

    assert ok is True
    assert len(seen) == 1
    assert seen[0]["pgid"] > 1
    assert seen[0]["sandbox_dir"] == str(tmp_path)
    assert seen[0]["sidecar_written"] is True
    # A cleanly finished command leaves no orphan record behind.
    assert list(agents_dir.glob("*.json")) == []


def test_gate_sidecar_is_kept_when_the_group_kill_is_refused(tmp_path: Path, monkeypatch) -> None:
    """A survivor must stay reachable: the sidecar is the only handle on it."""
    monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
    # Stand in for a refused group kill (a sandbox that denies killpg): the
    # direct child may be gone while its grandchildren are not.
    monkeypatch.setattr(coord_util, "_kill_process_group", lambda proc: False)

    ok, output = coord_util._run_shell(
        'python3 -c "import time; time.sleep(30)"', tmp_path, timeout=0.2
    )

    assert ok is False
    assert output.startswith("TIMEOUT")
    sidecars = list((tmp_path / ".forge" / "runs" / "agents").glob("*.json"))
    assert len(sidecars) == 1
    record = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert record["pgid"] > 1
    assert record["owner_pid"] == os.getpid()
    assert record["sandbox_dir"] == str(tmp_path)
    # Leave nothing running behind this assertion.
    from theforge.process_group import kill_agent_group

    kill_agent_group(record["pgid"])


# ── Timeout audits keep the history the run actually produced ──────────


def test_snapshot_live_state_detaches_from_the_running_worker() -> None:
    state = CoordinatorState(run_id="abc123")
    state.gate_decisions.append("FAIL")
    register_live_state("issue-50", state)
    try:
        snapshot = snapshot_live_state("issue-50")
    finally:
        release_live_state("issue-50", state)

    assert snapshot is not None
    assert snapshot.run_id == "abc123"
    assert snapshot.gate_decisions == ["FAIL"]
    assert snapshot is not state
    assert snapshot_live_state("issue-50") is None


def test_release_live_state_ignores_a_superseded_object() -> None:
    first = CoordinatorState(run_id="first")
    second = CoordinatorState(run_id="second")
    register_live_state("issue-50", first)
    register_live_state("issue-50", second)
    release_live_state("issue-50", first)
    try:
        current = snapshot_live_state("issue-50")
        assert current is not None
        assert current.run_id == "second"
    finally:
        release_live_state("issue-50")


def test_timeout_state_preserves_dev_and_gate_history(tmp_path: Path) -> None:
    """The audit for a timed-out story keeps the telemetry the engine accumulated."""
    import datetime

    from theforge.sprint import runner as sprint_runner

    live = CoordinatorState(run_id="deadbeef1234")
    live.phase = Phase.VALIDATE
    live.started_at = "2026-07-28T00:00:00+00:00"
    live.gate_decisions.extend(["FAIL", "FAIL"])
    live.dev_durations.extend([12.0, 30.0])
    live.workspace_path = tmp_path / "ws"
    live.log_dir = tmp_path / "logs"

    register_live_state("issue-50", live)
    try:
        with patch.object(sprint_runner, "_make_story_log_dir", return_value=tmp_path / "logs"):
            state = sprint_runner._terminal_state_for(
                "issue-50",
                config=_MinimalConfig(tmp_path),
                sprint_name="sprint-a",
                started_at=datetime.datetime(2026, 7, 28, tzinfo=datetime.timezone.utc),
                error="Worker timeout (>3600s) during phase VALIDATE",
                error_type="TimeoutError",
            )
    finally:
        release_live_state("issue-50", live)

    assert state.run_id == "deadbeef1234"
    assert state.gate_decisions == ["FAIL", "FAIL"]
    assert state.dev_durations == [12.0, 30.0]
    # ...but the story is reported as the scheduler ended it, not mid-VALIDATE.
    assert state.phase is Phase.ESCALATE
    assert state.error_type == "TimeoutError"


def test_timeout_state_falls_back_when_the_story_never_reached_the_engine(
    tmp_path: Path,
) -> None:
    import datetime

    from theforge.sprint import runner as sprint_runner

    with patch.object(sprint_runner, "_make_story_log_dir", return_value=tmp_path / "logs"):
        state = sprint_runner._terminal_state_for(
            "issue-nonexistent",
            config=_MinimalConfig(tmp_path),
            sprint_name="sprint-a",
            started_at=datetime.datetime(2026, 7, 28, tzinfo=datetime.timezone.utc),
            error="Worker exception: boom",
            error_type="RuntimeError",
        )

    assert state.run_id is None
    assert state.phase is Phase.ESCALATE
    assert state.workspace_path == tmp_path / "issue-nonexistent"


class _MinimalConfig:
    """Just the two attributes ``_terminal_state_for`` reads off the config."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.workspace = _MinimalWorkspace()


class _MinimalWorkspace:
    path_pattern = "{slug}"


# ── Every listed story slug can actually be tailed ─────────────────────


def _make_story_logs(project_root: Path, sprint: str, slug: str, names: list[str]) -> Path:
    story_dir = project_root / ".forge" / "logs" / sprint / slug
    story_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (story_dir / name).write_text("content\n", encoding="utf-8")
    return story_dir


def test_story_log_resolves_to_a_dev_iteration_log(tmp_path: Path) -> None:
    from theforge.cli.status import _find_story_log_path

    _make_story_logs(
        tmp_path,
        "issues-50,230",
        "issue-50",
        ["dev-iter-1-gpt.log", "dev-iter-2-gpt.log", "audit.yaml", "preflight.yaml"],
    )

    found = _find_story_log_path(tmp_path, "issue-50", "issues-50,230")

    assert found is not None
    assert found.name.startswith("dev-iter-")


def test_story_log_falls_back_to_audit_yaml(tmp_path: Path) -> None:
    from theforge.cli.status import _find_story_log_path

    _make_story_logs(tmp_path, "issues-50,230", "issue-230", ["audit.yaml", "preflight.yaml"])

    found = _find_story_log_path(tmp_path, "issue-230", "issues-50,230")

    assert found is not None
    assert found.name == "audit.yaml"


def test_story_log_prefers_run_log_when_present(tmp_path: Path) -> None:
    from theforge.cli.status import _find_story_log_path

    _make_story_logs(tmp_path, "solo", "issue-9", ["run-abc.log", "dev-iter-1-gpt.log"])

    found = _find_story_log_path(tmp_path, "issue-9", "solo")

    assert found is not None
    assert found.name == "run-abc.log"


def test_story_log_resolves_without_a_known_sprint_name(tmp_path: Path) -> None:
    from theforge.cli.status import _find_story_log_path

    _make_story_logs(tmp_path, "issues-50,230", "issue-50", ["dev-iter-1-gpt.log"])

    found = _find_story_log_path(tmp_path, "issue-50", None)

    assert found is not None
    assert found.name == "dev-iter-1-gpt.log"


def test_every_listed_slug_opens(tmp_path: Path) -> None:
    """The enumerator and the tailer must agree — that is the whole contract."""
    from theforge.cli.status import _find_story_log_path

    sprint = "issues-50,230"
    _make_story_logs(tmp_path, sprint, "issue-50", ["dev-iter-1-gpt.log", "audit.yaml"])
    _make_story_logs(tmp_path, sprint, "issue-230", ["audit.yaml"])

    writer = SprintStateWriter("run1", tmp_path, sprint)
    writer.init(
        [
            {"slug": "issue-50", "path": "Issue #50", "status": "failed"},
            {"slug": "issue-230", "path": "Issue #230", "status": "failed"},
        ]
    )

    from theforge.sprint.status_reader import read_live_status

    listed = [e.slug for e in (read_live_status("run1", tmp_path) or []) if e.slug]
    assert listed == ["issue-50", "issue-230"]
    for slug in listed:
        path = _find_story_log_path(tmp_path, slug, sprint)
        assert path is not None and path.exists(), f"listed slug {slug} is not tailable"


# ── End-to-end: a timed-out sprint leaves terminal live state ──────────


def test_timed_out_sprint_writes_terminal_state_before_teardown(tmp_path: Path) -> None:
    """The .state file a crash or a stop would find must already read terminal."""
    import threading

    from tests.test_sprint_resume import _make_config, _make_manifest, _make_spec_file
    from theforge.sprint import run_sprint

    config = _make_config(tmp_path)
    spec = _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest = _make_manifest(tmp_path, [spec.name])

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

        def submit(self, fn, *args, **kwargs):
            assert isinstance(args[-1], threading.Event)
            return _NeverDoneFuture()

    # The engine registers its live state for the story it is running; a worker
    # that times out never returns, so this registry is the only way its
    # accumulated history can reach the audit the scheduler writes for it.
    live = CoordinatorState(run_id="livedbeef999")
    live.gate_decisions.extend(["FAIL", "FAIL"])
    register_live_state("feature-a", live)

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
        # Keep the live state on disk so the test can read what a run killed
        # during wrap-up would have left behind.
        patch.object(SprintStateWriter, "remove", lambda self: None),
    ):
        try:
            run_sprint(config, manifest, run_id="terminalrun01")
        finally:
            release_live_state("feature-a", live)

    audit_path = next((tmp_path / ".forge" / "logs").rglob("audit.yaml"))
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    # AC (d): the timeout audit keeps the history the run really produced.
    assert audit["run_id"] == "livedbeef999"
    assert audit["iterations"]["gate_decisions"] == ["FAIL", "FAIL"]

    state = tmp_path / ".forge" / "runs" / "terminalrun01.state"
    data = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert is_terminal_sprint_phase(data["sprint_phase"])
    assert data["sprint_phase"] == "failed"
    story = data["stories"][0]
    assert story["status"] == "failed"
    assert story["detail"]["gate_status"] == "timeout"
    assert story["detail"]["gate_status"] != "running"


# ── The reaper really kills an orphaned gate tree ──────────────────────


def _pid_is_gone(pid: int) -> bool:
    """True once *pid* is neither alive nor a zombie this process must reap.

    ``os.kill(pid, 0)`` succeeds against a zombie, so a killed direct child
    reads as alive until its exit status is collected. Reaping it here is what
    makes the check mean "the process is gone" rather than "it has not been
    waited on yet".
    """
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return True
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _wait_until_gone(pids: list[int], timeout: float = 5.0) -> list[int]:
    """Poll until every pid in *pids* is gone; return whichever survived."""
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if not _pid_is_gone(pid)]
        if remaining:
            time.sleep(0.05)
    return remaining


def _dead_pid() -> int:
    """Return a pid that has certainly exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def test_reaper_kills_an_orphaned_gate_tree_after_the_owner_dies(
    tmp_path: Path, monkeypatch
) -> None:
    """The production symptom: an xcodebuild tree outliving the sprint that ran it.

    Runs the real registration path (``_run_shell_detailed`` spawning into its own
    session), neuters only the group kill so the tree genuinely survives teardown
    — the sandbox-refused-killpg case — then marks the owner sprint dead and lets
    ``reap_orphan_agents`` do what ``forge stop`` calls it to do. Both the gate
    shell and its grandchild must actually die (#2013).
    """
    monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
    # A refused group kill: teardown reaches nothing, exactly the state that
    # leaves a live tree behind for the reaper to find.
    monkeypatch.setattr(coord_util, "_kill_process_group", lambda proc: False)

    cmd = (
        f'{sys.executable} -u -c "import subprocess, sys, time; '
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(120)']); "
        'print(child.pid, flush=True); time.sleep(120)"'
    )
    ok, output, _exit_code, timed_out = coord_util._run_shell_detailed(cmd, tmp_path, timeout=2)

    assert ok is False and timed_out is True
    grandchild_pid = int(
        next(line for line in output.splitlines() if line.strip().isdigit()).strip()
    )

    sidecars = list((tmp_path / ".forge" / "runs" / "agents").glob("*.json"))
    assert len(sidecars) == 1, "the surviving group must still be registered"
    record = json.loads(sidecars[0].read_text(encoding="utf-8"))
    gate_pid = record["pgid"]

    try:
        assert not _pid_is_gone(grandchild_pid), "grandchild should still be running"

        # The owner sprint is gone — the state `forge stop` leaves behind.
        record["owner_pid"] = _dead_pid()
        sidecars[0].write_text(json.dumps(record), encoding="utf-8")

        reaped = reap_orphan_agents(tmp_path)

        assert reaped == 1
        assert _wait_until_gone([gate_pid, grandchild_pid]) == [], (
            "reap_orphan_agents left gate descendants alive — the exact #2013 symptom"
        )
        assert list((tmp_path / ".forge" / "runs" / "agents").glob("*.json")) == []
    finally:
        for pid in (gate_pid, grandchild_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass


# ── A stopped story's audit keeps the history it accumulated ───────────


def _live_story_state(tmp_path: Path, slug: str, sprint_name: str) -> CoordinatorState:
    state = CoordinatorState(run_id="stopdbeef777")
    state.phase = Phase.VALIDATE
    state.started_at = "2026-07-28T00:00:00+00:00"
    state.sprint_name = sprint_name
    state.gate_decisions.extend(["FAIL", "FAIL"])
    state.workspace_path = tmp_path / slug
    state.log_dir = tmp_path / ".forge" / "logs" / sprint_name / slug
    return state


def test_live_story_audit_is_flushed_while_the_story_runs(tmp_path: Path) -> None:
    from tests.test_sprint_resume import _make_config
    from theforge.sprint.audit import write_live_story_audit
    from theforge.task import TaskStory

    sprint_name = "issues-50,230"
    config = _make_config(tmp_path)
    task = TaskStory(name="Issue #50", slug="issue-50", story_text="do the thing")
    state = _live_story_state(tmp_path, "issue-50", sprint_name)

    path = write_live_story_audit(config, task, state, sprint_name=sprint_name)

    assert path is not None and path.exists()
    audit = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert audit["in_flight"] is True
    assert audit["run_id"] == "stopdbeef777"
    assert audit["iterations"]["gate_decisions"] == ["FAIL", "FAIL"]
    # The flush is a live observation, not a verdict: no substrate record and no
    # ESCALATE marker are written for a story that has not finished.
    assert not (tmp_path / ".forge" / "audits").exists()


def test_phase_change_flushes_the_story_audit_once_per_phase(tmp_path: Path) -> None:
    """Flush cost tracks real progress, not live-update chatter."""
    import threading

    from theforge.sprint import runner as sprint_runner

    flushed: list[str] = []
    update = sprint_runner._make_worker_phase_fn(
        "issue-50",
        {},
        threading.Lock(),
        None,
        audit_flush=flushed.append,
    )

    update({"phase": "DEV"})
    update({"phase": "DEV", "cost_usd": 0.2})
    update({"cost_usd": 0.4})
    update({"phase": "VALIDATE"})

    assert flushed == ["DEV", "VALIDATE"]


def test_stop_finalizes_the_in_flight_audit_without_losing_history(tmp_path: Path) -> None:
    """AC (d) for the operator-stop path, end to end.

    The sprint process flushes the story's audit as it runs; ``forge stop`` — a
    different process, which cannot see that run's memory — finalizes the file it
    left behind. The accumulated run_id/gate history must survive that handoff.
    """
    from tests.test_sprint_resume import _make_config
    from theforge.cli import status as cli_status
    from theforge.sprint.audit import write_live_story_audit
    from theforge.task import TaskStory

    sprint_name = "issues-50,230"
    config = _make_config(tmp_path)
    task = TaskStory(name="Issue #50", slug="issue-50", story_text="do the thing")
    audit_path = write_live_story_audit(
        config, task, _live_story_state(tmp_path, "issue-50", sprint_name), sprint_name=sprint_name
    )
    assert audit_path is not None

    writer = SprintStateWriter("run1", tmp_path, sprint_name)
    writer.init([{"slug": "issue-50", "path": "Issue #50", "status": "running"}])
    writer.update("issue-50", phase="VALIDATE", detail={"gate_status": "running"})
    writer.set_phase("running")

    with (
        patch("theforge.process_group.reap_orphan_agents", return_value=0),
        patch("theforge.sprint.lock.cleanup_story_locks", side_effect=lambda *a, **k: None),
    ):
        cli_status._cleanup_stopped_run("run1", tmp_path, "issue-50", pid=424242)

    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert audit["in_flight"] is False
    assert audit["interrupted_by"] == "stopped"
    assert audit["outcome"]["success"] is False
    assert audit["outcome"]["error_type"] == "OperatorStopped"
    assert "VALIDATE" in audit["outcome"]["message"]
    assert audit["timing"]["finished_at"]
    # The point of the whole exercise: the history is still there.
    assert audit["run_id"] == "stopdbeef777"
    assert audit["iterations"]["gate_decisions"] == ["FAIL", "FAIL"]


def test_stop_does_not_overwrite_a_finished_story_audit(tmp_path: Path) -> None:
    """A completed story wrote the real audit; stop must not guess over it."""
    from theforge.cli import status as cli_status
    from theforge.sprint.audit import finalize_interrupted_story_audit

    sprint_name = "issues-50,230"
    audit_path = tmp_path / ".forge" / "logs" / sprint_name / "issue-230" / "audit.yaml"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    final_audit = {
        "run_id": "finished9999",
        "outcome": {"success": True, "final_phase": "DONE", "message": "approved"},
    }
    audit_path.write_text(yaml.dump(final_audit), encoding="utf-8")

    assert finalize_interrupted_story_audit(tmp_path, sprint_name, "issue-230") is None
    assert yaml.safe_load(audit_path.read_text(encoding="utf-8")) == final_audit

    # ...and the same holds through the stop path itself.
    writer = SprintStateWriter("run1", tmp_path, sprint_name)
    writer.init([{"slug": "issue-230", "path": "Issue #230", "status": "done"}])
    with (
        patch("theforge.process_group.reap_orphan_agents", return_value=0),
        patch("theforge.sprint.lock.cleanup_story_locks", side_effect=lambda *a, **k: None),
    ):
        cli_status._cleanup_stopped_run("run1", tmp_path, "issue-230", pid=424242)

    assert yaml.safe_load(audit_path.read_text(encoding="utf-8")) == final_audit
