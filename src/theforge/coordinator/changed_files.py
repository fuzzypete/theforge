"""Structured capture of the file set a run changed against its base ref.

A run's audit record has always said what the work *cost* and never what it
*touched*, so spend could not be attributed to code (issue #2347). The diffstat
the reviewer sees (:func:`review_context._get_diff_stat`) is a formatted string
built for a prompt; this module is its structured sibling — the same comparison,
resolved to concrete SHAs and per-file line counts, in a shape the audit record
and the SQLite index can carry.

Two properties make the snapshot usable after the fact:

* The comparison is named by **resolved SHAs**, not by the moving base-branch
  ref. ``main`` at audit time is not ``main`` at capture time, so a record that
  stored the branch name would describe a comparison nobody can reproduce.
* The snapshot is **captured while the worktree still exists** and stored on
  :class:`~theforge.coordinator.state.CoordinatorState`. Landing deletes the
  worktree and the branch; an audit writer that recomputed from disk would find
  nothing and record an empty set for exactly the runs that changed the most.

``None`` and ``{"files": []}`` are different claims and are kept apart
everywhere: ``None`` means no comparison could be made, an empty ``files`` list
means the comparison was made and nothing changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import util as _cu

if TYPE_CHECKING:
    from theforge.config import ForgeConfig

    from .state import CoordinatorState


def _resolve_ref(workspace_path: Path, rev: str) -> str | None:
    """Return the resolved commit SHA for ``rev``, or None when unresolvable."""
    ok, out = _cu._run_shell(f"git rev-parse --verify {rev}^{{commit}}", workspace_path)
    if not ok:
        return None
    sha = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        return sha
    return None


def _merge_base(workspace_path: Path, base_branch: str, head_ref: str) -> str | None:
    """Return the merge base of ``base_branch`` and ``head_ref``, or None."""
    ok, out = _cu._run_shell(f"git merge-base {base_branch} {head_ref}", workspace_path)
    if not ok:
        return None
    sha = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        return sha
    return None


def _parse_numstat(output: str) -> list[dict]:
    """Parse ``git diff --numstat`` output into per-file entries.

    Defensive by construction: ``_run_shell`` merges stderr into stdout, so a git
    warning would otherwise become a bogus file entry. Only lines that match the
    numstat triple exactly are accepted.

    Binary files are reported by git as ``-`` for both counts. They are recorded
    with zero insertions/deletions and ``binary: true`` — the flag is what keeps
    a changed binary distinguishable from a file with no line changes, which a
    bare ``0/0`` could not be.
    """
    files: list[dict] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        raw_ins, raw_del, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not path:
            continue
        binary = raw_ins == "-" and raw_del == "-"
        if binary:
            insertions = deletions = 0
        elif raw_ins.isdigit() and raw_del.isdigit():
            insertions, deletions = int(raw_ins), int(raw_del)
        else:
            continue
        files.append(
            {
                "path": path,
                "insertions": insertions,
                "deletions": deletions,
                "binary": binary,
            }
        )
    files.sort(key=lambda entry: entry["path"])
    return files


def collect_changed_files(
    workspace_path: Path,
    base_branch: str = "main",
    *,
    head: str = "HEAD",
) -> dict | None:
    """Return ``{base_ref, head_ref, files}`` for ``base_branch...head``, or None.

    Both refs are resolved to SHAs *before* the diff is taken, and the diff is
    taken between those SHAs, so the stored comparison is the one that ran and
    can be re-derived later from the record alone.

    Renames are decomposed into a delete and an add (``--no-renames``) so every
    entry names a real path — a ``{old => new}`` entry would not join against a
    file path in the index.

    Returns None when either ref cannot be resolved or the diff itself fails: a
    comparison that could not be made must not be recorded as one that found
    nothing.
    """
    if not workspace_path.exists():
        return None
    head_ref = _resolve_ref(workspace_path, head)
    if head_ref is None:
        return None
    base_ref = _merge_base(workspace_path, base_branch, head_ref)
    if base_ref is None:
        return None
    ok, out = _cu._run_shell(
        f"git -c core.quotePath=false diff --numstat --no-renames {base_ref} {head_ref}",
        workspace_path,
    )
    if not ok:
        return None
    return {"base_ref": base_ref, "head_ref": head_ref, "files": _parse_numstat(out)}


def collect_commit_files(workspace_path: Path, revs: list[str]) -> dict | None:
    """Return ``{commits, files}`` for the union of ``revs``, or None.

    The per-commit counterpart of :func:`collect_changed_files`, for the case
    where a branch carries more than one story's work and only some of its
    commits belong to the story being asked about (a cost-aware batch group's
    shared worktree, #2525).

    ``None`` on any unresolvable rev or failed diff, and on an empty ``revs``,
    holding the same distinction the module keeps everywhere: a comparison that
    could not be made is not a comparison that found nothing. A caller grounding
    findings against the result must treat ``None`` as "this story's file set is
    unknown", never as "this story changed nothing".
    """
    if not revs or not workspace_path.exists():
        return None
    resolved: list[str] = []
    for rev in revs:
        sha = _resolve_ref(workspace_path, rev)
        if sha is None:
            return None
        resolved.append(sha)
    paths: dict[str, dict] = {}
    for sha in resolved:
        # --format= suppresses the commit header so only numstat rows remain;
        # _parse_numstat ignores anything that is not a numstat triple anyway.
        ok, out = _cu._run_shell(
            f"git -c core.quotePath=false show --numstat --no-renames --format= {sha}",
            workspace_path,
        )
        if not ok:
            return None
        for entry in _parse_numstat(out):
            paths.setdefault(entry["path"], entry)
    return {
        "commits": resolved,
        "files": sorted(paths.values(), key=lambda entry: entry["path"]),
    }


def capture_changed_files(
    state: CoordinatorState,
    config: ForgeConfig,
    workspace_path: Path | None = None,
) -> dict | None:
    """Capture the run's changed-file set onto ``state`` and return it.

    Call this at any seam that is about to destroy the evidence — landing runs
    ``git worktree remove`` and deletes the branch, after which the comparison
    is gone. Recapture semantics: a later call replaces an earlier snapshot,
    because the last capture before cleanup is the one that describes the whole
    run (the pre-merge auto-commit lands files no earlier capture had seen).

    A failed capture never clobbers a good one — a worktree that has already
    been removed leaves the snapshot taken while it existed in place.
    """
    path = workspace_path or state.workspace_path
    if path is None:
        return state.changed_files
    snapshot = collect_changed_files(Path(path), config.workspace.base_branch)
    if snapshot is not None:
        state.changed_files = snapshot
    return state.changed_files


def resolve_changed_files(state: CoordinatorState, config: ForgeConfig) -> dict | None:
    """Return the changed-file block for the audit record.

    Prefers the snapshot captured at the pre-cleanup seam. Falls back to a
    *transient* collection from the workspace when no snapshot was stored — the
    path that covers runs terminating before review (escalated and failed runs
    keep their worktree, and those are the runs most worth attributing).

    Deliberately does not store the fallback: the in-flight audit flush also
    calls this repeatedly while the story is still running, and freezing its
    first mid-run answer onto the state would make the final record describe an
    earlier moment than the one it claims to.
    """
    if state.changed_files is not None:
        return state.changed_files
    path = state.workspace_path
    if path is None:
        return None
    return collect_changed_files(Path(path), config.workspace.base_branch)
