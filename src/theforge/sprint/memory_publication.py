"""Protected-base-safe transport for publishing project memory (#2598).

Forge's project memory — canonical run records under ``.forge/audits/runs``,
knowledge summaries under ``.forge/knowledge/summaries``, and landing evidence
under ``.forge/audits/landing`` — is *tracked*. Forge's generated ``.gitignore``
denies ``.forge/**`` and then re-includes precisely those trees so the corpus
accumulates in version control and travels to a fresh clone. That is the
feature, not an implementation detail.

Until now the only way it reached a repository was a direct commit onto the base
branch in the project-root checkout. A repository that requires its base branch
to advance by merge or pull request — an ordinary and legitimate posture, and
one forge itself satisfies every time it lands a story — therefore never
received its own memory:

    ✗ SPRINT  canonical story run audit publish failed: Failed to commit story
    run audits at .forge/audits/runs ... ⛔ COMMIT BLOCKED: Non-doc changes on
    'main'.

The failure is not recoverable by retry, because the operation forge attempts
next sprint is the one the policy refuses. The workaround adopted downstream was
an allowlist entry granting forge direct-commit rights on a protected branch — a
privilege escalation to work around a publication path.

This module is that path. It has two halves, and separating them is what makes
the transport safe under parallelism as well as under branch protection:

**Staging.** :func:`stage_pending_project_memory` moves everything pending in
the tracked memory trees out of the project-root working tree into an ignored
staging area, leaving the checkout clean. A dirty project root refuses a
landing, and under ``max_parallel > 1`` that dirt is routinely a *sibling*
story's own run record. Publishing used to be the only thing that cleaned the
root, so a transport that cannot commit is also a transport that strands its
successor. Staging decouples the two: the root is clean the moment the artifacts
are staged, whether or not the publish that follows succeeds.

**Publication.** :func:`publish_staged_project_memory` republishes the staged
tree from an isolated git worktree on a dedicated long-lived memory branch, and
opens or updates a pull request into the base branch. The project-root checkout
is never committed to and never has its branch moved, so nothing this module
does can dirty a checkout another story is about to land into.

This module does not import the sprint runner, for the reason ``audit_publish``
does not (ADR-0008): a change to how memory is transported claims this file
alone.
"""

from __future__ import annotations

import datetime
import json
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..coordinator.landing_evidence import PROJECT_MEMORY_STAGING_RELPATH
from ..log_util import _log_line
from ..story_run_artifacts import STORY_RUN_ARTIFACT_DIRS, porcelain_paths

# The tracked project-memory trees this transport publishes. Defined in
# ``theforge.story_run_artifacts`` rather than here since #2775: the
# coordinator's landing precondition has to excuse exactly these trees, and it
# cannot import the publisher without closing a cycle. The alias keeps this
# module's own vocabulary — what it publishes is project memory — over a name
# chosen for the shared boundary.
PROJECT_MEMORY_DIRS: tuple[str, ...] = STORY_RUN_ARTIFACT_DIRS

# Ignored by ``.forge/**``, so staging never dirties the checkout it drains.
# Owned by ``coordinator.landing_evidence`` because the evidence readers have to
# look here too: between a publish and the memory pull request merging, this is
# where published project memory lives.
MEMORY_STAGING_RELPATH = PROJECT_MEMORY_STAGING_RELPATH

# One long-lived branch, updated in place. The alternative — a branch per
# sprint — produces one open pull request per sprint against a base the
# operator has already said they gate carefully, which converts a publication
# problem into a review-queue problem. See the spike document for the
# reasoning and for what the operator sees when the PR stays open.
MEMORY_BRANCH = "forge/project-memory"

_MEMORY_COMMIT_MESSAGE = "chore(memory): publish forge project memory"

# Publication end states.
MEMORY_PUBLISH_CLEAN = "clean"  # nothing staged; nothing to do
MEMORY_PUBLISH_PUBLISHED = "published"  # branch pushed and a PR is open
MEMORY_PUBLISH_PUSHED_NO_PR = "pushed_without_pr"  # branch pushed, no PR carrier
MEMORY_PUBLISH_STAGED_ONLY = "staged_only"  # staged and retained; publish failed
MEMORY_PUBLISH_NO_REMOTE = "no_remote"  # no origin to publish to


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def _sh(cmd: str, cwd: Path, timeout: int = 120) -> tuple[bool, str]:
    from ..coordinator import util as _cu  # noqa: PLC0415

    return _cu._run_shell(cmd, cwd, timeout=timeout)


