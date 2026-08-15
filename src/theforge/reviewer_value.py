"""Mechanical reviewer-value signals for adaptive reviewer routing (#1443, #2156).

Records and aggregates *coordinator-computable* per-reviewer value signals —
P1 uniqueness (did this reviewer surface blocking findings no peer did?) and
latency-per-P1 (wall-clock cost per blocking finding raised) — then exposes them
as routing-safe aggregate signals only after ADR-0006 clause-2 admissibility
gating: a sample floor, recency weighting via the shared #1392 mechanism, and
taint exclusion via #1852.

The same machinery serves both reviewer phases. ``phase`` selects which persisted
section a fold writes and a signal read consults — ``plan_review_value`` for the
plan-review pool (#1443) and ``code_review_value`` for the code-review pool
(#2156) — so the two histories stay separately queryable and a reviewer that is
redundant in one phase is not penalised in the other.

This module stays on the routing-safe side of the ADR-0006 boundary: it never
judges review "quality", "goodness", or "helpfulness". Uniqueness is computed by
deterministic anchor-overlap over the SAME structured findings the coordinator
already merges (:func:`plan_finding_classifier.extract_anchors`) — no LLM, no
prose scoring. Latency and P1 counts are mechanical.

Data flow:

- Per-run signals are computed at reviewer-pool completion —
  ``coordinator/plan_flow.py`` for plan review, ``coordinator/review_pool.py``
  for code review — via :func:`compute_reviewer_uniqueness`, persisted in the
  native audit record, and folded into the matching ``model_profiles.yaml``
  section by :func:`fold_reviewer_value` — reusing the exact taint gate and
  recency-ring mechanism the #1388 reviewer-completion signal uses.
- At routing time :func:`get_reviewer_value_signal` reads that folded section and
  returns raw / recency-weighted / sample-floor-gated values, mirroring
  :func:`model_profiles.get_review_signal`.

All aggregation here is pure. The only cross-module dependencies —
``plan_finding_classifier`` (anchor extraction) and ``model_profiles``
(``_weighted_rate`` / ``_recency_params``) — are imported lazily inside the
functions that need them so this module composes without an import cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from theforge.review import PlanReviewFinding, ReviewFinding

# ── Reviewer phases ─────────────────────────────────────────────────────────
# The two reviewer pools that produce structured blocking findings. Each folds
# into its OWN persisted section so plan-review and code-review value histories
# stay separately queryable (#2156) — a reviewer that is redundant when judging
# plans is not thereby judged redundant when judging diffs.
PLAN_PHASE = "plan_review"
CODE_PHASE = "code_review"

# ── Persisted section / ring key names ──────────────────────────────────────
# One place owns the value-section schema; the fold writer and the signal reader
# both key off these constants so they can never drift apart. ``SECTION`` stays
# the literal plan-review key so profile data written before #2156 continues to
# resolve without migration.
SECTION = "plan_review_value"
CODE_SECTION = "code_review_value"
_SECTION_BY_PHASE = {PLAN_PHASE: SECTION, CODE_PHASE: CODE_SECTION}
BY_COMPLEXITY = "by_complexity"
UNIQUENESS_RING = "_uniqueness_recent"
LATENCY_RING = "_latency_per_p1_recent"
UNIQUENESS_SUM = "_uniqueness_sum"
LATENCY_SUM = "_latency_per_p1_sum"

# Bound each ring on disk, matching the capability rings in model_profiles.
VALUE_RECENCY_WINDOW = 200

# Default sample floor: below this many admissible (non-tainted, P1-bearing) pool
# samples the aggregate is not routing-safe and falls through to tier ordering.
DEFAULT_VALUE_MIN_RUNS = 5

# Severities that count as a blocking ("P1") finding — the same set both reviewer
# pools treat as blocking (review.py / plan_flow.py / review_phase.py all use
# ``in ("P0", "P1")``). Deliberately excludes the plan-review ``P1-impl``
# severity, which is the *downgraded*, non-blocking outcome of corroboration.
_P1_SEVERITIES = frozenset({"P0", "P1"})


def section_for_phase(phase: str = PLAN_PHASE) -> str:
    """Return the persisted profile section a reviewer phase folds into.

    Raises on an unknown phase rather than silently defaulting: a typo must not
    quietly route one phase's value history into the other's section.
    """
    try:
        return _SECTION_BY_PHASE[phase]
    except KeyError:
        raise ValueError(
            f"unknown reviewer value phase {phase!r}; expected one of {sorted(_SECTION_BY_PHASE)}"
        ) from None


# ── Per-run sample carrier ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ReviewerValueSample:
    """One reviewer's mechanical value signals for one pool attempt.

    ``unique_p1`` / ``total_p1`` and ``latency_s`` are the raw mechanical
    measurements recorded verbatim in the audit trail. The aggregate routing
    signal folds a sample only when ``total_p1 > 0`` (uniqueness rate and
    latency-per-P1 are otherwise undefined); the audit still records every
    reviewer regardless.
    """

    name: str
    complexity: str
    unique_p1: int
    total_p1: int
    latency_s: float | None
    actual_model: str | None = None
    provider: str | None = None
    cli: str | None = None
    # Concrete identity that served this reviewer attempt (#2226). When the
    # identity fields above name a family alias, this is the only record of
    # which version produced the value measurement.
    resolved_model: str | None = None

    def uniqueness_rate(self) -> float | None:
        """Fraction of this reviewer's P1s no peer corroborated; None if no P1s."""
        if self.total_p1 <= 0:
            return None
        return self.unique_p1 / self.total_p1

    def latency_per_p1(self) -> float | None:
        """Wall-clock seconds per P1 raised; None if no P1s or latency unknown."""
        if self.total_p1 <= 0 or self.latency_s is None:
            return None
        return self.latency_s / self.total_p1


