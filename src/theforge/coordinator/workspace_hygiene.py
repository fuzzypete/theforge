"""Workspace hygiene gate.

The coordinator owns deterministic phase boundaries. Some phases (PREFLIGHT,
PLAN, PLAN_REVIEW, REVIEW) must not mutate the repo tree; others (DEV) own
mutation. This module provides the mechanical enforcement: snapshot the tree
before a phase, compare after, and either quarantine stray untracked files or
fail loudly with the offending paths.

Three layers:

1. ``check_phase_no_mutation``: invoked around PLAN / PLAN_REVIEW. Any change
   to the porcelain set fails the phase. Paths under forge's own runtime
   directory (``.forge/``) are excluded mechanically by ``snapshot_porcelain``
   — regardless of the consumer repo's gitignore state — so coordinator-driven
   artifact writes never register as mutations.

2. ``enforce_pre_dev_hygiene`` / ``enforce_pre_review_hygiene``: invoked
   symmetrically before DEV iteration 1 and before each REVIEW cycle.
   Unexpected untracked paths are moved to ``.forge/quarantine/<run-id>/``
   (audited, not silently deleted) and the phase proceeds with a clean tree.
   If quarantine fails, the phase is rejected with a diagnostic pointing at
   the offending paths instead of being handed to an agent that will fail
   mysteriously. REVIEW symmetry exists because stray pollution from a prior
   interrupted iteration would otherwise be visible to reviewers (see issue
   #1501).

3. ``reconcile_post_review_mutations``: invoked AFTER the REVIEW pool returns.
   Reviewer agents may improvise in the worktree (e.g. shell redirects writing
   ``test_script.sh``, or accidentally editing a tracked file while exercising
   the change). All reviewer-side mutations are mechanically reconciled: new
   untracked paths are auto-quarantined; tracked-file mutations (modifications,
   deletions, staged adds) are reverted back to HEAD via ``git reset`` +
   ``git checkout HEAD --``. This way no reviewer-side filesystem effect can
   invalidate an otherwise-sound review verdict (see issue #1497). Escalation
   is reserved for the failure case where revert or quarantine itself fails
   (filesystem error), so the consensus-capture / resume-replay machinery
   (issue #1499) still has a real failsafe to ride on.

Sanctioned scratch space (``.forge/tmp/<run-id>/``) is provisioned by
``ensure_scratch_dir``; it lives under ``.forge/``, which the snapshot
mechanically excludes, so the directory never registers with the gate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Paths agents may legitimately leave at root without tripping the gate.
# Restrict to operator-facing work artifacts; everything else is quarantined.
_ALLOWED_ROOT_UNTRACKED: frozenset[str] = frozenset()

# Forge's own runtime directory. Everything under it is coordinator-written
# bookkeeping (quarantine dir, escalation markers, scratch, tmp) — never
# operator contamination. Historically the gate relied on the consumer repo
# gitignoring ``.forge/`` to keep these out of its porcelain snapshots, but
# that assumption was unenforced: a repo that only ignores the specific
# ``.forge/`` files known to an older forge version (or none at all) exposed
# forge's own output as untracked "contamination", tripping quarantine and
# escalating on artifacts forge itself wrote (hdp #183, TheForge #1699). The
# exclusion is now mechanical and independent of gitignore state.
_FORGE_RUNTIME_DIR = ".forge"
_FORGE_RUNTIME_PREFIX = ".forge/"


def _is_forge_runtime_path(path: str) -> bool:
    """True if ``path`` (workspace-relative) is under forge's runtime dir.

    Matches the directory itself and anything beneath it, so ``.forge``,
    ``.forge/quarantine`` and ``.forge/escalated`` are all recognised as
    forge-owned regardless of the consumer repo's gitignore contents.
    """
    return path == _FORGE_RUNTIME_DIR or path.startswith(_FORGE_RUNTIME_PREFIX)


def snapshot_porcelain(workspace_path: Path) -> set[str]:
    """Return the porcelain entry set for ``workspace_path``.

    Each entry is the raw two-character status code plus the path
    (``"?? foo.py"``, ``" M src/x.py"``, ...). Entries under forge's own
    runtime directory (``.forge/``) are filtered out mechanically — they are
    coordinator-written artifacts, never operator contamination — so the gate
    is correct even in a consumer repo that does not gitignore ``.forge/``. On
    git failure returns an empty set; callers treat that as "no snapshot"
    rather than crashing.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    raw = proc.stdout.decode("utf-8", errors="replace")
    if not raw:
        return set()
    # -z entries are NUL-terminated; renames split across two NUL fields.
    return {
        entry for entry in raw.split("\0") if entry and not _is_forge_runtime_path(_path_of(entry))
    }