def staging_dir(project_root: Path) -> Path:
    return project_root.joinpath(*MEMORY_STAGING_RELPATH)


@dataclass
class StagedMemory:
    """What staging drained out of the project-root checkout.

    ``paths`` are repo-relative, so the same list addresses the staging area and
    the memory worktree without either end knowing about the other's root.
    """

    root: Path
    paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.paths)


def pending_memory_paths(project_root: Path) -> list[str]:
    """Repo-relative paths pending in the tracked memory trees, if any.

    ``-uall`` rather than the default: porcelain collapses a wholly untracked
    tree to its top directory (``?? .forge/``), which names something broader
    than the memory trees. A first-ever sprint — one with no committed memory
    yet — is exactly the case that collapse would hide.
    """
    pending: list[str] = []
    for memory_dir in PROJECT_MEMORY_DIRS:
        directory = project_root / memory_dir
        if not directory.exists() and not directory.parent.exists():
            continue
        ok, out = _sh(f"git status --porcelain -uall -- {shlex.quote(memory_dir)}", project_root)
        if not ok:
            continue
        for path in porcelain_paths(out):
            if path.endswith(".tmp"):
                # A write in progress, or a stray one left by a repository whose
                # writers pre-date #2598. Never memory, never publishable.
                continue
            if path == memory_dir or path.startswith(f"{memory_dir}/"):
                pending.append(path)
    return sorted(set(pending))


def stage_pending_project_memory(project_root: Path) -> StagedMemory:
    """Drain pending project memory out of the checkout into the staging area.

    Artifacts absent from ``HEAD`` are moved out. Artifacts present in ``HEAD``
    are copied and then restored from it, because moving one leaves a deletion
    behind and the checkout would still be dirty — the condition staging exists
    to remove. Either way the working tree ends clean and the staging area holds
    the content to publish.

    Presence in ``HEAD`` is the test rather than presence in the index, because
    the direct transport this can be reached from ``git add``\\ s before it
    commits: a refused commit leaves new artifacts staged in the index, and a
    restore keyed to the index would restore exactly the content being drained.

    Best-effort per path: a file that cannot be staged is reported in
    ``errors`` and left where it is, rather than aborting the whole drain and
    stranding the artifacts that could have been staged. The caller decides what
    a partial drain means; it is never silently reported as a clean root,
    because the next dirt check will see what remains.
    """
    staged = StagedMemory(root=staging_dir(project_root))
    if not (project_root / ".git").exists():
        return staged
    for path in pending_memory_paths(project_root):
        source = project_root / path
        destination = staged.root / path
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                continue  # directories carry no content of their own
            quoted = shlex.quote(path)
            in_head, _ = _sh(f"git cat-file -e HEAD:{quoted}", project_root)
            if source.exists():
                shutil.copy2(source, destination)
            if in_head:
                # Restore the committed content; the staged copy carries the new.
                _sh(f"git reset -q HEAD -- {quoted}", project_root)
                ok, out = _sh(f"git checkout HEAD -- {quoted}", project_root)
                if not ok:
                    staged.errors.append(f"{path}: {out.strip()}")
                    continue
            else:
                # Never committed here. Drop it from the index if a refused
                # direct publish left it staged, then take it off disk.
                _sh(f"git rm -q --cached --force -- {quoted}", project_root)
                if source.exists():
                    source.unlink()
            staged.paths.append(path)
        except OSError as exc:
            staged.errors.append(f"{path}: {exc}")
    return staged


def staged_memory_paths(project_root: Path) -> list[str]:
    """Repo-relative paths currently held in the staging area."""
    root = staging_dir(project_root)
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def prune_merged_staged_memory(project_root: Path, base_branch: str) -> list[str]:
    """Drop staged artifacts whose content is now in the base branch.

    Staging is retained after a successful publish rather than cleared, because
    until the memory pull request merges it is the only place forge's own
    readers can find what it just published — the canonical tree was drained to
    publish it. Retaining it forever would grow without bound, so what is
    already in the base branch is dropped: that content is in the checkout's
    history, and the next pull puts it back under ``.forge/audits``.

    Returns the paths pruned.
    """
    root = staging_dir(project_root)
    pruned: list[str] = []
    for path in staged_memory_paths(project_root):
        ok, _ = _sh(
            f"git cat-file -e origin/{shlex.quote(base_branch)}:{shlex.quote(path)}",
            project_root,
        )
        if not ok:
            continue
        try:
            (root / path).unlink()
            pruned.append(path)
        except OSError:
            continue
    for directory in sorted(root.rglob("*"), reverse=True) if root.exists() else []:
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return pruned


