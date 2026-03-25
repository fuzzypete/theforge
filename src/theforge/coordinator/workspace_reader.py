"""Workspace reader utilities.

Pure read-only functions that extract state from the worktree:
  - commit log vs base branch
  - handoff file content
  - raw dev_notes from handoff
  - parsed DevHandoff
  - formatted dev notes for reviewer consumption

These are called by the coordinator loop, review pool, and phase handlers.
No LLM calls or side effects.
"""

from __future__ import annotations

from pathlib import Path

from theforge.artifacts import resolve_handoff_path
from theforge.config import ForgeConfig
from theforge.devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff

from . import util as _cu


def _has_uncommitted_changes(workspace_path: Path) -> bool:
    """Check if the worktree has uncommitted changes (staged or unstaged)."""
    ok, status = _cu._run_shell("git status --porcelain", workspace_path)
    return ok and bool(status.strip())


def _get_commit_log(workspace_path: Path, base_branch: str = "main") -> str:
    """Get the commit log vs the base branch (like a PR commit list).

    If the worktree has uncommitted changes, appends a warning so reviewers
    know the commits don't tell the full story.
    """
    dirty = _has_uncommitted_changes(workspace_path)

    ok, log = _cu._run_shell(
        f"git log {base_branch}..HEAD --format='%h %s' --reverse", workspace_path
    )

    parts: list[str] = []
    if ok and log:
        parts.append(log)
    else:
        parts.append("(no commits ahead of base branch)")

    if dirty:
        parts.append(
            "\n⚠ WARNING: Worktree has uncommitted changes not reflected above. "
            "Run `git diff` and `git diff --cached` to see them."
        )

    return "\n".join(parts)


def _get_handoff_content(config: ForgeConfig, workspace_path: Path) -> str:
    """Read the configured handoff content as text for the reviewer."""
    if not config.validation.handoff_file:
        return "(exit-code gate mode — no handoff file)"
    handoff_path = resolve_handoff_path(workspace_path, config.validation.handoff_file)
    if handoff_path is not None and handoff_path.exists():
        return handoff_path.read_text(encoding="utf-8")
    return f"({config.validation.handoff_file} not found)"


def _get_raw_dev_notes(config: ForgeConfig, workspace_path: Path) -> str | None:
    """Extract raw dev_notes from the configured handoff file, or None if absent."""
    if not config.validation.handoff_file:
        return None
    handoff_path = resolve_handoff_path(workspace_path, config.validation.handoff_file)
    if handoff_path is None or not handoff_path.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(handoff_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    val = data.get("dev_notes")
    if isinstance(val, str) and val.strip():
        return val
    return None


def _parse_dev_handoff(config: ForgeConfig, workspace_path: Path) -> DevHandoff | None:
    """Parse and validate the dev handoff from the configured handoff file.

    Returns None only when there's no handoff file at all (exit-code gate mode).
    Returns DevHandoff with parse_errors when dev_notes is missing/blank or
    fails schema validation — so the retry loop can request a rewrite.
    """
    if not config.validation.handoff_file:
        return None
    handoff_path = resolve_handoff_path(workspace_path, config.validation.handoff_file)
    if handoff_path is None or not handoff_path.exists():
        return None
    raw = _get_raw_dev_notes(config, workspace_path)
    if raw is None:
        try:
            handoff_label = str(handoff_path.relative_to(workspace_path))
        except ValueError:
            handoff_label = str(handoff_path)
        return DevHandoff(
            summary="",
            commits=[],
            acceptance_criteria=[],
            story_deviations=[],
            deferred_items=[],
            gate_result="",
            parse_errors=[f"dev_notes field is missing or blank in {handoff_label}"],
            raw={},
        )
    return parse_dev_handoff(raw)


def _get_dev_notes(config: ForgeConfig, workspace_path: Path) -> str | None:
    """Extract dev_notes from the configured handoff file as reviewer text.

    If the dev handoff is valid structured YAML, formats it as structured
    markdown sections. Falls back to raw text if parsing fails.
    """
    raw = _get_raw_dev_notes(config, workspace_path)
    if raw is None:
        return None
    handoff = parse_dev_handoff(raw)
    if handoff.parse_errors:
        # Fall back to raw text when structured parsing fails
        return raw
    formatted = dev_handoff_to_reviewer_text(handoff)
    return formatted if formatted else raw
