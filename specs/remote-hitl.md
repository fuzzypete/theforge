---
name: "Remote human-in-the-loop: async ntfy-based decisions"
slug: remote-hitl
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/config.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
  - tests/test_config.py
pytest_target: tests/
---

# Remote Human-in-the-Loop

## Problem

`HUMAN_REVIEW` is synchronous stdin-blocking. Running campaigns headlessly
(nohup, CI, overnight) hits `HUMAN_REVIEW`, gets EOF, and auto-escalates —
the human never gets a say. The operator needs to be able to approve,
reject, extend, or escalate from their phone, not from the terminal.

## Design

When `--notify` is active and the notification backend is `ntfy`, `HUMAN_REVIEW`
switches from stdin-blocking to **async poll mode**:

1. Forge publishes a decision-request notification to the configured ntfy topic
   with action buttons embedded in the notification
2. Coordinator subscribes to a reply topic (`<url>-reply`) and long-polls
   for a response
3. Human taps a button on their phone → ntfy fires an HTTP action that
   publishes to the reply topic
4. Coordinator reads the reply, unblocks, and transitions accordingly
5. If no reply arrives within `human_review_timeout_seconds`, auto-escalates
   and notifies

Without `--notify` or with a non-ntfy backend, `HUMAN_REVIEW` behaves
exactly as today (stdin blocking, EOF → escalate).

---

## Requirements

### R1: ntfy action buttons in HUMAN_REVIEW notification

