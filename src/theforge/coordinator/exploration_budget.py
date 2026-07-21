"""Per-sprint exploration budget accounting (#325, ADR-0006 clause 8 "bounded").

Challenger sampling must fire at most a configured number of exploration runs
*per sprint* across all routing keys (default 1), not one-per-key unbounded. A
sprint spans multiple stories, each run in a freshly-created ``CoordinatorState``
(and potentially separate worker processes under ``--parallel``), so the counter
cannot live in process memory — it is a durable append-only ledger under the
sprint directory, mirroring the ``timeout_escalation_used`` sentinel pattern that
already fires-once-per-sprint across workers.

The ledger is a rebuildable audit artifact under ``.forge/`` (gitignored). Each
recorded exploration appends one JSON line; ``consumed_count`` is the line count.
For single-story runs with no sprint, there is no durable boundary, so the
caller falls back to disabling exploration (a lone story is not a sprint).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_LEDGER_NAME = "exploration_used.jsonl"


def ledger_path(project_root: Path, sprint_name: str) -> Path:
    """Return the durable per-sprint exploration ledger path."""
    return project_root / ".forge" / "sprints" / sprint_name / _LEDGER_NAME


def consumed_count(project_root: Path, sprint_name: str | None) -> int:
    """Return how many exploration runs the sprint has already consumed.

    Zero when there is no sprint boundary or the ledger does not yet exist. Best
    effort: a malformed/partial line still counts as a consumed slot (fail
    closed toward the bound rather than under-counting and over-exploring).
    """
    if not sprint_name:
        return 0
    path = ledger_path(project_root, sprint_name)
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def remaining_budget(
    project_root: Path, sprint_name: str | None, per_sprint_cap: int
) -> int | None:
    """Return the remaining exploration budget for the sprint.

    ``None`` signals "exploration not active" — either the cap is disabled
    (``<= 0``) or there is no sprint boundary to bound against — so the router
    records on-policy winner mode and never fires a challenger. Otherwise a
    non-negative remaining count (``max(0, cap - consumed)``).
    """
    if per_sprint_cap <= 0 or not sprint_name:
        return None
    return max(0, per_sprint_cap - consumed_count(project_root, sprint_name))


def record_exploration(project_root: Path, sprint_name: str | None, entry: dict) -> bool:
    """Append one consumed-exploration record to the durable sprint ledger.

    Returns True when a slot was recorded. No-op (returns False) when there is
    no sprint boundary. The write is append-only so concurrent sprint workers
    never clobber each other's counts.
    """
    if not sprint_name:
        return False
    path = ledger_path(project_root, sprint_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except OSError as exc:  # pragma: no cover - disk failure is best-effort
        log.warning("failed to record exploration budget consumption: %s", exc)
        return False
