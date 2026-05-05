"""Helpers for reopen-aware issue contract handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ReopenContractState:
    """Derived reopen-contract metadata for a GitHub issue."""

    reopened_at: str | None = None
    reopened_by: str | None = None
    reopen_comment_at: str | None = None
    reopen_comment_author: str | None = None
    reopen_comment_body: str | None = None
    body_edited_after_reopen: bool = False
    body_last_edited_at: str | None = None

    @property
    def has_reopen_context(self) -> bool:
        return self.reopen_comment_body is not None

    @property
    def has_stale_body(self) -> bool:
        return self.has_reopen_context and not self.body_edited_after_reopen


def analyze_reopen_contract(
    issue: Mapping[str, Any],
    timeline: Sequence[Mapping[str, Any]],
) -> ReopenContractState:
    """Return reopen-aware contract state from issue JSON plus timeline events."""
    if str(issue.get("state", "OPEN")).upper() != "OPEN":
        return ReopenContractState()

    reopened_event = _latest_reopened_event(timeline)
    if reopened_event is None:
        return ReopenContractState()

    reopened_at = _coerce_timestamp(reopened_event.get("created_at"))
    if reopened_at is None:
        return ReopenContractState()

    comment = _first_reopen_comment(issue.get("comments") or [], reopened_at)
    reopen_comment_at = _coerce_timestamp(comment.get("createdAt")) if comment else None

    # Clearance signal precedence:
    # 1. Issue body's ``lastEditedAt`` (from gh API) compared against the
    #    newest reopen-comment timestamp — the natural artifact when an
    #    operator reconciles the body. ``gh issue edit`` updates this field
    #    reliably, while timeline ``edited`` events are inconsistent.
    # 2. Timeline ``edited`` event with ``changes.body`` after the reopen.
    body_last_edited_at = _coerce_timestamp(issue.get("lastEditedAt"))
    body_edited_after_reopen = _body_edit_clears_contract(
        body_last_edited_at=body_last_edited_at,
        reopen_comment_at=reopen_comment_at,
        reopened_at=reopened_at,
        timeline=timeline,
    )

    return ReopenContractState(
        reopened_at=reopened_at,
        reopened_by=_actor_login(reopened_event.get("actor")),
        reopen_comment_at=reopen_comment_at,
        reopen_comment_author=_actor_login(comment.get("author")) if comment else None,
        reopen_comment_body=_comment_body(comment) if comment else None,
        body_edited_after_reopen=body_edited_after_reopen,
        body_last_edited_at=body_last_edited_at,
    )


def _body_edit_clears_contract(
    *,
    body_last_edited_at: str | None,
    reopen_comment_at: str | None,
    reopened_at: str,
    timeline: Sequence[Mapping[str, Any]],
) -> bool:
    """Return True when a body edit recognized as reconciliation has occurred.

    Compares the body's ``lastEditedAt`` (if present) against the newest
    reopen-comment timestamp first — this is the cheapest, most reliable
    signal available from ``gh issue view --json lastEditedAt``. Falls back
    to scanning the timeline for an ``edited`` event after the reopen.
    """
    threshold = reopen_comment_at or reopened_at
    if body_last_edited_at is not None and body_last_edited_at > threshold:
        return True
    return _has_body_edit_after_reopen(timeline, reopened_at)


def append_reopen_context(story_text: str, state: ReopenContractState) -> str:
    """Append a structured reopen-context block when present."""
    if not state.has_reopen_context:
        return story_text

    author = state.reopen_comment_author or state.reopened_by or "unknown"
    when = state.reopen_comment_at or state.reopened_at or "unknown time"
    quoted = "\n".join(
        f"> {line}" if line else ">" for line in state.reopen_comment_body.splitlines()
    )
    block = (
        "## Reopen Context\n\n"
        f"This issue was reopened on {state.reopened_at or 'unknown time'}.\n"
        "Operator follow-up comment from "
        f"{author} at {when} defines the current reopen context:\n\n"
        f"{quoted}"
    )
    return f"{story_text.rstrip()}\n\n{block}\n"


def format_reopen_stale_detail(state: ReopenContractState) -> str:
    """Render the operator-facing stale-contract refusal detail."""
    when = state.reopen_comment_at or state.reopened_at or "unknown time"
    author = state.reopen_comment_author or state.reopened_by or "unknown author"
    return (
        f"issue reopened with new context in the comment from {when} by {author}; "
        f"edit the issue body (its lastEditedAt must be newer than {when}) "
        "or pass --force"
    )


def _latest_reopened_event(
    timeline: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    reopened_events = [
        event
        for event in timeline
        if isinstance(event, Mapping)
        and str(event.get("event", "")).lower() == "reopened"
        and _coerce_timestamp(event.get("created_at")) is not None
    ]
    if not reopened_events:
        return None
    return max(reopened_events, key=lambda event: _coerce_timestamp(event.get("created_at")) or "")


def _first_reopen_comment(
    comments: Sequence[Any],
    reopened_at: str,
) -> Mapping[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        created_at = _coerce_timestamp(comment.get("createdAt"))
        if created_at is None or created_at < reopened_at:
            continue
        body = _comment_body(comment)
        if body is None:
            continue
        candidates.append(comment)
    if not candidates:
        return None

    non_bot = [comment for comment in candidates if not _is_bot_actor(comment.get("author"))]
    pool = non_bot or candidates
    return min(pool, key=lambda comment: _coerce_timestamp(comment.get("createdAt")) or "")


def _has_body_edit_after_reopen(
    timeline: Sequence[Mapping[str, Any]],
    reopened_at: str,
) -> bool:
    for event in timeline:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event", "")).lower() != "edited":
            continue
        created_at = _coerce_timestamp(event.get("created_at"))
        if created_at is None or created_at < reopened_at:
            continue
        changes = event.get("changes")
        if isinstance(changes, Mapping) and "body" in changes:
            return True
    return False


def _comment_body(comment: Mapping[str, Any] | None) -> str | None:
    if not isinstance(comment, Mapping):
        return None
    body = str(comment.get("body", "")).strip()
    return body or None


def _actor_login(actor: Any) -> str | None:
    if not isinstance(actor, Mapping):
        return None
    login = actor.get("login")
    if isinstance(login, str) and login.strip():
        return login.strip()
    return None


def _is_bot_actor(actor: Any) -> bool:
    if not isinstance(actor, Mapping):
        return False
    login = str(actor.get("login", "")).strip().lower()
    actor_type = str(actor.get("type", "")).strip().lower()
    return actor_type == "bot" or login.endswith("[bot]")


def _coerce_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
