---
name: "Notification reliability — pending file interface with pluggable transport"
slug: notification-reliability
pytest_target: tests/
---

# Notification Reliability

## Problem

ntfy is a single point of failure for notifications AND the HITL decision gate.
Three failure modes observed in a single session:

1. ntfy publish hangs indefinitely (fixed with socket timeout)
2. ntfy publish times out — notifications never arrive, only a warning logged
3. HITL escalation gate blocks forever waiting for ntfy reply that never comes

ntfy is also being used as a bidirectional decision channel (publish + poll
for reply) which it was never designed for. The HITL gate polls a reply topic
with no maximum timeout, creating an infinite blocking dependency on an
unreliable external service.

## Design

### Core interface: pending decision file

The coordinator writes `.forge/pending/<run-id>.yaml` when a human decision is
needed. The coordinator polls that file for a `decision` field. That's the only
interface contract — everything else is transport.

```yaml
# .forge/pending/abc123.yaml (written by coordinator)
run_id: abc123
story: adaptive-model-assignment
phase: ESCALATE
reason: "Dev agent produced 0 changes — auth failure suspected"
options: [retry, skip, abort]
created_at: "2026-03-22T17:30:00Z"
timeout_at: "2026-03-22T17:40:00Z"
# decision:        # <-- human/tool fills this in
# decided_at:      # <-- human/tool fills this in
```

How the decision gets written doesn't matter to the coordinator:
- Human edits the YAML by hand
- `forge decide <run-id> retry` writes it via CLI
- A webhook endpoint writes it
- A future dashboard writes it
- A Slack bot writes it

### Mandatory timeout

The coordinator polls `.forge/pending/<run-id>.yaml` with a configurable
timeout (default: 10 minutes). When the timeout expires, auto-escalate with
a clear log message and remove the pending file.

```yaml
# forge.yaml
notifications:
  hitl_timeout_seconds: 600  # 10 min default, 0 = skip HITL entirely
```

### Pluggable notification backends (one-way push)

Notification backends are one-way — they tell the human "go look at pending."
They don't carry the return decision. Multiple can be configured:

```yaml
notifications:
  hitl_timeout_seconds: 600
  backends:
    - type: terminal    # osascript on macOS, notify-send on Linux (default)
    - type: ntfy        # existing, kept as option
      url: https://ntfy.sh/my-topic
    - type: webhook     # POST JSON to any URL
      url: https://hooks.slack.com/...
```

Terminal notification is the default — zero config, zero external deps, always
works on macOS.

### `forge decide` CLI

```bash
forge status                    # shows pending decisions
forge decide <run-id> retry     # writes decision to pending file
forge decide <run-id> skip
forge decide <run-id> abort
```

### Cleanup

Pending files are removed after the decision is consumed or timeout expires.
`forge status` cleans stale files (coordinator process no longer running).

## Acceptance criteria

- Coordinator writes `.forge/pending/<run-id>.yaml` for HITL decisions
- Coordinator polls pending file with configurable timeout
- Auto-escalate on timeout expiry with log message
- `forge decide <run-id> <action>` writes decision to pending file
- `forge status` lists pending decisions
- Terminal notification (osascript) works as default backend
- ntfy still works when configured (backward compatible)
- Webhook backend sends POST with JSON body
- Multiple backends can be configured simultaneously
- Backend failure logs warning and continues (never blocks)
- Pending files cleaned up after consumption or timeout
- forge.yaml backward compatible (old ntfy config still works)
- All existing tests pass
