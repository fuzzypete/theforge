---
name: "Escalation controls — configurable policy for terminal failures"
slug: escalation-controls
pytest_target: tests/
---

# Escalation Controls

## Problem

Escalation behavior is hardcoded. When max review cycles are exhausted or
persistent P1s remain, the coordinator either blocks on HITL (ntfy/pending file)
or auto-rejects. There's no way to configure per-project or per-story policies.

## Solution

Configurable escalation policies in forge.yaml:

```yaml
escalation:
  on_max_cycles: auto_reject     # auto_reject | pending_file | notify_and_reject
  on_persistent_p1: escalate_model  # escalate_model | pending_file | auto_reject
  on_budget_exceeded: skip       # skip | pending_file | abort_sprint
  on_startup_failure: escalate   # escalate | retry_different_model | abort
  timeout_seconds: 600           # max wait for pending file decisions
```

Each policy defines what the coordinator does at that decision point. The
coordinator remains deterministic — policies are evaluated mechanically.

## Acceptance criteria

- Escalation policies configurable in forge.yaml
- Each policy maps to a concrete coordinator action
- Default policies match current behavior (backward compat)
- Pending file path used when policy is `pending_file`
- Notification sent when policy includes `notify_`
- All existing tests pass
- New tests for each policy option
