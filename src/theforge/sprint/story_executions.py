"""Stories this sprint process is executing right now, written down.

A mid-run re-exec (``os.execv`` after ``workspace.pull_base_branch`` observes new
source) replaces the process *image* but keeps the process *identity*. Agent
subprocess groups spawned before that moment survive it and are recognised on
the far side through their sidecars (:mod:`theforge.sprint.live_stories`). A
story executing in *pure Python* at that instant — VALIDATE's post-gate
bookkeeping, a phase transition, the commit that follows an approved review —
leaves no such trace: its worker thread is annihilated by ``execv``, and the
worktree it was writing to surfaces afterwards indistinguishable from a foreign
run's leftovers. That is how a gate-green, review-approved story was classified
as an ``active-worktree-collision`` and dropped (#2617).

So ownership is recorded rather than inferred. Before a story is dispatched, this
module writes ``.forge/runs/stories/{owner_pid}-{slug}.json`` naming the slug,
its worktree, the owning pid, and that pid's *start time*. The record is cleared
only once the sprint's scheduler has consumed the worker's future and settled the
story's terminal outcome — deliberately later than the worker's own return, since
a re-exec in between would otherwise destroy the only durable proof of ownership.

Identity is a start time, never an id, exactly as in :mod:`theforge.process_group`:
a pid is recycled, so a record naming one describes a past moment. The pid *and*
the start time survive ``execv`` together, which is precisely what makes the pair
able to say "this is the same run, continuing" and unable to say it about anyone
else. A record whose fingerprint disagrees with the process now holding that pid
is a dead run's leftover and claims nothing.

Stdlib-only apart from :mod:`theforge.process_tree` (itself stdlib-only), per
convention 4.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from theforge import process_tree

__all__ = [
    "StoryExecutionRecord",
    "StoryExecutionScan",
    "clear_story_execution",
    "current_process_fingerprint",
    "executions_dir",
    "register_story_execution",
    "scan_story_executions",
    "sweep_story_executions",
]


@dataclass(frozen=True)
class StoryExecutionRecord:
    """One story a sprint process had dispatched but not yet settled."""

    slug: str
    owner_pid: int
    owner_fingerprint: str | None
    worktree: str | None
    run_id: str | None
    path: Path


@dataclass(frozen=True)
class StoryExecutionScan:
    """What a registry read established, including what it could not.

    ``owned`` are records this very process wrote and has not cleared —
    confirmed in-flight work of this run. ``unverifiable`` are records claiming
    this pid whose start-time fingerprint could not be compared (a platform that
    will not describe a process, a record written without one): they may be ours
    and may be a dead run's, so they resolve to *unknown* rather than to either
    answer. ``failures`` describe records or listings that could not be read at
    all; like an unreadable agent sidecar, they taint rather than resolve.
    """

    owned: tuple[StoryExecutionRecord, ...] = ()
    unverifiable: tuple[StoryExecutionRecord, ...] = ()
    failures: tuple[str, ...] = ()


def executions_dir(project_root: Path) -> Path:
    """Where this run's story-execution records live."""
    return Path(project_root) / ".forge" / "runs" / "stories"


def current_process_fingerprint() -> str | None:
    """This process's start time, or None where the platform will not say.

    ``os.execv`` does not restart the process, so this value is identical either
    side of a re-exec — which is what lets a record written before one still be
    recognised as this run's own after it.
    """
    info = process_tree.process_info(os.getpid())
    return None if info is None else info.fingerprint


def _record_name(owner_pid: int, slug: str) -> str:
    # Slugs are issue/story identifiers, but the filename must not be able to
    # escape the registry directory whatever a custom slug contains.
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in slug)
    return f"{owner_pid}-{safe}.json"


def _record_path(project_root: Path, slug: str, owner_pid: int) -> Path:
    return executions_dir(project_root) / _record_name(owner_pid, slug)


