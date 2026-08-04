"""API key resolution, secret helpers, and notification config parsing."""

from __future__ import annotations

import logging
import os
from typing import Any

from .types import (
    DEFAULT_HITL_TIMEOUT_SECONDS,
    BackendConfig,
    NotificationConfig,
    NtfyConfig,
    SlackConfig,
)

log = logging.getLogger("theforge.config")


def _resolve_secret(key: str, secrets: dict[str, str]) -> str | None:
    """Check secrets dict first, then fall back to os.environ."""
    return secrets.get(key) or os.getenv(key)


def _parse_notifications(
    notif_data: dict[str, Any], secrets: dict[str, str]
) -> NotificationConfig:
    """Parse notifications section from raw YAML dict."""
    notif_backend = notif_data.get("backend", "none")
    ntfy_config: NtfyConfig | None = None
    if "ntfy" in notif_data:
        ntfy_data = notif_data["ntfy"]
        ntfy_url = ntfy_data.get("url") or secrets.get("NTFY_URL") or os.getenv("NTFY_URL") or ""
        if ntfy_url:
            ntfy_config = NtfyConfig(
                url=ntfy_url,
                priority=ntfy_data.get("priority", "high"),
            )
        elif notif_backend == "ntfy":
            log.warning("ntfy backend enabled but no URL configured — notifications disabled")
    elif notif_backend == "ntfy":
        ntfy_url = secrets.get("NTFY_URL") or os.getenv("NTFY_URL") or ""
        if ntfy_url:
            ntfy_config = NtfyConfig(url=ntfy_url, priority="high")
        else:
            log.warning("ntfy backend enabled but no URL configured — notifications disabled")

    slack_config: SlackConfig | None = None
    if "slack" in notif_data:
        slack_data = notif_data["slack"]
        slack_config = SlackConfig(
            webhook_url_env=str(slack_data.get("webhook_url_env", "SLACK_WEBHOOK_URL")),
            channel=slack_data.get("channel") or None,
            mention_on_escalate=slack_data.get("mention_on_escalate") or None,
        )
    elif notif_backend == "slack":
        slack_config = SlackConfig(webhook_url_env="SLACK_WEBHOOK_URL")

    # hitl_timeout_seconds is the canonical YAML key; human_review_timeout_seconds is the alias
    _hitl_timeout = int(
        notif_data.get(
            "hitl_timeout_seconds",
            notif_data.get("human_review_timeout_seconds", DEFAULT_HITL_TIMEOUT_SECONDS),
        )
    )

    # Build pluggable backends list
    # New format: backends: [{type: terminal}, {type: ntfy, url: ...}]
    # Old format: backend: ntfy + ntfy: {url, priority} → synthesise a single ntfy entry
    _backends_raw = notif_data.get("backends")
    backends: list[BackendConfig]
    if _backends_raw is not None:
        backends = []
        for b in _backends_raw:
            if isinstance(b, dict):
                backends.append(
                    BackendConfig(
                        type=str(b.get("type", "terminal")),
                        url=b.get("url") or None,
                        priority=b.get("priority") or None,
                        webhook_url_env=b.get("webhook_url_env") or None,
                        channel=b.get("channel") or None,
                        mention_on_escalate=b.get("mention_on_escalate") or None,
                    )
                )
    elif notif_backend == "ntfy" and ntfy_config is not None:
        backends = [BackendConfig(type="ntfy", url=ntfy_config.url, priority=ntfy_config.priority)]
    elif notif_backend == "slack" and slack_config is not None:
        backends = [
            BackendConfig(
                type="slack",
                webhook_url_env=slack_config.webhook_url_env,
                channel=slack_config.channel,
                mention_on_escalate=slack_config.mention_on_escalate,
            )
        ]
    elif notif_backend == "none":
        backends = []
    else:
        backends = [BackendConfig(type="terminal")]

    return NotificationConfig(
        backend=notif_backend,
        ntfy=ntfy_config,
        slack=slack_config,
        script=notif_data.get("script"),
        human_review_timeout_seconds=_hitl_timeout,
        backends=tuple(backends),
    )