def _has_origin(project_root: Path) -> bool:
    ok, out = _sh("git remote", project_root)
    return ok and "origin" in out.split()


def _remote_branch_exists(project_root: Path, branch: str) -> bool:
    ok, out = _sh(f"git ls-remote --heads origin {shlex.quote(branch)}", project_root, timeout=60)
    return ok and bool(out.strip())


def _memory_branch_start_point(project_root: Path, base_branch: str) -> str:
    """Where a fresh memory worktree should start.

    Continue the existing memory branch when there is one, so unmerged memory
    from an earlier sprint is not dropped on the floor. Restart from the base
    when the branch's content is already in the base — the pull request merged,
    and continuing from a branch whose commits are all ancestors of base would
    grow a permanently-behind branch for no reason.
    """
    remote_memory = f"origin/{MEMORY_BRANCH}"
    if not _remote_branch_exists(project_root, MEMORY_BRANCH):
        return f"origin/{base_branch}"
    _sh(f"git fetch origin {shlex.quote(MEMORY_BRANCH)}", project_root, timeout=60)
    merged, _ = _sh(
        f"git merge-base --is-ancestor {shlex.quote(remote_memory)} "
        f"origin/{shlex.quote(base_branch)}",
        project_root,
    )
    return f"origin/{base_branch}" if merged else remote_memory


def _open_memory_pr(project_root: Path, base_branch: str) -> str | None:
    """Open a pull request for the memory branch, or return the existing one.

    ``gh`` absence is not an error here. A repository forge can push to but
    cannot open a PR against still gets its memory onto a branch, and the
    operator gets a state that says exactly that (``pushed_without_pr``) rather
    than a failure that says nothing about what did work.
    """
    if shutil.which("gh") is None:
        return None
    ok, out = _sh(
        f"gh pr list --head {shlex.quote(MEMORY_BRANCH)} --state open --json url",
        project_root,
        timeout=60,
    )
    if ok:
        try:
            existing = json.loads(out.strip() or "[]")
        except ValueError:
            existing = []
        if isinstance(existing, list) and existing:
            url = existing[0].get("url") if isinstance(existing[0], dict) else None
            if isinstance(url, str) and url:
                return url
    body = (
        "Canonical run records, knowledge summaries and landing evidence produced "
        "by TheForge.\n\nThese are generated project memory: additive files under "
        "`.forge/audits/` and `.forge/knowledge/summaries/`. They carry no source "
        "changes. Merging keeps the corpus accumulating in this repository.\n"
    )
    ok_create, create_out = _sh(
        "gh pr create "
        f"--base {shlex.quote(base_branch)} --head {shlex.quote(MEMORY_BRANCH)} "
        f"--title {shlex.quote('chore(memory): publish forge project memory')} "
        f"--body {shlex.quote(body)}",
        project_root,
        timeout=120,
    )
    if not ok_create:
        _log(f"⚠ SPRINT  could not open a project-memory pull request: {create_out.strip()}")
        return None
    for token in create_out.split():
        if token.startswith("http"):
            return token.strip()
    return None


