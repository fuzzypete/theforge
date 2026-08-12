"""GitHub query helpers for sprint story collection.

Provides functions to fetch open issues from GitHub by milestone or label,
and to assemble a ResolvedSprint from the resulting issue list.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from ..task import TaskStory
    from .manifest import ResolvedSprint
    from .unmeasured import AcceptedUnmeasuredSpend

from ..log_util import _log_line
from . import unmeasured as unmeasured_spend_policy
from .budget import budget_verification_spend

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


@dataclass(frozen=True)
class SprintCarryBudgetSnapshot:
    """Budget-relevant spend already attached to a logical sprint before dispatch."""

    sprint_id: str | None
    carried_cost_usd: float
    selected_cost_by_slug: dict[str, float]
    unresolved_unmeasured_sources: tuple[str, ...]
    accepted_unmeasured_spend: tuple["AcceptedUnmeasuredSpend", ...]
    accepted_unmeasured_ceiling_usd: float
    verification_spend_usd: float

    @property
    def headroom_is_lower_bound(self) -> bool:
        return bool(self.unresolved_unmeasured_sources)

    def remaining_headroom_usd(self, budget_usd: float) -> float:
        return round(float(budget_usd) - self.verification_spend_usd, 4)


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def _is_issue_slug(slug: str) -> bool:
    return re.fullmatch(r"issue-\d+", slug) is not None


def _existing_sprint_id(sprint_name: str, project_root: Path) -> str | None:
    """Return the stable sprint id when one already exists, else ``None``."""
    sprint_id_path = project_root / ".forge" / "logs" / sprint_name / ".sprint_id"
    try:
        if sprint_id_path.exists():
            sprint_id = sprint_id_path.read_text(encoding="utf-8").strip()
            return sprint_id or None
    except OSError:
        return None
    return None


def _prior_sprint_block(project_root: Path, sprint_id: str | None) -> dict:
    """Return this sprint's block from sprint-audit.yaml, or ``{}``."""
    if not sprint_id:
        return {}
    audit_path = project_root / ".forge" / "audits" / "sprint-audit.yaml"
    if not audit_path.exists():
        return {}
    try:
        import yaml  # noqa: PLC0415

        with open(audit_path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        sprint_block = data.get("sprint", {})
        if not isinstance(sprint_block, dict):
            return {}
        if sprint_block.get("sprint_id") != sprint_id:
            return {}
        return sprint_block
    except Exception:
        return {}


def prior_unmeasured_spend_sources(project_root: Path, sprint_id: str | None) -> list[str]:
    """The sources the prior generation recorded as unmeasured, if any."""
    recorded = _prior_sprint_block(project_root, sprint_id).get("unmeasured_spend_sources") or []
    return [str(source) for source in recorded if source] if isinstance(recorded, list) else []


def prior_sprint_cost_incomplete(
    project_root: Path,
    sprint_id: str | None,
    accepted: Mapping[str, "AcceptedUnmeasuredSpend"] | None = None,
) -> bool:
    """Return True when the prior generation recorded an incomplete sprint cost."""
    sprint_block = _prior_sprint_block(project_root, sprint_id)
    if sprint_block.get("cost_complete") is not False:
        return False
    if accepted:
        recorded = sprint_block.get("unmeasured_spend_sources") or []
        if isinstance(recorded, list) and unmeasured_spend_policy.all_sources_accepted(
            [str(source) for source in recorded if source], accepted
        ):
            return False
    return True


def _parse_accumulated_story_timestamp(value: object) -> datetime.datetime | None:
    """Parse timestamps persisted in accumulated sprint story state."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_prior_sprint_accounting(
    project_root: Path,
    sprint_id: str | None,
) -> tuple[float, datetime.datetime | None, dict[str, dict]]:
    """Recover prior same-sprint cost/timing from progressive story state."""
    if not sprint_id:
        return 0.0, None, {}

    from .audit import _load_accumulated_stories  # noqa: PLC0415

    recovered_entries: dict[str, dict] = {}
    recovered_cost = 0.0
    earliest_started_at: datetime.datetime | None = None
    for raw_entry in _load_accumulated_stories(sprint_id, project_root):
        if not isinstance(raw_entry, dict):
            continue
        canonical_ref = raw_entry.get("canonical_ref")
        if not isinstance(canonical_ref, str) or not canonical_ref:
            continue
        entry = dict(raw_entry)
        recovered_entries[canonical_ref] = entry
        try:
            recovered_cost += float(entry.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        started_at = _parse_accumulated_story_timestamp(entry.get("started_at"))
        if started_at is not None and (
            earliest_started_at is None or started_at < earliest_started_at
        ):
            earliest_started_at = started_at

    return round(recovered_cost, 4), earliest_started_at, recovered_entries


def load_sprint_carry_budget_snapshot(
    *,
    project_root: Path,
    sprint_name: str,
    selected_slugs: list[str],
    sprint_id: str | None = None,
    accepted_unmeasured: Mapping[str, "AcceptedUnmeasuredSpend"] | None = None,
) -> SprintCarryBudgetSnapshot:
    """Return the carried budget state the next dispatch will inherit."""
    resolved_sprint_id = sprint_id or _existing_sprint_id(sprint_name, project_root)
    if not resolved_sprint_id:
        return SprintCarryBudgetSnapshot(
            sprint_id=None,
            carried_cost_usd=0.0,
            selected_cost_by_slug={},
            unresolved_unmeasured_sources=(),
            accepted_unmeasured_spend=(),
            accepted_unmeasured_ceiling_usd=0.0,
            verification_spend_usd=0.0,
        )

    carried_cost_usd, _started_at, entries_by_ref = read_prior_sprint_accounting(
        project_root, resolved_sprint_id
    )
    selected = set(selected_slugs)
    selected_cost_by_slug: dict[str, float] = {}
    occurrence_ids: dict[str, str | None] = {}
    raw_unmeasured_sources: list[str] = []
    for entry in entries_by_ref.values():
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        try:
            cost_usd = float(entry.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            cost_usd = 0.0
        if slug in selected and cost_usd > 0.0:
            selected_cost_by_slug[slug] = selected_cost_by_slug.get(slug, 0.0) + cost_usd
        if "cost_usd" in entry and entry.get("cost_usd") is None:
            raw_source = f"carried:{slug}"
            raw_unmeasured_sources.append(raw_source)
            origin = unmeasured_spend_policy.build_source(
                raw_source,
                unmeasured_spend_policy.read_story_audit(project_root, sprint_name, slug),
            ).origin
            occurrence_ids[raw_source] = origin.get("run_id")

    if accepted_unmeasured is not None:
        accepted_index = dict(accepted_unmeasured)
    else:
        from .audit import _load_accepted_unmeasured_spend  # noqa: PLC0415

        accepted_index = unmeasured_spend_policy.accepted_by_source(
            _load_accepted_unmeasured_spend(resolved_sprint_id, project_root)
        )
    if prior_sprint_cost_incomplete(project_root, resolved_sprint_id, accepted_index):
        raw_unmeasured_sources.append("carried:prior-generation")
    unresolved, applied = unmeasured_spend_policy.partition(
        raw_unmeasured_sources,
        accepted_index,
        current_generation=set(),
        occurrence_ids=occurrence_ids,
    )
    accepted_ceiling_usd = unmeasured_spend_policy.accepted_ceiling_total(applied)
    verification_spend_usd = budget_verification_spend(
        accumulated_cost=0.0,
        prior_cost=carried_cost_usd,
        accepted_unmeasured_ceiling_usd=accepted_ceiling_usd,
    )
    return SprintCarryBudgetSnapshot(
        sprint_id=resolved_sprint_id,
        carried_cost_usd=carried_cost_usd,
        selected_cost_by_slug=dict(sorted(selected_cost_by_slug.items())),
        unresolved_unmeasured_sources=tuple(unresolved),
        accepted_unmeasured_spend=tuple(applied),
        accepted_unmeasured_ceiling_usd=accepted_ceiling_usd,
        verification_spend_usd=verification_spend_usd,
    )


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
    closed_dependency_slugs: set[str] = set()
    for issue in issues:
        number = issue["number"]
        try:
            task = source.fetch(str(number), project_root)
        except IssueClosedError as exc:
            _log(f"WARNING: skipping issue #{number} — {exc}")
            closed_dependency_slugs.add(f"issue-{number}")
            continue
        canonical_ref = f"issue:{number}"
        stories.append((task, source, canonical_ref))

    return ResolvedSprint(
        name=name,
        budget_usd=budget_usd,
        stories=stories,
        max_parallel=max_parallel,
        closed_dependency_slugs=closed_dependency_slugs,
    )