def _path_of(entry: str) -> str:
    """Extract the path portion of a porcelain entry (status is 3 chars)."""
    return entry[3:] if len(entry) > 3 else entry


def unexpected_entries(before: set[str], after: set[str]) -> list[str]:
    """Return entries present in ``after`` but not in ``before``, sorted by path."""
    new = after - before
    return sorted(new, key=_path_of)


def check_phase_no_mutation(
    workspace_path: Path,
    before: set[str],
    phase_name: str,
) -> tuple[bool, str | None, list[str]]:
    """Verify ``phase_name`` left the tree unchanged.

    Returns ``(ok, diagnostic, offending_paths)``. When ``ok`` is False, the
    diagnostic names the phase and lists every path the phase introduced or
    modified — operator-grade language, no silent failures.
    """
    after = snapshot_porcelain(workspace_path)
    new_entries = unexpected_entries(before, after)
    if not new_entries:
        return True, None, []
    paths = [_path_of(e) for e in new_entries]
    rendered = ", ".join(paths)
    diagnostic = (
        f"Workspace hygiene violation: phase {phase_name} is not authorized to mutate "
        f"the repo tree, but introduced unexpected paths: {rendered}. "
        "Remove or justify."
    )
    return False, diagnostic, paths


def ensure_scratch_dir(workspace_path: Path, run_id: str) -> Path:
    """Create ``.forge/tmp/<run-id>/`` and return it.

    The directory lives under ``.forge/``, which ``snapshot_porcelain``
    excludes mechanically, so it is safe for agents to use as exploratory
    scratch space without tripping the hygiene gate.
    """
    scratch = workspace_path / ".forge" / "tmp" / run_id
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _quarantine_root(workspace_path: Path, run_id: str, label: str) -> Path:
    return workspace_path / ".forge" / "quarantine" / run_id / label


def quarantine_paths(
    workspace_path: Path,
    paths: list[str],
    quarantine_root: Path,
) -> tuple[list[str], list[str]]:
    """Move ``paths`` (workspace-relative) under ``quarantine_root``.

    Returns ``(moved, failed)``. Moves preserve the relative tree so the
    operator can locate originals. ``shutil.move`` is used so cross-device
    moves degrade to copy+delete cleanly.

    Paths under forge's own runtime directory (``.forge/``) are skipped
    outright: the quarantine root itself lives at ``.forge/quarantine/...``, so
    remediation must never target its own bookkeeping location. Moving
    ``.forge/quarantine`` into a subdirectory of itself is a move-into-own-
    subtree ``OSError`` that would otherwise surface as a spurious quarantine
    failure and escalate the phase (hdp #183, TheForge #1699). Such paths are
    reported as neither moved nor failed — there is nothing to remediate.
    """
    quarantine_root.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    failed: list[str] = []
    for rel_path in paths:
        if _is_forge_runtime_path(rel_path):
            # Never remediate forge's own runtime artifacts (see docstring).
            continue
        src = workspace_path / rel_path
        if not src.exists() and not src.is_symlink():
            # Path appeared in porcelain but is gone now (race or untracked dir
            # that already lost its entries). Treat as moved — no work to do.
            moved.append(rel_path)
            continue
        dest = quarantine_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            moved.append(rel_path)
        except OSError:
            failed.append(rel_path)
    return moved, failed


