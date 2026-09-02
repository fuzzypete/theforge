"""Sprint DAG scheduler and story triage logic."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import ForgeConfig
from ..coordinator.audit import has_review_approve, latest_run_outcome
from ..coordinator.gate import _run_gate
from ..coordinator.state import EntryGateOutcome, GateLabel, GateRunFacts
from ..log_util import _log_line
from ..task import TaskStory
from .manifest import _build_task_from_story

#: Story phase written to live sprint state while a story's reuse gate runs.
#: Resume triage runs one gate per story before any story is dispatched; without
#: a phase of its own that window reads as ``waiting`` for its whole (possibly
#: many-minute) duration (#2014).
REUSE_GATE_PHASE = "REUSE_GATE"

#: ``GateLabel.purpose`` for the per-story resume-triage gate.
REUSE_GATE_PURPOSE = "reuse gate"


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def _issue_number_from_slug(slug: str) -> int | None:
    match = re.fullmatch(r"issue-(\d+)", slug)
    if match is None:
        return None
    return int(match.group(1))


def _issue_number_from_ref(ref: str) -> int | None:
    match = re.search(r"(?:^|[/-])issue-(\d+)(?:$|[^0-9])", ref)
    if match is None:
        return None
    return int(match.group(1))


@dataclass
class StoryTriage:
    """Result of triaging a story for sprint resume."""

    story_path: str
    action: str  # "skip_merged", "skip", "review", "dev", "full"
    reason: str
    worktree_path: Path | None = None
    slug: str = ""
    # Structured outcome of the reuse gate when it is what routed this story to
    # DEV, for the phase that has to act on it. ``reason`` above stays the
    # operator-facing record for the sprint log; nothing downstream parses it
    # (#2796). None for every action the reuse gate did not decide, and for a
    # gate that failed outright rather than running out of time.
    gate_outcome: EntryGateOutcome | None = None


@dataclass(frozen=True)
class MergeEvidence:
    """Structured merge detection result for resume triage."""

    merged: bool
    source: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None


def _has_prior_review_approve(
    project_root: Path,
    slug: str,
    base_branch: str,
    branch: str,
) -> bool:
    """Return True when audit history shows a landed APPROVE for this story.

    Resume merged detection needs the persisted review outcome even when the
    feature branch still appears ahead of base (squash merges rewrite commits,
    so git topology alone cannot prove the merge). This helper intentionally
    bypasses the stale-branch guard inside has_review_approve, but it still
    requires landing evidence so a zero-delta APPROVE or failed merge attempt
    does not satisfy the story during resume.
    """
    return has_review_approve(
        project_root,
        slug,
        base_branch,
        branch,
        allow_unmerged_commits=True,
        require_landed=True,
    )


#: A git object id: 40 hex chars under sha1, 64 under sha256. Used to tell a
#: tree oid on ``merge-tree``'s first output line from an error message, since
#: both can accompany exit status 1.
_OID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

#: Record separator used to split ``git log`` output into whole commit messages.
#: ``\x1e`` cannot appear in a commit message produced by git's own tooling.
_COMMIT_RECORD_SEP = "\x1e"

#: Most base-branch commits whose message mentions the issue that are read back
#: and checked for a closing reference.
_MAX_COMMIT_SCAN = 200

#: GitHub's closing keywords. Only these turn an issue reference into a claim
#: that the issue was completed by the commit; every other mention is a
#: cross-reference, which is what cross-references are for.
_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)

#: The sigils GitHub accepts immediately before an issue number. Both the git
#: prefilter and the authoritative Python matcher are built from this tuple, so
#: a spelling can never be advertised by one and silently dropped by the other
#: — the drift that made ``Closes GH-N`` unreachable at runtime. Neither entry
#: may contain a regex metacharacter, since both are interpolated into an ERE
#: (for ``git log --grep``) and a Python pattern unescaped.
_REFERENCE_SIGILS = ("#", "GH-")


def _reference_alternation() -> str:
    """Return the ``#|GH-`` alternation shared by the prefilter and matcher."""
    return "|".join(_REFERENCE_SIGILS)


