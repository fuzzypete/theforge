---
name: "ntfy notifications for DONE and ESCALATE terminal states"
slug: ntfy-terminal-notifications
file_scope:
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# ntfy Terminal Notifications

## Problem

`_ntfy_publish()` is only called inside `_run_remote_human_review()`. Every
other terminal state (DONE, ESCALATE, ALREADY_DONE) only calls `_notify()`
which uses macOS `osascript` — desktop-only, no phone.

Users who are away from their desk receive zero notification when a task
completes or escalates. The ntfy backend config exists, is correct, and the
topic subscription works — but forge never uses it for task outcomes.

## Solution

Add `_ntfy_publish()` calls at the two terminal outcomes when ntfy is
configured:

### 1. DONE (success)

After a task completes successfully and is merged (or marked done), publish:

```
Title: TheForge: ✓ done — {slug}
Body:
APPROVE — ${cost:.2f}  {duration}
{synthesis_summary[:120] or "Approved and merged."}
Branch: {branch_name}
```

### 2. ESCALATE (failure)

When a task escalates (max cycles exhausted or unrecoverable error), publish:

```
Title: TheForge: ✗ escalated — {slug}
Body:
{cycles} cycles exhausted — ${cost:.2f}  {duration}
{last_p1_description[:120] or error[:120]}
Branch: {branch_name}
```

### When to publish

Only publish when ntfy is configured (`config.notifications.ntfy is not None`)
and the `--notify` flag was passed (same gate as `_escalate_notify()`).

Do NOT add action buttons to DONE/ESCALATE notifications — these are
informational only. No reply polling needed.

### Where in coordinator.py

The coordinator's main `run()` function already calls `_escalate_notify()` at
the end of the ESCALATE path and reaches DONE via the DONE state. Add
`_ntfy_publish()` calls at these same sites, after the existing `_notify()`
calls.

For DONE: the notification fires just before the function returns the final
`CoordinatorState`. Check `state.final_phase == "DONE"`.

For ESCALATE: fires alongside the existing `_escalate_notify()` calls. Since
`_escalate_notify()` only does macOS notifications, the ntfy call is additive.

## Acceptance Criteria

- [ ] DONE state publishes to ntfy when ntfy is configured and notify=True
- [ ] ESCALATE state publishes to ntfy when ntfy is configured and notify=True
- [ ] ALREADY_DONE publishes a DONE-style notification (task was already complete)
- [ ] Notification body includes cost, duration, and summary/error (≤120 chars)
- [ ] No action buttons on DONE/ESCALATE notifications
- [ ] Fails silently if ntfy publish raises an exception (same as existing pattern)
- [ ] No ntfy call when `config.notifications.ntfy is None`
- [ ] No ntfy call when `notify=False`
- [ ] Existing tests pass without modification
- [ ] New test: DONE path calls `_ntfy_publish` with correct title and body
- [ ] New test: ESCALATE path calls `_ntfy_publish` with correct title and body
- [ ] New test: no ntfy call when ntfy not configured