def register_story_execution(
    slug: str,
    *,
    project_root: Path,
    worktree: str | Path | None = None,
    run_id: str | None = None,
    owner_pid: int | None = None,
) -> Path:
    """Record that this process now owns *slug*'s execution; return the path.

    Raises on any failure to write. That is deliberate and the caller depends on
    it: dispatching a story whose ownership could not be recorded is dispatching
    work that a re-exec would then be free to classify as a foreign collision and
    drop — the exact loss this registry exists to prevent. Refusing to launch
    costs one story an infrastructure failure; launching unowned risks the whole
    of what that story is about to spend.
    """
    pid = os.getpid() if owner_pid is None else int(owner_pid)
    payload = {
        "slug": slug,
        "owner_pid": pid,
        "owner_fingerprint": current_process_fingerprint(),
        "worktree": str(worktree) if worktree is not None else None,
        "run_id": run_id,
    }
    path = _record_path(Path(project_root), slug, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_record(path, payload)
    return path


def clear_story_execution(slug: str, *, project_root: Path, owner_pid: int | None = None) -> None:
    """Drop this process's ownership record for *slug*.

    Best-effort: a record that cannot be removed makes a later launch defer a
    finished story, which the prior-outcome reconciliation then settles — a far
    cheaper failure than the removal being allowed to disturb the run.
    """
    pid = os.getpid() if owner_pid is None else int(owner_pid)
    try:
        _record_path(Path(project_root), slug, pid).unlink()
    except OSError:
        pass


def _write_record(path: Path, payload: dict[str, object]) -> None:
    """Atomically replace a record in-place.

    Readers treat a malformed record as unresolved ownership, so that failure
    mode stays reserved for genuinely broken records rather than our own partial
    writes — the same reason the agent sidecars are written this way.
    """
    encoded = json.dumps(payload)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        Path(tmp_name).replace(path)
    except Exception:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _parse(path: Path) -> StoryExecutionRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    slug = data.get("slug")
    owner_pid = data.get("owner_pid")
    if not isinstance(slug, str) or not slug or not isinstance(owner_pid, int):
        return None
    fingerprint = data.get("owner_fingerprint")
    worktree = data.get("worktree")
    run_id = data.get("run_id")
    return StoryExecutionRecord(
        slug=slug,
        owner_pid=owner_pid,
        owner_fingerprint=fingerprint if isinstance(fingerprint, str) and fingerprint else None,
        worktree=worktree if isinstance(worktree, str) and worktree else None,
        run_id=run_id if isinstance(run_id, str) and run_id else None,
        path=path,
    )


def _list_records(project_root: Path) -> tuple[list[Path], str | None]:
    """Record paths, plus a failure description when they could not be listed."""
    directory = executions_dir(project_root)
    if not directory.exists():
        return [], None
    try:
        return sorted(directory.glob("*.json")), None
    except OSError as exc:
        return [], f"story execution registry {directory} could not be listed: {exc}"


def scan_story_executions(
    project_root: Path,
    *,
    owner_pid: int | None = None,
    owner_fingerprint: str | None = None,
) -> StoryExecutionScan:
    """Read the registry, split by whether each record is provably this run's.

    A record is *owned* only when it names this pid **and** carries the start
    time this process actually has. Both halves are load-bearing: the pid alone
    is recycled, and the fingerprint alone belongs to no particular run. A record
    naming another pid is another run's business and is neither claimed nor
    reported — the launch guard's other checks already cover a foreign worktree.
    """
    pid = os.getpid() if owner_pid is None else int(owner_pid)
    fingerprint = current_process_fingerprint() if owner_fingerprint is None else owner_fingerprint
    paths, listing_failure = _list_records(Path(project_root))
    failures: list[str] = []
    if listing_failure is not None:
        failures.append(listing_failure)

    owned: list[StoryExecutionRecord] = []
    unverifiable: list[StoryExecutionRecord] = []
    for path in paths:
        record = _parse(path)
        if record is None:
            failures.append(f"story execution record {path.name} is unreadable")
            continue
        if record.owner_pid != pid:
            continue
        if fingerprint is None or record.owner_fingerprint is None:
            # One side of the identity is missing, so "same run" can be neither
            # established nor denied. Saying "not ours" here would hand a live
            # story back to the collision branch on an unanswered question.
            unverifiable.append(record)
            continue
        if record.owner_fingerprint == fingerprint:
            owned.append(record)
        # A fingerprint that disagrees is a dead run whose pid we now hold. It
        # claims nothing, and it is not uncertainty either.
    return StoryExecutionScan(
        owned=tuple(owned), unverifiable=tuple(unverifiable), failures=tuple(failures)
    )


def sweep_story_executions(project_root: Path, *, exclude_slugs: Iterable[str] = ()) -> list[Path]:
    """Delete records left by runs that are gone; return what was removed.

    A record whose owning process is no longer alive — or whose pid is now held
    by a different process — describes work nothing is doing. Left in place it
    would accumulate and, once a pid was recycled onto a live sprint, start
    vouching for a worktree that sprint never touched. Records owned by this
    process are never swept, and neither is anything named in *exclude_slugs*.
    """
    from theforge.detach import _is_pid_alive  # noqa: PLC0415

    protected = {s for s in exclude_slugs if s}
    my_pid = os.getpid()
    my_fingerprint = current_process_fingerprint()
    paths, _failure = _list_records(Path(project_root))
    removed: list[Path] = []
    for path in paths:
        record = _parse(path)
        if record is not None and record.slug in protected:
            continue
        if record is not None and record.owner_pid == my_pid:
            if my_fingerprint is None or record.owner_fingerprint is None:
                continue
            if record.owner_fingerprint == my_fingerprint:
                continue
        elif record is not None and _is_pid_alive(record.owner_pid):
            info = process_tree.process_info(record.owner_pid)
            # A live owner keeps its record unless the pid demonstrably belongs
            # to something else now. Unreadable start times leave it alone: the
            # cost of a stale record is a deferral, the cost of deleting a live
            # one is the drop this module exists to prevent.
            if (
                info is None
                or record.owner_fingerprint is None
                or info.fingerprint == record.owner_fingerprint
            ):
                continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed
