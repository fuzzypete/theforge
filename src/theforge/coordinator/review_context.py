"""Helpers for gathering review context from a worktree.

Provides verified git metadata, handoff content, and dev-notes extraction used
by the review pool and entry points in engine.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from theforge.devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff

from . import util as _cu

if TYPE_CHECKING:
    from theforge.config import ForgeConfig

    from .state import CoordinatorState


def hard_convention_review_kwargs(config: ForgeConfig) -> dict[str, object]:
    """Extract the hard-convention kwargs that build_review_prompt expects.

    Centralizes the propagation of ``config.conventions_hard`` (allowed_root_files,
    no_scratch_files) into the reviewer prompt so the reviewer can proactively
    reject diffs that introduce repo-root scratch files instead of relying on
    the runtime hygiene gate as the sole backstop.
    """
    hard = config.conventions_hard
    if hard is None:
        return {"allowed_root_files": None, "no_scratch_files": None}
    return {
        "allowed_root_files": hard.allowed_root_files,
        "no_scratch_files": hard.no_scratch_files,
    }


def _latest_forge_handoff_path(state: CoordinatorState) -> Path | None:
    """Return the forge artifact path from the most recent dev iteration, if present."""
    if not state.dev_handoff_snapshots:
        return None
    snap = state.dev_handoff_snapshots[-1]
    if not isinstance(snap, dict):
        return None
    if snap.get("source") == "structured_output" and snap.get("path"):
        p = Path(snap["path"])
        return p if p.exists() else None
    return None


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
    ok, stat = _cu._run_shell(f"git diff {base_branch}...HEAD --stat", workspace_path)
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
    ok, diff = _cu._run_shell(f"git diff {base_branch}...HEAD", workspace_path)
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


def _get_handoff_commit_mismatch(
    workspace_path: Path,
    base_branch: str,
    forge_handoff_path: Path | None = None,
) -> str | None:
    """Compare self-reported handoff commits to git log and return mismatch details."""
    handoff = _parse_dev_handoff(forge_handoff_path=forge_handoff_path)
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
        "Dev handoff commit list does not match verified git history.",
    ]
    if missing_from_branch:
        parts.append("Claims not found on branch:")
        parts.extend(f"- {line}" for line in missing_from_branch)
    if omitted_from_handoff:
        parts.append("Commits present on branch but omitted from handoff:")
        parts.extend(f"- {line}" for line in omitted_from_handoff)
    return "\n".join(parts)


def _get_handoff_commit_warning(
    workspace_path: Path,
    base_branch: str,
    forge_handoff_path: Path | None = None,
) -> str | None:
    """Compare self-reported handoff commits to git log and return a warning if mismatched."""
    mismatch = _get_handoff_commit_mismatch(
        workspace_path, base_branch, forge_handoff_path=forge_handoff_path
    )
    if mismatch is None:
        return None
    return f"⚠ WARNING: {mismatch}"


def _get_handoff_content(
    forge_handoff_path: Path | None = None,
) -> str:
    """Read the forge artifact handoff content as text for the reviewer."""
    if forge_handoff_path is not None and forge_handoff_path.exists():
        raw = forge_handoff_path.read_text(encoding="utf-8")
        return f"[Captured from agent structured output]\n{raw}"
    return "(no forge handoff artifact)"


def _get_raw_dev_notes(
    forge_handoff_path: Path | None = None,
) -> str | None:
    """Extract raw dev_notes from the forge artifact, or None if absent."""
    if forge_handoff_path is not None and forge_handoff_path.exists():
        try:
            data = yaml.safe_load(forge_handoff_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            return yaml.dump(data, allow_unicode=True, default_flow_style=False)
    return None


def _parse_dev_handoff(
    forge_handoff_path: Path | None = None,
) -> DevHandoff | None:
    """Parse and validate the dev handoff from the forge artifact.

    Returns None when no forge artifact is present.
    Returns DevHandoff with parse_errors when the artifact is unreadable or invalid.
    """
    if forge_handoff_path is None or not forge_handoff_path.exists():
        return None
    raw = _get_raw_dev_notes(forge_handoff_path=forge_handoff_path)
    if raw is None:
        return DevHandoff(
            summary="",
            commits=[],
            acceptance_criteria=[],
            story_deviations=[],
            deferred_items=[],
            gate_result=None,
            parse_errors=[f"forge handoff artifact unreadable: {forge_handoff_path}"],
            raw={},
        )
    return parse_dev_handoff(raw)


def _get_dev_notes(
    workspace_path: Path,
    forge_handoff_path: Path | None = None,
) -> str | None:
    """Extract dev_notes from the forge artifact as reviewer text.

    If the dev handoff is valid structured YAML, formats it as structured
    markdown sections. Falls back to raw text if parsing fails.
    workspace_path is accepted but unused — kept for call-site compatibility.
    """
    raw = _get_raw_dev_notes(forge_handoff_path=forge_handoff_path)
    if raw is None:
        return None
    handoff = parse_dev_handoff(raw)
    if handoff.parse_errors:
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
