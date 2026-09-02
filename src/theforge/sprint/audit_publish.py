"""Terminal sprint audit publication (#2402).

A sprint's last act is to write down what it did and put that record where the
next run — and the operator — can find it. That is one responsibility with a
boundary this module owns end to end: it begins by constructing the inputs the
sprint audit and summary writers need out of the run's context and execution
state, and it ends with the canonical per-run audit JSON committed and pushed to
the base branch, or a publication failure reported against a recorded end state.

It moved here out of ``sprint/runner.py`` under ADR-0008. The point of the move
is independent changeability, so the one rule this module keeps is that **it does
not import the runner**: a change to how a sprint publishes its audits claims
this file, and a change to how it resolves a queued PR claims the runner, and the
scheduler is free to run them at the same time. A dependency back on the runner
would give the two a shared claim again and make the move a relocation.

That rule is why the entry points annotate the sprint's execution state as
``Any`` rather than importing :class:`~theforge.sprint.runner.SprintExecutionState`
for the annotation alone. They take the state object itself — never a list of the
members it carries, which is the parameter threading #2399 ended — and read
``state.context`` for the frozen run context.
"""

from __future__ import annotations

import datetime
import json
import shlex
from pathlib import Path
from typing import Any

from ..config import ForgeConfig
from ..coordinator import workspace as coordinator_workspace
from ..log_util import _log_line
from .audit import _write_sprint_audit, _write_sprint_summary
from .manifest import SprintResult
from .memory_publication import (
    MEMORY_PUBLISH_CLEAN,
    MEMORY_PUBLISH_NO_REMOTE,
    MEMORY_PUBLISH_PUBLISHED,
    MEMORY_PUBLISH_PUSHED_NO_PR,
    MEMORY_PUBLISH_STAGED_ONLY,
    PROJECT_MEMORY_DIRS,
    porcelain_paths,
    stage_and_publish_project_memory,
)

# The tracked project-memory trees this module publishes. Owned by
# ``memory_publication`` so the transport and the dirt attribution below can
# never disagree about what "a story-run artifact" is (#2598).
_STORY_RUN_ARTIFACT_DIRS = PROJECT_MEMORY_DIRS
_STORY_RUN_AUDIT_COMMIT_MESSAGE = "chore(audit): record sprint run audits"

# Where the outcome of the publish step is recorded. Lives under .forge/ (which
# .gitignore denies wholesale), so writing it never dirties the base-branch
# checkout the sprint is publishing from.
_STORY_RUN_AUDIT_PUBLISH_STATE_PATH = ".forge/audit-publish-state.json"

# A push rejected because the base branch moved is reconciled and retried. Three
# attempts covers the sprint's own merges landing while the push is in flight;
# beyond that the remote is being advanced by something other than this run and
# looping longer just delays the operator's answer.
_STORY_RUN_AUDIT_PUSH_ATTEMPTS = 3

# Publish end states, recorded to ``_STORY_RUN_AUDIT_PUBLISH_STATE_PATH`` and
# carried on ``StoryRunAuditPublishError.state``. They exist so that an operator
# looking at local-only audit commits can tell *which* thing happened: the run
# never got here (no/stale record), it got here and the remote refused
# (``push_refused``), or publishing was deliberately skipped (``local_only``).
AUDIT_PUBLISH_CLEAN = "clean"
AUDIT_PUBLISH_COMMITTED = "committed_unpublished"
AUDIT_PUBLISH_PUBLISHED = "published"
AUDIT_PUBLISH_LOCAL_ONLY = "local_only"
AUDIT_PUBLISH_COMMIT_FAILED = "commit_failed"
AUDIT_PUBLISH_BRANCH_MISMATCH = "branch_mismatch"
AUDIT_PUBLISH_PUSH_REFUSED = "push_refused"
AUDIT_PUBLISH_RECONCILE_FAILED = "reconcile_failed"
AUDIT_PUBLISH_VERIFY_FAILED = "verify_failed"


def _log(msg: str) -> None:
    # Worker-slug prefixing (parallel attribution) is applied centrally by
    # ``_log_line``; do not prepend it here or it would double-tag. The prefix
    # stays "[sprint]" after the move: this is still the sprint speaking, and an
    # operator reading a run log should not have to learn a new tag because a
    # function changed files.
    _log_line("[sprint]", msg)


