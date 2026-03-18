"""Tests for theforge.sessions — session ID persistence."""

from __future__ import annotations

import json
from pathlib import Path

from theforge.sessions import load_sessions, save_sessions


def test_round_trip(tmp_path: Path) -> None:
    """save then load returns the same data."""
    save_sessions(tmp_path, "dev-abc123", {"reviewer1": "rev-xyz456"})
    result = load_sessions(tmp_path)
    assert result["dev_session_id"] == "dev-abc123"
    assert result["reviewer_session_ids"] == {"reviewer1": "rev-xyz456"}


def test_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    """load_sessions returns {} when no file exists."""
    assert load_sessions(tmp_path) == {}


def test_no_dev_session_id(tmp_path: Path) -> None:
    """save_sessions omits dev_session_id key when it is None."""
    save_sessions(tmp_path, None, {"r": "s"})
    data = json.loads((tmp_path / ".forge/sessions.json").read_text())
    assert "dev_session_id" not in data
    assert data["reviewer_session_ids"] == {"r": "s"}


def test_empty_reviewer_session_ids(tmp_path: Path) -> None:
    """save_sessions omits reviewer_session_ids when dict is empty."""
    save_sessions(tmp_path, "dev-1", {})
    data = json.loads((tmp_path / ".forge/sessions.json").read_text())
    assert data["dev_session_id"] == "dev-1"
    assert "reviewer_session_ids" not in data


def test_directory_auto_created(tmp_path: Path) -> None:
    """save_sessions creates .forge/ if it doesn't exist."""
    workspace = tmp_path / "my-workspace"
    workspace.mkdir()
    save_sessions(workspace, "dev-1", {})
    assert (workspace / ".forge/sessions.json").exists()


def test_load_invalid_json_returns_empty_dict(tmp_path: Path) -> None:
    """load_sessions returns {} on corrupt JSON."""
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "sessions.json").write_text("not valid json{{{")
    assert load_sessions(tmp_path) == {}


def test_load_returns_partial_data(tmp_path: Path) -> None:
    """load_sessions works fine when only dev_session_id is present."""
    save_sessions(tmp_path, "dev-only", {})
    result = load_sessions(tmp_path)
    assert result.get("dev_session_id") == "dev-only"
    assert result.get("reviewer_session_ids") is None