def publish_staged_project_memory(
    project_root: Path,
    base_branch: str,
    *,
    push: bool = True,
) -> tuple[str, str | None]:
    """Publish staged memory from an isolated worktree on the memory branch.

    Returns ``(state, pr_url)``. The staging area is cleared only on a state
    that actually got the content off this machine; anything else retains it, so
    a later sprint's publish carries it forward rather than losing it.

    The project-root checkout is read but never committed to and never has its
    branch moved. That is the property the whole transport rests on: a story
    landing into the project root at the same moment sees a checkout this
    function has not touched.
    """
    paths = staged_memory_paths(project_root)
    if not paths:
        return MEMORY_PUBLISH_CLEAN, None
    if not push or not _has_origin(project_root):
        _log(
            f"⚠ SPRINT  {len(paths)} project-memory artifact(s) staged at "
            f"{'/'.join(MEMORY_STAGING_RELPATH)}: no origin to publish them to."
        )
        return MEMORY_PUBLISH_NO_REMOTE, None

    ok_fetch, fetch_out = _sh(
        f"git fetch origin {shlex.quote(base_branch)}", project_root, timeout=120
    )
    if not ok_fetch:
        _log(f"⚠ SPRINT  project-memory publish could not fetch origin: {fetch_out.strip()}")
        return MEMORY_PUBLISH_STAGED_ONLY, None

    start_point = _memory_branch_start_point(project_root, base_branch)
    temp_root = Path(tempfile.mkdtemp(prefix="forge-memory-"))
    worktree = temp_root / "memory"
    try:
        ok_wt, wt_out = _sh(
            f"git worktree add --force -B {shlex.quote(MEMORY_BRANCH)} "
            f"{shlex.quote(str(worktree))} {shlex.quote(start_point)}",
            project_root,
            timeout=180,
        )
        if not ok_wt:
            _log(f"⚠ SPRINT  project-memory worktree could not be created: {wt_out.strip()}")
            return MEMORY_PUBLISH_STAGED_ONLY, None

        for path in paths:
            source = staging_dir(project_root) / path
            destination = worktree / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        quoted = " ".join(shlex.quote(path) for path in paths)
        ok_add, add_out = _sh(f"git add --force -- {quoted}", worktree)
        if not ok_add:
            _log(f"⚠ SPRINT  project-memory publish could not stage artifacts: {add_out.strip()}")
            return MEMORY_PUBLISH_STAGED_ONLY, None

        ok_diff, diff_out = _sh("git status --porcelain", worktree)
        if ok_diff and not diff_out.strip():
            # Everything staged is already on the memory branch — a resumed or
            # repeated publish. Nothing to commit; staging is still the local
            # read-through for anything the base branch has not absorbed yet.
            prune_merged_staged_memory(project_root, base_branch)
            return MEMORY_PUBLISH_CLEAN, None

        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        ok_commit, commit_out = _sh(
            f"git commit -m {shlex.quote(f'{_MEMORY_COMMIT_MESSAGE} ({stamp})')}", worktree
        )
        if not ok_commit:
            _log(f"⚠ SPRINT  project-memory publish could not commit: {commit_out.strip()}")
            return MEMORY_PUBLISH_STAGED_ONLY, None

        ok_push, push_out = _sh(
            f"git push --force-with-lease origin {shlex.quote(MEMORY_BRANCH)}",
            worktree,
            timeout=180,
        )
        if not ok_push:
            _log(f"⚠ SPRINT  project-memory branch push was refused: {push_out.strip()}")
            return MEMORY_PUBLISH_STAGED_ONLY, None

        prune_merged_staged_memory(project_root, base_branch)
        pr_url = _open_memory_pr(project_root, base_branch)
        if pr_url:
            _log(f"Published forge project memory to {MEMORY_BRANCH} ({pr_url}).")
            return MEMORY_PUBLISH_PUBLISHED, pr_url
        _log(
            f"Published forge project memory to origin/{MEMORY_BRANCH}; no pull request "
            f"carrier was created. Open one into '{base_branch}' to land the corpus."
        )
        return MEMORY_PUBLISH_PUSHED_NO_PR, None
    finally:
        _sh(f"git worktree remove --force {shlex.quote(str(worktree))}", project_root)
        _sh("git worktree prune", project_root)
        shutil.rmtree(temp_root, ignore_errors=True)


def stage_and_publish_project_memory(
    project_root: Path,
    base_branch: str,
    *,
    push: bool = True,
) -> tuple[str, str | None]:
    """Drain the checkout and publish, in that order.

    The order is the contract. Staging is what unblocks the next story; the
    publish is what gets the corpus off this machine. A publish failure must
    therefore never undo the drain, which is why staging retains its content
    instead of putting it back.
    """
    staged = stage_pending_project_memory(project_root)
    if staged.errors:
        _log(
            "⚠ SPRINT  some project-memory artifacts could not be staged: "
            + "; ".join(staged.errors)
        )
    return publish_staged_project_memory(project_root, base_branch, push=push)
