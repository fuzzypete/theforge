"""Pure ranking math for the #2348 structural-decay spike (see the package docstring).

Nothing here touches the substrate, the filesystem, or the config. It takes
already-loaded ``(run, path)`` touch rows and line counts and returns ranked
candidates, a threshold verdict, and the line-count comparison — which is what
makes the methodology testable against seeded data rather than only against
whatever the repository happens to have recorded.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# ── Trust thresholds ─────────────────────────────────────────────────────
#
# The spike's answer to "how much recorded data is required before a ranking is
# trustworthy". These are the numbers the report checks itself against; they are
# justified in the spike record and deliberately live here as one named block so
# a reader can see the whole bar at once.

MIN_RUN_COVERAGE = 0.80
"""Fraction of measured cost-bearing runs that must join to a changed-file set."""

MIN_JOINABLE_RUNS = 30
"""Joinable measured runs required in the window before any ranking is computed."""

MIN_TOUCHING_RUNS = 10
"""Touching runs a path needs before its excess figure is more than an anecdote."""

MIN_COHORT_RUNS = 5
"""Comparable non-touching runs required before a controlled comparison is made."""


# ── Controls ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Control:
    """One confounder control, with where its value comes from.

    ``availability`` is the honest part. ``indexed`` means the value is a column
    on ``audit_records``; ``derived`` means it was reconstructed from the decoded
    record; ``unavailable`` means it could not be recovered for this dataset. An
    unavailable control is never silently dropped — it is forced into the
    weakest-signal candidate set, so an uncontrolled comparison always says so.
    """

    key: str
    label: str
    availability: str

    @property
    def usable(self) -> bool:
        return self.availability != "unavailable"


def _complexity_band(score: object) -> str | None:
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    value = int(score)
    if value <= 3:
        return "1-3"
    if value <= 6:
        return "4-6"
    return "7-10"


def _size_band(files_changed: int) -> str:
    if files_changed <= 2:
        return "1-2"
    if files_changed <= 5:
        return "3-5"
    if files_changed <= 10:
        return "6-10"
    return "11+"


def _panel_band(panel_size: int | None) -> str | None:
    if panel_size is None:
        return None
    if panel_size <= 1:
        return "solo"
    if panel_size <= 3:
        return "2-3"
    return "4+"


# ── Run projection ───────────────────────────────────────────────────────


@dataclass
class RunFacts:
    """One measured run, with its changed-file set and its control values."""

    run_id: str
    slug: str | None
    started_at: str | None
    cost_usd: float
    paths: set[str] = field(default_factory=set)
    insertions: int = 0
    deletions: int = 0
    complexity_score: int | None = None
    dev_model: str | None = None
    panel_size: int | None = None
    review_cycles: int | None = None
    finding_files: tuple[str, ...] = ()

    @property
    def files_changed(self) -> int:
        return len(self.paths)

    @property
    def attributed_cost(self) -> float:
        """Run cost divided evenly across the files it changed.

        Even division is a stated simplification, not a measurement: the
        substrate records what a run cost and which files it touched, never how
        the spend was distributed between them. It is used because the
        alternatives are worse — charging the full run cost to every file
        multiplies one run's spend by its file count, and weighting by
        insertions rewards verbosity. The comparison is like-for-like (the
        comparables are attributed the same way), so the residual survives the
        simplification even though the absolute figure is soft.
        """
        return self.cost_usd / self.files_changed if self.files_changed else 0.0

    def cohort_key(self, controls: Sequence[Control]) -> tuple:
        values: list[object] = []
        for control in controls:
            if control.key == "complexity":
                values.append(_complexity_band(self.complexity_score))
            elif control.key == "dev_model":
                values.append(self.dev_model)
            elif control.key == "panel_size":
                values.append(_panel_band(self.panel_size))
            elif control.key == "intended_size":
                values.append(_size_band(self.files_changed))
        return tuple(values)


def build_runs(
    touch_rows: Iterable[dict],
    *,
    record_extras: dict[str, dict] | None = None,
) -> list[RunFacts]:
    """Fold ``(run, path)`` touch rows into one :class:`RunFacts` per run.

    ``record_extras`` carries the controls that are not indexed columns —
    ``panel_size``, ``review_cycles``, ``finding_files`` — keyed by run id. It is
    optional: absent, those controls report as ``unavailable`` rather than being
    quietly treated as controlled.
    """
    extras = record_extras or {}
    runs: dict[str, RunFacts] = {}
    for row in touch_rows:
        run_id = str(row["run_id"])
        run = runs.get(run_id)
        if run is None:
            cost = row.get("total_cost_usd")
            score = row.get("complexity_score")
            extra = extras.get(run_id, {})
            findings = extra.get("finding_files") or ()
            run = RunFacts(
                run_id=run_id,
                slug=row.get("slug"),
                started_at=row.get("started_at"),
                cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
                complexity_score=(
                    int(score)
                    if isinstance(score, (int, float)) and not isinstance(score, bool)
                    else None
                ),
                dev_model=row.get("dev_resolved_model") or row.get("dev_model"),
                panel_size=extra.get("panel_size"),
                review_cycles=extra.get("review_cycles"),
                finding_files=tuple(findings),
            )
            runs[run_id] = run
        run.paths.add(str(row["path"]))
        for key in ("insertions", "deletions"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(run, key, getattr(run, key) + int(value))
    return list(runs.values())


def resolve_controls(runs: Sequence[RunFacts]) -> list[Control]:
    """Report each control's availability against the actual dataset.

    A control that no run carries a value for is ``unavailable``, not merely
    unused: the difference between "we controlled for panel size" and "we could
    not" is the difference between a residual and a raw total.
    """

    def present(attr: str) -> bool:
        return any(getattr(run, attr) is not None for run in runs)

    return [
        Control(
            "complexity",
            "complexity score band",
            "indexed" if present("complexity_score") else "unavailable",
        ),
        Control(
            "dev_model",
            "dev model / tier proxy",
            "indexed" if present("dev_model") else "unavailable",
        ),
        Control(
            "panel_size",
            "reviewer panel size",
            "derived" if present("panel_size") else "unavailable",
        ),
        Control("intended_size", "intended feature size (files changed)", "derived"),
    ]


# ── Controlled excess ────────────────────────────────────────────────────


@dataclass
class Comparison:
    """One touching run measured against its comparable cohort."""

    run_id: str
    attributed_cost: float
    expected_cost: float | None
    cohort_size: int
    controls_applied: tuple[str, ...]

    @property
    def excess(self) -> float:
        if self.expected_cost is None:
            return 0.0
        return self.attributed_cost - self.expected_cost


def _compare(
    run: RunFacts,
    others: Sequence[RunFacts],
    controls: Sequence[Control],
) -> Comparison:
    """Compare ``run`` against non-touching runs, relaxing controls as needed.

    Controls are dropped in reverse order of the strength of their claim —
    intended size first, then panel size, then dev model — until the cohort
    reaches :data:`MIN_COHORT_RUNS`. Whatever survived is recorded in
    ``controls_applied``, so a comparison that ended up controlling for nothing
    reports an empty tuple rather than passing itself off as controlled.
    """
    usable = [c for c in controls if c.usable]
    while True:
        key = run.cohort_key(usable)
        cohort = [o for o in others if o.cohort_key(usable) == key]
        if len(cohort) >= MIN_COHORT_RUNS:
            return Comparison(
                run_id=run.run_id,
                attributed_cost=run.attributed_cost,
                expected_cost=statistics.median(o.attributed_cost for o in cohort),
                cohort_size=len(cohort),
                controls_applied=tuple(c.key for c in usable),
            )
        if not usable:
            return Comparison(
                run_id=run.run_id,
                attributed_cost=run.attributed_cost,
                expected_cost=None,
                cohort_size=len(cohort),
                controls_applied=(),
            )
        usable = usable[:-1]


@dataclass
class Candidate:
    """A ranked path with the evidence it was ranked on."""

    path: str
    touching_runs: int
    joinable_spend_usd: float
    excess_usd: float
    comparisons: list[Comparison]
    line_count: int | None
    co_touched: list[tuple[str, int]]
    finding_mentions: int
    review_cycles: int
    weakest_signal: str

    @property
    def controlled_comparisons(self) -> int:
        return sum(1 for c in self.comparisons if c.expected_cost is not None)


def _weakest_signal(candidate_fields: dict, controls: Sequence[Control]) -> str:
    """Name the single most limiting fact behind an entry.

    Ordered most-disqualifying first. The output is one sentence because it is
    read next to the number it qualifies; a list of caveats gets skimmed, and a
    candidate whose weakest signal is skimmed is a candidate that gets
    rubber-stamped.
    """
    touching = candidate_fields["touching_runs"]
    controlled = candidate_fields["controlled_comparisons"]
    uncontrolled = touching - controlled
    if touching < MIN_TOUCHING_RUNS:
        return (
            f"{touching} touching run(s) is below the sample floor of "
            f"{MIN_TOUCHING_RUNS} for a tier-controlled comparison; treat the "
            "excess figure as directional, not measured"
        )
    if controlled == 0:
        return (
            "no comparable cohort reached "
            f"{MIN_COHORT_RUNS} runs at any control level; the figure shown is "
            "raw attributed spend, not excess"
        )
    if uncontrolled:
        return (
            f"{uncontrolled} of {touching} touching runs found no cohort and "
            "contributed no excess; the total understates by an unknown amount"
        )
    missing = [c.label for c in controls if not c.usable]
    if missing:
        return f"uncontrolled for {', '.join(missing)} — not recoverable from these records"
    weakest = min(
        (c for c in candidate_fields["comparisons"] if c.expected_cost is not None),
        key=lambda c: c.cohort_size,
        default=None,
    )
    if weakest is not None:
        dropped = [c.label for c in controls if c.usable and c.key not in weakest.controls_applied]
        if dropped:
            return (
                f"the thinnest comparison dropped {', '.join(dropped)} to reach a "
                f"{weakest.cohort_size}-run cohort"
            )
        return f"thinnest comparison rests on {weakest.cohort_size} comparable runs"
    return "no controlled comparison available"


def rank_candidates(
    runs: Sequence[RunFacts],
    *,
    line_counts: dict[str, int] | None = None,
    path_filter=None,
) -> list[Candidate]:
    """Rank paths by controlled excess spend, descending.

    Every path touched by any run gets an entry — including ones below the
    sample floor, which are ranked but carry a weakest-signal line saying so.
    Silently dropping them would hide the fact that the dataset is thin, which
    is exactly the finding this spike needs to be able to report.
    """
    controls = resolve_controls(runs)
    counts = line_counts or {}
    by_path: dict[str, list[RunFacts]] = {}
    for run in runs:
        for path in run.paths:
            if path_filter is not None and not path_filter(path):
                continue
            by_path.setdefault(path, []).append(run)

    candidates: list[Candidate] = []
    for path, touching in sorted(by_path.items()):
        others = [r for r in runs if path not in r.paths]
        comparisons = [_compare(run, others, controls) for run in touching]
        co_touch: dict[str, int] = {}
        for run in touching:
            for other_path in run.paths:
                if other_path != path:
                    co_touch[other_path] = co_touch.get(other_path, 0) + 1
        fields = {
            "touching_runs": len(touching),
            "controlled_comparisons": sum(1 for c in comparisons if c.expected_cost is not None),
            "comparisons": comparisons,
        }
        candidates.append(
            Candidate(
                path=path,
                touching_runs=len(touching),
                joinable_spend_usd=sum(r.attributed_cost for r in touching),
                excess_usd=sum(c.excess for c in comparisons),
                comparisons=comparisons,
                line_count=counts.get(path),
                co_touched=sorted(co_touch.items(), key=lambda kv: (-kv[1], kv[0]))[:3],
                finding_mentions=sum(1 for r in touching if path in r.finding_files),
                review_cycles=sum(r.review_cycles or 0 for r in touching),
                weakest_signal=_weakest_signal(fields, controls),
            )
        )
    candidates.sort(key=lambda c: (-c.excess_usd, -c.touching_runs, c.path))
    return candidates


# ── Threshold + line-count comparison ────────────────────────────────────


def threshold_status(coverage: dict, candidates: Sequence[Candidate]) -> dict:
    """Return whether the recorded data clears the trust bar, and what fails.

    Reported as a set of independently-checked conditions rather than a single
    boolean, because "which floor are we short of" is the actionable part — it
    is what says whether the answer is "wait for more runs" or "the join itself
    is broken".
    """
    checks = [
        {
            "name": "run coverage",
            "actual": round(coverage.get("run_coverage_ratio", 0.0), 4),
            "required": MIN_RUN_COVERAGE,
            "met": coverage.get("run_coverage_ratio", 0.0) >= MIN_RUN_COVERAGE,
            "detail": (
                f"{coverage.get('joinable_runs', 0)} of {coverage.get('measured_runs', 0)} "
                "measured cost-bearing runs join to a changed-file set"
            ),
        },
        {
            "name": "joinable sample",
            "actual": coverage.get("joinable_runs", 0),
            "required": MIN_JOINABLE_RUNS,
            "met": coverage.get("joinable_runs", 0) >= MIN_JOINABLE_RUNS,
            "detail": f"{coverage.get('joinable_runs', 0)} joinable measured runs in window",
        },
        {
            "name": "ranked candidate floor",
            "actual": max((c.touching_runs for c in candidates), default=0),
            "required": MIN_TOUCHING_RUNS,
            "met": any(c.touching_runs >= MIN_TOUCHING_RUNS for c in candidates),
            "detail": (
                f"{sum(1 for c in candidates if c.touching_runs >= MIN_TOUCHING_RUNS)} path(s) "
                f"reach {MIN_TOUCHING_RUNS} touching runs"
            ),
        },
        {
            "name": "controlled comparison floor",
            "actual": sum(1 for c in candidates if c.controlled_comparisons > 0),
            "required": 1,
            "met": any(c.controlled_comparisons > 0 for c in candidates),
            "detail": (
                f"{sum(1 for c in candidates if c.controlled_comparisons > 0)} path(s) obtained "
                f"a cohort of at least {MIN_COHORT_RUNS} comparable runs"
            ),
        },
    ]
    return {"met": all(check["met"] for check in checks), "checks": checks}


def compare_to_line_counts(
    candidates: Sequence[Candidate],
    line_counts: dict[str, int],
    *,
    top: int = 10,
) -> dict:
    """Compare the excess ranking against ranking by ``wc -l``. The ship gate.

    If the two orderings agree, this analysis has rediscovered module size with
    extra steps and should be killed rather than shipped — so the comparison
    reports Spearman rank correlation over the paths both rankings cover, the
    top-N overlap, and the entries that move furthest between them. A high
    correlation is a *kill* signal, and this function is written to surface it
    plainly rather than bury it.
    """
    ranked = [c for c in candidates if c.line_count is not None]
    excess_order = [c.path for c in ranked]
    size_order = [
        path
        for path, _ in sorted(
            ((c.path, c.line_count or 0) for c in ranked), key=lambda kv: (-kv[1], kv[0])
        )
    ]
    excess_rank = {path: i for i, path in enumerate(excess_order)}
    size_rank = {path: i for i, path in enumerate(size_order)}
    n = len(ranked)
    correlation: float | None = None
    if n > 1:
        d_squared = sum((excess_rank[p] - size_rank[p]) ** 2 for p in excess_order)
        correlation = 1 - (6 * d_squared) / (n * (n**2 - 1))
    top_excess = excess_order[:top]
    top_size = size_order[:top]
    movers = sorted(
        ((p, size_rank[p] - excess_rank[p]) for p in excess_order),
        key=lambda kv: (-abs(kv[1]), kv[0]),
    )[:5]
    return {
        "compared_paths": n,
        "spearman": None if correlation is None else round(correlation, 4),
        "top_excess": top_excess,
        "top_line_count": top_size,
        "overlap": sorted(set(top_excess) & set(top_size)),
        "overlap_ratio": (len(set(top_excess) & set(top_size)) / len(top_excess))
        if top_excess
        else 0.0,
        "biggest_movers": movers,
    }
