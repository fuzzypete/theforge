"""Story source abstraction: fetch specs from files or GitHub issues."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..task import TaskStory
from .manifest import _build_task_from_story

if TYPE_CHECKING:
    from ..config import ForgeConfig
    from ..coordinator.state import CoordinatorResult

_log = logging.getLogger(__name__)
_BLOCKED_BY_BODY_RE = re.compile(
    r"blocked by\s+(?:https?://github\.com/[^/\s]+/[^/\s]+/issues/)?#?(?P<number>\d+)",
    re.IGNORECASE,
)


class IssueClosedError(RuntimeError):
    """Raised by ``GitHubIssueSource.fetch()`` when the issue is already closed.

    Distinct from generic ``RuntimeError`` so callers can selectively skip
    closed issues while still propagating transient auth/network failures.
    """


@runtime_checkable
class StorySource(Protocol):
    """Protocol for fetching story specs and handling lifecycle callbacks."""

    def fetch(self, ref: str, project_root: Path) -> TaskStory:
        """Fetch a TaskStory from the given reference."""
        ...

    def on_complete(
        self,
        task: TaskStory,
        result: "CoordinatorResult",
        config: "ForgeConfig",
    ) -> None:
        """Called when a story completes successfully."""
        ...

    def on_escalate(
        self,
        task: TaskStory,
        state: object,
        config: "ForgeConfig",
    ) -> None:
        """Called when a story escalates."""
        ...


class FileSource:
    """Loads story specs from local markdown files."""

    def fetch(self, ref: str, project_root: Path) -> TaskStory:
        full_path = (project_root / ref).resolve()
        return _build_task_from_story(full_path)

    def on_complete(
        self,
        task: TaskStory,
        result: "CoordinatorResult",
        config: "ForgeConfig",
    ) -> None:
        pass  # no-op for file-based stories

    def on_escalate(
        self,
        task: TaskStory,
        state: object,
        config: "ForgeConfig",
    ) -> None:
        pass  # no-op for file-based stories


class GitHubIssueSource:
    """Loads story specs from GitHub issues via the gh CLI."""

    def _fetch_issue_blockers(self, number: int, project_root: Path) -> list[int]:
        """Return issue numbers that block this issue.

        Best-effort: tries GitHub's timeline API first, then falls back to issue-body
        text patterns. Native relationship payloads have varied over time, so the
        parser accepts several candidate keys and event names.
        """
        blockers = self._fetch_issue_blockers_from_timeline(number, project_root)
        return sorted(blockers)

    def _fetch_issue_blockers_from_timeline(self, number: int, project_root: Path) -> set[int]:
        """Return blocker issue numbers from the GitHub issue timeline."""
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"repos/{{owner}}/{{repo}}/issues/{number}/timeline?per_page=100",
                ],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            return set()

        if proc.returncode != 0:
            return set()

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return set()

        if not isinstance(data, list):
            return set()

        blockers: set[int] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            event = str(item.get("event", "")).lower()
            if "blocked" not in event or "by" not in event or "unblock" in event:
                continue
            for key in ("blocking_issue", "source", "subject", "issue", "blocker"):
                candidate = item.get(key)
                if isinstance(candidate, dict):
                    blocker_number = candidate.get("number")
                    if isinstance(blocker_number, int):
                        blockers.add(blocker_number)
        return blockers

    def _parse_issue_blockers_from_body(self, body: str) -> list[int]:
        """Return blocker issue numbers referenced in body text."""
        blockers = {int(match.group("number")) for match in _BLOCKED_BY_BODY_RE.finditer(body)}
        return sorted(blockers)

    def fetch(self, ref: str, project_root: Path) -> TaskStory:
        """Fetch issue body via `gh issue view` and build a TaskStory.

        ref is the issue number as a string.
        """
        number = int(ref)
        try:
            proc = subprocess.run(
                ["gh", "issue", "view", str(number), "--json", "title,body,state"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"Failed to fetch GitHub issue #{number}: {exc}") from exc

        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"gh issue view #{number} failed: {err}")

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh issue view #{number} returned malformed JSON: {exc}") from exc

        state = data.get("state", "OPEN")
        if state.upper() != "OPEN":
            raise IssueClosedError(f"issue #{number} is already {state.lower()}")

        title = data.get("title", f"Issue #{number}")
        body = data.get("body", "")
        blockers = self._fetch_issue_blockers(number, project_root)
        if not blockers:
            blockers = self._parse_issue_blockers_from_body(body)
        blocker_slugs = [f"issue-{blocker}" for blocker in blockers]

        slug = f"issue-{number}"
        return TaskStory(
            name=title,
            story_path=None,
            slug=slug,
            story_text=body,
            depends_on=blocker_slugs,
            inferred_dependencies=blocker_slugs,
            github_issue=number,
        )

    def on_complete(
        self,
        task: TaskStory,
        result: "CoordinatorResult",
        config: "ForgeConfig",
    ) -> None:
        """Close the issue when on_approve is 'merge' and the merge succeeded."""
        if config.workspace.on_approve != "merge":
            return
        # Only close if actually merged
        if result.merge is None or not result.merge.get("merged", False):
            return
        if task.github_issue is None:
            return

        summary = ""
        if result.state.review_results:
            summary = result.state.review_results[-1].summary
        comment = f"Completed by TheForge. {summary}".strip()

        try:
            proc = subprocess.run(
                [
                    "gh",
                    "issue",
                    "close",
                    str(task.github_issue),
                    "--comment",
                    comment,
                ],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=30,
            )
        except Exception as exc:
            _log.warning("gh issue close #%s failed: %s", task.github_issue, exc)
            return
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            _log.warning(
                "gh issue close #%s failed (exit %d): %s", task.github_issue, proc.returncode, err
            )

    def on_escalate(
        self,
        task: TaskStory,
        state: object,
        config: "ForgeConfig",
    ) -> None:
        """Post a comment on the issue when the story escalates."""
        if task.github_issue is None:
            return

        error = getattr(state, "error", None) or "Story escalated"
        comment = f"TheForge escalated this story: {error}"

        try:
            proc = subprocess.run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(task.github_issue),
                    "--body",
                    comment,
                ],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=30,
            )
        except Exception as exc:
            _log.warning("gh issue comment #%s failed: %s", task.github_issue, exc)
            return
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            _log.warning(
                "gh issue comment #%s failed (exit %d): %s",
                task.github_issue,
                proc.returncode,
                err,
            )


def resolve(
    entry: str | dict,
    project_root: Path,
) -> tuple[StorySource, str, str]:
    """Resolve a manifest entry to (source, ref, canonical_ref).

    - String entry -> FileSource
    - Dict with "issue" key -> GitHubIssueSource
    """
    if isinstance(entry, str):
        return FileSource(), entry, entry
    if isinstance(entry, dict) and "issue" in entry:
        number = str(entry["issue"])
        return GitHubIssueSource(), number, f"issue:{number}"
    raise ValueError(f"Unsupported manifest entry: {entry!r}")


def canonicalize_ref(entry: str | dict) -> str:
    """Return a canonical string reference for a manifest entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and "issue" in entry:
        return f"issue:{entry['issue']}"
    raise ValueError(f"Unsupported manifest entry: {entry!r}")
