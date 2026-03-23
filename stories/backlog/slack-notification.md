---
name: "Slack notification backend — replace ntfy with webhook"
slug: slack-notification
pytest_target: tests/
---

# Slack Notification Backend

## Problem

ntfy.sh public service is unreliable (went down for days, no SLA). Notifications
are one-way status updates — sprint started, story approved, escalation happened,
cost summary. Slack is what OSS teams and small shops actually use.

## Solution

Add a Slack webhook notification backend. One-way only (MVP). No return channel,
no interactive buttons — that's a future story.

### Configuration

```yaml
notifications:
  backend: slack
  slack:
    webhook_url_env: SLACK_WEBHOOK_URL  # read from .forge/.env
    channel: "#theforge"               # optional override
    mention_on_escalate: "@here"       # optional
```

### Events to notify

- Sprint started (story count, budget)
- Story APPROVE (cost, duration, PR link)
- Story ESCALATE (reason, cost, pending decision file path)
- Sprint complete (summary: succeeded/failed/skipped, total cost)
- Budget exceeded
- Agent startup failure

### Implementation

- New `_slack_publish()` in `coord_notify.py` alongside `_ntfy_publish()`
- Backend selection via `config.notifications.backend`
- Slack message format: structured blocks with fields, not raw text
- Webhook URL from environment (never in forge.yaml)
- Failure is best-effort: log warning and continue, never block pipeline

## Acceptance criteria

- `backend: slack` sends notifications via Slack webhook
- All 6 event types produce Slack messages
- Webhook URL read from environment variable
- Notification failure logs warning and continues
- Existing ntfy backend still works when selected
- `backend: none` disables all notifications
- All existing tests pass
- New tests mock webhook POST and verify message format
