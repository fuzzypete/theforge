"""ntfy HTTP transport helpers: publish, poll reply, poll plan reply, poll escalate reply."""

from __future__ import annotations

import json
import time
import unicodedata
import urllib.request

from . import util as _cu

_BLOCKING_POLL_CHUNK = 300  # seconds per poll iteration in blocking mode


def _ntfy_reply_url(base_url: str) -> str:
    """Append '-reply' to the topic segment of the ntfy URL."""
    return base_url.rstrip("/") + "-reply"


def _ntfy_publish(
    url: str,
    title: str,
    body: str,
    priority: str = "high",
    actions: str | None = None,
) -> None:
    """POST a message to an ntfy topic. Fails silently."""
    # HTTP header values must be ASCII-safe for urllib/http.client.
    safe_title = (
        title.replace("✓", "OK")
        .replace("✗", "X")
        .replace("—", "-")
        .replace("–", "-")
        .replace("…", "...")
    )
    safe_title = (
        unicodedata.normalize("NFKD", safe_title).encode("ascii", "ignore").decode("ascii")
    )
    headers: dict[str, str] = {
        "Title": safe_title,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if actions:
        headers["Actions"] = actions
    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as exc:
        _cu._log(f"WARNING: ntfy publish failed (continuing): {exc}")


def _ntfy_poll_reply(
    reply_url: str,
    since_ts: int,
    timeout_seconds: int,
) -> tuple[str, str | None]:
    """Poll ntfy reply topic until a valid decision arrives or timeout."""
    deadline = time.monotonic() + timeout_seconds
    poll_url = f"{reply_url}/json?poll=1&since={since_ts}"

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(poll_url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") not in ("message", None):
                    continue
                msg = (obj.get("message") or "").strip()
                msg_lower = msg.lower()
                if msg_lower == "approve":
                    return "approve", None
                if msg_lower == "extend":
                    return "extend", None
                if msg_lower == "escalate":
                    return "escalate", None
                if msg_lower.startswith("reject:"):
                    findings = msg[7:].strip()
                    return "reject", findings or None
        except Exception:
            pass

        sleep_secs = min(10.0, max(0.0, deadline - time.monotonic()))
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    return "timeout", None


def _ntfy_poll_plan_reply(
    reply_url: str,
    since_ts: int,
    timeout_seconds: int,
) -> str:
    """Poll ntfy reply topic for a plan review decision.

    Returns 'approve', 'regenerate', 'abandon', or 'timeout'.
    """
    deadline = time.monotonic() + timeout_seconds
    poll_url = f"{reply_url}/json?poll=1&since={since_ts}"

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(poll_url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") not in ("message", None):
                    continue
                msg = (obj.get("message") or "").strip().lower()
                if msg == "approve":
                    return "approve"
                if msg == "regenerate":
                    return "regenerate"
                if msg == "abandon":
                    return "abandon"
        except Exception:
            pass

        sleep_secs = min(10.0, max(0.0, deadline - time.monotonic()))
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    return "timeout"


def _ntfy_poll_escalate_reply(
    reply_url: str,
    since_ts: int,
    timeout_seconds: int,
) -> str:
    """Poll ntfy reply topic for an escalate gate decision.

    Returns 'approve', 'reject', 'continue', or 'timeout'.
    """
    deadline = time.monotonic() + timeout_seconds
    poll_url = f"{reply_url}/json?poll=1&since={since_ts}"

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(poll_url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") not in ("message", None):
                    continue
                msg = (obj.get("message") or "").strip().lower()
                if msg == "approve":
                    return "approve"
                if msg == "reject":
                    return "reject"
                if msg == "continue":
                    return "continue"
        except Exception:
            pass

        sleep_secs = min(10.0, max(0.0, deadline - time.monotonic()))
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    return "timeout"
