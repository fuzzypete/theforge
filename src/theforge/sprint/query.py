"""GitHub query helpers for sprint story collection.

Provides functions to fetch open issues from GitHub by milestone or label,
and to assemble a ResolvedSprint from the resulting issue list.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from ..log_util import _log_line

if TYPE_CHECKING:
    from ..task import TaskStory
    from .manifest import ResolvedSprint

logger = logging.getLogger(__name__)


class MilestoneNotFoundError(RuntimeError):
    """Raised when a milestone title is absent from the repository."""


@dataclass(frozen=True)
class DependencyBatchPlan:
    """Dry-run dependency analysis for a sprint query."""

    assignments: dict[str, int]
    blocked: dict[str, list[str]]


@dataclass(frozen=True)
class NormalizedDependencyPlan:
    """Normalized task graph plus explicit blocked stories."""

    tasks: list["TaskStory"]
    blocked: dict[str, list[str]]


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def _is_issue_slug(slug: str) -> bool:
    return re.fullmatch(r"issue-\d+", slug) is not None


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


def fetch_issues_by_numbers(
    numbers: list[int],
    project_root: Path | None = None,
) -> list[dict]:
    """Fetch specific issues by number.

    Returns a list of ``{"number": int, "title": str}`` dicts ordered by number.
    Raises ``RuntimeError`` if any requested issue is missing or a ``gh`` call fails.
    """
    issues: list[dict] = []
    found_numbers: set[int] = set()

    for number in numbers:
        result = subprocess.run(
            ["gh", "issue", "view", str(number), "--json", "number,title"],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Could not resolve to an issue" in stderr or "not found" in stderr.lower():
                continue
            raise RuntimeError(f"gh issue view {number!r} failed: {stderr}")
        try:
            issue = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"gh issue view returned malformed JSON for #{number}: {exc}"
            ) from exc
        issues.append({"number": issue["number"], "title": issue["title"]})
        found_numbers.add(issue["number"])

    missing = sorted(set(numbers) - found_numbers)
    if missing:
        missing_str = ", ".join(str(number) for number in missing)
        raise RuntimeError(f"Issue number(s) not found in this repository: {missing_str}")

    return sorted(issues, key=lambda x: x["number"])


def assign_dependency_batches(
    tasks: list["TaskStory"],
    max_parallel: int | None,
) -> DependencyBatchPlan:
    """Return dry-run dependency batches and unresolved external blockers."""
    return assign_dependency_batches_with_satisfied(tasks, max_parallel, satisfied=set())


def assign_dependency_batches_with_satisfied(
    tasks: list["TaskStory"],
    max_parallel: int | None,
    *,
    satisfied: set[str],
) -> DependencyBatchPlan:
    """Return dry-run batches while preserving unresolved external blockers."""
    from .dag import build_dag  # noqa: PLC0415

    batch_assignments: dict[str, int] = {}
    normalized = normalize_dependency_plan(tasks, satisfied=satisfied)
    blocked = dict(normalized.blocked)
    dag = build_dag(normalized.tasks, satisfied=satisfied)
    active_batch = 0
    _ = max_parallel  # width is enforced at runtime; dry-run batches reflect dependency frontiers

    while not dag.is_done():
        ready = [
            task
            for task in dag.ready()
            if task.slug not in batch_assignments and task.slug not in blocked
        ]
        if not ready:
            for task in dag.remaining():
                if task.slug in blocked:
                    continue
                unmet = sorted(dag.unmet_deps(task.slug))
                if unmet:
                    blocked[task.slug] = unmet
            break
        for task in ready:
            batch_assignments[task.slug] = active_batch
            dag.mark_complete(task.slug)
        active_batch += 1

    return DependencyBatchPlan(assignments=batch_assignments, blocked=blocked)


def normalize_dependency_plan(
    tasks: list["TaskStory"],
    *,
    satisfied: set[str],
) -> NormalizedDependencyPlan:
    """Normalize external dependencies and classify stories blocked before scheduling.

    External dependencies already listed in ``satisfied`` are removed from the
    DAG inputs. Unresolved issue-backed dependencies remain in the DAG so a live
    scheduler can re-check GitHub issue state and unblock them on a later tick.
    Other unresolved external dependencies are preserved in ``blocked`` so
    callers can keep the affected stories out of the runnable graph while still
    reporting the blocker chain explicitly.
    """
    known_slugs = {task.slug for task in tasks}
    blocked = {
        task.slug: sorted(
            dep_slug
            for dep_slug in task.depends_on
            if dep_slug not in known_slugs
            and dep_slug not in satisfied
            and not _is_issue_slug(dep_slug)
        )
        for task in tasks
    }
    blocked = {slug: dep_slugs for slug, dep_slugs in blocked.items() if dep_slugs}
    normalized_tasks = [
        replace(
            task,
            depends_on=[
                dep_slug
                for dep_slug in task.depends_on
                if dep_slug in known_slugs
                or dep_slug in satisfied
                or (dep_slug not in known_slugs and _is_issue_slug(dep_slug))
            ],
        )
        for task in tasks
    ]
    return NormalizedDependencyPlan(tasks=normalized_tasks, blocked=blocked)


def _restored_prior_story(issue: dict, number: int, slug: str, prior: dict) -> TaskStory:
    """A record-only ``TaskStory`` for an issue this sprint already ran.

    The issue is closed, so its body was never fetched: this story exists to
    carry the sprint's own recorded execution forward into the audit, and the
    runner refuses to dispatch it for exactly that reason. Only the identity and
    scheduling fields the accumulated record already holds are reconstructed.
    """
    from ..task import TaskStory  # noqa: PLC0415

    title = issue.get("title") or prior.get("path") or f"Issue #{number}"
    depends_on = [dep for dep in (prior.get("depends_on") or []) if isinstance(dep, str)]
    return TaskStory(
        name=str(title),
        slug=slug,
        github_issue=number,
        depends_on=depends_on,
    )


def build_resolved_sprint(
    issues: list[dict],
    name: str,
    budget_usd: float,
    max_parallel: int | None,
    project_root: Path,
    prior_outcomes: "dict[str, dict] | None" = None,
) -> ResolvedSprint:
    """Build a ResolvedSprint from a list of ``{"number", "title"}`` issue dicts.

    Fetches the full issue body for each issue via ``GitHubIssueSource``.
    Issues that are already closed at fetch time are skipped with a warning.
    Any other fetch failure (auth, network, malformed JSON) raises immediately.

    ``prior_outcomes`` is this sprint's own accumulated story record, keyed by
    slug, supplied on the re-exec path. A story that landed just before a
    mid-sprint re-exec has closed its issue by the time the new process image
    re-resolves the sprint, and classifying it as a pre-existing closed
    dependency wrote it out of the record of the sprint that ran it while its
    spend stayed in the sprint total (#2847). Whether a story earns a record is
    settled by whether this sprint did the work, so an issue whose closure this
    sprint's own record accounts for stays a story of the sprint. An issue
    nothing in the record accounts for is still an external closed dependency.

    Args:
        issues: Ordered list of ``{"number": int, "title": str}`` dicts.
        name: Sprint name.
        budget_usd: Budget ceiling in USD.
        max_parallel: Optional concurrency cap.
        project_root: Repository root (used for ``gh`` CWD).
        prior_outcomes: Slug -> prior-generation story record, or ``None``.

    Returns:
        A fully populated ``ResolvedSprint`` ready for ``run_sprint()``.
    """
    from .manifest import ResolvedSprint  # noqa: PLC0415
    from .prior_landing import prior_execution_recorded  # noqa: PLC0415
    from .sources import GitHubIssueSource, IssueClosedError, StorySource  # noqa: PLC0415

    prior_outcomes = prior_outcomes or {}
    source = GitHubIssueSource()
    stories: list[tuple[TaskStory, StorySource, str]] = []
    closed_dependency_slugs: set[str] = set()
    reconciled_prior_slugs: set[str] = set()
    for issue in issues:
        number = issue["number"]
        slug = f"issue-{number}"
        canonical_ref = f"issue:{number}"
        try:
            task = source.fetch(str(number), project_root)
        except IssueClosedError as exc:
            prior = prior_outcomes.get(slug)
            if isinstance(prior, dict) and prior_execution_recorded(prior):
                _log(
                    f"RESTORED issue #{number} — closed, but this sprint's own record shows it "
                    f"ran here (outcome {prior.get('outcome') or 'unknown'}); keeping its story "
                    "record rather than reclassifying it as a closed dependency"
                )
                stories.append(
                    (_restored_prior_story(issue, number, slug, prior), source, canonical_ref)
                )
                reconciled_prior_slugs.add(slug)
                continue
            _log(f"WARNING: skipping issue #{number} — {exc}")
            closed_dependency_slugs.add(slug)
            continue
        stories.append((task, source, canonical_ref))

    return ResolvedSprint(
        name=name,
        budget_usd=budget_usd,
        stories=stories,
        max_parallel=max_parallel,
        closed_dependency_slugs=closed_dependency_slugs,
        reconciled_prior_slugs=reconciled_prior_slugs,
    )