def _enforce_phase_entry_hygiene(
    workspace_path: Path,
    run_id: str,
    *,
    label: str,
    phase_label: str,
) -> tuple[bool, str | None, dict]:
    """Quarantine unexpected untracked paths before a phase starts.

    Behaviour:
      - Untracked paths (status ``??``) outside the allow-list → quarantined
        to ``.forge/quarantine/<run-id>/<label>/``. Audit-trail preserved,
        worktree returned to a clean baseline, the phase proceeds. This is the
        primary motivator: stray scratch at repo root that silently sabotages
        agent runs (issues #1179, #1501).
      - Modified tracked files (status M/A/D/...) → audited and left in
        place. These are legitimate worktree-reuse state (work-in-progress
        from a prior interrupted run); validate-phase's auto-commit owns
        cleanup. Silent quarantine of tracked changes would qualify as
        "forge ate my work".
      - If quarantine fails for any untracked path → reject with the
        offending paths so the operator knows what to clean up manually.

    Returns ``(ok, diagnostic, audit)`` where ``audit`` is always a dict with
    keys ``snapshot``, ``modified``, ``quarantined``, ``quarantine_dir``.
    """
    snapshot = snapshot_porcelain(workspace_path)
    audit: dict = {
        "snapshot": sorted(snapshot),
        "modified": [],
        "quarantined": [],
        "quarantine_dir": None,
    }
    if not snapshot:
        return True, None, audit

    untracked: list[str] = []
    modified: list[str] = []
    for entry in snapshot:
        # Porcelain format: XY␠path. "??" = untracked. Anything else = tracked
        # change (modified, added, deleted, renamed, copied, unmerged).
        status = entry[:2]
        path = _path_of(entry)
        if status == "??":
            if path in _ALLOWED_ROOT_UNTRACKED:
                continue
            untracked.append(path)
        else:
            modified.append(path)

    audit["modified"] = sorted(modified)

    if not untracked:
        return True, None, audit

    quarantine_dir = _quarantine_root(workspace_path, run_id, label)
    moved, failed = quarantine_paths(workspace_path, sorted(untracked), quarantine_dir)
    audit["quarantined"] = moved
    audit["quarantine_dir"] = str(quarantine_dir.relative_to(workspace_path))
    if failed:
        rendered = ", ".join(failed)
        diagnostic = (
            f"Unexpected untracked files in worktree before {phase_label} could not be "
            f"quarantined: {rendered}. Workspace hygiene gate refuses to hand a "
            f"dirty tree to the {phase_label.lower()} agent. Resolve manually before retrying."
        )
        return False, diagnostic, audit

    return True, None, audit


def enforce_pre_dev_hygiene(
    workspace_path: Path,
    run_id: str,
    *,
    iteration: int,
) -> tuple[bool, str | None, dict]:
    """Reject or sanitise the worktree before DEV starts (see #1179)."""
    return _enforce_phase_entry_hygiene(
        workspace_path,
        run_id,
        label=f"iter-{iteration}",
        phase_label="DEV",
    )


