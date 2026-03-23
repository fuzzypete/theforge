"""Helpers for transient artifact paths within a worktree."""

from __future__ import annotations

from pathlib import Path

PLAN_PATH = Path(".forge/plan.md")
LEGACY_PLAN_PATH = Path("forge_plan.md")
AUDIT_PATH = Path(".forge/audit.yaml")
LEGACY_AUDIT_PATH = Path("forge_audit.yaml")
LEGACY_HANDOFF_PATH = Path("handoff.yaml")


def ensure_parent_dir(path: Path) -> None:
    """Create the parent directory for a file path when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)


def handoff_paths(workspace_path: Path, handoff_file: str) -> list[Path]:
    """Return preferred and legacy handoff paths for the workspace."""
    paths: list[Path] = []
    if handoff_file:
        paths.append(workspace_path / handoff_file)
    legacy_path = workspace_path / LEGACY_HANDOFF_PATH
    if legacy_path not in paths:
        paths.append(legacy_path)
    return paths


def resolve_handoff_path(workspace_path: Path, handoff_file: str) -> Path | None:
    """Return the existing handoff path, preferring the configured location."""
    for path in handoff_paths(workspace_path, handoff_file):
        if path.exists():
            return path
    return handoff_paths(workspace_path, handoff_file)[0] if handoff_file else None


def plan_paths(workspace_path: Path) -> list[Path]:
    """Return preferred and legacy plan paths for the workspace."""
    return [workspace_path / PLAN_PATH, workspace_path / LEGACY_PLAN_PATH]


def resolve_plan_path(workspace_path: Path) -> Path:
    """Return the existing plan path, preferring .forge/plan.md."""
    for path in plan_paths(workspace_path):
        if path.exists():
            return path
    return workspace_path / PLAN_PATH


def audit_paths(workspace_path: Path) -> list[Path]:
    """Return preferred and legacy audit paths for the workspace."""
    return [workspace_path / AUDIT_PATH, workspace_path / LEGACY_AUDIT_PATH]


def resolve_audit_path(workspace_path: Path) -> Path:
    """Return the existing audit path, preferring .forge/audit.yaml."""
    for path in audit_paths(workspace_path):
        if path.exists():
            return path
    return workspace_path / AUDIT_PATH
