---
name: "Campaign and run notifications"
slug: notifications
file_scope:
  - src/theforge/campaign.py
  - src/theforge/coordinator.py
  - src/theforge/cli.py
  - tests/test_campaign.py
pytest_target: tests/
---

# Campaign and Run Notifications

## Problem

Long-running campaigns take 30–90 minutes. The operator has no way to
know when a campaign finishes (or fails) without actively tailing the
log. A native OS notification at key events would allow the operator to
walk away and come back when work is done.

## Requirements

### R1: Notification on campaign completion

When `run_campaign()` finishes (any outcome), send a native OS
notification with:

- **Title**: `TheForge: <campaign name>`
- **Body**: `✓ <N> passed, ✗ <M> failed — $<cost>  <duration>`
  e.g. `✓ 7 passed, ✗ 0 failed — $6.89  37m 02s`
- **Sound**: default system alert sound

### R2: Notification on individual spec escalation

When `run_task()` transitions to ESCALATE (not ALREADY_DONE), send:

- **Title**: `TheForge: escalated — <slug>`
- **Body**: The `state.error` message (truncated to 120 chars)

This fires whether the escalation is from a standalone `forge run` or
from within a campaign. It's the one signal that requires immediate
human attention.

### R3: Notification backend — macOS only, fail silent

Use `osascript` to send notifications:

```python
import subprocess, shutil

def _notify(title: str, body: str) -> None:
    """Send a native OS notification. Fails silently on unsupported platforms."""
    if shutil.which("osascript") is None:
        return
    script = (
        f'display notification {_osa_quote(body)} '
        f'with title {_osa_quote(title)} '
        f'sound name "default"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except Exception:
        pass
```

Where `_osa_quote(s)` escapes backslashes and double-quotes and wraps
in double-quotes for AppleScript string literals.

The function must:
- Never raise (all errors are swallowed)
- Never block for more than 5 seconds
- Do nothing on Linux/Windows (no `osascript`)

### R4: Placement

- Campaign notification: called in `run_campaign()` immediately after
  the `"Campaign complete: ..."` log line.
- Escalation notification: called in `_coordinator_loop()` (and
  `run_review_only()`) at the point where `state.phase = Phase.ESCALATE`
  is set and the run is about to return a failure result. Only fire on
  terminal escalations (not mid-loop budget checks that eventually
  succeed — only when we're about to `return CoordinatorResult(success=False, ...)`).

### R5: `--no-notify` flag

Add `--no-notify` to `forge run`, `forge campaign`, and `forge review`
subparsers. When passed, skip all notifications. Default is to notify.

Wire through:
- `cmd_run` → `run_task(..., notify=not args.no_notify)`
- `cmd_campaign` → `run_campaign(..., notify=not args.no_notify)`
- `cmd_review` → `run_from_review(..., notify=not args.no_notify)`

Add `notify: bool = True` keyword parameter to `run_task()`,
`run_campaign()`, and `run_from_review()`. Pass it down to `_notify()`
call sites as a guard.

### R6: Tests

Add to `tests/test_campaign.py`:

- `test_campaign_notification_sent`: mock `_notify`, run a campaign to
  completion, verify `_notify` was called once with the campaign name
  in the title and the success count in the body.
- `test_campaign_notification_skipped_with_no_notify`: pass
  `notify=False`, verify `_notify` is never called.

Add to `tests/test_coordinator.py`:

- `test_escalation_notification_sent`: mock `_notify`, trigger an
  escalation (e.g. workspace creation failure), verify `_notify` called
  with "escalated" in the title.
- `test_escalation_no_notification_when_notify_false`: `notify=False`,
  verify `_notify` never called.
- `test_notify_fail_silent`: patch `subprocess.run` to raise
  `OSError`, call `_notify(...)`, verify no exception propagates.
- `test_notify_noop_without_osascript`: patch `shutil.which` to return
  `None`, verify subprocess is never called.

## Out of scope

- Linux notifications (`notify-send`, `libnotify`)
- Email or Slack notifications
- Per-spec success notifications (too noisy for campaigns)
- Campaign progress notifications ("3/7 done")

## Acceptance criteria

1. Running a campaign sends exactly one notification on completion
2. A spec escalation sends a notification from both `forge run` and
   within `forge campaign`
3. `--no-notify` suppresses all notifications
4. `_notify()` never raises; failure is always silent
5. All existing tests pass
6. New tests cover notification sending, suppression, and silent failure