# Historical name, kept so the plan-review producer chain reads unchanged. The
# carrier was never plan-specific in content — only in naming.
PlanReviewerValueSample = ReviewerValueSample


# ── Deterministic uniqueness computation (Step 2) ───────────────────────────


class _UnionFind:
    """Minimal union-find over integer item indices."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


def plan_review_anchor_text(finding: "PlanReviewFinding") -> str:
    """Anchor text for a plan-review finding: its issue description."""
    return finding.description


def code_review_anchor_text(finding: "ReviewFinding") -> str:
    """Anchor text for a code-review finding: observed behaviour plus evidence.

    A :class:`review.ReviewFinding` splits what a plan finding carries in one
    prose blob across ``observed`` (behaviour-only, by construction free of
    symbol detail) and ``evidence`` (the file/line/symbol anchor). Corroboration
    between two reviewers who spotted the same defect lives mostly in the latter,
    so both feed the anchor extractor. ``file`` is deliberately NOT concatenated:
    file-path anchors are dropped below anyway (two reviewers naming the same
    file have not thereby raised the same issue).
    """
    return " ".join(part for part in (finding.observed, finding.evidence) if part)


def compute_reviewer_uniqueness(
    named_findings: list[tuple[str, list[Any]]],
    *,
    anchor_text: Callable[[Any], str] | None = None,
) -> dict[str, tuple[int, int]]:
    """Compute each reviewer's (unique_p1_count, total_p1_count) over a pool.

    Deterministic, LLM-free. Only blocking (P0/P1) findings participate. Two P1
    findings are considered the *same issue* when they share at least one non-file
    structural anchor (the identity rule already used by plan-review corroboration
    in :func:`review.apply_plan_corroboration`). A reviewer's P1 finding is
    *unique* when the anchor-group it belongs to contains no other reviewer — i.e.
    no peer in this same pool independently surfaced that issue.

    A P1 with no extractable non-file anchor cannot be corroborated, so it forms
    its own singleton group and counts as unique (novel-by-default), matching how
    corroboration treats an uncorroborated single-reviewer finding.

    ``anchor_text`` maps one finding to the prose the anchor extractor reads,
    which is the only phase-specific part of this computation: plan findings carry
    a single description, code-review findings split behaviour and evidence across
    two fields (see :func:`code_review_anchor_text`). Defaults to the plan-review
    extractor.

    Returns a mapping for every reviewer name passed in (including reviewers that
    raised zero P1s, which map to ``(0, 0)``), so the caller can record a row per
    reviewer regardless of outcome.
    """
    from theforge.plan_finding_classifier import extract_anchors, strip_reviewer_prefix

    to_text = anchor_text or plan_review_anchor_text

    # Collect one item per P1 finding: (reviewer_name, non_file_anchor_values).
    item_reviewer: list[str] = []
    item_anchors: list[frozenset[str]] = []
    totals: dict[str, int] = {name: 0 for name, _ in named_findings}

    for name, findings in named_findings:
        for f in findings:
            if f.severity not in _P1_SEVERITIES:
                continue
            totals[name] = totals.get(name, 0) + 1
            anchors = extract_anchors(strip_reviewer_prefix(to_text(f)))
            non_file = frozenset(a.value for a in anchors if a.kind != "file_path")
            item_reviewer.append(name)
            item_anchors.append(non_file)

    n = len(item_reviewer)
    uf = _UnionFind(n)
    # Union any two items that share a non-file anchor value.
    anchor_to_first: dict[str, int] = {}
    for i in range(n):
        for value in item_anchors[i]:
            if value in anchor_to_first:
                uf.union(anchor_to_first[value], i)
            else:
                anchor_to_first[value] = i

    # Distinct reviewers per group.
    group_reviewers: dict[int, set[str]] = {}
    for i in range(n):
        group_reviewers.setdefault(uf.find(i), set()).add(item_reviewer[i])

    uniques: dict[str, int] = {name: 0 for name, _ in named_findings}
    for i in range(n):
        reviewers = group_reviewers[uf.find(i)]
        if len(reviewers) == 1:
            uniques[item_reviewer[i]] = uniques.get(item_reviewer[i], 0) + 1

    return {name: (uniques.get(name, 0), totals.get(name, 0)) for name, _ in named_findings}


def compute_plan_reviewer_uniqueness(
    named_findings: list[tuple[str, list["PlanReviewFinding"]]],
) -> dict[str, tuple[int, int]]:
    """Plan-review-phase alias for :func:`compute_reviewer_uniqueness` (#1443)."""
    return compute_reviewer_uniqueness(named_findings, anchor_text=plan_review_anchor_text)


# ── Fold (Step 3, write side) ───────────────────────────────────────────────


def _empty_value_bucket() -> dict[str, Any]:
    return {
        "runs": 0,
        "tainted_runs": 0,
        UNIQUENESS_SUM: 0.0,
        LATENCY_SUM: 0.0,
        "avg_uniqueness_rate": 0.0,
        "avg_latency_per_p1": 0.0,
        UNIQUENESS_RING: [],
        LATENCY_RING: [],
    }


def _fold_bucket(bucket: dict[str, Any], uniq: float, latency_per_p1: float | None) -> None:
    runs = int(bucket.get("runs", 0)) + 1
    bucket["runs"] = runs
    u_sum = float(bucket.get(UNIQUENESS_SUM, 0.0)) + uniq
    bucket[UNIQUENESS_SUM] = u_sum
    bucket["avg_uniqueness_rate"] = round(u_sum / runs, 4)
    u_ring = bucket.setdefault(UNIQUENESS_RING, [])
    if not isinstance(u_ring, list):
        u_ring = []
        bucket[UNIQUENESS_RING] = u_ring
    u_ring.append(round(uniq, 4))
    if len(u_ring) > VALUE_RECENCY_WINDOW:
        del u_ring[:-VALUE_RECENCY_WINDOW]
    # Latency-per-P1 is only meaningful when the pool measured wall-clock; a run
    # with unknown latency still counts toward uniqueness but is skipped here so
    # an unmeasured run is never folded in as a 0-second review.
    if latency_per_p1 is not None:
        l_sum = float(bucket.get(LATENCY_SUM, 0.0)) + latency_per_p1
        bucket[LATENCY_SUM] = l_sum
        l_ring = bucket.setdefault(LATENCY_RING, [])
        if not isinstance(l_ring, list):
            l_ring = []
            bucket[LATENCY_RING] = l_ring
        l_ring.append(round(latency_per_p1, 4))
        if len(l_ring) > VALUE_RECENCY_WINDOW:
            del l_ring[:-VALUE_RECENCY_WINDOW]
        measured = len(l_ring)
        if measured > 0:
            bucket["avg_latency_per_p1"] = round(l_sum / measured, 4)


def fold_reviewer_value(
    entry: dict[str, Any],
    sample: ReviewerValueSample,
    *,
    phase: str = PLAN_PHASE,
    tainted: bool = False,
) -> None:
    """Fold one reviewer value sample into a model profile entry.

    ``phase`` selects the persisted section (``plan_review_value`` /
    ``code_review_value``) so the two reviewer phases accumulate independently.

    Mirrors :func:`model_profiles._update_review_completion`: keeps running
    counters and a bounded recency ring so the routing-admissible signal is
    recency-weighted (#1392) and recomputable from stored data. A sample with no
    P1s (``uniqueness_rate() is None``) carries no defined value and is not folded.

    Taint gate (ADR-0006 clause 4, #1852): a tainted run "doesn't teach", so its
    reviewer samples are tallied under ``tainted_runs`` and kept out of every
    routing aggregate — never deleted, so the exclusion stays visible.
    """
    uniq = sample.uniqueness_rate()
    if uniq is None:
        return  # no P1s → uniqueness/latency-per-P1 undefined; nothing to learn
    from theforge.model_profiles_identity import _normalize_band  # noqa: PLC0415
    from theforge.model_profiles_storage import _fold_resolved_model  # noqa: PLC0415

    band = _normalize_band(sample.complexity)
    section = entry.setdefault(section_for_phase(phase), {})
    section.setdefault("runs", 0)
    section.setdefault("tainted_runs", 0)
    band_bucket = section.setdefault(BY_COMPLEXITY, {}).setdefault(band, _empty_value_bucket())

    # Fold into both the band-agnostic section total and the per-band bucket so a
    # caller can consult either granularity.
    if tainted:
        for bucket in (section, band_bucket):
            bucket["tainted_runs"] = int(bucket.get("tainted_runs", 0)) + 1
        return

    lpp = sample.latency_per_p1()
    for bucket in (section, band_bucket):
        _fold_bucket(bucket, uniq, lpp)
        # Version attribution (#2226): say which concrete model produced this
        # value observation, so a population accumulated under a family alias is
        # not read as describing one reviewer when it describes several.
        _fold_resolved_model(bucket, sample.resolved_model, success=None, tainted=False)


def fold_plan_reviewer_value(
    entry: dict[str, Any],
    sample: ReviewerValueSample,
    *,
    tainted: bool = False,
) -> None:
    """Plan-review-phase alias for :func:`fold_reviewer_value` (#1443)."""
    fold_reviewer_value(entry, sample, phase=PLAN_PHASE, tainted=tainted)


# ── Signal reader (Step 3, read side) ───────────────────────────────────────


def _bucket_signal(
    bucket: dict[str, Any] | None,
    sum_key: str,
    ring_key: str,
    *,
    min_runs: int,
    recency: Any | None,
) -> dict[str, Any]:
    """Build a raw/weighted/floor signal from one value accumulator bucket."""
    from theforge.model_profiles_read_model import (  # noqa: PLC0415
        _recency_params,
        _weighted_rate,
    )

    mode, half_life, window = _recency_params(recency)
    bucket = bucket or {}
    runs = int(bucket.get("runs", 0))
    ring = bucket.get(ring_key)
    ring = [float(v) for v in ring] if isinstance(ring, list) else []
    total = float(bucket.get(sum_key, 0.0))
    # ``runs`` counts uniqueness samples; latency may be measured on a subset, so
    # divide the latency sum by the number of measured latency samples, not runs.
    denom = len(ring) if ring_key == LATENCY_RING else runs
    raw = round(total / denom, 4) if denom > 0 else None
    weighted = _weighted_rate(
        ring,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = runs >= min_runs and runs > 0
    return {
        "raw": raw,
        "weighted": weighted,
        "runs": runs,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
    }


def get_reviewer_value_signal(
    profiles: dict,
    model: str,
    complexity: str | None = None,
    min_runs: int = DEFAULT_VALUE_MIN_RUNS,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
    phase: str = PLAN_PHASE,
) -> dict:
    """Return a structured reviewer *value* routing signal (#1443, #2156).

    The reviewer analog of :func:`model_profiles.get_review_signal`, over the
    section this module folds for ``phase`` (``plan_review_value`` /
    ``code_review_value``). Reads only the in-memory ``profiles`` dict (no disk,
    no LLM). Both sub-signals share the raw / weighted / floor / rate contract so
    the explain renderer surfaces them uniformly:

    - ``uniqueness_rate``: the ``min_runs``-gated recency-weighted fraction of this
      reviewer's P1s that no peer corroborated. ``rate`` is ``None`` below the
      sample floor so a cold-start reviewer falls through to tier ordering.
    - ``latency_per_p1``: the ``min_runs``-gated recency-weighted wall-clock
      seconds spent per P1 raised — the "is this reviewer earning its wall-clock
      cost?" signal.
    - ``runs`` / ``tainted_runs``: admissible sample count and the count excluded
      for taint (#1852), surfaced so the exclusion stays auditable.
    - ``resolved_population``: which concrete versions produced the samples
      (#2226), so a value rate accumulated under a family alias is not read as
      describing one reviewer when it describes several.
    """
    from theforge.model_profiles_identity import _normalize_band  # noqa: PLC0415
    from theforge.model_profiles_read_model import (  # noqa: PLC0415
        _matching_profile_entries,
        _resolved_population,
    )

    section_key = section_for_phase(phase)
    matching = _matching_profile_entries(
        profiles, model, actual_model=actual_model, provider=provider, cli=cli
    )
    band = _normalize_band(complexity) if complexity is not None else None

    # Merge all matching identity entries into a synthetic accumulator bucket so a
    # reviewer split across several profile rows aggregates cleanly.
    merged: dict[str, Any] = {
        "runs": 0,
        "tainted_runs": 0,
        UNIQUENESS_SUM: 0.0,
        LATENCY_SUM: 0.0,
        UNIQUENESS_RING: [],
        LATENCY_RING: [],
    }
    consulted: list[dict] = []
    for _, entry in matching:
        section = entry.get(section_key)
        if not isinstance(section, dict):
            continue
        if band is None:
            bucket = section
        else:
            bucket = (section.get(BY_COMPLEXITY) or {}).get(band)
            if not isinstance(bucket, dict):
                continue
        consulted.append(bucket)
        merged["runs"] += int(bucket.get("runs", 0))
        merged["tainted_runs"] += int(bucket.get("tainted_runs", 0))
        merged[UNIQUENESS_SUM] += float(bucket.get(UNIQUENESS_SUM, 0.0))
        merged[LATENCY_SUM] += float(bucket.get(LATENCY_SUM, 0.0))
        u_ring = bucket.get(UNIQUENESS_RING)
        if isinstance(u_ring, list):
            merged[UNIQUENESS_RING].extend(float(v) for v in u_ring)
        l_ring = bucket.get(LATENCY_RING)
        if isinstance(l_ring, list):
            merged[LATENCY_RING].extend(float(v) for v in l_ring)

    uniqueness = _bucket_signal(
        merged, UNIQUENESS_SUM, UNIQUENESS_RING, min_runs=min_runs, recency=recency
    )
    latency = _bucket_signal(merged, LATENCY_SUM, LATENCY_RING, min_runs=min_runs, recency=recency)
    return {
        "uniqueness_rate": uniqueness,
        "latency_per_p1": latency,
        "runs": int(merged["runs"]),
        "tainted_runs": int(merged["tainted_runs"]),
        "resolved_population": _resolved_population(consulted),
    }
