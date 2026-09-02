"""Reading one audit record into the facts the effectiveness report measures (#1867).

The companion to :mod:`theforge.knowledge_effectiveness`, split from it because
the two change for different reasons: this module changes when the *audit record
schema* changes, that one when the *metric definitions* do. Everything here is
schema knowledge — which block holds the plan-regeneration flag, which manifest
phases can carry prior-run context, which field is measured cost — reduced to a
:class:`RunSignals` the metric layer can compute over without knowing an audit
record exists.

Two rules hold throughout:

- **Cohorts come from the manifest, not from configuration.** A run is
  ``with_prior_summary`` only when an eligible phase's manifest says prior-run
  context was enabled *and* something was actually included. Enabled with
  nothing included is ``without_prior_summary`` only when the manifest records
  a readable index state; failed-closed index maintenance is ``unclassified``.
  Disabled or absent is also ``unclassified``, because a run from before the
  feature existed is not evidence about the feature.
- **Absent telemetry is unavailable, never zero.** Every metric input is
  ``None``-able, so "this record predates the telemetry" can never be read
  downstream as "this record recorded a zero".
"""

from __future__ import annotations

from dataclasses import dataclass

from theforge.task.invariant_selector import ELIGIBLE_PHASES as INVARIANT_ELIGIBLE_PHASES
from theforge.task.prior_run_selector import (
    ELIGIBLE_PHASES,
    INDEX_STATE_MISSING,
    INDEX_STATE_READY,
    INDEX_STATE_STALE_SCHEMA,
    INDEX_STATE_UNREADABLE,
)

# ── Cohorts ──────────────────────────────────────────────────────────────────

COHORT_WITH = "with_prior_summary"
COHORT_WITHOUT = "without_prior_summary"
COHORT_UNCLASSIFIED = "unclassified"

#: Cohorts for the #1875 invariant-context spike. Same rule as above: the
#: manifest decides, not configuration, and a run from before the feature
#: existed is not evidence about the feature.
INVARIANT_COHORT_WITH = "with_invariant_context"
INVARIANT_COHORT_WITHOUT = "without_invariant_context"

_COMPLEXITY_BANDS = {
    "small": "LOW",
    "medium": "MEDIUM",
    "large": "HIGH",
    "low": "LOW",
    "high": "HIGH",
}

_FAILED_CLOSED_INDEX_STATES = frozenset(
    {INDEX_STATE_MISSING, INDEX_STATE_UNREADABLE, INDEX_STATE_STALE_SCHEMA}
)


