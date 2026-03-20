---
name: "Sprint and run notifications"
slug: notifications
file_scope:
  - src/theforge/sprint.py
  - src/theforge/coordinator.py
  - src/theforge/config.py
  - src/theforge/cli.py
  - tests/test_sprint.py
  - tests/test_coordinator.py
  - tests/test_config.py
pytest_target: tests/
---

# Sprint and Run Notifications

## Problem

Long-running sprints take 30–90 minutes. The operator is often away from
the machine and has no way to know when a sprint finishes or escalates.
A local macOS notification is useless when you're not at your desk. The
operator needs a signal that reaches their phone.

## Design

Notifications are configured in `forge.yaml` under `notifications:` and
are **opt-in from the CLI** (`--notify` flag). The backend is pluggable:
`ntfy`, `email`, `script`, or `none`. The default backend is `none`.

`ntfy` is the recommended backend — it sends push notifications to the
ntfy iOS/Android app and Apple Watch with nothing more than an HTTP POST.
No API key required for basic use (public server). Self-hostable.

---

## Requirements

### R1: `NotificationConfig` in `config.py`

Add to the config dataclasses:

```python
@dataclass(frozen=True)
class NtfyConfig:
    url: str                    # e.g. "https://ntfy.sh/my-secret-topic"
    priority: str = "default"   # "min" | "low" | "default" | "high" | "urgent"

@dataclass(frozen=True)
class EmailConfig:
    to: str                     # recipient address
    smtp_host: str = "localhost"
    smtp_port: int = 25
    from_addr: str = "theforge@localhost"

@dataclass(frozen=True)
class NotificationConfig:
    backend: str = "none"       # "none" | "ntfy" | "email" | "script"
    ntfy: NtfyConfig | None = None
    email: EmailConfig | None = None
    script: str | None = None   # shell command; FORGE_TITLE and FORGE_BODY env vars set
```

Add `notifications: NotificationConfig` to `ForgeConfig` with a default
of `NotificationConfig()` (backend="none", everything else None).

Parse from `forge.yaml`:
```yaml
notifications:
  backend: ntfy
  ntfy:
    url: https://ntfy.sh/my-secret-topic
    priority: high
```

```yaml
notifications:
  backend: email
  email:
    to: paul@example.com
    smtp_host: smtp.example.com
    smtp_port: 587
```

```yaml
notifications:
  backend: script
  script: "curl -d '$FORGE_BODY' ntfy.sh/my-topic"
```

If `notifications:` is absent, `NotificationConfig()` is used (no-op).

### R2: `_notify()` dispatch function

In `coordinator.py`, add:

```python
def _notify(config: NotificationConfig, title: str, body: str) -> None:
    """Send a remote notification. Always fails silently."""
    try:
        if config.backend == "ntfy":
            _notify_ntfy(config.ntfy, title, body)
        elif config.backend == "email":
            _notify_email(config.email, title, body)
        elif config.backend == "script":
            _notify_script(config.script, title, body)
        # backend == "none": no-op
    except Exception:
        pass
```

#### `_notify_ntfy`

```python
import urllib.request

def _notify_ntfy(cfg: NtfyConfig, title: str, body: str) -> None:
    req = urllib.request.Request(
        cfg.url,
        data=body.encode(),
        headers={
            "Title": title,
            "Priority": cfg.priority,
            "Content-Type": "text/plain",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)
```

Use `urllib` (stdlib only — no `requests` dependency).

#### `_notify_email`

```python
import smtplib
from email.message import EmailMessage

def _notify_email(cfg: EmailConfig, title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"[TheForge] {title}"
    msg["From"] = cfg.from_addr
    msg["To"] = cfg.to
    msg.set_content(body)
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=10) as smtp:
        smtp.send_message(msg)
```

#### `_notify_script`

```python
import subprocess, os

def _notify_script(script: str, title: str, body: str) -> None:
    env = {**os.environ, "FORGE_TITLE": title, "FORGE_BODY": body}
    subprocess.run(script, shell=True, env=env, timeout=10, check=False,
                   capture_output=True)
```

All three helpers must be called only from within `_notify()`'s try/except.

### R3: Notification events

**Sprint completion** — called in `run_sprint()` immediately after
the "Sprint complete: ..." log line:

- Title: `TheForge: <sprint name>`
- Body: `✓ N passed, ✗ M failed — $X.XX  <duration>`

**Spec escalation** — called at every terminal `Phase.ESCALATE`
transition in `run_task()` and `run_from_review()`, immediately before
`return CoordinatorResult(success=False, ...)`:

- Title: `TheForge: escalated — <slug>`
- Body: `state.error` truncated to 120 chars

Only fire on terminal escalations. Do not fire for ALREADY_DONE.

### R4: `--notify` CLI flag (opt-in)

Notifications only fire when `--notify` is passed. This guards against
accidental notifications during iterative testing even when forge.yaml
has a backend configured.

Add `--notify` (`store_true`, default `False`) to `forge run`,
`forge sprint`, and `forge review` subparsers.

Add `notify: bool = False` to `run_task()`, `run_sprint()`, and
`run_from_review()` signatures. Guard every `_notify()` call: `if notify:`.

### R5: Duration formatting

Add `_fmt_duration(seconds: float) -> str`:

```python
def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"
```

Use it everywhere duration is currently printed as raw seconds — sprint
per-spec lines, DONE summary, per-agent timing lines.

### R6: Tests

`tests/test_config.py`:
- `test_notification_config_ntfy`: forge.yaml with ntfy block → backend=="ntfy", url set
- `test_notification_config_absent`: no `notifications:` key → backend=="none"
- `test_notification_config_script`: script backend parses correctly

`tests/test_coordinator.py`:
- `test_escalation_notification_sent`: mock `_notify`, trigger terminal escalation, verify called with "escalated" in title
- `test_escalation_no_notification_when_notify_false`: notify=False → `_notify` never called
- `test_notify_ntfy_posts`: mock `urllib.request.urlopen`, call `_notify_ntfy`, verify URL and headers
- `test_notify_fail_silent`: patch `_notify_ntfy` to raise, call `_notify(...)`, verify no exception propagates
- `test_fmt_duration_hours`: `_fmt_duration(3723)` → `"1h 02m 03s"`
- `test_fmt_duration_minutes`: `_fmt_duration(125)` → `"2m 05s"`
- `test_fmt_duration_seconds`: `_fmt_duration(45)` → `"45s"`

`tests/test_sprint.py`:
- `test_sprint_notification_sent`: mock `_notify`, complete sprint, verify called once with sprint name in title
- `test_sprint_notification_not_sent_without_flag`: notify=False → `_notify` never called

## Out of scope

- Slack / webhook backends
- Per-spec success notifications in sprints (too noisy)
- Sprint progress notifications ("3/7 done")
- macOS `osascript` local notifications
- SMTP authentication

## Acceptance criteria

1. `forge sprint sprints/hardening.yaml --notify` sends one push notification
   on completion, visible on iPhone via ntfy app
2. Terminal escalation sends notification from both `forge run --notify` and within sprint
3. Without `--notify`, nothing fires regardless of forge.yaml config
4. `_notify()` never raises; all errors silently swallowed
5. All existing tests pass unchanged
6. `_fmt_duration` used for all duration output (hours/mins/secs)
