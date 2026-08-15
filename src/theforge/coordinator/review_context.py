"""Helpers for gathering review context from a worktree.

Provides verified git metadata, handoff content, and dev-notes extraction used
by the review pool and entry points in engine.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from theforge.devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff
from theforge.validation_profiles import last_merge_authority_record

from . import util as _cu

if TYPE_CHECKING:
    from theforge.config import ForgeConfig

    from .state import CoordinatorState


def gate_profile_prompt_kwargs(state: CoordinatorState) -> dict[str, object]:
    """Name the validation profile behind the verdict a reviewer is handed (#2358).

    Both prompt-building routes (the normal review pool and the review-only
    entry point) go through this, so a reviewer cannot be told the verdict
    without also being told what standing it carries. Empty when no
    merge-authority run was recorded — a legacy config, an older resumed record,
    or a story whose gate was suppressed — in which case the prompt reads
    exactly as it did before profiles existed.
    """
    record = last_merge_authority_record(getattr(state, "validation_runs", None))
    if record is None:
        return {}
    return {
        "authoritative_gate_profile": record.get("profile"),
        "authoritative_gate_authority": record.get("authority"),
    }


def hard_convention_review_kwargs(config: ForgeConfig) -> dict[str, object]:
    """Extract the hard-convention kwargs that build_review_prompt expects.

    Centralizes the propagation of ``config.conventions_hard`` (stack,
    allowed_root_files, no_scratch_files) into the reviewer prompt so the
    reviewer can proactively reject diffs that introduce repo-root scratch
    files instead of relying on the runtime hygiene gate as the sole backstop.
    """
    hard = config.conventions_hard
    if hard is None:
        return {"stack": None, "allowed_root_files": None, "no_scratch_files": None}
    return {
        "stack": hard.stack,
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


def _split_commit_line(line: str) -> tuple[str, str]:
    """Split a ``"<sha> <message>"`` commit line into ``(lowercased sha, message)``."""
    parts = line.split(maxsplit=1)
    if not parts:
        return "", ""
    sha = parts[0].strip().lower()
    message = parts[1].strip() if len(parts) > 1 else ""
    return sha, message


def _commits_match(a: str, b: str) -> bool:
    """Return True when two commit lines refer to the same commit.

    Messages must be identical; the SHAs match when one is a prefix of the
    other. ``git log --oneline`` abbreviates SHAs while a dev handoff may cite
    the full 40-char SHA (or vice-versa), so an exact string compare would flag
    a legitimately clean run as a mismatch (issue #1851). Prefix matching
    reconciles the two forms without loosening the message check.
    """
    sha_a, msg_a = _split_commit_line(a)
    sha_b, msg_b = _split_commit_line(b)
    if msg_a != msg_b or not sha_a or not sha_b:
        return False
    return sha_a.startswith(sha_b) or sha_b.startswith(sha_a)


def _reconcile_handoff_commits(
    handoff_lines: list[str], actual_lines: list[str]
) -> tuple[list[str], list[str]]:
    """Return ``(missing_from_branch, omitted_from_handoff)`` tolerant of SHA abbreviation.

    A handoff commit is "missing" only when no git commit matches it by message
    and prefix-compatible SHA; symmetrically for commits omitted from the
    handoff. Shared by the prose mismatch warning and the structured
    tree-currency trust check so both stop false-tainting full-SHA handoffs.
    """
    missing_from_branch = [
        line
        for line in handoff_lines
        if not any(_commits_match(line, actual) for actual in actual_lines)
    ]
    omitted_from_handoff = [
        line
        for line in actual_lines
        if not any(_commits_match(line, handoff) for handoff in handoff_lines)
    ]
    return missing_from_branch, omitted_from_handoff


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
    missing_from_branch, omitted_from_handoff = _reconcile_handoff_commits(
        handoff_lines, actual_lines
    )

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


REVIEWER_TREE_CURRENCY_CHECK = "reviewer_tree_currency"
REVIEWER_TREE_CURRENCY_PRODUCER = "coordinator.review_context.reviewer_tree_currency"


def evaluate_reviewer_tree_currency(
    workspace_path: Path,
    base_branch: str,
    forge_handoff_path: Path | None = None,
) -> dict | None:
    """Return a structured reviewer tree-currency trust-check entry, or None.

    Promotes the coordinator-computed handoff-vs-git commit reconciliation (the
    landed #1826 tree-currency mechanism) from prose-only review context into a
    machine-readable pass/fail trust-check entry (issue #1851). This is the
    first mechanical producer of the trust-status marker: the same evidence that
    drives the reviewer's prose warning also decides whether the run is trusted.

    Applicability: the check only applies when the dev handoff exists and parses
    with a usable commit list. When there is no parseable handoff there is
    nothing to reconcile against, so return None (not-applicable → no entry →
    the run stays ``unchecked``; absence of a proof is not taint, ADR-0006
    clause 4).

    Result: FAIL when the handoff's self-reported commits diverge from verified
    git history (a stale-checkout / wrong-tree signal, #1534), PASS when they
    reconcile exactly. Evidence carries the expected (handoff) and actual (git)
    commit lists plus the specific divergences so the taint is auditable.
    """
    from .trust_status import CHECK_FAIL, CHECK_PASS, make_trust_check  # noqa: PLC0415

    handoff = _parse_dev_handoff(forge_handoff_path=forge_handoff_path)
    handoff_lines = _handoff_commit_lines(handoff)
    if handoff_lines is None:
        return None

    actual_lines = _get_raw_commit_lines(workspace_path, base_branch)
    missing_from_branch, omitted_from_handoff = _reconcile_handoff_commits(
        handoff_lines, actual_lines
    )

    reconciled = not missing_from_branch and not omitted_from_handoff
    evidence = {
        "base_branch": base_branch,
        "expected_commits": handoff_lines,
        "actual_commits": actual_lines,
        "missing_from_branch": missing_from_branch,
        "omitted_from_handoff": omitted_from_handoff,
    }
    return make_trust_check(
        check=REVIEWER_TREE_CURRENCY_CHECK,
        result=CHECK_PASS if reconciled else CHECK_FAIL,
        producer=REVIEWER_TREE_CURRENCY_PRODUCER,
        evidence=evidence,
    )


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