def _story_run_artifact_label(artifact_dir: str) -> str:
    """Return an operator-facing label for a tracked story-run artifact tree."""
    if artifact_dir == ".forge/audits/runs":
        return "story run audits"
    if artifact_dir == ".forge/knowledge/summaries":
        return "knowledge summaries"
    if artifact_dir == ".forge/audits/landing":
        return "landing evidence"
    return f"story run artifacts under {artifact_dir}"


def _porcelain_paths(dirty_status: str) -> list[str]:
    """Paths named by a ``git status --porcelain`` block, one per entry.

    Delegates to the transport's parser, which does not slice a fixed status
    column: the status text reaching this function has already been stripped by
    ``_run_shell``, so a fixed slice ate the first character of the first path
    whenever that path's status was worktree-only (``" M "``). Attribution then
    failed to recognise forge's own artifact and the landing refused instead of
    republishing (#2598).
    """
    return porcelain_paths(dirty_status)


def story_run_artifact_dirt_only(dirty_status: str) -> bool:
    """Whether every path in a porcelain status block is a story-run artifact.

    A dirty project root refuses a landing, and under ``max_parallel > 1`` the
    dirt is routinely a *sibling* story's own canonical run record and knowledge
    summary, written between the losing story's entry check and its merge
    (#2602). Distinguishing that from operator dirt is what lets the integration
    seam republish and retry instead of discarding approved, paid-for work —
    while operator dirt still refuses exactly as before.

    Returns ``False`` for a clean status: there is nothing to attribute.
    """
    paths = _porcelain_paths(dirty_status)
    if not paths:
        return False
    return all(
        any(
            path == artifact_dir or path.startswith(f"{artifact_dir}/")
            for artifact_dir in _STORY_RUN_ARTIFACT_DIRS
        )
        for path in paths
    )


def project_root_dirt_is_story_run_artifacts_only(project_root: Path) -> bool:
    """Whether the project root's *only* dirt is pending story-run artifacts.

    Asks git with ``-uall`` rather than reading the landing check's own status
    text: the default porcelain output collapses a wholly-untracked tree to its
    top directory (``?? .forge/``), which names something broader than the
    artifact trees and could not be attributed to a sibling. Expanding the entry
    is what lets a first-ever sprint — one with no committed artifacts yet — get
    the same tolerance as a steady-state one.

    Fails closed: a root git cannot describe is not a root to retry a landing
    into.
    """
    from ..coordinator import util as _cu  # noqa: PLC0415

    ok, out = _cu._run_shell("git status --porcelain -uall", project_root)
    if not ok:
        return False
    return story_run_artifact_dirt_only(out.strip())


def read_audit_publish_state(project_root: Path) -> str | None:
    """The last recorded publish end state for ``project_root``, if any.

    Best-effort: an unreadable or absent marker is simply no answer, never an
    error, because every caller is reporting *about* a failure already.
    """
    path = project_root / _STORY_RUN_AUDIT_PUBLISH_STATE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = payload.get("state") if isinstance(payload, dict) else None
    return recorded if isinstance(recorded, str) else None


class StoryRunAuditPublishError(RuntimeError):
    """Canonical story run audits could not be published to the base branch.

    ``state`` is one of the ``AUDIT_PUBLISH_*`` constants and names the end
    state the checkout was left in. It is a ``RuntimeError`` subclass because
    the sprint entry point already treats a publish failure as fatal; the extra
    attribute only makes the *kind* of failure legible to callers and tests.
    """

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


