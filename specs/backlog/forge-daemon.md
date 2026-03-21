---
name: "forge daemon — persistent background sprint runner"
slug: forge-daemon
pytest_target: tests/
---

# Forge Daemon

## Problem

Forge sprints run as shell background processes (`forge sprint ... &`). This
is fragile:

- Terminal close kills the process (no nohup by default)
- macOS App Nap can SIGTERM background processes
- Mac sleep can kill or freeze them
- Duplicate `forge sprint` launches collide on the same worktree
- Crashes produce `outcome: crashed` with zero diagnostics
- No way to query what's running without `ps aux | grep forge`
- No recovery — crashed sprints stay dead until someone notices

## Solution

Add `forge daemon` — a persistent background process that manages sprint
execution. All sprint runs go through the daemon. The daemon:

1. Runs as a proper background process (daemonized, survives terminal close)
2. Manages a single sprint queue (no duplicate runs for the same spec)
3. Logs crash context (signal, phase, cost at time of death)
4. Sends ntfy notification on unexpected termination
5. Exposes status via `forge status`

### CLI Commands

```bash
forge daemon start          # start the daemon (idempotent)
forge daemon stop           # graceful shutdown
forge daemon status         # show running/queued sprints
forge sprint <manifest>     # submits to daemon (or runs directly if no daemon)
forge status                # alias for daemon status + recent run history
```

### Architecture

```
forge daemon start
  └─ daemonize (double-fork or systemd/launchd)
  └─ write PID to .forge/daemon.pid
  └─ listen on unix socket .forge/daemon.sock
  └─ event loop:
       ├─ accept sprint submissions
       ├─ run specs sequentially (one at a time)
       ├─ catch SIGTERM/SIGKILL and log context
       └─ send ntfy on crash/completion

forge sprint sprints/m1.yaml
  └─ connect to .forge/daemon.sock
  └─ submit manifest
  └─ daemon queues and runs it
  └─ CLI shows live log tail (optional --detach)

forge status
  └─ read from .forge/daemon.sock
  └─ show: running sprint, phase, cost, duration, queue
```

### Fallback

If no daemon is running, `forge sprint` and `forge run` work as they do
today — direct execution in the foreground. The daemon is opt-in, not
required.

### macOS Integration

On macOS, `forge daemon start` can optionally create a launchd plist for
auto-start on login:

```bash
forge daemon install         # create ~/Library/LaunchAgents/com.theforge.daemon.plist
forge daemon uninstall       # remove it
```

### Crash Diagnostics

When the daemon catches SIGTERM/SIGKILL on a child sprint process:

```yaml
crash:
  signal: SIGTERM (15)
  phase: PLAN_REVIEW
  iteration: 0
  cost_at_crash: 0.57
  uptime_seconds: 148
  last_log_event: "phase_start PLAN_REVIEW"
```

This is written to `.forge/logs/crashes.jsonl` and sent via ntfy.

### Status File

The daemon maintains `.forge/daemon.json`:

```json
{
  "pid": 12345,
  "started_at": "2026-03-21T00:15:00Z",
  "current_sprint": {
    "manifest": "sprints/m1-finish.yaml",
    "spec": "escalate-hitl",
    "phase": "DEV",
    "iteration": 1,
    "cost_usd": 2.34,
    "started_at": "2026-03-21T00:24:10Z"
  },
  "queue": [],
  "completed": [
    {"spec": "deepseek-loop-diagnostics", "outcome": "done", "cost": 1.38}
  ]
}
```

Readable by `forge status` or any external tool.

## Acceptance Criteria

- [ ] `forge daemon start` daemonizes and writes PID to `.forge/daemon.pid`
- [ ] `forge daemon stop` sends SIGTERM, waits for graceful shutdown
- [ ] `forge daemon status` shows running sprint, phase, cost, queue
- [ ] `forge sprint <manifest>` submits to daemon when running, runs directly
      when not
- [ ] `forge status` shows daemon status + recent run history
- [ ] Daemon survives terminal close
- [ ] Daemon prevents duplicate sprint runs for same spec
- [ ] Crash diagnostics logged to `.forge/logs/crashes.jsonl` with signal,
      phase, cost, last event
- [ ] ntfy notification on unexpected crash
- [ ] `.forge/daemon.json` maintained with current state (readable by tools)
- [ ] Fallback: all commands work without daemon (current behavior preserved)
- [ ] macOS launchd integration via `forge daemon install` (optional)
- [ ] All existing tests pass
- [ ] New tests for daemon lifecycle, submission, status, crash logging
