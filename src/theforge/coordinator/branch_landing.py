"""One answer to "has this branch's work landed on the base branch?".

Two callers ask that question and used to answer it separately. Resume triage
(:mod:`theforge.sprint.dag`) consulted the audit trail, git topology, GitHub's
merged-PR record and a closing reference in a base commit. The worktree sweep
(:mod:`theforge.coordinator.workspace`) asked something narrower — are this
branch's own commits present on origin? — which a squash merge permanently
invalidates: the squash commit is a new SHA, so the branch's commits never
appear on origin as commits and the count stays non-zero forever (#2795).

So the question has one owner, here, and both callers consume its answer.

Three things this module deliberately does *not* do:

* It does not collapse to a boolean. "I could not tell" is a different answer
  from "it has not landed", and an operator staring at a preserved worktree
  needs to know which one they have. :class:`BranchLanding` carries a
  tri-state, the source that decided it, and — when it could not decide —
  which evidence was absent.
* It does not widen deletion. Only :data:`LANDED` is new grounds for
  reclaiming a worktree; :data:`UNLANDED` and :data:`UNDECIDABLE` preserve, as
  every unproven branch did before.
* It does not read unpublished-commit counts as evidence that work is absent.
  Local-only commits are a reason to be careful, never a proof that a landing
  did not happen elsewhere.

Evidence is consulted strongest-first, and cheapest-first within that: the
audit trail and git topology are local and decide before any ``gh`` subprocess
runs, because the sweep calls this once per candidate worktree on every
WORKSPACE entry.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .audit import has_review_approve, latest_run_outcome

#: The branch's work is in the base branch. Some source affirmed it.
LANDED = "landed"

#: The branch's work is provably *not* in the base branch, or the story's own
#: audit record says the run finished without landing. Preserve.
UNLANDED = "unlanded"

#: No source affirmed a landing and none proved the work absent. Preserve, and
#: say which evidence was missing.
UNDECIDABLE = "undecidable"


@dataclass(frozen=True)
class BranchLanding:
    """Whether a branch's work has landed, and what decided that.

    ``source`` names the evidence that produced a :data:`LANDED` or
    :data:`UNLANDED` verdict. ``absent_evidence`` lists, in the order they were
    consulted, the sources that declined or could not be read — it is what an
    operator needs to separate a genuinely unlanded branch from one nothing
    could speak for.
    """

    status: str
    source: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    absent_evidence: tuple[str, ...] = ()

    @property
    def landed(self) -> bool:
        return self.status == LANDED

    @property
    def undecidable(self) -> bool:
        return self.status == UNDECIDABLE

    def describe_source(self, base_branch: str) -> str:
        """Name the evidence this verdict rests on, for an operator to contest."""
        if self.pr_number is not None and self.landed:
            return f"merged PR #{self.pr_number}"
        if self.source == "content" and self.pr_number is not None:
            # A PR merged, and the branch still carries content base lacks. The
            # PR number belongs in the message — it is the claim an operator
            # would otherwise reach for — but it is not what this verdict rests
            # on, so it cannot be reported as the deciding evidence.
            absent_from = f"branch content is absent from {base_branch}"
            return f"{absent_from} despite merged PR #{self.pr_number}"
        if self.source == "audit":
            return "prior APPROVE in audit trail"
        if self.source == "topology":
            return f"branch merged into {base_branch} history"
        if self.source == "issue_commit":
            return f"closing reference in a {base_branch} commit message"
        if self.source == "content":
            return f"branch content is absent from {base_branch}"
        if self.source == "audit_contradiction":
            return "last recorded run for this story did not land"
        return self.source or "unknown"

    def describe_absent(self) -> str:
        """Name the evidence that was missing, for an undecidable verdict."""
        if not self.absent_evidence:
            return "no evidence source could be read"
        return "; ".join(self.absent_evidence)


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


def _has_prior_review_approve(
    project_root: Path,
    slug: str,
    base_branch: str,
    branch: str,
) -> bool:
    """Return True when audit history shows a landed APPROVE for this story.

    Merged detection needs the persisted review outcome even when the feature
    branch still appears ahead of base (squash merges rewrite commits, so git
    topology alone cannot prove the merge). This helper intentionally bypasses
    the stale-branch guard inside has_review_approve, but it still requires
    landing evidence so a zero-delta APPROVE or failed merge attempt does not
    satisfy the story during resume.
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


