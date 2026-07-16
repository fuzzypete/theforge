"""Operator-action queue: list operator-action issues with dependency readiness.

Slice 3 of epic #1469. Once the ``operator-action`` type is recognized at
intake (#1473) and excluded from the dev lifecycle (#1474), the operator still
has no surfaced view of which operator-actions are *next* on their queue. This
module computes, for each open ``operator-action`` issue, whether every issue it
``depends_on`` has landed (closed):

- **ready**   — all declared dependencies are closed (or there are none).
- **blocked** — at least one declared dependency is still open.

The readiness signal is recomputed from GitHub on every call, so re-running
``forge status --operator-actions`` (or a watch-mode refresh) always reflects
current dependency state without a manual refresh step.

``depends_on`` parsing reuses ``GitHubIssueSource`` so this surface honors the
exact same frontmatter/prose declarations the scheduler DAG does.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .sprint.shape_gate import OPERATOR_ACTION_LABEL

_GH_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class DependencyState:
    """A single ``depends_on`` reference and whether it has landed (closed)."""

    number: int
    closed: bool


@dataclass(frozen=True)
class OperatorActionEntry:
    """An open operator-action issue with its resolved dependency readiness."""

    issue_number: int
    title: str
    dependencies: tuple[DependencyState, ...]

    @property
    def pending(self) -> tuple[DependencyState, ...]:
        """Dependencies that have not yet landed (still open)."""
        return tuple(dep for dep in self.dependencies if not dep.closed)

    @property
    def pending_count(self) -> int:
        """Number of still-open dependencies gating this operator-action."""
        return len(self.pending)

    @property
    def ready(self) -> bool:
        """True when every declared dependency has landed (or there are none)."""
        return self.pending_count == 0


def _parse_body_dependencies(body: str) -> list[int]:
    """Return the ``depends_on`` issue numbers declared in an issue body.

    Reuses ``GitHubIssueSource`` so operator-action readiness honors the same
    frontmatter and prose dependency declarations the scheduler DAG consumes.
    """
    from .sprint.sources import GitHubIssueSource

    source = GitHubIssueSource()
    metadata = source._parse_issue_metadata(body, 0).data
    deps: set[int] = set(source._parse_issue_blockers_from_body_metadata(body, metadata))
    for _phrase, refs in source._find_prose_dependency_phrases(body):
        deps.update(refs)
    return sorted(deps)


def _gh_list_operator_actions(project_root: Path) -> list[dict]:
    """Return open operator-action issues (number/title/body) via the gh CLI.

    Best-effort: returns an empty list on any gh failure so the status surface
    degrades to "no operator-actions" rather than crashing.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--label",
                OPERATOR_ACTION_LABEL,
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,title,body",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if proc.returncode != 0:
        return []

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and "number" in item]


def _gh_issue_closed_states(numbers: list[int], project_root: Path) -> dict[int, bool]:
    """Return ``{issue_number: is_closed}`` for the given dependency issues.

    Best-effort per issue: an unresolvable dependency defaults to *not* closed
    (open) so an operator-action is never falsely reported ready when its
    dependency state could not be verified.
    """
    states: dict[int, bool] = {}
    for number in numbers:
        try:
            proc = subprocess.run(
                ["gh", "issue", "view", str(number), "--json", "state"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=_GH_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError):
            states[number] = False
            continue
        if proc.returncode != 0:
            states[number] = False
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            states[number] = False
            continue
        state = str(data.get("state", "")).upper()
        states[number] = state == "CLOSED"
    return states


def build_operator_action_queue(
    project_root: Path,
    *,
    fetch_issues: Callable[[], list[dict]] | None = None,
    fetch_states: Callable[[list[int]], dict[int, bool]] | None = None,
) -> list[OperatorActionEntry]:
    """Return the operator-action queue with resolved dependency readiness.

    ``fetch_issues`` / ``fetch_states`` are injection seams for testing; they
    default to the gh-backed implementations. Entries are sorted by issue number
    for stable, tooling-friendly output.
    """
    fetch_issues = fetch_issues or (lambda: _gh_list_operator_actions(project_root))
    fetch_states = fetch_states or (lambda nums: _gh_issue_closed_states(nums, project_root))

    issues = fetch_issues()
    parsed: list[tuple[dict, list[int]]] = [
        (issue, _parse_body_dependencies(str(issue.get("body", "") or ""))) for issue in issues
    ]

    all_deps = sorted({num for _issue, deps in parsed for num in deps})
    states = fetch_states(all_deps) if all_deps else {}

    entries: list[OperatorActionEntry] = []
    for issue, deps in parsed:
        dep_states = tuple(DependencyState(num, states.get(num, False)) for num in deps)
        entries.append(
            OperatorActionEntry(
                issue_number=int(issue["number"]),
                title=str(issue.get("title", "") or ""),
                dependencies=dep_states,
            )
        )
    entries.sort(key=lambda entry: entry.issue_number)
    return entries


def format_operator_action_queue(entries: list[OperatorActionEntry]) -> str:
    """Render the operator-action queue as human- and tooling-parseable text.

    Layout is intentionally stable (fixed column order: state, issue ref, title,
    dependency detail) so adjacent tooling can consume it without breaking on
    cosmetic changes::

        Operator-action queue (3 issues):
          ready    #1471  Validate v0.11 substrate (deps: #1326 ok, #1437 ok)
          blocked  #1480  Cut v0.11.0rc1 (deps: #1450 open, #1469 open - 2 pending)
          ready    #1495  Coordinate vendor quota (no deps)
    """
    if not entries:
        return "Operator-action queue: none."

    noun = "issue" if len(entries) == 1 else "issues"
    lines = [f"Operator-action queue ({len(entries)} {noun}):"]
    for entry in entries:
        state = "ready  " if entry.ready else "blocked"
        deps_detail = _format_dependency_detail(entry)
        lines.append(f"  {state}  #{entry.issue_number}  {entry.title} {deps_detail}")
    return "\n".join(lines)


def _format_dependency_detail(entry: OperatorActionEntry) -> str:
    """Render the ``(deps: ...)`` suffix for one operator-action entry."""
    if not entry.dependencies:
        return "(no deps)"
    refs = ", ".join(
        f"#{dep.number} {'ok' if dep.closed else 'open'}" for dep in entry.dependencies
    )
    if entry.ready:
        return f"(deps: {refs})"
    return f"(deps: {refs} - {entry.pending_count} pending)"
