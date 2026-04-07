"""Helpers for gathering review context from a worktree.

Provides verified git metadata, handoff content, and dev-notes extraction used
by the review pool and entry points in engine.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from theforge.artifacts import resolve_handoff_path
from theforge.config import ForgeConfig
from theforge.devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff

from . import util as _cu

if TYPE_CHECKING:
    pass


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

    ok, log = _cu._run_shell(f"git log {base_branch}..HEAD --oneline --reverse", workspace_path)

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


def _get_diff_stat(workspace_path: Path, base_branch: str = "main") -> str:
    """Get verified diff stat vs the base branch."""
    ok, stat = _cu._run_shell(f"git diff {base_branch} --stat", workspace_path)
    if ok and stat.strip():
        return stat
    return "(no files changed vs base branch)"


def _get_diff_content(
    workspace_path: Path,
    base_branch: str = "main",
    *,
    max_chars: int = 300_000,
) -> str:
    """Get verified diff content vs the base branch, truncating when too large."""
    ok, diff = _cu._run_shell(f"git diff {base_branch}", workspace_path)
    if not ok and not diff.strip():
        return "(failed to load git diff vs base branch)"
    if not diff.strip():
        return "(no diff vs base branch)"
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + f"\n[... diff truncated at {max_chars} chars ...]"


def _normalize_commit_lines(text: str) -> list[str]:
    """Normalize git log text into comparable one-line commit entries."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _get_raw_commit_lines(workspace_path: Path, base_branch: str = "main") -> list[str]:
    """Return raw one-line commits ahead of base branch without human-readable warnings."""
    ok, log = _cu._run_shell(f"git log {base_branch}..HEAD --oneline --reverse", workspace_path)
    if not ok or not log.strip():
        return []
    return _normalize_commit_lines(log)


def _handoff_commit_lines(handoff: DevHandoff | None) -> list[str] | None:
    """Return normalized commit lines from parsed handoff, or None if unavailable."""
    if handoff is None or handoff.parse_errors:
        return None
    lines: list[str] = []
    for commit in handoff.commits:
        sha = commit.get("sha", "").strip()
        message = commit.get("message", "").strip()
        if sha and message:
            lines.append(f"{sha} {message}")
    return lines


def _get_handoff_commit_warning(
    config: ForgeConfig, workspace_path: Path, base_branch: str
) -> str | None:
    """Compare self-reported handoff commits to git log and return a warning if mismatched."""
    handoff = _parse_dev_handoff(config, workspace_path)
    handoff_lines = _handoff_commit_lines(handoff)
    if handoff_lines is None:
        return None

    actual_lines = _get_raw_commit_lines(workspace_path, base_branch)
    actual_set = set(actual_lines)
    handoff_set = set(handoff_lines)

    missing_from_branch = [line for line in handoff_lines if line not in actual_set]
    omitted_from_handoff = [line for line in actual_lines if line not in handoff_set]

    if not missing_from_branch and not omitted_from_handoff:
        return None

    parts = [
        "⚠ WARNING: Dev handoff commit list does not match verified git history.",
    ]
    if missing_from_branch:
        parts.append("Claims not found on branch:")
        parts.extend(f"- {line}" for line in missing_from_branch)
    if omitted_from_handoff:
        parts.append("Commits present on branch but omitted from handoff:")
        parts.extend(f"- {line}" for line in omitted_from_handoff)
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


def _get_commit_diffs(
    workspace_path: Path,
    base_branch: str,
    per_commit_limit: int = 50_000,
    total_limit: int = 300_000,
) -> str:
    """Get full `git show` output for each commit on the branch, with truncation."""
    ok, log = _cu._run_shell(f"git log {base_branch}..HEAD --oneline --reverse", workspace_path)
    if not ok or not log.strip():
        return "(no commits ahead of base branch)"

    commit_lines = [line.strip() for line in log.splitlines() if line.strip()]
    parts: list[str] = []
    total_chars = 0

    for idx, line in enumerate(commit_lines):
        sha = line.split()[0]
        ok_show, diff = _cu._run_shell(f"git show {sha}", workspace_path)
        if not ok_show:
            diff = f"(failed to load git show for {sha})"
        if len(diff) > per_commit_limit:
            diff = (
                diff[:per_commit_limit] + f"\n[... diff truncated at {per_commit_limit} chars ...]"
            )

        addition = diff if not parts else f"\n\n{diff}"
        if total_chars + len(addition) > total_limit:
            remaining = len(commit_lines) - idx
            parts.append(f"[... remaining {remaining} commits omitted — total diff too large ...]")
            break

        parts.append(diff)
        total_chars += len(addition)

    return "\n\n".join(parts)