def _short_ref(ref: str) -> str:
    """Reduce a ref to its bare branch name for comparison.

    ``refs/heads/main``, ``origin/main`` and ``main`` all name the same branch;
    the configured base is a local branch name while GitHub reports the branch
    the PR merged *into*, so both spellings have to normalise before they can be
    compared.
    """
    name = ref.strip()
    for prefix in ("refs/heads/", "refs/remotes/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name.removeprefix("origin/")


def _lookup_merged_pr_for_branch(
    branch: str,
    project_root: Path,
    base_branch: str,
) -> BranchLanding | None:
    """Return merged PR metadata for ``branch`` when GitHub reports one.

    ``None`` covers both "GitHub reports no merged PR into ``base_branch``" and
    "the probe could not run"; :func:`_merged_pr_probe` is what separates them
    for the caller that has to report absent evidence.
    """
    landing, _probe_ok = _merged_pr_probe(branch, project_root, base_branch)
    return landing


def _merged_pr_probe(
    branch: str,
    project_root: Path,
    base_branch: str,
) -> tuple[BranchLanding | None, bool]:
    """Ask GitHub for a merged PR on ``branch``: (evidence, probe-succeeded).

    The second element is what makes a failed ``gh`` call undecidable rather
    than a statement that no PR exists. A network failure is not evidence.

    A merged PR only counts when it merged into ``base_branch``. "This branch
    was merged somewhere" is not the question — a repository with a release line
    routinely merges branches into refs that are not the base this run was
    configured against, and reading such a PR as a landing deleted work that had
    never reached the configured base (#2795, cycle 3). A record with no
    readable base ref is discarded for the same reason: an unnamed target cannot
    be checked against the one that matters.
    """
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
                "number,url,mergedAt,baseRefName",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None, False
        data = json.loads(result.stdout or "[]")
        if not isinstance(data, list):
            return None, False
        wanted_base = _short_ref(base_branch)
        merged_prs: list[tuple[str, int, str | None]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            merged_at = item.get("mergedAt")
            number = item.get("number")
            url = item.get("url")
            base_ref = item.get("baseRefName")
            if not isinstance(merged_at, str) or not merged_at:
                continue
            if not isinstance(number, int):
                continue
            if not isinstance(base_ref, str) or _short_ref(base_ref) != wanted_base:
                continue
            merged_prs.append((merged_at, number, url if isinstance(url, str) else None))
        if not merged_prs:
            return None, True
        _merged_at, pr_number, pr_url = max(merged_prs, key=lambda item: item[0])
        return (
            BranchLanding(
                status=LANDED,
                source="github_pr",
                pr_number=pr_number,
                pr_url=pr_url,
            ),
            True,
        )
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None, False


def _with_pr_metadata(
    landing: BranchLanding,
    branch: str,
    project_root: Path,
    issue_number: int | None,
    base_branch: str,
) -> BranchLanding:
    """Attach merged-PR metadata when GitHub can identify the landing PR."""
    if not landing.landed or issue_number is None or landing.pr_number is not None:
        return landing
    merged_pr = _lookup_merged_pr_for_branch(branch, project_root, base_branch)
    if merged_pr is None:
        return landing
    return BranchLanding(
        status=LANDED,
        source=landing.source,
        pr_number=merged_pr.pr_number,
        pr_url=merged_pr.pr_url,
        absent_evidence=landing.absent_evidence,
    )


def _rev_parse(project_root: Path, rev: str) -> str | None:
    """Resolve ``rev`` to an object id, or ``None`` when git cannot."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", rev],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    oid = result.stdout.decode("utf-8", errors="replace").strip()
    return oid or None


def _joined_base_by_merge(
    project_root: Path,
    branch: str,
    base_branch: str,
) -> bool | None:
    """Did ``branch`` join ``base_branch`` through a merge? ``None`` when unreadable.

    Called only once ``branch`` is known to be an ancestor of ``base_branch``
    with base ahead of it. Two very different histories produce that shape:

    * a real merge — ``branch`` entered base as the *second* parent of a merge
      commit, so it sits off base's first-parent line; and
    * an abandoned branch that never committed anything and was simply left
      pointing at an old base commit, which sits *on* that first-parent line.

    The earlier revision of this check tried to tell them apart by asking
    whether the branch still had commits base lacks — but once a branch is an
    ancestor of base, base lacks nothing of it, so that count is always zero and
    every genuine merge resolved as undecidable (#2795, cycle 2).

    First-parent position separates them exactly. Walking base's first-parent
    line down to the first commit ``branch`` already contains, the oldest commit
    listed has ``branch`` as its own first parent precisely when the branch is a
    plain old base commit. Anything else means base reached the branch by
    merging it.
    """
    try:
        walk = subprocess.run(
            ["git", "rev-list", "--first-parent", f"{branch}..{base_branch}"],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if walk.returncode != 0:
        return None
    line = [
        ln.strip()
        for ln in walk.stdout.decode("utf-8", errors="replace").splitlines()
        if ln.strip()
    ]
    if not line:
        # Base is ahead by the count above yet its first-parent walk is empty:
        # the two probes disagree, so this is not an answer.
        return None
    oldest_first_parent = _rev_parse(project_root, f"{line[-1]}^1")
    branch_tip = _rev_parse(project_root, branch)
    if oldest_first_parent is None or branch_tip is None:
        return None
    return oldest_first_parent != branch_tip


def _topology_landed(
    project_root: Path,
    branch: str,
    base_branch: str,
) -> bool | None:
    """Whether git topology alone proves the merge. ``None`` when unreadable.

    Only the regular-merge shape is provable here: the branch is an ancestor of
    base, base has advanced past it, and base reached the branch by merging it
    rather than by leaving it behind (see :func:`_joined_base_by_merge`). A
    fast-forward merge (same tip) and a squash merge are both invisible to
    topology, which is why the sources after it exist.
    """
    try:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_branch],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if ancestor.returncode == 1:
            return False
        if ancestor.returncode != 0:
            # 128 and friends: a bad ref or a broken repository. Not an answer.
            return None
        # Count commits in base_branch NOT reachable from branch.
        ahead_result = subprocess.run(
            ["git", "rev-list", f"{branch}..{base_branch}", "--count"],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        ahead_count = int(ahead_result.stdout.decode("utf-8", errors="replace").strip() or "0")
        if ahead_count == 0:
            # Same tip: a fast-forward merge and a branch that never committed
            # are identical here. The audit trail and PR record decide.
            return False
        return _joined_base_by_merge(project_root, branch, base_branch)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def resolve_branch_landing(
    branch: str,
    base_branch: str,
    project_root: Path,
    *,
    slug: str | None = None,
) -> BranchLanding:
    """Resolve whether ``branch``'s work has landed on ``base_branch``.

    Evidence is consulted strongest-first, and issue state is deliberately not a
    precondition for any of it (#2111). Whether a referencing GitHub issue is
    closed is a policy another system owns and is free to redefine — symptom bugs
    are now held open pending verification after their fix lands — so gating merge
    detection on it silently disabled detection for a whole class of story and
    re-ran work already in the base branch. Issue closure survives only as a
    corroborating signal for *external* dependencies in
    ``resolve_satisfied_dependencies``.

    Order of precedence:

    1. The forge audit trail — the evidence this run recorded when it landed the
       branch. Owned evidence is consulted before any external signal.
    2. Git topology — a regular merge, provable locally.
    3. GitHub's own record of a PR from the branch merged *into this base
       branch*, accepted only when the branch's current content does not
       contradict it.
    4. A base commit whose message *closes* the issue. This is the weakest
       signal — prose about the code rather than the code — so it runs last,
       only once every stronger source has declined, and only when neither the
       branch's content nor the audit trail contradicts it (#2374).

    Sources 3 and 4 are external claims *about* the branch, and both yield to
    the branch's content: work that is provably not in base has not landed,
    whoever says otherwise. Sources 1 and 2 are not claims — topology proves
    containment, and the audit assertion is this run's own landing record.

    Both local sources run before the ``gh`` subprocess, so the common cases
    cost no network at all.

    When nothing affirms a landing, the branch is :data:`UNLANDED` only on
    positive evidence that its work is absent from base — a content replay that
    changes base, or the story's own record of a run that finished without
    landing. Everything else is :data:`UNDECIDABLE` and names what was missing.
    """
    absent: list[str] = []
    issue_number = _issue_number_from_slug(slug) if slug is not None else None
    if issue_number is None:
        issue_number = _issue_number_from_ref(branch)

    content_absent: bool | None = None

    def _content_contradicts() -> bool:
        """Whether the branch provably carries content ``base_branch`` lacks.

        Computed at most once per resolution: both external sources need it and
        it costs a ``merge-tree`` replay.
        """
        nonlocal content_absent
        if content_absent is None:
            content_absent = _branch_adds_content_to_base(project_root, base_branch, branch)
        return content_absent

    # 1. Owned evidence: the APPROVE + landed record this run wrote itself.
    if slug is not None:
        try:
            if _has_prior_review_approve(project_root, slug, base_branch, branch):
                return _with_pr_metadata(
                    BranchLanding(status=LANDED, source="audit"),
                    branch,
                    project_root,
                    issue_number,
                    base_branch,
                )
            absent.append("no landed APPROVE in the audit trail")
        except Exception:
            # A transient audit-read failure must not discard the remaining
            # evidence sources below — fall through rather than claim unlanded.
            absent.append("audit trail unreadable")
    else:
        absent.append("no story slug, so the audit trail was not consulted")

    # 2. Git topology.
    topology = _topology_landed(project_root, branch, base_branch)
    if topology is True:
        return _with_pr_metadata(
            BranchLanding(status=LANDED, source="topology"),
            branch,
            project_root,
            issue_number,
            base_branch,
        )
    if topology is None:
        absent.append("git topology could not be read")
    else:
        absent.append(f"branch is not merged into {base_branch} by topology")

    # 3. GitHub's record of the merge, into this base branch. Fast-forward
    # merges at the same tip and squash merges are both invisible to topology,
    # so they need this and the issue-commit fallback below.
    #
    # A merged PR is *external* evidence — a claim about the branch made
    # somewhere else — and the same rule that governs the closing reference
    # below governs it: content beats a claim about content. A PR record is
    # about the commits that merged, not about the branch as it stands now, so
    # a branch that acquired further commits after its PR landed still holds
    # work base does not have, and reclaiming its worktree destroys that work
    # (#2795, cycle 3). The two local sources above are exempt: topology proves
    # containment by construction, and the audit assertion is this run's own
    # record of landing this story.
    if issue_number is None:
        absent.append(
            "no issue reference in the branch name, so the merged-PR lookup "
            "and closing-reference scan were skipped"
        )
    else:
        merged_pr, probe_ok = _merged_pr_probe(branch, project_root, base_branch)
        if merged_pr is not None:
            if not _content_contradicts():
                return merged_pr
            return BranchLanding(
                status=UNLANDED,
                source="content",
                pr_number=merged_pr.pr_number,
                pr_url=merged_pr.pr_url,
                absent_evidence=tuple(absent),
            )
        absent.append(
            f"GitHub reports no merged PR into {base_branch} for the branch"
            if probe_ok
            else "the merged-PR lookup could not run"
        )

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
    if _content_contradicts():
        return BranchLanding(
            status=UNLANDED,
            source="content",
            absent_evidence=tuple(absent),
        )
    if slug is not None and _audit_contradicts_merge(project_root, slug):
        return BranchLanding(
            status=UNLANDED,
            source="audit_contradiction",
            absent_evidence=tuple(absent),
        )
    if issue_number is not None:
        if _has_base_commit_closing_issue(project_root, base_branch, issue_number):
            return BranchLanding(status=LANDED, source="issue_commit")
        absent.append(f"no {base_branch} commit closes issue #{issue_number}")

    # Nothing affirmed a landing and nothing proved the work absent. Saying
    # "unlanded" here would be inventing the stronger of the two answers.
    return BranchLanding(status=UNDECIDABLE, absent_evidence=tuple(absent))
