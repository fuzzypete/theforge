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
    from ..task import TaskStory
    from .manifest import ResolvedSprint

logger = logging.getLogger(__name__)


class MilestoneNotFoundError(RuntimeError):
    """Raised when a milestone title is absent from the repository."""


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
            # select(.pull_request == null) excludes PRs — the /issues endpoint
            # returns both issues and pull requests; .pull_request is absent (null)
            # for real issues and an object for pull requests.
            ".[] | select(.pull_request == null) | {number: .number, title: .title}",
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

    Raises ``MilestoneNotFoundError`` if the milestone is not found and
    ``RuntimeError`` if the ``gh`` call fails.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--jq",
            f".[] | select(.title == {json.dumps(milestone)}) | .number",
            "repos/{owner}/{repo}/milestones?state=all",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root) if project_root else None,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to look up milestone {milestone!r}: {result.stderr.strip()}")
    number = result.stdout.strip()
    if not number:
        raise MilestoneNotFoundError(f"Milestone {milestone!r} not found in this repository")
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
    try:
        number = _get_milestone_number(milestone, project_root)
    except MilestoneNotFoundError:
        if not milestone.isdigit():
            raise
        number = milestone
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


def assign_dependency_batches(
    tasks: list["TaskStory"],
    max_parallel: int | None,
) -> dict[str, int]:
    """Return deterministic dry-run batch numbers for dependency-aware execution."""
    from .dag import build_dag  # noqa: PLC0415

    batch_assignments: dict[str, int] = {}
    known_slugs = {task.slug for task in tasks}
    satisfied = {
        dep_slug for task in tasks for dep_slug in task.depends_on if dep_slug not in known_slugs
    }
    dag = build_dag(tasks, satisfied=satisfied)
    active_batch = 0
    _ = max_parallel  # width is enforced at runtime; dry-run batches reflect dependency frontiers

    while not dag.is_done():
        ready = [task for task in dag.ready() if task.slug not in batch_assignments]
        if not ready:
            break
        for task in ready:
            batch_assignments[task.slug] = active_batch
            dag.mark_complete(task.slug)
        active_batch += 1

    return batch_assignments


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
