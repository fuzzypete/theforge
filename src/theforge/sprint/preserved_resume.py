"""Shared operator guidance for preserved sprint stories."""

from __future__ import annotations

PRESERVED_RESUME_COMMAND = "forge run --resume <story-file>"
PRESERVED_RESUME_GUIDANCE = f"resolve with `{PRESERVED_RESUME_COMMAND}`"
PRESERVED_ESCALATED_DETAIL = (
    f"escalated worktree preserved for human review; {PRESERVED_RESUME_GUIDANCE}"
)


def preserved_escalated_message(slug: str) -> str:
    """Canonical preserved-state message for an escalated worktree."""
    return f"PRESERVED {slug}: {PRESERVED_ESCALATED_DETAIL}"
