"""Story source abstraction: fetch specs from files or GitHub issues."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..shape_check.heuristics import (
    FIX_READY_STATUS_LABELS,
    RECOGNIZED_STATUS_LABELS,
    derive_fix_ready,
)
from ..task import (
    ALLOW_MUTATE_FORGE_YAML_KEY,
    RECOGNIZED_STORY_TYPES,
    StoryTypeError,
    TaskStory,
)
from ..task.story import FrontmatterParseResult, _parse_frontmatter_block
from .manifest import _build_task_from_story
from .reopen_context import analyze_reopen_contract, append_reopen_context

if TYPE_CHECKING:
    from ..config import ForgeConfig
    from ..coordinator.state import CoordinatorResult

_log = logging.getLogger(__name__)
_BLOCKED_BY_BODY_RE = re.compile(
    r"blocked by\s+(?:https?://github\.com/[^/\s]+/[^/\s]+/issues/)?#?(?P<number>\d+)",
    re.IGNORECASE,
)
# Matches "Depends on #N", "depends on: #N", "depends_on: #N", "depends_on: issue-N",
# "depends_on: [issue-N, issue-M]", full GitHub issue URLs, etc.
_DEPENDS_ON_BODY_RE = re.compile(
    r"depends[_ ]on:?\s*"
    r"(?:\[([^\]]*)\]|"  # bracketed list form: depends_on: [issue-1, issue-2]
    r"(?:https?://github\.com/[^/\s]+/[^/\s]+/issues/|issue-)?#?(\d+))",  # single ref
    re.IGNORECASE,
)
_ISSUE_REF_RE = re.compile(r"(?:https?://github\.com/[^/\s]+/[^/\s]+/issues/|issue-)?#?(\d+)")
_ISSUE_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_FENCED_CODE_BLOCK_RE = re.compile(r"(?ms)^[ \t]*```.*?$.*?^[ \t]*```[ \t]*(?:\r?\n|$)")
_BLOCKQUOTE_LINE_RE = re.compile(r"(?m)^[ \t]*>.*(?:\r?\n|$)")
_INLINE_CODE_SPAN_RE = re.compile(r"(?<!`)`([^`\n]|``)*`(?!`)")


def _strip_illustrative_markdown(text: str) -> str:
    """Remove markdown regions that should not count as declarations."""
    stripped = _FENCED_CODE_BLOCK_RE.sub("\n", text)
    stripped = _BLOCKQUOTE_LINE_RE.sub("\n", stripped)
    return _INLINE_CODE_SPAN_RE.sub("", stripped)


def _derive_type_from_labels(labels: list[str], issue_number: int) -> tuple[str | None, list[str]]:
    """Pick the structured story type from a list of GH label names.

    Returns ``(type, warnings)``. Multiple recognized type labels yield a
    StoryTypeError so the issue is rejected rather than silently picking one;
    zero matches return ``(None, warning)`` so downstream gates can flag it as
    a migration concern.
    """
    matches = [lbl for lbl in labels if lbl in RECOGNIZED_STORY_TYPES]
    if len(matches) > 1:
        raise StoryTypeError(
            f"issue #{issue_number} has multiple type labels {sorted(set(matches))!r} — "
            f"exactly one of {sorted(RECOGNIZED_STORY_TYPES)} is required"
        )
    if matches:
        return matches[0], []
    warning = (
        f"GH issue #{issue_number} has no recognized type label — expected one of: "
        f"{', '.join(sorted(RECOGNIZED_STORY_TYPES))}"
    )
    _log.warning(warning)
    return None, [warning]


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

    def _parse_issue_metadata(self, body: str, number: int) -> FrontmatterParseResult:
        """Return parsed leading YAML metadata from an issue body, if present."""
        return _parse_frontmatter_block(body, source_name=f"GH issue #{number} metadata")

    def _fetch_issue_timeline(self, number: int, project_root: Path) -> list[dict]:
        """Return raw GitHub timeline events for an issue."""
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
            return []

        if proc.returncode != 0:
            return []

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []

        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _fetch_issue_blockers(self, number: int, project_root: Path) -> list[int]:
        """Return issue numbers that block this issue.

        Best-effort: tries GitHub's timeline API first, then falls back to issue-body
        text patterns. Native relationship payloads have varied over time, so the
        parser accepts several candidate keys and event names.
        """
        blockers = self._fetch_issue_blockers_from_timeline(
            self._fetch_issue_timeline(number, project_root)
        )
        return sorted(blockers)

    def _fetch_issue_blockers_from_timeline(self, timeline: list[dict]) -> set[int]:
        """Return blocker issue numbers from the GitHub issue timeline."""
        blockers: set[int] = set()
        for item in timeline:
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

    def _parse_issue_blockers_from_body_metadata(
        self, body: str, metadata: dict | None = None
    ) -> list[int]:
        """Return blocker issue numbers from explicit issue-body YAML metadata.

        GitHub issues may declare scheduler dependencies in leading YAML
        frontmatter:

            ---
            depends_on:
              - issue-123
            ---

        Free-form prose is handled by ``_find_prose_dependency_phrases`` and
        merged into the blockers set by ``fetch`` when no timeline edges exist.
        """
        metadata = metadata if metadata is not None else self._parse_issue_metadata(body, 0).data
        if not metadata:
            return []

        raw_deps = metadata.get("depends_on", [])
        if raw_deps is None:
            return []
        if isinstance(raw_deps, (str, int)):
            dep_values = [raw_deps]
        elif isinstance(raw_deps, list):
            dep_values = raw_deps
        else:
            return []

        blockers: set[int] = set()
        for raw_dep in dep_values:
            match = _ISSUE_REF_RE.fullmatch(str(raw_dep).strip())
            if match is not None:
                blockers.add(int(match.group(1)))
        return sorted(blockers)

    def _body_without_issue_metadata(self, body: str) -> str:
        """Return issue body with the structured metadata block removed."""
        if self._parse_issue_metadata(body, number=0).warning is not None:
            return body
        return _ISSUE_FRONTMATTER_RE.sub("", body, count=1)

    def _body_without_illustrative_markdown(self, body: str) -> str:
        """Return issue body without non-declarative markdown examples."""
        return _strip_illustrative_markdown(self._body_without_issue_metadata(body))

    def _find_prose_dependency_phrases(self, body: str) -> list[tuple[str, list[int]]]:
        """Return dependency-shaped prose phrases and the issue refs they declare.

        Refs returned here are honored as scheduler blockers when no native
        ``blocked_by`` timeline relationship is present; see ``fetch``.
        """
        scan_body = self._body_without_illustrative_markdown(body)
        matches: list[tuple[str, list[int]]] = []
        for match in _BLOCKED_BY_BODY_RE.finditer(scan_body):
            matches.append((match.group(0), [int(match.group("number"))]))
        for match in _DEPENDS_ON_BODY_RE.finditer(scan_body):
            bracket_content, single_number = match.group(1), match.group(2)
            if bracket_content is not None:
                refs = [
                    int(num_match.group(1))
                    for num_match in _ISSUE_REF_RE.finditer(bracket_content)
                ]
            elif single_number is not None:
                refs = [int(single_number)]
            else:
                refs = []
            if refs:
                matches.append((match.group(0), sorted(set(refs))))
        return matches

    def _dependency_authoring_warnings(
        self, body: str, structured_blockers: list[int]
    ) -> list[str]:
        """Return prose phrases whose refs are not honored as scheduler edges.

        When a prose ref appears in ``structured_blockers`` (because either the
        timeline, frontmatter, or prose itself promoted it to a blocker), the
        phrase is silent. Phrases left over here typically indicate prose that
        could not be promoted (e.g. a timeline-only blocker set that excludes a
        prose-mentioned issue).
        """
        declared = set(structured_blockers)
        warnings: list[str] = []
        for phrase, refs in self._find_prose_dependency_phrases(body):
            if refs and set(refs).issubset(declared):
                continue
            warnings.append(phrase)
        return warnings

    def fetch(self, ref: str, project_root: Path) -> TaskStory:
        """Fetch issue body via `gh issue view` and build a TaskStory.

        ref is the issue number as a string.
        """
        number = int(ref)
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(number),
                    "--json",
                    "title,body,state,labels,closedAt,stateReason,updatedAt,comments",
                ],
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
        metadata_result = self._parse_issue_metadata(body, number)
        metadata = metadata_result.data
        timeline = self._fetch_issue_timeline(number, project_root)
        reopen_state = analyze_reopen_contract(data, timeline)
        blockers = sorted(self._fetch_issue_blockers_from_timeline(timeline))
        if not blockers:
            body_blockers: set[int] = set(
                self._parse_issue_blockers_from_body_metadata(body, metadata)
            )
            for _phrase, refs in self._find_prose_dependency_phrases(body):
                body_blockers.update(refs)
            blockers = sorted(body_blockers)
        blocker_slugs = [f"issue-{blocker}" for blocker in blockers]
        dependency_warnings = self._dependency_authoring_warnings(body, blockers)
        if metadata_result.warning is not None:
            dependency_warnings = [metadata_result.warning, *dependency_warnings]

        label_names = [
            lbl.get("name", "").strip().lower()
            for lbl in (data.get("labels") or [])
            if isinstance(lbl, dict) and lbl.get("name")
        ]
        story_type, type_warnings = _derive_type_from_labels(label_names, number)
        fix_ready, investigation_ready, readiness_warnings = derive_fix_ready(story_type, body)
        status_labels = sorted(set(label_names) & RECOGNIZED_STATUS_LABELS)
        # Surface label/body disagreement for operator awareness — body is authoritative.
        if (
            story_type == "bug"
            and fix_ready is False
            and any(lbl in FIX_READY_STATUS_LABELS for lbl in label_names)
        ):
            readiness_warnings = [
                *readiness_warnings,
                "label claims fix-ready but body lacks a complete Diagnosis section",
            ]
        if (
            story_type == "bug"
            and fix_ready is True
            and status_labels
            and not (set(status_labels) & FIX_READY_STATUS_LABELS)
        ):
            readiness_warnings = [
                *readiness_warnings,
                f"body has complete Diagnosis but label is {status_labels[0]!r}",
            ]

        slug = f"issue-{number}"
        return TaskStory(
            name=title,
            story_path=None,
            slug=slug,
            story_text=append_reopen_context(body, reopen_state),
            depends_on=blocker_slugs,
            inferred_dependencies=blocker_slugs,
            dependency_warnings=dependency_warnings,
            github_issue=number,
            allow_mutate_forge_yaml=metadata.get(ALLOW_MUTATE_FORGE_YAML_KEY) is True,
            type=story_type,
            type_warnings=type_warnings,
            fix_ready=fix_ready,
            investigation_ready=investigation_ready,
            readiness_warnings=readiness_warnings,
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
