"""Process-group isolation and reaping for agent subprocesses.

Agent CLIs (claude, codex, gemini) spawn descendant processes through their
tool layer — the Bash tool, in particular, forks arbitrary shell commands. A
plain ``proc.kill()`` SIGKILLs only the immediate agent process; a descendant
can inherit the agent's stdout pipe write-end and hold it open after the agent
itself is dead. The parent's blocking read (``for line in proc.stdout``) then
never sees EOF and hangs indefinitely — long past the configured timeout
(observed: a 900s plan timeout that ran 3h47m before the orphan finally
exited; #1672).

The fix is to launch the agent as its own process-group leader
(``start_new_session=True``) and, at kill time, signal the *whole group* with
``os.killpg`` instead of the single process. Killing the group closes every
inherited fd — including the orphan's copy of the stdout write-end — which
delivers EOF and unblocks the read within the timeout window.
"""

from __future__ import annotations

import os
import signal
import subprocess

# Passed to subprocess.Popen/subprocess.run for agent invocations. On POSIX
# this makes the child a session (and process-group) leader whose PGID equals
# its PID, so os.killpg(pid, ...) targets the child and every descendant it
# spawns. Empty on platforms without setsid (Windows), where killpg is also
# unavailable and kill_process_group() falls back to proc.kill().
NEW_PROCESS_GROUP_KWARGS: dict[str, bool] = (
    {"start_new_session": True} if hasattr(os, "setsid") else {}
)


def kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL ``proc`` and every descendant sharing its process group.

    ``proc`` MUST have been started with ``start_new_session=True`` (see
    ``NEW_PROCESS_GROUP_KWARGS``) so that its PGID equals its PID; otherwise
    the process group would be the parent's and killing it would take down the
    orchestrator itself. Call this *before* ``proc.wait()`` so the leader is
    still a resolvable (zombie) process and its PGID is intact.

    Falls back to ``proc.kill()`` when ``os.killpg`` is unavailable (non-POSIX)
    or the group has already been reaped.
    """
    killpg = getattr(os, "killpg", None)
    pid = proc.pid
    if killpg is not None and isinstance(pid, int):
        try:
            # start_new_session=True guarantees PGID == PID for the leader.
            killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            # Group already gone, or we lack permission — fall through to the
            # single-process kill, which is a strict subset of what killpg does.
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
