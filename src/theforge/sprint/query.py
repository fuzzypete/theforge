"""GitHub query helpers for sprint story collection.

Provides functions to fetch open issues from GitHub by milestone or label,
and to assemble a ResolvedSprint from the resulting issue list.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manifest import ResolvedSprint

logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


def fetch_issues_for_milestone(
    milestone: str,
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch open issues in a milestone, ordered by issue number.

    Returns a list of ``{"number": int, "title": str}`` dicts.
    Raises ``RuntimeError`` if the ``gh`` call fails.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--milestone",
        milestone,
        "--state",
        "open",
        "--json",
        "number,title",
        "--limit",
        "9999",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(project_root) if project_root else None,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue list --milestone {milestone!r} failed: {result.stderr.strip()}"
        )
    issues: list[dict] = json.loads(result.stdout) or []
    return sorted(issues, key=lambda x: x["number"])


def fetch_issues_for_label(
    label: str,
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch open issues with a label, ordered by issue number.

    Returns a list of ``{"number": int, "title": str}`` dicts.
    Raises ``RuntimeError`` if the ``gh`` call fails.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--label",
        label,
        "--state",
        "open",
        "--json",
        "number,title",
        "--limit",
        "9999",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(project_root) if project_root else None,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh issue list --label {label!r} failed: {result.stderr.strip()}")
    issues: list[dict] = json.loads(result.stdout) or []
    return sorted(issues, key=lambda x: x["number"])


def build_resolved_sprint(
    issues: list[dict],
    name: str,
    budget_usd: float,
    max_parallel: int | None,
    project_root: Path,
) -> ResolvedSprint:
    """Build a ResolvedSprint from a list of ``{"number", "title"}`` issue dicts.

    Fetches the full issue body for each issue via ``GitHubIssueSource``.
    Issues that are already closed at fetch time are skipped with a warning.

    Args:
        issues: Ordered list of ``{"number": int, "title": str}`` dicts.
        name: Sprint name.
        budget_usd: Budget ceiling in USD.
        max_parallel: Optional concurrency cap.
        project_root: Repository root (used for ``gh`` CWD).

    Returns:
        A fully populated ``ResolvedSprint`` ready for ``run_sprint()``.
    """
    from ..task import TaskStory  # noqa: PLC0415
    from .manifest import ResolvedSprint  # noqa: PLC0415
    from .sources import GitHubIssueSource, StorySource  # noqa: PLC0415

    source = GitHubIssueSource()
    stories: list[tuple[TaskStory, StorySource, str]] = []
    for issue in issues:
        number = issue["number"]
        try:
            task = source.fetch(str(number), project_root)
        except RuntimeError as exc:
            _log(f"WARNING: skipping issue #{number} — {exc}")
            continue
        canonical_ref = f"issue:{number}"
        stories.append((task, source, canonical_ref))

    return ResolvedSprint(
        name=name,
        budget_usd=budget_usd,
        stories=stories,
        max_parallel=max_parallel,
    )