@dataclass(frozen=True)
class RunSignals:
    """The deterministic facts one audit record contributes to the report.

    Every metric input is ``None``-able: that is how "this record predates the
    telemetry" stays distinguishable from "this record recorded a zero".
    """

    run_id: str
    slug: str
    started_at: str | None
    cohort: str
    bucket: tuple[str, str, tuple[str, ...]] | None
    success: bool
    plan_regenerated: bool | None
    restated_findings: int | None
    total_findings: int | None
    dev_iterations: int | None
    review_cycles: int | None
    cost_usd: float | None
    invariant_cohort: str = COHORT_UNCLASSIFIED
    invariant_included: int | None = None
    invariant_uncertain: int | None = None
    invariant_dropped: int | None = None

    @property
    def classified(self) -> bool:
        return self.cohort in (COHORT_WITH, COHORT_WITHOUT)

    @property
    def invariant_classified(self) -> bool:
        return self.invariant_cohort in (INVARIANT_COHORT_WITH, INVARIANT_COHORT_WITHOUT)


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _cost(value: object) -> float | None:
    """Measured cost only. ``None`` stays ``None`` — never coerced to 0.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def classify_cohort(record: dict) -> str:
    """Which cohort a run belongs to, decided only by its context manifests.

    Only prose-eligible plan/dev/review manifests classify cohorts. Preflight's
    signal-only manifest is advisory visibility, not treatment/control evidence,
    so preflight-only records stay ``unclassified`` rather than inflating the
    control cohort. An enabled manifest whose selector failed closed on index
    maintenance is also ``unclassified``: unreadable input is not evidence that
    the readable corpus was empty.

    These cohorts are descriptive groupings of what the selector happened to do
    for a story, not independently assigned treatment/control arms. A later
    report may summarize them, but it must not treat them as a causal
    comparison unless the assignment mechanism changes.
    """
    manifests = record.get("context_manifests")
    if not isinstance(manifests, list):
        return COHORT_UNCLASSIFIED

    enabled_somewhere = False
    failed_closed_somewhere = False
    for entry in manifests:
        manifest = _mapping(entry)
        phase = (_text(manifest.get("phase")) or "").lower()
        if phase not in ELIGIBLE_PHASES:
            continue
        prior = _mapping(manifest.get("prior_run_context"))
        if prior.get("enabled") is not True:
            continue
        enabled_somewhere = True
        included = prior.get("included")
        if isinstance(included, list) and included:
            return COHORT_WITH
        index_state = _text(prior.get("index_state")) or INDEX_STATE_READY
        if index_state in _FAILED_CLOSED_INDEX_STATES:
            failed_closed_somewhere = True
    if failed_closed_somewhere:
        return COHORT_UNCLASSIFIED
    return COHORT_WITHOUT if enabled_somewhere else COHORT_UNCLASSIFIED


def classify_invariant_cohort(record: dict) -> str:
    """Which invariant-context cohort a run belongs to (#1875).

    Mirrors :func:`classify_cohort` deliberately: enabled somewhere *and*
    something included is treatment; enabled with nothing included is a genuine
    control; disabled or absent is unclassified. Preflight manifests never
    classify — the selector refuses that phase, so a preflight manifest saying
    "not injected here" is not evidence either way.
    """
    manifests = record.get("context_manifests")
    if not isinstance(manifests, list):
        return COHORT_UNCLASSIFIED

    enabled_somewhere = False
    for entry in manifests:
        manifest = _mapping(entry)
        phase = (_text(manifest.get("phase")) or "").lower()
        if phase not in INVARIANT_ELIGIBLE_PHASES:
            continue
        invariants = _mapping(manifest.get("invariant_context"))
        if invariants.get("enabled") is not True:
            continue
        enabled_somewhere = True
        included = invariants.get("included")
        if isinstance(included, list) and included:
            return INVARIANT_COHORT_WITH
    return INVARIANT_COHORT_WITHOUT if enabled_somewhere else COHORT_UNCLASSIFIED


def _invariant_counts(record: dict) -> tuple[int | None, int | None, int | None]:
    """Included / uncertain / dropped invariants across eligible manifests.

    ``None`` when no eligible manifest carried an enabled ``invariant_context``
    block — unavailable telemetry, never a zero.
    """
    manifests = record.get("context_manifests")
    if not isinstance(manifests, list):
        return (None, None, None)

    included = uncertain = dropped = 0
    observed = False
    for entry in manifests:
        manifest = _mapping(entry)
        phase = (_text(manifest.get("phase")) or "").lower()
        if phase not in INVARIANT_ELIGIBLE_PHASES:
            continue
        invariants = _mapping(manifest.get("invariant_context"))
        if invariants.get("enabled") is not True:
            continue
        observed = True
        included += _list_len(invariants.get("included"))
        uncertain += _list_len(invariants.get("uncertain"))
        dropped += _list_len(invariants.get("dropped"))
    if not observed:
        return (None, None, None)
    return (included, uncertain, dropped)


def _list_len(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _bucket(record: dict) -> tuple[str, str, tuple[str, ...]] | None:
    """Comparability key: work type, complexity band, domains.

    ``None`` when preflight recorded neither a work type nor a complexity — such
    a run can still be counted in a cohort, but it cannot be matched against
    anything, so it is excluded from the bucketed comparison.
    """
    preflight = _mapping(record.get("preflight"))
    if not preflight:
        return None
    work_type = _text(preflight.get("work_type"))
    band = _complexity_band(preflight)
    if work_type is None and band is None:
        return None
    domains_raw = preflight.get("domains")
    domains = (
        tuple(sorted(d.strip() for d in domains_raw if isinstance(d, str) and d.strip()))
        if isinstance(domains_raw, list)
        else ()
    )
    return (work_type or "unknown", band or "UNKNOWN", domains)


def _complexity_band(preflight: dict) -> str | None:
    raw = _text(preflight.get("complexity"))
    if raw is not None:
        return _COMPLEXITY_BANDS.get(raw.lower(), raw.upper())
    score = _count(preflight.get("complexity_score"))
    if score is None:
        return None
    if score <= 3:
        return "LOW"
    if score <= 6:
        return "MEDIUM"
    return "HIGH"


def _review_recurrence(record: dict) -> tuple[int | None, int | None]:
    """Restated findings and total findings across this run's review cycles.

    ``restated_findings`` / ``novel_findings`` are the primary signal. When an
    iteration predates them, ``repeated_findings_by_severity`` against
    ``finding_counts`` is the equivalent recurrence signal. A run whose
    ``review_loop`` carries neither yields ``(None, None)`` — unavailable, not a
    zero recurrence rate.
    """
    loop = _mapping(record.get("iterations")).get("review_loop")
    if not isinstance(loop, list) or not loop:
        return (None, None)

    restated = 0
    total = 0
    observed = False
    for entry in loop:
        iteration = _mapping(entry)
        restated_n = _count(iteration.get("restated_findings"))
        novel_n = _count(iteration.get("novel_findings"))
        if restated_n is not None and novel_n is not None:
            restated += restated_n
            total += restated_n + novel_n
            observed = True
            continue
        repeated = _sum_severity(iteration.get("repeated_findings_by_severity"))
        counted = _sum_severity(iteration.get("finding_counts"))
        if repeated is None or counted is None:
            continue
        restated += repeated
        total += max(counted, repeated)
        observed = True
    if not observed:
        return (None, None)
    return (restated, total)


def _sum_severity(value: object) -> int | None:
    counts = value if isinstance(value, dict) else None
    if counts is None:
        return None
    total = 0
    for count in counts.values():
        parsed = _count(count)
        if parsed is not None:
            total += parsed
    return total


def extract_signals(record: dict) -> RunSignals:
    """Reduce one migrated audit record to the facts the report needs."""
    task = _mapping(record.get("task"))
    iterations = _mapping(record.get("iterations"))
    plan_review = record.get("plan_review")
    restated, total_findings = _review_recurrence(record)
    inv_included, inv_uncertain, inv_dropped = _invariant_counts(record)

    return RunSignals(
        run_id=_text(record.get("run_id")) or "",
        slug=_text(task.get("slug")) or _text(task.get("name")) or "",
        started_at=_text(_mapping(record.get("timing")).get("started_at")),
        cohort=classify_cohort(record),
        bucket=_bucket(record),
        success=bool(_mapping(record.get("outcome")).get("success")),
        # A record with no plan_review block never reached plan review; that is
        # an unavailable denominator, not a run with zero regenerations.
        plan_regenerated=(
            bool(_mapping(plan_review).get("regenerated"))
            if isinstance(plan_review, dict)
            else None
        ),
        restated_findings=restated,
        total_findings=total_findings,
        dev_iterations=_count(iterations.get("dev_iterations_productive")),
        review_cycles=_count(iterations.get("review_cycles_total")),
        cost_usd=_cost(_mapping(record.get("cost")).get("total_usd")),
        invariant_cohort=classify_invariant_cohort(record),
        invariant_included=inv_included,
        invariant_uncertain=inv_uncertain,
        invariant_dropped=inv_dropped,
    )
