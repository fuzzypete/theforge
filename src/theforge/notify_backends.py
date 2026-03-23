"""Pluggable notification backend dispatch.

Each backend is one-way (fire-and-forget push). Backend failures log a
warning and never raise or block.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import coord_util as _cu

if TYPE_CHECKING:
    from .config import ForgeConfig


def send_notifications(
    config: "ForgeConfig", title: str, body: str, *, is_escalation: bool = False
) -> None:
    """Send notifications to all configured backends."""
    for backend in config.notifications.backends:
        try:
            btype = backend.type
            if btype == "terminal":
                _send_terminal(title, body)
            elif btype == "ntfy":
                if backend.url:
                    _send_ntfy(backend.url, backend.priority or "high", title, body)
                else:
                    _cu._log("WARNING: ntfy backend configured but no URL — skipping")
            elif btype == "webhook":
                if backend.url:
                    _send_webhook(backend.url, title, body)
                else:
                    _cu._log("WARNING: webhook backend configured but no URL — skipping")
            elif btype == "slack":
                _send_slack(
                    webhook_url_env=backend.webhook_url_env or "SLACK_WEBHOOK_URL",
                    title=title,
                    body=body,
                    channel=backend.channel,
                    mention_on_escalate=backend.mention_on_escalate if is_escalation else None,
                    secrets=config.secrets,
                )
            else:
                _cu._log(f"WARNING: unknown notification backend type {btype!r} — skipping")
        except Exception as exc:
            _cu._log(f"WARNING: notification backend {backend.type!r} failed (continuing): {exc}")


def _osa_safe(s: str) -> str:
    """Escape a string for use inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_terminal(title: str, body: str) -> None:
    """Send a native OS notification. Fails silently on unsupported platforms."""
    system = platform.system()
    if system == "Darwin":
        if shutil.which("osascript") is None:
            return
        script = (
            f'display notification "{_osa_safe(body)}"'
            f' with title "{_osa_safe(title)}" sound name "default"'
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
    elif system == "Linux":
        if shutil.which("notify-send") is None:
            return
        try:
            subprocess.run(
                ["notify-send", title, body],
                timeout=5,
                check=False,
                capture_output=True,
            )
        except Exception:
            pass


def _send_ntfy(url: str, priority: str, title: str, body: str) -> None:
    """Send an ntfy push notification."""
    from .coord_notify import _ntfy_publish

    _ntfy_publish(url, title, body, priority=priority)


def _send_webhook(url: str, title: str, body: str) -> None:
    """POST JSON payload to a webhook URL."""
    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def _send_slack(
    webhook_url_env: str,
    title: str,
    body: str,
    channel: str | None = None,
    mention_on_escalate: str | None = None,
    secrets: "dict[str, str] | None" = None,
) -> None:
    """POST a Slack Block Kit message to the configured incoming webhook URL."""
    webhook_url = (secrets or {}).get(webhook_url_env) or os.environ.get(webhook_url_env)
    if not webhook_url:
        _cu._log(
            f"WARNING: Slack backend enabled but env var {webhook_url_env!r} is not set — skipping"
        )
        return

    text_body = body
    if mention_on_escalate:
        text_body = f"{mention_on_escalate} {body}"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title, "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text_body},
        },
    ]
    payload_dict: dict = {"blocks": blocks}
    if channel:
        payload_dict["channel"] = channel

    payload = json.dumps(payload_dict).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        pass
