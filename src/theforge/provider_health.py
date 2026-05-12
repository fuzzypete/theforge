"""Provider-health signal for adaptive reviewer routing.

A reviewer model that hit a provider-shape failure (capacity, rate-limit, 5xx,
quota exhaustion) within a recent window is "unhealthy".  The adaptive router
consumes this signal so it stops picking flapping models on subsequent
selections within the same sprint and across sprint boundaries.

This module is pure: only stdlib + yaml.  No LLM calls, no subprocess.

Persistence schema (``.forge/provider_health.yaml``)::

    records:
      - model: openai-gpt-5.5
        signature: capacity
        timestamp: 2026-05-12T07:23:00Z
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .agent_types import AgentResult

log = logging.getLogger(__name__)

# Default lookback window: a provider-shape failure within this many seconds
# marks the model as unhealthy.  One hour is long enough to span the obvious
# "we just saw it crash this sprint" case and short enough that a real
# provider-side recovery becomes visible within a single operator coffee break.
DEFAULT_HEALTH_WINDOW_SECONDS = 3600


# Lowercase substring patterns in agent output that indicate a provider-side
# failure rather than a runtime/code error.  Kept narrow so non-provider
# failures (parse errors, schema rejections, dev-code crashes) do not poison
# the routing signal.
_PROVIDER_SHAPE_PATTERNS: tuple[str, ...] = (
    "at capacity",
    "selected model is at capacity",
    "overloaded",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "resource exhausted",
    "quota exceeded",
    "quota_exceeded",
    "try again later",
    "429",
    "503",
    "529",
)


@dataclass(frozen=True)
class ProviderHealthRecord:
    """A single observed provider-shape failure for a named model/profile."""

    model: str  # profile name (e.g. "openai-gpt-5.5"); same identifier _select_reviewers sees
    signature: str  # short tag describing the failure shape ("capacity", "rate_limit", ...)
    timestamp: str  # ISO-8601 UTC timestamp


def classify_provider_shape_failure(result: AgentResult) -> str | None:
    """Return a signature tag if *result* is a provider-shape failure, else None.

    A "provider-shape" failure means the provider itself refused or returned a
    transient capacity/quota signal — not a runtime error in our code or a
    parse/schema problem.  Only these shapes feed the health signal.
    """
    if result.success:
        return None
    if result.cli_quota_error_observed:
        return "cli_quota"
    output = (result.output or "").lower()
    for pattern in _PROVIDER_SHAPE_PATTERNS:
        if pattern in output:
            # Coalesce the family signatures so the audit reads cleanly.
            if pattern in ("at capacity", "selected model is at capacity", "overloaded", "529"):
                return "capacity"
            if pattern in ("rate limit", "rate_limit", "429"):
                return "rate_limit"
            if pattern in (
                "resource_exhausted",
                "resource exhausted",
                "quota exceeded",
                "quota_exceeded",
            ):
                return "quota"
            if pattern == "503":
                return "5xx"
            if pattern == "try again later":
                return "transient"
    return None


def load_provider_health(path: Path) -> list[ProviderHealthRecord]:
    """Read ``.forge/provider_health.yaml``; return [] when absent or malformed."""
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return []
        raw_records = data.get("records", [])
        if not isinstance(raw_records, list):
            return []
        result: list[ProviderHealthRecord] = []
        for r in raw_records:
            if not isinstance(r, dict):
                continue
            model = r.get("model")
            signature = r.get("signature")
            timestamp = r.get("timestamp")
            if not model or not signature or not timestamp:
                continue
            result.append(
                ProviderHealthRecord(
                    model=str(model),
                    signature=str(signature),
                    timestamp=str(timestamp),
                )
            )
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("[provider_health] Failed to load %s: %s", path, exc)
        return []


def append_provider_health_record(path: Path, record: ProviderHealthRecord) -> None:
    """Append *record* to ``.forge/provider_health.yaml``, creating it if absent."""
    existing: list[dict] = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                existing = [r for r in data["records"] if isinstance(r, dict)]
        except Exception as exc:  # noqa: BLE001
            log.warning("[provider_health] Could not read %s: %s", path, exc)

    existing.append(
        {
            "model": record.model,
            "signature": record.signature,
            "timestamp": record.timestamp,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"records": existing}, f, default_flow_style=False, allow_unicode=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[provider_health] Failed to write %s: %s", path, exc)


def _parse_iso(ts: str) -> datetime.datetime | None:
    try:
        # Tolerate trailing 'Z'.
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def unhealthy_models(
    records: list[ProviderHealthRecord],
    *,
    now: datetime.datetime | None = None,
    window_seconds: int = DEFAULT_HEALTH_WINDOW_SECONDS,
) -> set[str]:
    """Return the set of model names whose latest failure is within *window_seconds*.

    A model with no record in the window is healthy.  When *now* is None, the
    current UTC time is used.
    """
    if not records:
        return set()
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    cutoff = now - datetime.timedelta(seconds=window_seconds)
    result: set[str] = set()
    for r in records:
        ts = _parse_iso(r.timestamp)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        if ts >= cutoff:
            result.add(r.model)
    return result


def utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
