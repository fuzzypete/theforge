"""Per-sprint exploration budget accounting (#325, ADR-0006 clause 8 "bounded").

Challenger sampling must fire at most a configured number of exploration runs
*per sprint* across all routing keys (default 1), not one-per-key unbounded. A
sprint spans multiple stories, each run in a freshly-created ``CoordinatorState``
(and, under ``--parallel``, separate worker processes), so the counter cannot
live in process memory.

Reads and writes must also be **atomic across workers**: a read-count-then-append
scheme has a TOCTOU race where two workers both observe one free slot and both
consume it, blowing a cap of one. Instead a slot is claimed with
``os.O_CREAT | os.O_EXCL`` on a per-index sentinel file — the same fire-once
primitive ``engine.py`` already uses for ``timeout_escalation_used``. Only one
worker can create ``slot_<i>``; a loser falls through to the next index, and when
all ``cap`` indices exist the reservation fails and the caller routes on-policy.

The slot dir is a rebuildable audit artifact under ``.forge/`` (gitignored); each
claimed slot file holds the JSON record of the exploration that consumed it. For
single-story runs with no sprint there is no durable boundary, so the caller
disables exploration (a lone story is not a sprint).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_SLOT_DIRNAME = "exploration_slots"
_SLOT_PREFIX = "slot_"


def slot_dir(project_root: Path, sprint_name: str) -> Path:
    """Return the durable per-sprint exploration slot directory."""
    return project_root / ".forge" / "sprints" / sprint_name / _SLOT_DIRNAME


def consumed_count(project_root: Path, sprint_name: str | None) -> int:
    """Return how many exploration slots the sprint has already claimed.

    Zero when there is no sprint boundary or no slot has been claimed yet.
    """
    if not sprint_name:
        return 0
    directory = slot_dir(project_root, sprint_name)
    if not directory.exists():
        return 0
    try:
        return sum(1 for p in directory.iterdir() if p.name.startswith(_SLOT_PREFIX))
    except OSError:
        return 0


def remaining_budget(
    project_root: Path, sprint_name: str | None, per_sprint_cap: int
) -> int | None:
    """Return the remaining exploration budget for the sprint (advisory).

    ``None`` signals "exploration not active" — either the cap is disabled
    (``<= 0``) or there is no sprint boundary to bound against — so the router
    records on-policy winner mode and never fires a challenger. Otherwise a
    non-negative remaining count (``max(0, cap - consumed)``).

    This is an *advisory* read used to avoid deciding a challenger when the cap is
    already exhausted; the authoritative, race-free consume is
    :func:`reserve_slot`, which the caller must still honor before routing a
    challenger.
    """
    if per_sprint_cap <= 0 or not sprint_name:
        return None
    return max(0, per_sprint_cap - consumed_count(project_root, sprint_name))


def reserve_slot(
    project_root: Path, sprint_name: str | None, per_sprint_cap: int, entry: dict
) -> bool:
    """Atomically claim one exploration slot for the sprint; record ``entry`` in it.

    Returns ``True`` iff a slot in ``[0, per_sprint_cap)`` was claimed by THIS
    call — the authoritative signal that this worker may fire its challenger.
    Returns ``False`` when there is no sprint boundary, the cap is disabled, or
    every slot index is already claimed (cap reached / lost the race). The claim
    is a single ``O_CREAT | O_EXCL`` create per index, so concurrent workers can
    never both succeed on the same index and the cap holds exactly.
    """
    if not sprint_name or per_sprint_cap <= 0:
        return False
    directory = slot_dir(project_root, sprint_name)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - disk failure is best-effort
        log.warning("failed to create exploration slot dir: %s", exc)
        return False
    payload = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    for i in range(per_sprint_cap):
        slot = directory / f"{_SLOT_PREFIX}{i}"
        try:
            fd = os.open(str(slot), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue  # someone else owns this index — try the next one
        except OSError as exc:  # pragma: no cover - disk failure is best-effort
            log.warning("failed to claim exploration slot %d: %s", i, exc)
            return False
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    return False  # all cap slots already claimed
