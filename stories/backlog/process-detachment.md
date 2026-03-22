---
name: "Process detachment — every forge run survives terminal close"
slug: process-detachment
pytest_target: tests/
---

# Process Detachment

## Problem

`forge run` and `forge sprint` die when the terminal closes, Mac sleeps, or
App Nap activates. The current daemon story shipped a full server (unix socket,
queue, PID management) but the actual need is simpler: every forge process
should survive its parent terminal.

The socket-based daemon also runs stale code (it was started before the latest
commit) and requires explicit `forge daemon start` per project.

## Solution

Replace the explicit daemon with automatic process detachment. Every `forge run`
and `forge sprint` forks, detaches from the terminal (setsid), and runs in the
background by default. No server, no socket, no queue.

### Behavior

```bash
forge run story.md              # daemonizes, prints run ID, returns
forge run story.md --fg         # foreground (current behavior)
forge sprint manifest.yaml      # daemonizes
forge sprint manifest.yaml --fg # foreground
```

On launch:
1. Fork + setsid (double-fork on macOS for clean detachment)
2. Redirect stdout/stderr to `.forge/logs/<slug>/run.log`
3. Write PID to `.forge/runs/<run-id>.pid`
4. Disable App Nap via `NSProcessInfo` (macOS) or `IOPMAssertionCreateWithName`
5. Print run ID and log path to the original terminal, then exit parent

### `forge status`

Scans `.forge/runs/*.pid` and `.forge/logs/` to show active runs:

```
$ forge status
RUN ID      STORY                          PHASE     COST    ELAPSED
a1b2c3d4    adaptive-model-assignment      REVIEW    $3.42   12m
e5f6g7h8    handoff-integrity              DEV       $1.20    8m

2 active runs. Use 'forge logs <run-id>' for live output.
```

Implementation: check if PID is alive, read the latest forge.log entry for
that run_id to get phase/cost/duration.

### `forge logs <run-id>`

Tail the run's log file (equivalent to `tail -f .forge/logs/<slug>/run.log`).

### `forge stop <run-id>`

Send SIGTERM to the PID. The existing crash diagnostics handler logs context
and exits cleanly.

### Cleanup

On completion (DONE/ESCALATE), the process removes its PID file. Stale PID
files (process not running) are cleaned up by `forge status`.

### No changes to sprint parallel execution

`forge sprint` with `max_parallel: 3` still uses ThreadPoolExecutor internally.
The daemonization wraps the entire sprint process, not individual stories.

## Acceptance criteria

- `forge run` daemonizes by default, prints run ID and log path
- `--fg` flag preserves current foreground behavior
- Process survives terminal close (setsid)
- App Nap disabled on macOS
- `.forge/runs/<run-id>.pid` written on start, removed on completion
- `forge status` lists active runs with phase, cost, elapsed
- `forge logs <run-id>` tails the run log
- `forge stop <run-id>` sends SIGTERM for clean shutdown
- Stale PID files cleaned up by `forge status`
- All existing tests pass
- `forge daemon start/stop` still works (not removed, just deprecated)