def _record_audit_publish_state(
    project_root: Path,
    base_branch: str,
    state: str,
    detail: str | None = None,
) -> None:
    """Record the publish end state next to the checkout it describes.

    Best-effort: a sprint must not fail because this marker could not be
    written, and the marker must never mask the error it is describing.
    """
    path = project_root / _STORY_RUN_AUDIT_PUBLISH_STATE_PATH
    payload = {
        "state": state,
        "base_branch": base_branch,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "detail": detail,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover — filesystem-level failure
        _log(f"Warning: could not record story run audit publish state: {exc}")


def _require_audit_publish_branch(project_root: Path, base_branch: str, *, operation: str) -> None:
    """Refuse before mutating the project root from the wrong checked-out branch."""
    try:
        current_branch = coordinator_workspace._current_checked_out_branch(project_root)
    except RuntimeError as exc:
        raise StoryRunAuditPublishError(
            f"Failed to verify the checked-out branch before {operation} story run audits: {exc}",
            state=AUDIT_PUBLISH_BRANCH_MISMATCH,
        ) from exc
    if current_branch == base_branch:
        return
    raise StoryRunAuditPublishError(
        f"Refusing to {operation} story run audits for base branch '{base_branch}' because "
        f"the project root currently has '{current_branch}' checked out. Check out "
        f"{base_branch} and rerun the publish, or move the pending audit records off "
        f"'{current_branch}'.",
        state=AUDIT_PUBLISH_BRANCH_MISMATCH,
    )


def _reconcile_base_with_origin(project_root: Path, base_branch: str) -> None:
    """Fetch origin and rebase the local base branch onto its current head.

    Raises ``StoryRunAuditPublishError`` when the reconcile itself cannot be
    completed, aborting any partial rebase so the checkout is left usable.
    """
    from ..coordinator import util as _cu  # noqa: PLC0415

    _require_audit_publish_branch(project_root, base_branch, operation="reconcile")
    quoted_base = shlex.quote(base_branch)
    ok_fetch, fetch_out = _cu._run_shell(f"git fetch origin {quoted_base}", project_root)
    if not ok_fetch:
        raise StoryRunAuditPublishError(
            f"Failed to fetch origin/{base_branch} while publishing story run audits: "
            f"{fetch_out.strip()}",
            state=AUDIT_PUBLISH_RECONCILE_FAILED,
        )

    ok_rebase, rebase_out = _cu._run_shell(
        f"git rebase origin/{quoted_base} {quoted_base}",
        project_root,
    )
    if not ok_rebase:
        _cu._run_shell("git rebase --abort", project_root)
        raise StoryRunAuditPublishError(
            f"Failed to rebase '{base_branch}' onto origin/{base_branch} while publishing "
            f"story run audits: {rebase_out.strip()}",
            state=AUDIT_PUBLISH_RECONCILE_FAILED,
        )


def _commit_story_run_audits(project_root: Path, base_branch: str, *, publish: bool) -> None:
    """Commit and publish canonical per-run audit JSON emitted during a sprint.

    The sprint writes these records to the project-root base-branch checkout on
    the operator's behalf. A commit that is never pushed is unowned state: later
    story worktrees are cut from that checkout and GitHub attributes the audit
    JSON to whichever story happens to be running. So the commit is only half
    the operation — this pushes it to origin and verifies the base branch is no
    longer ahead, raising loudly if either step fails.

    ``publish`` comes from ``_base_branch_tracks_origin``: it is false only when
    this run lands stories by merging into the local base checkout *and* has
    opted out of pushing them. Pushing a branch publishes all of its ancestors,
    so a push here would then also publish those local merges. In that one
    configuration the commit stays local and the fact is warned about instead.

    A rejected push is not a failure of this step so much as a statement about
    where the remote is: the sprint's own merges are what usually advance the
    base branch, so a run that landed stories is the *normal* case for the local
    branch being behind. So a rejection is reconciled (fetch + rebase onto the
    current remote head) and retried, and only exhausting that raises. Whichever
    end state the checkout lands in is recorded to
    ``.forge/audit-publish-state.json`` so it survives to where the state is
    observed.
    """
    from ..coordinator import util as _cu  # noqa: PLC0415

    if not (project_root / ".git").exists():
        return

    try:
        _require_audit_publish_branch(project_root, base_branch, operation="publish")
    except StoryRunAuditPublishError as exc:
        _record_audit_publish_state(project_root, base_branch, exc.state, detail=str(exc))
        raise

    dirty_dirs: list[str] = []
    for artifact_dir in _STORY_RUN_ARTIFACT_DIRS:
        artifact_path = project_root / artifact_dir
        if not artifact_path.exists() and not artifact_path.parent.exists():
            continue
        quoted_artifact_dir = shlex.quote(artifact_dir)
        ok_status, status_out = _cu._run_shell(
            f"git status --porcelain -- {quoted_artifact_dir}",
            project_root,
        )
        if not ok_status:
            raise StoryRunAuditPublishError(
                f"Failed to inspect {_story_run_artifact_label(artifact_dir)} "
                f"at {artifact_dir}: {status_out}",
                state=AUDIT_PUBLISH_COMMIT_FAILED,
            )
        if status_out.strip():
            dirty_dirs.append(artifact_dir)
    if not dirty_dirs:
        # Nothing pending. Record it so a marker from an earlier run cannot be
        # mistaken for this one's outcome.
        _record_audit_publish_state(project_root, base_branch, AUDIT_PUBLISH_CLEAN)
        return

    quoted_dirty_dirs = " ".join(shlex.quote(path) for path in dirty_dirs)
    ok_add, add_out = _cu._run_shell(f"git add -- {quoted_dirty_dirs}", project_root)
    if not ok_add:
        artifact_list = ", ".join(
            f"{_story_run_artifact_label(path)} at {path}" for path in dirty_dirs
        )
        raise StoryRunAuditPublishError(
            f"Failed to stage {artifact_list}: {add_out}",
            state=AUDIT_PUBLISH_COMMIT_FAILED,
        )

    commit_cmd = f'git commit -m "{_STORY_RUN_AUDIT_COMMIT_MESSAGE}" -- {quoted_dirty_dirs}'
    ok_commit, commit_out = _cu._run_shell(commit_cmd, project_root)
    if not ok_commit:
        artifact_list = ", ".join(
            f"{_story_run_artifact_label(path)} at {path}" for path in dirty_dirs
        )
        raise StoryRunAuditPublishError(
            f"Failed to commit {artifact_list}: {commit_out}",
            state=AUDIT_PUBLISH_COMMIT_FAILED,
        )
    _log("Committed canonical story run audit records to the base branch checkout.")
    # Written before the push so that a crash mid-publish is distinguishable
    # from a run that never reached this function at all.
    _record_audit_publish_state(project_root, base_branch, AUDIT_PUBLISH_COMMITTED)

    if not publish:
        _record_audit_publish_state(
            project_root,
            base_branch,
            AUDIT_PUBLISH_LOCAL_ONLY,
            detail="workspace.auto_push is off for a run that lands stories locally",
        )
        _log(
            f"⚠ SPRINT  story run audit records remain local: this run merges stories into "
            f"'{base_branch}' with workspace.auto_push off, so pushing would also publish those "
            f"local merges. Push '{base_branch}' yourself before any workflow that diffs a story "
            f"branch against origin/{base_branch}."
        )
        return

    quoted_base = shlex.quote(base_branch)
    push_out = ""
    for attempt in range(1, _STORY_RUN_AUDIT_PUSH_ATTEMPTS + 1):
        ok_push, push_out = _cu._run_shell(
            f"git push origin {quoted_base}",
            project_root,
        )
        if ok_push:
            break
        if attempt == _STORY_RUN_AUDIT_PUSH_ATTEMPTS:
            _record_audit_publish_state(
                project_root,
                base_branch,
                AUDIT_PUBLISH_PUSH_REFUSED,
                detail=push_out.strip(),
            )
            raise StoryRunAuditPublishError(
                f"Failed to push story run audits to origin/{base_branch} after "
                f"{_STORY_RUN_AUDIT_PUSH_ATTEMPTS} attempts (fetch + rebase between "
                f"attempts): {push_out.strip()}",
                state=AUDIT_PUBLISH_PUSH_REFUSED,
            )
        _log(
            f"Push of story run audits to origin/{base_branch} was refused "
            f"(attempt {attempt}/{_STORY_RUN_AUDIT_PUSH_ATTEMPTS}); reconciling with the "
            f"current remote head and retrying."
        )
        try:
            _reconcile_base_with_origin(project_root, base_branch)
        except StoryRunAuditPublishError as exc:
            _record_audit_publish_state(
                project_root,
                base_branch,
                exc.state,
                detail=str(exc),
            )
            raise

    ok_ahead, ahead_out = _cu._run_shell(
        f"git rev-list --count origin/{quoted_base}..{quoted_base}",
        project_root,
    )
    if not ok_ahead:
        message = (
            f"Failed to verify story run audits reached origin/{base_branch}: {ahead_out.strip()}"
        )
        _record_audit_publish_state(
            project_root, base_branch, AUDIT_PUBLISH_VERIFY_FAILED, detail=message
        )
        raise StoryRunAuditPublishError(message, state=AUDIT_PUBLISH_VERIFY_FAILED)
    try:
        ahead = int(ahead_out.strip())
    except ValueError:
        message = (
            f"Failed to verify story run audits reached origin/{base_branch}: "
            f"unexpected rev-list output {ahead_out.strip()!r}"
        )
        _record_audit_publish_state(
            project_root, base_branch, AUDIT_PUBLISH_VERIFY_FAILED, detail=message
        )
        raise StoryRunAuditPublishError(message, state=AUDIT_PUBLISH_VERIFY_FAILED) from None
    if ahead > 0:
        message = (
            f"Story run audits were committed but '{base_branch}' is still {ahead} commit(s) "
            f"ahead of origin/{base_branch} after push. Publish or reset it before rerunning."
        )
        _record_audit_publish_state(
            project_root, base_branch, AUDIT_PUBLISH_VERIFY_FAILED, detail=message
        )
        raise StoryRunAuditPublishError(message, state=AUDIT_PUBLISH_VERIFY_FAILED)
    _record_audit_publish_state(project_root, base_branch, AUDIT_PUBLISH_PUBLISHED)
    _log(f"Pushed canonical story run audit records to origin/{base_branch}.")


TRANSPORT_DIRECT = "direct"
TRANSPORT_MEMORY_BRANCH = "memory-branch"

# Publish end states that mean the base branch refused what forge asked of it,
# rather than that the operation could not be attempted. These are the states a
# branch-protection policy produces, and the ones the memory-branch transport
# can actually rescue: retrying the same direct commit next sprint attempts the
# operation the policy refuses (#2598).
_REFUSED_BY_POLICY = frozenset({AUDIT_PUBLISH_COMMIT_FAILED, AUDIT_PUBLISH_PUSH_REFUSED})


def memory_transport(*, lands_locally: bool) -> str:
    """Which transport publishes a run's project memory.

    The question is not which ``on_approve`` mode is configured but whether this
    run advances the base branch from the project-root checkout at all. A run
    that does — ``on_approve: merge``, or ``--auto-merge`` in sequential mode —
    is already committing there, so a memory commit alongside its merges is
    consistent with what it does anyway and keeps the corpus on the base branch
    with no review latency. A run that does not reaches the base branch only
    through pull requests, and a direct memory commit would be the *one* thing
    forge does to that branch without one.

    The choice is not final in either direction: a direct commit the base branch
    refuses falls back to the memory branch, which is what makes the reported
    ``merge``-mode failure recoverable without granting forge direct-commit
    rights on a protected branch.
    """
    return TRANSPORT_DIRECT if lands_locally else TRANSPORT_MEMORY_BRANCH


def _publish_via_memory_branch(config: ForgeConfig, *, push: bool, reason: str) -> str:
    """Stage the checkout clean and publish from the memory branch.

    Never raises; returns the end state so a caller that reached here as a
    *fallback* can decide whether the original failure has actually been
    rescued. Every end state leaves the project root clean and the artifacts
    either published or retained in staging, so there is no outcome that both
    loses records and contaminates a later story's checkout — the two failure
    modes the raising direct path exists to shout about.
    """
    state, pr_url = stage_and_publish_project_memory(
        config.project_root,
        config.workspace.base_branch,
        push=push,
    )
    detail = f"{reason}; pr={pr_url}" if pr_url else reason
    _record_audit_publish_state(
        config.project_root,
        config.workspace.base_branch,
        f"memory_branch_{state}",
        detail=detail,
    )
    if state in {MEMORY_PUBLISH_PUBLISHED, MEMORY_PUBLISH_CLEAN, MEMORY_PUBLISH_PUSHED_NO_PR}:
        return state
    if state == MEMORY_PUBLISH_NO_REMOTE:
        _log(
            "⚠ SPRINT  project memory is staged locally and this checkout has no origin; "
            "it will publish on the first run with a remote."
        )
        return state
    if state == MEMORY_PUBLISH_STAGED_ONLY:
        _log(
            "⚠ SPRINT  project memory could not be published this run; it is retained under "
            f"{'/'.join(('.forge', 'memory-staging'))} and the next publish carries it forward."
        )
    return state


def publish_story_run_artifacts_for_config(
    config: ForgeConfig,
    *,
    lands_locally: bool,
) -> None:
    """Publish pending canonical story-run artifacts for a config-owned checkout.

    This is the shared publication surface for any entry point that writes the
    tracked story-run artifact trees into ``config.project_root``. Callers pass
    whether their landing path leaves commits on the local base branch, and this
    helper derives the rest of the publish contract from config.
    """
    from ..coordinator.workspace import _base_branch_tracks_origin  # noqa: PLC0415

    push = _base_branch_tracks_origin(config, lands_locally=lands_locally)
    if memory_transport(lands_locally=lands_locally) == TRANSPORT_MEMORY_BRANCH:
        _publish_via_memory_branch(
            config, push=True, reason="this run reaches the base branch through pull requests"
        )
        return

    try:
        _commit_story_run_audits(
            config.project_root,
            config.workspace.base_branch,
            publish=push,
        )
    except StoryRunAuditPublishError as exc:
        if getattr(exc, "state", None) not in _REFUSED_BY_POLICY:
            raise
        _log(
            "⚠ SPRINT  the base branch refused a direct project-memory commit "
            f"({exc}); republishing through the memory branch instead."
        )
        rescued = _publish_via_memory_branch(
            config,
            push=push,
            reason=f"direct publish refused ({exc.state})",
        )
        if rescued not in {MEMORY_PUBLISH_PUBLISHED, MEMORY_PUBLISH_PUSHED_NO_PR}:
            # The fallback did not get the corpus off this machine, so the
            # original failure stands. Downgrading it on the strength of an
            # alternative that also did nothing would report a publication that
            # did not happen — and the direct path raises precisely so a sprint
            # cannot exit reporting success over unpublished base-branch state.
            raise


def write_terminal_sprint_audits(
    state: Any,  # SprintExecutionState — see the module docstring on why not typed
    *,
    result: SprintResult,
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    sprint_log_dir: Path | None,
    dropped_slugs: dict[str, str] | None,
    triages: dict[str, Any],
) -> None:
    """Write the terminal sprint audit, summary and RCA for a finished run.

    The inputs both writers key by slug or canonical ref — the ref→slug map, the
    canonical refs themselves, the tasks by slug, and each story's triage action
    — are derived here rather than at the call site. They are derived from the
    run context's resolved sprint, so the two writers cannot be handed mappings
    that disagree with each other or with the sprint that ran.

    ``sprint_log_dir`` is ``None`` when the run could not create its log
    directory; the summary and RCA are the artifacts that live in it, so they
    are skipped and the audit — which is written to the project root — is not.
    """
    ctx = state.context
    slug_map: dict[str, str] = ctx.slug_by_canonical_ref
    canonical_refs = [entry[2] for entry in ctx.slug_to_context.values()]
    tasks_by_slug = {slug: entry[0] for slug, entry in ctx.slug_to_context.items()}
    triage_actions_by_ref = {
        canonical_ref: triage.action for canonical_ref, triage in triages.items()
    }

    # sprint-audit.yaml (existing format; kept for backward compatibility)
    _write_sprint_audit(
        manifest=ctx.resolved,
        result=result,
        canonical_refs=canonical_refs,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        project_root=ctx.config.project_root,
        story_times=state.story_times,
        batch_assignments=state.batch_assignments,
        slug_map=slug_map,
        tasks_by_slug=tasks_by_slug,
        ci_break_slug=state.stop.halt_slug,
        sprint_id=ctx.sprint_id,
        dropped_slugs=dropped_slugs,
        skipped_issues=ctx.skipped_issues,
        current_story_entries_by_ref=state.current_story_entries_by_ref,
        triage_actions_by_ref=triage_actions_by_ref,
        run_id=ctx.run_id,
        live_telemetry_snapshots=state.live_telemetry_snapshots,
        story_state=state.stories,
    )

    if sprint_log_dir is None:
        return

    # sprint-summary.yaml to .forge/logs/<sprint-name>/
    _write_sprint_summary(
        manifest=ctx.resolved,
        result=result,
        canonical_refs=canonical_refs,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        sprint_log_dir=sprint_log_dir,
        story_times=state.story_times,
        batch_assignments=state.batch_assignments,
        slug_map=slug_map,
        run_id=ctx.run_id,
        tasks_by_slug=tasks_by_slug,
        ci_break_slug=state.stop.halt_slug,
        sprint_id=ctx.sprint_id,
        project_root=ctx.config.project_root,
        dropped_slugs=dropped_slugs,
        skipped_issues=ctx.skipped_issues,
        triage_actions_by_ref=triage_actions_by_ref,
        current_story_entries_by_ref=state.current_story_entries_by_ref,
        story_state=state.stories,
        config=ctx.config,
        live_telemetry_snapshots=state.live_telemetry_snapshots,
    )

    # Eagerly generate sprint-rca.yaml when any story finished non-DONE.
    # The RCA engine is a pure function over the artifacts just written
    # (sprint-summary.yaml + per-story audit/logs), so it runs off the
    # runner's hot path and stays regenerable via `forge rca`.
    try:
        from .rca import write_sprint_rca  # noqa: PLC0415

        rca_path = write_sprint_rca(sprint_log_dir)
        if rca_path is not None:
            _log(f"Sprint RCA written: {rca_path}")
    except Exception as rca_exc:  # noqa: BLE001 — RCA is best-effort
        _log(f"Warning: sprint RCA generation failed: {rca_exc}")


def publish_story_run_audits(
    state: Any,  # SprintExecutionState — see the module docstring on why not typed
    *,
    lands_locally: bool,
) -> None:
    """Publish this run's canonical audit JSON, reporting a failure loudly.

    The failure is logged with the recorded end state and re-raised rather than
    swallowed: a local-only audit commit contaminates every later story PR cut
    from this checkout, so the sprint must exit nonzero rather than report
    success over divergent base-branch state.
    """
    config = state.context.config
    try:
        publish_story_run_artifacts_for_config(config, lands_locally=lands_locally)
    except RuntimeError as exc:
        end_state = getattr(exc, "state", None)
        state_suffix = (
            f" [state={end_state}; recorded in {_STORY_RUN_AUDIT_PUBLISH_STATE_PATH}]"
            if end_state
            else ""
        )
        _log(f"✗ SPRINT  canonical story run audit publish failed: {exc}{state_suffix}")
        raise


def drain_project_memory_before_dispatch(state: Any) -> list[str]:
    """Clear a finished sibling's project memory out of the shared checkout.

    Called immediately before a story is admitted. The pass-level publish
    (:func:`publish_pending_story_run_audits`) runs once per scheduling pass,
    which leaves a window: a sibling can finish *during* a pass, between that
    publish and the dispatch of the next story in the same ``ready`` snapshot.
    The story then enters with the sibling's record in the shared checkout, and
    under a project-root landing workflow that is a refusal — of approved work,
    for a reason that has nothing to do with the story.

    Draining rather than publishing is what makes this affordable at this
    frequency: it is three ``git status`` probes and a file move, with no branch,
    commit or push. The staged content is published by the next pass-level
    publish or by the terminal sweep.

    Only for runs that do *not* land in the project root, which is the same
    condition that lets those runs publish without waiting for a quiet pass.
    A run that lands locally publishes by committing the working-tree copies, so
    draining them out from under that commit would hide the memory it is about
    to publish.

    Returns the paths drained, so a caller can say what it moved. Never raises:
    a story must not fail to dispatch because bookkeeping could not run.
    """
    from .memory_publication import stage_pending_project_memory  # noqa: PLC0415

    try:
        staged = stage_pending_project_memory(state.context.config.project_root)
    except Exception as exc:  # noqa: BLE001 — never blocks a dispatch
        _log(f"Warning: could not drain pending project memory before dispatch: {exc}")
        return []
    if staged.errors:
        _log(
            "⚠ SPRINT  some project memory could not be drained before dispatch: "
            + "; ".join(staged.errors)
        )
    return staged.paths


def publish_pending_story_run_audits(
    state: Any,  # SprintExecutionState — see the module docstring on why not typed
    *,
    lands_locally: bool,
) -> bool:
    """Publish whatever canonical run artifacts are already pending, mid-sprint.

    A sprint writes each story's run record and knowledge summary into the
    project-root checkout as that story finishes, and re-evaluates the landing
    precondition — which refuses on *any* project-root dirt, untracked files
    included — at every later story's entry. Publishing only at sprint exit
    therefore left a story's own artifacts standing across the window in which
    they would refuse its successor (#2595). This is the same publish, called
    while the run is still going, so publication keeps pace with enforcement.

    It differs from :func:`publish_story_run_audits` in exactly one way: a
    failure is reported and swallowed rather than raised. At sprint exit there
    is nothing left to lose by raising; here, raising would abandon the stories
    still to run over a transport problem that the terminal sweep — which does
    raise — will retry and, if it persists, report. Returns whether the publish
    completed.
    """
    try:
        publish_story_run_audits(state, lands_locally=lands_locally)
    except RuntimeError as exc:
        _log(
            "⚠ SPRINT  mid-sprint story run audit publish did not complete; deferring to "
            f"the terminal publish: {exc}"
        )
        return False
    return True
