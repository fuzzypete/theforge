"""Tests for process-group isolation and reaping of agent subprocesses (#1672).

The PLAN runner hung for 3h47m against a 900s timeout because a Bash-tool
descendant inherited the agent's stdout pipe and held it open after the parent
`claude` process was SIGKILLed — so the coordinator's blocking
`for line in proc.stdout` read never saw EOF. Launching the agent in its own
process group and killing the whole group at the deadline reaps such orphans
and delivers EOF promptly.
"""

import os
import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest

from theforge.runners.process_group import NEW_PROCESS_GROUP_KWARGS, kill_process_group


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
def test_kill_process_group_unblocks_read_held_open_by_descendant() -> None:
    # The shell forks a background `sleep` that inherits stdout and keeps the
    # pipe's write end open, then the shell itself exits. A plain kill of the
    # shell would leave `sleep` holding stdout, so reading to EOF blocks for the
    # full sleep. kill_process_group must take the descendant down too.
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 60 & echo parent-done"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **NEW_PROCESS_GROUP_KWARGS,
    )
    assert proc.stdout is not None

    # Let the background child start and inherit the stdout fd.
    time.sleep(0.5)

    captured: dict[str, str] = {}

    def _read() -> None:
        captured["data"] = proc.stdout.read()  # blocks until every writer closes

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()

    # The read must still be blocked: the `sleep 60` descendant holds stdout.
    reader.join(timeout=1.0)
    assert reader.is_alive(), "stdout hit EOF before the descendant was killed"

    kill_process_group(proc)
    proc.wait(timeout=5)

    # Killing the group closed the descendant's fd → EOF → read returns promptly.
    reader.join(timeout=5.0)
    assert not reader.is_alive(), "kill_process_group did not unblock the stdout read"
    assert "parent-done" in captured["data"]


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
def test_kill_process_group_reaps_the_whole_tree() -> None:
    # Capture a marker file the descendant would write if it survived past the
    # kill; its absence proves the descendant was reaped, not just the parent.
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo started; wait"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **NEW_PROCESS_GROUP_KWARGS,
    )
    time.sleep(0.5)
    child_pgid = os.getpgid(proc.pid)

    kill_process_group(proc)
    proc.wait(timeout=5)

    # The group must be gone: signalling it with 0 raises ProcessLookupError.
    with pytest.raises(ProcessLookupError):
        os.killpg(child_pgid, 0)


def test_kill_process_group_tolerates_non_int_pid() -> None:
    # A MagicMock proc (pid is not an int) must not raise; it falls back to
    # proc.kill(). Guards the runner's existing MagicMock-based timeout tests.
    mock_proc = MagicMock()
    kill_process_group(mock_proc)
    mock_proc.kill.assert_called_once()


def test_new_process_group_kwargs_requests_new_session_on_posix() -> None:
    if hasattr(os, "setsid"):
        assert NEW_PROCESS_GROUP_KWARGS == {"start_new_session": True}
    else:
        assert NEW_PROCESS_GROUP_KWARGS == {}