def _closing_reference_pattern(issue_number: int) -> re.Pattern[str]:
    """Return a matcher for an explicit closing reference to ``issue_number``.

    Matches GitHub's own closing syntax — ``fixes #12``, ``Closes GH-12``,
    ``resolved owner/repo#12`` — and nothing else. The trailing boundary keeps
    ``#12`` from matching inside ``#123``.
    """
    keywords = "|".join(_CLOSING_KEYWORDS)
    return re.compile(
        rf"\b(?:{keywords})\b\s*:?\s*"
        rf"(?:[\w.-]+/[\w.-]+)?(?:{_reference_alternation()})"
        rf"{issue_number}(?![0-9])",
        re.IGNORECASE,
    )


def _reference_grep_pattern(issue_number: int) -> str:
    """Return the ``git log --grep`` ERE that retrieves every supported spelling.

    This is only a prefilter — it narrows the commits git hands back, and
    :func:`_closing_reference_pattern` decides. It must therefore be at least as
    permissive as the matcher for every sigil in :data:`_REFERENCE_SIGILS`;
    anything it drops the matcher never sees (#2374).
    """
    return f"({_reference_alternation()}){issue_number}"


def _has_base_commit_closing_issue(
    project_root: Path,
    base_branch: str,
    issue_number: int,
) -> bool:
    """Return True when a base commit message *asserts* the issue was closed.

    GitHub squash commits commonly preserve the closing reference from the PR
    body in the final base-branch commit even though the branch tip is not
    topologically merged. This git-level check catches externally merged
    branches that never produced a forge APPROVE audit record.

    A bare mention (``disabled model X, see #12``) is deliberately *not*
    evidence: a reference to a unit of work is not a statement that the work
    landed, and treating it as one skipped open stories with preserved work
    on the strength of unrelated configuration commits (#2374).
    """
    pattern = _closing_reference_pattern(issue_number)
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--format=%B{_COMMIT_RECORD_SEP}",
                # Extended regex + case-insensitive so the prefilter covers
                # every sigil the matcher accepts (``#N`` and ``GH-N``, in any
                # case). The issue number is an int, so nothing user-controlled
                # reaches the pattern.
                "--extended-regexp",
                "--regexp-ignore-case",
                f"--grep={_reference_grep_pattern(issue_number)}",
                # Bound the scan: the prefilter is a substring match, so a
                # low-numbered issue can match a great many commits. Missing a
                # closing reference older than this window costs a re-run of a
                # landed story; the opposite error discards live work.
                f"--max-count={_MAX_COMMIT_SCAN}",
                base_branch,
            ],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False
        messages = result.stdout.decode("utf-8", errors="replace").split(_COMMIT_RECORD_SEP)
        return any(pattern.search(message) for message in messages)
    except (subprocess.TimeoutExpired, OSError):
        return False


