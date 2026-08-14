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
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .coordinator import util as _cu

if TYPE_CHECKING:
    from .config import ForgeConfig


MAX_PENDING_NOTIFICATION_BODY_CHARS = 900


def _truncate_notification_text(text: str, limit: int) -> str:
    """Trim text to fit a notification body budget."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _format_pending_deadline(timeout_at: str, *, now: datetime | None = None) -> str:
    """Render an absolute deadline plus the remaining or overdue window."""
    deadline = datetime.fromisoformat(timeout_at)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    now_utc = now or datetime.now(timezone.utc)
    remaining_seconds = (deadline - now_utc).total_seconds()
    deadline_utc = deadline.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if remaining_seconds >= 0:
        return (
            f"Deadline: {deadline_utc} "
            f"({_cu._fmt_duration(max(0.0, remaining_seconds))} remaining)"
        )
    return f"Deadline: {deadline_utc} ({_cu._fmt_duration(abs(remaining_seconds))} overdue)"


def format_pending_decision_notification(
    pending_record: Mapping[str, object],
    *,
    pending_path: Path | str,
    max_chars: int = MAX_PENDING_NOTIFICATION_BODY_CHARS,
) -> str:
    """Build an actionable notification body from the pending decision record."""
    reason = str(pending_record.get("reason") or "").strip()
    run_id = str(pending_record.get("run_id") or "").strip()
    timeout_at = str(pending_record.get("timeout_at") or "").strip()
    options = [
        str(option).strip() for option in pending_record.get("options") or [] if str(option)
    ]
    phase = str(pending_record.get("phase") or "").strip()
    pending_path_str = str(pending_path)
    metadata_lines = []
    if phase:
        metadata_lines.append(f"Phase: {phase}")
    if run_id:
        metadata_lines.append(f"Run ID: {run_id}")
    if timeout_at:
        metadata_lines.append(_format_pending_deadline(timeout_at))

    command_lines: list[str] = []
    if run_id and options:
        option_set = "|".join(options)
        command_lines = [
            "Reply with:",
            f"  forge decide {run_id} <{option_set}>",
        ]
    elif run_id:
        command_lines = [
            "Reply with:",
            f"  forge decide {run_id} <action>",
        ]

    fallback_lines = [
        "Reply with:",
        f"  forge decide {run_id} <action>" if run_id else "  forge decide <run-id> <action>",
        f"Full option set omitted from this notification; read {pending_path_str}",
    ]

    def _compose(command_block: Sequence[str], *, reason_text: str) -> str:
        lines: list[str] = []
        if reason_text:
            lines.append(reason_text)
        if metadata_lines:
            if lines:
                lines.append("")
            lines.extend(metadata_lines)
        if command_block:
            if lines:
                lines.append("")
            lines.extend(command_block)
        return "\n".join(lines)

    if command_lines:
        full_body = _compose(command_lines, reason_text=reason)
        if len(full_body) <= max_chars:
            return full_body
        available_reason = max_chars - len(_compose(command_lines, reason_text=""))
        trimmed_body = _compose(
            command_lines,
            reason_text=_truncate_notification_text(reason, available_reason),
        )
        if len(trimmed_body) <= max_chars:
            return trimmed_body

    available_reason = max_chars - len(_compose(fallback_lines, reason_text=""))
    return _compose(
        fallback_lines,
        reason_text=_truncate_notification_text(reason, available_reason),
    )


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
    from .coordinator.notify import _ntfy_publish

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
