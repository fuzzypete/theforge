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
from ..spike_guard import check_spike_closure
from ..task import (
    ALLOW_MUTATE_FORGE_YAML_KEY,
    RECOGNIZED_STORY_TYPES,
    StoryTypeError,
    TaskStory,
)
from ..task.story import FrontmatterParseResult, _parse_frontmatter_block
from .manifest import _build_task_from_story
from .reopen_context import analyze_reopen_contract, append_reopen_context
from .shape_gate import OPERATOR_ACTION_LABEL

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


def classify_already_done_disposition(state: object) -> str:
    """Classify why a no-merge ALREADY_DONE acceptance needs no code change.

    Returns one of:

    * ``"already_implemented"`` — the working tree already contains the change
      the spec asks for. Resolves to a ``completed`` close.
    * ``"premise_obsolete"`` — the spec's premise no longer exists in the
      codebase, so the requested change is not applicable. Resolves to a
      ``not planned`` close.
    * ``"ambiguous"`` — the two dispositions cannot be told apart from the
      structured preflight state. The decision is routed to the operator rather
      than guessed.

    The only mechanical signal available is the structured symptom-verification
    record preflight writes for bug-typed stories (``preflight_symptom_verification``).
    Everything else (feature/refactor stories, git-state cache hits) is
    genuinely ambiguous and must go to the operator.
    """
    symptom = getattr(state, "preflight_symptom_verification", None) or {}
    if not isinstance(symptom, dict):
        return "ambiguous"
    status = str(symptom.get("status") or "").strip().lower()
    reproduces_now = symptom.get("reproduces_now")
    # A verified-resolved symptom that no longer reproduces means the fix is
    # present in the code — already implemented.
    if status == "verified_resolved" and reproduces_now is False:
        return "already_implemented"
    # A symptom that was never reproducible against the current baseline means
    # the bug's premise is gone — premise obsolete.
    if status == "not_reproduced" and reproduces_now is False:
        return "premise_obsolete"
    return "ambiguous"


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
        """Resolve the tracking issue when a story completes successfully.

        Two terminal shapes reach here in merge mode:

        * a merged land — close the issue with the review summary (unchanged);
        * a no-merge ALREADY_DONE acceptance — the working tree already
          satisfied the spec, so no PR shipped. Post a durable comment stating
          the determination and its evidence, then either close with the
          resolved disposition or route the issue to the operator-action queue
          when the disposition is ambiguous. Without this branch the issue would
          be left silently open (issue #1937).
        """
        if config.workspace.on_approve != "merge":
            return
        if task.github_issue is None:
            return

        merged = result.merge is not None and result.merge.get("merged", False)
        if not merged:
            if getattr(result.state, "preflight_verdict", None) == "ALREADY_DONE":
                self._resolve_already_done_issue(task, result, config)
            return

        summary = ""
        if result.state.review_results:
            summary = result.state.review_results[-1].summary
        comment = f"Completed by TheForge. {summary}".strip()

        if not self._may_close_issue(task, config, closing_comment=comment):
            return

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

    def _resolve_already_done_issue(
        self,
        task: TaskStory,
        result: "CoordinatorResult",
        config: "ForgeConfig",
    ) -> None:
        """Post the ALREADY_DONE determination and resolve/route the issue.

        Always leaves a durable, operator-visible comment carrying the
        determination and its evidence. Then, depending on the disposition:

        * ``already_implemented`` — close with ``--reason completed``;
        * ``premise_obsolete`` — close with ``--reason "not planned"``;
        * ``ambiguous`` — leave the issue open, comment, and add the
          ``operator-action`` label so it surfaces in the operator-action queue.
        """
        number = task.github_issue
        assert number is not None  # guarded by caller
        project_root = config.project_root
        reason = (getattr(result.state, "preflight_reason", None) or "").strip()
        if not reason:
            reason = "(no reason recorded)"
        disposition = classify_already_done_disposition(result.state)

        header = (
            "TheForge accepted this issue without a code change: preflight determined "
            "the spec is already satisfied (verdict: ALREADY_DONE). No pull request was "
            "opened because the working tree already meets the spec."
        )
        evidence = f"Evidence: {reason}"

        if disposition == "already_implemented":
            body = (
                f"{header}\n\n{evidence}\n\n"
                "Disposition: already implemented — the change this issue asks for is "
                "already present in the codebase. Closing as completed."
            )
            if not self._may_close_issue(task, config, closing_comment=body):
                return
            self._run_issue_gh(
                ["issue", "close", str(number), "--comment", body, "--reason", "completed"],
                project_root,
                f"issue close #{number}",
            )
        elif disposition == "premise_obsolete":
            body = (
                f"{header}\n\n{evidence}\n\n"
                "Disposition: premise obsolete — the premise this issue depends on is "
                "no longer present in the codebase. Closing as not planned."
            )
            if not self._may_close_issue(task, config, closing_comment=body):
                return
            self._run_issue_gh(
                ["issue", "close", str(number), "--comment", body, "--reason", "not planned"],
                project_root,
                f"issue close #{number}",
            )
        else:
            body = (
                f"{header}\n\n{evidence}\n\n"
                "Disposition is ambiguous (already-implemented vs. premise-obsolete). "
                f"Routing to the operator-action queue via the '{OPERATOR_ACTION_LABEL}' "
                "label and leaving this issue open for an operator to resolve."
            )
            self._run_issue_gh(
                ["issue", "comment", str(number), "--body", body],
                project_root,
                f"issue comment #{number}",
            )
            self._run_issue_gh(
                ["issue", "edit", str(number), "--add-label", OPERATOR_ACTION_LABEL],
                project_root,
                f"issue edit #{number}",
            )

    def _may_close_issue(
        self,
        task: TaskStory,
        config: "ForgeConfig",
        *,
        closing_comment: str | None = None,
    ) -> bool:
        """Return whether the tracking issue may be closed, refusing spikes without an outcome.

        Every close this source performs goes through here (#2600). A spike
        that records neither a do-not-proceed decision nor a follow-on work
        item stays open, and the refusal is posted to the issue so the operator
        sees why rather than finding a story that landed against an open issue.

        A non-spike story never reaches ``gh``: ``task.type`` already answers
        the question, so an ordinary close keeps its previous cost and its
        previous failure modes.
        """
        number = task.github_issue
        assert number is not None  # guarded by every caller
        decision = check_spike_closure(
            number,
            config.project_root,
            known_type=task.type,
            closing_comment=closing_comment,
        )
        if decision.allowed:
            return True
        _log.warning("refusing to close issue #%s: %s", number, decision.reason)
        # The comment that would have accompanied the close carries the story's
        # result — an ALREADY_DONE determination and its evidence, or the review
        # summary. It stays durable whether or not the close is allowed, so a
        # refusal withholds the closure and nothing else.
        note = (
            "TheForge finished this spike's work but did **not** close the issue.\n\n"
            f"{decision.reason}"
        )
        body = f"{closing_comment}\n\n---\n\n{note}" if closing_comment else note
        self._run_issue_gh(
            ["issue", "comment", str(number), "--body", body],
            config.project_root,
            f"issue comment #{number}",
        )
        return False

    @staticmethod
    def _run_issue_gh(args: list[str], project_root: Path, failure_desc: str) -> bool:
        """Run a ``gh`` subcommand, logging (never raising) on failure.

        Returns True on success. Failures are logged with ``failure_desc`` so an
        operator can correlate a stuck issue with the failed resolution attempt.
        """
        try:
            proc = subprocess.run(
                ["gh", *args],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=30,
            )
        except Exception as exc:
            _log.warning("gh %s failed: %s", failure_desc, exc)
            return False
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            _log.warning("gh %s failed (exit %d): %s", failure_desc, proc.returncode, err)
            return False
        return True

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