def _branch_adds_content_to_base(
    project_root: Path,
    base_branch: str,
    branch: str,
) -> bool:
    """Return True when ``branch`` provably contributes content base does not have.

    Git *topology* cannot tell a squash-merged branch from an unmerged one: both
    leave the branch a non-ancestor of base with unique commits of its own. That
    is the whole reason the issue-commit source exists, so topology must never
    veto it — an earlier revision of this fix vetoed on non-ancestor-plus-unique-
    commits and thereby suppressed every genuine squash merge whose branch still
    existed (#2374).

    Content does distinguish them. Merging ``branch`` into ``base_branch`` is a
    no-op exactly when the branch's work is already present in base, whether it
    got there by squash, rebase, or cherry-pick, and that holds even after base
    advances with unrelated commits. ``git merge-tree --write-tree`` computes
    that merge without touching the worktree.

    Only a *positive* proof vetoes, and a conflict is one. ``merge-tree`` exits
    0 for a clean merge and 1 for a conflicted one; a conflict means base and
    branch changed the same lines differently, so the branch's work as written
    is demonstrably not what base carries. That is content evidence, not an
    inconclusive result, and it vetoes (#2374).

    Everything genuinely inconclusive returns False and leaves the closing
    reference standing: a git too old for ``--write-tree`` (exit 129), an
    unreadable ref, an unparseable oid, a timeout. A missing branch lands here
    too, and needs care: ``merge-tree`` rejects an unknown ref with exit 1 —
    the *same* status as a conflict — writing "not something we can merge" to
    stderr and nothing to stdout. So the exit code alone cannot separate the
    two; a merge that actually ran is identified by the tree oid on its first
    stdout line. That distinction matters, because an externally merged branch
    that was then deleted has no evidence left *but* the closing reference.

    One accepted false negative: a branch squash-merged long ago whose files
    base has since rewritten conflicts on replay and is vetoed. That re-runs a
    landed story, which is the safe direction — the failure this whole change
    exists to prevent is discarding live work.
    """
    try:
        merged = subprocess.run(
            ["git", "merge-tree", "--write-tree", base_branch, branch],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        # 0 = clean merge, 1 = conflict, anything else = could not run.
        if merged.returncode not in (0, 1):
            return False
        merged_tree = merged.stdout.decode("utf-8", errors="replace").strip().splitlines()
        if not merged_tree or not _OID_RE.fullmatch(merged_tree[0].strip()):
            # No tree oid on the first line: git refused the merge rather than
            # performing one, so nothing was proved either way. Today an
            # unknown ref yields empty stdout; the oid check also covers a
            # refusal that writes some other diagnostic there.
            return False
        if merged.returncode == 1:
            return True
        base_tree = subprocess.run(
            ["git", "rev-parse", f"{base_branch}^{{tree}}"],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if base_tree.returncode != 0:
            return False
        base_oid = base_tree.stdout.decode("utf-8", errors="replace").strip()
        if not base_oid:
            return False
        return merged_tree[0].strip() != base_oid
    except (subprocess.TimeoutExpired, OSError):
        return False


def _audit_contradicts_merge(project_root: Path, slug: str) -> bool:
    """Return True when the last recorded run for ``slug`` says it did not land.

    A recorded outcome of unsuccessful — or a final review verdict of
    REQUEST_CHANGES — with no landing status is the run's own account of having
    finished without landing. It is stronger evidence than prose in someone
    else's commit message, so it vetoes the textual fallback (#2374).

    Any audit-read failure returns False: an unreadable audit has no opinion and
    must not silently invert into a veto.
    """
    try:
        record = latest_run_outcome(project_root, slug)
    except Exception:
        return False
    if record is None:
        return False
    landing = str(record.get("landing_status") or "").strip().lower()
    if landing == "landed":
        return False
    verdict = str(record.get("verdict") or "").strip().upper()
    outcome = record.get("outcome_success")
    return outcome == 0 or verdict == "REQUEST_CHANGES"


def _lookup_merged_pr_for_branch(
    branch: str,
    project_root: Path,
) -> MergeEvidence | None:
    """Return merged PR metadata for ``branch`` when GitHub reports one."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "closed",
                "--json",
                "number,url,mergedAt",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "[]")
        if not isinstance(data, list):
            return None
        merged_prs: list[tuple[str, int, str | None]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            merged_at = item.get("mergedAt")
            number = item.get("number")
            url = item.get("url")
            if not isinstance(merged_at, str) or not merged_at:
                continue
            if not isinstance(number, int):
                continue
            merged_prs.append((merged_at, number, url if isinstance(url, str) else None))
        if not merged_prs:
            return None
        _merged_at, pr_number, pr_url = max(merged_prs, key=lambda item: item[0])
        return MergeEvidence(
            merged=True,
            source="github_pr",
            pr_number=pr_number,
            pr_url=pr_url,
        )
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None


def _with_pr_metadata(
    evidence: MergeEvidence,
    branch: str,
    project_root: Path,
    issue_number: int | None,
) -> MergeEvidence:
    """Attach merged-PR metadata when GitHub can identify the landing PR."""
    if not evidence.merged or issue_number is None or evidence.pr_number is not None:
        return evidence
    merged_pr = _lookup_merged_pr_for_branch(branch, project_root)
    if merged_pr is None:
        return evidence
    return MergeEvidence(
        merged=True,
        source=evidence.source,
        pr_number=merged_pr.pr_number,
        pr_url=merged_pr.pr_url,
    )


def _branch_merge_evidence(
    branch: str,
    base_branch: str,
    project_root: Path,
    slug: str | None = None,
) -> MergeEvidence:
    """Return structured merge evidence for ``branch`` against ``base_branch``.

    Evidence is consulted strongest-first, and issue state is deliberately not a
    precondition for any of it (#2111). Whether a referencing GitHub issue is
    closed is a policy another system owns and is free to redefine — symptom bugs
    are now held open pending verification after their fix lands — so gating merge
    detection on it silently disabled detection for a whole class of story and
    re-ran work already in the base branch. Issue closure survives only as a
    corroborating signal for *external* dependencies in
    :func:`resolve_satisfied_dependencies`.

    Order of precedence:

    1. The forge audit trail — the evidence this run recorded when it landed the
       branch. Owned evidence is consulted before any external signal.
    2. Git topology — a regular merge, provable locally.
    3. GitHub's own record of a merged PR for the branch.
    4. A base commit whose message *closes* the issue. This is the weakest
       signal — prose about the code rather than the code — so it runs last,
       only once every stronger source has declined, and only when neither git
       topology nor the audit trail contradicts it (#2374).
    """
    no_merge = MergeEvidence(merged=False)
    issue_number = _issue_number_from_slug(slug) if slug is not None else None
    if issue_number is None:
        issue_number = _issue_number_from_ref(branch)

    # 1. Owned evidence: the APPROVE + landed record this run wrote itself.
    if slug is not None:
        try:
            if _has_prior_review_approve(project_root, slug, base_branch, branch):
                return _with_pr_metadata(
                    MergeEvidence(merged=True, source="audit"),
                    branch,
                    project_root,
                    issue_number,
                )
        except Exception:
            # A transient audit-read failure must not discard the remaining
            # evidence sources below — fall through rather than claim no_merge.
            pass

    # 2. Git topology.
    try:
        merge_result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_branch],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if merge_result.returncode == 0:
            # Count commits in base_branch NOT reachable from branch.
            ahead_result = subprocess.run(
                ["git", "rev-list", f"{branch}..{base_branch}", "--count"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            ahead_count = int(ahead_result.stdout.decode("utf-8", errors="replace").strip() or "0")
            if ahead_count > 0:
                # Distinguish a real regular merge from an abandoned branch whose
                # tip is merely behind base_branch. A merged branch must also have
                # unique commits that are now reachable from base_branch.
                unique_result = subprocess.run(
                    ["git", "rev-list", f"{base_branch}..{branch}", "--count"],
                    cwd=str(project_root),
                    capture_output=True,
                    timeout=30,
                )
                unique_count = int(
                    unique_result.stdout.decode("utf-8", errors="replace").strip() or "0"
                )
                if unique_count > 0:
                    # Regular merge: base has moved past branch and branch had
                    # unique work of its own.
                    return _with_pr_metadata(
                        MergeEvidence(merged=True, source="topology"),
                        branch,
                        project_root,
                        issue_number,
                    )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass

    # 3. GitHub's record of the merge. Fast-forward merges at the same tip and
    # squash merges are both invisible to topology, so they need this and the
    # issue-commit fallback below.
    merged_pr = (
        _lookup_merged_pr_for_branch(branch, project_root) if issue_number is not None else None
    )
    if merged_pr is not None:
        return merged_pr

    # 4. Weakest signal last: a base commit message that closes the issue. The
    #    merged-PR lookup above already declined, so there is no PR metadata to
    #    attach here.
    #
    #    Sources that describe the code beat sources that describe prose about
    #    the code: a branch whose work is provably absent from base has not
    #    landed whatever a message says, and a last run recorded as unsuccessful
    #    with nothing landed is not overridden by a textual match (#2374).
    #
    #    The absence test is deliberately about *content*, not topology. A
    #    squash-merged branch and an unmerged one are topologically identical
    #    (non-ancestor, unique commits), so vetoing on that shape suppressed
    #    exactly the squash merges this source exists to detect.
    if issue_number is None:
        return no_merge
    if _branch_adds_content_to_base(project_root, base_branch, branch):
        return no_merge
    if slug is not None and _audit_contradicts_merge(project_root, slug):
        return no_merge
    if _has_base_commit_closing_issue(project_root, base_branch, issue_number):
        return MergeEvidence(merged=True, source="issue_commit")

    return no_merge


def _is_branch_merged(
    branch: str,
    base_branch: str,
    project_root: Path,
    slug: str | None = None,
) -> bool:
    """Return True if branch has been merged into base_branch.

    Detection must cover the merge strategies theforge uses:

    1. Regular merge commit (git merge --no-edit fallback):
       --is-ancestor passes AND branch..base_branch count > 0 (base advanced
       past the branch tip via a merge commit) AND base_branch..branch count > 0
       (the branch had unique commits before merge).

    2. Fast-forward merge (git merge --ff-only, preferred):
       After an FF merge, branch and base point at the same commit, so
       branch..base_branch count == 0.

    3. Squash merge (configured default):
       The feature branch tip remains an ancestor of base because it was based
       on base, but the squash commit on base is a new commit with no parent
       relationship to the branch. Git topology alone therefore cannot prove
       the merge, so the forge APPROVE audit trail (when slug is provided), a
       merged PR for the branch, and finally a base commit that *closes* the
       issue stand in for it.

    See :func:`_branch_merge_evidence` for the order these are consulted in and
    why GitHub issue state is not part of it.

    A branch that was merely created at base HEAD (count == 0, no audit entry)
    correctly returns False.
    """
    return _branch_merge_evidence(branch, base_branch, project_root, slug=slug).merged


def _is_issue_closed(issue_number: int, project_root: Path) -> bool:
    """Return True when GitHub issue ``issue_number`` is not OPEN.

    GitHub is the source of truth for issue-backed external dependencies. Any
    CLI, parsing, or schema failure returns False so missing state does not
    spuriously unblock dependent sprint work.
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "state"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        state = data.get("state")
        if not isinstance(state, str):
            return False
        return state.upper() != "OPEN"
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return False


class StoryDAG:
    """Dependency-aware scheduler for concurrent story execution.

    Tracks two distinct states per story:
    - _completed: stories that satisfied their dependencies (merged or ALREADY_DONE).
      Used to unlock downstream stories in ready().
    - _finished: all stories that are done processing (any outcome).
      Used by is_done() to detect completion.

    mark_complete() adds to both sets (story ran and satisfied deps).
    mark_skipped() adds only to _finished (story is done but deps not satisfied).
    """

    def __init__(self, tasks: list[TaskStory], satisfied: set[str] | None = None) -> None:
        self._tasks: dict[str, TaskStory] = {t.slug: t for t in tasks}
        self._hard_deps: dict[str, set[str]] = {t.slug: set(t.depends_on) for t in tasks}
        self._soft_deps: dict[str, set[str]] = {t.slug: set(t.collision_deps) for t in tasks}
        self._completed: set[str] = (
            set(satisfied) if satisfied else set()
        )  # satisfied: merged or ALREADY_DONE
        self._finished: set[str] = set()  # all done: any outcome

    def ready(self) -> list[TaskStory]:
        """Return tasks that are not finished and whose deps are all released.

        Hard deps (``depends_on``) must be in ``_completed``. Soft deps
        (``collision_deps``) release on any terminal state — completed OR
        finished — because the upstream reaching a terminal-but-not-merged
        state means the collision the synthetic edge guarded against does
        not exist on disk, so the downstream can proceed.
        """
        return [
            task
            for slug, task in self._tasks.items()
            if slug not in self._finished
            and all(dep in self._completed for dep in self._hard_deps[slug])
            and all(
                dep in self._completed or dep in self._finished for dep in self._soft_deps[slug]
            )
        ]

    def mark_complete(self, slug: str) -> None:
        """Story satisfied deps (merged / ALREADY_DONE). Unlocks dependents."""
        self._completed.add(slug)
        if slug in self._tasks:
            self._finished.add(slug)

    def mark_skipped(self, slug: str) -> None:
        """Story finished without satisfying deps (failed / budget / blocked)."""
        self._finished.add(slug)

    def is_done(self) -> bool:
        """True when every task has been finished (any outcome)."""
        return len(self._finished) == len(self._tasks)

    def unmet_deps(self, slug: str) -> list[str]:
        """Dependency slugs that genuinely block this story (for skip messages).

        Hard deps are unmet when not yet completed. Soft (collision) deps are
        unmet only when their upstream has not reached any terminal state —
        a terminal-but-not-merged upstream releases the soft edge.
        """
        unmet = [dep for dep in self._hard_deps[slug] if dep not in self._completed]
        unmet.extend(
            dep
            for dep in self._soft_deps[slug]
            if dep not in self._completed and dep not in self._finished
        )
        return unmet

    def remaining(self) -> list[TaskStory]:
        """Tasks not yet finished (not in _finished)."""
        return [task for slug, task in self._tasks.items() if slug not in self._finished]


def build_dag(tasks: list[TaskStory], satisfied: set[str] | None = None) -> StoryDAG:
    """Build a StoryDAG from a list of TaskStory objects.

    Raises ValueError if:
    - any depends_on slug references a story not present in the manifest and not in satisfied
    - circular dependencies are detected

    Args:
        tasks: Stories in this sprint manifest.
        satisfied: Slugs already merged to main (from prior sprints). Dependencies
            on these slugs are treated as met without requiring them in the manifest.
    """
    known_slugs = {t.slug for t in tasks}
    _satisfied = satisfied or set()

    # Validate all depends_on slugs exist in the manifest or are pre-satisfied
    for task in tasks:
        missing = [
            dep
            for dep in task.depends_on
            if dep not in known_slugs
            and dep not in _satisfied
            and _issue_number_from_slug(dep) is None
        ]
        if missing:
            raise ValueError(
                f"Story '{task.slug}' depends on unknown slug(s): {', '.join(missing)}. "
                f"All depends_on slugs must reference stories in the sprint manifest."
            )

    # Detect circular dependencies via DFS (gray/white/black coloring).
    # Exclude satisfied slugs — they are external and not in the DFS graph.
    deps = {
        t.slug: [d for d in t.depends_on if d not in _satisfied and d in known_slugs]
        for t in tasks
    }
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _dfs(slug: str) -> None:
        visited.add(slug)
        in_stack.add(slug)
        for dep in deps[slug]:
            if dep in in_stack:
                raise ValueError(
                    f"Circular dependency detected: '{slug}' → '{dep}'. "
                    f"Sprint manifest contains a dependency cycle."
                )
            if dep not in visited:
                _dfs(dep)
        in_stack.discard(slug)

    for task in tasks:
        if task.slug not in visited:
            _dfs(task.slug)

    return StoryDAG(tasks, satisfied=_satisfied)


def resolve_satisfied_dependencies(
    tasks: list[TaskStory],
    *,
    project_root: Path,
    base_branch: str,
    branch_pattern: str,
    pre_satisfied: set[str] | None = None,
) -> set[str]:
    """Return external dependency slugs already satisfied outside this manifest.

    Only dependencies absent from ``tasks`` are candidates here. They count as
    satisfied when explicitly pre-marked (resume-mode skip state) or when their
    corresponding branch is already merged into the base branch.
    """
    manifest_slugs = {task.slug for task in tasks}
    dependent_slugs = {dep for task in tasks for dep in task.depends_on}
    satisfied_slugs = set(pre_satisfied or set())

    for dep_slug in dependent_slugs - manifest_slugs:
        if dep_slug in satisfied_slugs:
            continue
        branch = branch_pattern.format(slug=dep_slug)
        if _is_branch_merged(branch, base_branch, project_root, slug=dep_slug):
            satisfied_slugs.add(dep_slug)
            continue
        issue_number = _issue_number_from_slug(dep_slug)
        if issue_number is not None and _is_issue_closed(issue_number, project_root):
            satisfied_slugs.add(dep_slug)

    return satisfied_slugs


def _branch_tip_sha(commits_ahead: list[str] | None) -> str | None:
    """Return the branch tip's abbreviated SHA from ``git log --oneline`` lines.

    ``commits_ahead`` is newest-first, so its first entry starts with the tip's
    abbreviated SHA. Reading it here costs nothing; asking git again for a value
    already in hand would add a subprocess per story to the triage loop.
    """
    if not commits_ahead:
        return None
    head = commits_ahead[0].split(maxsplit=1)
    return head[0] if head else None


def _merged_skip_reason(evidence: MergeEvidence, base_branch: str) -> str:
    """Render a skip reason that names the evidence the skip rests on.

    A skip discards preserved work, so it is reported as a skip and states what
    produced it — an operator reading ``SKIP issue-N (...)`` can then contest
    the specific claim instead of reading a bare "already merged" as completion
    (#2374). Every source is named, including the weakest one.
    """
    if evidence.pr_number is not None:
        detail = f"merged PR #{evidence.pr_number}"
    elif evidence.source == "audit":
        detail = "prior APPROVE in audit trail"
    elif evidence.source == "topology":
        detail = f"branch merged into {base_branch} history"
    elif evidence.source == "issue_commit":
        detail = f"closing reference in a {base_branch} commit message"
    else:
        detail = evidence.source or "unknown"
    return f"already merged to {base_branch} (evidence: {detail})"


def _triage_spec(
    story_path: str,
    config: ForgeConfig,
    project_root: Path,
    *,
    task: TaskStory | None = None,
    on_gate_start: Callable[[GateLabel], None] | None = None,
    on_gate_end: Callable[[GateLabel], None] | None = None,
) -> StoryTriage:
    """Determine the optimal re-entry point for a story.

    Decision tree:
      merged to base?           → skip_merged
      no worktree?              → full
      0 commits ahead?          → full (stale; run_task will clean up)
      gate passes?              → review
      gate fails?               → dev

    When *task* is provided, skip building from disk (used for issue-backed stories).

    ``on_gate_start`` / ``on_gate_end`` bracket the reuse gate — the only step
    here that can run for minutes. They fire with the gate's ``GateLabel`` so the
    caller can publish live progress for exactly that window, rather than for
    the cheap git probes that surround it (#2014). Every other path returns
    without calling either.
    """
    if task is None:
        full_path = (project_root / story_path).resolve()
        task = _build_task_from_story(full_path)
    slug = task.slug

    branch = config.workspace.branch_pattern.format(slug=slug)
    base_branch = config.workspace.base_branch
    worktree_path = project_root / config.workspace.path_pattern.format(slug=slug)
    commits_ahead: list[str] | None = None
    commits_behind: int | None = None

    # 1. Probe branch divergence up front so same-tip branches can distinguish
    # fast-forward merges (audit-backed skip_merged) from stale/empty worktrees
    # that were merely created at the base tip.
    try:
        log_result = subprocess.run(
            ["git", "log", f"{base_branch}..{branch}", "--oneline"],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        commits_ahead = [
            ln
            for ln in log_result.stdout.decode("utf-8", errors="replace").strip().splitlines()
            if ln.strip()
        ]
        if not commits_ahead:
            behind_result = subprocess.run(
                ["git", "rev-list", f"{branch}..{base_branch}", "--count"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            commits_behind = int(
                behind_result.stdout.decode("utf-8", errors="replace").strip() or "0"
            )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        commits_ahead = None
        commits_behind = None

    # 2. Check if already merged to base branch. This must stay ahead of the
    # stale/empty classification below: a same-tip branch is ambiguous, and
    # resolving it toward "no work was done" discards a landed story, while
    # resolving it toward "already merged" at worst repeats a skip (#2111).
    # Pass slug so _is_branch_merged can use the audit trail as a tiebreaker
    # for fast-forward merges where branch and base land on the same commit.
    merge_evidence = _branch_merge_evidence(branch, base_branch, project_root, slug=slug)
    if merge_evidence.merged:
        return StoryTriage(
            story_path=story_path,
            action="skip_merged",
            reason=_merged_skip_reason(merge_evidence, base_branch),
            worktree_path=None,
            slug=slug,
        )

    # 3. A branch at the base tip (0 ahead, 0 behind) with no merge evidence of
    # any kind is stale/empty, not merged. This can happen when a prior run
    # created the branch/worktree but never produced story commits. Treat it as
    # full so WORKSPACE recreates it, even if the old worktree directory has
    # already been removed. Step 2 above has already claimed every same-tip
    # branch backed by audit, PR, or issue-commit evidence.
    if commits_ahead == [] and commits_behind == 0:
        stale_reason = (
            f"branch is at {base_branch} HEAD with 0 commits ahead"
            if not worktree_path.exists()
            else f"worktree exists but branch is at {base_branch} HEAD (stale)"
        )
        return StoryTriage(
            story_path=story_path,
            action="full",
            reason=stale_reason,
            worktree_path=None,
            slug=slug,
        )

    # 4. Check if worktree exists
    if not worktree_path.exists():
        return StoryTriage(
            story_path=story_path,
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug=slug,
        )

    # 5. Check commits ahead of base branch
    if commits_ahead is None:
        try:
            log_result = subprocess.run(
                ["git", "log", f"{base_branch}..{branch}", "--oneline"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            commits_ahead = [
                ln
                for ln in log_result.stdout.decode("utf-8", errors="replace").strip().splitlines()
                if ln.strip()
            ]
        except (subprocess.TimeoutExpired, OSError):
            commits_ahead = []

    if not commits_ahead:
        # Stale worktree: 0 commits ahead of base. run_task WORKSPACE phase will
        # recreate it from scratch. Pass worktree_path=None so callers don't reuse it.
        return StoryTriage(
            story_path=story_path,
            action="full",
            reason=f"worktree exists but 0 commits ahead of {base_branch} (stale)",
            worktree_path=None,
            slug=slug,
        )

    # 5. Check audit trail for a prior review APPROVE
    if has_review_approve(project_root, slug, base_branch, branch):
        return StoryTriage(
            story_path=story_path,
            action="skip_merged",
            reason=(
                "prior APPROVE in audit trail; branch already satisfied "
                f"({len(commits_ahead)} commits ahead)"
            ),
            worktree_path=None,
            slug=slug,
        )

    # 6. Gate pre-check to decide REVIEW vs DEV entry
    gate_label = GateLabel(
        purpose=REUSE_GATE_PURPOSE,
        slug=slug,
        target=branch,
        commit=_branch_tip_sha(commits_ahead),
        worktree_path=str(worktree_path),
    )
    if on_gate_start is not None:
        on_gate_start(gate_label)
    # Which profile this pre-check ran, and what its result is worth. The triage
    # reason is this decision's durable record — it is what the sprint log and
    # state report for "why did this story enter at REVIEW" — so the profile
    # travels in it rather than being reconstructible only from config (#2358).
    gate_selection: list = []
    gate_facts: list[GateRunFacts] = []
    _gate_started = time.monotonic()
    try:
        gate_decision, gate_err, gate_output_tail = _run_gate(
            config,
            worktree_path,
            task=task,
            label=gate_label,
            selection_out=gate_selection,
            facts_out=gate_facts,
        )
    finally:
        gate_elapsed_s = time.monotonic() - _gate_started
        if on_gate_end is not None:
            on_gate_end(gate_label)

    profile_note = f", {gate_selection[0].describe()}" if gate_selection else ""

    if gate_err is None and gate_decision == "PASS":
        return StoryTriage(
            story_path=story_path,
            action="review",
            reason=(
                f"worktree exists, gate passes ({len(commits_ahead)} commits ahead{profile_note})"
            ),
            worktree_path=worktree_path,
            slug=slug,
        )

    reason_detail = gate_err or f"gate returned {gate_decision}"
    # A gate that ran out of time is a different condition from a gate that
    # failed, and the story it routes to DEV has to be told which one happened.
    # The flag comes from the gate's own run facts rather than from matching its
    # error text, so nothing downstream re-derives it (#2796).
    facts = gate_facts[0] if gate_facts else None
    gate_outcome = None
    if facts is not None and facts.timed_out:
        gate_outcome = EntryGateOutcome(
            outcome="timeout",
            command=facts.command,
            timeout_s=facts.timeout_s,
            elapsed_s=gate_elapsed_s,
            output_tail=gate_output_tail or "",
            profile=gate_selection[0].describe() if gate_selection else None,
        )
        _log(
            f"  reuse gate did not finish for {slug}:"
            f" killed at its {facts.timeout_s}s budget after {gate_elapsed_s:.1f}s"
        )
    return StoryTriage(
        story_path=story_path,
        action="dev",
        reason=f"worktree exists, gate fails ({reason_detail}{profile_note})",
        worktree_path=worktree_path,
        slug=slug,
        gate_outcome=gate_outcome,
    )