When entering `HUMAN_REVIEW` in remote mode, publish to the ntfy topic
with three action buttons using ntfy's
[HTTP action](https://docs.ntfy.sh/publish/#action-buttons) format:

```
Title: TheForge: review needed — <slug>
Body:  <verdict> (<P1> P1, <P2> P2) — $<cost>  <duration>
       <one-line summary, truncated to 120 chars>
       Branch: feat/<slug>

Actions: http, Approve, <reply_url>, method=POST, body=approve;
         http, Extend+1, <reply_url>, method=POST, body=extend;
         http, Escalate, <reply_url>, method=POST, body=escalate
```

Where `reply_url` is the ntfy reply topic URL (derived by appending `-reply`
to the base topic URL, e.g. `https://ntfy.sh/theforge-pwickersham-reply`).

ntfy action buttons fire an HTTP POST to the reply URL when tapped. The
body is the decision string.

### R2: Reply topic polling

After publishing the notification, subscribe to the reply topic using
ntfy's [streaming endpoint](https://docs.ntfy.sh/subscribe/api/):

```
GET <reply_url>/json?poll=1&since=<unix_timestamp_before_publish>
```

Poll every 10 seconds until a message arrives or `human_review_timeout_seconds`
elapses. Parse the response body as the decision.

Valid decision strings (case-insensitive, strip whitespace):
- `approve` → transition to DONE
- `extend` → grant one additional review cycle, send last findings back to dev
- `escalate` → transition to ESCALATE

Unknown strings are ignored (keep polling). This allows the human to
accidentally tap a button twice without breaking the flow.

The `extend` decision:
- Fully resets `state.dev_iteration` to 0 AND `state.review_cycle` to 0
- Increments `state.human_review_extra_cycles` (for audit only)
- Does NOT decrement any cycle counter — grants a completely fresh budget
  of `max_review_cycles` regardless of how many cycles were consumed
- Uses the last review's findings as the feedback to the dev agent
- Continues the DEV → VALIDATE → REVIEW loop

The human is explicitly overriding the cycle ceiling — this is not a
retry within the existing budget, it is a fresh start at the human's
explicit direction.

### R3: Reject via reply message

For freeform rejection with custom findings, the human can publish directly
to the reply topic with a message beginning with `reject:`:

```
reject: the error handling in run_from_review doesn't reset dev_iteration
```

The coordinator strips the `reject:` prefix and treats the remainder as
human findings text, feeding it back to the dev agent. This consumes one
review cycle (same as the existing reject path).

If cycles are exhausted when `reject` arrives, treat it as `extend` +
reject — automatically grant one more cycle.

### R4: Timeout

Add `human_review_timeout_seconds: int = 14400` (4 hours) to
`NotificationConfig`. If no reply arrives in this window:

- Auto-escalate
- Publish a timeout notification: `"TheForge: timed out waiting for review decision — <slug>"`

### R5: `NotificationConfig` additions

```python
@dataclass(frozen=True)
class NotificationConfig:
    backend: str = "none"
    ntfy: NtfyConfig | None = None
    email: EmailConfig | None = None
    script: str | None = None
    human_review_timeout_seconds: int = 14400  # ← new
```

Parse from `forge.yaml`:
```yaml
notifications:
  backend: ntfy
  ntfy:
    url: https://ntfy.sh/theforge-pwickersham
    priority: high
  human_review_timeout_seconds: 7200  # 2 hours
```

### R6: Reply topic URL derivation

```python
def _ntfy_reply_url(base_url: str) -> str:
    """Append '-reply' to the topic segment of the ntfy URL."""
    # https://ntfy.sh/theforge-pwickersham → https://ntfy.sh/theforge-pwickersham-reply
    return base_url.rstrip("/") + "-reply"
```

### R7: Remote mode activation

Remote poll mode activates when ALL of these are true:
- `notify=True` (--notify passed)
- `config.notifications.backend == "ntfy"`
- `config.notifications.ntfy` is not None

Otherwise, `HUMAN_REVIEW` uses existing stdin behaviour unchanged.

### R8: Coordinator state additions

Add to `CoordinatorState`:
- `human_review_decision: str | None = None` — the decision received
  (`"approve"`, `"extend"`, `"escalate"`, `"reject"`, `"timeout"`)
- `human_review_extra_cycles: int = 0` — cycles granted by `extend`

Record in audit log under `human_review`:
```yaml
human_review:
  mode: remote          # or "interactive" or "auto-escalated"
  decision: approve
  waited_seconds: 312
  extra_cycles_granted: 0
```

### R9: Tests

`tests/test_coordinator.py` — `TestRemoteHumanReview` class:

- `test_remote_approve`: mock ntfy poll to return `"approve"`, verify DONE
- `test_remote_extend_grants_cycle`: mock poll returns `"extend"`, verify
  `max_review_cycles` effectively incremented, dev called again
- `test_remote_escalate`: mock poll returns `"escalate"`, verify ESCALATE
- `test_remote_reject_with_findings`: mock poll returns `"reject: fix the bug"`,
  verify findings fed back to dev
- `test_remote_timeout`: mock poll to never return, advance clock past timeout,
  verify ESCALATE and timeout notification sent
- `test_remote_mode_not_activated_without_notify`: `notify=False` → stdin
  mode, not remote
- `test_remote_mode_not_activated_without_ntfy`: non-ntfy backend → stdin mode
- `test_ntfy_reply_url`: `_ntfy_reply_url("https://ntfy.sh/my-topic")` →
  `"https://ntfy.sh/my-topic-reply"`

`tests/test_config.py`:
- `test_human_review_timeout_parsed`: verify `human_review_timeout_seconds`
  read from forge.yaml
- `test_human_review_timeout_default`: absent from forge.yaml → 14400

### R10: Campaign end notification

When `run_campaign()` completes (all specs processed), if remote mode
is active, publish an informational summary — no action buttons:

```
Title: TheForge campaign complete — "<campaign_name>"
Body:  7 specs: 6 succeeded · 1 failed
       Total cost: $12.83   Duration: 57m 14s
```

No reply polling — informational only. Also send on mid-campaign budget
exhaustion (include `stopped_reason` in body).

### R11: Duration pretty-printing

Replace all raw `{elapsed:.0f}s` duration displays in `coordinator.py`,
`campaign.py`, and `cli.py` with a shared helper:

```python
def _fmt_duration(seconds: float) -> str:
    """Format as '2h 14m 3s', '14m 3s', or '47s'."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
```

Used in: log lines, campaign summary, notification bodies.

## Out of scope

- Web dashboard or dedicated forge server
- Multi-user review (one human decides per cycle)
- SMS or email action buttons (ntfy-only for interactive decisions)
- Persistent reply topic history cleanup
- Review summary included in rejection findings (human text only)

## Acceptance criteria

1. `forge run specs/foo.md --notify` with ntfy configured: reaching
   `HUMAN_REVIEW` sends a push notification with three action buttons
2. Tapping `[Approve]` on iPhone transitions to DONE
3. Tapping `[Extend+1]` grants one more cycle and sends findings back to dev
4. Publishing `reject: <text>` to the reply topic feeds findings to dev
5. No reply within `human_review_timeout_seconds` → auto-escalate + timeout notification
6. Without `--notify` or without ntfy backend, stdin behaviour is unchanged
7. All existing tests pass
8. New tests cover all remote decision paths

## Dependencies

- `specs/human-in-the-loop.md` must be implemented first (`HUMAN_REVIEW`
  phase, `run_task(interactive=)` signature)
- `specs/notifications.md` is already implemented (`--no-notify` flag,
  `_notify()` osascript helper). This spec extends `NotificationConfig`
  with `NtfyConfig` and `human_review_timeout_seconds` — do not depend
  on notifications.md providing these; implement them here.
