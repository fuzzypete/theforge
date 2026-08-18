"""Shared operator guidance for preserved sprint stories."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

PRESERVED_REVIEW_COMMAND = "forge review <story-file>"
PRESERVED_REVIEW_GUIDANCE = f"resolve with `{PRESERVED_REVIEW_COMMAND}`"
_ISSUE_PATH_RE = re.compile(r"^Issue #(?P<number>\d+)$")
_ISSUE_SLUG_RE = re.compile(r"^issue-(?P<number>\d+)$")


def _story_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _issue_number(
    *,
    canonical_ref: str | None = None,
    path: str | None = None,
    slug: str | None = None,
) -> str | None:
    """Best-effort issue number extraction for issue-backed preserved stories."""
    if canonical_ref and canonical_ref.startswith("issue:"):
        number = canonical_ref.split(":", 1)[1].strip()
        if number.isdigit():
            return number
    if path:
        match = _ISSUE_PATH_RE.match(path)
        if match:
            return match.group("number")
    if slug:
        match = _ISSUE_SLUG_RE.match(slug)
        if match:
            return match.group("number")
    return None


def preserved_review_command(
    *,
    canonical_ref: str | None = None,
    path: str | None = None,
    slug: str | None = None,
) -> str:
    """Return the runnable operator command for a preserved escalated story."""
    issue_number = _issue_number(canonical_ref=canonical_ref, path=path, slug=slug)
    if issue_number is not None:
        return f"forge review --issue {issue_number}"
    if path is not None:
        return f"forge review {shlex.quote(path)}"
    return PRESERVED_REVIEW_COMMAND


def preserved_escalated_detail(
    *,
    canonical_ref: str | None = None,
    path: str | None = None,
    slug: str | None = None,
) -> str:
    """Canonical preserved-state detail with source-appropriate review guidance."""
    command = preserved_review_command(canonical_ref=canonical_ref, path=path, slug=slug)
    return f"escalated worktree preserved for human review; resolve with `{command}`"


def preserved_escalated_detail_for_story(story: Mapping[str, object]) -> str:
    """Canonical preserved-state detail derived from a summary/live-status row."""
    return preserved_escalated_detail(
        canonical_ref=_story_str(story.get("canonical_ref")),
        path=_story_str(story.get("path")),
        slug=_story_str(story.get("slug")),
    )


def preserved_escalated_message(
    slug: str,
    *,
    canonical_ref: str | None = None,
    path: str | None = None,
) -> str:
    """Canonical preserved-state message for an escalated worktree."""
    detail = preserved_escalated_detail(canonical_ref=canonical_ref, path=path, slug=slug)
    return f"PRESERVED {slug}: {detail}"
