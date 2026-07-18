"""Ready-label queue: list sprint-eligible issues carrying the ``ready`` label.

Per ADR-0001, the mid-sprint workflow does **not** introduce a ``forge queue``
command. "Queue for next sprint" means the ``ready`` label is applied, and
normal sprint selection picks the issue up. This module surfaces that eligible
set so the operator can see, at a glance, which open issues are ready for the
next sprint — optionally scoped to a milestone.

No queue *ordering* or *priority* semantics are added: the list is simply the
current set of open, ``ready``-labeled issues, recomputed from GitHub on each
call so it always reflects live label/milestone state.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

READY_LABEL = "ready"

_GH_TIMEOUT_SECONDS = 30

# Dev-runnable and operator work-object type labels, in the order we prefer to
# report them when an issue carries more than one. This is display-only — it
# does not gate eligibility (the ``ready`` label alone does that).
_TYPE_LABELS = (
    "bug",
    "enhancement",
    "task",
    "documentation",
    "epic",
    "operator-action",
)


@dataclass(frozen=True)
class ReadyEntry:
    """An open, ``ready``-labeled issue eligible for the next sprint."""

    issue_number: int
    title: str
    type_label: str


def _issue_type_label(labels: list[str]) -> str:
    """Return the best display type for an issue from its label set.

    Prefers a known work-object type (bug, enhancement, ...) in a stable order;
    falls back to ``"—"`` when the issue carries no recognized type label.
    """
    label_set = {name.lower() for name in labels}
    for candidate in _TYPE_LABELS:
        if candidate in label_set:
            return candidate
    return "—"


def _gh_list_ready_issues(project_root: Path, milestone: str | None) -> list[dict]:
    """Return open ``ready``-labeled issues (number/title/labels) via the gh CLI.

    When ``milestone`` is given, the listing is scoped to that GitHub milestone.
    Best-effort: returns an empty list on any gh failure so the status surface
    degrades to "no ready issues" rather than crashing.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--label",
        READY_LABEL,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,labels",
    ]
    if milestone:
        cmd.extend(["--milestone", milestone])

    try:
        proc = subprocess.run(
            cmd,
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


def _label_names(issue: dict) -> list[str]:
    """Extract label names from a gh issue JSON record."""
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def build_ready_queue(
    project_root: Path,
    *,
    milestone: str | None = None,
    fetch_issues: Callable[[], list[dict]] | None = None,
) -> list[ReadyEntry]:
    """Return the ``ready``-labeled, sprint-eligible issue set.

    ``milestone`` optionally scopes the listing to one GitHub milestone.
    ``fetch_issues`` is an injection seam for testing; it defaults to the
    gh-backed implementation. Entries are sorted by issue number for stable,
    tooling-friendly output.
    """
    fetch_issues = fetch_issues or (lambda: _gh_list_ready_issues(project_root, milestone))

    issues = fetch_issues()
    entries: list[ReadyEntry] = []
    for issue in issues:
        entries.append(
            ReadyEntry(
                issue_number=int(issue["number"]),
                title=str(issue.get("title", "") or ""),
                type_label=_issue_type_label(_label_names(issue)),
            )
        )
    entries.sort(key=lambda entry: entry.issue_number)
    return entries


def format_ready_queue(entries: list[ReadyEntry], *, milestone: str | None = None) -> str:
    """Render the ready-label queue as human- and tooling-parseable text.

    Column order is stable (issue ref, type, ``ready`` marker, title) so adjacent
    tooling can consume it without breaking on cosmetic changes::

        Ready for next sprint (2 issues):
          #1487  bug          ready  status --watch blank during preflight
          #1512  bug          ready  cut-rc.sh shim wrapper regression
    """
    scope = f" in {milestone}" if milestone else ""
    if not entries:
        return f"Ready for next sprint{scope}: none."

    noun = "issue" if len(entries) == 1 else "issues"
    lines = [f"Ready for next sprint{scope} ({len(entries)} {noun}):"]
    type_width = max((len(entry.type_label) for entry in entries), default=0)
    for entry in entries:
        lines.append(
            f"  #{entry.issue_number}  {entry.type_label.ljust(type_width)}  ready  {entry.title}"
        )
    return "\n".join(lines)
