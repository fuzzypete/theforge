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
from urllib.parse import quote

if TYPE_CHECKING:
    from .manifest import ResolvedSprint

logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


def _gh_api_paginate_issues(
    endpoint: str,
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch all issues from a GitHub API endpoint with no hard cap.

    Uses ``gh api --paginate`` to iterate through all result pages automatically.
    Returns a list of ``{"number": int, "title": str}`` dicts.
    Raises ``RuntimeError`` if the ``gh`` call fails.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--jq",
            ".[] | {number: .number, title: .title}",
            endpoint,
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root) if project_root else None,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {endpoint!r} failed: {result.stderr.strip()}")
    issues: list[dict] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                issues.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"gh api returned malformed JSON: {exc}") from exc
    return issues


def _get_milestone_number(
    milestone: str,
    project_root: Path | None = None,
) -> str:
    """Return the GitHub milestone number for the given milestone title.

    Raises ``RuntimeError`` if the milestone is not found or the ``gh`` call fails.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--jq",
            f".[] | select(.title == {json.dumps(milestone)}) | .number",
            "repos/{owner}/{repo}/milestones",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root) if project_root else None,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to look up milestone {milestone!r}: {result.stderr.strip()}")
    number = result.stdout.strip()
    if not number:
        raise RuntimeError(f"Milestone {milestone!r} not found in this repository")
    return number


def fetch_issues_for_milestone(
    milestone: str,
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch ALL open issues in a milestone with no hard cap on result count.

    Resolves the milestone title to a numeric ID, then pages through all open
    issues via ``gh api --paginate``.

    Returns a list of ``{"number": int, "title": str}`` dicts ordered by number.
    Raises ``RuntimeError`` if the milestone is not found or the ``gh`` call fails.
    """
    number = _get_milestone_number(milestone, project_root)
    endpoint = f"repos/{{owner}}/{{repo}}/issues?milestone={number}&state=open&per_page=100"
    issues = _gh_api_paginate_issues(endpoint, project_root)
    return sorted(issues, key=lambda x: x["number"])


def fetch_issues_for_label(
    label: str,
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch ALL open issues with a label with no hard cap on result count.

    Uses ``gh api --paginate`` for true pagination across all result pages.

    Returns a list of ``{"number": int, "title": str}`` dicts ordered by number.
    Raises ``RuntimeError`` if the ``gh`` call fails.
    """
    encoded = quote(label, safe="")
    endpoint = f"repos/{{owner}}/{{repo}}/issues?labels={encoded}&state=open&per_page=100"
    issues = _gh_api_paginate_issues(endpoint, project_root)
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
    Any other fetch failure (auth, network, malformed JSON) raises immediately.

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
    from .sources import GitHubIssueSource, IssueClosedError, StorySource  # noqa: PLC0415

    source = GitHubIssueSource()
    stories: list[tuple[TaskStory, StorySource, str]] = []
    for issue in issues:
        number = issue["number"]
        try:
            task = source.fetch(str(number), project_root)
        except IssueClosedError as exc:
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
