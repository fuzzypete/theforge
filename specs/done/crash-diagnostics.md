---
name: "Crash diagnostics — log context on SIGTERM/SIGKILL"
slug: crash-diagnostics
pytest_target: tests/
---

# Crash Diagnostics

## Problem

When a forge process receives SIGTERM, the handler emits `run_end: crashed`
with zero context. We don't know:
- What signal was received
- What phase was active
- How much budget was spent at time of crash
- What the last log event was

This makes every crash a mystery requiring manual investigation.

## Solution

Enhance the SIGTERM handler in `coordinator.py` to capture and log crash
context before exiting.

### Enhanced crash log event

```json
{
  "event": "run_end",
  "outcome": "crashed",
  "signal": 15,
  "signal_name": "SIGTERM",
  "phase_at_crash": "PLAN_REVIEW",
  "iteration_at_crash": 0,
  "cost_at_crash": 0.57,
  "last_event": "phase_start",
  "uptime_seconds": 148.3
}
```

### ntfy notification on crash

If ntfy is configured, send a crash notification:

```
Title: TheForge CRASHED — escalate-hitl
Body:  SIGTERM during PLAN_REVIEW (iter 0)
       Cost at crash: $0.57
       Uptime: 2m 28s
```

## Acceptance Criteria

- [ ] SIGTERM handler logs signal number and name
- [ ] SIGTERM handler logs phase and iteration at time of crash
- [ ] SIGTERM handler logs accumulated cost at time of crash
- [ ] SIGTERM handler logs uptime (seconds since run_start)
- [ ] ntfy notification sent on crash (if configured)
- [ ] All fields added to the `run_end` structured log event
- [ ] All existing tests pass
- [ ] New test for crash handler context capture
