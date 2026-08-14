"""What an issue cost across every run that worked on it.

Every cost figure an operator sees is scoped to a run. The per-run records sum
correctly, but nothing sums them: an issue that fails, is preserved and is
re-entered accrues spend across several runs, and the figure that should decide
whether to re-run it — what the issue has cost so far — existed only by querying
the audit substrate by hand (#2365).

This module is the one place that aggregation happens. It is deliberately split
in two:

* :func:`aggregate_issue_cost` is pure over already-loaded run records, so the
  grouping, the de-duplication of carried-forward spend and the unmeasured flag
  are testable without a substrate.
* :func:`load_issue_cost` is the best-effort I/O wrapper every display surface
  calls. It never raises: a missing, unreadable or empty substrate yields
  ``None``, so a status view still renders when the index is not there.

Two rules the aggregate exists to get right:

**Each run's spend is counted once.** A run that is dropped mid-flight and
re-executed within the same sprint has its work folded into the successor's
record by :func:`theforge.sprint.audit.carry_prior_generation_work`. When the
dropped generation never wrote a record of its own, the successor carries its
cost — and if both records are nonetheless present, summing the column would
count those dollars twice. A record that names a ``parent_run_id`` it did not
leave independently recorded therefore *subsumes* that parent, and the parent
is excluded from both the total and the attempt count.

**An unmeasured contributor is disclosed, not absorbed.** A run whose transport
reported no cost records ``cost.total_usd`` as ``None`` (#1992). One such run
makes the issue total a lower bound, and the aggregate says so rather than
presenting a partial sum as a complete one.

Stdlib-only: the substrate import is deferred into :func:`load_issue_cost`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_ISSUE_REF_RE = re.compile(r"(?:issue-|#)(\d+)")


def issue_number_from_slug(text: object) -> int | None:
    """Extract N from an ``issue-N`` / ``#N`` token, else ``None``.

    The single normalization every caller shares. Story slugs, digest rows and
    pending-decision entries each name an issue in their own shape, and a
    grouping rule that differed between them would under- or over-group the
    very runs the aggregate exists to add up.
    """
    if not text:
        return None
    match = _ISSUE_REF_RE.search(str(text))
    return int(match.group(1)) if match else None


def _measured_cost(record: dict) -> float | None:
    """This run's own spend, or ``None`` when it went unmeasured.

    Field precedence matches :func:`theforge.coordinator.audit_substrate._flat_fields`
    so the aggregate and the indexed ``total_cost_usd`` column never disagree.
    """
    for block, key in (("totals", "cost_usd"), ("cost", "total_usd")):
        section = record.get(block)
        if not isinstance(section, dict):
            continue
        value = section.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    # No block carried a number. Either the run recorded a null cost — it ran on
    # a transport that reported none (#1992) — or it recorded no cost at all.
    # Both are spend this aggregate cannot vouch for, and neither is zero.
    return None


def _subsumed_parent(record: dict) -> str | None:
    """The run id whose spend this record already carries, if any."""
    prior = record.get("prior_generation")
    if not isinstance(prior, dict):
        return None
    if prior.get("independently_recorded"):
        # The parent reported its own dollars; this record did not restate them.
        return None
    parent = record.get("parent_run_id") or prior.get("run_id")
    return parent if isinstance(parent, str) and parent else None


def _outcome_label(record: dict) -> str:
    """A short, operator-legible account of how one run ended."""
    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    landing = record.get("landing_status")
    if landing == "landed":
        return "landed"
    verdict = record.get("verdict")
    if not verdict:
        reviews = record.get("reviews")
        if isinstance(reviews, list):
            for review in reversed(reviews):
                if isinstance(review, dict) and review.get("verdict"):
                    verdict = review.get("verdict")
                    break
    if isinstance(verdict, str) and verdict:
        return verdict
    phase = outcome.get("final_phase")
    if isinstance(phase, str) and phase:
        return phase
    return "unknown"


def _started_at(record: dict) -> str:
    timing = record.get("timing") if isinstance(record.get("timing"), dict) else {}
    value = timing.get("started_at")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class IssueCostAggregate:
    """What one issue has cost across every recorded run that worked on it.

    ``attempts`` counts runs the substrate has recorded, so during a live run
    the in-flight attempt is not yet included — which is exactly the reading the
    re-entry surface wants: "this would be attempt N+1, and the N before it cost
    this much".

    ``complete`` is False when any contributing run recorded unmeasured spend.
    ``measured_total_usd`` is then a lower bound, never the issue's cost.
    """

    key: str
    attempts: int
    measured_total_usd: float
    complete: bool = True
    outcomes: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()

    @property
    def total_usd(self) -> float | None:
        """The issue total, or ``None`` when a contributor went unmeasured."""
        return self.measured_total_usd if self.complete else None

    @property
    def has_prior_attempts(self) -> bool:
        """Whether more than one run has worked this issue."""
        return self.attempts > 1

    def format_cost(self) -> str:
        """``$12.34``, or an explicit lower bound when a contributor is unknown.

        Mirrors :func:`theforge.coordinator.util._fmt_cost_total` rather than
        importing it: this module is stdlib-only by design so the aggregation is
        testable without dragging in the process-group machinery that lives
        alongside that helper.
        """
        if self.complete:
            return f"${self.measured_total_usd:.2f}"
        if self.measured_total_usd:
            return f"unknown (>= ${self.measured_total_usd:.2f} measured)"
        return "unknown"

    def describe(self) -> str:
        """``$341.10 across 5 runs`` — the cost and the attempt count together.

        The count travels with the figure because they call for different
        responses: one expensive attempt is a mis-scoped story, five cheap ones
        are a loop that is not converging.
        """
        runs = "run" if self.attempts == 1 else "runs"
        return f"{self.format_cost()} across {self.attempts} {runs}"


def aggregate_issue_cost(
    records: Iterable[dict],
    *,
    key: str,
) -> IssueCostAggregate | None:
    """Sum an issue's recorded runs into one aggregate; ``None`` when there are none.

    ``records`` are migrated run records for a single issue — see
    :func:`records_for_issue` for the grouping rule that selects them. Records
    subsumed by a successor that carried their spend forward are dropped, so a
    dropped-and-re-executed generation contributes its dollars once.
    """
    by_run: dict[str, dict] = {}
    anonymous: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        run_id = record.get("run_id")
        if isinstance(run_id, str) and run_id:
            by_run.setdefault(run_id, record)
        else:
            anonymous.append(record)

    subsumed = {
        parent
        for run_id, record in by_run.items()
        if (parent := _subsumed_parent(record)) and parent != run_id
    }
    contributing = [record for run_id, record in by_run.items() if run_id not in subsumed]
    contributing.extend(anonymous)
    if not contributing:
        return None

    contributing.sort(key=_started_at)
    measured = 0.0
    complete = True
    for record in contributing:
        cost = _measured_cost(record)
        if cost is None:
            complete = False
        else:
            measured += cost

    return IssueCostAggregate(
        key=key,
        attempts=len(contributing),
        measured_total_usd=round(measured, 4),
        complete=complete,
        outcomes=tuple(_outcome_label(r) for r in contributing),
        run_ids=tuple(str(r.get("run_id") or "") for r in contributing),
    )


def records_for_issue(
    conn: object,
    *,
    slug: str | None = None,
    issue_id: int | None = None,
) -> list[dict]:
    """Migrated run records belonging to one issue, oldest first.

    The grouping rule, in one place: ``issue_id`` when a record has one, and the
    exact ``slug`` otherwise. ``issue_id`` is nullable — it is only populated
    when the task's ``github_issue`` parsed as an integer — so a file-based
    story groups by slug alone. Two distinct slugs that both recorded a null
    ``issue_id`` are never merged.
    """
    from theforge.coordinator import audit_substrate  # noqa: PLC0415

    resolved_issue_id = issue_id if issue_id is not None else issue_number_from_slug(slug)
    clauses: list[str] = []
    params: list[object] = []
    if resolved_issue_id is not None:
        clauses.append("issue_id = ?")
        params.append(int(resolved_issue_id))
    if slug:
        clauses.append("slug = ?")
        params.append(slug)
    if not clauses:
        return []

    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT run_id, raw_json, record_schema_version FROM audit_records "
        f"WHERE {' OR '.join(clauses)} ORDER BY COALESCE(started_at, '') ASC",
        tuple(params),
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        try:
            run_id, raw, version = row["run_id"], row["raw_json"], row["record_schema_version"]
        except (TypeError, IndexError, KeyError):
            run_id, raw, version = row[0], row[1], row[2]
        record = audit_substrate._load_migrated(raw, version)
        if record is None:
            continue
        # The indexed run id is authoritative: a record whose payload predates
        # the id being stored inline would otherwise aggregate as anonymous and
        # defeat the carry-forward de-duplication.
        if not record.get("run_id"):
            record = {**record, "run_id": run_id}
        out.append(record)
    return out


def load_issue_cost(
    project_root: Path | None,
    *,
    slug: str | None = None,
    issue_id: int | None = None,
) -> IssueCostAggregate | None:
    """Best-effort issue aggregate for a display surface; ``None`` when unknown.

    Never raises and never writes. A missing or corrupt substrate, an
    unresolvable identifier, or an issue with no recorded run all yield
    ``None``, so no status view fails because the index is absent.
    """
    if project_root is None:
        return None
    resolved_issue_id = issue_id if issue_id is not None else issue_number_from_slug(slug)
    if not slug and resolved_issue_id is None:
        return None
    key = f"#{resolved_issue_id}" if resolved_issue_id is not None else str(slug)
    try:
        from theforge.coordinator import audit_substrate  # noqa: PLC0415

        conn = audit_substrate.open_readonly(Path(project_root))
    except Exception:  # noqa: BLE001 - a status view must not fail on the index
        return None
    try:
        records = records_for_issue(conn, slug=slug, issue_id=resolved_issue_id)
        return aggregate_issue_cost(records, key=key)
    except Exception:  # noqa: BLE001 - same: best-effort by inheritance
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def issue_cost_lines(
    project_root: Path | None,
    slug: str,
    *,
    indent: str = "    ",
    label: str = "issue",
    aggregate: IssueCostAggregate | None = None,
) -> Sequence[str]:
    """Ready-to-print ``issue:`` line for ``slug``, or an empty list.

    Empty for an issue with a single recorded run: that run's own cost already
    *is* the issue's cost, so the common case reads exactly as it does today
    rather than gaining a restatement of the figure beside it.
    """
    if aggregate is None:
        aggregate = load_issue_cost(project_root, slug=slug)
    if aggregate is None or not aggregate.has_prior_attempts:
        return []
    return [f"{indent}{label}: {aggregate.describe()}"]
