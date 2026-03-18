"""Session ID persistence for forge review path.

Reads/writes .forge/sessions.json inside the worktree so that dev and reviewer
session IDs survive across separate ``forge review`` invocations.
"""

from __future__ import annotations

import json
from pathlib import Path

_SESSIONS_FILE = ".forge/sessions.json"


def save_sessions(
    workspace_path: Path,
    dev_session_id: str | None,
    reviewer_session_ids: dict[str, str],
) -> None:
    """Persist session IDs to <workspace_path>/.forge/sessions.json."""
    target = workspace_path / _SESSIONS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if dev_session_id is not None:
        data["dev_session_id"] = dev_session_id
    if reviewer_session_ids:
        data["reviewer_session_ids"] = reviewer_session_ids
    target.write_text(json.dumps(data, indent=2))


def load_sessions(workspace_path: Path) -> dict:
    """Load session IDs from <workspace_path>/.forge/sessions.json.

    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    target = workspace_path / _SESSIONS_FILE
    try:
        return json.loads(target.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