def _attempt_tracked_revert(workspace_path: Path, rel_path: str) -> None:
    """Best-effort: restore ``rel_path`` to HEAD content via reset + checkout.

    Subprocess return codes are intentionally ignored here — the caller
    verifies success by re-reading the porcelain snapshot, not by trusting
    these calls. ``git reset/checkout`` may legitimately exit nonzero (e.g.
    path not in HEAD for a newly staged add, or .git/index.lock blocks the
    operation); the snapshot is the source of truth.
    """
    for cmd in (
        ["git", "reset", "-q", "HEAD", "--", rel_path],
        ["git", "checkout", "-q", "HEAD", "--", rel_path],
    ):
        try:
            subprocess.run(
                cmd,
                cwd=str(workspace_path),
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # Snapshot will catch unresolved state below.
            continue


def reconcile_post_review_mutations(
    workspace_path: Path,
    before: set[str],
    run_id: str,
    cycle: int,
) -> tuple[bool, str | None, list[str], dict]:
    """Mechanically neutralize reviewer-side worktree mutations after REVIEW.

    Issue #1497: REVIEW-phase reviewers occasionally improvise in the worktree
    (e.g. ``cat <<EOF > test_script.sh``, or editing a tracked file while
    exercising the change). No reviewer-side filesystem effect must invalidate
    an otherwise-sound review verdict.

    For each new porcelain entry since ``before``:
      - Untracked paths (status ``??``) → quarantined to
        ``.forge/quarantine/<run-id>/review-<cycle>-post/`` symmetrically
        with pre-phase hygiene.
      - Tracked changes (M/D/A/staged-anything) → best-effort
        ``git reset HEAD -- <path>`` + ``git checkout HEAD -- <path>``,
        then VERIFIED via a follow-up porcelain snapshot. Subprocess
        return codes are not trusted; the snapshot is the source of truth.
        Any path still showing tracked status after the revert attempt is
        treated as a revert failure (e.g. .git/index.lock present, real
        filesystem error) and triggers escalation. Newly-staged-adds, after
        reset, surface as untracked and fold into the quarantine pass.

    Returns ``(ok, diagnostic, offending_paths, audit)``. ``ok=False`` is
    reserved for the failure case where revert or quarantine cannot resolve
    the worktree to a clean state; that path still triggers the existing
    consensus-capture + resume-replay escalation block in review_phase.

    Audit dict keys: ``snapshot_after``, ``quarantined``, ``quarantine_dir``,
    ``tracked_changes``, ``reverted``.
    """
    after = snapshot_porcelain(workspace_path)
    new_entries = unexpected_entries(before, after)
    audit: dict = {
        "snapshot_after": sorted(after),
        "quarantined": [],
        "quarantine_dir": None,
        "tracked_changes": [],
        "reverted": [],
    }
    if not new_entries:
        return True, None, [], audit

    untracked_initial: list[str] = []
    tracked_initial: list[str] = []
    for entry in new_entries:
        path = _path_of(entry)
        if entry[:2] == "??":
            untracked_initial.append(path)
        else:
            tracked_initial.append(path)
    audit["tracked_changes"] = sorted(tracked_initial)

    untracked_all: list[str] = list(untracked_initial)

    if tracked_initial:
        for path in sorted(tracked_initial):
            _attempt_tracked_revert(workspace_path, path)

        # Truth comes from re-snapshotting, not from subprocess return codes.
        # Anything still showing tracked status is an unresolved revert
        # (real filesystem/git failure — e.g. index.lock); newly untracked
        # entries are staged-add residue we can quarantine.
        post_revert = snapshot_porcelain(workspace_path)
        post_revert_new = unexpected_entries(before, post_revert)
        still_tracked: list[str] = []
        residual_untracked: list[str] = []
        for entry in post_revert_new:
            p = _path_of(entry)
            if entry[:2] == "??":
                residual_untracked.append(p)
            else:
                still_tracked.append(p)

        if still_tracked:
            rendered = ", ".join(sorted(still_tracked))
            diagnostic = (
                f"Reviewer-introduced tracked changes could not be reverted "
                f"after REVIEW: {rendered}. Workspace hygiene gate refuses to "
                "treat the review as valid against an unresolved dirty tree."
            )
            return False, diagnostic, sorted(still_tracked), audit

        audit["reverted"] = sorted(tracked_initial)
        for p in residual_untracked:
            if p not in untracked_all:
                untracked_all.append(p)

    if not untracked_all:
        return True, None, [], audit

    quarantine_dir = _quarantine_root(workspace_path, run_id, f"review-{cycle}-post")
    moved, failed = quarantine_paths(workspace_path, sorted(untracked_all), quarantine_dir)
    audit["quarantined"] = moved
    audit["quarantine_dir"] = str(quarantine_dir.relative_to(workspace_path))
    if failed:
        rendered = ", ".join(failed)
        diagnostic = (
            f"Reviewer-introduced untracked paths could not be quarantined "
            f"after REVIEW: {rendered}. Workspace hygiene gate refuses to "
            "treat the review as valid against a still-dirty tree."
        )
        return False, diagnostic, failed, audit

    return True, None, [], audit


def enforce_pre_review_hygiene(
    workspace_path: Path,
    run_id: str,
    *,
    cycle: int,
) -> tuple[bool, str | None, dict]:
    """Reject or sanitise the worktree before REVIEW starts (see #1501).

    Symmetric counterpart to ``enforce_pre_dev_hygiene``: stray untracked
    paths inherited from a prior interrupted iteration, resume, or a
    non-DEV phase that wrote where it shouldn't are quarantined before any
    reviewer observes the tree, so reviewers evaluate the implementation
    rather than stale pollution.
    """
    return _enforce_phase_entry_hygiene(
        workspace_path,
        run_id,
        label=f"review-{cycle}",
        phase_label="REVIEW",
    )
